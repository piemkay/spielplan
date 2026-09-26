"""One fetch, filed in the raw store. Every adapter goes through here.

Files under `ctx.task.key` and the made-against `request_url` (what `_validators` reads). Stores
non-retryable failures only. TMDB/OMDb keys ride in the stored url; bounded, see decision 377.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from spielplan.acquire import fetch, rawstore

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

# Anything shorter is a challenge page or bot block. Only applied where a caller asks: JSON may be tiny.
MIN_HTML_BYTES = 512

JSON = "application/json"
HTML = "text/html"


def json_body(response: fetch.Response) -> str:
    """A body that does not parse as JSON, said once for every caller that asked for JSON.

    A captive portal answers 200; filed `ok` it would displace the last good document.
    """
    return _decode(response)[1]


def json_object(response: fetch.Response) -> str:
    """`json_body`, and the value has to be a JSON object (Trakt's array pages use `json_body`)."""
    data, note = _decode(response)
    if note:
        return note
    if not isinstance(data, dict):
        return f"answered HTTP {response.status} with JSON that is not an object"
    return ""


def _decode(response: fetch.Response) -> tuple[Any, str]:
    """The body as JSON, or the sentence saying it is not one."""
    try:
        return json.loads(response.text), ""
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, f"answered HTTP {response.status} with a body that is not JSON"


@dataclass(frozen=True)
class View:
    """One url to try for a title. `name` suffixes the stored `kind`, keeping two views apart."""

    name: str
    url: str
    page: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Captured:
    """What one request did. `error` empty means stored and parsable; `unchanged` means a 304."""

    doc_id: int | None = None
    response: fetch.Response | None = None
    error: str = ""
    unchanged: bool = False
    # Which view this came from.
    name: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def content(self) -> bytes:
        """The bytes, or empty. A caller that needs them has already checked `ok`."""
        return self.response.content if self.response is not None else b""


async def capture(
    ctx: StageContext,
    *,
    source: str,
    kind: str,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    page: int = 0,
    request_meta: dict[str, Any] | None = None,
    content_type: str = JSON,
    min_bytes: int = 0,
    verdict: Callable[[fetch.Response], str] | None = None,
    name: str = "",
) -> Captured:
    """Fetch one url politely and file the answer. The only way bytes enter this app at stage 2.

    Every failure returns as `Captured.error`, never raised (decision 334). `verdict` judges a refusal
    inside a 200 (OMDb); it sets only the `ok` flag and the bytes are stored either way.
    """
    try:
        resp = await ctx.fetcher.get(url, params=params, headers=headers, conditional=True)
    except fetch.HostPaused as exc:
        # No request was made, so no raw row (decision 422).
        return Captured(error=f"{exc}, so it was not asked", name=name)
    except fetch.FetchError as exc:
        if exc.retryable:
            return Captured(error=str(exc), name=name)
        # Filed under the bare url: there is no `request_url` to read, and the query may hold a
        # credential.
        doc_id = await rawstore.store(
            ctx.conn, source=source, kind=kind, url=url, content=b"",
            entity_key=ctx.task.key, page=page, http_status=exc.status,
            content_type=content_type, ok=False, error=str(exc), run_id=ctx.run_id,
        )
        return Captured(doc_id=doc_id, error=str(exc), name=name)

    if resp.from_cache:
        # A 304: the bytes already held are the document.
        return Captured(response=resp, unchanged=True, name=name)

    # A zero-length 200 always fails (the store refuses empty documents by raising), and before the
    # verdict, since there is no document to judge. JSON callers default to `json_object`.
    note = ""
    if len(resp.content) < max(min_bytes, 1):
        note = f"empty body (HTTP {resp.status}, {len(resp.content)}b)"
    elif verdict is not None:
        note = verdict(resp)
    elif "json" in content_type:
        note = json_object(resp)
    if note:
        doc_id = await rawstore.store(
            ctx.conn, source=source, kind=kind, url=resp.request_url, content=resp.content,
            entity_key=ctx.task.key, page=page, http_status=resp.status,
            content_type=resp.content_type or content_type, request_meta=request_meta,
            ok=False, error=note, run_id=ctx.run_id,
        )
        return Captured(doc_id=doc_id, response=resp, error=note, name=name)

    doc_id = await rawstore.store(
        ctx.conn, source=source, kind=kind, url=resp.request_url, content=resp.content,
        entity_key=ctx.task.key, page=page, http_status=resp.status,
        content_type=resp.content_type or content_type, request_meta=request_meta,
        etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"),
        run_id=ctx.run_id,
    )
    return Captured(doc_id=doc_id, response=resp, name=name)


async def fetch_views(
    ctx: StageContext,
    *,
    source: str,
    kind_prefix: str,
    views: Iterable[View],
    content_type: str = HTML,
    min_bytes: int = MIN_HTML_BYTES,
) -> list[Captured]:
    """Fetch each view independently; a title with one of two views is worth more than none."""
    return [
        await capture(
            ctx, source=source, kind=f"{kind_prefix}:{view.name}", url=view.url,
            page=view.page, request_meta=view.meta or None, content_type=content_type,
            min_bytes=min_bytes, name=view.name,
        )
        for view in views
    ]


def notes(captured: Iterable[Captured]) -> str:
    """The failures among `captured`, as one board sentence; empty when nothing failed."""
    return "; ".join(
        f"{cap.name}: {cap.error}" if cap.name else cap.error
        for cap in captured if not cap.ok
    )


__all__ = [
    "HTML",
    "JSON",
    "MIN_HTML_BYTES",
    "Captured",
    "View",
    "capture",
    "fetch_views",
    "json_body",
    "json_object",
    "notes",
]
