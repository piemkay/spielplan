"""The three keyed sources' credentials, read out of the connector store (decision 377).

Absent or unreadable credentials are `None`, never an exception: stage 2 notes and advances.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from spielplan.core import secrets

log = logging.getLogger("spielplan.sources.credentials")

TMDB = "tmdb"
OMDB = "omdb"
TRAKT = "trakt"


async def _blobs(conn: asyncpg.Connection, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The connector's (config, secret) pair, or two empty dicts if it cannot be read."""
    try:
        return await secrets.get_connector_secrets(conn, name)
    except secrets.SecretsUnreadable as exc:
        # Logged on every read: not transient, and names the source that stops.
        log.error(
            "connector %s secrets are unreadable, so its source is skipped this drain: %s",
            name, exc,
        )
        return {}, {}


async def tmdb_auth(conn: asyncpg.Connection) -> tuple[dict[str, str], dict[str, str]] | None:
    """`(headers, params)` for a TMDB request, or None when no key is configured. v3 `api_key` only."""
    _config, secret = await _blobs(conn, TMDB)
    key = str(secret.get("api_key") or "")
    if not key:
        return None
    return {"accept": "application/json"}, {"api_key": key}


async def omdb_key(conn: asyncpg.Connection) -> str | None:
    """The OMDb `apikey` query value, or None when none is configured."""
    _config, secret = await _blobs(conn, OMDB)
    return str(secret.get("api_key") or "") or None


async def trakt_headers(conn: asyncpg.Connection) -> dict[str, str] | None:
    """The three headers every Trakt call carries, or None when no client id is configured.

    The client id lives in the config blob, not the sealed one, so this works with an unreadable DEK.
    """
    config, _secret = await _blobs(conn, TRAKT)
    client_id = str(config.get("client_id") or "")
    if not client_id:
        return None
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }


__all__ = ["OMDB", "TMDB", "TRAKT", "omdb_key", "tmdb_auth", "trakt_headers"]
