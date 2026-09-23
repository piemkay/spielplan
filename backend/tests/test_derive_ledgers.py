"""The two curated ledgers at their own points. Spec v2.1 §8 stage 3, §14 risk 5; decisions 326, 376.

THE SCAR IS INVISIBLE IN A HAPPY-PATH TEST, and that governs every test in this file. A brand-new
acquired title carries no adjudication and no correction, so "the derive applies BOTH ledgers,
corrections last" is satisfied VACUOUSLY by an applier that does nothing at all -- which is precisely
the state §14.5 describes ("787 rows and half the register facet, twice in one afternoon, before the
cause was found"). So every test below runs against a title that HAS a curated row, and the curated
rows arrive through the importer's own loaders from files in the shipped header, rather than being
typed straight into `credit_correction` and `dna_adjudication`: a test that INSERTs its own ledger
row proves the applier reads a table, and what has to be proved is that it reads the ledger the
bundle ships.

THE SHIPPED CORRECTION IS A REAL ONE. `importer/dna.py:767-769` records that "five of the shipped
ledger's six rows are unreflected in the corpus's own `content.sqlite`", so the six are genuinely
unapplied facts rather than a formality, and `make_bundle` ships one of them verbatim
(`composer`, title 8, a name and an evidence link). This file derives the title that row names.

WHOSE BYTES THE DERIVE READS, said plainly because the pairing is odd on sight.
`fixtures/sources/tmdb_movie_detail.json` is the tree's one captured TMDB detail response and it is
Arrival's; the shipped ledger names corpus title 8. What these tests need from the document is a
REAL music credit for the `composer` verdict to overrule -- a synthesised one would only prove that
the applier can delete a row the test itself inserted -- and what they need from the ledger is the
shipped row. So the document is filed against the title the ledger names, and no assertion below
reads the film's name, its year or its card.

ORDER IS THE REQUIREMENT AND THE END STATE CANNOT SEE IT. Swapping the two calls, or merging them
into one pass, leaves the corrected credit exactly where a test asserting only the final rows would
look for it. `test_the_two_ledgers_are_applied_at_their_own_points_and_in_that_order` therefore
observes the two applications as they happen, through the same module attribute `derive/rebuild.py`
calls, and reads the database at each one.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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

# The title the shipped `corrections_v1.tsv` names (`make_bundle.py:721-724`), and the title the
# shipped `adjudications_v1.tsv` scopes its one per-title verdict to (`:873-877`). Written out
# rather than looked up: if either ledger is re-pointed at another row the tests below must fail
# loudly rather than quietly assert nothing, and a lookup would make them assert nothing.
CORRECTED = 8
ADJUDICATED = 1
NEIGHBOUR = 2

CORRECTED_KEY = "jellyfin:corrected-title"

# `mood.cosy` is in the fixture vocabulary (`make_bundle.VOCAB:97`) and `cozy` is deliberately not:
# it is an ALIAS in `alias_map_v1.tsv` and the source term of the shipped blanket rename, which is
# what a retired id looks like after a merge. A tag may carry it -- `dna_tag.term` has no foreign
# key on `dna_term`, because a result file written before a merge still ingests.
TERM = "mood.cosy"
RETIRED = "cozy"
OTHER = "mood.dread"


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    """`test_dna_import.py:53-56`'s fixture, repeated for its own reason rather than imported.

    A pytest fixture reached by importing it from another test module is an unused import to every
    linter and a silent dependency between two files' collection order; three lines is cheaper than
    either. What this file needs from it is the shipped ledgers, and `make_bundle` is the one
    statement of what the corpus ships.
    """
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """A raw store of this test's own, reached the way the worker reaches it.

    `test_derive_rebuild.py:113-125`'s fixture and `test_acquire_rawstore.py:38-52`'s before it,
    verbatim in mechanism and for its reason: `acquire/rawstore` takes no root argument on purpose,
    because a root that can be passed in is a root a caller can pass wrong.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


