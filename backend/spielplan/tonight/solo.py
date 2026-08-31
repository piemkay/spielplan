"""§6.2 step 8 — "Tonight, for {name}". Spec v2.1 §6.2 step 8 (rewritten, 54f), §6.4, §6.8.

    "\"Tonight, for {name}\" lands **directly on three picks and a wildcard** ranked by the
     personal Ledger with no tilt — the fastest path to a film must not be slower than browsing
     Home. A **sharpen this** control runs the same adaptive round against the same pool and
     re-ranks in place; the provenance line then reads \"tilted by your N answers\" instead of
     \"unseen first\". There is no ballot in solo mode: with one participant, approval share is
     not a measurement. A **reshuffle** control walks further down the ranking."

NO SESSION ROW, AND WHAT THAT COSTS. §6.2 is explicit — solo mints no `session`. So it can mint
no `session_participant` and therefore no `session_answer` either, since both are keyed on a
session. The sharpen round is consequently **stateless**: the client carries its own answers and
hands them back, and the server replays them. Nothing is lost that the spec asks for — there is
no blind property to protect with one participant, and no ballot — but it does mean §14 risk 6's
"log every vote" cannot cover solo. That tension is between two sentences of the spec rather
than a choice made here, and it is reported as a v2.2 defect rather than resolved by inventing
the row §6.2 forbids.

THE SAME POOL, WHICH IS WHAT MAKES SOLO A MODE RATHER THAN A SECOND RECOMMENDER. `pool.build`
with one seat: owned only, the selected kind only, the soft budget with its label, and the
rewatch setting. A solo path that ranked the whole library would reintroduce exactly the
unowned, unwatchable and over-long suggestions the pool filters exist to remove.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from spielplan.tonight import combine as combine_rules
from spielplan.tonight import dna as dna_reads
from spielplan.tonight import pool as pool_rules
from spielplan.tonight import round as round_rules
from spielplan.tonight import tilt as tilt_rules

# 54f: "three picks and a wildcard".
PICKS = 3

# §6.2 step 8's two why-line forms. The pull names terms the pick actually carries; the stretch
# is the wildcard's honest label, and §6.4 fixes what it is honest about (≈ −1 pp top-hit rate).
PULL_WHY = "pulls you with {terms}"
STRETCH_WHY = "a stretch — outside your usual"

# The provenance line, in the two forms 54f names. "unseen first" reports the rewatch filter;
# the tilted form replaces it once the sharpen round has answers, rather than being appended —
# 54f says "instead of", and the prototype concatenated them so the line claimed a tilt on a
# round nobody had run.
PROVENANCE_PLAIN = "{budget} min budget · unseen first"
PROVENANCE_TILTED = "{budget} min budget · tilted by your {n} answers"
PROVENANCE_REWATCH = "{budget} min budget · rewatches included"

# How many terms a why-line may name. Copy, not a tuned number — the same bound `home/why.py`
# puts on a shelf's why for the same reason: a one-line why that names five terms is not one.
NAMED_TERMS = 2


def _pair_side(candidate) -> dict[str, Any] | None:
    """One side of a sharpen pair, as the card needs it. Explicit rather than `vars()`: a
    candidate also carries its per-seat scores, and a payload that shipped those would put the
    pool's own ranking on the screen §6.2 step 3 keeps it off."""
    if candidate is None:
        return None
    return {
        "title_id": candidate.title_id, "name": candidate.name, "year": candidate.year,
        "runtime_min": candidate.runtime_min, "poster_path": candidate.poster_path,
        "fit_line": candidate.fit_line,
    }


def provenance(*, budget_min: int, answers: int, include_rewatches: bool) -> str:
    if answers:
        return PROVENANCE_TILTED.format(budget=budget_min, n=answers)
    if include_rewatches:
        return PROVENANCE_REWATCH.format(budget=budget_min)
    return PROVENANCE_PLAIN.format(budget=budget_min)


def why_line(terms: Sequence[str]) -> str:
    return PULL_WHY.format(terms=" + ".join(terms[:NAMED_TERMS]))


