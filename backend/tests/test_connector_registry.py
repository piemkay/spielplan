"""Connector config and the one time an env var may write it (§2, §6.6): seeding never overwrites an
admin's edit, and a secret needs SECRETS_KEY. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import asyncpg
import pytest

from spielplan.connectors import registry
from spielplan.core.config import Settings


async def _link_state(conn, user_id: int) -> str | None:
    return await conn.fetchval("SELECT jellyfin_link_state FROM app_user WHERE id = $1", user_id)


async def _second_connection(pg_url):
    """A lock is only a lock across two connections; the json codecs mirror the `db` fixture's."""
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


async def _link(conn, app_user_id: str, token: str, *, for_update: bool) -> None:
    """The sleep widens the window to a certainty; `save_jellyfin` merges again internally."""
    async with conn.transaction():
        cfg = await registry.load_jellyfin(conn, for_update=for_update)
        tokens = dict(cfg.user_tokens)
        await asyncio.sleep(0.2)
        tokens[app_user_id] = token
        await registry.save_jellyfin(conn, user_tokens=tokens)


def _settings(**overrides) -> Settings:
    """Custody comes from the process-wide `settings()` via the `secrets_key` fixture."""
    base = {"secrets_key": "test-secrets-key-not-a-real-one-at-all", "database_url": "postgresql://x/y"}
    return Settings(**{**base, **overrides})


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
    seeds = registry.env_seeds(
        _settings(tmdb_api_key="t", omdb_api_key="o", trakt_client_id="c",
                  trakt_client_secret="s")
    )
    assert set(seeds) == {"tmdb", "omdb", "trakt"}
    assert seeds["trakt"] == ({"client_id": "c"}, {"client_secret": "s"})


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
    # `no_secrets_key` makes this deterministic: the refusal reads the process-wide `settings()`.
    cfg = Settings(
        _env_file=None,
        database_url="postgresql://x/y", session_secret="not-a-secrets-key-and-not-a-real-one",
        jellyfin_url="http://jf", jellyfin_api_key="k",
    )
    with pytest.raises(RuntimeError, match="SECRETS_KEY"):
        await registry.seed_from_env(db, cfg)
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0


async def test_a_stored_secret_is_ciphertext_carrying_its_key_id(db, secrets_key):
    """Every ciphertext carries its key_id, so rotation can find what to re-wrap."""
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
    """The form posts the masked key empty to mean "leave it alone"."""
    await registry.save_jellyfin(db, url="http://jf", api_key="k", user_tokens={"1": "tok"})
    await registry.save_jellyfin(db, url="http://jf/library")
    cfg = await registry.load_jellyfin(db)
    assert cfg.user_tokens == {"1": "tok"}
    assert cfg.api_key == "k"
    assert cfg.url == "http://jf/library"


async def test_tokens_are_addressed_by_app_user_id(db, secrets_key):
    """JSON object keys are strings: "3" and 3 must not silently mismatch."""
    await registry.save_jellyfin(db, url="http://jf", api_key="k", user_tokens={"3": "tok"})
    cfg = await registry.load_jellyfin(db)
    assert cfg.token_for(3) == "tok"
    assert cfg.token_for(4) is None


async def test_the_probed_version_is_stored_beside_the_url_not_in_the_secret(db, secrets_key):
    """The verdict is config, not a credential: it needs no SECRETS_KEY."""
    await registry.save_jellyfin(
        db, url="http://jf", api_key="k", server_version="10.8.13", server_supported=False
    )
    cfg = await registry.load_jellyfin(db)
    assert (cfg.server_version, cfg.server_supported) == ("10.8.13", False)
    stored = await db.fetchval("SELECT config FROM connector_config WHERE name = 'jellyfin'")
    assert "10.8.13" in str(stored), "the verdict belongs in the plaintext config half"
    assert "10.8.13" in registry.make_client(cfg).played_write_refusal()

    # The mask-and-resave the admin form does on every URL edit must not blank it.
    await registry.save_jellyfin(db, url="http://jf/library")
    kept = await registry.load_jellyfin(db)
    assert (kept.server_version, kept.server_supported) == ("10.8.13", False)

    upgraded = await registry.save_jellyfin(db, server_version="10.10.3", server_supported=True)
    assert (upgraded.server_version, upgraded.server_supported) == ("10.10.3", True)
    assert registry.make_client(await registry.load_jellyfin(db)).played_write_refusal() is None


