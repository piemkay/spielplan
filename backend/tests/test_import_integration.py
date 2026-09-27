"""End-to-end import against a real Postgres 16 (§4.1, §10). `copy_records_to_table` picks encoders from the
destination column types, so type mismatches only exist against a real server. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import tarfile
from pathlib import Path

import asyncpg
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


def _models_only(root: Path) -> Path:
    """Re-inventoried, or deleting two listed files would be a different refusal from the one under test."""
    (root / "content.sqlite").unlink()
    (root / "reviews.sqlite").unlink()
    fx.reinventory(root)
    return root


async def _import(db, bundle, artifacts_root: Path):
    report = await bundle_import.import_bundle(db, bundle, artifacts_root)
    assert report.ok, report.render()
    return report


async def test_bundle_imports_clean(db, bundle, tmp_path):
    report = await _import(db, bundle, tmp_path / "artifacts")

    assert report.table_counts["loaded:title"] == len(fx.TITLES)
    assert report.table_counts["loaded:dna_tag"] == len(fx.EXTRACTED)
    assert report.table_counts["loaded:dna_projected"] == len(fx.PROJECTED)
    assert report.table_counts["loaded:review_store.review"] == 3
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)


async def test_sqlite_integer_booleans_reach_postgres_boolean_columns(db, bundle, tmp_path):
    """SQLite has no boolean type; binary COPY raises on `title.is_owned` without a cast."""
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
    """A jsonb column read back as text is a string every consumer iterates by character."""
    await _import(db, bundle, tmp_path / "artifacts")
    report = await db.fetchval("SELECT report FROM artifact_bundle WHERE version = 'test-v1'")
    assert isinstance(report, dict)
    assert report["ok"] is True
    manifest = await db.fetchval("SELECT manifest FROM artifact_bundle WHERE version = 'test-v1'")
    assert isinstance(manifest, dict)
    # `artifact_bundle.manifest` is BUNDLE.json, the corpus's
    # identity record, not `artifacts/manifest.json`.
    assert {"bundle_version", "tables", "files"} <= set(manifest)
    assert manifest["bundle_version"] == "test-v1"
    assert await db.fetchval(
        "SELECT vocabulary_version FROM artifact_bundle WHERE version = 'test-v1'"
    ) == "v1"


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


async def test_reimporting_the_same_bundle_is_refused_while_it_is_active(db, bundle, tmp_path):
    """At the seed's version decision 162's refusal fires first; the version rule needs a second version."""
    await _import(db, bundle, tmp_path / "artifacts")
    _models_only(bundle.root)

    at_the_seeds_version = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(bundle.root), tmp_path / "artifacts"
    )
    assert not at_the_seeds_version.ok
    assert any(f.rule == "seed-once" for f in at_the_seeds_version.failures)
    assert any(
        "under its own version string" in f.message for f in at_the_seeds_version.failures
    )

    # The version rule at the only version that can reach it: an active model bundle offered again.
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    _models_only(tmp_path / "bundle2")
    await _import(db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts")

    again = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts"
    )
    assert not again.ok
    assert any("already the active bundle" in f.message for f in again.failures)


async def test_a_second_bundle_version_swaps_the_models_over_the_seeded_content(db, bundle, tmp_path):
    """Decision 162: models re-ship, content does not; the seeded spine must come through untouched."""
    await _import(db, bundle, tmp_path / "artifacts")
    spine = [dict(r) for r in await db.fetch("SELECT id, name, kind FROM title ORDER BY id")]

    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    _models_only(tmp_path / "bundle2")
    second = bundle_import.Bundle.open(tmp_path / "bundle2")
    report = await _import(db, second, tmp_path / "artifacts")

    # Not `== 0`: this bundle ships no spine, which the report says in words.
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
    """§10: observations survive re-import; `user_vector.bundle_version` is SET NULL, not CASCADE."""
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

    # Decision 162's re-import shape: the corpus re-ships models, never content.
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    _models_only(tmp_path / "bundle2")
    await _import(db, bundle_import.Bundle.open(tmp_path / "bundle2"), tmp_path / "artifacts")

    assert await db.fetchval("SELECT count(*) FROM verdict") == 1
    assert await db.fetchval("SELECT count(*) FROM user_title") == 1


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
    """An empty selection meaning "everything" is the unpartitioned query rule 5 prevents."""
    await _import(db, bundle, tmp_path / "artifacts")
    with pytest.raises(ValueError, match="at least one kind"):
        await library.list_titles(db, kinds=[])


async def test_hidden_counts_report_the_unselected_kind(db, bundle, tmp_path):
    """§6.0: a toggle that hides things has to say how many."""
    await _import(db, bundle, tmp_path / "artifacts")
    assert await library.count_by_kind(db, exclude=["movie"]) == {"series": 2}
    assert await library.count_by_kind(db, exclude=["movie", "series"]) == {}


async def test_a_person_filter_keeps_the_kind_partition(db, bundle, tmp_path):
    """The person filter does NOT suspend the partition; both kinds is how a filmography is seen."""
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
    # The fixture's tmdb "Sci-Fi" answers decision 473's canonical "Science Fiction".
    assert "Science Fiction" in both_genres and "Science Fiction" not in movie_genres


async def test_library_search_matches_titles_and_aliases(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")
    hits, total = await library.list_titles(db, kinds=["movie"], q="chungking")
    assert total >= 1
    # Id 5 carries "Chungking Express" only as an ALIAS; its name is the CJK original.
    assert 5 in {t["id"] for t in hits}


# `role_class = 'cast'`, as the corpus writes on every credit; a classless row would stand apart.
_CROSS_DEPARTMENT = (
    "INSERT INTO credit (title_id, person_id, department, job, character, billing_order, source,"
    " role_class) VALUES ($1, $2, $3, 'Actor', $4, $5, $6, 'cast')"
)


async def test_a_credit_is_one_row_per_person_and_job_across_department_spellings(
    db, bundle, tmp_path
):
    """One job under two department spellings used to yield
    duplicate keys that crashed the card's keyed each."""
    await _import(db, bundle, tmp_path / "artifacts")

    # This insert adds a second SOURCE, a different character spelling and a later billing order.
    await db.execute(_CROSS_DEPARTMENT, 1, 4, "Actor", "Lt. Hanna", 6, "omdb")
    assert await db.fetchval(
        "SELECT count(*) FROM credit WHERE title_id = 1 AND person_id = 4"
    ) == 3

    credits = await library.credits_for(db, 1)
    rows = [c for c in credits if c["person_id"] == 4 and c["job"] == "Actor"]
    assert len(rows) == 1
    # Both spellings stay visible, while `department` resolves to TMDB's canonical one.
    assert sorted(rows[0]["departments"]) == ["Acting", "Actor"]
    assert rows[0]["department"] == "Acting"
    assert sorted(rows[0]["sources"]) == ["omdb", "tmdb"]

    # The client key is total: one `person_id:(role_class ?? job)` per row, as `TitleDetail.svelte` keys.
    keys = {f"{c['person_id']}:{c['role_class'] or c['job']}" for c in credits}
    assert len(keys) == len(credits)

    # The directing-first sort survives losing `c.department` as a grouping column.
    assert credits[0]["job"] == "Director"

    # The lowest billing order's character: an unordered `array_agg(...)[1]` depended on COPY order.
    assert rows[0]["character"] == "Vincent Hanna"
    await db.execute("DELETE FROM credit WHERE title_id = 1 AND person_id = 4")
    await db.execute(_CROSS_DEPARTMENT, 1, 4, "Actor", "Lt. Hanna", 6, "omdb")
    await db.execute(_CROSS_DEPARTMENT, 1, 4, "Acting", "Vincent Hanna", 1, "tmdb")
    reinserted = await library.credits_for(db, 1)
    again = next(c for c in reinserted if c["person_id"] == 4 and c["job"] == "Actor")
    assert again["character"] == "Vincent Hanna"


