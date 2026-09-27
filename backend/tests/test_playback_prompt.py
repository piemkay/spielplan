"""Arming writes no state: §13's capture rate needs an honest denominator. One OPEN prompt per
(user, title); a dismissal is keyed on the Jellyfin session id (decision 211)."""

from __future__ import annotations

import logging

import pytest

from spielplan.connectors.jellyfin import JellyfinClient, NowPlaying
from spielplan.connectors.registry import JellyfinConfig, save_jellyfin
from spielplan.sync import playback, seen

PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"
TICKS = 170 * 60 * 10_000_000
# No server: the push `seen.set_state` then refuses is not what is under test.
NO_JELLYFIN = JellyfinConfig(url="", api_key="")


@pytest.fixture
async def world(db, fake_jellyfin, secrets_key):
    module, transport = fake_jellyfin
    await db.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id) "
        "VALUES (1, 'movie', 'Heat', 1995, 'jf-1'), (6, 'series', 'Severance', 2022, 'jf-6')"
    )
    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', $1, 'linked') RETURNING id", PATRICK_JF
    )
    jenny = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    client = JellyfinClient("http://jellyfin.test", module.API_KEY, transport=transport)
    return {"module": module, "client": client, "patrick": patrick, "jenny": jenny}


def watching(
    *, user=PATRICK_JF, item="jf-1", fraction=0.95, session="sess-1", played=False,
    item_type="Movie", series=None, raw=None,
):
    """`raw` carries the minimum every `NowPlayingItem` has (an id and a `Type`), which decides
    whether `_resolve_session`'s ProviderIds fallback has anything to work with."""
    return NowPlaying(
        session_id=session, jf_user_id=user, item_id=item,
        position_ticks=int(TICKS * fraction), runtime_ticks=TICKS, played=played,
        item_type=item_type, series_id=series,
        raw=raw if raw is not None else {"Id": item, "Type": item_type},
    )


def _pushes(monkeypatch) -> list[dict]:
    """Patched at `spielplan.push.send.send_to_user`: `notify` calls the module attribute."""
    sent: list[dict] = []

    async def record(_conn, _user_id, payload, **_kw):
        sent.append(payload)
        return []

    monkeypatch.setattr("spielplan.push.send.send_to_user", record)
    return sent


async def _prompts(db, user_id):
    return await db.fetch(
        "SELECT id, title_id, prompt_state, progress FROM playback_event "
        "WHERE user_id = $1 ORDER BY id", user_id,
    )


async def test_a_session_past_the_threshold_arms_a_prompt(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)
    assert report.armed == 1
    rows = await _prompts(db, world["patrick"])
    assert len(rows) == 1
    assert rows[0]["prompt_state"] == "armed"
    assert 0.94 < rows[0]["progress"] < 0.96


async def test_arming_writes_no_seen_state(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


async def test_a_session_below_the_threshold_arms_nothing(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching(fraction=0.5)], report)
    assert (report.armed, report.watching) == (0, 1)
    assert await _prompts(db, world["patrick"]) == []


async def test_jellyfins_own_played_flag_arms_below_the_threshold(db, world):
    """§7.3 names two triggers: ">= 90%" and the "IsPlayed delta"."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(fraction=0.4, played=True)], report)
    assert report.armed == 1


async def test_repeated_polls_of_one_viewing_arm_it_once(db, world):
    """Ten minutes above 90% at one poll per minute."""
    report = playback.WatchReport()
    for _ in range(10):
        await playback.observe(db, [watching()], report)
    assert report.armed == 1
    assert report.already_armed == 9
    assert len(await _prompts(db, world["patrick"])) == 1


async def test_a_second_viewing_while_the_first_is_unanswered_adds_no_card(db, world):
    """Jellyfin session ids are per device; one unanswered question per (user, title) across devices."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(session="tv")], report)
    await playback.observe(db, [watching(session="phone")], report)
    assert report.armed == 1
    assert report.already_armed == 1
    assert len(await _prompts(db, world["patrick"])) == 1


async def test_an_unlinked_jellyfin_user_is_skipped_not_an_error(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching(user=JENNY_JF)], report)
    assert (report.armed, report.watching) == (0, 1)
    assert await db.fetchval("SELECT count(*) FROM playback_event") == 0


