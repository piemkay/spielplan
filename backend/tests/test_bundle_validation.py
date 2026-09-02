"""Validator tests — spec v2.1 §4.1 landmine rules, §4.3's artifacts, §10's report.

Each broken-bundle test asserts the validator catches exactly the rule it was built to catch;
the clean-bundle test asserts it does NOT flag the things that are legitimately present
(duplicate tmdb_ids, (title,term) pairs in both DNA tiers, non-ASCII text).

M4.5 added the three properties the shipped bundle disproved: a schema this app does not expect
produces a report line rather than an OperationalError (§10), every model artifact is named and
shaped the way the corpus actually writes it (§4.3), and the Backbone's identity column is
checked against the spine rather than trusted (decision 162).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from spielplan.importer import bundle as bundle_import
from spielplan.importer.validate import IDENTITY_ARRAY
from tests.fixtures import make_bundle as fx


@pytest.fixture
def clean(tmp_path):
    return fx.make_bundle(tmp_path / "bundle")


@pytest.fixture
def unidentified(tmp_path):
    """A bundle with decision 162's identity column stripped back out.

    The corpus does not write this array yet; the exporter has to add it. The fixture does,
    because the row requires the importer to check it — so the *absent* case is the broken
    bundle here, built by the `break_*` helper like every other rule in this file.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    fx.break_identity_missing(root)
    return root


def _rules(report, severity):
    return {f.rule for f in report.findings if f.severity == severity}


def _validate(root):
    return bundle_import.validate(bundle_import.Bundle.open(root))


def test_clean_bundle_validates(clean):
    report = _validate(clean)
    assert report.ok, report.render()
    assert report.bundle_version == "test-v1"
    assert report.vocabulary_version == "v1"


def test_legitimate_duplicates_are_notes_not_failures(clean):
    # §4.1 rule 6: duplicate tmdb_id values are legitimate (movie/series pairs); the bundle
    # fixture has one. A validator that failed on this would reject every real bundle.
    report = _validate(clean)
    dupes = [f for f in report.findings if f.rule == "rule6-no-unique"]
    assert dupes and all(f.severity == "note" for f in dupes)
    assert any(f.detail.get("observed", 0) >= 1 for f in dupes)


def test_shared_dna_pairs_are_counted_not_deduped(clean):
    # §4.1 rule 1: pairs in both tiers "must stay distinguishable" — counted, never merged.
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
    """§4.1 rule 5: `kind` is non-null and movie/series only — "the unpartitioned crowd top-10
    is 8/10 TV series, so this is not cosmetic".

    Broken here rather than through `fx.break_kind`, which sets `kind = NULL` and now raises
    `IntegrityError` before the validator is ever called: the fixture's DDL is the corpus's, and
    the corpus declares `kind TEXT NOT NULL`. A NULL is unreachable in a bundle whose schema
    matches the artifact, so the reachable half of the rule is the unknown value.
    """
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
    """§4.1 rule 8: never "clean" non-ASCII — the corpus legitimately contains CJK, RTL
    scripts, ZWSP and emoji. A row count proves nothing here; the characters do."""
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


# --- §10's report is a list, and a list has to survive its first surprise ----------------------
#
# "Importer enforces every §4.1 landmine rule and produces a migration report (counts per table,
# validation failures, vocabulary version)." Against the shipped bundle it produced an
# OperationalError instead — `dna_evidence.dna_tag_id`, a column no exported bundle has ever
# carried — so the operator got a stack trace where §10 promises a page, and every rule after
# the first surprise went unchecked.


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
    """The expensive half of the defect. One unreadable column aborted the whole pass, so a
    bundle with a schema surprise AND three real landmine violations reported the surprise as a
    traceback and the violations not at all."""
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
    # …and the counted facts are still collected: a report that stops enumerating stops counting.
    assert report.table_counts["title"] == 8


def test_a_content_db_with_none_of_the_dna_layer_reports_every_table_it_wanted(clean):
    """§10's manifest lists the DNA layer as part of the bundle. A bundle without it is a
    reportable bundle, not an exception: every table this app cannot find is one line."""
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
    """The same defect one file type over: the validator reads four JSON documents out of the
    bundle, and a truncated one has to be a report line rather than a JSONDecodeError thrown
    from inside the pass the operator is watching."""
    (_artifacts(clean) / "feature_contract.json").write_text("{not json", encoding="utf-8")

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("not readable JSON" in f.message for f in report.failures)


