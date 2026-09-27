"""The fixture held to the shape of a bundle the corpus actually produced: `real_bundle_shapes.json`,
shapes only (no values), extracted by `ops/bundle_shapes.py`; a real bundle too with `CORPUS_BUNDLE_DIR`."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from spielplan.importer import bundle as bundle_import
from spielplan.importer.validate import FROZEN_RATING_SOURCE_IDS
from spielplan.ledger.hyperparams import DEFAULTS, from_mapping
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
    if not SHAPES.is_file():                                    # pragma: no cover - see docstring
        pytest.fail(
            f"{SHAPES} is missing. It is the ground truth for every assertion in this file; "
            "regenerate it with ops/bundle_shapes.py against a real bundle."
        )
    return json.loads(SHAPES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bundle_root(tmp_path_factory) -> Path:
    """The three tests at the foot read its rows, which a shape manifest by design cannot carry."""
    root = tmp_path_factory.mktemp("fixture-bundle")
    make_bundle.make_bundle(root)
    return root


@pytest.fixture(scope="module")
def built(bundle_root) -> dict:
    return shapes_mod.extract(bundle_root)


def test_the_fixture_contract_declares_the_keys_the_shipped_contract_declares(shipped, built):
    """§4.3 calls `feature_contract.json` the exhaustive definition of the tower's input."""
    ours = set(built["json"]["artifacts/feature_contract.json"]["keys"])
    theirs = set(shipped["json"]["artifacts/feature_contract.json"]["keys"])
    # Equality, not a subset: a fixture that added the shipped keys while keeping its own would pass.
    assert ours == theirs, (
        f"fixture contract keys {sorted(ours)} != shipped {sorted(theirs)}. "
        "Extra fixture keys are as bad as missing ones: they are what the parser reads."
    )


def test_the_fixture_lists_feature_names_the_way_the_shipped_contract_does(shipped, built):
    """The shipped contract is one flat list of 6,435 names, not a dict keyed by block."""
    theirs = shipped["json"]["artifacts/feature_contract.json"]
    ours = built["json"]["artifacts/feature_contract.json"]
    assert "feature_names.item_patterns" in theirs, "manifest is stale; regenerate it"
    assert "feature_names.item_patterns" in ours, (
        "the fixture's `feature_names` is not a flat list of column names, but the shipped "
        "contract's is -- so `feature_names` means two different things in the two files."
    )


def test_the_fixture_uses_the_shipped_column_grammar_in_every_block(shipped, built):
    """Every block uses a `<tag>:` prefix; a scale model may use fewer grammars, never an unshipped one."""
    theirs = set(shipped["json"]["artifacts/feature_contract.json"]["feature_names.item_patterns"])
    ours = set(built["json"]["artifacts/feature_contract.json"].get("feature_names.item_patterns", []))
    assert "p:<s>:<s>" in theirs, f"manifest is stale; shipped patterns are {sorted(theirs)}"
    # Without this the subset below passes vacuously on an empty set.
    assert ours, (
        "the fixture's contract yields no column patterns at all -- `feature_names` is not a "
        "flat list of names, so this comparison would pass without comparing anything."
    )
    assert ours <= theirs, (
        f"the fixture declares column grammars the shipped contract does not have: "
        f"{sorted(ours - theirs)}. Shipped: {sorted(theirs)}. A fixture that names columns the "
        "way the builder keys them cannot fail when the builder is wrong."
    )


# Decision 162's `title_identity` is not exported yet: the fixture ships it and the gap is declared here.
DECISION_162_NOT_YET_SHIPPED = {"artifacts/backbone.npz": {"title_identity"}}


def test_the_fixture_npz_arrays_are_named_the_way_the_corpus_names_them(shipped, built):
    """`title_ids` versus `title_id`: one character, and stage 9 cannot find a coordinate."""
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


# §6.4's axis TSVs are not shipped yet; declared file by file, built off `make_bundle.AXES`.
SPEC_REQUIRED_NOT_YET_SHIPPED = tuple(
    f"artifacts/dna_vocab/v1/{facet}.tsv" for facet in sorted(make_bundle.AXES)
)


