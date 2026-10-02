"""Film arithmetic's routes (§6.0, decisions 559 and 560), all under `/api/mix` so none collides with
`/api/titles/{title_id}`. A refused recipe is a 422 naming its limit."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.api.library import CatalogFilters
from spielplan.db import library
from spielplan.home import mix, mix_table

router = APIRouter(prefix="/api/mix", tags=["mix"])

Kind = Literal["movie", "series"]


def _refused(exc: mix.MixRefused) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.detail())


def _recipe(like: list[str], less: list[str]) -> list[mix.Ingredient]:
    return [mix.ingredient(v, like=True) for v in like] + [mix.ingredient(v, like=False) for v in less]


@router.get("/films")
async def films(conn: DB, _: ActiveUser, q: str = "", limit: int = Query(8, ge=1, le=50)) -> dict[str, Any]:
    """The title picker: either kind, owned or not, among titles carrying two or more DNA terms."""
    return {"items": await mix_table.picker(conn, q, limit)}


@router.get("/titles")
async def titles(
    conn: DB,
    user: ActiveUser,
    request: Request,
    filters: CatalogFilters,
    kind: Kind,
    like: list[str] = Query([]),
    less: list[str] = Query([]),
    pool: Literal["library", "beyond"] = "library",
    sort: Literal["match", "for_you"] = "match",
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """A recipe's grid for one kind: `pool` picks the library or the well-known titles beyond it."""
    try:
        recipe = _recipe(like, less)
        eligible = await library.eligible_ids(conn, kinds=[kind], user_id=user.id, **filters)
        return await mix_table.recipe_page(
            request.app.state, conn, user_id=user.id, kind=kind, recipe=recipe, eligible=eligible,
            pool=pool, sort=sort, limit=limit, offset=offset,
        )
    except mix.MixRefused as exc:
        raise _refused(exc) from exc


@router.get("/twists")
async def twists(
    conn: DB,
    user: ActiveUser,
    request: Request,
    filters: CatalogFilters,
    kind: list[Literal["movie", "series", "both"]] = Query(),
    like: list[str] = Query([]),
    less: list[str] = Query([]),
    seed: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Up to three twists from the member's own films placed high, of each kind shown (`kind` repeats,
    or `both`); `seed + 1` is the shuffle, which pages through them and wraps."""
    try:
        kinds = library.normalise_kinds(library.KINDS if "both" in kind else kind)
        recipe = _recipe(like, less)
        eligible = await library.eligible_ids(conn, kinds=kinds, user_id=user.id, **filters)
        return await mix_table.twist_page(
            request.app.state, conn, user_id=user.id, kinds=kinds, recipe=recipe, eligible=eligible,
            seed=seed,
        )
    except mix.MixRefused as exc:
        raise _refused(exc) from exc
