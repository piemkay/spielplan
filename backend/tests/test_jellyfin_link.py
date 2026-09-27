"""Account linking through the admin routes (§3.3, §7.3, §14.3), end to end against `ops/fake_jellyfin.py`
behind `registry.make_client`. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.sync import seen

PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"


@pytest.fixture
async def admin(secrets_key, app, fake_jellyfin, monkeypatch):
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
    await client.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    return client, module


async def _configure(client, module) -> None:
    response = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": module.API_KEY},
    )
    assert response.status_code == 200


async def _users(client) -> dict[str, dict]:
    return {u["name"]: u for u in (await client.get("/api/admin/users")).json()}


async def test_the_connector_starts_unconfigured_and_says_so(admin):
    client, _module = admin
    body = (await client.get("/api/admin/connectors/jellyfin")).json()
    # "Nothing configured" and "credentials will not decrypt" are different states; an unprobed server
    # is `server_supported: null`, never a refusal; the webhook token itself appears in one response only.
    assert body == {"url": "", "has_api_key": False, "configured": False,
                    "library_ids": [], "secrets_unreadable": False,
                    "has_webhook_token": False,
                    "server_version": "", "server_supported": None,
                    "trigger": {
                        "webhook": {"last_item_added_at": None, "last_delivery_at": None,
                                    "deliveries_7d": 0, "last_refusal": None},
                        "delta_poll": {"watermark": None, "last_run_at": None,
                                       "last_run_ok": None, "last_ok_at": None,
                                       "last_filed": None},
                    }}


async def test_the_api_key_never_comes_back_out(admin):
    """§14.3: the key is admin-equivalent on the whole media server."""
    client, module = admin
    await _configure(client, module)
    body = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert body["has_api_key"] is True
    assert module.API_KEY not in str(body)


async def test_editing_the_url_does_not_blank_the_key(admin):
    """The form shows a mask, so an empty field means "leave it alone"."""
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
    for path in (
        "/api/admin/connectors/jellyfin/test",
        "/api/admin/connectors/jellyfin/users",
        # An unconfigured connector is 409, not the `ok: false` of a server that refused (decision 364).
        "/api/admin/connectors/jellyfin/libraries",
    ):
        response = await (
            client.post(path) if path.endswith("test") else client.get(path)
        )
        assert response.status_code == 409
        assert "not configured" in response.json()["detail"]


async def test_the_libraries_the_pick_picks_from_are_listed_from_the_server(admin):
    """Read off the fake's own table: the shapes a server sends belong to the double, not the test."""
    client, module = admin
    await _configure(client, module)

    body = (await client.get("/api/admin/connectors/jellyfin/libraries")).json()
    assert body["ok"] is True
    listed = [(lib["id"], lib["name"]) for lib in body["libraries"]]
    assert listed == [(lib["Id"], lib["Name"]) for lib in module.LIBRARIES]
    assert ("jf-lib-home", "Home Videos") in listed, (
        "the library nobody would acquire from is what makes a pick mean anything"
    )
    # The app's own spelling: nothing of Jellyfin's `BaseItemDto` reaches the browser.
    assert all(set(lib) == {"id", "name"} for lib in body["libraries"]), body


async def test_a_server_that_will_not_list_its_libraries_is_reported_rather_than_raised(
    admin, monkeypatch
):
    """An EMPTY pick means "the whole server" (decision 364), so a refusal must never read as `[]`."""
    client, _module = admin
    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": "not-the-admin-key"},
    )
    assert saved.status_code == 200, saved.text

    response = await client.get("/api/admin/connectors/jellyfin/libraries")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ok"], body["status"], body["libraries"]) == (False, 401, [])
    assert body["error"], "a reported failure with nothing to report is a blank card"

    for label, answer in {
        "a portal's sign-in page": httpx.Response(200, text="<html>Sign in</html>"),
        "a bare list": httpx.Response(200, json=[{"Id": "jf-lib-films", "Name": "Films"}]),
    }.items():
        monkeypatch.setattr(
            registry, "make_client",
            lambda cfg, a=answer: JellyfinClient(
                cfg.url, cfg.api_key, transport=httpx.MockTransport(lambda _request: a)
            ),
        )
        answered = await client.get("/api/admin/connectors/jellyfin/libraries")
        assert answered.status_code == 200, f"{label} -> {answered.status_code} {answered.text}"
        reported = answered.json()
        assert (reported["ok"], reported["libraries"]) == (False, []), label
        assert reported["error"], label


