"""The per-user tier set, and what changing it costs. Spec v2.1 §6.3, §4.2, §5.2; decision 11.

Decision 11, in full: "The tier set is a **per-user preference**. `ledger_cutpoints` already
carries it … what needs stating is the re-initialisation rule. Changing K invalidates that
user's `boundaries`: on save, their cutpoints are re-initialised to the **equal-mass quantiles**
of that user's fitted `s` distribution for the new K (§6.3's measured quantile shape … is
authored for K = 7 and is not defined for any other K), and a Ledger refit is queued for that
user alone — tier *edits* are observations and survive the change, tier *boundaries* do not.
One user changing their tier set never touches another's."

FOUR THINGS, AND EACH ONE IS A SENTENCE OF THAT PARAGRAPH

  1. Equal-mass quantiles of *their own* fitted `s`, not the measured F3/D7/C15/B25/A25/A+17/S8
     shape: that shape is authored for K = 7 and means nothing at any other K. At K = 7 the
     initialisation the *model* uses is still the measured one (`model.initial_cutpoints`) —
     this is a re-initialisation of an existing board, which is a different question, and a
     board that already has an `s` distribution should be cut where that distribution is.
  2. A refit queued for that user alone. Recorded rather than run: §5.3 budgets a full MAP
     refit at "seconds", which is not a thing to do inside a settings save. The re-init itself
     is arithmetic over rows that already exist, so the board is correct when the save returns.
  3. `tier_edit` rows are not touched. They are observations; §4.2 keeps every observation
     table append-only and this is not the exception.
  4. Scoped to one user. `ledger_cutpoints` is keyed `(user_id, kind)`, so this is a property
     of the WHERE clause and the test that proves it needs a second person in the database.

WHAT DECISION 11 DOES NOT SAY, AND THE CHOICES MADE HERE

  * **A relabel is not a change in K.** "Changing K invalidates that user's boundaries" is the
    reason the re-init exists, so renaming F to E at the same size keeps the learned boundaries
    and queues nothing. Discarding a fitted board because somebody preferred a different letter
    would be the rule doing more than it says.
  * **Both kinds get the same set.** Decision 11's own note: "the schema permits a different
    tier set for films and series. That is finer granularity than the decision requires: the
    settings control sets one set per user and writes it to both kind rows."
  * **The bounds.** Two at the minimum, because a one-level set has no boundaries and orders
    nothing. Twelve at the maximum, because every level needs observations before its cutpoint
    is anything but its prior, and past a dozen a tier list is a ranked list wearing letters.
    Neither number is in the spec; both are refusals rather than clamps, so a caller learns.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import asyncpg
import numpy as np

from spielplan.ledger.observations import DEFAULT_TIER_SET, KINDS

log = logging.getLogger("spielplan.rank.tiers")

MIN_TIERS = 2
MAX_TIERS = 12


class TierSetRefused(ValueError):
    """A tier set this app will not store. Refused rather than repaired: a silently corrected
    tier set is one the person did not choose."""


@dataclass
class TierSetReport:
    user_id: int
    tier_set: tuple[str, ...]
    previous: tuple[str, ...] = ()
    k_changed: bool = False
    refit_queued: bool = False
    # Per kind: how the new boundaries were produced. "quantile" when the person had a fitted
    # distribution to cut, "prior" when they had nothing to cut yet — a distinction worth
    # keeping, because the second is not a worse version of the first, it is a different claim.
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
    if len(set(labels)) != len(labels):
        raise TierSetRefused("two tiers cannot share a label — the board would be ambiguous")
    return tuple(labels)


def equal_mass_quantiles(s: np.ndarray, k: int) -> np.ndarray:
    """Decision 11's re-initialisation: the K-1 cuts that put equal mass in each level.

    `np.quantile` with the default linear interpolation, and the result is *not* forced apart:
    a person whose whole board sits on one value gets coincident cutpoints, which is the
    ordered logit's honest answer and a state `model.feasible` explicitly admits (the cone is
    closed). Nudging them apart here would invent a spread the data does not have.
    """
    if s.size < 2:
        raise ValueError("equal-mass quantiles need at least two values")
    return np.quantile(np.asarray(s, dtype=float), np.arange(1, k) / k)


async def tier_set_of(conn: asyncpg.Connection, *, user_id: int) -> tuple[str, ...]:
    """The person's set, or §4.2's default. One set per user; the movie row is the one asked
    because `save` writes both and they cannot disagree."""
    row = await conn.fetchval(
        "SELECT tier_set FROM ledger_cutpoints WHERE user_id = $1 ORDER BY kind LIMIT 1",
        user_id,
    )
    return tuple(row) if row else DEFAULT_TIER_SET


async def _fitted_s(conn: asyncpg.Connection, *, user_id: int, kind: str) -> np.ndarray:
    """The distribution decision 11 cuts. `observed` is exactly "the person has an observation
    on this title", which is the board — quantiles over the whole owned library would be
    quantiles of the model's opinion rather than of theirs."""
    rows = await conn.fetch(
        "SELECT s FROM ledger_state WHERE user_id = $1 AND kind = $2 AND observed",
        user_id,
        kind,
    )
    return np.asarray([float(r["s"]) for r in rows], dtype=float)


async def save_tier_set(
    conn: asyncpg.Connection, *, user_id: int, tier_set: Sequence[str]
) -> TierSetReport:
    """Decision 11's save, in one transaction.

    Returns what changed, because the control has to warn "this discards your learned
    cutpoints and queues a refit" and a warning that cannot say whether it applies is noise.
    """
    labels = validate(tier_set)
    previous = await tier_set_of(conn, user_id=user_id)
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
                # A relabel at the same K. §4.2's CHECK still holds, the boundaries still mean
                # what they meant, and nothing is invalidated.
                boundaries = [float(b) for b in existing["boundaries"]]
                report.initialised[kind] = "kept"
            else:
                s = await _fitted_s(conn, user_id=user_id, kind=kind)
                if s.size >= 2:
                    boundaries = [float(b) for b in equal_mass_quantiles(s, len(labels))]
                    report.initialised[kind] = "quantile"
                else:
                    # Nothing fitted yet, so there is no distribution to cut. §6.3's measured
                    # shape is the prior at K = 7 and equal mass elsewhere — which is exactly
                    # `model.initial_cutpoints`, and reaching for it here keeps one definition
                    # of "where a level starts before anybody has used it".
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

        # Decision 11: "tier edits are observations and survive the change". Counted rather
        # than assumed — the count is what the report says out loud, and the absence of a
        # DELETE in this function is what makes it true.
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


async def refits_owed(conn: asyncpg.Connection) -> list[tuple[int, str]]:
    """The worker's sweep. Ordered oldest first so a queue never starves its own head."""
    rows = await conn.fetch(
        "SELECT user_id, kind FROM ledger_cutpoints WHERE refit_requested_at IS NOT NULL "
        "ORDER BY refit_requested_at, user_id, kind"
    )
    return [(int(r["user_id"]), str(r["kind"])) for r in rows]


async def clear_refit_request(conn: asyncpg.Connection, *, user_id: int, kind: str) -> None:
    await conn.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = NULL "
        "WHERE user_id = $1 AND kind = $2",
        user_id,
        kind,
    )


__all__ = [
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
