"""§8 stage 3's derive against a real Postgres and raw store (§14 risk 5). "The same rows" means every table
read without its surrogate id and ordered by it. Fixtures are real captures. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import gzip
import json
import socket
from pathlib import Path

import pytest

from spielplan.acquire import rawstore
from spielplan.core.config import settings
from spielplan.derive import ids, parse, rebuild

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"

# Ids in the app's half of the partition, written out: this file is about the derive, not the mint.
ARRIVAL = 1_000_000_701
NEIGHBOUR = 1_000_000_702
SPARSE = 1_000_000_703

# `entity_key` is the TASK's key, never the title id, so a derive must find documents through the task.
ARRIVAL_KEY = "jellyfin:arrival-4k"
NEIGHBOUR_KEY = "jellyfin:heat-hd"
SPARSE_KEY = "title:sparse"

# The `title` row too: a derive that moved a neighbour's card would otherwise pass.
_SNAPSHOT: dict[str, str] = {
    "title": "SELECT year, runtime_min, original_language, overview, tagline, poster_path,"
             " backdrop_path, trailer_key, wikidata_id, wikipedia_title, rt_slug,"
             " metacritic_slug FROM title WHERE id = $1",
    "title_meta": "SELECT source, payload, fetched_at FROM title_meta WHERE title_id = $1"
                  " ORDER BY source",
    "title_alias": "SELECT alias, region, language, kind FROM title_alias WHERE title_id = $1"
                   " ORDER BY alias, region, language, kind",
    "title_genre": "SELECT genre, source FROM title_genre WHERE title_id = $1"
                   " ORDER BY genre, source",
    "title_keyword": "SELECT keyword, source FROM title_keyword WHERE title_id = $1"
                     " ORDER BY keyword, source",
    "title_country": "SELECT country, source FROM title_country WHERE title_id = $1"
                     " ORDER BY country, source",
    "title_company": "SELECT company, role, source FROM title_company WHERE title_id = $1"
                     " ORDER BY company, role, source",
    "title_video": "SELECT site, key, type, official, source FROM title_video"
                   " WHERE title_id = $1 ORDER BY site, key, type, source",
    "credit": "SELECT person_id, department, job, character, billing_order, source, role_class"
              " FROM credit WHERE title_id = $1 ORDER BY id",
    "award": "SELECT body, category, year, won, person_id FROM award WHERE title_id = $1"
             " ORDER BY id",
    "platform_rating": "SELECT platform, metric, score, scale, votes FROM display.platform_rating"
                       " WHERE title_id = $1 ORDER BY platform, metric",
    "review": "SELECT source, author, url, rating, published_at, is_critic, body, word_count"
              " FROM review_store.review WHERE title_id = $1 ORDER BY id",
}

# `(source, stored kind, fixture, page)`; `tmdb:find` is claimed by no parser, which is why it is seeded.
ARRIVAL_DOCUMENTS: tuple[tuple[str, str, str, int], ...] = (
    ("tmdb", "find", "../http/tmdb_find.json", 0),
    ("tmdb", "movie_detail", "tmdb_movie_detail.json", 0),
    ("omdb", "detail", "omdb_detail.json", 0),
    ("trakt", "summary", "trakt_summary.json", 0),
    ("trakt", "comments", "trakt_comments.json", 1),
    ("wikipedia", "article", "wikipedia_article.json", 0),
    ("rottentomatoes", "page:main", "rt_page.html", 0),
    ("metacritic", "page:main", "metacritic_page.html", 0),
    ("metacritic", "reviews:critics", "metacritic_reviews_critics.html", 0),
    ("metacritic", "reviews:users", "metacritic_reviews_users.html", 0),
)

_HTML = "text/html"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """The module takes no root argument on purpose, so `DATA_DIR` is set."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


async def _title(conn, title_id: int, name: str, **columns) -> None:
    columns.setdefault("kind", "movie")
    columns.setdefault("origin", "acquired")
    names = ", ".join(("id", "name", *columns))
    marks = ", ".join(f"${i}" for i in range(1, len(columns) + 3))
    await conn.execute(
        f"INSERT INTO title ({names}) VALUES ({marks})", title_id, name, *columns.values()
    )


async def _task(conn, key: str, title_id: int) -> None:
    """A dict, not `json.dumps`: the pool's codec would encode a string twice and `->>` would read NULL."""
    await conn.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ('acquire', $1, $2)",
        key, {"title_id": title_id},
    )


def _url(source: str, kind: str, key: str, page: int) -> str:
    """Review views nest under their page's path; `derive/rebuild._under` reads that nesting."""
    head, _, view = kind.partition(":")
    if source not in rebuild.SCRAPED_PAGES:
        return f"https://{source}.test/{key}/{kind}/{page}"
    page_url = f"https://{source}.test/{key}/page/"
    return page_url if head == rebuild.SCRAPED_EVIDENCE else f"{page_url}{view}/"


async def _document(conn, source: str, kind: str, name: str, *, key: str, page: int = 0,
                    url: str | None = None) -> int:
    body = fixture(name)
    return await rawstore.store(
        conn, source=source, kind=kind, url=url or _url(source, kind, key, page),
        content=body, entity_key=key, page=page,
        content_type=_HTML if name.endswith(".html") else "application/json",
    )


