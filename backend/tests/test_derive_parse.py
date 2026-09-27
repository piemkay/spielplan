"""§8 stage 3's parsers against responses real servers sent
(`fixtures/sources/README.md` records them): hand-written markup would hide every
real-world trap. Only `upsert_person` and `known_people` touch the database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spielplan.derive import ids, parse, reviews

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def rows(parsed: parse.ParsedTitle, table: str) -> list[dict]:
    return [dict(row) for row in parsed.table(table)]


def values(parsed: parse.ParsedTitle, table: str, *fields: str) -> list[tuple]:
    return [tuple(row.get(f) for f in fields) for row in parsed.table(table)]


@pytest.fixture
def tmdb() -> parse.ParsedTitle:
    return parse.parse_document("tmdb", "movie_detail", fixture("tmdb_movie_detail.json"))


def test_the_tmdb_detail_becomes_one_meta_row_in_the_corpus_column_names(tmdb):
    """A payload key spelled differently from `title_meta`'s
    shipped columns would be invisible to `meta.best`."""
    meta = tmdb.meta
    assert meta is not None and len(tmdb.table("title_meta")) == 1
    assert meta["source"] == "tmdb"
    assert (meta["year"], meta["runtime_min"], meta["content_rating"]) == (2016, 116, "PG-13")
    assert meta["tagline"] == "Why are they here?"
    assert meta["plot_full"].startswith("Taking place after alien crafts land")
    assert meta["plot_short"] is None, "TMDB carries one plot; `plot_short` is Wikipedia's lead"
    assert meta["budget"] == 47_000_000 and meta["revenue"] == 203_388_186
    assert meta["poster_url"] == (
        "https://image.tmdb.org/t/p/w500/x2FJsf1ElAgr63Y3PNPtJrcmpoe.jpg"
    )
    assert meta["backdrop_url"].startswith("https://image.tmdb.org/t/p/w1280/"), (
        "the backdrop is requested at w1280 and the poster at w500; one size for both would "
        "serve a 500px backdrop to every card"
    )
    assert set(meta) == {
        "source", "year", "runtime_min", "tagline", "plot_short", "plot_full", "status",
        "original_language", "budget", "revenue", "poster_url", "backdrop_url", "homepage",
        "content_rating", "episode_count", "season_count", "first_air_date", "last_air_date",
        "in_production", "extra",
    }


def test_the_certification_is_read_from_one_of_three_markets_and_not_from_all_of_them(tmdb):
    """FR comes first in the fixture and is not one of the three
    markets, so "first entry" would store a visa number."""
    payload = json.loads((FIXTURES / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
    markets = [r["iso_3166_1"] for r in payload["release_dates"]["results"]]
    assert markets[0] not in ("US", "GB", "DE"), "the fixture no longer exercises the skip"
    assert tmdb.meta["content_rating"] == "PG-13"


def test_only_the_top_six_billed_cast_survive_and_the_seventh_and_eighth_do_not(tmdb):
    """The fixture ships eight so the cut has a boundary."""
    payload = json.loads((FIXTURES / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
    assert len(payload["credits"]["cast"]) == 8, "the fixture no longer straddles the cut"
    cast = [r for r in tmdb.table("credit") if r["role_class"] == "cast"]
    assert [r["billing_order"] for r in cast] == [0, 1, 2, 3, 4, 5]
    assert [r["person"]["name"] for r in cast][:3] == [
        "Amy Adams", "Jeremy Renner", "Forest Whitaker"
    ]
    assert cast[0]["character"] == "Louise Banks" and cast[0]["department"] == "Acting"


def test_the_second_unit_and_the_stills_photographer_are_not_the_films_dp_or_director(tmdb):
    """Substring matching promoted 18,500 second-unit directors and 3,312 stills photographers."""
    payload = json.loads((FIXTURES / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
    jobs = {c["job"] for c in payload["credits"]["crew"]}
    assert {"First Assistant Director", "Still Photographer"} <= jobs, (
        "the fixture no longer carries a support credit to demote"
    )
    kept = {(r["job"], r["role_class"]) for r in tmdb.table("credit")}
    assert ("Director", "director") in kept
    assert ("Director of Photography", "dp") in kept
    assert not any(job in ("First Assistant Director", "Still Photographer") for job, _ in kept)


def test_every_tmdb_row_carries_the_seven_other_tables_with_their_own_source(tmdb):
    assert values(tmdb, "title_genre", "genre", "position", "source") == [
        ("Drama", 0, "tmdb"), ("Science Fiction", 1, "tmdb"), ("Mystery", 2, "tmdb"),
    ]
    assert values(tmdb, "title_keyword", "keyword", "source")[:2] == [
        ("spacecraft", "tmdb"), ("extraterrestrial technology", "tmdb"),
    ]
    assert values(tmdb, "title_company", "company", "role", "country")[0] == (
        "FilmNation Entertainment", "production", "US",
    )
    assert values(tmdb, "title_alias", "alias", "region")[0] == ("Story of Your Life", "US")
    assert values(tmdb, "title_video", "key", "site", "type")[0] == (
        "dyFa0tOwA1k", "YouTube", "Clip",
    )
    assert values(tmdb, "platform_rating", "metric", "scale", "source") == [
        ("user_score", 10.0, "tmdb"), ("popularity", None, "tmdb"),
    ]
    assert {r["source"] for table in tmdb.rows.values() for r in table} == {"tmdb"}


def test_tmdb_emits_the_primary_country_twice_and_the_derives_key_is_what_dedupes_it(tmdb):
    """TMDB names the primary country twice; the derive's key dedupes, the parser does not."""
    assert values(tmdb, "title_country", "country").count(("US",)) == 2


