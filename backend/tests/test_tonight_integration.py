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

VOCAB = "v1"

# One authored axis with a clean ±1.0 pair, plus a second so "the widest axis" has something to
# choose between. Shaped like §6.4's TSVs: facet -> (left pole, right pole, {term: weight}).
AXES = {
    "mood": ("heavy", "light", {"dread": -1.0, "cosy": 1.0}),
    "pacing": ("patient", "propulsive", {"patient": -1.0, "relentless": 0.8}),
}
TERMS = {"dread": "mood", "cosy": "mood", "patient": "pacing", "relentless": "pacing"}

# Deliberately polarised: titles 1-3 are heavy, 4-6 are light, so a session over this pool has
# a contested axis to find and an opposite pole to reserve a slot for. Title 2 carries a
# projected duplicate of an extracted tag — §4.1 rule 1's "pairs exist in both tiers" in
# miniature, and the read must count it once.
EXTRACTED = [
    (1, "dread", 3), (1, "patient", 2),
    (2, "dread", 2), (2, "relentless", 3),
    (3, "dread", 1),
    (4, "cosy", 3), (4, "relentless", 2),
    (5, "cosy", 2), (5, "patient", 3),
    (6, "cosy", 1),
]
# `(title, term, n_sources)`. The third field is a COUNT of independent keyword sources, 1..8 on
# the shipped bundle, which the importer writes into `dna_projected.weight` and `0004_dna.sql`'s
# view re-exposes as `confidence` — and reading that name as a 0..1 probability is finding 20
# itself. Seeded as confidences, every projection here landed near 0.2 under either expression,
# so the whole Tonight suite passed with decision 188 reverted and the plan's stated risk ("this
# changes Tonight, not only Home") was covered by nothing. Title 2's 8 is the corpus maximum and
# the value at which the unbounded `0.30 * n_sources` puts an inferred term above every
# quote-verified one. [M4.9 review cycle 1: M49-D188-02; decision 188]
PROJECTED = [(2, "dread", 8), (7, "relentless", 1)]


async def seed_dna(db):
    """The naming layer the tilt, the contested axis and the match lines all read.

    Without it every DNA read returns {} and the tilt is permanently empty — which is a fixture
    that makes three assertions vacuous while passing.
    """
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, $3)",
        VOCAB, len(AXES), len(TERMS),
    )
    for ord_, facet in enumerate(AXES):
        await db.execute(
            "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)", VOCAB, facet, ord_
        )
    for term, facet in TERMS.items():
        await db.execute(
            "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3)", VOCAB, term, facet
        )
    for facet, (left, right, weights) in AXES.items():
        await db.execute(
            "INSERT INTO dna_axis (version, facet, left_pole, right_pole) VALUES ($1, $2, $3, $4)",
            VOCAB, facet, left, right,
        )
        for term, weight in weights.items():
            await db.execute(
                "INSERT INTO dna_axis_weight (version, facet, term, weight) VALUES ($1, $2, $3, $4)",
                VOCAB, facet, term, weight,
            )
    for title_id, term, salience in EXTRACTED:
        tag_id = await db.fetchval(
            "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, provider) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'fixture') RETURNING id",
            title_id, VOCAB, term, TERMS[term], salience, 0.4 + 0.1 * salience,
        )
        # §4.1 rule 1: "a tag without its quote is unfalsifiable."
        await db.execute(
            "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'fixture')",
            tag_id, f"a line about {term}",
        )
    for title_id, term, n_sources in PROJECTED:
        # Into the column the importer writes it into. `weight` is where `n_sources` lands.
        await db.execute(
            "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
            "VALUES ($1, $2, $3, $4, $5, 'keyword:fixture')",
            title_id, VOCAB, term, TERMS[term], n_sources,
        )



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
    await seed_dna(db)

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


# --- §6.2 step 2: the room, its seats, and "join channels, all equivalent" ----------------


import asyncio  # noqa: E402
import random  # noqa: E402

import asyncpg  # noqa: E402

from spielplan.tonight import rooms  # noqa: E402


async def open_room(db, world, **kw):
    params = dict(
        host_user_id=world["patrick"], kind="movie", budget_min=130,
        include_rewatches=False, bundle_version=BUNDLE,
    )
    params.update(kw)
    return await rooms.open_session(db, **params)


def test_a_room_code_is_readable_across_a_room():
    """§6.2's own example is `MX-2210`. It is the channel that still works when push is
    undeliverable and the WebSocket needs the app open, and §11 hands it to a Home Assistant
    dashboard — so it has to survive being read aloud and typed on a phone."""
    import random

    codes = {rooms.make_code(random.Random(seed)) for seed in range(300)}
    for code in codes:
        letters, _, digits = code.partition("-")
        assert len(letters) == 2 and len(digits) == rooms.CODE_LENGTH
        assert not set(code) & set("IO01"), f"{code} cannot be dictated unambiguously"
    assert len(codes) > 100, "the space must be wide enough that two live rooms rarely collide"


async def test_opening_a_room_seats_the_host_first_and_each_guest_after(db, world):
    """§6.2 step 1 ("members and/or N guests") and step 2's hand-the-phone order: the guests
    take their turns *after* the initiator, so the seat carries that order."""
    room = await open_room(db, world, guests=2)
    lobby = await rooms.lobby(db, room["session_id"])

    assert [s["seat"] for s in lobby["seats"]] == [1, 2, 3]
    assert lobby["seats"][0]["role"] == "host"
    assert lobby["seats"][0]["user_id"] == world["patrick"]
    assert [s["role"] for s in lobby["seats"][1:]] == ["guest", "guest"]
    assert all(s["user_id"] is None for s in lobby["seats"][1:]), (
        "§4.2: NULL is the guest slot; a guest with a user_id would have a Ledger and a login"
    )
    assert [s["name"] for s in lobby["seats"][1:]] == ["Guest 1", "Guest 2"]


async def test_every_join_channel_lands_on_the_same_seat(db, world):
    """§6.2 step 2: "Join channels, all equivalent." A member who arrives by room code and
    then taps the same room in the open-rooms list must re-attach, not seat twice — two seats
    change the participant count every average and §13's approval share are computed over."""
    room = await open_room(db, world)
    by_code = await rooms.join(
        db, session_id=await rooms.resolve_code(db, room["room_code"]), user_id=world["jenny"]
    )
    by_list = await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])

    assert by_code["participant_id"] == by_list["participant_id"]
    assert by_code["created"] is True and by_list["created"] is False
    seated = await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1 AND user_id = $2",
        room["session_id"], world["jenny"],
    )
    assert seated == 1


async def test_a_code_matching_no_live_room_is_refused(db, world):
    """A join carrying a code that matches no currently active session neither opens a new
    session nor attaches the caller to an ended one."""
    room = await open_room(db, world)
    with pytest.raises(rooms.RoomError) as absent:
        await rooms.resolve_code(db, "ZZ-9999")
    assert absent.value.reason == "no_room"

    await rooms.set_state(db, room["session_id"], rooms.STATE_RESOLVED)
    with pytest.raises(rooms.RoomError):
        await rooms.resolve_code(db, room["room_code"])


async def test_a_room_code_is_unique_among_live_rooms_and_reusable_after(db, world):
    """Two live rooms sharing a code walks a household member into the wrong evening. After a
    room ends the code is a free handle again — it names a room, not a permanent thing.

    Comparing two drawn codes is close to no test at all: the space is thousands wide, so an
    implementation with no uniqueness rule whatsoever passes that assertion nearly always. What
    this asserts is the rule itself — the database refuses the second live row — because the way
    it actually breaks is a race the application cannot see. `open_session` asks whether a code
    is taken and then inserts, and two people tapping "Together" at the same moment is the
    ordinary case in a household, not the rare one. The review found it; the index it lands
    on is `session_room_code_live` in 0013.
    """
    first = await open_room(db, world)
    second = await open_room(db, world, host_user_id=world["jenny"])
    assert first["room_code"] != second["room_code"]

    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO session (room_code, host_user_id, kind, runtime_budget_min, "
            "bundle_version) VALUES ($1, $2, 'movie', 130, $3)",
            first["room_code"], world["jenny"], BUNDLE,
        )
    await rooms.set_state(db, first["session_id"], rooms.STATE_RESOLVED)
    await db.execute(
        "INSERT INTO session (room_code, host_user_id, kind, runtime_budget_min, bundle_version) "
        "VALUES ($1, $2, 'movie', 130, $3)",
        first["room_code"], world["patrick"], BUNDLE,
    )


class _TakesTheCodeMidRace:
    """A connection that lets a check pass and then takes the code before the insert lands.

    The window this opens by hand is the one two devices open by themselves: `open_session`
    asks whether a code is free and then inserts under it, and two people tapping "Together"
    at the same moment is the ordinary event in a household. Everything else is delegated, so
    the code under test is the real one.
    """

    def __init__(self, conn, *, host_user_id, bundle_version):
        self._conn = conn
        self._host = host_user_id
        self._bundle = bundle_version
        self.armed = True
        self.stolen: str | None = None

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query, *args):
        free = await self._conn.fetchval(query, *args)
        if self.armed and "SELECT 1 FROM session" in query:
            self.armed = False
            self.stolen = args[0]
            await self._conn.execute(
                "INSERT INTO session (room_code, host_user_id, kind, runtime_budget_min, "
                "bundle_version) VALUES ($1, $2, 'movie', 130, $3)",
                self.stolen, self._host, self._bundle,
            )
        return free


async def test_a_code_taken_between_the_check_and_the_insert_is_retried(db, world):
    """0013's partial unique index is what actually keeps two live rooms from sharing a code;
    the check before the insert only keeps the common case off it. Lose that race and the index
    does its job as an unhandled `UniqueViolationError` — a 500 on the main control of the
    surface, for two taps that arrived together.

    Deterministic rather than waited for: the connection takes the code in the window itself.
    """
    racing = _TakesTheCodeMidRace(db, host_user_id=world["jenny"], bundle_version=BUNDLE)
    opened = await rooms.open_session(
        racing, host_user_id=world["patrick"], kind="movie", budget_min=130,
        include_rewatches=False, bundle_version=BUNDLE, rng=random.Random(7),
    )

    assert racing.stolen and opened["room_code"] != racing.stolen, "it drew again after losing"
    assert await rooms.resolve_code(db, opened["room_code"]) == opened["session_id"]
    seats = await db.fetch(
        "SELECT seat FROM session_participant WHERE session_id = $1", opened["session_id"]
    )
    assert [r["seat"] for r in seats] == [1], "and the retry seated the host exactly once"


async def test_opening_a_second_room_abandons_the_first_one_nobody_started(db, world):
    """0013 admits `abandoned` and nothing ever wrote it, so a room ended exactly one way: by
    reaching a result. A household that opens a room and drifts off to do something else leaves
    it live forever — on §6.2 step 2's open-rooms list for every device, with an age that only
    grows, and holding the host's seat, so every later visit to the surface restores them into
    a room nobody is in.

    A host cannot be hosting two rooms nobody has started. The second tap says the first is
    over, which is the one moment the intent is unambiguous and needs no new control to read.
    """
    first = await open_room(db, world)
    second = await open_room(db, world)

    row = await db.fetchrow(
        "SELECT state, ended_at FROM session WHERE id = $1", first["session_id"]
    )
    assert row["state"] == rooms.STATE_ABANDONED
    assert row["ended_at"] is not None, "0013's CHECK ties the two together"
    listed = [r["session_id"] for r in await rooms.open_rooms(db, viewer_id=world["patrick"])]
    assert listed == [second["session_id"]]
    assert first["room_code"] != second["room_code"]


async def test_a_started_room_is_not_abandoned_by_a_second_one(db, world):
    """The claim is about a room nobody started, and only that. A round in progress is people
    answering on their own devices, and 54e's reveal waits for every seat — ending it because
    somebody opened a second room somewhere else would take the evening away from them."""
    running = await running_room(db, world)
    await open_room(db, world)

    row = await db.fetchrow(
        "SELECT state, ended_at FROM session WHERE id = $1", running["session_id"]
    )
    assert row["state"] == rooms.STATE_VOTING and row["ended_at"] is None


async def test_one_hosts_second_room_leaves_another_hosts_alone(db, world):
    """Two rooms in one household is a supported evening — §6.2 step 2's list is plural, and
    `to_session` exists to keep one room's frames out of the other. Only the host's own."""
    hers = await open_room(db, world, host_user_id=world["jenny"])
    await open_room(db, world, host_user_id=world["patrick"])
    await open_room(db, world, host_user_id=world["patrick"])

    row = await db.fetchrow("SELECT state FROM session WHERE id = $1", hers["session_id"])
    assert row["state"] == rooms.STATE_OPEN


async def test_the_open_rooms_list_is_visible_to_every_member_not_only_the_host(db, world):
    """§6.2 step 2: "active sessions are visible to **every household device**", with the six
    facets the spec's example string names."""
    room = await open_room(db, world, budget_min=60)
    listed = await rooms.open_rooms(db, viewer_id=world["jenny"])

    assert [r["session_id"] for r in listed] == [room["session_id"]]
    row = listed[0]
    assert row["room_code"] == room["room_code"]
    assert row["host"] == "patrick"
    assert row["kind"] == "movie"
    assert row["runtime_budget_min"] == 60
    assert row["skips_seen"] is True, "the default rewatch toggle reads as 'skips seen'"
    assert row["started_at"] is not None
    assert row["joinable"] is True and row["viewer_seated"] is False


async def test_an_ended_room_leaves_the_open_rooms_list(db, world):
    room = await open_room(db, world)
    await rooms.set_state(db, room["session_id"], rooms.STATE_RESOLVED)
    assert await rooms.open_rooms(db, viewer_id=world["jenny"]) == []


async def test_a_seat_the_viewer_already_holds_is_not_offered_again(db, world):
    """The open-rooms row drives "tappable empty seats", so it has to know whether this
    device's member is already in the room — otherwise the tap is a no-op nobody can predict."""
    room = await open_room(db, world)
    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    row = (await rooms.open_rooms(db, viewer_id=world["jenny"]))[0]

    assert row["viewer_seated"] is True
    assert row["joinable"] is False


async def test_joining_a_started_room_is_refused_rather_than_silently_ignored(db, world):
    """§6.2 never says when joining closes; the smallest rule that keeps a round coherent is
    that the participant set the pool was built for is fixed once pairs are being served. A
    late arrival joins the next session, and is told so."""
    room = await open_room(db, world)
    await rooms.set_state(db, room["session_id"], rooms.STATE_VOTING)
    with pytest.raises(rooms.RoomError) as started:
        await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    assert started.value.reason == "started"


class _SeatsSomeoneMidRace:
    """A connection that seats somebody in the window between the seat read and the insert.

    The window two devices open by themselves: `join` reads whether the caller is already
    seated, works out the next free seat number, and inserts. Everything between those is
    another device doing the same thing — the banner and the open-rooms row are two channels to
    one control, and §6.2 step 2 says they are "all equivalent", which is a promise about
    arriving twice at once as much as about arriving twice in a row.
    """

    def __init__(self, conn, *, session_id, user_id, role="member", seat=None):
        self._conn = conn
        self._session_id = session_id
        self._user_id = user_id
        self._role = role
        self._seat = seat
        self.armed = True

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query, *args):
        value = await self._conn.fetchval(query, *args)
        if self.armed and "max(seat)" in query:
            self.armed = False
            await self._conn.execute(
                "INSERT INTO session_participant (session_id, user_id, role, seat) "
                "VALUES ($1, $2, $3, $4)",
                self._session_id, self._user_id, self._role,
                self._seat if self._seat is not None else value,
            )
        return value


async def test_joining_twice_at_once_returns_the_one_seat_rather_than_a_500(db, world):
    """§6.2 step 2: the room code, the banner and the open-rooms row are "all equivalent", and
    `join` is idempotent for exactly that reason — a second seat changes the participant count
    every average and §13's approval share are computed over.

    It was idempotent in sequence and a 500 at once. Two of those channels are one tap apart on
    the same screen, and the index that stops the second seat did its job by raising into the
    route. What the person saw was a join that failed on a room they were, by then, in.
    """
    room = await open_room(db, world)
    racing = _SeatsSomeoneMidRace(
        db, session_id=room["session_id"], user_id=world["jenny"], seat=2
    )

    joined = await rooms.join(racing, session_id=room["session_id"], user_id=world["jenny"])

    assert joined["created"] is False, "the seat that already existed is the seat they get"
    assert joined["seat"] == 2
    seats = await db.fetch(
        "SELECT user_id FROM session_participant WHERE session_id = $1 AND user_id = $2",
        room["session_id"], world["jenny"],
    )
    assert len(seats) == 1, "one member, one seat"


async def test_two_members_racing_for_the_same_seat_number_both_sit_down(db, world):
    """The other half of the same window, and the one a household actually hits: two people
    tapping the same open-rooms row read `max(seat) + 1` and both got the same number. One of
    them was told the room had failed."""
    room = await open_room(db, world)
    racing = _SeatsSomeoneMidRace(db, session_id=room["session_id"], user_id=None, role="guest")

    joined = await rooms.join(racing, session_id=room["session_id"], user_id=world["jenny"])

    assert joined["created"] is True
    assert joined["seat"] == 3, "the seat the other device took is not offered twice"
    seats = await db.fetch(
        "SELECT seat FROM session_participant WHERE session_id = $1 ORDER BY seat",
        room["session_id"],
    )
    assert [r["seat"] for r in seats] == [1, 2, 3]


async def test_the_lobby_carries_no_candidate_and_no_ranking(db, world):
    """§6.2 step 3: the pool is "internal — **never shown as a step**", and the lobby is the
    screen most likely to leak it."""
    room = await open_room(db, world)
    seen = await rooms.lobby(db, room["session_id"])
    text = repr(seen)

    for leaked in ("title_id", "score", "candidates", "pool", "group_score"):
        assert leaked not in text, f"the lobby payload carries {leaked}"


async def test_the_invitation_list_is_every_member_not_already_seated(db, world):
    """§6.2 step 2's push goes to "members' phones" — not to the host, who opened the room, and
    not to anyone already in it."""
    room = await open_room(db, world)
    assert await rooms.members_to_invite(
        db, session_id=room["session_id"], host_user_id=world["patrick"]
    ) == [world["jenny"]]

    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    assert await rooms.members_to_invite(
        db, session_id=room["session_id"], host_user_id=world["patrick"]
    ) == []


async def test_a_pool_built_from_the_rooms_seats_matches_the_rooms_controls(db, world):
    """The seam between §6.2 step 1's controls and step 3's pool: the room's kind, budget and
    rewatch setting are what the pool is built from, not defaults the caller supplies again."""
    room = await open_room(db, world, budget_min=200, include_rewatches=True)
    seen = await rooms.lobby(db, room["session_id"])
    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    seats = await rooms.seats_of(db, room["session_id"])

    built = await pool.build(
        db, seats=seats, kind=seen["kind"], budget_min=seen["runtime_budget_min"],
        include_rewatches=seen["include_rewatches"], bundle_version=BUNDLE,
    )
    ids = [c.title_id for c in built]
    assert 5 in ids and 4 in ids, "rewatches included and a 200-minute film inside a 200 budget"
    assert 7 not in ids, "a film session never admits a series"


async def test_guest_seats_are_not_members_for_the_pools_purposes(db, world):
    """§6.2 step 3, at the seam: the seats a room produces carry the taste question, so a guest
    cannot contribute a term by accident."""
    room = await open_room(db, world, guests=2)
    seats = await rooms.seats_of(db, room["session_id"])

    assert [s.is_member for s in seats] == [True, False, False]
    assert [s.user_id for s in seats] == [world["patrick"], None, None]


# --- §6.2 steps 4-6: the round, the combine and the blind ballot --------------------------


from spielplan.tonight import ballot, combine, play  # noqa: E402
from spielplan.tonight import round as rnd  # noqa: E402
from spielplan.tonight import tilt as tilt_rules  # noqa: E402

# WHAT THE ROUTE PASSES, read from the round rather than spelled. This was a bare `1.0` with
# no argument attached to it -- §6.3's `straddle_z`, left behind when decision 214 separated
# the two scales and `api/tonight.py` started handing `play` the round's own constant. Every
# assertion in this file about when a round ends was therefore graded at a boundary the app
# does not serve: at 1.0 a pool keeps straddlers that the shipped 0.6 has already resolved, so
# a round this layer called unfinished is one a household would have been shown the end of.
# [decision 214]
Z = rnd.BOUNDARY_Z


async def running_room(db, world, *, guests=0, budget_min=200, include_rewatches=True):
    """A started session with both members seated and the pool frozen. Wide by default so the
    pool is every film the fixture has — a two-title pool has no shortlist boundary."""
    room = await open_room(
        db, world, guests=guests, budget_min=budget_min, include_rewatches=include_rewatches
    )
    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    await play.start(db, room["session_id"])
    seats = await db.fetch(
        "SELECT id, seat, role FROM session_participant WHERE session_id = $1 ORDER BY seat",
        room["session_id"],
    )
    return {**room, "seats": [dict(s) for s in seats]}


async def answer_once(db, participant_id, answer=rnd.A):
    """Serve this seat its next pair and answer it, the way a route would."""
    state = await play.state_for(db, participant_id, z=Z)
    if state["_pair"] is None:
        return None
    return await play.record_answer(
        db, participant_id=participant_id, pair=state["_pair"], answer=answer,
        seq=state["answered"] + 1, latency_ms=900, z=Z,
    )


async def _last_arm(db, participant_id):
    """The arm of this seat's most recent live answer, as the server stored it."""
    return await db.fetchval(
        "SELECT selection FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL "
        "ORDER BY seq DESC LIMIT 1",
        participant_id,
    )


async def run_to_the_end(db, participant_id, answer=rnd.A):
    for _ in range(rnd.CAP_PAIRS + 2):
        row = await db.fetchrow(
            "SELECT ended_by FROM session_participant WHERE id = $1", participant_id
        )
        if row["ended_by"] is not None:
            return row["ended_by"]
        if await answer_once(db, participant_id, answer) is None:
            break
    return (await db.fetchrow(
        "SELECT ended_by FROM session_participant WHERE id = $1", participant_id
    ))["ended_by"]


