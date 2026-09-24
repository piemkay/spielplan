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

Every connector §6.6 draws a card for is one `ConnectorSpec` in `CONNECTORS`, read and written
through `load_connector` / `save_connector` and tested through `test_connector` (M5.5 plan A1-A4,
decision 433). Jellyfin's row is `load_jellyfin` and `save_jellyfin` themselves; the section at
the end of this module argues why.
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
    # §7.2's `POST /events/jellyfin` is "token-authed", and the token is this app's own rather
    # than anything Jellyfin issues: the operator pastes it into the Webhook plugin's header
    # field and the plugin presents it on every `ItemAdded` (decision 332). Sealed beside the two
    # credentials above and out of the repr for their reason -- whoever holds it can file
    # acquisition work in this household's name -- although it travels in the opposite direction,
    # which is the whole of `save_jellyfin`'s argument about what an origin change destroys.
    webhook_token: str = field(default="", repr=False)
    # §7.1's pin, probed and stored rather than computed and thrown away. It lives in the config
    # half — it is not a secret and needs no migration — and it is what gates the Played write
    # (`JellyfinClient.played_write_refusal`), because an advisory pin let a 10.8 install 404
    # every write for ever while every visible check passed. `None` is "nobody has probed yet",
    # which has to stay distinguishable from "probed, and refused".
    server_version: str = ""
    server_supported: bool | None = None
    # §7.2's fifteen-minute delta poll, as the one instant it reads from (decision 366) -- the
    # `MinDateLastSaved` it sends, since decision 409 made the server's save the whole delta. The
    # config half and never the sealed one: it is not a secret, and a value that vanished with a
    # SECRETS_KEY failure would re-read the corpus on the next poll.
    # Deliberately not named after `user_title.jf_synced_at`, which `0006_jellyfin.sql:9-11`
    # argues at length is a different thing per (user, title) -- "the app has an explicit action
    # Jellyfin has not seen" rather than "everything this server created after T". `None` is
    # "never polled", which `delta_since` turns into the instant this install gained the poll
    # (decision 412) and never into epoch.
    delta_watermark: datetime | None = None
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

    def webhook_token_matches(self, presented: str | None) -> bool:
        """§7.2's token check, as a rule in the domain package rather than in the route.

        An empty stored token matches nothing, which is the half a route cannot be trusted to
        remember. An install that has never minted one -- seeded from env, or configured before
        this milestone existed -- holds `""`, and a plain equality against a caller that also
        sends nothing would hand the intake path to anyone who found the URL. `compare_digest`
        because this is a bearer credential compared on every delivery (§14.3).

        OVER BYTES, which `core/auth.constant_time_equals` already spells this way and which this
        rule needs for a second reason: what arrives here is whatever a STRANGER sent. Starlette
        decodes a header as latin-1, so one byte >= 0x80 makes the left operand a non-ASCII `str`
        -- and `compare_digest` raises `TypeError` on those, which nothing in `app.py` catches.
        That answered 500 with a traceback to an unauthenticated caller on the one route §7.2
        opens to the world, breaking decision 365's "never 500" before the token check had even
        finished. Every latin-1 string encodes, so no header can raise here, and a token that is
        not ASCII simply fails to match. [review cycle 1: m52-rev-events-01]
        """
        return bool(self.webhook_token) and hmac.compare_digest(
            (presented or "").encode("utf-8"), self.webhook_token.encode("utf-8")
        )


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
    # §2's "Jellyfin, LLM, TMDB, OMDb, Trakt": the LLM half is each provider's key, sealed like
    # every credential above (M5.5 plan A3). The model is the admin card's to choose, and the
    # `llm` settings row is never seeded -- its cap is the one figure decision 325 refuses to ship,
    # and an env file supplying it would be that default arriving by another door.
    if cfg.gemini_api_key:
        seeds["gemini"] = ({}, {"api_key": cfg.gemini_api_key})
    if cfg.anthropic_api_key:
        seeds["anthropic"] = ({}, {"api_key": cfg.anthropic_api_key})
    if cfg.openai_api_key:
        seeds["openai"] = ({}, {"api_key": cfg.openai_api_key})
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


