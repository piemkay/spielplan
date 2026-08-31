"""Dev harness — NOT the app.

This serves the same API surface as `spielplan.app` from the test fixture bundle, in memory,
so the SvelteKit front end can be developed and inspected on a machine with no Postgres. It
exists because the real stack needs `docker compose up` (Postgres 16), and a laptop without
Docker should still be able to look at the UI.

It is deliberately dumb: no auth beyond a cookie flag, no persistence, no model. If a response
shape here ever disagrees with `spielplan/api/`, the real app is right and this file is wrong —
`tests/test_devstub_contract.py` compares the two path sets so the drift is caught.

Run:  python ops/devstub.py            (serves http://127.0.0.1:8080)
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import Cookie, FastAPI, HTTPException, Query, Response
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from spielplan.api.auth import SURFACES  # noqa: E402 - the real surface list, not a copy
from spielplan.core.config import settings  # noqa: E402
from spielplan.db.library import normalise_kinds  # noqa: E402 - §4.1 rule 5's real validator
from spielplan.home import rail, shelves  # noqa: E402 - decision 117's real gate, real copy
from spielplan.home.why import NAMED_TERM_CAP, WhyTerm  # noqa: E402
from spielplan.ledger.hyperparams import Hyperparams  # noqa: E402 - §4.3's real margins
from spielplan.rank import board as rank_board  # noqa: E402 - the real badges
from spielplan.rank import queue as rank_queue  # noqa: E402 - the real 70/20/10 selector
from spielplan.rank import tiers as rank_tiers  # noqa: E402 - decision 11's real rules
from spielplan.rate import VERDICT_LABELS, balance, battle, queue  # noqa: E402
from spielplan.rate import session as rate_session  # noqa: E402
from spielplan.tonight import combine as tonight_combine  # noqa: E402 - the real slate
from spielplan.tonight import pool as tonight_pool  # noqa: E402 - the real §6.2 step 3 pool
from spielplan.tonight import rooms as tonight_rooms_mod  # noqa: E402 - the real room code
from spielplan.tonight import round as tonight_round  # noqa: E402 - the real adaptive round
from spielplan.tonight import solo as tonight_solo  # noqa: E402 - the real solo copy
from spielplan.tonight import tilt as tonight_tilt  # noqa: E402 - the real centring lever
from tests.fixtures import make_bundle as fx  # noqa: E402

BUNDLE = ROOT / "data" / "devstub-bundle"
STATE: dict[str, Any] = {
    "imported": False,
    "users": {},
    "next_id": 1,
    "seen": {},
    # M2. `verdicts` and `rate` are per user id; `seen` is not, because M1 already made it a
    # household fact here and two spellings of "seen" in one harness is the bug this file
    # exists to keep out of the front end. §6.7's rail is NOT here: `home.rail` keeps its own
    # in-process buffer and the harness writes to that one.
    "verdicts": {},
    "rate": {},
    # §12 M2's onboarding. Keyed by endpoint, because §4.2 makes `push_subscription.endpoint`
    # UNIQUE — one endpoint is one device is one row, and re-subscribing must not accumulate.
    "push": {},
    "onboarding_complete": False,
    "next_row_id": 1,
    "catalog": None,
    "dna": None,
    "scores": {},
    "tier_sets": {},
    "tonight": {},
    "tonight_seq": 0,
    "tier_edits": {},
    "rank_comparisons": {},
    "jellyfin": {"url": "", "has_api_key": False, "configured": False, "library_ids": [],
                 "linked_users": 0},
}

app = FastAPI(title="Spielplan dev harness")


def _db() -> sqlite3.Connection:
    if not (BUNDLE / "content.sqlite").exists():
        fx.make_bundle(BUNDLE)
    db = sqlite3.connect(f"file:{BUNDLE / 'content.sqlite'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _nav(role: str) -> dict[str, list[dict[str, str]]]:
    """§6.6 is admin-role only, so the harness has to hide the admin entries too — a UI built
    against a stub that always returns them would never exercise the member shell."""
    account = [
        {"key": "account", "href": "/account", "label": "Account & passkeys"},
        {"key": "taste", "href": "/taste", "label": "My Taste"},
    ]
    if role == "admin":
        account += [
            {"key": "admin", "href": "/admin/data", "label": "Admin view"},
            {"key": "setup", "href": "/setup", "label": "Setup wizard"},
        ]
    return {"surfaces": [dict(s) for s in SURFACES], "account": account}


def _user(name: str, role: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": STATE["next_id"], "name": name, "role": role,
        "must_change_password": False, "auth_method": "password",
        "admin_reauth_required": False, "show_model": False,
        "nav": _nav(role), "has_pin": False, "passkeys": 0,
        "jellyfin": {"linked": False, "state": None},
        **extra,
    }


def _me(sid: str | None) -> dict[str, Any]:
    user = STATE["users"].get(sid)
    if not user:
        raise HTTPException(401, "not signed in")
    return user


# --- health / config ---------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "role": "devstub", "bundle": "test-v1" if STATE["imported"] else None,
            "public_url": "http://127.0.0.1:8080"}


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "public_url": "http://127.0.0.1:8080",
        "has_bundle": STATE["imported"],
        "bundle": (
            {"version": "test-v1", "vocabulary_version": "v1", "titles": len(fx.TITLES),
             "owned": len(fx.TITLES), "present": {}, "missing_required": []}
            if STATE["imported"] else None
        ),
    }


# --- setup / auth ------------------------------------------------------------


class AdminInit(BaseModel):
    name: str
    password: str


class MemberInit(BaseModel):
    name: str
    role: str = "member"


class LoginRequest(BaseModel):
    name: str
    password: str
    device_label: str | None = None


@app.get("/api/setup/state")
def setup_state() -> dict[str, Any]:
    has_admin = any(u["role"] == "admin" for u in STATE["users"].values())
    members = sum(1 for u in STATE["users"].values() if u["role"] == "member")
    return {
        "required": not has_admin,
        "steps": [
            {"step": s, "done": done}
            for s, done in (
                ("admin", has_admin), ("connectors", False),
                ("bundle", STATE["imported"]), ("members", members > 0), ("onboarding", False),
            )
        ],
        "has_admin": has_admin,
        "member_count": members,
        "bundle": (
            {"version": "test-v1", "imported_at": "2026-08-29T00:00:00Z"}
            if STATE["imported"] else None
        ),
        "note": "first boot · a bundle-less app is a legal state",
    }


def _sign_in(response: Response, user: dict[str, Any]) -> dict[str, Any]:
    sid = f"dev-{user['id']}"
    STATE["users"][sid] = user
    response.set_cookie("spielplan_session", sid, httponly=True, samesite="lax", path="/")
    return user


@app.post("/api/setup/admin", status_code=201)
def create_admin(body: AdminInit, response: Response) -> dict[str, Any]:
    if any(u["role"] == "admin" for u in STATE["users"].values()):
        raise HTTPException(409, "an admin account already exists")
    user = _user(body.name, "admin")
    STATE["next_id"] += 1
    return _sign_in(response, user)


@app.post("/api/setup/members", status_code=201)
def create_member(body: MemberInit) -> dict[str, Any]:
    user = _user(body.name, body.role, must_change_password=True)
    STATE["next_id"] += 1
    STATE["users"][f"pending-{user['id']}"] = user
    return {**user, "one_time_password": "kq7mrn24tphs",
            "note": "shown once — the account is locked to a password change at first login"}


@app.post("/api/setup/connectors")
def seed_connector() -> dict[str, Any]:
    return {"ok": True, "name": "jellyfin", "has_secrets": False}


@app.post("/api/setup/onboarding/complete")
def complete_onboarding() -> dict[str, bool]:
    STATE["onboarding_complete"] = True
    return {"ok": True}


# --- §12 M2's member PWA-install/push onboarding ------------------------------------------------
#
# The harness has no VAPID key and no push service, so `vapid_public_key` is always null — which
# is the state a real install is in until M4 ships the sender, and the state the onboarding screen
# has to render honestly rather than throwing a DOMException at the member. What IS reproduced is
# the shape and the one rule the UI depends on: one row per endpoint, so re-subscribing the same
# device updates rather than accumulating.


def _device_handle(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


def _subscriptions() -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "device_label": row["device_label"],
            # Never the endpoint: it is a bearer capability (see spielplan/api/push.py).
            "device": _device_handle(row["endpoint"]),
            "created_at": row["created_at"],
            "last_seen_ok": None,
        }
        for row in STATE["push"].values()
    ]


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: dict[str, str] = {}
    device_label: str | None = None


class PushEndpointRequest(BaseModel):
    endpoint: str


@app.get("/api/push/state")
def push_state() -> dict[str, Any]:
    return {
        "onboarding_complete": STATE["onboarding_complete"],
        "vapid_public_key": None,
        "subscriptions": _subscriptions(),
    }


@app.post("/api/push/subscribe", status_code=201)
def push_subscribe(body: PushSubscribeRequest) -> dict[str, Any]:
    existing = STATE["push"].get(body.endpoint)
    row = existing or {
        "id": len(STATE["push"]) + 1,
        "endpoint": body.endpoint,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    row["device_label"] = body.device_label
    STATE["push"][body.endpoint] = row
    return {
        "ok": True,
        "id": row["id"],
        "device": _device_handle(body.endpoint),
        "subscriptions": _subscriptions(),
    }


@app.delete("/api/push/subscription")
def push_unsubscribe(body: PushEndpointRequest) -> dict[str, Any]:
    if STATE["push"].pop(body.endpoint, None) is None:
        raise HTTPException(404, "no such subscription")
    return {"ok": True, "subscriptions": _subscriptions()}


@app.post("/api/auth/login")
def login(body: LoginRequest, response: Response) -> dict[str, Any]:
    for user in STATE["users"].values():
        if user["name"].lower() == body.name.lower():
            return _sign_in(response, user)
    raise HTTPException(401, "wrong name or password")


@app.get("/api/auth/me")
def me(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    return _me(spielplan_session)


@app.post("/api/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("spielplan_session", path="/")
    return {"ok": True}


@app.get("/api/auth/switchable")
def switchable() -> list[dict[str, Any]]:
    return []


class PreferencesRequest(BaseModel):
    show_model: bool


@app.post("/api/auth/preferences")
def set_preferences(
    body: PreferencesRequest, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    user["show_model"] = body.show_model
    return {"ok": True, "show_model": body.show_model}


# --- bundle ------------------------------------------------------------------


@app.get("/api/admin/bundle/state")
def bundle_state() -> dict[str, Any]:
    return {
        "bundles": (
            [{"version": "test-v1", "state": "active", "imported_at": "2026-08-29T00:00:00Z",
              "activated_at": "2026-08-29T00:00:00Z"}] if STATE["imported"] else []
        ),
        "loaded": ({"version": "test-v1", "vocabulary_version": "v1", "titles": len(fx.TITLES),
                    "owned": len(fx.TITLES), "present": {}, "missing_required": []}
                   if STATE["imported"] else None),
        "import_dir": str(BUNDLE),
        "rebuild_set": [
            "user fold-in vectors (closed-form, ms)",
            "per-label-count blend weights",
            "full Personal Ledger MAP refit",
            "Cold Tower re-placement of every app-acquired title",
        ],
    }


def _real_report() -> dict[str, Any]:
    """Use the REAL validator so the report the UI renders is the real thing."""
    from spielplan.importer import bundle as bundle_import

    fx.make_bundle(BUNDLE)
    return bundle_import.validate(bundle_import.Bundle.open(BUNDLE)).as_dict()


@app.post("/api/admin/bundle/validate")
def validate_bundle() -> dict[str, Any]:
    return {"bundle_version": "test-v1", "report": _real_report(), "text": ""}


@app.post("/api/admin/bundle/import")
def import_bundle() -> dict[str, Any]:
    report = _real_report()
    STATE["imported"] = True
    return {"bundle_version": "test-v1", "report": report, "text": "",
            "restart_required": True,
            "note": "restart backend and worker — no process may score or refit with a loaded "
                    "bundle version different from the active row"}


# --- library -----------------------------------------------------------------


def _titles(kind: str) -> list[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM title WHERE kind = ? ORDER BY year DESC", (kind,)).fetchall()


def _seen_state(title_id: int) -> str:
    """The harness's ONE seen state.

    `/api/titles`, `/api/titles/{id}/state`, `/api/rate` and `/api/home` all read it. Two
    spellings of "seen" in one harness is a day the front end spends chasing a card that is
    seen on the shelf and unseen on the queue — and the real app has one column.
    """
    return STATE["seen"].get(title_id, "unseen")


def _placement(title_id: int) -> str:
    """Stand-in for §8 stage 10's Cold Tower badge. One rule, read by the catalog and by
    Home's `new_in_library` shelf, so a title cannot be cold on one surface and warm on the
    next."""
    return "cold_tower" if title_id % 3 == 0 else "warm"


@app.get("/api/titles")
def list_titles(
    kind: list[str] = Query(...),
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    seen: str = "any",
    person_id: int | None = None,
    limit: int = 60,
    offset: int = 0,
) -> dict[str, Any]:
    kinds = [k for k in ("movie", "series") if k in kind]
    if not kinds:
        raise HTTPException(422, "select at least one kind: 'movie', 'series', or both")
    if not STATE["imported"]:
        return {"kinds": kinds, "total": 0, "hidden": {}, "limit": limit, "offset": offset,
                "items": []}
    rows = [r for k in kinds for r in _titles(k)]
    items = []
    with _db() as db:
        for r in rows:
            if q and q.lower() not in r["name"].lower():
                continue
            if decade and not (r["year"] and decade <= r["year"] < decade + 10):
                continue
            if genre:
                g = db.execute(
                    "SELECT 1 FROM title_genre WHERE title_id = ? AND genre = ?", (r["id"], genre)
                ).fetchone()
                if not g:
                    continue
            if person_id:
                c = db.execute(
                    "SELECT 1 FROM credit WHERE title_id = ? AND person_id = ?", (r["id"], person_id)
                ).fetchone()
                if not c:
                    continue
            items.append({
                "id": r["id"], "kind": r["kind"], "name": r["name"], "year": r["year"],
                "runtime_min": r["runtime_min"], "poster_path": None, "is_owned": True,
                "placement": _placement(r["id"]),
                "seen_state": _seen_state(r["id"]),
            })
    if seen != "any":
        items = [i for i in items if i["seen_state"] == seen]
    total = len(items)
    hidden = {}
    with _db() as db:
        for other in ("movie", "series"):
            if other in kinds:
                continue
            n = db.execute("SELECT count(*) FROM title WHERE kind = ?", (other,)).fetchone()[0]
            if n:
                hidden[other] = n
    return {"kinds": kinds, "total": total, "hidden": hidden, "limit": limit, "offset": offset,
            "items": items[offset : offset + limit]}


@app.get("/api/titles/{title_id}")
def title_detail(title_id: int) -> dict[str, Any]:
    with _db() as db:
        t = db.execute("SELECT * FROM title WHERE id = ?", (title_id,)).fetchone()
        if not t:
            raise HTTPException(404, "no such title")
        credits = [
            {"person_id": r["person_id"], "name": r["name"], "department": r["department"],
             "job": r["job"], "ord": r["ord"], "character": r["character"],
             "sources": sorted({s for s in r["sources"].split(",")})}
            for r in db.execute(
                "SELECT c.person_id, p.name, c.department, c.job, min(c.ord) AS ord,"
                " max(c.character) AS character, group_concat(DISTINCT c.source) AS sources"
                " FROM credit c JOIN person p ON p.id = c.person_id WHERE c.title_id = ?"
                " GROUP BY c.person_id, p.name, c.department, c.job", (title_id,)
            ).fetchall()
        ]
        extracted = []
        for g in db.execute("SELECT * FROM dna_tag WHERE title_id = ?", (title_id,)).fetchall():
            ev = db.execute(
                "SELECT quote, source FROM dna_evidence WHERE dna_tag_id = ?", (g["id"],)
            ).fetchall()
            extracted.append({
                "term": g["term"], "facet": g["facet"], "salience": g["salience"],
                "confidence": g["confidence"], "n_sources": g["n_sources"],
                "provider": g["provider"],
                "evidence": [{"quote": e["quote"], "source": e["source"]} for e in ev],
            })
        projected = [
            {"term": r["term"], "facet": r["facet"], "weight": r["weight"], "via": r["via"]}
            for r in db.execute(
                "SELECT * FROM dna_projected WHERE title_id = ?", (title_id,)
            ).fetchall()
        ]
        ratings = [
            {"platform": r["platform"], "score": r["score"], "votes": r["votes"]}
            for r in db.execute(
                "SELECT * FROM platform_rating WHERE title_id = ?", (title_id,)
            ).fetchall()
        ]

    return {
        "title": {
            "id": t["id"], "kind": t["kind"], "name": t["name"],
            "original_name": t["original_name"], "year": t["year"],
            "runtime_min": t["runtime_min"], "overview": t["overview"], "tagline": None,
            "poster_path": None, "backdrop_path": None, "trailer_key": None,
            "is_owned": True, "placement": _placement(t["id"]),
            "seen_state": _seen_state(t["id"]),
            "imdb_id": t["imdb_id"], "tmdb_id": t["tmdb_id"],
        },
        "credits": credits,
        "platform_ratings": {
            "display_only": True,
            "note": "display-only schema — platform scores are a popularity conduit and are "
                    "never model features",
            "items": ratings,
        },
        "dna": {"extracted": extracted, "projected": projected,
                "note": "extracted tags are quote-verified; projected tags are inferred"},
        "model_line": {"available": False, "reason": "dev harness — no artifact bundle loaded"},
        "actions": {"play_on_jellyfin": None, "show_on_map": {"title_id": title_id}},
    }


@app.get("/api/facets")
def facets(kind: list[str] = Query(default=["movie"])) -> dict[str, Any]:
    kinds = [k for k in ("movie", "series") if k in kind] or ["movie"]
    marks = ",".join("?" * len(kinds))
    with _db() as db:
        genres = [
            r[0] for r in db.execute(
                "SELECT DISTINCT g.genre FROM title_genre g JOIN title t ON t.id = g.title_id"
                f" WHERE t.kind IN ({marks}) ORDER BY 1", kinds
            )
        ]
        decades = [
            r[0] for r in db.execute(
                f"SELECT DISTINCT (year/10)*10 FROM title WHERE kind IN ({marks})"
                " AND year IS NOT NULL ORDER BY 1 DESC", kinds
            )
        ]
    return {"kinds": kinds, "genres": genres, "decades": decades}


@app.get("/api/people/{person_id}")
def person(person_id: int) -> dict[str, Any]:
    with _db() as db:
        p = db.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
        if not p:
            raise HTTPException(404, "no such person")
        films = [
            dict(r) for r in db.execute(
                "SELECT t.id, t.kind, t.name, t.year FROM credit c JOIN title t ON t.id = c.title_id"
                " WHERE c.person_id = ? GROUP BY t.id", (person_id,)
            )
        ]
    return {"person": {"id": p["id"], "name": p["name"], "profile_path": None},
            "filmography": films}


# --- M1: passkeys, seen state, finish prompts, Jellyfin ----------------------
#
# Dumb on purpose. The passkey routes here answer with shapes, not ceremonies: WebAuthn cannot
# be faked usefully without a real authenticator, and pretending otherwise would teach the UI
# that a passkey always works. `tests/test_webauthn.py` runs the real ceremony against a
# software authenticator; this only keeps the harness from 404-ing on the paths the UI calls.


class PasskeyRegisterVerify(BaseModel):
    ceremony_id: str
    credential: dict
    label: str | None = None


class PasskeyLoginOptions(BaseModel):
    name: str | None = None


class PasskeyLoginVerify(BaseModel):
    ceremony_id: str
    credential: dict
    device_label: str | None = None


class StateRequest(BaseModel):
    state: str


class PromptAnswer(BaseModel):
    finished: bool


class JellyfinSettings(BaseModel):
    url: str = ""
    api_key: str = ""


class LinkRequest(BaseModel):
    jellyfin_user_id: str
    jellyfin_username: str | None = None
    jellyfin_password: str | None = None


@app.post("/api/auth/passkey/register/options")
def passkey_register_options(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    _me(spielplan_session)
    raise HTTPException(501, "the dev harness has no authenticator — run the real stack")


@app.post("/api/auth/passkey/register")
def passkey_register(
    body: PasskeyRegisterVerify, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    raise HTTPException(501, "the dev harness has no authenticator — run the real stack")


@app.get("/api/auth/passkey/credentials")
def passkey_credentials(spielplan_session: str | None = Cookie(default=None)) -> list[dict[str, Any]]:
    _me(spielplan_session)
    return []


@app.delete("/api/auth/passkey/credentials/{credential_id:path}")
def passkey_delete(credential_id: str) -> dict[str, bool]:
    raise HTTPException(404, "no such passkey on this account")


@app.post("/api/auth/passkey/login/options")
def passkey_login_options(body: PasskeyLoginOptions) -> dict[str, Any]:
    raise HTTPException(501, "the dev harness has no authenticator — run the real stack")


@app.post("/api/auth/passkey/login")
def passkey_login(body: PasskeyLoginVerify) -> dict[str, Any]:
    raise HTTPException(501, "the dev harness has no authenticator — run the real stack")


@app.get("/api/titles/{title_id}/state")
def get_state(title_id: int) -> dict[str, Any]:
    return {"state": STATE["seen"].get(title_id, "unseen"), "state_changed_at": None,
            "jf_synced_at": None}


@app.post("/api/titles/{title_id}/state")
def set_state(title_id: int, body: StateRequest) -> dict[str, Any]:
    if body.state not in ("seen", "unseen"):
        raise HTTPException(400, "state must be one of seen, unseen")
    STATE["seen"][title_id] = body.state
    return {"state": body.state, "synced": False, "reason": "Jellyfin not configured"}


@app.get("/api/prompts/finish")
def finish_prompts() -> list[dict[str, Any]]:
    return []


@app.post("/api/prompts/finish/{event_id}")
def answer_finish_prompt(event_id: int, body: PromptAnswer) -> dict[str, Any]:
    raise HTTPException(404, "no open prompt with that id")


@app.get("/api/admin/connectors/jellyfin")
def get_jellyfin() -> dict[str, Any]:
    # Like the real route, this never returns the key itself (§14.3) — only whether one is set.
    return dict(STATE["jellyfin"])


@app.put("/api/admin/connectors/jellyfin")
def put_jellyfin(body: JellyfinSettings) -> dict[str, Any]:
    STATE["jellyfin"] = {
        "url": body.url,
        "has_api_key": bool(body.api_key) or STATE["jellyfin"]["has_api_key"],
        "configured": bool(body.url) and (bool(body.api_key) or STATE["jellyfin"]["has_api_key"]),
        "library_ids": [],
        "linked_users": 0,
    }
    return STATE["jellyfin"]


@app.post("/api/admin/connectors/jellyfin/test")
def test_jellyfin() -> dict[str, Any]:
    return {"ok": False, "error": "the dev harness has no Jellyfin", "status": None}


@app.get("/api/admin/connectors/jellyfin/users")
def jellyfin_users() -> list[dict[str, Any]]:
    return []


@app.post("/api/admin/connectors/jellyfin/sync")
def jellyfin_sync() -> dict[str, Any]:
    return {"pushed": 0, "adopted": 0, "unchanged": 0, "needs_relink": [], "resolve": {},
            "users": [], "skipped_no_link": True}


@app.post("/api/admin/connectors/jellyfin/poll")
def jellyfin_poll() -> dict[str, Any]:
    return {"armed": 0, "already_armed": 0, "watching": 0, "unresolved": [],
            "skipped_no_link": True}


@app.get("/api/admin/users")
def admin_users() -> list[dict[str, Any]]:
    return [
        {**u, "is_active": True, "jellyfin_user_id": None, "jellyfin_link_state": None,
         "has_jellyfin_token": False}
        for u in STATE["users"].values()
    ]


@app.post("/api/admin/users/{user_id}/jellyfin")
def link_jellyfin(user_id: int, body: LinkRequest) -> dict[str, Any]:
    raise HTTPException(409, "Jellyfin is not configured — set its URL and API key first")


@app.delete("/api/admin/users/{user_id}/jellyfin")
def unlink_jellyfin(user_id: int) -> dict[str, bool]:
    return {"ok": True}


# --- M2: shared reading of the fixture catalog -------------------------------
#
# Everything below answers out of the bundle `tests/fixtures/make_bundle.py` writes, plus the
# in-memory journal. Where the real app reads a fitted number, this reads an INVENTED one —
# see `_scores`. Where the real app decides something, the decision is imported from the real
# module rather than restated: `rate_session.card_type_for` and `.advance` run the block
# machine, `balance.ClassBalance.of` renders the widget, `queue.reason_for` and
# `battle.reason_for` write the why-lines, `shelves.greeting` picks the band, and
# `rail.redact` is decision 117's gate. A harness that re-derived any of those would drift
# from the app on exactly the properties the front end is built against.

KINDS: tuple[str, ...] = ("movie", "series")
NOW_YEAR = datetime.now().year
HP = Hyperparams()          # §4.3's real margins — 1.6 decisive, 1.0 hesitant
RNG = random.Random(20260830)


def _catalog() -> list[dict[str, Any]]:
    """Every title in the fixture bundle, read once."""
    cached = STATE.get("catalog")
    if cached is None:
        with _db() as db:
            cached = [
                dict(r) for r in db.execute(
                    "SELECT id, kind, name, year, runtime_min, overview FROM title ORDER BY id"
                )
            ]
        STATE["catalog"] = cached
    return cached


def _title(title_id: int) -> dict[str, Any] | None:
    return next((t for t in _catalog() if t["id"] == title_id), None)


def _genres_of(title_id: int) -> list[str]:
    """The fixture's genres per title, cached. `_catalog` does not select them because §6.0's
    grid never needed them; §6.3's genre filter does."""
    cache = STATE.get("genres")
    if cache is None:
        with _db() as db:
            cache = {}
            for row in db.execute("SELECT title_id, genre FROM title_genre"):
                cache.setdefault(row["title_id"], []).append(row["genre"])
        STATE["genres"] = cache
    return cache.get(title_id, [])


