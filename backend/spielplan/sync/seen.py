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
"""

from __future__ import annotations

import logging
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


@dataclass
class LinkedUser:
    app_user_id: int
    name: str
    jf_user_id: str
    token: str | None
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
    # Users whose sweep actually completed. `needs_relink` being empty is not evidence of
    # health — it is also what a Jellyfin outage looks like.
    completed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pushed": self.pushed,
            "adopted": self.adopted,
            "unchanged": self.unchanged,
            "needs_relink": self.needs_relink,
            "owed_unreachable": self.owed_unreachable,
            "resolve": self.resolve,
            "users": self.users,
            "completed": self.completed,
            "skipped_no_link": self.skipped_no_link,
        }


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
    """§7.3: 'a 401 on write -> re-link prompt.' The app-side state stays written and
    `jf_synced_at` stays NULL, so the write is owed, not lost — the next sync after a
    successful re-link pushes it."""
    await conn.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' "
        "WHERE id = $1 AND jellyfin_user_id IS NOT NULL",
        app_user_id,
    )


async def _push(
    conn: asyncpg.Connection,
    client: JellyfinClient,
    user: LinkedUser,
    *,
    title_id: int,
    jellyfin_id: str,
    seen: bool,
) -> tuple[bool, str | None]:
    """Write one Played flag with that user's own token. Returns (pushed, refusal)."""
    if not user.token:
        # §7.3's least-privilege path: the admin key would work and is explicitly not used.
        # No token means the link is half-made, which is a re-link prompt, not a fallback.
        await _mark_needs_relink(conn, user.app_user_id)
        return False, "no per-user Jellyfin token — re-link required"
    try:
        await client.set_played(jellyfin_id, user.jf_user_id, seen, user.token)
    except JellyfinError as exc:
        if exc.is_auth_failure:
            await _mark_needs_relink(conn, user.app_user_id)
            return False, "Jellyfin rejected the per-user token — re-link required"
        # Transport or server error: leave jf_synced_at NULL and try again next sync.
        log.warning("Played write for user %s failed: %s", user.app_user_id, exc)
        return False, str(exc)

    await conn.execute(
        "UPDATE user_title SET jf_synced_at = now() WHERE user_id = $1 AND title_id = $2",
        user.app_user_id, title_id,
    )
    return True, None


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
    """
    state = await conn.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user_id, title_id
    )
    if state is None:
        # §7.3: "an absent row is the *default*, not an assertion" — there is nothing owed and
        # nothing to say to Jellyfin. Reached only by a caller that pushed without writing.
        return False, "nothing owed"

    jellyfin_id = await conn.fetchval("SELECT jellyfin_id FROM title WHERE id = $1", title_id)
    if client is None or not jellyfin_id:
        if client is not None:
            return False, "not on Jellyfin"
        if cfg.secrets_unreadable:
            # The tap is kept either way — that is §3.3 — but "Jellyfin not configured" would be
            # a lie an admin cannot act on: the connector *is* configured and its credentials are
            # sealed under a SECRETS_KEY this process does not have (M4.7 dd03).
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"

    users = {u.app_user_id: u for u in await linked_users(conn, cfg)}
    user = users.get(user_id)
    if user is None:
        return False, "this account is not linked to a Jellyfin user"

    return await _push(
        conn, client, user, title_id=title_id, jellyfin_id=jellyfin_id, seen=state == "seen"
    )


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
    out of an absence. `_push`'s own stamp is safe here for the same reason it is elsewhere: its
    UPDATE names a row that either exists and is ours to stamp, or does not exist and takes no
    rows — which is what the old code's `if prior.existed` guard spelled out by hand.
    """
    if client is None:
        if cfg.secrets_unreadable:
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"

    jellyfin_id = await conn.fetchval("SELECT jellyfin_id FROM title WHERE id = $1", title_id)
    if not jellyfin_id:
        return False, "not on Jellyfin"

    users = {u.app_user_id: u for u in await linked_users(conn, cfg)}
    user = users.get(user_id)
    if user is None:
        return False, "this account is not linked to a Jellyfin user"

    return await _push(
        conn, client, user, title_id=title_id, jellyfin_id=jellyfin_id,
        seen=prior_state == "seen",
    )


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


