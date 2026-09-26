"""Admin routes: §6.6's Jellyfin connector, Users card (the only place accounts are made, decision
166) and System card. Every route is `AdminUser`. The API key never comes back out (§14.3).
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, status
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from spielplan.acquire import intake, queue
from spielplan.api.deps import DB, AdminUser, write_txn
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError, canonical_id
from spielplan.connectors.registry import JellyfinConfig, load_jellyfin, save_jellyfin
from spielplan.core import auth, logs, secrets, webauthn
from spielplan.core.config import settings
from spielplan.importer import dna
from spielplan.llm.client import header_key
from spielplan.sync import playback, seen

log = logging.getLogger("spielplan.api.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _no_control_characters(value: str) -> str:
    """Control characters are never typeable, and a NUL reaches Postgres as a 500: refuse at the edge."""
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("must not contain control characters")
    return value


# Decision 364 makes the pick the acquisition boundary, so it is validated at the edge: whitespace
# would bind as a null ParentId (the whole server), and a GUID is stored as the server spells it.


def _a_folder_jellyfin_could_name(value: str) -> str:
    canonical = canonical_id(value)
    if canonical == "0" * 32:
        raise ValueError("the nil GUID names no library")
    return canonical


def _each_library_once(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


LibraryId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_control_characters),
    AfterValidator(_a_folder_jellyfin_could_name),
]


class JellyfinSettings(BaseModel):
    # Empty means "keep the stored one": a partial save must never blank the half it did not send.
    url: str = Field(default="", max_length=512)
    # Trimmed: a double-clicked key carries a trailing space. Blank once trimmed means keep.
    api_key: Annotated[str, StringConstraints(strip_whitespace=True)] = ""
    # Nullable: None keeps the stored pick, while `[]` means the whole server (decision 364).
    library_ids: Annotated[
        list[LibraryId], Field(max_length=64), AfterValidator(_each_library_once)
    ] | None = None
    # Minted only when asked (decision 418): the value has one appearance (decision 332), and the
    # connectors page discards the response.
    mint_webhook_token: bool = False


class LinkRequest(BaseModel):
    jellyfin_user_id: str = Field(min_length=1, max_length=64)
    # Optional (§7.3): a link without a token attributes playback but cannot write Played state.
    jellyfin_username: str | None = None
    jellyfin_password: str | None = None


# Trimmed: the unique index is on `lower(name)` with no trim.
AccountName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_control_characters),
]

# Decision 166: two roles; a Literal makes `role='guest'` a 422.
AccountRole = Literal["member", "admin"]

# §6.6's admin floor is a check then a write, so every route that could break it takes this lock.
# The name is the seam, not the row: demoting A while disabling B is the race.
_ROSTER_LOCK = "app_user_admin_floor"


class CreateUser(BaseModel):
    name: AccountName
    role: AccountRole = "member"


class EditUser(BaseModel):
    name: AccountName | None = None
    role: AccountRole | None = None


class ActiveRequest(BaseModel):
    is_active: bool


async def _client(conn) -> JellyfinClient:
    client = registry.make_client(await load_jellyfin(conn))
    if client is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Jellyfin is not configured — set its URL and API key first"
        )
    return client


async def _store_probed_version(conn, cfg: JellyfinConfig) -> JellyfinConfig:
    """§7.1's version verdict, stored so the Played write can refuse (`played_write_refusal`).
    Best-effort: an unreachable server leaves the stored verdict; saved only when it changed."""
    client = registry.make_client(cfg)
    if client is None:
        return cfg
    try:
        raw, supported = await client.probe_version()
    except JellyfinError as exc:
        log.info("jellyfin version not probed on save (%s); the stored verdict stands", exc)
        return cfg
    if (raw, supported) == (cfg.server_version, cfg.server_supported):
        return cfg
    return await save_jellyfin(conn, server_version=raw, server_supported=supported)


@router.get("/connectors/jellyfin")
async def get_jellyfin(_: AdminUser, conn: DB) -> dict[str, object]:
    """`secrets_unreadable` (restored dump, wrong SECRETS_KEY) differs from `configured: false`: the
    card must say "re-enter the key"."""
    cfg = await load_jellyfin(conn)
    return {
        "url": cfg.url,
        "has_api_key": bool(cfg.api_key),
        "configured": cfg.configured,
        "library_ids": cfg.library_ids,
        "linked_users": len(cfg.user_tokens),
        "secrets_unreadable": cfg.secrets_unreadable,
        # Whether a webhook token exists, never the token (§14.3; decision 332).
        "has_webhook_token": bool(cfg.webhook_token),
        # `null` means never probed, distinct from a stored `false`.
        "server_version": cfg.server_version,
        "server_supported": cfg.server_supported,
        # §6.6's webhook status, as facts from `acquire/intake` (decision 455).
        "trigger": await intake.trigger_status(
            conn, poll_job=DELTA_POLL_JOB, watermark=cfg.delta_watermark
        ),
    }


@router.put("/connectors/jellyfin")
async def put_jellyfin(body: JellyfinSettings, _: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's Save, which also probes §7.1's version. The only response that carries a freshly minted
    webhook token (decisions 416, 418). A key no header can carry is refused here, since FastAPI's
    422 would quote it back."""
    if body.api_key and header_key(body.api_key) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "api_key must be printable ASCII with no whitespace or control character inside it",
        )
    before = await load_jellyfin(conn)
    cfg = await save_jellyfin(
        conn,
        url=body.url or None,
        api_key=body.api_key or None,
        library_ids=body.library_ids,
        mint_webhook_token=body.mint_webhook_token,
    )
    cfg = await _store_probed_version(conn, cfg)
    return {
        "url": cfg.url,
        "has_api_key": bool(cfg.api_key),
        "configured": cfg.configured,
        # Echoed: `None` kept the pick and `[]` widened it, and the caller cannot tell otherwise.
        "library_ids": cfg.library_ids,
        # `null` unless this save minted it, so a one-time reveal can never linger.
        "webhook_token": (
            cfg.webhook_token
            if cfg.webhook_token and cfg.webhook_token != before.webhook_token
            else None
        ),
        "server_version": cfg.server_version,
        "server_supported": cfg.server_supported,
    }


