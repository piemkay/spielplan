"""§8 stage 3's derive, against a real Postgres and a real raw store. Spec v2.1 §8, §14 risk 5.

IDEMPOTENCE IS THE ACCEPTANCE TEST AND IT IS WRITTEN BEFORE THE LEDGER APPLIERS EXIST, so that
`derive/ledgers.py` is added to a derive already known to run twice. §14.5's scar -- 787 curated
rows reverted, twice -- is a scar about a derive that regenerated rows; a derive that regenerated
DIFFERENT rows each time cannot be fixed by applying a ledger after it. The last test in this file
is where they arrived: the same snapshot equality, over a title that carries a curated row in each
ledger, because the assertion at the top of this file is the one an applier that does nothing
passes. The appliers' own tests are `test_derive_ledgers.py`'s.

WHAT "THE SAME ROWS" MEANS HERE, since it is the whole point. `credit` and `review_store.review`
carry `bigserial` keys, so a re-derive gives every row a new id and a byte-identical comparison of
whole rows would be meaningless. `_snapshot` therefore reads each table WITHOUT its surrogate id
and ORDERED BY it, which asserts the two things that matter at once: the values are the same, and
they are in the same order. Decision 375 makes that order a property of the data -- the documents
are read in a deterministic order and `credit.billing_order` comes off the document's own billing
-- so a derive that re-inserted in a different order would be caught here rather than on a card.

THE FIXTURES ARE REAL RESPONSES. `backend/tests/fixtures/sources/` is one captured document per
source from the corpus's own raw store, and `metacritic_page_alpha_2018.html` is the real
collision: the corpus asked `movie/alpha` for Alpha (2026) and Metacritic served the 2018 film. A
synthesised page would prove that `page_belongs_to_title` returns False when told to; this one
proves the derive asks it, with the cast this run produced, about bytes a real guess really
returned.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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

# Ids in the app's own half of the partition (§4.1, `0015_seed.sql:20-25`), written out rather
# than minted: this file is about the derive and a fixture that had to walk stage 1 would fail for
# the driver's reasons. `test_reviews_gate.py` and `test_acquisition_board.py` take the same shape.
ARRIVAL = 1_000_000_701
NEIGHBOUR = 1_000_000_702
SPARSE = 1_000_000_703

# `raw_document.entity_key` is the acquisition TASK's key and never the title id
# (`acquire/rawstore.py:278-285`), and a document belongs to a title when it is filed under the key
# of a task whose payload names that title (`acquire/board.py:27-44`). Keying the fixtures on a
# Jellyfin item is what makes that indirection load-bearing: a derive that reconstructed the key
# from `title_id` would read nothing here.
ARRIVAL_KEY = "jellyfin:arrival-4k"
NEIGHBOUR_KEY = "jellyfin:heat-hd"
SPARSE_KEY = "title:sparse"

# Every derived table, read without its surrogate key and in the order the derive wrote it. The
# `title` row is in here too: §6.0's card is what `resolve_title_fields` resolves out of
# `title_meta`, and a derive that moved a neighbour's card would otherwise pass every table below.
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
    "title_language": "SELECT language, role, source FROM title_language WHERE title_id = $1"
                      " ORDER BY language, role, source",
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

# The eight documents an Arrival acquisition leaves behind, in the shape `sources/` files them:
# `(source, stored kind, fixture, page)`. `tmdb:find` is here and no parser claims it -- it is
# fetched for the TMDB id stage 2 has already taken off it -- which is what makes it worth seeding.
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
    """A raw store of this test's own, reached the way the worker reaches it.

    `test_acquire_rawstore.py:38-52`'s fixture, verbatim in mechanism and for its reason: the
    module takes no root argument on purpose, because a root that can be passed in is a root a
    caller can pass wrong, and the one caller that matters runs in a container where the directory
    is a bind mount.
    """
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
    """The row that relates a document's `entity_key` to a title. `acquire/board.py:37-44`.

    The payload is a dict and not `json.dumps` of one: the pool registers a json/jsonb codec
    (`db/pool.py:35-44`) whose encoder is `json.dumps`, so a string argument is encoded a second
    time and lands as a JSON STRING that `payload ->> 'title_id'` reads as NULL.
    `test_acquisition_board.py:103-108` seeds the same row the same way.
    """
    await conn.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ('acquire', $1, $2)",
        key, {"title_id": title_id},
    )


def _url(source: str, kind: str, key: str, page: int) -> str:
    """The url a document of this kind really carries, because the derive now reads it.

    A scraped source puts every document of one title UNDER one page path:
    `sources/metacritic.py` fetches the page at `<base>/<path>/` and its two review views at
    `<base>/<path>/critic-reviews/` and `<base>/<path>/user-reviews/`. That nesting is what ties a
    `reviews:*` row to the `page:main` the identity check ruled on, and `derive/rebuild._under` is
    what reads it - so a helper that filed them at unrelated urls would seed a state stage 2
    cannot produce and would make every test here pass for the wrong reason.
    [M5.3 review cycle 1, m53-c1-slug-02]
    """
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


# --- the acceptance test ------------------------------------------------------------------------


async def test_two_derives_of_one_title_leave_the_same_rows(db, raw_root):
    """§14 risk 5's first invariant: "idempotent re-ingest". Every table, values and order.

    The comparison is whole-snapshot rather than per table on purpose. A derive that lost one
    table's rows on the second pass and duplicated another's would satisfy any count assertion
    taken table by table with a `>=`, and this is the assertion the milestone's exit criterion
    states in one line: "Deriving the same title a second time changes nothing."
    """
    await _seed_arrival(db)

    first = await rebuild.derive_title(db, ARRIVAL)
    before = await _snapshot(db, ARRIVAL)
    assert before["credit"], "the fixture derived no credits, so this test would prove nothing"
    assert before["review"], "the fixture derived no reviews, so half the tables are untested"

    second = await rebuild.derive_title(db, ARRIVAL)
    after = await _snapshot(db, ARRIVAL)

    # Named before compared, so the failure says WHICH table moved. A whole-dict diff over
    # thirteen tables is a thousand lines pytest truncates, and the one thing a reader needs from
    # it is the table name.
    changed = {table: (len(before[table]), len(after[table]))
               for table in _SNAPSHOT if after[table] != before[table]}
    assert not changed, f"a second derive changed these tables (rows before, after): {changed}"
    assert after == before
    assert second.rows == first.rows
    assert second.sources == first.sources


async def test_a_second_derive_adds_no_person_and_keeps_every_credit_pointed_at_the_same_row(
    db, raw_root
):
    """The one table a re-derive writes that is NOT scoped by title, so a duplicate is permanent.

    `derive/ids.upsert_person` is the mechanism and its own docstring argues the mint: "a PERSON
    silently acquires the credits of whoever held that id in the corpus". What is asserted here is
    that the DERIVE does not defeat it -- it hands the parser's person block over whole, so the
    second run finds each human by the same key the first minted them under.

    THE TWO SOURCES ARE NOT MERGED, which is the half that fails silently. TMDB's credits carry a
    `tmdb_id` and OMDb's carry a bare name, so `Denis Villeneuve` is looked up two different ways;
    dropping the id would collapse them onto one `person` row, losing the identifier §4.3's
    `p:<role_class>:<name>` grammar and M5.4's DNA both read.
    """
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


# --- decision 375's scope -----------------------------------------------------------------------


async def test_a_source_whose_document_is_gone_keeps_the_rows_it_wrote(db, raw_root):
    """Decision 375's whole point, and the failure it is written against.

    "Never delete by `title_id` alone - a derive that ran with TMDB down would erase the credits a
    previous successful derive wrote." Here the document is removed from the store rather than the
    source failing, which is the same fact reaching the derive: `_DOCUMENTS` returns no OMDb row,
    so 'omdb' is not in the scope, so nothing deletes what OMDb wrote last time.

    `award` is asserted with it because it is the one derived table with no source column: its
    delete is by `title_id` and is guarded by whether an award-producing source was read at all, so
    a run that read none must leave the awards standing.
    """
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    omdb = {
        table: [row for row in rows if "omdb" in row]
        for table, rows in (await _snapshot(db, ARRIVAL)).items()
        if table in ("title_meta", "title_genre", "title_country", "title_language", "credit")
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
    """The other half of replace-by-scope: the delete runs even when the parse produced nothing.

    `derive/parse.py:1389-1394` states it -- "A parse that returns ZERO rows still has to delete
    last run's rows for the source it was about, and `ParsedTitle.row_sources` is empty precisely
    then" -- which is why the document's LABEL is in the scope and not only its rows' sources. A
    derive scoping on the rows alone would leave a whole source's rows behind for ever the day its
    response went empty, and nothing on the board would say so.
    """
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    assert [r for r in await db.fetch(_SNAPSHOT["title_genre"], ARRIVAL) if r["source"] == "trakt"]

    # The same source, the same url, a newer document that says nothing. `latest` takes this one.
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


# --- decision 420: the bundle's rows are not this derive's to replace --------------------------


# Bundle rows written the way the importer writes them: by a loader that names no `origin`, so the
# column's default says who wrote them. Filed under labels this crawl ALSO produces ('trakt',
# 'metacritic') and one it does not ('letterboxd'), because `importer/reviews.py` copies the
# corpus's own `source` column and the label alone cannot tell the two authors apart.
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
    """Decision 420, and decision 162 behind it: content seeds once, so a bundle row is the only copy.

    §12's M2 row parks the thin bundle titles "as acquisition jobs for M5 enrichment", and
    `placement/reconcile._park_thin` writes them at stage 2 - so the drain crawls a CORPUS title
    and stage 3 derives it. Decision 375 scoped the delete on `(title_id, source)`, and the
    importer files the corpus's review bodies under the very labels the crawl uses, so that derive
    deleted every bundle review under 'trakt' and 'metacritic' and put one crawl's first page in
    their place; `award`'s delete had no predicate but `title_id` and took every bundle award the
    moment OMDb was read. Measured on the ten real fixture documents: 400 bundle reviews to 91, 12
    bundle awards to 3. The export ships no `data/raw/`, and no §8 stage 2 source serves a corpus
    review body or an IMDb per-category award row back - there was nothing to re-derive them from.

    The derive now takes back only what it can rebuild: rows it wrote itself, `origin = 'derived'`.
    Twice, because the second pass is where a derive that could no longer find its own rows would
    either duplicate them or start deleting the bundle's.
    [M5.3 review cycle 2, m53-c2-d375-01]
    """
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
    """Decision 420's other half: adding to the bundle's rows must not mean doubling them.

    The corpus crawled these same sources, so a corpus title's Trakt comments and Metacritic
    reviews are, row for row, the ones the drain fetches again. With the bundle's rows kept, a
    derive that inserted everything it parsed would file each of them a second time under the
    same label - and `review_store.review` refuses no duplicate, so decision 335's `sum(word_count)`
    would count one review twice and a thin title would clear stage 4 on arithmetic. Seeded here
    by deriving once and handing every row to the bundle, which is exactly the state an import of
    the corpus leaves: the same rows, a different author.
    """
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
    """§4.1: "title_meta (multi-source, per-source rows kept - 'one block = one droppable source')".

    A derive that collapsed them destroys the thing `SOURCE_PRIORITY` exists to order, and
    `0015_seed.sql:190-210` records the measurement from the three sibling tables that were keyed
    more coarsely: 17,342 / 19,092 / 32,463 duplicate groups and a COPY that rolled the seed back.
    """
    await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)

    rows = await db.fetch(
        "SELECT source, payload FROM title_meta WHERE title_id = $1 ORDER BY source", ARRIVAL)
    assert [row["source"] for row in rows] == ["omdb", "tmdb", "trakt", "wikipedia"]
    # The rows are not copies of each other: the whole reason to keep four is that they disagree.
    assert rows[1]["payload"]["tagline"] and not rows[0]["payload"]["tagline"]
    assert rows[3]["payload"]["plot_short"], "wikipedia is the only source carrying plot_short"
    assert not rows[1]["payload"]["plot_short"]


async def test_the_meta_row_carries_the_document_s_own_fetch_time(db, raw_root):
    """`title_meta.fetched_at` is when this source said this, and the derive is the only writer
    that can know it: the importer loads a table the corpus assembled at some other time.

    Taken from the raw document rather than from `now()`, which is also what keeps the row
    identical across two derives of the same bytes - the property the first test in this file
    measures over every column at once.
    """
    stored = await _seed_arrival(db)
    await rebuild.derive_title(db, ARRIVAL)
    expected = await db.fetchval(
        "SELECT fetched_at FROM raw_document WHERE id = $1", stored[("tmdb", "movie_detail")])
    assert await db.fetchval(
        "SELECT fetched_at FROM title_meta WHERE title_id = $1 AND source = 'tmdb'",
        ARRIVAL) == expected


# --- the landmine: resolve_title_fields groups the whole database ---------------------------------


async def test_a_derive_of_one_title_leaves_another_title_s_card_exactly_as_it_was(db, raw_root):
    """Plan section 9's named landmine: "`resolve_title_fields` groups the whole database."

    "Calling it per title without the WHERE clause turns a per-title derive into a full-library
    rewrite, silently, on every acquisition." Silently is the operative word -- almost every row
    such a call touched would get back the value it already had -- so the neighbour is given a card
    that DISAGREES with what its own `title_meta` resolves to, and a trailer key that disagrees
    with its own `title_video`. An unscoped resolve repairs both and fails here.
    """
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
    # The control: the derive did resolve the card it was asked about, so this is a scope test and
    # not a test that nothing happened.
    card = await db.fetchrow("SELECT overview, tagline, poster_path FROM title WHERE id = $1",
                             ARRIVAL)
    assert card["overview"] and card["tagline"] and card["poster_path"]


async def test_a_document_filed_under_another_title_s_task_is_not_read(db, raw_root):
    """`acquire/board.py:27-44`'s seam, from the derive's side: "a document belongs to a title when
    it is filed under the key of a task that belongs to that title".

    The neighbour's document is real, good and about another film. A derive that read every
    `raw_document` row, or that reconstructed the key from `title.jellyfin_id` instead of asking
    the task table, would write The Expanse's cast and networks onto Arrival.

    THE NEIGHBOUR'S SOURCE IS ONE ARRIVAL DOES NOT HAVE, and that is what makes the assertion
    bite. Seeding the neighbour with a second `tmdb:movie_detail` proves nothing: `DISTINCT ON
    (source, kind, page)` collapses the two into one row, so an unfiltered read returns exactly
    as many documents as a filtered one and every count still agrees.
    """
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


# --- the scraped pages: a guess that landed on another film ---------------------------------------


async def test_a_scraped_page_for_another_film_is_refused_and_writes_nothing(db, raw_root):
    """The real collision. `metacritic_page_alpha_2018.html` is what `movie/alpha` served when the
    corpus asked it for Alpha (2026), and the same guess lands on it for any same-named film.

    THE REFUSAL IS NOT REDUNDANT WITH STAGE 2'S. `sources/rottentomatoes.py:135-139` leaves the
    bytes in the store under `ok = true` on purpose -- "a page that turned out to be another film
    is still the honest record of what the guess returned" -- so every ok document is a document
    the derive will find, and a derive that parsed them all writes another film's scores and
    reviews under this title. §8 stage 7's quote verification cannot catch it afterwards: a quote
    from the wrong film IS a genuine substring of the wrong film's pack.
    """
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

    # WHAT IS LEFT UNDER `platform = 'metacritic'` IS OMDB'S RELAY AND NOT THE PAGE. OMDb files
    # IMDb's, RT's and Metacritic's scores under their own names (`derive/parse.py:570-579`), so
    # the platform is not the same fact as the document: refusing the page must take the page's
    # own two scores and leave the relayed one, which is a row this derive read from elsewhere.
    scores = await db.fetch(
        "SELECT metric, score FROM display.platform_rating"
        " WHERE title_id = $1 AND platform = 'metacritic' ORDER BY metric", ARRIVAL)
    relayed = [row for row in parse.parse_document("omdb", "detail", fixture("omdb_detail.json"))
               .table("platform_rating") if row["source"] == "metacritic"]
    assert [(row["metric"], row["score"]) for row in scores] == [
        (row["metric"], row["value"]) for row in relayed]

    # And the sources that were about this film are all still here, so the refusal is per source.
    assert await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1 AND source = 'trakt'",
        ARRIVAL) > 0


async def test_reviews_fetched_from_a_path_no_accepted_page_proves_are_not_written(db, raw_root):
    """The pairing the test above cannot see: a GOOD `page:main` and reviews from another slug.

    `_refuses` judges the `page:main` document and used to record the verdict per SOURCE, while
    `_DOCUMENTS` resolves `metacritic:page:main` and `metacritic:reviews:critics` to their newest
    ok rows INDEPENDENTLY. So a run in which the page and the reviews were fetched under different
    slugs left `refused` empty and wrote the other film's critic and user reviews into this
    title's pack - which is the state §8 stage 7 cannot see, because a quote from the wrong film
    IS a genuine substring of the wrong film's pack, and which decision 335 then counts toward
    `count(DISTINCT source) >= 2`.

    IT IS AN ORDINARY STATE AND NOT A CONTRIVANCE. `metacritic:reviews` resolves the path itself
    when the page kind failed, `wikidata:resolve` can fill the slug on a later drain, and a review
    view that 404s stores `ok = false` - which leaves the PREVIOUS run's rows the newest ok ones
    for their kind while a fresh, correct `page:main` sits beside them. The url is the tie, and
    the store already carries it. [M5.3 review cycle 1, m53-c1-slug-02]
    """
    await _title(db, ARRIVAL, "Arrival", year=2016, jellyfin_id="arrival-4k")
    await _task(db, ARRIVAL_KEY, ARRIVAL)
    await _document(db, "tmdb", "movie_detail", "tmdb_movie_detail.json", key=ARRIVAL_KEY)
    await _document(db, "metacritic", "page:main", "metacritic_page.html", key=ARRIVAL_KEY)
    # The same two review views, filed under a DIFFERENT metacritic path - the shape a slug that
    # moved between drains leaves behind.
    for kind in ("reviews:critics", "reviews:users"):
        await _document(db, "metacritic", kind, f"metacritic_{kind.replace(':', '_')}.html",
                        key=ARRIVAL_KEY,
                        url=f"https://metacritic.test/{ARRIVAL_KEY}/other-page/{kind[8:]}/")

    report = await rebuild.derive_title(db, ARRIVAL)

    assert "metacritic:page:main" in report.documents, report.documents
    assert not await db.fetch(
        "SELECT 1 FROM review_store.review WHERE title_id = $1 AND source = 'metacritic'", ARRIVAL)
    assert [line for line in report.refused if "reviews" in line], report.refused
    # The page itself is this title's and its scores stay: the drop is per document, not per
    # source, so a proved page is not punished for a review view fetched from somewhere else.
    assert await db.fetchval(
        "SELECT count(*) FROM display.platform_rating WHERE title_id = $1 AND platform ="
        " 'metacritic'", ARRIVAL) > 0


async def test_a_scraped_source_with_no_accepted_page_writes_no_reviews_either(db, raw_root):
    """The same rule where the identity check never ran at all, which closes the other half.

    A fix that only compared a reviews document against an ACCEPTED `page:main` would still write
    the reviews of a source that has no page document under this title - and that is exactly when
    they are least vouched for, because `metacritic:reviews` resolves and fetches a path of its
    own when the page kind failed. `_under` reads a missing page as False for that reason.
    [M5.3 review cycle 1, m53-c1-slug-02]
    """
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
    """`DeriveReport.rows` is "what one derive ... wrote", and `stages.derive` puts it on §6.6's
    board, which `0005_ledger.sql:137` makes a product surface.

    `_replace` used to return `len(rows)` - the length of the list handed to `executemany` - while
    eight of the ten targets carry a conflict clause BECAUSE the ordinary path collides: TMDB names
    a film's primary language once in `spoken_languages` and again in `original_language`, and OMDb
    relays Metacritic's Metascore under `platform = 'metacritic'` while the Metacritic page carries
    its own. So the overstatement is on the happy path rather than at an edge, it is stable run to
    run - which is what makes it the kind of number that gets trusted - and the one place it is
    load-bearing is precisely the relay collision an operator would use the board to look at.
    [M5.3 review cycle 1, m53-rev1-derive-report-rows-overstates-what-landed]
    """
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

    # `person` is deliberately outside the comparison: `DeriveReport.people` is the same number
    # and it counts humans this derive resolved rather than rows in a table scoped to one title.
    claimed = {table: count for table, count in report.rows.items() if table in counted}
    assert set(claimed) >= {"display.platform_rating", "title_language", "title_country"}, (
        f"the fixture does not exercise the three tables that collide: {sorted(claimed)}"
    )
    assert claimed == {table: counted[table] for table in claimed}, (
        f"the board's counts are not the database's: board {claimed}, database {counted}"
    )


async def test_the_refusal_reads_the_cast_this_run_parsed_and_not_the_one_already_stored(
    db, raw_root
):
    """`_refuses` named change 2, and the only thing between the corpus's sweep and a wrong page.

    `mdc/parse/rebuild.py:556-562` builds the cast from `credit` because its sweep runs AFTER the
    whole rebuild wrote it. A per-title derive judging the page before writing sees the previous
    derive's credits, and on a title acquired minutes ago sees none at all -- and with nothing of
    ours to compare, `page_belongs_to_title` returns True, because absence of evidence is not
    evidence. So this is the same wrong page, on a title with no stored credits, refused only
    because the TMDB document in the same pass supplied the cast that contradicts it.
    """
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
    """The corpus's sweep DELETEs what a wrong page produced (`:595-600`); the scope does it here.

    A refused source keeps its label in the delete scope and contributes no rows, which is the same
    repair one statement earlier. The order is the one that happens in practice: the page is
    accepted while the title has no cast, and the cast that contradicts it arrives on the next
    drain.
    """
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
    """The corpus clears it (`:601-602`); this derive does not, and the difference is decision 372.

    §8 stage 2 owns `title`'s identity columns, and `sources/rottentomatoes.py:132-140` writes a
    slug only for a page that passed this same predicate - so a slug on the row was earned by
    evidence, and a refusal at derive time means the evidence moved afterwards. Clearing it here
    would be stage 3 writing identity, which decision 372 spends a `ValueError` to prevent.
    """
    await _seed_arrival(db, (
        ("tmdb", "movie_detail", "tmdb_movie_detail.json", 0),
        ("metacritic", "page:main", "metacritic_page_alpha_2018.html", 0),
    ))
    await db.execute("UPDATE title SET metacritic_slug = 'movie/alpha' WHERE id = $1", ARRIVAL)

    assert (await rebuild.derive_title(db, ARRIVAL)).refused
    assert await db.fetchval(
        "SELECT metacritic_slug FROM title WHERE id = $1", ARRIVAL) == "movie/alpha"


# --- holes, damage and the documents nothing claims -----------------------------------------------


async def test_a_title_whose_required_document_never_arrived_derives_what_it_has(db, raw_root):
    """"A hole in the raw store is not a failure." Decision 334 makes `tmdb:detail` the one
    required source and every other one best-effort, and it makes that stage 2's question.

    A derive that raised here would fail a title whose other seven sources answered perfectly well,
    and whose bytes are already on disk - so the fact is reported and the rows are written.
    """
    await _seed_arrival(db, tuple(
        doc for doc in ARRIVAL_DOCUMENTS if doc[:2] != ("tmdb", "movie_detail")))

    report = await rebuild.derive_title(db, ARRIVAL)

    assert report.required_missing
    assert report.rows["credit"] and report.rows["review"]
    assert "tmdb" not in report.sources
    # The control: with the document present the same property reads the other way.
    await _document(db, "tmdb", "movie_detail", "tmdb_movie_detail.json", key=ARRIVAL_KEY)
    assert not (await rebuild.derive_title(db, ARRIVAL)).required_missing


async def test_a_document_no_parser_claims_is_skipped_and_its_bytes_are_never_read(db, raw_root):
    """`tmdb:find`, `wikipedia:search` and `tvmaze:lookup` are fetched for an identifier stage 2
    has already taken off them, so there is nothing here to derive.

    Asserted by damaging the file rather than by counting: `rawstore.read` refuses a file whose
    digest does not match its row, so a derive that read this document would raise instead of
    ignoring it - which is a stronger statement than "no rows appeared".
    """
    stored = await _seed_arrival(db)
    path = rawstore.resolve(await db.fetchval(
        "SELECT content_path FROM raw_document WHERE id = $1", stored[("tmdb", "find")]))
    path.write_bytes(gzip.compress(b"not the document this row names"))

    report = await rebuild.derive_title(db, ARRIVAL)

    assert "tmdb:find" not in report.documents
    assert report.rows["title_meta"] == 4


async def test_a_document_whose_file_is_not_the_one_its_row_names_stops_the_derive(db, raw_root):
    """Decision 361's `OSError`, deliberately not caught. `acquire/rawstore.py:430-441` says what
    it means: "the file under that path is not this row's document".

    Under decision 162 content seeds once, so what a derive writes from somebody else's bytes
    cannot be taken back - and the whole derive is one transaction, so the refusal leaves the
    title exactly as the last good derive left it rather than half-rewritten.
    """
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
    """`derive/gate.measure` takes the same line and gives the reason: a missing title and a title
    with nothing derived are different facts, and zeros would put a count on §6.6's board for a row
    that is absent."""
    with pytest.raises(LookupError) as caught:
        await rebuild.derive_title(db, ARRIVAL)
    assert str(ARRIVAL) in str(caught.value)


async def test_a_title_with_no_documents_at_all_derives_nothing_and_does_not_raise(db, raw_root):
    """The state every acquired title passes through between stage 1 and stage 2."""
    await _title(db, SPARSE, "Unfetched", year=2026)
    await _task(db, SPARSE_KEY, SPARSE)

    report = await rebuild.derive_title(db, SPARSE)

    assert report.documents == () and report.rows == {} and report.required_missing
    assert await db.fetchval("SELECT count(*) FROM title_meta WHERE title_id = $1", SPARSE) == 0


# --- the promise the raw store exists for ---------------------------------------------------------


async def test_the_derive_opens_no_socket(db, raw_root, monkeypatch):
    """§8: "All fetched bytes land in the app's own raw store, so re-parsing is free forever."

    Free means no request. The dynamic half is here because the static half -- `derive/` imports no
    transport -- is `test_derive_parse.py`'s, and a module that reached the network through a
    helper it imported would pass that one.
    """
    await _seed_arrival(db)

    def no_connect(*args, **kwargs):
        raise AssertionError(
            "the derive opened a socket: re-parsing reads the store and never re-fetches, and the "
            "hosts it would reach are eight third parties on a household's own IP address"
        )

    # `socket.socket.connect` and not `socket.socket`. Replacing the CLASS breaks the already-open
    # asyncpg connection this test derives through - Windows' proactor loop does
    # `isinstance(sock, socket.socket)` on every transfer - so the guard would fail for a reason
    # that has nothing to do with the derive. The method is the thing a new outbound connection
    # calls, and this one is already established.
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connect)
    report = await rebuild.derive_title(db, ARRIVAL)
    assert report.rows["credit"]


async def test_the_documents_are_read_newest_first_and_per_page(db, raw_root):
    """Two rules in one read, and `sources/trakt.py:160-162` names the second: comments are stored
    "Unique per (sort, page) or the raw store's newest-per-page view collapses two sorts into one
    document".

    So `page` is part of the key a derive reads by, and `rawstore.latest` has no room for it. The
    first half is `latest`'s own `ok`-and-newest rule: a second capture of one page supersedes the
    first rather than adding to it.
    """
    await _seed_arrival(db, (("trakt", "comments", "trakt_comments.json", 1),))
    # A genuinely different page: `COMMENT_SORTS` fetches `likes`, `lowest` and `highest`, so the
    # second page carries other comments. The same bytes twice would prove nothing here, because
    # `_reviews` dedupes a source's reviews by fingerprint and would collapse them.
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

    # A second capture of page 1 - different bytes, same page - replaces it and adds nothing.
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
    """`derive/reviews.py:116-129` exists for this: "`_mc_from_json` already dedupes on `body[:120]`
    within a document but cannot see across two", and Metacritic's critic and user pages overlap.

    `review_store.review` has a `bigserial` key and no natural one, so the database refuses
    nothing - and decision 335's gate sums `word_count`, so a review stored twice is a title
    clearing stage 4 on one review filed under two urls.

    THE `page:main` DOCUMENT IS IN THE SEED AND HAS TO BE. A scraped source's review documents are
    taken only when they hang off a `page:main` this title's identity check accepted, so a fixture
    carrying reviews alone derives nothing at all - which is the shape a real acquisition cannot
    produce either, because `metacritic:reviews` resolves and proves the path before it fetches a
    view. [M5.3 review cycle 1, m53-c1-slug-02]
    """
    await _seed_arrival(db, (
        ("metacritic", "page:main", "metacritic_page.html", 0),
        ("metacritic", "reviews:critics", "metacritic_reviews_critics.html", 0),
        ("metacritic", "reviews:users", "metacritic_reviews_critics.html", 1),
    ))
    await rebuild.derive_title(db, ARRIVAL)

    bodies = [r["body"] for r in await db.fetch(
        "SELECT body FROM review_store.review WHERE title_id = $1", ARRIVAL)]
    assert bodies and len(bodies) == len(set(bodies))


# The prefix each routed source's captured responses are FILED under, where the filesystem does
# not repeat the source's own name. `mpst_bulk`'s are `mpst_meta.json`, which the `split("_")`
# below already reaches; Rotten Tomatoes' are `rt_page.html` and `rt_page_unscored.html`, and
# nothing does - `"rt_page.html".startswith("rottentomatoes")` is False, which is why this guard
# parsed eleven of its twelve routed parsers and never once ran `parse_rt_page`. That is the one
# source the guard could least afford to miss: RT is one of the two scraped by a guessed slug, so
# its markup moves under the app without warning and it is the likeliest to start emitting a table
# it did not emit yesterday - and `rottentomatoes` is not in `AWARD_SOURCES`, so an award row from
# it would be dropped by every derive that did not also read OMDb.
#
# A table and not a rename, because the fixture names are what the captures were saved as and
# `test_derive_parse.py` reads them by those names too. The completeness assertion below is what
# keeps the table honest: a source whose captures match neither spelling reddens this guard rather
# than dropping out of it silently, which is the failure being repaired.
# [M5.3 review cycle 1, m53-rev1-award-guard-never-parses-the-rt-page]
_FIXTURE_PREFIX = {"rottentomatoes": "rt"}


def test_no_source_outside_the_award_guard_emits_an_award():
    """`award` has no source column, so `AWARD_SOURCES` is the only thing deciding whether the
    derive may delete this title's awards - and a source missing from it has its awards silently
    dropped rather than refused.

    The answer is read back off the captured responses instead of being restated: every fixture
    under `fixtures/sources/` is parsed through the same dispatch the derive uses, and the sources
    that produced an `award` row must be inside the guard. A parser that starts emitting one under
    a new source reddens this on the day it lands.

    WHICH IS ONLY TRUE IF EVERY ROUTED PARSER IS ACTUALLY CALLED, and for one review cycle one of
    them was not: the fixture filter matched the SOURCE's own name, and Rotten Tomatoes' captures
    are `rt_*`. So `parse_rt_page` was never invoked here, `emitting` could not contain
    `rottentomatoes`, and the guard was structurally incapable of seeing the event it exists for
    on one of the two sources scraped by a guessed slug. The exercised set is now asserted against
    the registry, which is the half that keeps the reading honest: a parser this guard does not
    call is a source it cannot answer for, and the next fixture named unlike its source fails here
    instead of disappearing from the measurement.
    [M5.3 review cycle 1, m53-rev1-award-guard-never-parses-the-rt-page]
    """
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


# --- decision 372's other side --------------------------------------------------------------------


async def test_the_derive_writes_the_card_fields_and_the_three_title_columns_below_them(
    db, raw_root
):
    """Decision 372: "every field a card renders ... is written by stage 3 from the raw store".

    `year`, `runtime_min` and `original_language` are on `title` rather than in `title_meta`, so
    `resolve_title_fields` does not reach them and the derive resolves them by the same `best()`.
    """
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
    """`importer/meta.py:157-159`'s sentence, one column over: "blanking one would also overwrite
    whatever §8's acquisition path wrote for a title the corpus never shipped".

    `acquire/stages._mint` takes `year` and `runtime_min` off the Jellyfin item the household owns,
    and Wikipedia's meta row carries neither - so an overwrite here would lose the library's own
    answer to a source that merely did not have one.
    """
    await _seed_arrival(db, (("wikipedia", "article", "wikipedia_article.json", 0),))
    await db.execute("UPDATE title SET runtime_min = 116 WHERE id = $1", ARRIVAL)

    await rebuild.derive_title(db, ARRIVAL)

    row = await db.fetchrow("SELECT year, runtime_min FROM title WHERE id = $1", ARRIVAL)
    assert (row["year"], row["runtime_min"]) == (2016, 116)


async def test_an_identifier_the_parse_yielded_and_no_adapter_wrote_is_filled_never_clobbered(
    db, raw_root
):
    """Decision 372's other side, for the two columns 0026 added.

    The write goes through `sources/_ids.set_ids`, which is the one implementation of
    fill-never-clobber and refuses a card field by name - so a derive cannot use this door to write
    something stage 2 owns. The clobber half is asserted with a household-supplied article title,
    because that is the case `sources/wikipedia.py:224-226` says COALESCE is protecting.
    """
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
    """`display.platform_rating`'s key is `(title_id, platform, metric)` and two documents of one
    run land on it: OMDb relays Metacritic's Metascore under `platform = 'metacritic'` while the
    Metacritic page carries its own.

    The corpus writes this table through `INSERT OR REPLACE` (`mdc/parse/rebuild.py:53-54`), so
    last write wins; the derive reads documents in a deterministic order and takes the scraped
    pages last, which means the source's own page beats a relay of it. What matters for §14 risk 5
    is that it is the same one on every run, which the first test in this file measures.
    """
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


# --- the two curated ledgers, at their own points --------------------------------------------------


async def test_a_title_carrying_both_curated_ledgers_derives_twice_and_keeps_both(db, raw_root):
    """§14 risk 5's second invariant, over the first: "adjudication- AND corrections-at-derivation".

    The acceptance test at the top of this file measures a derive that carries NO curated row, which
    is the vacuous half: an applier that does nothing passes it. This one gives the title one row in
    each ledger and asserts the same snapshot equality afterwards, so the two invariants are held
    together rather than in turn -- a correction re-applied by deleting and re-inserting the credits
    it did not name would satisfy "the correction is still there" and fail this.

    BOTH CURATED ROWS ARE THE HOUSEHOLD'S, which is decision 326's other half and the one no bundle
    can supply. §6.6's editors (M5.6) write Postgres rows carrying `origin = 'household'`, and the
    appliers must not filter on that column -- a derive that applied only the bundle's rows would
    make the editors write to a table nothing reads, which is `credit_correction`'s own situation
    one milestone ago. The bundle-shipped side of both ledgers is asserted in
    `test_derive_ledgers.py`, which loads the real files through the importer's own loaders.

    ARRIVAL is in this app's half of §4.1's id partition, and that is the point rather than a
    convenience: a curated ledger authored upstream can only ever name a corpus id, so an acquired
    title is exactly the case whose curated fixes have nowhere to come from but the household.
    """
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
