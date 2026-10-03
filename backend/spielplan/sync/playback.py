"""Playback watcher and the finish prompt (§7.3): watching only arms a prompt; `answer` writes.

Both taps write (decision 211). A series resolves to the show and asks only on its last episode
(decision 210). Once per viewing; never for a title already seen, or a viewing already declined.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError, NowPlaying, Outage

log = logging.getLogger("spielplan.sync.playback")

OPEN_STATES = ("armed", "shown")
# A prompt put away by its x is answered by nobody, and the seen sync keeps away from it for good (§7.3).
GUARD_STATES = (*OPEN_STATES, "closed")

_outage = Outage(log)

# §7.3: ">= 90% playback ... arms a per-user prompt"; the spec's number, not a setting.
FINISH_THRESHOLD = 0.9


@dataclass
class WatchReport:
    armed: int = 0
    already_armed: int = 0
    watching: int = 0
    unresolved: list[str] = field(default_factory=list)
    # Resolved sessions whose episode list could not be read (decision 210(c)); not "unresolved".
    undecided: list[str] = field(default_factory=list)
    skipped_no_link: bool = False
    # Whether `/Sessions` answered, for §6.6's "last syncs" (decision 454).
    reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "already_armed": self.already_armed,
            "watching": self.watching,
            "unresolved": self.unresolved[:10],
            "undecided": self.undecided[:10],
            "skipped_no_link": self.skipped_no_link,
            "reached": self.reached,
        }


async def arm(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    session_id: str,
    progress: float,
) -> bool:
    """Record a finished playback and arm its prompt. True if this call armed it.

    Idempotent via the partial unique index. Nothing for a title already `seen`, and nothing for a
    viewing (`jf_session_id`) already declined or closed; a rewatch on the same device is not asked again.
    """
    already_seen = await conn.fetchval(
        "SELECT 1 FROM user_title WHERE user_id = $1 AND title_id = $2 AND state = 'seen'",
        user_id, title_id,
    )
    if already_seen:
        return False

    row = await conn.fetchrow(
        """
        INSERT INTO playback_event (source, title_id, user_id, finished, progress, jf_session_id)
        SELECT 'jellyfin', $1::integer, $2::bigint, true, $3::real, $4::text
         WHERE NOT EXISTS (SELECT 1 FROM playback_event
                            WHERE user_id = $2 AND title_id = $1 AND jf_session_id = $4
                              AND prompt_state IN ('dismissed', 'closed'))
        ON CONFLICT (user_id, title_id) WHERE finished AND prompt_state IN ('armed', 'shown')
        DO NOTHING
        RETURNING id
        """,
        title_id, user_id, progress, session_id,
    )
    return row is not None


async def _resolve_session(conn: asyncpg.Connection, playing: NowPlaying) -> int | None:
    """The title this session is playing, or None. Resolve only — never upsert (§7.2).

    `series_id or item_id` on `title.jellyfin_id` (decision 210), then the copy map, then the
    item's own provider ids.
    """
    key = playing.series_id or playing.item_id
    title_id = await conn.fetchval("SELECT id FROM title WHERE jellyfin_id = $1", key)
    if title_id is None:
        title_id = await conn.fetchval(
            "SELECT title_id FROM title_jellyfin_item WHERE jellyfin_id = $1", key
        )
    if title_id is None and playing.raw:
        title_id = await resolve.resolve_title_id(conn, playing.raw)
    return title_id


async def _series_finished(client: JellyfinClient | None, playing: NowPlaying) -> bool | None:
    """Is this episode the last one the server lists? None when that cannot be answered.

    Decision 210(c): not the Series-level `Played` flag and not per-episode arming. Undecidable
    arms nothing.
    """
    if client is None:
        # Only callers that resolve their own sessions; `poll` always has a client.
        return None
    try:
        episodes = await client.episodes(str(playing.series_id), playing.jf_user_id)
    except JellyfinError as exc:
        log.debug("cannot list the episodes of %s: %s", playing.series_id, exc)
        return None
    if not episodes:
        return None
    last = str((episodes[-1] or {}).get("Id") or "")
    return bool(last) and last == playing.item_id


async def observe(
    conn: asyncpg.Connection,
    sessions: list[NowPlaying],
    report: WatchReport,
    *,
    client: JellyfinClient | None = None,
) -> None:
    """Turn one `/Sessions` reading into armed prompts: past the threshold, or marked played."""
    users = {
        r["jellyfin_user_id"]: r["id"]
        for r in await conn.fetch(
            "SELECT id, jellyfin_user_id FROM app_user "
            "WHERE jellyfin_user_id IS NOT NULL AND is_active"
        )
    }

    for playing in sessions:
        report.watching += 1
        user_id = users.get(playing.jf_user_id)
        if user_id is None:
            # An unlinked Jellyfin user is not an error (§3.3); there is nobody to ask.
            continue
        if not (playing.played or playing.fraction >= FINISH_THRESHOLD):
            continue
        title_id = await _resolve_session(conn, playing)
        if title_id is None:
            # The id that was playing, which an operator can paste into Jellyfin.
            report.unresolved.append(playing.item_id)
            continue
        if playing.series_id:
            # An episode, keyed as resolution keyed it.
            finished = await _series_finished(client, playing)
            if finished is None:
                report.undecided.append(playing.item_id)
                continue
            if not finished:
                # Mid-series: §7.3's question is about the show.
                continue
        if await arm(
            conn,
            user_id=user_id,
            title_id=title_id,
            session_id=playing.session_id,
            progress=playing.fraction,
        ):
            report.armed += 1
            await notify(conn, user_id=user_id, title_id=title_id)
        else:
            report.already_armed += 1


async def notify(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> None:
    """§7.3's push for an armed prompt: best-effort, once per arming; the banner stands regardless."""
    try:
        from spielplan.push import send as push_send

        name = await conn.fetchval("SELECT name FROM title WHERE id = $1", title_id)
        await push_send.send_to_user(
            conn,
            user_id,
            {
                "kind": "playback.finished",
                "title_id": title_id,
                "title": "Spielplan",
                # §7.3's own words, so the notification and the banner ask the same question.
                "body": f"Did you finish {name}?",
                # The worker only renders: a per-title tag, so no other notification replaces this.
                "tag": f"finish:{title_id}",
                # Home, where `FinishPrompt` is mounted.
                "url": "/",
            },
        )
    except Exception:
        # Best-effort: the in-app banner is already queued (§6 preamble).
        log.info("finish-prompt push for user %s was not delivered", user_id)


