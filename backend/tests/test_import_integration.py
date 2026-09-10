"""End-to-end import against a real Postgres 16. Spec v2.1 §4.1, §10, §12 (M0 exit criterion).

These are the tests the unit suite could not be: `copy_records_to_table` resolves its encoders
from the *destination column types*, so type mismatches only exist against a real server. Two
shipped bugs lived exactly here — SQLite integer booleans into `boolean` columns, and json
columns arriving as text and being iterated character by character.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from spielplan.api import admin as admin_api
from spielplan.db import library
from spielplan.home import rail
from spielplan.importer import bundle as bundle_import
from spielplan.placement import features
from spielplan.placement.contract import FeatureContract
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
    # `artifact_bundle.manifest` is BUNDLE.json, the corpus's own identity record — not
    # `artifacts/manifest.json`, which §4.3 defines as the fitted cut-points and which this row
    # held until M4.5. `vocabulary_version` was asserted here and no bundle has ever written it;
    # the version travels in its own column, off the `dna_vocab/<version>/` directory.
    assert {"bundle_version", "tables", "files"} <= set(manifest)
    assert manifest["bundle_version"] == "test-v1"
    assert await db.fetchval(
        "SELECT vocabulary_version FROM artifact_bundle WHERE version = 'test-v1'"
    ) == "v1"


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
    """§10: a bundle already flipped active is not re-staged over itself.

    Reaching that rule now takes a second version. Decision 162 gave the *seed's* version string
    a more specific refusal — a models-only bundle exported under it would rewrite the one
    `artifact_bundle` row recording that content was ever seeded — and `refuse_on_install_state`
    runs before anything is staged, so at that version the operator gets that line instead. Both
    are asserted, in the order they fire: without the first the version rule looks reachable at
    the seed's version when it is not, and without the second it is not exercised at all.
    """
    await _import(db, bundle, tmp_path / "artifacts")
    (bundle.root / "content.sqlite").unlink()
    (bundle.root / "reviews.sqlite").unlink()

    at_the_seeds_version = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(bundle.root), tmp_path / "artifacts"
    )
    assert not at_the_seeds_version.ok
    assert any(f.rule == "seed-once" for f in at_the_seeds_version.failures)
    assert any(
        "under its own version string" in f.message for f in at_the_seeds_version.failures
    )

    # And the version rule itself, at the only version that can still reach it: a model bundle
    # of this install's own, imported once and then offered again while it is the active row.
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    (tmp_path / "bundle2" / "content.sqlite").unlink()
    (tmp_path / "bundle2" / "reviews.sqlite").unlink()
    await _import(db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts")

    again = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts"
    )
    assert not again.ok
    assert any("already the active bundle" in f.message for f in again.failures)


async def test_a_second_bundle_version_swaps_the_models_over_the_seeded_content(db, bundle, tmp_path):
    """§10's re-import at a second version, in decision 162's shape: models re-ship, content
    does not.

    This asserted "the content tables are replaced and `title` is upserted — no primary-key
    collision" while the second bundle carried a second copy of the corpus's spine. Under
    decision 162 that import is refused outright, and the collision it was defending against is
    gone by construction rather than survived: no content loader runs, so no row is rewritten.
    What is left to assert is that the seeded spine comes through the swap *untouched* — a
    models-only import that quietly rewrote a title would be the same defect one level down.
    """
    await _import(db, bundle, tmp_path / "artifacts")
    spine = [dict(r) for r in await db.fetch("SELECT id, name, kind FROM title ORDER BY id")]

    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    (tmp_path / "bundle2" / "content.sqlite").unlink()
    (tmp_path / "bundle2" / "reviews.sqlite").unlink()
    second = bundle_import.Bundle.open(tmp_path / "bundle2")
    report = await _import(db, second, tmp_path / "artifacts")

    # Not `== 0`: a count of zero is a claim about what the bundle shipped, and this bundle
    # ships no spine at all. The report says so in words instead (see the counts test below).
    assert "loaded:title" not in report.table_counts
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)
    assert [
        dict(r) for r in await db.fetch("SELECT id, name, kind FROM title ORDER BY id")
    ] == spine

    rows = await db.fetch("SELECT version, state, kind FROM artifact_bundle")
    assert {r["version"]: r["state"] for r in rows} == {
        "test-v1": "superseded",
        "test-v2": "active",
    }
    assert {r["version"]: r["kind"] for r in rows} == {"test-v1": "seed", "test-v2": "model"}
    assert [f.rule for f in report.findings if f.rule == "swap"], "§10's flip went unreported"


async def test_ledger_observations_survive_a_reimport(db, bundle, tmp_path):
    """§10: 'Ledger observations always survive re-import.'

    `verdict` references `title(id) ON DELETE CASCADE`, so a re-import that DELETEd titles would
    take the user's entire rating history with it. Under decision 162 a re-import carries no
    content at all, so that particular path is closed by construction and what this now guards
    is the other cascade on the same claim: 0015 makes `user_vector.bundle_version` SET NULL
    rather than CASCADE, and a re-import that pruned a bundle row used to take the fold-in with
    it."""
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

    # decision 162's re-import shape: the corpus re-ships models, never content. The claim is
    # unchanged — the observations reference `title.id`, and a re-import must not disturb them.
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    (tmp_path / "bundle2" / "content.sqlite").unlink()
    (tmp_path / "bundle2" / "reviews.sqlite").unlink()
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


_CROSS_DEPARTMENT = (
    "INSERT INTO credit (title_id, person_id, department, job, character, billing_order, source)"
    " VALUES ($1, $2, $3, 'Actor', $4, $5, $6)"
)


async def test_a_credit_is_one_row_per_person_and_job_across_department_spellings(
    db, bundle, tmp_path
):
    """§4.1: "credit (dedupe at read time, never at import)" + §6.0's title card.

    TMDB files one job under two department spellings, and the real export carries 7,918
    (title, person, job) triples spanning more than one of them across 1,216 of 19,071 titles —
    816 inside the twelve credits the card renders. Grouping the read on the department handed
    the card two rows that differ in nothing it shows, and Svelte 5's keyed each throws on the
    duplicate key in the production branch as well as in dev, so with no `+error.svelte` the
    panel died mid-render: no platform scores, no DNA tiers, no model line.
    """
    await _import(db, bundle, tmp_path / "artifacts")

    # Three rows, not two. The fixture itself now files Al Pacino's one job under both `Acting`
    # and `Actor` (M4.8, `platform-fixture-carries-the-corpus-awkward-shapes`), so the collision
    # arrives through the importer the way it arrives from the corpus; this insert stays because
    # it adds the second SOURCE, its own spelling of the character and a later billing order,
    # which is what the two assertions below are actually about.
    await db.execute(_CROSS_DEPARTMENT, 1, 4, "Actor", "Lt. Hanna", 6, "omdb")
    assert await db.fetchval(
        "SELECT count(*) FROM credit WHERE title_id = 1 AND person_id = 4"
    ) == 3

    credits = await library.credits_for(db, 1)
    rows = [c for c in credits if c["person_id"] == 4 and c["job"] == "Actor"]
    assert len(rows) == 1
    # Both spellings stay visible — §4.1 rule 1 keeps what the sources said — while the single
    # `department` the card reads resolves to the TMDB canonical one.
    assert sorted(rows[0]["departments"]) == ["Acting", "Actor"]
    assert rows[0]["department"] == "Acting"
    assert sorted(rows[0]["sources"]) == ["omdb", "tmdb"]

    # The client key is total: one `person_id:job` per row, which is what stops the throw.
    keys = {f"{c['person_id']}:{c['job']}" for c in credits}
    assert len(keys) == len(credits)

    # The directing-first sort survives losing `c.department` as a grouping column.
    assert credits[0]["job"] == "Director"

    # The character is the lowest billing order's, not the heap's: 2,300 (title, person) pairs
    # in the corpus carry more than one distinct character across sources, and an unordered
    # `array_agg(...)[1]` made the value this PAYLOAD carried a function of COPY order. It is
    # the payload that is under test and not a rendering: §6.0's card list does not name the
    # character and `TitleDetail.svelte` prints name and job alone, so the field ships and no
    # surface shows it (decision 197). That is the reason the ordered aggregate stays.
    assert rows[0]["character"] == "Vincent Hanna"
    await db.execute("DELETE FROM credit WHERE title_id = 1 AND person_id = 4")
    await db.execute(_CROSS_DEPARTMENT, 1, 4, "Actor", "Lt. Hanna", 6, "omdb")
    await db.execute(_CROSS_DEPARTMENT, 1, 4, "Acting", "Vincent Hanna", 1, "tmdb")
    reinserted = await library.credits_for(db, 1)
    again = next(c for c in reinserted if c["person_id"] == 4 and c["job"] == "Actor")
    assert again["character"] == "Vincent Hanna"


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
    # Four stored rows behind three rendered ones: the director twice (tmdb and omdb) and Al
    # Pacino twice, under the two department spellings TMDB files leads under. The second is
    # M4.8's fixture row — "dedupe at read time, never at import" is only testable against a
    # bundle that carries the duplicate.
    assert await db.fetchval("SELECT count(*) FROM credit WHERE title_id = 1") == 4

    # `version` is required since M4.9: the card resolves the active vocabulary once and hands
    # it down, so two imported bundles cannot put two vocabularies on one card (finding 10).
    dna = await library.dna_for(db, 1, version="v1")
    # The corpus's term ids are `<facet>.<term>`, dotted and facet-prefixed — `obsession` and
    # `morally-grey` were this repo's own spelling, and the feature contract's `dna:` columns
    # are keyed by the shipped id.
    assert {t["term"] for t in dna["extracted"]} == {"themes.obsession", "characters.morally_grey"}
    assert {t["term"] for t in dna["projected"]} == {"themes.obsession", "era.period"}

    # The json-codec bug: evidence must be a list of dicts, not a JSON string that the UI
    # would iterate one character at a time.
    obsession = next(t for t in dna["extracted"] if t["term"] == "themes.obsession")
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


async def test_a_models_only_reimport_leaves_the_seed_counts_standing_as_the_diff(db, bundle, tmp_path):
    """§10: "a diff report — never a silent sync". The diff only means something if the counts
    are comparable, and under decision 162 the two sides of it are no longer two imports of the
    same tables.

    The re-import ships no content, so it states no content counts — and the seed's counts are
    what the install is still described by. That makes two things load-bearing: the seed's
    report has to survive the swap (it is one half of the diff), and it has to remain *true*
    after it. A models-only import that reported `loaded:title: 0`, or one that left the spine
    at a different size than the seed counted, would both make the two reports read as a
    library that emptied itself.
    """
    first = await _import(db, bundle, tmp_path / "artifacts")
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    (tmp_path / "bundle2" / "content.sqlite").unlink()
    (tmp_path / "bundle2" / "reviews.sqlite").unlink()
    second = await _import(db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts")

    # The two counts the old comparison named, now compared against the install they describe.
    assert first.table_counts["loaded:title"] == await db.fetchval("SELECT count(*) FROM title")
    assert first.table_counts["loaded:dna_tag"] == await db.fetchval(
        "SELECT count(*) FROM dna_tag"
    )
    for table in ("loaded:title", "loaded:dna_tag"):
        assert table not in second.table_counts, f"{table} counted by a bundle carrying no content"

    # Absence with a reason attached, which is the difference between a diff and a silence.
    assert any(
        f.rule == "bundle" and "models-only" in f.message for f in second.findings
    ), "the report does not say why it counts no content"
    assert first.vocabulary_version == second.vocabulary_version == "v1"

    # Both halves are readable side by side afterwards: §10's diff is between stored reports,
    # and the flip must not overwrite the one the new bundle is being diffed against.
    stored = {
        r["version"]: r["report"]
        for r in await db.fetch("SELECT version, report FROM artifact_bundle")
    }
    assert stored["test-v1"]["counts"]["loaded:title"] == first.table_counts["loaded:title"]
    assert "loaded:title" not in stored["test-v2"]["counts"]


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

    # A models-only re-import at a second version, which is the case §10 is actually about
    # once decision 162 has settled that content arrives once and models re-ship.
    fx.make_bundle(tmp_path / "b2", version="test-v2")
    (tmp_path / "b2" / "content.sqlite").unlink()
    (tmp_path / "b2" / "reviews.sqlite").unlink()
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


# --- M4.7: the unpacked tree is scratch space, not a second copy of the bundle -----------------


def _tarred(root: Path, target: Path) -> Path:
    """The shape an operator drops into `/data/import`: one `.tar` holding the bundle directory.

    Every import test above hands `Bundle.open` a directory, which is the one shape that never
    unpacks — so the tree `_unpack` writes existed in the suite nowhere at all, and neither did
    the fact that nothing ever removed it.
    """
    with tarfile.open(target, "w") as tar:
        tar.add(root, arcname=root.name)
    return target


async def test_a_committed_import_removes_the_tree_it_unpacked(db, tmp_path):
    """`_unpack` was the only writer of `.unpacked-<stem>/` and there was no cleaner anywhere.

    The unpack is a full second copy of the bundle, `content.sqlite` and `reviews.sqlite`
    included — 790 MB of a 1042 MB bundle, and the two files the staged
    `/data/artifacts/<version>/` copy deliberately does not carry. The same bundle offered as
    `.tar` and as `.tar.zst` has two stems and left two trees; the measured total was 3.6 GB for
    one bundle. `docker-compose.yml` binds `./data/import` from the host, so that is the
    household's own disk, and `POST /validate` — documented as writing nothing — is what spends
    it. [M4.7 dd10]
    """
    fx.make_bundle(tmp_path / "bundle")
    archive = _tarred(tmp_path / "bundle", tmp_path / "spielplan-bundle.tar")
    bundle = bundle_import.Bundle.open(archive)

    unpacked = tmp_path / ".unpacked-spielplan-bundle"
    assert unpacked.is_dir() and (unpacked / "bundle" / "content.sqlite").is_file()

    report = await _import(db, bundle, tmp_path / "artifacts")

    assert not unpacked.exists()
    assert [f.message for f in report.findings if f.rule == "cleanup"] == [
        f"removed the unpacked bundle tree at {unpacked}"
    ]
    # What survives is what §10 says survives: the staged artifacts, and the archive itself.
    assert (tmp_path / "artifacts" / "test-v1").is_dir()
    assert archive.is_file()


async def test_a_failed_import_keeps_its_unpacked_tree_for_the_retry(db, tmp_path):
    """The other half, and the reason the cleanup is not in a `finally`.

    A models-only bundle into an install with no content is refused before it writes anything,
    and a refusal is exactly when the operator tries again — with the same file, usually after
    doing the thing the report told them to. Making them re-extract a gigabyte to do it would be
    a punishment for a failure that is not theirs. [M4.7 dd10]
    """
    fx.make_bundle(tmp_path / "models", version="test-v2")
    (tmp_path / "models" / "content.sqlite").unlink()
    (tmp_path / "models" / "reviews.sqlite").unlink()
    archive = _tarred(tmp_path / "models", tmp_path / "models-only.tar")
    bundle = bundle_import.Bundle.open(archive)

    unpacked = tmp_path / ".unpacked-models-only"
    assert unpacked.is_dir()

    report = await bundle_import.import_bundle(db, bundle, tmp_path / "artifacts")

    assert not report.ok, report.render()
    assert unpacked.is_dir(), "the retry would have to unpack the whole bundle again"


async def test_a_cleanup_that_could_not_remove_the_tree_says_so_rather_than_claiming_it_did(
    db, tmp_path, monkeypatch
):
    """The note followed the call, not the outcome.

    `_clean_unpacked` removes the tree with `ignore_errors=True` — correctly, because a committed
    import must not be failed by its own housekeeping — and then added the "removed" note
    unconditionally. `ignore_errors` swallows EACCES, EBUSY, ENOTEMPTY and "cannot call rmtree on
    a symbolic link" alike, so every one of those was reported to the household as a success.
    The install that meets it is this milestone's own: a `.unpacked-*` tree written by the
    previous root container, which the uid-1000 image can read and reuse but cannot unlink out of
    a root-owned 0755 directory. The Data tab then says 790 MB were freed while they are still on
    the host disk — and dd10 exists precisely to report that fact.

    `rmtree` is replaced rather than a real EACCES provoked: POSIX chmod does not stop root and
    Windows needs an ACL, so a genuine failure is not portable, and a no-op is exactly what
    `ignore_errors=True` degenerates to when the unlink fails. The patch is on the module
    attribute, so it covers `import_bundle`'s other `rmtree` too — the one that clears a staged
    `/data/artifacts/<version>` before re-copying it — which is not reached here because this
    version has never been staged into `tmp_path`. [M4.7 cycle 2 finding 12]
    """
    fx.make_bundle(tmp_path / "bundle")
    archive = _tarred(tmp_path / "bundle", tmp_path / "spielplan-bundle.tar")
    bundle = bundle_import.Bundle.open(archive)
    unpacked = tmp_path / ".unpacked-spielplan-bundle"
    monkeypatch.setattr(bundle_import.shutil, "rmtree", lambda *a, **k: None)

    report = await _import(db, bundle, tmp_path / "artifacts")

    assert unpacked.is_dir(), "this test proves nothing if the tree is gone"
    notes = [f.message for f in report.findings if f.rule == "cleanup"]
    assert notes == [
        f"could not remove the unpacked bundle tree at {unpacked} - delete it by hand"
    ], "a cleanup that removed nothing reported a removal"


# --- M4.9: the rows the real export ships ------------------------------------------------------
#
# Four of these need a shape the committed fixture does not carry, and `make_bundle.py` is M4.8's
# file rather than this milestone's. So each helper below writes the corpus's own shape into the
# bundle's sqlite before the import, exactly as `test_load_mapping.py::_add_duplicate_per_source_rows`
# already does for 0015's three tables — the DDL comes from `fixtures/real_bundle_shapes.json`,
# which is the committed manifest of a real bundle. Each helper names the shape `make_bundle.py`
# would have to gain for it to become unnecessary.


def _add_company_rows(root: Path) -> None:
    """`title_company` as the corpus ships it: keyed per source, so one company credited by two
    sources on one title is two rows.

    The committed fixture has no `title_company` table at all — the loader named the whole table
    in `SKIPPED_TABLES` until M4.9, so there was nothing for a fixture to feed. `country` is
    shipped and deliberately unmapped (the mapping says why), and is written here so the
    unmapped-column report line is exercised rather than assumed.
    """
    db = sqlite3.connect(root / "content.sqlite")
    with db:
        db.execute(
            "CREATE TABLE title_company (title_id INTEGER NOT NULL, source TEXT NOT NULL,"
            " company TEXT NOT NULL, role TEXT, country TEXT,"
            " PRIMARY KEY (title_id, source, company, role))"
        )
        db.executemany(
            "INSERT INTO title_company (title_id, source, company, role, country)"
            " VALUES (?,?,?,?,?)",
            [
                # One company, one role, one title, two sources: 8,594 groups of this shape in
                # the shipped bundle, every one a collision under the app's pre-0018 key.
                (1, "tmdb", "Warner Bros.", "production", "US"),
                (1, "omdb", "Warner Bros.", "production", "US"),
                (1, "tmdb", "Regency Enterprises", "production", "US"),
                (3, "tmdb", "StudioCanal", "production", "GB"),
            ],
        )
    db.close()


async def test_title_company_lands_per_source_and_is_not_reported_skipped(db, tmp_path):
    """Decision 193 and 0018 section 3: the fourth per-source table, four days late.

    `0003_content.sql:120-125` keyed the table (title_id, company, role), one component coarser
    than the corpus, so `load.py` named it in `SKIPPED_TABLES` and none of its 47,607 shipped
    rows landed: 8,594 duplicate groups under the app's key, 11,654 rows discarded (decision
    195). §4.1's "tables mirror the corpus export" is what the load stands on.

    The last block is decision 194, and it is the half that was recorded wrongly across this
    milestone: `features.py:403` counts company rows into the thin-title meta block and
    `'companies'` sits in `_COUNT_KEYS`, so `_n_companies` really does go non-zero — but
    `n_companies_log` is a column of no contract this app has loaded, and `build_vector`
    (features.py:104-111) counts a key the block does not declare as a miss and moves on. The
    count is produced and discarded. Asserted here rather than argued in a comment, because the
    two acts a reader draws from the wrong version are both wrong: re-running placement over
    19,071 titles that would not move, or writing the counts into columns §4.3 calls "the
    exhaustive definition of the tower's input".
    """
    root = fx.make_bundle(tmp_path / "bundle")
    _add_company_rows(root)
    report = await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    assert "title_company" not in report.skipped_tables
    assert report.table_counts["loaded:title_company"] == 4

    rows = await db.fetch(
        "SELECT source, company FROM title_company WHERE title_id = 1 ORDER BY source, company"
    )
    assert [(r["source"], r["company"]) for r in rows] == [
        ("omdb", "Warner Bros."), ("tmdb", "Regency Enterprises"), ("tmdb", "Warner Bros."),
    ], "the two sources naming one company must survive as two rows"
    # The column this app has no home for is a report line, not a silent drop (§4.1).
    assert "country" in report.unmapped_columns["title_company"]

    # ...and the feature contract's thin-title signal can see them. `_meta` is the block
    # `features.py` builds by hand, and `_n_companies` is the count `_COUNT_KEYS` turns into
    # `n_companies_log`.
    meta = await features._meta(db, [1, 2], "v1")
    assert meta[1]["_n_companies"] == 3.0
    assert meta[2]["_n_companies"] == 0.0, "a title with no company rows still produces a block"

    # ...and the count reaches no coordinate. `n_companies_log` is in `contract.META_PRODUCTIONS`'
    # grammar and in the shipped contract's `meta` block nowhere: v20260828 declares 57 one-hot
    # columns there (13 `decade:`, 2 `kind:`, 5 `runtime:`, 37 `lang:`) and the fixture's
    # `_contract_columns` builds the same four families, so `Block.column` answers None and
    # `build_vector` counts the key as unmapped. Whatever loading this table changes, it is not
    # an input the checkpoint was trained on. [decision 194]
    shipped = FeatureContract.load_path(
        next((tmp_path / "artifacts").rglob("feature_contract.json"))
    )
    assert shipped.block("meta").column("n_companies_log") is None, (
        "a contract that DOES declare the column changes this milestone's argument entirely: "
        "the count would then reach a coordinate and every seeded placement would be stale"
    )
    assert any(name.startswith("kind:") for name in shipped.block("meta").names), (
        "the meta block is empty, so the assertion above passes by being unable to fail"
    )


def _add_second_video_source(root: Path) -> None:
    """Two sources reporting one trailer — the shape that turns a clean validate into a unique
    violation on COPY under the app's pre-0018 key.

    The shipped bundle has exactly one distinct `title_video.source` (`tmdb`) and zero duplicate
    groups, so no artifact can falsify the key; only a fixture can. `make_bundle.py`'s `VIDEOS`
    would have to gain a second row sharing `(title_id, site, key)` under a different `source`
    for this helper to become unnecessary.
    """
    db = sqlite3.connect(root / "content.sqlite")
    with db:
        db.execute(
            "INSERT INTO title_video (title_id, source, key, site, type) VALUES (?,?,?,?,?)",
            (1, "omdb", "heat-trailer-key", "YouTube", "Trailer"),
        )
    db.close()


async def test_two_video_sources_sharing_a_site_and_key_both_land(db, tmp_path):
    """§4.1 "tables mirror the corpus export"; 0018 section 4.

    The corpus keys `title_video` (title_id, source, key); the app keyed it (title_id, site, key)
    and dropped `source` at the mapping, so the first export in which a second source lists a
    trailer the first already has raises `UniqueViolationError` inside the transaction that
    carries the whole seed — a 500 reading "database error" while a household watches an import.
    Latent rather than live, which is precisely the shape 0015's three tables had the week before
    they were live.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    _add_second_video_source(root)
    report = await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    assert report.table_counts["loaded:title_video"] == len(fx.VIDEOS) + 1
    rows = await db.fetch(
        "SELECT source, site, key FROM title_video WHERE title_id = 1 ORDER BY source"
    )
    assert [(r["source"], r["site"], r["key"]) for r in rows] == [
        ("omdb", "YouTube", "heat-trailer-key"), ("tmdb", "YouTube", "heat-trailer-key"),
    ]