def test_the_fixture_ships_the_dna_vocabulary_files_the_corpus_ships(shipped, built):
    """`dna_tag` and `dna_projected` FK to `dna_vocabulary(version)`,
    so misnamed files load no DNA at all."""
    theirs = {f for f in shipped["files"] if f.startswith("artifacts/dna_vocab/")}
    ours = {f for f in built["files"] if f.startswith("artifacts/dna_vocab/")}
    assert theirs, "manifest is stale; the shipped bundle has no dna_vocab directory"

    declared = {f for f in ours if f.startswith(SPEC_REQUIRED_NOT_YET_SHIPPED)}
    # The exception must still be one: if the corpus starts shipping axes, this fails.
    assert not (declared & theirs), (
        f"the corpus now ships {sorted(declared & theirs)} — delete the "
        "SPEC_REQUIRED_NOT_YET_SHIPPED entry and compare them like everything else."
    )
    assert (ours - declared) <= theirs, (
        f"the fixture invents vocabulary files the corpus does not ship: "
        f"{sorted((ours - declared) - theirs)}. Shipped: {sorted(theirs)}."
    )


def test_the_fixture_checkpoint_names_its_tensors_the_way_the_corpus_does(shipped, built):
    """`cold_tower.pt` is a bare state_dict, so tensor
    names are the architecture; shapes compared by rank."""
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
    """64-d is a constant here and a trained weight shape in the corpus."""
    from spielplan.placement.tower import EMBED_DIM

    head = shipped["pt"]["artifacts/cold_tower.pt"]["head_e.weight"]
    assert head[0] == EMBED_DIM, (
        f"the shipped tower emits {head[0]}-d embeddings and this app is built for {EMBED_DIM}"
    )


def test_the_cold_tower_report_line_says_the_version_was_assumed(bundle_root):
    """The checkpoint carries no version, so the report states the version was assumed, not enforced."""
    report = bundle_import.validate(bundle_import.Bundle.open(bundle_root))
    notes = [f for f in report.findings if f.rule == "cold-tower"]
    assert notes, f"the tower was not constructed at all: {report.render()}"
    line = notes[0].message
    assert "assumed v2" in line, f"the report states the version as read rather than assumed: {line}"
    assert "bare state_dict" in line and "not a check" in line, line
    assert notes[0].detail["assumed"], "the assumption has to be machine-readable too"
    assert line.isascii(), f"an import report line a cp1252 console cannot print: {line!r}"
    # The tower itself carries it, which makes the log line and the report line one fact.
    from spielplan.models.artifacts import ArtifactStore
    from spielplan.placement.contract import FeatureContract
    from spielplan.placement.tower import load_tower

    store = ArtifactStore.open(bundle_root / "artifacts", "shapes-v1")
    contract = FeatureContract.from_store(store)
    assert any("assumed v2" in note for note in load_tower(store, contract).notes)


def test_the_fixture_corrections_ledger_has_the_shipped_header(shipped, built):
    """The importer read a column the shipped ledger does not have, so the real one raised KeyError."""
    theirs = shipped["tsv"]["artifacts/corrections_v1.tsv"]
    ours = built["tsv"]["artifacts/corrections_v1.tsv"]
    assert ours == theirs, (
        f"fixture header {ours} != shipped header {theirs}. The importer parses the fixture's."
    )


def test_the_fixture_seed_list_entries_carry_the_shipped_keys(shipped, built):
    """The shipped entries have no `decade`; reading one loaded every onboarding list with NULL decades."""
    theirs = shipped["json"]["artifacts/seed_list.json"]
    ours = built["json"]["artifacts/seed_list.json"]
    assert theirs["type"] == "list" and theirs["entry_type"] == "dict", "manifest is stale"
    assert ours["type"] == theirs["type"] and ours["entry_type"] == theirs["entry_type"]
    assert ours["entry_keys"] == theirs["entry_keys"], (
        f"fixture seed-list entry keys {ours['entry_keys']} != shipped {theirs['entry_keys']}"
    )


# Proposal 157's two thresholds are not shipped yet: declared here like the axes and `title_identity`.
PROPOSAL_157_NOT_YET_SHIPPED = frozenset({"straddle_z", "tension_credible_mass"})


def test_the_fixture_hyperparameters_are_the_constants_the_corpus_ships(shipped, built):
    """The two files shared three key names of twelve; every
    unmatched one is a silently defaulted constant."""
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


