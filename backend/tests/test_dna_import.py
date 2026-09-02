"""The naming layer, loaded from the files the corpus actually ships. Spec v2.1 §4.1, §4.3, §10.

Two rows live here.

`data-rules-vocabulary-layer-loads-the-files-the-corpus-ships` — the vocabulary loader read
`terms.tsv`, `aliases.tsv` and `adjudications.tsv`, none of which appear in a bundle. The corpus
ships per-facet `vocab_<facet>_v1.tsv`, `alias_map_v1.tsv` and an `adjudications_v1.tsv` keyed
per TITLE. The per-title keying is the sharp one: `ON CONFLICT (version, term) DO UPDATE`
collapses 817 per-title verdicts onto one row per term, silently and in the direction that
loses data.

`data-rules-corrections-ledger-parsed-at-its-real-header` — the shipped header is
`kind, title_id, value, evidence, note`; the loader read `r["field"]`, so a real bundle raised
`KeyError` where §10 promises a report.

Table shapes below are asserted against `fixtures/real_bundle_shapes.json` — the committed,
data-free manifest of a real bundle — rather than against this repo's reading of §10, which is
the reading that produced the invented file names in the first place.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spielplan.importer import dna
from spielplan.importer.load import SKIPPED_TABLES
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHAPES = json.loads((FIXTURES / "real_bundle_shapes.json").read_text(encoding="utf-8"))


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


@pytest.fixture
def vocab_dir(bundle_dir) -> Path:
    return bundle_dir / "artifacts" / "dna_vocab" / "v1"


@pytest.fixture
def content_db(bundle_dir):
    db = sqlite3.connect(f"file:{bundle_dir / 'content.sqlite'}?mode=ro", uri=True)
    db.text_factory = str
    try:
        yield db
    finally:
        db.close()


async def _seed_titles(conn) -> None:
    """`dna_tag.title_id` references `title(id)`, and §4.1 carries the corpus ids over verbatim."""
    await conn.executemany(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, $3)",
        [(t[0], t[1], t[2]) for t in fx.TITLES],
    )


# --- the corrections ledger, at its real header (no database) --------------------------


def test_corrections_ledger_parses_the_header_the_corpus_ships(bundle_dir):
    """§4.3: `corrections_v1.tsv` travels with the bundle and is applied at every derive.

    The shipped header is `kind, title_id, value, evidence, note` — the manifest is the
    authority here, so the header this test pins comes out of it rather than out of the file
    the fixture happens to write.
    """
    assert SHAPES["tsv"]["artifacts/corrections_v1.tsv"] == [
        "kind", "title_id", "value", "evidence", "note"
    ]

    report = ImportReport()
    rows = dna.parse_corrections(bundle_dir / "artifacts" / "corrections_v1.tsv", report)

    assert report.ok, report.render()
    assert len(rows) == 1
    correction = rows[0]
    assert correction.title_id == 8
    assert correction.field == "composer"
    assert correction.new_value == "Kunihiko Murai"
    assert correction.evidence == "https://example.invalid/tampopo"
    assert correction.note == "credited twice upstream"


def test_an_unrecognised_corrections_header_fails_the_report_naming_the_column(bundle_dir):
    """§10: the importer 'produces a migration report'. `r["field"]` raised `KeyError` instead —
    an uncaught exception is not a report, and the operator gets a stack trace where §10
    promises a validation failure."""
    fx.break_corrections_header(bundle_dir)
    report = ImportReport()

    rows = dna.parse_corrections(bundle_dir / "artifacts" / "corrections_v1.tsv", report)

    assert rows == []
    assert not report.ok
    message = " ".join(f.message for f in report.failures)
    assert "kind" in message and "value" in message and "evidence" in message


def test_a_ledger_with_no_rows_is_reported_rather_than_loaded_as_zero(tmp_path):
    """§14.5's scar is 787 curated fixes reverted twice. A ledger that parses to nothing is the
    same outcome as one that was never applied, so it may not pass in silence."""
    path = tmp_path / "corrections_v1.tsv"
    path.write_text("kind\ttitle_id\tvalue\tevidence\tnote\n", encoding="utf-8")
    report = ImportReport()

    assert dna.parse_corrections(path, report) == []
    assert report.ok, "an empty ledger is not a failed bundle"
    assert any(f.severity == "warn" for f in report.findings)


def test_a_row_whose_title_id_is_not_a_number_is_reported_not_raised(tmp_path):
    path = tmp_path / "corrections_v1.tsv"
    path.write_text(
        "kind\ttitle_id\tvalue\tevidence\tnote\n"
        "composer\tnot-a-number\tSomebody\t\t\n"
        "director\t8\tMichael Mann\t\t\n",
        encoding="utf-8",
    )
    report = ImportReport()

    rows = dna.parse_corrections(path, report)

    assert [r.field for r in rows] == ["director"]
    assert any(f.severity == "warn" for f in report.findings)


# --- the onboarding list, at the keys the corpus writes (Postgres) ----------------------


async def test_the_onboarding_list_decade_is_derived_from_the_shipped_year(db, bundle_dir):
    """§4.3: "the 100-title decade-stratified onboarding list" (§6.1's first-run queue seed).

    The shipped entries are keyed `kind, pct_dislike, pct_like, pct_ok, raters, title,
    title_id, year` — there is no `decade`, and the loader read `int(item["decade"])`, so every
    real bundle's list loaded with a NULL decade. §6.1 seeds the first rating queue from this
    list *because* it spans the decades; a NULL column makes that stratification unreadable
    without changing a row count, so nothing else would ever have noticed.
    """
    await _seed_titles(db)
    path = bundle_dir / "artifacts" / "seed_list.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    assert entries and all("decade" not in e for e in entries), (
        "the fixture ships a `decade` key the corpus does not write; this test would pass on it"
    )
    report = ImportReport()

    await dna.load_seed_list(db, path, report)

    rows = await db.fetch("SELECT position, title_id, decade FROM seed_list ORDER BY position")
    assert len(rows) == len(entries)
    expected = {e["title_id"]: (e["year"] // 10) * 10 for e in entries}
    assert {r["title_id"]: r["decade"] for r in rows} == expected
    assert all(r["decade"] is not None for r in rows)


async def test_an_onboarding_entry_with_no_year_loads_without_a_decade(db, bundle_dir, tmp_path):
    """A year the corpus never resolved is a hole in the stratification, not a broken bundle:
    §6.1's queue still needs the title. It loads with a NULL decade rather than raising."""
    await _seed_titles(db)
    path = tmp_path / "seed_list.json"
    path.write_text(
        json.dumps([{"title_id": fx.TITLES[0][0], "title": "x", "kind": "movie", "year": None}]),
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_seed_list(db, path, report)

    assert await db.fetchval("SELECT decade FROM seed_list WHERE position = 0") is None
    assert report.ok


# --- the vocabulary layer, from the files the bundle contains (Postgres) ----------------


async def test_the_vocabulary_loads_from_the_per_facet_files_the_bundle_ships(db, vocab_dir):
    """§4.3: `dna_vocab/v1/` is 'vocabulary TSVs, alias map, S matrix, adjudications'. The
    loader read a single `terms.tsv`; the corpus ships one file per facet, and the term id
    already carries its facet (`mood.dread`), so the facet is the prefix and never a rebuilt
    `mood.mood.dread`."""
    assert not (vocab_dir / "terms.tsv").exists(), "no bundle contains this file"
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    terms = await db.fetch("SELECT term, facet, gloss FROM dna_term WHERE version = 'v1'")
    assert len(terms) == len(fx.VOCAB)
    by_term = {r["term"]: r for r in terms}
    assert by_term["mood.dread"]["facet"] == "mood"
    assert by_term["mood.dread"]["gloss"].startswith("a low hum of dread")
    assert await db.fetchval("SELECT count(*) FROM dna_facet WHERE version = 'v1'") == 11
    assert await db.fetchval("SELECT term_count FROM dna_vocabulary") == len(fx.VOCAB)


async def test_the_pacing_axes_file_is_not_mistaken_for_a_facet_vocabulary(db, vocab_dir):
    """`vocab_pacing_axes_v1.tsv` matches the per-facet glob and is a different artifact — its
    columns are `id, ax_tempo, ax_pressure, ...` with no label or gloss. A loader that trusts
    the glob invents a twelfth facet named `pacing_axes` out of it."""
    columns = SHAPES["tsv"]["artifacts/dna_vocab/v1/vocab_pacing_axes_v1.tsv"]
    assert "label" not in columns and "gloss" not in columns
    (vocab_dir / "vocab_pacing_axes_v1.tsv").write_text(
        "\t".join(columns) + "\npacing.patient\t0.1\t0.2\t0.3\t0.4\t0.5\t\n", encoding="utf-8"
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    facets = [r["facet"] for r in await db.fetch("SELECT facet FROM dna_facet")]
    assert "pacing_axes" not in facets
    assert await db.fetchval("SELECT count(*) FROM dna_term WHERE version = 'v1'") == len(fx.VOCAB)


async def test_the_alias_map_loads_under_the_name_the_bundle_uses(db, vocab_dir):
    """§8 stage 8 projects the second tier through this map. The loader read `aliases.tsv`
    (`alias`, `term`); the bundle ships `alias_map_v1.tsv` (`raw_term`, ..., `vocab_term`)."""
    assert SHAPES["tsv"]["artifacts/dna_vocab/v1/alias_map_v1.tsv"] == [
        "raw_term", "df", "facet", "vocab_term", "via_concept", "kind"
    ]
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    rows = await db.fetch("SELECT alias, term FROM dna_alias WHERE version = 'v1' ORDER BY alias")
    assert [(r["alias"], r["term"]) for r in rows] == [
        ("cozy", "mood.cosy"), ("slow-burn", "pacing.patient")
    ]


async def test_an_alias_that_maps_to_nothing_is_skipped_rather_than_crashing(db, vocab_dir):
    """`alias_map_v1.tsv` carries raw terms the vocabulary did not adopt; `dna_alias.term` is
    NOT NULL, so an unmapped raw term is a constraint violation mid-transaction unless the
    loader drops it."""
    (vocab_dir / "alias_map_v1.tsv").write_text(
        "raw_term\tdf\tfacet\tvocab_term\tvia_concept\tkind\n"
        "slow-burn\t12\tpacing\tpacing.patient\t\talias\n"
        "gritty\t400\tmood\t\t\tunmapped\n",
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    assert await db.fetchval("SELECT count(*) FROM dna_alias") == 1


# --- adjudications are per title (Postgres) ---------------------------------------------


async def test_adjudications_load_in_their_real_per_title_shape(db, vocab_dir):
    """§6.6's ledger editor writes `adjudications_v1.tsv` back, so every shipped column has to
    survive the round trip. The loader read `term, verdict, target, note`; the file is
    `scope, title_id, term, action, target, quote, source, note`."""
    assert SHAPES["tsv"]["artifacts/dna_vocab/v1/adjudications_v1.tsv"] == [
        "scope", "title_id", "term", "action", "target", "quote", "source", "note"
    ]
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    rows = await db.fetch(
        "SELECT scope, title_id, term, verdict, target, source, note FROM dna_adjudication "
        "ORDER BY title_id NULLS LAST"
    )
    assert len(rows) == 2
    per_title, global_row = rows
    assert (per_title["scope"], per_title["title_id"]) == ("title", 1)
    assert (per_title["term"], per_title["verdict"]) == ("mood.cosy", "drop")
    assert per_title["source"] == "trakt:comment"
    assert (global_row["scope"], global_row["title_id"]) == ("global", None)
    assert (global_row["term"], global_row["verdict"], global_row["target"]) == (
        "cozy", "rename", "mood.cosy"
    )


async def test_one_term_adjudicated_on_many_titles_keeps_one_row_per_title(db, vocab_dir):
    """The defect this row exists for. `ON CONFLICT (version, term) DO UPDATE` keeps the LAST
    verdict for a term and throws the rest away — 817 per-title verdicts become one row per
    term, with no failure and no count to notice it by.

    The shipped fixture carries a single per-title verdict, which cannot tell a collapse from a
    correct load, so this test writes the collision itself.
    """
    (vocab_dir / "adjudications_v1.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        "title\t1\tmood.cosy\tdrop\t\t\ttrakt:comment\twrong film\n"
        "title\t2\tmood.cosy\tkeep\t\t\ttrakt:comment\tright film\n"
        "title\t3\tmood.cosy\tdrop\t\t\ttrakt:comment\talso wrong\n",
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    rows = await db.fetch(
        "SELECT title_id, verdict FROM dna_adjudication WHERE term = 'mood.cosy' "
        "ORDER BY title_id"
    )
    assert [(r["title_id"], r["verdict"]) for r in rows] == [(1, "drop"), (2, "keep"), (3, "drop")]


async def test_a_title_scoped_verdict_with_no_title_is_reported_not_stored(db, vocab_dir):
    (vocab_dir / "adjudications_v1.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        "title\t\tmood.cosy\tdrop\t\t\ttrakt:comment\tno title to apply this to\n",
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    # The vocabulary itself has to have loaded, or this passes on the loader that reads no file
    # in this directory at all -- which is the loader M4.5 replaced.
    assert await db.fetchval("SELECT count(*) FROM dna_term WHERE version = 'v1'") == len(fx.VOCAB)
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 0
    assert any(f.severity == "warn" for f in report.findings)


# --- the two tiers, at the upstream keying (Postgres) ------------------------------------


async def test_the_extracted_tier_loads_with_its_evidence_at_the_upstream_keying(
    db, vocab_dir, content_db
):
    """§4.1 rule 1: 'dna_evidence ships with the extracted tier — a tag without its quote is
    unfalsifiable.' Upstream `dna_tag` has no `id` at all: its PK is (title_id, term), and
    `dna_evidence` is keyed by the same pair, not by a `dna_tag_id`. The loader selected
    `id, ..., n_sources, provider` and died on `no such column: id`."""
    assert SHAPES["sqlite"]["content.sqlite"]["dna_tag"] == [
        "title_id", "term", "facet", "salience", "confidence", "runs_found"
    ]
    assert SHAPES["sqlite"]["content.sqlite"]["dna_evidence"] == [
        "id", "title_id", "term", "pass_id", "src", "quote"
    ]
    await _seed_titles(db)
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    await dna.load_tags(db, content_db, "v1", report)

    assert report.ok, report.render()
    assert report.table_counts["loaded:dna_tag"] == len(fx.EXTRACTED)
    assert report.table_counts["loaded:dna_evidence"] == len(fx.EXTRACTED)
    rows = await db.fetch(
        "SELECT g.term, g.n_sources, e.quote, e.source FROM dna_tag g "
        "JOIN dna_evidence e ON e.dna_tag_id = g.id WHERE g.title_id = 1 ORDER BY g.term"
    )
    assert [r["term"] for r in rows] == ["characters.morally_grey", "themes.obsession"]
    assert rows[1]["quote"] == "the work eats the man and he lets it"
    assert rows[1]["source"] == "trakt:comment"
    # `runs_found` is how many extraction runs found the tag — a weight, never a filter (rule 2).
    assert rows[1]["n_sources"] == 3


async def test_the_projected_tier_keeps_n_sources_as_a_weight(db, vocab_dir, content_db):
    """§4.1 rule 2: 'salience, confidence, n_sources are weights, never filters.' Upstream the
    projected tier is (title_id, term, facet, n_sources, sources); this schema calls the weight
    `weight` and the provenance `via`, and the loader selected columns of those names."""
    assert SHAPES["sqlite"]["content.sqlite"]["dna_projected"] == [
        "title_id", "term", "facet", "n_sources", "sources"
    ]
    await _seed_titles(db)
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    await dna.load_projected(db, content_db, "v1", report)

    assert report.ok, report.render()
    assert report.table_counts["loaded:dna_projected"] == len(fx.PROJECTED)
    row = await db.fetchrow(
        "SELECT weight, via FROM dna_projected WHERE title_id = 1 AND term = 'themes.obsession'"
    )
    assert row["weight"] == pytest.approx(2.0)
    assert "keyword:obsession" in row["via"] and "keyword:heist" in row["via"]


async def test_the_shared_pairs_stay_distinguishable_across_the_two_tiers(
    db, vocab_dir, content_db
):
    """Rule 1: '14,181 (title,term) pairs exist in both and must stay distinguishable.'"""
    await _seed_titles(db)
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    await dna.load_tags(db, content_db, "v1", report)
    await dna.load_projected(db, content_db, "v1", report)

    tiers = await db.fetch(
        "SELECT tier FROM dna_tagged WHERE title_id = 1 AND term = 'themes.obsession' "
        "ORDER BY tier"
    )
    assert [r["tier"] for r in tiers] == ["extracted", "projected"]


async def test_loading_the_dna_layer_twice_does_not_duplicate_it(db, vocab_dir, content_db):
    """§10 calls a re-import 'a planned admin event', which means running it twice may not
    double the rows. The extracted tier's UNIQUE carries `provider`, and the corpus exports no
    provider column, so the ON CONFLICT that was meant to make this idempotent never fires."""
    await _seed_titles(db)
    report = ImportReport()
    for _ in range(2):
        await dna.load_vocabulary(db, vocab_dir, "v1", report)
        await dna.load_tags(db, content_db, "v1", report)
        await dna.load_projected(db, content_db, "v1", report)

    assert report.ok, report.render()
    assert await db.fetchval("SELECT count(*) FROM dna_tag") == len(fx.EXTRACTED)
    assert await db.fetchval("SELECT count(*) FROM dna_evidence") == len(fx.EXTRACTED)
    assert await db.fetchval("SELECT count(*) FROM dna_projected") == len(fx.PROJECTED)
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 2


async def test_the_corrections_ledger_is_not_duplicated_by_a_re_import(db, bundle_dir):
    """Decision 162 makes a model re-import a repeated event. The loader did a bare INSERT with
    no clear, so every re-import appended the whole ledger again."""
    report = ImportReport()
    path = bundle_dir / "artifacts" / "corrections_v1.tsv"

    await dna.load_corrections(db, path, report)
    await dna.load_corrections(db, path, report)

    row = await db.fetchrow("SELECT title_id, field, new_value, evidence FROM credit_correction")
    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert (row["title_id"], row["field"], row["new_value"]) == (8, "composer", "Kunihiko Murai")
    assert row["evidence"] == "https://example.invalid/tampopo"


# --- every DNA table the §10 manifest names is accounted for -----------------------------


async def test_every_shipped_dna_table_is_loaded_or_skipped_with_a_reason(
    db, vocab_dir, content_db
):
    """§10's manifest line: 'DNA layer (tag + projected + evidence + annotation + term_signal +
    exclusion)'. Three of those have no table here at all, and an unloaded table used to be
    invisible — `ImportReport` tracked unmapped *columns within mapped tables*. The list comes
    from the shape manifest, so a table the corpus adds cannot slip through this test either.
    """
    await _seed_titles(db)
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    await dna.load_tags(db, content_db, "v1", report)
    await dna.load_projected(db, content_db, "v1", report)

    shipped = [t for t in SHAPES["sqlite"]["content.sqlite"] if t.startswith("dna_")]
    assert {"dna_tag", "dna_projected", "dna_evidence", "dna_annotation", "dna_term_signal",
            "dna_exclusion"} <= set(shipped)
    for table in shipped:
        loaded = report.table_counts.get(f"loaded:{table}")
        reason = SKIPPED_TABLES.get(table)
        assert loaded or reason, f"{table} is neither loaded nor reported as skipped"
        # A loader that claims a table it never writes is the same silence with a count on it.
        assert not (loaded and reason), f"{table} is both loaded and named as skipped"