async def _seed_arrival(conn, documents=ARRIVAL_DOCUMENTS) -> dict[tuple[str, str], int]:
    await _title(conn, ARRIVAL, "Arrival", year=2016, jellyfin_id="arrival-4k")
    await _task(conn, ARRIVAL_KEY, ARRIVAL)
    stored = {}
    for source, kind, name, page in documents:
        stored[(source, kind)] = await _document(
            conn, source, kind, name, key=ARRIVAL_KEY, page=page
        )
    return stored


async def _snapshot(conn, title_id: int) -> dict[str, list[tuple]]:
    return {
        table: [tuple(row) for row in await conn.fetch(query, title_id)]
        for table, query in _SNAPSHOT.items()
    }


async def test_two_derives_of_one_title_leave_the_same_rows(db, raw_root):
    """Whole-snapshot: per-table counts with `>=` would miss one table lost and another duplicated."""
    await _seed_arrival(db)

    first = await rebuild.derive_title(db, ARRIVAL)
    before = await _snapshot(db, ARRIVAL)
    assert before["credit"], "the fixture derived no credits, so this test would prove nothing"
    assert before["review"], "the fixture derived no reviews, so half the tables are untested"

    second = await rebuild.derive_title(db, ARRIVAL)
    after = await _snapshot(db, ARRIVAL)

    # Named before compared, so the failure says WHICH table moved.
    changed = {table: (len(before[table]), len(after[table]))
               for table in _SNAPSHOT if after[table] != before[table]}
    assert not changed, f"a second derive changed these tables (rows before, after): {changed}"
    assert after == before
    assert second.rows == first.rows
    assert second.sources == first.sources


async def test_a_second_derive_adds_no_person_and_keeps_every_credit_pointed_at_the_same_row(
    db, raw_root
):
    """`person` is not scoped by title, so a duplicate is permanent; ids are never dropped."""
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    people = await db.fetch("SELECT id, name, tmdb_id FROM person ORDER BY id")
    assert people, "no person was minted, so this test would pass against a derive that mints none"
    assert all(row["id"] >= ids.APP_ID_MIN for row in people), (
        f"a derive minted a person below {ids.APP_ID_MIN}: spec 4.1's id partition is the only "
        "thing keeping an acquired person from inheriting a corpus person's credits"
    )
    assert any(row["tmdb_id"] for row in people), (
        "no minted person carries a tmdb id, so the two lookup paths were never exercised"
    )
    credits_before = [tuple(r) for r in await db.fetch(
        "SELECT person_id, source, job FROM credit WHERE title_id = $1 ORDER BY id", ARRIVAL)]

    await rebuild.derive_title(db, ARRIVAL)
    assert [tuple(r) for r in await db.fetch(
        "SELECT id, name, tmdb_id FROM person ORDER BY id")] == [tuple(r) for r in people]
    assert [tuple(r) for r in await db.fetch(
        "SELECT person_id, source, job FROM credit WHERE title_id = $1 ORDER BY id",
        ARRIVAL)] == credits_before


async def test_a_name_only_credit_finds_the_person_this_title_already_credits(db, raw_root):
    """A bare OMDb name matches the person already credited
    on THIS title, so no name-only twin is minted."""
    await _seed_arrival(db)
    twin = await db.fetchval(
        "INSERT INTO person (name) VALUES ('Denis Villeneuve') RETURNING id"
    )
    await db.execute(
        "INSERT INTO credit (title_id, person_id, department, job, role_class, source)"
        " VALUES ($1, $2, 'Directing', 'Director', 'director', 'omdb')", ARRIVAL, twin,
    )

    await rebuild.derive_title(db, ARRIVAL)

    directors = await db.fetch(
        "SELECT DISTINCT c.person_id, p.tmdb_id FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = $1 AND c.role_class = 'director'", ARRIVAL,
    )
    assert len(directors) == 1 and directors[0]["tmdb_id"], (
        f"Arrival's director is credited as {len(directors)} people: {[tuple(r) for r in directors]}"
    )
    assert set(await db.fetchval(
        "SELECT array_agg(DISTINCT source) FROM credit WHERE title_id = $1 AND person_id = $2",
        ARRIVAL, directors[0]["person_id"],
    )) >= {"tmdb", "omdb"}
    assert await db.fetchval(
        "SELECT count(*) FROM person WHERE name = 'Denis Villeneuve' AND tmdb_id IS NULL"
    ) == 1, "the derive minted another name-only Villeneuve beside the one it was repairing"


def test_the_same_title_match_prefers_the_person_who_carries_an_id():
    """An id'd person beats a name-only one, two id'd people
    match nothing, and name-only keeps the lower id."""
    credited: ids.Credited = {}
    ids.note_credited(credited, "Denis Villeneuve", "director", 1_000_000_021, False)
    ids.note_credited(credited, "Denis  Villeneuve", "director", 40115, True)
    assert credited[("denisvilleneuve", "director")] == (40115, True)
    ids.note_credited(credited, "Denis Villeneuve", "director", 1_000_000_030, False)
    assert credited[("denisvilleneuve", "director")] == (40115, True)
    ids.note_credited(credited, "Denis Villeneuve", "director", 50000, True)
    assert credited[("denisvilleneuve", "director")] == (None, True)

    ids.note_credited(credited, "Ali Abbasi", "director", 1_000_000_018, False)
    ids.note_credited(credited, "Ali Abbasi", "director", 1_000_000_009, False)
    assert credited[("aliabbasi", "director")] == (1_000_000_009, False)

    ids.note_credited(credited, "王家卫", "director", 7, True)
    assert ("", "director") not in credited


