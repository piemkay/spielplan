"""The fixture has the corpus's shape, not its data, so each test mutates the generated
`content.sqlite` with raw SQL rather than growing `make_bundle.py`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spielplan.art import hosts
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


# `make_bundle` ships bare paths, which decision 501's host rule refuses; tests that assert art
# write these URLs into their own copy.
HEAT_POSTER = "https://image.tmdb.org/t/p/w500/heat.jpg"
HEAT_BACKDROP = "https://image.tmdb.org/t/p/w1280/heat-bd.jpg"


def servable_heat(root: Path) -> None:
    edit(root, f"UPDATE title_meta SET poster_url = '{HEAT_POSTER}', backdrop_url = "
               f"'{HEAT_BACKDROP}' WHERE title_id = 1 AND source = 'tmdb'")


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
        # `_upsert_titles` stages through a TEMP TABLE ... ON COMMIT DROP, which needs a transaction.
        async with db.transaction():
            await load.load_content(db, content, report, bundle_root=root)
    finally:
        content.close()
    return report


async def test_title_meta_keeps_one_row_per_source(db, root):
    """The rule is about storage: dropping tmdb must leave omdb's poster behind."""
    report = await load_content(db, root)
    assert report.ok, report.render()

    assert report.table_counts["loaded:title_meta"] == len(fx.META)
    assert await db.fetchval("SELECT count(*) FROM title_meta") == len(fx.META)

    sources = await db.fetch(
        "SELECT source FROM title_meta WHERE title_id = 1 ORDER BY source"
    )
    assert [r["source"] for r in sources] == ["omdb", "tmdb", "wikipedia"]


async def test_the_per_source_payload_carries_the_corpus_columns(db, root):
    """21 typed columns packed into one `payload jsonb`; `_best` walks the corpus's field names."""
    await load_content(db, root)
    payload = await db.fetchval(
        "SELECT payload FROM title_meta WHERE title_id = 1 AND source = 'tmdb'"
    )
    assert isinstance(payload, dict)
    # Every shipped column but the two that became the primary key.
    assert set(payload) == set(SHIPPED_COLUMNS["title_meta"]) - {"title_id", "source"}
    assert payload["tagline"] == fx.META[0][2]
    assert payload["plot_full"] == fx.META[0][4]


async def test_a_source_can_be_dropped_without_taking_the_others_with_it(db, root):
    """Dropping a source is a DELETE of one source, not a re-import."""
    await load_content(db, root)
    await db.execute("DELETE FROM title_meta WHERE source = 'tmdb'")
    left = await db.fetch("SELECT source FROM title_meta WHERE title_id = 1 ORDER BY source")
    assert [r["source"] for r in left] == ["omdb", "wikipedia"]


async def test_the_content_spine_reads_the_resolved_card(db, root):
    """The art is written as the corpus stores it, a full TMDB URL: bare paths are refused by
    decision 501, and a None poster would hold the spine to nothing."""
    servable_heat(root)
    await load_content(db, root)
    title = await library.get_title(db, 1)
    assert title["overview"] == fx.META[0][4]
    assert title["tagline"] == fx.META[0][2]
    assert title["poster_path"] == HEAT_POSTER
    assert title["backdrop_path"] == HEAT_BACKDROP
    assert title["trailer_key"] == "heat-trailer-key"


async def test_the_card_resolves_per_field_not_per_block(db, root):
    """`mdc/export.py:34-45` resolves each field independently over SOURCE_PRIORITY."""
    add_meta(root, 4, "omdb", tagline="An omdb tagline.", plot_full="An omdb plot.")
    await load_content(db, root)

    title = await library.get_title(db, 4)
    assert title["tagline"] == "An omdb tagline."          # tmdb carries none
    assert title["overview"] == "Chungking Express — a synthetic plot."   # …but carries this


async def test_null_and_empty_string_are_absent_and_the_walk_continues(db, root):
    """Two sources deep is the case a `COALESCE(tmdb, omdb)` gets wrong."""
    add_meta(root, 7, "omdb", tagline="", plot_full="An omdb plot for the bear.")
    add_meta(root, 7, "trakt", tagline="A trakt tagline.")
    await load_content(db, root)

    title = await library.get_title(db, 7)
    assert title["tagline"] == "A trakt tagline."
    assert title["overview"] == "The Bear — a synthetic plot."


