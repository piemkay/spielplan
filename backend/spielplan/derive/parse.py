"""§8 stage 3's parsers: raw bytes in, derived rows out, and nothing in between.

Spec v2.1 §8 stage 3 ("per-title parse of raw docs into title_meta/credit/review/..., keyed
`entity_key`"), §8's preamble (`spec:398`, "All fetched bytes land in the app's own raw store, so
re-parsing is free forever"), §4.1's content spine and §3.1's seven roles; decisions 372, 374,
375.

PORT VERDICT: **ported with two named changes**, both structural, from
`mdc/parse/titles.py` (1,032 lines). All fourteen entry points are here --
`parse_tmdb_detail`, `parse_omdb_awards`, `parse_omdb`, `parse_jellyfin_item`,
`parse_trakt_summary`, `parse_trakt_ratings`, `parse_tvmaze`, `parse_wikipedia`, `parse_mpst`,
`parse_wikidata_entity`, `parse_letterboxd_page`, `parse_rt_page`, `page_belongs_to_title` and
`parse_metacritic_page` -- with their field selections, their regexes and the arguments their
comments make. The corpus's header is still the rule that governs all of them and is quoted
rather than paraphrased: "Every source writes into the same tables tagged with its own `source`,
and nothing is merged or reconciled here. Keeping TMDB's genres and OMDb's genres side by side
rather than picking a winner is deliberate: disagreement between sources is information."
`importer/meta.SOURCE_PRIORITY` is the app's half of that bargain -- the reader picks, per field,
and the writer keeps every answer.

THE TWO NAMED CHANGES.

  1. **sqlite3 -> Postgres, which here means NO CONNECTION AT ALL.** Every corpus parser takes a
     `sqlite3.Connection` for one reason: to call `upsert_person` inline. A parser that holds a
     connection is a parser that cannot be re-run over the raw store without one, and it is one
     import away from holding a fetcher too. So a credit row carries the PERSON AS DATA -- name,
     imdb id, tmdb id, profile path -- and `derive.ids.upsert_person` is called by the derive,
     once, where the transaction is. `classify_role` and `keep_credit` stay here, because which
     credits survive is a property of the document and not of the database.
  2. **The sink callback becomes a return value.** `emit(table, tuple)` becomes
     `ParsedTitle.rows[table]`, and a row is a MAPPING rather than a positional tuple. The
     corpus can afford positions because one hand-written INSERT per table sits next to the
     emit; here the rows cross a module boundary, and a column added to `title_meta` in the
     middle would silently shift nineteen values one place to the left with every type still
     matching. The KEYS are the corpus's own column names (`tests/fixtures/real_bundle_shapes.json`
     records them), because `title_meta.payload` is literally that dict on the app side --
     `importer/meta._meta_rows` builds it from the shipped columns -- and a derived row that
     spelled `plot` where an imported one spells `plot_full` would be invisible to `meta.best`
     and so to every card.

WHAT THIS MODULE MAY IMPORT, and why the list is this short: `spielplan.sources._htmlutil`,
`spielplan.derive.ids` and the standard library. That is exactly `mdc/parse/titles.py`'s own
import list, and it is the whole of "re-parsing is free forever". A parser that can reach the
network is a parser whose next bug costs another crawl of somebody else's host, on a household's
IP address, against eight rate limits. `test_derive_parse.py` reads every module under
`spielplan/derive/` with `ast` and fails on an import of `httpx`, `requests`, `urllib.request` or
`spielplan.acquire.fetch`.

TWO PARSERS ARE HERE FOR SOURCES NOTHING FETCHES, and that is deliberate rather than an
oversight. Decision 374 does not port `mdc/sources/letterboxd.py` (§8 stage 2 names eight sources
and Letterboxd is not one of them) and ports only `wikidata:resolve` of the three Wikidata
handlers, so no adapter in this app produces a `letterboxd` page or a `wikidata` entity.
`parse_letterboxd_page` and `parse_wikidata_entity` are still owed, for the same reason
`parse_mpst` and `parse_jellyfin_item` are: the bundle's `title_meta` already carries rows under
those source names and `importer/meta.SOURCE_PRIORITY` orders all eleven of them, so a re-parse
path that cannot read a source the install already holds is a hole in §8 stage 3 rather than a
saving. NOBODY SHOULD ADD A CRAWLER TO MATCH: the parser existing is not a decision that the
source should be fetched, and decision 374 is the one that says it should not.

ORDERING IS WRITTEN INTO THE ROWS, not left to the reader. Decision 375 makes "no changed
ordering" a property of the data, so every list this module walks keeps the source payload's own
order in `position` / `billing_order`, and `credit` rows come out in the order the document
billed them. A derive that re-inserts in a different order and relies on `bigserial` to sort
would be idempotent in content and not in appearance, which is the half of §14 risk 5 that only
shows up on a card.

A PARSER MAY EMIT THE SAME ROW TWICE AND THE DERIVE MUST EXPECT IT. TMDB names a film's primary
language once in `spoken_languages` and again in `original_language`, and names a country once in
`production_countries` and again in `origin_country`, so `parse_tmdb_detail` emits two identical
`title_language` rows and two identical `title_country` rows for Arrival. That is the corpus's
behaviour and the corpus writes through `INSERT OR IGNORE` for all seven multi-row tables
(`mdc/parse/rebuild.py:40-53`) and `INSERT OR REPLACE` for `title_meta` (`:30`). The app's keys
are the same facts -- `(title_id, source, language, role)` and `(title_id, source, country)`,
0015_seed.sql sections 9 -- so the derive's INSERT needs `ON CONFLICT DO NOTHING` or the first
acquisition to reach it dies on a unique violation. Deduplicating here instead would be the wrong
end: two sources agreeing is information §4.1 keeps, and the key is where the app already says so.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from spielplan.derive.ids import CAST_BILLING_LIMIT, classify_role, keep_credit, loose_name
from spielplan.sources._htmlutil import clean_text, ld_json, next_data, unescape, walk

# The eleven derived tables §8 stage 3 writes, and the whole vocabulary of `ParsedTitle.rows`.
# Named so a typo in an emit is a KeyError here rather than a table the derive silently never
# writes -- the corpus's `emit` takes the table name as a string and has the same exposure.
TABLES: tuple[str, ...] = (
    "title_meta", "title_genre", "title_keyword", "title_language", "title_country",
    "title_company", "title_alias", "title_video", "credit", "award", "platform_rating",
)

# TWO CORPUS COLUMNS ARE DELIBERATELY NOT EMITTED, for one reason: this app's tables do not have
# them, so a row carrying them would read as data the install keeps and does not.
#
#   * the keyword table's corpus-side weight column. The corpus ships it
#     (`tests/fixtures/real_bundle_shapes.json`) and `importer/load.py`'s `MAPPINGS` drops it,
#     because `0003_content.sql:96-102` is `(title_id, keyword, source)`. All three parsers here
#     would emit the constant 1.0 anyway - the corpus's real keyword weights come from the
#     MovieLens genome, which is a different loader and `ml_genome_score`'s own table. Putting it
#     back would also be read as a rule-2 filter by `test_landmine_guards.py`'s SQL scan, which
#     sees a keyword argument of that name beside a Python `or` as a predicate; that is a false
#     positive and NOT why the field is absent, but it is a second reason to leave it out.
#   * `person.gender`, argued where the person is written (`derive/ids.py`, change note 3).
#
# Both are still in the raw store, so the migration that adds either column is followed by a
# re-derive and not by a re-crawl (`spec:398`).


@dataclass(frozen=True)
class ParsedTitle:
    """What one raw document says, as rows, before anything has touched the database.

    `source` is the DOCUMENT's source and is what decision 375's replace-by-`(title_id, source)`
    scope is keyed on. It is NOT always the `source` on the rows: `parse_omdb` files its
    `platform_rating` rows under `imdb`, `rottentomatoes` and `metacritic`, because OMDb serves
    other people's scores and the corpus records whose they are. The derive has to scope its
    delete by the set of row sources it is about to re-insert rather than by this field, or an
    OMDb re-parse leaves last run's IMDb rating behind beside the new one.
    """

    source: str
    rows: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)

    def table(self, name: str) -> tuple[Mapping[str, Any], ...]:
        """The rows for one target table, empty when the document had none."""
        return self.rows.get(name, ())

    @property
    def meta(self) -> Mapping[str, Any] | None:
        """The `title_meta` payload this document produced, or None when it produced none.

        One row per document by construction: `title_meta`'s primary key is `(title_id, source)`
        (`0003_content.sql:72-79`) and decision 375 keeps it that way, so a parser emitting two
        would be a unique violation the derive could not resolve.
        """
        rows = self.table("title_meta")
        return rows[0] if rows else None

    @property
    def row_sources(self) -> frozenset[str]:
        """Every `source` value appearing on a row, which is the derive's real delete scope."""
        return frozenset(
            str(row["source"]) for table in self.rows.values() for row in table if "source" in row
        )


