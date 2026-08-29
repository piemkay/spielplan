"""Connector configuration: where it lives, and the one time an env var may write it.

Spec v2.1 §2: "Everything connector-related … is configured **in the admin UI** and stored in
`connector_config` — not env vars, because the owner explicitly wants connector setup in the
admin view; env vars may *seed* connector config on first boot for automated installs."

Two rules follow, and both are load-bearing:

  1. **Seeding is first-boot only.** `seed_from_env` writes a connector that has no row yet
     and never touches one that does. If env could overwrite, every container restart would
     silently revert whatever the admin last saved, which is the failure mode that makes
     people stop trusting the admin UI.
  2. **A secret needs SECRETS_KEY.** Seeding an API key without one would either drop it on
     the floor or store it in the clear; the app refuses instead (§2), loudly, at boot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.core import secrets
from spielplan.core.config import Settings, settings

log = logging.getLogger("spielplan.connectors")

JELLYFIN = "jellyfin"


@dataclass(frozen=True)
class JellyfinConfig:
    """The Jellyfin connector as configured, with its secrets already opened."""

    url: str = ""
    api_key: str = ""
    library_ids: list[str] = field(default_factory=list)
    # §7.3: per-user access tokens obtained at link time, keyed by *app* user id as a string
    # because JSON object keys are strings and round-tripping them as ints invites a silent
    # type mismatch between "3" and 3.
    user_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key)

    def token_for(self, app_user_id: int) -> str | None:
        return self.user_tokens.get(str(app_user_id))


def env_seeds(cfg: Settings | None = None) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """The (config, secrets) pair each connector would be seeded with, for those whose env
    vars are actually set. A connector with no env presence is absent from this mapping, which
    is what keeps "nothing configured" distinguishable from "configured empty"."""
    cfg = cfg or settings()
    seeds: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    if cfg.jellyfin_url and cfg.jellyfin_api_key:
        seeds[JELLYFIN] = (
            {"url": cfg.jellyfin_url.rstrip("/"), "library_ids": []},
            {"api_key": cfg.jellyfin_api_key, "user_tokens": {}},
        )
    # §6.6 configures these in the admin UI at M5; seeding them here means an automated
    # install can arrive with them already filled in.
    if cfg.tmdb_api_key:
        seeds["tmdb"] = ({}, {"api_key": cfg.tmdb_api_key})
    if cfg.omdb_api_key:
        seeds["omdb"] = ({}, {"api_key": cfg.omdb_api_key})
    if cfg.trakt_client_id:
        seeds["trakt"] = (
            {"client_id": cfg.trakt_client_id},
            {"client_secret": cfg.trakt_client_secret} if cfg.trakt_client_secret else {},
        )
    return seeds


async def seed_from_env(conn: asyncpg.Connection, cfg: Settings | None = None) -> list[str]:
    """Write env-provided connector config for connectors that have no row yet.

    Returns the names actually seeded. Called once at boot; idempotent by construction,
    because the second call finds the rows it wrote the first time and leaves them alone.
    """
    seeds = env_seeds(cfg)
    if not seeds:
        return []

    existing = {
        r["name"] for r in await conn.fetch("SELECT name FROM connector_config")
    }
    seeded: list[str] = []
    for name, (config, secret) in seeds.items():
        if name in existing:
            # §2: the DB wins after first boot. Say so once rather than silently diverging
            # from what the operator put in the env file.
            log.info("connector %s already configured — env seed ignored (§2)", name)
            continue
        if secret:
            # Refuse rather than fall back (§2). Raising here stops the boot, which is the
            # correct loudness: a connector seeded without custody is worse than no connector.
            #
            # Deliberately `settings()` and not the `cfg` argument: `cfg` supplies the env
            # *values* to seed, while custody is a process-wide fact that `core.secrets` reads
            # from `settings()`. Checking the argument here would let a caller pass a config
            # whose SECRETS_KEY the storage layer never sees, and the guard would pass while
            # the write failed one frame later.
            settings().require_secrets_key()
        await secrets.put_connector_secrets(conn, name, config, secret or None)
        seeded.append(name)

    if seeded:
        await conn.execute(
            "INSERT INTO setup_step (step) VALUES ('connectors') ON CONFLICT (step) DO NOTHING"
        )
        log.info("seeded connector config from env: %s", ", ".join(sorted(seeded)))
    return seeded


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) — what "the same server" means for credential binding."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return (parsed.scheme, parsed.hostname or "", parsed.port)


def make_client(cfg: JellyfinConfig):
    """Build the Jellyfin client for a stored configuration, or None if there is none.

    One construction site, called module-qualified everywhere, so an integration test can point
    the whole app at `ops/fake_jellyfin.py` by replacing this single function instead of
    threading a transport through every route.
    """
    from spielplan.connectors.jellyfin import JellyfinClient

    return JellyfinClient(cfg.url, cfg.api_key) if cfg.configured else None


async def load_jellyfin(conn: asyncpg.Connection) -> JellyfinConfig:
    config, secret = await secrets.get_connector_secrets(conn, JELLYFIN)
    tokens = secret.get("user_tokens") or {}
    return JellyfinConfig(
        url=str(config.get("url") or ""),
        api_key=str(secret.get("api_key") or ""),
        library_ids=list(config.get("library_ids") or []),
        user_tokens={str(k): str(v) for k, v in tokens.items() if v},
    )


async def save_jellyfin(
    conn: asyncpg.Connection,
    *,
    url: str | None = None,
    api_key: str | None = None,
    library_ids: list[str] | None = None,
    user_tokens: dict[str, str] | None = None,
) -> JellyfinConfig:
    """Merge a partial update into the stored connector.

    Partial on purpose: the admin UI shows the API key as a masked placeholder and posts the
    URL without it, and a whole-row write would blank the key every time someone corrects a
    typo in the address.
    """
    current = await load_jellyfin(conn)
    next_url = (url if url is not None else current.url).rstrip("/")

    # §14.3: the API key is admin-equivalent on the whole media server, and the per-user tokens
    # are real credentials. They are bound to the server they were issued by, so pointing the
    # connector at a different origin must not carry them along — every later request would
    # send them, in a header, to whatever host was just typed in. Changing the port or the
    # scheme counts: it is a different server until someone says otherwise by re-entering.
    moved = bool(current.url) and _origin(next_url) != _origin(current.url)
    if moved:
        log.info("jellyfin origin changed — stored credentials dropped, re-entry required (§14.3)")

    merged = JellyfinConfig(
        url=next_url,
        api_key=api_key if api_key else ("" if moved else current.api_key),
        library_ids=library_ids if library_ids is not None else current.library_ids,
        user_tokens=(
            user_tokens
            if user_tokens is not None
            else ({} if moved else current.user_tokens)
        ),
    )
    # A URL on its own is not a secret, and saving one must not demand SECRETS_KEY — the admin
    # types the address first and pastes the key second.
    secret: dict[str, Any] | None = None
    if merged.api_key or merged.user_tokens:
        settings().require_secrets_key()
        secret = {"api_key": merged.api_key, "user_tokens": merged.user_tokens}
    await secrets.put_connector_secrets(
        conn, JELLYFIN, {"url": merged.url, "library_ids": merged.library_ids}, secret
    )
    return merged


__all__ = [
    "JELLYFIN",
    "JellyfinConfig",
    "env_seeds",
    "make_client",
    "load_jellyfin",
    "save_jellyfin",
    "seed_from_env",
]