async def test_no_connector_credential_survives_a_repr(db, secrets_key):
    """A default dataclass repr copies credentials into any traceback that holds the object."""
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="ADMIN-EQUIVALENT-KEY",
        user_tokens={"1": "PER-USER-TOKEN"},
    )
    cfg = await registry.load_jellyfin(db)
    assert cfg.api_key == "ADMIN-EQUIVALENT-KEY", "held, and usable -- just not printed"

    printed = repr(cfg)
    assert "ADMIN-EQUIVALENT-KEY" not in printed
    assert "PER-USER-TOKEN" not in printed
    assert "jellyfin.local" in printed, "the URL is what makes such a line worth printing"

    client = repr(registry.make_client(cfg))
    assert "ADMIN-EQUIVALENT-KEY" not in client
    assert "jellyfin.local" in client


async def test_an_unconfigured_connector_builds_no_client(db):
    assert registry.make_client(registry.JellyfinConfig()) is None
    assert registry.make_client(registry.JellyfinConfig(url="http://jf")) is None
    assert registry.make_client(registry.JellyfinConfig(url="http://jf", api_key="k")) is not None


async def test_a_for_update_load_locks_the_connector_row(db, pg_url, secrets_key):
    """`lock_timeout` turns a dropped `FOR UPDATE` into a red test rather than a hang."""
    await registry.save_jellyfin(db, url="http://jf", api_key="k")
    other = await _second_connection(pg_url)
    take_it = "SELECT name FROM connector_config WHERE name = 'jellyfin' FOR UPDATE"
    try:
        await other.execute("SET lock_timeout = '300ms'")

        async with db.transaction():
            await registry.load_jellyfin(db)
            async with other.transaction():
                await other.execute(take_it)

        async with db.transaction():
            await registry.load_jellyfin(db, for_update=True)
            with pytest.raises(asyncpg.PostgresError) as exc:
                async with other.transaction():
                    await other.execute(take_it)
            assert "lock" in str(exc.value).lower()

        # And released with the transaction, not carried back into the pool.
        async with other.transaction():
            await other.execute(take_it)
    finally:
        await other.close()


async def test_two_links_at_once_keep_both_tokens(db, pg_url, secrets_key):
    """The sealed map merges in Python; without the lock exactly one of two tokens survives."""
    await registry.save_jellyfin(db, url="http://jf", api_key="k")
    one, two = await _second_connection(pg_url), await _second_connection(pg_url)
    try:
        await asyncio.gather(
            _link(one, "1", "tok-1", for_update=True),
            _link(two, "2", "tok-2", for_update=True),
        )
    finally:
        await one.close()
        await two.close()

    assert (await registry.load_jellyfin(db)).user_tokens == {"1": "tok-1", "2": "tok-2"}


async def test_moving_the_server_drops_the_credentials_bound_to_it(db, secrets_key):
    """Credentials are bound to the server that issued them; re-pointing must not send them elsewhere."""
    linked = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', 'jf-user-patrick', 'linked') RETURNING id"
    )
    never_linked = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={"1": "tok"},
        server_version="10.10.3", server_supported=True,
    )
    moved = await registry.save_jellyfin(db, url="http://elsewhere.example:8096")

    assert moved.url == "http://elsewhere.example:8096"
    assert moved.api_key == ""
    assert moved.user_tokens == {}
    assert moved.configured is False, "the admin has to enter a key for the new server"
    assert (moved.server_version, moved.server_supported) == ("", None)
    assert await _link_state(db, linked) == "needs_relink"
    assert await _link_state(db, never_linked) is None, "0006's CHECK pairs the id and the state"


async def test_moving_the_server_de_links_the_accounts_even_when_the_secret_will_not_open(
    db, secrets_key, monkeypatch
):
    """Under an unreadable SECRETS_KEY the token map loads empty, yet a move must still de-link accounts."""
    from spielplan.core.config import settings

    linked = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', 'jf-user-patrick', 'linked') RETURNING id"
    )
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={str(linked): "tok"}
    )

    # The .env came back wrong, or did not come back at all.
    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    unreadable = await registry.load_jellyfin(db)
    assert (unreadable.secrets_unreadable, unreadable.user_tokens) == (True, {})

    moved = await registry.save_jellyfin(db, url="http://elsewhere.example:8096")

    assert moved.api_key == "" and moved.user_tokens == {}
    assert await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = 'jellyfin'"
    ) is None, "the credentials really were destroyed -- so the badge must say so"
    assert await _link_state(db, linked) == "needs_relink", (
        "the account reads 'linked' with no credential anywhere and nothing else will correct it"
    )


