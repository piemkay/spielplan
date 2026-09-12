"""The route inventory: every path the app registers, and the two rules it is held to.

Spec v2.1 §3.2 (the session cookie is the door); `docs/TESTING.md`'s lesson from M3 — "a row
whose `what` names a surface has to be tested through that surface", written because
`backend/spielplan/api/rank.py` shipped with no backend test of any kind and hard-coding one
selection arm inside it passed the entire suite. [M4.10 finding 34]

Three anonymous-refusal sweeps already existed and every one of them is a list kept by hand:
`test_api_gating.py`'s two walks (the admin gate, and decision 179's locked account),
`test_rank_routes.py::test_the_whole_rank_surface_is_behind_a_session` naming six Rank routes, and
`test_tonight_routes.py::test_every_tonight_route_refuses_an_unauthenticated_caller` naming eight
Tonight ones. None of them is wrong and none of them is replaced here: they assert things an
inventory cannot — who the caller is, which role is refused, what the refusal costs — and several
are named in `spec_coverage.toml` rows. What none of them can do is notice a router nobody listed.
This file is the enumeration underneath them, so a route added without `ActiveUser` fails here
rather than passing three partial sweeps.

Two sources, because neither is the route table on its own. `openapi()["paths"]` is every HTTP
route with its methods, and carries no WebSocket and no dependency information at all; `app.routes`
under FastAPI 0.141 is a list of opaque `_IncludedRouter` objects whose children are reached
through `effective_candidates()`, and the WebSocket arrives there as a wrapper whose own `path` is
the empty string with the real route hanging off `original_route`. The schema still builds in
process even though `/openapi.json` is 404 to an anonymous caller (M4.7 sec-12), which
`test_http_seam.py::test_the_schema_still_builds_in_process_for_the_gating_sweeps` exists to hold.

The division of labour with M4.16's `platform-every-route-is-behind-a-session` row, stated so
neither milestone writes the other's test: that row is a *static* walk, asserting that every route
resolves `active_user` or `admin_user` through its dependant. This file asks the question over HTTP
instead, and the two answers are not the same one. A route can be behind the dependency and still
answer (a body validator that runs first, a handler that catches too much), and a route can
authenticate by hand and be entirely correct. So the allow-list below is not the dependency walk's
exemption set: it is measured from what a stranger actually receives. The Tonight WebSocket used to
be this file's example of the second case, on the ground that a socket cannot take an HTTP
dependency — true, and beside the point, since M4.12 gave `deps.py` the two a socket *can* take
(decision 225). It is behind `active_user_ws` now, and the handshake below still measures it the
only way this file measures anything: by connecting as a stranger.

Skipped without TEST_DATABASE_URL for the two live sweeps; see tests/conftest.py. The three static
rules need no database and must not skip, because they are properties of `create_app` alone.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi.routing import APIWebSocketRoute

from spielplan.api.deps import ActiveUser
from spielplan.app import create_app

REPO = Path(__file__).resolve().parents[2]

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# The method column for a route that has none. A WebSocket is in the inventory because §6.2's
# blind-vote property depends on who the frames reach, and because it is exactly the kind of route
# `openapi()` cannot see — the one class of route the old sweeps were structurally blind to.
WEBSOCKET = "WEBSOCKET"

# `GET /{path}` — the SPA fallback, registered only when `SPIELPLAN_STATIC_DIR` names a real build
# (`app.py`), so it is present in the container and absent under pytest. Excluded from the
# inventory, and named here rather than filtered silently: it is the app shell, it is anonymous on
# purpose, and an inventory that contained it would assert a different set depending on whether
# the front end had been built. `test_the_inventory_is_the_apps_own_route_table` holds the
# exclusion to this one path, so a *real* route registered outside `/api/` fails rather than
# disappearing through the same door.
SPA_FALLBACK = "/{path}"

# What a stranger is served on purpose. Enumerated from the code and then measured against the
# running app below in both directions, because an allow-list is the one construct in this file
# that can rot quietly: a route that gains `ActiveUser` leaves its entry behind as a permanent
# exemption for a gate that is already there.
ANONYMOUS = frozenset(
    {
        # §14 / ops-02: the image's HEALTHCHECK, CI's wait loop and `e2e/run.mjs` all poll this
        # before any account exists, and all three key on the status code.
        ("GET", "/api/health"),
        # `library.py`: "what the shell needs before a user is known" — the origin and whether a
        # bundle exists, deliberately free of any user or connector detail.
        ("GET", "/api/config"),
        # §3.1's first-boot redirect reads this before there is anybody to authenticate. A
        # stranger gets two fields and not the install fingerprint; the split is held by
        # `test_api_gating.py::test_a_locked_account_sees_only_the_anonymous_setup_state`.
        ("GET", "/api/setup/state"),
        # §3.1: the first admin is minted here, so there is no session that could gate it.
        ("POST", "/api/setup/admin"),
        # §3.2's two doors. Both run before a session exists, and both read the *incoming* cookie
        # only to destroy the session this device was already holding (dd24).
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/passkey/login/options"),
        ("POST", "/api/auth/passkey/login"),
        # Decision 179's fourth route, and ungated by design: it clears the cookie for whoever
        # holds it and answers `{"ok": true}` either way, so refusing a stranger would be a way of
        # not logging somebody out. `test_api_gating.py`'s UNGATED_BY_DESIGN says the same thing
        # from the dependency side.
        ("POST", "/api/auth/logout"),
    }
)

# Routes no test file names. THIS SET MAY ONLY SHRINK, and the same test holds both directions:
# an entry leaves when a test names its route, and `test_the_untested_set_may_only_shrink` fails
# until it does; nothing may be added, and that test pins the size so an addition cannot be the
# one-line edit the other rule's failure message invites. A new route arrives with the test that
# names it, which is the whole point of the rule.
#
# Every one of these is *gated*: the sweep above drives each of them anonymously and M4.16's
# dependency walk will see them too. What is missing is an assertion about what the route does, so
# each entry names the milestone whose plan opens that surface next.
#
# The plan behind finding 34 counted seventeen of these, measured at the M4.5 head. NINE of the
# seventeen are named by the time this milestone opens, so eight remain — which is the set below,
# and the count is measured here rather than carried over from the plan. Seven were named by the
# milestones that own them (the six passkey routes, `/api/tonight/channel`); the other two,
# `/api/rate/not-seen` and `/api/rate/balance`, were already named in passing at the M4.9 head by
# fixtures that tap them on the way to something else (`test_restore_drill.py:430`,
# `test_secrets_custody.py:169`, `test_account_security.py:221`). This milestone's own two seam
# tests, beside the verdict races in `test_rate_session.py`, therefore make that naming TRUE rather
# than shrinking the set: it is eight on both sides of M4.10, and the ratchet below has not yet been
# exercised by a real shrink. Rebuilt from `git show HEAD:` over all 86 test files and e2e specs
# before this was written: the unnamed set at the M4.9 head is exactly those eight entries.
# [M4.10 cycle 1, m410-rev1-route-inventory-untested-preamble-arithmetic-is-false]
#
# **M4.11 is that first real shrink, and the set below is seven.** Decision 212 gave the finish
# prompt a handoff and wired `onAnswered` to `loadShelves()`, so `e2e/specs/08-jellyfin.spec.js`
# now asks §6.0's banner population for its own contents on both sides of the tap -- over HTTP,
# against the app, with the "before" reading taken from the route precisely because "the element is
# missing" is a different statement from "the population does not name this title". That is an
# assertion about what the route does, which is the one thing the entry said was missing, so the
# entry leaves and the pin follows it down. Worth recording that the ratchet cost nothing to obey
# here: the shrink was found by the rule rather than by a reader. [M4.11; decision 212]
#
# **M4.12 takes two more, and the set below is five.** Its finding 3 is about what the undo and
# escape routes DO — each produced its next card by replaying the whole round a second time — so the
# test that closes it is an HTTP test of both handlers, which is exactly the assertion the two
# entries said was missing. Found by the rule again, on the run that added that test. [M4.12]
UNTESTED = frozenset(
    {
        # M4.14 (bundle import and artifact custody). Its ~127 s POST is the finding that made the
        # missing §5.3 registry row visible; M4.14 owns the import state the route needs.
        "/api/admin/bundle/import",
        # M4.9 shipped the Data card's sources-and-terms list; M4.16's decision 172 settles where
        # that surface lives, and the assertion about its payload belongs with that ruling.
        "/api/admin/data/sources",
        # M4.6 shipped the per-credential revoke (its plan's route table). `e2e/specs/17-users.
        # spec.js`'s "the row editor lists a passkey and revokes that one credential" drives it
        # through the Users screen, so it is exercised and named nowhere: the client builds the URL
        # from a template, and a literal is what this rule can see. M4.11 is the next plan to open
        # the admin Users routes (its Jellyfin-link column reads `/api/admin/users`).
        "/api/admin/users/{user_id}/passkeys/{credential_id}",
        # The filter vocabulary Rank and the Library read. M4.15 owns the client half
        # (`dd-fe-facet-vocabulary-read-has-no-sequence-guard`), which is the bug this route's
        # payload is the input to.
        "/api/facets",
        # A Home projection whose domain function `test_home.py` drives directly — which is
        # precisely the shape `docs/TESTING.md`'s M3 lesson is about. `/api/home/shelves` has no
        # client caller at all today (`home.svelte.js` reads `/api/home`), so nothing reaches it
        # over HTTP to assert against; M4.15 owns the client half this payload is the input to.
        #
        # `/api/home/pending-verdicts` stood here too until M4.11, and for a reason worth keeping:
        # `test_devstub_contract.py` drives it against the *harness*, which is why that file is out
        # of the haystack above — a grep cannot tell the stub's route from the app's. The entry left
        # when a Playwright spec asked the real one (see the preamble), not when the harness did.
        "/api/home/shelves",
        # `/api/tonight/seats/{participant_id}/undo` and `.../escape` stood here for M4.12, whose
        # findings hold both handlers (the undo/escape pair at `play.py:410/436/456`, and escape
        # availability on a finished round). They leave with finding 3's repair: both routes built
        # their payload by calling `play.state_for` again after the write, and
        # `test_tonight_integration.py::test_one_tap_reads_the_frozen_pool_once_and_runs_the_round_once`
        # drives each of them over HTTP and asserts what it does -- one frozen-pool decode and one
        # round for the undo, and neither for the escape, which has no next pair to compute. That is
        # an assertion about the route rather than about the rule underneath it, which is the one
        # thing the entries said was missing. [M4.12 finding 3]
    }
)


def _leaf_routes(routes):
    """Every route object the app will actually match, unwrapped.

    FastAPI 0.141 stops flattening `include_router`: an included router is one opaque route whose
    children come out of `effective_candidates()` with the prefixes already applied, and each child
    is a context wrapper rather than the route itself. For HTTP routes the wrapper carries the
    right `path`; for the WebSocket it carries the empty string, and only `original_route` knows
    where the socket lives. Unwrapping every leaf rather than special-casing the socket, so the
    next route class the framework wraps is found the same way.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _leaf_routes(candidates())
            continue
        original = getattr(route, "original_route", None)
        yield original if original is not None else route
        yield from _leaf_routes(getattr(route, "routes", ()))