def test_the_declared_exception_carries_the_thresholds_the_app_actually_ships(bundle_root):
    """The fixture is the only bundle that ships §6.3's thresholds, so it must not carry a retired value."""
    raw = json.loads(
        (bundle_root / "artifacts" / "ledger_hyperparams.json").read_text(encoding="utf-8")
    )
    loaded, _ = from_mapping(raw)
    assert loaded.source == "bundle", "read through the loader the app boots on, not the defaults"
    assert loaded.straddle_z == DEFAULTS.straddle_z, (
        f"the fixture bundle boots §6.3's badge at straddle_z={loaded.straddle_z} while the app "
        f"ships {DEFAULTS.straddle_z}: every stack this repo can boot badges a whole fitted "
        "board and the queue's boundary and exploration arms draw from the same set."
    )
    assert loaded.tension_credible_mass == DEFAULTS.tension_credible_mass, (
        f"the fixture bundle boots §6.3's tension badge at {loaded.tension_credible_mass} "
        f"while the app ships {DEFAULTS.tension_credible_mass}."
    )


def test_the_fixture_bundle_json_records_what_the_corpus_records(shipped, built):
    """The fixture invented `vocabulary_version` and `title_count`; the corpus writes neither."""
    theirs = set(shipped["json"]["BUNDLE.json"]["keys"])
    ours = set(built["json"]["BUNDLE.json"]["keys"])
    assert "tables" in theirs and "files" in theirs, "manifest is stale"
    assert ours == theirs, (
        f"fixture BUNDLE.json keys {sorted(ours)} != shipped {sorted(theirs)}. Invented: "
        f"{sorted(ours - theirs)}; missing: {sorted(theirs - ours)}."
    )
    # `tables` is where a title count comes from, so the fixture may not name tables of its own.
    assert set(built["json"]["BUNDLE.json"]["tables.keys"]) <= set(
        shipped["json"]["BUNDLE.json"]["tables.keys"]
    ), "the fixture's BUNDLE.json counts tables the corpus does not ship"


def test_the_review_loader_reads_columns_the_shipped_table_has(shipped):
    """The loader selected Postgres column names that `reviews.sqlite` does not have."""
    from spielplan.importer.reviews import REVIEW_SOURCE

    theirs = set(shipped["sqlite"]["reviews.sqlite"]["review"])
    ours = set(REVIEW_SOURCE.values())
    assert ours <= theirs, (
        f"the loader selects {sorted(ours - theirs)} from `review`, which ships "
        f"{sorted(theirs)}. A missing column is imported as NULL, so this is silent."
    )


def test_the_fixture_invents_no_table_the_corpus_does_not_ship(shipped, built):
    """A scale model may ship fewer tables, never tables the corpus has never produced."""
    theirs = set(shipped["sqlite"]["content.sqlite"])
    ours = set(built["sqlite"]["content.sqlite"])
    assert ours <= theirs, f"the fixture invents tables the bundle does not ship: {sorted(ours - theirs)}"


def test_the_fixture_invents_no_column_the_corpus_does_not_ship(shipped, built):
    """The fixture's `seed_list` is an onboarding list; the corpus ships a list registry under that name."""
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


@pytest.mark.skipif(
    not os.environ.get("CORPUS_BUNDLE_DIR"),
    reason="CORPUS_BUNDLE_DIR is unset; set it to a real bundle directory to check the manifest",
)
def test_a_real_bundle_still_matches_the_committed_manifest(shipped):
    """A snapshot rots; a real bundle, when reachable, is checked against it."""
    live = shapes_mod.extract(Path(os.environ["CORPUS_BUNDLE_DIR"]))
    assert live["tsv"] == shipped["tsv"], "a shipped TSV header changed"
    assert live["npz"] == shipped["npz"], "a shipped npz array set changed"
    # Exact shapes here: against a real bundle a changed width IS the architecture changing.
    assert live["pt"] == shipped["pt"], "a shipped checkpoint's tensor names or shapes changed"
    assert live["sqlite"] == shipped["sqlite"], "a shipped sqlite schema changed"
    assert live["json"] == shipped["json"], "a shipped JSON shape changed"


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
    """The committed manifest must carry no title, name or review text."""
    assert shapes_mod.column_pattern(name) == pattern


def test_the_pattern_reducer_separates_grammars_that_differ():
    """A reducer that mapped everything to one token would make every assertion above vacuous."""
    assert shapes_mod.column_pattern("p:director:X") != shapes_mod.column_pattern("credit:3")
    assert shapes_mod.column_pattern("decade:1990") != shapes_mod.column_pattern("genre:crime")


# An allow-list, not a reject-list: prose is unbounded, while a shape manifest's legitimate leaves
# (names, paths, patterns, type names, frozen ids, cut grids) are few and can be enumerated.

