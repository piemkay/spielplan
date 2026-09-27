"""Pool membership is a query: owned (re-derived, §7.2), kind (§4.1 rule 5), and rewatch, whose
"every participant" a one-member fixture cannot tell from "any"."""

from __future__ import annotations

import pytest

from spielplan.tonight import pool
from tests.helpers import insert_user

BUNDLE = "test-v1"

VOCAB = "v1"

# A clean ±1.0 pair plus a second axis, so "the widest axis" has a choice. Shaped like §6.4's TSVs.
AXES = {
    "mood": ("heavy", "light", {"dread": -1.0, "cosy": 1.0}),
    "pacing": ("patient", "propulsive", {"patient": -1.0, "relentless": 0.8}),
}
TERMS = {"dread": "mood", "cosy": "mood", "patient": "pacing", "relentless": "pacing"}

# Titles 1-3 heavy, 4-6 light, so there is a contested axis. Title 2's projected duplicate of an
# extracted tag must be counted once (§4.1 rule 1).
EXTRACTED = [
    (1, "dread", 3), (1, "patient", 2),
    (2, "dread", 2), (2, "relentless", 3),
    (3, "dread", 1),
    (4, "cosy", 3), (4, "relentless", 2),
    (5, "cosy", 2), (5, "patient", 3),
    (6, "cosy", 1),
]
# `(title, term, n_sources)`: a COUNT of keyword sources (1..8), stored in `dna_projected.weight`,
# not a 0..1 probability. 8 is the corpus maximum (decision 188).
PROJECTED = [(2, "dread", 8), (7, "relentless", 1)]


async def seed_dna(db):
    """Without it every DNA read returns {} and three assertions go vacuous."""
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
        # `weight` is where the importer writes `n_sources`.
        await db.execute(
            "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
            "VALUES ($1, $2, $3, $4, $5, 'keyword:fixture')",
            title_id, VOCAB, term, TERMS[term], n_sources,
        )


@pytest.fixture
async def world(db):
    """Two members, because "every participant" is trivial with one.

    1..4  owned films, both scored          - the pool proper
    5     owned film, both have seen it     - the rewatch default removes it
    6     owned film, only Patrick has seen - the rewatch default KEEPS it
    7     owned series                      - the kind filter removes it from a film night
    8     film in the catalog, NOT owned    - the ownership filter removes it"""
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
    patrick = await insert_user(db, "patrick", "admin")
    jenny = await insert_user(db, "jenny")

    # Title 2 is the max-min winner and title 4 the mean winner, so ordering tests test the rule.
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
    # An absent row and a filtered row look identical, so the series is scored too.
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


async def test_the_pool_holds_only_owned_titles_of_the_sessions_kind(db, world):
    ids = [c.title_id for c in await build(db, world)]

    assert 8 not in ids, "title 8 is in the catalog but not owned"
    assert 7 not in ids, "title 7 is a series and this is a film session"
    assert set(ids) <= {1, 2, 3, 4, 6}


async def test_ownership_is_re_read_rather_than_remembered(db, world):
    """The pool is built at session open, so it must read the current `is_owned`."""
    assert 1 in [c.title_id for c in await build(db, world)]
    await db.execute("UPDATE title SET is_owned = false WHERE id = 1")
    assert 1 not in [c.title_id for c in await build(db, world)]


async def test_the_rewatch_default_removes_only_what_everyone_has_seen(db, world):
    """"Any participant" would strip title 6 too, and with it most of the good films."""
    ids = [c.title_id for c in await build(db, world)]

    assert 5 not in ids, "both members have seen title 5"
    assert 6 in ids, "only one member has seen title 6, so it stays in the default pool"


async def test_include_rewatches_admits_both(db, world):
    ids = [c.title_id for c in await build(db, world, include_rewatches=True)]
    assert 5 in ids and 6 in ids


async def test_the_budget_admits_forty_minutes_of_grace_and_labels_it(db, world):
    """Title 3 is 151 min against 130 (admitted, "runs 21 min over"); title 4 is 200 (dropped)."""
    by_id = {c.title_id: c for c in await build(db, world)}

    assert 3 in by_id and by_id[3].over_budget_min == 21
    assert by_id[3].fit_line == "runs 21 min over"
    assert 4 not in by_id, "200 min is past 130 + 40"
    assert by_id[1].over_budget_min is None and by_id[1].fit_line == "fits your 130 min"


async def test_a_wider_budget_admits_the_long_one(db, world):
    by_id = {c.title_id: c for c in await build(db, world, budget_min=170)}
    assert 4 in by_id and by_id[4].over_budget_min == 30


async def test_the_pool_is_ordered_by_the_plain_average_of_both_members(db, world):
    """Title 4 (mean 0.55) outranks title 1 (0.45) though it is Patrick's worst: §0 row 3."""
    ordered = await build(db, world, budget_min=200)
    ranked = [c.title_id for c in ordered]

    assert ranked.index(4) < ranked.index(1)
    by_id = {c.title_id: c for c in ordered}
    assert by_id[4].group_score == pytest.approx(0.55)
    assert by_id[1].group_score == pytest.approx(0.45)


async def test_a_score_from_a_superseded_bundle_is_not_returned(db, world):
    """§10: no process scores with a version other than the active row's."""
    assert await build(db, world) != []
    assert await build(db, world, bundle_version="some-other-version") == []


async def test_a_title_only_one_member_has_a_score_for_is_not_a_candidate(db, world):
    """A one-person mean would look like a group preference."""
    await db.execute("DELETE FROM user_score WHERE user_id = $1 AND title_id = 1", world["jenny"])
    assert 1 not in [c.title_id for c in await build(db, world)]


async def test_a_guest_seat_neither_scores_nor_filters(db, world):
    """A guest has no taste term and an unknown seen-state, so it neither scores nor filters."""
    guest = pool.Seat(participant_id=3, user_id=None, is_member=False)
    with_guest = await build(db, world, seats=[*world["seats"], guest])
    without = await build(db, world)

    assert [c.title_id for c in with_guest] == [c.title_id for c in without]
    for c in with_guest:
        assert set(c.scores) == {1, 2}, "a guest seat must not appear among the scored seats"


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
    """Read aloud and typed on a phone, and shown on a Home Assistant dashboard (§11)."""
    import random

    codes = {rooms.make_code(random.Random(seed)) for seed in range(300)}
    for code in codes:
        letters, _, digits = code.partition("-")
        assert len(letters) == 2 and len(digits) == rooms.CODE_LENGTH
        assert not set(code) & set("IO01"), f"{code} cannot be dictated unambiguously"
    assert len(codes) > 100, "the space must be wide enough that two live rooms rarely collide"


async def test_opening_a_room_seats_the_host_first_and_each_guest_after(db, world):
    """Guests take their turns after the initiator, so the seat carries that order."""
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
    """A second seat would change every per-participant average and §13's approval share."""
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
    """Neither opens a new session nor attaches to an ended one."""
    room = await open_room(db, world)
    with pytest.raises(rooms.RoomError) as absent:
        await rooms.resolve_code(db, "ZZ-9999")
    assert absent.value.reason == "no_room"

    await rooms.set_state(db, room["session_id"], rooms.STATE_RESOLVED)
    with pytest.raises(rooms.RoomError):
        await rooms.resolve_code(db, room["room_code"])


async def test_a_room_code_is_unique_among_live_rooms_and_reusable_after(db, world):
    """Two drawn codes rarely collide, so the database rule itself is asserted:
    `session_room_code_live` (0013), because the real failure is a race."""
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
    """Takes the code between `open_session`'s check and its insert; everything else is delegated."""

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
    """Losing the race used to raise `UniqueViolationError` as a 500."""
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
    """A host cannot host two unstarted rooms; the second tap ends the first (0013's `abandoned`)."""
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
    """A started round has people answering; only an unstarted room is abandoned."""
    running = await running_room(db, world)
    await open_room(db, world)

    row = await db.fetchrow(
        "SELECT state, ended_at FROM session WHERE id = $1", running["session_id"]
    )
    assert row["state"] == rooms.STATE_VOTING and row["ended_at"] is None


async def test_one_hosts_second_room_leaves_another_hosts_alone(db, world):
    """Two rooms in one household are supported; only the host's own is abandoned."""
    hers = await open_room(db, world, host_user_id=world["jenny"])
    await open_room(db, world, host_user_id=world["patrick"])
    await open_room(db, world, host_user_id=world["patrick"])

    row = await db.fetchrow("SELECT state FROM session WHERE id = $1", hers["session_id"])
    assert row["state"] == rooms.STATE_OPEN


async def test_the_open_rooms_list_is_visible_to_every_member_not_only_the_host(db, world):
    """Visible to every household device, with the six facets the spec's example names."""
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
    """The row drives "tappable empty seats", so it must know this device's member is already in."""
    room = await open_room(db, world)
    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    row = (await rooms.open_rooms(db, viewer_id=world["jenny"]))[0]

    assert row["viewer_seated"] is True
    assert row["joinable"] is False


async def test_joining_a_started_room_is_refused_rather_than_silently_ignored(db, world):
    """The participant set is fixed once pairs are served; a late arrival is told to join the next."""
    room = await open_room(db, world)
    await rooms.set_state(db, room["session_id"], rooms.STATE_VOTING)
    with pytest.raises(rooms.RoomError) as started:
        await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    assert started.value.reason == "started"


class _SeatsSomeoneMidRace:
    """Seats somebody between `join`'s seat read and its insert: the channels are one control."""

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
    """It was idempotent in sequence and a 500 at once."""
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
    """Two people read `max(seat) + 1` and both got the same number."""
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
    """The pool is never shown as a step, and the lobby is where it would leak."""
    room = await open_room(db, world)
    seen = await rooms.lobby(db, room["session_id"])
    text = repr(seen)

    for leaked in ("title_id", "score", "candidates", "pool", "group_score"):
        assert leaked not in text, f"the lobby payload carries {leaked}"


async def test_the_invitation_list_is_every_member_not_already_seated(db, world):
    """Not the host, and not anyone already seated."""
    room = await open_room(db, world)
    assert await rooms.members_to_invite(
        db, session_id=room["session_id"], host_user_id=world["patrick"]
    ) == [world["jenny"]]

    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    assert await rooms.members_to_invite(
        db, session_id=room["session_id"], host_user_id=world["patrick"]
    ) == []