@pytest.fixture
def omdb() -> parse.ParsedTitle:
    return parse.parse_document("omdb", "detail", fixture("omdb_detail.json"))


def test_omdb_relays_three_other_platforms_scores_and_files_them_under_their_own_names(omdb):
    """OMDb relays IMDb, RT and Metacritic scores; filed under `omdb` they would collapse onto one row."""
    assert values(omdb, "platform_rating", "source", "metric", "value", "scale") == [
        ("imdb", "user_score", 7.9, 10.0),
        ("rottentomatoes", "critic_score", 94.0, 100.0),
        ("metacritic", "critic_score", 81.0, 100.0),
    ]
    assert omdb.table("platform_rating")[0]["votes"] == 852_057, (
        "`imdbVotes` is '852,057' and belongs to the IMDb row alone"
    )
    assert omdb.source == "omdb"


def test_omdbs_prose_fields_become_columns_again(omdb):
    """Runtime is "116 min", BoxOffice is "$100,546,139", Year is "2016"."""
    meta = omdb.meta
    assert (meta["runtime_min"], meta["revenue"], meta["year"]) == (116, 100_546_139, 2016)
    assert meta["content_rating"] == "PG-13"
    assert meta["plot_full"].startswith("Linguistics professor Louise Banks")


def test_a_writers_parenthesised_contribution_is_not_part_of_his_name(omdb):
    """A name with a parenthesis never matches the same human from TMDB."""
    writers = [r["person"]["name"] for r in omdb.table("credit") if r["role_class"] == "writer"]
    assert writers == ["Eric Heisserer", "Ted Chiang"]
    assert all("(" not in name for name in writers)


def test_omdbs_awards_sentence_becomes_a_headline_row_and_two_tallies(omdb):
    """"Won 1 Oscar. 71 wins & 268 nominations total" is all the award data OMDb has."""
    assert values(omdb, "award", "award", "category", "result", "count") == [
        ("Oscar", "ceremony", "won", 1),
        ("any award", "total", "won", 71),
        ("any award", "total", "nominated", 268),
    ]


@pytest.mark.parametrize(
    ("blurb", "expected"),
    [
        ("Won 7 Oscars. 21 wins & 43 nominations total",
         [("Oscar", "ceremony", "won", 7), ("any award", "total", "won", 21),
          ("any award", "total", "nominated", 43)]),
        # OMDb's templating drops the full stop; the headline must still end where the tally begins.
        ("Nominated for 1 BAFTA Award1 nomination total",
         [("BAFTA Award", "ceremony", "nominated", 1), ("any award", "total", "nominated", 1)]),
        ("N/A", []),
        (None, []),
    ],
)
def test_the_awards_sentence_is_read_up_to_whichever_comes_first(blurb, expected):
    assert parse.parse_omdb_awards(blurb) == expected


def test_the_trakt_summary_is_thin_and_says_so_in_the_rows_it_does_not_emit():
    parsed = parse.parse_document("trakt", "summary", fixture("trakt_summary.json"))
    meta = parsed.meta
    assert (meta["year"], meta["runtime_min"], meta["status"]) == (2016, 116, "released")
    assert meta["poster_url"] is None and meta["backdrop_url"] is None
    assert values(parsed, "title_genre", "genre") == [
        ("science-fiction",), ("drama",), ("mystery",)
    ]
    assert values(parsed, "title_country", "country") == [("US",)], (
        "Trakt writes the country lowercase and the column holds ISO codes"
    )
    assert values(parsed, "platform_rating", "metric", "scale") == [("user_score", 10.0)]


