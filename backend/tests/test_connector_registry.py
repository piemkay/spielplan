"""Connector config and the one time an env var may write it. Spec v2.1 §2, §6.6.

"Everything connector-related … is configured **in the admin UI** and stored in
`connector_config` — **not env vars**, because the owner explicitly wants connector setup in
the admin view; env vars may *seed* connector config **on first boot** for automated installs."

Two properties, and both are the kind that only fail on the second boot:

  * seeding never overwrites — otherwise every container restart silently reverts whatever the
    admin last saved, which is how people learn not to trust the admin UI;
  * a secret needs SECRETS_KEY — §2 says the app refuses rather than falling back, and a
    connector seeded without custody is worse than no connector.

This closes the M0 waiver on `platform-connector-config-env-seed-only`, which recorded that
nothing read `JELLYFIN_*` at boot at all.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.connectors import registry
from spielplan.core.config import Settings


def _settings(**overrides) -> Settings:
    """Env values to seed *from*. Custody comes from the process-wide `settings()`, which the
    `secrets_key` fixture provides — see `seed_from_env`'s note on why those are separate."""
    base = {"secrets_key": "test-secrets-key", "database_url": "postgresql://x/y"}
    return Settings(**{**base, **overrides})


# --- what env can offer -------------------------------------------------------------------


def test_no_env_means_no_seed():
    """"Nothing configured" has to stay distinguishable from "configured empty"."""
    assert registry.env_seeds(_settings()) == {}


def test_jellyfin_needs_both_a_url_and_a_key_to_be_seedable():
    assert registry.env_seeds(_settings(jellyfin_url="http://jf")) == {}
    assert registry.env_seeds(_settings(jellyfin_api_key="k")) == {}
    seeds = registry.env_seeds(_settings(jellyfin_url="http://jf/", jellyfin_api_key="k"))
    assert seeds["jellyfin"] == (
        {"url": "http://jf", "library_ids": []},
        {"api_key": "k", "user_tokens": {}},
    )


def test_the_other_connectors_seed_too():
    """§6.6 configures TMDB/OMDb/Trakt in the admin UI at M5; seeding lets an automated
    install arrive with them already filled in."""
    seeds = registry.env_seeds(
        _settings(tmdb_api_key="t", omdb_api_key="o", trakt_client_id="c",
                  trakt_client_secret="s")
    )
    assert set(seeds) == {"tmdb", "omdb", "trakt"}
    assert seeds["trakt"] == ({"client_id": "c"}, {"client_secret": "s"})


# --- first boot, and every boot after -----------------------------------------------------


async def test_the_first_boot_seeds_and_records_the_wizard_step(db, secrets_key):
    seeded = await registry.seed_from_env(
        db, _settings(jellyfin_url="http://jf", jellyfin_api_key="from-env")
    )
    assert seeded == ["jellyfin"]
    cfg = await registry.load_jellyfin(db)
    assert (cfg.url, cfg.api_key) == ("http://jf", "from-env")
    steps = {r["step"] for r in await db.fetch("SELECT step FROM setup_step")}
    assert "connectors" in steps


async def test_a_second_boot_leaves_the_admins_edit_alone(db, secrets_key):
    """The property that only fails on restart: env must not revert the admin UI."""
    cfg = _settings(jellyfin_url="http://jf", jellyfin_api_key="from-env")
    await registry.seed_from_env(db, cfg)
    await registry.save_jellyfin(db, url="http://edited-in-the-admin-ui", api_key="typed-by-hand")

    assert await registry.seed_from_env(db, cfg) == []
    stored = await registry.load_jellyfin(db)
    assert stored.url == "http://edited-in-the-admin-ui"
    assert stored.api_key == "typed-by-hand"


async def test_seeding_is_idempotent(db, secrets_key):
    cfg = _settings(jellyfin_url="http://jf", jellyfin_api_key="from-env")
    assert await registry.seed_from_env(db, cfg) == ["jellyfin"]
    assert await registry.seed_from_env(db, cfg) == []
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 1