async def test_starting_a_room_freezes_the_pool_it_was_built_from(db, world):
    """§6.2 step 6: "nothing re-ranks within the evening by predicted enjoyment (measured:
    worth 0.000)". The pool is computed once at start; a nightly fit landing mid-evening, or
    somebody marking a title seen in the other room, must not move tonight's ranking."""
    room = await running_room(db, world)
    before = await play.snapshot_of(db, room["session_id"])

    await db.execute("UPDATE user_score SET score = 0.01 WHERE user_id = $1", world["patrick"])
    await db.execute("UPDATE title SET is_owned = false WHERE id = 1")
    after = await play.snapshot_of(db, room["session_id"])

    assert after.scores == before.scores
    assert after.title_ids == before.title_ids


async def test_a_started_room_cannot_be_started_twice(db, world):
    room = await running_room(db, world)
    with pytest.raises(play.RoundError) as again:
        await play.start(db, room["session_id"])
    assert again.value.reason == "already_started"


async def test_starting_a_room_the_household_ended_says_so_rather_than_already_started(db, world):
    """§6.8's register: a refusal the household cannot act on is the defect.

    `start`'s claim is `WHERE id = $1 AND state = 'open'`, and an empty result was read as two
    cases only -- no such session, or a room that has started. A room the host ended is neither, so
    the host whose lobby outlived their own End (their other device, the reveal, or a frame they
    missed) tapped Start and was told 409 "that room has already started": a sentence that is false,
    that names no action, and that sends them looking for a round that does not exist. Its partner
    in the same lifecycle pair asks `ended_at` and answers 404 "that session has ended", and
    finding 8's repair put the same question in `play._participant`; `start` was the entry point
    left reading `state` alone. One extra column on a statement already being run.
    [M4.12 review cycle 1: M412-PLAY-3; decision 216]
    """
    room = await open_room(db, world)
    await rooms.end_session(db, room["session_id"])

    with pytest.raises(play.RoundError) as ended:
        await play.start(db, room["session_id"])
    assert ended.value.reason == "no_room", "an evening that is over is not one that has begun"
    assert "ended" in str(ended.value), str(ended.value)

    # The sibling on the same row, for the comparison the register is about: both entry points
    # answer the same question with the same status.
    with pytest.raises(rooms.RoomError) as joined:
        await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    assert joined.value.reason == "no_room"


async def test_a_pool_with_nothing_in_it_says_so_rather_than_serving_a_pair(db, world):
    """§6.2 defines the happy path only; an empty pool is a real state on a small library with
    a tight budget, and the honest answer is copy rather than a crash."""
    room = await open_room(db, world, budget_min=60, include_rewatches=False)
    with pytest.raises(play.RoundError) as empty:
        await play.start(db, room["session_id"])
    assert empty.value.reason == "empty_pool"


# --- §14 risk 6: log every vote ------------------------------------------------------------


async def test_every_answered_pair_writes_exactly_one_row_that_replays_the_round(db, world):
    """§14 risk 6: "log every vote". §4.2 fixes the columns; the binding that matters is that
    `A` names `title_a` — an answer stored against the wrong side of its own pair is an
    instrument that reads backwards, and it looks identical in every count."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]

    served = []
    for _ in range(3):
        state = await play.state_for(db, seat, z=Z)
        if state["_pair"] is None:
            break
        served.append((state["_pair"].title_a, state["_pair"].title_b, state["_pair"].selection))
        await play.record_answer(
            db, participant_id=seat, pair=state["_pair"], answer=rnd.A,
            seq=state["answered"] + 1, latency_ms=1234, z=Z,
        )

    rows = await db.fetch(
        "SELECT seq, title_a, title_b, answer, selection, latency_ms FROM session_answer "
        "WHERE participant_id = $1 ORDER BY seq",
        seat,
    )
    assert len(rows) == len(served)
    assert [r["seq"] for r in rows] == list(range(1, len(served) + 1))
    for row, (a, b, selection) in zip(rows, served, strict=True):
        assert (row["title_a"], row["title_b"]) == (a, b)
        assert row["selection"] == selection
        assert row["answer"] == "A"
        assert row["latency_ms"] == 1234


async def test_the_counter_never_drifts_from_the_rows_it_counts(db, world):
    """`answered_count` is the number the lobby and the waiting screen display. A denormalised
    counter that drifts from its rows turns "3 of 4 finished" into a round nobody can end."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    for _ in range(4):
        if await answer_once(db, seat) is None:
            break
        counted = await db.fetchval(
            "SELECT answered_count FROM session_participant WHERE id = $1", seat
        )
        rows = await db.fetchval(
            "SELECT count(*) FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL",
            seat,
        )
        assert counted == rows


async def test_a_replayed_pair_is_refused_rather_than_counted_twice(db, world):
    """§13's figures count *rows* and §4.2's tables are append-only, so a replay would weight
    one judgement twice in the data admitted to evaluate the round — and could not be taken
    back. Same single-use property `api/rank.py` gives a queue pair."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    state = await play.state_for(db, seat, z=Z)
    await play.record_answer(
        db, participant_id=seat, pair=state["_pair"], answer=rnd.A, seq=1, latency_ms=None, z=Z
    )
    with pytest.raises(play.RoundError) as stale:
        await play.record_answer(
            db, participant_id=seat, pair=state["_pair"], answer=rnd.B, seq=1,
            latency_ms=None, z=Z,
        )
    assert stale.value.reason == "stale_pair"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 1


async def test_an_answer_moves_the_participants_tilt(db, world):
    """§4.2: "session_participant(… tilt jsonb …)". §6.2 step 5 makes the tilt the thing every
    vote also yields, so an answer that left it empty would be a vote that chose without
    meaning anything.

    DIRECTIONAL NOW, BECAUSE `isinstance(after, dict)` WAS TRUE OF `{}` (M4.12 finding 45). It
    was the registered evidence for `tonight-rank-tilt-centred-on-the-pool-mean`'s integration
    half and it passed on a tilt that had not moved at all — which is precisely the state the
    defect this milestone fixed in solo produced. Three claims, none of which an empty dict or a
    sign flip survives:

      * the pair the round actually served moved it, and only the terms that pair carries;
      * under the resulting tilt the CHOSEN title outranks the rejected one, which is what
        "chosen-minus-rejected" means on the score the shortlist is built from — and is not a
        restatement of the arithmetic, since it is the inner product that has to come out
        positive, not any one coordinate;
      * the opposite answer on the same pair is the exact negation, so the tilt is a reading of
        the person's answer and not of the pair.
    """
    room = await running_room(db, world)
    chose_a, chose_b = room["seats"][0]["id"], room["seats"][1]["id"]
    assert await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", chose_a) == {}

    state = await play.state_for(db, chose_a, z=Z)
    pair = state["_pair"]
    assert pair is not None and pair.selection == rnd.SELECTION_ADAPTIVE, (
        "a held-out answer moves no tilt by design (54b), so this claim needs an adaptive pair"
    )
    # The same pair to the other seat, the way `test_a_held_out_answer_...` above hands one over:
    # two seats of one room answer independently, and the pool they are scored against is frozen.
    await play.record_answer(
        db, participant_id=chose_a, pair=pair, answer=rnd.A, seq=1, latency_ms=900, z=Z
    )
    await play.record_answer(
        db, participant_id=chose_b, pair=pair, answer=rnd.B, seq=1, latency_ms=900, z=Z
    )

    after = await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", chose_a)
    mirrored = await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", chose_b)
    snapshot = await play.snapshot_of(db, room["session_id"])
    frame = snapshot.frame()
    chosen, rejected = snapshot.dna[pair.title_a], snapshot.dna[pair.title_b]

    assert after != {}, "the vote chose without meaning anything"
    assert set(after) <= set(chosen) | set(rejected), (
        "a term neither film in the pair carries cannot be what the answer said"
    )
    assert tilt_rules.adjustment(after, chosen, frame) > (
        tilt_rules.adjustment(after, rejected, frame)
    ), "the answer has to raise what was chosen above what was not"
    assert mirrored == pytest.approx({t: -v for t, v in after.items()}), (
        "the opposite answer on the same pair is the opposite reading"
    )


# --- 54c: stopping, the cap, the escape ----------------------------------------------------


async def test_a_round_ends_with_exactly_one_named_reason(db, world):
    """54c/54g and §14 risk 6: the rate of each has to be recoverable, so the reason is stored
    and it is one of three."""
    room = await running_room(db, world)
    reason = await run_to_the_end(db, room["seats"][0]["id"])
    assert reason in rnd.END_REASONS


async def test_the_cap_ends_a_round_that_will_not_resolve(db, world):
    """"subject to a **hard cap of 20 pairs**". Answering EITHER to everything keeps lifting
    the whole pool together, so nothing separates and the cap is the only exit."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    reason = await run_to_the_end(db, seat, answer=rnd.EITHER)
    answered = await db.fetchval(
        "SELECT answered_count FROM session_participant WHERE id = $1", seat
    )
    assert reason == rnd.CAP
    assert answered <= rnd.CAP_PAIRS
    # On the eight-title fixture the pool runs out of distinct pairs first, which is the same
    # terminal state and the reason the deadlock rule exists — the 20-pair bound itself is
    # asserted where a pool can be made large enough to reach it
    # (test_tonight_round.py::test_a_round_that_cannot_resolve_ends_at_the_hard_cap_of_twenty).
    assert answered >= 6, "a round has to actually ask before it gives up"


async def test_two_participants_may_stop_at_different_pair_counts(db, world):
    """54c: "Participants converge at different points." The whole substance of the adaptive
    round is that it is per participant — one that ended both seats together would pass every
    test about a single seat."""
    room = await running_room(db, world)
    first, second = room["seats"][0]["id"], room["seats"][1]["id"]
    await run_to_the_end(db, first, answer=rnd.A)
    for _ in range(2):
        await answer_once(db, second, rnd.EITHER)

    counts = await db.fetch(
        "SELECT id, answered_count, ended_by FROM session_participant WHERE session_id = $1 "
        "ORDER BY seat",
        room["session_id"],
    )
    assert counts[0]["ended_by"] is not None
    assert counts[1]["ended_by"] is None, "one seat ending must not end the other"
    assert counts[0]["answered_count"] != counts[1]["answered_count"]


async def test_the_escape_is_refused_before_pair_six_and_ends_the_round_after(db, world):
    """54c: "From the sixth pair a persistent 'just pick for us' ends that person's round
    immediately on what is known so far." Refused, not ignored — a control that silently does
    nothing is worse than one that is not there."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]

    with pytest.raises(play.RoundError) as early:
        await play.escape(db, seat)
    assert early.value.reason == "too_early"

    for _ in range(5):
        if await answer_once(db, seat) is None:
            break
    row = await db.fetchrow("SELECT ended_by, answered_count FROM session_participant WHERE id = $1", seat)
    if row["ended_by"] is None and row["answered_count"] >= rnd.ESCAPE_FROM_PAIR - 1:
        out = await play.escape(db, seat)
        assert out["ended_by"] == rnd.ESCAPE
        stored = await db.fetchrow(
            "SELECT ended_by, converged_at FROM session_participant WHERE id = $1", seat
        )
        assert stored["ended_by"] == "escape"
        assert stored["converged_at"] is None, "an escape is not a convergence"


async def test_the_state_reports_when_the_escape_becomes_available(db, world):
    """54c makes the control a property of the round's state; a client computing it from a
    count would be a second implementation of the rule.

    ASSERTED, NOT GUARDED BY AN `if`. Both halves used to sit under `if answered >= ESCAPE_FROM_PAIR
    - 1`, so a round that ended before pair five passed this test without evaluating the thing it is
    named for. The pool is widened here instead, and the count is asserted: a seat that never reaches
    pair five is a fixture fault, and this is the test that has to say so.

    AND IT CLOSES AGAIN WHEN THE SEAT ENDS, which is the half nothing held at all: the flag was
    computed from `answered_count` alone, so a seat that had converged, hit the cap or taken the
    escape itself still advertised a control `play.escape` answers with `round_over` — and the
    availability travels precisely so the client is not a second implementation of the rule. The
    pure predicate stays what it is, which is why both are asserted side by side below.
    [M4.12 finding 9]
    """
    # Ten more films, scored a hair apart for both members, so the shortlist is genuinely unsettled
    # at pair five whatever the round's boundary is. `world`'s own pool is six titles wide, and at
    # `BOUNDARY_Z` (decision 214) a pool that narrow can resolve in one or two pairs — which is the
    # condition that made the `if` above hide both assertions.
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT g, 'movie', 'Near ' || g, 2011, 100, true FROM generate_series(9, 18) AS g
        """
    )
    for user_id in (world["patrick"], world["jenny"]):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "SELECT $1, g, 'movie', $2, 0.30 + 0.01 * g, 0.0 FROM generate_series(9, 18) AS g",
            user_id, BUNDLE,
        )

    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    assert (await play.state_for(db, seat, z=Z))["escape_available"] is False

    for _ in range(rnd.ESCAPE_FROM_PAIR - 1):
        assert await answer_once(db, seat) is not None, "the round ended before 54c's escape opens"
    state = await play.state_for(db, seat, z=Z)
    assert state["answered"] == rnd.ESCAPE_FROM_PAIR - 1, state
    assert state["escape_available"] is True

    ended = await play.escape(db, seat)
    assert ended["ended_by"] == rnd.ESCAPE
    assert ended["escape_available"] is False, "the control that ended the round still offers itself"
    after = await play.state_for(db, seat, z=Z)
    assert after["escape_available"] is False, (
        "a seat that has ended still advertises the escape, which the route refuses with round_over"
    )
    assert rnd.escape_available(after["answered"]) is True, (
        "the count alone still says yes, so this asserts the ended_by half rather than the count's"
    )


# --- §6 preamble: undo everywhere ----------------------------------------------------------


async def test_a_participant_can_take_back_the_answer_they_just_gave(db, world):
    """§6's preamble puts undo on every surface, and the round is the one place a mis-tap is
    otherwise permanent: a hard cap, a blind reveal, no second pass. Tombstone rather than
    DELETE — §14 risk 6 says log every vote, and a retraction is itself a fact."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    # At least two answers, the last of them adaptive. 54b's arm moves no tilt at all, so an undo
    # that took one back would take nothing back and this test would be measuring the arm rather
    # than the undo. Which pairs the arm draws is a fact about this seat now, not about every
    # tenth slot (decision 223), so the fixture asks the rows rather than counting to ten.
    answered = 0
    while answered < 2 or await _last_arm(db, seat) == rnd.SELECTION_HOLDOUT:
        assert await answer_once(db, seat) is not None, "the round ended before two answers"
        answered += 1
    tilt_before = await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", seat)

    out = await play.retract(db, seat, z=Z)
    assert out["answered"] == answered - 1
    row = await db.fetchrow(
        "SELECT answered_count, tilt FROM session_participant WHERE id = $1", seat
    )
    assert row["answered_count"] == answered - 1
    assert row["tilt"] != tilt_before, "the retracted answer no longer moves the tilt"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == answered, "the row survives as a tombstone"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL",
        seat,
    ) == answered - 1


async def test_undo_reaches_only_your_own_last_live_answer(db, world):
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    with pytest.raises(play.RoundError) as nothing:
        await play.retract(db, seat, z=Z)
    assert nothing.value.reason == "nothing_to_undo"


async def test_a_finished_round_cannot_be_edited(db, world):
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await run_to_the_end(db, seat)
    with pytest.raises(play.RoundError) as over:
        await play.retract(db, seat, z=Z)
    assert over.value.reason == "round_over"


# --- 54c: the waiting state ----------------------------------------------------------------


async def test_the_waiting_payload_carries_counts_and_never_an_answer(db, world):
    """54c: "Someone who finishes early sees the others' **progress and never their answers**."
    The blind property is a fact about what the statement can return — "the payload cannot carry
    the answers, not that the UI declines to draw them"."""
    room = await running_room(db, world)
    first = room["seats"][0]["id"]
    await answer_once(db, first)
    await answer_once(db, first)

    seen = await play.progress(db, room["session_id"])
    assert [p["answered"] for p in seen] == [2, 0]

    # Asserted over the KEYS rather than by grepping the repr: "answered" contains "answer",
    # so a substring check passes an implementation that leaks the answers and fails one that
    # does not. The set is exhaustive on purpose — a field added later has to be argued for.
    assert {k for p in seen for k in p} == {
        "participant_id", "seat", "name", "answered", "expected", "finished", "ended_by",
    }
    values = repr([list(p.values()) for p in seen])
    for leaked in ("EITHER", "NEITHER", "title", "tilt"):
        assert leaked not in values, f"the waiting payload carries {leaked}"


# --- §6.2 step 2: the guest hand-off --------------------------------------------------------


async def test_a_guest_cannot_answer_before_the_initiator_has_finished(db, world):
    """§6.2 step 2: "Guests use the initiator's phone **after the initiator finishes**
    (hand-the-phone, sequential turns)." The refusal is what makes the phone's owner able to
    finish; without it the guest's first tap lands in the middle of the host's round."""
    room = await running_room(db, world, guests=2)
    guest = next(s for s in room["seats"] if s["role"] == "guest")
    host = next(s for s in room["seats"] if s["role"] == "host")

    state = await play.state_for(db, guest["id"], z=Z)
    with pytest.raises(play.RoundError) as early:
        await play.record_answer(
            db, participant_id=guest["id"], pair=state["_pair"], answer=rnd.A, seq=1,
            latency_ms=None, z=Z,
        )
    assert early.value.reason == "not_your_turn"

    await run_to_the_end(db, host["id"])
    assert await answer_once(db, guest["id"]) is not None


async def test_only_one_guest_turn_is_open_at_a_time(db, world):
    """"sequential turns" — two guests on one phone answering at once is one seat holding two
    people's answers."""
    room = await running_room(db, world, guests=2)
    host = next(s for s in room["seats"] if s["role"] == "host")
    guests = [s for s in room["seats"] if s["role"] == "guest"]
    await run_to_the_end(db, host["id"])

    assert await answer_once(db, guests[0]["id"]) is not None
    state = await play.state_for(db, guests[1]["id"], z=Z)
    with pytest.raises(play.RoundError) as waiting:
        await play.record_answer(
            db, participant_id=guests[1]["id"], pair=state["_pair"], answer=rnd.A, seq=1,
            latency_ms=None, z=Z,
        )
    assert waiting.value.reason == "not_your_turn"


async def test_a_guest_is_ranked_by_the_pools_order_and_never_a_members_ledger(db, world):
    """§6.2 step 3 and the prototype's `const u = guest ? 'p' : who`. A guest's prior is the
    pool's own member-average order, flat — not one member's scores wearing their name."""
    room = await running_room(db, world, guests=1)
    snapshot = await play.snapshot_of(db, room["session_id"])
    guest = next(s for s in room["seats"] if s["role"] == "guest")
    host = next(s for s in room["seats"] if s["role"] == "host")

    guest_prior = snapshot.member_average()
    host_prior = snapshot.pool_scores_for(host["id"])
    assert guest["id"] not in {p for s in snapshot.scores.values() for p in s}
    assert guest_prior != host_prior


# --- §6.2 step 5-6: the combine and the blind ballot ---------------------------------------


async def finished_session(db, world, **kw):
    room = await running_room(db, world, **kw)
    for seat in room["seats"]:
        if seat["role"] == "guest":
            continue
        await run_to_the_end(db, seat["id"])
    return room


async def test_the_slate_is_persisted_rather_than_recomputed_on_read(db, world):
    """§4.2 gives the round a durable per-title table. A slate re-derived later cannot be
    compared against the votes that produced it, which is what §14 risk 6 exists to require."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"], z=Z)

    rows = await db.fetch(
        "SELECT title_id, rank, slot, group_score, per_user_match, conflict FROM session_result "
        "WHERE session_id = $1 ORDER BY rank",
        room["session_id"],
    )
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert sorted(r["title_id"] for r in rows if r["slot"] == "finalist") == sorted(slate.finalists)
    assert [r["title_id"] for r in rows if r["slot"] == "wildcard"] == [slate.wildcard]

    # And it does not move when the Ledger underneath it does.
    await db.execute("UPDATE user_score SET score = 0.01")
    again = await db.fetch(
        "SELECT title_id, rank FROM session_result WHERE session_id = $1 ORDER BY rank",
        room["session_id"],
    )
    assert [(r["title_id"], r["rank"]) for r in again] == [(r["title_id"], r["rank"]) for r in rows]


async def test_a_quiet_session_stores_no_conflict_at_all(db, world):
    """§6.2 step 5: "below that, decide silently". A conflict object written on every session
    turns §6.8's repair register into background noise.

    The whole assertion used to sit under `if slate.conflict is None`, which is the shape of a
    test that cannot fail: an implementation that wrote a conflict on every session took the
    other branch and passed. So the session here is made quiet rather than hoped to be — both
    seats answer `EITHER` to every pair, which by decision 154 lifts both candidates and pulls
    the two of them in no opposing direction at all — and the assertion is unconditional.

    AND THE HOUSEHOLD IS MADE QUIET TOO, WHICH IT WAS NOT (M4.12 decision 218). "EITHER to every
    pair" is a statement about one seat's own answers; §6.2 step 5's D is mean-minus-min over the
    MEMBERS' tonight scores, and this fixture's two members disagree by 0.7 on title 4 by design.
    It passed anyway because `tilt.centred` gave every term neither film in a pair carried the
    same fabricated coordinate, which accumulated over twenty answers into a component both
    participants shared and which swamped the Ledger difference between them — finding 27's
    "they move as a block", propping up a quiet-session fixture. With the block gone, D is 0.35
    and the split is real. So the two Ledgers are made equal here: a household of one taste
    cannot produce a divergence, which is what this test needs and what it only appeared to have.
    """
    await db.execute(
        "UPDATE user_score SET score = (SELECT s2.score FROM user_score s2 "
        "WHERE s2.user_id = $1 AND s2.title_id = user_score.title_id) "
        "WHERE user_id = $2 AND EXISTS (SELECT 1 FROM user_score s3 "
        "WHERE s3.user_id = $1 AND s3.title_id = user_score.title_id)",
        world["patrick"], world["jenny"],
    )
    room = await running_room(db, world)
    for seat in room["seats"]:
        if seat["role"] == "guest":
            continue
        await run_to_the_end(db, seat["id"], answer=rnd.EITHER)

    slate = await play.finish(db, room["session_id"], z=Z)

    assert slate.conflict is None, "nobody pulled against anybody"
    stored = await db.fetch(
        "SELECT conflict FROM session_result WHERE session_id = $1", room["session_id"]
    )
    assert stored, "a finished session has rows, or the assertion below is about nothing"
    assert all(r["conflict"] is None for r in stored)


async def test_every_participant_gets_a_match_line_naming_terms_the_title_carries(db, world):
    """§6.2 step 7 + §6.8, and §6.0's own invariant: a card shown under a reason it does not
    satisfy is the defect `home/why.py` was inverted to make unrepresentable. The winner card
    is that claim on the screen the whole round exists to produce."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    # The rank-1 row's OWN title, not `slate.finalists[0]`: `rank` orders the slate by group
    # score, while `finalists` is its membership — and a surfaced split reorders the second by
    # the zeroed score, so the two are not the same title.
    winner_row = await db.fetchrow(
        "SELECT title_id, per_user_match FROM session_result WHERE session_id = $1 AND rank = 1",
        room["session_id"],
    )
    lines = winner_row["per_user_match"]
    seats = await db.fetch(
        "SELECT id FROM session_participant WHERE session_id = $1", room["session_id"]
    )
    assert set(lines) == {str(s["id"]) for s in seats}, "every participant gets a line"

    carried = {
        r["term"]
        for r in await db.fetch(
            "SELECT term FROM dna_tagged WHERE title_id = $1", winner_row["title_id"]
        )
    }
    for line in lines.values():
        assert line["line"], "an empty line is not a match line"
        for term in line["terms"]:
            assert term["term"] in carried, (
                f"{term['term']} is named for a title that does not carry it"
            )


