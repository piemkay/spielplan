"""The playback watcher and the finish prompt. Spec v2.1 §7.3, §13.

"**Jellyfin playback is a suggestion, never a silent write:** >= 90% playback … arms a
per-user prompt — 'Did you finish X?' -> one tap sets `seen`."

The rule with teeth is the negative one: arming writes no state. §13 measures rating capture
against *finished playbacks*, and that denominator stops meaning anything the moment the app
starts marking things seen on its own.

The second rule is arithmetic. The poll runs every minute and a film sits above 90% for its
last ten; without a guard the household answers the same question ten times. The guard is one
*open* prompt per (user, title) — not one per Jellyfin session id, because that id is derived
from the device and is the same one every time that television plays anything.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.connectors.jellyfin import JellyfinClient, NowPlaying
from spielplan.connectors.registry import save_jellyfin
from spielplan.sync import playback

PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"
TICKS = 170 * 60 * 10_000_000


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


def watching(*, user=PATRICK_JF, item="jf-1", fraction=0.95, session="sess-1", played=False):
    return NowPlaying(
        session_id=session, jf_user_id=user, item_id=item,
        position_ticks=int(TICKS * fraction), runtime_ticks=TICKS, played=played,
    )


async def _prompts(db, user_id):
    return await db.fetch(
        "SELECT id, title_id, prompt_state, progress FROM playback_event "
        "WHERE user_id = $1 ORDER BY id", user_id,
    )


# --- arming -------------------------------------------------------------------------------


async def test_a_session_past_the_threshold_arms_a_prompt(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)
    assert report.armed == 1
    rows = await _prompts(db, world["patrick"])
    assert len(rows) == 1
    assert rows[0]["prompt_state"] == "armed"
    assert 0.94 < rows[0]["progress"] < 0.96


async def test_arming_writes_no_seen_state(db, world):
    """The whole point of §7.3: inferred state never outranks the person."""
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


async def test_a_session_below_the_threshold_arms_nothing(db, world):
    report = playback.WatchReport()
    await playback.observe(db, [watching(fraction=0.5)], report)
    assert (report.armed, report.watching) == (0, 1)
    assert await _prompts(db, world["patrick"]) == []


async def test_jellyfins_own_played_flag_arms_below_the_threshold(db, world):
    """§7.3 names two triggers: ">= 90%" *and* the "IsPlayed delta". Someone who stops at 88%
    and taps "mark watched" in Jellyfin should still be asked."""
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
    """The guard is one OPEN prompt per (user, title), not one per Jellyfin session.

    Someone who starts a film, stops, and restarts it on another device that evening has one
    unanswered question, not two. `test_a_rewatch_on_the_same_device_asks_again` covers the
    other half: once the question is answered, the next viewing may ask again.
    """
    report = playback.WatchReport()
    await playback.observe(db, [watching(session="tv")], report)
    await playback.observe(db, [watching(session="phone")], report)
    assert report.armed == 1
    assert report.already_armed == 1
    assert len(await _prompts(db, world["patrick"])) == 1


async def test_an_unlinked_jellyfin_user_is_skipped_not_an_error(db, world):
    """§3.3: linking is optional. Someone watching on an unlinked Jellyfin account simply has
    nobody in this app to ask."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(user=JENNY_JF)], report)
    assert (report.armed, report.watching) == (0, 1)
    assert await db.fetchval("SELECT count(*) FROM playback_event") == 0


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


# --- the poll, end to end through the fake server -------------------------------------------


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
    """§3.3: a degraded sync, never a broken app."""
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    report = await playback.poll(db, JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2))
    assert (report.armed, report.watching) == (0, 0)


# --- answering ------------------------------------------------------------------------------


async def test_the_queue_surfaces_the_prompt_and_marks_it_shown(db, world):
    """§7.3: "when undeliverable, the prompt queues and surfaces as an in-app banner on next
    open"."""
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
    """The one state write on the whole playback path."""
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


async def test_answering_no_writes_nothing(db, world):
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.95)
    event = (await _prompts(db, world["patrick"]))[0]

    result = await playback.answer(
        db, user_id=world["patrick"], event_id=event["id"], finished=False
    )
    assert result["ok"] and result["seen"] is False
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "dismissed"


async def test_an_answered_prompt_does_not_come_back(db, world):
    """A card that reappears after being dismissed teaches people to ignore the banner."""
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
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


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


# --- the threshold itself ---------------------------------------------------------------


def test_the_threshold_is_the_one_the_spec_names():
    """§7.3 says ">= 90% playback". Bracketing it with 0.5 and 0.95 would leave every value in
    between passing, which is the same as not testing the number at all."""
    from spielplan.core.config import settings

    assert settings().finish_threshold == 0.9


async def test_the_boundary_is_inclusive(db, world):
    """Exactly 90% counts. `>` instead of `>=` is a one-character change no other test here
    would notice."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(fraction=0.899, session="under")], report)
    assert report.armed == 0

    await playback.observe(db, [watching(fraction=0.9, session="at")], report)
    assert report.armed == 1


# --- one open prompt, and a rewatch that is allowed to ask again --------------------------


async def test_a_rewatch_on_the_same_device_asks_again(db, world):
    """Jellyfin's `SessionInfo.Id` is derived from the client and device, so it is the *same*
    id every time that television plays anything. Deduplicating on it would suppress the ten
    polls inside one viewing and also the rewatch three months later — a fair question the
    household would simply never be asked."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(session="living-room-tv")], report)
    event = (await _prompts(db, world["patrick"]))[0]
    await playback.answer(db, user_id=world["patrick"], event_id=event["id"], finished=False)

    await playback.observe(db, [watching(session="living-room-tv")], report)
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


# --- the interaction with the two-way sync -------------------------------------------------


async def test_a_title_already_seen_arms_nothing(db, world):
    """§7.3 makes the sync two-way, so Jellyfin's own Played flag can have arrived first and
    been adopted. Asking "Did you finish X?" about something the app already shows as seen is a
    question with no answer — and the card's own copy ("nothing is marked until you say so")
    would be false the moment it appeared."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')",
        world["patrick"],
    )
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)
    assert report.armed == 0
    assert await db.fetchval("SELECT count(*) FROM playback_event") == 0


async def test_an_open_prompt_closes_when_the_state_arrives_another_way(db, world):
    """Armed at 21:40, adopted by the 15-minute sync at 21:45, opened by the person at 21:50.
    The question is already answered; showing it would contradict every other surface."""
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.96)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, jf_synced_at) "
        "VALUES ($1, 1, 'seen', now())",
        world["patrick"],
    )

    assert await playback.pending(db, world["patrick"]) == []
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "answered"
