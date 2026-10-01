from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from spielplan.ledger import ladder, observations
from spielplan.rate import queue, reask
from tests.helpers import insert_user

# Distinct, far-apart crowd counts so P(seen) order and id order never coincide.
ITEM_N = {1: 180_000, 2: 42_000, 3: 9_000, 4: 3_000, 5: 300, 6: 12_000, 7: 25_000, 8: 900}
YEARS = {1: 1995, 2: 2010, 3: 1982, 4: 1994, 5: 2021, 6: 2016, 7: 1999, 8: 1975}
# Neither id order nor popularity order, so seed order is a third list.
SEED = [(8, 1970), (4, 1990), (1, 1990), (2, 2010), (6, 2010)]


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
        "patrick": await insert_user(db, "patrick", "admin"),
        "mia": await insert_user(db, "mia", "member"),
    }


@pytest.fixture
async def world(db):
    return await make_world(db)


def _age(title_id: int) -> float:
    return min(1.0, max(0.0, (datetime.now(UTC).year - YEARS[title_id]) / queue.AGE_SATURATION_YEARS))


def p_seen(features: queue.Features) -> float:
    """The logistic `_CANDIDATES` computes, from the terms the why-line reads: SQL orders, this checks."""
    z = queue.WEIGHTS.intercept + sum(queue.contributions(features).values())
    return 1.0 / (1.0 + math.exp(-z))


def expected_p(
    title_id: int, *, owned: bool = True, co_seen: float = 0.0, playback: bool = False
) -> float:
    crowd = min(1.0, math.log1p(ITEM_N[title_id]) / math.log1p(queue.CROWD_SATURATION))
    age = min(1.0, max(0.0, (datetime.now(UTC).year - YEARS[title_id]) / queue.AGE_SATURATION_YEARS))
    return p_seen(
        queue.Features(playback=playback, co_seen=co_seen, crowd=crowd, owned=owned, age=age)
    )


async def place_all(db, user_id, title_ids, tier=4):
    for title_id in title_ids:
        await ladder.place(db, user_id=user_id, title_id=title_id, tier=tier)


async def backdate_placements(db, user_id, days):
    await db.execute(
        "UPDATE tier_edit SET created_at = now() - ($2 || ' days')::interval WHERE user_id = $1",
        user_id,
        str(days),
    )


async def test_a_fresh_households_first_queue_is_the_seed_list_most_likely_seen_first(db, world):
    """Decision 490: P(seen) orders inside the seed list. Computed from `expected_p`, not pinned:
    the age term moves with the calendar."""
    cards = await queue.next_cards(
        db, user_id=world["patrick"], kind="movie", limit=5, rng=random.Random(0)
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
    cards = await queue.next_cards(
        db, user_id=world["patrick"], kind="movie", limit=6, reask_rate=0.0
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
        for c in await queue.next_cards(
            db, user_id=patrick, kind="movie", limit=5, reask_rate=0.0
        )
    ]
    await db.execute("UPDATE title SET is_owned = false WHERE id = 2")
    after = [
        c.title_id
        for c in await queue.next_cards(
            db, user_id=patrick, kind="movie", limit=5, reask_rate=0.0
        )
    ]
    assert before.index(2) < before.index(8) and before.index(2) < before.index(6)
    assert after.index(2) > after.index(8) and after.index(2) > after.index(6), after


async def test_a_pinned_title_is_served_even_after_a_not_seen_answer_or_a_placement(db, world):
    """A pin lifts an earlier "not seen" and a placement: a rewatch is placed again (decision 550)."""
    patrick = world["patrick"]
    await observations.record_not_seen(db, user_id=patrick, title_id=3)
    await ladder.place(db, user_id=patrick, title_id=5, tier=3)

    unpinned = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=8, reask_rate=0.0
    )
    assert 3 not in [c.title_id for c in unpinned]

    pinned = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=8, head=[3], reask_rate=0.0
    )
    assert pinned[0].title_id == 3
    assert pinned[0].source == "pinned"
    assert pinned[0].reason == queue.PINNED_REASON

    unpinned = [c.title_id for c in unpinned]
    assert 5 not in unpinned, "a placed title never returns unpinned"
    placed = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=8, head=[5], reask_rate=0.0
    )
    assert placed[0].title_id == 5 and placed[0].reask_of is None
    assert placed[0].reason == queue.SEEN_REASON, "a placement made it seen"