async def test_approvals_stay_hidden_until_every_participant_has_submitted(db, world):
    """54e: "Approvals stay hidden until every participant has submitted; then they are
    revealed together." Enforced in the read — a payload that carries an approval is one
    request away from being read whatever the screen does."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"], z=Z)
    first, second = room["seats"][0]["id"], room["seats"][1]["id"]

    await ballot.submit(db, participant_id=first, approved=slate.finalists[:1])
    with pytest.raises(ballot.BallotError) as hidden:
        await ballot.tally(db, room["session_id"])
    assert hidden.value.reason == "still_voting"
    assert await ballot.submitted_count(db, room["session_id"]) == (1, 2)

    await ballot.submit(db, participant_id=second, approved=slate.finalists[:2])
    revealed = await ballot.tally(db, room["session_id"])
    assert revealed, "on the last submission every approval becomes readable together"


async def test_the_winner_is_the_most_approved_with_ties_broken_by_group_score(db, world):
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"], z=Z)
    first, second = room["seats"][0]["id"], room["seats"][1]["id"]
    target = slate.finalists[1]

    await ballot.submit(db, participant_id=first, approved=[target])
    await ballot.submit(db, participant_id=second, approved=[target])
    out = await ballot.resolve(db, room["session_id"])

    assert out["chosen_title_id"] == target, "approvals decide, not the group score"
    assert out["approval_share"] == pytest.approx(1.0)
    assert out["participants"] == 2


async def test_the_approval_share_is_approvals_over_participants_and_is_persisted(db, world):
    """§13's headline metric for the whole feature, and §4.2 says where it lives. Persisted
    rather than derived: a share recomputed later moves with whatever the code does next, which
    is the opposite of a measurement."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"], z=Z)
    first, second = room["seats"][0]["id"], room["seats"][1]["id"]

    await ballot.submit(db, participant_id=first, approved=[slate.finalists[0]])
    await ballot.submit(db, participant_id=second, approved=[])
    out = await ballot.resolve(db, room["session_id"])

    assert out["approval_share"] == pytest.approx(0.5)
    stored = await db.fetchrow(
        "SELECT chosen_title_id, approval_share, participants FROM session_outcome "
        "WHERE session_id = $1",
        room["session_id"],
    )
    assert stored["chosen_title_id"] == out["chosen_title_id"]
    assert float(stored["approval_share"]) == pytest.approx(0.5)
    assert stored["participants"] == 2


async def test_resolving_twice_returns_the_same_outcome(db, world):
    """Two devices tapping reveal at the same moment is the normal case, and a share that
    changed between them would be the measurement moving under the thing it measures."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"], z=Z)
    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[slate.finalists[0]])

    first = await ballot.resolve(db, room["session_id"])
    second = await ballot.resolve(db, room["session_id"])
    assert first["chosen_title_id"] == second["chosen_title_id"]
    assert first["approval_share"] == pytest.approx(second["approval_share"])


async def test_a_ballot_may_only_name_titles_on_tonights_slate(db, world):
    """54e: "among the three finalists and the wildcard". A ballot over the whole pool is a
    ranking exercise, and one naming a title nobody was offered is a client bug that would
    otherwise land in §13's numbers."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    seat = room["seats"][0]["id"]
    with pytest.raises(ballot.BallotError) as off_slate:
        await ballot.submit(db, participant_id=seat, approved=[8])
    assert off_slate.value.reason == "not_on_slate"


async def test_an_empty_ballot_is_an_answer_rather_than_a_silence(db, world):
    """"tap everything you would be happy with" — none of them is a real answer, and it has to
    be distinguishable from not having voted, or the round never reveals."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[])

    assert await ballot.everyone_submitted(db, room["session_id"]) is True
    out = await ballot.resolve(db, room["session_id"])
    assert out["approval_share"] == pytest.approx(0.0)


# --- 54b: the hold-out arm, through the write path -----------------------------------------


async def test_a_held_out_answer_is_stored_as_held_out_and_moves_no_tilt(db, world):
    """54b: hold-out pairs are "used for neither selection nor stopping" — and the tilt feeds
    the tonight score the shortlist is built from, so it is the same stream. The client never
    names the arm: it comes off the served pair, which the server drew."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    pair = rnd.Pair(
        title_a=1, title_b=2, selection=rnd.SELECTION_HOLDOUT, reason="uniform-random",
    )
    await play.record_answer(
        db, participant_id=seat, pair=pair, answer=rnd.A, seq=1, latency_ms=None, z=Z
    )
    row = await db.fetchrow(
        "SELECT a.selection, p.tilt, p.answered_count FROM session_answer a "
        "JOIN session_participant p ON p.id = a.participant_id WHERE a.participant_id = $1",
        seat,
    )
    assert row["selection"] == "uniform_holdout"
    assert row["tilt"] == {}, "a held-out answer must not move the tilt"
    assert row["answered_count"] == 1, "it still costs the person one of their twenty"


async def test_the_match_lines_actually_name_something(db, world):
    """A guard against the fixture, not against the code.

    With no `dna_tag` rows every match line falls to the neutral branch, every `terms` list is
    empty, and the loop that checks "the term is carried" never runs — a test that passes while
    asserting nothing. So: at least one line on the winner names a term, and that term is one
    the winner carries.
    """
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    top = await db.fetchrow(
        "SELECT title_id, per_user_match FROM session_result WHERE session_id = $1 AND rank = 1",
        room["session_id"],
    )
    lines = top["per_user_match"]

    named = [t["term"] for line in lines.values() for t in line["terms"]]
    assert named, "the fixture carries no DNA, so this row's assertions are vacuous"
    carried = {
        r["term"] for r in await db.fetch(
            "SELECT term FROM dna_tagged WHERE title_id = $1 AND version = $2",
            top["title_id"], VOCAB,
        )
    }
    assert set(named) <= carried
    assert all(line["sign"] in ("pull", "against", "neutral", "none") for line in lines.values())


async def test_a_household_pulling_opposite_ways_gets_one_of_each(db, world):
    """§6.2 step 5 end to end, over real rows: two people answering in opposite directions on a
    polarised pool must produce a slate with a title from each pole and the sanctioned copy —
    not three heavy films under a line promising one of each.

    Titles 1-3 are heavy (`dread`), 4-6 light (`cosy`), so the mood axis is genuinely contested
    once the two of them answer against each other.

    NO SKIP. This test used to give up with `pytest.skip("this pool did not divide the household
    enough to surface a split")` the moment `slate.contested` came back None, and every
    assertion about the surfaced split sits after that line -- so the one integration-layer test
    of 54d over real rows reported a live regression as a green run. Mutation-proven both ways
    with `combine.contested_facet` patched to return None: against the skip the whole file
    reported `1 skipped` and passed, and against the assertion below it fails and names the
    cause. The coverage contract's rule 2 is existence-only (`test_spec_coverage.py` is a regex
    over `def test_*`), so a skipped test still closes its row -- which is how this could stand
    for a milestone. If the fixture ever genuinely stops dividing the household, the repair is
    the fixture: widen the salience gap between the two poles, or seat a third member. Not this
    line. [M4.12 finding 44]
    """
    room = await running_room(db, world)
    heavy_seat, light_seat = room["seats"][0]["id"], room["seats"][1]["id"]

    # Each answers toward their own pole: whichever side of the pair carries `dread` for one,
    # `cosy` for the other. `A`/`B` is decided by the pair the round happens to serve, so the
    # tilt is driven by choosing the title that matches the seat's pole.
    snapshot = await play.snapshot_of(db, room["session_id"])
    for seat, want in ((heavy_seat, "dread"), (light_seat, "cosy")):
        for _ in range(rnd.CAP_PAIRS):
            state = await play.state_for(db, seat, z=Z)
            if state["_pair"] is None or state["stop_reason"] is not None:
                break
            pair = state["_pair"]
            a_has = want in snapshot.dna.get(pair.title_a, {})
            b_has = want in snapshot.dna.get(pair.title_b, {})
            answer = rnd.A if a_has and not b_has else rnd.B if b_has and not a_has else rnd.EITHER
            await play.record_answer(
                db, participant_id=seat, pair=pair, answer=answer,
                seq=state["answered"] + 1, latency_ms=None, z=Z,
            )
    for seat in (heavy_seat, light_seat):
        row = await db.fetchrow(
            "SELECT ended_by, answered_count FROM session_participant WHERE id = $1", seat
        )
        if row["ended_by"] is None and row["answered_count"] >= rnd.ESCAPE_FROM_PAIR - 1:
            await play.escape(db, seat)

    slate = await play.finish(db, room["session_id"], z=Z)
    assert slate.contested is not None, (
        "this pool no longer divides the household enough to surface a split, so every "
        "assertion below is about nothing -- fix the fixture (widen the salience gap between "
        "titles 1-3 and 4-6, or seat a third member), do not skip"
    )

    stored = await db.fetch(
        "SELECT title_id, slot, conflict FROM session_result WHERE session_id = $1 "
        "AND slot IN ('finalist', 'wildcard') ORDER BY rank",
        room["session_id"],
    )
    weights = snapshot.axes[slate.contested]
    poles = [
        combine.axis_position(snapshot.dna.get(r["title_id"], {}), weights)
        for r in stored if r["slot"] == "finalist"
    ]
    assert any(p < 0 for p in poles) and any(p > 0 for p in poles), (
        "a surfaced split must put a title from each pole on the slate, not merely say so"
    )
    conflicts = [r["conflict"] for r in stored if r["conflict"] is not None]
    assert conflicts, "a surfaced split is stored"
    assert conflicts[0]["headline"].startswith(f"You're split on {slate.contested}")
    assert "hate" not in conflicts[0]["explanation"].lower()


# --- 54b + §13: the held-out stream, and the evaluation that reads only it -----------------


from spielplan.tonight import evaluation  # noqa: E402


async def test_the_shortlist_is_identical_with_the_held_out_answers_removed(db, world):
    """54b's requirement stated as a replay: "replaying the round with those answers removed
    produces the identical shortlist and the identical stopping point."

    Through the write path rather than in the abstract, because this is where the guard would
    actually fail: a combine that read every `session_answer` row would produce a different
    slate the moment a hold-out landed, and nothing above it would notice.
    """
    room = await running_room(db, world)
    seats = [s["id"] for s in room["seats"]]
    for seat in seats:
        await run_to_the_end(db, seat)
    # A hold-out answer that the round never saw, written after the fact so it cannot have
    # steered the pairs that were served.
    snapshot = await play.snapshot_of(db, room["session_id"])
    ids = sorted(snapshot.title_ids)[:2]
    await db.execute(
        "INSERT INTO session_answer "
        "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
        "VALUES ($1, $2, 99, $3, $4, 'A', 'uniform_holdout')",
        room["session_id"], seats[0], ids[0], ids[1],
    )

    with_holdout = await play.finish(db, room["session_id"], z=Z)
    await db.execute(
        "DELETE FROM session_answer WHERE session_id = $1 AND selection = 'uniform_holdout'",
        room["session_id"],
    )
    without = await play.finish(db, room["session_id"], z=Z)

    assert with_holdout.finalists == without.finalists
    assert with_holdout.wildcard == without.wildcard
    assert [t for t, _ in with_holdout.ranked] == [t for t, _ in without.ranked]


async def test_the_evaluation_reads_held_out_rows_and_nothing_else(db, world):
    """§13's guard from the other side. `round.replay` keeps the hold-out stream out of the
    model; this keeps everything else out of the evaluation. Two functions in two modules,
    because M3 found `duel.selection` had four read paths where it looked like one."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    snapshot = await play.snapshot_of(db, room["session_id"])
    ids = sorted(snapshot.title_ids)[:2]
    await db.execute(
        "INSERT INTO session_answer "
        "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
        "VALUES ($1, $2, 98, $3, $4, 'A', 'uniform_holdout')",
        room["session_id"], seat, ids[0], ids[1],
    )

    held = await evaluation.held_out_answers(db, room["session_id"])
    # Every held-out row and no other, rather than a hard-coded one: the arm is a rate drawn from
    # the seat's key now (decision 223), so whether the two answers above were adaptive is a fact
    # about this seat rather than something a count of 1 can assume. The claim is the filter's.
    stored = [
        r["seq"] for r in await db.fetch(
            "SELECT seq FROM session_answer WHERE session_id = $1 AND selection = $2 ORDER BY seq",
            room["session_id"], rnd.SELECTION_HOLDOUT,
        )
    ]
    assert [h["seq"] for h in held] == stored, "an adaptive answer must not reach the evaluation"
    assert 98 in stored

    total = await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE session_id = $1", room["session_id"]
    )
    assert total > len(held), "the fixture is only meaningful while adaptive rows also exist"


async def test_a_retracted_answer_is_not_evaluated(db, world):
    """§6's undo means the answer no longer counts. An evaluation that scored it would be
    measuring a tap the person took back."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    snapshot = await play.snapshot_of(db, room["session_id"])
    ids = sorted(snapshot.title_ids)[:2]
    await db.execute(
        "INSERT INTO session_answer "
        "(session_id, participant_id, seq, title_a, title_b, answer, selection, retracted_at) "
        "VALUES ($1, $2, 97, $3, $4, 'A', 'uniform_holdout', now())",
        room["session_id"], seat, ids[0], ids[1],
    )
    assert await evaluation.held_out_answers(db, room["session_id"]) == []


async def test_the_agreement_figure_carries_its_own_denominator(db, world):
    """M3's lesson on `Agreement.rate`: None rather than 0.0 on an empty sample, because a rate
    over no pairs is not a number and printing 0.00 beside it reads as a bad result rather than
    an absent one. §0 fixes a noise floor that calls anything under 0.003–0.008 a tie."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    # The round serves its own hold-out at pair 10 and 20 (54b), and those are covered by
    # test_the_shortlist_is_identical_with_the_held_out_answers_removed. This test is about the
    # figure's arithmetic, so it starts from a stream it controls.
    await db.execute(
        "DELETE FROM session_answer WHERE session_id = $1 AND selection = 'uniform_holdout'",
        room["session_id"],
    )
    empty = await evaluation.shortlist_agreement(db, room["session_id"])
    assert empty.rate is None and empty.decisive == 0

    slate = await db.fetch(
        "SELECT title_id, slot FROM session_result WHERE session_id = $1 ORDER BY rank",
        room["session_id"],
    )
    finalist = next(r["title_id"] for r in slate if r["slot"] == "finalist")
    other = next(r["title_id"] for r in slate if r["slot"] != "finalist")
    seat = room["seats"][0]["id"]
    await db.execute(
        "INSERT INTO session_answer "
        "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
        "VALUES ($1, $2, 96, $3, $4, 'A', 'uniform_holdout')",
        room["session_id"], seat, finalist, other,
    )
    agreed = await evaluation.shortlist_agreement(db, room["session_id"])
    assert agreed.decisive == 1 and agreed.agreed == 1 and agreed.rate == pytest.approx(1.0)


async def test_a_level_answer_is_counted_but_not_decisive(db, world):
    """`either` and `neither` say nothing about which side of the boundary a title is on.
    Folding one into a side would invent a threshold the person did not cross — the same
    refusal `rank/evaluation.py` makes for a model tie."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    # The round serves its own hold-out at pair 10 and 20 (54b), and those are covered by
    # test_the_shortlist_is_identical_with_the_held_out_answers_removed. This test is about the
    # figure's arithmetic, so it starts from a stream it controls.
    await db.execute(
        "DELETE FROM session_answer WHERE session_id = $1 AND selection = 'uniform_holdout'",
        room["session_id"],
    )
    slate = await db.fetch(
        "SELECT title_id, slot FROM session_result WHERE session_id = $1 ORDER BY rank",
        room["session_id"],
    )
    finalist = next(r["title_id"] for r in slate if r["slot"] == "finalist")
    other = next(r["title_id"] for r in slate if r["slot"] != "finalist")
    seat = room["seats"][0]["id"]
    await db.execute(
        "INSERT INTO session_answer "
        "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
        "VALUES ($1, $2, 95, $3, $4, 'EITHER', 'uniform_holdout')",
        room["session_id"], seat, finalist, other,
    )
    figure = await evaluation.shortlist_agreement(db, room["session_id"])
    assert figure.pairs == 1 and figure.decisive == 0 and figure.rate is None


async def test_the_report_carries_the_rate_at_which_each_ending_fired(db, world):
    """54b's second added row and §14 risk 6's own words: "the rate at which the cap and the
    escape control fire". Unrecoverable if the three endings were not distinguishable."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"], z=Z)
    reported = await evaluation.report(db, room["session_id"])

    assert set(reported["ended_by"]) == set(rnd.END_REASONS)
    assert sum(reported["ended_by"].values()) == len(room["seats"])
    assert "chosen_title_id" not in reported, (
        "§13 evaluates the round; a report that can name a candidate invites a surface to draw "
        "it, which is how a held-out stream stops being held out"
    )


# --- §6.2 step 8: solo -----------------------------------------------------------------------


from spielplan.tonight import solo  # noqa: E402


async def solo_picks(db, world, **kw):
    params = dict(
        user_id=world["patrick"], kind="movie", budget_min=200,
        include_rewatches=True, bundle_version=BUNDLE,
        # What the route passes (decision 223): 54f mints no session row, so `user.id` is the only
        # thing about a solo sharpen round that is stable from one request to the next.
        holdout_key=str(world["patrick"]),
    )
    params.update(kw)
    return await solo.picks(db, **params)


async def test_solo_lands_on_three_picks_and_a_wildcard_with_no_round_first(db, world):
    """54f: "lands **directly on three picks and a wildcard** ranked by the personal Ledger with
    no tilt — the fastest path to a film must not be slower than browsing Home." The prototype
    forced the question round before showing any pick, which §6.2 never asked for."""
    out = await solo_picks(db, world)

    assert len(out["picks"]) == solo.PICKS
    assert out["wildcard"] is not None
    assert out["wildcard"]["title_id"] not in {p["title_id"] for p in out["picks"]}
    assert out["sharpened"] is False
    assert out["answered"] == 0


async def test_the_picks_are_the_persons_own_ledger_order_with_no_tilt(db, world):
    """"ranked by the personal Ledger with **no tilt**". Patrick's and Jenny's orders differ in
    the fixture, so a solo run that returned the household average would be visible."""
    mine = await solo_picks(db, world, user_id=world["patrick"])
    theirs = await solo_picks(db, world, user_id=world["jenny"])

    assert [p["title_id"] for p in mine["picks"]] != [p["title_id"] for p in theirs["picks"]]


async def test_every_pick_carries_a_why_and_a_budget_fit_line(db, world):
    """§6.8 makes the one-line why mandatory for every recommendation, and §6.2 step 8 fixes
    both branches of the fit line. Solo is the surface a person reaches when they want a film
    in one tap, so an unexplained pick fails the register at its cheapest point."""
    out = await solo_picks(db, world, budget_min=130)

    for card in [*out["picks"], out["wildcard"]]:
        assert card["why"], "a pick with no why is a pick with no reason"
        assert card["fit_line"], "the soft budget is only honest if the label is there"
        runtime = card["runtime_min"]
        if runtime is not None and runtime > 130:
            assert card["fit_line"] == f"runs {runtime - 130} min over"
            assert runtime <= 130 + 40, "nothing past the +40 admission bound"
        elif runtime is not None:
            assert card["fit_line"] == "fits your 130 min"


async def test_the_wildcard_says_it_is_a_stretch_and_the_picks_do_not(db, world):
    """§6.2 step 8 gives the two forms: "pulls you with {terms}" / "a stretch — outside your
    usual". §6.4 makes the wildcard's label the honest half of a measured cost."""
    out = await solo_picks(db, world)

    assert out["wildcard"]["why"] == solo.STRETCH_WHY
    for card in out["picks"]:
        assert card["why"] != solo.STRETCH_WHY


async def test_a_why_line_only_names_terms_the_pick_carries(db, world):
    """The §6.0 invariant again: a card shown under a reason it does not satisfy. The tilt may
    order the terms; it may never admit one."""
    out = await solo_picks(db, world)
    named = 0
    for card in out["picks"]:
        carried = {
            r["term"] for r in await db.fetch(
                "SELECT term FROM dna_tagged WHERE title_id = $1 AND version = $2",
                card["title_id"], VOCAB,
            )
        }
        for term in card["terms"]:
            named += 1
            assert term["term"] in carried
    assert named, "the fixture carries no DNA, so this assertion is vacuous"


async def test_the_provenance_line_reports_the_budget_and_the_filter(db, world):
    """54f fixes both forms and says the tilted one replaces the other rather than joining it —
    the prototype concatenated them, so the line claimed a tilt on a round nobody had run."""
    plain = await solo_picks(db, world, budget_min=130, include_rewatches=False)
    assert plain["provenance"] == "130 min budget · unseen first"


