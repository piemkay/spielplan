"""§6.2 step 5's combine, and the one string that is a hard rule.

Spec v2.1 §6.2 step 5 (rewritten, 54d), §6.4, §6.5, §0 rows 3 and 4, §14 risk 6.

Four things here, and three of them are traps the prototype fell into.

  * **Zeroing is not an alternative.** Removing a facet's influence cannot put a title on the
    other pole into the result. The prototype printed "here's one of each" over a plain top-3
    that could land wholly on one side, so the test that matters is the one where zeroing alone
    would produce a same-pole slate.
  * **"Below that, decide silently" is half the rule.** A surfacing that fires on every
    measurable disagreement turns §6.8's repair register into background noise, and §14 risk 6
    needs the fire rate before anyone re-tunes the threshold.
  * **The third slot is *replaced*, not appended.** Four finalists is a different promise from
    the one §6.2 makes, and it is the easy mistake — appending is one line shorter.
  * **The conflict copy is bounded on the way out.** §6.6 hands the phrasing to an LLM, and an
    LLM asked to explain a disagreement reaches for "someone will hate this" because it reads
    better. AUC 0.610 does not support that sentence, so a prompt asking nicely is not the
    guarantee — the filter is.
"""

from __future__ import annotations

import pytest

from spielplan.tonight import combine as C
from spielplan.tonight import copy as copy_rules

# One authored axis, §6.4-shaped: term -> weight in [-1, 1], negative = left pole.
AXES = {"mood": {"dread": -1.0, "bleak": -0.8, "cosy": 1.0, "warm": 0.8}}

DNA = {
    1: {"dread": 1.0},            # hard left
    2: {"bleak": 1.0},            # left
    3: {"dread": 0.9},            # left
    4: {"cosy": 1.0},             # hard right
    5: {"warm": 1.0},             # right
    6: {},                        # off the axis entirely
    7: {"dread": 0.95},           # left, and all but a duplicate of title 1
}


def scores(**per_seat):
    """{participant_id: {title_id: tonight score}} from keyword seats p1=..., p2=..."""
    return {int(k[1:]): v for k, v in per_seat.items()}


# --- 54d: three finalists, one wildcard ---------------------------------------------------


def test_the_slate_is_exactly_three_finalists_and_one_wildcard():
    slate = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6, 5: 0.5},
                               p2={1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6, 5: 0.5}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 6)},
        dna=DNA, axes=AXES,
    )
    assert len(slate.finalists) == 3
    assert slate.wildcard is not None
    assert slate.wildcard not in slate.finalists, "the wildcard is never counted among the three"
    assert len(slate.ballot_titles) == 4


def test_the_finalists_are_the_top_three_by_the_plain_average():
    """§0 row 3 again, at the combine rather than at the pool: averaging, not a dominance rule.
    Title 4 is p1's worst and the group's best."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.60, 2: 0.55, 3: 0.50, 4: 0.10, 5: 0.05},
                               p2={1: 0.20, 2: 0.25, 3: 0.30, 4: 0.99, 5: 0.05}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 6)},
        dna=DNA, axes=AXES,
    )
    assert slate.finalists[0] == 4, "mean 0.545 beats title 1's 0.40"


def test_the_wildcard_is_a_step_outside_rather_than_the_fourth_best():
    """§6.4: the exploratory slot is "regions of DNA space near the user's liked regions but
    **unvisited**". A wildcard drawn by rank is the fourth-best film and not a step outside
    anything.

    The fixture makes the two readings disagree: title 7 is next in rank and all but a
    duplicate of the finalists, while title 4 is further down and on the opposite pole. A
    rank-drawn wildcard returns 7; an honest one returns 4."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.8, 3: 0.7, 7: 0.6, 4: 0.1},
                               p2={1: 0.9, 2: 0.8, 3: 0.7, 7: 0.6, 4: 0.1}),
        member_ledger={t: [0.5, 0.5] for t in (1, 2, 3, 4, 7)},
        dna=DNA, axes=AXES,
    )
    assert slate.finalists == [1, 2, 3]
    assert slate.wildcard == 4, "the furthest in DNA terms, not the next in rank (7)"


