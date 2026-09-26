"""The `/events` namespace and §7.2's Jellyfin webhook (decisions 332, 365, 367). No SQL here: the
token check, debounce and enqueue live in `connectors/` and `acquire/`. The operator's template is
`ops/jellyfin-webhook-template.json`, which is the contract."""

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

# A header: the only way the Webhook plugin's Generic destination carries a secret, and it stays out
# of access logs. A constant, not a setting (decision 365).
WEBHOOK_TOKEN_HEADER = "X-Spielplan-Token"

# Bytes. The template renders under 2 KB; the headroom lets a too-deep body reach `json.loads` and
# be recorded as unreadable.
MAX_BODY_BYTES = 256 * 1024

# Seconds for the body once the token is accepted: uvicorn has no body-read timeout.
BODY_DEADLINE_S = 30.0


@asynccontextmanager
async def _connection() -> AsyncIterator[asyncpg.Connection]:
    """`deps.db` driven by hand, so no connection is held while the body is awaited: slow senders
    holding the token could otherwise drain the pool."""
    source = deps.db()
    conn = await anext(source)
    try:
        yield conn
    finally:
        await source.aclose()


async def _read_body(request: Request) -> bytes | str:
    """The bytes, or `intake.PAYLOAD_TOO_LARGE` / `DELIVERY_INTERRUPTED`; never an exception
    (`ClientDisconnect` would 500)."""
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
    """§7.2: recorded, never acquired here, and 202 because the plugin fires and forgets. The order is
    the security: 503 while the stored token is unreadable, 401 before the body is read (a stranger
    writes nothing), then every body is recorded with its reason and answered 202 (decision 365)."""
    async with _connection() as conn:
        cfg = await registry.load_jellyfin(conn)
    if cfg.secrets_unreadable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{registry.SECRETS_UNREADABLE_REASON}: this delivery cannot be authenticated until "
            "the Jellyfin connector's secrets open again",
        )
    if not cfg.webhook_token_matches(request.headers.get(WEBHOOK_TOKEN_HEADER)):
        # One answer for a missing and a wrong token: the repair is the same, and it reveals nothing.
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
        # Unparseable (RecursionError included, a RuntimeError): recorded as unreadable, bytes not kept.
        payload = None
    async with _connection() as conn:
        event = await intake.record_event(conn, payload)
    return {"recorded": event.id, "state": event.state, "reason": event.reason}
