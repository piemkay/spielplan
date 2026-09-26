"""The DNA vocabulary version a bundle carries, derived once from `dna_vocab/<version>/` (§4.3).

Directories only, natural order (`v10` > `v2`), and more than one raises `VocabularyError`
(decision 163), since `ArtifactStore.open`'s caller has no report to write into.
"""

from __future__ import annotations

import re
from pathlib import Path

# Split off the trailing digits so `v10` orders above `v2`.
_TRAILING_DIGITS = re.compile(r"^(.*?)(\d*)$")


def _natural_key(name: str) -> tuple[str, int]:
    head, digits = _TRAILING_DIGITS.match(name).groups()  # the pattern matches every string
    # -1 so `vocab` orders below `vocab1`; the key must be total.
    return (head, int(digits) if digits else -1)


class VocabularyError(RuntimeError):
    """Two or more `dna_vocab/<version>/` directories in one artifacts tree (decision 163).

    Carries the versions in natural order.
    """

    def __init__(self, vocab_dir: Path, versions: tuple[str, ...]) -> None:
        self.vocab_dir = vocab_dir
        self.versions = versions
        super().__init__(
            f"{vocab_dir} holds {len(versions)} vocabulary versions ({', '.join(versions)}); "
            "decision 163 allows exactly one, so this bundle's vocabulary cannot be named"
        )


def version_of(artifacts_dir: Path) -> str | None:
    """The vocabulary version of the bundle whose `artifacts/` tree is `artifacts_dir`.

    `None` when there is no tree or no version directory (legal, §3.1). Raises `VocabularyError`
    when the tree holds more than one version.
    """
    vocab_dir = artifacts_dir / "dna_vocab"
    if not vocab_dir.is_dir():
        return None
    versions = sorted((p.name for p in vocab_dir.iterdir() if p.is_dir()), key=_natural_key)
    if not versions:
        return None
    if len(versions) > 1:
        raise VocabularyError(vocab_dir, tuple(versions))
    return versions[0]
