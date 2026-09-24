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

import ast
import asyncio
import logging
import re
from collections import Counter
from pathlib import Path

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


async def _link_state(db, user_id):
    return await db.fetchval("SELECT jellyfin_link_state FROM app_user WHERE id = $1", user_id)


async def _store_connector(db, world, *, tokens: dict[str, str] | None = None):
    """The connector as the admin saved it, which is what `sync_all` reads.

    `tokens=None` stores a link with no per-user credential — the state `api/admin.py` creates on
    purpose ("A link with no token is real but incomplete: it attributes playback and feeds the
    P(seen) prior") and the one §7.3's adopt-only sweep exists for.
    """
    return await save_jellyfin(
        db, url="http://jellyfin.test", api_key=world["module"].API_KEY, user_tokens=tokens or {}
    )


def _linked(world, *, token: str | None = "") -> seen.LinkedUser:
    """Patrick as `linked_users` would build him, with the token the case under test needs."""
    return seen.LinkedUser(
        world["patrick"], "patrick", PATRICK_JF,
        world["token"] if token == "" else token, "linked",
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
    """§7.3's badge is cleared on evidence of a Played write that succeeded, and nothing weaker.

    The negative half is asserted first because it is the half that was wrong. A sweep sends the
    token only for rows with `jf_synced_at IS NULL`, so a sweep with nothing owed exercises the
    token *never* — and this test used to pin exactly that sweep as sufficient evidence, under a
    comment in `sync_all` claiming the opposite. A revoked token therefore read green on §6.6's
    card, and by finding 1's rules an account reading "linked" with no usable credential is the
    state that then froze that member's sweep at their next tap. [finding 2]
    """
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

    # Now the person marks something while Jellyfin is unreachable, so the next sweep owes a write
    # and has to make it. That write is the evidence.
    await seen.set_state(
        db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen"
    )
    report = await seen.sync_all(db, world["client"])
    assert (report.pushed, report.wrote) == (1, {"patrick"})
    assert await _link_state(db, patrick) == "linked"


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
    # …and the pointer did **not** follow the Played flag, which is the half of this rule that was
    # wrong. Following it wrote `title.jellyfin_id` to the copy *this* user had played, once per
    # user per sweep: with two members on two copies the measured sequence across three sweeps was
    # `jf-1, jf-1b, jf-1, jf-1b, jf-1, jf-1b`, and `playback.observe` resolves a session against
    # that one column, so §7.3's finish prompt was lost for whichever member was not on the winning
    # copy — permanently, because the user order is fixed. It is elected once per sweep now, from
    # the copies themselves: keep the live current value, else the lowest live id. [§7.1, finding 5]
    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1"


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


async def test_a_member_whose_own_reads_fail_is_named_rather_than_swallowed(
    db, world, caplog, monkeypatch
):
    """The half of finding 3 that arrives through the other door: the library answered, the
    member did not.

    §7.2's library read is keyless and each member's read is not, so the two fail independently.
    A Jellyfin account deleted or renamed 404s that member's `/Items` for ever while the
    household's read keeps succeeding — and `sync_all` caught the exception per member, logged a
    warning and moved on, leaving `users` full, `completed` short and every counter zero,
    `push_failed` included. §6.6's card keyed "unreachable" on an empty `users`, so it printed
    "pushed 0 - adopted 0 - unchanged 0" in green for a sweep in which neither direction of §7.3
    ran for anybody. `failed_users` is the fact that was missing; the shortening of `completed`
    was already there and is not legible on its own. [review cycle 1: seen-02; §3.3, §6.6]

    The second sweep asserts ops-15's half of the same finding: this fault is permanent until the
    mapping is corrected, so a WARNING per sweep is 96 lines a day for one fact. It is loud once
    and DEBUG after -- and deliberately not routed through `_note_unreachable`, whose memo belongs
    to the whole server: holding that open here would swallow the first WARNING of a real outage.
    """
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
    """The test's connection with `SELECT now()` answering in the past, and nothing else changed.

    The seam, not the process clock. `sync_user` takes its sweep boundary from the database and
    from nowhere else, so this is the only place a skew can be injected at all — which is exactly
    the fact the test below measures. Every other statement is delegated untouched, including the
    resolver's own reads and writes.
    """

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
    """§7.3's loop rule turns on one comparison: was the row changed before this sweep started,
    or during it? Before → the disagreement came from Jellyfin and is adopted. During → the
    person just acted, and their action is pushed rather than overwritten.

    `state_changed_at` is stamped by Postgres `now()`, so taking the sweep's snapshot from
    `datetime.now(UTC)` compares two clocks — and they are not the same clock. Measured on this
    machine, Postgres sits 78 ms from the app process; another run of this project measured
    220 ms the other way. Both signs fail silently. Server ahead: a change that came from
    Jellyfin is never adopted. Server behind: an action taken *during* the sweep looks older
    than the snapshot and gets adopted, reverting what the person just did.

    This used to be asserted by monkeypatching `seen.datetime` with `raising=False` on a module
    that imports no `datetime`, which is a patch with no seam behind it: running the body without
    it gave the identical result, so the test could not fail. The skew now goes in where the code
    actually reads the boundary — one statement on the connection — and the proof is that moving it
    **changes the answer**. Ten seconds behind makes every present row read as acted-during-the-sweep,
    so the genuine Jellyfin-side change below is pushed back instead of adopted. A `sync_user` that
    took its snapshot from this process's clock would be unmoved by this proxy and would keep
    answering (1, 0) for both halves. [tq3]
    """
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

    # The first scenario's row is removed before the second: under a skewed boundary *every* present
    # row reads as acted-during-the-sweep, which is the whole point, so leaving it behind would have
    # the second half counting two pushes and asserting nothing about its own title.
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


# --- M4.10 finding 11: the two halves of the explicit action, apart -----------------------------
#
# `set_state` is the app-side write and then the network call. `rate/session.py` awaited the
# whole of it inside the verdict transaction, so a Jellyfin taking 1.5 s held the row locks for
# 1.5 s. `push_owed` is the second half on its own, and `retract` is the compensating write that
# `rate/session.py` used to keep a private, drifted copy of.


async def _owe(db, user_id: int, title_id: int, state: str) -> None:
    """The app-side write exactly as an observation's transaction leaves it.

    `jf_synced_at = NULL` is §7.3's table reading "the person acted and Jellyfin has not been
    told yet" — the debt `push_owed` settles and the sweep would settle eventually.
    """
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at) "
        "VALUES ($1, $2, $3, now(), NULL)",
        user_id, title_id, state,
    )


