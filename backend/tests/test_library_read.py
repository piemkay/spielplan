"""The §6.0/§6.4 read layer against a real Postgres 16. Spec v2.1 §6.0, §6.3, §6.4, §4.1
rules 1, 2 and 5, §4.3, §10.

M4.9 found seven defects in this layer and every one of them is a fact about a SQL statement, so
almost every test here is an integration test. A unit test over the builder would have agreed
with the builder: the shipped DNA predicate was self-consistent and matched nothing a person can
type, the pagination bug needs a real planner and a real UPDATE between two fetches, and the LIKE
metacharacters only mean anything to Postgres.

Five things this file pins, each with the finding it closes:

* **the DNA predicate reads the shipped id** (finding 2). §4.3's vocabulary id IS `facet.term`,
  so `mood.cosy` is what `dna_tag.term` holds. The predicate used to build `facet || '.' || term`
  on top of that, which matched `mood_tone.mood.cosy` and nothing else.
* **every DNA read is scoped to one vocabulary** (finding 10). `home/why.py` scoped the shelves
  and said why; the card, the catalog/Rank filter and §6.4's wander neighbours did not.
* **the catalogue is a total order and its search is literal** (findings 11 and 12). Both are
  invisible until the data has ties or metacharacters in it, which the corpus does: 584 tie
  groups over 1,175 titles, 340 NULL-year titles, and titles with a percent sign in the name.
* **two listing routes partition by kind and bound their limit** (findings 13 and 14).
* **a projected term never outranks an extracted one** (finding 20), over an imported bundle
  and in one expression that Home and Tonight both read.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from spielplan.db import dna_terms, library
from spielplan.home import why as why_mod
from spielplan.importer import bundle as bundle_import
from spielplan.tonight import dna as tonight_dna
from tests.fixtures import make_bundle as fx

VOCAB = "v1"


async def _titles(db, rows) -> None:
    """`(id, kind, name, year)` tuples, owned, because the catalog lists owned and unowned
    alike and nothing here turns on the flag."""
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, $4, true)", rows
    )


async def _vocabulary(db, version: str, *, facets=("mood",)) -> None:
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, 0)",
        version, len(facets),
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(version, facet, i) for i, facet in enumerate(facets)],
    )


async def _tag(db, title_id: int, term: str, *, version: str = VOCAB, salience: int = 3) -> None:
    """The extracted tier, stored the way the loader stores it: the whole `facet.term` id in
    `term`, the id's own prefix in `facet` (`importer/dna.app_facet`), `provider` empty."""
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider) "
        "VALUES ($1, $2, $3, $4, $5, '')",
        title_id, version, term, term.split(".", 1)[0], salience,
    )


async def _projected(db, title_id: int, term: str, *, version: str = VOCAB, weight=1) -> None:
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1, $2, $3, $4, $5, 'keyword')",
        title_id, version, term, term.split(".", 1)[0], weight,
    )


async def _matching(db, dna: str, *, kind: str = "movie") -> list[int]:
    """The ids §6.3's DNA filter admits, through the builder the catalog and the Rank board
    share. Run against `title` directly rather than through a fitted board: the predicate is
    what is under test, and a ledger fit would only add a second reason for a red line."""
    clause, args = library.rank_filters(
        kind=kind, user_id=1, filters=library.RankFilters(dna=dna)
    )
    rows = await db.fetch(f"SELECT t.id FROM title t WHERE {clause} ORDER BY t.id", *args)
    return [int(r["id"]) for r in rows]


@pytest.fixture
async def tagged(db):
    """Three films, one vocabulary, both tiers, one term carried in both (§4.1 rule 1)."""
    await _titles(db, [(1, "movie", "Heat", 1995), (2, "movie", "Se7en", 1995),
                       (3, "movie", "Paddington 2", 2017)])
    await _vocabulary(db, VOCAB, facets=("mood", "sensibility"))
    await _tag(db, 1, "mood.cosy")
    await _tag(db, 2, "sensibility.bleak")
    await _projected(db, 1, "mood.cosy")
    await _projected(db, 3, "mood.cosy")


