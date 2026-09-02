"""Loader mapping tests. Spec v2.1 §4.1 rules 1, 3, 6, and §6.0's platform-score block.

Most of this file needs no database — the mapping is a data structure and can be read. The
per-source keys at the end are the exception and say why: a dropped key component is a
*unique violation*, which only the destination server can raise.
"""

from __future__ import annotations

import json
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
    """§4.1 rule 1: the two tiers are loaded by `importer/dna.py` with two separate statements.
    If either ever appears in the generic MAPPINGS table, merging them becomes a one-line
    change — which is exactly what the rule forbids."""
    targets = {m.target for m in load.MAPPINGS}
    assert "dna_tag" not in targets
    assert "dna_projected" not in targets


def test_no_mapping_names_a_column_the_corpus_does_not_ship():
    """M4.5 inverts what this test used to assert.

    It read: "a column this app expects but the bundle lacks is imported as NULL and reported,
    not fatal" — and it asserted that on `title.tagline`, a column the corpus does not export
    on `title` at all. §4.1's shape note runs the other way: an *unmapped bundle* column is a
    report line because the corpus owns its own names; a mapping naming a column upstream does
    not have is this app asserting a name, and NULLs are the wrong answer to it. The manifest
    is the ground truth, and against it the mapping named `ml_link.title_id`,
    `ml_genome_score.ml_movie_id`, `rating_title_map.source_key` and `watchlist.source`.
    """
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
    assert order.index("ml_genome_tag") < order.index("ml_genome_score")
    assert order.index("rating_source") < order.index("rating_title_map")


def test_the_three_per_source_tables_carry_the_corpus_key(content):
    """The corpus keys `title_language`, `title_country` and `platform_rating` per source, and
    0015 now keys them the same way (owner decision 2026-09-02, §4.1 "tables mirror the corpus
    export"). A mapping that drops `source` collapses 17,342 / 19,092 / 32,463 duplicate groups
    in the shipped bundle onto one another, so COPY raises a unique violation and the whole seed
    rolls back — the failure this milestone exists to remove."""
    assert _map("title_language").columns["source"] == "source"
    assert _map("title_country").columns["source"] == "source"
    platform = _map("display.platform_rating")
    assert platform.columns["platform"] == "source"
    assert platform.columns["metric"] == "metric"
    assert platform.columns["scale"] == "scale"


def test_language_role_and_source_are_two_different_facts(content):
    """`role` comes from `is_primary`; `source` says who claimed it. Both survive: 0015's key is
    (title_id, source, language, role), and folding one into the other loses a fact."""
    tmap = _map("title_language")
    assert tmap.columns["role"] == "is_primary"
    assert tmap.columns["source"] == "source"
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    rows = list(load._rows(content, tmap))
    assert rows
    for row in rows:
        assert row[idx["role"]] in ("primary", "")
        assert row[idx["source"]] == "tmdb"


# --- the per-source keys, against a real Postgres ---------------------------------------
#
# Skipped without TEST_DATABASE_URL; see tests/conftest.py. These need a server because the
# defect is a *unique violation*: the mapping is well-formed Python either way, and only the
# destination key can tell the two apart.


def _add_duplicate_per_source_rows(root: Path) -> dict[str, int]:
    """Give the fixture the shape the shipped bundle actually has, and report the row counts.

    The committed fixture ships one source per title, so it cannot fail the way the artifact
    does: 17,342 / 19,092 / 32,463 duplicate groups under the app's pre-0015 keys. The rows
    below are the corpus's own second and third sources for one title, and the metrics the
    corpus records per source (`critic_score` beside `user_score`, and the unscaled
    `popularity` / `dist_10` it also keeps).
    """
    db = sqlite3.connect(root / "content.sqlite")
    language = db.execute("SELECT language FROM title_language WHERE title_id = 1").fetchone()[0]
    country = db.execute("SELECT country FROM title_country WHERE title_id = 1").fetchone()[0]
    with db:
        db.executemany(
            "INSERT INTO title_language (title_id, source, language, is_primary) VALUES (?,?,?,?)",
            [(1, "omdb", language, 1), (1, "wikidata", language, 0), (1, "trakt", language, 1)],
        )
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
        for table in ("title_language", "title_country", "platform_rating")
    }
    db.close()
    return counts


async def test_every_per_source_row_survives_the_import(db, tmp_path):
    """§4.1 "tables mirror the corpus export": every shipped row lands, none collides.

    Before 0015 the three targets were keyed without `source`, and this import died on
    `UniqueViolationError` inside the one transaction that carries the whole seed.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    shipped = _add_duplicate_per_source_rows(root)
    bundle = bundle_import.Bundle.open(root)

    report = await bundle_import.import_bundle(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    assert await db.fetchval("SELECT count(*) FROM title_language") == shipped["title_language"]
    assert await db.fetchval("SELECT count(*) FROM title_country") == shipped["title_country"]
    assert (
        await db.fetchval("SELECT count(*) FROM display.platform_rating")
        == shipped["platform_rating"]
    )

    # ...and the surviving rows are distinguishable by the component that was being dropped.
    langs = await db.fetch(
        "SELECT source, role FROM title_language WHERE title_id = 1 ORDER BY source"
    )
    assert [(r["source"], r["role"]) for r in langs] == [
        ("omdb", "primary"), ("tmdb", "primary"), ("trakt", "primary"), ("wikidata", ""),
    ]
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
    """§6.0's card, now that the row is per (platform, metric).

    Two rules decide what it shows. §4.1 rule 3 keeps the block display-only, so nothing here
    is a filter over a model input — it is a rendering choice. §6.0 wants the caption with the
    number, and the corpus's unscaled metrics (`popularity`, `dist_1`..`dist_10`,
    `critic_review_count`, `audience_rating_count` — 12 of one title's 22 shipped rows) have no
    caption they could honestly print: they are not scores on a stated range. So the card reads
    the scored metrics, each carrying its own `metric` and `scale`.
    """
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
    """The seam between the loader and §4.2's meta block. The corpus builds the tower's one
    `lang:` column from `title.original_language` and from nothing else — `title_language` is a
    different, multi-source fact averaging 2.98 entries a title. The loader mapped every column
    of `title` except this one, so on a real seed it stays NULL, and the meta block's `lang:`
    production never fires against a bundle: the tower is fed a vector missing a column it was
    trained with, silently, on every title.
    """
    assert _map("title").columns["original_language"] == "original_language"

    tmap = _map("title")
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    rows = list(load._rows(content, tmap))
    assert rows, "the fixture must ship a spine"
    languages = {row[idx["original_language"]] for row in rows}
    assert languages - {None}, "the fixture ships no original_language to carry"
