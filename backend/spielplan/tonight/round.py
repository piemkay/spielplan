"""§6.2 step 4's adaptive round. Spec v2.1 §6.2 step 4 (rewritten, 54b/54c), §0 rows 4 and 6,
§13 stream (a), §14 risk 6; decision 154.

    "**4. The round (adaptive length).** Each participant answers this-or-that pairs of real
     candidates on their own device — 'Which one tonight?' `A` / `B` / `either` / `neither`. …
     Among candidates whose posterior interval still straddles the shortlist boundary, the
     round picks the pair whose answer would most reduce the number of titles still straddling
     it; ties are broken toward the pair spanning the widest DNA axis. … The round ends for a
     person when the shortlist boundary is resolved … subject to a hard cap of 20 pairs. From
     the sixth pair a persistent 'just pick for us' ends that person's round immediately."

Pure and seeded, exactly as `rank/queue.py` is: "over a long draw the shares are …" is a claim
about a distribution, and a distribution is measured by drawing from it many thousands of
times, which is not a thing to do through Postgres.

WHY SELECTION HERE DOES NOT CONTRADICT §0 ROW 6. The measured null — "for *profiles*, no
selection rule beats random (best +0.0013, CI spans 0)" — is about estimating a person's stable
taste, a global-ranking problem. This is best-arm identification inside a pool of tens, for a
person whose mood is not their profile: the regime the spec already concedes selection helps in
(§6.3's tier queue is boundary-targeted for exactly this reason). Different objective, same
conclusion — which is why `rate/battle.py` still draws uniformly and this module does not, and
why the two must not be merged.

THE MODEL, AND WHY IT IS THIS ONE. §6.2 names a "posterior interval" per candidate and never
says what distribution it is. This keeps a Gaussian per candidate on the tonight-score scale
and updates it with the standard two-player Gaussian win/loss update (the truncated-Gaussian
moment match behind TrueSkill). Three reasons rather than one: it is the same win/loss/tie
family §5.2's Davidson arm already uses, so the round and the Ledger do not disagree about what
a comparison means; the update is closed-form and microseconds, which §6's "<1.5 s per battle"
budget cares about; and it produces a *variance*, which is the thing "still straddles the
boundary" is a statement about — a point estimate has no interval to straddle with.

THE ANCHOR IS WHAT MAKES DECISION 154 A LIKELIHOOD. `either` must LIFT BOTH and `neither` must
LOWER BOTH — opposite signals, not two names for a shrug. A pairwise model cannot express that:
between two candidates, "both good" and "both bad" are the same tie. So both answers are scored
against a virtual opponent pinned at the pool's median tonight score: `either` is both
candidates beating it, `neither` is both losing to it. That turns "lifts both" into a term in
the same likelihood as A and B instead of an ad-hoc nudge with an invented magnitude — and it
is why `neither` "eliminates two candidates at once", which no A/B answer can do.

§13's GUARD, BINDING A STOPPING RULE. 54b: hold-out pairs are "used for neither selection nor
stopping". This module enforces it in one place — `replay` filters them out before a single
belief is updated — because a rule spread across three call sites is a rule with three ways to
forget it. Two consequences carried over verbatim from `rank/queue.py`:

  * **The hold-out arm never receives a fallback**, in either direction. An adaptive slot with
    no straddler left still reports itself adaptive; the tenth pair is a hold-out whether or not
    the round has anything to resolve. Its *rate* has to be independent of the model's own
    confidence, or the evaluation stream stops being independent of what it audits.
  * **An arm is reported as the arm that drew it.**
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# `tonight/` had no numpy until perf-01. CLAUDE.md's numpy-only contract is about
# `ledger/model.py` and points the other way: numpy is a declared dependency, imported by
# sixteen other modules under `backend/spielplan/`, and it is what makes 54c's search affordable
# at real pool size without changing the rule.
import numpy as np

# --- decision 154's four answers ----------------------------------------------------------

A = "A"
B = "B"
EITHER = "EITHER"
NEITHER = "NEITHER"
ANSWERS: tuple[str, ...] = (A, B, EITHER, NEITHER)

# The two values `session_answer.selection` admits (0013's CHECK). Spelled the way the column
# spells them — a second spelling is how an exclusion silently stops matching, which is the
# lesson 0005's `uniform_holdout` already carries.
SELECTION_ADAPTIVE = "adaptive"
SELECTION_HOLDOUT = "uniform_holdout"

# 54c's three ways a round ends. §14 risk 6 wants the rate of each, so they are distinguishable
# and exhaustive: an implementation that reported `cap` for a converged round would still end
# every round while making that rate unreadable.
CONVERGED = "converged"
CAP = "cap"
ESCAPE = "escape"
END_REASONS: tuple[str, ...] = (CONVERGED, CAP, ESCAPE)

# 54c's constants. The spec's own numbers, not tunables: they are not constants of the §5.2
# recipe, so §4.3's `ledger_hyperparams.json` is not where they belong — the same reasoning
# `rank/queue.py` applies to §6.3's 70/20/10 shares.
CAP_PAIRS = 20
ESCAPE_FROM_PAIR = 6
HOLDOUT_EVERY = 10

# 54d: "three finalists and a wildcard". The boundary the round exists to resolve is the cut
# between rank 3 and rank 4.
SHORTLIST_SIZE = 3

# The pairwise noise of a single answer, on the tonight-score scale. "Which one tonight?" is a
# noisier question than a considered verdict — the person is choosing a mood, not reporting a
# preference — so it is deliberately not small.
BETA = 0.5

# A guest has no Ledger, so their prior is flat and wide. 54c: they "start from the pool prior
# and [are] carried entirely by their answers … which naturally makes a guest's round a little
# longer". The factor is what makes "a little longer" true rather than asserted.
GUEST_VAR_FACTOR = 4.0


class EscapeTooEarly(Exception):
    """54c: the escape "ends that person's round immediately on what is known so far", and it
    must not be reachable before the round has learned anything. Refused, not ignored: a
    control that silently does nothing is worse than one that is not there."""


@dataclass(frozen=True)
class Belief:
    """One candidate's tonight score, as a posterior rather than a number.

    `mu` is the participant's Ledger score plus whatever this round's answers have moved it;
    `var` is what makes "still straddles the boundary" a question with an answer.
    """

    mu: float
    var: float


@dataclass(frozen=True)
class Pair:
    title_a: int
    title_b: int
    selection: str
    reason: str

    def public(self) -> dict[str, object]:
        """The pair as a participant's device sees it. `selection` travels because 54b/§13
        require the hold-out stream to be identifiable end to end — and never so a client can
        choose it: the server seals the arm, exactly as `api/rank.py` seals a queue pair."""
        return {
            "title_a": self.title_a,
            "title_b": self.title_b,
            "selection": self.selection,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Answered:
    """One `session_answer` row, as the replay needs it."""

    seq: int
    title_a: int
    title_b: int
    answer: str
    selection: str = SELECTION_ADAPTIVE


@dataclass(frozen=True)
class Round:
    """Everything a replay produces: the posterior, what is still unresolved, what to ask
    next, and whether to ask at all."""

    beliefs: dict[int, Belief]
    answered: int
    straddlers: frozenset[int]
    next_pair: Pair | None
    stop_reason: str | None


# --- the prior -----------------------------------------------------------------------------


def initial(
    pool_scores: Mapping[int, float], *, prior_var: float = 1.0, has_profile: bool = True
) -> dict[int, Belief]:
    """The posterior a round starts from.

    A member starts at their own §5.1 score — 54c: "their Ledger score for that title — their
    stable taste — plus a **mood tilt** learned from this round's answers".

    A guest starts from **the pool's own order**, wide. 54c: "a participant with no Ledger …
    starts from the pool prior and is carried entirely by their answers", and the coverage row
    says the same thing from the other side — "ranked by the candidate pool's own member-average
    order". The caller hands that average in as `pool_scores`; the only difference from a member
    is the variance, which is what "carried entirely by their answers" means and what makes a
    guest's round "a little longer".

    It is emphatically NOT the host's Ledger wearing the guest's name — the prototype's `const
    u = guest ? 'p' : who`, which is a privacy-shaped defect rather than "contributes no taste
    term". The pool average is nobody's scores.

    An earlier draft flattened it, and the review found what that costs: with every mean equal,
    the shortlist boundary equals that mean and EVERY candidate straddles it, so the selection
    rule searches all n(n−1)/2 pairs with an O(n) update inside each instead of the handful the
    boundary actually separates. A guest's evening got measurably slower than everybody else's
    for no information gained — and the ordering the row requires was not there either.
    """
    var = prior_var * (GUEST_VAR_FACTOR if not has_profile else 1.0)
    return {t: Belief(mu=float(s), var=var) for t, s in pool_scores.items()}


def anchor_of(beliefs: Mapping[int, Belief]) -> float:
    """The virtual opponent `either` and `neither` are scored against — the pool's median
    tonight score. The median rather than the mean because a pool with one runaway favourite
    should not drag the thing "both of these would do" is measured against."""
    if not beliefs:
        return 0.0
    mus = sorted(b.mu for b in beliefs.values())
    mid = len(mus) // 2
    return mus[mid] if len(mus) % 2 else (mus[mid - 1] + mus[mid]) / 2.0


# --- the boundary --------------------------------------------------------------------------


def boundary(beliefs: Mapping[int, Belief]) -> float | None:
    """"the shortlist boundary" — the cut between rank 3 and rank 4 (54d fixes the shortlist at
    three finalists).

    None when the pool is no larger than the shortlist: three candidates *are* the shortlist,
    so there is no boundary to resolve and nothing to ask about.
    """
    if len(beliefs) <= SHORTLIST_SIZE:
        return None
    mus = sorted((b.mu for b in beliefs.values()), reverse=True)
    return (mus[SHORTLIST_SIZE - 1] + mus[SHORTLIST_SIZE]) / 2.0


def straddles(belief: Belief, cut: float, *, z: float) -> bool:
    return abs(belief.mu - cut) < z * math.sqrt(max(belief.var, 0.0))


def straddlers(beliefs: Mapping[int, Belief], *, z: float) -> set[int]:
    """Who the round still cannot place either side of the cut.

    One predicate, used by both selection and stopping — the round stops exactly when it has
    nothing left to ask. `rank/board.py` and `rank/queue.py` share `straddles()` for the same
    reason: a badge threshold and a queue threshold that drift apart give you a title that is
    queue-eligible and unbadged (proposal 157).
    """
    cut = boundary(beliefs)
    if cut is None:
        return set()
    return {t for t, b in beliefs.items() if straddles(b, cut, z=z)}


# --- the update ----------------------------------------------------------------------------


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _v_w(t: float) -> tuple[float, float]:
    """The truncated-Gaussian moment-match pair. `v` is how far the mean moves per unit of
    standard deviation; `w = v(v + t)` is the fraction of variance the observation removes.

    The tail guard matters: at large negative `t` — a heavy favourite losing — `Phi` underflows
    and `v` becomes 0/0. The limit is `-t`, which is the correct answer (a shock moves the mean
    a lot) rather than a NaN that silently poisons the whole posterior.
    """
    denom = _Phi(t)
    v = -t if denom < 1e-12 else _phi(t) / denom
    w = v * (v + t)
    return v, min(max(w, 0.0), 1.0)


def _duel(winner: Belief, loser: Belief) -> tuple[Belief, Belief]:
    c2 = winner.var + loser.var + BETA * BETA
    c = math.sqrt(c2)
    t = (winner.mu - loser.mu) / c
    v, w = _v_w(t)
    return (
        Belief(mu=winner.mu + winner.var / c * v, var=winner.var * (1.0 - winner.var / c2 * w)),
        Belief(mu=loser.mu - loser.var / c * v, var=loser.var * (1.0 - loser.var / c2 * w)),
    )


def _against_anchor(belief: Belief, anchor: float, *, wins: bool) -> Belief:
    """One candidate against the virtual opponent. The anchor has no variance of its own and
    never updates: it is a reference level, not a competitor."""
    fixed = Belief(mu=anchor, var=0.0)
    if wins:
        moved, _ = _duel(belief, fixed)
    else:
        _, moved = _duel(fixed, belief)
    return moved


def update(
    beliefs: Mapping[int, Belief], *, title_a: int, title_b: int, answer: str, anchor: float
) -> dict[int, Belief]:
    """One answer, applied. Returns a new mapping; the input is not mutated.

    Decision 154, made arithmetic:

      * `A` / `B` — a beats b, or b beats a. The two posteriors **separate**.
      * `EITHER` — both beat the anchor. Both means rise, and neither moves relative to the
        other, because the answer says nothing about their order.
      * `NEITHER` — both lose to the anchor. Both means fall, which is the only answer that can
        push two candidates below the boundary at once.

    Titles the answer did not name are untouched: an answer about two titles is evidence about
    those two.
    """
    if answer not in ANSWERS:
        raise ValueError(f"{answer!r} is not one of {ANSWERS} (decision 154)")
    out = dict(beliefs)
    a, b = out[title_a], out[title_b]
    if answer == A:
        out[title_a], out[title_b] = _duel(a, b)
    elif answer == B:
        out[title_b], out[title_a] = _duel(b, a)
    else:
        wins = answer == EITHER
        out[title_a] = _against_anchor(a, anchor, wins=wins)
        out[title_b] = _against_anchor(b, anchor, wins=wins)
    return out


# --- stopping, the cap, and the escape -----------------------------------------------------


def escape_available(answered: int) -> bool:
    """54c: "From the sixth pair a persistent 'just pick for us'…". A participant answering
    their Nth pair has N-1 behind them, so the control appears once five are answered."""
    return answered >= ESCAPE_FROM_PAIR - 1


def escape(*, answered: int) -> str:
    if not escape_available(answered):
        raise EscapeTooEarly(
            f"the escape opens at pair {ESCAPE_FROM_PAIR}; {answered} pairs answered"
        )
    return ESCAPE


def stop_reason(
    beliefs: Mapping[int, Belief], *, answered: int, z: float
) -> str | None:
    """Why this participant's round is over, or None while it runs.

    Convergence is checked first. §14 risk 6 wants the rate at which the cap fires, and a round
    that reported `cap` for a board it had actually resolved would still end — while making
    that rate say the opposite of the truth.
    """
    if not straddlers(beliefs, z=z):
        return CONVERGED
    if answered >= CAP_PAIRS:
        return CAP
    return None


# --- selection -----------------------------------------------------------------------------


def is_holdout(seq: int) -> bool:
    """54b: "Every tenth pair is the uniform-random hold-out". `seq` is 1-based — §6.7's own
    log line reads `session_answer(p, pair 4)`."""
    return seq % HOLDOUT_EVERY == 0


def _axis_span(axes: Mapping[int, Mapping[str, float]] | None, a: int, b: int) -> float:
    """How far apart two candidates sit on the widest DNA axis they both carry.

    54c: "ties are broken toward the pair spanning the widest DNA axis, because a pair of
    near-identical titles teaches nothing about the tilt." The axes are §6.4's shipped,
    authored artifact (`dna_axis` / `dna_axis_weight`), so this is a lookup and not a model.
    """
    if not axes:
        return 0.0
    va, vb = axes.get(a, {}), axes.get(b, {})
    shared = set(va) & set(vb)
    if not shared:
        return 0.0
    return max(abs(va[f] - vb[f]) for f in shared)


def _answer_probabilities(a: Belief, b: Belief, anchor: float) -> dict[str, float]:
    """What the model expects a participant to answer, given where it currently is.

    A and B split the ordering mass by the pairwise Gaussian. The remaining mass is the
    "these two are the same to me tonight" mass, and decision 154 splits it by *level*: a pair
    the model already places above the anchor is more likely to draw `either`, one below it
    `neither`. Both halves are needed — an expectation that ignored `neither` would rate a pair
    of weak candidates as uninformative, when it is the pair one answer can eliminate outright.
    """
    c = math.sqrt(a.var + b.var + BETA * BETA)
    p_a = _Phi((a.mu - b.mu) / c)
    # The mass the model reserves for "both", shrinking as the pair separates: a pair the model
    # is sure about is a pair it does not expect a level answer to.
    both = math.exp(-abs(a.mu - b.mu) / max(c, 1e-9))
    level = (a.mu + b.mu) / 2.0 - anchor
    p_either = both * _Phi(level / max(c, 1e-9))
    p_neither = both * (1.0 - _Phi(level / max(c, 1e-9)))
    p_a_only = (1.0 - both) * p_a
    p_b_only = (1.0 - both) * (1.0 - p_a)
    total = p_a_only + p_b_only + p_either + p_neither
    if total <= 0:
        return {A: 0.25, B: 0.25, EITHER: 0.25, NEITHER: 0.25}
    return {
        A: p_a_only / total, B: p_b_only / total,
        EITHER: p_either / total, NEITHER: p_neither / total,
    }


def expected_straddlers(
    beliefs: Mapping[int, Belief], *, title_a: int, title_b: int, anchor: float, z: float
) -> float:
    """How many candidates the round expects to still be unplaced after asking this pair.

    54c's selection rule verbatim: "the pair whose answer would most reduce the number of
    titles still straddling it". Expectation over the four answers, weighted by how likely the
    model thinks each is — because a pair whose answer is a foregone conclusion reduces nothing
    however dramatic the hypothetical would be.
    """
    probs = _answer_probabilities(beliefs[title_a], beliefs[title_b], anchor)
    total = 0.0
    for answer, p in probs.items():
        if p <= 0.0:
            continue
        after = update(beliefs, title_a=title_a, title_b=title_b, answer=answer, anchor=anchor)
        total += p * len(straddlers(after, z=z))
    return total


def _holdout(pool: Sequence[int], rng: random.Random) -> Pair | None:
    """54b: "drawn uniformly at random from the candidate pool".

    Uniform over unordered **pairs of the whole pool** — not over the straddling set, because
    a sample restricted to what the model is unsure about is exactly the adaptive selection the
    guard exists to prevent. Drawing an ordered pair and forgetting the order is uniform over
    unordered pairs, which is why it is done that way rather than by materialising n(n-1)/2.
    """
    n = len(pool)
    if n < 2:
        return None
    i = rng.randrange(n)
    j = rng.randrange(n - 1)
    if j >= i:
        j += 1
    return Pair(
        title_a=pool[i], title_b=pool[j],
        selection=SELECTION_HOLDOUT,
        reason="uniform-random, held out — this pair never tunes tonight's shortlist",
    )

# --- selection: 54c's expectation, evaluated for every pair at once -------------------------
#
# perf-01. `expected_straddlers` above is the rule written out, and it is unaffordable at real
# pool size: `update` copies the whole belief dict and `straddlers` rescans and re-sorts it,
# four times per pair, and `select` calls it for every unordered pair of the straddling set. On
# the shipped 696-title owned pool — 344 straddlers at `prior_var = 1.0` and a measured
# `user_score` sd of 0.504 — that is 59k pairs and 36 s; a guest seat's 660 straddlers cost
# 126 s. The §6 preamble budgets 1.5 s per battle.
#
# 54c's SENTENCE IS UNTOUCHED. The pair this returns is the pair the scalar returns, and the
# equivalence is asserted rather than argued
# (`test_the_fast_pair_evaluator_reproduces_the_scalar_expectation_exactly` and
# `test_the_fast_pair_search_serves_the_pair_the_scalar_argmin_serves`). Nothing here bounds the
# search to the K straddlers nearest the cut: that changes which pair the rule picks, and
# decision 168 rules it out on measurement — at a tighter score spread K = 24 diverges on a
# quarter of fresh boards, and its safety is a property of a distribution that moves as a
# household accumulates verdicts.
#
# THE IDENTITY IT RESTS ON is `update`'s own last paragraph: "Titles the answer did not name are
# untouched". After any of the four answers the belief set differs in exactly two entries, so
# both quantities the count needs are local:
#
#   * the cut is the midpoint of the 3rd and 4th largest mu, and the only mus that can enter or
#     leave the top four are the two that moved — so the top six of the unchanged board, minus
#     the two moved rows, plus the two new mus, always contains it;
#   * `straddlers` counts `|mu - cut| < z*sigma`, which is `mu - d < cut < mu + d` for the reach
#     `d = z*sigma` — two `searchsorted` probes into the sorted `mu - d` and `mu + d` arrays,
#     with the two moved titles corrected by hand using the scalar predicate itself.
#
# `either` and `neither` move each named title against the anchor and against nothing else, so
# those two answers are precomputed once per title rather than twice per pair; `A` and `B` share
# one `c` and one `t = (mu_a - mu_b)/c`, and `_answer_probabilities`' own `p_a` is `Phi(t)`, the
# same value `_v_w` needs. That leaves three `erf` and three `exp` per pair.
#
# WHERE THE TWO CAN DISAGREE, named rather than hidden. The bulk probe rewrites `|mu - cut| < d`
# as `mu - d < cut < mu + d`, the same inequality in exact arithmetic but not in float64, so an
# UNMOVED title whose interval endpoint lands within an ulp of the cut can be counted differently
# by the two. That residue is bounded at one title per such row and can never go negative,
# because every row is in exactly one of three sets and each contributes 0 or 1:
#
#   * `can` — `mu - d` sorts strictly below `mu + d`, so the two searchsorted probes give
#     `[lo < cut] - [hi <= cut]`, which is 0 or 1 and is 0 whenever the interval is wholly below
#     the cut. This is the set the residue lives in, and it is reachable only when `d` is within
#     an ulp of `mu`'s own scale: at `mu` 1.0 and `d` between 2^-54 and 2^-53 the cut can sit ON
#     the mean with `fl(mu + d)` still rounding back to it. That is a variance around 1e-32, and
#     `initial`/`update` cannot produce one — `_duel` shrinks the variance like 0.25/k and the
#     prior starts at 1.0 — so the corner is named here rather than guarded against;
#   * `pin` — `d` is positive but under half an ulp of `mu`, so both endpoints round to `mu` and
#     the rewrite would subtract one for a row `straddles()` counts. Those are held separately
#     and matched by equality against the cut, which is exactly when `|mu - cut| < d` holds;
#   * the rest — `d` is not positive, `|mu - cut| < d` is false for every cut, and they appear
#     in no array. That is also why a non-positive `z` needs no special case.
#
# THE TWO MOVED TITLES ARE TAKEN OUT THE WAY THEY WERE PUT IN. `base` counts the whole board
# under the rewrite, including `a` and `b` under their OLD intervals, so `_count` removes them
# under the rewrite too and re-adds them under their new beliefs with `straddles()`' own
# expression. Using the exact predicate for the removal was a real defect and not a rounding
# nicety: on a board of eight where one title's `mu - z*sigma` is bit-for-bit the moved cut, five
# of twenty-eight pairs diverged by up to 0.22 straddlers and `select` served a different pair.
# Measured over random pools the surviving residue does not appear at all (max difference 0.0
# over 12 pools x 200 pairs, decision 168; 0 over the shapes the equivalence test sweeps), which
# is why that test asserts equality and not a tolerance.

_ERF = np.frompyfunc(math.erf, 1, 1)
_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)

# Pairs are evaluated in blocks, so peak memory is a property of this number and not of the
# pool: a guest board of 716 candidates is 174k pairs, and the per-answer working set (a
# (pairs x 8) mu array plus some thirty vectors) is tens of megabytes unchunked and grows
# linearly with a library that grows. §1 specifies a 4 vCPU box. This is NOT the new constant
# decision 168 forbids — that ruling is about K, a number that would change which pair 54c's
# rule picks. This one changes nothing a caller can observe, and the equivalence test is what
# says so rather than this comment.
_PAIR_BLOCK = 1 << 15


def _phi_many(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / _SQRT2PI


def _Phi_many(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _ERF(x / _SQRT2).astype(np.float64))


def _v_w_many(t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """`_v_w`, elementwise. The tail guard is the same one and for the same reason: at large
    negative `t` the denominator underflows and the ratio is not a number, and the limit `-t` is
    the correct answer rather than a NaN that poisons the posterior."""
    denom = _Phi_many(t)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = _phi_many(t) / denom
    v = np.where(denom < 1e-12, -t, ratio)
    w = v * (v + t)
    return v, np.clip(w, 0.0, 1.0)


@dataclass(frozen=True, eq=False)
class _Board:
    """One `select` call's beliefs, in the shape the pair search reads them.

    Built once per call. `ids` is the pool in ascending title order, so a row index and a title
    id sort the same way and the `(a, b)` half of 54c's tie-break needs no remapping.
    """

    ids: np.ndarray             # int64[n], ascending title ids
    row: dict[int, int]
    mu: np.ndarray              # float64[n]
    var: np.ndarray             # float64[n]
    reach: np.ndarray           # float64[n] — z*sigma, the scalar's own straddle expression
    lo_row: np.ndarray          # float64[n] — mu-reach, in row order
    hi_row: np.ndarray          # float64[n] — mu+reach, in row order
    can: np.ndarray             # bool[n] — the rows whose interval survives rounding
    lo: np.ndarray              # sorted lo_row over `can`
    hi: np.ndarray              # sorted hi_row over `can`
    pin: np.ndarray             # sorted mu over the rows whose interval collapsed to a point
    top: np.ndarray             # the (up to six) rows with the largest mu
    top_mu: np.ndarray
    either_mu: np.ndarray
    either_var: np.ndarray
    neither_mu: np.ndarray
    neither_var: np.ndarray
    anchor: float
    z: float
    live: bool                  # False when the pool is no larger than the shortlist

    @property
    def n(self) -> int:
        return int(self.ids.size)

    @classmethod
    def of(cls, beliefs: Mapping[int, Belief], pool: Sequence[int], *, anchor: float, z: float) -> _Board:
        n = len(pool)
        ids = np.asarray(pool, dtype=np.int64)
        mu = np.fromiter((beliefs[t].mu for t in pool), dtype=np.float64, count=n)
        var = np.fromiter((beliefs[t].var for t in pool), dtype=np.float64, count=n)
        reach = z * np.sqrt(np.maximum(var, 0.0))
        lo_row, hi_row = mu - reach, mu + reach
        # Three sets, so that every row contributes 0 or 1 and the rewrite can never subtract.
        # `can` is where the interval survives rounding; `pin` is a positive reach under half an
        # ulp of `mu`, where both endpoints round back to `mu` and only equality with the cut can
        # satisfy `|mu - cut| < reach`; everything else has no reach and never straddles.
        can = lo_row < hi_row
        pin = (reach > 0.0) & ~can
        top = np.argsort(-mu, kind="stable")[: min(n, SHORTLIST_SIZE + 3)]

        # `either`/`neither` against the anchor: the moved belief depends on the title alone.
        c2 = var + BETA * BETA
        c = np.sqrt(c2)
        v_up, w_up = _v_w_many((mu - anchor) / c)
        v_dn, w_dn = _v_w_many((anchor - mu) / c)
        return cls(
            ids=ids,
            row={int(t): i for i, t in enumerate(pool)},
            mu=mu,
            var=var,
            reach=reach,
            lo_row=lo_row,
            hi_row=hi_row,
            can=can,
            lo=np.sort(lo_row[can]),
            hi=np.sort(hi_row[can]),
            pin=np.sort(mu[pin]),
            top=top,
            top_mu=mu[top],
            either_mu=mu + var / c * v_up,
            either_var=var * (1.0 - var / c2 * w_up),
            neither_mu=mu - var / c * v_dn,
            neither_var=var * (1.0 - var / c2 * w_dn),
            anchor=anchor,
            z=z,
            live=n > SHORTLIST_SIZE,
        )

    def _cut(self, ia: np.ndarray, ib: np.ndarray, mu_a: np.ndarray, mu_b: np.ndarray) -> np.ndarray:
        """`boundary()` after the two substitutions: the midpoint of the 3rd and 4th largest mu.

        The candidate set is the top six rows with the two moved ones struck out, plus the two
        new mus. At most two of the six are struck, so at least four survive, and every one of
        them is at least as large as anything outside the top six — so the four largest of the
        moved board are always in here, and rows that are not cannot reach position three.
        """
        k = self.top.size
        cand = np.empty((ia.size, k + 2), dtype=np.float64)
        struck = (self.top[None, :] == ia[:, None]) | (self.top[None, :] == ib[:, None])
        cand[:, :k] = np.where(struck, -np.inf, self.top_mu[None, :])
        cand[:, k] = mu_a
        cand[:, k + 1] = mu_b
        cand.sort(axis=1)
        return (cand[:, k - 1] + cand[:, k - 2]) / 2.0

    def _count(
        self, ia: np.ndarray, ib: np.ndarray,
        mu_a: np.ndarray, var_a: np.ndarray, mu_b: np.ndarray, var_b: np.ndarray,
    ) -> np.ndarray:
        """`len(straddlers(after))` for one answer, over every pair at once."""
        cut = self._cut(ia, ib, mu_a, mu_b)
        # The whole board by interval, `#(lo < cut) - #(hi <= cut)`, plus the pinned rows, which
        # straddle exactly when the cut IS their mean.
        base = (
            np.searchsorted(self.lo, cut, side="left")
            - np.searchsorted(self.hi, cut, side="right")
        )
        if self.pin.size:
            base = base + (
                np.searchsorted(self.pin, cut, side="right")
                - np.searchsorted(self.pin, cut, side="left")
            )
        # `a` and `b` are in that total under their OLD intervals, so they come out the way they
        # went in — the rewrite, not `straddles()`. Removing them with the exact predicate is
        # what let the two disagree by a whole straddler rather than by a rounding residue.
        was = self._contributes(ia, cut) + self._contributes(ib, cut)
        # Re-added under the new beliefs with `straddles()`' own expression, because that is what
        # `straddlers(after)` will compute for them and there is no precomputed interval to reuse.
        now = (np.abs(mu_a - cut) < self.z * np.sqrt(np.maximum(var_a, 0.0))).astype(np.int64)
        now += (np.abs(mu_b - cut) < self.z * np.sqrt(np.maximum(var_b, 0.0))).astype(np.int64)
        return base - was + now

    def _contributes(self, rows: np.ndarray, cut: np.ndarray) -> np.ndarray:
        """What `base` counted for these rows, in `base`'s own arithmetic."""
        can = self.can[rows]
        interval = can & (self.lo_row[rows] < cut) & (self.hi_row[rows] > cut)
        pinned = ~can & (self.reach[rows] > 0.0) & (self.mu[rows] == cut)
        return (interval | pinned).astype(np.int64)

    def _block(self, ia: np.ndarray, ib: np.ndarray) -> np.ndarray:
        mu, var = self.mu, self.var
        ma, mb, va, vb = mu[ia], mu[ib], var[ia], var[ib]

        # `_duel`, both ways round. `b.var + a.var` is `a.var + b.var` to the bit, and IEEE
        # subtraction makes `(mb - ma)` the negation of `(ma - mb)` — exactly, except that both
        # are `+0.0` when the two means are equal, where `-t` is `-0.0` instead. `_v_w` reads it
        # only through `exp(-x*x/2)` and `erf(x)`, both of which give the same double for the two
        # zeros, so one `c` and one `t` serve `A`, `B` and `_answer_probabilities`' `p_a` alike.
        c2 = va + vb + BETA * BETA
        c = np.sqrt(c2)
        t = (ma - mb) / c
        v_p, w_p = _v_w_many(t)
        v_m, w_m = _v_w_many(-t)

        # `_answer_probabilities`, verbatim and in its own order.
        p_a = _Phi_many(t)
        cg = np.maximum(c, 1e-9)
        both = np.exp(-np.abs(ma - mb) / cg)
        level = (ma + mb) / 2.0 - self.anchor
        phi_level = _Phi_many(level / cg)
        p_either = both * phi_level
        p_neither = both * (1.0 - phi_level)
        p_a_only = (1.0 - both) * p_a
        p_b_only = (1.0 - both) * (1.0 - p_a)
        total = p_a_only + p_b_only + p_either + p_neither
        with np.errstate(divide="ignore", invalid="ignore"):
            probs = (
                np.where(total <= 0.0, 0.25, p_a_only / total),
                np.where(total <= 0.0, 0.25, p_b_only / total),
                np.where(total <= 0.0, 0.25, p_either / total),
                np.where(total <= 0.0, 0.25, p_neither / total),
            )

        moved = (
            (ma + va / c * v_p, va * (1.0 - va / c2 * w_p),
             mb - vb / c * v_p, vb * (1.0 - vb / c2 * w_p)),
            (ma - va / c * v_m, va * (1.0 - va / c2 * w_m),
             mb + vb / c * v_m, vb * (1.0 - vb / c2 * w_m)),
            (self.either_mu[ia], self.either_var[ia], self.either_mu[ib], self.either_var[ib]),
            (self.neither_mu[ia], self.neither_var[ia], self.neither_mu[ib], self.neither_var[ib]),
        )

        # Summed in `probs`' own order and skipping the same terms, because the scalar's
        # accumulator is a Python float and float addition is not associative.
        out = np.zeros(ia.size, dtype=np.float64)
        for p, (mu_a, var_a, mu_b, var_b) in zip(probs, moved, strict=True):
            count = self._count(ia, ib, mu_a, var_a, mu_b, var_b)
            out = out + np.where(p <= 0.0, 0.0, p * count)
        return out

    def expected(self, ia: np.ndarray, ib: np.ndarray) -> np.ndarray:
        """54c's expectation for every `(ia, ib)` pair of rows, as `expected_straddlers` gives it.

        A pool no larger than the shortlist has no boundary to resolve, so every pair leaves
        nothing straddling and the scalar returns 0.0 for all of them.
        """
        if not self.live:
            return np.zeros(ia.size, dtype=np.float64)
        if ia.size <= _PAIR_BLOCK:
            return self._block(ia, ib)
        out = np.empty(ia.size, dtype=np.float64)
        for start in range(0, ia.size, _PAIR_BLOCK):
            end = start + _PAIR_BLOCK
            out[start:end] = self._block(ia[start:end], ib[start:end])
        return out


