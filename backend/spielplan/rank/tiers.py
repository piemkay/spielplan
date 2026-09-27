"""The per-user tier set (decision 11): a change in K re-cuts that user's boundaries at equal-mass
quantiles of their own `s` and queues a refit; a relabel at the same K keeps them. Both kinds share
one set, and `tier_edit` rows always survive.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import asyncpg
import numpy as np

from spielplan.ledger.observations import KINDS, tier_set_of

log = logging.getLogger("spielplan.rank.tiers")

MIN_TIERS = 2
MAX_TIERS = 12
# The label goes into §6.7's rail line, which `rail.record` refuses past 400 characters after a
# drop has committed; refuse long labels here instead.
MAX_LABEL = 24


class TierSetRefused(ValueError):
    """A tier set this app will not store. Refused, never silently corrected."""


@dataclass
class TierSetReport:
    user_id: int
    tier_set: tuple[str, ...]
    previous: tuple[str, ...] = ()
    k_changed: bool = False
    refit_queued: bool = False
    # Per kind: "kept", "quantile" (their fitted s was cut) or "prior" (nothing to cut yet).
    initialised: dict[str, str] = field(default_factory=dict)
    tier_edits_kept: int = 0


def validate(tier_set: Sequence[str]) -> tuple[str, ...]:
    labels = [str(label).strip() for label in tier_set]
    if not (MIN_TIERS <= len(labels) <= MAX_TIERS):
        raise TierSetRefused(
            f"a tier set has between {MIN_TIERS} and {MAX_TIERS} levels, not {len(labels)}"
        )
    if any(not label for label in labels):
        raise TierSetRefused("every tier needs a label")
    if any(len(label) > MAX_LABEL for label in labels):
        raise TierSetRefused(
            f"a tier label is at most {MAX_LABEL} characters - it has to fit on a board and "
            "inside a model-log line"
        )
    if len(set(labels)) != len(labels):
        raise TierSetRefused("two tiers cannot share a label — the board would be ambiguous")
    return tuple(labels)


def equal_mass_quantiles(s: np.ndarray, k: int) -> np.ndarray:
    """Decision 11's re-initialisation: the K-1 equal-mass cuts. Coincident cuts are allowed."""
    if s.size < 2:
        raise ValueError("equal-mass quantiles need at least two values")
    return np.quantile(np.asarray(s, dtype=float), np.arange(1, k) / k)


async def _fitted_s(conn: asyncpg.Connection, *, user_id: int, kind: str) -> np.ndarray:
    """The distribution decision 11 cuts: the rated board's `s`, not the whole library's."""
    rows = await conn.fetch(
        "SELECT s FROM ledger_state WHERE user_id = $1 AND kind = $2 AND observed",
        user_id,
        kind,
    )
    return np.asarray([float(r["s"]) for r in rows], dtype=float)


async def save_tier_set(
    conn: asyncpg.Connection, *, user_id: int, tier_set: Sequence[str]
) -> TierSetReport:
    """Decision 11's save, in one transaction. Returns what changed, for the control's warning."""
    labels = validate(tier_set)
    # Both kind rows hold one set (decision 11), so either answers "what did they have before".
    previous = await tier_set_of(conn, user_id=user_id, kind=KINDS[0])
    report = TierSetReport(user_id=user_id, tier_set=labels, previous=previous)
    report.k_changed = len(labels) != len(previous)

    async with conn.transaction():
        for kind in KINDS:
            existing = await conn.fetchrow(
                "SELECT boundaries, tier_set FROM ledger_cutpoints "
                "WHERE user_id = $1 AND kind = $2",
                user_id,
                kind,
            )
            keep = (
                existing is not None
                and len(existing["tier_set"]) == len(labels)
                and len(existing["boundaries"]) == len(labels) - 1
            )
            if keep:
                # A relabel at the same K invalidates nothing.
                boundaries = [float(b) for b in existing["boundaries"]]
                report.initialised[kind] = "kept"
            else:
                s = await _fitted_s(conn, user_id=user_id, kind=kind)
                if s.size >= 2:
                    boundaries = [float(b) for b in equal_mass_quantiles(s, len(labels))]
                    report.initialised[kind] = "quantile"
                else:
                    # Nothing fitted yet: the model's own prior.
                    from spielplan.ledger import model

                    boundaries = [float(b) for b in model.initial_cutpoints(len(labels))]
                    report.initialised[kind] = "prior"

            await conn.execute(
                """
                INSERT INTO ledger_cutpoints
                    (user_id, kind, boundaries, tier_set, refit_requested_at, updated_at)
                VALUES ($1, $2, $3::float8[], $4::text[], CASE WHEN $5 THEN now() END, now())
                ON CONFLICT (user_id, kind) DO UPDATE SET
                    boundaries = EXCLUDED.boundaries,
                    tier_set = EXCLUDED.tier_set,
                    refit_requested_at = COALESCE(
                        EXCLUDED.refit_requested_at, ledger_cutpoints.refit_requested_at
                    ),
                    updated_at = now()
                """,
                user_id,
                kind,
                boundaries,
                list(labels),
                report.k_changed,
            )

        # Decision 11: tier edits survive the change.
        report.tier_edits_kept = int(
            await conn.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id)
        )

    report.refit_queued = report.k_changed
    log.info(
        "tier set for user %s: %s -> %s (%s)",
        user_id,
        "/".join(previous),
        "/".join(labels),
        "refit queued" if report.refit_queued else "boundaries kept",
    )
    return report


async def refits_owed(conn: asyncpg.Connection) -> list[tuple[int, str, datetime]]:
    """The worker's sweep: `(user_id, kind, refit_requested_at)`, oldest first.

    The stamp travels so `clear_refit_request` keeps a request made during the fit.
    """
    rows = await conn.fetch(
        "SELECT user_id, kind, refit_requested_at FROM ledger_cutpoints "
        "WHERE refit_requested_at IS NOT NULL ORDER BY refit_requested_at, user_id, kind"
    )
    return [(int(r["user_id"]), str(r["kind"]), r["refit_requested_at"]) for r in rows]


async def clear_refit_request(
    conn: asyncpg.Connection, *, user_id: int, kind: str, requested_at: datetime
) -> None:
    """Clear the request the sweep serviced (stamp from `refits_owed`); a newer one survives."""
    await conn.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = NULL "
        "WHERE user_id = $1 AND kind = $2 AND refit_requested_at <= $3",
        user_id,
        kind,
        requested_at,
    )


__all__ = [
    "MAX_LABEL",
    "MAX_TIERS",
    "MIN_TIERS",
    "TierSetRefused",
    "TierSetReport",
    "clear_refit_request",
    "equal_mass_quantiles",
    "refits_owed",
    "save_tier_set",
    "tier_set_of",
    "validate",
]
