"""The DNA vocabulary version a bundle carries, derived once. Spec v2.1 §4.3; decisions 163, 256.

§4.3 names the vocabulary by the directory it ships in — `dna_vocab/<version>/`, holding the
"vocabulary TSVs, alias map, S matrix, adjudications" — and by no key anywhere else. That is why
this module exists at all: the version is a fact about the TREE, and four readers each derived it
for themselves and disagreed about the same tree.

What the four were, measured at M4.14's open:

  * `bundle._vocabulary_version` preferred a `BUNDLE.json` `vocabulary_version` key and fell back
    to `sorted(p.name for p in vocab.iterdir() if p.is_dir())[-1]`;
  * `validate.py` repeated that fallback inline, a hundred lines away from the first copy;
  * `ArtifactStore.open` preferred an `artifacts/manifest.json` key and fell back to
    `sorted((root / "dna_vocab").glob("*"))[-1].name` — which includes FILES;
  * `refuse_on_install_state` read `dna_vocabulary.imported_at`, which is the INSTALL's version
    and not the bundle's at all.

Probed in this worktree before the repair: with `v1`, `v2` and `v10` staged, both bundle-side
readers picked `v2`, because `sorted` is lexicographic and "v2" > "v10" as text. Drop a
`zz_notes.txt` beside them and the store reported `vocab_version = 'zz_notes.txt'` while the
bundle reader still said `v2`. One tree, two answers — and the answer is what decision 163's
refusal is computed from.

Confirmed against the real corpus export `v20260828`: its sixteen top-level `BUNDLE.json` keys
carry no vocabulary key at all. So every real bundle rests on this directory listing rather than
on a declaration, which makes the listing's rules load-bearing rather than a fallback nobody
reaches. Decision 256 turns the absent declaration into a refusal rather than a default, and it
refuses on what this function returns.

The three rules, and what each one is for:

  * DIRECTORIES ONLY. §4.3's `<version>` is a directory. A file in `dna_vocab/` is a stray — an
    editor backup, a `.DS_Store`, an operator's notes — and naming one as the vocabulary is how
    the store came to report `zz_notes.txt` as a version of anything.
  * NATURAL ORDER, so `v10` sorts above `v2`. Nothing picks from the set any more, because more
    than one is refused outright; but the refusal NAMES the set, and it names it newest last,
    because "keep the newest" is what the operator does next and lexicographic order is exactly
    the trap that made all three listing readers above wrong.
  * MORE THAN ONE IS REFUSED. Decision 163 makes a vocabulary change a migration rather than an
    import, so exactly one version's file set may be present — `models/artifacts.py`'s
    `VOCAB_FILES` comment says so in as many words ("there is exactly one version's file set to
    name until that migration is planned") while the code directly beneath it silently picked one
    of however many it found. A tree carrying two vocabularies has no answer to give, and
    inventing one is the silent catastrophe decision 163 exists to convert into a readable
    refusal.

It refuses by RAISING rather than by returning a sentinel. The two bundle-side callers sit inside
report-producing code and turn `VocabularyError` into the `report.fail` an operator reads in the
Data tab; `ArtifactStore.open`'s caller does not, and that is the caller a sentinel would hurt —
`None` there is indistinguishable from §3.1's legal bundle-less install, so the one context with
no report to write is the one where the exception has to carry the sentence itself.
"""

from __future__ import annotations

import re
from pathlib import Path

# The trailing digit run, split off so `v10` orders above `v2`. Two lines rather than a natsort
# dependency: the only names this ever sees are the `dna_vocab/<version>/` directories the corpus
# writes, `v1` and `v10` shaped, and a third-party ordering would be one more pin to carry for one
# comparison that surfaces in a refusal message.
_TRAILING_DIGITS = re.compile(r"^(.*?)(\d*)$")


def _natural_key(name: str) -> tuple[str, int]:
    head, digits = _TRAILING_DIGITS.match(name).groups()  # the pattern matches every string
    # -1 for a name with no trailing digits, so `vocab` orders below `vocab1` rather than beside
    # it. Nothing in a corpus bundle looks like that; the key still has to be total, because the
    # set it orders is whatever an operator's filesystem happens to hold.
    return (head, int(digits) if digits else -1)


class VocabularyError(RuntimeError):
    """Two or more `dna_vocab/<version>/` directories in one artifacts tree (decision 163).

    Carries the versions in natural order so a caller that writes a report can name them the way
    the message does, without re-deriving the ordering that is this module's whole point.
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

    `None` is a legal answer twice over, and both cases are §3.1's rather than errors: a bundle
    that ships no `dna_vocab/` tree at all (under decision 162 the only recurring bundle is
    models-only, which need not carry one) and a tree holding no version directory. What the
    CALLER does with the `None` is decision 256's business — on an install that already has a
    vocabulary it is a refusal, because decision 163's comparison cannot otherwise be made — and
    this function stays the one place that says what the tree holds.

    Raises `VocabularyError` when the tree holds more than one version.
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
