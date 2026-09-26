"""The naming layer, loaded from the files the corpus actually ships (§4.1, §4.3, §10), and the curated
ledgers a models-only import reloads (decision 247). Shapes are held to `real_bundle_shapes.json`."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from spielplan.api import admin as admin_api
from spielplan.dna import aliases as dna_aliases
from spielplan.importer import dna
from spielplan.importer import validate as validator
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


def test_corrections_ledger_parses_the_header_the_corpus_ships(bundle_dir):
    """The header is taken from the shape manifest, not from the file the fixture writes."""
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
    """`r["field"]` raised `KeyError`; §10 promises a report, not a stack trace."""
    fx.break_corrections_header(bundle_dir)
    report = ImportReport()

    rows = dna.parse_corrections(bundle_dir / "artifacts" / "corrections_v1.tsv", report)

    assert rows == []
    assert not report.ok
    message = " ".join(f.message for f in report.failures)
    assert "kind" in message and "value" in message and "evidence" in message


def test_a_ledger_with_no_rows_is_reported_rather_than_loaded_as_zero(tmp_path):
    """A ledger that parses to nothing is the same outcome as one never applied."""
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


def test_a_latin_1_byte_in_the_corrections_ledger_is_a_report_line_not_an_exception(tmp_path):
    """`validate._read_tsv` is the one opener of a curated TSV, so a stray byte is a finding."""
    path = tmp_path / "corrections_v1.tsv"
    path.write_bytes(
        b"kind\ttitle_id\tvalue\tevidence\tnote\n"
        + "composer\t8\tJo\xebl Beaulieu\t\t\n".encode("latin-1")
    )
    report = ImportReport()

    rows = dna.parse_corrections(path, report)

    assert rows == []
    failures = [f for f in report.failures if f.rule == "corrections"]
    assert len(failures) == 1, report.render()
    assert "corrections_v1.tsv" in failures[0].message
    assert "UTF-8" in failures[0].message


async def test_the_onboarding_list_decade_is_derived_from_the_shipped_year(db, bundle_dir):
    """No `decade` key ships; a NULL decade makes the first-run stratification unreadable."""
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


async def test_the_seed_list_import_reports_entries_with_no_card_text(db, bundle_dir):
    """A thin onboarding list (no poster, no card text) is reported on import, under `seed-list`."""
    await _seed_titles(db)
    first, second, third = (t[0] for t in fx.TITLES[:3])
    await db.execute(
        "UPDATE title SET overview = 'A heist, told straight.', poster_path = 'https://x/p.jpg' "
        "WHERE id = $1",
        first,
    )
    await db.execute("UPDATE title SET overview = 'Told to the end.' WHERE id = $1", second)
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) "
        "VALUES ($1, 'mpst', '{\"plot_full\": \"Told to the end.\"}'::jsonb)",
        second,
    )
    await db.execute("UPDATE title SET overview = 'A quiet film.' WHERE id = $1", third)
    report = ImportReport()

    await dna.load_seed_list(db, bundle_dir / "artifacts" / "seed_list.json", report)

    thin = [f for f in report.findings if f.rule == "seed-list-cards"]
    assert [f.severity for f in thin] == ["warn"], report.render()
    total = len(fx.TITLES)
    # Everything but `first` lacks a poster; everything but `first` and `third` lacks card text.
    assert thin[0].detail == {"no_text": total - 2, "no_poster": total - 1, "titles": total}
    assert thin[0].message.isascii(), thin[0].message
    assert not [f for f in report.findings if f.rule == "seed-list" and f.severity == "warn"]


async def test_an_onboarding_entry_with_no_year_loads_without_a_decade(db, bundle_dir, tmp_path):
    """An unresolved year is a hole in the stratification, not a broken bundle."""
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


async def test_a_shorter_onboarding_list_leaves_no_tail_of_the_old_one(db, bundle_dir, tmp_path):
    """An upsert with no clear is a merge: a shorter list left the old tail behind."""
    await _seed_titles(db)
    report = ImportReport()
    await dna.load_seed_list(db, bundle_dir / "artifacts" / "seed_list.json", report)
    assert await db.fetchval("SELECT count(*) FROM seed_list") == len(fx.TITLES)

    shorter = tmp_path / "seed_list.json"
    shorter.write_text(
        json.dumps([
            {"title_id": t[0], "title": t[2], "kind": t[1], "year": t[4]} for t in fx.TITLES[:3]
        ]),
        encoding="utf-8",
    )

    await dna.load_seed_list(db, shorter, report)

    rows = await db.fetch("SELECT position, title_id FROM seed_list ORDER BY position")
    assert [(r["position"], r["title_id"]) for r in rows] == [
        (0, fx.TITLES[0][0]), (1, fx.TITLES[1][0]), (2, fx.TITLES[2][0])
    ]


async def test_an_onboarding_entry_naming_an_unseeded_title_is_skipped_and_counted(db, tmp_path):
    """`seed_list.title_id` is a NOT NULL FK, so an unseeded title is skipped and counted, not fatal."""
    await _seed_titles(db)
    path = tmp_path / "seed_list.json"
    path.write_text(
        json.dumps([
            {"title_id": fx.TITLES[0][0], "title": "Heat", "kind": "movie", "year": 1995},
            {"title_id": 999_001, "title": "a title this install never seeded",
             "kind": "movie", "year": 2026},
            {"title_id": fx.TITLES[1][0], "title": "Prisoners", "kind": "movie", "year": 2013},
        ]),
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_seed_list(db, path, report)

    assert report.ok, report.render()
    rows = await db.fetch("SELECT position, title_id FROM seed_list ORDER BY position")
    assert [(r["position"], r["title_id"]) for r in rows] == [
        (0, fx.TITLES[0][0]), (1, fx.TITLES[1][0])
    ]
    skipped = [f for f in report.findings if f.rule == "seed-list" and f.severity == "warn"]
    assert len(skipped) == 1, report.render()
    assert skipped[0].detail == {"skipped": 1, "title_ids": [999_001]}
    assert "999001" in skipped[0].message, skipped[0].message


async def test_an_empty_onboarding_list_is_refused_rather_than_clearing_the_installed_one(
    db, tmp_path
):
    """Decision 260: a present-but-empty list must not clear the installed one."""
    await _seed_titles(db)
    full = tmp_path / "seed_list.json"
    full.write_text(
        json.dumps([
            {"title_id": t[0], "title": t[1], "kind": "movie", "year": 1995}
            for t in fx.TITLES[:3]
        ]),
        encoding="utf-8",
    )
    await dna.load_seed_list(db, full, ImportReport())
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 3

    for payload in ("[]", '{"titles": []}'):
        empty = tmp_path / "seed_list.json"
        empty.write_text(payload, encoding="utf-8")
        report = ImportReport()

        await dna.load_seed_list(db, empty, report)

        assert report.ok, "an empty list is not a failed bundle; it is a refusal to replace"
        assert await db.fetchval("SELECT count(*) FROM seed_list") == 3, payload
        findings = [f for f in report.findings if f.rule == "seed-list"]
        assert [f.severity for f in findings] == ["warn"], report.render()
        assert findings[0].detail == {"stored": 3}
        assert "left in place" in findings[0].message, findings[0].message


async def test_an_onboarding_list_of_unknown_ids_is_told_apart_from_an_empty_one(db, tmp_path):
    """Parsed-but-all-unknown ids get a different line from a genuinely empty file."""
    await _seed_titles(db)
    installed = tmp_path / "installed.json"
    installed.write_text(
        json.dumps([
            {"title_id": t[0], "title": t[2], "kind": t[1], "year": t[4]} for t in fx.TITLES[:3]
        ]),
        encoding="utf-8",
    )
    await dna.load_seed_list(db, installed, ImportReport())
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 3

    renumbered = tmp_path / "seed_list.json"
    renumbered.write_text(
        json.dumps([
            {"title_id": 900_001, "title": "renumbered", "kind": "movie", "year": 1994},
            {"title_id": 900_002, "title": "renumbered too", "kind": "movie", "year": 2001},
        ]),
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_seed_list(db, renumbered, report)

    assert report.ok, report.render()
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 3, "guard 2 still holds"
    findings = [f for f in report.findings if f.rule == "seed-list"]
    assert [f.severity for f in findings] == ["warn"], report.render()
    # The count and ids guard 1 owes, and guard 2's no-replacement fact, in one line.
    assert findings[0].detail == {
        "skipped": 2, "title_ids": [900_001, 900_002], "stored": 3
    }, findings[0].detail
    assert "never seeded" in findings[0].message, findings[0].message
    assert "left in place" in findings[0].message, findings[0].message

    # And the genuinely empty file still gets the sentence written for it, unchanged.
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    plain = ImportReport()
    await dna.load_seed_list(db, empty, plain)
    assert [f.detail for f in plain.findings if f.rule == "seed-list"] == [{"stored": 3}]


async def test_an_onboarding_year_this_app_cannot_read_loads_without_a_decade(db, tmp_path):
    """`NaN`, `Infinity` and an epoch stamp are three
    spellings of an unresolved year, and none may raise."""
    await _seed_titles(db)
    path = tmp_path / "seed_list.json"
    # Written as TEXT: `json.dumps` emits bare `NaN`/`Infinity` and `json.loads` accepts them.
    ids = [t[0] for t in fx.TITLES[:4]]
    path.write_text(
        f'[{{"title_id": {ids[0]}, "title": "unresolved", "kind": "movie", "year": NaN}},'
        f' {{"title_id": {ids[1]}, "title": "infinite", "kind": "movie", "year": Infinity}},'
        f' {{"title_id": {ids[2]}, "title": "epoch", "kind": "movie", "year": 1756000000}},'
        f' {{"title_id": {ids[3]}, "title": "ordinary", "kind": "movie", "year": 1995}}]',
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_seed_list(db, path, report)

    assert report.ok, report.render()
    rows = await db.fetch("SELECT title_id, decade FROM seed_list ORDER BY position")
    assert [r["decade"] for r in rows] == [None, None, None, 1990], [
        (r["title_id"], r["decade"]) for r in rows
    ]
    # A hole in the stratification is a smaller loss than a refused seed.
    assert len(rows) == 4
    note = next(f for f in report.findings if f.rule == "seed-list" and f.severity == "note")
    assert note.detail["undated"] == 3, note.message


async def test_an_absent_curated_ledger_names_what_is_still_installed(db, bundle_dir, tmp_path):
    """An absent ledger changes nothing on a models-only import, so the warning must not claim otherwise."""
    await _seed_titles(db)
    seed = bundle_dir / "artifacts" / "seed_list.json"
    corrections = bundle_dir / "artifacts" / "corrections_v1.tsv"
    vocab = bundle_dir / "artifacts" / "dna_vocab" / "v1"
    await dna.load_seed_list(db, seed, ImportReport())
    await dna.load_corrections(db, corrections, ImportReport())
    await dna.load_vocabulary(db, vocab, "v1", ImportReport())
    await dna.load_adjudications(db, vocab, "v1", ImportReport())
    before = {
        table: await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in ("seed_list", "credit_correction", "dna_adjudication")
    }
    assert all(before.values()), before

    absent = tmp_path / "absent"
    absent.mkdir()
    report = ImportReport()
    await dna.load_seed_list(db, absent / "seed_list.json", report)
    await dna.load_corrections(db, absent / "corrections_v1.tsv", report)
    await dna.load_adjudications(db, absent, "v1", report)

    assert report.ok, report.render()
    after = {
        table: await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in ("seed_list", "credit_correction", "dna_adjudication")
    }
    assert after == before, "an absent ledger file changes nothing (decision 247)"
    lines = {f.rule: f for f in report.findings}
    assert set(lines) == {"seed-list", "corrections", "adjudications"}, report.render()
    for rule, table in (
        ("seed-list", "seed_list"),
        ("corrections", "credit_correction"),
        ("adjudications", "dna_adjudication"),
    ):
        assert lines[rule].detail["stored"] == before[table], lines[rule].message
        assert str(before[table]) in lines[rule].message, lines[rule].message
        assert "left in place" in lines[rule].message, lines[rule].message
    # The one sentence the app's own why-line contradicted on the very next Rate card.
    assert "P(seen)" not in lines["seed-list"].message, lines["seed-list"].message
    assert "will not survive" not in lines["corrections"].message, lines["corrections"].message


async def test_a_bundle_with_no_axis_file_says_which_weights_it_left_standing(
    db, bundle_dir, tmp_path
):
    """With weights installed, the no-axis warning must not claim consequences the install lacks."""
    vocab = bundle_dir / "artifacts" / "dna_vocab" / "v1"
    await dna.load_vocabulary(db, vocab, "v1", ImportReport())
    await dna.load_axes(db, vocab, "v1", ImportReport())
    weights = await db.fetch(
        "SELECT facet, term, weight FROM dna_axis_weight WHERE version = 'v1' ORDER BY facet, term"
    )
    assert weights, "the fixture authors axes; without them this test cannot fail"

    for axis in vocab.glob("*.tsv"):
        if axis.stem in fx.AXES:
            axis.unlink()
    report = ImportReport()

    await dna.load_axes(db, vocab, "v1", report)

    assert report.ok, report.render()
    after = await db.fetch(
        "SELECT facet, term, weight FROM dna_axis_weight WHERE version = 'v1' ORDER BY facet, term"
    )
    assert [tuple(r) for r in after] == [tuple(r) for r in weights], "decision 247: nothing moves"
    warned = [f for f in report.findings if f.rule == "axes" and f.severity == "warn"]
    assert len(warned) == 1, report.render()
    assert "no authored axis definition" in warned[0].message
    assert warned[0].detail["stored"] == len(weights)
    assert str(len(weights)) in warned[0].message, warned[0].message
    assert "no axes to plot" not in warned[0].message, warned[0].message
    assert "facet split" not in warned[0].message, warned[0].message

    # The install that HAS those consequences is still told about them.
    await db.execute("DELETE FROM dna_axis_weight WHERE version = 'v1'")
    bare = ImportReport()
    await dna.load_axes(db, vocab, "v1", bare)
    axis_warn = next(f for f in bare.findings if f.rule == "axes" and f.severity == "warn")
    assert "no axes to plot" in axis_warn.message, axis_warn.message
    # Since decision 479 an axisless split is surfaced by person, so the facet split is what is lost.
    assert "facet split (§6.2 step 5) is off" in axis_warn.message, axis_warn.message
    assert "surfaced by person" in axis_warn.message, axis_warn.message


async def test_the_vocabulary_loads_from_the_per_facet_files_the_bundle_ships(db, vocab_dir):
    """The term id already carries its facet, so the facet is the prefix, never `mood.mood.dread`."""
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
    """`vocab_pacing_axes_v1.tsv` matches the glob but is a coordinates file, not a vocabulary."""
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


def _ship_a_label_that_is_not_the_leaf(vocab_dir: Path) -> None:
    """The fixture's labels are all the leaf, so a loader storing the leaf would pass without this."""
    with (vocab_dir / "vocab_era_v1.tsv").open("a", encoding="utf-8", newline="") as fh:
        fh.write("era.wwii\tWorld War II\tthe Second World War as the ground\t\t0.01\t0.4\t0.5\t\t\t\t\n")


