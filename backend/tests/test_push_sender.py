"""Sending a web push, against a real database. Spec v2.1 §2, §4.2, §6 preamble, §7.3, §12 (M4).

M2 stored subscriptions; §12's M4 row ("push join") and §7.3 ("push arrives with the M4
stack") owe the delivery. Four things go wrong here quietly rather than loudly, and each one
has its register below:

  * **A regenerated keypair breaks every subscription in silence.** The browser bound its
    subscription to the key it saw; a second pair means the push service keeps answering 201
    and the phone never rings again. §2 puts the pair in the database, sealed like every other
    secret, and first boot is the only place it may be minted.
  * **A DER signature is a valid signature that no push service accepts.** `cryptography`
    produces DER; JWS wants raw r||s (RFC 7515 A.3). The failure is a 401 from the push
    service, in production, on somebody's Friday night.
  * **Pruning on the wrong status code deletes a live phone.** §4.2 names 404 and 410 and no
    others: everything else is a push service having a bad minute.
  * **The endpoint is a bearer capability.** Anyone holding it can push to that device, and
    `auth` decrypts the messages. Neither may reach a log line or a returned value.

The transport is a fake push service, injected the way `connectors/jellyfin.py` injects one:
the real request building runs — encryption, signing, headers — and only the socket is absent.
The bodies are decrypted here with the subscription's own private key, so what is asserted is
that a *browser* could read them, not that we can read our own output.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import json
import logging
import os
import time
from base64 import urlsafe_b64decode
from dataclasses import dataclass

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from spielplan.api.push import device_handle
from spielplan.core import secrets
from spielplan.push import keys, send

PAYLOAD = {"kind": "session-invite", "room": "GOLD-42"}


# --- the fake push service --------------------------------------------------------------------


@dataclass
class Device:
    """One browser's subscription, with the private half a real phone would keep."""

    id: int
    endpoint: str
    p256dh: str
    auth: bytes
    private: ec.EllipticCurvePrivateKey


class FakePushService(httpx.AsyncBaseTransport):
    """A push service is an HTTPS POST that answers a status code; that is all this is.

    `answers` and `fails` are keyed by endpoint so one call can be told to succeed for one
    device and fail for another — which is the only way to assert that a prune took exactly
    one row.
    """

    def __init__(self) -> None:
        self.answers: dict[str, int] = {}
        self.fails: dict[str, Exception] = {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.fails:
            raise self.fails[url]
        return httpx.Response(self.answers.get(url, 201))

    def request_to(self, device: Device) -> httpx.Request:
        for request in self.requests:
            if str(request.url) == device.endpoint:
                return request
        raise AssertionError("no request was made to that device")


async def _member(db, name: str) -> int:
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, 'member') RETURNING id", name
    )


async def _device(db, user_id: int, endpoint: str) -> Device:
    """Register a device the way `POST /api/push/subscribe` would, with real browser keys."""
    private = ec.generate_private_key(ec.SECP256R1())
    p256dh = keys.b64(
        private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    auth = os.urandom(16)
    row_id = await db.fetchval(
        """
        INSERT INTO push_subscription (user_id, device_label, endpoint, p256dh, auth)
        VALUES ($1, 'phone', $2, $3, $4) RETURNING id
        """,
        user_id,
        endpoint,
        p256dh,
        keys.b64(auth),
    )
    return Device(id=row_id, endpoint=endpoint, p256dh=p256dh, auth=auth, private=private)


@dataclass
class Household:
    jenny: int
    patrick: int
    phone: Device        # jenny's
    laptop: Device       # jenny's second device
    patricks_phone: Device


@pytest.fixture
async def household(db, secrets_key) -> Household:
    """§4.2's two members, one of them with two devices, and the household's VAPID pair."""
    await keys.ensure_keypair(db)
    jenny = await _member(db, "jenny")
    patrick = await _member(db, "patrick")
    return Household(
        jenny=jenny,
        patrick=patrick,
        phone=await _device(db, jenny, "https://push.example.test/f/jenny-phone"),
        laptop=await _device(db, jenny, "https://push.example.test/f/jenny-laptop"),
        patricks_phone=await _device(db, patrick, "https://push.example.test/f/patrick-phone"),
    )


def _decrypt(body: bytes, device: Device) -> dict:
    """The receiving half of RFC 8291 §3.4, as a browser's service worker performs it."""
    salt, id_len = body[:16], body[20]
    as_public = body[21 : 21 + id_len]
    record = body[21 + id_len :]

    ua_public = urlsafe_b64decode(device.p256dh + "=" * (-len(device.p256dh) % 4))
    shared = device.private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    )
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=device.auth,
        info=b"WebPush: info\x00" + ua_public + as_public,
    ).derive(shared)
    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    return json.loads(AESGCM(cek).decrypt(nonce, record, None).removesuffix(b"\x02"))


