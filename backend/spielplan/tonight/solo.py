"""§6.2 step 8 — "Tonight, for {name}": step 4's round for one seat, then three picks and a
wildcard from the same pool, no session.

§6.2 forbids a session row, so the round is stateless: the client carries its answers and the
server replays them (so §14 risk 6's vote log cannot cover solo). Solo asks no hold-out pair.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from spielplan.connectors import registry
from spielplan.db import dna_terms
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import copy as copy_rules
from spielplan.tonight import dna as dna_reads
from spielplan.tonight import pool as pool_rules
from spielplan.tonight import round as round_rules
from spielplan.tonight import tilt as tilt_rules

# "three picks and a wildcard".
PICKS = 3

# §6.2 step 8's why-line for the wildcard.
STRETCH_WHY = "A step outside your usual"

# The same bound `home/why.py` puts on a one-line why, and the provenance's two mood terms.
NAMED_TERMS = 2

# Below half a pool standard deviation of adjustment over the reach, the round read no strong mood.
STRONG_MOOD = 0.5


def _pair_side(film: Mapping[str, Any] | None, genres: Mapping[int, list[str]]) -> dict[str, Any] | None:
    """One side of the round's pair, field by field: never its step, never a score."""
    if film is None:
        return None
    return {
        "title_id": film["title_id"], "name": film["name"], "year": film["year"],
        "kind": film["kind"], "runtime_min": film["runtime_min"],
        "poster_path": film["poster_path"], "genres": genres.get(film["title_id"], []),
    }


def duration(minutes: int) -> str:
    """How the app writes a runtime: "2h 10m", "2h", "45m"."""
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h and m else f"{h}h" if h else f"{m}m"


def why_line(terms: Sequence[str]) -> str:
    line = " · ".join(terms[:NAMED_TERMS])
    return line[:1].upper() + line[1:]


async def _mood_terms(
    conn: asyncpg.Connection,
    space: tilt_rules.Space,
    played: round_rules.Round,
    reach: Mapping[int, Sequence[float]],
) -> list[str] | None:
    """The provenance's terms: None with no round, [] for no strong mood, else the two terms the
    mood projects back highest, as labels."""
    if not played.adaptive:
        return None
    adjust = list(round_rules.adjustments(played.mood.mean, reach).values())
    if len(adjust) < 2 or statistics.pstdev(adjust) < STRONG_MOOD:
        return []
    weights = tilt_rules.back(space.terms, space.axes, played.mood.mean)
    top = [t for t, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])) if w > 0][:NAMED_TERMS]
    named = await dna_terms.labels_for(conn, top)
    words = [str((named.get(t) or {}).get("label") or dna_terms.label_of(t, None)) for t in top]
    return [w[:1].lower() + w[1:] for w in words]


