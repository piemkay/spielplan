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
requires a `title_ids` array — int32, strictly increasing, aligned row-for-row — which is the
name the corpus's exporter ships (`backbone.npz` carries E, E_full, E_hat, b_hat, b_i,
cold_mask, item_n, mu and `title_ids`). It read `title_id`, singular, until M4.5: against a real
bundle that name is absent, so the loader raised on a file that was in fact complete. If the
exporter ever instead means "rows are in dense title.id order", that is a different contract and
this loader must be told, not left to guess: a silently wrong index produces plausible numbers
for the wrong films.

A title with no Backbone row is normal, not exceptional (§8 stage 10: a newly acquired title has
no crowd data at all), so every lookup here returns None rather than raising.
"""

from __future__ import annotations

import logging
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from spielplan.ledger.hyperparams import DEFAULTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from spielplan.models.artifacts import ArtifactStore

log = logging.getLogger("spielplan.scoring.backbone")

# §1: "one frozen 64-d collaborative item space". The width is a property of the basis, so a
# file that disagrees is a fault rather than a thing to accommodate.
EMBED_DIM = 64

# §5.1: "gate = n_t / (n_t + k)  evidence gating, k ≈ 10". The one number the section names,
# and the only shrinkage constant it defines.
#
# READ from `ledger_hyperparams.py`'s field rather than written here, because §5.2's "every
# constant comes from `ledger_hyperparams.json`" is a rule about where the number LIVES, and this
# one lived in three places: here, and twice as a literal `gate_k: 10` in §6.0's why-numbers
# (`home/shelves.py`), where it was the same quantity under a second spelling. One field, three
# readers, so the gate and the two printed numbers cannot drift apart. [M4.13 step 34d, dd14]
#
# It is NOT yet a path from the bundle to the gate, and this comment used to imply one ("a
# corpus-side re-tune reaches all of them or none"). `DEFAULTS` is the dataclass's own default
# instance, so this binds 10.0 in every process whatever `ledger_hyperparams.json` says, and
# `gate()` below is called as `gate(n_t)` with `k` defaulted. `hyperparams.py`'s
# `_PARSED_NOT_THREADED` names the gap and makes a bundle that re-tunes `gate_k` say so in its
# import report. [M4.13 cycle 2, M413-C2-DIM-HP-01]
EVIDENCE_K = DEFAULTS.gate_k

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
WARM_GATE = DEFAULTS.warm_gate
WARM_SUPPORT = EVIDENCE_K * WARM_GATE / (1.0 - WARM_GATE)   # 90, still derived from the two

ESource = Literal["backbone", "blended", "cold_tower", "none"]

BACKBONE_FILE = "backbone.npz"
_REQUIRED = ("title_ids", "E", "b_i", "item_n", "mu")

# A ROW OF E THAT IS NOT A COORDINATE. §4.3 lists the arrays this file ships and does not say
# that some of E's rows are placeholders — but the export does exactly that: on v20260828
# `cold_mask` is true on 2,879 of 14,397 rows, E is written as zeros for every one of them
# (max ||E|| = 9.4e-14) and their real coordinate is kept in `E_hat`/`b_hat`, two arrays §4.3
# never names. Read as a coordinate, a zero row is worse than an absent one: ⟨v_u, e(t)⟩ is
# exactly 0 for every user for ever, `item_n` is often large so the gate rounds to 1.0 and
# `e_source` says 'backbone', and §12's M2 criterion — which counts a title as coordinated
# whenever e_source is not 'none' — passes it. 1,915 of these clear `item_n >= WARM_SUPPORT`, so
# `placement.warm_title_ids` also excused them from the sweep that exists to place them. 1,915 and
# not the 1,918 with item_n >= 90: WARM_SUPPORT is computed rather than written and comes out one
# ulp above 90, so the three rows at exactly 90 fall on §5.1's BLEND side — the property
# `test_scoring.py`'s `edge` assertion pins, here and in `warm_title_ids`, which compares the same
# way. [M4.13 cycle 1, M413-REV-04]
#
# So a flagged row is treated as ABSENT, which is §5.1's gate → 0 limit and already written:
# "a title with no Backbone row has n_t = 0, so gate is exactly 0 and both terms collapse onto
# the Cold Tower's". Nothing substitutes the bundle's own `E_hat` into `E` — its scale is three
# orders off the Backbone's (median ||E_hat|| 27.05 over every row against a median ||E|| of
# 0.3835 over the rows that have a coordinate at all), which is a separate finding about the
# blend and not this one.
#
# WHAT THIS COSTS, SAID OUT LOUD. Excluding the row makes `support()` report 0 and `coordinate()`
# report gate 0 with `e_source = 'cold_tower'` for a title the crowd may have rated two hundred
# thousand times. Internally that is coherent — the gate weights a coordinate, and there is no
# coordinate to weight — but §8 stage 10's badge reads `e_source == 'cold_tower'` as "no crowd
# data yet", and after the sweep stamps them these titles wear it: 182 of the reference library's
# 839 owned titles, on top of the 130 `cs-62-sweep-badges-130-titles-cold` already counts. Decision
# 238 hands the repair to whichever milestone owns §8 stage 10's badge input, and this is named
# here rather than pre-empted: a title served at e(t) = 0 and called warm was the worse of the two,
# and it was invisible.
#
# WHAT THAT MILESTONE IS OWED IS NOT THE `title.placement` CHECK, which this paragraph used to
# say. cs-62's own 130 are titles that HAVE a Backbone row below WARM_SUPPORT, so their
# `e_source` is 'blended' and a fourth `title.placement` state would describe them; these 182
# have no row in the index at all, so their `e_source` is genuinely 'cold_tower', and the badge
# is decided off `e_source` and never off `placement` --
# `PosterCard.svelte`'s expression says so in as many words and
# `test_static_contracts.py::test_the_cold_badge_expression_reads_e_source_not_placement` freezes
# it character for character. So widening `title.placement` moves nothing for these rows, and the
# open question is a different one: how §8 stage 10 should describe a title the CROWD rated and
# the corpus did not place. A fourth `ESource` value (and the CHECK on `title_prior.e_source` in
# `0009_scoring.sql`, not the one on `title.placement` in `0003_content.sql`), or a flag beside
# it. Recorded as its own line so it is not absorbed into a repair that cannot perform it.
# [M4.13 cycle 2, M413-C2-DIM5-06]
COLD_MASK_ARRAY = "cold_mask"

# The fallback when no mask ships. Measured on v20260828: the largest flagged row's norm is
# 9.4e-14 and the smallest unflagged one's is 6.3e-5 — nine orders apart, so any epsilon inside
# that gap separates them and no real coordinate is anywhere near it.
COLD_ROW_NORM = 1e-9


class BackboneError(RuntimeError):
    """The basis is present but unusable. §3.1 makes an *absent* bundle legal; a corrupt one is
    not the same thing, and the caller decides whether to degrade or refuse."""


@contextmanager
def _reading(what: str) -> Iterator[None]:
    """Every read of `backbone.npz` fails as `BackboneError` and as nothing else.

    `app.py:256-260` catches `BackboneError` alone, logs it, and substitutes `Backbone.empty()`,
    with a comment that says why: a basis that will not load must degrade rather than stop "a
    boot the admin needs in order to fix the bundle". That guard was unreachable for the most
    likely corruption there is. `np.load` raises `zipfile.BadZipFile` for a truncated or
    half-copied archive (measured: a file cut at 50%, at 97% and at 2% all raise it) and
    `ValueError` for a file that is not an npz at all, neither of which is a `RuntimeError` — so
    an interrupted `docker cp` into `/data/artifacts` took the lifespan down and left the
    operator with no Data tab to re-import from.

    `EOFError` is the SAME failure at depth zero, and it is the first state every one of those
    copies passes through: `docker cp`, `scp` and a restore all create the destination and
    truncate it before they write a byte, so "half-copied" starts at nothing copied. It is not
    caught by the four above and it is not a `RuntimeError` either. `np.load` raises it from its
    own empty-magic check (`if not magic`) and ONLY at exactly zero bytes — one byte upward is
    already `BadZipFile` — which is why the truncation ladder that measured 2%, 50% and 97% never
    met it, and why `ArtifactStore.open` is no help: `present` is built with `.exists()`, and a
    zero-byte file exists. [M4.13 cycle 1, M413-R1-HP-02]

    `zlib.error` is the same failure at the one depth the four above still could not name, and it
    is the depth the SHIPPED file sits at: every member of the corpus's `backbone.npz` is
    DEFLATE-compressed (`compress_type 8` on all nine arrays of v20260828), so damage IN PLACE --
    a bad sector, a flaky SMB copy, a resumed transfer that leaves the file the right length --
    fails inside the decompressor before the CRC that would have made it a `BadZipFile` is ever
    computed. `zlib.error` subclasses `Exception` directly: not `OSError`, not `ValueError`, not
    `RuntimeError`. Measured by damaging the compressed payload at evenly spaced offsets, about
    one site in twenty escaped as 'invalid block type' / 'invalid literal/lengths set' / 'invalid
    distance too far back' while the rest landed as `BadZipFile: Bad CRC-32`, which was caught --
    so the guard covered the truncation ladder completely and the corpus's own compression mode
    only mostly. The registered test's corrupt-member case built its archive with
    `zipfile.ZipFile(path, "w")`, i.e. ZIP_STORED, which is the one mode the corpus does not ship
    and the one mode in which this cannot happen. [M4.13 cycle 2, M413-C2-DIM-BB-02]

    A context manager rather than one `try` around the whole loader because `NpzFile` re-reads
    the member on every subscript (see `Backbone`'s own docstring): the open, the `E_full`
    comparison and the cold-mask read are three separate reads of the file, any one of which can
    be the truncated member, and each says which it was. The shape and alignment checks inside
    raise `BackboneError` already, which this deliberately does not catch or re-wrap.
    [M4.13 step 32, finding 24]
    """
    try:
        yield
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, zlib.error) as exc:
        raise BackboneError(
            f"{BACKBONE_FILE} could not be read ({what}): {type(exc).__name__}: {exc}"
        ) from exc


def cold_row_mask(source: Any, n: int, e: np.ndarray | None = None) -> np.ndarray:
    """`bool[n]`: which rows of `backbone.npz` carry no coordinate at all.

    `source` is the opened `backbone.npz` — an `NpzFile`, which is what both callers hold and
    the only thing whose `.files` this consults. `e` lets a caller that already has E pass it in
    rather than have the 3.7 MB member re-parsed: `NpzFile` re-reads on every subscript.

    The shipped mask is the authority; the norm is the fallback for a bundle that predates it,
    and a bundle with neither has no cold rows to find.
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
    # §4.3's "per-title support counts", for EVERY row the file ships — cold-masked rows
    # included, which is what separates it from `row_of`. See `crowd_support` below: the two
    # indexes answer two different questions and this milestone is where they stopped being the
    # same answer. [M4.13 cycle 2, M413-C2-DIM5-01]
    support_of: dict[int, int] = field(default_factory=dict)
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
            e_full_shape = tuple(np.asarray(z["E_full"]).shape) if "E_full" in z.files else None

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
                f"mu has {mu_arr.size} entries; §5.1 uses it as the crowd's global mean (a "
                "scalar) — a per-item μ would change b(t)'s shrinkage target from a constant "
                "to a vector and must be settled before the first real bundle"
            )
        if n and not np.all(np.diff(title_ids) > 0):
            raise BackboneError(
                "title_ids is not strictly increasing; duplicate or unsorted ids make the "
                "row → title mapping ambiguous, which is a wrong film rather than an error"
            )

        notes: list[str] = []
        if e_full_shape is not None:
            # §4.3 names both E and E_full and never defines the difference. Every serving path
            # here uses E; E_full is recorded as provenance. If the prefix reading fails on a
            # real bundle we find out here rather than through a ranked list.
            with _reading("comparing E_full against E"):
                e_full = np.asarray(z["E_full"])
            if e_full.ndim != 2 or e_full.shape[0] != n or e_full.shape[1] < EMBED_DIM:
                notes.append(f"E_full has shape {e_full_shape}; ignored, E is used for scoring")
            elif not np.allclose(e_full[:, :EMBED_DIM], e, atol=1e-5):
                notes.append(
                    "E_full[:, :64] does not equal E; §4.3 never defines the difference, so E "
                    "is used for scoring and E_full is unread provenance"
                )

        # The flagged rows are excluded from the index rather than from the arrays: `row()`
        # returning None is the one statement every reader here already handles (§8 stage 10),
        # and it is what makes `coordinate()` take the pure Cold Tower limit for them.
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

    def crowd_support(self, title_id: int) -> int:
        """§4.3's "per-title support counts": how often the crowd rated this title, as shipped.

        THE TWO WERE ONE NUMBER UNTIL THIS MILESTONE, which is why this method has to exist and
        say so. §4.3 ships `item_n` and glosses it "(the §5.1 gate input)", so every reader was
        entitled to treat the crowd's rating count and the gate's n_t as the same quantity, and
        `support()` above served both. Excluding cold-masked rows from `row_of` (cs-01) split
        them for 2,879 rows of v20260828: a title the crowd has rated 260,131 times has no
        coordinate for the gate to weight, so its n_t is 0 — and its crowd support is still
        260,131.

        `support()` stays the gate's input -- the same `n_t` `coordinate()` computes inline, under
        the name the spec uses, which is why it survives with no production caller of its own.
        This is the other half, for the readers that want popularity rather than evidence: §6.1's
        P(seen) term in `rate/queue.py`, which weights `log1p(item_n)/log1p(1e5)` at 2.0 in the
        logit, and §8 stage 10's badge payload. Both read it out of `title_prior.item_n`, which
        `serve.materialise_priors` writes from here; `title_prior.gate` is the column that
        carries the gate's own answer, and it is 0 for these rows, so the row still says what
        happened. [M4.13 cycle 2, M413-C2-DIM5-01]
        """
        return int(self.support_of.get(int(title_id), 0))

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
    item_n: int              # n_t, the gate's input — 0 for a row this basis treats as absent
    e_source: ESource        # which limit of the blend this title landed in
    # §4.3's shipped support count, which is `item_n` for every row that has a coordinate and
    # the file's own figure for the cold-masked ones that do not. See `Backbone.crowd_support`.
    # Defaulted so the fixtures that build a Coordinate by hand keep the pre-split identity.
    # [M4.13 cycle 2, M413-C2-DIM5-01]
    crowd_n: int = 0


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
        crowd_n=backbone.crowd_support(title_id),
    )


