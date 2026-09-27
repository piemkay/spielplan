"""Validator tests (§4.1 landmine rules, §4.3 artifacts, §10's report): each broken bundle is caught by
exactly its rule, and legitimate shapes (duplicate tmdb_ids, both DNA tiers, non-ASCII) are not flagged."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from spielplan.importer import bundle as bundle_import
from spielplan.importer import load
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport
from spielplan.importer.validate import IDENTITY_ARRAY
from tests.fixtures import make_bundle as fx


@pytest.fixture
def clean(tmp_path):
    return fx.make_bundle(tmp_path / "bundle")


@pytest.fixture
def unidentified(tmp_path):
    """Decision 162's identity column stripped out; the corpus
    does not write it yet, so absence is the broken case."""
    root = fx.make_bundle(tmp_path / "bundle")
    fx.break_identity_missing(root)
    return root


def _rules(report, severity):
    return {f.rule for f in report.findings if f.severity == severity}


def _validate(root, *, reinventory: bool = True):
    """Re-inventories first, so each break is one failure and not also
    a sha256 mismatch. `reinventory=False` is the read-only door
    for `CORPUS_BUNDLE_DIR`, a bundle this repo must never modify."""
    if reinventory:
        fx.reinventory(root)
    return bundle_import.validate(bundle_import.Bundle.open(root))


def test_clean_bundle_validates(clean):
    report = _validate(clean)
    assert report.ok, report.render()
    assert report.bundle_version == "test-v1"
    assert report.vocabulary_version == "v1"


def test_legitimate_duplicates_are_notes_not_failures(clean):
    # §4.1 rule 6: duplicate tmdb_ids are legitimate; failing on them would reject every real bundle.
    report = _validate(clean)
    dupes = [f for f in report.findings if f.rule == "rule6-no-unique"]
    assert dupes and all(f.severity == "note" for f in dupes)
    assert any(f.detail.get("observed", 0) >= 1 for f in dupes)


def test_shared_dna_pairs_are_counted_not_deduped(clean):
    # §4.1 rule 1: pairs in both tiers are counted, never merged.
    report = _validate(clean)
    shared = next(f for f in report.findings if f.rule == "rule1-two-tiers")
    assert shared.severity == "note"
    assert shared.detail["shared"] == 3


def test_frozen_rating_source_ids(clean):
    fx.break_rating_source_ids(clean)
    report = _validate(clean)
    assert not report.ok
    assert "rule4-frozen-ids" in _rules(report, "fail")


def test_null_kind_fails(clean):
    """The corpus declares `kind TEXT NOT NULL`, so the reachable half of rule 5 is an unknown value."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("UPDATE title SET kind = 'episode' WHERE id = 3")
    db.commit()
    db.close()

    report = _validate(clean)
    assert not report.ok
    assert "rule5-kind" in _rules(report, "fail")


def test_tag_without_evidence_fails(clean):
    fx.break_evidence(clean)
    report = _validate(clean)
    assert not report.ok
    assert "rule1-evidence" in _rules(report, "fail")


def test_denylisted_table_fails(clean):
    fx.break_denylist(clean)
    report = _validate(clean)
    assert not report.ok
    assert "rule7-denylist" in _rules(report, "fail")


def test_merged_tiers_fail(clean):
    fx.break_merged_tiers(clean)
    report = _validate(clean)
    assert not report.ok
    assert "rule1-two-tiers" in _rules(report, "fail")


def test_out_of_range_salience_fails(clean):
    fx.break_salience(clean)
    report = _validate(clean)
    assert not report.ok
    assert "rule2-weights" in _rules(report, "fail")


def test_missing_rating_source_table_fails(clean):
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("DROP TABLE rating_source")
    db.commit()
    db.close()
    report = _validate(clean)
    assert not report.ok
    assert "rule4-frozen-ids" in _rules(report, "fail")


def test_non_ascii_survives_the_read_path(clean):
    """§4.1 rule 8: a row count proves nothing; the characters do."""
    report = _validate(clean)
    assert report.ok
    assert report.table_counts["title"] == 8

    db = sqlite3.connect(f"file:{clean / 'content.sqlite'}?mode=ro", uri=True)
    db.text_factory = str
    try:
        names = {r[0] for r in db.execute("SELECT primary_title FROM title")}
        overviews = " ".join(
            r[0] for r in db.execute("SELECT plot_full FROM title_meta WHERE plot_full IS NOT NULL")
        )
    finally:
        db.close()

    assert "重慶森林" in names                      # CJK
    assert "🎬" in overviews                # emoji
    assert "​" in overviews                    # zero-width space


def test_report_renders_human_readable(clean):
    report = _validate(clean)
    text = report.render()
    assert "bundle test-v1" in text
    assert "vocabulary v1" in text
    assert "rows:" in text


# §10 promises a report; one unexpected column used to abort the pass as an OperationalError.


@pytest.mark.parametrize(
    ("table", "column", "rule"),
    [
        ("dna_tag", "salience", "rule2-weights"),
        ("dna_evidence", "term", "rule1-evidence"),
        ("title", "kind", "rule5-kind"),
    ],
)
def test_a_column_the_validator_queries_but_the_bundle_lacks_is_reported_not_raised(
    clean, table, column, rule
):
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.commit()
    db.close()

    report = _validate(clean)               # the assertion is that this line returns at all

    assert not report.ok
    assert rule in _rules(report, "fail")
    named = [f for f in report.failures if column in f.message and table in f.message]
    assert named, (
        f"the report must name {table}.{column} — an operator holding a bundle the app cannot "
        f"read needs the table and the column, not a rule id. Got: {report.render()}"
    )