@router.post("/connectors/jellyfin/test")
async def test_jellyfin(_: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's test button: reports the version and whether it clears §7.1's >= 10.9 pin."""
    client = await _client(conn)
    try:
        probe = await client.check()
    except JellyfinError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status}
    # Stored as `check` returns it: `None` must stay "not reported", never "below the pin".
    await save_jellyfin(
        conn,
        server_version=str(probe.get("version") or ""),
        server_supported=probe["supported"],
    )
    return {"ok": True, **probe}


@router.get("/connectors/jellyfin/users")
async def jellyfin_users(_: AdminUser, conn: DB) -> list[dict[str, object]]:
    """§3.3: "Admin view maps each app user <-> one Jellyfin user (GET /Users)"."""
    client = await _client(conn)
    try:
        users = await client.users()
    except JellyfinError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Jellyfin: {exc}") from exc
    return [{"id": u.id, "name": u.name, "is_admin": u.is_admin} for u in users]


@router.get("/connectors/jellyfin/libraries")
async def jellyfin_libraries(_: AdminUser, conn: DB) -> dict[str, object]:
    """Library folders for §6.6's pick (decision 364). An envelope: `[]` from an unreachable server
    must not look like a deliberate whole-server pick."""
    client = await _client(conn)
    try:
        folders = await client.libraries()
    except JellyfinError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status, "libraries": []}
    return {
        "ok": True,
        # A folder with no id is dropped: the pick is stored as ids.
        "libraries": [
            {"id": str(folder["Id"]), "name": str(folder.get("Name") or "")}
            for folder in folders
            if folder.get("Id")
        ],
    }


@router.get("/users")
async def app_users(_: AdminUser, conn: DB) -> list[dict[str, object]]:
    """The user-mapping table's left-hand column (§6.6 Users)."""
    rows = await conn.fetch(
        """
        SELECT u.id, u.name, u.role, u.is_active, u.jellyfin_user_id, u.jellyfin_link_state,
               u.pin_hash IS NOT NULL AS has_pin,
               (SELECT count(*) FROM webauthn_credential c WHERE c.user_id = u.id) AS passkeys
          FROM app_user u ORDER BY u.id
        """
    )
    cfg = await load_jellyfin(conn)
    return [
        {**dict(r), "has_jellyfin_token": cfg.token_for(r["id"]) is not None} for r in rows
    ]


async def _target(conn: asyncpg.Connection, user_id: int) -> asyncpg.Record:
    """Read inside the writing transaction, so each floor check sees the roster the write changes."""
    row = await conn.fetchrow(
        "SELECT id, name, role, is_active FROM app_user WHERE id = $1", user_id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    return row


async def _refuse_if_last_active_admin(conn: asyncpg.Connection, row, verb: str) -> None:
    """§6.6's admin floor (decision 166). A security rule: with zero admins `POST /api/setup/admin`
    lets anyone mint one."""
    if row["role"] != "admin" or not row["is_active"]:
        return
    others = await conn.fetchval(
        "SELECT count(*) FROM app_user WHERE role = 'admin' AND is_active AND id <> $1",
        row["id"],
    )
    if not others:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the last active admin cannot be {verb} (§6.6: at least one active admin always "
            "exists) — promote another account first",
        )


def _refuse_self(admin: auth.SessionUser, user_id: int, what: str) -> None:
    """§6.6: an admin cannot reset or disable their own account from this tab."""
    if admin.id == user_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"an admin cannot {what} from the Users tab (§6.6)"
        )


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUser, _: AdminUser, conn: DB) -> dict[str, object]:
    """Returns the one-time password exactly once; only its hash is stored (§3.1)."""
    otp = auth.new_one_time_password()
    # Hashed before the transaction: argon2 inside it would hold the row lock for tens of ms.
    password_hash = await auth.hash_password_async(otp)
    try:
        async with write_txn(conn):
            user_id = await conn.fetchval(
                """
                INSERT INTO app_user (name, role, password_hash, must_change_password)
                VALUES ($1, $2, $3, true) RETURNING id
                """,
                body.name,
                body.role,
                password_hash,
            )
    except asyncpg.UniqueViolationError as exc:
        # The `lower(name)` index catches what a check-then-insert would miss.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists"
        ) from exc
    return {
        "id": user_id,
        "name": body.name,
        "role": body.role,
        "one_time_password": otp,
        "note": "shown once — the account is locked to a password change at first login",
    }