async def test_a_source_whose_document_is_gone_keeps_the_rows_it_wrote(db, raw_root):
    """Decision 375: a source missing from this run keeps
    the rows it wrote; `award` is guarded by source too."""
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    omdb = {
        table: [row for row in rows if "omdb" in row]
        for table, rows in (await _snapshot(db, ARRIVAL)).items()
        if table in ("title_meta", "title_genre", "title_country", "credit")
    }
    assert all(omdb.values()), f"OMDb wrote nothing to one of these tables: {omdb}"
    awards = [tuple(r) for r in await db.fetch(_SNAPSHOT["award"], ARRIVAL)]
    imdb_rating = [tuple(r) for r in await db.fetch(
        "SELECT platform, metric, score FROM display.platform_rating"
        " WHERE title_id = $1 AND platform = 'imdb'", ARRIVAL)]
    assert awards and imdb_rating, "the OMDb fixture stopped supplying awards or the IMDb score"

    await db.execute("DELETE FROM raw_document WHERE source = 'omdb'")
    report = await rebuild.derive_title(db, ARRIVAL)

    assert "omdb" not in report.sources
    after = await _snapshot(db, ARRIVAL)
    for table, rows in omdb.items():
        assert [row for row in after[table] if "omdb" in row] == rows, (
            f"{table} lost the rows OMDb wrote although this derive never read an OMDb document"
        )
    assert [tuple(r) for r in await db.fetch(_SNAPSHOT["award"], ARRIVAL)] == awards
    assert [tuple(r) for r in await db.fetch(
        "SELECT platform, metric, score FROM display.platform_rating"
        " WHERE title_id = $1 AND platform = 'imdb'", ARRIVAL)] == imdb_rating


async def test_a_source_that_now_says_less_loses_exactly_what_it_stopped_saying(db, raw_root):
    """The document's LABEL is in the delete scope, so a source that now says nothing loses its old rows."""
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    assert [r for r in await db.fetch(_SNAPSHOT["title_genre"], ARRIVAL) if r["source"] == "trakt"]

    # The same source and url, a newer document that says nothing. `latest` takes this one.
    await rawstore.store(
        db, source="trakt", kind="summary", url="https://trakt.test/empty", content=b"{}",
        entity_key=ARRIVAL_KEY,
    )
    report = await rebuild.derive_title(db, ARRIVAL)

    assert "trakt" in report.sources, "the empty document was not even read"
    rows = await _snapshot(db, ARRIVAL)
    assert not [row for row in rows["title_genre"] if "trakt" in row]
    assert not [row for row in rows["title_meta"] if "trakt" in row]
    assert [row for row in rows["title_genre"] if "tmdb" in row], (
        "the empty Trakt response took TMDB's genres with it, which is the unscoped delete"
    )


# Bundle rows as the importer writes them, under labels the crawl also uses and one it does not.
_BUNDLE_REVIEWS = (
    ("trakt", "a corpus reader", "The corpus held this Trakt comment long before the crawl did."),
    ("metacritic", "a corpus critic", "A critic's paragraph the export shipped and no page serves."),
    ("letterboxd", "a corpus diarist", "A Letterboxd entry, from a source decision 374 left out."),
)
_BUNDLE_AWARDS = (
    ("Academy Awards", "Best Sound Editing", 2017, True),
    ("BAFTA Awards", "Best Sound", 2017, True),
)


async def _seed_bundle_rows(conn, title_id: int) -> None:
    await conn.execute("UPDATE title SET origin = 'bundle' WHERE id = $1", title_id)
    await conn.executemany(
        "INSERT INTO review_store.review (title_id, source, author, body) VALUES ($1, $2, $3, $4)",
        [(title_id, *row) for row in _BUNDLE_REVIEWS],
    )
    await conn.executemany(
        "INSERT INTO award (title_id, body, category, year, won) VALUES ($1, $2, $3, $4, $5)",
        [(title_id, *row) for row in _BUNDLE_AWARDS],
    )


async def _bundle_rows(conn, title_id: int) -> tuple[list[tuple], list[tuple]]:
    reviews = [tuple(r) for r in await conn.fetch(
        "SELECT source, author, body, word_count FROM review_store.review"
        " WHERE title_id = $1 AND origin = 'bundle' ORDER BY id", title_id)]
    awards = [tuple(r) for r in await conn.fetch(
        "SELECT body, category, year, won FROM award"
        " WHERE title_id = $1 AND origin = 'bundle' ORDER BY id", title_id)]
    return reviews, awards