def _verdicts(user_id: int) -> dict[int, int]:
    """The person's current label per title — the harness's `rate.LIVE_LABEL`."""
    return STATE["verdicts"].setdefault(user_id, {})


def _label_counts(user_id: int, kinds: Sequence[str]) -> list[int]:
    counts = [0, 0, 0]
    for title_id, value in _verdicts(user_id).items():
        title = _title(title_id)
        if title is not None and title["kind"] in kinds:
            counts[value] += 1
    return counts


def _dna() -> dict[int, list[tuple[WhyTerm, float]]]:
    """The two DNA tiers, ranked the way `home.why.TERM_RANK` ranks them.

    §4.1 rule 1: a (title, term) pair may exist in both tiers and must stay distinguishable.
    So a term carried by both is returned once and tiered `extracted` — the pool cannot
    silently promote an inferred tag — and the rank is the larger of the two.
    """
    cached = STATE.get("dna")
    if cached is not None:
        return cached
    ranked: dict[int, dict[str, tuple[WhyTerm, float]]] = {}

    def offer(title_id: int, term: str, facet: str, tier: str, rank: float) -> None:
        slot = ranked.setdefault(title_id, {})
        held = slot.get(term)
        if held is None:
            slot[term] = (WhyTerm(term=term, facet=facet, tier=tier), rank)
            return
        best_tier = "extracted" if "extracted" in (held[0].tier, tier) else "projected"
        slot[term] = (WhyTerm(term=term, facet=facet, tier=best_tier), max(held[1], rank))

    with _db() as db:
        for r in db.execute("SELECT title_id, term, facet, salience FROM dna_tag"):
            offer(r["title_id"], r["term"], r["facet"], "extracted",
                  0.60 + 0.40 * ((r["salience"] or 1.0) / 3.0))
        for r in db.execute("SELECT title_id, term, facet, weight FROM dna_projected"):
            offer(r["title_id"], r["term"], r["facet"], "projected", 0.30 * (r["weight"] or 0.5))

    cached = {
        title_id: sorted(slot.values(), key=lambda pair: (-pair[1], pair[0].term))
        for title_id, slot in ranked.items()
    }
    STATE["dna"] = cached
    return cached


def _terms_for(title_id: int) -> list[WhyTerm]:
    return [term for term, _rank in _dna().get(title_id, [])]


def _carries(title_id: int, term: str) -> bool:
    return any(t.term == term for t in _terms_for(title_id))


def _common_terms(title_ids: Sequence[int], limit: int = NAMED_TERM_CAP) -> list[WhyTerm]:
    """The terms carried by EVERY one of these titles, best-named first — `why.common_terms`.

    Computed by intersection over the cards actually returned, so a vocabulary clause built
    from it cannot be false (§6.8)."""
    ids = sorted({int(t) for t in title_ids})
    if not ids:
        return []
    pooled: dict[str, tuple[WhyTerm, float, int]] = {}
    for title_id in ids:
        for term, rank in _dna().get(title_id, []):
            held, top, seen = pooled.get(term.term, (term, 0.0, 0))
            tier = "extracted" if "extracted" in (held.tier, term.tier) else "projected"
            pooled[term.term] = (
                WhyTerm(term=term.term, facet=term.facet, tier=tier), max(top, rank), seen + 1
            )
    everywhere = [(t, rank) for t, rank, n in pooled.values() if n == len(ids)]
    everywhere.sort(key=lambda pair: (-pair[1], pair[0].term))
    return [t for t, _rank in everywhere[:limit]]


def _scores(user_id: int, kind: str) -> dict[int, float]:
    """INVENTED, deterministic, per (user, kind). Only the SHAPE is a contract.

    There is no Ledger here — no fold-in, no MAP fit, no Backbone arithmetic — so a harness
    that returned `null` for every model number would teach the front end that the tier badge
    and the §6.7 annotations never render, which is as wrong as leaking one that will never
    arrive. These are stable pseudo-random numbers standing where a fitted `user_score.score`
    would be, and every quantity derived from them (`cdf`, `tier`, `s`) is derived by the same
    arithmetic the real code uses.
    """
    cache = STATE["scores"].get((user_id, kind))
    if cache is None:
        cache = {
            t["id"]: random.Random(f"{user_id}:{kind}:{t['id']}").random()
            for t in _catalog() if t["kind"] == kind
        }
        STATE["scores"][(user_id, kind)] = cache
    return cache


