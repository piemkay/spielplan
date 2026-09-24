"""§7.2's `POST /events/jellyfin`, over HTTP against the real app. Decisions 332, 365, 367, 369.

"**every Jellyfin add triggers acquisition** - `POST /events/jellyfin` (ItemAdded), token-authed"
(`docs/spielplan-spec_v2.1.md:342`). The clause names an authentication and a body and settles
neither, so decision 332 gives the token its home - a `connector_config` secret under `jellyfin`,
401 on a wrong one, 503 naming `SECRETS_UNREADABLE_REASON` when it cannot be opened - and decision
365 settles what the handler does with a body it cannot act on: 202, recorded, never 400 and never
500.

OVER HTTP AND NOT THROUGH THE FUNCTION, because everything this file is about happens outside
`intake.record_event` and is invisible to a caller that has it. `test_jellyfin_intake.py` asserts
what a delivered body becomes; the assertions here are the ones only a request can make - that the
token is checked BEFORE a row is written, that a body FastAPI never validated still reaches the
handler, that an unreadable `SECRETS_KEY` is a 503 with a reason rather than the unhandled
`InvalidTag` M4.7 took off six other routes, and that a wrong verb is a method mismatch rather
than the namespace being swallowed.

THE PAYLOADS ARE THE DOUBLE'S. `ops/fake_jellyfin.py`'s emitter renders the Webhook plugin's
template and POSTs it, here over an ASGI transport into the app under test rather than across a
compose network, so what this route receives is what a plugin sends: flattened `Provider_*` keys,
an episode carrying its own beside its series identity (M52-C4-TTA-04), and the refusals the double
can be asked for on purpose - a wrong token, a missing field, another notification type, a burst of
twelve. A test that authored those bodies itself would have turned the double into a mock and
would prove only that the handler tolerates a shape nobody sends. The single exception is the
unparseable body, and it says why in its own docstring.

AND THE TEMPLATE IS ASSERTED, NOT ONLY SHIPPED. `ops/jellyfin-webhook-template.json` is the text
an operator pastes into the plugin and decision 365 calls it the contract, which is worth nothing
if it drifts from the route: the three rules at the bottom render it the way Handlebars would,
feed the result to the real route, and hold its field list against what the double actually sends.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import intake
from spielplan.api import events
from spielplan.connectors import registry, resolve
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.core.config import settings
from spielplan.db import pool

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "ops" / "jellyfin-webhook-template.json"

ADMIN_PASSWORD = "an-admin-password"
JELLYFIN_URL = "http://jellyfin.test"
# The operator regenerated SECRETS_KEY, or restored a dump without its .env - one fact to the app
# either way, and over `core/config`'s length floor so that the refusal under test is the custody
# one and not the configuration one. Same value and same reason as `test_secrets_custody.py:46`.
OTHER_KEY = "a-different-secrets-key-not-a-real-one"

# The Bear, which `ops/fake_jellyfin.py` gives twelve episodes precisely so §7.2's burst can be
# emitted in full. Severance keeps its two because `test_jellyfin_client.py` pins them.
SERIES = "jf-7"
MOVIE = "jf-1"


@pytest.fixture
async def webhook(secrets_key, db, app, fake_jellyfin, monkeypatch):
    """A configured install whose Jellyfin can fire the plugin's webhook back at it.

    The emitter's transport is pointed at the application under test, so the whole path is real:
    the double builds the payload, serialises it, adds the operator's header and POSTs it, and the
    route that answers is the one `create_app` mounted. `client._transport.app` is the house idiom
    for reaching the ASGI application the `app` fixture wrapped (`test_bundle_import_job.py:940`,
    `test_http_seam.py:171`); the fixture hands out clients rather than the application because no
    test before this one needed to hand it to something else.

    THE TOKEN IS READ OUT OF THE DATABASE AND NEVER OUT OF A RESPONSE. Decision 332 shows it once
    on §6.6's card and `test_jellyfin_link.py::test_the_api_key_never_comes_back_out` holds the
    same rule for the admin key, so a test that could fetch it over HTTP would be asserting a leak.
    The PUT that configures the connector mints it only when it asks to (`mint_webhook_token`,
    decision 418), because a plain Save must never discard the one reveal an admin was shown.
    """
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
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    saved = await client.put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": module.API_KEY, "mint_webhook_token": True},
    )
    assert saved.status_code == 200, saved.text

    cfg = await registry.load_jellyfin(db)
    assert cfg.webhook_token, "a save that asks for the webhook token is what mints it (decision 418)"

    module.WEBHOOK_TRANSPORT = httpx.ASGITransport(app=client._transport.app)
    module.WEBHOOK_URL = "http://test/events/jellyfin"
    module.WEBHOOK_TOKEN = cfg.webhook_token
    return {"client": client, "module": module, "token": cfg.webhook_token}


async def _emit(module, **body) -> dict:
    """Fire the plugin at the app and hand back what the double reports: the statuses the route
    answered, and the payloads it rendered to get them."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://fake-jellyfin"
    ) as control:
        response = await control.post("/_test/item-added", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _rows(db) -> list[dict]:
    return [
        dict(row) for row in await db.fetch(
            "SELECT id, item_id, item_type, resolved_key, state, reason, raw "
            "FROM jellyfin_intake ORDER BY id"
        )
    ]


def _delivery(token: str) -> dict:
    """An ASGI scope for one delivery carrying the right token, for the two tests that have to
    control the body's arrival -- which no HTTP client in this suite can do: httpx hands the app
    the whole body at once, so a sender that stalls or leaves mid-body is only reachable one
    layer down, where uvicorn's own `receive` lives."""
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/events/jellyfin", "raw_path": b"/events/jellyfin",
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
        "headers": [
            (events.WEBHOOK_TOKEN_HEADER.lower().encode("ascii"), token.encode("ascii")),
            (b"content-type", b"text/plain; charset=utf-8"),  # `_PLUGIN_CONTENT_TYPE` says why
        ],
    }