async def test_the_bundle_s_reviews_and_awards_survive_the_derive_that_enriches_their_title(
    db, raw_root
):
    """Decision 420: only `origin = 'derived'` rows are replaced; the bundle's are the only copy. Twice."""
    await _seed_arrival(db)
    await _seed_bundle_rows(db, ARRIVAL)
    before = await _bundle_rows(db, ARRIVAL)
    assert len(before[0]) == len(_BUNDLE_REVIEWS) and len(before[1]) == len(_BUNDLE_AWARDS)

    first = await rebuild.derive_title(db, ARRIVAL)
    assert {"trakt", "metacritic", "omdb"} <= set(first.sources), (
        "the crawl did not read the labels the bundle rows are filed under, so nothing was at risk"
    )
    assert await _bundle_rows(db, ARRIVAL) == before, (
        "the derive deleted rows the bundle wrote under a label it crawled; nothing can re-fetch them"
    )
    derived = await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1 AND origin = 'derived'",
        ARRIVAL,
    )
    assert derived and first.rows["review"] == derived, (
        "the board's review count is not the rows this derive wrote"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM award WHERE title_id = $1 AND origin = 'derived'", ARRIVAL
    ) == first.rows["award"] > 0, "the OMDb blurb stopped yielding an award, so half is untested"

    snapshot = await _snapshot(db, ARRIVAL)
    second = await rebuild.derive_title(db, ARRIVAL)
    assert await _bundle_rows(db, ARRIVAL) == before
    assert await _snapshot(db, ARRIVAL) == snapshot, "the second derive changed a table"
    assert second.rows == first.rows


async def test_a_review_or_award_the_bundle_already_holds_is_not_written_beside_it(db, raw_root):
    """A duplicate review would double decision 335's
    `sum(word_count)` and let a thin title pass stage 4."""
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    await db.execute("UPDATE title SET origin = 'bundle' WHERE id = $1", ARRIVAL)
    await db.execute("UPDATE review_store.review SET origin = 'bundle' WHERE title_id = $1", ARRIVAL)
    await db.execute("UPDATE award SET origin = 'bundle' WHERE title_id = $1", ARRIVAL)
    held = await _bundle_rows(db, ARRIVAL)
    assert held[0] and held[1], "the fixture derived no review or no award to hand over"

    report = await rebuild.derive_title(db, ARRIVAL)

    assert await _bundle_rows(db, ARRIVAL) == held
    assert await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1 AND origin = 'derived'",
        ARRIVAL,
    ) == 0, "a review the bundle already holds was written a second time"
    assert await db.fetchval(
        "SELECT count(*) FROM award WHERE title_id = $1 AND origin = 'derived'", ARRIVAL
    ) == 0, "an award the bundle already holds was written a second time"
    assert "review" not in report.rows and "award" not in report.rows, report.rows


async def test_title_meta_keeps_one_row_per_source_and_is_never_collapsed(db, raw_root):
    """§4.1: per-source meta rows are kept; collapsing them destroys what `SOURCE_PRIORITY` orders."""
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)

    rows = await db.fetch(
        "SELECT source, payload FROM title_meta WHERE title_id = $1 ORDER BY source", ARRIVAL)
    assert [row["source"] for row in rows] == ["omdb", "tmdb", "trakt", "wikipedia"]
    # The rows disagree, which is the whole reason to keep four.
    assert rows[1]["payload"]["tagline"] and not rows[0]["payload"]["tagline"]
    assert rows[3]["payload"]["plot_short"], "wikipedia is the only source carrying plot_short"
    assert not rows[1]["payload"]["plot_short"]


async def test_the_meta_row_carries_the_document_s_own_fetch_time(db, raw_root):
    """The document's own fetch time, which also keeps two derives of the same bytes identical."""
    stored = await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    expected = await db.fetchval(
        "SELECT fetched_at FROM raw_document WHERE id = $1", stored[("tmdb", "movie_detail")])
    assert await db.fetchval(
        "SELECT fetched_at FROM title_meta WHERE title_id = $1 AND source = 'tmdb'",
        ARRIVAL) == expected


async def test_a_derive_of_one_title_leaves_another_title_s_card_exactly_as_it_was(db, raw_root):
    """An unscoped `resolve_title_fields` would silently rewrite
    the whole library; the neighbour's card is made to disagree."""
    await _seed_arrival(db)
    await _title(
        db, NEIGHBOUR, "Heat", year=1995, overview="the card an operator typed",
        tagline="a tagline no source supplied", poster_path="/kept.jpg",
        backdrop_path="/kept-wide.jpg", trailer_key="kept-trailer",
    )
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, 'tmdb', $2)",
        NEIGHBOUR, {"plot_full": "what tmdb says", "tagline": "tmdb's tagline",
                    "poster_url": "/tmdb.jpg", "backdrop_url": "/tmdb-wide.jpg"},
    )
    await db.execute(
        "INSERT INTO title_video (title_id, source, site, key, type)"
        " VALUES ($1, 'tmdb', 'YouTube', 'other-trailer', 'Trailer')", NEIGHBOUR)
    before = await _snapshot(db, NEIGHBOUR)

    await rebuild.derive_title(db, ARRIVAL)

    after = await _snapshot(db, NEIGHBOUR)
    changed = {table: (before[table], after[table])
               for table in _SNAPSHOT if after[table] != before[table]}
    assert not changed, (
        f"deriving {ARRIVAL} rewrote {sorted(changed)} on title {NEIGHBOUR}: {changed}"
    )
    assert after == before
    # The control: the derive did resolve the card it was asked about.
    card = await db.fetchrow("SELECT overview, tagline, poster_path FROM title WHERE id = $1",
                             ARRIVAL)
    assert card["overview"] and card["tagline"] and card["poster_path"]


