from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from spielplan.ledger import observations
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rate import balance, battle, queue, reask

# Distinct, far-apart crowd counts so P(seen) order and id order never coincide.
ITEM_N = {1: 180_000, 2: 42_000, 3: 9_000, 4: 3_000, 5: 300, 6: 12_000, 7: 25_000, 8: 900}
YEARS = {1: 1995, 2: 2010, 3: 1982, 4: 1994, 5: 2021, 6: 2016, 7: 1999, 8: 1975}
# Neither id order nor popularity order, so seed order is a third list.
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
    return min(1.0, max(0.0, (datetime.now(UTC).year - YEARS[title_id]) / queue.AGE_SATURATION_YEARS))


def expected_p(
    title_id: int, *, owned: bool = True, co_seen: float = 0.0, playback: bool = False
) -> float:
    """A third, independent spelling of P(seen): SQL orders, Python explains, this checks both."""
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


async def test_a_fresh_households_first_queue_is_the_seed_list_most_likely_seen_first(db, world):
    """Decision 490: P(seen) orders inside the seed list. Computed from `expected_p`, not pinned:
    the age term moves with the calendar."""
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
        # P(seen) travels with the card but the sentence does not print it (A3 of 2026-09-26).
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
    """Decision 490: the list keeps precedence until consumed, even over higher P(seen) outside it."""
    cards = await queue.next_sweep_cards(
        db, user_id=world["patrick"], kinds=["movie"], limit=6, reask_rate=0.0
    )
    seeds = {t for t, _ in SEED}
    outside = max((3, 5, 7), key=expected_p)
    assert expected_p(outside) > min(expected_p(t) for t in seeds), "the fixture must test it"
    assert {c.title_id for c in cards[:5]} == seeds
    assert cards[5].title_id == outside and cards[5].source == "p_seen"


async def test_a_seed_title_in_the_library_leads_one_that_is_not(db, world):
    """Decision 490: the library is a seed-order signal; unowned seeds drew most "not seen" answers."""
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
    """A pin lifts an earlier "not seen" (C5.2); a rated title stays out pinned or not."""
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
    """Computed from `expected_p`, not pinned, as the age term moves; three distinct probabilities
    rule out a constant P(seen)."""
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
        assert card.p_seen == pytest.approx(expected_p(card.title_id), abs=1e-9)


async def test_a_cold_masked_titles_crowd_count_still_reaches_the_popularity_term(db, world):
    """`title_prior.item_n` is the crowd count, distinct from §5.1's n_t: a cold-masked row's gate
    is 0, but its popularity term must stay."""
    patrick = world["patrick"]
    # Title 7 is the best-supported film outside the seed list, so the first card tests popularity.
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
    # 2.0 logits is the whole weight: the difference between offering the card and burying it.
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
    """Co-seen is worth 1.2 log-odds against a widest gap of 1.12; playback is worth 2.5."""
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
    """Shrunk towards the person's own seen rate by two pseudo-answers each way, read only below it
    (decision 521)."""
    half = (20_000, 40_000)                       # the kind's answers: half of them seen
    assert queue.unfamiliarity(0, 0, *half) == 0.0, "no answers, no opinion"
    assert queue.unfamiliarity(0, 1, *half) == pytest.approx(-0.2)
    assert queue.unfamiliarity(0, 3, *half) == pytest.approx(2 * (2 / 7 - 0.5))
    assert (
        queue.unfamiliarity(0, 30, *half)
        < queue.unfamiliarity(0, 3, *half)
        < queue.unfamiliarity(0, 1, *half)
    )
    assert queue.unfamiliarity(0, 10_000, *half) > -1.0
    for seen, answered in ((1, 2), (5, 5), (16, 21), (40, 41)):
        assert queue.unfamiliarity(seen, answered, *half) == 0.0, "a known language is never raised"
    # The weight is positive and the feature is not, so it can only lower P(seen).
    assert queue.WEIGHTS.unfamiliar > 0
    base = queue.Features(owned=True, crowd=0.5)
    assert queue.p_seen(base) > queue.p_seen(
        queue.Features(owned=True, crowd=0.5, unfamiliar=queue.unfamiliarity(0, 3, *half))
    )
    assert queue.dominant(queue.Features(unfamiliar=-0.5)) is None, "it is never the named cause"


