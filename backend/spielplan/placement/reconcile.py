"""Placement reconciliation (§5.3) and §10's rebuild set.

Warm titles have no `title_placement` row: their coordinate is the shipped Backbone row. A covered
title below `WARM_SUPPORT` is swept so §5.1's blend has an ê to use.
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
from spielplan.scoring.backbone import WARM_SUPPORT, cold_row_mask

log = logging.getLogger("spielplan.placement")

# Titles per forward pass: ~13 MB of float32 on the corpus contract.
CHUNK = 512

SCOPES = ("owned_missing", "app_acquired", "reimport", "all_missing")

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
    # Blocks that produced rows but never hit a declared column in the whole run: a key-grammar bug.
    blocks_never_hit: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope, "considered": self.considered, "warm": self.warm,
            "demoted": self.demoted, "placed": self.placed, "parked_thin": self.parked_thin,
            "failed": self.failed, "build_ms_p50": self.build_ms_p50,
            "place_ms_p50": self.place_ms_p50, "elapsed_ms": self.elapsed_ms,
            "blocks_never_hit": list(self.blocks_never_hit), "notes": list(self.notes),
        }


# --- who is warm ------------------------------------------------------------------------------


def warm_title_ids(store: Any) -> list[int]:
    """The titles whose coordinate §5.1 takes from the Backbone outright.

    Support >= `WARM_SUPPORT` and not cold-masked. Needs the exporter's `title_ids`; row order is
    never guessed.
    """
    if getattr(store, "is_empty", True) or not store.present.get("backbone.npz"):
        return []
    npz = store.npz("backbone.npz")
    if "title_ids" not in npz.files:
        raise ValueError(
            "backbone.npz ships no `title_ids` array, so its rows cannot be matched to titles; "
            "§4.3 does not name one and the exporter must add it"
        )
    ids = np.asarray(npz["title_ids"]).astype(np.int64)
    keep = np.ones(ids.size, dtype=bool)
    if "item_n" in npz.files:
        keep &= np.asarray(npz["item_n"]).astype(np.int64).reshape(-1) >= WARM_SUPPORT
    keep &= ~cold_row_mask(npz, ids.size)
    return [int(t) for t in ids[keep]]


async def classify_warm(conn: Any, store: Any, *, bundle_version: str) -> tuple[int, int]:
    """Stamp `placement = 'warm'` on `warm_title_ids`, and demote the rest (coverage can shrink, §10)."""
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


# Still 'unplaced' OR no row for this bundle: §12's count reads the stamp, so a row alone is not done.
_MISSING_SQL = """
SELECT t.id
  FROM title t
 WHERE t.placement <> 'warm'
   AND ({owned})
   AND (t.placement = 'unplaced'
        OR NOT EXISTS (
              SELECT 1 FROM title_placement p
               WHERE p.title_id = t.id AND p.bundle_version = $1
           ))
 ORDER BY t.id