# --- finding 2: the shipped term id, bare and qualified ---------------------------------


async def test_the_bare_and_the_qualified_dna_term_select_the_same_rows(db, tagged):
    """§6.3 writes its own example qualified — "show only `mood.cosy`" — and §6.3's filter
    control invites the bare half. Both have to be the same predicate.

    The third assertion is the one that stops the fix from being "match anything": a different
    facet in front of the same bare term is a different id, and selects nothing.
    """
    assert await _matching(db, "mood.cosy") == [1, 3]
    assert await _matching(db, "cosy") == [1, 3]
    assert await _matching(db, "sensibility.bleak") == [2]
    assert await _matching(db, "bleak") == [2]
    assert await _matching(db, "pacing.cosy") == []

    # The string the shipped predicate matched, and the only one it matched: `facet || '.' ||
    # term` over a term that is already qualified. Nobody can type it and nothing must return
    # for it. [M4.9 finding 2]
    assert await _matching(db, "mood.mood.cosy") == []


async def test_dna_tiers_are_returned_for_both_spellings(db, tagged):
    """§4.1 rule 1: a survivor of a DNA filter says which tier admitted it.

    `dna_tiers_for` is a second copy of the predicate — it must agree with the filter about
    every spelling, or the board badges a title under a term that did not select it.
    """
    for spelling in ("cosy", "mood.cosy"):
        matched = await library.dna_tiers_for(db, title_ids=[1, 2, 3], dna=spelling)
        assert matched[1] == ["extracted", "projected"], "a pair in both tiers reports both"
        assert matched[3] == ["projected"]
        assert 2 not in matched


# --- finding 10: one vocabulary, everywhere ---------------------------------------------


@pytest.fixture
async def two_vocabularies(db):
    """A household that has imported two bundles — §10's own scenario.

    `v2` is the newer row by `imported_at`, so it is the active one. Title 1 carries a term in
    each vocabulary; title 2 shares the new one and title 3 shares the old one, which is what
    makes the wander query's answer a fact about the version rather than about the term.
    """
    await _titles(db, [(1, "movie", "Heat", 1995), (2, "movie", "Se7en", 1995),
                       (3, "movie", "Collateral", 2004)])
    await _vocabulary(db, "v1")
    await _vocabulary(db, "v2")
    await db.execute(
        "UPDATE dna_vocabulary SET imported_at = now() - interval '1 day' WHERE version = 'v1'"
    )
    for title_id in (1, 3):
        await _tag(db, title_id, "mood.superseded", version="v1")
        await _projected(db, title_id, "mood.superseded_projection", version="v1")
    for title_id in (1, 2):
        await _tag(db, title_id, "mood.current", version="v2")
        await _projected(db, title_id, "mood.current_projection", version="v2")


async def test_two_vocabularies_do_not_mix_on_the_card_the_filter_or_the_neighbours(
    db, app, two_vocabularies
):
    """§10 + decision 163: a re-import leaves two vocabularies coexisting, and `home/why.py`
    already refused to mix them because "a term's facet and gloss are version-scoped".

    Three readers had no version predicate at all. All three are asked here, because the failure
    is not that one of them is wrong — it is that a household would see two vocabularies on one
    screen, and any one of them leaking is enough for that.
    """
    active = await dna_terms.active_version(db)
    assert active == "v2", "the newest imported row is the active vocabulary"
    assert await why_mod.vocabulary_version(db) == active, "one notion, not two"
    assert await tonight_dna.active_version(db) == active

    # 1. the §6.0 card
    card = await library.dna_for(db, 1, version=active)
    assert [t["term"] for t in card["extracted"]] == ["mood.current"]
    assert [t["term"] for t in card["projected"]] == ["mood.current_projection"]

    # 2. the catalog/Rank DNA filter, both tiers
    assert await _matching(db, "current") == [1, 2]
    assert await _matching(db, "current_projection") == [1, 2]
    assert await _matching(db, "superseded") == []
    assert await _matching(db, "superseded_projection") == []

    # 3. §6.4's wander. Title 3 shares only the superseded term with title 1, so it is the row
    # that appears if and only if the neighbour queries have lost the version.
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    response = await client.get("/api/titles/1/similar-by-term?kind=movie")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [n["title_id"] for n in body["extracted"]] == [2]
    assert [n["title_id"] for n in body["projected"]] == [2]