def http_routes(application) -> set[tuple[str, str]]:
    """Every `(METHOD, path)` under `/api/` the app serves, from the app's own schema."""
    return {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in operations
        if method.upper() in METHODS
    }


def websocket_routes(application) -> set[tuple[str, str]]:
    return {
        (WEBSOCKET, route.path)
        for route in _leaf_routes(application.routes)
        if isinstance(route, APIWebSocketRoute)
    }


def inventory(application) -> set[tuple[str, str]]:
    return http_routes(application) | websocket_routes(application)


def outside_api(application) -> set[str]:
    return {path for path in application.openapi()["paths"] if not path.startswith("/api/")}


def concrete(path: str) -> str:
    """Fill path parameters with a value that exists nowhere — the gate must fire first.

    Borrowed from `test_api_gating.py`, and for its reason: probed as the literal `{session_id}` a
    request still routes, so it reports a pass it did not earn on the day a route starts reading
    its parameter before the dependency runs.
    """
    return re.sub(r"\{[^}]+\}", "999999", path)


# The haystack for the naming rule, and the two files left out of it. This module names every route
# in ANONYMOUS and every route in UNTESTED, so counting itself would make the rule self-satisfying
# — an inventory that proves every route is named because the inventory names them. The
# self-test at the bottom depends on this exclusion, which is why it is not left to a comment.
SELF = Path(__file__).name

