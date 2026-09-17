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


def _validate(root, *, reinventory: bool = True):
    """Validate the bundle each test BUILT, with its own inventory describing it.

    `fx.reinventory` first, because M4.14 wired `_verify_bundle_files` into `bundle.validate()`
    ahead of every other rule and the corpus writes BUNDLE.json LAST, over the tree it has just
    described. Every test below edits the tree after `make_bundle` returned, so without this the
    report would carry the edit's own rule AND a sha256 mismatch this file created -- two
    failures for one break, and no way to tell which rule caught what. The hash pass has its own
    tests, which address `_verify_bundle_files` directly through `_integrity` and therefore do
    not come through here. [M4.14 step B1]

    `reinventory=False` IS THE READ-ONLY DOOR, and it exists because the default is a write.
    `CORPUS_BUNDLE_DIR` names a bundle this repository does not own and must never modify, and
    re-stating its inventory would be wrong twice over: it rewrites 995 MB of somebody else's
    evidence in place, and it makes the one test that runs this validator over a REAL bundle
    unable to fail the integrity rule at all -- a corpus that shipped a wrong hash would be
    quietly corrected here instead of reported, which is the opposite of what that test is for.
    The flag is named rather than inferred from the path, because a helper that decides for
    itself whether its argument is safe to write to is a rule nobody can grep for.
    """
    if reinventory:
        fx.reinventory(root)
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
    # Read-only: see `_validate`. The corpus's own inventory is part of what is under test here.
    report = _validate(Path(os.environ["CORPUS_BUNDLE_DIR"]), reinventory=False)

    text = report.render()
    assert report.table_counts, "the pass ended before it counted a single table"
    # Every shipped table counted, not just the ones before the first surprise.
    assert len(report.table_counts) >= 25, sorted(report.table_counts)
    assert all(f.rule and f.message for f in report.findings)
    assert "bundle unknown" not in text, text.splitlines()[0]
    # `data-rules-the-stored-report-is-the-whole-report` says render() encodes to ASCII "for both
    # the fixture and a real bundle", and this is the real-bundle half. It cannot be asserted where
    # the fixture half is: `test_render_encodes_to_ascii` builds its report by hand, so it measures
    # the frame and the two glyphs it was written with, while every message this package
    # interpolates a bundle's own strings into - a title name, a table, a column - is outside it.
    # `_ASCII_FOLD` is a five-entry map rather than `encode("ascii", "replace")` on purpose, so a
    # codepoint it does not carry reaches whatever console prints the report, which CLAUDE.md
    # forbids. A corpus of 13,324 titles is the only place that surface is exercised at width.
    # [M4.14 cycle 3, m414-c3-rec-06]
    assert text.isascii(), [line for line in text.splitlines() if not line.isascii()][:3]


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
    """§10's re-import is "a planned admin event with a migration report". Two imports both
    stamped "unknown" cannot be told apart, and `unknown` is also the artifact directory they
    would share."""
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


def test_a_seed_that_lost_a_title_its_backbone_names_is_still_refused(clean):
    """The other half of a merge: the corpus dropped the row and kept the model. Nothing
    downstream notices — `row_of` resolves, and the coordinate belongs to a film this install
    has never heard of.

    A SEED, and that is now the whole content of this test: decision 248 splits the `absent`
    case by which spine is being checked against. Here the spine is the bundle's OWN
    `content.sqlite`, so a backbone row no title in the same export claims is the bundle
    disagreeing with itself. Against an INSTALLED spine the same shape is a note, and
    `test_an_identity_naming_a_title_the_spine_does_not_carry_is_a_counted_note` holds that half.
    """
    db = sqlite3.connect(clean / "content.sqlite")
    db.execute("DELETE FROM title WHERE id = 7")
    db.commit()
    db.close()

    report = _validate(clean)

    assert not report.ok
    assert "identity" in _rules(report, "fail")
    assert any("does not carry" in f.message for f in report.failures)


def test_a_series_runtime_that_is_a_total_is_named_in_the_report(clean):
    """Decision 192: `runtime_min` means two things on `series`, and §10's report says so.

    126 corpus series carry a season or series **total** in the column `home/shelves.py:73-75`
    reads as minutes per episode — 67 between 110 and 199, 59 at 200 or more (*Press Gang* 1290,
    *The Life & Times of Tim* 900). None is owned today, so nothing is visibly wrong yet, which
    is exactly why the report is where it has to be visible. Decision 192 stops there: no
    `episode_count`/`season_count` column, and nothing in the app dividing a total by an episode
    count — §4.1 puts that resolution corpus-side.

    The fixture's two series run 48 and 30 minutes, so the clean bundle prints no such line and
    one is written here to make the branch reachable.
    """
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


