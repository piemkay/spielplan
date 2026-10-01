"""Library routes (§6.0). `kind` is required and repeated everywhere: an unpartitioned list is a
measured bug (§4.1 rule 5), and a default would hide it.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.db import dna_terms, genres, library
from spielplan.home import rail, suggest, why, wish
from spielplan.models import artifacts, basis
from spielplan.rank import read as rank_read
from spielplan.scoring import serve

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
    person_id: list[int] | None = Query(None),
    owned_only: bool = False,
    sort: Literal["for_you", "newest"] | None = None,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """`kind` and `person_id` repeat (`?kind=` is a 422, never "everything"). The response's `sort` is
    the order really used: `match` under a search, `newest` when nothing of the member's ranks the
    selection (decision 515); `for_you_available` says whether their order exists."""
    try:
        kinds = library.normalise_kinds(kind)
        # Decision 473: an unknown genre is a wrong question, not an empty grid.
        genre = genres.canonical(genre) if genre else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    bundle = await artifacts.active_bundle_version(conn)
    personal = await serve.personal_kinds(
        conn, user_id=user.id, kinds=kinds, bundle_version=bundle
    )
    if q and q.strip():
        effective = "match"
    elif sort == "newest" or not personal:
        effective = "newest"
    else:
        effective = "for_you"

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
        sort="for_you" if effective == "for_you" else "newest",
        bundle_version=bundle,
    )
    # Decision 516: the card leads with the original title where it is the viewer's language.
    await library.carry_original_names(conn, rows)
    return {
        "kinds": kinds,
        "sort": effective,
        "for_you_available": bool(personal),
        "total": total,
        # §6.0: the hidden count, under the SAME filters as the list.
        "hidden": await library.count_by_kind(
            conn, exclude=kinds, user_id=user.id, q=q, genre=genre, decade=decade,
            seen=seen, person_id=person_id, owned_only=owned_only,
        ),
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get("/titles/{title_id}")
async def title_detail(title_id: int, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """§6.0's title card: every section labelled with its provenance."""
    title = await library.get_title(conn, title_id, user_id=user.id)
    if title is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such title")

    store = request.app.state.artifacts
    # Decision 486: model numbers only while Show the model is on, gated where the payload is built.
    show_model = rail.visible_to(user)
    # Resolved once, so the card cannot mix two vocabularies (§10).
    version = await dna_terms.active_version(conn)
    dna = await library.dna_for(conn, title_id, version=version)
    # Decision 486: a term is shown by its shipped label, never its id.
    labels = await dna_terms.labels_for(
        conn, [t["term"] for t in dna["extracted"]] + [p["term"] for p in dna["projected"]]
    )

    # Read unconditionally: "no server" and "not in your library" are different sentences.
    link = await registry.play_link(conn)
    jf_url = None
    play_reason = None
    if link is None:
        play_reason = "no_server"
    elif not title.get("jellyfin_id"):
        play_reason = "not_in_library"
    else:
        jf_url = link(title["jellyfin_id"])

    body: dict[str, Any] = {
        "title": {
            k: title[k]
            for k in (
                "id", "kind", "name", "original_name", "year", "runtime_min", "overview",
                "tagline", "poster_path", "backdrop_path", "trailer_key", "is_owned",
                "placement", "seen_state", "imdb_id", "tmdb_id",
                "original_language",
            )
        },
        "genres": await library.title_genres(conn, title_id),
        "credits": await library.credits_for(conn, title_id),
        # §4.1 rule 3: labelled at the boundary; the note is the same fact for members (decision 486).
        "platform_ratings": {
            "note": "For reference only — these never change your suggestions.",
            "items": await library.platform_ratings(conn, title_id),
        },
        # §4.1 rule 1 — two tiers, two lists.
        "dna": {
            "extracted": [_extracted(t, labels, show_model) for t in dna["extracted"]],
            "projected": [{**p, **labels[p["term"]]} for p in dna["projected"]],
            "note": "extracted tags are quote-verified; projected tags are inferred",
        },
        # Decision 531: §6.3's ranking rows. Unreadable constants hide them rather than 503 the card.
        "ranking": None
        if request.app.state.hyperparams is None
        else await rank_read.standing(
            conn, user_id=user.id, kind=title["kind"], title_id=title_id,
            hp=request.app.state.hyperparams,
        ),
        # Decision 515: why this title is suggested, or None.
        "why": await suggest.why_suggested(
            conn, user_id=user.id, title_id=title_id,
            bundle_version=await artifacts.active_bundle_version(conn),
        ),
        "shares": []
        if version is None
        else await why.shares_with(
            conn, user_id=user.id, title_id=title_id, kind=title["kind"], version=version
        ),
        "wish": {
            **await wish.state_for(conn, user_id=user.id, title_id=title_id),
            "likely_too": await wish.likely_too(
                conn, viewer_id=user.id, title_id=title_id,
                bundle_version=await artifacts.active_bundle_version(conn),
            ),
        },
        "actions": {
            "play_on_jellyfin": jf_url,
            "play_reason": play_reason,
        },
    }
    if show_model:
        # §6.0's model line, behind Show the model since decision 486: it reads this viewer's fit. With no
        # bundle there is nothing honest to print (§3.1).
        body["model_line"] = (
            {"available": False, "reason": "no artifact bundle imported"}
            if store.is_empty
            else await serve.model_line(
                conn, user_id=user.id, title_id=title_id, bundle_version=store.version
            )
        )
    return body


# The extracted tier's weights: a Show-the-model annotation, never member copy (decision 486).
_TAG_NUMBERS = ("salience", "confidence", "n_sources")


def _extracted(
    tag: dict[str, Any], labels: dict[str, dict[str, Any]], show_model: bool
) -> dict[str, Any]:
    shaped = {**tag, **labels[tag["term"]]}
    if not show_model:
        for key in _TAG_NUMBERS:
            shaped.pop(key, None)
    return shaped


@router.get("/facets")
async def facets(
    conn: DB,
    _: ActiveUser,
    kind: list[Literal["movie", "series"]] = Query(default=["movie"]),
) -> dict[str, Any]:
    try:
        kinds = library.normalise_kinds(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {
        "kinds": kinds,
        "genres": await library.genres(conn, kinds),
        "decades": await library.decades(conn, kinds),
    }


@router.get("/config")
async def client_config(request: Request) -> dict[str, Any]:
    """Unauthenticated: only what the shell needs before a user is known, no user or connector detail."""
    store = request.app.state.artifacts
    return {
        "public_url": settings().public_url,
        "bundle": store.summary() if not store.is_empty else None,
        "has_bundle": not store.is_empty,
        # A bundle is imported but this process could not load it: the header says a restart is owed.
        # From memory, not the database (decision 271).
        "restart_required": basis.unloaded(request.app.state),
        # The poster URL version (`art/poster.url_epoch`): an opaque digest, not a date.
        "art_epoch": getattr(request.app.state, "art_epoch", None),
    }
