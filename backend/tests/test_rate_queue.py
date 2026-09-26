"""What the Rate surface asks next, and why. Spec v2.1 §6.1, §6.8, §5.2, §13, decision 50.

Four questions, and the tests are grouped by which one they answer.

**Which title next?** §6.1 gives the queue three ordering rules — recorded-seen first, then the
imported decade-stratified `seed_list`, then descending P(seen) — and §6.8 puts a mandatory
one-line why on every card. §13 supplies the falsifier: "not-seen rate in the rating queue
(>50% = queue bug)". A constant P(seen) would satisfy every ordering assertion that only checks
"the list came back", so the tests here move one signal at a time and require the order to
move with it.

**Which pair next?** §0 row 6 measured that no selection rule beats random for profiles (best
+0.0013, CI spans 0). So the sampler's correctness is a *distributional* property and is tested
as one: 20,000 draws against an exact enumeration of the eligible pairs, with a chi-square. A
sampler that weighted strata by member count instead of by pair count passes every "the pair
shares a class" assertion and fails this one.

**How is the person labelling?** §5.2's 5x lever and its 60% threshold. The rule is tested at
the boundary, because 60% and "more than 60%" are different rules and only one of them is the
measurement.

**Is the re-ask stream actually silent?** §13 stream (b) needs three properties at once, and
each has its own test: nothing on the wire distinguishes it (asserted over the serialised
payload, not over the dataclass), the row does (asserted by computing sigma from it), and it is
counted neither by the Ledger nor by the class-balance widget.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from spielplan.ledger import observations
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rate import balance, battle, queue, reask

# --- the world ------------------------------------------------------------------------------

# Eight owned films and two series. `item_n` is the crowd's rating count — P(seen)'s popularity
# term, and §5.1's gate input as well for every row that carries a coordinate, which is every row
# this fixture writes by hand. The eight values are distinct and far apart so "ordered by P(seen)"
# and "ordered by id" are never the same list, which is what makes an ordering assertion able to
# fail. The one row where the two quantities come apart is the cold-masked one, and
# `test_a_cold_masked_titles_crowd_count_still_reaches_the_popularity_term` below builds it.
# [M4.13 cycle 2, M413-C2-DIM5-01]
ITEM_N = {1: 180_000, 2: 42_000, 3: 9_000, 4: 3_000, 5: 300, 6: 12_000, 7: 25_000, 8: 900}
YEARS = {1: 1995, 2: 2010, 3: 1982, 4: 1994, 5: 2021, 6: 2016, 7: 1999, 8: 1975}
# Deliberately NOT id order and NOT popularity order: "ordered by seed position" then has to be
# a different list from both.
SEED = [(8, 1970), (4, 1990), (1, 1990), (2, 2010), (6, 2010)]


async def make_user(db, name, role="member"):
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


async def make_world(db, *, with_priors=True, seed=True):
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, is_owned)
        SELECT x.id, x.kind, x.name, x.year, true
        FROM unnest($1::int[], $2::text[], $3::text[], $4::int[]) AS x(id, kind, name, year)
        """,
        list(range(1, 11)),
        ["movie"] * 8 + ["series"] * 2,
        [f"Title {i}" for i in range(1, 11)],
        [YEARS.get(i, 2000) for i in range(1, 11)],
    )
    if with_priors:
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('t1', '{}', 'active')"
        )
        for title_id, n in ITEM_N.items():
            await db.execute(
                """
                INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source)
                VALUES ($1, 't1', 0.5, 0.5, $2, $3, 'backbone')
                """,
                title_id,
                n,
                n / (n + 10.0),
            )
    if seed:
        await db.executemany(
            "INSERT INTO seed_list (position, title_id, decade) VALUES ($1, $2, $3)",
            [(i, t, d) for i, (t, d) in enumerate(SEED)],
        )
    return {
        "patrick": await make_user(db, "patrick", "admin"),
        "mia": await make_user(db, "mia", "member"),
    }


@pytest.fixture
async def world(db):
    return await make_world(db)


def _age(title_id: int) -> float:
    """The age term the fixture implies, shared by `expected_p` and the crowd-term test below."""
    return min(1.0, max(0.0, (datetime.now(UTC).year - YEARS[title_id]) / queue.AGE_SATURATION_YEARS))


def expected_p(
    title_id: int, *, owned: bool = True, co_seen: float = 0.0, playback: bool = False
) -> float:
    """P(seen) recomputed by hand from the fixture, independently of both spellings in the
    module. This is the third implementation on purpose: SQL orders, Python explains, and this
    checks that the two agree with the formula the docstring claims."""
    crowd = min(1.0, math.log1p(ITEM_N[title_id]) / math.log1p(queue.CROWD_SATURATION))
    age = min(1.0, max(0.0, (datetime.now(UTC).year - YEARS[title_id]) / queue.AGE_SATURATION_YEARS))
    return queue.p_seen(
        queue.Features(playback=playback, co_seen=co_seen, crowd=crowd, owned=owned, age=age)
    )


async def rate_all(db, user_id, title_ids, value=2):
    for title_id in title_ids:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)


async def backdate_verdicts(db, user_id, days):
    await db.execute(
        "UPDATE verdict SET created_at = now() - ($2 || ' days')::interval WHERE user_id = $1",
        user_id,
        str(days),
    )


# --- §6.1: the queue's three ordering rules ---------------------------------------------------


async def test_a_fresh_households_first_queue_is_the_seed_list_most_likely_seen_first(db, world):
    """§6.1: "seeded first run from the imported 100-title decade-stratified `seed_list`", and
    decision 490: inside the list, P(seen) decides the order.

    The fixture's file order (8, 4, 1, 2, 6) is neither id order nor P(seen) order, and it is
    the order the household test was served in: on v20260925 that put the corpus's divisive
    pre-2010 picks first and a third of the first block came back "not seen". The expected order
    is computed from `expected_p` -- the formula's third spelling -- rather than pinned, because
    the age term moves with the calendar.
    """
    cards = await queue.next_sweep_cards(
        db, user_id=world["patrick"], kinds=["movie"], limit=5, rng=random.Random(0)
    )
    seeds = [t for t, _ in SEED]
    expected = sorted(seeds, key=expected_p, reverse=True)
    assert expected != seeds, "the fixture no longer separates P(seen) order from file order"
    assert [c.title_id for c in cards] == expected
    assert {c.source for c in cards} == {"seed"}
    decades = dict(SEED)
    for card in cards:
        # The number that placed the card travels with it -- as on every other card -- and the
        # sentence does not print it: P(seen) is the queue's model, behind Show the model since
        # the second household test (A3 of 2026-09-26).
        assert card.p_seen == pytest.approx(expected_p(card.title_id), abs=1e-9)
        crowd = math.log1p(ITEM_N[card.title_id]) / math.log1p(queue.CROWD_SATURATION)
        if crowd >= queue.WELL_KNOWN_CROWD:
            assert card.reason == f"A well-known film from the {decades[card.title_id]}s."
        else:
            # 900 crowd ratings is not "well-known", whatever list the title is on.
            assert card.reason == (
                f"A film from the {decades[card.title_id]}s we ask everyone about first."
            )
        assert "position" not in card.reason, "a 0-based file index is not a reason"
        assert "%" not in card.reason and "starter" not in card.reason, card.reason
    assert {c.title_id for c in cards if "well-known" not in c.reason} == {8}, (
        "the fixture no longer puts one seed title on each side of the well-known line"
    )