class _Rows:
    """The corpus's `emit` sink, one indirection shorter.

    `mdc/parse/titles.py` is written against `Sink = Callable[[str, tuple], None]` so the same
    parsers can feed a rebuild, a projection and an ingest. This app has one consumer, so the
    sink is a collector and the parsers read line for line against the corpus's.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._rows: dict[str, list[Mapping[str, Any]]] = {}

    def emit(self, table: str, **row: Any) -> None:
        if table not in TABLES:
            raise KeyError(f"{table} is not one of the tables stage 3 writes: {', '.join(TABLES)}")
        row.setdefault("source", self._source)
        self._rows.setdefault(table, []).append(row)

    def done(self) -> ParsedTitle:
        return ParsedTitle(
            source=self._source,
            rows={table: tuple(rows) for table, rows in self._rows.items()},
        )


def _payload(data: Any) -> Mapping[str, Any] | None:
    """`data` if it is a non-empty mapping, else None.

    Every JSON parser below starts here. A source that answers 200 with a list, a string or null
    -- which is what a rate limiter, a CDN error page and a changed API all look like once
    `json.loads` has succeeded -- must produce no rows rather than an AttributeError that the
    driver would record as a failed stage. Decision 334 makes a source that did not answer a note
    on the job; a source that answered nonsense is the same fact.

    AN EMPTY MAPPING IS NOTHING TO READ AND NOT A TITLE WITH EVERY FIELD NULL. The corpus emits
    its `title_meta` row unconditionally because it is only ever handed a payload it just
    fetched; here the same document is re-parsed for ever, so an all-NULL row would put a source
    into `title_meta` for the title, `meta.best` would walk it on every field, and §6.0's card
    would show a source that said nothing. `ParsedTitle.rows == {}` is the honest answer.
    """
    return data if isinstance(data, Mapping) and data else None


# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------


def _year(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _tmdb_img(path: str | None, size: str = "w500") -> str | None:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


def _tmdb_certification(data: Mapping[str, Any], *, is_movie: bool) -> str | None:
    """The one age rating kept, from the three markets this household's catalog is rated in.

    Lifted out of `parse_tmdb_detail`'s body, which is where the corpus keeps it
    (`mdc/parse/titles.py:44-58`); the two nested loops and the `break` that leaves both are the
    only part of that function a reader has to trace twice.
    """
    if is_movie:
        for entry in (data.get("release_dates") or {}).get("results") or []:
            if entry.get("iso_3166_1") in ("US", "GB", "DE"):
                for release in entry.get("release_dates") or []:
                    if release.get("certification"):
                        return release["certification"]
        return None
    for entry in (data.get("content_ratings") or {}).get("results") or []:
        if entry.get("iso_3166_1") in ("US", "GB", "DE") and entry.get("rating"):
            return entry["rating"]
    return None


def parse_tmdb_detail(data: Any) -> ParsedTitle:
    """TMDB's `movie/{id}` or `tv/{id}` with its append_to_response blocks.

    The richest single document in the crawl and the one `SOURCE_PRIORITY` puts first, so the
    field selection here decides most title cards. `is_movie` is inferred from the payload rather
    than passed in because the same function serves both endpoints and TMDB's own shapes are the
    honest discriminator; §8 stage 1 has already decided `title.kind` and decision 372 forbids an
    enrichment flipping it.
    """
    data = _payload(data)
    if data is None:
        return ParsedTitle(source="tmdb")
    rows = _Rows("tmdb")
    is_movie = "title" in data or "release_date" in data

    runtime = data.get("runtime")
    if runtime is None:
        episodes = data.get("episode_run_time") or []
        runtime = int(sum(episodes) / len(episodes)) if episodes else None

    collection = data.get("belongs_to_collection") or {}
    rows.emit(
        "title_meta",
        year=_year(data.get("release_date") or data.get("first_air_date")),
        runtime_min=runtime,
        tagline=data.get("tagline"),
        plot_short=None,
        plot_full=clean_text(data.get("overview")) or None,
        status=data.get("status"),
        original_language=data.get("original_language"),
        budget=data.get("budget"),
        revenue=data.get("revenue"),
        poster_url=_tmdb_img(data.get("poster_path")),
        backdrop_url=_tmdb_img(data.get("backdrop_path"), "w1280"),
        homepage=data.get("homepage"),
        content_rating=_tmdb_certification(data, is_movie=is_movie),
        episode_count=data.get("number_of_episodes"),
        season_count=data.get("number_of_seasons"),
        first_air_date=data.get("first_air_date"),
        last_air_date=data.get("last_air_date"),
        in_production=1 if data.get("in_production") else 0,
        extra=json.dumps({
            "adult": data.get("adult"),
            "collection": collection.get("name") or None,
            "collection_id": collection.get("id") or None,
            "type": data.get("type"),
            "created_by": [c.get("name") for c in (data.get("created_by") or [])],
            "seasons": [
                {"n": s.get("season_number"), "eps": s.get("episode_count"),
                 "air": s.get("air_date"), "name": s.get("name"), "overview": s.get("overview")}
                for s in (data.get("seasons") or [])
            ],
            "recommendations": [
                r.get("id") for r in (data.get("recommendations") or {}).get("results") or []
            ],
            "similar": [r.get("id") for r in (data.get("similar") or {}).get("results") or []],
        }, ensure_ascii=False),
    )

    for position, genre in enumerate(data.get("genres") or []):
        if genre.get("name"):
            rows.emit("title_genre", genre=genre["name"], position=position)

    for country in data.get("production_countries") or []:
        if country.get("iso_3166_1"):
            rows.emit("title_country", country=country["iso_3166_1"])
    for country in data.get("origin_country") or []:
        rows.emit("title_country", country=country)

    primary = data.get("original_language")
    for language in data.get("spoken_languages") or []:
        code = language.get("iso_639_1")
        if code:
            rows.emit("title_language", language=code, is_primary=1 if code == primary else 0)
    if primary:
        rows.emit("title_language", language=primary, is_primary=1)

    keywords = data.get("keywords") or {}
    for keyword in keywords.get("keywords") or keywords.get("results") or []:
        if keyword.get("name"):
            rows.emit("title_keyword", keyword=keyword["name"])

    for company in data.get("production_companies") or []:
        if company.get("name"):
            rows.emit("title_company", company=company["name"], role="production",
                      country=company.get("origin_country"))
    for network in data.get("networks") or []:
        if network.get("name"):
            rows.emit("title_company", company=network["name"], role="network",
                      country=network.get("origin_country"))

    alternatives = data.get("alternative_titles") or {}
    for alias in alternatives.get("titles") or alternatives.get("results") or []:
        if alias.get("title"):
            rows.emit("title_alias", alias=alias["title"],
                      region=alias.get("iso_3166_1") or "", language=None)

    for video in (data.get("videos") or {}).get("results") or []:
        if video.get("key"):
            rows.emit("title_video", key=video["key"], site=video.get("site"),
                      type=video.get("type"), name=video.get("name"))

    if data.get("vote_average") is not None:
        rows.emit("platform_rating", metric="user_score", value=data["vote_average"],
                  scale=10.0, votes=data.get("vote_count"))
    if data.get("popularity") is not None:
        rows.emit("platform_rating", metric="popularity", value=data["popularity"],
                  scale=None, votes=None)

    _tmdb_credits(rows, data)
    return rows.done()


def _tmdb_credits(rows: _Rows, data: Mapping[str, Any]) -> None:
    """`credits` for a film, `aggregate_credits` for a series, which differ in one shape.

    A series' cast entry carries `roles: [{character, episode_count}]` instead of a flat
    `character`, and its crew entry carries `jobs: [...]` instead of a flat `job` -- one person,
    several jobs, each with its own episode count. Flattening a series crew entry to its first
    job would lose the others; keeping the entry whole would file a director under whichever job
    TMDB happened to list first.
    """
    credits = data.get("credits") or data.get("aggregate_credits") or {}

    for index, member in enumerate(credits.get("cast") or []):
        order = member.get("order", index)
        role = classify_role(None, None, is_cast=True)
        if not keep_credit(role, order):
            continue
        character = member.get("character")
        episodes = None
        if not character and member.get("roles"):
            first = member["roles"][0] or {}
            character = first.get("character")
            episodes = first.get("episode_count")
        rows.emit(
            "credit",
            person={"name": member.get("name") or "?", "tmdb_id": member.get("id"),
                    "profile_path": _tmdb_img(member.get("profile_path"), "w185")},
            department="Acting", job="Actor", character=character, billing_order=order,
            episode_count=episodes or member.get("total_episode_count"), role_class=role,
        )

    for member in credits.get("crew") or []:
        if member.get("jobs"):
            jobs = [(j.get("job"), j.get("episode_count")) for j in member["jobs"]]
        else:
            jobs = [(member.get("job"), member.get("episode_count"))]
        for job, episodes in jobs:
            role = classify_role(member.get("department"), job)
            if not keep_credit(role, None):
                continue
            rows.emit(
                "credit",
                person={"name": member.get("name") or "?", "tmdb_id": member.get("id"),
                        "profile_path": _tmdb_img(member.get("profile_path"), "w185")},
                department=member.get("department"), job=job, character=None,
                billing_order=None, episode_count=episodes, role_class=role,
            )


# ---------------------------------------------------------------------------
# OMDb
# ---------------------------------------------------------------------------

_OMDB_NA = {"N/A", "", None}


def _omdb_val(value: Any) -> Any:
    """OMDb's value, with its "N/A" sentinel mapped to NULL and entities decoded.

    OMDb serves JSON but its strings come straight out of IMDb's HTML, so `C&ocirc;te
    d&amp;#x27;Ivoire` arrives escaped - twice - inside a field nothing else would think to
    decode.
    """
    if value in _OMDB_NA:
        return None
    return unescape(value) if isinstance(value, str) else value


# IMDb - and so OMDb - writes a qualified language name inverted and comma separated: "Norse,
# Old" is Old Norse, "Greek, Ancient (to 1453)" is Ancient Greek. Splitting the field on commas
# turns each of those into two languages, one of which ("Old") is not a language at all.
_LANG_QUALIFIERS = {"old", "ancient", "middle", "modern", "classical"}


def _omdb_languages(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        # OMDb's literal for a silent film. It is a statement about the absence of language, not
        # a language, and storing it as one makes 'None' the 40th most common language in the
        # corpus.
        if part.lower() == "none":
            continue
        head = re.sub(r"\s*\([^)]*\)", "", part).strip()
        if head.lower() in _LANG_QUALIFIERS and out:
            out[-1] = f"{head} {out[-1]}"
            continue
        out.append(part)
    return out


# "Won 7 Oscars. 21 wins & 43 nominations total" - and, when OMDb's own templating slips,
# "Nominated for 1 BAFTA Award1 nomination total". The headline award name is therefore read up
# to whatever comes first: a full stop, the start of the tally, or the end of the string.
_AWARD_HEADLINE = re.compile(
    r"\b(Won|Nominated for)\s+(\d+)\s+(.+?)\s*(?=\d+\s+(?:win|nomination)|\.|$)", re.I)
_AWARD_TALLY = re.compile(r"(\d+)\s+(win|nomination)s?\b", re.I)


def parse_omdb_awards(blurb: str | None) -> list[tuple[str, str, str, int]]:
    """Turn OMDb's awards sentence into rows of (award, category, result, n).

    OMDb gives one free-text summary per title and no structured award data at all, so this is
    the whole of what can be recovered: which headline award the film won or was nominated for,
    and its total wins and nominations. Per-category and per-year detail is genuinely not in the
    payload - that comes from Wikidata's P166/P1411 statements, which land in the same table
    under source='wikidata'.
    """
    text = (blurb or "").strip()
    if not text:
        return []
    out: list[tuple[str, str, str, int]] = []
    rest = text
    headline = _AWARD_HEADLINE.search(text)
    if headline:
        name = re.sub(r"\s+", " ", headline.group(3)).strip(" .,")
        if name.lower().endswith("s") and not name.lower().endswith("ss"):
            name = name[:-1]
        if name:
            out.append((name, "ceremony",
                        "won" if headline.group(1).lower() == "won" else "nominated",
                        int(headline.group(2))))
        rest = text[headline.end():]
    for tally in _AWARD_TALLY.finditer(rest):
        out.append(("any award", "total",
                    "won" if tally.group(2).lower() == "win" else "nominated",
                    int(tally.group(1))))
    return out


def _money(value: Any) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


# OMDb's `Ratings` block, and the source each score is really about. Lifted out of the corpus's
# if/elif chain (`mdc/parse/titles.py:335-350`) into data because the interesting fact is the
# third column: these rows are NOT filed under `omdb`. OMDb is the messenger, and a
# `platform_rating` row saying `omdb` held IMDb's 8.1 would make OMDb look like a rating
# platform the household could weigh. `ParsedTitle.row_sources` exists for this.
_OMDB_RATINGS: tuple[tuple[str, str, str, float, str], ...] = (
    ("Internet Movie Database", r"([\d.]+)/10", "imdb", 10.0, "user_score"),
    ("Rotten Tomatoes", r"(\d+)%", "rottentomatoes", 100.0, "critic_score"),
    ("Metacritic", r"(\d+)/100", "metacritic", 100.0, "critic_score"),
)


def parse_omdb(data: Any) -> ParsedTitle:
    """OMDb's `?i=tt...` detail blob: IMDb's fields, flattened into strings.

    Everything here is prose that was a field once, so almost every value needs a regex to become
    one again -- `Runtime` is "116 min", `Year` is "2016" or "2016-2019", `BoxOffice` is
    "$100,546,139". Nothing else in the crawl needs this much repair, which is why `_omdb_val`,
    `_omdb_languages` and `_money` exist and are used nowhere else.
    """
    data = _payload(data)
    if data is None:
        return ParsedTitle(source="omdb")
    rows = _Rows("omdb")

    runtime = _omdb_val(data.get("Runtime"))
    if runtime:
        minutes = re.search(r"(\d+)", str(runtime))
        runtime = int(minutes.group(1)) if minutes else None

    year_match = re.search(r"\d{4}", str(data.get("Year") or ""))
    seasons = str(data.get("totalSeasons", ""))
    rows.emit(
        "title_meta",
        year=int(year_match.group(0)) if year_match else None,
        runtime_min=runtime,
        tagline=None,
        plot_short=None,
        plot_full=clean_text(_omdb_val(data.get("Plot"))) or None,
        status=None,
        original_language=None,
        budget=None,
        revenue=_money(_omdb_val(data.get("BoxOffice"))),
        poster_url=_omdb_val(data.get("Poster")),
        backdrop_url=None,
        homepage=_omdb_val(data.get("Website")),
        content_rating=_omdb_val(data.get("Rated")),
        episode_count=None,
        season_count=int(seasons) if seasons.isdigit() else None,
        first_air_date=None,
        last_air_date=None,
        in_production=None,
        extra=json.dumps({"awards": _omdb_val(data.get("Awards")),
                          "dvd": _omdb_val(data.get("DVD")),
                          "production": _omdb_val(data.get("Production")),
                          "type": data.get("Type")}, ensure_ascii=False),
    )

    for position, genre in enumerate((_omdb_val(data.get("Genre")) or "").split(",")):
        genre = genre.strip()
        if genre:
            rows.emit("title_genre", genre=genre, position=position)
    for country in (_omdb_val(data.get("Country")) or "").split(","):
        country = country.strip()
        if country:
            rows.emit("title_country", country=country)
    for index, language in enumerate(_omdb_languages(_omdb_val(data.get("Language")))):
        rows.emit("title_language", language=language, is_primary=1 if index == 0 else 0)

    for field_name, department, job, role in (("Director", "Directing", "Director", "director"),
                                              ("Writer", "Writing", "Writer", "writer")):
        for name in (_omdb_val(data.get(field_name)) or "").split(","):
            # OMDb parenthesises the contribution -- "Eric Heisserer (screenplay)" -- and that
            # belongs in `job`, which this source does not carry at that resolution, rather than
            # in a person's name.
            name = re.sub(r"\s*\([^)]*\)", "", name).strip()
            if not name:
                continue
            rows.emit("credit", person={"name": name}, department=department, job=job,
                      character=None, billing_order=None, episode_count=None, role_class=role)
    for index, name in enumerate((_omdb_val(data.get("Actors")) or "").split(",")):
        name = name.strip()
        if not name or index >= CAST_BILLING_LIMIT:
            continue
        rows.emit("credit", person={"name": name}, department="Acting", job="Actor",
                  character=None, billing_order=index, episode_count=None, role_class="cast")

    for rating in data.get("Ratings") or []:
        name, value = rating.get("Source"), rating.get("Value") or ""
        for label, pattern, source, scale, metric in _OMDB_RATINGS:
            if name != label:
                continue
            found = re.match(pattern, value)
            if found:
                votes = _money(_omdb_val(data.get("imdbVotes"))) if source == "imdb" else None
                rows.emit("platform_rating", source=source, metric=metric,
                          value=float(found.group(1)), scale=scale, votes=votes)

    # The free-text blurb itself is kept in title_meta.extra above, so nothing is lost by storing
    # only the structured reading of it here.
    for name, category, result, count in parse_omdb_awards(_omdb_val(data.get("Awards"))):
        rows.emit("award", award=name, category=category, year=None, result=result,
                  person=None, count=count)

    return rows.done()


# ---------------------------------------------------------------------------
# Jellyfin  (owned-library presentation signals)
# ---------------------------------------------------------------------------


def parse_jellyfin_item(item: Any) -> ParsedTitle:
    """The household's own copy: codecs, channels, subtitle languages, the file.

    Not one of §8 stage 2's eight sources and not fetched by stage 3 either -- the item comes
    from §7's sweep, which is M5.2's -- but the bundle ships `jellyfin` rows in `title_meta` and
    `SOURCE_PRIORITY` orders them sixth, so the re-parse path owes it a reader. The presentation
    block is the whole reason this source exists: it is the only one that knows whether tonight's
    copy is the 4K HDR one or a 720p rip.
    """
    item = _payload(item)
    if item is None:
        return ParsedTitle(source="jellyfin")
    rows = _Rows("jellyfin")
    ticks = item.get("RunTimeTicks")
    runtime = int(ticks / 600_000_000) if ticks else None

    streams = item.get("MediaStreams") or []
    video = next((s for s in streams if s.get("Type") == "Video"), {})
    audios = [s for s in streams if s.get("Type") == "Audio"]
    subtitles = [s for s in streams if s.get("Type") == "Subtitle"]
    best_audio = max(audios, key=lambda s: s.get("Channels") or 0, default={})
    media = (item.get("MediaSources") or [{}])[0]

    presentation = {
        "width": video.get("Width"), "height": video.get("Height"),
        "video_codec": video.get("Codec"), "video_range": video.get("VideoRange"),
        "video_range_type": video.get("VideoRangeType"),
        "video_dovi": video.get("VideoDoViTitle"),
        "bit_depth": video.get("BitDepth"),
        "audio_codec": best_audio.get("Codec"),
        "audio_channels": best_audio.get("Channels"),
        "audio_profile": best_audio.get("Profile"),
        "audio_layout": best_audio.get("ChannelLayout"),
        "audio_tracks": [{"codec": a.get("Codec"), "ch": a.get("Channels"),
                          "lang": a.get("Language"), "title": a.get("Title")} for a in audios],
        "subtitle_languages": sorted({s["Language"] for s in subtitles if s.get("Language")}),
        "container": media.get("Container"),
        "bitrate": media.get("Bitrate"),
        "size_bytes": media.get("Size"),
        "path": item.get("Path"),
        "user_data": item.get("UserData"),
    }

    rows.emit(
        "title_meta",
        year=item.get("ProductionYear"), runtime_min=runtime,
        tagline=(item.get("Taglines") or [None])[0], plot_short=None,
        plot_full=clean_text(item.get("Overview")) or None, status=item.get("Status"),
        original_language=None, budget=None, revenue=None, poster_url=None, backdrop_url=None,
        homepage=None, content_rating=item.get("OfficialRating"),
        episode_count=item.get("RecursiveItemCount"), season_count=item.get("ChildCount"),
        first_air_date=item.get("PremiereDate"), last_air_date=item.get("EndDate"),
        in_production=None, extra=json.dumps(presentation, ensure_ascii=False),
    )

    for position, genre in enumerate(item.get("Genres") or []):
        rows.emit("title_genre", genre=genre, position=position)
    for country in item.get("ProductionLocations") or []:
        rows.emit("title_country", country=country)
    for studio in item.get("Studios") or []:
        if studio.get("Name"):
            rows.emit("title_company", company=studio["Name"], role="production", country=None)
    if item.get("CommunityRating") is not None:
        rows.emit("platform_rating", metric="community", value=item["CommunityRating"],
                  scale=10.0, votes=None)
    if item.get("CriticRating") is not None:
        rows.emit("platform_rating", metric="critic_score", value=item["CriticRating"],
                  scale=100.0, votes=None)

    for index, person in enumerate(item.get("People") or []):
        kind = (person.get("Type") or "").lower()
        if kind == "actor":
            role, order = "cast", index
        else:
            role, order = classify_role(None, person.get("Type")), None
        if not keep_credit(role, order):
            continue
        providers = {k.lower(): v for k, v in (person.get("ProviderIds") or {}).items()}
        # Jellyfin's `Role` is the character for an actor and the JOB for crew, so passing it
        # through unconditionally filed "Director" and "Screenplay" as character names on 2,029
        # credits.
        rows.emit(
            "credit",
            person={"name": person.get("Name") or "?", "imdb_id": providers.get("imdb")},
            department=person.get("Type"), job=person.get("Type"),
            character=person.get("Role") if kind == "actor" else None,
            billing_order=order, episode_count=None, role_class=role,
        )

    return rows.done()


# ---------------------------------------------------------------------------
# Trakt / TVmaze
# ---------------------------------------------------------------------------


def parse_trakt_summary(data: Any) -> ParsedTitle:
    """Trakt's `?extended=full` summary. Thin, and third in `SOURCE_PRIORITY` for that reason."""
    data = _payload(data)
    if data is None:
        return ParsedTitle(source="trakt")
    rows = _Rows("trakt")
    rows.emit(
        "title_meta",
        year=data.get("year"), runtime_min=data.get("runtime"), tagline=data.get("tagline"),
        plot_short=None, plot_full=clean_text(data.get("overview")) or None,
        status=data.get("status"), original_language=data.get("language"), budget=None,
        revenue=None, poster_url=None, backdrop_url=None, homepage=data.get("homepage"),
        content_rating=data.get("certification"), episode_count=data.get("aired_episodes"),
        season_count=None, first_air_date=data.get("first_aired"), last_air_date=None,
        in_production=None,
        extra=json.dumps({"country": data.get("country"), "network": data.get("network"),
                          "trailer": data.get("trailer"),
                          "available_translations": data.get("available_translations")},
                         ensure_ascii=False),
    )
    for position, genre in enumerate(data.get("genres") or []):
        rows.emit("title_genre", genre=genre, position=position)
    if data.get("country"):
        rows.emit("title_country", country=str(data["country"]).upper())
    if data.get("language"):
        rows.emit("title_language", language=data["language"], is_primary=1)
    if data.get("network"):
        rows.emit("title_company", company=data["network"], role="network", country=None)
    if data.get("rating") is not None:
        rows.emit("platform_rating", metric="user_score", value=data["rating"], scale=10.0,
                  votes=data.get("votes"))
    return rows.done()


