"""A wrong `SECRETS_KEY` boots green and then 500s the member writes; these tests drive the routes,
because only HTTP can say that a member's tap answers 200."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import asyncpg
import httpx
import pytest
from cryptography.exceptions import InvalidTag

from spielplan.connectors import registry
from spielplan.core import secrets as sec
from spielplan.core import secrets_cli
from spielplan.core.config import settings
from spielplan.push import keys as push_keys
from spielplan.sync import seen

ADMIN_PASSWORD = "an-admin-password"
JELLYFIN_URL = "http://jellyfin.test"
JELLYFIN_KEY = "JF-ADMIN-KEY-UNSCOPED"

# A regenerated key, or a dump restored without its .env: the same fact to the app.
OTHER_KEY = "a-different-secrets-key-not-a-real-one"


def _use_key(monkeypatch, value: str) -> None:
    """`settings()` is `lru_cache`d, so the clear is not optional."""
    monkeypatch.setenv("SECRETS_KEY", value)
    settings.cache_clear()


@contextlib.asynccontextmanager
async def _boot(monkeypatch, pg_url, tmp_path):
    """A whole lifespan: these assertions are about a second process, or a boot under another key."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings.cache_clear()

    from spielplan.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def custody(secrets_key, db, app):
    """`secrets_key` precedes `app`: the lifespan mints the DEK, so a later key was never the install's."""
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    user_id = (await client.get("/api/auth/me")).json()["id"]
    # Decision 117 gates the rail line where Rate says why a push did not happen; without it the
    # assertion below would be vacuous.
    await db.execute("UPDATE app_user SET show_model = true WHERE id = $1", user_id)
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned, jellyfin_id, overview)
        SELECT i, 'movie', 'Title ' || i, true, 'jf-' || i, 'A film about ' || i
          FROM generate_series(1, 20) AS i
        """
    )
    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": JELLYFIN_KEY},
    )
    assert saved.status_code == 200 and saved.json()["configured"] is True
    event_id = await db.fetchval(
        "INSERT INTO playback_event (source, title_id, user_id, finished) "
        "VALUES ('jellyfin', 2, $1, true) RETURNING id",
        user_id,
    )
    # One session answers both a placement and a not-seen; its first card is drawn here.
    await db.execute("INSERT INTO ladder_setup (user_id) VALUES ($1)", user_id)
    assert (await client.post("/api/rate/session", json={"kinds": ["movie"]})).status_code == 200
    return {"client": client, "user_id": user_id, "event_id": event_id}


@pytest.fixture
async def no_connector_yet(secrets_key, db, app):
    """A DEK minted for the VAPID pair alone and no connector yet: every fresh install, and the state
    where `load_jellyfin` has no ciphertext to fail on."""
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0
    assert await db.fetchval("SELECT count(*) FROM data_encryption_key") == 1
    return client


async def test_every_member_write_still_commits_under_a_changed_secrets_key(
    custody, db, monkeypatch
):
    """The reason must name the variable, not "Jellyfin not configured": Jellyfin IS configured."""
    client, user_id, event_id = custody["client"], custody["user_id"], custody["event_id"]
    _use_key(monkeypatch, OTHER_KEY)

    state = await client.post("/api/titles/1/state", json={"state": "seen"})
    assert state.status_code == 200, state.text
    assert state.json()["synced"] is False
    assert state.json()["reason"] == registry.SECRETS_UNREADABLE_REASON
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", user_id
    ) == "seen"

    card = (await client.get("/api/rate")).json()["card"]
    placed = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})
    assert placed.status_code == 200, placed.text
    assert any("SECRETS_KEY" in line for line in placed.json()["log"]), placed.json()["log"]

    nxt = placed.json()["card"]
    not_seen = await client.post("/api/rate/not-seen", json={"card_token": nxt["token"]})
    assert not_seen.status_code == 200, not_seen.text
    assert any("SECRETS_KEY" in line for line in not_seen.json()["log"]), not_seen.json()["log"]

    prompt = await client.post(f"/api/prompts/finish/{event_id}", json={"finished": True})
    assert prompt.status_code == 200, prompt.text
    assert prompt.json()["sync"]["synced"] is False
    assert prompt.json()["sync"]["reason"] == registry.SECRETS_UNREADABLE_REASON
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 2", user_id
    ) == "seen"


async def test_no_admin_connector_route_answers_500_under_a_changed_secrets_key(
    custody, monkeypatch
):
    """409 is legitimate for the three that need a client; 500 is the unhandled `InvalidTag`."""
    client = custody["client"]
    _use_key(monkeypatch, OTHER_KEY)

    listed = await client.get("/api/admin/users")
    assert listed.status_code == 200, listed.text

    got = await client.get("/api/admin/connectors/jellyfin")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["secrets_unreadable"] is True
    assert body["configured"] is False and body["has_api_key"] is False
    # The URL survives: the admin must see which server to know only the key is missing.
    assert body["url"] == JELLYFIN_URL

    for method, path in (
        ("POST", "/api/admin/connectors/jellyfin/test"),
        ("GET", "/api/admin/connectors/jellyfin/users"),
        ("POST", "/api/admin/connectors/jellyfin/sync"),
        ("POST", "/api/admin/connectors/jellyfin/poll"),
    ):
        answered = await client.request(method, path)
        assert answered.status_code != 500, f"{method} {path} -> {answered.text}"


async def test_the_put_that_re_enters_the_api_key_succeeds_by_sealing_under_a_fresh_dek(
    custody, db, monkeypatch
):
    """`ensure_dek` unwraps the active DEK before sealing, so the repair retires the unreadable row
    and mints a fresh one; the retired row stays for when the right .env returns."""
    client = custody["client"]
    _use_key(monkeypatch, OTHER_KEY)

    put = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": "a-freshly-issued-jellyfin-key"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["configured"] is True

    got = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert got["secrets_unreadable"] is False and got["has_api_key"] is True

    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == "a-freshly-issued-jellyfin-key"
    assert await db.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 1
    assert await db.fetchval("SELECT count(*) FROM data_encryption_key") == 2, (
        "the unreadable row is retired, not deleted: ciphertexts still name it"
    )


async def test_the_first_connector_save_under_an_unreadable_dek_is_not_a_500(
    no_connector_yet, db, monkeypatch
):
    """With nothing sealed yet `load_jellyfin` reports no failure, so the repair must belong to the
    DEK, not to a connector's ciphertext."""
    client = no_connector_yet
    _use_key(monkeypatch, OTHER_KEY)

    card = await client.get("/api/admin/connectors/jellyfin")
    assert card.status_code == 200, card.text
    assert card.json()["configured"] is False and card.json()["secrets_unreadable"] is False
    system = await client.get("/api/admin/system")
    assert system.status_code == 200 and system.json()["secrets"]["unreadable"] is True

    put = await client.put(
        "/api/admin/connectors/jellyfin", json={"url": JELLYFIN_URL, "api_key": JELLYFIN_KEY}
    )
    assert put.status_code == 200, put.text
    assert put.json()["configured"] is True

    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == JELLYFIN_KEY
    assert await db.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 1
    assert await db.fetchval("SELECT count(*) FROM data_encryption_key") == 2, (
        "the unreadable row is retired rather than deleted here too"
    )