# What Handlebars.Net 2.1.6's default encoder, `HtmlEncoderLegacy` and not its `HtmlEncoder`,
# rewrites inside a `{{Field}}` by name; beyond these four it writes every UTF-16 code unit above
# 159 as `&#N;` (`_encode`). An apostrophe, a backtick, `=`, a backslash and every control
# character pass through it untouched. [M5.2 review cycle 4: M52-C4-WH-02]
_HTML_ENCODED = {'"': "&quot;", "&": "&amp;", "<": "&lt;", ">": "&gt;"}
# The fields the plugin's `DataObjectHelpers` fills for EVERY item, so an omitted one renders as
# an empty string; `SeriesId`, `SeriesName` and `Year` are set only where the item has one. And
# the three of them it passes through `Escape()` first, which turns each `"` into `\"`.
_ALWAYS_SET = ("NotificationType", "Name", "ItemId", "ItemType")
_PRE_ESCAPED = ("Name", "SeriesName", "ItemType")
# And what the rendered body travels as: `GenericClient.SendAsync` sends it as
# `new StringContent(body, Encoding.UTF8, contentType)`, `contentType` being
# `MediaTypeNames.Text.Plain` unless the operator adds a Content-Type header (plugin v17 and v18) --
# never the JSON type httpx's `json=` labels a body with. [M5.2 review cycle 4: M52-C4-WH-02]
_PLUGIN_CONTENT_TYPE = {"Content-Type": "text/plain; charset=utf-8"}


def _encode(value: str) -> str:
    """`HtmlEncoderLegacy.EncodeImpl` over a C# string, which it walks one `char` at a time -- one
    UTF-16 code unit, so a character outside the BMP is written as its two surrogates."""
    units = value.encode("utf-16-le", "surrogatepass")
    return "".join(
        _HTML_ENCODED.get(chr(unit), f"&#{unit};" if unit > 159 else chr(unit))
        for unit in (int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2))
    )


def _render(values: dict, *, pre_escaped: bool = True, template: str | None = None) -> str:
    """The published template as the Webhook plugin renders it, which is not a plain substitution.

    It used to be one, and so it modelled a renderer the plugin never is: every test built on it
    passed a title straight into the JSON, where the plugin (jellyfin-plugin-webhook from v14,
    Handlebars.Net 2.1.6, compiled with the default configuration) does two things first. The
    fields above reach the template with every double quote already backslash-escaped, and a
    `{{Field}}` then goes through `HtmlEncoderLegacy` -- so a quote became `\\&quot;`, which no JSON
    parser reads, and an accented letter became `&#233;`. `{{{Field}}}` writes the escaped value raw.

    Anything else between braces is refused rather than guessed at. The helper that would look
    like the answer, `{{json_encode Name}}`, first shipped in plugin 18 for Jellyfin 10.11; the
    plugin 10.9 and 10.10 run resolves no such helper, Handlebars.Net throws on it, and that add
    is never delivered at all.

    `pre_escaped=False` renders as a plugin that stopped escaping would, which is the one renderer
    under which decision 405's field order is what stands between a title and a forged field.
    [M5.2 review cycle 3: M52-C3-EVENTS-05; decision 417. Review cycle 4: M52-C4-WH-02, the encoder]
    """
    data = {field: "" for field in _ALWAYS_SET} | {k: str(v) for k, v in values.items()}
    if pre_escaped:
        data |= {k: v.replace('"', '\\"') for k, v in data.items() if k in _PRE_ESCAPED}

    def expand(match: re.Match) -> str:
        raw, encoded = match.group(1), match.group(2)
        if raw:
            return data.get(raw, "")
        if encoded:
            return _encode(data.get(encoded, ""))
        raise AssertionError(
            f"the template uses {match.group(0)}, which this renderer does not model. Model it "
            "against plugin 17 (Jellyfin 10.10) before publishing it: a helper that plugin cannot "
            "resolve throws, and the plugin then delivers nothing for that add"
        )

    # One pass, because Handlebars never reads a value it has written: a title spelling
    # `{{ItemId}}` is a title, and a second substitution would make it a field.
    source = TEMPLATE.read_text(encoding="utf-8") if template is None else template
    text = re.sub(r"\{\{!--.*?--\}\}", "", source, flags=re.S)
    return re.sub(r"\{\{\{(\w+)\}\}\}|\{\{(\w+)\}\}|\{\{.*?\}\}", expand, text).strip()


# --- decision 332: the token, and what it refuses ---------------------------------------------