# The second way a test file names a route without asking the route anything: every request
# `test_devstub_contract.py` makes goes to `ops/devstub.py`'s own ASGI app (`apps`/`harness`
# fixtures), and it touches the real app only to compare path *sets*. So a path literal there is an
# assertion about the harness, and CLAUDE.md settles which of the two this rule is about —
# "ops/devstub.py is a harness, not the app: backend/spielplan/api/ wins on any disagreement",
# which M4.10's plan repeats as a gotcha of its own. Measured rather than assumed: without this
# exclusion, M4.10's harness test for decision 203's banner link discharged
# `/api/home/pending-verdicts` from a set that may only shrink, while `api/home.py`'s handler stayed
# exactly as unasserted as finding 34 found it — and a ratchet closes a debt for good.
# [M4.10 finding 34; CLAUDE.md's harness rule]
HARNESS_ONLY = "test_devstub_contract.py"


def _haystacks() -> dict[str, tuple[str, str]]:
    """Every test file that could name a route, with the quote characters its language uses.

    Python tests and Playwright specs, which is where a request is actually made — `e2e/helpers.js`
    included, because the seeding path decision 186 collapsed into it reaches routes no spec spells
    itself. Vitest files are deliberately absent: they run against `fetch` doubles and name client
    paths (`/rate/undo`), not server ones. Two Python files are skipped by name, one for each way a
    file can name a route while asserting nothing about it: see `SELF` and `HARNESS_ONLY`.
    """
    hay: dict[str, tuple[str, str]] = {}
    for path in sorted((REPO / "backend" / "tests").rglob("test_*.py")):
        if path.name in (SELF, HARNESS_ONLY):
            continue
        hay[path.name] = (path.read_text(encoding="utf-8"), "\"'")
    for path in sorted((REPO / "e2e" / "specs").glob("*.spec.js")):
        hay[path.name] = (path.read_text(encoding="utf-8"), "\"'`")
    helpers = REPO / "e2e" / "helpers.js"
    hay[helpers.name] = (helpers.read_text(encoding="utf-8"), "\"'`")
    return hay


