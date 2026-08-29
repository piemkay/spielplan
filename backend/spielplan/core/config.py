"""Configuration. Spec v2.1 §2.

Env vars with sane defaults. The load-bearing rule: `SECRETS_KEY` is required for
secret-dependent connectors and NEVER falls back to `SESSION_SECRET` — the app refuses to
start those connectors rather than silently weakening the boundary.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # §3.2: 90-day sliding sessions; admin routes re-prompt after 24 h.
    session_days: int = 90
    admin_reauth_hours: int = 24

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

    # §7.3: ">= 90% playback … arms a per-user prompt". A threshold, not a constant, because
    # a household that watches through the credits will want it lower.
    finish_threshold: float = 0.9

    @field_validator("public_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

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
        from urllib.parse import urlparse

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
