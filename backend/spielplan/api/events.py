"""The `/events` namespace, and §7.2's Jellyfin webhook in it. Spec v2.1 §7.2, §7.3, §11;
decisions 332, 365, 367.

Decision 332 settled two things about `/events` at M5.1 and gave the second one to M5.2: that the
namespace is declined by the SPA fallback the way `/api` is (`app.py`, and it is one rule over the
head segment rather than a clause each), and that §7.2's `POST /events/jellyfin` is token-authed
with the token held as a `connector_config` secret under `jellyfin`, generated at first save and
shown once on §6.6's Jellyfin card. The first was the spine's - a namespace the shell answers is a
namespace no later milestone can mount a route into without discovering it - and the second is
this module's, which owns the webhook, its token and its 401. The debounce it feeds is
`acquire/intake.py`'s.

So this module made the namespace REAL to everything that walks the app before it had a route in
it at all. The router mount guard
(`test_static_contracts.py::test_every_router_under_api_is_mounted_on_the_application`) reads every
module under `api/` that exports a `router` and asserts the application serves its paths, so
M5.2's first route is mounted by adding a route here rather than by also remembering to add an
`include_router` to `app.py` - which is the edit a milestone building a webhook is most likely to
forget, and the one the fallback would then answer with the app shell.

NO SQL, EVER. `api/` decides only HTTP shapes and the rules live in the domain packages
(CLAUDE.md Conventions), so the webhook's debounce, its token check and its enqueue all belong
under `spielplan/acquire/` and `spielplan/connectors/`. `test_layering_guards.py`'s residue
ratchet records this module's count by leaving it out of `ALLOWED_RESIDUE` entirely: a module
absent from that dict holds zero by assertion, and the grown half of the ratchet fails on the
first statement that appears here rather than tolerating it under a missing key. That is why the
handler below is four decisions and one call: `registry.load_jellyfin` answers who may deliver,
`cfg.webhook_token_matches` answers whether this caller is that, and `intake.record_event` -- or
`intake.record_refusal`, for a body that never arrived whole -- is the whole of what a delivery
does to the database.

NO `ANONYMOUS` ENTRY, WHICH IS THE OPPOSITE OF WHAT M5.1 EXPECTED HERE. That paragraph read that
the route "will need one, because a token in a header is not a session cookie"; decision 367
settles it the other way and the reason is in the second direction the list is measured in.
`test_route_inventory.py`'s `ANONYMOUS` is what a stranger is served ON PURPOSE, and
`test_every_route_on_the_anonymous_allow_list_is_still_served_to_a_stranger` asserts that every
entry answers something other than 401. This route answers a stranger 401 by construction, so an
entry would be false in exactly the direction that second test exists to hold, and admitting it
would need a carve-out inside the test written to prevent carve-outs. Left out, rule 1
(`test_every_route_outside_the_anonymous_allow_list_refuses_a_signed_out_caller`) passes it
honestly, because it genuinely does refuse a signed-out caller - and that is only true while the
token check below stays the FIRST thing the handler does. [decision 367]

THE TEMPLATE IS PUBLISHED, NOT DESCRIBED. `ops/jellyfin-webhook-template.json` carries the exact
text an operator pastes into the Webhook plugin's Generic destination, the way `.env.example`
carries config, and decision 365 calls that file the contract: a route whose accepted shape is
only written down in its own tests is a shape an operator cannot produce.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from starlette.requests import ClientDisconnect

from spielplan.acquire import intake
from spielplan.api import deps
from spielplan.connectors import registry

log = logging.getLogger("spielplan.api.events")

router = APIRouter(prefix="/events", tags=["events"])

# §7.2 says "token-authed" and fixes nothing else, so this route fixes it. The Webhook plugin's
# Generic destination can carry a secret exactly one way - "Add Request Header" - which rules out
# a query parameter before taste does, and a header keeps the token out of every access log and
# out of `PUBLIC_URL`-shaped screenshots. The `X-Spielplan-` prefix is the one `api/deps.py`
# already spells for `X-Spielplan-Reauth`, so the app has one namespace of its own headers rather
# than two. A constant and not a setting: an operator who can change the header name has a second
# way to get this wrong and no surface that would tell them they had. [decision 365]
WEBHOOK_TOKEN_HEADER = "X-Spielplan-Token"

# The largest body this route reads. The published template renders under two kilobytes, and an
# operator who ticks the plugin's "Send All Properties" sends a few more; nothing a Jellyfin sends
# comes near this. It is a bound and not a guess at a size: without one a token holder's 45 MB of
# JSON zeros parsed, dumped, and died in the `::text::jsonb` cast -- 500, a traceback, nothing
# recorded -- and a 50 MB string was stored whole in a table nothing prunes. Room above the
# template for one more thing on purpose: a body nested past the decoder's own depth limit is
# about 120 KB, and it has to reach `json.loads` to be recorded as the unreadable payload it is
# (`test_a_body_this_schema_cannot_hold_is_recorded_rather_than_answered_with_a_500`).
# [review cycle 3: M52-C3-EVENTS-02, M52-C3-SWEEP-07]
MAX_BODY_BYTES = 256 * 1024

# How long a body may take to arrive once the token has been accepted. uvicorn has no body-read
# timeout of its own, so without this a sender that sends headers and then nothing is waited for
# as long as its socket stays open. Generous for a plugin posting two kilobytes on a household
# network, and what it bounds is this handler alone: no connection is held while it runs
# (`_connection`). [review cycle 3: M52-C3-EVENTS-03]
BODY_DEADLINE_S = 30.0


@asynccontextmanager
async def _connection() -> AsyncIterator[asyncpg.Connection]:
    """One pooled connection for one block, bounded and released exactly as `deps.db` does it --
    because it IS `deps.db`, driven by hand rather than by FastAPI.

    By hand because a dependency is held for the whole request, and this is the one route in the
    app that reads its body itself: FastAPI reads a DECLARED body before it resolves dependencies,
    so every other POST has its bytes before it takes a connection, while this handler took one
    and then awaited the body with it checked out. Ten sockets from anyone holding the token --
    every Jellyfin admin can read it in the plugin's config -- sending headers and then nothing
    took the pool to idle 0, and every phone in the household was answered 503 for as long as they
    stayed open. `deps.current_user_ws` is the same repair for a socket that waits without bound.
    [review cycle 3: M52-C3-EVENTS-03; M4.12 finding 16]
    """
    source = deps.db()
    conn = await anext(source)
    try:
        yield conn
    finally:
        await source.aclose()


async def _read_body(request: Request) -> bytes | str:
    """The delivered bytes, or the reason there are none (`intake.PAYLOAD_TOO_LARGE`,
    `intake.DELIVERY_INTERRUPTED`) -- never an exception.

    `ClientDisconnect` is what Starlette raises when the sender leaves mid-body, and nothing in
    `app.py` caught it: `ServerErrorMiddleware` answered 500 and re-raised, and uvicorn wrote
    "Exception in ASGI application" with a traceback, for a Jellyfin whose link merely blinked.
    It is also what uvicorn hands a handler whose sender closed the socket before the first read,
    body buffered or not. A body past `MAX_BODY_BYTES` stops being read at the cap rather than
    being buffered to find out how big it was. [review cycle 3: M52-C3-EVENTS-02]
    """
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(BODY_DEADLINE_S):
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return intake.PAYLOAD_TOO_LARGE
                chunks.append(chunk)
    except (ClientDisconnect, TimeoutError):
        return intake.DELIVERY_INTERRUPTED
    return b"".join(chunks)


@router.post("/jellyfin", status_code=status.HTTP_202_ACCEPTED)
async def jellyfin_item_added(request: Request) -> dict[str, Any]:
    """§7.2's `POST /events/jellyfin` (ItemAdded): accepted and recorded, never acquired here.

    202 AND NOT 200, because the two answers say different things to a sender that will not ask
    again. The Webhook plugin fires and forgets: it has no retry, no queue and nowhere to put a
    failure except its own log, so everything this route can honestly promise is that the event
    has been written down. What becomes of it - is the library picked, does the item resolve, does
    the household already own the title - is `acquire/intake.py`'s sweep, minutes later, and a 200
    would be claiming that work happened on this thread (plan A4, decision 363).

    THE ORDER BELOW IS THE WHOLE OF THIS ROUTE'S SECURITY, and it is the opposite of the order
    that reads naturally. The token is checked BEFORE the body is even read, so that a caller with
    no token cannot write a row: exit-criterion check 9 is "a wrong token is 401 with nothing
    recorded as pending", and a handler that recorded first and refused second would hand anyone
    who found the URL an unbounded writer into `jellyfin_intake`. It is also what makes decision
    367's reading of the inventory true - rule 1 sweeps every non-allow-listed route with an empty
    body and no cookies and requires 401, and this route earns that answer rather than being
    excused from it.

    503 BEFORE 401, AND NAMING THE REASON (M4.7 dd03's shape, decision 332). Under a changed
    `SECRETS_KEY` the stored token cannot be opened, so the app does not know whether this caller
    presented the right one; answering 401 there would tell a correctly-configured operator that
    their token is wrong and send them to regenerate a secret that is fine. `load_jellyfin`
    degrades rather than raising, which is why this is a field and not an `except`, and 503 is
    the honest code: the credential this route is checked against is temporarily unreadable.
    Never 500 - that was the unhandled `InvalidTag` M4.7 removed from six other routes.

    THEN TOLERANCE, AND IT IS ASYMMETRIC ON PURPOSE (decision 365). The plugin renders an
    OPERATOR-AUTHORED template, so every field this app requires is a field an operator can
    forget, and the plugin fills it with values it quote-escapes but never writes as JSON - a title
    carrying a backslash or a control character renders a body that will not parse at all, as does a
    double quote under a two-brace `{{Name}}` (decision 417). Both of those are answered 202 and
    recorded with the reason, because a webhook the app rejects is one the operator never learns
    about: the plugin shows them a 4xx in a log they are not reading, while §6.6's card can show
    them the body that arrived and what was wrong with it. `read_event` decides which refusal it
    was; this handler decides only that none of them is an error of the sender's transport.

    The reason travels back in the body as well as into the row, for the operator testing their
    template with `curl` before they trust it to a library scan.

    AND THE BODY IS READ WITH NO CONNECTION HELD, UNDER A CAP AND A DEADLINE. The token check
    borrows a connection and gives it back before a byte of the body is awaited, and the row is
    written on a second one afterwards, so how fast a sender sends decides nothing about the pool
    the household's phones share. A body too large, cut off or never finished is recorded with
    that reason (`intake.record_refusal`) and answered 202 like every other body this route cannot
    act on. [review cycle 3: M52-C3-EVENTS-02, M52-C3-EVENTS-03]
    """
    async with _connection() as conn:
        cfg = await registry.load_jellyfin(conn)
    if cfg.secrets_unreadable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{registry.SECRETS_UNREADABLE_REASON}: this delivery cannot be authenticated until "
            "the Jellyfin connector's secrets open again",
        )
    if not cfg.webhook_token_matches(request.headers.get(WEBHOOK_TOKEN_HEADER)):
        # One answer and one log line for both spellings of the mistake - no header at all, and a
        # header carrying the wrong token - because the operator's repair is the same for each:
        # re-paste the token §6.6 showed them once. Distinguishing them in the response would also
        # tell an unauthenticated caller which half of the credential they had guessed. The token
        # itself is never logged; what an operator needs from this line is that a delivery reached
        # the app at all, which is the half they cannot see from the plugin's side.
        log.warning(
            "jellyfin webhook: a delivery presented no usable %s and was refused",
            WEBHOOK_TOKEN_HEADER,
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "the Jellyfin webhook token is missing or wrong",
        )

    raw = await _read_body(request)
    if isinstance(raw, str):
        async with _connection() as conn:
            event = await intake.record_refusal(conn, raw)
        return {"recorded": event.id, "state": event.state, "reason": event.reason}
    try:
        payload: Any = json.loads(raw)
    except (ValueError, RecursionError):
        # Not a Mapping, so `read_event` records it as an unreadable payload. The bytes are not
        # kept: `record_event` stores the parsed body and there is none, and a column holding
        # whatever arrived on an unauthenticated-shaped path is a place for something unpleasant
        # to be stored. The reason names the fault and §6.6 can say when it last happened.
        #
        # `RecursionError` because the decoder's own depth limit is one more way a body is
        # unreadable, and it is a `RuntimeError` rather than a `ValueError`: an object nested
        # twenty thousand deep walked past this line and answered 500, which is the one answer
        # decision 365 forbids. [review cycle 1: m52-rev-events-02]
        payload = None
    async with _connection() as conn:
        event = await intake.record_event(conn, payload)
    return {"recorded": event.id, "state": event.state, "reason": event.reason}