async def picks(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    budget_min: int,
    include_rewatches: bool,
    bundle_version: str,
    answers: Sequence[round_rules.Answered] = (),
    offset: int = 0,
    z: float = 1.0,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Three picks, a wildcard, and the next pair if the person is sharpening.

    `answers` is 54f's optional round, carried by the client because §6.2 forbids the row that
    would hold it. With none, the ranking is the personal Ledger and the tilt is exactly zero —
    which is what "ranked by the personal Ledger with **no tilt**" requires, and why
    `tilt.adjustment` returns 0.0 for an empty tilt rather than a small number.
    """
    seat = pool_rules.Seat(participant_id=user_id, user_id=user_id, is_member=True)
    candidates = await pool_rules.build(
        conn, seats=[seat], kind=kind, budget_min=budget_min,
        include_rewatches=include_rewatches, bundle_version=bundle_version,
    )
    if not candidates:
        return {
            "picks": [], "wildcard": None,
            "provenance": provenance(
                budget_min=budget_min, answers=0, include_rewatches=include_rewatches
            ),
            "empty": (
                f"Nothing in the library fits {budget_min} minutes tonight — widen the budget "
                "or include rewatches."
            ),
            "pair": None, "answered": 0, "sharpened": False,
        }

    version = await dna_reads.active_version(conn)
    ids = [c.title_id for c in candidates]
    vectors = await dna_reads.vectors_for(conn, ids, version=version or "")
    prior = {c.title_id: c.group_score for c in candidates}

    live = [a for a in answers if a.selection != round_rules.SELECTION_HOLDOUT]
    played = round_rules.replay(
        prior, list(answers), z=z, has_profile=True,
        axes=combine_rules.axis_positions(vectors, await dna_reads.axes_for(conn, version=version or "")),
        rng=rng or random.Random(0),
    )
    frame = tilt_rules.frame(vectors)
    tilt: dict[str, float] = {}
    for a in live:
        a_dna, b_dna = vectors.get(a.title_a, {}), vectors.get(a.title_b, {})
        if a.answer == round_rules.A:
            tilt = tilt_rules.observe(tilt, chosen=a_dna, rejected=b_dna, frame=frame)
        elif a.answer == round_rules.B:
            tilt = tilt_rules.observe(tilt, chosen=b_dna, rejected=a_dna, frame=frame)
        else:
            tilt = tilt_rules.observe_level(
                tilt, first=a_dna, second=b_dna, frame=frame,
                toward=a.answer == round_rules.EITHER,
            )

    scored = {
        t: b.mu + tilt_rules.adjustment(tilt, vectors.get(t, {}), frame)
        for t, b in played.beliefs.items()
    }
    order = combine_rules.ranked(scored)
    by_id = {c.title_id: c for c in candidates}

    # 54f/proposal 65: reshuffle "walks further down the ranking" rather than re-drawing. A
    # random re-draw from a ranked list either returns the same top titles or silently degrades
    # the picks; a walk wraps, and the wrap is worth saying out loud.
    span = max(len(order) - 1, 1)
    start = (offset * PICKS) % span if offset else 0
    chosen = [t for t, _ in order[start:start + PICKS]]
    if len(chosen) < PICKS:
        chosen += [t for t, _ in order if t not in chosen][: PICKS - len(chosen)]
    wildcard = combine_rules.wildcard_from(order, chosen, vectors)

    async def card(title_id: int, *, stretch: bool) -> dict[str, Any]:
        c = by_id[title_id]
        terms = await dna_reads.terms_carried_by(
            conn, title_id, version=version or "", limit=NAMED_TERMS
        )
        return {
            "title_id": title_id, "name": c.name, "year": c.year,
            "runtime_min": c.runtime_min, "poster_path": c.poster_path,
            "fit_line": c.fit_line, "over_budget_min": c.over_budget_min,
            # §6.8 makes the one-line why mandatory. A pick with no carried term still gets a
            # line rather than an empty string — an unexplained pick is the register failing at
            # its cheapest point, and silence is worse than "we cannot say yet".
            "why": (
                STRETCH_WHY if stretch
                else why_line([t["term"] for t in terms]) if terms
                else "top of your ledger tonight"
            ),
            "terms": [{"term": t["term"], "tier": t["tier"]} for t in terms],
        }

    return {
        "picks": [await card(t, stretch=False) for t in chosen],
        "wildcard": None if wildcard is None else await card(wildcard, stretch=True),
        "provenance": provenance(
            budget_min=budget_min, answers=len(live), include_rewatches=include_rewatches
        ),
        "empty": None,
        "answered": len(answers),
        "sharpened": bool(live),
        "wrapped": bool(offset) and start < (offset * PICKS),
        # 54f's sharpen round, on the same pool. None once it has converged or hit the cap.
        "pair": None if played.stop_reason else (
            None if played.next_pair is None else {
                "selection": played.next_pair.selection,
                "reason": played.next_pair.reason,
                "a": _pair_side(by_id.get(played.next_pair.title_a)),
                "b": _pair_side(by_id.get(played.next_pair.title_b)),
            }
        ),
        "stop_reason": played.stop_reason,
        "tilt": tilt,
    }


def moved_facets(tilt: Mapping[str, float]) -> list[dict[str, Any]]:
    """The terms the sharpen round moved, for a "leaning in / leaning away" chip.

    Ordering only, no threshold: a threshold on a weight is what §4.1 rule 2 forbids.
    """
    return [
        {"term": term, "direction": "leaning in" if value > 0 else "leaning away"}
        for term, value in tilt_rules.moved_facets(tilt)
        if value != 0.0
    ]


__all__ = [
    "NAMED_TERMS",
    "PICKS",
    "PROVENANCE_PLAIN",
    "PROVENANCE_REWATCH",
    "PROVENANCE_TILTED",
    "PULL_WHY",
    "STRETCH_WHY",
    "moved_facets",
    "picks",
    "provenance",
    "why_line",
]
