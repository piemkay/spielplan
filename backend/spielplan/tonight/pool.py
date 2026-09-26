"""§6.2 step 3's candidate pool. Spec v2.1 §6.2 steps 1 and 3, §0 row 3, §4.1 rule 5, §5.1, §10.

    "**Candidate pool (internal — never shown as a step):** owned titles passing the
     kind/budget/rewatch filters, ranked by the **plain average** of member Ledger scores
     (measured: nothing dominates averaging; dominance rules cost −0.012). Guests contribute no
     taste term unless they have a grid profile."

Two halves, split by what they can be falsified with. The **arithmetic** — which title outranks
which, and how far over budget one runs — is a function of numbers and lives at the top of this
file, pure. The **membership** is a query and lives at the bottom, because §7.2 re-derives
`is_owned` from Jellyfin and a stale flag is exactly the failure a pure test cannot see.

PLAIN, AND WHAT IT RULES OUT. §0 row 3 is the measurement: "no aggregation rule dominates plain
averaging, and dominance rules cost −0.012" against a documented noise floor of 0.003–0.008. So
the mean here is not a default that a cleverer rule may later replace — max-min and the Nash
product were the two v1.1 proposed, and both are *measured worse*. `group_score` therefore takes
a bare map of scores and returns their mean, with no seat weights and nowhere to put one.

AND WHAT ORDERS IT IS THE LEDGER ALONE. §0 row 4: the stored mood profile is worth **0.000** for
choose-tonight, and within-evening re-ranking is worth 0.000 as well. The tilt is something the
*round* learns and adds to a participant's tonight score; it never reaches the prior the round
starts from, which is why nothing in this module accepts one.

THE BUDGET IS SOFT, AND SAYS SO. §6.2 step 1: "a **runtime budget slider** (soft — the pool
admits up to budget + 40 min; over-budget results are labelled 'runs N min over')". Admission
and labelling are one pass (`with_budget`) so the two cannot disagree about where the boundary
is, and N is measured from the budget the person set rather than from the +40 bound they never
saw.

AND ON A SERIES NIGHT IT IS PER EPISODE. §6.2 step 1 as amended by 54h: the bound is compared
with the show's per-episode runtime, never with a season or a series total, "and every label that
states a number on a series card says so". That is what the arithmetic here has always done —
`title.runtime_min` is per-episode for a series — so this module's change is the label and not the
rule. Hiding the slider under Series was the alternative and it removes a control §6.2 step 1
gives for both kinds. [decision 219]
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

import asyncpg

from spielplan.db import genres as genre_vocab

# ONE SCALE FOR EVERY MEMBER, FROZEN WITH THE POOL (decision 477). §5.1 standardises a score's cf
# half over the whole reference population, which the owned pool is not drawn from: on the first
# household evening one member's owned-pool scores ran to 13.28 (owned cf sd 3.16 against 0.45
# elsewhere) and the other's to 3.13, so the plain average of step 5 was one person's Ledger, and
# the round's boundary resolved after one pair. Every Tonight read of a member's Ledger therefore
# goes through `rank_normal` below — ranks over tonight's pool, mapped onto normal quantiles at
# sd 1.0 — and the marker names the rule a room was started under, so a deploy never changes the
# prior, the next pair or the combine of an evening already in flight (decision 223's reason for
# freezing the hold-out nonce with the pool). A rule that changes takes a new marker.
SCALE_MARKER = "rank_normal_sd1"
# sd 1.0 and not 0.5, from the sweep `test_tonight_round.py` pins beside `BOUNDARY_Z`: at a
# 700-title pool the median round is 14.5 pairs at sd 0.5, 12.5 at 0.75 and 11.5 at 1.0, and 1.0
# is also the variance decision 214's `prior_var = 1.0` argument assumes. [decision 477]
SCALE_SD = 1.0

_NORMAL = NormalDist()

# §6.2 step 1's "not tonight" control (decision 480): up to three of these per seated member
# (decision 505), each a presence predicate over vocabulary-v1 terms. Authored here rather than in
# the bundle because the list is a household control and not a model artifact; a term the active
# vocabulary does not carry matches nothing, so a re-import can only make a veto remove less,
# never break it.
VETOES: dict[str, tuple[str, tuple[str, ...]]] = {
    "violence": ("violence", ("mood.violent", "themes.violence", "mood.gory")),
    "sexual_violence": ("sexual violence", ("themes.sexual_violence",)),
    "horror": ("horror", ("mood.terrifying", "themes.slasher", "themes.body_horror")),
    "harrowing": (
        "harrowing",
        ("sensibility.harrowing", "sensibility.emotionally_devastating", "mood.devastating",
         "mood.bleak"),
    ),
}
MAX_VETOES = 3

# The tiers a veto reads, both of them and each by name, so the predicate still carries §4.1 rule
# 1's discriminator and still compares no weight (rule 2). Decision 480 read the quote-verified
# tier alone, and the second household evening (WX-7467, "violence", "horror" and "harrowing"
# ruled out) is what that cost: John Wick, Transformers: Revenge of the Fallen and In Bruges were
# all served to the member who had ruled out violence, because each carries mood.violent or
# themes.violence by projection and by nothing else. A person saying "nothing violent" wants
# recall over precision (decision 504). That evening's pool, rebuilt on the live install without
# its vetoes, is 669 owned films at 120 min: the extracted tier keeps 463 of them under those
# three vetoes and both tiers keep 281, and among the titles the evening actually showed, Raiders
# of the Lost Ark, Wolf Children and Rear Window also leave by projection alone. That is the price,
# and `play.start` still names the vetoes when they empty a pool.
VETO_TIERS: tuple[str, ...] = ("extracted", "projected")


def veto_terms(keys: Iterable[str]) -> list[str]:
    """The vocabulary terms a set of veto keys removes; an unknown key removes nothing."""
    return sorted({term for key in keys if key in VETOES for term in VETOES[key][1]})


def veto_labels(keys: Iterable[str]) -> list[str]:
    return [VETOES[k][0] for k in VETOES if k in set(keys)]

# §6.2 step 1: "the pool admits up to budget + 40 min". The spec's number, not a tunable — it
# is not a constant of the §5.2 recipe, so §4.3's `ledger_hyperparams.json` is not where it
# belongs (the same reasoning `rank/queue.py` applies to §6.3's 70/20/10 shares).
BUDGET_GRACE_MIN = 40

# §6.2 step 1's slider default: the budget a label falls back to when the session row that held
# one cannot be read. A bare `130` used to sit in the reveal's card builder inside `api/tonight.py`
# — the one place that prints "fits your N min" — which made the router a second holder of a
# number `fit_line` below quotes verbatim. The spec's figure rather than a tunable, for the same
# reason `BUDGET_GRACE_MIN` above is. [M4.12 arch-06]
DEFAULT_BUDGET_MIN = 130

# 54h's qualifier, and the one place it is spelled. §6.2 step 1 defines the budget for both kinds
# and was silent on what it means for a series; the code had already picked an answer — the bound
# below is applied to `title.runtime_min`, which for a series is minutes PER EPISODE
# (`home/shelves.py:938` says the same thing over the same column) — and the labels did not say
# so. Measured against the shipped bundle the series pool is 121 of 121 owned titles at budget 60,
# 130 and 200 alike, so the slider narrows nothing on a series night and a bare "fits your 60 min"
# reads as a promise about the evening. Decision 219 keeps the per-episode reading and makes every
# label that states a number say which number it is. [decision 219; 54h]
KIND_SERIES = "series"
PER_EPISODE = " per episode"


@dataclass(frozen=True)
class Seat:
    """One participant of a session, as the pool needs them.

    `is_member` is the taste question and not the account question: §6.2 step 3 says "Guests
    contribute no taste term **unless they have a grid profile**", so a persistent guest who
    has filled in the 60-title grid is a member for this purpose and an ephemeral one is not.
    The grid itself is M7 (§12), so today the two coincide — but the pool asks the question it
    means, so that M7 changes one predicate rather than every read.
    """

    participant_id: int
    user_id: int | None
    is_member: bool


@dataclass(frozen=True)
class Candidate:
    """One title the evening could resolve to.

    `scores` is per **participant id**, not per user id: a session is seats, and the round's
    arithmetic is over seats. `over_budget_min` and `fit_line` are stamped by `with_budget`
    once, at session open, because three surfaces render them (the pair card, the result card,
    solo) and three recomputations drift.
    """

    title_id: int
    kind: str
    name: str
    runtime_min: int | None
    scores: Mapping[int, float]
    over_budget_min: int | None = None
    fit_line: str = ""
    year: int | None = None
    poster_path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def group_score(self) -> float:
        return group_score(self.scores)


# --- the plain average -------------------------------------------------------------------


def group_score(scores: Mapping[int, float]) -> float:
    """§6.2 step 3's "plain average of member Ledger scores".

    Unweighted, and structurally unweightable: there is no second argument. A seat's label
    count, its seniority and its hosting the room are all irrelevant, which is what "plain"
    means and what §0 row 3 measured as undominated.
    """
    if not scores:
        return 0.0
    return sum(scores.values()) / len(scores)


def rank_normal(scores: Mapping[int, float], *, sd: float = SCALE_SD) -> dict[int, float]:
    """One member's scores over tonight's pool, as normal quantiles of their own ranks.

    Φ⁻¹((r − 0.5)/n)·sd, r the 1-based rank ascending, ties broken by title id so two builds over
    the same numbers agree. Monotone, so a member's own order is untouched; what changes is that
    every member now spans the same range, and a runaway favourite counts for the pool's top
    quantile rather than for thirteen of somebody else's units (decision 477). A pool of one has
    no spread to map, so its title sits at the centre.
    """
    items = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    if n < 2:
        return {t: 0.0 for t, _ in items}
    return {t: sd * _NORMAL.inv_cdf((i + 0.5) / n) for i, (t, _) in enumerate(items)}


def score_for_seats(
    member_scores: Mapping[int, float], seats: Sequence[Seat]
) -> dict[int, float]:
    """Keep only the seats that contribute a taste term.

    A guest is *omitted*, not zeroed. Zeroing looks identical on a single title and is a
    different rule: it drags every candidate toward the bottom by the same amount, which
    preserves the order but destroys the scale the D threshold (§6.2 step 5) is measured on.
    """
    contributing = {s.participant_id for s in seats if s.is_member}
    return {pid: v for pid, v in member_scores.items() if pid in contributing}


def order(candidates: Iterable[Candidate]) -> list[Candidate]:
    """The pool, best first.

    Ties break by `title_id` rather than by input order: the pool is computed once at session
    open and carried through the round (§6.2 step 7 — nothing re-ranks within the evening), so
    a second build over the same numbers has to produce the same list, and a stable sort over
    an unordered query result is not stable at all.
    """
    return sorted(candidates, key=lambda c: (-c.group_score, c.title_id))


# --- the soft budget ---------------------------------------------------------------------


def admits(*, runtime_min: int | None, budget_min: int) -> bool:
    """§6.2 step 1's soft bound.

    A title of unknown runtime is admitted. `title.runtime_min` is nullable and the corpus has
    gaps; the budget is soft by design, so a title nobody can measure is not evidence that it
    runs long, and dropping it would remove a watchable film from the evening over a missing
    metadata field.
    """
    if runtime_min is None:
        return True
    return runtime_min <= budget_min + BUDGET_GRACE_MIN


def over_budget_by(*, runtime_min: int | None, budget_min: int) -> int | None:
    """How far past the slider a title runs, or None when it fits (or cannot be measured).

    Measured from the budget the person set, never from the +40 admission bound they never
    saw: the label exists to make the softness legible, and a label counting from a hidden
    number would make it less so.
    """
    if runtime_min is None or runtime_min <= budget_min:
        return None
    return runtime_min - budget_min


def fit_line(*, runtime_min: int | None, budget_min: int, kind: str) -> str:
    """§6.2 step 8's two branches, verbatim: "fits your 130 min" / "runs 21 min over" — and on a
    series session, 54h's qualifier: "fits your 60 min per episode".

    `kind` is required rather than defaulted, because a default is how the qualifier would go
    missing on exactly the surface it exists for: every caller holds the kind already (the
    candidate carries it, and the reveal's card takes the session's), and the one that did not
    was the one printing an unqualified runtime on a series card.

    "runtime unknown" states no number, so it takes no qualifier — decision 219 puts it on the
    labels that say something, and a qualifier on a label that measures nothing would be
    precision about an absence. [decision 219]
    """
    if runtime_min is None:
        return "runtime unknown"
    per = PER_EPISODE if kind == KIND_SERIES else ""
    over = over_budget_by(runtime_min=runtime_min, budget_min=budget_min)
    if over is None:
        return f"fits your {budget_min} min{per}"
    return f"runs {over} min over{per}"


def with_budget(candidates: Iterable[Candidate], *, budget_min: int) -> list[Candidate]:
    """Apply the budget and stamp the label in one pass.

    One pass because a title admitted by one rule and labelled by another that disagrees about
    the boundary is the failure mode a soft budget invites — and it is invisible until someone
    reads "fits your 130 min" on a 171-minute film.
    """
    import dataclasses

    out = []
    for c in candidates:
        if not admits(runtime_min=c.runtime_min, budget_min=budget_min):
            continue
        out.append(
            dataclasses.replace(
                c,
                over_budget_min=over_budget_by(runtime_min=c.runtime_min, budget_min=budget_min),
                # The candidate's own kind, not the session's, though 0013 makes them the same:
                # `session.kind` is single-valued because "an evening resolves to ONE title", so
                # asking the row is asking the session, and it keeps this pass from needing an
                # argument it would only pass through. [§4.1 rule 5; decision 219]
                fit_line=fit_line(
                    runtime_min=c.runtime_min, budget_min=budget_min, kind=c.kind
                ),
            )
        )
    return out


# --- membership: the query ---------------------------------------------------------------


async def build(
    conn: asyncpg.Connection,
    *,
    seats: Sequence[Seat],
    kind: str,
    budget_min: int,
    include_rewatches: bool,
    bundle_version: str,
    vetoed_terms: Sequence[str] = (),
    dna_version: str | None = None,
) -> list[Candidate]:
    """§6.2 step 3's pool, ordered.

    Four filters, and each one is a sentence:

      * **owned** — "owned titles passing the …". §7.2 re-derives `is_owned` from Jellyfin, so
        an unowned title on the winner card is a Play-on-Jellyfin CTA that opens nothing.
      * **kind** — §4.1 rule 5 binds every ranking surface, and an evening resolves to ONE
        title, so the session picked a side rather than rendering two sections.
      * **rewatch** — §6.2 step 1's default excludes titles *every* participant has seen. The
        quantifier is the rule: "any participant has seen" would strip the household's shared
        favourites out of every evening, and is the only reading under which a rewatch toggle
        would be off by default.
      * **budget** — applied in `with_budget` above, after the query, because the label is
        arithmetic and the admission bound is the same arithmetic.

    §10's invariant binds `bundle_version` into the read: a score from a superseded basis is
    not returned as a stale number, it is not returned at all.

    And a fifth, when the room asked for it: **not tonight** — a title carrying a vetoed term in
    either tier, quote-verified or projected, is not a candidate (decisions 480 and 504). A
    presence predicate on `term` and `tier` through the sanctioned view, like §6.4's `NOT
    has(...)`, and never a salience or confidence threshold (§4.1 rule 2). No vocabulary version
    means no DNA to read, so nothing is vetoed rather than everything.
    """
    member_user_ids = [s.user_id for s in seats if s.is_member and s.user_id is not None]
    by_user = {s.user_id: s.participant_id for s in seats if s.user_id is not None}
    if not member_user_ids:
        return []

    rows = await conn.fetch(
        """
        SELECT t.id AS title_id, t.kind, t.name, t.year, t.runtime_min, t.poster_path,
               us.user_id, us.score
          FROM user_score us
          JOIN title t ON t.id = us.title_id
         WHERE us.user_id = ANY($1::bigint[])
           AND us.kind = $2
           AND us.bundle_version = $3
           AND t.is_owned
           AND (
                $4::boolean
                OR EXISTS (
                    SELECT 1 FROM unnest($1::bigint[]) AS m(user_id)
                     WHERE NOT EXISTS (
                        SELECT 1 FROM user_title ut
                         WHERE ut.user_id = m.user_id AND ut.title_id = t.id
                           AND ut.state = 'seen'
                     )
                )
           )
           AND (
                cardinality($5::text[]) = 0 OR $6::text IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM dna_tagged d
                     WHERE d.title_id = t.id AND d.version = $6 AND d.tier = ANY($7::text[])
                       AND d.term = ANY($5::text[])
                )
           )
        """,
        member_user_ids, kind, bundle_version, include_rewatches,
        list(vetoed_terms), dna_version, list(VETO_TIERS),
    )

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(
            row["title_id"],
            {
                "title_id": row["title_id"],
                "kind": row["kind"],
                "name": row["name"],
                "year": row["year"],
                "runtime_min": row["runtime_min"],
                "poster_path": row["poster_path"],
                "scores": {},
            },
        )
        seat_id = by_user.get(row["user_id"])
        if seat_id is not None:
            entry["scores"][seat_id] = float(row["score"])

    # A title only one member has a score for is not comparable to one both do: the mean of a
    # single score is that score, which would let a title nobody else can see outrank the
    # household's actual agreement. Every seated member must have scored it.
    wanted = {by_user[u] for u in member_user_ids}
    candidates = [
        Candidate(
            title_id=e["title_id"], kind=e["kind"], name=e["name"], year=e["year"],
            runtime_min=e["runtime_min"], poster_path=e["poster_path"],
            scores=score_for_seats(e["scores"], seats),
        )
        for e in grouped.values()
        if set(e["scores"]) >= wanted
    ]
    return order(with_budget(candidates, budget_min=budget_min))


# How many genres a pair card names beside the year and the runtime. The second household evening
# asked "Warriors of the Wind or Perfect Days?" of a member who knew neither, and the card said only
# "1984 · fits your 120 min". Two, in decision 473's canonical order, because a pair card is half a
# phone wide and a third genre wraps it past the fold §6 preamble's phone-first layout holds.
CARD_GENRES = 2


async def genres_of(conn: asyncpg.Connection, title_ids: Sequence[int]) -> dict[int, list[str]]:
    """Each title's genres as a card names them: decision 473's one canonical vocabulary, read
    across the structured sources with Wikidata's free text left out, as the catalogue's facet
    reads them. Plain words a member already knows, and never a DNA term — the pair card is shown
    before anything is decided, so it describes the title and says nothing about the pool."""
    rows = await conn.fetch(
        "SELECT title_id, lower(genre) AS genre FROM title_genre "
        "WHERE title_id = ANY($1::int[]) AND source <> ALL($2::text[])",
        list(title_ids), list(genre_vocab.EXCLUDED_SOURCES),
    )
    raw: dict[int, set[str]] = {}
    for r in rows:
        raw.setdefault(r["title_id"], set()).add(r["genre"])
    return {t: genre_vocab.facet(labels)[:CARD_GENRES] for t, labels in raw.items()}


__all__ = [
    "BUDGET_GRACE_MIN",
    "CARD_GENRES",
    "DEFAULT_BUDGET_MIN",
    "KIND_SERIES",
    "MAX_VETOES",
    "PER_EPISODE",
    "SCALE_MARKER",
    "SCALE_SD",
    "VETOES",
    "VETO_TIERS",
    "Candidate",
    "Seat",
    "admits",
    "build",
    "fit_line",
    "genres_of",
    "group_score",
    "order",
    "over_budget_by",
    "rank_normal",
    "score_for_seats",
    "veto_labels",
    "veto_terms",
    "with_budget",
]