def _jwt_parts(authorization: str) -> tuple[dict, dict, bytes, bytes]:
    """(header, claims, signature, signing input) out of `vapid t=<jwt>, k=<key>`."""
    token = authorization.removeprefix("vapid t=").split(",")[0]
    header, claims, signature = token.split(".")
    return (
        json.loads(urlsafe_b64decode(header + "=" * (-len(header) % 4))),
        json.loads(urlsafe_b64decode(claims + "=" * (-len(claims) % 4))),
        urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{claims}".encode("ascii"),
    )


async def _count(db) -> int:
    return await db.fetchval("SELECT count(*) FROM push_subscription")


# --- §2: the keypair is minted once, at first boot, and sealed like every other secret ---------


async def test_first_boot_generates_one_keypair_and_a_restart_reuses_it(db, secrets_key):
    """§2: "A web-push VAPID keypair is generated at first boot and stored the same way."

    A second pair would invalidate every subscription registered against the first while every
    layer keeps reporting success — the household simply stops being notified.
    """
    first = await keys.ensure_keypair(db)
    second = await keys.ensure_keypair(db)

    assert first == second
    assert await db.fetchval("SELECT count(*) FROM app_setting WHERE key = 'push.vapid'") == 1
    # The uncompressed point browsers want for `applicationServerKey` (RFC 8292 §3.2).
    point = urlsafe_b64decode(first + "=" * (-len(first) % 4))
    assert len(point) == 65 and point[0] == 0x04


async def test_the_private_half_is_sealed_under_the_dek_and_carries_its_key_id(db, secrets_key):
    """§2 puts this pair on the same footing as a connector secret: AEAD under the DEK, with a
    `key_id` beside the ciphertext so rotation stays possible. §14.3 is why that matters — the
    private half is what lets anyone push to this household's phones."""
    public = await keys.ensure_keypair(db)
    row = await db.fetchrow(
        "SELECT value, secret, secret_key_id FROM app_setting WHERE key = 'push.vapid'"
    )

    assert row["value"] == {"public_key": public}, "only the public half is stored in the clear"
    assert row["secret_key_id"] == await db.fetchval("SELECT key_id FROM data_encryption_key")

    _key_id, dek = await secrets.ensure_dek(db)
    sealed = secrets.open_sealed(dek, row["secret"])
    # The BASE64URL TEXT, not the raw scalar. `seal` stores `{"private_key": "<b64url>"}`, and
    # those 32 raw bytes never appear in that JSON whether or not it is encrypted — so the raw
    # form passes against a no-op cipher and asserts nothing. Verified: with `seal` replaced by
    # a pass-through the raw check stayed green and this one goes red.
    assert sealed["private_key"].encode() not in bytes(row["secret"]), "not stored in clear"
    assert keys.unb64(sealed["private_key"]) not in bytes(row["secret"])


async def test_the_loaded_pair_signs_for_the_public_half_it_hands_the_browser(db, secrets_key):
    """The two halves have to be one pair. A browser subscribes against the public half and the
    push service verifies the signature against it; a mismatch is a 401 on every delivery."""
    public = await keys.ensure_keypair(db)
    vapid = await keys.load(db)

    assert vapid.public_key == public
    point = urlsafe_b64decode(public + "=" * (-len(public) % 4))
    verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    signature = vapid.sign(b"a signing input")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    verifier.verify(encode_dss_signature(r, s), b"a signing input", ec.ECDSA(hashes.SHA256()))