async def test_a_pool_built_from_the_rooms_seats_matches_the_rooms_controls(db, world):
    """The room's kind, budget and rewatch setting build the pool, not caller defaults."""
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
    """The seats carry the taste question, so a guest cannot contribute a term by accident."""
    room = await open_room(db, world, guests=2)
    seats = await rooms.seats_of(db, room["session_id"])

    assert [s.is_member for s in seats] == [True, False, False]
    assert [s.user_id for s in seats] == [world["patrick"], None, None]


from spielplan.tonight import ballot, combine, play  # noqa: E402
from spielplan.tonight import round as rnd  # noqa: E402
from spielplan.tonight import tilt as tilt_rules  # noqa: E402


async def widen(db, world, *, extra=110):
    """Decision 477: rank-standardised members make `world`'s six films resolve in one pair, so tests
    of later answers need a household-sized pool. Each title carries one verified term so answers
    move the tilt."""
    first = 100
    ids = list(range(first, first + extra))
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT g, 'movie', 'Wide ' || g, 2012, 100, true FROM unnest($1::int[]) AS g
        """,
        ids,
    )
    for k, user_id in enumerate((world["patrick"], world["jenny"])):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "SELECT $1, g, 'movie', $2, 0.001 * ((g * $3) % 997), 0.0 FROM unnest($4::int[]) AS g",
            user_id, BUNDLE, 7 + 6 * k, ids,
        )
    terms = list(TERMS)
    for g in ids:
        term = terms[g % len(terms)]
        tag_id = await db.fetchval(
            "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, provider) "
            "VALUES ($1, $2, $3, $4, $5, 0.6, 'fixture') RETURNING id",
            g, VOCAB, term, TERMS[term], 1 + g % 3,
        )
        await db.execute(
            "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'fixture')",
            tag_id, f"a line about {term}",
        )


async def running_room(
    db, world, *, guests=0, budget_min=200, include_rewatches=True, wide=False
):
    """Wide by default: a two-title pool has no shortlist boundary. `wide` adds `widen`'s films."""
    if wide:
        await widen(db, world)
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
    state = await play.state_for(db, participant_id)
    if state["_pair"] is None:
        return None
    return await play.record_answer(
        db, participant_id=participant_id, pair=state["_pair"], answer=answer,
        seq=state["answered"] + 1, latency_ms=900,
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
    """§6.2 step 6: the pool is computed once at start; nothing re-ranks mid-evening."""
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
    """`start` must tell an ended room from a started one (decision 216): "already started" was false."""
    room = await open_room(db, world)
    await rooms.end_session(db, room["session_id"])

    with pytest.raises(play.RoundError) as ended:
        await play.start(db, room["session_id"])
    assert ended.value.reason == "no_room", "an evening that is over is not one that has begun"
    assert "ended" in str(ended.value), str(ended.value)

    # Both entry points answer the same question with the same status.
    with pytest.raises(rooms.RoomError) as joined:
        await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    assert joined.value.reason == "no_room"


async def test_a_pool_with_nothing_in_it_says_so_rather_than_serving_a_pair(db, world):
    """An empty pool is real on a small library with a tight budget."""
    room = await open_room(db, world, budget_min=60, include_rewatches=False)
    with pytest.raises(play.RoundError) as empty:
        await play.start(db, room["session_id"])
    assert empty.value.reason == "empty_pool"


async def test_every_answered_pair_writes_exactly_one_row_that_replays_the_round(db, world):
    """`A` must name `title_a`: an answer stored backwards looks identical in every count."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]

    served = []
    for _ in range(3):
        state = await play.state_for(db, seat)
        if state["_pair"] is None:
            break
        served.append((state["_pair"].title_a, state["_pair"].title_b, state["_pair"].selection))
        await play.record_answer(
            db, participant_id=seat, pair=state["_pair"], answer=rnd.A,
            seq=state["answered"] + 1, latency_ms=1234,
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
    """A drifting `answered_count` turns "3 of 4 finished" into a round nobody can end."""
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
    """§13 counts rows and §4.2 is append-only, so a replay is refused, as `api/rank.py` does."""
    # Wide, so a replay is not refused as `round_over`.
    room = await running_room(db, world, wide=True)
    seat = room["seats"][0]["id"]
    state = await play.state_for(db, seat)
    await play.record_answer(
        db, participant_id=seat, pair=state["_pair"], answer=rnd.A, seq=1, latency_ms=None
    )
    with pytest.raises(play.RoundError) as stale:
        await play.record_answer(
            db, participant_id=seat, pair=state["_pair"], answer=rnd.B, seq=1,
            latency_ms=None,
        )
    assert stale.value.reason == "stale_pair"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 1


async def test_an_answer_moves_the_participants_tilt(db, world):
    """Directional: the served pair moves only its terms, the chosen title outranks the rejected one
    under the tilt, and the opposite answer is the exact negation."""
    room = await running_room(db, world)
    chose_a, chose_b = room["seats"][0]["id"], room["seats"][1]["id"]
    assert await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", chose_a) == {}

    state = await play.state_for(db, chose_a)
    pair = state["_pair"]
    assert pair is not None and pair.selection == rnd.SELECTION_ADAPTIVE, (
        "a held-out answer moves no tilt by design (54b), so this claim needs an adaptive pair"
    )
    # Two seats answer independently against a frozen pool.
    await play.record_answer(
        db, participant_id=chose_a, pair=pair, answer=rnd.A, seq=1, latency_ms=900
    )
    await play.record_answer(
        db, participant_id=chose_b, pair=pair, answer=rnd.B, seq=1, latency_ms=900
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


async def test_a_round_ends_with_exactly_one_named_reason(db, world):
    """§14 risk 6 needs the rate of each reason, so it is stored."""
    room = await running_room(db, world)
    reason = await run_to_the_end(db, room["seats"][0]["id"])
    assert reason in rnd.END_REASONS


async def test_the_cap_ends_a_round_that_will_not_resolve(db, world):
    """EITHER to everything lifts the whole pool together, so the cap is the only exit."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    reason = await run_to_the_end(db, seat, answer=rnd.EITHER)
    answered = await db.fetchval(
        "SELECT answered_count FROM session_participant WHERE id = $1", seat
    )
    assert reason == rnd.CAP
    assert answered <= rnd.CAP_PAIRS
    # The eight-title fixture runs out of pairs first; the 20-pair bound is tested in
    # `test_tonight_round.py`.
    assert answered >= 6, "a round has to actually ask before it gives up"


async def test_two_participants_may_stop_at_different_pair_counts(db, world):
    """The round is per participant; ending both seats together would pass single-seat tests."""
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
    """Refused, not ignored: a control that silently does nothing is worse than none."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]

    with pytest.raises(play.RoundError) as early:
        await play.escape(db, seat)
    assert early.value.reason == "too_early"
    # The member reads the refusal, so it names the control by its label (decision 486).
    assert "just pick for us" in str(early.value) and "escape" not in str(early.value)

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
    """Asserted, not guarded by an `if`; and it closes again when the seat ends (finding 9)."""
    # A household-sized pool, so the shortlist is unsettled at pair five (decision 477).
    room = await running_room(db, world, wide=True)
    seat = room["seats"][0]["id"]
    assert (await play.state_for(db, seat))["escape_available"] is False

    for _ in range(rnd.ESCAPE_FROM_PAIR - 1):
        assert await answer_once(db, seat) is not None, "the round ended before 54c's escape opens"
    state = await play.state_for(db, seat)
    assert state["answered"] == rnd.ESCAPE_FROM_PAIR - 1, state
    assert state["escape_available"] is True

    ended = await play.escape(db, seat)
    assert ended["ended_by"] == rnd.ESCAPE
    assert ended["escape_available"] is False, "the control that ended the round still offers itself"
    after = await play.state_for(db, seat)
    assert after["escape_available"] is False, (
        "a seat that has ended still advertises the escape, which the route refuses with round_over"
    )
    assert rnd.escape_available(after["answered"]) is True, (
        "the count alone still says yes, so this asserts the ended_by half rather than the count's"
    )


async def test_a_participant_can_take_back_the_answer_they_just_gave(db, world):
    """Tombstone, not DELETE: §14 risk 6 logs every vote."""
    room = await running_room(db, world, wide=True)
    seat = room["seats"][0]["id"]
    # The last answer must be adaptive: 54b's arm moves no tilt, so undoing it proves nothing.
    # The arm is per seat (decision 223), so the rows are asked.
    answered = 0
    while answered < 2 or await _last_arm(db, seat) == rnd.SELECTION_HOLDOUT:
        assert await answer_once(db, seat) is not None, "the round ended before two answers"
        answered += 1
    tilt_before = await db.fetchval("SELECT tilt FROM session_participant WHERE id = $1", seat)

    out = await play.retract(db, seat)
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
        await play.retract(db, seat)
    assert nothing.value.reason == "nothing_to_undo"


async def test_a_finished_round_cannot_be_edited(db, world):
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await run_to_the_end(db, seat)
    with pytest.raises(play.RoundError) as over:
        await play.retract(db, seat)
    assert over.value.reason == "round_over"


async def test_the_waiting_payload_carries_counts_and_never_an_answer(db, world):
    """The payload cannot carry the answers, not merely that the UI declines to draw them."""
    room = await running_room(db, world, wide=True)
    first = room["seats"][0]["id"]
    await answer_once(db, first)
    await answer_once(db, first)

    seen = await play.progress(db, room["session_id"])
    assert [p["answered"] for p in seen] == [2, 0]

    # Over the KEYS, not the repr: "answered" contains "answer". Exhaustive on purpose.
    assert {k for p in seen for k in p} == {
        "participant_id", "seat", "name", "answered", "expected", "finished", "ended_by",
    }
    values = repr([list(p.values()) for p in seen])
    for leaked in ("EITHER", "NEITHER", "title", "tilt"):
        assert leaked not in values, f"the waiting payload carries {leaked}"


async def test_a_guest_cannot_answer_before_the_initiator_has_finished(db, world):
    """Without the refusal a guest's first tap lands in the middle of the host's round."""
    room = await running_room(db, world, guests=2)
    guest = next(s for s in room["seats"] if s["role"] == "guest")
    host = next(s for s in room["seats"] if s["role"] == "host")

    state = await play.state_for(db, guest["id"])
    with pytest.raises(play.RoundError) as early:
        await play.record_answer(
            db, participant_id=guest["id"], pair=state["_pair"], answer=rnd.A, seq=1,
            latency_ms=None,
        )
    assert early.value.reason == "not_your_turn"

    await run_to_the_end(db, host["id"])
    assert await answer_once(db, guest["id"]) is not None


async def test_only_one_guest_turn_is_open_at_a_time(db, world):
    """Two guests at once is one seat holding two people's answers."""
    room = await running_room(db, world, guests=2)
    host = next(s for s in room["seats"] if s["role"] == "host")
    guests = [s for s in room["seats"] if s["role"] == "guest"]
    await run_to_the_end(db, host["id"])

    assert await answer_once(db, guests[0]["id"]) is not None
    state = await play.state_for(db, guests[1]["id"])
    with pytest.raises(play.RoundError) as waiting:
        await play.record_answer(
            db, participant_id=guests[1]["id"], pair=state["_pair"], answer=rnd.A, seq=1,
            latency_ms=None,
        )
    assert waiting.value.reason == "not_your_turn"


async def test_a_guest_is_ranked_by_the_pools_order_and_never_a_members_ledger(db, world):
    """A guest's prior is the pool's member-average order, not one member's scores."""
    room = await running_room(db, world, guests=1)
    snapshot = await play.snapshot_of(db, room["session_id"])
    guest = next(s for s in room["seats"] if s["role"] == "guest")
    host = next(s for s in room["seats"] if s["role"] == "host")

    guest_prior = snapshot.member_average()
    host_prior = snapshot.pool_scores_for(host["id"])
    assert guest["id"] not in {p for s in snapshot.scores.values() for p in s}
    assert guest_prior != host_prior


async def finished_session(db, world, **kw):
    room = await running_room(db, world, **kw)
    for seat in room["seats"]:
        if seat["role"] == "guest":
            continue
        await run_to_the_end(db, seat["id"])
    return room


async def test_the_slate_is_persisted_rather_than_recomputed_on_read(db, world):
    """A slate re-derived later cannot be compared with the votes that produced it."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"])

    rows = await db.fetch(
        "SELECT title_id, rank, slot, group_score, per_user_match, conflict FROM session_result "
        "WHERE session_id = $1 ORDER BY rank",
        room["session_id"],
    )
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert sorted(r["title_id"] for r in rows if r["slot"] == "finalist") == sorted(slate.finalists)
    assert [r["title_id"] for r in rows if r["slot"] == "wildcard"] == [slate.wildcard]

    # It does not move when the Ledger underneath does.
    await db.execute("UPDATE user_score SET score = 0.01")
    again = await db.fetch(
        "SELECT title_id, rank FROM session_result WHERE session_id = $1 ORDER BY rank",
        room["session_id"],
    )
    assert [(r["title_id"], r["rank"]) for r in again] == [(r["title_id"], r["rank"]) for r in rows]


async def test_a_quiet_session_stores_no_conflict_at_all(db, world):
    """Made quiet, not hoped quiet: both seats answer EITHER and the two Ledgers are equal, since D
    is over the MEMBERS' scores (decision 218)."""
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

    slate = await play.finish(db, room["session_id"])

    assert slate.conflict is None, "nobody pulled against anybody"
    stored = await db.fetch(
        "SELECT conflict FROM session_result WHERE session_id = $1", room["session_id"]
    )
    assert stored, "a finished session has rows, or the assertion below is about nothing"
    assert all(r["conflict"] is None for r in stored)


async def test_every_participant_gets_a_match_line_naming_terms_the_title_carries(db, world):
    """A winner card shown under a reason it does not satisfy is the defect."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"])
    # The rank-1 row's own title: a surfaced split reorders `finalists`.
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
    """Enforced in the read: a payload carrying an approval is one request from being read."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"])
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
    slate = await play.finish(db, room["session_id"])
    first, second = room["seats"][0]["id"], room["seats"][1]["id"]
    target = slate.finalists[1]

    await ballot.submit(db, participant_id=first, approved=[target])
    await ballot.submit(db, participant_id=second, approved=[target])
    out = await ballot.resolve(db, room["session_id"])

    assert out["chosen_title_id"] == target, "approvals decide, not the group score"
    assert out["approval_share"] == pytest.approx(1.0)
    assert out["participants"] == 2


