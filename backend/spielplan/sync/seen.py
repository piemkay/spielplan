"""Two-way seen-state sync (§7.3); `jf_synced_at` is the loop guard.

Absent row + Played: adopt. NULL stamp: push. Stamped and agreeing: nothing. Stamped and
disagreeing: adopt. Never under an open prompt (decision 211) or over a series row (decisions 210, 213).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import (
    JellyfinClient,
    JellyfinError,
    Outage,
    last_played_of,
    played_of,
)
from spielplan.connectors.registry import (
    SECRETS_UNREADABLE_REASON,
    JellyfinConfig,
    save_jellyfin,
)
from spielplan.home import notices, wish

log = logging.getLogger("spielplan.sync.seen")

STATES = ("seen", "unseen")

# `_push`'s outcome; `sync_user` must tell a missing token, a rejected one and an outage apart.
PUSH_OK = "ok"
PUSH_APP_ONLY = "app_only"          # decisions 210, 533: a series is settled without a write
PUSH_NO_TOKEN = "no_token"
PUSH_AUTH_FAILURE = "auth_failure"
PUSH_ERROR = "error"
PUSH_NOTHING = "nothing"            # the row went away under us; nothing is owed
# A concurrent push held the lock: never attempted, still owed, and not a §6.6 `push_failed`.
PUSH_BUSY = "busy"

# Advisory-lock namespaces (two-int form); the per-title key is hashed into the second int.
_PUSH_LOCK = 7303
_SWEEP_LOCK = 7304

# A tap waits this long for another push of the same (user, title), then leaves the debt owed.
_PUSH_LOCK_TRIES = 20
_PUSH_LOCK_WAIT_S = 0.1
PUSH_BUSY_REASON = "another write for this title is in flight"

# A local day carrying this many of one member's LastPlayedDates is a bulk mark, not plays (decision 562).
BULK_DAY = 10

# An outage logs once (§3.3). The sweep boundary stays the database's `now()`, never this clock.
_outage = Outage(log)

# Members whose own `/Items` read failed last sweep, logged on change; independent of the outage memo.
_failed_users_logged: frozenset[str] = frozenset()


@dataclass
class LinkedUser:
    app_user_id: int
    name: str
    jf_user_id: str
    # The decrypted per-user token: `repr=False` where it is declared (§14.3).
    token: str | None = field(repr=False)
    link_state: str


@dataclass
class SyncReport:
    pushed: int = 0
    adopted: int = 0
    unchanged: int = 0
    needs_relink: list[str] = field(default_factory=list)
    resolve: dict[str, Any] = field(default_factory=dict)
    users: list[str] = field(default_factory=list)
    skipped_no_link: bool = False
    # Owed writes whose title has left the library.
    owed_unreachable: int = 0
    # Owed writes for a link with no per-user token; only re-signing-in settles them (§7.3).
    owed_no_token: int = 0
    # Users whose sweep actually completed. `needs_relink` being empty is not evidence of
    # health — it is also what a Jellyfin outage looks like.
    completed: list[str] = field(default_factory=list)
    # Members whose sweep raised: without this a per-member failure read as a healthy quiet sweep.
    failed_users: list[str] = field(default_factory=list)
    # Non-credential Played write failures (a pre-10.9 server's 404, a 500, a timeout); reasons capped.
    push_failed: int = 0
    push_errors: list[str] = field(default_factory=list)
    # Members whose Played write succeeded: the only evidence that clears the re-link badge.
    wrote: set[str] = field(default_factory=set)
    # §7.2: "mark removed titles `is_owned = false` ... re-derived from Jellyfin, never trusted
    # stale". The count of titles this sweep stopped claiming the household owns.
    unowned: int = 0
    # Another sweep of this household held the lock (§5.3 vs §6.6's "sync now"); the loser reports.
    already_running: bool = False
    # Whether the library read answered, for §6.6's "last syncs" (decision 454).
    reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "pushed": self.pushed,
            "adopted": self.adopted,
            "unchanged": self.unchanged,
            "needs_relink": self.needs_relink,
            "owed_unreachable": self.owed_unreachable,
            "owed_no_token": self.owed_no_token,
            "push_failed": self.push_failed,
            "push_errors": self.push_errors,
            # A sorted list: this dict is JSON.
            "wrote": sorted(self.wrote),
            "unowned": self.unowned,
            "resolve": self.resolve,
            "users": self.users,
            "completed": self.completed,
            "failed_users": self.failed_users,
            "skipped_no_link": self.skipped_no_link,
            "already_running": self.already_running,
            "reached": self.reached,
        }

    def _note_push_error(self, reason: str) -> None:
        if len(self.push_errors) < 5 and reason not in self.push_errors:
            self.push_errors.append(reason)


async def linked_users(conn: asyncpg.Connection, cfg: JellyfinConfig) -> list[LinkedUser]:
    rows = await conn.fetch(
        "SELECT id, name, jellyfin_user_id, jellyfin_link_state FROM app_user "
        "WHERE jellyfin_user_id IS NOT NULL AND is_active ORDER BY id"
    )
    return [
        LinkedUser(
            app_user_id=r["id"],
            name=r["name"],
            jf_user_id=r["jellyfin_user_id"],
            token=cfg.token_for(r["id"]),
            link_state=r["jellyfin_link_state"],
        )
        for r in rows
    ]


async def _mark_needs_relink(conn: asyncpg.Connection, app_user_id: int) -> None:
    """§7.3: 'a 401 on write -> re-link prompt.' The write stays owed (`jf_synced_at` NULL)."""
    await conn.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' "
        "WHERE id = $1 AND jellyfin_user_id IS NOT NULL",
        app_user_id,
    )


@asynccontextmanager
async def _title_lock(conn: asyncpg.Connection, user_id: int, title_id: int):
    """Serialise the pushes for one (user, title). Yields whether the lock is held.

    Without it two taps can reach Jellyfin in the opposite order to the app. Session-level, not
    `_xact_`, so no transaction spans the round trip; released in `finally`; bounded wait.
    """
    key = f"{user_id}:{title_id}"
    held = False
    for attempt in range(_PUSH_LOCK_TRIES):
        if attempt:
            await asyncio.sleep(_PUSH_LOCK_WAIT_S)
        held = bool(
            await conn.fetchval(
                "SELECT pg_try_advisory_lock($1, hashtext($2)::int)", _PUSH_LOCK, key
            )
        )
        if held:
            break
    if not held:
        log.warning("no push lock for user %s title %s within the budget", user_id, title_id)
    try:
        yield held
    finally:
        if held:
            await conn.execute(
                "SELECT pg_advisory_unlock($1, hashtext($2)::int)", _PUSH_LOCK, key
            )


async def _stamp(conn: asyncpg.Connection, user_id: int, title_id: int, seen: bool) -> None:
    """§7.3's loop guard, only if the row still holds the value that was pushed; else still owed."""
    await conn.execute(
        "UPDATE user_title SET jf_synced_at = now() "
        "WHERE user_id = $1 AND title_id = $2 AND state = $3",
        user_id, title_id, "seen" if seen else "unseen",
    )


