"""§5.2's constants come from the bundle. Spec v2.1 §4.3, §5.2, §3.1.

§4.3: "`ledger_hyperparams.json` — the tuned constants of the §5.2 recipe … re-tunable offline
in the corpus project. Per-user cutpoints and per-arm sensitivities are **not** shipped — they
are fitted in-app by design."

Two properties, and both fail quietly rather than loudly: a constant read and then ignored is a
knob the corpus project tunes into a void, and a bundle-less household that silently uses
someone's defaults while reporting them as measured is worse than one that says so.

The last section is integration, against a real Postgres, and has to be: §10 makes a bundle swap
a restart, so "read once" is a claim about the *process* — it lives in `app.state`, which only
the real lifespan fills, from a store only the `artifact_bundle` row can name. Skipped without
TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import types

import httpx
import pytest

from spielplan.ledger import hyperparams as hp_module
from spielplan.ledger.hyperparams import DEFAULTS, Hyperparams, from_mapping, load


class _Store:
    """The two things `load` asks an ArtifactStore for."""

    def __init__(self, root=None):
        self.root = root
        self.is_empty = root is None

    def path(self, name):
        return self.root / name


# --- the numbers §4.3 names -----------------------------------------------------------------


def test_the_spec_own_numbers_are_the_defaults():
    """§4.3 gives two of them in the prose itself. If the file drifts from the spec, the drift
    should be visible here rather than in a fit nobody can explain."""
    assert DEFAULTS.lambda_ridge == 3.0        # "anchor (ridge) strength λ (currently 3.0)"
    assert DEFAULTS.tie_prior_delta0 == 0.22   # "tie-prior initialisation δ₀ = 0.22"
    assert DEFAULTS.source == "default"


def test_the_tie_prior_converts_to_davidsons_nu():
    """δ₀ is a tie *rate*; ν is Davidson's parameter. P(TIE | d=0) = ν/(2+ν), so ν = 2δ/(1−δ) —
    and 0.22 is the measured share of random pairs that are genuine ties (§4.2)."""
    assert DEFAULTS.nu0() == pytest.approx(2 * 0.22 / 0.78)
    tie_rate = DEFAULTS.nu0() / (2 + DEFAULTS.nu0())
    assert tie_rate == pytest.approx(0.22)


def test_the_decisive_toggle_carries_the_numbers_the_copy_promises():
    """§6.1: "a persistent decisive toggle sets the margin weight (~1.6 vs 1.0)"."""
    assert DEFAULTS.margin_for(decisive=True) == 1.6
    assert DEFAULTS.margin_for(decisive=False) == 1.0


# --- reading a bundle -------------------------------------------------------------------------


def test_a_bundle_constant_replaces_the_default():
    hp, notes = from_mapping({"lambda_ridge": 7.5, "steps": 40})
    assert hp.lambda_ridge == 7.5
    assert hp.steps == 40
    assert hp.source == "bundle"
    assert any("omits" in n for n in notes), "a partial bundle should say what it left out"


def test_a_bundle_that_omits_everything_still_fits():
    """§3.1 makes an empty artifact store legal, and a bundle may ship a subset."""
    hp, notes = from_mapping({})
    assert hp.lambda_ridge == DEFAULTS.lambda_ridge
    assert hp.source == "bundle"
    assert notes


def test_per_user_keys_are_ignored_and_reported():
    """§4.3: per-user cutpoints and per-arm sensitivities "are fitted in-app by design". Taking
    them from a bundle would replace one household's fitted thresholds with another's."""
    hp, notes = from_mapping(
        {"lambda_ridge": 4.0, "cutpoints": [1, 2, 3], "per_user_sensitivity": 0.5}
    )
    assert hp.lambda_ridge == 4.0
    assert not hasattr(hp, "cutpoints")
    assert sum("ignored per-user key" in n for n in notes) == 2


def test_an_unknown_constant_is_reported_rather_than_dropped():
    """A constant the corpus project tuned and this app silently ignores is exactly the kind of
    thing that looks like it is working."""
    _hp, notes = from_mapping({"lambda_lunar": 1.0})
    assert any("unknown hyperparameter 'lambda_lunar'" in n for n in notes)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"lambda_ridge": -1.0}, "positive"),
        ({"lambda_ridge": 0}, "positive"),
        ({"b_i_tau": "big"}, "positive"),
        ({"steps": 0}, "positive integer"),
        ({"steps": 2.5}, "positive integer"),
        ({"tie_prior_delta0": 0.0}, "probability"),
        ({"tie_prior_delta0": 1.0}, "probability"),
        ({"margin_form": "margin^2"}, "margin_form"),
        ({"sigma_inflation_cap": -3}, "sigma_inflation_cap"),
    ],
)
def test_a_nonsensical_constant_is_refused_at_the_boundary(payload, message):
    """Not clamped, not defaulted — refused. A λ of −1 is a corpus-side bug, and a fit that
    quietly substitutes 3.0 for it hides the bug in a number nobody will question."""
    with pytest.raises(ValueError, match=message):
        from_mapping(payload)


def test_sigma_inflation_cap_accepts_the_spec_word_and_a_number():
    """§4.3 ships "σ-inflation rate constant and cap"; §5.2 says the cap is the prior σ, which
    is per title and so cannot be a constant — hence the sentinel."""
    assert from_mapping({"sigma_inflation_cap": "prior"})[0].sigma_inflation_cap == "prior"
    assert from_mapping({"sigma_inflation_cap": 2.5})[0].sigma_inflation_cap == 2.5


# --- the digest -------------------------------------------------------------------------------


def test_the_digest_changes_with_any_constant_that_changes_a_fit():
    """`ledger_fit.hp_digest` is a precondition, not a hint: a cached fit built under other
    constants is wrong rather than stale."""
    base = DEFAULTS.digest()
    for field, value in (
        ("lambda_ridge", 4.0), ("lambda_bt", 2.0), ("steps", 5), ("lr", 0.2),
        ("margin_weighting", False), ("margin_form", "none"), ("tie_prior_delta0", 0.3),
        ("b_i_tau", 0.5), ("sigma_inflation_c", 0.1), ("sigma_inflation_cap", 3.0),
    ):
        import dataclasses

        assert dataclasses.replace(DEFAULTS, **{field: value}).digest() != base, field


def test_provenance_alone_does_not_invalidate_a_cache():
    """The same constants from a bundle and from the defaults produce the same fit, so a cache
    thrown away over provenance would be thrown away for no numerical reason."""
    import dataclasses

    assert dataclasses.replace(DEFAULTS, source="bundle").digest() == DEFAULTS.digest()


# --- loading from a store ----------------------------------------------------------------------


def test_a_bundle_less_household_gets_defaults_and_is_told_so(tmp_path):
    """§3.1: an empty artifact store is legal, so a household can rate before any corpus export
    exists. What it must not do is present this app's guesses as the corpus's measurements."""
    hp, notes = load(_Store(None))
    assert hp is DEFAULTS
    assert any("no artifact bundle" in n for n in notes)


def test_a_bundle_with_no_hyperparams_file_is_not_an_error(tmp_path):
    hp, notes = load(_Store(tmp_path))
    assert hp is DEFAULTS
    assert any("ships no ledger_hyperparams.json" in n for n in notes)


@pytest.mark.parametrize("payload", ["[]", "null", "3", '"tuned"'])
def test_a_constants_file_that_is_not_an_object_is_a_value_error(tmp_path, payload):
    """The refusal has to be a `ValueError`, because that is the class all four readers catch.

    `json.loads` accepts a top-level array, `null`, a number and a string, so `_flatten`'s
    `raw.items()` raised `AttributeError` for them — not a `ValueError`, and therefore caught by
    none of the four: the lifespan's `except` clauses, so the process failed to start instead of
    degrading; the three routers' `_hyperparams` fallbacks, so the 503 was a 500; and
    `validate_hyperparams`, so the Data tab's Validate button answered 500 with no report line on
    the one surface whose whole job is to report. `importer/validate._read_json` already applies
    this check to `manifest.json` and `BUNDLE.json`. [M4.10 cycle 1, M410-R1-02 / M410-R1-03]
    """
    (tmp_path / "ledger_hyperparams.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        load(_Store(tmp_path))
    assert "must be a JSON object" in str(refused.value), str(refused.value)


def test_a_constants_path_that_is_not_a_file_is_not_read_as_an_absent_one(tmp_path):
    """Present and unopenable is not the same state as absent, and only one of them may default.

    The guard was `is_file()`, which is false for a path that exists as a directory — what a `tar`
    extraction or an out-of-band copy of an artifacts tree can leave behind. Both readers then
    treated the bundle as shipping no constants: DEFAULTS served under that note, and
    `validate_hyperparams` emitting neither a failure nor its "constants read" line. That is the
    silent substitution this milestone refuses by name, because DEFAULTS carry a different
    `hp_digest` and every cached fit in the install is discarded behind it.

    An `OSError` rather than a refusal of its own, because that is what `read_text` raises and what
    the lifespan, the three routers and the validator now all report. [M4.10 cycle 1, M410-R1-06]
    """
    (tmp_path / "ledger_hyperparams.json").mkdir()
    with pytest.raises(OSError):
        load(_Store(tmp_path))


def test_the_shipped_fixture_bundle_parses(tmp_path):
    """The fixture writes the file §4.3 describes; if the two drift apart, every M2 fit is
    running on defaults while the report claims otherwise."""
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    hp, notes = load(_Store(tmp_path / "b" / "artifacts"))
    assert hp.source == "bundle"
    assert hp.lambda_ridge == 3.0
    assert hp.tie_prior_delta0 == 0.22
    assert not [n for n in notes if "unknown" in n], f"fixture ships a key the app ignores: {notes}"


def test_the_fixture_ships_no_per_user_constants(tmp_path):
    """§4.3 is explicit that per-user cutpoints and per-arm sensitivities are not shipped."""
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    raw = json.loads(
        (tmp_path / "b" / "artifacts" / "ledger_hyperparams.json").read_text(encoding="utf-8")
    )
    assert not [k for k in raw if "cutpoint" in k.lower() or "sensitiv" in k.lower()]


def test_hyperparams_are_frozen():
    """A fit that mutates its own constants half way through is a fit nobody can reproduce."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        Hyperparams().lambda_ridge = 9.0


