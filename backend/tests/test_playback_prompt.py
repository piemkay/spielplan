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

A dismissed viewing is the exception M4.11 adds, and it is keyed on that session id precisely
because "this viewing" is what it has to mean: a "no" left no open prompt and no `seen` row, so the
next of those ten polls armed the question again and pushed it again. Keyed narrowly, in the insert
rather than in the index, because the index's predicate would have banned every later viewing from
every device for good — which `0006_jellyfin.sql` argues against in its own words. [decision 211]

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import logging

import pytest

from spielplan.connectors.jellyfin import JellyfinClient, NowPlaying
from spielplan.connectors.registry import JellyfinConfig, save_jellyfin
from spielplan.sync import playback, seen

PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"
TICKS = 170 * 60 * 10_000_000
# A configuration with no server, for the one test that needs `seen.set_state`'s app-side half
# and nothing else: the push it refuses ("jellyfin not configured") is not what is under test.
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
    """One `/Sessions` row, in the shape a real server sends it.

    `raw` defaults to the minimum every Jellyfin `NowPlayingItem` carries — an id and a `Type` —
    rather than to `{}`, because that is what decides whether `_resolve_session`'s ProviderIds
    fallback has anything to work with, and a default of nothing would have hidden the difference.
    """
    return NowPlaying(
        session_id=session, jf_user_id=user, item_id=item,
        position_ticks=int(TICKS * fraction), runtime_ticks=TICKS, played=played,
        item_type=item_type, series_id=series,
        raw=raw if raw is not None else {"Id": item, "Type": item_type},
    )


def _pushes(monkeypatch) -> list[dict]:
    """Every payload `notify` hands the sender, without a keypair or a socket.

    Patched at `spielplan.push.send.send_to_user` because `notify` imports the module and calls
    the attribute — which is also what makes "exactly one push per viewing" measurable at all.
    """
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
    unanswered question, not two. `test_a_rewatch_in_a_new_viewing_asks_again` covers the
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


async def test_an_episode_session_arms_the_prompt_for_its_series(db, world):
    """decision 210: Jellyfin never plays a Series. The row carries the episode's own `Id` and a
    `SeriesId` for the folder, so resolving `item_id` put every television session in
    `report.unresolved` — which nothing logs and no surface shows — and the whole Series partition
    sat outside §13's capture loop while "rating capture > 70% of finished playbacks" read healthy.

    End to end through the fake rather than off a constructed `NowPlaying`, because the shape is
    half the bug: the double used to emit the Series item itself, so both the M1 exit criterion and
    e2e 08 certified a behaviour that existed only against the double.
    """
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    await world["module"].force_session(
        world["module"].SessionControl(user_id=PATRICK_JF, item_id="jf-6", fraction=0.96)
    )
    report = await playback.poll(db, world["client"])

    assert report.unresolved == [], "an episode of a library series is not an unknown item"
    assert report.armed == 1
    assert [r["title_id"] for r in await _prompts(db, world["patrick"])] == [6]


async def test_a_mid_series_episode_arms_nothing(db, world):
    """decision 210(c): a series is finished when the finished episode is the last one the server
    knows about. Episode 1 of 2 is a resolved session and not a finished show — so nothing is
    armed, and nothing is reported as a failure either."""
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
    """§7.1: a household with two copies of one film has two item ids, and `title.jellyfin_id`
    can only hold one of them. The `/Sessions` row for the other one carries the id, the name and
    the type and no ProviderIds at all — which is what a real server sends and what the fake's
    `_now_playing` sends — so the copy map the sweep writes is the only thing that can place it."""
    await db.execute(
        "INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ('jf-1b', 1)"
    )
    report = playback.WatchReport()
    await playback.observe(db, [watching(item="jf-1b")], report)

    assert (report.armed, report.unresolved) == (1, [])
    assert [r["title_id"] for r in await _prompts(db, world["patrick"])] == [1]


async def test_a_session_on_a_copy_no_sweep_has_seen_resolves_through_its_provider_ids(db, world):
    """The last fallback, and the reason it is `resolve.resolve_title_id` rather than another
    query here: that function already knows the provider keys §7.1 names and already refuses the
    ambiguous name/year match. A copy added since the last sweep still resolves."""
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
    """Undecidable is not "yes" (decision 210(c)). A server that will not list the episodes leaves
    the app unable to say whether the show is finished, and the alternative to reporting it is
    marking a twenty-hour series seen on the strength of one episode.

    Reported in `undecided` and NOT in `unresolved`, which is the half review cycle 2 separated.
    One list carried both outcomes, and `unresolved` is the one an operator is told "matched no
    title" about: the repair that sentence names -- import the title, add a provider id -- is the
    wrong repair here, because the session DID resolve, the episode id resolves in Jellyfin
    perfectly, and the fault is a `/Shows/{id}/Episodes` their proxy blocks or their server errors
    on. Measured before the split: `report.unresolved = ['jf-6-e2']` for a session the resolver had
    just placed on title 6. [M4.11 review cycle 2: m411-rev2-pb-02]
    """
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


async def test_an_outage_is_logged_once_and_so_is_its_end(db, world, caplog, monkeypatch):
    """ops-15. The behaviour above was already right; the log was not. This poll runs every sixty
    seconds, so a box powered off overnight wrote ten hours of identical warnings and buried the
    backup and refit lines §6.6 makes the operator's data.

    Three polls: down, still down, back. The cadence is deliberately unchanged — §7.3's prompt
    window is minutes long — so what changes is the level, and the recovery says how long it was.
    """
    monkeypatch.setattr(playback, "_unreachable_since", None)
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)
    down = JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2)

    with caplog.at_level(logging.DEBUG, logger="spielplan.sync.playback"):
        await playback.poll(db, down)
        await playback.poll(db, down)
        await playback.poll(db, world["client"])

    lines = [r for r in caplog.records if r.name == "spielplan.sync.playback"]
    assert [r.levelname for r in lines] == ["WARNING", "DEBUG", "INFO"]
    assert "reachable again" in lines[-1].getMessage()


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