async def test_the_vocabulary_label_is_stored_as_shipped(db, vocab_dir):
    """§6.8's why is in vocabulary terms: the shipped label, never the id."""
    _ship_a_label_that_is_not_the_leaf(vocab_dir)
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    labels = {r["term"]: r["label"] for r in await db.fetch("SELECT term, label FROM dna_term")}
    assert labels["era.wwii"] == "World War II"
    assert labels["characters.morally_grey"] == "morally_grey", "stored as shipped, not respelled"


async def test_label_backfill_fills_only_null_labels_and_is_idempotent(db, vocab_dir):
    """Only NULL labels are filled, idempotently, and nothing from a directory that is gone."""
    _ship_a_label_that_is_not_the_leaf(vocab_dir)
    await dna.load_vocabulary(db, vocab_dir, "v1", ImportReport())
    await db.execute("UPDATE dna_term SET label = NULL")
    await db.execute("UPDATE dna_term SET label = 'kept as stored' WHERE term = 'mood.dread'")
    await db.execute("DELETE FROM dna_term WHERE term = 'visual.neon'")
    rows = await db.fetchval("SELECT count(*) FROM dna_term")

    filled = await dna.backfill_labels(db, vocab_dir, "v1")

    labels = {r["term"]: r["label"] for r in await db.fetch("SELECT term, label FROM dna_term")}
    assert filled == rows - 1, "every NULL label, and not the one already stored"
    assert labels["era.wwii"] == "World War II"
    assert labels["mood.dread"] == "kept as stored"
    assert "visual.neon" not in labels, "a backfill is an UPDATE: it writes no content row"
    assert await db.fetchval("SELECT count(*) FROM dna_term") == rows
    assert await dna.backfill_labels(db, vocab_dir, "v1") == 0
    assert await dna.backfill_labels(db, vocab_dir.parent / "v9", "v9") == 0