async def _targets(
    conn: asyncpg.Connection,
    title_id: int,
    jellyfin_id: str | None,
    copies: list[str] | None,
    seen: bool,
) -> list[str]:
    """The item id(s) one Played write has to reach.

    `seen`: the representative copy. `unseen`: every copy (map, page-set and representative), or
    a remaining copy's Played flag would be adopted straight back.
    """
    if jellyfin_id is None:
        jellyfin_id = await conn.fetchval("SELECT jellyfin_id FROM title WHERE id = $1", title_id)
    if seen:
        return [jellyfin_id] if jellyfin_id else []

    rows = await conn.fetch(
        "SELECT jellyfin_id FROM title_jellyfin_item WHERE title_id = $1 ORDER BY jellyfin_id",
        title_id,
    )
    out = [r["jellyfin_id"] for r in rows]
    for extra in [*(copies or []), jellyfin_id]:
        if extra and extra not in out:
            out.append(extra)
    return out


async def _push(
    conn: asyncpg.Connection,
    client: JellyfinClient,
    user: LinkedUser,
    *,
    title_id: int,
    seen: bool,
    kind: str | None = None,
    jellyfin_id: str | None = None,
    copies: list[str] | None = None,
) -> tuple[bool, str | None, str]:
    """Write one Played flag with that user's own token. Returns (pushed, refusal, outcome)."""
    if kind == "series":
        # Decisions 210(a) and 533: a write on the Series folder is Jellyfin's recursive MarkPlayed or
        # MarkUnplayed, which rewrites every episode's history and zeroes its resume point, so a
        # series is app-only both ways; this stamp settles the debt without certifying agreement (213).
        await _stamp(conn, user.app_user_id, title_id, seen)
        return True, f"series {'seen' if seen else 'unseen'} is app-only", PUSH_APP_ONLY
    if not user.token:
        # §7.3's least-privilege path: no admin key; a half-made link is a re-link prompt.
        await _mark_needs_relink(conn, user.app_user_id)
        return False, "no per-user Jellyfin token — re-link required", PUSH_NO_TOKEN

    items = await _targets(conn, title_id, jellyfin_id, copies, seen)
    if not items:
        return False, "not on Jellyfin", PUSH_ERROR
    reached = 0
    gone: JellyfinError | None = None
    for item_id in items:
        try:
            await client.set_played(item_id, user.jf_user_id, seen, user.token)
        except JellyfinError as exc:
            if exc.is_auth_failure:
                await _mark_needs_relink(conn, user.app_user_id)
                return False, "Jellyfin rejected the per-user token — re-link required", PUSH_AUTH_FAILURE
            if not seen and exc.status == 404:
                # A 404 on one copy of an unseen is a copy that has gone, not a failed write (§7.2).
                log.debug("copy %s of title %s is gone from Jellyfin: %s", item_id, title_id, exc)
                gone = exc
                continue
            # Leave it owed. `error`: a pre-10.9 server takes this branch on every write (§7.1).
            log.error("Played write for user %s failed: %s", user.app_user_id, exc)
            return False, str(exc), PUSH_ERROR
        reached += 1

    if not reached:
        log.error("Played write for user %s failed: %s", user.app_user_id, gone)
        return False, str(gone), PUSH_ERROR

    await _stamp(conn, user.app_user_id, title_id, seen)
    return True, None, PUSH_OK


