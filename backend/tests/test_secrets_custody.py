"""Secrets custody when the key is wrong, and the two executors that repair it.

Spec v2.1 §2 (Configuration, Backups), §3.1, §3.3, §14.3, §14 risk 3; decision 181;
docs/milestones/M4.7-plan.md §2 findings 1-4.

The defect these tests close was invisible from every layer the suite already had. A wrong or
lost `SECRETS_KEY` boots green: the lifespan completes (the VAPID public half is read without
unwrapping), `/api/health` says `ok: true`, login and `/me` succeed — and then the seen-state
write, the verdict, the not-seen, the finish prompt and both admin connector routes answer 500,
because `cryptography`'s `InvalidTag` is caught nowhere and `app.py`'s only handler is for
`asyncpg.PostgresError`. Worse, `PUT /api/admin/connectors/jellyfin` is on that list, so the one
route that could have re-entered the key dies of the problem it exists to fix.

**These tests drive the routes.** The M0 row that was supposed to cover this proved its property
with a bare asyncpg connection against a database nobody restores into, and shipped both failure
modes past itself. A test of `load_jellyfin` would do the same thing again: the assertion that
matters is that a member's tap answers 200 over HTTP, and the only thing that can say so is HTTP.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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

# The operator regenerated `SECRETS_KEY`, or restored a dump without its .env. Both are the same
# fact to the app, and both are over the config floor `core/config` now enforces.
OTHER_KEY = "a-different-secrets-key-not-a-real-one"


def _use_key(monkeypatch, value: str) -> None:
    """Point the process at a different SECRETS_KEY, the way editing .env and restarting does.

    `settings()` is `lru_cache`d, so the clear is not optional: without it every later read
    would still hold the key the fixture set.
    """
    monkeypatch.setenv("SECRETS_KEY", value)
    settings.cache_clear()


@contextlib.asynccontextmanager
async def _boot(monkeypatch, pg_url, tmp_path):
    """One whole application lifespan against the test database — a container start.

    The `app` fixture cannot serve here: half of these assertions are about what a *second*
    process does, or about a boot under a key the first boot did not have.
    """
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings.cache_clear()

    from spielplan.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def custody(secrets_key, db, app):
    """A configured household: an admin, twenty owned films, a sealed Jellyfin credential.

    `secrets_key` precedes `app` deliberately — the lifespan mints the DEK, so a key set after
    it is a key the first boot never saw and the whole scenario is the wrong one.
    """
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    user_id = (await client.get("/api/auth/me")).json()["id"]
    # Decision 117 gates §6.7's rail behind a per-user toggle, and the rail line is where the
    # Rate surface reports why a push did not happen. Without this the reason is redacted out
    # of the response and the assertion below would be vacuous.
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
    # §6.1's Mix alternates sweep and battle; sweep throughout is what lets one session answer
    # both a verdict and a not-seen, which are two of the four member writes under test.
    assert (await client.post("/api/rate/session", json={"mode": "sweep"})).status_code == 200
    return {"client": client, "user_id": user_id, "event_id": event_id}


@pytest.fixture
async def no_connector_yet(secrets_key, db, app):
    """An install that has a DEK row and has never configured a connector.

    The state every wrong-key test above was blind to. `custody` seals a Jellyfin credential
    first, so `load_jellyfin` always had a ciphertext to fail on and the repair was always armed
    by `secrets_unreadable`; here first boot mints the DEK for the VAPID pair alone
    (`push/keys.ensure_keypair`) and `connector_config` is empty — which is every household that
    has not opened the Connectors card yet, including every fresh install.
    """
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0
    assert await db.fetchval("SELECT count(*) FROM data_encryption_key") == 1
    return client


# --- dd03: the wrong key degrades, and never 500s -------------------------------------------


async def test_every_member_write_still_commits_under_a_changed_secrets_key(
    custody, db, monkeypatch
):
    """§3.3: "the app must work when Jellyfin is down" — and a sealed credential that will not
    open is the same thing one layer down. §3.1 keeps a half-configured boot legal.

    All four writes the finding names, over HTTP: 200, the row committed, `synced` false, and a
    reason naming the variable the admin has to fix rather than "Jellyfin not configured", which
    would be a lie about an install whose Jellyfin is configured.
    """
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
    verdict = await client.post(
        "/api/rate/verdict", json={"card_token": card["token"], "value": 2}
    )
    assert verdict.status_code == 200, verdict.text
    assert any("SECRETS_KEY" in line for line in verdict.json()["log"]), verdict.json()["log"]

    nxt = verdict.json()["card"]
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
    """The six admin reads the finding lists, plus the three that reach Jellyfin.

    409 is a legitimate answer for the three that need a client — "Jellyfin is not configured"
    is what `_client` already says when there is nothing to talk to. 500 is not: it is the
    unhandled `InvalidTag` reaching the transport with `{"detail": "database error"}` on it.
    """
    client = custody["client"]
    _use_key(monkeypatch, OTHER_KEY)

    listed = await client.get("/api/admin/users")
    assert listed.status_code == 200, listed.text

    got = await client.get("/api/admin/connectors/jellyfin")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["secrets_unreadable"] is True
    assert body["configured"] is False and body["has_api_key"] is False
    # The URL survives: the admin has to see which server the household is pointed at in order
    # to know that re-entering the key is all that is missing.
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
    """The recovery path, and the reason it did not exist.

    `save_jellyfin` begins by reading the current row, and `ensure_dek` unwraps the active DEK
    before it can seal anything — so on the install that needs it most the repair route failed
    with exactly the error it was there to clear. Retiring the unreadable row is what lets a
    fresh one be minted; the retired row stays, so every ciphertext naming it still opens if the
    correct .env ever comes back.
    """
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
    """The same repair, on the install that has nothing sealed yet — where it did not exist.

    The repair was derived from `load_jellyfin` reporting `secrets_unreadable`, which it can only
    do for a row that already holds a ciphertext. On an install whose DEK was minted for the
    VAPID pair alone, `save_jellyfin` saw a perfectly ordinary unconfigured connector, called
    `ensure_dek`, and the `SecretsUnreadable` went out through the route with no handler on it at
    all — not even the JSON `{"detail": ...}` shape, because `app.py`'s handlers cover
    `PostgresError` and `HTTPException`. The two facts the admin sees on the way in are asserted
    too, because they are what makes the 500 baffling rather than merely wrong: the Connectors
    card says "not configured" while the System card says custody is broken.
    [M4.7 dd03; docs/milestones/M4.7-plan.md section 2 finding 1]
    """
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


async def test_the_setup_wizards_connector_route_seals_under_a_fresh_dek_too(
    no_connector_yet, db, monkeypatch
):
    """`POST /api/setup/connectors` reaches `put_connector_secrets` on the same page of the
    wizard, takes `AdminUser` and is not gated on setup being incomplete — so it is the second
    front door onto the state above, and an admin who lands on it gets the same 500 unless the
    repair belongs to the DEK rather than to one connector's stored ciphertext."""
    _use_key(monkeypatch, OTHER_KEY)

    posted = await no_connector_yet.post(
        "/api/setup/connectors", json={"name": "tmdb", "secrets": {"api_key": "a-tmdb-key"}}
    )
    assert posted.status_code == 200, posted.text

    _config, secret = await sec.get_connector_secrets(db, "tmdb")
    assert secret["api_key"] == "a-tmdb-key"