async def test_a_seeded_connector_does_not_take_the_boot_down_under_an_unreadable_dek(
    secrets_key, db, pg_url, tmp_path, monkeypatch, caplog
):
    """The env seed runs first in the lifespan, so an unreadable DEK must not stop the container:
    `spielplan-secrets reset` needs one that is up."""
    await sec.ensure_dek(db)
    monkeypatch.setenv("TMDB_API_KEY", "a-tmdb-key-from-the-env-file")
    _use_key(monkeypatch, OTHER_KEY)

    with caplog.at_level(logging.ERROR, logger="spielplan.connectors"):
        async with _boot(monkeypatch, pg_url, tmp_path) as application:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            )
            health = await client.get("/api/health")
            await client.aclose()

    assert health.status_code == 200 and health.json()["ok"] is True
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the seed was skipped, not written half-sealed"
    )
    assert any(
        "SECRETS_KEY" in record.getMessage() and "tmdb" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


async def test_a_url_only_save_under_a_wrong_key_does_not_destroy_the_ciphertext(
    secrets_key, custody, db, monkeypatch
):
    """`secrets=None` NULLs both sealed columns; an unreadable ciphertext is not worthless, since the
    right key may still turn up."""
    client = custody["client"]
    before = await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = $1", registry.JELLYFIN
    )
    _use_key(monkeypatch, OTHER_KEY)

    put = await client.put(
        "/api/admin/connectors/jellyfin", json={"url": "http://jellyfin.test", "api_key": ""}
    )
    assert put.status_code == 200, put.text

    after = await db.fetchrow(
        "SELECT config ->> 'url' AS url, secrets_encrypted FROM connector_config WHERE name = $1",
        registry.JELLYFIN,
    )
    assert after["url"] == JELLYFIN_URL
    assert bytes(after["secrets_encrypted"]) == bytes(before)

    # And it really is only unreadable: put the original key back and the secret returns.
    _use_key(monkeypatch, secrets_key)
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == JELLYFIN_KEY