async def picks(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    budget_min: int,
    include_rewatches: bool,
    bundle_version: str,
    answers: Sequence[round_rules.Answered] = (),
    offset: int = 0,
    sharpen: bool = False,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Three picks, a wildcard, and the round's next pair when `sharpen` asks for one.

    With no `answers` the order is the person's own; `sharpen=False` skips only the draw.
    """
    seat = pool_rules.Seat(participant_id=user_id, user_id=user_id, is_member=True)
    candidates = await pool_rules.build(
        conn, seats=[seat], kind=kind, budget_min=budget_min,
        include_rewatches=include_rewatches, bundle_version=bundle_version,
    )
    budget = duration(budget_min) + (pool_rules.PER_EPISODE if kind == pool_rules.KIND_SERIES else "")
    if not candidates:
        return {
            "picks": [], "wildcard": None,
            "provenance": copy_rules.provenance(budget=budget, terms=None),
            "no_round": None,
            "empty": (
                f"Nothing in the library fits {duration(budget_min)} tonight — allow a little "
                "longer, or include rewatches."
            ),
            "pair": None, "answered": 0, "sharpened": False,
        }

    version = await dna_reads.active_version(conn)
    ids = [c.title_id for c in candidates]
    tagged = await dna_reads.vectors_for(conn, ids, version=version or "")
    vectors = {t: tagged.get(t, {}) for t in ids}
    space = tilt_rules.space(vectors)
    # The group round's scale (decision 477); monotone, so the order with no mood is the Ledger's own.
    stable = pool_rules.rank_normal({c.title_id: c.group_score for c in candidates})
    seen = await pool_rules.seen_among(conn, user_id=user_id, title_ids=ids)
    reach = {t: space.project(vectors[t]) for t in pool_rules.reach(stable, seen=seen)}

    liked, word, placed = await pool_rules.liked_films(
        conn, user_id=user_id, kind=kind, dna_version=version
    )
    has_round = round_rules.has_round(liked)
    film_dna = (
        await dna_reads.vectors_for(conn, [f["title_id"] for f in liked], version=version or "")
        if has_round else {}
    )
    films = [
        round_rules.Film(
            title_id=f["title_id"], step=f["step"], runtime_min=f["runtime_min"],
            z=space.project(film_dna.get(f["title_id"], {})),
        )
        for f in liked
    ] if has_round else []
    played = round_rules.replay(
        films, stable, reach, list(answers),
        holdout_key=None, rng=rng or random.Random(0), select=sharpen,
    )
    order = combine_rules.ranked(round_rules.tonight(stable, reach, played.mood.mean))
    by_id = {c.title_id: c for c in candidates}

    # Reshuffle walks further down the ranking, the re-ranked top 30 first, and wraps (decision 222).
    # The span is the ranking's full length, so every title is reachable.
    span = max(len(order), 1)
    start = (offset * PICKS) % span if offset else 0
    chosen = [t for t, _ in order[start:start + PICKS]]
    if len(chosen) < PICKS:
        chosen += [t for t, _ in order if t not in chosen][: PICKS - len(chosen)]
    wildcard = combine_rules.wildcard_from(order, chosen, vectors)
    # §7.1's Play on Jellyfin on every pick (decision 527); absent with no server or no item.
    play_url: dict[int, str] = {}
    link = await registry.play_link(conn)
    if link is not None:
        items = await conn.fetch(
            "SELECT id, jellyfin_id FROM title WHERE id = ANY($1::int[]) AND jellyfin_id IS NOT NULL",
            [*chosen, *([] if wildcard is None else [wildcard])],
        )
        play_url = {r["id"]: link(r["jellyfin_id"]) for r in items}

    async def card(title_id: int, *, stretch: bool) -> dict[str, Any]:
        c = by_id[title_id]
        terms = await dna_reads.terms_carried_by(
            conn, title_id, version=version or "", limit=NAMED_TERMS
        )
        # Decision 486: labels, never vocabulary ids; the id stays on `terms`.
        named = await dna_terms.labels_for(conn, [t["term"] for t in terms])
        words = [str((named.get(t["term"]) or {}).get("label") or t["term"]) for t in terms]
        return {
            "title_id": title_id, "name": c.name, "year": c.year, "kind": c.kind,
            "runtime_min": c.runtime_min, "poster_path": c.poster_path,
            "fit_line": c.fit_line, "over_budget_min": c.over_budget_min,
            "play_url": play_url.get(title_id),
            # Why there is no Play: "no server" and "not in your library" are different sentences.
            "play_reason": (
                None if title_id in play_url else "no_server" if link is None else "not_in_library"
            ),
            # §6.8: every pick gets a why, with no model noun (decision 486).
            "why": (
                STRETCH_WHY if stretch
                else why_line(words) if terms
                else "Near the top of your list tonight"
            ),
            "terms": [
                {"term": t["term"], "tier": t["tier"], "label": w}
                for t, w in zip(terms, words, strict=True)
            ],
        }

    nxt = None if played.stop_reason else played.next_pair
    shown = {f["title_id"]: f for f in liked}
    pair_genres = await pool_rules.genres_of(conn, [nxt.title_a, nxt.title_b]) if nxt else {}
    return {
        "picks": [await card(t, stretch=False) for t in chosen],
        "wildcard": None if wildcard is None else await card(wildcard, stretch=True),
        "provenance": copy_rules.provenance(
            budget=budget, terms=await _mood_terms(conn, space, played, reach)
        ),
        "no_round": None if has_round else copy_rules.no_round(
            need=round_rules.MIN_ROUND_FILMS, have=len(liked), word=word, placed=placed
        ),
        "empty": None,
        "answered": len(answers),
        # Whether any answer moved the mood.
        "sharpened": bool(played.adaptive),
        # A wrap by modulus or by the wrap-fill above, which fires first on uneven pools (decision 222).
        "wrapped": bool(offset) and (start + PICKS > span or start < offset * PICKS),
        # The round, on the same pool (decision 532). None once it has ended, or when none was asked.
        "pair": None if nxt is None else {
            "selection": nxt.selection,
            "reason": nxt.reason,
            "a": _pair_side(shown.get(nxt.title_a), pair_genres),
            "b": _pair_side(shown.get(nxt.title_b), pair_genres),
        },
        "stop_reason": played.stop_reason,
        # A seat's header and escape, named as its card names them (`play._card`).
        "cap": round_rules.CAP_PAIRS,
        "typical": round_rules.TYPICAL_PAIRS,
        "escape_available": nxt is not None and round_rules.escape_available(len(answers)),
    }


__all__ = [
    "NAMED_TERMS",
    "PICKS",
    "STRETCH_WHY",
    "STRONG_MOOD",
    "duration",
    "picks",
    "why_line",
]