# --- the corpus's own spellings ---------------------------------------------------------------
#
# The fixture ships `ledger_hyperparams.json` under the corpus's names (M4.5). Before that, the
# fixture and this reader agreed on a spelling the artifact has never used, so every assertion
# below was true of a file no bundle contains.


def _shipped(tmp_path):
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    return json.loads(
        (tmp_path / "b" / "artifacts" / "ledger_hyperparams.json").read_text(encoding="utf-8")
    )


def test_the_corpus_spellings_reach_the_fields_they_tune(tmp_path):
    """Rule 1 of the module — "every constant comes from the bundle" — is what fails silently
    here: under this app's spellings the corpus's tuned λ_bt and learning rate both arrive as
    the defaults while `source` still reads "bundle"."""
    hp, _notes = from_mapping(_shipped(tmp_path))
    assert hp.lambda_ridge == 3.0          # anchor_ridge_lambda
    assert hp.lambda_bt == 0.3             # bt_weight_lam_bt — the default is 1.0
    assert hp.lr == 0.5                    # learning_rate — the default is 0.1
    assert hp.steps == 30                  # same name in both, and the default is 200
    assert hp.margin_form == "margin/mean(margin)"          # margin_weight_form, as prose
    assert hp.sigma_inflation_cap == "prior"                # sigma_inflation.cap, "prior_sigma"
    assert hp.sigma_inflation_grace_months == 12            # sigma_inflation.trigger_months


