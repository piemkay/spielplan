"""The transport is a fake push service: encryption, signing and headers run for real, and
bodies are decrypted with the subscription's own private key, as a browser would."""

from __future__ import annotations

import ast
import json
import logging
import os
import time
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from spielplan.core import logs, secrets
from spielplan.push import keys, send
from spielplan.push.send import device_handle

PAYLOAD = {"kind": "session-invite", "room": "GOLD-42"}


@dataclass
class Device:
    id: int
    endpoint: str
    p256dh: str
    auth: bytes
    private: ec.EllipticCurvePrivateKey


class FakePushService(httpx.AsyncBaseTransport):
    """`answers` and `fails` are keyed by endpoint, so a prune can be shown to take exactly one row."""

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


def _compact(payload: dict) -> bytes:
    """The sender's own compact framing, so size assertions compare against the real plaintext."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _payload_of(size: int) -> dict:
    envelope = len(_compact({"pad": ""}))
    return {"pad": "x" * (size - envelope)}


async def test_first_boot_generates_one_keypair_and_a_restart_reuses_it(db, secrets_key):
    """A second pair would silently invalidate every subscription made against the first."""
    first = await keys.ensure_keypair(db)
    second = await keys.ensure_keypair(db)

    assert first == second
    assert await db.fetchval("SELECT count(*) FROM app_setting WHERE key = 'push.vapid'") == 1
    # The uncompressed point browsers want for `applicationServerKey` (RFC 8292 §3.2).
    point = urlsafe_b64decode(first + "=" * (-len(first) % 4))
    assert len(point) == 65 and point[0] == 0x04


async def test_the_private_half_is_sealed_under_the_dek_and_carries_its_key_id(db, secrets_key):
    public = await keys.ensure_keypair(db)
    row = await db.fetchrow(
        "SELECT value, secret, secret_key_id FROM app_setting WHERE key = 'push.vapid'"
    )

    assert row["value"] == {"public_key": public}, "only the public half is stored in the clear"
    assert row["secret_key_id"] == await db.fetchval("SELECT key_id FROM data_encryption_key")

    _key_id, dek = await secrets.ensure_dek(db)
    # The sealed payload is bound to its row by AAD; opening it names that row.
    sealed = secrets.open_sealed(
        dek, row["secret"], secrets.aad_for("app_setting", keys.SETTING_KEY)
    )
    # The base64url TEXT, not the raw scalar: the raw bytes never appear in the JSON, sealed or not.
    assert sealed["private_key"].encode() not in bytes(row["secret"]), "not stored in clear"
    assert keys.unb64(sealed["private_key"]) not in bytes(row["secret"])


async def test_the_loaded_pair_signs_for_the_public_half_it_hands_the_browser(db, secrets_key):
    """A mismatched pair is a 401 on every delivery."""
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
    """§3.1 makes a half-configured boot legal and §2 forbids falling back to SESSION_SECRET."""
    assert await keys.ensure_keypair(db) is None
    assert await db.fetchval("SELECT count(*) FROM app_setting") == 0
    assert await keys.public_key(db) is None
    assert await keys.load(db) is None


async def test_no_route_returns_the_private_half(secrets_key, app, db):
    """Held as a key object with no repr and no accessor, so no route can serialise it by accident."""
    client = app()
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-pw"})

    row = await db.fetchrow("SELECT value, secret FROM app_setting WHERE key = 'push.vapid'")
    state = await client.get("/api/push/state")
    assert state.json()["vapid_public_key"] == row["value"]["public_key"]

    _key_id, dek = await secrets.ensure_dek(db)
    private = secrets.open_sealed(
        dek, row["secret"], secrets.aad_for("app_setting", keys.SETTING_KEY)
    )["private_key"]
    assert private not in state.text
    assert bytes(row["secret"]).hex() not in state.text
    # A repr lands in tracebacks and log lines, which is the other way a secret escapes.
    assert private not in repr(await keys.load(db))


async def test_the_authorization_header_is_the_vapid_scheme_with_a_token_and_the_key(
    household, db
):
    """RFC 8292 §3: `Authorization: vapid t=<JWT>, k=<public key>`."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    header = service.request_to(household.phone).headers["authorization"]
    assert header.startswith("vapid t=")
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert token.count(".") == 2
    assert key == await keys.public_key(db)