# --- M4.9's one join on the key §4.1 forbids: DELETED with the code ---------------------------
#
# `test_a_duplicated_imdb_id_fails_the_import_naming_the_rule` stood here. It guarded
# `load._resolve_ml_links`, the single place this app joined `ml_link` to `title` on `imdb_id` --
# the key §4.1 says "must never be the join key" -- and it refused a duplicated id rather than
# letting the join attach one title's genome vector to another. Decision 291 declines the genome
# slice, so the join and the exception it needed go together; deleted, not waived, because the
# requirement stopped existing rather than stopping being tested. What replaces it is a static
# refusal one file over: `test_load_mapping.py::test_no_loader_path_joins_titles_on_imdb_id`
# rejects the join coming back under any name, which is the half of the guarantee §4.1 still owes.


# --- M4.14 step B1: BUNDLE.json ships 42 hashes and nothing read them --------------------------
#
# `_verify_bundle_files` is called by `bundle.validate()` ahead of every other rule; these tests
# address it directly, which is also the point of the design -- it needs a tree and a report and
# nothing else, so it can run before a connection exists, let alone a row.


def test_a_latin_1_byte_in_an_id_bearing_ledger_is_a_report_line_not_an_exception(clean):
    """`validate._read_tsv`'s contract: "this is the only opener of a curated TSV in the importer".

    The docstring named `bundle.validate_id_partition` among its five call sites and that function
    opened both ledgers itself - `corrections.open(encoding="utf-8")` and the same shape for
    `adjudications_v1.tsv` - with no handler. `validate_id_partition` runs FIRST inside
    `validate()`, ahead of the corrections parse that does go through the handler, so one latin-1
    byte in either of the two hand-edited ledgers (6 corrections and 828 adjudications on
    v20260828, which is what makes them the files a stray byte reaches) left `POST /validate`
    holding a `UnicodeDecodeError` with no finding at all - the exact class step B2 says it
    closed, on the route the docstring claims to have closed it for.
    [M4.14 cycle 1, M414-REV-247-05]
    """
    for name in ("corrections_v1.tsv", "dna_vocab/v1/adjudications_v1.tsv"):
        path = clean / "artifacts" / name
        path.write_bytes(path.read_bytes() + b"\ndirector\t1\tGeorges M\xe9li\xe8s\te\tn\n")

        report = _validate(clean)

        assert not report.ok, f"{name}: {report.render()}"
        named = [f for f in report.failures if "cannot be read as UTF-8 TSV" in f.message]
        assert any(path.name in f.message for f in named), f"{name}: {report.render()}"


def test_two_vocabulary_directories_are_a_report_line_even_when_the_bundle_declares_one(clean):
    """Decision 163's two-version refusal is what `importer/vocab.py` was written to produce, and
    a declaring bundle disarmed both readers that could turn it into a report line.

    `bundle._vocabulary_version` returned the BUNDLE.json key without calling `version_of`, and
    `_read_bundle_identity` set `report.vocabulary_version` from the same key as the first act of
    `validate_artifacts` - so the guarded `if report.vocabulary_version is None` branch that
    catches `VocabularyError` was skipped, and `_validate_model_artifacts`' unguarded
    `ArtifactStore.open`, five lines above it, raised `VocabularyError` out of `validate()`, out
    of `validate_for_install`, out of both routes. `api/artifacts.py::_open` catches only
    `BundleOpenError`/`TarError`/`ZstdError`/`OSError` and `app.py` registers no generic handler,
    so the Data tab rendered an Internal Server Error with no report, no findings and no sentence
    naming the two vocabularies - for the one bundle shape the whole module exists to refuse.

    Decision 256's own refusal message is what creates the precondition: it tells the operator to
    "export the bundle with `vocabulary_version` in BUNDLE.json", which is precisely the
    declaration that disarmed both readers. [M4.14 cycle 1]
    """
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

    # And the seam the traceback actually came out of: `_validate_model_artifacts` builds an
    # `ArtifactStore` three hundred lines above the branch that catches this, so a caller
    # reaching `validate_artifacts` with the declaration already on the report - which is every
    # caller, since `_read_bundle_identity` sets it as its first act - met the raise there.
    direct = ImportReport(bundle_version="test-v1")
    direct.vocabulary_version = "v2"
    validator.validate_artifacts(clean / "artifacts", direct)
    assert not direct.ok, direct.render()
    vocabulary = [f for f in direct.failures if f.rule == "vocabulary"]
    assert vocabulary, direct.render()
    assert vocabulary[0].detail["versions"] == ["v1", "v2"]


