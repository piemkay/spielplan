"""Rotten Tomatoes - scores only, one of the two hosts §8 stage 2 scrapes, and the slower one.

Spec v2.1 §8 stage 2 ("rt:page", `spec:367`), §8's politeness clause (`spec:404`); decisions 334,
340, 372, 374.

Measured against the live site: the SCORES - critic %, audience % and their counts - are
server-rendered into the `<media-scorecard>` custom element and parse fine. The review text is
not: RT hydrates its review lists client-side, so a plain fetch of `/reviews` returns a shell
containing zero review prose. "A review connector was written, tested against the real pages,
found to yield nothing, and removed" (`mdc/sources/rottentomatoes.py:1-11`). The audience
percentage is the reason this connector still earns its request: OMDb supplies the critic score
and not the audience one.

PORT VERDICT: **ported with named changes** from `mdc/sources/rottentomatoes.py` (96 lines).
Taken verbatim: `BASE` (`:28`), `candidate_paths` (`:31-45`) with its year-first ordering and its
handling of the type prefix Wikidata attaches, the canonical-slug read off the FINAL url
(`:70-72`), and the `page_belongs_to_title(..., mode="rt", people_decide=False)` call with its
argument (`:80-86`).

`people_decide=False` IS THE PORT MOST WORTH READING TWICE. A Metacritic page brings review text,
so a disjoint cast is enough to refuse it; an RT page brings two percentages, "while its cast
list is the English dub for every anime series we hold - so there either signal may vouch for the
page, and only a page that fails both is dropped" (`mdc/parse/titles.py:951-956`). Ported as the
flag it is rather than as a default, because the two sources really do differ.

NAMED CHANGE 1: THE FETCH GOES THROUGH `_views.capture`, so the bytes land under `ctx.task.key`
and `response.request_url` and both validators, and a non-retryable failure lands as a row rather
than as a `continue` that leaves no trace. `:69-71`'s bare `except Exception: continue` is the
shape this replaces - it swallowed a robots refusal and a 500 identically.

NAMED CHANGE 2: A REFUSED OR MISSING PAGE IS A NOTE (`:93-94` raise `Permanent`). RT is not the
required source under decision 334.

NAMED CHANGE 3: `MIN_HTML_BYTES` IS APPLIED HERE, where the corpus applies it only through
`fetch_views` and this module does not use that helper. A 200 with a stub body is a soft block,
and recording one as good would hide the wall from §6.6's board and let the derive parse nothing.

THE SLUG IS THE POINT, NOT ONLY THIS PAGE'S SCORE. `mdc/sources/rottentomatoes.py:82-83` records
what a wrong one cost the corpus: `mdc bulk` joins a Kaggle review dataset on it, "so a wrong one
carries a thousand reviews of the wrong film with it". This app has no such join today, and the
refusal is ported anyway - §8 stage 7 verifies that a quote is a substring of the pack, which a
quote from the wrong film is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "rottentomatoes"
BASE = "https://www.rottentomatoes.com"


def candidate_paths(row: Any) -> list[str]:
    """The RT paths worth trying for this title, best first. `:31-45`, verbatim.

    A slug Wikidata supplied is an IDENTIFIER and is the only candidate; Wikidata stores these
    with the type prefix already attached, so `m/the_matrix` is passed through and `the_matrix`
    is prefixed. That single-element answer is the whole of §8's "halves guessing" on this
    source, and it is why `wikidata:resolve` runs at priority 25 and this at 76.

    A slug built from the name is a GUESS, and the year-qualified form is tried first: where RT
    has had to disambiguate two films of one name, the qualified slug is the only one of the pair
    that can be this title.
    """
    prefix = "m" if row["kind"] == "movie" else "tv"
    if row["rt_slug"]:
        slug = row["rt_slug"].strip("/")
        return [slug if slug.startswith(("m/", "tv/")) else f"{prefix}/{slug}"]
    name = row["name"] or row["original_name"]
    if not name:
        return []
    base = _ids.slugify(name).replace("-", "_")
    out = [f"{prefix}/{base}_{row['year']}"] if row["year"] else []
    out.append(f"{prefix}/{base}")
    return out


@handler("rt:page", source=SOURCE, priority=76, phase="enrich",
         description="Rotten Tomatoes scorecard (critic % + audience %)")
async def page(ctx: StageContext) -> SourceResult:
    """One request per candidate, at seven-tenths of a request a second.

    `requires=None`: there is no key. The cost is paid in time instead - `acquire/hosts.py` gives
    this host `rps=0.7, burst=1, max_concurrency=1` and a quarter-hour breaker cooldown, which
    are the numbers the corpus crawled it at without being blocked. A test that finds this slow
    injects a clock into the `Fetcher`; the policy is not the thing to change.
    """
    kind = "rt:page"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    paths = candidate_paths(row)
    if not paths:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no slug candidate")

    guessed = not row["rt_slug"]
    people = await _ids.known_people(ctx.conn, row["id"]) if guessed else set()

    wrong: list[str] = []
    errors: list[str] = []
    unproven: list[str] = []
    last_doc: int | None = None
    for path in paths:
        captured = await _views.capture(
            ctx, source=SOURCE, kind="page:main", url=f"{BASE}/{path}",
            content_type=_views.HTML, min_bytes=_views.MIN_HTML_BYTES,
            request_meta={"requested": path}, name=path,
        )
        last_doc = captured.doc_id or last_doc
        if not captured.ok:
            errors.append(f"{path}: {captured.error}")
            continue
        if captured.unchanged:
            if guessed:
                # A 304 ON A GUESS IS NOT THE IDENTITY CHECK, and here it is the SIGN of a
                # previous refusal rather than of a previous acceptance. An accepted guess writes
                # `rt_slug` below and `candidate_paths` then returns that one identifier, so a
                # GUESSED candidate that gets a conditional answer is one whose bytes this app
                # already holds and did not accept - `_views.capture` stores a refused page
                # `ok = true` with its validators ("The bytes stay in the store either way"), and
                # `fetch._validators` keys on the url alone. Returning ok here put a path the row
                # does not carry on §6.6's board under a title this app had already judged the
                # page not to be about. The remaining candidates are still worth trying, which
                # the old early return also gave up on.
                # [M5.3 review cycle 1, m53-c1-slug-01, M53-C1-NET-01]
                unproven.append(path)
                continue
            # A SUPPLIED SLUG IS DIFFERENT AND KEEPS THE OLD ANSWER. It is an identifier Wikidata
            # yielded rather than a guess, `belongs_to_title` is not run against it on the fresh
            # path either (the `guessed and` below), and the bytes this app holds under it are
            # still the page. Nothing to check, and nothing to write that is not already there.
            return SourceResult(source=SOURCE, kind=kind, ok=True,
                                note=f"path={path}: unchanged since the last fetch")

        # THE CANONICAL SLUG COMES OFF `response.url` AND NOT `request_url`, and this is the one
        # place in the package where that is right. `/m/the_matrix` 302s to `/m/matrix`, and the
        # canonical form is the only one RT's own sub-paths accept, so the LAST hop is the answer
        # to "what is this title's slug". The bytes are still filed under `request_url`, because
        # that is what the next conditional request will be keyed on (`rawstore.store`'s
        # co-keying paragraph).
        canonical = urlparse(captured.response.url).path.strip("/")
        if guessed and not _ids.belongs_to_title(
            captured.content, year=row["year"], people=people, mode="rt", people_decide=False
        ):
            # The bytes stay in the store either way - it is append-only, and a page that turned
            # out to be another film is still the honest record of what the guess returned. What
            # it does not get is the slug.
            wrong.append(canonical)
            continue
        await _ids.set_ids(ctx.conn, row["id"], rt_slug=canonical)
        note = f"path={canonical}"
        return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                            note=note if canonical == path else f"{note} (from {path})")

    refusals = [f"{', '.join(wrong)}: a different title of the same name"] if wrong else []
    if unproven:
        # Apart from `wrong` because they are different facts: one page was read and judged, the
        # other is bytes this app holds and has never accepted. Both refuse the slug.
        refusals.append(f"{', '.join(unproven)}: unchanged since a fetch that was never accepted")
    if refusals:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=last_doc,
                            note="; ".join(refusals))
    return SourceResult(
        source=SOURCE, kind=kind, ok=False, doc_id=last_doc,
        note=f"no RT page for any of {', '.join(paths)}"
             + (f": {'; '.join(errors)}" if errors else ""),
    )