def test_the_prose_form_maps_only_to_itself():
    """The corpus states the margin form as a sentence. Mapping it by prefix would read any
    later form as this one, which is the §5.2 recipe changing without the fit noticing."""
    assert from_mapping({"margin_weight_form": "none"})[0].margin_form == "none"
    with pytest.raises(ValueError, match="margin_form"):
        from_mapping({"margin_weight_form": "w = sqrt(margin); 1.0 when disabled"})


def test_a_constant_the_corpus_tuned_and_this_app_cannot_use_is_named(tmp_path):
    """§4.3's gap, declared. `logit_clip`, `item_prior_shrink` and `user_offset_shrink_lam` are
    tuned upstream and have no term in this app's fit; reporting them as "unknown" would file
    them with typos, and dropping them is the silence rule 1 exists to prevent."""
    _hp, notes = from_mapping(_shipped(tmp_path))
    for key in ("logit_clip", "item_prior_shrink", "user_offset_shrink_lam"):
        assert any(key in n and "no term in this app" in n for n in notes), key
    assert not [n for n in notes if "unknown" in n], notes


def test_no_shipped_key_disappears_without_a_word(tmp_path):
    """The property behind all of the above: every key in the file either lands in a field or
    is accounted for in the notes. A key that does neither is a knob tuned into a void."""
    raw = _shipped(tmp_path)
    hp, notes = from_mapping(raw)
    from spielplan.ledger.hyperparams import CORPUS_NAMES

    for key, value in raw.items():
        leaves = [key] if not isinstance(value, dict) else [f"{key}.{k}" for k in value]
        for leaf in leaves:
            field = CORPUS_NAMES.get(leaf, leaf)
            landed = field in hp.__dataclass_fields__ and field != "source"
            assert landed or any(leaf in n for n in notes), leaf


