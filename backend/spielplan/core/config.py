"""Configuration. Spec v2.1 §2.

Env vars with sane defaults. The load-bearing rule: `SECRETS_KEY` is required for
secret-dependent connectors and NEVER falls back to `SESSION_SECRET` — the app refuses to
start those connectors rather than silently weakening the boundary.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("spielplan")

# §2 generates both secrets with `secrets.token_urlsafe`, at 48 and 32 bytes (`.env.example:12,18`),
# so every value an operator following the documentation produces is 43 characters or longer. The
# floor is therefore not a guess about entropy: it is the shortest thing the documented gesture can
# emit, and anything below it was typed by hand. `SECRETS_KEY=x` HKDF-expands into a perfectly valid
# KEK (`core/secrets._kek`), which is exactly how a guessable key guards §14.3's admin-equivalent
# Jellyfin credential while every test stays green.
_MIN_SECRET_CHARS = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://spielplan:spielplan@db:5432/spielplan"

    # §2: required config because WebAuthn binds credentials to the origin. The same origin
    # must be reachable via Tailscale so passkeys work identically at home and remote.
    # §14.4: changing this later invalidates every registered passkey.
    public_url: str = "http://localhost:8080"

    session_secret: str = ""
    # §2: wraps the DEK that AEAD-encrypts connector secrets. Absent => connectors that need
    # secrets refuse to start. Never derived from session_secret.
    secrets_key: str | None = None

    tz: str = "Europe/Berlin"
    data_dir: Path = Path("/data")
    static_dir: Path | None = Field(default=None, alias="SPIELPLAN_STATIC_DIR")
    role: str = Field(default="backend", alias="SPIELPLAN_ROLE")

    # The one way past the refusals below, for the two processes that legitimately have no
    # operator: `ops/devstub.py` (a harness with no database, no cookies worth signing and no
    # passkeys) and README's host-run "Developing" flow. It is a field rather than a bare
    # `os.environ` read so that it is declared where the rest of the required config is, and it
    # announces itself at WARNING every time it is honoured — an install that reaches this line by
    # accident says so in the log rather than signing cookies with nothing.
    insecure_dev: bool = Field(default=False, alias="SPIELPLAN_INSECURE_DEV")

    # §3.2: 90-day sliding sessions; admin routes re-prompt after 24 h. Both numbers are fixed
    # by the spec, so the env vars tune them and cannot turn them off: 0 is not "disabled", it
    # is a session that has expired by the time the login response arrives, or an admin route
    # that re-prompts milliseconds after a fresh sign-in — silently, with nothing logged.
    session_days: int = Field(default=90, gt=0, le=3650)
    admin_reauth_hours: int = Field(default=24, gt=0)

    # §2: "Everything connector-related … is configured in the admin UI and stored in
    # `connector_config` — not env vars … env vars may *seed* connector config on first boot
    # for automated installs." These fields are that seed and nothing else: they are read once
    # at boot by `connectors.registry.seed_from_env`, only for a connector with no row yet, and
    # no runtime code path reads them again. After first boot the database is the source.
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    tmdb_api_key: str = ""
    omdb_api_key: str = ""
    trakt_client_id: str = ""
    trakt_client_secret: str = ""

    @field_validator("public_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _required_config_is_present(self) -> Settings:
        """§2 calls PUBLIC_URL, SESSION_SECRET and SECRETS_KEY required config. Enforce it here.

        Until now the only enforcement was `docker-compose.yml`'s `${VAR:?}`, which exists for
        exactly one of the ways this app is started: not for README's host-run "Developing" flow,
        not for a systemd unit, not for a CI job with a blank env. Outside compose the defaults
        were accepted silently and two of them are dangerous. An empty `session_secret` signed
        every session cookie, Rank pair and Tonight pair with `"insecure-dev-secret"` — a constant
        in the public repository — so §2's "rotating SESSION_SECRET invalidates sessions" was
        vacuous and anyone could mint a cookie. And `rp_id` reads `urlparse(public_url).hostname or
        "localhost"`, so both `""` and `spielplan.example.tld` (a bare hostname, which is what an
        operator writes when copying a host rather than the template's URL) bound every passkey
        registered in that window to `localhost`; §14.4's promise is that changing PUBLIC_URL
        invalidates credentials, and this reached it by accident, with the browser reporting only
        "the relying party ID is not a registrable domain suffix".

        Refusing at construction is the only place that covers every entrypoint, and it refuses
        with all the reasons at once so a half-filled `.env` is one round trip, not three.
        [M4.7 sec-03, spec-04, ops-10, cs-44; decision 181]
        """
        if self.insecure_dev:
            log.warning(
                "SPIELPLAN_INSECURE_DEV is set: the spec section 2 config refusals are OFF. "
                "This process may sign cookies with an empty SESSION_SECRET and bind passkeys "
                "to localhost. Never set it on an install anyone else can reach."
            )
            return self

        problems: list[str] = []
        parsed = urlparse(self.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            problems.append(
                f"PUBLIC_URL must be the full origin the app is reached on, scheme included "
                f"(e.g. https://spielplan.example.tld) - got {self.public_url!r}. WebAuthn binds "
                f"every passkey to this origin (spec section 2, section 14.4)"
            )
        if len(self.session_secret) < _MIN_SECRET_CHARS:
            problems.append(
                f"SESSION_SECRET must be at least {_MIN_SECRET_CHARS} characters; generate one "
                'with: python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )
        # Absent stays legal: §3.1 makes a half-configured boot a legal state and
        # `require_secrets_key` below is the refusal that belongs to it. An empty string is what
        # `.env.example:19` ships and what compose's `${SECRETS_KEY:?}` already rejects, and
        # `require_secrets_key` reads it as absent, so only a *typed* short key is refused here.
        if self.secrets_key and len(self.secrets_key) < _MIN_SECRET_CHARS:
            problems.append(
                f"SECRETS_KEY must be at least {_MIN_SECRET_CHARS} characters; generate one "
                'with: python -c "import secrets;print(secrets.token_urlsafe(32))"'
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def import_dir(self) -> Path:
        return self.data_dir / "import"

    @property
    def rp_id(self) -> str:
        """WebAuthn relying-party id — the host of PUBLIC_URL."""
        return urlparse(self.public_url).hostname or "localhost"

    def require_secrets_key(self) -> str:
        if not self.secrets_key:
            raise RuntimeError(
                "SECRETS_KEY is not set. Spec §2: connector secrets are AEAD-encrypted under a "
                "DEK wrapped by SECRETS_KEY, and the app refuses to start secret-dependent "
                "connectors rather than falling back to SESSION_SECRET."
            )
        return self.secrets_key


@lru_cache
def settings() -> Settings:
    return Settings()