def test_zero_is_absent_too():
    """`budget` and `revenue` are integers, and 0 there means unknown."""
    rows = {"tmdb": {"budget": 0}, "omdb": {"budget": 12}}
    assert meta.best(rows, "budget", meta.SOURCE_PRIORITY) == 12


async def test_the_overview_falls_back_from_plot_full_to_plot_short(db, root):
    """wikipedia alone carries `plot_short`."""
    edit(root, "DELETE FROM title_meta WHERE title_id = 6")
    add_meta(root, 6, "wikipedia", plot_short="A one-line synthetic summary.")
    await load_content(db, root)

    title = await library.get_title(db, 6)
    assert title["overview"] == "A one-line synthetic summary."
    assert title["tagline"] is None
    assert title["poster_path"] is None


async def test_a_title_with_no_meta_row_renders_without_those_fields(db, root):
    """Title 8 ships no meta row; the card must render."""
    await load_content(db, root)
    title = await library.get_title(db, 8)
    assert title is not None
    assert title["name"] == "Tampopo"
    assert (title["overview"], title["tagline"], title["poster_path"]) == (None, None, None)
    assert title["backdrop_path"] is None and title["trailer_key"] is None


async def test_a_bundle_with_no_meta_table_still_imports(db, root):
    """A bundle without `title_meta` warns rather than failing the import."""
    edit(root, "DROP TABLE title_meta")
    report = await load_content(db, root)

    assert report.ok, report.render()
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)
    assert await db.fetchval("SELECT count(*) FROM title WHERE overview IS NOT NULL") == 0
    assert any(f.rule == "title-meta" and f.severity == "warn" for f in report.findings)


async def test_the_source_order_travels_with_the_bundle(db, root):
    """The order is read from the bundle (decision 162). The poster no longer moves: decision 501's
    host rule makes omdb's IMDb-hosted poster ineligible whatever the order."""
    servable_heat(root)
    set_bundle_key(root, "source_priority", ["omdb", "tmdb", "wikipedia", "trakt", "tvmaze"])
    await load_content(db, root)

    title = await library.get_title(db, 1)
    assert title["overview"] == "A shorter synthetic plot."
    assert title["poster_path"] == HEAT_POSTER
    assert title["tagline"] == "A Los Angeles crime saga."


async def test_a_bundle_shipping_no_order_gets_the_corpus_order_and_a_report_line(db, root):
    """A silent default is not fine: the operator must see which order resolved the catalog."""
    report = await load_content(db, root)
    notes = [f for f in report.findings if f.rule == "source-priority"]
    assert notes, report.render()
    assert notes[0].detail["priority"] == list(meta.SOURCE_PRIORITY)
    title = await library.get_title(db, 1)
    assert title["overview"] == fx.META[0][4]


# Decisions 499 and 501: an MPST retelling, a shared synopsis and an IMDb-hosted poster are
# ineligible.

MPST_PLOT = "The film opens on its own ending and then retells the rest, ending included."
TVMAZE_POSTER = "https://static.tvmaze.com/uploads/images/original_untouched/1/prisoners.jpg"


async def test_an_mpst_synopsis_is_never_the_overview(db, root):
    """Decision 499: mpst is never the overview. The rule is per field, so Tampopo's poster resolves."""
    add_meta(root, 1, "mpst", plot_full="A retelling of Heat, ending included.")
    add_meta(root, 8, "mpst", plot_full=MPST_PLOT)
    add_meta(root, 8, "tvmaze", poster_url=TVMAZE_POSTER)
    report = await load_content(db, root)

    assert (await library.get_title(db, 1))["overview"] == fx.META[0][4]
    tampopo = await library.get_title(db, 8)
    assert tampopo["overview"] is None
    assert tampopo["poster_path"] == TVMAZE_POSTER
    assert await db.fetchval(
        "SELECT payload ->> 'plot_full' FROM title_meta WHERE title_id = 8 AND source = 'mpst'"
    ) == MPST_PLOT
    note = next(f for f in report.findings if "without_overview" in f.detail)
    assert note.detail["without_overview"] == 1, report.render()


