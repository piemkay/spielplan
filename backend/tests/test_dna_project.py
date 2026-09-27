"""§8 stage 8's per-title projection against a real Postgres (decisions 384, 385). The fixture's keywords
and alias rows are disjoint, so it projects nothing (measured below) and other tests insert rows.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from spielplan.db import dna_terms
from spielplan.dna.project import DEFAULT_SOURCES, project_title
from spielplan.importer import dna as importer_dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

VOCAB = "v1"

# Five terms over four facets: a wrong facet shows as a wrong facet.
TERMS = (
    "pacing.patient",
    "mood.cosy",
    "mood.dread",
    "themes.obsession",
    "register.deadpan",
)

# §4.1's minting rule: `origin = 'acquired'` and an id at or above 1e9.
ACQUIRED = 1_000_000_007


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
        # The facet is the term's own prefix, as `importer/dna.app_facet` stores it.
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


async def _projected(db, title_id: int):
    return await db.fetch(
        "SELECT id, term, facet, weight, via, created_at FROM dna_projected"
        " WHERE title_id = $1 ORDER BY term",
        title_id,
    )


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


async def test_the_shipped_fixture_projects_nothing_and_that_is_the_measurement(db, bundle_dir):
    """The self-mapping keys on the whole term id, so `themes.obsession` is never reached as "obsession"."""
    report = ImportReport()
    # Stamped `acquired`, because stage 8 refuses a bundle title: the zero measured here is the map's.
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

    # So a fixture that later grows an overlapping row fails HERE.
    aliases = set(await db.fetchval("SELECT array_agg(alias) FROM dna_alias WHERE version = $1",
                                    VOCAB))
    keywords = {keyword for _, _, keyword in fx.KEYWORDS}
    assert aliases == {"slow-burn", "cozy"}
    assert keywords == {"heist", "investigation", "family", "cooking"}
    assert aliases & keywords == set()


def test_the_inventories_the_projection_reads_are_the_corpus_list():
    """`title_aspect` must be absent: its phrases are what the vocabulary was curated FROM."""
    assert DEFAULT_SOURCES == (
        "llm:gemini37", "llm:sonnet5", "llm:terra",
        "movielens", "movielens_tags", "mpst", "tmdb", "wikidata",
    )
    assert "title_aspect" not in DEFAULT_SOURCES
    # So the zero above is about the alias map, not a source filter eating every row.
    assert {source for _, source, _ in fx.KEYWORDS} <= set(DEFAULT_SOURCES)


async def test_the_keyword_table_carries_no_weight_to_fold_in(db):
    """Genome relevance and tag counts encode popularity (rho ~0.51); pinned against the live schema."""
    columns = set(await db.fetchval(
        "SELECT array_agg(column_name::text) FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'title_keyword'"
    ))
    assert columns == {"title_id", "keyword", "source"}


async def test_a_keyword_that_matches_an_alias_row_projects_one_row(db):
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
    """The stored alias and the incoming keyword both pass through `alias_key`."""
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
    """The weight counts INDEPENDENT inventories; `via` names the keywords that produced the row."""
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
    """TMDB naming a term twice is one opinion said twice."""
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
    """Decision 385: a raw count, never the corpus's 0.3/0.6/1.0
    ladder, which that column would accept silently."""
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
    """Three inventories read at 0.225; the ladder would give
    0.15, inside the band and a third of the evidence."""
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


async def test_a_lexicon_alias_row_never_projects(db):
    """A second alias row onto the SAME term with no `kind` projects, so this asserts the column."""
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
    """The INSERT is asserted to succeed first, or the absence would pass against a refused row."""
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
    """~49% of the corpus's rows map to nothing: the common case, silent by design."""
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("time travel", "tmdb"), ("black and white", "wikidata")])

    assert await project_title(db, ACQUIRED) == 0
    assert await _projected(db, ACQUIRED) == []


async def test_a_keyword_from_an_inventory_the_projection_does_not_read_is_ignored(db):
    """The same keyword from `tmdb` projects in the same test, so this is about the source column."""
    await _vocabulary(db)
    await _alias(db, "slow-burn", "pacing.patient")
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "title_aspect")])

    assert await project_title(db, ACQUIRED) == 0

    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])
    assert await project_title(db, ACQUIRED) == 1


async def test_an_install_with_no_vocabulary_projects_nothing(db):
    """`dna_projected.version` has a foreign key, so there is no version the projection could invent."""
    await _title(db, ACQUIRED, "La Jetee", origin="acquired")
    await _keywords(db, ACQUIRED, [("slow-burn", "tmdb")])

    assert await dna_terms.active_version(db) is None
    assert await project_title(db, ACQUIRED) == 0
    assert await db.fetchval("SELECT count(*) FROM dna_projected") == 0


async def test_the_projection_is_scoped_to_one_vocabulary_version(db):
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


async def test_re_running_the_projection_for_one_title_changes_nothing(db):
    """Same `id` and `created_at`: a delete-and-rewrite
    would re-stamp "when did this claim first appear"."""
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
    """`DO UPDATE`, not `DO NOTHING`, which would freeze a row at the first pass's evidence."""
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
    """§4.1 rule 1: the tiers are read together only through `dna_tagged`."""
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
    """Both writers of `dna_projected.via` use one unit
    (decision 393); order differs and is not compared."""
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
    """The bundle's rows are not re-projected; an upsert keyed wider than the title fails here."""
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
    """Content is seeded once (decision 162), so a re-projected bundle row could never be restored."""
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


async def _realistic_install(db) -> None:
    """The alias map, not the keyword count, decides the cost: it is read whole on every call."""
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
    """§5.3: <1 s per title on a GPU-less box; measured ~36 ms, and the spec's budget is asserted."""
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