async def _push_current(
    conn: asyncpg.Connection,
    client: JellyfinClient,
    user: LinkedUser,
    *,
    title_id: int,
    kind: str | None = None,
    jellyfin_id: str | None = None,
    copies: list[str] | None = None,
) -> tuple[bool, str | None, str]:
    """Push whatever the row says *now*, read under the per-(user, title) lock."""
    async with _title_lock(conn, user.app_user_id, title_id) as held:
        if not held:
            return False, PUSH_BUSY_REASON, PUSH_BUSY
        state = await conn.fetchval(
            "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2",
            user.app_user_id, title_id,
        )
        if state is None:
            # §7.3: "an absent row is the *default*, not an assertion" — there is nothing owed.
            return False, "nothing owed", PUSH_NOTHING
        return await _push(
            conn, client, user, title_id=title_id, seen=state == "seen",
            kind=kind, jellyfin_id=jellyfin_id, copies=copies,
        )


async def push_owed(
    conn: asyncpg.Connection,
    client: JellyfinClient | None,
    cfg: JellyfinConfig,
    *,
    user_id: int,
    title_id: int,
) -> tuple[bool, str | None]:
    """Tell Jellyfin what the app has already written. Returns (pushed, refusal).

    `set_state` minus its write, so callers can commit the write in their transaction and push
    after it (§3.3). The authoritative read of what is owed is `_push_current`'s, under the lock.
    """
    state = await conn.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user_id, title_id
    )
    if state is None:
        # §7.3: an absent row is the default, not an assertion; nothing is owed.
        return False, "nothing owed"

    row = await conn.fetchrow(
        """
        SELECT t.kind, t.jellyfin_id,
               NOT t.is_owned AND t.owned_checked_at IS NOT NULL AS checked_and_gone,
               EXISTS (SELECT 1 FROM title_jellyfin_item i WHERE i.title_id = t.id) AS has_copy
          FROM title t WHERE t.id = $1
        """,
        title_id,
    )
    kind = row["kind"] if row is not None else None
    jellyfin_id = row["jellyfin_id"] if row is not None else None
    if client is None or not jellyfin_id:
        if client is not None:
            return False, "not on Jellyfin"
        if cfg.secrets_unreadable:
            # Configured but sealed under a SECRETS_KEY this process lacks (M4.7 dd03).
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"

    if row["checked_and_gone"] and not row["has_copy"]:
        # The last sweep looked for it and found no copy: refuse rather than spend a round trip on
        # a dead id. `owned_checked_at` separates this from a merely unverified import (§7.2).
        return False, "not on Jellyfin"

    users = {u.app_user_id: u for u in await linked_users(conn, cfg)}
    user = users.get(user_id)
    if user is None:
        return False, "this account is not linked to a Jellyfin user"

    pushed, refusal, _outcome = await _push_current(
        conn, client, user, title_id=title_id, kind=kind, jellyfin_id=jellyfin_id
    )
    return pushed, refusal