def _cdfs(user_id: int, kind: str) -> dict[int, float]:
    """§5.2's 0..1 weight: "the empirical CDF of the user's own fitted `s` values, computed
    per kind". Postgres' `percent_rank()`, in Python."""
    scores = _scores(user_id, kind)
    order = sorted(scores, key=lambda title_id: (scores[title_id], title_id))
    n = len(order)
    return {title_id: (0.0 if n < 2 else i / (n - 1)) for i, title_id in enumerate(order)}


def _tier_index(cdf: float, tier_set: Sequence[str]) -> int:
    return min(int(cdf * len(tier_set)), len(tier_set) - 1)


def _beta(user_id: int, kind: str) -> tuple[float, bool]:
    """(β, fitted?). §5.1's optimum is 0.8, but a profile the fold-in has never touched was
    ranked by the crowd prior alone, i.e. at β 0 — printing 0.80 there is the decorative
    why-line §6.0 forbids. The harness has no nightly job, so five labels of the kind stand in
    for "the fold-in has run"."""
    if sum(_label_counts(user_id, [kind])) >= 5:
        return shelves.DEFAULT_BETA, True
    return 0.0, False


# --- M2: the Rate surface (§6.1, §6.7, §13, decision 35) ---------------------
#
# Four disciplines are taken from `spielplan/rate/session.py`, because they are the properties
# the front end is built against:
#
#   1. THE CARD IS THE SERVER'S. Every write names a `card_token`, never a title id. A token
#      that is not the current one is a 409 carrying a reason — which is also the double-tap
#      guard, and the reason string the chip renders.
#   2. NO PREDICTION BEFORE THE TAP (§6.1; anchoring, Cosley 2003). `_public_card` is an
#      ALLOW-LIST built field by field. It never copies the stored card and deletes keys: a
#      deny-list leaks the first time a field is added, and the two fields it would leak are
#      §13's re-ask reference and the pair's verdict band.
#   3. THE CARD TYPE IS A FUNCTION OF THE SLOT, never of the last card served, and the counter
#      runs 1..15 and rolls. Both come from `rate_session.card_type_for` / `.advance`.
#   4. A CORRECTION DOES NOT ADVANCE. It repairs the question rather than answering it.
#
# The reveal is the one place the harness invents a belief, and it invents it strictly after
# the tap: the `cdf` is `_scores`', the BANDING around it is `predicted_class`'s real
# arithmetic (the person's own live label counts cut their own axis), and the sentence is
# built by the real `rate_session.reveal_for`.


def _new_rate_session(user_id: int, kinds: Sequence[str]) -> dict[str, Any]:
    return {
        "id": user_id, "user_id": user_id, "kinds": list(kinds), "mode": "mix",
        "decisive": False, "block_index": 0, "slot": 1, "seq": 0,
        "current_card": None, "card_token": None, "observations": [],
    }


def _rate(user: dict[str, Any]) -> dict[str, Any]:
    """One live session per person — `rate_session_one_live`, in a dict."""
    s = STATE["rate"].get(user["id"])
    if s is None:
        s = _new_rate_session(user["id"], KINDS)
        STATE["rate"][user["id"]] = s
    return s


def _observed_title_ids(s: dict[str, Any]) -> set[int]:
    return {t for o in s["observations"] if not o["undone"] for t in o["title_ids"]}


def _skipped_title_ids(s: dict[str, Any]) -> set[int]:
    return {
        t for o in s["observations"]
        if not o["undone"] and o["kind_of"] == "skip" for t in o["title_ids"]
    }


def _features(title: dict[str, Any]) -> tuple[queue.Features, int]:
    """§6.1's P(seen) features over what the harness actually knows: the title is owned and it
    has an age. No Jellyfin history, no crowd counts, no household co-seen — so the why-line
    names `owned` or `age` and never claims a signal that is not there."""
    years_out = max(0, NOW_YEAR - (title["year"] or NOW_YEAR))
    return (
        queue.Features(
            seen=_seen_state(title["id"]) == "seen",
            owned=True,
            age=min(years_out / queue.AGE_SATURATION_YEARS, 1.0),
        ),
        years_out,
    )


def _sweep_card(title: dict[str, Any], *, source: str) -> dict[str, Any]:
    features, years_out = _features(title)
    return {
        "type": "sweep",
        "kind": title["kind"],
        "title_id": title["id"],
        "reason": queue.reason_for(features, source=source, years_out=years_out),
        "p_seen": None if source == "seed" else queue.p_seen(features),
        # §13: `source` and `reask_of` are both re-ask markers. They live on the card the
        # server holds and are never projected — see `_public_card`.
        "source": source,
        "reask_of": None,
        "substituted_for": None,
    }


def _draw_sweep(s: dict[str, Any], *, exclude: set[int], head: Sequence[int]) -> dict | None:
    """§6.1's queue, P(seen)-ordered, with §6.0's banner head pinned to the front."""
    verdicts = _verdicts(s["user_id"])
    pool = [t for t in _catalog() if t["kind"] in s["kinds"] and t["id"] not in exclude]
    if not pool:
        return None
    pinned = {title_id: i for i, title_id in enumerate(head)}

    def rank(title: dict[str, Any]) -> tuple[Any, ...]:
        seen = _seen_state(title["id"]) == "seen"
        pending = seen and title["id"] not in verdicts
        return (
            pinned.get(title["id"], len(pinned)),
            0 if pending else 1,
            -queue.p_seen(_features(title)[0]),
            title["id"],
        )

    title = min(pool, key=rank)
    seen = _seen_state(title["id"]) == "seen"
    return _sweep_card(
        title, source="pending_verdict" if seen and title["id"] not in verdicts else "p_seen"
    )


def _draw_battle(s: dict[str, Any], *, exclude: set[int]) -> dict[str, Any] | None:
    """§6.1: "Pairs drawn **at random** from the user's seen titles within verdict bands."

    None when no (kind, class) stratum holds two members — which is what makes the
    substitution in `_ensure_card` reachable rather than theoretical.
    """
    verdicts = _verdicts(s["user_id"])
    strata: dict[tuple[str, int], list[int]] = {}
    for title in _catalog():
        title_id = title["id"]
        if title["kind"] not in s["kinds"] or title_id in exclude:
            continue
        if title_id not in verdicts or _seen_state(title_id) != "seen":
            continue
        strata.setdefault((title["kind"], verdicts[title_id]), []).append(title_id)
    eligible = sorted(k for k, members in strata.items() if len(members) >= 2)
    if not eligible:
        return None
    kind, verdict_class = RNG.choice(eligible)
    title_a, title_b = RNG.sample(sorted(strata[(kind, verdict_class)]), 2)
    return {
        "type": "battle",
        "kind": kind,
        "title_a": title_a,
        "title_b": title_b,
        # The band the pair was drawn from. Server-side only: a card that carried it would
        # hand the person their own prior label back before they answered.
        "verdict_class": verdict_class,
        "reason": battle.reason_for(verdict_class),
        "reask_of": None,
        "substituted_for": None,
    }


def _stash(s: dict[str, Any], card: dict[str, Any] | None) -> dict[str, Any]:
    """The card and its token move together — `rate_session_card_has_token` as an invariant
    rather than a reminder."""
    s["current_card"] = card
    s["card_token"] = str(uuid.uuid4()) if card is not None else None
    return s


def _ensure_card(s: dict[str, Any], *, head: Sequence[int] = ()) -> dict[str, Any]:
    """Idempotent: draws only when the table is empty, because a GET that redrew would make
    "next card preloaded" a lie.

    The substitution rule is the wrinkle: when the slot calls for a battle and no verdict band
    yet holds two titles, a sweep is served in its place and THE SLOT IS NOT CHANGED — so
    alternation resumes by itself rather than the surface silently becoming Sweep-only.
    """
    if s["current_card"] is not None:
        return s
    served = _observed_title_ids(s)
    skipped = _skipped_title_ids(s)
    wanted = rate_session.card_type_for(s["mode"], s["slot"])
    card: dict[str, Any] | None
    if wanted == "battle":
        card = _draw_battle(s, exclude=skipped)
        if card is None and s["mode"] != "battle":
            card = _draw_sweep(s, exclude=served, head=head)
            if card is not None:
                card["substituted_for"] = "battle"
    else:
        card = _draw_sweep(s, exclude=served, head=head)
        if card is None and s["mode"] != "sweep":
            card = _draw_battle(s, exclude=skipped)
            if card is not None:
                card["substituted_for"] = "sweep"
    return _stash(s, card)


def _card_title(title_id: int) -> dict[str, Any] | None:
    """The poster-forward card of §6.8 and nothing else: no score, no tier, no placement —
    a badge that says "no crowd data yet" is still a statement about the model."""
    title = _title(title_id)
    if title is None:
        return None
    return {
        "id": title["id"], "kind": title["kind"], "name": title["name"], "year": title["year"],
        "runtime_min": title["runtime_min"], "poster_path": None,
        # The real truncation, not a second one: §6.1's task on a sweep card is "did you see
        # this?", so the aid is the plot logline, never "cleaned" (§4.1 rule 8).
        "recall_aid": rate_session._recall_aid(title["overview"]),
    }


def _public_card(s: dict[str, Any]) -> dict[str, Any] | None:
    """§6.1's allow-list, field by field — the construction of `rate.session.public_card`."""
    card = s["current_card"]
    if card is None or s["card_token"] is None:
        return None
    if card["type"] == "sweep":
        return {
            "type": "sweep",
            "token": s["card_token"],
            "kind": card["kind"],
            "title": _card_title(card["title_id"]),
            "reason": card["reason"],
            "p_seen": card.get("p_seen"),
            "substituted_for": card.get("substituted_for"),
            # §6.8 / proposal 52: lowercase, worst -> best, matching the stored ordinal.
            "verdict_labels": [[i, label] for i, label in enumerate(VERDICT_LABELS)],
            "controls": ["verdict", "not_seen", "skip"],
        }
    return {
        "type": "battle",
        "token": s["card_token"],
        "kind": card["kind"],
        "left": {**(_card_title(card["title_a"]) or {}), "outcome": "A"},
        "right": {**(_card_title(card["title_b"]) or {}), "outcome": "B"},
        "reason": card["reason"],
        "substituted_for": card.get("substituted_for"),
        "outcomes": list(rate_session.OUTCOMES),
        "corrections": {"label": "not seen", "sides": ["left", "both", "right"]},
        "controls": ["duel", "correction", "skip"],
    }


def _stale(reason: str) -> HTTPException:
    return HTTPException(409, detail={
        "reason": reason,
        "message": {
            "no_card": "there is no card on the table",
            "stale_card": "that card has already been answered",
            "wrong_card_type": "that answer does not fit the card on the table",
        }.get(reason, "the card token is not current"),
    })


def _take_card(s: dict[str, Any], token: str, *, want: str) -> dict[str, Any]:
    if s["current_card"] is None or s["card_token"] is None:
        raise _stale("no_card")
    if s["card_token"] != token:
        raise _stale("stale_card")
    if s["current_card"]["type"] != want:
        raise _stale("wrong_card_type")
    return s["current_card"]


def _append(
    s: dict[str, Any],
    *,
    kind_of: str,
    card: dict[str, Any],
    title_ids: Sequence[int],
    prior: Sequence[dict[str, Any]] = (),
    row_id: int | None = None,
) -> None:
    """One journal row, then the cursor moves — decision 35's "observation journal with
    compensating writes rather than a lastAction variable".

    `advances` is derived from `kind_of`, which is `rate_observation_advances_rule` in DDL: a
    correction is a repair, not an observation, so the counter the person is reading does not
    move.
    """
    advances = kind_of != "correction"
    block_index, slot = (
        rate_session.advance(s["block_index"], s["slot"]) if advances
        else (s["block_index"], s["slot"])
    )
    s["seq"] += 1
    s["observations"].append({
        "seq": s["seq"], "block_index": s["block_index"], "slot": s["slot"],
        "kind_of": kind_of, "advances": advances, "card": card,
        "title_ids": list(title_ids), "prior": [dict(p) for p in prior],
        "row_id": row_id, "undone": False,
    })
    s["block_index"], s["slot"] = block_index, slot
    s["current_card"], s["card_token"] = None, None


def _prior_of(user_id: int, title_id: int) -> dict[str, Any]:
    """What Undo has to put back: the seen state and the label, exactly as they stand now."""
    return {
        "title_id": title_id,
        "state": STATE["seen"].get(title_id),
        "verdict": _verdicts(user_id).get(title_id),
    }


def _next_row_id() -> int:
    STATE["next_row_id"] += 1
    return STATE["next_row_id"] - 1


def _sync_line(state: str, reason: str = "Jellyfin not configured") -> str:
    """§6.7's rail reports what actually happened, never a write that did not happen. The dev
    harness has no Jellyfin, so the §7.3 push is always owed rather than made."""
    return f"user_title.state = {state} -> not pushed ({reason})"


def _undo_availability(s: dict[str, Any]) -> dict[str, Any]:
    """Decision 35: "the chip disables visibly at the boundary"."""
    live = [o for o in s["observations"] if not o["undone"]]
    if not live:
        return {"available": False, "kind": None, "reason": "empty"}
    if live[-1]["block_index"] != s["block_index"]:
        return {"available": False, "kind": None, "reason": "block_boundary"}
    return {"available": True, "kind": live[-1]["kind_of"], "reason": None}


def _class_balance(s: dict[str, Any]) -> dict[str, Any]:
    """§5.2's measured 5x lever, rendered by `balance`'s own projection so the widget's copy
    and its 60% threshold have exactly one home."""
    return balance.ClassBalance.of(_label_counts(s["user_id"], s["kinds"])).as_dict()


