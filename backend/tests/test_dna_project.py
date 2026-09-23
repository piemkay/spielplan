"""§8 stage 8's per-title projection, against a real Postgres. Spec v2.1 §8 stage 8, §5.3, §4.1
rules 1 and 2, §14 risk 7; decisions 163, 188, 384, 385, 387.

Almost every test here is an integration test, and that is forced rather than preferred. What
this milestone adds is one derivation written across two tables the app already ships and a third
it writes into: the keywords come from `title_keyword`, the map from `dna_alias` joined to
`dna_term`, the version from the row `db/dna_terms.active_version` picks, and the write lands on
`UNIQUE (title_id, version, term)`. A unit test over a dict built in Python would agree with
whatever the dict was built from and would have nothing to say about any of them -- least of all
about idempotence, which is a claim about what a second write does to a row that already exists.

**Three of these tests exist because a strict port would have got them wrong**, and each names
the cost in its own docstring: the weight is a COUNT and not the corpus's agreement ladder
(decision 385), the re-run is an upsert and not a delete-and-rewrite, and the cap the corpus
tuned is not ported at all (decision 384). The first of those is the one that fails silently:
0.3 in that column is a number the read layer accepts, renders and ranks by, and the only
symptom is that acquired titles quietly lose to bundle titles on every surface that reads DNA.

**The fixture bundle cannot exercise this at all, and that is measured rather than skipped.** Its
four keywords are `heist`, `investigation`, `family` and `cooking`; its alias map ships two rows,
`slow-burn` and `cozy`. The two sets are disjoint, so the bundle as shipped projects nothing
through this function, and the first test asserts exactly that against the real loader rather
than leaving it as a remark. Every other test inserts its own rows.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import sqlite3
import time
from pathlib import Path

import pytest

from spielplan.db import dna_terms
from spielplan.dna import project as project_module
from spielplan.dna.project import DEFAULT_SOURCES, project_title
from spielplan.importer import dna as importer_dna
from spielplan.importer.report import ImportReport
from spielplan.models.artifacts import VOCAB_FILES
from tests.fixtures import make_bundle as fx

MODULE = Path(project_module.__file__)

VOCAB = "v1"

# Five terms over four facets -- enough that a wrong facet is visible as a wrong facet rather
# than as the only one there is, and few enough that a test can name every row it expects.
TERMS = (
    "pacing.patient",
    "mood.cosy",
    "mood.dread",
    "themes.obsession",
    "register.deadpan",
)

# `origin = 'acquired'` and an id at or above 1e9 (`0008_placement.sql:46-48`, §4.1's minting
# rule) -- the population §8 stage 8 actually runs over, and the one the bundle's own projected
# rows can never collide with.
ACQUIRED = 1_000_000_007


# --- the fixtures ----------------------------------------------------------------------


async def _vocabulary(db, *, version: str = VOCAB, terms=TERMS) -> None:
    facets = sorted({term.split(".", 1)[0] for term in terms})
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, $3)",
        version, len(facets), len(terms),
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(version, facet, i) for i, facet in enumerate(facets)],
    )
    await db.executemany(
        # The facet is the term's own prefix, which is `importer/dna.app_facet`'s rule and the
        # value the loader stores. A fixture that wrote the corpus's extraction label here would
        # be testing the projection against the defect M4.9 finding 1 measured rather than
        # against the vocabulary. [M4.9 finding 1]
        "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3)",
        [(version, term, term.split(".", 1)[0]) for term in terms],
    )


async def _alias(db, alias: str, term: str, *, version: str = VOCAB, kind: str | None = None):
    await db.execute(
        "INSERT INTO dna_alias (version, alias, term, kind) VALUES ($1, $2, $3, $4)",
        version, alias, term, kind,
    )


async def _title(db, title_id: int, name: str, *, origin: str = "bundle") -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned, origin) VALUES ($1, 'movie', $2, true, $3)",
        title_id, name, origin,
    )


async def _keywords(db, title_id: int, pairs) -> None:
    await db.executemany(
        "INSERT INTO title_keyword (title_id, keyword, source) VALUES ($1, $2, $3)",
        [(title_id, keyword, source) for keyword, source in pairs],
    )


def _docstrings(tree: ast.Module) -> set[int]:
    """The `id()` of every docstring constant in `tree`, so a guard can read the code alone.

    This package argues its decisions in prose, at length and by name, so a guard written over
    the file's text reports the argument for a rule as a breach of it. Same shape as
    `test_landmine_guards.py:422-432`, which excludes docstrings for the same reason.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