async def test_a_document_filed_under_another_title_s_task_is_not_read(db, raw_root):
    """The neighbour's source is one Arrival lacks, or `DISTINCT ON` would hide an unfiltered read."""
    await _seed_arrival(db)
    await _title(db, NEIGHBOUR, "The Expanse", kind="series", year=2015, jellyfin_id="expanse-hd")
    await _task(db, NEIGHBOUR_KEY, NEIGHBOUR)
    await _document(db, "tvmaze", "show", "tvmaze_show.json", key=NEIGHBOUR_KEY)

    report = await rebuild.derive_title(db, ARRIVAL)

    assert "tvmaze:show" not in report.documents
    assert "tvmaze" not in report.sources
    assert not await db.fetch(
        "SELECT 1 FROM title_meta WHERE title_id = $1 AND source = 'tvmaze'", ARRIVAL)
    assert not await db.fetch(
        "SELECT 1 FROM credit WHERE title_id = $1 AND source = 'tvmaze'", ARRIVAL)
    assert await db.fetchval("SELECT count(*) FROM credit WHERE title_id = $1", NEIGHBOUR) == 0


async def test_a_scraped_page_for_another_film_is_refused_and_writes_nothing(db, raw_root):
    """The wrong page stays stored `ok = true`, so the derive
    itself must refuse it; quote verification cannot."""
    documents = tuple(
        (source, kind, "metacritic_page_alpha_2018.html" if source == "metacritic"
         and kind == "page:main" else name, page)
        for source, kind, name, page in ARRIVAL_DOCUMENTS
    )
    await _seed_arrival(db, documents)

    report = await rebuild.derive_title(db, ARRIVAL)

    assert report.refused == ("metacritic:page:main: a different title of the same name",)
    assert not await db.fetch(
        "SELECT 1 FROM review_store.review WHERE title_id = $1 AND source = 'metacritic'", ARRIVAL)

    # OMDb's relayed Metacritic score survives: refusing the page takes only the page's own scores.
    scores = await db.fetch(
        "SELECT metric, score FROM display.platform_rating"
        " WHERE title_id = $1 AND platform = 'metacritic' ORDER BY metric", ARRIVAL)
    relayed = [row for row in parse.parse_document("omdb", "detail", fixture("omdb_detail.json"))
               .table("platform_rating") if row["source"] == "metacritic"]
    assert [(row["metric"], row["score"]) for row in scores] == [
        (row["metric"], row["value"]) for row in relayed]

    # And the sources about this film all remain: the refusal is per source.
    assert await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1 AND source = 'trakt'",
        ARRIVAL) > 0


async def test_reviews_fetched_from_a_path_no_accepted_page_proves_are_not_written(db, raw_root):
    """Reviews from another slug beside a good page are dropped by url; the url is the tie."""
    await _title(db, ARRIVAL, "Arrival", year=2016, jellyfin_id="arrival-4k")
    await _task(db, ARRIVAL_KEY, ARRIVAL)
    await _document(db, "tmdb", "movie_detail", "tmdb_movie_detail.json", key=ARRIVAL_KEY)
    await _document(db, "metacritic", "page:main", "metacritic_page.html", key=ARRIVAL_KEY)
    # The same two review views, filed under a DIFFERENT metacritic path.
    for kind in ("reviews:critics", "reviews:users"):
        await _document(db, "metacritic", kind, f"metacritic_{kind.replace(':', '_')}.html",
                        key=ARRIVAL_KEY,
                        url=f"https://metacritic.test/{ARRIVAL_KEY}/other-page/{kind[8:]}/")

    report = await rebuild.derive_title(db, ARRIVAL)

    assert "metacritic:page:main" in report.documents, report.documents
    assert not await db.fetch(
        "SELECT 1 FROM review_store.review WHERE title_id = $1 AND source = 'metacritic'", ARRIVAL)
    assert [line for line in report.refused if "reviews" in line], report.refused
    # The drop is per document: a proved page keeps its scores.
    assert await db.fetchval(
        "SELECT count(*) FROM display.platform_rating WHERE title_id = $1 AND platform ="
        " 'metacritic'", ARRIVAL) > 0


async def test_a_scraped_source_with_no_accepted_page_writes_no_reviews_either(db, raw_root):
    """With no page document at all, `_under` reads the page as missing and writes no reviews."""
    await _title(db, ARRIVAL, "Arrival", year=2016, jellyfin_id="arrival-4k")
    await _task(db, ARRIVAL_KEY, ARRIVAL)
    await _document(db, "metacritic", "reviews:critics", "metacritic_reviews_critics.html",
                    key=ARRIVAL_KEY)

    report = await rebuild.derive_title(db, ARRIVAL)

    assert report.documents == ("metacritic:reviews:critics",), report.documents
    assert report.refused == (
        "metacritic:reviews:critics: not fetched from the page this title proved",
    )
    assert not await db.fetch("SELECT 1 FROM review_store.review WHERE title_id = $1", ARRIVAL)