# The four frozen ids that state no version. Measured read-only against v20260828: `url`,
# `license` and `notes` are non-NULL and non-empty on all eleven rows, and `version` is a real
# string on seven — `rt_kaggle`, `api v2`, `ml-32m` and their siblings on 7, 11, 21, 23, 26, 28
# and 31 — while ids 1 (tmdb-users), 2 (metacritic-users), 3 (metacritic-critics) and 4
# (trakt-comments) ship the empty string. [M4.9 review cycle 1: M49-MIG-04]
UNVERSIONED_RATING_SOURCE_IDS = (1, 2, 3, 4)


def _add_rating_source_terms(root: Path) -> dict[int, tuple[str, str, str, str]]:
    """The per-dataset terms the corpus ships on all eleven frozen ids.

    The fixture writes id/name/family/audience/origin/scale_lo/scale_hi and leaves the four terms
    columns NULL, because until 0018 section 5 there was nowhere for them to land.

    The values below carry the artefact's SHAPE and not a uniformly populated one: four of the
    eleven state no version, and the empty string is what they state it with. That is why the
    mapping leaves these four columns out of `coalesce_empty` (rule 6's NULL-to-`''`): a NULL
    means the bundle said nothing, `''` means the dataset publishes none, and the importer must
    not merge the two before the card has had a chance to render both as absent.
    """
    terms = {
        i: (
            f"https://example.invalid/dataset/{i}",
            "CC BY 4.0" if i % 2 else "research use only, no redistribution",
            "" if i in UNVERSIONED_RATING_SOURCE_IDS else "2026-08",
            f"attribution: dataset {i} authors",
        )
        for i in fx.RATING_SOURCE_IDS
    }
    db = sqlite3.connect(root / "content.sqlite")
    with db:
        db.executemany(
            "UPDATE rating_source SET url = ?, license = ?, version = ?, notes = ? WHERE id = ?",
            [(*values, i) for i, values in terms.items()],
        )
    db.close()
    return terms