def test_every_candidate_lands_in_exactly_one_slot():
    """§4.2's `session_result` stores one row per candidate carrying its slot, so the slots
    have to partition the pool rather than overlap it."""
    slate = C.combine(
        per_participant=scores(p1={t: 1.0 - 0.1 * t for t in range(1, 7)},
                               p2={t: 1.0 - 0.1 * t for t in range(1, 7)}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 7)},
        dna=DNA, axes=AXES,
    )
    assert [r["title_id"] for r in slate.rows] == [t for t, _ in slate.ranked]
    assert [r["rank"] for r in slate.rows] == list(range(1, len(slate.rows) + 1))
    by_slot = {}
    for row in slate.rows:
        by_slot.setdefault(row["slot"], []).append(row["title_id"])
    assert sorted(by_slot[C.SLOT_FINALIST]) == sorted(slate.finalists)
    assert by_slot[C.SLOT_WILDCARD] == [slate.wildcard]


# --- 54d: D, and the silence below it -----------------------------------------------------


def test_d_is_the_mean_minus_the_minimum_of_the_seated_members():
    """Owner decision 2026-08-29, recovered from the prototype's `spread()` because DNA_MODEL
    is not vendored here. One member cannot disagree with themselves."""
    assert C.divergence([0.6, 0.2]) == pytest.approx(0.2)
    assert C.divergence([0.5, 0.5]) == pytest.approx(0.0)
    assert C.divergence([0.9]) == pytest.approx(0.0)
    assert C.divergence([]) == pytest.approx(0.0)


def test_the_threshold_is_inclusive_at_exactly_twenty_hundredths():
    """§6.2 step 5: "**D ≥ 0.20**". An implementation using `>` fires on 0.2000001 and not on
    0.20, which is invisible on real data and wrong on the boundary the spec names."""
    assert C.D_THRESHOLD == 0.20
    at = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={1: [0.6, 0.2], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert at.d == pytest.approx(0.20)
    assert at.contested is not None, "D = 0.20 is a split"


def test_below_the_threshold_the_split_is_decided_silently():
    """"~14.5% of nights; **below that, decide silently**". No copy, no zeroed facet, no
    reserved slot — and still a full slate of three."""
    quiet = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={1: [0.599, 0.201], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert quiet.d == pytest.approx(0.199)
    assert quiet.contested is None
    assert quiet.conflict is None
    assert len(quiet.finalists) == 3


def test_the_fire_rate_is_recoverable_from_the_slate():
    """§14 risk 6 wants the rate at which surfacing fires, so whether it fired has to be a
    fact on the session rather than an inference from the copy."""
    quiet = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 5)},
        dna=DNA, axes=AXES,
    )
    assert quiet.conflict is None and quiet.d == pytest.approx(0.0)


def test_divergent_answers_surface_a_split_even_when_d_is_zero():
    """§6.2 step 5's *other* trigger: "divergent answers on the leading candidates". Two people
    with identical Ledgers can still have answered tonight in opposite directions."""
    assert C.divergent_answers(
        [{1: 0.9, 2: 0.1}, {1: 0.1, 2: 0.9}], leading=[1, 2]
    )
    assert not C.divergent_answers(
        [{1: 0.9, 2: 0.1}, {1: 0.8, 2: 0.2}], leading=[1, 2]
    ), "agreeing about the order but not the amount is not a divergence"


# --- 54d: the reserved third slot ---------------------------------------------------------


def test_a_surfaced_split_reserves_the_third_slot_for_the_opposite_pole():
    """The construction 54d exists for. Every high-scoring title here is on the LEFT pole, so
    zeroing the axis alone leaves a slate of three left-pole films under copy that promises one
    of each — the prototype's exact defect."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20},
                               p2={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20}),
        member_ledger={1: [0.7, 0.1], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5], 5: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert slate.contested == "mood"

    poles = [C.axis_position(DNA[t], AXES["mood"]) for t in slate.finalists]
    assert any(p < 0 for p in poles) and any(p > 0 for p in poles), (
        "the slate must actually contain one of each, not merely say so"
    )


def test_the_reserved_slot_replaces_the_third_rather_than_being_appended():
    """"replacing the third-ranked title, not appended alongside it". Four finalists is a
    different promise from the one §6.2 makes — and appending is the shorter implementation."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20},
                               p2={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20}),
        member_ledger={1: [0.7, 0.1], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5], 5: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert len(slate.finalists) == 3
    assert 3 not in slate.finalists, "the third-ranked left-pole title is the one displaced"