async def test_a_same_origin_edit_keeps_them(db, secrets_key):
    """A trailing slash or a path is not a different server."""
    linked = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', 'jf-user-patrick', 'linked') RETURNING id"
    )
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={"1": "tok"}
    )
    same = await registry.save_jellyfin(db, url="http://jellyfin.local:8096/")
    assert same.api_key == "k"
    assert same.user_tokens == {"1": "tok"}
    assert await _link_state(db, linked) == "linked"


async def test_the_port_is_part_of_the_origin(db, secrets_key):
    await registry.save_jellyfin(db, url="http://jellyfin.local:8096", api_key="k")
    moved = await registry.save_jellyfin(db, url="http://jellyfin.local:9096")
    assert moved.api_key == ""


async def test_the_webhook_token_is_minted_at_the_first_save_and_never_rotated(db, secrets_key):
    """Never rotated: the operator pasted it into the Webhook plugin and it is shown only once."""
    typed = await registry.save_jellyfin(
        db, url="http://jf", api_key="k", mint_webhook_token=True
    )
    assert len(typed.webhook_token) >= 32
    assert (await registry.load_jellyfin(db)).webhook_token == typed.webhook_token

    edited = await registry.save_jellyfin(db, url="http://jf/library", mint_webhook_token=True)
    assert edited.webhook_token == typed.webhook_token, "a URL edit must not rotate it"
    probed = await registry.save_jellyfin(db, server_version="10.10.3", server_supported=True)
    assert probed.webhook_token == typed.webhook_token, "nor may the sweep's own probe save"

    row = await db.fetchrow(
        "SELECT config, secrets_encrypted FROM connector_config WHERE name = 'jellyfin'"
    )
    assert typed.webhook_token not in str(row["config"]), "it belongs in the sealed half"
    assert typed.webhook_token.encode() not in bytes(row["secrets_encrypted"])


async def test_a_save_no_admin_performed_does_not_mint_the_token_no_admin_would_see(
    db, secrets_key
):
    """Decision 416: a background save may neither mint nor destroy the token."""
    seeded = await registry.save_jellyfin(db, url="http://jf", api_key="k")
    assert seeded.configured and seeded.webhook_token == "", "the state an upgrade arrives in"

    for background in (
        await registry.save_jellyfin(db, delta_watermark=datetime(2026, 3, 4, tzinfo=UTC)),
        await registry.save_jellyfin(db, server_version="10.10.3", server_supported=True),
        await registry.save_jellyfin(db, user_tokens={"1": "utok"}),
    ):
        assert background.webhook_token == "", "a save nobody is watching may not mint one"
    assert (await registry.load_jellyfin(db)).webhook_token == ""

    minted = await registry.save_jellyfin(db, url="http://jf", mint_webhook_token=True)
    assert minted.webhook_token, "the admin's own save is what mints it"
    kept = await registry.save_jellyfin(db, delta_watermark=datetime(2026, 3, 5, tzinfo=UTC))
    assert kept.webhook_token == minted.webhook_token, "and a background save carries it forward"


async def test_two_first_saves_at_once_are_told_the_same_token(db, pg_url, secrets_key):
    """`for_update` locks nothing before the row exists, so the first save takes the row first."""
    one, two = await _second_connection(pg_url), await _second_connection(pg_url)
    try:
        first, second = await asyncio.gather(
            registry.save_jellyfin(
                one, url="http://jf", api_key="k-one", mint_webhook_token=True
            ),
            registry.save_jellyfin(
                two, url="http://jf", api_key="k-two", mint_webhook_token=True
            ),
        )
    finally:
        await one.close()
        await two.close()

    stored = await registry.load_jellyfin(db)
    assert stored.webhook_token, "one of them minted"
    assert {first.webhook_token, second.webhook_token} == {stored.webhook_token}, (
        "an admin may not be shown a token the next save overwrote"
    )