async def _projected(db, title_id: int):
    return await db.fetch(
        "SELECT id, term, facet, weight, via, created_at FROM dna_projected"
        " WHERE title_id = $1 ORDER BY term",
        title_id,
    )


# --- the bundle as shipped, measured ---------------------------------------------------


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


async def test_the_shipped_fixture_projects_nothing_and_that_is_the_measurement(db, bundle_dir):
    """The bundle this repository ships cannot exercise stage 8, and the number is zero.

    Decision 391's rule one facet over: measure what this install actually produces and state it,
    rather than asserting around the gap. `alias_map_v1.tsv` ships two rows (`slow-burn` ->
    `pacing.patient`, `cozy` -> `mood.cosy`) and `title_keyword` ships four keywords (`heist`,
    `investigation`, `family`, `cooking`), one per title, all from `tmdb`. The two sets are
    disjoint and the map's second half cannot rescue them either: the implicit self-mapping keys
    on the whole term id, so `themes.obsession` is reachable as "themes.obsession" and never as
    "obsession".

    That is not a defect in the fixture. It is a fixture built to exercise the IMPORTER, whose
    `dna_projected` rows arrive already computed out of `content.sqlite`; this function is the
    first code in the app that derives one. So the real paths below insert their own rows, and
    this test exists so that a reader who finds them all synthetic knows why.
    """
    report = ImportReport()
    # Stamped `acquired` because stage 8 projects nothing else and refuses a bundle title outright
    # (the refusal test below): the zero measured here is the alias map's, and a title the
    # function refuses would measure the refusal instead. [M5.4 review cycle 3, M54-DIM5-04]
    await db.executemany(
        "INSERT INTO title (id, kind, name, origin) VALUES ($1, $2, $3, 'acquired')",
        [(t[0], t[1], t[2]) for t in fx.TITLES],
    )
    await importer_dna.load_vocabulary(
        db, bundle_dir / "artifacts" / "dna_vocab" / VOCAB, VOCAB, report
    )
    assert report.ok, report.render()
    await db.executemany(
        "INSERT INTO title_keyword (title_id, source, keyword) VALUES ($1, $2, $3)", fx.KEYWORDS
    )

    written = [await project_title(db, title_id) for title_id, _, _ in fx.KEYWORDS]

    assert written == [0, 0, 0, 0]
    assert await db.fetchval("SELECT count(*) FROM dna_projected") == 0

    # The measurement itself, so that a fixture which later grew an overlapping row fails HERE
    # rather than quietly turning the zero above into a passing accident.
    aliases = set(await db.fetchval("SELECT array_agg(alias) FROM dna_alias WHERE version = $1",
                                    VOCAB))
    keywords = {keyword for _, _, keyword in fx.KEYWORDS}
    assert aliases == {"slow-burn", "cozy"}
    assert keywords == {"heist", "investigation", "family", "cooking"}
    assert aliases & keywords == set()


# --- the inventories, and the column that is deliberately not read ----------------------


def test_the_inventories_the_projection_reads_are_the_corpus_list():
    """`mdc/dna/project.py:78-79`'s tuple, ported whole. Three independent pass-0 LLM memories,
    the relevance-thresholded MovieLens genome and its free user tags, MPST's 71-term controlled
    set, and the two crawled keyword inventories.

    `title_aspect` is the one that has to be absent: it is ~30% complete and its phrases are the
    material the vocabulary was curated FROM, so projecting them back would be close to circular.
    Asserted as an absence rather than left to the comment, because the shape of a later mistake
    here is somebody adding the richest available inventory to a list that reads like a
    convenience.
    """
    assert DEFAULT_SOURCES == (
        "llm:gemini37", "llm:sonnet5", "llm:terra",
        "movielens", "movielens_tags", "mpst", "tmdb", "wikidata",
    )
    assert "title_aspect" not in DEFAULT_SOURCES
    # The fixture's own keyword source is in the list, so the zero measured above is a statement
    # about the alias map and not about a source filter quietly eating every row.
    assert {source for _, source, _ in fx.KEYWORDS} <= set(DEFAULT_SOURCES)