def _instant(raw: Any) -> datetime | None:
    """A stored ISO instant as an aware datetime, or None -- decision 366's watermark, read back.

    jsonb holds no timestamp type, so the value round-trips as text and this is where it stops
    being text: §7.2's poll compares it with the instant it began, and a string compare agrees with
    an instant compare only while both sides spell the precision identically. A value that will
    not parse -- hand-edited jsonb, or a dump from something that wrote its own -- reads as "never
    polled" rather than as an instant: that re-reads the library once, which the queue's
    `(kind, key)` identity absorbs, where a guessed floor could skip the household's adds for ever.
    """
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _new_webhook_token() -> str:
    """One token for §7.2's webhook (decision 332). `token_urlsafe` is the stdlib `secrets`, not
    `core.secrets` imported below -- 256 bits, and URL-safe because an operator pastes this value
    by hand into the Webhook plugin's header field and must not have to quote any of it."""
    return token_urlsafe(32)


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
        # The whole config half, and not the URL alone. What failed is the sealed column; the
        # plaintext one opened perfectly, and `save_jellyfin` rebuilds `stored_config` from the
        # config it was handed -- so every key this read dropped was ERASED by the next save of a
        # corrected URL, which is the one save this state invites. That was invisible while
        # `library_ids` had no reader. Decision 364 makes it the acquisition boundary, so the
        # erasure silently widens it from the libraries the admin picked to the whole server, and
        # decision 366's watermark would go with it and re-read the corpus. A custody failure is a
        # fact about the credentials (§2, M4.7 dd03); taking the connector's plaintext settings
        # down with them is a second failure that the first one does not justify.
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

    `delta_watermark` is §7.2's delta poll writing down where it got to (decision 366), so it is
    the poll's to pass and nobody else's: every other caller leaves it `None` and carries the
    stored instant forward. The webhook token is the opposite — no caller passes a VALUE, because
    this function is the only thing that ever mints one (decision 332).

    `watermark_origin` is the server that watermark was read FROM, and it is what keeps the move
    branch below true under a race. The poll's save names no URL, so `moved` was computed against
    whatever was stored at that instant -- and an admin who moved the connector while a poll was
    paging had the move's drop overwritten by an instant belonging to the OLD server's read, from
    which the new server's first poll then started, stepping over everything it held from before.
    A watermark whose origin is not the stored one is not written. [M5.2 review cycle 3:
    M52-C3-STATE-05; decision 366]

    `mint_webhook_token` is that mint's permission, and only `api/admin.put_jellyfin` holds it
    (decision 416). Decision 332 gives the token one appearance, on the save that made it, so a
    save nobody is watching may not be the one that makes it: the merge is reached by the delta
    poll's watermark write, by the sweep's version probe and by §7.3's link route, and on every
    install that arrives here already configured -- an upgrade, or `seed_from_env` -- the FIRST of
    those is a background job. It minted, sealed and returned the value into a worker that dropped
    it, after which the PUT's one-time reveal is `null` for ever while §6.6's card says a token
    exists. Gated on the gesture instead, a background save carries the stored token forward
    untouched and `has_webhook_token: false` keeps telling the admin to ask for one -- which the PUT
    does only when its body says so, not on every Save (decision 418).
    [review cycle 1: m52-rev-delta-01, m52-rev1-token-01]
    """
    async with conn.transaction():
        # The lock below locks a row, and `load_jellyfin` says so: "it locks nothing when the row
        # does not exist yet". That is exactly the save decision 332 mints on -- the first one --
        # so two simultaneous first saves each read no row, each minted their own token, and one
        # admin was shown a value the other's commit had already overwritten, with no rotation
        # path anywhere in the app to recover it. Taking the row first costs nothing: this call
        # writes one before it returns in any case (`put_connector_secrets` upserts), and a save
        # that raises rolls this back with the rest of the transaction.
        # [review cycle 1: m52-rev1-token-02; decision 332]
        await conn.execute(
            "INSERT INTO connector_config (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            JELLYFIN,
        )
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
        # The poll's watermark, refused when the read it describes was of another server than the
        # one this row now names -- see `watermark_origin` above.
        if watermark_origin is not None and (
            _origin(watermark_origin.rstrip("/")) != _origin(next_url)
        ):
            delta_watermark = None

        merged = JellyfinConfig(
            url=next_url,
            api_key=api_key if api_key else ("" if moved or lost else current.api_key),
            # THE ONE SERVER-BOUND VALUE HERE WITH NO `moved` BRANCH, and the omission is the
            # argument rather than the oversight the four fields below could make it look like.
            # §14.3 drops what this app would SEND to whatever host was just typed in, and the
            # version pair and the watermark are facts this app DERIVED from the old server. The
            # pick is neither: it is the admin's own recorded intent, it travels nowhere, and
            # `moved` fires on http->https, on a new port and on hostname->IP -- the same install
            # at a corrected address, whose library ids are GUIDs minted in its own database and
            # still match. Dropping it there would widen the boundary from the libraries the
            # admin picked to the whole server on the commonest gesture of the three, which is
            # the harm the custody branch above already refuses in these words: the household
            # billed for the library they deselected. What survival costs instead is bounded and
            # repairable -- a genuinely different install lists none of the stored ids, so its
            # adds stay pending, and both feeders name the stale ids, until the admin re-picks
            # from the list `GET /connectors/jellyfin/libraries` serves (decision 410; this said
            # "recorded `library not picked`", which is what the double did and no Jellyfin
            # does, since the server 400s a `ParentId` it cannot find [M5.2 review cycle 3:
            # M52-C3-LIB-02]) -- and it is the same answer M4.11 gave for every other Jellyfin id
            # this schema holds: `title.jellyfin_id` and the copy map are re-derived from a real
            # read (`prune_missing_items`), never erased on a URL edit.
            # [review cycle 1: m52-rev-lib-01; decision 364, §14.3]
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
            # A watermark belongs to the clock that stamped it. `DateLastSaved` is the SERVER's
            # instant for when it saved an item (decision 409), so carrying one across a move is the
            # same category error as carrying the version verdict, with a worse failure mode: a
            # new server whose library was imported before the old watermark would have every one
            # of its adds skipped, silently and for ever. Dropped on a move, the next poll falls
            # back to the floor `delta_since` names and re-reads the new server from there -- and
            # this comment used to say the queue's `(kind, key)` identity absorbs that re-read. It
            # absorbs nothing of it on a genuinely different install: the key is `jellyfin:<Id>`
            # and a re-imported library carries new ids. What absorbs it is decision 411: an item
            # that resolves to a title the bundle supplied and the app already placed is filed
            # below every genuine add and closed at stage 1 without walking, and a title the new
            # server holds that the app has never placed is filed as the add it is. That fails
            # loudly and cheaply, in queue rows, rather than quietly and permanently.
            # [M5.2 review cycle 3: M52-C3-STATE-06]
            delta_watermark=(
                delta_watermark if delta_watermark is not None
                else (None if moved else current.delta_watermark)
            ),
            # The one credential here that an origin change does NOT destroy, and the asymmetry is
            # the argument rather than an oversight. §14.3's rule above is about credentials this
            # app SENDS: the admin key and the §7.3 tokens would travel, in a header, to whatever
            # host was just typed in. The webhook token never leaves this install -- it is what a
            # caller must present to `POST /events/jellyfin` -- so nothing about a new Jellyfin
            # address puts it anywhere it was not already. Dropping it would cost what §6.6 cannot
            # give back: the value is displayed once at generation (decision 332), so an admin
            # correcting a port would silently kill the intake path with no surface that says so
            # and no way to re-read the token they had already pasted into the plugin. What
            # survival costs instead is bounded and visible -- the old server can still POST, and
            # every event it sends is filtered by the library pick and by §8's own gates.
            # `lost` is a different fact: nothing could be read, so there is nothing to carry.
            webhook_token="" if lost else current.webhook_token,
        )
        if mint_webhook_token and merged.configured and not merged.webhook_token:
            # Generated at the first save that leaves this connector configured, and never
            # rotated by a later one (decision 332): an operator who has pasted it into the
            # Webhook plugin must not have it changed under them by an unrelated edit to the URL.
            # `mint_webhook_token` is the gesture this milestone's own review added (decision
            # 416): the docstring above argues why the state is not enough to mint on.
            merged = replace(merged, webhook_token=_new_webhook_token())
        # A URL on its own is not a secret, and saving one must not demand SECRETS_KEY — the admin
        # types the address first and pastes the key second.
        secret: dict[str, Any] | None = None
        # `or merged.webhook_token` is what makes the survival above real rather than stated. The
        # sealed blob is written whole, so a move that leaves no api_key and no user_tokens would
        # otherwise fall through to the `secret=None` write below and put NULL over the ciphertext
        # -- destroying the webhook token on exactly the save this merge just argued must keep it,
        # while the comment two screens up still said it survived. What is re-sealed there is a
        # blob holding the token and nothing else: §14.3's credentials are already "" by then.
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
            # Absent rather than JSON null: "nobody has probed" is the absence of a verdict, and
            # an explicit null would have to be told from a stored `false` by every reader.
            stored_config["server_supported"] = merged.server_supported
        if merged.delta_watermark is not None:
            # Absent rather than null, for the reason one line up: "never polled" is the absence
            # of an instant, and `delta_since` is where that absence becomes a floor.
            stored_config["delta_watermark"] = (
                merged.delta_watermark.astimezone(UTC).isoformat()
            )
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


# The migration whose `applied_at` is the instant this install gained §7.2's fallback, spelled the
# way `db/migrate.py` keys `schema_migration`: the file's stem (decision 412).
_FALLBACK_MIGRATION = "0025_jellyfin_intake"


async def delta_since(conn: asyncpg.Connection, cfg: JellyfinConfig) -> datetime:
    """The instant §7.2's delta poll reads from: the stored watermark, or -- the first time --
    the instant this install gained the poll at all (decisions 366 and 412).

    Never epoch, and that is where this function started. The poll reads every row the server
    SAVED after this instant (decision 409), so an epoch floor selects the household's entire
    corpus on the very first poll and files an acquisition task for every title it already owns.

    AND NOT THE INSTALL'S OWN CREATION INSTANT EITHER, which is what decision 366 wrote and what
    this function returned until review cycle 3 measured the install it did not describe. Every
    install that exists meets M5.2 as an UPGRADE, and there `min(applied_at)` is when 0001 ran --
    months before this milestone -- so the first poll filed a task for every title the household
    had added since installing the app, each already resolved by M4.11's sweep, and every genuine
    add waited behind them. The floor is 0025's own `applied_at`: on a fresh install one migration
    run from `min(applied_at)`, on an upgrade the upgrade itself. What the household added before
    it is the full sweep's unmatched report's to hand on (decision 370), and stage 1 closes any
    re-offer of a title already placed without walking it (decision 411) -- this docstring used
    to name that exit before it existed. [M5.2 review cycle 3: M52-C3-STATE-02]

    Read here rather than carried on the config because it is a fact about the DATABASE and not
    about the connector: a caller with a config has not necessarily got a fresh row, and
    `load_jellyfin` is on the path of six routes that have no use for this query at all.
    """
    if cfg.delta_watermark is not None:
        return cfg.delta_watermark
    gained = await conn.fetchval(
        "SELECT coalesce((SELECT applied_at FROM schema_migration WHERE version = $1),"
        "                (SELECT min(applied_at) FROM schema_migration))",
        _FALLBACK_MIGRATION,
    )
    # A database with no applied migration has no `connector_config` table to have got here
    # through, and one without 0025 has no intake table either, so neither arm can be empty in
    # practice; if both ever were, now is the only floor that still cannot enqueue a corpus.
    return gained or datetime.now(UTC)


# --- §6.6's connectors, as one table (M5.5 plan A1, A2, A4) ----------------------------------
#
# §6.6 draws a card per connector -- Jellyfin, the LLM providers, TMDB, OMDb, Trakt -- each with
# settings, a secret and a test button, and §2 stores every one of them in `connector_config`.
# Until M5.5 this module could read and write exactly one. What follows names all of them and
# gives any of them one read and one write over the storage `core.secrets` already had, so a card
# M5.7 adds is a row here rather than a loader and a route of its own.
#
# JELLYFIN'S ROW IS ITS OWN TWO FUNCTIONS, BY IDENTITY (plan A2). The generic merge below knows
# nothing of §14.3's origin rule, of the watermark's origin (decision 366), of the library pick
# that survives a move (decision 364) or of the mint a save has to ask for (decisions 416, 418),
# and each of those is in the bodies above because a review found it missing. So the row points at
# `load_jellyfin` and `save_jellyfin` rather than wrapping them, `JellyfinConfig` stays the type
# their callers read, and every caller keeps calling them by name: becoming a row changes nothing
# a caller of Jellyfin can observe.


@dataclass(frozen=True)
class ConnectorState:
    """Any connector but Jellyfin, as stored: the plaintext half and the opened sealed half.

    Two dicts rather than a class per connector, because a provider card, a source card and the
    `llm` settings row differ only in the names they hold, and those names are declared once, by
    their `ConnectorSpec`. Jellyfin keeps `JellyfinConfig`: its callers read fields, and it is the
    one connector whose merge has rules of its own.
    """

    name: str
    config: dict[str, Any] = field(default_factory=dict)
    # `JellyfinConfig`'s rule and its reason: a provider key bills the household and a TMDB key is
    # an account's, and a default repr copies either into any traceback or `%r` log line that
    # happens to hold the state (§14.3). The name and the settings stay visible, because they are
    # what makes such a line worth having.
    secrets: dict[str, Any] = field(default_factory=dict, repr=False)
    # `JellyfinConfig.secrets_unreadable`, drawing the same line: a connector nobody has set up,
    # against one whose credentials exist and will not open under this SECRETS_KEY -- and only the
    # second tells the admin to restore an env file (§2, §3.3, M4.7 dd03).
    secrets_unreadable: bool = False


@dataclass(frozen=True)
class ConnectorSpec:
    """One of §6.6's connectors: how it is read, written and tested, and what it stores.

    `load(conn, *, for_update=False)` and `save(conn, **fields)` are the connector's read and its
    partial merge. `config_fields` and `secret_fields` are the names a save may carry, split by the
    half of the row §2 stores them in. `test` is the card's test button, or None where this build
    has none (decision 433). `seeded` says `env_seeds` offers the connector a first-boot seed, and
    `test_connector_registry.py` holds the flag to what `env_seeds` returns rather than trusting it.
    """

    name: str
    load: Callable[..., Awaitable[Any]]
    save: Callable[..., Awaitable[Any]]
    config_fields: tuple[str, ...]
    secret_fields: tuple[str, ...]
    test: Callable[[asyncpg.Connection], Awaitable[dict[str, Any]]] | None
    seeded: bool


async def _load_state(
    name: str, conn: asyncpg.Connection, *, for_update: bool = False
) -> ConnectorState:
    """The generic read, which degrades rather than raising when the sealed half will not open.

    `load_jellyfin` argues both halves and this is its answer for every other connector. The lock
    is the row, for the read-modify-write `_save_state` does under it. An unreadable DEK returns the
    whole plaintext half with `secrets_unreadable` set, never an exception: §3.1 makes a
    half-configured install legal, and a save rebuilt from an empty read would erase the admin's
    settings along with the credential that actually failed.
    """
    if for_update:
        await conn.execute("SELECT name FROM connector_config WHERE name = $1 FOR UPDATE", name)
    try:
        config, secret = await secrets.get_connector_secrets(conn, name)
    except secrets.SecretsUnreadable as exc:
        # ERROR and on every read, for `load_jellyfin`'s reasons: §6.6 names logs as the operator's
        # data, this is not transient, and de-duplicating it would need process state the worker
        # container does not share with the backend.
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

    Partial for `save_jellyfin`'s reason: a card shows a stored secret as a mask and posts it empty,
    and a whole-row write would blank the key every time the admin changed the model. So a config
    value of None, and a secret of None or "", keep what is stored. A name the spec does not declare
    is refused before anything is written, because a misspelt `apikey` would otherwise be stored in
    the plaintext half beside the model while the provider went on refusing the key the admin
    believes they saved.

    `unset` names declared SETTINGS to remove, under the same row lock, because None already means
    "keep" and decision 450's spend guard has to be able to say "back to the default": a model to
    `pricing.DEFAULT_MODELS`, a price override to the table, the extraction assignment to nobody.
    A secret cannot be unset -- an empty field keeps a key, and a route that could clear one would
    silently disconnect a provider mid-acquisition (decision 452) -- and neither can an undeclared
    name or a field this same call sets, which would be two answers to one question.
    """
    spec = CONNECTORS[name]
    declared = spec.config_fields + spec.secret_fields
    unknown = sorted(set(fields) - set(declared))
    if unknown:
        raise ValueError(
            f"connector {name} has no field {', '.join(unknown)}; "
            f"it declares {', '.join(declared) or 'none'}"
        )
    refused = sorted(set(unset) - set(spec.config_fields))
    if refused:
        raise ValueError(
            f"connector {name} cannot unset {', '.join(refused)}; only its settings are unset "
            f"({', '.join(spec.config_fields) or 'none'}), and a secret is kept by an empty field"
        )
    both = sorted(key for key in unset if fields.get(key) is not None)
    if both:
        raise ValueError(f"connector {name} was asked to set and unset {', '.join(both)} at once")
    typed = {key: fields[key] for key in spec.secret_fields if fields.get(key) not in (None, "")}
    if typed:
        # §2's refusal, before anything is written: a typed credential with no custody has nowhere
        # to be sealed, and `seed_from_env` argues why this is `settings()` and no argument.
        settings().require_secrets_key()
    async with conn.transaction():
        # The row before the lock, for `save_jellyfin`'s reason: `for_update` locks nothing when no
        # row exists, and the first save is exactly the one with none, so two first saves would each
        # merge onto an empty read. [review cycle 1: m52-rev1-token-02]
        await conn.execute(
            "INSERT INTO connector_config (name) VALUES ($1) ON CONFLICT (name) DO NOTHING", name
        )
        current = await _load_state(name, conn, for_update=True)
        config = dict(current.config)
        config.update({key: fields[key] for key in spec.config_fields if fields.get(key) is not None})
        for key in unset:
            config.pop(key, None)
        if not typed:
            # Nothing new to seal, so the sealed columns are left exactly as they are. On an install
            # whose DEK will not open that is what keeps a ciphertext which is unreadable only until
            # the right .env returns (§2, `save_jellyfin`'s last branch, M4.7 dd03); anywhere else it
            # is the stored secret carried forward without being re-sealed.
            await secrets.put_connector_config(conn, name, config)
            return replace(current, config=config)
        # A typed credential is the gesture that may retire a DEK row nothing can open, argued at
        # `save_jellyfin`'s last write (§14.3, §2, M4.7 dd03). What could not be read cannot be
        # carried, so an unreadable row's other secrets are gone once this seals over them.
        sealed = {**({} if current.secrets_unreadable else current.secrets), **typed}
        await secrets.put_connector_secrets(conn, name, config, sealed, retire_unreadable=True)
        return ConnectorState(name=name, config=config, secrets=sealed)


