"""The §6.1 sweep queue: which title to ask about next, and the one line saying why.

§6.1: "**Queue:** P(seen)-ordered (Jellyfin history, popularity, household co-seen), seeded
first run from the imported 100-title decade-stratified `seed_list`. Blocks of 15; each card
shows its queue reason ('queued because: 72% likely you have seen it')." The reason is a plain
sentence since the second household test and its probability waits behind Show the model (see
`reason_for`).

Three sentences, three rules, and they compose in one ORDER BY:

  1. **Recorded-seen first.** A title the app already holds as `seen` with no verdict is
     exactly the population of §6.0's pending-verdicts banner. It is not an estimate, so
     `p_seen` is 1.0 for it by definition rather than by a large weight, and "seen first" and
     "highest P(seen) first" are then the same instruction rather than two that can diverge.
  2. **Then the seed list, most likely seen first (decision 490).** A fresh household has no
     seen rows and no verdicts, so their first queue *is* `seed_list` — the spec's "seeded first
     run" falls out of the ordering instead of needing a mode flag. Seed precedence ends by
     CONSUMPTION: once every seed title carries an answer the branch is empty and P(seen) governs
     for good. All 100 stay; what changed is the order INSIDE the list. It was the file's own
     position order, which on v20260925 put 56 pre-2010 titles first and asked a household about
     divisive films it had mostly not seen: 33% not-seen in the first block. Ordered by P(seen)
     — popularity, the library, age, and the household — the same two members' own answers put
     that first block at 1 not-seen in 28. "Decade-stratified" is a property of the list the
     corpus chose, and every title in it is still served before anything outside it.
  3. **Then descending P(seen).**

A title the person PINNED — §6.0's pending-verdicts banner, the title card's "Rate it" or Rate's
own search — leads everything, is served even over an earlier "not seen" (a verdict writes seen,
and the person has just said they know it), and names the pin as its reason.

WHAT P(SEEN) IS, AND WHAT IT IS NOT
It is a six-feature logistic over signals this app already holds, and it exists to *order a
queue*. It never enters `score_u(t)` and it is not a model feature: §4.1 rule 3 keeps the
display schema away from the feature builder, and nothing here reads it. `title_prior.item_n`
is the sanctioned popularity quantity — §4.3 ships it as "the per-title support counts" — so
the popularity term is a crowd *support count*, never a crowd *score*.

That sentence used to rest on §4.3's parenthetical instead ("the §5.1 gate input"), and for one
milestone it was false here. The gloss holds only while every row carries a coordinate: M4.13
excluded `cold_mask` rows from the basis, so a title the crowd rated 260,131 times has n_t = 0
because there is nothing for the gate to weight — and `serve.materialise_priors` wrote that 0
into this column for 2,879 rows of v20260828, 375 of the owned ones shipping `item_n >= 90`.
At weight 2.0 through `log1p(n)/log1p(1e5)` that is the whole of the crowd term, so the sweep
ordered the most-watched films in the catalogue as if nobody had ever seen them. The column now
carries `Backbone.crowd_support`, which is the file's own count for every row, and
`title_prior.gate` carries the gate. [M4.13 cycle 2, M413-C2-DIM5-01]

The weights are a stated prior, not a fit. There is no labelled data to fit them on until the
surface runs; the surface then generates exactly that label, because a verdict means seen and
`Not seen` means unseen. §13 names the instrument that falsifies them — "not-seen rate in the
rating queue (>50% = queue bug)" — and `not_seen_rate` below computes it. Fitting is a later
milestone's move; inventing a fit now would be inventing the data.

WHY THE FORMULA IS WRITTEN TWICE
Postgres orders and Python explains. The SQL evaluates the logistic so `ORDER BY ... LIMIT` can
work over the whole catalog without shipping it to the client; Python evaluates the same
logistic on the returned row so the reason line can name the *dominant* term. They are two
spellings of one formula, and the moment they disagree the returned cards stop being sorted by
the number they report — which is what
`test_once_the_seed_list_is_answered_the_queue_is_ordered_by_descending_p_seen` checks.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.rate import reask as reask_stream

log = logging.getLogger("spielplan.rate.queue")

# ---------------------------------------------------------------------------------------------
# TUNED NUMBERS. These belong in `spielplan/ledger/hyperparams.py` — that module is "the only
# module in the package allowed to contain a tuning number", and re-tuning is supposed to reach
# the app through `ledger_hyperparams.json`. It is wave-1 frozen for this milestone, so they sit
# here, in one block, under the same contract: change them here and nowhere else. Reported as a
# gap.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeenWeights:
    """Log-odds weights for P(seen). Every one of them is named in a reason line, so the
    weighting is auditable from the UI and not only from this file."""

    intercept: float = -2.2   # nothing known at all -> 0.10: the base rate for "have you seen
                              # this arbitrary catalog title"
    playback: float = 2.5     # §7.3's >=90% playback poll fired and nobody answered the prompt
    co_seen: float = 1.2      # §6.1's "household co-seen"
    crowd: float = 2.0        # §6.1's "popularity", as title_prior.item_n
    owned: float = 0.8        # it is in the Jellyfin library (§7.2 keeps is_owned re-derived)
    age: float = 0.6          # more years on the shelf, more chances to have seen it
    unfamiliar: float = 2.0   # this person keeps answering "not seen" to titles of its language
                              # and kind (see `unfamiliarity`); the feature is <= 0, so this
                              # only ever lowers P(seen)


WEIGHTS = SeenWeights()

# The pseudo-count `unfamiliarity` shrinks a person's own answers towards "no opinion" with: two
# seen and two not-seen, so one "not seen" moves a language's titles by 0.4 logit rather than by
# the whole weight, and the term only bites once the answers keep saying the same thing.
FAMILIAR_PSEUDO = 2.0

# A title is "old" for this purpose once it has been out four decades; past that the extra years
# stop carrying information about whether this household saw it.
AGE_SATURATION_YEARS = 40.0
# The item_n at which the popularity term saturates. The transform is log1p(n)/log1p(SAT)
# clipped to 1: n=10 -> 0.21, n=1e3 -> 0.60, n=1e4 -> 0.80. Rank-preserving, so it cannot
# reorder the catalog relative to a percentile version of itself; only the spacing differs.
CROWD_SATURATION = 100_000.0

# §13: "not-seen rate in the rating queue (>50% = queue bug)".
NOT_SEEN_BUG_THRESHOLD = 0.50
NOT_SEEN_WINDOW = 200

# A recorded state is not an estimate.
P_SEEN_RECORDED = 1.0

SOURCES: tuple[str, ...] = ("pinned", "seed", "p_seen", "pending_verdict", "reask")

FEATURE_NAMES: tuple[str, ...] = ("playback", "co_seen", "crowd", "owned", "age", "unfamiliar")

# The popularity term at which a title is "well-known" in member copy: about a thousand crowd
# ratings (log1p(1000) / log1p(CROWD_SATURATION) = 0.60). On the v20260926b catalogue that is the
# top 35% of films and the top 8% of series, and every seed film clears it. A title below it is
# never called well-known, whatever list it is on -- the list the corpus ships is not the claim.
WELL_KNOWN_CROWD = 0.6


# --- the estimate -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Features:
    """The six circumstantial signals, plus the one recorded fact that overrides them."""

    seen: bool = False        # user_title.state = 'seen' — a record, not a signal
    playback: bool = False
    co_seen: float = 0.0      # share of the *other* active household members who have seen it
    crowd: float = 0.0        # log1p(item_n) / log1p(CROWD_SATURATION), clipped
    owned: bool = False
    age: float = 0.0          # (this year - release year) / 40, clipped
    unfamiliar: float = 0.0   # -1..0: `unfamiliarity` of this person's answers in its language

    def vector(self) -> dict[str, float]:
        return {
            "playback": float(self.playback),
            "co_seen": float(self.co_seen),
            "crowd": float(self.crowd),
            "owned": float(self.owned),
            "age": float(self.age),
            "unfamiliar": float(self.unfamiliar),
        }


def unfamiliarity(seen: float, answered: float, pseudo: float = FAMILIAR_PSEUDO) -> float:
    """How firmly this person's own answers say they do not know titles of one language and kind.

    §6.1 orders the queue by P(seen) over signals that are all about the TITLE -- its crowd, the
    library, its age, the household -- and none about what this person has already told the
    queue. So the second household test watched it drift: Jenny answered "not seen" to Attack on
    Titan and to Berserk, Monster came three cards later on its crowd count, and Death Note now
    leads her series queue on the `owned` term -- the household owns 39 Japanese series, and
    nothing in the formula knows whose they are. Her own answers are the only signal that does,
    and they are exactly the label P(seen) predicts: a verdict means seen and `Not seen` unseen.

    The share of the person's answered titles in the group that they had seen, shrunk towards a
    half by `pseudo` answers each way, read only below a half: 0 until the answers lean towards
    "not seen", -1 at the limit. Never above zero, because a language the person knows well is no
    reason to ask about a title in it that nothing else says they saw -- this lowers titles the
    person keeps not knowing and moves nothing else, so the why-line never names it.
    [H7 of the 2026-09-26 household test]
    """
    share = (seen + pseudo) / (answered + 2.0 * pseudo)
    return min(0.0, share - 0.5) * 2.0


def contributions(features: Features, weights: SeenWeights = WEIGHTS) -> dict[str, float]:
    """Each term's signed contribution to the log-odds. The intercept is deliberately absent:
    it is the same for every title, so it explains nothing about *this* one."""
    v = features.vector()
    return {name: getattr(weights, name) * v[name] for name in FEATURE_NAMES}


def p_seen(features: Features, weights: SeenWeights = WEIGHTS) -> float:
    """P(this person has seen this title).

    `seen` short-circuits to 1.0 rather than entering the logistic with a large weight. The app
    is not estimating there — §7.3 already adopted the state, or the person set it — and a
    number below 1 would leave a recorded fact competing with an accumulation of circumstantial
    evidence, which is how a title nobody watched ends up ahead of one that was.
    """
    if features.seen:
        return P_SEEN_RECORDED
    z = weights.intercept + sum(contributions(features, weights).values())
    return 1.0 / (1.0 + math.exp(-z))


def dominant(features: Features, weights: SeenWeights = WEIGHTS) -> str | None:
    """The term that put this title where it is. None when nothing at all is known about it —
    then the why-line carries the probability and stops, rather than naming a cause worth 0."""
    scored = contributions(features, weights)
    best = max(scored, key=lambda name: (scored[name], name))
    return best if scored[best] > 0.0 else None


# §6.8: "every shelf, recommendation, question and conflict carries a one-line why". Phrased in
# the person's vocabulary and not the model's — the card is asking them to remember something.
# Whole sentences since the second household test: "queued because: 86% likely you have seen it"
# was the queue's working printed for a member, and the probability is a model number, which
# decision 486 keeps behind Show the model (the card's `model.p_seen`). [A3 of 2026-09-26]
PHRASES = {
    "playback": "You played it to the end.",
    "co_seen": "Someone else in the house has seen it.",
    "crowd": "A well-known {noun}.",
    "owned": "It's in your library.",
    "age": "It has been out {years}.",
}

# The line when no term is strong enough to name -- nothing known at all, or a crowd too small to
# call the title well-known.
UNSURE_REASON = "One you might have seen."

# The one sentence a recorded-seen card carries. It is deliberately the *whole* truth about that
# card and no more: "you have this marked seen". §13's re-ask targets are also marked seen, so
# they get this same sentence from this same branch and the wire cannot tell the two apart. The
# tempting longer form — "...and have not rated it" — is what would give the stream away, and it
# would be false on exactly the cards it gave away.
SEEN_REASON = "You have this marked as seen."

# The person asked for this one — Rate's search or the title card's "Rate it" — so the pin IS the
# reason, and a probability beside it would explain a placement the queue did not make.
PINNED_REASON = "You picked this one."


def reason_for(
    features: Features,
    *,
    source: str,
    kind: str = "movie",
    seed_decade: int | None = None,
    years_out: int | None = None,
    weights: SeenWeights = WEIGHTS,
) -> str:
    """§6.8's mandatory one-line why, in the copy register the spec calls "quiet reasons".

    A seed card printed "seed list position 0 of 100": 0-based, a file's index, and a statement
    about the corpus's list rather than about the person. Decision 490 then made it "a starter
    title from the 1970s · 86% likely you have seen it", which the second household test still
    read as jargon: "starter title" is the corpus's name for its list and the percentage is the
    queue's model. So a seed card says what the title is -- "A well-known film from the 1970s"
    -- and says "well-known" only where the crowd term bears it out (`WELL_KNOWN_CROWD`); a seed
    below that line says the one thing true of every list title, that everyone is asked it
    first. No card carries a number: P(seen) travels beside the reason, behind Show the model.
    [§6.1, §6.8, decision 486; A3 of the 2026-09-26 household test]
    """
    if source in ("pending_verdict", "reask"):
        return SEEN_REASON
    if source == "pinned":
        return PINNED_REASON
    noun = "series" if kind == "series" else "film"
    known = features.crowd >= WELL_KNOWN_CROWD
    if source == "seed":
        when = f" from the {seed_decade}s" if seed_decade else ""
        if known:
            return f"A well-known {noun}{when}."
        return f"A {noun}{when} we ask everyone about first."
    cause = dominant(features, weights)
    if cause == "crowd" and not known:
        # The crowd term leads but is too small to call the title well-known: name the strongest
        # term that can be said truthfully instead.
        scored = contributions(features, weights)
        rest = [name for name in scored if name != "crowd" and scored[name] > 0.0]
        cause = max(rest, key=lambda name: (scored[name], name)) if rest else None
    if cause is None:
        return UNSURE_REASON
    years = years_out if years_out is not None else 0
    return PHRASES[cause].format(years="1 year" if years == 1 else f"{years} years", noun=noun)


# --- the card ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueCard:
    title_id: int
    reason: str            # §6.8 one-line why, e.g. "It's in your library."
    p_seen: float | None
    source: str            # seed | p_seen | pending_verdict | reask
    reask_of: int | None   # verdict.id being silently re-asked; None otherwise

    def public(self) -> dict[str, Any]:
        """The allow-list projection that may reach the client.

        §13 stream (b) requires the served payload to carry "no marker distinguishing a re-ask
        from a first observation", and `source` gives it away exactly as loudly as `reask_of`
        does — so neither is in here. An allow-list and not a copy-and-delete: a field added to
        this dataclass later has to be added to this dict on purpose before it can leak.
        """
        return {"title_id": self.title_id, "reason": self.reason, "p_seen": self.p_seen}


# --- the candidate query ----------------------------------------------------------------------

# Two exclusions, each a rule rather than a nicety:
#   * a title the person has ever given a real verdict on does not come back. The predicate is
#     `NOT is_reask` and not `superseded_by IS NULL`, because a re-ask supersedes the row it
#     re-asks (`record_verdict` does this deliberately) — a predicate on that column would hand
#     every re-asked title straight back to the fresh queue.
#   * a title the person has explicitly answered "not seen" does not come back either. An
#     *adopted* unseen is an absent row, never an 'unseen' row (see `sync/seen.py`'s table), so
#     this only ever removes an answer somebody actually gave. A PINNED title is the exception:
#     the person has just searched for it or tapped "Rate it" on it, which is a newer answer than
#     the "not seen" — and one household member's mark-seen-then-wait detour never reached the
#     film at all. The pin lifts this exclusion and nothing else; a rated title stays out.
#
# The person's history arrives as three small CTEs joined to `title`, rather than as correlated
# sub-selects evaluated per row. Ordering by a computed score means the whole partition is
# scored and sorted whatever the LIMIT is, so the per-row cost is the cost: on a 30,000-title
# catalog the sub-select spelling took 267 ms for a block of 15 and this one takes 43 ms. Each
# CTE is bounded by what one household has done, not by the catalog.
_CANDIDATES = """
WITH household AS (
    SELECT count(*)::float8 AS n
      FROM app_user
     WHERE id <> $1 AND is_active AND role IN ('admin', 'member')
), co_seen AS (
    SELECT o.title_id, count(*)::float8 AS n
      FROM user_title o
      JOIN app_user au ON au.id = o.user_id
     WHERE o.user_id <> $1 AND o.state = 'seen'
       AND au.is_active AND au.role IN ('admin', 'member')
     GROUP BY o.title_id
), played AS (
    SELECT DISTINCT title_id
      FROM playback_event
     WHERE user_id = $1 AND finished AND title_id IS NOT NULL
), rated AS (
    SELECT DISTINCT title_id FROM verdict WHERE user_id = $1 AND NOT is_reask
), familiar AS (
    -- This person's own answers per kind and original language: `unfamiliarity`'s two counts.
    -- An 'unseen' row is an answer somebody gave (an adopted unseen is an absent row).
    SELECT t.kind, t.original_language AS lang,
           count(*) FILTER (WHERE ut.state = 'seen')::float8 AS seen_n,
           count(*)::float8                                   AS answered_n
      FROM user_title ut
      JOIN title t ON t.id = ut.title_id
     WHERE ut.user_id = $1 AND t.original_language IS NOT NULL
     GROUP BY t.kind, t.original_language
), cand AS (
    SELECT t.id,
           t.kind,
           t.year,
           -- The clock the printed age is counted on, selected rather than read off
           -- `datetime.now()` in Python: `age` below is clipped to 1.0 because that is where the
           -- *feature* saturates, so the why-line cannot be derived from it and has to subtract
           -- years itself. Two clocks would make the sentence disagree with the ordering it
           -- explains across a midnight or a mis-set container TZ; both `now()` calls in this
           -- statement are the one transaction timestamp. [M4.10 finding 19, §6.8]
           EXTRACT(year FROM now())::int                             AS this_year,
           COALESCE(ut.state = 'seen', false)                       AS seen,
           t.is_owned                                               AS owned,
           sl.position                                              AS seed_position,
           sl.decade                                                AS seed_decade,
           (pl.title_id IS NOT NULL)                                AS playback,
           COALESCE(cs.n, 0.0)                                      AS co_seen_n,
           COALESCE(tp.item_n, 0)::float8                           AS item_n,
           CASE WHEN t.year IS NULL THEN 0.0
                ELSE least(1.0, greatest(0.0,
                     (EXTRACT(year FROM now())::float8 - t.year::float8) / $5::float8))
           END                                                      AS age,
           COALESCE(fa.seen_n, 0.0)                                 AS lang_seen_n,
           COALESCE(fa.answered_n, 0.0)                             AS lang_answered_n,
           array_position($6::int[], t.id)                          AS head_pos
      FROM title t
      LEFT JOIN user_title  ut ON ut.title_id = t.id AND ut.user_id = $1
      LEFT JOIN seed_list   sl ON sl.title_id = t.id
      LEFT JOIN title_prior tp ON tp.title_id = t.id
      LEFT JOIN co_seen     cs ON cs.title_id = t.id
      LEFT JOIN played      pl ON pl.title_id = t.id
      LEFT JOIN rated       rt ON rt.title_id = t.id
      LEFT JOIN familiar    fa ON fa.kind = t.kind AND fa.lang = t.original_language
     WHERE t.kind = ANY($2::text[])
       AND NOT (t.id = ANY($3::int[]))
       AND rt.title_id IS NULL
       AND (t.id = ANY($6::int[]) OR NOT (ut.title_id IS NOT NULL AND ut.state = 'unseen'))
), scored AS (
    SELECT c.*,
           least(1.0, ln(1.0 + c.item_n) / ln(1.0 + $7::float8))              AS crowd,
           least(1.0, c.co_seen_n / greatest(1.0, (SELECT n FROM household))) AS co_seen,
           least(0.0, (c.lang_seen_n + $15::float8)
                      / (c.lang_answered_n + 2.0 * $15::float8) - 0.5) * 2.0 AS unfamiliar
      FROM cand c
)
SELECT s.*,
       CASE WHEN s.seen THEN 1.0
            ELSE 1.0 / (1.0 + exp(-( $8::float8
                                     + $9::float8  * (s.playback)::int
                                     + $10::float8 * s.co_seen
                                     + $11::float8 * s.crowd
                                     + $12::float8 * (s.owned)::int
                                     + $13::float8 * s.age
                                     + $14::float8 * s.unfamiliar ))) END AS p_seen
  FROM scored s
 ORDER BY s.head_pos ASC NULLS LAST,
          NOT s.seen,
          -- Decision 490: the seed list still leads, and inside it P(seen) decides.
          s.seed_position IS NULL,
          p_seen DESC,
          s.seed_position ASC NULLS LAST,
          s.id
 LIMIT $4
