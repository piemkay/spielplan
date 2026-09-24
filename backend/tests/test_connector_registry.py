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
import dataclasses
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


# --- §7.2: the webhook's token, and the instant the delta poll reads from -------------------


async def test_the_webhook_token_is_minted_at_the_first_save_and_never_rotated(db, secrets_key):
    """§7.2's `POST /events/jellyfin` is "token-authed", and decision 332 makes that token a
    connector secret this app generates and displays exactly once.

    Never rotated is the half with a cost attached: the operator has pasted the value into the
    Jellyfin Webhook plugin's own header field, and §6.6 cannot show it to them a second time, so
    a save that minted a fresh one would kill the intake path on the day somebody fixed a typo in
    the URL — and nothing in the app would say so until a household noticed that nothing had been
    acquired for a fortnight. Sealed rather than stored beside the URL, because whoever holds it
    can file acquisition work in this household's name (§14.3).

    AND MINTED BY A SAVE AN ADMIN PERFORMED, never by one nobody is watching (decision 416).
    `put_jellyfin` is the only response in this app that ever carries the value, so a background
    save that minted one sealed it into the database and showed it to nobody -- after which that
    route's one-time reveal answered `null` for ever while §6.6's card said a token existed.
    """
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
    """Decision 416, and the state it is about is every install that existed before this
    milestone: `webhook_token` is new here, so an upgraded connector loads configured and empty,
    and the FIRST save to reach the merge is a background one -- `poll_delta`'s watermark write
    on the worker's first tick, the sweep's version probe, §7.3's link route. Each of them
    minted, sealed and returned the value into a caller that dropped it.

    The cost is not one lost gesture. Decision 332 gives the token one appearance and there is no
    rotation route anywhere in this app, so `POST /events/jellyfin` would answer 401 to every real
    delivery for ever while the card reported `has_webhook_token: true`. So the mint is the admin
    save's to ask for: a background save may not create one, and may not destroy one either.
    [review cycle 1: m52-rev-delta-01, m52-rev1-token-01]
    """
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
    """Decision 332 shows the token once, so a save that mints one an admin never receives has
    spent the only appearance it gets -- and there is no rotation route anywhere in this app.

    `load_jellyfin(for_update=True)` says in its own docstring that it "locks nothing when the row
    does not exist yet", and the FIRST save is exactly the save that mints: both transactions read
    no row, both minted, and the loser's admin was shown a value that was never stored. Measured
    both ways as this landed, against the same two connections: without the row taken first, two
    tokens are minted and one of them is the stored one; with it, both saves are told the same
    value the database holds. [review cycle 1: m52-rev1-token-02]
    """
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
    """The asymmetry §14.3 actually draws, which is about DIRECTION and not about the connector.

    The API key and the per-user tokens are dropped on an origin change because this app would
    otherwise send them, in a header, to whatever host was just typed in. The webhook token never
    leaves this install — it is what a caller has to present to reach `POST /events/jellyfin` —
    so a new Jellyfin address puts it nowhere it was not already, while dropping it would silently
    end the intake path with nothing to show the admin and no way to re-read a value that is
    displayed once (decision 332).

    The assertion that matters is the one after the reload: the sealed blob is written whole, so a
    move leaving no api_key and no user_tokens would otherwise fall through to the write that puts
    NULL over the ciphertext, and the token would be gone from the disk while the object returned
    by the same call still carried it.
    """
    first = await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", user_tokens={"1": "tok"},
        mint_webhook_token=True,
    )
    moved = await registry.save_jellyfin(db, url="http://elsewhere.example:8096")

    assert (moved.api_key, moved.user_tokens) == ("", {})
    assert moved.webhook_token == first.webhook_token
    assert (await registry.load_jellyfin(db)).webhook_token == first.webhook_token


