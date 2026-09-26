"""§7.2's `POST /events/jellyfin` over HTTP (decisions 332, 365): the token
is checked before any row, a body it cannot act on is 202 and recorded,
never 400 or 500. Payloads are the double's. Needs TEST_DATABASE_URL."""

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
# Over `core/config`'s length floor, so the refusal under test is custody, not configuration.
OTHER_KEY = "a-different-secrets-key-not-a-real-one"

# The Bear, which the double gives twelve episodes for §7.2's burst.
SERIES = "jf-7"
MOVIE = "jf-1"


@pytest.fixture
async def webhook(secrets_key, db, app, fake_jellyfin, monkeypatch):
    """The token is read from the database, never from a response:
    a test that could fetch it would be asserting a leak."""
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
    """The statuses the route answered, and the payloads the double rendered to get them."""
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
    """httpx hands the app the whole body at once, so a stalling sender is only reachable one layer down."""
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


# Handlebars.Net 2.1.6's `HtmlEncoderLegacy`: these four, and every UTF-16 unit above 159 as `&#N;`.
_HTML_ENCODED = {'"': "&quot;", "&": "&amp;", "<": "&lt;", ">": "&gt;"}
# Filled for EVERY item; `SeriesId`, `SeriesName`, `Year`
# only where set. Those three pass through `Escape()`.
_ALWAYS_SET = ("NotificationType", "Name", "ItemId", "ItemType")
_PRE_ESCAPED = ("Name", "SeriesName", "ItemType")
# `GenericClient.SendAsync` sends `text/plain` UTF-8, never the JSON type httpx's `json=` uses.
_PLUGIN_CONTENT_TYPE = {"Content-Type": "text/plain; charset=utf-8"}


def _encode(value: str) -> str:
    """One UTF-16 code unit at a time, so a non-BMP character is two surrogate entities."""
    units = value.encode("utf-16-le", "surrogatepass")
    return "".join(
        _HTML_ENCODED.get(chr(unit), f"&#{unit};" if unit > 159 else chr(unit))
        for unit in (int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2))
    )


def _render(values: dict, *, pre_escaped: bool = True, template: str | None = None) -> str:
    """As the plugin renders it: `Escape()` backslashes quotes, then `{{Field}}` HTML-encodes; `{{{Field}}}`
    writes raw. Unknown helpers are refused, as Handlebars throws on them."""
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

    # One pass: Handlebars never re-reads a value it has written.
    source = TEMPLATE.read_text(encoding="utf-8") if template is None else template
    text = re.sub(r"\{\{!--.*?--\}\}", "", source, flags=re.S)
    return re.sub(r"\{\{\{(\w+)\}\}\}|\{\{(\w+)\}\}|\{\{.*?\}\}", expand, text).strip()


async def test_a_delivery_that_carries_the_token_is_accepted_and_recorded(webhook, db):
    """202, not 200: nothing is acquired yet; one row, not a task, since the sender never retries."""
    sent = await _emit(webhook["module"], item_id=MOVIE)
    assert sent["statuses"] == [202], sent

    rows = await _rows(db)
    assert len(rows) == 1
    assert (rows[0]["state"], rows[0]["reason"]) == (intake.PENDING, None)
    assert (rows[0]["item_id"], rows[0]["item_type"]) == (MOVIE, "Movie")
    assert rows[0]["resolved_key"] == MOVIE
    # Kept whole for the operator; the column holds an object via `$1::text::jsonb`.
    assert rows[0]["raw"]["Name"] == "Heat"


async def test_a_wrong_token_is_refused_and_records_nothing(webhook, db):
    """Recording before refusing would still answer 401 while handing anyone an unbounded writer."""
    sent = await _emit(webhook["module"], item_id=MOVIE, token="not-the-token-that-was-minted")
    assert sent["statuses"] == [401], sent
    assert await _rows(db) == []


async def test_a_delivery_with_no_token_header_at_all_is_refused_and_records_nothing(webhook, db):
    """An empty stored token must not match an absent presented one."""
    sent = await _emit(webhook["module"], item_id=MOVIE, token="")
    assert sent["statuses"] == [401], sent
    assert await _rows(db) == []


async def test_a_token_header_that_is_not_ascii_is_refused_and_records_nothing(webhook, db):
    """Starlette decodes headers as latin-1, and `hmac.compare_digest` raises on a non-ASCII `str`."""
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
    """401 would mislead a correctly configured operator; the degradation precedes the comparison."""
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