_CREDIT = (
    "INSERT INTO credit (title_id, person_id, department, job, billing_order, source, role_class)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7)"
)


async def test_a_crew_member_is_one_row_per_role_across_job_spellings(db, bundle, tmp_path):
    """One row per (person, class): TMDB's label on it, every spelling in `jobs`, every agreeing source."""
    await _import(db, bundle, tmp_path / "artifacts")
    await db.execute("INSERT INTO person (id, name) VALUES (70, 'Elliot Goldenthal')")
    await db.executemany(_CREDIT, [
        (1, 70, "Sound", "Original Music Composer", None, "tmdb", "composer"),
        (1, 70, "Music", "Composer", None, "wikidata", "composer"),
        (1, 70, "Sound", "Original Music Composer", None, "correction", "crew"),
        (1, 1, "Writing", "Writer", None, "tmdb", "writer"),
        (1, 1, "Writing", "Screenplay", None, "wikidata", "writer"),
    ])

    credits = await library.credits_for(db, 1)
    goldenthal = [c for c in credits if c["person_id"] == 70]
    assert len(goldenthal) == 1, goldenthal
    assert goldenthal[0]["role_class"] == "composer"
    assert goldenthal[0]["job"] == "Original Music Composer"
    assert goldenthal[0]["jobs"] == ["Original Music Composer", "Composer"]
    assert goldenthal[0]["sources"] == ["correction", "tmdb", "wikidata"]

    mann = [c for c in credits if c["person_id"] == 1]
    assert [(c["role_class"], c["job"]) for c in mann] == [
        ("director", "Director"), ("writer", "Writer"),
    ]
    assert mann[1]["jobs"] == ["Writer", "Screenplay"]
    assert credits[0]["role_class"] == "director", "the directing-first order did not survive"
    keys = [f"{c['person_id']}:{c['role_class'] or c['job']}" for c in credits]
    assert len(set(keys)) == len(keys)


async def test_one_name_with_ids_that_cannot_disagree_is_one_credit_row(db, bundle, tmp_path):
    """Same `loose_name`, same class, ids that cannot disagree: one row naming all `person_ids`."""
    await _import(db, bundle, tmp_path / "artifacts")
    await db.executemany(
        "INSERT INTO person (id, name, imdb_id, tmdb_id) VALUES ($1, $2, $3, $4)",
        [
            (80, "John Williams", "nm0002354", None), (81, "John Williams", None, 491),
            (82, "John  Williams", None, None),
            (90, "Sam Jones", "nm0000001", None), (91, "Sam Jones", "nm0000002", None),
            (95, "王家卫", None, None), (96, "王家卫", None, None),
        ],
    )
    await db.executemany(_CREDIT, [
        (1, 80, "Sound", "Original Music Composer", None, "wikidata", "composer"),
        (1, 81, "Sound", "Original Music Composer", None, "tmdb", "composer"),
        (1, 82, "Music", "Composer", None, "omdb", "composer"),
        (1, 90, "Acting", "Actor", 7, "tmdb", "cast"),
        (1, 91, "Acting", "Actor", 8, "wikidata", "cast"),
        (1, 95, "Editing", "Editor", None, "tmdb", "editor"),
        (1, 96, "Editing", "Editor", None, "wikidata", "editor"),
    ])

    credits = await library.credits_for(db, 1)
    williams = [c for c in credits if c["name"].startswith("John")]
    assert len(williams) == 1, williams
    assert williams[0]["person_ids"] == [80, 81, 82]
    assert williams[0]["person_id"] == 80
    assert williams[0]["job"] == "Original Music Composer"
    assert len([c for c in credits if c["name"] == "Sam Jones"]) == 2
    assert len([c for c in credits if c["name"] == "王家卫"]) == 2


