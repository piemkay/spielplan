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

    A guest starts **flat**: same mean for every candidate, wider variance. That is 54c's "a
    participant with no Ledger … starts from the pool prior and is carried entirely by their
    answers", and the flatness is the whole point. The prototype's bug was literal substitution
    — `const u = guest ? 'p' : who`, the host's Ledger wearing the guest's name — which is a
    privacy-shaped defect rather than "contributes no taste term". What a guest is *shown* is
    still the pool's own order, because that is the order the pairs are drawn from; what they
    are *scored* by starts at nothing.
    """
    if not has_profile:
        flat = sum(pool_scores.values()) / len(pool_scores) if pool_scores else 0.0
        return {
            t: Belief(mu=flat, var=prior_var * GUEST_VAR_FACTOR) for t in pool_scores
        }
    return {t: Belief(mu=float(s), var=prior_var) for t, s in pool_scores.items()}


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
    """
    pool = sorted(beliefs)
    if len(pool) < 2:
        return None
    if is_holdout(seq):
        return _holdout(pool, rng)

    already = set(asked or ())
    anchor = anchor_of(beliefs)
    unresolved = sorted(straddlers(beliefs, z=z))
    # An adaptive slot with nothing left to resolve still has to serve something if the round
    # is being asked for a pair — but it says `adaptive`, because that is the arm that drew it.
    candidates = unresolved if len(unresolved) >= 2 else pool

    best: tuple[float, float, int, int] | None = None
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            if frozenset({a, b}) in already:
                continue
            expected = expected_straddlers(
                beliefs, title_a=a, title_b=b, anchor=anchor, z=z
            )
            # Rounded before comparison so "as informative as each other" is a real tie the
            # axis rule can break, rather than a floating-point accident that never occurs.
            key = (round(expected, 9), -_axis_span(axes, a, b), a, b)
            if best is None or key < best:
                best = key
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
    asked = {frozenset({x.title_a, x.title_b}) for x in answers}
    nxt = (
        None
        if reason
        else select(beliefs, seq=count + 1, rng=rng or random.Random(0), z=z, axes=axes,
                    asked=asked)
    )
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