async def test_the_seed_list_still_leads_titles_outside_it_until_it_is_answered(db, world):
    """Decision 490 keeps all 100: the list is still the first run, only its inside order moved.

    The best-placed film outside the list out-scores some of the five seeds on P(seen), and it
    still waits for the whole list -- the precedence ends by consumption, as §6.1's "seeded first
    run" has it, and the corpus's decade stratification is the list's to keep.
    """
    cards = await queue.next_sweep_cards(
        db, user_id=world["patrick"], kinds=["movie"], limit=6, reask_rate=0.0
    )
    seeds = {t for t, _ in SEED}
    outside = max((3, 5, 7), key=expected_p)
    assert expected_p(outside) > min(expected_p(t) for t in seeds), "the fixture must test it"
    assert {c.title_id for c in cards[:5]} == seeds
    assert cards[5].title_id == outside and cards[5].source == "p_seen"


async def test_a_seed_title_in_the_library_leads_one_that_is_not(db, world):
    """Decision 490: "the library" is one of the signals the seed order reads. On v20260925 no
    household member answered "not seen" on an owned seed, against 43% and 65% on the unowned,
    less-rated ones. Taking title 2 out of the library costs it §6.1's owned term and moves it
    behind the two seeds it led."""
    patrick = world["patrick"]
    before = [
        c.title_id
        for c in await queue.next_sweep_cards(
            db, user_id=patrick, kinds=["movie"], limit=5, reask_rate=0.0
        )
    ]
    await db.execute("UPDATE title SET is_owned = false WHERE id = 2")
    after = [
        c.title_id
        for c in await queue.next_sweep_cards(
            db, user_id=patrick, kinds=["movie"], limit=5, reask_rate=0.0
        )
    ]
    assert before.index(2) < before.index(8) and before.index(2) < before.index(6)
    assert after.index(2) > after.index(8) and after.index(2) > after.index(6), after


async def test_a_pinned_title_is_served_even_after_a_not_seen_answer(db, world):
    """C5.2 of the household test: a person who searched for a film, or tapped "Rate it" on its
    card, has answered "do you know it" afresh. An earlier "not seen" is the one exclusion a pin
    lifts; unpinned, the answer still holds, and a rated title stays out pinned or not."""
    patrick = world["patrick"]
    await observations.record_not_seen(db, user_id=patrick, title_id=3)
    await observations.record_verdict(db, user_id=patrick, title_id=5, value=1)

    unpinned = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=8, reask_rate=0.0
    )
    assert 3 not in [c.title_id for c in unpinned]

    pinned = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=8, head=[3], reask_rate=0.0
    )
    assert pinned[0].title_id == 3
    assert pinned[0].source == "pinned"
    assert pinned[0].reason == queue.PINNED_REASON

    rated = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=8, head=[5], reask_rate=0.0
    )
    assert 5 not in [c.title_id for c in rated], "a pin never re-opens a rated title"


async def test_once_the_seed_list_is_answered_the_queue_is_ordered_by_descending_p_seen(db, world):
    """§6.1: "**Queue:** P(seen)-ordered (Jellyfin history, popularity, household co-seen)".

    Seed precedence ends by consumption rather than by a counter, so this is the same call as
    the test above with five verdicts in between. The remaining films are 3, 5 and 7, and the
    expected order is computed from `expected_p` — a third implementation of the formula that
    shares nothing with the SQL — rather than pinned to a literal, because the age term moves
    with the calendar and a literal would make this test expire.

    It still cannot pass for a constant P(seen): that ordering is asserted to differ from id
    order, and the three probabilities are asserted to be three distinct numbers.
    """
    patrick = world["patrick"]
    await rate_all(db, patrick, [t for t, _ in SEED])

    remaining = [3, 5, 7]
    expected = sorted(remaining, key=expected_p, reverse=True)
    assert expected != remaining, "the fixture no longer separates P(seen) order from id order"

    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert [c.title_id for c in cards] == expected
    assert {c.source for c in cards} == {"p_seen"}

    probabilities = [c.p_seen for c in cards]
    assert probabilities == sorted(probabilities, reverse=True)
    assert len(set(probabilities)) == 3, "a constant P(seen) would leave the order to the id"
    for card in cards:
        # Postgres ordered and Python explained; this is the check that they agree.
        assert card.p_seen == pytest.approx(expected_p(card.title_id), abs=1e-9)


async def test_a_cold_masked_titles_crowd_count_still_reaches_the_popularity_term(db, world):
    """The reader cs-01 moved without naming it. §6.1's "popularity", off `title_prior.item_n`.

    A `cold_mask` row is excluded from the basis, so §5.1's n_t is 0 for it and its gate is 0 --
    correctly, there is no coordinate for the gate to weight. `title_prior.item_n` is a different
    quantity with the same name: §4.3's per-title support count, which §6.1 weights at 2.0
    through log1p(n)/log1p(1e5) and §8 stage 10's badge payload carries. Writing the gate's 0
    into it moved the logit by the full 2.0 at the largest flagged row of v20260828 (260,131
    crowd ratings) and by 0.99 at the flagged set's median support of 299 -- so with nothing else
    firing, P(seen) fell from 0.45 to 0.10 and the sweep offered the most-watched film in the
    catalogue as one the household has probably never seen.

    Seeded as `serve.materialise_priors` now writes such a row -- the count, gate 0.0, e_source
    'cold_tower' -- rather than through a bundle, because what is under test is the READER: every
    fixture in this file hand-writes `title_prior` with `gate = n/(n+10)`, so until now the queue
    had never met a row where the two disagreed. `test_placement.py::
    test_the_shared_fixtures_flagged_row_is_swept_and_served_from_the_tower` pins the writer.
    [M4.13 cycle 2, M413-C2-DIM5-01]
    """
    patrick = world["patrick"]
    # Title 7 is the best-supported film outside the seed list, so the queue's first card is a
    # claim about the popularity term and not about seed position.
    await db.execute(
        "UPDATE title_prior SET gate = 0.0, e_source = 'cold_tower' WHERE title_id = 7"
    )
    await rate_all(db, patrick, [t for t, _ in SEED])

    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=8, rng=random.Random(0)
    )
    card = next(c for c in cards if c.title_id == 7)
    assert card.source == "p_seen"
    assert card.p_seen == pytest.approx(expected_p(7), abs=1e-9), (
        "the gate is 0 for this row and the popularity term must not read the gate"
    )
    # The term itself, against the one a zeroed column would have produced. 2.0 logits is the
    # whole weight, so the difference is the difference between offering the card and burying it.
    crowd = math.log1p(ITEM_N[7]) / math.log1p(queue.CROWD_SATURATION)
    blind = queue.p_seen(queue.Features(crowd=0.0, owned=True, age=_age(7)))
    assert card.p_seen == pytest.approx(
        queue.p_seen(queue.Features(crowd=crowd, owned=True, age=_age(7))), abs=1e-9
    )
    assert card.p_seen - blind > 0.2, (
        f"the popularity term contributed {card.p_seen - blind:.4f} for a title with "
        f"{ITEM_N[7]:,} crowd ratings"
    )
    order = [c.title_id for c in cards]
    assert order.index(7) < order.index(5), (
        "the film with 25,000 crowd ratings is queued behind the one with 300"
    )


async def test_p_seen_moves_the_queue_when_a_signal_moves(db, world):
    """§13 calls a queue that misses "a queue bug", which is only meaningful if the estimate is
    real. Two of §6.1's three named inputs are moved here, one at a time, and each has to
    reorder the queue on its own.

    Title 5 starts last of the three: it is the least popular, the newest, and nobody else in
    the house has seen it. Household co-seen is worth 1.2 in log-odds and the widest gap it has
    to close is 1.12, so marking it seen for the *other* member has to carry it to the front. A
    playback event is worth 2.5 and takes title 3 there in turn.

    A constant P(seen) leaves the order at the id order through all three calls, and every
    assertion below fails.
    """
    patrick, mia = world["patrick"], world["mia"]
    await rate_all(db, patrick, [t for t, _ in SEED])
    baseline = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert [c.title_id for c in baseline][-1] == 5
    assert [c.title_id for c in baseline] != [3, 5, 7], "this is the id order, not a P(seen) one"

    # §6.1's "household co-seen": mia has seen title 5, patrick has never been asked.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", mia
    )
    with_co_seen = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert with_co_seen[0].title_id == 5, "the household's other member is a named §6.1 input"
    assert with_co_seen[0].p_seen == pytest.approx(expected_p(5, co_seen=1.0), abs=1e-9)
    assert with_co_seen[0].reason == "Someone else in the house has seen it."

    # §6.1's "Jellyfin history": §7.3's >=90% poll fired on title 3 and nobody answered it.
    await db.execute(
        "INSERT INTO playback_event (source, title_id, user_id, finished) "
        "VALUES ('jellyfin', 3, $1, true)",
        patrick,
    )
    with_playback = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert with_playback[0].title_id == 3
    assert with_playback[0].reason == "You played it to the end."
    assert with_playback[0].p_seen == pytest.approx(expected_p(3, playback=True), abs=1e-9)


