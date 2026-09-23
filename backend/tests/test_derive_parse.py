"""§8 stage 3's parsers, against responses eight real servers actually sent.

Spec v2.1 §8 stage 3 and §8's preamble (`spec:398`, "All fetched bytes land in the app's own raw
store, so re-parsing is free forever"), §3.1's seven roles, §4.1's id partition; decisions 162,
334, 372, 374, 375.

WHY THE FIXTURES ARE CAPTURES AND NOT HAND-WRITTEN MARKUP. A parser asserted against markup its
own author invented agrees with itself and with nothing else: every one of the failures the
corpus's comments record -- RT's related-titles carousel reusing `slot="critics-score"`,
Metacritic's page carrying forty-two score elements of which two are the film's, OMDb double-
escaping entities inside JSON, IMDb writing "Norse, Old" for Old Norse, Wikidata deprecating a
director it still lists -- is a property of a real response and invisible in a clean one.
`backend/tests/fixtures/sources/README.md` records where each came from and when.

WHAT IS MEASURED HERE, since a parser is easy to test vacuously:

  * Every parser against its captured response, asserting the TARGET TABLE each row lands in and
    the `source` string it is filed under. The source is not decoration: `display.platform_rating`
    is keyed `(title_id, platform, metric)` and OMDb relays three other platforms' scores, so a
    parser that filed them under `omdb` would make OMDb look like a rating platform.
  * `page_belongs_to_title` at its year tolerance in BOTH directions, and against the real
    collision -- the corpus asked Metacritic for Alpha (2026) and got the 2018 film, whose
    `metacritic_slug` is NULL in the corpus to this day because this check refused the page.
  * A parser handed truncated bytes, empty bytes and changed markup returns NOTHING and does not
    raise. Decision 334 makes a source that did not answer a note on the job; a parser that
    raised would turn that note into a stage that fails for ever against bytes already in the raw
    store, which no retry can improve.
  * Every module under `spielplan/derive/` imports no transport, read with `ast`. That guard is
    the whole of "re-parsing is free forever": a parser that can reach the network is a parser
    whose next bug costs another crawl of somebody else's host.

THE DATABASE IS TOUCHED BY EXACTLY TWO FUNCTIONS and they are the two that need it: `upsert_person`
mints into the app's half of the id partition, and `known_people` reads the cast the scraped-page
refusal compares against. Everything else in this file is pure, which is the property `derive/`
exists to have.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from spielplan.derive import ids, parse, reviews

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"
PACKAGE = Path(parse.__file__).resolve().parent


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def rows(parsed: parse.ParsedTitle, table: str) -> list[dict]:
    return [dict(row) for row in parsed.table(table)]


def values(parsed: parse.ParsedTitle, table: str, *fields: str) -> list[tuple]:
    return [tuple(row.get(f) for f in fields) for row in parsed.table(table)]


# --- TMDB ---------------------------------------------------------------------------------------


@pytest.fixture
def tmdb() -> parse.ParsedTitle:
    return parse.parse_document("tmdb", "movie_detail", fixture("tmdb_movie_detail.json"))


def test_the_tmdb_detail_becomes_one_meta_row_in_the_corpus_column_names(tmdb):
    """The payload keys are `title_meta`'s shipped columns, because that IS the payload.

    `importer/meta._meta_rows` builds a bundle row's `payload` out of the corpus's own column
    names, so a derived row spelling `plot` where an imported one spells `plot_full` would be
    invisible to `meta.best` and so to every card the household sees - and invisible to the test
    suite too, because both rows would be well-formed jsonb.
    """
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
    """`release_dates` carries ~40 countries and this household watches in three.

    The fixture keeps FR before US deliberately: FR is not one of the three, so a reader that
    took the first entry's certification would store a French visa number as an age rating.
    """
    payload = json.loads((FIXTURES / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
    markets = [r["iso_3166_1"] for r in payload["release_dates"]["results"]]
    assert markets[0] not in ("US", "GB", "DE"), "the fixture no longer exercises the skip"
    assert tmdb.meta["content_rating"] == "PG-13"


def test_only_the_top_six_billed_cast_survive_and_the_seventh_and_eighth_do_not(tmdb):
    """§3.1, through `keep_credit`. The fixture ships eight so the cut has a boundary."""
    payload = json.loads((FIXTURES / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
    assert len(payload["credits"]["cast"]) == 8, "the fixture no longer straddles the cut"
    cast = [r for r in tmdb.table("credit") if r["role_class"] == "cast"]
    assert [r["billing_order"] for r in cast] == [0, 1, 2, 3, 4, 5]
    assert [r["person"]["name"] for r in cast][:3] == [
        "Amy Adams", "Jeremy Renner", "Forest Whitaker"
    ]
    assert cast[0]["character"] == "Louise Banks" and cast[0]["department"] == "Acting"


def test_the_second_unit_and_the_stills_photographer_are_not_the_films_dp_or_director(tmdb):
    """The measured defect `classify_role` replaces, against two real credits on a real film.

    Matching on "does the job contain 'director'" promoted 18,500 assistant and second-unit
    directors to director and 3,312 still photographers to cinematographer, and each of them then
    carries a director's weight in the graph §3.1 exists to keep the second unit gaffer out of.
    """
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


def test_tmdb_emits_the_primary_language_twice_and_the_derives_key_is_what_dedupes_it(tmdb):
    """A faithful port of a duplicate, held here so the derive cannot be surprised by it.

    TMDB names English once in `spoken_languages` and again in `original_language`, and the
    United States once in `production_countries` and again in `origin_country`. The corpus writes
    every one of these tables through `INSERT OR IGNORE` (`mdc/parse/rebuild.py:40-53`), so the
    duplicate is expected upstream too. The app's keys say the same thing --
    `(title_id, source, language, role)` and `(title_id, source, country)`, `0015_seed.sql`
    section 9 - so the derive needs `ON CONFLICT DO NOTHING` and the first acquisition to reach
    it otherwise dies on a unique violation. Deduplicating in the parser would be the wrong end:
    two sources agreeing is information §4.1 keeps.
    """
    assert values(tmdb, "title_language", "language", "is_primary").count(("en", 1)) == 2
    assert values(tmdb, "title_country", "country").count(("US",)) == 2


# --- OMDb ---------------------------------------------------------------------------------------


@pytest.fixture
def omdb() -> parse.ParsedTitle:
    return parse.parse_document("omdb", "detail", fixture("omdb_detail.json"))


def test_omdb_relays_three_other_platforms_scores_and_files_them_under_their_own_names(omdb):
    """The single most consequential `source` in this module.

    OMDb is a messenger: its `Ratings` block carries IMDb's /10, Rotten Tomatoes' percentage and
    Metacritic's /100. Filed under `omdb` they would make OMDb a rating platform the household
    could weigh, and `display.platform_rating`'s key `(title_id, platform, metric)` would then
    collapse three platforms' critic scores onto one row.
    """
    assert values(omdb, "platform_rating", "source", "metric", "value", "scale") == [
        ("imdb", "user_score", 7.9, 10.0),
        ("rottentomatoes", "critic_score", 94.0, 100.0),
        ("metacritic", "critic_score", 81.0, 100.0),
    ]
    assert omdb.table("platform_rating")[0]["votes"] == 852_057, (
        "`imdbVotes` is '852,057' and belongs to the IMDb row alone"
    )
    assert omdb.source == "omdb"
    assert omdb.row_sources == {"omdb", "imdb", "rottentomatoes", "metacritic"}, (
        "the derive's replace scope is the row sources and not the document's source"
    )


def test_omdbs_prose_fields_become_columns_again(omdb):
    """Runtime is "116 min", BoxOffice is "$100,546,139", Year is "2016"."""
    meta = omdb.meta
    assert (meta["runtime_min"], meta["revenue"], meta["year"]) == (116, 100_546_139, 2016)
    assert meta["content_rating"] == "PG-13"
    assert meta["plot_full"].startswith("Linguistics professor Louise Banks")


def test_a_writers_parenthesised_contribution_is_not_part_of_his_name(omdb):
    """OMDb writes "Eric Heisserer (screenplay)"; `person.name` is the human.

    A name with a parenthesis in it never matches the same human from TMDB, so the graph grows a
    second node for him that no later source ever merges with the first.
    """
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
        # OMDb's own templating slips and drops the full stop; the headline still has to end
        # where the tally begins rather than swallowing it.
        ("Nominated for 1 BAFTA Award1 nomination total",
         [("BAFTA Award", "ceremony", "nominated", 1), ("any award", "total", "nominated", 1)]),
        ("N/A", []),
        (None, []),
    ],
)
def test_the_awards_sentence_is_read_up_to_whichever_comes_first(blurb, expected):
    assert parse.parse_omdb_awards(blurb) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # IMDb inverts a qualified language name and comma-separates it. Splitting on the comma
        # makes "Old" a language, and it is not one.
        ("English, Norse, Old", ["English", "Old Norse"]),
        ("Greek, Ancient (to 1453)", ["Ancient Greek"]),
        # OMDb's literal for a silent film: a statement about the absence of language.
        ("None", []),
        ("English, Mandarin, Russian", ["English", "Mandarin", "Russian"]),
    ],
)
def test_a_qualified_language_name_is_one_language_and_not_two(raw, expected):
    assert parse._omdb_languages(raw) == expected


# --- Trakt / TVmaze / Jellyfin / MPST --------------------------------------------------------


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
    """`display.platform_rating` is keyed `(title_id, platform, metric)` (`0015_seed.sql:210`),
    so a distribution folded into `extra` would be unreadable by the only surface that renders
    this table."""
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


def test_the_jellyfin_item_carries_the_presentation_block_nothing_else_knows():
    """The only source that knows whether tonight's copy is the 4K HDR one or a 720p rip."""
    parsed = parse.parse_document("jellyfin", "items", fixture("jellyfin_item.json"))
    extra = json.loads(parsed.meta["extra"])
    assert (extra["width"], extra["height"]) == (3840, 2160)
    assert extra["video_codec"] == "hevc"
    assert extra["audio_channels"] == max(
        t["ch"] or 0 for t in extra["audio_tracks"]
    ), "the headline audio track is the one with the most channels"
    assert parsed.meta["runtime_min"] == 98, "RunTimeTicks are 100ns units"