async def test_once_the_seed_list_is_answered_the_queue_is_ordered_by_descending_p_seen(db, world):
    """Computed from `expected_p`, not pinned, as the age term moves; three distinct probabilities
    rule out a constant P(seen)."""
    patrick = world["patrick"]
    await place_all(db, patrick, [t for t, _ in SEED])

    remaining = [3, 5, 7]
    expected = sorted(remaining, key=expected_p, reverse=True)
    assert expected != remaining, "the fixture no longer separates P(seen) order from id order"

    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
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
    await place_all(db, patrick, [t for t, _ in SEED])

    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=8, rng=random.Random(0)
    )
    card = next(c for c in cards if c.title_id == 7)
    assert card.source == "p_seen"
    assert card.p_seen == pytest.approx(expected_p(7), abs=1e-9), (
        "the gate is 0 for this row and the popularity term must not read the gate"
    )
    # 2.0 logits is the whole weight: the difference between offering the card and burying it.
    crowd = math.log1p(ITEM_N[7]) / math.log1p(queue.CROWD_SATURATION)
    blind = p_seen(queue.Features(crowd=0.0, owned=True, age=_age(7)))
    assert card.p_seen == pytest.approx(
        p_seen(queue.Features(crowd=crowd, owned=True, age=_age(7))), abs=1e-9
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
    await place_all(db, patrick, [t for t, _ in SEED])
    baseline = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    )
    assert [c.title_id for c in baseline][-1] == 5
    assert [c.title_id for c in baseline] != [3, 5, 7], "this is the id order, not a P(seen) one"

    # §6.1's "household co-seen": mia has seen title 5, patrick has never been asked.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", mia
    )
    with_co_seen = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
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
    with_playback = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    )
    assert with_playback[0].title_id == 3
    assert with_playback[0].reason == "You played it to the end."
    assert with_playback[0].p_seen == pytest.approx(expected_p(3, playback=True), abs=1e-9)


async def test_a_persons_not_seen_answers_lower_that_languages_titles_and_nothing_else(db, world):
    """Decision 521: shrunk towards the person's own seen rate by two pseudo-answers each way, and
    read only below it. A member whose every answer is "not seen" lowers nothing: that is their
    rate, not a language's. The why-line is unchanged: the term corrects ordering and is not a cause."""
    patrick, mia = world["patrick"], world["mia"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned, original_language) VALUES "
        "(51, 'series', 'Anime 1', 2010, true, 'ja'), (52, 'series', 'Anime 2', 2010, true, 'ja'),"
        "(53, 'series', 'Anime 3', 2010, true, 'ja'), (54, 'series', 'Anime 4', 2010, true, 'ja'),"
        "(55, 'series', 'Drama 1', 2010, true, 'en'), (56, 'series', 'Drama 2', 2010, true, 'en'),"
        "(57, 'series', 'Drama 3', 2010, true, 'en'), (58, 'series', 'Drama 4', 2010, true, 'en')"
    )
    await place_all(db, patrick, (57, 58))

    async def queue_for(user):
        cards = await queue.next_cards(
            db, user_id=user, kind="series", limit=10, exclude=(9, 10), reask_rate=0.0
        )
        return {card.title_id: card for card in cards}

    before = await queue_for(patrick)
    assert before[54].p_seen == pytest.approx(before[55].p_seen), "the fixture is otherwise even"

    await observations.record_not_seen(db, user_id=patrick, title_id=51)
    one = await queue_for(patrick)
    for title_id in (52, 53):
        await observations.record_not_seen(db, user_id=patrick, title_id=title_id)
    after = await queue_for(patrick)
    assert after[54].p_seen < one[54].p_seen < before[54].p_seen, "each miss says it more firmly"
    assert set(after) == {54, 55, 56}, "an answered 'not seen' is never asked again"
    assert list(after)[-1] == 54, "the fourth Japanese series now waits behind the English two"
    age = min(1.0, (datetime.now(UTC).year - 2010) / queue.AGE_SATURATION_YEARS)
    # 0 of 3 Japanese against a kind rate of 2/5, shrunk by 2 x 2 pseudo-answers, doubled.
    unfamiliar = 2 * ((2 * 2 * 2 / 5) / (3 + 2 * 2) - 2 / 5)
    assert after[54].p_seen == pytest.approx(
        p_seen(queue.Features(owned=True, age=age, unfamiliar=unfamiliar)), abs=1e-9
    )
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
    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=3, rng=random.Random(0)
    )
    assert cards[0].title_id == 5
    assert cards[0].source == "seen"
    assert cards[0].p_seen == 1.0
    assert cards[0].reason == queue.SEEN_REASON
    assert cards[1].source == "seed", "the seed list resumes underneath the recorded-seen card"