def test_the_trakt_histogram_stays_ten_rows_rather_than_one_blob():
    """`display.platform_rating` is keyed by metric, so the histogram stays ten rows."""
    parsed = parse.parse_document("trakt", "ratings", fixture("trakt_ratings.json"))
    metrics = [r["metric"] for r in parsed.table("platform_rating")]
    assert sorted(metrics) == sorted(f"dist_{n}" for n in range(1, 11))
    assert all(r["scale"] is None for r in parsed.table("platform_rating"))
    assert parsed.meta is None, "a histogram says nothing about the title itself"


def test_the_tvmaze_show_is_the_series_shaped_source():
    parsed = parse.parse_document("tvmaze", "show", fixture("tvmaze_show.json"))
    meta = parsed.meta
    assert (meta["year"], meta["season_count"], meta["status"]) == (2015, 6, "Ended")
    assert (meta["first_air_date"], meta["last_air_date"]) == ("2015-12-14", "2022-01-14")
    assert meta["in_production"] == 0, "status is Ended, so the flag is 0 and not NULL"
    assert meta["original_language"] == "en", "TVmaze writes 'English'; the column holds a code"
    assert values(parsed, "title_company", "company", "role") == [("Prime Video", "network")]
    cast = [r for r in parsed.table("credit") if r["role_class"] == "cast"]
    assert len(cast) == 6 and [r["billing_order"] for r in cast] == [0, 1, 2, 3, 4, 5]
    assert cast[0]["character"] == 'James "Jim" Holden'


def test_the_article_splits_into_a_lead_a_plot_and_a_capped_budget_of_craft_prose():
    parsed = parse.parse_document("wikipedia", "article", fixture("wikipedia_article.json"))
    meta = parsed.meta
    assert meta["plot_short"] and meta["plot_short"].startswith("Arrival is a 2016")
    assert meta["plot_full"] and "heptapod" in meta["plot_full"].lower()
    assert meta["homepage"] == "https://en.wikipedia.org/wiki/Arrival_(film)"
    assert meta["year"] is None and meta["runtime_min"] is None
    craft = json.loads(meta["extra"])["craft_sections"]
    assert sorted(craft) == ["filming", "visual effects"]


def test_the_reception_section_is_a_review_and_is_not_also_stored_as_craft_prose():
    """`KEEP_SECTIONS` and `RECEPTION_SECTIONS` must agree,
    or one section is stored as both craft and review."""
    page = fixture("wikipedia_article.json")
    craft = json.loads(parse.parse_document("wikipedia", "article", page).meta["extra"])
    assert "critical response" not in craft["craft_sections"]
    assert "critical response" in craft["section_lengths"], (
        "the fixture no longer carries a reception section"
    )
    got = reviews.parse_document("wikipedia", "article", page)
    assert [r.headline for r in got] == ["Critical Response"]
    assert got[0].author_kind == "critic" and got[0].publication == "Wikipedia"
    assert got[0].external_id == "43991244:critical response"
    assert len(got[0].body.split()) > 50


def test_a_film_with_no_tomatometer_is_not_given_its_neighbours_percentage():
    """The carousel reuses `slot="critics-score"`: scoping
    prevents inventing a score for a film that has none."""
    page = fixture("rt_page_unscored.html")
    parsed = parse.parse_document("rottentomatoes", "page:main", page)
    assert not [r for r in parsed.table("platform_rating") if r["metric"] == "critic_score"]
    assert not [r for r in parsed.table("platform_rating") if r["metric"] == "audience_score"]
    # The percentage an unscoped scan would have reached for, and whose film it is.
    assert parse._RT_SLOT.search(page.decode("utf-8")).group(2) == "100"


def test_the_rt_scorecard_is_read_and_the_related_titles_carousel_is_not():
    parsed = parse.parse_document("rottentomatoes", "page:main", fixture("rt_page.html"))
    assert values(parsed, "platform_rating", "metric", "value", "scale") == [
        ("critic_score", 94.0, 100.0),
        ("audience_score", 83.0, 100.0),
        ("critic_review_count", 441.0, None),
        ("audience_rating_count", 50000.0, None),
    ]
    assert {r["source"] for r in parsed.table("platform_rating")} == {"rottentomatoes"}


def test_the_metacritic_page_takes_the_first_of_each_score_and_not_the_best_of_them():
    """Forty-two score elements, two of them this film's; the carousel's 100s come later."""
    page = fixture("metacritic_page.html")
    assert page.count(b'title="Metascore') >= 3, "the fixture no longer has a carousel to skip"
    parsed = parse.parse_document("metacritic", "page:main", page)
    assert values(parsed, "platform_rating", "metric", "value", "scale") == [
        ("critic_score", 81.0, 100.0),
        ("user_score", 8.2, 10.0),
    ]
    # One row per metric: the key `(title_id, platform, metric)` would raise on a second.
    metrics = [r["metric"] for r in parsed.table("platform_rating")]
    assert len(metrics) == len(set(metrics))