"""


def _features(row: asyncpg.Record) -> Features:
    return Features(
        seen=bool(row["seen"]),
        playback=bool(row["playback"]),
        co_seen=float(row["co_seen"]),
        crowd=float(row["crowd"]),
        owned=bool(row["owned"]),
        age=float(row["age"]),
        unfamiliar=float(row["unfamiliar"]),
    )


def _card(row: asyncpg.Record, *, weights: SeenWeights) -> QueueCard:
    features = _features(row)
    if features.seen:
        source = "pending_verdict"
    elif row["head_pos"] is not None:
        source = "pinned"
    elif row["seed_position"] is not None:
        source = "seed"
    else:
        source = "p_seen"
    years_out = None
    if row["year"] is not None:
        # From the year, NOT from `age` — which is the clipped feature and therefore pegged at
        # 1.0 for everything released before `now() - AGE_SATURATION_YEARS`. Multiplying it back
        # out printed "it has been out 40 years" for 6,806 of the corpus's 19,071 titles, on
        # exactly the cards where the age term is the dominant one and so the one the line names
        # (unowned, no playback, nobody else in the house, no crowd support). §6.8 makes the
        # why-line normative copy, and a sentence that is wrong by eleven years for a 1975 film
        # is not a quiet reason, it is a false one. The feature stays clipped: saturation is the
        # model's claim that the 41st year carries no more information, and this changes only
        # what the card says. [M4.10 finding 19]
        years_out = max(0, int(row["this_year"]) - int(row["year"]))
    return QueueCard(
        title_id=int(row["id"]),
        reason=reason_for(
            features,
            source=source,
            kind=str(row["kind"]),
            seed_decade=row["seed_decade"],
            years_out=years_out,
            weights=weights,
        ),
        # A seed card carried None here while the list was served in file order, because the
        # queue had not used a probability to place it. Decision 490 orders the list by P(seen),
        # so the number is now the one that placed the card, as on every other card.
        p_seen=float(row["p_seen"]),
        source=source,
        reask_of=None,
    )


async def next_sweep_cards(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    limit: int,
    exclude: Sequence[int] = (),
    head: Sequence[int] = (),
    rng: random.Random | None = None,
    reask_rate: float = reask_stream.REASK_RATE,
    weights: SeenWeights = WEIGHTS,
) -> list[QueueCard]:
    """The next `limit` sweep cards, best first. An empty list means the queue is drained.

    `head` is the §7.3 banner CTA's pins: "You've watched X and Y recently — rate them?" puts
    those title ids at the front, in the order given, and they stay ordinary candidates — a
    pinned title that has since been rated is simply not there. Rate's own search and the title
    card's "Rate it" pin the same way; a pin also lifts an earlier "not seen" (see `_CANDIDATES`),
    and `exclude` still wins over it — the session decides what a pin may re-open.

    `rng`, `reask_rate` and `weights` are test seams, not part of the interface this module
    publishes: the declared call — `next_sweep_cards(conn, user_id=..., kinds=..., limit=...,
    exclude=..., head=...)` — behaves exactly as specified without them. §13 stream (b) needs a
    coin flip per slot, and a coin nobody can hold still cannot be tested.
    """
    if not kinds:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    if limit <= 0:
        return []
    rng = rng or random.Random()
    skip = list(dict.fromkeys(int(t) for t in exclude))

    fresh_rows = await conn.fetch(
        _CANDIDATES,
        user_id,
        list(kinds),
        skip,
        limit,
        AGE_SATURATION_YEARS,
        [int(t) for t in head],
        CROWD_SATURATION,
        weights.intercept,
        weights.playback,
        weights.co_seen,
        weights.crowd,
        weights.owned,
        weights.age,
        weights.unfamiliar,
        FAMILIAR_PSEUDO,
    )
    fresh = [_card(row, weights=weights) for row in fresh_rows]

    # §13 stream (b): "~10% of comparisons/verdicts re-asked after >= 3 days". The draw is per
    # slot, so the rate is a property of the queue rather than of how long a sitting ran.
    reasks: list[reask_stream.VerdictReask] = []
    if reask_rate > 0.0:
        reasks = await reask_stream.verdict_candidates(
            conn, user_id=user_id, kinds=kinds, limit=limit, exclude=skip, rng=rng
        )
    return _interleave(
        fresh, reasks, limit=limit, rate=reask_rate, rng=rng, head=tuple(head)
    )


def _interleave(
    fresh: list[QueueCard],
    reasks: Sequence[reask_stream.VerdictReask],
    *,
    limit: int,
    rate: float,
    rng: random.Random,
    head: Sequence[int] = (),
) -> list[QueueCard]:
    """Spend about `rate` of the slots on re-asks, and fall through when either pool runs dry.

    Falling through matters in both directions: a household with nothing old enough to re-ask
    still gets a full queue, and a household that has rated everything still gets asked
    something — a queue made only of re-asks is what §13's stream looks like at the end of the
    catalog.

    `head` is the exception, and it has to be. §6.0's pending-verdicts banner names up to three
    titles and its CTA "opens the §6.1 queue with those titles at the head of the queue". The
    pin is applied upstream as an ORDER BY, so without this the coin comes up re-ask on ~10% of
    taps and spends the pinned slot on a different title — the banner names three films and
    serves a fourth, which is the exact failure that requirement exists to prevent. A pinned
    card is never traded for a re-ask; §13 has every other slot.
    """
    fresh_q = list(fresh)
    reask_q = list(reasks)
    pinned = set(head)
    out: list[QueueCard] = []
    taken: set[int] = set()
    while len(out) < limit and (fresh_q or reask_q):
        next_is_pinned = bool(fresh_q) and fresh_q[0].title_id in pinned
        take_reask = (
            bool(reask_q) and not next_is_pinned and (not fresh_q or rng.random() < rate)
        )
        if take_reask:
            candidate = reask_q.pop(0)
            if candidate.title_id in taken:
                continue
            card = QueueCard(
                title_id=candidate.title_id,
                # The same branch, the same sentence and the same probability as a genuinely
                # pending card. A re-ask target carries a verdict, and a verdict implies seen.
                reason=SEEN_REASON,
                p_seen=P_SEEN_RECORDED,
                source="reask",
                reask_of=candidate.verdict_id,
            )
        else:
            card = fresh_q.pop(0)
            if card.title_id in taken:
                continue
        taken.add(card.title_id)
        out.append(card)
    return out


# --- §13's instrument -------------------------------------------------------------------------


@dataclass(frozen=True)
class NotSeenRate:
    """§13: "not-seen rate in the rating queue (>50% = queue bug)"."""

    answered: int
    not_seen: int
    window: int

    @property
    def rate(self) -> float | None:
        return None if self.answered == 0 else self.not_seen / self.answered

    @property
    def queue_bug(self) -> bool:
        rate = self.rate
        return rate is not None and rate > NOT_SEEN_BUG_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "not_seen": self.not_seen,
            "window": self.window,
            "rate": self.rate,
            "queue_bug": self.queue_bug,
            "threshold": NOT_SEEN_BUG_THRESHOLD,
        }


async def not_seen_rate(
    conn: asyncpg.Connection, *, user_id: int | None = None, window: int = NOT_SEEN_WINDOW
) -> NotSeenRate:
    """How often the queue guessed wrong, over the last `window` answers it got.

    Read from the `rate_observation` journal rather than from `user_title`, because
    `user_title` holds one row per (user, title) and a later "seen" erases the "not seen" that
    is the whole measurement. The journal is append-only and an undone row is tombstoned, so
    `undone_at IS NULL` counts what the person actually left standing.
    """
    row = await conn.fetchrow(
        """
        SELECT count(*) AS answered,
               count(*) FILTER (WHERE kind_of = 'not_seen') AS not_seen
          FROM (SELECT kind_of
                  FROM rate_observation
                 WHERE ($1::bigint IS NULL OR user_id = $1)
                   AND undone_at IS NULL
                   AND kind_of IN ('verdict', 'not_seen')
                 ORDER BY id DESC
                 LIMIT $2) recent
        """,
        user_id,
        window,
    )
    return NotSeenRate(answered=int(row["answered"]), not_seen=int(row["not_seen"]), window=window)


__all__ = [
    "AGE_SATURATION_YEARS",
    "CROWD_SATURATION",
    "SOURCES",
    "WEIGHTS",
    "Features",
    "NotSeenRate",
    "QueueCard",
    "SeenWeights",
    "contributions",
    "dominant",
    "next_sweep_cards",
    "not_seen_rate",
    "p_seen",
    "reason_for",
    "unfamiliarity",
]
