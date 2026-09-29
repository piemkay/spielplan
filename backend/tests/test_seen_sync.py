"""The first sync after linking must not push the app's absence over Jellyfin's history: an
absent `user_title` row is the default, not an assertion."""

from __future__ import annotations

import asyncio
import logging

import asyncpg
import httpx
import pytest

from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import (
    SECRETS_UNREADABLE_REASON,
    JellyfinConfig,
    load_jellyfin,
    save_jellyfin,
)
from spielplan.sync import seen

PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"

# Mirrors ops/fake_jellyfin.py's ITEMS, so every fake item resolves to a real title row.
TITLES = [
    (1, "movie", "Heat", 1995, "tt0113277", 949),
    (2, "movie", "Prisoners", 2013, "tt1392214", 146233),
    (3, "movie", "Paddington 2", 2017, None, 346648),
    (6, "series", "Severance", 2022, "tt11280740", 95396),
    (7, "series", "The Bear", 2022, "tt14452776", 136315),
    (8, "movie", "Tampopo", 1985, "tt0092048", 11081),
]


@pytest.fixture
async def world(db, fake_jellyfin, secrets_key):
    module, transport = fake_jellyfin
    for title_id, kind, name, year, imdb, tmdb in TITLES:
        await db.execute(
            "INSERT INTO title (id, kind, name, year, imdb_id, tmdb_id) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            title_id, kind, name, year, imdb, tmdb,
        )

    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', $1, 'linked') RETURNING id", PATRICK_JF
    )
    jenny = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )

    client = JellyfinClient("http://jellyfin.test", module.API_KEY, transport=transport)
    _jf_id, token = await client.authenticate_by_name("patrick", module.PASSWORD)
    cfg = JellyfinConfig(
        url="http://jellyfin.test", api_key=module.API_KEY, user_tokens={str(patrick): token}
    )
    return {
        "module": module, "client": client, "cfg": cfg,
        "patrick": patrick, "jenny": jenny, "token": token,
    }


async def _state(db, user_id, title_id):
    return await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = $2",
        user_id, title_id,
    )


async def _link_state(db, user_id):
    return await db.fetchval("SELECT jellyfin_link_state FROM app_user WHERE id = $1", user_id)


async def _store_connector(db, world, *, tokens: dict[str, str] | None = None):
    """`tokens=None` stores a link with no per-user credential, the state §7.3's adopt-only sweep is for."""
    return await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY, user_tokens=tokens or {}
    )


def _linked(world, *, token: str | None = "") -> seen.LinkedUser:
    return seen.LinkedUser(
        world["patrick"], "patrick", PATRICK_JF,
        world["token"] if token == "" else token, "linked",
    )


async def test_the_app_side_write_never_depends_on_jellyfin(db, world):
    """§3.3: the person's action lands even with no client, and the response says Jellyfin was not told."""
    result = await seen.set_state(
        db, None, JellyfinConfig(), user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result == {"state": "seen", "synced": False, "reason": "Jellyfin not configured"}
    row = await _state(db, world["patrick"], 1)
    assert row["state"] == "seen"
    assert row["jf_synced_at"] is None, "an unsynced write stays owed, not silently forgotten"


async def test_marking_seen_writes_played_with_the_users_own_token(db, world):
    """The fake refuses the admin key on that route, so a fallback to it would fail here."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result["synced"] is True
    assert "jf-1" in world["module"].state.played[PATRICK_JF]
    assert world["module"].state.write_log == [
        {"user": PATRICK_JF, "item": "jf-1", "played": True}
    ]
    assert (await _state(db, world["patrick"], 1))["jf_synced_at"] is not None


async def test_marking_unseen_issues_the_delete(db, world):
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    world["module"].state.played[PATRICK_JF].add("jf-1")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="unseen"
    )
    assert result["synced"] is True
    assert "jf-1" not in world["module"].state.played[PATRICK_JF]


async def test_a_title_that_is_not_on_jellyfin_is_marked_locally_and_says_so(db, world):
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=2, state="seen"
    )
    assert result["synced"] is False
    assert result["reason"] == "not on Jellyfin"
    assert (await _state(db, world["patrick"], 2))["state"] == "seen"


async def test_an_unlinked_account_keeps_its_own_state(db, world):
    """The title is on Jellyfin, so the refusal must name the missing link."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["jenny"], title_id=1, state="seen"
    )
    assert result["synced"] is False
    assert "not linked" in result["reason"]
    assert (await _state(db, world["jenny"], 1))["state"] == "seen"


async def test_an_invalid_state_is_refused_before_anything_is_written(db, world):
    with pytest.raises(ValueError):
        await seen.set_state(
            db, None, JellyfinConfig(), user_id=world["patrick"], title_id=1, state="forgotten"
        )
    assert await _state(db, world["patrick"], 1) is None


async def test_reading_back_our_own_write_changes_nothing(db, world):
    """After the app writes, Jellyfin holds our value; the next sync must see agreement and stop."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    before = await _state(db, world["patrick"], 1)
    world["module"].state.write_log.clear()

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    after = await _state(db, world["patrick"], 1)
    assert (report.pushed, report.adopted) == (0, 0)
    assert world["module"].state.write_log == [], "no write means no loop"
    assert after["state"] == before["state"] == "seen"
    assert after["jf_synced_at"] == before["jf_synced_at"], "the agreement was not re-stamped"


async def test_a_jellyfin_side_change_after_the_sync_is_accepted(db, world):
    """The disagreement is newer than the last agreement, so it came from Jellyfin and is adopted."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    # Someone opens Jellyfin and marks it unwatched.
    world["module"].state.played[PATRICK_JF].discard("jf-1")

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert report.adopted == 1
    assert (await _state(db, world["patrick"], 1))["state"] == "unseen"


async def test_an_action_taken_while_jellyfin_was_down_is_pushed_by_the_next_sync(db, world):
    """`jf_synced_at IS NULL` means the person acted and Jellyfin has not been told."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await seen.set_state(
        db, None, JellyfinConfig(), user_id=world["patrick"], title_id=1, state="seen"
    )

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert report.pushed == 1
    assert "jf-1" in world["module"].state.played[PATRICK_JF]
    assert (await _state(db, world["patrick"], 1))["jf_synced_at"] is not None


async def test_the_first_sync_after_linking_never_erases_jellyfin_history(db, world):
    """Pushing the app's empty state would wipe every Played flag on the server."""
    world["module"].state.played[PATRICK_JF].update({"jf-1", "jf-6"})

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert world["module"].state.write_log == [], "nothing was pushed"
    assert report.adopted == 2
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"
    assert (await _state(db, world["patrick"], 6))["state"] == "seen"
    assert await _state(db, world["patrick"], 2) is None, "unwatched titles get no row"