async def test_the_webhook_token_survives_the_move_that_drops_the_credentials(db, secrets_key):
    """The webhook token never leaves this install, so a server move keeps it."""
    first = await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={"1": "tok"},
        mint_webhook_token=True,
    )
    moved = await registry.save_jellyfin(db, url="http://elsewhere.example:8096")

    assert (moved.api_key, moved.user_tokens) == ("", {})
    assert moved.webhook_token == first.webhook_token
    assert (await registry.load_jellyfin(db)).webhook_token == first.webhook_token


async def test_the_library_pick_survives_the_move_that_drops_the_credentials(db, secrets_key):
    """The pick is the admin's intent and travels nowhere, so a move keeps it."""
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", library_ids=["jf-lib-films"]
    )
    moved = await registry.save_jellyfin(db, url="https://jellyfin.local:8920")

    assert (moved.api_key, moved.server_version, moved.delta_watermark) == ("", "", None)
    assert moved.library_ids == ["jf-lib-films"], "a corrected address is not a new pick"
    assert (await registry.load_jellyfin(db)).library_ids == ["jf-lib-films"]


async def test_an_install_that_has_minted_no_token_matches_nothing_a_caller_can_send(
    db, secrets_key
):
    """With no token minted, a plain equality would admit every caller that sends nothing."""
    await registry.seed_from_env(db, _settings(jellyfin_url="http://jf", jellyfin_api_key="k"))
    seeded = await registry.load_jellyfin(db)
    assert seeded.configured and seeded.webhook_token == ""
    assert seeded.webhook_token_matches("") is False
    assert seeded.webhook_token_matches(None) is False

    minted = await registry.save_jellyfin(db, url="http://jf", mint_webhook_token=True)
    assert minted.webhook_token_matches(minted.webhook_token) is True
    assert minted.webhook_token_matches(minted.webhook_token + "x") is False
    assert minted.webhook_token_matches("") is False
    # Over bytes: Starlette decodes headers as latin-1, and `hmac.compare_digest` raises on non-ASCII `str`.
    assert minted.webhook_token_matches("\xff") is False
    assert minted.webhook_token_matches(minted.webhook_token + "\u00e9") is False


async def test_the_webhook_token_does_not_survive_a_repr(db, secrets_key):
    """The static sweep matches only `api_key`, `token` and
    `user_tokens`, so this credential is named here."""
    cfg = await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", mint_webhook_token=True
    )
    assert cfg.webhook_token, "there is something to print"
    assert cfg.webhook_token not in repr(cfg)
    assert "jellyfin.local" in repr(cfg), "the URL is what makes such a line worth printing"


async def test_the_delta_poll_starts_at_this_installs_own_creation_instant(db, secrets_key):
    """Never epoch: the floor is 0025's `applied_at`, and not the install's when the install is older."""
    gained = await db.fetchval(
        "SELECT applied_at FROM schema_migration WHERE version = '0025_jellyfin_intake'"
    )
    fresh = await registry.save_jellyfin(db, url="http://jf", api_key="k")
    assert fresh.delta_watermark is None, "nothing has polled yet"
    assert await registry.delta_since(db, fresh) == gained
    assert gained.year > 2000, "an epoch floor is what this test exists to refuse"

    await db.execute(
        "UPDATE schema_migration SET applied_at = applied_at - interval '180 days'"
        " WHERE version < '0025'"
    )
    assert await registry.delta_since(db, fresh) == gained, (
        "an upgraded install's first poll read from the day it was installed"
    )

    polled = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    stored = await registry.save_jellyfin(db, delta_watermark=polled)
    assert stored.delta_watermark == polled
    reloaded = await registry.load_jellyfin(db)
    assert reloaded.delta_watermark == polled, "jsonb holds no timestamp; it round-trips as text"
    assert await registry.delta_since(db, reloaded) == polled
    assert "2026-03-04" in str(
        await db.fetchval("SELECT config FROM connector_config WHERE name = 'jellyfin'")
    ), "the watermark is config, not a secret"


