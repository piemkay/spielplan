"""Restores the documented way into the target `docker compose up` produces, then drives the
routes: the degradation is a property of the routes, not of a helper."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
import pytest

from spielplan.backup import nightly
from spielplan.connectors import registry
from spielplan.core import secrets as sec
from spielplan.core import secrets_cli
from spielplan.core.config import settings
from spielplan.db import migrate
from tests.helpers import create_database, drop_database, sibling
from tests.test_backup import _client, _inside_the_container

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"
JELLYFIN_URL = "http://jellyfin.test"
JELLYFIN_KEY = "JF-ADMIN-KEY-UNSCOPED"

# The operator restored the dumps but not the `.env` beside them (§2's warning).
OTHER_KEY = "a-different-secrets-key-not-a-real-one"

# Two members: a degradation that holds for one driven account is not household-wide.
MEMBERS = ("mira", "tom")


def _restore(dump: Path, database_url: str, *, clean: bool) -> subprocess.CompletedProcess:
    """`--no-owner`: the operator may have changed `POSTGRES_USER`; `--clean --if-exists`: the target
    has already booted. `check=False`: the exit code is an assertion."""
    argv = [*_client("pg_restore")]
    if clean:
        argv += ["--clean", "--if-exists"]
    # Addressed for whichever pg_restore `_client` resolved: a container-run one cannot see
    # this host's published port, and a host-run one must not be handed the container's.
    argv += ["--no-owner", "--dbname", _inside_the_container(database_url)]
    with dump.open("rb") as handle:
        return subprocess.run(argv, stdin=handle, capture_output=True, timeout=600, check=False)


@contextlib.asynccontextmanager
async def _boot(monkeypatch, database_url: str, data_dir: Path):
    """A whole application lifespan: a second process against a database the first never saw."""
    # NOT translated: this is the app's own asyncpg connection from this host; only a
    # container-run pg_dump's DSN is addressed from inside.
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    settings.cache_clear()

    from spielplan.app import create_app

    application = create_app()
    opened: list[httpx.AsyncClient] = []

    def make() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        )
        opened.append(client)
        return client

    try:
        async with application.router.lifespan_context(application):
            yield make
    finally:
        for client in opened:
            with contextlib.suppress(Exception):
                await client.aclose()
        settings.cache_clear()


async def _sign_in(make, name: str, password: str) -> httpx.AsyncClient:
    """Through the front door: a surviving cookie would prove the cookie, not the account."""
    client = make()
    signed = await client.post("/api/auth/login", json={"name": name, "password": password})
    assert signed.status_code == 200, signed.text
    return client


@pytest.fixture
async def target(pg_url):
    """A second database: restoring over the source proves nothing."""
    admin, name, url = sibling(pg_url, "_drill")
    await create_database(admin, name)
    try:
        yield url
    finally:
        await drop_database(admin, name)


@pytest.fixture
async def installed(secrets_key, db, pg_url, tmp_path, monkeypatch):
    """`secrets_key` precedes the boot: the lifespan mints the DEK, and a later key was never the
    install's."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned, jellyfin_id, overview)
        SELECT i, 'movie', 'Title ' || i, true, 'jf-' || i, 'A film about ' || i
          FROM generate_series(1, 20) AS i
        """
    )
    members: list[dict[str, object]] = []
    async with _boot(monkeypatch, pg_url, tmp_path / "source") as make:
        admin = make()
        created = await admin.post(
            "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
        )
        assert created.status_code == 201, created.text
        for index, name in enumerate(MEMBERS):
            account = await admin.post("/api/admin/users", json={"name": name, "role": "member"})
            assert account.status_code == 201, account.text
            otp = account.json()["one_time_password"]
            user_id = account.json()["id"]
            # §3.1's forced first-login change, before the backup: a locked account reaches no member write.
            phone = await _sign_in(make, name, otp)
            changed = await phone.post(
                "/api/auth/password",
                json={"current_password": otp, "new_password": MEMBER_PASSWORD},
            )
            assert changed.status_code == 200, changed.text
            # Decision 117 gates the rail line where Rate says why a push did not happen; without it the
            # assertions below would be vacuous.
            await db.execute("UPDATE app_user SET show_model = true WHERE id = $1", user_id)
            event_id = await db.fetchval(
                "INSERT INTO playback_event (source, title_id, user_id, finished) "
                "VALUES ('jellyfin', $2, $1, true) RETURNING id",
                user_id,
                index + 2,
            )
            members.append(
                {"name": name, "id": user_id, "event_id": event_id, "title_id": index + 2}
            )
        saved = await admin.put(
            "/api/admin/connectors/jellyfin",
            json={"url": JELLYFIN_URL, "api_key": JELLYFIN_KEY},
        )
        assert saved.status_code == 200 and saved.json()["configured"] is True

    facts = await db.fetchrow(
        """
        SELECT k.key_id,
               c.secrets_encrypted,
               (SELECT value ->> 'public_key' FROM app_setting WHERE key = 'push.vapid') AS vapid
          FROM data_encryption_key k
          JOIN connector_config c ON c.secrets_key_id = k.key_id
         WHERE c.name = $1
        """,
        registry.JELLYFIN,
    )
    assert facts is not None and facts["vapid"], "the household under test is not configured"

    monkeypatch.setattr(nightly, "PG_DUMP", _client("pg_dump"))
    dump = tmp_path / nightly.dump_name(datetime.now(UTC))
    # Addressed for whichever pg_dump `_client` resolved: the container cannot see this
    # host's published port. See `_inside_the_container`.
    assert nightly.dump(_inside_the_container(pg_url), dump) > 0

    return {
        "dump": dump,
        "members": members,
        "key_id": facts["key_id"],
        "ciphertext": bytes(facts["secrets_encrypted"]),
        "vapid": facts["vapid"],
    }


async def _first_boot(monkeypatch, target: str, tmp_path: Path) -> dict[str, str]:
    """Returns the fresh install's DEK and VAPID key, so assertions can say "the source's"."""
    async with _boot(monkeypatch, target, tmp_path / "target") as make:
        state = await make().get("/api/setup/state")
        assert state.status_code == 200, state.text
    conn = await asyncpg.connect(target)
    try:
        return {
            "key_id": await conn.fetchval(
                "SELECT key_id FROM data_encryption_key WHERE retired_at IS NULL"
            ),
            "vapid": await conn.fetchval(
                "SELECT value ->> 'public_key' FROM app_setting WHERE key = 'push.vapid'"
            ),
        }
    finally:
        await conn.close()