def _pattern(path: str, quotes: str) -> re.Pattern[str]:
    """A route path as it appears in a test: at the start of a string literal, whole.

    Three decisions worth stating, because each one is a class of wrong answer:

    The match must begin at an opening quote. A route mentioned in prose — `GET
    /api/admin/data/sources` inside a comment — is a reference, not a test of it, and two of the
    seven entries in UNTESTED are only unnamed because of this clause. Backticks count in
    JavaScript, where a template literal is how a parametrised path is written, and do not count in
    Python, where they appear only inside comments and docstrings.

    A parameter segment matches any run of non-separator characters, so `f"/api/titles/{tid}"` and
    `` `/api/tonight/seats/${seat}/answer` `` both name their route. The alternative — requiring the
    parameter's declared name — would measure spelling rather than coverage.

    The match must end at the path: `/api/rank` is not named by `"/api/rank/queue"`, and
    `/api/titles/{title_id}` is not named by `f"/api/titles/{tid}/state"`. Hence `}` in the
    lookahead as well as `/`, or the parameter run would stop one character short and the trailing
    brace would look like the end of the path.
    """
    segments = [
        r"[^/\"'`\s)]+" if segment.startswith("{") else re.escape(segment)
        for segment in path.split("/")
        if segment
    ]
    return re.compile("[" + quotes + "]/" + "/".join(segments) + r"(?![A-Za-z0-9/_{}-])")


def unnamed_paths(application) -> set[str]:
    hay = _haystacks()
    return {
        path
        for _method, path in inventory(application)
        if not any(_pattern(path, quotes).search(text) for text, quotes in hay.values())
    }


# --- the inventory itself ---------------------------------------------------------------------


def test_the_inventory_is_the_apps_own_route_table():
    """Everything below is a subtraction from this set, so an empty or partial one passes by
    being unable to fail — which is the failure mode that gave every earlier sweep its blind spot.

    The two allow-lists are checked against the inventory in the direction that rots: an entry
    naming a route the app no longer serves is an exemption nobody will ever re-examine.
    """
    application = create_app()
    found = inventory(application)

    assert len(found) > 80, f"the inventory found {len(found)} routes, so it is sweeping a stub"
    assert (WEBSOCKET, "/api/tonight/channel") in found, (
        "the WebSocket is the one route openapi() cannot see, and it is the one the blind-vote "
        "property depends on"
    )
    for probe in (("POST", "/api/rate/verdict"), ("GET", "/api/rank/queue")):
        assert probe in found, f"{probe} is served and the inventory missed it"

    stale = ANONYMOUS - found
    assert not stale, f"ANONYMOUS names routes the app does not serve: {sorted(stale)}"
    paths = {path for _method, path in found}
    assert paths >= UNTESTED, (
        f"UNTESTED names routes the app does not serve: {sorted(UNTESTED - paths)}"
    )

    assert outside_api(application) <= {SPA_FALLBACK}, (
        "a route is served outside the /api namespace and the inventory is skipping it: "
        f"{sorted(outside_api(application) - {SPA_FALLBACK})}"
    )


