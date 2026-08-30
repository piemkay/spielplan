"""The frozen Backbone basis, and the one coordinate §5.1 scores against. Spec v2.1 §5.1, §4.3.

§4.3 ships `backbone.npz` — "E, E_full, b_i, μ, plus the per-title support counts `item_n`
(the §5.1 gate input)". §5.1 turns those arrays into two numbers per title:

    score_u(t) = b(t) + μ_u + w_cf·⟨v_u, e(t)⟩     e(t) = E[t]                       if rated (warm)
                                                   e(t) = gate·E[t] + (1-gate)·ê(t)  else
                 b(t) = shrunk item prior; b̂(t) from the Cold Tower for cold titles
                 gate = n_t / (n_t + k)            evidence gating, k ≈ 10

THE BRANCH THAT ISN'T ONE. The two lines above are the two limits of a single expression, so
this module writes the expression and never the branch:

    e(t) = gate·E[t] + (1-gate)·ê(t)      with ê := E[t] when the Cold Tower has not placed it
    b(t) = gate·b_i[t] + (1-gate)·b̂(t)    with b̂ := μ    when the Cold Tower has not placed it

"e(t) = E[t] if rated (warm)" is the gate → 1 limit; a title with no Backbone row has n_t = 0,
so gate is exactly 0 and both terms collapse onto the Cold Tower's. Written as one expression
there is nothing for three call sites to disagree about, and `e_source` reports which limit a
particular title actually landed in.

"rated (warm)" is read as CROWD support, not "this viewer rated it": n_t is a crowd count, so
the gate is a crowd quantity, and §6.0 prints it as one number on a shared title card rather
than a different number per viewer.

"b(t) = shrunk item prior" is read as an instruction to the serving layer rather than a claim
about the file: §4.3 ships the raw `b_i`, §5.1 names `b(t)`, and the gate is the only shrinkage
constant the section defines. Reusing it is a smaller choice than inventing a second, unmeasured
one — so a title with a thin crowd row has its bias pulled toward the crowd mean μ.

THE ID MAPPING IS NOT IN THE SPEC. §4.3 lists E, E_full, b_i, μ and item_n and names no
alignment between a row of E and a row of `title`. Without one no row can be joined to anything,
and §4.1 forbids `imdb_id` as the join key (NULL on 21% of titles). This loader therefore
requires a `title_id` array — int32, strictly increasing, aligned row-for-row — which is what
the fixture bundle ships. If the corpus exporter instead means "rows are in dense title.id
order", that is a different contract and this loader must be told, not left to guess: a silently
wrong index produces plausible numbers for the wrong films.

A title with no Backbone row is normal, not exceptional (§8 stage 10: a newly acquired title has
no crowd data at all), so every lookup here returns None rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from spielplan.models.artifacts import ArtifactStore

log = logging.getLogger("spielplan.scoring.backbone")

# §1: "one frozen 64-d collaborative item space". The width is a property of the basis, so a
# file that disagrees is a fault rather than a thing to accommodate.
EMBED_DIM = 64

# §5.1: "gate = n_t / (n_t + k)  evidence gating, k ≈ 10". The one number the section names,
# and the only shrinkage constant it defines.
EVIDENCE_K = 10.0

# Where "rated (warm)" starts. §5.1 writes the branch — `e(t) = E[t] if rated (warm)`, else
# `gate·E[t] + (1-gate)·ê(t)` — and never says which titles are warm, which is the one thing
# the branch needs. Read as "has a Backbone row at all" the middle line is dead: every title
# would take E outright or have no E to blend, and `item_n`, which §4.3 calls "the §5.1 gate
# input", would feed nothing on e(t).
#
# So warm is defined here from the gate itself: warm is where the blend has stopped changing
# the answer. At gate 0.9 the Cold Tower contributes a tenth of a coordinate whose own error is
# larger than that, so blending below it is what the gate is for and blending above it is
# arithmetic nobody can measure. n_t = k·g/(1−g) = 90 at k = 10.
#
# NOT a measured constant — the corpus project tuned k, not this. It is a threshold the spec
# omits, chosen so that the branch it creates is a no-op at the boundary; if the exporter ever
# ships one, this becomes a read.
WARM_GATE = 0.9
WARM_SUPPORT = EVIDENCE_K * WARM_GATE / (1.0 - WARM_GATE)   # 90

ESource = Literal["backbone", "blended", "cold_tower", "none"]

BACKBONE_FILE = "backbone.npz"
_REQUIRED = ("title_id", "E", "b_i", "item_n", "mu")


class BackboneError(RuntimeError):
    """The basis is present but unusable. §3.1 makes an *absent* bundle legal; a corrupt one is
    not the same thing, and the caller decides whether to degrade or refuse."""


@dataclass(frozen=True, eq=False)
class Backbone:
    """`backbone.npz`, read once and indexed by title_id.

    Read once is load-bearing: `NpzFile` ignores the `mmap_mode` `ArtifactStore.npz()` passes
    and re-reads the member on every subscript, so `store.npz("backbone.npz")["E"]` inside a
    loop re-parses the file per title. `open()` binds each array to a local and never touches
    the NpzFile again. At corpus scale the resident cost is 12k × 64 × 4 ≈ 3 MB.
    """

    version: str | None = None
    title_ids: np.ndarray | None = None
    E: np.ndarray | None = None
    b_i: np.ndarray | None = None
    item_n: np.ndarray | None = None
    mu: float = 0.0
    row_of: dict[int, int] = field(default_factory=dict)
    e_full_shape: tuple[int, ...] | None = None
    notes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.E is None

    @property
    def n_rows(self) -> int:
        return 0 if self.title_ids is None else int(self.title_ids.size)

    @classmethod
    def empty(cls) -> Backbone:
        """A first-class value, the analogue of `ArtifactStore.empty()` — a bundle-less
        household still ranks (by the Cold Tower alone, or not at all), it does not crash."""
        return cls()

    @classmethod
    def open(cls, store: ArtifactStore) -> Backbone:
        if store.is_empty or not store.present.get(BACKBONE_FILE):
            return cls.empty()

        z = store.npz(BACKBONE_FILE)
        missing = [name for name in _REQUIRED if name not in z.files]
        if missing:
            raise BackboneError(
                f"{BACKBONE_FILE} is missing {missing}; §4.3 names E, E_full, b_i, mu and item_n, "
                "and `title_id` is what aligns a row of E to a row of `title`"
            )

        title_ids = np.asarray(z["title_id"]).astype(np.int64, copy=False).reshape(-1)
        e = np.ascontiguousarray(np.asarray(z["E"]), dtype=np.float32)
        b_i = np.asarray(z["b_i"]).astype(np.float32, copy=False).reshape(-1)
        item_n = np.asarray(z["item_n"]).astype(np.int64, copy=False).reshape(-1)
        mu_arr = np.asarray(z["mu"])
        e_full_shape = tuple(np.asarray(z["E_full"]).shape) if "E_full" in z.files else None

        if e.ndim != 2 or e.shape[1] != EMBED_DIM:
            raise BackboneError(
                f"E is {e.shape}, not (N, {EMBED_DIM}) — §1's frozen 64-d item space is a "
                "property of the basis, not a shape to accommodate"
            )
        n = e.shape[0]
        for name, arr in (("title_id", title_ids), ("b_i", b_i), ("item_n", item_n)):
            if arr.size != n:
                raise BackboneError(
                    f"{name} has {arr.size} entries against E's {n} rows; the arrays are aligned "
                    "row-for-row or nothing in this file can be joined to a title"
                )
        if mu_arr.size != 1:
            raise BackboneError(
                f"mu has {mu_arr.size} entries; §5.1 uses it as the crowd's global mean (a "
                "scalar) — a per-item μ would change b(t)'s shrinkage target from a constant "
                "to a vector and must be settled before the first real bundle"
            )
        if n and not np.all(np.diff(title_ids) > 0):
            raise BackboneError(
                "title_id is not strictly increasing; duplicate or unsorted ids make the "
                "row → title mapping ambiguous, which is a wrong film rather than an error"
            )

        notes: list[str] = []
        if e_full_shape is not None:
            # §4.3 names both E and E_full and never defines the difference. Every serving path
            # here uses E; E_full is recorded as provenance. If the prefix reading fails on a
            # real bundle we find out here rather than through a ranked list.
            e_full = np.asarray(z["E_full"])
            if e_full.ndim != 2 or e_full.shape[0] != n or e_full.shape[1] < EMBED_DIM:
                notes.append(f"E_full has shape {e_full_shape}; ignored, E is used for scoring")
            elif not np.allclose(e_full[:, :EMBED_DIM], e, atol=1e-5):
                notes.append(
                    "E_full[:, :64] does not equal E; §4.3 never defines the difference, so E "
                    "is used for scoring and E_full is unread provenance"
                )

        backbone = cls(
            version=store.version,
            title_ids=title_ids,
            E=e,
            b_i=b_i,
            item_n=item_n,
            mu=float(mu_arr.reshape(-1)[0]),
            row_of={int(t): i for i, t in enumerate(title_ids)},
            e_full_shape=e_full_shape,
            notes=tuple(notes),
        )
        for note in notes:
            log.warning("backbone %s: %s", store.version, note)
        return backbone

    # --- lookups. A missing row is normal (§8 stage 10), so None, never an exception. ------

    def row(self, title_id: int) -> int | None:
        return self.row_of.get(int(title_id))

    def support(self, title_id: int) -> int:
        """§5.1's n_t. Zero for a title the crowd never rated — which is what makes its gate 0."""
        row = self.row(title_id)
        return 0 if row is None else int(self.item_n[row])

    def embedding(self, title_id: int) -> np.ndarray | None:
        row = self.row(title_id)
        return None if row is None else self.E[row]

    def raw_prior(self, title_id: int) -> float | None:
        """`b_i[t]` as shipped — the *un*shrunk crowd bias. `b(t)` is what §5.1 ranks on."""
        row = self.row(title_id)
        return None if row is None else float(self.b_i[row])

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rows": self.n_rows,
            "mu": self.mu,
            "e_full_shape": list(self.e_full_shape) if self.e_full_shape else None,
            "notes": list(self.notes),
        }