# Admitted by name: `NoneType` is capitalised, and a form admitting it would admit `Kurosawa`.
_TYPE_NAMES = frozenset({"NoneType", "bool", "dict", "float", "int", "list", "object", "str"})

# The suffixes the corpus ships; any other path is admitted only by the manifest's own `files` list.
_BUNDLE_SUFFIXES = frozenset({".json", ".md", ".npz", ".pt", ".sqlite", ".tsv", ".txt"})

# The manifest's own punctuation; everything between two separators must be a schema-shaped token.
_MANIFEST_SEPARATORS = re.compile(r"[/.:_-]")

# A letter run followed by three or more digits is an external id (`tt0113277`).
_EXTERNAL_ID = re.compile(r"[A-Za-z][0-9]{3,}")

# `biB` is lower-case stem then capitals; `Heat` is the other way round.
_VARIANT_TAG = re.compile(r"[a-z0-9]+[A-Z]+")

# `shares_fixed.json` is keyed by slash-joined decimals like `0.20/0.35/0.45`.
_CUT_GRID = re.compile(r"[0-9]+\.[0-9]{2}(?:/[0-9]+\.[0-9]{2})+")


def _is_schema_token(text: str) -> bool:
    if not text or _EXTERNAL_ID.search(text):
        return False
    for segment in _MANIFEST_SEPARATORS.split(text):
        if segment in ("<s>", "<n>") or segment.isdigit():
            continue
        # Rejects the empty segment too, which is how a leading slash and `..` get out.
        if not segment.isalnum():
            return False
        if not (segment.islower() or segment.isupper() or _VARIANT_TAG.fullmatch(segment)):
            return False
    return True


def _is_bundle_path(text: str) -> bool:
    """Includes upstream `prep/` and `datasets/` paths from BUNDLE.json's `source_provenance`."""
    stem, dot, suffix = text.rpartition(".")
    return bool(stem) and dot == "." and f".{suffix}" in _BUNDLE_SUFFIXES


def _prose_in_manifest(manifest: dict) -> list[str]:
    """Rendered with `ascii()`: a leaked leaf is likely non-ASCII and must not crash a cp1252 console."""
    files = set(manifest.get("files", ()))
    found: list[str] = []

    def admitted(leaf: str) -> bool:
        if leaf in _TYPE_NAMES:
            return True
        if not _is_schema_token(leaf):
            return False
        if leaf.isdigit():
            # The frozen `rating_source` ids (§4.1 rule 4) are
            # the only bare numbers a shape manifest carries.
            return int(leaf) in FROZEN_RATING_SOURCE_IDS
        if "/" in leaf or "." in leaf:
            return leaf in files or _is_bundle_path(leaf) or bool(_CUT_GRID.fullmatch(leaf))
        return True

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for value in node:
                walk(value, path)
        elif isinstance(node, str) and not admitted(node):
            found.append(f"{path}: {ascii(node)}")

    walk({k: v for k, v in manifest.items() if k != "_note"})
    return found


def test_the_committed_manifest_carries_no_prose(shipped):
    """An allow-list, with self-tests proving it rejects values and admits schema tokens."""
    found = _prose_in_manifest(shipped)
    assert not found, (
        f"{len(found)} leaf string(s) in the committed manifest are not shapes the allow-list "
        f"admits -- regenerate it, or widen `_is_schema_token` with a reason: {found[:5]}"
    )


# A title, a person, an external id and a two-word name.
_PROSE_THE_MANIFEST_MUST_NEVER_CARRY = (
    ("Heat", "a one-word film title -- invisible to the space test this replaced"),
    ("Kurosawa", "a person's name, the second thing the manifest's `_note` promises is absent"),
    ("tt0113277", "an external id, which is what makes a leaked leaf re-identifiable"),
    ("Michael Mann", "the two-word name -- the only shape the old guard could ever see"),
)


def _manifest_carrying(leaf: str | None) -> dict:
    """The leaf is planted in a column list, a JSON key set and the file list; everything else is copied."""
    planted = [leaf] if leaf is not None else []
    return {
        "_note": "Shapes only -- no values. Regenerate with ops/bundle_shapes.py.",
        "files": ["content.sqlite", "artifacts/backbone.npz", *planted],
        "sqlite": {"content.sqlite": {"title": ["id", "tmdb_id", *planted]}},
        "json": {
            "artifacts/manifest.json": {
                "type": "object",
                "keys": ["class_shares", "fitted_cuts", *planted],
                "class_shares.keys": ["netflix-prize", "movielens-32m"],
                "fitted_cuts.keys": ["1", "31"],
                "n_titles.type": "int",
            }
        },
        "npz": {"artifacts/backbone.npz": ["E", "E_hat", "biB", "b_i"]},
        "tsv": {"artifacts/corrections_v1.tsv": ["kind", "title_id", "value"]},
        "pt": {"artifacts/cold_tower.pt": {"head_e.weight": [64, 768]}},
    }