async def test_a_seeded_connector_does_not_take_the_boot_down_under_an_unreadable_dek(
    secrets_key, db, pg_url, tmp_path, monkeypatch, caplog
):
    """§3.1 keeps a half-configured boot legal, and the env seed runs before anything else can.

    `seed_from_env` writes any connector with no row yet, unguarded, and `app.py`'s lifespan
    calls it first — so on an install whose DEK will not open the exception escaped the lifespan
    and the container never started. M4.7 is what made it reachable: before step 8 none of the
    six seed variables reached a container at all, and `x-app-env` now forwards every one. The
    operator's repair is `docker compose exec backend spielplan-secrets reset`, which needs a
    container that is up. [M4.7 dd03; docs/milestones/M4.7-plan.md section 2 finding 2]
    """
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
    """The admin corrects the address and has not found the old .env yet.

    `put_connector_secrets(..., secrets=None)` means "this connector has no secret" and NULLs
    both sealed columns — correct when the caller read the secret and carried it forward, and a
    silent deletion when the caller could not read it at all. The ciphertext is unreadable, not
    worthless: the right key may still be on a USB stick.
    """
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
    """The other half of the save above, and the one case where preserving is the wrong answer.

    §14.3 binds the API key and §7.3's per-user tokens to the server that issued them, so a
    changed origin drops both — but the preservation this milestone added for an unreadable DEK
    ran first and kept them, which meant the household's whole credential set survived a save
    that said it had discarded it. Restoring the original .env then handed the old server's
    admin-equivalent key, in a header, to whatever host had been typed in. One gesture reaches
    it: the Connectors card prefills the URL and posts the key field empty, so correcting the
    address while custody is broken is exactly this request.
    """
    client, user_id = custody["client"], custody["user_id"]
    # A token too, because §7.3's credentials travel the same column and the same code path.
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

    # The .env the card and the README tell the admin to look for comes back. Nothing returns
    # with it, which is the point: those credentials were dead to this install the moment the
    # origin changed.
    _use_key(monkeypatch, secrets_key)
    cfg = await registry.load_jellyfin(db)
    assert cfg.url == "http://new-jellyfin.test:8096"
    assert cfg.api_key == "" and cfg.user_tokens == {}
    assert registry.make_client(cfg) is None