async def test_push_owed_pushes_the_state_the_app_already_committed(db, world):
    """§7.3's mapping, from the row rather than from an argument: `seen` -> Played = true, under
    the linked user's own token. The fake refuses the admin key on that route, so a fallback to
    it would fail here."""
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
    """§7.3: "an absent row is the *default*, not an assertion." There is nothing to push, and
    saying so is not the same as failing to push — the caller's `_sync_line` prints this reason
    into §6.7's rail verbatim, so it has to be true."""
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    pushed, reason = await seen.push_owed(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1
    )
    assert (pushed, reason) == (False, "nothing owed")
    assert world["module"].state.write_log == []
    assert await _state(db, world["patrick"], 1) is None, "reading the debt must not invent one"


async def test_push_owed_refuses_for_the_same_four_reasons_set_state_did(db, world):
    """The refusal strings are a published interface: §6.7's rail prints them and `_sync_line`
    does not translate them. Moving the network half into its own function must not reword one.

    The unreadable-DEK case is the one that matters most and is the one that would rot quietly:
    "Jellyfin not configured" is a lie an admin cannot act on, because the connector *is*
    configured and its credentials are sealed under a SECRETS_KEY this process does not have
    (M4.7 dd03).
    """
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
    """The composition, asserted where it used to be an argument about reading order.

    `set_state` keeps its contract exactly — the dict, the owed row, the push — and what has
    changed is only that the second half has a name its other caller can reach for.
    """
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=world["patrick"], title_id=1, state="seen"
    )
    assert result == {"state": "seen", "synced": True, "reason": None}
    assert world["module"].state.write_log == [
        {"user": PATRICK_JF, "item": "jf-1", "played": True}
    ]
    # The state that reached Jellyfin is the one the first statement wrote, not one carried past
    # it: `push_owed` reads the row, so the two can no longer disagree.
    assert (await _state(db, world["patrick"], 1))["state"] == "seen"


async def test_retract_puts_back_exactly_the_flag_the_forward_action_set(db, world):
    """Decision 35's compensating write, as a function of the prior state alone.

    A prior `seen` means we set Played false and must set it back to true. **No prior row at all
    means there is no flag of ours to put back**, and sending Played = false would be the one row
    this module's own header says never happens — "push the app's absence over Jellyfin's history",
    reached from the Undo direction instead of the first-sync one. Decision 210 and `data-10`: for a
    series that DELETE is a recursive MarkUnplayed over every episode, and for a movie it erases a
    watch the household may have recorded in Jellyfin years ago. The accepted consequence is that
    Jellyfin keeps the flag the forward action set and §7.3's next sweep adopts it, which decision
    172(1) rules correct — an absent app row is a default, and Jellyfin history is real history.

    The row is seeded holding the state being put back, because that is what `observations.undo`
    leaves behind (it restores `user_title` byte for byte) and because the stamp is conditional on
    it now: `_stamp` names the row only `WHERE state = $3`, so a pair that cannot occur in
    production does not get stamped either. [finding 4]
    """
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
    # The row is stamped as agreed; its own state is left exactly as found — `retract` never writes
    # `user_title`, because `undo` has already restored it.
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
    """§7.3: "a 401 on write -> re-link prompt" — on the retraction as much as on the push.

    `rate/session.py._compensate_push` was a second implementation of this path and it had lost
    both of `_push`'s refusal branches: a link with no usable token and a token Jellyfin rejects
    both returned silently, so a token that expired between a tap and its Undo left
    `jellyfin_link_state` reading `linked` with nothing anywhere asking the person to fix it.
    """
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
    # The restored row is untouched by a retraction that could not be made: the debt stands.
    assert (await _state(db, patrick, 1))["jf_synced_at"] is None


async def test_retract_refuses_without_a_client_and_without_a_jellyfin_id(db, world):
    """The two cases the old copy got right, kept: no connector and a title the server does not
    carry are both "nothing to retract", and neither is an error the person can act on."""
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


# --- M4.11: the link state the sweep used to freeze on -----------------------------------------
#
# `api/admin.py` creates a link with no per-user token on purpose: it attributes playback and feeds
# §3.3's P(seen) prior. §7.3 forbids the admin key as a write fallback, so such a member simply has
# nothing to push with — and the sweep took the *dead-token* `break` on that, which meant the first
# title they marked in the app stopped their whole sweep, at every sweep, for ever.


async def test_a_tokenless_link_adopts_the_whole_library_and_never_stops_at_an_owed_row(db, world):
    """§7.3 + §3.3, finding 1. The owed row is the FIRST title in `/Items` order on purpose: that
    is where the sweep used to stop, so everything after it in the page-set is the evidence.

    What must be true at once: the member's Jellyfin history arrives (that is what the link is for),
    the owed write stays owed rather than being abandoned or faked, the count is reported where
    §6.6's card can print it, the admin key is never tried, and the sweep counts as completed — a
    half-made link is not an outage.
    """
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
    """The third way a sweep can look healthy while proving nothing: the only row it owed a write
    for is a title Jellyfin no longer lists, so the token is never exercised. `owed_unreachable`
    already counted that case; the badge was promoted anyway. [finding 2]"""
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


# --- M4.11: a failed push is a counted fact, not silence ----------------------------------------


class _PlayedWriteFails(httpx.AsyncBaseTransport):
    """The fake Jellyfin with §7.3's one write broken in a way that is not a credential problem.

    404 is the case §7.1's pin is about — a server below 10.9 has no `/UserPlayedItems` route at all
    — and 500 and a timeout are the proxy and the outage. None of the three is a 401, so none of
    them may produce a re-link prompt: sending the household to re-type a password cannot add a
    route to their media server.
    """

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
    """cs-05. Before this counter existed, `SyncReport` had no way to say the app->Jellyfin
    direction was dead: the non-auth branch logged one warning and `sync_user` carried on, so
    §6.6's card printed `pushed 0 - adopted 0 - unchanged N` — which is also what a healthy quiet
    sweep prints — and the promotion at the foot of `sync_all` restored a flagged link to healthy.
    """
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
    """§7.1: "Pin Jellyfin >= 10.9". The pin was a sentence in a manual test button — computed,
    returned to the browser and discarded — while `/UserPlayedItems` is the 10.9 route, so a 10.8
    install 404ed every write for the life of the install with every visible check passing (cs-39).

    The sweep re-probes at its head, stores the verdict beside the URL, and the write then refuses
    locally and by name. Counted as a failed push, never as a re-link: no password can add a route.
    """
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


# --- M4.11: two phones, one title --------------------------------------------------------------


