"""§6.2 step 5's combine: three finalists, a wildcard, and the person split that reserves a slot.

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
    conflict: dict[str, Any] | None = None
    d: float = 0.0
    rows: list[dict[str, Any]] = field(default_factory=list)
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
    dna: Mapping[int, Mapping[str, float]] | None = None,
) -> Slate:
    """§6.2 step 5, end to end.

    `member_ledger` is {title_id: [each member's rank-standardised Ledger score]}: D's input, not
    the tonight scores, which move with the round's answers (decisions 477, 478).
    """
    dna = dna or {}
    scores = group_scores(per_participant)
    order = ranked(scores)
    if not order:
        return Slate(ranked=[], finalists=[], wildcard=None)

    d = divergence(member_ledger.get(order[0][0], ()))
    finalists = [t for t, _ in order[:FINALISTS]]
    conflict = None
    reserved_for: dict[int, int] = {}
    # A hard split is surfaced by person, on D alone (decisions 479 and 542).
    split = len(per_participant) >= 2 and d >= D_THRESHOLD
    if split:
        finalists, reserved_for, each = one_for_each(order, per_participant)
        conflict = copy_rules.person_conflict(d=d, one_for_each=each)

    wildcard = wildcard_from(order, finalists, dna)
    # A surfaced split persists the slate's reading order as rank, since its finalists are no
    # longer a prefix of `order`; `group_score` stays the plain average.
    if split:
        placed = {*finalists, wildcard}
        sequence = [
            *finalists,
            *([wildcard] if wildcard is not None else []),
            *(t for t, _ in order if t not in placed),
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
            "reserved_for": reserved_for.get(title_id),
        })

    return Slate(
        ranked=order, finalists=finalists, wildcard=wildcard, conflict=conflict, d=d, rows=rows,
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
    "combine",
    "divergence",
    "group_scores",
    "one_for_each",
    "ranked",
    "wildcard_from",
]