def test_unfamiliarity_is_read_against_the_persons_own_seen_rate():
    """Read against a fixed half, it lowered every English film for a low-rate member (finding F3)."""
    assert queue.unfamiliarity(10, 40, 10, 40) == 0.0, "one language at their own rate"
    assert queue.unfamiliarity(0, 0, 10, 40) == 0.0, "an unasked language"
    assert queue.unfamiliarity(0, 12, 0, 12) == 0.0, "all 'not seen' is the person, not a language"
    # Two languages: 16 of 21 English series seen and 0 of 3 Japanese.
    kind = (16, 24)
    assert queue.unfamiliarity(16, 21, *kind) == 0.0, "the language they know is not lowered"
    japanese = queue.unfamiliarity(0, 3, *kind)
    assert japanese == pytest.approx(2 * (4 * 16 / 24 / 7 - 16 / 24))
    # The same three misses say less about a person who rarely knows what they are asked.
    assert japanese < queue.unfamiliarity(0, 3, 4, 24) < 0.0


async def test_a_persons_not_seen_answers_lower_that_languages_titles_and_nothing_else(db, world):
    """A member whose every answer is "not seen" lowers nothing: that is their rate, not a language's.
    The why-line is unchanged: the term corrects ordering and is not a cause."""
    patrick, mia = world["patrick"], world["mia"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned, original_language) VALUES "
        "(51, 'series', 'Anime 1', 2010, true, 'ja'), (52, 'series', 'Anime 2', 2010, true, 'ja'),"
        "(53, 'series', 'Anime 3', 2010, true, 'ja'), (54, 'series', 'Anime 4', 2010, true, 'ja'),"
        "(55, 'series', 'Drama 1', 2010, true, 'en'), (56, 'series', 'Drama 2', 2010, true, 'en'),"
        "(57, 'series', 'Drama 3', 2010, true, 'en'), (58, 'series', 'Drama 4', 2010, true, 'en')"
    )
    await rate_all(db, patrick, (57, 58))

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
            queue.Features(owned=True, age=age, unfamiliar=queue.unfamiliarity(0, 3, 2, 5))
        ),
        abs=1e-9,
    ), "SQL orders and Python explains: the two spellings of the term agree"
    assert after[55].p_seen == pytest.approx(before[55].p_seen), "nothing else moves"
    assert after[54].reason == before[54].reason == "It's in your library."

    theirs = await queue_for(mia)
    assert theirs[54].p_seen == pytest.approx(before[54].p_seen), "Patrick's answers are his own"

    # Mia says "not seen" to the only series she is asked about: her rate, not English's.
    await observations.record_not_seen(db, user_id=mia, title_id=55)
    hers = await queue_for(mia)
    assert hers[56].p_seen == pytest.approx(theirs[56].p_seen), "a uniform 'not seen' moves nothing"
    assert hers[54].p_seen == pytest.approx(theirs[54].p_seen)


async def test_a_title_the_app_already_holds_as_seen_leads_the_queue(db, world):
    """A record, not an estimate: P(seen) is 1.0."""
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
    """The line names the dominant cause and not its probability (A3 of 2026-09-26; decision 486)."""
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
    # Every fixture film is owned: title 5 (300 ratings) is named for the library, not as well-known.
    reasons = {c.title_id: c.reason for c in cards}
    assert reasons[5] == "It's in your library.", reasons
    assert set(reasons.values()) <= {"A well-known film.", "It's in your library."}, reasons


async def test_the_age_why_line_prints_the_titles_real_age_and_not_the_saturation_point(db, world):
    """The printed age is the real age, not the feature clipped at `AGE_SATURATION_YEARS`. Counted
    on Postgres's clock: a Python clock disagrees across midnight or a wrong container TZ."""
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

    # The ordering feature is still clipped at 1.0; only the copy changed.
    clipped = queue.p_seen(queue.Features(owned=False, age=1.0))
    assert cards[41].p_seen == pytest.approx(clipped)
    assert cards[42].p_seen == pytest.approx(
        queue.p_seen(queue.Features(owned=False, age=(this_year - 2021) / queue.AGE_SATURATION_YEARS))
    )
    assert cards[41].p_seen > cards[42].p_seen, "and the older film still sorts first"


async def test_a_title_already_rated_or_explicitly_not_seen_never_returns(db, world):
    """"Not seen" is an answer and is not asked again; an adopted unseen is an absent row, so this
    removes only what the person said."""
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

    await rate_all(db, patrick, sorted(seen_ids))
    assert await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=8, rng=random.Random(0)
    ) == []