def test_unfamiliarity_only_lowers_and_needs_the_answers_to_keep_saying_so():
    """The person's own answers per language and kind, shrunk towards "no opinion" by two
    pseudo-answers each way and read only below a half (H7 of 2026-09-26)."""
    assert queue.unfamiliarity(0, 0) == 0.0, "no answers, no opinion"
    assert queue.unfamiliarity(0, 1) == pytest.approx(-0.2)
    assert queue.unfamiliarity(0, 3) == pytest.approx(2 * (2 / 7 - 0.5))
    assert queue.unfamiliarity(0, 30) < queue.unfamiliarity(0, 3) < queue.unfamiliarity(0, 1)
    assert queue.unfamiliarity(0, 10_000) > -1.0
    for seen, answered in ((1, 2), (5, 5), (16, 21), (40, 41)):
        assert queue.unfamiliarity(seen, answered) == 0.0, "a known language is never raised"
    # And it can only ever take P(seen) down: the weight is positive and the feature is not.
    assert queue.WEIGHTS.unfamiliar > 0
    base = queue.Features(owned=True, crowd=0.5)
    assert queue.p_seen(base) > queue.p_seen(
        queue.Features(owned=True, crowd=0.5, unfamiliar=queue.unfamiliarity(0, 3))
    )
    assert queue.dominant(queue.Features(unfamiliar=-0.5)) is None, "it is never the named cause"


async def test_a_persons_not_seen_answers_lower_that_languages_titles_and_nothing_else(db, world):
    """H7 of the second household test: Rate's series queue drifted into anime.

    Jenny answered "not seen" to Attack on Titan and to Berserk, and Monster came three cards
    later; today Death Note leads her series queue on the `owned` term, because the household
    owns 39 Japanese series and nothing in §6.1's P(seen) signals was about her. Her own answers
    are: three "not seen" on one language's series lower that language's series for her, and for
    nobody else, and nothing she knows is raised. The why-line does not change -- the term is a
    correction to the ordering, not a cause the card can name.
    """
    patrick, mia = world["patrick"], world["mia"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned, original_language) VALUES "
        "(51, 'series', 'Anime 1', 2010, true, 'ja'), (52, 'series', 'Anime 2', 2010, true, 'ja'),"
        "(53, 'series', 'Anime 3', 2010, true, 'ja'), (54, 'series', 'Anime 4', 2010, true, 'ja'),"
        "(55, 'series', 'Drama 1', 2010, true, 'en'), (56, 'series', 'Drama 2', 2010, true, 'en')"
    )

    async def queue_for(user):
        cards = await queue.next_sweep_cards(
            db, user_id=user, kinds=["series"], limit=10, exclude=(9, 10), reask_rate=0.0
        )
        return {card.title_id: card for card in cards}

    before = await queue_for(patrick)
    assert before[54].p_seen == pytest.approx(before[55].p_seen), "the fixture is otherwise even"

    for title_id in (51, 52, 53):
        await observations.record_not_seen(db, user_id=patrick, title_id=title_id)
    after = await queue_for(patrick)
    assert set(after) == {54, 55, 56}, "an answered 'not seen' is never asked again"
    assert list(after)[-1] == 54, "the fourth Japanese series now waits behind the English two"
    age = min(1.0, (datetime.now(UTC).year - 2010) / queue.AGE_SATURATION_YEARS)
    assert after[54].p_seen == pytest.approx(
        queue.p_seen(
            queue.Features(owned=True, age=age, unfamiliar=queue.unfamiliarity(0, 3))
        ),
        abs=1e-9,
    ), "SQL orders and Python explains: the two spellings of the term agree"
    assert after[55].p_seen == pytest.approx(before[55].p_seen), "nothing else moves"
    assert after[54].reason == before[54].reason == "It's in your library."

    theirs = await queue_for(mia)
    assert theirs[54].p_seen == pytest.approx(before[54].p_seen), "Patrick's answers are his own"


async def test_a_title_the_app_already_holds_as_seen_leads_the_queue(db, world):
    """§6.0's pending-verdicts banner population — "You've watched X and Y recently — rate
    them?" — is exactly "marked seen, no verdict". It is a record and not an estimate, so its
    P(seen) is 1.0 and it cannot be out-ranked by an accumulation of circumstantial evidence."""
    patrick = world["patrick"]
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", patrick
    )
    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=3, rng=random.Random(0)
    )
    assert cards[0].title_id == 5
    assert cards[0].source == "pending_verdict"
    assert cards[0].p_seen == 1.0
    assert cards[0].reason == queue.SEEN_REASON
    assert cards[1].source == "seed", "the seed list resumes underneath the pending verdict"


async def test_every_card_carries_the_one_line_why_that_names_its_dominant_cause(db, world):
    """§6.8: "every shelf, recommendation, question and conflict carries a one-line why". §6.1
    quoted the form "queued because: 72% likely you have seen it", which the second household
    test read as the queue's working: the line is a whole sentence naming the dominant cause, and
    its probability is not in it (A3 of 2026-09-26; decision 486's model number)."""
    patrick = world["patrick"]
    await rate_all(db, patrick, [t for t, _ in SEED])
    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    sentences = {
        phrase.format(years=f"{y} years", noun=noun)
        for phrase in queue.PHRASES.values()
        for y in range(0, 80)
        for noun in ("film", "series")
    } | {queue.UNSURE_REASON}
    for card in cards:
        assert card.reason in sentences, card.reason
        assert "%" not in card.reason and "queued because" not in card.reason, card.reason
    # The fixture's films are all owned: a crowd of ~1,000 ratings or more is named, and one below
    # that line (title 5, 300 ratings) is not called well-known -- the library is named instead.
    reasons = {c.title_id: c.reason for c in cards}
    assert reasons[5] == "It's in your library.", reasons
    assert set(reasons.values()) <= {"A well-known film.", "It's in your library."}, reasons