async def test_the_library_pick_survives_the_move_that_drops_the_credentials(db, secrets_key):
    """The one server-bound value in this merge with no `moved` branch, pinned so that it is a
    decision rather than an omission.

    §14.3 drops what this app would SEND to whatever host was just typed in, and the version pair
    and the watermark are facts this app DERIVED from the old server. The pick is neither: it is
    the admin's own recorded intent and it travels nowhere. `moved` fires on http->https, on a new
    port and on hostname->IP -- the same install at a corrected address, whose library ids are
    GUIDs minted in its own database and still match -- so dropping it would widen the boundary
    from the libraries the admin picked to the whole server on the commonest of the three
    gestures, which is the harm the custody branch already refuses in those words: the household
    billed for the library they deselected. A genuinely different install answers for none of the
    stored ids, and its adds are recorded `library not picked` until the admin re-picks from the
    list `GET /connectors/jellyfin/libraries` now serves. [review cycle 1: m52-rev-lib-01]
    """
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
    """The empty case, which is a real state and not a hypothetical: `seed_from_env` writes the
    secret directly rather than through `save_jellyfin`, so an automated install arrives with a
    configured connector and no token at all, and every install configured before this milestone
    is in the same state until its next save.

    A plain equality would then admit a caller that also sends nothing, which is every caller.
    The rule lives on the config rather than in the route because `api/` decides HTTP shapes and
    this is the §7.2 clause itself.
    """
    await registry.seed_from_env(db, _settings(jellyfin_url="http://jf", jellyfin_api_key="k"))
    seeded = await registry.load_jellyfin(db)
    assert seeded.configured and seeded.webhook_token == ""
    assert seeded.webhook_token_matches("") is False
    assert seeded.webhook_token_matches(None) is False

    minted = await registry.save_jellyfin(db, url="http://jf", mint_webhook_token=True)
    assert minted.webhook_token_matches(minted.webhook_token) is True
    assert minted.webhook_token_matches(minted.webhook_token + "x") is False
    assert minted.webhook_token_matches("") is False
    # Over BYTES, and these are the inputs that made that necessary: Starlette decodes a header
    # as latin-1, so one byte >= 0x80 arrives here as a non-ASCII `str`, and `hmac.compare_digest`
    # raises `TypeError` on those. Nothing in `app.py` catches it, so the app's one
    # stranger-reachable route answered an unauthenticated caller 500 with a traceback -- the one
    # answer decision 365 forbids, from before the token check had even finished.
    # [review cycle 1: m52-rev-events-01]
    assert minted.webhook_token_matches("\xff") is False
    assert minted.webhook_token_matches(minted.webhook_token + "\u00e9") is False


async def test_the_webhook_token_does_not_survive_a_repr(db, secrets_key):
    """`test_no_connector_credential_survives_a_repr` above is this rule for the two credentials
    §14.3 names by name, and `test_static_contracts.py`'s static sweep matches on the field names
    `api_key`, `token` and `user_tokens` — so the third credential this connector now holds is
    covered by neither until it is named here."""
    cfg = await registry.save_jellyfin(
        db, url="http://jellyfin.local:8096", api_key="k", mint_webhook_token=True
    )
    assert cfg.webhook_token, "there is something to print"
    assert cfg.webhook_token not in repr(cfg)
    assert "jellyfin.local" in repr(cfg), "the URL is what makes such a line worth printing"


async def test_the_delta_poll_starts_at_this_installs_own_creation_instant(db, secrets_key):
    """decision 366, and the whole of it is "never epoch". The poll reads every row the server
    saved after the floor (decision 409), so an epoch floor selects the household's entire corpus
    on the very first poll and files an acquisition task for every title it already owns.
    `min(applied_at) FROM schema_migration` is the one instant this schema already records that
    means "when this database came into existence".

    The watermark is connector state and not a household fact, so it needs no DDL — and it lives
    in the plaintext config half beside `library_ids`, because it is not a secret and a value
    that vanished with a SECRETS_KEY failure would re-read the corpus on the next poll.

    Decision 412 moves the floor from `min(applied_at)` to 0025's own `applied_at` -- the instant
    this install gained §7.2's fallback. On a fresh install the two are one migration run apart;
    on an UPGRADE, which is every install that exists, `min(applied_at)` is when 0001 ran, and a
    first poll from there filed every title added since the install as a new add. Both are held:
    the floor is 0025's, and it is not the install's when the install is older.
    [review cycle 3: M52-C3-STATE-02]
    """
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
    """A watermark belongs to the clock that stamped it, which is the argument the probed version
    already makes one field over (§7.1). Carrying one to a different server is worse than losing
    it: a new install whose library was imported before the old watermark would have every one of
    its adds skipped, silently and for ever, while dropping it costs one re-read -- and what
    absorbs that re-read is decision 411, which files a re-offer of a title the bundle supplied
    and the app already placed below every genuine add and closes it at stage 1. This docstring
    used to credit the queue's `(kind, key)` identity, which absorbs nothing of a re-imported
    library carrying new ids. The floor the re-read starts from is decision 412's.
    [review cycle 3: M52-C3-STATE-02, M52-C3-STATE-06]"""
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
    """M4.7's dd03 state, read again one milestone later. `load_jellyfin` degrades to a config
    carrying the stored URL when the sealed half will not open — and it used to carry the URL
    ALONE, while `save_jellyfin` rebuilds the stored config from what it was handed. So the next
    save of a corrected address erased every other key in the plaintext column, and correcting the
    address is the one gesture this state invites.

    That was invisible while `library_ids` had no reader. Decision 364 makes it the acquisition
    boundary, so the erasure silently widens it from the libraries the admin picked to the whole
    server — the household billed for the library they deselected — and decision 366's watermark
    went with it, which re-reads the corpus. What failed is the credential column; taking the
    connector's plaintext settings down with it is a second failure the first does not justify.
    """
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