async def retract(
    conn: asyncpg.Connection,
    client: JellyfinClient | None,
    cfg: JellyfinConfig,
    *,
    user_id: int,
    title_id: int,
    prior_state: str | None,
) -> tuple[bool, str | None]:
    """Put back exactly the Played flag a forward action set, and only that (decision 35).

    Through `_push`, never `set_state`: `observations.undo` already restored the row. No prior
    row, no retraction (decision 210): Played = false would overwrite Jellyfin's history.
    """
    if prior_state is None:
        return False, "no prior state to put back"
    if client is None:
        if cfg.secrets_unreadable:
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"

    row = await conn.fetchrow("SELECT kind, jellyfin_id FROM title WHERE id = $1", title_id)
    if row is None or not row["jellyfin_id"]:
        return False, "not on Jellyfin"

    users = {u.app_user_id: u for u in await linked_users(conn, cfg)}
    user = users.get(user_id)
    if user is None:
        return False, "this account is not linked to a Jellyfin user"

    async with _title_lock(conn, user_id, title_id) as held:
        if not held:
            return False, PUSH_BUSY_REASON
        # `_push`, not `_push_current`: the value is what was sent forward; the lock still applies.
        pushed, refusal, _outcome = await _push(
            conn, client, user, title_id=title_id, seen=prior_state == "seen",
            kind=row["kind"], jellyfin_id=row["jellyfin_id"],
        )
    return pushed, refusal


async def set_state(
    conn: asyncpg.Connection,
    client: JellyfinClient | None,
    cfg: JellyfinConfig,
    *,
    user_id: int,
    title_id: int,
    state: str,
) -> dict[str, Any]:
    """The explicit user action: mark a title seen or unseen, then tell Jellyfin.

    The write commits before, and outside the lock of, the push: a failed push leaves the debt
    owed (§3.3), and the pusher re-reads the row under the lock.
    """
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, not {state!r}")

    await conn.execute(
        """
        INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at)
        VALUES ($1, $2, $3, now(), NULL)
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET state = EXCLUDED.state, state_changed_at = now(), jf_synced_at = NULL
        """,
        user_id, title_id, state,
    )

    pushed, refusal = await push_owed(conn, client, cfg, user_id=user_id, title_id=title_id)
    return {"state": state, "synced": pushed, "reason": refusal}


async def _adopt(
    conn: asyncpg.Connection, user_id: int, title_id: int, seen: bool,
    played_at: datetime | None = None,
) -> None:
    """Take Jellyfin's value as ours and stamp the agreement."""
    await conn.execute(
        """
        INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at, played_at)
        VALUES ($1, $2, $3, now(), now(), $4)
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET state = EXCLUDED.state, state_changed_at = now(), jf_synced_at = now()
        """,
        user_id, title_id, "seen" if seen else "unseen", played_at,
    )


def _folder_played(item: dict) -> bool:
    """Jellyfin's computed Played flag for a Series folder, distrusted when it has no children.

    `Played = playedCount >= totalCount` is true for an empty folder (decision 210); any child
    count that reads zero voids it.
    """
    if not played_of(item):
        return False
    for key in ("RecursiveItemCount", "ChildCount"):
        value = item.get(key)
        if value is not None and int(value) == 0:
            return False
    data = item.get("UserData") or {}
    unplayed = data.get("UnplayedItemCount")
    childless = (
        unplayed is not None and int(unplayed) == 0 and data.get("PlayedPercentage") is None
    )
    return not childless