async def test_an_unresolvable_jellyfin_item_is_reported_not_invented(db, world):
    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert report.resolve["unmatched"] == 1
    assert report.resolve["unmatched_names"] == ["Christmas 2019"]
    assert await db.fetchval("SELECT count(*) FROM title") == len(TITLES)


async def test_a_link_with_no_token_refuses_the_write_and_asks_for_a_re_link(db, world):
    """§7.3's least-privilege path: the admin key would work and is deliberately not used."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    tokenless = JellyfinConfig(url="http://jellyfin.test", api_key=world["module"].API_KEY)

    result = await seen.set_state(
        db, world["client"], tokenless, user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result["synced"] is False
    assert "re-link" in result["reason"]
    assert world["module"].state.write_log == [], "the admin key was never tried"
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", world["patrick"]
    ) == "needs_relink"
    # Written and still owed, so a re-link settles it.
    row = await _state(db, world["patrick"], 1)
    assert row["state"] == "seen" and row["jf_synced_at"] is None


async def test_a_rejected_token_marks_the_link_and_keeps_the_write_owed(db, world):
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    stale = JellyfinConfig(
        url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): "expired-token"},
    )
    result = await seen.set_state(
        db, world["client"], stale, user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result["synced"] is False
    assert "re-link" in result["reason"]
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", world["patrick"]
    ) == "needs_relink"
    assert (await _state(db, world["patrick"], 1))["jf_synced_at"] is None


async def test_a_dead_token_stops_the_sweep_rather_than_hammering_the_server(db, world):
    """One bad token means every remaining title would fail the same way."""
    for title_id, jf in ((1, "jf-1"), (2, "jf-2"), (3, "jf-3")):
        await db.execute("UPDATE title SET jellyfin_id = $2 WHERE id = $1", title_id, jf)
        await seen.set_state(
            db, None, JellyfinConfig(), user_id=world["patrick"], title_id=title_id, state="seen"
        )

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, "expired-token", "linked"),
        report,
    )
    assert report.needs_relink == ["patrick"]
    assert report.pushed == 0


async def test_sync_all_skips_cleanly_when_nothing_is_configured(db, world):
    """A fresh install, not an error."""
    report = await seen.sync_all(db, world["client"])
    assert report.skipped_no_link is True


async def test_sync_all_runs_every_linked_user_and_reports(db, world):
    await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): world["token"]},
    )
    world["module"].state.played[PATRICK_JF].add("jf-1")

    report = await seen.sync_all(db, world["client"])
    assert report.users == ["patrick"]
    assert report.adopted == 1
    assert report.skipped_no_link is False
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"


async def test_a_clean_sync_clears_a_stale_re_link_flag(db, world):
    """Cleared only by a Played write that succeeded: a sweep with nothing owed never uses the token."""
    patrick = world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await db.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' WHERE id = $1", patrick
    )
    await _store_connector(db, world, tokens={str(patrick): world["token"]})

    quiet = await seen.sync_all(db, world["client"])
    assert quiet.completed == ["patrick"], "the sweep itself was perfectly healthy"
    assert (quiet.pushed, quiet.wrote) == (0, set()), "and it wrote nothing, so it proved nothing"
    assert await _link_state(db, patrick) == "needs_relink"

    # An owed write, so the next sweep must use the token; that write is the evidence.
    await seen.set_state(
        db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen"
    )
    report = await seen.sync_all(db, world["client"])
    assert (report.pushed, report.wrote) == (1, {"patrick"})
    assert await _link_state(db, patrick) == "linked"


async def test_unlinking_forgets_the_token_and_keeps_the_seen_state(db, world):
    """The link is optional, so removing it must leave a working account."""
    await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): world["token"]},
    )
    await seen.set_state(
        db, None, JellyfinConfig(), user_id=world["patrick"], title_id=1, state="seen"
    )

    await seen.unlink(db, world["patrick"])

    row = await db.fetchrow(
        "SELECT jellyfin_user_id, jellyfin_link_state FROM app_user WHERE id = $1",
        world["patrick"],
    )
    assert row["jellyfin_user_id"] is None and row["jellyfin_link_state"] is None
    assert (await load_jellyfin(db)).user_tokens == {}
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"


@pytest.fixture
async def both_linked(db, world):
    await db.execute(
        "UPDATE app_user SET jellyfin_user_id = $2, jellyfin_link_state = 'linked' WHERE id = $1",
        world["jenny"], JENNY_JF,
    )
    _jf, token = await world["client"].authenticate_by_name("jenny", world["module"].PASSWORD)
    await save_jellyfin(
        db,
        url="http://jellyfin.test",
        api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): world["token"], str(world["jenny"]): token},
    )
    return world


async def test_each_linked_user_syncs_their_own_state(db, both_linked):
    """Two Jellyfin accounts, two independent answers about the same film."""
    world = both_linked
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    world["module"].state.played[PATRICK_JF].add("jf-1")

    report = await seen.sync_all(db, world["client"])
    assert sorted(report.users) == ["jenny", "patrick"]
    assert sorted(report.completed) == ["jenny", "patrick"]
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"
    assert await _state(db, world["jenny"], 1) is None, "jenny watched nothing"


async def test_one_users_action_is_pushed_under_their_own_token(db, both_linked):
    world = both_linked
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    cfg = await load_jellyfin(db)

    await seen.set_state(
        db, world["client"], cfg, user_id=world["jenny"], title_id=1, state="seen"
    )
    assert world["module"].state.write_log == [
        {"user": JENNY_JF, "item": "jf-1", "played": True}
    ]
    assert "jf-1" in world["module"].state.played[JENNY_JF]
    assert "jf-1" not in world["module"].state.played[PATRICK_JF], "the other account is untouched"


def _duplicate_of(module, item_id: str, new_id: str) -> dict:
    """A second item for the same film: a "Movies 4K" library, or a second rip."""
    original = next(i for i in module.ITEMS if i["Id"] == item_id)
    return {**original, "Id": new_id}


async def test_a_duplicate_copy_cannot_erase_an_explicit_seen(db, world, monkeypatch):
    """Reconciled per item, a second unplayed copy would adopt `unseen` over the person's action."""
    module = world["module"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])

    await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    assert "jf-1" in module.state.played[PATRICK_JF]
    assert "jf-1b" not in module.state.played[PATRICK_JF], "only one copy was written"

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"
    assert report.adopted == 0