async def test_a_boot_without_a_secrets_key_stores_no_keypair_and_does_not_raise(
    db, no_secrets_key
):
    """§3.1 makes a half-configured boot a legal state — the app must still serve the wizard
    that configures it — and §2 forbids falling back to SESSION_SECRET. So there is nowhere to
    put a private half, and the honest answer is None rather than a crash at startup."""
    assert await keys.ensure_keypair(db) is None
    assert await db.fetchval("SELECT count(*) FROM app_setting") == 0
    assert await keys.public_key(db) is None
    assert await keys.load(db) is None


async def test_no_route_returns_the_private_half(secrets_key, app, db):
    """§4.2's keys are secrets and this one is the household's. The onboarding screen needs the
    public half and nothing else; a private half that can be serialised is one an `api/` route
    can return by accident, so it is held as a key object with no repr and no accessor."""
    client = app()
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-pw"})

    row = await db.fetchrow("SELECT value, secret FROM app_setting WHERE key = 'push.vapid'")
    state = await client.get("/api/push/state")
    assert state.json()["vapid_public_key"] == row["value"]["public_key"]

    _key_id, dek = await secrets.ensure_dek(db)
    private = secrets.open_sealed(dek, row["secret"])["private_key"]
    assert private not in state.text
    assert bytes(row["secret"]).hex() not in state.text
    # A repr lands in tracebacks and log lines, which is the other way a secret escapes.
    assert private not in repr(await keys.load(db))


# --- RFC 8292: the VAPID signature -------------------------------------------------------------


async def test_the_authorization_header_is_the_vapid_scheme_with_a_token_and_the_key(
    household, db
):
    """RFC 8292 §3: `Authorization: vapid t=<JWT>, k=<public key>`. The `k` is how the push
    service ties the request to the key the subscription was created against."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    header = service.request_to(household.phone).headers["authorization"]
    assert header.startswith("vapid t=")
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert token.count(".") == 2
    assert key == await keys.public_key(db)


async def test_the_jwt_names_the_push_services_origin_an_expiry_and_a_contact(household, db):
    """RFC 8292 §2: `aud` is the origin of the push service, `exp` is at most 24 h away, `sub`
    is how its operator reaches whoever runs this application server. The audience is the
    origin and not the endpoint — the endpoint is a bearer capability, and a token naming it
    hands that capability to every log the service keeps."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    header, claims, _signature, _signed = _jwt_parts(
        service.request_to(household.phone).headers["authorization"]
    )
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert claims["aud"] == "https://push.example.test"
    assert household.phone.endpoint not in json.dumps(claims)
    # RFC 8292 caps the lifetime at 24 h; the lower bound matters as much, because a one-second
    # token would satisfy a bare `<= 24h` and be expired before the push service read it.
    assert 3600 <= claims["exp"] - time.time() <= 24 * 3600
    assert claims["sub"].startswith(("http", "mailto:"))


async def test_the_jwt_signature_is_raw_r_s_and_not_der(household, db):
    """RFC 7515 A.3: ES256 signatures are the two 32-byte integers, concatenated. The DER
    envelope `cryptography` produces instead is a perfectly valid signature that every push
    service rejects — and the rejection arrives in production, not here."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    _header, _claims, signature, signed = _jwt_parts(
        service.request_to(household.phone).headers["authorization"]
    )
    assert len(signature) == 64, "a DER signature is 70-72 bytes and self-describing"

    public = await keys.public_key(db)
    point = urlsafe_b64decode(public + "=" * (-len(public) % 4))
    verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    verifier.verify(
        encode_dss_signature(
            int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        ),
        signed,
        ec.ECDSA(hashes.SHA256()),
    )


# --- RFC 8291: the payload is encrypted for the device, not for the push service ---------------


async def test_the_body_is_one_aes128gcm_record_the_subscribed_browser_can_read(household, db):
    """RFC 8291/8188: salt, record size, key id length, the ephemeral public key, then the
    record. The push service forwards this without being able to read it, which is the point:
    §4.2's `p256dh` and `auth` are what make the household's prompts private from it."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    body = service.request_to(household.phone).content
    assert service.request_to(household.phone).headers["content-encoding"] == "aes128gcm"
    assert len(body[:16]) == 16, "a 16-byte salt"
    # The declared `rs`, not a comparison that is trivially true: 4096 >= 120 tells you nothing.
    # A decoder reads records of exactly this size, so the field has to be the size the sender
    # padded to and the record has to fit inside it.
    assert int.from_bytes(body[16:20], "big") == send._RECORD_SIZE
    assert len(body) - 21 - len(body[21:86]) <= send._RECORD_SIZE
    assert body[20] == 65, "one uncompressed P-256 point as the key id"
    assert _decrypt(body, household.phone) == PAYLOAD