@router.patch("/users/{user_id}")
async def edit_user(user_id: int, body: EditUser, _: AdminUser, conn: DB) -> dict[str, object]:
    if body.name is None and body.role is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "send a name, a role, or both")
    try:
        async with write_txn(conn, lock=_ROSTER_LOCK):
            row = await _target(conn, user_id)
            if body.role is not None and body.role != row["role"]:
                await _refuse_if_last_active_admin(conn, row, "demoted")
            updated = await conn.fetchrow(
                """
                UPDATE app_user SET name = coalesce($2, name), role = coalesce($3, role)
                 WHERE id = $1 RETURNING id, name, role, is_active
                """,
                user_id,
                body.name,
                body.role,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists"
        ) from exc
    return dict(updated)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, admin: AdminUser, conn: DB) -> dict[str, object]:
    """Reissues the one-time password and re-arms the first-login change; every session goes too."""
    _refuse_self(admin, user_id, "reset their own password")
    otp = auth.new_one_time_password()
    password_hash = await auth.hash_password_async(otp)
    async with write_txn(conn):
        await _target(conn, user_id)
        await conn.execute(
            "UPDATE app_user SET password_hash = $2, must_change_password = true WHERE id = $1",
            user_id,
            password_hash,
        )
        # Clear the guesses that made the reset necessary (§3.2).
        await auth.clear_password_lockout(conn, user_id)
        revoked = await auth.destroy_user_sessions(conn, user_id)
    return {
        "ok": True,
        "user_id": user_id,
        "one_time_password": otp,
        "sessions_revoked": revoked,
        "note": "shown once — the account is locked to a password change at first login",
    }


@router.post("/users/{user_id}/reset-pin")
async def reset_pin(user_id: int, admin: AdminUser, conn: DB) -> dict[str, object]:
    """The counters clear with the hash, or the new PIN would be locked out (§3.2)."""
    _refuse_self(admin, user_id, "reset their own PIN")
    async with write_txn(conn):
        await _target(conn, user_id)
        await conn.execute(
            "UPDATE app_user SET pin_hash = NULL, pin_failed_count = 0, pin_locked_until = NULL "
            "WHERE id = $1",
            user_id,
        )
    return {"ok": True, "user_id": user_id, "has_pin": False}