async def test_the_alias_map_loads_under_the_name_the_bundle_uses(db, vocab_dir):
    """The bundle ships `alias_map_v1.tsv` (`raw_term`, ..., `vocab_term`)."""
    assert SHAPES["tsv"]["artifacts/dna_vocab/v1/alias_map_v1.tsv"] == [
        "raw_term", "df", "facet", "vocab_term", "via_concept", "kind"
    ]
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    rows = await db.fetch("SELECT alias, term FROM dna_alias WHERE version = 'v1' ORDER BY alias")
    assert [(r["alias"], r["term"]) for r in rows] == [
        ("cozy", "mood.cosy"), ("slow-burn", "pacing.patient")
    ]


async def test_the_alias_kind_is_stored_and_a_lexicon_row_never_projects(db, vocab_dir):
    """`kind` is stored as shipped; a map with no `kind` column still loads with NULL."""
    (vocab_dir / "alias_map_v1.tsv").write_text(
        "raw_term\tdf\tfacet\tvocab_term\tvia_concept\tkind\n"
        "slow-burn\t12\tpacing\tpacing.patient\t\talias\n"
        "cozy\t9\tmood\tmood.cosy\t\tlexicon\n",
        encoding="utf-8",
    )
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    kinds = {r["alias"]: r["kind"] for r in await db.fetch("SELECT alias, kind FROM dna_alias")}
    assert kinds == {"slow-burn": "alias", "cozy": "lexicon"}
    note = next(f for f in report.findings if "lexicon" in f.detail)
    assert note.detail["lexicon"] == 1
    projected = await dna_aliases.load_alias_map(db, "v1")
    assert projected["slow burn"] == ("pacing", "pacing.patient")
    assert "cozy" not in projected, "a lexicon row projected"

    await db.execute("DELETE FROM dna_alias")
    (vocab_dir / "alias_map_v1.tsv").write_text(
        "raw_term\tdf\tfacet\tvocab_term\tvia_concept\ncozy\t9\tmood\tmood.cosy\t\n",
        encoding="utf-8",
    )
    await dna._load_aliases(db, vocab_dir / "alias_map_v1.tsv", "v1", ImportReport())
    assert await db.fetchval("SELECT kind FROM dna_alias WHERE alias = 'cozy'") is None
    assert (await dna_aliases.load_alias_map(db, "v1"))["cozy"] == ("mood", "mood.cosy")