async def test_the_age_why_line_prints_the_titles_real_age_and_not_the_saturation_point(db, world):
    """§6.8's why-line is copy, and copy is either true or it is not.

    The printed number was derived from the `age` FEATURE, which the SQL clips to 1.0 at
    `AGE_SATURATION_YEARS` — so every title released before `now() - 40` printed exactly "it has
    been out 40 years", including 6,806 of the corpus's 19,071. It printed it on precisely the
    cards where it is the sentence the person reads: an unowned title with no playback, nobody
    else in the house and no crowd support has no other term above zero, so `dominant` names age.

    The clipped feature is asserted unchanged in the same breath — saturation is the model's claim
    that the forty-first year carries no more information about whether this household saw the
    film, and this milestone changes the copy and not the ordering.

    The expected years are counted on POSTGRES's clock, read here from the same `now()` the query
    uses, because that is the half of the fix a Python-side `datetime.now().year` would leave
    unasserted: two clocks that agree today disagree across a midnight or a mis-set container TZ.
    [M4.10 finding 19]
    """
    patrick = world["patrick"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES "
        "(41, 'movie', 'Old and unowned', 1975, false), "
        "(42, 'movie', 'Young and unowned', 2021, false)"
    )
    this_year = await db.fetchval("SELECT EXTRACT(year FROM now())::int")
    cards = {
        card.title_id: card
        for card in await queue.next_sweep_cards(
            db,
            user_id=patrick,
            kinds=["movie"],
            limit=2,
            exclude=tuple(range(1, 11)),
            reask_rate=0.0,
        )
    }
    assert set(cards) == {41, 42}, "both unowned titles are ordinary candidates"

    for title_id, released in ((41, 1975), (42, 2021)):
        years = this_year - released
        assert cards[title_id].reason == f"It has been out {years} years.", (
            f"title {title_id} was released in {released} and the card says: "
            f"{cards[title_id].reason!r}"
        )
    assert "40 years" not in cards[41].reason, (
        "the 1975 film printed the saturation point rather than its age"
    )

    # The ordering number is unchanged: the feature is still clipped at 1.0 for the old title and
    # is still the raw ratio for the young one.
    clipped = queue.p_seen(queue.Features(owned=False, age=1.0))
    assert cards[41].p_seen == pytest.approx(clipped)
    assert cards[42].p_seen == pytest.approx(
        queue.p_seen(queue.Features(owned=False, age=(this_year - 2021) / queue.AGE_SATURATION_YEARS))
    )
    assert cards[41].p_seen > cards[42].p_seen, "and the older film still sorts first"


async def test_a_title_already_rated_or_explicitly_not_seen_never_returns(db, world):
    """Decision-doc proposal 37: the queue "does not exclude titles already given a verdict …
    and wraps forever". Both halves are fixed here.

    "Not seen" is an *answer*, and an answered question is not asked again. An **adopted**
    unseen is an absent `user_title` row rather than a row saying 'unseen' (see `sync/seen.py`),
    so this exclusion can only ever remove something the person actually said.
    """
    patrick = world["patrick"]
    await observations.record_verdict(db, user_id=patrick, title_id=8, value=1)
    await observations.record_not_seen(db, user_id=patrick, title_id=4)

    seen_ids = {
        c.title_id
        for c in await queue.next_sweep_cards(
            db, user_id=patrick, kinds=["movie"], limit=8, rng=random.Random(0)
        )
    }
    assert 8 not in seen_ids and 4 not in seen_ids
    assert seen_ids == {1, 2, 3, 5, 6, 7}

    # …and the queue drains rather than wrapping.
    await rate_all(db, patrick, sorted(seen_ids))
    assert await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=8, rng=random.Random(0)
    ) == []


async def test_the_banner_cta_pins_its_titles_to_the_front_and_exclude_holds_them_out(db, world):
    """§7.3's capture prompt has "a permanent surface" in §6.0's banner, whose CTA leads to this
    queue; `head` is what "→ the §6.1 queue" means for the two titles it named. `exclude` is the
    sitting's own memory — a card already served does not come back inside the same block."""
    patrick = world["patrick"]
    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=4, head=[7, 3], rng=random.Random(0)
    )
    assert [c.title_id for c in cards][:2] == [7, 3]

    later = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=4, head=[7, 3], exclude=[7], rng=random.Random(0)
    )
    assert 7 not in [c.title_id for c in later]
    assert later[0].title_id == 3


async def test_the_queue_partitions_by_kind(db, world):
    """§4.1 rule 5: "every ranking surface partitions by it". The queue is a selection of one or
    both kinds and never neither — the empty selection is the unpartitioned query the rule
    exists to prevent."""
    patrick = world["patrick"]
    films = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=20, rng=random.Random(0)
    )
    series = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["series"], limit=20, rng=random.Random(0)
    )
    assert {c.title_id for c in films} == set(range(1, 9))
    assert {c.title_id for c in series} == {9, 10}
    with pytest.raises(ValueError, match="at least one kind"):
        await queue.next_sweep_cards(db, user_id=patrick, kinds=[], limit=5)


# --- §6.1 Battle: "drawn at random … within verdict bands" -------------------------------------

# movie/2 holds 4 members (6 pairs), movie/1 holds 3 (3 pairs), series/2 holds 2 (1 pair), and
# movie/0 is a singleton that must never be drawn. 10 eligible pairs in total, with very
# different stratum sizes — which is what separates "uniform over pairs" from the two wrong
# samplers ("uniform over strata" and "weighted by member count").
POOL = [
    battle.PoolMember(1, "movie", 2),
    battle.PoolMember(2, "movie", 2),
    battle.PoolMember(3, "movie", 2),
    battle.PoolMember(4, "movie", 2),
    battle.PoolMember(5, "movie", 1),
    battle.PoolMember(6, "movie", 1),
    battle.PoolMember(7, "movie", 1),
    battle.PoolMember(8, "series", 2),
    battle.PoolMember(9, "series", 2),
    battle.PoolMember(10, "movie", 0),
]


def test_the_battle_sampler_is_uniform_over_every_eligible_in_class_pair():
    """§6.1: "Pairs drawn **at random** from the user's seen titles within verdict bands", and
    §0 row 6: no selection rule beats random for profiles (best +0.0013, CI spans 0).

    20,000 draws over the 10 eligible pairs, expected 2,000 each. The chi-square statistic is
    compared against 27.88, the 0.999 quantile at 9 degrees of freedom, so a correct sampler
    fails this once in a thousand runs and the seed is fixed so it is the same run every time.

    A sampler that picked a stratum uniformly would give the single series/2 pair 1/3 of every
    draw — chi-square in the thousands. One weighting strata by member count would give the
    3-member stratum 3/9 of the draws spread over 3 pairs and the 2-member one 2/9 over 1 pair,
    which also fails. Only pair-count weighting lands here.
    """
    pairs = battle.eligible_pairs(POOL)
    assert len(pairs) == 10
    counts = dict.fromkeys(pairs, 0)
    rng = random.Random(20260830)
    draws = 20_000
    for _ in range(draws):
        a, b, _kind, _cls = battle.draw(POOL, rng=rng)
        counts[(min(a, b), max(a, b))] += 1

    expected = draws / len(pairs)
    chi2 = sum((n - expected) ** 2 / expected for n in counts.values())
    assert chi2 < 27.88, f"pair frequencies are not uniform: chi2={chi2:.1f} over {counts}"
    spread = max(counts.values()) / min(counts.values())
    assert spread < 1.15, f"one pair is served {spread:.2f}x as often as another: {counts}"
    assert 10 not in {t for pair in counts for t in pair}, "a singleton class was drawn from"


def test_which_poster_is_a_and_which_is_b_is_itself_random():
    """§6.1: "two posters are the buttons". A sampler that always put the lower id on the left
    would bake the corpus's id order into every duel's position bias."""
    rng = random.Random(7)
    firsts = [battle.draw(POOL, rng=rng)[0:2] for _ in range(4_000)]
    lower_first = sum(1 for a, b in firsts if a < b)
    # Expected 2,000 with a binomial sd of ~32; +-6 sd is 1,810..2,190.
    assert 1_810 < lower_first < 2_190, f"position is not randomised: {lower_first}/4000"


def test_a_pair_never_crosses_a_verdict_class_or_a_kind():
    """§6.1's "within verdict bands" and §4.1 rule 5's partition, as a property of every draw.

    Cross-class pairs re-derive a boundary the ordered-logit arm already knows from the verdicts
    themselves; a cross-kind duel is not evidence about either partition, and
    `observations.record_duel` refuses to write one.
    """
    band = {m.title_id: (m.kind, m.verdict_class) for m in POOL}
    rng = random.Random(3)
    for _ in range(2_000):
        a, b, kind, verdict_class = battle.draw(POOL, rng=rng)
        assert band[a] == band[b] == (kind, verdict_class)
        assert a != b


