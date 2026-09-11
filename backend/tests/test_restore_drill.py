"""The restore drill. Spec v2.1 §2 (Backups), §3.1, §3.3, §10; decision 181;
docs/milestones/M4.7-plan.md §2 findings 18 and 22, §6 exit criteria 1 and 2.

§2 promises a nightly dump and, one clause later, that the operator can get their household
back from it. Nothing in this repository ever performed that second half. The M0 row said it
did: `test_backup.py::test_a_dump_restored_without_secrets_key_leaves_connector_config_undecryptable`
restores into `blank_url`, whose own docstring says it exists because "a migrated one already
has them" — a database with no schema, which is not the target any operator has. It never runs
`migrate.apply_all` on the restored `schema_migration` table, never starts the lifespan, never
touches a route, and never tries the changed-key branch. Two failure modes shipped past it:

  * the only documented command (`pg_restore -d ... /backups/<dump>`, the compose file's own
    comment until M4.7) exits 1 against the target `docker compose up` produces, with
    `connector_config` empty and the *fresh install's* VAPID keypair still in `app_setting` —
    an install that boots, accepts logins, reports Jellyfin as unconfigured and holds a public
    key none of the household's phones is subscribed against;
  * restored under a `SECRETS_KEY` that is not the one from that night, every member write and
    both admin connector routes answered 500, including the PUT that would have repaired it.

So this file restores the way the compose comment and README now say to, into the target they
name, and then **drives the routes**. That is the whole methodological point (plan §8): the
degradation dd03 asks for is a property of `POST /api/titles/{id}/state`, not of
`registry.load_jellyfin`, and a test of the helper would certify the fix the way the M0 row
certified the bug.

`pg_dump`/`pg_restore` come from the postgres:16 container when the box has no libpq client,
through `tests/test_backup.py`'s resolver — one definition of "how this machine reaches a
`pg_dump`", because two would drift and the second would be the one that silently skips.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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
from tests.test_backup import _client, _drop, _inside_the_container, _recreate, _sibling

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"
JELLYFIN_URL = "http://jellyfin.test"
JELLYFIN_KEY = "JF-ADMIN-KEY-UNSCOPED"

# The operator who restored the dumps and not the `.env` beside them, or who regenerated the
# variable — §2 warns about exactly this and until M4.7 the warning had no landing place.
OTHER_KEY = "a-different-secrets-key-not-a-real-one"

# Two, because §6's exit criterion says two: a degradation that happens to hold for the account
# the drill happens to drive is not a household-wide property.
MEMBERS = ("mira", "tom")


def _restore(dump: Path, database_url: str, *, clean: bool) -> subprocess.CompletedProcess:
    """`pg_restore` as the operator runs it, with and without the two flags that matter.

    `--no-owner` because §2's restore target is a `docker compose up` whose `POSTGRES_USER` the
    operator is free to have changed; `--clean --if-exists` because the target has already
    booted. `check=False`: the exit code is one of the assertions.
    """
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
    """One whole application lifespan against `database_url` — a container start.

    The `app` fixture cannot serve: every assertion here is about what a *second* process does
    against a database the first one never saw, or about a boot under a different key. Yields a
    client factory, so one test can hold an admin session and two member sessions at once.
    """
    # NOT translated: this is the APP's connection, made from this host by asyncpg, and
    # 127.0.0.1:5432 from here is a different checkout's database entirely. Only the DSN handed
    # to a container-run pg_dump is addressed from inside; see _inside_the_container.
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
    """A phone opening the app after the restore, through the front door.

    Not the cookie the source install issued: the restored `auth_session` row and the same
    `SESSION_SECRET` would carry it, but then the drill would prove a cookie survived rather
    than that the account did.
    """
    client = make()
    signed = await client.post("/api/auth/login", json={"name": name, "password": password})
    assert signed.status_code == 200, signed.text
    return client


@pytest.fixture
async def target(pg_url):
    """The database the dump is restored into, next to the test one and dropped after.

    A second database rather than the source: restoring over the database the dump came from
    proves nothing at all, which is the shape of the hole this file closes.
    """
    admin, name, url = _sibling(pg_url, "_drill")
    await _recreate(admin, name)
    try:
        yield url
    finally:
        await _drop(admin, name)


@pytest.fixture
async def installed(secrets_key, db, pg_url, tmp_path, monkeypatch):
    """A household as it stands on the night of the backup, and the dump taken from it.

    Everything the restore has to carry is here and each piece is here for a reason finding 18
    names: the sealed connector credential (`connector_config` + `data_encryption_key`, whose
    foreign key is what a plain restore fails on), the VAPID keypair (`app_setting`, whose
    primary key is the other failure), two member accounts created the only way M4.6 allows,
    and a playback event each so the finish prompt has something to answer.

    `secrets_key` precedes the boot deliberately: the lifespan mints the DEK, and a key set
    afterwards is a key the install never had.
    """
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
            # §3.1's forced first-login change, done before the backup: an account still locked
            # to it can reach four routes and none of them is a member write.
            phone = await _sign_in(make, name, otp)
            changed = await phone.post(
                "/api/auth/password",
                json={"current_password": otp, "new_password": MEMBER_PASSWORD},
            )
            assert changed.status_code == 200, changed.text
            # Decision 117 gates §6.7's rail behind a per-user toggle, and the rail line is
            # where the Rate surface reports why a push did not happen. Without it the reason
            # is redacted and the assertions below would be vacuous.
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
    """`docker compose up` against an empty database: the state every restore lands in.

    It mints a DEK and a VAPID keypair of its own, and those two rows are what a restore has to
    replace. Returning them is what lets the assertions be "the source's, not this install's"
    rather than merely "present".
    """
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


# --- exit criterion 1: the README procedure, into the target the operator has ------------------


@pytest.mark.parametrize("booted", [True, False])
async def test_the_documented_restore_carries_custody_and_the_app_writes_through_the_route(
    installed, target, tmp_path, monkeypatch, booted
):
    """§2's restore, run as README's Recovery block runs it, and then used.

    Both targets, because the two documents describe the same command against different
    databases and both have to hold. `booted=True` is the one that has ever failed and the one
    an operator actually has — `docker compose up` is what produces a database before anyone
    can restore anything, and it arrives holding its own DEK row and its own VAPID keypair.
    `booted=False` is README's advice taken literally: a database nothing has touched.

    The pass conditions are §6's, in its order: `pg_restore` exit 0; `apply_all` applies
    **zero** migrations over the restored `schema_migration`; the connector row, the DEK row and
    the VAPID public key are the *source's* rather than this install's; the app boots; and a
    member's own seen-state write answers 200 over HTTP.
    """
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
    """The negative control, so the two flags above are load-bearing rather than decorative.

    `docker compose exec db pg_restore -d ... /backups/<dump>` was the sole restore instruction
    in this repository until M4.7. Against the target `docker compose up` produces it exits
    non-zero with hundreds of ignored errors, and the two that matter are silent in the result:
    `connector_config`'s COPY fails on its foreign key to a `data_encryption_key` row that
    cannot be inserted over the fresh install's, so the table ends **empty**, and
    `app_setting`'s COPY fails on the primary key, so the push keypair stays the one no phone
    is subscribed against. `get_connector_secrets` returns `{}` for a missing row rather than
    raising, so the install reports Jellyfin as merely unconfigured and nothing says anything
    was lost.
    """
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


# --- exit criterion 2: the same dump under a changed SECRETS_KEY -------------------------------


async def test_the_same_dump_under_a_changed_secrets_key_boots_and_every_member_write_commits(
    installed, target, tmp_path, monkeypatch, caplog
):
    """§2's warning, arriving as an operator meets it: the dumps were copied, the `.env` was not.

    §3.1 keeps a half-configured boot legal and §3.3 makes the app-side write independent of
    Jellyfin, so the whole matrix is 200s with a truthful reason — for **both** members, over
    the four routes a member's tap reaches. The one number that is not negotiable is the last:
    zero 500s anywhere, because a 500 here is `cryptography`'s `InvalidTag` reaching a phone as
    "database error", and it used to include the admin PUT that would have fixed it.
    """
    assert _restore(installed["dump"], target, clean=True).returncode == 0
    monkeypatch.setenv("SECRETS_KEY", OTHER_KEY)
    settings.cache_clear()

    with caplog.at_level(logging.ERROR, logger="spielplan"):
        async with _boot(monkeypatch, target, tmp_path / "restored") as make:
            # The operator's first chance to learn: one ERROR at boot naming the variable and
            # the key_id, on a container that came up rather than one that crash-looped.
            assert any(
                "SECRETS_KEY" in record.getMessage() and installed["key_id"] in record.getMessage()
                for record in caplog.records
            ), [r.getMessage() for r in caplog.records]

            admin = await _sign_in(make, "patrick", ADMIN_PASSWORD)
            connector = await admin.get("/api/admin/connectors/jellyfin")
            assert connector.status_code == 200, connector.text
            assert connector.json()["secrets_unreadable"] is True
            assert connector.json()["configured"] is False
            # The URL survives, because "re-enter the key" is only actionable if the admin can
            # see which server the household is pointed at.
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
        # §3.3: the tap is kept. Both members, both titles — the reason is a report about
        # Jellyfin, not a refusal of the person's own state.
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
    """The last line of §6's second criterion, and the only way back that does not need psql.

    Every ciphertext in the restored database names a DEK row this `SECRETS_KEY` cannot open,
    and `ensure_dek` unwraps the active row before it can seal anything — so on the install that
    needs it most, re-entering the credential failed with the error it was there to clear.
    `spielplan-secrets reset` retires what it cannot open (retires, not deletes: if the right
    `.env` turns up on a USB stick, every ciphertext naming that key still opens) and clears the
    columns that named it, which is what lets the PUT mint a fresh DEK and seal under it.
    """
    assert _restore(installed["dump"], target, clean=True).returncode == 0
    monkeypatch.setenv("SECRETS_KEY", OTHER_KEY)
    settings.cache_clear()

    async with _boot(monkeypatch, target, tmp_path / "restored") as make:
        # In a thread because `main` calls `asyncio.run`, which refuses to nest — and through
        # `main` rather than its internals because the runbook names the command, not the
        # helper. `settings().database_url` is the restored target, as `docker compose exec`
        # would make it.
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

    # The app's own connection shape: `get_connector_secrets` reads `config` back as a dict,
    # and a bare connection hands jsonb over as text (db/pool.py's codecs are why).
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