def test_a_contract_field_of_the_wrong_type_is_reported_not_raised(clean):
    """A value of the wrong TYPE reaches the contract parser as an `int()`/`float()` conversion
    rather than as its own refusal, and a conversion that fails is a traceback. §10 has no room
    for one: the frozen scale is a number or the bundle is refused in a sentence."""
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
    """The row's own sentence: "the validator run over a real bundle yields a failure list an
    operator can read". Read-only, and opt-in — the corpus bundle is 1.15 GB and lives outside
    this repo."""
    report = _validate(Path(os.environ["CORPUS_BUNDLE_DIR"]))

    text = report.render()
    assert report.table_counts, "the pass ended before it counted a single table"
    # Every shipped table counted, not just the ones before the first surprise.
    assert len(report.table_counts) >= 25, sorted(report.table_counts)
    assert all(f.rule and f.message for f in report.findings)
    assert "bundle unknown" not in text, text.splitlines()[0]


# --- §4.3's model artifacts, checked against each other ---------------------------------------
#
# §10 puts validation before the flip so a bad bundle never becomes the active one. Every failure
# below otherwise surfaces later and somewhere else, and one of them never surfaces at all.


def _artifacts(root):
    return root / "artifacts"


def _contract(root) -> dict:
    return json.loads((_artifacts(root) / "feature_contract.json").read_text(encoding="utf-8"))


