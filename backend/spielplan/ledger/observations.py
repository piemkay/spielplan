"""What the fit sees, and the four ways a person writes to it. Spec v2.1 §4.1, §4.2, §5.2,
§6.1, §13, decision 35.

Two halves, and they answer to different sentences.

READING (`load_observations`) turns the three observation tables into the `ObservationSet`
`model.fit` consumes. Three rules decide the queries, and each of them is a rule someone will
later "clean up" unless the reason is written next to it:

  * **Every verdict row, superseded and live alike.** §5.2's fourth arm is "rewatch
    re-ratings → new ordinal observation → drift signal for free", and §4.2 keeps the table
    append-only precisely so that history survives. Adding `WHERE superseded_by IS NULL` is
    the single most likely regression in this subsystem and deletes the fourth arm without
    deleting a line of code.
  * **No held-out duel.** §13: "the 10% uniform-random comparison stream is the *only* data
    used to evaluate the tier model — adaptively-selected pairs inflate reliability (measured
    effect; the guard is non-negotiable)." A pair the model was fitted on is not held out, so
    `selection = 'uniform_holdout'` never reaches the fit. §13 does not write this exclusion
    down; it follows from what the stream is for.
  * **Per kind.** §4.1 rule 5: "every ranking surface partitions by it (measured: the
    unpartitioned crowd top-10 is 8/10 TV series)". The Ledger's whole output is a ranking,
    `ledger_cutpoints` is already keyed `(user_id, kind)`, and cutpoints are not separable
    from `s` — they live in one likelihood. So the fit is per (user, kind), and a duel whose
    two titles are of different kinds is refused at the write rather than half-dropped here.

WRITING (`record_*`) is append-only. §4.2: a re-rating INSERTs a new row and stamps
`superseded_by` on the previous one. Nothing in this package updates a verdict's value, and
the only code allowed to DELETE a verdict or a duel is `undo` below — decision 35's
block-scoped journal, which needs compensating writes rather than a `lastAction` variable.

The embedding source is injected. §5.1 says a title's coordinate is its Backbone row when it
has one and the Cold Tower's placement when it does not, but *which* is a question about
artifacts and the placement pipeline, and the Ledger must not learn the answer: it takes a
callable and gets back 64-d rows plus a mask. §3.1 makes a bundle-less household legal, so
`zero_embeddings` is a real mode and not a stub — with e = 0 the model degenerates to
s = μ + r and still ranks everything the person has rated.
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
from spielplan.ledger.model import EMBED_DIM, OUT_A, OUT_B, OUT_TIE, ObservationSet

log = logging.getLogger("spielplan.ledger.observations")

Kind = Literal["movie", "series"]
KINDS: tuple[str, ...] = ("movie", "series")

# §4.2's own default, repeated in `ledger_cutpoints.tier_set`'s DDL default. The size of this
# tuple is K, and `ledger_cutpoints`' CHECK ties `array_length(boundaries) = K − 1`.
DEFAULT_TIER_SET: tuple[str, ...] = ("F", "D", "C", "B", "A", "A+", "S")

# §4.2: 0 disliked / 1 ok / 2 liked. Named so the API and the log lines do not each spell the
# mapping out and drift apart.
VERDICT_LABELS: tuple[str, ...] = ("disliked", "fine", "liked")

OUTCOMES = {"A": OUT_A, "B": OUT_B, "TIE": OUT_TIE}

# §13 stream (a). Held out from the fit; still written, still read by the evaluation.
HELD_OUT = "uniform_holdout"

# The arms the fit's ordinal block carries. `model.ObservationSet.ord_arm` numbers them.
ARM_VERDICT, ARM_TIER = 0, 1


# --- the embedding seam ---------------------------------------------------------------------

# (title_ids) -> (float64[n, 64], bool[n]). May be sync or async: a Backbone-backed source is
# an in-memory npz lookup and a placement-backed one is a query, and neither should have to
# pretend to be the other.
EmbeddingSource = Callable[
    [Sequence[int]],
    "tuple[np.ndarray, np.ndarray] | Awaitable[tuple[np.ndarray, np.ndarray]]",
]


def zero_embeddings(title_ids: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    """§3.1: "a bundle-less app is a legal state". With no artifact bundle there is no 64-d
    basis, so e = 0 and s = μ + r: the fit still learns cutpoints, σ and a ranking over what
    the person has rated, and loses only generalisation to what they have not."""
    n = len(title_ids)
    return np.zeros((n, EMBED_DIM)), np.zeros(n, dtype=bool)


def placement_embeddings(
    conn: asyncpg.Connection, *, bundle_version: str | None = None
) -> EmbeddingSource:
    """The Cold Tower half of §5.1's e(t), read from `title_placement`.

    Only the half that lives in Postgres. A warm title's coordinate *is* the row `backbone.npz`
    ships (§8 stage 10 stamps `title.placement = 'warm'` and stores no vector — copying those
    rows into the database would make a bundle re-import recompute five things where §10 says
    four), so a Backbone-backed source belongs in front of this one via `chain`. On its own
    this returns `embedded = False` for every warm title, which is honest rather than wrong:
    those titles get an r and simply do not inform v.
    """

    async def rows(title_ids: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        ids = list(title_ids)
        matrix = np.zeros((len(ids), EMBED_DIM))
        embedded = np.zeros(len(ids), dtype=bool)
        if not ids:
            return matrix, embedded
        # §10: "everything expressed in the old Backbone's basis is garbage against a new one."
        # A placement from another bundle is not stale, it is meaningless, so it is not read.
        found = await conn.fetch(
            """
            SELECT p.title_id, p.e_hat
            FROM title_placement p
            JOIN artifact_bundle b ON b.version = p.bundle_version
            WHERE p.title_id = ANY($1::int[])
              AND ($2::text IS NULL OR p.bundle_version = $2)
              AND ($2::text IS NOT NULL OR b.state = 'active')
            """,
            ids,
            bundle_version,
        )
        position = {tid: i for i, tid in enumerate(ids)}
        for row in found:
            i = position[row["title_id"]]
            matrix[i] = np.frombuffer(row["e_hat"], dtype="<f4").astype(float)
            embedded[i] = True
        return matrix, embedded

    return rows


def chain(*sources: EmbeddingSource) -> EmbeddingSource:
    """§5.1: the warm Backbone row first, the Cold Tower placement second. The first source
    that has a row for a title wins; a title no source has keeps e = 0 and `embedded = False`."""

    async def rows(title_ids: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        ids = list(title_ids)
        matrix = np.zeros((len(ids), EMBED_DIM))
        embedded = np.zeros(len(ids), dtype=bool)
        for source in sources:
            missing = ~embedded
            if not missing.any():
                break
            wanted = [tid for tid, gap in zip(ids, missing, strict=True) if gap]
            part, present = await resolve_embeddings(source, wanted)
            where = np.flatnonzero(missing)[present]
            matrix[where] = part[present]
            embedded[where] = True
        return matrix, embedded

    return rows


async def resolve_embeddings(
    embeddings: EmbeddingSource, title_ids: Sequence[int]
) -> tuple[np.ndarray, np.ndarray]:
    """Call the injected source and check its shape here, once.

    A source that hands back 32 columns produces a fit that converges to a wrong answer rather
    than an error, so the width is asserted at the seam rather than discovered in σ.
    """
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
    """`obs` is what `model.fit` takes; the rest is what the *writer* of the fit needs.

    `model` has no clock by design (§5.3 budgets are measured on it, and a budget measured
    through a database is a measurement of a database), but §5.2's freshness rule — "after 12
    months untouched, a title's σ inflates" — is a question about wall-clock time. So the
    per-title clock travels beside the `ObservationSet` rather than inside it.
    """

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
    mean_margin: float = 1.0

    @property
    def title_ids(self) -> np.ndarray:
        return self.obs.title_ids


async def tier_set_of(conn: asyncpg.Connection, *, user_id: int, kind: str) -> tuple[str, ...]:
    """§5.2: "K = size of the configured tier set (default 7: F/D/C/B/A/A+/S ⇒ 6 learned
    cutpoints)". The set is per (user, kind) because `ledger_cutpoints` is."""
    configured = await conn.fetchval(
        "SELECT tier_set FROM ledger_cutpoints WHERE user_id = $1 AND kind = $2", user_id, kind
    )
    return tuple(configured) if configured else DEFAULT_TIER_SET


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

    # ------------------------------------------------------------------------------------
    # §4.2 / §5.2 arm 1 and arm 4. There is deliberately NO `WHERE superseded_by IS NULL`.
    # A rewatch re-rating is "a new ordinal observation (drift signal for free)", which is a
    # statement about the *set*, not about the row: both the answer the person gave in 2024
    # and the one they gave last week are in it, and the fit places the title between them.
    #
    # `NOT is_reask` is the one exclusion. §13 stream (b) is "a separate silent re-ask
    # stream — ~10% of comparisons/verdicts re-asked after ≥3 days; ~200 re-asks measure the
    # flip rate σ". A re-ask is the *same* judgement posed twice to measure judgement noise,
    # not a second judgement; counting it would double one answer's weight and would make
    # every user's ranking depend on which rows the re-ask scheduler happened to sample. The
    # filter is on `is_reask` (NOT NULL, DEFAULT false) rather than on `reask_of` (nullable,
    # ON DELETE SET NULL) because a rule keyed on the nullable column fails *open* the moment
    # an undo clears it — it would silently start counting both. The row stays in the table,
    # so §13's instrument keeps its readings; it just does not move the model it measures.
    # ------------------------------------------------------------------------------------
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

    # §5.2 arm 3: "Tier edits (drag-drop, explicit picks) — K-level ordered logit … drag-and-drop
    # = data, not override; the model re-fits around it." An edit does not delete an earlier
    # edit, so every row counts, exactly as for verdicts.
    tier_edits = await conn.fetch(
        """
        SELECT e.title_id, e.tier, e.created_at
        FROM tier_edit e
        JOIN title t ON t.id = e.title_id
        WHERE e.user_id = $1 AND t.kind = $2
        ORDER BY e.id
        """,
        user_id,
        kind,
    )

    # §5.2 arm 2. Both sides must be of this kind: §4.1 rule 5 partitions the ranking, and a
    # duel with one foot in each partition is not evidence about either. `record_duel` refuses
    # to write one, so this predicate should never fire — it is here because a filter that only
    # holds because of a check somewhere else is a filter that stops holding quietly.
    #
    # `selection <> 'uniform_holdout'`: see §13 in the module docstring.
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

    clamped = 0
    for row in tier_edits:
        level = int(row["tier"])
        if not 0 <= level < n_levels:
            # A household that shrinks its tier set leaves rows above K−1 behind. Clamping
            # keeps them meaning "the top tier they had" rather than crashing the nightly job
            # or, worse, indexing a cutpoint that does not exist.
            clamped += 1
            level = min(max(level, 0), n_levels - 1)
        ord_index.append(position[row["title_id"]])
        ord_level.append(level)
        ord_arm.append(ARM_TIER)
        _touch(row["title_id"], row["created_at"])
    if clamped:
        log.warning("clamped %d tier edit(s) outside 0..%d", clamped, n_levels - 1)

    duel_a: list[int] = []
    duel_b: list[int] = []
    duel_outcome: list[int] = []
    duel_margin: list[float] = []
    for row in duels:
        duel_a.append(position[row["title_a"]])
        duel_b.append(position[row["title_b"]])
        duel_outcome.append(OUTCOMES[row["outcome"]])
        # §4.2: "margin optional: decisive vs hesitant". A margin-less row (§6.3's drag-drop
        # neighbour duels) is not weightless — it is an ordinary, non-decisive comparison, so
        # it carries §6.1's hesitant weight. The constant comes from `hp`, never from here.
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
        # §5.2 names no decay constant, and §4.3's list of shipped constants has no home for
        # one. The freshness mechanism the spec *does* name operates on σ, not on the
        # likelihood, so every ordinal row weighs the same.
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
        mean_margin=float(np.mean(duel_margin)) if duel_margin else hp.margin_hesitant,
    )