async def test_the_keyword_table_carries_no_weight_to_fold_in(db):
    """The corpus's "what is deliberately not used" is the `weight` column on `title_keyword`,
    and on this app that column does not exist to be used.

    `mdc/dna/project.py:47-51` refuses it because MovieLens genome relevance and user-tag counts
    both encode popularity, and genome relevance correlates rho ~= 0.51 with per-title tag count:
    folding it in imports a popularity confound into a representation meant to describe the film.
    The bundle's own `title_keyword` DOES ship it (`fixtures/make_bundle.py:418-420`, `weight
    REAL DEFAULT 1.0`) and `importer/load.py:156-159` maps three columns out of four, so the
    refusal was already taken twice before this function existed.

    Pinned against the live schema rather than against the migration text, because the thing that
    would re-open it is a later migration widening the table -- at which point this fails and the
    milestone that widened it has to say what it means to do with the number.
    """
    columns = set(await db.fetchval(
        "SELECT array_agg(column_name::text) FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'title_keyword'"
    ))
    assert columns == {"title_id", "keyword", "source"}


# --- what a keyword that maps produces --------------------------------------------------


async def test_a_keyword_that_matches_an_alias_row_projects_one_row(db):
    """The whole derivation in one row: the term the map names, the facet `dna_term` carries, the
    inventory that said it, and a count of one."""
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])

    assert await project_title(db, ACQUIRED) == 1

    rows = await _projected(db, ACQUIRED)
    assert len(rows) == 1
    assert rows[0]["term"] == "pacing.patient"
    assert rows[0]["facet"] == "pacing"
    assert rows[0]["via"] == "keyword:slow-burn"
    assert rows[0]["weight"] == pytest.approx(1.0)


async def test_a_keyword_spelled_differently_from_the_map_row_still_projects(db):
    """Gap 2, from the other side. The app stores `raw_term` exactly as the bundle spelled it, so
    the incoming keyword and the stored alias both have to pass through `alias_key` -- normalising
    one side only is the silent drop `load_alias_map`'s docstring warns about, one step later.

    Four spellings of one phrase, which is the set `alias_key`'s own docstring promises to
    collapse: case, the hyphen/space swap, surrounding punctuation and a leading article.
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    for offset, spelling in enumerate(("Slow-Burn", "slow burn", " slow-burn. ", "The slow burn")):
        title_id = ACQUIRED + offset
        await _title(db, title_id, f"title {offset}", origin="acquired")
        await _keywords(db, title_id, [(spelling, "tmdb")])

        assert await project_title(db, title_id) == 1, spelling

        rows = await _projected(db, title_id)
        assert [row["term"] for row in rows] == ["pacing.patient"], spelling


async def test_three_inventories_naming_one_term_make_one_row_carrying_all_three(db):
    """A projected tag's only quality signal is how many INDEPENDENT inventories named it, so the
    weight is a count of inventories and the row is per term.

    `via` is the other question and the other column: the KEYWORDS that produced the row, which
    is what `0004_dna.sql:111` declares it to hold and what the bundle's rows carry -- one
    vocabulary of provenance across both populations, rather than one per producer (decision
    393). Three inventories, three spellings, one row.
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _alias(db, "unhurried", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [
        ("slow-burn", "tmdb"), ("unhurried", "wikidata"), ("slow burn", "movielens"),
    ])

    assert await project_title(db, ACQUIRED) == 1

    rows = await _projected(db, ACQUIRED)
    assert len(rows) == 1
    assert rows[0]["weight"] == pytest.approx(3.0)
    assert rows[0]["via"] == "keyword:slow burn, keyword:slow-burn, keyword:unhurried"


