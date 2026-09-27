"""§6.6 Data's three ledger editors: three artifacts, household rows only,
applied at once (decision 445). Exports are read back by the importer's
own readers, never by a parser written here. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from spielplan.curated import Refused, adjudications, axes, corrections
from spielplan.derive import ids, ledgers, rebuild
from spielplan.importer import dna
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

# The title the shipped `corrections_v1.tsv` names; no other title here is in the bundle's credit ledger.
CORRECTED = 8
BUNDLE_COMPOSER = "Kunihiko Murai"
HOUSEHOLD_COMPOSER = "The Household Composer"

# A facet with no shipped axis, so the household's axis lands beside the bundle's rather than over one.
UNSHIPPED_FACET = "structure"

# The three bytes a writer that joins on TAB and ends lines on LF corrupts.
AWKWARD = 'a line\twith a tab\nand a second, "quoted" line'


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    """Restated rather than imported: an imported fixture is
    an unused name to ruff and couples collection order."""
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


@pytest.fixture
def vocab_dir(bundle_dir) -> Path:
    return bundle_dir / "artifacts" / "dna_vocab" / "v1"


async def _install(conn, bundle_dir: Path) -> None:
    """Counts asserted, so a fixture that stopped shipping a ledger cannot turn a test green."""
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
    return [(r["name"], r["source"]) for r in await conn.fetch(
        "SELECT p.name, c.source FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')"
        " ORDER BY c.id", title_id)]


async def _source_credit(conn, title_id: int, name: str) -> None:
    person_id = await ids.upsert_person(conn, name=name)
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, role_class, source)"
        " VALUES ($1, $2, 'Sound', 'Original Music Composer', 'composer', 'tmdb')",
        title_id, person_id,
    )


async def _ledgers(conn) -> dict[str, list[tuple]]:
    """Ids included, so "left alone" means byte-for-byte."""
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
    head = await conn.fetchrow(
        "SELECT origin, left_pole, right_pole FROM dna_axis WHERE version = 'v1' AND facet = $1", facet
    )
    terms = await conn.fetch(
        "SELECT term, weight FROM dna_axis_weight WHERE version = 'v1' AND facet = $1 ORDER BY term",
        facet,
    )
    return (*tuple(head), tuple(tuple(r) for r in terms)) if head else ()


def _saved(tmp_path: Path, name: str, text: str) -> Path:
    """Bytes, not `write_text`: Windows text mode turns the LF inside a quoted field into CRLF."""
    folder = tmp_path / "export"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(text.encode("utf-8"))
    return path


async def test_each_editor_writes_only_its_own_table_and_only_as_the_household(db, bundle_dir):
    """Each write is checked against all four curated tables,
    so a sibling-table write is caught where it happens."""
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


async def test_the_verdict_export_folds_back_through_the_importers_reader_and_loader(
    db, bundle_dir, tmp_path
):
    """The loader, not the parser alone, because it is what a bundle carrying this file back would run."""
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
    """The household axis is withdrawn first, since `load_axes` leaves a household axis in place."""
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


async def test_a_household_correction_survives_a_models_only_re_import_and_still_takes_effect(
    db, bundle_dir
):
    """Before decision 326, a correction absent from the next bundle's TSV was wiped by the re-import."""
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
    """`composer_add` first: its withdrawal must take back
    exactly the minted row and nothing the source wrote."""
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
    """A bundle title has no raw store and is never re-derived, so nothing brings the credit back."""
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
    """Without the guard the loader overwrote the axis while the row still said `household`."""
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


async def test_a_bundle_row_is_read_only_in_every_editor(db, bundle_dir):
    """A bundle row withdrawn in the app comes back at the next models-only import."""
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
    """Each refusal is a row the applier would store and then ignore or miscount."""
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
    """The applier refuses an evidence-less row at apply time,
    so the editor refuses it where the admin sees why."""
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
