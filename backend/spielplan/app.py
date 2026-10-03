"""FastAPI application (§1, §2). A bundle-less app is a legal state (§3.1): nothing in startup may
raise because a bundle is missing.
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

from spielplan.api import acquisition as acquisition_api
from spielplan.api import admin as admin_api
from spielplan.api import art as art_api
from spielplan.api import artifacts as artifacts_api
from spielplan.api import auth as auth_api
from spielplan.api import curated as curated_api
from spielplan.api import events as events_api
from spielplan.api import flywheel as flywheel_api
from spielplan.api import home as home_api
from spielplan.api import ladder as ladder_api
from spielplan.api import library as library_api
from spielplan.api import llm as llm_api
from spielplan.api import mix as mix_api
from spielplan.api import passkeys as passkeys_api
from spielplan.api import push as push_api
from spielplan.api import rank as rank_api
from spielplan.api import rate as rate_api
from spielplan.api import setup as setup_api
from spielplan.api import state as state_api
from spielplan.api import taste as taste_api
from spielplan.api import tonight as tonight_api
from spielplan.api import wish as wish_api
from spielplan.api.deps import carry_slid_session_cookie
from spielplan.art.poster import ArtService, url_epoch
from spielplan.connectors import registry
from spielplan.core import logs, secrets, storage
from spielplan.core.config import Settings, settings
from spielplan.db import migrate, pool
from spielplan.models import basis
from spielplan.models.artifacts import ArtifactStore
from spielplan.push import keys as push_keys
from spielplan.rate import session as rate_session

# Without this, INFO lines are dropped: uvicorn leaves the root logger at WARNING with no handler.
logs.configure()
# The System card's log ring (§6.6). The worker's lines stay in its container log.
logs.install()
log = logging.getLogger("spielplan")

# A host or network that cannot be reached at all is a plain OSError with one of these errnos.
# Not every OSError: EACCES or ENOSPC from `/data` must not read "database unreachable".
_UNREACHABLE_ERRNOS = frozenset({
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ENETDOWN,
    errno.EHOSTDOWN,
    errno.ENETRESET,
})

# Per step (acquire, ping, release): asyncpg's release waits the same timeout again, so a probe can
# answer after ~3x this, past the image HEALTHCHECK's 5 s.
_HEALTH_TIMEOUT_S = 2

# Half of Docker's default 10 s stop grace, leaving the rest for the pool's close.
SETTLE_GRACE_S = 5


async def _report_secret_custody(conn: asyncpg.Connection, cfg: Settings) -> None:
    """Say at boot whether SECRETS_KEY still opens the stored DEK. Never raise (§3.1)."""
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
    """`WEB_CONCURRENCY` or `--workers`/`-w`: what makes uvicorn or gunicorn fork a second app process."""
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
    """Refused in create_app so every forked child dies with the reason: Tonight's lobby, the rail
    buffers, the push in-flight set and the model caches are process-global."""
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
        # Logged on both branches: the runbooks grep "applied migrations" to read the schema state.
        log.info("applied migrations: %s", ", ".join(applied) or "(none pending)")
        # After the migrations, so a fresh database's birth is the one it now has (`art/poster.url_epoch`).
        app.state.art_epoch = await url_epoch(conn)
        # First boot only (§2): a connector that has a row is left alone.
        await registry.seed_from_env(conn, cfg)
        # At boot, not on first use: the pair must exist before onboarding subscribes a phone (§2).
        # None without SECRETS_KEY, which §3.1 allows.
        if await push_keys.ensure_keypair(conn) is None:
            log.info("no web-push keypair — prompts fall back to §6's in-app banner")
        await _report_secret_custody(conn, cfg)
        # The one loader the re-pin also uses, so boot and a hot swap load the same things (decision 497).
        basis.pin(app.state, await basis.load(conn, cfg.artifacts_dir))

    if app.state.artifacts.is_empty:
        log.info("no artifact bundle active — serving setup wizard and admin routes (§3.1)")
    # Probed once at boot and kept for `/api/health`; a report, never a refusal (§3.1).
    app.state.storage = storage.Watch(cfg.data_dir, storage.BACKEND_MOUNTS)
    if (problem := storage.refusal(app.state.storage.result)) is not None:
        log.warning("storage: %s", problem)
    # Opened after every step that can fail; closed before the pool its reads borrow from.
    app.state.art = await ArtService(
        cfg.data_dir / "cache" / "art", egress=cfg.art_egress
    ).open()
    # Armed after the boot pin, so the first comparison is against what was loaded (decision 497).
    basis.start(app.state, cfg.artifacts_dir)
    try:
        yield
    finally:
        await basis.stop(app.state)
        app.state.storage.close()
        await app.state.art.close()
        # Lets handed-off Jellyfin pushes land before the pool closes; nothing is cancelled at the end.
        await rate_session.settled(timeout=SETTLE_GRACE_S)
        await pool.close_pool()


def create_app() -> FastAPI:
    _refuse_multiple_workers()
    cfg = settings()
    # The schema would enumerate every route to anyone: dev only. `openapi()` still works in process.
    dev = cfg.insecure_dev
    app = FastAPI(
        title="Spielplan",
        version="1.4.0",
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
    app.include_router(art_api.router)
    app.include_router(state_api.router)
    app.include_router(rate_api.router)
    app.include_router(rank_api.router)
    app.include_router(ladder_api.router)
    app.include_router(home_api.router)
    app.include_router(mix_api.router)
    app.include_router(taste_api.router)
    app.include_router(tonight_api.router)
    app.include_router(push_api.router)
    app.include_router(wish_api.router)
    app.include_router(admin_api.router)
    app.include_router(acquisition_api.router)
    app.include_router(flywheel_api.router)
    app.include_router(curated_api.router)
    # After `admin`, which must answer `/api/admin/connectors/jellyfin/test` before llm's dispatch can.
    app.include_router(llm_api.router)
    # `SpaFallback` declines `/events`, so this router must be the one that serves it (decision 332).
    app.include_router(events_api.router)

    @app.get("/api/health")
    async def health() -> Response:
        """Postgres reachability, in the status code: every consumer (HEALTHCHECK, CI, e2e) keys on it.
        Each step is bounded so a hung database cannot drain the pool."""
        try:
            async with pool.pool().acquire(timeout=_HEALTH_TIMEOUT_S) as conn:
                await conn.fetchval("SELECT 1", timeout=_HEALTH_TIMEOUT_S)
            db_ok = True
        except Exception:
            db_ok = False
        store: ArtifactStore = app.state.artifacts
        # Last probe only, never probed here, so a hung disk cannot hang the check. Names, not paths: this
        # body is unauthenticated. Not in the status code: a read-only mount does not stop serving.
        watch: storage.Watch | None = getattr(app.state, "storage", None)
        unwritable = storage.unwritable(watch.current()) if watch is not None else []
        return JSONResponse(
            status_code=200 if db_ok else 503,
            content={
                "ok": db_ok,
                "role": cfg.role,
                # `is_empty`, not `.version`: a broken store keeps its row's version but loaded nothing.
                "bundle": None if store.is_empty else store.version,
                "public_url": cfg.public_url,
                "storage": {"ok": not unwritable, "unwritable": unwritable},
            },
        )

    # Starlette discards the Response `deps.current_user` wrote the slid cookie onto once anything
    # raises; each handler is the default with that header put back (§3.2).
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc: StarletteHTTPException) -> Response:
        return carry_slid_session_cookie(request, await http_exception_handler(request, exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError) -> Response:
        handled = await request_validation_exception_handler(request, exc)
        return carry_slid_session_cookie(request, handled)

    @app.exception_handler(asyncpg.UniqueViolationError)
    async def _conflict(request, exc: asyncpg.UniqueViolationError) -> Response:
        """A uniqueness conflict is the client's news: 409 naming the constraint. Starlette resolves
        handlers along the MRO, so this wins over `PostgresError` wherever it is written."""
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
        """Connection failures before any session exists (refused, DNS, timeout, TLS) are OSErrors, not
        `PostgresError`. Named classes, not `OSError`: a PermissionError on `/data` is not the database."""
        log.exception("database unreachable")
        return carry_slid_session_cookie(
            request, JSONResponse(status_code=503, content={"detail": "database unreachable"})
        )

    @app.exception_handler(OSError)
    async def _db_unreachable_by_errno(request, exc: OSError) -> Response:
        """An unreachable host or network is a plain OSError, found only by errno. Anything else re-raises
        and stays a 500 with its own traceback."""
        if exc.errno not in _UNREACHABLE_ERRNOS:
            raise exc
        return await _db_unreachable(request, exc)

    # §1: the SvelteKit PWA is a static build served by the backend. Absent in dev.
    static_dir = cfg.static_dir
    if static_dir and static_dir.is_dir():
        root = static_dir.resolve()
        app.mount("/_app", StaticFiles(directory=root / "_app"), name="assets")

        class SpaFallback(APIRoute):
            """Declines the server namespaces at match time: refusing in the handler made an unrouted POST a
            partial match, which Starlette answers 405 instead of 404 (decision 332)."""

            SERVER_NAMESPACES = frozenset({"api", "events"})

            def matches(self, scope: Scope) -> tuple[Match, Scope]:
                match, child = super().matches(scope)
                path = child.get("path_params", {}).get("path", "")
                # `lstrip` first, or `//events/x` has an empty head segment and escapes both namespaces.
                if (
                    match is not Match.NONE
                    and path.lstrip("/").split("/", 1)[0] in self.SERVER_NAMESPACES
                ):
                    return Match.NONE, {}
                return match, child

        async def spa(path: str) -> FileResponse:
            """The path arrives un-normalised: resolve it, then refuse anything outside the static root.
            Nothing served here is content-hashed, so each answer is revalidated: a heuristically fresh
            index.html outlives the chunks it names on an installed phone after the next deploy."""
            index = root / "index.html"
            revalidate = {"Cache-Control": "no-cache"}
            if not path:
                return FileResponse(index, headers=revalidate)
            try:
                candidate = (root / path).resolve()
            except (OSError, ValueError):
                return FileResponse(index, headers=revalidate)
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate, headers=revalidate)
            return FileResponse(index, headers=revalidate)

        app.router.routes.append(SpaFallback("/{path:path}", spa, methods=["GET"]))

    return app


app = create_app()