async def test_rating_source_url_license_version_and_notes_survive_the_import(db, tmp_path):
    """§4.1 rule 4 + §10: the eleven frozen ids arrive with the terms that govern them.

    This is the one place the corpus recorded them — the Netflix Prize's research-use-only
    clause, the CC BY attributions naming their authors — and `load.py:191-198` mapped
    `id/name/scale_hi` alone, so no surface could print the attribution those licences require
    and no operator could tell which source bars redistribution of a movie-data archive.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    shipped = _add_rating_source_terms(root)
    await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    rows = await db.fetch(
        "SELECT id, url, license, version, notes FROM rating_source ORDER BY id"
    )
    assert [r["id"] for r in rows] == sorted(fx.RATING_SOURCE_IDS), "rule 4's frozen ids"
    assert {
        r["id"]: (r["url"], r["license"], r["version"], r["notes"]) for r in rows
    } == shipped
    # The restrictive licence has to stay legible as such, not collapse into an empty string.
    assert [r["id"] for r in rows if "no redistribution" in (r["license"] or "")], (
        "a source barring redistribution must still say so after the import"
    )
    # And an unversioned dataset arrives unversioned: the empty string four of the eleven ship
    # on v20260828 is stored as the empty string, neither turned into a NULL nor invented into a
    # value. It is the Data card's job to render that as absent rather than as a blank cell.
    assert {r["id"] for r in rows if r["version"] == ""} == set(UNVERSIONED_RATING_SOURCE_IDS)


async def test_the_data_card_reads_the_terms_the_import_carried(db, tmp_path):
    """The other half of the same row: the payload §6.6's sources-and-terms list renders from.

    The test above proves the four columns land in `rating_source`; nothing proved they leave it
    again. `admin.data_sources` is the route the Data card fetches (step 8.3: "no UI beyond that
    list"), and it could have been reduced to the `id/name/scale` it answered before this
    milestone with every gate still green — the row would have kept printing as covered while
    the surface that answers "which of the eleven bars redistribution" had nothing to print.
    Called directly rather than over HTTP because the claim is about the SELECT and this file
    has a real Postgres and a real import; `test_api_gating.py` owns the route's admin gate.
    [M4.9 review cycle 1: M49-MIG-02]
    """
    root = fx.make_bundle(tmp_path / "bundle")
    shipped = _add_rating_source_terms(root)
    await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    card = await admin_api.data_sources(None, db)

    assert [s["id"] for s in card["sources"]] == sorted(fx.RATING_SOURCE_IDS), "rule 4's ids"
    dropped = sorted({"url", "license", "version", "notes"} - set(card["sources"][0]))
    assert not dropped, f"the Data card's own route stopped answering with {dropped}"
    assert {
        s["id"]: (s["url"], s["license"], s["version"], s["notes"]) for s in card["sources"]
    } == shipped, "the terms reach the card exactly as the bundle stated them"


def _add_ml_links(root: Path) -> None:
    """MovieLens's link table as the corpus exports it — keyed by external ids, with no
    `title_id` for the app to read.

    One row names an imdb_id the fixture's spine does not have, because `_resolve_ml_links`
    reports how many of the shipped links resolved and a run where every one resolves cannot
    tell that number apart from a count of rows.
    """
    db = sqlite3.connect(root / "content.sqlite")
    imdb = [r[0] for r in db.execute("SELECT imdb_id FROM title WHERE imdb_id IS NOT NULL")]
    assert len(imdb) >= 3, "the fixture spine must carry imdb ids for the link to resolve on"
    with db:
        db.execute(
            "CREATE TABLE ml_link (movie_id INTEGER PRIMARY KEY, imdb_id TEXT, tmdb_id INTEGER)"
        )
        db.executemany(
            "INSERT INTO ml_link (movie_id, imdb_id, tmdb_id) VALUES (?,?,?)",
            [(i + 1, value, None) for i, value in enumerate(imdb[:3])]
            + [(9001, "tt0000000", None)],
        )
    db.close()


async def test_the_ml_link_resolution_reports_how_many_links_landed(db, tmp_path):
    """§4.3's genome block reads through this join, so §10 owes a count of it.

    The join is on `imdb_id`, the key §4.1 names as one that "must never be the join key", and
    the exception is unavoidable: MovieLens keys its links by external ids and rule 6 rules out
    `tmdb_id`, which is legitimately duplicated across the movie/series pair. What makes the
    exception safe is the refusal in `test_bundle_validation.py` plus this line — 8,022 of 9,826
    on the shipped bundle, and a number that moves is a bundle whose spine changed under its
    links.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    _add_ml_links(root)
    report = await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    assert await db.fetchval("SELECT count(*) FROM ml_link WHERE title_id IS NOT NULL") == 3
    notes = [f for f in report.findings if f.rule == "ml-link"]
    assert [f.severity for f in notes] == ["note"]
    assert notes[0].detail == {"linked": 3, "total": 4}
    assert "3 of 4 MovieLens links resolved to a title by imdb_id" in notes[0].message


async def test_the_import_narrates_its_two_model_writes_on_the_rail(db, bundle, tmp_path):
    """§6.7's rail "narrates **every model write** in one human-readable line", and decision 189
    records the two this process performs.

    `bundle_swap` and `reconcile` have sat in `EVENT_KINDS` wearing a `ModelRail` colour rule
    since M2 with no caller anywhere, so the one event that invalidates every fitted number in
    the app — the flip — reached the rail from nowhere. Both are household-scoped: a re-import
    belongs to the install rather than to whoever pressed the button, and `rail.recent` merges
    the household buffer into every member's rail, which is what makes the line able to explain
    a Home page that changed under all of them.

    The buffer is a process-global ring; `forget()` empties it so this test reads its own two
    events rather than whatever an earlier test left behind.
    """
    rail.forget()

    await _import(db, bundle, tmp_path / "artifacts")

    events = rail.recent(user_id=1)          # newest first
    assert [e["kind"] for e in events] == ["bundle_swap", "reconcile"]
    assert {e["scope"] for e in events} == {"household"}
    assert {e["bundle"] for e in events} == {"test-v1"}
    swap, reconcile = events
    assert swap["text"].startswith("bundle_swap(test-v1) = active")
    assert "every fitted number is expressed in this basis" in swap["text"]
    # Nothing was active before, so the line has no "superseding" clause to make up.
    assert "superseding" not in swap["text"] and swap["detail"]["superseded"] is None
    assert reconcile["text"].startswith("reconcile(test-v1) = ")
    assert reconcile["detail"]["steps"] == list(bundle_import.REBUILD_SET)


def _add_a_marked_but_unrepairable_review(root: Path) -> str:
    """One review body carrying a mojibake marker the conservative repair declines.

    This is the shipped corpus's actual state in miniature: 86 of 485,602 rows carry a marker and
    none repairs, because the damage is a truncated sequence rather than a whole-string cp1252
    round trip. The committed fixture's three bodies are all clean, so without this row the
    import cannot reach the branch at all. `make_bundle.py` would have to gain a marked body for
    it to become unnecessary.
    """
    body = "Un film Ãƒ voir"
    db = sqlite3.connect(root / "reviews.sqlite")
    with db:
        db.execute(
            "INSERT INTO review (title_id, source, author, author_kind, body)"
            " VALUES (?,?,?,?,?)",
            (1, "letterboxd", "user", "user", body),
        )
    db.close()
    return body


async def test_marked_review_rows_that_repair_nothing_are_a_warning_not_a_note(db, tmp_path):
    """§4.1 rule 8 + §10: "0 repaired" over a clean corpus and over a broken one are two facts.

    The old line was a note reading "0 review row(s) repaired … (expected around 73)", which is
    what a clean bundle prints and what the shipped one prints, and the report could not tell
    them apart. Nothing in this repository enumerates those 73 rows, so the expectation was a
    claim the report had no way to check; what it can say is how many rows carry a marker, and
    that none of them could be repaired without guessing at bytes the corpus lost. The repair
    stays conservative on purpose — `Ã` plus a non-continuation byte is ambiguous between
    é/ã/á/à and would corrupt `L'Âge d'Or`. [M4.9 finding 33]
    """
    root = fx.make_bundle(tmp_path / "bundle")
    body = _add_a_marked_but_unrepairable_review(root)
    report = await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    rule8 = [f for f in report.findings if f.rule == "rule8-mojibake"]
    assert [f.severity for f in rule8] == ["warn"], report.render()
    assert rule8[0].detail == {"marked": 1, "repaired": 0, "total": 4}
    assert "expected around 73" not in report.render()

    # Rule 8's other half, in the same breath: the row is stored exactly as it arrived. A
    # warning is what the app owes here, not a repair it cannot make.
    assert await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE body = $1", body
    ) == 1


