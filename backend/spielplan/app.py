"""FastAPI application. Spec v2.1 §1, §2, §3.1.

First boot is a defined sequence and **a bundle-less app is a legal state** (§3.1): the app
boots with `/data/artifacts` and `artifact_bundle` empty, serving the setup wizard and admin
routes, and artifact-dependent surfaces render an explicit "no bundle imported" state instead
of erroring. Nothing in startup may raise because a bundle is missing.
"""

from __future__ import annotations

import logging
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
from spielplan.core.config import settings
from spielplan.db import migrate, pool
from spielplan.models.artifacts import ArtifactStore
from spielplan.push import keys as push_keys
from spielplan.scoring import backbone

log = logging.getLogger("spielplan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = settings()
    await pool.open_pool(cfg.database_url)

    async with pool.acquire() as conn:
        applied = await migrate.apply_all(conn)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
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
        # §4.3: artifacts load "when present"; an empty store is legal.
        app.state.artifacts = await ArtifactStore.load_active(conn, cfg.artifacts_dir)
        # §5.1's basis, loaded once per process. §10 restarts on a bundle swap, so a process
        # never has to reload it — and a Backbone that fails to load degrades the scoring
        # surfaces rather than stopping a boot the admin needs in order to fix the bundle.
        try:
            app.state.backbone = backbone.load_for(app.state.artifacts)
        except backbone.BackboneError:
            log.exception("backbone.npz is unusable — serving without collaborative scores")
            app.state.backbone = backbone.Backbone.empty()

    if app.state.artifacts.is_empty:
        log.info("no artifact bundle active — serving setup wizard and admin routes (§3.1)")
    try:
        yield
    finally:
        await pool.close_pool()


def create_app() -> FastAPI:
    cfg = settings()
    app = FastAPI(title="Spielplan", version="0.1.0", lifespan=lifespan, docs_url="/api/docs")

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
    async def health() -> dict[str, object]:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        store: ArtifactStore = app.state.artifacts
        return {
            "ok": db_ok,
            "role": cfg.role,
            "bundle": store.version,          # null is a legal, reported state (§3.1)
            "public_url": cfg.public_url,
        }

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

    @app.exception_handler(asyncpg.PostgresError)
    async def _pg_error(request, exc: asyncpg.PostgresError) -> Response:
        log.exception("database error")
        return carry_slid_session_cookie(
            request, JSONResponse(status_code=500, content={"detail": "database error"})
        )

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
