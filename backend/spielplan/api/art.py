"""Same-origin, session-gated poster route (§6.8; decisions 483-485), so no `<img src>` names a
third-party host. Where the bytes come from is `art/poster.py`'s.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from spielplan.api import deps
from spielplan.api.deps import ActiveUserBrief
from spielplan.art.poster import Answer

router = APIRouter(prefix="/api", tags=["art"])


def _response(request: Request, answer: Answer) -> Response:
    # `private`: no edge cache may hold a session-gated answer. Never `immutable`: the URL names a title.
    headers = {
        "Cache-Control": f"private, max-age={answer.max_age}",
        "X-Content-Type-Options": "nosniff",
    }
    if answer.status != 200:
        return Response(status_code=answer.status, headers=headers)
    if answer.etag:
        headers["ETag"] = answer.etag
        if request.headers.get("if-none-match") == answer.etag:
            return Response(status_code=304, headers=headers)
    return Response(answer.body, media_type=answer.content_type, headers=headers)


@router.get("/art/{title_id}/poster", response_class=Response)
async def poster(title_id: int, request: Request, user: ActiveUserBrief) -> Response:
    answer = await request.app.state.art.poster(title_id, connect=deps.brief_connection)
    # A whole `Response` skips FastAPI's, and with it the slid session's Set-Cookie.
    return deps.carry_slid_session_cookie(request, _response(request, answer))