def test_the_contested_axis_stops_explaining_the_ranking():
    """"The contested axis is **zeroed, not averaged**" — its INFLUENCE is removed, which is a
    statement about how much of the ranking it explains.

    An earlier version subtracted the axis position from the score, and the review measured
    what that costs: `axis_position` is normalised to [−1, 1] while a group score sits on
    §5.1's scale, where a whole pool may span 0.1. Subtracting one from the other does not zero
    the axis — it multiplies its influence with the sign flipped, so the two unreserved slots
    end up decided by the axis's own pole convention, which is the opposite of what 54d asks.
    The assertion is corrected to the property rather than to the arithmetic.
    """
    # A pool the axis explains completely: score rises with the mood position.
    base = {1: 0.10, 3: 0.15, 2: 0.20, 5: 0.60, 4: 0.70}
    poles = {t: C.axis_position(DNA[t], AXES["mood"]) for t in base}
    out = C.zeroed(base, facet="mood", dna=DNA, axes=AXES)

    def covariance(y):
        mx = sum(poles.values()) / len(poles)
        my = sum(y.values()) / len(y)
        return sum((poles[t] - mx) * (y[t] - my) for t in y)

    assert covariance(base) > 0.0, "the fixture is only meaningful while the axis explains it"
    assert covariance(out) == pytest.approx(0.0, abs=1e-9), (
        "after zeroing, the axis explains none of the ranking"
    )
    assert sum(out.values()) == pytest.approx(sum(base.values())), (
        "removing an influence is not moving the whole pool"
    )


def test_a_pool_the_axis_does_not_explain_is_left_alone():
    """Zeroing an axis nothing varies on is a no-op, not a rescale — a title off the axis
    entirely must not move because two other titles disagree about mood."""
    flat = {6: 0.5, 1: 0.4}
    assert C.zeroed(flat, facet="pacing", dna=DNA, axes=AXES) == pytest.approx(flat)


def test_a_pool_with_nothing_on_the_other_pole_does_not_promise_one():
    """A library with no counterweight is a fact about the library. Surfacing a split whose
    alternative does not exist is the promise §0's surfacing rule forbids — "a surfaced split
    must never ship bare"."""
    one_sided = {t: {"dread": 1.0} for t in (1, 2, 3, 4, 5)}
    slate = C.combine(
        per_participant=scores(p1={t: 1.0 - 0.05 * t for t in (1, 2, 3, 4, 5)},
                               p2={t: 1.0 - 0.05 * t for t in (1, 2, 3, 4, 5)}),
        member_ledger={1: [0.7, 0.1], **{t: [0.5, 0.5] for t in (2, 3, 4, 5)}},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=one_sided, axes=AXES,
    )
    assert slate.contested is None
    assert slate.conflict is None
    assert len(slate.finalists) == 3


def test_the_contested_facet_needs_two_people_pulling_opposite_ways():
    assert C.contested_facet([{"dread": 1.0}, {"cosy": 1.0}], AXES) == "mood"
    assert C.contested_facet([{"dread": 1.0}, {"dread": 0.5}], AXES) is None, (
        "leaning the same way by different amounts is agreement"
    )
    assert C.contested_facet([{"dread": 1.0}], AXES) is None


# --- §6.2 step 5 / §6.5: the hard rule on the copy ------------------------------------------


def test_the_sanctioned_line_says_only_what_d_supports():
    line = copy_rules.D_LINE.format(d=0.24)
    assert "below your usual" in line
    assert not copy_rules.overclaims(line)


@pytest.mark.parametrize(
    "phrase",
    [
        "Jenny will hate this.",
        "Patrick is going to dislike the pacing.",
        "One of you can't stand bleak films.",
        "This would ruin the evening for Mia.",
        "She won't like it.",
        "He'll find it unbearable.",
    ],
)
def test_a_phrasing_that_predicts_a_feeling_never_reaches_the_participant(phrase):
    """"D predicts 'one of you is likely to land below your usual tonight' (AUC 0.610), never
    'someone will hate this' — a hard rule on the §6.6 conflict-phrasing LLM task." Replaced,
    not edited: editing out the word leaves the sentence that wanted to say it."""
    assert copy_rules.overclaims(phrase)
    assert copy_rules.bounded(phrase, d=0.24) == copy_rules.D_LINE.format(d=0.24)


