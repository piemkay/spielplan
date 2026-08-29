"""A software WebAuthn authenticator, for testing the real ceremonies.

Spec v2.1 §3.2 makes passkeys primary and §14.4 promises that changing `PUBLIC_URL`
invalidates them. Neither claim can be checked by mocking `verify_authentication_response` —
that would assert we call the library we call. This produces genuine CTAP2 structures signed
by a real P-256 key, so `core.webauthn` runs its actual verification path and the interesting
cases are reachable: an assertion for the wrong origin, an assertion for the wrong rp_id, and
a replay whose signature is perfectly valid and whose counter has not moved.

Only the pieces the app uses: ES256, attestation format "none", no extensions.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

AAGUID = b"\x00" * 16

FLAG_UP = 0x01          # user present
FLAG_UV = 0x04          # user verified
FLAG_AT = 0x40          # attested credential data included


def b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cose_key(public: ec.EllipticCurvePublicKey) -> bytes:
    """COSE_Key for ES256, the encoding an authenticator puts in attested credential data."""
    numbers = public.public_numbers()
    return cbor2.dumps(
        {
            1: 2,        # kty: EC2
            3: -7,       # alg: ES256
            -1: 1,       # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }
    )


@dataclass
class SoftAuthenticator:
    """One authenticator holding one credential, the way a phone holds one passkey."""

    rp_id: str
    origin: str
    credential_id: bytes = field(default_factory=lambda: os.urandom(32))
    sign_count: int = 0
    _key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    # --- pieces ---------------------------------------------------------------------

    def _client_data(self, *, kind: str, challenge: bytes, origin: str | None = None) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": b64(challenge),
                "origin": origin or self.origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def _auth_data(self, *, rp_id: str | None = None, flags: int, count: int) -> bytes:
        rp_hash = hashlib.sha256((rp_id or self.rp_id).encode("utf-8")).digest()
        data = rp_hash + bytes([flags]) + struct.pack(">I", count)
        if flags & FLAG_AT:
            public = _cose_key(self._key.public_key())
            data += AAGUID + struct.pack(">H", len(self.credential_id)) + self.credential_id
            data += public
        return data

    # --- ceremonies -----------------------------------------------------------------

    def register(self, challenge: bytes, *, origin: str | None = None, rp_id: str | None = None):
        """Produce a registration response. `origin`/`rp_id` override for the negative tests."""
        client_data = self._client_data(kind="webauthn.create", challenge=challenge, origin=origin)
        auth_data = self._auth_data(
            rp_id=rp_id, flags=FLAG_UP | FLAG_UV | FLAG_AT, count=self.sign_count
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def authenticate(
        self,
        challenge: bytes,
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        advance: bool = True,
    ):
        """Produce an assertion.

        `advance=False` replays the current counter — a signature that verifies perfectly and
        must still be refused, which is the only reason §4.2 stores `sign_count` at all.
        """
        if advance:
            self.sign_count += 1
        client_data = self._client_data(kind="webauthn.get", challenge=challenge, origin=origin)
        auth_data = self._auth_data(rp_id=rp_id, flags=FLAG_UP | FLAG_UV, count=self.sign_count)
        signature = self._key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


__all__ = ["SoftAuthenticator", "b64"]