async def test_every_card_carries_the_one_line_why_that_names_its_dominant_cause(db, world):
    """The line names the dominant cause and not its probability (A3 of 2026-09-26; decision 486)."""
    patrick = world["patrick"]
    await place_all(db, patrick, [t for t, _ in SEED])
    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
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
        for card in await queue.next_cards(
            db,
            user_id=patrick,
            kind="movie",
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
    clipped = p_seen(queue.Features(owned=False, age=1.0))
    assert cards[41].p_seen == pytest.approx(clipped)
    assert cards[42].p_seen == pytest.approx(
        p_seen(queue.Features(owned=False, age=(this_year - 2021) / queue.AGE_SATURATION_YEARS))
    )
    assert cards[41].p_seen > cards[42].p_seen, "and the older film still sorts first"


async def test_a_title_already_placed_or_explicitly_not_seen_never_returns(db, world):
    """"Not seen" is an answer and is not asked again; an adopted unseen is an absent row, so this
    removes only what the person said."""
    patrick = world["patrick"]
    await ladder.place(db, user_id=patrick, title_id=8, tier=3)
    await observations.record_not_seen(db, user_id=patrick, title_id=4)

    seen_ids = {
        c.title_id
        for c in await queue.next_cards(
            db, user_id=patrick, kind="movie", limit=8, rng=random.Random(0)
        )
    }
    assert 8 not in seen_ids and 4 not in seen_ids
    assert seen_ids == {1, 2, 3, 5, 6, 7}

    await place_all(db, patrick, sorted(seen_ids))
    assert await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=8, rng=random.Random(0)
    ) == []


async def test_the_banner_cta_pins_its_titles_to_the_front_and_exclude_holds_them_out(db, world):
    """`head` is the banner CTA's pin; `exclude` is the sitting's memory of cards already served."""
    patrick = world["patrick"]
    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=4, head=[7, 3], rng=random.Random(0)
    )
    assert [c.title_id for c in cards][:2] == [7, 3]

    later = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=4, head=[7, 3], exclude=[7], rng=random.Random(0)
    )
    assert 7 not in [c.title_id for c in later]
    assert later[0].title_id == 3


async def test_the_queue_partitions_by_kind(db, world):
    """One kind at a time: anything else is the unpartitioned query §4.1 rule 5 forbids."""
    patrick = world["patrick"]
    films = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=20, rng=random.Random(0)
    )
    series = await queue.next_cards(
        db, user_id=patrick, kind="series", limit=20, rng=random.Random(0)
    )
    assert {c.title_id for c in films} == set(range(1, 9))
    assert {c.title_id for c in series} == {9, 10}
    with pytest.raises(ValueError, match="kind must be one of"):
        await queue.next_cards(db, user_id=patrick, kind="both", limit=5)


# --- films rated before the set-up (decisions 537, 550) ------------------------------------------