async def _titles(conn) -> None:
    """The fixture's own title table. `test_dna_import.py:74-79` seeds it the same way.

    §4.1 carries the corpus ids over verbatim, which is why a curated ledger authored upstream can
    name one at all -- and why `credit_correction.title_id` carries no foreign key
    (`0015_seed.sql:157-158`): a verdict must outlive a title this install has not acquired.
    """
    await conn.executemany(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, $3)",
        [(t[0], t[1], t[2]) for t in fx.TITLES],
    )


async def _shipped_ledgers(conn, bundle_dir: Path) -> None:
    """Load the vocabulary and both curated ledgers the way an import does, and refuse a silent zero.

    The counts are asserted here rather than in each test because every test below rests on them:
    a `make_bundle` that stopped shipping a correction would turn eight tests green over an applier
    that never ran, which is the vacuous pass this whole file is written against.
    """
    vocab = bundle_dir / "artifacts" / "dna_vocab" / "v1"
    report = ImportReport()
    await dna.load_vocabulary(conn, vocab, "v1", report)
    await dna.load_adjudications(conn, vocab, "v1", report)
    await dna.load_corrections(conn, bundle_dir / "artifacts" / "corrections_v1.tsv", report)
    assert report.ok, report.render()
    assert await conn.fetchval("SELECT count(*) FROM credit_correction") == 1
    assert await conn.fetchval("SELECT count(*) FROM dna_adjudication") == 2


async def _write_adjudications(conn, tmp_path: Path, rows: str) -> None:
    """Replace the adjudication ledger with `rows`, through the loader that reads the real header.

    ADJUDICATION_COLUMNS is the authority for the header, not this string: the whole point of going
    through `load_adjudications` is that a test cannot invent a column shape the importer does not
    read. `load_adjudications` DELETEs by version before inserting, so this replaces the shipped
    two rather than adding to them.
    """
    vocab = tmp_path / "ledger"
    vocab.mkdir(exist_ok=True)
    (vocab / "adjudications_v1.tsv").write_text(
        "\t".join(dna.ADJUDICATION_COLUMNS) + "\n" + rows, encoding="utf-8"
    )
    report = ImportReport()
    await dna.load_adjudications(conn, vocab, "v1", report)
    assert report.ok, report.render()


async def _write_corrections(conn, tmp_path: Path, rows: str) -> None:
    """Replace the credit ledger with `rows`, through the loader that reads the real header."""
    path = tmp_path / "corrections_v1.tsv"
    path.write_text("\t".join(dna.CORRECTIONS_COLUMNS) + "\n" + rows, encoding="utf-8")
    report = ImportReport()
    await dna.load_corrections(conn, path, report)
    assert report.ok, report.render()


async def _tag(conn, title_id: int, term: str, *, facet: str = "mood", salience: int = 2,
               provider: str = "", n_sources: int | None = None,
               quote: str = "a verbatim sentence from this title's pack") -> int:
    """One extracted tag with the evidence §4.1 rule 1 requires it to carry.

    Every tag here gets a quote, because "a tag without its quote is unfalsifiable" is the rule the
    DROP_EVIDENCE verdict operates on and a tag seeded without one would be dropped by an applier
    doing the right thing, for a reason the test never intended.

    `provider` defaults to `''` and not to None: `0018_read_layer.sql:68-70` collapsed the NULL the
    importer used to write, "so the semantics of 'no provider recorded' stay one value instead of
    two" -- and a fixture writing NULL would be seeding a row this schema cannot hold.
    """
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
    """This title's music credits as the ledger sees them, by `apply_corrections`' own predicate."""
    return [tuple(row) for row in await conn.fetch(
        "SELECT p.name, c.source, c.role_class, c.job FROM credit c JOIN person p ON p.id ="
        " c.person_id WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%'"
        " OR lower(c.job) LIKE '%music%') ORDER BY c.id", title_id)]


async def _seed_derivable(conn) -> None:
    """The title the shipped correction names, with one real TMDB response filed against it.

    One document and not the eight: what every test that derives here needs is the music credit the
    ledger overrules, and the seven others would put this file's assertions at the mercy of a
    scraped page's year check. `_task` is the row that relates a document's `entity_key` to a title
    (`acquire/board.py:37-44`); the payload is a dict rather than a JSON string because the pool
    registers a codec that would encode it twice (`test_derive_rebuild.py:140-145`).
    """
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