# --- rule 1: a stranger is refused everywhere but the allow-list ------------------------------


async def _handshake(client, path: str) -> list[str]:
    """Drive one WebSocket handshake over ASGI and return the frame types the app sent.

    httpx has no WebSocket transport, so the app is called directly — the same private-transport
    route `test_api_gating.py` takes, and written out again rather than imported because no test
    module in this suite imports another. `websocket.disconnect` follows the connect so a socket
    that *is* accepted ends its read loop instead of leaving the hub holding it.
    """
    cookies = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 51000),
        "server": ("test", 80), "subprotocols": [],
        "headers": [(b"host", b"test"), (b"cookie", cookies.encode())],
    }
    incoming = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]
    sent: list[str] = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message["type"])

    await client._transport.app(scope, receive, send)
    return sent


async def _answered_without_a_session(client, routes) -> list[str]:
    """Rule 1's predicate, as a function so the self-test at the bottom can run the rule itself
    rather than a restatement of it."""
    served = []
    for method, path in sorted(routes):
        answer = await client.request(method, concrete(path), json={})
        if answer.status_code != 401:
            served.append(f"{method} {path} -> {answer.status_code}")
    return served


async def test_every_route_outside_the_anonymous_allow_list_refuses_a_signed_out_caller(app):
    """§3.2 over HTTP, for every route at once instead of three lists' worth.

    One client against all of them rather than a parametrised case each, for `test_api_gating.py`'s
    reason: a refusal happens inside the dependency before a handler or a body validator runs, so
    nothing is written and nothing needs a clean database between probes. Every offender is
    collected instead of failing on the first, because the useful failure is the shape of the set —
    one route is a mistake, a whole router is a missing dependency.

    `create_app()` builds a second application for the enumeration while the fixture's one answers
    the requests. The routers are module-level, so the two register the same routes; this is the
    convention `paths_behind` already follows.
    """
    anonymous = app()
    served = await _answered_without_a_session(anonymous, http_routes(create_app()) - ANONYMOUS)

    assert not served, (
        # ASCII, like every message here: this one prints a list of route paths on a Windows
        # console, and a section sign is what makes cp1252 kill the run instead of the assertion.
        "these routes answered a caller holding no session, and the cookie is the door "
        f"- add the session dependency, or add the route to ANONYMOUS with its reason: {served}"
    )

    frames = await _handshake(anonymous, "/api/tonight/channel")
    assert frames == ["websocket.close"], (
        "the Tonight channel carries who is in the room and each seat's progress; an anonymous "
        f"handshake was answered with {frames}"
    )


async def test_every_route_on_the_anonymous_allow_list_is_still_served_to_a_stranger(app):
    """The other direction, and the one that decays silently: a gate added to one of these eight
    leaves its entry behind as a permanent exemption, and the sweep above shrinks by one route
    with nothing saying so.

    "Not 401" rather than 200, because five of the eight are probed with an empty body and the 422
    that answers is itself the proof: the body validator only runs once no dependency has refused.
    """
    for method, path in sorted(ANONYMOUS):
        # A fresh client per probe: /api/auth/logout clears the cookie jar it is called on, and
        # /api/setup/admin is the one entry whose answer depends on what an earlier probe did.
        answer = await app().request(method, path, json={})
        assert answer.status_code != 401, (
            f"{method} {path} is on the anonymous allow-list and refuses a stranger - if the gate "
            "is deliberate, take the entry out so the sweep covers the route"
        )


# --- rule 2: every route is named by some test ------------------------------------------------


def test_every_route_is_named_by_some_test_file():
    """`docs/TESTING.md`'s M3 lesson, mechanised: a route no test names is a surface whose
    behaviour nobody has written down, however well its domain function is covered.

    Naming is a proxy and a weak one — it cannot tell a request from a string in an unrelated
    assertion, and it misses a route an e2e spec drives through the UI (the admin passkey revoke is
    exactly that case, and its UNTESTED entry says so). It is the proxy that fails for the thing
    that actually went wrong, which a stronger instrument nobody writes does not.
    """
    unnamed = unnamed_paths(create_app())
    new = unnamed - UNTESTED
    assert not new, (
        "these routes are named by no test file at any layer - write the test, or, if the "
        f"milestone that owns the surface is not this one, add it to UNTESTED: {sorted(new)}"
    )