async def test_a_secret_without_secrets_key_refuses_rather_than_falls_back(db, no_secrets_key):
    """§2: "The app refuses to start secret-dependent connectors without SECRETS_KEY rather
    than falling back to SESSION_SECRET"."""
    # `no_secrets_key` is what makes this a test rather than a coin flip: the refusal is checked
    # against the process-wide `settings()`, which reads `.env`, and this constructor's
    # arguments never reached it. See the fixture.
    cfg = Settings(
        _env_file=None,
        database_url="postgresql://x/y", session_secret="not-a-secrets-key",
        jellyfin_url="http://jf", jellyfin_api_key="k",
    )
    with pytest.raises(RuntimeError, match="SECRETS_KEY"):
        await registry.seed_from_env(db, cfg)
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0


# --- storage ------------------------------------------------------------------------------


async def test_a_stored_secret_is_ciphertext_carrying_its_key_id(db, secrets_key):
    """§2: connector secrets are AEAD-encrypted under a DEK and every ciphertext carries its
    key_id, so rotation can find what to re-wrap."""
    await registry.save_jellyfin(db, url="http://jf", api_key="a-real-key")
    row = await db.fetchrow(
        "SELECT config, secrets_encrypted, secrets_key_id FROM connector_config "
        "WHERE name = 'jellyfin'"
    )
    assert row["secrets_key_id"] is not None
    assert b"a-real-key" not in bytes(row["secrets_encrypted"])
    assert "a-real-key" not in str(row["config"]), "the key must not leak into plaintext config"


async def test_a_url_alone_does_not_require_secrets_key(db):
    """The admin types the address first and pastes the key second."""
    cfg = await registry.save_jellyfin(db, url="http://jf")
    assert cfg.url == "http://jf"
    assert cfg.configured is False
    assert await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = 'jellyfin'"
    ) is None


async def test_a_partial_save_keeps_the_secrets_it_did_not_send(db, secrets_key):
    """The form shows the key as a mask and posts it empty to mean "leave it alone". Same
    server, so the credentials stay — see the §14.3 tests below for the case where they do not.
    """
    await registry.save_jellyfin(db, url="http://jf", api_key="k", user_tokens={"1": "tok"})
    await registry.save_jellyfin(db, url="http://jf/library")
    cfg = await registry.load_jellyfin(db)
    assert cfg.user_tokens == {"1": "tok"}
    assert cfg.api_key == "k"
    assert cfg.url == "http://jf/library"


async def test_tokens_are_addressed_by_app_user_id(db, secrets_key):
    """JSON object keys are strings; round-tripping them as ints invites a silent mismatch
    between "3" and 3, which would look exactly like a missing token."""
    await registry.save_jellyfin(db, url="http://jf", api_key="k", user_tokens={"3": "tok"})
    cfg = await registry.load_jellyfin(db)
    assert cfg.token_for(3) == "tok"
    assert cfg.token_for(4) is None


async def test_an_unconfigured_connector_builds_no_client(db):
    assert registry.make_client(registry.JellyfinConfig()) is None
    assert registry.make_client(registry.JellyfinConfig(url="http://jf")) is None
    assert registry.make_client(registry.JellyfinConfig(url="http://jf", api_key="k")) is not None


# --- §14.3: credentials do not follow the connector to a new server ------------------------


async def test_moving_the_server_drops_the_credentials_bound_to_it(db, secrets_key):
    """The API key is admin-equivalent on the whole media server and the per-user tokens are
    real credentials. Both are bound to the server that issued them, so re-pointing the URL
    must not carry them along — every later request would send them, in a header, to whatever
    host was just typed in."""
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={"1": "tok"}
    )
    moved = await registry.save_jellyfin(db, url="http://elsewhere.example:8096")

    assert moved.url == "http://elsewhere.example:8096"
    assert moved.api_key == ""
    assert moved.user_tokens == {}
    assert moved.configured is False, "the admin has to enter a key for the new server"


async def test_a_same_origin_edit_keeps_them(db, secrets_key):
    """A trailing slash, or a path, is not a different server — and losing the key over one
    would teach the admin to distrust the form."""
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={"1": "tok"}
    )
    same = await registry.save_jellyfin(db, url="http://jellyfin.local:8096/")
    assert same.api_key == "k"
    assert same.user_tokens == {"1": "tok"}


async def test_the_port_is_part_of_the_origin(db, secrets_key):
    await registry.save_jellyfin(db, url="http://jellyfin.local:8096", api_key="k")
    moved = await registry.save_jellyfin(db, url="http://jellyfin.local:9096")
    assert moved.api_key == ""
