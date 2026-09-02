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


# --- data-rules-importer-maps-the-shipped-content-schema ----------------------------------


async def test_every_mapped_column_exists_on_both_sides(db, root):
    """The mapping is a claim about two schemas at once. Against the shipped manifest it named
    `ml_link.title_id`, `ml_genome_score.ml_movie_id`, `rating_title_map.source_key` and
    `watchlist.source` — four columns the corpus does not export."""
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
