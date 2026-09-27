"""What the Ledger fit sees (`load_observations`), and the append-only ways a person writes to it.

Reads keep superseded verdicts (§5.2's rewatch arm), drop §13's held-out duels, and are per kind.
Only `undo` may DELETE a verdict or duel (decision 35). The embedding source is injected.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import asyncpg
import numpy as np

from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.model import (
    EMBED_DIM,
    MEASURED_TIER_SHARES,
    OUT_A,
    OUT_B,
    OUT_TIE,
    ObservationSet,
)
from spielplan.scoring import backbone as scoring_backbone

log = logging.getLogger("spielplan.ledger.observations")

Kind = Literal["movie", "series"]
KINDS: tuple[str, ...] = ("movie", "series")

# §4.2's default, repeated in `ledger_cutpoints.tier_set`'s DDL. Its size is K.
DEFAULT_TIER_SET: tuple[str, ...] = ("F", "D", "C", "B", "A", "A+", "S")

# §4.2: verdict values 0 / 1 / 2.
VERDICT_LABELS: tuple[str, ...] = ("disliked", "fine", "liked")

OUTCOMES = {"A": OUT_A, "B": OUT_B, "TIE": OUT_TIE}

# §13 stream (a). Held out from the fit; still written, still read by the evaluation.
HELD_OUT = "uniform_holdout"

# The arms the fit's ordinal block carries. `model.ObservationSet.ord_arm` numbers them.
ARM_VERDICT, ARM_TIER = 0, 1


# --- the embedding seam ---------------------------------------------------------------------

# (title_ids) -> (float64[n, 64], bool[n]). May be sync or async.
EmbeddingSource = Callable[
    [Sequence[int]],
    "tuple[np.ndarray, np.ndarray] | Awaitable[tuple[np.ndarray, np.ndarray]]",
]


def zero_embeddings(title_ids: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    """§3.1's bundle-less mode: e = 0, so s = μ + r still ranks everything the person has rated."""
    n = len(title_ids)
    return np.zeros((n, EMBED_DIM)), np.zeros(n, dtype=bool)


async def _placement_pairs(
    conn: asyncpg.Connection, title_ids: Sequence[int], *, bundle_version: str | None
) -> dict[int, tuple[np.ndarray, float]]:
    """The Cold Tower's (ê, b̂) for these ids, in one basis — `serve.placements`' shape."""
    ids = [int(t) for t in title_ids]
    if not ids:
        return {}
    # `$2 IS NULL` falls back to the active bundle. Callers that know the version must pass it:
    # during §10's pre-flip rebuild the active row is the OUTGOING bundle.
    found = await conn.fetch(
        """
        SELECT p.title_id, p.e_hat, p.b_hat
        FROM title_placement p
        JOIN artifact_bundle b ON b.version = p.bundle_version
        WHERE p.title_id = ANY($1::int[])
          AND ($2::text IS NULL OR p.bundle_version = $2)
          AND ($2::text IS NOT NULL OR b.state = 'active')
        """,
        ids,
        bundle_version,
    )
    return {
        int(r["title_id"]): (scoring_backbone.unpack_vec(r["e_hat"]), float(r["b_hat"]))
        for r in found
    }


def latest_tier_edit_sql(user: str = "$1") -> str:
    """The person's latest drop per title (`title_id, tier, n_levels`); `user` is their placeholder.

    One pass over their own `tier_edit` rows: a correlated subquery would re-run per board row.
    """
    return f"""
    SELECT DISTINCT ON (title_id) title_id, tier, n_levels
      FROM tier_edit
     WHERE user_id = {user}
     ORDER BY title_id, created_at DESC, id DESC
"""


# The person's CURRENT label on each title, `$1` = user id. Newest non-re-ask row, NOT
# `superseded_by IS NULL`: a re-ask supersedes the original, so that predicate would drop the title.
LIVE_LABEL_SQL = """
    SELECT DISTINCT ON (v.title_id) v.title_id, v.value, v.id AS verdict_id, v.created_at
      FROM verdict v
     WHERE v.user_id = $1 AND NOT v.is_reask
     ORDER BY v.title_id, v.created_at DESC, v.id DESC
"""