def test_no_pair_exists_until_one_class_holds_two_titles():
    """The seam's own contract: None when the user has fewer than two seen+verdicted titles in
    any one class. Three titles in three different classes is still no pair."""
    rng = random.Random(1)
    assert battle.draw([], rng=rng) is None
    assert battle.draw([battle.PoolMember(1, "movie", 2)], rng=rng) is None
    spread = [battle.PoolMember(i + 1, "movie", i) for i in range(3)]
    assert battle.draw(spread, rng=rng) is None
    split = [battle.PoolMember(1, "movie", 2), battle.PoolMember(2, "series", 2)]
    assert battle.draw(split, rng=rng) is None, "same class, different kinds, is not a pair"


def test_the_battle_why_line_names_the_shared_answer_and_no_section():
    """§6.8's one-line why on the battle card, in decision 486's register.

    The line carried 54a's whole clause, "(§6.3)" and "(§6.2)" included, about fifty words under
    two posters: on an iPhone that pushed Tie, the decisive toggle and Skip below the fold, and a
    member was shown two section numbers on the surface they rate on. Then "queued because: you
    rated both liked · random pairs build your profile best", which the second household test
    still read as the queue talking about itself. It now says the one fact that makes the pair a
    fair question -- the person put both in the same place -- under the card's own question, and
    how pairs are drawn is the rail's to say. The deleted falsehood of finding 22 stays pinned as
    an absence. [M4.10 finding 22; decisions 486, 491; A1 and A3 of 2026-09-26]
    """
    for verdict_class, label in enumerate(("disliked", "fine", "liked")):
        line = battle.reason_for(verdict_class)
        assert line == f"You rated both of these {label}.", line
        assert "queued because" not in line and "profile" not in line, line
        assert "§" not in line and "tier queue" not in line, line
        assert "only pay off" not in line, "the clause 54a deleted is back"
        assert len(line) < 80, f"the why-line has to fit a phone card: {len(line)} chars"


def test_a_pair_already_answered_is_not_drawn_again():
    """C5.4: S.W.A.T. vs The Village was served three times in one member's first eight cards,
    each an ordinary duel row. `rank/read.asked_pairs`' rule -- a repeat is an independent Davidson
    row on the strength of one judgement -- holds on this surface too; §13's re-ask stream is the
    one way a compared pair comes back."""
    rng = random.Random(5)
    answered = {frozenset(p) for p in battle.eligible_pairs(POOL)[:9]}
    left = set(battle.eligible_pairs(POOL)) - {tuple(sorted(p)) for p in answered}
    assert len(left) == 1
    for _ in range(300):
        a, b, _kind, _cls = battle.draw(POOL, rng=rng, answered=answered)
        assert (min(a, b), max(a, b)) in left

    everything = {frozenset(p) for p in battle.eligible_pairs(POOL)}
    assert battle.draw(POOL, rng=rng, answered=everything) is None, (
        "a pool whose every pair has been compared has no battle left"
    )


def test_the_sampler_is_uniform_over_the_pairs_not_yet_answered():
    """Sampling without replacement is still random (§0 row 6 is untouched), so the draw over
    what remains has to be as uniform as `test_the_battle_sampler_is_uniform_over_every_eligible_
    in_class_pair` makes the unfiltered one. The answered set here is the whole 6-pair movie/2
    stratum, which a sampler still weighting strata by their FULL pair count would keep choosing
    and throwing away -- and the four pairs left are spread over two strata of unequal size.

    20,000 draws over 4 cells, expected 5,000 each; 16.27 is the 0.999 quantile at 3 degrees of
    freedom, and the seed is fixed.
    """
    answered = {frozenset((a, b)) for a in (1, 2, 3, 4) for b in (1, 2, 3, 4) if a < b}
    remaining = [p for p in battle.eligible_pairs(POOL) if frozenset(p) not in answered]
    assert len(remaining) == 4
    counts = dict.fromkeys(remaining, 0)
    rng = random.Random(20260925)
    for _ in range(20_000):
        a, b, _kind, _cls = battle.draw(POOL, rng=rng, answered=answered)
        counts[(min(a, b), max(a, b))] += 1
    chi2 = sum((n - 5_000) ** 2 / 5_000 for n in counts.values())
    assert chi2 < 16.27, f"the remaining pairs are not drawn uniformly: {counts}"


def test_the_enumerated_remainder_is_still_randomised_left_and_right():
    """A big band compared almost to the end sends `draw` past its rejection budget to the
    enumerated remainder, and the one pair left must still land on either side at random."""
    pool = [battle.PoolMember(i, "movie", 2) for i in range(1, 21)]
    pairs = battle.eligible_pairs(pool)
    answered = {frozenset(p) for p in pairs if p != (7, 13)}
    rng = random.Random(11)
    firsts = []
    for _ in range(400):
        a, b, _kind, _cls = battle.draw(pool, rng=rng, answered=answered)
        assert {a, b} == {7, 13}
        firsts.append(a)
    # Binomial(400, 0.5): sd 10, so +-6 sd.
    assert 140 < firsts.count(7) < 260, firsts.count(7)


async def test_a_duel_the_person_answered_is_never_redrawn_as_a_battle(db, world):
    """The same rule through the database: `next_battle_pair` reads every pair this person has
    compared -- in any context, as `rank/read.asked_pairs` does -- and not only profile battles,
    so a pair settled on Rank does not come back as a battle either."""
    patrick = world["patrick"]
    await rate_all(db, patrick, [1, 2, 3], value=2)
    await observations.record_duel(
        db, user_id=patrick, title_a=1, title_b=2, outcome="A", context="profile_battle"
    )
    await observations.record_duel(
        db, user_id=patrick, title_a=3, title_b=2, outcome="B", context="tier_queue",
        selection="boundary",
    )
    for seed in range(40):
        pair = await battle.next_battle_pair(
            db, user_id=patrick, kinds=["movie"], rng=random.Random(seed), reask_rate=0.0
        )
        assert {pair.title_a, pair.title_b} == {1, 3}, "the only pair nobody has compared"
    await observations.record_duel(
        db, user_id=patrick, title_a=1, title_b=3, outcome="TIE", context="profile_battle"
    )
    assert await battle.next_battle_pair(
        db, user_id=patrick, kinds=["movie"], rng=random.Random(0), reask_rate=0.0
    ) is None, "every pair in the band is compared, so the pool is drained"


async def test_a_first_sitting_never_pairs_two_disliked_titles(db, world):
    """Decision 493: below `EARLY_LABELS` live ratings a profile battle leaves the disliked band
    out. Duels 2-5 on the v20260925 install were disliked-vs-disliked, met in the first minutes
    by members who then switched to Sweep; §5.2 credits comparisons with resolution *within* the
    liked class. Three disliked titles and two liked: every draw is the liked pair."""
    patrick = world["patrick"]
    await rate_all(db, patrick, [1, 2, 3], value=0)
    await rate_all(db, patrick, [4, 6], value=2)
    for seed in range(40):
        pair = await battle.next_battle_pair(
            db, user_id=patrick, kinds=["movie"], rng=random.Random(seed), reask_rate=0.0
        )
        assert {pair.title_a, pair.title_b} == {4, 6}, pair
        assert pair.verdict_class == 2

    # Nothing but disliked titles is no pair at all in a first sitting.
    mia = world["mia"]
    await rate_all(db, mia, [1, 2, 3], value=0)
    assert await battle.next_battle_pair(
        db, user_id=mia, kinds=["movie"], rng=random.Random(0), reask_rate=0.0
    ) is None


def test_past_the_first_sitting_the_disliked_band_is_drawn_again():
    """Decision 493 is a first-sitting rule, not a new selection rule: from `EARLY_LABELS` live
    ratings on every band is drawn uniformly, as §6.1 and §0 row 6 have it."""
    early = battle.open_bands(POOL, labels=battle.EARLY_LABELS - 1)
    assert 10 not in {m.title_id for m in early}, "title 10 is the disliked singleton"
    assert {m.verdict_class for m in early} == {1, 2}
    later = battle.open_bands(POOL, labels=battle.EARLY_LABELS)
    assert later == list(POOL)


