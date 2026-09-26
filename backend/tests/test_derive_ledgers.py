"""The two curated ledgers at their own points (§8 stage 3, §14 risk 5). Every test runs on a title that HAS
a curated row loaded through the importer's loaders, since an applier that does nothing passes vacuously.
Arrival's TMDB capture is filed against title 8 for its real music credit. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from pathlib import Path

import pytest

from spielplan.acquire import rawstore
from spielplan.core.config import settings
from spielplan.derive import ids, ledgers, rebuild
from spielplan.importer import dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

SOURCES = Path(__file__).resolve().parent / "fixtures" / "sources"

# The title both shipped ledgers name; written out so a re-pointed ledger fails loudly.
CORRECTED = 8
ADJUDICATED = 1
NEIGHBOUR = 2

CORRECTED_KEY = "jellyfin:corrected-title"

# `cozy` is an alias and the shipped rename's source; `dna_tag.term` has no FK, so a tag may carry it.
TERM = "mood.cosy"
RETIRED = "cozy"
OTHER = "mood.dread"


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    """Restated rather than imported: an imported fixture is an unused name and couples collection order."""
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """`acquire/rawstore` takes no root argument on purpose."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


async def _titles(conn) -> None:
    """`credit_correction.title_id` has no FK: a verdict
    must outlive a title this install has not acquired."""
    await conn.executemany(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, $3)",
        [(t[0], t[1], t[2]) for t in fx.TITLES],
    )


async def _shipped_ledgers(conn, bundle_dir: Path) -> None:
    """Counts asserted, so a fixture that stopped shipping a ledger cannot turn these green."""
    vocab = bundle_dir / "artifacts" / "dna_vocab" / "v1"
    report = ImportReport()
    await dna.load_vocabulary(conn, vocab, "v1", report)
    await dna.load_adjudications(conn, vocab, "v1", report)
    await dna.load_corrections(conn, bundle_dir / "artifacts" / "corrections_v1.tsv", report)
    assert report.ok, report.render()
    assert await conn.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert await conn.fetchval("SELECT count(*) FROM dna_adjudication") == 2


async def _write_adjudications(conn, tmp_path: Path, rows: str) -> None:
    """Through `load_adjudications`, which DELETEs by version first, so the header cannot be invented."""
    vocab = tmp_path / "ledger"
    vocab.mkdir(exist_ok=True)
    (vocab / "adjudications_v1.tsv").write_text(
        "\t".join(dna.ADJUDICATION_COLUMNS) + "\n" + rows, encoding="utf-8"
    )
    report = ImportReport()
    await dna.load_adjudications(conn, vocab, "v1", report)
    assert report.ok, report.render()


async def _write_corrections(conn, tmp_path: Path, rows: str) -> None:
    path = tmp_path / "corrections_v1.tsv"
    path.write_text("\t".join(dna.CORRECTIONS_COLUMNS) + "\n" + rows, encoding="utf-8")
    report = ImportReport()
    await dna.load_corrections(conn, path, report)
    assert report.ok, report.render()


async def _tag(conn, title_id: int, term: str, *, facet: str = "mood", salience: int = 2,
               provider: str = "", n_sources: int | None = None,
               quote: str = "a verbatim sentence from this title's pack") -> int:
    """Every tag has a quote, and `provider` is `''`: 0018 collapsed NULL into one value."""
    tag_id = await conn.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, n_sources, provider)"
        " VALUES ($1, 'v1', $2, $3, $4, $5, $6) RETURNING id",
        title_id, term, facet, salience, n_sources, provider,
    )
    await conn.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'trakt:comment')",
        tag_id, quote,
    )
    return tag_id


async def _terms(conn, title_id: int) -> list[str]:
    return [row["term"] for row in await conn.fetch(
        "SELECT term FROM dna_tag WHERE title_id = $1 ORDER BY term", title_id)]


