"""End-to-end import against a real Postgres 16. Spec v2.1 §4.1, §10, §12 (M0 exit criterion).

These are the tests the unit suite could not be: `copy_records_to_table` resolves its encoders
from the *destination column types*, so type mismatches only exist against a real server. Two
shipped bugs lived exactly here — SQLite integer booleans into `boolean` columns, and json
columns arriving as text and being iterated character by character.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spielplan.db import library
from spielplan.importer import bundle as bundle_import
from tests.fixtures import make_bundle as fx


@pytest.fixture
def bundle(tmp_path) -> bundle_import.Bundle:
    fx.make_bundle(tmp_path / "bundle")
    return bundle_import.Bundle.open(tmp_path / "bundle")


async def _import(db, bundle, artifacts_root: Path):
    report = await bundle_import.import_bundle(db, bundle, artifacts_root)
    assert report.ok, report.render()
    return report


# --- M0 exit criterion: "bundle imports clean" ----------------------------------------


async def test_bundle_imports_clean(db, bundle, tmp_path):
    report = await _import(db, bundle, tmp_path / "artifacts")

    assert report.table_counts["loaded:title"] == len(fx.TITLES)
    assert report.table_counts["loaded:dna_tag"] == len(fx.EXTRACTED)
    assert report.table_counts["loaded:dna_projected"] == len(fx.PROJECTED)
    assert report.table_counts["loaded:review_store.review"] == 3
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)


async def test_sqlite_integer_booleans_reach_postgres_boolean_columns(db, bundle, tmp_path):
    """SQLite has no boolean type. Without an explicit cast asyncpg's binary COPY raises
    `TypeError: a boolean is required` on `title.is_owned` — the first, required mapping."""
    await _import(db, bundle, tmp_path / "artifacts")
    owned = await db.fetchval("SELECT count(*) FROM title WHERE is_owned")
    assert owned == len(fx.TITLES)
    assert isinstance(await db.fetchval("SELECT is_owned FROM title LIMIT 1"), bool)


async def test_the_bundle_becomes_the_one_active_row(db, bundle, tmp_path):
    """§10: the flip is transactional and a partial unique index allows exactly one active."""
    await _import(db, bundle, tmp_path / "artifacts")
    rows = await db.fetch("SELECT version, state FROM artifact_bundle")
    assert [(r["version"], r["state"]) for r in rows] == [("test-v1", "active")]


async def test_the_report_is_stored_as_json_not_as_a_string(db, bundle, tmp_path):
    """The json codec bug in miniature: a jsonb column read back as text is a string that
    every consumer will iterate one character at a time."""
    await _import(db, bundle, tmp_path / "artifacts")
    report = await db.fetchval("SELECT report FROM artifact_bundle WHERE version = 'test-v1'")
    assert isinstance(report, dict)
    assert report["ok"] is True
    manifest = await db.fetchval("SELECT manifest FROM artifact_bundle WHERE version = 'test-v1'")
    assert isinstance(manifest, dict)
    assert manifest["vocabulary_version"] == "v1"


# --- §4.1 rules, verified against the real schema --------------------------------------


async def test_rule1_both_tiers_land_separately_and_shared_pairs_survive(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    shared = await db.fetch(
        "SELECT g.title_id, g.term FROM dna_tag g "
        "JOIN dna_projected p ON p.title_id = g.title_id AND p.term = g.term"
    )
    assert len(shared) == 3, "the fixture's overlapping pairs must exist in BOTH tables"

    tiers = await db.fetch(
        "SELECT tier, count(*) AS n FROM dna_tagged GROUP BY tier ORDER BY tier"
    )
    assert {r["tier"]: r["n"] for r in tiers} == {
        "extracted": len(fx.EXTRACTED),
        "projected": len(fx.PROJECTED),
    }


async def test_rule1_every_extracted_tag_keeps_its_quote(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    orphans = await db.fetchval(
        "SELECT count(*) FROM dna_tag g "
        "WHERE NOT EXISTS (SELECT 1 FROM dna_evidence e WHERE e.dna_tag_id = g.id)"
    )
    assert orphans == 0


async def test_rule6_null_pk_components_became_empty_strings(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    nulls = await db.fetchval(
        "SELECT count(*) FROM title_alias WHERE region IS NULL OR language IS NULL OR kind IS NULL"
    )
    assert nulls == 0
    assert await db.fetchval("SELECT count(*) FROM title_alias WHERE region = ''") >= 1


async def test_rule8_non_ascii_survives_the_round_trip(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    name = await db.fetchval("SELECT name FROM title WHERE id = 5")
    assert name == "重慶森林"
    overview = await db.fetchval("SELECT overview FROM title WHERE id = 1")
    assert "\U0001f3ac" in overview and "​" in overview
    review = await db.fetchval("SELECT body FROM review_store.review WHERE title_id = 5")
    assert "王家衛" in review


async def test_rule3_platform_ratings_land_in_the_display_schema_only(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    assert await db.fetchval("SELECT count(*) FROM display.platform_rating") == 3
    in_public = await db.fetchval(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'platform_rating'"
    )
    assert in_public == 0


# --- §10: a re-import is a planned event, not a collision ------------------------------


async def test_reimporting_the_same_bundle_is_refused_while_it_is_active(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    again = await bundle_import.import_bundle(db, bundle, tmp_path / "artifacts")
    assert not again.ok
    assert any("already the active bundle" in f.message for f in again.failures)


async def test_a_second_bundle_version_imports_over_the_first(db, bundle, tmp_path):
    """The content tables are replaced and `title` is upserted — no primary-key collision."""
    await _import(db, bundle, tmp_path / "artifacts")

    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    second = bundle_import.Bundle.open(tmp_path / "bundle2")
    report = await _import(db, second, tmp_path / "artifacts")

    assert report.table_counts["loaded:title"] == len(fx.TITLES)
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)
    rows = await db.fetch("SELECT version, state FROM artifact_bundle")
    assert {r["version"]: r["state"] for r in rows} == {
        "test-v1": "superseded",
        "test-v2": "active",
    }


async def test_ledger_observations_survive_a_reimport(db, bundle, tmp_path):
    """§10: 'Ledger observations always survive re-import.' `verdict` references
    `title(id) ON DELETE CASCADE`, so a re-import that DELETEs titles would take the user's
    entire rating history with it. This is the test that catches that."""
    await _import(db, bundle, tmp_path / "artifacts")
    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'member') RETURNING id"
    )
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 1, 2)", user_id
    )
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')", user_id
    )

    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    await _import(db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts")

    assert await db.fetchval("SELECT count(*) FROM verdict") == 1
    assert await db.fetchval("SELECT count(*) FROM user_title") == 1


# --- M0 exit criterion: "Library list and title card render imported titles" ------------


async def test_library_lists_titles_partitioned_by_kind(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")

    movies, movie_total = await library.list_titles(db, kinds=["movie"])
    series, series_total = await library.list_titles(db, kinds=["series"])

    assert movie_total == 6
    assert series_total == 2
    assert movie_total + series_total == len(fx.TITLES)
    assert {t["kind"] for t in movies} == {"movie"}
    assert {t["kind"] for t in series} == {"series"}


async def test_both_kinds_selected_returns_everything(db, bundle, tmp_path):
    """Owner decision 2026-08-29: kind is two toggles, either or both active."""
    await _import(db, bundle, tmp_path / "artifacts")
    rows, total = await library.list_titles(db, kinds=["movie", "series"])
    assert total == len(fx.TITLES)
    assert {t["kind"] for t in rows} == {"movie", "series"}


async def test_selecting_no_kind_is_an_error_not_everything(db, bundle, tmp_path):
    """An empty selection silently meaning 'everything' is the unpartitioned query §4.1 rule 5
    exists to prevent."""
    await _import(db, bundle, tmp_path / "artifacts")
    with pytest.raises(ValueError, match="at least one kind"):
        await library.list_titles(db, kinds=[])


async def test_hidden_counts_report_the_unselected_kind(db, bundle, tmp_path):
    """§6.0: a toggle that hides things has to say how many."""
    await _import(db, bundle, tmp_path / "artifacts")
    assert await library.count_by_kind(db, exclude=["movie"]) == {"series": 2}
    assert await library.count_by_kind(db, exclude=["movie", "series"]) == {}


async def test_a_person_filter_keeps_the_kind_partition(db, bundle, tmp_path):
    """Owner decision: the person filter does NOT suspend the partition — selecting both kinds
    is how you see a whole filmography."""
    await _import(db, bundle, tmp_path / "artifacts")
    both, total = await library.list_titles(db, kinds=["movie", "series"], person_id=1)
    assert total >= 1
    films, film_total = await library.list_titles(db, kinds=["movie"], person_id=1)
    assert film_total <= total


async def test_facet_vocabulary_spans_the_selected_kinds(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    movie_genres = await library.genres(db, ["movie"])
    both_genres = await library.genres(db, ["movie", "series"])
    assert set(movie_genres) < set(both_genres)
    assert "Sci-Fi" in both_genres and "Sci-Fi" not in movie_genres


async def test_library_search_matches_titles_and_aliases(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    hits, total = await library.list_titles(db, kinds=["movie"], q="chungking")
    assert total >= 1
    # id 5 carries "Chungking Express" only as an ALIAS; its name is the CJK original.
    assert 5 in {t["id"] for t in hits}


async def test_title_card_payload_is_complete(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")

    title = await library.get_title(db, 1)
    assert title["name"] == "Heat"

    credits = await library.credits_for(db, 1)
    # §4.1: "credit (dedupe at read time, never at import)" — the fixture stores the director
    # twice, from tmdb and omdb, and the card must show one row citing both.
    directors = [c for c in credits if c["job"] == "Director"]
    assert len(directors) == 1
    assert sorted(directors[0]["sources"]) == ["omdb", "tmdb"]
    assert await db.fetchval("SELECT count(*) FROM credit WHERE title_id = 1") == 3

    dna = await library.dna_for(db, 1)
    assert {t["term"] for t in dna["extracted"]} == {"obsession", "morally-grey"}
    assert {t["term"] for t in dna["projected"]} == {"obsession", "period"}

    # The json-codec bug: evidence must be a list of dicts, not a JSON string that the UI
    # would iterate one character at a time.
    obsession = next(t for t in dna["extracted"] if t["term"] == "obsession")
    assert isinstance(obsession["evidence"], list)
    assert obsession["evidence"][0]["quote"] == "the work eats the man and he lets it"
    assert obsession["evidence"][0]["source"] == "trakt:comment"


async def test_seen_filter_treats_a_missing_row_as_unseen(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')", user_id
    )

    seen, seen_total = await library.list_titles(db, kinds=["movie"], user_id=user_id, seen="seen")
    unseen, unseen_total = await library.list_titles(
        db, kinds=["movie"], user_id=user_id, seen="unseen"
    )

    assert seen_total == 1 and seen[0]["id"] == 1
    assert unseen_total == 5, "titles with no user_title row are unseen, not missing"


async def test_combined_filters_number_their_parameters_correctly(db, bundle, tmp_path):
    """`list_titles` builds SQL by hand with a $N counter; combining every filter at once is
    the case where an off-by-one in that counter shows up."""
    await _import(db, bundle, tmp_path / "artifacts")
    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('mia', 'member') RETURNING id"
    )
    rows, total = await library.list_titles(
        db,
        kinds=["movie"],
        user_id=user_id,
        q="heat",
        genre="Crime",
        decade=1990,
        seen="unseen",
        person_id=1,
        owned_only=True,
        limit=5,
        offset=0,
    )
    assert total == 1
    assert rows[0]["name"] == "Heat"


# --- §6.0: the catalog's filters, each able to fail --------------------------------------


async def test_genre_and_decade_filters_actually_narrow(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")

    _, all_films = await library.list_titles(db, kinds=["movie"])
    _, crime = await library.list_titles(db, kinds=["movie"], genre="Crime")
    _, nineties = await library.list_titles(db, kinds=["movie"], decade=1990)

    assert 0 < crime < all_films
    assert 0 < nineties < all_films

    rows, _ = await library.list_titles(db, kinds=["movie"], decade=1990)
    assert all(1990 <= r["year"] < 2000 for r in rows)


async def test_a_filter_that_matches_nothing_returns_nothing(db, bundle, tmp_path):
    """An empty result is a legitimate answer, not an error and not a silent fallback."""
    await _import(db, bundle, tmp_path / "artifacts")
    rows, total = await library.list_titles(db, kinds=["movie"], genre="Documentary")
    assert total == 0 and rows == []


async def test_pagination_returns_each_title_once(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    first, total = await library.list_titles(db, kinds=["movie"], limit=3, offset=0)
    second, _ = await library.list_titles(db, kinds=["movie"], limit=3, offset=3)
    ids = [t["id"] for t in first + second]
    assert len(ids) == len(set(ids)), "a page boundary must not repeat or drop a title"
    assert len(ids) == min(6, total)


async def test_the_person_filter_hides_the_other_kind_until_both_are_selected(db, bundle, tmp_path):
    """Owner decision 18: the person filter does NOT suspend the kind partition — selecting
    both kinds is how a whole filmography is seen. The fixture credits Ada Cross-Kind on a
    film and a series precisely so this can fail."""
    await _import(db, bundle, tmp_path / "artifacts")
    ada = await db.fetchval("SELECT id FROM person WHERE name = 'Ada Cross-Kind'")

    films, film_total = await library.list_titles(db, kinds=["movie"], person_id=ada)
    series, series_total = await library.list_titles(db, kinds=["series"], person_id=ada)
    both, both_total = await library.list_titles(db, kinds=["movie", "series"], person_id=ada)

    assert film_total == 1 and series_total == 1
    assert both_total == 2, "with both kinds on, the filmography is complete"
    assert {t["kind"] for t in both} == {"movie", "series"}
    assert {t["id"] for t in films + series} == {t["id"] for t in both}


async def test_hidden_counts_answer_why_the_list_is_short(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    assert await library.count_by_kind(db, exclude=["movie"]) == {"series": 2}
    assert await library.count_by_kind(db, exclude=["series"]) == {"movie": 6}


# --- §10: the report is the diff material -----------------------------------------------


async def test_the_report_counts_every_loaded_table(db, bundle, tmp_path):
    """§10: "a migration report (counts per table, validation failures, vocabulary version)".
    Those counts are what a re-import is diffed against."""
    report = await _import(db, bundle, tmp_path / "artifacts")
    counts = report.table_counts

    for table in ("loaded:title", "loaded:person", "loaded:credit", "loaded:dna_tag",
                  "loaded:dna_projected", "loaded:display.platform_rating"):
        assert table in counts, f"{table} is not counted in the report"
        assert counts[table] > 0

    assert report.vocabulary_version == "v1"
    text = report.render()
    assert "vocabulary v1" in text and "rows:" in text


async def test_an_unmapped_bundle_column_is_reported_not_dropped_silently(db, bundle, tmp_path):
    """§4.1's shape note: the corpus export is the authority on its own column names, and this
    app must survive it gaining one — visibly."""
    import sqlite3

    con = sqlite3.connect(bundle.content_db)
    con.execute("ALTER TABLE title ADD COLUMN some_new_corpus_column TEXT")
    con.commit()
    con.close()

    report = await _import(db, bundle, tmp_path / "artifacts")
    assert "some_new_corpus_column" in report.unmapped_columns.get("title", [])


async def test_a_second_import_reports_the_same_counts(db, bundle, tmp_path):
    """The diff a re-import is judged on only means something if the counts are comparable."""
    first = await _import(db, bundle, tmp_path / "artifacts")
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    second = await _import(db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts")
    assert first.table_counts["loaded:title"] == second.table_counts["loaded:title"]
    assert first.table_counts["loaded:dna_tag"] == second.table_counts["loaded:dna_tag"]


async def test_the_import_recomputes_the_rebuild_set_before_it_flips(db, bundle, tmp_path):
    """§10's sequence: "validate -> stage -> recompute the rebuild set against the **staged**
    bundle -> transactionally flip".

    M0 shipped the *report* of the rebuild set with nothing behind it, which was correct then —
    none of the four things existed before M2. What that leaves behind is an import that reads
    as if it rebuilt and did not, so this asserts the work actually happened: a user vector and
    a ledger state exist afterwards, both stamped with the bundle that was staged.

    Before the flip matters as much as the recompute. Run after it, a failing rebuild leaves a
    new basis active with every fitted number still expressed in the old one — §10's "garbage
    against a new one", made active and served.
    """
    from spielplan.ledger import observations

    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('Patrick', 'admin') RETURNING id"
    )
    report = await _import(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    for title_id, value in ((1, 2), (2, 1), (3, 0), (4, 2), (5, 1)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)

    # Re-import the same content as a second version, which is the case §10 is actually about.
    fx.make_bundle(tmp_path / "b2", version="test-v2")
    second = bundle_import.Bundle.open(tmp_path / "b2")
    report2 = await _import(db, second, tmp_path / "artifacts")
    assert report2.ok, report2.render()

    notes = [f for f in report2.findings if f.rule in ("rebuild", "rebuild-set")]
    assert notes, "the import reported no rebuild at all"
    titles = " ".join(f.message for f in notes)
    for expected in ("fold-in", "blend", "Ledger", "Cold Tower"):
        assert expected.lower() in titles.lower(), f"§10 names {expected} and the report omits it"

    assert await db.fetchval(
        "SELECT count(*) FROM user_vector WHERE bundle_version = 'test-v2'"
    ) > 0, "step 1 wrote no fold-in vector against the staged basis"
    assert await db.fetchval("SELECT count(*) FROM ledger_state WHERE user_id = $1", patrick) > 0


async def test_a_freshly_activated_bundle_serves_its_cold_titles_immediately(db, bundle, tmp_path):
    """§10's rebuild set exists so that the moment a bundle goes active, every fitted number is
    expressed in its basis. That has to include the coordinates the other three steps read.

    §10 lists the fold-in first and the Cold Tower re-placement fourth, and the first
    implementation executed them in that order — so `title_prior` and every `user_score` row
    were materialised against a `title_placement` table the new bundle had not been written into
    yet. The import returned ok, the flip happened, and the library served from that instant had
    its coordinate-less titles missing from every ranked list and its low-support titles shrunk
    toward μ instead of toward b̂ — until the next nightly sweep, hours later.

    The listing order is §10's prose; the execution order is what the steps actually need.
    """
    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('Patrick', 'admin') RETURNING id"
    )
    report = await _import(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    # Title 8 has no Backbone row at all (the fixture makes §5.1's cold branch reachable), so it
    # exists only if step 4 ran before the step that materialised the priors.
    priced = await db.fetchrow(
        "SELECT b, e_source FROM title_prior WHERE title_id = 8 AND bundle_version = 'test-v1'"
    )
    assert priced is not None, "the cold title has no prior at all"
    assert priced["e_source"] == "cold_tower", (
        f"a freshly activated bundle prices its cold title as {priced['e_source']!r} — the "
        "fold-in ran before the placement it reads"
    )
    assert priced["b"] is not None

    # And the report still reads in §10's order, so the import screen matches the spec's prose.
    rebuild = [f.message for f in report.findings if f.rule == "rebuild"]
    assert len(rebuild) == 4
    assert "fold-in" in rebuild[0] and "Cold Tower" in rebuild[3]
    assert patrick
