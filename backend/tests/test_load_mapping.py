"""Loader mapping tests (§4.1 rules 1, 3, 6). Mostly no database; the per-source keys need one."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from spielplan.db import library
from spielplan.importer import bundle as bundle_import
from spielplan.importer import load
from tests.fixtures import make_bundle as fx


@pytest.fixture
def content(tmp_path):
    root = fx.make_bundle(tmp_path / "bundle")
    db = sqlite3.connect(f"file:{root / 'content.sqlite'}?mode=ro", uri=True)
    db.text_factory = str
    yield db
    db.close()


def _map(target: str) -> load.TableMap:
    return next(m for m in load.MAPPINGS if m.target == target)


def test_null_pk_components_are_coalesced_to_empty_string(content):
    """§4.1 rule 6: 'coalesce NULLable PK components (title_alias.region etc.) to ''.'"""
    tmap = _map("title_alias")
    rows = list(load._rows(content, tmap))

    assert rows, "fixture must contain alias rows with NULL PK components"
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    for row in rows:
        for col in ("region", "language", "kind"):
            assert row[idx[col]] is not None, f"{col} must never reach a PK as NULL"


def test_non_pk_nulls_are_left_alone(content):
    """Coalescing is targeted, not blanket: `character` may legitimately be NULL."""
    tmap = _map("credit")
    rows = list(load._rows(content, tmap))
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    assert any(r[idx["character"]] is None for r in rows)


def test_platform_rating_is_the_only_display_schema_target():
    """§4.1 rule 3: the display-only schema has exactly one door into it."""
    display_targets = [m.target for m in load.MAPPINGS if m.target.startswith("display.")]
    assert display_targets == ["display.platform_rating"]


def test_dna_tiers_are_not_in_the_generic_loader():
    """§4.1 rule 1: in the generic MAPPINGS table, merging the two tiers would be a one-line change."""
    targets = {m.target for m in load.MAPPINGS}
    assert "dna_tag" not in targets
    assert "dna_projected" not in targets


def test_no_mapping_names_a_column_the_corpus_does_not_ship():
    """A mapping naming a column upstream does not ship is
    this app asserting a name; the manifest is the truth."""
    shipped = json.loads(
        (Path(__file__).parent / "fixtures" / "real_bundle_shapes.json").read_text(
            encoding="utf-8"
        )
    )["sqlite"]["content.sqlite"]
    for tmap in load.MAPPINGS:
        assert tmap.source in shipped, f"{tmap.source} is not a table the corpus ships"
        missing = sorted(set(tmap.columns.values()) - set(shipped[tmap.source]))
        assert not missing, f"{tmap.source} maps column(s) the bundle lacks: {missing}"


def test_every_mapping_column_is_distinct():
    for tmap in load.MAPPINGS:
        sources = list(tmap.columns.values())
        assert len(sources) == len(set(sources)), f"{tmap.target} maps a source column twice"


def test_load_order_puts_parents_before_children():
    """FK order: person before credit, title before everything that references it."""
    order = [m.target for m in load.MAPPINGS]
    assert order.index("title") == 0
    assert order.index("person") < order.index("credit")


def test_the_per_source_tables_carry_the_corpus_key(content):
    """Without `source` in the key, the shipped bundle's
    duplicate groups collide and the whole seed rolls back."""
    assert _map("title_country").columns["source"] == "source"
    platform = _map("display.platform_rating")
    assert platform.columns["platform"] == "source"
    assert platform.columns["metric"] == "metric"
    assert platform.columns["scale"] == "scale"


def test_the_genome_slice_is_named_as_skipped_rather_than_mapped():
    """Decision 291: the genome is a corpus-side artefact;
    skipped, not unmapped, so §10 still reports a count."""
    slice_tables = {"ml_genome_tag", "ml_link", "ml_genome_score"}
    assert slice_tables <= set(load.SKIPPED_TABLES)
    assert not slice_tables & {m.source for m in load.MAPPINGS}
    assert not slice_tables & {m.target for m in load.MAPPINGS}
    for table in sorted(slice_tables):
        reason = load.SKIPPED_TABLES[table]
        assert "media-graph-spec_v1.1.md:175" in reason, (
            f"`{table}`'s reason must cite the clause it upholds, not merely decline the table"
        )
        # Both spellings name the decision that skipped the table.
        assert "decision 291" in reason or "decisions 291 and 311" in reason, (
            f"`{table}`'s reason no longer cites the decision that skipped it: {reason}"
        )


# A corpus figure: `888,023` or ungrouped `888023`. Decision numbers and line citations stay outside it.
_COUNTED = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b")


def test_no_skip_reason_states_a_row_count_the_import_did_not_measure():
    """A skip reason is rendered beside the report's own
    counts, so a corpus figure there reads as a measurement."""
    guilty = {
        table: _COUNTED.findall(reason)
        for table, reason in load.SKIPPED_TABLES.items()
        if _COUNTED.search(reason)
    }
    assert not guilty, (
        f"a skip reason states a count no import measured: {guilty}. The operator reads this "
        "string beside `report.table_counts` for the bundle in hand, which is what section 10's "
        "'counts per table' means; argue the corpus figure in the comment above the dict instead."
    )


def test_the_skip_reason_guard_sees_the_corpus_figure_come_back():
    shipped = ("888,023 relevance rows for a block no import this build populates; "
               "media-graph-spec_v1.1.md:175, decisions 291 and 311")
    assert _COUNTED.search(shipped), "the guard no longer sees the string this repaired"
    assert _COUNTED.search(shipped.replace("888,023", "888023")), "an ungrouped count escapes"
    for innocent in load.SKIPPED_TABLES.values():
        assert not _COUNTED.search(innocent), innocent
    assert not _COUNTED.search(
        "the genome relevance scores; media-graph-spec_v1.1.md:175 makes the whole slice a "
        "corpus-side validation artifact (decisions 291 and 311)"
    ), "the repaired reason's own citations are not counts"


def test_no_loader_path_joins_titles_on_imdb_id():
    """§4.1: `imdb_id` "must never be the join key"; static, because a join can return under any name."""
    assert not hasattr(load, "_resolve_ml_links")
    source = Path(load.__file__).read_text(encoding="utf-8")
    # Qualified reads and JOINs, not the word: `"imdb_id": "imdb_id"` is a mapped column and must stay.
    offenders = [
        line.strip() for line in source.splitlines()
        if "imdb_id" in line and (".imdb_id" in line or "JOIN " in line.upper())
    ]
    assert not offenders, f"the loader resolves through imdb_id again: {offenders}"


def test_title_company_is_mapped_rather_than_skipped():
    """Mapped with the corpus's own key, which made the dedupe unnecessary (decision 193)."""
    assert "title_company" not in load.SKIPPED_TABLES
    tmap = _map("title_company")
    assert tmap.source == "title_company"
    # `source` (who said so) and `role` (what the company did) are two facts, both PK components.
    assert tmap.columns["source"] == "source"
    assert tmap.columns["role"] == "role"
    assert {"source", "role"} <= set(tmap.coalesce_empty)
    # §4.1 makes the company's `country` a report line, so the mapping must not claim it.
    assert "country" not in tmap.columns.values()