def test_the_report_names_every_violation_after_the_first_surprise(clean):
    """One surprise must not hide the real violations after it."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("ALTER TABLE dna_tag DROP COLUMN salience")     # the surprise
    db.execute("UPDATE rating_source SET id = 99 WHERE id = 31")
    db.execute("UPDATE title SET kind = 'episode' WHERE id = 3")
    db.execute("CREATE TABLE title_bak (id INTEGER)")
    db.commit()
    db.close()

    report = _validate(clean)

    assert {"rule2-weights", "rule4-frozen-ids", "rule5-kind", "rule7-denylist"} <= _rules(
        report, "fail"
    ), report.render()
    # ...and the counted facts are still collected.
    assert report.table_counts["title"] == 8


def test_a_content_db_with_none_of_the_dna_layer_reports_every_table_it_wanted(clean):
    """Every table this app cannot find is one line, not an exception."""
    db = sqlite3.connect(clean / "content.sqlite")
    for table in ("dna_tag", "dna_projected", "dna_evidence"):
        db.execute(f"DROP TABLE {table}")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    assert "rule1-two-tiers" in _rules(report, "fail")
    assert any("dna_tag" in f.message and "dna_projected" in f.message for f in report.failures)


def test_an_artifact_json_the_app_cannot_parse_is_reported_not_raised(clean):
    """A truncated JSON document is a report line, not a JSONDecodeError."""
    (_artifacts(clean) / "feature_contract.json").write_text("{not json", encoding="utf-8")

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("not readable JSON" in f.message for f in report.failures)


def test_a_contract_field_of_the_wrong_type_is_reported_not_raised(clean):
    """A failed `int()`/`float()` conversion must be a sentence, not a traceback."""
    payload = _contract(clean)
    payload["text_block"]["text_scale"] = "2.6055"

    _write_contract(clean, payload)

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("text_scale" in f.message for f in report.failures)


@pytest.mark.skipif(
    not os.environ.get("CORPUS_BUNDLE_DIR"),
    reason="CORPUS_BUNDLE_DIR is unset; set it to a real bundle directory to validate it",
)
def test_the_validator_reports_over_a_real_bundle():
    """Read-only and opt-in: the corpus bundle is 1.15 GB and lives outside this repo."""
    # Read-only: the corpus's own inventory is part of what is under test.
    report = _validate(Path(os.environ["CORPUS_BUNDLE_DIR"]), reinventory=False)

    text = report.render()
    assert report.table_counts, "the pass ended before it counted a single table"
    # Every shipped table counted, not just the ones before the first surprise.
    assert len(report.table_counts) >= 25, sorted(report.table_counts)
    assert all(f.rule and f.message for f in report.findings)
    assert "bundle unknown" not in text, text.splitlines()[0]
    # The real-bundle half of render()'s ASCII rule: only
    # a real corpus exercises interpolated names at width.
    assert text.isascii(), [line for line in text.splitlines() if not line.isascii()][:3]


def _artifacts(root):
    return root / "artifacts"


def _contract(root) -> dict:
    return json.loads((_artifacts(root) / "feature_contract.json").read_text(encoding="utf-8"))


def _write_contract(root, payload: dict) -> None:
    (_artifacts(root) / "feature_contract.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_the_shipped_model_artifacts_load_from_the_bundle_the_corpus_produces(clean):
    """The tower loads, both id vectors are found, the scale is
    read from `text_block`, and versions come from BUNDLE.json."""
    report = _validate(clean)

    assert report.ok, report.render()
    notes = {f.rule: f for f in report.findings if f.severity == "note"}
    assert "cold-tower" in notes, "the tower was never constructed, only inspected"
    assert notes["cold-tower"].detail["embed_dim"] == 64
    assert notes["feature-contract"].detail["text_scale"] == 2.0
    assert (report.bundle_version, report.vocabulary_version) == ("test-v1", "v1")


def test_a_bundle_that_records_no_version_is_refused_rather_than_named_unknown(clean):
    """Two imports stamped "unknown" cannot be told apart and would share one artifact directory."""
    (clean / "BUNDLE.json").unlink()

    report = _validate(clean)

    assert not report.ok
    assert "bundle-identity" in _rules(report, "fail")
    assert any("BUNDLE.json" in f.message for f in report.failures)


def test_the_backbone_id_vector_is_read_under_the_name_the_corpus_ships(clean):
    """`backbone.npz` ships `title_ids`; demanding `title_id` refused correct bundles."""
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["title_id"] = arrays.pop("title_ids")          # the name the app invented
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert any("title_ids" in f.message for f in report.failures), report.render()


def test_a_backbone_with_no_id_array_is_refused(clean):
    """Matching by row order would give every title another film's plausible coordinate."""
    fx.break_backbone_id_array(clean)

    report = _validate(clean)

    assert not report.ok
    assert any("title_ids" in f.message for f in report.failures)


def test_a_backbone_in_the_wrong_number_of_dimensions_is_refused(clean):
    """§1: one frozen 64-d space; every consumer indexes 64 columns."""
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        kept = {k: npz[k] for k in npz.files}
    kept["E"] = kept["E"][:, :32]
    np.savez(path, **kept)

    report = _validate(clean)

    assert not report.ok
    assert any("64" in f.message for f in report.failures)


def test_the_review_text_embedding_is_read_under_the_name_the_corpus_ships(clean):
    """The review-text block matched to the wrong rows scores a title on another film's reviews."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["title_id"] = arrays.pop("title_ids")
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "review-text" in _rules(report, "fail")
    assert any("title_ids" in f.message for f in report.failures)


def test_a_review_text_embedding_narrower_than_the_contract_truncates_to_is_refused(clean):
    """Truncation is not padding: fewer columns than the contract slices cannot produce the block."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["emb"] = arrays["emb"][:, :16]
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "review-text" in _rules(report, "fail")


def test_a_review_text_embedding_with_no_coverage_flags_is_refused(clean):
    """Without the coverage flags, 6,010 rows of float noise would read as review text."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files if k != "covered"}
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "review-text" in _rules(report, "fail")
    assert any("covered" in f.message for f in report.failures)


def test_coverage_flags_that_do_not_line_up_with_the_ids_are_refused(clean):
    """`features.py` indexes `covered` by position; a mismatched length shifts or breaks every flag."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["covered"] = arrays["covered"][:-1]
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert any("flags and the mapping disagree" in f.message for f in report.failures)