async def test_sharpening_re_ranks_in_place_and_changes_the_provenance_line(db, world):
    """54f: "A **sharpen this** control runs the same adaptive round against the same pool and
    re-ranks in place; the provenance line then reads 'tilted by your N answers' instead of
    'unseen first'.""" ""
    # Rewatches included so the pool is larger than the shortlist: three candidates ARE the
    # shortlist, so a round over them has nothing to ask and converges before serving a pair.
    # `sharpen=True` because 54f's third control is now the only thing that draws a pair: the
    # door and Reshuffle land on picks and pay for no search (finding 35). This test is about
    # the control, so it presses it.
    first = await solo_picks(db, world, budget_min=130, include_rewatches=True, sharpen=True)
    assert first["pair"] is not None, "the sharpen round has to have something to ask"

    answered = [
        rnd.Answered(seq=1, title_a=first["pair"]["a"]["title_id"],
                     title_b=first["pair"]["b"]["title_id"], answer=rnd.A)
    ]
    after = await solo_picks(
        db, world, budget_min=130, include_rewatches=True, answers=answered, sharpen=True
    )

    assert after["provenance"] == "130 min budget · tilted by your 1 answers"
    assert "unseen first" not in after["provenance"], "54f says instead of, not as well as"
    assert after["sharpened"] is True
    assert len(after["picks"]) == solo.PICKS, "re-ranked in place, not replaced by a queue"


async def test_reshuffle_walks_the_ranking_rather_than_redrawing(db, world):
    """§6.2 step 8 names the control; a random re-draw from a ranked list either returns the
    same top titles or silently degrades the picks."""
    first = await solo_picks(db, world)
    second = await solo_picks(db, world, offset=1)

    assert [p["title_id"] for p in first["picks"]] != [p["title_id"] for p in second["picks"]]


async def test_solo_leaves_no_session_row_and_publishes_no_room(db, world):
    """§6.2 step 8: "no session row". A solo evening that minted one would make itself joinable
    by every household device and would pollute §13's approval-share population with
    one-participant rows, where approval share is not a measurement at all."""
    before = {
        table: await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in ("session", "session_participant", "session_answer",
                      "session_result", "session_outcome", "session_ballot")
    }
    out = await solo_picks(db, world)
    await solo_picks(db, world, offset=2)
    if out["pair"] is not None:
        await solo_picks(db, world, answers=[
            rnd.Answered(seq=1, title_a=out["pair"]["a"]["title_id"],
                         title_b=out["pair"]["b"]["title_id"], answer=rnd.A)
        ])

    after = {
        table: await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in before
    }
    assert after == before
    assert await rooms.open_rooms(db, viewer_id=world["jenny"]) == []


async def test_solo_writes_no_observation_of_any_kind(db, world):
    """"A **reshuffle** control walks further down the ranking" — a browse gesture, not an
    observation. A control that quietly taught the Ledger "you rejected these three" would put
    a navigation action on §5.2's one write-path for taste."""
    before = {
        table: await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in ("verdict", "duel", "tier_edit", "user_title")
    }
    out = await solo_picks(db, world)
    await solo_picks(db, world, offset=1)
    if out["pair"] is not None:
        await solo_picks(db, world, answers=[
            rnd.Answered(seq=1, title_a=out["pair"]["a"]["title_id"],
                         title_b=out["pair"]["b"]["title_id"], answer=rnd.NEITHER)
        ])
    after = {table: await db.fetchval(f"SELECT count(*) FROM {table}") for table in before}
    assert after == before


async def test_solo_uses_the_same_pool_as_a_group_session_would(db, world):
    """"from the same pool" is what makes solo a mode of Tonight rather than a second
    recommender: owned only, one kind, the soft budget, the rewatch setting."""
    out = await solo_picks(db, world, budget_min=130, include_rewatches=False)
    shown = {p["title_id"] for p in out["picks"]}
    if out["wildcard"]:
        shown.add(out["wildcard"]["title_id"])

    assert 8 not in shown, "not owned"
    assert 7 not in shown, "a series in a film session"
    assert 4 not in shown, "200 minutes is past 130 + 40"


async def test_an_empty_pool_says_what_to_change(db, world):
    """§6.2 gives the happy path only. "You have seen everything that fits" is a real state on
    a household library, and the honest answer names the two controls that would change it."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) "
        "SELECT $1, id, 'seen' FROM title WHERE kind = 'movie' "
        "ON CONFLICT (user_id, title_id) DO UPDATE SET state = 'seen'",
        world["patrick"],
    )
    out = await solo_picks(db, world, budget_min=200, include_rewatches=False)

    assert out["picks"] == [] and out["wildcard"] is None
    assert "widen the budget" in out["empty"] and "include rewatches" in out["empty"]


async def test_the_sharpen_pair_carries_no_score(db, world):
    """§6.2 step 3: the pool is "internal — never shown as a step". A sharpen card that shipped
    the candidate's own score would put the ranking on the screen it is kept off.

    NO SKIP. It used to give up when the pool served no pair, which is a registered test
    asserting nothing on the runs where it matters (finding 44). The door does not serve a pair
    at all now — that is finding 35's repair, not a shortfall — so the sharpen control is pressed
    here and the pair is asserted to exist before anything is said about it.
    """
    out = await solo_picks(db, world, sharpen=True)
    assert out["pair"] is not None, "the sharpen control has to ask something on this pool"
    for side in ("a", "b"):
        assert "scores" not in out["pair"][side]
        assert "group_score" not in out["pair"][side]


# --- the adversarial review's survivors ------------------------------------------------------


async def test_the_answer_after_an_undo_is_accepted(db, world):
    """§6 preamble's "undo everywhere" — the whole of it, which is undo AND carry on.

    `retract` tombstones the row (§14 risk 6: log every vote) and rewinds the counter, and the
    replacement answer then has to land somewhere. 0013's non-partial unique index on
    (participant_id, seq) made it collide with the tombstone forever: one tap on Undo ended that
    participant's round, and because 54e's reveal waits for every seat, the household's evening
    with it. The row's own words — "the round is served a pair again rather than advancing" — are
    what this asserts, and no test reached the *re-answer*.

    WHERE IT LANDS CHANGED IN M4.12 AND THE REQUIREMENT DID NOT. The replacement used to take the
    seq the retraction freed, which made `answered_count` and the row's identity one number and let
    an interleaved answer wedge the seat for ever (finding 11). The seq is now minted from every
    row there has ever been, so the tombstone keeps its 2 and the replacement takes 3 — which is
    what 0014's partial index always permitted, and one fewer thing for the index to have to
    forgive. "Undo and carry on" is unchanged, and that is the clause §6's preamble writes.
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    await play.retract(db, seat, z=Z)

    assert await answer_once(db, seat) is not None, "the round has to be able to continue"
    live = await db.fetch(
        "SELECT seq FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL "
        "ORDER BY seq",
        seat,
    )
    assert [r["seq"] for r in live] == [1, 3]
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 3, "and the tombstone is still there, because §14 risk 6 says log every vote"


async def test_a_guest_is_ranked_by_the_pools_order_rather_than_a_flat_prior(db, world):
    """`tonight-rank-guest-not-borrowed-ledger`: "A guest with no grid profile is **ranked by
    the candidate pool's own member-average order**".

    54c says the same thing from the other side — a participant with no Ledger "starts from the
    **pool prior**". A flat prior is not the pool prior: it is no prior, it ranks nothing, and
    it makes every candidate straddle the shortlist boundary at once, which turns the selection
    rule's pair search from O(k²) over the straddlers into O(n²) over the whole pool with an
    O(n) update inside each — the guest's evening gets measurably slower than everyone else's
    for no information gained.
    """
    room = await running_room(db, world, guests=1)
    snapshot = await play.snapshot_of(db, room["session_id"])
    guest = next(s for s in room["seats"] if s["role"] == "guest")

    beliefs = rnd.initial(snapshot.member_average(), prior_var=1.0, has_profile=False)
    means = {t: b.mu for t, b in beliefs.items()}
    pool_order = sorted(snapshot.member_average(), key=lambda t: -snapshot.member_average()[t])
    guest_order = sorted(means, key=lambda t: -means[t])

    assert guest_order == pool_order, "the guest is ranked by the pool's own order"
    assert len(set(means.values())) > 1, "a flat prior ranks nothing and straddles everything"
    # And still no member's Ledger: the pool average is not any one person's scores.
    host = next(s for s in room["seats"] if s["role"] == "host")
    assert means != snapshot.pool_scores_for(host["id"])
    assert guest["id"] not in {p for s in snapshot.scores.values() for p in s}

async def test_two_seats_finishing_together_do_not_both_write_the_slate(db, world, pg_url):
    """54e's reveal is simultaneous, so the combine runs on whichever answer finishes the room --
    and when two people finish at the same moment, that is both of them.

    `_announce` reads `SELECT state FROM session` and then calls `finish` in a separate
    statement, with no lock between them. `everyone_finished` turns true the instant the last of
    the two final answers commits, so if the second commits before the first's announce reads,
    both see `voting` and both combine. `finish` is `DELETE FROM session_result` then re-INSERT,
    and the loser's DELETE cannot see the winner's uncommitted rows -- so it deletes nothing,
    inserts rank 1, blocks on the winner's, and is refused by `session_result_rank`.

    Two real connections, because that is the only place this exists: run them in sequence and
    the second simply rewrites the first's committed slate, which is why the suite was green.

    The database is what keeps the evening intact -- the unique index means there is never a
    doubled slate. What it costs is a 500 on the last answer of somebody's round, on a room that
    is by then perfectly fine, which is exactly what `ballot.resolve` was made idempotent to
    avoid at the other end of the same beat.
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    title_id = await db.fetchval("SELECT id FROM title LIMIT 1")

    winner = await asyncpg.connect(pg_url)
    try:
        # The winner's transaction, open and uncommitted: its slate exists and nobody else can
        # see it. This is the state the loser's DELETE runs against.
        txn = winner.transaction()
        await txn.start()
        # Standing exactly where the winning `finish` stands: holding the session's lock, with
        # its slate written and not yet visible to anyone else. Referencing the module's own
        # constant rather than repeating the number, so the two cannot drift apart.
        await winner.execute(
            "SELECT pg_advisory_xact_lock($1, $2)", play._FINISH_LOCK, session_id
        )
        await winner.execute(
            "INSERT INTO session_result (session_id, title_id, rank, slot, group_score) "
            "VALUES ($1, $2, 1, 'finalist', 0.5)",
            session_id, title_id,
        )

        loser = asyncio.create_task(play.finish(db, session_id, z=Z))
        await asyncio.sleep(0.3)
        assert not loser.done(), "the loser has to wait rather than write into the same slate"
        await txn.commit()

        # It must not raise. Before the fix it came back as an unhandled UniqueViolationError,
        # which the route has no handler for: a 500 on the last answer of the round.
        await asyncio.wait_for(loser, timeout=15)
    finally:
        await winner.close()

    ranks = [
        r["rank"]
        for r in await db.fetch(
            "SELECT rank FROM session_result WHERE session_id = $1 ORDER BY rank", session_id
        )
    ]
    assert ranks == sorted(set(ranks)), f"one slate, not two: {ranks}"


class _FailsOnTheNthSeat:
    """A connection that writes the room and then loses the next seat.

    The same shape as the two racing proxies above, and here for the same reason: the failure
    this is about happens BETWEEN two statements, and nothing outside the function can stand
    there otherwise.
    """

    def __init__(self, conn, *, fail_on: int):
        self._conn = conn
        self._fail_on = fail_on
        self.seats = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, query, *args, **kw):
        if "INSERT INTO session_participant" in query:
            self.seats += 1
            if self.seats == self._fail_on:
                raise asyncpg.PostgresConnectionError("the write went nowhere")
        return await self._conn.execute(query, *args, **kw)


async def test_a_room_that_fails_to_open_does_not_take_the_previous_one_with_it(db, world):
    """The abandon is destructive, so it may not run before anything that can fail.

    It did. `open_session` abandoned the host's other open rooms first and then did the fallible
    work: allocate a code, which is refused after twenty collisions, then three inserts, each its
    own autocommit statement. Any of those failing left the host's live evening abandoned with no
    room in its place -- a tap on "Together" that answers 409 and takes away the room they were
    already in.

    Forced by making the allocation fail: every code the generator returns is one that is already
    taken, so the loop exhausts its attempts and raises. What this test is about is what survives.
    """
    live = await open_room(db, world)
    hers = await open_room(db, world, host_user_id=world["jenny"])

    monkey = pytest.MonkeyPatch()
    monkey.setattr(rooms, "make_code", lambda _rng: live["room_code"])
    try:
        with pytest.raises(rooms.RoomError) as refused:
            await open_room(db, world)
    finally:
        monkey.undo()
    assert refused.value.reason == "no_code"

    row = await db.fetchrow(
        "SELECT state, ended_at FROM session WHERE id = $1", live["session_id"]
    )
    assert row["state"] == rooms.STATE_OPEN, "the room they were in is still the room they are in"
    assert row["ended_at"] is None
    listed = {r["session_id"] for r in await rooms.open_rooms(db, viewer_id=world["patrick"])}
    assert live["session_id"] in listed
    assert hers["session_id"] in listed, "and nobody else's room moved either"


async def test_opening_a_room_is_one_fact_rather_than_four(db, world):
    """The session row, its seats and the abandonment land together or not at all.

    Every statement in `open_session` used to stand alone, which was survivable while none of
    them took anything away. §6.2 step 2 seats guests at open time precisely so the round can
    refuse a guest's turn early -- a room that kept three of its five seats because the fourth
    insert was lost silently stops doing that, and there is no later moment that repairs it.
    """
    live = await open_room(db, world)
    before = await db.fetchval("SELECT count(*) FROM session")

    # Fail on the third seat: the session row and the host seat are already written by then.
    flaky = _FailsOnTheNthSeat(db, fail_on=3)
    with pytest.raises(asyncpg.PostgresError):
        await rooms.open_session(
            flaky, host_user_id=world["patrick"], kind="movie", budget_min=130,
            include_rewatches=False, bundle_version=BUNDLE, guests=3,
        )
    assert flaky.seats == 3, "the failure landed where this test needs it"

    assert await db.fetchval("SELECT count(*) FROM session") == before, "no half-made room"
    assert await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id NOT IN "
        "(SELECT id FROM session)"
    ) == 0
    row = await db.fetchrow("SELECT state FROM session WHERE id = $1", live["session_id"])
    assert row["state"] == rooms.STATE_OPEN, "and the abandon did not land without its room"


async def test_each_match_line_branch_says_the_thing_it_is_for(db, world):
    """§6.2 step 7's three cases, one assertion each. Neither of the two below the pull had one.

    `play._match_lines` sorts a title's carried terms by the participant's tilt and picks a
    branch: terms pulling toward them, terms working against them, or neither. Mutating the sign
    comparison that separates the second from the third left 109 tests passing, because every
    test that reached this code answered `A` on everything and only ever produced pulls. §6.8
    makes the copy the rule rather than decoration, so a branch nothing reads is a sentence the
    household could be shown that nobody has checked.

    Driven through the real builder with a hand-made tilt, because the branch is a property of
    the tilt and the title's terms together and no seeded round reliably produces all three.
    """
    room = await running_room(db, world)
    snapshot = await play.snapshot_of(db, room["session_id"])
    title_id = next(t for t, terms in snapshot.dna.items() if terms)
    carried = sorted(snapshot.dna[title_id])
    assert carried, "the title this test is about has to carry something"

    # The same rows `finish` hands the builder: it reads `name` and `seat` off them, so
    # `rooms.seats_of`'s narrower shape is not what this seam takes.
    seats_sql = (
        "SELECT p.id, p.role, p.tilt, p.seat, u.name FROM session_participant p "
        "LEFT JOIN app_user u ON u.id = p.user_id WHERE p.session_id = $1 ORDER BY p.seat"
    )
    first = (await db.fetch(seats_sql, room["session_id"]))[0]["id"]

    async def line_for(tilt):
        await db.execute("UPDATE session_participant SET tilt = $1 WHERE id = $2", tilt, first)
        seats = await db.fetch(seats_sql, room["session_id"])
        lines = await play._match_lines(db, snapshot=snapshot, seats=seats, title_id=title_id)
        return lines[str(first)]

    pulled = await line_for({carried[0]: 1.0})
    assert pulled["sign"] == "pull"
    assert carried[0] in pulled["line"], "the line names the term that earned it"

    against = await line_for({t: -1.0 for t in carried})
    assert against["sign"] == "against"
    assert "works against them" in against["line"], "§6.2 step 7's honest negative"
    assert against["terms"] and against["terms"][0]["term"] in carried, (
        "and it names a term the title carries, not one chosen for contrast"
    )

    # The third case: nothing the title carries moves this person either way. It gets a line of
    # its own rather than the no-pull line, which needs a term this participant does not have.
    neutral = await line_for({t: 0.0 for t in carried})
    assert neutral["sign"] == "neutral"
    assert neutral["terms"] == [], "no term reached either sign, so none is named"
    assert "works against them" not in neutral["line"]
    assert neutral["line"], "a participant is never omitted"


from spielplan.tonight import dna as tonight_dna  # noqa: E402


async def test_a_projection_never_outranks_a_quote_verified_tag_on_this_surface(db, world):
    """Decision 188's bound, asserted through the two readers §6.2 actually calls.

    The plan's risk paragraph is that the bounded projected branch "changes Tonight, not only
    Home" — the tilt vectors, the authored-axis positions, `terms_carried_by` and §6.2 step 7's
    match lines all read the number. What shipped to cover that was a substring assertion over
    the SQL fragment and a band query issued straight against `dna_tagged`: reverting the
    expression left every assertion in the four Tonight suites passing, because their fixtures
    seeded `weight` as a confidence and no seeded value could reach the extracted floor.

    Title 2 carries `dread` in both tiers, quote-verified at salience 2 and inferred from 8
    keyword sources. The quote-verified reading is the one the surface must speak with, and the
    loudest term — the one a match line names first — must be the tag with a quote behind it.
    Under `0.30 * n_sources` the projection reads 2.40 and takes both. [M49-D188-02]
    """
    extracted = 0.60 + 0.40 * (2 / 3)        # salience 2, the shipped extracted expression

    carried = await tonight_dna.terms_carried_by(db, 2, version=VOCAB)
    assert carried[0]["term"] == "relentless", (
        "an inferred term is the loudest thing this title carries, so the match line names it "
        f"first: {[(t['term'], round(t['weight'], 3)) for t in carried]}"
    )
    assert max(t["weight"] for t in carried) <= 1.0, (
        "1.00 is the extracted tier's cap and nothing may speak louder than it"
    )
    assert next(t for t in carried if t["term"] == "dread")["weight"] == pytest.approx(extracted)

    vectors = await tonight_dna.vectors_for(db, [2], version=VOCAB)
    assert vectors[2]["dread"] == pytest.approx(extracted), (
        "the tilt and the authored-axis positions read this vector, so the two tiers crossing "
        "moves where the round thinks a title sits"
    )


# --- M4.12 findings 4 and 42: one lifecycle owner, and the invitation off the request path -----
#
# Two rules the router used to own outright. The `voting -> ballot` transition lived inside the
# answer handler's `_announce`, which made a phone's POST its only caller: one transient failure
# left the evening in `voting` for ever, with every vote intact and nothing able to reach them.
# And §6.2 step 2's push fan-out was a closure in the same module, so who gets invited and what
# the invitation says were observable only through the ASGI app and a push transport.
# `play.settle` and `rooms.invite` are where the two live now. The route half — that a GET of the
# session, the ballot or the result calls the first of them, and that opening a room does not wait
# for the second — is in `test_tonight_routes.py`, because only the route can get that wrong.

from spielplan.tonight import result  # noqa: E402


async def test_a_room_whose_every_seat_has_finished_is_settled_by_a_read(db, world):
    """§6.2 steps 5-6 (54e): the transition needs an owner a read can call.

    `finished_session` leaves exactly the state finding 4 describes — every member seat ended,
    the room still in `voting`, no slate — because it answers through `play.record_answer` and
    never through the handler that used to be the combine's only caller. That is not a contrived
    fixture: it is what a dropped connection between the last answer's commit and the combine
    leaves behind, and what a container restart mid-request leaves behind.
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]

    assert await play.everyone_finished(db, session_id) is True
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_VOTING
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) == 0, "the votes are in and there is nothing to read them with"

    assert await play.settle(db, session_id, z=Z) is True, "this call is what moved the room"

    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_BALLOT
    slate = await ballot.slate_of(db, session_id)
    assert [r["slot"] for r in slate].count(combine.SLOT_WILDCARD) == 1
    assert 1 <= sum(1 for r in slate if r["slot"] == combine.SLOT_FINALIST) <= 3


async def test_settling_a_room_twice_leaves_the_one_slate(db, world):
    """Every read calls `settle`, so a second caller is the ordinary case rather than the race.

    `finish` is safe to repeat by construction — `pg_advisory_xact_lock` and then DELETE plus
    re-INSERT in one transaction — and `ballot.resolve` is idempotent at the other end. The
    boolean is what keeps a caller from acting on a transition that was somebody else's: it is
    what decides whether `_announce` pushes a lobby frame.
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    assert await play.settle(db, session_id, z=Z) is True
    first = [
        tuple(r) for r in await db.fetch(
            "SELECT title_id, rank, slot FROM session_result WHERE session_id = $1 ORDER BY rank",
            session_id,
        )
    ]

    assert await play.settle(db, session_id, z=Z) is False, "the room has already moved on"

    again = [
        tuple(r) for r in await db.fetch(
            "SELECT title_id, rank, slot FROM session_result WHERE session_id = $1 ORDER BY rank",
            session_id,
        )
    ]
    assert again == first, "one slate, whoever reads the room"


async def test_a_room_that_is_still_answering_is_not_settled_by_a_read(db, world):
    """54e's reveal is simultaneous, so a read may not close a room one seat is still playing.

    The guard is `everyone_finished`, the same predicate the answer handler used — what this
    milestone changes is where it is called from, not what it says.
    """
    room = await running_room(db, world)
    session_id = room["session_id"]
    await run_to_the_end(db, room["seats"][0]["id"])

    assert await play.settle(db, session_id, z=Z) is False
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_VOTING
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) == 0


async def test_the_invitation_asks_every_member_even_when_one_send_fails(db, world):
    """§6.2 step 2's push, as a rule about who is asked and what they are told.

    The sender is a callable (arch-06) so that rule can be asserted without an ASGI app and a
    push transport: `tag` and `url` are the two fields the service worker cannot invent
    (syncpush-10), and a failing send may not take the next member's invitation with it, because
    §6's preamble makes push best-effort with an in-app equivalent for every prompt.
    """
    room = await open_room(db, world)
    asked: list[tuple[int, dict]] = []

    async def flaky(conn, user_id, payload):
        asked.append((user_id, payload))
        raise RuntimeError("no route to the push service")

    await rooms.invite(
        db, flaky, session_id=room["session_id"], host_user_id=world["patrick"],
        room_code=room["room_code"],
    )

    assert [u for u, _ in asked] == [world["jenny"]], (
        "the host is holding the phone that opened the room"
    )
    payload = asked[0][1]
    assert payload["kind"] == "tonight.invite"
    assert payload["tag"] == f"tonight:{room['session_id']}"
    assert payload["url"] == "/tonight"
    assert room["room_code"] in payload["body"]


async def test_the_reveal_is_assembled_where_the_other_tonight_rules_are(db, world):
    """arch-06: four of §6.2 step 7's rules used to live in a closure inside the router.

    The wildcard's label, which rows reach the screen at all, the budget the fit line is measured
    against, and the runners-up order — and three of the four had been wrong at least once,
    because none was reachable without the ASGI app. §6.2 step 7's "runners-up" are the titles
    that RAN, ordered by how close they came: `rank` is 1-best, so the tie-break is ascending,
    and the pool's tail (`runner_up`, on no ballot and therefore always "0 approved") is not a
    runner-up at all.
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    await play.settle(db, session_id, z=Z)
    slate = await ballot.slate_of(db, session_id)
    # Approve the wildcard alone, so the winner is a card the rank order would not have chosen
    # and the losing finalists line up behind it on approvals rather than on rank.
    wildcard_id = next(r["title_id"] for r in slate if r["slot"] == combine.SLOT_WILDCARD)
    for seat in await db.fetch(
        "SELECT id FROM session_participant WHERE session_id = $1", session_id
    ):
        await ballot.submit(db, participant_id=seat["id"], approved=[wildcard_id])
    counted = await ballot.tally(db, session_id)
    outcome = await ballot.resolve(db, session_id)

    card = await result.slate(db, session_id, counted, outcome, play_url=None)

    assert card["beat"] == "VOTES REVEALED TOGETHER"
    assert card["winner"]["title_id"] == wildcard_id
    assert card["winner"]["label"] == combine.WILDCARD_LABEL, (
        "the honestly-labelled wildcard, from the one place that holds the words"
    )
    assert card["winner"]["play_url"] is None, "absent rather than guessed with no connector"
    assert {c["slot"] for c in card["runners_up"]} <= {
        combine.SLOT_FINALIST, combine.SLOT_WILDCARD
    }, "the pool's tail was never on a ballot, so it is not a runner-up"
    assert all(c["title_id"] != wildcard_id for c in card["runners_up"])
    order = [(-c["approvals"], c["rank"]) for c in card["runners_up"]]
    assert order == sorted(order), "most approved first, then the closest on rank"
    assert card["winner"]["fit_line"], "the budget fit line the reveal prints"
    assert card["winner"]["kind"] == "movie", "a runtime means nothing without the kind"