async def test_the_approval_share_is_approvals_over_participants_and_is_persisted(db, world):
    """Persisted: a share recomputed later would move with the code."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"])
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
    """Two devices revealing at once is normal; the share must not change between them."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"])
    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[slate.finalists[0]])

    first = await ballot.resolve(db, room["session_id"])
    second = await ballot.resolve(db, room["session_id"])
    assert first["chosen_title_id"] == second["chosen_title_id"]
    assert first["approval_share"] == pytest.approx(second["approval_share"])


async def test_a_ballot_may_only_name_titles_on_tonights_slate(db, world):
    """A title nobody was offered would land in §13's numbers."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"])
    seat = room["seats"][0]["id"]
    with pytest.raises(ballot.BallotError) as off_slate:
        await ballot.submit(db, participant_id=seat, approved=[8])
    assert off_slate.value.reason == "not_on_slate"


async def test_an_empty_ballot_is_an_answer_rather_than_a_silence(db, world):
    """An empty ballot is an answer, or the round never reveals."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"])
    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[])

    assert await ballot.everyone_submitted(db, room["session_id"]) is True
    out = await ballot.resolve(db, room["session_id"])
    assert out["approval_share"] == pytest.approx(0.0)


async def test_a_held_out_answer_is_stored_as_held_out_and_moves_no_tilt(db, world):
    """The tilt feeds the shortlist's score, so hold-out answers move none. The arm comes off the
    served pair, never from the client."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    pair = rnd.Pair(
        title_a=1, title_b=2, selection=rnd.SELECTION_HOLDOUT, reason="uniform-random",
    )
    await play.record_answer(
        db, participant_id=seat, pair=pair, answer=rnd.A, seq=1, latency_ms=None
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
    """A guard against the fixture: with no `dna_tag` rows the term check never runs."""
    room = await finished_session(db, world)
    await play.finish(db, room["session_id"])
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
    """No skip: a `pytest.skip` here reported a regression as green (finding 44). If the fixture stops
    dividing the household, fix the fixture."""
    room = await running_room(db, world)
    heavy_seat, light_seat = room["seats"][0]["id"], room["seats"][1]["id"]

    # Each seat picks the title matching its own pole, whichever side the pair puts it.
    snapshot = await play.snapshot_of(db, room["session_id"])
    for seat, want in ((heavy_seat, "dread"), (light_seat, "cosy")):
        for _ in range(rnd.CAP_PAIRS):
            state = await play.state_for(db, seat)
            if state["_pair"] is None or state["stop_reason"] is not None:
                break
            pair = state["_pair"]
            a_has = want in snapshot.dna.get(pair.title_a, {})
            b_has = want in snapshot.dna.get(pair.title_b, {})
            answer = rnd.A if a_has and not b_has else rnd.B if b_has and not a_has else rnd.EITHER
            await play.record_answer(
                db, participant_id=seat, pair=pair, answer=answer,
                seq=state["answered"] + 1, latency_ms=None,
            )
    for seat in (heavy_seat, light_seat):
        row = await db.fetchrow(
            "SELECT ended_by, answered_count FROM session_participant WHERE id = $1", seat
        )
        if row["ended_by"] is None and row["answered_count"] >= rnd.ESCAPE_FROM_PAIR - 1:
            await play.escape(db, seat)

    slate = await play.finish(db, room["session_id"])
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


async def test_the_shortlist_is_identical_with_the_held_out_answers_removed(db, world):
    """Through the write path: a combine reading every row would change the slate on a hold-out."""
    room = await running_room(db, world)
    seats = [s["id"] for s in room["seats"]]
    for seat in seats:
        await run_to_the_end(db, seat)
    # Written after the fact, so it cannot have steered the served pairs.
    snapshot = await play.snapshot_of(db, room["session_id"])
    ids = sorted(snapshot.title_ids)[:2]
    await db.execute(
        "INSERT INTO session_answer "
        "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
        "VALUES ($1, $2, 99, $3, $4, 'A', 'uniform_holdout')",
        room["session_id"], seats[0], ids[0], ids[1],
    )

    with_holdout = await play.finish(db, room["session_id"])
    await db.execute(
        "DELETE FROM session_answer WHERE session_id = $1 AND selection = 'uniform_holdout'",
        room["session_id"],
    )
    without = await play.finish(db, room["session_id"])

    assert with_holdout.finalists == without.finalists
    assert with_holdout.wildcard == without.wildcard
    assert [t for t, _ in with_holdout.ranked] == [t for t, _ in without.ranked]


from spielplan.tonight import solo  # noqa: E402


async def solo_picks(db, world, **kw):
    params = dict(
        user_id=world["patrick"], kind="movie", budget_min=200,
        include_rewatches=True, bundle_version=BUNDLE,
        # What the route passes (decision 223): solo mints no session row, so `user.id` is the stable key.
        holdout_key=str(world["patrick"]),
    )
    params.update(kw)
    return await solo.picks(db, **params)


async def test_solo_lands_on_three_picks_and_a_wildcard_with_no_round_first(db, world):
    """54f: straight to picks; the fastest path to a film must not be slower than Home."""
    out = await solo_picks(db, world)

    assert len(out["picks"]) == solo.PICKS
    assert out["wildcard"] is not None
    assert out["wildcard"]["title_id"] not in {p["title_id"] for p in out["picks"]}
    assert out["sharpened"] is False
    assert out["answered"] == 0


async def test_the_picks_are_the_persons_own_ledger_order_with_no_tilt(db, world):
    """Patrick's and Jenny's orders differ, so a household average would show."""
    mine = await solo_picks(db, world, user_id=world["patrick"])
    theirs = await solo_picks(db, world, user_id=world["jenny"])

    assert [p["title_id"] for p in mine["picks"]] != [p["title_id"] for p in theirs["picks"]]


async def test_every_pick_carries_a_why_and_a_budget_fit_line(db, world):
    """§6.8's one-line why is mandatory, and §6.2 step 8 fixes both fit-line branches."""
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
    """§6.4 makes the wildcard's label the honest half of a measured cost."""
    out = await solo_picks(db, world)

    assert out["wildcard"]["why"] == solo.STRETCH_WHY
    for card in out["picks"]:
        assert card["why"] != solo.STRETCH_WHY


async def test_a_why_line_only_names_terms_the_pick_carries(db, world):
    """The tilt may order the terms; it may never admit one."""
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
    """The tilted form replaces the other rather than joining it."""
    plain = await solo_picks(db, world, budget_min=130, include_rewatches=False)
    assert plain["provenance"] == "130 min budget · unseen first"


