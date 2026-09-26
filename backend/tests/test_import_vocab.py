"""One derivation of the DNA vocabulary version (§4.3, decision 163)."""

from __future__ import annotations

from pathlib import Path

import pytest

from spielplan.importer import vocab


def _tree(root: Path, *versions: str, strays: tuple[str, ...] = ()) -> Path:
    """Returns the artifacts directory, which is what `version_of` takes."""
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
    assert vocab.version_of(_tree(tmp_path / "a", "v1", strays=("zz_notes.txt",))) == "v1"
    assert vocab.version_of(_tree(tmp_path / "b", strays=("zz_notes.txt", ".DS_Store"))) is None
    assert vocab.version_of(_tree(tmp_path / "c")) is None
    # No `dna_vocab/` at all: a models-only re-import need not carry one, so None rather than a raise.
    (tmp_path / "d" / "artifacts").mkdir(parents=True)
    assert vocab.version_of(tmp_path / "d" / "artifacts") is None


def test_v10_sorts_above_v2(tmp_path):
    """Lexicographic order is the trap ("v2" > "v10" as
    text); the refusal must name versions newest last."""
    artifacts = _tree(tmp_path, "v1", "v2", "v10")

    with pytest.raises(vocab.VocabularyError) as caught:
        vocab.version_of(artifacts)

    assert caught.value.versions == ("v1", "v2", "v10"), caught.value.versions
    assert caught.value.versions[-1] == "v10"
    assert "v1, v2, v10" in str(caught.value), str(caught.value)
    # Pinned so nobody "simplifies" the key away: plain sorting puts v2 last.
    assert sorted(p.name for p in (artifacts / "dna_vocab").iterdir())[-1] == "v2"


def test_more_than_one_version_directory_is_refused(tmp_path):
    """Decision 163: a vocabulary change is a migration, not an import."""
    with pytest.raises(vocab.VocabularyError) as caught:
        vocab.version_of(_tree(tmp_path / "two", "v1", "v2"))

    message = str(caught.value)
    assert "v1, v2" in message, message
    assert "decision 163" in message, message
    assert message.isascii(), message  # CLAUDE.md: this reaches a cp1252 console via the report

    assert vocab.version_of(_tree(tmp_path / "one", "v1")) == "v1"
