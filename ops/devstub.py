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

import sqlite3
import sys
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from tests.fixtures import make_bundle as fx  # noqa: E402

BUNDLE = ROOT / "data" / "devstub-bundle"
STATE: dict[str, Any] = {"imported": False, "users": {}, "next_id": 1}

app = FastAPI(title="Spielplan dev harness")


def _db() -> sqlite3.Connection:
    if not (BUNDLE / "content.sqlite").exists():
        fx.make_bundle(BUNDLE)
    db = sqlite3.connect(f"file:{BUNDLE / 'content.sqlite'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


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
    user = {"id": STATE["next_id"], "name": body.name, "role": "admin",
            "must_change_password": False, "auth_method": "password",
            "admin_reauth_required": False, "show_model": False}
    STATE["next_id"] += 1
    return _sign_in(response, user)


@app.post("/api/setup/members", status_code=201)
def create_member(body: MemberInit) -> dict[str, Any]:
    user = {"id": STATE["next_id"], "name": body.name, "role": body.role,
            "must_change_password": True, "auth_method": "password",
            "admin_reauth_required": False, "show_model": False}
    STATE["next_id"] += 1
    STATE["users"][f"pending-{user['id']}"] = user
    return {**user, "one_time_password": "kq7mrn24tphs",
            "note": "shown once — the account is locked to a password change at first login"}


@app.post("/api/setup/connectors")
def seed_connector() -> dict[str, Any]:
    return {"ok": True, "name": "jellyfin", "has_secrets": False}


@app.post("/api/setup/onboarding/complete")
def complete_onboarding() -> dict[str, bool]:
    return {"ok": True}


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
                "placement": "cold_tower" if r["id"] % 3 == 0 else "warm",
                "seen_state": "seen" if r["id"] % 2 else "unseen",
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
            "is_owned": True, "placement": "warm",
            "seen_state": "seen" if t["id"] % 2 else "unseen",
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


if __name__ == "__main__":
    import uvicorn

    fx.make_bundle(BUNDLE)
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
