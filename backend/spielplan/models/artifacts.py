"""The artifact store (§4.3, §10): read-only bundle files, loaded when present; empty is legal (§3.1).
An active row whose files are gone loads BROKEN: `is_empty` stays True and fitting jobs refuse, via
`assert_not_broken`, since `assert_matches` passes such a store.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg

# The one vocabulary-version derivation; `importer/vocab.py` is stdlib-only, so importing it is cheap.
from spielplan.importer import vocab

log = logging.getLogger("spielplan.artifacts")

# §4.3's exhaustive file list -> required. Optional files are reported missing but never block a load.
BUNDLE_FILES: dict[str, bool] = {
    "manifest.json": True,
    "backbone.npz": False,
    "cold_tower.pt": False,
    "feature_contract.json": False,
    "content_X.npz": False,
    "review_text_emb.npz": False,
    "ledger_hyperparams.json": False,
    # The corpus's own yardsticks (§14 risk 1). Optional: a bundle without them is older, not broken.
    "cold_eval.json": False,
    "content_summary.json": False,
    "equating_map.json": False,
    "seed_list.json": False,
    "judgement_set_v1.tsv": False,
    "audit.json": False,
    "corrections_v1.tsv": False,
}

# §4.3's vocabulary files as the corpus ships them. The version is in each filename, so this is
# v1's set (decision 163 allows one version).
VOCAB_FILES = ("vocab_v1_all.tsv", "alias_map_v1.tsv", "s_matrix_v1.tsv", "adjudications_v1.tsv")


@dataclass
class ArtifactStore:
    version: str | None = None
    root: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    present: dict[str, bool] = field(default_factory=dict)
    vocab_version: str | None = None
    # Active row, files gone (§10): readers see `is_empty`, fitting jobs refuse.
    broken: bool = False
    # BUNDLE.json as `artifact_bundle.manifest` holds it: §10 stages only `artifacts/`, one level below.
    identity: dict[str, Any] = field(default_factory=dict)

    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_empty(self) -> bool:
        """§3.1's question. A broken store answers True too: it carries a version but no files."""
        return self.version is None or self.broken

    @classmethod
    def empty(cls) -> ArtifactStore:
        return cls()

    @classmethod
    def open(cls, root: Path, version: str, identity: dict[str, Any] | None = None) -> ArtifactStore:
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        present = {name: (root / name).exists() for name in BUNDLE_FILES}
        # `vocab.version_of` raises on a tree with two vocabularies rather than pick one (decision 256).
        vocab_version = vocab.version_of(root)
        return cls(version=version, root=root, manifest=manifest, present=present,
                   vocab_version=vocab_version, identity=dict(identity or {}))

    @classmethod
    async def load_active(cls, conn: asyncpg.Connection, artifacts_dir: Path) -> ArtifactStore:
        """No active row: EMPTY (§3.1). Active row, directory absent or unreadable: BROKEN, version kept."""
        row = await conn.fetchrow(
            "SELECT version, manifest FROM artifact_bundle WHERE state = 'active' LIMIT 1"
        )
        if row is None:
            return cls.empty()
        root = artifacts_dir / row["version"]
        if not root.is_dir():
            # Broken, not empty: the version is kept so no job fits in a zero basis and
            # stamps it. The active row stays: only the importer may move it (§10).
            log.error(
                "artifact_bundle %s is active but %s does not exist - a broken install, not a "
                "bundle-less one: artifact-dependent surfaces render the no-bundle state and the "
                "model jobs refuse rather than fitting in a zero basis",
                row["version"], root,
            )
            return cls(version=row["version"], root=root, broken=True,
                       identity=_as_mapping(row["manifest"]))
        try:
            return cls.open(root, row["version"], identity=_as_mapping(row["manifest"]))
        except vocab.VocabularyError:
            # Two vocabularies in the tree: the same BROKEN state, so the boot survives (decision 258).
            log.exception(
                "artifact_bundle %s is active and %s could not be read as a bundle - a broken "
                "install, not a bundle-less one: artifact-dependent surfaces render the "
                "no-bundle state and the model jobs refuse rather than fitting in a zero basis",
                row["version"], root,
            )
            return cls(version=row["version"], root=root, broken=True,
                       identity=_as_mapping(row["manifest"]))

    def assert_matches(self, active_version: str | None) -> None:
        """§10: refuse to score or refit against a bundle other than the active one. None == None passes."""
        if self.version != active_version:
            raise RuntimeError(
                f"loaded bundle {self.version!r} != active bundle {active_version!r}; the backend "
                "loads the active bundle within seconds, and if it cannot, restart backend and "
                "worker (section 10 swap sequence, decision 497)"
            )

    def assert_not_broken(self) -> None:
        """Asked separately: a broken store carries the active version, so `assert_matches` passes it."""
        if self.broken:
            raise RuntimeError(
                f"artifact_bundle {self.version!r} is active but {self.root} does not exist; "
                "restore the bundle directory or import it again before any fit - refusing "
                "rather than refitting every board in a zero basis (§10)"
            )

    def path(self, name: str) -> Path:
        # A broken store carries a root whose directory is missing.
        if self.root is None or self.broken:
            raise RuntimeError("no artifact bundle loaded")
        return self.root / name

    def json(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            self._cache[name] = json.loads(self.path(name).read_text(encoding="utf-8"))
        return self._cache[name]

    def npz(self, name: str) -> Any:
        """numpy is imported lazily so a bundle-less boot without the scientific stack still starts."""
        if name not in self._cache:
            import numpy as np

            self._cache[name] = np.load(self.path(name), mmap_mode="r", allow_pickle=False)
        return self._cache[name]

    def missing_required(self) -> list[str]:
        return [n for n, required in BUNDLE_FILES.items() if required and not self.present.get(n)]

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            # What THIS process loaded, for §6.6's Data tab.
            "broken": self.broken,
            "missing_path": str(self.root) if self.broken else None,
            "vocabulary_version": self.vocab_version,
            "present": self.present,
            "missing_required": self.missing_required(),
            # BUNDLE.json's per-table row counts; `artifacts/manifest.json` never carried a title count.
            "titles": self.identity.get("tables", {}).get("title"),
        }


async def active_bundle_version(conn: asyncpg.Connection) -> str | None:
    """The one active-version resolver; here because this module imports nothing heavy."""
    return await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")


async def active_bundle_key(conn: asyncpg.Connection) -> tuple[str, Any] | None:
    """(version, activated_at): the version alone cannot see a restage (decision 497)."""
    row = await conn.fetchrow(
        "SELECT version, activated_at FROM artifact_bundle WHERE state = 'active'"
    )
    return None if row is None else (row["version"], row["activated_at"])


def _as_mapping(value: Any) -> dict[str, Any]:
    """jsonb arrives decoded through the pool's codec, but as raw text on a codec-less connection."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}