async def test_two_keywords_from_one_inventory_are_one_source(db):
    """The count is of inventories, not of rows. TMDB naming a term twice is one opinion said
    twice, and counting it as two would make the weight a measure of how talkative a crawl is --
    which is the popularity confound the `title_keyword.weight` paragraph refuses, arriving by
    the other door."""
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _alias(db, "unhurried", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb"), ("unhurried", "tmdb")])

    assert await project_title(db, ACQUIRED) == 1

    rows = await _projected(db, ACQUIRED)
    assert rows[0]["weight"] == pytest.approx(1.0)
    assert rows[0]["via"] == "keyword:slow-burn, keyword:unhurried", (
        "the WEIGHT is the count of inventories and `via` is the spellings that produced the "
        "row, so one talkative inventory raises the second and never the first"
    )


@pytest.mark.parametrize(
    "inventories, ladder",
    [(("tmdb",), 0.3), (("tmdb", "wikidata"), 0.6), (("tmdb", "wikidata", "mpst"), 1.0)],
)
async def test_the_weight_is_the_count_and_never_the_corpus_agreement_ladder(
    db, inventories, ladder
):
    """Decision 385, and the one place a faithful port would have been the bug.

    `mdc/dna/project.py:82-89` maps 1/2/3+ inventories onto 0.3/0.6/1.0 so a projected tag sits on
    the same 0..1 scale as a salience. This app does not: `importer/dna.load_projected` writes the
    bundle's raw `n_sources` into `dna_projected.weight`, decision 188 keeps it raw, and
    `db/dna_terms.TERM_WEIGHT` bounds it at read time instead. Writing the ladder here would put
    acquired titles and bundle titles on different scales through the one expression both are read
    by, which is M4.9 finding 20 reopened through a new path -- and it would be invisible, because
    0.3 is a number that column accepts.

    The ladder value is asserted as an inequality on purpose: the single-inventory case is the one
    where a wrong port is nearly right (1.0 against 0.3), and an equality against the correct
    value alone would pass on a row that happened to be one either way.
    """
    await _vocabulary(db)
    for i in range(len(inventories)):
        await _alias(db, f"alias-{i}", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [(f"alias-{i}", s) for i, s in enumerate(inventories)])

    await project_title(db, ACQUIRED)

    rows = await _projected(db, ACQUIRED)
    assert rows[0]["weight"] == pytest.approx(float(len(inventories)))
    assert rows[0]["weight"] != pytest.approx(ladder)


async def test_the_read_layer_reads_a_projected_row_of_this_kind_in_its_documented_band(db):
    """The same claim one layer out, where it is a fact about a surface rather than about a column.

    `db/dna_terms.TERM_WEIGHT` documents the bands the saturating form produces: extracted
    0.733..1.00 for salience 1..3, projected 0.15..0.267 for a count of 1..8. Read through the
    sanctioned view, a row this function writes for three inventories has to land at 0.225. The
    corpus's ladder would have put the same row at 0.30 * (1.0 / 2.0) = 0.15 -- inside the band,
    indistinguishable, and one third of the evidence. A single inventory would land at 0.0692,
    BELOW the band the module documents as its floor, which is the case a reader could have
    caught and nobody would have looked for.
    """
    await _vocabulary(db)
    for i in range(3):
        await _alias(db, f"alias-{i}", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [
        ("alias-0", "tmdb"), ("alias-1", "wikidata"), ("alias-2", "mpst"),
    ])
    await project_title(db, ACQUIRED)

    read = await db.fetchval(
        f"SELECT {dna_terms.TERM_WEIGHT} FROM dna_tagged d"
        " WHERE d.title_id = $1 AND d.term = $2 AND d.tier = 'projected'",
        ACQUIRED, "pacing.patient",
    )
    assert read == pytest.approx(0.225, abs=1e-6)


# --- what never projects ----------------------------------------------------------------


async def test_a_lexicon_alias_row_never_projects(db):
    """Exit check 12. `kind='lexicon'` rows are extraction-lexicon and query-bridge entries: the
    register migration files them so the mood twins' keyword surfaces cannot walk projected rows
    into the register facet through the back door (measured: Django and Hostel both inheriting
    `register.pulp`, cos 0.894).

    The contrast is the test: a second alias row onto the SAME term with `kind` absent projects in
    the same run, so this asserts the column and not the keyword, the term or the map."""
    await _vocabulary(db)
    await _alias(db, "wisecracking", "register.deadpan", kind="lexicon")
    await _alias(db, "straight-faced", "register.deadpan")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("wisecracking", "tmdb")])

    assert await project_title(db, ACQUIRED) == 0
    assert await _projected(db, ACQUIRED) == []

    await _keywords(db, ACQUIRED, [("straight-faced", "wikidata")])
    assert await project_title(db, ACQUIRED) == 1
    rows = await _projected(db, ACQUIRED)
    assert [(row["term"], row["via"]) for row in rows] == [
        ("register.deadpan", "keyword:straight-faced"),
    ]