def test_a_contract_with_no_frozen_text_scale_is_refused(clean):
    """The corpus puts `text_scale` inside `text_block`; a defaulted scale moves every coordinate."""
    payload = _contract(clean)
    payload["text_block"].pop("text_scale")
    _write_contract(clean, payload)

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("text_scale" in f.message for f in report.failures)


def test_a_tower_whose_width_disagrees_with_its_contract_is_refused_at_import(clean):
    """A contract/tower width mismatch places at plausible nonsense without raising."""
    payload = _contract(clean)
    before = payload["input_dim"]
    # Three meta columns appended, so the contract still parses and only the WIDTH moves.
    payload["feature_names"] += ["lang:aa", "lang:bb", "lang:cc"]
    payload["content_blocks"][-1]["size"] += 3
    payload["content_dim"] += 3
    payload["input_dim"] += 3
    _write_contract(clean, payload)

    report = _validate(clean)

    assert not report.ok
    assert "cold-tower" in _rules(report, "fail")
    assert any(
        str(before) in f.message and str(before + 3) in f.message for f in report.failures
    ), report.render()


def test_a_cold_tower_that_is_not_v2_is_refused(clean):
    """Only a bare state_dict is taken as v2; a wrapper, whatever version it claims, is refused."""
    import torch

    path = _artifacts(clean) / "cold_tower.pt"
    state = dict(torch.load(path, map_location="cpu", weights_only=True))
    torch.save(
        {
            "state_dict": state,
            "version": 1,
            "arch": "cold_tower_v2",
            "input_dim": int(state["trunk.0.weight"].shape[1]),
            "embed_dim": int(state["head_e.weight"].shape[0]),
        },
        path,
    )

    report = _validate(clean)

    assert not report.ok
    assert "cold-tower" in _rules(report, "fail")
    assert any("v2" in f.message for f in report.failures)


def test_a_cold_tower_whose_heads_cannot_be_found_is_refused(clean):
    """§5.1 needs both heads; an unreconstructable checkpoint must fail here, not at first placement."""
    fx.break_cold_tower_heads(clean)

    report = _validate(clean)

    assert not report.ok
    assert "cold-tower" in _rules(report, "fail")


def test_a_contract_the_placer_will_reject_fails_validation_rather_than_the_import(clean):
    """A contract the placer rejects would take the import
    down; validate reports the parser's sentence first."""
    payload = _contract(clean)
    payload.pop("feature_names")
    _write_contract(clean, payload)

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("feature_names" in f.message for f in report.failures)


def test_a_vocabulary_directory_without_the_corpus_term_files_is_refused(clean):
    """The corpus ships `vocab_<version>_all.tsv` and per-facet TSVs, never `terms.tsv`."""
    vocab = _artifacts(clean) / "dna_vocab" / "v1"
    assert not (vocab / "terms.tsv").exists(), "the corpus ships no terms.tsv"
    for path in vocab.glob("vocab_*.tsv"):
        path.unlink()

    report = _validate(clean)

    assert not report.ok
    assert "vocabulary" in _rules(report, "fail")
    assert any("vocab_v1_all.tsv" in f.message for f in report.failures)


# A corpus-side merge changes what an id means without changing it; the ids still ascend.


def test_a_models_only_bundle_with_no_identity_vector_fails_validation(unidentified):
    """A models-only bundle has no spine, so without the identity vector the check would be skipped."""
    (unidentified / "content.sqlite").unlink()
    report = _validate(unidentified)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any(IDENTITY_ARRAY in f.message for f in report.failures)


def test_a_seed_with_no_identity_vector_is_checked_against_its_own_spine(unidentified):
    """A seed's own `content.sqlite` is a better identity source, so the absent vector is a warning."""
    report = _validate(unidentified)

    assert report.ok, report.render()
    assert "identity" not in _rules(report, "fail")
    assert "identity" in _rules(report, "warn")
    assert any(IDENTITY_ARRAY in f.message for f in report.findings if f.severity == "warn")


def test_a_seed_whose_spine_does_not_carry_a_backbone_id_is_still_refused(unidentified):
    """The spine fallback is a check, not a bypass."""
    import sqlite3

    db = sqlite3.connect(unidentified / "content.sqlite")
    db.execute("DELETE FROM title WHERE id = 1")
    db.commit()
    db.close()
    report = _validate(unidentified)

    assert not report.ok
    assert "identity" in _rules(report, "fail")


def test_an_identity_that_disagrees_with_the_spine_names_the_title(clean):
    """A re-identified id still ascends and resolves; only the identity column sees it."""
    fx.break_identity_mismatch(clean)          # title 1's imdb_id becomes tt0000001

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    named = [f for f in report.failures if "Heat" in f.message and "tt0113277" in f.message]
    assert named, report.render()
    assert named[0].detail["titles"][0]["title_id"] == 1


def test_a_title_with_no_imdb_id_is_identified_by_tmdb_and_kind(clean):
    """Per ROW: 2,139 shipped backbone titles have no imdb_id, so tmdb and kind identify them."""
    identity = np.load(_artifacts(clean) / "backbone.npz", allow_pickle=False)[IDENTITY_ARRAY]
    assert "tmdb:346648:movie" in identity.tolist(), "title 3 falls back to tmdb + kind"

    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("UPDATE title SET tmdb_id = 999999 WHERE id = 3")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any("Paddington 2" in f.message for f in report.failures), report.render()


def test_an_identity_vector_that_is_not_row_aligned_is_refused(clean):
    """A misaligned vector would pass a spot check on the first few rows."""
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays[IDENTITY_ARRAY] = arrays[IDENTITY_ARRAY][:-2]
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any("row-aligned" in f.message for f in report.failures)