class _CountsTheRowsFetched:
    """A connection that remembers how many rows each `fetch` handed back."""

    def __init__(self, conn):
        self._conn, self.rows = conn, 0

    async def fetch(self, *args, **kw):
        out = await self._conn.fetch(*args, **kw)
        self.rows += len(out)
        return out

    def __getattr__(self, name):
        return getattr(self._conn, name)


async def test_the_reveal_reads_the_slate_rather_than_the_whole_pool(db, world):
    """`session_result` holds the POOL, not the slate, which is what makes the filter's home matter.

    `combine.sequence` is a permutation of every candidate and `finish` writes a row per element,
    so a session on the shipped 696-title owned pool stores 696 rows to put four cards on a screen.
    `result.slate` selected all of them -- joined to `title`, with `per_user_match` jsonb on every
    row -- and then discarded all but the finalists and the wildcard in Python, while
    `ballot.slate_of` reads the same table with `slot IN ('finalist', 'wildcard')` in SQL. Two
    readers of one table disagreeing about where the filter lives is the kind of thing arch-06
    lifted this module out of the router to make visible; the reveal is §6.2 step 7's payoff and
    the one request the whole round exists to produce.

    Counted rather than timed, for `test_the_solo_door_lands_on_picks_without_running_the_pair_
    search`'s reason: a six-film fixture is fast either way, and the claim is about rows crossing
    the wire and not about this machine. [M4.12 review cycle 1: M412-RESULT-4]
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    await play.settle(db, session_id, z=Z)
    for seat in await db.fetch(
        "SELECT id FROM session_participant WHERE session_id = $1", session_id
    ):
        await ballot.submit(db, participant_id=seat["id"], approved=[])
    counted = await ballot.tally(db, session_id)
    outcome = await ballot.resolve(db, session_id)

    counting = _CountsTheRowsFetched(db)
    card = await result.slate(counting, session_id, counted, outcome, play_url=None)

    stored = await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    )
    on_the_ballot = await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1 "
        "AND slot IN ('finalist', 'wildcard')",
        session_id,
    )
    assert stored > on_the_ballot, (
        f"this claim is about a table that holds the pool's tail, and it holds {stored} rows"
    )
    assert len(card["finalists"]) + bool(card["wildcard"]) == on_the_ballot
    assert counting.rows == on_the_ballot, (
        f"the reveal fetched {counting.rows} joined rows to render {on_the_ballot} cards"
    )


# --- M4.12 findings 5, 6 and 34: the claim, the seat that cannot be asked, the named refusal ----
#
# Three holes with one shape: the round was built from a set of seats nobody held still, and a
# seat the round could not ask anything had no way to end. `play.start` read the seats, built the
# pool and flipped the state last, so a join was admitted for the whole duration of the build --
# tens of seconds on a real library -- and the seat that arrived in that window had no entry in
# the frozen `scores` map. From there every exit was closed: `boundary` is None on a prior of
# nothing, so `stop_reason` is `converged` at zero answers, `_end` is reachable only from an
# answer (which needs a pair) and from the escape (refused below five answers),
# `everyone_finished` stays false, and no route can close the room. The same dead end arrives
# with no race at all on a pool of two or three candidates (decision 215) -- a series night with
# three owned shows, or a tight budget on a modest library.
#
# So the claim is the first statement of `start` and join's rule is a property of its own INSERT
# (finding 5), a seat whose replay has a reason and no pair ends itself (finding 6, and the reason
# the two pool sizes below reach the ballot rather than waiting), and a member the pool cannot
# rank is named in the refusal instead of being reported as a budget problem (finding 34,
# decision 216).

import json  # noqa: E402


async def second_connection(pg_url):
    """Another real connection to the same database, codecs and all.

    `conftest.db` registers a JSON codec on `json` and `jsonb`, and `play.start` relies on it: it
    passes the pool payload as a dict so the snapshot is not encoded twice and stored as a JSON
    *string* (the comment at its `jsonb_set` says so). A bare `asyncpg.connect` refuses the
    argument outright, which is a fixture failure wearing the costume of a product bug.
    """
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


class _SlowPoolBuild:
    """A connection whose candidate-pool query takes its time, the way a real library does.

    The join/start race is a window, and on the fixture's eight titles the window is microseconds
    wide -- so a gathered join and start would interleave differently on every run and the suite
    would report the defect as a flake. 0.6 s is the plan's own reproduction figure and is modest
    against the tens of seconds a 696-title pool was measured at. Keyed on `t.is_owned` rather
    than on `user_score`, which `start`'s unscored-member check also reads (decision 216).
    """

    def __init__(self, conn, *, delay=0.6):
        self._conn = conn
        self._delay = delay
        self.built = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetch(self, query, *args, **kw):
        if "t.is_owned" in query:
            self.built = True
            await asyncio.sleep(self._delay)
        return await self._conn.fetch(query, *args, **kw)


async def test_a_join_and_a_start_do_not_both_win(db, world, pg_url):
    """§6.2 step 2's rule as the host's lobby states it: "Anyone who joins before you start is in."

    Which is a claim about two outcomes and not about three. The third -- seated, and outside the
    pool the round was built from -- is the one the code produced: `join` consulted a session row
    read before its own retry loop and its INSERT carried no state predicate, and `start` was not
    a transaction at all. Two real connections, because in sequence either order is correct, and
    that is why the suite was green.

    The assertion is the invariant rather than the winner: whichever of the two stands, every
    seated member is in the frozen snapshot. A seat outside it is a seat that can never be asked
    anything, which is finding 6's dead end arriving by finding 5's route.
    """
    room = await open_room(db, world)
    session_id = room["session_id"]
    starter = await second_connection(pg_url)
    joiner = await second_connection(pg_url)
    slow = _SlowPoolBuild(starter)

    async def claim_it():
        return await play.start(slow, session_id)

    async def sit_down():
        # Inside the build window, well after the first statement of either version of `start`.
        await asyncio.sleep(0.15)
        return await rooms.join(joiner, session_id=session_id, user_id=world["jenny"])

    try:
        started, joined = await asyncio.gather(claim_it(), sit_down(), return_exceptions=True)
    finally:
        await starter.close()
        await joiner.close()

    assert slow.built, "the pool query never ran, so there was no window to race"
    assert not isinstance(started, BaseException), f"the host's start failed: {started!r}"

    seated = [
        r["id"] for r in await db.fetch(
            "SELECT id FROM session_participant WHERE session_id = $1 AND user_id = $2",
            session_id, world["jenny"],
        )
    ]
    if isinstance(joined, BaseException):
        assert isinstance(joined, rooms.RoomError) and joined.reason == "started", repr(joined)
        assert seated == [], "a refused join may not leave a seat behind"
    else:
        assert seated == [joined["participant_id"]], "one member, one seat"

    snapshot = await play.snapshot_of(db, session_id)
    carried = {pid for seat_scores in snapshot.scores.values() for pid in seat_scores}
    members = {
        r["id"] for r in await db.fetch(
            "SELECT id FROM session_participant WHERE session_id = $1 AND role <> 'guest'",
            session_id,
        )
    }
    assert members and members <= carried, (
        "a seated member is outside the pool the round was built from"
    )


async def test_a_refused_start_leaves_the_room_open_and_joinable(db, world, pg_url):
    """The claim's rollback obligation, which is the hazard the claim itself introduces.

    `start` still refuses a pool of fewer than two candidates (decision 215 leaves admission
    alone), and that refusal now happens after the room would have been claimed. A room left in
    `voting` with no snapshot is a new instance of the defect this change closes -- worse, because
    an empty snapshot is exactly the seat that ends itself below, so the evening would end before
    it began. One transaction is what makes the refusal honest: the claim rolls back with it, and
    the join that was waiting behind it proceeds.
    """
    room = await open_room(db, world, budget_min=60)
    session_id = room["session_id"]
    starter = await second_connection(pg_url)
    joiner = await second_connection(pg_url)
    slow = _SlowPoolBuild(starter)

    async def claim_it():
        return await play.start(slow, session_id)

    async def sit_down():
        await asyncio.sleep(0.15)
        return await rooms.join(joiner, session_id=session_id, user_id=world["jenny"])

    try:
        refused, joined = await asyncio.gather(claim_it(), sit_down(), return_exceptions=True)
    finally:
        await starter.close()
        await joiner.close()

    assert isinstance(refused, play.RoundError) and refused.reason == "empty_pool"
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_OPEN, "a refusal that leaves the room claimed takes the evening with it"
    assert not isinstance(joined, BaseException), f"the waiting join was refused: {joined!r}"
    assert await db.fetchval(
        "SELECT context -> 'pool' FROM session WHERE id = $1", session_id
    ) is None, "no snapshot was written, so none may be readable"

    # And the room is still a room: a wider budget starts it, with both seats in the pool.
    await db.execute("UPDATE session SET runtime_budget_min = 200 WHERE id = $1", session_id)
    await play.start(db, session_id)
    snapshot = await play.snapshot_of(db, session_id)
    carried = {pid for seat_scores in snapshot.scores.values() for pid in seat_scores}
    assert len(carried) == 2, "the member who joined while the refusal rolled back is in the pool"


async def test_a_seat_that_can_never_be_asked_ends_itself(db, world):
    """Finding 6's dead end, closed where it cannot be reached any other way.

    A member seat the frozen snapshot holds no scores for: `pool_scores_for` is empty, so the
    prior is empty, `boundary` is None, nothing straddles and `stop_reason` is `converged` before
    a single answer -- with `pair` None, which leaves `_end` unreachable from every write path
    there is. The seat then holds the room open for ever: `everyone_finished` is false and no
    route can close it.

    `ended_by` is stamped with the replay's own reason rather than a fourth value: 0013's
    `session_ended_states` CHECK admits `converged | cap | escape` and is sha256-checksummed, and
    `converged` is honest about the mechanism -- the boundary is empty -- if generous about the
    word (decision 215).
    """
    room = await running_room(db, world)
    session_id = room["session_id"]
    late = await make_user(db, "mia")
    orphan = await db.fetchval(
        "INSERT INTO session_participant (session_id, user_id, role, seat) "
        "VALUES ($1, $2, 'member', 9) RETURNING id",
        session_id, late,
    )
    snapshot = await play.snapshot_of(db, session_id)
    assert snapshot.pool_scores_for(orphan) == {}, "the fixture is the state finding 5 leaves"

    state = await play.state_for(db, orphan, z=Z)

    assert state["pair"] is None and state["stop_reason"] == rnd.CONVERGED
    assert state["ended_by"] == rnd.CONVERGED, "the payload reports the seat as ended"
    row = await db.fetchrow(
        "SELECT ended_by, converged_at FROM session_participant WHERE id = $1", orphan
    )
    assert row["ended_by"] == rnd.CONVERGED, "and the row says so, so the room can close"
    assert row["converged_at"] is not None, "0013 ties the timestamp to the reason"

    # Reading the round again is not a second ending, and the other seats still have to finish.
    await play.state_for(db, orphan, z=Z)
    assert await play.settle(db, session_id, z=Z) is False
    for seat in room["seats"]:
        await run_to_the_end(db, seat["id"])
    assert await play.settle(db, session_id, z=Z) is True


@pytest.mark.parametrize(
    "budget_min,include_rewatches,candidates", [(70, False, 3), (60, True, 2)]
)
async def test_a_pool_too_small_for_a_round_reaches_the_ballot(
    db, world, budget_min, include_rewatches, candidates
):
    """Decision 215: a pool of two or three goes to the ballot rather than waiting for ever.

    `boundary` returns None at or below the shortlist size (54d fixes the slate at three finalists
    plus a wildcard, which is four titles), so nothing straddles and the round is over before it
    starts. Admission is deliberately unchanged -- refusing below four denies a household with
    three owned shows any evening at all -- so the seat ends itself, the room settles from a read,
    and three titles get voted on.

    Reproduced end to end before the fix: start 200, round `{stop_reason: converged, pair: None}`,
    lobby `voting`, escape 409 `too_early`, undo 409, ballot 200 with an empty slate, for ever.
    """
    room = await open_room(db, world, budget_min=budget_min, include_rewatches=include_rewatches)
    session_id = room["session_id"]
    await rooms.join(db, session_id=session_id, user_id=world["jenny"])
    await play.start(db, session_id)
    snapshot = await play.snapshot_of(db, session_id)
    assert len(snapshot.title_ids) == candidates, "the fixture has to be the pool size it claims"

    seats = [
        r["id"] for r in await db.fetch(
            "SELECT id FROM session_participant WHERE session_id = $1 ORDER BY seat", session_id
        )
    ]
    for seat in seats:
        state = await play.state_for(db, seat, z=Z)
        assert state["pair"] is None, "there is no shortlist boundary to resolve"
        assert state["ended_by"] == rnd.CONVERGED

    assert await play.settle(db, session_id, z=Z) is True
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_BALLOT
    slate = await ballot.slate_of(db, session_id)
    assert len(slate) == candidates, "every candidate is on the ballot when there are this few"


async def test_a_refusal_names_the_member_the_pool_cannot_rank(db, world):
    """Finding 34 / decision 216: §6.8's register is what a refusal owes.

    `pool.build` keeps only titles every seated member has scored, so a member whose fold-in has
    not run for this bundle -- a freshly created account before the 60 s tick, a worker that is
    down, the minutes after a re-import -- empties the pool. The answer was "nothing in the
    library fits tonight -- widen the budget or include rewatches": the host widens, retries, gets
    the same sentence, and nothing anywhere names the member.

    The budget message is kept for the case it is true of. Two refusals, two causes, and the
    household can act on both.
    """
    room = await open_room(db, world, budget_min=200, include_rewatches=True)
    unscored = await make_user(db, "mia")
    await rooms.join(db, session_id=room["session_id"], user_id=unscored)

    with pytest.raises(play.RoundError) as refused:
        await play.start(db, room["session_id"])

    assert refused.value.reason == "unscored_member"
    assert "mia" in str(refused.value), "the one fact the household can act on"
    assert "widen the budget" not in str(refused.value)
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", room["session_id"]
    ) == rooms.STATE_OPEN, "and the room is still joinable, so the fold-in can catch up"

    # Any score for this bundle is all the check asks for: it distinguishes a member the Ledger
    # has not reached yet from a library with nothing for tonight, and only the first can be
    # named (decision 216). Once the fold-in has run, the room starts.
    for title_id in (1, 2):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "VALUES ($1, $2, 'movie', $3, 0.5, 0.0)",
            unscored, title_id, BUNDLE,
        )
    await play.start(db, room["session_id"])

    tight = await open_room(db, world, budget_min=60)
    with pytest.raises(play.RoundError) as empty:
        await play.start(db, tight["session_id"])
    assert empty.value.reason == "empty_pool"
    assert "widen the budget" in str(empty.value), "the budget message, for the budget case"


def test_the_named_refusal_reaches_the_household_as_a_refusal_and_not_a_500():
    """`_room_error` is the one mapping, so a reason it has never heard of takes a silent default.

    409 rather than 422: the host's request is well formed and the world is not ready, which is
    what every other "the world moved" refusal on this surface answers. Named explicitly beside
    the others so the status is a decision recorded in the mapping rather than whatever the
    default happens to be on the day somebody changes it. [decision 216]
    """
    from spielplan.api import tonight as tonight_api

    mapped = tonight_api._room_error(play.RoundError("unscored_member", "mia has no scores yet"))
    assert mapped.status_code == 409
    assert mapped.detail["reason"] == "unscored_member"
    assert "mia" in mapped.detail["message"]


class _EscapesMidEnding:
    """A connection on which the seat ends by another route inside `_end`'s own statement.

    The window two writers open by themselves, and it exists only because `state_for` became one
    of them: a device reads the round of a seat that has just converged while the person taps
    "just pick for us" on the phone. Deterministic rather than waited for, which is the shape
    every racing proxy above this one takes, and for the same reason: the failure happens BETWEEN
    two statements and nothing outside the function can stand there otherwise.
    """

    def __init__(self, conn, *, participant_id):
        self._conn = conn
        self._participant_id = participant_id
        self.armed = True

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query, *args, **kw):
        # `fetchval` and not `execute`: `_end` reads its own `RETURNING id` now, because the write
        # paths have to know whether their ending landed (M4.12 finding 3 / `when_answered`). The
        # statement and the window are the same ones; only the method that issues them moved. And
        # `armed` is what caught that rather than a test quietly passing over a window that no
        # longer opens.
        if self.armed and "SET ended_by" in query:
            self.armed = False
            await self._conn.execute(
                "UPDATE session_participant SET ended_by = 'escape' WHERE id = $1",
                self._participant_id,
            )
        return await self._conn.fetchval(query, *args, **kw)


async def test_the_first_ending_recorded_is_the_one_that_stands(db, world):
    """§14 risk 6 wants "the rate at which each of the three fires", and last-write-wins loses it.

    A seat with six answers can converge AND be escaped: `escape` only asks that the round is
    still open and that pair six has been answered. Until `state_for` ended seats it had one
    writer and the question could not arise; now a read and a tap can both land, and an `ended_by`
    the later of them overwrites is a measurement that reports the household's escape as a
    convergence. The guard is in `_end`'s statement, so the loser writes nothing at all —
    including `converged_at`, which 0013's CHECK ties to the reason.
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    ended = await run_to_the_end(db, seat)
    assert ended is not None, "the fixture needs a seat whose round is over"
    await db.execute(
        "UPDATE session_participant SET ended_by = NULL, converged_at = NULL WHERE id = $1", seat
    )

    racing = _EscapesMidEnding(db, participant_id=seat)
    state = await play.state_for(racing, seat, z=Z)

    assert racing.armed is False, "the window never opened, so nothing was raced"
    row = await db.fetchrow(
        "SELECT ended_by, converged_at FROM session_participant WHERE id = $1", seat
    )
    assert row["ended_by"] == rnd.ESCAPE, "the writer that arrived first is the one that counts"
    assert row["converged_at"] is None, "and nothing of the losing write landed either"
    # The payload reports this read's own reason, which is the deliberate half: to the device
    # both readings say the same thing, and the row is what §14 risk 6 counts.
    assert state["ended_by"] == state["stop_reason"]


# --- M4.12 findings 10, 11 and 12: every write guard was a check-then-act ------------------------
#
# One shape, three surfaces. `record_answer` read the participant with a plain SELECT and compared
# the card's seq against `answered_count + 1` OUTSIDE the transaction that inserts, so two taps on
# one card both passed and the loser collided with 0014's partial unique index, which `app.py`'s
# `_conflict` answers 409 `conflict: session_answer_seq` -- a status the client re-reads on, but
# carrying the name of a database object where the round's contract defines a reason, over a card
# that no longer existed, and leaving a reused seq behind it. `retract` recomputed
# `answered_count` from the rows surviving its own tombstone, so an answer committing in between
# left the counter BEHIND the highest live seq, the next card was minted at a seq that already
# existed, and every later tap collided with it for ever -- the exact dead end 0014's own comment
# says it removed. `ballot.submit` was DELETE-then-INSERT with no lock, so the second of two taps
# on the one moment 54e makes social was shown an index name, and its whole ballot -- DELETE and
# every INSERT are one transaction -- was rolled back with it.
#
# THE STATUS IN THIS PARAGRAPH WAS 500 IN NINE PLACES, and the seam has answered 409 since M4.7's
# `_conflict` (`test_http_seam.py`: "the seam M4.10 and M4.12 plug into, asserted here so they can
# point at it"). The three repairs and every assertion below are unchanged -- they were never
# about the status code -- but the record of why has to be what the app does.
# [M4.12 cycle 1, M412-CONC-02]
#
# The repair is M4.10's idiom at three more seams: the checks move inside the transaction behind a
# `FOR UPDATE OF p` read of the seat, the seq is minted from the ROWS (tombstones included, so a
# seq is never reused) while `answered_count` stays displayed progress, and the ballot takes
# `pg_advisory_xact_lock` on its participant. 0014's index stays as the backstop; nothing here
# catches a unique violation, because a repair that caught one would be the check-then-act again
# with the race moved into the except branch.
#
# THESE TESTS NEED TWO REAL CONNECTIONS AND A WIDENED WINDOW. In sequence every interleaving below
# is correct, which is why the suite was green on all three defects. `_SlowSnapshot` delays the
# snapshot read -- the one await both writers make between their guard and their write -- so the
# overlap is a fact of the test rather than a property of the scheduler. Under the repair the loser
# blocks on the seat's row lock instead, so the same fixture is deterministic in both directions.