"""

# Decision 470: owned, on the seed list, or rated by anyone (superseded verdicts count too).
_SWEPT = (
    "(t.is_owned"
    " OR EXISTS (SELECT 1 FROM seed_list s WHERE s.title_id = t.id)"
    " OR EXISTS (SELECT 1 FROM verdict v WHERE v.title_id = t.id))"
)


async def titles_needing_placement(conn: Any, *, bundle_version: str, scope: str) -> list[int]:
    """The scope's work list. `all_missing` is the admin's widening to every uncoordinated title."""
    if scope not in SCOPES:
        raise ValueError(f"unknown placement scope {scope!r} (known: {list(SCOPES)})")
    if scope == "app_acquired":
        rows = await conn.fetch(
            "SELECT id FROM title WHERE origin = 'acquired' ORDER BY id"
        )
        return [int(r["id"]) for r in rows]

    owned = "true" if scope == "all_missing" else _SWEPT
    rows = await conn.fetch(_MISSING_SQL.format(owned=owned), bundle_version)
    ids = [int(r["id"]) for r in rows]
    if scope == "reimport":
        # §10: re-place every app-acquired title unconditionally.
        acquired = await conn.fetch("SELECT id FROM title WHERE origin = 'acquired'")
        ids = sorted({*ids, *(int(r["id"]) for r in acquired)})
    return ids


# --- placing ----------------------------------------------------------------------------------


_UPSERT = """
INSERT INTO title_placement (
    title_id, bundle_version, e_hat, b_hat, contract_sha256, tower_sha256, input_dim,
    blocks_present, blocks_dropped, blocks_imputed, blocks_empty, blocks_unmapped,
    nnz, build_ms, place_ms
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (title_id, bundle_version) DO UPDATE SET
    e_hat = EXCLUDED.e_hat, b_hat = EXCLUDED.b_hat,
    contract_sha256 = EXCLUDED.contract_sha256, tower_sha256 = EXCLUDED.tower_sha256,
    input_dim = EXCLUDED.input_dim, blocks_present = EXCLUDED.blocks_present,
    blocks_dropped = EXCLUDED.blocks_dropped, blocks_imputed = EXCLUDED.blocks_imputed,
    blocks_empty = EXCLUDED.blocks_empty, blocks_unmapped = EXCLUDED.blocks_unmapped,
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
    # A block in `hit_nowhere` and never in `hit_somewhere` is a key-grammar disagreement.
    hit_somewhere: set[str] = set()
    hit_nowhere: set[str] = set()
    # Titles not parked because their only gaps are unenrichable, reported so the count is explained.
    excused_blocks: set[str] = set()
    excused_titles = 0
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
            hit_somewhere.update(b.blocks_present)
            hit_nowhere.update(b.blocks_empty)
            if not np.isfinite(e_hat[i]).all() or not np.isfinite(b_hat[i]):
                report.failed += 1
                report.notes.append(f"title {b.title_id}: tower returned a non-finite placement")
                continue
            rows.append((
                b.title_id, bundle_version, e_hat[i].astype(np.float32).tobytes(),
                float(b_hat[i]), contract.sha256, tower.sha256, contract.input_dim,
                list(b.blocks_present), list(b.blocks_dropped), list(b.blocks_imputed),
                list(b.blocks_empty), dict(b.unmapped),
                b.nnz, b.build_ms, per_title,
            ))
            builds.append(b.build_ms)
            places.append(per_title)
            if b.is_thin:
                parked.append(b)
            elif b.blocks_dropped or b.blocks_empty:
                excused_titles += 1
                excused_blocks.update(b.blocks_dropped)

        if not rows:
            continue          # every title in the chunk failed; nothing to badge
        placed_ids = [r[0] for r in rows]
        # One transaction per chunk, so the upsert and the badge land together.
        async with conn.transaction():
            await conn.executemany(_UPSERT, rows)
            await conn.execute(
                "UPDATE title SET placement = 'cold_tower', placement_bundle = $2, "
                "placement_at = now() WHERE id = ANY($1::int[])",
                placed_ids, bundle_version,
            )
            parked_now = await _park_thin(conn, parked, len(contract.blocks))
        report.placed += len(placed_ids)
        report.parked_thin += parked_now

    report.build_ms_p50 = _p50(builds)
    report.place_ms_p50 = _p50(places)

    # Reported, not raised: the placements are still the best available.
    if excused_titles:
        report.notes.append(
            f"{excused_titles} title(s) placed and badged but not parked: their only gaps are "
            "feature block(s) " + ", ".join(sorted(excused_blocks)) + ", which no §8 stage 2 "
            "enrichment can fill - a genome unavailable for new titles by construction, an award "
            "nobody gave, a review-text row the export marks uncovered (§8 stage 9, §5.3)"
        )

    report.blocks_never_hit = sorted(hit_nowhere - hit_somewhere)
    if report.blocks_never_hit:
        report.notes.append(
            "feature block(s) " + ", ".join(report.blocks_never_hit) + " produced database keys "
            "and hit none of the columns the contract declares — the builder and the contract "
            "disagree about the key grammar, which no §8 stage 2 enrichment can fix (§4.3)"
        )


async def _park_thin(conn: Any, thin: Sequence[features.BuiltVector], n_blocks: int) -> int:
    """§5.3: park an acquisition job at §8 stage 2 for each thin title (already placed and badged).

    `ON CONFLICT DO NOTHING`: a title already in the pipeline is not dragged back.
    """
    parked = 0
    for b in thin:
        why = []
        if b.blocks_dropped:
            why.append("missing " + ", ".join(b.blocks_dropped))
        if b.blocks_empty:
            why.append("no declared column hit in " + ", ".join(b.blocks_empty))
        reason = (
            f"placed with {len(b.blocks_present)} of {n_blocks + 1} feature blocks — "
            f"{'; '.join(why)}; queued for §8 stage 2 enrichment"
        )
        detail = {
            "blocks_present": list(b.blocks_present),
            "blocks_dropped": list(b.blocks_dropped),
            "blocks_imputed": list(b.blocks_imputed),
            "blocks_empty": list(b.blocks_empty),
            "blocks_unmapped": dict(b.unmapped),
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

    # Warmth decides who lacks a coordinate; `app_acquired` is re-placed regardless (§10).
    if scope != "app_acquired":
        report.warm, report.demoted = await classify_warm(
            conn, store, bundle_version=version
        )

    ids = await titles_needing_placement(conn, bundle_version=version, scope=scope)
    report.considered = len(ids)
    if ids:
        # Lazily: torch costs ~200 MB and a second, and the steady state places nothing.
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


# --- §10's rebuild set: exactly four steps; the first three are injected ----------------------


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

# §10: the Map is a deterministic axis scatter and needs no rebuild step.
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
    """§10's four steps, in §10's order. The first three are injected."""
    return (
        RebuildStep(REBUILD_STEP_IDS[0], REBUILD_SET[0], fold_in or _noop),
        RebuildStep(REBUILD_STEP_IDS[1], REBUILD_SET[1], blend_weights or _noop),
        RebuildStep(REBUILD_STEP_IDS[2], REBUILD_SET[2], ledger_refit or _noop),
        RebuildStep(REBUILD_STEP_IDS[3], REBUILD_SET[3], _replace_placements),
    )


async def _replace_placements(conn: Any, store: Any, version: str) -> dict[str, Any]:
    """Step 4. The import-time sweep (§5.3) is folded in here rather than added as a fifth step."""
    report = await reconcile(conn, store, bundle_version=version, scope="reimport")
    return report.as_dict()


async def assert_staged(conn: Any, store: Any, version: str) -> None:
    """§10's one sanctioned exception to "score only the active bundle": the staged rebuild."""
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
    """Run §10's rebuild set against the staged bundle. Observations are never touched."""
    await assert_staged(conn, store, version)

    plan = rebuild_plan(fold_in=fold_in, blend_weights=blend_weights, ledger_refit=ledger_refit)

    # Placement runs first: steps 1-3 read the coordinates it writes. The report keeps §10's order.
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