def test_a_seed_that_lost_a_title_its_backbone_names_is_still_refused(clean):
    """Decision 248: against the bundle's OWN spine, an unclaimed
    backbone row is the bundle disagreeing with itself."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("DELETE FROM title WHERE id = 7")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any("does not carry" in f.message for f in report.failures)


def test_a_series_runtime_that_is_a_total_is_named_in_the_report(clean):
    """Decision 192: series `runtime_min` may be a total; the report names it, nothing divides it."""
    assert not any(f.rule == "runtime-semantics" for f in _validate(clean).findings), (
        "the committed fixture must be clean or this test cannot tell the branch apart"
    )
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("UPDATE title SET runtime_min = 1290 WHERE kind = 'series' AND id = 6")
    db.commit()
    db.close()

    report = _validate(clean)

    assert report.ok, report.render()
    notes = [f for f in report.findings if f.rule == "runtime-semantics"]
    assert [f.severity for f in notes] == ["note"]
    assert notes[0].detail == {"series": 1, "threshold": 110}
    assert "rather than minutes per episode" in notes[0].message


def test_a_latin_1_byte_in_an_id_bearing_ledger_is_a_report_line_not_an_exception(clean):
    """`validate_id_partition` runs first and opened both ledgers without the `_read_tsv` handler."""
    for name in ("corrections_v1.tsv", "dna_vocab/v1/adjudications_v1.tsv"):
        path = clean / "artifacts" / name
        path.write_bytes(path.read_bytes() + b"\ndirector\t1\tGeorges M\xe9li\xe8s\te\tn\n")

        report = _validate(clean)

        assert not report.ok, f"{name}: {report.render()}"
        named = [f for f in report.failures if "cannot be read as UTF-8 TSV" in f.message]
        assert any(path.name in f.message for f in named), f"{name}: {report.render()}"


def test_two_vocabulary_directories_are_a_report_line_even_when_the_bundle_declares_one(clean):
    """A declaring bundle skipped `version_of`, so two
    vocabulary directories raised instead of reporting."""
    shutil.copytree(
        clean / "artifacts" / "dna_vocab" / "v1", clean / "artifacts" / "dna_vocab" / "v2"
    )
    payload = _manifest(clean)
    payload["vocabulary_version"] = "v2"
    _write_manifest(clean, payload)

    report = _validate(clean)

    assert not report.ok, report.render()
    named = [f for f in report.failures if "vocabulary versions" in f.message]
    assert named, report.render()
    assert "v1, v2" in named[0].message, named[0].message

    # `_validate_model_artifacts` is where the raise came from, so it is asked directly too.
    direct = ImportReport(bundle_version="test-v1")
    direct.vocabulary_version = "v2"
    validator.validate_artifacts(clean / "artifacts", direct)
    assert not direct.ok, direct.render()
    vocabulary = [f for f in direct.failures if f.rule == "vocabulary"]
    assert vocabulary, direct.render()
    assert vocabulary[0].detail["versions"] == ["v1", "v2"]


def test_a_vocabulary_declaration_that_is_not_a_string_leaves_one_answer_on_the_screen(clean):
    """A non-string declaration must not give two different vocabulary answers on one screen."""
    for declaration in (1, True, ["v1"]):
        payload = _manifest(clean)
        payload["vocabulary_version"] = declaration
        _write_manifest(clean, payload)

        report = _validate(clean)

        assert report.ok, report.render()
        assert report.vocabulary_version == "v1", declaration
        assert "dna_vocab/1/" not in report.render()


def _retag_vocabulary(root: Path, version: str) -> None:
    """The version is in the directory AND every filename, so a retag moves files, not just a directory."""
    vocab = root / "artifacts" / "dna_vocab"
    (vocab / "v1").rename(vocab / version)
    for path in sorted((vocab / version).iterdir()):
        if "_v1" in path.name:
            path.rename(path.with_name(path.name.replace("_v1", f"_{version}")))


def test_a_declaration_that_contradicts_the_shipped_tree_is_refused_rather_than_preferred(clean):
    """A declaration that contradicts the shipped tree is refused, or both DNA blocks go empty."""
    _models_only(clean)
    _retag_vocabulary(clean, "v2")
    payload = _manifest(clean)
    payload["vocabulary_version"] = "v1"
    _write_manifest(clean, payload)

    report = _validate(clean)

    assert not report.ok, report.render()
    named = [f for f in report.failures if "two answers" in f.message]
    assert len(named) == 1, report.render()
    assert "'v1'" in named[0].message and "dna_vocab/v2/" in named[0].message

    # The second reader is asked directly: `validate()` stops at the first.
    direct = ImportReport(bundle_version="test-v1")
    validator.validate_artifacts(clean / "artifacts", direct)
    assert not direct.ok, direct.render()
    two = [f for f in direct.failures if f.rule == "vocabulary" and "two answers" in f.message]
    assert len(two) == 1, direct.render()
    assert two[0].detail == {"declared": "v1", "derived": "v2"}
    # `declared or derived` still decides the ANSWER, so nothing else in the report moves.
    assert direct.vocabulary_version == "v1"


def test_the_id_boundary_reads_the_adjudications_ledger_the_version_names(clean):
    """Decision 247 guard 3: the boundary check must read the file the version names."""
    _retag_vocabulary(clean, "v2")
    ledger = clean / "artifacts" / "dna_vocab" / "v2" / "adjudications_v2.tsv"
    offender = bundle_import.APP_ID_MIN + 7
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + f"title\t{offender}\tmood.dread\tkeep\t\t\t\t\n",
        encoding="utf-8",
    )
    fx.reinventory(clean)

    opened = bundle_import.Bundle.open(clean)
    assert opened.vocabulary_version == "v2", "the fixture no longer builds the tree it claims"
    report = ImportReport(bundle_version=opened.version)
    bundle_import.validate_id_partition(opened, report)

    assert not report.ok, report.render()
    offending = [f for f in report.failures if f.rule == "id-partition"]
    assert len(offending) == 1, report.render()
    assert "dna_vocab/v2/adjudications_v2.tsv" in offending[0].message
    assert str(offender) in offending[0].message


def test_a_bundle_that_names_a_vocabulary_and_ships_none_is_not_told_it_names_nothing(clean):
    """A bundle that declares a vocabulary but ships no tree must not be told it names nothing."""
    _models_only(clean)
    shutil.rmtree(clean / "artifacts" / "dna_vocab")
    payload = _manifest(clean)
    payload["vocabulary_version"] = "v1"
    _write_manifest(clean, payload)

    report = _validate(clean)

    assert report.ok, report.render()
    assert report.vocabulary_version == "v1"
    vocabulary = [f for f in report.findings if f.rule == "vocabulary"]
    assert len(vocabulary) == 1 and vocabulary[0].severity == "warn", report.render()
    assert "'v1'" in vocabulary[0].message and "dna_vocab/v1/" in vocabulary[0].message
    assert "names nothing" not in report.render()


def test_a_table_count_bundle_json_gets_wrong_is_refused_before_anything_is_staged(clean):
    """Table counts are checkable before anything is copied, so validate refuses them up front."""
    assert _validate(clean).ok

    payload = _manifest(clean)
    payload["tables"] = {**payload["tables"], "title": 1008}
    _write_manifest(clean, payload)

    report = _validate(clean, reinventory=False)

    assert not report.ok, report.render()
    drift = [f for f in report.failures if f.rule == "bundle-integrity"]
    assert len(drift) == 1, report.render()
    assert "title" in drift[0].message and "1,008" in drift[0].message


def _manifest(root) -> dict:
    return json.loads((root / "BUNDLE.json").read_text(encoding="utf-8"))


def _write_manifest(root, payload: dict) -> None:
    (root / "BUNDLE.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _integrity(root) -> ImportReport:
    report = ImportReport()
    validator._verify_bundle_files(root, report)
    return report


def _models_only(root):
    """No real model bundle exists yet, so the models-only shape is constructed here."""
    spine = validator._spine_identities(root / "content.sqlite", ImportReport())
    (root / "content.sqlite").unlink()
    fx.reinventory(root)          # a spine removed from the tree is removed from its inventory
    return spine


def test_every_listed_file_is_hashed_before_a_row_is_written(clean):
    """A same-size edit is the case only a hash can see (a zeroed `equating_map.json` went active)."""
    listed = _manifest(clean)["files"]
    assert "content.sqlite" in listed and len(listed) >= 30, sorted(listed)[:5]

    report = _integrity(clean)
    assert report.ok, report.render()
    assert any(f.rule == "bundle-integrity" and "sha256 verified" in f.message
               for f in report.findings)

    target = clean / "artifacts" / "equating_map.json"
    target.write_bytes(b"0" * len(target.read_bytes()))
    assert target.stat().st_size == listed["artifacts/equating_map.json"]["bytes"]
    # Directly, not through `_validate`, which would re-inventory and erase the edit.
    stale = bundle_import.validate(bundle_import.Bundle.open(clean))
    assert not stale.ok, "the hash pass is wired into validate() and has to see this"
    assert {f.rule for f in stale.failures} == {"bundle-integrity"}, (
        "every other rule still passes; only the hash pass can see a same-size edit: "
        + stale.render()
    )

    report = _integrity(clean)
    assert not report.ok
    failure = report.failures[0]
    assert failure.rule == "bundle-integrity"
    assert "equating_map.json" in failure.message
    assert failure.detail["first"]["declared_sha256"] == (
        listed["artifacts/equating_map.json"]["sha256"]
    )


def test_a_listed_file_that_is_missing_and_an_unlisted_file_are_the_same_rule(clean):
    """Both mean the tree on disk is not the tree the corpus inventoried."""
    (clean / "artifacts" / "audit.json").unlink()
    (clean / "artifacts" / "curator-notes.txt").write_text("left behind\n", encoding="utf-8")

    report = _integrity(clean)

    assert not report.ok
    assert {f.rule for f in report.failures} == {"bundle-integrity"}
    assert any("artifacts/audit.json" in f.message for f in report.failures), report.render()
    assert any("artifacts/curator-notes.txt" in f.message for f in report.failures)


def test_bundle_json_is_the_only_file_exempt_from_the_unlisted_rule(clean):
    """Exempt by PATH: a second BUNDLE.json one directory down is still uninventoried."""
    listed = _manifest(clean)["files"]
    on_disk = {p.relative_to(clean).as_posix() for p in clean.rglob("*") if p.is_file()}
    assert on_disk - set(listed) == {"BUNDLE.json"}
    assert _integrity(clean).ok

    (clean / "artifacts" / "BUNDLE.json").write_text("{}", encoding="utf-8")

    report = _integrity(clean)
    assert not report.ok
    assert any("artifacts/BUNDLE.json" in f.message for f in report.failures), report.render()


def test_a_validations_entry_that_is_not_ok_is_a_failure(clean):
    """A failed `validations` row is the exporter saying the bundle is wrong."""
    payload = _manifest(clean)
    payload["validations"][0] = {
        "check": "deny_list", "ok": False, "detail": "shipped tables clean; bad=['review_bak']",
    }
    _write_manifest(clean, payload)

    report = _integrity(clean)

    assert not report.ok
    named = [f for f in report.failures if "deny_list" in f.message]
    assert named, report.render()
    assert "review_bak" in named[0].message
    assert named[0].detail["check"] == "deny_list"


def test_a_truncated_sqlite_file_is_a_report_line_not_an_exception(clean):
    """Both truncated databases are report lines now, not a 500 or an uncaught error mid-import."""
    for name in ("content.sqlite", "reviews.sqlite"):
        path = clean / name
        path.write_bytes(path.read_bytes()[:2048])

    report = _integrity(clean)

    assert not report.ok
    malformed = [f.message for f in report.failures if "not a readable SQLite database" in f.message]
    assert any("content.sqlite" in m for m in malformed), report.render()
    assert any("reviews.sqlite" in m for m in malformed)

    db = sqlite3.connect(f"file:{clean / 'content.sqlite'}?mode=ro", uri=True)
    try:
        content = validator.validate_content(db, ImportReport())
    finally:
        db.close()
    assert not content.ok
    assert any("content.sqlite cannot be read" in f.message for f in content.failures)


def test_a_listed_file_that_cannot_be_read_is_a_report_line_not_an_exception(clean, monkeypatch):
    """Injected rather than provoked: EACCES states are not portable to a temp directory."""
    denied = clean / "artifacts" / "backbone.npz"
    real = validator._sha256

    def refuse(path):
        if path == denied:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(validator, "_sha256", refuse)

    report = _integrity(clean)

    assert not report.ok
    named = [f.message for f in report.failures if "artifacts/backbone.npz" in f.message]
    assert len(named) == 1, report.render()
    assert "cannot be read" in named[0] and "PermissionError" in named[0], named[0]
    # One unreadable file is one line; the rest of the inventory is still checked.
    assert not [f for f in report.failures if "are not in the bundle" in f.message], report.render()
    assert not [f for f in report.failures if "do not match BUNDLE.json" in f.message], report.render()


def test_a_manifest_value_this_app_cannot_read_is_a_report_line_not_an_exception(clean):
    """Bare `int()` coercions of BUNDLE.json values reached the operator as "Internal Server Error"."""
    original = _manifest(clean)

    # Two passes: `validate()` stops at the integrity boundary.
    manifest = json.loads(json.dumps(original))
    first = sorted(manifest["files"])[0]
    manifest["files"][first]["bytes"] = "abc"
    _write_manifest(clean, manifest)

    report = bundle_import.validate(bundle_import.Bundle.open(clean))

    assert not report.ok, report.render()
    named = [f.message for f in report.failures if f.rule == "bundle-integrity"]
    assert any(first in m and "'abc'" in m for m in named), report.render()
    # Enumerated rather than abandoned: the other files are still hashed.
    assert not any("do not match BUNDLE.json" in m for m in named), report.render()
    assert not any("are not in the bundle" in m for m in named), report.render()

    manifest = json.loads(json.dumps(original))
    manifest["tables"]["title"] = "many"
    _write_manifest(clean, manifest)

    counts = bundle_import.validate(bundle_import.Bundle.open(clean))

    assert not counts.ok, counts.render()
    unreadable = [f.message for f in counts.failures if f.rule == "bundle-integrity"]
    assert any("'many'" in m and "title" in m for m in unreadable), counts.render()
    # The readable counts are still compared.
    assert not any("do not hold the number of rows" in m for m in unreadable), counts.render()


def test_an_inventory_entry_that_declares_no_digest_is_not_counted_as_verified(clean):
    """An entry with no digest was counted as "sha256 verified"; declarations are now counted."""
    target = clean / "artifacts" / "equating_map.json"
    original = len(target.read_bytes())
    target.write_bytes(b"{}" + b" " * (original - 2))
    assert target.stat().st_size == original, "same size: only a hash can see this"

    payload = _manifest(clean)
    payload["files"] = {name: entry["bytes"] for name, entry in payload["files"].items()}
    _write_manifest(clean, payload)

    report = _integrity(clean)

    assert not report.ok, report.render()
    named = [f.message for f in report.failures if f.rule == "bundle-integrity"]
    assert any("artifacts/equating_map.json" in m for m in named), report.render()
    # The affirming sentence is not written beside failures.
    assert not any("sha256 verified" in f.message for f in report.findings), report.render()

    # Four spellings of a declaration that is not there.
    for shape in ({}, None, 0, {"bytes": original}, {"bytes": original, "sha256": ""}):
        payload = _manifest(clean)
        payload["files"]["artifacts/equating_map.json"] = shape
        _write_manifest(clean, payload)

        degenerate = _integrity(clean)

        assert not degenerate.ok, f"{shape!r}: {degenerate.render()}"
        assert any(
            "artifacts/equating_map.json" in f.message for f in degenerate.failures
        ), f"{shape!r}: {degenerate.render()}"


def test_a_feature_contract_block_with_no_size_is_reported_not_raised(clean):
    """A non-object block or non-numeric `size` must be a warn
    here and a parser failure below, never a traceback."""
    contract_path = clean / "artifacts" / "feature_contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert isinstance(contract["content_blocks"], list) and contract["content_blocks"]
    contract["content_blocks"] = [
        "a block written as its own name", {"name": "genre", "size": "wide"},
        {"name": "era", "size": None}, *contract["content_blocks"],
    ]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    report = _validate(clean)

    unreadable = [f for f in report.findings if f.rule == "feature-contract"
                  and "content_blocks" in f.message]
    assert len(unreadable) == 1, report.render()
    assert unreadable[0].severity == "warn", unreadable[0].message
    assert unreadable[0].detail["sized"] == len(contract["content_blocks"]) - 3
    # The parser's own reading is a failure with a sentence.
    parsed = [f for f in report.failures if f.rule == "feature-contract"]
    assert parsed, report.render()
    assert any("cannot be parsed" in f.message or "usable `size`" in f.message for f in parsed), (
        report.render()
    )


# Orphans used to pass validation and raise inside the transaction as a 500.


def test_an_orphan_child_row_names_the_table_the_column_the_count_and_the_first_ids(clean):
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("INSERT INTO title_genre (title_id, source, genre) VALUES (4242, 'tmdb', 'noir')")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    orphan = next(f for f in report.failures if f.rule == "integrity-orphan")
    assert "title_genre" in orphan.message and "title_id" in orphan.message
    assert "4242" in orphan.message, orphan.message
    assert orphan.detail["table"] == "title_genre"
    assert orphan.detail["column"] == "title_id"
    assert orphan.detail["rows"] == 1
    assert orphan.detail["ids"] == [4242]


def test_a_null_in_a_mapped_not_null_column_is_a_failure(clean):
    """`award.body` is NOT NULL with no rule-6 coalesce."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("UPDATE award SET award = NULL WHERE id = 1")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    nulls = next(f for f in report.failures if f.rule == "integrity-null")
    assert "award.award" in nulls.message, nulls.message
    assert nulls.detail == {"table": "award", "column": "award", "rows": 1}