# --- §6.6: one generic surface, with Jellyfin as its first instance (M5.5 plan A1-A4) ---------


def test_the_jellyfin_spec_is_load_jellyfin_and_save_jellyfin_themselves():
    """Plan A2: the two hardened functions become the spec's callables BY IDENTITY, not through a
    wrapper that could drift from them. M5.2's four review cycles are written into those two bodies
    -- the mint a save has to ask for (decisions 416, 418), the origin rule of §14.3, the custody
    degrade of M4.7 dd03 -- and a parallel generic path would be a second answer to every one.

    The secret fields are held to what `JellyfinConfig` actually hides from its repr, so the list
    the spec declares cannot name a credential the dataclass prints or miss one it seals.
    """
    spec = registry.spec_for("jellyfin")
    assert spec.load is registry.load_jellyfin
    assert spec.save is registry.save_jellyfin
    assert spec.seeded is True
    assert spec.test is None, "api/admin.test_jellyfin stores the 7.1 verdict as it tests (decision 433)"
    hidden = {f.name for f in dataclasses.fields(registry.JellyfinConfig) if not f.repr}
    assert set(spec.secret_fields) == hidden == {"api_key", "user_tokens", "webhook_token"}
    declared = {f.name for f in dataclasses.fields(registry.JellyfinConfig)}
    assert set(spec.config_fields) <= declared


async def test_the_generic_calls_answer_for_jellyfin_exactly_as_its_own_functions_do(
    db, secrets_key
):
    """`load_connector` / `save_connector` on `jellyfin` are `load_jellyfin` / `save_jellyfin`, and
    the one argument the generic path refuses is the mint: decision 416 gives it to
    `api/admin.put_jellyfin` alone, and a generic write that forwarded it would hand a card's body
    field the one gesture decision 418 says only a save that asks may make.
    """
    saved = await registry.save_connector(db, "jellyfin", url="http://jf/", api_key="k")
    assert isinstance(saved, registry.JellyfinConfig)
    assert (saved.url, saved.api_key) == ("http://jf", "k")
    assert saved.webhook_token == "", "a save that did not ask mints nothing (decision 418)"
    assert saved == await registry.load_jellyfin(db)
    assert await registry.load_connector(db, "jellyfin") == await registry.load_jellyfin(db)

    with pytest.raises(ValueError, match="decision 416"):
        await registry.save_connector(db, "jellyfin", url="http://jf", mint_webhook_token=True)
    assert (await registry.load_jellyfin(db)).webhook_token == ""


async def test_a_provider_round_trips_with_its_key_sealed_and_its_model_in_plaintext(
    db, secrets_key
):
    """§2: a connector's secret is AEAD-sealed under the DEK and its settings are not. A provider
    key bills the household, so it belongs with the sealed half; the model is what the card shows.

    The masked save is the form's idiom, which `save_jellyfin` argues: the key is shown as a mask
    and posted empty, and a whole-row write would blank it every time the admin changed the model.
    """
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
    """The admin picks a model before pasting a key, as they type Jellyfin's address first; and the
    `llm` row (decisions 324, 325) holds no secret at all, so neither may demand SECRETS_KEY. A key,
    though, is §2's refusal: without custody there is nowhere to seal it, and nothing is written.
    """
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
    """A misspelt field is refused rather than stored: `apikey` would otherwise land in the
    plaintext half, beside the model, and the provider would go on answering 401 to the key the
    admin believes they saved. The refusal names what is declared, so the fix is one read."""
    with pytest.raises(ValueError, match="api_key") as refused:
        await registry.save_connector(db, "gemini", apikey="KEY-IN-THE-WRONG-PLACE")
    assert "apikey" in str(refused.value)
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0

    with pytest.raises(LookupError, match="gemini"):
        registry.spec_for("gemeni")
    with pytest.raises(LookupError, match="jellyfin"):
        await registry.load_connector(db, "plex")