def parse_trakt_ratings(data: Any) -> ParsedTitle:
    """Trakt's rating histogram, one `platform_rating` row per star.

    Kept as ten rows rather than one blob because the display schema's `platform_rating` is keyed
    `(title_id, platform, metric)` (`0015_seed.sql:207-210`) and a distribution collapsed into
    `extra` would be unreadable by the only surface that renders this table.
    """
    rows = _Rows("trakt")
    for star, count in ((_payload(data) or {}).get("distribution") or {}).items():
        rows.emit("platform_rating", metric=f"dist_{star}", value=float(count), scale=None,
                  votes=None)
    return rows.done()


def parse_tvmaze(data: Any) -> ParsedTitle:
    """TVmaze's `shows/{id}?embed[]=cast&embed[]=crew&embed[]=seasons`, series only."""
    data = _payload(data)
    if data is None:
        return ParsedTitle(source="tvmaze")
    rows = _Rows("tvmaze")
    embedded = data.get("_embedded") or {}
    rows.emit(
        "title_meta",
        year=_year(data.get("premiered")),
        runtime_min=data.get("averageRuntime") or data.get("runtime"),
        tagline=None, plot_short=None, plot_full=clean_text(data.get("summary")) or None,
        status=data.get("status"),
        original_language=(data.get("language") or "").lower()[:2] or None,
        budget=None, revenue=None, poster_url=(data.get("image") or {}).get("original"),
        backdrop_url=None, homepage=data.get("officialSite"), content_rating=None,
        episode_count=None, season_count=len(embedded.get("seasons") or []) or None,
        first_air_date=data.get("premiered"), last_air_date=data.get("ended"),
        in_production=1 if data.get("status") == "Running" else 0,
        extra=json.dumps({"type": data.get("type"), "schedule": data.get("schedule"),
                          "webChannel": (data.get("webChannel") or {}).get("name"),
                          "network": (data.get("network") or {}).get("name")},
                         ensure_ascii=False),
    )
    for position, genre in enumerate(data.get("genres") or []):
        rows.emit("title_genre", genre=genre, position=position)
    network = data.get("network") or data.get("webChannel") or {}
    if network.get("name"):
        code = (network.get("country") or {}).get("code")
        rows.emit("title_company", company=network["name"], role="network", country=code)
        if code:
            rows.emit("title_country", country=code)
    if (data.get("rating") or {}).get("average") is not None:
        rows.emit("platform_rating", metric="user_score", value=data["rating"]["average"],
                  scale=10.0, votes=None)
    for index, member in enumerate(embedded.get("cast") or []):
        if index >= CAST_BILLING_LIMIT:
            break
        person = member.get("person") or {}
        rows.emit("credit", person={"name": person.get("name") or "?"}, department="Acting",
                  job="Actor", character=(member.get("character") or {}).get("name"),
                  billing_order=index, episode_count=None, role_class="cast")
    for member in embedded.get("crew") or []:
        role = classify_role(None, member.get("type"))
        if not keep_credit(role, None):
            continue
        person = member.get("person") or {}
        rows.emit("credit", person={"name": person.get("name") or "?"},
                  department=member.get("type"), job=member.get("type"), character=None,
                  billing_order=None, episode_count=None, role_class=role)
    return rows.done()