async def test_a_url_edit_keeps_the_watermark_and_a_move_drops_it(db, secrets_key):
    """A watermark belongs to the clock that stamped it; carried to another server it would skip adds."""
    polled = datetime(2026, 3, 4, tzinfo=UTC)
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", delta_watermark=polled
    )
    same = await registry.save_jellyfin(db, url="http://jellyfin.local:8096/")
    assert same.delta_watermark == polled, "a trailing slash is not a different server"

    moved = await registry.save_jellyfin(db, url="http://elsewhere.example:8096")
    assert moved.delta_watermark is None
    assert await registry.delta_since(db, moved) == await db.fetchval(
        "SELECT applied_at FROM schema_migration WHERE version = '0025_jellyfin_intake'"
    )


async def test_a_custody_failure_does_not_erase_the_library_pick_or_the_watermark(
    db, secrets_key, monkeypatch
):
    """A custody failure must not erase the plaintext settings on the next address save."""
    from spielplan.core.config import settings

    polled = datetime(2026, 3, 4, tzinfo=UTC)
    await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k",
        library_ids=["jf-lib-films"], delta_watermark=polled,
    )

    # The .env came back wrong, or did not come back at all.
    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    degraded = await registry.load_jellyfin(db)
    assert (degraded.secrets_unreadable, degraded.api_key) == (True, "")
    assert degraded.library_ids == ["jf-lib-films"], "the pick is plaintext config, not a secret"
    assert degraded.delta_watermark == polled

    await registry.save_jellyfin(db, url="http://jellyfin.local:8096/")
    kept = await registry.load_jellyfin(db)
    assert kept.library_ids == ["jf-lib-films"], "a URL correction must not widen the boundary"
    assert kept.delta_watermark == polled



async def test_a_provider_round_trips_with_its_key_sealed_and_its_model_in_plaintext(
    db, secrets_key
):
    """The masked save keeps the key when only the model changes."""
    state = await registry.save_connector(
        db, "gemini", api_key="GEMINI-KEY-NOT-REAL", model="gemini-3.6-flash"
    )
    assert state == registry.ConnectorState(
        name="gemini", config={"model": "gemini-3.6-flash"},
        secrets={"api_key": "GEMINI-KEY-NOT-REAL"},
    )
    row = await db.fetchrow(
        "SELECT config, secrets_encrypted, secrets_key_id FROM connector_config WHERE name = 'gemini'"
    )
    assert row["secrets_key_id"] is not None
    assert b"GEMINI-KEY-NOT-REAL" not in bytes(row["secrets_encrypted"])
    assert row["config"] == {"model": "gemini-3.6-flash"}, "the key must not leak into plaintext"

    masked = await registry.save_connector(db, "gemini", api_key="", model="gemini-3.7-flash")
    assert masked.secrets == {"api_key": "GEMINI-KEY-NOT-REAL"}
    assert masked.config == {"model": "gemini-3.7-flash"}
    assert await registry.load_connector(db, "gemini") == masked


async def test_a_settings_save_needs_no_secrets_key_and_a_key_save_refuses_without_one(
    db, no_secrets_key
):
    """A settings save needs no SECRETS_KEY; a key save refuses without one and writes nothing."""
    chosen = await registry.save_connector(db, "openai", model="gpt-5.6-terra")
    assert chosen.config == {"model": "gpt-5.6-terra"} and chosen.secrets == {}
    assert await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = 'openai'"
    ) is None

    await registry.save_connector(db, "llm", cap_usd=25.0, extraction_provider="openai")
    merged = await registry.save_connector(db, "llm", passes=2, cap_usd=None)
    assert merged.config == {"cap_usd": 25.0, "extraction_provider": "openai", "passes": 2}, (
        "a value of None keeps what is stored"
    )

    with pytest.raises(RuntimeError, match="SECRETS_KEY"):
        await registry.save_connector(db, "anthropic", api_key="would-be-stored-in-the-clear")
    assert await db.fetchval(
        "SELECT count(*) FROM connector_config WHERE name = 'anthropic'"
    ) == 0, "a refused save writes nothing, not even the row it would have locked"


async def test_a_save_names_the_fields_a_connector_declares(db):
    """A misspelt `apikey` would land in the plaintext half while the provider answered 401."""
    with pytest.raises(ValueError, match="api_key") as refused:
        await registry.save_connector(db, "gemini", apikey="KEY-IN-THE-WRONG-PLACE")
    assert "apikey" in str(refused.value)
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0

    with pytest.raises(LookupError, match="gemini"):
        registry.fields_of("gemeni")
    with pytest.raises(LookupError, match="trakt"):
        await registry.load_connector(db, "plex")


