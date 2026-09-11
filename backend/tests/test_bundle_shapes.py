"""The fixture is held to the shape of a bundle the corpus actually produced.

`tests/fixtures/make_bundle.py` stands in for the corpus export, and until now nothing checked
that it stood in for anything real. It reproduced every measured landmine faithfully -- both DNA
tiers overlapping, the frozen `rating_source` ids, duplicate `tmdb_id`s, NULL alias PK
components, CJK and emoji -- and invented every *structure* around them, so the whole import
layer was verified against this repo's reading of §10 rather than against the artifact.

`tests/fixtures/real_bundle_shapes.json` is the ground truth: shapes only, extracted from a real
bundle by `ops/bundle_shapes.py`, carrying no values (a feature column is recorded as
`p:<s>:<s>`, never `p:director:Adam Arkin`). These tests hold the fixture to it, and hold a real
bundle to it too when `CORPUS_BUNDLE_DIR` is set -- so a corpus-side format change fails here
rather than surfacing as a mystery at import time.

Milestone M4.5, row `data-rules-fixture-matches-the-shipped-artifact`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from spielplan.importer import bundle as bundle_import
from tests.fixtures import make_bundle

ROOT = Path(__file__).resolve().parents[2]
SHAPES = Path(__file__).resolve().parent / "fixtures" / "real_bundle_shapes.json"


def _load_shapes_module():
    spec = importlib.util.spec_from_file_location("bundle_shapes", ROOT / "ops" / "bundle_shapes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bundle_shapes", module)
    spec.loader.exec_module(module)
    return module


shapes_mod = _load_shapes_module()


@pytest.fixture(scope="module")
def shipped() -> dict:
    """The shape of a bundle the corpus actually built."""
    if not SHAPES.is_file():                                    # pragma: no cover - see docstring
        pytest.fail(
            f"{SHAPES} is missing. It is the ground truth for every assertion in this file; "
            "regenerate it with ops/bundle_shapes.py against a real bundle."
        )
    return json.loads(SHAPES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bundle_root(tmp_path_factory) -> Path:
    """The bundle itself, built once. `built` below reduces it to shapes; the three tests at
    the foot of this file read its rows, which is where the corpus's awkward shapes live -- a
    shape manifest carries no values by design, so it cannot see a facet naming or a duplicate
    department."""
    root = tmp_path_factory.mktemp("fixture-bundle")
    make_bundle.make_bundle(root)
    return root


@pytest.fixture(scope="module")
def built(bundle_root) -> dict:
    """The shape of the bundle `make_bundle` produces, read the same way."""
    return shapes_mod.extract(bundle_root)


# --- the feature contract: §4.3's "exhaustive definition of the tower's input" ----------------


def test_the_fixture_contract_declares_the_keys_the_shipped_contract_declares(shipped, built):
    """§4.3 calls `feature_contract.json` the exhaustive definition of the tower's input and
    §8 stage 9 builds vectors "from this file and nothing else". A fixture whose contract has a
    different top level is not a scale model of that file; it is a different file."""
    ours = set(built["json"]["artifacts/feature_contract.json"]["keys"])
    theirs = set(shipped["json"]["artifacts/feature_contract.json"]["keys"])
    # Equality, not `theirs <= ours`. A subset check passes the moment the fixture *adds* the
    # shipped keys while keeping its own `blocks`/`block_order` — and `contract.py` reads
    # `blocks`, so the real artifact would still be rejected with the test green. That is the
    # M4 pattern (a test that cannot fail) reintroduced in the test written to prevent it.
    assert ours == theirs, (
        f"fixture contract keys {sorted(ours)} != shipped {sorted(theirs)}. "
        "Extra fixture keys are as bad as missing ones: they are what the parser reads."
    )


def test_the_fixture_lists_feature_names_the_way_the_shipped_contract_does(shipped, built):
    """The shipped contract carries ONE flat list of 6,435 column names. The fixture carried a
    dict keyed by block. Same word, different data structure, and the difference is what let the
    credit block's key grammar go unnoticed."""
    theirs = shipped["json"]["artifacts/feature_contract.json"]
    ours = built["json"]["artifacts/feature_contract.json"]
    assert "feature_names.item_patterns" in theirs, "manifest is stale; regenerate it"
    assert "feature_names.item_patterns" in ours, (
        "the fixture's `feature_names` is not a flat list of column names, but the shipped "
        "contract's is -- so `feature_names` means two different things in the two files."
    )


