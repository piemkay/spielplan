"""§5.2's constants come from the bundle (§4.3). A constant read and ignored, or a default presented as
measured, both fail quietly. The boot section needs TEST_DATABASE_URL."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil

import httpx
import pytest

from spielplan.ledger import hyperparams as hp_module
from spielplan.ledger.hyperparams import DEFAULTS, Hyperparams, from_mapping, load


class _Store:
    def __init__(self, root=None):
        self.root = root
        self.is_empty = root is None

    def path(self, name):
        return self.root / name


def test_the_spec_own_numbers_are_the_defaults():
    """§4.3 states two of them in prose; drift from the spec should show here."""
    assert DEFAULTS.lambda_ridge == 3.0        # "anchor (ridge) strength λ (currently 3.0)"
    assert DEFAULTS.tie_prior_delta0 == 0.22   # "tie-prior initialisation δ₀ = 0.22"
    assert DEFAULTS.source == "default"


def test_the_tie_prior_converts_to_davidsons_nu():
    """P(TIE | d=0) = ν/(2+ν), so ν = 2δ/(1−δ); 0.22 is the measured tie share (§4.2)."""
    assert DEFAULTS.nu0() == pytest.approx(2 * 0.22 / 0.78)
    tie_rate = DEFAULTS.nu0() / (2 + DEFAULTS.nu0())
    assert tie_rate == pytest.approx(0.22)


def test_the_decisive_toggle_carries_the_numbers_the_copy_promises():
    """§6.1: a "decisive switch ... sets the margin weight (~1.6 vs 1.0)" (decision 520)."""
    assert DEFAULTS.margin_for(decisive=True) == 1.6
    assert DEFAULTS.margin_for(decisive=False) == 1.0


def test_a_bundle_constant_replaces_the_default():
    hp, notes = from_mapping({"lambda_ridge": 7.5, "steps": 40})
    assert hp.lambda_ridge == 7.5
    assert hp.steps == 40
    assert hp.source == "bundle"
    assert any("omits" in n for n in notes), "a partial bundle should say what it left out"


def test_a_bundle_that_omits_everything_still_fits():
    hp, notes = from_mapping({})
    assert hp.lambda_ridge == DEFAULTS.lambda_ridge
    assert hp.source == "bundle"
    assert notes


def test_per_user_keys_are_ignored_and_reported():
    """Taking per-user cutpoints from a bundle would replace
    one household's fitted thresholds with another's."""
    hp, notes = from_mapping(
        {"lambda_ridge": 4.0, "cutpoints": [1, 2, 3], "per_user_sensitivity": 0.5}
    )
    assert hp.lambda_ridge == 4.0
    assert not hasattr(hp, "cutpoints")
    assert sum("ignored per-user key" in n for n in notes) == 2


def test_an_unknown_constant_is_reported_rather_than_dropped():
    """A tuned constant silently ignored is exactly the kind of thing that looks like it is working."""
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
        # A tolerance <= 0 declares every Newton solve converged, and `lr_min` 0 never ends the halving.
        ({"newton_tol": -5.0}, "positive number"),
        ({"newton_tol": 0}, "positive number"),
        ({"lr_min": -1.0}, "positive number"),
    ],
)
def test_a_nonsensical_constant_is_refused_at_the_boundary(payload, message):
    """Not clamped, not defaulted: a fit that substitutes 3.0 for λ = -1 hides the bug."""
    with pytest.raises(ValueError, match=message):
        from_mapping(payload)


def test_sigma_inflation_cap_accepts_the_spec_word_and_a_number():
    """§5.2's cap is the prior σ, which is per title, hence the sentinel."""
    assert from_mapping({"sigma_inflation_cap": "prior"})[0].sigma_inflation_cap == "prior"
    assert from_mapping({"sigma_inflation_cap": 2.5})[0].sigma_inflation_cap == 2.5


def test_the_digest_changes_with_any_constant_that_changes_a_fit():
    """`hp_digest` is a precondition: a fit cached under other constants is wrong, not stale."""
    base = DEFAULTS.digest()
    for field, value in (
        ("lambda_ridge", 4.0), ("lambda_bt", 2.0), ("steps", 5), ("lr", 0.2), ("tie_prior_delta0", 0.3),
        ("b_i_tau", 0.5), ("sigma_inflation_c", 0.1), ("sigma_inflation_cap", 3.0),
    ):
        import dataclasses

        assert dataclasses.replace(DEFAULTS, **{field: value}).digest() != base, field


