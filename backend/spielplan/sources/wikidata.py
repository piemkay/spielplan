"""Wikidata - the one request that halves the guessing. §8 stage 2 says so in those words.

Spec v2.1 §8 stage 2 ("wikidata:resolve (-> MC/RT/Letterboxd slugs - halves guessing)",
`spec:366`), §4.1 rule 6; decisions 340, 372, 374.

Wikidata stores the Rotten Tomatoes (P1258), Metacritic (P1712) and Letterboxd (P6127) ids as
properties of the film, so reading them here means the two scraped sources never have to build a
url from a name - "which roughly halves their request count and removes the main source of
mis-matched titles" (`mdc/sources/wikidata.py:5-9`). That is why this kind's `default_priority`
is below both of theirs, and why `test_sources_adapters.py` asserts the ordering rather than
trusting this paragraph: an identifier read is a request that finds the right film, and a slug
guessed from a title cannot tell two films of one name apart at all.

PORT VERDICT: **ported with named changes** from `mdc/sources/wikidata.py` (223 lines), of which
one of three handlers is taken. Taken: the `SPARQL` endpoint (`:27`), the query text (`:103-114`)
with its five OPTIONAL clauses and the `schema:about` / `schema:isPartOf` article join, the
`Accept: application/sparql-results+json` header and the `format=json` parameter (`:115-118`),
and the binding walk that turns the answer into column values (`:126-143`).

THE BATCH SHAPE IS NOT PORTED (decision 374). `_batch_key` (`:61-62`), `plan_resolve` (`:65-77`),
`plan_entities` (`:80-90`) and the "empty batch" skips are shapes of a wholesale crawl over
nineteen thousand titles, where `RESOLVE_BATCH = 120` ids to a request is what makes the endpoint
affordable at all. §8's unit is ONE TITLE walking ten stages, so the `VALUES ?imdb` clause below
holds exactly one id and there is no batch key, no chunking and no plan step. The endpoint is
paced at half a request a second by `acquire/hosts.py` with the reason written beside the number -
"the service is free, shared and expensive to query, so the override buys the access and the rate
is the manners" - and one title's query is the cheapest thing that endpoint is ever asked.

`wikidata:entity` (`:168-206`) AND `wikidata:labels` (`:209-223`) ARE NOT PORTED EITHER, for the
same reason and one more: they are batch handlers that chain a THIRD batch handler off their own
results, and §8 stage 2 names neither. The claims they fetch - awards, box office, countries of
origin, filming locations - are a layer this app does not have a table for; the milestone that
adds one ports them. `CLAIMS_OF_INTEREST` (`:34-58`) goes with them.

LETTERBOXD IS FILLED HERE AND NEVER CRAWLED (decision 374). P6127 arrives in this same answer at
no extra cost, so `title.letterboxd_slug` is written; `mdc/sources/letterboxd.py` is not ported
because §8 stage 2 names eight sources and Letterboxd is not one of them.

THE TMDB BINDING IS SELECTED AND DELIBERATELY NOT WRITTEN. P4947 is in the query because the
query is ported verbatim, and the corpus reads it into no column either (`:110`, and nothing at
`:126-143` consumes it). `tmdb:resolve` runs ten priority points earlier and answers the same
question from TMDB's own `/find`, so writing it here would be a second answer to a question one
source owns - and §4.1 rule 6 exists because these columns carry legitimate duplicates, so the
two answers would not even be comparable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "wikidata"
SPARQL = "https://query.wikidata.org/sparql"

# The properties, named rather than left as numbers in a query string, because P1712 and P1258
# are unreadable and transposing them would file Metacritic's slug in Rotten Tomatoes' column -
# a mistake that produces a working url for the wrong site and no error anywhere.
P_IMDB = "P345"
P_ROTTEN_TOMATOES = "P1258"
P_METACRITIC = "P1712"
P_LETTERBOXD = "P6127"
P_TMDB = "P4947"

# `mdc/sources/wikidata.py:103-114`, with `VALUES ?imdb` carrying one id instead of a hundred and
# twenty. Interpolated rather than parameterised because SPARQL has no bind parameters over HTTP;
# it is safe because the only thing that reaches `{imdb}` has been through `_ids.valid_imdb`,
# which is a `fullmatch` on `tt\d{5,10}` and admits no quote, brace or whitespace.
QUERY = """
SELECT ?item ?imdb ?rt ?mc ?lb ?tmdb ?article WHERE {{
  VALUES ?imdb {{ "{imdb}" }}
  ?item wdt:{p_imdb} ?imdb .
  OPTIONAL {{ ?item wdt:{p_rt} ?rt . }}
  OPTIONAL {{ ?item wdt:{p_mc} ?mc . }}
  OPTIONAL {{ ?item wdt:{p_lb} ?lb . }}
  OPTIONAL {{ ?item wdt:{p_tmdb} ?tmdb . }}
  OPTIONAL {{ ?article schema:about ?item ;
                       schema:isPartOf <https://en.wikipedia.org/> . }}
}}
"""

RESULTS_JSON = "application/sparql-results+json"


def _binding(row: dict, name: str) -> str:
    return str((row.get(name) or {}).get("value") or "")


def _article_title(url: str) -> str:
    """`https://en.wikipedia.org/wiki/Heat_(1995_film)` -> `Heat (1995 film)`.

    `mdc/sources/wikidata.py:142-143`, verbatim. The action API takes the human-readable title
    and not the url, so this is the form `wikipedia:article` will ask for.
    """
    return unquote(url.rsplit("/", 1)[-1]).replace("_", " ")


@handler("wikidata:resolve", source=SOURCE, priority=25, phase="enrich",
         description="SPARQL: Q-id + RT/Metacritic/Letterboxd slugs + article title")
async def resolve(ctx: StageContext) -> SourceResult:
    """One title, one query, five identifiers.

    `requires=None`: the SPARQL endpoint is free and unauthenticated, so there is no capability
    to gate this on and `available_kinds` never filters it out. Five of §8 stage 2's eight
    sources are in that position; the three that are not are `tmdb`, `omdb` and `trakt`.

    PRIORITY 25, WHICH IS A NAMED CHANGE FROM THE CORPUS'S 15. §8's stage 2 block lists
    `tmdb:resolve` then `tmdb:detail` then this (`spec:365-366`), and the corpus's lower number
    comes from a crawl that planned Wikidata in batches up front, before any title had a TMDB id.
    Here the order is the driver's and the spec's, and what the number must preserve is the half
    that is load-bearing - this runs before `rt:page` (76) and `metacritic:page` (77).
    """
    kind = "wikidata:resolve"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        # The whole query is `?item wdt:P345 ?imdb`, so with no IMDb id there is nothing to ask.
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no imdb id to resolve from")

    query = QUERY.format(imdb=imdb_id, p_imdb=P_IMDB, p_rt=P_ROTTEN_TOMATOES,
                         p_mc=P_METACRITIC, p_lb=P_LETTERBOXD, p_tmdb=P_TMDB)
    captured = await _views.capture(
        ctx, source=SOURCE, kind="resolve", url=SPARQL,
        params={"query": query, "format": "json"},
        headers={"Accept": RESULTS_JSON}, content_type=RESULTS_JSON,
        request_meta={"imdb_id": imdb_id},
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=captured.error)
    if captured.unchanged:
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note="unchanged since the last fetch")

    found: dict[str, str] = {}
    for binding in (captured.response.json().get("results") or {}).get("bindings") or []:
        # Several bindings can come back for one film - a Wikidata item carrying two Metacritic
        # ids produces one row per pair - so the first non-empty value of each wins, exactly as
        # `mdc/sources/wikidata.py:130-143` accumulates into one `rec` per IMDb id.
        qid = _binding(binding, "item").rsplit("/", 1)[-1]
        if qid.startswith("Q"):
            found.setdefault("wikidata_id", qid)
        for name, column in (("rt", "rt_slug"), ("mc", "metacritic_slug"),
                             ("lb", "letterboxd_slug")):
            value = _binding(binding, name)
            if value:
                found.setdefault(column, value)
        article = _binding(binding, "article")
        if article:
            found.setdefault("wikipedia_title", _article_title(article))

    if not found:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=f"wikidata holds no item with {P_IMDB} = {imdb_id}")
    filled = await _ids.set_ids(ctx.conn, row["id"], **found)
    return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                        note=f"offered {', '.join(filled)}")
