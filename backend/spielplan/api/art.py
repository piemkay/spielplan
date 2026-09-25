"""`GET /api/art/{title_id}/poster`, the one place a poster enters a page. Spec v2.1 §6.8
("Poster-forward 2:3 cards"), §3.1, §3.2; decisions 483, 484 and 485.

Same-origin and session-gated, so no `<img src>` in this app names a third-party host, a phone's
address and Referer never reach one, and a locked account (§3.1) is refused here as on every other
route - behind `ActiveUserBrief` rather than `ActiveUser`, for the reason `api/deps.py` gives
beside it. Everything about where the bytes come from is `art/poster.py`'s; this module decides
only what the answer looks like on the wire.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from spielplan.api import deps
from spielplan.api.deps import ActiveUserBrief
from spielplan.art.poster import Answer

router = APIRouter(prefix="/api", tags=["art"])


def _response(request: Request, answer: Answer) -> Response:
    # Decision 483: `private` so no edge cache holds an answer a session gated, a max-age on the
    # 404 as well as on the 200, and never `immutable` - the URL names a title, not a file.
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
    # A `Response` returned whole skips the one FastAPI builds, and with it a session slide's
    # Set-Cookie; `current_user` parks that header on the request for exactly this.
    return deps.carry_slid_session_cookie(request, _response(request, answer))
