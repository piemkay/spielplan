"""The alias map reader (§8 stage 8). The fixture bundle ships only two alias rows, so most cases
insert their own; `alias_key` is `mdc/aspects/prompt.normalise` ported verbatim."""

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
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle" / "artifacts" / "dna_vocab" / "v1"


@pytest.fixture
async def loaded(db, vocab_dir):
    """Through the real loader, so the tests read what `_load_aliases` actually produced."""
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    assert report.ok, report.render()
    return db


async def _alias(conn, alias: str, term: str, *, kind: str | None = None, version: str = "v1"):
    await conn.execute(
        "INSERT INTO dna_alias (version, alias, term, kind) VALUES ($1, $2, $3, $4)",
        version, alias, term, kind,
    )


async def _second_vocabulary(conn) -> None:
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v2', 1, 1)"
    )
    await conn.execute("INSERT INTO dna_facet (version, facet, ord) VALUES ('v2', 'mood', 0)")
    await conn.execute(
        "INSERT INTO dna_term (version, term, facet) VALUES ('v2', 'mood.dread', 'mood')"
    )
    await _alias(conn, "dreadful", "mood.dread", version="v2")


@pytest.mark.parametrize(
    "spelling",
    ["slow-burn", "slow burn", "Slow-Burn", "The slow burn", "  slow burn.  ", "slow/burn"],
)
def test_every_spelling_the_key_exists_for_folds_to_one(spelling):
    """The app stores `raw_term` as the bundle spelled it,
    so every spelling of a keyword must reach the row."""
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
    """Separately, not one composite phrase: a composite passes as long as SOME rule fired."""
    assert alias_key(phrase) == expected


@pytest.mark.parametrize("phrase", ["long take", "long takes"])
def test_a_plural_is_left_alone(phrase):
    """Deliberately not collapsed: a stemming key would merge two authored rows with no evidence."""
    assert alias_key(phrase) == phrase


def test_a_phrase_of_pure_punctuation_folds_to_nothing():
    """`load_alias_map` must refuse the empty key, or it answers every keyword that folds to nothing."""
    assert alias_key(" -- ") == ""
    assert alias_key(None) == ""


@pytest.mark.parametrize("keyword", ["slow-burn", "slow burn", "Slow-Burn", "The slow burn"])
async def test_the_four_spellings_of_one_alias_resolve_to_one_term(loaded, keyword):
    """Both sides pass through `alias_key`: "a different
    normalisation here would silently drop most of the map"."""
    amap = await load_alias_map(loaded, "v1")

    assert amap[alias_key(keyword)] == ("pacing", "pacing.patient")


async def test_a_row_the_loader_wrote_with_no_kind_is_in_the_map(loaded):
    """NULL `kind` reads as "not known to be lexicon"; excluding NULL would empty pre-fill maps."""
    kinds = await loaded.fetch("SELECT alias, kind FROM dna_alias WHERE version = 'v1'")
    assert {r["alias"]: r["kind"] for r in kinds} == {"slow-burn": "alias", "cozy": "spelling"}
    await _alias(loaded, "slow burner", "pacing.patient", kind=None)

    amap = await load_alias_map(loaded, "v1")

    assert amap["slow burn"] == ("pacing", "pacing.patient")
    assert amap["cozy"] == ("mood", "mood.cosy")
    assert amap["slow burner"] == ("pacing", "pacing.patient")


async def test_a_lexicon_row_never_enters_the_map(loaded):
    """Without the skip the mood twins' keywords walk projected rows into the register facet."""
    await _alias(loaded, "pulpy", "register.deadpan", kind="lexicon")
    await _alias(loaded, "campy", "register.deadpan", kind="Lexicon")
    await _alias(loaded, "straight faced", "register.deadpan", kind="alias")

    amap = await load_alias_map(loaded, "v1")

    assert "pulpy" not in amap
    assert "campy" not in amap, "the column is hand-authored in a TSV; Lexicon is the same word"
    assert amap["straight faced"] == ("register", "register.deadpan")


async def test_a_row_whose_term_the_vocabulary_does_not_carry_is_skipped(loaded):
    """`0004_dna.sql` foreign-keys `version` alone, so the
    row is written and must be skipped at the read."""
    await _alias(loaded, "gritty", "mood.gritty")
    stored = await loaded.fetchval("SELECT count(*) FROM dna_alias WHERE term = 'mood.gritty'")
    assert stored == 1, "if this row were refused there would be no gap 3 to close"
    assert await loaded.fetchval("SELECT count(*) FROM dna_term WHERE term = 'mood.gritty'") == 0

    amap = await load_alias_map(loaded, "v1")

    assert "gritty" not in amap


async def test_a_term_is_a_raw_spelling_of_itself(loaded):
    """`dna_term` stores no labels or aliases, so the id is the one spelling this half contributes."""
    amap = await load_alias_map(loaded, "v1")

    assert amap["themes.obsession"] == ("themes", "themes.obsession")
    assert amap["pacing.relentless"] == ("pacing", "pacing.relentless")


async def test_an_explicit_map_row_beats_the_implicit_self_mapping(loaded):
    """`setdefault`, not assignment: an authored row is a decision and the default is a default."""
    await _alias(loaded, "themes.obsession", "themes.surveillance")

    amap = await load_alias_map(loaded, "v1")

    assert amap["themes.obsession"] == ("themes", "themes.surveillance")


async def test_a_register_term_gets_no_implicit_self_mapping(loaded):
    """Register's projected tier is built only from the explicit map; both halves asserted."""
    await _alias(loaded, "deadpan", "register.deadpan")

    amap = await load_alias_map(loaded, "v1")

    assert "register.deadpan" not in amap
    assert amap["deadpan"] == ("register", "register.deadpan")
    assert amap["mood.dread"] == ("mood", "mood.dread"), "other facets keep the implicit half"


async def test_an_install_with_no_vocabulary_reads_an_empty_map(db):
    assert await active_version(db) is None

    assert await load_alias_map(db) == {}


async def test_the_map_is_scoped_to_one_vocabulary_version(loaded):
    """§14 risk 7: every read is scoped to one version; a re-import can leave two rows."""
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
    """Last-write-wins sorts by the cluster's collation,
    which nothing pins, so the statement names its order."""
    # `slow-burn` is the row the bundle ships; all three spellings fold to one key.
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