async def test_the_library_pick_is_stored_by_the_save_and_reported_by_the_card(admin):
    client, module = admin
    await _configure(client, module)

    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": "", "library_ids": ["jf-lib-shows"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["library_ids"] == ["jf-lib-shows"]

    card = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert card["library_ids"] == ["jf-lib-shows"]
    assert card["has_api_key"] is True, "picking a library blanked the key the form only masked"


async def test_a_partial_save_keeps_the_stored_pick_and_an_explicit_empty_list_widens_it(admin):
    """Absent keeps the pick; `[]` widens it to the whole
    server. One answer for both would lose a gesture."""
    client, module = admin
    await _configure(client, module)
    await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "library_ids": ["jf-lib-films", "jf-lib-shows"]},
    )

    kept = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test/", "api_key": ""},
    )
    assert kept.json()["library_ids"] == ["jf-lib-films", "jf-lib-shows"], (
        "a save that never mentioned the pick blanked it, and blanking it means the whole server"
    )

    widened = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "library_ids": []},
    )
    assert widened.json()["library_ids"] == []
    assert (await client.get("/api/admin/connectors/jellyfin")).json()["library_ids"] == []


async def test_a_library_id_this_column_cannot_hold_is_refused_at_the_edge_and_not_by_postgres(
    admin,
):
    """Postgres holds no NUL in a jsonb string, so the edge refuses it as a 422 before the write 500s."""
    client, module = admin
    await _configure(client, module)

    for label, pick in {
        "a control character": ["a\x00b"],
        "an id longer than any GUID": ["x" * 65],
        "an empty id": [""],
        "more ids than a household has libraries": [f"jf-lib-{n}" for n in range(65)],
    }.items():
        refused = await client.put(
            "/api/admin/connectors/jellyfin",
            json={"url": "http://jellyfin.test", "library_ids": pick},
        )
        assert refused.status_code == 422, f"{label}: {refused.status_code} {refused.text}"

    assert (await client.get("/api/admin/connectors/jellyfin")).json()["library_ids"] == [], (
        "a refused save leaves the stored boundary exactly where the admin left it"
    )


async def test_the_pick_is_stored_once_in_the_servers_spelling_and_never_blank_or_nil(admin):
    """Whitespace binds to a null `Guid?` (the whole server);
    a GUID is stored once, in the server's spelling."""
    client, module = admin
    await _configure(client, module)
    dashed = "6213b704-a0d9-5429-3110-f4d561b0f614"

    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test",
              "library_ids": [dashed, "{" + dashed.upper() + "}", dashed.replace("-", ""),
                              "jf-lib-films"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["library_ids"] == ["6213b704a0d954293110f4d561b0f614", "jf-lib-films"]

    for label, pick in {
        "a blank id": [" "],
        "the nil GUID": ["00000000-0000-0000-0000-000000000000"],
    }.items():
        refused = await client.put(
            "/api/admin/connectors/jellyfin",
            json={"url": "http://jellyfin.test", "library_ids": pick},
        )
        assert refused.status_code == 422, f"{label}: {refused.status_code} {refused.text}"


async def test_a_save_the_connectors_page_sends_mints_no_token_nobody_would_see(admin, db):
    """The page's `save()` discards the answer, so a mint on every PUT sealed a token nobody ever saw."""
    client, module = admin

    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": module.API_KEY},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["webhook_token"] is None
    assert (await registry.load_jellyfin(db)).webhook_token == "", "a token nobody was shown"
    assert (await client.get("/api/admin/connectors/jellyfin")).json()["has_webhook_token"] is False


async def test_a_url_no_request_can_be_sent_to_is_saved_rather_than_answered_500(admin, db):
    """`httpx.InvalidURL` is not an `httpx.HTTPError`; the probe is best-effort after the save commits."""
    client, module = admin

    typo = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin:8O96", "api_key": module.API_KEY},
    )

    assert typo.status_code == 200, typo.text
    assert typo.json()["server_supported"] is None, "nothing could be probed, so no verdict"