def test_the_fixture_uses_the_shipped_column_grammar_in_every_block(shipped, built):
    """Every block, not just credit -- and checking only credit is how the scale of the defect
    was missed the first time.

    The shipped contract prefixes every column with its block tag: `kw:`, `dna:`, `g:`, `p:`,
    `genre:`, `country:`, `lang:`, `decade:`, `runtime:`, `award:`, `kind:`. The builders in
    `placement/features.py` emit bare keys for most of them and `person_id::text` for credit,
    so all nine content blocks miss, not one. The fixture declared `<block>:<n>` throughout,
    which the parser reduces to the bare key the builder happens to produce -- so the fixture
    agreed with the implementation about a grammar neither shares with the corpus.

    Direction is `ours <= theirs`: a scale model may use fewer grammars, never a grammar the
    shipped contract does not contain.
    """
    theirs = set(shipped["json"]["artifacts/feature_contract.json"]["feature_names.item_patterns"])
    ours = set(built["json"]["artifacts/feature_contract.json"].get("feature_names.item_patterns", []))
    assert "p:<s>:<s>" in theirs, f"manifest is stale; shipped patterns are {sorted(theirs)}"
    # Without this the assertion below passes vacuously: the fixture's `feature_names` is a
    # dict rather than a flat list, so the extractor records no patterns at all and the empty
    # set is a subset of everything. An emptiness that satisfies the check is the same failure
    # this file exists to catch, one level further in.
    assert ours, (
        "the fixture's contract yields no column patterns at all -- `feature_names` is not a "
        "flat list of names, so this comparison would pass without comparing anything."
    )
    assert ours <= theirs, (
        f"the fixture declares column grammars the shipped contract does not have: "
        f"{sorted(ours - theirs)}. Shipped: {sorted(theirs)}. A fixture that names columns the "
        "way the builder keys them cannot fail when the builder is wrong."
    )


# decision 162 requires the model bundle to carry an identity column row-aligned to
# `title_ids`, and the exporter does not write one yet — so the fixture ships it and the gap is
# DECLARED here, the same way the axis TSVs above are. `validate.py` refuses a bundle without
# it, which is the whole point: a check that is skipped on every bundle in existence is not a
# check. The test below also asserts the exception is still needed, so it cannot rot.
DECISION_162_NOT_YET_SHIPPED = {"artifacts/backbone.npz": {"title_identity"}}


def test_the_fixture_npz_arrays_are_named_the_way_the_corpus_names_them(shipped, built):
    """`backbone.npz` ships `title_ids`; `backbone.py:84` requires `title_id`, and so do
    `reconcile.py:110` and the validator. `review_text_emb.npz` ships `title_ids` and
    `features.py:166` reads `title_id`. One character, and §8 stage 9 cannot find a coordinate.

    Only files present in both are compared: a scale model may omit an artifact, but an
    artifact it does ship must be named the way the corpus names it.
    """
    theirs = shipped["npz"]
    ours = built["npz"]
    shared = sorted(set(ours) & set(theirs))
    assert shared, "the fixture ships no npz the corpus ships -- the comparison is vacuous"
    already = {f: set(DECISION_162_NOT_YET_SHIPPED.get(f, ())) & set(theirs[f]) for f in shared}
    assert not any(already.values()), (
        f"the corpus now ships {already} — delete the DECISION_162_NOT_YET_SHIPPED entry and "
        "compare those arrays like everything else."
    )
    mismatched = {
        f: {"fixture": ours[f], "shipped": theirs[f]}
        for f in shared
        if set(ours[f]) - set(DECISION_162_NOT_YET_SHIPPED.get(f, ())) != set(theirs[f])
    }
    assert not mismatched, f"npz array names differ from the shipped bundle: {mismatched}"


# §6.4 makes the per-facet axis TSVs "a shipped, authored artifact … shipped in `dna_vocab/v1/`",
# and the corpus does not ship them — proposal 140 asks for them to be added to §4.3's and §10's
# manifests, and the exporter has not done it. The fixture therefore carries them under the name
# the app reads, and the gap is DECLARED here rather than hidden by a looser assertion. The test
# below also asserts the exception is still needed, so it cannot rot into a permanent excuse.
SPEC_REQUIRED_NOT_YET_SHIPPED = ("artifacts/dna_vocab/v1/axes/",)