def _collapse(
    items: list[dict], resolved: resolve.ResolveReport
) -> dict[int, tuple[list[str], bool, datetime | None]]:
    """Group one user's page-set into `{title_id: (every copy, played on any copy, latest play date)}`.

    Pure over the sweep's resolution. Played is OR-ed across copies: per-item reconciliation let
    a duplicate nobody played overwrite an explicit `seen`.
    """
    collapsed: dict[int, tuple[list[str], bool, datetime | None]] = {}
    for item in items:
        item_id = str(item.get("Id") or "")
        title_id = resolved.items.get(item_id)
        if title_id is None:
            continue
        played = (
            _folder_played(item) if resolved.kinds.get(title_id) == "series" else played_of(item)
        )
        copies, was_played, last = collapsed.get(title_id, ([], False, None))
        when = max(filter(None, (last, last_played_of(item))), default=None)
        collapsed[title_id] = ([*copies, item_id], was_played or played, when)
    return collapsed


async def _open_prompt_titles(conn: asyncpg.Connection, user_id: int) -> set[int]:
    """The titles this member has an unanswered finish prompt for, or one put away (decisions 211, 554).

    `GUARD_STATES` is imported here to avoid a circular import with `sync/playback.py`.
    """
    from spielplan.sync.playback import GUARD_STATES

    rows = await conn.fetch(
        "SELECT DISTINCT title_id FROM playback_event "
        "WHERE user_id = $1 AND finished AND title_id IS NOT NULL "
        "AND prompt_state = ANY($2::text[])",
        user_id, list(GUARD_STATES),
    )
    return {r["title_id"] for r in rows}