def test_the_two_scales_come_from_the_attribute_rather_than_from_the_reader():
    """The element text is bare digits and the classes are
    Tailwind, so the scale is read from the attribute."""
    parsed = parse.parse_document("metacritic", "page:main", fixture("metacritic_page.html"))
    scales = {r["metric"]: r["scale"] for r in parsed.table("platform_rating")}
    assert scales == {"critic_score": 100.0, "user_score": 10.0}


def test_the_real_collision_is_refused_by_the_cast_the_page_claims():
    """A wrong-film page would pass quote verification, since its quotes ARE in that pack."""
    page = fixture("metacritic_page_alpha_2018.html")
    assert parse.metacritic_page_year(page) == 2018
    ours = {ids.loose_name(n) for n in ("Aaron Pierre", "Golshifteh Farahani")}
    assert not parse.page_belongs_to_title(page, year=2026, people=ours)


def test_a_restoration_is_dated_decades_after_the_film_and_is_still_the_same_film():
    """Metacritic dates `movie/black-orpheus` 2006, the Criterion re-release of a 1959 film."""
    page = fixture("metacritic_page_black_orpheus_2006.html")
    assert parse.metacritic_page_year(page) == 2006
    theirs = parse.page_people(page)
    assert theirs, "the fixture no longer states a cast"
    assert parse.page_belongs_to_title(page, year=1959, people={next(iter(theirs))})


@pytest.mark.parametrize(
    ("ours", "belongs"),
    [(2016, True), (2017, True), (2018, True), (2019, False), (2014, True), (2013, False)],
)
def test_the_year_tolerance_is_two_in_both_directions(ours, belongs):
    """Two years is the corpus's measured tolerance: wider
    re-admits collisions, zero refuses straddling releases."""
    page = fixture("metacritic_page.html")          # states 2016
    assert parse.PAGE_YEAR_TOLERANCE == 2
    assert parse.page_belongs_to_title(page, year=ours, people=set()) is belongs


def test_a_page_that_says_nothing_about_itself_cannot_contradict_us():
    """A challenge page or a moved schema block must not delete a slug that was right."""
    assert parse.page_belongs_to_title(b"<html></html>", year=2026, people={"amyadams"})
    assert parse.page_belongs_to_title(b"", year=None, people=set())


def test_rotten_tomatoes_lets_a_matching_year_vouch_for_a_page_its_dub_cast_does_not():
    """An RT page's cast is the English dub for every anime series, so the year may vouch alone."""
    page = fixture("metacritic_page_alpha_2018.html")
    theirs = {ids.loose_name("Kodi Smit-McPhee")}
    assert theirs & parse.page_people(page), "the fixture no longer names that actor"
    ours = {ids.loose_name("Aaron Pierre")}
    assert not parse.page_belongs_to_title(page, year=2018, people=ours, mode="metacritic")
    assert parse.page_belongs_to_title(page, year=2018, people=ours, mode="rt",
                                       people_decide=False)
    assert not parse.page_belongs_to_title(page, year=2026, people=ours, mode="rt",
                                          people_decide=False)


def test_rt_reads_its_year_from_the_page_title_when_the_schema_block_does_not_state_one():
    """"Alpha (2026)" exists on RT only because "Alpha" is taken, so the parenthesis is worth reading."""
    assert parse.rt_page_year(b"<title>Alpha (2026) | Rotten Tomatoes</title>") == 2026
    assert parse.rt_page_year(b"<title>Arrival | Rotten Tomatoes</title>") is None


def test_rts_schema_block_dates_rts_own_record_and_the_tolerance_is_what_absorbs_it():
    """RT's `dateCreated` dates its own record, a year late for Arrival; the tolerance absorbs it."""
    page = fixture("rt_page.html")
    assert b"<title>Arrival (2016)" in page and b'"dateCreated":"2017-01-27"' in page
    assert parse.rt_page_year(page) == 2017
    assert abs(2017 - 2016) <= parse.PAGE_YEAR_TOLERANCE
    assert parse.page_belongs_to_title(page, year=2016, people=set(), mode="rt")