@pytest.mark.parametrize("booted", [True, False])
async def test_the_documented_restore_carries_custody_and_the_app_writes_through_the_route(
    installed, target, tmp_path, monkeypatch, booted
):
    """`booted=True` is the target an operator actually has, holding its own DEK and VAPID rows;
    `booted=False` is a database nothing has touched."""
    before = await _first_boot(monkeypatch, target, tmp_path) if booted else {}

    done = _restore(installed["dump"], target, clean=True)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-4000:]

    conn = await asyncpg.connect(target)
    try:
        assert await migrate.apply_all(conn) == [], (
            "the restored schema_migration table is not this build's; the upgrade drill and "
            "this one would then be testing different schemas"
        )
        row = await conn.fetchrow(
            "SELECT config ->> 'url' AS url, secrets_encrypted, secrets_key_id "
            "FROM connector_config WHERE name = $1",
            registry.JELLYFIN,
        )
        assert row is not None, "the restore lost connector config (finding 18)"
        assert row["url"] == JELLYFIN_URL
        assert bytes(row["secrets_encrypted"]) == installed["ciphertext"]
        assert row["secrets_key_id"] == installed["key_id"]
        assert await conn.fetchval(
            "SELECT count(*) FROM data_encryption_key WHERE key_id = $1", installed["key_id"]
        ) == 1
        vapid = await conn.fetchval(
            "SELECT value ->> 'public_key' FROM app_setting WHERE key = 'push.vapid'"
        )
        assert vapid == installed["vapid"], (
            "every push_subscription in this dump is bound to that key; a restore that kept the "
            "fresh install's keypair silently unsubscribes every phone in the household"
        )
        if booted:
            assert before["key_id"] != installed["key_id"] and before["vapid"] != vapid, (
                "this target never had custody of its own, so the assertions above are free"
            )
    finally:
        await conn.close()

    member = installed["members"][0]
    async with _boot(monkeypatch, target, tmp_path / "restored") as make:
        phone = await _sign_in(make, member["name"], MEMBER_PASSWORD)
        admin = await _sign_in(make, "patrick", ADMIN_PASSWORD)

        connector = await admin.get("/api/admin/connectors/jellyfin")
        assert connector.status_code == 200, connector.text
        assert connector.json()["configured"] is True
        assert connector.json()["secrets_unreadable"] is False

        state = await phone.post("/api/titles/1/state", json={"state": "seen"})
        assert state.status_code == 200, state.text
        assert state.json()["state"] == "seen"

    conn = await asyncpg.connect(target)
    try:
        assert await conn.fetchval(
            "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", member["id"]
        ) == "seen"
    finally:
        await conn.close()


