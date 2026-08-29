-- 0002_users — accounts, auth, sessions. Spec v2.1 §3, §4.2 (user/webauthn_credential).

CREATE TABLE app_user (
    id               bigserial PRIMARY KEY,
    name             text NOT NULL,
    role             text NOT NULL CHECK (role IN ('admin', 'member', 'guest')),
    avatar           text,
    colour           text,                       -- §6.8: stable per-person accent
    jellyfin_user_id text,                       -- §3.3: optional, one-to-one
    -- §3.1: creation issues a one-time password and locks the account to a password change
    -- at first login; passkey registration is prompted afterwards.
    password_hash    text,
    must_change_password boolean NOT NULL DEFAULT false,
    -- §3.2: optional 4-digit PIN for fast user-switching on a shared/TV device.
    -- 10,000 possibilities, so the lockout below is the actual defence, not the hash.
    pin_hash         text,
    pin_failed_count integer NOT NULL DEFAULT 0,
    pin_locked_until timestamptz,
    is_active        boolean NOT NULL DEFAULT true,
    -- §6.7, owner decision 2026-08-29: one global per-user "show the model" preference,
    -- default off, living in the account dropdown. It governs the transparency rail and every
    -- inline numeric annotation; the title card's model line is deliberately NOT gated (§6.0).
    show_model       boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX app_user_name_key ON app_user (lower(name));
-- §3.3: the app-user <-> jellyfin-user map is one-to-one.
CREATE UNIQUE INDEX app_user_jellyfin_key ON app_user (jellyfin_user_id)
    WHERE jellyfin_user_id IS NOT NULL;

-- §3.2: WebAuthn passkeys are primary. Multiple per user (phone + desktop).
-- Credentials are bound to PUBLIC_URL; changing it invalidates all of these (§14.4).
CREATE TABLE webauthn_credential (
    credential_id  bytea PRIMARY KEY,
    user_id        bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    public_key     bytea NOT NULL,
    sign_count     bigint NOT NULL DEFAULT 0,
    transports     text[],
    label          text,
    rp_id          text NOT NULL,               -- the origin these were registered against
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz
);
CREATE INDEX webauthn_credential_user ON webauthn_credential (user_id);

-- §3.2: HttpOnly cookies, 90-day sliding; admin routes re-prompt after 24 h, which is why
-- the elevation timestamp lives on the session rather than being re-derived.
-- NAME NOTE: §4.2 reserves the bare name `session` for a *Tonight* session, so the auth
-- session table is `auth_session`. The spec never names this table.
CREATE TABLE auth_session (
    id              text PRIMARY KEY,           -- opaque, signed into the cookie
    user_id         bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    device_label    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    admin_verified_at timestamptz,
    auth_method     text NOT NULL CHECK (auth_method IN ('password', 'passkey', 'pin'))
);
CREATE INDEX auth_session_user ON auth_session (user_id);
CREATE INDEX auth_session_expiry ON auth_session (expires_at);

-- §4.2: web-push targets; pruned on 404/410 from the push service.
CREATE TABLE push_subscription (
    id            bigserial PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    device_label  text,
    endpoint      text NOT NULL UNIQUE,
    p256dh        text NOT NULL,
    auth          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_ok  timestamptz
);
CREATE INDEX push_subscription_user ON push_subscription (user_id);