def standard_embeddings(
    conn: Any, backbone: Any = None, *, bundle_version: str | None = None
) -> EmbeddingSource:
    """§5.1's coordinate for every title, read as its direction (decision 471).

    The same `backbone.coordinate` blend serving uses, so the fit and serving read one coordinate.
    `backbone` and `bundle_version` must describe the same bundle. The import of `scoring` does not
    cycle only because `ledger/hyperparams.py` imports nothing first-party.
    """
    # An empty basis makes `coordinate` take its own gate-0 limit; no branch needed.
    basis = (
        backbone
        if backbone is not None and not getattr(backbone, "is_empty", True)
        else scoring_backbone.Backbone.empty()
    )

    async def rows(title_ids: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        ids = [int(t) for t in title_ids]
        matrix = np.zeros((len(ids), EMBED_DIM))
        embedded = np.zeros(len(ids), dtype=bool)
        if not ids:
            return matrix, embedded
        placed = await _placement_pairs(conn, ids, bundle_version=bundle_version)
        for i, title_id in enumerate(ids):
            coordinate = scoring_backbone.coordinate(title_id, basis, placed.get(title_id))
            if coordinate is not None:
                matrix[i] = scoring_backbone.direction(coordinate)
                embedded[i] = True
        return matrix, embedded

    return rows


async def resolve_embeddings(
    embeddings: EmbeddingSource, title_ids: Sequence[int]
) -> tuple[np.ndarray, np.ndarray]:
    """Call the injected source and check its shape here: a wrong width converges, wrongly."""
    ids = list(title_ids)
    result: Any = embeddings(ids)
    if inspect.isawaitable(result):
        result = await result
    matrix, embedded = result
    matrix = np.asarray(matrix, dtype=float)
    embedded = np.asarray(embedded, dtype=bool)
    if matrix.shape != (len(ids), EMBED_DIM):
        raise ValueError(
            f"embedding source returned {matrix.shape}, expected {(len(ids), EMBED_DIM)}"
        )
    if embedded.shape != (len(ids),):
        raise ValueError(f"embedding mask has shape {embedded.shape}, expected {(len(ids),)}")
    # A non-finite coordinate poisons every title through v. Treat it as absent, loudly.
    bad = embedded & ~np.isfinite(matrix).all(axis=1)
    if bad.any():
        log.warning("dropping %d non-finite embedding row(s) from the fit", int(bad.sum()))
        matrix[bad] = 0.0
        embedded[bad] = False
    return matrix, embedded


# --- what the fit sees ------------------------------------------------------------------------


@dataclass(frozen=True)
class Observations:
    """`obs` is what `model.fit` takes; the rest (incl. the clock `model` may not have) is the writer's."""

    obs: ObservationSet
    user_id: int
    kind: str
    tier_set: tuple[str, ...]
    # Per row of `obs.title_ids`: the most recent observation of any arm for that title.
    last_observed_at: tuple[datetime | None, ...] = ()
    n_verdicts: int = 0
    n_tier_edits: int = 0
    n_duels: int = 0
    n_held_out: int = 0
    n_reask: int = 0

    @property
    def title_ids(self) -> np.ndarray:
        return self.obs.title_ids


async def tier_set_of(conn: asyncpg.Connection, *, user_id: int, kind: str) -> tuple[str, ...]:
    """The configured tier set for (user, kind), or §4.2's default."""
    configured = await conn.fetchval(
        "SELECT tier_set FROM ledger_cutpoints WHERE user_id = $1 AND kind = $2", user_id, kind
    )
    return tuple(configured) if configured else DEFAULT_TIER_SET


# --- decision 11: a tier level outlives the tier set it was written in -----------------------


def _tier_shares(k: int) -> np.ndarray:
    """§6.3's measured level shares at K = 7, equal mass at any other K (as `model.initial_cutpoints`)."""
    shares = MEASURED_TIER_SHARES if k == len(MEASURED_TIER_SHARES) else (1.0 / k,) * k
    return np.asarray(shares, dtype=float)


def rescale_level(level: int, *, k_from: int | None, k_to: int) -> int:
    """Decision 11: a tier level written under `k_from` levels, re-read under `k_to`.

    Maps the band's midpoint by cumulative prior mass, then clamps. `k_from is None` (unknown
    board) reads the level as written.
    """
    if k_to < 1:
        raise ValueError(f"a tier set has at least one level, not {k_to}")
    if k_from is None or int(k_from) < 2 or int(k_from) == int(k_to):
        return min(max(int(level), 0), k_to - 1)
    cum_from = np.cumsum(_tier_shares(int(k_from)))
    cum_to = np.cumsum(_tier_shares(int(k_to)))
    band = min(max(int(level), 0), int(k_from) - 1)
    low = 0.0 if band == 0 else float(cum_from[band - 1])
    mass = 0.5 * (low + float(cum_from[band]))
    # Bands are half-open [low, high), so a midpoint on a boundary belongs to the band above.
    mapped = int(np.searchsorted(cum_to, mass, side="right"))
    return min(max(mapped, 0), k_to - 1)


async def load_observations(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource = zero_embeddings,
) -> Observations:
    """Every observation the fit is allowed to see, for one (user, kind)."""
    _check_kind(kind)
    tier_set = await tier_set_of(conn, user_id=user_id, kind=kind)

    # Deliberately NO `superseded_by IS NULL`: superseded verdicts are §5.2's rewatch arm.
    # Re-asks (§13 stream b) are excluded by `is_reask`, not the nullable `reask_of`.
    verdicts = await conn.fetch(
        """
        SELECT v.title_id, v.value, v.created_at
        FROM verdict v
        JOIN title t ON t.id = v.title_id
        WHERE v.user_id = $1 AND t.kind = $2 AND NOT v.is_reask
        ORDER BY v.id
        """,
        user_id,
        kind,
    )

    tier_edits = await conn.fetch(
        """
        SELECT e.title_id, e.tier, e.n_levels, e.created_at
        FROM tier_edit e
        JOIN title t ON t.id = e.title_id
        WHERE e.user_id = $1 AND t.kind = $2
        ORDER BY e.id
        """,
        user_id,
        kind,
    )

    # Both sides of this kind (§4.1 rule 5), though `record_duel` already refuses cross-kind duels.
    duels = await conn.fetch(
        """
        SELECT d.title_a, d.title_b, d.outcome, d.margin, d.created_at
        FROM duel d
        JOIN title ta ON ta.id = d.title_a
        JOIN title tb ON tb.id = d.title_b
        WHERE d.user_id = $1 AND ta.kind = $2 AND tb.kind = $2
          AND d.selection <> $3 AND NOT d.is_reask
        ORDER BY d.id
        """,
        user_id,
        kind,
        HELD_OUT,
    )
    excluded = await conn.fetchrow(
        """
        SELECT
          count(*) FILTER (WHERE d.selection = $3)                       AS held_out,
          count(*) FILTER (WHERE d.is_reask AND d.selection <> $3)       AS reask
        FROM duel d
        JOIN title ta ON ta.id = d.title_a
        WHERE d.user_id = $1 AND ta.kind = $2
        """,
        user_id,
        kind,
        HELD_OUT,
    )
    reask_verdicts = await conn.fetchval(
        """
        SELECT count(*) FROM verdict v JOIN title t ON t.id = v.title_id
        WHERE v.user_id = $1 AND t.kind = $2 AND v.is_reask
        """,
        user_id,
        kind,
    )

    ids = sorted(
        {r["title_id"] for r in verdicts}
        | {r["title_id"] for r in tier_edits}
        | {r["title_a"] for r in duels}
        | {r["title_b"] for r in duels}
    )
    position = {tid: i for i, tid in enumerate(ids)}
    n = len(ids)
    n_levels = len(tier_set)

    ord_index: list[int] = []
    ord_level: list[int] = []
    ord_arm: list[int] = []
    touched: list[datetime | None] = [None] * n

    def _touch(title_id: int, at: datetime) -> None:
        i = position[title_id]
        if touched[i] is None or at > touched[i]:
            touched[i] = at

    for row in verdicts:
        ord_index.append(position[row["title_id"]])
        ord_level.append(int(row["value"]))
        ord_arm.append(ARM_VERDICT)
        _touch(row["title_id"], row["created_at"])

    rescaled = 0
    for row in tier_edits:
        written = int(row["tier"])
        level = rescale_level(written, k_from=row["n_levels"], k_to=n_levels)
        if level != written:
            rescaled += 1
        ord_index.append(position[row["title_id"]])
        ord_level.append(level)
        ord_arm.append(ARM_TIER)
        _touch(row["title_id"], row["created_at"])
    if rescaled:
        log.warning(
            "rescaled %d tier edit(s) into the %d-level set they are now read against",
            rescaled,
            n_levels,
        )

    duel_a: list[int] = []
    duel_b: list[int] = []
    duel_outcome: list[int] = []
    duel_margin: list[float] = []
    for row in duels:
        duel_a.append(position[row["title_a"]])
        duel_b.append(position[row["title_b"]])
        duel_outcome.append(OUTCOMES[row["outcome"]])
        # A margin-less row (§6.3's drop neighbours) is an ordinary, hesitant comparison.
        margin = row["margin"]
        duel_margin.append(hp.margin_hesitant if margin is None else float(margin))
        _touch(row["title_a"], row["created_at"])
        _touch(row["title_b"], row["created_at"])

    matrix, embedded = await resolve_embeddings(embeddings, ids)
    obs = ObservationSet(
        title_ids=np.asarray(ids, dtype=np.int64),
        embeddings=matrix,
        embedded=embedded,
        ord_index=np.asarray(ord_index, dtype=np.int64),
        ord_level=np.asarray(ord_level, dtype=np.int64),
        ord_arm=np.asarray(ord_arm, dtype=np.int64),
        # No decay: §5.2's freshness rule acts on σ, not on the likelihood.
        ord_weight=np.ones(len(ord_index)),
        duel_a=np.asarray(duel_a, dtype=np.int64),
        duel_b=np.asarray(duel_b, dtype=np.int64),
        duel_outcome=np.asarray(duel_outcome, dtype=np.int64),
        duel_margin=np.asarray(duel_margin, dtype=float),
        n_levels=n_levels,
    )
    return Observations(
        obs=obs,
        user_id=user_id,
        kind=kind,
        tier_set=tier_set,
        last_observed_at=tuple(touched),
        n_verdicts=len(verdicts),
        n_tier_edits=len(tier_edits),
        n_duels=len(duels),
        n_held_out=int(excluded["held_out"] or 0),
        n_reask=int(excluded["reask"] or 0) + int(reask_verdicts or 0),
    )


# --- the write path ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorState:
    """What `user_title` held before an observation changed it, `jf_synced_at` included (§7.3)."""

    title_id: int
    existed: bool
    state: str | None = None
    jf_synced_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title_id": self.title_id,
            "existed": self.existed,
            "state": self.state,
            "jf_synced_at": self.jf_synced_at.isoformat() if self.jf_synced_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PriorState:
        stamp = raw.get("jf_synced_at")
        return cls(
            title_id=int(raw["title_id"]),
            existed=bool(raw["existed"]),
            state=raw.get("state"),
            jf_synced_at=datetime.fromisoformat(stamp) if stamp else None,
        )


@dataclass(frozen=True)
class Write:
    """One append-only observation, and everything decision 35's journal needs to undo it."""

    arm: Literal["verdict", "duel", "tier_edit", "not_seen"]
    # None for `not_seen`, which changes state and writes no observation row.
    row_id: int | None
    user_id: int
    kind: str
    title_ids: tuple[int, ...]
    # The row this write stamped superseded, so undo can un-stamp exactly that one.
    superseded_id: int | None = None
    implied_seen: bool = False
    prior_state: tuple[PriorState, ...] = ()
    # §6.7's model-log line.
    log: str = ""

    def prior_state_json(self) -> list[dict[str, Any]]:
        return [p.as_dict() for p in self.prior_state]


async def _capture_prior(
    conn: asyncpg.Connection, *, user_id: int, title_id: int
) -> PriorState:
    row = await conn.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = $2",
        user_id,
        title_id,
    )
    if row is None:
        return PriorState(title_id=title_id, existed=False)
    return PriorState(
        title_id=title_id, existed=True, state=row["state"], jf_synced_at=row["jf_synced_at"]
    )