async def test_title_card_payload_is_complete(db, bundle, tmp_path):
    await _import(db, bundle, tmp_path / "artifacts")

    title = await library.get_title(db, 1)
    assert title["name"] == "Heat"

    credits = await library.credits_for(db, 1)
    # The fixture stores the director twice (tmdb, omdb); the card shows one row citing both.
    directors = [c for c in credits if c["job"] == "Director"]
    assert len(directors) == 1
    assert sorted(directors[0]["sources"]) == ["omdb", "tmdb"]
    # Four stored rows behind three rendered: dedupe at read time, never at import.
    assert await db.fetchval("SELECT count(*) FROM credit WHERE title_id = 1") == 4

    # `version` is required: two imported bundles cannot put two vocabularies on one card.
    dna = await library.dna_for(db, 1, version="v1")
    # The corpus's term ids are `<facet>.<term>`, as the contract's `dna:` columns key them.
    assert {t["term"] for t in dna["extracted"]} == {"themes.obsession", "characters.morally_grey"}
    assert {t["term"] for t in dna["projected"]} == {"themes.obsession", "era.period"}

    # Evidence must be a list of dicts, not a JSON string iterated by character.
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
    """`list_titles` counts `$N` by hand; every filter at once is where an off-by-one shows."""
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
    """Ada Cross-Kind is credited on a film and a series so this can fail."""
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


async def test_the_report_counts_every_loaded_table(db, bundle, tmp_path):
    """§10's per-table counts are what a re-import is diffed against."""
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
    """The corpus owns its column names; a new one must be visible, not dropped."""
    import sqlite3

    con = sqlite3.connect(bundle.content_db)
    con.execute("ALTER TABLE title ADD COLUMN some_new_corpus_column TEXT")
    con.commit()
    con.close()
    fx.reinventory(bundle.root)

    report = await _import(db, bundle, tmp_path / "artifacts")
    assert "some_new_corpus_column" in report.unmapped_columns.get("title", [])


async def test_a_models_only_reimport_leaves_the_seed_counts_standing_as_the_diff(db, bundle, tmp_path):
    """The seed's report must survive the swap and stay true of the spine afterwards."""
    first = await _import(db, bundle, tmp_path / "artifacts")
    fx.make_bundle(tmp_path / "bundle2", version="test-v2")
    _models_only(tmp_path / "bundle2")
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

    # The flip must not overwrite the report the new bundle is diffed against.
    stored = {
        r["version"]: r["report"]
        for r in await db.fetch("SELECT version, report FROM artifact_bundle")
    }
    assert stored["test-v1"]["counts"]["loaded:title"] == first.table_counts["loaded:title"]
    assert "loaded:title" not in stored["test-v2"]["counts"]