async def _collapse(
    conn: asyncpg.Connection, items: list[dict], resolved: resolve.ResolveReport
) -> dict[int, tuple[str, bool]]:
    """Resolve a library page-set to `{title_id: (representative item id, played)}`.

    Several Jellyfin items can be one title: a household with "Movies" and "Movies 4K", a
    duplicated folder, two rips of the same film. `resolve.upsert_item` maps them all to the
    same `title.id` and even counts the case as `relinked`.

    Reconciling per *item* would then let the second copy overwrite the decision the first one
    just made — and because `jf_synced_at` records agreement per (user, title) while agreement
    is actually per (user, item), the second copy's disagreement always looks newer. The result
    is an explicit `seen` erased by a duplicate that was never played, decided by whatever
    order `/Items` happened to return.

    So the flags are OR-ed across duplicates — Jellyfin marks Played on the copy that was
    actually watched — and the played copy becomes the representative, because that is the item
    a later write should target.
    """
    collapsed: dict[int, tuple[str, bool]] = {}
    for item in items:
        title_id = await resolve.upsert_item(conn, item, resolved)
        if title_id is None:
            continue
        item_id = str(item.get("Id") or "")
        is_played = played_of(item)
        current = collapsed.get(title_id)
        if current is None:
            collapsed[title_id] = (item_id, is_played)
        elif is_played and not current[1]:
            collapsed[title_id] = (item_id, True)

    for title_id, (item_id, _played) in collapsed.items():
        # Keep `title.jellyfin_id` pointing at the same copy this sync reconciled against, so
        # an explicit `set_state` (which pushes to `title.jellyfin_id`) and the sync do not
        # write to two different items and then disagree about the result.
        await conn.execute(
            "UPDATE title SET jellyfin_id = $2 WHERE id = $1 AND jellyfin_id IS DISTINCT FROM $2",
            title_id, item_id,
        )
    return collapsed


async def sync_user(
    conn: asyncpg.Connection,
    client: JellyfinClient,
    user: LinkedUser,
    report: SyncReport,
) -> bool:
    """One linked user's whole library, reconciled by the table at the top of this module.

    Returns True when the sweep completed — which is what lets `sync_all` clear a stale
    re-link flag on evidence rather than on the absence of bad news.
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
    items = await client.all_items(user.jf_user_id)
    resolved = resolve.ResolveReport()
    collapsed = await _collapse(conn, items, resolved)

    completed = True
    for title_id, (item_id, jf_seen) in collapsed.items():
        row = await conn.fetchrow(
            "SELECT state, state_changed_at, jf_synced_at FROM user_title "
            "WHERE user_id = $1 AND title_id = $2",
            user.app_user_id, title_id,
        )

        if row is None:
            if jf_seen:
                await _adopt(conn, user.app_user_id, title_id, True)
                report.adopted += 1
            else:
                report.unchanged += 1
            continue

        app_seen = row["state"] == "seen"
        acted_during_this_sweep = row["state_changed_at"] > snapshot_at
        if row["jf_synced_at"] is None or acted_during_this_sweep:
            pushed, refusal = await _push(
                conn, client, user, title_id=title_id, jellyfin_id=item_id, seen=app_seen
            )
            if pushed:
                report.pushed += 1
            elif refusal and "re-link" in refusal:
                if user.name not in report.needs_relink:
                    report.needs_relink.append(user.name)
                # The token is dead for every remaining title too; stop hammering the server.
                completed = False
                break
            continue

        if app_seen == jf_seen:
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
    report.resolve = resolved.as_dict()
    return completed


async def sync_all(
    conn: asyncpg.Connection, client: JellyfinClient | None = None
) -> SyncReport:
    """The 15-minute job (§5.3 `jellyfin-seen-sync`), and the admin's "sync now" button.

    `client` is injectable so the same code path can be driven against a server that is not
    the configured one — which is how the integration tests reach `ops/fake_jellyfin.py`
    in-process instead of asserting against a mock of this function's own calls.
    """
    from spielplan.connectors.registry import load_jellyfin

    report = SyncReport()
    cfg = await load_jellyfin(conn)
    if not cfg.configured:
        report.skipped_no_link = True
        return report

    client = client or JellyfinClient(cfg.url, cfg.api_key)
    users = await linked_users(conn, cfg)
    if not users:
        report.skipped_no_link = True
        return report

    for user in users:
        report.users.append(user.name)
        try:
            if await sync_user(conn, client, user, report):
                report.completed.append(user.name)
        except JellyfinError as exc:
            # §3.3: Jellyfin being unreachable is a degraded sync, never a broken app.
            log.warning("seen sync for %s failed: %s", user.name, exc)

    # Clear a stale re-link flag only on positive evidence. "Nothing failed" is not evidence:
    # a push is only attempted for rows that owe one, `sync_user` raising is swallowed just
    # above, and a link with no token has nothing to push at all — all three would otherwise
    # promote a broken link to "linked" and tell the admin everything is fine.
    for user in users:
        if user.link_state != "needs_relink":
            continue
        if user.token and user.name in report.completed:
            await conn.execute(
                "UPDATE app_user SET jellyfin_link_state = 'linked' WHERE id = $1",
                user.app_user_id,
            )
    return report


async def forget_token(conn: asyncpg.Connection, app_user_id: int) -> None:
    """Drop a stored per-user Jellyfin token.

    A token is bound to one Jellyfin identity, so it must not outlive the mapping that gave it
    meaning: re-pointing an account at a different Jellyfin user while keeping the old token
    would send that user's credential with the new user's id.
    """
    from spielplan.connectors.registry import load_jellyfin

    cfg = await load_jellyfin(conn)
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
