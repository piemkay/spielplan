"""The artifact store. Spec v2.1 §4.3, §10.

Read-only files from the corpus project under `/data/artifacts/<bundle-version>/`, loaded at
boot **when present**. An empty store is legal (§3.1) — `ArtifactStore.empty()` is a first-class
value, not an error path, and every artifact-dependent surface asks `is_empty` rather than
catching an exception.

§10 invariant, enforced by `load_active`: *no process may score or refit with a loaded bundle
version different from the active row.* The store therefore carries the version it loaded and
`assert_matches` is called by the scoring/refit entrypoints.
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
    "equating_map.json": False,
    "seed_list.json": False,
    "judgement_set_v1.tsv": False,
    "audit.json": False,
    "corrections_v1.tsv": False,
}

# §4.3: `dna_vocab/<version>/` — vocabulary TSVs, alias map, S matrix, adjudications, and
# (§6.4) the authored axis definitions, one TSV per facet.
VOCAB_FILES = ("terms.tsv", "aliases.tsv", "adjudications.tsv")


@dataclass
class ArtifactStore:
    version: str | None = None
    root: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    present: dict[str, bool] = field(default_factory=dict)
    vocab_version: str | None = None

    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_empty(self) -> bool:
        return self.version is None

    @classmethod
    def empty(cls) -> ArtifactStore:
        return cls()

    @classmethod
    def open(cls, root: Path, version: str) -> ArtifactStore:
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        present = {name: (root / name).exists() for name in BUNDLE_FILES}
        vocab_version = manifest.get("vocabulary_version")
        if vocab_version is None:
            vocab_dirs = sorted((root / "dna_vocab").glob("*")) if (root / "dna_vocab").is_dir() else []
            vocab_version = vocab_dirs[-1].name if vocab_dirs else None
        return cls(version=version, root=root, manifest=manifest, present=present,
                   vocab_version=vocab_version)

    @classmethod
    async def load_active(cls, conn: asyncpg.Connection, artifacts_dir: Path) -> ArtifactStore:
        """Load whatever `artifact_bundle` says is active. Missing => the empty store."""
        row = await conn.fetchrow(
            "SELECT version FROM artifact_bundle WHERE state = 'active' LIMIT 1"
        )
        if row is None:
            return cls.empty()
        root = artifacts_dir / row["version"]
        if not root.is_dir():
            # The DB says active but the files are gone — that is a broken install, not an
            # empty one. Report it loudly and keep serving the setup/admin surfaces.
            log.error(
                "artifact_bundle %s is active but %s does not exist — "
                "artifact-dependent surfaces will render the no-bundle state",
                row["version"], root,
            )
            return cls.empty()
        return cls.open(root, row["version"])

    def assert_matches(self, active_version: str | None) -> None:
        """§10: refuse to score or refit against a bundle other than the active one."""
        if self.version != active_version:
            raise RuntimeError(
                f"loaded bundle {self.version!r} != active bundle {active_version!r}; "
                "restart backend and worker after a bundle swap (§10 swap sequence)"
            )

    def path(self, name: str) -> Path:
        if self.root is None:
            raise RuntimeError("no artifact bundle loaded")
        return self.root / name

    def json(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            self._cache[name] = json.loads(self.path(name).read_text(encoding="utf-8"))
        return self._cache[name]

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
            "vocabulary_version": self.vocab_version,
            "present": self.present,
            "missing_required": self.missing_required(),
            "titles": self.manifest.get("title_count"),
            "owned": self.manifest.get("owned_count"),
        }