async def test_a_body_encrypted_for_one_device_does_not_decrypt_with_anothers_keys(
    household, db
):
    """Each device's copy is encrypted for that device alone. If one body opened with another
    subscription's keys, a phone that once shared a household could read prompts meant for a
    member it no longer is."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    for_phone = service.request_to(household.phone).content
    assert _decrypt(for_phone, household.phone) == PAYLOAD
    with pytest.raises((InvalidTag, ValueError)):
        _decrypt(for_phone, household.laptop)


# --- §4.2: "pruned on 404/410 from the push service", and on nothing else ----------------------


async def test_a_delivered_push_stamps_last_seen_ok(household, db):
    """§4.2's 90-day sweep in `worker.py` reads `last_seen_ok`; nothing else writes it. A
    delivered push that did not stamp it lets the sweep delete a phone that works."""
    service = FakePushService()
    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    assert [r.status for r in results] == [201, 201]
    assert all(r.ok and not r.pruned for r in results)
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE last_seen_ok IS NOT NULL"
    ) == 2
    assert await db.fetchval(
        "SELECT last_seen_ok FROM push_subscription WHERE id = $1", household.patricks_phone.id
    ) is None


async def test_a_404_prunes_exactly_that_device(household, db):
    """§4.2 names 404 because it is the push service saying the endpoint is not one of its
    endpoints any more. The other device of the same member is a different row, on a different
    endpoint, and must survive."""
    service = FakePushService()
    service.answers[household.phone.endpoint] = 404
    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    assert [(r.status, r.pruned) for r in results] == [(404, True), (201, False)]
    assert await _count(db) == 2
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE id = $1", household.phone.id
    ) == 0
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE id = $1", household.laptop.id
    ) == 1


async def test_a_410_prunes_exactly_that_device(household, db):
    """410 Gone is the code a push service uses when the subscription was revoked — the member
    uninstalled the PWA or turned notifications off. Keeping the row means retrying that phone
    forever."""
    service = FakePushService()
    service.answers[household.laptop.endpoint] = 410
    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    assert [(r.status, r.pruned) for r in results] == [(201, False), (410, True)]
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE id = $1", household.laptop.id
    ) == 0
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE id = $1", household.phone.id
    ) == 1


async def test_a_500_a_429_and_a_timeout_each_leave_the_subscription_in_place(household, db):
    """Everything that is not 404 or 410 is the push service having a bad minute. Pruning on a
    500, a rate limit or a timeout deletes a live phone over a transient outage — and the
    member finds out weeks later, by not being invited to anything."""
    service = FakePushService()
    service.answers[household.phone.endpoint] = 500
    service.answers[household.laptop.endpoint] = 429
    assert not any(
        r.pruned for r in await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)
    )

    timing_out = FakePushService()
    timing_out.fails[household.phone.endpoint] = httpx.ReadTimeout("no answer")
    timing_out.fails[household.laptop.endpoint] = httpx.ConnectError("refused")
    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=timing_out)

    assert [(r.status, r.pruned) for r in results] == [(None, False), (None, False)]
    assert await _count(db) == 3
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE last_seen_ok IS NOT NULL"
    ) == 0


async def test_only_that_members_devices_are_sent_to_or_pruned(household, db):
    """§4.2 keys the table on `user_id` and §7.3's prompt is per-user: the household has no
    shared notification. A send that reached the other member's phone would deliver Jenny's
    "did you finish X?" to Patrick — the one mistake tapping "no" cannot undo."""
    service = FakePushService()
    service.answers[household.patricks_phone.endpoint] = 410
    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    sent_to = {str(request.url) for request in service.requests}
    assert sent_to == {household.phone.endpoint, household.laptop.endpoint}
    assert len(results) == 2
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE user_id = $1", household.patrick
    ) == 1


async def test_a_member_with_no_device_is_a_silent_no_op(household, db):
    """§6's preamble: push is best-effort and every prompt has an in-app equivalent. A member
    who declined notifications is a normal household, not an error state."""
    lonely = await _member(db, "a-member-who-declined")
    assert await send.send_to_user(db, lonely, PAYLOAD, transport=FakePushService()) == []


# --- §6 preamble: best-effort means no send outcome reaches the caller -------------------------


async def test_a_failing_send_never_raises_into_the_caller(household, db):
    """§6's preamble makes push best-effort *because* of the iPhone that may never receive one:
    "every push-carried prompt also exists as an in-app banner". A lobby (§6.2) that raised on
    a delivery failure would break on exactly the device the constraint was written about."""
    service = FakePushService()
    service.fails[household.phone.endpoint] = httpx.ConnectError("no route to host")
    service.answers[household.laptop.endpoint] = 400
    # A subscription whose keys are unusable — M2 rows predate this sender, and a browser may
    # send anything. Encryption fails before the request is even built.
    await db.execute(
        "UPDATE push_subscription SET p256dh = 'not-a-key' WHERE id = $1", household.laptop.id
    )

    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)
    assert [(r.status, r.ok, r.pruned) for r in results] == [
        (None, False, False),
        (None, False, False),
    ]
    assert await _count(db) == 3


async def test_a_household_with_no_keypair_sends_nothing_and_raises_nothing(household, db):
    """§3.1's half-configured install, mid-session. Nothing to sign with is a logged fact and
    a fallback to §6's banner, not an exception in the middle of opening a session."""
    await db.execute("DELETE FROM app_setting WHERE key = 'push.vapid'")
    service = FakePushService()

    assert await send.send_to_user(db, household.jenny, PAYLOAD, transport=service) == []
    assert service.requests == []