async def test_the_row_counts_on_the_board_are_the_rows_the_database_actually_holds(db, raw_root):
    """Rows are counted as landed, not as handed to `executemany`: conflicts happen on the happy path."""
    await _seed_arrival(db)
    report = await rebuild.derive_title(db, ARRIVAL)

    counted = {}
    for target in rebuild.SCOPE_COLUMN:
        counted[target] = await db.fetchval(
            f"SELECT count(*) FROM {target} WHERE title_id = $1", ARRIVAL)
    counted["title_meta"] = await db.fetchval(
        "SELECT count(*) FROM title_meta WHERE title_id = $1", ARRIVAL)
    counted["review"] = await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1", ARRIVAL)

    # `person` is outside the comparison: `DeriveReport.people` counts humans, not rows.
    claimed = {table: count for table, count in report.rows.items() if table in counted}
    assert set(claimed) >= {"display.platform_rating", "title_country"}, (
        f"the fixture does not exercise the two tables that collide: {sorted(claimed)}"
    )
    assert claimed == {table: counted[table] for table in claimed}, (
        f"the board's counts are not the database's: board {claimed}, database {counted}"
    )


async def test_the_refusal_reads_the_cast_this_run_parsed_and_not_the_one_already_stored(
    db, raw_root
):
    """The cast comes from this run's TMDB parse; stored credits may be none on a new title."""
    assert await db.fetchval("SELECT count(*) FROM credit") == 0
    page = fixture("metacritic_page_alpha_2018.html")
    assert parse.page_belongs_to_title(page, year=2016, people=set(), mode="metacritic"), (
        "the fixture no longer passes on an empty cast, so this test proves nothing about when "
        "the cast is read"
    )

    await _seed_arrival(db, (
        ("tmdb", "movie_detail", "tmdb_movie_detail.json", 0),
        ("metacritic", "page:main", "metacritic_page_alpha_2018.html", 0),
    ))
    report = await rebuild.derive_title(db, ARRIVAL)

    assert report.refused
    assert not await db.fetch(
        "SELECT 1 FROM display.platform_rating WHERE title_id = $1 AND platform = 'metacritic'",
        ARRIVAL)


async def test_a_refused_page_takes_back_the_rows_a_previous_derive_wrote_from_it(db, raw_root):
    """A refused source keeps its label in the delete scope and contributes no rows."""
    await _seed_arrival(db, (("metacritic", "page:main", "metacritic_page_alpha_2018.html", 0),))
    await rebuild.derive_title(db, ARRIVAL)
    assert await db.fetchval(
        "SELECT count(*) FROM display.platform_rating WHERE title_id = $1", ARRIVAL) > 0

    await _document(db, "tmdb", "movie_detail", "tmdb_movie_detail.json", key=ARRIVAL_KEY)
    await rebuild.derive_title(db, ARRIVAL)

    assert not await db.fetch(
        "SELECT 1 FROM display.platform_rating WHERE title_id = $1 AND platform = 'metacritic'",
        ARRIVAL)


async def test_the_slug_a_refused_page_was_fetched_under_is_left_for_stage_two(db, raw_root):
    """Decision 372: stage 2 owns identity columns, so the derive does not clear the slug."""
    await _seed_arrival(db, (
        ("tmdb", "movie_detail", "tmdb_movie_detail.json", 0),
        ("metacritic", "page:main", "metacritic_page_alpha_2018.html", 0),
    ))
    await db.execute("UPDATE title SET metacritic_slug = 'movie/alpha' WHERE id = $1", ARRIVAL)

    assert (await rebuild.derive_title(db, ARRIVAL)).refused
    assert await db.fetchval(
        "SELECT metacritic_slug FROM title WHERE id = $1", ARRIVAL) == "movie/alpha"


async def test_a_title_whose_required_document_never_arrived_derives_what_it_has(db, raw_root):
    """Decision 334: only `tmdb:detail` is required, and a hole is reported, not raised."""
    await _seed_arrival(db, tuple(
        doc for doc in ARRIVAL_DOCUMENTS if doc[:2] != ("tmdb", "movie_detail")))

    report = await rebuild.derive_title(db, ARRIVAL)

    assert "tmdb:movie_detail" not in report.documents
    assert report.rows["credit"] and report.rows["review"]
    assert "tmdb" not in report.sources


async def test_a_document_no_parser_claims_is_skipped_and_its_bytes_are_never_read(db, raw_root):
    """The file is damaged: a derive that read it would raise, which is stronger than "no rows appeared"."""
    stored = await _seed_arrival(db)
    path = rawstore.resolve(await db.fetchval(
        "SELECT content_path FROM raw_document WHERE id = $1", stored[("tmdb", "find")]))
    path.write_bytes(gzip.compress(b"not the document this row names"))

    report = await rebuild.derive_title(db, ARRIVAL)

    assert "tmdb:find" not in report.documents
    assert report.rows["title_meta"] == 4