async def test_two_vocabularies_written_in_one_transaction_still_resolve_to_one(db):
    """The tie `ACTIVE_VERSION`'s second sort key exists for, and the tie it cannot be.

    `imported_at` defaults to `now()`, which is transaction-stable: two DIFFERENT versions
    written inside one transaction carry the same timestamp to the microsecond, so ordering on
    `imported_at` alone leaves the row Postgres returns to the plan — and the card, the shelves
    and the catalog filter could then resolve different vocabularies inside one request. That is
    the case `version DESC` settles.

    The case the module's comment used to name — two rows sharing a version string — cannot
    arise at all, and the last assertion is that refusal rather than a claim about it:
    `dna_vocabulary.version` is `text PRIMARY KEY` (0004_dna.sql:18) and `importer/dna.py:134`
    upserts on it, so a second bundle carrying `v1` refreshes the one row. A tie-break written
    for an impossible tie teaches the next reader that the key it is scoped by is not unique,
    which is the fact the whole version-scoping change turns on.
    [M4.9 review cycle 1: M49-REV1-04]
    """
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) "
        "VALUES ('v20260828', 1, 0), ('v20260901', 1, 0)"
    )
    stamps = {r["imported_at"] for r in await db.fetch("SELECT imported_at FROM dna_vocabulary")}
    assert len(stamps) == 1, (
        f"one transaction, one now(): {stamps} - if these ever differ, `imported_at DESC` "
        "settles it alone and this test is measuring the wrong thing"
    )
    assert await dna_terms.active_version(db) == "v20260901"

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await db.execute(
            "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 0), "
            "('v1', 1, 0)"
        )


# --- finding 11: pagination is a total order --------------------------------------------


async def test_paging_over_ties_with_a_rewrite_between_pages_loses_and_repeats_nothing(db):
    """§6.0 pages this list with LIMIT/OFFSET and the client appends, so the sort has to be a
    TOTAL order or the person reads a catalogue with holes in it.

    600 rows tied on `(year, lower(name))` — the corpus's own worst case is 584 tie groups over
    1,175 titles plus 340 NULL-year titles that tie on the first key outright — walked in pages
    of 100 with a handful of rows rewritten between every page, which is what the nightly
    reconcile and §7.2's sync do while somebody is scrolling. An UPDATE moves the row to the end
    of the heap, so an untied sort re-orders around it and the page boundaries slide.
    """
    total = 600
    page = 100
    await _titles(db, [(i, "movie", "Tied", None) for i in range(1, total + 1)])

    seen: list[int] = []
    for offset in range(0, total, page):
        rows, reported = await library.list_titles(
            db, kinds=["movie"], limit=page, offset=offset
        )
        assert reported == total
        seen += [int(r["id"]) for r in rows]
        # Rewritten between pages, from the part already read: the row keeps its sort keys and
        # changes its physical position, which is exactly the case an OFFSET cannot survive
        # without a tie-break.
        await db.execute(
            "UPDATE title SET runtime_min = COALESCE(runtime_min, 0) + 1 "
            "WHERE id = ANY($1::int[])",
            [i for i in range(offset + 1, offset + 6)],
        )

    duplicated = sorted({i for i in seen if seen.count(i) > 1})
    missing = sorted(set(range(1, total + 1)) - set(seen))
    assert not duplicated, f"{len(duplicated)} titles were returned on two pages"
    assert not missing, f"{len(missing)} titles were never returned"
    assert len(seen) == total


# --- finding 12: LIKE metacharacters are text -------------------------------------------


