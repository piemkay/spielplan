"""Web-push subscriptions. Spec v2.1 §4.2, §6 preamble, §7.3, §12 (M2).

§4.2 carries the table and the worker already prunes it; what M2 owes is the write that fills
it. §7.3's finish prompt "queues and surfaces as an in-app banner" *when undeliverable* — a
fallback that only means something once the deliverable path exists.

Three properties this module is built around, all of them from §6's preamble and §4.2:

  * **One row per endpoint.** A phone re-registers its service worker on every app update and
    resubscribes with the same endpoint each time; if that inserted, a household of two phones
    would accumulate a row per release and every notification would arrive n times. `endpoint`
    is UNIQUE in the schema, so the upsert is on the endpoint, not on (user, endpoint).
  * **A subscription belongs to the member, not to the household.** §4.2 keys the table on
    `user_id`, and §7.3's prompt is "per-user": every read here is scoped to the signed-in
    member, so one member's phone can never be handed another member's notification.
  * **The keys are secrets.** The endpoint URL is a bearer capability — anyone holding it can
    push to that device — and `auth` is the message-encryption key. Neither is logged, and
    neither comes back out of the API. Devices are identified to the UI by a hash of the
    endpoint instead.

The *sending* half is M4's `spielplan.push` (§12); this is the subscribe/unsubscribe path plus
the read the onboarding screen needs. `router` is exported for `spielplan.app` to register.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser
from spielplan.push import keys

router = APIRouter(prefix="/api/push", tags=["push"])
log = logging.getLogger(__name__)


class SubscriptionKeys(BaseModel):
    """The two keys `PushSubscription.toJSON()` carries, under the names the browser uses."""

    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)


class SubscriptionIn(BaseModel):
    # Shaped so the client can post `subscription.toJSON()` plus a label, unchanged. Extra
    # fields (`expirationTime`) are ignored rather than rejected: browsers add to that object.
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: SubscriptionKeys
    device_label: str | None = Field(default=None, max_length=64)


class EndpointIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


def device_handle(endpoint: str) -> str:
    """A stable, non-reversible name for one push target.

    The endpoint is a bearer capability, so it must not reach a log line, a UI string or an
    error message. A hash prefix is enough for both jobs it has to do: tell one device apart
    from another in the account list, and let an operator correlate log lines.
    """
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


async def vapid_public_key(conn) -> str | None:
    """The application server key the browser needs at subscribe time, or None.

    Chrome and Edge refuse `pushManager.subscribe()` without one. M2 read `VAPID_PUBLIC_KEY`
    from the environment because the sender that owns the pair was still M4 work; that env var
    is gone with this milestone, and deliberately not kept as an override. A subscription is
    bound to the key it was created against, so an env-supplied public half whose private half
    nothing holds would let every browser subscribe against a key `push/send.py` cannot sign
    with — the push service would reject every delivery and no screen would ever say so. §2
    puts the pair in the database ("generated at first boot and stored the same way"), and the
    database is now the only answer.

    Still None when the pair is absent (§3.1's half-configured boot), which the onboarding
    screen renders as "notifications aren't configured yet" rather than throwing a
    DOMException at the member.
    """
    return await keys.public_key(conn)


async def _subscriptions(conn, user_id: int) -> list[dict[str, object]]:
    """This member's devices. Never the household's — see the module docstring."""
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
    """§3.1's fifth step, read back per user.

    `POST /api/setup/onboarding/complete` records completion as `{user_id: true}` inside one
    `setup_step` row, because §6's preamble makes onboarding a per-phone act — one member
    finishing it must not silence the prompt for the other. Nothing read that back until now,
    which is why the screen could not know whether to ask.
    """
    detail = await conn.fetchval("SELECT detail FROM setup_step WHERE step = 'onboarding'")
    return bool((detail or {}).get(str(user_id)))


@router.get("/state")
async def state(user: ActiveUser, conn: DB) -> dict[str, object]:
    """Everything the onboarding screen needs, in one round trip."""
    return {
        "onboarding_complete": await _onboarding_done(conn, user.id),
        "vapid_public_key": await vapid_public_key(conn),
        "subscriptions": await _subscriptions(conn, user.id),
    }


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(body: SubscriptionIn, user: ActiveUser, conn: DB) -> dict[str, object]:
    """Store a browser PushSubscription for the signed-in member.

    ON CONFLICT on `endpoint` rather than an insert: a phone resubscribes with the same
    endpoint every time its service worker is replaced, and §4.2's UNIQUE says so — one
    endpoint is one device, and one device is one row.

    The conflicting row's `user_id` is overwritten on purpose. An endpoint is minted per
    browser profile, so the only way an existing endpoint arrives under a different member is
    that the same browser is now signed in as that member — and the notifications must follow
    the person at the phone, not the person who first granted permission on it.

    `last_seen_ok` goes back to NULL: it means "the push service accepted a delivery" (§4.2's
    prune reads it), and a fresh subscription has no deliveries yet. Leaving a stale value
    there would let the 90-day prune delete a device that just re-registered.
    """
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
            last_seen_ok = NULL
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
async def unsubscribe(body: EndpointIn, user: ActiveUser, conn: DB) -> dict[str, object]:
    """Drop one device, scoped to the member who owns it.

    The `user_id = $1` in the WHERE clause is the load-bearing half: without it, an endpoint
    string is enough to un-subscribe somebody else's phone, and endpoints travel (a shared
    browser, a copied bug report). A row belonging to another member is not "forbidden" here
    but simply not found, which is the same answer as a row that never existed.
    """
    removed = await conn.fetchval(
        "DELETE FROM push_subscription WHERE user_id = $1 AND endpoint = $2 RETURNING id",
        user.id,
        body.endpoint,
    )
    if removed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such device on this account")
    log.info("push subscription removed for user %s (device %s)", user.id, device_handle(body.endpoint))
    return {"ok": True, "subscriptions": await _subscriptions(conn, user.id)}