# --- the write path ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorState:
    """What `user_title` held before an observation implied a change to it.

    Undo compensates what it did, not what it intended (§7.3's `jf_synced_at` is the loop
    guard, so restoring the state without restoring the stamp would make the next sweep push a
    write nobody asked for). `existed = False` means there was no row at all, which §7.3 is
    explicit is "the *default*, not an assertion".
    """

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
    # §4.2: "a re-rating supersedes rather than overwrites". The row this write stamped, so
    # undo can un-stamp exactly that one — `rate_observation.superseded_verdict_id`.
    superseded_id: int | None = None
    implied_seen: bool = False
    prior_state: tuple[PriorState, ...] = ()
    # §6.7's model-log rail: the arm and the entities are knowledge this module has and the
    # route does not.
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
    """The app-side half of §7.3's write. `jf_synced_at = NULL` marks the push as *owed*: the
    person acted and Jellyfin has not been told yet. The network call belongs to the route,
    after the commit — §3.3: "the app must work when Jellyfin is down"."""
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
    """§5.2 arm 1, and §6.1's "Verdict implies `seen`".

    §4.2 in full: the INSERT is the whole of the write, and the previous live row is *stamped*,
    never edited. The two statements share one transaction, so no half-superseded state — a
    title with two live verdicts, or with none — is reachable by a caller that dies between
    them.
    """
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
        # §4.2's supersede stamp. A re-ask supersedes too: it is the person's latest answer and
        # the card must show it. It is excluded from the *fit* (see `load_observations`), which
        # is a different question from which row is current — and the two only look like one
        # question because the fit famously does not filter on `superseded_by` at all.
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
    """§5.2 arm 2. Writes the RAW margin, not a weight.

    §6.1: "a persistent decisive toggle sets the margin weight (~1.6 vs 1.0)". Pass `decisive`
    with `hp` and the two numbers stay in `ledger_hyperparams.json` where §4.3 puts them; pass
    `margin=None` with neither and the row is margin-less, which is what §6.3's drag-drop
    neighbour duels are. `model` normalises whatever is stored — the functional form is §4.3's
    `margin_form`, so it is applied where that constant is read and not here.
    """
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
        # §4.1 rule 5. Dropping it in the loader instead would leave a row in the table that
        # nothing ever reads, which is how a "why is my count wrong" afternoon starts.
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
    """§5.2 arm 3. "drag-and-drop = data, not override; the model re-fits around it."

    No implied `seen`: §6.1 says a *verdict* implies seen and says it of nothing else, and a
    board can carry a title the person has placed from a trailer without claiming they watched
    it. The neighbour duels §6.3 pairs with a drop-between are two `record_duel` calls by the
    caller, because whether there were neighbours is a fact about the board, not about the arm.
    """
    if via not in ("drag_drop", "explicit"):
        raise ValueError(f"via must be 'drag_drop' or 'explicit', not {via!r}")
    kind = await kind_of(conn, title_id)
    tier_set = await tier_set_of(conn, user_id=user_id, kind=kind)
    if not 0 <= tier < len(tier_set):
        raise ValueError(f"tier {tier} is outside the configured set {tier_set}")

    row_id = await conn.fetchval(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1,$2,$3,$4) RETURNING id",
        user_id,
        title_id,
        tier,
        via,
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
    """§6.1's `Not seen`, and §4.2's owner decision of 2026-08-29: "no 'forgotten' state — 'seen,
    don't remember' is marked plain `unseen`; verdict/duel history is append-only and survives
    the flip."

    So this writes no observation row and deletes none: it is a state change, and the person's
    ratings of the title are still in the likelihood. It is here rather than in `sync.seen`
    because it is one of the Rate surface's five taps and the journal must be able to undo it
    with the same call it undoes the other four with.
    """
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
    """Decision 35 scopes Undo to the current block. Reaching further back is refused rather
    than silently performed, because the person's mental model of "one more tap" ends at the
    block boundary and a compensating write they cannot see is not an undo."""


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
    """Reverse one observation and the state it implied. Decision 35's compensating write.

    **This is the only function in the package permitted to DELETE a verdict or a duel row.**
    §4.2 makes those tables append-only for everything else; `data-rules-verdict-append-only`
    says so, and a static half of that row's test greps for it.

    A verdict's supersede chain is *spliced*, not merely un-stamped: the row the deleted one
    superseded inherits the deleted row's own `superseded_by`. For the ordinary case — undoing
    the newest rating — that is NULL, so the previous rating becomes live again. For the case
    decision 35 does not reach but which a future audit tool might, it leaves exactly one live
    row instead of two.

    Pass the `Write` the record_* call returned, or its pieces if the journal round-tripped
    them through jsonb.
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
            # Spelled out per arm rather than interpolated: the table name is the one thing in
            # this function that must never be able to come from a caller.
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
    """Put `user_title` back exactly as it was — including `jf_synced_at`.

    §7.3 makes that stamp the loop guard: "present + NULL → push". Restoring `seen` with a NULL
    stamp where the row previously had one would make the next 15-minute sweep push a write the
    person never asked for.
    """
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
    "chain",
    "kind_of",
    "load_observations",
    "placement_embeddings",
    "record_duel",
    "record_not_seen",
    "record_tier_edit",
    "record_verdict",
    "resolve_embeddings",
    "tier_set_of",
    "undo",
    "zero_embeddings",
]