# --- what the gate is actually weighting ----------------------------------------------------
# A MEASUREMENT, AND DELIBERATELY NOT A REPAIR. §5.1's middle line reads
# `e(t) = gate·E[t] + (1-gate)·ê(t)`, which only means "a blend" while the two halves are
# comparable quantities. On v20260828, over the 3,860 rows with a real Backbone row below
# WARM_SUPPORT — exactly the set the middle line exists for, and exactly the set `blend_ratios`
# below measures — median ||E|| is 0.0152 against median ||E_hat|| 20.47, and the weighted ratio
# ((1-g)·||ê||)/(g·||E||) runs p10 82.5, median 525.8, p90 5,498.9. The cold half therefore
# decides the personal term of every thin title, at every gate the spec's own k produces.
#
# EVERY POPULATION IN THAT SENTENCE IS NAMED, because the version it replaces got two of them
# wrong in the same breath and neither was visible. It read "3,846 rows ... below WARM_SUPPORT",
# which is the count at a literal cut of 90 — `WARM_SUPPORT` is computed and lands one ulp above
# it, so the 14 non-cold rows at exactly 90 are measured and the quantiles were a percent off the
# helper's own. And it read "median ||E|| over warm rows is 0.184", which was taken over
# `item_n >= 90` INCLUDING the 1,918 cold-masked rows whose E is all zeros — the rows this file
# spends forty lines arguing must be read as ABSENT. Over rows that actually carry a coordinate
# the warm median is 0.3835, not 0.184: a factor of two, not a percent. The cold-mask block above
# gets the same ulp right for its own 1,915-against-1,918; this got it wrong 350 lines later.
# [M4.13 cycle 2, M413-C2-DIM5-03]
#
# Nothing here rescales anything, and that is decision 236 rather than caution. §4.1 says the
# artifact is carried over verbatim, so whether E is meant to be unit-scale item factors or
# support-weighted is the CORPUS's contract — and the two candidate answers (normalise ê to E's
# median row norm; normalise both to unit norm) are different models of what e(t) means, not two
# spellings of one. The question goes upstream as a proposal; the app lands the measurement so
# that the answer arrives with a number behind it, and `coordinate`'s single blend expression
# above is left exactly as §5.1 writes it. `placement/reconcile.py`'s write is untouched for the
# same reason and a second one: `title_placement.e_hat` is read by the Ledger's fit and by §6.7's
# rail as well, so a scale applied at the write would move three readers at once.
# [M4.13 step 13, cs-02 / dd15, decision 236]


