"""§6.2 step 8 — "Tonight, for {name}": step 4's round for one seat, then three picks and a
wildcard from the same pool, no session.

§6.2 forbids a session row, so the round is stateless: the client carries its answers and the
server replays them (so §14 risk 6's vote log cannot cover solo).
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from spielplan.connectors import registry
from spielplan.db import dna_terms
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import dna as dna_reads
from spielplan.tonight import pool as pool_rules
from spielplan.tonight import round as round_rules
from spielplan.tonight import tilt as tilt_rules

# 54f: "three picks and a wildcard".
PICKS = 3

# §6.2 step 8's two why-lines: "{term} · {term}", and the wildcard's label.
STRETCH_WHY = "A step outside your usual"

# §6.2 step 8's provenance line: the tilted form replaces "Unseen first", never appended to it.
PROVENANCE_PLAIN = "Unseen first · fits in {budget}"
PROVENANCE_TILTED = "Tilted by your {n} {answers} · fits in {budget}"
PROVENANCE_REWATCH = "Rewatches included · fits in {budget}"

# The same bound `home/why.py` puts on a one-line why.
NAMED_TERMS = 2


def _pair_side(candidate, genres: Mapping[int, list[str]]) -> dict[str, Any] | None:
    """One side of the round's pair, field by field so per-seat scores never ship (§6.2 step 3)."""
    if candidate is None:
        return None
    return {
        "title_id": candidate.title_id, "name": candidate.name, "year": candidate.year,
        "kind": candidate.kind, "runtime_min": candidate.runtime_min,
        "poster_path": candidate.poster_path, "fit_line": candidate.fit_line,
        "over_budget_min": candidate.over_budget_min,
        "genres": genres.get(candidate.title_id, []),
    }


def duration(minutes: int) -> str:
    """How the app writes a runtime: "2h 10m", "2h", "45m"."""
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h and m else f"{h}h" if h else f"{m}m"


def provenance(*, budget_min: int, answers: int, include_rewatches: bool, kind: str) -> str:
    budget = duration(budget_min) + (pool_rules.PER_EPISODE if kind == pool_rules.KIND_SERIES else "")
    if answers:
        return PROVENANCE_TILTED.format(
            n=answers, answers="answer" if answers == 1 else "answers", budget=budget
        )
    if include_rewatches:
        return PROVENANCE_REWATCH.format(budget=budget)
    return PROVENANCE_PLAIN.format(budget=budget)


def why_line(terms: Sequence[str]) -> str:
    line = " · ".join(terms[:NAMED_TERMS])
    return line[:1].upper() + line[1:]


async def picks(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    budget_min: int,
    include_rewatches: bool,
    bundle_version: str,
    holdout_key: str,
    answers: Sequence[round_rules.Answered] = (),
    offset: int = 0,
    sharpen: bool = False,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Three picks, a wildcard, and the round's next pair when `sharpen` asks for one.

    With no `answers` the tilt is exactly zero. `holdout_key` arrives made, so the route and this
    call derive the arm from one key; `sharpen=False` skips only the search.
    """
    seat = pool_rules.Seat(participant_id=user_id, user_id=user_id, is_member=True)
    candidates = await pool_rules.build(
        conn, seats=[seat], kind=kind, budget_min=budget_min,
        include_rewatches=include_rewatches, bundle_version=bundle_version,
    )
    if not candidates:
        return {
            "picks": [], "wildcard": None,
            "provenance": provenance(
                budget_min=budget_min, answers=0, include_rewatches=include_rewatches, kind=kind
            ),
            "empty": (
                f"Nothing in the library fits {duration(budget_min)} tonight — allow a little "
                "longer, or include rewatches."
            ),
            "pair": None, "answered": 0, "sharpened": False,
        }

    version = await dna_reads.active_version(conn)
    ids = [c.title_id for c in candidates]
    tagged = await dna_reads.vectors_for(conn, ids, version=version or "")
    # A key per candidate, so a library with no DNA still counts as the pool (§10 predicate below).
    vectors = {t: tagged.get(t, {}) for t in ids}
    # The group round's scale (decision 477); monotone, so the no-tilt order is the Ledger's own.
    prior = pool_rules.rank_normal({c.title_id: c.group_score for c in candidates})

    played = round_rules.replay(
        prior, list(answers), has_profile=True,
        axes=combine_rules.axis_positions(vectors, await dna_reads.axes_for(conn, version=version or "")),
        rng=rng or random.Random(0), holdout_key=holdout_key, select=sharpen,
    )
    # The rows the replay counted and no others (`tilt.applies`); N counts answers that tilted.
    counted = [
        a for a in answers
        if a.selection != round_rules.SELECTION_HOLDOUT
        and tilt_rules.applies(vectors, title_a=a.title_a, title_b=a.title_b)
    ]
    frame = tilt_rules.frame(vectors)
    tilt: dict[str, float] = {}
    for a in counted:
        tilt = tilt_rules.applied(
            tilt, answer=a.answer, title_a=a.title_a, title_b=a.title_b,
            vectors=vectors, frame=frame,
        )

    scored = {
        t: b.mu + tilt_rules.adjustment(tilt, vectors.get(t, {}), frame)
        for t, b in played.beliefs.items()
    }
    order = combine_rules.ranked(scored)
    by_id = {c.title_id: c for c in candidates}

    # 54f/proposal 65: reshuffle walks further down the ranking and wraps (decision 222). The span
    # is the ranking's full length, so every title is reachable.
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
    pair_genres = await pool_rules.genres_of(conn, [nxt.title_a, nxt.title_b]) if nxt else {}
    return {
        "picks": [await card(t, stretch=False) for t in chosen],
        "wildcard": None if wildcard is None else await card(wildcard, stretch=True),
        "provenance": provenance(
            budget_min=budget_min, answers=len(counted), include_rewatches=include_rewatches,
            kind=kind,
        ),
        "empty": None,
        # Every answer given (a hold-out costs one of twenty), while `sharpened` reports what tilted.
        "answered": len(answers),
        "sharpened": bool(counted),
        # A wrap by modulus or by the wrap-fill above, which fires first on uneven pools (decision 222).
        "wrapped": bool(offset) and (start + PICKS > span or start < offset * PICKS),
        # The round, on the same pool (decision 532). None once it has ended, or when none was asked.
        "pair": None if nxt is None else {
            "selection": nxt.selection,
            "reason": nxt.reason,
            "a": _pair_side(by_id.get(nxt.title_a), pair_genres),
            "b": _pair_side(by_id.get(nxt.title_b), pair_genres),
        },
        "stop_reason": played.stop_reason,
        # A seat's header and escape, named as its card names them (`play._card`).
        "cap": round_rules.CAP_PAIRS,
        "typical": round_rules.TYPICAL_PAIRS,
        "escape_available": nxt is not None and round_rules.escape_available(len(answers)),
        "tilt": tilt,
    }


__all__ = [
    "NAMED_TERMS",
    "PICKS",
    "PROVENANCE_PLAIN",
    "PROVENANCE_REWATCH",
    "PROVENANCE_TILTED",
    "STRETCH_WHY",
    "duration",
    "picks",
    "provenance",
    "why_line",
]