async def _music(conn, title_id: int) -> list[tuple]:
    return [tuple(row) for row in await conn.fetch(
        "SELECT p.name, c.source, c.role_class, c.job FROM credit c JOIN person p ON p.id ="
        " c.person_id WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%'"
        " OR lower(c.job) LIKE '%music%') ORDER BY c.id", title_id)]


async def _seed_derivable(conn) -> None:
    """One document for the music credit; `_task` relates `entity_key` to the title."""
    await conn.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ('acquire', $1, $2)",
        CORRECTED_KEY, {"title_id": CORRECTED},
    )
    await rawstore.store(
        conn, source="tmdb", kind="movie_detail",
        url=f"https://tmdb.test/{CORRECTED}/movie_detail",
        content=(SOURCES / "tmdb_movie_detail.json").read_bytes(),
        entity_key=CORRECTED_KEY, content_type="application/json",
    )


async def test_the_shipped_per_title_verdict_drops_the_tag_it_names_and_leaves_the_others(
    db, bundle_dir
):
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _tag(db, ADJUDICATED, TERM)
    await _tag(db, ADJUDICATED, OTHER)

    stats = await ledgers.apply_adjudications(db, ADJUDICATED)

    assert await _terms(db, ADJUDICATED) == [OTHER]
    assert stats["dropped"] == 1


async def test_the_shipped_blanket_rename_repoints_a_retired_term_onto_the_vocabularys(
    db, bundle_dir
):
    """A re-point must move the facet too, or §6.8 draws the tag in the wrong place."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _tag(db, ADJUDICATED, RETIRED, facet="unknown")

    stats = await ledgers.apply_adjudications(db, ADJUDICATED)

    assert [tuple(row) for row in await db.fetch(
        "SELECT term, facet FROM dna_tag WHERE title_id = $1", ADJUDICATED)] == [(TERM, "mood")]
    assert stats["repointed"] == 1


async def test_a_per_title_verdict_beats_the_blanket_rule_for_its_term(db, bundle_dir, tmp_path):
    """"A per-title verdict always beats the blanket rule
    for its term" (29 kept, 92 dropped in the audit)."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, (
        f"title\t{ADJUDICATED}\t{TERM}\tkeep\t\t\t\tthe owner looked at this one\n"
        f"global\t\t{TERM}\tdrop\t\t\t\teverywhere else\n"
    ))
    await _tag(db, ADJUDICATED, TERM)
    await _tag(db, NEIGHBOUR, TERM)

    kept = await ledgers.apply_adjudications(db, ADJUDICATED)
    swept = await ledgers.apply_adjudications(db, NEIGHBOUR)

    assert await _terms(db, ADJUDICATED) == [TERM], (
        "the blanket drop took a term the owner explicitly kept for this title"
    )
    assert await _terms(db, NEIGHBOUR) == []
    assert kept.get("kept") == 1 and "dropped" not in kept
    assert swept["dropped"] == 1