async def test_a_document_whose_file_is_not_the_one_its_row_names_stops_the_derive(db, raw_root):
    """Decision 361's `OSError` is not caught: the one transaction leaves the last good derive intact."""
    stored = await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    before = await _snapshot(db, ARRIVAL)

    path = rawstore.resolve(await db.fetchval(
        "SELECT content_path FROM raw_document WHERE id = $1",
        stored[("tmdb", "movie_detail")]))
    path.write_bytes(gzip.compress(b'{"title": "somebody elses film"}'))

    with pytest.raises(OSError) as caught:
        await rebuild.derive_title(db, ARRIVAL)
    assert "sha256" in str(caught.value)
    assert await _snapshot(db, ARRIVAL) == before


async def test_deriving_a_title_that_does_not_exist_is_a_lookup_error(db, raw_root):
    """A missing title and a title with nothing derived are different facts."""
    with pytest.raises(LookupError) as caught:
        await rebuild.derive_title(db, ARRIVAL)
    assert str(ARRIVAL) in str(caught.value)


async def test_a_title_with_no_documents_at_all_derives_nothing_and_does_not_raise(db, raw_root):
    """The state every acquired title passes through between stage 1 and stage 2."""
    await _title(db, SPARSE, "Unfetched", year=2026)
    await _task(db, SPARSE_KEY, SPARSE)

    report = await rebuild.derive_title(db, SPARSE)

    assert report.documents == () and report.rows == {}
    assert await db.fetchval("SELECT count(*) FROM title_meta WHERE title_id = $1", SPARSE) == 0


async def test_the_derive_opens_no_socket(db, raw_root, monkeypatch):
    """The dynamic half; the static import guard is `test_derive_parse.py`'s."""
    await _seed_arrival(db)

    def no_connect(*args, **kwargs):
        raise AssertionError(
            "the derive opened a socket: re-parsing reads the store and never re-fetches, and the "
            "hosts it would reach are eight third parties on a household's own IP address"
        )

    # `connect`, not the class: Windows' proactor loop checks
    # `isinstance(sock, socket.socket)` on the open connection.
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connect)
    report = await rebuild.derive_title(db, ARRIVAL)
    assert report.rows["credit"]


async def test_the_documents_are_read_newest_first_and_per_page(db, raw_root):
    """`page` is part of the read key; a second capture of one page supersedes the first."""
    await _seed_arrival(db, (("trakt", "comments", "trakt_comments.json", 1),))
    # A genuinely different page; the same bytes twice would be deduped by fingerprint.
    other = [{**comment, "id": comment["id"] + 90_000,
              "comment": f"{comment['comment']} (from the lowest-rated sort)"}
             for comment in json.loads(fixture("trakt_comments.json"))]
    await rawstore.store(
        db, source="trakt", kind="comments", url="https://trakt.test/lowest/1",
        content=json.dumps(other).encode("utf-8"), entity_key=ARRIVAL_KEY, page=11,
    )
    await rebuild.derive_title(db, ARRIVAL)
    two_pages = await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1", ARRIVAL)

    # A second capture of page 1 replaces it and adds nothing.
    await rawstore.store(
        db, source="trakt", kind="comments", url="https://trakt.test/again", content=b"[]",
        entity_key=ARRIVAL_KEY, page=1,
    )
    await rebuild.derive_title(db, ARRIVAL)
    one_page = await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1", ARRIVAL)

    assert two_pages == one_page * 2 > 0, (
        f"two pages derived {two_pages} reviews and one derived {one_page}: a read that ignored "
        "`page` would have found one document both times"
    )


async def test_one_review_carried_by_two_documents_of_one_source_is_stored_once(db, raw_root):
    """Metacritic's critic and user pages overlap; the `page:main`
    document is seeded because reviews hang off it."""
    await _seed_arrival(db, (
        ("metacritic", "page:main", "metacritic_page.html", 0),
        ("metacritic", "reviews:critics", "metacritic_reviews_critics.html", 0),
        ("metacritic", "reviews:users", "metacritic_reviews_critics.html", 1),
    ))
    await rebuild.derive_title(db, ARRIVAL)

    bodies = [r["body"] for r in await db.fetch(
        "SELECT body FROM review_store.review WHERE title_id = $1", ARRIVAL)]
    assert bodies and len(bodies) == len(set(bodies))


# RT's captures are filed as `rt_*`, so the name filter needs this table or `parse_rt_page` never runs.
_FIXTURE_PREFIX = {"rottentomatoes": "rt"}


def test_no_source_outside_the_award_guard_emits_an_award():
    """`award` has no source column, so `AWARD_SOURCES` decides
    deletion; every routed parser must be exercised."""
    routed = parse.parsed_sources()
    emitting = set()
    exercised = set()
    for path in sorted(FIXTURES.glob("*.*")):
        if path.suffix == ".md":
            continue
        for (source, head), label in routed.items():
            kind = "page:main" if head == "page" else head
            if not path.name.startswith(_FIXTURE_PREFIX.get(source, source.split("_")[0])):
                continue
            exercised.add((source, head))
            result = parse.parse_document(source, kind, path.read_bytes())
            if result.table("award"):
                emitting.add(label)
    assert exercised == set(routed), (
        f"{sorted(set(routed) - exercised)} is routed and this guard never parsed it: the answer "
        "is read back off the captured responses, so a parser it does not call is a source whose "
        "awards it cannot see"
    )
    assert emitting, "no fixture produced an award row, so this guard measured nothing"
    assert emitting <= rebuild.AWARD_SOURCES, (
        f"{sorted(emitting - rebuild.AWARD_SOURCES)} emits an award row and is not in "
        "AWARD_SOURCES, so the derive writes those rows and never replaces them"
    )