# ---------------------------------------------------------------------------
# Wikipedia / Wikidata
# ---------------------------------------------------------------------------

# `mdc/sources/wikipedia.py:22` and `:164-183`, moved here rather than into `spielplan/sources/`.
# It is a pure function over the plaintext extract -- no request shape, no URL, no key -- and the
# only two callers are parsers, one in this module and one in `derive/reviews.py`. Filed with the
# source in the corpus because that is where the `action=query&prop=extracts` request lives; here
# the fetcher for it is an adapter the `sources` package owns and this is the reading of what it
# stored. One definition, because `parse_wikipedia` and `parse_wikipedia_reception` MUST agree
# about where a section starts: they divide one article between them and a disagreement stores
# the reception prose twice, once as craft material and once as a review.
SECTION_RE = re.compile(r"^==+\s*(.+?)\s*==+\s*$", re.M)


def split_sections(text: str) -> dict[str, str]:
    """Split a plaintext Wikipedia extract into `{section_name: body}`."""
    if not text:
        return {}
    out: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return {"_lead": text.strip()}
    lead = text[: matches[0].start()].strip()
    if lead:
        out["_lead"] = lead
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if body:
            key = name.lower()
            out[key] = (out.get(key, "") + "\n" + body).strip()
    return out


# Sections worth keeping in full. These are where Wikipedia describes HOW A FILM WAS MADE - which
# is the only free, hallucination-resistant source of `craft` material (§3.3.1: long takes,
# practical effects, painterly, sound-design-forward) and of `theme` material stated by someone
# other than the marketing department. Reception sections are handled separately as critic
# reviews and are excluded here to avoid storing them twice.
KEEP_SECTIONS = {
    "themes", "analysis", "interpretation", "style", "themes and analysis",
    "themes and interpretation", "production", "development", "writing",
    "pre-production", "casting", "filming", "cinematography", "photography",
    "visual effects", "special effects", "effects", "editing", "design",
    "production design", "costume design", "sound", "sound design", "music",
    "soundtrack", "score", "influences", "background", "adaptation",
    "box office", "setting", "characters", "genre",
}