async def test_relinking_under_a_wrong_key_is_refused_rather_than_keeping_a_stale_token(
    secrets_key, custody, db, monkeypatch
):
    """§7.3's token belongs to one Jellyfin identity, and an unreadable DEK cannot enumerate it.

    `seen.forget_token` decides whether there is anything to drop by reading `cfg.user_tokens`,
    which `load_jellyfin` reports empty for exactly this install — so the no-password branch of
    the link route answered 200, left the previous identity's token sealed, and re-bound it to
    the new `jellyfin_user_id` as soon as the right .env came back: `seen.linked_users` pairs the
    stored mapping with `token_for` and never reads `link_state`. Refused rather than cleared,
    because that ciphertext also holds the admin key and every other member's token, unreadable
    only until that .env returns (§2) — and the PUT that re-enters the key clears the tokens as
    part of the repair, so the refusal has a cure the admin is already being sent to.
    """
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
    """The branch that did not merely 500 — it stopped the container from starting.

    `ensure_keypair` catches `RuntimeError` for §2's no-SECRETS_KEY refusal, and the bare
    `InvalidTag` sailed straight through it, out of the lifespan, with a traceback that never
    named `SECRETS_KEY`. `SecretsUnreadable` subclassing `RuntimeError` is what fixes it, which
    is why that base class is load-bearing rather than decorative.
    """
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


# --- sec-10: a ciphertext belongs to its row, and there is one DEK row ----------------------


async def test_a_ciphertext_moved_to_another_row_does_not_open(secrets_key, db):
    """§14 risk 3: a leaked Jellyfin key is admin-equivalent on the whole media server.

    The DEK wrap has always used associated data; `seal`/`open_sealed` passed None, so a
    ciphertext was bound to nothing at all. Copying `jellyfin`'s two columns onto the `tmdb` row
    made `get_connector_secrets('tmdb')` hand back the Jellyfin admin key with every CHECK
    constraint satisfied.
    """
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
    """The migration-era fallback in `open_sealed`, and the write that makes it migration-era.

    Every install predating M4.7 holds ciphertexts sealed with no associated data. They must open
    on the next read — a binding that stranded them would turn a security improvement into the
    very data loss it is defending against — and they must not stay unbound afterwards. The
    fallback's comment said they are "re-sealed by the next save", but the only writers of a
    connector row are `seed_from_env` (first boot only), the setup route and an admin save, so a
    household that never reopens the Connectors card would carry an unbound ciphertext for ever
    and the branch could never be deleted. The re-seal happens on the read that opens it, where
    the DEK is already in hand, exactly as `push/keys._rebind_to_its_row` does for the VAPID
    pair. [M4.7 sec-10; docs/milestones/M4.7-plan.md section 2 finding 5]
    """
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
    """sec-10's reproduction, run against the rows M4.7 is actually protecting.

    The move above is asserted over two ciphertexts that `put_connector_secrets` wrote — which
    is to say, over rows sealed *after* the binding existed. Every row on a real install predates
    it, carries no associated data, and opens in whatever row it is copied into: `open_sealed`'s
    fallback said it "cannot weaken the binding above", and that was true only of blobs the fix
    itself had written. The re-seal is what closes it, and this is the assertion that says so —
    one ordinary read of the row, then the same move, and now it fails.
    [M4.7 sec-10; docs/milestones/M4.7-plan.md section 2 finding 5]
    """
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
    """§2 says "the one DEK row". `ensure_dek` was SELECT-then-INSERT with no lock, and three
    concurrent calls on separate connections produced two active rows — harmless for reads, and
    fatal for a rotation executor that has to know which row it is re-wrapping."""
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
    """The M0 row's headline property, asserted at last against a real first boot.

    Three of its four named tests are pure-function tests of `_wrap`/`_unwrap` in a file with no
    `db` fixture, and the fourth inserts a one-byte fake key — so "exactly one random 256-bit
    key" was permanently green and could not see sec-10's race at all.
    """
    rows = await db.fetch("SELECT key_id, wrapped_dek, retired_at FROM data_encryption_key")
    assert len(rows) == 1
    assert rows[0]["retired_at"] is None
    assert len(sec._unwrap(bytes(rows[0]["wrapped_dek"]), secrets_key)) == 32
    assert bytes(rows[0]["wrapped_dek"]) != b"\x00" * len(rows[0]["wrapped_dek"])


