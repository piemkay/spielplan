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


def test_the_reserved_slot_goes_to_the_best_title_on_the_opposite_pole():
    """54d: the third slot is reserved "for the **highest-scoring** title on the opposite pole".

    The test above asserts the slate contains one of each, which is a weaker claim: it holds for
    any opposite-pole title, so an implementation reaching for the wrong end of the same list
    passed every test in this file (checked, by making it `opposite[-1]`).

    This board is built so the reservation has to CHOOSE. Zeroing the mood axis lifts the
    off-pole title 4 into the leading two, which flips the lead pole to the right, so the
    opposite-pole candidates are the left-pole 2 (0.90) and 3 (0.85) — both outside the two
    slots already filled, and one of them better than the other. `opposite[-1]` reserves 3 here.
    """
    per = {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.40, 5: 0.20}
    slate = C.combine(
        per_participant=scores(p1=per, p2=per),
        member_ledger={1: [0.7, 0.1], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5], 5: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert slate.contested == "mood", "the split has to be surfaced for the reservation to run"

    reserved = [t for t in slate.finalists if t in (2, 3)]
    assert reserved == [2], (
        f"the reservation took {reserved}, and 2 outscores 3 on the same pole"
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


# --- M4.12: the split branch, repaired -----------------------------------------------------
#
# EVERY TEST BELOW IS GREEN ON A BRANCH THAT CANNOT EXECUTE ON THE SHIPPED BUNDLE, and saying so
# is part of the coverage. Decision 173 ships no `dna_axis_weight` rows, so `tonight/dna.axes_for`
# returns {}, `contested_facet` iterates zero axes and returns None, and no real evening reaches
# any of this. The axes here are hand-seeded, which makes these statements about the RULE and
# never about what a household will see this month; §14 risk 6's split rate reads a permanent 0
# until proposal 140's corpus work lands, which is a different repository. The repairs land now so
# that the day the axes arrive the branch is not four defects deep. [M4.12 findings 21-24]

# One authored axis with two poles and nothing else, so a title's position is exactly the sign of
# the term it carries and the arithmetic in each fixture can be read off the page.
PACE = {"pace": {"slow": -1.0, "fast": 1.0}}
# Two people pulling opposite ways on it, which is all `contested_facet` asks for.
PULLING_APART = [{"slow": 1.0}, {"fast": 1.0}]


def _split(dna, per, *, top):
    """A two-member evening that agrees on the ranking and diverges on `top` in the Ledger.

    D carries the split rather than `divergent_answers`, so the fixtures below can hold the group
    order fixed and vary only the DNA - mean minus min of [0.7, 0.1] is 0.30, comfortably over the
    0.20 threshold.
    """
    return C.combine(
        per_participant={10: per, 20: per},
        member_ledger={top: [0.7, 0.1], **{t: [0.5, 0.5] for t in per if t != top}},
        tilts=PULLING_APART, dna=dna, axes=PACE,
    )


def test_a_neutral_leader_still_gets_the_counterweight_the_split_promises():
    """40.6% of the real corpus carries no DNA at all, so the title the zeroed ranking leads with
    routinely has no position on the contested axis - and the reservation was keyed off *its*
    pole. Every product against 0.0 is 0.0, so the opposite set came out empty, the spanning test
    failed too, and the else branch dropped the copy and shipped the zeroed ranking bare: 31.8% of
    splits that HAD a counterweight, silenced by a property of one title. [finding 21]

    Title 1 leads and sits off the axis; the pool holds two slow titles and two fast ones, so
    there is plainly something on both sides for the slate to say "one of each" about.
    """
    dna = {1: {}, 2: {"slow": 1.0}, 3: {"slow": 0.9}, 4: {"fast": 1.0}, 5: {"fast": 0.8}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.40, 5: 0.30}, top=1)

    assert C.axis_position(dna[1], PACE["pace"]) == 0.0, "the fixture's leader is off the axis"
    assert slate.contested == "pace", "the pool has both poles, so the split is surfaceable"
    assert slate.conflict is not None, "§0: a surfaced split must never ship bare"
    poles = [C.axis_position(dna[t], PACE["pace"]) for t in slate.finalists]
    assert any(p < 0 for p in poles) and any(p > 0 for p in poles), (
        "the slate must hold one of each, not merely say so"
    )
    assert slate.reserved == 2, "the highest-scoring title on the far side, not merely one of them"


def test_a_pool_with_no_counterweight_ships_the_ranking_its_scores_show():
    """The else branch is the honest one - a library with nothing on the other pole cannot be
    promised one - but it shipped `adjusted_order[:3]`. Nothing is surfaced there, so the three
    cards were drawn from a ranking the household is never shown, under no copy explaining why: a
    rank-5 title on the slate with a rank-3 title beneath it as a runner-up. [finding 21, second
    half]

    Every title here leans slow; their positions still differ (mixed vectors), so zeroing genuinely
    reorders the pool and the two rankings disagree about the top three.
    """
    dna = {
        1: {"slow": 1.0}, 2: {"slow": 1.0, "fast": 0.5}, 3: {"slow": 1.0, "fast": 0.8},
        4: {"slow": 1.0, "fast": 0.2}, 5: {"slow": 1.0, "fast": 0.9},
    }
    per = {1: 0.90, 2: 0.70, 3: 0.55, 4: 0.80, 5: 0.50}
    assert all(C.axis_position(v, PACE["pace"]) < 0 for v in dna.values()), (
        "the fixture is only meaningful while the far pole is genuinely empty"
    )
    adjusted = C.ranked(
        C.zeroed(C.group_scores({10: per, 20: per}), facet="pace", dna=dna, axes=PACE)
    )
    assert [t for t, _ in adjusted[:3]] != [1, 4, 2], "the two rankings have to disagree"

    slate = _split(dna, per, top=1)
    assert slate.contested is None and slate.conflict is None
    assert slate.finalists == [1, 4, 2], "the top three by the group score the reveal displays"
    assert slate.reserved is None


def test_a_split_over_two_candidates_returns_a_slate_rather_than_raising():
    """`next(t for t, _ in adjusted_order if t not in finalists)` had no default, and on a pool of
    exactly two the free slots have consumed both. The unhandled StopIteration fires inside
    `play.finish`, which runs inside the answer handler - a 500 on the last answer of the evening.

    It was unreachable only because a round over two candidates could never end (finding 6), so
    decision 215's small-pool fix would have turned one silent hang into one outage. Two finalists
    spanning the axis is a complete slate over a pool of two, not an error. [finding 22]
    """
    dna = {1: {"slow": 1.0}, 2: {"fast": 1.0}}
    slate = C.combine(
        per_participant={10: {1: 0.9, 2: 0.1}, 20: {1: 0.1, 2: 0.9}},
        member_ledger={1: [0.5, 0.5], 2: [0.5, 0.5]},
        tilts=PULLING_APART, dna=dna, axes=PACE,
    )
    assert slate.contested == "pace", "the two answers were opposite, so the split is real"
    assert slate.finalists == [1, 2], "both candidates, spanning the axis, and no third to find"
    assert slate.wildcard is None, "nothing is left outside the finalists to explore towards"
    assert [r["rank"] for r in slate.rows] == [1, 2]


def test_neither_free_finalist_on_the_reference_pole_reserves_slot_two_as_well():
    """Repairing the neutral leader does not finish the job: with the reference pole taken from
    the first free finalist that carries one, 219 of 1,971 random pools still came out
    [neutral, neutral, one pole] - one titled card under copy promising one of each.

    §6.2 fixes that sentence verbatim and `copy.SPLIT_LINE` keeps it out of the model's reach, and
    dropping `contested` is finding 21 again. So the slate is made true instead: slot 2 takes the
    best title on the reference pole and slot 3 the best on the opposite one. Exactly three
    finalists still - 54d fixes the count. [decision 221]
    """
    dna = {1: {}, 2: {}, 3: {"slow": 1.0}, 4: {"fast": 1.0}, 5: {"fast": 0.5}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.60, 4: 0.50, 5: 0.40}, top=1)

    assert slate.contested == "pace"
    assert len(slate.finalists) == C.FINALISTS, "still three; no fourth is appended"
    poles = [C.axis_position(dna[t], PACE["pace"]) for t in slate.finalists]
    assert sorted((p > 0) - (p < 0) for p in poles) == [-1, 0, 1], (
        f"one of each beside the neutral leader, not {slate.finalists} at poles {poles}"
    )
    assert slate.finalists[0] == 1, "the zeroed leader keeps slot 1"
    assert slate.finalists[1] == 4, "slot 2: the best title on the reference pole"
    assert slate.finalists[2] == 3, "slot 3: the best title on the opposite pole"
    assert slate.reserved == 3, "the counterweight is the far-pole card, and only that one"


