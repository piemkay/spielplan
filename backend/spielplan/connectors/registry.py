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

# The one sentence every degraded surface says, so the rail line, the seen-state response and
# §6.6's Connectors card cannot drift apart. It names the environment variable because that is
# the thing an operator can act on; a member reading it in the rail learns that their tap was
# kept and that the household's admin has something to fix. [M4.7 dd03]
SECRETS_UNREADABLE_REASON = "connector secrets unreadable (SECRETS_KEY)"


@dataclass(frozen=True)
class JellyfinConfig:
    """The Jellyfin connector as configured, with its secrets already opened."""

    url: str = ""
    # Both credentials are out of the repr, the way `push/keys.py` already writes a signing key
    # ("no accessor and no repr"): §14.3 calls the API key admin-equivalent on the whole media
    # server and the per-user tokens real credentials, and a default repr copies them into any
    # traceback or `log.debug("%s", cfg)` that happens to hold a config. `url` stays visible,
    # because that is what makes such a line worth having.
    api_key: str = field(default="", repr=False)
    library_ids: list[str] = field(default_factory=list)
    # §7.3: per-user access tokens obtained at link time, keyed by *app* user id as a string
    # because JSON object keys are strings and round-tripping them as ints invites a silent
    # type mismatch between "3" and 3.
    user_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    # §7.1's pin, probed and stored rather than computed and thrown away. It lives in the config
    # half — it is not a secret and needs no migration — and it is what gates the Played write
    # (`JellyfinClient.played_write_refusal`), because an advisory pin let a 10.8 install 404
    # every write for ever while every visible check passed. `None` is "nobody has probed yet",
    # which has to stay distinguishable from "probed, and refused".
    server_version: str = ""
    server_supported: bool | None = None
    # Not "unconfigured": the row exists and its credentials are real, they just cannot be
    # opened with this SECRETS_KEY. Distinguishing the two is the whole point — the first is a
    # household that has not set up Jellyfin, the second is a household whose admin has to
    # restore an env file, and only one of them should be told so (§2, §3.3).
    secrets_unreadable: bool = False

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
        try:
            await secrets.put_connector_secrets(conn, name, config, secret or None)
        except secrets.SecretsUnreadable as exc:
            # A custody failure is reported here, never raised. `app.py`'s lifespan calls this
            # before anything else, so the exception escaped the lifespan and the container never
            # started — with `restart: unless-stopped` looping it, and the repair the README names
            # (`docker compose exec backend spielplan-secrets reset`) needing a container that is
            # up. §3.1 makes a half-configured boot legal and `_report_secret_custody` one frame
            # later already establishes that this is the shape custody failures take at boot.
            #
            # Skipped rather than repaired: retiring the operator's DEK row is an explicit admin
            # gesture (`ensure_dek`'s `retire_unreadable`), and doing it unattended at every
            # restart would spend on an env variable the one repair that costs the household
            # every ciphertext the correct .env would still have opened.
            #
            # M4.7 is what made this reachable: before step 8 none of the six seed variables
            # arrived in a container at all, and `x-app-env` now forwards every one — so an
            # operator restoring a dump on a box whose .env carries any of them meets it.
            # [M4.7 dd03, ds01; decision 181]
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