async def test_an_alias_that_maps_to_nothing_is_skipped_rather_than_crashing(db, vocab_dir):
    """`dna_alias.term` is NOT NULL, so an unadopted raw term must be dropped, not inserted."""
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


async def test_a_latin_1_byte_in_the_alias_map_is_a_report_line_not_an_exception(db, vocab_dir):
    """`load_axes` globs every `*.tsv`, so it meets the alias map's bad byte too and must not raise."""
    (vocab_dir / "alias_map_v1.tsv").write_bytes(
        b"raw_term\tdf\tfacet\tvocab_term\tvia_concept\tkind\n"
        + "cosy\xa0\t12\tmood\tmood.cosy\t\talias\n".encode("latin-1")
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    failures = [f for f in report.failures if "alias_map_v1.tsv" in f.message]
    assert len(failures) == 1, report.render()
    assert "UTF-8" in failures[0].message
    assert await db.fetchval("SELECT count(*) FROM dna_alias") == 0
    assert await db.fetchval("SELECT count(*) FROM dna_term WHERE version = 'v1'") == len(fx.VOCAB)
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 2
    assert {r["facet"] for r in await db.fetch("SELECT facet FROM dna_axis")} == set(fx.AXES)
    passed_over = [
        f for f in report.findings
        if f.rule == "axes" and f.detail.get("files") == ["alias_map_v1.tsv"]
    ]
    assert [f.severity for f in passed_over] == ["warn"], report.render()


async def test_adjudications_load_in_their_real_per_title_shape(db, vocab_dir):
    """§6.6's editor writes the file back, so every shipped column must survive the round trip."""
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
    """`ON CONFLICT (version, term)` would keep one verdict per term; the collision is written here."""
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

    # The vocabulary must have loaded, or this passes on a loader that reads nothing.
    assert await db.fetchval("SELECT count(*) FROM dna_term WHERE version = 'v1'") == len(fx.VOCAB)
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 0
    assert any(f.severity == "warn" for f in report.findings)


async def test_an_empty_adjudications_ledger_is_refused_rather_than_replacing_the_verdicts(
    db, vocab_dir
):
    """This loader DELETEs first, so an empty file would clear every stored verdict."""
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 2

    (vocab_dir / "adjudications_v1.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n", encoding="utf-8"
    )
    second = ImportReport()

    await dna.load_adjudications(db, vocab_dir, "v1", second)

    assert second.ok, "an empty ledger is not a failed bundle; it is a refusal to replace"
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 2
    findings = [f for f in second.findings if f.rule == "adjudications"]
    assert [f.severity for f in findings] == ["warn"], second.render()
    assert findings[0].detail == {"stored": 2}
    assert "left in place" in findings[0].message, findings[0].message


async def test_the_adjudications_ledger_is_written_under_the_version_the_caller_names(
    db, vocab_dir
):
    """On a models-only import no vocabulary run supplies a version; it must thread from the caller."""
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v2', 0, 0)"
    )
    (vocab_dir / "adjudications_v2.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        "title\t4\tmood.cosy\tdrop\t\t\ttrakt:comment\tthe v2 ledger\n",
        encoding="utf-8",
    )
    second = ImportReport()

    await dna.load_adjudications(db, vocab_dir, "v2", second)

    assert second.ok, second.render()
    rows = await db.fetch(
        "SELECT version, title_id FROM dna_adjudication ORDER BY version, title_id NULLS LAST"
    )
    assert [(r["version"], r["title_id"]) for r in rows] == [("v1", 1), ("v1", None), ("v2", 4)]