def test_the_fixture_ships_the_dna_vocabulary_files_the_corpus_ships(shipped, built):
    """`importer/dna.py` reads `terms.tsv`, `aliases.tsv` and `adjudications.tsv`; the corpus
    ships `vocab_<facet>_v1.tsv`, `alias_map_v1.tsv` and a per-*title* `adjudications_v1.tsv`.
    The whole naming layer is written against files that do not exist, and `dna_tag` /
    `dna_projected` both FK to `dna_vocabulary(version)`, so no DNA row can load at all."""
    theirs = {f for f in shipped["files"] if f.startswith("artifacts/dna_vocab/")}
    ours = {f for f in built["files"] if f.startswith("artifacts/dna_vocab/")}
    assert theirs, "manifest is stale; the shipped bundle has no dna_vocab directory"

    declared = {f for f in ours if f.startswith(SPEC_REQUIRED_NOT_YET_SHIPPED)}
    # The exception has to still be an exception. If the corpus starts shipping axis TSVs, this
    # fails and the allowlist goes away rather than quietly covering a real drift.
    assert not (declared & theirs), (
        f"the corpus now ships {sorted(declared & theirs)} — delete the "
        "SPEC_REQUIRED_NOT_YET_SHIPPED entry and compare them like everything else."
    )
    assert (ours - declared) <= theirs, (
        f"the fixture invents vocabulary files the corpus does not ship: "
        f"{sorted((ours - declared) - theirs)}. Shipped: {sorted(theirs)}."
    )


# --- the checkpoint: the only place the architecture is written down ---------------------------


def test_the_fixture_checkpoint_names_its_tensors_the_way_the_corpus_does(shipped, built):
    """`cold_tower.pt` is a bare `torch.save(model.state_dict())`, so the tensor names are the
    whole of §4.3's "the exporter must ship v2" that a bundle actually carries.
    `placement/tower.py` pins them as `TRUNK_FIRST` / `EMBED_HEAD` / `PRIOR_HEAD` — three
    constants that were checked against a comment, because the manifest had no `.pt` branch and
    recorded the checkpoint as a bare filename.

    Shapes are compared by rank, not by value: the fixture's trunk is 128-wide and the shipped
    one 768-wide, and pinning a hidden width would make the manifest churn on every retrain.
    The rank is what the loader reads — `head_e.weight` must be 2-d for `nn.Linear` to be
    rebuilt from it.
    """
    from spielplan.placement import tower

    theirs = shipped["pt"]["artifacts/cold_tower.pt"]
    ours = built["pt"]["artifacts/cold_tower.pt"]
    for name in (tower.TRUNK_FIRST, f"{tower.EMBED_HEAD}.weight", f"{tower.PRIOR_HEAD}.weight"):
        assert name in theirs, (
            f"tower.py reconstructs the module from {name!r} and no shipped checkpoint has it; "
            f"the corpus writes {sorted(theirs)}"
        )
    assert set(ours) == set(theirs), (
        f"fixture checkpoint tensors {sorted(ours)} != shipped {sorted(theirs)}"
    )
    assert {k: len(v) for k, v in ours.items()} == {k: len(v) for k, v in theirs.items()}


def test_the_shipped_checkpoint_embeds_at_the_dimension_every_consumer_assumes(shipped):
    """§5.1's e(t) is 64-d and so is §5.2's user vector, §4.2's `user_vector.vec` and
    `title_placement.dim CHECK (dim = 64)`. That number is a constant in this repo and a trained
    weight shape in the corpus; nothing compared the two."""
    from spielplan.placement.tower import EMBED_DIM

    head = shipped["pt"]["artifacts/cold_tower.pt"]["head_e.weight"]
    assert head[0] == EMBED_DIM, (
        f"the shipped tower emits {head[0]}-d embeddings and this app is built for {EMBED_DIM}"
    )


# --- cold_eval.json: the one reference value the bundle ships ----------------------------------