def test_a_jellyfin_crew_members_job_is_not_stored_as_the_character_he_played():
    """Jellyfin's `Role` is the character for an actor and the JOB for crew, and passing it
    through unconditionally filed "Director" and "Screenplay" as character names on 2,029
    credits."""
    payload = json.loads((FIXTURES / "jellyfin_item.json").read_text(encoding="utf-8"))
    assert any((p.get("Type") or "").lower() != "actor" for p in payload["People"]), (
        "the fixture no longer carries a crew entry"
    )
    parsed = parse.parse_document("jellyfin", "items", fixture("jellyfin_item.json"))
    for row in parsed.table("credit"):
        if row["role_class"] != "cast":
            assert row["character"] is None


def test_mpst_is_a_full_retelling_and_sits_last_in_the_source_order():
    """Its synopsis gives away the ending, which is why `SOURCE_PRIORITY` puts mpst eleventh
    (`importer/meta.py:33-37`) rather than why it is excluded."""
    parsed = parse.parse_document("mpst_bulk", "meta", fixture("mpst_meta.json"))
    assert parsed.meta["plot_full"].startswith("The film starts with the voice of Dr. Louise")
    assert parsed.meta["year"] is None, "MPST knows a plot and a tag set and nothing else"
    assert values(parsed, "title_keyword", "keyword", "source") == [("flashback", "mpst")]
    assert json.loads(parsed.meta["extra"])["synopsis_source"] == "imdb"