SECTION_CHAR_CAP = 12000


def parse_wikipedia(data: Any) -> ParsedTitle:
    """The article's lead, its plot, and a capped budget of craft prose.

    The cap is a budget across all kept sections rather than a limit per section, so a film with
    one long Production section and a film with eight short ones cost the same. It is spent in
    the order `split_sections` returns, which is the article's own order.
    """
    pages = ((_payload(data) or {}).get("query") or {}).get("pages") or []
    if not pages:
        return ParsedTitle(source="wikipedia")
    page = pages[0] if isinstance(pages, list) else next(iter(pages.values()))
    rows = _Rows("wikipedia")
    sections = split_sections(page.get("extract") or "")
    plot = None
    for key in ("plot", "synopsis", "plot summary", "premise", "story"):
        if sections.get(key):
            plot = sections[key]
            break

    kept: dict[str, str] = {}
    budget = SECTION_CHAR_CAP
    for name, body in sections.items():
        if name not in KEEP_SECTIONS or budget <= 0:
            continue
        text = clean_text(body)[:budget]
        if len(text) < 80:
            continue
        kept[name] = text
        budget -= len(text)

    article = (page.get("title") or "").replace(" ", "_")
    rows.emit(
        "title_meta",
        year=None, runtime_min=None, tagline=None,
        plot_short=clean_text(sections.get("_lead", ""))[:2000] or None,
        plot_full=clean_text(plot) if plot else None,
        status=None, original_language=None, budget=None, revenue=None, poster_url=None,
        backdrop_url=None, homepage=f"https://en.wikipedia.org/wiki/{article}",
        content_rating=None, episode_count=None, season_count=None, first_air_date=None,
        last_air_date=None, in_production=None,
        extra=json.dumps({"page_id": page.get("pageid"), "title": page.get("title"),
                          "section_lengths": {k: len(v) for k, v in sections.items()},
                          "craft_sections": kept}, ensure_ascii=False),
    )
    return rows.done()


