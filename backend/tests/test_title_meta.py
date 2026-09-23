"""title_meta, the resolved title card, and the import report's table accounting.

Spec v2.1 §4.1 (the content spine and its landmine rules), §6.0 (the title detail card),
§10 (the migration report); decision 162.

These are integration tests because every claim here is a claim about what Postgres holds after
a real COPY: `title_meta.payload` is `jsonb`, the per-source rows are kept by a composite
primary key, and the resolution is an UPDATE that has to leave a meta-less title alone rather
than blanking it.

The fixture is the corpus's shape but not the corpus's data, so the mutations each test needs
are applied here as raw SQL against the generated `content.sqlite`. `make_bundle.py` is shared
with four other test files and a `break_*` helper per assertion below would be eighteen more
entry points for shapes that only one test cares about.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spielplan.db import library
from spielplan.importer import dna as dna_loader
from spielplan.importer import load, meta
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

MANIFEST = json.loads(
    (Path(__file__).parent / "fixtures" / "real_bundle_shapes.json").read_text(encoding="utf-8")
)
SHIPPED_COLUMNS: dict[str, list[str]] = MANIFEST["sqlite"]["content.sqlite"]


# --- fixture plumbing -------------------------------------------------------------------


@pytest.fixture
def root(tmp_path) -> Path:
    return fx.make_bundle(tmp_path / "bundle")


def edit(root: Path, *statements: str) -> None:
    """Apply raw SQL to the bundle's content.sqlite before it is imported."""
    db = sqlite3.connect(root / "content.sqlite")
    for statement in statements:
        db.execute(statement)
    db.commit()
    db.close()


def add_meta(root: Path, title_id: int, source: str, **fields: object) -> None:
    columns = ", ".join(["title_id", "source", *fields])
    marks = ", ".join(["?"] * (2 + len(fields)))
    db = sqlite3.connect(root / "content.sqlite")
    db.execute(
        f"INSERT INTO title_meta ({columns}) VALUES ({marks})",
        (title_id, source, *fields.values()),
    )
    db.commit()
    db.close()


def set_bundle_key(root: Path, key: str, value: object) -> None:
    path = root / "BUNDLE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[key] = value
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


async def load_content(db, root: Path) -> ImportReport:
    report = ImportReport(bundle_version="test-v1")
    content = sqlite3.connect(f"file:{root / 'content.sqlite'}?mode=ro", uri=True)
    content.text_factory = str          # rule 8: UTF-8 in, UTF-8 out
    try:
        # `_upsert_titles` stages through a TEMP TABLE ... ON COMMIT DROP, which needs the
        # transaction `import_bundle` opens around the whole load.
        async with db.transaction():
            await load.load_content(db, content, report, bundle_root=root)
    finally:
        content.close()
    return report


# --- data-rules-title-meta-multi-source-rows-kept ----------------------------------------


async def test_title_meta_keeps_one_row_per_source(db, root):
    """§4.1: `title_meta (multi-source, per-source rows kept — "one block = one droppable
    source")`. The rule is about storage: dropping tmdb must leave omdb's poster behind."""
    report = await load_content(db, root)
    assert report.ok, report.render()

    assert report.table_counts["loaded:title_meta"] == len(fx.META)
    assert await db.fetchval("SELECT count(*) FROM title_meta") == len(fx.META)

    sources = await db.fetch(
        "SELECT source FROM title_meta WHERE title_id = 1 ORDER BY source"
    )
    assert [r["source"] for r in sources] == ["omdb", "tmdb", "wikipedia"]


async def test_the_per_source_payload_carries_the_corpus_columns(db, root):
    """The corpus ships 21 typed columns and Postgres holds one `payload jsonb`. Packing is
    only faithful if the corpus's own field names survive it — `_best` walks them by name."""
    await load_content(db, root)
    payload = await db.fetchval(
        "SELECT payload FROM title_meta WHERE title_id = 1 AND source = 'tmdb'"
    )
    assert isinstance(payload, dict)
    # Every shipped column but the two that became this row's primary key.
    assert set(payload) == set(SHIPPED_COLUMNS["title_meta"]) - {"title_id", "source"}
    assert payload["tagline"] == fx.META[0][2]
    assert payload["plot_full"] == fx.META[0][4]