def test_an_unmeasured_constant_falls_back_instead_of_refusing_the_bundle():
    """The shipped σ-inflation rate is JSON null — the corpus saying it has no measurement yet.
    Passing None through would trip the positivity check and refuse a merely incomplete
    bundle; §3.1 makes a documented default the answer, and the note says which."""
    hp, notes = from_mapping({"sigma_inflation": {"rate_c_per_sqrt_month": None}})
    assert hp.sigma_inflation_c == DEFAULTS.sigma_inflation_c
    assert any("unmeasured" in n for n in notes)


def test_the_provenance_string_is_not_read_as_a_constant(tmp_path):
    """`source` in the file names the script that tuned the numbers; `Hyperparams.source`
    records where this app read them from. Two different facts, one word."""
    hp, notes = from_mapping(_shipped(tmp_path))
    assert hp.source == "bundle"
    assert any("'source' is provenance" in n for n in notes)


# --- read once at boot, refused loudly ---------------------------------------------------------
#
# §4.3 makes `ledger_hyperparams.json` the single source of the §5.2 constants and §10 makes a
# bundle swap a restart, so nothing in a running process can change these numbers. Both halves of
# what went wrong follow from nobody acting on that: the lifespan never set `app.state.hyperparams`
# at all, so all three Ledger routers fell back to reading the file per request — a read, a
# `from_mapping` validation and a discarded note list on every tap, every board GET and every duel,
# with `load_cache` re-digesting the result afterwards; and `from_mapping`'s `ValueError` had no
# catcher between the file and the client, so one hand-edited or badly restored constant made the
# Rate and Rank surfaces 500 for everyone with nothing saying why. [M4.10 finding 10; ml06, perf-07]


@pytest.fixture
async def booted(db, pg_url, tmp_path, monkeypatch):
    """The real app over ASGI, with a bundle already active *before* the lifespan runs.

    `conftest.app` boots and then hands the test a client, which is one step too late for this
    property: the read under test happens inside the lifespan, so the staged files and the
    `artifact_bundle` row have to exist before it. The environment munging is conftest's and is
    here for conftest's reasons — `Settings` reads `.env` from the working directory, so a
    developer who followed the README would otherwise boot this test with their own connector
    configured. What is added is the staging.

    Yields `boot(mutate=None)`, where `mutate` is handed the built bundle root so a `break_*`
    helper from the fixture can be applied to it before it is staged — a bundle that was already
    broken when the operator imported it, which is the case §10's validate step misses today.
    """
    from spielplan.core.config import settings
    from tests.fixtures import make_bundle as fx

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    neutral = tmp_path / "no-dot-env"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
    settings.cache_clear()

    stack = contextlib.AsyncExitStack()

    async def boot(mutate=None, *, version: str = "hp-v1"):
        from spielplan.app import create_app

        root = tmp_path / "bundle"
        fx.make_bundle(root, version=version)
        if mutate is not None:
            mutate(root)
        shutil.copytree(root / "artifacts", tmp_path / "artifacts" / version)
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state) "
            "VALUES ($1, '{}'::jsonb, 'active')",
            version,
        )
        application = create_app()
        await stack.enter_async_context(application.router.lifespan_context(application))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        )
        stack.push_async_callback(client.aclose)
        return application, client

    try:
        yield boot
    finally:
        await stack.aclose()
        settings.cache_clear()


