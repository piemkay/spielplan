"""Library / Home routes. Spec v2.1 §6.0.

M0 scope: "a paginated list over `title`, partitioned by kind (§4.1 rule 5), filter/search on
title/alias/genre/decade/seen-state, and the title detail card". Home shelves are M2 (§12).

`kind` is a required query parameter on the listing route. That is not defensiveness; §4.1
rule 5 makes an unpartitioned list a measured bug ("the unpartitioned crowd top-10 is 8/10 TV
series"), and a default would hide it.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.core.config import settings
from spielplan.db import library

router = APIRouter(prefix="/api", tags=["library"])


@router.get("/titles")
async def list_titles(
    conn: DB,
    user: ActiveUser,
    kind: list[Literal["movie", "series"]] = Query(
        ..., description="§4.1 rule 5: one or both, never neither. Repeat the parameter for both."
    ),
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    seen: Literal["any", "seen", "unseen"] = "any",
    person_id: int | None = None,
    owned_only: bool = False,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Owner decision 2026-08-29: kind is two toggles, either or both active, never neither.

    Repeated rather than comma-joined (`?kind=movie&kind=series`) so the empty selection is
    unrepresentable in the URL — `?kind=` is a validation error, not a silent "everything".
    """
    try:
        kinds = library.normalise_kinds(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    rows, total = await library.list_titles(
        conn,
        kinds=kinds,
        user_id=user.id,
        q=q,
        genre=genre,
        decade=decade,
        seen=seen,
        person_id=person_id,
        owned_only=owned_only,
        limit=limit,
        offset=offset,
    )
    return {
        "kinds": kinds,
        "total": total,
        # §6.0: a toggle that hides things has to say how many. Silent truncation is the
        # failure this control was introduced to fix, so the count travels with the list.
        "hidden": await library.count_by_kind(conn, exclude=kinds),
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get("/titles/{title_id}")
async def title_detail(title_id: int, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """The §6.0 title detail card.

    Everything the card shows is labelled with its provenance: the DNA tiers stay separate,
    platform scores are marked display-only, and the model line reports what it actually has
    rather than inventing numbers when no bundle is loaded.
    """
    title = await library.get_title(conn, title_id, user_id=user.id)
    if title is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such title")

    store = request.app.state.artifacts
    dna = await library.dna_for(conn, title_id)

    jf_url = None
    if title.get("jellyfin_id"):
        cfg = await conn.fetchval("SELECT config->>'url' FROM connector_config WHERE name = 'jellyfin'")
        if cfg:
            # §7.1: deep-link to the server's web player; direct playback is a later refinement.
            jf_url = f"{cfg.rstrip('/')}/web/#/details?id={title['jellyfin_id']}"

    return {
        "title": {
            k: title[k]
            for k in (
                "id", "kind", "name", "original_name", "year", "runtime_min", "overview",
                "tagline", "poster_path", "backdrop_path", "trailer_key", "is_owned",
                "placement", "seen_state", "imdb_id", "tmdb_id",
            )
        },
        "credits": await library.credits_for(conn, title_id),
        # §4.1 rule 3 — labelled at the boundary so the client cannot forget.
        "platform_ratings": {
            "display_only": True,
            "note": "display-only schema — platform scores are a popularity conduit and are "
                    "never model features",
            "items": await library.platform_ratings(conn, title_id),
        },
        # §4.1 rule 1 — two tiers, two lists.
        "dna": {
            "extracted": dna["extracted"],
            "projected": dna["projected"],
            "note": "extracted tags are quote-verified; projected tags are inferred",
        },
        # §6.0: "the model line in the data voice (`b(t) 0.52 · β 0.8 · gate 0.93`)".
        # With no bundle there is nothing honest to print, so the card says so (§3.1).
        "model_line": (
            {"available": False, "reason": "no artifact bundle imported"}
            if store.is_empty
            else {"available": True, "bundle": store.version}
        ),
        "actions": {
            "play_on_jellyfin": jf_url,
            "show_on_map": {"title_id": title_id},
        },
    }


@router.get("/titles/{title_id}/similar-by-term")
async def similar_by_term(title_id: int, conn: DB, _: ActiveUser, limit: int = 12) -> dict[str, Any]:
    """§6.4 wander: neighbours by *shared term*, each edge labelled with the terms it rides on.
    Every connection is nameable — edges are DNA terms, never opaque similarity.

    Extracted and projected neighbours are returned separately (rule 1).
    """
    async def neighbours(table: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await conn.fetch(
                f"""
                WITH mine AS (SELECT term FROM {table} WHERE title_id = $1)
                SELECT o.title_id, t.name, t.year, t.kind,
                       array_agg(o.term ORDER BY o.term) AS via,
                       count(*) AS shared
                  FROM {table} o
                  JOIN mine m ON m.term = o.term
                  JOIN title t ON t.id = o.title_id
                 WHERE o.title_id <> $1
                 GROUP BY o.title_id, t.name, t.year, t.kind
                 ORDER BY count(*) DESC, t.name
                 LIMIT $2
                """,
                title_id,
                limit,
            )
        ]

    return {
        "extracted": await neighbours("dna_tag"),
        "projected": await neighbours("dna_projected"),
    }


@router.get("/facets")
async def facets(
    conn: DB,
    _: ActiveUser,
    kind: list[Literal["movie", "series"]] = Query(default=["movie"]),
) -> dict[str, Any]:
    """Filter vocabulary for the catalog controls, over the selected kinds."""
    try:
        kinds = library.normalise_kinds(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {
        "kinds": kinds,
        "genres": await library.genres(conn, kinds),
        "decades": await library.decades(conn, kinds),
    }


@router.get("/people/{person_id}")
async def person_detail(person_id: int, conn: DB, _: ActiveUser) -> dict[str, Any]:
    """§6.0: 'credits, each person tappable → filters the library to their filmography'."""
    person = await conn.fetchrow("SELECT id, name, profile_path FROM person WHERE id = $1", person_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such person")
    rows = await conn.fetch(
        """
        SELECT DISTINCT t.id, t.kind, t.name, t.year, t.poster_path,
               array_agg(DISTINCT c.job) AS jobs
          FROM credit c JOIN title t ON t.id = c.title_id
         WHERE c.person_id = $1
         GROUP BY t.id, t.kind, t.name, t.year, t.poster_path
         ORDER BY t.year DESC NULLS LAST
        """,
        person_id,
    )
    return {"person": dict(person), "filmography": [dict(r) for r in rows]}


@router.get("/config")
async def client_config(request: Request) -> dict[str, Any]:
    """What the shell needs before a user is known: whether a bundle exists, and the origin.
    Deliberately unauthenticated and deliberately free of any user or connector detail."""
    store = request.app.state.artifacts
    return {
        "public_url": settings().public_url,
        "bundle": store.summary() if not store.is_empty else None,
        "has_bundle": not store.is_empty,
    }