async def test_a_duplicate_copy_marked_in_jellyfin_is_adopted(db, world, monkeypatch):
    """Played on *any* copy means watched."""
    module = world["module"]
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])
    module.state.played[PATRICK_JF].add("jf-1b")

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"
    # The pointer must not follow the Played flag: `playback.observe` resolves sessions against it.
    # Elected once per sweep: keep the live current value, else the lowest live id (§7.1).
    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1"


async def test_an_action_taken_during_the_sweep_is_pushed_not_adopted(db, world):
    """The library is read once; `user_title` live. A tap in between is newer than the snapshot."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    # Agree first, so the row has a jf_synced_at and would otherwise take the adopt branch.
    await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    world["module"].state.played[PATRICK_JF].discard("jf-1")   # Jellyfin says not played
    # …and the person re-asserts it after the snapshot would have been taken.
    await db.execute(
        "UPDATE user_title SET state = 'seen', state_changed_at = now() + interval '1 hour' "
        "WHERE user_id = $1 AND title_id = 1",
        world["patrick"],
    )

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert report.adopted == 0
    assert report.pushed == 1
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"


async def test_an_owed_write_for_a_vanished_title_is_reported(db, world):
    """A deleted title leaves a push nothing will settle; counting it makes the gap known."""
    # A title the app knows and Jellyfin does not list — deleted from the library, or renamed
    # into a folder the scan no longer reaches.
    await db.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id) "
        "VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone')"
    )
    await seen.set_state(
        db, None, JellyfinConfig(), user_id=world["patrick"], title_id=4, state="seen"
    )
    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )
    assert report.owed_unreachable == 1


async def test_a_link_with_no_token_is_not_promoted_to_linked(db, world):
    """A link with no token pushes nothing, so a clean sweep over it is not evidence."""
    await db.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' WHERE id = $1", world["patrick"]
    )
    await save_jellyfin(db, url="http://jellyfin.test", api_key=world["module"].API_KEY)

    await seen.sync_all(db, world["client"])
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", world["patrick"]
    ) == "needs_relink"


async def test_an_unreachable_jellyfin_does_not_promote_a_broken_link(db, world):
    await db.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' WHERE id = $1", world["patrick"]
    )
    await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): world["token"]},
    )
    report = await seen.sync_all(db, JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2))
    assert report.completed == []
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", world["patrick"]
    ) == "needs_relink"


async def test_a_member_whose_own_reads_fail_is_named_rather_than_swallowed(
    db, world, caplog, monkeypatch
):
    """Member reads fail independently of the keyless library read, so a failing member is named in
    `failed_users`. Loud once, then DEBUG, and not via the server-wide `_outage` memo."""
    monkeypatch.setattr(seen, "_failed_users_logged", frozenset())
    await _store_connector(db, world, tokens={str(world["patrick"]): world["token"]})
    await db.execute(
        "UPDATE app_user SET jellyfin_user_id = 'jf-user-ghost' WHERE id = $1", world["patrick"]
    )

    with caplog.at_level(logging.DEBUG, logger="spielplan.sync.seen"):
        report = await seen.sync_all(db, world["client"])
        loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.getMessage() for r in loud] == ["seen sync for patrick failed: GET /Items -> 404"]

        await seen.sync_all(db, world["client"])
        still_loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert still_loud == loud, (
            "the account is gone until somebody re-maps it, so every later sweep meets the same "
            f"404; that is a state and not news: {[r.getMessage() for r in still_loud]}"
        )

    assert report.users == ["patrick"], "the member is still linked; the sweep still tried"
    assert report.completed == []
    assert report.failed_users == ["patrick"]
    assert (report.pushed, report.adopted, report.unchanged) == (0, 0, 0)
    assert report.push_failed == 0, (
        "a read that never happened is not a failed Played write -- §7.3 counts those separately "
        "and widening either into the other loses the distinction §6.6's card renders"
    )
    assert report.as_dict()["failed_users"] == ["patrick"], "the card reads the dict, not the object"


class _DatabaseClockBehind:
    """`sync_user` takes its sweep boundary from the database alone, so the skew is injected there."""

    def __init__(self, conn, skew_seconds: float) -> None:
        self._conn = conn
        self._skew = skew_seconds

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchval(self, query, *args, **kwargs):
        if query == "SELECT now()":
            return await self._conn.fetchval(
                "SELECT now() - make_interval(secs => $1)", self._skew
            )
        return await self._conn.fetchval(query, *args, **kwargs)


async def test_the_sweep_boundary_ignores_this_processs_clock(db, world):
    """The boundary must be Postgres `now()`, like `state_changed_at`. Ten seconds behind makes every
    row read as acted-during-the-sweep, so the Jellyfin change is pushed back, not adopted."""
    world["module"].state.write_log.clear()
    user = _linked(world)

    async def reconcile(conn, title_id: int, item_id: str) -> seen.SyncReport:
        await db.execute("UPDATE title SET jellyfin_id = $2 WHERE id = $1", title_id, item_id)
        await seen.set_state(
            db, world["client"], world["cfg"], user_id=world["patrick"], title_id=title_id,
            state="seen",
        )
        # Someone opens Jellyfin and marks it unwatched — a genuine Jellyfin-side change, made
        # after the app and Jellyfin last agreed.
        world["module"].state.played[PATRICK_JF].discard(item_id)
        world["module"].state.write_log.clear()
        report = seen.SyncReport()
        await seen.sync_user(conn, world["client"], user, report)
        return report

    honest = await reconcile(db, 1, "jf-1")
    assert (honest.adopted, honest.pushed) == (1, 0), (
        "the change predates the sweep, so it is Jellyfin's and must be adopted"
    )
    assert (await _state(db, world["patrick"], 1))["state"] == "unseen"
    assert world["module"].state.write_log == [], "nothing should have been pushed back"

    # Under a skewed boundary every present row reads as acted-during, so the first row goes first.
    await db.execute("DELETE FROM user_title WHERE user_id = $1", world["patrick"])

    skewed = await reconcile(_DatabaseClockBehind(db, 10), 2, "jf-2")
    assert (skewed.adopted, skewed.pushed) == (0, 1), (
        "with the boundary ten seconds in the past the same row reads as a mid-sweep action and is "
        "pushed -- which is only observable if the boundary comes from the database"
    )
    assert (await _state(db, world["patrick"], 2))["state"] == "seen"
    assert world["module"].state.write_log == [
        {"user": PATRICK_JF, "item": "jf-2", "played": True}
    ]


# `push_owed` is `set_state`'s network half on its own; `retract` is the compensating write.


async def _owe(db, user_id: int, title_id: int, state: str) -> None:
    """`jf_synced_at = NULL`: the debt `push_owed` settles."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at) "
        "VALUES ($1, $2, $3, now(), NULL)",
        user_id, title_id, state,
    )


