"""Playback watcher and the finish prompt. Spec v2.1 §7.3.

"**Jellyfin playback is a suggestion, never a silent write:** >= 90% playback (poll
`/Sessions` + `IsPlayed` delta) **arms a per-user prompt** — 'Did you finish X?' -> one tap
sets `seen` and offers the verdict flow. Push notification if the user isn't in the app —
best-effort; when undeliverable, the prompt queues and surfaces as an in-app banner on next
open. The banner path is the whole M1 behaviour; push arrives with the M4 stack."

Nothing in this module writes `user_title` on its own. Arming a prompt is the entire effect of
watching, and `answer` — reached only by a person tapping — is the only place a state write
happens. That separation is the point: inferred state must never outrank the person, and §13
measures rating capture against *finished playbacks*, which stops being a real denominator the
moment the app starts marking things seen on its own.

Under decision 211 *both* taps write. "No" used to write nothing at all, which left `user_title`
empty — and an empty row is exactly what the 15-minute sweep adopts Jellyfin's own Played flag
into, so the app asked, the person answered, and the sweep answered differently inside the quarter
hour. A decline is now an explicit `unseen` through `seen.set_state`; the sweep's half of the same
rule is `seen._open_prompt_titles`, which stops it adopting for a (user, title) whose prompt is
still open.

A session on a series plays an **episode**, and §4.1 rule 5 gives the app no title for one. So the
prompt resolves `series_id or item_id` (decision 210) and asks about the show — and asks only when
that episode is the last one the server knows about, which is decision 210(c), argued at
`_series_finished`. Resolving the episode's own id is how the whole Series partition came to sit
outside §13's capture loop while capture read healthy.

The prompt is armed once per viewing, not once per poll. A film sits above 90% for its last
ten minutes and the poll runs every minute; without the partial unique index on
`(user_id, title_id) WHERE prompt_state IN ('armed','shown')` the household answers the same
question ten times.

The other half of that guard is here rather than in the index: a prompt is never armed for a
title the person is already recorded as having seen. §7.3 makes the sync two-way, so Jellyfin's
own Played flag can — correctly — have arrived first and been adopted; asking "Did you finish
X?" about something the app already shows as seen is a question with no answer, and the card's
own copy ("nothing is marked until you say so") would be false when it appeared.

And the third guard is the dismissal, which the index cannot express either: a declined viewing is
not asked again. `arm` carries the argument, including what keying it on `jf_session_id` costs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError, NowPlaying

log = logging.getLogger("spielplan.sync.playback")

OPEN_STATES = ("armed", "shown")

# How long Jellyfin has been unreachable from this process, or None while it answers (ops-15).
# `time.monotonic` rather than a wall clock: the only question asked of it is how long the outage
# lasted, and an NTP step mid-outage would print a negative or an absurd number. It also keeps
# this module free of `datetime`, the way `sync/seen.py` is for its own stated reason.
_unreachable_since: float | None = None

# §7.3: ">= 90% playback ... arms a per-user prompt". A module constant, not a `Settings` field,
# because §7.3 states one number and no proposal asks for a knob: this was a `finish_threshold`
# setting carrying a comment inviting a household to lower it, and the name appeared in neither
# `.env.example` nor `docker-compose.yml`, so no operator of the stack this repository ships could
# have reached it anyway. Configurability nobody asked for and nobody can use is the kind
# CLAUDE.md forbids; the spec is where this number changes. [M4.7 ds07]
FINISH_THRESHOLD = 0.9


def _note_unreachable(exc: JellyfinError) -> None:
    """Log an unreachable Jellyfin once, not once a minute, for as long as it stays down.

    The behaviour was already right (§3.3: a degraded sync, never a broken app); the observability
    was not. This poll runs every sixty seconds, so a box powered off overnight wrote ten hours of
    identical warnings from here and another every fifteen minutes from the sweep — which is what
    buried the backup and refit lines an operator does want. Deliberately not a backoff schedule
    and not a cadence change: §7.3's prompt window is minutes long, and §8 calls a retry knob
    nobody asked for configurability.

    `sync/seen.py` keeps its own pair rather than sharing this one, because the sweep and the poll
    are two outages to an operator reading the log — and because importing `seen` at module scope
    here would invert the lazy import `seen._open_prompt_titles` already makes to avoid the cycle.
    [M4.11 finding 18]
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


