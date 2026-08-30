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


# --- §4.3's model artifacts, checked against each other -----------------------------------------
#
# §10 puts validation before the flip so a bad bundle never becomes the active one. Every failure
# below otherwise surfaces later and somewhere else, and one of them never surfaces at all.


def _artifacts(root):
    return root / "artifacts"


def test_a_tower_whose_width_disagrees_with_its_contract_is_refused_at_import(clean):
    """The one silent failure in the set, and the reason the check exists.

    §8 stage 9 builds the vector from the contract; the tower consumes it. If the two disagree
    the placement does not raise — it runs a short vector into a wide first layer, or a wide one
    into a narrow one, and writes coordinates that are the right shape and the wrong numbers.
    Every downstream surface then works perfectly on a library placed at plausible nonsense.
    """
    import json

    path = _artifacts(clean) / "feature_contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["blocks"]["genre"] += 3          # the tower still expects the old width
    path.write_text(json.dumps(contract), encoding="utf-8")

    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "cold-tower" in _rules(report, "fail")
    assert any("wrong columns" in f.message for f in report.failures)


def test_a_backbone_with_no_title_id_array_is_refused(clean):
    """§4.3 lists "E, E_full, b_i, μ … item_n" and names no id mapping — but E's rows have no
    stated correspondence to `title.id`, so without one the basis is unusable and the only safe
    reading is a refusal. Matching by row order instead would give every title a plausible
    coordinate belonging to some other film."""
    import numpy as np

    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        kept = {k: npz[k] for k in npz.files if k != "title_id"}
    np.savez(path, **kept)

    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert any("title_id" in f.message for f in report.failures)


def test_a_backbone_in_the_wrong_number_of_dimensions_is_refused(clean):
    """§1 fixes "one frozen 64-d collaborative item space". A 32-d basis is not a smaller
    version of it — every consumer indexes into 64 columns."""
    import numpy as np

    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        kept = {k: npz[k] for k in npz.files}
    kept["E"] = kept["E"][:, :32]
    np.savez(path, **kept)

    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert any("64" in f.message for f in report.failures)


def test_a_cold_tower_that_is_not_v2_is_refused(clean):
    """§4.3: "the live model; the exporter **must** ship v2"."""
    import torch

    path = _artifacts(clean) / "cold_tower.pt"
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    ckpt["version"] = 1
    torch.save(ckpt, path)

    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert any("v2" in f.message for f in report.failures)


def test_a_contract_the_placer_will_reject_fails_validation_rather_than_the_import(clean):
    """§10 recomputes the rebuild set during import, and step 4 builds every unplaced title's
    vector from this contract. So a contract §8 stage 9's parser refuses does not produce a
    bad placement — it takes the whole import down.

    That is the right failure, in the wrong place. Without this check the operator standing in
    front of the import screen gets a 500 out of a background step; with it they get the
    parser's own sentence, on the validate pass, before anything is staged. The bundle shipped
    to this project's own e2e fixture was exactly this shape: block sizes declared, per-column
    `feature_names` absent.
    """
    import json

    path = _artifacts(clean) / "feature_contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract.pop("feature_names")
    path.write_text(json.dumps(contract), encoding="utf-8")

    report = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("feature_names" in f.message for f in report.failures)
