"""Role gating and identity, at the route (§3.1, §3.2, §6.6). The sweeps every route answers are in
`test_route_inventory.py`. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import os

import asyncpg
import httpx

from spielplan.api import deps
from spielplan.app import create_app
from spielplan.core import auth
from spielplan.core.config import settings
from tests.helpers import (
    ADMIN_PASSWORD,
    MEMBER_PASSWORD,
    concrete,
    household,
    resolves,
    route_table,
    websocket,
)


def paths_behind(*targets) -> set[tuple[str, str]]:
    """The HTTP routes whose dependency graph reaches any of `targets`."""
    return {
        key
        for key, route in route_table(create_app()).items()
        if key[0] != "WS" and any(resolves(route.dependant, target) for target in targets)
    }


def test_a_route_without_conn_still_holds_a_pooled_connection_for_its_session():
    """`current_user` takes `conn: DB` and FastAPI caches
    it per request, so dropping `conn` saves nothing."""
    behind_db = paths_behind(deps.db)
    assert ("GET", "/api/model-log") in behind_db, (
        "the route that takes no `conn` is not behind `deps.db` either - if that is now true, "
        "`api/home.py:model_log`'s docstring says the opposite and is the thing to fix"
    )
    # Every authenticated route is behind the session load, so a connection-free route is impossible.
    behind_user = paths_behind(deps.active_user)
    assert behind_user <= behind_db, sorted(behind_user - behind_db)


def test_the_poster_route_is_gated_and_holds_no_pooled_connection_for_its_request():
    """Sixty posters against a pool of ten: this route must
    not be behind `deps.db`, yet stays behind §3.1's lock."""
    brief = paths_behind(deps.active_user_brief)
    assert ("GET", "/api/art/{title_id}/poster") in brief, sorted(brief)
    held = brief & paths_behind(deps.db)
    assert not held, f"these hold a pooled connection for the whole request: {sorted(held)}"


async def test_the_spa_fallback_does_not_answer_for_the_api_namespace(tmp_path):
    """The SPA catch-all is mounted only with `SPIELPLAN_STATIC_DIR`; it must decline `/api` and `/events`
    by head segment, while real routes still answer 405 to a wrong verb and client routes get the shell."""
    build = tmp_path / "static"
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text("<html>shell</html>", encoding="utf-8")

    previous = os.environ.get("SPIELPLAN_STATIC_DIR")
    os.environ["SPIELPLAN_STATIC_DIR"] = str(build)
    settings.cache_clear()
    try:
        built = create_app()
        # No lifespan: every assertion is decided by the router before a handler runs.
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=built), base_url="http://test"
        )
        async with client:
            gone = await client.post("/api/setup/members", json={"name": "x", "role": "member"})
            assert gone.status_code == 404, (
                "a deleted API route must read as deleted in the app that ships, not as a "
                f"method mismatch: {gone.status_code}"
            )
            unknown = await client.get("/api/nope")
            assert unknown.status_code == 404
            assert "html" not in unknown.text.lower(), "an API path was answered with the shell"

            wrong_verb = await client.post("/api/auth/me")
            assert wrong_verb.status_code == 405, (
                "the decline must not swallow a real route: GET /api/auth/me exists, so a POST "
                "to it is a method mismatch and 405 is the honest answer"
            )

            # With no static build the fallback is not mounted, so only this build can see the 405.
            unrouted = await client.get("/events/playback")
            assert unrouted.status_code == 404, (
                "an unrouted /events path must be 404 in the app that ships, not the shell: "
                f"{unrouted.status_code}"
            )
            assert "html" not in unrouted.text.lower(), (
                "the /events namespace was answered with the app shell - a webhook sender that "
                "gets HTML where it expected JSON fails in a much less obvious place"
            )
            posted = await client.post("/events/playback", json={})
            assert posted.status_code == 404, (
                "a POST to an unrouted /events path must read as 404 and never as 405: the "
                "fallback is registered methods=['GET'], so a namespace it does not decline is "
                f"a partial match and Starlette answers 405: {posted.status_code}"
            )

            # A GET to §7.2's POST route is a method mismatch,
            # which only a namespace with a route in it can show.
            events_wrong_verb = await client.get("/events/jellyfin")
            assert events_wrong_verb.status_code == 405, (
                "the decline must not swallow a real route: POST /events/jellyfin exists, so a "
                "GET to it is a method mismatch and 405 is the honest answer"
            )

            # A leading double slash made the head segment "", and
            # httpx reads `//x` as an authority, hence absolute URLs.
            for probe in ("http://test//events/jellyfin", "http://test//api/nope"):
                doubled = await client.get(httpx.URL(probe))
                assert doubled.status_code == 404, (
                    f"{probe} reached the app shell: the namespace decline reads the head "
                    f"segment and a leading slash makes that the empty string"
                )
                assert "html" not in doubled.text.lower()
                slashed_post = await client.post(httpx.URL(probe), json={})
                assert slashed_post.status_code == 404, (
                    f"a POST to {probe} answered {slashed_post.status_code}; a namespace whose "
                    "contract says it can never 405 answered 405"
                )

            # A head segment, not a string prefix: `/eventsish` shares every character of the namespace.
            sibling = await client.get("/eventsish")
            assert sibling.status_code == 200 and "shell" in sibling.text, (
                "the decline swallowed a client-side route whose name merely starts with a "
                "declined namespace - the split is on `/`"
            )

            client_route = await client.get("/rank")
            assert client_route.status_code == 200
            assert "shell" in client_route.text, "the fallback must still serve the SPA"
    finally:
        if previous is None:
            os.environ.pop("SPIELPLAN_STATIC_DIR", None)
        else:
            os.environ["SPIELPLAN_STATIC_DIR"] = previous
        settings.cache_clear()