# --- Wikipedia / Wikidata ----------------------------------------------------------------------


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
    """The two Wikipedia parsers divide one article and must agree about where a section starts.

    `KEEP_SECTIONS` deliberately excludes reception prose - "Reception sections are handled
    separately as critic reviews and are excluded here to avoid storing them twice" - and
    `RECEPTION_SECTIONS` is the other half. If they disagreed, the same 3,000 characters would be
    both a craft block the pack reads as production detail and a review the gate counts.
    """
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


@pytest.fixture
def wikidata() -> parse.ParsedTitle:
    blob = json.loads((FIXTURES / "wikidata_entity.json").read_text(encoding="utf-8"))
    return parse.parse_wikidata_entity(blob["entity"], blob["labels"])


def test_wikidata_resolves_its_q_ids_through_the_label_lookup_and_drops_what_it_cannot(wikidata):
    """An absent label is a credit NOT emitted rather than a credit named "Q193570"."""
    blob = json.loads((FIXTURES / "wikidata_entity.json").read_text(encoding="utf-8"))
    assert values(wikidata, "credit", "job", "role_class")[:2] == [
        ("Director", "director"), ("Screenplay", "writer"),
    ]
    assert wikidata.table("credit")[0]["person"]["name"] == "Denis Villeneuve"
    without_labels = parse.parse_wikidata_entity(blob["entity"], {})
    assert without_labels.table("credit") == ()
    assert without_labels.table("title_genre") == ()


def test_a_wikidata_award_carries_the_year_its_point_in_time_qualifier_states(wikidata):
    """P585 is what makes an award row answerable by year; the mainsnak says only THAT it won."""
    won = [r for r in wikidata.table("award") if r["result"] == "won"]
    assert ("Academy Award for Best Sound Editing", 2017) in [
        (r["award"], r["year"]) for r in won
    ]
    assert any(r["year"] is None for r in wikidata.table("award")), (
        "not every statement carries P585, and a missing year is NULL rather than a guess"
    )
    assert {r["category"] for r in wikidata.table("award")} == {"award"}


def test_the_money_wikidata_states_is_the_largest_of_its_statements(wikidata):
    """P2130 and P2142 are multi-valued - several sources, several currencies, several years."""
    assert wikidata.meta["budget"] == 50_000_000
    assert wikidata.meta["revenue"] == 203_388_186
    assert wikidata.meta["plot_full"] is None, "Wikidata states facts, not prose"


@pytest.mark.parametrize(
    ("name", "collective"),
    [
        ("the Wachowskis", False),
        ("Coen brothers", True),
        ("Ben Davis and Camille Griffin", True),
        ("Zucker, Abrahams and Zucker", True),
        # Hyphenated pen-names for pairs are deliberately NOT caught: real surnames and given
        # names hyphenate too, and a rule that fired on those would cost far more than it saves.
        ("Salim-Javed", False),
        ("Hou Hsiao-hsien", False),
        ("Jean-Pierre Jeunet", False),
        ("", False),
    ],
)
def test_a_duo_is_not_a_human_and_must_not_become_a_graph_node(name, collective):
    assert parse.is_collective(name) is collective


def test_a_deprecated_statement_is_not_a_credit():
    """142 deprecated statements over six credit properties put Sam Mendes and Danny Boyle on
    *No Time to Die*. Wikidata says "not really" with a rank and with two qualifiers."""
    entity = {"claims": {"P57": [
        {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q1"}}}},
        {"rank": "deprecated", "mainsnak": {"datavalue": {"value": {"id": "Q2"}}}},
        {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q3"}}},
         "qualifiers": {"P3831": [{}]}},
        {"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q4"}}},
         "qualifiers": {"P582": [{}]}},
    ]}}
    labels = {"Q1": "Kept", "Q2": "Deprecated", "Q3": "Dubbing Director", "Q4": "Left Early"}
    parsed = parse.parse_wikidata_entity(entity, labels)
    assert [r["person"]["name"] for r in parsed.table("credit")] == ["Kept"]


# --- the two scraped pages ----------------------------------------------------------------------


def test_a_film_with_no_tomatometer_is_not_given_its_neighbours_percentage():
    """What `<media-scorecard>` scoping actually buys, measured rather than assumed.

    The corpus says the same `slot="critics-score"` markup is reused by the related-titles
    carousel, "so an unscoped scan picks up a neighbouring film's percentage instead of this
    one's". Over 200 captured RT pages the two readings agree on 189 and differ on 11 - and
    every one of the 11 is this shape: the film's OWN scorecard carries no percentage while the
    carousel below it does. So the scoping does not correct a score, it prevents one being
    invented for a film that has none, which is the worse failure of the two and the one a
    household would act on.

    `rt_page_unscored.html` is one of the eleven. Scoped it yields no critic score; unscoped the
    first percentage on the page is a neighbouring film's 100%.
    """
    page = fixture("rt_page_unscored.html")
    parsed = parse.parse_document("rottentomatoes", "page:main", page)
    assert not [r for r in parsed.table("platform_rating") if r["metric"] == "critic_score"]
    assert not [r for r in parsed.table("platform_rating") if r["metric"] == "audience_score"]
    # The percentage an unscoped scan would have reached for, and whose film it is.
    assert parse._RT_SLOT.search(page.decode("utf-8")).group(2) == "100"