async def _set_state(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, state: str
) -> None:
    """The app-side half of §7.3's write. `jf_synced_at = NULL` marks the Jellyfin push as owed."""
    await conn.execute(
        """
        INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at)
        VALUES ($1, $2, $3, now(), NULL)
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET state = EXCLUDED.state, state_changed_at = now(), jf_synced_at = NULL
        """,
        user_id,
        title_id,
        state,
    )


async def kind_of(conn: asyncpg.Connection, title_id: int) -> str:
    kind = await conn.fetchval("SELECT kind FROM title WHERE id = $1", title_id)
    if kind is None:
        raise LookupError(f"no title {title_id}")
    return str(kind)


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")


async def record_verdict(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    value: int,
    source: str = "sweep",
    is_reask: bool = False,
    reask_of: int | None = None,
) -> Write:
    """§5.2 arm 1, and §6.1's "Verdict implies `seen`". Insert, then stamp the previous row."""
    if value not in (0, 1, 2):
        raise ValueError(f"verdict value must be 0, 1 or 2, not {value!r}")
    kind = await kind_of(conn, title_id)

    async with conn.transaction():
        prior = await _capture_prior(conn, user_id=user_id, title_id=title_id)
        row_id = await conn.fetchval(
            """
            INSERT INTO verdict (user_id, title_id, value, source, is_reask, reask_of)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """,
            user_id,
            title_id,
            value,
            source,
            is_reask,
            reask_of,
        )
        # A re-ask supersedes too: it is the latest answer, though the fit excludes it.
        superseded = await conn.fetch(
            """
            UPDATE verdict SET superseded_by = $1
            WHERE user_id = $2 AND title_id = $3 AND id <> $1 AND superseded_by IS NULL
            RETURNING id
            """,
            row_id,
            user_id,
            title_id,
        )
        implied_seen = prior.state != "seen"
        await _set_state(conn, user_id=user_id, title_id=title_id, state="seen")

    if len(superseded) > 1:
        log.warning(
            "title %d had %d live verdicts for user %d; all superseded by %d",
            title_id,
            len(superseded),
            user_id,
            row_id,
        )
    tail = " · implies seen" if implied_seen else ""
    return Write(
        arm="verdict",
        row_id=int(row_id),
        user_id=user_id,
        kind=kind,
        title_ids=(title_id,),
        superseded_id=int(superseded[0]["id"]) if superseded else None,
        implied_seen=implied_seen,
        prior_state=(prior,),
        log=(
            f"verdict(title {title_id}) = {VERDICT_LABELS[value]} -> ordered-logit arm"
            + (" · re-ask (§13 stream b), held out of the fit" if is_reask else "")
            + tail
        ),
    )


