"""Account linking through the admin routes. Spec v2.1 §3.3, §6.6, §7.3, §14.3.

"Admin view maps each app user <-> one Jellyfin user (`GET /Users`), **optional, one-to-one**.
… Authentication is **never** delegated to Jellyfin (the app must work when Jellyfin is down)."

The whole app is pointed at `ops/fake_jellyfin.py` by replacing one function
(`registry.make_client`), so these exercise the real routes end to end — including the parts
that only exist over HTTP, like the API key never coming back out of a GET.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient

PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"


@pytest.fixture
async def admin(secrets_key, app, fake_jellyfin, monkeypatch):
    """An admin session with the app's Jellyfin client pointed at the in-process fake."""
    module, transport = fake_jellyfin

    monkeypatch.setattr(
        registry,
        "make_client",
        lambda cfg: (
            JellyfinClient(cfg.url, cfg.api_key, transport=transport) if cfg.configured else None
        ),
    )

    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    await client.post("/api/setup/members", json={"name": "jenny", "role": "member"})
    return client, module


async def _configure(client, module) -> None:
    response = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": module.API_KEY},
    )
    assert response.status_code == 200


async def _users(client) -> dict[str, dict]:
    return {u["name"]: u for u in (await client.get("/api/admin/users")).json()}


# --- §6.6: configuring the connector ------------------------------------------------------


async def test_the_connector_starts_unconfigured_and_says_so(admin):
    client, _module = admin
    body = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert body == {"url": "", "has_api_key": False, "configured": False,
                    "library_ids": [], "linked_users": 0}


async def test_the_api_key_never_comes_back_out(admin):
    """§14.3: the key is admin-equivalent on the whole media server. A GET that returned it
    would turn every admin session into a copy of it."""
    client, module = admin
    await _configure(client, module)
    body = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert body["has_api_key"] is True
    assert module.API_KEY not in str(body)


async def test_editing_the_url_does_not_blank_the_key(admin):
    """The form shows a mask, so an empty field means "leave it alone" — otherwise correcting
    a typo in the address silently disconnects the server."""
    client, module = admin
    await _configure(client, module)
    await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test/", "api_key": ""},
    )
    body = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert body["has_api_key"] is True
    assert body["url"] == "http://jellyfin.test", "the trailing slash is normalised away"


async def test_the_test_button_reports_the_server_and_the_version_pin(admin):
    client, module = admin
    await _configure(client, module)
    probe = (await client.post("/api/admin/connectors/jellyfin/test")).json()
    assert probe["ok"] is True
    assert probe["server_name"] == "Fake Jellyfin"
    assert probe["supported"] is True


async def test_routes_that_need_jellyfin_refuse_cleanly_when_it_is_unconfigured(admin):
    client, _module = admin
    for path in ("/api/admin/connectors/jellyfin/test", "/api/admin/connectors/jellyfin/users"):
        response = await (
            client.post(path) if path.endswith("test") else client.get(path)
        )
        assert response.status_code == 409
        assert "not configured" in response.json()["detail"]


# --- §3.3: the mapping ----------------------------------------------------------------------


async def test_jellyfin_users_are_listed_for_the_mapping_table(admin):
    client, module = admin
    await _configure(client, module)
    listed = (await client.get("/api/admin/connectors/jellyfin/users")).json()
    assert {u["name"] for u in listed} == {"patrick", "jenny"}


async def test_a_link_without_credentials_is_real_but_incomplete(admin):
    """§7.3's least-privilege path costs "one-time password entry per linked user". Until that
    happens the link attributes playback and cannot write Played state."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)

    response = await client.post(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin",
        json={"jellyfin_user_id": PATRICK_JF},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "user_id": users["patrick"]["id"], "jellyfin_user_id": PATRICK_JF,
        "has_token": False, "state": "needs_relink",
    }
    assert (await _users(client))["patrick"]["has_jellyfin_token"] is False


async def test_linking_with_credentials_stores_that_users_own_token(admin):
    client, module = admin
    await _configure(client, module)
    users = await _users(client)

    response = await client.post(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin",
        json={
            "jellyfin_user_id": PATRICK_JF,
            "jellyfin_username": "patrick",
            "jellyfin_password": module.PASSWORD,
        },
    )
    assert response.json()["has_token"] is True
    after = await _users(client)
    assert after["patrick"]["jellyfin_link_state"] == "linked"
    assert after["patrick"]["has_jellyfin_token"] is True
    assert (await client.get("/api/admin/connectors/jellyfin")).json()["linked_users"] == 1


async def test_credentials_for_a_different_jellyfin_user_are_refused(admin):
    """Signing in as jenny and mapping the row to patrick's Jellyfin id would store a token
    that can only ever write the wrong person's state."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)

    response = await client.post(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin",
        json={
            "jellyfin_user_id": PATRICK_JF,
            "jellyfin_username": "jenny",
            "jellyfin_password": module.PASSWORD,
        },
    )
    assert response.status_code == 400
    assert "different Jellyfin user" in response.json()["detail"]
    assert (await _users(client))["patrick"]["jellyfin_user_id"] is None