async def test_a_source_can_be_dropped_without_taking_the_others_with_it(db, root):
    """The point of keeping the rows: one block is droppable, and the drop is a DELETE of one
    source rather than a re-import."""
    await load_content(db, root)
    await db.execute("DELETE FROM title_meta WHERE source = 'tmdb'")
    left = await db.fetch("SELECT source FROM title_meta WHERE title_id = 1 ORDER BY source")
    assert [r["source"] for r in left] == ["omdb", "wikipedia"]


async def test_the_content_spine_reads_the_resolved_card(db, root):
    """§6.0's title detail card comes through `db.library`, which is the content spine."""
    await load_content(db, root)
    title = await library.get_title(db, 1)
    assert title["overview"] == fx.META[0][4]
    assert title["tagline"] == fx.META[0][2]
    assert title["poster_path"] == "/heat.jpg"
    assert title["backdrop_path"] == "/heat-bd.jpg"
    assert title["trailer_key"] == "heat-trailer-key"


# --- library-rate-title-card-text-and-art-resolve-from-title-meta -------------------------


async def test_the_card_resolves_per_field_not_per_block(db, root):
    """`mdc/export.py:34-45` resolves each field independently over SOURCE_PRIORITY. A
    whole-block rule would take omdb's plot along with omdb's tagline; per field keeps tmdb's
    plot, which is the one the corpus itself would export."""
    add_meta(root, 4, "omdb", tagline="An omdb tagline.", plot_full="An omdb plot.")
    await load_content(db, root)

    title = await library.get_title(db, 4)
    assert title["tagline"] == "An omdb tagline."          # tmdb carries none
    assert title["overview"] == "Chungking Express — a synthetic plot."   # …but carries this


async def test_null_and_empty_string_are_absent_and_the_walk_continues(db, root):
    """`_best` skips None, '' and 0 rather than treating a present-but-empty column as an
    answer. Two sources deep is the case a `COALESCE(tmdb, omdb)` gets wrong."""
    add_meta(root, 7, "omdb", tagline="", plot_full="An omdb plot for the bear.")
    add_meta(root, 7, "trakt", tagline="A trakt tagline.")
    await load_content(db, root)

    title = await library.get_title(db, 7)
    assert title["tagline"] == "A trakt tagline."
    assert title["overview"] == "The Bear — a synthetic plot."


def test_zero_is_absent_too():
    """The third of `_best`'s three absent values, and the only one no card field can carry:
    `budget` and `revenue` are integers, and 0 there means unknown, not free."""
    rows = {"tmdb": {"budget": 0}, "omdb": {"budget": 12}}
    assert meta.best(rows, "budget", meta.SOURCE_PRIORITY) == 12


async def test_the_overview_falls_back_from_plot_full_to_plot_short(db, root):
    """wikipedia is the only source carrying `plot_short`; a title whose only meta row is
    wikipedia's still gets an overview."""
    edit(root, "DELETE FROM title_meta WHERE title_id = 6")
    add_meta(root, 6, "wikipedia", plot_short="A one-line synthetic summary.")
    await load_content(db, root)

    title = await library.get_title(db, 6)
    assert title["overview"] == "A one-line synthetic summary."
    assert title["tagline"] is None
    assert title["poster_path"] is None


async def test_a_title_with_no_meta_row_renders_without_those_fields(db, root):
    """Title 8 ships no meta row at all. §6.0's card must render, not raise."""
    await load_content(db, root)
    title = await library.get_title(db, 8)
    assert title is not None
    assert title["name"] == "Tampopo"
    assert (title["overview"], title["tagline"], title["poster_path"]) == (None, None, None)
    assert title["backdrop_path"] is None and title["trailer_key"] is None