def test_the_rt_scorecard_is_read_and_the_related_titles_carousel_is_not():
    """The ordinary page, where the scorecard is where the film's own two percentages live."""
    parsed = parse.parse_document("rottentomatoes", "page:main", fixture("rt_page.html"))
    assert values(parsed, "platform_rating", "metric", "value", "scale") == [
        ("critic_score", 94.0, 100.0),
        ("audience_score", 83.0, 100.0),
        ("critic_review_count", 441.0, None),
        ("audience_rating_count", 50000.0, None),
    ]
    assert {r["source"] for r in parsed.table("platform_rating")} == {"rottentomatoes"}


def test_the_metacritic_page_takes_the_first_of_each_score_and_not_the_best_of_them():
    """A real capture carries forty-two score elements and two of them are this film's.

    The fixture keeps six in page order: 81/100, 8.2/10, a `null` user score, 81 again, and two
    100s from the "best movies" carousel. Without "the first occurrence is the page-level score"
    this parser reports a Metascore of 100 for every film on Metacritic with a carousel.
    """
    page = fixture("metacritic_page.html")
    assert page.count(b'title="Metascore') >= 3, "the fixture no longer has a carousel to skip"
    parsed = parse.parse_document("metacritic", "page:main", page)
    assert values(parsed, "platform_rating", "metric", "value", "scale") == [
        ("critic_score", 81.0, 100.0),
        ("user_score", 8.2, 10.0),
    ]
    # ONE ROW PER METRIC is the half that outlives this fixture's ordering. "Skip a metric
    # already seen" and "stop once both are in" are two halves of one rule, and only the second
    # is exercised by a page whose own two scores happen to come first; a page listing two
    # Metascores before its user score needs the first, and `display.platform_rating` is keyed
    # `(title_id, platform, metric)` so the extra row would be a unique violation.
    metrics = [r["metric"] for r in parsed.table("platform_rating")]
    assert len(metrics) == len(set(metrics))


def test_the_two_scales_come_from_the_attribute_rather_than_from_the_reader():
    """"Metascore 81 out of 100" and "User score 8.2 out of 10" differ by a factor of ten, and
    the element text is just the digits. A reader taking the digits would have to know which
    element it was looking at, and the class names around them are Tailwind."""
    parsed = parse.parse_document("metacritic", "page:main", fixture("metacritic_page.html"))
    scales = {r["metric"]: r["scale"] for r in parsed.table("platform_rating")}
    assert scales == {"critic_score": 100.0, "user_score": 10.0}


def test_the_letterboxd_page_yields_nothing_because_its_ld_json_sits_in_a_cdata_wrapper():
    """A measurement, not an oversight, and the reason decision 374 costs nothing.

    Letterboxd wraps its `ld+json` block in `/* <![CDATA[ */ ... /* ]]> */`, which
    `sources/_htmlutil.ld_json` cannot decode - so this parser yields nothing, from this capture
    and from every other. The corpus's own database is the proof: zero `platform_rating` rows and
    zero `title_meta` rows under `letterboxd` against 9,909 under `tmdb`. The parser is ported
    faithfully, defect included, because decision 374 does not port the crawler either; a repair
    belongs to whoever decides to crawl the source, and this test holds the behaviour that exists
    rather than the one that was intended - so a future repair to `ld_json` turns it red and is
    read by someone.
    """
    page = fixture("letterboxd_film_page.html")
    assert b"aggregateRating" in page and b"CDATA" in page
    parsed = parse.parse_document("letterboxd", "film:page", page)
    assert parsed.rows == {} and parsed.source == "letterboxd"
    # The logic is right; the wrapper is what defeats it.
    unwrapped = page.replace(b"/* <![CDATA[ */", b"").replace(b"/* ]]> */", b"")
    assert values(parse.parse_letterboxd_page(unwrapped), "platform_rating", "metric", "scale") \
        == [("user_score", 5.0)]


# --- did the slug land on the right film? -------------------------------------------------------


def test_the_real_collision_is_refused_by_the_cast_the_page_claims():
    """The corpus asked Metacritic for `movie/alpha` on behalf of Alpha (2026) and got the 2018
    film. Its `metacritic_slug` is NULL in the corpus to this day because of this check.

    Without it every review on that page lands on the wrong `title_id`, describing a different
    film entirely - which no dedup or length filter can catch, and which §8 stage 7's quote
    verification passes, because a quote from the wrong film's pack IS a substring of that pack.
    """
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
    """Festival versus general release, US versus original market. Two years is the corpus's
    measured tolerance: widening it re-admits the same-name collision it exists to catch and
    narrowing it to zero refuses every film whose premiere and release straddle a new year."""
    page = fixture("metacritic_page.html")          # states 2016
    assert parse.PAGE_YEAR_TOLERANCE == 2
    assert parse.page_belongs_to_title(page, year=ours, people=set()) is belongs