async def _probe_provider(name: str, conn: asyncpg.Connection) -> dict[str, Any]:
    """A provider card's test button: the provider's free models-list read (decision 433).

    The two answers known without a request are given without one -- no key typed yet, and a key
    this SECRETS_KEY cannot open, in the rail's own sentence. Otherwise the call is
    `spielplan.llm`'s, imported here the way `make_client` imports the Jellyfin client: the adapters
    are that package's and may read this module, and this module is on the path of every route that
    reads a connector while the adapters are wanted by one button. The request goes through the
    shared fetcher with the key in a header, which is `client.probe`'s to hold (§9).
    """
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
    """A source card's test button: `connectors/probes`' cheap request for TMDB, OMDb or Trakt
    (decision 453).

    Imported here rather than at the top, for `_probe_provider`'s reason: the probes read
    `sources/credentials` and the shared fetcher, and this module is on the path of every route
    that reads a connector while those are wanted by three buttons. The fetcher is
    `client.open_fetcher`, the one built outside a drain, looked up at the press so a test that
    replaces it for the provider buttons replaces it for these too.
    """
    from spielplan.connectors import probes
    from spielplan.llm import client

    return await probes.PROBES[name](conn, open_fetcher=client.open_fetcher)


def _stored(
    name: str,
    *,
    config_fields: tuple[str, ...] = (),
    secret_fields: tuple[str, ...] = (),
    test: Callable[[asyncpg.Connection], Awaitable[dict[str, Any]]] | None = None,
    seeded: bool,
) -> ConnectorSpec:
    """A connector the generic read and merge serve, bound to its own row by name."""
    return ConnectorSpec(
        name=name, load=partial(_load_state, name), save=partial(_save_state, name),
        config_fields=config_fields, secret_fields=secret_fields, test=test, seeded=seeded,
    )


