"""Loader mapping tests. Spec v2.1 §4.1 rules 1, 3, 6, and §6.0's platform-score block.

Most of this file needs no database — the mapping is a data structure and can be read. The
per-source keys at the end are the exception and say why: a dropped key component is a
*unique violation*, which only the destination server can raise.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from spielplan.db import library
from spielplan.importer import bundle as bundle_import
from spielplan.importer import load
from tests.fixtures import make_bundle as fx


@pytest.fixture
def content(tmp_path):
    root = fx.make_bundle(tmp_path / "bundle")
    db = sqlite3.connect(f"file:{root / 'content.sqlite'}?mode=ro", uri=True)
    db.text_factory = str
    yield db
    db.close()


def _map(target: str) -> load.TableMap:
    return next(m for m in load.MAPPINGS if m.target == target)


def test_null_pk_components_are_coalesced_to_empty_string(content):
    """§4.1 rule 6: 'coalesce NULLable PK components (title_alias.region etc.) to ''.'"""
    tmap = _map("title_alias")
    rows = list(load._rows(content, tmap))

    assert rows, "fixture must contain alias rows with NULL PK components"
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    for row in rows:
        for col in ("region", "language", "kind"):
            assert row[idx[col]] is not None, f"{col} must never reach a PK as NULL"


def test_non_pk_nulls_are_left_alone(content):
    """Coalescing is targeted, not blanket: `character` may legitimately be NULL."""
    tmap = _map("credit")
    rows = list(load._rows(content, tmap))
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    assert any(r[idx["character"]] is None for r in rows)


def test_platform_rating_is_the_only_display_schema_target():
    """§4.1 rule 3: the display-only schema has exactly one door into it."""
    display_targets = [m.target for m in load.MAPPINGS if m.target.startswith("display.")]
    assert display_targets == ["display.platform_rating"]


def test_dna_tiers_are_not_in_the_generic_loader():
    """§4.1 rule 1: the two tiers are loaded by `importer/dna.py` with two separate statements.
    If either ever appears in the generic MAPPINGS table, merging them becomes a one-line
    change — which is exactly what the rule forbids."""
    targets = {m.target for m in load.MAPPINGS}
    assert "dna_tag" not in targets
    assert "dna_projected" not in targets


def test_no_mapping_names_a_column_the_corpus_does_not_ship():
    """M4.5 inverts what this test used to assert.

    It read: "a column this app expects but the bundle lacks is imported as NULL and reported,
    not fatal" — and it asserted that on `title.tagline`, a column the corpus does not export
    on `title` at all. §4.1's shape note runs the other way: an *unmapped bundle* column is a
    report line because the corpus owns its own names; a mapping naming a column upstream does
    not have is this app asserting a name, and NULLs are the wrong answer to it. The manifest
    is the ground truth, and against it the mapping named `ml_link.title_id`,
    `ml_genome_score.ml_movie_id`, `rating_title_map.source_key` and `watchlist.source`. The
    first two are history since decision 291 declined the genome slice; the loop is over
    `MAPPINGS` as it stands, so the assertion cannot outlive the mappings it was written for.
    """
    shipped = json.loads(
        (Path(__file__).parent / "fixtures" / "real_bundle_shapes.json").read_text(
            encoding="utf-8"
        )
    )["sqlite"]["content.sqlite"]
    for tmap in load.MAPPINGS:
        assert tmap.source in shipped, f"{tmap.source} is not a table the corpus ships"
        missing = sorted(set(tmap.columns.values()) - set(shipped[tmap.source]))
        assert not missing, f"{tmap.source} maps column(s) the bundle lacks: {missing}"


def test_every_mapping_column_is_distinct():
    for tmap in load.MAPPINGS:
        sources = list(tmap.columns.values())
        assert len(sources) == len(set(sources)), f"{tmap.target} maps a source column twice"


def test_load_order_puts_parents_before_children():
    """FK order: person before credit, title before everything that references it."""
    order = [m.target for m in load.MAPPINGS]
    assert order.index("title") == 0
    assert order.index("person") < order.index("credit")
    # `ml_genome_tag` before `ml_genome_score` was asserted here until decision 291 declined the
    # slice; the pair that survives is the one §4.1 rule 4's frozen ids depend on.
    assert order.index("rating_source") < order.index("rating_title_map")


def test_the_three_per_source_tables_carry_the_corpus_key(content):
    """The corpus keys `title_language`, `title_country` and `platform_rating` per source, and
    0015 now keys them the same way (owner decision 2026-09-02, §4.1 "tables mirror the corpus
    export"). A mapping that drops `source` collapses 17,342 / 19,092 / 32,463 duplicate groups
    in the shipped bundle onto one another, so COPY raises a unique violation and the whole seed
    rolls back — the failure this milestone exists to remove."""
    assert _map("title_language").columns["source"] == "source"
    assert _map("title_country").columns["source"] == "source"
    platform = _map("display.platform_rating")
    assert platform.columns["platform"] == "source"
    assert platform.columns["metric"] == "metric"
    assert platform.columns["scale"] == "scale"


def test_the_genome_slice_is_named_as_skipped_rather_than_mapped():
    """Decision 291: `media-graph-spec_v1.1.md:175` fixed the genome as a corpus-side artefact --
    "validation artifact only, never shipped or imported into the app" -- and this mapping had
    reversed that without a note.

    A data-structure test because the reversal was a data structure: three `TableMap`s, no
    argument anywhere, 888,023 `ml_genome_score` rows loaded on the one content seed (decision 162)
    into a block `placement/contract.py` DECLARES zero-imputed and `features._genome` populated
    wherever the rows existed -- a live tower input for the 1,055 cold-masked titles decision 304
    re-measured, which is why decision 311 states the rule and the path as two claims.
    The move is into `SKIPPED_TABLES` rather than out of the file entirely, because §10 owes "counts per
    table" and a table this app declines must still produce a line an operator can read -- the
    lesson `title_meta`'s missing 46,318 rows taught.
    """
    slice_tables = {"ml_genome_tag", "ml_link", "ml_genome_score"}
    assert slice_tables <= set(load.SKIPPED_TABLES)
    assert not slice_tables & {m.source for m in load.MAPPINGS}
    assert not slice_tables & {m.target for m in load.MAPPINGS}
    for table in sorted(slice_tables):
        reason = load.SKIPPED_TABLES[table]
        assert "media-graph-spec_v1.1.md:175" in reason, (
            f"`{table}`'s reason must cite the clause it upholds, not merely decline the table"
        )
        # `decisions 291 and 311` is the narrowed spelling: decision 311 restated what the skip
        # COSTS -- the tables are empty on any install THIS BUILD seeds and not on one seeded
        # before it -- and left decision 291's ruling exactly as it stands. Both spellings name the
        # decision that skipped the table, which is the whole of what this asserts.
        # [decision 311; M4.16 cycle 4, M416-C4-GEN-03]
        assert "decision 291" in reason or "decisions 291 and 311" in reason, (
            f"`{table}`'s reason no longer cites the decision that skipped it: {reason}"
        )


# A corpus figure in the corpus's own idiom: `888,023`, or the ungrouped `888023` a re-cut would
# print. Three-digit decision numbers and `media-graph-spec_v1.1.md:175` are deliberately outside
# it -- those are citations, and the rule is about a MEASUREMENT standing where the report puts
# its own. [decision 184; M4.16 cycle 4, M416-C4-GEN-09]
_COUNTED = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b")


def test_no_skip_reason_states_a_row_count_the_import_did_not_measure():
    """§10 owes "a migration report (counts per table)", and that is `report.table_counts`.

    `load._account_for_shipped_tables` renders each reason verbatim into a `table-skipped` note,
    and `importer/report.py`'s `render()` says in as many words that the string "is what the
    wizard and the Data tab show" -- in the same report whose counts block prints what the bundle
    in front of the operator actually holds. `ml_genome_score`'s reason carried "888,023 relevance
    rows", which is v20260828's corpus figure and not a reading of anything: on the fixture this
    suite imports the two numbers sit in one report five orders of magnitude apart. The other two
    slice reasons name no count, which is the shape this holds them all to.

    The figure is not lost -- it is argued where it belongs, in the `SKIPPED_TABLES` comment above
    the dict and in decisions 291, 304 and 311. `validate.py`'s EXPECTED block states the house
    rule this is the inverse of: a corpus figure is presented "with the observed value next to the
    expected one, so a re-import diff shows drift instead of hiding it". An unpaired one beside
    the real measurement is the drift hidden. [decision 309's rule; M4.16 cycle 4, M416-C4-GEN-09]
    """
    guilty = {
        table: _COUNTED.findall(reason)
        for table, reason in load.SKIPPED_TABLES.items()
        if _COUNTED.search(reason)
    }
    assert not guilty, (
        f"a skip reason states a count no import measured: {guilty}. The operator reads this "
        "string beside `report.table_counts` for the bundle in hand, which is what section 10's "
        "'counts per table' means; argue the corpus figure in the comment above the dict instead."
    )


def test_the_skip_reason_guard_sees_the_corpus_figure_come_back():
    """The reason exactly as it shipped, and the ungrouped spelling a re-cut would produce.

    Held against the citations the reasons legitimately carry, because a rule that also refused
    `media-graph-spec_v1.1.md:175` or `decisions 291 and 311` would have to be narrowed by the
    first person it stopped rather than satisfied. [M4.16 cycle 4, M416-C4-GEN-09]
    """
    shipped = ("888,023 relevance rows for a block no import this build populates; "
               "media-graph-spec_v1.1.md:175, decisions 291 and 311")
    assert _COUNTED.search(shipped), "the guard no longer sees the string this repaired"
    assert _COUNTED.search(shipped.replace("888,023", "888023")), "an ungrouped count escapes"
    for innocent in load.SKIPPED_TABLES.values():
        assert not _COUNTED.search(innocent), innocent
    assert not _COUNTED.search(
        "the genome relevance scores; media-graph-spec_v1.1.md:175 makes the whole slice a "
        "corpus-side validation artifact (decisions 291 and 311)"
    ), "the repaired reason's own citations are not counts"


def test_no_loader_path_joins_titles_on_imdb_id():
    """§4.1: "`imdb_id` ... must never be the join key" -- and until decision 291 this module held
    the one exception to it, `_resolve_ml_links`, joining `ml_link` to `title` on `imdb_id`
    because MovieLens keys its link table by external ids and rule 6 ruled out `tmdb_id`.

    The exception was defensible while the genome was imported; it is not an exception this app
    needs once the slice is declined, and the M4.9 requirement that policed it goes with the code
    (decision 291's cost paragraph). Static, over the source, because what must not come back is
    a *join* rather than a function name: re-adding the resolution under any name would restore
    the one read of a key §4.1 forbids, and the rule then has nothing enforcing it at all.
    """
    assert not hasattr(load, "_resolve_ml_links")
    source = Path(load.__file__).read_text(encoding="utf-8")
    # Qualified reads and bare JOINs, not the mere word: `title.imdb_id` is a mapped column and
    # `"imdb_id": "imdb_id"` must stay. What must not come back is a statement that *resolves*
    # through it -- `t.imdb_id = l.imdb_id`, or any JOIN naming the column at all.
    offenders = [
        line.strip() for line in source.splitlines()
        if "imdb_id" in line and (".imdb_id" in line or "JOIN " in line.upper())
    ]
    assert not offenders, f"the loader resolves through imdb_id again: {offenders}"


def test_title_company_is_mapped_rather_than_skipped():
    """Decision 193, and the half of it that is a data structure rather than a key.

    `title_company` sat in `SKIPPED_TABLES` with the reason "loading it needs a cross-source
    dedupe that does not exist yet", so none of its 47,607 shipped rows landed — 8,594 duplicate
    groups under the app's key, 11,654 rows discarded (decision 195). 0018 section 3 gives the
    table the corpus's own key, which is what made the dedupe unnecessary rather than merely
    overdue, and §4.1's "tables mirror the corpus export" is the reason to load it.

    NOT because the Cold Tower was reading a zero. `features.py:403` does count company rows
    into the thin-title meta block and `'companies'` does sit in `_COUNT_KEYS`, but
    `n_companies_log` is a column of no feature contract this app has loaded, so `build_vector`
    counts the key as unmapped and the number reaches no coordinate. That is decision 194, and
    `test_import_integration.py` measures it against the contract the fixture ships.
    """
    assert "title_company" not in load.SKIPPED_TABLES
    tmap = _map("title_company")
    assert tmap.source == "title_company"
    # `source` (who said so) and `role` (what the company did) are two facts, exactly as on
    # `title_language`; both are PK components under 0018 and both are rule-6 coalesced.
    assert tmap.columns["source"] == "source"
    assert tmap.columns["role"] == "role"
    assert {"source", "role"} <= set(tmap.coalesce_empty)
    # `country` is the company's own nationality and this schema has no column for it. §4.1
    # makes that a report line, so it must not be quietly claimed by the mapping either.
    assert "country" not in tmap.columns.values()


def test_language_role_and_source_are_two_different_facts(content):
    """`role` comes from `is_primary`; `source` says who claimed it. Both survive: 0015's key is
    (title_id, source, language, role), and folding one into the other loses a fact."""
    tmap = _map("title_language")
    assert tmap.columns["role"] == "is_primary"
    assert tmap.columns["source"] == "source"
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    rows = list(load._rows(content, tmap))
    assert rows
    for row in rows:
        assert row[idx["role"]] in ("primary", "")
        assert row[idx["source"]] == "tmdb"


# --- the per-source keys, against a real Postgres ---------------------------------------
#
# Skipped without TEST_DATABASE_URL; see tests/conftest.py. These need a server because the
# defect is a *unique violation*: the mapping is well-formed Python either way, and only the
# destination key can tell the two apart.


def _add_duplicate_per_source_rows(root: Path) -> dict[str, int]:
    """Give the fixture the shape the shipped bundle actually has, and report the row counts.

    The committed fixture ships one source per title, so it cannot fail the way the artifact
    does: 17,342 / 19,092 / 32,463 duplicate groups under the app's pre-0015 keys. The rows
    below are the corpus's own second and third sources for one title, and the metrics the
    corpus records per source (`critic_score` beside `user_score`, and the unscaled
    `popularity` / `dist_10` it also keeps).
    """
    db = sqlite3.connect(root / "content.sqlite")
    language = db.execute("SELECT language FROM title_language WHERE title_id = 1").fetchone()[0]
    country = db.execute("SELECT country FROM title_country WHERE title_id = 1").fetchone()[0]
    with db:
        db.executemany(
            "INSERT INTO title_language (title_id, source, language, is_primary) VALUES (?,?,?,?)",
            [(1, "omdb", language, 1), (1, "wikidata", language, 0), (1, "trakt", language, 1)],
        )
        db.executemany(
            "INSERT INTO title_country (title_id, source, country) VALUES (?,?,?)",
            [(1, "omdb", country), (1, "wikidata", country)],
        )
        db.executemany(
            "INSERT INTO platform_rating (title_id, source, metric, value, scale, votes)"
            " VALUES (?,?,?,?,?,?)",
            [(1, "imdb", "critic_score", 74.0, 100.0, None),
             (1, "tmdb", "popularity", 58.1371, None, None),
             (1, "trakt", "dist_10", 24479.0, None, None)],
        )
    counts = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("title_language", "title_country", "platform_rating")
    }
    db.close()
    # BUNDLE.json is the corpus's inventory of the tree and M4.14 reads it before the first row
    # is written, so a helper that adds rows to `content.sqlite` after `make_bundle` returned has
    # to say so: otherwise the bundle these tests import is one whose manifest no longer matches,
    # and the refusal they meet is a sha256 mismatch rather than the mapping rule under test.
    # [M4.14 step B1]
    fx.reinventory(root)
    return counts


async def test_every_per_source_row_survives_the_import(db, tmp_path):
    """§4.1 "tables mirror the corpus export": every shipped row lands, none collides.

    Before 0015 the three targets were keyed without `source`, and this import died on
    `UniqueViolationError` inside the one transaction that carries the whole seed.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    shipped = _add_duplicate_per_source_rows(root)
    bundle = bundle_import.Bundle.open(root)

    report = await bundle_import.import_bundle(db, bundle, tmp_path / "artifacts")
    assert report.ok, report.render()

    assert await db.fetchval("SELECT count(*) FROM title_language") == shipped["title_language"]
    assert await db.fetchval("SELECT count(*) FROM title_country") == shipped["title_country"]
    assert (
        await db.fetchval("SELECT count(*) FROM display.platform_rating")
        == shipped["platform_rating"]
    )

    # ...and the surviving rows are distinguishable by the component that was being dropped.
    langs = await db.fetch(
        "SELECT source, role FROM title_language WHERE title_id = 1 ORDER BY source"
    )
    assert [(r["source"], r["role"]) for r in langs] == [
        ("omdb", "primary"), ("tmdb", "primary"), ("trakt", "primary"), ("wikidata", ""),
    ]
    countries = await db.fetch(
        "SELECT source FROM title_country WHERE title_id = 1 ORDER BY source"
    )
    assert [r["source"] for r in countries] == ["omdb", "tmdb", "wikidata"]
    ratings = await db.fetch(
        "SELECT platform, metric, scale FROM display.platform_rating "
        "WHERE title_id = 1 ORDER BY platform, metric"
    )
    assert [(r["platform"], r["metric"]) for r in ratings] == [
        ("imdb", "critic_score"), ("imdb", "user_score"), ("metacritic", "critic_score"),
        ("tmdb", "popularity"), ("trakt", "dist_10"),
    ]


async def test_the_card_shows_one_number_per_platform_and_metric(db, tmp_path):
    """§6.0's card, now that the row is per (platform, metric).

    Two rules decide what it shows. §4.1 rule 3 keeps the block display-only, so nothing here
    is a filter over a model input — it is a rendering choice. §6.0 wants the caption with the
    number, and the corpus's unscaled metrics (`popularity`, `dist_1`..`dist_10`,
    `critic_review_count`, `audience_rating_count` — 12 of one title's 22 shipped rows) have no
    caption they could honestly print: they are not scores on a stated range. So the card reads
    the scored metrics, each carrying its own `metric` and `scale`.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    _add_duplicate_per_source_rows(root)
    await bundle_import.import_bundle(db, bundle_import.Bundle.open(root), tmp_path / "artifacts")

    items = await library.platform_ratings(db, 1)
    assert [(i["platform"], i["metric"], i["scale"]) for i in items] == [
        ("imdb", "critic_score", 100.0), ("imdb", "user_score", 10.0),
        ("metacritic", "critic_score", 100.0),
    ]
    assert all(i["score"] is not None for i in items)


def test_the_title_row_carries_the_language_the_tower_was_trained_on(content):
    """The seam between the loader and §4.2's meta block. The corpus builds the tower's one
    `lang:` column from `title.original_language` and from nothing else — `title_language` is a
    different, multi-source fact averaging 2.98 entries a title. The loader mapped every column
    of `title` except this one, so on a real seed it stays NULL, and the meta block's `lang:`
    production never fires against a bundle: the tower is fed a vector missing a column it was
    trained with, silently, on every title.
    """
    assert _map("title").columns["original_language"] == "original_language"

    tmap = _map("title")
    idx = {c: i for i, c in enumerate(tmap.pg_columns)}
    rows = list(load._rows(content, tmap))
    assert rows, "the fixture must ship a spine"
    languages = {row[idx["original_language"]] for row in rows}
    assert languages - {None}, "the fixture ships no original_language to carry"