async def test_a_row_whose_vocabulary_term_is_absent_never_projects(db):
    """Gap 3. `dna_alias` has no foreign key from `term` to `dna_term` (`0004_dna.sql:42-47`
    foreign-keys `version` alone), so the table holds mappings onto terms the vocabulary never
    adopted, and `importer/dna.py:207-208`'s comment says the opposite.

    The INSERT succeeding is asserted BEFORE the absence, because an absence assertion alone
    passes just as green on an install where the row was refused -- and then gap 3 would be
    closed against a table that never held the row it is about. A projected row onto a term no
    facet declares is the defect M4.9 finding 1 measured at 206,151 of 223,136 rows: its facet
    joins nothing, so §6.8's palette gives it no colour and every per-facet aggregate misses it.
    """
    await _vocabulary(db)
    await _alias(db, "robotic", "themes.robots")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("robotic", "tmdb")])

    assert await db.fetchval(
        "SELECT count(*) FROM dna_alias WHERE version = $1 AND term = $2", VOCAB, "themes.robots"
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM dna_term WHERE version = $1 AND term = $2", VOCAB, "themes.robots"
    ) == 0

    assert await project_title(db, ACQUIRED) == 0
    assert await _projected(db, ACQUIRED) == []


async def test_a_keyword_that_maps_to_nothing_produces_no_row_and_no_error(db):
    """~49% of the corpus's source rows map to nothing, so the unmapped keyword is the common
    case and not the edge one. It is silent by design: the map is a whitelist of spellings
    somebody adjudicated, and a keyword outside it is an inventory this vocabulary has no term
    for, which is §8.4's naming-failure queue's subject and not this function's."""
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("time travel", "tmdb"), ("black and white", "wikidata")])

    assert await project_title(db, ACQUIRED) == 0
    assert await _projected(db, ACQUIRED) == []


async def test_a_keyword_from_an_inventory_the_projection_does_not_read_is_ignored(db):
    """`title_aspect` is the inventory the corpus names and refuses; the rule is the list, so any
    source outside it is not read. The same keyword from `tmdb` projects in the same test, which
    is what makes this about the source column."""
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "title_aspect")])

    assert await project_title(db, ACQUIRED) == 0

    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])
    assert await project_title(db, ACQUIRED) == 1


async def test_an_install_with_no_vocabulary_projects_nothing(db):
    """§3.1 makes a bundle-less app a legal state and `active_version` answers None there. A
    projection that raised on it would turn first boot into an error report about a tier nobody
    has fed yet; `dna_projected.version` has a foreign key to `dna_vocabulary`, so there is also
    no version it could invent."""
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])

    assert await dna_terms.active_version(db) is None
    assert await project_title(db, ACQUIRED) == 0
    assert await db.fetchval("SELECT count(*) FROM dna_projected") == 0


async def test_the_projection_is_scoped_to_one_vocabulary_version(db):
    """§14 risk 7: "every read is scoped to one version". Decision 163 means this app creates no
    second vocabulary, and the risk's own argument is that it must still survive one -- a
    projection computed half in one basis and half in another is the silent catastrophe that
    paragraph describes.

    Two vocabularies, the same alias spelling mapping to a different term in each. The default
    resolves through `db/dna_terms.active_version` and writes the newer basis; naming the older
    one explicitly writes that one, and neither run can see the other's rows.
    """
    await _vocabulary(db, version="v1", terms=("pacing.patient",))
    await _vocabulary(db, version="v2", terms=("mood.dread",))
    await _alias(db, "slow-burn", "pacing.patient", version="v1")
    await _alias(db, "slow-burn", "mood.dread", version="v2")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])

    assert await dna_terms.active_version(db) == "v2"
    assert await project_title(db, ACQUIRED) == 1
    assert await project_title(db, ACQUIRED, version="v1") == 1

    rows = await db.fetch(
        "SELECT version, term FROM dna_projected WHERE title_id = $1 ORDER BY version", ACQUIRED
    )
    assert [(row["version"], row["term"]) for row in rows] == [
        ("v1", "pacing.patient"), ("v2", "mood.dread"),
    ]


# --- idempotence and isolation ----------------------------------------------------------