class _PlayedWriteGate(httpx.AsyncBaseTransport):
    """The fake with §7.3's one write held open until the test lets it through.

    A real interleaving rather than an argued one: the divergence under test is an ordering problem
    between two writers, and the only way to pin the order is to stop one of them mid-flight.
    """

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
    """§7.3's conflict rule, raced on two connections. [finding 4]

    Two phones, or one impatient double tap. The app row was written in one order and Jellyfin
    reached in the other, and both pushes stamped `jf_synced_at` unconditionally — so the row ended
    up "present + stamped + disagrees", which the table at the top of `sync/seen.py` reads as a
    Jellyfin-side change. The next sweep therefore *reverted the person*, and stamped a fresh
    `state_changed_at`, leaving nothing in the row to recover the intent from.

    Measured here in the failing order: the first phone's push is held on the wire until the second
    phone's app-side write has committed. The repair is the per-(user, title) session lock plus a
    stamp conditional on the row still holding the pushed value, and it is the pair that closes it:
    the last push to run re-reads the row under the lock, and the one interleaving left over leaves
    the stamp NULL, which §7.3 already reads as a debt the sweep settles.
    """
    module, transport = fake_jellyfin
    patrick = world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    arrived, release = asyncio.Event(), asyncio.Event()
    first_phone = JellyfinClient(
        "http://jellyfin.test", module.API_KEY,
        transport=_PlayedWriteGate(transport, arrived, release),
    )

    # Three connections: one per phone, because two coroutines on one asyncpg connection are not
    # concurrent, and `db` left free to observe both without racing either.
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
        # ...and then its push is given every chance to reach the server before the first one's
        # does, because that ordering — the later value written to the database, the earlier one
        # written to Jellyfin — IS the fault. Bounded, because the repair is precisely that it
        # cannot get there first: the second phone is waiting for the lock the first one holds, so
        # an unbounded wait would hang on the fixed code instead of measuring it.
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
    """dd17-duplicate-copy. §7.3: "App is authoritative for explicit user actions", on the exact
    library `_collapse`'s own docstring is written for — "Movies" and "Movies 4K".

    The push went to `title.jellyfin_id` alone, so the other copy kept `Played = true`; the next
    sweep OR-collapsed the copies, read "present + stamped + disagrees" and adopted `seen` straight
    back over the person. Two sweeps, because the first one is where it happened and the second is
    where a household would notice it had happened twice.
    """
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
    """dd17-representative-pointer. `title.jellyfin_id` is the deep link and the single-write
    representative (§7.1), and it has to be stable: `playback.observe` resolves a `/Sessions` row
    against that one column, so a pointer that moves loses §7.3's finish prompt for whoever is not
    on the copy it moved to.

    It moved every user sweep, because it was written to the copy *this* user had played — measured
    sequence with two members on two copies, `jf-1, jf-1b, jf-1, jf-1b, jf-1, jf-1b`. Sampled per
    `sync_user` call rather than per sweep, because a sweep ends on whichever member went last and
    the flip is invisible from outside it.
    """
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


# --- M4.11: decision 210, a series is asymmetric -----------------------------------------------


async def test_marking_a_series_unseen_is_app_only_and_is_not_re_owed(db, world):
    """Decision 210(a) and `dd05-series-unseen`. `ITEM_TYPES = "Movie,Series"`, so the stored id is
    the Series FOLDER item, and Jellyfin's `Folder.MarkUnplayed` iterates every recursive non-folder
    child and resets `Played`, `PlayCount`, `PlaybackPositionTicks` and `LastPlayedDate` on each.
    The app has no episode identity (§4.1 rule 5) and cannot restore any of it, so one tap on "not
    seen" would erase a household's progress through six seasons.

    The stamp is what keeps the decision affordable: without it the row owes a write for ever and
    every sweep re-refuses it.
    """
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
    """Decision 213, and the composition decisions 210(a) and 210(4) were never posed together.

    The sibling above never puts the series in the fake's played set, so Jellyfin AGREES with the
    app's unseen and the un-marking guard answers it. This is the configuration a household is
    actually in: they finished the show in Jellyfin, so the folder is played, and then they marked
    it not-seen in the app to queue a rewatch — or declined this milestone's finish prompt, which
    decision 211 makes the same write. Decision 210(a) means the app sent Jellyfin nothing, so the
    folder stays played by design and `jf_synced_at` records a settlement rather than an
    agreement; reading it as an agreement made the disagreement look like a newer human change,
    and the sweep reverted the tap fifteen minutes later with no in-app remedy.
    """
    module, patrick = world["module"], world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-6' WHERE id = 6")

    await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=6, state="seen"
    )
    assert "jf-6" in module.state.played[PATRICK_JF], "the POST is decision 210(b)'s half"

    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=6, state="unseen"
    )
    assert result["reason"] == "series unseen is app-only"
    assert "jf-6" in module.state.played[PATRICK_JF], "and Jellyfin still says played, for ever"

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)

    assert report.adopted == 0, "the folder flag is not evidence about a row the app never sent"
    assert (await _state(db, patrick, 6))["state"] == "unseen"

    # Twice, because the first sweep leaving it alone would mean nothing if the second re-armed
    # the adoption: the disagreement here is permanent by design and every sweep meets it again.
    await seen.sync_user(db, world["client"], _linked(world), seen.SyncReport())
    assert (await _state(db, patrick, 6))["state"] == "unseen"


async def test_a_series_stays_seen_when_a_new_episode_recomputes_the_folder_flag(db, world):
    """`dd05-series-played-recompute`, and decision 210(4): a computed folder flag may mark but
    never un-mark.

    Jellyfin does not store Played on a Series; `Folder.FillUserDataDtoValues` computes
    `playedCount >= totalCount`. So the flag turns false the day Season 2 lands with nobody having
    acted — and the adopt branch's whole premise is that a disagreement is a human change made after
    the last agreement. An explicit "seen" became "unseen" on the day a show continued.
    """
    module, patrick = world["module"], world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-6' WHERE id = 6")
    result = await seen.set_state(
        db, world["client"], world["cfg"], user_id=patrick, title_id=6, state="seen"
    )
    assert result["synced"] is True
    assert module.state.write_log == [
        {"user": PATRICK_JF, "item": "jf-6", "played": True}
    ], "decision 210(b): 'seen' on a series still POSTs"

    # A new episode arrives and the folder flag is recomputed to false. Nobody acted.
    module.state.played[PATRICK_JF].discard("jf-6")
    module.state.write_log.clear()

    report = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), report)

    assert report.adopted == 0
    assert (await _state(db, patrick, 6))["state"] == "seen"
    assert module.state.write_log == [], "and no DELETE was sent to the Series id either"


def test_a_childless_series_folder_is_never_read_as_played():
    """Decision 210's tail. `dto.Played = playedCount >= totalCount` is `true` when `totalCount` is
    zero, so an empty or not-yet-scanned series folder reports "played" with nobody having watched
    anything — and adopting that writes `seen` onto a show the household has never started.

    Pure, and deliberately not driven through the fake: `ops/fake_jellyfin.py` answers `/Items` with
    the projection a real server sends for the field set this app asks for, which carries no child
    count at all. The shapes below are the ones Jellyfin does send when it sends one.
    """
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


# --- M4.11: decision 211, the sweep does not answer the question the app is asking --------------


