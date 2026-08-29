"""A fake Jellyfin server. Test infrastructure — never shipped in the app image.

§7.3 promises that seen state flows *both ways* and that the Played write uses the linked
user's own token rather than the admin key. Neither claim can be checked against a mock inside
the process that makes it: a mock asserts that we called what we think we call. This is a real
HTTP server that behaves the way Jellyfin >= 10.9 behaves on the handful of routes §7.1 names,
including the part that matters most — **it refuses the admin API key on `/UserPlayedItems`**.
That refusal is what turns "we use per-user tokens" from a comment into a test.

Two ways in:

  * Playwright drives it over HTTP as a compose service (`ops/compose.e2e.yml`), so the browser
    test can flip a flag in "Jellyfin" and watch it arrive in the app.
  * The backend integration tests mount it through `httpx.ASGITransport`, so the same server
    answers with no socket and no port.

The `/_test/*` routes are the control surface and have no Jellyfin counterpart.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

TICKS_PER_MINUTE = 60 * 10_000_000

API_KEY = os.environ.get("FAKE_JELLYFIN_API_KEY", "fake-admin-key")
SERVER_VERSION = os.environ.get("FAKE_JELLYFIN_VERSION", "10.10.3")

# Mirrors backend/tests/fixtures/make_bundle.py so every item resolves to a fixture title —
# except the last two, which are deliberately awkward:
#   * "Tampopo" carries NO ProviderIds, so it exercises the name/year fallback;
#   * "Christmas 2019" resolves to nothing at all and must not create a title (§4.2: title.id
#     is carried over verbatim from the corpus, so this connector never mints one).
ITEMS: list[dict[str, Any]] = [
    {"Id": "jf-1", "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
     "RunTimeTicks": 170 * TICKS_PER_MINUTE, "ProviderIds": {"Imdb": "tt0113277", "Tmdb": "949"}},
    {"Id": "jf-2", "Name": "Prisoners", "Type": "Movie", "ProductionYear": 2013,
     "RunTimeTicks": 153 * TICKS_PER_MINUTE,
     "ProviderIds": {"Imdb": "tt1392214", "Tmdb": "146233"}},
    {"Id": "jf-3", "Name": "Paddington 2", "Type": "Movie", "ProductionYear": 2017,
     "RunTimeTicks": 103 * TICKS_PER_MINUTE, "ProviderIds": {"Tmdb": "346648"}},
    {"Id": "jf-6", "Name": "Severance", "Type": "Series", "ProductionYear": 2022,
     "RunTimeTicks": 48 * TICKS_PER_MINUTE,
     "ProviderIds": {"Imdb": "tt11280740", "Tmdb": "95396"}},
    {"Id": "jf-7", "Name": "The Bear", "Type": "Series", "ProductionYear": 2022,
     "RunTimeTicks": 30 * TICKS_PER_MINUTE,
     "ProviderIds": {"Imdb": "tt14452776", "Tmdb": "136315"}},
    {"Id": "jf-8", "Name": "Tampopo", "Type": "Movie", "ProductionYear": 1985,
     "RunTimeTicks": 114 * TICKS_PER_MINUTE, "ProviderIds": {}},
    {"Id": "jf-x", "Name": "Christmas 2019", "Type": "Movie", "ProductionYear": 2019,
     "RunTimeTicks": 41 * TICKS_PER_MINUTE, "ProviderIds": {}},
]

USERS: list[dict[str, Any]] = [
    {"Id": "jf-user-patrick", "Name": "patrick", "Policy": {"IsAdministrator": True}},
    {"Id": "jf-user-jenny", "Name": "jenny", "Policy": {"IsAdministrator": False}},
]

PASSWORD = os.environ.get("FAKE_JELLYFIN_PASSWORD", "jf-password")


class State:
    """Everything mutable, in one object so `/\\_test/reset` is a single assignment."""

    def __init__(self) -> None:
        self.played: dict[str, set[str]] = {u["Id"]: set() for u in USERS}
        self.tokens: dict[str, str] = {}          # token -> jellyfin user id
        self.sessions: list[dict[str, Any]] = []
        self.write_log: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.__init__()


state = State()
router = APIRouter()


def _require_api_key(token: str | None) -> None:
    if token != API_KEY:
        raise HTTPException(401, "invalid api key")


def _user_for_token(token: str | None) -> str | None:
    return state.tokens.get(token or "")


def _item(item_id: str) -> dict[str, Any]:
    for item in ITEMS:
        if item["Id"] == item_id:
            return item
    raise HTTPException(404, "no such item")


@router.get("/System/Info/Public")
async def info() -> dict[str, Any]:
    """Unauthenticated, exactly as Jellyfin serves it — which is what lets the admin's test
    button tell a wrong URL apart from a wrong key."""
    return {"ServerName": "Fake Jellyfin", "Version": SERVER_VERSION, "Id": "fake-server"}


@router.get("/Users")
async def users(x_emby_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _require_api_key(x_emby_token)
    return USERS


# Jellyfin returns a small default projection and only adds the rest when `Fields` asks. A fake
# that ignores `Fields` lets a client "prove" it requests ProviderIds while never requiring it —
# the assertion becomes the implementation compared to itself. So the projection is real here.
DEFAULT_FIELDS = {"Id", "Name", "Type", "ProductionYear", "RunTimeTicks"}
ITEM_TYPES_ASKED: list[str] = []


@router.get("/Items")
async def items(
    userId: str = Query(...),  # noqa: N803 - Jellyfin's own parameter name
    StartIndex: int = Query(default=0),  # noqa: N803
    Limit: int = Query(default=500),  # noqa: N803
    Recursive: str = Query(default="false"),  # noqa: N803
    IncludeItemTypes: str = Query(default=""),  # noqa: N803
    Fields: str = Query(default=""),  # noqa: N803
    x_emby_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_api_key(x_emby_token)
    if userId not in state.played:
        raise HTTPException(404, "no such user")

    # Without Recursive the real server returns the library folders, not the films inside them.
    # A client that forgets it gets an empty result rather than a silently-correct one.
    if Recursive.lower() != "true":
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": StartIndex}

    wanted_types = {t.strip().lower() for t in IncludeItemTypes.split(",") if t.strip()}
    # Recorded so a test can assert the client narrowed the query rather than asking for
    # everything and filtering afterwards.
    globals()["ITEM_TYPES_ASKED"] = sorted(wanted_types)
    fields = {f.strip() for f in Fields.split(",") if f.strip()}
    matching = [
        item for item in ITEMS
        if not wanted_types or str(item["Type"]).lower() in wanted_types
    ]

    played = state.played[userId]
    page = []
    for item in matching[StartIndex : StartIndex + Limit]:
        projected = {k: v for k, v in item.items() if k in DEFAULT_FIELDS or k in fields}
        if "UserData" in fields:
            projected["UserData"] = {
                "Played": item["Id"] in played, "PlaybackPositionTicks": 0
            }
        page.append(projected)
    return {"Items": page, "TotalRecordCount": len(matching), "StartIndex": StartIndex}


@router.get("/Sessions")
async def sessions(x_emby_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _require_api_key(x_emby_token)
    return state.sessions


@router.post("/Users/AuthenticateByName")
async def authenticate(request: Request) -> dict[str, Any]:
    body = await request.json()
    name = str(body.get("Username") or "")
    match = next((u for u in USERS if u["Name"].lower() == name.lower()), None)
    if match is None or body.get("Pw") != PASSWORD:
        raise HTTPException(401, "invalid username or password")
    token = f"user-token-{match['Id']}"
    state.tokens[token] = match["Id"]
    return {"AccessToken": token, "User": match}


@router.api_route("/UserPlayedItems/{item_id}", methods=["POST", "DELETE"])
async def set_played(
    item_id: str,
    request: Request,
    userId: str = Query(...),  # noqa: N803
    x_emby_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """The one write, and the one place this fake is stricter than a mock would be.

    A real Jellyfin admin key *would* be accepted here — it is admin-equivalent and has no
    read-only variant (§14.3). This fake refuses it on purpose, because the app's own restraint
    is the only thing enforcing §7.3's least-privilege path and a test that cannot fail when
    that restraint is removed is not testing it.
    """
    if x_emby_token == API_KEY:
        raise HTTPException(403, "this fake refuses the admin key on Played writes (§7.3)")
    token_user = _user_for_token(x_emby_token)
    if token_user is None:
        raise HTTPException(401, "invalid token")
    if token_user != userId:
        raise HTTPException(403, "a user token may only set that user's own state")

    _item(item_id)
    played = request.method == "POST"
    if played:
        state.played[userId].add(item_id)
    else:
        state.played[userId].discard(item_id)
    state.write_log.append({"user": userId, "item": item_id, "played": played})
    return {"Played": played, "ItemId": item_id, "UserId": userId}


# --- control surface (no Jellyfin counterpart) ------------------------------------------


class PlayedControl(BaseModel):
    user_id: str
    item_id: str
    played: bool = True


class SessionControl(BaseModel):
    user_id: str
    item_id: str
    fraction: float = 0.95
    session_id: str = "sess-1"


control = APIRouter(prefix="/_test")


@control.post("/reset")
async def reset() -> dict[str, bool]:
    state.reset()
    return {"ok": True}


@control.post("/played")
async def force_played(body: PlayedControl) -> dict[str, Any]:
    """Simulate someone marking a title watched *in Jellyfin* — the other direction."""
    if body.played:
        state.played.setdefault(body.user_id, set()).add(body.item_id)
    else:
        state.played.setdefault(body.user_id, set()).discard(body.item_id)
    return {"ok": True, "played": sorted(state.played[body.user_id])}


@control.post("/session")
async def force_session(body: SessionControl) -> dict[str, Any]:
    item = _item(body.item_id)
    runtime = int(item["RunTimeTicks"])
    state.sessions = [
        s for s in state.sessions if s["Id"] != body.session_id
    ] + [
        {
            "Id": body.session_id,
            "UserId": body.user_id,
            "NowPlayingItem": {
                "Id": body.item_id,
                "Name": item["Name"],
                "RunTimeTicks": runtime,
                "UserData": {"Played": body.item_id in state.played.get(body.user_id, set())},
            },
            "PlayState": {"PositionTicks": int(runtime * body.fraction)},
        }
    ]
    return {"ok": True, "sessions": len(state.sessions)}


@control.post("/sessions/clear")
async def clear_sessions() -> dict[str, bool]:
    state.sessions = []
    return {"ok": True}


@control.get("/state")
async def dump() -> dict[str, Any]:
    return {
        "played": {k: sorted(v) for k, v in state.played.items()},
        "tokens": list(state.tokens),
        "sessions": len(state.sessions),
        "writes": state.write_log,
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Fake Jellyfin", docs_url=None, redoc_url=None)
    app.include_router(router)
    app.include_router(control)
    return app


app = create_app()

if __name__ == "__main__":  # pragma: no cover - the compose entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8096")))