async def test_re_running_the_projection_for_one_title_changes_nothing(db):
    """Exit check 11, and the reason the write is an upsert rather than a delete-and-rewrite.

    "Changes nothing" is asserted as a fact about the ROWS and not only about the values: the
    same `id`, the same `created_at`, the same weight and the same `via`. A delete-and-rewrite
    would satisfy a values-only assertion while renumbering every row and re-stamping every
    timestamp -- and §6.6's reject review and §8's audit trail both read "when did this claim
    first appear", which a sweep that re-stamps answers wrongly and confidently.
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _alias(db, "cozy", "mood.cosy")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [
        ("slow-burn", "tmdb"), ("cozy", "tmdb"), ("cozy", "movielens"),
    ])

    assert await project_title(db, ACQUIRED) == 2
    before = [dict(row) for row in await _projected(db, ACQUIRED)]

    assert await project_title(db, ACQUIRED) == 2
    after = [dict(row) for row in await _projected(db, ACQUIRED)]

    assert before == after
    assert [row["id"] for row in before] == [row["id"] for row in after]
    assert [row["created_at"] for row in before] == [row["created_at"] for row in after]
    assert [row["weight"] for row in after] == [pytest.approx(2.0), pytest.approx(1.0)]


async def test_a_second_run_that_gains_an_inventory_updates_the_row_it_already_wrote(db):
    """The other half of idempotence, and the reason it is `DO UPDATE` rather than `DO NOTHING`.

    A title acquires keywords over time -- §8 stage 2 fetches from eight sources and stage 8 runs
    after them -- so a term named by one inventory today and by two tomorrow has to end up
    carrying two. `DO NOTHING` passes the re-run test above and freezes every row at whatever the
    first pass happened to see, which is a claim about the evidence that stops being true without
    anything recording that it did.
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _alias(db, "unhurried", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])
    await project_title(db, ACQUIRED)
    first = (await _projected(db, ACQUIRED))[0]

    await _keywords(db, ACQUIRED, [("unhurried", "wikidata")])
    await project_title(db, ACQUIRED)

    rows = await _projected(db, ACQUIRED)
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["weight"] == pytest.approx(2.0)
    assert rows[0]["via"] == "keyword:slow-burn, keyword:unhurried"


async def test_the_projection_never_touches_the_extracted_tier(db):
    """§4.1 rule 1. The tiers are separate tables and stay separate: re-running a projection can
    never touch a quote-verified tag, which is the property that lets a consumer wanting only
    auditable claims read one table.

    The two tiers are read together exactly once here, through `dna_tagged`, which carries the
    `tier` discriminator -- the one statement §4.1 permits. The extracted row is also read on its
    own, before and after, to show the projection did not reach it.
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
        " VALUES ($1, $2, 'mood.dread', 'mood', 3, 'pilot')",
        ACQUIRED, VOCAB,
    )
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])
    extracted_before = [dict(row) for row in await db.fetch(
        "SELECT id, term, facet, salience FROM dna_tag WHERE title_id = $1", ACQUIRED
    )]

    await project_title(db, ACQUIRED)

    extracted_after = [dict(row) for row in await db.fetch(
        "SELECT id, term, facet, salience FROM dna_tag WHERE title_id = $1", ACQUIRED
    )]
    assert extracted_before == extracted_after

    tiered = await db.fetch(
        "SELECT term, tier FROM dna_tagged WHERE title_id = $1 ORDER BY tier, term", ACQUIRED
    )
    assert [(row["term"], row["tier"]) for row in tiered] == [
        ("mood.dread", "extracted"), ("pacing.patient", "projected"),
    ]


async def test_both_writers_of_dna_projected_spell_via_the_same_way(db, bundle_dir):
    """THE TEST THAT WAS MISSING, AND THE REASON THE TWO UNITS DRIFTED.

    `dna_projected` has two writers -- `importer/dna.load_projected` for the bundle's titles and
    this module for acquired ones -- and every test in the tree read exactly one of them. So one
    wrote keyword provenance and the other wrote inventory names into the same `text` column, each
    pinned by its own green assertion, and nothing compared them: for a bundle title "which
    keyword produced this term" was answerable and for an acquired title it was not, with no
    reader able to tell the two populations apart because both are comma-joined text.

    The bundle row here is the fixture's own, loaded through the real importer rather than typed
    out, because a hand-written "bundle-style" row is how the drift stayed invisible the first
    time (decision 393). This docstring said so while the body handed a typed literal to the
    private `_via`, which `load_projected` could stop calling with this test still green.
    [M5.4 review cycle 3, M54-DIM5-05]

    THE ORDER IS NOT COMPARED, and the reason is measured rather than chosen. The fixture ships
    `["keyword:obsession", "keyword:heist"]`, `_via` keeps that order and this writer sorts, so
    the two rows differ in order while agreeing on unit, prefix and joiner. Decision 393 says
    sorted on both sides; the importer is M5.3's file, so the half it owes is recorded here rather
    than made up by loosening this writer.
    """
    await db.executemany(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, $3)",
        [(t[0], t[1], t[2]) for t in fx.TITLES],
    )
    report = ImportReport()
    await importer_dna.load_vocabulary(
        db, bundle_dir / "artifacts" / "dna_vocab" / VOCAB, VOCAB, report
    )
    content = sqlite3.connect(f"file:{bundle_dir / 'content.sqlite'}?mode=ro", uri=True)
    try:
        await importer_dna.load_projected(db, content, VOCAB, report)
    finally:
        content.close()
    assert report.ok, report.render()
    imported = await db.fetchval(
        "SELECT via FROM dna_projected WHERE title_id = 1 AND term = 'themes.obsession'"
    )
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _alias(db, "obsession", "themes.obsession")
    await _alias(db, "heist", "themes.obsession")
    await _keywords(db, ACQUIRED, [("obsession", "tmdb"), ("heist", "wikidata")])

    await project_title(db, ACQUIRED)
    via = (await _projected(db, ACQUIRED))[0]["via"]

    assert via == "keyword:heist, keyword:obsession"
    assert sorted(imported.split(", ")) == via.split(", "), (
        "the two writers of one column must put one kind of value in it, or section 6.6's review "
        "reads two vocabularies of provenance as one"
    )


async def test_another_titles_projected_rows_are_untouched(db):
    """The milestone's own refusal: it does not re-project the bundle's existing `dna_projected`
    rows. Those arrived already computed out of `content.sqlite` through
    `importer/dna.load_projected`, and re-deriving them wholesale is a different question with
    its own cost.

    The row below is written the way the importer really writes one -- a raw count of inventories
    in `weight`, and in `via` the flattened keyword provenance the bundle ships, which on this
    install's fixture is `["keyword:obsession", "keyword:heist"]` -- for a title this run is not
    about. A function that swept the table, or that keyed its upsert on anything wider than the
    title it was handed, fails here. The `via` spelling used to be a source list, which no row
    `importer/dna._via` produces has ever carried (decision 393).
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, 7, "a bundle title")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
        " VALUES (7, $1, 'pacing.patient', 'pacing', 4, 'keyword:slow-burn, keyword:unhurried')",
        VOCAB,
    )
    bundle_before = [dict(row) for row in await _projected(db, 7)]
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])

    await project_title(db, ACQUIRED)

    assert [dict(row) for row in await _projected(db, 7)] == bundle_before
    assert [row["term"] for row in await _projected(db, ACQUIRED)] == ["pacing.patient"]