# --- F1: the DNA ledger, at ingest ---------------------------------------------------------------


async def test_the_shipped_per_title_verdict_drops_the_tag_it_names_and_leaves_the_others(
    db, bundle_dir
):
    """The ledger's whole job, against the row the bundle actually ships.

    `mdc/dna/adjudication.py`'s header states what a drop is for: "a tag can be quote-verified and
    still be wrong: the quote is a metaphor, or praise, or describes the novel rather than the film
    ... Verification is mechanical and cannot see any of that." So the tag is real, its evidence is
    real, and only the owner's verdict says it should not be there.
    """
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
    """The second shipped verdict, and the facet that has to travel with the term.

    A re-point that moved `term` and left `facet` would put a `mood` row under whatever facet the
    retired id was filed under, and §6.8 colours the map by facet -- so the tag would still be
    there, still quote-verified, and drawn in the wrong place. `_merge_into` takes the facet from
    the vocabulary for this reason, and so does this.
    """
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _tag(db, ADJUDICATED, RETIRED, facet="unknown")

    stats = await ledgers.apply_adjudications(db, ADJUDICATED)

    assert [tuple(row) for row in await db.fetch(
        "SELECT term, facet FROM dna_tag WHERE title_id = $1", ADJUDICATED)] == [(TERM, "mood")]
    assert stats["repointed"] == 1


async def test_a_per_title_verdict_beats_the_blanket_rule_for_its_term(db, bundle_dir, tmp_path):
    """`mdc/dna/adjudication.py:26-28`, which is the sentence the three-phase order exists for:

    "A per-title verdict therefore always beats the blanket rule for its term, which is what lets
    the audit keep 29 `central_couple` rows while dropping 92."

    Two titles and one term: the blanket rule drops it everywhere, the per-title `keep` shadows the
    blanket rule where the owner looked. A sweep that ran the blanket rules first, or that did not
    track which terms the per-title rows named, drops both -- and the 29 rows the audit kept are the
    measurement of what that costs.
    """
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
    """The one place the ledger could invent a tag, and `mdc/dna/adjudication.py:174-177` refuses it.

    "The ledger never invents a tag. Every action removes or re-points evidence that an extractor
    produced and verification accepted; nothing is created here, so a tag still traces to a verbatim
    quote in its own title's pack." A re-point onto a word `dna_term` has never heard of creates a
    term the facet colours, the shelves and §6.8's map have no row for -- so the tag keeps the term
    it had, which is wrong in the way the owner already named and therefore still visible.
    """
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
    """`_merge_into` (`mdc/dna/adjudication.py:103-123`), which is the half a bare UPDATE gets wrong.

    Re-pointing `cozy` onto `mood.cosy` for a title that already carries `mood.cosy` is two rows
    claiming one reading. The corpus keeps the higher salience and unions the evidence, "exactly as
    `apply_db_repair.repoint_tag` did -- a re-pointed row is the same reading of the same film, so
    its quote belongs to the surviving tag". Here the merge is also what keeps the write legal:
    `dna_tag` is UNIQUE (title_id, version, term, provider), so a plain UPDATE would raise.

    The shipped ledger is replaced rather than added to, and the reason is worth stating once for
    the five tests below that do the same: it carries a per-title DROP on `mood.cosy` for this very
    title, so run against it the surviving row would be the RE-POINTED one and every assertion here
    would pass over a merge that never happened.
    """
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
    """Named change 2, and the distinction `dna_tag`'s unique key is built to keep.

    §6.6's parallel mode runs two providers over one title and `UNIQUE (title_id, version, term,
    provider)` is what lets both readings stand. The corpus has no provider column at all, so its
    merge folds any tag carrying the target term; folding across providers here would leave one row
    where two extractions disagreed, with the higher salience and nothing recording that anything
    was merged -- a quiet agreement between two providers that never agreed.
    """
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
    """DROP_EVIDENCE, and §4.1 rule 1 as the reason the tag goes with the quote.

    The corpus's phase 1 drops the named quote and then drops the tag if nothing is left: "a tag
    with no surviving quote has no grounds left". That is rule 1 exactly -- "a tag without its quote
    is unfalsifiable" -- so a tag whose last quote the owner has just ruled false has nothing left to
    trace to. The second tag keeps a quote the verdict does not name and therefore survives.
    """
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
    """Named change 1, and the reason it is not a tidiness note.

    `0004_dna.sql:137` declares "keep | rename | drop | merge"; the ledger the corpus actually ships
    spells its column `action` and fills it with `DROP`, `REPOINT` and `DROP_EVIDENCE`, and
    `importer/dna.py:286` stores that string verbatim. There is no CHECK constraint to make either
    wrong, so an applier that understood one vocabulary would do NOTHING on an install carrying the
    other -- and doing nothing is what §14.5's scar looks like from the outside. The two spellings
    are asserted to produce the same two rows, on two titles, in one run.
    """
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
    """It runs at every derive, so running it twice has to be running it once.

    The mechanism is that every action is stated against the term it names: the second pass finds no
    row carrying the dropped term and none carrying the re-pointed one, so there is nothing to act
    on. Asserted rather than reasoned, because a merge that re-pointed the SURVIVOR onto itself would
    delete the row it had just kept -- and the second run is the only place that shows.
    """
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


