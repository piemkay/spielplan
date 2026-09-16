"""The import report as text: it prints on a Windows console, and a failure keeps its evidence.
Spec v2.1 §10.

    "Importer enforces every §4.1 landmine rule and produces a migration report (counts per
     table, validation failures, vocabulary version). Bundle re-import ... is a planned admin
     event with a diff report -- never a silent sync."

Two properties of that text, both measured false against the shipped code:

* It must encode to ASCII. CLAUDE.md's rule is not decoration here: `render()` is the message
  of every `assert report.ok, report.render()` in this suite and the text an ops script prints,
  and a Windows cp1252/cp850 console raises `UnicodeEncodeError` on a decorative glyph. The
  header's middle dot and the note line's check mark are why `ops/m45_exit_criterion.py` works
  around the report by never printing it. [M4.14 finding 2.24]
* A failure must render its `detail`. Rule 7 says "N denied table(s)" in the message and puts
  the names in `detail` (`validate.py`), and `render()` dropped every one of them -- so the only
  human-readable record of a refused import named nothing to go and fix, which is the opposite
  of what §10 asks a diff report to be. [M4.14 finding 2.22]

No database and no bundle: both assert about `ImportReport` itself, so the report under test is
built here rather than validated out of a fixture.
"""

from __future__ import annotations

import ast
from pathlib import Path

import spielplan
from spielplan.importer.report import _ASCII_FOLD, ImportReport

# The three methods that write a sentence an operator reads. Anything they are handed is report
# text, wherever in the package it was written.
_REPORT_CALLS = ("fail", "warn", "note")


def _report_with_one_of_each() -> ImportReport:
    """A report carrying a failure with `detail`, a warn and a note -- the three severities."""
    report = ImportReport(bundle_version="v20260828", vocabulary_version="v2")
    report.fail(
        "rule7-denylist",
        "bundle contains 2 denied table(s) - export must read live tables only",
        tables=["review_bak", "title_good"],
    )
    report.warn("corrections", "corrections_v1.tsv absent - curated credit fixes will not apply")
    report.note("rows", "12,345 rows read")
    return report


def test_one_statement_said_twice_is_one_finding():
    """A report is the set of statements an import made about a bundle, not a tally of calls.

    Two readers of one file are the shape this milestone deliberately keeps - `validate()` parses
    `corrections_v1.tsv` so the header check happens before anything is staged, and
    `load_corrections` parses it again because the rows it parses are the rows it writes - and
    they are two READINGS of one implementation on purpose, so that the two cannot come to
    disagree. What they must not also be is two report lines: measured on a ledger with one bad
    title_id, the import produced four `corrections` findings for two problems, and an operator
    counting lines to judge how bad a ledger is counted double. Section 10 makes this text the
    record the household comes back to.

    Identity is the whole finding - severity, rule, message and detail - so two facts that happen
    to share a rule stay two lines, and a per-file message is a different statement for each file.
    [M4.14 cycle 1, M414-REV-247-04]
    """
    report = ImportReport()
    report.warn("corrections", "corrections_v1.tsv parses to no corrections")
    report.warn("corrections", "corrections_v1.tsv parses to no corrections")
    report.warn("corrections", "seed_list.json parses to nothing")
    report.fail("axes", "mood.tsv: weight 2.0 is outside [-1, 1]", file="mood.tsv")
    report.fail("axes", "mood.tsv: weight 2.0 is outside [-1, 1]", file="mood.tsv")
    report.note("rows", "8 rows read", table="title")
    report.note("rows", "8 rows read", table="person")

    assert [(f.severity, f.rule, f.message) for f in report.findings] == [
        ("warn", "corrections", "corrections_v1.tsv parses to no corrections"),
        ("warn", "corrections", "seed_list.json parses to nothing"),
        ("fail", "axes", "mood.tsv: weight 2.0 is outside [-1, 1]"),
        ("note", "rows", "8 rows read"),
        ("note", "rows", "8 rows read"),
    ], report.render()
    assert [f.detail["table"] for f in report.findings if f.rule == "rows"] == [
        "title", "person"
    ], "two counts that differ only in their detail are two facts"


