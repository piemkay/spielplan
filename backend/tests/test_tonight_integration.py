"""§6.2's database side. Spec v2.1 §6.2, §4.1 rule 5, §4.2, §5.1, §7.2, §10, §13, §14 risk 6.

The pool's *membership* is a query, and three of its four filters are the kind of rule a pure
test cannot fail:

  * **owned** — §7.2 re-derives `is_owned` from Jellyfin ("never trusted stale — the corpus
    flag goes stale the moment the library changes"). A pool that admits an unowned title puts
    a Play-on-Jellyfin CTA on the winner card that opens nothing.
  * **kind** — §4.1 rule 5, whose measured reason is that the unpartitioned crowd top-10 is
    8/10 TV series. A film session that admits a series is that bug on the one surface that
    ends in pressing play.
  * **rewatch** — §6.2 step 1's default excludes titles *every* participant has seen. The
    quantifier is the whole rule, and a one-member fixture cannot tell "every" from "any".

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.tonight import pool

BUNDLE = "test-v1"


async def make_user(db, name, role="member"):
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


@pytest.fixture
async def world(db):
    """Two members, and eight titles chosen so each filter has something to remove.

    Two members because §6.2's rewatch quantifier is "every participant", which a one-member
    fixture satisfies trivially and therefore cannot falsify.

        1..4  owned films, both scored          — the pool proper
        5     owned film, both have seen it     — the rewatch default removes it
        6     owned film, only Patrick has seen — the rewatch default KEEPS it
        7     owned series                      — the kind filter removes it from a film night
        8     film in the catalog, NOT owned    — the ownership filter removes it
    """
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}', 'active')",
        BUNDLE,
    )
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT x.id, x.kind, x.name, 2010, x.runtime, x.owned
        FROM unnest($1::int[], $2::text[], $3::text[], $4::int[], $5::boolean[])
             AS x(id, kind, name, runtime, owned)
        """,
        [1, 2, 3, 4, 5, 6, 7, 8],
        ["movie"] * 6 + ["series", "movie"],
        [f"Title {i}" for i in range(1, 9)],
        [100, 110, 151, 200, 95, 105, 45, 100],
        [True] * 7 + [False],
    )
    patrick = await make_user(db, "patrick", "admin")
    jenny = await make_user(db, "jenny")

    # §5.1's per-user half, materialised by the nightly job. Deliberately not equal: title 2 is
    # the max-min winner and title 4 the mean winner, so the ordering assertions below are
    # about the rule and not about a coincidence.
    scores = {
        patrick: {1: 0.50, 2: 0.45, 3: 0.30, 4: 0.20, 5: 0.60, 6: 0.55, 8: 0.99},
        jenny:   {1: 0.40, 2: 0.50, 3: 0.35, 4: 0.90, 5: 0.60, 6: 0.55, 8: 0.99},
    }
    for user_id, per_title in scores.items():
        for title_id, score in per_title.items():
            await db.execute(
                "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
                "VALUES ($1, $2, 'movie', $3, $4, 0.0)",
                user_id, title_id, BUNDLE, score,
            )
    # The series needs a score too, or the kind filter would be untested: an absent row and a
    # filtered row look identical in the output.
    for user_id in (patrick, jenny):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "VALUES ($1, 7, 'series', $2, 0.95, 0.0)",
            user_id, BUNDLE,
        )

    # Seen state: both have seen 5; only Patrick has seen 6.
    for user_id in (patrick, jenny):
        await db.execute(
            "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen')", user_id
        )
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 6, 'seen')", patrick
    )

    return {
        "patrick": patrick,
        "jenny": jenny,
        "seats": [
            pool.Seat(participant_id=1, user_id=patrick, is_member=True),
            pool.Seat(participant_id=2, user_id=jenny, is_member=True),
        ],
    }


async def build(db, world, **kw):
    params = dict(
        seats=world["seats"], kind="movie", budget_min=130,
        include_rewatches=False, bundle_version=BUNDLE,
    )
    params.update(kw)
    return await pool.build(db, **params)


# --- §6.2 step 3: the four filters --------------------------------------------------------