async def test_the_banner_cta_pins_its_titles_to_the_front_and_exclude_holds_them_out(db, world):
    """`head` is the banner CTA's pin; `exclude` is the sitting's memory of cards already served."""
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
    """The empty selection is the unpartitioned query §4.1 rule 5 forbids."""
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


# movie/2 holds 4 members (6 pairs), movie/1 3 (3 pairs), series/2 2 (1 pair), and movie/0 is
# a singleton. Unequal strata separate "uniform over pairs" from the wrong samplers.
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
    """§0 row 6: no rule beats random. 27.88 is chi-square's 0.999 quantile at 9 df; the seed is
    fixed. Uniform-over-strata and member-count weighting both fail."""
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
    """Lower id always left would bake id order into every duel's position bias."""
    rng = random.Random(7)
    firsts = [battle.draw(POOL, rng=rng)[0:2] for _ in range(4_000)]
    lower_first = sum(1 for a, b in firsts if a < b)
    # Expected 2,000 with a binomial sd of ~32; +-6 sd is 1,810..2,190.
    assert 1_810 < lower_first < 2_190, f"position is not randomised: {lower_first}/4000"


def test_a_pair_never_crosses_a_verdict_class_or_a_kind():
    """`observations.record_duel` refuses a cross-kind duel."""
    band = {m.title_id: (m.kind, m.verdict_class) for m in POOL}
    rng = random.Random(3)
    for _ in range(2_000):
        a, b, kind, verdict_class = battle.draw(POOL, rng=rng)
        assert band[a] == band[b] == (kind, verdict_class)
        assert a != b


def test_no_pair_exists_until_one_class_holds_two_titles():
    """Three titles in three different classes is still no pair."""
    rng = random.Random(1)
    assert battle.draw([], rng=rng) is None
    assert battle.draw([battle.PoolMember(1, "movie", 2)], rng=rng) is None
    spread = [battle.PoolMember(i + 1, "movie", i) for i in range(3)]
    assert battle.draw(spread, rng=rng) is None
    split = [battle.PoolMember(1, "movie", 2), battle.PoolMember(2, "series", 2)]
    assert battle.draw(split, rng=rng) is None, "same class, different kinds, is not a pair"


def test_the_battle_why_line_names_the_shared_answer_and_no_section():
    """The line says the one fact that makes the pair fair, in decision 486's register; how pairs
    are drawn is the rail's to say."""
    for verdict_class, label in enumerate(("disliked", "fine", "liked")):
        line = battle.reason_for(verdict_class)
        assert line == f"You rated both of these {label}.", line
        assert "queued because" not in line and "profile" not in line, line
        assert "§" not in line and "tier queue" not in line, line
        assert "only pay off" not in line, "the clause 54a deleted is back"
        assert len(line) < 80, f"the why-line has to fit a phone card: {len(line)} chars"


def test_a_pair_already_answered_is_not_drawn_again():
    """A repeat is an independent Davidson row; only §13's re-ask stream brings a pair back."""
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
    """The answered set is all of movie/2, which a sampler weighting by FULL pair count keeps
    picking. 16.27 is the 0.999 quantile at 3 df; the seed is fixed."""
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
    """A nearly exhausted band falls past `draw`'s rejection budget to the enumerated remainder."""
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
    """Every compared pair, in any context, as `rank/read.asked_pairs` reads them."""
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
    """Decision 493: below `EARLY_LABELS` live ratings the disliked band is left out."""
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
    early = battle.open_bands(POOL, labels=battle.EARLY_LABELS - 1)
    assert 10 not in {m.title_id for m in early}, "title 10 is the disliked singleton"
    assert {m.verdict_class for m in early} == {1, 2}
    later = battle.open_bands(POOL, labels=battle.EARLY_LABELS)
    assert later == list(POOL)


async def test_the_battle_pool_is_only_titles_that_are_both_seen_and_verdicted(db, world):
    """A side set unseen by the corrections row leaves the pool though its verdict remains."""
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
    """At exactly 60% nothing is given up yet, so (3,3,9) and (3,3,10) differ; so do (0,0,14) and
    (0,0,15) on the floor."""
    result = balance.ClassBalance.of(counts)
    assert result.warn is warns
    assert (result.copy is not None) is warns
    assert result.counts == tuple(counts)
    assert sum(result.shares) == pytest.approx(1.0 if sum(counts) else 0.0)


