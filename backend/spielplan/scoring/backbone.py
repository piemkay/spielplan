"""The frozen Backbone basis (`backbone.npz`, §4.3) and the per-title coordinate §5.1 scores against.

§5.1's warm/cold branch is written as one blend, `gate·warm + (1-gate)·cold`, whose limits are the
two cases. Rows align to `title` through the exporter's `title_ids` array, which §4.3 does not name.
"""

from __future__ import annotations

import logging
import zipfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from spielplan.ledger.hyperparams import DEFAULTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from spielplan.models.artifacts import ArtifactStore

log = logging.getLogger("spielplan.scoring.backbone")

EMBED_DIM = 64

# One field shared with §6.0's why-numbers. `DEFAULTS` binds the dataclass default, not the bundle's
# value; `hyperparams._PARSED_NOT_THREADED` reports the gap.
EVIDENCE_K = DEFAULTS.gate_k

# §5.1 never says which titles are "warm"; warm is where the gate stops changing the answer.
WARM_GATE = DEFAULTS.warm_gate
WARM_SUPPORT = EVIDENCE_K * WARM_GATE / (1.0 - WARM_GATE)   # 90, still derived from the two

ESource = Literal["backbone", "blended", "cold_tower", "none"]

BACKBONE_FILE = "backbone.npz"
_REQUIRED = ("title_ids", "E", "b_i", "item_n", "mu")

# The export writes E as zeros for `cold_mask` rows. Such a row is treated as absent (gate 0) for
# the coordinate, but its b_i is a real crowd prior and is still read (see `raw_prior`).
COLD_MASK_ARRAY = "cold_mask"

# Fallback when no mask ships: flagged rows have norm ~1e-13, real ones >= ~6e-5.
COLD_ROW_NORM = 1e-9


class BackboneError(RuntimeError):
    """The basis is present but unusable (an absent bundle is legal, §3.1)."""


@contextmanager
def _reading(what: str) -> Iterator[None]:
    """Every read of `backbone.npz` fails as `BackboneError`, which `app.py` degrades on.

    `np.load` raises EOFError at zero bytes, BadZipFile on truncation, and zlib.error on in-place
    damage to a DEFLATE member; none of those is a RuntimeError.
    """
    try:
        yield
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, zlib.error) as exc:
        raise BackboneError(
            f"{BACKBONE_FILE} could not be read ({what}): {type(exc).__name__}: {exc}"
        ) from exc


def cold_row_mask(source: Any, n: int, e: np.ndarray | None = None) -> np.ndarray:
    """`bool[n]`: which rows of `backbone.npz` carry no coordinate at all.

    Pass `e` if you already hold E: `NpzFile` re-parses a member on every subscript.
    """
    files = set(getattr(source, "files", ()) or ())
    if COLD_MASK_ARRAY in files:
        mask = np.asarray(source[COLD_MASK_ARRAY]).reshape(-1)
        if mask.size != n:
            raise BackboneError(
                f"{COLD_MASK_ARRAY} has {mask.size} entries against E's {n} rows; a mask that "
                "does not align row-for-row names the wrong films as uncoordinated"
            )
        return mask.astype(bool, copy=False)
    if e is None:
        if "E" not in files:
            return np.zeros(n, dtype=bool)
        e = source["E"]
    return np.linalg.norm(np.asarray(e, dtype=np.float64), axis=1) < COLD_ROW_NORM