async def test_the_sweep_does_not_adopt_while_a_finish_prompt_is_open(db, world):
    """Decision 211 (option B) and §7.3:315, "Jellyfin playback is a suggestion, never a silent
    write". Coverage row `jellyfin-acquisition-eval-playback-arms-prompt-never-writes` says "only
    the user's explicit tap writes seen", and its own registered test asserted the opposite: the
    sweep adopted Jellyfin's auto-set Played for a title with no `user_title` row, and
    `playback.pending` then closed the armed prompt as `answered`. For a linked member the prompt
    window was at most one sweep, "offers the verdict flow" never happened, and §13's capture rate
    counted a sync as a reply. [spec-14]
    """
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

    # The guard is the open prompt and nothing else: once it is closed the same sweep adopts.
    await db.execute("UPDATE playback_event SET prompt_state = 'dismissed'")
    answered = seen.SyncReport()
    await seen.sync_user(db, world["client"], _linked(world), answered)
    assert answered.adopted == 1
    assert (await _state(db, patrick, 1))["state"] == "seen"


# --- M4.11: §7.2, ownership is re-derived in both directions ----------------------------------


def _a_page_short_of_its_own_count(request: httpx.Request) -> httpx.Response:
    """A `/Items` page that stops far short of the count on that same page.

    A reverse proxy answering one page with an interstitial, a filter applied after the query, a
    clamping gateway: the server says the library holds six and hands back one, and until review
    cycle 2 `all_items` returned that one with no error at all. Everything else answers normally,
    which is the whole point -- this is not an outage and nothing else in the sweep can tell.
    """
    if request.url.path == "/System/Info/Public":
        return httpx.Response(200, json={"Version": "10.10.3"})
    return httpx.Response(200, json={
        "Items": [{"Id": "jf-1", "Type": "Movie", "ProviderIds": {"Imdb": "tt0113277"}}],
        "TotalRecordCount": 6,
    })


async def test_a_sweep_that_could_not_read_the_library_un_owns_nothing(db, world, monkeypatch):
    """The negative case first, because this is the most destructive statement in the module: a bug
    here un-owns the household's whole library, empties §6.2's candidate pool and strips the owned
    Home shelves. §7.2's "never trusted stale" is not a licence to act on a read that did not
    happen.

    Three gates, and all three are asserted. An unreachable server returns before the ownership
    pass at all; a read that resolved nothing is refused inside it, because a library that resolves
    to nothing is a failure mode and never a household that owns no films; and a read that came
    back TRUNCATED aborts the sweep in the client.

    The third was the hole. The docstring above this statement claimed `all_items` "raises rather
    than truncating for exactly this reason", and it did not: `TotalRecordCount` was only ever used
    to stop the walk early, so a page shorter than `Limit` ended it however many rows that same
    page said were left. Measured before the fix, against this fixture: a read of 1 of 6 left
    `report.unowned == 4`, dropped five `title_jellyfin_item` rows, and cut Tonight's pool to the
    one title that survived the cut -- with `completed` full, `failed_users` empty and the admin
    card green. The only gate this statement has of its own is the empty resolution, and a
    truncated-but-non-empty read passes it.
    [M4.11 review cycle 2: m411-rev2-resolve-01, m411-rev2-jf-01]
    """
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
    """§7.2: "mark removed titles `is_owned = false` ... re-derived from Jellyfin, never trusted
    stale". Nothing anywhere wrote false (cs-11): a film deleted from the library kept `is_owned`,
    stayed in §6.2's candidate pool and on the owned Home shelves, and its winner card deep-linked
    to an item id the server no longer has.

    `owned_checked_at` moves with it, because that column is what lets a later sweep tell "still
    owned" from "not looked at since", and the copy map is pruned in the same pass or §7.3's
    "unseen clears every copy" keeps sending DELETEs to a dead item id.
    """
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
    """Decision 413. §3.3 makes the Jellyfin link optional, and `sync_all` returned at
    `skipped_no_link` BEFORE the library pass -- a pass that reads with the admin key and needs no
    member at all. So on an install that had saved the connector and linked nobody, a film the
    household deleted stayed owned, in §6.2's pool, Tonight's pool and the owned shelves, and a
    title the spine marked unowned was never flipped back on. M5.2 turned that live: both intake
    jobs gate on `make_client` alone and stage 1 mints `is_owned` from the item's own `Id`, so
    that install manufactured exactly the rows nothing could ever falsify. The docstring beside
    the gate filed the repair under a spec amendment that says nothing about the gate. The pass
    now runs first, gated exactly as before; only the per-member half needs a member.
    [review cycle 3: m52-c3-own-04]
    """
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
    """The other end of the same prune, one tap later.

    `_falsify_ownership` un-owns the title and empties its copy map, but nothing clears
    `title.jellyfin_id` — and `_targets` appends that pointer unconditionally as its last
    fallback, so an "unseen" tap on a film Jellyfin no longer has spent a round trip on a dead
    item id and returned the raw transport line. `TitleDetail.svelte` renders `res.reason`
    verbatim, so the sentence under the title read "DELETE /UserPlayedItems/jf-gone -> 404". The
    app already knew: it un-owned the title in the same sweep that emptied the map.
    [review cycle 1: M411-REV-03; §7.2]
    """
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


# --- M5.2: the ownership half stays where M4.11 built it (decision 362) ------------------------


PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"
# The optional quotes are not defensive spelling. `importer/load.py:511` builds its SET list as
# `f'"{c}" = EXCLUDED."{c}"'` over a column list that includes `is_owned`, so the quoted identifier
# is the form this package's own generated SQL emits -- and generating the SET list from a column
# list is how both of its UPDATEs are written (`resolve.py:248`, `load.py:511`). A guard that read
# only the bare spelling would miss a delta path re-deriving ownership in the house idiom.
#
# AND THE ROW-ASSIGNMENT FORM, which the first alternative cannot see at all: in
# `SET (is_owned, owned_checked_at) = (false, now())` the column name is followed by a comma
# and the `=` belongs to the tuple. That is not an exotic spelling -- it is
# `_falsify_ownership`'s own two-column write (`seen.py:1180`) with one comma moved, so it is
# the shape a delta-side falsifier would most plausibly arrive in. Read as a SECOND
# ALTERNATIVE rather than by loosening the first, because `is_owned` inside a parenthesised
# list is a write only when that list is the target of a SET: an INSERT column list names it
# the same way, and `stages._mint`'s INSERT is the statement the docstring below spends a
# paragraph explaining this guard does not report. [review cycle 2: m52-rev2-own-02]
_OWNERSHIP_WRITE = re.compile(
    r'(?:"?\bis_owned\b"?\s*=|\bset\s*\([^)]*\bis_owned\b[^)]*\)\s*=)', re.IGNORECASE
)
# The copy map's DELETE, in the spellings `test_landmine_guards.TITLE_DELETE` already accepts for
# the sibling table: the optional `ONLY`, an optional schema qualifier, optional quotes. That guard
# names `title_jellyfin_item` as a DERIVED table it must not flag, so this is the only guard that
# covers it, and there is no argument for it reading fewer spellings of one statement than the
# guard one file over reads of the other.
_COPY_MAP_DELETE = re.compile(
    r"delete\s+from\s+(?:only\s+)?(?:\w+\s*\.\s*)?\"?title_jellyfin_item\"?(?![\w\"])",
    re.IGNORECASE,
)


