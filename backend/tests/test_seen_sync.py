"""Two-way seen sync against a real (fake) Jellyfin. Spec v2.1 §7.3, §3.3, §14.3.

The three sentences under test:

  * "App is authoritative for explicit user actions."
  * "Conflict rule: last-writer-wins with the app's explicit action outranking Jellyfin's
    inferred state; `jf_synced_at` prevents loops."
  * "a 401 on write -> re-link prompt."

And the one the spec does not spell out but every household would notice: the first sync after
linking must not push the app's *absence* of state over Jellyfin's watch history. An absent
`user_title` row is the default, not an assertion.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import JellyfinConfig, load_jellyfin, save_jellyfin
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
    """A linked household: two app users, a Jellyfin with a token for one of them, a library."""
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


# --- the explicit action -------------------------------------------------------------------


async def test_the_app_side_write_never_depends_on_jellyfin(db, world):
    """§3.3: "the app must work when Jellyfin is down". No client at all — the person's action
    still lands, and the response says plainly that the media server does not know."""
    result = await seen.set_state(
        db, None, JellyfinConfig(), user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result == {"state": "seen", "synced": False, "reason": "Jellyfin not configured"}
    row = await _state(db, world["patrick"], 1)
    assert row["state"] == "seen"
    assert row["jf_synced_at"] is None, "an unsynced write stays owed, not silently forgotten"


async def test_marking_seen_writes_played_with_the_users_own_token(db, world):
    """§7.3: `seen` -> `POST /UserPlayedItems/{itemId}?userId=`, under the linked user's token.
    The fake refuses the admin key on that route, so a fallback to it would fail here."""
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
    """§3.3: linking is optional and an unlinked user functions normally.

    The title *is* on Jellyfin, so the only thing missing is the link — which is what the
    refusal has to name if the person is to do anything about it.
    """
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["jenny"], title_id=1, state="seen"
    )
    assert result["synced"] is False
    assert "not linked" in result["reason"]
    assert (await _state(db, world["jenny"], 1))["state"] == "seen"


async def test_an_invalid_state_is_refused_before_anything_is_written(db, world):
    """§4.2: two states, and there is no 'forgotten' (owner decision 2026-08-29)."""
    with pytest.raises(ValueError):
        await seen.set_state(
            db, None, JellyfinConfig(), user_id=world["patrick"], title_id=1, state="forgotten"
        )
    assert await _state(db, world["patrick"], 1) is None


# --- the loop guard ---------------------------------------------------------------------------


async def test_reading_back_our_own_write_changes_nothing(db, world):
    """The loop guard, stated as the spec states it: after the app writes, Jellyfin holds our
    value, and the next sync must see agreement and stop."""
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
    """We agreed once; the disagreement is therefore newer than that agreement, so it came
    from Jellyfin's side and the app adopts it."""
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
    """The failure this whole table exists to prevent. The household has watched things; the
    app has no rows yet; pushing that absence would wipe every Played flag on the server."""
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


# --- §7.3: the re-link path ----------------------------------------------------------------


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
    # The state is written and still owed, so a re-link settles it rather than losing it.
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


# --- the job, end to end --------------------------------------------------------------------


async def test_sync_all_skips_cleanly_when_nothing_is_configured(db, world):
    """Nothing stored in `connector_config` yet — a fresh install, not an error."""
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
    await db.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' WHERE id = $1", world["patrick"]
    )
    await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY,
        user_tokens={str(world["patrick"]): world["token"]},
    )
    await seen.sync_all(db, world["client"])
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", world["patrick"]
    ) == "linked"


# --- §3.3: unlinking ---------------------------------------------------------------------------


async def test_unlinking_forgets_the_token_and_keeps_the_seen_state(db, world):
    """§3.3: the link is optional, so removing it must leave a working account behind."""
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


# --- both users, which is what §12's exit criterion actually says ---------------------------


@pytest.fixture
async def both_linked(db, world):
    """§12 M1: "seen states flow both ways **for both users**"."""
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
    """Two people, two Jellyfin accounts, two independent answers about the same film."""
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