def test_the_untested_set_may_only_shrink():
    """Separate from the rule above because it fails for the opposite reason, and it is the half
    that makes the set a ratchet rather than a licence.

    A route that gains a test has to leave UNTESTED in the same diff. Left behind, the entry keeps
    excusing a route that no longer needs excusing, and the next reader counts more gaps than there
    are — which is how the plan inherited this set at seventeen when nine of them had already been
    named by the four intervening milestones and by two fixtures passing through.

    BOTH DIRECTIONS, because only one of them was held and the other is the one the set is named
    for. The subtraction below closes entries and nothing counted them: a route added behind
    `ActiveUser` with no test answers 401 to a stranger, so rule 1 passes it by design, and rule 2
    fails with a message that hands the author the escape — "add it to UNTESTED". One line in the
    frozenset and the suite is green again with the debt up by one, under a comment that forbids
    exactly that edit. The message is the right advice for a route another milestone owns, which is
    what the set is for; the pin is what keeps it from being a licence.

    Pinned rather than capped. `<=` funds a free slot the moment a legitimate shrink lands and
    nothing forces the number down with it, which is the one-directional baseline M4.16's
    `platform-api-layer-sql-baseline` row already names as a defect class — "a baseline that only
    ratchets one way turns into a permanent excuse the moment a module is emptied". At `==` the set
    cannot move in either direction without this number moving in the same diff, under a message
    that says which direction is allowed. [M4.10 cycle 2, M410-C2-COV-01]
    """
    unnamed = unnamed_paths(create_app())
    closed = UNTESTED - unnamed
    assert not closed, (
        "these routes are named by a test now, so they are no longer untested - delete them from "
        f"UNTESTED: {sorted(closed)}"
    )
    assert len(UNTESTED) == 5, (
        f"UNTESTED holds {len(UNTESTED)} routes and the ratchet is pinned at 5 - an entry leaves "
        "when a test names its route, and lowering this number is how that is recorded; raising it "
        "is the edit this rule exists to make argue for itself"
    )


def test_the_haystack_leaves_out_the_files_that_ask_their_routes_nothing():
    """The two exclusions above, and the reason they need a rule of their own: they NARROW this
    instrument, and a narrowing is invisible from the outside.

    M4.8's lesson was a widened guard that silently stopped seeing the thing it was written for; an
    exclusion is the same failure with the sign flipped. A file skipped by a name that has since
    been renamed is skipped for ever, and it takes with it every route it was the only namer of.
    So both names must be real files and must really be absent from the haystack — and
    `HARNESS_ONLY` must still be load-bearing, i.e. name at least one route this app serves. The
    day it names none, the exclusion is hiding nothing and should go rather than sit there as a
    narrowing nobody can see. Today the one route it names is `/api/home/pending-verdicts`, and
    the exclusion is still what makes the difference there: M4.11 took that entry out of UNTESTED
    on a Playwright spec asking the app, which is exactly the distinction this narrowing draws, so
    a harness-only namer must still not be able to close an entry on its own.
    """
    tests = REPO / "backend" / "tests"
    hay = _haystacks()
    for name in (SELF, HARNESS_ONLY):
        assert (tests / name).is_file(), (
            f"{name} is excluded from the haystack and is not a file any more, so the exclusion "
            "silently covers nothing - rename it here or delete it"
        )
        assert name not in hay, f"{name} is in the haystack this rule greps"

    harness = (tests / HARNESS_ONLY).read_text(encoding="utf-8")
    named = sorted(
        path for _method, path in inventory(create_app()) if _pattern(path, "\"'").search(harness)
    )
    assert named, (
        f"{HARNESS_ONLY} names no route this app serves, so excluding it hides nothing - delete "
        "the exclusion instead of leaving the haystack narrower than it has to be"
    )


# --- the guard, caught catching something ----------------------------------------------------


def _with_a_route_nobody_gated():
    """The app, plus one route of exactly the shape finding 34 is about: a new `@router.get` under
    a prefix three partial sweeps already cover, with no `ActiveUser` and no test.
    """
    application = create_app()

    @application.get("/api/rank/leak")
    async def leak() -> dict[str, bool]:
        return {"ok": True}

    return application