async def poll(conn: asyncpg.Connection, client: JellyfinClient | None = None) -> WatchReport:
    """The 1-minute job. Reads `/Sessions` and arms whatever finished."""
    from spielplan.connectors import registry

    report = WatchReport()
    cfg = await registry.load_jellyfin(conn)
    if not cfg.configured:
        report.skipped_no_link = True
        return report
    # `make_client` threads the §7.1 version verdict onto the client.
    client = client or registry.make_client(cfg)
    try:
        sessions = await client.sessions()
    except JellyfinError as exc:
        _outage.down(exc)
        return report
    report.reached = True
    _outage.up()
    await observe(conn, sessions, report, client=client)
    return report


async def pending(conn: asyncpg.Connection, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """The queued prompts for one person, newest first; reading them marks them `shown`."""
    # A prompt whose title has since become `seen` (on another surface) is closed, not shown.
    await conn.execute(
        """
        UPDATE playback_event e SET prompt_state = 'answered'
         WHERE e.user_id = $1 AND e.finished AND e.prompt_state = ANY($2::text[])
           AND EXISTS (SELECT 1 FROM user_title ut
                        WHERE ut.user_id = e.user_id AND ut.title_id = e.title_id
                          AND ut.state = 'seen')
        """,
        user_id, list(OPEN_STATES),
    )
    rows = await conn.fetch(
        """
        SELECT e.id, e.title_id, e.progress, e.at, t.name, t.kind, t.year, t.poster_path
          FROM playback_event e JOIN title t ON t.id = e.title_id
         WHERE e.user_id = $1 AND e.finished AND e.prompt_state = ANY($2::text[])
         ORDER BY e.at DESC
         LIMIT $3
        """,
        user_id, list(OPEN_STATES), limit,
    )
    if rows:
        await conn.execute(
            "UPDATE playback_event SET prompt_state = 'shown' "
            "WHERE id = ANY($1::bigint[]) AND prompt_state = 'armed'",
            [r["id"] for r in rows],
        )
    return [dict(r) for r in rows]


async def answer(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    event_id: int,
    finished: bool,
    client: JellyfinClient | None = None,
) -> dict[str, Any]:
    """The tap, and the only place this module writes `user_title`: `seen` or `unseen` (decision 211).

    Config first, then the state write, then consuming the prompt, so a failure never loses the
    answer. Not one transaction: the Jellyfin push must not hold database rows (§3.3).
    """
    row = await conn.fetchrow(
        "SELECT title_id, at FROM playback_event "
        "WHERE id = $1 AND user_id = $2 AND prompt_state = ANY($3::text[])",
        event_id, user_id, list(OPEN_STATES),
    )
    if row is None:
        return {"ok": False, "reason": "no open prompt with that id"}

    from spielplan.connectors import registry
    from spielplan.sync import seen as seen_sync

    # Before anything is written, so its failures cost only the tap.
    cfg = await registry.load_jellyfin(conn)
    if client is None:
        client = registry.make_client(cfg)
    sync = await seen_sync.set_state(
        conn, client, cfg,
        user_id=user_id, title_id=row["title_id"], state="seen" if finished else "unseen",
    )
    if finished:
        await conn.execute(
            "UPDATE user_title SET played_at = greatest(played_at, $3) "
            "WHERE user_id = $1 AND title_id = $2",
            user_id, row["title_id"], row["at"],
        )
    await conn.execute(
        "UPDATE playback_event SET prompt_state = $2 WHERE id = $1",
        event_id, "answered" if finished else "dismissed",
    )
    return {"ok": True, "title_id": row["title_id"], "seen": finished, "sync": sync}


async def close(conn: asyncpg.Connection, *, user_id: int, event_id: int) -> dict[str, Any]:
    """The prompt's x on Home (decision 554): no answer, so no `user_title` write."""
    title_id = await conn.fetchval(
        "UPDATE playback_event SET prompt_state = 'closed' "
        "WHERE id = $1 AND user_id = $2 AND prompt_state = ANY($3::text[]) RETURNING title_id",
        event_id, user_id, list(OPEN_STATES),
    )
    if title_id is None:
        return {"ok": False, "reason": "no open prompt with that id"}
    return {"ok": True, "title_id": title_id}


async def reopen(conn: asyncpg.Connection, *, user_id: int, event_id: int) -> dict[str, Any]:
    """The x's Undo: back to `shown`, while no other prompt for the title has opened since."""
    try:
        reopened = await conn.fetchval(
            """
            UPDATE playback_event e SET prompt_state = 'shown'
             WHERE e.id = $1 AND e.user_id = $2 AND e.prompt_state = 'closed'
               AND NOT EXISTS (SELECT 1 FROM playback_event o
                                WHERE o.user_id = e.user_id AND o.title_id = e.title_id
                                  AND o.finished AND o.prompt_state = ANY($3::text[]))
            RETURNING id
            """,
            event_id, user_id, list(OPEN_STATES),
        )
    except asyncpg.UniqueViolationError:
        # Another prompt for the title armed between the check and the write.
        reopened = None
    if reopened is None:
        return {"ok": False, "reason": "no closed prompt with that id to reopen"}
    return {"ok": True}


__all__ = [
    "GUARD_STATES", "OPEN_STATES", "WatchReport", "answer", "arm", "close", "observe", "pending",
    "poll", "reopen",
]
