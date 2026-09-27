"""§8 stage 3's parsers: raw bytes in, derived rows out, and nothing in between.

No connection and no transport: rows carry people as data. Every source's rows are kept side by
side; the reader picks. Rows may repeat, so the derive inserts with `ON CONFLICT DO NOTHING`.
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

# The eleven derived tables; a typo in an emit is a KeyError here.
TABLES: tuple[str, ...] = (
    "title_meta", "title_genre", "title_keyword", "title_language", "title_country",
    "title_company", "title_alias", "title_video", "credit", "award", "platform_rating",
)

# The corpus keyword weight and `person.gender` are not emitted: this schema has no such columns.


@dataclass(frozen=True)
class ParsedTitle:
    """What one raw document says, as rows, before anything has touched the database.

    `source` is the document's; rows may be filed under others (OMDb relays IMDb/RT/MC scores).
    """

    source: str
    rows: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)

    def table(self, name: str) -> tuple[Mapping[str, Any], ...]:
        """The rows for one target table, empty when the document had none."""
        return self.rows.get(name, ())

    @property
    def meta(self) -> Mapping[str, Any] | None:
        """The `title_meta` payload this document produced, or None; at most one per document."""
        rows = self.table("title_meta")
        return rows[0] if rows else None


class _Rows:
    """The corpus's `emit` sink, as a collector."""

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

    A 200 that decoded to a list, string or `{}` yields no rows, never an exception or an all-NULL row.
    """
    return data if isinstance(data, Mapping) and data else None


# TMDB


def _year(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _tmdb_img(path: str | None, size: str = "w500") -> str | None:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


def _tmdb_certification(data: Mapping[str, Any], *, is_movie: bool) -> str | None:
    """The one age rating kept, from the three markets this household's catalog is rated in."""
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

    `is_movie` comes from the payload's shape; `title.kind` is never flipped (decision 372).
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

    A series crew entry carries several `jobs`; each is classified separately.
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


# OMDb

_OMDB_NA = {"N/A", "", None}


def _omdb_val(value: Any) -> Any:
    """OMDb's value, with "N/A" mapped to NULL and (doubly escaped) entities decoded."""
    if value in _OMDB_NA:
        return None
    return unescape(value) if isinstance(value, str) else value


# IMDb writes qualified languages inverted: "Norse, Old" is Old Norse, not two languages.
_LANG_QUALIFIERS = {"old", "ancient", "middle", "modern", "classical"}