def test_the_shipped_cold_eval_carries_the_arms_the_app_reads(shipped, built):
    """§14 risk 1's mitigation is "expectations instrumented, not assumed", and until M4.13 this
    file was in no list and read nowhere: `user_vector.cv_rho` is a held-out Spearman per
    (user, kind) with no reference value anywhere in the app, while the corpus had measured the
    cold path (0.35225) against the warm ceiling (0.39193) and shipped both.

    `models/artifacts.ColdEval` now reads it, so the keys it reaches for are pinned HERE rather
    than against a hand-written fixture - that is this file's whole argument, and it applies with
    more force to a file the fixture does not write at all. Note what is therefore NOT asserted:
    the fixture ships no `cold_eval.json`, so there is no fixture-versus-shipped comparison to
    make. The file is optional in `BUNDLE_FILES` for exactly that reason, and the absent case has
    its own tests (`test_boot_logging.py`, `test_home.py`). [M4.13 step 35, cs-31]
    """
    ours = shipped["json"]["artifacts/cold_eval.json"]
    assert {"cold", "ceiling"} <= set(ours["keys"]), (
        f"ColdEval reads the cold and ceiling arms and the shipped file has {sorted(ours['keys'])}"
    )
    for arm in ("cold", "ceiling", "hybrid"):
        assert "spearman" in ours[f"{arm}.keys"], f"{arm} carries no spearman"
    assert "cold:tunedblend_vs_prior" in ours["keys"]
    assert set(ours["cold:tunedblend_vs_prior.keys"]) == {"ci95", "delta"}, (
        "the interval ColdEval reports beside the two figures"
    )
    assert ours["n_test.type"] == "int"
    assert "artifacts/cold_eval.json" not in built["json"], (
        "the fixture now writes cold_eval.json - compare it to the shipped shape here, and say so "
        "loudly: e2e/run.mjs does not rebuild data/import"
    )


def test_the_cold_tower_report_line_says_the_version_was_assumed(bundle_root):
    """§4.3: "the earlier `cold_tower` run is superseded - the exporter must ship v2", and
    `tower.py` checked that against values it had substituted itself.

    The corpus writes `torch.save(model.state_dict())`: a bare mapping of eight tensors with no
    `version`, no `arch` and no `input_dim`. The loader filled in its own 2 and 'cold_tower_v2' and
    then tested those against its own allow-lists, so the guard could not fail on any bundle that
    has ever been produced while the error string presented it as enforcing the spec. It is
    deliberately NOT tightened into a refusal - that would refuse every shipped bundle - so what
    changes is that the assumption is visible where an operator reads a bundle's claims, which §10
    makes the import report. The guard that does bite on this format is the input-width cross-check
    against `feature_contract.json`, and it is untouched. [M4.13 step 36, cs-54]
    """
    report = bundle_import.validate(bundle_import.Bundle.open(bundle_root))
    notes = [f for f in report.findings if f.rule == "cold-tower"]
    assert notes, f"the tower was not constructed at all: {report.render()}"
    line = notes[0].message
    assert "assumed v2" in line, f"the report states the version as read rather than assumed: {line}"
    assert "bare state_dict" in line and "not a check" in line, line
    assert notes[0].detail["assumed"], "the assumption has to be machine-readable too"
    assert line.isascii(), f"an import report line a cp1252 console cannot print: {line!r}"
    # The tower itself carries it, which is what makes the log line and the report line one fact.
    from spielplan.models.artifacts import ArtifactStore
    from spielplan.placement.contract import FeatureContract
    from spielplan.placement.tower import load_tower

    store = ArtifactStore.open(bundle_root / "artifacts", "shapes-v1")
    contract = FeatureContract.from_store(store)
    assert any("assumed v2" in note for note in load_tower(store, contract).notes)


# --- the curated ledgers ---------------------------------------------------------------------


def test_the_fixture_corrections_ledger_has_the_shipped_header(shipped, built):
    """§8 stage 3 re-applies `corrections_v1.tsv` at every derive and §14.5 is the scar for what
    happens when it is not applied. The importer reads a column the shipped file does not have,
    so the real ledger raises KeyError instead of loading."""
    theirs = shipped["tsv"]["artifacts/corrections_v1.tsv"]
    ours = built["tsv"]["artifacts/corrections_v1.tsv"]
    assert ours == theirs, (
        f"fixture header {ours} != shipped header {theirs}. The importer parses the fixture's."
    )


def test_the_fixture_seed_list_entries_carry_the_shipped_keys(shipped, built):
    """§4.3's "100-title decade-stratified onboarding list". The shipped entries are keyed
    `kind, pct_dislike, pct_like, pct_ok, raters, title, title_id, year` — there is no `decade`
    anywhere, and `dna.py` read `int(item["decade"])`, so every real bundle's onboarding list
    loaded with a NULL decade and the stratification the list exists for was lost on import.

    This comparison is the one the file was missing: `seed_list.json` was in the manifest and
    nothing compared it, which is why the fixture could keep declaring a key of its own.
    """
    theirs = shipped["json"]["artifacts/seed_list.json"]
    ours = built["json"]["artifacts/seed_list.json"]
    assert theirs["type"] == "list" and theirs["entry_type"] == "dict", "manifest is stale"
    assert ours["type"] == theirs["type"] and ours["entry_type"] == theirs["entry_type"]
    assert ours["entry_keys"] == theirs["entry_keys"], (
        f"fixture seed-list entry keys {ours['entry_keys']} != shipped {theirs['entry_keys']}"
    )


