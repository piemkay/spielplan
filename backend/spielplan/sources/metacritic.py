"""Metacritic - the best-structured critic corpus on the open web, and the slug that must be right.

Spec v2.1 §8 stage 2 ("metacritic:page->reviews", `spec:367`), §8 stage 5, §8's politeness clause
(`spec:404`); decisions 334, 340, 372, 374.

Every Metacritic review carries a normalised 0-100 score alongside its excerpt, which makes it
the one source where the rating attached to a piece of text is unambiguous. User reviews (0-10)
are collected too, and skew far more negative than IMDb's - "exactly the tail the corpus is
otherwise short of" (`mdc/sources/metacritic.py:1-8`). §8 stage 4's gate counts these.

PORT VERDICT: **ported with named changes** from `mdc/sources/metacritic.py` (152 lines). Taken
verbatim: `BASE` (`:19`), `candidate_paths` (`:22-43`) including the same-name refusal and the
year-first ordering, `resolve_path`'s identity argument (`:54-101`), the two review views and the
measurement that fixed them at two (`:137-145`), and `_known_people` (`:46-51`, now
`_ids.known_people`).

THE TWO VIEWS ARE A MEASUREMENT AND NOT A CHOICE. "Verified against the live site:
`?filter=Positive|Negative` and `?page=N` both return a shell whose review list is hydrated
client-side (0 server-rendered reviews), so they are pure wasted requests. The default views ARE
server-rendered and give ~50 user reviews and ~40 critic reviews per title, which is plenty."
(`:137-141`). This host runs at seven-tenths of a request a second; adding a third view costs
another second and a half per title across the whole library and returns nothing.

WHY A GUESS HAS TO BE PROVED HERE AND NOT AT PARSE TIME. `resolve_path`'s own paragraph: "two
films share a name often enough that `movie/alpha` is the 2018 film and `movie/the-beekeeper` the
2024 one, and the site cheerfully serves whichever it has. So a guess has to fetch the title page
and be recognised - by its cast, or failing that its year - before any review page is worth
requesting." A review page carries no identity markup of its own, so an unchecked guess is
invisible from the reviews side, which is why the title page is fetched even when only the
reviews are wanted.

NAMED CHANGE 1: `candidate_paths` IS SPLIT. `slug_candidates` is the pure half - a row in, a list
out - and `candidate_paths` adds the "another title already holds this slug" read the corpus
takes through an optional `conn` argument (`:35-40`). The split is what lets the ordering and the
year-qualification be asserted at the value rather than through a fixture; the refusal keeps its
own test against real rows.

NAMED CHANGE 2: NO SELF-ENQUEUE (`:120-122`) AND NO `task.payload` PATH HAND-OFF (`:135`). Both
kinds are registered in one stage and the driver runs them in priority order, 77 then 86
(`acquire/pipeline.py:46-60`). `metacritic:reviews` reads the slug `metacritic:page` wrote on the
title, and resolves one itself if there is none - which is the corpus's own fallback, kept for
its reason: a review fetch against an unverified guess would write another film's reviews into
this title's pack.

NAMED CHANGE 3: `Permanent` (`:100-101`, `:119`, `:151`) BECOMES A NOTE. Metacritic is not the
required source (decision 334).

NAMED CHANGE 4: THE IDENTITY CHECK READS THE BYTES IN HAND. `:86-93` writes the page, reads the
row back out of the raw store with `rawstore.latest` and decompresses it from disk to run the
check. Here `_views.capture` returns the response it just stored, so the same bytes are checked
without a round trip through the filesystem. The document is stored first either way, which is
what §8's preamble requires and what makes the refusal cheap to revisit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "metacritic"
BASE = "https://www.metacritic.com"


def slug_candidates(row: Any) -> list[str]:
    """The Metacritic paths worth trying for this title, best first. `:22-43`'s pure half.

    A slug Wikidata supplied is an identifier and is the only candidate; Wikidata stores these
    with the type prefix sometimes attached, so both spellings are normalised to one. A slug
    built from the name is a guess, and the year-qualified form comes first: where Metacritic has
    disambiguated two films of one name, it is the only one of the two that can be right.
    """
    prefix = "movie" if row["kind"] == "movie" else "tv"
    if row["metacritic_slug"]:
        slug = row["metacritic_slug"].strip("/")
        return [slug if slug.startswith(("movie/", "tv/")) else f"{prefix}/{slug}"]
    name = row["name"] or row["original_name"]
    if not name:
        return []
    base = f"{prefix}/{_ids.slugify(name)}"
    return [f"{base}-{row['year']}", base] if row["year"] else [base]


async def candidate_paths(conn: asyncpg.Connection, row: Any) -> list[str]:
    """`slug_candidates`, minus any guess another title has already claimed. `:35-40`.

    "A slug derived from the title cannot distinguish two films of the same name - `The
    Beekeeper` (1986) and (2024) both produce `movie/the-beekeeper`, and whichever page exists
    would hand its reviews to both. If another title already holds this slug, refuse to guess."

    Applied to the GUESS only, which is the corpus's own scope: a supplied slug is an identifier
    and §4.1 rule 6 says these columns carry legitimate duplicates, so two titles sharing one is
    a fact rather than a collision.
    """
    candidates = slug_candidates(row)
    if row["metacritic_slug"] or not candidates:
        return candidates
    taken = await conn.fetchval(
        "SELECT count(*) FROM title WHERE metacritic_slug = $1 AND id <> $2",
        candidates[-1], row["id"],
    )
    return [] if taken else candidates


@dataclass(frozen=True)
class _Resolved:
    """A path proved to be about this film, and what proving it cost."""

    path: str = ""
    doc_id: int | None = None
    fetched: bool = False
    note: str = ""


async def resolve_path(ctx: StageContext, row: Any) -> _Resolved:
    """The Metacritic path for this title, proved to be about this film. `:54-101`.

    A slug Wikidata supplied is taken as given and NOT fetched here - the caller fetches it,
    because it has not been fetched yet. A guess is fetched and recognised first, so `fetched` is
    what tells the caller which of the two happened.
    """
    if row["metacritic_slug"]:
        return _Resolved(path=slug_candidates(row)[0])

    candidates = await candidate_paths(ctx.conn, row)
    if not candidates:
        return _Resolved(note="no slug candidate")
    people = await _ids.known_people(ctx.conn, row["id"])

    wrong: list[str] = []
    errors: list[str] = []
    unproven: list[str] = []
    last_doc: int | None = None
    for path in candidates:
        captured = await _views.capture(
            ctx, source=SOURCE, kind="page:main", url=f"{BASE}/{path}/",
            content_type=_views.HTML, min_bytes=_views.MIN_HTML_BYTES,
            request_meta={"path": path}, name=path,
        )
        last_doc = captured.doc_id or last_doc
        if not captured.ok:
            errors.append(f"{path}: {captured.error}")
            continue
        if captured.unchanged:
            # A 304 ON A GUESSED CANDIDATE PROVES NOTHING, AND THIS LOOP HOLDS NOTHING ELSE.
            # `resolve_path` returns above whenever the row carries a slug, and the only writer of
            # that column here is the `set_ids` at the foot of this loop - so a candidate that
            # reaches a conditional request has been fetched before and was NOT accepted.
            # `_views.capture` files a refused page `ok = true` with its validators on purpose
            # (below: "The bytes stay: append-only") and `fetch._validators` keys on the url alone,
            # so the refusal is precisely what arms the 304. The sentence that used to stand here -
            # "it was accepted the first time or the slug would not be on the row" - asserted the
            # negation of its own precondition, and on the strength of it `metacritic:reviews`
            # resolved the same path and filed another film's critic and user pages under this
            # title's entity key. No new evidence is no promotion: the next candidate is tried and
            # a run that saw nothing but 304s refuses, which is where §9's "page_belongs_to_title
            # is not optional" and exit check 9's "nothing written" put it.
            # [M5.3 review cycle 1, m53-c1-slug-01, M53-C1-NET-01]
            unproven.append(path)
            continue
        if not _ids.belongs_to_title(captured.content, year=row["year"], people=people,
                                     mode="metacritic"):
            # The bytes stay: append-only, and a page that turned out to be another film is the
            # honest record of what the guess returned. What it does not get is the slug, or a
            # review fetch.
            wrong.append(path)
            continue
        await _ids.set_ids(ctx.conn, row["id"], metacritic_slug=path)
        return _Resolved(path=path, doc_id=captured.doc_id, fetched=True)

    refusals = [f"{', '.join(wrong)}: a different film of the same name"] if wrong else []
    if unproven:
        # Named apart from `wrong` because the two are different facts and an operator reading
        # §6.6's board acts on them differently: one is a page this app has read and judged, the
        # other is a page it holds bytes for and has never accepted. Both refuse.
        refusals.append(f"{', '.join(unproven)}: unchanged since a fetch that was never accepted")
    if refusals:
        return _Resolved(doc_id=last_doc, note="; ".join(refusals))
    return _Resolved(
        doc_id=last_doc,
        note=f"no metacritic page for any of {', '.join(candidates)}"
             + (f": {'; '.join(errors)}" if errors else ""),
    )


@handler("metacritic:page", source=SOURCE, priority=77, phase="enrich",
         description="Metacritic title page (metascore + user score)")
async def page(ctx: StageContext) -> SourceResult:
    """The title page: the metascore, the user score, and the proof that the slug is this film's.

    `requires=None`: there is no key, and the cost is paid in time. `acquire/hosts.py` gives this
    host `rps=0.7, burst=1, max_concurrency=1` and a quarter-hour breaker cooldown - the rate the
    corpus crawled it at without being blocked. A slow test injects a clock into the `Fetcher`.
    """
    kind = "metacritic:page"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")

    resolved = await resolve_path(ctx, row)
    if not resolved.path:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=resolved.doc_id,
                            note=resolved.note)
    if resolved.fetched:
        return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=resolved.doc_id,
                            note=f"path={resolved.path}"
                                 + (f" ({resolved.note})" if resolved.note else ""))

    # A supplied slug has not been fetched yet; a guessed one already was, by the identity check.
    captured = await _views.capture(
        ctx, source=SOURCE, kind="page:main", url=f"{BASE}/{resolved.path}/",
        content_type=_views.HTML, min_bytes=_views.MIN_HTML_BYTES,
        request_meta={"path": resolved.path}, name=resolved.path,
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=f"no metacritic page at {resolved.path}: {captured.error}")
    return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                        note=f"path={resolved.path}")


@handler("metacritic:reviews", source=SOURCE, priority=86, phase="enrich",
         description="Scored critic excerpts + user reviews")
async def reviews(ctx: StageContext) -> SourceResult:
    """Two views, both server-rendered. The rows §8 stage 4's gate counts come out of these.

    The slug is read off the title, where `metacritic:page` wrote it nine priority points
    earlier. When there is none - the page kind failed, or an operator retried this stage alone -
    it is resolved and proved here rather than guessed, because "review pages carry no identity
    markup of their own, so an unchecked guess would be invisible from this side" (`:131-134`).
    """
    kind = "metacritic:reviews"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")

    resolved = await resolve_path(ctx, row)
    if not resolved.path:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=resolved.doc_id,
                            note=resolved.note)

    path = resolved.path.strip("/")
    captured = await _views.fetch_views(
        ctx, source=SOURCE, kind_prefix="reviews", views=[
            _views.View(name="critics", url=f"{BASE}/{path}/critic-reviews/"),
            _views.View(name="users", page=1, url=f"{BASE}/{path}/user-reviews/"),
        ],
    )
    got = [cap for cap in captured if cap.ok]
    if not got:
        return SourceResult(source=SOURCE, kind=kind, ok=False,
                            doc_id=next((c.doc_id for c in captured if c.doc_id), None),
                            note=f"no review view fetched: {_views.notes(captured)}")
    note = f"{len(got)}/{len(captured)} views"
    failed = _views.notes(captured)
    return SourceResult(source=SOURCE, kind=kind, ok=True,
                        doc_id=next((c.doc_id for c in got if c.doc_id), None),
                        note=f"{note}; {failed}" if failed else note)