async def test_a_latin_1_byte_in_the_adjudications_ledger_is_a_report_line_not_an_exception(
    db, vocab_dir
):
    """The largest hand-edited ledger, read inside the import transaction."""
    (vocab_dir / "adjudications_v1.tsv").write_bytes(
        b"scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        + "title\t1\tmood.cosy\tdrop\t\tune com\xe9die\ttrakt:comment\t\n".encode("latin-1")
    )
    report = ImportReport()

    await dna.load_adjudications(db, vocab_dir, "v1", report)

    failures = [f for f in report.failures if f.rule == "adjudications"]
    assert len(failures) == 1, report.render()
    assert "adjudications_v1.tsv" in failures[0].message
    assert "UTF-8" in failures[0].message
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 0


async def test_the_extracted_tier_loads_with_its_evidence_at_the_upstream_keying(
    db, vocab_dir, content_db
):
    """Upstream `dna_tag` has no `id`; its PK is (title_id, term), and evidence is keyed the same way."""
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
    # `runs_found` is a weight, never a filter (rule 2).
    assert rows[1]["n_sources"] == 3


async def test_the_projected_tier_keeps_n_sources_as_a_weight(db, vocab_dir, content_db):
    """Upstream `n_sources`/`sources` land in `weight`/`via`."""
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


async def test_the_shipped_extraction_label_becomes_a_report_note_not_data(
    db, vocab_dir, content_db
):
    """The vocabulary facet lands; the extraction label becomes
    a report note, since everything keys on the facet."""
    labelled = [row for row in fx.EXTRACTED if row[2] in fx.EXTRACTION_LABELS.values()]
    assert labelled, "the fixture must ship extraction labels or this test asserts nothing"

    await _seed_titles(db)
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    await dna.load_tags(db, content_db, "v1", report)
    await dna.load_projected(db, content_db, "v1", report)

    # The vocabulary facet, on every row of both tiers, joinable to `dna_facet`.
    for table in ("dna_tag", "dna_projected"):
        stored = await db.fetch(f"SELECT term, facet FROM {table} WHERE version = 'v1'")
        assert stored
        assert all(r["facet"] == r["term"].split(".", 1)[0] for r in stored), table
        assert not {r["facet"] for r in stored} & set(fx.EXTRACTION_LABELS.values()), table

    # Counted as a NOTE: nothing is wrong with the bundle, and §10 wants the rewrite's size stated.
    validation = ImportReport()
    validator.validate_content(content_db, validation)
    counted = [
        f for f in validation.findings
        if f.rule == "rule1-two-tiers" and "extraction label" in f.message
    ]
    assert [f.severity for f in counted] == ["note"]
    assert counted[0].detail == {
        "dna_tag": len(labelled),
        "dna_projected": len([r for r in fx.PROJECTED if r[2] in fx.EXTRACTION_LABELS.values()]),
    }
    assert "is imported and the label is not stored" in counted[0].message


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
    """The corpus exports no provider column, so the UNIQUE including `provider` never fired."""
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


async def test_loading_the_corrections_ledger_twice_leaves_one_copy(db, bundle_dir):
    """The loader's own idempotency; the importer's re-import path is `test_bundle_lifecycle.py`'s."""
    report = ImportReport()
    path = bundle_dir / "artifacts" / "corrections_v1.tsv"

    await dna.load_corrections(db, path, report)
    await dna.load_corrections(db, path, report)

    row = await db.fetchrow("SELECT title_id, field, new_value, evidence FROM credit_correction")
    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert (row["title_id"], row["field"], row["new_value"]) == (8, "composer", "Kunihiko Murai")
    assert row["evidence"] == "https://example.invalid/tampopo"

    # Guard 2 on this ledger too: a bare header must not delete the installed corrections.
    header = path.read_text(encoding="utf-8").splitlines()[0]
    path.write_text(header + "\n", encoding="utf-8")

    await dna.load_corrections(db, path, report)

    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1, (
        "a ledger that parses to nothing cleared the rows it could not replace"
    )
    empty = [f for f in report.findings if "parses to no corrections" in f.message]
    assert len(empty) == 1 and empty[0].severity == "warn", report.render()