# --- duplicates: two Jellyfin items, one title ---------------------------------------------


def _duplicate_of(module, item_id: str, new_id: str) -> dict:
    """A second Jellyfin item for the same film — a "Movies 4K" library, or a second rip."""
    original = next(i for i in module.ITEMS if i["Id"] == item_id)
    return {**original, "Id": new_id}


async def test_a_duplicate_copy_cannot_erase_an_explicit_seen(db, world, monkeypatch):
    """The failure the collapse exists to prevent.

    Two items resolve to one title. The person marks it seen; the push lands on one copy; the
    other copy still reads not-played. Reconciled per item, the second one adopts `unseen` over
    the person's own action — and stamps `jf_synced_at`, so the corrective push is not even
    owed. Whatever order /Items returns decides the outcome.
    """
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
    """The other direction of the same rule: Played on *any* copy means watched."""
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
    # …and the title now points at the copy that was actually played, so the next explicit
    # write goes to the same item the sync reads back.
    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1b"


# --- an action taken while the sweep is running ---------------------------------------------


async def test_an_action_taken_during_the_sweep_is_pushed_not_adopted(db, world):
    """The library is read once at the start; `user_title` is read live. A tap that lands in
    between is newer than the snapshot it would be compared against."""
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


# --- debts the sweep cannot reach ------------------------------------------------------------


async def test_an_owed_write_for_a_vanished_title_is_reported(db, world):
    """A title deleted from Jellyfin leaves a push nothing will ever settle. Counting it is the
    difference between a known gap and a silent one."""
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


# --- the re-link flag is cleared on evidence, never on silence -------------------------------


async def test_a_link_with_no_token_is_not_promoted_to_linked(db, world):
    """"Nothing failed" is not evidence: a link with no token has nothing to push, so a sweep
    over it refuses no writes and would otherwise look perfectly healthy."""
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


async def test_the_sweep_boundary_ignores_this_processs_clock(db, world, monkeypatch):
    """§7.3's loop rule turns on one comparison: was the row changed before this sweep started,
    or during it? Before → the disagreement came from Jellyfin and is adopted. During → the
    person just acted, and their action is pushed rather than overwritten.

    `state_changed_at` is stamped by Postgres `now()`, so taking the sweep's snapshot from
    `datetime.now(UTC)` compares two clocks — and they are not the same clock. Measured on this
    machine, Postgres sits 78 ms from the app process; another run of this project measured
    220 ms the other way. Both signs fail silently. Server ahead: a change that came from
    Jellyfin is never adopted. Server behind: an action taken *during* the sweep looks older
    than the snapshot and gets adopted, reverting what the person just did.

    Wall-clock offsets cannot express this — any margin small enough to sit inside real skew is
    smaller than the time between two statements. So the process clock is moved instead: ten
    seconds behind, which is the failing direction. Code that reads its snapshot from the
    database does not notice. Code that reads it from here decides every row the wrong way.
    """
    from datetime import UTC, datetime, timedelta

    class SlowClock:
        @staticmethod
        def now(tz=None):
            return datetime.now(tz or UTC) - timedelta(seconds=10)

    # `raising=False`: the fixed module does not import `datetime` at all, and that is the point.
    monkeypatch.setattr(seen, "datetime", SlowClock, raising=False)

    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    # Someone opens Jellyfin and marks it unwatched — a genuine Jellyfin-side change, made
    # after the app and Jellyfin last agreed.
    world["module"].state.played[PATRICK_JF].discard("jf-1")
    world["module"].state.write_log.clear()

    report = seen.SyncReport()
    await seen.sync_user(
        db, world["client"],
        seen.LinkedUser(world["patrick"], "patrick", PATRICK_JF, world["token"], "linked"),
        report,
    )

    assert report.adopted == 1 and report.pushed == 0, (
        "the change predates the sweep, so it is Jellyfin's and must be adopted — a snapshot "
        "taken from a slow process clock makes every row look like it changed mid-sweep"
    )
    assert (await _state(db, world["patrick"], 1))["state"] == "unseen"
    assert world["module"].state.write_log == [], "nothing should have been pushed back"
