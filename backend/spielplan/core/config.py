from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("spielplan")

# §2's documented `token_urlsafe` gesture emits 43+ chars; anything shorter was typed by hand.
_MIN_SECRET_CHARS = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://spielplan:spielplan@db:5432/spielplan"

    # WebAuthn binds every passkey to this origin; changing it invalidates them all (§14.4).
    public_url: str = "http://localhost:8080"

    session_secret: str = ""
    # Wraps the DEK for connector secrets. Never derived from session_secret (§2).
    secrets_key: str | None = None

    tz: str = "Europe/Berlin"
    data_dir: Path = Path("/data")
    static_dir: Path | None = Field(default=None, alias="SPIELPLAN_STATIC_DIR")
    role: str = Field(default="backend", alias="SPIELPLAN_ROLE")

    # Turns the §2 refusals below off, for README's host-run dev flow only.
    insecure_dev: bool = Field(default=False, alias="SPIELPLAN_INSECURE_DEV")

    # Decision 483's no-egress switch for posters; e2e turns it off. Jellyfin is asked either way.
    art_egress: bool = Field(default=True, alias="SPIELPLAN_ART_EGRESS")

    # 0 would not mean "disabled": it expires every session on arrival, hence gt=0.
    session_days: int = Field(default=90, gt=0, le=3650)
    admin_reauth_hours: int = Field(default=24, gt=0)

    # First-boot seed only (§2): read once by `connectors.registry.seed_from_env`, never at runtime.
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    tmdb_api_key: str = ""
    omdb_api_key: str = ""
    trakt_client_id: str = ""
    trakt_client_secret: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    @field_validator("public_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _required_config_is_present(self) -> Settings:
        """Refuse at construction, the one place every entrypoint passes, with all reasons at once."""
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
        # Absent stays legal (§3.1's half-configured boot); only a typed short key is refused.
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