async def test_a_second_lifespan_adopts_the_first_boots_key_id(
    secrets_key, db, pg_url, tmp_path, monkeypatch
):
    """A restart must not mint a second key: every ciphertext names a `key_id`, and a second
    active row is the state 0017's partial unique index exists to make impossible."""
    async with _boot(monkeypatch, pg_url, tmp_path):
        pass
    first = await db.fetchval("SELECT key_id FROM data_encryption_key")
    assert first is not None

    async with _boot(monkeypatch, pg_url, tmp_path):
        pass
    assert await db.fetchval("SELECT count(*) FROM data_encryption_key") == 1
    assert await db.fetchval("SELECT key_id FROM data_encryption_key") == first


# --- spec-08: rotation has an executor ------------------------------------------------------


async def _run_cli(*argv: str) -> int:
    """`spielplan-secrets` as an operator runs it, argparse and `asyncio.run` included.

    In a thread because `main` calls `asyncio.run`, which refuses to nest inside the loop pytest
    is already running. Driving `main` rather than its internals is the point: the coverage row
    names the command, and a test of the helper would leave the argument parsing and the exit
    codes — the parts an operator actually meets — unasserted.
    """
    return await asyncio.to_thread(secrets_cli.main, list(argv))


async def test_rewrap_moves_the_wrapping_and_leaves_every_ciphertext_untouched(
    secrets_key, db, pg_url, monkeypatch, capsys
):
    """§2: "rotating SECRETS_KEY is an explicit admin action that re-wraps the one DEK row."

    The whole promise is that the DEK does not change, so no ciphertext has to be rewritten and
    no `key_id` advances. Until now the only implementation was a pure function whose sole caller
    was a unit test, and an operator who followed `.env.example` and edited the variable landed
    in dd03 instead.
    """
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

    # The old key no longer opens it, and the new one does — with the plaintext unchanged, which
    # is the half `platform-key-rotation-semantics` has always claimed and never asserted.
    with pytest.raises(sec.SecretsUnreadable):
        await sec.get_connector_secrets(db, registry.JELLYFIN)
    _use_key(monkeypatch, OTHER_KEY)
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == JELLYFIN_KEY


async def test_rewrap_refuses_when_more_than_one_row_is_un_retired(
    secrets_key, db, pg_url, monkeypatch, capsys
):
    """The guard for the install that lost sec-10's race before 0017 existed.

    The index is dropped here on purpose: on a database that has applied 0017 this state cannot
    be created, and the only install that can reach the executor holding two active rows is one
    where the migration itself refused to apply. Guessing which row is current is a data decision
    a command must not take on the operator's behalf.
    """
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
    """The one executor that WRITES a SECRETS_KEY must hold the floor the validator holds.

    `cs-44` put a 32-character floor on SECRETS_KEY in `core/config`, and this command wrote a
    new wrapping under anything at all and then told the operator to "put the new value in .env
    and restart" — so following its own success message left an app that refuses to start and a
    CLI that refuses to run, both with a `ValidationError`, and a DEK now wrapped under the key
    neither will accept. Same floor, same message, same generator one-liner: a refusal an
    operator meets once is worth more than two that contradict each other.
    [M4.7 cs-44, spec-08; docs/milestones/M4.7-plan.md section 2 finding 4]
    """
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
    """The floor is on the value being written, not on the value being replaced.

    An install that ran M4.6 with `SECRETS_KEY=x` is exactly the install `cs-44`'s floor is
    about, and after the upgrade its app will not boot. Its way out is to generate a real key,
    put it in `.env` so the CLI can construct `Settings` at all, and rewrap from the short one —
    so refusing `--old-key` on length would close the only door out of the state the floor was
    added to end. A wrong `--old-key` is already refused by the unwrap, with the message that
    names SECRETS_KEY. [M4.7 cs-44]
    """
    short = "x"
    dek = b"\x02" * 32
    # Wrapped by hand rather than by `ensure_dek`, because no process can mint this row any more:
    # `Settings` refuses the key that wrapped it, which is precisely the install being rescued.
    await db.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ('a-pre-floor-row', $1)",
        sec._wrap(dek, short),
    )

    # What the operator does before running the command: the real key goes into the environment
    # first, because `spielplan-secrets` constructs `Settings` before it opens a connection.
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
    """The lossy repair, and the only way back on an install whose key is simply gone.

    `rewrap` needs the old key. An operator who regenerated `SECRETS_KEY`, or who restored a dump
    without its .env, does not have it — and without `reset` the sealed columns keep pointing at
    a key nothing can open, which is a permanent 500 in every code path that reads them.
    """
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
    # The whole row for the VAPID pair, not two NULLed columns: the public half left behind is
    # not a smaller pair, it is a key the subscribe screen keeps offering while nothing can sign
    # for it — and `ensure_keypair` short-circuited on exactly that column, so the row that
    # survived this repair blocked every later boot from minting the replacement the message
    # above promises. See the boot test below for the other half of the same fix.
    assert await db.fetchval(
        "SELECT count(*) FROM app_setting WHERE key = 'push.vapid'"
    ) == 0

    # And the card is fillable again: the next save mints a fresh DEK and seals under it.
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
    """The two repairs in the order the UI offers them, which is where `reset` became a no-op.

    The card's repair retires the unreadable row and mints a fresh one, so "which *active* row
    will not open" answers no from that moment on while every other ciphertext — a second
    connector, `app_setting/push.vapid` — is still sealed under the retired one. `reset` asked
    that question and `api/admin`'s System card asked "which key does any sealed row name that
    will not open", so the operator who took the repair the Connectors card offers first and then
    followed its own advice to run `reset` was told "custody is intact" beside a System card
    still reporting `unreadable: true`. Nothing could then bring web-push back: `/api/push/state`
    keeps handing browsers an application server key the household cannot sign for, which is the
    failure `push/keys`' module docstring is written against.

    One question, in `core.secrets`, is the fix — so the card and the command cannot answer
    differently, and a third caller cannot invent a fourth version.
    [M4.7 dd03, ops-11; docs/milestones/M4.7-plan.md section 2 finding 3]
    """
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
    # And the credential the card's repair sealed is untouched: `reset` clears what named the
    # rows it cannot open, and the fresh row is not one of them.
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == "a-freshly-issued-jellyfin-key"