@pytest.mark.parametrize(("leaf", "why"), _PROSE_THE_MANIFEST_MUST_NEVER_CARRY)
def test_the_manifest_guard_rejects_the_values_it_promises_are_absent(leaf, why):
    """Planted in all three positions, because the leak channel nobody predicted is the one that matters."""
    assert not _prose_in_manifest(_manifest_carrying(None)), (
        "the scaffolding this test plants into is not itself clean, so a rejection below would "
        "prove nothing about the planted leaf"
    )
    found = _prose_in_manifest(_manifest_carrying(leaf))
    caught = [line for line in found if line.endswith(f": {ascii(leaf)}")]
    assert {line.rsplit(": ", 1)[0] for line in caught} == {
        "/files",
        "/sqlite/content.sqlite/title",
        "/json/artifacts/manifest.json/keys",
    }, f"{leaf!r} ({why}) reached the manifest unnamed somewhere: caught {found}"


@pytest.mark.parametrize(
    "leaf",
    [
        "title_id",                                  # column names: rejecting them gets the guard waived
        "imdb_id",
        "content.sqlite",                            # a path in the manifest's own `files` list
        "artifacts/dna_vocab/v1/vocab_mood_v1.tsv",  # a path admitted by form, not by that list
        "prep/cold_tower_artifacts.npz",             # upstream, in BUNDLE.json's provenance map
        "p:<s>:<s>",                                 # what `column_pattern` reduces a person to
        "decade:<n>",
        "<s>",
        "E_hat",                                     # `backbone.npz` array names: capital first
        "biB",                                       # and capital last
        "netflix-prize",                             # a rating-source class `audit.json` keys on
        "ratings_from_users_ge_100",                 # a long digit run behind a separator
        "s_pruned_20260825",                         # and a longer one
        "cold:tunedblend_vs_prior",                  # a `cold_eval.json` metric key
        "0.20/0.35/0.45",                            # §8's share-cut grid
        "31",                                        # a frozen `rating_source` id, §4.1 rule 4
        "str",                                       # what `_json_shape` writes for a scalar
    ],
)
def test_the_manifest_guard_admits_what_a_shape_manifest_legitimately_carries(leaf):
    """Every one is a leaf the committed manifest carries today."""
    manifest = _manifest_carrying(None)
    manifest["json"]["artifacts/manifest.json"]["keys"].append(leaf)
    assert not _prose_in_manifest(manifest), f"{leaf!r} is a shape the manifest carries today"


def test_the_manifest_covers_the_artifacts_the_app_reads(shipped):
    """A manifest that omitted the files under test would pin nothing."""
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
    """Structure, not volume: eight titles and 19,071 must agree."""
    db = tmp_path / "x.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a integer, b text)")
    conn.execute("INSERT INTO t VALUES (1, 'x')")
    conn.commit()
    conn.close()
    assert shapes_mod._sqlite_shapes(db) == {"t": ["a", "b"]}


# A duplicated department, a facet spelling and a pool size are values, so these read the built rows.


def _content(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "content.sqlite")


def test_the_fixture_ships_a_credit_recorded_under_two_departments(bundle_root):
    """§4.1: credits dedupe at read time, never at import, so the fixture must ship a collision."""
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
    """Both namings coexist, which is what makes the failure partial and silent."""
    # Written out, not read from `make_bundle.EXTRACTION_LABELS`, which cannot be its own witness.
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
            # WHICH label: a facet spelled from the line above
            # rather than the mapping passed both checks above.
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
    """The pool is opt-in and must not widen the contract, or the tower's `input_dim` moves."""
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

    # Axis TSVs are the one artifact the corpus does not ship,
    # so only this notices if the pool stops writing them.
    vocab = root / "artifacts" / "dna_vocab" / "v1"
    assert {f"{facet}.tsv" for facet in make_bundle.AXES} <= {p.name for p in vocab.glob("*.tsv")}
    assert not (vocab / "axes").exists(), "an axis in a subdirectory cannot reach a real bundle"
