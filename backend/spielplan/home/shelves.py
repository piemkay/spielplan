"""§6.0's Home: the pending-verdicts banner and the six shelves.

A shelf has no items, only one `section` per kind, so an interleaved ranking is unrepresentable
(§4.1 rule 5, decision 18). A shelf that names terms selects its cards BY them (`why.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from statistics import NormalDist
from typing import Any

import asyncpg

from spielplan.db import library
from spielplan.home import taste
from spielplan.home import why as why_mod
from spielplan.home.why import WhyTerm
from spielplan.ledger.observations import LIVE_LABEL_SQL, rescale_level
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

# §4.2's default tier set, used when `ledger_cutpoints` has no row for this (user, kind) yet.
DEFAULT_TIER_SET: tuple[str, ...] = ("F", "D", "C", "B", "A", "A+", "S")

# The standard normal the sweet spot reads each member's rank through (decision 477's scale).
_NORMAL = NormalDist()

# §6.0's shelf order, verbatim from the (normative) table.
SHELF_IDS: tuple[str, ...] = (
    "because_anchor",
    "top_of_ledger",
    "never_watched_term",
    "shared_sweet_spot",
    "school_night",
    "new_in_library",
)


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
    shared_terms: list[WhyTerm] = field(default_factory=list)
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
            "shared_terms": [t.as_dict() for t in self.shared_terms],
            "caption": self.caption,
            "anchor": self.anchor,
            "items": self.items,
        }


@dataclass
class Shelf:
    """§6.0's shelf. Note what is missing: there is no `items`, and there never will be."""

    id: str
    ranking: bool
    sections: list[Section] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ranking": self.ranking,
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

    @property
    def excluded(self) -> frozenset[int]:
        return self.claimed | self.avoided


# --- the pending-verdicts banner ------------------------------------------------------------


def _name_list(names: Sequence[str], total: int) -> str:
    """Proposal 21's copy, exactly: one, two, three, then two-and-N-more."""
    if total <= 1:
        return names[0]
    if total == 2:
        return f"{names[0]} and {names[1]}"
    if total == 3:
        return f"{names[0]}, {names[1]} and {names[2]}"
    return f"{names[0]}, {names[1]} and {total - 2} more"