async def test_every_dna_row_carries_the_terms_own_facet_prefix(db, bundle, tmp_path):
    """§4.3 + §6.8: the app's facet is the vocabulary's facet id, which is the term's prefix.

    `dna_facet`, `dna_term`, §6.4's axes and §6.8's fixed colour per facet all key on it, and
    `0004_dna.sql:73-88` gives neither tag table an FK to `dna_facet` — so a `facet` that joins
    nothing raises nothing anywhere. On the shipped bundle 29,188 of 31,540 `dna_tag` rows and
    206,151 of 223,136 `dna_projected` rows were in that state, which is 92.5% of the chips on
    the card rendering in the neutral colour §6.8 reserves for "no facet".
    """
    shipped = {row[2] for row in fx.EXTRACTED} | {row[2] for row in fx.PROJECTED}
    assert shipped & set(fx.EXTRACTION_LABELS.values()), (
        "the fixture must ship the corpus's extraction labels or this test asserts nothing"
    )

    await _import(db, bundle, tmp_path / "artifacts")

    for table in ("dna_tag", "dna_projected"):
        unjoinable = await db.fetchval(
            f"SELECT count(*) FROM {table} g "
            "LEFT JOIN dna_facet f ON f.version = g.version AND f.facet = g.facet "
            "WHERE f.facet IS NULL"
        )
        assert unjoinable == 0, f"{table} rows carry a facet no dna_facet row has"
        wrong = await db.fetchval(
            f"SELECT count(*) FROM {table} WHERE facet <> split_part(term, '.', 1)"
        )
        assert wrong == 0, f"{table}.facet is not the term's own prefix"

    # The label is gone from the data — and it was not simply absent from the bundle.
    stored = {r["facet"] for r in await db.fetch("SELECT DISTINCT facet FROM dna_tag")}
    assert not stored & set(fx.EXTRACTION_LABELS.values())
    assert "characters" in stored, "the facet whose palette entry was misspelled for seven files"