# §6.3's two thresholds are proposal 157 ("any threshold that is a bare sigma constant belongs
# in ledger_hyperparams.json") and the corpus does not ship them yet. The fixture carries them
# under the name `from_mapping` reads, and the gap is DECLARED here the way the axis TSVs and
# `title_identity` are — with a guard below that the exception is still an exception.
PROPOSAL_157_NOT_YET_SHIPPED = frozenset({"straddle_z", "tension_credible_mass"})


def test_the_fixture_hyperparameters_are_the_constants_the_corpus_ships(shipped, built):
    """§4.3: "`ledger_hyperparams.json` — the tuned constants of the §5.2 recipe … re-tunable
    offline". `ledger_hyperparams.json` was asserted to be IN the manifest and then never
    compared, and the two files share three key names out of twelve: the corpus ships
    `anchor_ridge_lambda`, `bt_weight_lam_bt`, `learning_rate`, `margin_weight_form` and a
    nested `sigma_inflation` object, while `ledger/hyperparams.py` reads `lambda_ridge`,
    `lambda_bt`, `lr`, `margin_form`, `sigma_inflation_c` and `sigma_inflation_cap`.

    Every unmatched name is a constant the corpus project re-tunes and this app silently
    replaces with its own default — rule 1 of that module ("every constant comes from the
    bundle") failing with a note nobody reads rather than a refusal.
    """
    theirs = set(shipped["json"]["artifacts/ledger_hyperparams.json"]["keys"])
    ours = set(built["json"]["artifacts/ledger_hyperparams.json"]["keys"])
    assert theirs, "manifest is stale; the shipped bundle has no ledger_hyperparams.json"
    assert not (PROPOSAL_157_NOT_YET_SHIPPED & theirs), (
        f"the corpus now ships {sorted(PROPOSAL_157_NOT_YET_SHIPPED & theirs)} — delete the "
        "PROPOSAL_157_NOT_YET_SHIPPED entry and compare those constants like everything else."
    )
    assert (ours - PROPOSAL_157_NOT_YET_SHIPPED) == theirs, (
        f"fixture constants {sorted(ours - PROPOSAL_157_NOT_YET_SHIPPED)} != shipped "
        f"{sorted(theirs)}. A fixture naming a constant the way the app happens to read it "
        "cannot fail when the app reads the wrong name."
    )


# --- BUNDLE.json: the corpus's own record of what a bundle is ---------------------------------


def test_the_fixture_bundle_json_records_what_the_corpus_records(shipped, built):
    """The identity file the fixture invented, one directory over from the fixture this
    milestone exists to correct: it declared `vocabulary_version` and `title_count`, neither of
    which the corpus writes, and omitted `tables`, `files`, `validations`, `source_provenance`
    and the rest of what it does.

    `bundle.py` reads `vocabulary_version` from here and decision 163's refusal depends on that
    read; `ArtifactStore.summary()` wants a title count. Both are answerable from the shipped
    file — the vocabulary from the `dna_vocab/<version>/` directory, the count from `tables` —
    and neither is answerable from a key the corpus has never written.
    """
    theirs = set(shipped["json"]["BUNDLE.json"]["keys"])
    ours = set(built["json"]["BUNDLE.json"]["keys"])
    assert "tables" in theirs and "files" in theirs, "manifest is stale"
    assert ours == theirs, (
        f"fixture BUNDLE.json keys {sorted(ours)} != shipped {sorted(theirs)}. Invented: "
        f"{sorted(ours - theirs)}; missing: {sorted(theirs - ours)}."
    )
    # `tables` is a row count per shipped table, and it is where a title count comes from. A
    # fixture free to name tables of its own would make that read untestable.
    assert set(built["json"]["BUNDLE.json"]["tables.keys"]) <= set(
        shipped["json"]["BUNDLE.json"]["tables.keys"]
    ), "the fixture's BUNDLE.json counts tables the corpus does not ship"


# --- reviews.sqlite: the columns the loader actually selects -----------------------------------