import time  # noqa: E402


class _SlowSnapshot:
    """A connection whose frozen-pool read takes its time, the way a real 696-title pool does, and
    which times how long its locked read of the seat had to wait.

    The delay is keyed on the one statement `snapshot_of` issues, and before the repair that read
    sat between the seq guard and the INSERT -- precisely the window two taps race. A megabyte of
    jsonb decoded per read makes it real in production (finding 3) rather than a test artefact.

    `waited` is how the tests below prove the race happened AFTER the repair, where the losing tap
    is refused before it ever reads a snapshot. It is the duration of the `FOR UPDATE OF p` read:
    the winner's returns at once, and the loser's cannot return until the winner commits, so one of
    the two is held for the whole delay. Measuring the mechanism rather than the outcome is the
    point -- an assertion that only read the outcome would pass just as well if the two calls had
    run one after the other, which is how these three defects stayed green for a milestone.
    """

    def __init__(self, conn, *, delay=0.3):
        self._conn = conn
        self.delay = delay
        self.read = False
        self.waited = 0.0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query, *args, **kw):
        if "SELECT context FROM session" in query:
            self.read = True
            await asyncio.sleep(self.delay)
        return await self._conn.fetchval(query, *args, **kw)

    async def fetchrow(self, query, *args, **kw):
        if "FOR UPDATE OF p" not in query:
            return await self._conn.fetchrow(query, *args, **kw)
        began = time.perf_counter()
        try:
            return await self._conn.fetchrow(query, *args, **kw)
        finally:
            self.waited = time.perf_counter() - began


def _one_of_them_waited(*conns):
    """Exactly one of these connections had its seat read held by the other's transaction.

    Which one is the scheduler's to decide; that one of them waited is the repair, and that only
    one did is the proof they were in flight together rather than in sequence.
    """
    held = [c for c in conns if c.waited >= c.delay / 2]
    assert len(held) == 1, (
        "the two writers did not contend for the seat: waited "
        f"{[round(c.waited, 3) for c in conns]} against a {conns[0].delay}s hold"
    )


async def _seat_rows(db, participant_id):
    """(answered_count, live seqs, every seq including tombstones) -- the three numbers these two
    defects put out of step with each other."""
    counted = await db.fetchval(
        "SELECT answered_count FROM session_participant WHERE id = $1", participant_id
    )
    live = [
        r["seq"] for r in await db.fetch(
            "SELECT seq FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL "
            "ORDER BY seq",
            participant_id,
        )
    ]
    every = [
        r["seq"] for r in await db.fetch(
            "SELECT seq FROM session_answer WHERE participant_id = $1 ORDER BY seq", participant_id
        )
    ]
    return counted, live, every


def _sound(counted, live, every):
    """The invariant the seq minting exists to keep: the counter displays the live rows, and the
    highest seq ever issued is never behind it. A counter ahead of the rows is the wedge."""
    assert len(live) == counted, f"answered_count {counted} against live seqs {live}"
    assert max(every, default=0) >= counted, f"highest seq {every} behind answered_count {counted}"
    assert len(set(every)) == len(every), f"a seq was reused: {every}"


async def test_two_answers_on_one_card_are_one_200_and_one_409(db, world, pg_url):
    """§13's single-use seal, under the double tap that actually happens on a phone.

    Both racers passed a seq guard read outside the transaction that inserts, so the loser hit
    0014's partial unique index, and `app.py`'s `_conflict` answered that 409
    `conflict: session_answer_seq`. The client re-reads the round on a 409 and only on a 409
    (`tonight.svelte.js`'s `answer`), so it did re-read -- and what stood behind that re-read was a
    reused seq and a constraint name where §6.8's register wants a sentence. The repair makes the
    loser wait, re-read and refuse on `stale_pair`: the same status, with a reason the round
    defines. [M4.12 finding 10; M4.12 cycle 1, M412-CONC-02]
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    state = await play.state_for(db, seat, z=Z)
    pair, seq = state["_pair"], state["answered"] + 1

    first_conn = _SlowSnapshot(await second_connection(pg_url))
    second_conn = _SlowSnapshot(await second_connection(pg_url))

    async def tap(conn, answer):
        return await play.record_answer(
            conn, participant_id=seat, pair=pair, answer=answer, seq=seq, latency_ms=900, z=Z,
        )

    try:
        outcomes = await asyncio.gather(
            tap(first_conn, rnd.A), tap(second_conn, rnd.B), return_exceptions=True
        )
    finally:
        await first_conn.close()
        await second_conn.close()

    refused = [o for o in outcomes if isinstance(o, BaseException)]
    assert all(isinstance(o, play.RoundError) for o in refused), (
        f"a write guard answered with something other than a refusal: {refused!r}"
    )
    assert len(refused) == 1, f"exactly one tap may stand: {outcomes!r}"
    assert refused[0].reason == "stale_pair", "the reason the client re-reads on"

    counted, live, every = await _seat_rows(db, seat)
    _sound(counted, live, every)
    assert counted == 1, "one card, one answer"
    assert len(every) == 1, "and one row, tombstones included"
    _one_of_them_waited(first_conn, second_conn)


async def test_an_undo_gathered_with_an_answer_leaves_the_seat_playing(db, world, pg_url):
    """§6's preamble puts undo on every surface; this is the interleaving that made it a trap.

    `retract` wrote `answered_count = len(live answers)` over rows read around its own tombstone,
    so an answer committing inside that window left the counter behind the highest live seq. The
    next card was then minted at a seq that already existed and every subsequent tap collided with
    it -- 409 `conflict: session_answer_seq` for ever, on a seat whose round nothing but an answer
    can end and which no re-read can advance. [M4.12 finding 11; M4.12 cycle 1, M412-CONC-02]
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    state = await play.state_for(db, seat, z=Z)
    pair, seq = state["_pair"], state["answered"] + 1

    answering = _SlowSnapshot(await second_connection(pg_url))
    undoing = _SlowSnapshot(await second_connection(pg_url))
    try:
        outcomes = await asyncio.gather(
            play.record_answer(
                answering, participant_id=seat, pair=pair, answer=rnd.A, seq=seq,
                latency_ms=900, z=Z,
            ),
            play.retract(undoing, seat, z=Z),
            return_exceptions=True,
        )
    finally:
        await answering.close()
        await undoing.close()

    for outcome in outcomes:
        # Either ordering is a correct evening: the undo lands and the answer's card is stale, or
        # the answer lands and the undo takes it back. What neither may be is a database error.
        assert not isinstance(outcome, asyncpg.PostgresError), f"the seat wedged: {outcome!r}"
        if isinstance(outcome, BaseException):
            assert isinstance(outcome, play.RoundError), repr(outcome)
            assert outcome.reason == "stale_pair", outcome.reason
    _sound(*await _seat_rows(db, seat))
    _one_of_them_waited(answering, undoing)

    # The wedge was never the race itself -- it was the state it left behind, which turned the
    # NEXT tap into a collision and kept doing so. So the assertion is that the seat keeps playing.
    ended = await db.fetchval("SELECT ended_by FROM session_participant WHERE id = $1", seat)
    assert ended is None, "neither a gathered answer nor a gathered undo ends a round"
    for _ in range(3):
        if await answer_once(db, seat) is None:
            break
        _sound(*await _seat_rows(db, seat))