def test_a_vocabulary_declaration_that_is_not_a_string_leaves_one_answer_on_the_screen(clean):
    """One bundle, one vocabulary, whichever reader is asked.

    `bundle._vocabulary_version` accepts the BUNDLE.json key only `if isinstance(declared, str)
    and declared` and otherwise falls to the directory; `_read_bundle_identity` accepted anything
    truthy and coerced it with `str()`. So an export writing `"vocabulary_version": 1` - a JSON
    number, a bool, a list - put "v1" in the report header and "no dna_vocab/1/" in the failure
    beneath it: two vocabularies for one bundle on one screen, which is the shape step B7 removed
    one field over for `bundle_version`, and a refusal sending the operator to look for a
    directory the corpus never named. [M4.14 cycle 1, m414-c1-declaration-read-twice]
    """
    for declaration in (1, True, ["v1"]):
        payload = _manifest(clean)
        payload["vocabulary_version"] = declaration
        _write_manifest(clean, payload)

        report = _validate(clean)

        assert report.ok, report.render()
        assert report.vocabulary_version == "v1", declaration
        assert "dna_vocab/1/" not in report.render()


def _retag_vocabulary(root: Path, version: str) -> None:
    """Move the fixture's `dna_vocab/v1/` tree to another version, files and all.

    The corpus writes the version into the directory name AND into every filename inside it
    (`dna_vocab/v1/vocab_v1_all.tsv`, `adjudications_v1.tsv`), so a tree at another version is
    not a directory rename - which is exactly why a reader that hard-codes one of those
    filenames goes quiet at any version but v1 instead of failing.
    """
    vocab = root / "artifacts" / "dna_vocab"
    (vocab / "v1").rename(vocab / version)
    for path in sorted((vocab / version).iterdir()):
        if "_v1" in path.name:
            path.rename(path.with_name(path.name.replace("_v1", f"_{version}")))


def test_a_declaration_that_contradicts_the_shipped_tree_is_refused_rather_than_preferred(clean):
    """One bundle, one vocabulary - the half of that rule a DISAGREEING declaration walked past.

    Both readers computed `vocab.version_of` and then discarded it whenever BUNDLE.json carried
    the key, so a models-only bundle retrained on v2 and declaring "v1" - the likeliest first
    value for that key, because decision 256's refusal coaches the operator to add it and the
    install's version is the number in front of them - validated ok. It then passed decision
    163's comparison on the declaration, staged its v2 tree, and left `ArtifactStore.open`
    deriving v2 from the staged directory while every `dna_tag` and `dna_projected` row stayed on
    v1: both DNA blocks empty for every title, which is the catastrophe decision 163's own
    refusal message describes. The seed kind was covered by the DNA-rows rule; this is the
    models-only kind, which decision 162 makes the only one that arrives again.
    [M4.14 cycle 3, M414-C3-VOCAB-01]
    """
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

    # The second reader, which decides the answer by the same rule and had the same hole. It is
    # asked directly because `validate()` stops at the first one, and a fix applied to one of two
    # derivations is how they came to disagree in the first place.
    direct = ImportReport(bundle_version="test-v1")
    validator.validate_artifacts(clean / "artifacts", direct)
    assert not direct.ok, direct.render()
    two = [f for f in direct.failures if f.rule == "vocabulary" and "two answers" in f.message]
    assert len(two) == 1, direct.render()
    assert two[0].detail == {"declared": "v1", "derived": "v2"}
    # `declared or derived` still decides the ANSWER, so nothing else in the report moves.
    assert direct.vocabulary_version == "v1"