_CACHE: dict[tuple[str | None, str], Backbone] = {}


def load_for(store: ArtifactStore) -> Backbone:
    """The Backbone for a loaded store, read at most once per (version, root).

    A bundle directory is immutable for the life of its version (§10 stages to
    `/data/artifacts/<version>/` and flips), so caching on the version is caching on the file.
    """
    key = (store.version, str(store.root))
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
    """§5.1: `gate = n_t / (n_t + k)`, k ≈ 10.

    n_t = 0 gives exactly 0.0, which is what makes "no Backbone row" and "pure Cold Tower" the
    same statement rather than two.
    """
    n = max(0, int(item_n))
    return n / (n + k)


@dataclass(frozen=True, eq=False)
class Coordinate:
    """Everything §5.1 needs about one title, in the active bundle's basis."""

    title_id: int
    e: np.ndarray            # (64,) float64 — e(t)
    b: float                 # b(t), the shrunk item prior
    gate: float              # §5.1's gate, printed on the §6.0 model line
    item_n: int              # n_t, the gate's input
    e_source: ESource        # which limit of the blend this title landed in


def coordinate(
    title_id: int,
    backbone: Backbone,
    placement: tuple[np.ndarray, float] | None = None,
) -> Coordinate | None:
    """One title's (e, b), or None when it has neither a Backbone row nor a Cold Tower placement.

    `placement` is the Cold Tower's (ê, b̂) for this title in this bundle's basis — §5.3's
    "placement reconciliation", read from `title_placement`. None returned is §12's M2 exit
    criterion failing for this title: it is excluded from every ranked list and named in the
    reconciliation report rather than ranked on a number nobody can defend.
    """
    row = backbone.row(title_id)
    n_t = 0 if row is None else int(backbone.item_n[row])
    g = gate(n_t)

    e_row = None if row is None else backbone.E[row].astype(np.float64)
    b_row = None if row is None else float(backbone.b_i[row])
    e_hat, b_hat = (None, None) if placement is None else (np.asarray(placement[0], dtype=np.float64),
                                                           float(placement[1]))

    if e_row is None and e_hat is None:
        return None

    # The one expression. `warm` is what the gate weights, `cold` what (1-gate) weights; when
    # one half is absent the other stands in, which is exactly the limit §5.1 writes out.
    e_warm = e_row if e_row is not None else e_hat
    e_cold = e_hat if e_hat is not None else e_row
    b_warm = b_row if b_row is not None else b_hat
    b_cold = b_hat if b_hat is not None else backbone.mu

    if e_row is None:
        source: ESource = "cold_tower"
    elif e_hat is None:
        source = "backbone"
    else:
        source = "blended"

    # `e_warm is e_cold` only when there is nothing to blend: return the row untouched rather
    # than g·E + (1-g)·E, which is E to within float error and not E.
    e = e_warm if e_cold is e_warm else g * e_warm + (1.0 - g) * e_cold
    b = b_warm if b_cold is b_warm else g * b_warm + (1.0 - g) * b_cold

    return Coordinate(
        title_id=int(title_id),
        e=np.ascontiguousarray(e, dtype=np.float64),
        b=float(b),
        gate=g,
        item_n=n_t,
        e_source=source,
    )


# --- the bytea convention -------------------------------------------------------------------
# 0008's `title_placement.e_hat` is "64 × float32 LE, the same convention as user_vector.vec".
# One pair of functions, so the two tables cannot drift into two conventions.


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