async def test_a_body_that_is_not_json_is_accepted_and_recorded_rather_than_refused(webhook, db):
    """Authored here, since the double posts dicts: a 400 into a plugin with no retry loses the add."""
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
    """Bodies `json.loads` accepts and the schema will not take; `RecursionError` is not a `ValueError`."""
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
    """The template renders under 2 KB; a body past the cap is recorded with its reason, not stored."""
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
    """`ClientDisconnect` reached `ServerErrorMiddleware` as a 500; it is recorded instead."""
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
    """No pooled connection is held while the body arrives, and the read ends at its own deadline."""
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
    """`omit` makes the double 409 a field the template never sends, so this is a real delivery."""
    sent = await _emit(webhook["module"], item_id=MOVIE, omit=["ItemId"])
    assert sent["statuses"] == [202], sent
    assert "ItemId" not in sent["payloads"][0]

    rows = await _rows(db)
    assert [(row["state"], row["reason"]) for row in rows] == [(intake.SKIPPED, intake.NO_ITEM_ID)]


async def test_a_delivery_for_another_notification_type_is_recorded_and_never_pending(webhook, db):
    """One URL can carry several templates, so the notification type is read, not assumed."""
    sent = await _emit(webhook["module"], item_id=MOVIE, notification_type="PlaybackStart")
    assert sent["statuses"] == [202], sent

    rows = await _rows(db)
    assert [(row["state"], row["reason"]) for row in rows] == [
        (intake.SKIPPED, intake.NOT_ITEM_ADDED)
    ]


async def test_a_burst_of_twelve_episode_deliveries_is_twelve_rows_and_one_key(webhook, db):
    """Keyed on `SeriesId` (decision 369), with the episode ids kept beside it."""
    sent = await _emit(webhook["module"], item_id=SERIES, episodes=12)
    assert sent["statuses"] == [202] * 12, sent

    rows = await _rows(db)
    assert len(rows) == 12
    assert {row["resolved_key"] for row in rows} == {SERIES}
    assert {row["item_type"] for row in rows} == {"Episode"}
    assert len({row["item_id"] for row in rows}) == 12
    assert {row["state"] for row in rows} == {intake.PENDING}


async def test_a_get_to_the_webhook_is_a_method_mismatch_and_not_a_missing_route(webhook):
    """404 would tell an operator who mistyped the method that the URL is wrong."""
    answered = await webhook["client"].get("/events/jellyfin")
    assert answered.status_code == 405, answered.text


async def test_the_published_template_renders_a_body_this_route_acts_on(webhook, db):
    """An episode: `SeriesId` is what turns a season into one job."""
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
    """The real plugin escapes quotes, so a forged key is only a title. Rendered again unescaped,
    decision 405's field order is what keeps the forged duplicate inert."""
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
    """`{{{Name}}}` keeps any title free of backslashes and control
    characters; those still never file under a foreign id."""
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
    """Worked by hand from Handlebars.Net 2.1.6 `HtmlEncoderLegacy.EncodeImpl` (the plugin's default
    encoder), not read back from `_HTML_ENCODED`: a table checked against itself proves nothing."""
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
    """A variable Jellyfin does not fill renders empty, and a required field quietly becomes optional."""
    rendered = _render({})
    named = set(re.findall(r'"(\w+)":', TEMPLATE.read_text(encoding="utf-8").split("--}}")[1]))

    assert json.loads(rendered) is not None, "the published template must render parseable JSON"
    assert named >= {"NotificationType", "ItemId", "ItemType", "Name", "SeriesId", "SeriesName",
                     "Year"}, f"the template dropped a field decision 365 publishes: {named}"


async def test_the_published_template_carries_only_keys_the_double_can_render(webhook):
    """A key this app invented would stay empty on every delivery for ever."""
    movie = (await _emit(webhook["module"], item_id=MOVIE))["payloads"][0]
    episode = (await _emit(webhook["module"], item_id=SERIES, episodes=1))["payloads"][0]

    named = set(re.findall(r'"(\w+)":', TEMPLATE.read_text(encoding="utf-8").split("--}}")[1]))
    assert named <= set(movie) | set(episode), (
        "the published template names fields the Webhook plugin does not send: "
        f"{sorted(named - (set(movie) | set(episode)))}"
    )


def test_the_published_template_tells_the_operator_what_the_route_requires():
    """Renaming `WEBHOOK_TOKEN_HEADER` without re-cutting the template fails here."""
    note = TEMPLATE.read_text(encoding="utf-8").split("--}}")[0]

    assert "/events/jellyfin" in note
    assert events.WEBHOOK_TOKEN_HEADER in note
    for field in ("ItemId", "ItemType"):
        assert field in note, f"the note does not say that {field} is required"


# The plugin's Item Type boxes and the class each lets through, per jellyfin-plugin-webhook v17/v18 source.
PLUGIN_ITEM_TYPE_BOXES = {
    "Movies": "Movie", "Episodes": "Episode", "Season": "Season", "Series": "Series",
    "Albums": "MusicAlbum", "Songs": "Audio", "Videos": "Video",
}


def test_the_published_template_names_the_item_types_to_untick_and_not_only_those_to_tick():
    """A new destination has every box ticked, so the note must name the boxes to untick."""
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