async def test_a_bundle_with_no_meta_table_still_imports(db, root):
    """The whole-catalog form of the same rule: a bundle exported without `title_meta` leaves
    every card without those fields and warns, rather than taking the import down."""
    edit(root, "DROP TABLE title_meta")
    report = await load_content(db, root)

    assert report.ok, report.render()
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)
    assert await db.fetchval("SELECT count(*) FROM title WHERE overview IS NOT NULL") == 0
    assert any(f.rule == "title-meta" and f.severity == "warn" for f in report.findings)


async def test_the_source_order_travels_with_the_bundle(db, root):
    """Decision 162 makes this app the consumer of an order the corpus owns, so the order is
    read from the bundle. Reversing the first two sources moves the plot and the poster and
    leaves the tagline where it was — only tmdb has one."""
    set_bundle_key(root, "source_priority", ["omdb", "tmdb", "wikipedia", "trakt", "tvmaze"])
    await load_content(db, root)

    title = await library.get_title(db, 1)
    assert title["overview"] == "A shorter synthetic plot."
    assert title["poster_path"] == "/heat-omdb.jpg"
    assert title["tagline"] == "A Los Angeles crime saga."


async def test_a_bundle_shipping_no_order_gets_the_corpus_order_and_a_report_line(db, root):
    """A default is fine; a silent default is not — the operator has to be able to see which
    order resolved their catalog."""
    report = await load_content(db, root)
    notes = [f for f in report.findings if f.rule == "source-priority"]
    assert notes, report.render()
    assert notes[0].detail["priority"] == list(meta.SOURCE_PRIORITY)
    title = await library.get_title(db, 1)
    assert title["overview"] == fx.META[0][4]


# --- jellyfin-acquisition-eval-a-re-derive-is-idempotent (the resolution half) -------------
#
# §8 stage 3 derives ONE title, and `resolve_title_fields` grouped every `title_meta` row in the
# database. The four tests below are the two halves of the row that lands here: the scope is real
# (a derive writes the title it names and nothing else) and the rule is not forked (the scoped
# path and the wholesale path are the same `best()` walking the same order).


CARD = ("overview", "tagline", "poster_path", "backdrop_path", "trailer_key")

# What §8's acquisition path wrote for a title, in the only shape a test can tell apart from what
# the corpus resolved: values no source in the fixture carries.
ACQUIRED = (
    "Written by section 8's acquisition path.",
    "Acquired, not imported.",
    "/acquired.jpg",
    "/acquired-bd.jpg",
    "acquired-trailer-key",
)
BLANK = (None,) * len(CARD)


async def _card(db, title_id: int) -> tuple:
    row = await db.fetchrow(
        f"SELECT {', '.join(CARD)} FROM title WHERE id = $1", title_id
    )
    return tuple(row[column] for column in CARD)


async def _write_card(db, title_id: int, values: tuple) -> None:
    assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(CARD, start=2))
    await db.execute(f"UPDATE title SET {assignments} WHERE id = $1", title_id, *values)


async def test_a_scoped_resolve_touches_only_the_titles_it_names(db, root):
    """A derive that named title 1 and rewrote title 3 is a full-library rewrite wearing the
    name of an acquisition, and it is silent: almost every row it touched would get back the
    value it already had, so only a title whose card came from somewhere else can show it.

    Title 3 is that title here. It carries meta rows and a `title_video` row, so an unscoped
    pass has something to write over both of its fields - which is what makes the trailer key
    the assertion that fails when the grouping query is scoped and the UPDATE below it is not.
    """
    await load_content(db, root)
    await _write_card(db, 3, ACQUIRED)
    await _write_card(db, 1, BLANK)
    report = ImportReport()

    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), report, title_ids=[1])

    assert await _card(db, 1) == (
        fx.META[0][4], fx.META[0][2], "/heat.jpg", "/heat-bd.jpg", "heat-trailer-key",
    )
    assert await _card(db, 3) == ACQUIRED
    # §10's accounting is per table and not a total; the same applies to a call that resolved one
    # title. Two titles carry a trailer key at this point and the line must not claim them both.
    note = next(f for f in report.findings if f.rule == "title-card")
    assert note.detail["titles"] == 1
    assert "1 carry a trailer key" in note.message