# A provider's settings are its model and decision 343's admin price override (USD per 1M tokens,
# input and output); its one secret is the key, which travels in a header and nowhere else (§9).
_PROVIDER_FIELDS = ("model", "price_input", "price_output")

CONNECTORS: dict[str, ConnectorSpec] = {
    spec.name: spec
    for spec in (
        # No test here: `api/admin.test_jellyfin` stores §7.1's probed verdict as it tests, and it
        # stays that card's route (decision 433).
        ConnectorSpec(
            name=JELLYFIN, load=load_jellyfin, save=save_jellyfin,
            config_fields=(
                "url", "library_ids", "server_version", "server_supported", "delta_watermark",
            ),
            secret_fields=("api_key", "user_tokens", "webhook_token"),
            test=None, seeded=True,
        ),
        # The three keyed sources, whose adapters keep reading through `sources/credentials.py`
        # (decision 434); their test buttons are M5.7's source cards (decisions 433, 453). Trakt's
        # client id is config and not a secret, which is `env_seeds`' own split above.
        _stored("tmdb", secret_fields=("api_key",), test=partial(_probe_source, "tmdb"),
                seeded=True),
        _stored("omdb", secret_fields=("api_key",), test=partial(_probe_source, "omdb"),
                seeded=True),
        _stored("trakt", config_fields=("client_id",), secret_fields=("client_secret",),
                test=partial(_probe_source, "trakt"), seeded=True),
        _stored("gemini", config_fields=_PROVIDER_FIELDS, secret_fields=("api_key",),
                test=partial(_probe_provider, "gemini"), seeded=True),
        _stored("anthropic", config_fields=_PROVIDER_FIELDS, secret_fields=("api_key",),
                test=partial(_probe_provider, "anthropic"), seeded=True),
        _stored("openai", config_fields=_PROVIDER_FIELDS, secret_fields=("api_key",),
                test=partial(_probe_provider, "openai"), seeded=True),
        # §6.6's LLM settings, which belong to no one provider: the per-task assignment for the one
        # task M5 calls, parallel mode and its providers, the pass count (decision 324) and the
        # monthly cap (decision 325). Nothing here is secret, and nothing is seeded, because the
        # cap is the default decision 325 refuses to ship.
        _stored(
            "llm",
            config_fields=(
                "cap_usd", "extraction_provider", "parallel", "parallel_providers", "passes",
            ),
            seeded=False,
        ),
    )
}