async def test_sharpening_re_ranks_in_place_and_changes_the_provenance_line(db, world):
    """54f: the provenance line reads 'tilted by your N answers' after sharpening."""
    # Rewatches included so the pool exceeds the shortlist. `sharpen=True`: it is now the only thing
    # that draws a pair (finding 35).
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
    """A random redraw from a ranked list repeats the top or degrades the picks."""
    first = await solo_picks(db, world)
    second = await solo_picks(db, world, offset=1)

    assert [p["title_id"] for p in first["picks"]] != [p["title_id"] for p in second["picks"]]


async def test_solo_leaves_no_session_row_and_publishes_no_room(db, world):
    """A session row would make solo joinable and pollute §13's approval share."""
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
    """A browse gesture, not an observation: it must not teach the Ledger."""
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
    """"The same pool": owned only, one kind, the soft budget, the rewatch setting."""
    out = await solo_picks(db, world, budget_min=130, include_rewatches=False)
    shown = {p["title_id"] for p in out["picks"]}
    if out["wildcard"]:
        shown.add(out["wildcard"]["title_id"])

    assert 8 not in shown, "not owned"
    assert 7 not in shown, "a series in a film session"
    assert 4 not in shown, "200 minutes is past 130 + 40"


async def test_an_empty_pool_says_what_to_change(db, world):
    """The honest answer names the two controls that would change it."""
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
    """The pool is never shown, so a sharpen card carries no score. No skip: the pair is asserted to
    exist first (finding 44)."""
    out = await solo_picks(db, world, sharpen=True)
    assert out["pair"] is not None, "the sharpen control has to ask something on this pool"
    for side in ("a", "b"):
        assert "scores" not in out["pair"][side]
        assert "group_score" not in out["pair"][side]


async def test_the_answer_after_an_undo_is_accepted(db, world):
    """Undo and carry on: the replacement takes a fresh seq (every row counts, tombstones included),
    so it cannot collide with the tombstone."""
    room = await running_room(db, world, wide=True)
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
    assert [r["seq"] for r in live] == [1, 3]
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 3, "and the tombstone is still there, because §14 risk 6 says log every vote"


async def test_a_guest_is_ranked_by_the_pools_order_rather_than_a_flat_prior(db, world):
    """A flat prior makes every candidate straddle at once: O(n²) pair search for no information."""
    room = await running_room(db, world, guests=1)
    snapshot = await play.snapshot_of(db, room["session_id"])
    guest = next(s for s in room["seats"] if s["role"] == "guest")

    beliefs = rnd.initial(snapshot.member_average(), prior_var=1.0, has_profile=False)
    means = {t: b.mu for t, b in beliefs.items()}
    pool_order = sorted(snapshot.member_average(), key=lambda t: -snapshot.member_average()[t])
    guest_order = sorted(means, key=lambda t: -means[t])

    assert guest_order == pool_order, "the guest is ranked by the pool's own order"
    assert len(set(means.values())) > 1, "a flat prior ranks nothing and straddles everything"
    # Still no member's Ledger: the pool average is not one person's scores.
    host = next(s for s in room["seats"] if s["role"] == "host")
    assert means != snapshot.pool_scores_for(host["id"])
    assert guest["id"] not in {p for s in snapshot.scores.values() for p in s}

async def test_two_seats_finishing_together_do_not_both_write_the_slate(db, world, pg_url):
    """Two final answers can both see `voting` and both combine; the loser's DELETE cannot see the
    winner's rows. Two real connections: in sequence it is correct."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    title_id = await db.fetchval("SELECT id FROM title LIMIT 1")

    winner = await asyncpg.connect(pg_url)
    try:
        # The winner's transaction, open and uncommitted: the state the loser's DELETE runs against.
        txn = winner.transaction()
        await txn.start()
        # Holding the session's lock with its slate written, via the module's own constant.
        await winner.execute(
            "SELECT pg_advisory_xact_lock($1, $2)", play._FINISH_LOCK, session_id
        )
        await winner.execute(
            "INSERT INTO session_result (session_id, title_id, rank, slot, group_score) "
            "VALUES ($1, $2, 1, 'finalist', 0.5)",
            session_id, title_id,
        )

        loser = asyncio.create_task(play.finish(db, session_id))
        await asyncio.sleep(0.3)
        assert not loser.done(), "the loser has to wait rather than write into the same slate"
        await txn.commit()

        # It must not raise: an unhandled UniqueViolationError was a 500 on the last answer.
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
    """The failure happens BETWEEN two statements, so a proxy stands there."""

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
    """The abandon is destructive, so it must not run before the fallible work. Forced by making every
    generated code already taken."""
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
    """Session row, seats and abandonment land together or not at all."""
    live = await open_room(db, world)
    before = await db.fetchval("SELECT count(*) FROM session")

    # Fail on the third seat: the session row and host seat are already written.
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
    """Driven through the real builder with a hand-made tilt: no seeded round produces every branch.
    The negative and neutral cases are pinned below the seat's median; the fourth is at its top."""
    room = await running_room(db, world)
    snapshot = await play.snapshot_of(db, room["session_id"])
    title_id = next(t for t, terms in snapshot.dna.items() if terms)
    carried = sorted(snapshot.dna[title_id])
    assert carried, "the title this test is about has to carry something"

    # The rows `finish` hands the builder; it reads `name` and `seat`.
    seats_sql = (
        "SELECT p.id, p.role, p.tilt, p.seat, u.name FROM session_participant p "
        "LEFT JOIN app_user u ON u.id = p.user_id WHERE p.session_id = $1 ORDER BY p.seat"
    )
    first = (await db.fetch(seats_sql, room["session_id"]))[0]["id"]
    others = [t for t in snapshot.title_ids if t != title_id]
    below = {first: {title_id: -1.0, **{t: float(i) for i, t in enumerate(others)}}}
    above = {first: {title_id: 99.0, **{t: float(i) for i, t in enumerate(others)}}}

    async def line_for(tilt, tonight):
        await db.execute("UPDATE session_participant SET tilt = $1 WHERE id = $2", tilt, first)
        seats = await db.fetch(seats_sql, room["session_id"])
        lines = await play._match_lines(
            db, snapshot=snapshot, seats=seats, title_id=title_id, tonight=tonight,
        )
        return lines[str(first)]

    pulled = await line_for({carried[0]: 1.0}, below)
    assert pulled["sign"] == "pull"
    assert carried[0] in pulled["line"], "the line names the term that earned it, by its label"
    assert "patrick" in pulled["line"], "and the person it is about"
    # In plain words: what this person's answers leaned toward.
    assert pulled["line"] == f"patrick leaned toward {carried[0]} tonight", pulled["line"]

    against = await line_for({t: -1.0 for t in carried}, below)
    assert against["sign"] == "against"
    assert "works against them" in against["line"], "§6.2 step 7's honest negative"
    assert against["line"].startswith("patrick: "), "whose negative it is, on a card with two"
    assert against["terms"] and against["terms"][0]["term"] in carried, (
        "and it names a term the title carries, not one chosen for contrast"
    )

    # Nothing the title carries moves this person either way.
    neutral = await line_for({t: 0.0 for t in carried}, below)
    assert neutral["sign"] == "neutral"
    assert neutral["terms"] == [], "no term reached either sign, so none is named"
    assert "works against them" not in neutral["line"]
    assert neutral["line"], "a participant is never omitted"

    # Top of the seat's own order: the negative would be false, so it names what the title carries.
    favourite = await line_for({t: -1.0 for t in carried}, above)
    assert favourite["sign"] == "pull", favourite
    assert "works against them" not in favourite["line"]
    assert {t["term"] for t in favourite["terms"]} <= set(carried)
    # Claims only what the branch establishes; never "leaned toward".
    assert favourite["line"].startswith("suits patrick's usual taste — "), favourite["line"]
    assert "leaned" not in favourite["line"] and "+" not in favourite["line"]


from spielplan.tonight import dna as tonight_dna  # noqa: E402


async def test_a_projection_never_outranks_a_quote_verified_tag_on_this_surface(db, world):
    """Title 2 carries `dread` quote-verified (salience 2) and projected from 8 sources; the
    quote-verified reading must win. Under `0.30 * n_sources` the projection reads 2.40."""
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


# `play.settle` owns the `voting -> ballot` transition and `rooms.invite` the push fan-out; the
# route half is in `test_tonight_routes.py`.

from spielplan.tonight import result  # noqa: E402


async def test_a_room_whose_every_seat_has_finished_is_settled_by_a_read(db, world):
    """`finished_session` leaves every seat ended, the room in `voting` and no slate: what a dropped
    connection or a restart mid-request leaves."""
    room = await finished_session(db, world)
    session_id = room["session_id"]

    assert await play.everyone_finished(db, session_id) is True
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_VOTING
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) == 0, "the votes are in and there is nothing to read them with"

    assert await play.settle(db, session_id) is True, "this call is what moved the room"

    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_BALLOT
    slate = await ballot.slate_of(db, session_id)
    assert [r["slot"] for r in slate].count(combine.SLOT_WILDCARD) == 1
    assert 1 <= sum(1 for r in slate if r["slot"] == combine.SLOT_FINALIST) <= 3


async def test_settling_a_room_twice_leaves_the_one_slate(db, world):
    """Every read calls `settle`, so a second caller is ordinary; the boolean says whose transition."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    assert await play.settle(db, session_id) is True
    first = [
        tuple(r) for r in await db.fetch(
            "SELECT title_id, rank, slot FROM session_result WHERE session_id = $1 ORDER BY rank",
            session_id,
        )
    ]

    assert await play.settle(db, session_id) is False, "the room has already moved on"

    again = [
        tuple(r) for r in await db.fetch(
            "SELECT title_id, rank, slot FROM session_result WHERE session_id = $1 ORDER BY rank",
            session_id,
        )
    ]
    assert again == first, "one slate, whoever reads the room"


async def test_a_room_that_is_still_answering_is_not_settled_by_a_read(db, world):
    """The reveal is simultaneous, so a read may not close a room one seat is still playing."""
    room = await running_room(db, world)
    session_id = room["session_id"]
    await run_to_the_end(db, room["seats"][0]["id"])

    assert await play.settle(db, session_id) is False
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_VOTING
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) == 0


async def test_the_invitation_asks_every_member_even_when_one_send_fails(db, world):
    """The sender is a callable, so the rule is asserted without ASGI; one failed send must not skip
    the next member."""
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
    # The room's own join link (decision 481).
    assert payload["url"] == f"/tonight?room={room['room_code']}"
    assert room["room_code"] in payload["body"]


async def test_the_reveal_is_assembled_where_the_other_tonight_rules_are(db, world):
    """Runners-up are titles that RAN, by `rank` ascending; the pool's tail is not a runner-up."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    await play.settle(db, session_id)
    slate = await ballot.slate_of(db, session_id)
    # The wildcard alone approved, so approvals and rank disagree.
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
    assert card["wildcard"] is None, "the wildcard won, so the winner card is its one place"
    order = [(-c["approvals"], c["rank"]) for c in card["runners_up"]]
    assert order == sorted(order), "most approved first, then the closest on rank"
    assert card["winner"]["fit_line"], "the budget fit line the reveal prints"
    assert card["winner"]["kind"] == "movie", "a runtime means nothing without the kind"


