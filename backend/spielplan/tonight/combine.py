"""§6.2 step 5's combine: three finalists, a wildcard, and the split that reserves a slot (54d).

D is mean - min of the seated members' rank-standardised Ledger scores for the leading candidate
(owner decision 2026-08-29; threshold recalibrated by decision 478).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from spielplan.tonight import copy as copy_rules

# §6.2 step 5's threshold, inclusive; below it decide silently (rank-standardised, decision 478).
D_THRESHOLD = 0.40

# 54d: "three finalists and a wildcard".
FINALISTS = 3

# §6.4's explore policy: "~1 exploratory slot in 6 ... honestly labelled".
WILDCARD_LABEL = "A step outside your usual"

# The wildcard is drawn from the best twentieth of the ranking, at least twelve (decision 482).
WILDCARD_SHARE = 0.05
WILDCARD_FLOOR = 12

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
    # 54d's opposite-pole title "labelled as such" (decision 220); None with no reservation.
    reserved: int | None = None
    # The person split's reservations, {title_id: participant_id} (decision 479).
    reserved_for: dict[int, int] = field(default_factory=dict)

    @property
    def ballot_titles(self) -> list[int]:
        """54e: "everything they would be happy with among the three finalists and the
        wildcard"."""
        return [*self.finalists, *( [self.wildcard] if self.wildcard is not None else [] )]


# --- the group score ---------------------------------------------------------------------


def group_scores(per_participant: Mapping[int, Mapping[int, float]]) -> dict[int, float]:
    """§6.2 step 5: "averaged across participants — plain averaging, unchanged" (§0 row 3)."""
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
    """Ledger divergence for one candidate: **mean − min** (owner decision, 2026-08-29).

    For a couple this is |Δ|/2; the threshold is calibrated on this formula.
    """
    values = [float(v) for v in member_scores]
    if len(values) < 2:
        return 0.0
    return sum(values) / len(values) - min(values)


def divergent_answers(orderings: Sequence[Mapping[int, float]], leading: Sequence[int]) -> bool:
    """§6.2 step 5's other trigger: two participants order a pair of leading candidates oppositely."""
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
    """Where one title sits on one authored axis (§6.4), normalised by the weight it engaged.

    Weights are used as weights and never as a filter (§4.1 rule 2).
    """
    engaged = sum(abs(weights[t]) * abs(dna[t]) for t in set(dna) & set(weights))
    if engaged <= 0.0:
        return 0.0
    total = sum(weights[t] * dna[t] for t in set(dna) & set(weights))
    return total / engaged


def axis_positions(
    dna: Mapping[int, Mapping[str, float]], axes: Mapping[str, Mapping[str, float]]
) -> dict[int, dict[str, float]]:
    """Every candidate's position on every authored axis: 54c's tie-break is about axes, not terms."""
    return {
        title_id: {facet: axis_position(vec, weights) for facet, weights in axes.items()}
        for title_id, vec in dna.items()
    }