async def test_a_wrong_jellyfin_password_is_refused_and_links_nothing(admin):
    client, module = admin
    await _configure(client, module)
    users = await _users(client)
    response = await client.post(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin",
        json={
            "jellyfin_user_id": PATRICK_JF,
            "jellyfin_username": "patrick",
            "jellyfin_password": "wrong",
        },
    )
    assert response.status_code == 401
    assert (await _users(client))["patrick"]["jellyfin_user_id"] is None


async def test_one_jellyfin_user_cannot_be_linked_to_two_accounts(admin):
    """§3.3: one-to-one, held by the partial unique index rather than by a lookup — two admins
    linking at once would both pass a lookup."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)

    first = await client.post(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin",
        json={"jellyfin_user_id": PATRICK_JF},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/admin/users/{users['jenny']['id']}/jellyfin",
        json={"jellyfin_user_id": PATRICK_JF},
    )
    assert second.status_code == 409
    assert "one-to-one" in second.json()["detail"]
    assert (await _users(client))["jenny"]["jellyfin_user_id"] is None


async def test_one_account_maps_to_at_most_one_jellyfin_user(admin):
    """Relinking replaces; it never accumulates."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)
    for jf_id in (PATRICK_JF, JENNY_JF):
        assert (
            await client.post(
                f"/api/admin/users/{users['patrick']['id']}/jellyfin",
                json={"jellyfin_user_id": jf_id},
            )
        ).status_code == 200
    assert (await _users(client))["patrick"]["jellyfin_user_id"] == JENNY_JF


async def test_linking_an_unknown_account_is_a_404(admin):
    client, module = admin
    await _configure(client, module)
    response = await client.post(
        "/api/admin/users/999999/jellyfin", json={"jellyfin_user_id": PATRICK_JF}
    )
    assert response.status_code == 404


async def test_unlinking_leaves_a_working_account(admin):
    """§3.3: the link is optional, so removing it must not break anything."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)
    await client.post(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin",
        json={
            "jellyfin_user_id": PATRICK_JF,
            "jellyfin_username": "patrick",
            "jellyfin_password": module.PASSWORD,
        },
    )
    assert (await client.delete(
        f"/api/admin/users/{users['patrick']['id']}/jellyfin"
    )).status_code == 200

    after = await _users(client)
    assert after["patrick"]["jellyfin_user_id"] is None
    assert after["patrick"]["jellyfin_link_state"] is None
    assert after["patrick"]["has_jellyfin_token"] is False
    assert (await client.get("/api/auth/me")).status_code == 200


# --- §3.3: authentication is never delegated to Jellyfin -------------------------------------


async def test_signing_in_works_while_jellyfin_is_unreachable(secrets_key, app, monkeypatch):
    """The promise that decides the whole connector design: the app must work when Jellyfin is
    down. Nothing on the auth path may touch it."""
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient("http://127.0.0.1:1", "key", timeout=0.2),
    )
    client = app()
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-pass"})
    await client.post("/api/auth/logout")

    signed_in = await client.post(
        "/api/auth/login", json={"name": "patrick", "password": "an-admin-pass"}
    )
    assert signed_in.status_code == 200
    assert (await client.get("/api/auth/me")).json()["jellyfin"] == {
        "linked": False, "state": None
    }


async def test_sync_now_reports_cleanly_with_nothing_linked(admin):
    client, module = admin
    await _configure(client, module)
    report = (await client.post("/api/admin/connectors/jellyfin/sync")).json()
    assert report["skipped_no_link"] is True


async def test_re_mapping_without_credentials_drops_the_old_token(admin):
    """A token belongs to one Jellyfin identity. Keeping it across a re-map would send the
    previous user's credential with the new user's id — which fails, but only after the app
    has tried."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)
    user_id = users["patrick"]["id"]

    await client.post(
        f"/api/admin/users/{user_id}/jellyfin",
        json={
            "jellyfin_user_id": PATRICK_JF,
            "jellyfin_username": "patrick",
            "jellyfin_password": module.PASSWORD,
        },
    )
    assert (await _users(client))["patrick"]["has_jellyfin_token"] is True

    await client.post(
        f"/api/admin/users/{user_id}/jellyfin", json={"jellyfin_user_id": JENNY_JF}
    )
    after = (await _users(client))["patrick"]
    assert after["jellyfin_user_id"] == JENNY_JF
    assert after["has_jellyfin_token"] is False
    assert after["jellyfin_link_state"] == "needs_relink"