def spec_for(name: str) -> ConnectorSpec:
    """The registered connector called `name`, or a refusal that names every one there is."""
    try:
        return CONNECTORS[name]
    except KeyError:
        raise LookupError(
            f"no connector named {name!r}; the registered ones are {', '.join(sorted(CONNECTORS))}"
        ) from None


async def load_connector(
    conn: asyncpg.Connection, name: str, *, for_update: bool = False
) -> JellyfinConfig | ConnectorState:
    """Any connector's stored state: `load_jellyfin(conn, for_update=...)` for Jellyfin, since that
    is its row's `load`, and the degrading generic read for every other."""
    return await spec_for(name).load(conn, for_update=for_update)


async def save_connector(
    conn: asyncpg.Connection, name: str, *, unset: Iterable[str] = (), **fields: Any
) -> JellyfinConfig | ConnectorState:
    """Any connector's partial merge: `save_jellyfin(conn, **fields)` for Jellyfin, since that is
    its row's `save`, and the generic merge for every other.

    One argument is refused on the way to Jellyfin, and it is the mint. Decision 416 gives
    `mint_webhook_token` to `api/admin.put_jellyfin` alone and decision 418 lets only a save that
    asks make the one appearance decision 332 allows, so a generic write that forwarded it would
    hand that gesture to whatever body a future card posts. Every other argument reaches
    `save_jellyfin` untouched, and its own signature refuses what it does not take.

    `unset` removes declared settings of any connector but Jellyfin (`_save_state`, decision 450),
    and is not forwarded when empty, so a save that names none is the call it always was. Jellyfin
    refuses it: its merge carries decision 364's library pick, where an absent pick and an empty
    one are different answers, and a generic removal is a third nobody argued for.
    """
    spec = spec_for(name)
    if spec.name == JELLYFIN and "mint_webhook_token" in fields:
        raise ValueError(
            "the jellyfin webhook token is minted only by the admin's own save "
            "(api/admin.put_jellyfin, decision 416), never through save_connector"
        )
    removed = tuple(unset)
    if not removed:
        return await spec.save(conn, **fields)
    if spec.name == JELLYFIN:
        raise ValueError(
            "the jellyfin connector unsets nothing through save_connector: its merge is"
            " save_jellyfin's, and decision 364's library pick is not a setting to remove"
        )
    return await spec.save(conn, unset=removed, **fields)


async def test_connector(conn: asyncpg.Connection, name: str) -> dict[str, Any]:
    """The one dispatch behind every card's test button (plan A4: "One dispatch table, not a route
    per provider"). A connector with no test in this build is refused by name rather than answered
    with a guessed probe, and the API maps the refusal to 404 (decision 433)."""
    spec = spec_for(name)
    if spec.test is None:
        raise LookupError(f"connector {name} has no test in this build")
    return await spec.test(conn)


__all__ = [
    "CONNECTORS",
    "JELLYFIN",
    "SECRETS_UNREADABLE_REASON",
    "ConnectorSpec",
    "ConnectorState",
    "JellyfinConfig",
    "delta_since",
    "env_seeds",
    "make_client",
    "load_connector",
    "load_jellyfin",
    "save_connector",
    "save_jellyfin",
    "seed_from_env",
    "spec_for",
    "test_connector",
]
