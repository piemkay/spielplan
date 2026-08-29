"""Loader mapping tests that need no database. Spec v2.1 §4.1 rules 1, 3, 6."""

from __future__ import annotations

import sqlite3

import pytest

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
    cols = [r[1] for r in content.execute('PRAGMA table_info("title_alias")')]
    rows = list(load._rows(content, tmap, cols))

    assert rows, "fixture must contain alias rows with NULL PK components"
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    for row in rows:
        for col in ("region", "language", "kind"):
            assert row[idx[col]] is not None, f"{col} must never reach a PK as NULL"


def test_non_pk_nulls_are_left_alone(content):
    """Coalescing is targeted, not blanket: `character` may legitimately be NULL."""
    tmap = _map("credit")
    cols = [r[1] for r in content.execute('PRAGMA table_info("credit")')]
    rows = list(load._rows(content, tmap, cols))
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


def test_missing_source_column_selects_null_rather_than_failing(content):
    """§4.1 shape note: the corpus export is the authority on its own column names. A column
    this app expects but the bundle lacks is imported as NULL and reported, not fatal."""
    tmap = _map("title")
    cols = [r[1] for r in content.execute('PRAGMA table_info("title")')]
    cols_without_tagline = [c for c in cols if c != "tagline"]
    rows = list(load._rows(content, tmap, cols_without_tagline))
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    assert all(r[idx["tagline"]] is None for r in rows)


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