async def test_an_unreadable_provider_secret_degrades_and_only_a_typed_key_retires_it(
    db, secrets_key, monkeypatch, caplog
):
    """A save that types no key writes only the config half, so an unreadable ciphertext survives."""
    from spielplan.core.config import settings

    await registry.save_connector(db, "anthropic", api_key="OLD-KEY-NOT-REAL", model="claude-a")
    sealed = await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = 'anthropic'"
    )

    # The .env came back wrong, or did not come back at all.
    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    caplog.set_level(logging.ERROR, logger="spielplan.connectors")
    degraded = await registry.load_connector(db, "anthropic")
    assert (degraded.secrets_unreadable, degraded.secrets) == (True, {})
    assert degraded.config == {"model": "claude-a"}, "the plaintext half opened perfectly"
    assert any(
        r.levelno == logging.ERROR and "anthropic" in r.getMessage() for r in caplog.records
    )

    kept = await registry.save_connector(db, "anthropic", api_key="", model="claude-b")
    assert kept.secrets_unreadable is True
    assert bytes(await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = 'anthropic'"
    )) == bytes(sealed), "a settings save must not erase the ciphertext it could not read"
    assert (await registry.load_connector(db, "anthropic")).config == {"model": "claude-b"}

    resealed = await registry.save_connector(db, "anthropic", api_key="NEW-KEY-NOT-REAL")
    assert (resealed.secrets, resealed.secrets_unreadable) == ({"api_key": "NEW-KEY-NOT-REAL"}, False)
    reloaded = await registry.load_connector(db, "anthropic")
    assert reloaded.secrets == {"api_key": "NEW-KEY-NOT-REAL"}
    assert reloaded.config == {"model": "claude-b"}


async def test_no_connector_state_prints_its_secrets(db, secrets_key):
    """A default repr copies a provider key into any traceback or `%r` log line."""
    state = await registry.save_connector(
        db, "openai", api_key="BILLABLE-KEY-NOT-REAL", model="gpt-5.6-terra"
    )
    assert state.secrets["api_key"] == "BILLABLE-KEY-NOT-REAL", "held, and usable"
    printed = repr(state)
    assert "BILLABLE-KEY-NOT-REAL" not in printed
    assert "openai" in printed and "gpt-5.6-terra" in printed, "what makes the line worth printing"


def test_the_llm_providers_seed_their_key_and_the_llm_settings_seed_nothing():
    """Each provider's key and nothing else; no cap is seeded because decision 325 ships none."""
    assert registry.env_seeds(_settings(gemini_api_key="g")) == {"gemini": ({}, {"api_key": "g"})}
    seeds = registry.env_seeds(
        _settings(gemini_api_key="g", anthropic_api_key="a", openai_api_key="o")
    )
    assert seeds == {
        "gemini": ({}, {"api_key": "g"}),
        "anthropic": ({}, {"api_key": "a"}),
        "openai": ({}, {"api_key": "o"}),
    }
    assert "llm" not in seeds


async def test_the_three_providers_seed_on_first_boot_and_never_over_an_admins_edit(
    db, secrets_key
):
    cfg = _settings(gemini_api_key="g-env", anthropic_api_key="a-env", openai_api_key="o-env")
    assert await registry.seed_from_env(db, cfg) == ["gemini", "anthropic", "openai"]
    for name, key in (("gemini", "g-env"), ("anthropic", "a-env"), ("openai", "o-env")):
        assert (await registry.load_connector(db, name)).secrets == {"api_key": key}
    row = await db.fetchrow(
        "SELECT secrets_encrypted, secrets_key_id FROM connector_config WHERE name = 'gemini'"
    )
    assert row["secrets_key_id"] is not None and b"g-env" not in bytes(row["secrets_encrypted"])

    await registry.save_connector(db, "gemini", api_key="typed-by-hand", model="gemini-3.6-flash")
    assert await registry.seed_from_env(db, cfg) == []
    edited = await registry.load_connector(db, "gemini")
    assert edited.secrets == {"api_key": "typed-by-hand"}
    assert edited.config == {"model": "gemini-3.6-flash"}