def parse_mpst(data: Any) -> ParsedTitle:
    """MPST synopsis and tags.

    The synopsis is the longest plot text in the corpus - a median ~4,900 chars against
    Wikipedia's ~2,900 - because it is a full retelling rather than a lead paragraph, and it
    gives away the ending either way. It lands in `plot_full` under its own source, next to the
    others, unreconciled, and `SOURCE_PRIORITY` puts mpst LAST for exactly that reason
    (`importer/meta.py:33-37`: "a poor default and a good last resort").

    The tags come from a closed 71-term vocabulary, so unlike TMDB's open keyword list or
    MovieLens' inferred genome they are a controlled label set. `extra` records which of the 71
    were applied. They are crowd annotations, not expert ones, so treat them as a precision
    check, never as negatives.

    Nothing in this app fetches MPST -- it is a bulk dataset the corpus joined once, not one of
    §8 stage 2's eight sources -- and this parser exists for the reason the module header gives
    for `parse_letterboxd_page`: the shipped bundle carries `mpst` rows and a re-parse path that
    cannot read them is a hole.
    """
    data = _payload(data)
    if data is None:
        return ParsedTitle(source="mpst")
    rows = _Rows("mpst")
    synopsis = clean_text(data.get("plot_synopsis") or "")
    tags = [t for t in (data.get("tags") or []) if t]

    if synopsis or tags:
        rows.emit(
            "title_meta",
            year=None, runtime_min=None, tagline=None, plot_short=None,
            plot_full=synopsis or None, status=None, original_language=None, budget=None,
            revenue=None, poster_url=None, backdrop_url=None, homepage=None, content_rating=None,
            episode_count=None, season_count=None, first_air_date=None, last_air_date=None,
            in_production=None,
            extra=json.dumps({"tags": tags, "synopsis_source": data.get("synopsis_source"),
                              "mpst_split": data.get("split")}, ensure_ascii=False),
        )
    for tag in tags:
        rows.emit("title_keyword", keyword=tag)
    return rows.done()


# Wikidata models writing duos, sibling teams and bands as entities in their own right, so a
# P57/P58 statement can point at "the Wachowskis" rather than at a person. Stored as people they
# become graph nodes sitting BESIDE the humans they are made of - Fargo's editors read
# ['Coen brothers', 'Ethan Coen', 'Joel Coen'], and "Ben Davis and Camille Griffin" holds 19 dp
# credits next to Ben Davis's own.
_COLLECTIVE = re.compile(r"(\s(and|&)\s|/|\b(brothers|sisters|bros|brothers\.|team)\b)", re.I)


def is_collective(name: str | None) -> bool:
    """Is this label a duo/collective rather than one human?"""
    n = (name or "").strip()
    if not n:
        return False
    if _COLLECTIVE.search(n):
        return True
    # "Zucker, Abrahams and Zucker" style. Hyphenated pen-names for pairs ("Salim-Javed",
    # "Boileau-Narcejac") are deliberately NOT caught: real surnames and given names hyphenate
    # too (Hou Hsiao-hsien, Jean-Pierre Jeunet), and a rule that fires on those would cost far
    # more than it saves.
    return n.count(",") >= 2


WD_PEOPLE = {"P57": ("Directing", "Director", "director"),
             "P58": ("Writing", "Screenplay", "writer"),
             "P344": ("Camera", "Director of Photography", "dp"),
             "P86": ("Sound", "Composer", "composer"),
             "P1040": ("Editing", "Editor", "editor"),
             "P2554": ("Art", "Production Designer", "prod_designer")}


def _wd_value(statement: Mapping[str, Any]) -> Any:
    return ((statement.get("mainsnak") or {}).get("datavalue") or {}).get("value")


def parse_wikidata_entity(entity: Any, labels: Mapping[str, str] | None = None) -> ParsedTitle:
    """A `wbgetentities` entity plus the label lookup its Q-ids resolve through.

    NOTHING IN THIS APP FETCHES THIS DOCUMENT. Decision 374 ports only `wikidata:resolve` of the
    corpus's three Wikidata handlers, because `wikidata:entity` and `wikidata:labels` are batch
    kinds -- shapes of a wholesale crawl over nineteen thousand titles -- and §8's unit is one
    title. The parser is still owed: the bundle ships `wikidata` rows in `title_meta`, `credit`
    and `award`, so a re-parse path without it cannot re-derive a title the corpus enriched. A
    reader who wants the data crawled again is looking at a decision, not a missing function.

    `labels` is separate because Wikidata's entity holds Q-ids and not names: P57 says the
    director is Q193570, and the label lookup is the batch the corpus resolved separately. An
    absent label is a credit NOT emitted rather than a credit named "Q193570".
    """
    entity = _payload(entity)
    if entity is None:
        return ParsedTitle(source="wikidata")
    labels = labels or {}
    rows = _Rows("wikidata")
    claims = entity.get("claims") or {}

    def qids(prop: str, *, credits: bool = False) -> list[str]:
        """Q-ids from a property's statements.

        With `credits=True`, honour the two things Wikidata uses to say "not really": a
        DEPRECATED rank, and qualifiers that scope the claim to something other than the work
        itself. Ignoring them put Sam Mendes and Danny Boyle on *No Time to Die* (both explicitly
        deprecated), Danny Boyle on *28 Weeks Later*, the Spanish dubbing director on *The Jungle
        Book*, and John Lasseter on *Toy Story 4* via an end-time qualifier - 142 deprecated
        statements over six credit properties.
        """
        out = []
        for statement in claims.get(prop, []):
            if credits:
                if statement.get("rank") == "deprecated":
                    continue
                qualifiers = statement.get("qualifiers") or {}
                # P3831 object has role (dubbing director), P582 end time
                if "P3831" in qualifiers or "P582" in qualifiers:
                    continue
            value = _wd_value(statement)
            if isinstance(value, Mapping) and value.get("id"):
                out.append(value["id"])
        return out

    def qids_with_year(prop: str) -> list[tuple[str, int | None]]:
        """Q-ids plus the statement's "point in time" qualifier (P585).

        That qualifier is what makes an award row answerable by year - the mainsnak alone says
        only THAT the film won something.
        """
        out: list[tuple[str, int | None]] = []
        for statement in claims.get(prop, []):
            value = _wd_value(statement)
            if not (isinstance(value, Mapping) and value.get("id")):
                continue
            year = None
            for qualifier in (statement.get("qualifiers") or {}).get("P585") or []:
                time = ((qualifier.get("datavalue") or {}).get("value") or {}).get("time")
                if isinstance(time, str) and re.match(r"[+-]\d{4}", time):
                    year = int(time[1:5])
                    break
            out.append((value["id"], year))
        return out

    def quantities(prop: str) -> list[float]:
        out = []
        for statement in claims.get(prop, []):
            value = _wd_value(statement)
            if isinstance(value, Mapping) and value.get("amount"):
                with contextlib.suppress(ValueError):
                    out.append(float(str(value["amount"]).lstrip("+")))
        return out

    for prop, (department, job, role) in WD_PEOPLE.items():
        for qid in qids(prop, credits=True):
            name = labels.get(qid)
            if not name or is_collective(name):
                continue
            rows.emit("credit", person={"name": name}, department=department, job=job,
                      character=None, billing_order=None, episode_count=None, role_class=role)

    for qid in qids("P495"):
        if labels.get(qid):
            rows.emit("title_country", country=labels[qid])
    for qid in qids("P364"):
        if labels.get(qid):
            rows.emit("title_language", language=labels[qid], is_primary=1)
    for qid in qids("P136"):
        if labels.get(qid):
            rows.emit("title_genre", genre=labels[qid], position=None)
    for prop in ("P921", "P840", "P915"):
        for qid in qids(prop):
            if labels.get(qid):
                rows.emit("title_keyword", keyword=labels[qid])
    for qid in qids("P272"):
        if labels.get(qid):
            rows.emit("title_company", company=labels[qid], role="production", country=None)
    for qid in qids("P449"):
        if labels.get(qid):
            rows.emit("title_company", company=labels[qid], role="network", country=None)

    for prop, result in (("P166", "won"), ("P1411", "nominated")):
        for qid, year in qids_with_year(prop):
            if labels.get(qid):
                rows.emit("award", award=labels[qid], category="award", year=year, result=result,
                          person=None, count=1)

    budget = quantities("P2130")
    box_office = quantities("P2142")
    if budget or box_office:
        description = ((entity.get("descriptions") or {}).get("en") or {}).get("value")
        rows.emit(
            "title_meta",
            year=None, runtime_min=None, tagline=None, plot_short=None, plot_full=None,
            status=None, original_language=None,
            budget=int(max(budget)) if budget else None,
            revenue=int(max(box_office)) if box_office else None,
            poster_url=None, backdrop_url=None, homepage=None, content_rating=None,
            episode_count=None, season_count=None, first_air_date=None, last_air_date=None,
            in_production=None,
            extra=json.dumps({"qid": entity.get("id"), "description": description},
                             ensure_ascii=False),
        )
    return rows.done()