def select(
    beliefs: Mapping[int, Belief],
    *,
    seq: int,
    rng: random.Random,
    z: float,
    axes: Mapping[int, Mapping[str, float]] | None = None,
    asked: Iterable[frozenset[int]] | None = None,
) -> Pair | None:
    """The next pair, and the arm that produced it.

    Every tenth pair is the hold-out and is chosen by none of the adaptive machinery. Nothing
    ever falls into or out of that arm: an adaptive slot with no straddler left still reports
    itself adaptive, because the hold-out's *rate* is the one thing §13 needs to be independent
    of the model's own confidence.

    `asked` is what the participant has already answered. M3-open-points §3.1 is the reason it
    exists: the Rank queue's boundary arm consults nothing, so it re-serves about five distinct
    pairs forever, and ten repeats of one judgement shrink that pair's posterior by √10 on the
    strength of one answer — the reliability inflation §13 guards against, arriving by a
    different door. The round does not repeat it.

    The search itself is `_Board`'s, not `expected_straddlers`', and the two agree to the bit;
    the scalar stays above as the reference the equivalence test measures against. Nothing about
    which pair 54c's rule names changes here — only what it costs to find it.
    """
    pool = sorted(beliefs)
    if len(pool) < 2:
        return None
    if is_holdout(seq):
        return _holdout(pool, rng)

    already = set(asked or ())
    anchor = anchor_of(beliefs)
    board = _Board.of(beliefs, pool, anchor=anchor, z=z)
    n = board.n
    unresolved = np.asarray(
        [board.row[t] for t in sorted(straddlers(beliefs, z=z))], dtype=np.int64
    )
    # A pair the participant has already answered, as the one integer `i*n + j` identifies it.
    # A stored answer can name a title the pool no longer holds (§10: a re-import moves the
    # pool under it), and such a pair blocks nothing because it can never be generated.
    blocked = np.asarray(
        [
            board.row[min(p)] * n + board.row[max(p)]
            for p in already
            if len(p) == 2 and min(p) in board.row and max(p) in board.row
        ],
        dtype=np.int64,
    )

    def _best(pairs: tuple[np.ndarray, np.ndarray]) -> tuple[float, float, int, int] | None:
        ia, ib = pairs
        if ia.size and blocked.size:
            keep = ~np.isin(ia * n + ib, blocked)
            ia, ib = ia[keep], ib[keep]
        if not ia.size:
            return None
        expected = board.expected(ia, ib)
        # Rounded before comparison so "as informative as each other" is a real tie the axis
        # rule can break, rather than a floating-point accident that never occurs. `np.round`
        # is a multiply-rint-divide and Python's `round` is correctly rounded, so the vectorised
        # pass only narrows the field — generously — and the tie itself is decided with the
        # scalar's own `round` over the handful of distinct values that survive.
        approx = np.round(expected, 9)
        near = np.flatnonzero(approx <= approx.min() + 1.5e-9)
        uniq, back = np.unique(expected[near], return_inverse=True)
        rounded = np.asarray([round(float(v), 9) for v in uniq])[back]
        best = rounded.min()
        tie = near[rounded == best]
        a_ids, b_ids = board.ids[ia[tie]], board.ids[ib[tie]]
        if axes:
            k = min(
                range(tie.size),
                key=lambda i: (-_axis_span(axes, int(a_ids[i]), int(b_ids[i])),
                               int(a_ids[i]), int(b_ids[i])),
            )
        else:
            # With no axes the span is 0.0 for every pair, so the key is (expected, a, b) and
            # the winner is the lexicographically smallest pair — `_axis_span`'s own early
            # return, without n dictionary lookups to reach it.
            k = int(np.lexsort((b_ids, a_ids))[0])
        a, b = int(a_ids[k]), int(b_ids[k])
        return (float(best), -_axis_span(axes, a, b), a, b)

    def _within(xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        i, j = np.triu_indices(xs.size, 1)
        return xs[i], xs[j]

    def _touching(xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        left = np.repeat(xs, n)
        right = np.tile(np.arange(n, dtype=np.int64), xs.size)
        keep = left != right
        left, right = left[keep], right[keep]
        # Two straddlers name each other twice; the scalar evaluates the repeat and keeps the
        # same winner, so collapsing it here changes the cost and not the answer.
        key = np.unique(np.minimum(left, right) * n + np.maximum(left, right))
        return key // n, key % n

    # Three searches, narrowest first, and the order is 54c's sentence: "Among candidates whose
    # posterior interval still straddles the shortlist boundary, the round picks the pair whose
    # answer would most reduce the number of titles still straddling it."
    #
    #   1. Between straddlers, which is the sentence as written.
    #   2. A straddler against anyone, once every pair *between* straddlers has been answered.
    #      Still a question about the unresolved candidates, which is what the round is for.
    #   3. Anything left, when there is no straddler at all — the case this fallback already
    #      covered.
    #
    # Only the first two existed, and the second only fired when there were fewer than two
    # straddlers. With exactly two and their one pair already answered nothing matched, and
    # `replay` reads a None pair as the exhaustion ending: a forty-title pool ended, recorded as
    # `cap`, at pair fourteen — six pairs of the person's budget unspent, two candidates still
    # unplaced, and §14 risk 6's rate of each ending calling that the cap.
    #
    # Widening straight to the whole pool would have been the smaller diff and the wrong one:
    # with nothing able to reduce the straddler count, every pair ties on information and the
    # argmin falls through to the lowest title ids — two well-placed candidates, asked about
    # for no reason, while the two the round exists to separate go unmentioned.
    best = _best(_within(unresolved)) if unresolved.size >= 2 else None
    if best is None and unresolved.size:
        best = _best(_touching(unresolved))
    if best is None:
        best = _best(_within(np.arange(n, dtype=np.int64)))
    if best is None:
        return None
    _, _, a, b = best
    return Pair(
        title_a=a, title_b=b,
        selection=SELECTION_ADAPTIVE,
        reason="the pair that would settle the most of tonight's shortlist",
    )


# --- replay --------------------------------------------------------------------------------


def replay(
    pool_scores: Mapping[int, float],
    answers: Sequence[Answered],
    *,
    z: float,
    prior_var: float = 1.0,
    has_profile: bool = True,
    axes: Mapping[int, Mapping[str, float]] | None = None,
    rng: random.Random | None = None,
    escaped: bool = False,
) -> Round:
    """The whole round, from the pool and the rows.

    THE ONE PLACE §13's GUARD IS ENFORCED. Hold-out answers are filtered out here, before a
    single belief moves, so selection and stopping cannot read them by any route. A rule spread
    across three call sites is a rule with three ways to forget it — which is exactly what M3
    found when `duel.selection` turned out to have four read paths rather than one.

    They still count toward the cap: a hold-out is a pair the person actually answered, so it
    costs them one of their twenty. It is excluded from the model, not from their evening.
    """
    beliefs = initial(pool_scores, prior_var=prior_var, has_profile=has_profile)
    for answered_row in sorted(answers, key=lambda x: x.seq):
        if answered_row.selection == SELECTION_HOLDOUT:
            continue
        if answered_row.title_a not in beliefs or answered_row.title_b not in beliefs:
            # §10: a re-import can change the pool under a stored answer. Skipping is the only
            # honest option — the alternative is inventing a belief for a title that is no
            # longer a candidate.
            continue
        beliefs = update(
            beliefs,
            title_a=answered_row.title_a,
            title_b=answered_row.title_b,
            answer=answered_row.answer,
            anchor=anchor_of(beliefs),
        )

    count = len(answers)
    reason = ESCAPE if escaped else stop_reason(beliefs, answered=count, z=z)
    unresolved = frozenset(straddlers(beliefs, z=z))
    # Live ADAPTIVE answers only. A hold-out pair suppressed from the adaptive arm is a
    # hold-out steering selection, which is exactly what 54b forbids — 'used for neither
    # selection nor stopping' is a statement about the whole selector, not only about the
    # posterior. The review found it here rather than in the update.
    asked = {
        frozenset({x.title_a, x.title_b})
        for x in answers
        if x.selection != SELECTION_HOLDOUT
    }
    nxt = (
        None
        if reason
        else select(beliefs, seq=count + 1, rng=rng or random.Random(0), z=z, axes=axes,
                    asked=asked)
    )
    if reason is None and nxt is None:
        # §6.2 describes the happy path and never a pool small enough to run out of distinct
        # pairs — but a household library can be, and a round with nothing left to ask and no
        # way to end is a deadlock. It is the same terminal state as the cap (the round ended
        # without resolving the boundary), and 54g fixes `ended_by` at three values, so `cap`
        # is what it is recorded as rather than a fourth value nobody defined. Reported as a
        # v2.2 spec defect.
        reason = CAP
    return Round(
        beliefs=beliefs, answered=count, straddlers=unresolved,
        next_pair=nxt, stop_reason=reason,
    )


__all__ = [
    "A",
    "ANSWERS",
    "B",
    "Answered",
    "BETA",
    "Belief",
    "CAP",
    "CAP_PAIRS",
    "CONVERGED",
    "END_REASONS",
    "ESCAPE",
    "ESCAPE_FROM_PAIR",
    "EITHER",
    "EscapeTooEarly",
    "GUEST_VAR_FACTOR",
    "HOLDOUT_EVERY",
    "NEITHER",
    "Pair",
    "Round",
    "SELECTION_ADAPTIVE",
    "SELECTION_HOLDOUT",
    "SHORTLIST_SIZE",
    "anchor_of",
    "boundary",
    "escape",
    "escape_available",
    "expected_straddlers",
    "initial",
    "is_holdout",
    "replay",
    "select",
    "stop_reason",
    "straddles",
    "straddlers",
    "update",
]