@dataclass
class WatchReport:
    armed: int = 0
    already_armed: int = 0
    watching: int = 0
    unresolved: list[str] = field(default_factory=list)
    # Sessions that DID resolve to a title and whose series episode list could not be read
    # (decision 210(c)'s "undecidable is not yes"). A separate list because `unresolved` is what an
    # operator is told "matched no title" about, and the repair that sentence points at — import
    # the title, add a provider id — is the wrong repair here: the title is already imported, the
    # episode id resolves in Jellyfin perfectly, and the fault is a `/Shows/{id}/Episodes` the
    # server blocked or errored. One list said both, so the household with a proxy rule in front of
    # that route was told to fix the one thing that was not broken.
    # [M4.11 review cycle 2: m411-rev2-pb-02; decision 210(c), ops-15]
    undecided: list[str] = field(default_factory=list)
    skipped_no_link: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "already_armed": self.already_armed,
            "watching": self.watching,
            "unresolved": self.unresolved[:10],
            "undecided": self.undecided[:10],
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

    The third guard is the dismissal, and it is the one the index cannot express. `answer`'s own
    rule — "a card that comes back after being dismissed teaches people to ignore the banner" —
    had nothing enforcing it: a "no" produced neither an open prompt nor a `seen` row, the film sat
    above the threshold for another ten minutes, and every one of those polls armed a fresh prompt
    and sent a fresh push (measured: two rows, two pushes). So the insert refuses a (user, title)
    this **viewing** was already declined for, keyed on `jf_session_id` — which is what
    `0006_jellyfin.sql` recorded that column for. Not the index predicate: widening it to
    `'dismissed'` would refuse every later viewing from every device, for good. The cost, stated
    rather than hidden: Jellyfin derives `SessionInfo.Id` from the client and device, so a rewatch
    on the *same* device is not asked again either — the viewing that asks again is the one that
    arrives as a new session. A row with no session id (§11's external-playback path) is keyed to
    no viewing and is never refused by this clause. [M4.11 finding 11]
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
        SELECT $1::text, $2::integer, $3::bigint, true, $4::real, $5::text
         WHERE NOT EXISTS (SELECT 1 FROM playback_event
                            WHERE user_id = $3 AND title_id = $2 AND jf_session_id = $5
                              AND prompt_state = 'dismissed')
        ON CONFLICT (user_id, title_id) WHERE finished AND prompt_state IN ('armed', 'shown')
        DO NOTHING
        RETURNING id
        """,
        source, title_id, user_id, progress, session_id,
    )
    return row is not None


async def _resolve_session(conn: asyncpg.Connection, playing: NowPlaying) -> int | None:
    """The title this session is playing, or None. Resolve only — never upsert.

    Three lookups, first hit wins, ordered from the cheapest and most certain to the one that can
    be wrong:

      1. `title.jellyfin_id` for `series_id or item_id` (decision 210). Jellyfin never plays a
         Series: the row carries the **episode's** own id and a `SeriesId` for the folder, so
         resolving `item_id` alone reported every television session as unresolved.
      2. `title_jellyfin_item`, the copy map the sweep writes (§7.1). The pointer above names one
         representative copy per title, and a household with two copies of a show has a second
         `SeriesId` that the pointer does not know — which an Episode cannot work around through
         (3) either, because `resolve.kind_of` knows only Movie and Series.
      3. `resolve.resolve_title_id` on the item itself, which already knows ProviderIds, kind and
         the name/year fallback's refusals — so a film played from a copy no sweep has recorded
         yet still resolves.

    "Never upsert" is the constraint the whole poll is written under: this runs every sixty seconds
    and a write here would mint copy rows from a per-user read, which §7.2 makes the admin-key
    sweep's job precisely because item visibility is per-user.
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
    """Is this episode the last one the server knows of? None when that cannot be answered.

    Decision 210(c), and the two alternatives it refused have to be named here because the cheap
    one looks right:

      * arming on the **Series-level `Played` flag** reads a value Jellyfin computes
        (`playedCount >= totalCount`, and true for a folder with no children) — which is the flag
        `seen._folder_played` exists to distrust, and which the sweep would have adopted as `seen`
        before the prompt could ask. §7.3:315 calls that the silent write.
      * **per-episode arming** asks "Did you finish S01E03?", and §4.1 rule 5 gives the app no
        title for an episode, so the answer would have nowhere to land.

    The list comes from the client's cached `GET /Shows/{SeriesId}/Episodes`, read once per series
    rather than once per session row, and its order is the server's own season/episode sort — this
    asks which id is last in it, not which number is highest, because a household's special or
    unnumbered episode is still an episode the server places.

    Undecidable is not "yes": a server that will not list the episodes arms nothing and is reported
    in `WatchReport.undecided`, because the alternative is marking a twenty-hour show seen on one
    episode.
    """
    if client is None:
        # Reachable from a caller that resolves its own sessions (the integration tests do) and
        # from nothing in production: `poll` always has one.
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
    """Turn one `/Sessions` reading into armed prompts.

    A session counts as finished when it has passed the threshold *or* when Jellyfin already
    marks the item played — §7.3 names both the ">= 90%" and the "IsPlayed delta" paths, and a
    viewer who stops at 88% and taps "mark watched" in Jellyfin should still be asked.

    `client` is needed only to decide decision 210(c)'s question for an episode, and is keyword-only
    so that the three-argument calls this function has always taken keep meaning what they did.
    """
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
        if not (playing.played or playing.fraction >= FINISH_THRESHOLD):
            continue
        title_id = await _resolve_session(conn, playing)
        if title_id is None:
            # Always the id that was *playing*, even when the lookup went through `series_id`:
            # this list is what an operator reads to find out what the app could not place, and
            # an episode id is something they can paste into Jellyfin.
            report.unresolved.append(playing.item_id)
            continue
        if playing.series_id:
            # An episode. `series_id` rather than `item_type` because it is the key resolution
            # just used, so the two cannot disagree about what this row is.
            finished = await _series_finished(client, playing)
            if finished is None:
                # `undecided`, not `unresolved`: this session resolved to a title and the question
                # that could not be answered is decision 210(c)'s ("is this the last episode?").
                # See `WatchReport.undecided`.
                report.undecided.append(playing.item_id)
                continue
            if not finished:
                # Mid-series, which is the ordinary case: §7.3's question is about the show, and
                # the show is not finished. Counted in `watching` and nothing else — a fact, not
                # a failure.
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
    """§7.3: "Push notification if the user isn't in the app — best-effort (§6 preamble); when
    undeliverable, the prompt queues and surfaces as an in-app banner on next open."

    The banner path was the whole M1 behaviour and is unchanged: this adds the carrier §7.3
    dates to "the M4 stack". It runs **after** the prompt is armed and its outcome is discarded,
    which is what makes the two independent — a household with no keypair, no subscription or
    no network still has an armed prompt waiting on next open, and a delivery failure is never
    a reason for the prompt not to exist.

    Sent once, when the prompt is armed, rather than on every poll: the poll runs each minute
    and `arm` is idempotent per viewing, so notifying from anywhere else would be a notification
    a minute for as long as the credits run.
    """
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
                # §7.3's own words for the prompt, so the notification and the banner ask the
                # same question.
                "body": f"Did you finish {name}?",
                # The service worker is a renderer and nothing else, so the replacement key and
                # the destination have to arrive with the payload. Both were absent, and its
                # fallbacks are the literal `'spielplan'` and `'/'` — one tag shared with every
                # other notification this app sends, which made a Tonight invitation replace an
                # unread finish prompt on the lock screen. Per title rather than per event, so a
                # second arming for the same show replaces its own card. [M4.11 finding 22]
                "tag": f"finish:{title_id}",
                # Home, which is where `FinishPrompt` is mounted: the notification and the banner
                # are the same question, so the tap has to land where the question is.
                "url": "/",
            },
        )
    except Exception:
        # Best-effort in the strongest sense: §6's preamble guarantees an in-app equivalent for
        # every push-carried prompt, and this one is already queued.
        log.info("finish-prompt push for user %s was not delivered", user_id)