class _CountsTheRowsFetched:
    """Only `session_result` reads: the per-seat `session_ballot` reads would count people."""

    def __init__(self, conn):
        self._conn, self.rows = conn, 0

    async def fetch(self, *args, **kw):
        out = await self._conn.fetch(*args, **kw)
        if "session_result" in str(args[0] if args else kw.get("query", "")):
            self.rows += len(out)
        return out

    def __getattr__(self, name):
        return getattr(self._conn, name)


async def test_the_reveal_reads_the_slate_rather_than_the_whole_pool(db, world):
    """`session_result` holds the POOL; the reveal filters to the slate in SQL. Counted, not timed."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    await play.settle(db, session_id)
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


# `start` claims the room first so no join slips in during the pool build; a seat the round cannot
# ask ends itself (finding 6); an unrankable member is named (decision 216).

import json  # noqa: E402


async def second_connection(pg_url):
    """`conftest.db`'s JSON codec: `play.start` passes a dict, which a bare connection refuses."""
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


class _SlowPoolBuild:
    """Widens the join/start window to 0.6 s so the race is deterministic. Keyed on `t.is_owned`,
    not `user_score`, which the unscored-member check also reads."""

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
    """Whichever of the two wins, every seated member is in the frozen snapshot."""
    room = await open_room(db, world)
    session_id = room["session_id"]
    starter = await second_connection(pg_url)
    joiner = await second_connection(pg_url)
    slow = _SlowPoolBuild(starter)

    async def claim_it():
        return await play.start(slow, session_id)

    async def sit_down():
        # Inside the build window.
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
    """One transaction: a refused start rolls the claim back, and the waiting join proceeds."""
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

    # A wider budget starts it, with both seats in the pool.
    await db.execute("UPDATE session SET runtime_budget_min = 200 WHERE id = $1", session_id)
    await play.start(db, session_id)
    snapshot = await play.snapshot_of(db, session_id)
    carried = {pid for seat_scores in snapshot.scores.values() for pid in seat_scores}
    assert len(carried) == 2, "the member who joined while the refusal rolled back is in the pool"


async def test_a_seat_that_can_never_be_asked_ends_itself(db, world):
    """A seat with no scores converges at zero answers with no pair, so no write path could end it.
    `ended_by` stays `converged`: 0013's CHECK admits three values (decision 215)."""
    room = await running_room(db, world)
    session_id = room["session_id"]
    late = await insert_user(db, "mia")
    orphan = await db.fetchval(
        "INSERT INTO session_participant (session_id, user_id, role, seat) "
        "VALUES ($1, $2, 'member', 9) RETURNING id",
        session_id, late,
    )
    snapshot = await play.snapshot_of(db, session_id)
    assert snapshot.pool_scores_for(orphan) == {}, "the fixture is the state finding 5 leaves"

    state = await play.state_for(db, orphan)

    assert state["pair"] is None and state["stop_reason"] == rnd.CONVERGED
    assert state["ended_by"] == rnd.CONVERGED, "the payload reports the seat as ended"
    row = await db.fetchrow(
        "SELECT ended_by, converged_at FROM session_participant WHERE id = $1", orphan
    )
    assert row["ended_by"] == rnd.CONVERGED, "and the row says so, so the room can close"
    assert row["converged_at"] is not None, "0013 ties the timestamp to the reason"

    # Reading again is not a second ending.
    await play.state_for(db, orphan)
    assert await play.settle(db, session_id) is False
    for seat in room["seats"]:
        await run_to_the_end(db, seat["id"])
    assert await play.settle(db, session_id) is True


@pytest.mark.parametrize(
    "budget_min,include_rewatches,candidates", [(70, False, 3), (60, True, 2)]
)
async def test_a_pool_too_small_for_a_round_reaches_the_ballot(
    db, world, budget_min, include_rewatches, candidates
):
    """Decision 215: two or three candidates go to the ballot. Admission is unchanged: refusing below
    four would deny a small household any evening."""
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
        state = await play.state_for(db, seat)
        assert state["pair"] is None, "there is no shortlist boundary to resolve"
        assert state["ended_by"] == rnd.CONVERGED

    assert await play.settle(db, session_id) is True
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == rooms.STATE_BALLOT
    slate = await ballot.slate_of(db, session_id)
    assert len(slate) == candidates, "every candidate is on the ballot when there are this few"


async def test_a_refusal_names_the_member_the_pool_cannot_rank(db, world):
    """An unscored member empties the pool; the refusal names them, not the budget."""
    room = await open_room(db, world, budget_min=200, include_rewatches=True)
    unscored = await insert_user(db, "mia")
    await rooms.join(db, session_id=room["session_id"], user_id=unscored)

    with pytest.raises(play.RoundError) as refused:
        await play.start(db, room["session_id"])

    assert refused.value.reason == "unscored_member"
    assert "mia" in str(refused.value), "the one fact the household can act on"
    assert "widen the budget" not in str(refused.value)
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", room["session_id"]
    ) == rooms.STATE_OPEN, "and the room is still joinable, so the fold-in can catch up"

    # Any score for this bundle is all the check needs (decision 216).
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
    """409, not 422: the request is well formed and the world is not ready. Named in the mapping."""
    from spielplan.api import tonight as tonight_api

    mapped = tonight_api._room_error(play.RoundError("unscored_member", "mia has no scores yet"))
    assert mapped.status_code == 409
    assert mapped.detail["reason"] == "unscored_member"
    assert "mia" in mapped.detail["message"]


class _EscapesMidEnding:
    """A read that ends a converged seat races an escape tap inside `_end`'s statement."""

    def __init__(self, conn, *, participant_id):
        self._conn = conn
        self._participant_id = participant_id
        self.armed = True

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query, *args, **kw):
        # `fetchval`: `_end` reads its own `RETURNING id`. `armed` catches a window that stopped opening.
        if self.armed and "SET ended_by" in query:
            self.armed = False
            await self._conn.execute(
                "UPDATE session_participant SET ended_by = 'escape' WHERE id = $1",
                self._participant_id,
            )
        return await self._conn.fetchval(query, *args, **kw)


async def test_the_first_ending_recorded_is_the_one_that_stands(db, world):
    """The first ending stands: last-write-wins would report an escape as a convergence. The loser
    writes nothing, `converged_at` included."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    ended = await run_to_the_end(db, seat)
    assert ended is not None, "the fixture needs a seat whose round is over"
    await db.execute(
        "UPDATE session_participant SET ended_by = NULL, converged_at = NULL WHERE id = $1", seat
    )

    racing = _EscapesMidEnding(db, participant_id=seat)
    state = await play.state_for(racing, seat)

    assert racing.armed is False, "the window never opened, so nothing was raced"
    row = await db.fetchrow(
        "SELECT ended_by, converged_at FROM session_participant WHERE id = $1", seat
    )
    assert row["ended_by"] == rnd.ESCAPE, "the writer that arrived first is the one that counts"
    assert row["converged_at"] is None, "and nothing of the losing write landed either"
    # The payload reports this read's own reason; the row is what §14 risk 6 counts.
    assert state["ended_by"] == state["stop_reason"]


# Every write guard was a check-then-act. The checks now run inside the transaction behind
# `FOR UPDATE OF p`, the seq is minted from all rows, and the ballot takes an advisory lock.
# These tests need two connections and a widened window (`_SlowSnapshot`).


import time  # noqa: E402


class _SlowSnapshot:
    """Delays the one read between the seq guard and the INSERT. `waited` times the locked seat read,
    proving the two were in flight together and not in sequence."""

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
    """Exactly one waited: the repair, and proof they overlapped."""
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
    """The counter displays live rows, and the highest seq issued is never behind it."""
    assert len(live) == counted, f"answered_count {counted} against live seqs {live}"
    assert max(every, default=0) >= counted, f"highest seq {every} behind answered_count {counted}"
    assert len(set(every)) == len(every), f"a seq was reused: {every}"


async def test_two_answers_on_one_card_are_one_200_and_one_409(db, world, pg_url):
    """The loser waits, re-reads and refuses on `stale_pair`: still a 409, with the round's reason."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    state = await play.state_for(db, seat)
    pair, seq = state["_pair"], state["answered"] + 1

    first_conn = _SlowSnapshot(await second_connection(pg_url))
    second_conn = _SlowSnapshot(await second_connection(pg_url))

    async def tap(conn, answer):
        return await play.record_answer(
            conn, participant_id=seat, pair=pair, answer=answer, seq=seq, latency_ms=900,
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
    """`retract` recounted around its own tombstone, leaving the counter behind the highest seq."""
    room = await running_room(db, world, wide=True)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    state = await play.state_for(db, seat)
    pair, seq = state["_pair"], state["answered"] + 1

    answering = _SlowSnapshot(await second_connection(pg_url))
    undoing = _SlowSnapshot(await second_connection(pg_url))
    try:
        outcomes = await asyncio.gather(
            play.record_answer(
                answering, participant_id=seat, pair=pair, answer=rnd.A, seq=seq,
                latency_ms=900,
            ),
            play.retract(undoing, seat),
            return_exceptions=True,
        )
    finally:
        await answering.close()
        await undoing.close()

    for outcome in outcomes:
        # Either ordering is correct; neither may be a database error.
        assert not isinstance(outcome, asyncpg.PostgresError), f"the seat wedged: {outcome!r}"
        if isinstance(outcome, BaseException):
            assert isinstance(outcome, play.RoundError), repr(outcome)
            assert outcome.reason == "stale_pair", outcome.reason
    _sound(*await _seat_rows(db, seat))
    _one_of_them_waited(answering, undoing)

    # The wedge was the state left behind, so the seat must keep playing.
    ended = await db.fetchval("SELECT ended_by FROM session_participant WHERE id = $1", seat)
    assert ended is None, "neither a gathered answer nor a gathered undo ends a round"
    for _ in range(3):
        if await answer_once(db, seat) is None:
            break
        _sound(*await _seat_rows(db, seat))


async def test_a_replacement_answer_takes_a_fresh_seq_rather_than_the_tombstones(db, world):
    """The counter counts live rows; the seq is minted from every row there has ever been."""
    room = await running_room(db, world, wide=True)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    await play.retract(db, seat)

    written = await answer_once(db, seat)
    counted, live, every = await _seat_rows(db, seat)
    _sound(counted, live, every)
    assert every == [1, 2, 3], "the tombstone keeps its seq and the replacement takes the next"
    assert live == [1, 3]
    assert counted == 2, "answered_count is what the waiting screen displays, nothing more"
    assert written["seq"] == 3, "and the caller is told the seq that was actually written"


async def test_the_counter_cannot_be_moved_past_the_rows_it_counts(db, world):
    """In the statement, not the callers: a caller's check is one another caller omits."""
    room = await running_room(db, world)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)

    with pytest.raises(AssertionError):
        await play._set_answered(db, seat, count=5, tilt={})
    assert await db.fetchval(
        "SELECT answered_count FROM session_participant WHERE id = $1", seat
    ) == 1, "the refused write left the counter alone"