async def test_an_unreadable_provider_secret_degrades_and_only_a_typed_key_retires_it(
    db, secrets_key, monkeypatch, caplog
):
    """M4.7 dd03 for every connector the generic path serves, argued in `load_jellyfin` and
    `save_jellyfin`: an unreadable DEK must not take a route down, so the read degrades -- the
    plaintext half kept, the secrets empty, the state said out loud -- and is logged at ERROR on
    every read. A save that types no key writes the config half alone, so a ciphertext that is
    unreadable only until the right .env returns is not erased; a save that types one is the
    admin's repair, and retires the row nothing can open.
    """
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
    """`JellyfinConfig`'s rule for every other connector: a provider key bills the household and
    a default repr copies it into any traceback or `%r` log line holding the state (§14.3)."""
    state = await registry.save_connector(
        db, "openai", api_key="BILLABLE-KEY-NOT-REAL", model="gpt-5.6-terra"
    )
    assert state.secrets["api_key"] == "BILLABLE-KEY-NOT-REAL", "held, and usable"
    printed = repr(state)
    assert "BILLABLE-KEY-NOT-REAL" not in printed
    assert "openai" in printed and "gpt-5.6-terra" in printed, "what makes the line worth printing"


def test_the_llm_providers_seed_their_key_and_the_llm_settings_seed_nothing():
    """§2 names LLM among the connectors env may seed ("Jellyfin, LLM, TMDB, OMDb, Trakt"). Each
    provider's key and nothing else: no model override is declared (plan A3's "if the owner wants
    them" was not asked for), and no cap is seeded because decision 325 ships none."""
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
    """The two properties the Jellyfin tests above hold, for the connector family M5 is about."""
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
    """`seeded` is a claim about `env_seeds`, held to it rather than restated beside it: with every
    seed variable `Settings` declares for a registered connector set, the connectors env seeds are
    exactly the ones the registry marks. The variables are found through the registry's own names,
    so a connector added to either side without the other fails here."""
    from spielplan.core.config import Settings

    prefixes = tuple(f"{name}_" for name in registry.CONNECTORS)
    fields = [name for name in Settings.model_fields if name.startswith(prefixes)]
    assert {"gemini_api_key", "anthropic_api_key", "openai_api_key"} <= set(fields)
    every = _settings(**{name: f"http://{name}.example" for name in fields})
    seeded = {spec.name for spec in registry.CONNECTORS.values() if spec.seeded}
    assert seeded == set(registry.env_seeds(every))
    assert "llm" in registry.CONNECTORS and "llm" not in seeded


async def test_the_test_dispatch_is_one_table_and_refuses_a_connector_with_no_test(monkeypatch):
    """Plan A4: "One dispatch table, not a route per provider". The table is `ConnectorSpec.test`,
    and a connector with none is refused by name rather than answered with a guessed probe -- which
    the API maps to 404. Jellyfin is one of those on purpose: its card's test stores §7.1's verdict
    as it tests, and stays `api/admin.test_jellyfin` (decision 433)."""
    conn = object()
    called: list[object] = []

    async def probe(given):
        called.append(given)
        return {"ok": True, "detail": "stub"}

    monkeypatch.setitem(
        registry.CONNECTORS, "tmdb", dataclasses.replace(registry.CONNECTORS["tmdb"], test=probe)
    )
    assert await registry.test_connector(conn, "tmdb") == {"ok": True, "detail": "stub"}
    assert called == [conn]

    for untested in ("omdb", "jellyfin", "llm"):
        with pytest.raises(LookupError, match=f"connector {untested} has no test in this build"):
            await registry.test_connector(conn, untested)


async def test_a_provider_probe_refuses_before_any_request_when_it_holds_no_usable_key(
    db, secrets_key, monkeypatch
):
    """The provider test answers the two states it can know without a request, in the sentences
    the card shows: no key typed yet, and a key that will not open (the rail's own sentence,
    `SECRETS_UNREADABLE_REASON`). Neither reaches `spielplan.llm`, which is only imported once
    there is a key to send."""
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