async def test_a_repoint_onto_a_term_the_vocabulary_does_not_know_is_refused_and_counted(
    db, bundle_dir, tmp_path
):
    """"The ledger never invents a tag": a re-point onto an unknown term keeps the old one."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, (
        f"title\t{ADJUDICATED}\t{TERM}\trename\tmood.invented\t\t\tnot in the vocabulary\n"
    ))
    await _tag(db, ADJUDICATED, TERM)

    stats = await ledgers.apply_adjudications(db, ADJUDICATED)

    assert await _terms(db, ADJUDICATED) == [TERM]
    assert stats["repoint_target_unknown"] == 1
    assert "repointed" not in stats


async def test_a_repoint_onto_a_term_the_title_already_carries_merges_and_keeps_the_louder_reading(
    db, bundle_dir, tmp_path
):
    """A plain UPDATE would violate `dna_tag`'s unique key; the shipped per-title DROP is replaced first."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, f"global\t\t{RETIRED}\trename\t{TERM}\t\t\t\n")
    await _tag(db, ADJUDICATED, TERM, salience=1, n_sources=1, quote="the surviving reading")
    await _tag(db, ADJUDICATED, RETIRED, salience=3, n_sources=4, quote="the re-pointed reading")

    stats = await ledgers.apply_adjudications(db, ADJUDICATED)

    rows = [tuple(row) for row in await db.fetch(
        "SELECT term, salience, n_sources FROM dna_tag WHERE title_id = $1", ADJUDICATED)]
    assert rows == [(TERM, 3, 4)], "the merge lost the louder reading or left two rows behind"
    assert sorted(row["quote"] for row in await db.fetch(
        "SELECT e.quote FROM dna_evidence e JOIN dna_tag t ON t.id = e.dna_tag_id"
        " WHERE t.title_id = $1", ADJUDICATED)) == ["the re-pointed reading", "the surviving reading"]
    assert stats["repointed"] == 1


async def test_a_merge_does_not_fold_two_providers_readings_into_one_row(db, bundle_dir, tmp_path):
    """Folding across providers would record an agreement two providers never made."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, f"global\t\t{RETIRED}\trename\t{TERM}\t\t\t\n")
    await _tag(db, ADJUDICATED, TERM, salience=1, provider="alpha")
    await _tag(db, ADJUDICATED, RETIRED, salience=3, provider="beta")

    await ledgers.apply_adjudications(db, ADJUDICATED)

    assert [tuple(row) for row in await db.fetch(
        "SELECT provider, term, salience FROM dna_tag WHERE title_id = $1 ORDER BY provider",
        ADJUDICATED)] == [("alpha", TERM, 1), ("beta", TERM, 3)]


async def test_an_evidence_drop_takes_the_quote_and_then_the_tag_it_was_holding_up(
    db, bundle_dir, tmp_path
):
    """§4.1 rule 1: a tag with no surviving quote has no grounds left."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, (
        f"title\t{ADJUDICATED}\t{TERM}\tDROP_EVIDENCE\t\tmasterpiece\t\taudit-ruled false\n"
    ))
    await _tag(db, ADJUDICATED, TERM, quote="A MASTERPIECE of dream logic, start to finish.")
    await _tag(db, ADJUDICATED, OTHER, quote="a low hum that outlasts the last scene")

    stats = await ledgers.apply_adjudications(db, ADJUDICATED)

    assert await _terms(db, ADJUDICATED) == [OTHER]
    assert stats["evidence_dropped"] == 1 and stats["dropped"] == 1


async def test_the_ledger_the_corpus_ships_is_read_as_the_same_verdicts_the_schema_declares(
    db, bundle_dir, tmp_path
):
    """The corpus writes `DROP`/`REPOINT`, the schema documents `keep|rename|...`: both must act."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, (
        f"title\t{ADJUDICATED}\t{OTHER}\tDROP\t\t\t\tthe corpus spelling\n"
        f"title\t{ADJUDICATED}\t{RETIRED}\tREPOINT\t{TERM}\t\t\tthe corpus spelling\n"
        f"title\t{NEIGHBOUR}\t{OTHER}\tdrop\t\t\t\tthe schema spelling\n"
        f"title\t{NEIGHBOUR}\t{RETIRED}\tmerge\t{TERM}\t\t\tthe schema spelling\n"
    ))
    for title_id in (ADJUDICATED, NEIGHBOUR):
        await _tag(db, title_id, OTHER)
        await _tag(db, title_id, RETIRED)

    corpus = await ledgers.apply_adjudications(db, ADJUDICATED)
    schema = await ledgers.apply_adjudications(db, NEIGHBOUR)

    assert await _terms(db, ADJUDICATED) == [TERM]
    assert await _terms(db, NEIGHBOUR) == [TERM]
    assert corpus["dropped"] == schema["dropped"] == 1
    assert corpus["repointed"] == schema["repointed"] == 1


async def test_applying_the_dna_ledger_twice_leaves_exactly_what_it_left_the_first_time(
    db, bundle_dir, tmp_path
):
    """A merge re-pointing the survivor onto itself would delete it; only a second run shows that."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, (
        f"title\t{ADJUDICATED}\t{OTHER}\tdrop\t\t\t\t\n"
        f"title\t{ADJUDICATED}\t{RETIRED}\trename\t{TERM}\t\t\t\n"
    ))
    await _tag(db, ADJUDICATED, TERM, salience=2)
    await _tag(db, ADJUDICATED, RETIRED, salience=3)
    await _tag(db, ADJUDICATED, OTHER)

    await ledgers.apply_adjudications(db, ADJUDICATED)
    first = [tuple(row) for row in await db.fetch(
        "SELECT term, facet, salience FROM dna_tag WHERE title_id = $1 ORDER BY term", ADJUDICATED)]
    await ledgers.apply_adjudications(db, ADJUDICATED)

    assert first == [(TERM, "mood", 3)]
    assert [tuple(row) for row in await db.fetch(
        "SELECT term, facet, salience FROM dna_tag WHERE title_id = $1 ORDER BY term",
        ADJUDICATED)] == first