async def test_a_route_added_without_a_session_fails_both_rules():
    """Both rules, against a route that really is registered — not a fabricated inventory.

    This is the test the plan's step 9 asks for as a manual step ("validate the guard by adding a
    throwaway `@router.get('/api/rank/leak')`"), kept rather than thrown away: a guard whose
    failure nobody has seen is a guard nobody knows the shape of. Done on a doctored app, so no
    source file is touched and nothing has to be reverted.

    What the same leak does to the three sweeps this file sits under, measured rather than assumed,
    because it is the other half of finding 34's claim: nothing. `admin_paths()` still finds 23, so
    `test_api_gating.py`'s stated count does not move — it counts admin-gated routes, and a route
    behind nothing is not one. Its forced-change walk still finds decision 179's three declared
    routes, because a route behind nothing is in neither side of that subtraction. Its live locked
    sweep never probes the leak for the same reason, and its hand-rolled-reader scan reads source
    for session calls the leak does not make. The Rank and Tonight sweeps are literal lists and
    cannot name a route nobody added to them. A route added without `ActiveUser` passes the entire
    suite, which is why this file exists and is the assertion it has to be able to make.

    No database and no lifespan: the leak route takes no dependency, which is what makes it a leak,
    so driving it needs nothing the fixture provides.
    """
    leaky = _with_a_route_nobody_gated()
    leak = ("GET", "/api/rank/leak")

    assert leak in inventory(leaky), "the inventory did not see a route added to a live app"
    assert leak not in ANONYMOUS, "the allow-list would have excused it"
    assert "/api/rank/leak" in unnamed_paths(leaky) - UNTESTED, (
        "the naming rule did not notice a route no test names - this is the assertion the "
        "exclusion of this file from the haystack is load-bearing for"
    )

    transport = httpx.ASGITransport(app=leaky)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        served = await _answered_without_a_session(client, {leak})
    assert served == ["GET /api/rank/leak -> 200"], (
        f"rule 1's own predicate did not report a route that answers a stranger: {served}"
    )


async def test_a_gated_route_with_no_test_is_invisible_to_rule_1_and_caught_only_by_rule_2(app):
    """The blind spot the size pin is for, and the one seventeen routes actually arrived through.

    Rule 1 measures what a stranger receives, deliberately (the module docstring says why), so a
    route behind `ActiveUser` answers 401 and rule 1 reports nothing — correctly. The whole of
    what knows the route is untested is therefore rule 2, and rule 2's failure message names the
    escape in as many words: add the path to UNTESTED. Measured before the pin was written, with a
    gated route registered and its path appended to the set: all seven tests in this file passed,
    and the set was nine.

    So the chain is asserted end to end rather than argued — rule 1 blind, rule 2 catching, and the
    excuse costing a second line that contradicts whoever takes it. `/api/rank/leak` above is the
    other half of the same question and cannot answer this one: a route behind nothing fails both
    rules, which is the case that was never the problem. [M4.10 cycle 2, M410-C2-COV-01]
    """
    anonymous = app()
    application = anonymous._transport.app
    leaked = "/api/rank/leak_gated"

    @application.get(leaked)
    async def leak(user: ActiveUser) -> dict[str, bool]:
        return {"ok": True}

    # FastAPI memoises the schema on the first build and `inventory` reads it, so a route added to
    # a live app is invisible here for a reason that is not the app's.
    application.openapi_schema = None
    assert ("GET", leaked) in inventory(application), "the route was not registered"

    served = await _answered_without_a_session(anonymous, {("GET", leaked)})
    assert served == [], (
        f"rule 1 reported a gated route, so this is no longer the case it cannot see: {served}"
    )
    assert leaked in unnamed_paths(application) - UNTESTED, (
        "rule 2 is the only rule that can see an untested route behind a session, and it did not"
    )
    # The pinned size, spelled again because this assertion is about the pin rather than about the
    # set: an excused leak makes the set a size the pin rejects. The two literals therefore move in
    # the same diff, and M4.11 is where that was learned -- taking one entry out left this one at 8
    # and the red read as a defect in the shrink rather than as this test's own arithmetic.
    assert len(UNTESTED | {leaked}) != 7, (
        "excusing it is one line in a frozenset, and the size pin in "
        "`test_the_untested_set_may_only_shrink` is the second line that edit has to defeat"
    )
