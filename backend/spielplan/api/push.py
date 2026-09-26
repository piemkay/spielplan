"""Web-push subscriptions (§4.2). One row per endpoint (a resubscribe upserts); every read is scoped
to the member; the endpoint and `auth` never leave the API; writes need the credential, not a PIN
(decision 170). A stored endpoint is a URL the backend will POST to, so it is validated (sec-13)."""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from spielplan.api.deps import DB, ActiveUser, CredentialedUser
from spielplan.connectors.registry import JELLYFIN
from spielplan.core.config import settings
from spielplan.push import keys
from spielplan.push.send import device_handle

router = APIRouter(prefix="/api/push", tags=["push"])
log = logging.getLogger(__name__)


class SubscriptionKeys(BaseModel):
    """The two keys `PushSubscription.toJSON()` carries, under the names the browser uses."""

    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)


class SubscriptionIn(BaseModel):
    # `subscription.toJSON()` plus a label, as posted; extra browser fields are ignored.
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: SubscriptionKeys
    device_label: str | None = Field(default=None, max_length=64)

    @field_validator("endpoint")
    @classmethod
    def _endpoint_is_a_push_service_on_the_internet(cls, value: str) -> str:
        """§14.3: https, and a literal address must be `is_global` (this covers Tailscale's 100.64/10, which
        `is_private` misses since 3.12.4). Names are not resolved; no domain allowlist."""
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("a push endpoint must be an https URL")
        host = (parts.hostname or "").strip().rstrip(".")
        if not host:
            raise ValueError("a push endpoint must name a host")
        if host.lower() == "localhost":
            raise ValueError("a push endpoint may not name this machine")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return value
        if not address.is_global:
            raise ValueError("a push endpoint may not name a private or local address")
        return value


class EndpointIn(BaseModel):
    # Unvalidated: a row stored before the rule must stay deletable by its string.
    endpoint: str = Field(min_length=1, max_length=2048)


async def _refuse_a_household_host(conn, endpoint: str) -> None:
    """The Jellyfin server and the app itself are public names but never push services. The URL is read
    raw so an unreadable DEK cannot fail a subscribe (§3.1)."""
    host = (urlsplit(endpoint).hostname or "").strip().rstrip(".").lower()
    configured = await conn.fetchval(
        "SELECT config ->> 'url' FROM connector_config WHERE name = $1", JELLYFIN
    )
    ours = {
        (urlsplit(str(configured or "")).hostname or "").strip().rstrip(".").lower(),
        settings().rp_id.strip().rstrip(".").lower(),
    }
    if host and host in ours:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a push endpoint may not name this household's own servers",
        )


async def vapid_public_key(conn) -> str | None:
    """From the database only: an env override could hand browsers a key nothing can sign for. None
    when unconfigured, which the onboarding screen renders."""
    return await keys.public_key(conn)


async def _subscriptions(conn, user_id: int) -> list[dict[str, object]]:
    """This member's devices, never the household's."""
    rows = await conn.fetch(
        """
        SELECT id, device_label, endpoint, created_at, last_seen_ok
        FROM push_subscription WHERE user_id = $1 ORDER BY created_at
        """,
        user_id,
    )
    return [
        {
            "id": row["id"],
            "device_label": row["device_label"],
            # Deliberately not the endpoint: see `device_handle`.
            "device": device_handle(row["endpoint"]),
            "created_at": row["created_at"],
            "last_seen_ok": row["last_seen_ok"],
        }
        for row in rows
    ]


async def _onboarding_done(conn, user_id: int) -> bool:
    """§3.1's onboarding step, per user: stored as `{user_id: true}` in one `setup_step` row."""
    detail = await conn.fetchval("SELECT detail FROM setup_step WHERE step = 'onboarding'")
    return bool((detail or {}).get(str(user_id)))


@router.get("/state")
async def state(user: ActiveUser, conn: DB) -> dict[str, object]:
    return {
        "onboarding_complete": await _onboarding_done(conn, user.id),
        "vapid_public_key": await vapid_public_key(conn),
        "subscriptions": await _subscriptions(conn, user.id),
    }


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(body: SubscriptionIn, user: CredentialedUser, conn: DB) -> dict[str, object]:
    """Upserts on `endpoint` and moves it to the caller: notifications follow the person at the phone.
    `created_at` is refreshed (a conflict proves the device live) while `last_seen_ok` is left alone,
    so the 90-day prune never takes a live phone."""
    await _refuse_a_household_host(conn, body.endpoint)
    handle = device_handle(body.endpoint)
    row = await conn.fetchrow(
        """
        INSERT INTO push_subscription (user_id, device_label, endpoint, p256dh, auth)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (endpoint) DO UPDATE SET
            user_id      = EXCLUDED.user_id,
            device_label = COALESCE(EXCLUDED.device_label, push_subscription.device_label),
            p256dh       = EXCLUDED.p256dh,
            auth         = EXCLUDED.auth,
            created_at   = now()
        RETURNING id, created_at
        """,
        user.id,
        body.device_label,
        body.endpoint,
        body.keys.p256dh,
        body.keys.auth,
    )
    # The handle, never the endpoint or the auth key.
    log.info("push subscription stored for user %s (device %s)", user.id, handle)
    return {
        "ok": True,
        "id": row["id"],
        "device": handle,
        "subscriptions": await _subscriptions(conn, user.id),
    }


@router.delete("/subscription")
async def unsubscribe(body: EndpointIn, user: CredentialedUser, conn: DB) -> dict[str, object]:
    """`user_id = $1` is load-bearing: an endpoint string alone must not silence another member's phone."""
    removed = await conn.fetchval(
        "DELETE FROM push_subscription WHERE user_id = $1 AND endpoint = $2 RETURNING id",
        user.id,
        body.endpoint,
    )
    if removed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device on this account")
    log.info("push subscription removed for user %s (device %s)", user.id, device_handle(body.endpoint))
    return {"ok": True, "subscriptions": await _subscriptions(conn, user.id)}