async def test_two_ballot_submissions_from_one_seat_are_both_recorded(db, world, pg_url):
    """54e: re-submitting replaces, even when two submits overlap; the loser waits for the lock."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    slate = await play.finish(db, session_id)
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
    # One of the two whole ballots, never a mixture.
    assert approved in (sorted(one), sorted(two)), f"a half-written ballot: {approved}"


class _SlowTally:
    """Delays AFTER the tally query, so a re-submit can land between the count and the stored outcome."""

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
    """`submit`'s lock is per participant and never meets `resolve`, so a re-submit could land between
    tally and INSERT. The assertion is agreement between outcome and ballot rows, not the winner."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    slate = await play.finish(db, session_id)
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
        # Inside the tally window.
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


# The pair search runs off the loop, and a tap replays once. A patched replay blocks its own
# thread with `time.sleep`; the assertions are mechanisms (a coroutine run mid-search, timer
# gaps, decode and replay counts), not the real selector's duration.


import contextlib  # noqa: E402
import dataclasses  # noqa: E402


async def _watch_the_loop(gaps):
    """A 10 ms timer resumed after 600 ms was not scheduled at all: one frame owned the thread."""
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
    """Patched where `play` looks them up, so the tally is of what the code did. Counting, not timing:
    two fast replays are still twice the work."""
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
    """THE DEADLOCK IS THE ASSERTION: `run_coroutine_threadsafe` from a replay on the loop can never
    be served, and from a thread it is served at once."""
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
    """Two real accounts on one ASGI app: `world`'s admin has no obtainable cookie. 120 films, so a
    round still runs at pair six on the rank-standardised scale (decision 477)."""
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
        SELECT g, 'movie', 'Film ' || g, 2010, 95 + g, true FROM generate_series(1, 120) AS g
        """
    )
    for user_id in [r["id"] for r in await db.fetch("SELECT id FROM app_user ORDER BY id")]:
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "SELECT $1, g, 'movie', $2, 0.9 - 0.005 * g, 0.0 FROM generate_series(1, 120) AS g",
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
    """The pair search is pure CPU on the one event loop, so it stalled the other phone and health."""
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
    """Counted, not timed: two decodes and two replays per tap are wrong at any speed."""
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
    """The card is now built from the write's own replay, so each answer posts the previous seal and
    every field is compared with a fresh read. Three taps: short of the hold-out slot."""
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
    """The replay runs after the commit, so an undo can land in between; `_end`'s `when_answered`
    refuses the stale ending. Fired from inside the search on a second connection."""
    room = await running_room(db, world, wide=True)
    seat = room["seats"][0]["id"]
    await answer_once(db, seat)
    await answer_once(db, seat)
    state = await play.state_for(db, seat)
    pair, seq = state["_pair"], state["answered"] + 1

    loop = asyncio.get_running_loop()
    real = rnd.replay
    undoing = await second_connection(pg_url)
    calls: list[int] = []

    def undoes_the_answer_it_is_replaying(*args, **kw):
        calls.append(1)
        if len(calls) > 1:
            # The undo's own replay must be the real one.
            return real(*args, **kw)
        asyncio.run_coroutine_threadsafe(play.retract(undoing, seat), loop).result(timeout=5)
        return dataclasses.replace(real(*args, **kw), next_pair=None, stop_reason=rnd.CAP)

    monkeypatch.setattr(rnd, "replay", undoes_the_answer_it_is_replaying)
    try:
        written = await play.record_answer(
            db, participant_id=seat, pair=pair, answer=rnd.A, seq=seq, latency_ms=900,
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
    # The next tap has to work.
    assert await answer_once(db, seat) is not None, "the seat cannot be answered any more"
    _sound(*await _seat_rows(db, seat))


async def test_an_undo_during_a_read_of_the_round_leaves_the_seat_playing(
    db, world, pg_url, monkeypatch
):
    """`state_for` ends stranded seats and its replay is off the loop, so it needs `when_answered`
    too: refusing costs one read, ending on a stale reason costs the round."""
    room = await running_room(db, world, wide=True)
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
        asyncio.run_coroutine_threadsafe(play.retract(undoing, seat), loop).result(timeout=5)
        return dataclasses.replace(real(*args, **kw), next_pair=None, stop_reason=rnd.CAP)

    monkeypatch.setattr(rnd, "replay", undoes_the_answer_it_is_replaying)
    try:
        seen = await play.state_for(db, seat)
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

    # The next read replays the real answers and serves a pair again.
    again = await play.state_for(db, seat)
    assert again["_pair"] is not None and again["ended_by"] is None, again
    assert await answer_once(db, seat) is not None, "the seat cannot be answered any more"
    _sound(*await _seat_rows(db, seat))


async def test_the_combine_does_not_hold_the_loop_either(db, world, monkeypatch):
    """`finish` replays once per seat on the answer that ends the room; the tally shows it is per seat."""
    room = await finished_session(db, world)
    seats = await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1", room["session_id"]
    )
    tally = _count_the_work(monkeypatch, replay_delay=0.3)

    async with _loop_gaps() as gaps:
        await play.finish(db, room["session_id"])

    assert tally["replays"] == seats, f"one replay per seat, not {tally['replays']} for {seats}"
    assert tally["snapshots"] == 1, "and one decode of the frozen pool for the whole combine"
    assert max(gaps) < 0.2, (
        f"the combine held the loop for {max(gaps):.3f}s over {tally['replays']} seats"
    )


async def test_the_combine_does_not_search_for_a_pair_it_will_never_show(db, world, monkeypatch):
    """`finish` reads beliefs only, so it passes `select=False`. The second run with the flag forced
    on must search and give the identical slate, keeping the first assertion non-vacuous."""
    # Wide, so one answer leaves a boundary to straddle (decision 477).
    room = await running_room(db, world, wide=True)
    for seat in room["seats"]:
        await answer_once(db, seat["id"])
        # The escape written directly: `play.escape` refuses before pair six.
        await db.execute(
            "UPDATE session_participant SET ended_by = 'escape' WHERE id = $1", seat["id"]
        )
    _, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    slate = await play.finish(db, room["session_id"])
    assert calls == [], f"the combine ran {len(calls)} pair searches for pairs nobody is shown"

    real = rnd.replay
    monkeypatch.setattr(rnd, "replay", lambda *a, **kw: real(*a, **{**kw, "select": True}))
    again = await play.finish(db, room["session_id"])

    assert calls, "no seat had a boundary left to search, so the assertion above proved nothing"
    assert again.rows == slate.rows, "the flag moved the slate, and it may only move the cost"


# Decision 169: a host-only End for a room that will never settle, as `abandoned` rather than a
# DELETE (§14 risk 6 reads those rows). Through the routes: an ended room must stop serving.


async def _answers_twice(client, seat):
    """Two answers through the real routes, so the room the host ends has votes in it."""
    for _ in range(2):
        card = await _round_card(client, seat)
        assert card["card_token"], card
        answered = await _tap(client, seat, card)
        assert answered.status_code == 200, answered.text


async def test_the_host_can_end_a_started_room_and_a_member_cannot(household, db):
    """Checked at the route, like `start`: the host is a column on `session`."""
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
    """The list is every device's, which made the defect household-wide."""
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
    """§14 risk 6: the control ends the evening; it does not unhappen it."""
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
    """The card is taken BEFORE the room ends: a token minted while live is not a way back in."""
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

    # The member's own seat must be refused too.
    mine = await household["member"].get(f"/api/tonight/seats/{theirs}/round")
    assert mine.status_code == 404, mine.text


async def test_ending_a_room_the_household_already_resolved_is_refused(db, world):
    """`FOR UPDATE` before `set_state`, so a second End waits and is refused on what committed."""
    room = await finished_session(db, world)
    slate = await play.finish(db, room["session_id"])
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
    """`finish` moved the room with a bare `set_state`, reviving an ended room. The End is fired from
    inside the combine's replay: the real interleaving."""
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
        settled = await play.settle(db, session_id)
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
        await play.state_for(db, room["seats"][0]["id"])
    assert refused.value.reason == "no_room", "the seats of an ended room stay unserved"


# Through the routes with the arm as it ships: the seal is measured where tokens are minted. Ids
# restart per test (`conftest.db` recreates the schema), so the arm is deterministic here.

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
    """The hold-out pair for a seat at a count is one pair on every read; it used to re-roll per GET."""
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

    # The token names that pair and no other, and the row records it (§4.2 append-only).
    assert (await _tap(host, seat, card)).status_code == 200
    row = await db.fetchrow(
        "SELECT title_a, title_b, selection FROM session_answer WHERE participant_id = $1 "
        "ORDER BY seq DESC LIMIT 1",
        seat,
    )
    assert (row["title_a"], row["title_b"]) == _pair_ids(card)
    assert row["selection"] == rnd.SELECTION_HOLDOUT


async def test_the_card_a_phone_stashed_is_the_card_the_undo_re_issues(household, db):
    """After an undo the re-issued token is byte-identical to the stashed one (decision 223)."""
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
    """The arm is drawn from the seat id, so solo can re-derive it (decision 223)."""
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


# 54f: solo is "the fastest path to a film", so taps must not pay for searches they do not show.


