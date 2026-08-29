"""Playback watcher and the finish prompt. Spec v2.1 §7.3.

"**Jellyfin playback is a suggestion, never a silent write:** >= 90% playback (poll
`/Sessions` + `IsPlayed` delta) **arms a per-user prompt** — 'Did you finish X?' -> one tap
sets `seen` and offers the verdict flow. Push notification if the user isn't in the app —
best-effort; when undeliverable, the prompt queues and surfaces as an in-app banner on next
open. The banner path is the whole M1 behaviour; push arrives with the M4 stack."

Nothing in this module writes `user_title`. Arming a prompt is the entire effect, and
`answer_prompt` — reached only by a person tapping — is the only place a state write happens.
That separation is the point: inferred state must never outrank the person, and §13 measures
rating capture against *finished playbacks*, which stops being a real denominator the moment
the app starts marking things seen on its own.

The prompt is armed once per viewing, not once per poll. A film sits above 90% for its last
ten minutes and the poll runs every minute; without the partial unique index on
`(user_id, title_id) WHERE prompt_state IN ('armed','shown')` the household answers the same
question ten times.

The other half of that guard is here rather than in the index: a prompt is never armed for a
title the person is already recorded as having seen. §7.3 makes the sync two-way, so Jellyfin's
own Played flag can — correctly — have arrived first and been adopted; asking "Did you finish
X?" about something the app already shows as seen is a question with no answer, and the card's
own copy ("nothing is marked until you say so") would be false when it appeared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError, NowPlaying
from spielplan.core.config import settings

log = logging.getLogger("spielplan.sync.playback")

OPEN_STATES = ("armed", "shown")


@dataclass
class WatchReport:
    armed: int = 0
    already_armed: int = 0
    watching: int = 0
    unresolved: list[str] = field(default_factory=list)
    skipped_no_link: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "already_armed": self.already_armed,
            "watching": self.watching,
            "unresolved": self.unresolved[:10],
            "skipped_no_link": self.skipped_no_link,
        }


async def arm(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    session_id: str,
    progress: float,
    source: str = "jellyfin",
) -> bool:
    """Record a finished playback and arm its prompt. True if this call armed it.

    `ON CONFLICT DO NOTHING` against the partial unique index is what makes the poll
    idempotent — including against two polls that overlap, which a lock-free `SELECT` first
    would not be.

    A title the person already has as `seen` arms nothing: the two-way sync (§7.3) may have
    adopted Jellyfin's own Played flag minutes earlier, and a prompt asking about a title the
    app already shows as seen is both redundant and, in its own words, untrue.
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
        VALUES ($1, $2, $3, true, $4, $5)
        ON CONFLICT (user_id, title_id) WHERE finished AND prompt_state IN ('armed', 'shown')
        DO NOTHING
        RETURNING id
        """,
        source, title_id, user_id, progress, session_id,
    )
    return row is not None


async def observe(
    conn: asyncpg.Connection, sessions: list[NowPlaying], report: WatchReport
) -> None:
    """Turn one `/Sessions` reading into armed prompts.

    A session counts as finished when it has passed the threshold *or* when Jellyfin already
    marks the item played — §7.3 names both the ">= 90%" and the "IsPlayed delta" paths, and a
    viewer who stops at 88% and taps "mark watched" in Jellyfin should still be asked.
    """
    threshold = settings().finish_threshold
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
            # An unlinked Jellyfin user is not an error (§3.3: linking is optional) — there is
            # simply nobody in this app to ask.
            continue
        if not (playing.played or playing.fraction >= threshold):
            continue
        title_id = await conn.fetchval(
            "SELECT id FROM title WHERE jellyfin_id = $1", playing.item_id
        )
        if title_id is None:
            report.unresolved.append(playing.item_id)
            continue
        if await arm(
            conn,
            user_id=user_id,
            title_id=title_id,
            session_id=playing.session_id,
            progress=playing.fraction,
        ):
            report.armed += 1
        else:
            report.already_armed += 1


async def poll(conn: asyncpg.Connection, client: JellyfinClient | None = None) -> WatchReport:
    """The 1-minute job. Reads `/Sessions` and arms whatever finished.

    `client` is injectable for the same reason `seen.sync_all`'s is: the integration tests
    drive this against the in-process fake server rather than a mock of the call.
    """
    from spielplan.connectors.registry import load_jellyfin

    report = WatchReport()
    cfg = await load_jellyfin(conn)
    if not cfg.configured:
        report.skipped_no_link = True
        return report
    try:
        sessions = await (client or JellyfinClient(cfg.url, cfg.api_key)).sessions()
    except JellyfinError as exc:
        log.warning("/Sessions poll failed: %s", exc)
        return report
    await observe(conn, sessions, report)
    return report


async def pending(conn: asyncpg.Connection, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """The queued prompts for one person, newest first.

    Reading them marks them `shown`. §7.3's banner is the undeliverable-push fallback, so
    "has this been surfaced at least once" is a real distinction — it is what M4's push layer
    will check before sending a notification for something already answered on screen.
    """
    # A prompt whose title has since become `seen` — because the person marked it, or because
    # the 15-minute sync adopted Jellyfin's own Played flag (§7.3) — is closed rather than
    # shown. The question has been answered; asking it again would contradict what every other
    # surface already says.
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
    """The tap. `finished` true writes `seen` — the only state write on the playback path.

    Both answers close the prompt: "no" is an answer, and a card that comes back after being
    dismissed teaches people to ignore the banner.
    """
    row = await conn.fetchrow(
        "SELECT title_id FROM playback_event "
        "WHERE id = $1 AND user_id = $2 AND prompt_state = ANY($3::text[])",
        event_id, user_id, list(OPEN_STATES),
    )
    if row is None:
        return {"ok": False, "reason": "no open prompt with that id"}

    await conn.execute(
        "UPDATE playback_event SET prompt_state = $2 WHERE id = $1",
        event_id, "answered" if finished else "dismissed",
    )
    result: dict[str, Any] = {"ok": True, "title_id": row["title_id"], "seen": finished}
    if finished:
        from spielplan.connectors.registry import load_jellyfin
        from spielplan.sync import seen as seen_sync

        cfg = await load_jellyfin(conn)
        if client is None and cfg.configured:
            client = JellyfinClient(cfg.url, cfg.api_key)
        result["sync"] = await seen_sync.set_state(
            conn, client, cfg, user_id=user_id, title_id=row["title_id"], state="seen"
        )
    return result


__all__ = ["WatchReport", "answer", "arm", "observe", "pending", "poll"]