# ---------------------------------------------------------------------------
# Score-only pages (Letterboxd / RT / Metacritic)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(re.sub(r"[^\d]", "", str(value)) or 0) or None
    except ValueError:
        return None


def parse_letterboxd_page(content: bytes) -> ParsedTitle:
    """Letterboxd's film page: an aggregate rating out of 5, and its genres.

    DECISION 374 DOES NOT PORT THE CRAWLER. §8 stage 2 names eight sources and Letterboxd is not
    among them, so `mdc/sources/letterboxd.py` stays in the corpus and nothing in this app fetches
    this page. `title.letterboxd_slug` keeps being written -- by `wikidata:resolve`, which yields
    it -- so the column is filled and the source is not crawled. The parser is here because the
    bundle ships `letterboxd` rows and `SOURCE_PRIORITY` orders them; adding an adapter to match
    would be reversing a decision, not filling a gap.
    """
    rows = _Rows("letterboxd")
    for blob in ld_json(content):
        for item in (blob if isinstance(blob, list) else [blob]):
            if not isinstance(item, Mapping):
                continue
            aggregate = item.get("aggregateRating") or {}
            if aggregate.get("ratingValue") is not None:
                rows.emit("platform_rating", metric="user_score",
                          value=float(aggregate["ratingValue"]),
                          scale=float(aggregate.get("bestRating") or 5),
                          votes=_as_int(aggregate.get("ratingCount")))
            for genre in item.get("genre") or []:
                if isinstance(genre, str):
                    rows.emit("title_genre", genre=genre, position=None)
    return rows.done()


_RT_SCORECARD = re.compile(rb"<media-scorecard\b.*?</media-scorecard>", re.I | re.S)
_RT_SLOT = re.compile(
    r'<rt-text[^>]*slot="(critics-score|audience-score)"[^>]*>\s*([\d.]+)\s*%', re.I | re.S)
_RT_REVIEW_LINK = re.compile(
    r'<rt-link[^>]*slot="(critics-reviews|audience-reviews)"[^>]*>\s*([\d,]+)[^<]*</rt-link>',
    re.I | re.S)