async def test_push_owed_pushes_the_state_the_app_already_committed(db, world):
    """From the row, not an argument. The fake refuses the admin key, so a fallback would fail."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await _owe(db, world["patrick"], 1, "seen")

    pushed, reason = await seen.push_owed(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1
    )
    assert (pushed, reason) == (True, None)
    assert world["module"].state.write_log == [
        {"user": PATRICK_JF, "item": "jf-1", "played": True}
    ]
    assert (await _state(db, world["patrick"], 1))["jf_synced_at"] is not None

    # And the other direction, from the same function with nothing but the row changed.
    await db.execute(
        "UPDATE user_title SET state = 'unseen', jf_synced_at = NULL WHERE title_id = 1"
    )
    pushed, reason = await seen.push_owed(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1
    )
    assert pushed is True
    assert "jf-1" not in world["module"].state.played[PATRICK_JF]


async def test_push_owed_finds_nothing_owed_where_there_is_no_row(db, world):
    """The caller prints this reason into §6.7's rail verbatim, so it has to be true."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    pushed, reason = await seen.push_owed(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1
    )
    assert (pushed, reason) == (False, "nothing owed")
    assert world["module"].state.write_log == []
    assert await _state(db, world["patrick"], 1) is None, "reading the debt must not invent one"


async def test_push_owed_refuses_for_the_same_four_reasons_set_state_did(db, world):
    """The refusal strings are printed verbatim by the rail. For an unreadable DEK, "Jellyfin not
    configured" would be a lie: the connector is configured (M4.7 dd03)."""
    patrick, jenny = world["patrick"], world["jenny"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await _owe(db, patrick, 1, "seen")
    await _owe(db, patrick, 2, "seen")
    await _owe(db, jenny, 1, "seen")

    assert await seen.push_owed(
        db, None, JellyfinConfig(), user_id=patrick, title_id=1
    ) == (False, "Jellyfin not configured")
    assert await seen.push_owed(
        db, None, JellyfinConfig(url="http://jellyfin.test", secrets_unreadable=True),
        user_id=patrick, title_id=1,
    ) == (False, SECRETS_UNREADABLE_REASON)
    # Title 2 carries no `jellyfin_id`: the app knows the film and the server does not.
    assert await seen.push_owed(
        db, world["client"], world["cfg"], user_id=patrick, title_id=2
    ) == (False, "not on Jellyfin")
    pushed, reason = await seen.push_owed(
        db, world["client"], world["cfg"], user_id=jenny, title_id=1
    )
    assert pushed is False and "not linked" in reason

    assert world["module"].state.write_log == [], "nothing refused may have reached the server"
    for user_id, title_id in ((patrick, 1), (patrick, 2), (jenny, 1)):
        row = await _state(db, user_id, title_id)
        assert row["state"] == "seen" and row["jf_synced_at"] is None, (
            "every refusal leaves the write owed rather than lost (§3.3)"
        )


async def test_set_state_is_still_the_write_and_then_the_push(db, world):
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result == {"state": "seen", "synced": True, "reason": None}
    assert world["module"].state.write_log == [
        {"user": PATRICK_JF, "item": "jf-1", "played": True}
    ]
    # `push_owed` reads the row, so the pushed state and the written one cannot disagree.
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"


async def test_retract_puts_back_exactly_the_flag_the_forward_action_set(db, world):
    """No prior row means no flag of ours to put back: Played = false would erase real history (for a
    series, every episode). `_stamp` names the row only `WHERE state = $3`."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    patrick = world["patrick"]

    await _owe(db, patrick, 1, "seen")
    pushed, reason = await seen.retract(
        db, world["client"], world["cfg"], user_id=patrick, title_id=1, prior_state="seen"
    )
    assert (pushed, reason) == (True, None)
    assert world["module"].state.write_log[-1] == {
        "user": PATRICK_JF, "item": "jf-1", "played": True
    }
    # `retract` never writes `user_title`: `undo` has already restored it.
    row = await _state(db, patrick, 1)
    assert row["state"] == "seen" and row["jf_synced_at"] is not None

    await db.execute("DELETE FROM user_title WHERE user_id = $1 AND title_id = 1", patrick)
    world["module"].state.write_log.clear()
    pushed, reason = await seen.retract(
        db, world["client"], world["cfg"], user_id=patrick, title_id=1, prior_state=None
    )
    assert (pushed, reason) == (False, "no prior state to put back")
    assert world["module"].state.write_log == [], (
        "the app's absence was pushed over Jellyfin's history (§7.3, decision 210)"
    )
    assert "jf-1" in world["module"].state.played[PATRICK_JF], "the flag it already held stands"
    assert await _state(db, patrick, 1) is None, (
        "an absence was turned into an explicit `unseen` assertion (§7.3)"
    )


async def test_retract_asks_for_a_re_link_where_the_old_private_copy_said_nothing(db, world):
    """§7.3: a 401 on the retraction also asks for a re-link."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    patrick = world["patrick"]
    await _owe(db, patrick, 1, "unseen")

    tokenless = JellyfinConfig(url="http://jellyfin.test", api_key=world["module"].API_KEY)
    pushed, reason = await seen.retract(
        db, world["client"], tokenless, user_id=patrick, title_id=1, prior_state="seen"
    )
    assert pushed is False and "re-link" in reason
    assert world["module"].state.write_log == [], "the admin key was never tried"
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", patrick
    ) == "needs_relink"

    await db.execute("UPDATE app_user SET jellyfin_link_state = 'linked' WHERE id = $1", patrick)
    expired = JellyfinConfig(
        url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(patrick): "expired-token"},
    )
    pushed, reason = await seen.retract(
        db, world["client"], expired, user_id=patrick, title_id=1, prior_state="seen"
    )
    assert pushed is False and "re-link" in reason
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", patrick
    ) == "needs_relink"
    # A retraction that could not be made leaves the debt standing.
    assert (await _state(db, patrick, 1))["jf_synced_at"] is None


async def test_retract_refuses_without_a_client_and_without_a_jellyfin_id(db, world):
    """No connector, or a title the server does not carry: nothing to retract, and no error."""
    patrick = world["patrick"]
    await _owe(db, patrick, 1, "unseen")
    assert await seen.retract(
        db, None, JellyfinConfig(), user_id=patrick, title_id=1, prior_state="seen"
    ) == (False, "Jellyfin not configured")
    assert await seen.retract(
        db, None, JellyfinConfig(url="http://jellyfin.test", secrets_unreadable=True),
        user_id=patrick, title_id=1, prior_state="seen",
    ) == (False, SECRETS_UNREADABLE_REASON)
    assert await seen.retract(
        db, world["client"], world["cfg"], user_id=patrick, title_id=1, prior_state="seen"
    ) == (False, "not on Jellyfin")
    assert world["module"].state.write_log == []