async def test_a_scoped_resolve_of_a_title_with_no_meta_row_writes_nothing(db, root):
    """The docstring's standing promise, now per title: title 8 ships no meta row, so a derive
    that names it has nothing to resolve and must leave the card alone rather than blanking it
    back to NULL. An acquisition that wrote a card and then derived the title it wrote is the
    ordinary §8 sequence, not an edge case."""
    await load_content(db, root)
    await _write_card(db, 8, ACQUIRED)

    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), title_ids=[8])

    assert await _card(db, 8) == ACQUIRED


async def test_the_scoped_and_unscoped_paths_resolve_one_title_identically(db, root):
    """The row's second half: the per-title resolution uses "the same source priority and the
    same absent-value rule as the bundle importer rather than a second implementation".

    Asserted by running both paths over the same rows under two different orders, because a fork
    shows up in exactly two places - which source wins a field, and whether a present-but-empty
    column counts as an answer. Title 1 carries both: omdb has a plot and no tagline, so
    reversing the first two sources moves the plot and must leave the tagline on tmdb.
    """
    await load_content(db, root)
    reversed_order = ["omdb", "tmdb", "wikipedia", "trakt", "tvmaze"]
    answers = []

    for priority in (list(meta.SOURCE_PRIORITY), reversed_order):
        await meta.resolve_title_fields(db, priority)
        wholesale = await _card(db, 1)
        await _write_card(db, 1, BLANK)

        await meta.resolve_title_fields(db, priority, title_ids=[1])

        assert await _card(db, 1) == wholesale
        answers.append(wholesale)

    assert answers[0] != answers[1], (
        "the two orders resolve title 1 identically, so the assertion above says nothing about "
        "priority and a scoped path that ignored it would pass"
    )
    assert answers[0][1] == answers[1][1] == fx.META[0][2], (
        "omdb's absent tagline moved the field, so the scoped path is walking `best()` no further "
        "than its first source"
    )


async def test_an_empty_title_id_list_resolves_nothing_rather_than_everything(db, root):
    """`[]` is "no titles", not "every title", and the difference lands at the one call site that
    can produce it - a derive whose scope came out empty - where the falsy reading runs a
    full-library rewrite in the name of resolving nothing."""
    await load_content(db, root)
    await _write_card(db, 1, BLANK)

    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), title_ids=[])

    assert await _card(db, 1) == BLANK


def test_the_source_order_is_readable_without_an_import_report(root):
    """A derive has no `ImportReport` and must not build one to ask which order resolved this
    install's cards.

    THIS DOCSTRING USED TO CLAIM MORE THAN THE TWO LINES BELOW PROVE. It said "the answer is still
    read from the bundle the import read it from, so a derive cannot resolve a card by an order the
    import never used" - and the two assertions are the proof of the opposite: the argument a
    derive passes returns the constant, and the argument it never passes returns the bundle's
    order. `derive/rebuild.derive_title` calls this with `bundle_root=None` because there is no
    manifest left to read (`api/artifacts.py:152`: the bundle "is deleted by its own import") and
    no column persists the order. What a bundle shipping its own order gets instead is the warning
    the test below asserts. [M5.3 review cycle 1,
    m53-rev1-derive-resolves-by-the-constant-not-the-bundle-order]
    """
    assert meta.source_priority(None) == list(meta.SOURCE_PRIORITY)

    set_bundle_key(root, "source_priority", ["omdb", "tmdb", "wikipedia"])
    assert meta.source_priority(root) == ["omdb", "tmdb", "wikipedia"]