def test_the_id_boundary_reads_the_adjudications_ledger_the_version_names(clean):
    """decision 247 guard 3 - "the version is the caller's and never a literal" - at the fifth
    id-bearing artifact's boundary check.

    `dna.load_adjudications` builds `adjudications_<version>.tsv` and `validate_id_partition`
    opened the literal `adjudications_v1.tsv` inside a directory named by the version, so at any
    vocabulary but v1 the two read different files: the check found nothing and passed in
    silence while the loader loaded the file that was there, and decision 162's boundary went
    unasserted on the one artifact of the five that names its own version.
    [M4.14 cycle 3, m414-c3-dim247-guard3]
    """
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
    """The `else` arm's sentence, over the bundle decision 256's remedy asks an operator to make.

    "No dna_vocab/ in this bundle and no DNA rows, so it names nothing" was written for a bundle
    that declares nothing, and the branch is reached by any bundle whose resolved version names
    no directory - including one that declared it. The report's header then carried the name and
    the line under it said the bundle named nothing: two answers about one field on one screen,
    two elements apart from decision 266's line quoting the same name.
    [M4.14 cycle 3, M414-C3-VOCAB-03]
    """
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
    """`validate_for_install`'s contract: every refusal an import can raise is reachable here.

    `compare_table_counts` had exactly one caller - `import_bundle`, inside the transaction, a
    hundred lines after the `rmtree` and `copytree` of the artifacts tree - and it needs nothing
    the validator does not already hold: `validate_content` fills `report.table_counts` from the
    same `content.sqlite` before a byte is copied, and the `loaded:<target>` keys the loaders add
    afterwards are deliberately not compared. So the Data tab said ok, the request answered 202,
    and the operator learned minutes later from a polled job row that the bundle was refused by a
    rule computable before the first byte moved - a 205 MB rmtree and copy on the real bundle,
    spent on a bundle validate could have refused.

    BUNDLE.json is the one file exempt from its own inventory, so an export that wrote its
    manifest from the wrong side of a filter keeps all 42 hashes correct and still lies about
    itself - which is what makes this a separate check from B1's rather than a second reading of
    it. The in-transaction call stays where it is as the backstop.
    [M4.14 cycle 1, m414-c1-dim-refusals-03]
    """
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
    """The `clean` bundle turned into a models-only re-import: its spine read off, then removed.

    The bundle these tests assert against is one the test CONSTRUCTS. No real model bundle
    exists -- `mdc export-bundle` has not been re-run since it learned to write `title_identity`
    -- and decision 162 makes this the only kind that will ever arrive again, so the shape has to
    be built here or asserted nowhere.
    """
    spine = validator._spine_identities(root / "content.sqlite", ImportReport())
    (root / "content.sqlite").unlink()
    fx.reinventory(root)          # a spine removed from the tree is removed from its inventory
    return spine


def test_every_listed_file_is_hashed_before_a_row_is_written(clean):
    """§10 puts validation before the flip, and until M4.14 it verified no bytes at all.

    The file changed here keeps its SIZE, which is the case only a hash can see and the case
    finding 2.4 reproduced against v20260828: a zeroed `equating_map.json` validated clean, was
    staged, and became the active bundle. The first assertion after the edit is the finding
    itself -- every other rule still passes.
    """
    listed = _manifest(clean)["files"]
    assert "content.sqlite" in listed and len(listed) >= 30, sorted(listed)[:5]

    report = _integrity(clean)
    assert report.ok, report.render()
    assert any(f.rule == "bundle-integrity" and "sha256 verified" in f.message
               for f in report.findings)

    target = clean / "artifacts" / "equating_map.json"
    target.write_bytes(b"0" * len(target.read_bytes()))
    assert target.stat().st_size == listed["artifacts/equating_map.json"]["bytes"]
    # Called directly rather than through `_validate`, which re-inventories and would erase the
    # very edit this test made. The whole-report pass now carries the hash pass ahead of every
    # other rule, so a same-size edit is one failure and exactly one.
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
    """Both mean the tree on disk is not the tree the corpus inventoried, which is the shape a
    half-extracted archive and a hand-edited bundle both take."""
    (clean / "artifacts" / "audit.json").unlink()
    (clean / "artifacts" / "curator-notes.txt").write_text("left behind\n", encoding="utf-8")

    report = _integrity(clean)

    assert not report.ok
    assert {f.rule for f in report.failures} == {"bundle-integrity"}
    assert any("artifacts/audit.json" in f.message for f in report.failures), report.render()
    assert any("artifacts/curator-notes.txt" in f.message for f in report.failures)


def test_bundle_json_is_the_only_file_exempt_from_the_unlisted_rule(clean):
    """It cannot list its own hash, so present-but-unlisted has exactly one legitimate member --
    verified against v20260828 and against this fixture. The exemption is the PATH and not the
    name: a second BUNDLE.json one directory down is still a file nobody inventoried."""
    listed = _manifest(clean)["files"]
    on_disk = {p.relative_to(clean).as_posix() for p in clean.rglob("*") if p.is_file()}
    assert on_disk - set(listed) == {"BUNDLE.json"}
    assert _integrity(clean).ok

    (clean / "artifacts" / "BUNDLE.json").write_text("{}", encoding="utf-8")

    report = _integrity(clean)
    assert not report.ok
    assert any("artifacts/BUNDLE.json" in f.message for f in report.failures), report.render()