def test_the_wildcard_comes_from_the_ranking_the_finalists_came_from():
    """A surfaced split rebuilds the finalists from the zeroed ranking and then drew §6.4's
    exploratory pick from the unzeroed one - two rankings deciding one slate. [finding 23]

    The fixture makes them disagree where it is decidable. Titles 8 and 9 are mirror images about
    the finalists' DNA centre, so `wildcard_from`'s distance is bit-for-bit equal for both and the
    tie falls to whichever the ranking it was handed puts first. The group score prefers 9; the
    zeroed score prefers 8, because 9 sits on the pole whose influence the zeroing removed. An
    honest "step outside your usual" is the one the slate's own ranking names.
    """
    dna = {1: {"slow": 1.0}, 2: {}, 3: {"fast": 1.0}, 8: {"slow": 0.6}, 9: {"fast": 0.6}}
    per = {3: 0.95, 2: 0.85, 1: 0.60, 9: 0.52, 8: 0.50}
    slate = _split(dna, per, top=3)
    assert slate.contested == "pace" and slate.finalists == [3, 2, 1]

    centre = {}
    for t in slate.finalists:
        for term, value in dna[t].items():
            centre[term] = centre.get(term, 0.0) + value / len(slate.finalists)

    def distance(title_id):
        vec = dna[title_id]
        return sum((vec.get(x, 0.0) - centre.get(x, 0.0)) ** 2 for x in set(vec) | set(centre))

    assert distance(8) == distance(9), "the fixture is only decidable while the distances tie"
    assert C.ranked(C.group_scores({10: per, 20: per}))[3][0] == 9, "the group score prefers 9"
    assert slate.wildcard == 8, (
        "the wildcard was drawn from the unzeroed ranking the finalists did not come from"
    )


