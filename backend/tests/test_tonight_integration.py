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
PROJECTED = [(2, "dread", 0.7), (7, "relentless", 0.4)]


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
    for title_id, term, weight in PROJECTED:
        await db.execute(
            "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
            "VALUES ($1, $2, $3, $4, $5, 'keyword:fixture')",
            title_id, VOCAB, term, TERMS[term], weight,
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
    ordinary case in a household, not the rare one. The review found it; 0015 is the index.
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

Z = 1.0


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
    state = await play.state_for(db, participant_id, z=Z, rng=random.Random(0))
    if state["_pair"] is None:
        return None
    return await play.record_answer(
        db, participant_id=participant_id, pair=state["_pair"], answer=answer,
        seq=state["answered"] + 1, latency_ms=900, z=Z,
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
        state = await play.state_for(db, seat, z=Z, rng=random.Random(0))
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
    state = await play.state_for(db, seat, z=Z, rng=random.Random(0))
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
    meaning anything."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    assert await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", seat) == {}

    await answer_once(db, seat)
    after = await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", seat)
    assert isinstance(after, dict)


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
    count would be a second implementation of the rule."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    assert (await play.state_for(db, seat, z=Z))["escape_available"] is False
    for _ in range(5):
        if await answer_once(db, seat) is None:
            break
    state = await play.state_for(db, seat, z=Z)
    if state["answered"] >= rnd.ESCAPE_FROM_PAIR - 1:
        assert state["escape_available"] is True


# --- §6 preamble: undo everywhere ----------------------------------------------------------


async def test_a_participant_can_take_back_the_answer_they_just_gave(db, world):
    """§6's preamble puts undo on every surface, and the round is the one place a mis-tap is
    otherwise permanent: a hard cap, a blind reveal, no second pass. Tombstone rather than
    DELETE — §14 risk 6 says log every vote, and a retraction is itself a fact."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    tilt_before = await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", seat)

    out = await play.retract(db, seat)
    assert out["answered"] == 1
    row = await db.fetchrow(
        "SELECT answered_count, tilt FROM session_participant WHERE id = $1", seat
    )
    assert row["answered_count"] == 1
    assert row["tilt"] != tilt_before, "the retracted answer no longer moves the tilt"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 2, "the row survives as a tombstone"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL",
        seat,
    ) == 1


async def test_undo_reaches_only_your_own_last_live_answer(db, world):
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    with pytest.raises(play.RoundError) as nothing:
        await play.retract(db, seat)
    assert nothing.value.reason == "nothing_to_undo"


async def test_a_finished_round_cannot_be_edited(db, world):
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await run_to_the_end(db, seat)
    with pytest.raises(play.RoundError) as over:
        await play.retract(db, seat)
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

    state = await play.state_for(db, guest["id"], z=Z, rng=random.Random(0))
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
    state = await play.state_for(db, guests[1]["id"], z=Z, rng=random.Random(0))
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
    """
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
    """
    room = await running_room(db, world)
    heavy_seat, light_seat = room["seats"][0]["id"], room["seats"][1]["id"]

    # Each answers toward their own pole: whichever side of the pair carries `dread` for one,
    # `cosy` for the other. `A`/`B` is decided by the pair the round happens to serve, so the
    # tilt is driven by choosing the title that matches the seat's pole.
    snapshot = await play.snapshot_of(db, room["session_id"])
    for seat, want in ((heavy_seat, "dread"), (light_seat, "cosy")):
        for _ in range(rnd.CAP_PAIRS):
            state = await play.state_for(db, seat, z=Z, rng=random.Random(0))
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
    if slate.contested is None:
        pytest.skip("this pool did not divide the household enough to surface a split")

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
    assert len(held) == 1, "an adaptive answer must not reach the evaluation"
    assert held[0]["seq"] == 98

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
    first = await solo_picks(db, world, budget_min=130, include_rewatches=True)
    assert first["pair"] is not None, "the sharpen round has to have something to ask"

    answered = [
        rnd.Answered(seq=1, title_a=first["pair"]["a"]["title_id"],
                     title_b=first["pair"]["b"]["title_id"], answer=rnd.A)
    ]
    after = await solo_picks(
        db, world, budget_min=130, include_rewatches=True, answers=answered
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
    the candidate's own score would put the ranking on the screen it is kept off."""
    out = await solo_picks(db, world)
    if out["pair"] is None:
        pytest.skip("this pool converged before serving a pair")
    for side in ("a", "b"):
        assert "scores" not in out["pair"][side]
        assert "group_score" not in out["pair"][side]


# --- the adversarial review's survivors ------------------------------------------------------


async def test_the_answer_after_an_undo_is_accepted(db, world):
    """§6 preamble's "undo everywhere" — the whole of it, which is undo AND carry on.

    `retract` tombstones the row (§14 risk 6: log every vote) and rewinds the counter, so the
    replacement answer arrives at the seq the retraction freed. 0013's non-partial unique index
    on (participant_id, seq) made that insert collide with the tombstone forever: one tap on
    Undo ended that participant's round, and because 54e's reveal waits for every seat, the
    household's evening with it. The row's own words — "the round is served a pair again rather
    than advancing" — are what this asserts, and no test reached the *re-answer*.
    """
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    await play.retract(db, seat)

    assert await answer_once(db, seat) is not None, "the round has to be able to continue"
    live = await db.fetch(
        "SELECT seq FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL "
        "ORDER BY seq",
        seat,
    )
    assert [r["seq"] for r in live] == [1, 2]
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
