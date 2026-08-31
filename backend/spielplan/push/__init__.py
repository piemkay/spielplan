"""Web-push delivery — the sending half. Spec v2.1 §2, §4.2, §6 preamble, §7.3, §12 (M4).

`api/push.py` shipped the subscribe/unsubscribe half in M2; §12's M4 row ("push join") and
§7.3's "push arrives with the M4 stack" fund this one. Two modules, split along the line the
spec draws between custody and protocol:

  * `keys`  — the VAPID keypair. §2: "A web-push VAPID keypair is generated at first boot and
    stored the same way" as every other secret, so the private half is AEAD-sealed under the
    DEK and carries its `key_id`. It never leaves this module as bytes: callers get a
    `VapidKeys` that can sign and nothing else, because a private half that can be serialised
    is a private half an `api/` route can accidentally return.
  * `send`  — RFC 8291 (aes128gcm) message encryption and RFC 8292 VAPID, over `httpx`. Both
    are hand-rolled against `cryptography`, which already carries the EC/ECDH/HKDF/AES-GCM
    primitives for §2's DEK and §3.2's passkeys: §1 pins a CPU-only image whose dependency set
    a static guard polices, and pywebpush/py-vapid/http-ece would each add one to do arithmetic
    the image already has.

§6's preamble makes push **best-effort** — "every push-carried prompt also exists as an in-app
banner" — so nothing here raises into a caller. A lobby that blocked on a delivery receipt
would break on exactly the iPhone that constraint was written about.
"""