# --- F2: the credit ledger, last -----------------------------------------------------------------


async def test_the_shipped_correction_replaces_the_music_credit_the_source_got_wrong(
    db, bundle_dir, raw_root
):
    """`mdc/corrections.py`'s subject, on the row the bundle ships and a credit a real source wrote.

    "These are not parser bugs. The parsers read what the sources say ... No parser change fixes a
    source that is wrong, so the correction is recorded against the title and applied after the
    rebuild that would otherwise overwrite it." The derive here writes the source's own music credit
    from real captured bytes; the ledger then overrules it.
    """
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
    """The correction mints a human, and `derive/ids.upsert_person` is the only door it may use.

    `mdc/corrections.py:71-77` writes its own two-line `_person_id` because the corpus has one id
    space. Here a person minted below 1e9 "silently acquires the credits of whoever held that id in
    the corpus" (`derive/ids.py:220-224`), and `credit.person_id` is a foreign key that will happily
    point at them -- so a correction is the last place to open that door.
    """
    await _titles(db)
    await _shipped_ledgers(db, bundle_dir)
    await _seed_derivable(db)
    await rebuild.derive_title(db, CORRECTED)

    minted = await db.fetchval("SELECT id FROM person WHERE name = 'Kunihiko Murai'")
    assert minted is not None and minted >= ids.APP_ID_MIN


async def test_applying_the_credit_ledger_twice_in_a_row_adds_nothing(db, bundle_dir, raw_root):
    """`already_correct`, which is what lets this run after every single rebuild.

    "Idempotent: a correction already in place is a no-op, so this can run after every rebuild
    without accumulating duplicates." Without it the second pass deletes the corrected credit and
    inserts an identical one -- the same rows to a reader and a new `bigserial` and a new `person`
    lookup every time, which is the accumulation the sentence promises there is none of.
    """
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
    """The row §6.6's credit editor will write at M5.6, written here the way that editor must.

    `origin = 'household'` and everything else the same shape as the bundle's: decision 326 says
    "a household row applies exactly like a bundle row", so the only thing this fixture may differ
    in is the column that says who may replace it.
    """
    return await conn.fetchval(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin)"
        " VALUES ($1, 'composer', $2, 'the household read the end credits', '', 'household')"
        " RETURNING id",
        title_id, name,
    )