async def test_an_episode_session_arms_the_prompt_for_its_series(db, world):
    """Decision 210: Jellyfin never plays a Series; a session carries the episode `Id` and `SeriesId`."""
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    await world["module"].force_session(
        world["module"].SessionControl(user_id=PATRICK_JF, item_id="jf-6", fraction=0.96)
    )
    report = await playback.poll(db, world["client"])

    assert report.unresolved == [], "an episode of a library series is not an unknown item"
    assert report.armed == 1
    assert [r["title_id"] for r in await _prompts(db, world["patrick"])] == [6]


async def test_a_mid_series_episode_arms_nothing(db, world):
    """Decision 210(c): a series is finished when its last episode the server knows of is."""
    report = playback.WatchReport()
    await playback.observe(
        db,
        [watching(item="jf-6-e1", item_type="Episode", series="jf-6", fraction=0.96)],
        report,
        client=world["client"],
    )
    assert (report.armed, report.unresolved, report.watching) == (0, [], 1)
    assert await _prompts(db, world["patrick"]) == []


async def test_a_session_on_a_copy_the_sweep_recorded_resolves_through_the_copy_map(db, world):
    """A second copy's `/Sessions` row carries no ProviderIds, so only the sweep's copy map places it."""
    await db.execute(
        "INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ('jf-1b', 1)"
    )
    report = playback.WatchReport()
    await playback.observe(db, [watching(item="jf-1b")], report)

    assert (report.armed, report.unresolved) == (1, [])
    assert [r["title_id"] for r in await _prompts(db, world["patrick"])] == [1]


async def test_a_session_on_a_copy_no_sweep_has_seen_resolves_through_its_provider_ids(db, world):
    await db.execute("UPDATE title SET imdb_id = 'tt0113277' WHERE id = 1")
    report = playback.WatchReport()
    await playback.observe(
        db,
        [watching(
            item="jf-1b",
            raw={"Id": "jf-1b", "Type": "Movie", "ProviderIds": {"Imdb": "tt0113277"}},
        )],
        report,
    )
    assert (report.armed, report.unresolved) == (1, [])
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 0, (
        "the 1-minute poll resolves and never upserts: the copy map is the sweep's to write"
    )


async def test_an_episode_whose_series_cannot_be_listed_arms_nothing_and_is_reported(db, world):
    """Undecidable is not "yes" (decision 210(c)). Reported in `undecided`, not `unresolved`: the
    session did resolve, and `unresolved`'s repair (import the title) is the wrong one."""
    unreachable = JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2)
    report = playback.WatchReport()
    await playback.observe(
        db,
        [watching(item="jf-6-e2", item_type="Episode", series="jf-6", fraction=0.99)],
        report,
        client=unreachable,
    )
    assert (report.armed, report.undecided) == (0, ["jf-6-e2"])
    assert report.unresolved == [], (
        "the session resolved to a title; only the episode list did not answer (decision 210(c))"
    )
    assert report.as_dict()["undecided"] == ["jf-6-e2"], "and the route body carries it too"
    assert await _prompts(db, world["patrick"]) == []


async def test_an_unresolved_item_is_reported(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching(item="jf-unknown")], report)
    assert report.unresolved == ["jf-unknown"]
    assert report.armed == 0


async def test_two_people_watching_two_things_get_one_prompt_each(db, world):
    await db.execute(
        "UPDATE app_user SET jellyfin_user_id = $1, jellyfin_link_state = 'linked' WHERE id = $2",
        JENNY_JF, world["jenny"],
    )
    report = playback.WatchReport()
    await playback.observe(
        db,
        [watching(session="a"), watching(user=JENNY_JF, item="jf-6", session="b")],
        report,
    )
    assert report.armed == 2
    assert len(await _prompts(db, world["patrick"])) == 1
    assert len(await _prompts(db, world["jenny"])) == 1


async def test_the_poll_reads_sessions_and_arms(db, world):
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    await world["module"].force_session(
        world["module"].SessionControl(user_id=PATRICK_JF, item_id="jf-1", fraction=0.97)
    )
    report = await playback.poll(db, world["client"])
    assert report.armed == 1
    assert len(await _prompts(db, world["patrick"])) == 1


async def test_the_poll_does_nothing_when_jellyfin_is_unconfigured(db, world):
    report = await playback.poll(db, world["client"])
    assert report.skipped_no_link is True


async def test_an_unreachable_jellyfin_does_not_break_the_poll(db, world):
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    report = await playback.poll(db, JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2))
    assert (report.armed, report.watching) == (0, 0)