async def test_films_rated_before_the_set_up_come_first_behind_a_pin_newest_answer_first(db, world):
    """After the cut-over a seen film with an old answer and no new step leads the queue, with a
    sentence that never names the old answer; before it, nothing is "rated before"."""
    patrick = world["patrick"]
    await observations.record_verdict(db, user_id=patrick, title_id=3, value=0)
    await observations.record_tier_edit(db, user_id=patrick, title_id=7, tier=6)
    await db.execute("UPDATE verdict SET created_at = now() - interval '2 days'")
    await db.execute("UPDATE tier_edit SET created_at = now() - interval '1 day'")
    before = await queue.next_cards(db, user_id=patrick, kind="movie", limit=8, reask_rate=0.0)
    assert "rated_before" not in {c.source for c in before}
    assert 7 not in [c.title_id for c in before], "before the set-up its step stands"

    await db.execute("INSERT INTO ladder_setup (user_id) VALUES ($1)", patrick)
    cards = await queue.next_cards(db, user_id=patrick, kind="movie", limit=8, reask_rate=0.0)
    assert [c.title_id for c in cards[:2]] == [7, 3], "the newest old answer first"
    for card in cards[:2]:
        assert (card.source, card.reason) == ("rated_before", queue.RATED_BEFORE_REASON)
        assert card.p_seen == 1.0
    assert cards[2].source == "seed"
    pinned = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=3, head=[1], reask_rate=0.0
    )
    assert [c.title_id for c in pinned] == [1, 7, 3]

    # A new step takes a film off the list, and so does Not seen (plan reading 12).
    await ladder.place(db, user_id=patrick, title_id=7, tier=5)
    await observations.record_not_seen(db, user_id=patrick, title_id=3)
    after = await queue.next_cards(db, user_id=patrick, kind="movie", limit=8, reask_rate=0.0)
    assert not {7, 3} & {c.title_id for c in after}


# --- the re-ask stream (§13 stream b) -------------------------------------------------------------


async def _first_edit(db, user_id: int, title_id: int) -> int:
    return await db.fetchval(
        "SELECT min(id) FROM tier_edit WHERE user_id = $1 AND title_id = $2", user_id, title_id
    )


async def test_a_placement_younger_than_three_days_is_never_re_asked(db, world):
    """A same-day re-ask measures short-term memory, not test-retest consistency."""
    patrick = world["patrick"]
    await place_all(db, patrick, [1], tier=5)
    await backdate_placements(db, patrick, days=2)
    assert await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    ) == []

    await backdate_placements(db, patrick, days=3)
    candidates = await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    )
    assert [(c.title_id, c.tier, c.tier_edit_id) for c in candidates] == [
        (1, 5, await _first_edit(db, patrick, 1))
    ]

    # With nothing eligible the queue is still full: the stream never costs a question.
    await backdate_placements(db, patrick, days=2)
    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0), reask_rate=1.0
    )
    assert len(cards) == 5 and all(c.source != "reask" for c in cards)


async def test_only_a_seen_films_current_step_since_the_set_up_is_re_asked(db, world):
    """A moved film is re-asked on its move; a film marked not seen since is not; a step from
    before the set-up is history, not a placement."""
    patrick = world["patrick"]
    await place_all(db, patrick, [1, 2, 3], tier=5)
    await ladder.place(db, user_id=patrick, title_id=1, tier=2)
    await backdate_placements(db, patrick, days=10)
    await observations.record_not_seen(db, user_id=patrick, title_id=2)
    moved = await db.fetchval("SELECT max(id) FROM tier_edit WHERE title_id = 1")

    candidates = await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    )
    assert {(c.title_id, c.tier_edit_id, c.tier) for c in candidates} == {
        (1, moved, 2), (3, await _first_edit(db, patrick, 3), 5),
    }
    assert await reask.placement_candidates(
        db, user_id=patrick, kind="series", limit=5, rng=random.Random(0)
    ) == [], "always the same kind"

    await db.execute("INSERT INTO ladder_setup (user_id) VALUES ($1)", patrick)
    assert await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    ) == []


async def test_the_same_film_is_not_re_asked_again_inside_the_cooldown(db, world):
    """Without a cooldown a small library re-asks the same handful every sitting."""
    patrick = world["patrick"]
    await place_all(db, patrick, [1, 2], tier=5)
    await backdate_placements(db, patrick, days=10)
    assert len(await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    )) == 2

    await ladder.place(
        db, user_id=patrick, title_id=1, tier=5, reask_of=await _first_edit(db, patrick, 1)
    )
    await backdate_placements(db, patrick, days=10)
    still = await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, rng=random.Random(0)
    )
    assert [c.title_id for c in still] == [2]

    # Past the cooldown it becomes eligible again: the guard is a window, not a tombstone.
    later = datetime.now(UTC) + reask.REASK_COOLDOWN + timedelta(days=1)
    revived = await reask.placement_candidates(
        db, user_id=patrick, kind="movie", limit=5, now=later, rng=random.Random(0)
    )
    assert sorted(c.title_id for c in revived) == [1, 2]