# A tokenless link is created on purpose; it must not take the dead-token `break`.


async def test_a_tokenless_link_adopts_the_whole_library_and_never_stops_at_an_owed_row(db, world):
    """The owed row is FIRST in `/Items` order, where the sweep used to stop."""
    module, patrick = world["module"], world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen")
    module.state.played[PATRICK_JF].update({"jf-2", "jf-6"})
    await _store_connector(db, world)

    report = await seen.sync_all(db, world["client"])

    assert report.adopted == 2, (
        "nothing after the owed row was reconciled: the sweep stopped at the first title this "
        f"member had marked in the app ({report.as_dict()})"
    )
    assert report.owed_no_token == 1
    assert report.completed == ["patrick"], "a link with no token is incomplete, not broken"
    assert module.state.write_log == [], "the admin key was never tried (§7.3, §14 risk 3)"
    owed = await _state(db, patrick, 1)
    assert owed["state"] == "seen" and owed["jf_synced_at"] is None, (
        "the write is owed, not lost: a later sign-in settles it"
    )
    assert (await _state(db, patrick, 2))["state"] == "seen"
    assert (await _state(db, patrick, 6))["state"] == "seen"
    assert await _link_state(db, patrick) == "needs_relink"
    assert report.needs_relink == ["patrick"]


async def test_an_owed_title_that_left_the_library_does_not_promote_the_link(db, world):
    """The only owed write is for a title Jellyfin no longer lists, so the token is never exercised."""
    patrick = world["patrick"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id) "
        "VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone')"
    )
    await db.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' WHERE id = $1", patrick
    )
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=4, state="seen")

    report = await seen.sync_all(db, world["client"])

    assert report.owed_unreachable == 1
    assert (report.pushed, report.wrote) == (0, set())
    assert await _link_state(db, patrick) == "needs_relink"


class _PlayedWriteFails(httpx.AsyncBaseTransport):
    """404 (a server below 10.9), 500 and a timeout: none is a 401, so none may prompt a re-link."""

    def __init__(self, inner: httpx.AsyncBaseTransport, status: int | None) -> None:
        self._inner = inner
        self._status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "UserPlayedItems" in request.url.path:
            if self._status is None:
                raise httpx.ReadTimeout("the media server did not answer", request=request)
            return httpx.Response(self._status, json={"error": "no"})
        return await self._inner.handle_async_request(request)


async def test_a_failed_played_write_is_counted_and_the_sweep_says_it_is_incomplete(
    db, world, fake_jellyfin
):
    """Without the counter a dead push direction printed the same line as a quiet healthy sweep."""
    module, transport = fake_jellyfin
    patrick = world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await _store_connector(db, world, tokens={str(patrick): world["token"]})

    for status in (404, 500, None):
        await db.execute("DELETE FROM user_title")
        module.state.write_log.clear()
        await db.execute(
            "UPDATE app_user SET jellyfin_link_state = 'linked' WHERE id = $1", patrick
        )
        await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen")
        broken = JellyfinClient(
            "http://jellyfin.test", module.API_KEY,
            transport=_PlayedWriteFails(transport, status),
        )

        report = await seen.sync_all(db, broken)

        assert report.push_failed == 1, f"{status}: {report.as_dict()}"
        assert report.push_errors, f"{status}: the reason is not reported anywhere"
        assert report.pushed == 0
        assert report.completed == [], f"{status}: a sweep that could not write is not complete"
        assert await _link_state(db, patrick) == "linked", (
            f"{status}: a missing route, a 500 and a timeout are not credential problems (§7.3)"
        )
        assert (await _state(db, patrick, 1))["jf_synced_at"] is None, (
            f"{status}: the debt must stand, or nothing will ever retry it"
        )


async def test_a_server_below_the_pin_counts_a_failed_push_and_never_a_re_link(
    db, world, monkeypatch
):
    """§7.1 pins Jellyfin >= 10.9. Counted as a failed push, never a re-link: no password adds a route."""
    module, patrick = world["module"], world["patrick"]
    monkeypatch.setattr(module, "SERVER_VERSION", "10.8.13")
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen")

    report = await seen.sync_all(db, world["client"])

    assert report.push_failed == 1
    assert any(
        "10.8.13" in reason and "UserPlayedItems" in reason for reason in report.push_errors
    ), f"the refusal has to name the route and the version: {report.push_errors}"
    assert module.state.write_log == [], "the refusal never reached the network"
    assert await _link_state(db, patrick) == "linked"
    stored = await load_jellyfin(db)
    assert (stored.server_version, stored.server_supported) == ("10.8.13", False)


