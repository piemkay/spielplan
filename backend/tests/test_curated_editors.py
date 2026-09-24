"""§6.6 Data's three ledger editors: three artifacts, household rows only, applied at once.

Spec v2.1 §6.6 Data as v2.1.3 amends it ("Three separate editors with separate semantics, never one
merged screen ... they write household rows beside the bundle's, which are read-only in the app, and
export the household's rows on demand in the importer's own columns"), §8 stage 3, §14.5, §6.4;
decisions 326, 342, 423 and 445. Plan §7 checks 10 and 11.

WHAT THE SCAR LOOKS LIKE FROM HERE. Decision 171's Cost paragraph recorded it before any editor
existed: "an in-app-authored correction absent from the next bundle's TSV is wiped (probed: P4
removed the app row)". Decision 326 scoped the importer's DELETEs and decision 423 ordered the
appliers so the household's row wins; what neither could test is the row an editor actually writes,
because there was no editor. So the survival test below authors through the editor, re-imports
through the importer's own models-only loader, and reads the card through the applier the derive
calls -- three real functions and no row typed into a table by the test.

THE EXPORTS ARE READ BACK BY THE IMPORTER'S OWN READERS AND BY NOTHING WRITTEN HERE. A round trip
checked by a parser in this file would prove the writer and the test agree, which is a weaker claim
than §6.6's "the TSV formats the corpus project already uses": `validate._read_tsv`,
`dna.parse_corrections`, `dna.load_adjudications` and `dna.load_axes` are what a bundle is read
with, so they are what an export has to satisfy. Each round trip carries a tab, a newline and a
quote character inside a field, because those are the three bytes a naive join-on-TAB writer breaks
and a hand-typed note is exactly where they arrive.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import csv
import io
import re
from pathlib import Path

import pytest

from spielplan.curated import Refused, adjudications, axes, corrections
from spielplan.derive import ids, ledgers, rebuild
from spielplan.importer import dna
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

CURATED = Path(__file__).resolve().parents[1] / "spielplan" / "curated"

# The title the shipped `corrections_v1.tsv` names (`make_bundle.py:721-724`); every other title
# here is one the bundle's credit ledger does not mention.
CORRECTED = 8
BUNDLE_COMPOSER = "Kunihiko Murai"
HOUSEHOLD_COMPOSER = "The Household Composer"

# A facet `make_bundle.VOCAB` declares and `make_bundle.AXES` ships no axis for: the household's
# axis lands beside the bundle's three rather than over one of them (decision 445: a bundle row is
# read-only in the app).
UNSHIPPED_FACET = "structure"

# The three bytes a writer that joins on TAB and ends lines on LF corrupts.
AWKWARD = 'a line\twith a tab\nand a second, "quoted" line'


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    """`test_dna_import.py`'s fixture, restated rather than imported (a fixture reached by import
    is an unused name to ruff and a collection-order dependency between two files)."""
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


@pytest.fixture
def vocab_dir(bundle_dir) -> Path:
    return bundle_dir / "artifacts" / "dna_vocab" / "v1"


async def _install(conn, bundle_dir: Path) -> None:
    """The titles, the vocabulary and the bundle's three curated ledgers, the way an import loads
    them, with the counts asserted so that a fixture that stopped shipping one cannot turn a test
    below green over an editor with nothing beside it."""
    await conn.executemany(
        "INSERT INTO title (id, kind, name, year) VALUES ($1, $2, $3, $4)",
        [(t[0], t[1], t[2], t[4]) for t in fx.TITLES],
    )
    vocab = bundle_dir / "artifacts" / "dna_vocab" / "v1"
    report = ImportReport()
    await dna.load_vocabulary(conn, vocab, "v1", report)
    await dna.load_corrections(conn, bundle_dir / "artifacts" / "corrections_v1.tsv", report)
    assert report.ok, report.render()
    assert await conn.fetchval("SELECT count(*) FROM dna_adjudication WHERE origin = 'bundle'") == 2
    assert await conn.fetchval("SELECT count(*) FROM credit_correction WHERE origin = 'bundle'") == 1
    assert await conn.fetchval("SELECT count(*) FROM dna_axis WHERE origin = 'bundle'") == len(fx.AXES)


async def _tag(conn, title_id: int, term: str, facet: str) -> None:
    """One extracted tag with the quote §4.1 rule 1 makes it carry."""
    tag_id = await conn.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
        " VALUES ($1, 'v1', $2, $3, 2, '') RETURNING id",
        title_id, term, facet,
    )
    await conn.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'a sentence', 'trakt:comment')",
        tag_id,
    )


async def _terms(conn, title_id: int) -> list[str]:
    return [r["term"] for r in await conn.fetch(
        "SELECT term FROM dna_tag WHERE title_id = $1 ORDER BY term", title_id)]


async def _music(conn, title_id: int) -> list[tuple[str, str]]:
    """This title's music credits by `apply_corrections`' own predicate, as (name, source)."""
    return [(r["name"], r["source"]) for r in await conn.fetch(
        "SELECT p.name, c.source FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')"
        " ORDER BY c.id", title_id)]


async def _source_credit(conn, title_id: int, name: str) -> None:
    """A music credit a source wrote, which is what a correction overrules or stands beside."""
    person_id = await ids.upsert_person(conn, name=name)
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, role_class, source)"
        " VALUES ($1, $2, 'Sound', 'Original Music Composer', 'composer', 'tmdb')",
        title_id, person_id,
    )


async def _ledgers(conn) -> dict[str, list[tuple]]:
    """Every row of the four curated tables, ids included, so "left alone" means byte-for-byte."""
    return {
        "dna_adjudication": [tuple(r) for r in await conn.fetch(
            "SELECT id, origin, scope, title_id, term, verdict, target, quote, source, note"
            " FROM dna_adjudication ORDER BY id")],
        "credit_correction": [tuple(r) for r in await conn.fetch(
            "SELECT id, origin, title_id, field, new_value, evidence, note FROM credit_correction"
            " ORDER BY id")],
        "dna_axis": [tuple(r) for r in await conn.fetch(
            "SELECT facet, origin, left_pole, right_pole FROM dna_axis ORDER BY facet")],
        "dna_axis_weight": [tuple(r) for r in await conn.fetch(
            "SELECT facet, term, weight FROM dna_axis_weight ORDER BY facet, term")],
    }


async def _axis(conn, facet: str) -> tuple:
    """One stored axis as (origin, left, right, ((term, weight), ...))."""
    head = await conn.fetchrow(
        "SELECT origin, left_pole, right_pole FROM dna_axis WHERE version = 'v1' AND facet = $1", facet
    )
    terms = await conn.fetch(
        "SELECT term, weight FROM dna_axis_weight WHERE version = 'v1' AND facet = $1 ORDER BY term",
        facet,
    )
    return (*tuple(head), tuple(tuple(r) for r in terms)) if head else ()


# The verb and the table it writes, read out of the string literals a module hands the driver. `DO
# UPDATE` is an upsert's second half and `FOR UPDATE` a row lock; neither names a table it writes.
_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|DELETE\s+FROM|(?<!DO )(?<!FOR )UPDATE)\s+(\w+)", re.IGNORECASE
)


def _sql_of(source: str) -> str:
    """A module's string literals, docstrings excluded: the SQL it can send, and none of its prose
    (a docstring saying "update the row" is not a write to a table named `the`)."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return "\n".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    )


