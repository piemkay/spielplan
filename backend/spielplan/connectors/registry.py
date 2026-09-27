"""Connector configuration: stored in `connector_config`, seeded from env on first boot only (§2).

Seeding never overwrites a row and refuses a secret without SECRETS_KEY. Every §6.6 card but
Jellyfin's is one row of `FIELDS`; Jellyfin's has its own load/save.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from secrets import token_urlsafe
from typing import Any

import asyncpg

from spielplan.core import secrets
from spielplan.core.config import Settings, settings

log = logging.getLogger("spielplan.connectors")

JELLYFIN = "jellyfin"

# The one sentence every degraded surface says; it names what an operator can act on (M4.7 dd03).
SECRETS_UNREADABLE_REASON = "connector secrets unreadable (SECRETS_KEY)"


@dataclass(frozen=True)
class JellyfinConfig:
    """The Jellyfin connector as configured, with its secrets already opened."""

    url: str = ""
    # Credentials stay out of the repr (§14.3); `url` stays visible.
    api_key: str = field(default="", repr=False)
    library_ids: list[str] = field(default_factory=list)
    # §7.3's per-user tokens, keyed by app user id as a string (JSON keys are strings).
    user_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    # §7.2's webhook bearer token, minted by this app (decision 332); sealed and out of the repr.
    webhook_token: str = field(default="", repr=False)
    # §7.1's probed pin; gates the Played write. `None` is "never probed", distinct from refused.
    server_version: str = ""
    server_supported: bool | None = None
    # §7.2's delta watermark (decisions 366, 409), plaintext config; `None` is "never polled".
    delta_watermark: datetime | None = None
    # Configured, but the credentials cannot be opened with this SECRETS_KEY (§2, §3.3).
    secrets_unreadable: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key)

    def token_for(self, app_user_id: int) -> str | None:
        return self.user_tokens.get(str(app_user_id))

    def webhook_token_matches(self, presented: str | None) -> bool:
        """§7.2's webhook token check: an empty stored token matches nothing.

        `compare_digest` over bytes: a latin-1 header with a non-ASCII byte must not raise.
        """
        return bool(self.webhook_token) and hmac.compare_digest(
            (presented or "").encode("utf-8"), self.webhook_token.encode("utf-8")
        )


def env_seeds(cfg: Settings | None = None) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """The (config, secrets) pair each connector would be seeded with, only for those set in env."""
    cfg = cfg or settings()
    seeds: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    if cfg.jellyfin_url and cfg.jellyfin_api_key:
        seeds[JELLYFIN] = (
            {"url": cfg.jellyfin_url.rstrip("/"), "library_ids": []},
            {"api_key": cfg.jellyfin_api_key, "user_tokens": {}},
        )
    if cfg.tmdb_api_key:
        seeds["tmdb"] = ({}, {"api_key": cfg.tmdb_api_key})
    if cfg.omdb_api_key:
        seeds["omdb"] = ({}, {"api_key": cfg.omdb_api_key})
    if cfg.trakt_client_id:
        seeds["trakt"] = (
            {"client_id": cfg.trakt_client_id},
            {"client_secret": cfg.trakt_client_secret} if cfg.trakt_client_secret else {},
        )
    # LLM provider keys are seeded; the `llm` settings row never is (decision 325).
    if cfg.gemini_api_key:
        seeds["gemini"] = ({}, {"api_key": cfg.gemini_api_key})
    if cfg.anthropic_api_key:
        seeds["anthropic"] = ({}, {"api_key": cfg.anthropic_api_key})
    if cfg.openai_api_key:
        seeds["openai"] = ({}, {"api_key": cfg.openai_api_key})
    return seeds


async def seed_from_env(conn: asyncpg.Connection, cfg: Settings | None = None) -> list[str]:
    """Write env-provided config for connectors with no row yet; returns the names seeded."""
    seeds = env_seeds(cfg)
    if not seeds:
        return []

    existing = {
        r["name"] for r in await conn.fetch("SELECT name FROM connector_config")
    }
    seeded: list[str] = []
    for name, (config, secret) in seeds.items():
        if name in existing:
            # §2: the DB wins after first boot.
            log.info("connector %s already configured — env seed ignored (§2)", name)
            continue
        if secret:
            # Refuse rather than fall back (§2); custody is process-wide, hence `settings()`.
            settings().require_secrets_key()
        try:
            await secrets.put_connector_secrets(conn, name, config, secret or None)
        except secrets.SecretsUnreadable as exc:
            # Reported, never raised: this runs first in the lifespan, and a crash loop blocks repair.
            log.error("connector %s not seeded from env: %s", name, exc)
            continue
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


def _instant(raw: Any) -> datetime | None:
    """A stored ISO instant as an aware datetime, or None (decision 366's watermark).

    An unparseable value reads as "never polled": a re-read is absorbed, a guessed floor is not.
    """
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _new_webhook_token() -> str:
    """One token for §7.2's webhook (decision 332): 256 bits, URL-safe for pasting by hand."""
    return token_urlsafe(32)


def make_client(cfg: JellyfinConfig):
    """Build the Jellyfin client for a stored configuration, or None if there is none.

    The one construction site, so a test can point the app at `ops/fake_jellyfin.py` here.
    """
    from spielplan.connectors.jellyfin import JellyfinClient

    if not cfg.configured:
        return None
    # The stored verdict travels with the client, so the one write can refuse by name (§7.1).
    return JellyfinClient(
        cfg.url, cfg.api_key,
        server_version=cfg.server_version, server_supported=cfg.server_supported,
    )


async def load_jellyfin(conn: asyncpg.Connection, *, for_update: bool = False) -> JellyfinConfig:
    """The stored connector, or a truthfully degraded one when its secrets will not open.

    Degraded keeps the plaintext config and drops the credentials, so no route 500s (dd03).
    `for_update` locks the row for a read-modify-write of the sealed token map (§14.3).
    """
    if for_update:
        await conn.execute(
            "SELECT name FROM connector_config WHERE name = $1 FOR UPDATE", JELLYFIN
        )
    try:
        config, secret = await secrets.get_connector_secrets(conn, JELLYFIN)
    except secrets.SecretsUnreadable as exc:
        # ERROR on every read: not transient, and §6.6 treats logs as the operator's data.
        log.error("jellyfin connector secrets are unreadable: %s", exc)
        # The whole plaintext half, so the next save does not erase the library pick or watermark.
        stored = await conn.fetchval(
            "SELECT config FROM connector_config WHERE name = $1", JELLYFIN
        ) or {}
        stored_supported = stored.get("server_supported")
        return JellyfinConfig(
            url=str(stored.get("url") or ""),
            library_ids=list(stored.get("library_ids") or []),
            server_version=str(stored.get("server_version") or ""),
            server_supported=None if stored_supported is None else bool(stored_supported),
            delta_watermark=_instant(stored.get("delta_watermark")),
            secrets_unreadable=True,
        )
    tokens = secret.get("user_tokens") or {}
    supported = config.get("server_supported")
    return JellyfinConfig(
        url=str(config.get("url") or ""),
        api_key=str(secret.get("api_key") or ""),
        library_ids=list(config.get("library_ids") or []),
        user_tokens={str(k): str(v) for k, v in tokens.items() if v},
        webhook_token=str(secret.get("webhook_token") or ""),
        server_version=str(config.get("server_version") or ""),
        server_supported=None if supported is None else bool(supported),
        delta_watermark=_instant(config.get("delta_watermark")),
    )


async def save_jellyfin(
    conn: asyncpg.Connection,
    *,
    url: str | None = None,
    api_key: str | None = None,
    library_ids: list[str] | None = None,
    user_tokens: dict[str, str] | None = None,
    server_version: str | None = None,
    server_supported: bool | None = None,
    delta_watermark: datetime | None = None,
    watermark_origin: str | None = None,
    mint_webhook_token: bool = False,
) -> JellyfinConfig:
    """Merge a partial update into the stored connector, in one transaction with the row locked.

    Partial so a masked key is not blanked. A watermark whose `watermark_origin` is not the stored
    server is dropped; only `mint_webhook_token` (the admin PUT) may mint a token (decision 416).
    """
    async with conn.transaction():
        # Create the row first, so two first saves serialise on its lock (decision 332).
        await conn.execute(
            "INSERT INTO connector_config (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            JELLYFIN,
        )
        current = await load_jellyfin(conn, for_update=True)
        next_url = (url if url is not None else current.url).rstrip("/")

        # §14.3: credentials are bound to the origin (scheme, host, port) that issued them.
        moved = bool(current.url) and _origin(next_url) != _origin(current.url)
        if moved:
            log.info("jellyfin origin changed — stored credentials dropped, re-entry required (§14.3)")
        if moved:
            # The link badge is the truth of the tokens: mark it in the same transaction, even unreadable.
            marked = await conn.execute(
                "UPDATE app_user SET jellyfin_link_state = 'needs_relink' "
                "WHERE jellyfin_user_id IS NOT NULL AND jellyfin_link_state = 'linked'"
            )
            if not marked.endswith(" 0"):
                log.info("jellyfin origin changed -- linked accounts need a re-link (%s)", marked)

        # An unreadable row has nothing to carry forward; the custody repair is `ensure_dek`'s.
        lost = current.secrets_unreadable
        # See `watermark_origin` above.
        if watermark_origin is not None and (
            _origin(watermark_origin.rstrip("/")) != _origin(next_url)
        ):
            delta_watermark = None

        merged = JellyfinConfig(
            url=next_url,
            api_key=api_key if api_key else ("" if moved or lost else current.api_key),
            # No `moved` branch: the pick is the admin's intent, travels nowhere, and survives an
            # address correction (decision 364).
            library_ids=library_ids if library_ids is not None else current.library_ids,
            user_tokens=(
                user_tokens
                if user_tokens is not None
                else ({} if moved or lost else current.user_tokens)
            ),
            # A verdict belongs to the server that answered; dropped on a move (§7.1).
            server_version=(
                server_version if server_version is not None
                else ("" if moved else current.server_version)
            ),
            server_supported=(
                server_supported if server_version is not None
                else (None if moved else current.server_supported)
            ),
            # A watermark is the old server's clock: dropped on a move; decision 411 absorbs the re-read.
            delta_watermark=(
                delta_watermark if delta_watermark is not None
                else (None if moved else current.delta_watermark)
            ),
            # Kept across a move: this token is presented to this install, never sent anywhere,
            # and is shown only once (decision 332). `lost` is a read that returned nothing.
            webhook_token="" if lost else current.webhook_token,
        )
        if mint_webhook_token and merged.configured and not merged.webhook_token:
            # Minted once, on the admin's own save (decisions 332, 416); never rotated by a later edit.
            merged = replace(merged, webhook_token=_new_webhook_token())
        # A URL alone is not a secret and needs no SECRETS_KEY.
        secret: dict[str, Any] | None = None
        # The webhook token alone still re-seals the blob, or a move would write NULL over it.
        if merged.api_key or merged.user_tokens or merged.webhook_token:
            settings().require_secrets_key()
            secret = {
                "api_key": merged.api_key,
                "user_tokens": merged.user_tokens,
                "webhook_token": merged.webhook_token,
            }
        stored_config: dict[str, Any] = {"url": merged.url, "library_ids": merged.library_ids}
        if merged.server_version:
            stored_config["server_version"] = merged.server_version
        if merged.server_supported is not None:
            # Absent rather than null: "never probed" is no verdict.
            stored_config["server_supported"] = merged.server_supported
        if merged.delta_watermark is not None:
            # Absent rather than null: "never polled" is no instant.
            stored_config["delta_watermark"] = (
                merged.delta_watermark.astimezone(UTC).isoformat()
            )
        if lost and not moved and secret is None:
            # Keep a ciphertext unreadable only until the right .env returns (§2), unless moved.
            await secrets.put_connector_config(conn, JELLYFIN, stored_config)
            return merged
        # `retire_unreadable`: re-entering the key is the admin gesture that repairs dd03 (§14.3, §2).
        await secrets.put_connector_secrets(
            conn, JELLYFIN, stored_config, secret, retire_unreadable=True
        )
        return merged


# Its `applied_at` is when this install gained §7.2's fallback (decision 412).
_FALLBACK_MIGRATION = "0025_jellyfin_intake"


async def delta_since(conn: asyncpg.Connection, cfg: JellyfinConfig) -> datetime:
    """The instant §7.2's delta poll reads from: the stored watermark, or the first time, 0025's
    `applied_at` (decisions 366, 412). Never epoch, which would enqueue the whole corpus."""
    if cfg.delta_watermark is not None:
        return cfg.delta_watermark
    gained = await conn.fetchval(
        "SELECT coalesce((SELECT applied_at FROM schema_migration WHERE version = $1),"
        "                (SELECT min(applied_at) FROM schema_migration))",
        _FALLBACK_MIGRATION,
    )
    # Unreachable in practice; now is the only floor that cannot enqueue a corpus.
    return gained or datetime.now(UTC)


# --- §6.6's other connectors; Jellyfin's merge (origin, watermark, library pick, mint) is its own ---


@dataclass(frozen=True)
class ConnectorState:
    """Any connector but Jellyfin, as stored: the plaintext half and the opened sealed half."""

    name: str
    config: dict[str, Any] = field(default_factory=dict)
    # Out of the repr (§14.3).
    secrets: dict[str, Any] = field(default_factory=dict, repr=False)
    # Credentials exist but will not open under this SECRETS_KEY (§2, §3.3).
    secrets_unreadable: bool = False


# Model and decision 343's price override (USD per 1M tokens); the key travels in a header (§9).
_PROVIDER_FIELDS = ("model", "price_input", "price_output")

# Each connector's (settings, secrets), as stored.
FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # The keyed sources (decisions 434, 453); Trakt's client id is config, not a secret.
    "tmdb": ((), ("api_key",)),
    "omdb": ((), ("api_key",)),
    "trakt": (("client_id",), ("client_secret",)),
    "gemini": (_PROVIDER_FIELDS, ("api_key",)),
    "anthropic": (_PROVIDER_FIELDS, ("api_key",)),
    "openai": (_PROVIDER_FIELDS, ("api_key",)),
    # §6.6's shared LLM settings (decisions 324, 325); nothing secret.
    "llm": (("cap_usd", "extraction_provider", "parallel", "parallel_providers", "passes"), ()),
}