def test_every_connector_the_registry_calls_seeded_is_one_env_can_seed():
    """Found through the registry's own names, so a connector added to one side only fails."""
    from spielplan.core.config import Settings

    connectors = {*registry.FIELDS, registry.JELLYFIN}
    prefixes = tuple(f"{name}_" for name in connectors)
    fields = [name for name in Settings.model_fields if name.startswith(prefixes)]
    assert {"gemini_api_key", "anthropic_api_key", "openai_api_key"} <= set(fields)
    every = _settings(**{name: f"http://{name}.example" for name in fields})
    assert set(registry.env_seeds(every)) == connectors - {"llm"}, "the llm settings seed nothing"


async def test_the_test_dispatch_is_one_table_and_refuses_a_connector_with_no_test(monkeypatch):
    """Jellyfin has no test in the table on purpose: its test stores §7.1's verdict."""
    conn = object()
    called: list[object] = []

    async def probe(given):
        called.append(given)
        return {"ok": True, "detail": "stub"}

    monkeypatch.setitem(registry.TESTS, "tmdb", probe)
    assert await registry.test_connector(conn, "tmdb") == {"ok": True, "detail": "stub"}
    assert called == [conn]

    with pytest.raises(LookupError, match="connector llm has no test in this build"):
        await registry.test_connector(conn, "llm")
    with pytest.raises(LookupError, match="jellyfin"):
        await registry.test_connector(conn, "jellyfin")


async def test_a_provider_probe_refuses_before_any_request_when_it_holds_no_usable_key(
    db, secrets_key, monkeypatch
):
    """Neither state reaches `spielplan.llm`, which is only imported once there is a key to send."""
    from spielplan.core.config import settings

    assert await registry.test_connector(db, "gemini") == {
        "ok": False, "error": "no API key is configured for gemini",
    }
    await registry.save_connector(db, "gemini", api_key="KEY-NOT-REAL")
    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    assert await registry.test_connector(db, "gemini") == {
        "ok": False, "error": registry.SECRETS_UNREADABLE_REASON,
    }


async def test_an_unset_removes_a_declared_setting_under_the_row_lock_and_nothing_else(db, secrets_key):
    """In the partial merge None means KEEP, so removal is named explicitly and done under the row lock."""
    await registry.save_connector(db, "llm", extraction_provider="gemini", passes=2, cap_usd=25)
    await registry.save_connector(db, "gemini", api_key="KEY-NOT-REAL", model="gemini-3.6-flash",
                                  price_input=1, price_output=4)

    state = await registry.save_connector(db, "llm", unset=("extraction_provider",), parallel=False)
    assert state.config == {"passes": 2, "cap_usd": 25, "parallel": False}
    assert (await registry.load_connector(db, "llm")).config == state.config

    await registry.save_connector(db, "gemini", unset=("model", "price_input", "price_output"))
    gemini = await registry.load_connector(db, "gemini")
    assert (gemini.config, gemini.secrets) == ({}, {"api_key": "KEY-NOT-REAL"})

    await registry.save_connector(db, "gemini", unset=("model",))
    assert (await registry.load_connector(db, "gemini")).config == {}


async def test_an_unset_refuses_a_secret_an_undeclared_name_a_contradiction_and_jellyfin(db, secrets_key):
    """A key is removed by nobody; Jellyfin's merge has its own rules and is out of reach."""
    await registry.save_connector(db, "gemini", api_key="KEY-NOT-REAL", model="gemini-3.6-flash")
    before = await db.fetch("SELECT * FROM connector_config ORDER BY name")

    with pytest.raises(ValueError, match="api_key"):
        await registry.save_connector(db, "gemini", unset=("api_key",))
    with pytest.raises(ValueError, match="modle"):
        await registry.save_connector(db, "gemini", unset=("modle",))
    with pytest.raises(ValueError, match="model"):
        await registry.save_connector(db, "gemini", model="gemini-3.7-flash", unset=("model",))
    # Its merge is `save_jellyfin`'s, and only `api/admin.put_jellyfin` mints its token (decision 416).
    with pytest.raises(LookupError, match="jellyfin"):
        await registry.save_connector(db, "jellyfin", unset=("library_ids",))
    with pytest.raises(LookupError, match="jellyfin"):
        await registry.save_connector(db, "jellyfin", mint_webhook_token=True)
    assert await db.fetch("SELECT * FROM connector_config ORDER BY name") == before