async def test_the_shipped_correction_replaces_the_music_credit_the_source_got_wrong(
    db, bundle_dir, raw_root
):
    """The source's own music credit is written from real bytes, then the ledger overrules it."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)

    music = await _music(db, CORRECTED)
    assert len(music) == 1, f"the ledger left more than one music credit standing: {music}"
    name, source, role_class, job = music[0]
    assert (name, source, role_class, job) == (
        "Kunihiko Murai", "correction", "composer", "Original Music Composer"
    )


async def test_the_corrected_person_is_minted_in_this_apps_half_of_the_id_partition(
    db, bundle_dir, raw_root
):
    """Only `derive/ids.upsert_person` mints, above 1e9."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)

    minted = await db.fetchval("SELECT id FROM person WHERE name = 'Kunihiko Murai'")
    assert minted is not None and minted >= ids.APP_ID_MIN


async def test_applying_the_credit_ledger_twice_in_a_row_adds_nothing(db, bundle_dir, raw_root):
    """Without `already_correct` each run deletes and re-inserts an identical credit."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)
    before = [tuple(row) for row in await db.fetch(
        "SELECT id, person_id, job, source FROM credit WHERE title_id = $1 ORDER BY id", CORRECTED)]

    stats = await ledgers.apply_corrections(db, CORRECTED)

    assert [tuple(row) for row in await db.fetch(
        "SELECT id, person_id, job, source FROM credit WHERE title_id = $1 ORDER BY id",
        CORRECTED)] == before, (
        "the second pass rewrote the corrected credit: same values, a new id, and one more of them "
        "every time a derive runs"
    )
    assert stats["already_correct"] == 1


HOUSEHOLD_COMPOSER = "The Household Composer"


async def _household_correction(conn, title_id: int, name: str) -> int:
    """`origin = 'household'`, otherwise the bundle's shape (decision 326)."""
    return await conn.fetchval(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin)"
        " VALUES ($1, 'composer', $2, 'the household read the end credits', '', 'household')"
        " RETURNING id",
        title_id, name,
    )