async def test_moving_the_server_under_a_wrong_key_drops_the_ciphertext_it_cannot_read(
    secrets_key, custody, db, monkeypatch
):
    """A changed origin must drop the credentials (§14.3), even while the DEK is unreadable:
    otherwise the old key is sent to the new host once the .env returns."""
    client, user_id = custody["client"], custody["user_id"]
    # A token too: §7.3's credentials travel the same column and code path.
    await registry.save_jellyfin(db, user_tokens={str(user_id): "OLD-SERVER-USER-TOKEN"})
    _use_key(monkeypatch, OTHER_KEY)

    put = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://new-jellyfin.test:8096", "api_key": ""},
    )
    assert put.status_code == 200, put.text
    assert put.json()["configured"] is False, "the admin has to enter a key for the new server"
    assert await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = $1", registry.JELLYFIN
    ) is None

    # Nothing returns with the original .env: the credentials died when the origin changed.
    _use_key(monkeypatch, secrets_key)
    cfg = await registry.load_jellyfin(db)
    assert cfg.url == "http://new-jellyfin.test:8096"
    assert cfg.api_key == "" and cfg.user_tokens == {}
    assert registry.make_client(cfg) is None


async def test_relinking_under_a_wrong_key_is_refused_rather_than_keeping_a_stale_token(
    secrets_key, custody, db, monkeypatch
):
    """An unreadable DEK cannot enumerate the old token, so relinking is refused rather than cleared;
    the key-re-entering PUT already clears tokens."""
    client, user_id = custody["client"], custody["user_id"]
    linked = await client.post(
        f"/api/admin/users/{user_id}/jellyfin", json={"jellyfin_user_id": "jf-user-A"}
    )
    assert linked.status_code == 200, linked.text
    # What a completed §7.3 sign-in leaves behind, sealed beside the admin key.
    await registry.save_jellyfin(db, user_tokens={str(user_id): "TOKEN-OF-JF-USER-A"})
    _use_key(monkeypatch, OTHER_KEY)

    relinked = await client.post(
        f"/api/admin/users/{user_id}/jellyfin", json={"jellyfin_user_id": "jf-user-B"}
    )
    assert relinked.status_code == 409, relinked.text
    assert "SECRETS_KEY" in relinked.json()["detail"], relinked.text

    _use_key(monkeypatch, secrets_key)
    cfg = await registry.load_jellyfin(db)
    assert cfg.user_tokens == {str(user_id): "TOKEN-OF-JF-USER-A"}
    (still,) = await seen.linked_users(db, cfg)
    assert (still.jf_user_id, still.token) == ("jf-user-A", "TOKEN-OF-JF-USER-A"), (
        "the token that survived the wrong key is still paired with the identity that issued it"
    )


