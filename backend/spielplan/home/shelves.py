"""§6.0's Home: the pending-verdicts banner and up to fifteen shelves a kind (decisions 562, 563).

A shelf has no items, only one `section` per kind, so an interleaved ranking is unrepresentable
(§4.1 rule 5, decision 18). A shelf that names terms selects its cards BY them (`why.py`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from statistics import NormalDist, fmean
from typing import Any

import asyncpg

from spielplan.db import genres as genre_vocab
from spielplan.db import library
from spielplan.home import mix, notices, suggest, taste, wish
from spielplan.home import why as why_mod
from spielplan.home.why import WhyTerm
from spielplan.ledger import ladder
from spielplan.ledger.observations import (
    DEFAULT_TIER_SET,
    LIVE_LABEL_SQL,
    cutover_sql,
    latest_tier_edit_sql,
    live_label_sql,
    rescale_level,
    tier_set_of,
)
from spielplan.scoring import serve
from spielplan.scoring.backbone import EVIDENCE_K

KIND_HEADINGS: dict[str, str] = {"movie": "Films", "series": "Series"}

SHELF_CAP = 12
SECTION_FLOOR = 3

NAMED_TITLES_CAP = 3

# Series runtime is minutes PER EPISODE (proposal 27).
SCHOOL_NIGHT_MAX_MIN: dict[str, int] = {"movie": 110, "series": 45}
SCHOOL_NIGHT_TITLE: dict[str, str] = {
    "movie": "Under 110 minutes",
    "series": "Episodes under 45 minutes",
}

# Not in the spec: below this many seen titles, "you have never watched anything X" says nothing.
FRONTIER_MIN_SEEN = 10

# Not in the spec: the tail both people must be in for §6.5's "region both like".
SWEET_SPOT_MIN_CDF = 0.70

# §5.1's optimum in this app's coordinates: β is the PERSONAL weight, so the corpus's 0.8 crowd is
# 0.2 here (decision 167). Printed as `beta_optimum`; a fitted β is what the ordering uses.
DEFAULT_BETA = 0.2

# The standard normal the sweet spot reads each member's rank through (decision 477's scale).
_NORMAL = NormalDist()

# Decision 544: Worth getting opens once this many of the person's titles of the kind shape their scores.
WORTH_GETTING_MIN_LABELS = 20
# Candidates read per card: a title no liked title is like is left out.
WORTH_GETTING_POOL = 4
# See all's whole list, longer than a shelf.
WORTH_GETTING_LIST_CAP = 60
# Decision 567: well-known feature films only; a niche genre opens on this many liked titles carrying it.
WORTH_GETTING_MIN_CROWD = 5000
WORTH_GETTING_MIN_RUNTIME = 60
WORTH_GETTING_NICHE = ("Documentary", "Music")
WORTH_GETTING_NICHE_LIKES = 3

# Decision 563: rows a kind, the first read's slots, and each family's rows and limits.
ROW_CAP = 15
HEAD_SLOTS = 5
BECAUSE_ROWS = 4
BECAUSE_TRIES = 2
TASTE_ROWS = 3
TASTE_LEADS = 12
TASTE_LIKED_MIN = 2
# Decision 565: one "{name} loved these" row per other member, up to this many.
PARTNER_ROWS = 3
GEM_SHARE = 1 / 3
GEM_MIN_CDF = 0.5
ACCLAIMED_SHARE = 0.2
# Decision 562: a real play holds a title out of the rewatch rows this long.
REWATCH_MONTHS = 12
_LONG_AGO = datetime.min.replace(tzinfo=UTC)


def _long_ago(col: str, months: str) -> str:
    return f"({col} IS NULL OR {col} < now() - make_interval(months => {months}))"


# --- payload types ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Suppressed:
    """One section that did not ship, and why. Gated with decision 117's annotations."""

    shelf: str
    kind: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"shelf": self.shelf, "kind": self.kind, "reason": self.reason}


@dataclass
class Section:
    """One kind's half of one shelf. The only place a list of titles ever lives."""

    kind: str
    heading: str
    title: str
    why: str
    why_terms: list[WhyTerm] = field(default_factory=list)
    why_numbers: dict[str, Any] = field(default_factory=dict)
    caption: str | None = None
    anchor: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "heading": self.heading,
            "title": self.title,
            "why": self.why,
            "why_terms": [t.as_dict() for t in self.why_terms],
            "why_numbers": self.why_numbers,
            "caption": self.caption,
            "anchor": self.anchor,
            "items": self.items,
        }


@dataclass
class Shelf:
    """§6.0's shelf. Note what is missing: there is no `items`, and there never will be."""

    id: str
    ranking: bool
    key: str = ""
    seen_only: bool = False
    sections: list[Section] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "ranking": self.ranking,
            "seen_only": self.seen_only,
            "sections": [s.as_dict() for s in self.sections],
        }


@dataclass(frozen=True)
class Ctx:
    """Everything every shelf builder needs, resolved once per request."""

    user_id: int
    bundle_version: str | None
    version: str | None  # the DNA vocabulary version
    kinds: tuple[str, ...]
    # Decision 475: titles earlier shelves in the claim order already show.
    claimed: frozenset[int] = frozenset()
    # Decision 512: titles the shelf's audience avoids; apart from `claimed` for the suppressed reason.
    avoided: frozenset[int] = frozenset()
    # Per kind; absent means no `ledger_cutpoints` row and no fold-in yet.
    tier_sets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    betas: dict[str, float] = field(default_factory=dict)
    day: str = ""
    nth: int = 0
    avoid: taste.Avoided | None = None
    # Reads every row of a request shares, made once: rated pools, liked terms, named terms.
    memo: dict[Any, Any] = field(default_factory=dict, compare=False)

    @property
    def excluded(self) -> frozenset[int]:
        return self.claimed | self.avoided

    def tier_set(self, kind: str) -> tuple[str, ...]:
        return self.tier_sets.get(kind, DEFAULT_TIER_SET)

    def beta(self, kind: str) -> float:
        """The weight the scores were ACTUALLY computed with; 0.0 when never fitted."""
        return self.betas.get(kind, 0.0)


# --- the pending-verdicts banner ------------------------------------------------------------


async def pending_verdicts(
    conn: asyncpg.Connection, *, user_id: int, cap: int = NAMED_TITLES_CAP
) -> dict[str, Any] | None:
    """§6.0's pending row, or None. Before the set-up: seen titles of either kind with no LIVE verdict.
    After it: seen titles never answered ("Rate {n} you watched"), else the films rated before the
    set-up that wait for their step ("Rate {n} again", decision 550).

    After the set-up, filtered by the live session's kinds so the CTA can serve what the copy names
    (proposal 150). Before it Rate is closed, and a session only journals the card's Not seen.
    """
    set_up = await ladder.set_up_at(conn, user_id=user_id)
    # One live session per person, so one row or none.
    live = None if set_up is None else await conn.fetchval(
        "SELECT kinds FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user_id
    )
    kinds = list(live or KIND_HEADINGS)
    if set_up is None:
        unanswered = """
            NOT EXISTS (
                SELECT 1 FROM verdict v
                 WHERE v.user_id = $1 AND v.title_id = t.id AND v.superseded_by IS NULL)"""
    else:
        # Neither placed since the cut-over nor answered before it, so not one of `rated_before`.
        unanswered = f"""
            NOT EXISTS (SELECT 1 FROM tier_edit e WHERE e.user_id = $1 AND e.title_id = t.id)
            AND NOT EXISTS (
                SELECT 1 FROM verdict v
                 WHERE v.user_id = $1 AND v.title_id = t.id AND NOT v.is_reask
                   AND v.created_at < {cutover_sql()})"""
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.name, t.kind
          FROM user_title ut
          JOIN title t ON t.id = ut.title_id
         WHERE ut.user_id = $1 AND ut.state = 'seen' AND t.kind = ANY($2::text[])
           AND {unanswered}
         ORDER BY ut.state_changed_at DESC, t.id DESC
        """,
        user_id,
        kinds,
    )
    again = not rows and set_up is not None
    if again:
        waiting = await ladder.rated_before(conn, user_id=user_id, kinds=kinds)
        found = await conn.fetch("SELECT id, name, kind FROM title WHERE id = ANY($1::int[])", waiting)
        by_id = {r["id"]: r for r in found}
        rows = [by_id[i] for i in waiting]
        # Rate opens on the first pin's kind, so with no live session the row counts that kind alone.
        rows = [r for r in rows if r["kind"] == rows[0]["kind"]]
    if not rows:
        return None

    total = len(rows)
    # The queue head is exactly the titles the copy names: two beyond three (proposal 21).
    named_n = total if total <= cap else cap - 1
    named = [dict(r) for r in rows[:named_n]]
    head = [int(r["id"]) for r in named]
    # Repeated `head=`, not comma-joined: `GET /api/rate` takes `head: list[int]`.
    query = "&".join(f"head={i}" for i in head)
    return {
        "count": total,
        "named": [{"title_id": int(r["id"]), "name": r["name"], "kind": r["kind"]} for r in named],
        "head_title_ids": head,
        # One compact row on every viewport (decision 528): the count, then the names.
        "copy": {
            "headline": f"Rate {total:,} again" if again else f"Rate {total:,} you watched",
            "names": " · ".join(r["name"] for r in named),
        },
        "cta": {
            "label": "Rate",
            # The server builds the link, so it cannot drift from the copy (proposal 150).
            "route": f"/rate?{query}",
        },
    }


# --- card assembly ------------------------------------------------------------------------------

# $1 user_id · $2 kind · $3 bundle_version in every statement below, so the fragments compose.
CARD_SELECT = """
        SELECT t.id AS title_id, t.kind, t.name, t.year, t.runtime_min, t.poster_path,
               t.placement, t.placement_at,
               (COALESCE(ut.state, 'unseen') = 'seen') AS seen,
               us.score, us.cf, tp.b, tp.gate, tp.item_n, tp.e_source,
               ls.s, ls.sigma, ls.cdf, ls.tier