async def test_the_corrections_note_says_the_ledger_is_stored_and_applied_nowhere(db, bundle_dir):
    """The derive applies corrections, not the import; the note
    must say neither "nothing applies them" nor "re-applied"."""
    report = ImportReport()

    await dna.load_corrections(db, bundle_dir / "artifacts" / "corrections_v1.tsv", report)

    notes = [f for f in report.findings if f.rule == "corrections" and f.severity == "note"]
    assert len(notes) == 1
    assert "§8 stage 3's derive applies them" in notes[0].message, notes[0].message
    assert "derive/ledgers.py" in notes[0].message, (
        "the note names a stage but not the applier, so nobody can go and read it"
    )
    assert "only after its title is next derived" in notes[0].message, notes[0].message
    # Refused over `render()`, which is what an operator reads.
    assert "nothing applies them" not in report.render(), report.render()
    assert "re-applied at derive" not in report.render(), report.render()
    # What THIS import applied, which is still zero.
    assert notes[0].detail["applied"] == 0
    assert notes[0].detail["corrections"] == 1
    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert await db.fetchval("SELECT count(*) FROM credit") == 0, (
        "the ledger is stored, not applied — nothing here may write a credit row"
    )


async def test_a_models_only_re_import_keeps_the_correction_the_household_typed(db, bundle_dir):
    """Decision 326: a household correction survives a models-only
    re-import; the bundle's rows are still replaced."""
    path = bundle_dir / "artifacts" / "corrections_v1.tsv"
    await dna.load_corrections(db, path, ImportReport())
    await db.execute(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin) "
        "VALUES (1, 'composer', 'Elliot Goldenthal', 'the disc sleeve', 'typed here', 'household')"
    )

    # One row changed and one added, so "replaced" is visible rather than inferred from a count.
    path.write_text(
        "kind\ttitle_id\tvalue\tevidence\tnote\n"
        "composer\t8\tKunihiko Murai\thttps://example.invalid/tampopo\tre-exported\n"
        "composer_add\t2\tJohann Johannsson\thttps://example.invalid/prisoners\tuncredited\n",
        encoding="utf-8",
    )
    report = ImportReport()

    await dna.load_corrections(db, path, report)

    rows = await db.fetch(
        "SELECT title_id, field, new_value, note, origin FROM credit_correction "
        "ORDER BY origin, title_id"
    )
    assert [(r["title_id"], r["field"], r["origin"]) for r in rows] == [
        (2, "composer_add", "bundle"), (8, "composer", "bundle"), (1, "composer", "household")
    ], "the re-import wiped the household's correction, or failed to replace the bundle's"
    household = rows[2]
    assert (household["new_value"], household["note"]) == ("Elliot Goldenthal", "typed here"), (
        "the household row survived the DELETE and was then overwritten by the INSERT"
    )
    # The count is the ledger this bundle carried, not the table.
    note = next(f for f in report.findings if f.rule == "corrections" and f.severity == "note")
    assert note.detail["corrections"] == 2, note.message


async def test_a_models_only_re_import_keeps_the_verdict_the_household_typed(db, vocab_dir):
    """The same scope on `dna_adjudication`: a household verdict would otherwise be deleted for good."""
    await _seed_titles(db)
    await dna.load_vocabulary(db, vocab_dir, "v1", ImportReport())
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, note, origin) "
        "VALUES ('v1', 'title', 1, 'mood.dread', 'drop', 'we watched it', 'household')"
    )

    # A different title and no `global` rule, so both under- and over-deletion are visible.
    (vocab_dir / "adjudications_v1.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        "title\t2\tmood.cosy\tdrop\t\t\ttrakt:comment\tre-exported\n",
        encoding="utf-8",
    )

    await dna.load_adjudications(db, vocab_dir, "v1", ImportReport())

    rows = await db.fetch(
        "SELECT title_id, term, verdict, note, origin FROM dna_adjudication "
        "ORDER BY origin, term"
    )
    assert [(r["title_id"], r["term"], r["origin"]) for r in rows] == [
        (2, "mood.cosy", "bundle"), (1, "mood.dread", "household")
    ], "the re-import wiped the household's verdict, or failed to replace the bundle's"
    assert rows[1]["note"] == "we watched it", "the household row was overwritten, not kept"


async def test_a_curated_row_stored_before_the_provenance_column_reads_as_the_bundles(
    db, bundle_dir, vocab_dir
):
    """0026's DEFAULT 'bundle' backfill is what makes the scoped
    DELETE safe; this passes against the unscoped one too."""
    await _seed_titles(db)
    await dna.load_vocabulary(db, vocab_dir, "v1", ImportReport())
    # Written the way every build up to 0026 wrote them: no `origin` in the column list.
    await db.execute(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence) "
        "VALUES (8, 'composer', 'somebody upstream corrected', 'https://example.invalid/old')"
    )
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict) "
        "VALUES ('v1', 'title', 1, 'mood.stale', 'drop')"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM credit_correction WHERE origin = 'bundle'"
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM dna_adjudication WHERE origin = 'bundle' AND term = 'mood.stale'"
    ) == 1

    await dna.load_corrections(db, bundle_dir / "artifacts" / "corrections_v1.tsv", ImportReport())
    await dna.load_adjudications(db, vocab_dir, "v1", ImportReport())

    assert await db.fetchval(
        "SELECT count(*) FROM credit_correction WHERE new_value = 'somebody upstream corrected'"
    ) == 0, "a pre-0026 correction outlived the re-import that owns it"
    assert await db.fetchval(
        "SELECT count(*) FROM dna_adjudication WHERE term = 'mood.stale'"
    ) == 0, "a pre-0026 verdict outlived the re-import that owns it"
    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 2