@router.get("/users/{user_id}/passkeys")
async def list_passkeys(user_id: int, _: AdminUser, conn: DB) -> list[dict[str, object]]:
    """The list half of §6.6's per-credential revoke; no public keys."""
    await _target(conn, user_id)
    return await webauthn.list_credentials(conn, user_id)


@router.delete("/users/{user_id}/passkeys/{credential_id:path}")
async def revoke_passkey(
    user_id: int, credential_id: str, _: AdminUser, conn: DB
) -> dict[str, bool]:
    """Scoped to the named account: an id off one row cannot revoke another account's key."""
    async with write_txn(conn):
        await _target(conn, user_id)
        removed = await webauthn.delete_credential(conn, user_id, credential_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey on that account")
    return {"ok": True}


@router.post("/users/{user_id}/active")
async def set_active(
    user_id: int, body: ActiveRequest, admin: AdminUser, conn: DB
) -> dict[str, object]:
    """§6.6's disable and its undo. Sessions are deleted so a disable takes effect on a live device now."""
    async with write_txn(conn, lock=_ROSTER_LOCK):
        row = await _target(conn, user_id)
        revoked = 0
        if not body.is_active:
            # The floor first: an admin disabling themselves needs to hear the household would have none.
            await _refuse_if_last_active_admin(conn, row, "disabled")
            _refuse_self(admin, user_id, "disable their own account")
        await conn.execute(
            "UPDATE app_user SET is_active = $2 WHERE id = $1", user_id, body.is_active
        )
        if not body.is_active:
            revoked = await auth.destroy_user_sessions(conn, user_id)
    return {
        "ok": True,
        "user_id": user_id,
        "is_active": body.is_active,
        "sessions_revoked": revoked,
    }


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, _: AdminUser, conn: DB) -> dict[str, bool]:
    """Everything referencing the row goes, including every Tonight session they hosted
    (`session.host_user_id` cascades)."""
    async with write_txn(conn, lock=_ROSTER_LOCK):
        row = await _target(conn, user_id)
        await _refuse_if_last_active_admin(conn, row, "deleted")
        await conn.execute("DELETE FROM app_user WHERE id = $1", user_id)
    return {"ok": True}


@router.post("/users/{user_id}/jellyfin")
async def link_jellyfin(
    user_id: int, body: LinkRequest, _: AdminUser, conn: DB
) -> dict[str, object]:
    """§3.3: optional and one-to-one, held by the partial unique index rather than a lookup."""
    if not await conn.fetchval("SELECT 1 FROM app_user WHERE id = $1", user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")

    # Refused while secrets are unreadable: the old identity's token cannot be dropped and would later
    # be sent under the new mapping. The connector PUT is the cure.
    if (await load_jellyfin(conn)).secrets_unreadable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{registry.SECRETS_UNREADABLE_REASON}: re-enter the Jellyfin API key before "
            "changing this account's mapping",
        )

    token: str | None = None
    if body.jellyfin_username and body.jellyfin_password:
        client = await _client(conn)
        try:
            jf_user_id, token = await client.authenticate_by_name(
                body.jellyfin_username, body.jellyfin_password
            )
        except JellyfinError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, f"Jellyfin refused that sign-in: {exc}"
            ) from exc
        if jf_user_id != body.jellyfin_user_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "those Jellyfin credentials belong to a different Jellyfin user",
            )

    # One transaction, connector row locked: the sealed token map merges in Python, so concurrent
    # links would lose an entry. `app_user` before the connector row, as `seen.unlink` does, to avoid
    # deadlock. The Jellyfin sign-in stays outside the lock.
    try:
        async with write_txn(conn):
            await conn.execute(
                "UPDATE app_user SET jellyfin_user_id = $2, jellyfin_link_state = $3 "
                "WHERE id = $1",
                user_id,
                body.jellyfin_user_id,
                # Linked without a token: attributes playback, cannot write Played state (§7.3).
                "linked" if token else "needs_relink",
            )
            if token:
                cfg = await load_jellyfin(conn, for_update=True)
                tokens = dict(cfg.user_tokens)
                tokens[str(user_id)] = token
                await save_jellyfin(conn, user_tokens=tokens)
            else:
                # Re-pointed without a sign-in: drop the old identity's token.
                await seen.forget_token(conn, user_id)
    except asyncpg.UniqueViolationError as exc:
        # Outside the block: the violation rolls back badge and token together.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that Jellyfin user is already linked to another account (§3.3: one-to-one)",
        ) from exc

    return {
        "ok": True,
        "user_id": user_id,
        "jellyfin_user_id": body.jellyfin_user_id,
        "has_token": bool(token),
        "state": "linked" if token else "needs_relink",
    }