async def test_a_replacement_answer_takes_a_fresh_seq_rather_than_the_tombstones(db, world):
    """§14 risk 6 logs every vote, so an undone answer stays as a row -- and the replacement
    therefore needs a seq of its own.

    `answered_count + 1` was both the displayed progress and the identity of the next row, which is
    what forced 0014 to make its index partial and what let an interleaved write put the two out of
    step. They are two numbers now: the counter counts the live rows, the seq is minted from every
    row there has ever been. [M4.12 finding 11]
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    await play.retract(db, seat, z=Z)

    written = await answer_once(db, seat)
    counted, live, every = await _seat_rows(db, seat)
    _sound(counted, live, every)
    assert every == [1, 2, 3], "the tombstone keeps its seq and the replacement takes the next"
    assert live == [1, 3]
    assert counted == 2, "answered_count is what the waiting screen displays, nothing more"
    assert written["seq"] == 3, "and the caller is told the seq that was actually written"


async def test_the_counter_cannot_be_moved_past_the_rows_it_counts(db, world):
    """The invariant in the statement rather than in the callers, for `_end`'s own reason: a check
    in a caller is a check another caller can be written without.

    A counter that disagrees with the rows IS the wedge: while the row's seq was `answered_count +
    1`, one left behind by an interleaved retract minted the next card at a seq that already
    existed. Minting from the rows removes that collision; this keeps the counter honest, because
    it is still the number the lobby and the waiting screen display. The write that would break it
    is refused and rolled back rather than committed and discovered three taps later on a phone.
    [M4.12 finding 11]
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)

    with pytest.raises(AssertionError):
        await play._set_answered(db, seat, count=5, tilt={})
    assert await db.fetchval(
        "SELECT answered_count FROM session_participant WHERE id = $1", seat
    ) == 1, "the refused write left the counter alone"


async def test_two_ballot_submissions_from_one_seat_are_both_recorded(db, world, pg_url):
    """54e's "re-submitting replaces rather than adds", including when the two submits overlap.

    DELETE-then-INSERT with no lock: the second tab's DELETE could not see the first's uncommitted
    rows, so it deleted nothing and its INSERTs collided with `session_ballot_one_per_title`.
    `app.py`'s `_conflict` answers that 409 `conflict: session_ballot_one_per_title`, so on the one
    moment 54e makes social the phone was shown the name of a database index -- and the whole of
    that submission rolled back with it, since the DELETE and every INSERT are one transaction.
    [M4.12 finding 12; M4.12 cycle 1, M412-CONC-02]
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    slate = await play.finish(db, session_id, z=Z)
    seat = room["seats"][0]["id"]
    one, two = slate.ballot_titles[:1], slate.ballot_titles[1:2]
    assert one and two, "the fixture needs two slate titles to tell the submissions apart"

    first_conn = await second_connection(pg_url)
    second_conn = await second_connection(pg_url)
    try:
        outcomes = await asyncio.gather(
            ballot.submit(first_conn, participant_id=seat, approved=one),
            ballot.submit(second_conn, participant_id=seat, approved=two),
            return_exceptions=True,
        )
    finally:
        await first_conn.close()
        await second_conn.close()

    assert not [o for o in outcomes if isinstance(o, BaseException)], (
        f"a re-submit is not a refusal: {outcomes!r}"
    )
    rows = await db.fetch(
        "SELECT title_id, approved FROM session_ballot WHERE participant_id = $1 ORDER BY title_id",
        seat,
    )
    assert [r["title_id"] for r in rows] == sorted(slate.ballot_titles), (
        "every slate title gets exactly one row -- that is what 'has this person submitted' reads"
    )
    approved = sorted(r["title_id"] for r in rows if r["approved"])
    # Which of the two committed last is the race's to decide; that it is one of them rather than a
    # mixture of the two is the repair: the loser waits, then replaces the whole ballot.
    assert approved in (sorted(one), sorted(two)), f"a half-written ballot: {approved}"


class _SlowTally:
    """A connection whose approval tally takes its time, the way a real reveal does.

    The window between `resolve` counting the approvals and storing the outcome is microseconds
    wide on the fixture, so a gathered re-submit would interleave differently on every run and the
    suite would report the defect as a flake. The delay is taken AFTER the query rather than
    before it, because what is being modelled is a tally that has already been computed: the whole
    hazard is that a ballot row changes between the count and the row it is stored as. 0.6 s is
    `_SlowPoolBuild`'s own figure.
    """

    def __init__(self, conn, *, delay=0.6):
        self._conn = conn
        self._delay = delay
        self.counted = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetch(self, query, *args, **kw):
        rows = await self._conn.fetch(query, *args, **kw)
        if "FILTER (WHERE b.approved)" in query:
            self.counted = True
            await asyncio.sleep(self._delay)
        return rows


async def test_a_re_submitted_ballot_cannot_land_after_the_outcome_is_stored(db, world, pg_url):
    """§13's number and §14 risk 6's log of the votes it came from, made to agree.

    `submit`'s advisory lock is keyed per participant, so it serialises one person's two submits
    and meets the other writer on the same beat -- `resolve` -- on nothing at all. The state
    question at the top of `submit` was a check-then-act on an autocommit connection, and the
    count, the winner and the share were computed outside any transaction and written after: so a
    changed-mind re-submit that had already passed the guard could commit between the tally and
    the INSERT, and `session_outcome` was left naming a winner the surviving `session_ballot` rows
    say nobody approved. Reproduced with the window widened: outcome `{chosen: 1, share: 0.5}`
    over rows in which title 1 had zero approvals and title 2 had two.

    Two phones on 54e's reveal is the ordinary case: A taps Submit, changes their mind and taps
    again, and B's submit completes the room in between. The assertion is the agreement rather
    than the winner -- which of the two orders happens is the race's to decide, and both are
    correct evenings -- and the refusal is asserted by its reason, because a late submit answering
    with a database error instead of §6.8's register is the other way to fail this.
    [M4.12 review cycle 2: M412-CONC-04]
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    slate = await play.finish(db, session_id, z=Z)
    first, second = room["seats"][0]["id"], room["seats"][1]["id"]
    one, two = slate.ballot_titles[:1], slate.ballot_titles[1:2]
    assert one and two, "the fixture needs two slate titles to tell the ballots apart"

    await ballot.submit(db, participant_id=first, approved=one)
    revealing = _SlowTally(await second_connection(pg_url))
    changing = await second_connection(pg_url)

    async def vote_and_reveal():
        await ballot.submit(revealing, participant_id=second, approved=two)
        assert await ballot.everyone_submitted(revealing, session_id)
        return await ballot.resolve(revealing, session_id)

    async def change_my_mind():
        # Inside the tally window, well after either version of `resolve` has counted.
        await asyncio.sleep(0.15)
        return await ballot.submit(changing, participant_id=first, approved=two)

    try:
        outcome, changed = await asyncio.gather(
            vote_and_reveal(), change_my_mind(), return_exceptions=True
        )
    finally:
        await revealing._conn.close()
        await changing.close()

    assert revealing.counted, "the tally never ran, so there was no window to race"
    assert not isinstance(outcome, BaseException), f"the reveal failed: {outcome!r}"
    if isinstance(changed, BaseException):
        assert isinstance(changed, ballot.BallotError) and changed.reason == "not_ballot", (
            f"a late re-submit is a refusal with a reason, never a database error: {changed!r}"
        )

    counted = {
        r["title_id"]: int(r["approvals"]) for r in await db.fetch(
            "SELECT title_id, count(*) FILTER (WHERE approved) AS approvals FROM session_ballot "
            "WHERE session_id = $1 GROUP BY title_id",
            session_id,
        )
    }
    stored = await db.fetchrow(
        "SELECT chosen_title_id, approval_share, participants FROM session_outcome "
        "WHERE session_id = $1",
        session_id,
    )
    assert stored is not None, "the evening resolved, so the one outcome row is there"
    won = counted[stored["chosen_title_id"]]
    assert won == max(counted.values()), (
        f"the stored winner {stored['chosen_title_id']} has {won} approvals in the ballot that "
        f"survived: {counted}"
    )
    assert float(stored["approval_share"]) == pytest.approx(won / stored["participants"]), (
        f"the stored share is not the share of the rows it is derived from: {dict(stored)} over "
        f"{counted}"
    )


# --- M4.12 findings 2 and 3: the search ran on the loop, and every tap paid for it twice ---------
#
# `grep -rn 'to_thread\|run_in_executor'` over `backend/spielplan/tonight` and `api/tonight.py`
# returned nothing, and the pair search is the one expensive thing Tonight does: on the shipped
# bundle 344 of 696 candidates straddle at the round's own boundary, and even vectorised the search
# over their pairs is numpy work with a pure-Python `axis_positions` pass in front of it. Run on the
# request's own loop it stops everything else this single-process app is doing -- the other phone's
# GET of its own round, the hub's next frame, `/api/health` -- and `record_answer` ran it INSIDE the
# transaction that wrote the answer, so the seat's `FOR UPDATE OF p` lock was held for the whole
# computation and the undo button queued behind a search it does not need. [findings 2, 3]
#
# The route then paid for the round a second time: `record_answer` read the frozen pool out of jsonb
# and replayed, and the handler called `state_for`, which did both again to produce a card the write
# had already computed. A 300-title pool is on the order of a megabyte of JSON decoded per read, and
# undo and escape had the same shape -- the escape replaying an entire round to be told that a seat
# which has just ended has no next pair.
#
# THE INSTRUMENT IS A PATCHED REPLAY, NOT THE REAL SELECTOR'S DURATION. A fixture pool replays in
# microseconds, so a timing assertion over it would measure the scheduler, and asserting against the
# real 696-title cost would make the suite depend on a bundle it does not have (decision 183 leaves
# the corpus to the owner). So the stand-in blocks its own thread with `time.sleep`, which is
# exactly the difference under test, and the assertions are mechanisms: a coroutine submitted from
# inside the search either runs or cannot, the gap between two wakeups of an ordinary 10 ms timer,
# and the number of snapshot decodes and replays per tap.


import contextlib  # noqa: E402
import dataclasses  # noqa: E402


async def _watch_the_loop(gaps):
    """Ask to be woken every 10 ms and record how long each wait actually took.

    The loop's own answer to "did a handler hold you". A coroutine that asked for 10 ms and was not
    resumed for 600 was not competing for CPU -- it was not scheduled at all, because one frame
    owned the thread. §2's box has 4 vCPU and §6's preamble budgets 1.5 s per battle, so the number
    that matters is the pause a tap imposes on everything else rather than the tap's own duration.
    """
    last = time.perf_counter()
    while True:
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


@contextlib.asynccontextmanager
async def _loop_gaps():
    gaps: list[float] = []
    watcher = asyncio.create_task(_watch_the_loop(gaps))
    await asyncio.sleep(0.03)  # one reading before the work starts, so `max` is never over nothing
    try:
        yield gaps
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def _count_the_work(monkeypatch, *, replay_delay=0.0):
    """Count the frozen-pool decodes and the round replays, and optionally make each replay cost
    what a real pool costs.

    Patched where `play` looks both of them up -- a module global in both cases -- so the tally is of
    what the code under test did rather than of what a wrapper was asked to do. Counting rather than
    timing is the point of finding 3: two replays that are each fast enough are still twice the work
    the round needs, and on the pool this is shipped against each one is seconds.
    """
    tally = {"snapshots": 0, "replays": 0}
    real_snapshot, real_replay = play.snapshot_of, rnd.replay

    async def counted_snapshot(conn, session_id):
        tally["snapshots"] += 1
        return await real_snapshot(conn, session_id)

    def counted_replay(*args, **kw):
        tally["replays"] += 1
        if replay_delay:
            time.sleep(replay_delay)
        return real_replay(*args, **kw)

    monkeypatch.setattr(play, "snapshot_of", counted_snapshot)
    monkeypatch.setattr(rnd, "replay", counted_replay)
    return tally


def _reads_the_rooms_mid_search(monkeypatch, client, *, hold=0.6):
    """A replay that costs what a real pool's does and, while it is running, asks the app for
    something else entirely.

    THE DEADLOCK IS THE ASSERTION. `run_coroutine_threadsafe` hands the loop a coroutine and waits
    for it from the calling thread. Off the loop -- the repair -- the loop is free, the request is
    served in milliseconds, and the household's other phone got an answer WHILE the search ran. On
    the loop -- the defect -- the thread that waits is the thread that would have to run it, so it
    cannot be served at all and the wait times out. Nothing about the scheduler decides which of
    those happens: the two are different in kind, not in degree.
    """
    loop = asyncio.get_running_loop()
    real = rnd.replay
    probe = {"status": None, "elapsed": None, "error": None, "replays": 0}

    def replay(*args, **kw):
        probe["replays"] += 1
        if probe["replays"] == 1:
            began = time.perf_counter()
            pending = asyncio.run_coroutine_threadsafe(client.get("/api/tonight/rooms"), loop)
            try:
                probe["status"] = pending.result(timeout=hold).status_code
            except Exception as exc:
                probe["error"] = type(exc).__name__
                pending.cancel()
            probe["elapsed"] = time.perf_counter() - began
        time.sleep(hold)
        return real(*args, **kw)

    monkeypatch.setattr(rnd, "replay", replay)
    return probe


@pytest.fixture
async def household(app, db):
    """Two real accounts with real cookies, a twelve-film library and a started room, over HTTP.

    The claim is about a request in flight, so the requests have to be real ones: two cookie jars
    on one ASGI app, which is what the `app` fixture's factory is for. `world` cannot serve this --
    it inserts its admin straight into `app_user`, and `POST /api/setup/admin` is first-boot only,
    so no cookie for that account is obtainable. The account helpers are `test_tonight_routes.py`'s
    in miniature, written out rather than imported: a cross-module import between two test files is
    a dependency neither file's header declares.

    Twelve films and not six, because one assertion below needs a round that is still running at
    pair six. At the round's own boundary (`BOUNDARY_Z`, decision 214) a six-title pool resolves its
    shortlist in one or two pairs, and an ended seat cannot reach 54c's escape at all.
    """
    host = app()
    created = await host.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    invited = await host.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    assert invited.status_code == 201, invited.text
    otp = invited.json()["one_time_password"]
    member = app()
    assert (
        await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    ).is_success
    assert (
        await member.post(
            "/api/auth/password",
            json={"current_password": otp, "new_password": "member-password"},
        )
    ).is_success

    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}', 'active')", BUNDLE
    )
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT g, 'movie', 'Film ' || g, 2010, 95 + g, true FROM generate_series(1, 12) AS g
        """
    )
    for user_id in [r["id"] for r in await db.fetch("SELECT id FROM app_user ORDER BY id")]:
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "SELECT $1, g, 'movie', $2, 0.9 - 0.05 * g, 0.0 FROM generate_series(1, 12) AS g",
            user_id, BUNDLE,
        )

    opened = await host.post(
        "/api/tonight/sessions",
        json={"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True},
    )
    assert opened.status_code == 201, opened.text
    session_id = opened.json()["session_id"]
    joined = await member.post("/api/tonight/sessions/join", json={"session_id": session_id})
    assert joined.status_code == 200, joined.text
    started = await host.post(f"/api/tonight/sessions/{session_id}/start")
    assert started.status_code == 200, started.text
    seats = {
        r["name"]: r["id"]
        for r in await db.fetch(
            "SELECT u.name, p.id FROM session_participant p JOIN app_user u ON u.id = p.user_id "
            "WHERE p.session_id = $1",
            session_id,
        )
    }
    return {"host": host, "member": member, "session_id": session_id, "seats": seats}


async def _round_card(client, participant_id):
    res = await client.get(f"/api/tonight/seats/{participant_id}/round")
    assert res.status_code == 200, res.text
    return res.json()


async def _tap(client, participant_id, card, answer=rnd.A):
    """One answer, posted the way `tonight.svelte.js` posts it: the sealed card and nothing else."""
    return await client.post(
        f"/api/tonight/seats/{participant_id}/answer",
        json={"card_token": card["card_token"], "answer": answer, "latency_ms": 900},
    )


def _pair_ids(card):
    pair = card["pair"]
    return None if pair is None else (pair["a"]["title_id"], pair["b"]["title_id"])


async def test_the_selector_does_not_block_the_household(household, monkeypatch):
    """§6's preamble budgets 1.5 s per battle on §2's 4 vCPU box -- which one tap used to own.

    The other phone's GET is what makes it a household failure rather than a slow request: the pair
    search is pure CPU on the one event loop this process has, so for its whole duration the second
    member's round does not load, the lobby's frames do not go out and `/api/health` does not
    answer. Measured at 36.2 s per replay on the real priors and 125.9 s for a guest seat before
    this milestone's step 1; the thread is what makes the repair true of the remainder rather than
    of a number that happens to be small today. [finding 2]
    """
    seat = household["seats"]["patrick"]
    card = await _round_card(household["host"], seat)
    assert card["pair"], "the fixture needs a pair to answer"
    probe = _reads_the_rooms_mid_search(monkeypatch, household["member"])

    async with _loop_gaps() as gaps:
        answered = await _tap(household["host"], seat, card)

    assert answered.status_code == 200, answered.text
    assert probe["error"] is None, (
        "an unrelated GET /api/tonight/rooms could not be served while the pair search ran "
        f"({probe['error']} after {probe['elapsed']:.3f}s) -- the loop was the selector's"
    )
    assert probe["status"] == 200
    assert probe["elapsed"] < 0.2, (
        f"the other phone's read waited {probe['elapsed']:.3f}s on this seat's answer"
    )
    assert max(gaps) < 0.2, f"a handler held the event loop for {max(gaps):.3f}s"
    assert probe["replays"] == 1, f"one tap, one search: {probe}"


async def test_one_tap_reads_the_frozen_pool_once_and_runs_the_round_once(household, monkeypatch):
    """Finding 3: every write route produced its next card by calling `state_for` after the write.

    Two decodes of the frozen pool and two runs of the round per tap, for a card `record_answer` had
    already computed -- and on the escape, a whole round replayed to be told that a seat which has
    just ended has nothing to be asked. Counted rather than timed: the work is wrong at any speed,
    and on the pool this ships against each replay is seconds.
    """
    seat = household["seats"]["patrick"]
    tally = _count_the_work(monkeypatch)

    card = await _round_card(household["host"], seat)
    assert (tally["snapshots"], tally["replays"]) == (1, 1), f"the reload is one of each: {tally}"

    tally.update(snapshots=0, replays=0)
    answered = await _tap(household["host"], seat, card)
    assert answered.status_code == 200, answered.text
    assert (tally["snapshots"], tally["replays"]) == (1, 1), f"one tap: {tally}"

    tally.update(snapshots=0, replays=0)
    undone = await household["host"].post(f"/api/tonight/seats/{seat}/undo")
    assert undone.status_code == 200, undone.text
    assert (tally["snapshots"], tally["replays"]) == (1, 1), f"one undo: {tally}"

    # Five answers, so 54c's control is open: `escape_available` is `answered >= 6 - 1`.
    for _ in range(rnd.ESCAPE_FROM_PAIR - 1):
        card = await _round_card(household["host"], seat)
        assert card["pair"], f"the pool ran out at {card['answered']}: {card['stop_reason']}"
        assert (await _tap(household["host"], seat, card)).status_code == 200

    state = await _round_card(household["host"], seat)
    assert state["escape_available"] and state["ended_by"] is None, state
    tally.update(snapshots=0, replays=0)
    escaped = await household["host"].post(f"/api/tonight/seats/{seat}/escape")
    assert escaped.status_code == 200, escaped.text
    assert escaped.json()["ended_by"] == rnd.ESCAPE
    assert (tally["snapshots"], tally["replays"]) == (0, 0), (
        f"an ended seat has no card to compute, so it reads nothing and replays nothing: {tally}"
    )


async def test_the_card_a_tap_hands_back_is_the_one_the_round_would_have_served(household):
    """The guard on having stopped calling `state_for` after every write.

    Passes against the old code by construction -- the card WAS `state_for`'s -- and that is the
    point: assembling it from the write's own replay is a second place the payload can be got
    wrong, and the way it goes wrong is a seal minted against the wrong seq. That reaches the
    household as a 409 on the next tap over a card it was just handed, on a client that re-reads on
    a 409 and only on a 409. So each answer here is posted with the seal the previous answer
    returned, and every field is compared against a fresh read of the same seat.

    Three taps, deliberately short of the hold-out slot: §13's every-tenth pair is drawn from the
    request's own generator, so a reload could legitimately name a different pair there. Every
    adaptive pair is a deterministic function of the posterior.
    """
    seat = household["seats"]["patrick"]
    card = await _round_card(household["host"], seat)
    for tap in range(3):
        answered = await _tap(household["host"], seat, card)
        assert answered.status_code == 200, f"tap {tap + 1}: {answered.text}"
        written = answered.json()
        afresh = await _round_card(household["host"], seat)
        assert written["answered"] == afresh["answered"]
        assert written["ended_by"] == afresh["ended_by"]
        assert written["stop_reason"] == afresh["stop_reason"]
        assert written["escape_available"] == afresh["escape_available"]
        assert _pair_ids(written) == _pair_ids(afresh), (
            f"tap {tap + 1} handed back a different pair than the round serves"
        )
        assert written["wrote"]["seq"] == tap + 1, "§14 risk 6 reads this against the rows"
        if written["pair"] is None:
            break
        card = written


async def test_an_undo_during_the_search_leaves_the_seat_playing(db, world, pg_url, monkeypatch):
    """The hazard this repair introduces, which is why it ships with a guard in the statement.

    `record_answer` replays after its transaction commits now, because a search held inside it holds
    the seat's row lock for the whole computation (finding 2) -- and a free loop is precisely what
    lets the next request in. So the household's undo can commit between the answer and the ending
    that answer's replay decided on, and an `ended_by` written from the stale round ends a seat that
    is playing again, on a round nothing but an answer can reopen (finding 6). `_end`'s
    `when_answered` is what refuses it.

    The undo is fired from inside the search, on a second connection, which is both the real
    interleaving and a proof of the repair it rides on: a thread that can get a coroutine run while
    it waits is a thread the loop is not blocked by. The forced stop reason is the other half of the
    fixture -- a six-title pool does not resolve inside the two answers given here, so the ending
    has to be the one the replay would have reported rather than one this pool produces.
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    state = await play.state_for(db, seat, z=Z)
    pair, seq = state["_pair"], state["answered"] + 1

    loop = asyncio.get_running_loop()
    real = rnd.replay
    undoing = await second_connection(pg_url)
    calls: list[int] = []

    def undoes_the_answer_it_is_replaying(*args, **kw):
        calls.append(1)
        if len(calls) > 1:
            # The undo's own replay, which must be the real one or this fixture ends the seat by
            # the very route it is asserting nothing ends it by.
            return real(*args, **kw)
        asyncio.run_coroutine_threadsafe(play.retract(undoing, seat, z=Z), loop).result(timeout=5)
        return dataclasses.replace(real(*args, **kw), next_pair=None, stop_reason=rnd.CAP)

    monkeypatch.setattr(rnd, "replay", undoes_the_answer_it_is_replaying)
    try:
        written = await play.record_answer(
            db, participant_id=seat, pair=pair, answer=rnd.A, seq=seq, latency_ms=900, z=Z,
        )
    finally:
        await undoing.close()
    monkeypatch.undo()

    assert len(calls) == 2, "the undo never landed inside the search, so nothing was raced"
    assert written["ended_by"] is None, "a stale reason ended a seat that is playing again"
    assert written["stop_reason"] is None and written["pair"] is None, (
        "a card built from a replay the seat has moved past reports the row and no pair"
    )
    assert written["answered"] == 2, written
    row = await db.fetchrow(
        "SELECT ended_by, answered_count FROM session_participant WHERE id = $1", seat
    )
    assert (row["ended_by"], row["answered_count"]) == (None, 2)
    _sound(*await _seat_rows(db, seat))
    # The wedge is never the race, it is the state it leaves: the next tap has to work.
    assert await answer_once(db, seat) is not None, "the seat cannot be answered any more"
    _sound(*await _seat_rows(db, seat))


async def test_an_undo_during_a_read_of_the_round_leaves_the_seat_playing(
    db, world, pg_url, monkeypatch
):
    """The same hazard on the third caller, which this milestone made a writer in the same diff.

    `state_for` ends a stranded seat (finding 6) and its replay is now off the loop (finding 2), so
    the gap between its `_answers` read and its `_end` is a real await point where another request
    for the seat is served and committed -- and the undo is the ordinary one, because `refresh()`
    ends in `loadRound` on every channel frame while the person taps Undo on the same phone. The
    write paths pass `_end`'s `when_answered` guard for exactly this interleaving; the read did not,
    on the argument that "a read that refused to end a stranded seat because the count moved under
    it would be the seat nothing ends again". That argument is false in both halves: the writer that
    moved the count evaluates the ending for its own count in `_next_card`, and the next read of
    this seat replays the fresh answers and ends it correctly -- so refusing costs one read, while
    ending on a stale reason costs the round. `_card` suppresses the pair on the stop reason and
    never on `ended_by`, so the stamped seat was then served a live pair it could not answer: every
    tap 409 `round_over`, and the client re-reads on a 409 and re-renders the same dead card.

    Fired from inside the read's own search on a second connection, which is the write-path test
    above turned on the read -- and the same proof that the search is off the loop.
    [M4.12 review cycle 1: M412-CONC-01]
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)

    loop = asyncio.get_running_loop()
    real = rnd.replay
    undoing = await second_connection(pg_url)
    calls: list[int] = []

    def undoes_the_answer_it_is_replaying(*args, **kw):
        calls.append(1)
        if len(calls) > 1:
            return real(*args, **kw)
        asyncio.run_coroutine_threadsafe(play.retract(undoing, seat, z=Z), loop).result(timeout=5)
        return dataclasses.replace(real(*args, **kw), next_pair=None, stop_reason=rnd.CAP)

    monkeypatch.setattr(rnd, "replay", undoes_the_answer_it_is_replaying)
    try:
        seen = await play.state_for(db, seat, z=Z)
    finally:
        await undoing.close()
    monkeypatch.undo()

    assert len(calls) == 2, "the undo never landed inside the read's search, so nothing was raced"
    row = await db.fetchrow(
        "SELECT ended_by, answered_count FROM session_participant WHERE id = $1", seat
    )
    assert row["ended_by"] is None, "a read ended a seat on a round it had already moved past"
    assert row["answered_count"] == 1, dict(row)
    assert (seen["ended_by"], seen["stop_reason"], seen["pair"]) == (None, None, None), (
        "a card built from a replay the seat has moved past reports the row and no pair"
    )
    assert seen["answered"] == 1, seen

    # And the seat is not stranded either, which is the cost the omitted guard was defended with:
    # the very next read replays the answers that are actually there and serves a pair again.
    again = await play.state_for(db, seat, z=Z)
    assert again["_pair"] is not None and again["ended_by"] is None, again
    assert await answer_once(db, seat) is not None, "the seat cannot be answered any more"
    _sound(*await _seat_rows(db, seat))


async def test_the_combine_does_not_hold_the_loop_either(db, world, monkeypatch):
    """`finish` replays once PER SEAT, on the answer that ends the room -- the request a household
    is most obviously waiting on, and the one 54e's simultaneous reveal makes everybody's.

    Same hop for the same reason, and the tally is what says the work is per seat rather than a
    constant somebody could mistake for cheap. [finding 2]
    """
    room = await finished_session(db, world)
    seats = await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1", room["session_id"]
    )
    tally = _count_the_work(monkeypatch, replay_delay=0.3)

    async with _loop_gaps() as gaps:
        await play.finish(db, room["session_id"], z=Z)

    assert tally["replays"] == seats, f"one replay per seat, not {tally['replays']} for {seats}"
    assert tally["snapshots"] == 1, "and one decode of the frozen pool for the whole combine"
    assert max(gaps) < 0.2, (
        f"the combine held the loop for {max(gaps):.3f}s over {tally['replays']} seats"
    )


async def test_the_combine_does_not_search_for_a_pair_it_will_never_show(db, world, monkeypatch):
    """The other half of the combine's cost, and the one threading only moves rather than removes.

    `finish` reads `played.beliefs` and nothing else, but replayed with `select` at its default, so
    every seat whose posterior still straddles the boundary paid for a full O(n^2) pair search whose
    result is discarded — and that is every seat that ended at 54c's escape, since `finish` never
    tells `replay` the seat escaped and a converged seat has an empty straddler set to search.
    Measured on a 696-title pool at the owned-pool spread: 95-101 ms per escaped seat against 0.7-1.0
    ms with the flag off, so a household where two or three people tap "just pick for us" spends
    200-300 ms of §6's 1.5 s budget on pairs nobody will be shown, on the request the whole round
    exists to produce. `solo.picks` already passes the flag for exactly this reason (finding 35).

    THE SECOND RUN IS WHAT KEEPS THE FIRST ASSERTION FROM BEING VACUOUS. A fixture whose seats have
    nothing left to straddle would satisfy `calls == []` under either implementation, so the same
    combine is run again with the flag forced back on: that run must search, and it must produce the
    identical slate — `select` decides whether a pair is chosen, never what the beliefs are, which
    is the whole of why it is safe here. [M4.12 review cycle 1: M412-PLAY-2]
    """
    room = await running_room(db, world)
    for seat in room["seats"]:
        await answer_once(db, seat["id"])
        # 54c's escape, written rather than tapped: `play.escape` refuses before pair six, and the
        # state this is about is the posterior it leaves behind, not the route that got there.
        await db.execute(
            "UPDATE session_participant SET ended_by = 'escape' WHERE id = $1", seat["id"]
        )
    _, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    slate = await play.finish(db, room["session_id"], z=Z)
    assert calls == [], f"the combine ran {len(calls)} pair searches for pairs nobody is shown"

    real = rnd.replay
    monkeypatch.setattr(rnd, "replay", lambda *a, **kw: real(*a, **{**kw, "select": True}))
    again = await play.finish(db, room["session_id"], z=Z)

    assert calls, "no seat had a boundary left to search, so the assertion above proved nothing"
    assert again.rows == slate.rows, "the flag moved the slate, and it may only move the cost"


# --- M4.12 findings 7, 8 and 9: the room the host can end, and the seats it stops serving --------
#
# `rooms.open_session`'s abandon clause takes rooms in state `open` only, and
# `test_a_started_room_is_not_abandoned_by_a_second_one` above asserts that ON PURPOSE -- a round in
# progress is people answering on their own phones. So a room that reached `voting` and lost a seat
# was live for ever: on §6.2 step 2's list for every household device, holding the member's seat so
# every later visit restored them into it, and unfinishable. There is no Tonight job in
# `worker.py`'s registry, and recovery was SQL against `session`. Decision 169 ships a host-only
# control in the same diff as `play.settle`, and the pair is the whole lifecycle: `settle` moves a
# room whose seats have all ended, and this is what the host taps when they never will.
#
# Not a DELETE, which is the other half of the decision: §14 risk 6 reads `session_answer`,
# `session_participant.ended_by` and `session_outcome` for the rates it asks for, and an evening the
# household abandoned is exactly the evening those rates are about. 0013's `session_ended_states`
# CHECK already ties `ended_at` to `abandoned`, so the state and the timestamp cannot drift and no
# migration is owed.
#
# And the room has to stop SERVING, which is finding 8: `play._participant` read the session's state
# and never its `ended_at`, so every seat in an abandoned room kept answering -- a round with a pair
# on it, an answer written into an evening that was over, an undo accepted. The assertions below go
# through the routes for that reason: the defect is what a phone still in the room receives.


async def _answers_twice(client, seat):
    """Two answers through the real routes, so the room the host ends has votes in it."""
    for _ in range(2):
        card = await _round_card(client, seat)
        assert card["card_token"], card
        answered = await _tap(client, seat, card)
        assert answered.status_code == 200, answered.text


async def test_the_host_can_end_a_started_room_and_a_member_cannot(household, db):
    """Decision 169: host-only. §6.2 step 1 gives the host the session's controls, and a member
    ending the host's evening for the whole household is that failure with the sign flipped.

    The same check `start` makes, written at the route for `start`'s reason: the host is a column on
    `session`, and a rule living in `rooms.end_session` would have to be told who was asking.
    """
    sid = household["session_id"]
    refused = await household["member"].post(f"/api/tonight/sessions/{sid}/end")
    assert refused.status_code == 403, refused.text
    still = await db.fetchrow("SELECT state, ended_at FROM session WHERE id = $1", sid)
    assert (still["state"], still["ended_at"]) == ("voting", None), (
        "a member's refused call left the room in something other than the round it was in"
    )

    ended = await household["host"].post(f"/api/tonight/sessions/{sid}/end")
    assert ended.status_code == 200, ended.text
    assert ended.json()["state"] == "abandoned", ended.text
    row = await db.fetchrow("SELECT state, ended_at FROM session WHERE id = $1", sid)
    assert row["state"] == "abandoned"
    assert row["ended_at"] is not None, (
        "0013's session_ended_states CHECK ties the two, so a state without a timestamp is not "
        "reachable -- if this fails the write went somewhere other than set_state"
    )


async def test_the_room_the_host_ended_leaves_every_households_open_rooms_list(household):
    """§6.2 step 2: "active sessions are visible to **every household device**" -- which is also
    what made the defect permanent rather than private to the host."""
    sid = household["session_id"]

    async def listed(client):
        rooms_res = await client.get("/api/tonight/rooms")
        assert rooms_res.status_code == 200, rooms_res.text
        return [r["session_id"] for r in rooms_res.json()["rooms"]]

    for who in ("host", "member"):
        assert sid in await listed(household[who]), f"{who} cannot see the room to begin with"

    assert (await household["host"].post(f"/api/tonight/sessions/{sid}/end")).status_code == 200

    for who in ("host", "member"):
        assert sid not in await listed(household[who]), (
            f"the ended room is still on {who}'s open-rooms list"
        )


async def test_the_votes_survive_the_room_the_host_ended(household, db):
    """§14 risk 6: "log every vote". The control ends the evening; it does not unhappen it.

    A DELETE would take the answers, the seats' `ended_by` and any slate with it -- three of the
    columns §14 risk 6 asks for rates from, on exactly the evenings a household cut short.
    """
    sid = household["session_id"]
    seat = household["seats"]["patrick"]
    await _answers_twice(household["host"], seat)
    counted = (
        "SELECT count(*) FROM session_answer a "
        "JOIN session_participant p ON p.id = a.participant_id WHERE p.session_id = $1"
    )
    before = await db.fetchval(counted, sid)
    assert before == 2, f"the fixture wrote {before} answers, so this asserts nothing"

    assert (await household["host"].post(f"/api/tonight/sessions/{sid}/end")).status_code == 200

    assert await db.fetchval(counted, sid) == before, "ending the room took the votes with it"
    assert await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1", sid
    ) == 2, "ending the room took the seats with it"
    assert await db.fetchval(
        "SELECT answered_count FROM session_participant WHERE id = $1", seat
    ) == 2


async def test_a_seat_in_an_ended_room_is_refused_rather_than_served_a_round(household, db):
    """Finding 8, through the routes the phone uses. `play._participant` selected `s.state` and
    never `s.ended_at`, so every seat in an abandoned room kept playing: a pair to answer, an
    answer written into an evening that was over, an undo accepted.

    The card is taken BEFORE the room ends, because that is the device the defect is about -- a
    member whose phone is on the round screen when the host closes the room. A token minted while
    the room was live is not a way back into it.
    """
    sid = household["session_id"]
    seat = household["seats"]["patrick"]
    theirs = household["seats"]["jenny"]
    await _answers_twice(household["host"], seat)
    card = await _round_card(household["host"], seat)
    assert card["pair"] is not None, "the fixture's seat has a pair, so there is one to be refused"

    assert (await household["host"].post(f"/api/tonight/sessions/{sid}/end")).status_code == 200

    read = await household["host"].get(f"/api/tonight/seats/{seat}/round")
    assert read.status_code == 404, (
        f"the round of a seat in an ended room answered {read.status_code}: {read.text}"
    )
    assert read.json()["detail"]["reason"] == "no_room", read.text

    answered = await _tap(household["host"], seat, card)
    assert answered.status_code == 404, (
        f"an answer landed in a room the host had closed: {answered.status_code} {answered.text}"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 2, "the refused answer was written anyway"

    undone = await household["host"].post(f"/api/tonight/seats/{seat}/undo")
    assert undone.status_code == 404, undone.text

    # The member's own seat, which is the sentence finding 8 is written about: the other phone kept
    # a lobby for a room that no longer exists, and the API was still serving that seat.
    mine = await household["member"].get(f"/api/tonight/seats/{theirs}/round")
    assert mine.status_code == 404, mine.text


async def test_ending_a_room_the_household_already_resolved_is_refused(db, world):
    """The guard the control needs, and the reason it is a row lock rather than a bare read.

    Two taps on End, or an End from a lobby whose room has since reached its reveal, would
    otherwise rewrite `state` over `resolved` -- and §14 risk 6 reads that row for the evenings
    that ended in a film. The live room is taken `FOR UPDATE` before `set_state`, so a second
    caller waits and is then refused on what the first actually committed, and an End arriving
    during `play.start`'s claim serialises with it instead of racing it.
    """
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"], z=Z)
    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[slate.finalists[0]])
    outcome = await ballot.resolve(db, room["session_id"])

    with pytest.raises(rooms.RoomError) as refused:
        await rooms.end_session(db, room["session_id"])
    assert refused.value.reason == "no_room"

    row = await db.fetchrow("SELECT state, ended_at FROM session WHERE id = $1", room["session_id"])
    assert row["state"] == "resolved" and row["ended_at"] is not None
    assert await db.fetchval(
        "SELECT chosen_title_id FROM session_outcome WHERE session_id = $1", room["session_id"]
    ) == outcome["chosen_title_id"], "the evening's own outcome was overwritten by ending it again"


async def test_a_combine_landing_after_the_host_ended_the_room_does_not_revive_it(
    db, world, pg_url, monkeypatch
):
    """The other ordering, which the row lock above does not reach on its own.

    `end_session` serialises two Ends against each other, and the test above asserts that. It
    cannot serialise against `finish`, which took `pg_advisory_xact_lock` in a different lock space
    and then moved the room with a bare `set_state(..., 'ballot')` -- a statement with no predicate
    at all, and one that writes `ended_at = NULL` for every state that is not an ended one. So an
    End committing while the combine ran was silently undone: the host's phone had its 200
    {"state": "abandoned"} and left the room, and the room came back in `ballot` with `ended_at`
    NULL, on §6.2 step 2's list for every household device and holding its code against
    `session_room_code_live`. It is the only write in the backend that can move a session from an
    ended state back to a live one.

    THE WINDOW IS THE WHOLE COMBINE, not an instant. `settle` reads the state in its own unlocked
    statement and then decodes the frozen pool, replays once per seat off the loop and runs the
    per-slate-title match lines before `finish` reaches that write -- and this is exactly the room
    decision 169's control exists for, so every other phone in it is polling the lobby and
    therefore calling `settle`. The End is fired from inside the replay for that reason: it is the
    real interleaving, and firing it from a thread that the loop is free to serve is the same proof
    `test_an_undo_during_the_search_leaves_the_seat_playing` rests on.
    [M4.12 review cycle 1: M412-PLAY-1]
    """
    room = await finished_session(db, world)
    session_id = room["session_id"]
    loop = asyncio.get_running_loop()
    real = rnd.replay
    ending = await second_connection(pg_url)
    calls: list[int] = []

    def ends_the_room_it_is_combining_for(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            asyncio.run_coroutine_threadsafe(rooms.end_session(ending, session_id), loop).result(
                timeout=5
            )
        return real(*args, **kw)

    monkeypatch.setattr(rnd, "replay", ends_the_room_it_is_combining_for)
    try:
        settled = await play.settle(db, session_id, z=Z)
    finally:
        await ending.close()
    monkeypatch.undo()

    assert calls, "the End never landed inside the combine, so nothing was raced"
    assert settled is False, "a room the household ended was not moved on by this call"
    row = await db.fetchrow("SELECT state, ended_at FROM session WHERE id = $1", session_id)
    assert (row["state"], row["ended_at"] is None) == ("abandoned", False), (
        f"the host's End was undone by the combine: {dict(row)}"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) == 0, "and no slate was written into an evening that had already ended"
    with pytest.raises(play.RoundError) as refused:
        await play.state_for(db, room["seats"][0]["id"], z=Z)
    assert refused.value.reason == "no_room", "the seats of an ended room stay unserved"


# --- M4.12 findings 28, 29 and 30: the arm is a rate, and its draw is sealed --------------------
#
# Through the routes, and with the arm left exactly as it ships. §13's guard is a claim about what
# a phone can obtain from the server -- twelve GETs at answered = 9 returned twelve DISTINCT
# "uniform-random" pairs, each sealed into a valid card token, and answering with the first-minted
# one was accepted -- so the seal has to be measured where the tokens are minted. `play.state_for`
# alone would show the pair but not the token, and the token is what made the stream choosable.
#
# Which pair the arm draws is a fact about the seat and no longer about the tenth slot (decision
# 223), and it is a *deterministic* fact here rather than a draw over whatever ran first:
# `conftest.db` drops and recreates the schema per test, so `session_participant`'s ids restart.
# The rate itself is measured over 2000 keys in `test_tonight_round.py`, where it costs a
# millisecond instead of a started room.

import itertools  # noqa: E402


async def _to_the_arm(client, seat):
    """Answer this seat's pairs until the card on the table is 54b's uniform-random one."""
    held = next(seq for seq in itertools.count(1) if rnd.is_holdout(seq, key=str(seat)))
    assert held <= rnd.CAP_PAIRS, (
        f"seat {seat}'s arm first fires at pair {held}, past the cap -- this test needs a seat "
        "whose hold-out is inside a round"
    )
    for _ in range(held - 1):
        card = await _round_card(client, seat)
        assert card["pair"] is not None, f"the round ended before seat {seat}'s pair {held}"
        assert (await _tap(client, seat, card)).status_code == 200
    card = await _round_card(client, seat)
    assert card["pair"] is not None, f"the round ended before seat {seat}'s pair {held}"
    assert card["pair"]["selection"] == rnd.SELECTION_HOLDOUT, (
        f"pair {held} was served by the adaptive arm, so this test would assert nothing: "
        f"{card['pair']['selection']}"
    )
    return card


async def test_twelve_reads_of_the_arms_card_return_one_pair_and_one_token(household, db):
    """54b/§13: the hold-out pair for a seat at an answer count is one pair, on every read.

    It was drawn with the request's own `SystemRandom` and nothing persisted it, so every GET of
    the round re-rolled §13's only admissible stream and every roll was sealed into a token the
    answer route accepted. A client could pick its own "uniform-random" pair by reloading, and an
    honest one hit it anyway: `refresh()` re-reads the round on every rooms.changed / lobby /
    reveal frame and on reconnect, so a screen lock changed the card under the person's thumb.
    [findings 28 and 29; decision 223]
    """
    host, seat = household["host"], household["seats"]["patrick"]
    card = await _to_the_arm(host, seat)

    reads = [await _round_card(host, seat) for _ in range(12)]
    pairs = {_pair_ids(c) for c in reads}
    tokens = {c["card_token"] for c in reads}
    assert pairs == {_pair_ids(card)}, (
        f"twelve reads of one card returned {len(pairs)} different uniform-random pairs: {pairs}"
    )
    assert tokens == {card["card_token"]}, (
        f"twelve reads minted {len(tokens)} different card tokens for one seat and one count"
    )

    # And the token names that pair and no other: what the seal carried is what the row records,
    # which is the half §13 counts -- `session_answer` is append-only (§4.2) and its `selection`
    # is the discriminator the evaluation reads.
    assert (await _tap(host, seat, card)).status_code == 200
    row = await db.fetchrow(
        "SELECT title_a, title_b, selection FROM session_answer WHERE participant_id = $1 "
        "ORDER BY seq DESC LIMIT 1",
        seat,
    )
    assert (row["title_a"], row["title_b"]) == _pair_ids(card)
    assert row["selection"] == rnd.SELECTION_HOLDOUT


async def test_the_card_a_phone_stashed_is_the_card_the_undo_re_issues(household, db):
    """Finding 29, which the seal closes by construction rather than with a second check.

    `retract` lowers `answered_count`, and the card is sealed against (seat, count), so every
    token ever minted for the retracted seq is valid again -- reproduced before this milestone as
    a pre-undo token naming [8, 21] accepted as the live seq-10 answer while the server was
    serving [8, 9]. With the draw sealed the re-issued token is byte-identical to the stashed one,
    so "still valid" and "still the same pair" stop being different statements. Sealing the
    retraction count as well would close the adaptive residual too, and is deliberately not in
    this milestone (decision 223).
    """
    host, seat = household["host"], household["seats"]["patrick"]
    card = await _to_the_arm(host, seat)
    stashed = card["card_token"]
    assert (await _tap(host, seat, card)).status_code == 200

    undone = await host.post(f"/api/tonight/seats/{seat}/undo")
    assert undone.status_code == 200, undone.text
    reopened = undone.json()
    assert reopened["card_token"] == stashed, (
        "the undo re-opened the seq with a different draw, so the token the phone kept names a "
        "pair the server no longer means to ask"
    )

    replayed = await host.post(
        f"/api/tonight/seats/{seat}/answer",
        json={"card_token": stashed, "answer": rnd.B, "latency_ms": 900},
    )
    assert replayed.status_code == 200, replayed.text
    live = await db.fetch(
        "SELECT title_a, title_b, selection FROM session_answer WHERE participant_id = $1 "
        "AND retracted_at IS NULL ORDER BY seq",
        seat,
    )
    assert (live[-1]["title_a"], live[-1]["title_b"]) == _pair_ids(card), (
        "the stashed token stored a pair other than the one on the screen"
    )
    assert live[-1]["selection"] == rnd.SELECTION_HOLDOUT


async def test_the_arm_a_seat_is_served_is_the_one_the_rule_draws_for_that_seat(household, db):
    """The key reaches `select` from the row, and §13's stream is exactly the rule's.

    The arm is drawn from the seat id (decision 223), which is what makes it re-derivable by the
    one caller that has no row to read it off -- solo. A round keyed on anything else would still
    look like a 1-in-10 stream from outside while classifying a different set of answers than the
    rule names, and nothing downstream would contradict it: `evaluation` reads the stored column.
    """
    host, seat = household["host"], household["seats"]["patrick"]
    served = []
    for _ in range(rnd.CAP_PAIRS):
        card = await _round_card(host, seat)
        if card["pair"] is None:
            break
        served.append((card["answered"] + 1, card["pair"]["selection"]))
        assert (await _tap(host, seat, card)).status_code == 200

    assert served, "this seat was served no pair at all"
    assert [seq for seq, arm in served if arm == rnd.SELECTION_HOLDOUT] == [
        seq for seq, _ in served if rnd.is_holdout(seq, key=str(seat))
    ], "the arms served are not the ones the rule draws for this seat"
    rows = await db.fetch(
        "SELECT seq, selection FROM session_answer WHERE participant_id = $1 ORDER BY seq", seat
    )
    assert [(r["seq"], r["selection"]) for r in rows] == served, (
        "the stored arm is not the arm the seat was served"
    )


# --- §6.2 step 8 (54f): the door, the walk and the tilt's own filter --------------------------
#
# M4.12 step 11, findings 35, 36 and 37. Three defects of one screen, and the reason they are one
# section is that all three are about the same sentence: 54f puts solo on "the fastest path to a
# film", which is a claim about what a tap costs, about a Reshuffle that moves, and about a
# provenance line that says what actually happened.


def _counting(module, name):
    """Wrap a module attribute so a test can say how often the round reached it.

    It patches `_select_pair` rather than `select`: `round.replay` binds the selector at import
    under a private alias, because `replay`'s own `select: bool` flag shadows the public name in
    that scope. Patching `round.select` would observe nothing at all, which is a green test
    asserting nothing — the shape this milestone has spent a stage removing.
    """
    original = getattr(module, name)
    calls: list[tuple] = []

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    return original, counted, calls


async def test_the_solo_door_lands_on_picks_without_running_the_pair_search(
    db, world, monkeypatch
):
    """54f: "lands **directly** on three picks and a wildcard … the fastest path to a film must
    not be slower than browsing Home."

    `picks` replayed AND selected on every request, and with nothing answered `stop_reason` is
    None, so the search ran over the straddling set of the whole pool — 0.677 s at 200 candidates
    and tens of seconds on the shipped 696-title owned pool, on the one screen the spec puts a
    latency sentence on. The call count is the assertion rather than the clock, because a six-film
    fixture is fast either way and a timing threshold on this pool would measure the test machine.
    """
    original, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    out = await solo_picks(db, world)

    assert calls == [], "the door paid for a pair search nobody asked for"
    assert out["pair"] is None and out["stop_reason"] is None, (
        "a door that reports a stop reason has run a round, and 54f says it has not"
    )
    assert len(out["picks"]) == solo.PICKS and out["wildcard"] is not None
    assert original is rnd.select, "the alias and the public selector must be one function"


async def test_reshuffle_does_not_run_the_pair_search_either(db, world, monkeypatch):
    """54f gives Reshuffle one job — "walks further down the ranking" — and it draws no pair, so
    it may not pay for one. This is the half of finding 35 a household meets most often: the
    search ran again on every press."""
    _, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    out = await solo_picks(db, world, offset=1)

    assert calls == []
    assert out["pair"] is None
    assert len(out["picks"]) == solo.PICKS


async def test_only_an_explicit_sharpen_draws_a_pair(db, world, monkeypatch):
    """The other side of the same flag: "sharpen this" is the tap that asks for a round, and it
    still gets one."""
    _, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    out = await solo_picks(db, world, sharpen=True)

    assert len(calls) == 1, "one tap, one search"
    assert out["pair"] is not None, "the control that exists to ask has to ask"
    assert "scores" not in out["pair"]["a"]


async def test_the_replay_still_runs_when_the_door_does_not_select(db, world):
    """The precision the flag's name invites someone to get wrong. The door skips the SELECTION;
    it does not skip the replay, because 54f's "re-ranks in place" is the whole of the sharpen
    control and the re-ranking is `played.beliefs` plus the tilt.

    So a door request carrying answers must return a different order from one carrying none — on
    the same pool, at the same offset, with no pair drawn in either.
    """
    first = await solo_picks(db, world, sharpen=True)
    assert first["pair"] is not None
    answered = [rnd.Answered(
        seq=1, title_a=first["pair"]["a"]["title_id"],
        title_b=first["pair"]["b"]["title_id"], answer=rnd.A,
    )]

    plain = await solo_picks(db, world)
    tilted = await solo_picks(db, world, answers=answered)

    assert tilted["pair"] is None, "a door is a door even with a history behind it"
    assert tilted["sharpened"] is True and "tilted by your 1 answers" in tilted["provenance"]
    assert tilted["tilt"] != {}, "the answers reached the ranking without a search being run"
    assert [p["title_id"] for p in tilted["picks"]] != [p["title_id"] for p in plain["picks"]]


async def test_reshuffle_moves_the_picks_on_a_four_title_pool(db, world):
    """Finding 36, in the shape a household meets it. `span = max(len(order) - 1, 1)` makes
    `3 * offset % 3` zero for every offset on a pool of four, so Reshuffle returned the identical
    three titles for ever — while `wrapped` reported True from the first press, which decision 222
    now renders on the screen.

    Four titles, and built the way SOLO builds one — a single seat, which is the whole reason
    solo can meet a small pool that a group session never would: the rewatch filter's quantifier
    is "every participant has seen", and with one participant that is "Patrick has seen". At a
    170 min budget his pool is titles 1-4 (5 and 6 he has seen, 7 is a series, 8 is unowned).
    """
    solo_seat = [pool.Seat(
        participant_id=world["patrick"], user_id=world["patrick"], is_member=True
    )]
    pool_ids = {c.title_id for c in await pool.build(
        db, seats=solo_seat, kind="movie", budget_min=170, include_rewatches=False,
        bundle_version=BUNDLE,
    )}
    assert len(pool_ids) == 4, f"this claim is about a four-title pool, and this one is {pool_ids}"

    presses = [
        await solo_picks(db, world, budget_min=170, include_rewatches=False, offset=n)
        for n in range(3)
    ]
    picked = [tuple(p["title_id"] for p in out["picks"]) for out in presses]

    assert picked[0] != picked[1], "two consecutive presses of Reshuffle returned the same three"
    assert presses[0]["wrapped"] is False, "the first view has not wrapped anything"
    # PER PRESS, not `any(...)`. The walk comes back round to the top of the ranking two ways: the
    # modulus at `start`, and the wrap-fill that splices the top back in whenever `start + PICKS`
    # passes the end -- and on any pool whose length is not a multiple of three the fill fires
    # FIRST. `wrapped` read the modulus alone, so on the press that first shows two titles the
    # household was looking at a second ago (ranks 4, 1, 2 here) the screen said nothing, which is
    # the exact confusion decision 222 exists to remove. `any(out["wrapped"] for out in
    # presses[1:])` was satisfied by press 2 and could not see it.
    # [M4.12 review cycle 1: M412-FE-2, M412-SOLO-02]
    assert picked[1][1:] == picked[0][:2], (
        f"press 1 is meant to wrap the top of the ranking back in: {picked}"
    )
    assert [out["wrapped"] for out in presses] == [False, True, True], (
        f"the line decision 222 renders is missing on the press that wraps: {picked}"
    )


async def test_every_title_in_a_seven_title_pool_is_reachable_as_a_pick(db, world):
    """The general form of the same defect: for any n congruent to 1 mod 3 the old span left the
    last-ranked title unreachable — n = 7 alternated [1,2,3] and [4,5,6] for ever, so the seventh
    title could not be picked no matter how many times the household pressed Reshuffle.

    64 presses because that is `SoloBody.offset`'s own bound, so this walks the whole range a
    client can ask for rather than a range chosen to pass.
    """
    await db.execute(
        "INSERT INTO title (id, kind, name, year, runtime_min, is_owned) "
        "VALUES (9, 'movie', 'Title 9', 2010, 100, true)"
    )
    for user_id in (world["patrick"], world["jenny"]):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "VALUES ($1, 9, 'movie', $2, 0.35, 0.0)",
            user_id, BUNDLE,
        )
    solo_seat = [pool.Seat(
        participant_id=world["patrick"], user_id=world["patrick"], is_member=True
    )]
    pool_ids = {c.title_id for c in await pool.build(
        db, seats=solo_seat, kind="movie", budget_min=200, include_rewatches=True,
        bundle_version=BUNDLE,
    )}
    assert len(pool_ids) == 7, f"this claim is about a seven-title pool, and this one is {pool_ids}"

    reached: set[int] = set()
    for n in range(64):
        out = await solo_picks(db, world, offset=n)
        reached |= {p["title_id"] for p in out["picks"]}

    assert reached == pool_ids, f"unreachable by any Reshuffle: {sorted(pool_ids - reached)}"