class _PlayedWriteGate(httpx.AsyncBaseTransport):
    """The divergence is an ordering problem, so one writer is stopped mid-flight."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, arrived: asyncio.Event, release: asyncio.Event
    ) -> None:
        self._inner = inner
        self._arrived = arrived
        self._release = release

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "UserPlayedItems" in request.url.path:
            self._arrived.set()
            await self._release.wait()
        return await self._inner.handle_async_request(request)


async def test_two_explicit_actions_at_once_leave_the_app_and_jellyfin_agreeing(
    db, pg_url, world, fake_jellyfin
):
    """The first phone's push is held on the wire until the second's app-side write commits. The
    per-(user, title) lock and a conditional stamp together close it."""
    module, transport = fake_jellyfin
    patrick = world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    arrived, release = asyncio.Event(), asyncio.Event()
    first_phone = JellyfinClient(
        "http://jellyfin.test", module.API_KEY,
        transport=_PlayedWriteGate(transport, arrived, release),
    )

    # One connection per phone (one asyncpg connection is not concurrent), and `db` to observe.
    phone_a = await asyncpg.connect(pg_url)
    phone_b = await asyncpg.connect(pg_url)
    try:
        first = asyncio.create_task(
            seen.set_state(
                phone_a, first_phone, world["cfg"], user_id=patrick, title_id=1, state="seen"
            )
        )
        await asyncio.wait_for(arrived.wait(), 5)
        # The first phone's row is committed and its push is on the wire, holding the lock.
        second = asyncio.create_task(
            seen.set_state(
                phone_b, world["client"], world["cfg"], user_id=patrick, title_id=1, state="unseen"
            )
        )
        while (await _state(db, patrick, 1))["state"] != "unseen":
            await asyncio.sleep(0.01)
        # Bounded: the fixed code makes the second phone wait for the lock, so it cannot get there first.
        for _ in range(50):
            if module.state.write_log:
                break
            await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, second)
    finally:
        await phone_a.close()
        await phone_b.close()

    row = await _state(db, patrick, 1)
    played = "jf-1" in module.state.played[PATRICK_JF]
    assert (row["state"] == "seen") == played, (
        f"the app says {row['state']} and Jellyfin says played={played}: both writers stamped an "
        "agreement that never happened, and the next sweep will revert the person (§7.3)"
    )
    assert row["state"] == "unseen", "the later of the two explicit actions is the one that stands"
    assert row["jf_synced_at"] is not None, (
        "the push that agreed with the row is the one that stamped it (§7.3's loop guard)"
    )

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)
    assert report.adopted == 0, "the following sweep must change neither side"
    assert (await _state(db, patrick, 1))["state"] == "unseen"


async def test_unseen_clears_every_copy_and_two_sweeps_leave_it_unseen(db, world, monkeypatch):
    """The push must reach every copy, or the next sweep's OR-collapse adopts `seen` back. Two sweeps."""
    module, patrick = world["module"], world["patrick"]
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])
    module.state.played[PATRICK_JF].update({"jf-1", "jf-1b"})
    await _store_connector(db, world, tokens={str(patrick): world["token"]})

    first = await seen.sync_all(db, world["client"])
    assert first.adopted == 1, "the household had watched it, so the app adopts that first"
    assert sorted(
        r["jellyfin_id"]
        for r in await db.fetch("SELECT jellyfin_id FROM title_jellyfin_item WHERE title_id = 1")
    ) == ["jf-1", "jf-1b"], "both copies are mapped to the one title (§7.1)"

    # The person says they have not seen it after all.
    result = await seen.set_state(
        db, world["client"], await load_jellyfin(db), user_id=patrick, title_id=1, state="unseen"
    )
    assert result["synced"] is True
    assert "jf-1" not in module.state.played[PATRICK_JF]
    assert "jf-1b" not in module.state.played[PATRICK_JF], (
        "the duplicate still reads Played, and the next sweep will adopt it back (§7.3)"
    )

    for sweep in (1, 2):
        report = await seen.sync_all(db, world["client"])
        assert report.adopted == 0, f"sweep {sweep} reverted the person: {report.as_dict()}"
        assert (await _state(db, patrick, 1))["state"] == "unseen"


async def test_the_representative_pointer_survives_six_sweeps_of_two_users_on_two_copies(
    db, both_linked, monkeypatch
):
    """`playback.observe` resolves sessions against `title.jellyfin_id`, so it must not move. Sampled
    per `sync_user` call: a flip is invisible from outside the sweep."""
    world = both_linked
    module = world["module"]
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])
    module.state.played[PATRICK_JF].add("jf-1")
    module.state.played[JENNY_JF].add("jf-1b")
    cfg = await load_jellyfin(db)
    users = await seen.linked_users(db, cfg)
    assert len(users) == 2

    pointers, relinked = [], []
    for _sweep in range(3):
        for user in users:
            report = seen.SyncReport()
            await seen.sync_user(db, world["client"], user, report)
            pointers.append(await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1"))
            relinked.append(report.resolve["relinked"])

    assert len(set(pointers)) == 1, f"the pointer moved: {pointers}"
    assert relinked[1:] == [0, 0, 0, 0, 0], (
        "a second live copy is not a re-link. §7.1 means a rebuilt library, which is the case where "
        f"the OLD id is gone -- counting every copy made §6.6's card claim one every sweep: {relinked}"
    )


async def test_marking_a_series_unseen_is_app_only_and_is_not_re_owed(db, world):
    """Decision 210(a): the stored id is the Series folder, and MarkUnplayed would reset every episode.
    The stamp stops the row owing that write for ever."""
    module, patrick = world["module"], world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-6' WHERE id = 6")

    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=6, state="unseen"
    )
    assert result == {"state": "unseen", "synced": True, "reason": "series unseen is app-only"}
    assert module.state.write_log == [], "a DELETE on a Series folder is a recursive MarkUnplayed"
    row = await _state(db, patrick, 6)
    assert row["state"] == "unseen"
    assert row["jf_synced_at"] is not None, "unstamped, the sweep would re-owe this row for ever"

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)
    assert (report.pushed, report.adopted) == (0, 0)
    assert module.state.write_log == []
    assert (await _state(db, patrick, 6))["state"] == "unseen"


async def test_a_series_marked_unseen_in_the_app_is_not_re_adopted_from_the_folder_flag(db, world):
    """Decision 213: the folder stays played by design after an app-side unseen, so `jf_synced_at`
    records a settlement, not an agreement, and must not be adopted over."""
    module, patrick = world["module"], world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-6' WHERE id = 6")

    # Played on Jellyfin's own account: the app never writes a series (decision 532).
    module.state.played[PATRICK_JF].add("jf-6")

    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=6, state="unseen"
    )
    assert result["reason"] == "series unseen is app-only"
    assert "jf-6" in module.state.played[PATRICK_JF], "and Jellyfin still says played, for ever"

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)

    assert report.adopted == 0, "the folder flag is not evidence about a row the app never sent"
    assert (await _state(db, patrick, 6))["state"] == "unseen"

    # Twice: the disagreement is permanent by design and every sweep meets it.
    await seen.sync_user(db, world["client"], _linked(world), seen.SyncReport())
    assert (await _state(db, patrick, 6))["state"] == "unseen"


async def test_a_series_stays_seen_when_a_new_episode_recomputes_the_folder_flag(db, world):
    """Decision 210(4): Jellyfin computes a series' Played, so it turns false when an episode lands;
    a computed folder flag may mark but never un-mark."""
    module, patrick = world["module"], world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-6' WHERE id = 6")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=6, state="seen"
    )
    assert result == {"state": "seen", "synced": True, "reason": "series seen is app-only"}
    assert module.state.write_log == [], (
        "decision 532: a POST on a Series folder is a recursive MarkPlayed that marks every "
        "remaining episode played and zeroes its resume point"
    )
    assert (await _state(db, patrick, 6))["jf_synced_at"] is not None

    # A new episode arrives and the folder flag is recomputed to false. Nobody acted.
    module.state.played[PATRICK_JF].discard("jf-6")

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)

    assert report.adopted == 0
    assert (await _state(db, patrick, 6))["state"] == "seen"
    assert module.state.write_log == [], "and no DELETE was sent to the Series id either"