async def test_the_pool_holds_only_owned_titles_of_the_sessions_kind(db, world):
    """§7.2 keeps `is_owned` derived from Jellyfin, so it changes under the app; §4.1 rule 5
    partitions every ranking surface and an evening resolves to one title."""
    ids = [c.title_id for c in await build(db, world)]

    assert 8 not in ids, "title 8 is in the catalog but not owned"
    assert 7 not in ids, "title 7 is a series and this is a film session"
    assert set(ids) <= {1, 2, 3, 4, 6}


async def test_ownership_is_re_read_rather_than_remembered(db, world):
    """The failure §7.2 warns about: a flag that was true when the score was computed and is
    false now. The pool is built at session open, so it must read the current row."""
    assert 1 in [c.title_id for c in await build(db, world)]
    await db.execute("UPDATE title SET is_owned = false WHERE id = 1")
    assert 1 not in [c.title_id for c in await build(db, world)]


async def test_the_rewatch_default_removes_only_what_everyone_has_seen(db, world):
    """§6.2 step 1: "default: exclude titles *every* participant has seen".

    Title 5 both have seen; title 6 only Patrick has. "Any participant has seen" would strip
    title 6 too — and with it every film one person in the household has already watched,
    which is most of the good ones."""
    ids = [c.title_id for c in await build(db, world)]

    assert 5 not in ids, "both members have seen title 5"
    assert 6 in ids, "only one member has seen title 6, so it stays in the default pool"


async def test_include_rewatches_admits_both(db, world):
    ids = [c.title_id for c in await build(db, world, include_rewatches=True)]
    assert 5 in ids and 6 in ids


async def test_the_budget_admits_forty_minutes_of_grace_and_labels_it(db, world):
    """§6.2 step 1's soft budget, through the query rather than the arithmetic: title 3 is
    151 min against a 130 budget (admitted, "runs 21 min over"), title 4 is 200 (dropped)."""
    by_id = {c.title_id: c for c in await build(db, world)}

    assert 3 in by_id and by_id[3].over_budget_min == 21
    assert by_id[3].fit_line == "runs 21 min over"
    assert 4 not in by_id, "200 min is past 130 + 40"
    assert by_id[1].over_budget_min is None and by_id[1].fit_line == "fits your 130 min"


async def test_a_wider_budget_admits_the_long_one(db, world):
    by_id = {c.title_id: c for c in await build(db, world, budget_min=170)}
    assert 4 in by_id and by_id[4].over_budget_min == 30


# --- §6.2 step 3 + §0 row 3: the plain average, against real rows -------------------------


async def test_the_pool_is_ordered_by_the_plain_average_of_both_members(db, world):
    """Title 4 (0.20 / 0.90, mean 0.55) outranks title 1 (0.50 / 0.40, mean 0.45) even though
    Patrick prefers title 1 and title 4 is his worst. That is the whole of §0 row 3: a
    dominance rule that protected Patrick would invert this, and was measured at −0.012."""
    ordered = await build(db, world, budget_min=200)
    ranked = [c.title_id for c in ordered]

    assert ranked.index(4) < ranked.index(1)
    by_id = {c.title_id: c for c in ordered}
    assert by_id[4].group_score == pytest.approx(0.55)
    assert by_id[1].group_score == pytest.approx(0.45)


async def test_a_score_from_a_superseded_bundle_is_not_returned(db, world):
    """§10: "everything expressed in the old Backbone's basis is garbage against a new one",
    and the invariant is that no process scores with a version other than the active row's."""
    assert await build(db, world) != []
    assert await build(db, world, bundle_version="some-other-version") == []


async def test_a_title_only_one_member_has_a_score_for_is_not_a_candidate(db, world):
    """The mean of one score is that score, so a title only one person is scored on would
    outrank the household's actual agreement while looking like a group preference."""
    await db.execute("DELETE FROM user_score WHERE user_id = $1 AND title_id = 1", world["jenny"])
    assert 1 not in [c.title_id for c in await build(db, world)]


async def test_a_guest_seat_neither_scores_nor_filters(db, world):
    """§6.2 step 3: "Guests contribute no taste term unless they have a grid profile", and
    §6.2 step 4: a guest's seen-state is unknown, so it cannot narrow anything either."""
    guest = pool.Seat(participant_id=3, user_id=None, is_member=False)
    with_guest = await build(db, world, seats=[*world["seats"], guest])
    without = await build(db, world)

    assert [c.title_id for c in with_guest] == [c.title_id for c in without]
    for c in with_guest:
        assert set(c.scores) == {1, 2}, "a guest seat must not appear among the scored seats"