"""

# LEFT JOIN throughout: a recency-ordered shelf must build with no score row at all.
CARD_FROM = """
          FROM title t
          LEFT JOIN user_title   ut ON ut.title_id = t.id AND ut.user_id = $1
          LEFT JOIN user_score   us ON us.title_id = t.id AND us.user_id = $1
                                   AND us.kind = t.kind AND us.bundle_version = $3
          LEFT JOIN title_prior  tp ON tp.title_id = t.id AND tp.bundle_version = $3
          LEFT JOIN ledger_state ls ON ls.title_id = t.id AND ls.user_id = $1
"""


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _card(
    row: asyncpg.Record,
    rank: int,
    *,
    tier_set: Sequence[str],
    beta: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One shelf card. Rank, seen dot and tier letter are ungated chrome; numbers live in `model`."""
    index = row["tier"]
    tier = tier_set[index] if index is not None and 0 <= index < len(tier_set) else None
    card = {
        "title_id": int(row["title_id"]),
        "kind": row["kind"],
        "name": row["name"],
        "year": row["year"],
        "runtime_min": row["runtime_min"],
        "poster_path": row["poster_path"],
        "placement": row["placement"],
        # Outside `model`: §8 stage 10's cold badge is product and reads these two, ungated.
        "item_n": row["item_n"],
        "e_source": row["e_source"],
        "seen": bool(row["seen"]),
        "rank": rank,
        "tier": tier,
        "model": {
            "score": _float(row["score"]),
            "cf": _float(row["cf"]),
            "b": _float(row["b"]),
            "gate": _float(row["gate"]),
            "beta": beta,
            "s": _float(row["s"]),
            "sigma": _float(row["sigma"]),
            "cdf": _float(row["cdf"]),
            "tier_index": index,
        },
    }
    if extra:
        card["model"].update(extra)
    return card


def _finish(section: Section, *, shelf_id: str, ctx: Ctx) -> tuple[Section | None, Suppressed | None]:
    """The gate every section passes: the floor and a why-line."""
    if len(section.items) < SECTION_FLOOR:
        after = _thinned_by(ctx)
        return None, Suppressed(
            shelf_id,
            section.kind,
            f"{len(section.items)} qualifying titles{after} · the floor is {SECTION_FLOOR}",
        )
    if not section.why.strip():
        return None, Suppressed(
            shelf_id, section.kind,
            "no why-line — a shelf that cannot say why it exists does not ship",
        )
    return section, None


def _thinned_by(ctx: Ctx) -> str:
    """The clause a suppressed reason adds when the claim (decision 475) or the member's avoid set
    (decision 512) is what left a shelf short."""
    parts = []
    if ctx.claimed:
        parts.append("once the shelves before it took theirs")
    if ctx.avoided:
        parts.append("leaving out titles like ones you disliked")
    return (" " + " and ".join(parts)) if parts else ""


def _board_level(row: asyncpg.Record, k: int) -> int | None:
    """The tier Rank renders: the latest drop rescaled to today's set (decision 11), else the model's."""
    if row["assigned"] is not None:
        return rescale_level(int(row["assigned"]), k_from=row["assigned_k"], k_to=k)
    return None if row["model_tier"] is None else int(row["model_tier"])


# --- because_anchor -------------------------------------------------------------------------


def _daily(user_id: int, day: str, key: str) -> int:
    """The day's order (decision 563): the same member on the same day reads the same Home."""
    return int.from_bytes(hashlib.sha256(f"{user_id}:{day}:{key}".encode()).digest()[:8], "big")