def test_a_duplicate_group_under_the_apps_key_is_a_failure(clean):
    """`_primary_role` collapses 0 and NULL, so rows distinct in the file collide under the app's key."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.executescript(
        """
        CREATE TABLE title_language_export AS SELECT * FROM title_language;
        DROP TABLE title_language;
        ALTER TABLE title_language_export RENAME TO title_language;
        INSERT INTO title_language (title_id, source, language, is_primary)
             VALUES (1, 'tmdb', 'en', 0), (1, 'tmdb', 'en', NULL);
        """
    )
    db.commit()
    raw = db.execute(
        "SELECT count(*) FROM (SELECT title_id, source, language, is_primary FROM title_language"
        " GROUP BY title_id, source, language, is_primary HAVING count(*) > 1)"
    ).fetchone()[0]
    db.close()
    assert raw == 0, "the rows differ in SQLite; only the transform makes them one key"

    report = _validate(clean)

    assert not report.ok
    duplicate = next(f for f in report.failures if f.rule == "integrity-duplicate")
    assert "title_language" in duplicate.message
    assert duplicate.detail["groups"] == 1
    assert duplicate.detail["key"] == ["title_id", "source", "language", "role"]


def test_a_declared_nullable_pk_component_whose_affinity_is_not_text_fails(clean):
    """Coalescing writes ''; a non-TEXT component declared nullable cannot hold it."""
    payload = _manifest(clean)
    assert payload["nullable_pk_columns"] == {
        "title_alias": [{"column": "region", "affinity": "TEXT"}]
    }
    assert _validate(clean).ok, "TEXT is the affinity rule 6's coalesce is written for"

    payload["nullable_pk_columns"]["title_alias"] = [{"column": "region", "affinity": "INTEGER"}]
    payload["nullable_pk_columns"]["ml_genome_score"] = [
        {"column": "movie_id", "affinity": "INTEGER"}
    ]
    _write_manifest(clean, payload)

    report = _validate(clean)

    assert not report.ok
    named = next(f for f in report.failures if f.rule == "rule6-coalesce")
    assert "title_alias.region" in named.message and "INTEGER" in named.message
    assert "ml_genome_score" not in named.message, (
        "the rule is about the columns this importer coalesces, not about every declared one"
    )


def test_every_table_the_integrity_gates_name_is_one_this_app_actually_loads():
    """Gates must key off what COPY reaches, not the bundle's
    schema, or a skipped table still refuses the seed."""
    gated = (
        {child for child, _, _, _ in validator._FOREIGN_KEYS}
        | {parent for _, _, parent, _ in validator._FOREIGN_KEYS}
        | {table for table, _ in validator._NOT_NULL_COLUMNS}
    )
    loaded = {tmap.source for tmap in load.MAPPINGS} | set(load.BESPOKE_TABLES)

    stale = sorted(gated - loaded)
    declined = sorted(t for t in stale if t in load.SKIPPED_TABLES)
    assert not stale, (
        f"the integrity gates declare expectations for {stale}, which no COPY reaches; "
        f"{declined} are tables this app has declined, so the gate can only refuse a seed "
        "over rows nothing would have loaded"
    )


def test_a_denied_table_in_the_reviews_database_is_found_too(clean):
    """§8 stage 5 re-extracts from these bodies, so a `review_bak` here is rule 7's class."""
    db = sqlite3.connect(clean / "reviews.sqlite")
    db.execute("CREATE TABLE review_bak (id INTEGER PRIMARY KEY, body TEXT)")
    db.commit()
    db.close()

    db = sqlite3.connect(f"file:{clean / 'reviews.sqlite'}?mode=ro", uri=True)
    try:
        report = validator.validate_reviews(db, ImportReport())
    finally:
        db.close()

    assert not report.ok
    denied = next(f for f in report.failures if f.rule == "rule7-denylist")
    assert "review_bak" in denied.message, denied.message
    assert denied.detail["database"] == "reviews.sqlite"