def _statements_matching(source: str, pattern: re.Pattern[str]) -> Counter[str]:
    """How many executable SQL literals matching `pattern` each function in one module holds.

    An ast walk over string literals and not a grep, because the clause "mark removed titles
    `is_owned = false`" is quoted in this package's prose repeatedly -- in `_falsify_ownership`'s
    own docstring, in `ResolveReport`'s field comment, in `acquire/stages._mint`'s argument for
    deriving the flag -- and a grep would either report all of it or have to be taught to tell an
    argument from a statement. A docstring is the one string a module never executes, so it is the
    one this skips; a comment is not a string at all and the parser never offers it.

    Deliberately not `test_landmine_guards._sql_literals`, which is the same idea one file over:
    that helper returns the literals and discards where they were, and where they were is most of
    the answer here. An f-string's spans are left unjoined for the same reason --
    `UPDATE title SET is_owned = {flag}` has to be reported, and it is reported by its first span.

    COUNTED AND NOT COLLECTED, which is the rest of it. A set of locations says that a named
    function may hold such a statement and nothing at all about how many it holds, so a second
    falsifier added as a branch INSIDE `connectors/resolve.upsert_item` -- which is what an author
    obeying "do not re-implement falsification" literally would write -- returns a set identical
    to the shipped one. The cost is that the count is a measurement of spans and not of
    statements: an f-string spelling the column in two spans counts twice by the rule above. That
    is the right way round for a tripwire, because it goes red asking to be looked at rather than
    quiet. [review cycle 2: m52-rev2-own-03]
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    functions = [
        (node.lineno, node.end_lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or not pattern.search(node.value):
            continue
        # The innermost enclosing def, which is the one a reader would blame. Chosen by the latest
        # start line rather than by walk order: `ast.walk` is breadth-first and says nothing at all
        # about nesting.
        enclosing = [span for span in functions if span[0] <= node.lineno <= span[1]]
        found[max(enclosing)[2] if enclosing else "<module level>"] += 1
    return found


def _package_statements(pattern: re.Pattern[str]) -> Counter[tuple[str, str]]:
    totals: Counter[tuple[str, str]] = Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        module = path.relative_to(PACKAGE).as_posix()
        counted = _statements_matching(path.read_text(encoding="utf-8"), pattern)
        for name, held in counted.items():
            totals[(module, name)] += held
    return totals


def test_no_second_path_learns_to_write_the_ownership_column():
    """Decision 362: neither of §7.2's two trigger paths writes `is_owned`, ever.

    §7.2's third bullet made both responsible for removals as well until v2.1.2, and neither can
    discharge it: a read filtered on `DateCreated > last_sync` is an add detector by construction
    and never observes an absence, and one `ItemAdded` says nothing about what the library no
    longer holds. So the rule is asserted from the NEW path's side, which is the only side that
    can regress. `test_a_sweep_that_could_not_read_the_library_un_owns_nothing` above already
    holds the shipped falsifier's three gates behaviourally, and a second falsifier bolted onto a
    delta poll or a webhook would pass every test in this file. This paragraph used to conclude
    that no behavioural test could hold it, and exit check 11 is one: its CI twin runs both
    feeders over an owned household and diffs the ownership columns
    (`test_jellyfin_intake.py::test_neither_feeder_writes_the_ownership_column_...`). This guard
    reads STATEMENTS, and a new caller of the shipped writers writes none, which is why
    `test_no_second_caller_reaches_the_shipped_ownership_writers` reads callers beside it.
    [review cycle 3: m52-c3-own-03]

    Two STATEMENTS OF THIS SPELLING, both M4.11's, both named, AND COUNTED -- because the count
    is the half a set of locations cannot hold. The violation that survives a location set is a
    second falsifier added as a BRANCH INSIDE `resolve.upsert_item`, which is what an author told
    "do not re-implement falsification" writes when they obey it literally, and which a set of
    locations reports as unchanged. [review cycle 2: m52-rev2-own-03] That is a narrower claim than "the
    only code that writes the column", which a walk over string literals cannot make and which
    this set must not be read as making. `resolve.upsert_item`'s `is_owned = true` is the
    re-derivation §7.2 asks for and is also the whole of "flips back when re-added" -- the flip
    back on is the ordinary sweep resolving the item again, not a path of its own.
    `seen._falsify_ownership`'s `is_owned = false` is what `acquire/stages._mint` calls "the ONE
    statement in the codebase that can un-own a title". The copy map's single DELETE is held
    beside them because `prune_missing_items` refuses on the same evidence for the same reason,
    and §7.3's "unseen clears every copy" breaks the moment a delta read may prune it.

    TWO FURTHER PATHS WRITE THE COLUMN AND NO LITERAL WALK CAN REACH EITHER, so a reader who takes
    this set for the whole answer will mis-reason about both. `acquire/stages._mint:923-939` names
    `is_owned` in an INSERT column list with the bound value `jellyfin_id is not None`, and
    `test_acquire_pipeline.py` pins an item with no `Id`, so it writes the flag false on a tested
    input -- but a mint creates the row it writes, and a statement that cannot reach a title that
    already exists cannot un-own one, which is the harm decision 362 is about.
    `importer/load._upsert_titles:511-518` builds `"is_owned" = EXCLUDED."is_owned"` at runtime out
    of an interpolated column name, and that one CAN un-own an existing title: §10's re-import
    defaults the column to false for a bundle that omits it (`load.py:134-137`). It is argued
    there and the next sweep re-derives the flag, so it is not a falsifier in decision 362's
    sense -- but it is a third writer, and a column name that does not exist until the statement
    is built is invisible to a parser by construction.

    KNOWN LIMITS, named rather than chased: a column reached by interpolation, by `+`
    concatenation or by `%`/`.format()`, and a column named only in an INSERT list. Resolving the
    first three would make this an interpreter rather than a walker. What it does read is every
    spelling this package actually emits -- the plain literal, the literal held in a constant, the
    f-string whose column is spelled out, and the quoted identifier its generated SQL emits -- plus
    the one it does not emit and is one comma away from: PostgreSQL's row assignment,
    `SET (is_owned, owned_checked_at) = (false, now())`, in which the column name is never followed
    by `=` at all. `test_the_ownership_guard_tells_a_statement_from_the_argument_about_one` pins
    each of them. [review cycle 1: M52-REV1-OWN-02; review cycle 2: m52-rev2-own-02]
    """
    assert _package_statements(_OWNERSHIP_WRITE) == {
        ("connectors/resolve.py", "upsert_item"): 1,
        ("sync/seen.py", "_falsify_ownership"): 1,
    }, (
        "decision 362: an `is_owned =` statement has appeared outside the completed sweep, or a "
        "second one inside a function this guard already names. Neither of section 7.2's two "
        "trigger paths may write the ownership column at all"
    )
    assert _package_statements(_COPY_MAP_DELETE) == {
        ("connectors/resolve.py", "prune_missing_items"): 1,
    }, "decision 362: the copy map is pruned against a completed library read and nothing else"


def test_the_ownership_guard_tells_a_statement_from_the_argument_about_one():
    """A guard that cannot report a violation is a green line rather than a proof.

    Fed both halves of the edit decision 362 exists to prevent: a delta poll falsifying ownership
    against the window it happened to read, and the same function with the statement taken out and
    the prose left in. The second half is not a formality -- this package argues about
    `is_owned = false` in prose far more often than it writes it, so a reader that could not tell
    the two apart would have to be either permanently red or permanently useless.
    """
    poll = (
        "async def poll_new_items(conn):\n"
        '    """§7.2: mark removed titles is_owned = false, re-derived from Jellyfin."""\n'
        '    await conn.execute("UPDATE title SET is_owned = false WHERE id <> ALL($1::int[])")\n'
    )
    argument_only = poll.rsplit("    await", 1)[0]

    assert _statements_matching(poll, _OWNERSHIP_WRITE) == {"poll_new_items": 1}
    assert _statements_matching(argument_only, _OWNERSHIP_WRITE) == {}

    # The same statement in the spelling this package's own generated SQL emits. It is not a
    # hypothetical house style: `importer/load.py:511` builds `"is_owned" = EXCLUDED."is_owned"`
    # by quoting every column it was handed, and the statement it feeds is an
    # `ON CONFLICT DO UPDATE SET` that can put an owned title back to false (`load.py:134-137`).
    # A guard for the un-owning statement that read only the bare spelling would not see a delta
    # path re-derive ownership in the idiom this package already writes -- and building the SET
    # list from a column list is how both `resolve.py:248` and `load.py:511` write an UPDATE.
    quoted = (
        "async def poll_new_items(conn):\n"
        "    await conn.execute('UPDATE title SET \"is_owned\" = false WHERE id = $1')\n"
    )
    assert _statements_matching(quoted, _OWNERSHIP_WRITE) == {"poll_new_items": 1}

    # AND A SECOND STATEMENT INSIDE A FUNCTION THIS GUARD ALREADY NAMES, which is the shape a set
    # of locations cannot report at all. An author told not to re-implement falsification, obeying
    # that literally by adding the delta path's falsifier as a branch inside `resolve.upsert_item`
    # rather than as a function of its own, leaves the location set byte-identical to the shipped
    # one -- so the guard has to count the statements a named function holds and not only say that
    # it may hold one. [review cycle 2: m52-rev2-own-03]
    twice = (
        "async def upsert_item(conn):\n"
        "    await conn.execute('UPDATE title SET is_owned = true WHERE id = $1')\n"
        "    await conn.execute('UPDATE title SET is_owned = false WHERE id <> ALL($2::int[])')\n"
    )
    assert _statements_matching(twice, _OWNERSHIP_WRITE) == {"upsert_item": 2}

    # AND POSTGRESQL'S ROW-ASSIGNMENT FORM, in both column orders, because `_falsify_ownership`'s
    # own statement is one comma away from it: `seen.py:1180` writes
    # `SET is_owned = false, owned_checked_at = now()`, and `SET (is_owned, owned_checked_at) =
    # (false, now())` is the tidy spelling of that same two-column write. The column name is not
    # followed by `=` in it at all, so a guard reading only `is_owned =` would let a delta-side
    # falsifier in through the spelling the statement it guards is closest to.
    # [review cycle 2: m52-rev2-own-02]
    for statement in (
        "UPDATE title SET (is_owned, owned_checked_at) = (false, now()) WHERE id = ANY($1::int[])",
        "UPDATE title SET (owned_checked_at, is_owned) = (now(), false) WHERE id = ANY($1::int[])",
    ):
        source = f"async def poll_new_items(conn):\n    await conn.execute('{statement}')\n"
        assert _statements_matching(source, _OWNERSHIP_WRITE) == {"poll_new_items": 1}, statement

    # The copy map's half, in the spellings `test_landmine_guards.TITLE_DELETE` already pins for
    # the sibling table: the optional `ONLY`, an optional schema qualifier, optional quotes. That
    # guard names `title_jellyfin_item` as a table it must NOT flag, so this is the only guard
    # covering the copy map, and it has no reason to read fewer spellings of one statement than
    # the guard one file over reads of the other.
    for statement in (
        "DELETE FROM public.title_jellyfin_item WHERE title_id = $1",
        'DELETE FROM "title_jellyfin_item" WHERE title_id = $1',
        "DELETE FROM ONLY title_jellyfin_item WHERE title_id = $1",
    ):
        source = f"async def prune(conn):\n    await conn.execute('{statement}')\n"
        assert _statements_matching(source, _COPY_MAP_DELETE) == {"prune": 1}, statement


# The four functions that write the ownership column or the copy map when called: the two
# statements the guard above counts, and the two that reach them. Plan §9 on the most destructive
# of them: "Do not add a second caller ... and do not let the delta path near it."
_OWNERSHIP_WRITERS = frozenset({
    "upsert_item", "upsert_items", "prune_missing_items", "_falsify_ownership",
})


def _writer_references(source: str) -> Counter[tuple[str, str]]:
    """Every place one module names one of `_OWNERSHIP_WRITERS`, by (enclosing def, writer).

    A NAME AND NOT ONLY A CALL, so that the writer handed on as a value -- to `functools.partial`,
    to a list of steps, under an alias at import -- is reported where it is named, which is the
    only place a walker can see it. An import is reported at module level under the writer's own
    name, whatever it is bound to. A `def` is not a reference, and a string is not one either, so
    `__all__` and the prose that argues about these four are ignored. KNOWN LIMIT: `getattr` with
    a spelled-out name, and a caller one hop further out (a feeder calling `seen.sync_all`); both
    are the behavioural twin's to catch, which diffs the columns and does not care how they moved.
    """
    tree = ast.parse(source)
    functions = [
        (node.lineno, node.end_lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    found: Counter[tuple[str, str]] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            named = node.id
        elif isinstance(node, ast.Attribute):
            named = node.attr
        elif isinstance(node, ast.alias):
            named = node.name.rpartition(".")[2]
        else:
            continue
        if named not in _OWNERSHIP_WRITERS:
            continue
        enclosing = [span for span in functions if span[0] <= node.lineno <= span[1]]
        found[(max(enclosing)[2] if enclosing else "<module level>", named)] += 1
    return found


def test_no_second_caller_reaches_the_shipped_ownership_writers():
    """Decision 362 held against the shape the statement guard above cannot see: a new CALLER.

    That guard counts literals spelling `is_owned =`, and an author told "do not re-implement
    falsification" obeys it by reusing what ships. A delta poll that "kept ownership fresh on add"
    with `resolve.upsert_items`, then `prune_missing_items` and `seen._falsify_ownership` over what
    it read writes no literal at all -- and un-owns every title outside its fifteen-minute window
    and prunes the copy map, which on a quiet household is the whole library, while that guard
    and every behavioural test in this file stay green. So the five references the four writers
    ship with are named, and a sixth anywhere in the package is red before anything runs.

    `sync_user` keeps its own because it resolves the library itself when it is driven alone;
    `sync_all` holds the other three, the library pass decision 413 runs whether or not a member
    is linked; and `upsert_item` is reached only through `upsert_items`. The second half is this
    guard's falsifier, fed the finding's own plant and the same writer taken under an alias.
    [review cycle 3: m52-c3-own-03]
    """
    totals: Counter[tuple[str, str, str]] = Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        module = path.relative_to(PACKAGE).as_posix()
        for (function, writer), held in _writer_references(path.read_text(encoding="utf-8")).items():
            totals[(module, function, writer)] += held

    assert totals == {
        ("connectors/resolve.py", "upsert_items", "upsert_item"): 1,
        ("sync/seen.py", "sync_user", "upsert_items"): 1,
        ("sync/seen.py", "sync_all", "upsert_items"): 1,
        ("sync/seen.py", "sync_all", "prune_missing_items"): 1,
        ("sync/seen.py", "sync_all", "_falsify_ownership"): 1,
    }, (
        "decision 362: a writer of the ownership column or the copy map is named somewhere new. "
        "The completed sweep is the only path that may reach them; neither of section 7.2's two "
        "trigger paths may call them at all"
    )

    plant = (
        "from spielplan.sync.seen import _falsify_ownership as keep_fresh\n"
        "async def poll_delta(conn, items):\n"
        '    """Keeps ownership fresh on add: upsert_items, then prune_missing_items."""\n'
        "    resolved = await resolve.upsert_items(conn, items)\n"
        "    await resolve.prune_missing_items(conn, resolved)\n"
        "    await keep_fresh(conn, resolved, None)\n"
    )
    assert _writer_references(plant) == {
        ("<module level>", "_falsify_ownership"): 1,
        ("poll_delta", "upsert_items"): 1,
        ("poll_delta", "prune_missing_items"): 1,
    }


SPEC = Path(__file__).resolve().parents[2] / "docs" / "spielplan-spec_v2.1.md"
REGISTER = Path(__file__).resolve().parents[2] / "docs" / "spec-v2.2-proposals.md"


def test_the_normative_file_gives_the_ownership_column_to_the_full_sweep_alone():
    """Decision 362 from the side CLAUDE.md sends the next reader to first.

    The guard above holds the code, and the code was never the problem. §7.2's third bullet went on
    saying "Both paths enqueue an acquisition job (§8) per new title and mark removed titles
    `is_owned = false`" -- the one normative document asking for the second falsifier
    `_falsify_ownership` calls the most destructive statement in the module -- while decision 362
    handed that bullet's amendment to "the milestone that holds the spec file" and the file's own
    v2.1.1 line handed it to M5.2, so each record pointed at the other. Under "where code and spec
    disagree, the code is the bug", a reader who met the sentence without the register had a spec
    clause telling them to add the falsifier. So the bullet naming `is_owned = false` gives it to
    the full sweep and cites decision 362, and a point release in the Status block names the
    decision it landed under. [decision 362; M5.2 review cycle 3: M52-C3-PAPER-02]
    """
    text = SPEC.read_text(encoding="utf-8")
    section = re.search(r"^### 7\.2 .*?(?=^### )", text, re.M | re.S)
    assert section, "the normative file has no section 7.2 heading for this rule to read"
    bullets = [b for b in re.split(r"\n(?=- )", section.group(0)) if "is_owned = false" in b]
    assert bullets, "section 7.2 no longer says who marks a removed title `is_owned = false`"
    for bullet in bullets:
        assert not re.search(r"\bBoth paths\b[^.]*\bmark removed titles\b", bullet), (
            "section 7.2 still gives the ownership column to both intake paths, which decision "
            f"362 refuses:\n  {ascii(bullet.strip()[:300])}"
        )
        assert "full sweep" in bullet and re.search(r"\bdecision 362\b", bullet, re.I), (
            "section 7.2's ownership bullet must give `is_owned = false` to the full sweep and "
            f"cite the decision that says so:\n  {ascii(bullet.strip()[:300])}"
        )
    status = re.search(r"^\*\*Status:\*\*.*?(?=\n[ \t]*\n)", text, re.M | re.S)
    assert status, "the normative file has no Status block"
    releases = re.findall(r"^\*\*v\d+\.\d+\.\d+ \(\d{4}-\d{2}-\d{2}\):\*\*.*$", status.group(0), re.M)
    assert any(re.search(r"\bdecision 362\b", line) for line in releases), (
        "section 7.2 was amended under decision 362 and no dated point release in the Status "
        "block says so (decision 288): the date says which wave, the number what it answered to"
    )


def test_the_library_pick_bounds_acquisition_and_never_ownership():
    """Decision 364's boundary, held where a misreading of it would do decision 362's harm.

    The pick is read by the webhook's membership check and the delta walk and by nothing on the
    full sweep's path, so a title in a library the admin deselects stays owned: it has left the
    household's acquisition scope and is still on the household's server. Decision 364's heading
    nonetheless called the pick "the ownership boundary", over a body, a §12 row and a code path
    that all make it the acquisition boundary -- and in this register "ownership" is `is_owned`,
    which decision 362 two entries up reserves to the full sweep. A later milestone taking the
    heading at its word would scope the full sweep's library read by the pick, and
    `_falsify_ownership` would then un-own every title in every deselected library. So the sweep's
    two modules never read `library_ids`, and the heading names the boundary its body draws.
    [decisions 362 and 364; M5.2 review cycle 3: M52-C3-PAPER-06]
    """
    for module in ("sync/seen.py", "connectors/resolve.py"):
        assert "library_ids" not in (PACKAGE / module).read_text(encoding="utf-8"), (
            f"{module} reads the library pick, so the full sweep's ownership is now scoped by a "
            "boundary decision 364 draws for acquisition alone"
        )
    heading = re.search(r"^### 364\. .*$", REGISTER.read_text(encoding="utf-8"), re.M)
    assert heading, "the register no longer heads decision 364"
    assert "acquisition boundary" in heading.group(0), (
        "decision 364's heading names a boundary its own body does not draw:\n  "
        + ascii(heading.group(0))
    )


# --- M4.11: §5.3, two sweeps of one household cannot overlap -----------------------------------


async def test_a_second_sweep_while_one_is_running_does_nothing(db, pg_url, world):
    """§5.3's 15-minute `jellyfin-seen-sync` against §6.6's "sync now". Nothing locked them, so the
    two swept the same member against two different library snapshots and the second decided its
    adoptions from a page-set the first had already acted on. "A sweep is already running" is a
    better answer for the admin card than two sweeps.

    A second connection, because an advisory lock is held per session: taken on the same connection
    it is re-entrant and would prove nothing.
    """
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

    # And with the lock gone the same sweep runs, so the guard is the lock and not a refusal.
    again = await seen.sync_all(db, world["client"])
    assert again.already_running is False
    assert again.users == ["patrick"]


# --- M4.11 review cycle 2 -----------------------------------------------------------------------


class _ProbeBlocked(httpx.AsyncBaseTransport):
    """The fake Jellyfin with the tokenless version probe blocked and every other route intact.

    A reverse proxy that restricts unauthenticated paths, an auth gateway in front of the server,
    or simply a box slow enough to time out the first request of a sweep and answer the second.
    `/System/Info/Public` is, in `probe_version`'s own words, "the one such guides tell people to
    block", while `/Items` carries the admin key and answers perfectly.
    """

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
    """ops-15's rule composed with step 1d's re-probe, which is where the two came apart.

    `_note_unreachable` makes the first failure a WARNING and every later one a DEBUG, and
    `_note_reachable` clears the memo with an INFO on the first success. The probe and the library
    read are different requests to different routes, so a server that answers `/Items` but not
    `/System/Info/Public` SET the memo and CLEARED it inside the same sweep -- 96 WARNINGs naming
    an unreachable server and 96 INFOs saying it came back, every day, for a server that was never
    down. A false "unreachable" is worse than the flood it was meant to be part of, because it
    teaches the operator to skip the true one. A probe that cannot answer is not an unreachable
    server: the library read one statement later is the authority, and the stored verdict (§7.1)
    stands meanwhile.

    The genuine outage is asserted in the same test, because the repair must not be "stop
    reporting": a real one is still exactly one WARNING however long it lasts, and one INFO when
    it ends. `sync/seen.py`'s half of ops-15 had no test at all -- the only `_unreachable_since`
    assertion in the repo was against `sync/playback.py` -- so this is that clause registered.
    `_unreachable_since` is process-global and no test resets it, hence the monkeypatch.
    [M4.11 review cycle 2: m411-rev2-seen-02; §3.3, ops-15]
    """
    module, transport = fake_jellyfin
    monkeypatch.setattr(seen, "_unreachable_since", None)
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
    """§6.6's `push_failed` counts Played writes that FAILED, and this one was never attempted.

    A member taps "seen" at the moment the 15-minute sweep reaches that same row -- the
    interleaving `acted_during_this_sweep` routes straight into `_push_current` -- and the sweep
    cannot take the per-(user, title) lock. Nothing reached the socket and the media server
    refused nothing, but the busy path returned `PUSH_ERROR`, so §6.6's card printed "1 Played
    write(s) failed - nothing reached Jellyfin for them" in red with `data-sync-health="failing"`
    and dropped that member from `completed`, for an event in which the app and Jellyfin never
    disagreed. cs-05 created that counter so a PERSISTENTLY broken write direction would be
    visible; a self-healing race is the one thing that must not be in it.

    Owed-and-not-attempted already has a shape in this module -- `owed_no_token` keeps the member
    in `completed` -- and this is that shape. The second sweep is the half that makes it true
    rather than merely quiet: the debt stands, and the next pass settles it.
    [M4.11 review cycle 2: m411-rev2-seen-03; §6.6, §7.3]
    """
    patrick = world["patrick"]
    await db.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.set_state(db, None, JellyfinConfig(), user_id=patrick, title_id=1, state="seen")
    # The sweep's whole lock budget is 20 x 100 ms; two tries is the same contention in 100 ms.
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
    """§7.3's "unseen clears every copy", for the household that deleted one of two rips.

    `_targets` unions the copy map, the page-set and the representative under a docstring saying
    "their union is never worse than any one of them", and `_push` walked that union in ONE try
    block -- so the first dead item id aborted the write to every live copy behind it. The map is
    pruned only by a completed sweep, so between a copy disappearing and the next sweep it still
    names the dead one, and `ORDER BY jellyfin_id` puts it first as often as not. `push_owed`'s
    `checked_and_gone` refusal cannot catch this one: the title IS still owned, because the other
    copy is in the library.

    Measured before the fix: the tap returned `{'synced': False, 'reason': 'DELETE
    /UserPlayedItems/jf-1 -> 404'}` -- the raw transport line §6.7's rail prints verbatim under
    the title -- the live copy stayed Played in Jellyfin against an app row reading unseen, and
    the next sweep failed the same way. A 404 on one copy of a multi-copy DELETE is a copy that
    has gone, which is not a failed unseen.
    [M4.11 review cycle 2: m411-rev2-resolve-02; §7.3, §7.2]
    """
    module, patrick = world["module"], world["patrick"]
    monkeypatch.setattr(module, "ITEMS", [*module.ITEMS, _duplicate_of(module, "jf-1", "jf-1b")])
    module.state.played[PATRICK_JF].update({"jf-1", "jf-1b"})
    await _store_connector(db, world, tokens={str(patrick): world["token"]})
    await seen.sync_all(db, world["client"])
    assert sorted(
        r["jellyfin_id"]
        for r in await db.fetch("SELECT jellyfin_id FROM title_jellyfin_item WHERE title_id = 1")
    ) == ["jf-1", "jf-1b"]

    # The 4K rip is deleted. Until the next sweep prunes it the map still names it, and it sorts
    # first -- which is the window every tap between two sweeps falls into.
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
    """Step 2g's own reason for the prune, against step 2g's placement of it.

    "In the same pass, prune `title_jellyfin_item` rows for items no longer in the library, or 2f's
    DELETE-every-copy will target dead item ids" -- and the pass it named runs AFTER the per-user
    loop, so the loop pushed against a map the previous sweep wrote and the harm that sentence
    describes stood for one whole sweep anyway. The gate the prune needs is a completed, non-empty
    library read, which is satisfied the moment `upsert_items` returns, so the prune moved there
    and the loop now pushes against a map this sweep's own read has validated.

    The round trip is what is asserted, because with `_push` tolerating a 404 on one copy of many
    the outcome is right either way -- and spending a request per sweep on an item id the same
    sweep already knows is gone is exactly what 2g exists to stop.
    [M4.11 review cycle 2: m411-rev2-resolve-02; §7.2, §7.3]
    """
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
