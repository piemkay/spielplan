"""Placement reconciliation and §10's rebuild set. Spec v2.1 §5.3, §8 stages 9–10, §10, §12.

§5.3, the job this module is: "**Placement reconciliation**: any owned title lacking a
coordinate gets a feature vector built from DB data per the feature contract (absent blocks
dropped — the tower's dropout training anticipates this; genome zero-imputed) and runs §8 stages
9–10 only. 19 such titles arrive with the initial bundle; thin ones (2 lack keywords, 3 lack any
DNA row) are still placed, badged, and parked as acquisition jobs for M5 enrichment." Trigger:
"bundle import + nightly sweep". Budget: "seconds".

§12's M2 exit criterion is one query against what this module writes:

    SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'   -- must be 0

**Where a coordinate lives, and why warm titles have no row.** A warm title's coordinate *is*
the row `backbone.npz` already ships (§4.3: "E, E_full, b_i, μ, plus the per-title support
counts `item_n`"). Copying those rows into Postgres would make a bundle re-import recompute five
things where §10 says four, so `title_placement` holds only what this app computed and
`title.placement` records which of the two a title has.

**Warm needs a threshold and the spec gives none.** It is defined once, in
`scoring.backbone.WARM_SUPPORT`, from the gate §5.1 does define: warm is where the blend has
stopped changing the answer (gate ≥ 0.9, i.e. `item_n ≥ 90` at k = 10). It lives beside the gate
rather than here because it is a *scoring* branch — this module only reads it to decide who is
excused from the sweep.

Read instead as "warm iff the Backbone covers the title at all", §5.1's middle line
`e(t) = gate·E[t] + (1-gate)·ê(t)` becomes unreachable: a covered title would be stamped warm and
never placed, so there would be no ê to blend, and every coordinate would come out of exactly one
of the two branches. That is the shape this module had first, and it made `item_n` — which §4.3
calls "the §5.1 gate input" — feed nothing at all on e(t).

This is *not* §8 stage 10's cold **badge** threshold, which is a display decision owned by another
lens; the two must not collapse into one constant.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spielplan.placement import features
from spielplan.placement.contract import FeatureContract, unproducible_meta_names
from spielplan.placement.tower import Tower, load_tower
from spielplan.scoring.backbone import WARM_SUPPORT

log = logging.getLogger("spielplan.placement")

# One forward pass per chunk. 512 keeps the staged matrix small (512 × 6,499 float32 ≈ 13 MB on
# the corpus contract) while making the per-title module and query overhead vanish.
CHUNK = 512

SCOPES = ("owned_missing", "app_acquired", "reimport", "all_missing")

# §5.3's own words, used verbatim in the parked job's reason so the admin board reads as the
# spec does.
PARK_STAGE = 2          # §8 stage 2 enrich — the fetch every later block is derived from
PARK_STATUS = "parked"


@dataclass
class PlacementReport:
    scope: str
    considered: int = 0
    warm: int = 0
    demoted: int = 0
    placed: int = 0
    parked_thin: int = 0
    failed: int = 0
    build_ms_p50: int = 0
    place_ms_p50: int = 0
    elapsed_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope, "considered": self.considered, "warm": self.warm,
            "demoted": self.demoted, "placed": self.placed, "parked_thin": self.parked_thin,
            "failed": self.failed, "build_ms_p50": self.build_ms_p50,
            "place_ms_p50": self.place_ms_p50, "elapsed_ms": self.elapsed_ms,
            "notes": list(self.notes),
        }


# --- who is warm ------------------------------------------------------------------------------


def warm_title_ids(store: Any) -> list[int]:
    """The titles whose coordinate §5.1 takes from the Backbone outright.

    Not the same as "the titles the Backbone covers". §5.1's second line,
    `e(t) = gate·E[t] + (1-gate)·ê(t)`, needs a Cold Tower coordinate for a title that HAS a
    Backbone row but little support behind it — and if every covered title is stamped warm,
    that title never gets one and the line never fires. `scoring.backbone.WARM_SUPPORT` carries
    the threshold and the reasoning; here it decides who is excused from the sweep.

    §4.3 lists `backbone.npz` as "E, E_full, b_i, μ, plus the per-title support counts `item_n`"
    and names no title-id array — but E is a matrix of rows with no stated correspondence to
    `title.id`, so it is unusable as specified. The bundle in hand ships `title_id`; without it
    this function cannot tell which row is which title and says so rather than guessing at row
    order, which would place the whole library against the wrong coordinates.
    """
    if getattr(store, "is_empty", True) or not store.present.get("backbone.npz"):
        return []
    npz = store.npz("backbone.npz")
    if "title_id" not in npz.files:
        raise ValueError(
            "backbone.npz ships no `title_id` array, so its rows cannot be matched to titles; "
            "§4.3 does not name one and the exporter must add it"
        )
    ids = np.asarray(npz["title_id"]).astype(np.int64)
    if "item_n" in npz.files:
        support = np.asarray(npz["item_n"]).astype(np.int64)
        ids = ids[support >= WARM_SUPPORT]
    return [int(t) for t in ids]


async def classify_warm(conn: Any, store: Any, *, bundle_version: str) -> tuple[int, int]:
    """Stamp `placement = 'warm'` on the titles §5.1 reads straight off the Backbone.

    A covered title with support below `WARM_SUPPORT` is deliberately NOT stamped, so the sweep
    picks it up and gives it a Cold Tower coordinate to be blended with — that is the whole of
    §5.1's middle line.

    The second half is not symmetry for its own sake: Backbone coverage can *shrink* under a new
    bundle, and a title left at 'warm' from the previous basis is a title claiming a coordinate
    that no longer exists (§10: "everything expressed in the old Backbone's basis is garbage
    against a new one").
    """
    ids = warm_title_ids(store)
    warm = await conn.fetchval(
        "SELECT count(*) FROM title WHERE id = ANY($1::int[])", ids
    )
    await conn.execute(
        """
        UPDATE title SET placement = 'warm', placement_bundle = $2, placement_at = now()
         WHERE id = ANY($1::int[])
           AND (placement <> 'warm' OR placement_bundle IS DISTINCT FROM $2)
        """,
        ids, bundle_version,
    )
    demoted = await conn.execute(
        """
        UPDATE title SET placement = 'unplaced', placement_bundle = NULL, placement_at = now()
         WHERE placement = 'warm' AND NOT (id = ANY($1::int[]))
        """,
        ids,
    )
    return int(warm or 0), _affected(demoted)


def _affected(status: str) -> int:
    tail = str(status).rsplit(" ", 1)[-1]
    return int(tail) if tail.isdigit() else 0


# --- who needs placing ------------------------------------------------------------------------


_MISSING_SQL = """
SELECT t.id
  FROM title t
 WHERE t.placement <> 'warm'
   AND ({owned})
   AND NOT EXISTS (
         SELECT 1 FROM title_placement p
          WHERE p.title_id = t.id AND p.bundle_version = $1
       )
 ORDER BY t.id
"""


async def titles_needing_placement(conn: Any, *, bundle_version: str, scope: str) -> list[int]:
    """The scope's work list.

    §5.3 scopes the sweep to *owned* titles, and `all_missing` is the admin-triggered widening —
    an unowned bundle title with no Backbone row genuinely has no coordinate, which is exactly
    what §5.3 says and is fine while the ranking surfaces rank the owned library.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown placement scope {scope!r} (known: {list(SCOPES)})")
    if scope == "app_acquired":
        rows = await conn.fetch(
            "SELECT id FROM title WHERE origin = 'acquired' ORDER BY id"
        )
        return [int(r["id"]) for r in rows]

    owned = "true" if scope == "all_missing" else "t.is_owned"
    rows = await conn.fetch(_MISSING_SQL.format(owned=owned), bundle_version)
    ids = [int(r["id"]) for r in rows]
    if scope == "reimport":
        # §10: "Cold Tower re-placement of **every** app-acquired title" — unconditionally,
        # because their vectors are expressed in the previous bundle's basis whether or not a
        # row exists for this one.
        acquired = await conn.fetch("SELECT id FROM title WHERE origin = 'acquired'")
        ids = sorted({*ids, *(int(r["id"]) for r in acquired)})
    return ids


# --- placing ----------------------------------------------------------------------------------


_UPSERT = """
INSERT INTO title_placement (
    title_id, bundle_version, e_hat, b_hat, contract_sha256, tower_sha256, input_dim,
    blocks_present, blocks_dropped, blocks_imputed, nnz, build_ms, place_ms
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
ON CONFLICT (title_id, bundle_version) DO UPDATE SET
    e_hat = EXCLUDED.e_hat, b_hat = EXCLUDED.b_hat,
    contract_sha256 = EXCLUDED.contract_sha256, tower_sha256 = EXCLUDED.tower_sha256,
    input_dim = EXCLUDED.input_dim, blocks_present = EXCLUDED.blocks_present,
    blocks_dropped = EXCLUDED.blocks_dropped, blocks_imputed = EXCLUDED.blocks_imputed,
    nnz = EXCLUDED.nnz, build_ms = EXCLUDED.build_ms, place_ms = EXCLUDED.place_ms,
    created_at = now()
"""

_PARK = """
INSERT INTO acquisition_job (title_id, stage, status, reason, detail)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (title_id) DO NOTHING
"""


async def place_titles(
    conn: Any,
    store: Any,
    contract: FeatureContract,
    tower: Tower,
    title_ids: Sequence[int],
    *,
    bundle_version: str,
    vocab_version: str,
    report: PlacementReport,
) -> None:
    """§8 stages 9 and 10 for a list of titles, in chunks of one forward pass each."""
    builds: list[int] = []
    places: list[int] = []
    for start in range(0, len(title_ids), CHUNK):
        chunk = list(title_ids[start:start + CHUNK])
        built = await features.build_vectors(
            conn, store, contract, chunk, vocab_version=vocab_version
        )
        began = time.perf_counter()
        e_hat, b_hat = tower.place(np.stack([b.vec for b in built]))
        place_ms = int(round((time.perf_counter() - began) * 1000))
        per_title = max(0, place_ms // max(1, len(chunk)))

        rows = []
        parked = []
        for i, b in enumerate(built):
            if not np.isfinite(e_hat[i]).all() or not np.isfinite(b_hat[i]):
                report.failed += 1
                report.notes.append(f"title {b.title_id}: tower returned a non-finite placement")
                continue
            rows.append((
                b.title_id, bundle_version, e_hat[i].astype(np.float32).tobytes(),
                float(b_hat[i]), contract.sha256, tower.sha256, contract.input_dim,
                list(b.blocks_present), list(b.blocks_dropped), list(b.blocks_imputed),
                b.nnz, b.build_ms, per_title,
            ))
            builds.append(b.build_ms)
            places.append(per_title)
            if b.is_thin:
                parked.append(b)

        if not rows:
            continue          # every title in the chunk failed; nothing to badge
        await conn.executemany(_UPSERT, rows)
        placed_ids = [r[0] for r in rows]
        # §8 stage 10: "appears in ranking/search/explore with a 'new — model placement, no
        # crowd data' badge until ratings accrue." `placement = 'cold_tower'` is that badge's
        # one input; `placement_bundle` is the basis it was computed in.
        await conn.execute(
            "UPDATE title SET placement = 'cold_tower', placement_bundle = $2, "
            "placement_at = now() WHERE id = ANY($1::int[])",
            placed_ids, bundle_version,
        )
        report.placed += len(placed_ids)
        report.parked_thin += await _park_thin(conn, parked, len(contract.blocks))

    report.build_ms_p50 = _p50(builds)
    report.place_ms_p50 = _p50(places)


async def _park_thin(conn: Any, thin: Sequence[features.BuiltVector], n_blocks: int) -> int:
    """§5.3: "thin ones … are still placed, badged, and parked as acquisition jobs for M5
    enrichment."

    The title is ready — placed, badged, visible. It is the *job* that is parked, at §8 stage 2
    (enrich), the fetch every later block is derived from: §8's pipeline is sequential, so
    re-entering at 2 regenerates 3..8. `ON CONFLICT DO NOTHING` because a title already moving
    through the pipeline must not be dragged back to stage 2 by a nightly sweep.
    """
    parked = 0
    for b in thin:
        missing = ", ".join(b.blocks_dropped)
        reason = (
            f"placed with {len(b.blocks_present)} of {n_blocks + 1} feature blocks — "
            f"missing {missing}; queued for §8 stage 2 enrichment"
        )
        detail = {
            "blocks_present": list(b.blocks_present),
            "blocks_dropped": list(b.blocks_dropped),
            "blocks_imputed": list(b.blocks_imputed),
            "nnz": b.nnz,
        }
        status = await conn.execute(
            _PARK, b.title_id, PARK_STAGE, PARK_STATUS, reason, detail
        )
        parked += _affected(status)
    return parked


def _p50(values: Sequence[int]) -> int:
    return int(np.median(values)) if len(values) else 0


# --- the job ----------------------------------------------------------------------------------


async def reconcile(
    conn: Any,
    store: Any,
    *,
    bundle_version: str | None = None,
    scope: str = "owned_missing",
    vocab_version: str | None = None,
) -> PlacementReport:
    """§5.3's reconciliation, for one scope. Idempotent: re-running places nothing new."""
    started = time.perf_counter()
    report = PlacementReport(scope=scope)
    version = bundle_version or getattr(store, "version", None)
    if version is None:
        report.notes.append("no artifact bundle is loaded — nothing to place against (§3.1)")
        return report

    contract = FeatureContract.from_store(store)
    report.notes.extend(contract.notes)
    unproducible = features.unproducible_blocks(contract)
    if unproducible:
        report.notes.append(
            "no source for contract block(s) " + ", ".join(unproducible) + " — always dropped"
        )
    stray = unproducible_meta_names(contract)
    if stray:
        report.notes.append(f"meta columns outside the grammar, always zero: {stray[:8]}")

    # Who is warm has to be settled before "lacking a coordinate" means anything, and Backbone
    # coverage is a property of the bundle rather than of the trigger — so every scope that can
    # place a title classifies first. `app_acquired` is the exception: §10 re-places those
    # unconditionally and their warmth is not the question.
    if scope != "app_acquired":
        report.warm, report.demoted = await classify_warm(
            conn, store, bundle_version=version
        )

    ids = await titles_needing_placement(conn, bundle_version=version, scope=scope)
    report.considered = len(ids)
    if ids:
        # torch costs ~200 MB and a second of import time; a sweep with nothing to place — the
        # steady state, once §12's exit criterion holds — must not pay for it.
        tower = load_tower(store, contract)
        vocab = vocab_version or await _vocab_version(conn, store)
        await place_titles(
            conn, store, contract, tower, ids,
            bundle_version=version, vocab_version=vocab, report=report,
        )
    report.elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    log.info("placement reconciliation %s: %s", scope, report.as_dict())
    return report


async def _vocab_version(conn: Any, store: Any) -> str:
    shipped = getattr(store, "vocab_version", None)
    if shipped:
        return str(shipped)
    row = await conn.fetchval(
        "SELECT version FROM dna_vocabulary ORDER BY imported_at DESC LIMIT 1"
    )
    return str(row or "v1")


async def placement_counts(conn: Any, *, bundle_version: str) -> dict[str, int]:
    """§12's M2 exit criterion, and the numbers the §6.6 admin board shows next to it."""
    row = await conn.fetchrow(
        """
        SELECT count(*) FILTER (WHERE is_owned)                            AS owned,
               count(*) FILTER (WHERE is_owned AND placement = 'warm')     AS owned_warm,
               count(*) FILTER (WHERE is_owned AND placement = 'cold_tower') AS owned_cold,
               count(*) FILTER (WHERE is_owned AND placement = 'unplaced') AS owned_unplaced,
               count(*) FILTER (WHERE origin = 'acquired')                 AS acquired
          FROM title
        """
    )
    placements = await conn.fetchval(
        "SELECT count(*) FROM title_placement WHERE bundle_version = $1", bundle_version
    )
    return {**{k: int(v) for k, v in dict(row).items()}, "placement_rows": int(placements)}


# --- §10's rebuild set --------------------------------------------------------------------------
#
# §10: "Re-import therefore recomputes: user fold-in vectors (closed-form, ms), per-label-count
# blend weights, a full Ledger MAP refit, Cold Tower re-placement of every app-acquired title
# (feature vectors rebuilt from the staged bundle's feature contract, whose column set may
# change). (The v1 Map is a deterministic axis scatter and needs no rebuild — a future UMAP lens
# would recompute here.)"
#
# Four things, named, in that order. Owning the list in one place is what makes "exactly four
# and nothing else" a test rather than a promise. The first three belong to other lenses and
# arrive as callables, so this module cannot drift from them and they cannot drift from the
# count.


@dataclass(frozen=True)
class RebuildStep:
    id: str
    title: str
    run: Callable[[Any, Any, str], Awaitable[dict[str, Any]]]


REBUILD_SET: tuple[str, ...] = (
    "user fold-in vectors (closed-form, ms)",
    "per-label-count blend weights",
    "full Personal Ledger MAP refit",
    "Cold Tower re-placement of every app-acquired title",
)

REBUILD_STEP_IDS: tuple[str, ...] = (
    "user-foldin", "blend-weights", "ledger-map-refit", "cold-tower-replacement",
)

# §10's parenthesis, as a guard: "The v1 Map is a deterministic axis scatter and needs no
# rebuild." A step named for one is a bug, and the deterministic axis scatter (§6.4) is the
# reason — `dna_axis` / `dna_axis_weight` are authored TSVs, not fitted state.
FORBIDDEN_STEP_WORDS: tuple[str, ...] = ("umap", "procrustes", "axis", "explore", "map rebuild")


async def _noop(_conn: Any, _store: Any, _version: str) -> dict[str, Any]:
    """A rebuild step whose lens is not wired in yet. It reports rather than pretends."""
    return {"skipped": "not wired"}


def rebuild_plan(
    *,
    fold_in: Callable[[Any, Any, str], Awaitable[dict[str, Any]]] | None = None,
    blend_weights: Callable[[Any, Any, str], Awaitable[dict[str, Any]]] | None = None,
    ledger_refit: Callable[[Any, Any, str], Awaitable[dict[str, Any]]] | None = None,
) -> tuple[RebuildStep, ...]:
    """§10's four steps, in §10's order.

    The first three are injected: they are the scoring and Ledger lenses' work, and this module
    calling into them directly would make the rebuild set depend on the order the lenses land
    rather than on the spec sentence.
    """
    return (
        RebuildStep(REBUILD_STEP_IDS[0], REBUILD_SET[0], fold_in or _noop),
        RebuildStep(REBUILD_STEP_IDS[1], REBUILD_SET[1], blend_weights or _noop),
        RebuildStep(REBUILD_STEP_IDS[2], REBUILD_SET[2], ledger_refit or _noop),
        RebuildStep(REBUILD_STEP_IDS[3], REBUILD_SET[3], _replace_placements),
    )


async def _replace_placements(conn: Any, store: Any, version: str) -> dict[str, Any]:
    """Step 4, and this lens's own.

    §5.3 names bundle import as a reconciliation trigger, so the sweep is folded *into* this
    step rather than added as a fifth — which is what keeps "exactly four things" literally
    true. Scope `reimport` re-places every app-acquired title unconditionally and every owned
    title the new Backbone does not cover.
    """
    report = await reconcile(conn, store, bundle_version=version, scope="reimport")
    return report.as_dict()


async def assert_staged(conn: Any, store: Any, version: str) -> None:
    """§10's invariant has exactly one sanctioned exception, and this is it.

    "Invariant: no process may score or refit with a loaded bundle version different from the
    active row" — but the swap sequence is "validate → stage → **recompute the rebuild set
    against the staged bundle** → transactionally flip". So the rebuild reads a non-active
    store, and it does it through a *positive* check (the row exists and is validated) rather
    than by skipping `assert_matches`.
    """
    if getattr(store, "version", None) != version:
        raise RuntimeError(
            f"rebuild was handed a store on {getattr(store, 'version', None)!r} but was asked "
            f"to rebuild against {version!r}"
        )
    state = await conn.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = $1", version
    )
    if state not in ("validated", "active"):
        raise RuntimeError(
            f"bundle {version!r} is {state!r}; §10 recomputes the rebuild set against a "
            "*staged* bundle, which is one that validated"
        )


async def run_rebuild(
    conn: Any,
    store: Any,
    version: str,
    *,
    fold_in: Callable[[Any, Any, str], Awaitable[dict[str, Any]]] | None = None,
    blend_weights: Callable[[Any, Any, str], Awaitable[dict[str, Any]]] | None = None,
    ledger_refit: Callable[[Any, Any, str], Awaitable[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Run §10's rebuild set against the staged bundle, and report what each step did.

    Nothing here touches `verdict`, `duel` or `tier_edit`: §10 — "Ledger *observations* always
    survive re-import (they reference `title.id` and vocabulary-independent facts)". Nothing
    here touches `dna_axis` or `dna_axis_weight` either, for the reason §10 gives in the same
    breath.
    """
    await assert_staged(conn, store, version)

    plan = rebuild_plan(fold_in=fold_in, blend_weights=blend_weights, ledger_refit=ledger_refit)

    # §10 LISTS the four steps in the order it lists them; it does not claim that is an execution
    # order, and it is not one. Steps 1-3 all read the coordinates step 4 writes: §5.1's e(t)
    # needs ê for a cold or low-support title, and §5.2's fit takes the same coordinates as its
    # embeddings. Run in listed order against a freshly staged bundle, the fold-in materialises
    # `title_prior` and every `user_score` row before a single title has been placed in the new
    # basis — so the newly-activated bundle serves a library with its cold titles missing and its
    # low-support ones shrunk toward μ instead of toward b̂, until the next nightly sweep.
    #
    # This is the same mistake the nightly jobs made (`worker.Job.stage`), in the one path §10
    # actually mandates. The REPORT stays in §10's order, so the import screen reads as the spec
    # does; only the execution is reordered, and the reordering is named here rather than implied
    # by how the tuple happens to be written.
    order = {"cold-tower-replacement": 0}
    outcomes: dict[str, dict[str, Any]] = {}
    for step in sorted(plan, key=lambda s: order.get(s.id, 1)):
        began = time.perf_counter()
        outcome = await step.run(conn, store, version)
        outcomes[step.id] = {
            "id": step.id,
            "title": step.title,
            "elapsed_ms": int(round((time.perf_counter() - began) * 1000)),
            **(outcome or {}),
        }
    results = [outcomes[step.id] for step in plan]
    log.info("rebuild set for %s: %s", version, [r["id"] for r in results])
    return results