def test_a_childless_series_folder_is_never_read_as_played():
    """`playedCount >= totalCount` is true at zero. Pure: the fake's `/Items` carries no child count."""
    resolved = resolve.ResolveReport(
        items={"jf-6": 6, "jf-7": 7}, kinds={6: "series", 7: "series"}
    )
    empty = {
        "Id": "jf-6", "Type": "Series", "ChildCount": 0,
        "UserData": {"Played": True, "UnplayedItemCount": 0},
    }
    watched = {
        "Id": "jf-7", "Type": "Series", "RecursiveItemCount": 18,
        "UserData": {"Played": True, "UnplayedItemCount": 0, "PlayedPercentage": 100.0},
    }

    collapsed = seen._collapse([empty, watched], resolved)

    assert collapsed[6] == (["jf-6"], False), "an empty folder's Played flag is nobody's opinion"
    assert collapsed[7] == (["jf-7"], True), "a watched show is still adopted"


async def test_the_sweep_does_not_adopt_while_a_finish_prompt_is_open(db, world):
    """Decision 211: the sweep must not adopt Played while a prompt is open, or a sync counts as a reply."""
    module, patrick = world["module"], world["patrick"]
    module.state.played[PATRICK_JF].add("jf-1")
    await db.execute(
        "INSERT INTO playback_event (source, title_id, user_id, finished, progress, prompt_state) "
        "VALUES ('jellyfin', 1, $1, true, 0.96, 'armed')",
        patrick,
    )

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)

    assert report.adopted == 0
    assert await _state(db, patrick, 1) is None, (
        "the sweep answered the question the app was in the middle of asking (decision 211)"
    )
    assert await db.fetchval("SELECT prompt_state FROM playback_event") == "armed"

    # The guard is the open prompt alone: once closed the same sweep adopts.
    await db.execute("UPDATE playback_event SET prompt_state = 'dismissed'")
    answered = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), answered)
    assert answered.adopted == 1
    assert (await _state(db, patrick, 1))["state"] == "seen"


def _a_page_short_of_its_own_count(request: httpx.Request) -> httpx.Response:
    """The server says six and hands back one; nothing else in the sweep can tell."""
    if request.url.path == "/System/Info/Public":
        return httpx.Response(200, json={"Version": "10.10.3"})
    return httpx.Response(200, json={
        "Items": [{"Id": "jf-1", "Type": "Movie", "ProviderIds": {"Imdb": "tt0113277"}}],
        "TotalRecordCount": 6,
    })


async def test_a_sweep_that_could_not_read_the_library_un_owns_nothing(db, world, monkeypatch):
    """Three gates: an unreachable server, a read that resolved nothing, and a TRUNCATED read (which
    aborts in the client). A bug here un-owns the whole library."""
    module = world["module"]
    await db.execute("UPDATE title SET is_owned = true, jellyfin_id = 'jf-' || id::text")
    await db.execute(
        "INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ('jf-1', 1)"
    )
    await _store_connector(db, world, tokens={str(world["patrick"]): world["token"]})
    owned_before = await db.fetchval("SELECT count(*) FROM title WHERE is_owned")
    assert owned_before == len(TITLES)

    outage = await seen.sync_all(db, JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2))
    assert outage.unowned == 0
    assert await db.fetchval("SELECT count(*) FROM title WHERE is_owned") == owned_before
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 1, (
        "the copy map was pruned against a read that never happened"
    )

    truncated = await seen.sync_all(db, JellyfinClient(
        "http://jellyfin.test", "k",
        transport=httpx.MockTransport(_a_page_short_of_its_own_count),
    ))
    assert truncated.unowned == 0, (
        "a library read that stopped one row into six un-owned the other five (§7.2)"
    )
    assert await db.fetchval("SELECT count(*) FROM title WHERE is_owned") == owned_before
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 1
    assert truncated.completed == [], "and it must not read as a sweep that finished"

    monkeypatch.setattr(module, "ITEMS", [])
    empty = await seen.sync_all(db, world["client"])
    assert empty.unowned == 0
    assert await db.fetchval("SELECT count(*) FROM title WHERE is_owned") == owned_before
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 1


async def test_a_title_removed_from_jellyfin_is_no_longer_owned(db, world):
    """`owned_checked_at` moves with it, and the copy map is pruned in the same pass."""
    patrick = world["patrick"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id, is_owned, owned_checked_at) "
        "VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone', true, now() - interval '2 days')"
    )
    await db.execute(
        "INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ('jf-gone', 4)"
    )
    await _store_connector(db, world, tokens={str(patrick): world["token"]})

    report = await seen.sync_all(db, world["client"])

    assert report.unowned == 1
    row = await db.fetchrow("SELECT is_owned, owned_checked_at FROM title WHERE id = 4")
    assert row["is_owned"] is False
    assert row["owned_checked_at"] > await db.fetchval("SELECT now() - interval '1 hour'"), (
        "the falsification is itself a re-derivation and has to be dated like one"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM title_jellyfin_item WHERE jellyfin_id = 'jf-gone'"
    ) == 0
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 1") is True


async def test_a_title_gone_from_the_library_is_un_owned_with_nobody_linked(db, world):
    """Decision 413: the library pass reads with the admin key, so it runs with nobody linked."""
    await db.execute("UPDATE app_user SET jellyfin_user_id = NULL, jellyfin_link_state = NULL")
    await db.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id, is_owned, owned_checked_at) "
        "VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone', true, now() - interval '2 days')"
    )
    await _store_connector(db, world)

    report = await seen.sync_all(db, world["client"])

    assert report.skipped_no_link is True, "no member was swept, and the report still says so"
    assert report.unowned == 1, report.as_dict()
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 4") is False
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 1") is True, (
        "the flip back on is the same pass, and it needs no member either"
    )


async def test_a_tap_on_a_title_the_library_dropped_is_refused_rather_than_sent(db, world):
    """`_targets` falls back to `title.jellyfin_id`, which is not cleared; the app already knows the
    title is gone, so the tap is refused instead of 404ing a dead id."""
    patrick, module = world["patrick"], world["module"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id, is_owned) "
        "VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone', true)"
    )
    await db.execute(
        "INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ('jf-gone', 4)"
    )
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.sync_all(db, world["client"])
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 4") is False
    module.state.write_log.clear()

    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=4, state="unseen"
    )

    assert result == {"state": "unseen", "synced": False, "reason": "not on Jellyfin"}
    assert module.state.write_log == [], "and no round trip was spent on the dead item id"
    assert (await _state(db, patrick, 4))["state"] == "unseen", "§3.3: the tap is kept either way"