async def test_the_import_recomputes_the_rebuild_set_before_it_flips(db, bundle, tmp_path):
    """§10: recompute the rebuild set against the STAGED bundle, before the
    flip. Step 3 must stamp the staged version, or the cache refuses the
    fit after the flip; the basis itself is `test_model_basis.py`'s."""
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.models.artifacts import ArtifactStore

    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('Patrick', 'admin') RETURNING id"
    )
    report = await _import(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    for title_id, value in ((1, 2), (2, 1), (3, 0), (4, 2), (5, 1)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)

    # A models-only re-import at a second version, the case §10 is about under decision 162.
    fx.make_bundle(tmp_path / "b2", version="test-v2")
    _models_only(tmp_path / "b2")
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

    # The stamp names the staged bundle, so the cache ACCEPTS the fit across the flip.
    stamped = await db.fetchval(
        "SELECT bundle_version FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", patrick
    )
    assert stamped == "test-v2", f"the rebuild's fit claims {stamped!r}, not the staged bundle"
    hp, _notes = load_hp(ArtifactStore.open(tmp_path / "artifacts" / "test-v2", "test-v2"))
    assert await refit.load_cache(
        db, user_id=patrick, kind="movie", hp=hp, lock=False
    ) is not None, "load_cache refused the fit the rebuild made one statement earlier"


async def test_a_freshly_activated_bundle_serves_its_cold_titles_immediately(db, bundle, tmp_path):
    """The Cold Tower re-placement runs before the priors and
    scores that read it, whatever §10's listing order."""
    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('Patrick', 'admin') RETURNING id"
    )
    report = await _import(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    # Title 8 has no Backbone row, so it exists only if step 4 ran before the priors.
    priced = await db.fetchrow(
        "SELECT b, e_source FROM title_prior WHERE title_id = 8 AND bundle_version = 'test-v1'"
    )
    assert priced is not None, "the cold title has no prior at all"
    assert priced["e_source"] == "cold_tower", (
        f"a freshly activated bundle prices its cold title as {priced['e_source']!r} — the "
        "fold-in ran before the placement it reads"
    )
    assert priced["b"] is not None

    # The report reads in run order.
    rebuild = [f.message for f in report.findings if f.rule == "rebuild"]
    assert len(rebuild) == 3
    assert "Cold Tower" in rebuild[0] and "fold-in" in rebuild[1]
    assert patrick


def _tarred(root: Path, target: Path) -> Path:
    """A directory never unpacks, so only an archive reaches `_unpack`'s tree."""
    with tarfile.open(target, "w") as tar:
        tar.add(root, arcname=root.name)
    return target


async def test_a_committed_import_removes_the_tree_it_unpacked(db, tmp_path):
    """The unpacked tree is a 790 MB second copy on the household's disk; a committed import removes it."""
    fx.make_bundle(tmp_path / "bundle")
    archive = _tarred(tmp_path / "bundle", tmp_path / "spielplan-bundle.tar")
    bundle = bundle_import.Bundle.open(archive)

    unpacked = tmp_path / ".unpacked-spielplan-bundle.tar"
    assert unpacked.is_dir() and (unpacked / "bundle" / "content.sqlite").is_file()

    report = await _import(db, bundle, tmp_path / "artifacts")

    assert not unpacked.exists()
    assert [f.message for f in report.findings if f.rule == "cleanup"] == [
        f"removed the unpacked bundle tree at {unpacked}"
    ]
    # What survives is what §10 says: the staged artifacts and the archive itself.
    assert (tmp_path / "artifacts" / "test-v1").is_dir()
    assert archive.is_file()


async def test_a_failed_import_keeps_its_unpacked_tree_for_the_retry(db, tmp_path):
    """A refusal is when the operator retries, so the tree is kept rather than re-extracted."""
    fx.make_bundle(tmp_path / "models", version="test-v2")
    _models_only(tmp_path / "models")
    archive = _tarred(tmp_path / "models", tmp_path / "models-only.tar")
    bundle = bundle_import.Bundle.open(archive)

    unpacked = tmp_path / ".unpacked-models-only.tar"
    assert unpacked.is_dir()

    report = await bundle_import.import_bundle(db, bundle, tmp_path / "artifacts")

    assert not report.ok, report.render()
    assert unpacked.is_dir(), "the retry would have to unpack the whole bundle again"


async def test_a_cleanup_that_could_not_remove_the_tree_says_so_rather_than_claiming_it_did(
    db, tmp_path, monkeypatch
):
    """`rmtree(ignore_errors=True)` swallows EACCES; the note must follow the outcome, not the call."""
    fx.make_bundle(tmp_path / "bundle")
    archive = _tarred(tmp_path / "bundle", tmp_path / "spielplan-bundle.tar")
    bundle = bundle_import.Bundle.open(archive)
    unpacked = tmp_path / ".unpacked-spielplan-bundle.tar"
    monkeypatch.setattr(bundle_import.shutil, "rmtree", lambda *a, **k: None)

    report = await _import(db, bundle, tmp_path / "artifacts")

    assert unpacked.is_dir(), "this test proves nothing if the tree is gone"
    notes = [f.message for f in report.findings if f.rule == "cleanup"]
    assert notes == [
        f"could not remove the unpacked bundle tree at {unpacked} - delete it by hand"
    ], "a cleanup that removed nothing reported a removal"


async def test_the_one_exit_that_returns_no_report_still_says_what_it_did_to_the_disk(
    db, tmp_path, monkeypatch, caplog
):
    """The `BaseException` arm re-raises, so its disk conclusion must reach a report that survives."""
    fx.make_bundle(tmp_path / "bundle")
    bundle = bundle_import.Bundle.open(tmp_path / "bundle")
    artifacts_root = tmp_path / "artifacts"
    staged = artifacts_root / bundle.version

    async def abandoned(*args, **kwargs):
        raise KeyboardInterrupt("the worker was stopped at its budget")

    monkeypatch.setattr(bundle_import.content_loader, "load_content", abandoned)
    monkeypatch.setattr(bundle_import.shutil, "rmtree", lambda *a, **k: None)
    caplog.set_level(logging.WARNING, logger="spielplan.importer")

    with pytest.raises(KeyboardInterrupt):
        await bundle_import.import_bundle(db, bundle, artifacts_root)

    assert staged.is_dir(), "this test proves nothing if the staged tree is gone"
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0, (
        "the transaction rolled back, so nothing indexes that directory"
    )
    logged = [r.getMessage() for r in caplog.records if r.name == "spielplan.importer"]
    assert logged, "the one exit with no report to return left no trace at all"
    assert any(str(staged) in m and "could not be removed" in m for m in logged), logged
    assert all(bundle.version in m for m in logged), logged


# Each helper writes a corpus shape the committed fixture lacks, from `real_bundle_shapes.json`.


def _add_company_rows(root: Path) -> None:
    """`title_company` keyed per source; `country` is shipped and deliberately unmapped."""
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
                # One company, one role, one title, two sources: a collision under the old key.
                (1, "tmdb", "Warner Bros.", "production", "US"),
                (1, "omdb", "Warner Bros.", "production", "US"),
                (1, "tmdb", "Regency Enterprises", "production", "US"),
                (3, "tmdb", "StudioCanal", "production", "GB"),
            ],
        )
    db.close()
    fx.reinventory(root)


async def test_title_company_lands_per_source_and_is_not_reported_skipped(db, tmp_path):
    """Decision 193: rows land per source. Decision 194: the company count reaches no contract column."""
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

    # ...and the thin-title meta block can see them.
    meta = await features._meta(db, [1, 2], "v1")
    assert meta[1]["_n_companies"] == 3.0
    assert meta[2]["_n_companies"] == 0.0, "a title with no company rows still produces a block"

    # ...and the count reaches no coordinate: `n_companies_log` is in no loaded contract.
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
    """Two sources for one trailer: a unique violation under the pre-0018 key."""
    db = sqlite3.connect(root / "content.sqlite")
    with db:
        db.execute(
            "INSERT INTO title_video (title_id, source, key, site, type) VALUES (?,?,?,?,?)",
            (1, "omdb", "heat-trailer-key", "YouTube", "Trailer"),
        )
    db.close()
    fx.reinventory(root)


async def test_two_video_sources_sharing_a_site_and_key_both_land(db, tmp_path):
    """The corpus keys `title_video` by source; dropping it would raise mid-seed."""
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


# Ids 1-4 ship `version` as the empty string on v20260828; the other seven carry one.
UNVERSIONED_RATING_SOURCE_IDS = (1, 2, 3, 4)