@router.delete("/users/{user_id}/jellyfin")
async def unlink_jellyfin(user_id: int, _: AdminUser, conn: DB) -> dict[str, bool]:
    await seen.unlink(conn, user_id)
    return {"ok": True}


@router.post("/connectors/jellyfin/sync")
async def sync_now(_: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's sync now: the same code path as the 15-minute job (§5.3)."""
    cfg = await load_jellyfin(conn)
    return (await seen.sync_all(conn, registry.make_client(cfg))).as_dict()


@router.post("/connectors/jellyfin/poll")
async def poll_now(_: AdminUser, conn: DB) -> dict[str, object]:
    """§7.3's 1-minute /Sessions watcher, on demand."""
    cfg = await load_jellyfin(conn)
    return (await playback.poll(conn, registry.make_client(cfg))).as_dict()


# Job names are spelled here, not imported: the worker registry pulls torch into the web process.
# `test_worker_registry.py` pins them.
BACKUP_JOB = "nightly-backup"

DELTA_POLL_JOB = "jellyfin-delta-poll"

STORAGE_JOB = "storage-check"

# A parameter: the newest row per named job is one index lookup each, where DISTINCT ON reads the
# whole table.
JOB_NAMES: tuple[str, ...] = (
    "session-prune",
    "push-subscription-prune",
    "webauthn-challenge-prune",
    "job-run-prune",
    "ledger-map-refit",
    "ledger-refresh",
    "fold-in-user-vectors",
    "fold-in-tick",
    "tier-set-refit",
    "placement-reconciliation",
    # The one job whose failure is invisible to a member: a library that simply never grows.
    "acquisition-drain",
    "metadata-backfill",
    "art-lookup",
    "jellyfin-seen-sync",
    "jellyfin-sessions-poll",
    DELTA_POLL_JOB,
    "jellyfin-intake-sweep",
    # Its phase and report stay on `/api/admin/bundle/state`; here only job health (decision 182).
    "bundle-import",
    STORAGE_JOB,
    BACKUP_JOB,
)

# Decision 454's last syncs: when each connector last actually answered. A subset of JOB_NAMES.
SYNC_JOBS: dict[str, str] = {
    "jellyfin-seen-sync": "Jellyfin",
    DELTA_POLL_JOB: "Jellyfin",
    "jellyfin-intake-sweep": "Jellyfin",
    "jellyfin-sessions-poll": "Jellyfin",
    "acquisition-drain": "Metadata sources",
}

# A night plus half a day of slack: fires before a second night is missed.
BACKUP_STALE_AFTER = timedelta(hours=36)


async def job_health(conn) -> dict[str, object]:
    """`jobs` is the newest run per job; `backup` and `last_syncs` are the newest that succeeded, a
    different row on exactly the install that needs reporting."""
    jobs = await conn.fetch(
        "SELECT j.name, r.started_at, r.finished_at, r.ok, r.detail "
        "  FROM unnest($1::text[]) AS j(name) "
        "  JOIN LATERAL ("
        "       SELECT started_at, finished_at, ok, detail FROM job_run "
        "        WHERE name = j.name ORDER BY started_at DESC LIMIT 1"
        "  ) r ON true "
        " ORDER BY j.name",
        list(JOB_NAMES),
    )
    # A sync counts only when its server answered, which `ok` does not say: `detail->>'reached'`, or
    # any report at all for the delta poll.
    succeeded = {
        r["name"]: r
        for r in await conn.fetch(
            "SELECT j.name, r.finished_at, r.detail "
            "  FROM unnest($1::text[]) AS j(name) "
            "  JOIN LATERAL ("
            "       SELECT finished_at, detail FROM job_run "
            "        WHERE name = j.name AND ok "
            "          AND (j.name = $2 OR (detail IS NOT NULL"
            "               AND coalesce((detail->>'reached')::boolean, true)))"
            "        ORDER BY started_at DESC LIMIT 1"
            "  ) r ON true",
            [BACKUP_JOB, *SYNC_JOBS],
            BACKUP_JOB,
        )
    }
    backup = succeeded.get(BACKUP_JOB)
    backup_at = backup["finished_at"] if backup else None
    return {
        "jobs": [dict(r) for r in jobs],
        "backup": {
            "at": backup_at,
            # Read defensively: older rows may lack the field.
            "bytes": (backup["detail"] or {}).get("bytes") if backup else None,
            # Never having dumped is stale too.
            "stale": backup_at is None or datetime.now(UTC) - backup_at > BACKUP_STALE_AFTER,
            "stale_after_hours": int(BACKUP_STALE_AFTER.total_seconds() // 3600),
        },
        # A null means never: a job left off would read as nonexistent.
        "last_syncs": [
            {
                "name": name,
                "connector": connector,
                "at": succeeded[name]["finished_at"] if name in succeeded else None,
                "detail": succeeded[name]["detail"] if name in succeeded else None,
            }
            for name, connector in SYNC_JOBS.items()
        ],
    }


# What the missing axis artifact costs, one sentence per surface.
AXES_DISABLES = (
    "§6.4's Map, when it ships (§12 M6; not built yet, decision 488), has no axes to plot.",
    "Tonight's facet split (§6.2 step 5) is off: a split is surfaced by person and never names a "
    "facet (decision 479), and 54c's widest-axis tie-break is 0.0 for every pair.",
)


@router.get("/data/sources")
async def data_sources(_: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6 Data's two read-only lists: dataset terms (§4.1 rule 4's licences) and the axis files the
    loader expects, built from the loader's own rule over this install's facets."""
    sources = await conn.fetch(
        "SELECT id, name, scale, url, license, version, notes FROM rating_source ORDER BY id"
    )
    version = await conn.fetchval(
        "SELECT vocabulary_version FROM artifact_bundle WHERE state = 'active'"
    )
    facets = [
        r["facet"] for r in await conn.fetch(
            "SELECT facet FROM dna_facet WHERE version = $1 ORDER BY ord, facet", version
        )
    ] if version else []
    # No vocabulary loaded: name the app's default facets rather than nothing.
    if not facets:
        facets = sorted(dna.DEFAULT_FACET_COLOURS)
    loaded = await conn.fetchval(
        "SELECT count(*) FROM dna_axis WHERE version = $1", version
    ) if version else 0
    return {
        "sources": [dict(r) for r in sources],
        "axes": {
            "vocabulary_version": version,
            "loaded": int(loaded or 0),
            "expected": [f"dna_vocab/{version or '<version>'}/{f}.tsv" for f in facets],
            "disables": list(AXES_DISABLES),
        },
    }


@router.get("/system")
async def system_card(_: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's System card, read-only: decision 182's backup, custody and job facts, plus decision 454's
    queue depth, last syncs and this process's log lines."""
    cfg = settings()
    key_id = await secrets.active_key_id(conn)
    unreadable = False
    if cfg.secrets_key:
        # `core.secrets` owns the question, so this and `spielplan-secrets reset` agree. Never raises.
        unreadable = bool(await secrets.unreadable_key_ids(conn))
    depth = await queue.stats(conn)
    by_state = dict.fromkeys((queue.PENDING, queue.LEASED, queue.DONE, queue.FAILED, queue.SKIPPED), 0)
    for row in depth:
        by_state[row["state"]] = by_state.get(row["state"], 0) + row["count"]
    return {
        **await job_health(conn),
        # Every state, zero included.
        "queue": {"by_state": by_state, "by_kind": depth},
        "logs": logs.snapshot(),
        "secrets": {
            # Absent is legal (§3.1).
            "configured": bool(cfg.secrets_key),
            "fingerprint": secrets.key_fingerprint(cfg.secrets_key) if cfg.secrets_key else None,
            "key_id": key_id,
            "unreadable": unreadable,
        },
    }