def test_render_encodes_to_ascii():
    """CLAUDE.md: console and test output is ASCII, because cp1252/cp850 consoles crash on glyphs."""
    text = _report_with_one_of_each().render()

    offenders = sorted({c for c in text if ord(c) > 127})
    assert not offenders, (
        "render() is printed by ops scripts and by failing assertions on a Windows console, so it "
        "must be ASCII (CLAUDE.md); non-ASCII character(s): "
        + ", ".join(hex(ord(c)) for c in offenders)
    )
    assert "\u00b7" not in text, "the header's middle dot: cp850 and cp1252 both choke on it"
    assert "\u2713" not in text, "the note glyph: 'render().encode(\"cp1252\")' raised on this one"
    text.encode("ascii")

    # The frame was never the whole surface, and the report above cannot show that: its own
    # strings are hand-written ASCII, so this test measured the header and the severity glyphs and
    # nothing else. The MESSAGES are written in the house voice -- a section citation and an em
    # dash, which is how `validate.py` and `dna.py` write all sixty-three of theirs -- and measured
    # on the shipped fixture, `render()` returned eight lines carrying U+2014 with every assertion
    # above green. A fold that simply dropped the glyphs would satisfy the encode() call and lose
    # the one token an operator searches the spec for, so the mapping is asserted and not only the
    # encoding. [M4.14 cycle 2 close-out, finding 2.24]
    house = ImportReport(bundle_version="v20260828", vocabulary_version="v2")
    house.fail("rating-source", "`rating_source` is missing \u2014 it is mandatory (\u00a74.3)")
    spoken = house.render()

    spoken.encode("ascii")
    assert "`rating_source` is missing - it is mandatory (section 4.3)" in spoken, spoken


def test_every_message_this_package_writes_folds_to_ascii():
    """`_ASCII_FOLD`'s own claim, which the test above cannot make.

    The comment over that map argues for a map rather than `encode("ascii", "replace")` on the
    grounds that "a codepoint outside this map survives to `test_render_encodes_to_ascii`, which
    fails naming it - a red test here rather than a UnicodeEncodeError on somebody's console".
    That was false: the test above builds its report by hand, so it can only ever measure the
    frame and the two glyphs it was written with, and a message written with a curly apostrophe,
    an ellipsis or an arrow would reach the operator's console unfolded. Sixty-odd report
    messages exist across `validate.py`, `dna.py`, `load.py`, `bundle.py`, `meta.py` and
    `reviews.py` and this milestone added many of them; the house voice writes section citations
    and em dashes, which is why the map has entries at all.

    The SOURCE is what is read, because that is where the next message will be written, and the
    offending codepoint is named as U+XXXX rather than printed - a guard whose failure message
    crashes the console it is protecting would be the same defect one layer out.
    [M4.14 cycle 3, m414-c3-rec-05; cycle 2 close-out, finding 2.24]
    """
    package = Path(spielplan.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _REPORT_CALLS:
                continue
            for text in ast.walk(node):
                if not isinstance(text, ast.Constant) or not isinstance(text.value, str):
                    continue
                for char in text.value:
                    if ord(char) > 127 and char not in _ASCII_FOLD:
                        offenders.append(
                            f"{path.relative_to(package)}:{text.lineno} U+{ord(char):04X}"
                        )

    assert not offenders, (
        "a report message carries a codepoint _ASCII_FOLD does not fold, so render() will raise "
        "UnicodeEncodeError on a cp1252 console (CLAUDE.md): "
        + ", ".join(sorted(set(offenders)))
        + " - add the glyph to _ASCII_FOLD with the plain text it stands for, or write the "
        "message in ASCII"
    )


def test_a_failure_renders_a_line_per_detail_key():
    """A failure's structured evidence is rendered; a warn and a note stay one line each."""
    report = ImportReport(bundle_version="v20260828", vocabulary_version="v2")
    report.fail(
        "rule7-denylist",
        "bundle contains 3 denied table(s)",
        tables=["review_bak", "title_good", "dna_tag_bak"],
        database="reviews.sqlite",
    )
    ids = [f"tt{n:07d}" for n in range(9)]
    report.fail("rule3-orphan", "42 orphan row(s) in dna_evidence", rows=42, title_ids=ids)
    report.warn("corrections", "corrections_v1.tsv absent", path="corrections_v1.tsv")
    report.note("table-skipped", "`review` not loaded: content tier", table="review")

    text = report.render()
    lines = text.splitlines()

    # Sorted keys, one line each, directly beneath the message they belong to.
    head = lines.index("x rule7-denylist: bundle contains 3 denied table(s)")
    assert lines[head + 1] == "    database: reviews.sqlite", lines[head : head + 4]
    assert lines[head + 2] == "    tables: review_bak, title_good, dna_tag_bak", lines[head : head + 4]

    # A long list is excerpted with its total, not pasted whole into a console.
    excerpt = next(ln for ln in lines if ln.startswith("    title_ids: "))
    assert "tt0000000" in excerpt and "(9 total)" in excerpt, excerpt
    assert "tt0000005" not in excerpt, excerpt
    assert "    rows: 42" in lines

    # Warnings and notes are not a remediation surface, and the report is already long.
    assert "    path: corrections_v1.tsv" not in lines, "a warn must stay one line"
    assert "    table: review" not in lines, "a note must stay one line"
    assert len([ln for ln in lines if ln.startswith("    ")]) == 4, lines

    # The structured surface is untouched: the API, the stored row and BundleImport.svelte all
    # read `findings[].detail`, and only the rendered excerpt is truncated.
    stored = [f for f in report.as_dict()["findings"] if f["rule"] == "rule3-orphan"][0]
    assert stored["detail"]["title_ids"] == ids, "as_dict() carries the whole list"