async def test_the_first_save_shows_the_webhook_token_once_and_no_later_save_rotates_it(
    admin, db
):
    """A later save must not rotate a token already pasted into the Webhook plugin."""
    client, module = admin

    address_only = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "mint_webhook_token": True},
    )
    assert address_only.json()["webhook_token"] is None, address_only.text

    first = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": module.API_KEY, "mint_webhook_token": True},
    )
    token = first.json()["webhook_token"]
    assert isinstance(token, str) and len(token) >= 32, first.text
    assert (await registry.load_jellyfin(db)).webhook_token == token, (
        "the response showed a token the connector row does not hold"
    )

    again = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test/", "api_key": ""},
    )
    assert again.json()["webhook_token"] is None, "shown once means once"
    assert (await registry.load_jellyfin(db)).webhook_token == token, (
        "an unrelated save rotated the token the operator had already pasted into the plugin"
    )


async def test_the_webhook_token_never_comes_back_out_of_the_card(admin):
    """Whoever holds the token can file acquisition work in the household's name."""
    client, module = admin
    before = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert before["has_webhook_token"] is False

    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": module.API_KEY, "mint_webhook_token": True},
    )
    token = saved.json()["webhook_token"]
    assert token

    card = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert card["has_webhook_token"] is True
    assert token not in str(card)


async def test_jellyfin_users_are_listed_for_the_mapping_table(admin):
    client, module = admin
    await _configure(client, module)
    listed = (await client.get("/api/admin/connectors/jellyfin/users")).json()
    assert {u["name"] for u in listed} == {"patrick", "jenny"}


async def test_a_link_without_credentials_is_real_but_incomplete(admin):
    """Until the password entry happens the link attributes playback and cannot write Played state."""
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


async def test_credentials_for_a_different_jellyfin_user_are_refused(admin):
    """A token for another Jellyfin user could only ever write the wrong person's state."""
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
    """Held by the partial unique index, not a lookup: two
    admins linking at once would both pass a lookup."""
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


async def test_signing_in_works_while_jellyfin_is_unreachable(secrets_key, app, monkeypatch):
    """The app must work when Jellyfin is down, so nothing on the auth path may touch it."""
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
    """A token belongs to one Jellyfin identity."""
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


async def test_two_members_linked_at_the_same_time_both_keep_their_token(admin, monkeypatch):
    """The sealed token map is merged in Python, so concurrent links lost a token. The barrier makes both
    sign-ins finish together so both requests are inside the read-modify-write at once."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)

    both_signed_in = asyncio.Barrier(2)
    real_authenticate = JellyfinClient.authenticate_by_name

    async def sign_in_and_wait_for_the_other_phone(self, username, password):
        credentials = await real_authenticate(self, username, password)
        await both_signed_in.wait()
        return credentials

    monkeypatch.setattr(
        JellyfinClient, "authenticate_by_name", sign_in_and_wait_for_the_other_phone
    )

    async def link(name: str, jf_id: str):
        return await client.post(
            f"/api/admin/users/{users[name]['id']}/jellyfin",
            json={
                "jellyfin_user_id": jf_id,
                "jellyfin_username": name,
                "jellyfin_password": module.PASSWORD,
            },
        )

    first, second = await asyncio.gather(link("patrick", PATRICK_JF), link("jenny", JENNY_JF))
    assert [r.status_code for r in (first, second)] == [200, 200], (first.text, second.text)
    assert [r.json()["has_token"] for r in (first, second)] == [True, True]

    after = await _users(client)
    assert {
        name: (after[name]["jellyfin_link_state"], after[name]["has_jellyfin_token"])
        for name in ("patrick", "jenny")
    } == {"patrick": ("linked", True), "jenny": ("linked", True)}, (
        "an account reads 'linked' with no token: one of the two merges was lost (§14.3)"
    )


async def test_linking_an_account_while_it_is_being_unlinked_does_not_deadlock(admin, monkeypatch):
    """Both writers take `app_user` then the connector
    row; the other order deadlocks and one answers 500."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)
    user_id = users["patrick"]["id"]
    credentials = {
        "jellyfin_user_id": PATRICK_JF,
        "jellyfin_username": "patrick",
        "jellyfin_password": module.PASSWORD,
    }
    assert (
        await client.post(f"/api/admin/users/{user_id}/jellyfin", json=credentials)
    ).json()["has_token"] is True

    signed_in = asyncio.Event()
    real_authenticate = JellyfinClient.authenticate_by_name
    real_forget_token = seen.forget_token

    async def sign_in_and_say_so(self, username, password):
        result = await real_authenticate(self, username, password)
        signed_in.set()
        return result

    async def hold_the_unlink_between_its_two_rows(conn, app_user_id):
        # The unlink has updated `app_user` and not reached the
        # connector row; the link's sign-in now takes its turn.
        await asyncio.wait_for(signed_in.wait(), timeout=10)
        await asyncio.sleep(0.05)
        return await real_forget_token(conn, app_user_id)

    monkeypatch.setattr(JellyfinClient, "authenticate_by_name", sign_in_and_say_so)
    monkeypatch.setattr(seen, "forget_token", hold_the_unlink_between_its_two_rows)

    unlinked, linked = await asyncio.gather(
        client.delete(f"/api/admin/users/{user_id}/jellyfin"),
        client.post(f"/api/admin/users/{user_id}/jellyfin", json=credentials),
    )
    assert (unlinked.status_code, linked.status_code) == (200, 200), (unlinked.text, linked.text)

    after = (await _users(client))["patrick"]
    assert (after["jellyfin_user_id"] is None) == (after["jellyfin_link_state"] is None)
    assert after["has_jellyfin_token"] is (after["jellyfin_user_id"] is not None), (
        "the two writers disagreed: a token is stored for an account that is not linked, or a "
        "linked account lost the token it was just given"
    )