async def test_the_battle_pool_is_only_titles_that_are_both_seen_and_verdicted(db, world):
    """§6.1: "from the user's **seen** titles within **verdict** bands" — a conjunction.

    A verdict implies seen, so the interesting direction is the other one: §6.1's corrections
    row ("not seen: [left] [both] [right]") sets a side unseen, and that title must leave the
    pool even though its verdict row is still there.
    """
    patrick = world["patrick"]
    await rate_all(db, patrick, [1, 2, 3], value=2)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", patrick
    )  # seen, never rated
    pool = await battle.battle_pool(db, user_id=patrick, kinds=["movie"])
    assert sorted(m.title_id for m in pool) == [1, 2, 3], "5 is seen but carries no verdict"

    await observations.record_not_seen(db, user_id=patrick, title_id=3)
    pool = await battle.battle_pool(db, user_id=patrick, kinds=["movie"])
    assert sorted(m.title_id for m in pool) == [1, 2], "3 was corrected back to unseen"

    pair = await battle.next_battle_pair(
        db, user_id=patrick, kinds=["movie"], rng=random.Random(0), reask_rate=0.0
    )
    assert {pair.title_a, pair.title_b} == {1, 2}
    assert pair.verdict_class == 2
    assert pair.reason == "You rated both of these liked."

    assert (
        await battle.next_battle_pair(
            db, user_id=patrick, kinds=["series"], rng=random.Random(0), reask_rate=0.0
        )
        is None
    )


# --- §6.1 / §5.2: the class-balance widget ----------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "warns"),
    [
        ((0, 0, 0), False),
        ((1, 1, 1), False),
        ((5, 5, 5), False),
        ((3, 3, 9), False),      # exactly 60% — the measured threshold is not yet exceeded
        ((3, 3, 10), True),      # 62.5%
        ((2, 3, 12), True),      # §5.2's 60%-"liked" labeller
        ((12, 2, 1), True),      # the failure mode is class-generic, not "liked"-specific
        ((0, 61, 39), True),
        # Decision 491's floor: under fifteen labels no share is a habit yet.
        ((1, 0, 0), False),
        ((0, 0, 14), False),
        ((0, 0, 15), True),
    ],
)
def test_the_warning_appears_above_sixty_percent_and_is_absent_below(counts, warns):
    """§5.2: "a 60%-'liked' labeller gives up ~0.07 rho", and §6.1 hangs the widget's warning on
    exactly that figure -- once fifteen labels stand (decision 491).

    The boundary is the test: at exactly 60% the person has not yet given anything up, so a
    warning there would be a warning about an inequality sign. (3,3,9) and (3,3,10) differ by one
    tap and must differ in outcome, and so do (0,0,14) and (0,0,15) on the other axis.
    """
    result = balance.ClassBalance.of(counts)
    assert result.warn is warns
    assert (result.copy is not None) is warns
    assert result.counts == tuple(counts)
    assert sum(result.shares) == pytest.approx(1.0 if sum(counts) else 0.0)


def test_the_warning_does_not_arm_before_fifteen_labels():
    """Decision 491. The floor was 1, and one label is 100% of a distribution: both household
    members were told "Heavy on ..." by their first verdict and the warning switched on and off
    six and ten times in a sitting. A perfectly balanced labeller trips a 60% rule by chance
    100% of the time at one label, 77.8% at three and 2.6% at fifteen, so the number is one
    §6.1 block -- and the widget says when the check begins rather than going quiet."""
    assert balance.WARN_MIN_VERDICTS == 15
    for total in range(1, 15):
        assert balance.ClassBalance.of((0, 0, total)).warn is False, total
    assert balance.ClassBalance.of((0, 0, 15)).warn is True
    assert balance.ClassBalance.of((0, 0, 3)).as_dict()["arms_at"] == 15


def test_the_warning_copy_is_the_measured_sentence_and_names_the_heavy_class():
    """§6.1's measured sentence, restated by decision 491: "about five times more" is §5.2's 5x
    lever and stays verbatim, and the tail says how to spread for the class that is heavy."""
    result = balance.ClassBalance.of((2, 3, 12))
    assert result.copy == (
        "Heavy on 'liked'. Spreading your ratings across all three answers matters about five "
        "times more than anything else you can do here. Rate some titles you didn't enjoy as "
        "well - but never change an honest answer to even things out."
    )
    disliked = balance.ClassBalance.of((12, 2, 1)).copy
    assert disliked.startswith("Heavy on 'disliked'.")
    assert "Rate some titles you enjoyed as well" in disliked
    fine = balance.ClassBalance.of((2, 12, 1)).copy
    assert fine.startswith("Heavy on 'fine'.")
    assert "better or worse than fine, say so" in fine


@pytest.mark.parametrize("counts", [(12, 2, 1), (2, 12, 1), (1, 2, 12)])
def test_the_warning_never_asks_for_a_different_answer(counts):
    """Decision 491: the old sentence said what mattered and not how, so both members read it
    as "change your answers". Whatever class is heavy, the copy widens what gets rated and says
    in so many words that an honest answer stays. ASCII, like every line this surface prints."""
    copy = balance.ClassBalance.of(counts).copy
    assert "about five times more" in copy
    assert "never change an honest answer" in copy
    assert copy.isascii(), copy


async def test_the_widget_counts_one_current_label_per_title(db, world):
    """§6.1: "a running **class-balance widget**" over the person's labels.

    A re-rating replaces a label rather than adding one — the distribution is over titles, not
    over taps — so rating title 1 `liked` and then `disliked` must leave the total at one.
    """
    patrick = world["patrick"]
    await rate_all(db, patrick, [1, 2, 3], value=2)
    await observations.record_verdict(db, user_id=patrick, title_id=4, value=0)
    assert (await balance.class_balance(db, user_id=patrick, kinds=["movie"])).counts == (1, 0, 3)

    await observations.record_verdict(db, user_id=patrick, title_id=1, value=0)
    result = await balance.class_balance(db, user_id=patrick, kinds=["movie"])
    assert result.counts == (2, 0, 2), "a re-rating replaced a label rather than adding one"
    assert result.total == 4
    assert result.warn is False


# --- §13 stream (b): the silent re-ask ---------------------------------------------------------


async def test_the_served_payload_carries_no_marker_distinguishing_a_re_ask(db, world):
    """§13 stream (b) with decision-doc proposal 50: re-asks are "indistinguishable from a
    normal card by design, never labelled in the UI".

    Asserted over the **serialised payload**, not over the dataclass: `reask_of` is not the only
    give-away — a `source` of 'reask' is exactly as loud, and so is a why-line phrased
    differently from the one a genuinely pending card carries. The strongest form of the claim
    is the one asserted here: the two payloads are identical apart from the title id.
    """
    patrick = world["patrick"]
    await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    await backdate_verdicts(db, patrick, days=10)
    # A genuinely pending card: marked seen by §7.3's sync, never rated.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", patrick
    )

    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=6, rng=random.Random(4), reask_rate=1.0
    )
    by_source = {c.source: c for c in cards}
    assert by_source["reask"].title_id == 1
    assert by_source["reask"].reask_of is not None
    assert by_source["pending_verdict"].title_id == 5

    wire = json.dumps([c.public() for c in cards])
    for marker in ("reask", "is_reask", "reask_of", "source", "pending_verdict"):
        assert marker not in wire, f"the payload leaks {marker!r}: {wire}"

    again = by_source["reask"].public()
    pending = by_source["pending_verdict"].public()
    assert again["reason"] == pending["reason"] == queue.SEEN_REASON
    assert again["p_seen"] == pending["p_seen"] == 1.0
    assert set(again) == set(pending) == {"title_id", "reason", "p_seen"}