def _add_rating_source_terms(root: Path) -> dict[int, tuple[str, str, str, str]]:
    """`version` is not rule-6 coalesced: NULL (said nothing) and '' (publishes none) stay distinct."""
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
    fx.reinventory(root)
    return terms


async def test_rating_source_url_license_version_and_notes_survive_the_import(db, tmp_path):
    """The licences require attribution and one bars redistribution, so the terms must land."""
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
    # The restrictive licence has to stay legible as such.
    assert [r["id"] for r in rows if "no redistribution" in (r["license"] or "")], (
        "a source barring redistribution must still say so after the import"
    )
    # An unversioned dataset arrives as '', neither NULL nor invented.
    assert {r["id"] for r in rows if r["version"] == ""} == set(UNVERSIONED_RATING_SOURCE_IDS)


async def test_the_data_card_reads_the_terms_the_import_carried(db, tmp_path):
    """The Data card's route must carry the terms back out."""
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


def _add_genome_slice(root: Path) -> None:
    """One score names a `tag_id` the slice lacks, so the
    decline must not depend on the slice's integrity."""
    db = sqlite3.connect(root / "content.sqlite")
    imdb = [r[0] for r in db.execute("SELECT imdb_id FROM title WHERE imdb_id IS NOT NULL")]
    assert len(imdb) >= 3, "the fixture spine must carry imdb ids for the slice to look loadable"
    with db:
        db.execute(
            "CREATE TABLE ml_link (movie_id INTEGER PRIMARY KEY, imdb_id TEXT, tmdb_id INTEGER)"
        )
        db.executemany(
            "INSERT INTO ml_link (movie_id, imdb_id, tmdb_id) VALUES (?,?,?)",
            [(i + 1, value, None) for i, value in enumerate(imdb[:3])],
        )
        db.execute("CREATE TABLE ml_genome_tag (tag_id INTEGER PRIMARY KEY, tag TEXT NOT NULL)")
        db.executemany(
            "INSERT INTO ml_genome_tag (tag_id, tag) VALUES (?,?)",
            [(1, "heist"), (2, "dread")],
        )
        db.execute(
            "CREATE TABLE ml_genome_score (movie_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,"
            " relevance REAL NOT NULL, PRIMARY KEY (movie_id, tag_id))"
        )
        db.executemany(
            "INSERT INTO ml_genome_score (movie_id, tag_id, relevance) VALUES (?,?,?)",
            [(1, 1, 0.9), (1, 2, 0.6), (2, 1, 0.7), (2, 4242, 0.5)],
        )
    db.close()
    fx.reinventory(root)


async def test_a_bundle_carrying_the_genome_slice_is_declined_with_the_clause_it_upholds(
    db, tmp_path
):
    """Decision 291: declined with a skip note, and nothing lands in the three tables."""
    root = fx.make_bundle(tmp_path / "bundle")
    _add_genome_slice(root)
    report = await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    for table in ("ml_genome_tag", "ml_link", "ml_genome_score"):
        assert "media-graph-spec_v1.1.md:175" in report.skipped_tables[table]
        assert f"loaded:{table}" not in report.table_counts
        assert await db.fetchval(f"SELECT count(*) FROM {table}") == 0
    skipped = {f.detail["table"] for f in report.findings if f.rule == "table-skipped"}
    assert {"ml_genome_tag", "ml_link", "ml_genome_score"} <= skipped
    # The integrity gates must not refuse the seed over a table no COPY reaches.
    integrity = [f.message for f in report.findings if f.rule.startswith("integrity-")]
    assert not [m for m in integrity if "ml_" in m], integrity
    # Declined, not unaccounted for: `unaccounted_tables` would fail every real import.


async def test_a_bundle_without_the_genome_slice_says_nothing_about_it(db, bundle, tmp_path):
    """A warning about a declined table would be noise on the one screen the import speaks."""
    report = await _import(db, bundle, tmp_path / "artifacts")

    named = [f.message for f in report.findings
             if "ml_link" in f.message or "ml_genome" in f.message]
    assert not named, f"a bundle without the slice is not a bundle with a problem: {named}"
    assert not {"ml_genome_tag", "ml_link", "ml_genome_score"} & set(report.skipped_tables), (
        "only tables the bundle actually ships are reported skipped "
        "(load.py `_account_for_shipped_tables`); a decline over an absent table is an "
        "invented line"
    )


async def test_the_import_writes_no_rail_line_because_the_rail_could_not_read_it(
    db, bundle, tmp_path
):
    """Decision 263: the import runs in the worker, whose
    ring buffer nobody reads, so it writes no rail line."""
    rail.forget()

    report = await _import(db, bundle, tmp_path / "artifacts")

    assert rail.recent(user_id=1) == [], (
        "the import records rail events that only the process running it could ever read"
    )
    swap = next(f for f in report.findings if f.rule == "swap")
    assert "superseded" in swap.detail, "the fact the bundle_swap line carried has nowhere else"
    assert swap.detail["superseded"] is None, "nothing was active before this import"


def _add_a_marked_but_unrepairable_review(root: Path) -> str:
    """A mojibake marker the conservative repair declines, as the shipped corpus has 86 of."""
    body = "Un film Ãƒ voir"
    db = sqlite3.connect(root / "reviews.sqlite")
    with db:
        db.execute(
            "INSERT INTO review (title_id, source, author, author_kind, body)"
            " VALUES (?,?,?,?,?)",
            (1, "letterboxd", "user", "user", body),
        )
    db.close()
    fx.reinventory(root)
    return body