def _omdb_languages(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        # OMDb's literal for a silent film: not a language.
        if part.lower() == "none":
            continue
        head = re.sub(r"\s*\([^)]*\)", "", part).strip()
        if head.lower() in _LANG_QUALIFIERS and out:
            out[-1] = f"{head} {out[-1]}"
            continue
        out.append(part)
    return out


# The headline award name runs to a full stop, the start of the tally, or the end.
_AWARD_HEADLINE = re.compile(
    r"\b(Won|Nominated for)\s+(\d+)\s+(.+?)\s*(?=\d+\s+(?:win|nomination)|\.|$)", re.I)
_AWARD_TALLY = re.compile(r"(\d+)\s+(win|nomination)s?\b", re.I)


def parse_omdb_awards(blurb: str | None) -> list[tuple[str, str, str, int]]:
    """Turn OMDb's awards sentence into rows of (award, category, result, n).

    Only the headline award and totals are recoverable; per-category detail comes from Wikidata.
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


# The third column is the source each score is really about: OMDb only relays them.
_OMDB_RATINGS: tuple[tuple[str, str, str, float, str], ...] = (
    ("Internet Movie Database", r"([\d.]+)/10", "imdb", 10.0, "user_score"),
    ("Rotten Tomatoes", r"(\d+)%", "rottentomatoes", 100.0, "critic_score"),
    ("Metacritic", r"(\d+)/100", "metacritic", 100.0, "critic_score"),
)


def parse_omdb(data: Any) -> ParsedTitle:
    """OMDb's `?i=tt...` detail blob: IMDb's fields, flattened into strings needing regex repair."""
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
            # The parenthesised contribution belongs in `job`, not the name.
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

    # The blurb itself is kept in `title_meta.extra`.
    for name, category, result, count in parse_omdb_awards(_omdb_val(data.get("Awards"))):
        rows.emit("award", award=name, category=category, year=None, result=result,
                  person=None, count=count)

    return rows.done()


# Jellyfin  (owned-library presentation signals)


def parse_jellyfin_item(item: Any) -> ParsedTitle:
    """The household's own copy: codecs, channels, subtitle languages, the file."""
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
        # Jellyfin's `Role` is the job for crew, so only actors get it as a character.
        rows.emit(
            "credit",
            person={"name": person.get("Name") or "?", "imdb_id": providers.get("imdb")},
            department=person.get("Type"), job=person.get("Type"),
            character=person.get("Role") if kind == "actor" else None,
            billing_order=order, episode_count=None, role_class=role,
        )

    return rows.done()


# Trakt / TVmaze


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
    """Trakt's rating histogram, one `platform_rating` row per star."""
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


# Wikipedia / Wikidata

# Shared by `parse_wikipedia` and `parse_wikipedia_reception`, which must agree on section starts
# or the reception prose is stored twice.
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


# Sections where Wikipedia describes how a film was made; Reception is handled as critic reviews.
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

    The cap is a budget across all kept sections, spent in article order.
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

    The synopsis is the longest plot text and spoils the ending, so `SOURCE_PRIORITY` puts mpst last.
    Tags are a closed 71-term crowd vocabulary: a precision check, never negatives.
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


# Wikidata models duos and teams as entities; stored as people they sit beside their members.
_COLLECTIVE = re.compile(r"(\s(and|&)\s|/|\b(brothers|sisters|bros|brothers\.|team)\b)", re.I)


def is_collective(name: str | None) -> bool:
    """Is this label a duo/collective rather than one human?"""
    n = (name or "").strip()
    if not n:
        return False
    if _COLLECTIVE.search(n):
        return True
    # "Zucker, Abrahams and Zucker" style. Hyphenated pair pen-names are deliberately not caught:
    # real names hyphenate too.
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

    Nothing fetches this document (decision 374); the bundle ships its rows. An absent label emits no
    credit rather than one named "Q193570".
    """
    entity = _payload(entity)
    if entity is None:
        return ParsedTitle(source="wikidata")
    labels = labels or {}
    rows = _Rows("wikidata")
    claims = entity.get("claims") or {}

    def qids(prop: str, *, credits: bool = False) -> list[str]:
        """Q-ids from a property's statements.

        With `credits=True`, skips DEPRECATED ranks and qualifiers scoping the claim elsewhere (142
        deprecated statements put wrong directors on films).
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
        """Q-ids plus the statement's "point in time" qualifier (P585), which dates an award."""
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


# Score-only pages (Letterboxd / RT / Metacritic)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(re.sub(r"[^\d]", "", str(value)) or 0) or None
    except ValueError:
        return None


def parse_letterboxd_page(content: bytes) -> ParsedTitle:
    """Letterboxd's film page: an aggregate rating out of 5, and its genres.

    Nothing fetches this page (decision 374); the bundle ships its rows.
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

    Scoped to `<media-scorecard>`: the related-titles carousel reuses the same markup. The older
    embedded-JSON shape is still read for old captures.
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

    The `title=` attribute states its scale. Only the first occurrence of each is this film's;
    the rest belong to a carousel.
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


# Did the slug land on the right film?


def _ld_year(content: bytes, *, fields: tuple[str, ...]) -> int | None:
    """Release year from a page's schema.org block describing the page's own title, if it states one."""
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
    # RT only puts the year in the title when disambiguating, which is exactly the risky case.
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


# Festival vs general release and restorations shift years. Ported exactly: wider re-admits
# same-name collisions, zero refuses January releases. Change only with a new measurement.
PAGE_YEAR_TOLERANCE = 2


def page_belongs_to_title(content: bytes, *, year: int | None, people: set[str],
                          mode: str = "metacritic", people_decide: bool = True) -> bool:
    """Is this scraped page really about the title we asked for?

    A wrong page's quotes pass §8 stage 7's verifier, so this check is not optional. A shared cast is
    the stronger signal; `people_decide` sets whether a disjoint cast alone refuses (Metacritic) or
    either signal may vouch (RT, whose casts are English dubs).
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


# dispatch

# `(source, kind prefix)` -> (parser, wants JSON, row source). The row source can differ from the
# store source (`mpst_bulk` -> `mpst`) and must be known even when a parse yields nothing.
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

    Changed markup costs a parser fix, never a failed stage. Failures and unknown kinds return an empty
    `ParsedTitle` carrying the source, so the derive can report it. `kind` matches on its prefix.
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
    """`(store source, kind prefix)` -> the `source` its rows are filed under. Returns a copy."""
    return {**_JSON_PARSERS_LABELS, **_BYTE_PARSERS_LABELS}


_JSON_PARSERS_LABELS = {key: label for key, (_fn, label) in _JSON_PARSERS.items()}
_BYTE_PARSERS_LABELS = {key: label for key, (_fn, label) in _BYTE_PARSERS.items()}
