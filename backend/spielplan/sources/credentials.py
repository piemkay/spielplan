"""The three keyed sources' credentials, read out of the connector store. Decision 377.

Spec v2.1 §2 ("Everything connector-related ... is configured **in the admin UI** and stored in
`connector_config` - not env vars"), §3.1 (a half-configured boot is legal), §6.6; decisions
334, 377.

PORT VERDICT: **rewritten**, because the thing being replaced is not code but a config file.
The corpus reads `ctx.cfg.tmdb_api_key`, `ctx.cfg.omdb_api_key` and `ctx.cfg.trakt_client_id`
off a `Config` loaded from disk (`mdc/sources/tmdb.py:34-41`, `omdb.py:31`, `trakt.py:22-29`);
§2 puts the same three in `connector_config`, behind the DEK. What is ported is the *shape* each
source asks for - TMDB wants `(headers, params)`, OMDb wants one query parameter, Trakt wants
three headers - so the adapters read here exactly what they read there.

NARROW BY CONSTRUCTION, AND KEPT WHEN THE GENERIC SEAM ARRIVED. Decision 377 chose the
three-function form so that the milestone building the generic `ConnectorSpec` could delete this
file rather than reconcile a second design. M5.5 built it - `registry.CONNECTORS` registers tmdb,
omdb and trakt for loading, saving and env seeding - and kept this module (decision 434), because
deleting or re-pointing it buys no reader. Both paths read through
`core.secrets.get_connector_secrets`, so there is one store and one reader, not two answers to
"what is configured". What this module returns is source-shaped request parts - TMDB's headers
and params, OMDb's query value, Trakt's headers - which are an adapter's concern and not
connector plumbing. Its registered test pins a degrade line naming the skipped source, which a
generic loader cannot name. And re-pointing it would edit three adapters and `acquire/stages.py`
for no new reader. It exposes `tmdb_auth`, `omdb_key` and `trakt_headers` and nothing generic;
M5.7's source cards write through `registry.save_connector`, into the rows this module reads.

AN ABSENT CREDENTIAL IS `None` AND NEVER AN EXCEPTION. The corpus raises `Skip`
(`mdc/sources/tmdb.py:41`, `mdc/sources/trakt.py:23`) and `mdc/runner.py` catches it; decision
334 makes a source with no credential a note in `acquisition_job.detail` under that source's
name, with stage 2 advancing, so the refusal is a return value here for the reason
`acquire/stages.py:20-29` gives one level up - "a normal outcome raised as an exception is one a
later `except Exception` swallows into a failure". An install that has configured TMDB and not
OMDb is §3.1's legal half-configured boot and not a broken drain, and under decision 334 only
`tmdb:detail` could park a stage at all.

A SECRET THAT WILL NOT OPEN IS ALSO `None`, AND IT IS LOGGED. The shipped read path raises
`secrets.SecretsUnreadable` for a restored dump whose `.env` did not travel with it
(M4.7 dd03), and `registry.load_jellyfin` already answers that state by degrading to an
unconfigured-looking config rather than by raising - "an unreadable DEK must not be able to take
a route down". Same answer here and for the same reason: a drain that died on it would take the
five keyless sources down with the three keyed ones, and the repair is an admin gesture rather
than a retry.

READ THROUGH THE SHIPPED PATH, WITH NO SQL OF ITS OWN. `secrets.get_connector_secrets` is the
function `registry.load_jellyfin` and M5.5's generic `registry.load_connector` both call
(decision 434). A second `SELECT ... FROM connector_config` here would be a second answer
to "what is configured" the day the admin UI starts writing these rows.
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
    """The connector's (config, secret) pair, or two empty dicts if it cannot be read.

    `get_connector_secrets` already answers `({}, {})` for a connector with no row, so the only
    arm this adds is the custody one.
    """
    try:
        return await secrets.get_connector_secrets(conn, name)
    except secrets.SecretsUnreadable as exc:
        # ERROR and on every read, which is `registry.load_jellyfin`'s own choice for this state
        # and its argument: §6.6 names logs as the operator's data, this is not transient, and
        # de-duplicating it would need process state the worker container would not share with
        # the backend. The sentence says which source stops, because that is what an operator
        # reading a board full of notes needs to connect to the one repair.
        log.error(
            "connector %s secrets are unreadable, so its source is skipped this drain: %s",
            name, exc,
        )
        return {}, {}


async def tmdb_auth(conn: asyncpg.Connection) -> tuple[dict[str, str], dict[str, str]] | None:
    """`(headers, params)` for a TMDB request, or None when no key is configured.

    ONE ARM WHERE THE CORPUS HAS TWO. `mdc/sources/tmdb.py:34-41` prefers a v4 bearer token and
    falls back to the v3 `api_key` query parameter; this app declares `tmdb_api_key` and no
    bearer (`core/config.py:71`), `registry.env_seeds` seeds that one key, and decision 377
    forbids widening either file from here. So the v3 arm is the whole of it, and the milestone
    that adds a bearer adds it where the admin UI writes one.
    """
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

    The client id is in the CONFIG blob and not the secret one, which is `registry.env_seeds`'
    own split (`:94-98`): the id travels in a request header on every call and is not a
    credential, while `trakt_client_secret` - which nothing in §8 stage 2 uses, because the
    summary and comments endpoints are unauthenticated - is sealed. So this read answers on an
    install whose DEK cannot be opened, which is the correct outcome rather than a lucky one.
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