def test_a_bundle_whose_order_is_not_the_apps_is_warned_about_at_the_one_moment_it_can_be(root):
    """The fork the sentence above used to deny, said out loud where somebody is reading findings.

    An install taking this branch ends with two resolution orders - the corpus titles resolved by
    the bundle's, every acquired title resolved by `SOURCE_PRIORITY` - and nothing anywhere records
    that they differ. It is silent in both directions, because almost every field agrees between
    two orders and the ones that do not look like a different source simply winning; and decision
    335 carries it further than a card, since the reviews gate names `title.overview` and its plot
    arm inherits whichever half a title is in. A `warn` and not a `fail`: the import is correct and
    the corpus's cards are right. [M5.3 review cycle 1,
    m53-rev1-derive-resolves-by-the-constant-not-the-bundle-order]
    """
    report = ImportReport()
    assert meta.source_priority(root, report) == list(meta.SOURCE_PRIORITY)
    assert not [f for f in report.findings if f.severity == "warn"], (
        "a bundle shipping no order of its own resolves by the app's and has nothing to warn about"
    )

    set_bundle_key(root, "source_priority", ["omdb", "tmdb", "wikipedia"])
    report = ImportReport()
    meta.source_priority(root, report)

    warned = [f for f in report.findings if f.rule == "source-priority" and f.severity == "warn"]
    assert len(warned) == 1, [f.as_dict() for f in report.findings]
    assert report.ok, "a divergent order is a fork to record and never a reason to refuse a bundle"
    assert "resolves an acquired title's card by the app's" in warned[0].message


# --- data-rules-import-reports-every-shipped-table ----------------------------------------


async def test_every_shipped_table_is_loaded_with_a_count_or_skipped_with_a_reason(db, root):
    """§10: "counts per table". A table the bundle ships and this app does not want is a
    decision, and a decision the report cannot state is indistinguishable from an oversight."""
    edit(
        root,
        "CREATE TABLE imdb_ratings (tconst TEXT, avg_rating REAL, num_votes INTEGER)",
        "CREATE TABLE dna_annotation (title_id INTEGER, vocab_version TEXT)",
    )
    report = await load_content(db, root)
    assert report.ok, report.render()

    shipped = _shipped(root)
    for table in shipped:
        loaded = _target_of(table)
        assert (
            (loaded and f"loaded:{loaded}" in report.table_counts)
            or table in report.skipped_tables
            or table in load.BESPOKE_TABLES
        ), f"{table} is accounted for nowhere in the report"

    assert report.skipped_tables["imdb_ratings"]
    assert report.skipped_tables["dna_annotation"]
    assert "imdb_ratings" in report.render()


async def test_a_shipped_table_the_mapping_does_not_know_fails_the_import(db, root):
    """The failure mode this row exists for: `title_meta` (46,318 rows) vanished for five
    milestones because an unmapped *table* produced no line anywhere."""
    edit(root, "CREATE TABLE title_franchise (title_id INTEGER, franchise TEXT)")
    report = await load_content(db, root)

    assert not report.ok
    assert any("title_franchise" in f.message for f in report.failures), report.render()
    assert await db.fetchval("SELECT count(*) FROM title") == 0


async def test_a_shipped_view_is_not_counted_as_a_table(db, root):
    """§10's "counts per table", and the one shape that was counted and then accounted nowhere.

    The validator enumerated `type IN ('table','view')` while `load.unaccounted_tables` and
    `_account_for_shipped_tables` both enumerate `type = 'table'`, so a view arrived in
    `report.table_counts` with a row count and no target ever held those rows - a hole in the
    exit criterion's "0 unaccounted", because the report said the rows came in. v20260828 ships
    no view, so the case is the next export's and the assertion is built here.
    [M4.14 step B9, finding 2.23]
    """
    edit(root, "CREATE VIEW title_sentiment AS SELECT id AS title_id, 1 AS score FROM title")

    report = await load_content(db, root)

    assert report.ok, report.render()
    assert "title_sentiment" not in report.table_counts
    assert "loaded:title_sentiment" not in report.table_counts
    assert "title_sentiment" not in report.skipped_tables

    validated = ImportReport()
    source = sqlite3.connect(f"file:{root / 'content.sqlite'}?mode=ro", uri=True)
    try:
        validator.validate_content(source, validated)
    finally:
        source.close()
    assert "title_sentiment" not in validated.table_counts, (
        "counted here and claimed by nobody on the load side is the defect, not the fix"
    )
    noted = [f for f in validated.findings if f.rule == "table-view"]
    assert noted and "title_sentiment" in noted[0].message, validated.render()
    assert noted[0].severity == "note"


