-- 0001_system — bootstrap, secrets, config, artifact bundles.
-- Spec v2.1 §2 (secrets), §3.1 (a bundle-less app is a legal state), §4.2 (tail), §10.

CREATE TABLE schema_migration (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text NOT NULL
);

-- §2: SECRETS_KEY wraps a random 256-bit data-encryption key created at first boot and
-- stored here. Connector secrets are AEAD-encrypted under that DEK; every ciphertext
-- carries a key_id so rotation is possible. Rotating SECRETS_KEY re-wraps this one row.
CREATE TABLE data_encryption_key (
    key_id       text PRIMARY KEY,
    wrapped_dek  bytea NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    retired_at   timestamptz
);

-- §2/§6.6: everything connector-related is configured in the admin UI and stored here,
-- not in env vars. Env vars may only *seed* this table on first boot.
CREATE TABLE connector_config (
    name               text PRIMARY KEY,
    config             jsonb NOT NULL DEFAULT '{}'::jsonb,
    secrets_encrypted  bytea,
    secrets_key_id     text REFERENCES data_encryption_key(key_id),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT connector_secret_has_key
        CHECK ((secrets_encrypted IS NULL) = (secrets_key_id IS NULL))
);

-- §10: bundle import is a planned admin event with a diff report, never a silent sync.
-- Invariant: no process may score or refit with a loaded bundle version different from
-- the active row, enforced by the partial unique index below.
CREATE TABLE artifact_bundle (
    version      text PRIMARY KEY,
    imported_at  timestamptz NOT NULL DEFAULT now(),
    manifest     jsonb NOT NULL,
    report       jsonb NOT NULL DEFAULT '{}'::jsonb,
    state        text NOT NULL DEFAULT 'staged'
                 CHECK (state IN ('staged', 'validated', 'active', 'superseded', 'failed')),
    activated_at timestamptz
);
CREATE UNIQUE INDEX artifact_bundle_one_active
    ON artifact_bundle ((state)) WHERE state = 'active';

-- §2: web-push VAPID keypair, generated at first boot, wrapped like every other secret.
CREATE TABLE app_setting (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    secret      bytea,
    secret_key_id text REFERENCES data_encryption_key(key_id),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- §3.1: the first-boot wizard is a defined sequence; each step records its completion so a
-- half-finished install resumes rather than restarting. A bundle-less app is a legal state,
-- so 'bundle' completing is NOT a precondition for the app serving.
CREATE TABLE setup_step (
    step         text PRIMARY KEY
                 CHECK (step IN ('admin', 'connectors', 'bundle', 'members', 'onboarding')),
    completed_at timestamptz NOT NULL DEFAULT now(),
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