@pytest.mark.parametrize(
    ("department", "job", "expected"),
    [
        # The seven §3.1 roles, spelled as the corpus's `credit.job` column spells them.
        ("Directing", "Director", "director"),
        ("Writing", "Writer", "writer"),
        ("Writing", "Screenplay", "writer"),
        ("Writing", "Story", "writer"),
        ("Writing", "Teleplay", "writer"),
        ("Writing", "Novel", "writer"),
        ("Editing", "Editor", "editor"),
        ("Camera", "Director of Photography", "dp"),
        ("Sound", "Original Music Composer", "composer"),
        ("Sound", "Composer", "composer"),
        ("Art", "Production Design", "prod_designer"),
        ("Art", "Production Designer", "prod_designer"),
        # Demoted by a qualifier even though the base title matches.
        ("Directing", "Assistant Director", None),
        ("Directing", "Second Unit Director", None),
        ("Camera", "Second Unit Director of Photography", None),
        ("Camera", "Still Photographer", None),
        ("Editing", "Assistant Editor", None),
        ("Sound", "Music Consultant", None),
        # Not one of the seven at all.
        ("Production", "Producer", None),
        ("Costume & Make-Up", "Costume Designer", None),
        ("Directing", "", None),
        ("Directing", None, None),
    ],
)
def test_the_role_vocabulary_is_closed_and_a_qualifier_demotes_a_matching_title(
    department, job, expected
):
    """A role class invented here is a feature coordinate the Cold Tower was never trained on."""
    assert parse.classify_role(department, job) == expected


def test_the_support_list_is_a_second_guard_and_the_closed_map_is_the_operative_one():
    """`_JOB_MAP` matches whole jobs, so `_SUPPORT` is
    unreachable today and guards the day a broader key lands."""
    assert not [(key, token) for key in ids._JOB_MAP for token in ids._SUPPORT if token in key]
    assert parse.classify_role("Camera", "Cinematography") == "dp"
    assert parse.classify_role("Camera", "Additional Cinematography") is None
    # It is consulted before the lookup, so a key it catches can never be classified.
    assert any(token in "additional cinematography" for token in ids._SUPPORT)


def test_a_cast_member_is_classified_by_being_cast_and_not_by_a_job_string():
    assert parse.classify_role(None, None, is_cast=True) == "cast"
    assert parse.classify_role("Acting", "Actor") is None, (
        "'Actor' is not in the job map: a cast credit is declared, not spelled"
    )


@pytest.mark.parametrize(
    ("role_class", "order", "kept"),
    [("cast", 0, True), ("cast", 5, True), ("cast", 6, False), ("cast", None, False),
     ("director", None, True), ("dp", 99, True), (None, 0, False), (None, None, False)],
)
def test_the_billed_cast_cut_is_six_and_a_crew_role_has_no_cut(role_class, order, kept):
    assert parse.keep_credit(role_class, order) is kept


@pytest.mark.parametrize(
    ("written", "spelled"),
    [
        # A Polish l-stroke has no NFKD decomposition, so the filter used to delete it.
        ("Andrzej Sekula", "Andrzej Sekula"),
        ("Micheal MacLiammoir", "Micheal Mac Liammoir"),
        ("A.J. Langer", "A. J. Langer"),
    ],
)
def test_one_human_written_two_ways_compares_equal(written, spelled):
    assert ids.loose_name(written) == ids.loose_name(spelled) != ""


def test_a_name_escaped_by_its_source_is_one_person_and_not_two():
    """TMDB serves `Nyong&apos;o` inside JSON, and upstream escaping sometimes runs twice."""
    assert ids.clean_name("Lupita Nyong&amp;#x27;o") == "Lupita Nyong'o"
    assert ids.clean_name("Gladys Knight &amp; The Pips") == "Gladys Knight & The Pips"
    assert ids.clean_name(None) == ""


@pytest.mark.parametrize(
    ("source", "kind"),
    [("tmdb", "movie_detail"), ("omdb", "detail"), ("trakt", "summary"), ("trakt", "ratings"),
     ("tvmaze", "show"), ("wikipedia", "article"), ("rottentomatoes", "page:main"),
     ("metacritic", "page:main")],
)
@pytest.mark.parametrize(
    "content",
    [
        b"",                                                  # the empty response
        b"{",                                                 # truncated JSON
        b'{"results": [{"id": 1}',                            # truncated mid-array
        b"<html><body><p>We are sorry.</p></body></html>",    # a challenge page
        b"null",
        b"[]",
        b'"a string where an object was"',
        b"\x00\x01\x02not text at all",
    ],
)
def test_a_parser_handed_rubbish_returns_nothing_and_does_not_raise(source, kind, content):
    """The bytes are already stored, so a raise could never
    be retried away; zero rows lets the gate park it."""
    parsed = parse.parse_document(source, kind, content)
    assert parsed.source == parse.parsed_sources()[(source, kind.split(":")[0])]
    assert parsed.rows == {} or all(not table for table in parsed.rows.values())