@pytest.fixture
async def searchable(db):
    """Two of the three characters LIKE reserves, in real title names, plus a backslash — which
    is Postgres's default escape character and so the one that changes what a needle means
    rather than merely widening it.

    `Room 100` and `Top 1000` carry the digits without the percent sign, one per kind. They are
    what makes the count line falsifiable: `100%` reads as "contains 100" while the wildcard is
    live and as "contains 100%" once it is not, and §6.0's hidden-by-kind count is computed from
    the same predicate as the listing, so it was wrong on the other side of the toggle.
    """
    await _titles(db, [
        (1, "movie", "Heat", 1995),
        (2, "movie", "100% Wolf", 2020),
        (3, "movie", "Back\\Slash", 1999),
        (4, "series", "100% Series", 2021),
        (5, "movie", "Room 100", 2011),
        (6, "series", "Top 1000", 2018),
    ])


async def _search(db, q: str, *, kinds=("movie",)) -> list[str]:
    rows, _total = await library.list_titles(db, kinds=list(kinds), q=q, limit=60)
    return sorted(r["name"] for r in rows)


async def test_like_metacharacters_in_the_search_needle_are_text(db, searchable):
    """§6.0: "filter/search on title/alias". The needle was interpolated into a LIKE pattern
    raw, so the wildcards kept their meaning: `%` returned the whole catalogue, `_` matched
    every one-character name, `h_at` returned *Heat*, and `100% Wolf` could not be searched for.

    §4.1 rule 8's distinction is the point — escaping is how the character the person typed
    stays that character, and it is not cleaning: `100%` still finds `100% Wolf`.
    """
    assert await _search(db, "%") == ["100% Wolf"]
    assert await _search(db, "_") == []
    assert await _search(db, "h_at") == []
    assert await _search(db, "100%") == ["100% Wolf"]
    assert await _search(db, "\\") == ["Back\\Slash"]

    # The half that must not regress: a needle with no metacharacters in it still matches as a
    # substring, case-insensitively, which is all §6.0 asks for.
    assert await _search(db, "heat") == ["Heat"]
    assert await _search(db, "100") == ["100% Wolf", "Room 100"]


async def test_the_hidden_by_kind_count_agrees_with_the_escaped_listing(db, searchable):
    """§6.0's count line names what the toggle is hiding, and it is computed from the same
    predicate as the listing — so an unescaped needle made both of them wrong, on both sides of
    the toggle. `100%` read as "contains 100" and pulled in `Room 100` and `Top 1000`.
    """
    rows, total = await library.list_titles(db, kinds=["movie"], q="100%", limit=60)
    assert [r["name"] for r in rows] == ["100% Wolf"]
    assert total == 1

    hidden = await library.count_by_kind(db, exclude=["movie"], q="100%")
    assert hidden == {"series": 1}, "the count may only promise what the toggle can reveal"

    # The toggle keeps its promise: turning Series on reveals exactly what was counted.
    both, _ = await library.list_titles(db, kinds=["movie", "series"], q="100%", limit=60)
    assert len(both) == total + hidden["series"]


# --- findings 13 and 14: two routes that ranked without a partition ----------------------


@pytest.fixture
async def wanderable(db, app):
    """One film and one series sharing a DNA term and a credited person, and an admin session.

    Both routes select `t.kind` into their SELECT list and into no predicate, so a fixture with
    one kind in it cannot fail them. The shared term and the shared person are what make the
    unpartitioned answer visible.
    """
    await _titles(db, [(1, "movie", "Heat", 1995), (2, "movie", "Collateral", 2004),
                       (3, "series", "The Wire", 2002)])
    await _vocabulary(db, VOCAB)
    for title_id in (1, 2, 3):
        await _tag(db, title_id, "mood.dread")
    await db.execute("INSERT INTO person (id, name) VALUES (7, 'Michael Mann')")
    await db.executemany(
        "INSERT INTO credit (title_id, person_id, department, job, source) "
        "VALUES ($1, 7, 'Directing', 'Director', 'tmdb')",
        [(1,), (2,), (3,)],
    )
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    return client