async def record_duel(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_a: int,
    title_b: int,
    outcome: str,
    context: str,
    selection: str = "random",
    decisive: bool | None = None,
    hp: Hyperparams | None = None,
    margin: float | None = None,
    is_reask: bool = False,
    reask_of: int | None = None,
) -> Write:
    """§5.2 arm 2. Writes the RAW margin, not a weight; `decisive` needs `hp` for the number."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, not {outcome!r}")
    if title_a == title_b:
        raise ValueError("a duel needs two different titles")
    if decisive is not None:
        if hp is None:
            raise ValueError("record_duel(decisive=...) needs hp — §4.3 owns 1.6 and 1.0")
        margin = hp.margin_for(decisive)

    kind_a, kind_b = await kind_of(conn, title_a), await kind_of(conn, title_b)
    if kind_a != kind_b:
        raise ValueError(
            f"cross-kind duel refused: title {title_a} is a {kind_a} and {title_b} a {kind_b} "
            "(§4.1 rule 5 partitions every ranking surface by kind)"
        )

    row_id = await conn.fetchval(
        """
        INSERT INTO duel (user_id, title_a, title_b, outcome, margin, context, selection,
                          is_reask, reask_of)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
        """,
        user_id,
        title_a,
        title_b,
        outcome,
        margin,
        context,
        selection,
        is_reask,
        reask_of,
    )
    if selection == HELD_OUT:
        weighting = "uniform-random, held out"
    elif margin is None:
        weighting = "margin-less"
    else:
        weighting = f"margin {margin:g}"
    return Write(
        arm="duel",
        row_id=int(row_id),
        user_id=user_id,
        kind=kind_a,
        title_ids=(title_a, title_b),
        log=(
            f"duel(title {title_a} vs {title_b}) = {outcome} -> Davidson arm, "
            f"{context}/{selection} · {weighting}"
        ),
    )


async def record_tier_edit(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, tier: int, via: str = "drag_drop"
) -> Write:
    """§5.2 arm 3. No implied `seen`: §6.1 says that only of a verdict."""
    if via not in ("drag_drop", "explicit"):
        raise ValueError(f"via must be 'drag_drop' or 'explicit', not {via!r}")
    kind = await kind_of(conn, title_id)
    tier_set = await tier_set_of(conn, user_id=user_id, kind=kind)
    if not 0 <= tier < len(tier_set):
        raise ValueError(f"tier {tier} is outside the configured set {tier_set}")

    # `n_levels` records the K this index means (decision 11); no tier-set history exists.
    row_id = await conn.fetchval(
        "INSERT INTO tier_edit (user_id, title_id, tier, via, n_levels) "
        "VALUES ($1,$2,$3,$4,$5) RETURNING id",
        user_id,
        title_id,
        tier,
        via,
        len(tier_set),
    )
    return Write(
        arm="tier_edit",
        row_id=int(row_id),
        user_id=user_id,
        kind=kind,
        title_ids=(title_id,),
        log=(
            f"tier_edit(title {title_id} -> {tier_set[tier]}, via={via}) "
            "-> K-level ordered logit"
        ),
    )


async def record_not_seen(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> Write:
    """§6.1's `Not seen`: a state change only; the title's ratings stay in the likelihood (§4.2)."""
    kind = await kind_of(conn, title_id)
    async with conn.transaction():
        prior = await _capture_prior(conn, user_id=user_id, title_id=title_id)
        await _set_state(conn, user_id=user_id, title_id=title_id, state="unseen")
    return Write(
        arm="not_seen",
        row_id=None,
        user_id=user_id,
        kind=kind,
        title_ids=(title_id,),
        prior_state=(prior,),
        log=f"not_seen(title {title_id}) -> state unseen, no observation row",
    )


# --- undo ------------------------------------------------------------------------------------


class UndoRefused(Exception):
    """Decision 35 scopes Undo to the current block; reaching further back is refused."""


@dataclass(frozen=True)
class Undo:
    arm: str
    row_id: int | None
    user_id: int
    kind: str
    title_ids: tuple[int, ...]
    unsuperseded: tuple[int, ...] = field(default_factory=tuple)
    restored: tuple[PriorState, ...] = field(default_factory=tuple)
    log: str = ""


async def undo(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    write: Write | None = None,
    arm: str | None = None,
    row_id: int | None = None,
    title_ids: Sequence[int] = (),
    prior_state: Sequence[PriorState] = (),
    block_started_at: datetime | None = None,
) -> Undo:
    """Reverse one observation and the state it implied (decision 35's compensating write).

    The only function permitted to DELETE a verdict or duel row (§4.2). A verdict's supersede
    chain is spliced, so exactly one row stays live. Pass the `Write`, or its jsonb pieces.
    """
    if write is not None:
        arm, row_id = write.arm, write.row_id
        title_ids = write.title_ids
        prior_state = write.prior_state
    if arm not in ("verdict", "duel", "tier_edit", "not_seen"):
        raise ValueError(f"cannot undo arm {arm!r}")

    unsuperseded: tuple[int, ...] = ()
    async with conn.transaction():
        if arm != "not_seen":
            if row_id is None:
                raise ValueError(f"undo of a {arm} needs its row id")
            # Spelled out per arm: the table name must never come from a caller.
            row = await conn.fetchrow(
                {
                    "verdict": "SELECT user_id, created_at FROM verdict WHERE id = $1 FOR UPDATE",
                    "duel": "SELECT user_id, created_at FROM duel WHERE id = $1 FOR UPDATE",
                    "tier_edit": (
                        "SELECT user_id, created_at FROM tier_edit WHERE id = $1 FOR UPDATE"
                    ),
                }[arm],
                row_id,
            )
            if row is None:
                raise UndoRefused(f"{arm} {row_id} is already gone")
            if row["user_id"] != user_id:
                raise UndoRefused(f"{arm} {row_id} belongs to another person")
            if block_started_at is not None and row["created_at"] < block_started_at:
                raise UndoRefused(
                    f"{arm} {row_id} was recorded before this block began — decision 35 scopes "
                    "Undo to the current block"
                )

            if arm == "verdict":
                spliced = await conn.fetch(
                    """
                    UPDATE verdict
                       SET superseded_by = (SELECT superseded_by FROM verdict WHERE id = $1)
                     WHERE superseded_by = $1
                    RETURNING id
                    """,
                    row_id,
                )
                unsuperseded = tuple(int(r["id"]) for r in spliced)
                await conn.execute("DELETE FROM verdict WHERE id = $1", row_id)
            elif arm == "duel":
                await conn.execute("DELETE FROM duel WHERE id = $1", row_id)
            else:
                await conn.execute("DELETE FROM tier_edit WHERE id = $1", row_id)

        for prior in prior_state:
            await _restore_state(conn, user_id=user_id, prior=prior)

    kind = ""
    if title_ids:
        kind = await kind_of(conn, int(title_ids[0]))
    restored = tuple(prior_state)
    return Undo(
        arm=str(arm),
        row_id=row_id,
        user_id=user_id,
        kind=kind,
        title_ids=tuple(int(t) for t in title_ids),
        unsuperseded=unsuperseded,
        restored=restored,
        log=(
            f"undo: {arm} {row_id if row_id is not None else ''} retracted -> "
            f"{0 if arm == 'not_seen' else 1} observation(s) removed, "
            f"{len(restored)} state(s) restored"
        ).replace("  ", " "),
    )


async def _restore_state(
    conn: asyncpg.Connection, *, user_id: int, prior: PriorState
) -> None:
    """Put `user_title` back exactly as it was, including `jf_synced_at` (§7.3's loop guard)."""
    if not prior.existed:
        await conn.execute(
            "DELETE FROM user_title WHERE user_id = $1 AND title_id = $2",
            user_id,
            prior.title_id,
        )
        return
    await conn.execute(
        """
        INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at)
        VALUES ($1, $2, $3, now(), $4)
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET state = EXCLUDED.state, state_changed_at = now(),
              jf_synced_at = EXCLUDED.jf_synced_at
        """,
        user_id,
        prior.title_id,
        prior.state,
        prior.jf_synced_at,
    )


__all__ = [
    "ARM_TIER",
    "ARM_VERDICT",
    "DEFAULT_TIER_SET",
    "HELD_OUT",
    "KINDS",
    "OUTCOMES",
    "VERDICT_LABELS",
    "EmbeddingSource",
    "Kind",
    "Observations",
    "PriorState",
    "Undo",
    "UndoRefused",
    "Write",
    "kind_of",
    "latest_tier_edit_sql",
    "load_observations",
    "record_duel",
    "record_not_seen",
    "record_tier_edit",
    "record_verdict",
    "rescale_level",
    "resolve_embeddings",
    "tier_set_of",
    "undo",
    "zero_embeddings",
]