@pytest.mark.parametrize(
    "phrase",
    [
        "You're split on pacing — one of you usually lands lower on a slow build.",
        "Tonight leans heavier than Jenny's usual evening.",
        "This sits below Patrick's typical Friday.",
    ],
)
def test_a_phrasing_that_stays_within_the_measurement_is_passed_through(phrase):
    """The bound is on the CLAIM, not on tone: a model that writes a better sentence than the
    sanctioned one keeps it, which is the whole reason §6.6 assigns the task to an LLM."""
    assert not copy_rules.overclaims(phrase)
    assert copy_rules.bounded(phrase, d=0.24) == phrase


def test_an_absent_phrasing_falls_back_to_the_sanctioned_string():
    """The LLM connector is M5. Until then — and whenever a call fails — the split still has to
    say something, and §6.2 already wrote it."""
    assert copy_rules.bounded(None, d=0.31) == copy_rules.D_LINE.format(d=0.31)
    assert copy_rules.bounded("", d=0.31) == copy_rules.D_LINE.format(d=0.31)


def test_the_headline_is_the_specs_own_sentence_and_never_a_models():
    """§6.2 step 5 fixes the headline verbatim; only the explanation is generated. Keeping them
    apart is what stops a model rewriting the sentence the spec wrote."""
    block = C.combine(
        per_participant=scores(p1={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20},
                               p2={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20}),
        member_ledger={1: [0.7, 0.1], **{t: [0.5, 0.5] for t in (2, 3, 4, 5)}},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
        phrasing="They will hate it.",
    ).conflict

    assert block["headline"] == (
        "You're split on mood — here's one of each. The axis is zeroed, not averaged."
    )
    assert block["explanation"] == copy_rules.D_LINE.format(d=block["d"])


# --- §6.2 step 6 / §0 row 4: nothing re-ranks within the evening --------------------------


def test_the_tonight_package_never_reaches_the_ledger_refit_or_the_serving_stack():
    """§6.2 step 6: "Votes *choose*; nothing re-ranks within the evening by predicted enjoyment
    (measured: worth 0.000)."

    Structural rather than behavioural: the pool is computed once at session open from
    `user_score`, and no module under `spielplan/tonight/` may import the fitter or the scorer,
    so a mid-session refit is unreachable rather than merely absent. A behavioural test would
    pass on the day someone adds the import and forgets the call.
    """
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "spielplan" / "tonight"
    banned = ("ledger.refit", "ledger import refit", "scoring.serve", "scoring import serve",
              "from spielplan.ledger import model")
    offenders = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        # Only import lines: the words may legitimately appear in a comment explaining why they
        # are not imported, which is exactly what the package docstring does.
        imports = "\n".join(
            line for line in text.splitlines()
            if line.startswith(("import ", "from ")) or line.lstrip().startswith(("import ", "from "))
        )
        offenders += [f"{path.name}: {b}" for b in banned if b in imports]
    assert not offenders, (
        f"a Tonight module reaches the fitter or the serving stack: {offenders}. §0 row 4 "
        "measured within-evening re-ranking at exactly 0.000."
    )


def test_the_wildcard_carries_its_honest_label():
    """§6.2 step 5: "one exploratory pick **honestly labelled**" — §6.4 gives the words ("a step
    outside your usual, honestly labelled") and the cost it is honest about (≈ −1 pp top-hit
    rate). An unlabelled wildcard is just a worse recommendation.

    Asserting the constant against its own value was a tautology over dead code: the label the
    household actually read was spelled a second time in the client, and nothing anywhere read
    this one. The route now serves it, and `test_the_wildcard_card_carries_the_label_it_is
    _honest_about` is where that is asserted. What is left here is the two properties the words
    themselves have to have.
    """
    assert C.WILDCARD_LABEL, "an unlabelled wildcard is just a worse recommendation"
    # §6.8's register: it names the cost to the person, in their words, and does not hedge.
    assert "usual" in C.WILDCARD_LABEL
    assert not any(w in C.WILDCARD_LABEL.lower() for w in ("explor", "epsilon", "random"))