# --- the endpoint and the auth key are secrets -------------------------------------------------


async def test_the_endpoint_and_the_auth_key_never_reach_a_log_line_or_a_result(
    household, db, caplog
):
    """The endpoint URL is a bearer capability — anyone holding it can push to that device —
    and `auth` is the message-encryption key. Every outcome here logs, so every outcome is a
    chance to leak one: the device handle from `api/push.py` is what an operator correlates on
    instead. httpx puts the URL in several of its exception messages, which is why the failure
    path logs the exception's type and never the exception."""
    service = FakePushService()
    service.answers[household.phone.endpoint] = 410
    service.answers[household.laptop.endpoint] = 500
    service.fails[household.patricks_phone.endpoint] = httpx.ReadTimeout(
        f"timed out for {household.patricks_phone.endpoint}"
    )

    with caplog.at_level(logging.DEBUG):
        results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)
        results += await send.send_to_user(db, household.patrick, PAYLOAD, transport=service)

    for device in (household.phone, household.laptop, household.patricks_phone):
        assert device.endpoint not in caplog.text
        assert device.endpoint not in repr(results)
        assert keys.b64(device.auth) not in caplog.text
        assert keys.b64(device.auth) not in repr(results)
    assert device_handle(household.phone.endpoint) in caplog.text


def _service() -> FakePushService:
    """A service that accepts everything — the default for tests about what the sender puts on
    the wire rather than about what a failure does."""
    return FakePushService()



# --- the properties the RFC makes catastrophic to lose ---------------------------------------