async def test_marked_review_rows_that_repair_nothing_are_a_warning_not_a_note(db, tmp_path):
    """"0 repaired" over a clean and a broken corpus are two facts; marked rows are a warning."""
    root = fx.make_bundle(tmp_path / "bundle")
    body = _add_a_marked_but_unrepairable_review(root)
    report = await _import(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    rule8 = [f for f in report.findings if f.rule == "rule8-mojibake"]
    assert [f.severity for f in rule8] == ["warn"], report.render()
    assert rule8[0].detail == {"marked": 1, "repaired": 0, "total": 4}
    assert "expected around 73" not in report.render()

    # Rule 8's other half: the row is stored exactly as it arrived.
    assert await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE body = $1", body
    ) == 1


async def test_every_dna_row_carries_the_terms_own_facet_prefix(db, bundle, tmp_path):
    """The facet is the term's prefix; neither tag table
    has an FK to `dna_facet`, so a mismatch is silent."""
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

    # The label is gone from the data, and it was not simply absent from the bundle.
    stored = {r["facet"] for r in await db.fetch("SELECT DISTINCT facet FROM dna_tag")}
    assert not stored & set(fx.EXTRACTION_LABELS.values())
    assert "characters" in stored, "the facet whose palette entry was misspelled for seven files"


# These drive `import_bundle` on two connections: the lock under test is per session.


async def _second_connection(pg_url: str):
    """`pg_try_advisory_lock` is SESSION-scoped; the codecs are copied so jsonb reads as objects."""
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


async def _staggered_imports(db, other, bundle, second, artifacts_root, monkeypatch):
    """`rebuild` is slowed so the second import arrives while the first holds the lock."""
    real_rebuild = bundle_import.rebuild

    async def slow_rebuild(*args, **kwargs):
        await asyncio.sleep(0.6)
        return await real_rebuild(*args, **kwargs)

    monkeypatch.setattr(bundle_import, "rebuild", slow_rebuild)

    winner = asyncio.create_task(bundle_import.import_bundle(db, bundle, artifacts_root))
    await asyncio.sleep(0.15)
    loser = await bundle_import.import_bundle(other, second, artifacts_root)
    return await winner, loser


async def test_two_concurrent_imports_produce_one_ok_report_and_one_named_refusal(
    db, bundle, tmp_path, pg_url, monkeypatch
):
    """Without the advisory lock the second import deleted the first's staged tree and raised a 500."""
    artifacts_root = tmp_path / "artifacts"
    fx.make_bundle(tmp_path / "again")
    other = await _second_connection(pg_url)
    try:
        first, second = await _staggered_imports(
            db, other, bundle, bundle_import.Bundle.open(tmp_path / "again"),
            artifacts_root, monkeypatch,
        )
    finally:
        await other.close()

    assert first.ok, first.render()
    assert not second.ok
    refusal = [f for f in second.failures if f.rule == "import"]
    assert len(refusal) == 1, second.render()
    assert "another import is already running" in refusal[0].message
    assert refusal[0].message.isascii()
    # No raw constraint name reaches the operator, and the install ends with one bundle.
    assert "title_alias_pkey" not in second.render()
    rows = await db.fetch("SELECT version, state FROM artifact_bundle")
    assert [(r["version"], r["state"]) for r in rows] == [("test-v1", "active")]
    assert sorted(p.name for p in artifacts_root.iterdir()) == ["test-v1"]


async def test_the_loser_never_deletes_the_winners_staged_tree(
    db, bundle, tmp_path, pg_url, monkeypatch
):
    """Counted file operations: a re-copy of the same bytes is invisible in the tree."""
    artifacts_root = tmp_path / "artifacts"
    fx.make_bundle(tmp_path / "again")
    staged_copies: list[str] = []
    removed: list[str] = []
    real_copytree, real_rmtree = bundle_import.shutil.copytree, bundle_import.shutil.rmtree
    monkeypatch.setattr(
        bundle_import.shutil, "copytree",
        lambda src, dst, *a, **k: (staged_copies.append(str(dst)), real_copytree(src, dst, *a, **k))[1],
    )
    monkeypatch.setattr(
        bundle_import.shutil, "rmtree",
        lambda path, *a, **k: (removed.append(str(path)), real_rmtree(path, *a, **k))[1],
    )

    other = await _second_connection(pg_url)
    try:
        first, second = await _staggered_imports(
            db, other, bundle, bundle_import.Bundle.open(tmp_path / "again"),
            artifacts_root, monkeypatch,
        )
    finally:
        await other.close()

    assert first.ok, first.render()
    assert not second.ok
    # `copytree` recurses into itself, so only calls whose destination IS the staged root count.
    staged = str((artifacts_root / "test-v1").resolve())
    passes = [dst for dst in staged_copies if dst == staged]
    assert passes == [staged], f"the tree was staged {len(passes)} times"
    assert staged not in removed, "the loser deleted the tree the winner was rebuilding against"


async def test_the_stored_report_carries_the_rebuild_the_swap_and_the_rebuild_set(
    db, bundle, tmp_path
):
    """The stored report was written before the rebuild; the Data tab renders only this row."""
    report = await _import(db, bundle, tmp_path / "artifacts")
    stored = await db.fetchval("SELECT report FROM artifact_bundle WHERE version = 'test-v1'")

    returned_rules = {f["rule"] for f in report.as_dict()["findings"]}
    stored_rules = {f["rule"] for f in stored["findings"]}
    assert returned_rules - stored_rules == set(), (
        "the stored report is missing rules the import produced: "
        f"{sorted(returned_rules - stored_rules)}"
    )
    assert {"rebuild", "swap", "rebuild-set"} <= stored_rules
    assert stored["ok"] is True
    rebuilt = " ".join(f["message"] for f in stored["findings"] if f["rule"] == "rebuild")
    assert all(step in rebuilt for step in bundle_import.REBUILD_SET), rebuilt

    # The fixture half of render()'s ASCII rule, over a report an import actually produced.
    rendered = report.render()
    offenders = sorted({c for c in rendered if ord(c) > 127})
    assert not offenders, (
        "render() of a real import is what `assert report.ok, report.render()` prints on a "
        "Windows console (CLAUDE.md); non-ASCII character(s): "
        + ", ".join(hex(ord(c)) for c in offenders)
    )


