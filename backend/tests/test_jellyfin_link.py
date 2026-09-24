"""Account linking through the admin routes. Spec v2.1 §3.3, §6.6, §7.3, §14.3.

"Admin view maps each app user <-> one Jellyfin user (`GET /Users`), **optional, one-to-one**.
… Authentication is **never** delegated to Jellyfin (the app must work when Jellyfin is down)."

The whole app is pointed at `ops/fake_jellyfin.py` by replacing one function
(`registry.make_client`), so these exercise the real routes end to end — including the parts
that only exist over HTTP, like the API key never coming back out of a GET.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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


# --- §6.6: configuring the connector ------------------------------------------------------


async def test_the_connector_starts_unconfigured_and_says_so(admin):
    client, _module = admin
    body = (await client.get("/api/admin/connectors/jellyfin")).json()
    # M4.7 dd03: "nothing configured" and "configured but the credentials will not decrypt" are
    # two different things the card has to say differently, so the payload carries both.
    #
    # And M4.11 adds §7.1's probed pair, whose unconfigured reading is the one that matters: an
    # empty version with `server_supported: null` is "nobody has probed", which `played_write_refusal`
    # must not treat as a refusal. A stored `false` is the other thing entirely. [M4.11 finding 16]
    #
    # And M5.2 adds §7.2's token as a third boolean, for `has_api_key`'s reason (decision 332,
    # §14.3): an install that has minted none holds `False` here, and the value itself appears in
    # exactly one response in this app, which is not this one.
    #
    # And M5.7 adds §6.6's webhook status as facts (decision 455), whose unconfigured reading is
    # nulls and a zero: nothing has arrived and no poll has run, which is an answer and not a 500.
    assert body == {"url": "", "has_api_key": False, "configured": False,
                    "library_ids": [], "linked_users": 0, "secrets_unreadable": False,
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
    for path in (
        "/api/admin/connectors/jellyfin/test",
        "/api/admin/connectors/jellyfin/users",
        # The third read of the same kind, added with §6.6's library pick: "there is no server to
        # ask" is 409 here as it is above, and not the `ok: false` the route answers for a server
        # that refused -- an unconfigured connector is not a connector that failed (decision 364).
        "/api/admin/connectors/jellyfin/libraries",
    ):
        response = await (
            client.post(path) if path.endswith("test") else client.get(path)
        )
        assert response.status_code == 409
        assert "not configured" in response.json()["detail"]


# --- §6.6 + decision 364: the library pick gets a list, a writer and a reader ------------------


async def test_the_libraries_the_pick_picks_from_are_listed_from_the_server(admin):
    """§6.6's card has named a "library pick" since M1 and nothing could enumerate what to pick.

    Not decoration: decision 364 makes `library_ids` the boundary §7.2's intake paths are scoped
    by, and the double's third library is the household's own camcorder footage. An admin who
    cannot see that library listed cannot say that acquisition should leave it alone.

    Read off the fake's own table rather than re-typed, because the shapes a Jellyfin server sends
    belong to the double (`ops/fake_jellyfin.py`) and a test that authors them has turned the
    refuser into a mock -- including the envelope, which is `{"Items": [...]}` here where `/Users`
    is a bare list.
    """
    client, module = admin
    await _configure(client, module)

    body = (await client.get("/api/admin/connectors/jellyfin/libraries")).json()
    assert body["ok"] is True
    listed = [(lib["id"], lib["name"]) for lib in body["libraries"]]
    assert listed == [(lib["Id"], lib["Name"]) for lib in module.LIBRARIES]
    assert ("jf-lib-home", "Home Videos") in listed, (
        "the library nobody would acquire from is what makes a pick mean anything"
    )
    # The app's own spelling, as the user-mapping read next door also answers in: nothing of
    # Jellyfin's `BaseItemDto` reaches the browser through this route.
    assert all(set(lib) == {"id", "name"} for lib in body["libraries"]), body


async def test_a_server_that_will_not_list_its_libraries_is_reported_rather_than_raised(
    admin, monkeypatch
):
    """The read an admin makes while repairing a connector must not fail along with it.

    The failure this refuses is not a 500 but a plausible empty list. Decision 364 reads an EMPTY
    pick as "the whole server", so a card handed `[]` because Jellyfin refused the key would be
    showing the admin the shape of a deliberate choice nobody made -- and the admin would save it.
    The test button one screen up reports the same way for the same reason.

    The second half is the one that reached the card as the wrong answer. A refusal only became
    `ok: false` by way of `JellyfinError`, and `_request` does not raise for a 200 it cannot read:
    a forward-auth portal's sign-in page collapsed to an empty envelope and was reported as a
    server with no libraries, while a body that parsed as a bare list left the route as an
    `AttributeError` -- a 500, which is worse than the 502 this route exists to avoid.
    [review cycle 1: m52-rev-lib-04]
    """
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
    """`library_ids` has been stored, merged and served since M1 with no writer and no reader.

    Decision 364 gives it both in one milestone, and this is the writer. Without it the row
    `spec_coverage.toml` registers -- "the same item after the library is picked enqueues once" --
    is reachable only by calling `save_jellyfin` from Python, which is not a gesture §6.6 offers
    anybody, and §7.2 would ship a boundary no household could ever draw.
    """
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
    """Decision 364's two answers, which one "empty means keep it" field cannot both give.

    Absent is the partial save the masked-key form already makes on every URL correction. `[]` is
    an admin deselecting the last library, which decision 364 reads as "the whole server" -- the
    state of every install in existence, since nothing has ever written this field. Collapsed into
    one answer the second gesture becomes unperformable: a household that picked one library once
    would have §7.2 acquire from that library alone for ever, with the UI offering no way back.
    """
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
    """Decision 364 made the pick the acquisition boundary, and this is the first route that ever
    wrote it. The bounds were argued in the model's own comment -- "the only caller that would not
    stop at the number of folders a server actually has is a crafted body ... the place to answer
    for one is the edge, as a 422" -- and then only two thirds of that sentence was written.

    A NUL is the third. `library_ids` is stored as jsonb on the connector row, `db/pool.py`
    registers `json.dumps` as that codec, and `\u0000` is a character Postgres holds nowhere in a
    jsonb string: the write raised, `app.py`'s `asyncpg.PostgresError` handler answered
    500 `{"detail": "database error"}`, and the admin's save was lost to a value the schema could
    have named. `_no_control_characters` had already answered the identical byte for account names
    (sec-04, as-11); this field is the one it did not cover. [review cycle 2: m52-c2-libid-01,
    m52-c2-lib-03]

    The two length bounds are asserted beside it because they had no test either: they are the
    same clause of the same comment, and a `max_length` nothing exercises is a number rather than
    a bound.
    """
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
    """Three values the edge stored verbatim and a real server reads as something else entirely.

    A pick of `[" "]` reaches `ParentId` as whitespace, which ASP.NET binds to a null `Guid?` --
    so the read covers the WHOLE SERVER, and the boundary the admin drew is silently widened to
    every library they deselected, which is decision 364's harm exactly. The all-zero GUID names
    no folder and is answered 400 on every scoped read. And one library spelled three ways
    (dashed, braced, upper case -- all of which Jellyfin accepts) was three scopes and three reads
    of the same folder per key, and three ids a comparison against `/Library/MediaFolders`'s own
    undashed spelling could never match. A GUID is stored as the server spells it, once; a blank
    or nil id is a 422. [review cycle 3: M52-C3-LIB-04]
    """
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


# --- §7.2 + decision 332: the webhook token, shown once ---------------------------------------


async def test_a_save_the_connectors_page_sends_mints_no_token_nobody_would_see(admin, db):
    """Decision 418. The only client of this route that ships is §6.6's connectors page, and its
    `save()` sends `{url, api_key}` and discards the answer -- so when every PUT minted, the first
    Save any admin pressed sealed a token, showed it to nobody, and left no route that could ever
    show or replace it: `has_webhook_token` read true, the reveal read null for ever, and every
    real `ItemAdded` was 401 for the life of the install. The exact body the page sends mints
    nothing now, and the card goes on saying that no token exists.
    [M5.2 review cycle 4: M52-C4-TOKEN-01]
    """
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
    """The save commits before the version probe, and the probe met `httpx.InvalidURL` -- which is
    not an `httpx.HTTPError` -- for a port with a letter in it, so the PUT answered 500 over a save
    that had already happened. The probe is best-effort (§3.1 makes a half-configured install
    legal), so a URL no request can be sent to is stored with no verdict and answered 200.
    [M5.2 review cycle 4: M52-C4-TOKEN-01]
    """
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
    """Decision 332: the token is "generated at first save and shown once" on §6.6's card.

    Three facts in one test because they are one gesture. The save that leaves the connector
    configured is the save that mints -- an address with no key is not a connector yet (§3.1 makes
    that half-configured state legal). The value appears in that response and in no other. And no
    later save changes it: an operator who has pasted the token into the Webhook plugin's header
    field must not have it rotated under them by an unrelated edit to the URL, because §7.2's
    intake would then answer the household's own server 401 with nothing on any surface saying so.

    "First save" is the first save that ASKS (decision 418): a client that can render the reveal
    sends `mint_webhook_token`, and the test above holds the body that does not.
    """
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
    """§14.3's rule about the API key, applied to the credential §7.2 adds beside it.

    Whoever holds this token can file acquisition work in the household's name, so the card says
    that one exists and never what it is: the GET is read by every admin session on every visit,
    long after the single appearance decision 332 allows the value itself.
    """
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


# --- M4.11: the link is one read-modify-write, and §7.1's pin is stored rather than shown ------


async def test_two_members_linked_at_the_same_time_both_keep_their_token(admin, monkeypatch):
    """§14.3's sealed token map, merged by two requests at once.

    Reproduced before the fix, against this same in-process fake: both responses answered
    `has_token: true` and `state: linked`, and `GET /api/admin/users` then showed jenny linked
    with `has_jellyfin_token: false` while the card counted `linked_users: 1`. The map is
    AEAD-sealed, so the merge happens in Python and cannot be expressed in SQL — and on an
    autocommit connection both requests read the same map, each added one entry, and the second
    write overwrote the first. `registry.save_jellyfin` re-read and merged again, so the window
    existed twice over.

    The account left behind is not cosmetically wrong. "Linked with no token" is exactly the
    state §7.3's sweep treats as a missing credential, which before this milestone stopped that
    member's whole reconciliation at their first owed row for ever — so a lost token here is a
    person whose seen states silently stop flowing, with nothing on any surface saying so.

    **The barrier is what makes this a test rather than a coin toss.** `asyncio.gather` alone does
    not reliably put both requests inside the window: each one makes a sign-in and four database
    round trips first, and measured against the unfixed route the two simply took turns and both
    tokens survived. So the sign-ins are made to finish together — which is also the real shape of
    this, two people on two phones doing §7.3's one-time password entry side by side on the sofa —
    and from there every step is an await that yields, so both requests are genuinely in the
    read-modify-write at once. The barrier releases *before* either request opens a transaction,
    so it cannot itself wedge the row lock the fix takes.
    """
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
    # The card's own count, which reads the token map rather than the badge — the two have to
    # agree, or §6.6 shows a household with more links than credentials.
    assert (await client.get("/api/admin/connectors/jellyfin")).json()["linked_users"] == 2


async def test_linking_an_account_while_it_is_being_unlinked_does_not_deadlock(admin, monkeypatch):
    """The two writers of this pair of rows, taking their locks in the same order.

    Link and Unlink are adjacent controls in §6.6's mapping table and they write the same two
    things: the account's badge in `app_user`, and that member's entry in the connector's sealed
    token map. `seen.unlink` has taken them in that order since M1; this route now takes a lock on
    the second, so it has to agree — in the opposite order the two transactions hold what the
    other is waiting for, and Postgres breaks the tie by aborting one of them with a deadlock
    error, which reaches the admin as a 500 on a button that was supposed to be idempotent.

    Measured with the locks in the other order (the connector row first, as the `write_txn(lock=)`
    idiom would suggest): `asyncpg.exceptions.DeadlockDetectedError`, and one of the two requests
    answering 500. Hence the handshake rather than a bare `gather`: the unlink is held between its
    two rows until the link's sign-in has returned, which is the one interleaving that produces it.

    Either order of commits is a correct answer — an admin who presses both buttons gets whichever
    landed second — so the assertion is that both requests answered and that the account ends up
    self-consistent. A token stored against an account that is no longer linked is the state
    `forget_token` exists to prevent (§7.3).
    """
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
        # The unlink has updated `app_user` and has not yet reached the connector row. Waiting for
        # the link's sign-in puts it at the head of its own transaction at this exact moment.
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
    """§7.1 pins Jellyfin >= 10.9 for the *write*, and the pin was a sentence in a button.

    `MIN_SERVER_VERSION` and `supported` have existed since M1; the test button returned the
    verdict to the browser and nothing stored it, so nothing on the write path could consult it.
    A 10.8 install therefore passed every visible check while `POST /UserPlayedItems` — the route
    that version pins — 404ed for the life of the household, which together with the sweep's
    missing failed-push counter read as a healthy quiet sync.

    Both routes, because an admin uses both and the write must be gated whichever they pressed:
    Save is where the credentials are known to be fresh, Test is where they go to prove it. The
    second half of this test is the case the stored verdict exists for — a server that *fell*
    below the pin, which is a downgrade or a restored VM, and which nothing would notice if the
    pair were only ever written once.
    """
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

    # The same server, answering a version below the pin. The fake reads its constant per request,
    # so this is the server changing under a configuration that did not.
    monkeypatch.setattr(module, "SERVER_VERSION", "10.8.13")
    probe = (await client.post("/api/admin/connectors/jellyfin/test")).json()
    assert (probe["ok"], probe["supported"]) == (True, False)
    card = (await client.get("/api/admin/connectors/jellyfin")).json()
    assert (card["server_version"], card["server_supported"]) == ("10.8.13", False), (
        "the test button computed the verdict and threw it away again"
    )

    # And the stored pair is what the one write reads. Built the way `registry.make_client` builds
    # it rather than through that function, because this file's fixture replaces it to reach the
    # fake — so the assertion would otherwise be about the fixture instead of the connector row.
    cfg = await registry.load_jellyfin(db)
    refusal = JellyfinClient(
        cfg.url, cfg.api_key,
        server_version=cfg.server_version, server_supported=cfg.server_supported,
    ).played_write_refusal()
    assert refusal is not None and "10.8.13" in refusal and "10.9" in refusal, refusal


async def test_sync_now_says_a_sweep_is_already_running_rather_than_starting_a_second(admin, db):
    """§6.6's "sync now" against §5.3's 15-minute job, which is a real collision on a live box:
    the button is pressed because something looks wrong, and the thing that looks wrong is
    usually a sweep in flight.

    Two sweeps of one household decide adoptions from two different library snapshots, so the
    loser reports instead of racing — as a 200 the card can print, not an exception. The lock is
    held here from a second connection, which is what the worker process is.

    The body is also the admin card's whole data, so the counters this milestone added are
    asserted by name: `push_failed` and `push_errors` are the app->Jellyfin direction's health,
    `owed_no_token` the repairable debt, `unowned` §7.2's falsified ownership, and `resolve`
    carries the unmatched items. A card that cannot see them is how a dead write direction read
    as "pushed 0 - adopted 0 - unchanged N" for a whole install.
    """
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