def test_a_validations_entry_that_is_not_ok_is_a_failure(clean):
    """68 rows on v20260828 and every one of them `ok: true` -- a verdict the corpus computes,
    writes into the bundle, and nothing here opened. A failed row is the exporter saying, inside
    the bundle, that the bundle is wrong."""
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
    """A truncated `content.sqlite` made `validate` itself a 500 and a truncated `reviews.sqlite`
    died as an uncaught `sqlite3.DatabaseError` inside the import transaction. Both are report
    lines now, from two directions: the integrity pass names them, and `validate_content`'s first
    query -- which runs above every `_guard` and so had no handler of its own -- reports."""
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
    """The readable/unreadable boundary, which is the one branch this loop did not have.

    `_verify_bundle_files` stat'd and hashed every listed file with no handler at all, and
    nothing between it and either route has one: `validate`, `validate_for_install`,
    `api/artifacts.validate_bundle` and `api/artifacts.import_bundle` all call it bare, `_open`
    guards `Bundle.open` alone, and `app.py`'s last-resort `OSError` handler re-raises anything
    outside five network errnos on purpose ("PermissionError on /data/artifacts and a full disk
    stay the 500 they should be"). Measured through the real app with a denied ACL on one listed
    file: `POST /api/admin/bundle/validate` and `POST /api/admin/bundle/import` both answered 500,
    and the Data tab renders `err.message` - "Internal Server Error", the one sentence nobody can
    act on - for a bundle that is intact.

    The error is INJECTED rather than provoked with a real EACCES, for the reason
    `test_a_cleanup_that_could_not_remove_the_tree_says_so` gives one file over: the states that
    produce it are the reference box's (uid 1000 against a root-owned `/data/import` bind mount)
    and are not portable to this suite's temp directory. What is asserted is the branch, the
    report line and that the pass carries on to the rest of the inventory.
    [M4.14 cycle 2, m414-c2-refusals-01, step B1]
    """
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
    # The rest of the inventory is still checked: one unreadable file is one report line, not an
    # abandoned pass, so the operator reads everything that is wrong with the bundle at once.
    assert not [f for f in report.failures if "are not in the bundle" in f.message], report.render()
    assert not [f for f in report.failures if "do not match BUNDLE.json" in f.message], report.render()


def test_a_manifest_value_this_app_cannot_read_is_a_report_line_not_an_exception(clean):
    """The leaf of the two maps this milestone taught the importer to read.

    `_verify_bundle_files` coerced `files[<name>]["bytes"]` with a bare `int()` and
    `compare_table_counts` did the same to every `tables` count, both on values read straight out
    of BUNDLE.json. Neither `validate()` nor `validate_for_install` has an `except`, neither
    `api/artifacts.validate_bundle` nor `import_bundle` has one, and `app.py` registers handlers
    for nine classes of which ValueError and TypeError are none - so a manifest this app cannot
    parse reached the operator as "Internal Server Error", the one sentence nobody can act on,
    out of both admin routes. Driven through `validate()` because that is the function both
    routes call and the one whose report the Data tab renders.

    The `tables` half is the worse of the two: `compare_table_counts` is called a SECOND time
    inside the import transaction, where a ValueError is none of `import_bundle`'s named
    refusals, so `except BaseException` re-raises and the job closes "the import did not run to a
    report" with an exception type as its only evidence. [M4.14 cycle 4, M414-C4-REF-01]
    """
    original = _manifest(clean)

    # Asked in two passes and not one, because `validate()` stops at the integrity boundary the
    # way it stops at decision 162's: a bundle whose inventory cannot be read is not one whose
    # table counts are worth thirty more lines. Each pass is one coercion.
    manifest = json.loads(json.dumps(original))
    first = sorted(manifest["files"])[0]
    manifest["files"][first]["bytes"] = "abc"
    _write_manifest(clean, manifest)

    report = bundle_import.validate(bundle_import.Bundle.open(clean))

    assert not report.ok, report.render()
    named = [f.message for f in report.failures if f.rule == "bundle-integrity"]
    assert any(first in m and "'abc'" in m for m in named), report.render()
    # Enumerated rather than abandoned at the first surprise, which is the whole argument of the
    # `data-rules-validation-reports-rather-than-raises` row: the other 32 files are still
    # hashed, so the operator reads everything wrong with the bundle at once.
    assert not any("do not match BUNDLE.json" in m for m in named), report.render()
    assert not any("are not in the bundle" in m for m in named), report.render()

    manifest = json.loads(json.dumps(original))
    manifest["tables"]["title"] = "many"
    _write_manifest(clean, manifest)

    counts = bundle_import.validate(bundle_import.Bundle.open(clean))

    assert not counts.ok, counts.render()
    unreadable = [f.message for f in counts.failures if f.rule == "bundle-integrity"]
    assert any("'many'" in m and "title" in m for m in unreadable), counts.render()
    # The readable counts are still compared: one count this app cannot parse is one report line.
    assert not any("do not hold the number of rows" in m for m in unreadable), counts.render()