async def test_the_bundles_table_counts_are_compared_against_the_report(db, tmp_path):
    """The counts are compared inside the transaction; the
    manifest is edited, since BUNDLE.json hashes itself out."""
    fx.make_bundle(tmp_path / "drifted", version="test-drift")
    manifest = tmp_path / "drifted" / "BUNDLE.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["tables"] = {**payload["tables"], "title": 4242}
    manifest.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(tmp_path / "drifted"), tmp_path / "artifacts"
    )

    assert not report.ok, report.render()
    drift = next(f for f in report.failures if f.rule == "bundle-integrity")
    assert "title" in drift.message and "4,242" in drift.message
    assert drift.detail["tables"] == ["title"]
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0

    fx.make_bundle(tmp_path / "bundle")
    clean = await _import(db, bundle_import.Bundle.open(tmp_path / "bundle"), tmp_path / "artifacts")
    # Two `bundle-integrity` notes for two different checks, named so neither passes for the other.
    integrity = [f.message for f in clean.findings if f.rule == "bundle-integrity"]
    assert len(integrity) == 2, integrity
    assert any("size and sha256 verified" in m for m in integrity)
    assert any("table count(s) agree with BUNDLE.json" in m for m in integrity)


async def test_no_import_path_turns_a_postgres_error_into_a_five_hundred(db, tmp_path):
    """Asserted through `import_bundle`: no import PATH may turn a PostgresError into a 500."""
    artifacts_root = tmp_path / "artifacts"
    for name, statement in (
        ("orphan",
         "INSERT INTO title_genre (title_id, source, genre) VALUES (4242, 'tmdb', 'noir')"),
        ("null", "UPDATE award SET award = NULL WHERE id = 1"),
        ("renamed", "ALTER TABLE title RENAME COLUMN runtime_min TO runtime_minutes"),
        ("unaccounted", "CREATE TABLE title_sentiment (title_id integer, score real)"),
    ):
        root = tmp_path / f"bundle-{name}"
        fx.make_bundle(root, version=f"test-{name}")
        _sqlite_exec(root, statement)
        fx.reinventory(root)

        report = await bundle_import.import_bundle(
            db, bundle_import.Bundle.open(root), artifacts_root
        )

        assert not report.ok, f"{name}: {report.render()}"
        assert report.failures, f"{name} produced no named failure"
        assert await db.fetchval(
            "SELECT count(*) FROM artifact_bundle WHERE version = $1", f"test-{name}"
        ) == 0
        assert not (artifacts_root / f"test-{name}").exists(), (
            f"{name} left a staged tree no artifact_bundle row names"
        )


async def test_an_import_that_crashes_inside_the_transaction_leaves_no_staged_tree(
    db, tmp_path, pg_url, monkeypatch
):
    """Every exit, `CancelledError` included, owes the disk the
    same cleanup; `except BaseException` is load-bearing."""
    artifacts_root = tmp_path / "artifacts"

    def explode(exc):
        async def _raise(*args, **kwargs):
            raise exc
        return _raise

    root = tmp_path / "bundle-pg"
    fx.make_bundle(root, version="test-pg")
    monkeypatch.setattr(
        bundle_import, "rebuild",
        explode(asyncpg.exceptions.PostgresError("a constraint with no rule")),
    )
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )
    assert not report.ok, report.render()
    stopped = next(f for f in report.failures if f.rule == "import")
    assert "PostgresError" in stopped.message and "a constraint with no rule" in stopped.message
    assert not (artifacts_root / "test-pg").exists(), "a database refusal left a staged tree"
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0

    crash = tmp_path / "bundle-bug"
    fx.make_bundle(crash, version="test-bug")
    monkeypatch.setattr(
        bundle_import, "rebuild", explode(RuntimeError("not a database error"))
    )
    with pytest.raises(RuntimeError):
        await bundle_import.import_bundle(db, bundle_import.Bundle.open(crash), artifacts_root)
    assert not (artifacts_root / "test-bug").exists(), "a crash left a staged tree"
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0

    budget = tmp_path / "bundle-budget"
    fx.make_bundle(budget, version="test-budget")
    at_the_rebuild = asyncio.Event()

    async def stalls(*args, **kwargs):
        at_the_rebuild.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(bundle_import, "rebuild", stalls)
    other = await _second_connection(pg_url)
    abandoned = asyncio.create_task(
        bundle_import.import_bundle(other, bundle_import.Bundle.open(budget), artifacts_root)
    )
    try:
        await asyncio.wait_for(at_the_rebuild.wait(), 60)
        # `task.cancel()` is what `asyncio.wait_for` does at the budget.
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
    finally:
        await other.close()
    assert not (artifacts_root / "test-budget").exists(), (
        "the cancellation the job budget delivers left a staged tree"
    )
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0