async def test_an_outage_is_logged_once_and_so_is_its_end(db, world, caplog, monkeypatch):
    """The poll runs every minute, so an outage logs once and its recovery once, with the duration."""
    monkeypatch.setattr(playback._outage, "since", None)
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    down = JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2)

    with caplog.at_level(logging.DEBUG, logger="spielplan.sync.playback"):
        await playback.poll(db, down)
        await playback.poll(db, down)
        await playback.poll(db, world["client"])

    lines = [r for r in caplog.records if r.name == "spielplan.sync.playback"]
    assert [r.levelname for r in lines] == ["WARNING", "DEBUG", "INFO"]
    assert "reachable again" in lines[-1].getMessage()


async def test_the_queue_surfaces_the_prompt_and_marks_it_shown(db, world):
    await playback.arm(
        db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95
    )
    queued = await playback.pending(db, world["patrick"])
    assert len(queued) == 1
    assert queued[0]["name"] == "Heat"
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "shown"

    # Shown is not answered — it is still there on the next open until someone taps.
    assert len(await playback.pending(db, world["patrick"])) == 1


async def test_answering_yes_writes_seen(db, world):
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]

    result = await playback.answer(
        db, user_id=world["patrick"], event_id=event["id"], finished=True
    )
    assert result["ok"] and result["seen"] is True
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", world["patrick"]
    ) == "seen"


async def test_answering_yes_pushes_to_jellyfin_under_the_users_token(db, world):
    _jf, token = await world["client"].authenticate_by_name("patrick", world["module"].PASSWORD)
    await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): token},
    )
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]

    result = await playback.answer(
        db, user_id=world["patrick"], event_id=event["id"], finished=True,
        client=world["client"],
    )
    assert result["sync"]["synced"] is True
    assert "jf-1" in world["module"].state.played[PATRICK_JF]


async def test_answering_no_writes_unseen(db, world):
    """An absent row is §4.2's default, which the sweep would overwrite with Jellyfin's Played flag."""
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]

    result = await playback.answer(
        db, user_id=world["patrick"], event_id=event["id"], finished=False
    )
    assert result["ok"] and result["seen"] is False
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", world["patrick"]
    ) == "unseen"
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "dismissed"


async def test_a_dismissed_viewing_is_not_re_armed_and_sends_no_second_push(
    db, world, monkeypatch
):
    """After a decline the poll still sees the film above threshold; only the dismissal stops a re-arm."""
    sent = _pushes(monkeypatch)
    report = playback.WatchReport()
    await playback.observe(db, [watching(session="living-room-tv")], report)
    event = (await _prompts(db, world["patrick"]))[0]
    await playback.answer(db, user_id=world["patrick"], event_id=event["id"], finished=False)

    for _ in range(3):
        await playback.observe(db, [watching(session="living-room-tv")], report)

    rows = await _prompts(db, world["patrick"])
    assert [r["prompt_state"] for r in rows] == ["dismissed"]
    assert len(sent) == 1, "one viewing, one question, one notification"


async def test_the_state_write_lands_before_the_prompt_is_consumed(db, world, monkeypatch):
    """Asserted from inside `set_state`, not by timestamps: the fault is an ordering one."""
    from spielplan.sync import seen as seen_sync

    original = seen_sync.set_state
    observed: dict[str, str | None] = {}

    async def recording(conn, client, cfg, *, user_id, title_id, state):
        observed[state] = await conn.fetchval(
            "SELECT prompt_state FROM playback_event WHERE user_id = $1 AND title_id = $2 "
            "ORDER BY id DESC LIMIT 1",
            user_id, title_id,
        )
        return await original(
            conn, client, cfg, user_id=user_id, title_id=title_id, state=state
        )

    monkeypatch.setattr(seen_sync, "set_state", recording)
    for title_id, finished in ((1, True), (6, False)):
        await playback.arm(
            db, user_id=world["patrick"], title_id=title_id, session_id="s", progress=0.95
        )
        event_id = await db.fetchval(
            "SELECT id FROM playback_event WHERE user_id = $1 AND title_id = $2",
            world["patrick"], title_id,
        )
        await playback.answer(
            db, user_id=world["patrick"], event_id=event_id, finished=finished
        )

    assert observed == {"seen": "armed", "unseen": "armed"}


