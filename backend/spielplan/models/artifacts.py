"""The artifact store. Spec v2.1 §4.3, §10.

Read-only files from the corpus project under `/data/artifacts/<bundle-version>/`, loaded at
boot **when present**. An empty store is legal (§3.1) — `ArtifactStore.empty()` is a first-class
value, not an error path, and every artifact-dependent surface asks `is_empty` rather than
catching an exception.

§10's invariant: *no process may score or refit with a loaded bundle version different from the
active row.* It is one comparison over two facts, and this module owns both halves -- the store
carries the version it loaded, `active_bundle_version` reads the row every other module used to
read for itself. Three production callers hold it: `worker._active_store`, through which every
model job the worker runs acquires its basis (§5.2's Ledger refit among them), and
`api/rate.py`/`api/rank.py`'s `_assert_active_basis`, where a mismatch is a 409 naming the restart
§10 asks for rather than a fit against a basis nobody serves. The sentence this paragraph replaces
claimed those callers while `grep -rn assert_matches` returned the definition, the claim itself,
one comment and six test lines. [M4.13, arch-03/tq1]

A BROKEN install is a second state and takes a second guard. An `artifact_bundle` row that is
active while `/data/artifacts/<version>` is absent used to load as the EMPTY store, so the worker
fitted every board from `zero_embeddings` under DEFAULTS and stamped the result with the very
version whose files are gone. `load_active` now carries that version with `broken = True`, which
repairs the stamp and in the same stroke makes `store.version == active_version` -- so the §10
invariant PASSES for a broken install. That is why `assert_not_broken` exists and is worded
separately: the refusal rests on the flag and never on the version comparison.

WHICH OF THE TWO IS ASKED FIRST IS PER CALLER, and the split is deliberate rather than drift --
the sentence here used to claim one order for all three, which the grep it was written from does
not show. `worker._active_store` asks `assert_not_broken` first because it RELOADS the store per
job, so both facts are fresh and the flag is the one that can see its own state. The two route
helpers ask it second, because their store was pinned once at boot (`app.py`) and is never
re-pinned: a process that is both broken on its outgoing version and stale against a new active
row is fixed by the restart §10 already asks for, and diagnosing it as a swap sends the operator
at the directory that exists rather than at the superseded one that does not. So the ORDER is
load-bearing on the request path and a reader must not level it. `is_empty` stays True either
way, so every artifact-dependent surface keeps rendering §3.1's no-bundle state instead of
raising. [M4.13, data-03; cycle 2, M413-C2-D1-02]
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger("spielplan.artifacts")

# §4.3 — the exhaustive file list. `required` files make a bundle loadable at all; the rest
# are reported as missing in the import report but do not block a load, because different
# milestones need different pieces (M0 needs the vocabulary and seed list; the Cold Tower is
# an M2 concern).
BUNDLE_FILES: dict[str, bool] = {
    # path relative to the bundle dir  ->  required
    "manifest.json": True,
    "backbone.npz": False,
    "cold_tower.pt": False,
    "feature_contract.json": False,
    "content_X.npz": False,
    "review_text_emb.npz": False,
    "ledger_hyperparams.json": False,
    # §14 risk 1's own mitigation is "expectations instrumented, not assumed", and these two are
    # the instrument the corpus already ships. `cold_eval.json` carries the held-out Spearman of
    # the cold path against the warm ceiling with confidence intervals - the only reference value
    # in the whole bundle for a number this app computes for itself (`user_vector.cv_rho`) and had
    # nothing to read against. `content_summary.json` states the corpus's own counts for the
    # blocks the feature contract declares. Neither was in this list, which §4.3 calls the
    # exhaustive one, so no surface could report them and the validator did not even miss them.
    # Optional like every other model file: a bundle without them is older, not broken.
    # [M4.13 step 35, cs-31]
    "cold_eval.json": False,
    "content_summary.json": False,
    "equating_map.json": False,
    "seed_list.json": False,
    "judgement_set_v1.tsv": False,
    "audit.json": False,
    "corrections_v1.tsv": False,
}

# §4.3: `dna_vocab/<version>/` — "vocabulary TSVs, alias map, S matrix, adjudications". Named
# here as the corpus ships them, which is not what this tuple said until M4.5: `terms.tsv`,
# `aliases.tsv` and `adjudications.tsv` are three files no exported bundle has ever contained,
# so every reader of this constant was addressing a vocabulary directory that does not exist.
#
# The version is inside the filename as well as in the directory name — `dna_vocab/v1/` holds
# `vocab_v1_all.tsv` — so this tuple is v1's file set rather than a template. Decision 163
# refuses a bundle whose vocabulary version differs from the active one, so there is exactly
# one version's file set to name until that migration is planned.
VOCAB_FILES = ("vocab_v1_all.tsv", "alias_map_v1.tsv", "s_matrix_v1.tsv", "adjudications_v1.tsv")


@dataclass(frozen=True)
class ColdEval:
    """`cold_eval.json` - what the corpus measured the cold path at, and the ceiling it is read
    against. Spec v2.1 §0 row 1, §14 risk 1.

    On v20260828: cold Spearman 0.35225 against a ceiling of 0.39193, with a CI on the tuned
    blend's delta over the prior. Those numbers are the yardstick for `user_vector.cv_rho`, which
    `scoring/foldin.py` computes per (user, kind) and which no surface could interpret: 0.41 is
    good and 0.21 is bad only relative to something, and this app carried nothing to be relative
    to. §0's own pipeline variance (0.003-0.008 Spearman) decides when a difference is neither -
    hence `read_against`, which applies it and answers "tie" rather than inventing a winner.

    The learning curve §0 row 1 also states is deliberately NOT here: it is a corpus measurement
    and belongs in the bundle rather than in Python, so it is an exporter ask (M4.13 step 35).
    What this class reads is only what the bundle already ships.
    """

    cold: float
    ceiling: float
    hybrid: float | None = None
    # The cold arm's tuned-blend-over-prior delta and its 95% interval, when the file carries
    # them. Carried rather than interpreted: an interval that straddles zero is the corpus saying
    # its own improvement is inside ITS noise, which is a different statement from §0's pipeline
    # floor and must not be collapsed into it.
    delta: float | None = None
    ci95: tuple[float, float] | None = None
    n_test: int | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> ColdEval | None:
        """Parse, or None. Never raises: a malformed yardstick must not take a boot or a shelf
        with it, and "this bundle ships no reference" is a state §3.1 already makes legal."""
        if not isinstance(raw, dict):
            return None
        cold, ceiling = _spearman(raw.get("cold")), _spearman(raw.get("ceiling"))
        if cold is None or ceiling is None:
            return None
        tuned = raw.get("cold:tunedblend_vs_prior")
        tuned = tuned if isinstance(tuned, dict) else {}
        ci = tuned.get("ci95")
        pair: tuple[float, float] | None = None
        if isinstance(ci, list | tuple) and len(ci) == 2 and all(_number(v) for v in ci):
            pair = (float(ci[0]), float(ci[1]))
        n_test = raw.get("n_test")
        return cls(
            cold=cold,
            ceiling=ceiling,
            hybrid=_spearman(raw.get("hybrid")),
            delta=float(tuned["delta"]) if _number(tuned.get("delta")) else None,
            ci95=pair,
            n_test=int(n_test) if isinstance(n_test, int) and not isinstance(n_test, bool) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cold": self.cold, "ceiling": self.ceiling, "hybrid": self.hybrid,
            "delta": self.delta, "ci95": list(self.ci95) if self.ci95 else None,
            "n_test": self.n_test,
        }

    def read_against(self, cv_rho: float | None, *, floor: float) -> dict[str, Any]:
        """One fitted `cv_rho`, read against this bundle's own figures.

        `floor` is §0's pipeline variance and is passed in rather than imported: this module is
        the one every caller already holds and has no dependency beyond asyncpg (see
        `active_bundle_version`), while the floor is a §5.2 constant and lives with the others in
        `ledger/hyperparams.py`. A caller that did not have to name it would be free to compare
        against no floor at all, which is the comparison this class exists to prevent - at §0's
        own variance a 0.004 lead is not a lead.
        """
        if cv_rho is None:
            return {**self.as_dict(), "cv_rho": None, "noise_floor": floor,
                    "vs_cold": None, "vs_ceiling": None, "reads": "not fitted"}
        rho = float(cv_rho)
        gap = rho - self.cold
        return {
            **self.as_dict(),
            "cv_rho": rho,
            "noise_floor": floor,
            "vs_cold": gap,
            "vs_ceiling": rho - self.ceiling,
            "reads": "tie" if abs(gap) <= floor else ("above cold" if gap > 0 else "below cold"),
        }

    def line(self, *, floor: float) -> str:
        """The boot line. ASCII only, like every other message this process prints."""
        interval = f", 95% CI [{self.ci95[0]:.4f}, {self.ci95[1]:.4f}]" if self.ci95 else ""
        n = f", n_test {self.n_test}" if self.n_test is not None else ""
        return (
            f"cold {self.cold:.5f} vs ceiling {self.ceiling:.5f}{interval}{n}; "
            f"a difference within {floor} is a tie"
        )


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _spearman(arm: Any) -> float | None:
    """One arm's held-out Spearman. The file nests it as `{"spearman": ..., "alpha": ...}`."""
    if isinstance(arm, dict) and _number(arm.get("spearman")):
        return float(arm["spearman"])
    return None


