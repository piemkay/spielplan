"""Wikidata: the MC/RT/Letterboxd slugs as identifiers, so the scrapers never guess (§8 stage 2).

One title per query (decision 374). The TMDB binding is selected but never written: `tmdb:resolve`
owns that column.
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

# Named, because transposing P1712 and P1258 yields a working url for the wrong site.
P_IMDB = "P345"
P_ROTTEN_TOMATOES = "P1258"
P_METACRITIC = "P1712"
P_LETTERBOXD = "P6127"
P_TMDB = "P4947"

# Interpolated (SPARQL has no bind parameters over HTTP); `{imdb}` has passed `_ids.valid_imdb`.
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
    """`https://en.wikipedia.org/wiki/Heat_(1995_film)` -> `Heat (1995 film)`."""
    return unquote(url.rsplit("/", 1)[-1]).replace("_", " ")


@handler("wikidata:resolve", source=SOURCE, priority=25, phase="enrich",
         description="SPARQL: Q-id + RT/Metacritic/Letterboxd slugs + article title")
async def resolve(ctx: StageContext) -> SourceResult:
    """One title, one query, five identifiers.

    Priority 25 keeps it after TMDB and before `rt:page` (76) and `metacritic:page` (77).
    """
    kind = "wikidata:resolve"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
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
        # Several bindings can come back for one film; the first non-empty value of each wins.
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