async def test_every_message_gets_a_fresh_salt_and_a_fresh_ephemeral_key(household, db):
    """RFC 8291 derives the content-encryption key and the nonce from the record salt and the
    ephemeral ECDH secret, so reusing either across two messages to one device reuses an
    AES-GCM (key, nonce) pair. That is a two-time pad: XOR the two bodies and both plaintexts
    fall out, and a valid tag can be forged for anything the service worker will accept —
    by the push service itself, or by anyone holding the endpoint.

    The code is correct; the *guard* is what this adds. A reviewer hoisting
    `ec.generate_private_key` out of the per-device loop — a correct-looking optimisation, it
    costs ~100 µs a call — breaks this and nothing else in the file notices, because the
    cross-device test survives on the differing ECDH secrets alone.
    """
    first = _service()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=first)
    second = _service()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=second)

    def parts(service, device):
        body = service.request_to(device).content
        return body[:16], body[21:86]          # the salt, and the ephemeral public point

    across_sends = (parts(first, household.phone), parts(second, household.phone))
    assert across_sends[0][0] != across_sends[1][0], "the salt repeated across two sends"
    assert across_sends[0][1] != across_sends[1][1], "the ephemeral key repeated across sends"

    within = (parts(first, household.phone), parts(first, household.laptop))
    assert within[0][0] != within[1][0], "two devices in one send shared a salt"
    assert within[0][1] != within[1][1], "two devices in one send shared an ephemeral key"


async def test_a_payload_too_large_for_one_record_is_refused_rather_than_sent(household, db):
    """A body past the record size the header declares violates RFC 8188 §2: a decoder splits
    it at `rs`, decrypts a fragment and fails the tag. A push service that does not enforce the
    limit answers 201 — so without this the send reports success, `last_seen_ok` is stamped,
    and the phone silently drops the message.

    `send_to_user` takes an arbitrary dict, and a §6.2 lobby invite that grows a title list or
    an overview string crosses 4 KB without anyone noticing.
    """
    service = _service()
    huge = {"kind": "session-invite", "filler": "x" * 5000}
    results = await send.send_to_user(db, household.jenny, huge, transport=service)

    assert results, "a refusal is still reported per device"
    assert all(not r.ok for r in results), "an unsendable body is not a delivery"
    assert service.requests == [], "nothing was put on the wire"
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE last_seen_ok IS NOT NULL"
    ) == 0, "an unsent message must not stamp a delivery"
    assert await db.fetchval("SELECT count(*) FROM push_subscription") == 3, (
        "and it is not a reason to prune a live device"
    )


async def test_a_compressed_subscription_key_is_refused_rather_than_sent(household, db):
    """RFC 8291 §3.4 assumes the uncompressed point on both sides: the raw point is mixed into
    `key_info`, so a 33-byte compressed one derives an IKM the browser does not, and the phone
    gets a body it cannot read while every layer reports success.

    No shipping browser sends one — but `POST /api/push/subscribe` accepts any 256-character
    string, so a hand-rolled subscription reaches here and earns a permanent 201-and-silence.
    """
    compressed = keys.b64(b"\x02" + b"\x11" * 32)
    await db.execute(
        "UPDATE push_subscription SET p256dh = $2 WHERE endpoint = $1",
        household.phone.endpoint, compressed,
    )
    service = _service()
    results = await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    bad = [r for r in results if not r.ok]
    assert bad, "a malformed subscription key is a refusal"
    assert household.phone.endpoint not in [str(r.url) for r in service.requests]
    assert await db.fetchval(
        "SELECT count(*) FROM push_subscription WHERE endpoint = $1", household.phone.endpoint
    ) == 1, "a malformed key is the client's bug, not a dead endpoint — the row stays"


def test_the_vapid_subject_is_one_the_push_service_will_accept():
    """RFC 8292 §2.1 sanctions a `mailto:` or an **https:** URI. §2 puts this app behind "one
    plain-HTTP port" with the operator's Traefik in front, so `PUBLIC_URL` is `http://…` on a
    LAN or Tailscale install — and APNs, the service §6's preamble makes push exist for,
    answers 403 to a `sub` it does not accept. That failure is invisible: an un-pruned row, one
    log line, and a household that never gets a notification.
    """
    assert send.vapid_subject("https://spielplan.example.tld") == "https://spielplan.example.tld"
    assert send.vapid_subject("https://spielplan.example.tld/") == "https://spielplan.example.tld"
    assert send.vapid_subject("http://localhost:8080") == "mailto:admin@localhost"
    assert send.vapid_subject("http://192.168.1.9:8080") == "mailto:admin@192.168.1.9"
    for public_url in ("https://x.test", "http://x.test", "", "not-a-url"):
        assert send.vapid_subject(public_url).startswith(("https://", "mailto:"))