async def test_a_household_correction_still_wins_after_the_re_import_renumbers_the_bundles(
    db, bundle_dir, raw_root
):
    """A re-import renumbers the bundle's rows above the household's; the household fix must still win."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)
    household = await _household_correction(db, CORRECTED, HOUSEHOLD_COMPOSER)

    await rebuild.derive_title(db, CORRECTED)
    assert [row[0] for row in await _music(db, CORRECTED)] == [HOUSEHOLD_COMPOSER]

    report = ImportReport()
    await dna.load_corrections(db, bundle_dir / "artifacts" / "corrections_v1.tsv", report)
    assert report.ok, report.render()
    ledger = [tuple(row) for row in await db.fetch(
        "SELECT id, origin FROM credit_correction WHERE title_id = $1 ORDER BY id", CORRECTED)]
    assert [origin for _id, origin in ledger] == ["household", "bundle"], (
        f"the re-import did not renumber the bundle row above the household's: {ledger}"
    )
    assert ledger[0][0] == household, "decision 326's scoped DELETE moved the household row"

    await rebuild.derive_title(db, CORRECTED)

    assert [row[0] for row in await _music(db, CORRECTED)] == [HOUSEHOLD_COMPOSER], (
        "a re-import of an unchanged ledger reverted a curated fix that was on the card the day "
        "before, and said nothing"
    )


async def test_a_household_dna_verdict_is_the_one_that_takes_effect_before_and_after_a_re_import(
    db, bundle_dir, tmp_path
):
    """Adjudications are FIRST-effective-wins, so the household's row must sort first."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _write_adjudications(db, tmp_path, f"title\t{ADJUDICATED}\t{TERM}\tdrop\t\t\t\tupstream\n")
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target, quote,"
        " source, note, origin) VALUES ('v1', 'title', $1, $2, 'rename', $3, '', '', '',"
        " 'household')",
        ADJUDICATED, TERM, OTHER,
    )

    async def verdict_of_the_day() -> list[str]:
        await db.execute("DELETE FROM dna_tag WHERE title_id = $1", ADJUDICATED)
        await _tag(db, ADJUDICATED, TERM)
        await ledgers.apply_adjudications(db, ADJUDICATED)
        return await _terms(db, ADJUDICATED)

    assert await verdict_of_the_day() == [OTHER]

    await _write_adjudications(db, tmp_path, f"title\t{ADJUDICATED}\t{TERM}\tdrop\t\t\t\tupstream\n")
    origins = [row["origin"] for row in await db.fetch(
        "SELECT origin FROM dna_adjudication WHERE title_id = $1 ORDER BY id", ADJUDICATED)]
    assert origins == ["household", "bundle"], f"the reload did not renumber the bundle row: {origins}"

    assert await verdict_of_the_day() == [OTHER], (
        "a byte-identical reload of the DNA ledger changed which author's verdict takes effect"
    )


async def test_a_correction_with_no_evidence_is_refused_and_writes_nothing(db, tmp_path):
    """Stored at import so the editor can round-trip it; refused here, where it costs nothing."""
    await _titles(db)
    await _write_corrections(db, tmp_path, f"composer\t{CORRECTED}\tSomebody Asserted\t\t\n")

    stats = await ledgers.apply_corrections(db, CORRECTED)

    assert stats == {"rows": 1, "no_evidence": 1}
    assert await _music(db, CORRECTED) == []


async def test_a_correction_naming_a_title_this_install_does_not_hold_is_counted_and_skipped(
    db, tmp_path
):
    """No FK, by design; the outcome is a count, not an exception that parks the acquisition."""
    absent = 4242
    await _titles(db)
    await _write_corrections(
        db, tmp_path, f"composer\t{absent}\tSomebody Asserted\tan evidence line\t\n"
    )

    stats = await ledgers.apply_corrections(db, absent)

    assert stats == {"rows": 1, "unknown_title": 1}
    assert await db.fetchval("SELECT count(*) FROM credit") == 0


async def test_a_joint_credit_is_added_beside_the_one_the_source_already_carries(
    db, tmp_path, raw_root
):
    """A `composer_add` implemented as replace would lose the name the source got right."""
    await _titles(db)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)
    source_credits = await _music(db, CORRECTED)
    assert len(source_credits) == 1, "the fixture stopped supplying a music credit to join"

    await _write_corrections(
        db, tmp_path, f"composer_add\t{CORRECTED}\tThe Other Composer\tthe album credit\t\n"
    )
    stats = await ledgers.apply_corrections(db, CORRECTED)

    music = await _music(db, CORRECTED)
    assert stats["added"] == 1
    assert len(music) == 2 and music[0] == source_credits[0]
    assert music[1][0] == "The Other Composer" and music[1][1] == "correction"
    assert (await ledgers.apply_corrections(db, CORRECTED))["already_correct"] == 1