async def test_the_provenance_line_counts_the_answers_the_replay_counted(db, world):
    """Finding 37. `round.replay` skips a stored answer whose titles have left the pool — §10, and
    "the alternative is inventing a belief for a title that is no longer a candidate" — and solo
    then walked the SAME list itself with no such guard.

    Solo rebuilds its pool on every request, so the two disagreed whenever anything changed between
    two sharpen taps: the answer moved the tilt the posterior had ignored, and 54f's provenance
    line credited it. Title 8 is in the catalogue and is not owned, so it is a title id the client
    can legitimately be holding from a pool that has since changed under it.
    """
    sent = [rnd.Answered(seq=1, title_a=1, title_b=8, answer=rnd.A)]

    out = await solo_picks(db, world, answers=sent)

    assert out["tilt"] == {}, "an answer the replay ignored moved the tilt"
    assert out["sharpened"] is False
    assert "tilted by your" not in out["provenance"], out["provenance"]
    # And it still costs them one of their twenty, which is the count that is NOT filtered: 54b
    # says a pair the person answered is a pair they answered.
    assert out["answered"] == 1


async def test_an_answer_inside_the_pool_still_counts(db, world):
    """The other half of the same predicate, because a filter that refused everything would pass
    the test above. The guard is §10's — "has this title left the pool" — and never "does this
    title carry any DNA": a library with no tags tilts nothing, and its sharpen round still
    re-ranked the picks."""
    first = await solo_picks(db, world, sharpen=True)
    sent = [rnd.Answered(
        seq=1, title_a=first["pair"]["a"]["title_id"],
        title_b=first["pair"]["b"]["title_id"], answer=rnd.A,
    )]

    out = await solo_picks(db, world, answers=sent)

    assert out["sharpened"] is True
    assert out["tilt"] != {}
    assert "tilted by your 1 answers" in out["provenance"]


async def test_an_answer_naming_one_title_twice_is_not_a_comparison(db, world):
    """The same predicate as the two above, on the degenerate pair a hand-made body can send.

    `tilt.applies` is the §10 membership check the milestone lifted into one helper, and its own
    docstring calls it "whether a stored answer still names two candidates of tonight's pool" --
    but `title_a in vectors and title_b in vectors` is true when the two are the same title, and
    one candidate named twice is not two candidates. `round.replay`'s membership guard reads the
    same way, so the two rules agreed on the wrong answer: the belief moved a long way on "I prefer
    title 1 to title 1" (mu 0.9 -> 0.37 at the measured spread) while `tilt.applied` returned a
    delta of exactly zero -- so the picks were re-ranked and the screen simultaneously read "tilted
    by your 1 answers" over a tilt of {}. That is the dishonest provenance finding 37 set out to
    remove, arriving through the shared predicate instead of through the old `vectors.get(id, {})`.

    The group round cannot reach it -- `select` and the hold-out arm never emit `i == j`, and the
    card seal binds the pair -- so this is solo's client-supplied answer list, which is why the
    guard belongs in the rule rather than in `SoloBody`. [M4.12 review cycle 1: M412-SOLO-04]
    """
    plain = await solo_picks(db, world)
    top = plain["picks"][0]["title_id"]

    out = await solo_picks(db, world, answers=[
        rnd.Answered(seq=1, title_a=top, title_b=top, answer=rnd.A)
    ])

    assert out["tilt"] == {}, "a title compared with itself moved the tilt"
    assert out["sharpened"] is False, "and reported that it had tilted the picks"
    assert "tilted by your" not in out["provenance"], out["provenance"]
    assert [p["title_id"] for p in out["picks"]] == [p["title_id"] for p in plain["picks"]], (
        "the ranking moved on an answer that compares nothing"
    )
    # It still costs them one of their twenty, for `test_the_provenance_line_counts_...`'s reason:
    # the filtered number is what tilted, `answered` is what they answered.
    assert out["answered"] == 1


async def test_a_series_session_says_its_budget_is_per_episode(db, world):
    """54h, amending §6.2 step 1 (decision 219): on a series session the budget is compared with
    the show's per-episode runtime, "and every label that states a number on a series card says
    so". Title 7 is the fixture's owned series at 45 min per episode.

    Measured against the shipped bundle the series pool is 121 of 121 owned titles at budget 60,
    130 and 200 alike — so the slider narrows nothing on a series night and the label was the only
    thing that could tell the household what the number meant.
    """
    series = await build(db, world, kind="series", budget_min=60, include_rewatches=True)
    films = await build(db, world, budget_min=130)

    assert [c.title_id for c in series] == [7]
    assert series[0].fit_line == "fits your 60 min per episode"
    assert all("per episode" not in c.fit_line for c in films), (
        "a film's runtime is the evening's, and qualifying it would be a different lie"
    )

    solo_series = await solo_picks(
        db, world, kind="series", budget_min=60, include_rewatches=True
    )
    assert solo_series["picks"][0]["fit_line"] == "fits your 60 min per episode"