async def test_a_dek_row_without_a_vapid_row_still_boots_under_a_changed_key(
    secrets_key, db, pg_url, tmp_path, monkeypatch
):
    """`ensure_keypair` catches `RuntimeError`, so `SecretsUnreadable` must subclass it or the
    container never starts."""
    await sec.ensure_dek(db)
    assert await db.fetchval("SELECT count(*) FROM app_setting") == 0, "no VAPID row yet"
    _use_key(monkeypatch, OTHER_KEY)

    async with _boot(monkeypatch, pg_url, tmp_path) as application:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        )
        health = await client.get("/api/health")
        await client.aclose()
    assert health.status_code == 200 and health.json()["ok"] is True
    assert await db.fetchval("SELECT count(*) FROM app_setting") == 0, (
        "the boot refused to mint a keypair it could not seal, rather than crashing"
    )


async def test_a_ciphertext_moved_to_another_row_does_not_open(secrets_key, db):
    """§14 risk 3: without associated data a ciphertext copied to another row opened there."""
    await sec.put_connector_secrets(
        db, registry.JELLYFIN, {"url": JELLYFIN_URL}, {"api_key": JELLYFIN_KEY}
    )
    await sec.put_connector_secrets(db, "tmdb", {}, {"api_key": "a-tmdb-key"})
    row = await db.fetchrow(
        "SELECT secrets_encrypted, secrets_key_id FROM connector_config WHERE name = $1",
        registry.JELLYFIN,
    )
    await db.execute(
        "UPDATE connector_config SET secrets_encrypted = $1, secrets_key_id = $2 "
        "WHERE name = 'tmdb'",
        row["secrets_encrypted"],
        row["secrets_key_id"],
    )

    with pytest.raises(sec.SecretsUnreadable) as exc:
        await sec.get_connector_secrets(db, "tmdb")
    assert "tmdb" in str(exc.value)


async def test_a_row_sealed_before_the_binding_still_opens_and_is_bound_by_that_read(
    secrets_key, db
):
    """Pre-M4.7 rows (no associated data) must still open, and are re-sealed on that read: a household
    that never reopens the Connectors card would otherwise stay unbound for ever."""
    key_id, dek = await sec.ensure_dek(db)
    unbound = sec.seal(dek, {"api_key": "sealed-before-m4.7"})
    await db.execute(
        "INSERT INTO connector_config (name, config, secrets_encrypted, secrets_key_id) "
        "VALUES ('tmdb', '{}'::jsonb, $1, $2)",
        unbound,
        key_id,
    )
    _config, secret = await sec.get_connector_secrets(db, "tmdb")
    assert secret == {"api_key": "sealed-before-m4.7"}

    stored = bytes(await db.fetchval(
        "SELECT secrets_encrypted FROM connector_config WHERE name = 'tmdb'"
    ))
    assert stored != bytes(unbound), "the read re-sealed the row with its own associated data"
    assert sec.open_sealed(dek, stored, sec.aad_for("connector_config", "tmdb")) == secret
    with pytest.raises(InvalidTag):
        sec.open_sealed(dek, stored, None)


async def test_a_pre_m4_7_row_that_has_been_read_once_no_longer_opens_in_another_row(
    secrets_key, db
):
    """Rows sealed before the binding open anywhere until one read re-seals them; then the move fails."""
    key_id, dek = await sec.ensure_dek(db)
    await db.execute(
        "INSERT INTO connector_config (name, config, secrets_encrypted, secrets_key_id) "
        "VALUES ($1, '{}'::jsonb, $2, $3)",
        registry.JELLYFIN,
        sec.seal(dek, {"api_key": JELLYFIN_KEY}),  # no associated data: a pre-M4.7 install
        key_id,
    )
    await sec.put_connector_secrets(db, "tmdb", {}, {"api_key": "a-tmdb-key"})

    _config, opened = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert opened["api_key"] == JELLYFIN_KEY, "the row still opens for its own connector"

    row = await db.fetchrow(
        "SELECT secrets_encrypted, secrets_key_id FROM connector_config WHERE name = $1",
        registry.JELLYFIN,
    )
    await db.execute(
        "UPDATE connector_config SET secrets_encrypted = $1, secrets_key_id = $2 "
        "WHERE name = 'tmdb'",
        row["secrets_encrypted"],
        row["secrets_key_id"],
    )
    with pytest.raises(sec.SecretsUnreadable):
        await sec.get_connector_secrets(db, "tmdb")