def test_the_warning_does_not_arm_before_fifteen_labels():
    """Decision 491: a balanced labeller trips 60% by chance at one label always, 2.6% at fifteen."""
    assert balance.WARN_MIN_VERDICTS == 15
    for total in range(1, 15):
        assert balance.ClassBalance.of((0, 0, total)).warn is False, total
    assert balance.ClassBalance.of((0, 0, 15)).warn is True
    assert balance.ClassBalance.of((0, 0, 3)).as_dict()["arms_at"] == 15


def test_the_warning_copy_is_the_measured_sentence_and_names_the_heavy_class():
    """Decision 491: "about five times more" is §5.2's 5x lever, kept verbatim."""
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
    """Decision 491: the copy never asks for a different answer. ASCII, like every line here."""
    copy = balance.ClassBalance.of(counts).copy
    assert "about five times more" in copy
    assert "never change an honest answer" in copy
    assert copy.isascii(), copy


async def test_the_widget_counts_one_current_label_per_title(db, world):
    """A re-rating replaces a label: the distribution is over titles, not taps."""
    patrick = world["patrick"]
    await rate_all(db, patrick, [1, 2, 3], value=2)
    await observations.record_verdict(db, user_id=patrick, title_id=4, value=0)
    assert (await balance.class_balance(db, user_id=patrick, kinds=["movie"])).counts == (1, 0, 3)

    await observations.record_verdict(db, user_id=patrick, title_id=1, value=0)
    result = await balance.class_balance(db, user_id=patrick, kinds=["movie"])
    assert result.counts == (2, 0, 2), "a re-rating replaced a label rather than adding one"
    assert result.total == 4
    assert result.warn is False


async def test_the_served_payload_carries_no_marker_distinguishing_a_re_ask(db, world):
    """Over the serialised payload: a 'reask' `source` or a different why-line would give it away.
    The two payloads must be identical apart from the title id."""
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
    """A same-day re-ask measures short-term memory, not test-retest consistency."""
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

    # With nothing eligible the queue is still full: the stream never costs a question.
    await backdate_verdicts(db, patrick, days=2)
    cards = await queue.next_sweep_cards(
        db, user_id=patrick, kinds=["movie"], limit=5, rng=random.Random(0), reask_rate=1.0
    )
    assert len(cards) == 5
    assert all(c.source != "reask" for c in cards)


async def test_the_re_ask_is_stored_distinguishably_and_sigma_is_computable_from_it(db, world):
    """A stored field nobody can compute from is a comment in a column; `flip_rate` must read it."""
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
    """An instrument that moved what it measures would measure itself: the fit and the widget both
    filter `NOT is_reask`."""
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

    # The battle band is the third consumer: a flipped re-ask must not move a title's band.
    pool = await battle.battle_pool(db, user_id=patrick, kinds=["movie"])
    assert {m.verdict_class for m in pool} == {2}


async def test_the_same_verdict_is_not_re_asked_again_inside_the_cooldown(db, world):
    """Without a cooldown a small library re-asks the same handful every sitting."""
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
    """Order preserved: a flip is literally `outcome <> original.outcome`, and position bias cancels."""
    patrick = world["patrick"]
    # Three liked titles: the ordinary draw compared against needs a pair not yet compared (C5.4).
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
    """The pure interleave over 40,000 slots: expected 4,000, sd 60, band 3,700..4,300."""
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
    """Reads the append-only `rate_observation` journal: a later "seen" erases `user_title`'s
    "not seen". An undone tap does not count."""
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


async def test_the_search_ranks_the_name_a_person_remembers_first_and_says_what_is_rated(db, world):
    """Exact name, then prefix, then the rest; a rated hit says so, because the queue will not serve
    it. The needle is matched literally (M4.9 finding 12)."""
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


async def test_a_block_of_cards_is_drawn_well_inside_the_two_second_budget(db, capsys):
    """5,000 titles: the candidate query's three correlated sub-selects per row only show at scale.
    Printed so a regression is legible with `-s`."""
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
    """The banner names its titles, so a re-ask must never take a pinned slot. `rate = 1.0` forces
    the coin, so this is deterministic."""
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

    # Without a pin the re-ask takes the first slot, as §13 asks.
    unpinned = q._interleave(fresh, reasks, limit=3, rate=1.0, rng=random.Random(0))
    assert unpinned[0].title_id == 1012