async def test_a_bundle_title_handed_to_the_projection_is_refused_and_its_rows_survive(db):
    """THE NON-GOAL AS A REFUSAL RATHER THAN A SENTENCE. The test above hands the function an
    ACQUIRED title and asserts isolation; this one hands it the bundle title itself, which is the
    call the module's own paragraph said no caller makes.

    Measured before the refusal existed: the importer's row -- a count of nine inventories and the
    corpus's keyword provenance -- came back as weight 1 and `keyword:slow-burn`, the corpus's
    `n_sources` replaced by a local inventory count on the one column `db/dna_terms.TERM_WEIGHT`
    ranks both populations by. Decision 162 seeds content once and `importer/bundle.py` names
    `load_projected` among the tiers it forbids re-importing, so nothing could have restored the
    row. [M5.4 review cycle 3, M54-DIM5-04]
    """
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, 7, "a bundle title")
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
        " VALUES (7, $1, 'pacing.patient', 'pacing', 9, 'keyword:languid, keyword:unhurried')",
        VOCAB,
    )
    await _keywords(db, 7, [("slow-burn", "tmdb")])
    imported = [dict(row) for row in await _projected(db, 7)]

    with pytest.raises(ValueError, match="acquired"):
        await project_title(db, 7)

    assert [dict(row) for row in await _projected(db, 7)] == imported


# --- §5.3's budget ----------------------------------------------------------------------


async def _realistic_install(db) -> None:
    """A vocabulary and an alias map the size of the one this app actually ships.

    v1 carries 582 terms over eleven facets (`artifacts/dna_vocab/v1/vocab_v1_all.tsv`) and the
    alias map is the larger artifact; 2,000 rows is the order of magnitude, and it is the map
    rather than the keyword count that decides what this function costs -- `load_alias_map` reads
    every alias row and every term row on every call. A budget measured against the five-term
    fixture above would be measuring nothing.
    """
    facets = ("mood", "themes", "pacing", "structure", "visual", "sound",
              "characters", "place", "era", "sensibility", "register")
    terms = [f"{facets[i % len(facets)]}.term_{i:03d}" for i in range(582)]
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, $3)",
        VOCAB, len(facets), len(terms),
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(VOCAB, facet, i) for i, facet in enumerate(facets)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3)",
        [(VOCAB, term, term.split(".", 1)[0]) for term in terms],
    )
    await db.executemany(
        "INSERT INTO dna_alias (version, alias, term) VALUES ($1, $2, $3)",
        [(VOCAB, f"raw phrase {i:04d}", terms[i % len(terms)]) for i in range(2000)],
    )