async def test_every_mapping_declares_the_targets_primary_key(db, root):
    """`TableMap.key` is the one place the app's key is written down, and this is what keeps it
    honest against the migration that declares it.

    `validate._validate_integrity` counts duplicate GROUPS under this key precisely so a COPY
    cannot meet one, and it derives the key list from `MAPPINGS` rather than from a list of its
    own -- which only helps if `MAPPINGS` and the DDL agree. They did not, three times: 0015
    re-keyed `title_language`, `title_country` and `display.platform_rating` after 17,342
    duplicate groups rolled a seed back, and 0018 re-keyed `title_company` and `title_video`.

    An EMPTY key is the other legal answer and it is checked too: section 4.1 says "credit
    (dedupe at read time, never at import)", so `credit` and `award` carry a surrogate
    `bigserial` the mapping does not write, and this asserts that shape rather than accepting
    silence. [M4.14 step B3]
    """
    for tmap in load.MAPPINGS:
        schema, table = ("public", tmap.target) if "." not in tmap.target else tmap.target.split(".")
        primary = {
            r["attname"]
            for r in await db.fetch(
                """
                SELECT a.attname
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                 WHERE i.indrelid = $1::regclass AND i.indisprimary
                """,
                f'{schema}."{table}"',
            )
        }
        assert primary, f"{tmap.target} has no primary key at all"
        if tmap.key:
            assert set(tmap.key) == primary, (
                f"{tmap.target} is keyed {sorted(primary)} and the mapping declares "
                f"{sorted(tmap.key)}; the duplicate-group check counts the wrong groups"
            )
        else:
            assert not (primary & set(tmap.pg_columns)), (
                f"{tmap.target} has no declared key but its primary key {sorted(primary)} is "
                "written by this mapping -- an empty key is for a surrogate the import never sets"
            )


# --- data-rules-importer-maps-the-shipped-content-schema ----------------------------------


async def test_every_mapped_column_exists_on_both_sides(db, root):
    """The mapping is a claim about two schemas at once. Against the shipped manifest it named
    `ml_link.title_id`, `ml_genome_score.ml_movie_id`, `rating_title_map.source_key` and
    `watchlist.source` — four columns the corpus does not export. Two of the four are history
    rather than coverage since decision 291 declined the genome slice; the sweep is over whatever
    `MAPPINGS` holds today, which is the only form of it that cannot go stale."""
    for tmap in load.MAPPINGS:
        shipped = SHIPPED_COLUMNS.get(tmap.source)
        assert shipped, f"{tmap.source} is not a table the corpus ships"
        missing = sorted(set(tmap.columns.values()) - set(shipped))
        assert not missing, f"{tmap.source} maps column(s) the bundle lacks: {missing}"

        schema, table = ("public", tmap.target) if "." not in tmap.target else tmap.target.split(".")
        rows = await db.fetch(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_schema = $1 AND table_name = $2",
            schema, table,
        )
        unknown = sorted(set(tmap.pg_columns) - {r["column_name"] for r in rows})
        assert not unknown, f"{tmap.target} has no column(s) {unknown}"


async def test_the_mapping_reads_the_names_the_corpus_ships(db, root):
    """The named cases from the row, asserted on the data rather than on the mapping: a title
    whose `name` came from an unmapped `title.name` would be NULL, not 'Heat'."""
    await load_content(db, root)
    row = await db.fetchrow("SELECT name, original_name FROM title WHERE id = 4")
    assert row["name"] == "Chungking Express"
    assert row["original_name"] == "重慶森林"
    assert await db.fetchval("SELECT billing_order FROM credit WHERE title_id = 1 LIMIT 1") == 0
    assert await db.fetchval(
        "SELECT count(*) FROM title_alias WHERE title_id = 4 AND kind = 'tmdb'"
    ) == 1
    assert await db.fetchval("SELECT scale FROM rating_source WHERE id = 1") == "10"