async def test_a_verdict_younger_than_three_days_is_never_re_asked(db, world):
    """§13: "~10% of comparisons/verdicts re-asked **after >=3 days**". The interval is the
    instrument: a same-day re-ask measures short-term memory, not test-retest consistency."""
    patrick = world["patrick"]
    await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    await backdate_verdicts(db, patrick, days=2)
    assert (
        await reask.verdict_candidates(
            db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
        )
        == []
    )

    await backdate_verdicts(db, patrick, days=3)
    candidates = await reask.verdict_candidates(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert [c.title_id for c in candidates] == [1]

    # …and with nothing eligible, the queue is still full: the stream never costs a question.
    await backdate_verdicts(db, patrick, days=2)
    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0), reask_rate=1.0
    )
    assert len(cards) == 5
    assert all(c.source != "reask" for c in cards)


async def test_the_re_ask_is_stored_distinguishably_and_sigma_is_computable_from_it(db, world):
    """§13 stream (b): "~200 re-asks measure the flip rate sigma that sets the tier budget".

    A stored field nobody can compute from is a comment in a column, so the row and the
    computation are asserted together: `is_reask` / `reask_of` on the write, and `flip_rate`
    reading the pair back as one observation of the same judgement asked twice.
    """
    patrick = world["patrick"]
    first = await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    steady = await observations.record_verdict(db, user_id=patrick, title_id=2, value=1)
    await backdate_verdicts(db, patrick, days=10)

    flipped = await observations.record_verdict(
        db, user_id=patrick, title_id=1, value=0, is_reask=True, reask_of=first.row_id
    )
    await observations.record_verdict(
        db, user_id=patrick, title_id=2, value=1, is_reask=True, reask_of=steady.row_id
    )

    row = await db.fetchrow(
        "SELECT is_reask, reask_of, source FROM verdict WHERE id = $1", flipped.row_id
    )
    assert row["is_reask"] is True and row["reask_of"] == first.row_id
    assert row["source"] == "sweep", "the stream hides in the ordinary source, not beside it"

    sigma = await reask.flip_rate(db, user_id=patrick)
    assert sigma.verdicts.n == 2 and sigma.verdicts.flips == 1
    assert sigma.duels.n == 0
    assert sigma.sigma == pytest.approx(0.5)
    assert sigma.sufficient is False, "§13 wants ~200 re-asks before sigma is worth quoting"
    assert sigma.as_dict()["target"] == 200


async def test_a_re_ask_is_not_a_second_observation_for_the_ledger_or_the_widget(db, world):
    """§13 stream (b): the re-ask stream is an instrument, and an instrument that moved the
    thing it measures would be measuring itself.

    Two consumers, one predicate: `load_observations` filters `NOT is_reask` out of the fit, and
    the class-balance widget reads the person's current label through the same rule. The re-ask
    row is still there — §13 needs it — it just does not count twice.
    """
    patrick = world["patrick"]
    first = await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    await rate_all(db, patrick, [2, 3], value=2)
    await backdate_verdicts(db, patrick, days=10)

    before_fit = await observations.load_observations(db, user_id=patrick, kind="movie", hp=DEFAULTS)
    before_widget = await balance.class_balance(db, user_id=patrick, kinds=["movie"])
    assert before_fit.n_verdicts == 3 and before_widget.counts == (0, 0, 3)

    await observations.record_verdict(
        db, user_id=patrick, title_id=1, value=0, is_reask=True, reask_of=first.row_id
    )

    after_fit = await observations.load_observations(db, user_id=patrick, kind="movie", hp=DEFAULTS)
    after_widget = await balance.class_balance(db, user_id=patrick, kinds=["movie"])
    assert after_fit.n_verdicts == 3, "the re-ask reached the fit as a second observation"
    assert after_fit.n_reask == 1, "…and it is still visible to §13's instrument"
    assert after_widget.counts == (0, 0, 3), "the re-ask moved the class-balance widget"

    # The battle band is the third consumer of the same rule: a flipped re-ask must not move a
    # title from the liked band into the disliked one.
    pool = await battle.battle_pool(db, user_id=patrick, kinds=["movie"])
    assert {m.verdict_class for m in pool} == {2}


async def test_the_same_verdict_is_not_re_asked_again_inside_the_cooldown(db, world):
    """Not in §13, decided in `reask`: without a cooldown a household with forty rated titles
    re-asks the same handful every sitting, and sigma becomes a reading about one title's mood
    rather than about the household's consistency."""
    patrick = world["patrick"]
    first = await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    await observations.record_verdict(db, user_id=patrick, title_id=2, value=1)
    await backdate_verdicts(db, patrick, days=10)

    assert len(
        await reask.verdict_candidates(
            db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
        )
    ) == 2

    await observations.record_verdict(
        db, user_id=patrick, title_id=1, value=2, is_reask=True, reask_of=first.row_id
    )
    still = await reask.verdict_candidates(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert [c.title_id for c in still] == [2]

    # Past the cooldown it becomes eligible again — the guard is a window, not a tombstone.
    later = datetime.now(UTC) + reask.REASK_COOLDOWN + timedelta(days=1)
    revived = await reask.verdict_candidates(
        db, user_id=patrick, kinds=["movie"], limit=5, now=later, rng=random.Random(0)
    )
    assert sorted(c.title_id for c in revived) == [1, 2]


async def test_a_duel_re_ask_preserves_the_order_it_was_asked_in(db, world):
    """§13 stream (b) covers "comparisons" too. The stored `(title_a, title_b)` order is
    preserved so a flip is literally `outcome <> original.outcome` with no normalisation, and
    whatever left/right position bias exists is constant across both asks and cancels out."""
    patrick = world["patrick"]
    # Three liked titles, not two: the pair a re-ask repeats is compared by definition, and the
    # ordinary draw the why-lines are compared against below needs a pair that is not (C5.4).
    await rate_all(db, patrick, [1, 2, 3], value=2)
    first = await observations.record_duel(
        db, user_id=patrick, title_a=2, title_b=1, outcome="A", context="profile_battle"
    )
    await db.execute("UPDATE duel SET created_at = now() - interval '10 days'")

    candidates = await reask.duel_candidates(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0)
    )
    assert len(candidates) == 1
    again = candidates[0]
    assert (again.title_a, again.title_b) == (2, 1), "the pair was re-ordered between asks"
    assert again.verdict_class == 2 and again.outcome == "A"

    pair = await battle.next_battle_pair(
        db, user_id=patrick, kinds=["movie"], rng=random.Random(0), reask_rate=1.0
    )
    assert (pair.title_a, pair.title_b) == (2, 1)
    assert pair.reask_of == first.row_id
    assert "reask" not in json.dumps(pair.public())
    ordinary = await battle.next_battle_pair(
        db, user_id=patrick, kinds=["movie"], rng=random.Random(0), reask_rate=0.0
    )
    assert pair.public()["reason"] == ordinary.public()["reason"]

    await observations.record_duel(
        db,
        user_id=patrick,
        title_a=2,
        title_b=1,
        outcome="B",
        context="profile_battle",
        is_reask=True,
        reask_of=first.row_id,
    )
    sigma = await reask.flip_rate(db, user_id=patrick)
    assert sigma.duels.n == 1 and sigma.duels.flips == 1


def test_about_one_slot_in_ten_is_a_re_ask():
    """§13: "**~10%** of comparisons/verdicts re-asked".

    Measured on the pure interleave over 40,000 slots with both pools kept deep, because the
    rate is the property and a handful of database round-trips cannot see it. Expected 4,000
    with a binomial sd of 60, so the +-5 sd band below is 3,700..4,300; a rate of 0 or of 1
    misses it by two orders of magnitude.
    """
    rng = random.Random(11)
    fresh = [queue.QueueCard(i, "why", 0.5, "p_seen", None) for i in range(100_000, 140_000)]
    asks = [
        reask.VerdictReask(verdict_id=i, title_id=i, value=1, asked_at=datetime.now(UTC))
        for i in range(200_000, 240_000)
    ]
    served = queue._interleave(
        fresh, asks, limit=40_000, rate=reask.REASK_RATE, rng=rng
    )
    reasks = sum(1 for c in served if c.source == "reask")
    assert len(served) == 40_000
    assert 3_700 < reasks < 4_300, f"{reasks}/40000 slots were re-asks"
    assert len({c.title_id for c in served}) == 40_000, "a title was served twice in one queue"