def parse_rt_page(content: bytes) -> ParsedTitle:
    """RT renders the scorecard server-side as custom elements.

    Scoped to the `<media-scorecard>` element deliberately: the same `slot="critics-score"` markup
    is reused by the related-titles carousel further down the page, so an unscoped scan picks up a
    neighbouring film's percentage instead of this one's.

    Historically the scores lived in an embedded JSON blob; that shape is still handled because
    older captures in the raw store use it - which is the raw store earning its keep. A source
    that changed its markup twice is a source whose every past capture is still parseable by
    whichever branch fits, without a request.
    """
    rows = _Rows("rottentomatoes")
    text = content.decode("utf-8", "replace")
    card = _RT_SCORECARD.search(content)
    scope = card.group(0).decode("utf-8", "replace") if card else text

    found: set[str] = set()
    for match in _RT_SLOT.finditer(scope):
        metric = "critic_score" if match.group(1).lower() == "critics-score" else "audience_score"
        if metric in found:
            continue          # first occurrence is this title's own score
        with contextlib.suppress(ValueError):
            rows.emit("platform_rating", metric=metric, value=float(match.group(2)), scale=100.0,
                      votes=None)
            found.add(metric)
        if len(found) == 2:
            break

    counted: set[str] = set()
    for match in _RT_REVIEW_LINK.finditer(scope):
        metric = ("critic_review_count" if match.group(1).lower() == "critics-reviews"
                  else "audience_rating_count")
        if metric in counted:
            continue
        counted.add(metric)
        rows.emit("platform_rating", metric=metric, value=float(_as_int(match.group(2)) or 0),
                  scale=None, votes=None)
    if found:
        return rows.done()

    blobs: list[Any] = []
    data = next_data(content)
    if data:
        blobs.append(data)
    for match in re.finditer(
        rb'<script[^>]*type="application/json"[^>]*>(.*?)</script>', content, re.S
    ):
        try:
            blobs.append(json.loads(match.group(1).decode("utf-8", "replace")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    for blob in blobs:
        for node in walk(blob, lambda n: isinstance(n, dict)
                         and ("criticsScore" in n or "audienceScore" in n)):
            for key, metric in (("criticsScore", "critic_score"),
                                ("audienceScore", "audience_score")):
                sub = node.get(key)
                if isinstance(sub, Mapping) and sub.get("score") is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        rows.emit("platform_rating", metric=metric, value=float(sub["score"]),
                                  scale=100.0,
                                  votes=_as_int(sub.get("ratingCount") or sub.get("reviewCount")))
            return rows.done()
    return rows.done()


_MC_TITLE_SCORE = re.compile(
    r'title="(Metascore|User score)\s+([\d.]+)\s+out of\s+(\d+)"', re.I)


def parse_metacritic_page(content: bytes) -> ParsedTitle:
    """Metacritic's title page: the Metascore and the user score, both from an attribute.

    The scores are read off the `title=` attribute rather than the element text because the
    attribute states its own scale -- "Metascore 81 out of 100", "User score 8.2 out of 10" --
    and the two scales differ by a factor of ten. A reader taking the digits from the element
    would have to know which one it was looking at, and the page's class names are Tailwind and
    change constantly.

    THE FIRST OCCURRENCE OF EACH IS THE PAGE'S OWN, and everything after it belongs to the
    "best movies" carousel further down: a real capture of this page carries forty-two matches,
    of which two are about this film. That is the same class of defect `_RT_SCORECARD` scopes
    away, one page over, caught by ordering instead of by scope.
    """
    rows = _Rows("metacritic")
    text = content.decode("utf-8", "replace")
    seen: set[str] = set()
    for match in _MC_TITLE_SCORE.finditer(text):
        metric = "critic_score" if match.group(1).lower() == "metascore" else "user_score"
        if metric in seen:
            continue
        seen.add(metric)
        with contextlib.suppress(ValueError):
            rows.emit("platform_rating", metric=metric, value=float(match.group(2)),
                      scale=float(match.group(3)), votes=None)
        if len(seen) == 2:
            break
    if seen:
        return rows.done()

    data = next_data(content)
    if data is None:
        return rows.done()
    for node in walk(data, lambda n: isinstance(n, dict) and "criticScoreSummary" in n):
        critic = node.get("criticScoreSummary") or {}
        if critic.get("score") is not None:
            rows.emit("platform_rating", metric="critic_score", value=float(critic["score"]),
                      scale=100.0, votes=_as_int(critic.get("reviewCount")))
        user = node.get("userScoreSummary") or {}
        if user.get("score") is not None:
            rows.emit("platform_rating", metric="user_score", value=float(user["score"]),
                      scale=10.0, votes=_as_int(user.get("reviewCount")))
        break
    return rows.done()


# ---------------------------------------------------------------------------
# Did the slug land on the right film?
# ---------------------------------------------------------------------------


def _ld_year(content: bytes, *, fields: tuple[str, ...]) -> int | None:
    """Release year from a page's schema.org block, if it states one.

    Both RT and Metacritic embed a `Movie`/`TVSeries` node describing THE PAGE'S OWN TITLE, which
    is the only thing on either page that can settle whether a guessed slug landed on the right
    film.
    """
    for blob in ld_json(content):
        for item in (blob if isinstance(blob, list) else [blob]):
            if not isinstance(item, Mapping):
                continue
            if item.get("@type") not in ("Movie", "TVSeries", "TVSeason", "CreativeWork",
                                         "CreativeWorkSeries"):
                continue
            for name in fields:
                match = re.match(r"(\d{4})-\d{2}-\d{2}", str(item.get(name) or ""))
                if match:
                    return int(match.group(1))
    return None


def metacritic_page_year(content: bytes) -> int | None:
    return _ld_year(content, fields=("datePublished", "dateCreated"))


_RT_TITLE_YEAR = re.compile(rb"<title>[^<]*\((\d{4})\)[^<]*</title>", re.I)


def rt_page_year(content: bytes) -> int | None:
    year = _ld_year(content, fields=("dateCreated", "datePublished"))
    if year:
        return year
    # RT only disambiguates in the page title when it has to - "Alpha (2026)" exists because
    # "Alpha" is already taken - which is exactly the case a guessed slug gets wrong, so it is
    # worth reading.
    match = _RT_TITLE_YEAR.search(content)
    return int(match.group(1)) if match else None


def page_people(content: bytes) -> set[str]:
    """Directors and billed cast the page claims, as loose-match keys."""
    out: set[str] = set()
    for blob in ld_json(content):
        for item in (blob if isinstance(blob, list) else [blob]):
            if not isinstance(item, Mapping):
                continue
            for name in ("director", "actor", "creator", "author"):
                value = item.get(name)
                for entry in (value if isinstance(value, list) else [value]):
                    person = (entry.get("name") if isinstance(entry, Mapping)
                              else entry if isinstance(entry, str) else None)
                    key = loose_name(clean_text(person)) if person else ""
                    if key:
                        out.add(key)
    return out


# A page's stated release date and our title's year routinely differ by a year or two - festival
# vs general release, US vs original market - and by much more for a restoration: Metacritic
# dates `movie/black-orpheus` 2006, the Criterion re-release of a 1959 film. So the year alone
# cannot decide.
#
# THE TOLERANCE IS TWO YEARS AND IS PORTED EXACTLY. Widening it re-admits the collision it
# exists to catch: `movie/alpha` is the 2018 film and there is a 2026 one, eight years apart, but
# same-name pairs two and three years apart are ordinary. Narrowing it to zero refuses a film
# whose festival premiere and general release straddle a new year, which is a large minority of
# everything a household acquires in January. The corpus measured both failures on real pages and
# this is where it landed; a change here needs the same measurement, not an opinion.
PAGE_YEAR_TOLERANCE = 2


def page_belongs_to_title(content: bytes, *, year: int | None, people: set[str],
                          mode: str = "metacritic", people_decide: bool = True) -> bool:
    """Is this scraped page really about the title we asked for?

    A slug guessed from the title cannot tell two films of the same name apart, and the site
    keeps whichever one it has: `movie/alpha` is the 2018 film, not the 2026 one, and every
    review on it would otherwise be written against the wrong `title_id` - with text that
    discusses a different film entirely, which no dedup or length filter can catch. §8 stage 7's
    quote verification cannot catch it either, and that is the reason this check is not optional
    here: a quote lifted from the wrong film's pack IS a genuine substring of that pack, so the
    verifier passes it and the household is shown a sentence about another film.

    It is the scraped-source twin of the refusal `connectors/resolve.py:145-179` makes at the
    Jellyfin end, and for the same stated reason - a wrong match is worse than no match, because
    nothing downstream can tell.

    Two independent signals, and a shared cast is much the stronger one. The year alone cannot
    decide: it clears a same-name, same-year collision (Metacritic's `tv/heartland` is the
    American 2007 series, not the Canadian one) and it condemns legitimate restorations
    (`movie/black-orpheus` is dated 2006, the re-release of a 1959 film).

    `people_decide` says how much a disjoint cast is worth on its own, and that is set by what
    the page carries. A Metacritic page brings REVIEW TEXT, so a wrong one poisons the corpus and
    a disjoint cast is enough to refuse it. A Rotten Tomatoes page brings only two percentages,
    while its cast list is the English dub for every anime series we hold - so there either
    signal may vouch for the page, and only a page that fails both is dropped.
    """
    year_of = metacritic_page_year if mode == "metacritic" else rt_page_year
    theirs = page_people(content)
    page_year = year_of(content)

    people_known = bool(theirs and people)
    if people_known and (theirs & people):
        return True
    if people_known and people_decide:
        return False
    if page_year is not None and page_year >= 1870 and year is not None:
        return abs(page_year - year) <= PAGE_YEAR_TOLERANCE
    # Nothing on the page contradicts us: absence of evidence is not evidence.
    return not people_known


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

# `(source, kind prefix)` -> the parser for it, and whether the parser wants decoded JSON. The
# kinds are the corpus's raw-document kinds (`mdc/sources/*.py`), which are what M5.1's raw store
# records verbatim, so a document stored by an adapter routes here on the two columns the store
# already carries (`entity_key` aside) and no third vocabulary is invented.
# THE THIRD COLUMN IS THE `source` THE ROWS ARE FILED UNDER, and it is not always the first.
# The raw store records what was crawled (`mpst_bulk`) and `title_meta` records who said it
# (`mpst`), and they differ for exactly one entry - which is enough to need the column, because
# decision 375's delete scope is the row source. A parse that returns ZERO rows still has to
# delete last run's rows for the source it was about, and `ParsedTitle.row_sources` is empty
# precisely then, so the scope has to be knowable without a successful parse.
_JSON_PARSERS: dict[tuple[str, str], tuple[Any, str]] = {
    ("tmdb", "movie_detail"): (parse_tmdb_detail, "tmdb"),
    ("tmdb", "tv_detail"): (parse_tmdb_detail, "tmdb"),
    ("omdb", "detail"): (parse_omdb, "omdb"),
    ("jellyfin", "items"): (parse_jellyfin_item, "jellyfin"),
    ("trakt", "summary"): (parse_trakt_summary, "trakt"),
    ("trakt", "ratings"): (parse_trakt_ratings, "trakt"),
    ("tvmaze", "show"): (parse_tvmaze, "tvmaze"),
    ("wikipedia", "article"): (parse_wikipedia, "wikipedia"),
    ("mpst_bulk", "meta"): (parse_mpst, "mpst"),
}

_BYTE_PARSERS: dict[tuple[str, str], tuple[Any, str]] = {
    ("rottentomatoes", "page"): (parse_rt_page, "rottentomatoes"),
    ("metacritic", "page"): (parse_metacritic_page, "metacritic"),
    ("letterboxd", "film"): (parse_letterboxd_page, "letterboxd"),
}


def parse_document(source: str, kind: str, content: bytes) -> ParsedTitle:
    """One raw document to its rows, and NEVER an exception.

    `mdc/parse/reviews.py:459-485` is the same dispatch with the same blanket `except`, and the
    argument for it is in that module's header: "Each source yields whatever it can rather than
    failing the document. A site that changes its markup costs a parser fix and a rebuild, never
    a re-crawl." Decision 334 is this app's version -- every source but `tmdb:detail` is
    best-effort and its failure is a note on the job, not a park -- and a parser that raised on
    changed markup would convert that note into a failed stage for a document already in the raw
    store, which no retry can improve until someone edits this file.

    THE SWALLOW IS BOUNDED AND VISIBLE, which is what keeps it from hiding a bug in this module:
    it returns an EMPTY `ParsedTitle` carrying the source, so the derive can see that a document
    it holds produced nothing and say so on the board. An unknown `(source, kind)` returns the
    same empty result rather than raising, because the raw store is append-only and holds kinds
    from crawls this version of the parser predates.

    `kind` is matched on its prefix before the colon: the store records `page:main` and
    `reviews:critics`, and only the part before the colon chooses a parser.
    """
    head = kind.split(":", 1)[0]
    entry = _BYTE_PARSERS.get((source, head))
    if entry is not None:
        parser, label = entry
        try:
            return parser(content)
        except Exception:                                  # noqa: BLE001
            return ParsedTitle(source=label)
    entry = _JSON_PARSERS.get((source, head))
    if entry is None:
        return ParsedTitle(source=source)
    parser, label = entry
    try:
        return parser(json.loads(content.decode("utf-8", "replace")))
    except Exception:                                      # noqa: BLE001
        return ParsedTitle(source=label)


def parsed_sources() -> Mapping[tuple[str, str], str]:
    """`(store source, kind prefix)` -> the `source` its rows are filed under.

    The derive's map from a `raw_document` row to a delete scope, and this module's own answer to
    "which documents can I read at all". Returned as a copy: the registry is not a thing a caller
    may add to, because a parser it did not import would not exist.
    """
    return {**_JSON_PARSERS_LABELS, **_BYTE_PARSERS_LABELS}


_JSON_PARSERS_LABELS = {key: label for key, (_fn, label) in _JSON_PARSERS.items()}
_BYTE_PARSERS_LABELS = {key: label for key, (_fn, label) in _BYTE_PARSERS.items()}
