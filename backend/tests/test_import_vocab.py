"""One derivation of the DNA vocabulary version. Spec v2.1 §4.3; decisions 163, 256.

No database and no bundle fixture: `version_of` reads a directory tree and returns a name, so
these tests build the exact trees the four old derivations disagreed about. The disagreement was
measured, not imagined — with `v1`, `v2` and `v10` staged both bundle-side readers picked `v2`,
and with a stray `zz_notes.txt` beside them `ArtifactStore.open` reported `'zz_notes.txt'` as the
vocabulary version while the bundle reader said `v2`. Decision 163's refusal is computed from
that answer, which is why one tree returning two answers is a data-rules defect and not a tidiness
one. [M4.14, imp-vocabulary-version-derived-four-ways / cs-36]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spielplan.importer import vocab


def _tree(root: Path, *versions: str, strays: tuple[str, ...] = ()) -> Path:
    """An `artifacts/` directory holding `dna_vocab/` with these version directories and strays.

    Returns the artifacts directory, because that is what `version_of` takes: the vocabulary is
    `dna_vocab/<version>/` UNDER the bundle's artifacts tree (§4.3), which is also the root the
    staged `/data/artifacts/<bundle-version>/` tree is.
    """
    artifacts = root / "artifacts"
    vocab_dir = artifacts / "dna_vocab"
    vocab_dir.mkdir(parents=True, exist_ok=True)
    for version in versions:
        (vocab_dir / version).mkdir()
        (vocab_dir / version / f"vocab_{version}_all.tsv").write_text("term\n", encoding="utf-8")
    for stray in strays:
        (vocab_dir / stray).write_text("not a vocabulary\n", encoding="utf-8")
    return artifacts


def test_only_directories_count_and_a_stray_file_is_ignored(tmp_path):
    """§4.3 names a directory, `dna_vocab/<version>/`, so only directories can name a version.

    The store's `sorted(glob("*"))[-1].name` did not filter, and an operator's notes file sorting
    last therefore became the version the §10 invariant was checked against. A stray is ignored,
    never returned — and a `dna_vocab/` holding nothing but strays is the same tree as one holding
    nothing, which §3.1 makes legal rather than an error.
    """
    assert vocab.version_of(_tree(tmp_path / "a", "v1", strays=("zz_notes.txt",))) == "v1"
    assert vocab.version_of(_tree(tmp_path / "b", strays=("zz_notes.txt", ".DS_Store"))) is None
    assert vocab.version_of(_tree(tmp_path / "c")) is None
    # No `dna_vocab/` at all: a models-only re-import need not carry one (decision 162), so this
    # is the common case rather than the odd one, and it is None rather than a raise.
    (tmp_path / "d" / "artifacts").mkdir(parents=True)
    assert vocab.version_of(tmp_path / "d" / "artifacts") is None


def test_v10_sorts_above_v2(tmp_path):
    """Lexicographic order is the trap that made three readers wrong: "v2" > "v10" as text.

    Nothing picks from the set any more — three versions is decision 163's refusal — but the
    refusal names the set, and it has to name it newest LAST, because "keep the newest and
    re-import" is the operator's next move and the old ordering would have pointed at v2.
    """
    artifacts = _tree(tmp_path, "v1", "v2", "v10")

    with pytest.raises(vocab.VocabularyError) as caught:
        vocab.version_of(artifacts)

    assert caught.value.versions == ("v1", "v2", "v10"), caught.value.versions
    assert caught.value.versions[-1] == "v10"
    assert "v1, v2, v10" in str(caught.value), str(caught.value)
    # The measured fact the key exists for, pinned so nobody "simplifies" the key away: plain
    # sorting puts v2 last, which is what both bundle-side readers returned for this exact tree.
    assert sorted(p.name for p in (artifacts / "dna_vocab").iterdir())[-1] == "v2"


def test_more_than_one_version_directory_is_refused(tmp_path):
    """Decision 163: a vocabulary change is a migration, not an import.

    So exactly one version's file set may be present, as `models/artifacts.py`'s `VOCAB_FILES`
    comment already said while the code beneath it picked one of several without saying so. Two
    vocabularies in one tree is a tree with no answer to give; the refusal names both and the
    section, because the operator has to choose which one the install is on.
    """
    with pytest.raises(vocab.VocabularyError) as caught:
        vocab.version_of(_tree(tmp_path / "two", "v1", "v2"))

    message = str(caught.value)
    assert "v1, v2" in message, message
    assert "decision 163" in message, message
    assert message.isascii(), message  # CLAUDE.md: this reaches a cp1252 console via the report

    # The boundary: one directory is the whole contract, and it answers rather than raising.
    assert vocab.version_of(_tree(tmp_path / "one", "v1")) == "v1"