async def _admin(client) -> None:
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text


async def _titles(db, count: int = 8) -> None:
    """Owned films, written straight to the table: these tests are about what the surface reads
    from the *bundle*, and an import would take a minute to say the same thing."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned, overview) "
        "SELECT i, 'movie', 'Title ' || i, true, 'A film about ' || i "
        "FROM generate_series(1, $1) AS i",
        count,
    )


async def test_the_lifespan_reads_the_constants_once_and_the_rate_route_never_again(
    db, booted, monkeypatch
):
    """§4.3 + §10: one read per process, because a swap is a restart.

    The counter is on `hyperparams.load` rather than on the file, because the file read is only
    the cheapest third of what the per-request path did — `from_mapping` re-validates every
    constant and rebuilds the note list each time, and the caller then re-digests the result.
    """
    reads: list[object] = []
    real = hp_module.load

    def counting(store):
        reads.append(getattr(store, "version", None))
        return real(store)

    # Patched before the boot: the read under test is the lifespan's, and it is the one that must
    # happen exactly once.
    monkeypatch.setattr(hp_module, "load", counting)
    application, client = await booted()

    assert reads == ["hp-v1"], "the lifespan reads the active bundle's constants, once"
    cached = getattr(application.state, "hyperparams", None)
    assert cached is not None, "the boot must leave the constants on app.state"
    assert cached.source == "bundle"
    assert cached.lambda_bt == 0.3, "the fixture's tuned lambda_bt, not the 1.0 default"

    await _admin(client)
    await _titles(db)
    card = (await client.get("/api/rate")).json()["card"]
    first = await client.post(
        "/api/rate/verdict", json={"card_token": card["token"], "value": 2}
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/rate/verdict", json={"card_token": first.json()["card"]["token"], "value": 1}
    )
    assert second.status_code == 200, second.text

    assert await db.fetchval("SELECT count(*) FROM verdict") == 2, "two real taps, not two no-ops"
    assert reads == ["hp-v1"], f"the routes re-read the file {len(reads) - 1} more time(s)"


async def test_an_unusable_basis_does_not_cost_the_constants_their_one_read(
    db, booted, monkeypatch
):
    """The per-request read came back for a reason that has nothing to do with the constants.

    `BackboneError` is a `RuntimeError`, and the lifespan read the basis and the constants in ONE
    `try` whose first handler takes it — so an unusable `backbone.npz`, a state `app.py`'s own
    comment declares supported ("a Backbone that fails to load degrades the scoring surfaces rather
    than stopping a boot the admin needs in order to fix the bundle"), skipped the constants read
    entirely. `app.state.hyperparams` stayed unset and all three Ledger routers went back to
    reading, re-validating and re-digesting the file on every tap, every board GET and every duel,
    with §4.3's provenance notes never logged either. The ordering argument survives the split: the
    basis is still read first, so a bad constant cannot degrade scoring.

    `break_backbone_ids_unsorted` is one of several faults `Backbone.open` refuses and
    `importer/validate.py` does not check, so this is reachable from a bundle the importer accepts.
    [M4.10 cycle 1, M410-R1-04 / M410-C1-HP-1]
    """
    from tests.fixtures import make_bundle as fx

    reads: list[object] = []
    real = hp_module.load

    def counting(store):
        reads.append(getattr(store, "version", None))
        return real(store)

    monkeypatch.setattr(hp_module, "load", counting)
    application, client = await booted(fx.break_backbone_ids_unsorted)

    assert application.state.backbone.is_empty, (
        "the fixture's mutation did not make the basis unusable, so this test measured nothing"
    )
    assert reads == ["hp-v1"], "the lifespan never read the constants at all"
    cached = getattr(application.state, "hyperparams", None)
    assert cached is not None and cached.source == "bundle"
    assert cached.lambda_bt == 0.3, "the fixture's tuned lambda_bt, not the 1.0 default"

    await _admin(client)
    await _titles(db)
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    assert tap.status_code == 200, tap.text
    assert reads == ["hp-v1"], f"the routes re-read the file {len(reads) - 1} more time(s)"


async def test_a_constants_file_that_is_not_an_object_degrades_the_boot_rather_than_killing_it(
    db, booted, caplog
):
    """§3.1 keeps a half-configured boot legal, and that is what this file may cost at most.

    A top-level array or `null` raised `AttributeError` out of `from_mapping`, which neither of the
    lifespan's handlers catches, so uvicorn logged "Application startup failed" and exited: the
    container crash-loops and `/api/admin/*`, `/api/setup/*` and the Data tab — the only in-app
    route to importing a bundle that parses — are all unreachable. That is strictly worse than what
    the same file cost before this milestone read it at boot, and it contradicts the promise written
    three lines above the read. The shape check in `from_mapping` makes the refusal a `ValueError`,
    which is the class this clause was written for. [M4.10 cycle 1, M410-R1-02]
    """

    def not_an_object(root):
        (root / "artifacts" / "ledger_hyperparams.json").write_text("[]", encoding="utf-8")

    application, client = await booted(not_an_object)

    assert (await client.get("/api/health")).status_code == 200, "the boot is not refused"
    assert getattr(application.state, "hyperparams", None) is None, (
        "a file `from_mapping` refused must not be cached as if it had loaded"
    )
    assert "ledger_hyperparams.json" in caplog.text
    # And the routes refuse the way the designed refusal does, rather than 500-ing.
    await _admin(client)
    await _titles(db)
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    assert tap.status_code == 503, tap.text
    assert "ledger constants" in tap.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM verdict") == 0


async def test_a_constant_the_fit_cannot_use_is_a_503_with_a_reason_and_not_a_500(
    db, booted, caplog
):
    """§4.3's refusal has to reach the person, and it may not reach them as a substituted number.

    `fx.break_straddle_z` is §6.3's threshold at zero — `from_mapping` refuses it, and until now
    that `ValueError` travelled from a file read inside a request handler all the way out: every
    Rate write and every Rank board answered 500 to everyone in the household, with the key named
    in no message anybody could see. Defaulting instead would be worse than either: the defaults
    carry a different `hp_digest`, so every cached fit in the install would be discarded and
    re-fitted behind a constant nobody chose.

    Both surfaces that read the constants per request are asserted, one each way round. Rate goes
    over HTTP because the tap is the thing that must not write. Tonight's reader is called
    directly: reaching `_z` over HTTP needs a room, a started evening, a seat and a shortlist, and
    what is under test is one `except` clause. (`api/rank.py`'s copy of the same reader belongs to
    M4.10's Rank-route step and is asserted beside that change.)
    """
    from fastapi import HTTPException

    from spielplan.api import tonight as tonight_api
    from tests.fixtures import make_bundle as fx

    application, client = await booted(fx.break_straddle_z)

    assert getattr(application.state, "hyperparams", None) is None, (
        "a constant set `from_mapping` refused must not be cached as if it had loaded"
    )
    assert "straddle_z" in caplog.text, "the boot log has to name the key the operator must fix"

    await _admin(client)
    await _titles(db)

    # A real card, so this is a real tap being refused rather than a token the route would have
    # rejected anyway — and §4.2's journal stays empty, because the refusal precedes the write.
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    assert tap.status_code == 503, tap.text
    assert "ledger constants" in tap.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM verdict") == 0

    with pytest.raises(HTTPException) as refused:
        tonight_api._z(types.SimpleNamespace(app=application))
    assert refused.value.status_code == 503
    assert "ledger constants" in refused.value.detail