async def test_a_delivery_that_carries_the_token_is_accepted_and_recorded(webhook, db):
    """The happy path, and the whole of what the route promises: accepted, written down.

    202 rather than 200 because nothing has been acquired yet - the sweep decides that minutes
    later - and one row rather than a task, because the handler that answers a sender with no
    retry may not spend the request on a decision it can take again (plan A4).
    """
    sent = await _emit(webhook["module"], item_id=MOVIE)
    assert sent["statuses"] == [202], sent

    rows = await _rows(db)
    assert len(rows) == 1
    assert (rows[0]["state"], rows[0]["reason"]) == (intake.PENDING, None)
    assert (rows[0]["item_id"], rows[0]["item_type"]) == (MOVIE, "Movie")
    assert rows[0]["resolved_key"] == MOVIE
    # The body is kept whole, so an operator whose template is wrong can be shown what arrived.
    # A dict rather than text because the fixture's connection carries the pool's jsonb codec:
    # `record_event` casts through `$1::text::jsonb`, so the column holds an object and not a
    # JSON string that a later reader would iterate character by character.
    assert rows[0]["raw"]["Name"] == "Heat"


async def test_a_wrong_token_is_refused_and_records_nothing(webhook, db):
    """Exit-criterion check 9: 401, and nothing recorded as pending.

    The second half is the one that needs a test. A handler that recorded first and refused
    second would still answer 401 and would still look right from the outside, while handing
    anyone who found the URL an unbounded writer into `jellyfin_intake` - and the table is what
    §6.6's card reads, so the flood would arrive in front of the household.
    """
    sent = await _emit(webhook["module"], item_id=MOVIE, token="not-the-token-that-was-minted")
    assert sent["statuses"] == [401], sent
    assert await _rows(db) == []


async def test_a_delivery_with_no_token_header_at_all_is_refused_and_records_nothing(webhook, db):
    """The operator who never added the header, which is the likelier of the two mistakes and
    the one an install that has minted no token would otherwise be open to: an empty stored token
    matching an absent presented one is the equality `webhook_token_matches` exists to refuse."""
    sent = await _emit(webhook["module"], item_id=MOVIE, token="")
    assert sent["statuses"] == [401], sent
    assert await _rows(db) == []


async def test_a_token_header_that_is_not_ascii_is_refused_and_records_nothing(webhook, db):
    """The third spelling of a wrong token, and the one that was not a 401 at all.

    Starlette decodes a header as latin-1, so a single byte >= 0x80 reaches
    `webhook_token_matches` as a non-ASCII `str` -- and `hmac.compare_digest` raises `TypeError`
    on those. `app.py` registers handlers for `asyncpg.PostgresError` and a list of transport
    errors and for nothing else, so the app answered an unauthenticated stranger 500 with a full
    traceback in the log, on the one route §7.2 opens to the world, before the token check had
    finished. Both parsers uvicorn can run deliver that byte, so it costs one curl.

    The header is written as raw bytes because that is the only way to send it: the double's
    emitter renders a header httpx would reject, and every other refusal in this file is a
    string an operator could type. [review cycle 1: m52-rev-events-01]
    """
    answered = await webhook["client"].post(
        "/events/jellyfin",
        json={"ItemId": MOVIE, "ItemType": "Movie"},
        headers=[(events.WEBHOOK_TOKEN_HEADER.encode("ascii"), b"\xff")],
    )
    assert answered.status_code == 401, answered.text
    assert await _rows(db) == []