async def test_a_second_sweep_while_one_is_running_does_nothing(db, pg_url, world):
    """An advisory lock is per session, so the second sweep needs its own connection."""
    await _store_connector(db, world, tokens={str(world["patrick"]): world["token"]})
    other = await asyncpg.connect(pg_url)
    try:
        assert await other.fetchval(
            "SELECT pg_try_advisory_lock($1, $2)", seen._SWEEP_LOCK, 0
        ) is True
        blocked = await seen.sync_all(db, world["client"])
    finally:
        await other.execute("SELECT pg_advisory_unlock_all()")
        await other.close()

    assert blocked.already_running is True
    assert (blocked.users, blocked.adopted, blocked.pushed) == ([], 0, 0)
    assert blocked.as_dict()["already_running"] is True, "the admin card has to be able to say so"

    # With the lock gone the same sweep runs: the guard is the lock.
    again = await seen.sync_all(db, world["client"])
    assert again.already_running is False
    assert again.users == ["patrick"]


class _ProbeBlocked(httpx.AsyncBaseTransport):
    """`/System/Info/Public` is the route guides tell people to block; `/Items` still answers."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/System/Info/Public":
            return httpx.Response(404, json={"error": "no"})
        return await self._inner.handle_async_request(request)


def _said(caplog, level: int) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.name == "spielplan.sync.seen" and r.levelno >= level
    ]


async def test_a_blocked_version_probe_is_not_an_unreachable_jellyfin(
    db, world, fake_jellyfin, caplog, monkeypatch
):
    """A blocked probe is not an unreachable server; the library read is the authority. A real outage
    is still one WARNING and one INFO. `_outage` is process-global, hence the patch."""
    module, transport = fake_jellyfin
    monkeypatch.setattr(seen._outage, "since", None)
    await _store_connector(db, world, tokens={str(world["patrick"]): world["token"]})
    blocked = JellyfinClient(
        "http://jellyfin.test", module.API_KEY, transport=_ProbeBlocked(transport)
    )

    with caplog.at_level(logging.DEBUG, logger="spielplan.sync.seen"):
        for _sweep in range(3):
            report = await seen.sync_all(db, blocked)
        assert report.users == ["patrick"], "the sweep itself ran: the library read is fine"
        assert _said(caplog, logging.INFO) == [], (
            "a server that answered every read this sweep made was reported as down, and back"
        )

        caplog.clear()
        dead = JellyfinClient("http://127.0.0.1:1", "k", timeout=0.2)
        for _sweep in range(3):
            await seen.sync_all(db, dead)
        warned = _said(caplog, logging.WARNING)
        assert len(warned) == 1 and "unreachable" in warned[0], warned

        caplog.clear()
        await seen.sync_all(db, blocked)
        assert any("reachable again" in line for line in _said(caplog, logging.INFO)), (
            "and the recovery line is the library read's to make, not the probe's"
        )


async def test_a_tap_holding_the_title_lock_is_owed_and_not_a_failed_played_write(
    db, pg_url, world, monkeypatch
):
    """A write never attempted because the sweep could not take the title lock is owed, not failed:
    `push_failed` is for persistently broken writes. The second sweep settles it."""
    patrick = world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen")
    # The sweep's lock budget is 20 x 100 ms; two tries is the same contention in 100 ms.
    monkeypatch.setattr(seen, "_PUSH_LOCK_TRIES", 2)

    other = await asyncpg.connect(pg_url)
    try:
        assert await other.fetchval(
            "SELECT pg_try_advisory_lock($1, hashtext($2)::int)",
            seen._PUSH_LOCK, f"{patrick}:1",
        ) is True
        contended = await seen.sync_all(db, world["client"])
    finally:
        await other.execute("SELECT pg_advisory_unlock_all()")
        await other.close()

    assert contended.push_failed == 0, (
        f"a tap in flight was counted beside a 404 and a 500: {contended.as_dict()}"
    )
    assert contended.push_errors == []
    assert contended.completed == ["patrick"], "nothing failed, so the member's sweep is complete"
    assert (await _state(db, patrick, 1))["jf_synced_at"] is None, "the debt stands"

    settled = await seen.sync_all(db, world["client"])
    assert settled.pushed == 1, "the next sweep settles it, which is why it was never a failure"
    assert (await _state(db, patrick, 1))["jf_synced_at"] is not None


async def test_unseen_clears_the_live_copy_when_the_other_one_left_the_library(
    db, world, monkeypatch
):
    """A 404 on one copy of a multi-copy DELETE is a copy that has gone, not a failed unseen; the
    first dead id must not abort the live copies behind it."""
    module, patrick = world["module"], world["patrick"]
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])
    module.state.played[PATRICK_JF].update({"jf-1", "jf-1b"})
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.sync_all(db, world["client"])
    assert sorted(
        r["jellyfin_id"]
        for r in await db.fetch("SELECT jellyfin_id FROM title_jellyfin_item WHERE title_id = 1")
    ) == ["jf-1", "jf-1b"]

    # The deleted rip sorts first in the map until the next sweep prunes it.
    monkeypatch.setattr(module, "ITEMS", [i for i in module.ITEMS if i["Id"] != "jf-1"])

    result = await seen.set_state(
        db, world["client"], await load_jellyfin(db), user_id=patrick, title_id=1, state="unseen"
    )

    assert result == {"state": "unseen", "synced": True, "reason": None}, result
    assert "jf-1b" not in module.state.played[PATRICK_JF], (
        "one dead item id stopped the write to the copy the household still has"
    )
    assert (await _state(db, patrick, 1))["jf_synced_at"] is not None, "so nothing is still owed"


async def test_the_copy_map_is_pruned_before_the_sweep_pushes_against_it(db, world, monkeypatch):
    """The prune runs once the library read completes, before the per-user loop, so no request is
    spent on an id this sweep knows is gone."""
    module, patrick = world["module"], world["patrick"]
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.sync_all(db, world["client"])
    await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="unseen")
    monkeypatch.setattr(module, "ITEMS", [i for i in module.ITEMS if i["Id"] != "jf-1"])

    attempted: list[str] = []
    written = world["client"].set_played

    async def recording(item_id, jf_user_id, played, token):
        attempted.append(item_id)
        return await written(item_id, jf_user_id, played, token)

    monkeypatch.setattr(world["client"], "set_played", recording)
    report = await seen.sync_all(db, world["client"])

    assert attempted == ["jf-1b"], (
        f"the sweep pushed at an item id its own library read had just dropped: {attempted}"
    )
    assert report.push_failed == 0, report.as_dict()
    assert await db.fetchval(
        "SELECT count(*) FROM title_jellyfin_item WHERE jellyfin_id = 'jf-1'"
    ) == 0