async def test_concurrent_first_boots_leave_exactly_one_active_dek_row(secrets_key, db, pg_url):
    """Three concurrent first boots produced two active rows; a rotation must know which to re-wrap."""
    conns = [await asyncpg.connect(pg_url) for _ in range(3)]
    try:
        results = await asyncio.gather(*(sec.ensure_dek(c) for c in conns))
    finally:
        for conn in conns:
            await conn.close()

    assert len({key_id for key_id, _ in results}) == 1, "the losers adopted the winner's key_id"
    assert len({dek for _, dek in results}) == 1, "and the winner's key material"
    assert await db.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 1


async def test_first_boot_creates_exactly_one_active_random_256_bit_key(
    secrets_key, app, db
):
    """Against a real first boot, not a pure function or a one-byte fake key."""
    rows = await db.fetch("SELECT key_id, wrapped_dek, retired_at FROM data_encryption_key")
    assert len(rows) == 1
    assert rows[0]["retired_at"] is None
    assert len(sec._unwrap(bytes(rows[0]["wrapped_dek"]), secrets_key)) == 32
    assert bytes(rows[0]["wrapped_dek"]) != b"\x00" * len(rows[0]["wrapped_dek"])


async def test_a_second_lifespan_adopts_the_first_boots_key_id(
    secrets_key, db, pg_url, tmp_path, monkeypatch
):
    """A second active row is what 0017's partial unique index forbids."""
    async with _boot(monkeypatch, pg_url, tmp_path):
        pass
    first = await db.fetchval("SELECT key_id FROM data_encryption_key")
    assert first is not None

    async with _boot(monkeypatch, pg_url, tmp_path):
        pass
    assert await db.fetchval("SELECT count(*) FROM data_encryption_key") == 1
    assert await db.fetchval("SELECT key_id FROM data_encryption_key") == first


async def _run_cli(*argv: str) -> int:
    """In a thread because `main` calls `asyncio.run`; through `main` so argument parsing and exit
    codes are asserted too."""
    return await asyncio.to_thread(secrets_cli.main, list(argv))


async def test_rewrap_moves_the_wrapping_and_leaves_every_ciphertext_untouched(
    secrets_key, db, pg_url, monkeypatch, capsys
):
    """The DEK does not change, so no ciphertext is rewritten and no `key_id` advances."""
    await sec.put_connector_secrets(
        db, registry.JELLYFIN, {"url": JELLYFIN_URL}, {"api_key": JELLYFIN_KEY}
    )
    before = await db.fetchrow(
        "SELECT k.key_id, k.wrapped_dek, c.secrets_encrypted, c.secrets_key_id "
        "FROM data_encryption_key k, connector_config c WHERE c.name = $1",
        registry.JELLYFIN,
    )
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("rewrap", "--old-key", secrets_key, "--new-key", OTHER_KEY) == 0
    assert "rewrapped" in capsys.readouterr().out

    after = await db.fetchrow(
        "SELECT k.key_id, k.wrapped_dek, c.secrets_encrypted, c.secrets_key_id "
        "FROM data_encryption_key k, connector_config c WHERE c.name = $1",
        registry.JELLYFIN,
    )
    assert after["key_id"] == before["key_id"]
    assert after["secrets_key_id"] == before["secrets_key_id"]
    assert bytes(after["secrets_encrypted"]) == bytes(before["secrets_encrypted"])
    assert bytes(after["wrapped_dek"]) != bytes(before["wrapped_dek"])

    # The old key no longer opens it, the new one does, and the plaintext is unchanged.
    with pytest.raises(sec.SecretsUnreadable):
        await sec.get_connector_secrets(db, registry.JELLYFIN)
    _use_key(monkeypatch, OTHER_KEY)
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == JELLYFIN_KEY