async def test_the_command_the_compose_file_used_to_give_loses_custody_and_says_so_in_its_exit(
    installed, target, tmp_path, monkeypatch
):
    """The negative control: without the flags `connector_config` restores empty and the VAPID key
    stays the fresh one, and nothing says so."""
    before = await _first_boot(monkeypatch, target, tmp_path)

    done = _restore(installed["dump"], target, clean=False)

    assert done.returncode != 0, "the plain restore is expected to fail; it stopped failing"
    assert b"errors ignored on restore" in done.stderr, done.stderr[-2000:]

    conn = await asyncpg.connect(target)
    try:
        assert await conn.fetchval("SELECT count(*) FROM connector_config") == 0
        assert await conn.fetchval(
            "SELECT key_id FROM data_encryption_key WHERE retired_at IS NULL"
        ) == before["key_id"]
        assert await conn.fetchval(
            "SELECT value ->> 'public_key' FROM app_setting WHERE key = 'push.vapid'"
        ) == before["vapid"]
        # And the part that makes it dangerous rather than merely broken: it looks fine.
        assert await conn.fetchval("SELECT count(*) FROM title") == 20
        assert await conn.fetchval("SELECT count(*) FROM app_user") == 3
    finally:
        await conn.close()


async def test_the_same_dump_under_a_changed_secrets_key_boots_and_every_member_write_commits(
    installed, target, tmp_path, monkeypatch, caplog
):
    """Zero 500s anywhere: a 500 here is `InvalidTag` reaching a phone as "database error"."""
    assert _restore(installed["dump"], target, clean=True).returncode == 0
    monkeypatch.setenv("SECRETS_KEY", OTHER_KEY)
    settings.cache_clear()

    with caplog.at_level(logging.ERROR, logger="spielplan"):
        async with _boot(monkeypatch, target, tmp_path / "restored") as make:
            # One ERROR at boot naming the variable and key_id, on a container that came up.
            assert any(
                "SECRETS_KEY" in record.getMessage() and installed["key_id"] in record.getMessage()
                for record in caplog.records
            ), [r.getMessage() for r in caplog.records]

            admin = await _sign_in(make, "patrick", ADMIN_PASSWORD)
            connector = await admin.get("/api/admin/connectors/jellyfin")
            assert connector.status_code == 200, connector.text
            assert connector.json()["secrets_unreadable"] is True
            assert connector.json()["configured"] is False
            # The URL survives: "re-enter the key" is only actionable if the admin sees which server.
            assert connector.json()["url"] == JELLYFIN_URL

            answered: list[tuple[str, str, int]] = []
            for member in installed["members"]:
                phone = await _sign_in(make, str(member["name"]), MEMBER_PASSWORD)

                state = await phone.post("/api/titles/1/state", json={"state": "seen"})
                assert state.status_code == 200, state.text
                assert state.json()["synced"] is False
                assert state.json()["reason"] == registry.SECRETS_UNREADABLE_REASON

                opened = await phone.post("/api/rate/session", json={"mode": "sweep"})
                assert opened.status_code == 200, opened.text
                card = (await phone.get("/api/rate")).json()["card"]
                verdict = await phone.post(
                    "/api/rate/verdict", json={"card_token": card["token"], "value": 2}
                )
                assert verdict.status_code == 200, verdict.text
                assert any("SECRETS_KEY" in line for line in verdict.json()["log"]), (
                    verdict.json()["log"]
                )

                nxt = verdict.json()["card"]
                not_seen = await phone.post("/api/rate/not-seen", json={"card_token": nxt["token"]})
                assert not_seen.status_code == 200, not_seen.text
                assert any("SECRETS_KEY" in line for line in not_seen.json()["log"]), (
                    not_seen.json()["log"]
                )

                prompt = await phone.post(
                    f"/api/prompts/finish/{member['event_id']}", json={"finished": True}
                )
                assert prompt.status_code == 200, prompt.text
                assert prompt.json()["sync"]["synced"] is False
                assert prompt.json()["sync"]["reason"] == registry.SECRETS_UNREADABLE_REASON

                for method, path in (
                    ("GET", "/api/auth/me"),
                    ("GET", "/api/rate"),
                    ("GET", "/api/titles/1/state"),
                ):
                    reply = await phone.request(method, path)
                    answered.append((method, path, reply.status_code))

            for method, path in (
                ("GET", "/api/admin/users"),
                ("GET", "/api/admin/connectors/jellyfin"),
                ("POST", "/api/admin/connectors/jellyfin/test"),
                ("GET", "/api/admin/connectors/jellyfin/users"),
                ("POST", "/api/admin/connectors/jellyfin/sync"),
                ("POST", "/api/admin/connectors/jellyfin/poll"),
            ):
                reply = await admin.request(method, path)
                answered.append((method, path, reply.status_code))

    assert [row for row in answered if row[2] == 500] == [], answered

    conn = await asyncpg.connect(target)
    try:
        # §3.3: the tap is kept; the reason reports on Jellyfin, not on the person's state.
        for member in installed["members"]:
            assert await conn.fetchval(
                "SELECT count(*) FROM user_title WHERE user_id = $1 AND state = 'seen' "
                "AND title_id IN (1, $2)",
                member["id"],
                member["title_id"],
            ) == 2
    finally:
        await conn.close()