async def test_similar_by_term_and_filmography_require_a_kind_selection(wanderable):
    """§4.1 rule 5: "every ranking surface partitions by it (measured: the unpartitioned crowd
    top-10 is 8/10 TV series)".

    Both routes rank — one by shared-term count, one by year — and neither had a kind predicate,
    so a wander from a film and a tap on a director's name both answered one interleaved list.
    The selection is the caller's and never the anchor's: asking from a film for both kinds has
    to return both, or the fix is a different rule wearing rule 5's name.
    """
    client = wanderable
    for path in ("/api/titles/1/similar-by-term", "/api/people/7"):
        assert (await client.get(path)).status_code == 422, f"{path} answered without a kind"
        assert (await client.get(f"{path}?kind=")).status_code == 422
        assert (await client.get(f"{path}?kind=documentary")).status_code == 422

    films = (await client.get("/api/titles/1/similar-by-term?kind=movie")).json()
    assert {n["kind"] for n in films["extracted"]} == {"movie"}
    assert [n["title_id"] for n in films["extracted"]] == [2]

    both = (await client.get("/api/titles/1/similar-by-term?kind=movie&kind=series")).json()
    assert {n["title_id"] for n in both["extracted"]} == {2, 3}
    assert both["kinds"] == ["movie", "series"]

    filmography = (await client.get("/api/people/7?kind=series")).json()["filmography"]
    assert [f["kind"] for f in filmography] == ["series"]
    assert [f["id"] for f in filmography] == [3]

    complete = (await client.get("/api/people/7?kind=movie&kind=series")).json()["filmography"]
    assert {f["id"] for f in complete} == {1, 2, 3}


async def test_similar_by_terms_limit_is_bounded_before_it_reaches_postgres(wanderable):
    """`limit` reached `LIMIT $2` unvalidated: `-1` was a 500 through the PostgresError handler
    and a large one a 200 over the whole join. The sibling catalog route has been bounded since
    M0, and §6.4's wander takes the tighter ceiling. [M4.9 finding 14]"""
    client = wanderable
    for bad in (0, -1, 61, 100000):
        response = await client.get(f"/api/titles/1/similar-by-term?kind=movie&limit={bad}")
        assert response.status_code == 422, f"limit={bad} was not refused ({response.status_code})"

    for good in (1, 12, 60):
        response = await client.get(f"/api/titles/1/similar-by-term?kind=movie&limit={good}")
        assert response.status_code == 200, response.text


async def test_two_providers_naming_one_term_are_one_shared_term_on_the_wander(db, wanderable):
    """§6.4: an edge is labelled with the terms it rides on, and ranked by how many there are.

    §6.6's parallel extraction mode writes one `dna_tag` row PER PROVIDER for one term —
    `0004_dna.sql:83` keys the table (title_id, version, term, provider) and 0018 section 2 is
    what finally arms that index — so the rows below are two legal rows, not a broken fixture.
    Rule 1 keeps both; what a READ of them owes is the question the reader asked, which is how
    many TERMS two titles share.

    Un-deduplicated the join multiplies on both sides: the film shares one term through two
    providers and scores four, the series shares two genuinely different terms and scores three,
    so the ranking inverts and the label reads one term four times. `placement/features._dna_x`
    already writes the DISTINCT this asserts, for the same reason and with the same citation.
    [M4.9 review cycle 1: M49-REV1-03]
    """
    client = wanderable
    # A second term the series shares with the anchor and the film does not.
    for title_id in (1, 3):
        await _tag(db, title_id, "mood.cosy")
    # ...and a second provider's opinion of the term the film does share.
    for title_id in (1, 2):
        await db.execute(
            "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider) "
            "VALUES ($1, $2, 'mood.dread', 'mood', 2, 'a-second-provider')",
            title_id, VOCAB,
        )

    body = (await client.get("/api/titles/1/similar-by-term?kind=movie&kind=series")).json()
    edges = {n["title_id"]: n for n in body["extracted"]}
    assert edges[2]["shared"] == 1, "one term, named by two providers, is one shared term"
    assert edges[2]["via"] == ["mood.dread"], "§6.4's label names the terms, not the rows"
    assert edges[3]["shared"] == 2 and edges[3]["via"] == ["mood.cosy", "mood.dread"]
    assert [n["title_id"] for n in body["extracted"]] == [3, 2], (
        "two providers agreeing about one term outranked a title sharing two different ones"
    )


