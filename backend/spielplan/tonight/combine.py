"""§6.2 step 5's combine: three finalists, a wildcard, and the split that reserves a slot.

Spec v2.1 §6.2 step 5 (rewritten, 54d), §6.4 (the explore policy and the axis artifact), §0
rows 3 and 4, §6.5.

    "Per-participant tonight scores are averaged across participants — plain averaging,
     unchanged … The round produces **three finalists and a wildcard**: the top three by group
     score, plus one exploratory pick honestly labelled … A hard split — divergent answers on
     the leading candidates, or Ledger divergence **D ≥ 0.20** (~14.5% of nights; below that,
     decide silently) — is **surfaced with the alternative in hand**, never silently averaged.
     The contested axis is **zeroed, not averaged**, and because zeroing only removes an
     influence it cannot by itself produce an alternative: **the third finalist slot is reserved
     for the highest-scoring title on the opposite pole of the contested axis**."

ZEROING IS NOT AN ALTERNATIVE, WHICH IS THE WHOLE POINT OF 54d. Removing a facet's influence
from the ranking cannot put a title on the other pole into the result — the prototype printed
"here's one of each" over a plain top-3 that could land wholly on one side. So the reservation
is a construction step, and it **replaces** the third-ranked title rather than being appended
beside it: a fourth finalist would be a different promise from the one §6.2 makes.

D's FORMULA IS RECOVERED, NOT CHOSEN. §6.2 gives the threshold (0.20) and the frequency (~14.5%
of nights) and never defines D. Proposal 63 says to recover the formula from `DNA_MODEL` §5.3 —
which is not vendored in this repo, so its own escalation clause fires and the owner settled it
on 2026-08-29: **mean − min of the seated members' §5.1 scores, per candidate**, guests without
a grid profile excluded. That is the prototype's `spread()`, the only formula any artifact here
carries. The risk is stated rather than hidden: mean-minus-min and |Δ| differ by exactly 2× for
a couple, so a threshold calibrated on one and shipped against the other fires at half or double
the intended rate. Filed as a v2.2 spec defect; the formula lives in one function so a
correction is one edit.

PER CANDIDATE, NOT PER NIGHT. Proposal 63 again: "the ~14.5% figure is the share of nights in
which the **winning** candidate crosses the threshold". So D is computed per candidate and the
session's split test reads the leading one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spielplan.tonight import copy as copy_rules

# §6.2 step 5's threshold, inclusive. "~14.5% of nights; below that, decide silently" — the
# silence is half the rule, and the half an implementation drops.
D_THRESHOLD = 0.20

# 54d: "three finalists and a wildcard".
FINALISTS = 3

# §6.4's explore policy: "~1 exploratory slot in 6, ranked by prior + proximity; cost ≈ −1 pp
# top-hit rate, honestly labelled". One slot beside three finalists is that ratio at this scale.
WILDCARD_LABEL = "a step outside your usual"

SLOT_FINALIST = "finalist"
SLOT_WILDCARD = "wildcard"
SLOT_RUNNER_UP = "runner_up"


@dataclass(frozen=True)
class Slate:
    """What the round produces. `ranked` is every candidate in group-score order; `finalists`
    and `wildcard` name the ones the ballot is over (54e)."""

    ranked: list[tuple[int, float]]
    finalists: list[int]
    wildcard: int | None
    contested: str | None = None
    conflict: dict[str, Any] | None = None
    d: float = 0.0
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ballot_titles(self) -> list[int]:
        """54e: "everything they would be happy with among the three finalists and the
        wildcard"."""
        return [*self.finalists, *( [self.wildcard] if self.wildcard is not None else [] )]


# --- the group score ---------------------------------------------------------------------


def group_scores(per_participant: Mapping[int, Mapping[int, float]]) -> dict[int, float]:
    """§6.2 step 5: "averaged across participants — plain averaging, unchanged".

    `per_participant` is {participant_id: {title_id: tonight score}}. Unweighted, for the same
    measured reason `pool.group_score` is: no aggregation rule dominates it and dominance rules
    cost −0.012 (§0 row 3).
    """
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    for scores in per_participant.values():
        for title_id, value in scores.items():
            totals[title_id] = totals.get(title_id, 0.0) + float(value)
            counts[title_id] = counts.get(title_id, 0) + 1
    return {t: totals[t] / counts[t] for t in totals}