async def test_a_member_receives_no_admin_entry_in_its_navigation(app):
    """"Hidden, not merely disabled": a client-side check still ships the link in the response."""
    _admin, member = await household(app)
    payload = (await member.get("/api/auth/me")).json()
    keys = {entry["key"] for entry in payload["nav"]["account"]}
    # "My Taste" left with decision 488: it led to the same unbuilt placeholder as the tab.
    assert keys == {"account"}
    assert "admin" not in str(payload["nav"])


async def test_an_admin_receives_the_admin_entries(app):
    admin, _member = await household(app)
    payload = (await admin.get("/api/auth/me")).json()
    keys = {entry["key"] for entry in payload["nav"]["account"]}
    assert {"admin", "setup"} <= keys


async def test_both_roles_see_every_shipped_surface(app):
    """Decision 488: an unshipped surface is in neither role's navigation."""
    admin, member = await household(app)
    for client in (admin, member):
        payload = (await client.get("/api/auth/me")).json()
        assert [s["key"] for s in payload["nav"]["surfaces"]] == [
            "home", "rate", "tonight", "rank"
        ]
        hrefs = [e["href"] for e in payload["nav"]["surfaces"] + payload["nav"]["account"]]
        assert "/map" not in hrefs and "/taste" not in hrefs, hrefs


async def test_a_surface_enters_navigation_in_its_place_when_it_ships(app, monkeypatch):
    """Flipping the flag is all a milestone does, and the surface arrives in §6's order."""
    from spielplan.api import auth as auth_api

    shipped = tuple({**s, "built": True} if s["key"] == "map" else s for s in auth_api.SURFACES)
    monkeypatch.setattr(auth_api, "SURFACES", shipped)
    _admin, member = await household(app)
    payload = (await member.get("/api/auth/me")).json()
    assert [s["key"] for s in payload["nav"]["surfaces"]] == [
        "home", "rate", "tonight", "rank", "map"
    ]
    assert auth_api.shipped("map") and not auth_api.shipped("taste")