def test_a_page_that_says_nothing_about_itself_cannot_contradict_us():
    """Absence of evidence is not evidence: a challenge page or a redesign that moved the
    schema.org block must not silently delete a slug that was right."""
    assert parse.page_belongs_to_title(b"<html></html>", year=2026, people={"amyadams"})
    assert parse.page_belongs_to_title(b"", year=None, people=set())


def test_rotten_tomatoes_lets_a_matching_year_vouch_for_a_page_its_dub_cast_does_not():
    """`people_decide` is set by what the page CARRIES. A Metacritic page brings review text, so
    a disjoint cast is enough to refuse it; an RT page brings two percentages, and its cast list
    is the English dub for every anime series this household holds."""
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
    """"Alpha (2026)" exists on RT only because "Alpha" is already taken - which is exactly the
    case a guessed slug gets wrong, so the parenthesis is worth reading."""
    assert parse.rt_page_year(b"<title>Alpha (2026) | Rotten Tomatoes</title>") == 2026
    assert parse.rt_page_year(b"<title>Arrival | Rotten Tomatoes</title>") is None


def test_rts_schema_block_dates_rts_own_record_and_the_tolerance_is_what_absorbs_it():
    """A measured reason the tolerance is two rather than zero, found in the captured page.

    RT's `ld+json` for Arrival states `dateCreated: 2017-01-27` - when RT created the record -
    while its `<title>` states the film's year, "Arrival (2016)". `_ld_year` prefers the schema
    block, as the corpus wrote it, so `rt_page_year` runs a year ahead of the film for a page RT
    catalogued late. One year is inside `PAGE_YEAR_TOLERANCE` and the page is correctly kept; a
    tolerance of zero would refuse the right page for every film RT filed after New Year.
    """
    page = fixture("rt_page.html")
    assert b"<title>Arrival (2016)" in page and b'"dateCreated":"2017-01-27"' in page
    assert parse.rt_page_year(page) == 2017
    assert abs(2017 - 2016) <= parse.PAGE_YEAR_TOLERANCE
    assert parse.page_belongs_to_title(page, year=2016, people=set(), mode="rt")


# --- classify_role and keep_credit --------------------------------------------------------------


@pytest.mark.parametrize(
    ("department", "job", "expected"),
    [
        # The seven §3.1 roles, spelled as the corpus's own `credit.job` column spells them: the
        # eight most common kept jobs across 281,000 rows.
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
    """`credit.role_class` is what "the feature contract's `p:<role_class>:<name>` grammar is
    built from" (`0015_seed.sql:50-53`), so a value invented here is a coordinate the Cold Tower
    was never trained on."""
    assert parse.classify_role(department, job) == expected


def test_the_support_list_is_a_second_guard_and_the_closed_map_is_the_operative_one():
    """Measured, because the two look like one rule and only one of them is load-bearing today.

    `_JOB_MAP` matches the WHOLE lowercased job, so "Second Unit Director of Photography" misses
    it for the same reason "Grip" does, and no key of the map contains any `_SUPPORT` token -
    which makes the qualifier list unreachable on today's vocabulary. It is ported anyway, and
    this test is how a later reader can tell the difference: the map is what the corpus GROWS
    (it gained "co-director" and "screenstory" over the crawl), and the day a broader key arrives
    the qualifier list is what keeps the stills photographer out of it. Deleting it as dead code
    would delete the guard on exactly the change that needs it.
    """
    assert not [(key, token) for key in ids._JOB_MAP for token in ids._SUPPORT if token in key]
    # Both of these are already decided by the map alone - "additional cinematography" is not a
    # key - which is the measurement, stated as code so it cannot quietly stop being true.
    assert parse.classify_role("Camera", "Cinematography") == "dp"
    assert parse.classify_role("Camera", "Additional Cinematography") is None
    # What the list WOULD do if the map ever answered first: it is consulted before the lookup,
    # so a key it catches can never be classified however the map grows.
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
        # A Polish l-stroke has no NFKD decomposition, so stripping combining marks leaves it
        # and the `[^a-z0-9]` filter then deletes it: "andrzejseku" never matched "andrzejsekula".
        ("Andrzej Sekula", "Andrzej Sekula"),
        ("Micheal MacLiammoir", "Micheal Mac Liammoir"),
        ("A.J. Langer", "A. J. Langer"),
    ],
)
def test_one_human_written_two_ways_compares_equal(written, spelled):
    assert ids.loose_name(written) == ids.loose_name(spelled) != ""


def test_a_name_escaped_by_its_source_is_one_person_and_not_two():
    """TMDB serves `Lupita Nyong&apos;o` inside JSON, where nothing else decodes entities, and
    an escaping pass upstream sometimes runs twice."""
    assert ids.clean_name("Lupita Nyong&amp;#x27;o") == "Lupita Nyong'o"
    assert ids.clean_name("Gladys Knight &amp; The Pips") == "Gladys Knight & The Pips"
    assert ids.clean_name(None) == ""


# --- a source that changed its markup degrades to zero rows, never to a failed stage ------------