async def test_reset_then_a_put_with_a_new_api_key_ends_the_drill_on_a_working_connector(
    installed, target, tmp_path, monkeypatch, capsys
):
    """`reset` retires (not deletes) what this key cannot open, so the PUT can mint a fresh DEK."""
    assert _restore(installed["dump"], target, clean=True).returncode == 0
    monkeypatch.setenv("SECRETS_KEY", OTHER_KEY)
    settings.cache_clear()

    async with _boot(monkeypatch, target, tmp_path / "restored") as make:
        # In a thread because `main` calls `asyncio.run`; through `main` because the runbook names it.
        assert await asyncio.to_thread(secrets_cli.main, ["reset"]) == 0
        printed = capsys.readouterr().out
        assert installed["key_id"] in printed, printed
        assert f"connector_config/{registry.JELLYFIN}" in printed, printed
        assert "app_setting/push.vapid" in printed, printed

        admin = await _sign_in(make, "patrick", ADMIN_PASSWORD)
        put = await admin.put(
            "/api/admin/connectors/jellyfin",
            json={"url": JELLYFIN_URL, "api_key": "a-freshly-issued-jellyfin-key"},
        )
        assert put.status_code == 200, put.text
        assert put.json()["configured"] is True

        got = await admin.get("/api/admin/connectors/jellyfin")
        assert got.status_code == 200 and got.json()["has_api_key"] is True
        assert got.json()["secrets_unreadable"] is False

    # A bare connection hands jsonb back as text; `get_connector_secrets` expects a dict.
    conn = await asyncpg.connect(target)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    try:
        _config, secret = await sec.get_connector_secrets(conn, registry.JELLYFIN)
        assert secret["api_key"] == "a-freshly-issued-jellyfin-key"
        assert await conn.fetchval(
            "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM data_encryption_key WHERE key_id = $1", installed["key_id"]
        ) == 1, "the unreadable row was deleted rather than retired"
    finally:
        await conn.close()


async def test_a_restored_install_with_no_model_bundle_says_so_on_the_title_card(
    installed, target, tmp_path, monkeypatch
):
    """The seed marker is 'superseded', never 'active', as `backup/movie_data.py` writes it. The
    reason string is asserted: §3.1 asks for an explicit state."""
    assert _restore(installed["dump"], target, clean=True).returncode == 0

    conn = await asyncpg.connect(target)
    try:
        await conn.execute(
            "INSERT INTO artifact_bundle (version, manifest, state, kind) "
            "VALUES ('v20260828', '{}'::jsonb, 'superseded', 'seed')"
        )
    finally:
        await conn.close()

    async with _boot(monkeypatch, target, tmp_path / "restored") as make:
        # DATA_DIR is this test's own, so the artifacts tree is absent on disk too.
        config = await make().get("/api/config")
        assert config.status_code == 200, config.text
        assert config.json()["has_bundle"] is False, config.text

        member = installed["members"][0]
        phone = await _sign_in(make, str(member["name"]), MEMBER_PASSWORD)
        # The model line is Show the model's (decision 486), so the member asks for it first.
        shown = await phone.post("/api/auth/preferences", json={"show_model": True})
        assert shown.status_code == 200, shown.text
        card = await phone.get("/api/titles/1")
        assert card.status_code == 200, card.text

        body = card.json()
        assert body["title"]["name"] == "Title 1"
        assert body["title"]["is_owned"] is True

        line = body["model_line"]
        assert line["available"] is False, line
        assert line["reason"] == "no artifact bundle imported", line
        assert set(line) == {"available", "reason"}, line