async def test_answering_no_writes_unseen(db, world):
    """decision 211, and the renamed half of what used to be `..._writes_nothing`.

    Writing nothing was the defect: an absent `user_title` row is §4.2's default rather than an
    assertion, which is exactly what the 15-minute sweep adopts Jellyfin's Played flag into — so
    the card the person declined came back as a `seen` state inside the quarter hour. "The sweep
    may not adopt while a prompt is open" only means something if closing the prompt leaves a fact.
    """
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
    """`answer`'s own rule — "a card that comes back after being dismissed teaches people to
    ignore the banner" — with something enforcing it at last (finding 11).

    Measured before the fix: the film is still above the threshold, the poll runs every sixty
    seconds, and `arm`'s two guards (an open prompt, an already-`seen` title) are both absent after
    a decline — so three further polls left `[(1, 'dismissed'), (2, 'armed')]` and sent two pushes.
    """
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
    """finding 12, and the order is the whole of it: the prompt used to be consumed first.

    Asserted from inside `set_state` rather than by a timestamp, because the fault is an ordering
    one and a clock cannot tell these two statements apart. Both answers are checked: decision 211
    made "no" a state write too, so both branches have a durable half to put first.
    """
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
    """The other half of finding 12. §13 measures rating capture against finished playbacks, and a
    tap that lost both the state and the question subtracted from the numerator with no trace:
    nothing re-arms it, because `observe` only arms while a live session is past the threshold and
    by then it is not. The question survives the failure, so the next open asks it again."""
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
    """§7.3's push and §7.3's banner are the same question, so the notification has to say which
    question it is and where the answer lives. The service worker is a renderer: with no `tag` it
    falls back to the literal `'spielplan'`, which every other notification this app sends also
    uses — so a Tonight invitation replaced an unread finish prompt on the lock screen."""
    sent = _pushes(monkeypatch)
    report = playback.WatchReport()
    await playback.observe(db, [watching()], report)

    assert report.armed == 1
    assert len(sent) == 1
    assert sent[0]["tag"] == "finish:1"
    assert sent[0]["url"] == "/"
    assert sent[0]["body"] == "Did you finish Heat?"


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
    # The decline itself wrote `unseen` (decision 211); what the refused second answer must not do
    # is turn that into `seen` behind a 404.
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


# --- the threshold itself ---------------------------------------------------------------


def test_the_threshold_is_the_one_the_spec_names():
    """§7.3 says ">= 90% playback". Bracketing it with 0.5 and 0.95 would leave every value in
    between passing, which is the same as not testing the number at all.

    Read off the module rather than `Settings`: M4.7 deleted the `finish_threshold` field,
    because a knob the shipped compose file cannot deliver is configurability nobody asked for
    and nobody can use. The number is still asserted; only its home moved. [M4.7 ds07]
    """
    assert playback.FINISH_THRESHOLD == 0.9


async def test_the_boundary_is_inclusive(db, world):
    """Exactly 90% counts. `>` instead of `>=` is a one-character change no other test here
    would notice."""
    report = playback.WatchReport()
    await playback.observe(db, [watching(fraction=0.899, session="under")], report)
    assert report.armed == 0

    await playback.observe(db, [watching(fraction=0.9, session="at")], report)
    assert report.armed == 1


# --- one open prompt, and a rewatch that is allowed to ask again --------------------------


async def test_a_rewatch_in_a_new_viewing_asks_again(db, world):
    """Renamed from `test_a_rewatch_on_the_same_device_asks_again`, because finding 11's fix makes
    the old name false and §9 does not allow a name that lies.

    The dismissal guard is keyed on `jf_session_id`, and Jellyfin derives `SessionInfo.Id` from the
    client and device — so the viewing it closes is that device's, and a rewatch from that same
    client is not asked again. What this pins is the half that must survive: the guard is not a
    (user, title) ban, which is what widening the partial unique index to `'dismissed'` would have
    made it (`0006_jellyfin.sql` argues the same point from the other side). A second viewing is a
    second question.
    """
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
    """Armed at 21:40 by the television, marked seen at 21:50 from the title card, opened at 21:55.
    The question is already answered; showing it would contradict every other surface.

    The "other way" used to be the 15-minute sweep adopting Jellyfin's own Played flag, and this
    test simulated it with a direct `user_title` INSERT. Decision 211 removed that path: the sweep
    no longer adopts for a (user, title) whose prompt is open, because a sync closing a prompt as
    `answered` is §7.3:315's silent write wearing the person's answer — and §13 then counted it in
    the denominator as one. So the arrival is now what it always should have been here: the person,
    on another surface, through the same `seen.set_state` the title card calls. The statement this
    pins is unchanged; only who can reach it is.
    """
    await playback.arm(db, user_id=world["patrick"], title_id=1, session_id="s", progress=0.96)
    await seen.set_state(
        db, None, NO_JELLYFIN, user_id=world["patrick"], title_id=1, state="seen"
    )

    assert await playback.pending(db, world["patrick"]) == []
    assert (await _prompts(db, world["patrick"]))[0]["prompt_state"] == "answered"