async def test_a_stale_admin_session_is_re_prompted(db, app):
    """§3.2: "admin routes re-prompt after 24 h"."""
    admin, _member = await household(app)
    assert (await admin.get("/api/admin/users")).status_code == 200

    await db.execute(
        "UPDATE auth_session SET admin_verified_at = now() - interval '25 hours'"
    )
    stale = await admin.get("/api/admin/users")
    assert stale.status_code == 401
    assert stale.headers.get("X-Spielplan-Reauth") == "admin"

    # Signing in again with the password is what clears it: that is the whole re-prompt.
    await admin.post("/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD})
    assert (await admin.get("/api/admin/users")).status_code == 200


async def test_the_me_payload_reports_the_re_prompt(app, db):
    """The shell has to know before it renders the admin link, not after a 401."""
    admin, _member = await household(app)
    await db.execute("UPDATE auth_session SET admin_verified_at = now() - interval '25 hours'")
    assert (await admin.get("/api/auth/me")).json()["admin_reauth_required"] is True


async def test_a_wrong_pin_leaves_the_session_identity_unchanged(db, app):
    """§3.2: the account chip "switches between member profiles, gated by the per-user PIN"."""
    admin, member = await household(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "0000"})
    assert refused.status_code == 401
    assert (await admin.get("/api/auth/me")).json()["name"] == "patrick"


