"""Validator tests — spec v2.1 §4.1 landmine rules, §10 report.

Each broken-bundle test asserts the validator catches exactly the rule it was built to catch;
the clean-bundle test asserts it does NOT flag the things that are legitimately present
(duplicate tmdb_ids, (title,term) pairs in both DNA tiers, non-ASCII text).
"""

from __future__ import annotations

import sqlite3

import pytest

from spielplan.importer import bundle as bundle_import
from tests.fixtures import make_bundle as fx


@pytest.fixture
def clean(tmp_path):
    return fx.make_bundle(tmp_path / "bundle")


def _rules(report, severity):
    return {f.rule for f in report.findings if f.severity == severity}


def test_clean_bundle_validates(clean):
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert report.ok, report.render()
    assert report.bundle_version == "test-v1"
    assert report.vocabulary_version == "v1"


def test_legitimate_duplicates_are_notes_not_failures(clean):
    # §4.1 rule 6: duplicate tmdb_id values are legitimate (movie/series pairs); the bundle
    # fixture has one. A validator that failed on this would reject every real bundle.
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    dupes = [f for f in report.findings if f.rule == "rule6-no-unique"]
    assert dupes and all(f.severity == "note" for f in dupes)
    assert any(f.detail.get("observed", 0) >= 1 for f in dupes)


def test_shared_dna_pairs_are_counted_not_deduped(clean):
    # §4.1 rule 1: pairs in both tiers "must stay distinguishable" — counted, never merged.
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    shared = next(f for f in report.findings if f.rule == "rule1-two-tiers")
    assert shared.severity == "note"
    assert shared.detail["shared"] == 3


def test_frozen_rating_source_ids(clean):
    fx.break_rating_source_ids(clean)
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule4-frozen-ids" in _rules(report, "fail")


def test_null_kind_fails(clean):
    fx.break_kind(clean)
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule5-kind" in _rules(report, "fail")


def test_tag_without_evidence_fails(clean):
    fx.break_evidence(clean)
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule1-evidence" in _rules(report, "fail")


def test_denylisted_table_fails(clean):
    fx.break_denylist(clean)
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule7-denylist" in _rules(report, "fail")


def test_merged_tiers_fail(clean):
    fx.break_merged_tiers(clean)
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule1-two-tiers" in _rules(report, "fail")


def test_out_of_range_salience_fails(clean):
    fx.break_salience(clean)
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule2-weights" in _rules(report, "fail")


def test_missing_rating_source_table_fails(clean):
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("DROP TABLE rating_source")
    db.commit()
    db.close()
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "rule4-frozen-ids" in _rules(report, "fail")


def test_non_ascii_survives_the_read_path(clean):
    """§4.1 rule 8: never "clean" non-ASCII — the corpus legitimately contains CJK, RTL
    scripts, ZWSP and emoji. A row count proves nothing here; the characters do."""
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert report.ok
    assert report.table_counts["title"] == 8

    db = sqlite3.connect(f"file:{clean / 'content.sqlite'}?mode=ro", uri=True)
    db.text_factory = str
    try:
        names = {r[0] for r in db.execute("SELECT name FROM title")}
        overviews = " ".join(r[0] for r in db.execute("SELECT overview FROM title"))
    finally:
        db.close()

    assert "重慶森林" in names                      # CJK
    assert "🎬" in overviews                # emoji
    assert "​" in overviews                    # zero-width space


def test_report_renders_human_readable(clean):
    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    text = report.render()
    assert "bundle test-v1" in text
    assert "vocabulary v1" in text
    assert "rows:" in text