def test_a_table_whose_name_merely_contains_good_is_not_denied(clean):
    """§4.1 anchors `%_good` at the END; `title_goodness` is not a stale copy."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("CREATE TABLE title_goodness (title_id INTEGER, score REAL)")
    db.commit()
    db.close()
    assert "rule7-denylist" not in _rules(_validate(clean), "fail")

    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("ALTER TABLE title_goodness RENAME TO title_good")
    db.commit()
    db.close()

    report = _validate(clean)
    assert "rule7-denylist" in _rules(report, "fail")
    assert any("title_good" in f.message for f in report.failures), report.render()


@pytest.mark.parametrize("relative", [
    "corrections_v1.tsv",
    "dna_vocab/v1/adjudications_v1.tsv",
    "dna_vocab/v1/alias_map_v1.tsv",
])
def test_a_latin_1_byte_in_a_curated_ledger_is_a_report_line_not_an_exception(clean, relative):
    """Hand-edited ledgers opened without a handler, from a route that promises a report."""
    path = clean / "artifacts" / relative
    path.write_bytes(path.read_bytes() + b"caf\xe9\t1\n")

    report = ImportReport()
    rows = validator._read_tsv(path, report, "corrections")

    assert rows is None, "an unreadable ledger is not an empty one (decision 247)"
    assert not report.ok
    assert path.name in report.failures[0].message
    assert report.failures[0].detail["file"] == path.name


def test_a_curated_ledger_whose_header_is_not_the_shipped_one_is_named(clean):
    """A column the file lacks reads as nothing, which is how a header no ledger carries went unnoticed."""
    path = clean / "artifacts" / "corrections_v1.tsv"
    report = ImportReport()

    assert validator._read_tsv(path, report, "corrections", ("title_id",)) is not None
    assert report.ok, report.render()

    assert validator._read_tsv(path, report, "corrections", ("invented_column",)) is None
    assert not report.ok
    assert "invented_column" in report.failures[0].message


@pytest.mark.parametrize("payload", [
    "{ not json at all",
    '{"count": 8}',
    '[{"title": "Heat"}]',
])
def test_a_malformed_seed_list_is_a_report_line_not_an_exception(clean, payload):
    """Checked to the shape the loader relies on, before the transaction."""
    (clean / "artifacts" / "seed_list.json").write_text(payload, encoding="utf-8")

    report = _validate(clean)

    assert not report.ok
    assert "seed-list" in _rules(report, "fail"), report.render()


def test_an_onboarding_title_the_bundles_own_spine_lacks_is_refused(clean):
    """One unseeded `title_id` aborts the whole import on a constraint, not a file."""
    path = clean / "artifacts" / "seed_list.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["title_id"] = 4242
    path.write_text(json.dumps(entries), encoding="utf-8")

    report = _validate(clean)

    assert not report.ok
    named = next(f for f in report.failures if f.rule == "seed-list")
    assert "4242" in named.message
    assert named.detail["title_ids"] == [4242]


def test_an_onboarding_year_this_app_cannot_read_is_a_note_and_not_a_crash(clean):
    """A `NaN` year is a note here, before the operator commits, not a ValueError after the copy."""
    path = clean / "artifacts" / "seed_list.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["year"] = "__nan__"
    entries[1]["year"] = "__inf__"
    # Substituted, not dumped: the bare `NaN` and `Infinity` literals are the point.
    path.write_text(
        json.dumps(entries).replace('"__nan__"', "NaN").replace('"__inf__"', "Infinity"),
        encoding="utf-8",
    )

    report = _validate(clean)

    assert report.ok, report.render()
    note = next(
        f for f in report.findings
        if f.rule == "seed-list" and "cannot read as a number" in f.message
    )
    assert note.severity == "note", note.message
    assert note.detail["rows"] == 2, note.message
    assert str(entries[0]["title_id"]) in note.message, note.message


@pytest.mark.parametrize("kind", ["seed", "model"])
@pytest.mark.parametrize("filename,rule", [
    ("feature_contract.json", "feature-contract"),
    ("cold_tower.pt", "cold-tower"),
    ("backbone.npz", "backbone"),
])
def test_an_absent_model_artifact_is_a_failure_on_both_bundle_kinds(clean, kind, filename, rule):
    """Decision 251: the rebuild needs all three on every
    import; an absent array fits from zeros silently."""
    spine = _models_only(clean) if kind == "model" else None
    (clean / "artifacts" / filename).unlink()
    fx.reinventory(clean)         # one absence, one line: not this one and a sha256 mismatch

    report = bundle_import.validate(bundle_import.Bundle.open(clean), spine=spine)

    assert not report.ok
    named = [f for f in report.failures if f.rule == rule and filename in f.message]
    assert named, report.render()
    assert "decision 251" in named[0].message
    assert not any(f.severity == "warn" and filename in f.message for f in report.findings), (
        "one absence, one line: an optional-artifact warn beside the failure says both"
    )


def test_dna_rows_with_no_vocabulary_directory_are_refused_and_an_empty_one_is_not(clean):
    shutil.rmtree(clean / "artifacts" / "dna_vocab")

    report = _validate(clean)

    assert not report.ok
    vocabulary = next(f for f in report.failures if f.rule == "vocabulary")
    assert "dna_tag" in vocabulary.message and "dna_projected" in vocabulary.message, (
        vocabulary.message
    )
    # Named apart, not summed: §4.1 rule 1 is a claim about rows in two tiers.
    assert (vocabulary.detail["dna_tag"], vocabulary.detail["dna_projected"]) == (9, 8), (
        "the fixture ships 9 dna_tag and 8 dna_projected rows"
    )

    db = sqlite3.connect(clean / "content.sqlite")
    db.executescript("DELETE FROM dna_tag; DELETE FROM dna_projected; DELETE FROM dna_evidence;")
    db.commit()
    db.close()

    report = _validate(clean)
    assert report.ok, report.render()
    assert "vocabulary" in _rules(report, "warn")


def test_an_identity_naming_a_title_the_spine_does_not_carry_is_a_counted_note(clean):
    """Decision 248: an uncovered id is inert and counted as a note; the model bundle is constructed."""
    spine = _models_only(clean)
    del spine[7]

    report = bundle_import.validate(bundle_import.Bundle.open(clean), spine=spine)

    assert report.ok, report.render()
    assert "identity" not in _rules(report, "fail")
    note = next(
        f for f in report.findings if f.rule == "identity" and f.severity == "note"
    )
    assert "never seeded" in note.message
    assert note.detail["rows"] == 1
    assert note.detail["title_ids"] == [7]


def test_an_identity_that_disagrees_with_the_spine_is_still_a_failure(clean):
    """A re-identified id is still a refusal on the same report."""
    spine = _models_only(clean)
    kind, _imdb_id, tmdb_id, name = spine[1]
    spine[1] = (kind, "tt9999999", tmdb_id, name)

    report = bundle_import.validate(bundle_import.Bundle.open(clean), spine=spine)

    assert not report.ok
    named = [f for f in report.failures if f.rule == "identity"]
    assert named, report.render()
    assert "Heat" in named[0].message and "tt0113277" in named[0].message


def test_a_backbone_whose_identity_token_is_neither_form_is_still_a_failure(clean):
    """An unreadable token is not a catalogue that moved on."""
    spine = _models_only(clean)
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    tokens = [str(token) for token in arrays[IDENTITY_ARRAY]]
    tokens[0] = "wikidata:Q123"
    arrays[IDENTITY_ARRAY] = np.array(tokens)
    np.savez(path, **arrays)
    fx.reinventory(clean)         # the token is the break; a stale hash beside it is not

    report = bundle_import.validate(bundle_import.Bundle.open(clean), spine=spine)

    assert not report.ok
    assert any("wikidata:Q123" in f.message for f in report.failures), report.render()