def _saved(tmp_path: Path, name: str, text: str) -> Path:
    """The export as a household would save it. Bytes, not `write_text`: on Windows text mode
    turns the LF inside a quoted field into CRLF, which is this test's platform and not the
    writer's defect."""
    folder = tmp_path / "export"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(text.encode("utf-8"))
    return path


# --- plan check 10: three editors, three artifacts, household rows only -------------------------


async def test_each_editor_writes_only_its_own_table_and_only_as_the_household(db, bundle_dir):
    """Decision 445: "The editors write only rows with `origin = 'household'`. A bundle row is
    read-only in the app", and "Three modules write three tables". Each write below is checked
    against all four curated tables, so an editor that also touched a sibling's table -- the merged
    write path §6.6 forbids -- or rewrote a bundle row in its own is caught at the write that did it.
    """
    await _install(db, bundle_dir)
    start = await _ledgers(db)

    await adjudications.author(db, scope="title", title_id=2, term="mood.dread", action="DROP")
    after_verdict = await _ledgers(db)
    added = [r for r in after_verdict["dna_adjudication"] if r not in start["dna_adjudication"]]
    assert [r[1] for r in added] == ["household"]
    assert {k: v for k, v in after_verdict.items() if k != "dna_adjudication"} == {
        k: v for k, v in start.items() if k != "dna_adjudication"
    }

    await corrections.author(
        db, title_id=1, kind="composer", value="Elliot Goldenthal", evidence="the end credits"
    )
    after_fact = await _ledgers(db)
    added = [r for r in after_fact["credit_correction"] if r not in after_verdict["credit_correction"]]
    assert [r[1] for r in added] == ["household"]
    assert {k: v for k, v in after_fact.items() if k != "credit_correction"} == {
        k: v for k, v in after_verdict.items() if k != "credit_correction"
    }

    await axes.author(
        db, facet=UNSHIPPED_FACET, left_pole="linear", right_pole="fractured",
        weights=[("structure.procedural", -0.5)],
    )
    after_axis = await _ledgers(db)
    assert [r for r in after_axis["dna_axis"] if r not in after_fact["dna_axis"]] == [
        (UNSHIPPED_FACET, "household", "linear", "fractured")
    ]
    assert [r for r in after_axis["dna_axis_weight"] if r not in after_fact["dna_axis_weight"]] == [
        (UNSHIPPED_FACET, "structure.procedural", -0.5)
    ]
    assert after_axis["dna_adjudication"] == after_fact["dna_adjudication"]
    assert after_axis["credit_correction"] == after_fact["credit_correction"]

    # And every bundle row that stood at the start stands at the end, unchanged and unrenumbered.
    for table in ("dna_adjudication", "credit_correction"):
        assert [r for r in after_axis[table] if r[1] == "bundle"] == [
            r for r in start[table] if r[1] == "bundle"
        ]


