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

import asyncio
import json

import asyncpg
import pytest

from spielplan.connectors import registry
from spielplan.core.config import Settings


async def _link_state(conn, user_id: int) -> str | None:
    return await conn.fetchval("SELECT jellyfin_link_state FROM app_user WHERE id = $1", user_id)


async def _second_connection(pg_url):
    """A connection of its own, because a lock is only a lock across two of them.

    The json codecs are the `db` fixture's, repeated here for the same reason it sets them: the
    connector's `config` column is jsonb and `get_connector_secrets` reads it as a dict.
    """
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


async def _link(conn, app_user_id: str, token: str, *, for_update: bool) -> None:
    """What `api/admin.link_jellyfin` does to the sealed token map: open it, add one, seal it.

    The sleep is the window, widened from microseconds to a certainty. `save_jellyfin` merges
    again internally, so without the lock on *this* read the second writer still merges onto a
    map it read before the first one wrote (§14.3).
    """
    async with conn.transaction():
        cfg = await registry.load_jellyfin(conn, for_update=for_update)
        tokens = dict(cfg.user_tokens)
        await asyncio.sleep(0.2)
        tokens[app_user_id] = token
        await registry.save_jellyfin(conn, user_tokens=tokens)


def _settings(**overrides) -> Settings:
    """Env values to seed *from*. Custody comes from the process-wide `settings()`, which the
    `secrets_key` fixture provides — see `seed_from_env`'s note on why those are separate."""
    base = {"secrets_key": "test-secrets-key-not-a-real-one-at-all", "database_url": "postgresql://x/y"}
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
        database_url="postgresql://x/y", session_secret="not-a-secrets-key-and-not-a-real-one",
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


async def test_the_probed_version_is_stored_beside_the_url_not_in_the_secret(db, secrets_key):
    """§7.1's pin has to be operative. `MIN_SERVER_VERSION` and its verdict were computed by the
    admin's test button and handed to the browser, and nothing else ever read them -- so a 10.8
    install 404ed every Played write for the life of the install while every visible check passed.

    The verdict is connector *config*, not a credential: the config half needs no SECRETS_KEY and
    no migration, and `make_client` is what carries it to the one write.
    """
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
    """§14.3: the API key is admin-equivalent on the whole media server and the per-user tokens
    are real credentials, and a default dataclass repr copies both into any traceback that happens
    to hold the object -- this milestone's review watched a pytest failure header print one.

    The static guard over every field under `connectors/` lives in `test_static_contracts.py`;
    this is the runtime fact about the objects the app actually builds.
    """
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


# --- §14.3: one lock over the read-modify-write -------------------------------------------


async def test_a_for_update_load_locks_the_connector_row(db, pg_url, secrets_key):
    """The lock, asserted as a lock -- and the absence of one first, so this cannot pass by
    accident on a database that happened to be busy.

    `lock_timeout` is what makes the blocked reader a failure this test can see: without it a
    regression that drops `FOR UPDATE` would leave the suite hanging rather than red.
    """
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
    """§14.3's map is AEAD-sealed, so the merge cannot be done in SQL: it is opened in Python,
    changed, and sealed again. Reproduced in this milestone's review -- both link responses said
    `linked`, and then one account showed `has_jellyfin_token=false`. By §7.3 that account is the
    worst of the two failures: the admin table calls it linked, and its owner's sweep stops at
    their first owed row with nothing that can ever settle it.

    Measured both ways as this landed, against the same two connections and the same widened
    window: with `for_update=False` exactly one of the two tokens survives; with the lock, both.
    """
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


# --- §14.3: credentials do not follow the connector to a new server ------------------------


async def test_moving_the_server_drops_the_credentials_bound_to_it(db, secrets_key):
    """The API key is admin-equivalent on the whole media server and the per-user tokens are
    real credentials. Both are bound to the server that issued them, so re-pointing the URL
    must not carry them along — every later request would send them, in a header, to whatever
    host was just typed in.

    The accounts move with them. §7.3 makes the link badge the admin's only signal, and an account
    left saying 'linked' with a token that was just dropped is the state which stopped that
    member's sweep at their first owed row and could never settle it. The probed version goes too:
    it is a fact about the server that answered the probe (§7.1).
    """
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
    """The same clause in the one state where the app cannot see what it is destroying.

    Under M4.7's dd03 -- a restored dump under a changed or missing SECRETS_KEY -- `load_jellyfin`
    degrades to `JellyfinConfig(url=<stored>, secrets_unreadable=True)` with an EMPTY token map. So
    the de-link was guarded on `moved and current.user_tokens`, which is falsy exactly there,
    while the same save still fell through to `put_connector_secrets(secret=None,
    retire_unreadable=True)` and wrote NULL over the ciphertext. The admin key and both per-user
    tokens were destroyed -- correctly, per §14.3, they belonged to the old server -- and every
    account kept reading 'linked'. Nothing self-corrects from there: `sync_all` returns
    `skipped_no_link` on an unconfigured connector, so the sweep's own `_mark_needs_relink` is
    never reached, and §6.6's Users roster tells the admin both members are healthy while no
    credential exists anywhere.

    The guard bought nothing in exchange, which is the other half of the argument: 'linked' is only
    ever written where a token was stored, so with no tokens held the UPDATE matches no row.
    [M4.11 review cycle 2: m411-rev2-jf-02; §14.3, §7.3, M4.7 dd03]
    """
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
    """A trailing slash, or a path, is not a different server — and losing the key over one
    would teach the admin to distrust the form. Nobody is de-linked either: a badge that turns
    amber when the admin fixes a typo is the same lie as one that stays green when the token is
    gone (§7.3)."""
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
