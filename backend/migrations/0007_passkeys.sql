-- 0007_passkeys — the one table WebAuthn needs that §4.2 does not name. Spec v2.1 §3.2, §14.4.
--
-- `webauthn_credential` (0002) stores what a passkey *is*. This stores what a ceremony *was*:
-- the challenge issued for one registration or one sign-in, so the response can be verified
-- against the value this server actually generated.
--
-- It has to be server-side and single-use. A challenge the client chooses, or one the server
-- accepts twice, turns the whole ceremony into a signature over attacker-supplied bytes and
-- the replay guard on `sign_count` becomes the only thing left standing.
CREATE TABLE webauthn_challenge (
    id         text PRIMARY KEY,          -- opaque handle; the client echoes it back on verify
    user_id    bigint REFERENCES app_user(id) ON DELETE CASCADE,   -- NULL for a sign-in
    purpose    text NOT NULL CHECK (purpose IN ('register', 'authenticate')),
    challenge  bytea NOT NULL,
    -- §14.4: "changing PUBLIC_URL later invalidates registered passkeys." Recording the rp_id
    -- the challenge was issued under means a ceremony that straddles a PUBLIC_URL change is
    -- rejected on the spot rather than producing a credential nobody can ever use.
    rp_id      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX webauthn_challenge_expiry ON webauthn_challenge (expires_at);