async def test_a_household_correction_still_wins_after_the_re_import_renumbers_the_bundles(
    db, bundle_dir, raw_root
):
    """Decision 423: decision 326 kept the household's ROW across a re-import; this keeps its FIX.

    THE MECHANISM IS A SEQUENCE AND NOT A LEDGER CHANGE. `credit_correction.id` is a `bigserial`,
    `importer/dna.load_corrections` DELETEs the bundle's rows and re-INSERTs them on every
    models-only import (decision 247, the only import decision 162 lets recur), and
    `apply_corrections` walks the ledger in id order where a `composer` row DELETEs every music
    credit and inserts one - so the LAST row applied is the one on the card. After one routine
    re-import of a byte-identical TSV, the bundle's row carries a higher id than the household's
    and quietly takes the card back. Nothing reports it: the count is unchanged, the household row
    is still in the table, and both M5.3's exit check 10 and M5.6's check 11 measure only that the
    row survived.

    IT IS ALSO PLAIN IDEMPOTENCE, which is why it is this milestone's defect rather than M5.6's
    question: reloading an unchanged file must not change what the next derive writes. §6.6's
    promise that a fix "survives every future re-derive" is what decides which way the tie breaks.
    [M5.3 review cycle 1, m53-c1-dim326-01; decision 423]
    """
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
    """The same rule on the other ledger, and the clause is the OPPOSITE way round on purpose.

    `apply_adjudications` is FIRST-effective-wins where `apply_corrections` is last-wins: `_rule`
    keys on the term and both the DROP and the REPOINT arms move the tag off it, so a second rule
    naming that term finds no row. Household FIRST is therefore household effective, and a fix that
    sorted both ledgers the same way would entrench exactly the outcome it was filed to prevent.

    THE FLIP IS WHAT THE RE-IMPORT USED TO DO. Before the reload the bundle's row had the lower id
    and its `drop` won; after a byte-identical reload the household's rename won. Both directions
    cannot be right, and §6.6 says which one is. [M5.3 review cycle 1, m53-c1-dim326-01;
    decision 423]
    """
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
    """`mdc/corrections.py:64-66`: "a correction without evidence is an opinion".

    The corpus refuses the row while READING the file; this app stores it and refuses it here, and
    named change 3 in `derive/ledgers.py` argues why: §6.6 has the ledger editor writing
    `corrections_v1.tsv` back out, so a row the import dropped would vanish from the file the
    household edits, and `0015_seed.sql:169-176` added the `evidence` column for that round trip.
    The row therefore has to survive the import and be refused where the refusal costs nothing.
    """
    await _titles(db)
    await _write_corrections(db, tmp_path, f"composer\t{CORRECTED}\tSomebody Asserted\t\t\n")

    stats = await ledgers.apply_corrections(db, CORRECTED)

    assert stats == {"rows": 1, "no_evidence": 1}
    assert await _music(db, CORRECTED) == []


async def test_a_correction_naming_a_title_this_install_does_not_hold_is_counted_and_skipped(
    db, tmp_path
):
    """The ledger is authored upstream against a corpus of nineteen thousand titles; an install
    holds the ones it acquired.

    `0015_seed.sql:157-158` makes this a design property rather than an accident: `title_id` carries
    no foreign key "for the same reason `credit_correction.title_id` does not: a curated verdict is
    authored upstream against a corpus id and must outlive a title this install has not acquired."
    So the row is legal, the title is absent, and the outcome is a count -- not an exception on a
    stage that would then park a perfectly good acquisition.
    """
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
    """`composer_add`, which the ledger's header calls "a joint credit showing only one name".

    It is the kind that must NOT replace: two composers really did score the film and the source
    named one of them. A `composer_add` implemented as a replace loses the name the source got
    right, which is a correction making the row less true than it found it.
    """
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
    """The applier removes what it minted once the ledger no longer asserts it.

    The row `apply_corrections` writes carries `source = 'correction'`, which is precisely the label
    decision 375's scoped delete can never reach - so for one review cycle a correction that was
    WITHDRAWN (a models-only re-import of a shorter TSV: `load_corrections` deletes the bundle's
    rows and re-inserts the file, decision 247), RE-POINTED (an owner fixing a typo in the name)
    or RETRACTED (its evidence cleared, so the applier refuses it as an opinion) left its invented
    credit on the card for ever, beside the source's own composer that `_write` restores. The board
    meanwhile said `{'rows': 0}`. This is the mirror of §14.5's scar: not a curated fix silently
    reverting, but a withdrawn one silently persisting, into §3.1's `p:composer:<name>` feature.
    Asserted through a DERIVE, because that is where a card is rebuilt and where the ledger is read.
    [M5.3 review cycle 2, m53-c2-scar-01]
    """
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

    # WITHDRAWN IS A SHORTER TSV AND NOT AN EMPTY ONE: `load_corrections` leaves the stored ledger
    # alone when the file parses to no rows at all, so the realistic withdrawal is a bundle whose
    # ledger still carries its other corrections and has dropped this one.
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
    """`mdc/corrections.py:60-61` filters the two declared kinds at load; this app filters them here.

    A third kind is a ledger the app has not been taught to apply, and the failure mode of guessing
    is specific and bad: every branch below `composer_add` is a music-credit write, so a `director`
    row applied by falling through would replace the film's composer with its director's name and
    call it a curated fix.
    """
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