def test_both_curated_ledger_statements_name_the_origin_they_may_replace():
    """Read off the SQL text: the behaviour tests need a household row no bundle can carry."""
    source = Path(dna.__file__).read_text(encoding="utf-8")

    deletes = [
        line.strip() for line in source.splitlines()
        # Comment lines are excluded: decision 171's quoted probe names the unscoped form.
        if not line.lstrip().startswith("#")
        and ("DELETE FROM credit_correction" in line or "DELETE FROM dna_adjudication" in line)
    ]
    assert len(deletes) == 2, deletes
    for statement in deletes:
        assert "origin = 'bundle'" in statement, (
            f"an unscoped curated-ledger DELETE is back: {statement} (decisions 171 and 326)"
        )

    inserts = [
        block for block in source.split("await conn.executemany(")[1:]
        if "INSERT INTO credit_correction" in block.split(")")[0]
        or "INSERT INTO dna_adjudication" in block.split(")")[0]
    ]
    assert len(inserts) == 2, [block[:80] for block in inserts]
    for block in inserts:
        head = block.split("rows,")[0]
        assert "origin" in head and "'bundle'" in head, (
            f"a curated-ledger INSERT leaves `origin` to the column default: {head.strip()}"
        )


async def test_every_shipped_dna_table_is_loaded_or_skipped_with_a_reason(
    db, vocab_dir, content_db
):
    """The list comes from the shape manifest, so a table the corpus adds cannot slip through."""
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


def _strip_axis_definitions(vocab_dir: Path) -> None:
    """No authored axis beside the vocabulary files nor in the old `axes/` subdirectory."""
    for facet in fx.AXES:
        (vocab_dir / f"{facet}.tsv").unlink(missing_ok=True)
    shutil.rmtree(vocab_dir / "axes", ignore_errors=True)


async def test_a_bundle_with_no_axis_artifact_loads_and_the_report_says_what_is_off(db, vocab_dir):
    """Decision 173: no axes ship; the report must say the facet split is off, not fail or stay quiet."""
    _strip_axis_definitions(vocab_dir)
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, "a bundle with no axes is a legal bundle (decision 173)"
    assert await db.fetchval("SELECT count(*) FROM dna_axis") == 0
    warnings = [f for f in report.findings if f.rule == "axes" and f.severity == "warn"]
    assert len(warnings) == 1, report.render()
    assert "§6.2 step 5" in warnings[0].message, warnings[0].message
    assert "facet split" in warnings[0].message, warnings[0].message
    assert "Map" in warnings[0].message, "the Map surface's half of the gap is still true"
    # The line is not conditional on there being something to count.
    assert "authored axis definition" in report.render()