def test_an_inventory_entry_that_declares_no_digest_is_not_counted_as_verified(clean):
    """The affirming sentence was unconditional and the check behind it was not.

    `wrong_hash = bool(declared_hash) and ...` made the digest optional: an entry that was not an
    object, or that carried `sha256` absent, null or empty, compared nothing, fell into the
    success branch, incremented `verified` and added its bytes to `checked_bytes` - and the note
    at the foot of the pass then told the operator that every listed file had been "size and
    sha256 verified against BUNDLE.json before anything was read". Nothing counted declarations,
    so the report could not tell "33 hashes matched" from "33 hashes were never declared".

    The case with teeth is finding 2.4's own example: a zeroed `artifacts/equating_map.json`
    padded back to its own length - the case only a hash can see - under a size-only `{name:
    bytes}` inventory, which is what an exporter that could not hash would write. Measured before
    the fix: `ok=True`, the affirming note, and the bundle staged and flipped active. Decision 162
    plus `artifact_bundle_one_seed` then make that seed the only one that household will ever
    get. [M4.14 cycle 4, M414-C4-REF-02]
    """
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
    # And the sentence that would have been false is not written at all: a report that says
    # "0 file(s) ... verified" beside 33 failures is two contradictory statements on one screen.
    assert not any("sha256 verified" in f.message for f in report.findings), report.render()

    # Each degenerate shape, because they are four spellings of one declaration that is not there
    # and the size-only one above is only the first.
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
    """The third unguarded coercion of the same shape, and the only one that predates M4.14.

    `sum(int(b.get("size", 0)) for b in declared)` over `feature_contract.json`'s
    `content_blocks` raised AttributeError on a block that is not an object and ValueError or
    TypeError on a `size` that is not a number - out of `validate_artifacts`, which has no
    handler between it and either route. A warn rather than a failure because the failure is one
    reader further down: §8 stage 9's own `FeatureContract` parser runs twenty lines below this
    and its refusal is what refuses the bundle.

    And the non-dict entry proved the sum was only the first frame: `FeatureContract` type-checks
    `size` only once `spec.get("name")` has worked, so that shape raised AttributeError out of
    `contract.py` instead - past the one `except ValueError` arm written to keep this file's
    defects out of the operator's face. Both halves are asserted here, because a fix that moves a
    traceback one frame is not a fix. [M4.14 cycle 4, M414-C4-REF-01]
    """
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
    # And the parser's own reading of the same three entries is a failure with a sentence, not a
    # traceback out of a route with no handler.
    parsed = [f for f in report.failures if f.rule == "feature-contract"]
    assert parsed, report.render()
    assert any("cannot be parsed" in f.message or "usable `size`" in f.message for f in parsed), (
        report.render()
    )