async def test_a_re_ask_card_says_what_a_fresh_card_of_a_seen_film_says(db, world):
    """A different why-line or probability would give the stream away; the wire's allow-list is
    `session.public_card`'s."""
    patrick = world["patrick"]
    await place_all(db, patrick, [1], tier=5)
    await backdate_placements(db, patrick, days=10)
    # A seen film nobody has placed yet: marked seen by §7.3's sync.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", patrick
    )

    cards = await queue.next_cards(
        db, user_id=patrick, kind="movie", limit=6, rng=random.Random(4), reask_rate=1.0
    )
    by_source = {c.source: c for c in cards}
    again, fresh = by_source["reask"], by_source["seen"]
    assert (again.title_id, again.reask_of) == (1, await _first_edit(db, patrick, 1))
    assert fresh.title_id == 5 and fresh.reask_of is None
    assert again.reason == fresh.reason == queue.SEEN_REASON
    assert again.p_seen == fresh.p_seen == 1.0


def test_about_one_slot_in_ten_is_a_re_ask():
    """The pure interleave over 40,000 slots: expected 4,000, sd 60, band 3,700..4,300."""
    rng = random.Random(11)
    fresh = [queue.QueueCard(i, "why", 0.5, "p_seen", None) for i in range(100_000, 140_000)]
    asks = [
        reask.PlacementReask(tier_edit_id=i, title_id=i, tier=4, asked_at=datetime.now(UTC))
        for i in range(200_000, 240_000)
    ]
    served = queue._interleave(fresh, asks, limit=40_000, rate=reask.REASK_RATE, rng=rng)
    reasks = sum(1 for c in served if c.source == "reask")
    assert len(served) == 40_000
    assert 3_700 < reasks < 4_300, f"{reasks}/40000 slots were re-asks"
    assert len({c.title_id for c in served}) == 40_000, "a title was served twice in one queue"


def test_a_head_pinned_slot_is_never_spent_on_a_reask():
    """The banner names its titles, so a re-ask must never take a pinned slot. `rate = 1.0` forces
    the coin, so this is deterministic."""
    fresh = [
        queue.QueueCard(title_id=41, reason="r", p_seen=1.0, source="seen", reask_of=None),
        queue.QueueCard(title_id=57, reason="r", p_seen=1.0, source="seen", reask_of=None),
        queue.QueueCard(title_id=99, reason="r", p_seen=0.4, source="p_seen", reask_of=None),
    ]
    reasks = [reask.PlacementReask(tier_edit_id=7, title_id=1012, tier=5, asked_at=None)]

    out = queue._interleave(fresh, reasks, limit=3, rate=1.0, rng=random.Random(0), head=(41, 57))
    assert [c.title_id for c in out][:2] == [41, 57]
    assert 1012 in [c.title_id for c in out], "the re-ask should take the first unpinned slot"
    assert out[2].reask_of == 7

    unpinned = queue._interleave(fresh, reasks, limit=3, rate=1.0, rng=random.Random(0))
    assert unpinned[0].title_id == 1012


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
    patrick = await insert_user(db, "patrick", "admin")

    timings = []
    for _ in range(20):
        started = time.perf_counter()
        cards = await queue.next_cards(
            db, user_id=patrick, kind="movie", limit=15, rng=random.Random(0)
        )
        timings.append((time.perf_counter() - started) * 1000)
        assert len(cards) == 15

    with capsys.disabled():
        print(
            f"\n  Rate queue over {n} titles, a block of 15: "
            f"median {statistics.median(timings):.1f} ms, "
            f"p95 {sorted(timings)[18]:.1f} ms, max {max(timings):.1f} ms  (budget 2,000 ms/card)"
        )
    assert statistics.median(timings) < 2_000
