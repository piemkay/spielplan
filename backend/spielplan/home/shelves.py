"""§6.0's M2 Home: the greeting, the pending-verdicts banner, and the six shelves.

Spec v2.1 §6.0 (M2 Home & Library), §4.1 rule 5, §5.1, §5.2, §6.4, §6.5, §6.8, §7.3;
decisions 18 and 117; proposals 20–33 and 150.

§6.0: "the default surface becomes personalized shelves over the catalog. A greeting; a
**pending-verdicts banner** … then shelves, each with a **mandatory one-line why in vocabulary
terms** — a shelf that cannot say why it exists doesn't ship".

TWO STRUCTURAL COMMITMENTS, and everything else follows from them.

**A shelf has no items.** It has `sections`, exactly one per selected kind, each with its own
title, why-line, ordering and cap. §4.1 rule 5 is measured ("the unpartitioned crowd top-10 is
8/10 TV series") and decision 18 gives it its precise reading: "a surface that **ranks** — Rank,
Tonight, the Home shelves — renders two headed sections and never one interleaved ranking,
because the measured failure is a *shared ranking* … not a shared screen. A surface that merely
**lists** in a kind-independent order — the catalog, sorted by year or title — may interleave
freely." So an interleaved ranking is not discouraged here, it is unrepresentable: there is no
shelf-level list for a client to render, and every ordered statement below binds `kind` as a
scalar. §5.2 supplies the arithmetic reason as well — the displayed 0..1 weight is an empirical
CDF computed *per kind*, so ordering across kinds compares numbers on two different scales.

**The terms come first, the membership second.** See `why.py`. A shelf that names DNA terms
selects its cards BY those terms, so §6.0's why-line is true of every card by construction.

TUNING NUMBERS. `ledger/hyperparams.py` owns every number the §5.2 recipe fits with, and none
of the constants below is one of those: 110/45 minutes are proposal 27's stated constants
("the thresholds … are constants, not copy"), 12 and 3 are proposal 28's shelf cap and floor,
3 is proposal 21's naming cap, and the two this design pins itself (`FRONTIER_MIN_SEEN`,
`SWEET_SPOT_MIN_CDF`) are printed in the payload where they apply so changing one is a constant
edit plus a copy change rather than a redesign.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import NormalDist
from time import perf_counter
from typing import Any

import asyncpg

from spielplan.db import library
from spielplan.home import taste
from spielplan.home import why as why_mod
from spielplan.home.why import WhyTerm

# The two imports this package takes from `ledger/`, and the reason the two constants above
# (`DEFAULT_TIER_SET`, `tier_set_of`) are still spelled locally while these are not: those restate
# one line of §4.2, and these are a MEASURED mapping and a TUNED constant. Shelf 1's headline and
# §6.3's board have to name the same tier for the same drop — that identity is the whole of §6.0
# row 1's verb — so they read the stored level through the same function or they disagree the moment
# K changes. [M4.13, dd06]
#
# `DEFAULTS` for the same reason one level up: §6.0's why-numbers print `gate_k`, which IS §5.1's
# evidence k, and §5.2 puts every tuned number in `ledger_hyperparams.json`. It was a literal 10
# twice in this file, beside a gate the cards report from `title_prior`. [M4.13 step 34d]
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.observations import LIVE_LABEL_SQL, rescale_level

# The type the one caller passes, and the bundle's own reading of the cold path. See
# `fit_yardstick` below: this module prints the fold-in's rho, so this module is where the
# reference value it is read against has to arrive. [M4.13 step 35]
from spielplan.models.artifacts import ColdEval
from spielplan.scoring import serve

# §6.0 / proposal 32: "The partition control is labelled **Films / Series** (not Movie / TV)".
# Taken from `scoring.serve` so the toggle, the ranked section and the shelf section cannot
# drift onto three spellings of the same word.
KIND_HEADINGS: dict[str, str] = dict(serve.HEADINGS)

# Proposal 22: "A greeting in four bands against §2's `TZ`". Server-side, so the band is the
# household clock rather than whatever the phone happens to be set to.
GREETING_BANDS: tuple[tuple[int, str, str], ...] = (
    (5, "up_late", "Up late"),
    (12, "morning", "Good morning"),
    (18, "afternoon", "Good afternoon"),
    (24, "evening", "Good evening"),
)

# Proposal 28: "Each shelf holds at most 12 titles … a shelf with fewer than three members is
# suppressed rather than shown short."
SHELF_CAP = 12
SECTION_FLOOR = 3

# Proposal 21: "At most three titles are named; beyond that the list reads '{title}, {title}
# and N more'."
NAMED_TITLES_CAP = 3

# Proposal 27: "Under the Series partition this shelf restates itself as 'Episodes under 45
# minutes' … the thresholds (110 min film, 45 min episode) are constants, not copy." Series
# runtime is minutes PER EPISODE, which is why one number cannot serve both.
SCHOOL_NIGHT_MAX_MIN: dict[str, int] = {"movie": 110, "series": 45}
SCHOOL_NIGHT_TITLE: dict[str, str] = {
    "movie": "Under 110 minutes",
    "series": "Episodes under 45 minutes",
}

# Pinned here, not in the spec: below this many seen titles of a kind, "you have never watched
# anything {term}" is true of nearly every term and carries no information. §6.1's own
# learning-curve copy puts the first meaningful personal signal at 5–100 labels.
FRONTIER_MIN_SEEN = 10

# Pinned here, not in the spec: the tail both people must be in for §6.5's "region both like"
# to mean anything while still admitting a shelf-sized set.
SWEET_SPOT_MIN_CDF = 0.70

# §5.1's measured optimum IN THIS APP'S COORDINATES, printed as `beta_optimum` and named in the
# caption a never-fitted profile carries. A fitted β is always preferred and always printed:
# §6.0's why-line names the number, and printing a constant the ordering did not use is exactly
# the decorative why-line §6.0 forbids.
#
# 0.2, not 0.8. Decision 167: β is the weight on the PERSONAL half here, and the corpus publishes
# its complement — its table is headed `blend beta (1.0 = crowd only)`, so the corpus optimum of
# 0.8 crowd is β = 0.2 in these coordinates (β_app = 1 − β_corpus). That is also where this app's
# own held-out Spearman peaks when `fit_user` is run over 150 real raters (+0.4324 at 0.20,
# against +0.4082 at 0.80), and the median fitted β is exactly 0.20. Nothing stored moves: the
# per-user cross-validation searches and serves in the same orientation, so every fitted number
# and every ranking was already right and only the printed constant was inverted.
#
# §5.1 and §6.0's two example strings still read `β 0.8` — that amendment is decision 167's and
# lands with M4.16's single pass over the spec file (decision 177), so the quotations elsewhere in
# this package match the file as published and this constant does not.
DEFAULT_BETA = 0.2

# §4.2's default tier set, used when `ledger_cutpoints` has no row for this (user, kind) yet.
DEFAULT_TIER_SET: tuple[str, ...] = ("F", "D", "C", "B", "A", "A+", "S")

# The standard normal the sweet spot reads each member's rank through (decision 477's scale).
_NORMAL = NormalDist()

# §6.0's shelf order, verbatim from the table. Fixed, because the table is normative.
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
    """One section that did not ship, and the sentence that says why.

    Named rather than counted: "3 shelves suppressed" cannot be acted on, and §6.0's rule is
    that a shelf which cannot justify itself is ABSENT — not present and empty — so without
    this list the absence is indistinguishable from a bug. Gated with the rest of decision
    117's annotations (`rail.GATED_KEYS`).
    """

    shelf: str
    kind: str | None
    reason: str
    # How long the builder that decided this took. NOT in `as_dict`: see `Section.ms`.
    ms: float = 0.0

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
    see_all: dict[str, Any] | None = None
    # How long this section's builder took, in milliseconds, filled by `build_shelves`.
    #
    # NOT IN `as_dict`, and that is the whole design. perf-10 is a fan-out to measure — six
    # shelves x two kinds, each with its own reads over `dna_tagged` — and the number that
    # measures it is a debugging annotation by decision 117's own definition. `rail.py`'s
    # docstring warns that "a builder that invents a new top-level numeric block does not
    # inherit the gate", so a per-section `ms` rendered beside the why-line would be a number
    # about this viewer's request travelling ungated past a gate implemented as a deletion.
    # `build_home` puts the roll-up inside the gated `model` block and `api/home.py` writes the
    # line to the server log, which is where a measurement belongs. [M4.9 finding 21]
    ms: float = 0.0

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
            "see_all": self.see_all,
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
    """Everything every shelf builder needs, resolved once per request.

    Bundling it is not tidiness: `bundle_version` binds every score and prior read (§10 — "a row
    from a superseded basis is not returned as a stale number; it is not returned at all"), and
    `version` binds every DNA read. A builder that took them as loose arguments could be called
    with one and not the other.
    """

    user_id: int
    bundle_version: str | None
    version: str | None  # the DNA vocabulary version
    kinds: tuple[str, ...]
    # Decision 475: the titles of this kind that shelves built earlier in the claim order already
    # show. A builder leaves them out and fills from its own next candidates; `build_shelves`
    # hands each builder its own copy, so no builder can widen another's.
    claimed: frozenset[int] = frozenset()
    # Decision 512: the owned titles of this kind the shelf's audience avoids - this member, or
    # either member for the shared shelf. Kept apart from `claimed` so a suppressed reason can say
    # which one emptied a shelf.
    avoided: frozenset[int] = frozenset()

    @property
    def excluded(self) -> frozenset[int]:
        return self.claimed | self.avoided


# --- the greeting -------------------------------------------------------------------------------


def greeting(now_local: datetime, name: str, *, tz: str = "") -> dict[str, str]:
    """Proposal 22's four bands, evaluated against §2's `TZ` rather than the device clock."""
    hour = now_local.hour
    band, prefix = next((b, p) for cutoff, b, p in GREETING_BANDS if hour < cutoff)
    return {"band": band, "text": f"{prefix}, {name}", "tz": tz}


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
    """§6.0's standing banner: titles already `seen` that carry no verdict. None if there are none.

    THE POPULATION, EXACTLY. `superseded_by IS NULL` is load-bearing: §4.2 is append-only and a
    re-rating supersedes rather than mutates, so "has a verdict" means "has a LIVE verdict". A
    title whose only verdict row has been superseded is unrated and belongs here — a bare
    `NOT EXISTS (SELECT 1 FROM verdict …)` would silently drop it.

    Proposal 150: this is NOT §7.3's finish prompt. That one is armed by a playback event, names
    one title, and its first tap writes `seen`. This one writes nothing at all, names up to
    three titles that are already seen "whatever set them so", and a finish prompt answered
    "yes" moves a title INTO this population and leaves it here until a verdict lands.

    FILTERED BY THE LIVE SESSION'S KINDS, because the CTA has to be able to serve what the copy
    names. §6.1's queue draws from the kinds the person set on the Rate surface, so a films-only
    session offered a banner naming a seen-but-unrated series and a link that opened on an
    unrelated film — proposal 150's own failure mode, "a prompt that names titles and then
    presents a different one is worse than no prompt". With no live session the queue has not
    been narrowed yet and both kinds are served, so both kinds are named. [M4.10 finding 23]
    """
    # One live session per person (0011's `rate_session_one_live`), so this is one row or none.
    # The default is taken from `KIND_HEADINGS` rather than spelled again, so the banner cannot
    # drift onto a third spelling of the two kinds.
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
    # Beyond three the copy names two and counts the rest (proposal 21), so the NAMED set — and
    # therefore the queue head — is two, not three. The head is the titles the copy actually
    # says; naming one set and queueing another is the failure proposal 150 exists to prevent.
    named_n = total if total <= cap else cap - 1
    named = [dict(r) for r in rows[:named_n]]
    text = _name_list([r["name"] for r in named], total)
    head = [int(r["id"]) for r in named]
    # REPEATED, not comma-joined: `GET /api/rate` declares `head: list[int] = Query([])`, so
    # `head=1,2` is a 422 and `head=1&head=2` is the contract. The client route uses the same
    # spelling so a client can forward the query string it was handed, verbatim, rather than
    # re-encoding it — which is the step at which the head would drift from the copy.
    # `mode=sweep` used to lead this query and carried nothing: `GET /api/rate` declares `head`
    # only (`api/rate.py`) and `rate/+page.svelte` reads only `head` from `searchParams`, so the
    # link stated a control neither end has. Removed rather than honoured — decision 203.
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
            # The SERVER builds the link. Proposal 150: "The CTA enters the §6.1 queue with the
            # named titles at its head — a prompt that names titles and then presents a
            # different one is worse than no prompt." A client that synthesises its own route
            # can drift from the copy it just rendered; following the app's own link cannot.
            "route": f"/rate?{query}",
            "api": f"/api/rate?{query}",
            # Decision 203 removed the dead `mode=sweep` from the two LINKS, not this field:
            # `POST /api/rate/session` does take a mode, so a client that wants the banner's
            # intent has a route for it. `GET /api/rate` still gains no `mode` parameter.
            "mode": "sweep",
            "head": head,
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

# LEFT JOIN throughout: §3.1 makes a bundle-less app legal and proposal 20 makes a zero-verdict
# user a first-week state, so a shelf ordered by recency rather than by score (§6.0's "New in
# the library") must still be buildable when no score row exists at all.
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
    """(β, fitted?) — the blend weight the scores were ACTUALLY computed with, not the ideal one.

    §5.1's optimum is β 0.2 in this app's coordinates (decision 167), and `DEFAULT_BETA` records
    it — but a profile the nightly fold-in has never fitted was ranked by the crowd prior alone,
    i.e. at β 0. Printing the optimum there would be the decorative why-line §6.0 forbids: the
    shelf would name a number that had no part in the ordering the person is looking at. So the
    fallback is 0.0 and the copy says why.
    """
    fit = await serve.fit_row(conn, user_id=user_id, kind=kind)
    if fit and fit["blend_beta"] is not None:
        return float(fit["blend_beta"]), True
    return 0.0, False


async def fit_yardstick(
    conn: asyncpg.Connection, *, user_id: int, kinds: Sequence[str], cold_eval: ColdEval | None
) -> list[dict[str, Any]] | None:
    """Each kind's fitted `cv_rho`, beside the figures the bundle itself measured.

    THE POINT IS THE REFERENCE, NOT THE NUMBER. `scoring/foldin.py` persists a held-out Spearman
    per (user, kind) and nothing could say whether 0.21 was a bad fit or a hard kind: the corpus
    ships cold 0.35225 against a ceiling of 0.39193 and the app read neither. So this returns None
    rather than a bare rho when the bundle ships no `cold_eval.json` - printing the number with
    nothing to compare it to is the defect, not a smaller version of it. §0's pipeline variance is
    applied inside `read_against`, so a 0.004 lead reads as "tie" and not as a win.

    INSIDE THE GATED `model` BLOCK, because this is an annotation about this viewer's fit rather
    than a number the ordering used. §6.0 mandates β on the why-line and on the title card's model
    line, which is why those two are ungated (decision 117's one exception); a held-out
    correlation is not in that sentence, and `rail.py` warns that "a builder that invents a new
    top-level numeric block does not inherit the gate". `build_home` therefore puts it under
    `model`, where `rail.redact` deletes it wholesale with everything else.

    One `user_vector` row per kind, on its primary key, in a payload whose shelf builders already
    read that row per shelf through `beta_of`. [M4.13 step 35, cs-31]
    """
    if cold_eval is None:
        return None
    rows: list[dict[str, Any]] = []
    for kind in kinds:
        fit = await serve.fit_row(conn, user_id=user_id, kind=kind)
        rho = None if fit is None else _float(fit["cv_rho"])
        rows.append({
            "kind": kind,
            "label_count": None if fit is None else fit["label_count"],
            **cold_eval.read_against(rho, floor=DEFAULTS.rho_noise_floor),
        })
    return rows


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _card(
    row: asyncpg.Record,
    rank: int,
    *,
    tier_set: Sequence[str],
    terms: Sequence[str],
    beta: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One shelf card.

    Proposal 29: "Every shelf card carries three overlays: its rank within the shelf (top-left),
    and the seen dot plus tier badge (top-right) … the shelf card shows the settled tier only."
    So `rank`, `seen` and the tier LETTER are chrome and stay ungated; every NUMBER behind them
    lives in `model`, which decision 117's gate removes wholesale (`rail.redact`).

    §6.3's straddle and tension badges deliberately do not appear — Home shows the settled tier.

    AND "SETTLED" IS THE MODEL'S. Decision 187 fixes proposal 29's undefined word: settled means
    the fitted tier, `ledger_state.tier` — the value that settles after a refit — so `CARD_FROM`
    joins no `tier_edit` and this letter is not the one Rank renders. The two disagree for
    exactly as long as a drop takes to be absorbed, which is the disagreement §6.3 makes visible
    with a tension badge on the surface built to show it; proposal 29 forbids that badge here,
    so Home states the model's reading and says nothing it cannot qualify. The one sentence on
    Home that must read the drop instead is shelf 1's headline, because its verb is "you put".

    The letter's SENTENCE is another matter. Decision 476 has it say "tier B, as on your Rank
    board", which is true only of a title that is on the board at all (`ls.observed`, as
    `rank/read.py` reads it) and whose board letter is this one. So `_finish` adds `on_board` and
    `board_tier` (Rank's letter: the latest drop, rescaled, else the fit) beside the badge, and the
    card says the Rank sentence only where it holds (decision 486 clause 7). The letter itself
    stays the model's.
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
        # OUTSIDE `model`, deliberately. §8 stage 10's badge — "new — model placement, no crowd
        # data" — is PRODUCT, not debugging, and decision 117's gate strips `model` wholesale
        # (`rail.GATED_KEYS`), so a badge computed from these two would simply vanish for every
        # person with the toggle off, which is everyone by default. `PosterCard.svelte:47-55`
        # states the rule the card must follow and it is the specification: "Off `e_source`/
        # `item_n`, NOT off `title.placement`." With them gated, `placement` was the only branch
        # Home could reach, and `0008_placement.sql:54-58` stamps `cold_tower` on any title with
        # a Backbone row and `item_n < 90` — so on the reference library 111 of the 130 badges
        # Home drew were false. `placement` stays beside them because §6.3's chrome and the
        # Cold Tower shelf both read it; what changed is that it is no longer the only thing a
        # card carries about its crowd support. [M4.9 finding 18]
        "item_n": row["item_n"],
        "e_source": row["e_source"],
        "seen": bool(row["seen"]),
        "rank": rank,
        "tier": tier,
        # The receipt for the why-line: which of the NAMED terms this card actually carries,
        # read back out of `dna_tagged` rather than asserted by the shelf (§6.8).
        "terms": list(terms),
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


async def _cards(
    conn: asyncpg.Connection,
    rows: Sequence[asyncpg.Record],
    *,
    ctx: Ctx,
    named_terms: Sequence[WhyTerm],
    beta: float,
    tier_set: Sequence[str],
) -> list[dict[str, Any]]:
    terms = [t.term for t in named_terms if t.role == "member"]
    carried: dict[int, list[str]] = {}
    if ctx.version and terms:
        carried = await why_mod.carried_by(
            conn, title_ids=[int(r["title_id"]) for r in rows], terms=terms, version=ctx.version
        )
    return [
        _card(row, i + 1, tier_set=tier_set, terms=carried.get(int(row["title_id"]), []), beta=beta)
        for i, row in enumerate(rows)
    ]


async def _finish(
    conn: asyncpg.Connection, section: Section, *, shelf_id: str, ctx: Ctx
) -> tuple[Section | None, Suppressed | None]:
    """The one gate every section passes through before it is allowed into the payload.

    Three conditions, all from §6.0. The shelf must have something to show (proposal 28's floor
    of three); it must have a why-line at all; and the terms it NAMES must be carried by every
    card it returns. The third is a second, independent read of the claim — the builders derive
    membership from the terms, so a failure means the builder is broken, and a broken builder
    must suppress a shelf rather than print the wrong why.
    """
    if len(section.items) < SECTION_FLOOR:
        # Named when the claim is what emptied it (decision 475), so "3 shelves above took the
        # titles" reads differently from "this household owns nothing like that".
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
        ids = [c["title_id"] for c in section.items]
        broken = await why_mod.unsupported(
            conn, why_terms=section.why_terms, title_ids=ids, version=ctx.version
        )
        if broken:
            return None, Suppressed(
                shelf_id,
                section.kind,
                f"why-line named {', '.join(broken)}, which not every card carries",
            )
        # Every shelf gets a vocabulary clause where the vocabulary supports one. Computed by
        # intersection over the cards actually returned, so it cannot be false (§6.8).
        section.shared_terms = await why_mod.common_terms(conn, title_ids=ids, version=ctx.version)
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
    """The letter §6.3's board renders for each of these titles that is on it.

    On the board is `ledger_state.observed` - §6.3's "every rated title", read as `rank/read.py`
    reads it - so a title §7.2's sync marked watched and nobody rated is absent, fitted tier or
    not. Where it is, the letter is the one `rank/board.build` renders: the latest `tier_edit`,
    re-read in the current set through `rescale_level` exactly as `rank/read.items` re-reads it,
    and the fitted tier where there is no drop. Decision 476's "tier B, as on your Rank board" is
    a quotation of this value, so the card is handed the value rather than a guess at it.
    """
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

    LIKENESS, THEN THE PAIR (decision 475, as decision 513 weighs it). The shelf holds the
    unseen owned titles most like the anchor - its terms weighed by how rare they are in the
    household's library, and in the anchor's own form, animated or live-action - and names a pair
    every one of them carries. It used to choose the pair covering the most titles and show that
    intersection by score, which named the anchor's most generic pair and let the score's
    extremes decide the rest; and then to count shared terms, which let the generic ones decide
    the cards. The why-line is still true of every card by construction (proposal 24): the pair
    is read off the cards, not assumed. Titles the member avoids never enter (decision 512).

    THE HEADLINE SAYS WHAT THE PERSON DID (decision 476). "you put X in S" only where a
    `tier_edit` exists - a person who has tiered nothing was being told they had put Mission:
    Impossible in S - and otherwise "you liked X" for a live liked verdict, or "More like X".
    """
    sid = "because_anchor"
    if not ctx.version:
        return None, Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")

    # Proposal 24: "Shelf 1's anchor is the user's top-scoring **seen** title in the active
    # kind." No fallback to the top-scoring title: the section says "Because you put X in A",
    # which is only true of a title this person actually placed.
    #
    # TWO PREDICATES, NOT ONE, and neither implies the other. `ut.state = 'seen'` is proposal
    # 24's; `ls.observed` is §6.3's "every rated title", read exactly as `rank/read.py:6` reads
    # it. They came apart because `refit_user` writes a `ledger_state` row for EVERY owned title
    # of the kind, observed or not (`ledger/refit.py:350-374`) — so a title §7.2's sync merely
    # marked watched arrived here with a fitted `tier`, and won the ORDER BY whenever its prior
    # beat the rated titles. Home then said "Because you put Title 9 in C" about a title with
    # zero observations, which is not even on the Rank board that sentence is quoting.
    # The converse fails too, which is why the `seen` join stays: a verdict implies seen
    # (`ledger/observations.py:640-641`) but a duel or a tier edit does not, so an observed
    # title can be one this person has never watched. [M4.9 finding 15]
    #
    # WHICH TIER THE HEADLINE NAMES is the same question §6.3 answers for the board, and the
    # verb decides it: "Because you PUT X in A" is a sentence about the last thing this person
    # did, not about the fit. `rank/board.py:20-27` — "the most recent `tier_edit` decides where
    # a title renders, and the model decides it only when there is no edit" — so the same
    # `DISTINCT ON (title_id)` subquery `rank/read.py:99-104` uses is joined here, rather than a
    # correlated subquery that would re-run per row against an index keyed (user_id, created_at)
    # which cannot answer "this title's latest". `model_tier` travels alongside because the
    # range check below is about the FIT, not about the drop. [M4.9 finding 16, decision 187]
    #
    # WHICH TITLE ANCHORS is the person's own assignment first - the tier the board shows (their
    # drop, or the fit where they dropped nothing), then their live verdict - and only then the
    # Ledger's `s`. An argmax of `s` alone let the scale of one coordinate choose: a title the
    # Cold Tower placed sat at s 21.8 against 9.7 for the next, and anchored the shelf over the
    # household's own top-tier titles (decision 475).
    #
    # "The tier the board shows" is a drop READ IN THE CURRENT SET, so the candidates come back
    # whole and are ordered here, after `rescale_level`, rather than by `COALESCE(te.tier,
    # ls.tier)` in SQL: decision 11 keeps a drop's index across a change in K, and an S drop on a
    # 7-level board (index 6) renders as T11 of 12 but sorted as 6, below any title fitted at
    # T7-T10. The comparison has to be made on the scale Rank renders, or the anchor is not the
    # title in the highest tier the board shows. [decision 475, decision 11]
    #
    # "Their live verdict" is `LIVE_LABEL_SQL` - the newest non-re-ask row - and not
    # `superseded_by IS NULL`: after §13's silent re-ask the only un-superseded row is the
    # instrument's, and the tie-break and the "you liked" headline would both report the re-ask's
    # answer as the person's (the reason `rate/direct.live_verdict` gives for the same read).
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
        # Both predicates, named: "seen" and "rated" fail for different reasons and are fixed by
        # different actions, and a reason line that names only one sends a person who has rated
        # nothing off to mark titles watched (§6.8 — the suppressed list exists to be acted on).
        return None, Suppressed(
            sid, kind,
            "no title of this kind is both seen and rated with a fitted tier yet",
        )

    def shown(row: asyncpg.Record) -> int:
        # RESCALED, not suppressed, and only for the ASSIGNED tier. Decision 11 keeps `tier_edit`
        # rows across a change in K, so a drop into a level that no longer exists is a state this
        # shelf is guaranteed to meet — and the answer is the one `rank/read.py` and
        # `rank/board.py` give, through the same helper, because §6.0 row 1's verb ("you PUT it
        # there") makes the headline a quotation of what §6.3 renders. Until M4.13 both surfaces
        # CLAMPED, so both named tier 6 of a grown 12-level set: agreeing, and wrong. Mapping on
        # one side only would have been worse than either — Home saying "in T6" while Rank showed
        # T11 is ml01's own measured symptom. Suppressing shelf 1 over a stale level would take
        # the whole shelf down for the one person who uses drag-and-drop most.
        # [decision 11, M4.9 finding 16, M4.13 dd06]
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
    # The FIT is checked, and not clamped: a model tier outside the set means the refit and the
    # cutpoints disagree, which is a bug rather than a state, and no clamp should paper over it.
    model_index = int(anchor["model_tier"])
    if not 0 <= model_index < len(tier_set):
        return None, Suppressed(
            sid, kind, f"anchor tier index {model_index} is outside the tier set"
        )
    index = shown(anchor)

    # Decision 513: the anchor's nearest titles by specificity-weighted likeness, in its own form,
    # and the pair among its terms that names them best. The neighbourhood is two per card slot,
    # so the pair is chosen to describe the nearest titles rather than to define a wider set.
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
        items=await _cards(conn, rows, ctx=ctx, named_terms=[t1, t2], beta=beta, tier_set=tier_set),
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 2: top_of_ledger -------------------------------------------------------------------


async def top_of_ledger(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 2 — "Top of your ledger" / "clean item prior + your fold-in, blended at β 0.8".

    Sourced from `scoring.serve.ranked_section`, which is the one ranked statement in the app
    and binds `kind` as a scalar. Reusing it is what makes "the number the shelf sorts on" and
    "the number the ranked surface sorts on" the same arithmetic rather than two of them.

    Proposal 25: "Unless a shelf's why-line says otherwise, shelves exclude titles the user has
    already seen; 'Top of your ledger' is the one exception and says so ('your highest,
    rewatches included')." Hence `seen='any'`, and hence the clause in the copy.
    """
    sid = "top_of_ledger"
    if not ctx.bundle_version:
        return None, Suppressed(sid, kind, "no active artifact bundle — no scores to rank")

    # The claim does not thin this shelf (decision 475); what the member avoids does (decision
    # 512): "the ones we think you'll enjoy most" is not true of a pattern they disliked four times.
    ranked = await serve.ranked_section(
        conn,
        user_id=ctx.user_id,
        kind=kind,
        bundle_version=ctx.bundle_version,
        seen="any",
        owned_only=True,
        limit=SHELF_CAP,
        exclude=sorted(ctx.avoided),
    )
    # The number the why-line prints is the number the ordering used: `serve` reports the stored
    # `blend_beta`, or 0.0 for a profile the nightly fold-in has never fitted.
    beta = float(ranked["beta"])
    personalised = bool(ranked["personalised"])
    tier_set = await tier_set_of(conn, user_id=ctx.user_id, kind=kind)
    items = [
        _card(row, i + 1, tier_set=tier_set, terms=[], beta=beta)
        for i, row in enumerate(_as_card_rows(ranked["items"]))
    ]
    why = (
        # Decision 476: §6.0's row in the member register. The β the ordering used still travels,
        # in `why_numbers`, which Show the model reveals (decision 486); the sentence says what a
        # person can check without it - whose taste ranked this and that rewatches are in it.
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
                     # §5.1's evidence k, from the field the gate itself reads
                     # (`scoring.backbone.EVIDENCE_K` is the same object). It was the literal 10
                     # here and again below, which is one quantity under three spellings: a
                     # re-tuned k would have moved the gate the cards report and left this
                     # why-line naming the old one. [M4.13 step 34d]
                     "gate_k": DEFAULTS.gate_k},
        # No caption: the one it carried named §5.1 and a β to a member (decision 486), and the
        # optimum it quoted rides `why_numbers` beside the fitted β.
        caption=None,
        items=items,
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


def _as_card_rows(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """`serve.ranked_section` returns `id` and `seen_state`; `_card` reads `title_id` and `seen`.
    One rename, in one place, rather than a second copy of the ranked statement."""
    return [
        dict(item, title_id=item["id"], seen=item["seen_state"] == "seen", placement_at=None)
        for item in items
    ]


# --- shelf 3: never_watched_term --------------------------------------------------------------


async def never_watched_term(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 3 — "You've never watched anything *{term}*" / "unvisited region of DNA space
    next to what you like" (§6.4's frontier, surfaced as a shelf).

    `role` is what keeps this honest. The candidate term is a **member** — every card carries it,
    which is what makes the title checkable. The neighbouring liked term is **anchor_side**: it
    describes the user's region, not the cards, which are unvisited by definition. §6.4's rule
    that "every connection is *nameable*" is satisfied because the edge is that term, printed.

    "which you like" is the member's own verdicts, from another facet than the candidate's
    (decision 514), and the titles the member avoids are neither counted nor shown (decision 512).
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
        # Decision 476: §6.4's frontier, said the way a member can check it. The cosine goes to
        # `why_numbers` with the rest of the model's reading (decision 486).
        why=f"close to {neighbour.name}, which you like",
        why_terms=[candidate, neighbour],
        why_numbers={"cos": round(cos, 4), "affinity": round(aff, 4),
                     "min_seen": FRONTIER_MIN_SEEN},
        # §6.4's measured explore policy ("~1 exploratory slot in 6 … cost ≈ −1 pp top-hit rate,
        # honestly labelled") is still what this shelf is, and it still says so - in words: the
        # label is that it is a step outside the usual, on purpose, rather than a top-hit rate.
        caption="a step outside what you usually watch, on purpose",
        items=await _cards(
            conn, rows, ctx=ctx, named_terms=[candidate], beta=beta, tier_set=tier_set
        ),
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 4: shared_sweet_spot ---------------------------------------------------------------


async def partner_for(conn: asyncpg.Connection, *, user_id: int) -> dict[str, Any] | None:
    """Proposal 26: "`{other}` is the member with the most co-seen titles … ties broken by the
    most recent co-seen title."

    A LEFT JOIN rather than an inner one, so a household whose second member has watched nothing
    in common still has an `{other}`: the shelf's real gate is the sweet spot itself (both above
    the CDF floor), and a zero co-seen count is an answer to "who", not the absence of one.
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
    """§6.0 row 4 — "You and {other} would both enjoy these" / "neither of you has seen them — a
    good pick for a night in together" (the shared sweet spot, ranked as Tonight's pool is).

    Ordered by the PLAIN AVERAGE of the two members' scores RANK-STANDARDISED over the owned
    library of the kind - the normal quantile of each member's own rank - which is how §6.2 step 3
    reads the Tonight pool's order since decision 477 ("the average is taken over each member's
    scores rank-standardised over the frozen pool"). That is what makes "ranked as Tonight's pool
    is" a shared arithmetic rather than a claim. It averaged the raw scores until the second
    household test, and the two members' raw scores sit on two scales (owned films: mean 0.39
    and 0.16, spread 0.83 and 0.93), so one member's units could outvote the other's. Plain
    averaging stands (§6.2: dominance rules cost −0.012).

    WHAT EITHER MEMBER AVOIDS IS OUT (decision 512): `ctx.avoided` is the union of the two
    members' avoid sets here, because "you would both enjoy these" is false of a title one of
    them has turned down the pattern of four times. That, and not the scale, is what the test
    found: one member read Past Lives, Hamnet and The Worst Person in the World as "both" after
    disliking every romance he had rated, and the other read Chainsaw Man after disliking every
    violent film she had.

    THE 0..1 WEIGHT. §5.2 defines it as "the empirical CDF of the user's own fitted `s` values,
    computed per kind", and `ledger_state.cdf` carries it — but only for titles the person has
    RATED. This shelf ranks titles neither has seen, so the same construction is applied to the
    serving score inside (user, kind) with `percent_rank()`, over the OWNED titles of the kind:
    the library this shelf and Tonight's pool rank. It read every scored title until the first
    household install, where 9.5k mostly-unowned titles put the owned library's mean at 0.42-0.46
    and the floor was being read against a long tail nobody could press Play on.
    [owner instruction of 2026-09-25 after the first household user test, C1.7]

    COPY NOTE. §6.0's title read as "have already rated" over titles neither has seen, and the
    caption had to contradict it. Decision 476 restates the row: the title says what the shelf
    predicts, and no number reaches a member with Show the model off (decision 486).
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
            row, i + 1, tier_set=tier_set, terms=[], beta=beta,
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
        # True of every card: unseen by both, near the top of both lists, and ordered by the plain
        # average Tonight's pool is ordered by (§6.2 step 3). The floor it clears is a number and
        # rides `why_numbers` (decision 486).
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
    """§6.0 row 5 — "Under 110 minutes" / "for a school night", restated per proposal 27.

    A NULL runtime is excluded: a shelf that claims a runtime bound must know the runtime. The
    comparison is strict, so a title at exactly the threshold is not "under" it.
    """
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
        items=await _cards(conn, rows, ctx=ctx, named_terms=[], beta=beta, tier_set=tier_set),
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- shelf 6: new_in_library ------------------------------------------------------------------


async def new_in_library(
    conn: asyncpg.Connection, *, ctx: Ctx, kind: str
) -> tuple[Section | None, Suppressed | None]:
    """§6.0 row 6 — "New in the library" / "no outside ratings yet, so we placed them by what
    they're about".

    Ordered by recency rather than by score, which is why it is the one shelf that still ships
    for a user with no verdicts (proposal 20 suppresses "every score-ordered shelf").

    THE CLAIM IS CHECKABLE, and proposal 33 says what makes it so: `item_n` — the count of crowd
    ratings behind a title, as §4.3 ships it. (Not "§5.1's gate input", which is the same number
    only while every row carries a coordinate: a cold-masked row has crowd support and no n_t,
    and `title_prior.gate` is the column that carries the gate. [M4.13 cycle 2, M413-C2-DIM5-01])
    The predicate is `title.placement = 'cold_tower'` (§8 stage 10's badge), a prior that is not
    `backbone`/`blended` (§5.1's evidence gate saying the same thing from the model's side), AND
    no crowd rating at all. The third clause is the one the docstring always promised and the
    SQL never read: the bundle's evaluation holdout serves 2,879 crowd-rated rows from the Cold
    Tower, so on the first household install 161 of this shelf's 180 film candidates had crowd
    ratings behind them - Raiders of the Lost Ark with 192,061 - under a why-line saying there
    were none. A title with crowd support is warm and must not be here, whichever writer ran last.

    Decision 475 exempts this shelf from the claim: it states a fact about an arrival, and a
    shelf that hid a new title because another shelf showed it would stop being true.
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
        # Decision 476: the same fact without the model's nouns (decision 486).
        why="no outside ratings yet, so we placed them by what they're about",
        why_numbers={"gate_k": DEFAULTS.gate_k},     # see `top_of_ledger` - one field, one k
        items=await _cards(conn, rows, ctx=ctx, named_terms=[], beta=beta, tier_set=tier_set),
    )
    return await _finish(conn, section, shelf_id=sid, ctx=ctx)


# --- assembly -----------------------------------------------------------------------------------

# `ranking=True` for the five shelves ordered by a ledger score; `new_in_library` is ordered by
# recency. All six partition by kind regardless — a Home row reads as a recommendation.
RANKING_SHELVES: frozenset[str] = frozenset(SHELF_IDS) - {"new_in_library"}

# Decision 475's claim order: "Your top picks" first, then the table's order. The display order
# stays `SHELF_IDS`; only the order in which shelves take titles changes.
CLAIM_ORDER: tuple[str, ...] = ("top_of_ledger",) + tuple(
    s for s in SHELF_IDS if s != "top_of_ledger"
)
# Every ranking shelf claims. "New in the library" neither claims nor is thinned: it states a
# fact about an arrival rather than ranking one, and it is built last in both orders anyway.
CLAIMING_SHELVES: frozenset[str] = RANKING_SHELVES


async def live_verdict_count(conn: asyncpg.Connection, *, user_id: int) -> int:
    """§4.2: a re-rating supersedes rather than mutates, so "how many verdicts" means "how many
    LIVE verdicts" here too — the same predicate the banner uses."""
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
    zero_verdicts: bool = False,
    partner: dict[str, Any] | None = None,
    avoided: taste.Avoided | None = None,
) -> tuple[list[Shelf], list[Suppressed]]:
    """§6.0's six shelves, in the table's order, each as one section per selected kind.

    Proposal 20's zero-verdict state is applied here rather than in a second code path: "tier
    badges, ledger weights and every score-ordered shelf are meaningless, so Home falls back to
    the catalog grid plus a route into the §6.1 seed-list queue." `new_in_library` is ordered by
    recency, not by a ledger nobody has yet, so it survives — that is a reading of the phrase,
    stated rather than assumed.

    Decision 512: every ranking shelf leaves out what its audience avoids - this member's avoid
    set, or for the shared shelf the union of both members' - and "New in the library", which
    reports an arrival rather than ranking one, leaves out nothing.
    """
    if partner is None:
        partner = await partner_for(conn, user_id=ctx.user_id)
    if avoided is None:
        avoided = await taste.avoided_for(conn, user_id=ctx.user_id, version=ctx.version)
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

    # Decision 475: BUILT in claim order, RENDERED in the table's. "Your top picks" claims first
    # and keeps its whole list, because its why-line promises your highest; every later shelf
    # leaves out what an earlier one of the same kind already shows and fills from its own next
    # candidates. Without it one household's Home drew three score-ordered shelves off the top of
    # one list, and Raiders, American History X and Dunkirk each appeared twice in one render.
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
            # perf-10: measured, not restructured. Every builder is timed at the one place they
            # are all called, so the number is the same arithmetic for all six and no builder
            # can be given a stopwatch someone else forgot. `perf_counter` and not `time`: this
            # is an interval, and a wall clock can move backwards under NTP.
            started = perf_counter()
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
            elapsed = (perf_counter() - started) * 1000.0
            if section is not None:
                section.ms = elapsed
                built[shelf_id, kind] = section
                if shelf_id in CLAIMING_SHELVES:
                    claimed[kind].update(int(c["title_id"]) for c in section.items)
            elif note is not None:
                # `replace` rather than assignment: `Suppressed` is frozen, because a reason line
                # that could be edited after the fact is not a record of why a shelf did not ship.
                notes[shelf_id, kind] = replace(note, ms=elapsed)

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


def sections_by_kind(shelves: Sequence[Shelf], kinds: Sequence[str]) -> list[dict[str, Any]]:
    """The same shelves, grouped the other way: one kind-headed region holding its own shelves.

    Two views of one structure, offered because both readings of §4.1 rule 5's "two headed
    sections" are legitimate renderings and neither can express an interleaved ranking — the
    items live inside a kind-scoped section in both. Nothing is duplicated: the section objects
    are the same ones `shelves` carries.
    """
    return [
        {
            "kind": kind,
            "heading": KIND_HEADINGS[kind],
            "shelves": [
                dict(shelf.as_dict(), sections=[s.as_dict() for s in shelf.sections if s.kind == kind])
                for shelf in shelves
                if any(s.kind == kind for s in shelf.sections)
            ],
        }
        for kind in kinds
    ]


async def build_home(
    conn: asyncpg.Connection,
    *,
    user: Any,
    kinds: Sequence[str],
    bundle_version: str | None,
    now_local: datetime,
    tz: str = "",
    q: str | None = None,
    person_id: int | None = None,
    limit: int = 60,
    offset: int = 0,
    cold_eval: ColdEval | None = None,
) -> dict[str, Any]:
    """The whole §6.0 Home payload, ungated. `rail.redact` applies decision 117 afterwards.

    `cold_eval` is the active bundle's own evaluation of the cold path, from the store the route
    holds; None on a bundle-less install and on any bundle older than that file. See
    `fit_yardstick`.

    THE MODE IS THE SERVER'S. §6.0: "Search or an active person-filter switches Home into the
    catalog grid; clearing it returns the shelves." Computing it here rather than in the client
    is what makes the two modes mutually exclusive by construction — a client cannot render
    shelves over a person filter, because with one set the payload carries no shelves to render.
    """
    chosen = library.normalise_kinds(kinds)
    ctx = Ctx(
        user_id=user.id,
        bundle_version=bundle_version,
        version=await why_mod.vocabulary_version(conn),
        kinds=tuple(chosen),
    )
    verdicts = await live_verdict_count(conn, user_id=user.id)
    mode = "grid" if (q and q.strip()) or person_id is not None else "shelves"
    partner = await partner_for(conn, user_id=user.id)

    payload: dict[str, Any] = {
        "mode": mode,
        "kinds": chosen,
        "greeting": greeting(now_local, user.name, tz=tz),
        "banner": await pending_verdicts(conn, user_id=user.id),
        "verdict_count": verdicts,
        "bundle": bundle_version,
        "vocabulary": ctx.version,
        "partner": partner,
        # What the shelves draw on: the household's OWNED titles, per kind. Home's count line used
        # to state the whole catalog - "13,330 films · 5,747 series hidden" - above shelves that
        # hold only owned titles, which is a count of something the screen is not showing.
        "library": await library.count_by_kind(conn, owned_only=True),
        "shelves": [],
        "sections": [],
        "shelves_total": 0,
        "catalog": None,
        "degraded": _degraded(bundle_version, verdicts),
        "suppressed": [],
        "avoiding": None,
        # NO EMBEDDED RAIL, for the reason `api/tonight.py` gives one file away: one drawer, not
        # one per route. This payload carried `rail.recent(user_id=...)` because the drawer was
        # mounted on Home and decision 117's gate was asked of the response
        # (`hasModelAnnotations`, whose whole body was `Array.isArray(payload?.rail)`). The
        # frontend shell stage of this milestone moved the mount into `+layout.svelte` and made
        # `showModel` read the preference, so `ModelRail` refetches `/api/model-log` on every
        # open and nothing in the app reads this key — up to RAIL_LIMIT events rode every Home
        # load, on Home's cadence rather than the drawer's, for no reader. The client gate went
        # with the key rather than staying behind it: asked of a payload that can no longer
        # carry a rail it answers `false` for every response this function builds, so the next
        # surface to reach for it hides the numbers decision 117 was turned on to show.
        # `suppressed` stays: it is a Home fact, it has no route of its own, and the drawer
        # renders it as a prop.
        # [M4.9 review cycle 1: M49-HOME-04]
    }

    if mode == "grid":
        # Decision 18: a surface that merely LISTS in a kind-independent order may interleave
        # freely. This is that surface — `library.list_titles`, ordered by year, or best match
        # first under a search (decision 472), which is a property of the text and not a kind.
        rows, total = await library.list_titles(
            conn, kinds=chosen, user_id=user.id, q=q, person_id=person_id,
            limit=limit, offset=offset,
        )
        payload["catalog"] = {
            "total": total,
            # The same filters as the listing above: a person filter over a four-title
            # filmography must not report "26 series hidden".
            "hidden": await library.count_by_kind(
                conn, exclude=chosen, user_id=user.id, q=q, person_id=person_id
            ),
            "limit": limit,
            "offset": offset,
            "q": q,
            "person_id": person_id,
            "items": rows,
        }
        return payload

    avoided = await taste.avoided_for(conn, user_id=user.id, version=ctx.version)
    shelves, dropped = await build_shelves(
        conn,
        ctx=ctx,
        zero_verdicts=(verdicts == 0 and bundle_version is not None),
        partner=partner,
        avoided=avoided,
    )
    # Decision 512, said where the member can read it: what their shelves leave out, by the
    # vocabulary's names and the canonical genres, and the film length past which they stop.
    # Plain facts about their own ratings, so ungated; null when nothing is left out.
    payload["avoiding"] = avoided.as_dict() if avoided else None
    payload["shelves"] = [s.as_dict() for s in shelves]
    payload["sections"] = sections_by_kind(shelves, chosen)
    payload["shelves_total"] = len(shelves)
    payload["suppressed"] = [s.as_dict() for s in dropped]
    # INSIDE `model`, so decision 117's gate removes it with everything else it removes. This is
    # the only top-level annotation Home's payload carries and it exists so `api/home.py` can
    # write one measurement line without re-timing what `build_shelves` already timed.
    payload["model"] = {
        "sections_ms": timings_of(shelves, dropped),
        # The fold-in's own quality number with something to read it against, per kind. None
        # when the bundle ships no reference - see `fit_yardstick`. [M4.13 step 35]
        "fit": await fit_yardstick(
            conn, user_id=user.id, kinds=chosen, cold_eval=cold_eval
        ),
    }
    return payload


def timings_of(
    shelves: Sequence[Shelf], dropped: Sequence[Suppressed]
) -> list[dict[str, Any]]:
    """Every builder that ran this request, slowest first. perf-10's instrument.

    Suppressed sections are in here too, and they have to be: a shelf that reads its whole
    population and then fails `_finish`'s floor costs the same milliseconds as one that ships,
    and a measurement that counted only what shipped would report the cheap half of the fan-out.
    """
    rows = [
        {"shelf": shelf.id, "kind": section.kind, "shipped": True, "ms": round(section.ms, 1)}
        for shelf in shelves
        for section in shelf.sections
    ] + [
        {"shelf": s.shelf, "kind": s.kind, "shipped": False, "ms": round(s.ms, 1)}
        for s in dropped
    ]
    return sorted(rows, key=lambda r: r["ms"], reverse=True)


def _degraded(bundle_version: str | None, verdicts: int) -> dict[str, Any] | None:
    """Proposal 20's two first-week states. Neither is optional, and neither is an error.

    Both fall out of the suppression rules anyway — with no scores every ranking section is under
    the floor — so this only supplies the right copy and the right route, never a second code
    path that could disagree with what actually shipped.
    """
    if bundle_version is None:
        return {
            "state": "no_bundle",
            "headline": "No artifact bundle imported.",
            # No section sign in a member's sentence, even on a state only a fresh install shows
            # (decision 486); §4.3 is where this fact is specified, and it stays in this comment.
            "why": "the catalog and the shelves come from a data bundle an admin imports",
            "cta": {"label": "Import a bundle", "route": "/admin/data"},
        }
    if verdicts == 0:
        return {
            "state": "zero_verdicts",
            # Decision 476: the first-week state in the member register. §6.1's learning curve is
            # still the promise, counted in ratings rather than "labels" (decision 486).
            "headline": "Rate a few titles to get your shelves.",
            "why": "your suggestions get about three times more personal between 5 and 100 "
                   "ratings — aim for 50–100 in your first sitting or two",
            "cta": {"label": "Rate 50 titles", "route": "/rate"},  # decision 203
        }
    return None