async def test_rewrap_refuses_when_more_than_one_row_is_un_retired(
    secrets_key, db, pg_url, monkeypatch, capsys
):
    """The index is dropped on purpose: only an install where 0017 refused to apply can reach this.
    Guessing which row is current is the operator's call."""
    await sec.ensure_dek(db)
    await db.execute("DROP INDEX data_encryption_key_one_active")
    await db.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ('a-second-row', $1)",
        sec._wrap(b"\x01" * 32, secrets_key),
    )
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("rewrap", "--old-key", secrets_key, "--new-key", OTHER_KEY) == 1
    printed = capsys.readouterr().out
    assert "refusing" in printed and "a-second-row" in printed


async def test_rewrap_refuses_a_new_key_the_app_would_refuse_to_boot_with(
    secrets_key, db, pg_url, monkeypatch, capsys
):
    """The same 32-character floor as `core/config` (cs-44): a key the app would refuse must not be
    written."""
    await sec.ensure_dek(db)
    before = bytes(await db.fetchval("SELECT wrapped_dek FROM data_encryption_key"))
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("rewrap", "--old-key", secrets_key, "--new-key", "x") == 1
    printed = capsys.readouterr().out
    assert "refusing" in printed and "SECRETS_KEY" in printed
    assert "token_urlsafe" in printed, printed
    assert bytes(await db.fetchval("SELECT wrapped_dek FROM data_encryption_key")) == before


async def test_rewrap_still_accepts_a_short_old_key_because_that_is_the_install_it_rescues(
    db, pg_url, monkeypatch, capsys
):
    """The floor is on the value being written: `--old-key` may be short, since that is the install
    being rescued."""
    short = "x"
    dek = b"\x02" * 32
    # Wrapped by hand: `Settings` refuses the short key that wrapped it.
    await db.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ('a-pre-floor-row', $1)",
        sec._wrap(dek, short),
    )

    # The real key goes into the environment first: `spielplan-secrets` builds `Settings` first.
    _use_key(monkeypatch, OTHER_KEY)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("rewrap", "--old-key", short, "--new-key", OTHER_KEY) == 0
    assert "rewrapped" in capsys.readouterr().out
    assert await sec.load_dek(db, "a-pre-floor-row") == dek, (
        "the same key material, now wrapped under the value .env can carry"
    )


async def test_reset_retires_what_it_cannot_unwrap_and_says_what_was_lost(
    custody, db, pg_url, monkeypatch, capsys
):
    """`rewrap` needs the old key; without `reset` a lost key is a permanent 500 on every read."""
    _use_key(monkeypatch, OTHER_KEY)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("reset") == 0
    printed = capsys.readouterr().out
    assert "connector_config/jellyfin" in printed
    assert "app_setting/push.vapid" in printed

    assert await db.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 0
    assert await db.fetchval(
        "SELECT secrets_encrypted IS NULL AND secrets_key_id IS NULL FROM connector_config "
        "WHERE name = $1",
        registry.JELLYFIN,
    ) is True
    # The whole VAPID row goes: a public half left behind is offered to browsers while nothing can
    # sign for it.
    assert await db.fetchval(
        "SELECT count(*) FROM app_setting WHERE key = 'push.vapid'"
    ) == 0

    # The next save mints a fresh DEK and seals under it.
    put = await custody["client"].put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": "re-entered-after-the-reset"},
    )
    assert put.status_code == 200, put.text
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == "re-entered-after-the-reset"


async def test_reset_reaches_the_rows_the_connectors_card_repair_retired(
    custody, db, pg_url, monkeypatch, capsys
):
    """After the card's repair the active row opens but older rows still name the retired key; one
    question in `core.secrets` keeps `reset` and the System card from disagreeing."""
    client = custody["client"]
    await sec.put_connector_secrets(db, "tmdb", {}, {"api_key": "a-tmdb-key"})
    retired = await sec.active_key_id(db)
    _use_key(monkeypatch, OTHER_KEY)

    repaired = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": "a-freshly-issued-jellyfin-key"},
    )
    assert repaired.status_code == 200, repaired.text
    system = (await client.get("/api/admin/system")).json()["secrets"]
    assert system["key_id"] != retired and system["unreadable"] is True

    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    assert await _run_cli("reset") == 0
    printed = capsys.readouterr().out
    assert "nothing to reset" not in printed, printed
    assert retired in printed, printed
    assert "connector_config/tmdb" in printed and "app_setting/push.vapid" in printed, printed

    assert (await client.get("/api/admin/system")).json()["secrets"]["unreadable"] is False
    assert await push_keys.public_key(db) is None, "the pair is gone, so the next boot mints one"
    # `reset` clears only what names unopenable rows; the fresh credential is not one.
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == "a-freshly-issued-jellyfin-key"