def test_the_persisted_ranks_read_in_slate_order_on_a_surfaced_split():
    """`ballot.slate_of` and `result.slate` both ORDER BY rank, and on a surfaced split the
    finalists are no longer a prefix of the group-score order - the reservation reaches down the
    ranking for the counterweight. Stamping the group-score rank therefore listed the wildcard
    above one of the three finalists, on the ballot and on the reveal. [finding 23]

    `session_result_rank` is UNIQUE (session_id, rank) over a NOT NULL smallint CHECK (rank >= 1),
    so the reordering has to stay a permutation of 1..n rather than a re-labelling of some of it.
    """
    dna = {t: {"slow": 1.0} for t in (1, 2, 3, 4)} | {5: {"fast": 1.0}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.80, 5: 0.30}, top=1)

    assert slate.contested == "pace" and slate.finalists == [1, 2, 5]
    assert [t for t, _ in slate.ranked][:3] == [1, 2, 3], (
        "the fixture is only meaningful while the reservation reaches past the group's top three"
    )
    by_rank = {r["rank"]: r for r in slate.rows}
    assert [r["rank"] for r in slate.rows] == sorted(by_rank), "the rows come out in rank order"
    assert sorted(by_rank) == list(range(1, len(slate.rows) + 1)), "a permutation of 1..n"
    assert [by_rank[i]["title_id"] for i in (1, 2, 3)] == slate.finalists
    last_finalist = max(r["rank"] for r in slate.rows if r["slot"] == C.SLOT_FINALIST)
    wildcard_rank = next(r["rank"] for r in slate.rows if r["slot"] == C.SLOT_WILDCARD)
    assert last_finalist < wildcard_rank, "the wildcard reads after all three finalists"
    assert by_rank[3]["group_score"] == pytest.approx(0.30), (
        "the score on the row stays the plain average; only the reading order moved"
    )


def test_exactly_the_reserved_finalist_is_labelled_as_such():
    """54d: the third slot is reserved for the opposite-pole title "**labelled as such**". Which
    of the three cards that is was computable and stated nowhere - no `reserved` or `opposite`
    anywhere in the router, the domain package or the Tonight page - so a household told "here's
    one of each" could not see which card was the other side of the split. [decision 220]

    One row, not two: under decision 221 slot 2 can be placed by construction as well, and
    labelling both tells the person nothing about which is which. The counterweight is the card
    the copy is about.
    """
    dna = {t: {"slow": 1.0} for t in (1, 2, 3, 4)} | {5: {"fast": 1.0}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.80, 5: 0.30}, top=1)
    assert slate.reserved == 5
    assert [r["title_id"] for r in slate.rows if r["reserved"]] == [5]

    per = {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6}
    quiet = C.combine(
        per_participant={10: per, 20: per},
        member_ledger={t: [0.5, 0.5] for t in per},
        dna=dna, axes=PACE,
    )
    assert quiet.contested is None and quiet.reserved is None
    assert not any(r["reserved"] for r in quiet.rows), (
        "no reservation happened, so no card claims to be the far side of anything"
    )