def test_the_review_loader_reads_columns_the_shipped_table_has(shipped):
    """§10 ships the review bodies "needed for future re-extraction and text embedding", and
    `reviews.py` selected `rating`, `published_at` and `is_critic` — the names of the *Postgres*
    columns it writes. `reviews.sqlite` has none of the three: the data sits in `rating_norm`,
    `created_date` and `author_kind`. All 485,602 rows therefore loaded with a NULL rating, no
    date and no critic flag, under a single warn line.

    The mapping is asserted here rather than in a review test because the manifest is what makes
    the claim checkable: it is the shipped schema, not this repo's reading of it.
    """
    from spielplan.importer.reviews import REVIEW_SOURCE

    theirs = set(shipped["sqlite"]["reviews.sqlite"]["review"])
    ours = set(REVIEW_SOURCE.values())
    assert ours <= theirs, (
        f"the loader selects {sorted(ours - theirs)} from `review`, which ships "
        f"{sorted(theirs)}. A missing column is imported as NULL, so this is silent."
    )


# --- content.sqlite: no invented structure ----------------------------------------------------


def test_the_fixture_invents_no_table_the_corpus_does_not_ship(shipped, built):
    """A scale model may ship fewer tables. It may not ship tables the corpus has never
    produced, because then the importer is written against a database that does not exist."""
    theirs = set(shipped["sqlite"]["content.sqlite"])
    ours = set(built["sqlite"]["content.sqlite"])
    assert ours <= theirs, f"the fixture invents tables the bundle does not ship: {sorted(ours - theirs)}"


def test_the_fixture_invents_no_column_the_corpus_does_not_ship(shipped, built):
    """Same rule, one level down -- and this is what the `seed_list` collision looks like from
    here: the fixture's `seed_list` is Spielplan's onboarding list, while the corpus ships a
    238-row *list registry* under that name."""
    theirs = shipped["sqlite"]["content.sqlite"]
    ours = built["sqlite"]["content.sqlite"]
    invented = {
        table: sorted(set(cols) - set(theirs.get(table, ())))
        for table, cols in ours.items()
        if table in theirs and set(cols) - set(theirs[table])
    }
    assert not invented, (
        f"the fixture declares columns the shipped table does not have: {invented}"
    )


# --- the real bundle, when one is available ---------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("CORPUS_BUNDLE_DIR"),
    reason="CORPUS_BUNDLE_DIR is unset; set it to a real bundle directory to check the manifest",
)
def test_a_real_bundle_still_matches_the_committed_manifest(shipped):
    """The manifest is a snapshot, and a snapshot rots. When a real bundle is reachable, the
    committed shapes are checked against it -- so a corpus-side format change fails this repo's
    suite instead of arriving as an import-time mystery."""
    live = shapes_mod.extract(Path(os.environ["CORPUS_BUNDLE_DIR"]))
    assert live["tsv"] == shipped["tsv"], "a shipped TSV header changed"
    assert live["npz"] == shipped["npz"], "a shipped npz array set changed"
    # Exact shapes here, not ranks: against a real bundle a changed hidden width or input_dim
    # IS the architecture changing under §8 stage 9, which is the mystery this file prevents.
    assert live["pt"] == shipped["pt"], "a shipped checkpoint's tensor names or shapes changed"
    assert live["sqlite"] == shipped["sqlite"], "a shipped sqlite schema changed"
    assert live["json"] == shipped["json"], "a shipped JSON shape changed"


# --- self-test: the extractor must not leak values, and must not flatten distinctions ---------


@pytest.mark.parametrize(
    ("name", "pattern"),
    [
        ("p:director:Adam Arkin", "p:<s>:<s>"),
        ("p:composer:A.R. Rahman", "p:<s>:<s>"),
        ("decade:1990", "decade:<n>"),
        ("genre:crime", "genre:<s>"),
        ("dna:mood.bittersweet", "dna:<s>"),
        ("credit:3", "credit:<n>"),
        ("year_norm", "<s>"),
    ],
)
def test_the_pattern_reducer_keeps_the_grammar_and_drops_the_value(name, pattern):
    """The manifest is committed, so it must carry no film title, no person's name and no review
    text. `column_pattern` is what guarantees that, and a reducer that cannot be checked is a
    privacy claim nobody has tested."""
    assert shapes_mod.column_pattern(name) == pattern


def test_the_pattern_reducer_separates_grammars_that_differ():
    """A reducer that mapped everything to one token would make every assertion above vacuous."""
    assert shapes_mod.column_pattern("p:director:X") != shapes_mod.column_pattern("credit:3")
    assert shapes_mod.column_pattern("decade:1990") != shapes_mod.column_pattern("genre:crime")


def test_the_committed_manifest_carries_no_prose(shipped):
    """Belt and braces on the same promise: nothing in the committed file reads like data."""
    found: list[str] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for value in node:
                walk(value, path)
        elif isinstance(node, str) and " " in node and "<s>" not in node and "<n>" not in node:
            found.append(f"{path}: {node[:60]}")

    walk({k: v for k, v in shipped.items() if k != "_note"})
    assert not found, f"the manifest carries value-shaped strings: {found[:5]}"