async def test_an_answer_whose_state_write_fails_leaves_the_prompt_open(db, world, monkeypatch):
    """Nothing re-arms a lost question (the session has passed), so a failed write leaves it open."""
    from spielplan.sync import seen as seen_sync

    async def boom(*_args, **_kwargs):
        raise RuntimeError("the seen write did not land")

    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.96)
    event = (await _prompts(db, world["patrick"]))[0]

    monkeypatch.setattr(seen_sync, "set_state", boom)
    with pytest.raises(RuntimeError):
        await playback.answer(
            db, user_id=world["patrick"], event_id=event["id"], finished=True
        )

    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] in playback.OPEN_STATES
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0

    monkeypatch.undo()
    again = await playback.answer(
        db, user_id=world["patrick"], event_id=event["id"], finished=True
    )
    assert again["ok"] and again["seen"] is True
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "answered"


async def test_the_prompt_notification_names_its_kind_and_its_destination(
    db, world, monkeypatch
):
    """With no `tag` the service worker falls back to `'spielplan'`, so a Tonight invitation
    replaced an unread finish prompt."""
    sent = _pushes(monkeypatch)
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)

    assert report.armed == 1
    assert len(sent) == 1
    assert sent[0]["tag"] == "finish:1"
    assert sent[0]["url"] == "/"
    assert sent[0]["body"] == "Did you finish Heat?"


async def test_an_answered_prompt_does_not_come_back(db, world):
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]
    await playback.answer(db, user_id=world["patrick"], event_id=event["id"], finished=True)
    assert await playback.pending(db, world["patrick"]) == []


async def test_a_prompt_cannot_be_answered_twice(db, world):
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]
    await playback.answer(db, user_id=world["patrick"], event_id=event["id"], finished=False)
    again = await playback.answer(
        db, user_id=world["patrick"], event_id=event["id"], finished=True
    )
    assert again["ok"] is False
    # The decline wrote `unseen` (decision 211); the refused second answer must not make it `seen`.
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", world["patrick"]
    ) == "unseen"


async def test_one_person_cannot_answer_anothers_prompt(db, world):
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]
    result = await playback.answer(
        db, user_id=world["jenny"], event_id=event["id"], finished=True
    )
    assert result["ok"] is False
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


async def test_the_queue_is_per_person(db, world):
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    assert await playback.pending(db, world["jenny"]) == []


def test_the_threshold_is_the_one_the_spec_names():
    """Bracketing with 0.5 and 0.95 would not test the number. Read off the module: it is no setting."""
    assert playback.FINISH_THRESHOLD == 0.9


async def test_the_boundary_is_inclusive(db, world):
    """`>` instead of `>=` is a one-character change no other test would notice."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(fraction=0.899, session="under")], report)
    assert report.armed == 0

    await playback.observe(db, [watching(fraction=0.9, session="at")], report)
    assert report.armed == 1


async def test_a_rewatch_in_a_new_viewing_asks_again(db, world):
    """The dismissal is keyed on `jf_session_id`, not (user, title): a second viewing is a second
    question."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(session="living-room-tv")], report)
    event = (await _prompts(db, world["patrick"]))[0]
    await playback.answer(db, user_id=world["patrick"], event_id=event["id"], finished=False)

    await playback.observe(db, [watching(session="bedroom-tv")], report)
    assert report.armed == 2
    assert len(await _prompts(db, world["patrick"])) == 2


async def test_only_one_prompt_is_open_at_a_time_for_a_title(db, world):
    report = playback.WatchReport()
    for session in ("tv", "tv", "phone"):
        await playback.observe(db, [watching(session=session)], report)
    open_prompts = [
        r for r in await _prompts(db, world["patrick"]) if r["prompt_state"] in ("armed", "shown")
    ]
    assert len(open_prompts) == 1


async def test_a_title_already_seen_arms_nothing(db, world):
    """The card's copy ("nothing is marked until you say so") would be false for a title already seen."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')",
        world["patrick"],
    )
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)
    assert report.armed == 0
    assert await db.fetchval("SELECT count(*) FROM playback_event") == 0


async def test_an_open_prompt_closes_when_the_state_arrives_another_way(db, world):
    """The sweep no longer adopts while a prompt is open (decision 211), so the state arrives
    through `seen.set_state`, as the title card calls it."""
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.96)
    await seen.set_state(
        db, None, NO_JELLYFIN, user_id=world["patrick"], title_id=1, state="seen"
    )

    assert await playback.pending(db, world["patrick"]) == []
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "answered"