async def _cards(
    conn: asyncpg.Connection,
    ctx: Ctx,
    kind: str,
    ids: Sequence[int],
    *,
    seen: bool = False,
    order: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """Cards for the owned `ids` the member has (not) seen: by their score, or in `order`."""
    rows = await conn.fetch(
        CARD_SELECT + CARD_FROM + """
         WHERE t.kind = $2 AND t.id = ANY($4::int[]) AND t.is_owned
           AND (COALESCE(ut.state, 'unseen') = 'seen') = $5
         ORDER BY us.score DESC NULLS LAST, t.year DESC NULLS LAST, t.id
        """,
        ctx.user_id, kind, ctx.bundle_version, list(ids), seen,
    )
    if order is not None:
        by_id = {int(r["title_id"]): r for r in rows}
        rows = [by_id[i] for i in order if i in by_id]
    tier_set, beta = ctx.tier_set(kind), ctx.beta(kind)
    return [_card(row, i + 1, tier_set=tier_set, beta=beta) for i, row in enumerate(rows[:SHELF_CAP])]


async def _rated_seen(
    conn: asyncpg.Connection, ctx: Ctx, kind: str, user_id: int
) -> list[dict[str, Any]]:
    """A member's seen and rated titles of a kind (neither implies the other) at the level their board
    shows, `loved` at its top two steps, `long_ago` with no real play in REWATCH_MONTHS."""
    key = ("rated", user_id, kind)
    if key not in ctx.memo:
        tier_set = (
            ctx.tier_set(kind) if user_id == ctx.user_id
            else await tier_set_of(conn, user_id=user_id, kind=kind)
        )
        rows = await conn.fetch(
            f"""
            SELECT t.id, t.name, t.is_owned, ls.tier AS model_tier, ut.played_at,
                   te.tier AS assigned, te.n_levels AS assigned_k, lv.value AS verdict,
                   {_long_ago("ut.played_at", "$3")} AS long_ago
              FROM ledger_state ls
              JOIN title t ON t.id = ls.title_id
              JOIN user_title ut ON ut.user_id = ls.user_id AND ut.title_id = t.id
                                AND ut.state = 'seen'
              LEFT JOIN ({latest_tier_edit_sql()}) te ON te.title_id = ls.title_id
              LEFT JOIN ({LIVE_LABEL_SQL}) lv ON lv.title_id = ls.title_id
             WHERE ls.user_id = $1 AND t.kind = $2 AND ls.tier IS NOT NULL AND ls.observed
            """,
            user_id,
            kind,
            REWATCH_MONTHS,
        )
        k = len(tier_set)
        ctx.memo[key] = [
            {**dict(r), "level": level, "loved": level >= k - 2}
            for r in rows
            for level in [_board_level(r, k)]
        ]
    return ctx.memo[key]


def _anchors(ctx: Ctx, rated: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The loved titles by step, then verdict, then the day's order; none loved, the single best."""
    loved = [r for r in rated if r["loved"]]
    ordered = sorted(
        loved or rated,
        key=lambda r: (
            -r["level"],
            -(r["verdict"] if r["verdict"] is not None else -1),
            _daily(ctx.user_id, ctx.day, f"anchor:{r['id']}"),
        ),
    )
    return ordered if loved else ordered[:1]


async def because_anchor(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 — "Because you liked *{anchor}*" or "More like *{anchor}*" / "{term} · {term}".

    Row `ctx.nth` tries anchors nth, nth + BECAUSE_ROWS, so no two rows, in either read, share one.
    """
    sid = "because_anchor"
    if not ctx.version:
        return None, Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")
    rated = await _rated_seen(conn, ctx, kind, ctx.user_id)
    if not rated:
        return None, Suppressed(
            sid, kind,
            "no title of this kind is both seen and rated with a fitted tier yet",
        )
    tries = _anchors(ctx, rated)[ctx.nth::BECAUSE_ROWS][:BECAUSE_TRIES]
    if not tries:
        return None, Suppressed(sid, kind, "no loved title of this kind left to anchor this row")
    for anchor in tries:
        section, note = await _because(conn, ctx=ctx, kind=kind, anchor=anchor)
        if section is not None:
            break
    return section, note


async def _because(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str, anchor: dict[str, Any]
) -> tuple[Section | None, Suppressed | None]:
    """One anchor's likest unseen owned titles (decision 513), and a term pair every card carries.
    The headline names no tier (decision 527)."""
    sid = "because_anchor"
    tier_set = ctx.tier_set(kind)
    # Checked, not clamped: a model tier outside the set is a bug, not a state.
    model_index = int(anchor["model_tier"])
    if not 0 <= model_index < len(tier_set):
        return None, Suppressed(
            sid, kind, f"anchor tier index {model_index} is outside the tier set"
        )
    index = anchor["level"]

    # Decision 513: the anchor's nearest titles, then the pair of its terms that names them best.
    terms = await why_mod.terms_for(conn, int(anchor["id"]), version=ctx.version, limit=None)
    neighbours, specificity = await why_mod.anchor_neighbours(
        conn,
        user_id=ctx.user_id,
        kind=kind,
        version=ctx.version,
        bundle_version=ctx.bundle_version,
        anchor_id=int(anchor["id"]),
        exclude=sorted(ctx.excluded),
        limit=why_mod.NEIGHBOURHOOD_PER_CARD * SHELF_CAP,
    )
    chosen = why_mod.likest_pair(
        neighbours, specificity, terms, cap=SHELF_CAP, floor=SECTION_FLOOR
    )
    if chosen is None:
        return None, Suppressed(
            sid, kind,
            f"no pair of {anchor['name']}'s terms is shared by {SECTION_FLOOR} of its nearest "
            f"unseen owned titles{_thinned_by(ctx)}",
        )
    members, t1, t2 = chosen
    tier = tier_set[index]
    if anchor["verdict"] == 2:
        title = f"Because you liked {anchor['name']}"
    else:
        title = f"More like {anchor['name']}"
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=title,
        why=why_mod.phrase([t1, t2]),
        why_terms=[t1.with_role("member"), t2.with_role("member")],
        anchor={"title_id": int(anchor["id"]), "name": anchor["name"], "tier": tier},
        items=await _cards(conn, ctx, kind, members, order=members),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- shelf 2: top_of_ledger -------------------------------------------------------------------


async def top_of_ledger(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 — "Your top picks", from `scoring.serve.top_scored`, the one ranked statement; unseen
    titles only (decision 562)."""
    sid = "top_of_ledger"
    if not ctx.bundle_version:
        return None, Suppressed(sid, kind, "no active artifact bundle — no scores to rank")

    # Not thinned by the claim (decision 475), but by what the member avoids (decision 512).
    ranked = await serve.top_scored(
        conn,
        user_id=ctx.user_id,
        kind=kind,
        bundle_version=ctx.bundle_version,
        limit=SHELF_CAP,
        exclude=sorted(ctx.avoided),
        unseen_only=True,
    )
    # The β the ordering used: stored `blend_beta`, or 0.0 when never fitted.
    beta = float(ranked["beta"])
    personalised = bool(ranked["personalised"])
    tier_set = ctx.tier_set(kind)
    items = [
        _card(row, i + 1, tier_set=tier_set, beta=beta)
        for i, row in enumerate(_as_card_rows(ranked["items"]))
    ]
    why = (
        # Member register (decision 476); β travels in the gated `why_numbers`.
        "The ones we think you'll enjoy most"
        if personalised
        else "What most people rate highest, until your own ratings take over"
    )
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title="Your top picks",
        why=why,
        why_numbers={"beta": beta, "beta_fitted": bool(ranked["fitted"]),
                     "beta_optimum": DEFAULT_BETA, "label_count": ranked["label_count"],
                     "gate_k": EVIDENCE_K},
        caption=None,
        items=items,
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


def _as_card_rows(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """`serve.top_scored` returns `id` and `seen_state`; `_card` reads `title_id` and `seen`."""
    return [
        dict(item, title_id=item["id"], seen=item["seen_state"] == "seen", placement_at=None)
        for item in items
    ]


# --- shelf 3: never_watched_term --------------------------------------------------------------


async def never_watched_term(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 3 — "You've never watched anything *{term}*" (§6.4's frontier as a shelf).

    The candidate term is a card `member`; the liked neighbour (decision 514) is `anchor_side` only.
    """
    sid = "never_watched_term"
    if not ctx.version:
        return None, Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")

    found = await why_mod.frontier_term(
        conn,
        user_id=ctx.user_id,
        kind=kind,
        version=ctx.version,
        min_seen=FRONTIER_MIN_SEEN,
        carrier_floor=SECTION_FLOOR,
        exclude=sorted(ctx.excluded),
        liked=await _liked(conn, ctx, kind),
    )
    if found is None:
        return None, Suppressed(
            sid, kind,
            f"no zero-coverage term carries {SECTION_FLOOR} unseen owned titles next to a term "
            f"from another facet that you liked on {why_mod.LIKED_TERM_MIN} titles and on most "
            f"you rated, or fewer than {FRONTIER_MIN_SEEN} seen titles of this kind to call any "
            f"region unvisited{_thinned_by(ctx)}",
        )
    candidate, neighbour, cos, aff = found

    ids = await why_mod.carriers(
        conn, terms=[candidate.term], kind=kind, version=ctx.version, user_id=ctx.user_id,
        exclude=sorted(ctx.excluded),
    )
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=f"You've never watched anything {candidate.name}",
        why=f"Close to {neighbour.name}, which you like",
        why_terms=[candidate, neighbour],
        why_numbers={"cos": round(cos, 4), "affinity": round(aff, 4),
                     "min_seen": FRONTIER_MIN_SEEN},
        # §6.4's "honestly labelled" exploratory slot, in words.
        caption="A step outside what you usually watch, on purpose",
        items=await _cards(conn, ctx, kind, ids),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- shelf 4: shared_sweet_spot ---------------------------------------------------------------


async def others_for(conn: asyncpg.Connection, *, user_id: int) -> list[dict[str, Any]]:
    """Proposal 26's order: the other active members by co-seen titles, then the latest, then id."""
    rows = await conn.fetch(
        """
        SELECT u.id, u.name
          FROM app_user u
          LEFT JOIN user_title b ON b.user_id = u.id AND b.state = 'seen'
                                AND EXISTS (SELECT 1 FROM user_title a
                                             WHERE a.user_id = $1 AND a.title_id = b.title_id
                                               AND a.state = 'seen')
         WHERE u.id <> $1 AND u.is_active AND u.role IN ('admin', 'member')
         GROUP BY u.id, u.name
         ORDER BY count(b.title_id) DESC, max(b.state_changed_at) DESC NULLS LAST, u.id
        """,
        user_id,
    )
    return [{"user_id": int(r["id"]), "name": r["name"]} for r in rows]


def _standardised(pos: int, n: int) -> float:
    return _NORMAL.inv_cdf((pos - 0.5) / n)


async def shared_sweet_spot(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 — "You and {other} would both enjoy these" / "You'd all enjoy these": unseen by every
    household member, above every member's CDF floor (decision 565).

    Ordered by the plain average of rank-standardised scores over the owned library, as Tonight's
    pool is (decision 477). Any member's avoid set is excluded (decision 512).
    """
    sid = "shared_sweet_spot"
    others = ctx.memo["household", kind]
    if not others:
        return None, Suppressed(sid, kind, "no other member with scores to share a sweet spot with")
    if not ctx.bundle_version:
        return None, Suppressed(sid, kind, "no active artifact bundle — no scores to intersect")

    beta, tier_set = ctx.beta(kind), ctx.tier_set(kind)
    members = [ctx.user_id, *(o["user_id"] for o in others)]
    # `pos`/`n` are each member's rank over the owned library of the kind, ties broken by id as
    # `tonight/pool.rank_normal` breaks them, so the quantile below is that function's.
    rows = await conn.fetch(
        """
        WITH ranked AS (
            SELECT us.user_id, us.title_id, us.score,
                   percent_rank() OVER (PARTITION BY us.user_id ORDER BY us.score) AS cdf,
                   row_number() OVER (PARTITION BY us.user_id ORDER BY us.score, us.title_id)
                       AS pos,
                   count(*) OVER (PARTITION BY us.user_id) AS n
              FROM user_score us
              JOIN title o ON o.id = us.title_id AND o.is_owned
             WHERE us.user_id = ANY($4::int[]) AND us.kind = $2 AND us.bundle_version = $3
        ), everyone AS (
            SELECT title_id, array_agg(pos) AS pos, array_agg(n) AS n,
                   min(cdf) FILTER (WHERE user_id <> $1) AS theirs_cdf
              FROM ranked
             GROUP BY title_id
            HAVING count(*) FILTER (WHERE cdf >= $5) = cardinality($4::int[])
        )
        SELECT t.id AS title_id, t.kind, t.name, t.year, t.runtime_min, t.poster_path,
               t.placement, NULL::timestamptz AS placement_at,
               false AS seen,
               a.score, NULL::real AS cf, tp.b, tp.gate, tp.item_n, tp.e_source,
               ls.s, ls.sigma, ls.cdf, ls.tier,
               a.cdf AS mine_cdf, e.theirs_cdf, e.pos, e.n
          FROM ranked a
          JOIN everyone e ON e.title_id = a.title_id
          JOIN title t ON t.id = a.title_id
          LEFT JOIN title_prior  tp ON tp.title_id = t.id AND tp.bundle_version = $3
          LEFT JOIN ledger_state ls ON ls.title_id = t.id AND ls.user_id = $1
         WHERE a.user_id = $1 AND t.is_owned
           AND NOT EXISTS (SELECT 1 FROM user_title s WHERE s.title_id = t.id
                            AND s.user_id = ANY($4::int[]) AND s.state = 'seen')
           AND NOT (t.id = ANY($6))
        """,
        ctx.user_id, kind, ctx.bundle_version, members, SWEET_SPOT_MIN_CDF, sorted(ctx.excluded),
    )

    paired = sorted(
        ((fmean(map(_standardised, r["pos"], r["n"])), r) for r in rows),
        key=lambda pr: (-pr[0], int(pr[1]["title_id"])),
    )[:SHELF_CAP]
    items = [
        _card(
            row, i + 1, tier_set=tier_set, beta=beta,
            extra={
                "mine_cdf": _float(row["mine_cdf"]),
                "theirs_cdf": _float(row["theirs_cdf"]),
                "pair_score": pair_score,
            },
        )
        for i, (pair_score, row) in enumerate(paired)
    ]
    two = len(others) == 1
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=f"You and {others[0]['name']} would both enjoy these" if two else "You'd all enjoy these",
        why=("Neither of you" if two else "None of you")
        + " has seen them — a good pick for a night in together",
        why_numbers={"min_cdf": SWEET_SPOT_MIN_CDF, "member_user_ids": members[1:]},
        caption=None,
        items=items,
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- shelf 5: school_night --------------------------------------------------------------------


async def school_night(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 5 — "Under 110 minutes". A NULL runtime is excluded; the bound is strict."""
    sid = "school_night"
    limit_min = SCHOOL_NIGHT_MAX_MIN[kind]
    beta, tier_set = ctx.beta(kind), ctx.tier_set(kind)
    rows = await conn.fetch(
        CARD_SELECT + CARD_FROM + """
         WHERE t.kind = $2 AND t.is_owned AND t.runtime_min IS NOT NULL AND t.runtime_min < $4
           AND COALESCE(ut.state, 'unseen') = 'unseen' AND NOT (t.id = ANY($6))
         ORDER BY us.score DESC NULLS LAST, t.runtime_min, t.id
         LIMIT $5
        """,
        ctx.user_id, kind, ctx.bundle_version, limit_min, SHELF_CAP, sorted(ctx.excluded),
    )
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=SCHOOL_NIGHT_TITLE[kind],
        why="For a school night",
        why_numbers={"max_minutes": limit_min},
        caption=(
            "Series runtime is minutes per episode" if kind == "series" else None
        ),
        items=[_card(row, i + 1, tier_set=tier_set, beta=beta) for i, row in enumerate(rows)],
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- taste rows (decision 563) ----------------------------------------------------------------


async def _liked(conn: asyncpg.Connection, ctx: Ctx, kind: str) -> list[tuple[WhyTerm, float]]:
    if ("liked", kind) not in ctx.memo:
        ctx.memo["liked", kind] = await why_mod.liked_terms(
            conn, user_id=ctx.user_id, kind=kind, version=ctx.version
        )
    return ctx.memo["liked", kind]


@dataclass(frozen=True)
class _Taste:
    """Per liked term: the liked seen titles carrying it, best first; the unseen owned ones; the liked
    owned ones with no real play in REWATCH_MONTHS."""

    liked: list[tuple[WhyTerm, float]]
    carried: dict[str, list[int]]
    names: dict[int, str]
    unseen: dict[str, set[int]]
    again: dict[str, set[int]]


async def _taste(conn: asyncpg.Connection, ctx: Ctx, kind: str) -> _Taste:
    if ("taste", kind) in ctx.memo:
        return ctx.memo["taste", kind]
    liked = await _liked(conn, ctx, kind)
    terms = [t.term for t, _aff in liked]
    rows = await conn.fetch(
        f"""
        WITH lv AS ({LIVE_LABEL_SQL})
        SELECT DISTINCT d.term, t.id, t.name, t.is_owned, ls.s,
               {_long_ago("ut.played_at", "$4")} AS long_ago
          FROM lv
          JOIN title t ON t.id = lv.title_id AND t.kind = $2
          JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1 AND ut.state = 'seen'
          JOIN dna_tagged d ON d.title_id = t.id AND d.version = $3 AND d.term = ANY($5::text[])
          LEFT JOIN ledger_state ls ON ls.user_id = $1 AND ls.title_id = t.id
         WHERE lv.value = {taste.LIKED}
         ORDER BY ls.s DESC NULLS LAST, t.id
        """,
        ctx.user_id, kind, ctx.version, REWATCH_MONTHS, terms,
    )
    carried: dict[str, list[int]] = {}
    again: dict[str, set[int]] = {}
    for r in rows:
        carried.setdefault(r["term"], []).append(int(r["id"]))
        if r["is_owned"] and r["long_ago"]:
            again.setdefault(r["term"], set()).add(int(r["id"]))
    unseen: dict[str, set[int]] = {}
    for r in await conn.fetch(
        """
        SELECT DISTINCT d.term, d.title_id
          FROM dna_tagged d
          JOIN title t ON t.id = d.title_id
          LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
         WHERE d.version = $3 AND d.term = ANY($4::text[]) AND t.kind = $2 AND t.is_owned
           AND COALESCE(ut.state, 'unseen') = 'unseen'
        """,
        ctx.user_id, kind, ctx.version, terms,
    ):
        unseen.setdefault(r["term"], set()).add(int(r["title_id"]))
    found = _Taste(liked, carried, {int(r["id"]): r["name"] for r in rows}, unseen, again)
    ctx.memo["taste", kind] = found
    return found


async def _term_pair(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str, again: bool
) -> tuple[Section | None, Suppressed | None]:
    """"{Label} & {label}": unseen owned titles carrying two liked terms of different groups, or with
    `again` the liked ones not played in a long while. No term is named twice in a kind's Home."""
    sid = "rewatch_term" if again else "taste_term"
    if not ctx.version:
        return None, Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")
    found = await _taste(conn, ctx, kind)
    leads = sorted(
        (t.term for t, _aff in found.liked[:TASTE_LEADS]),
        key=lambda term: _daily(ctx.user_id, ctx.day, f"term:{term}"),
    )
    named = ctx.memo.setdefault(("named", kind), set())
    pair = why_mod.taste_pair(
        found.liked,
        {term: set(ids) for term, ids in found.carried.items()},
        {term: ids - ctx.excluded for term, ids in (found.again if again else found.unseen).items()},
        leads=leads[ctx.nth:] + leads[:ctx.nth],
        taken=named,
        liked_min=TASTE_LIKED_MIN,
        floor=SECTION_FLOOR,
    )
    if pair is None:
        return None, Suppressed(
            sid, kind,
            f"no two liked terms of different groups are carried by {TASTE_LIKED_MIN} titles you "
            f"liked and by {SECTION_FLOOR} "
            f"{'you liked and have not watched in a long while' if again else 'unseen owned titles'}"
            f"{_thinned_by(ctx)}",
        )
    first, second, ids = pair
    # Labels such as "twists & turns" carry their own ampersand.
    title = f"{first.name}{', ' if '&' in first.name + second.name else ' & '}{second.name}"
    title = title[:1].upper() + title[1:]
    if again:
        order = sorted(ids, key=lambda i: _daily(ctx.user_id, ctx.day, f"title:{i}"))
        section = Section(
            kind=kind, heading=KIND_HEADINGS[kind], title=f"{title}, again",
            why="Ones you liked, not watched in a long while",
            why_terms=[first, second], why_numbers={"months": REWATCH_MONTHS},
            items=await _cards(conn, ctx, kind, order, seen=True, order=order),
        )
    else:
        both = set(found.carried[second.term])
        films = [found.names[i] for i in found.carried[first.term] if i in both][:2]
        section = Section(
            kind=kind, heading=KIND_HEADINGS[kind], title=title,
            why=f"Like {films[0]} and {films[1]}, which you liked",
            why_terms=[first, second],
            items=await _cards(conn, ctx, kind, sorted(ids)),
        )
    section, note = _finish(section, shelf_id=sid, ctx=ctx)
    if section is not None:
        named.update((first.term, second.term))
    return section, note


taste_term = partial(_term_pair, again=False)
rewatch_term = partial(_term_pair, again=True)


# --- hidden gems, acclaimed, the other member's (decision 563) --------------------------------


async def hidden_gems(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """Crowd-rated titles in the least-rated GEM_SHARE of the kind's owned ones, in the upper half of
    the member's own scores."""
    sid = "hidden_gems"
    rows = await conn.fetch(
        """
        WITH k AS (
            SELECT t.id, tp.item_n, percent_rank() OVER (ORDER BY us.score NULLS FIRST) AS mine
              FROM title t
              LEFT JOIN title_prior tp ON tp.title_id = t.id AND tp.bundle_version = $3
              LEFT JOIN user_score us ON us.title_id = t.id AND us.user_id = $1
                                     AND us.kind = t.kind AND us.bundle_version = $3
             WHERE t.kind = $2 AND t.is_owned
        ), g AS (
            SELECT id, mine, cume_dist() OVER (ORDER BY item_n) AS crowd FROM k WHERE item_n > 0
        )
        SELECT id FROM g WHERE crowd <= $4 AND mine >= $5
        """,
        ctx.user_id, kind, ctx.bundle_version, GEM_SHARE, GEM_MIN_CDF,
    )
    section = Section(
        kind=kind, heading=KIND_HEADINGS[kind], title="Hidden gems",
        why="Few people have rated them, but they're close to your taste",
        why_numbers={"share": GEM_SHARE, "min_cdf": GEM_MIN_CDF},
        items=await _cards(conn, ctx, kind, [int(r["id"]) for r in rows if r["id"] not in ctx.excluded]),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


async def acclaimed(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """Decision 547's platform score orders this row and enters no model (§4.1 rule 3)."""
    sid = "acclaimed"
    ids = await library.acclaimed(
        conn, kind=kind, share=ACCLAIMED_SHARE, min_votes=mix.WELL_KNOWN_VOTES
    )
    order = [i for i in ids if i not in ctx.excluded]
    section = Section(
        kind=kind, heading=KIND_HEADINGS[kind], title="Acclaimed",
        why="Among the highest rated anywhere",
        why_numbers={"share": ACCLAIMED_SHARE, "min_votes": mix.WELL_KNOWN_VOTES},
        items=await _cards(conn, ctx, kind, order, order=order),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


async def partner_loved(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """"{other} loved these", row `ctx.nth` for the nth other member: their top two steps, never
    which (decision 563 item 4)."""
    sid = "partner_loved"
    others = ctx.memo["others"]
    if ctx.nth >= len(others):
        return None, Suppressed(sid, kind, f"no other member {ctx.nth + 1}")
    member = others[ctx.nth]
    theirs = await _rated_seen(conn, ctx, kind, member["user_id"])
    section = Section(
        kind=kind, heading=KIND_HEADINGS[kind], title=f"{member['name']} loved these",
        why="You haven't seen them yet",
        why_numbers={"partner_user_id": member["user_id"]},
        items=await _cards(
            conn, ctx, kind, [r["id"] for r in theirs if r["loved"] and r["id"] not in ctx.excluded]
        ),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- rewatch rows (decision 562) --------------------------------------------------------------


async def watch_again(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """The member's loved titles with no real play in REWATCH_MONTHS: never played first, then the
    longest ago."""
    sid = "watch_again"
    rated = await _rated_seen(conn, ctx, kind, ctx.user_id)
    due = sorted(
        (r for r in rated if r["loved"] and r["long_ago"] and r["is_owned"]
         and r["id"] not in ctx.excluded),
        key=lambda r: (r["played_at"] or _LONG_AGO, _daily(ctx.user_id, ctx.day, f"title:{r['id']}")),
    )
    order = [r["id"] for r in due]
    section = Section(
        kind=kind, heading=KIND_HEADINGS[kind], title="Watch again",
        why="Ones you loved, not watched in a long while",
        why_numbers={"months": REWATCH_MONTHS},
        items=await _cards(conn, ctx, kind, order, seen=True, order=order),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


async def rewatch_together(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """"Watch again with {other}" / "Watch again together": seen and liked by every household
    member, none with a real play in REWATCH_MONTHS, ordered by the oldest play."""
    sid = "rewatch_together"
    others = ctx.memo["household", kind]
    if not others:
        return None, Suppressed(sid, kind, "no other member to watch again with")
    members = [ctx.user_id, *(o["user_id"] for o in others)]
    rows = await conn.fetch(
        f"""
        WITH lv AS (
            SELECT m.id AS user_id, l.title_id, l.value
              FROM unnest($3::int[]) m(id), LATERAL ({live_label_sql('m.id')}) l
        )
        SELECT t.id,
               CASE WHEN bool_or(ut.played_at IS NULL) THEN NULL ELSE min(ut.played_at) END AS oldest
          FROM title t
          JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = ANY($3::int[]) AND ut.state = 'seen'
          JOIN lv ON lv.user_id = ut.user_id AND lv.title_id = t.id AND lv.value = {taste.LIKED}
         WHERE t.kind = $1 AND t.is_owned AND NOT (t.id = ANY($4::int[]))
           AND {_long_ago("ut.played_at", "$2")}
         GROUP BY t.id
        HAVING count(*) = cardinality($3::int[])
        """,
        kind, REWATCH_MONTHS, members, sorted(ctx.excluded),
    )
    order = [
        int(r["id"]) for r in sorted(
            rows,
            key=lambda r: (r["oldest"] or _LONG_AGO, _daily(ctx.user_id, ctx.day, f"title:{r['id']}")),
        )
    ]
    two = len(others) == 1
    section = Section(
        kind=kind, heading=KIND_HEADINGS[kind],
        title=f"Watch again with {others[0]['name']}" if two else "Watch again together",
        why=f"You {'both' if two else 'all'} liked these, and it's been a while",
        why_numbers={"months": REWATCH_MONTHS, "member_user_ids": members[1:]},
        items=await _cards(conn, ctx, kind, order, seen=True, order=order),
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- shelf 6: new_in_library ------------------------------------------------------------------


async def new_in_library(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 6 — "New in the library": by recency, so it ships with zero verdicts.

    Requires no crowd rating at all (`item_n` 0): cold-masked rows are crowd-rated. Not claimed.
    """
    sid = "new_in_library"
    beta, tier_set = ctx.beta(kind), ctx.tier_set(kind)
    rows = await conn.fetch(
        CARD_SELECT + CARD_FROM + """
         WHERE t.kind = $2 AND t.is_owned AND t.placement = 'cold_tower'
           AND COALESCE(ut.state, 'unseen') = 'unseen'
           AND (tp.e_source IS NULL OR tp.e_source IN ('cold_tower', 'none'))
           AND COALESCE(tp.item_n, 0) = 0
         ORDER BY t.placement_at DESC NULLS LAST, t.id DESC
         LIMIT $4
        """,
        ctx.user_id, kind, ctx.bundle_version, SHELF_CAP,
    )
    wanted = await wish.wanted_by(
        conn, user_id=ctx.user_id, title_ids=[int(r["title_id"]) for r in rows]
    )
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title="New in the library",
        why="No outside ratings yet, so we placed them by what they're about",
        why_numbers={"gate_k": EVIDENCE_K},
        items=[
            {**_card(row, i + 1, tier_set=tier_set, beta=beta),
             "wanted": int(row["title_id"]) in wanted}
            for i, row in enumerate(rows)
        ],
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


# --- shelf 7: worth_getting -------------------------------------------------------------------


async def _worth_getting_opens(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> tuple[bool, int]:
    """Whether the person's own ratings of the kind are enough to suggest beyond the library."""
    fit = await serve.fit_row(conn, user_id=user_id, kind=kind)
    labels = int(fit["label_count"] or 0) if fit else 0
    beta = float(fit["blend_beta"] or 0.0) if fit else 0.0
    return labels >= WORTH_GETTING_MIN_LABELS and beta > 0.0, labels


def _with_likes(
    rows: Sequence[asyncpg.Record],
    likes: dict[int, suggest.Liked],
    *,
    ctx: Ctx,
    kind: str,
    cap: int,
    required: bool,
) -> list[dict[str, Any]]:
    """Cards carrying `like` and `wanted`; with `required`, a title no liked title is like is left out."""
    tier_set, beta = ctx.tier_set(kind), ctx.beta(kind)
    kept = [r for r in rows if not required or int(r["title_id"]) in likes][:cap]
    cards = []
    for i, row in enumerate(kept):
        liked = likes.get(int(row["title_id"]))
        cards.append({
            **_card(row, i + 1, tier_set=tier_set, beta=beta),
            "like": liked.as_dict() if liked else None,
            "wanted": row["wish_state"] == "want",
        })
    return cards


async def _likes(
    conn: asyncpg.Connection, rows: Sequence[asyncpg.Record], *, ctx: Ctx, kind: str
) -> dict[int, suggest.Liked]:
    """The viewer's own liked titles alone, whoever the list is for, so no one's ratings show to
    another (decision 552)."""
    return await suggest.likest_liked(
        conn, user_id=ctx.user_id, title_ids=[int(r["title_id"]) for r in rows], kind=kind,
        version=ctx.version,
    )


async def _shut_genres(conn: asyncpg.Connection, member_ids: Sequence[int], kind: str) -> list[str]:
    """Raw genre labels left out: TV Movie always, a niche genre unless every member the list is for
    has liked enough titles of the kind carrying it."""
    shut = ["tv movie"]
    for genre in WORTH_GETTING_NICHE:
        raw = genre_vocab.raw_labels(genre)
        for member_id in member_ids:
            liked = await conn.fetchval(
                f"WITH lv AS ({live_label_sql('$1')}) SELECT count(*) FROM lv"
                " JOIN title t ON t.id = lv.title_id AND t.kind = $2"
                f" WHERE lv.value = 2 AND {genre_vocab.predicate('$3', '$4')}",
                member_id, kind, raw, list(genre_vocab.EXCLUDED_SOURCES),
            )
            if liked < WORTH_GETTING_NICHE_LIKES:
                shut += raw
                break
    return shut


def _feature_sql(shut: str, excluded: str, decade: str) -> str:
    """Over aliases `t` and `tp`: a well-known feature film, in the decade when one is bound."""
    return f"""
           AND tp.item_n >= {WORTH_GETTING_MIN_CROWD} AND t.runtime_min >= {WORTH_GETTING_MIN_RUNTIME}
           AND NOT {genre_vocab.predicate(shut, excluded)}
           AND ({decade}::int IS NULL OR t.year >= {decade}::int AND t.year < {decade}::int + 10)
    """


async def _unowned_for_one(
    conn: asyncpg.Connection,
    *,
    ctx: Ctx,
    kind: str,
    member_id: int,
    avoids: Sequence[taste.Avoided],
    cap: int,
    keep_wanted: bool,
    decade: int | None = None,
) -> list[dict[str, Any]]:
    """Unowned titles of the kind by one member's own score, with that member's leave-outs and a crowd
    rating. The card and its Want are the viewer's; the viewer's own list keeps only titles like one
    they liked (decision 515)."""
    avoided = await taste.avoided_titles(conn, avoids, kind=kind, version=ctx.version, owned=False)
    rows = await conn.fetch(
        f"WITH lv AS ({live_label_sql('$7')})" + CARD_SELECT + ", w.state AS wish_state" + CARD_FROM
        + """
          JOIN user_score ms ON ms.title_id = t.id AND ms.user_id = $7 AND ms.kind = t.kind
                            AND ms.bundle_version = $3
          LEFT JOIN wish w ON w.title_id = t.id AND w.user_id = $1
         WHERE t.kind = $2 AND NOT t.is_owned""" + _feature_sql("$8", "$9", "$10") + """
           AND NOT EXISTS (SELECT 1 FROM user_title s WHERE s.title_id = t.id
                            AND s.user_id = $7 AND s.state = 'seen')
           AND t.id NOT IN (SELECT title_id FROM lv)
           AND NOT EXISTS (SELECT 1 FROM wish x WHERE x.title_id = t.id
                            AND x.user_id = $7 AND x.state = 'not_for_me')
           AND (w.state IS NULL OR $6)
           AND NOT (t.id = ANY($4::int[]))
         ORDER BY ms.score DESC, t.id
         LIMIT $5
        """,
        ctx.user_id, kind, ctx.bundle_version, sorted(avoided), cap * WORTH_GETTING_POOL,
        keep_wanted, member_id, await _shut_genres(conn, [member_id], kind),
        list(genre_vocab.EXCLUDED_SOURCES), decade,
    )
    likes = await _likes(conn, rows, ctx=ctx, kind=kind)
    return _with_likes(rows, likes, ctx=ctx, kind=kind, cap=cap, required=member_id == ctx.user_id)


async def _unowned_for_everyone(
    conn: asyncpg.Connection,
    *,
    ctx: Ctx,
    kind: str,
    member_ids: Sequence[int],
    avoids: Sequence[taste.Avoided],
    cap: int,
    decade: int | None,
) -> list[dict[str, Any]]:
    """Unowned titles ranked as the shared sweet spot ranks owned ones (decision 477), over every
    member given, leaving out what any of them has seen, rated, avoids or said Not for me."""
    avoided = await taste.avoided_titles(conn, avoids, kind=kind, version=ctx.version, owned=False)
    rows = await conn.fetch(
        f"""
        WITH rated AS (
            SELECT lv.title_id FROM unnest($1::bigint[]) m(id), LATERAL ({live_label_sql('m.id')}) lv
        ),
        ranked AS (
            SELECT us.user_id, us.title_id,
                   row_number() OVER (PARTITION BY us.user_id ORDER BY us.score, us.title_id)
                       AS pos,
                   count(*) OVER (PARTITION BY us.user_id) AS n
              FROM user_score us
              JOIN title o ON o.id = us.title_id AND NOT o.is_owned
             WHERE us.user_id = ANY($1::bigint[]) AND us.kind = $2 AND us.bundle_version = $3
        )
        SELECT r.title_id, array_agg(r.pos) AS pos, array_agg(r.n) AS n
          FROM ranked r
          JOIN title t ON t.id = r.title_id
          JOIN title_prior tp ON tp.title_id = r.title_id AND tp.bundle_version = $3
         WHERE r.title_id NOT IN (SELECT title_id FROM rated){_feature_sql("$5", "$6", "$7")}
           AND NOT EXISTS (SELECT 1 FROM user_title s WHERE s.title_id = r.title_id
                            AND s.user_id = ANY($1::bigint[]) AND s.state = 'seen')
           AND NOT EXISTS (SELECT 1 FROM wish x WHERE x.title_id = r.title_id
                            AND x.user_id = ANY($1::bigint[]) AND x.state = 'not_for_me')
           AND NOT (r.title_id = ANY($4::int[]))
         GROUP BY r.title_id
        HAVING count(*) = cardinality($1::bigint[])
        """,
        list(member_ids), kind, ctx.bundle_version, sorted(avoided),
        await _shut_genres(conn, member_ids, kind), list(genre_vocab.EXCLUDED_SOURCES), decade,
    )
    top = sorted(
        rows,
        key=lambda r: (
            -fmean(_standardised(p, n) for p, n in zip(r["pos"], r["n"], strict=True)),
            int(r["title_id"]),
        ),
    )[:cap]
    ids = [int(r["title_id"]) for r in top]
    found = await conn.fetch(
        CARD_SELECT + ", w.state AS wish_state" + CARD_FROM + """
          LEFT JOIN wish w ON w.title_id = t.id AND w.user_id = $1
         WHERE t.kind = $2 AND t.id = ANY($4::int[])
        """,
        ctx.user_id, kind, ctx.bundle_version, ids,
    )
    by_id = {int(r["title_id"]): r for r in found}
    cards = [by_id[i] for i in ids]
    likes = await _likes(conn, cards, ctx=ctx, kind=kind)
    return _with_likes(cards, likes, ctx=ctx, kind=kind, cap=cap, required=False)


async def worth_getting(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 7 — "Worth getting": beyond the library, absent until the person has rated enough of
    the kind. A ranking shelf that neither claims nor is thinned (decision 544)."""
    sid = "worth_getting"
    if not ctx.bundle_version:
        return None, Suppressed(sid, kind, "no active artifact bundle — no scores to rank")
    if not ctx.version:
        return None, Suppressed(sid, kind, "no DNA vocabulary imported — no liked title to name")
    opens, labels = await _worth_getting_opens(conn, user_id=ctx.user_id, kind=kind)
    if not opens:
        return None, Suppressed(
            sid, kind,
            f"your own scores rest on {labels} titles of this kind · it opens at "
            f"{WORTH_GETTING_MIN_LABELS} with a ranking of your own",
        )
    items = await _unowned_for_one(
        conn, ctx=ctx, kind=kind, member_id=ctx.user_id, avoids=[ctx.avoid] if ctx.avoid else [],
        cap=SHELF_CAP, keep_wanted=False,
    )
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title="Worth getting",
        why="Not in the library yet, close to what you love",
        why_numbers={"label_count": labels},
        items=items,
    )
    return _finish(section, shelf_id=sid, ctx=ctx)


class NotPickable(ValueError):
    """See all asked for a member whose own ratings do not open Worth getting, or for no member."""


async def _worth_getting_members(
    conn: asyncpg.Connection, *, viewer_id: int, kind: str
) -> list[dict[str, Any]]:
    """Every member, the viewer first, each pickable once their own ratings open the shelf."""
    rows = await conn.fetch(
        "SELECT id, name, role, colour FROM app_user WHERE is_active AND role IN ('admin', 'member') "
        "ORDER BY id <> $1, lower(name), id",
        viewer_id,
    )
    members = []
    for r in rows:
        opens, _labels = await _worth_getting_opens(conn, user_id=int(r["id"]), kind=kind)
        members.append({
            "id": int(r["id"]), "name": r["name"], "role": r["role"], "colour": r["colour"],
            "pickable": opens,
            "reason": None if opens else f"Not enough {KIND_HEADINGS[kind].lower()} rated yet",
        })
    return members


async def worth_getting_list(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    audience: int | str | None,
    bundle_version: str | None,
    decade: int | None = None,
    sort: str = "match",
) -> dict[str, Any]:
    """See all, for one member (the viewer unless `audience` names another) or for "everyone" whose
    own ratings open the shelf (decision 552), in one decade when asked, best match or newest
    first. Naming a member they do not open it for is refused; the viewer's own list is empty while
    their shelf is absent."""
    members = await _worth_getting_members(conn, viewer_id=user_id, kind=kind)
    pickable = [m["id"] for m in members if m["pickable"]]
    member = None
    if audience != "everyone":
        member = next(
            (m for m in members if m["id"] == (user_id if audience is None else audience)), None
        )
        if member is None:
            raise NotPickable("No such member")
        if audience is not None and not member["pickable"]:
            raise NotPickable(member["reason"])
    ctx = Ctx(
        user_id=user_id,
        bundle_version=bundle_version,
        version=await why_mod.vocabulary_version(conn),
        kinds=(kind,),
        tier_sets={kind: await tier_set_of(conn, user_id=user_id, kind=kind)},
        betas={kind: await _beta(conn, user_id=user_id, kind=kind)},
    )
    if member is None:
        audience_ids = pickable
    elif member["pickable"]:
        audience_ids = [member["id"]]
    else:
        audience_ids = []
    items: list[dict[str, Any]] = []
    if audience_ids and ctx.bundle_version and ctx.version:
        avoids = [await taste.avoided_for(conn, user_id=m, version=ctx.version) for m in audience_ids]
        if member:
            items = await _unowned_for_one(
                conn, ctx=ctx, kind=kind, member_id=member["id"], avoids=avoids,
                cap=WORTH_GETTING_LIST_CAP, keep_wanted=True, decade=decade,
            )
        else:
            items = await _unowned_for_everyone(
                conn, ctx=ctx, kind=kind, member_ids=audience_ids, avoids=avoids,
                cap=WORTH_GETTING_LIST_CAP, decade=decade,
            )
        if sort == "newest":
            items.sort(key=lambda c: -(c["year"] or 0))
            for i, card in enumerate(items, 1):
                card["rank"] = i
        await library.carry_original_names(conn, items, key="title_id")
    return {
        "kind": kind,
        "for": {"id": member["id"], "name": member["name"]} if member else "everyone",
        "members": members,
        "items": items,
    }


# --- assembly -----------------------------------------------------------------------------------

FAMILIES = {
    "because_anchor": because_anchor,
    "top_of_ledger": top_of_ledger,
    "taste_term": taste_term,
    "never_watched_term": never_watched_term,
    "shared_sweet_spot": shared_sweet_spot,
    "hidden_gems": hidden_gems,
    "acclaimed": acclaimed,
    "partner_loved": partner_loved,
    "school_night": school_night,
    "watch_again": watch_again,
    "rewatch_together": rewatch_together,
    "rewatch_term": rewatch_term,
    "new_in_library": new_in_library,
    "worth_getting": worth_getting,
}
# Decision 563: the families dealt in the day's order, with their rows; one row unless named.
MIDDLE: tuple[str, ...] = (
    "because_anchor", "taste_term", "never_watched_term", "shared_sweet_spot", "hidden_gems",
    "acclaimed", "partner_loved", "school_night",
)
# The rows of seen titles after Watch again sit lower, apart, so Home leads with unseen ones.
LATE_REWATCH: tuple[tuple[str, int], ...] = (("rewatch_together", 8), ("rewatch_term", 11))
MIDDLE_ROWS = {
    "because_anchor": range(1, BECAUSE_ROWS), "taste_term": range(TASTE_ROWS),
    "partner_loved": range(PARTNER_ROWS),
}
TAIL: tuple[str, ...] = ("new_in_library", "worth_getting")
# Decision 562: the only rows of seen titles.
REWATCH: frozenset[str] = frozenset({"watch_again", "rewatch_together", "rewatch_term"})
# The household rows, which leave out what any member avoids (decision 512).
SHARED: frozenset[str] = frozenset({"shared_sweet_spot", "rewatch_together"})

# `ranking=True` for rows ordered by a ledger score. Every row partitions by kind regardless.
RANKING_SHELVES: frozenset[str] = frozenset(FAMILIES) - REWATCH - {"new_in_library", "acclaimed"}
# Decision 544: Worth getting holds titles no other shelf can, so it neither claims nor is thinned.
CLAIMING_SHELVES: frozenset[str] = frozenset(FAMILIES) - set(TAIL)


def plan(user_id: int, day: str) -> list[tuple[str, int]]:
    """One day's rows as (family, nth), both kinds: Because #1 and top picks lead, Watch again is
    fourth, New and Worth getting close; the rest go in the day's order, one row a family a pass, so
    two rows of one family never touch."""
    left = {
        f: list(MIDDLE_ROWS.get(f, range(1)))
        for f in sorted(MIDDLE, key=lambda f: _daily(user_id, day, f))
    }
    middle: list[tuple[str, int]] = []
    while left:
        dealt = list(left)
        if middle and len(dealt) > 1 and dealt[0] == middle[-1][0]:
            dealt[0], dealt[1] = dealt[1], dealt[0]
        for family in dealt:
            middle.append((family, left[family].pop(0)))
            if not left[family]:
                del left[family]
    steps = [("because_anchor", 0), ("top_of_ledger", 0), *middle]
    steps.insert(3, ("watch_again", 0))
    late = sorted((f for f, _ in LATE_REWATCH), key=lambda f: _daily(user_id, day, f))
    for family, (_, at) in zip(late, LATE_REWATCH, strict=True):
        steps.insert(min(at, len(steps)), (family, 0))
    return steps + [(family, 0) for family in TAIL]


async def live_verdict_count(conn: asyncpg.Connection, *, user_id: int) -> int:
    """How many titles carry a live verdict (§4.2), since the member's cut-over."""
    return int(await conn.fetchval(f"SELECT count(*) FROM ({LIVE_LABEL_SQL}) l", user_id) or 0)


async def _context(
    conn: asyncpg.Connection, *, user: Any, kinds: Sequence[str], bundle_version: str | None, day: str
) -> tuple[Ctx, bool]:
    """What both reads of Home resolve once, and whether the member has no verdicts yet."""
    chosen = library.normalise_kinds(kinds)
    version = await why_mod.vocabulary_version(conn)
    others = await others_for(conn, user_id=user.id)
    avoid = await taste.avoided_for(conn, user_id=user.id, version=version)
    theirs = {o["user_id"]: await taste.avoided_for(conn, user_id=o["user_id"], version=version)
              for o in others}
    memo: dict[Any, Any] = {"others": others}
    for kind in chosen:
        # Decision 565: a member with nothing to read for the kind is left out, not a blocker.
        present = {int(r["id"]) for r in await conn.fetch(
            """
            SELECT m.id FROM unnest($1::int[]) m(id)
             WHERE EXISTS (SELECT 1 FROM user_score us WHERE us.user_id = m.id AND us.kind = $2
                             AND us.bundle_version = $3)
                OR EXISTS (SELECT 1 FROM ledger_state ls WHERE ls.user_id = m.id AND ls.kind = $2)
            """,
            list(theirs), kind, bundle_version,
        )}
        household = [o for o in others if o["user_id"] in present]
        memo["household", kind] = household
        memo["mine", kind] = await taste.avoided_titles(conn, [avoid], kind=kind, version=version)
        memo["shared", kind] = (
            await taste.avoided_titles(
                conn, [avoid, *(theirs[o["user_id"]] for o in household)], kind=kind, version=version
            )
            if household else memo["mine", kind]
        )
    ctx = Ctx(
        user_id=user.id,
        bundle_version=bundle_version,
        version=version,
        kinds=tuple(chosen),
        tier_sets={k: await tier_set_of(conn, user_id=user.id, kind=k) for k in chosen},
        betas={k: await _beta(conn, user_id=user.id, kind=k) for k in chosen},
        day=day,
        avoid=avoid,
        memo=memo,
    )
    verdicts = await live_verdict_count(conn, user_id=user.id)
    return ctx, verdicts == 0 and bundle_version is not None


async def build_shelves(
    conn: asyncpg.Connection,
    *,
    ctx: Ctx,
    steps: Sequence[tuple[str, int]],
    shown: Sequence[int],
    zero_verdicts: bool,
    room: int,
) -> tuple[list[Shelf], list[Suppressed]]:
    """`steps` as shelves of one section per kind, built top picks first and then in order, with
    `shown` already claimed; at `room` rows a kind only the tail is still tried.

    With zero verdicts every score-ordered shelf is suppressed (proposal 20).
    """
    built: dict[tuple[tuple[str, int], str], Section] = {}
    notes: dict[tuple[tuple[str, int], str], Suppressed] = {}
    claimed: dict[str, set[int]] = {kind: set(shown) for kind in ctx.kinds}
    rows = dict.fromkeys(ctx.kinds, 0)

    # Decision 475: built in claim order, rendered in plan order, so no title shows twice.
    for step in sorted(steps, key=lambda s: s[0] != "top_of_ledger"):
        family, nth = step
        for kind in ctx.kinds:
            if family not in TAIL and rows[kind] >= room:
                notes[step, kind] = Suppressed(family, kind, f"Home holds {ROW_CAP} rows of a kind")
                continue
            if zero_verdicts and family in RANKING_SHELVES:
                notes[step, kind] = Suppressed(
                    family, kind, "no verdicts yet — a score-ordered shelf would rank on a "
                                  "ledger this profile does not have"
                )
                continue
            scoped = replace(ctx, nth=nth)
            if family in CLAIMING_SHELVES:
                scoped = replace(
                    scoped,
                    claimed=frozenset(claimed[kind]),
                    avoided=ctx.memo["shared" if family in SHARED else "mine", kind],
                )
            section, note = await FAMILIES[family](conn, ctx=scoped, kind=kind)
            if section is not None:
                built[step, kind] = section
                rows[kind] += 1
                if family in CLAIMING_SHELVES:
                    claimed[kind].update(int(c["title_id"]) for c in section.items)
            elif note is not None:
                notes[step, kind] = note

    shelves: list[Shelf] = []
    dropped: list[Suppressed] = []
    for step in steps:
        family, nth = step
        shelf = Shelf(family, ranking=family in RANKING_SHELVES, key=f"{family}:{nth}",
                      seen_only=family in REWATCH)
        for kind in ctx.kinds:
            if (step, kind) in built:
                shelf.sections.append(built[step, kind])
            elif (step, kind) in notes:
                dropped.append(notes[step, kind])
        # §6.0: a shelf that cannot justify itself is ABSENT, never present and empty.
        if shelf.sections:
            shelves.append(shelf)
    # Decision 516: original title and language, one read for every card.
    await library.carry_original_names(
        conn, [card for shelf in shelves for s in shelf.sections for card in s.items], key="title_id"
    )
    return shelves, dropped


async def build_home(
    conn: asyncpg.Connection, *, user: Any, kinds: Sequence[str], bundle_version: str | None
) -> dict[str, Any]:
    """Home's first read, ungated: the notices and the plan's first HEAD_SLOTS rows; `more` names
    the day `build_rows` reads the rest of. `rail.redact` applies decision 117 afterwards."""
    day = notices.today().isoformat()
    ctx, zero_verdicts = await _context(
        conn, user=user, kinds=kinds, bundle_version=bundle_version, day=day
    )
    steps = plan(user.id, day)
    shelves, dropped = await build_shelves(
        conn, ctx=ctx, steps=steps[:HEAD_SLOTS], shown=(), zero_verdicts=zero_verdicts,
        room=HEAD_SLOTS,
    )
    payload = {
        "kinds": list(ctx.kinds),
        "banner": await pending_verdicts(conn, user_id=user.id),
        "setup_notice": await setup_notice(conn, user_id=user.id) if bundle_version else None,
        # OWNED titles per kind: what the shelves draw on.
        "library": await library.count_by_kind(conn, owned="only"),
        "shelves": [s.as_dict() for s in shelves],
        "shelves_total": len(shelves),
        "more": {"day": day} if len(steps) > HEAD_SLOTS else None,
        "degraded": _degraded(bundle_version),
        "suppressed": [s.as_dict() for s in dropped],
        # Decision 512's avoid set, ungated: facts about their own ratings.
        "avoiding": ctx.avoid.as_dict() if ctx.avoid else None,
        "arrived": await wish.arrived(conn, user_id=user.id),
        "wish": await wish.summary(conn),
    }
    return await notices.apply_hidden(conn, user_id=user.id, payload=payload)


async def build_rows(
    conn: asyncpg.Connection,
    *,
    user: Any,
    kinds: Sequence[str],
    bundle_version: str | None,
    day: str,
    shown: Sequence[int],
    named: Sequence[str] = (),
) -> dict[str, Any]:
    """Home's second read: the rest of `day`'s plan, with what the first read `shown` claimed and the
    `named` terms ("kind:term") its term rows named."""
    ctx, zero_verdicts = await _context(
        conn, user=user, kinds=kinds, bundle_version=bundle_version, day=day
    )
    for kind, _, term in (n.partition(":") for n in named):
        ctx.memo.setdefault(("named", kind), set()).add(term)
    shelves, dropped = await build_shelves(
        conn, ctx=ctx, steps=plan(user.id, day)[HEAD_SLOTS:], shown=shown,
        zero_verdicts=zero_verdicts, room=ROW_CAP - len(TAIL) - HEAD_SLOTS,
    )
    return {"shelves": [s.as_dict() for s in shelves], "suppressed": [s.as_dict() for s in dropped]}


async def _beta(conn: asyncpg.Connection, *, user_id: int, kind: str) -> float:
    fit = await serve.fit_row(conn, user_id=user_id, kind=kind)
    return float(fit["blend_beta"]) if fit and fit["blend_beta"] is not None else 0.0


def _degraded(bundle_version: str | None) -> dict[str, Any] | None:
    """Proposal 20's first-week state: copy and route only, never a second code path."""
    if bundle_version is None:
        return {
            "state": "no_bundle",
            "headline": "No movie data yet.",
            "why": "Your shelves appear here once an admin imports it",
            "cta": {"label": "Open Movie data", "route": "/admin/movie-data"},
        }
    return None


async def setup_notice(conn: asyncpg.Connection, *, user_id: int) -> dict[str, Any] | None:
    """Home's card before the member's set-up (decision 550); the shelves stay under it."""
    state = await ladder.state(conn, user_id=user_id)
    if state.done:
        return None
    k = len(await tier_set_of(conn, user_id=user_id, kind="movie"))
    why = (
        f"Rating is one tap now, on {'six' if k == 6 else k} steps of your own. "
        "The set-up takes about a minute"
    )
    if state.earlier_ratings:
        why += " — until then your shelves keep using your earlier ratings"
    return {
        "headline": "Set up your ladder.",
        "why": why,
        "cta": {"label": "Set up my ladder", "route": "/rate/setup"},
    }