async def test_an_unreadable_secrets_key_answers_with_the_reason_and_never_500s(
    webhook, db, monkeypatch
):
    """M4.7 dd03's shape on §7.2's route (decision 332), and the ordering claim inside it.

    Under a changed `SECRETS_KEY` the stored token will not open, so the app does not know whether
    this caller presented the right one. 401 would be a lie told to a correctly configured
    operator - it sends them to regenerate a secret that is fine - and 500 is the unhandled
    `InvalidTag` that reached the transport on six admin routes until M4.7. Both spellings of the
    caller are asserted, because the claim is that the answer does NOT depend on the token: the
    degradation happens before the comparison and is about the app's own custody.
    """
    monkeypatch.setenv("SECRETS_KEY", OTHER_KEY)
    settings.cache_clear()

    for label, token in (("the right token", None), ("a wrong one", "wrong")):
        sent = await _emit(webhook["module"], item_id=MOVIE, token=token)
        assert sent["statuses"] == [503], f"{label} -> {sent}"

    answered = await webhook["client"].post(
        "/events/jellyfin",
        json={"ItemId": MOVIE, "ItemType": "Movie"},
        headers={events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
    )
    assert answered.status_code == 503, answered.text
    assert registry.SECRETS_UNREADABLE_REASON in answered.json()["detail"], answered.text
    assert await _rows(db) == []


# --- decision 365: what the handler tolerates -------------------------------------------------


async def test_a_body_that_is_not_json_is_accepted_and_recorded_rather_than_refused(webhook, db):
    """The one body this file authors, and the reason is that the double cannot render it.

    The plugin writes a title into the operator's JSON with nothing that makes it JSON, so a title
    it cannot carry produces a body no parser will read - a quote under a plain `{{Name}}`, a
    backslash under any spelling (`_render` says why) - and the break happens in Handlebars, not in
    anything a server serialises, while `ops/fake_jellyfin.py` posts a dict through httpx. So the
    shape is written here and argued in `ops/jellyfin-webhook-template.json` beside the quoting
    that provokes it.

    202 and recorded, because the alternative is a 400 into a plugin with no retry and no surface:
    the operator learns nothing and the add is lost. FastAPI never sees a model here, which is what
    makes it possible - a declared body would have answered 422 before the handler ran.
    """
    answered = await webhook["client"].post(
        "/events/jellyfin",
        content=b'{"Name": "Ocean\'s "Eleven"", "ItemId": "jf-1", "ItemType": "Movie"}',
        headers={events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
    )
    assert answered.status_code == 202, answered.text
    assert answered.json()["reason"] == intake.UNREADABLE

    rows = await _rows(db)
    assert [(row["state"], row["reason"]) for row in rows] == [(intake.SKIPPED, intake.UNREADABLE)]


async def test_a_body_this_schema_cannot_hold_is_recorded_rather_than_answered_with_a_500(
    webhook, db
):
    """The other half of decision 365's "never 500", and the half a parse test cannot reach:
    four bodies `json.loads` ACCEPTS and this schema will not take.

    Each of them answered 202 from the handler and then raised out of the INSERT, so
    `app.py`'s `asyncpg.PostgresError` handler answered 500 with a traceback reading only
    "database error" -- over a delivery nothing had recorded, on a route whose sender does not
    retry. The last one never reached the INSERT at all: `RecursionError` is a `RuntimeError`,
    so the handler's `except ValueError` did not see it and Starlette answered the bare 500.

    Authored here for `...a_body_that_is_not_json...`'s reason, and a stronger form of it: these
    are bodies no `httpx` call can construct as an object and the double cannot render, because
    what makes them interesting is the bytes rather than the shape. `intake._storable` argues
    which of them are Postgres's refusal and which are the encoder's.
    """
    ok = json.dumps({"ItemId": MOVIE, "ItemType": "Movie"}).encode("utf-8")
    deep = b'{"a":' * 20_000 + b"1" + b"}" * 20_000
    bodies = {
        "a number no JSON parser reads": b'{"ItemId": "jf-1", "ItemType": "Movie", "Year": NaN}',
        "a NUL in a jsonb string": rb'{"ItemId": "jf-1", "ItemType": "Movie", "Name": "a\u0000b"}',
        "a lone surrogate": rb'{"ItemId": "jf-1", "ItemType": "Movie", "Name": "\ud800"}',
        "a body nested past the decoder\'s own limit": deep,
    }

    for label, body in bodies.items():
        await db.execute("DELETE FROM jellyfin_intake")
        answered = await webhook["client"].post(
            "/events/jellyfin",
            content=body,
            headers={events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
        )
        assert answered.status_code == 202, f"{label} -> {answered.status_code} {answered.text}"
        rows = await _rows(db)
        assert [(row["state"], row["reason"]) for row in rows] == [
            (intake.SKIPPED, intake.UNREADABLE)
        ], f"{label} was answered but not recorded"

    await db.execute("DELETE FROM jellyfin_intake")
    kept = await webhook["client"].post(
        "/events/jellyfin", content=ok,
        headers={events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
    )
    assert kept.status_code == 202, kept.text
    assert (await _rows(db))[0]["state"] == intake.PENDING, (
        "the tolerance above may not swallow a body the route can act on"
    )


async def test_a_body_larger_than_any_template_is_recorded_as_too_large_and_not_stored(
    webhook, db
):
    """Decision 365's "never 500" had no bound under it. `await request.body()` read whatever
    arrived, so a token holder's ~45 MB of JSON zeros parsed, dumped, and died in the
    `::text::jsonb` cast with "invalid memory alloc request size" -- 500, a traceback, and nothing
    recorded -- and a 50 MB string was simply stored, in a table nothing prunes. The published
    template renders under two kilobytes; a body past the cap is recorded with that reason and
    its bytes are neither buffered past the cap nor kept. A megabyte is enough to show there was
    no bound, without pushing the 45 MB statement at the shared test cluster.
    [review cycle 3: M52-C3-EVENTS-02, M52-C3-SWEEP-07]
    """
    body = json.dumps({"ItemId": MOVIE, "ItemType": "Movie", "Overview": "x" * 1_048_576})

    answered = await webhook["client"].post(
        "/events/jellyfin", content=body.encode("ascii"),
        headers={events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
    )

    assert answered.status_code == 202, answered.text
    rows = await _rows(db)
    assert [(row["state"], row["raw"]) for row in rows] == [(intake.SKIPPED, {})], (
        "a body of any size was stored whole"
    )
    assert rows[0]["reason"] == answered.json()["reason"]
    assert "too large" in rows[0]["reason"]


async def test_a_sender_that_leaves_mid_body_is_recorded_and_writes_no_traceback(webhook, db):
    """A delivery whose connection drops after the headers. `request.body()` raised Starlette's
    `ClientDisconnect`, nothing in `app.py` catches it, and `ServerErrorMiddleware` answered 500
    and re-raised, so uvicorn logged "Exception in ASGI application" with a full traceback -- on
    the one route decision 365 says never 500s, for a Jellyfin whose link merely blinked, and for
    any token holder who closes the socket on purpose. The delivery is recorded with that reason
    instead: the sender is gone, but the operator can still be shown that something arrived and
    broke. [review cycle 3: M52-C3-EVENTS-02]
    """
    application = webhook["client"]._transport.app
    messages = [
        {"type": "http.request", "body": b'{"ItemId": "jf-1", ', "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await application(_delivery(webhook["token"]), receive, send)

    assert sent and sent[0]["status"] == 202, sent[:1]
    rows = await _rows(db)
    assert [row["state"] for row in rows] == [intake.SKIPPED]
    assert "interrupted" in rows[0]["reason"]


async def test_a_body_that_trickles_holds_no_pooled_connection_and_is_cut_at_its_deadline(
    webhook, db, monkeypatch
):
    """The handler took `conn: DB`, so FastAPI checked one of the backend's ten pooled
    connections out BEFORE the body was read and held it across `await request.body()`, which
    nothing bounds -- uvicorn has no body-read timeout. Ten sockets from anyone holding the token
    (every Jellyfin admin can read it in the plugin's config) sending headers and then nothing took
    the pool to idle 0, and every phone in the household was answered 503 "database unavailable"
    for as long as they stayed open. The route is the only one in the app that reads a body by
    hand, and so the only one where a client decided how long a connection stayed checked out.

    Held here at the first chunk: the pool must be whole while the body is still arriving, and the
    read must end at its own deadline and be recorded rather than wait for a sender who never
    finishes. [review cycle 3: M52-C3-EVENTS-03]
    """
    monkeypatch.setattr(events, "BODY_DEADLINE_S", 0.5)
    application = webhook["client"]._transport.app
    first_chunk_taken, never = asyncio.Event(), asyncio.Event()
    calls = 0
    sent: list[dict] = []

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b'{"ItemId": "jf-1", ', "more_body": True}
        first_chunk_taken.set()
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    delivering = asyncio.create_task(application(_delivery(webhook["token"]), receive, send))
    try:
        await asyncio.wait_for(first_chunk_taken.wait(), 10)
        connections = pool.pool()
        assert connections.get_idle_size() == connections.get_size(), (
            "a body still arriving holds one of the pool's connections"
        )
        await asyncio.wait_for(delivering, 10)
    finally:
        delivering.cancel()

    assert sent and sent[0]["status"] == 202, sent[:1]
    rows = await _rows(db)
    assert [row["state"] for row in rows] == [intake.SKIPPED]
    assert "interrupted" in rows[0]["reason"]


async def test_a_payload_missing_the_item_id_is_accepted_and_recorded_with_that_reason(
    webhook, db
):
    """A required field an operator left out of their template: 202, recorded, enqueues nothing.

    `omit` is the double refusing to be wrong by accident - it 409s a field the template never
    sends - so this is a delivery a real plugin can produce and not a hole punched in a fixture.
    """
    sent = await _emit(webhook["module"], item_id=MOVIE, omit=["ItemId"])
    assert sent["statuses"] == [202], sent
    assert "ItemId" not in sent["payloads"][0]

    rows = await _rows(db)
    assert [(row["state"], row["reason"]) for row in rows] == [(intake.SKIPPED, intake.NO_ITEM_ID)]


async def test_a_delivery_for_another_notification_type_is_recorded_and_never_pending(webhook, db):
    """One URL can carry several of the plugin's templates, so the field is read rather than
    assumed: §7.2 names `ItemAdded` and a `PlaybackStart` pointed at this route is an operator's
    misconfiguration, which is worth recording and worth acting on never."""
    sent = await _emit(webhook["module"], item_id=MOVIE, notification_type="PlaybackStart")
    assert sent["statuses"] == [202], sent

    rows = await _rows(db)
    assert [(row["state"], row["reason"]) for row in rows] == [
        (intake.SKIPPED, intake.NOT_ITEM_ADDED)
    ]


async def test_a_burst_of_twelve_episode_deliveries_is_twelve_rows_and_one_key(webhook, db):
    """§7.2's burst as it crosses the wire: twelve requests, twelve 202s, one resolved title.

    The collapse into one job is `test_jellyfin_intake.py`'s, over the sweep. What this asserts is
    the half that only exists over HTTP - that twelve concurrent-ish deliveries each answer 202
    and each land, so the debounce is given the whole burst to collapse rather than a prefix of it.
    Keyed on `SeriesId` at the handler (decision 369): the episode ids are kept beside it, which is
    what lets §6.6 show an operator that twelve events arrived for one show.
    """
    sent = await _emit(webhook["module"], item_id=SERIES, episodes=12)
    assert sent["statuses"] == [202] * 12, sent

    rows = await _rows(db)
    assert len(rows) == 12
    assert {row["resolved_key"] for row in rows} == {SERIES}
    assert {row["item_type"] for row in rows} == {"Episode"}
    assert len({row["item_id"] for row in rows}) == 12
    assert {row["state"] for row in rows} == {intake.PENDING}


async def test_a_get_to_the_webhook_is_a_method_mismatch_and_not_a_missing_route(webhook):
    """The fourth probe decision 332's routing rule promised and could not make until a route
    existed: `/events/jellyfin` is served, so a GET to it is 405 and not 404.

    405 is the honest answer and 404 would be a lie - it would tell an operator who mistyped the
    plugin's method that the URL is wrong. The half this cannot see is the one that matters most
    on a shipped container, where the SPA fallback exists and a wrong verb once reached the app
    shell; that is `test_api_gating.py::test_the_spa_fallback_does_not_answer_for_the_api_namespace`,
    which builds a static directory to get at it.
    """
    answered = await webhook["client"].get("/events/jellyfin")
    assert answered.status_code == 405, answered.text


# --- decision 365: the template is the contract -----------------------------------------------


async def test_the_published_template_renders_a_body_this_route_acts_on(webhook, db):
    """The operator's copy, rendered and delivered: the strongest form this contract can take.

    An episode, because that is the rendering with something to get wrong - `SeriesId` is what
    turns a season into one job (decision 369), and a template that dropped it would still look
    like a working webhook until a library scan arrived. The values come out of the double's own
    payload, so the substitution is the plugin's variables against the plugin's data.
    """
    sent = await _emit(webhook["module"], item_id=SERIES, episodes=1)
    episode = sent["payloads"][0]
    await db.execute("DELETE FROM jellyfin_intake")

    answered = await webhook["client"].post(
        "/events/jellyfin",
        content=_render(episode).encode("utf-8"),
        headers={**_PLUGIN_CONTENT_TYPE, events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
    )
    assert answered.status_code == 202, answered.text

    rows = await _rows(db)
    assert [(row["state"], row["resolved_key"]) for row in rows] == [(intake.PENDING, SERIES)]
    assert rows[0]["item_id"] == episode["ItemId"]


async def test_a_title_that_forges_a_field_cannot_move_the_delivery_to_another_item(webhook, db):
    """A title shaped like a second key, rendered by the plugin and by a plugin that stopped escaping.

    The published note once said a title carrying a double quote "renders a body no JSON parser
    will read", and review cycle 1 answered that a name closing its own string and opening a second
    key renders a LEGAL object, which a parser reads last-wins -- so decision 405 moved the four
    fields the handler acts on after every free-text one. Both halves assumed the plugin writes a
    title unescaped. It does not: `Escape()` backslash-escapes every quote before Handlebars runs,
    so through the real plugin the forgery is a title with quotes in it and nothing more -- and
    under the plain `{{Name}}` the template used to publish, the HTML encoder then made that title
    unparseable, so the add was lost rather than forged. As the plugin renders it, each forgery
    below must now be filed on the plugin's own values AND pending, with its name intact.

    Rendered a second time with the escaping switched off, because under `{{{Name}}}` that
    escaping is all that keeps a quote inside its string, it is a detail of the plugin rather than
    of this app, and decision 405's order is what makes the forged duplicate inert if a plugin
    build ever drops it. Putting the JSON back into reading order is a one-line edit that looks
    like tidying; the second rendering is what would fail. Poisoning one field of the double's own
    payload, because this class exists only in the rendering: `ops/fake_jellyfin.py` posts a dict
    through httpx and can never produce it. [M5.2 review cycle 1: m52-rev-template-03; review
    cycle 3: M52-C3-EVENTS-05, decision 417]
    """
    movie = (await _emit(webhook["module"], item_id=MOVIE))["payloads"][0]
    episode = (await _emit(webhook["module"], item_id=SERIES, episodes=1))["payloads"][0]

    forgeries = [
        ("a title forging another item id", movie, "Name", f'x", "ItemId": "{MOVIE}-forged',
         (MOVIE, "Movie", MOVIE)),
        ("a title forging the notification type", movie, "Name",
         'x", "NotificationType": "PlaybackStart', (MOVIE, "Movie", MOVIE)),
        ("a title forging the item type", movie, "Name", 'x", "ItemType": "Episode',
         (MOVIE, "Movie", MOVIE)),
        ("a series name forging another show", episode, "SeriesName", f'x", "SeriesId": "{MOVIE}',
         (episode["ItemId"], "Episode", SERIES)),
    ]

    for pre_escaped in (True, False):
        renderer = "the plugin" if pre_escaped else "a plugin that stopped escaping"
        for label, payload, field, forged, expected in forgeries:
            await db.execute("DELETE FROM jellyfin_intake")
            answered = await webhook["client"].post(
                "/events/jellyfin",
                content=_render({**payload, field: forged}, pre_escaped=pre_escaped).encode("utf-8"),
                headers={**_PLUGIN_CONTENT_TYPE, events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
            )
            assert answered.status_code == 202, (
                f"{label}, rendered by {renderer} -> {answered.status_code} {answered.text}"
            )

            rows = await _rows(db)
            assert [(r["item_id"], r["item_type"], r["resolved_key"]) for r in rows] == [expected], (
                f"{label}, rendered by {renderer}: the row was not filed on the values the plugin "
                f"sent: {[(r['state'], r['reason'], r['item_id']) for r in rows]}"
            )
            assert [(r["state"], r["reason"]) for r in rows] == [(intake.PENDING, None)], (
                f"{label}, rendered by {renderer}"
            )
            if pre_escaped:
                assert rows[0]["raw"][field] == forged, (
                    f"{label}: the name reached the record as {rows[0]['raw'][field]!r}"
                )


async def test_a_title_the_plugin_writes_into_the_json_keeps_its_add_and_its_name(webhook, db):
    """What the plugin actually does to a title, and which titles the template lets through.

    The plugin writes `Name` and `SeriesName` into the operator's JSON with nothing that makes
    them JSON: `Escape()` backslash-escapes each double quote, and a `{{Field}}` then HTML-encodes
    four characters and every one above U+009F (`_render`). Under the plain `{{Name}}` the template
    used to publish, "Ocean's "Eleven"" rendered `\\&quot;` and was recorded as an unreadable payload
    -- the add never enqueued -- and "Law & Order" arrived as `Law &amp; Order` in the name §6.6's
    card shows, as every accented or non-Latin title arrived in entities; hence the two non-ASCII
    titles below. `{{{Name}}}` writes the escaped title raw, which is a legal JSON string for every
    title free of backslashes and control characters, so the first group must be pending, filed on
    the plugin's id, with the name exactly as the library has it.

    The second group is what no template can carry on the plugin Jellyfin 10.9 and 10.10 run:
    nothing there writes a backslash or a control character as JSON (`json_encode` first shipped
    in plugin 18, and an unknown helper throws). Those deliveries are still 202 and still recorded,
    and what is asserted of them is the half that cannot be negotiated -- none is ever filed under
    an id the plugin did not send -- rather than the unreadable reason they get today, so a handler
    that later learns to read them is not held back by this test. [M5.2 review cycle 3:
    M52-C3-EVENTS-05; decision 417. Review cycle 4: M52-C4-WH-02]
    """
    movie = (await _emit(webhook["module"], item_id=MOVIE))["payloads"][0]
    episode = (await _emit(webhook["module"], item_id=SERIES, episodes=1))["payloads"][0]
    carried = ['Ocean\'s "Eleven"', "Law & Order: <SVU> = `x`", '"Weird" Al', "Am\u00e9lie",
               "\u4e03\u4eba\u306e\u4f8d"]
    uncarried = ["AC\\DC", "a trailing backslash\\", "two\nlines", "a\ttab", "\\u0000",
                 f'x\\", "ItemId": "{MOVIE}-forged']

    for title in (*carried, *uncarried):
        for payload, field, expected in (
            (movie, "Name", (MOVIE, MOVIE)),
            (episode, "SeriesName", (episode["ItemId"], SERIES)),
        ):
            await db.execute("DELETE FROM jellyfin_intake")
            answered = await webhook["client"].post(
                "/events/jellyfin",
                content=_render({**payload, field: title}).encode("utf-8"),
                headers={**_PLUGIN_CONTENT_TYPE, events.WEBHOOK_TOKEN_HEADER: webhook["token"]},
            )
            assert answered.status_code == 202, f"{field}={title!a} -> {answered.status_code}"
            rows = await _rows(db)
            assert len(rows) == 1, f"{field}={title!a}: {len(rows)} rows recorded"
            (row,) = rows
            assert row["state"] != intake.PENDING or (row["item_id"], row["resolved_key"]) == expected, (
                f"{field}={title!a} was filed under {row['item_id']!r}, which the plugin never sent"
            )
            if title in carried:
                assert (row["state"], row["reason"]) == (intake.PENDING, None), (
                    f"{field}={title!a} lost its add: {row['state']} {row['reason']!r}"
                )
                assert (row["item_id"], row["resolved_key"]) == expected, f"{field}={title!a}"
                assert row["raw"][field] == title, (
                    f"{field}={title!a} reached the record as {row['raw'][field]!a}"
                )


def test_a_two_brace_field_is_encoded_the_way_the_plugins_handlebars_encodes_it():
    """The renderer every template test above stands on, held against the encoder the plugin runs.

    `_render` is a double of the plugin's renderer, and it modelled the wrong one of the two
    encoders Handlebars.Net ships. 2.1.6's `HandlebarsConfiguration` constructor sets
    `TextEncoder = new HtmlEncoderLegacy();`, and the plugin's `BaseOption.GetCompiledTemplate`
    compiles through the static `Handlebars`, whose configuration is that default and which
    nothing in the plugin replaces. `HtmlEncoderLegacy.EncodeImpl` rewrites `"`, `&`, `<` and `>`,
    passes an apostrophe, a backtick and `=`, and writes every `char` above 159 as `&#N;` -- a C#
    `char` is one UTF-16 code unit, so a character outside the BMP is two entities. The expected
    strings are that source worked by hand, not `_HTML_ENCODED` read back: a table checked against
    itself is how the other encoder survived a review cycle. No delivery differed, because every
    two-brace field of the published template is ASCII; the first free-text field an edit wrote
    with two braces would have been tested against the opposite of what the plugin sends.

    Read at https://raw.githubusercontent.com/Handlebars-Net/Handlebars.Net/2.1.6/source/Handlebars/
    (`Configuration/HandlebarsConfiguration.cs:91`, `IO/HtmlEncoderLegacy.cs:43-73`,
    `StringUtils/StringEnumerator.cs:28`) and jellyfin-plugin-webhook v17 and v18
    (`Jellyfin.Plugin.Webhook.csproj:16` pins 2.1.6; `Destinations/BaseOption.cs:106`).
    [M5.2 review cycle 4: M52-C4-WH-02; decision 417]
    """
    encoded = {
        "Ocean's Eleven": "Ocean's Eleven",
        "`x` = y": "`x` = y",
        "AC\\DC": "AC\\DC",
        # `Name` reaches Handlebars through `Escape()`, so its quotes are backslashed first.
        'Law & Order: <SVU> "x"': "Law &amp; Order: &lt;SVU&gt; \\&quot;x\\&quot;",
        "Am\u00e9lie": "Am&#233;lie",
        "\u4e03\u4eba\u306e\u4f8d": "&#19971;&#20154;&#12398;&#20365;",
        "\u009f": "\u009f",
        "\u00a0": "&#160;",
        "\U0001f600": "&#55357;&#56832;",
    }
    for title, expected in encoded.items():
        rendered = _render({"Name": title}, template="|{{Name}}|")
        assert rendered == f"|{expected}|", (
            f"{{{{Name}}}} = {title!a} rendered {rendered!a}; Handlebars.Net 2.1.6 writes {expected!a}"
        )
        raw = _render({"Name": title}, template="|{{{Name}}}|")
        assert raw == "|" + title.replace('"', '\\"') + "|", (
            f"{{{{{{Name}}}}}} = {title!a} rendered {raw!a}, which three braces never encode"
        )


def test_the_published_template_names_only_fields_the_plugin_actually_sends():
    """A template naming a variable Jellyfin does not fill renders an empty string and teaches
    the operator that the app ignores the field - which is how a required field becomes optional
    by accident. Held against the double, because the double is where the plugin's shape lives."""
    rendered = _render({})
    named = set(re.findall(r'"(\w+)":', TEMPLATE.read_text(encoding="utf-8").split("--}}")[1]))

    assert json.loads(rendered) is not None, "the published template must render parseable JSON"
    assert named >= {"NotificationType", "ItemId", "ItemType", "Name", "SeriesId", "SeriesName",
                     "Year"}, f"the template dropped a field decision 365 publishes: {named}"


async def test_the_published_template_carries_only_keys_the_double_can_render(webhook):
    """The other direction of the same rule, and the one that rots: a key this app invented would
    stay empty on every delivery for ever, and nothing about a webhook that works would say so."""
    movie = (await _emit(webhook["module"], item_id=MOVIE))["payloads"][0]
    episode = (await _emit(webhook["module"], item_id=SERIES, episodes=1))["payloads"][0]

    named = set(re.findall(r'"(\w+)":', TEMPLATE.read_text(encoding="utf-8").split("--}}")[1]))
    assert named <= set(movie) | set(episode), (
        "the published template names fields the Webhook plugin does not send: "
        f"{sorted(named - (set(movie) | set(episode)))}"
    )


def test_the_published_template_tells_the_operator_what_the_route_requires():
    """The note is the half of the contract a machine cannot check twice, so what it says is
    checked once: the path to POST to, the header this module names, and the two fields decision
    365 requires. Renaming `WEBHOOK_TOKEN_HEADER` without re-cutting the template fails here,
    which is the drift that would otherwise be found by an operator and not by this suite."""
    note = TEMPLATE.read_text(encoding="utf-8").split("--}}")[0]

    assert "/events/jellyfin" in note
    assert events.WEBHOOK_TOKEN_HEADER in note
    for field in ("ItemId", "ItemType"):
        assert field in note, f"the note does not say that {field} is required"


# The Webhook plugin's Item Type boxes, labelled as its destination form labels them, and the item
# class each one lets through. Read from the plugin's own source at v17 -- the release Jellyfin 10.10
# runs -- and unchanged at v18 (https://raw.githubusercontent.com/jellyfin/jellyfin-plugin-webhook/
# v17/Jellyfin.Plugin.Webhook/): the labels are Configuration/Web/config.html:97-125, and the gate
# is WebhookSender.cs:144-177, `if (baseOptions.EnableSeasons && itemType == typeof(Season))` and
# its six siblings.
PLUGIN_ITEM_TYPE_BOXES = {
    "Movies": "Movie", "Episodes": "Episode", "Season": "Season", "Series": "Series",
    "Albums": "MusicAlbum", "Songs": "Audio", "Videos": "Video",
}


def test_the_published_template_names_the_item_types_to_untick_and_not_only_those_to_tick():
    """A destination the plugin has just added has every one of those boxes ticked, and the note
    named only the three to keep.

    The plugin's config.js:629 hands the "Add Generic Destination" click to `generic.addConfig` as
    its `config`, and `baseConfig.setConfig` (:156-162) ticks each box whose field is undefined --
    on a click event, all seven. So an operator who did what the note said, ticking Movies, Series
    and Episodes, left Season, Albums, Songs and Videos ticked beside them, and every season of a
    new show and every track of an album became a delivery: an intake row, a read of the server
    inside the sweep's budget and a skipped row whose reason reads as a fault when there is none.
    The boxes to keep are the ones this app acts on -- what `resolve.kind_of` acquires, and the
    Episode `intake.read_event` resolves through its show (decision 369) -- so they are derived
    here rather than listed, and every other box is one the note must tell the operator to untick.
    [M5.2 review cycle 4: M52-C4-WH-03; decision 365]
    """
    note = TEMPLATE.read_text(encoding="utf-8").split("--}}")[0]
    kept = {
        label for label, kind in PLUGIN_ITEM_TYPE_BOXES.items()
        if resolve.kind_of({"Type": kind}) is not None or kind == intake.EPISODE
    }
    row = re.search(r"^\s*Item Type\s+tick (?P<keep>[\w, ]+?); UNTICK (?P<drop>[\w, ]+?)\s*$",
                    note, flags=re.M)

    assert row, "the note's Item Type row must name the boxes to untick, not only those to tick"
    assert {label.strip() for label in row["keep"].split(",")} == kept
    assert {label.strip() for label in row["drop"].split(",")} == set(PLUGIN_ITEM_TYPE_BOXES) - kept