def make_client(cfg: JellyfinConfig):
    """Build the Jellyfin client for a stored configuration, or None if there is none.

    One construction site, called module-qualified everywhere, so an integration test can point
    the whole app at `ops/fake_jellyfin.py` by replacing this single function instead of
    threading a transport through every route.
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

    §3.3 makes the app-side write independent of Jellyfin and §3.1 makes a half-configured boot
    legal, so an unreadable DEK must not be able to take a route down. This function is on the
    path of six routes (`api/state`, `api/rate` x4, `api/admin`) and until M4.7 the `InvalidTag`
    under it answered 500 on all of them — including `PUT /api/admin/connectors/jellyfin`, the
    one route that could have repaired custody, which made the failure unrecoverable from the UI
    (dd03). Degrading to an unconfigured-looking config with `secrets_unreadable` set keeps the
    stored URL (the admin needs to see what is configured) and drops the credentials, so
    `configured` is False, `make_client` returns None and every caller takes the path it already
    has for "Jellyfin is not set up" — with a different reason attached.

    `for_update` locks the connector row for the rest of the caller's transaction, which is how a
    read-modify-write of the sealed `user_tokens` map is serialised (§14.3, §7.3): the secret is
    AEAD-encrypted, so the merge cannot be expressed in SQL, and two members signing in on two
    phones otherwise read the same map and the second write drops the first token — leaving an
    account the admin table calls linked that cannot write a thing. Only meaningful inside a
    transaction (autocommit releases the lock with the statement), and it locks nothing when the
    row does not exist yet, which is why every caller that passes it follows a configured
    connector.
    """
    if for_update:
        await conn.execute(
            "SELECT name FROM connector_config WHERE name = $1 FOR UPDATE", JELLYFIN
        )
    try:
        config, secret = await secrets.get_connector_secrets(conn, JELLYFIN)
    except secrets.SecretsUnreadable as exc:
        # ERROR, and on every read rather than once per process: §6.6 names logs as the
        # operator's data, this is not transient, and de-duplicating it would need process state
        # that a second uvicorn worker or the worker container would not share. At household
        # scale a line per tap is the right volume; `app.py`'s boot probe is what makes the first
        # one arrive before anybody taps anything.
        log.error("jellyfin connector secrets are unreadable: %s", exc)
        stored_url = await conn.fetchval(
            "SELECT config ->> 'url' FROM connector_config WHERE name = $1", JELLYFIN
        )
        return JellyfinConfig(url=str(stored_url or ""), secrets_unreadable=True)
    tokens = secret.get("user_tokens") or {}
    supported = config.get("server_supported")
    return JellyfinConfig(
        url=str(config.get("url") or ""),
        api_key=str(secret.get("api_key") or ""),
        library_ids=list(config.get("library_ids") or []),
        user_tokens={str(k): str(v) for k, v in tokens.items() if v},
        server_version=str(config.get("server_version") or ""),
        server_supported=None if supported is None else bool(supported),
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
) -> JellyfinConfig:
    """Merge a partial update into the stored connector.

    Partial on purpose: the admin UI shows the API key as a masked placeholder and posts the
    URL without it, and a whole-row write would blank the key every time someone corrects a
    typo in the address.

    One transaction, with the row locked for the read, because this merge is the read-modify-write
    §14.3 cannot afford to lose: the sealed `user_tokens` map is opened here, changed in Python
    and sealed again, so two concurrent links otherwise read the same map and the loser's token
    disappears into an account that still reads "linked". `load_jellyfin(for_update=True)` argues
    why the lock is the row and not an advisory key.

    `server_version` and `server_supported` move as a pair — a version with no verdict, or a
    verdict with no version, is a refusal that cannot name itself (§7.1) — and a `None` version
    means "this save is not a probe", which carries the stored pair forward untouched.
    """
    async with conn.transaction():
        current = await load_jellyfin(conn, for_update=True)
        next_url = (url if url is not None else current.url).rstrip("/")

        # §14.3: the API key is admin-equivalent on the whole media server, and the per-user tokens
        # are real credentials. They are bound to the server they were issued by, so pointing the
        # connector at a different origin must not carry them along — every later request would
        # send them, in a header, to whatever host was just typed in. Changing the port or the
        # scheme counts: it is a different server until someone says otherwise by re-entering.
        moved = bool(current.url) and _origin(next_url) != _origin(current.url)
        if moved:
            log.info("jellyfin origin changed — stored credentials dropped, re-entry required (§14.3)")
        if moved:
            # §14.3 drops the tokens; §7.3 makes the link badge the only signal the admin has.
            # Until M4.11 every account kept reading 'linked' with nothing left to write with, and
            # by the sweep's own rules that is the state which stopped a member at their first
            # owed row and never recovered. The badge is the truth of the tokens, so it changes in
            # the same act — and the same transaction — that drops them.
            #
            # On `moved` alone, and NOT on `moved and current.user_tokens`. Under M4.7's dd03 —
            # a restored dump under a changed or missing SECRETS_KEY — `load_jellyfin` degrades to
            # a config with the stored URL and an EMPTY token map, so the one state in which the
            # app cannot see the credentials it is about to destroy was the one state that skipped
            # this UPDATE: the same save fell through to `put_connector_secrets(secret=None,
            # retire_unreadable=True)` and wrote NULL over the ciphertext while every account kept
            # reading 'linked', with no remedy from either side (`sync_all` returns
            # `skipped_no_link` on an unconfigured connector, so the sweep's own `_mark_needs_relink`
            # is unreachable). The guard bought nothing in exchange: 'linked' is only ever written
            # where a token was stored (`api/admin.py`, and the sweep's promotion on `report.wrote`),
            # so with no tokens held this statement matches no row.
            # [M4.11 review cycle 2: m411-rev2-jf-02; §14.3, §7.3, M4.7 dd03]
            marked = await conn.execute(
                "UPDATE app_user SET jellyfin_link_state = 'needs_relink' "
                "WHERE jellyfin_user_id IS NOT NULL AND jellyfin_link_state = 'linked'"
            )
            if not marked.endswith(" 0"):
                log.info("jellyfin origin changed -- linked accounts need a re-link (%s)", marked)

        # An unreadable current row is the same merge problem as `moved`, one layer down: there is
        # nothing to carry forward, because nothing could be read. Treating it as "keep what is
        # stored" would seal an empty api_key over a real one and call it a save.
        #
        # A merge fact and nothing more. M4.7 also derived the *custody repair* from it, and that was
        # wrong in the state where this connector has no ciphertext at all: `load_jellyfin` can only
        # report `secrets_unreadable` for a row that already holds one, so on an install whose DEK
        # was minted for the VAPID pair alone the repair never armed and the PUT 500ed. The repair is
        # `ensure_dek`'s to make, because it belongs to the DEK rather than to one connector's stored
        # bytes. [M4.7 dd03]
        lost = current.secrets_unreadable

        merged = JellyfinConfig(
            url=next_url,
            api_key=api_key if api_key else ("" if moved or lost else current.api_key),
            library_ids=library_ids if library_ids is not None else current.library_ids,
            user_tokens=(
                user_tokens
                if user_tokens is not None
                else ({} if moved or lost else current.user_tokens)
            ),
            # A verdict belongs to the server that answered the probe. Carrying 10.8's
            # "unsupported" across a move would refuse every Played write against a fresh 10.10
            # install (§7.1), so the pair is dropped with the credentials and the sweep's own
            # re-probe fills it in again.
            server_version=(
                server_version if server_version is not None
                else ("" if moved else current.server_version)
            ),
            server_supported=(
                server_supported if server_version is not None
                else (None if moved else current.server_supported)
            ),
        )
        # A URL on its own is not a secret, and saving one must not demand SECRETS_KEY — the admin
        # types the address first and pastes the key second.
        secret: dict[str, Any] | None = None
        if merged.api_key or merged.user_tokens:
            settings().require_secrets_key()
            secret = {"api_key": merged.api_key, "user_tokens": merged.user_tokens}
        stored_config: dict[str, Any] = {"url": merged.url, "library_ids": merged.library_ids}
        if merged.server_version:
            stored_config["server_version"] = merged.server_version
        if merged.server_supported is not None:
            # Absent rather than JSON null: "nobody has probed" is the absence of a verdict, and
            # an explicit null would have to be told from a stored `false` by every reader.
            stored_config["server_supported"] = merged.server_supported
        if lost and not moved and secret is None:
            # Correcting the URL on an install whose DEK will not open. There is no new secret to
            # seal and the stored one could not be read, so writing NULL over it would destroy a
            # ciphertext that is unreadable only until the right .env comes back (§2).
            #
            # `not moved`, because keeping the ciphertext is only right while it still belongs to the
            # server it is being kept for. A changed origin is the one save that has to fall through
            # to the NULL: `moved` means those credentials are dead to this install (§14.3), and
            # preserving them here would hand the old server's admin-equivalent key and every §7.3
            # token to whatever host was just typed in, on the day the correct .env came back — while
            # the log line above had already told the admin they were dropped. [M4.7 dd03]
            await secrets.put_connector_config(conn, JELLYFIN, stored_config)
            return merged
        # `retire_unreadable`: this is the PUT that re-enters the key, and it has to succeed or dd03's
        # failure has no cure but SQL — `ensure_dek` unwraps the active row before it can seal
        # anything, so the repair route died of the same error as the routes it exists to fix. An
        # admin typing a credential is the gesture that authorises retiring a row nothing can open,
        # whether or not this connector already held a ciphertext (§14.3, §2). [M4.7 dd03]
        await secrets.put_connector_secrets(
            conn, JELLYFIN, stored_config, secret, retire_unreadable=True
        )
        return merged


__all__ = [
    "JELLYFIN",
    "SECRETS_UNREADABLE_REASON",
    "JellyfinConfig",
    "env_seeds",
    "make_client",
    "load_jellyfin",
    "save_jellyfin",
    "seed_from_env",
]