def fields_of(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`FIELDS[name]`, or a refusal that names every connector there is."""
    try:
        return FIELDS[name]
    except KeyError:
        raise LookupError(
            f"no connector named {name!r}; the registered ones are {', '.join(sorted(FIELDS))}"
        ) from None


async def _load_state(
    name: str, conn: asyncpg.Connection, *, for_update: bool = False
) -> ConnectorState:
    """The generic read; an unreadable DEK degrades to the plaintext half, never an exception."""
    if for_update:
        await conn.execute("SELECT name FROM connector_config WHERE name = $1 FOR UPDATE", name)
    try:
        config, secret = await secrets.get_connector_secrets(conn, name)
    except secrets.SecretsUnreadable as exc:
        # ERROR on every read, for `load_jellyfin`'s reasons.
        log.error("connector %s secrets are unreadable: %s", name, exc)
        stored = await conn.fetchval(
            "SELECT config FROM connector_config WHERE name = $1", name
        ) or {}
        return ConnectorState(name=name, config=dict(stored), secrets_unreadable=True)
    return ConnectorState(name=name, config=dict(config), secrets=dict(secret))


async def _save_state(
    name: str, conn: asyncpg.Connection, *, unset: tuple[str, ...] = (), **fields: Any
) -> ConnectorState:
    """Merge a partial update into any connector but Jellyfin.

    None (or "" for a secret) keeps what is stored; undeclared names are refused before any write.
    `unset` removes declared settings only (decision 450); a secret cannot be unset (decision 452).
    """
    config_fields, secret_fields = fields_of(name)
    declared = config_fields + secret_fields
    unknown = sorted(set(fields) - set(declared))
    if unknown:
        raise ValueError(
            f"connector {name} has no field {', '.join(unknown)}; "
            f"it declares {', '.join(declared) or 'none'}"
        )
    refused = sorted(set(unset) - set(config_fields))
    if refused:
        raise ValueError(
            f"connector {name} cannot unset {', '.join(refused)}; only its settings are unset "
            f"({', '.join(config_fields) or 'none'}), and a secret is kept by an empty field"
        )
    both = sorted(key for key in unset if fields.get(key) is not None)
    if both:
        raise ValueError(f"connector {name} was asked to set and unset {', '.join(both)} at once")
    typed = {key: fields[key] for key in secret_fields if fields.get(key) not in (None, "")}
    if typed:
        # §2's refusal, before anything is written.
        settings().require_secrets_key()
    async with conn.transaction():
        # The row before the lock, as in `save_jellyfin`.
        await conn.execute(
            "INSERT INTO connector_config (name) VALUES ($1) ON CONFLICT (name) DO NOTHING", name
        )
        current = await _load_state(name, conn, for_update=True)
        config = dict(current.config)
        config.update({key: fields[key] for key in config_fields if fields.get(key) is not None})
        for key in unset:
            config.pop(key, None)
        if not typed:
            # Nothing new to seal: leave the sealed columns exactly as they are (M4.7 dd03).
            await secrets.put_connector_config(conn, name, config)
            return replace(current, config=config)
        # A typed credential may retire an unreadable DEK row; its other secrets are then gone.
        sealed = {**({} if current.secrets_unreadable else current.secrets), **typed}
        await secrets.put_connector_secrets(conn, name, config, sealed, retire_unreadable=True)
        return ConnectorState(name=name, config=config, secrets=sealed)


async def _probe_provider(name: str, conn: asyncpg.Connection) -> dict[str, Any]:
    """A provider card's test button: the provider's free models-list read (decision 433)."""
    state = await load_connector(conn, name)
    if state.secrets_unreadable:
        return {"ok": False, "error": SECRETS_UNREADABLE_REASON}
    key = str(state.secrets.get("api_key") or "")
    if not key:
        return {"ok": False, "error": f"no API key is configured for {name}"}
    from spielplan.llm import client

    async with client.open_fetcher(conn) as fetcher:
        return await client.probe(fetcher, name, key=key, model=state.config.get("model") or None)


async def _probe_source(name: str, conn: asyncpg.Connection) -> dict[str, Any]:
    """A source card's test button: `connectors/probes` for TMDB, OMDb or Trakt (decision 453)."""
    from spielplan.connectors import probes
    from spielplan.llm import client

    return await probes.PROBES[name](conn, open_fetcher=client.open_fetcher)


# Each card's test button (decision 433); Jellyfin's is `api/admin.test_jellyfin`, which stores
# §7.1's verdict as it tests.
TESTS: dict[str, Callable[[asyncpg.Connection], Awaitable[dict[str, Any]]]] = {
    **{name: partial(_probe_source, name) for name in ("tmdb", "omdb", "trakt")},
    **{name: partial(_probe_provider, name) for name in ("gemini", "anthropic", "openai")},
}


async def load_connector(
    conn: asyncpg.Connection, name: str, *, for_update: bool = False
) -> ConnectorState:
    fields_of(name)
    return await _load_state(name, conn, for_update=for_update)


async def save_connector(
    conn: asyncpg.Connection, name: str, *, unset: Iterable[str] = (), **fields: Any
) -> ConnectorState:
    return await _save_state(name, conn, unset=tuple(unset), **fields)


async def test_connector(conn: asyncpg.Connection, name: str) -> dict[str, Any]:
    """The one dispatch behind every card's test button; no test is refused by name (404, decision 433)."""
    test = TESTS.get(name)
    if test is None:
        fields_of(name)
        raise LookupError(f"connector {name} has no test in this build")
    return await test(conn)


__all__ = [
    "FIELDS",
    "JELLYFIN",
    "SECRETS_UNREADABLE_REASON",
    "TESTS",
    "ConnectorState",
    "JellyfinConfig",
    "delta_since",
    "env_seeds",
    "make_client",
    "load_connector",
    "load_jellyfin",
    "save_connector",
    "save_jellyfin",
    "fields_of",
    "seed_from_env",
    "test_connector",
]