async def test_a_synopsis_another_title_shares_is_absent_from_both_cards(db, root):
    """Text matched onto a title (Wikipedia, MPST) and shared by two titles is dropped from both.
    A shared TMDB overview is kept: one novel's synopsis on each adaptation."""
    shared = "Two lovers in a Paris nightclub - a synthetic plot two titles carry."
    edit(root, "DELETE FROM title_meta WHERE title_id IN (6, 7)")
    add_meta(root, 6, "wikipedia", plot_full=shared, plot_short="Severance's own one line.")
    add_meta(root, 7, "wikipedia", plot_full=f"  {shared}  ")
    add_meta(root, 8, "wikipedia", plot_full="A synthetic plot only Tampopo carries.")
    edit(root, "UPDATE title_meta SET plot_full = 'One novel, adapted twice.'"
               " WHERE source = 'tmdb' AND title_id IN (3, 4)")
    await load_content(db, root)

    assert (await library.get_title(db, 6))["overview"] == "Severance's own one line."
    assert (await library.get_title(db, 7))["overview"] is None
    assert (await library.get_title(db, 8))["overview"] == "A synthetic plot only Tampopo carries."
    assert (await library.get_title(db, 3))["overview"] == "One novel, adapted twice."
    assert (await library.get_title(db, 4))["overview"] == "One novel, adapted twice."

    await _write_card(db, 7, ACQUIRED)
    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), title_ids=[7])
    assert (await library.get_title(db, 7))["overview"] is None


async def test_an_image_on_a_host_the_app_may_not_serve_is_skipped(db, root):
    """Decision 501 narrows what is eligible and leaves the order alone: TVmaze now beats OMDb."""
    servable_heat(root)
    add_meta(root, 2, "tvmaze", poster_url=TVMAZE_POSTER)
    report = await load_content(db, root)

    assert (await library.get_title(db, 1))["poster_path"] == HEAT_POSTER
    assert (await library.get_title(db, 2))["poster_path"] == TVMAZE_POSTER
    held = await db.fetch("SELECT payload FROM title_meta")
    refused = sum(
        1 for row in held for field in ("poster_url", "backdrop_url")
        if row["payload"].get(field) and not hosts.servable(row["payload"][field])
    )
    note = next(f for f in report.findings if "refused_images" in f.detail)
    assert note.detail["refused_images"] == refused >= 2, report.render()

    await db.execute("DELETE FROM title_meta WHERE title_id = 2 AND source = 'tvmaze'")
    await _write_card(db, 2, ACQUIRED)
    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), title_ids=[2])
    assert (await library.get_title(db, 2))["poster_path"] is None, (
        "an IMDb-hosted poster is the only art this title has, and it is not art this app serves"
    )


# §8 stage 3 derives ONE title: the scope is real and the rule is not forked.


CARD = ("overview", "tagline", "poster_path", "backdrop_path", "trailer_key")

# Values no fixture source carries, so a rewrite shows.
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
    """Title 3 has meta rows and a `title_video` row, so an unscoped UPDATE would change its trailer."""
    servable_heat(root)
    await load_content(db, root)
    await _write_card(db, 3, ACQUIRED)
    await _write_card(db, 1, BLANK)
    report = ImportReport()

    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), report, title_ids=[1])

    assert await _card(db, 1) == (
        fx.META[0][4], fx.META[0][2], HEAT_POSTER, HEAT_BACKDROP, "heat-trailer-key",
    )
    assert await _card(db, 3) == ACQUIRED
    # Per title, not a total: two titles carry a trailer key but only one was resolved.
    note = next(f for f in report.findings if f.rule == "title-card")
    assert note.detail["titles"] == 1
    assert "1 carry a trailer key" in note.message


async def test_a_scoped_resolve_of_a_title_with_no_meta_row_writes_nothing(db, root):
    """Title 8 has no meta row, so a derive naming it must leave the card alone, not NULL it."""
    await load_content(db, root)
    await _write_card(db, 8, ACQUIRED)

    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), title_ids=[8])

    assert await _card(db, 8) == ACQUIRED