async def test_per_title_projection_of_one_acquired_title_stays_under_one_second(db):
    """§5.3: "DNA projection for a new title (per-title incremental ...) | acquisition | <1 s"
    (`spec:238`), on a box with no GPU (§1, §2).

    The measurement is the whole of stage 8 for one title: the version resolution, the alias map
    read, the fold on both sides of every lookup and the write. Forty keywords across five
    inventories is what a freshly acquired title carries when TMDB and Wikidata have both
    answered -- the fixture's one-keyword titles would time an empty loop.

    Measured on the development box against this fixture: 36 ms for the whole path, nearly all of
    it the 2,582-row alias-map read (2,000 alias rows plus 582 terms), which is why the fixture
    sizes the map rather than the keyword list. The headroom is ~27x, and the assertion is the
    spec's budget rather than the measurement so that a slower runner does not fail a build the
    budget would pass.

    The module is timed and not the `Job`: decision 387 leaves `worker.py:1136`'s registration
    without a `run`, and what §5.3 gives a budget is the work. That is
    `test_placement.py::test_cold_tower_placement_of_one_title_stays_under_one_second`'s own
    shape, which times `reconcile.reconcile` and not the job that reaches it.
    """
    await _realistic_install(db)
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    inventories = ("tmdb", "wikidata", "movielens", "movielens_tags", "mpst")
    await _keywords(db, ACQUIRED, [
        (f"raw phrase {i:04d}", inventories[i % len(inventories)]) for i in range(40)
    ])

    began = time.perf_counter()
    written = await project_title(db, ACQUIRED)
    elapsed = time.perf_counter() - began

    assert written == 40
    assert elapsed < 1.0, (
        f"one-title DNA projection took {elapsed * 1000:.0f} ms (budget 1 s, spec 5.3)"
    )


def test_the_projection_loads_no_model_and_needs_no_gpu():
    """The other half of the budget, and the reason it is not a close call: this function is a
    dictionary lookup over rows the database already holds. §1 makes CPU-only a hard constraint,
    and the way a projection would break it is by acquiring an embedding step -- which is exactly
    what `alias_key`'s docstring refuses ("real synonym resolution is pass 2's job and needs
    embeddings"). Asserted as the module's import list, because that is the one line the change
    would arrive on."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported == {"__future__", "asyncpg", "spielplan.db", "spielplan.dna.aliases"}


# --- decision 384: the cap that is not ported -------------------------------------------


def test_the_projection_cap_is_not_ported_and_no_fifth_vocabulary_file_is_read():
    """Decision 384. `mdc/dna/project.py:94-121` caps a projected row at 0.45 over the terms in
    `projection_capped_v1.txt`, tuned on the 2026-08-21 audit grid. None of it ships, because
    there is nothing for a 0.45 ceiling to bound: decision 188 keeps this column a raw count and
    `db/dna_terms.TERM_WEIGHT` does the bounding at read time.

    Three assertions rather than one, because the cap arrives in three pieces and each alone is
    inert: the ladder, the ceiling, and the file that feeds the list it applies to.
    `models/artifacts.VOCAB_FILES` staying at four names is what keeps a bundle validator from
    requiring an artifact the corpus regenerates after every projection rebuild and this app
    never reads.

    Read through the parse tree and not through the file text, because the module's own docstring
    argues the decision and therefore SAYS all three names. A text scan would either fail on the
    argument or be written loosely enough to miss the code, which is the same category error
    `test_dna_coverage.py`'s numeric-literal guard is written around one module over.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    for ported in ("PROJECTION_CAP", "AGREEMENT_WEIGHT", "agreement_weight", "_capped_terms"):
        assert not hasattr(project_module, ported), f"{ported} is decision 384's unported name"
    reaching_code = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) not in _docstrings(tree)
    }
    assert 0.45 not in reaching_code
    assert "projection_capped_v1.txt" not in reaching_code
    assert VOCAB_FILES == (
        "vocab_v1_all.tsv", "alias_map_v1.tsv", "s_matrix_v1.tsv", "adjudications_v1.tsv"
    )
