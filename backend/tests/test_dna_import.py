"""The naming layer, loaded from the files the corpus actually ships. Spec v2.1 §4.1, §4.3, §10.

Three rows live here.

`data-rules-vocabulary-layer-loads-the-files-the-corpus-ships` — the vocabulary loader read
`terms.tsv`, `aliases.tsv` and `adjudications.tsv`, none of which appear in a bundle. The corpus
ships per-facet `vocab_<facet>_v1.tsv`, `alias_map_v1.tsv` and an `adjudications_v1.tsv` keyed
per TITLE. The per-title keying is the sharp one: `ON CONFLICT (version, term) DO UPDATE`
collapses 817 per-title verdicts onto one row per term, silently and in the direction that
loses data.

`data-rules-corrections-ledger-parsed-at-its-real-header` — the shipped header is
`kind, title_id, value, evidence, note`; the loader read `r["field"]`, so a real bundle raised
`KeyError` where §10 promises a report.

`data-rules-a-models-only-import-loads-the-curated-ledgers-it-carries` — M4.14's half of this
file. Decision 247 puts the four curated ledgers back on the path a models-only re-import takes,
and the three guards that ruling travels with are asserted here, at the loaders: the onboarding
list is replaced rather than merged, an entry naming a title this install never seeded is skipped
and counted rather than aborting the whole import on `seed_list.title_id`'s NOT NULL foreign key,
and a ledger that parses to nothing does not replace one that did. Real volumes behind those
numbers: `corrections_v1.tsv` 6 rows, `adjudications_v1.tsv` 828 (817 title-scoped),
`seed_list.json` 100 entries. The lifecycle half — a models-only import running end to end —
lives in `test_bundle_lifecycle.py`; every ledger test here builds its own files, because no real
model bundle exists to assert against.

Table shapes below are asserted against `fixtures/real_bundle_shapes.json` — the committed,
data-free manifest of a real bundle — rather than against this repo's reading of §10, which is
the reading that produced the invented file names in the first place.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from spielplan.api import admin as admin_api
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


def test_a_latin_1_byte_in_the_corrections_ledger_is_a_report_line_not_an_exception(tmp_path):
    """§10 promises the operator a migration report, and `/validate` promises it writes nothing;
    one latin-1 byte in this file left both as a `UnicodeDecodeError` out of a route, with no
    finding recorded at all. It is a hand-edited six-row ledger, which makes it exactly the file
    a stray byte reaches, and `validate._read_tsv` is now the one opener of a curated TSV in the
    importer — this is one of its call sites. [M4.14 step B2, finding 2.12]
    """
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


async def test_a_shorter_onboarding_list_leaves_no_tail_of_the_old_one(db, bundle_dir, tmp_path):
    """Decision 247: the bundle's copy of a curated ledger is the whole truth.

    `ON CONFLICT (position) DO UPDATE` with no preceding clear is a merge wearing an upsert's
    clothes. Executed against the fixture: a three-entry list loaded over its eight left eight
    rows, positions 3-7 still naming the old ids — and `rate/queue.py` joins the whole table
    and counts the whole table, so §6.1's first run would have offered a merge of two onboarding
    lists. A list that grows produces the same row count either way, which is why nothing noticed
    for five milestones; the shrink is the only shape that shows it. The clear has to land before
    decision 247 wires this loader into the model path, or the wiring ships the merge.
    [M4.14 step C1, finding 2.15]
    """
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
    """Decision 247 guard 1. `seed_list.title_id` is a NOT NULL foreign key to `title(id)`
    (`0003_content.sql`), so one entry naming a title this install never seeded aborts the entire
    import on a violation that names a constraint rather than a file.

    Decision 248 makes that case ordinary rather than exotic: the corpus's catalogue is not frozen
    at this install's seed — its own `sqlite_sequence` reads title 21442 against the 19,071 a
    bundle exports — so a later bundle's 100-entry onboarding list can perfectly well name a title
    this household never acquired. §6.1 wants the titles it can offer; the count is what keeps the
    shortfall from passing as a full list. [M4.14 step C1, finding 2.15]
    """
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
    """Decision 260: guard 2 belongs to the third DELETE-first ledger as much as to the second.

    `load_corrections` returns before its DELETE when `parse_corrections` yields nothing and
    `load_adjudications` does the same with a warn naming the stored count. This loader is the
    one decision 247 newly put on the RECURRING path, and it is the one that gained a DELETE this
    milestone - so a `seed_list.json` present and parsing to zero usable entries (`[]`, or
    `{"titles": []}`, which is what a half-finished upstream export writes) deleted the install's
    onboarding list, wrote nothing back, and said so only as a note reading "0-title
    decade-stratified seed list loaded". Measured on this fixture: eight rows to zero, report ok.

    `rate/queue.py` counts this table for section 6.1's first-run queue and LEFT JOINs it for the
    ordering, so the household's onboarding seed silently becomes the P(seen) fallback.
    [M4.14 cycle 1, M414-REV-247-01, decision 260]
    """
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
    """Decision 260's guard tested `rows`, which is what guard 1 LEFT rather than what parsed.

    A `seed_list.json` that parsed perfectly and named only titles this install never seeded -
    decision 248's ordinary case taken to its limit, a corpus that renumbered its catalogue -
    therefore took the empty-ledger branch and reported "parses to no onboarding titles",
    byte-identical in message and detail to the line a genuinely empty export gets, and returned
    above guard 1's own warn so the skipped count and the offending ids went with it. The two
    have different remedies: re-export the file, or seed the titles it names.
    [M4.14 cycle 4, m414-c4-dim247-04]
    """
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
    # The count and the ids guard 1 owes, which the empty-ledger branch dropped, and the
    # no-replacement fact guard 2 owes -- one line carrying both, because both are true.
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
    """`_decade`'s docstring promises that "a title whose year the corpus never resolved keeps a
    NULL decade rather than failing the bundle", and it kept that promise for `null` only.

    `NaN` is how an unresolved year arrives from a frame - `json.dumps` writes the bare literal
    and `json.loads` accepts it - and it raised ValueError, which is not an
    `asyncpg.PostgresError`, a `sqlite3.DatabaseError` or an `OSError`, so `import_bundle`'s
    named-refusal arm could not see it, `except BaseException` re-raised, and the operator's
    report line was a Python type name for a defect in a named file - after the 1.04 GB copy, for
    a refusal `/validate` had just said would not happen. `Infinity` raised OverflowError the same
    way, and an epoch stamp in the field derived a decade outside `seed_list.decade`'s smallint
    and reached asyncpg as a DataError reported as "no rule in this importer named this refusal
    first". Three spellings of the one case the docstring already covers.
    [M4.14 cycle 4, m414-c4-dim247-03]
    """
    await _seed_titles(db)
    path = tmp_path / "seed_list.json"
    # Written as TEXT and not through `json.dumps`, because the bare `NaN` and `Infinity` literals
    # are the whole point: `json.dumps` emits them and `json.loads` accepts them by default, which
    # is how an unresolved year leaves a frame and arrives in this file.
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
    # §6.1 still wants the title in the queue: a hole in the stratification is a smaller loss
    # than a refused seed, which is the trade `_decade` was always documented to make.
    assert len(rows) == 4
    note = next(f for f in report.findings if f.rule == "seed-list" and f.severity == "note")
    assert note.detail["undated"] == 3, note.message


async def test_an_absent_curated_ledger_names_what_is_still_installed(db, bundle_dir, tmp_path):
    """Decision 247: "an absent ledger file stays a warning that changes nothing" - and each of
    the three warnings said something had changed.

    All three loaders return before they touch a row, so on the models-only path decision 247
    newly put them on, the install is byte-identical afterwards. The sentences were written for a
    SEED, where the install holds nothing, and read as statements about it: the sharpest is the
    onboarding list's, because `rate/queue.py` LEFT JOINs the table, orders on `s.seed_position
    ASC NULLS LAST` ahead of `p_seen DESC` and prints "seed list position N of M" on the very
    next card - so "the first rating queue falls back to P(seen) ordering alone" is the inverse
    of what happens, and the operator's remedy is work against a defect that does not exist.
    [M4.14 cycle 4, m414-c4-dim247-02, decisions 247 and 266]
    """
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
    """The warning decision 266's own Cost paragraph names - "false of an install whose weights
    are intact - the same defect from the other side" - and leaves standing.

    Decision 247's step C2 put `load_axes` on the models-only path, where the install can already
    hold weights and every declining branch in the loop leaves them exactly where they are. The
    `if not loaded` warn then asserted three facts about the INSTALL it had not read: that the Map
    has no axes to plot, that `session_result.conflict` is NULL on every evening, and that 54c's
    tie-break is 0.0 for every pair. The consequences are kept for the install that has them,
    which is the stored count coming back 0 - decision 173 makes that the shipped state.
    [M4.14 cycle 4, m414-c4-dim247-01, decisions 247 and 266]
    """
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
    assert "conflict is NULL" not in warned[0].message, warned[0].message

    # And the install that HAS those consequences is still told about them, because that is the
    # line an operator has to act on and decision 173 makes it the shipped state.
    await db.execute("DELETE FROM dna_axis_weight WHERE version = 'v1'")
    bare = ImportReport()
    await dna.load_axes(db, vocab, "v1", bare)
    axis_warn = next(f for f in bare.findings if f.rule == "axes" and f.severity == "warn")
    assert "no axes to plot" in axis_warn.message, axis_warn.message
    assert "session_result.conflict is NULL" in axis_warn.message


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


async def test_a_latin_1_byte_in_the_alias_map_is_a_report_line_not_an_exception(db, vocab_dir):
    """The same clause one file over, and the reason it is asserted separately: a reader that
    raises stops the enumeration, so everything the naming layer would have said after this file
    is lost as well. The vocabulary, the adjudications and the axes all still load, and the alias
    map is the only thing missing — which is what a report is for.

    This case found a FOURTH unguarded reader, which is why it asserts about two loaders. `load
    _axes` globs every `*.tsv` beside the vocabulary files — that is its candidate rule, and the
    pole header is how it tells an axis from its neighbours — so it opens the alias map too, and
    the byte reached it after `_load_aliases` had already reported the file. It is not that
    loader's file to diagnose, so it says only that it did not read it as an axis; what it may
    not do is raise, which ended `load_vocabulary` with the naming layer half loaded.
    [M4.14 step B2, finding 2.12]
    """
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


async def test_an_empty_adjudications_ledger_is_refused_rather_than_replacing_the_verdicts(
    db, vocab_dir
):
    """Decision 247 guard 2: omission may not be destructive.

    `parse_corrections` has refused to let a ledger that parses to nothing pass as a silent zero
    since M4.5, and this loader did not — and the asymmetry is sharper here, because this one
    DELETEs before it inserts. A present-but-empty `adjudications_v1.tsv`, which is the shape a
    half-finished upstream export writes, therefore cleared the stored verdicts and wrote none:
    828 of them on the shipped bundle, 817 scoped to a single title, with the report saying "0 DNA
    adjudications loaded" and nothing at all saying what had been there. §14.5's scar is a derive
    that fails to re-apply these; this was the import deleting them itself.
    [M4.14 step C2, finding 2.15]
    """
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
    """Decision 247 guard 3: the version is the caller's and never a literal.

    On a models-only import there is no `load_vocabulary` run to hand this loader a version, and
    `bundle.py`'s `or "v1"` was the only thing standing in for one — so the ledgers of an install
    on any other vocabulary would have been filed under a version it is not on, or refused by
    `dna_adjudication.version`'s foreign key to `dna_vocabulary` in the middle of the transaction.
    Decision 256 makes `bundle.py` refuse rather than default; this is the half that makes the
    refusal worth making, by proving the version threads all the way to the row. The DELETE is
    scoped the same way, so loading v2 leaves v1's verdicts alone. [M4.14 step C2, decision 247]
    """
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
    """The third of `validate._read_tsv`'s readers in this module. 828 hand-edited rows is the
    largest of the curated ledgers and the one most likely to carry a stray byte; it raised where
    §10 promises a report, and it raised INSIDE the import transaction.
    [M4.14 step B2, finding 2.12]
    """
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


async def test_the_shipped_extraction_label_becomes_a_report_note_not_data(
    db, vocab_dir, content_db
):
    """§4.3 + §10: the corpus's two namings, one of which the app keys on.

    The corpus keys a vocabulary id as `characters.amateur_sleuth` and files the extraction pass
    that found the tag under its own label — `character_dynamics`, `mood_tone`,
    `narrative_themes`. Both are correct upstream and they are not the same name for the same
    thing, so the app has to choose: `dna_facet`, `dna_term`, §6.4's axes and §6.8's palette all
    key on the vocabulary facet, so that is what lands and the extraction label is counted into
    the migration report instead. 29,188 of 31,540 `dna_tag` rows and 206,151 of 223,136
    `dna_projected` rows carry the label on the shipped bundle; the fixture ships the three
    measured labels plus the identical remainder.
    """
    labelled = [row for row in fx.EXTRACTED if row[2] in fx.EXTRACTION_LABELS.values()]
    assert labelled, "the fixture must ship extraction labels or this test asserts nothing"

    await _seed_titles(db)
    report = ImportReport()
    await dna.load_vocabulary(db, vocab_dir, "v1", report)
    await dna.load_tags(db, content_db, "v1", report)
    await dna.load_projected(db, content_db, "v1", report)

    # The data: the vocabulary facet, on every row of both tiers, joinable to `dna_facet`.
    for table in ("dna_tag", "dna_projected"):
        stored = await db.fetch(f"SELECT term, facet FROM {table} WHERE version = 'v1'")
        assert stored
        assert all(r["facet"] == r["term"].split(".", 1)[0] for r in stored), table
        assert not {r["facet"] for r in stored} & set(fx.EXTRACTION_LABELS.values()), table

    # The report: the validator counts the rows the two namings disagree on, per tier, as a NOTE
    # — nothing is wrong with the bundle, and §10 wants the size of the rewrite stated.
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


async def test_loading_the_corrections_ledger_twice_leaves_one_copy(db, bundle_dir):
    """The LOADER's idempotency, which is not the importer's re-import path and was named as if
    it were.

    The old name — "is not duplicated by a re-import" — described a path `import_bundle` never
    took: `bundle.py` held `load_corrections` inside `if db is not None:`, a branch a models-only
    bundle never enters, so under decision 162 the re-import this test cited could not append the
    ledger because it did not load the ledger at all. The six shipped corrections were dropped on
    every re-import instead, with no line in the report, while a green test said the opposite.
    Decision 247 puts the loader back on the model path; this asserts what it asserts, which is
    that calling it twice over one file leaves one copy. The importer's half — a models-only
    import running end to end — is `test_bundle_lifecycle.py`'s.
    [M4.14 finding 2.15, decision 247]
    """
    report = ImportReport()
    path = bundle_dir / "artifacts" / "corrections_v1.tsv"

    await dna.load_corrections(db, path, report)
    await dna.load_corrections(db, path, report)

    row = await db.fetchrow("SELECT title_id, field, new_value, evidence FROM credit_correction")
    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert (row["title_id"], row["field"], row["new_value"]) == (8, "composer", "Kunihiko Murai")
    assert row["evidence"] == "https://example.invalid/tampopo"

    # And decision 247's guard 2 on this ledger, which is the one the count left out. `dna.py`'s
    # module docstring says "four ledgers, four guards", and three of the four had a registered
    # test loading a present-but-EMPTY file over installed rows: the adjudications ledger's
    # (guard 2), the onboarding list's (decision 260) and the axis TSV's (decision 264). This
    # one - the ledger the other three are argued FROM, and the one whose DELETE the others
    # copy - had none, so its `if not rows: return` could be moved behind the clear and the whole
    # suite stayed green while a half-finished export shipping a bare header deleted the six
    # curated credit fixes section 14.5's scar is about.
    # [M4.14 cycle 3, m414-c3-dim247-corrections-empty-guard-is-the-fourth]
    header = path.read_text(encoding="utf-8").splitlines()[0]
    path.write_text(header + "\n", encoding="utf-8")

    await dna.load_corrections(db, path, report)

    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1, (
        "a ledger that parses to nothing cleared the rows it could not replace"
    )
    empty = [f for f in report.findings if "parses to no corrections" in f.message]
    assert len(empty) == 1 and empty[0].severity == "warn", report.render()


async def test_the_corrections_note_says_the_ledger_is_stored_and_applied_nowhere(db, bundle_dir):
    """§8 stage 3 is M5's, so §10's report may not say the ledger was applied.

    The line read "N credit corrections loaded and re-applied at derive", and grep finds exactly
    two readers of `credit_correction`: this writer and `backup/movie_data.py`. Five of the
    shipped ledger's six rows are unreflected in the corpus's own `content.sqlite` either, so
    nothing upstream pre-applied them. §14.5 is the scar for a derive that does not re-apply
    them — "787 rows reverted twice" — and a report claiming the application already happens is
    how that scar gets earned a third time.

    The other half of the repair is a non-event: nothing patches `credit` at import. §8 stage 3
    owns the application, and a second implementation of it here would be the derive-disagrees-
    with-the-ledger failure in miniature. [M4.9 finding 32]
    """
    report = ImportReport()

    await dna.load_corrections(db, bundle_dir / "artifacts" / "corrections_v1.tsv", report)

    notes = [f for f in report.findings if f.rule == "corrections" and f.severity == "note"]
    assert len(notes) == 1
    assert "stored for §8 stage 3 (M5)" in notes[0].message
    assert "nothing applies them yet" in notes[0].message
    assert notes[0].detail["applied"] == 0
    assert "re-applied at derive" not in report.render()
    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert await db.fetchval("SELECT count(*) FROM credit") == 0, (
        "the ledger is stored, not applied — nothing here may write a credit row"
    )


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


# --- §6.4's axis definitions, in the directory an exporter can actually reach ------------


def _strip_axis_definitions(vocab_dir: Path) -> None:
    """Leave the bundle with no authored axis anywhere: neither beside the vocabulary files,
    where the loader now reads them, nor in the `axes/` subdirectory it used to read."""
    for facet in fx.AXES:
        (vocab_dir / f"{facet}.tsv").unlink(missing_ok=True)
    shutil.rmtree(vocab_dir / "axes", ignore_errors=True)


async def test_a_bundle_with_no_axis_artifact_loads_and_the_report_says_what_is_off(db, vocab_dir):
    """Decision 173: the corpus ships no axes and the release is not gated on them, so the
    importer's job here is to say what that costs — not to fail, and not to stay quiet.

    The warning named the Map surface and stopped there. §6.2 step 5 is the half a household
    actually meets: with `dna_axis_weight` empty, `tonight/dna.axes_for` returns `{}`,
    `combine.contested_facet` iterates zero axes and returns None, `session_result.conflict` is
    always NULL, and 54c's widest-axis tie-break is 0.0 for every pair. §14 risk 6 then watches
    a split rate that is a permanent 0 and says nothing about the households it is watching.
    """
    _strip_axis_definitions(vocab_dir)
    report = ImportReport()

    await dna.load_vocabulary(db, vocab_dir, "v1", report)

    assert report.ok, "a bundle with no axes is a legal bundle (decision 173)"
    assert await db.fetchval("SELECT count(*) FROM dna_axis") == 0
    warnings = [f for f in report.findings if f.rule == "axes" and f.severity == "warn"]
    assert len(warnings) == 1, report.render()
    assert "§6.2 step 5" in warnings[0].message, warnings[0].message
    assert "conflict" in warnings[0].message, warnings[0].message
    assert "Map" in warnings[0].message, "the Map surface's half of the gap is still true"
    # §10 wants counts, and a count of zero is the one the operator needs: the line is not
    # conditional on there being something to count.
    assert "authored axis definition" in report.render()


async def test_an_axis_tsv_beside_the_vocabulary_files_is_the_one_that_loads(db, vocab_dir):
    """Decision 173's operative half. `mdc/export_bundle.py::_export_vocab` copies the regular
    files of `data/dna_vocab/v1/` and skips subdirectories by construction, so an axis authored
    into `axes/` could never travel in a bundle at all. The loader was waiting on a path no
    exporter can fill, which is why five milestones read the gap as "upstream has not authored
    them yet" rather than as "this app looks somewhere a bundle cannot reach".
    """
    _strip_axis_definitions(vocab_dir)
    (vocab_dir / "visual.tsv").write_text(
        "murky\tluminous\nvisual.neon\t0.75\nvisual.grainy\t-0.5\n", encoding="utf-8"
    )
    # The subdirectory is not a second supported location. Keeping it readable would keep the
    # unreachable path alive, and an operator who authored into it would still ship nothing.
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
    """`vocab_pacing_axes_v1.tsv` now sits in the very directory the axis loader reads, and it
    is not an axis definition: seven named columns of per-term coordinates, no label, no gloss,
    no poles. Read as one it keys `dna_axis` on a facet named `vocab_pacing_axes_v1`, which
    `dna_axis`'s FK to `dna_facet` turns into a ForeignKeyViolation in the middle of the import
    transaction — and which §6.2 step 5 would otherwise print at a household as a raw facet id.
    Decision 173 refuses it twice, and both refusals are asserted here because they fail
    differently: the pole rule passes the file over in silence, and the facet check would have
    warned about it by name. A vocabulary artifact is not a malformed axis, and a directory of
    twenty of them would bury the one line an operator has to read under twenty that mean
    nothing — so the loader may not merely survive this file, it has to say nothing about it.

    This one guards the new rule rather than reproducing the old defect: the loader that shipped
    before decision 173 never looked in this directory, so nothing here could fail against it.
    Measured under sabotage rather than assumed. Relaxing the pole rule alone to `len(poles) >=
    2` — the bound the `axes/` reader used — lands the file on the facet check and fails the
    silence assertion below; relaxing both writes `dna_axis` with a facet named
    `vocab_pacing_axes_v1` and raises ForeignKeyViolationError mid-import.
    """
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
    # Scoped to the axis rule: `load_vocabulary` already notes this file, correctly, as "not a
    # facet vocabulary". What may not happen is a second line calling it a broken axis.
    named = [
        f.message for f in report.findings
        if f.rule == "axes" and "vocab_pacing_axes_v1.tsv" in f.message
    ]
    assert not named, f"a vocabulary artifact was reported as a misnamed axis: {named}"


async def test_a_re_authored_axis_that_drops_a_term_drops_its_weight(db, vocab_dir):
    """Decision 261: the fourth curated ledger replaces its facet's weights rather than merging.

    This module's docstring states the rule for all four of decision 247's ledgers - "each of
    them replaces what it finds rather than merging with it: the bundle's copy is the whole truth
    for its version, and a ledger that arrives shorter has to end shorter" - and `load_axes` was
    the one with no clear: `ON CONFLICT (version, facet, term) DO UPDATE` with nothing deleting
    the facet's existing rows first. So a term the corpus removes from an axis kept its installed
    weight for ever, at a version decision 163 pins across every re-import, and the report said
    nothing about it.

    `tonight/dna.axes_for` reads every row at the version, `combine.axis_position` sums over the
    terms a title and the axis share, and both the numerator and the engaged-weight denominator
    of `contested_facet` and 54c's widest-axis tie-break move with a weight that should be gone.
    Latent today, because decision 173 ships no authored axis - and asserted here so that it
    cannot stop being latent silently. [M4.14 cycle 1, M414-REV-247-02, decision 261]
    """
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
    """Decision 264: guard 2 on the fourth DELETE-first ledger, which is the one that had none.

    Decision 261 gave `load_axes` a per-facet `DELETE FROM dna_axis_weight` and no guard, and the
    comment defending it claimed the only two paths through the loop were the two that decline a
    file. There is a third and it is the one that writes: a `<facet>.tsv` opening with a valid
    two-pole header and carrying no usable weight row is neither `not_an_axis` nor "not a
    vocabulary facet", so it fell through, cleared the facet's installed weights, inserted none,
    counted itself in `loaded` and reported `ok`. That is the shape a truncated or de-authored
    upstream export takes, and under decision 163 the version never changes, so the loss can only
    be undone by a corrected export.

    The sibling test above shrinks the axis from two terms to one and never to zero, and
    `test_an_absent_curated_ledger_leaves_the_installed_rows_standing` unlinks the files, which
    makes the glob find nothing and the loop body never run. Neither reaches the file that is
    present and says nothing. Both shapes are asserted here - the header alone, and a body whose
    every weight is unreadable as a number - because they arrive at the DELETE by different
    routes and only one of them leaves a warn behind on the way.
    [M4.14 cycle 2, m414-c2-dim247-axes-empty-file-wipes-the-facet, decision 264]
    """
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
        # The count in the only line the report carries used to include the file that loaded
        # nothing, which is what made "3 authored axis definition(s) loaded" a true-looking
        # sentence about a facet that had just been emptied.
        loaded = next(f for f in second.findings if "authored axis definition(s) loaded" in f.message)
        assert loaded.detail["facets"] == len(fx.AXES) - 1, loaded.message


async def test_an_axis_named_for_something_that_is_not_a_facet_is_reported_rather_than_raised(
    db, vocab_dir
):
    """Decision 191's prose spells the artifact `axis_<facet>_v1.tsv`, and the stem is what keys
    `dna_axis` — so that spelling names a facet called `axis_mood_v1`. `dna_axis` carries
    `FOREIGN KEY (version, facet) REFERENCES dna_facet` (`0004_dna.sql:57`), so a stem the
    vocabulary does not know is a ForeignKeyViolation mid-transaction. §10 promises a report,
    and an uncaught exception is not one; the operator gets the file name and the reason.
    """
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
    """Decision 247 counts §6.4's axis TSVs among the four ledgers a models-only re-import loads,
    and on that bundle `load_vocabulary` does not run — so there is no freshly declared facet set
    to hand the axis loader, and it has to ask the install.

    `dna_axis` carries `FOREIGN KEY (version, facet) REFERENCES dna_facet` (`0004_dna.sql:57`),
    so `dna_facet` at this version is not merely a convenient source for that set, it is the only
    one the constraint will accept — which is why the lookup lives in the loader rather than
    at the call site. With an empty set every axis file in the directory would be warned about by
    name as "not a vocabulary facet" and `dna_axis` would stay empty, so this passes only if the
    installed facets were actually read. [M4.14 step C2, finding 2.15]
    """
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


SPEC_DOC = Path(__file__).resolve().parents[2] / "docs" / "spielplan-spec_v2.1.md"


def _axis_artifact_clause() -> str:
    """§6.4's "Axis definitions are a shipped, authored artifact" clause, to the sentence after."""
    text = SPEC_DOC.read_text(encoding="utf-8")
    start = text.index("**Axis definitions are a shipped, authored artifact**")
    return text[start:text.index("Deterministic", start)]


def test_the_spec_states_the_axis_filename_rule_the_loader_enforces(vocab_dir):
    """Decision 227. The rule the loader keeps has to live in the document a corpus author reads.

    Decision 173 moved the loader off the `axes/` subdirectory it invented, and the sketch behind
    that ruling spells the artifact `axis_<facet>_v1.tsv` -- which this loader reads as a facet
    called `axis_mood_v1`, warns about by name, and skips. The loader is right to (the sketch's own
    example stems are not `dna_tag.facet` values either, so it cannot be authored as written), but
    the rule it enforces was written down nowhere normative: a docstring, a fixture and the §6.6
    Data card, while §6.4 named no filename and the decision register named the other one. A corpus
    arriving under proposal 140 as `axis_mood_v1.tsv` imports zero `dna_axis` rows, so `axes_for`
    returns {}, `combine.contested_facet` returns None, and §6.2 step 5's split is dark on every
    evening -- with the operator told the file is misnamed by an app whose spec says otherwise.

    Asserted against the shipped fixture bundle as well as against the sentence, because a spec
    sentence nothing is authored to is the same defect one step along.
    [M4.12 review cycle 1: D3-01; decision 227]
    """
    clause = _axis_artifact_clause()

    assert "`<facet>.tsv`" in clause, f"§6.4 still states no filename rule: {clause}"
    assert "axis_mood_v1.tsv" in clause, (
        "the register's own spelling has to be named as the one this is not, or the next author "
        "takes it from decision 173 and the import writes nothing"
    )
    assert "vocab_pacing_axes_v1.tsv" in clause, "the neighbour that is not an axis definition"
    assert "axes/" not in clause, "the subdirectory decision 173 retired"

    shipped = {p.name for p in vocab_dir.glob("*.tsv")}
    assert {f"{facet}.tsv" for facet in fx.AXES} <= shipped, (
        f"the fixture bundle is not authored to the rule §6.4 now states: {sorted(shipped)}"
    )


async def test_the_data_card_names_the_paths_the_axis_loader_actually_reads(db, vocab_dir):
    """§6.6's Data card is where decision 191 put the outstanding authoring task, and it builds
    its path list from the loader's rule rather than restating it, because a hand-written list
    is exactly how a card comes to name files nothing looks for. That is worth something only
    if the two still agree, so the assertion is the agreement and not the string: the card's own
    path, written into the bundle, is an axis the loader loads.

    The card carries §6.2 step 5's consequence as well as the Map's, because the import report
    that carries the same sentence is read once, at the moment the operator has already decided
    to import, and never reopened.
    """
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