def test_the_manifest_covers_the_artifacts_the_app_reads(shipped):
    """A manifest that omitted the files under test would pass every assertion above while
    pinning nothing."""
    files = set(shipped["files"])
    for required in (
        "BUNDLE.json",
        "content.sqlite",
        "reviews.sqlite",
        "artifacts/feature_contract.json",
        "artifacts/backbone.npz",
        "artifacts/cold_tower.pt",
        "artifacts/corrections_v1.tsv",
        "artifacts/ledger_hyperparams.json",
        "artifacts/seed_list.json",
        # M4.13: the only reference value in the bundle for a number the app computes itself.
        "artifacts/cold_eval.json",
    ):
        assert required in files, f"{required} is absent from the manifest"


def test_sqlite_shapes_are_read_without_row_counts(tmp_path):
    """The manifest pins structure, not volume: a fixture with eight titles and a bundle with
    19,071 must agree, or the manifest churns and nobody regenerates it."""
    db = tmp_path / "x.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a integer, b text)")
    conn.execute("INSERT INTO t VALUES (1, 'x')")
    conn.commit()
    conn.close()
    assert shapes_mod._sqlite_shapes(db) == {"t": ["a", "b"]}


# --- the shapes the corpus ships and the old fixture could not express -------------------------
#
# Everything above compares STRUCTURE, because `real_bundle_shapes.json` is shapes-only by
# design (its own `_note` says so: a committed manifest may carry no film title and no person's
# name). That is exactly why the three defects below were invisible to it -- a duplicated
# department, a facet spelling and a pool size are all *values*. These three read the built
# bundle's rows instead, and they are the reason `bundle_root` above is a fixture of its own.
# Milestone M4.8, row `platform-fixture-carries-the-corpus-awkward-shapes`.


def _content(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "content.sqlite")


def test_the_fixture_ships_a_credit_recorded_under_two_departments(bundle_root):
    """§4.1: "credit (dedupe at read time, never at import)" -- so the collision has to survive
    the import and reach the read layer intact, and a fixture that cannot emit one cannot
    falsify what the read layer does with it.

    TMDB files one job under two department spellings. The real export carries 7,918
    (title, person, job) triples spanning more than one department across 1,216 of its 19,071
    titles, 816 of them inside the twelve credits §6.0's card renders. Until this row the only
    bundle in the suite gave every credit a distinct (title, person, department, job), so the
    duplicate existed only where `test_import_integration.py` inserted one by hand -- a test
    that proves the query, and proves nothing about the input the importer is handed.
    """
    db = _content(bundle_root)
    try:
        collisions = db.execute(
            "SELECT title_id, person_id, job, count(DISTINCT department) FROM credit "
            "GROUP BY title_id, person_id, job HAVING count(DISTINCT department) > 1"
        ).fetchall()
    finally:
        db.close()
    assert collisions, (
        "no (title_id, person_id, job) in the fixture is filed under two department spellings, "
        "so the shape 1,216 real titles carry cannot be produced at all"
    )