def contested_facet(
    tilts: Sequence[Mapping[str, float]], axes: Mapping[str, Mapping[str, float]]
) -> str | None:
    """The authored axis two participants' pool-centred tilts pull against each other on most."""
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

    By regression on axis position, keeping the residual: subtracting a [-1, 1] position from a
    group score would multiply the axis's influence, not remove it.
    """
    weights = axes.get(facet, {})
    positions = {t: axis_position(dna.get(t, {}), weights) for t in scores}
    n = len(scores)
    if n < 2:
        return dict(scores)
    mean_x = sum(positions.values()) / n
    mean_y = sum(scores.values()) / n
    var_x = sum((positions[t] - mean_x) ** 2 for t in scores)
    if var_x <= 1e-12:
        # Every candidate sits at the same point on this axis, so it decides nothing already.
        return dict(scores)
    cov = sum((positions[t] - mean_x) * (scores[t] - mean_y) for t in scores)
    slope = cov / var_x
    return {t: scores[t] - slope * (positions[t] - mean_x) for t in scores}


# --- the slate -------------------------------------------------------------------------------


def wildcard_from(
    order: Sequence[tuple[int, float]], chosen: Sequence[int], dna: Mapping[int, Mapping[str, float]]
) -> int | None:
    """§6.4's exploratory slot: the candidate furthest in DNA from the finalists.

    Only from the best `WILDCARD_SHARE` of the ranking: §6.4 ranks it "by prior + proximity".
    """
    reach = max(WILDCARD_FLOOR, math.ceil(len(order) * WILDCARD_SHARE))
    rest = [t for t, _ in order[:reach] if t not in set(chosen)]
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

    # Ties by score order, so a pool with no DNA returns the best runner-up.
    return max(rest, key=lambda t: (distance(t), -rest.index(t)))


def one_for_each(
    order: Sequence[tuple[int, float]], per_participant: Mapping[int, Mapping[int, float]]
) -> tuple[list[int], dict[int, int], bool]:
    """Decision 479's person split: three finalists on which each seat has one of its own top three.

    Returns (finalists, {title: seat it is reserved for}, whether every seat got one).
    """
    own = {
        p: [t for t, _ in ranked(scores)] for p, scores in per_participant.items()
    }
    top = {p: set(titles[:FINALISTS]) for p, titles in own.items()}
    leader = order[0][0]
    plain = [t for t, _ in order[:FINALISTS]]

    def served(p: int, chosen: Sequence[int]) -> bool:
        return bool(top[p] & set(chosen))

    chosen = [leader]
    reserved_for: dict[int, int] = {}
    by_leader = sorted(per_participant, key=lambda p: (per_participant[p].get(leader, 0.0), p))
    for p in by_leader:
        if len(chosen) >= FINALISTS:
            break
        if served(p, chosen):
            continue
        free = next((t for t in plain if t not in chosen and t in top[p]), None)
        if free is not None:
            chosen.append(free)
            continue
        pick = next((t for t in own[p] if t not in chosen), None)
        if pick is not None:
            chosen.append(pick)
            reserved_for[pick] = p
    for t, _ in order:
        if len(chosen) >= FINALISTS:
            break
        if t not in chosen:
            chosen.append(t)
    rank = {t: i for i, (t, _) in enumerate(order)}
    finalists = sorted((t for t in chosen if t not in reserved_for), key=rank.__getitem__)
    finalists += [t for t in chosen if t in reserved_for]
    return finalists, reserved_for, all(served(p, finalists) for p in per_participant)


def combine(
    *,
    per_participant: Mapping[int, Mapping[int, float]],
    member_ledger: Mapping[int, Sequence[float]],
    tilts: Sequence[Mapping[str, float]] = (),
    axes: Mapping[str, Mapping[str, float]] | None = None,
    dna: Mapping[int, Mapping[str, float]] | None = None,
) -> Slate:
    """§6.2 step 5, end to end.

    `member_ledger` is {title_id: [each member's rank-standardised Ledger score]}: D's input, not
    the tonight scores, which move with the round's answers (decisions 477, 478).
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
    reserved: int | None = None
    # The ranking the slate is drawn from; a surfaced split replaces it with the zeroed one.
    slate_order = order

    if split and contested:
        # Zeroed, then the alternative: the third slot is REPLACED, never a fourth finalist.
        adjusted = zeroed(scores, facet=contested, dna=dna, axes=axes)
        adjusted_order = ranked(adjusted)
        free = [t for t, _ in adjusted_order[:FINALISTS - 1]]
        weights = axes.get(contested, {})
        poles = {t: axis_position(dna.get(t, {}), weights) for t, _ in adjusted_order}
        # The reference pole comes from a free finalist, else the pool: a leader may carry no DNA.
        ref = next((poles[t] for t in free if poles.get(t, 0.0) != 0.0), 0.0)
        if ref == 0.0:
            ref = next((p for p in poles.values() if p != 0.0), 0.0)
        finalists = list(free)
        opposite = [
            t for t, _ in adjusted_order
            if t not in finalists and poles.get(t, 0.0) * ref < 0.0
        ]
        if opposite and not any(poles.get(t, 0.0) * ref > 0.0 for t in free):
            # Neither free slot is on the reference pole: reserve slot two as well (decision 221).
            on_ref = [t for t, _ in adjusted_order if poles.get(t, 0.0) * ref > 0.0]
            reserved = opposite[0]
            finalists = [free[0], on_ref[0], reserved]
        elif opposite:
            reserved = opposite[0]
            finalists.append(reserved)
        elif any(poles.get(t, 0.0) * ref < 0.0 for t in finalists):
            # The free slots already span the axis: still surfaced, and the third is the next best.
            # A two-candidate pool has no third, which is a complete slate.
            third = next((t for t, _ in adjusted_order if t not in finalists), None)
            if third is not None:
                finalists.append(third)
        else:
            # Nothing on the other pole: decide silently (§0), and on `order`, since nothing is
            # surfaced.
            finalists = [t for t, _ in order[:FINALISTS]]
            contested = None
        if contested:
            slate_order = adjusted_order
            conflict = copy_rules.conflict(contested, d=d)

    # No axis artifact loaded (decision 173): a split by D alone is surfaced by person
    # (decision 479); `divergent_answers` fires too often to trigger it.
    reserved_for: dict[int, int] = {}
    by_person = not axes and len(per_participant) >= 2 and d >= D_THRESHOLD
    if by_person:
        finalists, reserved_for, each = one_for_each(order, per_participant)
        conflict = copy_rules.person_conflict(d=d, one_for_each=each)

    # The wildcard comes from the same ranking the slate was built from.
    wildcard = wildcard_from(slate_order, finalists, dna)
    # A surfaced split persists the slate's reading order as rank, since its finalists are no
    # longer a prefix of `order`; `group_score` stays the plain average.
    if contested or by_person:
        placed = {*finalists, wildcard}
        sequence = [
            *finalists,
            *([wildcard] if wildcard is not None else []),
            *(t for t, _ in slate_order if t not in placed),
        ]
    else:
        sequence = [t for t, _ in order]
    rows = []
    for rank, title_id in enumerate(sequence, start=1):
        if title_id in finalists:
            slot = SLOT_FINALIST
        elif title_id == wildcard:
            slot = SLOT_WILDCARD
        else:
            slot = SLOT_RUNNER_UP
        rows.append({
            "title_id": title_id, "rank": rank, "group_score": scores[title_id], "slot": slot,
            "reserved": title_id == reserved,
            "reserved_for": reserved_for.get(title_id),
        })

    return Slate(
        ranked=order, finalists=finalists, wildcard=wildcard,
        contested=contested, conflict=conflict, d=d, rows=rows, reserved=reserved,
        reserved_for=reserved_for,
    )


__all__ = [
    "D_THRESHOLD",
    "FINALISTS",
    "SLOT_FINALIST",
    "SLOT_RUNNER_UP",
    "SLOT_WILDCARD",
    "Slate",
    "WILDCARD_FLOOR",
    "WILDCARD_LABEL",
    "WILDCARD_SHARE",
    "axis_position",
    "axis_positions",
    "combine",
    "contested_facet",
    "divergence",
    "divergent_answers",
    "group_scores",
    "one_for_each",
    "ranked",
    "wildcard_from",
    "zeroed",
]