async def test_the_probed_version_and_its_verdict_are_stored_by_save_and_by_test(
    admin, db, monkeypatch
):
    """§7.1's >= 10.9 pin must be stored by Save and Test, so a server that fell below it is noticed."""
    client, module = admin

    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": module.API_KEY},
    )
    assert saved.status_code == 200, saved.text
    assert (saved.json()["server_version"], saved.json()["server_supported"]) == (
        module.SERVER_VERSION, True,
    )
    card = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert (card["server_version"], card["server_supported"]) == (module.SERVER_VERSION, True)

    # The fake reads its constant per request: the server changes under an unchanged configuration.
    monkeypatch.setattr(module, "SERVER_VERSION", "10.8.13")
    probe = (await client.post("/api/admin/connectors/jellyfin/test")).json()
    assert (probe["ok"], probe["supported"]) == (True, False)
    card = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert (card["server_version"], card["server_supported"]) == ("10.8.13", False), (
        "the test button computed the verdict and threw it away again"
    )

    # Built as `make_client` builds it, since this file's fixture replaces that function.
    cfg = await registry.load_jellyfin(db)
    refusal = JellyfinClient(
        cfg.url, cfg.api_key,
        server_version=cfg.server_version, server_supported=cfg.server_supported,
    ).played_write_refusal()
    assert refusal is not None and "10.8.13" in refusal and "10.9" in refusal, refusal


async def test_sync_now_says_a_sweep_is_already_running_rather_than_starting_a_second(admin, db):
    """Two sweeps would decide adoptions from two snapshots, so the loser reports as a 200."""
    client, module = admin
    await _configure(client, module)
    users = await _users(client)
    assert (
        await client.post(
            f"/api/admin/users/{users['patrick']['id']}/jellyfin",
            json={"jellyfin_user_id": PATRICK_JF},
        )
    ).status_code == 200

    assert await db.fetchval("SELECT pg_try_advisory_lock($1, 0)", seen._SWEEP_LOCK) is True
    try:
        response = await client.post("/api/admin/connectors/jellyfin/sync")
    finally:
        await db.fetchval("SELECT pg_advisory_unlock($1, 0)", seen._SWEEP_LOCK)

    assert response.status_code == 200
    body = response.json()
    assert body["already_running"] is True
    assert (body["pushed"], body["adopted"], body["unchanged"]) == (0, 0, 0)
    assert (body["users"], body["completed"], body["resolve"]) == ([], [], {})
    assert body["skipped_no_link"] is False, "a held lock is not an unlinked household"
    assert {
        "needs_relink", "owed_unreachable", "owed_no_token", "push_failed", "push_errors",
        "wrote", "unowned", "resolve", "already_running",
    } <= set(body), sorted(body)

    # And the second press, with nothing holding the lock, sweeps for real.
    after = (await client.post("/api/admin/connectors/jellyfin/sync")).json()
    assert after["already_running"] is False
    assert after["users"] == ["patrick"]