@pytest.mark.parametrize(
    ("source", "kind"),
    [("tmdb", "movie_detail"), ("omdb", "detail"), ("trakt", "summary"), ("trakt", "ratings"),
     ("tvmaze", "show"), ("wikipedia", "article"), ("mpst_bulk", "meta"), ("jellyfin", "items"),
     ("rottentomatoes", "page:main"), ("metacritic", "page:main"), ("letterboxd", "film:page")],
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
    """Decision 334 makes a source that did not answer a NOTE on the job, and decision 336 keeps
    `failed` for "this stage raised and will raise again".

    A document is already in the raw store by the time a parser sees it, so a retry cannot
    improve it: a parser that raised on changed markup would convert a note into a stage that
    fails for ever until someone edits `derive/parse.py`. Zero rows is the honest answer, stage
    4's gate then finds the title thin, and it parks with a deadline - which is the right state
    for a title waiting on a parser fix, and one the board can show.
    """
    parsed = parse.parse_document(source, kind, content)
    assert parsed.source == parse.parsed_sources()[(source, kind.split(":")[0])]
    assert parsed.rows == {} or all(not table for table in parsed.rows.values())


@pytest.mark.parametrize(
    ("source", "kind"),
    [("tmdb", "reviews"), ("trakt", "comments"), ("metacritic", "reviews:critics"),
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
    """The raw store is append-only, so it holds kinds older than whatever is reading it."""
    assert parse.parse_document("imdb_web", "title:main", b"<html></html>").rows == {}
    assert parse.parse_document("tmdb", "a_kind_that_never_existed", b"{}").rows == {}
    assert reviews.parse_document("letterboxd", "reviews", b"<html></html>") == []


def test_the_dispatch_matches_a_kind_on_the_part_before_the_colon():
    """The store records `page:main` and `reviews:critics`; only the head chooses a parser."""
    page = fixture("metacritic_page.html")
    assert parse.parse_document("metacritic", "page:main", page).rows
    assert parse.parse_document("metacritic", "page", page).rows
    assert parse.parsed_sources()[("metacritic", "page")] == "metacritic"
    # The one entry whose store source and row source differ: the raw store records what was
    # crawled and `title_meta` records who said it. Decision 375's delete scope is the second.
    assert parse.parsed_sources()[("mpst_bulk", "meta")] == "mpst"


def test_an_unknown_target_table_is_a_typo_and_not_a_table_nothing_ever_writes():
    """`emit` takes the table as a string, exactly as the corpus's sink does, so a misspelling
    would otherwise be a table the derive silently never writes and no test would name."""
    collector = parse._Rows("tmdb")
    with pytest.raises(KeyError) as refusal:
        collector.emit("title_metas", year=2016)
    assert "title_metas" in str(refusal.value)


# --- the review parsers -------------------------------------------------------------------------


def test_the_metacritic_critic_cards_are_read_without_a_dom_library():
    """`_mc_from_html` is the one parser in either project that needed BeautifulSoup, and this
    app declares neither `beautifulsoup4` nor `lxml`. The selectors are ported; the mechanism is
    a stdlib `HTMLParser` scan. `data-testid` stays the primary key into the card because the
    Tailwind class names around it change constantly."""
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
    """The scale is the attribute's, and that is a stronger answer than the view's.

    "Metascore 100 out of 100" and "User score 10 out of 10" each state their own denominator,
    so a page read under the wrong `view` still stores the right scale. The `view` decides
    `author_kind` - which is `is_critic` in the stored row - and it decides the scale ONLY for a
    card whose score element carries no such attribute, which is the fallback below.
    """
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
    """The only place `view` decides a number, and the reason it is passed in at all.

    Metacritic's older markup puts the score in the element text with the scale nowhere on the
    card, so a critic page parsed as a user page would turn a Metascore of 88 into a rating of
    8.8-out-of-10 - which `rating_norm` stores as 1.0 and no stored row can be read back as
    wrong.
    """
    card = ('<div data-testid="review-card">'
            '<div data-testid="review-card-header" href="/publication/x/">The Paper</div>'
            '<div class="c-siteReviewScore"><span>88</span></div>'
            '<div data-testid="review-quote-text">A sentence about a film.</div></div>')
    as_critic = reviews.parse_metacritic(card.encode(), "reviews:critics")
    as_user = reviews.parse_metacritic(card.encode(), "reviews:users")
    assert (as_critic[0].rating_scale, as_critic[0].rating_norm) == (100.0, 0.88)
    assert (as_user[0].rating_scale, as_user[0].rating_norm) == (10.0, 1.0)


def test_a_non_ascii_review_body_is_stored_exactly_as_it_arrived():
    """§4.1 rule 8: "never 'clean' non-ASCII". One of the captured user reviews is Turkish."""
    users = reviews.parse_document("metacritic", "reviews:users",
                                   fixture("metacritic_reviews_users.html"))
    foreign = [r for r in users if not r.body.isascii()]
    assert foreign, "the fixture no longer carries a non-ASCII body"
    assert "\ufffd" not in foreign[0].body


def test_the_tmdb_and_trakt_reviews_carry_their_own_ratings_and_their_dates_become_iso():
    tmdb_reviews = reviews.parse_document("tmdb", "reviews", fixture("tmdb_reviews.json"))
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
        # The source dataset's placeholder for "no date recorded", 200 years before cinema.
        ("1800-01-01", None),
        ("0000-00-00", None),
        ("sometime last autumn", None),
        ("", None),
        (None, None),
    ],
)
def test_a_review_date_is_iso_or_it_is_absent(written, iso):
    """A date column that sometimes holds prose is worse than one that is honestly empty."""
    assert reviews.iso_date(written) == iso


def test_a_parsed_review_becomes_a_row_through_the_imports_own_column_map():
    """`REVIEW_SOURCE` carries the argument for the one non-obvious pairing: `rating` takes
    `rating_norm` and not `rating_raw`, because the raw column is the review's own notation
    ("8/10", "Rotten") and the target is `double precision`."""
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
    """Metacritic's critic and user pages overlap, and a derive must not write one review twice."""
    one = reviews.ParsedReview(body="the same body", source="metacritic", author="A")
    two = reviews.ParsedReview(body="the same body", source="metacritic", author="A")
    other = reviews.ParsedReview(body="the same body", source="metacritic", author="B")
    assert one.fingerprint() == two.fingerprint() != other.fingerprint()
    assert reviews.ParsedReview(body="x", source="s", external_id="42").fingerprint() == "42"


def test_the_markup_scan_reports_the_outermost_match_and_not_the_nested_one():
    """A review card inside a review card would otherwise be returned twice - once on its own and
    once inside its parent - which is how one review becomes two."""
    page = ('<div data-testid="review-card">outer'
            '<div data-testid="review-card">inner</div></div>')
    found = reviews.collect(page, lambda _t, a: a.get("data-testid") == "review-card")
    assert len(found) == 1
    assert found[0].text == "outer inner"
    assert "inner" in found[0].html


def test_the_markup_scan_survives_a_stray_end_tag_and_a_truncated_document():
    """Metacritic's markup carries unclosed template markers and the occasional stray `</div>`,
    and a response can simply stop: a scan that popped unconditionally would close a card on
    somebody else's end tag and lose the rest of the page."""
    stray = '</span><div data-testid="c">kept</div></section>'
    assert [e.text for e in reviews.collect(stray, lambda _t, a: a.get("data-testid") == "c")] \
        == ["kept"]
    cut = '<div data-testid="c">this response stopped half'
    found = reviews.collect(cut, lambda _t, a: a.get("data-testid") == "c")
    assert [e.text for e in found] == ["this response stopped half"]
    void = '<div data-testid="c">a<br>b<img src="x">c</div><p>after</p>'
    assert [e.text for e in reviews.collect(void, lambda _t, a: a.get("data-testid") == "c")] \
        == ["a b c"]


# --- the two functions that need a database -----------------------------------------------------


async def test_an_acquired_person_is_minted_in_the_apps_half_of_the_id_space(db):
    """`0015_seed.sql:20-23`: "A disjoint range makes the collision arithmetically impossible
    instead of contingent on the corpus standing still."

    It is worth more for a person than for a title. A bundle title colliding with an app one is
    refused at import with the id named (`importer/bundle.py:539`); a PERSON below the floor
    silently acquires the credits of whoever held that id in the corpus, and `credit.person_id`
    is a foreign key that will happily point at them.
    """
    minted = await ids.upsert_person(db, name="Denis Villeneuve", tmdb_id=137427)
    assert minted >= ids.APP_ID_MIN == 1_000_000_000
    assert await db.fetchval("SELECT name FROM person WHERE id = $1", minted) == "Denis Villeneuve"


async def test_minting_the_same_person_twice_returns_one_row(db):
    """Two documents of one title name the same director, and so do two derives of it."""
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
    """Decision 372's fill-never-clobber, one table over. A source that is wrong about an imdb id
    must not be able to repoint a person the bundle curated."""
    person = await ids.upsert_person(db, name="Amy Adams", tmdb_id=9273,
                                     profile_path="https://image.tmdb.org/t/p/w185/a.jpg")
    await ids.upsert_person(db, name="Amy Adams", tmdb_id=9273, imdb_id="nm0010736",
                            profile_path="https://example.invalid/wrong.jpg")
    row = await db.fetchrow("SELECT imdb_id, profile_path FROM person WHERE id = $1", person)
    assert row["imdb_id"] == "nm0010736", "a NULL is filled"
    assert row["profile_path"].endswith("/a.jpg"), "a value already there is kept"


async def test_a_malformed_imdb_id_is_dropped_rather_than_stored(db):
    """`nm` plus five to ten digits. Jellyfin's ProviderIds are whatever a scraper guessed
    (`connectors/resolve.py:15-17`), and a junk id stored here becomes a join key."""
    person = await ids.upsert_person(db, name="Nobody", imdb_id="not-an-id")
    assert await db.fetchval("SELECT imdb_id FROM person WHERE id = $1", person) is None


async def test_the_mint_refuses_to_write_into_the_corpus_half_of_the_id_space(db):
    """The backstop `0015_seed.sql:39-44` names by hand: a sequence someone reset, or a default
    someone dropped in a repair, would otherwise mint quietly below the floor."""
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
    """The integration half of the wrong-page refusal, and the shape stage 3 applies it in.

    `known_people` is the corpus's own query (`mdc/sources/metacritic.py:46-51`,
    `mdc/parse/rebuild.py:559-565`) scoped to one title, and it is what supplies
    `page_belongs_to_title`'s `people`. Without a per-title read the refusal has no second signal
    at all and falls back to the year, which clears a same-name same-year collision.
    """
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) "
        "VALUES (1000000901, 'movie', 'Alpha', 2026, 'acquired')"
    )
    for name, role in (("Aaron Pierre", "cast"), ("Golshifteh Farahani", "cast"),
                       ("Julia Ducournau", "director"),
                       # A support credit this title also carries. `known_people` reads
                       # `role_class` and not `job`, so a grip does not vouch for a page.
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
    """A title acquired minutes ago has nothing to compare with, and `page_belongs_to_title` is
    written for that case: the page's year decides instead."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) "
        "VALUES (1000000902, 'movie', 'Arrival', 2016, 'acquired')"
    )
    assert await ids.known_people(db, 1000000902) == set()
    page = fixture("metacritic_page.html")
    assert parse.page_belongs_to_title(page, year=2016, people=set())
    assert not parse.page_belongs_to_title(page, year=2022, people=set())


# --- the guard that makes "re-parsing is free forever" true --------------------------------------

# Every spelling of a way to open a socket, and the two the corpus's own layering forbids. §8's
# preamble promises that fetched bytes land in the raw store "so re-parsing is free forever", and
# that promise is exactly this list being absent from this package.
TRANSPORT = ("httpx", "requests", "urllib.request", "urllib3", "http.client", "socket", "aiohttp",
             "spielplan.acquire.fetch", "spielplan.connectors")


def _absolute(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    prefix = ".".join(parts[: len(parts) - node.level + 1])
    return f"{prefix}.{node.module}" if node.module else prefix


def _imported_modules(source: str, *, package: str = "spielplan.derive") -> set[str]:
    """Every module `source` imports, plus each `from x import y` recorded as `x.y` as well.

    The second half is what `test_layering_guards.py`'s helper cannot do and names as a known
    limit: `from spielplan.acquire import fetch` records only `spielplan.acquire` there, and for
    a forbidden list of module names that spelling is the likeliest violation.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names if module)
    return modules