async def test_the_jwt_names_the_push_services_origin_an_expiry_and_a_contact(household, db):
    """RFC 8292 §2. The audience is the origin, not the endpoint: the endpoint is a bearer capability."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    header, claims, _signature, _signed = _jwt_parts(
        service.request_to(household.phone).headers["authorization"]
    )
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert claims["aud"] == "https://push.example.test"
    assert household.phone.endpoint not in json.dumps(claims)
    # RFC 8292 caps it at 24 h; the lower bound stops a token that expires before it is read.
    assert 3600 <= claims["exp"] - time.time() <= 24 * 3600
    assert claims["sub"].startswith(("http", "mailto:"))


async def test_the_jwt_signature_is_raw_r_s_and_not_der(household, db):
    """RFC 7515 A.3: raw r||s. The DER `cryptography` produces is valid, and push services reject it."""
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


async def test_the_body_is_one_aes128gcm_record_the_subscribed_browser_can_read(household, db):
    """RFC 8291/8188: salt, record size, key id length, the ephemeral public key, then the record."""
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    body = service.request_to(household.phone).content
    assert service.request_to(household.phone).headers["content-encoding"] == "aes128gcm"
    assert len(body[:16]) == 16, "a 16-byte salt"
    assert int.from_bytes(body[16:20], "big") == send._RECORD_SIZE
    assert body[20] == 65, "one uncompressed P-256 point as the key id"
    # RFC 8188 §2: the overhead is one padding delimiter plus the GCM tag, exactly; `send.py`'s size
    # check depends on that number.
    record = body[21 + body[20] :]
    assert len(record) == len(_compact(PAYLOAD)) + send._PAD_AND_TAG
    assert _decrypt(body, household.phone) == PAYLOAD


async def test_a_body_encrypted_for_one_device_does_not_decrypt_with_anothers_keys(
    household, db
):
    service = FakePushService()
    await send.send_to_user(db, household.jenny, PAYLOAD, transport=service)

    for_phone = service.request_to(household.phone).content
    assert _decrypt(for_phone, household.phone) == PAYLOAD
    with pytest.raises((InvalidTag, ValueError)):
        _decrypt(for_phone, household.laptop)


async def test_a_delivered_push_stamps_last_seen_ok(household, db):
    """`last_seen_ok` is what the 90-day sweep reads; a delivery that did not stamp it loses the phone."""
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
    """Only 404 and 410 prune (§4.2); anything else is transient."""
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
    lonely = await _member(db, "a-member-who-declined")
    assert await send.send_to_user(db, lonely, PAYLOAD, transport=FakePushService()) == []


async def test_a_failing_send_never_raises_into_the_caller(household, db):
    """A lobby that raised on a delivery failure would break on the iPhone push may never reach."""
    service = FakePushService()
    service.fails[household.phone.endpoint] = httpx.ConnectError("no route to host")
    service.answers[household.laptop.endpoint] = 400
    # Unusable keys (M2 rows predate this sender): encryption fails before the request is built.
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
    await db.execute("DELETE FROM app_setting WHERE key = 'push.vapid'")
    service = FakePushService()

    assert await send.send_to_user(db, household.jenny, PAYLOAD, transport=service) == []
    assert service.requests == []


async def test_the_endpoint_and_the_auth_key_never_reach_a_log_line_or_a_result(
    household, db, caplog
):
    """httpx puts the URL in its exception messages, so failures log the exception's type only; its
    own request lines are silenced by the logging both processes configure."""
    logs.configure()
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
    """Accepts everything: for tests about what goes on the wire."""
    return FakePushService()


async def test_every_message_gets_a_fresh_salt_and_a_fresh_ephemeral_key(household, db):
    """Reusing the salt or ephemeral key reuses an AES-GCM (key, nonce): a two-time pad. Hoisting
    `ec.generate_private_key` out of the loop breaks only this test."""
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
    """RFC 8188 §2: a body past `rs` fails the browser's tag while the push service answers 201."""
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


async def test_the_largest_payload_that_fits_one_record_is_sent_and_one_byte_more_is_not(
    household, db
):
    """At exactly `_RECORD_SIZE - _PAD_AND_TAG` the record fills `rs` to the byte and still decrypts."""
    fits = _payload_of(send._RECORD_SIZE - send._PAD_AND_TAG)
    assert len(_compact(fits)) == send._RECORD_SIZE - send._PAD_AND_TAG

    service = _service()
    results = await send.send_to_user(db, household.jenny, fits, transport=service)
    assert [r.ok for r in results] == [True, True], "the largest legal payload is a delivery"
    body = service.request_to(household.phone).content
    assert len(body[21 + body[20] :]) == send._RECORD_SIZE, "the record fills `rs` exactly"
    assert _decrypt(body, household.phone) == fits, "and a browser can still read it"

    # Refused in `_encrypt`, before the wire; `_deliver` logs it without pruning.
    with pytest.raises(ValueError, match="must fit one"):
        send._encrypt(
            _compact(_payload_of(send._RECORD_SIZE - send._PAD_AND_TAG + 1)),
            household.phone.p256dh,
            keys.b64(household.phone.auth),
        )


async def test_a_compressed_subscription_key_is_refused_rather_than_sent(household, db):
    """RFC 8291 §3.4 mixes the uncompressed point into `key_info`, so a compressed one derives the
    wrong IKM. The subscribe route accepts any string."""
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
    """RFC 8292 §2.1 allows `mailto:` or https; APNs answers 403 to an http `sub`, and `PUBLIC_URL`
    is http on a LAN install."""
    assert send.vapid_subject("https://spielplan.example.tld") == "https://spielplan.example.tld"
    assert send.vapid_subject("https://spielplan.example.tld/") == "https://spielplan.example.tld"
    assert send.vapid_subject("http://localhost:8080") == "mailto:admin@localhost"
    assert send.vapid_subject("http://192.168.1.9:8080") == "mailto:admin@192.168.1.9"
    for public_url in ("https://x.test", "http://x.test", "", "not-a-url"):
        assert send.vapid_subject(public_url).startswith(("https://", "mailto:"))


def test_naming_a_device_is_a_domain_rule_and_this_module_imports_no_api_layer():
    """`worker.py` imports this via `sync.playback`, so an `api/` import loads FastAPI in the worker.
    Parsed, not `sys.modules`: the app fixture has imported `spielplan.api` anyway."""
    assert send.device_handle is device_handle
    assert device_handle.__module__ == "spielplan.push.send"
    assert device_handle("https://push.example.test/f/x") != "https://push.example.test/f/x"

    tree = ast.parse(Path(send.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.startswith("spielplan.api")}, sorted(imported)
