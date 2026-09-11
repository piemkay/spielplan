"""FastAPI application. Spec v2.1 §1, §2, §3.1.

First boot is a defined sequence and **a bundle-less app is a legal state** (§3.1): the app
boots with `/data/artifacts` and `artifact_bundle` empty, serving the setup wizard and admin
routes, and artifact-dependent surfaces render an explicit "no bundle imported" state instead
of erroring. Nothing in startup may raise because a bundle is missing.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import ssl
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Response
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import Scope

from spielplan.api import admin as admin_api
from spielplan.api import artifacts as artifacts_api
from spielplan.api import auth as auth_api
from spielplan.api import home as home_api
from spielplan.api import library as library_api
from spielplan.api import passkeys as passkeys_api
from spielplan.api import push as push_api
from spielplan.api import rank as rank_api
from spielplan.api import rate as rate_api
from spielplan.api import setup as setup_api
from spielplan.api import state as state_api
from spielplan.api import tonight as tonight_api
from spielplan.api.deps import carry_slid_session_cookie
from spielplan.connectors import registry
from spielplan.core import secrets
from spielplan.core.config import Settings, settings
from spielplan.db import migrate, pool
from spielplan.ledger import hyperparams
from spielplan.models.artifacts import ArtifactStore
from spielplan.push import keys as push_keys
from spielplan.scoring import backbone

# The backend process configured no logging at all, so nothing this module reports was visible:
# uvicorn's `dictConfig` leaves the root logger at WARNING with no handlers, `spielplan` inherits
# that, and INFO records were dropped while WARNING/ERROR fell through to `logging.lastResort` —
# the message alone, with no timestamp, level or logger name. A real boot that applied every
# migration printed none of `lifespan`'s lines. That contradicts two clauses at once: §3.1 makes a
# bundle-less boot an explicitly *reported* state, and §6.6 names logs as operator data. It also
# blocks the M4.7 upgrade runbook, which tells the operator to confirm an upgrade by grepping
# `docker compose logs backend` for "applied migrations" (decision 181).
#
# Deliberately the same call `worker.py:23-25` makes rather than a `--log-config` file: one format
# across both entrypoints and one fewer artefact to drift. ASCII only (CLAUDE.md). It coexists with
# uvicorn's own configuration — `dictConfig` sets no root handlers and `disable_existing_loggers`
# is False, so uvicorn's loggers keep their formatting and this one adds the missing root handler.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
)
log = logging.getLogger("spielplan")

# The errnos that mean "this host could not be reached", for the handler below. Python raises a
# named `ConnectionError` subclass for a peer that answered with a refusal, a reset or a broken
# pipe; a peer or a network that could not be reached *at all* arrives as a plain `OSError` with
# one of these in `errno`, and there is no class to register for it. Keyed on the errno rather
# than widened back to `OSError` because the exclusion the class list was narrowed for still
# holds: `PermissionError` (13) and a full disk (28) come from `/data/artifacts`, not from
# Postgres, and answering those "database unreachable" points the operator at the one service
# that is working. [M4.7 ops-12, sec-08, cycle 2 finding 3]
_UNREACHABLE_ERRNOS = frozenset({
    errno.EHOSTUNREACH,   # no route to this host
    errno.ENETUNREACH,    # no route to this network
    errno.ENETDOWN,       # the local interface is down
    errno.EHOSTDOWN,      # the peer is down
    errno.ENETRESET,      # the network dropped an established connection
})

# The health probe's own bound, applied to each step it takes and not to the route as a whole.
# There are three steps, not two: the acquire, the ping, and the release. asyncpg defaults
# `Pool.release`'s timeout to the one the acquire was given (`pool.py:929`), and against a peer
# that has stopped answering it spends a whole one waiting for the out-of-band query cancel before
# it terminates the connection (`pool.py:221-232`). So a probe holds its pooled connection for up
# to twice this constant, not once, and answers after up to three times it — measured at 4.0 s for
# the one probe that got a connection against a frozen Postgres, and 6 s worst case for a probe
# that had to wait for a peer to let go of the last one. An earlier version of this comment called
# that "well inside" the 5 s the image's `HEALTHCHECK --timeout=5s` gives curl; it is not, and in
# that corner the check fails as a truncated request rather than as this route's answer. The
# constant is unchanged because what it should be is a judgement about how long a probe may wait
# on a busy pool — the owner's, with the corpus numbers — and not a number to move while
# correcting the arithmetic beside it. Later milestones reasoning from it should read the 3x.
# [M4.7 ops-02, dd-health-probes; cycle 3 finding 8]
_HEALTH_TIMEOUT_S = 2


async def _report_secret_custody(conn: asyncpg.Connection, cfg: Settings) -> None:
    """Say at boot whether SECRETS_KEY still opens the stored DEK. Never raise.

    The whole of dd03 was invisible until a member tapped something: the container came up, the
    health check passed, login worked, and the first seen-state write answered 500. This probe
    moves the discovery to the log line an operator reads right after `docker compose up`, which
    is the only moment they are looking. It cannot be a refusal — §3.1 keeps a half-configured
    boot legal, and refusing here would turn a recoverable custody problem into an app that will
    not start and so cannot be repaired through its own admin UI. [M4.7 dd03; decision 181]
    """
    if not cfg.secrets_key:
        return
    key_id = await secrets.active_key_id(conn)
    if key_id is None:
        return
    try:
        await secrets.load_dek(conn, key_id)
    except secrets.SecretsUnreadable as exc:
        log.error(
            "SECRETS_KEY does not open the active data-encryption key (%s): %s "
            "Connector credentials stay sealed; member writes still commit and report it.",
            key_id,
            exc,
        )


def _multi_worker_setting() -> str | None:
    """The settings uvicorn and gunicorn honour to fork a second app process, if any is set.

    `WEB_CONCURRENCY` is the environment variable both servers read as their default worker
    count; `--workers`/`-w` on the command line is the explicit form. Nothing else needs
    detecting, because nothing else forks this app.
    """
    concurrency = os.environ.get("WEB_CONCURRENCY", "").strip()
    if concurrency.isdigit() and int(concurrency) > 1:
        return f"WEB_CONCURRENCY={concurrency}"
    argv = sys.argv[1:]
    for index, token in enumerate(argv):
        if token.startswith("--workers="):
            value = token.split("=", 1)[1].strip()
            if value.isdigit() and int(value) > 1:
                return token
        elif token in ("--workers", "-w"):
            value = argv[index + 1].strip() if index + 1 < len(argv) else ""
            if value.isdigit() and int(value) > 1:
                return f"{token} {value}"
    return None


def _refuse_multiple_workers() -> None:
    """This app is single-process by construction, so say so at the only moment it can be fixed.

    Five pieces of state are process-global and correct only while there is one process:
    Tonight's lobby (`api/tonight.py`'s `HUB`), §6.7's rail buffers (`home/rail.py`'s `_BUFFERS`
    and `_SEQ`), the push in-flight set (`push/send.py`), and the Backbone and Cold Tower caches.
    The shipped CMD starts one uvicorn worker, which is what makes them correct — and nothing
    anywhere enforced it. A `--workers 2` an operator adds for a Friday night silently splits the
    household into two lobbies and two rails: half the phones join a session the other half cannot
    see, and the rail a member turns on shows the writes that happened to land in their process.
    Every one of those failures looks like a product bug and none of them logs anything.

    A refusal rather than a warning, and at `create_app` rather than in the lifespan, because both
    forms of the setting produce a *child* process that imports this module: raising here stops
    each child with the reason, which is the only place an operator will see it. It is not §3.1's
    territory — a half-configured boot is legal, but a boot that will corrupt shared state is a
    different kind of thing, and the config validator in `core/config.py` already establishes that
    refusing at construction is how this codebase handles a setting that cannot be honoured.

    The static guard on the Dockerfile CMD is the other half (M4.7's guards step): this catches
    the operator who changes how the container is started, that catches the change to the image.
    [M4.7 arch-09; decision 181]
    """
    setting = _multi_worker_setting()
    if setting is None:
        return
    raise RuntimeError(
        f"{setting} asks for more than one app process, and this app is single-process by "
        "construction. Tonight's lobby, the transparency rail buffers (spec section 6.7) and the "
        "push in-flight set are process-local, so a second worker splits one household into two "
        "lobbies and two rails with nothing in the log to say so. Give the box more CPU rather "
        "than the process more workers; see README, Shape."
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = settings()
    await pool.open_pool(cfg.database_url)

    async with pool.acquire() as conn:
        applied = await migrate.apply_all(conn)
        # Unconditionally, and with the same wording on both branches, because this line is an
        # instrument: README's Recovery and Upgrade blocks and `docs/TESTING.md`'s release
        # checklist all read the schema state off `docker compose logs backend | grep 'applied
        # migrations'`. Logged only `if applied`, a restore of a dump from the running image —
        # the case those blocks are written about, and the one M4.7's own exit criterion
        # measures — printed nothing at all, which is byte-for-byte what the operator also gets
        # from a backend crash-looping on a config refusal, from a mistyped service name, and
        # from the logging regression this milestone's step 1 exists to prevent. An instrument
        # whose healthy reading is indistinguishable from a broken instrument reports nothing.
        # `db/migrate._main` — `spielplan-migrate`, added by this same milestone — has carried
        # the branch from the start; this is the path the runbook actually greps.
        # [M4.7 ops-09, cycle 2 findings 1 and 8; decision 181]
        log.info("applied migrations: %s", ", ".join(applied) or "(none pending)")
        # §2: "env vars may *seed* connector config on first boot for automated installs."
        # First boot only — a connector that already has a row is left alone, so the admin UI
        # stays the source of truth the owner asked for.
        await registry.seed_from_env(conn, cfg)
        # §2: "A web-push VAPID keypair is generated at first boot and stored the same way."
        # Here rather than on first use: the pair the browser subscribes against must exist
        # before the member's onboarding screen asks for it, and a pair minted later would
        # differ from the one the phone already holds. Returns None without SECRETS_KEY —
        # §3.1 makes a half-configured boot legal, and the wizard is what fixes it.
        if await push_keys.ensure_keypair(conn) is None:
            log.info("no web-push keypair — prompts fall back to §6's in-app banner")
        await _report_secret_custody(conn, cfg)
        # §4.3: artifacts load "when present"; an empty store is legal.
        app.state.artifacts = await ArtifactStore.load_active(conn, cfg.artifacts_dir)
        # §5.1's basis, loaded once per process. §10 restarts on a bundle swap, so a process
        # never has to reload it — and a Backbone that fails to load degrades the scoring
        # surfaces rather than stopping a boot the admin needs in order to fix the bundle.
        # §4.3's constants ride with the basis, and for the same reason: §10 makes a bundle swap a
        # restart, so one read per process is all the spec ever asks for. Every Ledger router had
        # a `_hyperparams` fallback that re-read the file per request — a read, a `from_mapping`
        # validation and a note list on every tap of Rate, every board GET and every duel — and
        # `load_cache` then re-digested the result. One attribute here is the whole repair, and
        # the notes are logged once instead of being thrown away at DEBUG on every request:
        # §4.3's provenance ("a number from a default and the same number from a bundle mean
        # different things when someone is reading a refit report") is a boot fact.
        #
        # Ordered after the basis on purpose. `from_mapping` raises `ValueError` on a constant
        # outside its range, and the attribute is then left UNSET so the routers refuse with a
        # 503 rather than fit against silently substituted defaults — a different `hp_digest`
        # invalidates every cached fit in the install, which is a worse failure than a refusal.
        # Were the constants read first, that same ValueError would skip the basis and degrade
        # the scoring surfaces for a reason that has nothing to do with them. The boot itself is
        # not refused: §3.1 keeps a half-configured boot legal and the admin needs these routes
        # in order to import a bundle that parses. [M4.10 finding 10; ml06, perf-07]
        #
        # Two `try` blocks and not one, which is the whole of the ordering argument above and none
        # of its cost. `BackboneError` is a `RuntimeError`, so in one shared block an unusable
        # basis — a state the comment three lines up declares supported — took the first handler
        # and the constants read never ran: `app.state.hyperparams` stayed unset and all three
        # routers went back to reading, validating and re-digesting the file on every request,
        # which is the per-request cost (perf-07) this read exists to delete, with §4.3's
        # provenance notes never logged either. `OSError` joins `ValueError` on the second because
        # `hyperparams.load` now lets a present-but-unopenable constants file reach `read_text`
        # rather than reading it as an absent one, and a boot that dies on it would be the one
        # outcome §3.1 forbids here. [M4.10 cycle 1, M410-R1-02 / M410-R1-04 / M410-C1-HP-1]
        try:
            app.state.backbone = backbone.load_for(app.state.artifacts)
        except backbone.BackboneError:
            log.exception("backbone.npz is unusable — serving without collaborative scores")
            app.state.backbone = backbone.Backbone.empty()
        try:
            hp, notes = hyperparams.load(app.state.artifacts)
            app.state.hyperparams = hp
            for note in notes:
                log.info("hyperparameters: %s", note)
            # THE YARDSTICK, ONCE, BESIDE THE CONSTANTS IT IS READ WITH. Spec section 14's first
            # risk states its own mitigation as "expectations instrumented, not assumed", and
            # `user_vector.cv_rho` was neither: the fold-in computes a held-out Spearman per
            # (user, kind), stores it, and nothing in the app knew what a good one looks like. The
            # corpus ships the reference - `cold_eval.json`'s cold and ceiling figures - and this
            # is the process's one statement of it, at INFO like the constants above and for the
            # same reason: section 10 makes a bundle swap a restart, so the pair is a boot fact
            # and an operator reading `cv_rho` in a later log line has the scale in the same file.
            # The floor comes from `hp`, not from DEFAULTS, so a bundle that re-tunes the tie band
            # is reported with its own. [M4.13 step 35, cs-31]
            yardstick = app.state.artifacts.cold_eval()
            if yardstick is not None:
                log.info("fold-in rho reads against cold_eval.json: %s",
                         yardstick.line(floor=hp.rho_noise_floor))
            elif not app.state.artifacts.is_empty:
                log.info(
                    "bundle %s ships no cold_eval.json - a fitted cv_rho has no reference value "
                    "in this install (spec section 0 row 1, section 14 risk 1)",
                    app.state.artifacts.version,
                )
        except (ValueError, OSError):
            # `log.exception` so the key is named twice: once in the message the operator greps
            # for and once in the traceback that says which check refused it.
            log.exception(
                "ledger_hyperparams.json is unusable - the Rate and Rank surfaces will answer "
                "503 until the bundle is fixed (spec section 4.3)"
            )

    if app.state.artifacts.is_empty:
        log.info("no artifact bundle active — serving setup wizard and admin routes (§3.1)")
    try:
        yield
    finally:
        await pool.close_pool()


def create_app() -> FastAPI:
    _refuse_multiple_workers()
    cfg = settings()
    # The schema enumerated every admin and setup route with its body shapes, to anyone who asked:
    # both renderers and `/openapi.json` were registered before the SPA catch-all and behind no
    # auth, on a stack whose whole exposure story is "one plain-HTTP port behind the operator's
    # Traefik". That is the reconnaissance step for every other finding and it costs nothing to
    # close, because `create_app().openapi()` builds the schema without the route: the gating sweep
    # in `test_api_gating.py` and the harness comparison in `test_devstub_contract.py` read the
    # method, not the URL. `redoc_url` goes with it — a second renderer of the same document is the
    # same disclosure — and FastAPI registers neither renderer once `openapi_url` is None, so all
    # three are stated rather than left to that implication. Step 2's dev flag is the way back, for
    # the developer reading the schema on their own machine. [M4.7 sec-12; decision 181]
    dev = cfg.insecure_dev
    app = FastAPI(
        title="Spielplan",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if dev else None,
        redoc_url="/redoc" if dev else None,
        openapi_url="/openapi.json" if dev else None,
    )

    app.include_router(auth_api.router)
    app.include_router(passkeys_api.router)
    app.include_router(setup_api.router)
    app.include_router(artifacts_api.router)
    app.include_router(library_api.router)
    app.include_router(state_api.router)
    app.include_router(rate_api.router)
    app.include_router(rank_api.router)
    app.include_router(home_api.router)
    app.include_router(tonight_api.router)
    app.include_router(push_api.router)
    app.include_router(admin_api.router)

    @app.get("/api/health")
    async def health() -> Response:
        """Whether this process can reach Postgres, answered in the status code.

        All three consumers key on the code and none reads the body: the image's HEALTHCHECK is
        `curl -fsS ... || exit 1`, `.github/workflows/ci.yml`'s wait loop is the same shape, and
        `e2e/run.mjs` tests `res.ok`. A backend that cannot reach the database was therefore
        reported healthy by Docker, by CI and by the e2e harness, because the dict came back with
        `ok: false` and no `status_code`. The body is unchanged on purpose — `run.mjs` reads
        `.bundle` out of it and §3.1 makes a null bundle a legal, reported state.

        All three loops wait for success rather than for an answer, so a 503 while the database
        is still starting is read as "not ready yet" and costs one more iteration; nothing in the
        stack declares `depends_on: backend: service_healthy`, so an unhealthy backend blocks no
        other service's start either.

        Each step is bounded. `curl` gives up at 5 s but the server-side coroutine did not: it
        kept its pooled connection, so ten probes against a hung database took the pool to idle 0
        and the next real request never answered. There is deliberately no `command_timeout` on
        the pool to do this globally — the importer's COPY and the nightly refit legitimately run
        for minutes — and no separate `/api/ready`: one endpoint that tells the truth is what the
        three consumers already call. [M4.7 ops-02, dd-health-probes; decision 181]
        """
        try:
            async with pool.pool().acquire(timeout=_HEALTH_TIMEOUT_S) as conn:
                await conn.fetchval("SELECT 1", timeout=_HEALTH_TIMEOUT_S)
            db_ok = True
        except Exception:
            db_ok = False
        store: ArtifactStore = app.state.artifacts
        return JSONResponse(
            status_code=200 if db_ok else 503,
            content={
                "ok": db_ok,
                "role": cfg.role,
                # WHAT THIS PROCESS LOADED, which for a broken store is nothing. `is_empty`
                # rather than `.version`, because data-03 made those two answers differ: an
                # active row whose directory is gone loads carrying that row's version (so the
                # fit it refuses to make would have been stamped honestly) with `is_empty` still
                # True. Reading `.version` here therefore started reporting a bundle the process
                # cannot open one file of, on the one probe that is unauthenticated -- while
                # §6.6's Data tab, which owns this distinction, reports `loaded: null` beside
                # `active` and `broken` for the same store. Same vocabulary in both places: null
                # is a legal, reported state (§3.1), and "loaded nothing" is what it means.
                # [M4.13, data-03; cycle 2, M413-C2-D1-03]
                "bundle": None if store.is_empty else store.version,
                "public_url": cfg.public_url,
            },
        )

    # §3.2's sliding session cookie is re-issued by `deps.current_user`, which runs before the
    # route body and writes onto a Response that Starlette throws away the moment anything
    # raises. The row has already slid by then, so without these three the cookie and the row
    # disagree for the rest of the day (see `deps.carry_slid_session_cookie`). Each handler is
    # the framework default with that one header put back.
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc: StarletteHTTPException) -> Response:
        return carry_slid_session_cookie(request, await http_exception_handler(request, exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError) -> Response:
        handled = await request_validation_exception_handler(request, exc)
        return carry_slid_session_cookie(request, handled)

    @app.exception_handler(asyncpg.UniqueViolationError)
    async def _conflict(request, exc: asyncpg.UniqueViolationError) -> Response:
        """A uniqueness conflict is the client's news, not a server fault.

        Registered ahead of the general handler below in reading order, though the order is not
        what decides it: Starlette looks a handler up along `type(exc).__mro__`, and
        `UniqueViolationError` names itself before `PostgresError`, so the specific one wins
        wherever it is written. A test asserts that rather than trusting it.

        **This handler answers no finding of its own.** It is the seam M4.10 and M4.12 are told
        to plug into — the one place a race between two writers becomes a status the caller can
        act on instead of a 500 that says "database error" and loses the name of the constraint
        that fired. Recorded here, under `platform-http-error-seam`, rather than appearing
        unannounced inside a later product diff. A route that expects a particular conflict still
        catches it and says something better; this is the floor, and the WARNING is the note to
        whoever writes that route. [M4.7 plan §3 step 4; decision 181]
        """
        constraint = exc.constraint_name or "a uniqueness constraint"
        log.warning("unique violation reached the application handler: %s", constraint)
        return carry_slid_session_cookie(
            request,
            JSONResponse(status_code=409, content={"detail": f"conflict: {constraint}"}),
        )

    @app.exception_handler(asyncpg.PostgresError)
    async def _pg_error(request, exc: asyncpg.PostgresError) -> Response:
        log.exception("database error")
        return carry_slid_session_cookie(
            request, JSONResponse(status_code=500, content={"detail": "database error"})
        )

    @app.exception_handler(ConnectionError)
    @app.exception_handler(socket.gaierror)
    @app.exception_handler(TimeoutError)
    @app.exception_handler(ssl.SSLError)
    async def _db_unreachable(request, exc: OSError) -> Response:
        """The failure that has no `PostgresError` to be caught by, because there is no session.

        A refused connection, a name that does not resolve, an acquire that timed out and a TLS
        handshake that would not verify are all `OSError` family — `ConnectionRefusedError`,
        `socket.gaierror`, (3.11+) `TimeoutError` and `ssl.SSLError` — so none of them reached the
        handler above and every one of them escaped as a bare traceback. (A password Postgres
        itself rejects does not: `InvalidPasswordError` is a `PostgresError` and is the 500 above.
        What escapes is the attempt that never got far enough to be told.) First boot is when an
        install meets it: `.env.example` asks for the database password twice, once as
        `POSTGRES_PASSWORD` and once inside `DATABASE_URL`, and compose defaults each
        independently (ops-12) — and `backup/nightly.py` names a Postgres outside this compose
        file as supported, which is a host, a port and a certificate that can simply not be there.

        Named classes rather than one registration on `OSError`, because that is the family this
        handler is *in*, not the family it is named for. `PermissionError`, `FileNotFoundError`
        and a full disk are `OSError` too, and the likeliest of them arrives right here: §10's
        import stages a bundle with `shutil.copytree` into `/data/artifacts`
        (`importer/bundle.py:568`), which the image's `USER spielplan` cannot write on a host
        directory nobody chowned to uid 1000 — the failure README's `chown -R 1000:1000 data`
        line exists to prevent, and one this milestone made likelier by adding the `USER`
        directive at all (sec-08). Under a blanket handler that operator is answered "database
        unreachable" and the log says the same, on a stack whose database is fine; the one
        instrument that could have told them where to look now points at the wrong service.
        Starlette resolves a handler along `type(exc).__mro__`, so a named class wins over the
        errno handler below wherever both match — `ConnectionRefusedError` names `ConnectionError`
        first, and `SSLCertVerificationError` names `ssl.SSLError` first. `log.exception` keeps
        the traceback where it belongs, in the log the M4.7 logging step made visible.
        [M4.7 ops-12, sec-08; decision 181]
        """
        log.exception("database unreachable")
        return carry_slid_session_cookie(
            request, JSONResponse(status_code=503, content={"detail": "database unreachable"})
        )

    @app.exception_handler(OSError)
    async def _db_unreachable_by_errno(request, exc: OSError) -> Response:
        """The half of the same failure that has no class name to be registered under.

        Python raises a named `ConnectionError` subclass only where the peer answered: refused,
        reset, aborted, broken pipe. A host or a network that could not be reached at all —
        EHOSTUNREACH, ENETUNREACH, ENETDOWN, EHOSTDOWN, ENETRESET — is a *plain* `OSError`, a
        subclass of nothing above, so the four registrations above caught none of it and the peer
        going offline was a bare traceback where a refusal was a clean 503. That is not an exotic
        case for this app: §2's supported "Postgres outside this compose file" is reached over a
        network somebody's VPN, bridge or peer can drop, and asyncpg propagates the socket error
        unchanged out of `pool.acquire`.

        A re-raise rather than a wider net, because the exclusion the class list was narrowed for
        is the same one and it still holds. Starlette treats an exception raised by a handler as
        unhandled, so `PermissionError` on `/data/artifacts` and a full disk stay the 500 they
        should be, carrying their own traceback rather than somebody else's diagnosis — and the
        one instrument that names the failing service keeps naming the right one.
        [M4.7 ops-12, sec-08; cycle 2 finding 3]
        """
        if exc.errno not in _UNREACHABLE_ERRNOS:
            raise exc
        return await _db_unreachable(request, exc)

    # §1: the SvelteKit PWA is a static build served by the backend. Absent in dev.
    static_dir = cfg.static_dir
    if static_dir and static_dir.is_dir():
        root = static_dir.resolve()
        app.mount("/_app", StaticFiles(directory=root / "_app"), name="assets")

        class SpaFallback(APIRoute):
            """The catch-all, declining the `/api` namespace at match time rather than in the
            handler.

            Refusing inside the handler was a GET-shaped refusal: the route is registered for
            GET, so a POST to an unrouted `/api/...` path was a *partial* match — path yes,
            method no — and Starlette answers a partial match with 405. A route deleted outright
            (`POST /api/setup/members`, decision 164) was therefore indistinguishable from a
            method mismatch on a route that exists, and only in the container, where
            `SPIELPLAN_STATIC_DIR` is set; under pytest, with no static build, the same request
            is a plain 404. Declining the namespace gives the API the routing it has when this
            route is absent: 404 where nothing is served, 405 only where a real route refuses
            the verb. The reason is unchanged — an unknown API path must not be answered with
            the app shell, because a client that gets HTML where it expected JSON fails in a
            much less obvious place.
            """

            def matches(self, scope: Scope) -> tuple[Match, Scope]:
                match, child = super().matches(scope)
                path = child.get("path_params", {}).get("path", "")
                if match is not Match.NONE and (path == "api" or path.startswith("api/")):
                    return Match.NONE, {}
                return match, child

        async def spa(path: str) -> FileResponse:
            """SPA fallback for client-side routes.

            The path arrives percent-decoded and un-normalised, so `..` segments reach here
            verbatim: `static_dir / "../../data/backups/dump.sql"` would escape the static root
            and this route has no auth in front of it. Resolve first, then refuse anything that
            does not land inside the root — the same containment `StaticFiles` does for /_app.
            """
            index = root / "index.html"
            if not path:
                return FileResponse(index)
            try:
                candidate = (root / path).resolve()
            except (OSError, ValueError):
                return FileResponse(index)
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(index)

        app.router.routes.append(SpaFallback("/{path:path}", spa, methods=["GET"]))

    return app


app = create_app()
