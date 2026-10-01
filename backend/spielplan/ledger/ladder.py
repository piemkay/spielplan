"""The ladder's data layer: a member's cut-over (decision 537), the one placement writer, and the
reads every ladder surface shares.

A placement is a `tier_edit` that implies `seen`, plus the verdict its tier stands for wherever the
live one is none or another class. Surfaces gate on the set-up; `place` does not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from spielplan.ledger import model, observations, refit

# Advisory-lock namespace, unique across features (6202 tonight finish, 6303 queue answer): one
# member's drops, placements and set-up serialise on it.
LOCK = 6304


class NotSetUp(Exception):
    """A surface that waits for the set-up refuses with this."""


class AlreadySetUp(Exception):
    """The set-up is finished once; there is no redo."""


class SetupRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason   # "empty" | "duplicate" | "not_a_film" | "bad_tier"


@dataclass(frozen=True)
class LadderState:
    finished_at: datetime | None
    earlier_ratings: int
    rated_before: int

    @property
    def done(self) -> bool:
        return self.finished_at is not None


@dataclass(frozen=True)
class Placement:
    edit: observations.Write
    verdict: observations.Write | None
    kind: str
    tier: int
    label: str
    word: str
    log: str

    @property
    def tier_edit_id(self) -> int:
        return int(self.edit.row_id)

    @property
    def verdict_id(self) -> int | None:
        return None if self.verdict is None else int(self.verdict.row_id)

    @property
    def superseded_verdict_id(self) -> int | None:
        return None if self.verdict is None else self.verdict.superseded_id

    @property
    def implied_seen(self) -> bool:
        return self.edit.implied_seen

    @property
    def prior_state(self) -> tuple[observations.PriorState, ...]:
        return self.edit.prior_state


@dataclass(frozen=True)
class SetupResult:
    finished_at: datetime
    placed: tuple[Placement, ...]
    earlier_ratings: int
    rated_before: int


async def set_up_at(conn: asyncpg.Connection, *, user_id: int) -> datetime | None:
    return await conn.fetchval("SELECT finished_at FROM ladder_setup WHERE user_id = $1", user_id)


async def require_set_up(conn: asyncpg.Connection, *, user_id: int) -> datetime:
    finished_at = await set_up_at(conn, user_id=user_id)
    if finished_at is None:
        raise NotSetUp(f"user {user_id} has not set up their ladder")
    return finished_at


async def _earlier_ratings(conn: asyncpg.Connection, *, user_id: int) -> int:
    """Titles answered by verdict or tier_edit before the cut-over; before the set-up, every one."""
    return int(
        await conn.fetchval(
            """
            SELECT count(DISTINCT a.title_id) FROM (
                SELECT title_id, created_at FROM verdict WHERE user_id = $1
                UNION ALL
                SELECT title_id, created_at FROM tier_edit WHERE user_id = $1
            ) a
            WHERE a.created_at < COALESCE(
                (SELECT finished_at FROM ladder_setup WHERE user_id = $1), 'infinity'::timestamptz)
            """,
            user_id,
        )
        or 0
    )


async def state(conn: asyncpg.Connection, *, user_id: int) -> LadderState:
    return LadderState(
        finished_at=await set_up_at(conn, user_id=user_id),
        earlier_ratings=await _earlier_ratings(conn, user_id=user_id),
        rated_before=len(await rated_before(conn, user_id=user_id, kinds=observations.KINDS)),
    )


async def place(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    tier: int,
    via: str = "explicit",
    undoes: int | None = None,
    reask_of: int | None = None,
    source: str = "ladder",
) -> Placement:
    """One placement: the `tier_edit` and, where the live verdict is none or another class, the
    verdict of the tier's class (decision 508), in one transaction under the member's lock.
    Raises ValueError for a tier outside the set. Neither checks the set-up nor refits.
    """
    kind = await observations.kind_of(conn, title_id)
    tier_set = await observations.tier_set_of(conn, user_id=user_id, kind=kind)
    if not 0 <= tier < len(tier_set):
        raise ValueError(f"tier {tier} is outside the configured set {tier_set}")
    cls = model.verdict_class_of_tier(tier, len(tier_set))

    verdict: observations.Write | None = None
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", LOCK, user_id)
        # First, so the edit carries the seen state it implied and the verdict implies nothing more.
        edit = await observations.record_tier_edit(
            conn, user_id=user_id, title_id=title_id, tier=tier, via=via, undoes=undoes,
            reask_of=reask_of,
        )
        live = await conn.fetchval(
            f"SELECT l.value FROM ({observations.LIVE_LABEL_SQL}) l WHERE l.title_id = $2",
            user_id,
            title_id,
        )
        if live is None or int(live) != cls:
            verdict = await observations.record_verdict(
                conn, user_id=user_id, title_id=title_id, value=cls, source=source
            )

    line = edit.log
    if verdict is not None:
        line += f" + verdict = {observations.VERDICT_LABELS[cls]}"
    return Placement(
        edit=edit,
        verdict=verdict,
        kind=kind,
        tier=tier,
        label=tier_set[tier],
        word=observations.tier_words(tier_set)[tier],
        log=line,
    )


async def undo_placement(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    tier_edit_id: int,
    verdict_id: int | None,
    prior_state: Sequence[observations.PriorState],
    block_started_at: datetime | None = None,
) -> observations.Undo:
    """Decision 35 for a placement: both rows go (the verdict's supersede chain spliced) and the
    prior seen state comes back, in one transaction. Raises observations.UndoRefused."""
    async with conn.transaction():
        unsuperseded: tuple[int, ...] = ()
        if verdict_id is not None:
            taken = await observations.undo(
                conn, user_id=user_id, arm="verdict", row_id=verdict_id,
                block_started_at=block_started_at,
            )
            unsuperseded = taken.unsuperseded
        title_id = await conn.fetchval("SELECT title_id FROM tier_edit WHERE id = $1", tier_edit_id)
        edit = await observations.undo(
            conn, user_id=user_id, arm="tier_edit", row_id=tier_edit_id,
            title_ids=() if title_id is None else (int(title_id),),
            prior_state=prior_state, block_started_at=block_started_at,
        )
    removed = 1 if verdict_id is None else 2
    return observations.Undo(
        arm=edit.arm,
        row_id=edit.row_id,
        user_id=user_id,
        kind=edit.kind,
        title_ids=edit.title_ids,
        unsuperseded=unsuperseded,
        restored=edit.restored,
        log=(
            f"undo: placement {tier_edit_id} retracted -> {removed} observation(s) removed, "
            f"{len(edit.restored)} state(s) restored"
        ),
    )


async def placements(conn: asyncpg.Connection, *, user_id: int, kind: str) -> dict[int, int]:
    """title_id -> step in today's K, from the latest tier_edit since the cut-over. Seen state is not
    required: a placed film marked Not seen keeps its step (decision 550)."""
    k = len(await observations.tier_set_of(conn, user_id=user_id, kind=kind))
    rows = await conn.fetch(
        f"""
        SELECT e.title_id, e.tier, e.n_levels
          FROM ({observations.latest_tier_edit_sql()}) e
          JOIN title t ON t.id = e.title_id
         WHERE t.kind = $2
        """,
        user_id,
        kind,
    )
    return {
        int(r["title_id"]): observations.rescale_level(int(r["tier"]), k_from=r["n_levels"], k_to=k)
        for r in rows
    }


async def rated_before(
    conn: asyncpg.Connection, *, user_id: int, kinds: Sequence[str]
) -> list[int]:
    """Seen titles of these kinds answered before the cut-over (a non-re-ask verdict or a tier_edit)
    and not placed since; newest old answer first. [] before the set-up."""
    rows = await conn.fetch(
        """
        WITH cut AS (SELECT finished_at AS at FROM ladder_setup WHERE user_id = $1),
        old AS (
            SELECT title_id, created_at FROM verdict WHERE user_id = $1 AND NOT is_reask
            UNION ALL
            SELECT title_id, created_at FROM tier_edit WHERE user_id = $1
        )
        SELECT o.title_id, max(o.created_at) AS newest
          FROM old o
          JOIN cut ON o.created_at < cut.at
          JOIN title t ON t.id = o.title_id AND t.kind = ANY($2::text[])
          JOIN user_title ut ON ut.user_id = $1 AND ut.title_id = o.title_id AND ut.state = 'seen'
         WHERE NOT EXISTS (
               SELECT 1 FROM tier_edit e
                WHERE e.user_id = $1 AND e.title_id = o.title_id AND e.created_at >= cut.at)
         GROUP BY o.title_id
         ORDER BY newest DESC, o.title_id DESC
        """,
        user_id,
        list(kinds),
    )
    return [int(r["title_id"]) for r in rows]


async def finish_setup(
    conn: asyncpg.Connection, *, user_id: int, picks: Sequence[tuple[int, int]]
) -> SetupResult:
    """The cut-over (decision 537): each (title_id, tier) placed with `source = 'setup'`, the member's
    fit dropped and both kinds' refits queued, in one transaction. Writes nothing to Jellyfin itself:
    an unseen pick becomes seen and the sync pushes it (§7.3)."""
    chosen = [(int(title_id), int(tier)) for title_id, tier in picks]
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", LOCK, user_id)
        if await set_up_at(conn, user_id=user_id) is not None:
            raise AlreadySetUp(f"user {user_id} has already set up their ladder")
        if not chosen:
            raise SetupRefused("empty")
        ids = [title_id for title_id, _ in chosen]
        if len(set(ids)) != len(ids):
            raise SetupRefused("duplicate")
        films = await conn.fetchval(
            "SELECT count(*) FROM title WHERE id = ANY($1::int[]) AND kind = 'movie'", ids
        )
        if films != len(ids):
            raise SetupRefused("not_a_film")
        k = len(await observations.tier_set_of(conn, user_id=user_id, kind="movie"))
        if any(not 0 <= tier < k for _, tier in chosen):
            raise SetupRefused("bad_tier")

        finished_at = await conn.fetchval(
            "INSERT INTO ladder_setup (user_id) VALUES ($1) RETURNING finished_at", user_id
        )
        placed = []
        for title_id, tier in chosen:
            placed.append(
                await place(
                    conn, user_id=user_id, title_id=title_id, tier=tier, via="explicit",
                    source="setup",
                )
            )
        # The boards were fitted to the history; the sweep refits them from the set-up alone.
        await conn.execute("DELETE FROM ledger_fit WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM ledger_state WHERE user_id = $1", user_id)
        for kind in observations.KINDS:
            await refit._queue_full_refit(conn, user_id=user_id, kind=kind)
        after = await state(conn, user_id=user_id)
    return SetupResult(
        finished_at=finished_at,
        placed=tuple(placed),
        earlier_ratings=after.earlier_ratings,
        rated_before=after.rated_before,
    )


__all__ = [
    "LOCK",
    "AlreadySetUp",
    "LadderState",
    "NotSetUp",
    "Placement",
    "SetupRefused",
    "SetupResult",
    "finish_setup",
    "place",
    "placements",
    "rated_before",
    "require_set_up",
    "set_up_at",
    "state",
    "undo_placement",
]