# These need a server: the defect is a *unique violation*, which only the destination key can raise.


def _add_duplicate_per_source_rows(root: Path) -> dict[str, int]:
    """The committed fixture ships one source per title, so it cannot fail the way the real bundle does."""
    db = sqlite3.connect(root / "content.sqlite")
    country = db.execute("SELECT country FROM title_country WHERE title_id = 1").fetchone()[0]
    with db:
        db.executemany(
            "INSERT INTO title_country (title_id, source, country) VALUES (?,?,?)",
            [(1, "omdb", country), (1, "wikidata", country)],
        )
        db.executemany(
            "INSERT INTO platform_rating (title_id, source, metric, value, scale, votes)"
            " VALUES (?,?,?,?,?,?)",
            [(1, "imdb", "critic_score", 74.0, 100.0, None),
             (1, "tmdb", "popularity", 58.1371, None, None),
             (1, "trakt", "dist_10", 24479.0, None, None)],
        )
    counts = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("title_country", "platform_rating")
    }
    db.close()
    # BUNDLE.json is read before the first row, so rows added after `make_bundle` need a re-inventory.
    fx.reinventory(root)
    return counts


async def test_every_per_source_row_survives_the_import(db, tmp_path):
    root = fx.make_bundle(tmp_path / "bundle")
    shipped = _add_duplicate_per_source_rows(root)
    bundle = bundle_import.Bundle.open(root)

    report = await bundle_import.import_bundle(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    assert await db.fetchval("SELECT count(*) FROM title_country") == shipped["title_country"]
    assert (
        await db.fetchval("SELECT count(*) FROM display.platform_rating")
        == shipped["platform_rating"]
    )

    # ...and the surviving rows are distinguishable by the component that was being dropped.
    countries = await db.fetch(
        "SELECT source FROM title_country WHERE title_id = 1 ORDER BY source"
    )
    assert [r["source"] for r in countries] == ["omdb", "tmdb", "wikidata"]
    ratings = await db.fetch(
        "SELECT platform, metric, scale FROM display.platform_rating "
        "WHERE title_id = 1 ORDER BY platform, metric"
    )
    assert [(r["platform"], r["metric"]) for r in ratings] == [
        ("imdb", "critic_score"), ("imdb", "user_score"), ("metacritic", "critic_score"),
        ("tmdb", "popularity"), ("trakt", "dist_10"),
    ]


async def test_the_card_shows_one_number_per_platform_and_metric(db, tmp_path):
    """The card reads only scored metrics: the unscaled ones have no caption they could honestly print."""
    root = fx.make_bundle(tmp_path / "bundle")
    _add_duplicate_per_source_rows(root)
    await bundle_import.import_bundle(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    items = await library.platform_ratings(db, 1)
    assert [(i["platform"], i["metric"], i["scale"]) for i in items] == [
        ("imdb", "critic_score", 100.0), ("imdb", "user_score", 10.0),
        ("metacritic", "critic_score", 100.0),
    ]
    assert all(i["score"] is not None for i in items)


def test_the_title_row_carries_the_language_the_tower_was_trained_on(content):
    """The tower's `lang:` column comes from `title.original_language` and nothing else."""
    assert _map("title").columns["original_language"] == "original_language"

    tmap = _map("title")
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    rows = list(load._rows(content, tmap))
    assert rows, "the fixture must ship a spine"
    languages = {row[idx["original_language"]] for row in rows}
    assert languages - {None}, "the fixture ships no original_language to carry"