def _transport(source: str) -> list[str]:
    return sorted(
        m for m in _imported_modules(source)
        if any(m == bad or m.startswith(f"{bad}.") for bad in TRANSPORT)
    )


def _reaches_for_the_fetcher(source: str) -> bool:
    """Whether `source` touches a `.fetcher` attribute. `test_sources_adapters.py`'s helper.

    THE ONE WAY OUT OF THIS PACKAGE THAT NEEDS NO IMPORT, and the list above cannot see it.
    Decision 373 hands the fetcher to every stage on `StageContext.fetcher`, set at stage 2 and
    never cleared, so the context stage 3 is called with still carries a live, polite client:
    `await ctx.fetcher.get(url)` in a helper here would leave the process from behind the resume
    point with every import guard green. Nothing under `derive/` takes a context today, which is
    exactly why the rule costs nothing to hold now. AST and not a substring, because these modules
    argue about the fetcher in prose. [decision 373; M5.3 review cycle 2, M53-C2-NET-04]
    """
    return any(
        isinstance(node, ast.Attribute) and node.attr == "fetcher"
        for node in ast.walk(ast.parse(source))
    )


def test_no_module_in_the_derive_package_can_reach_the_network():
    """The invariant every other test in this file rests on.

    `mdc/parse/titles.py` imports `ids` and `_htmlutil` and no transport at all, and that is the
    whole of §8's "All fetched bytes land in the app's own raw store, so re-parsing is free
    forever" (`spec:398`). A parser that can fetch is a parser whose next bug costs another crawl
    of somebody else's host, on a household's IP address, against eight rate limits - and it is a
    parser whose tests need a network to run.

    The whole package rather than a named list, so a module a later phase of this milestone adds
    is guarded on the day it lands rather than on the day someone remembers to name it here.
    """
    modules = sorted(PACKAGE.rglob("*.py"))
    assert len(modules) >= 4, f"the guard found almost nothing to read in {PACKAGE}"
    for path in modules:
        source = path.read_text(encoding="utf-8")
        offenders = _transport(source)
        assert not offenders, f"derive/{path.name} reaches for transport: {offenders}"
        assert not _reaches_for_the_fetcher(source), (
            f"derive/{path.name} reaches for `.fetcher`: stage 3 re-reads the raw store, and a "
            "request made through the context's fetcher needs no import at all (decision 373)"
        )