def _counting(module, name):
    """Patches `_select_pair`: `round.replay` binds the selector under a private alias, so patching
    `round.select` would observe nothing."""
    original = getattr(module, name)
    calls: list[tuple] = []

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    return original, counted, calls


async def test_the_solo_door_lands_on_picks_without_running_the_pair_search(
    db, world, monkeypatch
):
    """The call count, not the clock: a six-film fixture is fast either way."""
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
    """Reshuffle draws no pair, so it may not pay for one (finding 35)."""
    _, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    out = await solo_picks(db, world, offset=1)

    assert calls == []
    assert out["pair"] is None
    assert len(out["picks"]) == solo.PICKS


async def test_only_an_explicit_sharpen_draws_a_pair(db, world, monkeypatch):
    _, counted, calls = _counting(rnd, "_select_pair")
    monkeypatch.setattr(rnd, "_select_pair", counted)

    out = await solo_picks(db, world, sharpen=True)

    assert len(calls) == 1, "one tap, one search"
    assert out["pair"] is not None, "the control that exists to ask has to ask"
    assert "scores" not in out["pair"]["a"]


async def test_the_replay_still_runs_when_the_door_does_not_select(db, world):
    """The door skips the SELECTION, not the replay: answers must still re-rank the picks."""
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
    """`3 * offset % 3` was zero on a pool of four. Built as solo builds it, one seat: at 170 min
    Patrick's pool is titles 1-4."""
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
    # PER PRESS: on a pool not a multiple of three the wrap-fill fires before the modulus, and
    # `wrapped` must say so on that press (decision 222).
    assert picked[1][1:] == picked[0][:2], (
        f"press 1 is meant to wrap the top of the ranking back in: {picked}"
    )
    assert [out["wrapped"] for out in presses] == [False, True, True], (
        f"the line decision 222 renders is missing on the press that wraps: {picked}"
    )


async def test_every_title_in_a_seven_title_pool_is_reachable_as_a_pick(db, world):
    """64 presses: `SoloBody.offset`'s own bound."""
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
    """Solo must skip an answer whose titles left the pool, as `round.replay` does. Title 8 is unowned."""
    sent = [rnd.Answered(seq=1, title_a=1, title_b=8, answer=rnd.A)]

    out = await solo_picks(db, world, answers=sent)

    assert out["tilt"] == {}, "an answer the replay ignored moved the tilt"
    assert out["sharpened"] is False
    assert "tilted by your" not in out["provenance"], out["provenance"]
    # `answered` is not filtered: an answered pair still costs one of twenty.
    assert out["answered"] == 1


async def test_an_answer_inside_the_pool_still_counts(db, world):
    """The guard is pool membership, never "carries DNA"."""
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
    """One title named twice is not two candidates; `tilt.applies` and `round.replay` both said it was.
    Only solo's client-supplied answers can send it."""
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
    # The filtered number is what tilted; `answered` is what they answered.
    assert out["answered"] == 1


async def test_a_series_session_says_its_budget_is_per_episode(db, world):
    """Decision 219: on a series night the budget is per episode. Title 7 runs 45 min per episode."""
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


# The 2026-09-25 household evening (decisions 477-481).

from spielplan.tonight import copy as copy_rules  # noqa: E402

LABELS = {"dread": "a sense of menace", "cosy": "snug", "relentless": "breathless",
          "patient": "unhurried"}


async def test_solo_and_the_reveal_speak_in_term_labels_and_never_ids(db, world):
    """Labels share no word with their ids, so an id reaching a line cannot hide (decision 486)."""
    for term, label in LABELS.items():
        await db.execute("UPDATE dna_term SET label = $1 WHERE term = $2", label, term)

    out = await solo_picks(db, world)
    for card in out["picks"]:
        for term in card["terms"]:
            assert term["label"] == LABELS[term["term"]]
            assert term["label"] in card["why"]
            assert term["term"] not in card["why"], f"an id reached a why-line: {card['why']}"

    room = await finished_session(db, world)
    await play.finish(db, room["session_id"])
    lines = [
        line
        for row in await db.fetch(
            "SELECT per_user_match FROM session_result WHERE session_id = $1 "
            "AND slot IN ('finalist', 'wildcard')",
            room["session_id"],
        )
        for line in row["per_user_match"].values()
    ]
    named = [t for line in lines for t in line["terms"]]
    assert named, "the fixture names no term on the slate, so this is vacuous"
    for line in lines:
        for term in line["terms"]:
            assert term["label"] in line["line"] and term["term"] not in line["line"], line


async def test_the_round_prior_and_the_combine_read_the_standardised_scores(db, world):
    """Decision 477: every Tonight read of a member is rank-standardised over the frozen pool."""
    room = await running_room(db, world, guests=1)
    snapshot = await play.snapshot_of(db, room["session_id"])
    host = next(s for s in room["seats"] if s["role"] == "host")
    member = next(s for s in room["seats"] if s["role"] == "member")

    stored = await db.fetchval(
        "SELECT context -> 'pool' ->> 'scale' FROM session WHERE id = $1", room["session_id"]
    )
    assert stored == pool.SCALE_MARKER, "the rule is frozen with the pool it applies to"

    raw_host = {t: s[host["id"]] for t, s in snapshot.scores.items()}
    assert snapshot.pool_scores_for(host["id"]) == pytest.approx(pool.rank_normal(raw_host))
    raw_member = {t: s[member["id"]] for t, s in snapshot.scores.items()}
    expected_avg = {
        t: (pool.rank_normal(raw_host)[t] + pool.rank_normal(raw_member)[t]) / 2
        for t in raw_host
    }
    assert snapshot.member_average() == pytest.approx(expected_avg), (
        "a guest's prior is the plain average on the room's scale, never the widest Ledger"
    )
    assert snapshot.member_ledger() == {
        t: [pytest.approx(v) for v in s.values()] for t, s in snapshot.ledger.items()
    }
    assert snapshot.scores[1][host["id"]] == pytest.approx(0.50), "the raw read is kept"


async def test_a_room_started_before_the_marker_keeps_its_raw_scale(db, world):
    """A deploy must not move an evening in flight: no marker reads raw; an unknown marker is refused."""
    room = await running_room(db, world)
    await db.execute(
        "UPDATE session SET context = context #- '{pool,scale}' WHERE id = $1", room["session_id"]
    )
    legacy = await play.snapshot_of(db, room["session_id"])
    assert legacy.scale is None
    assert legacy.ledger == legacy.scores

    await db.execute(
        "UPDATE session SET context = jsonb_set(context, '{pool,scale}', '\"rank_normal_sd9\"') "
        "WHERE id = $1",
        room["session_id"],
    )
    with pytest.raises(play.RoundError) as unknown:
        await play.snapshot_of(db, room["session_id"])
    assert unknown.value.reason == "no_room"


async def _no_axes(db):
    """Release data ships no axis artifact (decision 173)."""
    await db.execute("DELETE FROM dna_axis_weight")
    await db.execute("DELETE FROM dna_axis")


async def _disjoint_household(db, world):
    """Nine films; the plain top three are Patrick's own top three and none of Jenny's."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, runtime_min, is_owned) VALUES "
        "(20, 'movie', 'X', 2011, 100, true), (21, 'movie', 'Y', 2011, 100, true), "
        "(22, 'movie', 'Z', 2011, 100, true)"
    )
    orders = {
        world["patrick"]: {1: .9, 2: .8, 3: .7, 4: .6, 5: .5, 6: .4, 22: .3, 21: .2, 20: .1},
        world["jenny"]: {20: .9, 21: .8, 22: .7, 1: .6, 2: .5, 3: .4, 4: .3, 5: .2, 6: .1},
    }
    for user_id, scores in orders.items():
        for title_id, value in scores.items():
            await db.execute(
                "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
                "VALUES ($1, $2, 'movie', $3, $4, 0.0) ON CONFLICT (user_id, title_id) "
                "DO UPDATE SET score = EXCLUDED.score",
                user_id, title_id, BUNDLE, value,
            )


async def test_the_person_reservation_is_persisted_and_labelled_with_the_member(db, world):
    """Decision 479: Jenny's pick is `reserved_for` her seat, never `reserved`. Decision 486 gates D."""
    await _no_axes(db)
    await _disjoint_household(db, world)
    room = await running_room(db, world)
    jenny_seat = next(s["id"] for s in room["seats"] if s["role"] == "member")

    slate = await play.finish(db, room["session_id"])
    assert slate.reserved_for == {20: jenny_seat}
    rows = {
        r["title_id"]: r for r in await db.fetch(
            "SELECT title_id, slot, reserved, reserved_for, conflict FROM session_result "
            "WHERE session_id = $1",
            room["session_id"],
        )
    }
    assert rows[20]["slot"] == "finalist" and rows[20]["reserved_for"] == jenny_seat
    assert not any(r["reserved"] for r in rows.values()), "never the axis counterweight's flag"
    assert rows[20]["conflict"]["headline"] == copy_rules.PERSON_SPLIT_LINE

    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[20])
    counted = await ballot.tally(db, room["session_id"])
    outcome = await ballot.resolve(db, room["session_id"])
    member_view = await result.slate(db, room["session_id"], counted, outcome)
    card = next(c for c in member_view["finalists"] + [member_view["winner"]] if c["title_id"] == 20)
    assert card["reserved_for"]["name"] == "jenny"
    assert card["reserved"] is False
    assert "d" not in card["conflict"] and card["conflict"]["explanation"] == copy_rules.D_LINE_PLAIN

    modelled = await result.slate(db, room["session_id"], counted, outcome, show_model=True)
    shown = next(c for c in modelled["finalists"] + [modelled["winner"]] if c["title_id"] == 20)
    assert shown["conflict"]["d"] >= combine.D_THRESHOLD


async def test_the_reveal_carries_each_members_approval_breadth(db, world):
    """"Unanimous." stood over one member's only yes; the reveal now carries each yes's breadth."""
    room = await finished_session(db, world)
    await play.settle(db, room["session_id"])
    slate_titles = [r["title_id"] for r in await ballot.slate_of(db, room["session_id"])]
    host, member = room["seats"][0]["id"], room["seats"][1]["id"]
    await ballot.submit(db, participant_id=host, approved=slate_titles)
    await ballot.submit(db, participant_id=member, approved=[slate_titles[0]])
    counted = await ballot.tally(db, room["session_id"])
    outcome = await ballot.resolve(db, room["session_id"])
    body = await result.slate(db, room["session_id"], counted, outcome)

    assert "unanimous" not in body
    by_seat = {b["participant_id"]: b for b in body["breadth"]}
    assert (by_seat[host]["approved"], by_seat[host]["of"]) == (len(slate_titles), len(slate_titles))
    assert by_seat[host]["only_yes"] is False
    assert (by_seat[member]["approved"], by_seat[member]["only_yes"]) == (1, True), (
        "the winner was jenny's only yes, and the reveal says so"
    )
    assert by_seat[member]["name"] == "jenny"