async def test_an_axis_tsv_beside_the_vocabulary_files_is_the_one_that_loads(db, vocab_dir):
    """The exporter does not descend into subdirectories, so axes must sit beside the vocabulary files."""
    _strip_axis_definitions(vocab_dir)
    (vocab_dir / "visual.tsv").write_text(
        "murky\tluminous\nvisual.neon\t0.75\nvisual.grainy\t-0.5\n", encoding="utf-8"
    )
    # The subdirectory is not a second supported location.
    (vocab_dir / "axes").mkdir(exist_ok=True)
    (vocab_dir / "axes" / "mood.tsv").write_text(
        "heavy\tlight\nmood.dread\t-1.0\n", encoding="utf-8"
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    rows = await db.fetch("SELECT facet, left_pole, right_pole FROM dna_axis")
    assert [(r["facet"], r["left_pole"], r["right_pole"]) for r in rows] == [
        ("visual", "murky", "luminous")
    ], "the axes/ subdirectory is read, or the file beside the vocabulary is not"
    weights = await db.fetch("SELECT term, weight FROM dna_axis_weight ORDER BY term")
    assert [(r["term"], round(r["weight"], 3)) for r in weights] == [
        ("visual.grainy", -0.5), ("visual.neon", 0.75)
    ]


async def test_the_pacing_coordinates_file_is_not_read_as_an_axis_definition(db, vocab_dir):
    """The pacing coordinates file must be passed over in silence, neither loaded nor warned about."""
    columns = SHAPES["tsv"]["artifacts/dna_vocab/v1/vocab_pacing_axes_v1.tsv"]
    assert columns[0] == "id" and len(columns) > 2, "this file opens with column names, not poles"
    (vocab_dir / "vocab_pacing_axes_v1.tsv").write_text(
        "\t".join(columns) + "\npacing.patient\t0.1\t0.2\t0.3\t0.4\t0.5\t\n", encoding="utf-8"
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    facets = {r["facet"] for r in await db.fetch("SELECT facet FROM dna_axis")}
    assert facets == set(fx.AXES), facets
    pacing = await db.fetchrow("SELECT left_pole, right_pole FROM dna_axis WHERE facet = 'pacing'")
    assert (pacing["left_pole"], pacing["right_pole"]) == fx.AXES["pacing"][:2]
    # `load_vocabulary` already notes this file; the axis rule must not add a second line.
    named = [
        f.message for f in report.findings
        if f.rule == "axes" and "vocab_pacing_axes_v1.tsv" in f.message
    ]
    assert not named, f"a vocabulary artifact was reported as a misnamed axis: {named}"


async def test_a_re_authored_axis_that_drops_a_term_drops_its_weight(db, vocab_dir):
    """Decision 261: a re-authored axis replaces its facet's weights, so a dropped term loses its weight."""
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    left, right = fx.AXES["mood"][:2]
    before = await db.fetch(
        "SELECT term FROM dna_axis_weight WHERE facet = 'mood' ORDER BY term"
    )
    assert len(before) > 1, "the fixture axis has to carry more than one term to shrink"
    kept, dropped = before[0]["term"], before[-1]["term"]

    (vocab_dir / "mood.tsv").write_text(
        f"{left}\t{right}\n{kept}\t0.25\n", encoding="utf-8"
    )
    second = ImportReport()

    await dna.load_axes(db, vocab_dir, "v1", second)

    assert second.ok, second.render()
    rows = await db.fetch(
        "SELECT term, weight FROM dna_axis_weight WHERE facet = 'mood' ORDER BY term"
    )
    assert [(r["term"], r["weight"]) for r in rows] == [(kept, 0.25)], (
        f"{dropped} kept its installed weight after the bundle stopped carrying it"
    )
    # The other facets are untouched: the clear is the re-authored facet's, not the version's.
    assert await db.fetchval(
        "SELECT count(*) FROM dna_axis_weight WHERE facet <> 'mood'"
    ) > 0, "clearing one facet's weights emptied the others"


async def test_an_axis_file_that_parses_to_no_weight_leaves_the_facets_weights_standing(
    db, vocab_dir
):
    """Decision 264: a header-only or unreadable-weights file must not clear the facet's weights."""
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    left, right = fx.AXES["mood"][:2]
    installed = [
        (r["term"], r["weight"]) for r in await db.fetch(
            "SELECT term, weight FROM dna_axis_weight WHERE facet = 'mood' ORDER BY term"
        )
    ]
    assert installed, "the fixture ships mood weights for this to have something to lose"

    for body in (f"{left}\t{right}\n", f"{left}\t{right}\nmood.dread\tnot-a-number\n"):
        (vocab_dir / "mood.tsv").write_text(body, encoding="utf-8")
        second = ImportReport()

        await dna.load_axes(db, vocab_dir, "v1", second)

        assert second.ok, second.render()
        rows = [
            (r["term"], r["weight"]) for r in await db.fetch(
                "SELECT term, weight FROM dna_axis_weight WHERE facet = 'mood' ORDER BY term"
            )
        ]
        assert rows == installed, f"mood.tsv parsing to nothing cleared the facet: {rows}"
        kept = [
            f for f in second.findings
            if f.rule == "axes" and "left in place rather than replaced" in f.message
        ]
        assert len(kept) == 1, second.render()
        assert kept[0].severity == "warn" and "mood.tsv" in kept[0].message, kept[0].message
        assert kept[0].detail.get("stored") == len(installed), kept[0].detail
        # The count used to include the file that loaded nothing.
        loaded = next(f for f in second.findings if "authored axis definition(s) loaded" in f.message)
        assert loaded.detail["facets"] == len(fx.AXES) - 1, loaded.message


async def test_an_axis_named_for_something_that_is_not_a_facet_is_reported_rather_than_raised(
    db, vocab_dir
):
    """`axis_mood_v1.tsv` would name a facet `axis_mood_v1`; the FK refusal must be a report line."""
    (vocab_dir / "axis_mood_v1.tsv").write_text(
        "heavy\tlight\nmood.dread\t-1.0\n", encoding="utf-8"
    )
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    warnings = [f for f in report.findings if f.rule == "axes" and f.severity == "warn"]
    assert len(warnings) == 1, report.render()
    assert "axis_mood_v1.tsv" in warnings[0].message, warnings[0].message
    assert {r["facet"] for r in await db.fetch("SELECT facet FROM dna_axis")} == set(fx.AXES)


async def test_the_axis_loader_reads_the_installed_facets_when_no_vocabulary_ran(db, vocab_dir):
    """With no vocabulary run, the loader reads `dna_facet`, the only set the FK accepts."""
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    await db.execute("DELETE FROM dna_axis_weight")
    await db.execute("DELETE FROM dna_axis")
    second = ImportReport()

    await dna.load_axes(db, vocab_dir, "v1", second)

    assert second.ok, second.render()
    assert {r["facet"] for r in await db.fetch("SELECT facet FROM dna_axis")} == set(fx.AXES)
    named = [f.message for f in second.findings if f.rule == "axes" and f.severity == "warn"]
    assert not named, named


async def test_the_data_card_names_the_paths_the_axis_loader_actually_reads(db, vocab_dir):
    """The card builds its path from the loader's rule; that path must load."""
    card = await admin_api.data_sources(None, db)
    axes = card["axes"]

    assert axes["expected"], "the card names nothing for an operator to author"
    assert not any("axes/" in p for p in axes["expected"]), (
        "the card still sends the operator to the subdirectory decision 173 retired"
    )
    assert any("§6.2 step 5" in line for line in axes["disables"]), axes["disables"]

    _strip_axis_definitions(vocab_dir)
    named = PurePosixPath(axes["expected"][0])
    (vocab_dir / named.name).write_text(
        f"left\tright\n{named.stem}.example\t-1.0\n", encoding="utf-8"
    )
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    loaded = [r["facet"] for r in await db.fetch("SELECT facet FROM dna_axis")]
    assert loaded == [named.stem], f"the card names {named}, the loader loaded {loaded}"