def test_the_transport_guard_can_report_every_way_in():
    """A guard that cannot report a violation is a green line rather than a proof."""
    for illegal in (
        "import httpx\n",
        "from httpx import AsyncClient\n",
        "import requests\n",
        "from urllib.request import urlopen\n",
        "from spielplan.acquire import fetch\n",
        "from spielplan.acquire.fetch import Fetcher\n",
        "from spielplan.acquire import fetch as f\n",
        "import socket\n",
        "from spielplan.connectors import jellyfin\n",
    ):
        assert _transport(illegal), f"the guard missed: {illegal.strip()}"
    # And the imports this package legitimately makes are not reported.
    assert not _transport(
        "import json\nfrom spielplan.sources._htmlutil import clean_text\n"
        "from spielplan.importer.reviews import REVIEW_SOURCE\nimport asyncpg\n"
    )
    # The import-free way in, and the prose every module here writes about it.
    assert _reaches_for_the_fetcher("async def f(ctx):\n    return await ctx.fetcher.get('u')\n")
    assert _reaches_for_the_fetcher("def f(ctx):\n    client = ctx.fetcher\n")
    assert not _reaches_for_the_fetcher('"""The fetcher is handed to stage 2."""\nx = 1\n')


def test_the_parsers_import_the_two_modules_the_corpus_imports_and_little_else():
    """`mdc/parse/titles.py`'s import list IS the contract, not just an accident of it."""
    imported = _imported_modules((PACKAGE / "parse.py").read_text(encoding="utf-8"))
    assert "spielplan.sources._htmlutil" in imported
    assert "spielplan.derive.ids" in imported
    assert not any(m.startswith("spielplan.") for m in imported
                   if m not in {"spielplan.sources._htmlutil", "spielplan.derive.ids"}
                   and not m.startswith("spielplan.sources._htmlutil.")
                   and not m.startswith("spielplan.derive.ids.")), sorted(imported)
    assert "asyncpg" not in imported, "a parser that holds a connection cannot be re-run cheaply"