async def test_breadth_is_not_readable_before_every_ballot_is_in(db, world):
    """Per-seat counts leak votes, so `result.breadth` refuses until every seat is in (54e)."""
    room = await finished_session(db, world)
    await play.settle(db, room["session_id"])
    first = [r["title_id"] for r in await ballot.slate_of(db, room["session_id"])][:1]
    await ballot.submit(db, participant_id=room["seats"][0]["id"], approved=first)
    with pytest.raises(ballot.BallotError) as early:
        await result.breadth(db, room["session_id"], winner_id=first[0])
    assert early.value.reason == "still_voting"


async def test_progress_expected_is_an_estimate_not_the_cap(db, world):
    """Decision 507: past the median the expected count is dropped; the count alone is honest."""
    room = await running_room(db, world, wide=True)
    first = room["seats"][0]["id"]
    fresh = await play.progress(db, room["session_id"])
    assert {p["expected"] for p in fresh} == {rnd.TYPICAL_PAIRS}
    assert rnd.TYPICAL_PAIRS < rnd.CAP_PAIRS

    await db.execute(
        "UPDATE session_participant SET answered_count = 12 WHERE id = $1", first
    )
    past = await play.progress(db, room["session_id"])
    assert next(p for p in past if p["participant_id"] == first)["expected"] is None
    assert play.expected_pairs(rnd.TYPICAL_PAIRS - 1) == rnd.TYPICAL_PAIRS
    assert play.expected_pairs(rnd.TYPICAL_PAIRS) is None, "reached, so no invented next number"
    assert play.expected_pairs(40) is None, "and never the cap"


async def _veto_fixture(db):
    """Quote-verified on title 1, inferred on title 3: one per tier (decisions 480, 504)."""
    await db.execute(
        "INSERT INTO dna_term (version, term, facet) VALUES ($1, 'mood.violent', 'mood')", VOCAB
    )
    tag_id = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, provider) "
        "VALUES (1, $1, 'mood.violent', 'mood', 3, 0.9, 'fixture') RETURNING id",
        VOCAB,
    )
    await db.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'a fight', 'fixture')",
        tag_id,
    )
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES (3, $1, 'mood.violent', 'mood', 8, 'keyword:fixture')",
        VOCAB,
    )


async def test_a_vetoed_term_removes_the_titles_that_carry_it_in_either_tier(db, world):
    """Decision 504: presence over both tiers. Carriers leave, never neighbours."""
    await _veto_fixture(db)
    plain = await build(db, world, include_rewatches=True)
    vetoed = await build(
        db, world, include_rewatches=True, vetoed_terms=pool.veto_terms(["violence"]),
        dna_version=VOCAB,
    )
    assert 1 in {c.title_id for c in plain} and 3 in {c.title_id for c in plain}
    assert 1 not in {c.title_id for c in vetoed}, "the quote-verified carrier is vetoed"
    assert 3 not in {c.title_id for c in vetoed}, "and so is the one carrying it by projection"
    assert {c.title_id for c in plain} - {c.title_id for c in vetoed} == {1, 3}


async def test_vetoes_are_any_seated_members_to_set_and_only_before_start(db, world):
    """Any seated member, only before start. Decision 505: each member holds their own three, and the
    pool excludes the union."""
    await _veto_fixture(db)
    room = await open_room(db, world, include_rewatches=True, budget_min=200)
    joined = await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    stranger = await insert_user(db, "mia")

    three = ["violence", "horror", "harrowing"]
    assert await rooms.set_vetoes(
        db, session_id=room["session_id"], user_id=world["patrick"], keys=three
    ) == three
    kept = await rooms.set_vetoes(
        db, session_id=room["session_id"], user_id=world["jenny"], keys=["sexual_violence"]
    )
    assert kept == ["sexual_violence"], "a full set on one seat leaves the next seat its own three"
    lobby = await rooms.lobby(db, room["session_id"])
    union = ["violence", "sexual_violence", "horror", "harrowing"]
    assert [v["key"] for v in lobby["vetoes"]] == union
    by_seat = {s["user_id"]: [v["key"] for v in s["vetoes"]] for s in lobby["seats"]}
    assert by_seat == {world["patrick"]: three, world["jenny"]: ["sexual_violence"]}
    listed = await rooms.open_rooms(db, viewer_id=world["patrick"])
    assert [
        v["key"] for v in next(r for r in listed if r["session_id"] == room["session_id"])["vetoes"]
    ] == union, "the open-rooms row shows everything the room has ruled out"

    # One member lifting their own leaves the other's standing.
    await rooms.set_vetoes(
        db, session_id=room["session_id"], user_id=world["patrick"], keys=["violence"]
    )
    lobby = await rooms.lobby(db, room["session_id"])
    assert [v["key"] for v in lobby["vetoes"]] == ["violence", "sexual_violence"]

    with pytest.raises(rooms.RoomError) as outsider:
        await rooms.set_vetoes(
            db, session_id=room["session_id"], user_id=stranger, keys=["horror"]
        )
    assert outsider.value.reason == "not_seated"
    with pytest.raises(rooms.RoomError) as too_many:
        await rooms.set_vetoes(
            db, session_id=room["session_id"], user_id=world["jenny"], keys=list(pool.VETOES)
        )
    assert too_many.value.reason == "bad_veto"

    await play.start(db, room["session_id"])
    snapshot = await play.snapshot_of(db, room["session_id"])
    assert 1 not in snapshot.candidates and 3 not in snapshot.candidates, (
        "a veto one member set reached the frozen pool, over both tiers"
    )
    frozen = await db.fetchval(
        "SELECT context -> 'pool' FROM session WHERE id = $1", room["session_id"]
    )
    frozen = frozen if isinstance(frozen, dict) else json.loads(frozen)
    assert frozen["vetoes"] == ["violence", "sexual_violence"]
    host_seat = next(s["participant_id"] for s in lobby["seats"] if s["role"] == "host")
    assert frozen["vetoes_by"] == {
        str(joined["participant_id"]): ["sexual_violence"],
        str(host_seat): ["violence"],
    }, "whose each veto was is frozen beside the union, for the evening's reader"
    with pytest.raises(rooms.RoomError) as late:
        await rooms.set_vetoes(
            db, session_id=room["session_id"], user_id=world["jenny"], keys=[]
        )
    assert late.value.reason == "started"


async def test_a_pool_the_vetoes_empty_says_which_veto_to_lift(db, world):
    """The refusal names the vetoes, not the budget (decision 216's register)."""
    await _veto_fixture(db)
    for title_id in (2, 3, 4, 5, 6):
        tag_id = await db.fetchval(
            "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, provider) "
            "VALUES ($1, $2, 'mood.violent', 'mood', 1, 0.5, 'fixture') RETURNING id",
            title_id, VOCAB,
        )
        await db.execute(
            "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'a fight', 'f')",
            tag_id,
        )
    room = await open_room(db, world, include_rewatches=True, budget_min=200)
    await rooms.join(db, session_id=room["session_id"], user_id=world["jenny"])
    await rooms.set_vetoes(
        db, session_id=room["session_id"], user_id=world["patrick"], keys=["violence"]
    )
    with pytest.raises(play.RoundError) as empty:
        await play.start(db, room["session_id"])
    assert empty.value.reason == "empty_pool"
    assert "lift the veto on violence" in str(empty.value)


async def test_the_pair_card_names_each_titles_genres_in_plain_words(db, world):
    """Up to two canonical genres (decision 473), Wikidata's free text excluded and spellings merged.
    Plain words: the card is shown before anything is decided."""
    for title_id, genre, source in [
        (1, "Animation", "tmdb"), (1, "anime", "trakt"), (1, "Fantasy", "tmdb"),
        (1, "Adventure", "omdb"), (1, "coming-of-age film", "wikidata"),
        (2, "Drama", "tmdb"), (2, "drama", "trakt"),
    ]:
        await db.execute(
            "INSERT INTO title_genre (title_id, genre, source) VALUES ($1, $2, $3)",
            title_id, genre, source,
        )
    assert await pool.genres_of(db, [1, 2, 3]) == {
        1: ["Adventure", "Animation"], 2: ["Drama"],
    }, "two at most, in the canonical order; a title with none is absent"

    room = await running_room(db, world)
    snapshot = await play.snapshot_of(db, room["session_id"])
    assert snapshot.candidates[1]["genres"] == ["Adventure", "Animation"]
    assert snapshot.candidates[2]["genres"] == ["Drama"]
    assert snapshot.candidates[3]["genres"] == []
    card = await play.state_for(db, room["seats"][0]["id"])
    assert card["pair"] is not None, "the round has a pair to show, or this is vacuous"
    for side in ("a", "b"):
        assert isinstance(card["pair"][side]["genres"], list)
        assert "." not in "".join(card["pair"][side]["genres"]), "a vocabulary id, not a genre"


async def test_the_reveal_lists_each_card_once(db, world):
    """The runners-up are the finalists that lost; the wildcard is its own block."""
    room = await finished_session(db, world)
    session_id = room["session_id"]
    await play.settle(db, session_id)
    slate = await ballot.slate_of(db, session_id)
    finalists = [r["title_id"] for r in slate if r["slot"] == combine.SLOT_FINALIST]
    wildcard_id = next(r["title_id"] for r in slate if r["slot"] == combine.SLOT_WILDCARD)
    for seat in room["seats"]:
        await ballot.submit(db, participant_id=seat["id"], approved=[finalists[0]])
    card = await result.slate(
        db, session_id, await ballot.tally(db, session_id), await ballot.resolve(db, session_id),
    )
    assert card["winner"]["title_id"] == finalists[0]
    assert card["runners_up"] and all(
        c["slot"] == combine.SLOT_FINALIST for c in card["runners_up"]
    ), "the runners-up are the finalists that lost"
    assert wildcard_id not in {c["title_id"] for c in card["runners_up"]}
    assert card["wildcard"]["title_id"] == wildcard_id
    assert card["wildcard"]["approvals"] == 0, "and its own count travels with its own block"
