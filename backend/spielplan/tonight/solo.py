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
    holdout_key: str,
    answers: Sequence[round_rules.Answered] = (),
    offset: int = 0,
    sharpen: bool = False,
    # THE ROUND'S OWN BOUNDARY, and the retired 1.0 this defaulted to was §6.3's. Decision 214
    # separated the two scales and deleted `api/tonight.py::_z`, the helper that read `straddle_z`
    # out of the bundle; the route has passed `round.BOUNDARY_Z` ever since, so this default
    # reached no production caller and only the tests — which therefore measured every sharpen
    # round at a boundary the app does not serve, and at 1.0 a pool keeps straddlers the shipped
    # 0.6 has already resolved. `play.py` avoids the whole class by making `z` required of every
    # caller; the round's own constant is the same answer in the form `replay` and `select`
    # already use. [decision 214]
    z: float = round_rules.BOUNDARY_Z,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Three picks, a wildcard, and the next pair if the person is sharpening.

    `answers` is 54f's optional round, carried by the client because §6.2 forbids the row that
    would hold it. With none, the ranking is the personal Ledger and the tilt is exactly zero —
    which is what "ranked by the personal Ledger with **no tilt**" requires, and why
    `tilt.adjustment` returns 0.0 for an empty tilt rather than a small number.

    `holdout_key` arrives made rather than being composed here from `user_id`, and that is the one
    thing solo's arm can get wrong. 54b's arm is a rate drawn from a stable key (decision 223), and
    solo has two readers of it: the route re-derives the arm of every answer in `answers` — it may
    not accept it from the client — and this call draws the pair that will come back as one of
    them. A key composed in two places is a key that can be composed two ways, and the two
    disagreeing is a hold-out landing in the adaptive stream, which is the one thing §13 forbids.

    `sharpen` IS THE DOOR'S WHOLE COST, AND IT IS NOT A SKIP OF THE REPLAY. 54f puts this screen
    on the fastest path to a film — "must not be slower than browsing Home" — and the door and
    Reshuffle never draw a pair at all. They were paying for one anyway: `replay` selected
    unconditionally, and with nothing answered the search runs over the straddling set of the
    WHOLE pool (0.677 s at 200 candidates, tens of seconds on the shipped 696-title owned pool,
    and again on every press of Reshuffle). The replay itself still runs, because `played.beliefs`
    is what the ranking below is built from and a sharpen round that never reached the picks would
    be a control that did nothing; it is the selection alone that is skipped. [finding 35; 54f]
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
    tagged = await dna_reads.vectors_for(conn, ids, version=version or "")
    # A KEY PER CANDIDATE, ALWAYS. `vectors_for` gives one to every id it is asked about, so its
    # key set IS tonight's pool — except on a bundle carrying no DNA vocabulary at all, where it
    # returns nothing. Normalising here keeps the predicate below reading "is this title still a
    # candidate" (§10, and `round.replay`'s own guard) rather than "does this title carry tags":
    # a library with no DNA tilts nothing, but its sharpen round still re-ranked the picks and
    # 54f's provenance line has to be able to say so.
    vectors = {t: tagged.get(t, {}) for t in ids}
    prior = {c.title_id: c.group_score for c in candidates}

    played = round_rules.replay(
        prior, list(answers), z=z, has_profile=True,
        axes=combine_rules.axis_positions(vectors, await dna_reads.axes_for(conn, version=version or "")),
        rng=rng or random.Random(0), holdout_key=holdout_key, select=sharpen,
    )
    # THE SAME ROWS THE REPLAY COUNTED, AND NO OTHERS. §13's hold-out filter and §10's re-import
    # guard are both `replay`'s, twenty lines above; this loop walked the identical list and
    # applied only the first of them, so a stored answer whose title had left the pool moved the
    # tilt the posterior had deliberately ignored — and solo rebuilds its pool on every request,
    # so the two disagreed whenever anything changed between two sharpen taps. `tilt.applies` is
    # that predicate, shared rather than restated, and the count is taken AFTER it so 54f's
    # "tilted by your N answers" names the answers that tilted. [finding 37]
    counted = [
        a for a in answers
        if a.selection != round_rules.SELECTION_HOLDOUT
        and tilt_rules.applies(vectors, title_a=a.title_a, title_b=a.title_b)
    ]
    frame = tilt_rules.frame(vectors)
    tilt: dict[str, float] = {}
    for a in counted:
        tilt = tilt_rules.applied(
            tilt, answer=a.answer, title_a=a.title_a, title_b=a.title_b,
            vectors=vectors, frame=frame,
        )

    scored = {
        t: b.mu + tilt_rules.adjustment(tilt, vectors.get(t, {}), frame)
        for t, b in played.beliefs.items()
    }
    order = combine_rules.ranked(scored)
    by_id = {c.title_id: c for c in candidates}

    # 54f/proposal 65: reshuffle "walks further down the ranking" rather than re-drawing. A
    # random re-draw from a ranked list either returns the same top titles or silently degrades
    # the picks; a walk wraps, and the wrap is worth saying out loud — decision 222 renders that
    # line beside Reshuffle, which is what turns the flag below into a claim rather than a note.
    #
    # THE SPAN IS THE RANKING'S LENGTH, not one less than it. Off by one, the walk could not reach
    # the last-ranked title of any pool whose size is 1 more than a multiple of 3: on a four-title
    # pool `3 * offset % 3` is 0 for every offset, so Reshuffle returned the identical three
    # titles for ever while `wrapped` reported True from the first press. The shipped tests used a
    # six-film pool, where a span of five happens to walk. The wrap-fill below already handles a
    # start near the end, so this is the whole repair. [finding 36; decision 222]
    span = max(len(order), 1)
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
            budget_min=budget_min, answers=len(counted), include_rewatches=include_rewatches
        ),
        "empty": None,
        # `answered` is every answer they gave — a hold-out costs one of their twenty even though
        # it moves nothing (54b) — while `sharpened` and the provenance line above report what
        # actually reached the picks. Two numbers because they answer two different questions.
        "answered": len(answers),
        "sharpened": bool(counted),
        # BOTH WAYS THE WALK COMES BACK ROUND, because decision 222 renders this as a sentence
        # about what the household is looking at. The modulus is one of them; the wrap-fill four
        # lines above is the other, and on any pool whose length is not a multiple of PICKS it
        # fires FIRST — a four-title pool shows ranks 4, 1, 2 on press one, two of them titles
        # that were on the screen a second ago, while `start < offset * PICKS` is still false.
        # The flag went from over-reporting (True from press one, on the old modulus) to missing
        # the first real wrap, which is the same confusion with the sign flipped.
        # [M4.12 review cycle 1: M412-FE-2, M412-SOLO-02; decision 222]
        "wrapped": bool(offset) and (start + PICKS > span or start < offset * PICKS),
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