async def test_the_selected_kinds_share_one_ranking_and_one_limit(db, wanderable):
    """Decision 198: on this route the caller's selection IS rule 5's partition, not a section.

    Step 5.4 prescribes `AND t.kind = ANY($n)`, which is a filter: the selected kinds land in
    one statement under one `ORDER BY count(DISTINCT o.term) DESC, t.name` and one `LIMIT`. The
    coverage row read, for a milestone, "never return movies and series interleaved" — a
    sentence the test above falsifies in its own assertions, since asking from a film for both
    kinds has to answer with both.

    What the shape costs is measured here rather than promised away: two series sharing two of
    the anchor's terms fill a limit of two and push out the film that shares one, so a caller
    asking for both kinds is answered entirely in one, with no count line saying so. That is
    §4.1 rule 5's own measured failure ("the unpartitioned crowd top-10 is 8/10 TV series")
    surviving on a surface §6.4 does not render yet — and it is a fact about the answer, not
    about the anchor, which is why the film is asked for again on the line below.
    [M4.9 review cycle 1: M49-REV1-02]
    """
    client = wanderable
    await _titles(db, [(4, "series", "Deadwood", 2004)])
    await _tag(db, 4, "mood.dread")
    # A second term the anchor carries and only the series share.
    for title_id in (1, 3, 4):
        await _tag(db, title_id, "mood.cosy")

    crowded = (
        await client.get("/api/titles/1/similar-by-term?kind=movie&kind=series&limit=2")
    ).json()["extracted"]
    assert [n["kind"] for n in crowded] == ["series", "series"], (
        "the two kinds are ranked together, so the louder one can take the whole answer"
    )

    # The film was eligible throughout: the limit is what hid it, not the predicate.
    films = (
        await client.get("/api/titles/1/similar-by-term?kind=movie&limit=2")
    ).json()["extracted"]
    assert [n["title_id"] for n in films] == [2]


# --- finding 20: one term-weight expression, and the two bands cannot cross --------------


async def test_no_projected_term_outweighs_any_extracted_term(db, tmp_path):
    """§4.1 rule 1 calls the extracted tier quote-verified and the projected tier inferred, so
    a projection must never be the loudest term on a title — it is the term `terms_carried_by`
    names first in §6.2 step 7's match line and the term Home's why-line names first.

    `importer/dna.py` stores `n_sources` in `dna_projected.weight` and `0004_dna.sql:127-128`
    re-exposes that column to the sanctioned view as `confidence`, so the shipped
    `0.30 * COALESCE(d.confidence, 0.5)` ran to 2.40 against an extracted cap of 1.00. Measured
    on the real bundle: of 400 random tagged movie titles, 37 of the 48 carrying an extracted
    term had a projection as their loudest.

    The fixture's generated pool tops out at two sources, which is inside the band even under
    the old expression — so the corpus's own extreme (eight) is written in afterwards. That row
    is the one that reproduces 2.40, and the assertion is repeated over it rather than replacing
    the first: a bound that only holds on small numbers is not a bound.
    """
    fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(tmp_path / "bundle"), tmp_path / "artifacts"
    )
    assert report.ok, report.render()

    async def bands() -> dict[str, tuple[float, float]]:
        rows = await db.fetch(
            f"""
            SELECT d.tier, min({dna_terms.TERM_WEIGHT}) AS lo, max({dna_terms.TERM_WEIGHT}) AS hi
              FROM dna_tagged d
             GROUP BY d.tier
            """
        )
        return {r["tier"]: (float(r["lo"]), float(r["hi"])) for r in rows}

    measured = await bands()
    assert set(measured) == {"extracted", "projected"}
    assert measured["projected"][1] < measured["extracted"][0]

    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES (1, 'v1', 'mood.dread', 'mood', 8, 'keyword')"
    )
    widest = await bands()
    assert widest["projected"][1] < widest["extracted"][0], (
        "eight sources is the corpus's maximum and produced 2.40 under the old expression"
    )
    # Decision 188's numbers, so a later edit that keeps the ordering by flattening the column
    # is caught too: the form saturates, it does not clamp.
    assert widest["projected"][0] == pytest.approx(0.15, abs=0.005)
    assert widest["projected"][1] == pytest.approx(0.267, abs=0.005)
    assert widest["extracted"][0] >= 0.733