# --- F3: two calls, two points -------------------------------------------------------------------


async def test_a_shipped_correction_survives_a_derive_and_a_second_derive_changes_nothing(
    db, bundle_dir, raw_root
):
    """The milestone's exit criterion, checks 1 and 2, as one test.

    "Its `credit` rows are regenerated and the correction is still there afterwards ... Deriving the
    same title a second time changes nothing." The regeneration is the part that makes it a test:
    `rebuild._replace` really does delete and re-insert TMDB's own composer credit on the second
    run, so the correction has to be re-applied over rows that came back -- which is §14.5's scar
    stated as a sequence rather than as a warning.
    """
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
    """The clause the gate row spends its last sentence on, and the one an end-state test cannot see.

    "The two ledgers are applied at their own points rather than merged into one pass"
    (`spec_coverage.toml:3234`). Swapping the calls, running both at the end, or folding them into
    one pass all leave the corrected credit exactly where a final-rows assertion looks for it. So
    this reads the database AT each call, through the same module attribute `derive/rebuild.py`
    resolves at call time:

      * the DNA ledger runs at INGEST -- before this run's credits are written, so it sees none;
      * the credit ledger runs LAST -- after them, and over the source's own composer credit, which
        is the row it exists to overrule.

    A merged pass fails whichever end it is merged at: at the head the correction sees no credits to
    correct, at the tail the adjudication sees rows that moved under it.
    """
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
    """The applier, absent. Named rather than a lambda so the failure below reads as a stub."""
    return {}


async def test_a_curated_verdict_reverts_when_the_adjudicator_does_not_run(
    db, bundle_dir, monkeypatch
):
    """THE NEGATIVE CONTROL. §14.5's scar, reproduced once, because it is invisible otherwise.

    "That put them outside the derivation: `mdc dna ingest` rebuilds the extracted tier from the
    result files every run, so every ingest silently reverted them -- 787 rows and half the register
    facet, twice in one afternoon, before the cause was found."

    The sequence is that afternoon: the owner's verdict is applied, something writes the tier again,
    and the next derive either re-applies the verdict or does not. With the applier stubbed out the
    tag is simply back, with no failure, no count and nothing on the board -- which is why the scar
    took an afternoon to find, and why a test that only asserts the good path cannot tell the two
    derives apart.

    WHAT THE `_tag` CALLS STAND IN FOR IS M5.4'S STAGE 8, and this docstring used to name the
    importer's recurring path as well. It cannot be that: `importer/bundle.py` calls `load_tags`
    only inside the seed-only `if db is not None:` branch, whose `else:` names it among the content
    tiers decision 162 will not let a bundle load twice, and `seed-once` refuses a second content
    bundle. So the extracted tier has exactly two writers on a real install -- the one seed import,
    before any derive, and stage 8 from M5.4 -- and decision 376 is what makes the applier rule
    over those rows whatever wrote them, which is why re-inserting the row by hand is a faithful
    stand-in rather than a shortcut. Held in `test_m53_exit_criterion.py` by
    `test_the_negative_control_does_not_say_a_re_import_puts_the_extracted_tier_back`,
    which re-derives that branch reading rather than trusting this paragraph.
    [decision 162; M5.3 review cycle 1, M53-C1-SCAR-02]
    """
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
    """`DeriveReport` carries two mappings, for the same reason there are two calls.

    §6.6's board shows `acquisition_job.detail` and a line reading "3 curated rows applied" cannot
    tell an operator which ledger applied them, or whether the other one ran at all. §14.5 names
    "two distinct ledgers"; the report keeps them two. This is also the one test where ONE title
    carries a curated row in both ledgers at once, which is the shape the exit criterion runs.
    """
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