def _rate_payload(
    s: dict[str, Any],
    *,
    reveal: dict[str, Any] | None = None,
    log: Sequence[str] = (),
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One envelope for every route, carrying the next card — §6 preamble's "next card
    preloaded"."""
    card = _public_card(s)
    return {
        "session": {
            "id": s["id"], "mode": s["mode"], "kinds": s["kinds"], "decisive": s["decisive"],
            "block": {
                "index": s["block_index"], "slot": s["slot"], "size": rate_session.BLOCK_SIZE,
                "counter": f"{s['slot']} / {rate_session.BLOCK_SIZE}",
                "serving": rate_session.card_type_for(s["mode"], s["slot"]),
            },
        },
        "card": card,
        "drained": None if card else rate_session.DRAINED,
        "class_balance": _class_balance(s),
        "undo": _undo_availability(s),
        "reveal": reveal,
        "ledger": ledger,
        "log": list(log),
    }


def _prediction(user_id: int, title_id: int, kind: str) -> dict[str, Any]:
    """What the model would have guessed — READ BEFORE THE WRITE, served after it.

    The banding is `rate.session.predicted_class`'s, unchanged: the person's own three-class
    habit says where the cuts on their own axis fall, so a labeller who calls 20% of what they
    watch disliked has their disliked band at the bottom 20% of their ranking.
    """
    counts = _label_counts(user_id, [kind])
    total = sum(counts)
    if total == 0:
        return {"available": False, "reason": "no labels of your own to band against yet"}
    cdf = _cdfs(user_id, kind).get(title_id)
    if cdf is None:
        return {
            "available": False,
            "reason": "no fitted ranking for this title yet — rate a few more first",
        }
    low, high = counts[0] / total, (counts[0] + counts[1]) / total
    guess = 0 if cdf < low else (1 if cdf < high else 2)
    return {
        "available": True, "predicted": guess, "predicted_label": VERDICT_LABELS[guess],
        "cdf": cdf, "s": round((cdf - 0.5) * 4.0, 4), "label_count": total,
    }


def _ledger_delta(user_id: int, kind: str, title_ids: Sequence[int]) -> dict[str, Any]:
    """§5.3's "<50 ms" row. Invented like every other number here; the shape is the contract."""
    tier_set = shelves.DEFAULT_TIER_SET
    cdfs = _cdfs(user_id, kind)
    return {
        "applied": True,
        "kind": kind,
        "refit": False,
        "ms": 3.1,
        "rows": [
            {"title_id": t, "cdf": cdfs.get(t), "tier": _tier_index(cdfs.get(t, 0.0), tier_set)}
            for t in title_ids
        ],
    }


def _rail_record(kind: str, line: str, *, user_id: int | None = None,
                 title_id: int | None = None) -> None:
    """§6.7's journal — THE REAL WRITER, not a copy.

    `rail` keeps its ring buffer in the process rather than in Postgres ("never persisted"), so
    the harness can call it verbatim. That is the whole point: the event shape, the per-user
    scope and the refusal of an unknown kind are the app's, and there is no second projection
    here to drift from `rail.recent`'s.
    """
    rail.record(kind=kind, line=line, user_id=user_id, title_id=title_id,
                bundle_version=_bundle_version())


def _rail_recent(user_id: int, limit: int = rail.RAIL_LIMIT) -> list[dict[str, Any]]:
    """This person's events and the household's, newest first — never another person's."""
    return rail.recent(user_id=user_id, limit=limit)


Head = Annotated[list[int], Field(default_factory=list)]


class ControlsBody(BaseModel):
    mode: Literal["mix", "sweep", "battle"] | None = None
    kinds: list[Literal["movie", "series"]] | None = None
    decisive: bool | None = None
    restart: bool = False
    head: Head


class VerdictBody(BaseModel):
    card_token: str
    value: Literal[0, 1, 2]
    latency_ms: int | None = None
    head: Head


class CardBody(BaseModel):
    card_token: str
    latency_ms: int | None = None
    head: Head


class DuelBody(BaseModel):
    card_token: str
    outcome: Literal["A", "B", "TIE"]
    decisive: bool | None = None
    latency_ms: int | None = None
    head: Head


class CorrectionBody(BaseModel):
    card_token: str
    side: Literal["left", "both", "right"]


# PEP 563 (`from __future__ import annotations`) leaves the bodies above as strings, and
# pydantic resolves them through `sys.modules[cls.__module__]`. `tests/test_devstub_contract.py`
# loads this file with `spec_from_file_location`, which never registers it there, so the
# schemas would be "not fully defined" the moment that test asks for `app.openapi()`. Rebuilt
# here, from module scope, where the parent frame IS this module's namespace.
for _body in (ControlsBody, VerdictBody, CardBody, DuelBody, CorrectionBody):
    _body.model_rebuild(force=True)


@app.get("/api/rate")
def rate_current(
    spielplan_session: str | None = Cookie(default=None), head: list[int] = Query(default=[])
) -> dict[str, Any]:
    return _rate_payload(_ensure_card(_rate(_me(spielplan_session)), head=head))


@app.post("/api/rate/session")
def rate_controls(
    body: ControlsBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    """§6.1's mode and kind controls, plus the persistent decisive toggle. A fresh session
    opens in Mix, so every entry point lands on the same card type."""
    user = _me(spielplan_session)
    try:
        kinds = normalise_kinds(body.kinds) if body.kinds is not None else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.restart:
        STATE["rate"].pop(user["id"], None)
    s = _rate(user)
    # Changing the mode or the kinds drops the card on the table — a battle pair is meaningless
    # once Sweep is selected, and a film pair is meaningless once Films is switched off. The
    # decisive toggle does not: it changes the WEIGHT of the next answer, not the question.
    wanted = kinds if kinds is not None else s["kinds"]
    redraw = (body.mode is not None and body.mode != s["mode"]) or wanted != s["kinds"]
    s["mode"] = body.mode or s["mode"]
    s["kinds"] = wanted
    if body.decisive is not None:
        s["decisive"] = body.decisive
    if redraw:
        _stash(s, None)
    return _rate_payload(_ensure_card(s, head=body.head))


@app.delete("/api/rate/session")
def rate_end(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Close the live session. The journal stays: §4.2 is append-only and the rows are the
    record of what the person actually said."""
    user = _me(spielplan_session)
    return {"ended": STATE["rate"].pop(user["id"], None) is not None}


@app.post("/api/rate/verdict")
def rate_verdict(
    body: VerdictBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    """§6.1's `Liked / Fine / Disliked`, and "Verdict implies `seen`".

    The reveal rides on this response and on no other — §6.1's anchoring rule expressed as a
    route: the card carried no belief, the answer to the card carries it.
    """
    user = _me(spielplan_session)
    s = _rate(user)
    card = _take_card(s, body.card_token, want="sweep")
    title_id, kind = card["title_id"], card["kind"]
    # Strictly first: the reveal is what the model believed BEFORE this label existed.
    prediction = _prediction(user["id"], title_id, kind)
    prior = _prior_of(user["id"], title_id)
    implied_seen = prior["state"] != "seen"
    _verdicts(user["id"])[title_id] = body.value
    STATE["seen"][title_id] = "seen"
    row_id = _next_row_id()
    _append(s, kind_of="verdict", card=card, title_ids=[title_id], prior=[prior], row_id=row_id)
    ledger = _ledger_delta(user["id"], kind, [title_id])
    _rail_record(
        "verdict",
        rail.verdict_line(user["name"], _title(title_id)["name"],
                          VERDICT_LABELS[body.value], refit_ms=ledger["ms"]),
        user_id=user["id"], title_id=title_id,
    )
    log = (
        f"verdict(title {title_id}) = {VERDICT_LABELS[body.value]} -> ordered-logit arm"
        + (" · implies seen" if implied_seen else ""),
        _sync_line("seen"),
    )
    return _rate_payload(
        _ensure_card(s, head=body.head),
        reveal=rate_session.reveal_for(prediction, body.value),
        log=log,
        ledger=ledger,
    )


@app.post("/api/rate/not-seen")
def rate_not_seen(
    body: CardBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    """§6.1's one seen-state control. Owner decision 2026-08-29: a title you cannot remember is
    plain `unseen`, and the verdict and duel rows survive the flip (§4.2)."""
    user = _me(spielplan_session)
    s = _rate(user)
    card = _take_card(s, body.card_token, want="sweep")
    title_id = card["title_id"]
    prior = _prior_of(user["id"], title_id)
    STATE["seen"][title_id] = "unseen"
    _append(s, kind_of="not_seen", card=card, title_ids=[title_id], prior=[prior])
    return _rate_payload(
        _ensure_card(s, head=body.head),
        log=(f"not_seen(title {title_id}) -> state unseen, no observation row",
             _sync_line("unseen")),
    )


@app.post("/api/rate/skip")
def rate_skip(
    body: CardBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    """`Skip` writes nothing to any arm. The journal row IS the suppression, and it is not a
    `not_seen`, so §13's not-seen-rate instrument does not count it."""
    user = _me(spielplan_session)
    s = _rate(user)
    if s["current_card"] is None or s["card_token"] is None:
        raise _stale("no_card")
    if s["card_token"] != body.card_token:
        raise _stale("stale_card")
    card = s["current_card"]
    titles = (
        [card["title_id"]] if card["type"] == "sweep" else [card["title_a"], card["title_b"]]
    )
    _append(s, kind_of="skip", card=card, title_ids=titles)
    return _rate_payload(
        _ensure_card(s, head=body.head), log=("skipped — no observation row written",)
    )


@app.post("/api/rate/duel")
def rate_duel(
    body: DuelBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    """§6.1's battle answer, `Tie` included — one duel row, never a dropped one. §4.2: "about
    the same" is first-class data (22% of random pairs are genuine ties)."""
    user = _me(spielplan_session)
    s = _rate(user)
    card = _take_card(s, body.card_token, want="battle")
    hard = s["decisive"] if body.decisive is None else body.decisive
    margin = HP.margin_for(hard)
    row_id = _next_row_id()
    _append(
        s, kind_of="tie" if body.outcome == "TIE" else "duel", card=card,
        title_ids=[card["title_a"], card["title_b"]], row_id=row_id,
    )
    ledger = _ledger_delta(user["id"], card["kind"], [card["title_a"], card["title_b"]])
    _rail_record(
        "duel",
        f"duel({_title(card['title_a'])['name']} vs {_title(card['title_b'])['name']}) "
        f"= {body.outcome} → Davidson arm, margin {margin:g}",
        user_id=user["id"],
    )
    return _rate_payload(
        _ensure_card(s, head=body.head),
        log=(f"duel(title {card['title_a']} vs {card['title_b']}) = {body.outcome} -> "
             f"Davidson arm, profile_battle/random · margin {margin:g}",),
        ledger=ledger,
    )


@app.post("/api/rate/correction")
def rate_correction(
    body: CorrectionBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    """§6.1's corrections zone: "`not seen: [left] [both] [right]` -> sets that side `unseen`,
    swaps it out of the pair (`both` swaps the whole pair), **writes no duel row**, syncs per
    §7.3, covered by the persistent Undo."

    IT DOES NOT ADVANCE. A correction is a repair of the question, not an answer to it.
    """
    user = _me(spielplan_session)
    s = _rate(user)
    card = _take_card(s, body.card_token, want="battle")
    corrected = {
        "left": [card["title_a"]], "right": [card["title_b"]],
        "both": [card["title_a"], card["title_b"]],
    }[body.side]
    priors, lines = [], []
    for title_id in corrected:
        priors.append(_prior_of(user["id"], title_id))
        STATE["seen"][title_id] = "unseen"
        lines.append(_sync_line("unseen"))
    _append(s, kind_of="correction", card=card, title_ids=corrected, prior=priors)
    _stash(s, _redraw_pair(s, card, corrected=corrected))
    lines.append(
        "pair swapped, no duel row written" if body.side == "both"
        else "pair half swapped, no duel row written"
    )
    return _rate_payload(s, log=lines)


def _redraw_pair(
    s: dict[str, Any], card: dict[str, Any], *, corrected: Sequence[int]
) -> dict[str, Any] | None:
    """Keep the half the person did not correct; replace the half they did.

    A pool with only one member left in that class has no opponent to offer, and the honest
    answer is to fall through to whatever the slot can serve.
    """
    survivor = next((t for t in (card["title_a"], card["title_b"]) if t not in corrected), None)
    exclude = _skipped_title_ids(s) | set(corrected)
    if survivor is None:
        return _draw_battle(s, exclude=exclude) or _draw_sweep(
            s, exclude=_observed_title_ids(s), head=()
        )
    verdicts = _verdicts(s["user_id"])
    opponents = sorted(
        t["id"] for t in _catalog()
        if t["kind"] == card["kind"] and t["id"] not in exclude and t["id"] != survivor
        and verdicts.get(t["id"]) == card["verdict_class"] and _seen_state(t["id"]) == "seen"
    )
    if not opponents:
        return _draw_sweep(s, exclude=_observed_title_ids(s) | set(corrected), head=())
    opponent = RNG.choice(opponents)
    keep_left = survivor == card["title_a"]
    return {
        "type": "battle", "kind": card["kind"],
        "title_a": survivor if keep_left else opponent,
        "title_b": opponent if keep_left else survivor,
        "verdict_class": card["verdict_class"], "reason": card["reason"],
        "reask_of": None, "substituted_for": None,
    }


@app.post("/api/rate/undo")
def rate_undo(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Decision 35. Refused at the block boundary WITH A REASON, never silently no-opped — the
    chip has to be able to disable visibly, and `GET /api/rate` carries the same
    `undo.available` flag so it can do that before the tap."""
    user = _me(spielplan_session)
    s = _rate(user)
    live = [o for o in s["observations"] if not o["undone"]]
    if not live:
        raise HTTPException(409, detail={
            "reason": "empty", "message": "nothing to undo in this block"})
    row = live[-1]
    if row["block_index"] != s["block_index"]:
        raise HTTPException(409, detail={
            "reason": "block_boundary",
            "message": "undo reaches back to the start of this block of 15 and no further",
        })
    for prior in row["prior"]:
        if prior["state"] is None:
            STATE["seen"].pop(prior["title_id"], None)
        else:
            STATE["seen"][prior["title_id"]] = prior["state"]
        if prior["verdict"] is None:
            _verdicts(user["id"]).pop(prior["title_id"], None)
        else:
            _verdicts(user["id"])[prior["title_id"]] = prior["verdict"]
    row["undone"] = True
    # The EXACT card comes back, so a battle pair is itself rather than a reshuffle.
    s["block_index"], s["slot"] = row["block_index"], row["slot"]
    _stash(s, row["card"])
    arm = {"verdict": "verdict", "duel": "duel", "tie": "duel",
           "not_seen": "not_seen", "correction": "not_seen"}.get(row["kind_of"])
    ledger = None
    if arm in ("verdict", "duel"):
        ledger = _ledger_delta(user["id"], row["card"]["kind"], row["title_ids"])
    _rail_record("undo", f"undo: {row['kind_of']} retracted", user_id=user["id"])
    removed = 0 if arm in (None, "not_seen") else 1
    line = (
        f"undo: {arm or row['kind_of']} {row['row_id'] or ''} retracted -> "
        f"{removed} observation(s) removed, {len(row['prior'])} state(s) restored"
    ).replace("  ", " ")
    return _rate_payload(s, log=(line,), ledger=ledger)


@app.get("/api/rate/balance")
def rate_balance(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """§5.2's running class balance on its own, for the widget's own poll. Not partitioned by
    kind: §4.1 rule 5 binds surfaces that RANK, and this one ranks nothing."""
    return _class_balance(_rate(_me(spielplan_session)))


# --- M2: Home and the model-log rail (§6.0, §6.7, decisions 18 and 117) ------
#
# Two things here are not stubbed at all, because they are the parts of §6.0 a front end can
# get wrong invisibly:
#
#   * A SHELF HAS NO `items`. It has `sections`, exactly one per selected kind. §4.1 rule 5 is
#     measured ("the unpartitioned crowd top-10 is 8/10 TV series"), and decision 18's reading
#     is that a ranking surface renders two headed sections and never one interleaved ranking.
#     The `shelves.Shelf` / `shelves.Section` dataclasses are imported rather than re-declared,
#     so an interleaved ranking is unrepresentable here for the same reason it is there.
#   * A SHELF THAT CANNOT SAY WHY IT EXISTS DOES NOT SHIP. With eight fixture titles most
#     sections are under proposal 28's floor of three, and they are ABSENT — not present and
#     empty — with the reason in `suppressed`. That is the honest harness: a front end built
#     against six always-full shelves would have no empty state at all.
#
# Decision 117's gate is `rail.redact`, applied at the one exit, and `rail.visible_to` asks the
# one question. A stub that hid the numbers in the client instead would keep the promise in CSS.


def _now_local() -> tuple[datetime, str]:
    """§2's `TZ`. Proposal 22 puts the greeting on the household clock, not the device clock."""
    tz = settings().tz
    try:
        return datetime.now(ZoneInfo(tz)), tz
    except Exception:  # noqa: BLE001 - a bad TZ must not take Home down (§3.1)
        return datetime.now(), tz


def _home_kinds(kind: list[str]) -> list[str]:
    try:
        return normalise_kinds(kind)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _bundle_version() -> str | None:
    return "test-v1" if STATE["imported"] else None


def _vocabulary_version() -> str | None:
    return "v1" if STATE["imported"] else None


def _show_model(user: dict[str, Any]) -> bool:
    """Decision 117's one question, asked by the real function. A dict has no attributes, so
    the flag is lifted onto a namespace rather than the gate being reimplemented."""
    return rail.visible_to(SimpleNamespace(show_model=user.get("show_model", False)))


def _pending_verdicts(user_id: int) -> dict[str, Any] | None:
    """§6.0's standing banner: titles already `seen` that carry no verdict. None if there are
    none. Proposal 150: it never writes `seen`.

    Beyond three the copy names two and counts the rest, so the NAMED set — and therefore the
    queue head — is two, not three. Naming one set and queueing another is the failure that
    proposal exists to prevent.
    """
    verdicts = _verdicts(user_id)
    rows = [
        t for t in _catalog()
        if _seen_state(t["id"]) == "seen" and t["id"] not in verdicts
    ]
    if not rows:
        return None
    total = len(rows)
    cap = shelves.NAMED_TITLES_CAP
    named = rows[: total if total <= cap else cap - 1]
    head = [int(r["id"]) for r in named]
    # `_name_list` is proposal 21's copy. Reached through the real module rather than restated:
    # "two and N more" is a sentence the harness must not be free to spell differently.
    text = shelves._name_list([r["name"] for r in named], total)
    # REPEATED, not comma-joined: `GET /api/rate` declares `head: list[int]`, so `head=1,2` is
    # a 422 and `head=1&head=2` is the contract.
    query = "&".join(["mode=sweep"] + [f"head={i}" for i in head])
    return {
        "count": total,
        "named": [{"title_id": int(r["id"]), "name": r["name"], "kind": r["kind"]}
                  for r in named],
        "head_title_ids": head,
        "copy": {
            "wide": f"You watched {text} — a quick verdict keeps your profile sharp.",
            "compact": f"Watched, not rated: {text}",
        },
        "cta": {
            "label_wide": "Rate now", "label_compact": "Rate",
            "route": f"/rate?{query}", "api": f"/api/rate?{query}",
            "mode": "sweep", "head": head,
        },
    }


def _partner_for(user_id: int) -> dict[str, Any] | None:
    """Proposal 26: `{other}` is the member with the most co-seen titles. The harness's seen
    state is a household fact, so every seen title is co-seen."""
    others = [
        u for u in STATE["users"].values()
        if u["id"] != user_id and u["role"] in ("admin", "member")
    ]
    if not others:
        return None
    other = min(others, key=lambda u: u["id"])
    co_seen = sum(1 for t in _catalog() if _seen_state(t["id"]) == "seen")
    return {"user_id": other["id"], "name": other["name"], "co_seen": co_seen}


def _home_card(
    title: dict[str, Any],
    rank: int,
    *,
    user_id: int,
    tier_set: Sequence[str],
    terms: Sequence[str],
    beta: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One shelf card.

    Proposal 29: the rank, the seen dot and the tier LETTER are chrome and stay ungated; every
    NUMBER behind them lives in `model`, which decision 117's gate removes wholesale. §6.3's
    straddle and tension badges deliberately do not appear — Home shows the settled tier.
    """
    title_id, kind = title["id"], title["kind"]
    cdf = _cdfs(user_id, kind)[title_id]
    score = _scores(user_id, kind)[title_id]
    index = _tier_index(cdf, tier_set)
    placement = _placement(title_id)
    # Kept coherent with the placement: a cold title has no crowd support, which is exactly the
    # predicate §6.0's "New in the library" shelf checks from the model's side.
    item_n = 0 if placement == "cold_tower" else 40 + title_id * 7
    card = {
        "title_id": title_id, "kind": kind, "name": title["name"], "year": title["year"],
        "runtime_min": title["runtime_min"], "poster_path": None, "placement": placement,
        "seen": _seen_state(title_id) == "seen", "rank": rank, "tier": tier_set[index],
        "terms": list(terms),
        "model": {
            "score": round(score, 4),
            "cf": None if beta == 0.0 else round(score, 4),
            "b": round(score, 4),
            "gate": round(item_n / (item_n + 10), 4),
            "item_n": item_n,
            "e_source": "cold_tower" if placement == "cold_tower" else "backbone",
            "beta": beta,
            "s": round((cdf - 0.5) * 4.0, 4),
            "sigma": 0.4,
            "cdf": round(cdf, 4),
            "tier_index": index,
        },
    }
    if extra:
        card["model"].update(extra)
    return card


def _finish(
    section: shelves.Section, *, shelf_id: str, vocabulary: str | None
) -> tuple[shelves.Section | None, shelves.Suppressed | None]:
    """The one gate every section passes through. Three conditions, all from §6.0: proposal
    28's floor of three, a why-line at all, and — independently re-read — every card actually
    carrying every term the why-line NAMES."""
    if len(section.items) < shelves.SECTION_FLOOR:
        return None, shelves.Suppressed(
            shelf_id, section.kind,
            f"{len(section.items)} qualifying titles · the floor is {shelves.SECTION_FLOOR}",
        )
    if not section.why.strip():
        return None, shelves.Suppressed(
            shelf_id, section.kind,
            "no why-line — a shelf that cannot say why it exists does not ship",
        )
    if vocabulary:
        ids = [c["title_id"] for c in section.items]
        broken = [
            t.term for t in section.why_terms
            if t.role == "member" and not all(_carries(i, t.term) for i in ids)
        ]
        if broken:
            return None, shelves.Suppressed(
                shelf_id, section.kind,
                f"why-line named {', '.join(broken)}, which not every card carries",
            )
        section.shared_terms = _common_terms(ids)
    return section, None


def _unseen_owned(user_id: int, kind: str) -> list[dict[str, Any]]:
    """Proposal 25: unless a shelf's why-line says otherwise, shelves exclude titles the user
    has already seen."""
    scores = _scores(user_id, kind)
    return sorted(
        (t for t in _catalog() if t["kind"] == kind and _seen_state(t["id"]) == "unseen"),
        key=lambda t: (-scores[t["id"]], -(t["year"] or 0), t["id"]),
    )


def _because_anchor(user_id, kind, *, vocabulary):
    """§6.0 row 1 — "Because you put *{anchor}* in {tier}" / "shares {term} + {term} with it".

    THE INVERSION IS THE POINT (proposal 24): the pair is chosen for the size of its
    intersection and the shelf IS that intersection, so the why-line is true of every card by
    construction rather than being the anchor's first two terms pasted over a looser predicate.
    """
    sid = "because_anchor"
    if not vocabulary:
        return None, shelves.Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")
    verdicts, scores = _verdicts(user_id), _scores(user_id, kind)
    placed = [
        t for t in _catalog()
        if t["kind"] == kind and t["id"] in verdicts and _seen_state(t["id"]) == "seen"
    ]
    if not placed:
        return None, shelves.Suppressed(
            sid, kind, "no seen title of this kind carries a fitted tier yet")
    anchor = min(placed, key=lambda t: (-scores[t["id"]], t["id"]))
    tier_set = shelves.DEFAULT_TIER_SET
    index = _tier_index(_cdfs(user_id, kind)[anchor["id"]], tier_set)

    pool = _terms_for(anchor["id"])
    candidates = _unseen_owned(user_id, kind)
    best: tuple[WhyTerm, WhyTerm, list[dict[str, Any]]] | None = None
    for i, t1 in enumerate(pool):
        for t2 in pool[i + 1:]:
            members = [
                c for c in candidates
                if c["id"] != anchor["id"] and _carries(c["id"], t1.term)
                and _carries(c["id"], t2.term)
            ]
            if len(members) >= shelves.SECTION_FLOOR and (best is None or len(members) > len(best[2])):
                best = (t1, t2, members)
    if best is None:
        return None, shelves.Suppressed(
            sid, kind,
            f"no pair of {anchor['name']}'s terms covers {shelves.SECTION_FLOOR} unseen owned titles",
        )
    t1, t2, members = best
    beta, _fitted = _beta(user_id, kind)
    section = shelves.Section(
        kind=kind,
        heading=shelves.KIND_HEADINGS[kind],
        title=f"Because you put {anchor['name']} in {tier_set[index]}",
        why=f"shares {t1.term} + {t2.term} with it",
        why_terms=[t1.with_role("member"), t2.with_role("member")],
        anchor={"title_id": anchor["id"], "name": anchor["name"], "tier": tier_set[index]},
        items=[
            _home_card(t, i + 1, user_id=user_id, tier_set=tier_set,
                       terms=[t1.term, t2.term], beta=beta)
            for i, t in enumerate(members[: shelves.SHELF_CAP])
        ],
    )
    return _finish(section, shelf_id=sid, vocabulary=vocabulary)


def _top_of_ledger(user_id, kind, *, bundle_version, vocabulary):
    """§6.0 row 2 — "Top of your ledger" / "clean item prior + your fold-in, blended at β 0.8".

    Proposal 25: this is the one shelf that includes titles the user has already seen, and its
    why-line says so ("your highest, rewatches included").
    """
    sid = "top_of_ledger"
    if not bundle_version:
        return None, shelves.Suppressed(sid, kind, "no active artifact bundle — no scores to rank")
    beta, fitted = _beta(user_id, kind)
    scores = _scores(user_id, kind)
    tier_set = shelves.DEFAULT_TIER_SET
    rows = sorted(
        (t for t in _catalog() if t["kind"] == kind),
        key=lambda t: (-scores[t["id"]], t["id"]),
    )[: shelves.SHELF_CAP]
    label_count = sum(_label_counts(user_id, [kind]))
    why = (
        f"clean item prior + your fold-in, blended at β {beta:.2f} — your highest, "
        "rewatches included"
        if fitted
        else f"clean item prior alone — β {beta:.2f}, no fold-in yet — your highest, "
             "rewatches included"
    )
    section = shelves.Section(
        kind=kind,
        heading=shelves.KIND_HEADINGS[kind],
        title="Top of your ledger",
        why=why,
        why_numbers={"beta": beta, "beta_fitted": fitted,
                     "beta_optimum": shelves.DEFAULT_BETA, "label_count": label_count,
                     "gate_k": 10},
        caption=(
            None if fitted
            else f"§5.1's measured optimum is β {shelves.DEFAULT_BETA:.2f}; this profile is "
                 "not there yet"
        ),
        items=[
            _home_card(t, i + 1, user_id=user_id, tier_set=tier_set, terms=[], beta=beta)
            for i, t in enumerate(rows)
        ],
    )
    return _finish(section, shelf_id=sid, vocabulary=vocabulary)


def _never_watched_term(user_id, kind, *, vocabulary):
    """§6.0 row 3 — "You've never watched anything *{term}*" (§6.4's frontier as a shelf).

    `role` is what keeps it honest: the candidate term is a **member** (every card carries it);
    the neighbouring liked term is **anchor_side**, describing the user's region rather than
    the cards, which are unvisited by definition.
    """
    sid = "never_watched_term"
    if not vocabulary:
        return None, shelves.Suppressed(sid, kind, "no DNA vocabulary imported — no terms to name")
    thin = (
        f"no zero-coverage term carries {shelves.SECTION_FLOOR} unseen owned titles next to a "
        f"term you rate high, or fewer than {shelves.FRONTIER_MIN_SEEN} seen titles of this "
        f"kind to call any region unvisited"
    )
    seen_titles = [t for t in _catalog() if t["kind"] == kind and _seen_state(t["id"]) == "seen"]
    if len(seen_titles) < shelves.FRONTIER_MIN_SEEN:
        return None, shelves.Suppressed(sid, kind, thin)
    covered = {term.term for t in seen_titles for term in _terms_for(t["id"])}
    liked = [
        term for t in seen_titles if _verdicts(user_id).get(t["id"]) == 2
        for term in _terms_for(t["id"])
    ]
    candidates = _unseen_owned(user_id, kind)
    for term in sorted({t for c in candidates for t in _terms_for(c["id"])},
                       key=lambda t: t.term):
        if term.term in covered or not liked:
            continue
        members = [c for c in candidates if _carries(c["id"], term.term)]
        if len(members) < shelves.SECTION_FLOOR:
            continue
        neighbour = liked[0]
        beta, _fitted = _beta(user_id, kind)
        tier_set = shelves.DEFAULT_TIER_SET
        section = shelves.Section(
            kind=kind,
            heading=shelves.KIND_HEADINGS[kind],
            title=f"You've never watched anything {term.term}",
            why=("unvisited region of DNA space next to what you like "
                 f"— sits beside {neighbour.term} · cos 0.50"),
            why_terms=[term.with_role("member"), neighbour.with_role("anchor_side")],
            why_numbers={"cos": 0.5, "affinity": 0.5, "min_seen": shelves.FRONTIER_MIN_SEEN},
            caption=("one exploratory slot in six · costs about a point of top-hit rate, "
                     "honestly labelled"),
            items=[
                _home_card(t, i + 1, user_id=user_id, tier_set=tier_set,
                           terms=[term.term], beta=beta)
                for i, t in enumerate(members[: shelves.SHELF_CAP])
            ],
        )
        return _finish(section, shelf_id=sid, vocabulary=vocabulary)
    return None, shelves.Suppressed(sid, kind, thin)


def _shared_sweet_spot(user_id, kind, *, partner, bundle_version, vocabulary):
    """§6.0 row 4 — "You and {other} both rate these highly" / "the shared sweet spot — doubles
    as the Tonight prior". Ranked by the PLAIN AVERAGE of the two scores, which is what §6.2
    step 3 ranks the Tonight pool by; that shared arithmetic is what makes "doubles as" true."""
    sid = "shared_sweet_spot"
    if partner is None:
        return None, shelves.Suppressed(sid, kind, "no other member to share a sweet spot with")
    if not bundle_version:
        return None, shelves.Suppressed(
            sid, kind, "no active artifact bundle — no scores to intersect")
    mine, theirs = _cdfs(user_id, kind), _cdfs(partner["user_id"], kind)
    my_score, their_score = _scores(user_id, kind), _scores(partner["user_id"], kind)
    floor = shelves.SWEET_SPOT_MIN_CDF
    rows = sorted(
        (t for t in _catalog()
         if t["kind"] == kind and _seen_state(t["id"]) == "unseen"
         and mine[t["id"]] >= floor and theirs[t["id"]] >= floor),
        key=lambda t: (-(my_score[t["id"]] + their_score[t["id"]]) / 2.0, t["id"]),
    )[: shelves.SHELF_CAP]
    beta, _fitted = _beta(user_id, kind)
    tier_set = shelves.DEFAULT_TIER_SET
    section = shelves.Section(
        kind=kind,
        heading=shelves.KIND_HEADINGS[kind],
        title=f"You and {partner['name']} both rate these highly",
        why="the shared sweet spot — doubles as the Tonight prior",
        why_numbers={"min_cdf": floor, "partner_user_id": partner["user_id"],
                     "co_seen": partner["co_seen"]},
        caption=(f"neither of you has seen these — both of you land above {floor:.2f} on your "
                 "own ledgers, ranked by the plain average that seeds Tonight"),
        items=[
            _home_card(
                t, i + 1, user_id=user_id, tier_set=tier_set, terms=[], beta=beta,
                extra={"mine_cdf": round(mine[t["id"]], 4),
                       "theirs_cdf": round(theirs[t["id"]], 4),
                       "pair_score": round((my_score[t["id"]] + their_score[t["id"]]) / 2.0, 4)},
            )
            for i, t in enumerate(rows)
        ],
    )
    return _finish(section, shelf_id=sid, vocabulary=vocabulary)


def _school_night(user_id, kind, *, vocabulary):
    """§6.0 row 5 — "Under 110 minutes" / "for a school night", restated per proposal 27.

    A NULL runtime is excluded (a shelf that claims a runtime bound must know the runtime) and
    the comparison is strict, so a title at exactly the threshold is not "under" it."""
    sid = "school_night"
    limit_min = shelves.SCHOOL_NIGHT_MAX_MIN[kind]
    beta, _fitted = _beta(user_id, kind)
    tier_set = shelves.DEFAULT_TIER_SET
    rows = [
        t for t in _unseen_owned(user_id, kind)
        if t["runtime_min"] is not None and t["runtime_min"] < limit_min
    ][: shelves.SHELF_CAP]
    section = shelves.Section(
        kind=kind,
        heading=shelves.KIND_HEADINGS[kind],
        title=shelves.SCHOOL_NIGHT_TITLE[kind],
        why="for a school night",
        why_numbers={"max_minutes": limit_min},
        caption="series runtime is minutes per episode" if kind == "series" else None,
        items=[
            _home_card(t, i + 1, user_id=user_id, tier_set=tier_set, terms=[], beta=beta)
            for i, t in enumerate(rows)
        ],
    )
    return _finish(section, shelf_id=sid, vocabulary=vocabulary)


def _new_in_library(user_id, kind, *, vocabulary):
    """§6.0 row 6 — "New in the library" / "placed by the Cold Tower — no crowd data yet".

    Ordered by recency rather than by score, which is why it is the one shelf that still ships
    for a user with no verdicts (proposal 20 suppresses every score-ordered shelf)."""
    sid = "new_in_library"
    beta, _fitted = _beta(user_id, kind)
    tier_set = shelves.DEFAULT_TIER_SET
    rows = sorted(
        (t for t in _catalog()
         if t["kind"] == kind and _seen_state(t["id"]) == "unseen"
         and _placement(t["id"]) == "cold_tower"),
        key=lambda t: -t["id"],
    )[: shelves.SHELF_CAP]
    section = shelves.Section(
        kind=kind,
        heading=shelves.KIND_HEADINGS[kind],
        title="New in the library",
        why="placed by the Cold Tower — no crowd data yet",
        why_numbers={"gate_k": 10},
        items=[
            _home_card(t, i + 1, user_id=user_id, tier_set=tier_set, terms=[], beta=beta)
            for i, t in enumerate(rows)
        ],
    )
    return _finish(section, shelf_id=sid, vocabulary=vocabulary)


def _build_shelves(user_id, kinds, *, bundle_version, vocabulary, verdicts, partner):
    """§6.0's six shelves, in the table's order, each as one section per selected kind.

    Proposal 20's zero-verdict state is applied here rather than in a second code path, and
    `new_in_library` — ordered by recency, not by a ledger nobody has yet — survives it.
    """
    zero = verdicts == 0 and bundle_version is not None
    built: list[shelves.Shelf] = []
    dropped: list[shelves.Suppressed] = []
    for shelf_id in shelves.SHELF_IDS:
        ranking = shelf_id in shelves.RANKING_SHELVES
        shelf = shelves.Shelf(shelf_id, ranking=ranking)
        for kind in kinds:
            if zero and ranking:
                dropped.append(shelves.Suppressed(
                    shelf_id, kind, "no verdicts yet — a score-ordered shelf would rank on a "
                                    "ledger this profile does not have"))
                continue
            if shelf_id == "because_anchor":
                section, note = _because_anchor(user_id, kind, vocabulary=vocabulary)
            elif shelf_id == "top_of_ledger":
                section, note = _top_of_ledger(
                    user_id, kind, bundle_version=bundle_version, vocabulary=vocabulary)
            elif shelf_id == "never_watched_term":
                section, note = _never_watched_term(user_id, kind, vocabulary=vocabulary)
            elif shelf_id == "shared_sweet_spot":
                section, note = _shared_sweet_spot(
                    user_id, kind, partner=partner, bundle_version=bundle_version,
                    vocabulary=vocabulary)
            elif shelf_id == "school_night":
                section, note = _school_night(user_id, kind, vocabulary=vocabulary)
            else:
                section, note = _new_in_library(user_id, kind, vocabulary=vocabulary)
            if section is not None:
                shelf.sections.append(section)
            elif note is not None:
                dropped.append(note)
        # §6.0: a shelf that cannot justify itself is ABSENT, never present and empty.
        if shelf.sections:
            built.append(shelf)
    return built, dropped


def _catalog_page(kinds, *, q, person_id, limit, offset):
    """Decision 18: a surface that merely LISTS in a kind-independent order may interleave
    freely. This is that surface — `/api/titles`' own ordering, by year."""
    rows = [t for t in _catalog() if t["kind"] in kinds]
    if q and q.strip():
        rows = [t for t in rows if q.strip().lower() in t["name"].lower()]
    if person_id is not None:
        with _db() as db:
            credited = {
                r[0] for r in db.execute(
                    "SELECT title_id FROM credit WHERE person_id = ?", (person_id,))
            }
        rows = [t for t in rows if t["id"] in credited]
    rows.sort(key=lambda t: (-(t["year"] or 0), t["name"].lower()))
    hidden: dict[str, int] = {}
    for other in KINDS:
        if other in kinds:
            continue
        n = sum(1 for t in _catalog() if t["kind"] == other)
        if n:
            hidden[other] = n
    items = [
        {"id": t["id"], "kind": t["kind"], "name": t["name"], "year": t["year"],
         "runtime_min": t["runtime_min"], "poster_path": None, "is_owned": True,
         "placement": _placement(t["id"]), "seen_state": _seen_state(t["id"])}
        for t in rows[offset : offset + limit]
    ]
    return {"total": len(rows), "hidden": hidden, "limit": limit, "offset": offset,
            "q": q, "person_id": person_id, "items": items}


def _build_home(user, kinds, *, q=None, person_id=None, limit=60, offset=0) -> dict[str, Any]:
    """§6.0's Home payload, UNGATED — every caller redacts. THE MODE IS THE SERVER'S: with `q`
    or `person_id` set the payload carries `catalog` and no shelves, otherwise the shelves and
    no catalog. A client cannot render shelves over a person filter, because with one set there
    are no shelves in the payload to render."""
    user_id = user["id"]
    verdicts = len(_verdicts(user_id))
    bundle_version, vocabulary = _bundle_version(), _vocabulary_version()
    mode = "grid" if (q and q.strip()) or person_id is not None else "shelves"
    partner = _partner_for(user_id)
    now_local, tz = _now_local()
    payload: dict[str, Any] = {
        "mode": mode,
        "kinds": kinds,
        "greeting": shelves.greeting(now_local, user["name"], tz=tz),
        "banner": _pending_verdicts(user_id),
        "verdict_count": verdicts,
        "bundle": bundle_version,
        "vocabulary": vocabulary,
        "partner": partner,
        "shelves": [],
        "sections": [],
        "shelves_total": 0,
        "catalog": None,
        # Proposal 20's two first-week states, from the real builder so Home and Rate promise
        # the same thing about the learning curve.
        "degraded": shelves._degraded(bundle_version, verdicts),
        "suppressed": [],
        "rail": _rail_recent(user_id),
    }
    if mode == "grid":
        payload["catalog"] = _catalog_page(
            kinds, q=q, person_id=person_id, limit=limit, offset=offset)
        return payload
    built, dropped = _build_shelves(
        user_id, kinds, bundle_version=bundle_version, vocabulary=vocabulary,
        verdicts=verdicts, partner=partner)
    payload["shelves"] = [s.as_dict() for s in built]
    payload["sections"] = shelves.sections_by_kind(built, kinds)
    payload["shelves_total"] = len(built)
    payload["suppressed"] = [s.as_dict() for s in dropped]
    return payload


@app.get("/api/home")
def home(
    kind: list[Literal["movie", "series"]] = Query(...),
    q: str | None = None,
    person_id: int | None = None,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = _me(spielplan_session)
    payload = _build_home(
        user, _home_kinds(kind), q=q, person_id=person_id, limit=limit, offset=offset)
    return rail.redact(payload, show_model=_show_model(user))


@app.get("/api/home/shelves")
def home_shelves(
    kind: list[Literal["movie", "series"]] = Query(...),
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """The shelves alone, for a client that renders the greeting and banner separately. Same
    builder, same gate, same partition — and it deliberately cannot be asked for the grid."""
    user = _me(spielplan_session)
    payload = _build_home(user, _home_kinds(kind))
    slim = {
        key: payload[key]
        for key in ("kinds", "shelves", "sections", "shelves_total", "verdict_count",
                    "degraded", "partner", "bundle", "vocabulary", "suppressed")
        if key in payload
    }
    return rail.redact(slim, show_model=_show_model(user))


@app.get("/api/home/pending-verdicts")
def home_pending(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """§6.0's banner on its own. `{count: 0, …}` rather than a 404 for an empty population:
    "nothing to rate" is an answer, and a client that has to tell an error from an empty banner
    will get it wrong on the first flaky request."""
    user = _me(spielplan_session)
    return _pending_verdicts(user["id"]) or {
        "count": 0, "named": [], "head_title_ids": [], "copy": None, "cta": None}


@app.get("/api/model-log")
def model_log(
    limit: int = Query(rail.RAIL_LIMIT, ge=1, le=50),
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """§6.7's rail. Decision 117: one per-user toggle, default off.

    With the toggle OFF the response has NO `events` key at all — not an empty list, not a list
    the client is trusted to hide. A promise kept in CSS is not kept: the payload would still
    be in the network tab and in the service-worker cache.
    """
    user = _me(spielplan_session)
    if not _show_model(user):
        return {
            "show_model": False,
            "hint": "turn on 'show the model' in the account menu to see the model log",
        }
    events = _rail_recent(user["id"], limit)
    return {"show_model": True, "limit": limit, "kinds": rail.kinds_present(events),
            "events": events}


# --- M3: the Rank surface (§6.3, §6.7, §13) ----------------------------------
#
# The board, the badges and the 70/20/10 selector are the REAL modules — `spielplan.rank.board`
# and `spielplan.rank.queue` are pure by contract, so the harness runs them over its own
# invented `s` values and the front end is built against the shapes the app actually returns.
# What is faked is only what needs a database: the observations. A tier edit lands in `STATE`
# and a comparison is counted there, so both loops close.
#
# The one thing this harness cannot demonstrate is §6.3's "the model refits (incremental
# immediately)": there is no Ledger here, so a drop moves the title and does not move `s`.
# `backend/spielplan/api/rank.py` wins on that, as this file's header says it does on
# everything.

RANK_PAIR_SALT = "devstub/rank/pair"


def _rank_hp() -> Hyperparams:
    return Hyperparams()


def _rank_tier_set(user_id: int) -> tuple[str, ...]:
    return tuple(STATE["tier_sets"].get(user_id, shelves.DEFAULT_TIER_SET))


def _rank_cuts(user_id: int, kind: str) -> Any:
    """Equal-mass quantiles over the harness's own scores — decision 11's re-initialisation
    rule, and the closest thing here to a fitted cutpoint vector."""
    import numpy as np

    tier_set = _rank_tier_set(user_id)
    values = np.asarray(sorted(_scores(user_id, kind).values()), dtype=float)
    if values.size < 2:
        from spielplan.ledger import model as ledger_model

        return ledger_model.initial_cutpoints(len(tier_set))
    return rank_tiers.equal_mass_quantiles(values * 4.0 - 2.0, len(tier_set))


def _rank_items(user_id: int, kind: str) -> list[Any]:
    """§6.3's "every rated title" — here, every title the fixture person has a verdict on."""
    verdicts = _verdicts(user_id)
    scores = _scores(user_id, kind)
    edits = STATE["tier_edits"].get((user_id, kind), {})
    out = []
    for title in _catalog():
        if title["kind"] != kind or title["id"] not in verdicts:
            continue
        # A σ that varies per title, so straddle badges are reachable in the harness. The real
        # one is §5.2's Laplace diagonal, read from `ledger_state.sigma_eff`.
        sigma = 0.15 + 0.5 * random.Random(f"sigma:{user_id}:{title['id']}").random()
        out.append(
            rank_board.Item(
                title_id=title["id"],
                name=title["name"],
                s=float(scores.get(title["id"], 0.0)) * 4.0 - 2.0,
                sigma=sigma,
                assigned_tier=edits.get(title["id"]),
            )
        )
    return out


def _rank_matches(item: Any, filters: Any) -> bool:
    """The harness's filter, over the fixture's own columns. `db.library._filters` is the
    contract; this only has to agree about which titles survive."""
    title = _title(item.title_id) or {}
    if filters.q and filters.q.lower() not in str(title.get("name", "")).lower():
        return False
    if filters.runtime_max is not None:
        runtime = title.get("runtime_min")
        if runtime is None or runtime > filters.runtime_max:
            return False
    if filters.decade is not None:
        year = title.get("year")
        if year is None or not (filters.decade <= year < filters.decade + 10):
            return False
    if filters.genre and filters.genre not in _genres_of(item.title_id):
        return False
    if filters.dna and not _carries(item.title_id, filters.dna.split(".")[-1]):
        return False
    return filters.seen == "any" or (
        (filters.seen == "seen") == (_seen_state(item.title_id) == "seen")
    )


def _rank_filters(q, genre, decade, runtime_max, runtime_min, seen, dna):
    from spielplan.db.library import RankFilters

    return RankFilters(
        q=q, genre=genre, decade=decade, runtime_max=runtime_max,
        runtime_min=runtime_min, seen=seen, dna=dna,
    )


def _rank_board_payload(
    user: dict[str, Any], kind: str, filters: Any, log_line: str | None = None
) -> dict[str, Any]:
    hp = _rank_hp()
    tier_set = _rank_tier_set(user["id"])
    cuts = _rank_cuts(user["id"], kind)
    rows = _rank_items(user["id"], kind)
    active = filters.active()
    shown = [r for r in rows if _rank_matches(r, filters)] if active else rows
    tiers = rank_board.build(shown, cuts=cuts, tier_set=tier_set, hp=hp)
    payload = {
        "kind": kind,
        "tier_set": list(tier_set),
        "tiers": [
            {"index": t.index, "label": t.label, "entries": [e.public() for e in t.entries]}
            for t in tiers
        ],
        "rated": len(shown),
        "rated_total": len(rows),
        "queue_eligible": len(rank_queue.eligible(rows, cuts=cuts, hp=hp)),
        "filters": active,
        "dna_tiers": None,
        "why": f"{len(rows)} rated · learned cutpoints, refit nightly",
        "model": {
            "cutpoints": [float(b) for b in cuts],
            "hyperparams": hp.source,
            "straddle_z": hp.straddle_z,
            "tension_credible_mass": hp.tension_credible_mass,
            "held_out": {
                "kind": kind, "pairs": 0, "decisive": 0, "ties": 0, "agreed": 0,
                "unplaced": 0, "rate": None, "stream": "uniform_holdout",
            },
        },
    }
    if log_line:
        _rail_record("tier_edit", log_line, user_id=user["id"])
        payload["log"] = [log_line]
    return rail.redact(payload, show_model=_show_model(user))


class RankDropBody(BaseModel):
    title_id: int
    tier: int = Field(ge=0)
    above: int | None = None
    below: int | None = None


class RankAnswerBody(BaseModel):
    pair: str
    outcome: Literal["A", "B", "TIE"]
    decisive: bool = False


class RankTierSetBody(BaseModel):
    tier_set: list[str]


@app.get("/api/rank")
def rank_board_route(
    kind: Literal["movie", "series"] = Query(...),
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    runtime_max: int | None = Query(None, ge=1),
    runtime_min: int | None = Query(None, ge=1),
    seen: Literal["any", "seen", "unseen"] = "any",
    dna: str | None = None,
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = _me(spielplan_session)
    return _rank_board_payload(
        user, kind, _rank_filters(q, genre, decade, runtime_max, runtime_min, seen, dna)
    )


@app.post("/api/rank/drop")
def rank_drop(
    body: RankDropBody,
    kind: Literal["movie", "series"] = Query(...),
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    runtime_max: int | None = Query(None, ge=1),
    runtime_min: int | None = Query(None, ge=1),
    seen: Literal["any", "seen", "unseen"] = "any",
    dna: str | None = None,
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = _me(spielplan_session)
    tier_set = _rank_tier_set(user["id"])
    if not 0 <= body.tier < len(tier_set):
        raise HTTPException(422, f"tier {body.tier} is outside the configured set")
    STATE["tier_edits"].setdefault((user["id"], kind), {})[body.title_id] = body.tier
    neighbours = [t for t in (body.above, body.below) if t is not None]
    counts = STATE["rank_comparisons"].setdefault((user["id"], kind), {})
    if neighbours:
        for title_id in [body.title_id, *neighbours]:
            counts[title_id] = counts.get(title_id, 0) + 1
    title = _title(body.title_id) or {"name": f"title {body.title_id}"}
    line = rail.tier_edit_line(
        title["name"], tier_set[body.tier], via="drag_drop", neighbour_duels=len(neighbours)
    )
    return _rank_board_payload(
        user, kind, _rank_filters(q, genre, decade, runtime_max, runtime_min, seen, dna),
        log_line=line,
    )


def _rank_seal(user_id: int, kind: str, pair: Any) -> str:
    return URLSafeSerializer(RANK_PAIR_SALT).dumps(
        {"u": user_id, "k": kind, "a": pair.title_a, "b": pair.title_b, "arm": pair.arm}
    )


@app.get("/api/rank/queue")
def rank_queue_route(
    kind: Literal["movie", "series"] = Query(...),
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = _me(spielplan_session)
    hp = _rank_hp()
    pool = rank_queue.candidates(
        _rank_items(user["id"], kind),
        cuts=_rank_cuts(user["id"], kind),
        tier_set=_rank_tier_set(user["id"]),
        hp=hp,
        comparisons=STATE["rank_comparisons"].get((user["id"], kind), {}),
    )
    pair = rank_queue.draw(pool, rng=random.SystemRandom())
    if pair is None:
        return {
            "kind": kind,
            "pair": None,
            "reason": (
                "There is nothing to compare yet — rate a few more titles and the queue fills up."
            ),
        }
    names = {t["id"]: t["name"] for t in _catalog()}
    return {
        "kind": kind,
        "pair": {
            **pair.public(),
            "name_a": names.get(pair.title_a),
            "name_b": names.get(pair.title_b),
            "token": _rank_seal(user["id"], kind, pair),
        },
        "pool": len(pool),
    }


@app.post("/api/rank/queue/answer")
def rank_answer(
    body: RankAnswerBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    try:
        sealed = URLSafeSerializer(RANK_PAIR_SALT).loads(body.pair)
    except BadSignature as exc:
        raise HTTPException(409, {"reason": "stale_pair"}) from exc
    kind = str(sealed["k"])
    counts = STATE["rank_comparisons"].setdefault((user["id"], kind), {})
    for title_id in (int(sealed["a"]), int(sealed["b"])):
        counts[title_id] = counts.get(title_id, 0) + 1
    names = {t["id"]: t["name"] for t in _catalog()}
    line = rail.duel_line(
        names.get(int(sealed["a"]), str(sealed["a"])),
        names.get(int(sealed["b"]), str(sealed["b"])),
        body.outcome,
        context="tier_queue",
        selection=str(sealed["arm"]),
    )
    _rail_record("duel", line, user_id=user["id"])
    payload = rank_queue_route(kind=kind, spielplan_session=spielplan_session)
    payload["log"] = [line]
    return rail.redact(payload, show_model=_show_model(user))


@app.get("/api/rank/tiers")
def rank_tier_set(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _me(spielplan_session)
    return {
        "tier_set": list(_rank_tier_set(user["id"])),
        "min": rank_tiers.MIN_TIERS,
        "max": rank_tiers.MAX_TIERS,
        "warning": (
            "Changing the number of tiers discards your learned cutpoints and queues a refit. "
            "Your past moves are kept."
        ),
    }


@app.put("/api/rank/tiers")
def rank_save_tier_set(
    body: RankTierSetBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    previous = _rank_tier_set(user["id"])
    try:
        labels = rank_tiers.validate(body.tier_set)
    except rank_tiers.TierSetRefused as exc:
        raise HTTPException(422, str(exc)) from exc
    STATE["tier_sets"][user["id"]] = list(labels)
    changed = len(labels) != len(previous)
    if changed:
        # Decision 11: a change in K invalidates the boundaries, never the observations. The
        # real clamp lives in `ledger.observations.load_observations`; this mirrors its effect
        # so the harness's board does not index past its own labels.
        for key in list(STATE["tier_edits"]):
            if key[0] == user["id"]:
                STATE["tier_edits"][key] = {
                    t: min(v, len(labels) - 1) for t, v in STATE["tier_edits"][key].items()
                }
    return {
        "tier_set": list(labels),
        "previous": list(previous),
        "k_changed": changed,
        "refit_queued": changed,
        "tier_edits_kept": sum(
            len(v) for k, v in STATE["tier_edits"].items() if k[0] == user["id"]
        ),
    }


# --- M4: the Tonight surface (§6.2 rewritten, §6.7, §13) ---------------------
#
# The pool's arithmetic, the round, the tilt, the combine and the ballot are the REAL modules —
# `spielplan.tonight.{pool,round,tilt,combine,copy,solo,ballot}` are pure by contract, so the
# harness runs them over its own invented scores and the front end is built against the shapes
# the app actually returns. What is faked is only what needs a database: the session rows.
#
# Two things this harness cannot demonstrate, and `backend/spielplan/api/tonight.py` wins on
# both, as this file's header says it does on everything: a second device (there is one process
# and no WebSocket here, so the lobby does not go live), and the push invitation.

TONIGHT_PAIR_SALT = "devstub/tonight/pair"


def _tonight_axes() -> dict[str, dict[str, float]]:
    return {
        facet: dict(weights) for facet, (_l, _r, weights) in fx.AXES.items()
    }


def _tonight_dna() -> dict[int, dict[str, float]]:
    """The fixture's two tiers, merged the way `dna.vectors_for` merges them: max, not sum, so
    a term in both tiers is held once at the louder weight (§4.1 rule 1)."""
    out: dict[int, dict[str, float]] = {}
    for title_id, term, _facet, salience, _quote in fx.EXTRACTED:
        weight = 0.60 + 0.40 * (salience / 3.0)
        out.setdefault(title_id, {})[term] = max(out.setdefault(title_id, {}).get(term, 0.0), weight)
    for title_id, term, _facet, weight, _via in fx.PROJECTED:
        out.setdefault(title_id, {})[term] = max(
            out.setdefault(title_id, {}).get(term, 0.0), 0.30 * float(weight)
        )
    return out


def _tonight_candidates(seats: list[Any], kind: str, budget: int, rewatches: bool):
    """§6.2 step 3's pool, over the harness's invented scores."""
    catalog = {t["id"]: t for t in _catalog() if t["kind"] == kind}
    per_seat = {
        seat.participant_id: _scores(seat.user_id, kind) for seat in seats if seat.is_member
    }
    out = []
    for title_id, title in catalog.items():
        scores = {p: s[title_id] for p, s in per_seat.items() if title_id in s}
        if len(scores) < len([s for s in seats if s.is_member]):
            continue
        if not rewatches and all(
            STATE["seen"].get((seat.user_id, title_id)) == "seen"
            for seat in seats if seat.is_member
        ):
            continue
        out.append(
            tonight_pool.Candidate(
                title_id=title_id, kind=kind, name=title["name"], year=title.get("year"),
                runtime_min=title.get("runtime_min"), poster_path=title.get("poster_path"),
                scores=scores,
            )
        )
    return tonight_pool.order(tonight_pool.with_budget(out, budget_min=budget))


def _tonight_room(session_id: int) -> dict[str, Any]:
    room = STATE["tonight"].get(session_id)
    if room is None:
        raise HTTPException(404, {"reason": "no_room", "message": "no such session"})
    return room


def _tonight_lobby(room: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": room["session_id"],
        "room_code": room["room_code"],
        "state": room["state"],
        "kind": room["kind"],
        "runtime_budget_min": room["runtime_budget_min"],
        "include_rewatches": room["include_rewatches"],
        "started_at": room["started_at"],
        "host": room["host"],
        "seats": [dict(s) for s in room["seats"]],
    }


class TonightOpenBody(BaseModel):
    kind: str = "movie"
    runtime_budget_min: int = 130
    include_rewatches: bool = False
    guests: int = 0


class TonightJoinBody(BaseModel):
    session_id: int | None = None
    room_code: str | None = None


class TonightAnswerBody(BaseModel):
    card_token: str
    answer: str
    latency_ms: int | None = None


class TonightBallotBody(BaseModel):
    approved: list[int] = []


class TonightSoloBody(BaseModel):
    kind: str = "movie"
    runtime_budget_min: int = 130
    include_rewatches: bool = False
    offset: int = 0
    answers: list[dict[str, Any]] = []


@app.get("/api/tonight/rooms")
def tonight_rooms(spielplan_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user = _me(spielplan_session)
    return {
        "rooms": [
            {
                "session_id": r["session_id"], "room_code": r["room_code"], "state": r["state"],
                "host": r["host"]["name"], "started_at": r["started_at"], "kind": r["kind"],
                "runtime_budget_min": r["runtime_budget_min"],
                "skips_seen": not r["include_rewatches"],
                "seated": len(r["seats"]),
                "viewer_seated": any(s["user_id"] == user["id"] for s in r["seats"]),
                "joinable": r["state"] == "open"
                and not any(s["user_id"] == user["id"] for s in r["seats"]),
            }
            for r in STATE["tonight"].values()
            if r["ended_at"] is None
        ]
    }


@app.post("/api/tonight/sessions", status_code=201)
def tonight_open(
    body: TonightOpenBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    session_id = STATE["tonight_seq"] = STATE["tonight_seq"] + 1
    seat_id = session_id * 100
    room = {
        "session_id": session_id,
        "room_code": tonight_rooms_mod.make_code(random.Random(session_id)),
        "state": "open",
        "kind": body.kind,
        "runtime_budget_min": body.runtime_budget_min,
        "include_rewatches": body.include_rewatches,
        "started_at": datetime.now(UTC).isoformat(),
        "ended_at": None,
        "host": {"user_id": user["id"], "name": user["name"]},
        "seats": [
            {"participant_id": seat_id, "seat": 1, "role": "host", "user_id": user["id"],
             "name": user["name"], "avatar": user.get("avatar"), "answered_count": 0,
             "ended_by": None}
        ],
        "answers": {},
        "tilts": {},
        "slate": None,
        "ballots": {},
    }
    for i in range(body.guests):
        room["seats"].append({
            "participant_id": seat_id + 1 + i, "seat": 2 + i, "role": "guest", "user_id": None,
            "name": f"Guest {i + 1}", "avatar": None, "answered_count": 0, "ended_by": None,
        })
    STATE["tonight"][session_id] = room
    return {"session_id": session_id, "room_code": room["room_code"],
            "lobby": _tonight_lobby(room)}


@app.post("/api/tonight/sessions/join")
def tonight_join(
    body: TonightJoinBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    room = None
    if body.session_id is not None:
        room = _tonight_room(body.session_id)
    else:
        for candidate in STATE["tonight"].values():
            if candidate["room_code"].upper() == (body.room_code or "").upper().strip():
                room = candidate
    if room is None:
        raise HTTPException(404, {"reason": "no_room", "message": "no live room has that code"})
    existing = next((s for s in room["seats"] if s["user_id"] == user["id"]), None)
    if existing is None:
        existing = {
            "participant_id": room["session_id"] * 100 + len(room["seats"]),
            "seat": len(room["seats"]) + 1, "role": "member", "user_id": user["id"],
            "name": user["name"], "avatar": user.get("avatar"), "answered_count": 0,
            "ended_by": None,
        }
        room["seats"].append(existing)
    return {"session_id": room["session_id"], **existing, "lobby": _tonight_lobby(room)}


@app.get("/api/tonight/sessions/{session_id}")
def tonight_lobby_route(
    session_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    room = _tonight_room(session_id)
    return {
        **_tonight_lobby(room),
        "progress": [
            {"participant_id": s["participant_id"], "seat": s["seat"], "name": s["name"],
             "answered": s["answered_count"], "expected": tonight_round.CAP_PAIRS,
             "finished": s["ended_by"] is not None, "ended_by": s["ended_by"]}
            for s in room["seats"]
        ],
        "ballot": {"submitted": len(room["ballots"]), "seated": len(room["seats"]),
                   "revealed": len(room["ballots"]) >= len(room["seats"])},
        "me": next((s for s in room["seats"] if s["user_id"] == user["id"]), None),
    }


@app.post("/api/tonight/sessions/{session_id}/start")
def tonight_start(
    session_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = _tonight_room(session_id)
    seats = [
        tonight_pool.Seat(participant_id=s["participant_id"], user_id=s["user_id"],
                          is_member=s["role"] != "guest")
        for s in room["seats"]
    ]
    candidates = _tonight_candidates(
        seats, room["kind"], room["runtime_budget_min"], room["include_rewatches"]
    )
    if len(candidates) < 2:
        raise HTTPException(409, {"reason": "empty_pool",
                                  "message": "nothing in the library fits tonight"})
    room["pool"] = candidates
    room["state"] = "voting"
    return {"session_id": session_id, "state": room["state"]}


def _tonight_seat(room: dict[str, Any], participant_id: int) -> dict[str, Any]:
    seat = next((s for s in room["seats"] if s["participant_id"] == participant_id), None)
    if seat is None:
        raise HTTPException(404, {"reason": "no_seat", "message": "no such seat"})
    return seat


def _tonight_round_for(room: dict[str, Any], seat: dict[str, Any]):
    pool_scores = (
        {c.title_id: c.scores[seat["participant_id"]] for c in room["pool"]}
        if seat["role"] != "guest"
        else {c.title_id: c.group_score for c in room["pool"]}
    )
    answers = room["answers"].get(seat["participant_id"], [])
    return tonight_round.replay(
        pool_scores, answers, z=Hyperparams().straddle_z,
        has_profile=seat["role"] != "guest",
        axes=tonight_combine.axis_positions(_tonight_dna(), _tonight_axes()),
        escaped=seat["ended_by"] == tonight_round.ESCAPE,
    )


def _tonight_state(room: dict[str, Any], seat: dict[str, Any]) -> dict[str, Any]:
    played = _tonight_round_for(room, seat)
    by_id = {c.title_id: c for c in room["pool"]}
    pair = None if played.stop_reason else played.next_pair
    token = None
    if pair is not None:
        token = URLSafeSerializer(TONIGHT_PAIR_SALT).dumps(
            {"p": seat["participant_id"], "a": pair.title_a, "b": pair.title_b,
             "s": pair.selection, "n": seat["answered_count"] + 1}
        )

    def side(title_id: int) -> dict[str, Any] | None:
        c = by_id.get(title_id)
        if c is None:
            return None
        return {"title_id": c.title_id, "name": c.name, "year": c.year,
                "runtime_min": c.runtime_min, "poster_path": c.poster_path,
                "fit_line": c.fit_line, "over_budget_min": c.over_budget_min}

    return {
        "participant_id": seat["participant_id"],
        "answered": seat["answered_count"],
        "cap": tonight_round.CAP_PAIRS,
        "ended_by": seat["ended_by"],
        "stop_reason": played.stop_reason,
        "escape_available": tonight_round.escape_available(seat["answered_count"]),
        "card_token": token,
        "pair": None if pair is None else {
            "a": side(pair.title_a), "b": side(pair.title_b),
            "selection": pair.selection, "reason": pair.reason,
        },
    }


@app.get("/api/tonight/seats/{participant_id}/round")
def tonight_round_state(
    participant_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = next(
        (r for r in STATE["tonight"].values()
         if any(s["participant_id"] == participant_id for s in r["seats"])), None
    )
    if room is None:
        raise HTTPException(404, {"reason": "no_seat", "message": "no such seat"})
    return _tonight_state(room, _tonight_seat(room, participant_id))


@app.post("/api/tonight/seats/{participant_id}/answer")
def tonight_answer(
    participant_id: int, body: TonightAnswerBody,
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    user = _me(spielplan_session)
    room = next(
        (r for r in STATE["tonight"].values()
         if any(s["participant_id"] == participant_id for s in r["seats"])), None
    )
    if room is None:
        raise HTTPException(404, {"reason": "no_seat", "message": "no such seat"})
    seat = _tonight_seat(room, participant_id)
    try:
        sealed = URLSafeSerializer(TONIGHT_PAIR_SALT).loads(body.card_token)
    except BadSignature as exc:
        raise HTTPException(409, {"reason": "stale_pair"}) from exc
    if sealed["n"] != seat["answered_count"] + 1:
        raise HTTPException(409, {"reason": "stale_pair"})
    answers = room["answers"].setdefault(participant_id, [])
    answers.append(tonight_round.Answered(
        seq=sealed["n"], title_a=int(sealed["a"]), title_b=int(sealed["b"]),
        answer=body.answer, selection=str(sealed["s"]),
    ))
    seat["answered_count"] += 1
    played = _tonight_round_for(room, seat)
    if played.stop_reason:
        seat["ended_by"] = played.stop_reason
    line = rail.session_answer_line(str(participant_id), sealed["n"], body.answer)
    _rail_record("session_answer", line, user_id=user["id"])
    payload = {**_tonight_state(room, seat),
               "wrote": {"seq": sealed["n"], "stop_reason": played.stop_reason},
               "rail": rail.recent(user_id=user["id"], limit=5)}
    if all(s["ended_by"] for s in room["seats"]):
        _tonight_combine(room)
    return rail.redact(payload, show_model=_show_model(user))


@app.post("/api/tonight/seats/{participant_id}/undo")
def tonight_undo(
    participant_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = next(
        (r for r in STATE["tonight"].values()
         if any(s["participant_id"] == participant_id for s in r["seats"])), None
    )
    if room is None:
        raise HTTPException(404, {"reason": "no_seat", "message": "no such seat"})
    seat = _tonight_seat(room, participant_id)
    answers = room["answers"].get(participant_id, [])
    if not answers or seat["ended_by"]:
        raise HTTPException(409, {"reason": "nothing_to_undo"})
    gone = answers.pop()
    seat["answered_count"] -= 1
    return {**_tonight_state(room, seat), "retracted_seq": gone.seq}


@app.post("/api/tonight/seats/{participant_id}/escape")
def tonight_escape(
    participant_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = next(
        (r for r in STATE["tonight"].values()
         if any(s["participant_id"] == participant_id for s in r["seats"])), None
    )
    if room is None:
        raise HTTPException(404, {"reason": "no_seat", "message": "no such seat"})
    seat = _tonight_seat(room, participant_id)
    if not tonight_round.escape_available(seat["answered_count"]):
        raise HTTPException(409, {"reason": "too_early",
                                  "message": "the escape opens at pair 6"})
    seat["ended_by"] = tonight_round.ESCAPE
    if all(s["ended_by"] for s in room["seats"]):
        _tonight_combine(room)
    return {**_tonight_state(room, seat), "ended_by": seat["ended_by"]}


def _tonight_combine(room: dict[str, Any]) -> None:
    dna = _tonight_dna()
    axes = _tonight_axes()
    frame = tonight_tilt.frame(dna)
    per_participant: dict[int, dict[int, float]] = {}
    tilts = []
    for seat in room["seats"]:
        played = _tonight_round_for(room, seat)
        tilt: dict[str, float] = {}
        for a in room["answers"].get(seat["participant_id"], []):
            if a.selection == tonight_round.SELECTION_HOLDOUT:
                continue
            a_dna, b_dna = dna.get(a.title_a, {}), dna.get(a.title_b, {})
            if a.answer == tonight_round.A:
                tilt = tonight_tilt.observe(tilt, chosen=a_dna, rejected=b_dna, frame=frame)
            elif a.answer == tonight_round.B:
                tilt = tonight_tilt.observe(tilt, chosen=b_dna, rejected=a_dna, frame=frame)
            else:
                tilt = tonight_tilt.observe_level(
                    tilt, first=a_dna, second=b_dna, frame=frame,
                    toward=a.answer == tonight_round.EITHER,
                )
        if seat["role"] != "guest":
            tilts.append(tilt)
        per_participant[seat["participant_id"]] = {
            t: b.mu + tonight_tilt.adjustment(tilt, dna.get(t, {}), frame)
            for t, b in played.beliefs.items()
        }
    room["slate"] = tonight_combine.combine(
        per_participant=per_participant,
        member_ledger={c.title_id: list(c.scores.values()) for c in room["pool"]},
        tilts=tilts, axes=axes, dna=dna,
    )
    room["state"] = "ballot"


@app.get("/api/tonight/sessions/{session_id}/ballot")
def tonight_ballot_card(
    session_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = _tonight_room(session_id)
    slate = room["slate"]
    by_id = {c.title_id: c for c in room.get("pool", [])}
    titles = [] if slate is None else slate.ballot_titles
    return {
        "session_id": session_id,
        "slate": [
            {"title_id": t,
             "slot": "wildcard" if slate and t == slate.wildcard else "finalist",
             "name": by_id[t].name, "year": by_id[t].year,
             "runtime_min": by_id[t].runtime_min, "poster_path": by_id[t].poster_path}
            for t in titles if t in by_id
        ],
        "submitted": len(room["ballots"]),
        "seated": len(room["seats"]),
        "revealed": len(room["ballots"]) >= len(room["seats"]),
    }


@app.post("/api/tonight/seats/{participant_id}/ballot")
def tonight_submit_ballot(
    participant_id: int, body: TonightBallotBody,
    spielplan_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _me(spielplan_session)
    room = next(
        (r for r in STATE["tonight"].values()
         if any(s["participant_id"] == participant_id for s in r["seats"])), None
    )
    if room is None:
        raise HTTPException(404, {"reason": "no_seat", "message": "no such seat"})
    if room["state"] != "ballot":
        raise HTTPException(409, {"reason": "not_ballot"})
    room["ballots"][participant_id] = list(body.approved)
    revealed = len(room["ballots"]) >= len(room["seats"])
    if revealed:
        room["state"] = "resolved"
        room["ended_at"] = datetime.now(UTC).isoformat()
    return {"submitted": len(room["ballots"]), "seated": len(room["seats"]),
            "revealed": revealed}


@app.get("/api/tonight/sessions/{session_id}/result")
def tonight_result(
    session_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = _tonight_room(session_id)
    if len(room["ballots"]) < len(room["seats"]):
        raise HTTPException(409, {"reason": "still_voting",
                                  "message": "approvals stay hidden until everyone has submitted"})
    slate = room["slate"]
    by_id = {c.title_id: c for c in room["pool"]}
    approvals = {t: 0 for t in slate.ballot_titles}
    for approved in room["ballots"].values():
        for title_id in approved:
            approvals[title_id] = approvals.get(title_id, 0) + 1
    ordered = sorted(
        slate.ballot_titles,
        key=lambda t: (-approvals.get(t, 0), -dict(slate.ranked).get(t, 0.0), t),
    )
    winner_id = ordered[0]
    seated = len(room["seats"])

    def card(title_id: int, slot: str) -> dict[str, Any]:
        c = by_id[title_id]
        return {
            "title_id": title_id, "slot": slot, "name": c.name, "year": c.year,
            "runtime_min": c.runtime_min, "poster_path": c.poster_path,
            "approvals": approvals.get(title_id, 0),
            "fit_line": c.fit_line,
            "match_lines": [
                {"name": s["name"], "line": f"pulls {s['name']} with the pool's own terms",
                 "terms": [], "sign": "pull"}
                for s in room["seats"]
            ],
            "conflict": slate.conflict if slot == "finalist" else None,
            "play_url": None,
        }

    return {
        "session_id": session_id,
        "beat": "VOTES REVEALED TOGETHER",
        "winner": card(winner_id, "finalist"),
        "approval_share": approvals.get(winner_id, 0) / seated if seated else 0.0,
        "participants": seated,
        "unanimous": approvals.get(winner_id, 0) == seated,
        "finalists": [card(t, "finalist") for t in slate.finalists],
        "wildcard": None if slate.wildcard is None else card(slate.wildcard, "wildcard"),
        "runners_up": [
            card(t, "runner_up") for t, _ in slate.ranked
            if t not in slate.ballot_titles and t in by_id
        ][:4],
    }


@app.get("/api/tonight/sessions/{session_id}/evaluation")
def tonight_evaluation(
    session_id: int, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    _me(spielplan_session)
    room = _tonight_room(session_id)
    held = [
        a for answers in room["answers"].values() for a in answers
        if a.selection == tonight_round.SELECTION_HOLDOUT
    ]
    counted: dict[str, int] = {r: 0 for r in tonight_round.END_REASONS}
    for seat in room["seats"]:
        if seat["ended_by"]:
            counted[seat["ended_by"]] = counted.get(seat["ended_by"], 0) + 1
    return {
        "session_id": session_id,
        "approval_share": None,
        "participants": len(room["seats"]),
        "shortlist_agreement": {"pairs": len(held), "decisive": 0, "agreed": 0, "rate": None},
        "ended_by": counted,
    }


@app.post("/api/tonight/solo")
def tonight_solo_route(
    body: TonightSoloBody, spielplan_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    user = _me(spielplan_session)
    seat = tonight_pool.Seat(participant_id=user["id"], user_id=user["id"], is_member=True)
    candidates = _tonight_candidates(
        [seat], body.kind, body.runtime_budget_min, body.include_rewatches
    )
    provenance = tonight_solo.provenance(
        budget_min=body.runtime_budget_min, answers=len(body.answers),
        include_rewatches=body.include_rewatches,
    )
    if not candidates:
        return {"picks": [], "wildcard": None, "provenance": provenance,
                "empty": f"Nothing in the library fits {body.runtime_budget_min} minutes "
                         "tonight — widen the budget or include rewatches.",
                "pair": None, "answered": 0, "sharpened": False}
    dna = _tonight_dna()
    order = tonight_combine.ranked({c.title_id: c.group_score for c in candidates})
    by_id = {c.title_id: c for c in candidates}
    span = max(len(order) - 1, 1)
    start = (body.offset * tonight_solo.PICKS) % span if body.offset else 0
    chosen = [t for t, _ in order[start:start + tonight_solo.PICKS]]
    if len(chosen) < tonight_solo.PICKS:
        chosen += [t for t, _ in order if t not in chosen][: tonight_solo.PICKS - len(chosen)]
    wildcard = tonight_combine.wildcard_from(order, chosen, dna)

    def card(title_id: int, *, stretch: bool) -> dict[str, Any]:
        c = by_id[title_id]
        terms = sorted(dna.get(title_id, {}).items(), key=lambda kv: -kv[1])[:2]
        return {
            "title_id": title_id, "name": c.name, "year": c.year,
            "runtime_min": c.runtime_min, "poster_path": c.poster_path,
            "fit_line": c.fit_line, "over_budget_min": c.over_budget_min,
            "why": tonight_solo.STRETCH_WHY if stretch
            else tonight_solo.why_line([t for t, _ in terms]) if terms
            else "top of your ledger tonight",
            "terms": [{"term": t, "tier": "extracted"} for t, _ in terms],
        }

    return {
        "picks": [card(t, stretch=False) for t in chosen],
        "wildcard": None if wildcard is None else card(wildcard, stretch=True),
        "provenance": provenance,
        "empty": None,
        "answered": len(body.answers),
        "sharpened": bool(body.answers),
        "pair": None,
        "stop_reason": None,
        "tilt": {},
    }


if __name__ == "__main__":
    import uvicorn

    fx.make_bundle(BUNDLE)
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