async def test_reset_touches_nothing_when_custody_is_intact(
    custody, db, pg_url, monkeypatch, capsys
):
    """A destructive command run by mistake must be a no-op; "intact" is the same unwrap as elsewhere."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("reset") == 0
    assert "nothing to reset" in capsys.readouterr().out
    assert await db.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 1
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == JELLYFIN_KEY


async def test_a_boot_after_reset_mints_the_replacement_keypair_it_promised(
    secrets_key, db, pg_url, tmp_path, monkeypatch, capsys
):
    """`ensure_keypair` short-circuited on the public half, so a reset install never minted a new pair."""
    stale = await push_keys.ensure_keypair(db)
    assert stale is not None
    _use_key(monkeypatch, OTHER_KEY)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("reset") == 0
    assert "app_setting/push.vapid" in capsys.readouterr().out

    async with _boot(monkeypatch, pg_url, tmp_path):
        pass

    fresh = await push_keys.public_key(db)
    assert fresh is not None and fresh != stale, "the boot minted a replacement pair"
    signer = await push_keys.load(db)
    assert signer is not None and signer.public_key == fresh, (
        "and the household holds the half that signs for what the browser subscribes against"
    )
    assert await db.fetchval("SELECT count(*) FROM app_setting WHERE key = 'push.vapid'") == 1


async def test_a_keypair_row_whose_private_half_is_gone_is_not_offered_and_is_replaced(
    secrets_key, db, pg_url, tmp_path, monkeypatch
):
    """The boot decides on the private half: a partial restore reaches this state too, and `load`
    and `public_key` must agree."""
    stale = await push_keys.ensure_keypair(db)
    await db.execute(
        "UPDATE app_setting SET secret = NULL, secret_key_id = NULL WHERE key = $1",
        push_keys.SETTING_KEY,
    )

    assert await push_keys.public_key(db) is None, "a half-row is not an application server key"
    assert await push_keys.load(db) is None

    async with _boot(monkeypatch, pg_url, tmp_path):
        pass

    fresh = await push_keys.public_key(db)
    assert fresh is not None and fresh != stale
    assert (await push_keys.load(db)).public_key == fresh


async def test_a_vapid_row_sealed_before_the_binding_is_re_sealed_by_the_next_boot(
    secrets_key, db
):
    """`ensure_keypair` must re-seal a pre-binding row, or the fallback branch can never be removed.
    Rewound through the app's own writer so it matches a real pre-M4.7 row."""
    public = await push_keys.ensure_keypair(db)
    key_id, dek = await sec.ensure_dek(db)
    aad = sec.aad_for("app_setting", push_keys.SETTING_KEY)
    bound = await db.fetchval(
        "SELECT secret FROM app_setting WHERE key = $1", push_keys.SETTING_KEY
    )
    opened = sec.open_sealed(dek, bound, aad)
    await db.execute(
        "UPDATE app_setting SET secret = $2 WHERE key = $1",
        push_keys.SETTING_KEY,
        sec.seal(dek, opened),  # no associated data: what every install predating M4.7 holds
    )

    assert await push_keys.ensure_keypair(db) == public, "a stored pair is never re-minted"

    stored = bytes(await db.fetchval(
        "SELECT secret FROM app_setting WHERE key = $1", push_keys.SETTING_KEY
    ))
    assert sec.open_sealed(dek, stored, aad) == opened, "the same private half, still readable"
    with pytest.raises(InvalidTag):
        # After the re-seal the blob no longer opens without associated data (sec-10).
        sec.open_sealed(dek, stored, None)
    assert (await push_keys.load(db)).public_key == public
