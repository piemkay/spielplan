"""The mood tilt. Spec v2.1 §6.2 steps 4-5, §0 row 4, §4.2 `session_participant.tilt`.

    "Each vote also yields a tilt observation — chosen-minus-rejected DNA, **centred on the
     candidate-pool mean** (the measured centring lever)."

§0 row 4 is the whole reason this module is three functions rather than none: the stored 8-axis
mood profile is worth **0.000** for choose-tonight, while **3 shortlist-anchored answers ≈
+0.088 AUC**, and "centring on the shortlist matters more than question form". The centring is
the lever; the questionnaire around it was deleted.

WHY "CENTRED" HAD TO BECOME "STANDARDISED", AND WHY THAT IS NOT A LIBERTY. For a difference,
additive centring cancels exactly:

    (a − m) − (b − m) = a − b

so "chosen-minus-rejected, centred on the pool mean", read as subtraction alone, is a no-op on
every A/B answer — the pool cannot reach the tilt at all, and the thing §0 measured at +0.088
would be indistinguishable from the thing it measured at 0.000. The coverage row states the
requirement the spec's wording drops: "the identical answer on the identical pair produces a
different tilt when the surrounding pool differs". Centring on the pool's mean **and scaling by
the pool's own spread** is the reading under which every word of §6.2 holds and that requirement
is met: the same choice between the same two films is a stronger statement in a pool where they
are the extremes than in one where they are unremarkable. Filed as a v2.2 spec defect.

§4.1 RULES 1 AND 2. This module never reads the database; it is handed DNA vectors that the
caller has already read through the sanctioned `dna_tagged` view, carrying their tier. Salience
and confidence reach it only as *weights* inside those vectors — there is no predicate here,
and no threshold, so a 0.5 cut that would delete 44% of the extracted tier is unrepresentable
rather than merely discouraged.

THE SHAPE IS jsonb. §4.2 stores the accumulated tilt on `session_participant.tilt`, so
everything here is plain dicts of `str -> float` and never a numpy array.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

# Decision 154's four answers live in `round`, which imports nothing from this package (stdlib
# and numpy only), so this is a one-way edge and not a cycle. The alternative was a second
# spelling of "A"/"B"/"EITHER" here, and a second spelling is how a branch silently stops
# matching — the lesson migration 0005's own selection constants already carry. Cited by number
# and never quoted: `test_rank_integration.py`'s guard reads this package for every spelling of
# §13's held-out stream, and its allow-list holds deliberate READ paths. This module has none —
# 54b's exclusion is `play.py`'s and `solo.py`'s, on the rows — and the tilt is precisely the
# surface a held-out answer must never reach, so an entry here would pre-authorise the one leak
# that would matter, to buy a comment one word it can do without.
from spielplan.tonight import round as round_rules

# A facet the pool does not vary on carries no information about tonight, and dividing by its
# spread would be an infinity rather than an insight. The floor is not a tuned constant: it is
# the point below which a spread is indistinguishable from zero in float arithmetic.
MIN_SPREAD = 1e-9

Vector = Mapping[str, float]


@dataclass(frozen=True)
class Frame:
    """The pool's own frame of reference: what an average candidate looks like tonight, and how
    much tonight's candidates differ from each other.

    Built once per session from the candidate pool and carried, because §6.2 step 6 says
    nothing re-ranks within the evening — a frame that drifted as answers arrived would make
    the same answer mean different things at pair 3 and pair 13.
    """

    mean: dict[str, float]
    spread: dict[str, float]


def frame(pool_dna: Mapping[int, Vector]) -> Frame:
    """The mean and spread of every term across the candidate pool.

    A term absent from a candidate is a zero, not a gap: "this film is not cosy" is information
    about the pool, and treating absence as missing would make the mean a statement about the
    subset that happens to carry the term.

    AND THAT IS NOT WHAT `centred` DOES FOUR LINES DOWN, deliberately: decision 218 gives a term
    the vector does not carry no coordinate at all. The two are answers to different questions.
    This one is about the pool's DISTRIBUTION — where tonight's middle is, and how far apart
    tonight's candidates are — and an untagged title genuinely sits at zero in it; that one is
    about one candidate's POSITION, where absence is no statement to place. The asymmetry the
    pair leaves is real and is the price of both being right: an untagged title moves the
    reference point every other candidate is measured against while being exempt from it, which
    is the honest reading of a library where 32% of titles carry no rows. Said here because
    `centred`'s own paragraph names the opposed reading and this docstring did not, and a reader
    who generalises either sentence to the other function silently changes every stored pool
    frame. [decision 218; M4.12 review cycle 1: D3-04]
    """
    terms: set[str] = set()
    for vec in pool_dna.values():
        terms |= set(vec)
    n = len(pool_dna) or 1

    mean = {t: sum(v.get(t, 0.0) for v in pool_dna.values()) / n for t in terms}
    spread = {}
    for t in terms:
        var = sum((v.get(t, 0.0) - mean[t]) ** 2 for v in pool_dna.values()) / n
        spread[t] = math.sqrt(var)
    return Frame(mean=mean, spread=spread)


def centred(vec: Vector, f: Frame) -> dict[str, float]:
    """One candidate as its deviation from tonight's pool, in units of the pool's own spread.

    A candidate sitting exactly at the pool mean on a facet is a zero there, which is what
    makes "this film is unremarkable tonight" and "this film is not in the pool" the same
    statement — as they should be, for a tilt that is about tonight and not about the library.

    A TERM THE VECTOR DOES NOT CARRY GETS NO COORDINATE AT ALL (decision 218). Reading absence as
    0.0 and then centring it gave `(0 - mean)/spread`: a NEGATIVE coordinate on every term the
    pool carries, which is the opposite of the sentence above rather than a rounding of it. An
    untagged candidate — `dna.vectors_for` returns `{}` for a title with no rows, and 32% of the
    shipped library has none — therefore picked up the same non-zero adjustment as every other
    untagged candidate, so they rose or fell together by about 0.44 sd of the real score spread.
    A symmetric fixture cancels that exactly, which is why the toy pools in the tests read 0.0
    and the corpus did not. Now `adjustment(tilt, {}, f) == 0.0` by construction.

    THIS CHANGES A/B OBSERVATIONS TOO, and that is acknowledged here rather than discovered by
    someone comparing two evenings' stored tilts: `observe` centres both sides through this
    function, so a term the chosen film carries and the rejected one does not now contributes the
    chosen film's coordinate alone instead of that coordinate minus the absent one's fabricated
    negative. Answers already recorded were recorded under the old centring and are append-only;
    they are not rewritten. [decision 218; §6.2 step 5]
    """
    out = {}
    for t, m in f.mean.items():
        s = f.spread.get(t, 0.0)
        if s <= MIN_SPREAD:
            continue
        if t not in vec:
            continue
        out[t] = (vec[t] - m) / s
    return out


def _accumulate(tilt: Mapping[str, float], delta: Mapping[str, float]) -> dict[str, float]:
    out = dict(tilt)
    for term, value in delta.items():
        out[term] = out.get(term, 0.0) + value
    return out


def observe(
    tilt: Mapping[str, float], *, chosen: Vector, rejected: Vector, f: Frame | None = None,
    frame: Frame | None = None,
) -> dict[str, float]:
    """§6.2 step 5's separating answer: chosen minus rejected, both in the pool's frame.

    `frame` is the keyword the callers use; `f` is accepted so the module reads naturally from
    inside itself. Exactly one is required.
    """
    ref = frame if frame is not None else f
    if ref is None:
        raise TypeError("observe() needs the session's pool frame")
    a, b = centred(chosen, ref), centred(rejected, ref)
    delta = {t: a.get(t, 0.0) - b.get(t, 0.0) for t in set(a) | set(b)}
    return _accumulate(tilt, delta)


def observe_level(
    tilt: Mapping[str, float], *, first: Vector, second: Vector, toward: bool,
    f: Frame | None = None, frame: Frame | None = None,
) -> dict[str, float]:
    """Decision 154's two level answers, reaching the tilt.

    `either` (`toward=True`) says *both of these would do*, which is a statement about a region
    of DNA space rather than about an ordering; `neither` says the opposite of it. Both are the
    sum of the two centred vectors, signed — so a pair sitting at the pool's own centre carries
    no direction, which is exactly right: "either of these two unremarkable films" is not a mood.
    """
    ref = frame if frame is not None else f
    if ref is None:
        raise TypeError("observe_level() needs the session's pool frame")
    a, b = centred(first, ref), centred(second, ref)
    sign = 1.0 if toward else -1.0
    delta = {t: sign * (a.get(t, 0.0) + b.get(t, 0.0)) for t in set(a) | set(b)}
    return _accumulate(tilt, delta)


# --- one answer, applied ---------------------------------------------------------------------


def applies(vectors: Mapping[int, Vector], *, title_a: int, title_b: int) -> bool:
    """Whether a stored answer still names two candidates of tonight's pool.

    §10's re-import guard, as a predicate rather than as three copies of an `if`. `round.replay`
    has always skipped an answer whose titles have left the pool — "the alternative is inventing
    a belief for a title that is no longer a candidate" — and the tilt has to skip exactly the
    same rows, or the posterior and the tilt describe two different histories of one evening.
    It is a separate function from `applied` below because solo also *counts* with it: 54f's
    provenance line says "tilted by your N answers", and N has to be the number that tilted.
    TWO CANDIDATES, AND THE DOCSTRING ABOVE MEANS IT. Membership alone is satisfied by one title
    named twice, and one candidate named twice is not a comparison: `round.update` moved the
    belief a long way on "I prefer title 1 to title 1" (mu 0.9 -> 0.37 at the measured pool
    spread) while `applied` below returned a delta of exactly zero — so solo re-ranked its picks
    and reported "tilted by your 1 answers" over a tilt that had not moved, which is the
    dishonest provenance finding 37 exists to remove. No selector emits `i == j` (`select` and
    54b's arm both refuse it, and a group answer's pair is sealed into its card), so the pair can
    only arrive in solo's client-supplied answer list — which is the argument for the clause being
    here, in the predicate both the tilt and the count read, rather than in `SoloBody`.
    `round.replay` carries the same clause on the same row for the posterior half.
    [M4.12 review cycle 1: M412-SOLO-04]
    """
    return title_a != title_b and title_a in vectors and title_b in vectors


def applied(
    tilt: Mapping[str, float],
    *,
    answer: str,
    title_a: int,
    title_b: int,
    vectors: Mapping[int, Vector],
    frame: Frame,
) -> dict[str, float]:
    """§6.2 step 5's observation for one answer, whichever of decision 154's four it is.

    THE THIRD COPY IS WHAT MADE THIS A FUNCTION. The same four-branch dispatch stood in
    `play.record_answer`, in `play.retract` and in `solo.picks`, and the three had already drifted:
    only the two in `play` looked the titles up in the frozen pool, and solo's used
    `vectors.get(id, {})` — which, before decision 218, turned a title that had left the pool into
    the pool's anti-title and moved the tilt with a row `round.replay` twenty lines above had
    deliberately ignored. A rule with three call sites is a rule with three ways to forget it,
    which is the argument `round.replay` already makes about §13's hold-out filter.

    `answer` is one of `round.ANSWERS`; anything else is a level answer away from the pair, which
    is what `NEITHER` means. The membership check is *inside*, so no caller can apply an answer
    this module would refuse to count.
    """
    if not applies(vectors, title_a=title_a, title_b=title_b):
        return dict(tilt)
    a_dna, b_dna = vectors[title_a], vectors[title_b]
    if answer == round_rules.A:
        return observe(tilt, chosen=a_dna, rejected=b_dna, frame=frame)
    if answer == round_rules.B:
        return observe(tilt, chosen=b_dna, rejected=a_dna, frame=frame)
    return observe_level(
        tilt, first=a_dna, second=b_dna, frame=frame, toward=answer == round_rules.EITHER,
    )


def adjustment(tilt: Mapping[str, float], vec: Vector, f: Frame) -> float:
    """What this participant's tilt adds to one candidate's tonight score.

    The inner product of the tilt with the candidate's **centred** vector — the same frame on
    both sides. Against a raw vector it would measure a candidate's absolute DNA rather than its
    position in tonight's pool, which is the un-centred version §0 row 4 measured at 0.000.

    Divided by the number of terms the frame carries, so a pool described by forty terms does
    not out-shout one described by four. An empty tilt is exactly zero, which is what 54f's
    "ranked by the personal Ledger with **no tilt**" depends on.
    """
    if not tilt:
        return 0.0
    centred_vec = centred(vec, f)
    total = sum(weight * centred_vec.get(term, 0.0) for term, weight in tilt.items())
    return total / max(len(f.mean), 1)


def moved_facets(tilt: Mapping[str, float], *, top: int = 3) -> list[tuple[str, float]]:
    """The terms the round actually moved, strongest first — what §6.7's rail and any
    "leaning in / leaning away" chip would name. Ordering only; no threshold, because a
    threshold on a weight is what §4.1 rule 2 forbids."""
    return sorted(tilt.items(), key=lambda kv: -abs(kv[1]))[:top]


__all__ = [
    "Frame",
    "MIN_SPREAD",
    "adjustment",
    "applied",
    "applies",
    "centred",
    "frame",
    "moved_facets",
    "observe",
    "observe_level",
]