@pytest.mark.parametrize(
    ("source", "kind"),
    [("tmdb", "movie_detail"), ("trakt", "comments"), ("metacritic", "reviews:critics"),
     ("metacritic", "reviews:users"), ("wikipedia", "article")],
)
@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"<html><body>nope", b"null", b'{"results": null}', b"\xff\xfe\x00bad"],
)
def test_a_review_parser_handed_rubbish_returns_no_reviews_and_does_not_raise(
    source, kind, content
):
    assert reviews.parse_document(source, kind, content) == []


def test_a_document_from_a_crawl_this_parser_predates_is_skipped_rather_than_refused():
    """The raw store is append-only, so it holds kinds older than whatever reads it."""
    assert parse.parse_document("imdb_web", "title:main", b"<html></html>").rows == {}
    assert parse.parse_document("tmdb", "a_kind_that_never_existed", b"{}").rows == {}
    assert reviews.parse_document("letterboxd", "reviews", b"<html></html>") == []


def test_the_dispatch_matches_a_kind_on_the_part_before_the_colon():
    page = fixture("metacritic_page.html")
    assert parse.parse_document("metacritic", "page:main", page).rows
    assert parse.parse_document("metacritic", "page", page).rows
    assert parse.parsed_sources()[("metacritic", "page")] == "metacritic"


def test_an_unknown_target_table_is_a_typo_and_not_a_table_nothing_ever_writes():
    """`emit` takes the table as a string, so a misspelling would be a table nothing writes."""
    collector = parse._Rows("tmdb")
    with pytest.raises(KeyError) as refusal:
        collector.emit("title_metas", year=2016)
    assert "title_metas" in str(refusal.value)


def test_the_metacritic_critic_cards_are_read_without_a_dom_library():
    """No BeautifulSoup or lxml here: a stdlib `HTMLParser`, keyed on `data-testid`."""
    got = reviews.parse_document("metacritic", "reviews:critics",
                                 fixture("metacritic_reviews_critics.html"))
    assert len(got) == 3
    first = got[0]
    assert first.source == "metacritic" and first.author_kind == "critic"
    assert first.publication == "The Telegraph"
    assert first.author == "Robbie Collin"
    assert (first.rating_raw, first.rating_scale, first.rating_norm) == ("100", 100.0, 1.0)
    assert first.created_date == "2016-09-01", "Metacritic writes 'Sep 1, 2016'"
    assert first.url and first.url.startswith("http")
    assert first.body and len(first.body.split()) >= 5
    assert all(r.publication and r.author for r in got)


def test_a_user_card_is_scored_out_of_ten_and_a_critic_card_out_of_a_hundred():
    """The attribute states the denominator; the `view` decides `author_kind`."""
    users = reviews.parse_document("metacritic", "reviews:users",
                                   fixture("metacritic_reviews_users.html"))
    assert len(users) == 3
    assert {r.rating_scale for r in users} == {10.0}
    assert {r.author_kind for r in users} == {"user"}
    assert all(r.publication is None for r in users), "a user is an author, not a publication"
    assert all(0.0 <= r.rating_norm <= 1.0 for r in users)
    critics = reviews.parse_document("metacritic", "reviews:critics",
                                     fixture("metacritic_reviews_critics.html"))
    assert {r.rating_scale for r in critics} == {100.0}
    assert {r.author_kind for r in critics} == {"critic"}, (
        "the page decides who wrote it, and `is_critic` is what the stored row keeps"
    )


def test_a_card_whose_score_carries_no_attribute_falls_back_to_the_pages_own_scale():
    """Older markup has no scale attribute: a critic page read as users would turn 88 into 8.8/10."""
    card = ('<div data-testid="review-card">'
            '<div data-testid="review-card-header" href="/publication/x/">The Paper</div>'
            '<div class="c-siteReviewScore"><span>88</span></div>'
            '<div data-testid="review-quote-text">A sentence about a film.</div></div>')
    as_critic = reviews.parse_metacritic(card.encode(), "reviews:critics")
    as_user = reviews.parse_metacritic(card.encode(), "reviews:users")
    assert (as_critic[0].rating_scale, as_critic[0].rating_norm) == (100.0, 0.88)
    assert (as_user[0].rating_scale, as_user[0].rating_norm) == (10.0, 1.0)


def test_a_non_ascii_review_body_is_stored_exactly_as_it_arrived():
    """§4.1 rule 8: "never 'clean' non-ASCII"."""
    users = reviews.parse_document("metacritic", "reviews:users",
                                   fixture("metacritic_reviews_users.html"))
    foreign = [r for r in users if not r.body.isascii()]
    assert foreign, "the fixture no longer carries a non-ASCII body"
    assert "\ufffd" not in foreign[0].body