def _write_contract(root, payload: dict) -> None:
    (_artifacts(root) / "feature_contract.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_the_shipped_model_artifacts_load_from_the_bundle_the_corpus_produces(clean):
    """The row, positively: the tower the exporter actually writes loads, the two id vectors are
    found under the names they ship, the frozen scale is read from inside `text_block`, and both
    versions come out of `BUNDLE.json` at the bundle root rather than being reported "unknown"."""
    report = _validate(clean)

    assert report.ok, report.render()
    notes = {f.rule: f for f in report.findings if f.severity == "note"}
    assert "cold-tower" in notes, "the tower was never constructed, only inspected"
    assert notes["cold-tower"].detail["embed_dim"] == 64
    assert notes["feature-contract"].detail["text_scale"] == 2.0
    assert (report.bundle_version, report.vocabulary_version) == ("test-v1", "v1")


def test_a_bundle_that_records_no_version_is_refused_rather_than_named_unknown(clean):
    """§10's re-import is "a planned admin event with a diff report". Two imports both stamped
    "unknown" are not a diff, and `unknown` is also the artifact directory they would share."""
    (clean / "BUNDLE.json").unlink()

    report = _validate(clean)

    assert not report.ok
    assert "bundle-identity" in _rules(report, "fail")
    assert any("BUNDLE.json" in f.message for f in report.failures)


def test_the_backbone_id_vector_is_read_under_the_name_the_corpus_ships(clean):
    """`backbone.npz` ships `title_ids`. The app demanded `title_id`, so on every real bundle it
    reported the id vector absent while the vector was sitting in the file — and then refused a
    bundle that was, in this respect, correct."""
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["title_id"] = arrays.pop("title_ids")          # the name the app invented
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert any("title_ids" in f.message for f in report.failures), report.render()


def test_a_backbone_with_no_id_array_is_refused(clean):
    """§4.3 lists "E, E_full, b_i, μ … item_n" and names no id mapping — but E's rows have no
    stated correspondence to `title.id`, so without one the basis is unusable and the only safe
    reading is a refusal. Matching by row order instead would give every title a plausible
    coordinate belonging to some other film."""
    fx.break_backbone_id_array(clean)

    report = _validate(clean)

    assert not report.ok
    assert any("title_ids" in f.message for f in report.failures)


def test_a_backbone_in_the_wrong_number_of_dimensions_is_refused(clean):
    """§1 fixes "one frozen 64-d collaborative item space". A 32-d basis is not a smaller
    version of it — every consumer indexes into 64 columns."""
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        kept = {k: npz[k] for k in npz.files}
    kept["E"] = kept["E"][:, :32]
    np.savez(path, **kept)

    report = _validate(clean)

    assert not report.ok
    assert any("64" in f.message for f in report.failures)


def test_the_review_text_embedding_is_read_under_the_name_the_corpus_ships(clean):
    """`review_text_emb.npz` ships `title_ids` too, and the review-text block is 64 of §4.3's
    columns — a block matched to the wrong rows is a title scored on another film's reviews."""
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
    """§4.3 takes "columns 0..63 of the 256-d SVD embedding". Truncation is not padding: an
    embedding with fewer columns than the contract slices cannot produce the block at all."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["emb"] = arrays["emb"][:, :16]
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "review-text" in _rules(report, "fail")


def test_a_review_text_embedding_with_no_coverage_flags_is_refused(clean):
    """The contract's own `preprocessing.missing_review_text` is "zeros when covered=False".
    The shipped bundle sets the flag False on 6,010 of 14,397 rows whose `emb` is float noise
    around 1e-16; without the array those rows read as review text, so the block is *present*
    for titles that have none — §5.3's thin badge stays off and §8 stage 2 never parks the
    acquisition job. A rule the app cannot apply is a bundle it cannot honestly import."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files if k != "covered"}
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "review-text" in _rules(report, "fail")
    assert any("covered" in f.message for f in report.failures)


def test_coverage_flags_that_do_not_line_up_with_the_ids_are_refused(clean):
    """One flag per row, or the flag read for a title is another title's. `features.py` indexes
    `covered` by the same position it indexes `emb`, so a short array is an IndexError at
    placement time and a long one silently shifts every flag after the gap."""
    path = _artifacts(clean) / "review_text_emb.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays["covered"] = arrays["covered"][:-1]
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert any("flags and the mapping disagree" in f.message for f in report.failures)


def test_a_contract_with_no_frozen_text_scale_is_refused(clean):
    """§4.3 freezes `text_scale` at export time "so placements stay comparable across runs". The
    corpus puts it inside `text_block`; read at the top level it was missing from every real
    bundle, and a scale that is defaulted rather than frozen moves every coordinate."""
    payload = _contract(clean)
    payload["text_block"].pop("text_scale")
    _write_contract(clean, payload)

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("text_scale" in f.message for f in report.failures)


def test_a_tower_whose_width_disagrees_with_its_contract_is_refused_at_import(clean):
    """The one silent failure in the set, and the reason the check exists.

    §8 stage 9 builds the vector from the contract; the tower consumes it. If the two disagree
    the placement does not raise — it runs a short vector into a wide first layer, or a wide one
    into a narrow one, and writes coordinates that are the right shape and the wrong numbers.
    Every downstream surface then works perfectly on a library placed at plausible nonsense.
    """
    payload = _contract(clean)
    before = payload["input_dim"]
    # Three columns the meta grammar can produce, appended to the block the contract declares
    # last, so the contract still parses and only the WIDTH moves.
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
    """§4.3: "the live model; the exporter **must** ship v2".

    The corpus writes a bare state_dict, which declares no version at all and is taken as the v2
    it is; a checkpoint that wraps its weights and *claims* v1 is the case §4.3 forbids.
    """
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
    """The corpus names the heads `head_e` / `head_b`; §5.1 needs both halves of the cold branch,
    and a checkpoint this app cannot reconstruct must fail here rather than at first placement."""
    fx.break_cold_tower_heads(clean)

    report = _validate(clean)

    assert not report.ok
    assert "cold-tower" in _rules(report, "fail")


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
    payload = _contract(clean)
    payload.pop("feature_names")
    _write_contract(clean, payload)

    report = _validate(clean)

    assert not report.ok
    assert "feature-contract" in _rules(report, "fail")
    assert any("feature_names" in f.message for f in report.failures)


def test_a_vocabulary_directory_without_the_corpus_term_files_is_refused(clean):
    """`dna_tag` and `dna_projected` both reference `dna_vocabulary(version)`. The app checked
    for `terms.tsv`, a name no bundle has ever carried — so the check failed on every real
    bundle and could not fail on a broken one. The corpus writes `vocab_<version>_all.tsv` plus
    one TSV per facet."""
    vocab = _artifacts(clean) / "dna_vocab" / "v1"
    assert not (vocab / "terms.tsv").exists(), "the corpus ships no terms.tsv"
    for path in vocab.glob("vocab_*.tsv"):
        path.unlink()

    report = _validate(clean)

    assert not report.ok
    assert "vocabulary" in _rules(report, "fail")
    assert any("vocab_v1_all.tsv" in f.message for f in report.failures)


# --- decision 162: the identity column is checked, not trusted ---------------------------------
#
# Range partitioning stops two minters colliding; it cannot see the corpus MERGING two titles,
# which changes what an id means without changing the id. The ids still ascend afterwards, so
# `backbone.py`'s ordering check cannot see it either.


def test_a_models_only_bundle_with_no_identity_vector_fails_validation(unidentified):
    """Owner decision, 2026-09-02, amending this row.

    A models-only bundle is the only kind decision 162 says will arrive again, and it carries no
    spine of its own — so with no identity vector there is nothing at all to check a corpus-side
    merge against, and the check would be skipped in exactly the case it exists for. That is a
    refusal.
    """
    (unidentified / "content.sqlite").unlink()
    report = _validate(unidentified)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any(IDENTITY_ARRAY in f.message for f in report.failures)


def test_a_seed_with_no_identity_vector_is_checked_against_its_own_spine(unidentified):
    """The other half of the same decision, and the half that makes the milestone's claim true.

    A seed carries `content.sqlite`. That spine names all 19,071 titles where the vector would
    name only the 14,397 with a model row, and it is the same fact from the same export — so it
    is a *better* identity source, not a weaker one. The absent vector is warned, not failed,
    because failing it would make every bundle the corpus has ever built unimportable and this
    milestone's headline claim false by its own coverage row.

    The warning is asserted, not just the absence of a failure: a silent pass here would be the
    skipped check the row forbids.
    """
    report = _validate(unidentified)

    assert report.ok, report.render()
    assert "identity" not in _rules(report, "fail")
    assert "identity" in _rules(report, "warn")
    assert any(IDENTITY_ARRAY in f.message for f in report.findings if f.severity == "warn")


def test_a_seed_whose_spine_does_not_carry_a_backbone_id_is_still_refused(unidentified):
    """The spine fallback is a check, not a bypass. A row of E naming a title the bundle's own
    content.sqlite does not have is attributed to nothing, and that fails whether or not an
    identity vector is present."""
    import sqlite3

    db = sqlite3.connect(unidentified / "content.sqlite")
    db.execute("DELETE FROM title WHERE id = 1")
    db.commit()
    db.close()
    report = _validate(unidentified)

    assert not report.ok
    assert "identity" in _rules(report, "fail")


def test_an_identity_that_disagrees_with_the_spine_names_the_title(clean):
    """A corpus-side re-identification: id 1's row still ascends, still has an E row, still
    resolves — and is now a different film. The identity column is the only thing that sees it.
    """
    fx.break_identity_mismatch(clean)          # title 1's imdb_id becomes tt0000001

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    named = [f for f in report.failures if "Heat" in f.message and "tt0113277" in f.message]
    assert named, report.render()
    assert named[0].detail["titles"][0]["title_id"] == 1


def test_a_title_with_no_imdb_id_is_identified_by_tmdb_and_kind(clean):
    """The check is per ROW, because 2,139 of the shipped bundle's 14,397 backbone titles have
    no imdb_id and none have neither id. Title 3 is the fixture's NULL-imdb case: it must be
    identified rather than skipped, so changing its tmdb_id is caught."""
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
    """Row-aligned to `title_ids` is the whole content of the check: a shorter or longer vector
    identifies the wrong rows and would silently pass a spot check on the first few."""
    path = _artifacts(clean) / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    arrays[IDENTITY_ARRAY] = arrays[IDENTITY_ARRAY][:-2]
    np.savez(path, **arrays)

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any("row-aligned" in f.message for f in report.failures)


def test_an_identity_naming_a_title_the_spine_does_not_carry_is_refused(clean):
    """The other half of a merge: the corpus dropped the row and kept the model. Nothing
    downstream notices — `row_of` resolves, and the coordinate belongs to a film this install
    has never heard of."""
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("DELETE FROM title WHERE id = 7")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any("does not carry" in f.message for f in report.failures)