# --- M4.14 step B3: referential integrity is a report line, not a 500 --------------------------
#
# Nine synthetic orphan variants passed `validate()` and `validate_for_install()` and then raised
# inside the transaction, where `app.py` turns any `PostgresError` into 500 {"detail": "database
# error"} -- the staged tree left behind and no row named. §10 promises a report.


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
    """`award.award` becomes `award.body`, which is `text NOT NULL` (0003) with no rule 6
    coalesce standing behind it -- so a NULL here is a `NotNullViolationError` mid-transaction."""
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
    """The app's key is not always the corpus's, and `title_language` is the case where the
    difference is INVISIBLE to SQL: `role` is `_primary_role(is_primary)`, which collapses 0 and
    NULL onto ''. Two rows that differ in the file are one row under the app's key, and the COPY
    rolls the whole seed back -- which is what 17,342 duplicate groups did before 0015.

    The corpus's own primary key is dropped first, because with it in place the export cannot
    produce this shape; a re-key upstream is exactly how it would arrive (0018 re-keyed
    `title_video` for the same reason).
    """
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
    """The corpus's own `nullable_pk_note` is the contract: "the Postgres importer coalesces
    TEXT-affinity components to ''". A component this app coalesces whose affinity is not TEXT
    would write '' into a column that cannot hold one.

    v20260828 declares four components and two of them are INTEGER (`ml_genome_score.movie_id`
    and `.tag_id`) -- neither coalesced here -- which is why the rule is about the promise and
    not about the affinity alone. Asserted in both directions for that reason.
    """
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
    """Decision 291's other half: a gate standing over a table no COPY can reach.

    Both integrity loops key off the BUNDLE's schema -- `if child not in schema: continue` --
    and not off `load.MAPPINGS`, which the duplicate loop three lines below them does. So moving
    a table into `SKIPPED_TABLES` disarms nothing: the MovieLens slice left one foreign key and
    five NOT NULL expectations standing after decision 291 declined it, on tables the bundle
    still ships because the corpus is not asked to re-cut it. Every real import went on checking
    them, and an export whose cut dropped a tag one `ml_genome_score` row still names would have
    refused the household's one content seed (decision 162) over a table this build has decided
    it does not want -- with an `integrity-null` line saying "the column this app loads them
    into is NOT NULL" about a column nothing loads at all. The plan's own clause for decision
    291 is that the importer "accept a bundle with or without them".

    The predicate is COPY's reach and not `MAPPINGS` alone: `title_meta` and the DNA tables are
    loaded by bespoke paths and are gated here for exactly the reason the mapped ones are.
    Written as a rule rather than as a list of two names, so the next declined table cannot leave
    a gate behind it either. [decision 291; M4.16 cycle 1, M416-291-03]
    """
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


# --- M4.14 step B8: rule 7 is a rule about the bundle, and it read half of one ------------------


def test_a_denied_table_in_the_reviews_database_is_found_too(clean):
    """§8 stage 5 re-extracts DNA from these bodies, so a stale `review_bak` here is precisely
    the class rule 7 exists to catch -- and `reviews.sqlite` was first opened inside the load."""
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
    """§4.1 anchors `%_good` at the END of the name. Matched as a substring, a future
    `title_goodness` -- not a stale copy of anything -- would refuse the whole first boot."""
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


# --- M4.14 step B2: the artifact readers report rather than raise ------------------------------


@pytest.mark.parametrize("relative", [
    "corrections_v1.tsv",
    "dna_vocab/v1/adjudications_v1.tsv",
    "dna_vocab/v1/alias_map_v1.tsv",
])
def test_a_latin_1_byte_in_a_curated_ledger_is_a_report_line_not_an_exception(clean, relative):
    """These are the hand-edited ledgers -- 6 corrections and 828 adjudications on v20260828 --
    and they were opened with `encoding="utf-8"` and no handler out of a route whose docstring
    says "Writes nothing" and which §10 promises produces a report."""
    path = clean / "artifacts" / relative
    path.write_bytes(path.read_bytes() + b"caf\xe9\t1\n")

    report = ImportReport()
    rows = validator._read_tsv(path, report, "corrections")

    assert rows is None, "an unreadable ledger is not an empty one (decision 247)"
    assert not report.ok
    assert path.name in report.failures[0].message
    assert report.failures[0].detail["file"] == path.name


def test_a_curated_ledger_whose_header_is_not_the_shipped_one_is_named(clean):
    """The inverse of the same rule: a column this app asserts and the file does not have reads
    as nothing at all, which is how `parse_corrections` came to be written against a header no
    ledger carries."""
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
    """`validate_id_partition` swallowed a parse error into `payload = []` and
    `dna.load_seed_list` re-raised it inside the transaction through a bare `json.loads` and
    `int(item["title_id"])`. Checked to the shape the loader may then rely on."""
    (clean / "artifacts" / "seed_list.json").write_text(payload, encoding="utf-8")

    report = _validate(clean)

    assert not report.ok
    assert "seed-list" in _rules(report, "fail"), report.render()


def test_an_onboarding_title_the_bundles_own_spine_lacks_is_refused(clean):
    """`seed_list.title_id` is a NOT NULL foreign key to `title(id)` (0003), so one entry naming
    a title the spine does not carry aborts the whole import on a constraint violation that names
    a constraint rather than a file."""
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
    """`validate_for_install`'s contract is that "every refusal an import can raise is reachable
    here, otherwise the operator reads ok and is refused after they have committed" - and this
    function read `title_id` and nothing else of the entry.

    `dna._decade` turns `year` into `seed_list.decade`, and a `year` of `NaN` - the spelling an
    unresolved year arrives in from a frame, since `json.dumps` writes the bare literal and
    `json.loads` accepts it - raised ValueError inside the import transaction, past every arm
    that could have named it, after the 1.04 GB copy. The loader now keeps a NULL decade for it
    as its own docstring always promised; this is the half that says so BEFORE the operator
    commits. A note and not a failure, on the loader's trade: a hole in section 4.3's
    stratification is a smaller loss than a refused seed.
    [M4.14 cycle 4, m414-c4-dim247-03]
    """
    path = clean / "artifacts" / "seed_list.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["year"] = "__nan__"
    entries[1]["year"] = "__inf__"
    # Substituted rather than dumped, because the bare `NaN` and `Infinity` literals are the whole
    # point: `json.dumps` emits them and `json.loads` reads them back, which is the round trip an
    # unresolved year makes between the corpus's frame and this file.
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


