"""The alias map, read: the reader `dna_alias` has never had. Spec v2.1 §8 stage 8, §4.1 rule 1.

One row lives here: `data-rules-the-projected-tier-reads-the-alias-map-the-way-it-was-built`.
§8 stage 8 is a "per-title alias-map projection of its keywords", and until M5.4 the map had a
loader and no reader at all -- `grep -rn dna_alias backend/spielplan/` returned
`importer/dna.py` and `backup/movie_data.py` and nothing else. A table nothing reads is a table
whose fidelity gaps nothing has had to notice, and `spielplan/dna/aliases.py` closes three of
them at the read: a `kind='lexicon'` row never projects, the key is the normalised raw term, and
a row whose vocabulary term the active vocabulary does not carry is skipped.

WHY SO MANY ROWS ARE INSERTED HERE RATHER THAN READ OFF THE BUNDLE. Stated as a measurement
rather than skipped around (decision 391). The fixture bundle's `alias_map_v1.tsv` ships exactly
two rows -- `slow-burn -> pacing.patient` (kind `alias`) and `cozy -> mood.cosy` (kind
`spelling`) -- and `importer/dna._load_aliases` reads `raw_term` and `vocab_term` only, so BOTH
arrive in `dna_alias` with `kind` NULL. There is no lexicon row in the bundle and there cannot
be one in the table until the loader fills the column, which decision 383 records as owed to the
milestone that owns `importer/dna.py`. So the two rows the loader really writes carry the
NULL-`kind` case and the four-spellings case, and every other case inserts its own row. The
inserts are not a convenience: the row whose term the vocabulary does not carry exists to prove
that `dna_alias` accepts it, which is the whole of gap 3 -- `0004_dna.sql:42-47` foreign-keys
`version` alone, and `importer/dna.py:207-208`'s comment says the opposite.

The no-database half at the top tests `alias_key` directly, because the port's fidelity is the
claim: it is `mdc/aspects/prompt.normalise` verbatim under a different name, and every collapse
its docstring names is asserted here, along with the one it deliberately does not make.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spielplan.db.dna_terms import active_version
from spielplan.dna.aliases import alias_key, load_alias_map
from spielplan.importer import dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx


@pytest.fixture
def vocab_dir(tmp_path) -> Path:
    """`dna_vocab/v1/` as the corpus ships it, written by the shared bundle fixture."""
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle" / "artifacts" / "dna_vocab" / "v1"


@pytest.fixture
async def loaded(db, vocab_dir):
    """The bundle's vocabulary, loaded through the real loader rather than inserted.

    The rows this reader is wrong or right about are the rows the importer writes, so the tests
    below read what `_load_aliases` actually produced -- `kind` NULL included -- instead of a
    hand-built table that would agree with whatever the reader expected.
    """
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    assert report.ok, report.render()
    return db


async def _alias(conn, alias: str, term: str, *, kind: str | None = None, version: str = "v1"):
    """One `dna_alias` row, written the way only a loader that stored `kind` could write it."""
    await conn.execute(
        "INSERT INTO dna_alias (version, alias, term, kind) VALUES ($1, $2, $3, $4)",
        version, alias, term, kind,
    )


async def _second_vocabulary(conn) -> None:
    """A second imported vocabulary, minimal but real: version, facet, term and one alias."""
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v2', 1, 1)"
    )
    await conn.execute("INSERT INTO dna_facet (version, facet, ord) VALUES ('v2', 'mood', 0)")
    await conn.execute(
        "INSERT INTO dna_term (version, term, facet) VALUES ('v2', 'mood.dread', 'mood')"
    )
    await _alias(conn, "dreadful", "mood.dread", version="v2")


# --- the key itself, ported from the corpus (no database) -------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["slow-burn", "slow burn", "Slow-Burn", "The slow burn", "  slow burn.  ", "slow/burn"],
)
def test_every_spelling_the_key_exists_for_folds_to_one(spelling):
    """The four shapes §8 stage 8 needs, plus the two the corpus's own docstring names.

    This is the function's entire reason to exist: the app stores `raw_term` exactly as the
    bundle spelled it, so a keyword differing from the stored alias by case, a hyphen, a slash,
    surrounding punctuation or a leading article has to reach the same row, or the map silently
    answers nothing for it.
    """
    assert alias_key(spelling) == "slow burn"


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("SLOW BURN", "slow burn"),                          # case
        ("slow   burn", "slow burn"),                        # whitespace collapse
        ("slow\nburn", "slow burn"),                         # ... across a newline, not a run
        ("(slow burn)", "slow burn"),                        # surrounding punctuation
        ('"slow burn"', "slow burn"),                        # ... in each class the port lists
        ("slow burn;", "slow burn"),
        ("an unreliable narrator", "unreliable narrator"),   # a leading article, all three of
        ("a slow burn", "slow burn"),                        # which the corpus strips
        ("the slow burn", "slow burn"),
        ("director\u2019s cut", "director's cut"),           # U+2019, which sources disagree on
    ],
)
def test_the_key_collapses_exactly_what_the_corpus_docstring_names(phrase, expected):
    """Case, whitespace, surrounding punctuation, hyphen-vs-space and a leading article.

    Each collapse asserted separately rather than through one composite phrase: a composite
    passes as long as SOME rule fired, and the failure this guards against is one rule quietly
    dropping out of a port.
    """
    assert alias_key(phrase) == expected


@pytest.mark.parametrize("phrase", ["long take", "long takes"])
def test_a_plural_is_left_alone(phrase):
    """Deliberately not collapsed: the corpus sends both phrases on as two, and so does this.

    The corpus has a clustering pass that decides plurals later and this app has none, which
    makes this the most tempting line in the port to improve. It stays: a key that stemmed would
    merge two authored rows onto one term with no evidence left to argue about the merge, and
    the alias map is a lookup table rather than a place where synonymy is decided.
    """
    assert alias_key(phrase) == phrase


def test_a_phrase_of_pure_punctuation_folds_to_nothing():
    """The empty key is the one value `load_alias_map` must refuse to store.

    `_PUNCT` strips the whole string here, and a map that kept `""` as a key would answer every
    keyword that folds to nothing with whichever term happened to write that key last.
    """
    assert alias_key(" -- ") == ""
    assert alias_key(None) == ""


# --- the map, against the rows the loader really writes (Postgres) ----------------------


@pytest.mark.parametrize("keyword", ["slow-burn", "slow burn", "Slow-Burn", "The slow burn"])
async def test_the_four_spellings_of_one_alias_resolve_to_one_term(loaded, keyword):
    """The bundle stores `slow-burn`; a keyword arrives spelled however its source spelled it.

    Both sides pass through `alias_key`, which is the contract the corpus's docstring states as
    a warning -- "Using a different normalisation here would silently drop most of the map" --
    and which this app has to keep twice over, because it stores `raw_term` verbatim while the
    corpus read a file it had already normalised on the way in.
    """
    amap = await load_alias_map(loaded, "v1")

    assert amap[alias_key(keyword)] == ("pacing", "pacing.patient")


async def test_a_row_the_loader_wrote_with_no_kind_is_in_the_map(loaded):
    """NULL `kind` reads as "not known to be lexicon", and that is every shipped row today.

    The measurement rather than the assumption: the bundle's map file carries a `kind` column
    with `alias` and `spelling` in it, `_load_aliases` reads two columns and drops the rest, so
    both rows land NULL. A reader that treated NULL as "exclude" would empty the map on every
    install in existence, which is what getting decision 383's default backwards costs.
    """
    kinds = await loaded.fetch("SELECT alias, kind FROM dna_alias WHERE version = 'v1'")
    assert {r["alias"]: r["kind"] for r in kinds} == {"slow-burn": None, "cozy": None}

    amap = await load_alias_map(loaded, "v1")

    assert amap["slow burn"] == ("pacing", "pacing.patient")
    assert amap["cozy"] == ("mood", "mood.cosy")


async def test_a_lexicon_row_never_enters_the_map(loaded):
    """Extraction-lexicon and query-bridge rows are not projection sources, and never project.

    The corpus's measured reason, carried into `aliases.py`: without the skip the mood twins'
    keyword surfaces walk projected rows into the register facet through the back door -- Django
    and Hostel both inheriting `register.pulp`, cos 0.894. The third row is what makes this a
    test of `kind` and of nothing else about these three: all three name the same term and
    differ only in that column.
    """
    await _alias(loaded, "pulpy", "register.deadpan", kind="lexicon")
    await _alias(loaded, "campy", "register.deadpan", kind="Lexicon")
    await _alias(loaded, "straight faced", "register.deadpan", kind="alias")

    amap = await load_alias_map(loaded, "v1")

    assert "pulpy" not in amap
    assert "campy" not in amap, "the column is hand-authored in a TSV; Lexicon is the same word"
    assert amap["straight faced"] == ("register", "register.deadpan")


async def test_a_row_whose_term_the_vocabulary_does_not_carry_is_skipped(loaded):
    """Gap 3, and the insert is half the test: it proves `dna_alias` accepts the row.

    `importer/dna.py:207-208` says an unadopted term is "a constraint violation mid-transaction
    rather than a row" because `dna_alias.term` is NOT NULL. NOT NULL is a statement about the
    absence of a value, not about the existence of a term, and `0004_dna.sql:42-47` foreign-keys
    `version` alone -- so the row below is written, not rejected, and the comment is wrong. Left
    unskipped it would project a `dna_projected` row whose `facet` joins no `dna_facet`, the
    defect M4.9 finding 1 measured at 206,151 of 223,136 projected rows.
    """
    await _alias(loaded, "gritty", "mood.gritty")
    stored = await loaded.fetchval("SELECT count(*) FROM dna_alias WHERE term = 'mood.gritty'")
    assert stored == 1, "if this row were refused there would be no gap 3 to close"
    assert await loaded.fetchval("SELECT count(*) FROM dna_term WHERE term = 'mood.gritty'") == 0

    amap = await load_alias_map(loaded, "v1")

    assert "gritty" not in amap


async def test_a_term_is_a_raw_spelling_of_itself(loaded):
    """The map's second half: a keyword that is already a vocabulary term id maps to it.

    The corpus adds each term's label and its authored aliases here too; this app's `dna_term`
    stores neither on purpose (`importer/dna.py:143-146`), so the id is the one spelling this
    half can contribute and `alias_map_v1.tsv`'s own rows carry the others.
    """
    amap = await load_alias_map(loaded, "v1")

    assert amap["themes.obsession"] == ("themes", "themes.obsession")
    assert amap["pacing.relentless"] == ("pacing", "pacing.relentless")


async def test_an_explicit_map_row_beats_the_implicit_self_mapping(loaded):
    """`setdefault`, not assignment: an authored row is a decision and the default is a default.

    The direction matters and is invisible until the two disagree, which is why the row below
    aims one term id at a different term rather than at itself.
    """
    await _alias(loaded, "themes.obsession", "themes.surveillance")

    amap = await load_alias_map(loaded, "v1")

    assert amap["themes.obsession"] == ("themes", "themes.surveillance")


async def test_a_register_term_gets_no_implicit_self_mapping(loaded):
    """Rule 2 of the register proposal's migration protocol: register's projected tier is built
    only from the explicit projection map.

    Both halves asserted, because the skip alone would also be satisfied by a reader that had
    dropped the facet entirely: the implicit self-mapping is absent AND an authored row onto the
    same term still projects. The facet is named by this app's id (`register`) rather than the
    corpus's extraction label (`register_audience`) -- M4.9 finding 1.
    """
    await _alias(loaded, "deadpan", "register.deadpan")

    amap = await load_alias_map(loaded, "v1")

    assert "register.deadpan" not in amap
    assert amap["deadpan"] == ("register", "register.deadpan")
    assert amap["mood.dread"] == ("mood", "mood.dread"), "other facets keep the implicit half"


async def test_an_install_with_no_vocabulary_reads_an_empty_map(db):
    """M0 and the pre-seed state. `active_version` answers None there, and that is not an error.

    §8 stage 8 runs against an install that may not have imported a bundle yet, and a reader
    that raised would turn a state the app is designed to pass through into a job failure.
    """
    assert await active_version(db) is None

    assert await load_alias_map(db) == {}


async def test_the_map_is_scoped_to_one_vocabulary_version(loaded):
    """§14 risk 7: every read is scoped to one version, which the caller may leave to
    `db.dna_terms.active_version` but never to a literal.

    Checked rather than assumed: `dna_vocabulary` is a table, a re-import can leave two rows in
    it, and a map that mixed them would project terms from a vocabulary that no facet, axis or
    palette on any surface is keyed to.
    """
    await _second_vocabulary(loaded)

    v1 = await load_alias_map(loaded, "v1")
    v2 = await load_alias_map(loaded, "v2")
    active = await load_alias_map(loaded)

    assert "slow burn" in v1 and "dreadful" not in v1
    assert "dreadful" in v2 and "slow burn" not in v2
    assert active == v2, "the version left unnamed is the active one, not the first one"


async def test_two_spellings_that_fold_to_one_key_are_resolved_here_and_not_by_the_cluster(
    loaded,
):
    """The map is last-write-wins, so where two raw spellings fold to one key the winner IS the
    sort order -- and the sort order of a `text` column is the cluster's collation, which nothing
    in `docker-compose.yml` pins.

    Two installs holding byte-identical `dna_alias` rows would then derive different
    `dna_projected` rows, and README's Recovery path -- restore into a rebuilt box -- is how one
    install becomes the other. The three spellings below sort as "slow burn", "slow-burn",
    "Slow Burn" under `en_US.utf8` and as "Slow Burn", "slow burn", "slow-burn" under `C`, so the
    map's answer differed by locale until the statement said which order it meant.
    """
    # `slow-burn` -> `pacing.patient` is the row the bundle ships; these two are the spellings
    # a second authoring pass adds, and all three fold to one key.
    await _alias(loaded, "Slow Burn", "mood.dread")
    await _alias(loaded, "slow burn", "mood.cosy")

    under_c = [r["alias"] for r in await loaded.fetch(
        'SELECT alias FROM dna_alias WHERE version = $1 AND alias ILIKE $2 '
        'ORDER BY alias COLLATE "C"', "v1", "slow%burn",
    )]
    under_cluster = [r["alias"] for r in await loaded.fetch(
        "SELECT alias FROM dna_alias WHERE version = $1 AND alias ILIKE $2 ORDER BY alias",
        "v1", "slow%burn",
    )]
    assert under_c != under_cluster, (
        "this cluster sorts the two collations alike, so the assertion below proves nothing "
        "here -- the statement must still name one, because the next cluster will not"
    )

    amap = await load_alias_map(loaded, "v1")
    assert amap[alias_key("slow burn")] == ("pacing", "pacing.patient"), (
        "the map answered with whichever row the cluster's own locale happened to sort last"
    )