def test_three_modules_write_three_tables_and_share_no_write_function():
    """Plan check 10: "three artifacts; `grep` finds no shared write function".

    Read out of the three module sources, because the property is about where the writes are
    SPELLED: a shared helper that took a table name would pass every behavioural test above while
    being the merged write path §6.6 rules out ("never one merged screen"), and proposal 105's
    "separate semantics" is §14.5's two-ledger scar one layer up -- DNA verdicts and credit facts
    apply at different points of the derive, and one writer for both is how they come to be
    treated as one thing. The appliers the editors CALL are `derive/ledgers.py`'s and write
    `dna_tag` and `credit`; they are the derive's, not the editors', and they are not read here.
    """
    own = {
        "adjudications": {"dna_adjudication"},
        "corrections": {"credit_correction"},
        "axes": {"dna_axis", "dna_axis_weight"},
    }
    for module, tables in own.items():
        source = (CURATED / f"{module}.py").read_text(encoding="utf-8")
        written = {m.group(1).lower() for m in _WRITE.finditer(_sql_of(source))}
        assert written == tables, f"{module}.py writes {sorted(written)}, not {sorted(tables)}"

        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("spielplan.curated."), (
                    f"{module}.py imports {node.module}: one editor reaching another's writer"
                )
                if node.module == "spielplan.curated":
                    assert {alias.name for alias in node.names} <= {"Refused"}, (
                        f"{module}.py imports a sibling through the package"
                    )
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("spielplan.curated") for a in node.names)

    package = (CURATED / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(package)
    assert not _WRITE.search(_sql_of(package)), "the package holds a write every editor could reach"
    assert [n.name for n in tree.body if isinstance(n, ast.ClassDef)] == ["Refused"]
    assert not [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]


# --- the exports, read back by the importer's own readers --------------------------------------


async def test_the_verdict_export_folds_back_through_the_importers_reader_and_loader(
    db, bundle_dir, tmp_path
):
    """Decision 445: "Verdicts are written in the corpus's own spelling (DROP / REPOINT /
    DROP_EVIDENCE), so the export folds back unchanged", and "Export is household rows only, in
    the importer's column set". The loader is the last step and not the parser alone, because the
    loader is what a bundle carrying this file back would run: it has to land the same rows.
    """
    await _install(db, bundle_dir)
    await adjudications.author(
        db, scope="title", title_id=2, term="mood.dread", action="DROP_EVIDENCE",
        quote=AWKWARD, source="trakt:comment", note="about the novel\tnot the film",
    )
    await adjudications.author(
        db, scope="global", term='mood.it"s cosy', action="REPOINT", target="mood.cosy",
        note="a retired id",
    )
    await adjudications.author(db, scope="title", title_id=3, term="themes.obsession", action="DROP")
    household = [dict(r) for r in await db.fetch(
        "SELECT scope, title_id, term, verdict, target, quote, source, note FROM dna_adjudication"
        " WHERE origin = 'household' ORDER BY id")]

    name, text = await adjudications.export(db)

    assert name == "adjudications_v1.tsv"
    assert next(csv.reader(io.StringIO(text), delimiter="\t")) == list(dna.ADJUDICATION_COLUMNS)
    path = _saved(tmp_path, name, text)
    report = ImportReport()
    parsed = validator._read_tsv(path, report, "adjudications", dna.ADJUDICATION_COLUMNS)
    assert report.ok, report.render()
    assert parsed == [
        {
            "scope": r["scope"], "title_id": "" if r["title_id"] is None else str(r["title_id"]),
            "term": r["term"], "action": r["verdict"], "target": r["target"] or "",
            "quote": r["quote"] or "", "source": r["source"] or "", "note": r["note"] or "",
        }
        for r in household
    ]
    assert [r["action"] for r in parsed] == ["DROP_EVIDENCE", "REPOINT", "DROP"]

    await dna.load_adjudications(db, path.parent, "v1", report)

    assert report.ok, report.render()
    folded = [dict(r) for r in await db.fetch(
        "SELECT scope, title_id, term, verdict, target, quote, source, note FROM dna_adjudication"
        " WHERE origin = 'bundle' ORDER BY id")]
    assert folded == household, "the loader landed different rows from the ones the editor wrote"


async def test_the_correction_export_folds_back_through_parse_corrections(db, bundle_dir, tmp_path):
    """The credit ledger's round trip, through the parser `load_corrections` uses and in the
    bundle's own file name (`importer/bundle.py` reads `corrections_v1.tsv`). The bundle's row is
    absent from the export: decision 445 exports the household's rows only."""
    await _install(db, bundle_dir)
    await corrections.author(
        db, title_id=1, kind="composer", value="Elliot Goldenthal",
        evidence="https://example.invalid/heat\tend credits", note=AWKWARD,
    )
    await corrections.author(
        db, title_id=2, kind="composer_add", value='Johann "J" Johannsson', evidence="end credits",
    )
    household = [dna.Correction(*r) for r in await db.fetch(
        "SELECT title_id, field, new_value, evidence, note FROM credit_correction"
        " WHERE origin = 'household' ORDER BY id")]

    name, text = await corrections.export(db)

    assert name == "corrections_v1.tsv"
    assert next(csv.reader(io.StringIO(text), delimiter="\t")) == list(dna.CORRECTIONS_COLUMNS)
    report = ImportReport()
    parsed = dna.parse_corrections(_saved(tmp_path, name, text), report)
    assert report.ok, report.render()
    assert parsed == household
    assert BUNDLE_COMPOSER not in text


async def test_the_axis_export_is_the_file_load_axes_reads(db, bundle_dir, tmp_path):
    """Decision 342: the editor "exports `<facet>.tsv` in §6.4's format: the two poles alone on the
    header line, then one term-TAB-weight row per line". The household's axis is withdrawn before
    the file is loaded, because `load_axes` now leaves a household axis in place (the survival
    test below) and would otherwise decline the very file this is checking."""
    await _install(db, bundle_dir)
    await axes.author(
        db, facet=UNSHIPPED_FACET, left_pole='linear, "straight"', right_pole="fractured",
        weights=[("structure.procedural", -0.75), ('mood.it"s', 0.3), ("pacing.patient", 1.0)],
    )
    stored = await _axis(db, UNSHIPPED_FACET)

    name, text = await axes.export(db, UNSHIPPED_FACET)

    assert name == f"{UNSHIPPED_FACET}.tsv"
    lines = list(csv.reader(io.StringIO(text), delimiter="\t"))
    assert lines[0] == ['linear, "straight"', "fractured"], "the header line is the two poles alone"
    assert all(len(line) == 2 for line in lines[1:])
    path = _saved(tmp_path, name, text)
    await axes.withdraw(db, UNSHIPPED_FACET)
    report = ImportReport()

    await dna.load_axes(db, path.parent, "v1", report)

    assert report.ok, report.render()
    assert await _axis(db, UNSHIPPED_FACET) == ("bundle", *stored[1:])


# --- plan check 11 and decision 423: what survives a re-import --------------------------------


async def test_a_household_correction_survives_a_models_only_re_import_and_still_takes_effect(
    db, bundle_dir
):
    """Plan check 11 -- "app-authored correction, then a models-only re-import | the row survives
    (decision 326)" -- and the half decision 423 adds, that it is still the fix on the card.

    Title 8 is the title the bundle's own ledger corrects, so the household's row stands beside a
    bundle row naming the same credit: the population decision 423 says the credit editor exists
    for. `load_corrections` of the shipped TSV is decision 247's models-only path; the TSV does not
    carry the household's row, and before decision 326 that absence was the wipe decision 171
    probed. The editor applies at once (decision 445), so the card is right before the re-import;
    after it, the applier the derive calls is what has to keep it right.
    """
    await _install(db, bundle_dir)

    written = await corrections.author(
        db, title_id=CORRECTED, kind="composer", value=HOUSEHOLD_COMPOSER,
        evidence="the household read the end credits",
    )
    elsewhere = await corrections.author(
        db, title_id=1, kind="composer", value="Elliot Goldenthal", evidence="the end credits",
    )

    assert await _music(db, CORRECTED) == [(HOUSEHOLD_COMPOSER, ledgers.CORRECTION_SOURCE)], (
        "a fix typed in the editor is not on the card until something re-derives the title"
    )

    report = ImportReport()
    await dna.load_corrections(db, bundle_dir / "artifacts" / "corrections_v1.tsv", report)
    assert report.ok, report.render()

    ledger = [tuple(r) for r in await db.fetch(
        "SELECT id, origin FROM credit_correction WHERE title_id = $1 ORDER BY id", CORRECTED)]
    assert ledger[0] == (written["row"]["id"], "household"), f"the re-import took the row: {ledger}"
    assert [origin for _id, origin in ledger] == ["household", "bundle"]
    assert await db.fetchval(
        "SELECT origin FROM credit_correction WHERE id = $1", elsewhere["row"]["id"]
    ) == "household"

    await ledgers.apply_corrections(db, CORRECTED)

    assert await _music(db, CORRECTED) == [(HOUSEHOLD_COMPOSER, ledgers.CORRECTION_SOURCE)], (
        "the re-import renumbered the bundle's row above the household's and took the card back"
    )


async def test_a_title_correction_is_true_at_once_and_withdrawing_it_takes_its_credit_back(
    db, bundle_dir
):
    """Decision 445: "A title-scoped verdict or correction is applied to its title at once" and
    "Withdrawing a correction re-applies, and the applier reclaims the credit it minted".

    `composer_add` first, because it leaves the source's own credit standing: the withdrawal then
    has to take back exactly the one row the applier minted and nothing the source wrote. `composer`
    second, which replaces the source's credit -- and whose withdrawal restores nothing, on a bundle
    title for good (the test below).
    """
    await _install(db, bundle_dir)
    await _source_credit(db, 1, "A Source Composer")

    added = await corrections.author(
        db, title_id=1, kind="composer_add", value="The Added Composer", evidence="end credits",
    )

    assert await _music(db, 1) == [
        ("A Source Composer", "tmdb"), ("The Added Composer", ledgers.CORRECTION_SOURCE)
    ]
    assert added["applied"]["added"] == 1

    gone = await corrections.withdraw(db, added["row"]["id"])

    assert await _music(db, 1) == [("A Source Composer", "tmdb")]
    assert gone["applied"]["withdrawn"] == 1
    assert await db.fetchval(
        "SELECT count(*) FROM credit_correction WHERE id = $1", added["row"]["id"]
    ) == 0

    await corrections.author(
        db, title_id=1, kind="composer", value="The Replacement", evidence="end credits",
    )

    assert await _music(db, 1) == [("The Replacement", ledgers.CORRECTION_SOURCE)]


async def test_withdrawing_a_composer_correction_on_a_bundle_title_brings_no_credit_back(db, bundle_dir):
    """What the correction editor tells the household before it saves a `composer` row and again
    before it withdraws one (`ledgerEditors.svelte.js`'s `COMPOSER_WARNING`), held to the server: the
    applier replaces the title's music credits, the withdrawal reclaims only the one it minted, and a
    bundle title - no raw store (decision 162), never re-derived (decision 445) - gets nothing back
    from a derive either. `withdraw`'s docstring once promised the next derive would restore it.
    [M5.6 review cycle 1, m56-curated-01]"""
    await _install(db, bundle_dir)
    await _source_credit(db, 1, "The Bundle's Composer")

    mistake = await corrections.author(
        db, title_id=1, kind="composer", value="A Mistake", evidence="a misread poster",
    )
    assert await _music(db, 1) == [("A Mistake", ledgers.CORRECTION_SOURCE)]
    await corrections.withdraw(db, mistake["row"]["id"])
    await rebuild.derive_title(db, 1)

    assert await _music(db, 1) == []


async def test_a_global_drop_is_true_at_once_on_every_title_that_carries_the_term(db, bundle_dir):
    """Decision 445: "A blanket verdict is applied at once to every title that carries the term at
    the active version. Bundle titles are never re-derived, so a fix typed today is true today".

    And withdrawal "stops it applying and restores nothing it dropped (§8 stage 7)": the dropped
    tag stays dropped, and a fresh extraction of the term is no longer ruled on.
    """
    await _install(db, bundle_dir)
    await _tag(db, 2, "mood.dread", "mood")
    await _tag(db, 2, "themes.obsession", "themes")
    await _tag(db, 3, "mood.dread", "mood")
    await _tag(db, 4, "themes.obsession", "themes")

    blanket = await adjudications.author(db, scope="global", term="mood.dread", action="DROP")

    assert await _terms(db, 2) == ["themes.obsession"]
    assert await _terms(db, 3) == []
    assert await _terms(db, 4) == ["themes.obsession"]
    assert blanket["applied"]["titles"] == 2
    assert blanket["applied"]["dropped"] == 2

    await adjudications.author(db, scope="title", title_id=4, term="themes.obsession", action="DROP")

    assert await _terms(db, 4) == []
    assert await _terms(db, 2) == ["themes.obsession"], "a title verdict reached another title"

    await adjudications.withdraw(db, blanket["row"]["id"])
    await _tag(db, 3, "mood.dread", "mood")
    await ledgers.apply_adjudications(db, 3)

    assert await _terms(db, 2) == ["themes.obsession"], "withdrawing restored nothing, as it must"
    assert await _terms(db, 3) == ["mood.dread"], "a withdrawn verdict is still being applied"


async def test_a_household_axis_survives_a_bundle_that_ships_the_same_facet(db, bundle_dir, vocab_dir):
    """Decision 342: "`importer/dna.load_axes` leaves in place, with a warning on the import report,
    any facet whose stored axis is household-authored. That is decision 423's rule applied to the
    third ledger: the household's curated row takes effect."

    The axis is authored first and the bundle's file for the same facet arrives after it, which is
    the order a household that typed an axis and then imported the day the corpus authored one
    would meet. Without the guard the loader rewrites the poles and replaces every weight while the
    row still says `household` -- the household's axis gone under its own label.
    """
    await _install(db, bundle_dir)
    await axes.author(
        db, facet=UNSHIPPED_FACET, left_pole="linear", right_pole="fractured",
        weights=[("structure.procedural", -0.5), ("pacing.relentless", 0.4)],
    )
    typed = await _axis(db, UNSHIPPED_FACET)
    (vocab_dir / f"{UNSHIPPED_FACET}.tsv").write_text(
        "episodic\tserial\nstructure.procedural\t1.0\n", encoding="utf-8"
    )
    report = ImportReport()

    await dna.load_axes(db, vocab_dir, "v1", report)

    assert report.ok, report.render()
    assert await _axis(db, UNSHIPPED_FACET) == typed
    kept = [f for f in report.findings if f.rule == "axes" and f.detail.get("facet") == UNSHIPPED_FACET]
    assert len(kept) == 1 and kept[0].severity == "warn", report.render()
    assert "342" in kept[0].message and "423" in kept[0].message, kept[0].message
    assert (await _axis(db, "mood"))[0] == "bundle", "the bundle's own facets stopped loading"


# --- what the editors refuse, and what nothing may withdraw -----------------------------------


async def test_a_bundle_row_is_read_only_in_every_editor(db, bundle_dir):
    """Decision 445: "A bundle row is read-only in the app ... The household may withdraw only its
    own rows." A bundle row withdrawn in the app comes back at the next models-only import, so the
    withdrawal would be a fix that lasts until the household forgets it made one. The axis half is
    the same rule over a key with room for one row: `dna_axis` is keyed on (version, facet), so a
    household axis over a shipped facet could only be written by overwriting the bundle's."""
    await _install(db, bundle_dir)
    before = await _ledgers(db)
    verdict = await db.fetchval("SELECT id FROM dna_adjudication WHERE origin = 'bundle' LIMIT 1")
    fact = await db.fetchval("SELECT id FROM credit_correction WHERE origin = 'bundle' LIMIT 1")

    with pytest.raises(Refused):
        await adjudications.withdraw(db, verdict)
    with pytest.raises(Refused):
        await corrections.withdraw(db, fact)
    with pytest.raises(Refused):
        await axes.withdraw(db, "mood")
    with pytest.raises(Refused):
        await axes.author(
            db, facet="mood", left_pole="grim", right_pole="sunny", weights=[("mood.dread", -1.0)]
        )
    with pytest.raises(Refused):
        await axes.export(db, "mood")

    assert await _ledgers(db) == before
    with pytest.raises(LookupError):
        await adjudications.withdraw(db, 10**9)
    with pytest.raises(LookupError):
        await corrections.withdraw(db, 10**9)
    with pytest.raises(LookupError):
        await axes.withdraw(db, UNSHIPPED_FACET)


async def test_the_verdict_editor_refuses_what_the_applier_could_not_apply(db, bundle_dir):
    """Each refusal is a row the applier would store and then ignore or miscount: a re-point onto a
    term `dna_term` does not carry (`derive/ledgers._repoint` counts `repoint_target_unknown` and
    moves nothing), a DROP_EVIDENCE with no quote (which drops every quote the term has on the
    title), a DROP_EVIDENCE with no title (the blanket sweep never reaches that action), and a title
    this install does not hold. Refused here, where the admin sees why, rather than written as a
    fix that is never true. No vocabulary refuses first: `dna_adjudication.version` is an FK."""
    with pytest.raises(Refused, match="vocabulary"):
        await adjudications.author(db, scope="global", term="mood.dread", action="DROP")
    await _install(db, bundle_dir)
    before = await _ledgers(db)

    refusals = [
        dict(scope="title", title_id=1, term="mood.cosy", action="REPOINT", target="mood.invented"),
        dict(scope="title", title_id=1, term="mood.cosy", action="REPOINT"),
        dict(scope="title", title_id=1, term="mood.cosy", action="DROP_EVIDENCE"),
        dict(scope="global", term="mood.cosy", action="DROP_EVIDENCE", quote="a phrase"),
        dict(scope="title", title_id=999, term="mood.cosy", action="DROP"),
        dict(scope="title", term="mood.cosy", action="DROP"),
        dict(scope="global", title_id=1, term="mood.cosy", action="DROP"),
        dict(scope="term", term="mood.cosy", action="DROP"),
        dict(scope="global", term="  ", action="DROP"),
        dict(scope="global", term="mood.cosy", action="KEEP"),
    ]
    for kwargs in refusals:
        with pytest.raises(Refused) as refused:
            await adjudications.author(db, **kwargs)
        assert refused.value.reason and refused.value.reason.isascii(), kwargs

    assert await _ledgers(db) == before


async def test_the_correction_editor_refuses_an_opinion_and_a_title_nobody_holds(db, bundle_dir):
    """`derive/ledgers.apply_corrections` stores an evidence-less row and refuses it at apply time
    ("a correction without evidence is an opinion"), which in an editor is a saved fix that never
    reaches the card; so the editor refuses it where the admin can see why. A kind outside
    `CORRECTION_KINDS` is the applier's `unknown_kind` for the same reason."""
    await _install(db, bundle_dir)
    before = await _ledgers(db)

    refusals = [
        dict(title_id=1, kind="composer", value="Elliot Goldenthal", evidence=" "),
        dict(title_id=999, kind="composer", value="Elliot Goldenthal", evidence="end credits"),
        dict(title_id=1, kind="director", value="Michael Mann", evidence="end credits"),
        dict(title_id=1, kind="composer", value="", evidence="end credits"),
    ]
    for kwargs in refusals:
        with pytest.raises(Refused) as refused:
            await corrections.author(db, **kwargs)
        assert refused.value.reason and refused.value.reason.isascii(), kwargs

    assert await _ledgers(db) == before


async def test_the_axis_editor_refuses_every_file_load_axes_would_refuse(db, bundle_dir):
    """Decision 342: it "validates facet, terms and weights exactly as `importer/dna.load_axes`
    does, including at least one weight row (decision 264), so nothing the editor saves is a file
    the loader would refuse". An unknown facet is the FK `load_axes` checks before it writes; a
    weight outside [-1, 1] is its `report.fail`; a non-number is its skipped row; zero weight rows
    is decision 264's truncated export; a duplicated term is two rows the key holds as one."""
    with pytest.raises(Refused, match="vocabulary"):
        await axes.author(db, facet="mood", left_pole="a", right_pole="b", weights=[("mood.dread", 1)])
    await _install(db, bundle_dir)
    before = await _ledgers(db)

    good = dict(facet=UNSHIPPED_FACET, left_pole="linear", right_pole="fractured")
    refusals = [
        dict(good, facet="tone", weights=[("structure.procedural", 0.5)]),
        dict(good, weights=[("structure.procedural", 1.5)]),
        dict(good, weights=[("structure.procedural", float("nan"))]),
        dict(good, weights=[("structure.procedural", float("inf"))]),
        dict(good, weights=[("structure.procedural", "heavy")]),
        dict(good, weights=[]),
        dict(good, weights=[("structure.procedural", 0.5), ("structure.procedural", -0.5)]),
        dict(good, weights=[(" ", 0.5)]),
        dict(good, left_pole=" ", weights=[("structure.procedural", 0.5)]),
    ]
    for kwargs in refusals:
        with pytest.raises(Refused) as refused:
            await axes.author(db, **kwargs)
        assert refused.value.reason and refused.value.reason.isascii(), kwargs

    assert await _ledgers(db) == before