async def test_reset_touches_nothing_when_custody_is_intact(
    custody, db, pg_url, monkeypatch, capsys
):
    """A destructive command run by mistake must be a no-op, not a wipe. The read that decides
    is the same unwrap every other caller does, so "intact" cannot mean something else here."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()

    assert await _run_cli("reset") == 0
    assert "nothing to reset" in capsys.readouterr().out
    assert await db.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 1
    _config, secret = await sec.get_connector_secrets(db, registry.JELLYFIN)
    assert secret["api_key"] == JELLYFIN_KEY


# --- and the repair leaves an install a boot can finish -------------------------------------


async def test_a_boot_after_reset_mints_the_replacement_keypair_it_promised(
    secrets_key, db, pg_url, tmp_path, monkeypatch, capsys
):
    """`reset` says web-push comes back; before this, nothing could ever bring it back.

    The repair cleared the *private* half of `app_setting/push.vapid` while `ensure_keypair`
    short-circuited on the *public* one, so every later boot found a row, minted nothing, and
    `load()` answered None for ever — with `/api/push/state` still handing browsers a key the
    server could no longer sign for, which is the one failure `push/keys`' module docstring is
    written against. The only way out was `DELETE FROM app_setting` by hand, named in no
    document. Reproduced exactly here: install with a pair, change the key, reset, reboot.
    [M4.7 dd03; decision 181]
    """
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
    """The same repair, asserted against the column rather than against `reset`'s SQL.

    `reset` is not the only way a row can lose its sealed half — a partial restore or an
    operator's own UPDATE reaches the same state — so the boot decides on the half that matters
    rather than trusting one command to have removed the whole row. Both readers of the row have
    to agree while it is in that state: `load` has always called it no pair, and `public_key`
    calling it a pair is what let a phone subscribe against a dead key.
    """
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
    """`open_sealed`'s migration-era branch is only temporary if something re-seals the row.

    Its comment promises the row is "re-sealed with it by the next save through
    `put_connector_secrets` / `ensure_keypair`" and says to delete the branch "once no install
    predates M4.7". `ensure_keypair` had no save path for an existing row at all, so for the
    VAPID pair the promise was false for ever and the branch could never be removed. The row is
    rewound here through the app's own writer rather than hand-built, so the test cannot drift
    from the shape a pre-M4.7 install actually holds. [M4.7 sec-10; decision 181]
    """
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
        # The binding, asserted from the other side: after the re-seal the ciphertext no longer
        # opens with no associated data, so a blob moved to another row fails (sec-10) and the
        # fallback branch has one fewer install to keep it alive.
        sec.open_sealed(dek, stored, None)
    assert (await push_keys.load(db)).public_key == public