async def test_the_derive_writes_the_card_fields_and_the_three_title_columns_below_them(
    db, raw_root
):
    """`year`, `runtime_min` and `original_language` live
    on `title`, so the derive resolves them by `best()`."""
    await _seed_arrival(db)
    await db.execute(
        "UPDATE title SET year = NULL, runtime_min = NULL, original_language = NULL WHERE id = $1",
        ARRIVAL)

    await rebuild.derive_title(db, ARRIVAL)

    row = await db.fetchrow(
        "SELECT year, runtime_min, original_language, overview, tagline, poster_path,"
        " backdrop_path, trailer_key FROM title WHERE id = $1", ARRIVAL)
    assert row["year"] == 2016 and row["runtime_min"] == 116
    assert row["original_language"] == "en"
    assert all(row[column] for column in
               ("overview", "tagline", "poster_path", "backdrop_path", "trailer_key"))


async def test_a_year_the_library_supplied_is_not_erased_by_a_source_that_omits_one(db, raw_root):
    """The library's own year must not be erased by a source that simply omits one."""
    await _seed_arrival(db, (("wikipedia", "article", "wikipedia_article.json", 0),))
    await db.execute("UPDATE title SET runtime_min = 116 WHERE id = $1", ARRIVAL)

    await rebuild.derive_title(db, ARRIVAL)

    row = await db.fetchrow("SELECT year, runtime_min FROM title WHERE id = $1", ARRIVAL)
    assert (row["year"], row["runtime_min"]) == (2016, 116)


async def test_an_identifier_the_parse_yielded_and_no_adapter_wrote_is_filled_never_clobbered(
    db, raw_root
):
    """Through `sources/_ids.set_ids`, the one fill-never-clobber implementation."""
    await _seed_arrival(db, (("wikipedia", "article", "wikipedia_article.json", 0),))
    await rebuild.derive_title(db, ARRIVAL)
    filled = await db.fetchval("SELECT wikipedia_title FROM title WHERE id = $1", ARRIVAL)
    assert filled, "the wikipedia fixture no longer carries the article title in its payload"

    await db.execute("UPDATE title SET wikipedia_title = 'Arrival (the household said so)'"
                     " WHERE id = $1", ARRIVAL)
    await rebuild.derive_title(db, ARRIVAL)
    assert await db.fetchval(
        "SELECT wikipedia_title FROM title WHERE id = $1",
        ARRIVAL) == "Arrival (the household said so)"


async def test_two_sources_scoring_one_platform_leave_one_row_and_the_page_s_own_score(
    db, raw_root
):
    """Last write wins and the scraped pages are read last, the same way every run."""
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)

    rows = await db.fetch(
        "SELECT platform, metric, score, scale FROM display.platform_rating"
        " WHERE title_id = $1 AND platform = 'metacritic' ORDER BY metric", ARRIVAL)
    assert [row["metric"] for row in rows] == ["critic_score", "user_score"], (
        "OMDb relays only a critic score, so a second row here is the page's own"
    )
    page = parse.parse_document("metacritic", "page:main", fixture("metacritic_page.html"))
    theirs = {row["metric"]: row["value"] for row in page.table("platform_rating")}
    assert {row["metric"]: row["score"] for row in rows} == theirs


async def test_a_title_carrying_both_curated_ledgers_derives_twice_and_keeps_both(db, raw_root):
    """The acceptance test with a household row in each ledger, which the appliers must not filter out."""
    await _seed_arrival(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 1)")
    tag = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) VALUES"
        " ($1, 'v1', 'mood.cosy', 'mood', 2) RETURNING id", ARRIVAL)
    await db.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'trakt:comment')",
        tag, "a quote the owner ruled describes the novel")
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, origin)"
        " VALUES ('v1', 'title', $1, 'mood.cosy', 'drop', 'household')", ARRIVAL)
    await db.execute(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, origin)"
        " VALUES ($1, 'composer', 'The Household Composer', 'the album credit', 'household')",
        ARRIVAL)

    first = await rebuild.derive_title(db, ARRIVAL)
    before = await _snapshot(db, ARRIVAL)
    assert before["credit"], "the fixture derived no credits, so this test would prove nothing"

    second = await rebuild.derive_title(db, ARRIVAL)
    after = await _snapshot(db, ARRIVAL)

    changed = {table: (len(before[table]), len(after[table]))
               for table in _SNAPSHOT if after[table] != before[table]}
    assert not changed, f"a second derive changed these tables (rows before, after): {changed}"
    assert first.adjudications["dropped"] == 1 and second.adjudications == {"rules": 1}
    assert first.corrections["replaced"] == second.corrections["replaced"] == 1
    assert await db.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1", ARRIVAL) == 0, (
        "the curated DNA verdict was not applied, or was reverted by the second derive"
    )
    music = [tuple(row) for row in await db.fetch(
        "SELECT p.name, c.source FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')",
        ARRIVAL)]
    assert music == [("The Household Composer", "correction")]