@pytest.mark.parametrize("change", ["withdrawn", "repointed", "retracted"])
async def test_a_correction_the_ledger_stops_asserting_takes_its_credit_back_on_the_next_derive(
    db, tmp_path, raw_root, change
):
    """`source = 'correction'` is outside decision 375's
    delete, so the applier must take back what it minted."""
    await _titles(db)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)
    source = await _music(db, CORRECTED)
    assert len(source) == 1, "the fixture stopped supplying the music credit a correction joins"
    await _write_corrections(
        db, tmp_path, f"composer_add\t{CORRECTED}\tThe Other Composer\tthe album credit\t\n"
    )
    await rebuild.derive_title(db, CORRECTED)
    assert sorted(m[0] for m in await _music(db, CORRECTED)) == sorted(
        [source[0][0], "The Other Composer"])

    # A shorter TSV, not an empty one: `load_corrections` ignores a file with no rows.
    if change == "withdrawn":
        await _write_corrections(
            db, tmp_path, f"composer_add\t{NEIGHBOUR}\tSomeone Else\tthe album credit\t\n"
        )
        expected = [source[0][0]]
    elif change == "repointed":
        await _write_corrections(
            db, tmp_path, f"composer_add\t{CORRECTED}\tThe Other Composer Jr\tthe album credit\t\n"
        )
        expected = sorted([source[0][0], "The Other Composer Jr"])
    else:
        await db.execute("UPDATE credit_correction SET evidence = '' WHERE title_id = $1", CORRECTED)
        expected = [source[0][0]]
    report = await rebuild.derive_title(db, CORRECTED)

    assert sorted(m[0] for m in await _music(db, CORRECTED)) == expected
    assert report.corrections.get("withdrawn") == 1, (
        f"the board cannot say a curated credit was taken back: {report.corrections}"
    )
    again = await rebuild.derive_title(db, CORRECTED)
    assert sorted(m[0] for m in await _music(db, CORRECTED)) == expected
    assert "withdrawn" not in again.corrections, "a second derive reclaimed something again"


async def test_a_correction_kind_the_ledger_does_not_declare_is_counted_and_not_guessed_at(
    db, tmp_path, raw_root
):
    """Every branch below `composer_add` writes music
    credits, so a guessed kind would corrupt the composer."""
    await _titles(db)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)
    before = await _music(db, CORRECTED)

    await _write_corrections(
        db, tmp_path, f"director\t{CORRECTED}\tSomebody Else\tan evidence line\t\n"
    )
    stats = await ledgers.apply_corrections(db, CORRECTED)

    assert stats == {"rows": 1, "unknown_kind": 1}
    assert await _music(db, CORRECTED) == before