def test_the_tmdb_and_trakt_reviews_carry_their_own_ratings_and_their_dates_become_iso():
    # TMDB's reviews arrive appended to the detail call.
    appended = json.dumps({"reviews": json.loads(fixture("tmdb_reviews.json"))}).encode()
    tmdb_reviews = reviews.parse_document("tmdb", "movie_detail", appended)
    assert len(tmdb_reviews) == 3
    assert {r.source for r in tmdb_reviews} == {"tmdb"}
    assert {r.author_kind for r in tmdb_reviews} == {"user"}
    rated = [r for r in tmdb_reviews if r.rating_norm is not None]
    assert rated and all(r.rating_scale == 10.0 for r in rated)
    assert all(len(r.created_date) == 10 for r in tmdb_reviews)
    # An unrated review keeps its body and stores no rating rather than a guessed one.
    assert any(r.rating_raw is None for r in tmdb_reviews)

    trakt = reviews.parse_document("trakt", "comments", fixture("trakt_comments.json"))
    assert len(trakt) == 4 and {r.source for r in trakt} == {"trakt"}
    assert all(r.external_id for r in trakt), "Trakt ids make the fingerprint the source's own"


@pytest.mark.parametrize(
    ("written", "iso"),
    [
        ("Feb 23, 2023", "2023-02-23"),
        ("Sep 1, 2016", "2016-09-01"),
        ("2016-11-11T00:00:00.000Z", "2016-11-11"),
        # The dataset's placeholder for "no date recorded".
        ("1800-01-01", None),
        ("0000-00-00", None),
        ("sometime last autumn", None),
        ("", None),
        (None, None),
    ],
)
def test_a_review_date_is_iso_or_it_is_absent(written, iso):
    assert reviews.iso_date(written) == iso


def test_a_parsed_review_becomes_a_row_through_the_imports_own_column_map():
    """`rating` takes `rating_norm`: the raw column is notation ("8/10", "Rotten")."""
    got = reviews.parse_document("metacritic", "reviews:critics",
                                 fixture("metacritic_reviews_critics.html"))
    row = reviews.review_row(got[0], title_id=1_000_000_007)
    assert set(row) == {"title_id", "source", "author", "url", "rating", "published_at",
                        "is_critic", "body"}
    assert row["title_id"] == 1_000_000_007 and row["source"] == "metacritic"
    assert row["rating"] == got[0].rating_norm, "the normalised score, not the notation"
    assert row["is_critic"] is True
    assert "word_count" not in row, (
        "`word_count` is GENERATED ALWAYS (0003_content.sql:249-250) and decision 335 reads the "
        "gate off it precisely so the count comes from the stored body"
    )


def test_an_unrecognised_author_kind_is_null_rather_than_a_critic():
    """`bool('user')` is True, so a boolean cast would make every review a critic's."""
    row = reviews.review_row(reviews.ParsedReview(body="b", source="s", author_kind="robot"), 1)
    assert row["is_critic"] is None
    assert reviews.review_row(
        reviews.ParsedReview(body="b", source="s", author_kind="user"), 1
    )["is_critic"] is False


def test_a_review_with_no_id_of_its_own_still_has_a_stable_fingerprint():
    """Metacritic's critic and user pages overlap; one review must not be written twice."""
    one = reviews.ParsedReview(body="the same body", source="metacritic", author="A")
    two = reviews.ParsedReview(body="the same body", source="metacritic", author="A")
    other = reviews.ParsedReview(body="the same body", source="metacritic", author="B")
    assert one.fingerprint() == two.fingerprint() != other.fingerprint()
    assert reviews.ParsedReview(body="x", source="s", external_id="42").fingerprint() == "42"


def test_the_markup_scan_reports_the_outermost_match_and_not_the_nested_one():
    """A nested card would otherwise be returned twice."""
    page = ('<div data-testid="review-card">outer'
            '<div data-testid="review-card">inner</div></div>')
    found = reviews.collect(page, lambda _t, a: a.get("data-testid") == "review-card")
    assert len(found) == 1
    assert found[0].text == "outer inner"
    assert "inner" in found[0].html


def test_the_markup_scan_survives_a_stray_end_tag_and_a_truncated_document():
    """A scan that popped unconditionally would close a card on somebody else's end tag."""
    stray = '</span><div data-testid="c">kept</div></section>'
    assert [e.text for e in reviews.collect(stray, lambda _t, a: a.get("data-testid") == "c")] \
        == ["kept"]
    cut = '<div data-testid="c">this response stopped half'
    found = reviews.collect(cut, lambda _t, a: a.get("data-testid") == "c")
    assert [e.text for e in found] == ["this response stopped half"]
    void = '<div data-testid="c">a<br>b<img src="x">c</div><p>after</p>'
    assert [e.text for e in reviews.collect(void, lambda _t, a: a.get("data-testid") == "c")] \
        == ["a b c"]