def cold_eval_of(store: Any) -> ColdEval | None:
    """The active bundle's yardstick, or None for every reason there might not be one.

    Takes a possibly-None, possibly-empty, possibly-broken store so the route that asks for it
    stays one line: §3.1's bundle-less install, data-03's broken one and a bundle older than
    `cold_eval.json` are all "no reference", and none of the three is an error.
    """
    if store is None or getattr(store, "is_empty", True):
        return None
    reader = getattr(store, "cold_eval", None)
    return reader() if callable(reader) else None


@dataclass
class ArtifactStore:
    version: str | None = None
    root: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    present: dict[str, bool] = field(default_factory=dict)
    vocab_version: str | None = None
    # §10 tells an operator what to do about a swap; it says nothing about a bundle whose files
    # have been deleted under a live install, which `load_active` below calls "a broken install,
    # not an empty one" and then had to report as something. A flag rather than a third store
    # class: every reader of this object already branches on `is_empty`, and a broken install has
    # to keep answering those readers the way an empty one does (§3.1) while the jobs that would
    # WRITE a fit refuse. [M4.13, data-03]
    broken: bool = False
    # BUNDLE.json — the bundle's own identity record, as `artifact_bundle.manifest` holds it.
    # It cannot be read from `root`: §10 stages only the bundle's `artifacts/` subtree, and
    # BUNDLE.json sits one level above it at the bundle root. So it arrives from the database
    # or not at all, and an empty dict is the honest value when nobody supplied it.
    identity: dict[str, Any] = field(default_factory=dict)

    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_empty(self) -> bool:
        """§3.1's question: is there a basis to score in?

        `broken` answers it the same way `version is None` does, and has to: the whole point of
        carrying a broken install's version is that `assert_matches` can no longer detect it, and
        a surface that started reading `is_empty` as False would then open files that are not
        there. What a broken install changes is the verdict of everything that FITS - the model
        jobs, and the five request entrypoints that fit after a write - never what a surface
        renders. [M4.13 cycle 1: the routes were the half with no caller]
        """
        return self.version is None or self.broken

    @classmethod
    def empty(cls) -> ArtifactStore:
        return cls()

    @classmethod
    def open(cls, root: Path, version: str, identity: dict[str, Any] | None = None) -> ArtifactStore:
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        present = {name: (root / name).exists() for name in BUNDLE_FILES}
        vocab_version = manifest.get("vocabulary_version")
        if vocab_version is None:
            vocab_dirs = sorted((root / "dna_vocab").glob("*")) if (root / "dna_vocab").is_dir() else []
            vocab_version = vocab_dirs[-1].name if vocab_dirs else None
        return cls(version=version, root=root, manifest=manifest, present=present,
                   vocab_version=vocab_version, identity=dict(identity or {}))

    @classmethod
    async def load_active(cls, conn: asyncpg.Connection, artifacts_dir: Path) -> ArtifactStore:
        """Load whatever `artifact_bundle` says is active.

        No active row => the EMPTY store, which is §3.1's legal household. An active row whose
        directory is absent => the BROKEN store: same `is_empty`, different verdict from the
        jobs, and the version carried so the two cases are distinguishable at all.
        """
        row = await conn.fetchrow(
            "SELECT version, manifest FROM artifact_bundle WHERE state = 'active' LIMIT 1"
        )
        if row is None:
            return cls.empty()
        root = artifacts_dir / row["version"]
        if not root.is_dir():
            # The DB says active but the files are gone - that is a broken install, not an
            # empty one. Report it loudly and keep serving the setup/admin surfaces.
            #
            # It used to return `empty()`, which threw away the two facts an operator and a job
            # both need: WHICH version is active, and WHERE its files were expected. The worker
            # mapped the empty store to None, `_ledger_map_refit` and `_tier_set_refits` fitted
            # every board from `zero_embeddings` under DEFAULTS, `prune=True` overwrote
            # `ledger_state`, `ledger_cutpoints` was rewritten, and `ledger_fit.bundle_version`
            # was stamped with this very version - so `load_cache` trusted the zero basis and
            # every unrated owned title sat at s = mu with one score, one tier and one badge.
            # The active row is deliberately NOT cleared: a restore needs to know what was active,
            # and §10 gives no process but the importer the right to move that row.
            # [M4.13, data-03]
            log.error(
                "artifact_bundle %s is active but %s does not exist - a broken install, not a "
                "bundle-less one: artifact-dependent surfaces render the no-bundle state and the "
                "model jobs refuse rather than fitting in a zero basis",
                row["version"], root,
            )
            return cls(version=row["version"], root=root, broken=True,
                       identity=_as_mapping(row["manifest"]))
        return cls.open(root, row["version"], identity=_as_mapping(row["manifest"]))

    def assert_matches(self, active_version: str | None) -> None:
        """§10: refuse to score or refit against a bundle other than the active one.

        Called by `worker._active_store` and by the Rate/Rank request path's
        `_assert_active_basis`. A bundle-less install passes it (None == None), which is §3.1
        working rather than a hole: there is no basis to be wrong about.
        """
        if self.version != active_version:
            raise RuntimeError(
                f"loaded bundle {self.version!r} != active bundle {active_version!r}; "
                "restart backend and worker after a bundle swap (§10 swap sequence)"
            )

    def assert_not_broken(self) -> None:
        """Refuse to fit in a basis whose files are gone. §10's invariant, second arm.

        Separate from `assert_matches` and not a clause inside it, because the two refusals have
        different truth conditions and only one of them can see this state: once `load_active`
        carries a broken install's version, `self.version == active_version` holds and the
        invariant is satisfied by a store that cannot produce a single coordinate. Asked BEFORE
        any embedding source is built, because `is_empty` is still True - so `Backbone.open`
        returns `empty()`, `load_for` caches that, and a job let past this point would fit at zero
        and stamp it with the active version. [M4.13, data-03]
        """
        if self.broken:
            raise RuntimeError(
                f"artifact_bundle {self.version!r} is active but {self.root} does not exist; "
                "restore the bundle directory or import it again before any fit - refusing "
                "rather than refitting every board in a zero basis (§10)"
            )

    def path(self, name: str) -> Path:
        # `broken` beside `root is None`, because a broken store DOES carry a root and it is the
        # directory that is missing. Without this clause a reader asking for a file gets a
        # `FileNotFoundError` naming a path it had no reason to expect, where every caller of this
        # method is written against "no artifact bundle loaded". [M4.13, data-03]
        if self.root is None or self.broken:
            raise RuntimeError("no artifact bundle loaded")
        return self.root / name

    def json(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            self._cache[name] = json.loads(self.path(name).read_text(encoding="utf-8"))
        return self._cache[name]

    def cold_eval(self) -> ColdEval | None:
        """`cold_eval.json`, parsed once per store. None when the bundle ships none.

        Cached through `_cache` like every other read here, the failure included: this is asked on
        the §6.0 Home path, and a bundle with a malformed yardstick would otherwise be re-parsed
        and re-warned about on every request. [M4.13 step 35]

        `ValueError` rather than `json.JSONDecodeError`, which is a subclass of it and therefore a
        strict narrowing of the same catch. The one it excluded is `UnicodeDecodeError`: `json()`
        reads with `encoding="utf-8"` and strict errors, so one cp1252 byte in a hand-edited note
        -- or a Windows editor's UTF-16 BOM -- raises before `json.loads` is ever called. Nothing
        upstream decodes this file (`importer/validate.py` only tests `(root / name).exists()`),
        so the first read is on a request, and this call is now on three of them: §6.0's Home
        payload, §6.6's Data tab through `summary()`, and `/api/config`, which is the
        unauthenticated shell bootstrap. An escape there is a 500 on the shell AND on the one
        surface the bundle could be replaced from, which is the failure shape data-03 exists to
        close. `validate.py:511` and `placement/contract.py` already pair the two exceptions this
        way. [M4.13 cycle 2, M413-C2-DIM-CE-03]
        """
        key = "cold_eval.parsed"
        if key not in self._cache:
            parsed: ColdEval | None = None
            if self.present.get("cold_eval.json"):
                try:
                    parsed = ColdEval.from_mapping(self.json("cold_eval.json"))
                except (OSError, ValueError, RuntimeError):
                    parsed = None
                if parsed is None:
                    log.warning(
                        "bundle %s ships cold_eval.json but it carries no cold and ceiling "
                        "Spearman; fold-in rho has no reference value in this install",
                        self.version,
                    )
            self._cache[key] = parsed
        return self._cache[key]

    def npz(self, name: str) -> Any:
        """Lazily memory-map an .npz. numpy is imported here so a bundle-less boot on a box
        without the scientific stack still starts."""
        if name not in self._cache:
            import numpy as np

            self._cache[name] = np.load(self.path(name), mmap_mode="r", allow_pickle=False)
        return self._cache[name]

    def missing_required(self) -> list[str]:
        return [n for n, required in BUNDLE_FILES.items() if required and not self.present.get(n)]

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            # §6.6's Data tab is where an operator finds out the directory under the active row is
            # gone; a log line at boot is not a surface. Reported from the store rather than
            # re-stat'ed by the route, so the page says what THIS process loaded - which is the
            # distinction the `loaded` / `active` split on that page exists to draw. [M4.13]
            "broken": self.broken,
            "missing_path": str(self.root) if self.broken else None,
            "vocabulary_version": self.vocab_version,
            "present": self.present,
            "missing_required": self.missing_required(),
            # The Data tab's "N titles". It used to read `title_count` out of
            # `artifacts/manifest.json`, which §4.3 defines as the fitted 3-class cut-points and
            # which has never carried such a key — so the panel has always rendered nothing.
            # BUNDLE.json's `tables` is a row count per shipped table and is where the corpus
            # states it. `owned` is gone rather than relocated: §7.2 makes ownership a Jellyfin
            # fact re-derived per install, so no bundle can know it.
            "titles": self.identity.get("tables", {}).get("title"),
            # §6.6's Data tab is where "expectations instrumented, not assumed" (§14 risk 1)
            # becomes something an operator can read: the bundle's own cold and ceiling Spearmans,
            # beside the list of files it shipped. None for a bundle that ships no
            # `cold_eval.json`, which is the honest answer and is not a zero. [M4.13 step 35]
            "cold_eval": cold_eval.as_dict() if (cold_eval := self.cold_eval()) else None,
        }


async def active_bundle_version(conn: asyncpg.Connection) -> str | None:
    """THE active bundle version. One resolver, because there were four.

    `app.py`'s boot pin, `refit.active_bundle_version`, `api/home.py`'s deliberate DB read and
    `api/tonight.py`'s own `_bundle_version` each answered this question for themselves, and in
    the window §10 opens between the flip and the restart two of them can answer it differently
    from the third.

    It lives here rather than in `ledger/refit.py` and `refit.active_bundle_version` delegates to
    it, not the other way round, because this module is the one every caller already imports and
    the one with no dependencies of its own beyond asyncpg: `refit` pulls in numpy and the whole
    Ledger, which neither `api/tonight.py` nor `ArtifactStore` should have to load in order to ask
    which bundle is active. The delegation also makes `load_cache`'s comparison and the §10 guard
    literally the same read. [M4.13, arch-03]
    """
    return await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")


def _as_mapping(value: Any) -> dict[str, Any]:
    """`artifact_bundle.manifest` is jsonb. The app's pool registers a json codec so it arrives
    decoded, but a connection without one hands back the raw text — and a store that then
    reported nothing would be indistinguishable from a bundle that shipped nothing."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}