async def poll(conn: asyncpg.Connection, client: JellyfinClient | None = None) -> WatchReport:
    """The 1-minute job. Reads `/Sessions` and arms whatever finished.

    `client` is injectable for the same reason `seen.sync_all`'s is: the integration tests
    drive this against the in-process fake server rather than a mock of the call.
    """
    from spielplan.connectors import registry

    report = WatchReport()
    cfg = await registry.load_jellyfin(conn)
    if not cfg.configured:
        report.skipped_no_link = True
        return report
    # `make_client` and not a direct construction: it is the one site that threads the stored
    # §7.1 version verdict onto the client, and a client built without it cannot refuse the
    # Played write by name — which `answer` below reaches through `seen.set_state`.
    client = client or registry.make_client(cfg)
    try:
        sessions = await client.sessions()
    except JellyfinError as exc:
        _note_unreachable(exc)
        return report
    _note_reachable()
    await observe(conn, sessions, report, client=client)
    return report


async def pending(conn: asyncpg.Connection, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """The queued prompts for one person, newest first.

    Reading them marks them `shown`. §7.3's banner is the undeliverable-push fallback, so
    "has this been surfaced at least once" is a real distinction — it is what M4's push layer
    will check before sending a notification for something already answered on screen.
    """
    # A prompt whose title has since become `seen` is closed rather than shown: the question has
    # been answered, and asking it again would contradict what every other surface already says.
    #
    # The statement is unchanged under decision 211; what changed is who can produce the state it
    # closes on. It used to be the 15-minute sweep adopting Jellyfin's own Played flag, which made
    # this line the mechanism by which a sync "answered" a question the person was never shown
    # (§7.3:315's silent write, counted in §13's denominator as an answer). The sweep no longer
    # adopts for a title whose prompt is open, so what reaches here now is the person's own tap on
    # another surface — the title card — which is the case this was written for.
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
    """The tap, and the only place this module writes `user_title`.

    Both answers close the prompt, and under decision 211 both write: "yes" writes `seen`, "no"
    writes `unseen` as an explicit action. A "no" that wrote nothing left the absence the sweep
    adopts Jellyfin's Played flag into, so the card the person had just dismissed came back as a
    `seen` state fifteen minutes later — "the sweep may not adopt while a prompt is open" only
    means something if the closed prompt leaves a fact behind. Series are not special-cased here:
    `seen.set_state` is where decision 210(a) makes that write app-only with no recursive DELETE.

    The order of the three steps is the whole of finding 12. The prompt used to be consumed first
    and the state written second, on an autocommit connection: a failure in between left
    `prompt_state = 'answered'` with no `user_title` row and nothing that could re-arm it — `observe`
    only arms while a live session is past the threshold, which by then it no longer is — so every
    occurrence subtracted from §13's numerator silently. So the config load (which reads secrets and
    can fail) goes first, where a failure costs nothing; then the state write, which is the durable
    half; then the `UPDATE` that consumes the question. The window that remains is self-healing in
    the direction that matters: `pending()` closes a prompt whose title has become `seen`.

    Deliberately **not** one transaction around the two writes and the push. M4.10 moved the
    Jellyfin round trip out of the verdict transaction for the reason §3.3 states — a foreign
    server's socket must not hold a household's database rows — and wrapping them here would put
    it straight back.
    """
    row = await conn.fetchrow(
        "SELECT title_id FROM playback_event "
        "WHERE id = $1 AND user_id = $2 AND prompt_state = ANY($3::text[])",
        event_id, user_id, list(OPEN_STATES),
    )
    if row is None:
        return {"ok": False, "reason": "no open prompt with that id"}

    from spielplan.connectors import registry
    from spielplan.sync import seen as seen_sync

    # `load_jellyfin` degrades rather than raises on an unreadable DEK (§3.1, M4.7 dd03), and it
    # is called before anything is written so that the failures it does have — a dead pool, a
    # connector row that will not read — cost the person nothing but the tap.
    cfg = await registry.load_jellyfin(conn)
    if client is None:
        client = registry.make_client(cfg)
    sync = await seen_sync.set_state(
        conn, client, cfg,
        user_id=user_id, title_id=row["title_id"], state="seen" if finished else "unseen",
    )
    await conn.execute(
        "UPDATE playback_event SET prompt_state = $2 WHERE id = $1",
        event_id, "answered" if finished else "dismissed",
    )
    return {"ok": True, "title_id": row["title_id"], "seen": finished, "sync": sync}


__all__ = ["WatchReport", "answer", "arm", "observe", "pending", "poll"]
