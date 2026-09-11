"""Two-way seen-state sync. Spec v2.1 §7.3.

The spec gives three sentences and they decide everything here:

  * "**App is authoritative for explicit user actions.** `seen`/`unseen` set in the app writes
    Jellyfin's per-user Played flag."
  * "Conflict rule: last-writer-wins with the app's explicit action outranking Jellyfin's
    inferred state; `jf_synced_at` prevents loops."
  * "The mapping is the plain boolean: `seen` -> Played = true, `unseen` -> Played = false."

`user_title.jf_synced_at` carries the whole conflict resolution, and reading it correctly is
the difference between a sync and a loop:

  | user_title row | jf_synced_at | Jellyfin says | what happens                                |
  |----------------|--------------|---------------|---------------------------------------------|
  | absent         | –            | played        | adopt. An absent row is the *default*, not  |
  |                |              |               | an assertion, so there is nothing to defend |
  |                |              |               | and Jellyfin's history is real history.     |
  | absent         | –            | not played    | nothing — both already say unseen.          |
  | present        | NULL         | anything      | push. The person acted and Jellyfin has not |
  |                |              |               | been told yet.                              |
  | present        | set          | agrees        | nothing. This is the loop guard: the value  |
  |                |              |               | we just wrote comes back and stops here.    |
  | present        | set          | disagrees     | adopt. We agreed once; the change is newer  |
  |                |              |               | than that agreement, so it came from        |
  |                |              |               | Jellyfin's side.                            |

The row that never happens is "push the app's absence over Jellyfin's history" — the first
sync after linking would otherwise erase every Played flag the household ever set.

Three clauses qualify that table, and each of them is a fault this module shipped with:

  * **An open finish prompt suspends the whole row** (decision 211, §7.3:315 "Jellyfin playback
    is a suggestion, never a silent write"). While a `playback_event` for this (user, title) is
    armed or shown, the sweep adopts nothing for it: adopting would answer the question the app
    is in the middle of asking, and §13 would then count that answer in its capture rate.
  * **A series is asymmetric** (decisions 210 and 213). Jellyfin does not store Played on a
    Series folder; it computes `playedCount >= totalCount`, so the flag changes when a season
    lands and is `true` for an empty folder. The app never sends the folder a DELETE either,
    because Jellyfin executes that as a recursive MarkUnplayed over every episode and the app
    cannot put any of it back. Both halves land in the same place: for a series the last row of
    the table above does not apply at all. A `user_title` row that exists is never overwritten by
    the computed flag — an absent row still is, which is what "Jellyfin history is real history"
    was always about.
  * **An absent row is never retracted** (decision 210, §7.3). `retract` with no prior row would
    write Played = false over history the app never owned, which is the row above that never
    happens, reached from the Undo direction instead of the first-sync one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError, played_of
from spielplan.connectors.registry import (
    SECRETS_UNREADABLE_REASON,
    JellyfinConfig,
    save_jellyfin,
)

log = logging.getLogger("spielplan.sync.seen")

STATES = ("seen", "unseen")

# `_push`'s third return element. `sync_user` has to tell three refusals apart and only one of
# them may stop the sweep: a MISSING token is a half-made link `api/admin.py` creates on purpose
# ("A link with no token is real but incomplete: it attributes playback and feeds the P(seen)
# prior"), a REJECTED one is §7.3's "a 401 on write -> re-link prompt", and everything else is an
# outage to count and carry past. Reading that distinction out of the refusal *string* is what
# made one owed row freeze a tokenless member's whole sweep for the life of the install.
PUSH_OK = "ok"
PUSH_APP_ONLY = "app_only"          # decision 210: a series unseen is settled without a write
PUSH_NO_TOKEN = "no_token"
PUSH_AUTH_FAILURE = "auth_failure"
PUSH_ERROR = "error"
PUSH_NOTHING = "nothing"            # the row went away under us; nothing is owed
# A concurrent write of the same (user, title) held the lock. Told apart from PUSH_ERROR because
# §6.6's `push_failed` is defined as "Played writes that failed" and this one was never attempted:
# nothing reached the socket, nothing was refused by the media server, and the row is still owed
# with `jf_synced_at` NULL, which §7.3's table already reads as "the person acted and Jellyfin has
# not been told yet". Counted as a failure it put a self-healing 100 ms race — a tap landing on the
# same row the 15-minute sweep is pushing — into the counter cs-05 created so that a PERSISTENTLY
# broken write direction would be visible, and dropped that member from `completed`.
# [M4.11 review cycle 2: m411-rev2-seen-03; §6.6, §7.3]
PUSH_BUSY = "busy"

# The namespace halves of this module's two advisory locks. Two ints rather than one, following
# `tonight/play.py:48` and `api/deps.py`'s note that the single-argument `hashtext(name)` form is a
# different lock space: a namespace is what stops a title id colliding with another feature's lock
# on the same number. The per-title key is hashed into the second int because a lock takes two
# ints and this one needs three facts; a hash collision costs two unrelated titles one serialised
# push and can cost nothing else, which is a price worth the namespace.
_PUSH_LOCK = 7303
_SWEEP_LOCK = 7304

# How long an explicit action waits for another push of the same (user, title) before giving up.
# Bounded rather than blocking, because §3.3 will not have a person's tap hang on a foreign
# server's latency: the degradation is "not synced, debt owed", which §7.3's table already reads
# as "the person acted and Jellyfin has not been told yet" and the next sweep settles.
_PUSH_LOCK_TRIES = 20
_PUSH_LOCK_WAIT_S = 0.1
PUSH_BUSY_REASON = "another write for this title is in flight"

# §3.3 makes an unreachable Jellyfin a degraded sync rather than a broken app, so this is a
# logging rule and not a behaviour: a box powered off overnight produced ~96 warnings from this
# sweep and ~480 from the 1-minute poll by morning, interleaved with the backup and refit reports
# the operator actually wants. `time.monotonic`, not a datetime, for two reasons: the elapsed
# minutes must not jump when the host's clock is corrected, and §7.3's sweep boundary must stay
# the DATABASE's `now()` — a module that imports `datetime` invites the next reader to take the
# boundary from the process clock, which is the bug `test_the_sweep_boundary_ignores_this_processs_clock`
# exists to prevent.
_unreachable_since: float | None = None

# The members whose own `/Items` read failed on the last sweep, so that line is a state change and
# not a stream either. Separate from `_unreachable_since` on purpose, and NOT routed through it:
# the two faults are independent — the library read is keyless and each member's is not — so a
# member with a deleted Jellyfin account would otherwise hold the outage memo open for ever and
# swallow the first WARNING of a real outage, which is the one line ops-15 exists to preserve.
# Same shape as `worker._last_unresolved`, and for the same reason: a Jellyfin account that was
# deleted or renamed fails every fifteen minutes for as long as the mapping stands.
_failed_users_logged: frozenset[str] = frozenset()


@dataclass
class LinkedUser:
    app_user_id: int
    name: str
    jf_user_id: str
    # §7.3's per-user access token, decrypted, carried across the whole of one member's sweep —
    # so this object holds the credential for longer than any other in the codebase, and a
    # generated repr prints it. Same declaration as `JellyfinClient.api_key` and
    # `push/keys.py`'s signing key ("no accessor and no repr"): a credential is hidden where it
    # is declared, because the machinery that reprs it runs when the code is already failing.
    # `field(repr=False)` with no default on purpose — every construction site passes it
    # positionally, and a default here would push `link_state` into needing one too.
    # [review cycle 1: m411-rev-jf-02; §14.3]
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
    # Rows the app owes Jellyfin a write for and that this sweep could not reach, because the
    # title is no longer in the library. Reported rather than silently forgotten.
    owed_unreachable: int = 0
    # The same debt, for a different and repairable reason: the member's link carries no per-user
    # token, so the row is owed and this sweep deliberately did not try. §7.3 forbids the admin
    # key, so the only thing that settles these is the person signing in again — which is a
    # sentence §6.6's card can print, and a silent abort was not.
    owed_no_token: int = 0
    # Users whose sweep actually completed. `needs_relink` being empty is not evidence of
    # health — it is also what a Jellyfin outage looks like.
    completed: list[str] = field(default_factory=list)
    # Users whose sweep raised out of `sync_user` and was swallowed. The library read is keyless
    # and the per-user reads are not, so the two fail independently: a member's Jellyfin account
    # deleted or renamed 404s their `/Items` for ever while the household's read keeps working.
    # `sync_all` logged a warning and moved on, and every counter — `push_failed` included —
    # stayed zero, so §6.6's card printed the quiet healthy household for a sweep in which
    # neither direction of §7.3 ran for anybody. That is M4.11 finding 3's own sentence one layer
    # up, and `completed` alone could not say it: a shorter `completed` is only legible beside
    # the reason, which is this list. [review cycle 1: seen-02; §3.3, §6.6]
    failed_users: list[str] = field(default_factory=list)
    # Played writes that failed for a reason that is not a credential: a 404 from a server below
    # §7.1's 10.9 pin, a 500, a proxy that drops DELETE, a timeout. Before this counter existed the
    # entire app->Jellyfin direction could be dead for the life of an install while §6.6's card
    # printed "pushed 0 - adopted 0 - unchanged N", which is also what a healthy quiet sweep
    # prints. The reasons are deduplicated and capped: the same 404 repeated 1,500 times is one
    # fact, and this list is rendered to a person.
    push_failed: int = 0
    push_errors: list[str] = field(default_factory=list)
    # Users for whom a Played write actually SUCCEEDED. The only evidence §7.3's re-link badge may
    # be cleared on: a sweep sends the token only for rows with `jf_synced_at IS NULL`, so a quiet
    # sweep exercises a revoked token never and `completed` was promoting it on no evidence at all.
    wrote: set[str] = field(default_factory=set)
    # §7.2: "mark removed titles `is_owned = false` ... re-derived from Jellyfin, never trusted
    # stale". The count of titles this sweep stopped claiming the household owns.
    unowned: int = 0
    # Set when another sweep of the same household held the lock (§5.3's 15-minute job against
    # §6.6's "sync now"). Two sweeps of one user against two different library snapshots is worse
    # than one, so the loser reports rather than racing.
    already_running: bool = False

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
            # A sorted list, not the set: this dict is a route's JSON body (`api/admin.py`'s
            # "sync now") and a worker log line, and neither can carry a set.
            "wrote": sorted(self.wrote),
            "unowned": self.unowned,
            "resolve": self.resolve,
            "users": self.users,
            "completed": self.completed,
            "failed_users": self.failed_users,
            "skipped_no_link": self.skipped_no_link,
            "already_running": self.already_running,
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


def _note_unreachable(exc: JellyfinError) -> None:
    """Log an unreachable Jellyfin once, not once a sweep, for as long as it stays down.

    The behaviour is already right (§3.3: a degraded sync, never a broken app); the observability
    was not. A box powered off overnight produced a warning every fifteen minutes from here and
    every minute from the `/Sessions` poll, which is what the backup and refit reports an operator
    does want were buried under. Deliberately not a backoff schedule and not a cadence change —
    §7.3's prompt timing depends on the minute, and §8 calls a retry knob nobody asked for
    configurability.
    """
    global _unreachable_since

    if _unreachable_since is None:
        _unreachable_since = time.monotonic()
        log.warning("jellyfin is unreachable: %s", exc)
    else:
        log.debug("jellyfin is still unreachable: %s", exc)


def _note_reachable() -> None:
    """The other half: one INFO line when it comes back, carrying how long it was gone."""
    global _unreachable_since

    if _unreachable_since is not None:
        minutes = (time.monotonic() - _unreachable_since) / 60
        log.info("jellyfin reachable again after %d minute(s)", int(minutes))
        _unreachable_since = None


async def _mark_needs_relink(conn: asyncpg.Connection, app_user_id: int) -> None:
    """§7.3: 'a 401 on write -> re-link prompt.' The app-side state stays written and
    `jf_synced_at` stays NULL, so the write is owed, not lost — the next sync after a
    successful re-link pushes it."""
    await conn.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' "
        "WHERE id = $1 AND jellyfin_user_id IS NOT NULL",
        app_user_id,
    )


@asynccontextmanager
async def _title_lock(conn: asyncpg.Connection, user_id: int, title_id: int):
    """Serialise the pushes for one (user, title). Yields whether the lock is held.

    The divergence this closes is an *ordering* problem, not a lost update (§7.3's conflict rule,
    finding 4). Two phones — or one impatient double tap — wrote the app row in one order and
    reached Jellyfin in the other, leaving the database on the later value and the media server on
    the earlier one, both stamped as agreed; the table at the top of this module then reads
    "present + stamped + disagrees" as a Jellyfin-side change and adopts it, reverting the explicit
    action §7.3 says outranks Jellyfin's inferred state, with a fresh `state_changed_at` that makes
    the correction unrecoverable from the row. The conditional stamp below is worth having and does
    not fix it on its own: the loser's stamp correctly fails to match, the winner's has already
    succeeded, and the row still reads stamped-and-disagreeing.

    **Session-level, not `_xact_`**, and that is the load-bearing choice: the Jellyfin round trip
    must not sit inside a transaction, which is precisely the leak M4.10 moved out of the verdict
    path (decision 207). The cost is that `pg_advisory_lock` outlives the transaction and travels
    with a pooled connection, so it is released in `finally`, unconditionally, and held across no
    more than the one push.

    `pg_try_advisory_lock` with a bounded wait rather than `lock_timeout`, because `SET LOCAL` needs
    a transaction and a session-wide `SET` would ride the connection back into the pool — the same
    leak in a different costume. A wedged holder therefore degrades to "not synced, debt owed".
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
    """§7.3's loop guard — but only if the row still holds the value that was pushed.

    The stamp used to be unconditional, which is how a superseded push came to be recorded as an
    agreement that never happened (finding 4). `AND state = $3` leaves `jf_synced_at` NULL instead,
    and the table at the top of this module already reads a NULL as "the person acted and Jellyfin
    has not been told yet" — a debt, which is exactly what a losing push leaves behind.
    """
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
    """The item id(s) one Played write has to reach, which differ by direction.

    `seen` goes to the representative copy alone: Jellyfin marks Played on the item that was
    actually watched and the sweep OR-collapses the flags across copies, so one tick is enough and
    `title.jellyfin_id` is the copy every other surface deep-links to (§7.1).

    `unseen` has to clear **every** copy. A household whose libraries ship "Movies" and
    "Movies 4K" has two items per film; clearing one left the other reading Played, and the next
    sweep's OR-collapse adopted it straight back over the person's explicit action (finding 6).
    Three sources, because no single one is complete at every moment: `title_jellyfin_item` is the
    authority and is the admin read's full set, the page-set `copies` covers the first boot before
    any sweep has written that table, and the representative covers a title the map has not reached
    at all. Their union is never worse than any one of them — which is only true because `_push`
    treats a 404 on one member of the list as a copy that has gone rather than as a failed unseen.
    The map is pruned by a completed sweep and nothing else, so between a copy leaving the library
    and that sweep this list still names it, and one dead id used to abort the write to every live
    copy behind it. [M4.11 review cycle 2: m411-rev2-resolve-02]
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
    if kind == "series" and not seen:
        # Decision 210(a). `ITEM_TYPES = "Movie,Series"` means the stored id is the Series FOLDER
        # item, and Jellyfin's `Folder.MarkUnplayed` iterates every recursive non-folder child and
        # resets `Played`, `PlayCount`, `PlaybackPositionTicks` and `LastPlayedDate` on each. The
        # app has no episode identity (§4.1 rule 5), so it cannot restore any of that: one tap on
        # "not seen" would erase a household's progress through six seasons. So series unseen is
        # app-only, and the stamp is what keeps the next sweep from re-owing the row for ever.
        # "Seen" still POSTs, per decision 210(b) — marking every episode played is accepted as
        # the meaning of series-seen, and it is reversible in Jellyfin's own UI.
        #
        # This stamp is the one `jf_synced_at` in the module that certifies nothing about
        # Jellyfin: it records that the app settled the debt, not that the two sides agreed. The
        # sweep's adopt branch has to know that, and decision 213 is where it does — without it,
        # the folder's permanent, deliberate disagreement read as newer evidence and reverted the
        # tap on the next sweep.
        await _stamp(conn, user.app_user_id, title_id, seen)
        return True, "series unseen is app-only", PUSH_APP_ONLY
    if not user.token:
        # §7.3's least-privilege path: the admin key would work and is explicitly not used.
        # No token means the link is half-made, which is a re-link prompt, not a fallback.
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
                # One copy of a multi-copy DELETE that the library no longer has. The whole list
                # used to share one `try`, so the first dead id aborted the write to every live
                # copy behind it — and `_targets`'s own promise ("their union is never worse than
                # any one of them") was false for exactly the household §7.3's "unseen clears every
                # copy" is written for. The map is only pruned by a completed sweep, so between a
                # copy disappearing and that sweep it still names the dead id, and a tap in that
                # window cleared nothing and handed §6.7's rail the raw transport line.
                #
                # 404 alone, and only in the unseen direction: "the item is not there" is not a
                # failed unseen, while a 500 or a timeout says nothing about whether the copy
                # exists and must still stop the write. If EVERY copy answers 404 the title has
                # left the library entirely and the refusal below is unchanged.
                # [M4.11 review cycle 2: m411-rev2-resolve-02; §7.3, §7.2]
                log.debug("copy %s of title %s is gone from Jellyfin: %s", item_id, title_id, exc)
                gone = exc
                continue
            # Transport or server error: leave jf_synced_at NULL and try again next sync. At `error`
            # rather than `warning`, and counted by the caller, because this is the branch a server
            # below §7.1's 10.9 pin takes on every single write (there is no `/UserPlayedItems` route
            # to take) — for the life of the install, while every other figure reads healthy.
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
    """Push whatever the row says *now*, holding the per-(user, title) lock for the whole of it.

    Re-reading the state inside the lock is the half that makes the serialisation mean something:
    a pusher that carried a value in from before the lock would push a stale one and then the
    losing interleaving is back. Read under the lock, the last push of a contended pair always
    carries the last committed value, and the conditional stamp catches the one remaining order
    (a write that commits between this read and the stamp).
    """
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

    This is `set_state` minus its first statement, and splitting it out is what finally makes
    that function's own promise — "the app-side write is committed before the network call and
    never depends on it" — structurally true for its most frequent caller. `rate/session.py`
    awaited the whole of `set_state` *inside* the verdict transaction, so a Jellyfin that took
    1.5 s to answer held the row locks for 1.5 s: measured, a second connection could not see
    the committed verdict for 1.67 s and one backend sat `idle in transaction` for the whole
    wait, against a pool of ten. §3.3 ("the app must work when Jellyfin is down") is not a
    statement about the response body alone; it is a statement about what a foreign server's
    latency is allowed to hold. [M4.10 finding 11]

    What is owed is read from `user_title` rather than passed in, because that row IS §7.3's
    ledger of the debt: `ledger/observations._set_state` writes the state with
    `jf_synced_at = NULL` inside the observation's transaction, which is precisely the table's
    "the person acted and Jellyfin has not been told yet" row. Pushing anything else would be
    pushing an intention rather than a fact.

    It is read twice. The cheap read here answers "is anything owed at all" without taking a lock,
    and keeps the order of the four published refusals below — §6.7's rail prints them verbatim —
    stable; the authoritative one is `_push_current`'s, under the per-(user, title) lock, because a
    value read outside that lock can be superseded between the read and the socket (finding 4).
    """
    state = await conn.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user_id, title_id
    )
    if state is None:
        # §7.3: "an absent row is the *default*, not an assertion" — there is nothing owed and
        # nothing to say to Jellyfin. Reached only by a caller that pushed without writing.
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
            # The tap is kept either way — that is §3.3 — but "Jellyfin not configured" would be
            # a lie an admin cannot act on: the connector *is* configured and its credentials are
            # sealed under a SECRETS_KEY this process does not have (M4.7 dd03).
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"

    if row["checked_and_gone"] and not row["has_copy"]:
        # The same refusal, reached from what the last sweep already established. §7.2's
        # ownership pass un-owns a title the library dropped and the same sweep prunes its
        # `title_jellyfin_item` rows — but nothing clears `title.jellyfin_id`, which `_targets`
        # appends as its last fallback, so this call spent a round trip on a dead item id and
        # handed §6.7's rail the transport error ("DELETE /UserPlayedItems/jf-1 -> 404") as the
        # sentence a person reads under the title.
        #
        # Three conditions, and each one is load-bearing, because this may fire only for a title
        # a read actually looked for and did not find. `NOT is_owned` alone is also the
        # importer's own state: `load.py` sets `owned_checked_at = NULL` and logs "is_owned
        # imported but marked unverified" (§7.2), so before the first sweep every bundle title is
        # un-owned and would have been refused a write it can perfectly well make. The stamp is
        # what separates the two — `resolve.upsert_item` writes it for a title the library
        # carried, `_falsify_ownership` for one it did not. And an empty copy map on its own is
        # every title before any sweep has written that table at all.
        # [review cycle 1: M411-REV-03; §7.2, §7.3]
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
    """Put back exactly the Played flag a forward action set, and only that.

    Decision 35's compensating write, moved here from `rate/session.py._compensate_push`, which
    was a second implementation of the push path that had drifted from this one: it returned
    silently where the person's link had no token and where Jellyfin rejected it, so an expired
    token discovered on an Undo left `jellyfin_link_state` reading `linked` and produced no
    re-link prompt anywhere — the one thing §7.3 says a 401 on write must produce. Going through
    `_push` is the fix, and it is the shrink M4.10 owed. [M4.10 finding 11]

    `prior_state` is the state the row held before, or None for "there was no row" — a string
    rather than the ledger's `PriorState`, so `sync` does not import from `ledger`. It must not
    go through `set_state`: `observations.undo` has already restored `user_title` byte for byte,
    §7.3's `jf_synced_at` loop guard included, and a second app-side write would clear that
    stamp and, where the prior state was no row at all, invent an explicit `unseen` assertion
    out of an absence. `_push`'s own stamp names a row that either exists and holds the value
    being put back — `undo` restored it — or does not exist and takes no rows.

    **No prior row, no retraction** (decision 210, `data-10`). `prior_state is None` means the
    forward action created the row and the app had said nothing before it; retracting would send
    Played = false, which is the one row this module's own header says never happens — "push the
    app's absence over Jellyfin's history". The refusal comes before the token lookup so that an
    absent prior cannot even produce a re-link prompt for a write that must not be made. The
    accepted consequence, stated plainly: for a movie, Jellyfin keeps the flag the forward action
    set, and §7.3's table then adopts it on the next sweep — which decision 172(1) rules correct,
    because an absent app row is a default and Jellyfin history is real history. For a series the
    question does not arise, since decision 210 sends the folder no DELETE at all.
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
        # Not `_push_current`: the value here is the one the forward action pushed, which is a fact
        # about what this app sent and not about what the row says now. The lock still applies,
        # because a compensating write racing an explicit one diverges exactly as two explicit
        # ones do.
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

    The app-side write is committed before the network call and never depends on it. §3.3 is
    unambiguous that the app must work when Jellyfin is down, so a failed push leaves
    `jf_synced_at` NULL — a debt the next sync settles — rather than rolling back the person's
    action or reporting an error they cannot act on.

    Two statements, and their order is that promise: the write, then `push_owed`. A caller that
    needs the halves apart — because the write belongs in a transaction and the socket does not
    — calls them directly, which is what the Rate surface now does. [M4.10 finding 11]

    The app-side write is deliberately OUTSIDE `push_owed`'s per-(user, title) lock, and that is
    the same promise again: §3.3 will not have a person's tap wait on — or be lost to — a lock held
    for the duration of a foreign server's round trip. It is sufficient, because the pusher reads
    the row again inside the lock: whichever push goes last carries the last committed value, and
    the one interleaving left over (a write that commits between that read and the stamp) leaves
    `jf_synced_at` NULL rather than a false agreement. [finding 4]
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


async def _adopt(conn: asyncpg.Connection, user_id: int, title_id: int, seen: bool) -> None:
    """Take Jellyfin's value as ours and stamp the agreement."""
    await conn.execute(
        """
        INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at)
        VALUES ($1, $2, $3, now(), now())
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET state = EXCLUDED.state, state_changed_at = now(), jf_synced_at = now()
        """,
        user_id, title_id, "seen" if seen else "unseen",
    )