async def test_a_mapped_column_the_bundle_lacks_fails_naming_table_and_column(db, root):
    """§4.1's shape note keeps an *unmapped* bundle column a report line. The inverse — a
    column this app's mapping names and the bundle does not have — used to select NULL, so a
    renamed upstream column loaded a whole table of nothing and reported a warning."""
    edit(root, "ALTER TABLE title DROP COLUMN primary_title")
    report = await load_content(db, root)

    assert not report.ok
    failure = next(f for f in report.failures if f.detail.get("table") == "title")
    assert "primary_title" in failure.message
    assert await db.fetchval("SELECT count(*) FROM title") == 0


async def test_an_unmapped_bundle_column_is_still_only_a_report_line(db, root):
    """The rule only runs one way. The corpus is the authority on its own column names and
    this app must survive it gaining one."""
    edit(root, "ALTER TABLE title ADD COLUMN mood_forecast TEXT")
    report = await load_content(db, root)

    assert report.ok, report.render()
    assert "mood_forecast" in report.unmapped_columns["title"]


# --- data-rules-seed-registry-and-onboarding-list-are-distinct ----------------------------


REGISTRY = (
    """CREATE TABLE seed_list (id INTEGER PRIMARY KEY, slug TEXT NOT NULL, name TEXT,
           source TEXT, kind TEXT, category TEXT, weight REAL, item_count INTEGER,
           fetched_at REAL, notes TEXT)""",
    """CREATE TABLE title_list_membership (title_id INTEGER NOT NULL, list_id INTEGER NOT NULL,
           rank INTEGER, PRIMARY KEY (list_id, title_id))""",
    "INSERT INTO seed_list (id, slug, name, source, kind, category, weight, item_count)"
    " VALUES (11, 'imdb-top-250', 'IMDb Top 250', 'imdb', 'chart', 'canon', 1.0, 250)",
    "INSERT INTO seed_list (id, slug, name, source, kind, category, weight, item_count)"
    " VALUES (12, 'sight-and-sound-2022', NULL, NULL, 'poll', 'canon', 0.8, 100)",
    "INSERT INTO title_list_membership (title_id, list_id, rank) VALUES (1, 11, 3)",
    "INSERT INTO title_list_membership (title_id, list_id, rank) VALUES (4, 12, 7)",
)


async def test_the_registry_lands_in_title_list_and_not_in_the_onboarding_list(db, root):
    """Two different artifacts wearing one name. The registry is 238 rows of (id, slug, name,
    …); the onboarding list is §4.3's 100 decade-stratified title ids."""
    edit(root, *REGISTRY)
    report = await load_content(db, root)
    assert report.ok, report.render()

    assert report.table_counts["loaded:title_list"] == 2
    assert report.table_counts["loaded:title_list_membership"] == 2
    rows = await db.fetch("SELECT id, slug, name, source FROM title_list ORDER BY id")
    assert [(r["id"], r["slug"]) for r in rows] == [(11, "imdb-top-250"), (12, "sight-and-sound-2022")]
    # rule 6 in its new home: the registry's NULLable text lands as '' in a NOT NULL column.
    assert (rows[1]["name"], rows[1]["source"]) == ("", "")

    assert await db.fetchval("SELECT count(*) FROM seed_list") == 0


async def test_the_onboarding_list_is_populated_only_from_seed_list_json(db, root):
    """§4.3: `seed_list.json` is the onboarding list. Importing the registry first must not
    have written a single row into it, and loading the JSON must not disturb the registry."""
    edit(root, *REGISTRY)
    report = await load_content(db, root)
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 0

    await dna_loader.load_seed_list(db, root / "artifacts" / "seed_list.json", report)

    assert await db.fetchval("SELECT count(*) FROM seed_list") == len(fx.TITLES)
    assert await db.fetchval("SELECT title_id FROM seed_list WHERE position = 0") == fx.TITLES[0][0]
    assert await db.fetchval("SELECT count(*) FROM title_list") == 2


# --- helpers ------------------------------------------------------------------------------


def _shipped(root: Path) -> list[str]:
    db = sqlite3.connect(f"file:{root / 'content.sqlite'}?mode=ro", uri=True)
    try:
        return [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not r[0].startswith("sqlite_")
        ]
    finally:
        db.close()


def _target_of(source: str) -> str | None:
    return next((m.target for m in load.MAPPINGS if m.source == source), None)