async def test_a_negative_projected_weight_neither_raises_nor_outranks_an_extracted_term(db):
    """Decision 188's form is bounded and monotone only for `c >= 0`, and nothing bounds `c`.

    `dna_projected.weight` is `real` with no CHECK (`0004_dna.sql:110`), 0018 adds none, and
    `importer/dna.load_projected` copies the bundle's `n_sources` straight through — while
    `importer/validate.py` range-checks `dna_tag.salience` and nothing on this tier. §8 stage 7
    makes the bundle a trust boundary, so the two values below are what an upstream count can
    plausibly arrive as: `-1`, the classic unknown sentinel, and a fraction below it.

    Unclamped, `c / (1 + c)` answers them with a division by zero and with 1.80 — a 500 out of
    every Home shelf build and every Tonight `vectors_for`, or an inferred term louder than the
    extracted cap of 1.00, which is finding 20 reopened through a path the linear form it
    replaced could not reach. `GREATEST(..., 0.0)` is the precondition, written into the SQL.
    [M4.9 review cycle 1: M49-D188-03]
    """
    await _titles(db, [(1, "movie", "Heat", 1995)])
    await _vocabulary(db, VOCAB)
    await _tag(db, 1, "mood.dread", salience=1)
    await _projected(db, 1, "mood.cosy", weight=-1)
    await _projected(db, 1, "mood.bleak", weight=-1.2)
    await _projected(db, 1, "mood.warm", weight=8)

    rows = await db.fetch(
        f"SELECT d.term, d.tier, {dna_terms.TERM_WEIGHT} AS w FROM dna_tagged d ORDER BY d.term"
    )
    weights = {r["term"]: float(r["w"]) for r in rows}
    extracted_floor = min(float(r["w"]) for r in rows if r["tier"] == "extracted")
    projected = {t: w for t, w in weights.items() if t != "mood.dread"}
    assert all(0.0 <= w < extracted_floor for w in projected.values()), (
        f"a projection outside the band: {projected} against an extracted floor of "
        f"{extracted_floor}"
    )
    # Saturating still, above the floor: the clamp must not have flattened the real counts.
    assert weights["mood.warm"] > weights["mood.cosy"]


def test_home_and_tonight_read_one_term_weight_expression():
    """The expression was copied verbatim into `home/why.py` and `tonight/dna.py`, and both
    copies carried a comment claiming ranges the shipped data had never produced.

    Asserted on identity rather than on text: `dna_tagged` exposes `dna_projected.weight` as
    `confidence`, so changing the fragment moves Tonight's tilt vectors, its authored-axis
    positions, `terms_carried_by` and the winner card's match lines as well as Home's why-lines.
    A guard written over one reader would let the other drift back.
    """
    assert why_mod.TERM_RANK is dna_terms.TERM_WEIGHT
    assert tonight_dna.TERM_WEIGHT is dna_terms.TERM_WEIGHT

    # Decision 188's saturating form, named so a revert to the unbounded one is a red line and
    # not a silent widening — and its `GREATEST(..., 0.0)` precondition with it, because the
    # saturation is bounded and monotone only for a non-negative count (M49-D188-03).
    assert "1.0 + GREATEST(COALESCE(d.confidence, 0.5), 0.0)" in dna_terms.TERM_WEIGHT

    # And no third copy: `CASE d.tier` is the fragment's own opening, and it belongs to exactly
    # one module now.
    package = Path(why_mod.__file__).resolve().parents[1]
    holders = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "CASE d.tier" in path.read_text(encoding="utf-8")
    )
    assert holders == ["db/dna_terms.py"], holders