async def test_the_not_seen_rate_is_the_queue_bug_instrument(db, world):
    """§13: "not-seen rate in the rating queue (**>50% = queue bug**)".

    The instrument reads the append-only `rate_observation` journal rather than `user_title`,
    because `user_title` holds one row per (user, title) and a later "seen" erases the "not
    seen" that is the whole measurement. An undone tap is tombstoned and does not count.
    """
    patrick = world["patrick"]
    session_id = await db.fetchval(
        "INSERT INTO rate_session (user_id, kinds) VALUES ($1, ARRAY['movie']) RETURNING id",
        patrick,
    )
    answers = ["verdict", "not_seen", "not_seen", "verdict", "not_seen"]
    for seq, kind_of in enumerate(answers):
        await db.execute(
            """
            INSERT INTO rate_observation
                (session_id, user_id, seq, block_index, slot, kind_of, advances, card, title_ids)
            VALUES ($1, $2, $3, 0, $4, $5, true, '{}'::jsonb, ARRAY[$3]::int[])
            """,
            session_id,
            patrick,
            seq,
            seq + 1,
            kind_of,
        )

    rate = await queue.not_seen_rate(db, user_id=patrick)
    assert rate.answered == 5 and rate.not_seen == 3
    assert rate.rate == pytest.approx(0.6)
    assert rate.queue_bug is True, "§13 calls anything over 50% a queue bug"

    await db.execute(
        "UPDATE rate_observation SET undone_at = now() WHERE user_id = $1 AND seq = 4", patrick
    )
    assert (await queue.not_seen_rate(db, user_id=patrick)).rate == pytest.approx(0.5)
    assert (await queue.not_seen_rate(db, user_id=patrick)).queue_bug is False
    assert (await queue.not_seen_rate(db, user_id=world["mia"])).rate is None


# --- C5.2: the person's own pick --------------------------------------------------------------


async def test_the_search_ranks_the_name_a_person_remembers_first_and_says_what_is_rated(db, world):
    """Rate's "a title you know" (`rate/search.py`). An exact name first, then a name that starts
    with the query, then the rest; the catalogue grid orders by year and would have put every
    newer title with "heat" in its name above Heat. A hit already rated says so -- by the
    person's own label -- because the queue will not serve it and a pin that did nothing is the
    failure this search exists to end. The needle is matched literally (M4.9 finding 12)."""
    from spielplan.rate import search

    patrick = world["patrick"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES "
        "(51, 'movie', 'Heat', 1995, true), (52, 'movie', 'The Heat', 2013, false), "
        "(53, 'movie', 'Heatwave Summer', 2020, false), (54, 'series', 'Dead Heat', 2021, false), "
        "(55, 'movie', '100% Wolf', 2020, false)"
    )
    await db.execute(
        "INSERT INTO title_alias (title_id, alias) VALUES (52, 'Hot Pursuit Heat Edition')"
    )
    await observations.record_verdict(db, user_id=patrick, title_id=52, value=0)

    hits = await search.find(db, user_id=patrick, q="  Heat ")
    ids = [h["id"] for h in hits]
    assert ids[0] == 51, "the exact name first"
    assert ids[1] == 53, "then a name that starts with it"
    assert set(ids[2:]) == {52, 54}, "then every other match, both kinds"
    by_id = {h["id"]: h for h in hits}
    assert by_id[52]["rated"] == "disliked"
    assert by_id[51]["rated"] is None
    assert by_id[54]["kind"] == "series"

    assert [h["id"] for h in await search.find(db, user_id=patrick, q="100%")] == [55]
    assert await search.find(db, user_id=patrick, q="   ") == []
    assert len(await search.find(db, user_id=patrick, q="e", limit=2)) == 2


# --- §6's throughput budget -------------------------------------------------------------------


async def test_a_block_of_cards_is_drawn_well_inside_the_two_second_budget(db, capsys):
    """§6 preamble: "**<2 s per sweep card**, <1.5 s per battle, undo everywhere, next card
    preloaded."

    Measured on a 5,000-title catalog with priors on every one of them, because the candidate
    query is a scan with three correlated sub-selects per row and that only shows at scale.
    The number is printed so a regression is legible with `-s` rather than only as a red test.
    """
    import statistics
    import time

    n = 5_000
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, is_owned)
        SELECT g, CASE WHEN g % 5 = 0 THEN 'series' ELSE 'movie' END,
               'Title ' || g, 1960 + (g % 66), (g % 3 = 0)
        FROM generate_series(1, $1) g
        """,
        n,
    )
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('t1', '{}', 'active')"
    )
    await db.execute(
        """
        INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source)
        SELECT g, 't1', 0.5, 0.5, (g * 37) % 200000,
               ((g * 37) % 200000)::real / (((g * 37) % 200000) + 10), 'backbone'
        FROM generate_series(1, $1) g
        """,
        n,
    )
    patrick = await make_user(db, "patrick", "admin")

    timings = []
    for _ in range(20):
        started = time.perf_counter()
        cards = await queue.next_sweep_cards(
            db, user_id=patrick, kinds=["movie", "series"], limit=15, rng=random.Random(0)
        )
        timings.append((time.perf_counter() - started) * 1000)
        assert len(cards) == 15

    with capsys.disabled():
        print(
            f"\n  §6.1 queue over {n} titles, a block of 15: "
            f"median {statistics.median(timings):.1f} ms, "
            f"p95 {sorted(timings)[18]:.1f} ms, max {max(timings):.1f} ms  (budget 2,000 ms/card)"
        )
    assert statistics.median(timings) < 2_000


def test_a_head_pinned_slot_is_never_spent_on_a_reask():
    """§6.0's pending-verdicts banner names up to three titles and its CTA "opens the §6.1 queue
    with those titles at the head of the queue" — because "naming titles and then presenting a
    different card is worse than no prompt".

    The pin is applied upstream as an ORDER BY, and §13's stream then replaced the drawn card
    with a re-ask on ~10% of taps. The banner named three films and the surface served a fourth,
    with nothing to distinguish the substitution — §13 requires exactly that invisibility, so
    the person had no way to tell the CTA had not worked.

    `rate = 1.0` forces the coin every time, so this is deterministic rather than a 10% flake.
    """
    from spielplan.rate import queue as q
    from spielplan.rate.reask import VerdictReask

    fresh = [
        q.QueueCard(title_id=41, reason="r", p_seen=1.0, source="pending_verdict", reask_of=None),
        q.QueueCard(title_id=57, reason="r", p_seen=1.0, source="pending_verdict", reask_of=None),
        q.QueueCard(title_id=99, reason="r", p_seen=0.4, source="p_seen", reask_of=None),
    ]
    reasks = [VerdictReask(verdict_id=7, title_id=1012, value=2, asked_at=None)]

    out = q._interleave(
        fresh, reasks, limit=3, rate=1.0, rng=random.Random(0), head=(41, 57)
    )
    assert [c.title_id for c in out][:2] == [41, 57], (
        "the two titles the banner named must be served first, whatever the re-ask coin says"
    )

    # And §13 still gets every slot that is not pinned — the stream is protected, not disabled.
    assert 1012 in [c.title_id for c in out], "the re-ask should take the first unpinned slot"

    # Without a pin, the same call spends the first slot on the re-ask, which is the behaviour
    # §13 asks for and the reason this needed an exception rather than a rewrite.
    unpinned = q._interleave(fresh, reasks, limit=3, rate=1.0, rng=random.Random(0))
    assert unpinned[0].title_id == 1012