@dataclass(frozen=True)
class BlendReport:
    """The distribution of ((1-g)·||ê||)/(g·||E||), and what it could not measure.

    The three skip counts are not bookkeeping: each names a different reason a title has no
    ratio, and a report that folded them into one number could not tell "the blend is balanced"
    from "there was nothing to blend". A row at or above WARM_SUPPORT is §5.1's FIRST line (E
    outright, gate >= 0.9), a title with no row is its THIRD (gate exactly 0, both terms the
    Cold Tower's), and a degenerate pair has a zero on one side of the division.
    """

    n_offered: int                  # titles the caller handed over
    n_measured: int                 # the rows §5.1's middle line applies to
    n_warm: int                     # had a row at or above WARM_SUPPORT
    n_no_row: int                   # no Backbone row at all (or a cold-masked one)
    n_degenerate: int               # g == 0 or ||E|| == 0: no ratio exists
    ratios: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def quantile(self, q: float) -> float | None:
        """None rather than nan when nothing was measured: a percentile of an empty set is not a
        small number, and an exit criterion that prints it must say so."""
        if self.ratios.size == 0:
            return None
        return float(np.quantile(self.ratios, q))

    @property
    def p10(self) -> float | None:
        return self.quantile(0.10)

    @property
    def median(self) -> float | None:
        return self.quantile(0.50)

    @property
    def p90(self) -> float | None:
        return self.quantile(0.90)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offered": self.n_offered,
            "measured": self.n_measured,
            "warm": self.n_warm,
            "no_row": self.n_no_row,
            "degenerate": self.n_degenerate,
            "p10": self.p10,
            "median": self.median,
            "p90": self.p90,
        }