async def test_a_staging_failure_is_a_report_line_and_leaves_no_half_copied_tree(
    db, tmp_path, monkeypatch
):
    """Decision 249: a version no row names loses its half copy; a named version keeps its files."""
    artifacts_root = tmp_path / "artifacts"
    real_copytree = bundle_import.shutil.copytree

    def enospc(src, dst, *args, **kwargs):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "backbone.npz").write_bytes(b"the first file of a copy that stopped")
        raise shutil.Error([(str(src), str(dst), "[Errno 28] No space left on device")])

    root = tmp_path / "bundle-full-disk"
    fx.make_bundle(root, version="test-full")
    monkeypatch.setattr(bundle_import.shutil, "copytree", enospc)

    stopped = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )

    assert not stopped.ok, stopped.render()
    staging = [f.message for f in stopped.failures if f.rule == "stage"]
    assert len(staging) == 1 and "No space left on device" in staging[0], stopped.render()
    assert not (artifacts_root / "test-full").exists(), "a failed copy left a half-staged tree"
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0
    # The advisory lock is released on every exit, so the retry is not refused as concurrent.
    monkeypatch.setattr(bundle_import.shutil, "copytree", real_copytree)
    retry = await bundle_import.import_bundle(db, bundle_import.Bundle.open(root), artifacts_root)
    assert retry.ok, retry.render()

    # A version the table names keeps its tree (decision 249), and the operator gets a line.
    fx.make_bundle(tmp_path / "bundle-model", version="test-model")
    model = _models_only(tmp_path / "bundle-model")
    validated = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(model), artifacts_root, activate=False
    )
    assert validated.ok, validated.render()
    assert (artifacts_root / "test-model" / "backbone.npz").is_file()
    monkeypatch.setattr(
        bundle_import.shutil, "rmtree",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "the file is in use")),
    )

    held = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(model), artifacts_root
    )

    assert not held.ok, held.render()
    assert any("the file is in use" in f.message for f in held.failures), held.render()
    assert (artifacts_root / "test-model" / "backbone.npz").is_file(), (
        "a failed replacement removed the files an artifact_bundle row still names"
    )


def _stops_half_way(src, dst, *args, **kwargs):
    """CPython's `copytree` makes the destination first and raises `shutil.Error` at the end."""
    Path(dst).mkdir(parents=True)
    (Path(dst) / "audit.json").write_bytes(b"the first file of a copy that stopped")
    raise shutil.Error([(str(src), str(dst), "[Errno 28] No space left on device")])


async def test_a_failed_restage_leaves_no_half_tree_and_the_repair_stays_open(
    db, tmp_path, monkeypatch
):
    """On a restage the active row always names the version, so a half copy must still be removed."""
    artifacts_root = tmp_path / "artifacts"
    real_copytree = bundle_import.shutil.copytree
    root = tmp_path / "bundle"
    fx.make_bundle(root, version="test-v1")
    await _import(db, bundle_import.Bundle.open(root), artifacts_root)
    # README's "database restored, files missing", which is the state D2 was written for.
    shutil.rmtree(artifacts_root / "test-v1")

    # A copy that never began: the note may not claim files "remain on disk".
    def unwritable(src, dst, *args, **kwargs):
        raise PermissionError(13, "the artifacts directory is not writable")

    monkeypatch.setattr(bundle_import.shutil, "copytree", unwritable)
    nothing = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )

    assert not nothing.ok, nothing.render()
    rollback = [f.message for f in nothing.findings if f.rule == "rollback"]
    assert len(rollback) == 1 and "nothing was staged" in rollback[0], nothing.render()
    assert not (artifacts_root / "test-v1").exists()

    # And a copy that stopped half way.
    monkeypatch.setattr(bundle_import.shutil, "copytree", _stops_half_way)
    stopped = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )

    assert not stopped.ok, stopped.render()
    assert not (artifacts_root / "test-v1").exists(), "a failed restage kept its own half copy"
    assert not [f for f in stopped.failures if "removed to make room" in f.message], (
        "nothing was at that path to remove: a restage is the branch whose directory is gone"
    )
    assert await db.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = 'test-v1'"
    ) == "active", "the restage cleared the active row it exists to repair"

    # The repair is still open, which is the whole point of removing the wreckage.
    monkeypatch.setattr(bundle_import.shutil, "copytree", real_copytree)
    repaired = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )

    assert repaired.ok, repaired.render()
    assert [f for f in repaired.findings if f.rule == "restage"], repaired.render()
    assert (artifacts_root / "test-v1" / "backbone.npz").is_file()


async def test_a_replacement_that_fails_says_the_copy_it_removed_is_gone(
    db, tmp_path, monkeypatch
):
    """The `rmtree` runs first, so a failed replacement must report the copy it removed."""
    artifacts_root = tmp_path / "artifacts"
    real_copytree = bundle_import.shutil.copytree
    fx.make_bundle(tmp_path / "bundle", version="test-v1")
    await _import(db, bundle_import.Bundle.open(tmp_path / "bundle"), artifacts_root)
    fx.make_bundle(tmp_path / "bundle-model", version="test-model")
    model = _models_only(tmp_path / "bundle-model")
    validated = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(model), artifacts_root, activate=False
    )
    assert validated.ok, validated.render()
    before = sorted(p.name for p in (artifacts_root / "test-model").iterdir())
    assert "backbone.npz" in before

    monkeypatch.setattr(bundle_import.shutil, "copytree", _stops_half_way)
    stopped = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(model), artifacts_root
    )

    assert not stopped.ok, stopped.render()
    destroyed = [f.message for f in stopped.failures if "removed to make room" in f.message]
    assert len(destroyed) == 1, stopped.render()
    assert "test-model" in destroyed[0] and "restore that directory" in destroyed[0]
    assert "remain on disk" not in stopped.render(), (
        "the report told the operator the files were still there"
    )
    assert not (artifacts_root / "test-model").exists(), "half a copy was kept as provenance"
    assert await db.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = 'test-model'"
    ) == "validated"

    monkeypatch.setattr(bundle_import.shutil, "copytree", real_copytree)
    retry = await bundle_import.import_bundle(db, bundle_import.Bundle.open(model), artifacts_root)

    assert retry.ok, retry.render()
    assert sorted(p.name for p in (artifacts_root / "test-model").iterdir()) == before


def _sqlite_exec(root: Path, statement: str) -> None:
    db = sqlite3.connect(root / "content.sqlite")
    db.execute(statement)
    db.commit()
    db.close()