def test_the_fixture_ships_both_facet_namings(bundle_root):
    """The two namings coexist in one bundle, which is the whole of the defect.

    `dna_tag.facet` and `dna_projected.facet` carry the corpus's *extraction* labels
    (`mood_tone`, `narrative_themes`, `character_dynamics`) while the term ids and every
    vocabulary file carry the short facet id the term is prefixed with (`mood.dread` ->
    `mood`). 29,188 of 31,540 real `dna_tag` rows and 206,151 of 223,136 `dna_projected` rows
    mismatch, and `0004_dna.sql:73-90` and `:104-116` give neither column a foreign key to
    `dna_facet`, which is precisely why nothing raises: `importer/dna.py:295-310` and `:365-380`
    copy the shipped column verbatim and `load_vocabulary` derives its facet from the prefix, so
    the join is empty and every facet renders in the neutral colour.

    Both directions are asserted. A fixture whose facets ALL mismatched would be as useless as
    one where none did: the app's own read path has to keep working on the rows that agree,
    which is what makes the failure partial and therefore silent.
    """
    # The three labels the review measured on the real bundle, written out here rather than read
    # from `make_bundle.EXTRACTION_LABELS`, because that dict cannot be its own witness: a fourth
    # label invented in the fixture would be added there too, and a check that read it would agree
    # with the invention. Step 4b bolds the prohibition -- "Invent no fourth label: three measured
    # labels plus the identical remainder IS the shape, and a guessed label would be exactly the
    # fixture-invents-a-structure failure M4.5 exists to end".
    # [M4.8 review cycle 3: m48-rev3-fixture-01]
    measured = {"mood_tone", "narrative_themes", "character_dynamics"}
    db = _content(bundle_root)
    try:
        for table in ("dna_tag", "dna_projected"):
            rows = db.execute(f"SELECT term, facet FROM {table}").fetchall()  # noqa: S608
            assert rows, f"{table} is empty; this comparison would pass without comparing anything"
            mismatched = [(t, f) for t, f in rows if f != t.split(".", 1)[0]]
            agreeing = [(t, f) for t, f in rows if f == t.split(".", 1)[0]]
            assert mismatched, (
                f"every {table} row's facet equals its term's prefix, which is the shape the app "
                "wishes the corpus wrote; 29,188 of 31,540 real rows do not"
            )
            assert agreeing, (
                f"no {table} row's facet equals its term's prefix, so the fixture ships one "
                "naming rather than the two that coexist in a real bundle"
            )
            # WHICH label, not merely that one differs. `shipped_facet` (make_bundle.py:131-136)
            # is the rule the milestone wrote for this column and then applied to the generated
            # pool only -- the authored EXTRACTED/PROJECTED literals are inserted verbatim -- so
            # a row whose facet was spelled from the line above it rather than from the mapping
            # passed both assertions above. Measured: `mood.cosy` moved back to `mood` while
            # `mood.dread` keeps `mood_tone` ships one facet under two extraction labels in one
            # export, a shape the corpus does not produce, and `visual.neon` filed under
            # `visual_style` invents the fourth label; both were green.
            wrong = [(t, f) for t, f in rows if f != make_bundle.shipped_facet(t)]
            assert wrong == [], (
                f"{table} rows whose facet is not the one the mapping gives their term: {wrong}; "
                "the fixture would then ship a naming no export produces and M4.9's repair would "
                "be verified against it"
            )
            assert {f for _, f in mismatched} <= measured, (
                f"{table} carries an extraction label the review never measured: "
                f"{sorted({f for _, f in mismatched} - measured)}"
            )
    finally:
        db.close()


def test_the_scale_mode_grows_the_pool_without_widening_the_contract(tmp_path, bundle_root):
    """The pool is opt-in and costs the contract nothing.

    The real bundle yields 696 owned movies at §6's default 130-minute room, where the Tonight
    selector's replay cost is measured in tens of seconds against a 1.5 s budget. The pair
    search itself no longer needs a bundle to be guarded -- e87deed times `select` against a
    synthesized belief dict at 716 and 696 candidates -- but a belief dict is not a pool: it is
    ints to floats, and it cannot reach the importer, the database or `GET /seats/{id}/round`.
    The half M4.12's exit script measures is the seeded round, and at eight titles nothing in
    this suite can seed it. `pool_titles` supplies one -- and it must supply *only* that: the
    generated titles reuse the authored genres, keywords, people, terms and axes, so
    `feature_contract.json` is unchanged and every assertion above still holds against the
    default bundle. A pool that widened a block would move the tower's `input_dim` and make the
    timing fixture a different model.
    """
    root = make_bundle.make_bundle(tmp_path / "pool", pool_titles=700)

    db = _content(root)
    try:
        owned = db.execute(
            "SELECT count(*) FROM title WHERE kind = 'movie' AND is_owned"
        ).fetchone()[0]
    finally:
        db.close()
    assert owned >= 700, f"pool_titles=700 produced {owned} owned movies"

    def contract(where: Path) -> dict:
        return json.loads((where / "artifacts" / "feature_contract.json").read_text("utf-8"))

    default, scaled = contract(bundle_root), contract(root)
    assert scaled["feature_names"] == default["feature_names"], (
        "the pool changed the contract's columns: added "
        f"{sorted(set(scaled['feature_names']) - set(default['feature_names']))}"
    )
    assert scaled["input_dim"] == default["input_dim"]

    # §6.4's axis TSVs are the one artifact this fixture ships that the corpus does not
    # (SPEC_REQUIRED_NOT_YET_SHIPPED above), so nothing else would notice if the pool stopped
    # writing them -- and an axis with no file is a facet with no coordinate.
    axes = {p.name for p in (root / "artifacts" / "dna_vocab" / "v1" / "axes").glob("*.tsv")}
    assert axes == {f"{facet}.tsv" for facet in make_bundle.AXES}