async def pending_verdicts(
    conn: asyncpg.Connection, *, user_id: int, cap: int = NAMED_TITLES_CAP
) -> dict[str, Any] | None:
    """§6.0's banner: seen titles with no LIVE verdict, in the live Rate session's kinds. Or None.

    Filtered by the session's kinds so the CTA can serve what the copy names (proposal 150).
    """
    # One live session per person, so one row or none.
    live = await conn.fetchval(
        "SELECT kinds FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user_id
    )
    kinds = list(live or KIND_HEADINGS)
    rows = await conn.fetch(
        """
        SELECT t.id, t.name, t.kind, ut.state_changed_at
          FROM user_title ut
          JOIN title t ON t.id = ut.title_id
         WHERE ut.user_id = $1 AND ut.state = 'seen' AND t.kind = ANY($2::text[])
           AND NOT EXISTS (
                SELECT 1 FROM verdict v
                 WHERE v.user_id = $1 AND v.title_id = t.id AND v.superseded_by IS NULL)
         ORDER BY ut.state_changed_at DESC, t.id DESC
        """,
        user_id,
        kinds,
    )
    if not rows:
        return None

    total = len(rows)
    # The queue head is exactly the titles the copy names: two beyond three (proposal 21).
    named_n = total if total <= cap else cap - 1
    named = [dict(r) for r in rows[:named_n]]
    text = _name_list([r["name"] for r in named], total)
    head = [int(r["id"]) for r in named]
    # Repeated `head=`, not comma-joined: `GET /api/rate` takes `head: list[int]`.
    query = "&".join(f"head={i}" for i in head)
    return {
        "count": total,
        "named": [{"title_id": int(r["id"]), "name": r["name"], "kind": r["kind"]} for r in named],
        "head_title_ids": head,
        "copy": {
            # Proposal 21, verbatim, on both viewports.
            "wide": f"You watched {text} — a quick verdict keeps your profile sharp.",
            "compact": f"Watched, not rated: {text}",
        },
        "cta": {
            "label_wide": "Rate now",
            "label_compact": "Rate",
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


async def tier_set_of(conn: asyncpg.Connection, *, user_id: int, kind: str) -> tuple[str, ...]:
    """§4.2 / decision 11: the tier set is per user and per kind."""
    row = await conn.fetchval(
        "SELECT tier_set FROM ledger_cutpoints WHERE user_id = $1 AND kind = $2", user_id, kind
    )
    return tuple(row) if row else DEFAULT_TIER_SET


async def beta_of(conn: asyncpg.Connection, *, user_id: int, kind: str) -> tuple[float, bool]:
    """(β, fitted?): the weight the scores were ACTUALLY computed with; 0.0 when never fitted."""
    fit = await serve.fit_row(conn, user_id=user_id, kind=kind)
    if fit and fit["blend_beta"] is not None:
        return float(fit["blend_beta"]), True
    return 0.0, False


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
    """One shelf card. Rank, seen dot and tier letter are ungated chrome; numbers live in `model`.

    The letter is the FITTED tier (decision 187), not Rank's; `_finish` adds `board_tier` beside it.
    """
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


async def _finish(
    conn: asyncpg.Connection, section: Section, *, shelf_id: str, ctx: Ctx
) -> tuple[Section | None, Suppressed | None]:
    """The gate every section passes: the floor, a why-line, and every named term on every card."""
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
    if ctx.version:
        # Intersected over the cards actually returned, so it cannot be false (§6.8).
        section.shared_terms = await why_mod.common_terms(
            conn, title_ids=[c["title_id"] for c in section.items], version=ctx.version
        )
    board = await _board_letters(
        conn,
        user_id=ctx.user_id,
        title_ids=[int(c["title_id"]) for c in section.items],
        tier_set=await tier_set_of(conn, user_id=ctx.user_id, kind=section.kind),
    )
    for card in section.items:
        card["on_board"] = int(card["title_id"]) in board
        card["board_tier"] = board.get(int(card["title_id"]))
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


async def _board_letters(
    conn: asyncpg.Connection, *, user_id: int, title_ids: Sequence[int], tier_set: Sequence[str]
) -> dict[int, str | None]:
    """The letter §6.3's board renders for each of these titles that is on it (`ls.observed`)."""
    if not title_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT ls.title_id, ls.tier, te.tier AS assigned, te.n_levels AS assigned_k
          FROM ledger_state ls
          LEFT JOIN (
              SELECT DISTINCT ON (title_id) title_id, tier, n_levels
                FROM tier_edit
               WHERE user_id = $1 AND title_id = ANY($2)
               ORDER BY title_id, created_at DESC, id DESC
          ) te ON te.title_id = ls.title_id
         WHERE ls.user_id = $1 AND ls.title_id = ANY($2) AND ls.observed
        """,
        user_id,
        list(title_ids),
    )
    letters: dict[int, str | None] = {}
    for r in rows:
        if r["assigned"] is not None:
            level: int | None = rescale_level(
                int(r["assigned"]), k_from=r["assigned_k"], k_to=len(tier_set)
            )
        else:
            level = None if r["tier"] is None else int(r["tier"])
        in_set = level is not None and 0 <= level < len(tier_set)
        letters[int(r["title_id"])] = tier_set[level] if in_set else None
    return letters


# --- shelf 1: because_anchor ------------------------------------------------------------------


async def because_anchor(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 1 — "Because you put *{anchor}* in {tier}" / "shares {term} + {term} with it".

    The anchor's likest unseen owned titles (decision 513), and a term pair every card carries.
    The headline says what the person did (decision 476): "put" needs a `tier_edit`.
    """
    sid = "because_anchor"
    if not ctx.version:
        return None, Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")

    # Both seen AND rated: neither implies the other. The anchor is the highest tier the board
    # shows (ordered after `rescale_level`, so in Python), then the live verdict, then `s`.
    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    candidates = await conn.fetch(
        f"""
        SELECT t.id, t.name, ls.tier AS model_tier, ls.s,
               te.tier AS assigned, te.n_levels AS assigned_k, lv.value AS verdict
          FROM ledger_state ls
          JOIN title t ON t.id = ls.title_id
          JOIN user_title ut ON ut.user_id = ls.user_id AND ut.title_id = t.id AND ut.state = 'seen'
          LEFT JOIN (
              SELECT DISTINCT ON (title_id) title_id, tier, n_levels
                FROM tier_edit
               WHERE user_id = $1
               ORDER BY title_id, created_at DESC, id DESC
          ) te ON te.title_id = ls.title_id
          LEFT JOIN ({LIVE_LABEL_SQL}) lv ON lv.title_id = ls.title_id
         WHERE ls.user_id = $1 AND t.kind = $2 AND ls.tier IS NOT NULL AND ls.observed
        """,
        ctx.user_id,
        kind,
    )
    if not candidates:
        return None, Suppressed(
            sid, kind,
            "no title of this kind is both seen and rated with a fitted tier yet",
        )

    def shown(row: asyncpg.Record) -> int:
        # The tier Rank renders: the latest drop, rescaled to today's set (decision 11).
        if row["assigned"] is None:
            return int(row["model_tier"])
        return rescale_level(int(row["assigned"]), k_from=row["assigned_k"], k_to=len(tier_set))

    anchor = min(
        candidates,
        key=lambda r: (
            -shown(r),
            -(r["verdict"] if r["verdict"] is not None else -1),
            -float(r["s"]),
            int(r["id"]),
        ),
    )
    # Checked, not clamped: a model tier outside the set is a bug, not a state.
    model_index = int(anchor["model_tier"])
    if not 0 <= model_index < len(tier_set):
        return None, Suppressed(
            sid, kind, f"anchor tier index {model_index} is outside the tier set"
        )
    index = shown(anchor)

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
    scored = await conn.fetch(
        CARD_SELECT + CARD_FROM + " WHERE t.kind = $2 AND t.id = ANY($4)",
        ctx.user_id, kind, ctx.bundle_version, members,
    )
    by_id = {int(r["title_id"]): r for r in scored}
    rows = [by_id[i] for i in members]

    beta, _fitted = await beta_of(conn, user_id=ctx.user_id, kind=kind)
    tier = tier_set[index]
    if anchor["assigned"] is not None:
        title = f"Because you put {anchor['name']} in {tier}"
    elif anchor["verdict"] == 2:
        title = f"Because you liked {anchor['name']}"
    else:
        title = f"More like {anchor['name']}"
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=title,
        why=f"shares {why_mod.phrase([t1, t2])} with it",
        why_terms=[t1.with_role("member"), t2.with_role("member")],
        anchor={"title_id": int(anchor["id"]), "name": anchor["name"], "tier": tier},
        items=[_card(row, i + 1, tier_set=tier_set, beta=beta) for i, row in enumerate(rows)],
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 2: top_of_ledger -------------------------------------------------------------------


async def top_of_ledger(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 2 — "Top of your ledger", from `scoring.serve.top_scored`, the one ranked statement.

    The one shelf that includes seen titles, and says so (proposal 25).
    """
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
    )
    # The β the ordering used: stored `blend_beta`, or 0.0 when never fitted.
    beta = float(ranked["beta"])
    personalised = bool(ranked["personalised"])
    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    items = [
        _card(row, i + 1, tier_set=tier_set, beta=beta)
        for i, row in enumerate(_as_card_rows(ranked["items"]))
    ]
    why = (
        # Member register (decision 476); β travels in the gated `why_numbers`.
        "the ones we think you'll enjoy most — rewatches included"
        if personalised
        else "what most people rate highest, until your own ratings take over — rewatches included"
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
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


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
    beta, _fitted = await beta_of(conn, user_id=ctx.user_id, kind=kind)
    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    rows = await conn.fetch(
        CARD_SELECT + CARD_FROM + """
         WHERE t.kind = $2 AND t.id = ANY($4)
         ORDER BY us.score DESC NULLS LAST, t.year DESC NULLS LAST, t.id
         LIMIT $5
        """,
        ctx.user_id, kind, ctx.bundle_version, ids, SHELF_CAP,
    )
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=f"You've never watched anything {candidate.name}",
        why=f"close to {neighbour.name}, which you like",
        why_terms=[candidate, neighbour],
        why_numbers={"cos": round(cos, 4), "affinity": round(aff, 4),
                     "min_seen": FRONTIER_MIN_SEEN},
        # §6.4's "honestly labelled" exploratory slot, in words.
        caption="a step outside what you usually watch, on purpose",
        items=[_card(row, i + 1, tier_set=tier_set, beta=beta) for i, row in enumerate(rows)],
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 4: shared_sweet_spot ---------------------------------------------------------------


async def partner_for(conn: asyncpg.Connection, *, user_id: int) -> dict[str, Any] | None:
    """Proposal 26: the active member with the most co-seen titles, then the most recent one.

    LEFT JOIN, so zero co-seen titles still names a partner.
    """
    row = await conn.fetchrow(
        """
        SELECT u.id, u.name,
               count(b.title_id) AS co_seen,
               max(b.state_changed_at) AS last_co_seen
          FROM app_user u
          LEFT JOIN user_title b ON b.user_id = u.id AND b.state = 'seen'
                                AND EXISTS (SELECT 1 FROM user_title a
                                             WHERE a.user_id = $1 AND a.title_id = b.title_id
                                               AND a.state = 'seen')
         WHERE u.id <> $1 AND u.is_active AND u.role IN ('admin', 'member')
         GROUP BY u.id, u.name
         ORDER BY count(b.title_id) DESC, max(b.state_changed_at) DESC NULLS LAST, u.id
         LIMIT 1
        """,
        user_id,
    )
    if row is None:
        return None
    return {"user_id": int(row["id"]), "name": row["name"], "co_seen": int(row["co_seen"])}


async def shared_sweet_spot(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str, partner: dict[str, Any] | None
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 4 — "You and {other} would both enjoy these": unseen by both, above both CDF floors.

    Ordered by the plain average of rank-standardised scores over the owned library, as Tonight's
    pool is (decision 477). Either member's avoid set is excluded (decision 512).
    """
    sid = "shared_sweet_spot"
    if partner is None:
        return None, Suppressed(sid, kind, "no other member to share a sweet spot with")
    if not ctx.bundle_version:
        return None, Suppressed(sid, kind, "no active artifact bundle — no scores to intersect")

    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    beta, _fitted = await beta_of(conn, user_id=ctx.user_id, kind=kind)
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
             WHERE us.user_id = ANY($4) AND us.kind = $2 AND us.bundle_version = $3
        )
        SELECT t.id AS title_id, t.kind, t.name, t.year, t.runtime_min, t.poster_path,
               t.placement, NULL::timestamptz AS placement_at,
               false AS seen,
               a.score, NULL::real AS cf, tp.b, tp.gate, tp.item_n, tp.e_source,
               ls.s, ls.sigma, ls.cdf, ls.tier,
               a.cdf AS mine_cdf, b.cdf AS theirs_cdf,
               a.pos AS mine_pos, a.n AS mine_n, b.pos AS theirs_pos, b.n AS theirs_n
          FROM ranked a
          JOIN ranked b ON b.title_id = a.title_id AND b.user_id = $5
          JOIN title t ON t.id = a.title_id
          LEFT JOIN title_prior  tp ON tp.title_id = t.id AND tp.bundle_version = $3
          LEFT JOIN ledger_state ls ON ls.title_id = t.id AND ls.user_id = $1
         WHERE a.user_id = $1 AND t.is_owned AND a.cdf >= $6 AND b.cdf >= $6
           AND NOT EXISTS (SELECT 1 FROM user_title s WHERE s.title_id = t.id
                            AND s.user_id = $1 AND s.state = 'seen')
           AND NOT EXISTS (SELECT 1 FROM user_title s WHERE s.title_id = t.id
                            AND s.user_id = $5 AND s.state = 'seen')
           AND NOT (t.id = ANY($7))
        """,
        ctx.user_id, kind, ctx.bundle_version,
        [ctx.user_id, partner["user_id"]], partner["user_id"], SWEET_SPOT_MIN_CDF,
        sorted(ctx.excluded),
    )

    def standardised(pos: int, n: int) -> float:
        return _NORMAL.inv_cdf((pos - 0.5) / n)

    paired = sorted(
        (
            (
                (standardised(r["mine_pos"], r["mine_n"])
                 + standardised(r["theirs_pos"], r["theirs_n"])) / 2.0,
                r,
            )
            for r in rows
        ),
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
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title=f"You and {partner['name']} would both enjoy these",
        why="neither of you has seen them — a good pick for a night in together",
        why_numbers={"min_cdf": SWEET_SPOT_MIN_CDF, "partner_user_id": partner["user_id"],
                     "co_seen": partner["co_seen"]},
        caption=None,
        items=items,
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 5: school_night --------------------------------------------------------------------


async def school_night(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 5 — "Under 110 minutes". A NULL runtime is excluded; the bound is strict."""
    sid = "school_night"
    limit_min = SCHOOL_NIGHT_MAX_MIN[kind]
    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    beta, _fitted = await beta_of(conn, user_id=ctx.user_id, kind=kind)
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
        why="for a school night",
        why_numbers={"max_minutes": limit_min},
        caption=(
            "series runtime is minutes per episode" if kind == "series" else None
        ),
        items=[_card(row, i + 1, tier_set=tier_set, beta=beta) for i, row in enumerate(rows)],
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 6: new_in_library ------------------------------------------------------------------


async def new_in_library(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 6 — "New in the library": by recency, so it ships with zero verdicts.

    Requires no crowd rating at all (`item_n` 0): cold-masked rows are crowd-rated. Not claimed.
    """
    sid = "new_in_library"
    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    beta, _fitted = await beta_of(conn, user_id=ctx.user_id, kind=kind)
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
    section = Section(
        kind=kind,
        heading=KIND_HEADINGS[kind],
        title="New in the library",
        why="no outside ratings yet, so we placed them by what they're about",
        why_numbers={"gate_k": EVIDENCE_K},
        items=[_card(row, i + 1, tier_set=tier_set, beta=beta) for i, row in enumerate(rows)],
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- assembly -----------------------------------------------------------------------------------

# `ranking=True` for the five shelves ordered by a ledger score; `new_in_library` is ordered by
# recency. All six partition by kind regardless — a Home row reads as a recommendation.
RANKING_SHELVES: frozenset[str] = frozenset(SHELF_IDS) - {"new_in_library"}

# Decision 475's claim order: "Your top picks" first. Display order stays `SHELF_IDS`.
CLAIM_ORDER: tuple[str, ...] = ("top_of_ledger",) + tuple(
    s for s in SHELF_IDS if s != "top_of_ledger"
)
CLAIMING_SHELVES: frozenset[str] = RANKING_SHELVES


async def live_verdict_count(conn: asyncpg.Connection, *, user_id: int) -> int:
    """How many LIVE verdicts (§4.2), as the banner counts them."""
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM verdict WHERE user_id = $1 AND superseded_by IS NULL", user_id
        )
        or 0
    )


async def build_shelves(
    conn: asyncpg.Connection,
    *,
    ctx: Ctx,
    zero_verdicts: bool,
    avoided: taste.Avoided | None,
) -> tuple[list[Shelf], list[Suppressed]]:
    """§6.0's six shelves, in the table's order, each as one section per selected kind.

    With zero verdicts every score-ordered shelf is suppressed (proposal 20).
    """
    partner = await partner_for(conn, user_id=ctx.user_id)
    theirs = (
        await taste.avoided_for(conn, user_id=partner["user_id"], version=ctx.version)
        if partner is not None else None
    )
    mine_out: dict[str, frozenset[int]] = {}
    both_out: dict[str, frozenset[int]] = {}
    for kind in ctx.kinds:
        mine_out[kind] = await taste.avoided_titles(conn, [avoided], kind=kind, version=ctx.version)
        both_out[kind] = (
            await taste.avoided_titles(conn, [avoided, theirs], kind=kind, version=ctx.version)
            if theirs is not None else mine_out[kind]
        )
    built: dict[tuple[str, str], Section] = {}
    notes: dict[tuple[str, str], Suppressed] = {}
    claimed: dict[str, set[int]] = {kind: set() for kind in ctx.kinds}

    # Decision 475: built in claim order, rendered in the table's, so no title shows twice.
    for shelf_id in CLAIM_ORDER:
        ranking = shelf_id in RANKING_SHELVES
        for kind in ctx.kinds:
            if zero_verdicts and ranking:
                notes[shelf_id, kind] = Suppressed(
                    shelf_id, kind, "no verdicts yet — a score-ordered shelf would rank on a "
                                    "ledger this profile does not have"
                )
                continue
            scoped = (
                replace(
                    ctx,
                    claimed=frozenset(claimed[kind]),
                    avoided=both_out[kind] if shelf_id == "shared_sweet_spot" else mine_out[kind],
                )
                if shelf_id in CLAIMING_SHELVES else ctx
            )
            if shelf_id == "because_anchor":
                section, note = await because_anchor(conn, ctx=scoped, kind=kind)
            elif shelf_id == "top_of_ledger":
                section, note = await top_of_ledger(conn, ctx=scoped, kind=kind)
            elif shelf_id == "never_watched_term":
                section, note = await never_watched_term(conn, ctx=scoped, kind=kind)
            elif shelf_id == "shared_sweet_spot":
                section, note = await shared_sweet_spot(
                    conn, ctx=scoped, kind=kind, partner=partner
                )
            elif shelf_id == "school_night":
                section, note = await school_night(conn, ctx=scoped, kind=kind)
            else:
                section, note = await new_in_library(conn, ctx=scoped, kind=kind)
            if section is not None:
                built[shelf_id, kind] = section
                if shelf_id in CLAIMING_SHELVES:
                    claimed[kind].update(int(c["title_id"]) for c in section.items)
            elif note is not None:
                notes[shelf_id, kind] = note

    shelves: list[Shelf] = []
    dropped: list[Suppressed] = []
    for shelf_id in SHELF_IDS:
        shelf = Shelf(shelf_id, ranking=shelf_id in RANKING_SHELVES)
        for kind in ctx.kinds:
            if (shelf_id, kind) in built:
                shelf.sections.append(built[shelf_id, kind])
            elif (shelf_id, kind) in notes:
                dropped.append(notes[shelf_id, kind])
        # §6.0: a shelf that cannot justify itself is ABSENT, never present and empty.
        if shelf.sections:
            shelves.append(shelf)
    return shelves, dropped


async def build_home(
    conn: asyncpg.Connection, *, user: Any, kinds: Sequence[str], bundle_version: str | None
) -> dict[str, Any]:
    """The whole §6.0 Home payload, ungated. `rail.redact` applies decision 117 afterwards."""
    chosen = library.normalise_kinds(kinds)
    ctx = Ctx(
        user_id=user.id,
        bundle_version=bundle_version,
        version=await why_mod.vocabulary_version(conn),
        kinds=tuple(chosen),
    )
    verdicts = await live_verdict_count(conn, user_id=user.id)
    avoided = await taste.avoided_for(conn, user_id=user.id, version=ctx.version)
    shelves, dropped = await build_shelves(
        conn,
        ctx=ctx,
        zero_verdicts=(verdicts == 0 and bundle_version is not None),
        avoided=avoided,
    )
    # Decision 516: original title and language, one read for every card.
    await library.carry_original_names(
        conn, [card for shelf in shelves for s in shelf.sections for card in s.items], key="title_id"
    )
    return {
        "kinds": chosen,
        "banner": await pending_verdicts(conn, user_id=user.id),
        # OWNED titles per kind: what the shelves draw on.
        "library": await library.count_by_kind(conn, owned_only=True),
        "shelves": [s.as_dict() for s in shelves],
        "shelves_total": len(shelves),
        "degraded": _degraded(bundle_version, verdicts),
        "suppressed": [s.as_dict() for s in dropped],
        # Decision 512's avoid set, ungated: facts about their own ratings.
        "avoiding": avoided.as_dict() if avoided else None,
    }


def _degraded(bundle_version: str | None, verdicts: int) -> dict[str, Any] | None:
    """Proposal 20's two first-week states: copy and route only, never a second code path."""
    if bundle_version is None:
        return {
            "state": "no_bundle",
            "headline": "No artifact bundle imported.",
            "why": "the catalog and the shelves come from a data bundle an admin imports",
            "cta": {"label": "Import a bundle", "route": "/admin/data"},
        }
    if verdicts == 0:
        return {
            "state": "zero_verdicts",
            "headline": "Rate a few titles to get your shelves.",
            "why": "your suggestions get about three times more personal between 5 and 100 "
                   "ratings — aim for 50–100 in your first sitting or two",
            "cta": {"label": "Rate 50 titles", "route": "/rate"},  # decision 203
        }
    return None