async def sync_user(
    conn: asyncpg.Connection,
    client: JellyfinClient,
    user: LinkedUser,
    report: SyncReport,
    *,
    resolved: resolve.ResolveReport | None = None,
) -> bool:
    """One linked user's whole library, reconciled by the table at the top of this module.

    True when the sweep completed. `resolved` is `sync_all`'s keyless resolution, computed here
    only when a caller drives one user directly.
    """
    # The snapshot boundary, from the database's clock (`state_changed_at` uses `now()`): an
    # action taken during the sweep is newer than the data it would be reconciled against.
    snapshot_at = await conn.fetchval("SELECT now()")
    if resolved is None:
        resolved = await resolve.upsert_items(conn, await client.all_items(None))
        report.resolve = resolved.as_dict()
        await wish.announce_arrivals(conn, resolved.arrived)
    # The per-user read is for `UserData` only: Played is per user, identity is not.
    collapsed = _collapse(await client.all_items(user.jf_user_id), resolved)
    zone = notices._zone()
    per_day = Counter(last.astimezone(zone).date() for _c, _p, last in collapsed.values() if last)
    open_prompts = await _open_prompt_titles(conn, user.app_user_id)

    # No per-user token: adopt-only for the whole member (§14 risk 3 forbids the admin key).
    adopt_only = user.token is None
    if adopt_only:
        if user.link_state != "needs_relink":
            # Written only when it says something new.
            await _mark_needs_relink(conn, user.app_user_id)
        if user.name not in report.needs_relink:
            # Reported every sweep: §6.6's card is where the admin learns of it.
            report.needs_relink.append(user.name)

    completed = True
    for title_id, (copies, jf_seen, last) in collapsed.items():
        kind = resolved.kinds.get(title_id)
        row = await conn.fetchrow(
            "SELECT state, state_changed_at, jf_synced_at FROM user_title "
            "WHERE user_id = $1 AND title_id = $2",
            user.app_user_id, title_id,
        )
        if last is not None and per_day[last.astimezone(zone).date()] >= BULK_DAY:
            last = None
        if last is not None and row is not None:
            own_write = (
                row["jf_synced_at"] is not None
                and row["state_changed_at"] <= last <= row["jf_synced_at"]
            )
            if not own_write:
                await conn.execute(
                    "UPDATE user_title SET played_at = $3 WHERE user_id = $1 AND title_id = $2 "
                    "AND (played_at IS NULL OR played_at < $3)",
                    user.app_user_id, title_id, last,
                )

        if row is None:
            if jf_seen and title_id not in open_prompts:
                await _adopt(conn, user.app_user_id, title_id, True, played_at=last)
                report.adopted += 1
            else:
                report.unchanged += 1
            continue

        app_seen = row["state"] == "seen"
        acted_during_this_sweep = row["state_changed_at"] > snapshot_at
        if row["jf_synced_at"] is None or acted_during_this_sweep:
            # A series needs no token: it is settled app-only (decision 533).
            if adopt_only and kind != "series":
                report.owed_no_token += 1
                continue
            pushed, refusal, outcome = await _push_current(
                conn, client, user, title_id=title_id, kind=kind, copies=copies
            )
            if pushed and outcome == PUSH_APP_ONLY:
                report.unchanged += 1
            elif pushed:
                report.pushed += 1
                if outcome == PUSH_OK:
                    # Evidence for clearing §7.3's badge: a write the member's own token made.
                    report.wrote.add(user.name)
            elif outcome == PUSH_AUTH_FAILURE:
                if user.name not in report.needs_relink:
                    report.needs_relink.append(user.name)
                # The token is dead for every remaining title too; stop hammering the server.
                completed = False
                break
            elif outcome == PUSH_NOTHING:
                # The row was deleted under us (an Undo, most likely). Nothing is owed.
                report.unchanged += 1
            elif outcome == PUSH_BUSY:
                # A tap held the lock: nothing sent or refused, so not a `push_failed`; still owed.
                report.unchanged += 1
            else:
                # Not a 401 (§7.3's re-link trigger): counted, carried past, sweep incomplete.
                report.push_failed += 1
                report._note_push_error(refusal or "unknown")
                completed = False
            continue

        if app_seen == jf_seen:
            report.unchanged += 1
            continue

        if title_id in open_prompts:
            # Decision 211: adopting would answer the open prompt for the person; only a tap closes it.
            report.unchanged += 1
            continue

        if kind == "series":
            # A computed folder flag decides nothing for an existing row (decisions 210, 213): it
            # moves when nobody acts, and a series unseen is a deliberate, permanent disagreement.
            report.unchanged += 1
            continue

        await _adopt(conn, user.app_user_id, title_id, jf_seen)
        report.adopted += 1

    # Owed rows Jellyfin no longer lists can never be settled: counted, not forgotten.
    report.owed_unreachable += await conn.fetchval(
        "SELECT count(*) FROM user_title "
        "WHERE user_id = $1 AND jf_synced_at IS NULL AND NOT (title_id = ANY($2::int[]))",
        user.app_user_id, list(collapsed),
    )
    return completed