# --- M4.14 step B6, decision 251: three artifacts the rebuild cannot run without ----------------


@pytest.mark.parametrize("kind", ["seed", "model"])
@pytest.mark.parametrize("filename,rule", [
    ("feature_contract.json", "feature-contract"),
    ("cold_tower.pt", "cold-tower"),
    ("backbone.npz", "backbone"),
])
def test_an_absent_model_artifact_is_a_failure_on_both_bundle_kinds(clean, kind, filename, rule):
    """decision 251. §10 step 4 recomputes the rebuild set on EVERY import, and the rebuild
    reaches all three without a guard a real import can miss: `FeatureContract.from_store` runs
    unconditionally, `load_tower` sits inside a branch that is never empty on a real import, and
    the §5.2 refit composes its basis through `backbone.load_for(store)` -- where an absent array
    fits every board from zero embeddings, silently.

    Both kinds, because the models-only bundle is the only one that will arrive again and it is
    the one whose whole payload these three files are.
    """
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


# --- M4.14 step B4: a seed with DNA rows and no vocabulary to name them from --------------------


def test_dna_rows_with_no_vocabulary_directory_are_refused_and_an_empty_one_is_not(clean):
    """Reproduced before the fix as `validate.ok=True`, `/validate` 200, `/import` 500 on
    `0004_dna.sql:76`'s version foreign key: the directory's absence was warned at one line and
    the DNA rows were counted at another, and the two facts were never combined.

    The warning stays for a bundle with no DNA rows -- §3.1 makes an empty naming layer legal,
    and under decision 162 a models-only bundle is the kind least likely to ship the tree.
    """
    shutil.rmtree(clean / "artifacts" / "dna_vocab")

    report = _validate(clean)

    assert not report.ok
    vocabulary = next(f for f in report.failures if f.rule == "vocabulary")
    assert "dna_tag" in vocabulary.message and "dna_projected" in vocabulary.message, (
        vocabulary.message
    )
    # Named apart rather than summed to 17: §4.1 rule 1 is a claim about rows, and one number
    # covering both tiers loses the discriminator a UNION would -- which is why
    # `test_landmine_guards.py` rejects the addition that stood here. [M4.14]
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


# --- M4.14 decision 248: the corpus's catalogue is not frozen at this install's seed ------------


def test_an_identity_naming_a_title_the_spine_does_not_carry_is_a_counted_note(clean):
    """decision 248, and this test was `..._is_refused` until M4.14.

    The corpus keeps its own catalogue -- `sqlite_sequence` stood at title 21442 against the
    19,071 the bundle exported -- so a retrained `backbone.npz` covers whatever the corpus had at
    export time, and the first model bundle this app ever meets is refused by the old rule. Those
    rows are inert: a `Backbone` lookup is by id and never reaches them. The count is kept
    because it is the merge signal an operator reads.

    Asserted against a model bundle this test CONSTRUCTS: no real one exists, because
    `mdc export-bundle` has not been re-run since it learned to write `title_identity`.
    """
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
    """The other half of decision 248, and the half the check exists for: an id whose MEANING
    changed. Same constructed model bundle as the test above -- an uncovered id is a note there
    and a re-identified one is a refusal here, on one report.
    """
    spine = _models_only(clean)
    kind, _imdb_id, tmdb_id, name = spine[1]
    spine[1] = (kind, "tt9999999", tmdb_id, name)

    report = bundle_import.validate(bundle_import.Bundle.open(clean), spine=spine)

    assert not report.ok
    named = [f for f in report.failures if f.rule == "identity"]
    assert named, report.render()
    assert "Heat" in named[0].message and "tt0113277" in named[0].message


def test_a_backbone_whose_identity_token_is_neither_form_is_still_a_failure(clean):
    """`unparsed` stays a failure beside `absent`'s note: a token this app cannot read is not a
    catalogue that moved on, it is an exporter writing something nobody agreed on."""
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