async def test_a_correct_pin_switches_the_session(app):
    admin, member = await household(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    switched = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert switched.status_code == 200
    me = (await admin.get("/api/auth/me")).json()
    assert me["name"] == "jenny"
    assert me["auth_method"] == "pin"
    # The device was handed over: the admin session it held does not travel with it.
    assert "admin" not in {entry["key"] for entry in me["nav"]["account"]}


async def test_switching_to_an_account_with_no_pin_is_refused(app):
    """The chip only offers profiles that set one; otherwise it is a door with no lock."""
    admin, member = await household(app)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401
    assert "no switch PIN" in refused.json()["detail"]


async def test_the_switch_list_only_names_profiles_with_a_pin(app):
    admin, member = await household(app)
    assert (await admin.get("/api/auth/switchable")).json() == []
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    assert [u["name"] for u in (await admin.get("/api/auth/switchable")).json()] == ["jenny"]


async def test_the_switch_route_refuses_an_anonymous_caller(app):
    """A 4-digit PIN accepted from anyone would be 10,000 guesses against an ungated route."""
    _admin, member = await household(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    anonymous = app()
    refused = await anonymous.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401


async def test_a_locked_out_account_refuses_even_the_right_pin(db, app):
    admin, member = await household(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    for _ in range(auth.PIN_ATTEMPT_LIMIT):
        await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "0000"})
    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401
    assert "too many attempts" in refused.json()["detail"]
    assert (await admin.get("/api/auth/me")).json()["name"] == "patrick"


# Decision 179's four; ordered, because /logout ends the session and must be probed last.
REACHABLE_WHILE_LOCKED = (
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/switch"),
    ("POST", "/api/auth/logout"),
)

async def _locked_member(app):
    admin = app()
    await admin.post("/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD})
    otp = (
        await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]

    member = app()
    signed_in = await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert signed_in.status_code == 200
    assert signed_in.json()["must_change_password"] is True
    return member


async def test_a_locked_account_cannot_reach_the_app(app):
    """The auth layer enforces the lock, not the UI."""
    member = await _locked_member(app)
    assert (await member.get("/api/titles")).status_code == 403
    assert (await member.get("/api/prompts/finish")).status_code == 403
    # ...but /me and the password route stay reachable, or there would be no way out.
    assert (await member.get("/api/auth/me")).status_code == 200


async def test_every_other_authenticated_route_refuses_a_locked_account(app):
    """One session against every gated route: all are
    refused inside the dependency, so nothing is written."""
    member = await _locked_member(app)
    for method, path in sorted(paths_behind(deps.active_user, deps.active_user_brief)):
        response = await member.request(method, concrete(path), json={})
        assert response.status_code == 403, (
            f"{method} {path} let an account locked to a first-login change through "
            f"with {response.status_code}"
        )


async def test_the_four_routes_that_are_the_way_out_stay_reachable(app):
    """"Not 403": the empty-body 422 proves `active_user` let it through before validation."""
    member = await _locked_member(app)
    for method, path in REACHABLE_WHILE_LOCKED:
        response = await member.request(method, path, json={})
        assert response.status_code != 403, f"{method} {path} is the way out and it is shut"


async def test_a_locked_account_sees_only_the_anonymous_setup_state(app):
    """It answers 200 either way, so the payload is compared field for field."""
    member = await _locked_member(app)
    locked = await member.get("/api/setup/state")
    assert locked.status_code == 200
    assert set(locked.json()) == {"required", "note"}, (
        "an account locked to §3.1's first-login change was handed the privileged wizard state: "
        f"{sorted(locked.json())}"
    )
    assert locked.json() == (await app().get("/api/setup/state")).json()


async def test_the_tonight_channel_refuses_a_locked_account(app):
    """The close code 1008 is asserted, and an unlocked member must still be served."""
    admin, member = await household(app)
    otp = (
        await admin.post("/api/admin/users", json={"name": "kim", "role": "member"})
    ).json()["one_time_password"]
    locked = app()
    signed_in = await locked.post("/api/auth/login", json={"name": "kim", "password": otp})
    assert signed_in.json()["must_change_password"] is True

    refused = await websocket(locked, "/api/tonight/channel")
    assert [frame["type"] for frame in refused] == ["websocket.close"], (
        "an account locked to §3.1's first-login change was served the Tonight channel: "
        f"{[frame['type'] for frame in refused]}"
    )
    assert refused[0].get("code") == 1008, (
        f"the locked socket was closed with {refused[0].get('code')} rather than 1008"
    )

    served = await websocket(member, "/api/tonight/channel")
    assert [frame["type"] for frame in served][:2] == ["websocket.accept", "websocket.send"]


async def test_a_wrong_name_and_a_wrong_password_cost_the_same(app, monkeypatch):
    """Counted, not clocked: both paths must do the same argon2 work and the
    same round trips. The patch is on the sync primitive `to_thread`
    resolves; the one-row write residual is asserted as the number it is."""
    admin, _member = await household(app)

    calls: list[str] = []
    real = auth.verify_password

    def counted(stored_hash, password):
        calls.append(password)
        return real(stored_hash, password)

    monkeypatch.setattr(auth, "verify_password", counted)

    # Patched on `asyncpg.Connection`: the pool proxy resolves
    # methods at call time. Tags come from `execute`.
    queries: list[tuple[str, str | None]] = []
    for name in ("execute", "fetch", "fetchrow", "fetchval"):
        real_query = getattr(asyncpg.Connection, name)

        async def counted_query(self, query, *args, _real=real_query, _name=name, **kwargs):
            result = await _real(self, query, *args, **kwargs)
            queries.append((query, result if _name == "execute" else None))
            return result

        monkeypatch.setattr(asyncpg.Connection, name, counted_query)

    def rows_written() -> int:
        return sum(
            int(str(tag).rsplit(" ", 1)[-1])
            for _statement, tag in queries
            if tag is not None and str(tag).startswith("UPDATE ")
        )

    async def refusal_cost(name: str) -> tuple[int, tuple[str, ...], int]:
        calls.clear()
        queries.clear()
        response = await admin.post(
            "/api/auth/login", json={"name": name, "password": "definitely-wrong"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == auth.PASSWORD_REFUSAL
        return len(calls), tuple(statement for statement, _tag in queries), rows_written()

    existing = await refusal_cost("patrick")
    absent = await refusal_cost("nobody-lives-here")
    # `existing` above was one of them, so this reaches the limit and no further.
    for _ in range(auth.PASSWORD_ATTEMPT_LIMIT - 1):
        await refusal_cost("patrick")
    locked = await refusal_cost("patrick")

    assert existing[:2] == absent[:2] == locked[:2], (
        f"a wrong password against an existing name made {existing[0]} argon2 verifications "
        f"and issued {list(existing[1])}, a missing name {absent[0]} and {list(absent[1])}, a "
        f"locked-out one {locked[0]} and {list(locked[1])} - the difference is an "
        "account-enumeration oracle a stopwatch can read"
    )
    assert existing[0] > 0, "the counter was installed somewhere the login path does not reach"
    assert existing[1], "the query counter was installed somewhere the login path misses"

    # The residual, stated as a measurement rather than claimed away.
    assert (existing[2], absent[2], locked[2]) == (1, 0, 0), (
        f"the failure increment wrote {existing[2]} rows for an existing name, {absent[2]} for "
        f"an absent one and {locked[2]} for a locked-out one. (1, 0, 0) is the known gap this "
        "test, `core.auth.check_password`'s docstring and the coverage row "
        "`platform-login-throttle-and-input-bounds` each name as open; anything else means the "
        "work moved, and all three have to be rewritten to whatever it moved to"
    )