async def sync_all(
    conn: asyncpg.Connection, client: JellyfinClient | None = None
) -> SyncReport:
    """The 15-minute job (§5.3 `jellyfin-seen-sync`), and the admin's "sync now" button.

    The library is read and resolved once, with the admin key and no `userId` (§7.2); each
    member's own read supplies only `UserData`. `client` is injectable for the integration tests.
    """
    global _failed_users_logged

    from spielplan.connectors.registry import load_jellyfin, make_client

    report = SyncReport()
    cfg = await load_jellyfin(conn)
    if not cfg.configured:
        report.skipped_no_link = True
        return report

    # `make_client` threads §7.1's stored version verdict onto the client.
    client = client or make_client(cfg)
    users = await linked_users(conn, cfg)
    # Not a return: the ownership pass needs no linked member (decision 413).
    report.skipped_no_link = not users

    # One sweep per household at a time (§5.3 vs §6.6); session-level, released in `finally`.
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", _SWEEP_LOCK, 0):
        report.already_running = True
        log.info("a Jellyfin seen sweep is already running; this one did nothing")
        return report
    try:
        # §7.1's pin, re-probed each sweep; written back only when it changed.
        try:
            raw, supported = await client.probe_version()
        except JellyfinError as exc:
            # Not `_outage`: the library read below is the reachability authority (ops-15).
            log.debug("could not probe the Jellyfin version: %s", exc)
        else:
            if (raw, supported) != (cfg.server_version, cfg.server_supported):
                cfg = await save_jellyfin(conn, server_version=raw, server_supported=supported)
            client.server_version, client.server_supported = raw, supported

        try:
            library = await client.all_items(None)
        except JellyfinError as exc:
            # §3.3: a degraded sync, never a broken app; nothing below is decidable without the library.
            _outage.down(exc)
            return report
        report.reached = True
        _outage.up()
        resolved = await resolve.upsert_items(conn, library)
        # Once per sweep: resolution is user-independent.
        report.resolve = resolved.as_dict()
        await wish.announce_arrivals(conn, resolved.arrived)

        # Before the per-user loop, so unseen pushes never target copies this read no longer lists.
        pruned = await resolve.prune_missing_items(conn, resolved)
        if pruned:
            log.info("jellyfin library shrank: %d stale copy row(s) pruned", pruned)

        for user in users:
            report.users.append(user.name)
            try:
                if await sync_user(conn, client, user, report, resolved=resolved):
                    report.completed.append(user.name)
            except JellyfinError as exc:
                # Named in the report (§6.6); logged loudly once, then at DEBUG while it stays true.
                report.failed_users.append(user.name)
                log.log(
                    logging.WARNING if user.name not in _failed_users_logged else logging.DEBUG,
                    "seen sync for %s failed: %s", user.name, exc,
                )
        _failed_users_logged = frozenset(report.failed_users)

        await wish.retire_settled(conn)
        await _falsify_ownership(conn, resolved, report)

        # Clear a stale re-link flag only on a Played write this member's token made.
        for user in users:
            if user.link_state != "needs_relink":
                continue
            if user.token and user.name in report.wrote:
                await conn.execute(
                    "UPDATE app_user SET jellyfin_link_state = 'linked' WHERE id = $1",
                    user.app_user_id,
                )
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1, $2)", _SWEEP_LOCK, 0)
    return report


async def _falsify_ownership(
    conn: asyncpg.Connection, resolved: resolve.ResolveReport, report: SyncReport
) -> None:
    """§7.2: "mark removed titles `is_owned = false` ... re-derived from Jellyfin".

    Gated twice: the caller returns on a failed read (`all_items` refuses a truncated one) and an
    empty resolution is refused here. The completed sweep is the only caller (decision 362).
    """
    if not resolved.matched_title_ids:
        log.warning("not falsifying ownership: this sweep resolved no titles at all")
        return
    report.unowned = await conn.fetchval(
        """
        WITH dropped AS (
            UPDATE title SET is_owned = false, owned_checked_at = now()
             WHERE is_owned AND jellyfin_id IS NOT NULL AND NOT (id = ANY($1::int[]))
            RETURNING 1
        )
        SELECT count(*) FROM dropped
        """,
        list(resolved.matched_title_ids),
    )
    if report.unowned:
        log.info("jellyfin library shrank: %d title(s) no longer owned", report.unowned)


async def forget_token(conn: asyncpg.Connection, app_user_id: int) -> None:
    """Drop a stored per-user Jellyfin token, which must not outlive its mapping.

    Under the connector row's lock: the sealed map cannot be merged in SQL.
    """
    from spielplan.connectors.registry import load_jellyfin

    async with conn.transaction():
        cfg = await load_jellyfin(conn, for_update=True)
        if str(app_user_id) in cfg.user_tokens:
            tokens = dict(cfg.user_tokens)
            tokens.pop(str(app_user_id), None)
            await save_jellyfin(conn, user_tokens=tokens)


async def unlink(conn: asyncpg.Connection, app_user_id: int) -> None:
    """Drop the link and forget that user's token, in one transaction (§3.3: seen state stays)."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE app_user SET jellyfin_user_id = NULL, jellyfin_link_state = NULL "
            "WHERE id = $1",
            app_user_id,
        )
        await forget_token(conn, app_user_id)


__all__ = [
    "LinkedUser",
    "SyncReport",
    "forget_token",
    "linked_users",
    "set_state",
    "sync_all",
    "sync_user",
    "unlink",
]