def _folder_played(item: dict) -> bool:
    """Jellyfin's Played flag for a Series folder, read with the caveat decision 210 names.

    Jellyfin does not store Played on a Series: `Folder.FillUserDataDtoValues` computes
    `dto.Played = playedCount >= totalCount`, and `totalCount == 0` makes that `true`. So an empty
    or not-yet-scanned series folder reports "played" with nobody having watched anything, and
    adopting it writes `seen` onto a show the household has never started. Decision 210's tail
    forbids exactly that, so a folder that says it has no children is not evidence of anything.

    The count arrives in more than one shape across versions and field sets, and any one of them
    saying zero is enough: `RecursiveItemCount` and `ChildCount` on the item, and an
    `UnplayedItemCount` of zero in `UserData` with no `PlayedPercentage` beside it — Jellyfin fills
    the percentage only when the recursive count is above zero, so that pair is the childless case
    stated in `UserData`'s own vocabulary. A server that sends none of them is not asked to prove
    anything: the absent count keeps the adoption §7.3 wants for a real, watched show.
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
) -> dict[int, tuple[list[str], bool]]:
    """Group one user's page-set into `{title_id: (every copy of it, played on any copy)}`.

    Several Jellyfin items can be one title: a household with "Movies" and "Movies 4K", a
    duplicated folder, two rips of the same film. `resolve.upsert_items` has already mapped them
    all to the same `title.id`, once per sweep, which is why this is now a pure grouping over that
    map and writes nothing: resolution is user-independent (identity comes from ProviderIds and the
    corpus, never from `UserData`), and re-running it per user cost a measured 5.26 s per 1,500
    titles — ~77 s per cycle at the ~11,000 this connector is sized for, during which §7.3's
    60-second `/Sessions` poll cannot run.

    It no longer elects `title.jellyfin_id` either. Electing per item, per user, wrote the pointer
    to the copy *this* user had played, so with two members on two copies it flipped every sweep
    (measured: `jf-1, jf-1b, jf-1, jf-1b, jf-1, jf-1b`) and `playback.observe`, which resolves a
    session against that one column, lost §7.3's finish prompt for whichever member was not on the
    winning copy — permanently, because the user order is fixed. The election is
    `resolve._elect_representatives` now: once per sweep, never from a Played flag.

    What stays is the OR across duplicates. Reconciling per *item* lets the second copy overwrite
    the decision the first one just made, and because `jf_synced_at` records agreement per
    (user, title) while agreement is actually per (user, item), the second copy's disagreement
    always looks newer — an explicit `seen` erased by a duplicate nobody played, decided by
    whatever order `/Items` happened to return.
    """
    collapsed: dict[int, tuple[list[str], bool]] = {}
    for item in items:
        item_id = str(item.get("Id") or "")
        title_id = resolved.items.get(item_id)
        if title_id is None:
            continue
        played = (
            _folder_played(item) if resolved.kinds.get(title_id) == "series" else played_of(item)
        )
        copies, was_played = collapsed.get(title_id, ([], False))
        collapsed[title_id] = ([*copies, item_id], was_played or played)
    return collapsed


async def _open_prompt_titles(conn: asyncpg.Connection, user_id: int) -> set[int]:
    """The titles this member has an unanswered finish prompt for (decision 211).

    One query per user rather than one per row: the set is tiny (a prompt is armed by a viewing)
    and the loop it guards runs once per title in the library.

    `OPEN_STATES` is imported here rather than at module scope on purpose. `sync/playback.py`
    imports this module inside a function for exactly the same reason, and the two would otherwise
    be one edit away from a circular import — the constant is the only thing either needs from the
    other, and its definition belongs beside the partial unique index that enforces it
    (`0006_jellyfin.sql`).
    """
    from spielplan.sync.playback import OPEN_STATES

    rows = await conn.fetch(
        "SELECT DISTINCT title_id FROM playback_event "
        "WHERE user_id = $1 AND finished AND title_id IS NOT NULL "
        "AND prompt_state = ANY($2::text[])",
        user_id, list(OPEN_STATES),
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

    Returns True when the sweep completed — which is what lets `sync_all` clear a stale
    re-link flag on evidence rather than on the absence of bad news.

    `resolved` is the sweep-level resolution `sync_all` did once with the admin key and no
    `userId`: the library belongs to the household, not to a member (§7.2), and identity does not
    depend on who is asking. Passed in rather than recomputed because it is the same answer for
    every user; computed here when a caller drives one user directly, so that this function still
    means something on its own.
    """
    # Taken before the read, not after: the loop below re-reads `user_title` live but compares
    # it against this one snapshot of Jellyfin. An action taken *during* the sweep is newer
    # than the data it would be reconciled against, so it must be pushed, never adopted.
    #
    # From the DATABASE's clock, not this process's. `state_changed_at` is stamped by `now()`,
    # so a `datetime.now(UTC)` here compares two different clocks — and they are not the same
    # clock: measured on the development box, Postgres runs tens to hundreds of milliseconds
    # away from the application process, in either direction depending on the day. Both signs
    # break §7.3's rule silently. Server ahead: an action that finished before the sweep began
    # reads as "acted during the sweep" and is pushed, so a genuine Jellyfin-side change is
    # never adopted. Server behind: an action taken *during* the sweep reads as older than the
    # snapshot and is adopted — which reverts what the person just did, and is the reason this
    # is a correctness fix rather than a tidy-up.
    snapshot_at = await conn.fetchval("SELECT now()")
    if resolved is None:
        resolved = await resolve.upsert_items(conn, await client.all_items(None))
        report.resolve = resolved.as_dict()
    # The per-user read is for `UserData` and nothing else: Played is per-user, so the id in the
    # query decides whose flags come back, while identity came from the keyless read above.
    collapsed = _collapse(await client.all_items(user.jf_user_id), resolved)
    open_prompts = await _open_prompt_titles(conn, user.app_user_id)

    # §7.3's write path needs the member's own token and §14 risk 3 forbids the admin key as a
    # fallback, so a link without one is adopt-only. Hoisted out of the loop because the check is
    # a fact about the link, not about a row: taken per row it took the dead-token `break` below
    # and froze the sweep at the first title such a member had marked in the app — nothing after
    # it in `/Items` order was ever adopted, and nothing could settle the row either, since the
    # only thing that would is the write the missing token cannot make. `api/admin.py` creates
    # this state on purpose ("A link with no token is real but incomplete: it attributes playback
    # and feeds the P(seen) prior"), so skipping the member entirely would drop §3.3's prior that
    # the link exists for. [finding 1]
    adopt_only = user.token is None
    if adopt_only:
        if user.link_state != "needs_relink":
            # Written once, and only when it says something new. The per-row version of this UPDATE
            # re-issued itself every fifteen minutes against a row that already read
            # `needs_relink`, which is the churn half of the same finding.
            await _mark_needs_relink(conn, user.app_user_id)
        if user.name not in report.needs_relink:
            # Reported every sweep either way: §6.6's card is where the admin learns the link is
            # half-made, and a state that is already true is still the state.
            report.needs_relink.append(user.name)

    completed = True
    for title_id, (copies, jf_seen) in collapsed.items():
        kind = resolved.kinds.get(title_id)
        row = await conn.fetchrow(
            "SELECT state, state_changed_at, jf_synced_at FROM user_title "
            "WHERE user_id = $1 AND title_id = $2",
            user.app_user_id, title_id,
        )

        if row is None:
            if jf_seen and title_id not in open_prompts:
                await _adopt(conn, user.app_user_id, title_id, True)
                report.adopted += 1
            else:
                report.unchanged += 1
            continue

        app_seen = row["state"] == "seen"
        acted_during_this_sweep = row["state_changed_at"] > snapshot_at
        if row["jf_synced_at"] is None or acted_during_this_sweep:
            if adopt_only:
                report.owed_no_token += 1
                continue
            pushed, refusal, outcome = await _push_current(
                conn, client, user, title_id=title_id, kind=kind, copies=copies
            )
            if pushed:
                report.pushed += 1
                if outcome == PUSH_OK:
                    # Evidence, in the only form §7.3's badge may be cleared on: a write that the
                    # member's own token actually made. Decision 210's app-only series settlement
                    # is deliberately not evidence — it never touched the socket.
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
                # A tap on this very title took the per-(user, title) lock while the sweep reached
                # it — the one interleaving `acted_during_this_sweep` above routes straight here.
                # Nothing was sent and nothing was refused, so this is not a `push_failed`: the row
                # keeps its NULL `jf_synced_at`, the debt stands, and the next sweep settles it.
                # `completed` is deliberately untouched, which is the shape §7.3 already has for
                # owed-and-not-attempted (`owed_no_token`, above). [review cycle 2: m411-rev2-seen-03]
                report.unchanged += 1
            else:
                # §7.3 ties the re-link prompt to a 401 specifically, and this is not one: a 404
                # from a server below §7.1's pin, a 500, a proxy that drops DELETE. Counted and
                # carried past, and the sweep reports itself incomplete so that nothing downstream
                # reads it as health. Widening `is_auth_failure` to cover 404 would send the
                # household to re-type a password that cannot add a route to their server.
                report.push_failed += 1
                report._note_push_error(refusal or "unknown")
                completed = False
            continue

        if app_seen == jf_seen:
            report.unchanged += 1
            continue

        if title_id in open_prompts:
            # Decision 211 and §7.3:315, "Jellyfin playback is a suggestion, never a silent write".
            # The app is in the middle of asking "Did you finish X?"; adopting Jellyfin's flag here
            # answers it on the person's behalf, and `playback.pending` then closes the prompt as
            # `answered`, so §13's capture rate counts a sync as a reply. Only a tap closes it.
            report.unchanged += 1
            continue

        if kind == "series":
            # A computed folder flag decides nothing here, in either direction (decisions 210(4)
            # and 213). Jellyfin does not store Played on a Series: it computes
            # `playedCount >= totalCount`, so the value moves when nobody acts — while this
            # branch's whole premise is that a disagreement is a human change made after the last
            # agreement.
            #
            # Un-marking was the obvious half: the flag turns false the day Season 2 lands, and a
            # show cannot become less watched because the library grew. Marking is the half that
            # composed badly with decision 210(a), and it is decision 213's. For a series, the app
            # never sends the `unseen`: `_push` stamps `jf_synced_at` and returns without touching
            # the socket, so the stamp records a SETTLEMENT rather than an agreement, and the
            # folder stays played for ever by design. Reading that permanent, known, deliberate
            # disagreement as "Jellyfin changed, therefore adopt" reverted every app-side series
            # unseen — including decision 211's declining tap — within one sweep, with no in-app
            # remedy, under a surface that says the change is app-only.
            #
            # An ABSENT row is still adopted (the branch above): that is 210(4)'s real content —
            # Jellyfin's history against no assertion at all — and it is untouched here.
            report.unchanged += 1
            continue

        await _adopt(conn, user.app_user_id, title_id, jf_seen)
        report.adopted += 1

    # A push is owed for every row with a NULL `jf_synced_at`, but the sweep can only reach the
    # ones Jellyfin still lists. A title deleted from the library, or one whose item id changed
    # under a rebuild, leaves a debt nothing will ever settle — so it is counted rather than
    # silently forgotten.
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

    `client` is injectable so the same code path can be driven against a server that is not
    the configured one — which is how the integration tests reach `ops/fake_jellyfin.py`
    in-process instead of asserting against a mock of this function's own calls.

    The library is read once here, with the admin key and no `userId`, and resolved once: item
    visibility is per-user, and for §7.2's ownership "the household's library" is the admin's view
    of it rather than the union of two people's. (The documented alternative — resolve off the first
    linked user's page-set and take the owned set as the union across users — is N reads for a worse
    answer and would lose a title only one member can see.) Each member's own read then supplies
    nothing but `UserData`.
    """
    global _failed_users_logged

    from spielplan.connectors.registry import load_jellyfin, make_client

    report = SyncReport()
    cfg = await load_jellyfin(conn)
    if not cfg.configured:
        report.skipped_no_link = True
        return report

    # `make_client`, not a bare constructor: it threads §7.1's stored version verdict onto the
    # client, which is what lets the one write refuse by name instead of spending a 404 per tap.
    client = client or make_client(cfg)
    users = await linked_users(conn, cfg)
    if not users:
        report.skipped_no_link = True
        return report

    # §5.3's 15-minute job and §6.6's "sync now" must not sweep the same household against two
    # different library snapshots — the second sweep's adoptions would be decided from a page-set
    # the first one has already acted on. "A sweep is already running" is a better answer for the
    # admin card than two sweeps, and `pg_try_advisory_lock` is what makes it one statement. Session
    # level, released in `finally`, for the same pooled-connection reason as `_title_lock`.
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", _SWEEP_LOCK, 0):
        report.already_running = True
        log.info("a Jellyfin seen sweep is already running; this one did nothing")
        return report
    try:
        # §7.1's pin, re-probed rather than trusted: an operator upgrades the media server without
        # ever coming back to this app's admin page, and the stored verdict is what gates the write.
        # Written back only when it changed, because this runs every fifteen minutes and the config
        # row carries the AEAD-sealed secrets.
        try:
            raw, supported = await client.probe_version()
        except JellyfinError as exc:
            # Deliberately NOT `_note_unreachable`. This is a different request to a different
            # route — `/System/Info/Public`, the tokenless one whose own docstring calls it "the
            # one such guides tell people to block" — and the library read one statement later is
            # the authority on whether Jellyfin is reachable. Routed through the memo, a server
            # that answers `/Items` but not the probe SET and then CLEARED `_unreachable_since`
            # inside the same sweep, so ops-15's rate limit could never engage: 96 WARNINGs naming
            # an unreachable server and 96 INFOs saying it came back, every day, for a server that
            # was never down. A false "unreachable" is worse than the flood — it teaches the
            # operator to skip the real one. DEBUG, because nothing here is broken: the stored
            # verdict stands (§7.1) and a genuine outage speaks through the read below.
            # [M4.11 review cycle 2: m411-rev2-seen-02; §3.3, ops-15]
            log.debug("could not probe the Jellyfin version: %s", exc)
        else:
            if (raw, supported) != (cfg.server_version, cfg.server_supported):
                cfg = await save_jellyfin(conn, server_version=raw, server_supported=supported)
            client.server_version, client.server_supported = raw, supported

        try:
            library = await client.all_items(None)
        except JellyfinError as exc:
            # §3.3: Jellyfin being unreachable is a degraded sync, never a broken app. Nothing below
            # can be decided without the library — least of all §7.2's ownership pass — so the
            # report goes back as it stands, with `completed` empty, which is what keeps the
            # promotion at the foot of this function from reading an outage as health.
            _note_unreachable(exc)
            return report
        _note_reachable()
        resolved = await resolve.upsert_items(conn, library)
        # Once per sweep, not once per user: resolution is user-independent, and assigning it per
        # user made §6.6's card show only the last member's figures.
        report.resolve = resolved.as_dict()

        # BEFORE the per-user loop, not with the ownership pass after it. Step 2g put the prune
        # there and gave the reason in the same sentence — "or 2f's DELETE-every-copy will target
        # dead item ids" — which the placement itself defeated for one whole sweep: the loop below
        # pushes `unseen` against a map the previous sweep wrote, so a copy that left the library
        # was still named, and the DELETE that 404s on it was the first of the list. The gate the
        # prune needs is a completed, non-empty library read, and it is satisfied here — the caller
        # has already returned on a failed read and `prune_missing_items` refuses an empty one — so
        # this is the earliest point at which the map has been validated by the read the loop is
        # about to act on. [M4.11 review cycle 2: m411-rev2-resolve-02; §7.2, §7.3]
        pruned = await resolve.prune_missing_items(conn, resolved)
        if pruned:
            log.info("jellyfin library shrank: %d stale copy row(s) pruned", pruned)

        for user in users:
            report.users.append(user.name)
            try:
                if await sync_user(conn, client, user, report, resolved=resolved):
                    report.completed.append(user.name)
            except JellyfinError as exc:
                # Named in the report, not only in the log. The library read above succeeded, so
                # nothing downstream of here knew the sweep had lost a whole member — `completed`
                # simply got shorter and every counter stayed zero, which is exactly the shape
                # finding 3 called indistinguishable from health.
                #
                # And the line is ops-15's shape as well: loud the first sweep a member starts
                # failing, DEBUG while that stays true. A deleted or renamed Jellyfin account
                # 404s this read every fifteen minutes for as long as the mapping stands, and
                # the state now has a surface — §6.6's card names the member — so the log does
                # not have to repeat it 96 times a day.
                report.failed_users.append(user.name)
                log.log(
                    logging.WARNING if user.name not in _failed_users_logged else logging.DEBUG,
                    "seen sync for %s failed: %s", user.name, exc,
                )
        _failed_users_logged = frozenset(report.failed_users)

        await _falsify_ownership(conn, resolved, report)

        # Clear a stale re-link flag only on positive evidence, which is a Played write that
        # succeeded under this member's own token — `report.wrote`, not `report.completed`. A sweep
        # sends the token only for rows with `jf_synced_at IS NULL`, so a quiet sweep exercises a
        # revoked token never; `completed` promoted it anyway, under a comment claiming the
        # opposite, and the admin card then read green for a link that could write nothing.
        # [finding 2]
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
    """§7.2: "mark removed titles `is_owned = false` ... re-derived from Jellyfin, never trusted
    stale". Nothing in this codebase wrote false, so a film deleted from Jellyfin kept ownership,
    stayed in §6.2's candidate pool and on the owned Home shelves, and its winner card deep-linked
    to an item id the server no longer has.

    This is the most destructive statement in the module — a bug here un-owns the household's whole
    library and empties Tonight's pool — so it is gated twice. The caller has already returned on a
    failed library read, and `all_items` raises rather than truncating for exactly this reason: it
    counts distinct ids and refuses both a full page that repeats one it already has and a short
    page the server's own `TotalRecordCount` says is short, so neither a proxy caching page one nor
    one answered as an interstitial can hand this statement a page-one library to un-own the rest
    against. The second of those was the gap review cycle 2 closed: the only gate here is the empty
    resolution below, which a truncated-but-non-empty read passes. This refuses an empty resolution
    as well, because a library that resolved to nothing is a read that did not really happen rather
    than a household that owns nothing. [M4.11 review cycle 2: m411-rev2-resolve-01]

    `jellyfin_id IS NOT NULL` keeps the statement to titles Jellyfin ever carried: the importer's
    own `is_owned` for a title this server never had is not this sweep's to contradict.
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
    """Drop a stored per-user Jellyfin token.

    A token is bound to one Jellyfin identity, so it must not outlive the mapping that gave it
    meaning: re-pointing an account at a different Jellyfin user while keeping the old token
    would send that user's credential with the new user's id.

    The read-modify-write is under the connector row's own lock, because the map is AEAD-sealed and
    therefore cannot be merged in SQL: two members being linked or unlinked at once each read the
    sealed map, each dropped or added one entry, and the second write lost the first (`as05`). An
    account left reading `linked` with no token is not a cosmetic loss either — it is the state
    `sync_user`'s adopt-only branch above exists to survive. `unlink` already opens a transaction,
    and a nested `transaction()` is a savepoint, so this is safe both ways in.
    """
    from spielplan.connectors.registry import load_jellyfin

    async with conn.transaction():
        cfg = await load_jellyfin(conn, for_update=True)
        if str(app_user_id) in cfg.user_tokens:
            tokens = dict(cfg.user_tokens)
            tokens.pop(str(app_user_id), None)
            await save_jellyfin(conn, user_tokens=tokens)


async def unlink(conn: asyncpg.Connection, app_user_id: int) -> None:
    """Drop the link and forget that user's token. §3.3: the link is optional, so removing it
    must leave a fully functional account behind — seen state stays, it just stops syncing.

    One transaction: the route's connection is autocommit, so clearing the mapping and
    forgetting the token as two statements can leave a live access token stored against an
    account that no longer names the Jellyfin user it belongs to.
    """
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
