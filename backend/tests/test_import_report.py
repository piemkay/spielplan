"""The import report as text (§10): it must encode to ASCII for a cp1252 console, and a failure
must render its `detail`. No database."""

from __future__ import annotations

import ast
from pathlib import Path

import spielplan
from spielplan.importer.report import _ASCII_FOLD, ImportReport

# The methods that write a sentence an operator reads: anything handed to them is report text.
_REPORT_CALLS = ("fail", "warn", "note")


def _report_with_one_of_each() -> ImportReport:
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
    """Identity is the whole finding (severity, rule, message, detail): two readers of one file must
    not produce two report lines, since an operator counts lines to judge a ledger."""
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

    # Messages use the house voice (a § and an em dash); the mapping is asserted, not only the encoding.
    house = ImportReport(bundle_version="v20260828", vocabulary_version="v2")
    house.fail("rating-source", "`rating_source` is missing \u2014 it is mandatory (\u00a74.3)")
    spoken = house.render()

    spoken.encode("ascii")
    assert "`rating_source` is missing - it is mandatory (section 4.3)" in spoken, spoken


def test_every_message_this_package_writes_folds_to_ascii():
    """The SOURCE is read, because that is where the next message
    will be written; the codepoint is named as U+XXXX."""
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

    # The API, the stored row and BundleImport.svelte read `findings[].detail`; only the render truncates.
    stored = [f for f in report.as_dict()["findings"] if f["rule"] == "rule3-orphan"][0]
    assert stored["detail"]["title_ids"] == ids, "as_dict() carries the whole list"