async def test_the_scoped_and_unscoped_paths_resolve_one_title_identically(db, root):
    """A fork would show in which source wins and in whether empty counts as an answer."""
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
    """`[]` is "no titles"; the falsy reading would rewrite the whole library."""
    await load_content(db, root)
    await _write_card(db, 1, BLANK)

    await meta.resolve_title_fields(db, list(meta.SOURCE_PRIORITY), title_ids=[])

    assert await _card(db, 1) == BLANK


def test_the_source_order_is_readable_without_an_import_report(root):
    """`derive_title` passes `bundle_root=None` (the bundle is deleted by its import), so a derive
    resolves by the constant; the next test's warning covers a bundle shipping its own order."""
    assert meta.source_priority(None) == list(meta.SOURCE_PRIORITY)

    set_bundle_key(root, "source_priority", ["omdb", "tmdb", "wikipedia"])
    assert meta.source_priority(root) == ["omdb", "tmdb", "wikipedia"]


def test_a_bundle_whose_order_is_not_the_apps_is_warned_about_at_the_one_moment_it_can_be(root):
    """Two resolution orders on one install would be silent, so the import warns. `warn`, not
    `fail`: the corpus's cards are right."""
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


async def test_every_shipped_table_is_loaded_with_a_count_or_skipped_with_a_reason(db, root):
    """A skipped table is a decision the report must state."""
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
    """`title_meta` vanished for five milestones because an unmapped table produced no line."""
    edit(root, "CREATE TABLE title_franchise (title_id INTEGER, franchise TEXT)")
    report = await load_content(db, root)

    assert not report.ok
    assert any("title_franchise" in f.message for f in report.failures), report.render()
    assert await db.fetchval("SELECT count(*) FROM title") == 0


async def test_a_shipped_view_is_not_counted_as_a_table(db, root):
    """A view was validated and counted but never loaded; views are not tables."""
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
    """`validate` derives its duplicate check from `MAPPINGS`, so they must match the DDL. An empty
    key is legal: `credit` and `award` carry a surrogate `bigserial`."""
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


async def test_every_mapped_column_exists_on_both_sides(db, root):
    """The sweep is over whatever `MAPPINGS` holds today, so it cannot go stale."""
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
    """An unmapped `title.name` would load NULL, not 'Heat'."""
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
    """A renamed upstream column used to load a table of NULLs with only a warning."""
    edit(root, "ALTER TABLE title DROP COLUMN primary_title")
    report = await load_content(db, root)

    assert not report.ok
    failure = next(f for f in report.failures if f.detail.get("table") == "title")
    assert "primary_title" in failure.message
    assert await db.fetchval("SELECT count(*) FROM title") == 0


async def test_an_unmapped_bundle_column_is_still_only_a_report_line(db, root):
    """The corpus is the authority on its own columns; this app must survive it gaining one."""
    edit(root, "ALTER TABLE title ADD COLUMN mood_forecast TEXT")
    report = await load_content(db, root)

    assert report.ok, report.render()
    assert "mood_forecast" in report.unmapped_columns["title"]


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


async def test_the_registry_is_skipped_and_never_lands_in_the_onboarding_list(db, root):
    """The registry is 238 rows; the onboarding list is §4.3's 100 decade-stratified ids."""
    edit(root, *REGISTRY)
    report = await load_content(db, root)
    assert report.ok, report.render()

    assert {"seed_list", "title_list_membership"} <= set(report.skipped_tables)
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 0


async def test_the_onboarding_list_is_populated_only_from_seed_list_json(db, root):
    """Importing the registry first must write nothing into the onboarding list."""
    edit(root, *REGISTRY)
    report = await load_content(db, root)
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 0

    await dna_loader.load_seed_list(db, root / "artifacts" / "seed_list.json", report)

    assert await db.fetchval("SELECT count(*) FROM seed_list") == len(fx.TITLES)
    assert await db.fetchval("SELECT title_id FROM seed_list WHERE position = 0") == fx.TITLES[0][0]


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