def ranked(scores: Mapping[int, float]) -> list[tuple[int, float]]:
    """Best first; ties by title id, so a slate is reproducible from the same numbers."""
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# --- D ------------------------------------------------------------------------------------


def divergence(member_scores: Sequence[float]) -> float:
    """Ledger divergence for one candidate: **mean − min**.

    Owner decision, 2026-08-29 — see the module docstring for why this is a recovery rather
    than a preference, and for the factor-of-two risk it carries. Fewer than two members is
    0.0: one person cannot disagree with themselves, and §6.2's D is about a household.
    """
    values = [float(v) for v in member_scores]
    if len(values) < 2:
        return 0.0
    return sum(values) / len(values) - min(values)


def divergent_answers(orderings: Sequence[Mapping[int, float]], leading: Sequence[int]) -> bool:
    """§6.2 step 5's other trigger: "divergent answers on the leading candidates".

    True when two participants order the same pair of leading candidates in opposite
    directions — the smallest concrete reading of "divergent", and the one that cannot fire on
    a household that merely disagrees about how much.
    """
    for i, a in enumerate(leading):
        for b in leading[i + 1 :]:
            signs = {
                (scores[a] > scores[b]) - (scores[a] < scores[b])
                for scores in orderings
                if a in scores and b in scores
            }
            if 1 in signs and -1 in signs:
                return True
    return False


# --- the contested axis --------------------------------------------------------------------