async def test_a_shipped_correction_survives_a_derive_and_a_second_derive_changes_nothing(
    db, bundle_dir, raw_root
):
    """`rebuild._replace` re-inserts TMDB's composer on the second run, so the correction must re-apply."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)

    await rebuild.derive_title(db, CORRECTED)
    first = [tuple(row) for row in await db.fetch(
        "SELECT person_id, department, job, role_class, source FROM credit WHERE title_id = $1"
        " ORDER BY id", CORRECTED)]
    people = await db.fetchval("SELECT count(*) FROM person")
    assert await _music(db, CORRECTED) == [
        ("Kunihiko Murai", "correction", "composer", "Original Music Composer")]

    second = await rebuild.derive_title(db, CORRECTED)

    assert [tuple(row) for row in await db.fetch(
        "SELECT person_id, department, job, role_class, source FROM credit WHERE title_id = $1"
        " ORDER BY id", CORRECTED)] == first
    assert await db.fetchval("SELECT count(*) FROM person") == people
    assert second.corrections["replaced"] == 1, (
        "the second derive did not re-apply the ledger, which is the derive that reverts it"
    )


async def test_the_two_ledgers_are_applied_at_their_own_points_and_in_that_order(
    db, bundle_dir, raw_root, monkeypatch
):
    """Read AT each call, since the end state cannot see order: the DNA ledger at ingest sees no credits,
    the credit ledger last sees the source's composer."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)

    seen: list[tuple[str, int, list[str]]] = []
    real_adjudications = ledgers.apply_adjudications
    real_corrections = ledgers.apply_corrections

    async def _record(label, conn, title_id) -> None:
        seen.append((
            label,
            await conn.fetchval("SELECT count(*) FROM credit WHERE title_id = $1", title_id),
            [name for name, *_ in await _music(conn, title_id)],
        ))

    async def _adjudications(conn, title_id):
        await _record("adjudications", conn, title_id)
        return await real_adjudications(conn, title_id)

    async def _corrections(conn, title_id):
        await _record("corrections", conn, title_id)
        return await real_corrections(conn, title_id)

    monkeypatch.setattr(ledgers, "apply_adjudications", _adjudications)
    monkeypatch.setattr(ledgers, "apply_corrections", _corrections)

    await rebuild.derive_title(db, CORRECTED)

    assert [label for label, *_ in seen] == ["adjudications", "corrections"]
    assert seen[0][1] == 0, "the DNA ledger ran after this derive's rows were written"
    assert seen[1][1] > 0, "the credit ledger ran before the rows it corrects existed"
    assert seen[1][2] and "Kunihiko Murai" not in seen[1][2], (
        "the credit ledger had already been applied when it was called, so the two calls are one"
    )
    assert [name for name, *_ in await _music(db, CORRECTED)] == ["Kunihiko Murai"]


async def _never_ran(conn, title_id: int) -> dict[str, int]:
    return {}


async def test_a_curated_verdict_reverts_when_the_adjudicator_does_not_run(
    db, bundle_dir, monkeypatch
):
    """§14.5's scar reproduced: with the applier stubbed the tag is simply back, silently."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _tag(db, ADJUDICATED, TERM)

    await rebuild.derive_title(db, ADJUDICATED)
    assert await _terms(db, ADJUDICATED) == [], "the curated verdict was not applied at all"

    await _tag(db, ADJUDICATED, TERM)
    await rebuild.derive_title(db, ADJUDICATED)
    assert await _terms(db, ADJUDICATED) == [], "the verdict was not re-applied over rewritten tags"

    await _tag(db, ADJUDICATED, TERM)
    monkeypatch.setattr(ledgers, "apply_adjudications", _never_ran)
    await rebuild.derive_title(db, ADJUDICATED)

    assert await _terms(db, ADJUDICATED) == [TERM], (
        "the negative control proved nothing: the tag was gone with the applier disabled too"
    )


async def test_the_derive_reports_each_ledgers_counts_apart(db, bundle_dir, raw_root, tmp_path):
    """"3 curated rows applied" cannot say which ledger; one title here carries a row in both."""
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)
    await _write_adjudications(db, tmp_path, f"title\t{CORRECTED}\t{TERM}\tdrop\t\t\t\tboth\n")
    await _tag(db, CORRECTED, TERM)

    report = await rebuild.derive_title(db, CORRECTED)

    assert report.adjudications == {"rules": 1, "dropped": 1}
    assert report.corrections == {"rows": 1, "replaced": 1}
    assert await _terms(db, CORRECTED) == []
    assert [name for name, *_ in await _music(db, CORRECTED)] == ["Kunihiko Murai"]