async def test_an_acquired_person_is_minted_in_the_apps_half_of_the_id_space(db):
    """A person below the floor silently acquires a corpus person's credits."""
    minted = await ids.upsert_person(db, name="Denis Villeneuve", tmdb_id=137427)
    assert minted >= ids.APP_ID_MIN == 1_000_000_000
    assert await db.fetchval("SELECT name FROM person WHERE id = $1", minted) == "Denis Villeneuve"


async def test_minting_the_same_person_twice_returns_one_row(db):
    first = await ids.upsert_person(db, name="Joe Walker", tmdb_id=999565)
    again = await ids.upsert_person(db, name="Joe Walker", tmdb_id=999565)
    by_name = await ids.upsert_person(db, name="Joe Walker")
    assert first == again
    assert by_name != first, (
        "a name match is only consulted for people carrying NEITHER id, or two humans who share "
        "a name would merge - which is the failure `loose_name` exists to avoid from the other side"
    )
    assert await db.fetchval("SELECT count(*) FROM person WHERE name = 'Joe Walker'") == 2


async def test_a_second_document_fills_a_blank_and_never_overwrites_what_is_there(db):
    """Decision 372's fill-never-clobber: a wrong source must not repoint a curated person."""
    person = await ids.upsert_person(db, name="Amy Adams", tmdb_id=9273,
                                     profile_path="https://image.tmdb.org/t/p/w185/a.jpg")
    await ids.upsert_person(db, name="Amy Adams", tmdb_id=9273, imdb_id="nm0010736",
                            profile_path="https://example.invalid/wrong.jpg")
    row = await db.fetchrow("SELECT imdb_id, profile_path FROM person WHERE id = $1", person)
    assert row["imdb_id"] == "nm0010736", "a NULL is filled"
    assert row["profile_path"].endswith("/a.jpg"), "a value already there is kept"


async def test_a_malformed_imdb_id_is_dropped_rather_than_stored(db):
    """A junk imdb id stored here becomes a join key."""
    person = await ids.upsert_person(db, name="Nobody", imdb_id="not-an-id")
    assert await db.fetchval("SELECT imdb_id FROM person WHERE id = $1", person) is None


async def test_the_mint_refuses_to_write_into_the_corpus_half_of_the_id_space(db):
    """A reset sequence or dropped default would otherwise mint below the floor."""
    await db.execute("ALTER TABLE person ALTER COLUMN id SET DEFAULT 42")
    with pytest.raises(RuntimeError) as refusal:
        await ids.upsert_person(db, name="Minted Too Low", tmdb_id=1)
    message = str(refusal.value)
    assert "person 42" in message and "1000000000" in message
    assert "decision 162" in message
    assert message.isascii(), f"the refusal must print on a cp1252 console: {message}"
    assert await db.fetchval("SELECT count(*) FROM person WHERE id = 42") == 0, (
        "the refusal happens inside the transaction, so it leaves no row (decision 162)"
    )


async def test_a_scraped_page_for_the_wrong_film_is_refused_against_this_titles_own_cast(db):
    """Without a per-title cast the refusal falls back to the year alone."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) "
        "VALUES (1000000901, 'movie', 'Alpha', 2026, 'acquired')"
    )
    for name, role in (("Aaron Pierre", "cast"), ("Golshifteh Farahani", "cast"),
                       ("Julia Ducournau", "director"),
                       # `known_people` reads `role_class`, not `job`, so a grip does not vouch for a page.
                       ("Someone Else", None)):
        person = await ids.upsert_person(db, name=name)
        await db.execute(
            "INSERT INTO credit (title_id, person_id, source, job, role_class)"
            " VALUES (1000000901, $1, 'tmdb', 'x', $2)", person, role,
        )

    ours = await ids.known_people(db, 1000000901)
    assert ours == {ids.loose_name(n) for n in
                    ("Aaron Pierre", "Golshifteh Farahani", "Julia Ducournau")}

    page = fixture("metacritic_page_alpha_2018.html")
    assert not parse.page_belongs_to_title(page, year=2026, people=ours), (
        "Metacritic served the 2018 Alpha and every review on it would land on this title"
    )


async def test_a_title_with_no_credits_yet_has_an_empty_cast_and_that_is_an_answer(db):
    """A just-acquired title has no cast; the page's year decides."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) "
        "VALUES (1000000902, 'movie', 'Arrival', 2016, 'acquired')"
    )
    assert await ids.known_people(db, 1000000902) == set()
    page = fixture("metacritic_page.html")
    assert parse.page_belongs_to_title(page, year=2016, people=set())
    assert not parse.page_belongs_to_title(page, year=2022, people=set())