def blend_ratios(
    backbone: Backbone, placements: Mapping[int, tuple[np.ndarray, float]]
) -> BlendReport:
    """How far apart the two halves of §5.1's blend are, per title, over the rows it applies to.

    `placements` is `serve.placements()`'s shape — title_id -> (ê, b̂) — so the caller that
    already holds the basis and the Cold Tower's output can ask the question without a second
    read. Titles with no placement cannot be measured at all and are simply not offered.

    Reported and never acted on. See the block comment above: the rescaling is decision 236's
    upstream contract question, and `coordinate` is not to learn the answer here.
    """
    ratios: list[float] = []
    n_warm = n_no_row = n_degenerate = 0
    for title_id in sorted(placements):
        e_hat = np.asarray(placements[title_id][0], dtype=np.float64)
        row = backbone.row(int(title_id))
        if row is None:
            n_no_row += 1
            continue
        n_t = int(backbone.item_n[row])
        if n_t >= WARM_SUPPORT:
            n_warm += 1
            continue
        g = gate(n_t)
        warm_norm = float(np.linalg.norm(np.asarray(backbone.E[row], dtype=np.float64)))
        if g <= 0.0 or warm_norm <= 0.0:
            n_degenerate += 1
            continue
        ratios.append(((1.0 - g) * float(np.linalg.norm(e_hat))) / (g * warm_norm))
    return BlendReport(
        n_offered=len(placements),
        n_measured=len(ratios),
        n_warm=n_warm,
        n_no_row=n_no_row,
        n_degenerate=n_degenerate,
        ratios=np.asarray(ratios, dtype=np.float64),
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
