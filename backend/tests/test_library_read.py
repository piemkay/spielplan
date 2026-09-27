"""The §6.0/§6.4 read layer against a real Postgres 16: DNA ids, version scoping, total order,
literal search, kind partitions and term weights are all facts about SQL. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncpg
import pytest

from spielplan.db import dna_terms, library
from spielplan.home import why as why_mod
from spielplan.importer import bundle as bundle_import
from spielplan.tonight import dna as tonight_dna
from tests.fixtures import make_bundle as fx

VOCAB = "v1"


async def _titles(db, rows) -> None:
    """Owned, because the catalog lists owned and unowned alike and nothing here turns on the flag."""
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
    """The whole `facet.term` id in `term`, the id's own prefix in `facet`, as the loader stores it."""
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
    """Against `title` directly, not a fitted board: the predicate is under test."""
    clause, args = library.rank_filters(
        kind=kind, user_id=1, filters=library.RankFilters(dna=dna)
    )
    rows = await db.fetch(f"SELECT t.id FROM title t WHERE {clause} ORDER BY t.id", *args)
    return [int(r["id"]) for r in rows]


@pytest.fixture
async def tagged(db):
    await _titles(db, [(1, "movie", "Heat", 1995), (2, "movie", "Se7en", 1995),
                       (3, "movie", "Paddington 2", 2017)])
    await _vocabulary(db, VOCAB, facets=("mood", "sensibility"))
    await _tag(db, 1, "mood.cosy")
    await _tag(db, 2, "sensibility.bleak")
    await _projected(db, 1, "mood.cosy")
    await _projected(db, 3, "mood.cosy")


async def test_the_bare_and_the_qualified_dna_term_select_the_same_rows(db, tagged):
    """A different facet in front of the same bare term is a different id and selects nothing."""
    assert await _matching(db, "mood.cosy") == [1, 3]
    assert await _matching(db, "cosy") == [1, 3]
    assert await _matching(db, "sensibility.bleak") == [2]
    assert await _matching(db, "bleak") == [2]
    assert await _matching(db, "pacing.cosy") == []

    # `facet || '.' || term` over an already-qualified term: nobody can type it, so nothing must return.
    assert await _matching(db, "mood.mood.cosy") == []


async def test_dna_tiers_are_returned_for_both_spellings(db, tagged):
    """`dna_tiers_for` is a second copy of the predicate and must agree about every spelling."""
    for spelling in ("cosy", "mood.cosy"):
        matched = await library.dna_tiers_for(db, title_ids=[1, 2, 3], dna=spelling)
        assert matched[1] == ["extracted", "projected"], "a pair in both tiers reports both"
        assert matched[3] == ["projected"]
        assert 2 not in matched


@pytest.fixture
async def two_vocabularies(db):
    """`v2` is newer by `imported_at`, so active; titles 2 and 3 share only one version's term each."""
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


async def test_two_vocabularies_do_not_mix_on_the_card_or_the_filter(db, two_vocabularies):
    """Any one reader leaking puts two vocabularies on one screen."""
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


async def test_two_vocabularies_written_in_one_transaction_still_resolve_to_one(db):
    """`now()` is transaction-stable, so two versions written in one transaction tie on `imported_at`."""
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


async def test_paging_over_ties_with_a_rewrite_between_pages_loses_and_repeats_nothing(db):
    """An UPDATE moves the row to the end of the heap, so an untied sort slides the page boundaries."""
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
        # The row keeps its sort keys and changes its physical
        # position: what an OFFSET cannot survive untied.
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


@pytest.fixture
async def searchable(db):
    """Backslash is Postgres's default LIKE escape; `Room 100` and `Top 1000` make the count falsifiable."""
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
    """Escaping keeps the typed character that character; it is not cleaning (§4.1 rule 8)."""
    assert await _search(db, "%") == ["100% Wolf"]
    assert await _search(db, "_") == []
    assert await _search(db, "h_at") == []
    assert await _search(db, "100%") == ["100% Wolf"]
    assert await _search(db, "\\") == ["Back\\Slash"]

    # A needle with no metacharacters still matches as a case-insensitive substring.
    assert await _search(db, "heat") == ["Heat"]
    assert await _search(db, "100") == ["100% Wolf", "Room 100"]


async def test_the_hidden_by_kind_count_agrees_with_the_escaped_listing(db, searchable):
    rows, total = await library.list_titles(db, kinds=["movie"], q="100%", limit=60)
    assert [r["name"] for r in rows] == ["100% Wolf"]
    assert total == 1

    hidden = await library.count_by_kind(db, exclude=["movie"], q="100%")
    assert hidden == {"series": 1}, "the count may only promise what the toggle can reveal"

    # The toggle keeps its promise: turning Series on reveals exactly what was counted.
    both, _ = await library.list_titles(db, kinds=["movie", "series"], q="100%", limit=60)
    assert len(both) == total + hidden["series"]


async def test_no_projected_term_outweighs_any_extracted_term(db, tmp_path):
    """`n_sources` reached 2.40 against an extracted cap of
    1.00; the corpus's eight is written in to reproduce it."""
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
    # Decision 188's numbers: the form saturates, it does not clamp.
    assert widest["projected"][0] == pytest.approx(0.15, abs=0.005)
    assert widest["projected"][1] == pytest.approx(0.267, abs=0.005)
    assert widest["extracted"][0] >= 0.733


async def test_a_negative_projected_weight_neither_raises_nor_outranks_an_extracted_term(db):
    """`dna_projected.weight` has no CHECK, and `c / (1 +
    c)` at -1 divides by zero; `GREATEST` is the guard."""
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
    """Asserted on identity: the fragment feeds Home's why-lines and Tonight's vectors alike."""
    assert why_mod.TERM_RANK is dna_terms.TERM_WEIGHT
    assert tonight_dna.TERM_WEIGHT is dna_terms.TERM_WEIGHT

    # Decision 188's saturating form, with the `GREATEST(..., 0.0)` precondition it needs.
    assert "1.0 + GREATEST(COALESCE(d.confidence, 0.5), 0.0)" in dna_terms.TERM_WEIGHT
