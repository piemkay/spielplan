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
def built(tmp_path_factory) -> dict:
    """The shape of the bundle `make_bundle` produces, read the same way."""
    root = tmp_path_factory.mktemp("fixture-bundle")
    make_bundle.make_bundle(root)
    return shapes_mod.extract(root)


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