def test_provenance_alone_does_not_invalidate_a_cache():
    """The same constants from a bundle or the defaults produce the same fit."""
    import dataclasses

    assert dataclasses.replace(DEFAULTS, source="bundle").digest() == DEFAULTS.digest()


def test_a_bundle_less_household_gets_defaults_and_is_told_so(tmp_path):
    """A household may rate before any bundle, but must not present defaults as measurements."""
    hp, notes = load(_Store(None))
    assert hp is DEFAULTS
    assert any("no artifact bundle" in n for n in notes)


def test_a_bundle_with_no_hyperparams_file_is_not_an_error(tmp_path):
    hp, notes = load(_Store(tmp_path))
    assert hp is DEFAULTS
    assert any("ships no ledger_hyperparams.json" in n for n in notes)


@pytest.mark.parametrize("payload", ["[]", "null", "3", '"tuned"'])
def test_a_constants_file_that_is_not_an_object_is_a_value_error(tmp_path, payload):
    """`ValueError` is the class all four readers catch; a top-level array raised `AttributeError`."""
    (tmp_path / "ledger_hyperparams.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        load(_Store(tmp_path))
    assert "must be a JSON object" in str(refused.value), str(refused.value)


def test_a_constants_path_that_is_not_a_file_is_not_read_as_an_absent_one(tmp_path):
    """A directory at the path is present-and-unopenable, not absent, so it must not default."""
    (tmp_path / "ledger_hyperparams.json").mkdir()
    with pytest.raises(OSError):
        load(_Store(tmp_path))


def test_the_shipped_fixture_bundle_parses(tmp_path):
    """If the fixture and the reader drift apart, every fit
    runs on defaults while the report claims otherwise."""
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    hp, notes = load(_Store(tmp_path / "b" / "artifacts"))
    assert hp.source == "bundle"
    assert hp.lambda_ridge == 3.0
    assert hp.tie_prior_delta0 == 0.22
    assert not [n for n in notes if "unknown" in n], f"fixture ships a key the app ignores: {notes}"


def test_the_fixture_ships_no_per_user_constants(tmp_path):
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


# The fixture ships the corpus's own names for each constant.


def _shipped(tmp_path):
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    return json.loads(
        (tmp_path / "b" / "artifacts" / "ledger_hyperparams.json").read_text(encoding="utf-8")
    )


def test_the_corpus_spellings_reach_the_fields_they_tune(tmp_path):
    """Under this app's spellings the corpus's λ_bt and learning rate would arrive as defaults."""
    hp, _notes = from_mapping(_shipped(tmp_path))
    assert hp.lambda_ridge == 3.0          # anchor_ridge_lambda
    assert hp.lambda_bt == 0.3             # bt_weight_lam_bt — the default is 1.0
    assert hp.lr == 0.5                    # learning_rate — the default is 0.1
    assert hp.steps == 30                  # same name in both, and the default is 200
    assert hp.sigma_inflation_cap == "prior"                # sigma_inflation.cap, "prior_sigma"
    assert hp.sigma_inflation_grace_months == 12            # sigma_inflation.trigger_months


def test_the_one_margin_form_the_fit_applies_is_accepted_and_any_other_refused():
    """§4.3 ships the flag and the form; the fit only knows margin/mean(margin), so another value
    must fail the bundle rather than be served the one form there is."""
    prose = "w = margin / mean(margin); 1.0 when disabled"
    shipped = {"margin_weighting": True, "margin_weight_form": prose}
    assert from_mapping(shipped)[0] == from_mapping({})[0]
    for refused in (
        {"margin_weighting": False},
        {"margin_weighting": 1},
        {"margin_weight_form": "none"},
        {"margin_weight_form": "w = sqrt(margin); 1.0 when disabled"},
    ):
        with pytest.raises(ValueError, match="margin"):
            from_mapping(refused)


def test_a_constant_the_corpus_tuned_and_this_app_cannot_use_is_named(tmp_path):
    """Tuned upstream with no term in this app's fit: named, neither "unknown" nor dropped."""
    _hp, notes = from_mapping(_shipped(tmp_path))
    for key in ("logit_clip", "item_prior_shrink", "user_offset_shrink_lam"):
        assert any(key in n and "no term in this app" in n for n in notes), key
    assert not [n for n in notes if "unknown" in n], notes


def test_no_shipped_key_disappears_without_a_word(tmp_path):
    """Every key lands in a field or is accounted for in the notes."""
    raw = _shipped(tmp_path)
    hp, notes = from_mapping(raw)
    from spielplan.ledger.hyperparams import CORPUS_NAMES

    for key, value in raw.items():
        leaves = [key] if not isinstance(value, dict) else [f"{key}.{k}" for k in value]
        for leaf in leaves:
            field = CORPUS_NAMES.get(leaf, leaf)
            landed = field in hp.__dataclass_fields__ and field != "source"
            # The margin flag and form are checked against the one form the fit applies.
            checked = leaf in ("margin_weighting", "margin_weight_form")
            assert landed or checked or any(leaf in n for n in notes), leaf


def test_an_unmeasured_constant_falls_back_instead_of_refusing_the_bundle():
    """JSON null means no measurement yet: a documented default, noted, rather than a refused bundle."""
    hp, notes = from_mapping({"bt_weight_lam_bt": None, "learning_rate": None})
    assert (hp.lambda_bt, hp.lr) == (DEFAULTS.lambda_bt, DEFAULTS.lr)
    assert sum("unmeasured" in n for n in notes) == 2


def test_the_provenance_string_is_not_read_as_a_constant(tmp_path):
    """`source` in the file names the tuning script; `Hyperparams.source` is where this app read them."""
    hp, notes = from_mapping(_shipped(tmp_path))
    assert hp.source == "bundle"
    assert any("'source' is provenance" in n for n in notes)


# The shipped file leaves the σ-inflation rate null with `provisional: true`: nobody has measured it.


def test_a_null_sigma_inflation_rate_disables_the_inflation_and_notes_it():
    """Rate null -> c = 0.0, and at c = 0 `model.inflate_sigma` returns σ unchanged."""
    import numpy as np

    from spielplan.ledger import model

    hp, notes = from_mapping({"sigma_inflation": {"rate_c_per_sqrt_month": None}})
    assert hp.sigma_inflation_c == 0.0, "a null rate must not become this app's own 0.05"
    loud = [n for n in notes if "SIGMA-INFLATION DISABLED" in n]
    assert loud, f"the disabling has to be loud, not a footnote: {notes}"
    assert "rate_c_per_sqrt_month" in notes[0], "the note must name the key the corpus must fix"

    sigma = np.array([0.31, 0.48])
    prior = np.array([0.90, 0.90])
    neglected = np.array([60.0, 360.0])          # five and thirty years untouched
    held = model.inflate_sigma(sigma, prior, neglected, hp)
    assert held == pytest.approx(sigma), "c = 0 must leave every σ exactly where the fit put it"
    # And the control: the default rate still inflates.
    grown = model.inflate_sigma(sigma, prior, neglected, DEFAULTS)
    assert grown[1] > sigma[1] and DEFAULTS.sigma_inflation_c == 0.05


def test_a_provisional_constant_disables_its_rule_rather_than_falling_back():
    """`provisional: true` disables the rule even beside
    a number; `false` means run at the shipped number."""
    hp, notes = from_mapping(
        {"sigma_inflation": {"rate_c_per_sqrt_month": 0.07, "provisional": True}}
    )
    assert hp.sigma_inflation_c == 0.0, "a provisional rate is not a rate"
    assert any("SIGMA-INFLATION DISABLED" in n for n in notes), notes
    assert any(hp_module.PROVISIONAL_KEY in n for n in notes), (
        "a flag that changes the fit may not be read silently"
    )

    measured, _notes = from_mapping(
        {"sigma_inflation": {"rate_c_per_sqrt_month": 0.07, "provisional": False}}
    )
    assert measured.sigma_inflation_c == 0.07

    # Refused, not coerced: the string "false" is truthy.
    with pytest.raises(ValueError, match="must be a boolean"):
        from_mapping({"sigma_inflation": {"provisional": "false"}})


def test_from_mapping_refuses_a_boolean_where_a_number_is_required():
    """`bool` is an `int` subclass, so `true` would arrive as 1.0 under `source = "bundle"`."""
    for payload, message in (
        ({"anchor_ridge_lambda": True}, "positive number"),
        ({"steps": True}, "positive integer"),
        ({"newton_max_iter": True}, "positive integer"),
        ({"margin_weight_form": True}, "margin_weight_form"),
        ({"newton_tol": True}, "positive number"),
        ({"lr_min": True}, "positive number"),
        ({"sigma_inflation_cap": True}, "sigma_inflation_cap"),
    ):
        with pytest.raises(ValueError, match=message):
            from_mapping(payload)


def test_the_digest_is_the_same_for_twelve_and_twelve_point_zero():
    """`json.dumps` writes `12` and `12.0` differently; one set of constants must have one digest."""
    for field, spellings in (
        ("steps", (12, 12.0)),
        ("newton_max_iter", (50, 50.0)),
        ("sigma_inflation_grace_months", (12, 12.0)),
    ):
        digests = {dataclasses.replace(DEFAULTS, **{field: v}).digest() for v in spellings}
        assert len(digests) == 1, f"{field}: int and float spellings produced {digests}"
    # And the digest still MOVES for a real change, or the above are satisfied by a constant function.
    assert dataclasses.replace(DEFAULTS, steps=13).digest() != DEFAULTS.digest()


# §10 makes a bundle swap a restart, so the constants are read once, into `app.state`.


@pytest.fixture
async def booted(db, pg_url, tmp_path, monkeypatch):
    """A bundle is active *before* the lifespan runs; `mutate` breaks the bundle before it is staged."""
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
    """Written straight to the table: these tests are about what the surface reads from the bundle.
    The admin's ladder is set up, so Rate serves a card."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned, overview) "
        "SELECT i, 'movie', 'Title ' || i, true, 'A film about ' || i "
        "FROM generate_series(1, $1) AS i",
        count,
    )
    await db.execute("INSERT INTO ladder_setup (user_id) SELECT id FROM app_user")


async def test_the_lifespan_reads_the_constants_once_and_the_rate_route_never_again(
    db, booted, monkeypatch
):
    """The counter is on `hyperparams.load`, which is also the validation and digest per request."""
    reads: list[object] = []
    real = hp_module.load

    def counting(store):
        reads.append(getattr(store, "version", None))
        return real(store)

    # Patched before the boot: the lifespan's read is the one that must happen once.
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
    first = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/rate/place", json={"card_token": first.json()["card"]["token"], "tier": 3}
    )
    assert second.status_code == 200, second.text

    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 2, "two real taps, not two no-ops"
    assert reads == ["hp-v1"], f"the routes re-read the file {len(reads) - 1} more time(s)"


async def test_an_unusable_basis_does_not_cost_the_constants_their_one_read(
    db, booted, monkeypatch
):
    """`BackboneError` is a `RuntimeError`; one shared `try` skipped the constants read on a bad basis."""
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
    tap = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})
    assert tap.status_code == 200, tap.text
    assert reads == ["hp-v1"], f"the routes re-read the file {len(reads) - 1} more time(s)"


async def test_a_constants_file_that_is_not_an_object_degrades_the_boot_rather_than_killing_it(
    db, booted, caplog
):
    """A top-level array must degrade the boot (§3.1), not kill it."""

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
    tap = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})
    assert tap.status_code == 503, tap.text
    assert "ledger constants" in tap.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0


async def test_a_constant_the_fit_cannot_use_is_a_503_with_a_reason_and_not_a_500(
    db, booted, caplog
):
    """Defaulting would change `hp_digest` and discard every
    cached fit, so the answer is a 503 with a reason."""
    from tests.fixtures import make_bundle as fx

    application, client = await booted(fx.break_straddle_z)

    assert getattr(application.state, "hyperparams", None) is None, (
        "a constant set `from_mapping` refused must not be cached as if it had loaded"
    )
    assert "straddle_z" in caplog.text, "the boot log has to name the key the operator must fix"

    await _admin(client)
    await _titles(db)

    # A real card, so a real tap is refused; the refusal precedes the write.
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})
    assert tap.status_code == 503, tap.text
    assert "ledger constants" in tap.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0
