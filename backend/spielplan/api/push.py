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
  * **The two writes need the account credential, not the PIN** (decision 170). §3.2 makes the
    PIN a convenience for the household phone, so a PIN session reaches the product surfaces and
    nothing that changes how the account is reached later. Subscribing rebinds an endpoint to the
    caller and unsubscribing silences a device: whoever holds a switched-into phone could point
    another member's notifications at it, which is the same class as minting a passkey. Reading
    this member's own devices stays `ActiveUser` — it discloses nothing the phone does not hold.

And one property that is not about the table at all: **a stored endpoint is a URL this server
will POST to.** `push/send.py` sends to it verbatim, so what `POST /api/push/subscribe` accepts
is what the backend can be made to reach (§14.3, sec-13). The scheme and address rules are on
`SubscriptionIn`; the one that needs a connection — the household's own servers — is in the
route.

The *sending* half is M4's `spielplan.push` (§12); this is the subscribe/unsubscribe path plus
the read the onboarding screen needs — and `device_handle` now comes from `push/send.py`, where
a rule about naming a device belongs (CLAUDE.md, arch-02). `router` is exported for
`spielplan.app` to register.
"""

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
    # Shaped so the client can post `subscription.toJSON()` plus a label, unchanged. Extra
    # fields (`expirationTime`) are ignored rather than rejected: browsers add to that object.
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: SubscriptionKeys
    device_label: str | None = Field(default=None, max_length=64)

    @field_validator("endpoint")
    @classmethod
    def _endpoint_is_a_push_service_on_the_internet(cls, value: str) -> str:
        """§14.3: a stored endpoint is a URL this server later POSTs to, signed (sec-13).

        `push/send.py` posts to the stored string verbatim with the VAPID JWT attached, so until
        this check existed `POST /api/push/subscribe` was a way for any member's session to make
        the backend knock on an address of the caller's choosing inside the household's network —
        a reachability probe, since the method is fixed and the body is ciphertext, but §14.3
        does not grade the ones that only probe. A length bound was the whole contract.

        The two hosts in play are easy to confuse and only one of them is constrained here. §2
        puts the *app* behind "one plain-HTTP port", which is why `send.py::vapid_subject` exists
        at all: `PUBLIC_URL` is `http://...` on a LAN or Tailscale install and RFC 8292 will not
        take that as a `sub`. This rule is about the *push service's* endpoint instead — minted
        by Apple, Google or Mozilla, always https, never on the household's own network — so
        requiring https here says nothing about how the household reaches the app.

        A literal address is judged by `ipaddress`; a name is not resolved. Resolution happens at
        send time in a different process and a different second, so a validator that pre-empted
        it would be both wrong and a second lookup. No allowlist of push-service domains: the
        set of services is the set of browsers, it changes without asking this household, and an
        allowlist that lags one release silently stops notifications (the failure §4.2's pruning
        rules are written to avoid).

        The literal is judged by REACHABILITY (`is_global`) and not by an enumeration of ranges.
        An enumeration is a list that has to be maintained against `ipaddress` itself: written as
        `is_loopback or is_private or is_link_local or is_reserved or is_unspecified` it let
        100.64.0.0/10 through, because CVE-2024-4032 took RFC 6598 shared address space out of
        `is_private` in 3.12.4 — and that block is exactly what Tailscale assigns every node on a
        tailnet, which is the deployment the paragraph above names. `is_global` is one predicate
        that covers all five and that block, and it says the thing the rule means: a push service
        is on the internet. [M4.11 review cycle 2: m411-c2-push-02; §14.3]
        """
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
    # Deliberately not validated the way `SubscriptionIn.endpoint` is: this is the body of a
    # DELETE, and a row stored before that rule existed must stay deletable by the string it
    # was stored under. Refusing to *forget* an endpoint protects nothing.
    endpoint: str = Field(min_length=1, max_length=2048)


async def _refuse_a_household_host(conn, endpoint: str) -> None:
    """The half of sec-13 that needs a database read, which is why it is not in the validator.

    Two hosts this household runs are perfectly public names and would therefore survive
    `SubscriptionIn`'s check: the Jellyfin server (§3.3's connector, reachable from this process
    with an admin key) and the app itself (`PUBLIC_URL`). Neither is ever a push service, so an
    endpoint naming one is a probe by construction rather than a configuration mistake.

    The URL is read straight out of `connector_config` rather than through
    `registry.load_jellyfin`, because that opens the connector's secrets: §3.1 makes a boot with
    an unreadable DEK legal and subscribing a phone must not be the route that fails on it.
    """
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
async def subscribe(body: SubscriptionIn, user: CredentialedUser, conn: DB) -> dict[str, object]:
    """Store a browser PushSubscription for the signed-in member.

    ON CONFLICT on `endpoint` rather than an insert: a phone resubscribes with the same
    endpoint every time its service worker is replaced, and §4.2's UNIQUE says so — one
    endpoint is one device, and one device is one row.

    The conflicting row's `user_id` is overwritten on purpose. An endpoint is minted per
    browser profile, so the only way an existing endpoint arrives under a different member is
    that the same browser is now signed in as that member — and the notifications must follow
    the person at the phone, not the person who first granted permission on it.

    `last_seen_ok` is left ALONE on the conflict path, and that is what keeps §4.2's 90-day prune
    off a live phone. The column means "the push service accepted a delivery", so a genuinely new
    endpoint inserts with it NULL and is judged by `created_at = now()` — which is the row the
    prune was written for. Resetting it on a re-post looked like the conservative reading and was
    the opposite once `worker.py` started pruning on `COALESCE(last_seen_ok, created_at)`:
    `push.js::syncSubscription` re-posts the unchanged
    endpoint on every /account open, and on a household older than ninety days that made opening
    the account page arm the deletion of a live device on the same night's prune — measured end
    to end. Keeping the real delivery time is what makes the COALESCE harmless.
    [review cycle 1: m411-rev1-push-prune-deletes-a-live-device-that-just-re-registered; §4.2]

    `created_at` IS rewritten on the conflict path, and that is the other half of the same
    composition. The prune's own docstring says the row it takes is one "whose device never came
    back at all - no delivery, no rejection, ninety days of silence", and a re-post is the device
    coming back: `pushManager.getSubscription()` hands the browser's endpoint back only while that
    subscription is live, so an endpoint that CONFLICTS is a live device by construction — a lapsed
    one mints a new endpoint and INSERTs. With neither column moving, a household that simply sends
    no pushes for ninety days — no Jellyfin link arms no §7.3 prompt, and §6.2's invitation never
    goes to the host — lost every subscription on schedule however often the member re-registered,
    and `send_to_user` then returned `[]` in silence. The cost is that §6.6's device list, ordered
    by `created_at`, now reads in order of last registration rather than first, which is the more
    useful order for the thing that list is for. [M4.11 review cycle 2: m411-c2-push-01; §4.2]

    `SubscriptionIn` has already refused anything that is not an https host off this network;
    what is left is the half of that rule which needs a connection (sec-13).
    """
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