@dataclass(frozen=True, eq=False)
class Backbone:
    """`backbone.npz`, read once and indexed by title_id.

    Read once because `NpzFile` ignores `mmap_mode` and re-reads the member on every subscript.
    """

    version: str | None = None
    title_ids: np.ndarray | None = None
    E: np.ndarray | None = None
    b_i: np.ndarray | None = None
    item_n: np.ndarray | None = None
    mu: float = 0.0
    row_of: dict[int, int] = field(default_factory=dict)
    # Unlike `row_of`, these two include cold-masked rows.
    support_of: dict[int, int] = field(default_factory=dict)
    prior_of: dict[int, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.E is None

    @classmethod
    def empty(cls) -> Backbone:
        return cls()

    @classmethod
    def open(cls, store: ArtifactStore) -> Backbone:
        if store.is_empty or not store.present.get(BACKBONE_FILE):
            return cls.empty()

        with _reading("opening the archive and reading the required arrays"):
            z = store.npz(BACKBONE_FILE)
            missing = [name for name in _REQUIRED if name not in z.files]
            if missing:
                raise BackboneError(
                    f"{BACKBONE_FILE} is missing {missing}; §4.3 names E, E_full, b_i, mu and "
                    "item_n, and `title_ids` is what aligns a row of E to a row of `title`"
                )

            title_ids = np.asarray(z["title_ids"]).astype(np.int64, copy=False).reshape(-1)
            e = np.ascontiguousarray(np.asarray(z["E"]), dtype=np.float32)
            b_i = np.asarray(z["b_i"]).astype(np.float32, copy=False).reshape(-1)
            item_n = np.asarray(z["item_n"]).astype(np.int64, copy=False).reshape(-1)
            mu_arr = np.asarray(z["mu"])

        if e.ndim != 2 or e.shape[1] != EMBED_DIM:
            raise BackboneError(
                f"E is {e.shape}, not (N, {EMBED_DIM}) — §1's frozen 64-d item space is a "
                "property of the basis, not a shape to accommodate"
            )
        n = e.shape[0]
        for name, arr in (("title_ids", title_ids), ("b_i", b_i), ("item_n", item_n)):
            if arr.size != n:
                raise BackboneError(
                    f"{name} has {arr.size} entries against E's {n} rows; the arrays are aligned "
                    "row-for-row or nothing in this file can be joined to a title"
                )
        if mu_arr.size != 1:
            raise BackboneError(
                f"mu has {mu_arr.size} entries; §5.1 uses it as the crowd's global rating "
                "intercept (a scalar), and a per-item μ is a different model that must be "
                "settled before a bundle ships one"
            )
        if n and not np.all(np.diff(title_ids) > 0):
            raise BackboneError(
                "title_ids is not strictly increasing; duplicate or unsorted ids make the "
                "row → title mapping ambiguous, which is a wrong film rather than an error"
            )

        notes: list[str] = []
        with _reading(f"reading {COLD_MASK_ARRAY}"):
            cold = cold_row_mask(z, n, e=e)
        n_cold = int(cold.sum())
        if n_cold:
            # ASCII: a note is logged, and a Windows cp1252 console dies on a section sign.
            notes.append(
                f"{n_cold} of {n} rows carry no coordinate and are excluded from the basis; "
                "they take the gate-0 limit of e(t) and are left to the Cold Tower sweep"
            )

        backbone = cls(
            version=store.version,
            title_ids=title_ids,
            E=e,
            b_i=b_i,
            item_n=item_n,
            mu=float(mu_arr.reshape(-1)[0]),
            row_of={int(t): i for i, t in enumerate(title_ids) if not cold[i]},
            support_of={int(t): int(item_n[i]) for i, t in enumerate(title_ids)},
            prior_of={int(t): float(b_i[i]) for i, t in enumerate(title_ids)},
            notes=tuple(notes),
        )
        for note in notes:
            log.warning("backbone %s: %s", store.version, note)
        return backbone

    # A missing row is normal (§8 stage 10), so lookups return None, never raise.

    def row(self, title_id: int) -> int | None:
        return self.row_of.get(int(title_id))

    def support(self, title_id: int) -> int:
        """§5.1's n_t: 0 for a title with no coordinate row, which makes its gate 0."""
        row = self.row(title_id)
        return 0 if row is None else int(self.item_n[row])

    def crowd_support(self, title_id: int) -> int:
        """`item_n` as shipped, cold-masked rows included: popularity, not the gate's n_t."""
        return int(self.support_of.get(int(title_id), 0))

    def embedding(self, title_id: int) -> np.ndarray | None:
        row = self.row(title_id)
        return None if row is None else self.E[row]

    def raw_prior(self, title_id: int) -> float | None:
        """`b_i[t]` as shipped (already shrunk by the corpus), cold-masked rows included."""
        row = self.row(title_id)
        if row is not None:
            return float(self.b_i[row])
        return self.prior_of.get(int(title_id))


_CACHE: dict[tuple[str | None, str, int, int], Backbone] = {}


def _file_stamp(store: ArtifactStore) -> tuple[int, int]:
    """(size, mtime_ns) of the resolved `backbone.npz`, or (-1, -1) when there is none."""
    try:
        stat = store.path(BACKBONE_FILE).stat()
    except (RuntimeError, OSError):
        return (-1, -1)
    return (stat.st_size, stat.st_mtime_ns)


def load_for(store: ArtifactStore) -> Backbone:
    """The Backbone for a loaded store, cached per (version, root, size, mtime).

    The file stamp is in the key because a re-import rewrites the same version's directory.
    """
    key = (store.version, str(store.root), *_file_stamp(store))
    if key not in _CACHE:
        if len(_CACHE) >= 2:      # the swap window holds two: the outgoing and the incoming.
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = Backbone.open(store)
    return _CACHE[key]


def forget_cached() -> None:
    """Drop the cache. §10 restarts the process on a swap, so this exists for tests."""
    _CACHE.clear()


# --- the coordinate -------------------------------------------------------------------------


def gate(item_n: int, k: float = EVIDENCE_K) -> float:
    """§5.1: `gate = n_t / (n_t + k)`; n_t = 0 gives exactly 0.0 (pure Cold Tower)."""
    n = max(0, int(item_n))
    return n / (n + k)


@dataclass(frozen=True, eq=False)
class Coordinate:
    """Everything §5.1 needs about one title, in the active bundle's basis."""

    title_id: int
    e: np.ndarray            # (64,) float64 — e(t)
    b: float                 # b(t), the shrunk item prior
    gate: float              # §5.1's gate, printed on the §6.0 model line
    item_n: int              # n_t, the gate's input — 0 for a row this basis treats as absent
    e_source: ESource        # which limit of the blend this title landed in
    crowd_n: int = 0         # `Backbone.crowd_support`; defaulted for hand-built fixtures


def coordinate(
    title_id: int,
    backbone: Backbone,
    placement: tuple[np.ndarray, float] | None = None,
) -> Coordinate | None:
    """One title's (e, b), or None when it has neither a Backbone row nor a Cold Tower placement.

    `placement` is the Cold Tower's (ê, b̂) from `title_placement`.
    """
    row = backbone.row(title_id)
    n_t = 0 if row is None else int(backbone.item_n[row])
    g = gate(n_t)
    # A cold-masked row keeps its b_i, so the prior is gated by its crowd support.
    g_b = g if row is not None else gate(backbone.crowd_support(title_id))

    e_row = None if row is None else backbone.E[row].astype(np.float64)
    b_row = backbone.raw_prior(title_id)
    e_hat, b_hat = (None, None) if placement is None else (np.asarray(placement[0], dtype=np.float64),
                                                           float(placement[1]))

    if e_row is None and e_hat is None:
        return None

    # When one half is absent the other stands in, which is the limit §5.1 writes out.
    e_warm = e_row if e_row is not None else e_hat
    e_cold = e_hat if e_hat is not None else e_row
    b_warm = b_row if b_row is not None else b_hat
    b_cold = b_hat if b_hat is not None else b_row

    if e_row is None:
        source: ESource = "cold_tower"
    elif e_hat is None:
        source = "backbone"
    else:
        source = "blended"

    # Return the row untouched rather than g·E + (1-g)·E, which is E only to within float error.
    e = e_warm if e_cold is e_warm else g * e_warm + (1.0 - g) * e_cold
    b = b_warm if b_cold is b_warm else g_b * b_warm + (1.0 - g_b) * b_cold

    return Coordinate(
        title_id=int(title_id),
        e=np.ascontiguousarray(e, dtype=np.float64),
        b=float(b),
        gate=g,
        item_n=n_t,
        e_source=source,
        crowd_n=backbone.crowd_support(title_id),
    )


# --- what the personal half reads -----------------------------------------------------------
# E's row norm grows with crowd support and ê sits on another scale, so the raw inner product ranks
# by popularity. The fits read the direction instead, weighted by the gate for a Backbone-only row
# and by 1 otherwise (decisions 469, 471). `coordinate` stays unscaled.

# Stamped on every `user_vector` and `ledger_fit` row; a fit with any other stamp is refitted.
COORDINATE_GEOMETRY = "gated-direction"


def direction(c: Coordinate) -> np.ndarray:
    return directions([c])[0]


def directions(coords: Sequence[Coordinate]) -> np.ndarray:
    """`direction` for many titles at once, as one (n, 64) float64 matrix."""
    if not coords:
        return np.zeros((0, EMBED_DIM))
    e = np.ascontiguousarray([c.e for c in coords], dtype=np.float64)
    weight = np.asarray(
        [c.gate if c.e_source == "backbone" else 1.0 for c in coords], dtype=np.float64
    )
    norm = np.linalg.norm(e, axis=1)
    scale = np.divide(weight, norm, out=np.zeros_like(norm), where=norm > 0.0)
    return e * scale[:, None]


# --- the bytea convention: 64 x float32 LE, shared by title_placement.e_hat and user_vector.vec --


def pack_vec(v: np.ndarray) -> bytes:
    vec = np.asarray(v, dtype="<f4").reshape(-1)
    if vec.size != EMBED_DIM:
        raise ValueError(f"expected a {EMBED_DIM}-d vector, got {vec.size}")
    return vec.tobytes()


def unpack_vec(raw: bytes) -> np.ndarray:
    vec = np.frombuffer(raw, dtype="<f4")
    if vec.size != EMBED_DIM:
        raise ValueError(f"expected {EMBED_DIM * 4} bytes, got {len(raw)}")
    return vec.astype(np.float64)
