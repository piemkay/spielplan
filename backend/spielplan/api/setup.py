"""First-boot wizard. Spec v2.1 §3.1.

The sequence is normative and it ends at the bundle: create admin -> optional env-seeded
connector config -> bundle import (the *same* importer the §6.6 Data tab exposes). A
bundle-less app is a legal state, so the bundle step is skippable and the wizard reports
that explicitly rather than blocking.

Accounts are not made here. Decision 164 moves member creation to §6.6's Users card — the
wizard is reachable only until an admin exists, so the one path that could create an account
disappeared the moment first boot ended, and a household that wanted a third member had none.

Member first-run onboarding (PWA install + push permission, §6 preamble) is not a wizard step
either: it is a per-phone act the member performs on their own device, and this module keeps
only the route that records it, per user.
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints

from spielplan.api.deps import DB, ActiveUser, AdminUser, current_user, set_session_cookie, write_txn
from spielplan.core import auth, secrets
from spielplan.core.config import settings

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Decision 164: "members" is gone from the wizard's sequence, not merely hidden — the step it
# recorded was the one account-creating path in the app, and it now lives at §6.6's Users card.
STEPS = ("admin", "connectors", "bundle", "onboarding")

# The ribbon the prototype prints on every wizard step, as data rather than copy baked into the
# client. It is the one thing besides `required` that the anonymous login page needs.
NOTE = "first boot · a bundle-less app is a legal state"

# §3.1 stores this name and `lower(name)` is what login resolves against, so the trim happens
# before the row exists rather than being papered over at every read (as-11).
AccountName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class AdminInit(BaseModel):
    name: AccountName
    password: str = Field(min_length=10)


class ConnectorSeed(BaseModel):
    name: str
    config: dict = Field(default_factory=dict)
    secrets: dict | None = None


async def _optional_user(
    request: Request, response: Response, conn: DB
) -> auth.SessionUser | None:
    """`current_user`, but a missing or dead cookie is an answer rather than a 401.

    `GET /state` is the one route that must serve both a first-booting stranger and a signed-in
    operator, so it cannot be gated and cannot be open either (sec-14). Re-using `current_user`
    rather than re-reading the cookie here keeps the slide and the clearing Set-Cookie it
    already applies to `response`; only its refusal is swallowed.

    A session locked to §3.1's forced first-login change counts as a stranger here. Decision 179
    puts every other authenticated route behind `ActiveUser`, and the privileged half of `/state`
    is precisely the install fingerprint sec-14 took off the anonymous surface — member count,
    bundle version, how far the wizard got. Answering it as anonymous rather than 403 is what
    keeps `required` — the bit the first-boot redirect reads — the same bit for everyone.
    """
    try:
        user = await current_user(request, response, conn)
    except HTTPException:
        return None
    return None if user.must_change_password else user


OptionalUser = Annotated[auth.SessionUser | None, Depends(_optional_user)]


@router.get("/state")
async def state(user: OptionalUser, conn: DB) -> dict[str, object]:
    """§3.1's wizard state, cut to what the caller is entitled to know.

    An anonymous caller gets the two fields the login page and the wizard's first screen are
    written against and nothing else. The full payload fingerprints the install to anyone who can
    reach the origin — how many members the household has, which bundle version is loaded and
    when, how far the operator got — and §2 puts that origin on Tailscale, where Cloudflare's
    rate limit does not stand in front of a prober (sec-14). `required` is the same bit either
    way, so the redirect on first boot still works before anyone can sign in.
    """
    has_admin = await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") > 0
    # §3.1: the wizard is needed until an admin exists; after that it is a revisitable page.
    if user is None:
        return {"required": not has_admin, "note": NOTE}

    done = {r["step"] for r in await conn.fetch("SELECT step FROM setup_step")}
    members = await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'member'")
    bundle = await conn.fetchrow(
        "SELECT version, imported_at FROM artifact_bundle WHERE state = 'active'"
    )
    return {
        "required": not has_admin,
        "steps": [{"step": s, "done": s in done} for s in STEPS],
        "has_admin": has_admin,
        "member_count": members,
        "bundle": dict(bundle) if bundle else None,
        "note": NOTE,
    }


@router.post("/admin", status_code=status.HTTP_201_CREATED)
async def create_admin(body: AdminInit, response: Response, conn: DB) -> dict[str, object]:
    """Only callable while no admin exists — otherwise this would be a privilege-escalation
    endpoint reachable by anyone who can see the setup page.

    The check and the three writes are one transaction under one lock (as04). Unserialised, the
    count-then-INSERT let two submits with different names each pass the check and each become
    "the only admin", and a failure between the writes left an admin row whose password had been
    typed once into a form that errored while `/state` already reported `required=false` — a
    first boot that can never be re-run. The lock is what makes the loser read what the winner
    committed; the transaction is what makes the account, the wizard step and the session arrive
    together or not at all.
    """
    # argon2 is tens of milliseconds by design (§3.2), so the hash is computed before the lock
    # is taken rather than inside it, and off the event loop that is serving everyone else.
    password_hash = await auth.hash_password_async(body.password)
    try:
        async with write_txn(conn, lock="setup_admin"):
            if await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") > 0:
                raise HTTPException(status.HTTP_409_CONFLICT, "an admin account already exists")

            user_id = await conn.fetchval(
                """
                INSERT INTO app_user (name, role, password_hash, must_change_password)
                VALUES ($1, 'admin', $2, false) RETURNING id
                """,
                body.name,
                password_hash,
            )
            await conn.execute(
                "INSERT INTO setup_step (step) VALUES ('admin') ON CONFLICT (step) DO NOTHING"
            )
            sid = await auth.create_session(conn, user_id, auth_method="password")
    except asyncpg.UniqueViolationError as exc:
        # A pre-existing non-admin row already holds the name. `app_user_name_key` says so; the
        # generic PostgresError handler (`app.py:114-117`) would say "database error", 500.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists"
        ) from exc
    set_session_cookie(response, sid)
    return {"id": user_id, "name": body.name, "role": "admin"}


@router.post("/connectors")
async def seed_connector(body: ConnectorSeed, _: AdminUser, conn: DB) -> dict[str, object]:
    """§2: connectors are configured in the admin UI and stored in `connector_config`;
    env vars may only *seed* them on first boot. Writing a secret requires SECRETS_KEY —
    the app refuses rather than falling back."""
    if body.secrets:
        settings().require_secrets_key()
    await secrets.put_connector_secrets(conn, body.name, body.config, body.secrets)
    await conn.execute(
        "INSERT INTO setup_step (step) VALUES ('connectors') ON CONFLICT (step) DO NOTHING"
    )
    return {"ok": True, "name": body.name, "has_secrets": bool(body.secrets)}


@router.post("/onboarding/complete")
async def complete_onboarding(user: ActiveUser, conn: DB) -> dict[str, bool]:
    """§6 preamble: iOS has no programmatic install prompt, so onboarding is a guided act the
    phone confirms. Recorded per user because the ask is per phone: decision 180 amends that
    preamble to ask once and honour a decline, and this row is what "once" is counted against.

    `ActiveUser`, not `CurrentUser`: §3.1 locks an account created with a one-time password to
    the password change, and onboarding is the step *after* it (spec-05). /me, /password,
    /logout and /switch are the whole reachable set until the change is done.
    """
    await conn.execute(
        """
        INSERT INTO setup_step (step, detail) VALUES ('onboarding', $1)
        ON CONFLICT (step) DO UPDATE SET detail = setup_step.detail || EXCLUDED.detail
        """,
        {str(user.id): True},
    )
    return {"ok": True}