def axis_position(dna: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Where one title sits on one authored axis (§6.4's `dna_axis_weight` TSVs).

    A weighted sum over the terms the title carries, normalised by the weight it engaged so a
    title with two matching terms is not automatically more extreme than one with a single
    strong one. Weights are used as weights and never as a filter (§4.1 rule 2).
    """
    engaged = sum(abs(weights[t]) * abs(dna[t]) for t in set(dna) & set(weights))
    if engaged <= 0.0:
        return 0.0
    total = sum(weights[t] * dna[t] for t in set(dna) & set(weights))
    return total / engaged


def contested_facet(
    tilts: Sequence[Mapping[str, float]], axes: Mapping[str, Mapping[str, float]]
) -> str | None:
    """Which axis the participants pull against each other on, if any.

    The facet on which two participants' pool-centred tilts point in opposite directions with
    the largest combined magnitude. Reads §6.4's shipped, authored axis artifact rather than
    inventing a facet set — "Deterministic — no nightly rebuild, no Procrustes anchoring".
    """
    if len(tilts) < 2:
        return None
    best: tuple[float, str] | None = None
    for facet, weights in axes.items():
        positions = [
            sum(weights.get(term, 0.0) * value for term, value in tilt.items())
            for tilt in tilts
        ]
        if max(positions) <= 0.0 or min(positions) >= 0.0:
            continue          # everyone leans the same way; nothing is contested
        magnitude = max(positions) - min(positions)
        if best is None or magnitude > best[0]:
            best = (magnitude, facet)
    return best[1] if best else None


def zeroed(scores: Mapping[int, float], *, facet: str, dna, axes) -> dict[int, float]:
    """"The contested axis is **zeroed, not averaged**."

    Remove each title's position on the contested axis from its group score, so the axis stops
    deciding the ranking. It cannot by itself produce an alternative — which is exactly what
    54d says, and why the reservation below exists.
    """
    weights = axes.get(facet, {})
    return {
        t: s - axis_position(dna.get(t, {}), weights)
        for t, s in scores.items()
    }


# --- the slate -------------------------------------------------------------------------------


def _wildcard(
    order: Sequence[tuple[int, float]], chosen: Sequence[int], dna: Mapping[int, Mapping[str, float]]
) -> int | None:
    """§6.4's exploratory slot: "regions of DNA space near the user's liked regions but
    unvisited … honestly labelled".

    The candidate outside the finalists that is furthest, in DNA terms, from what the finalists
    already are. Distance rather than rank, because a wildcard drawn by rank is the fourth-best
    film and not a step outside anything.
    """
    rest = [t for t, _ in order if t not in set(chosen)]
    if not rest:
        return None
    if not dna:
        return rest[0]
    centre: dict[str, float] = {}
    for t in chosen:
        for term, value in dna.get(t, {}).items():
            centre[term] = centre.get(term, 0.0) + value / max(len(chosen), 1)

    def distance(title_id: int) -> float:
        vec = dna.get(title_id, {})
        terms = set(vec) | set(centre)
        return sum((vec.get(x, 0.0) - centre.get(x, 0.0)) ** 2 for x in terms)

    # Ties by score order, so a pool with no DNA at all still returns the best runner-up rather
    # than an arbitrary row.
    return max(rest, key=lambda t: (distance(t), -rest.index(t)))


def combine(
    *,
    per_participant: Mapping[int, Mapping[int, float]],
    member_ledger: Mapping[int, Sequence[float]],
    tilts: Sequence[Mapping[str, float]] = (),
    axes: Mapping[str, Mapping[str, float]] | None = None,
    dna: Mapping[int, Mapping[str, float]] | None = None,
    phrasing: str | None = None,
) -> Slate:
    """§6.2 step 5, end to end.

    `member_ledger` is {title_id: [each seated member's §5.1 score]} — D's input, and
    deliberately not the tonight scores: D is *Ledger* divergence, a fact about the household's
    stable taste, which is what DNA_MODEL §5.3 measured. A D computed from tonight scores would
    move with the round's own answers and stop being the quantity the 0.20 threshold was
    calibrated on.
    """
    axes = axes or {}
    dna = dna or {}
    scores = group_scores(per_participant)
    order = ranked(scores)
    if not order:
        return Slate(ranked=[], finalists=[], wildcard=None)

    leading = [t for t, _ in order[:FINALISTS]]
    top = order[0][0]
    d = divergence(member_ledger.get(top, ()))
    split = d >= D_THRESHOLD or divergent_answers(list(per_participant.values()), leading)

    contested = contested_facet(tilts, axes) if split else None
    conflict = None
    finalists = list(leading)

    if split and contested:
        # Zeroed, not averaged — and then the alternative, because zeroing alone cannot produce
        # one. The third slot is REPLACED: a fourth finalist is a different promise.
        adjusted = zeroed(scores, facet=contested, dna=dna, axes=axes)
        adjusted_order = ranked(adjusted)
        finalists = [t for t, _ in adjusted_order[:FINALISTS - 1]]
        weights = axes.get(contested, {})
        lead_pole = axis_position(dna.get(finalists[0], {}), weights)
        opposite = [
            t for t, _ in adjusted_order
            if t not in finalists
            and axis_position(dna.get(t, {}), weights) * lead_pole < 0.0
        ]
        if opposite:
            finalists.append(opposite[0])
        else:
            # Nothing on the other pole is a fact about the library, not a reason to silently
            # promise one anyway. The slate falls back to the plain third and the copy says so.
            finalists = [t for t, _ in adjusted_order[:FINALISTS]]
            contested = None
        if contested:
            conflict = copy_rules.conflict(contested, d=d, phrasing=phrasing)

    wildcard = _wildcard(order, finalists, dna)
    rows = []
    for rank, (title_id, score) in enumerate(order, start=1):
        if title_id in finalists:
            slot = SLOT_FINALIST
        elif title_id == wildcard:
            slot = SLOT_WILDCARD
        else:
            slot = SLOT_RUNNER_UP
        rows.append({"title_id": title_id, "rank": rank, "group_score": score, "slot": slot})

    return Slate(
        ranked=order, finalists=finalists, wildcard=wildcard,
        contested=contested, conflict=conflict, d=d, rows=rows,
    )


__all__ = [
    "D_THRESHOLD",
    "FINALISTS",
    "SLOT_FINALIST",
    "SLOT_RUNNER_UP",
    "SLOT_WILDCARD",
    "Slate",
    "WILDCARD_LABEL",
    "axis_position",
    "combine",
    "contested_facet",
    "divergence",
    "divergent_answers",
    "group_scores",
    "ranked",
    "zeroed",
]
