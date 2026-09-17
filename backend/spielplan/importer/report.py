"""The import report. Spec v2.1 §10.

"Importer enforces every §4.1 landmine rule and produces a migration report (counts per table,
validation failures, vocabulary version). Model re-import (retrained backbone) is a planned admin
event with a migration report — never a silent sync."

As §10 read until M4.16 that second sentence named "a planned admin event with a diff report", and
`backend/migrations/0001_system.sql:32` block-quotes it above `artifact_bundle`. That migration is
applied and sha256-checksummed, so an edit there is a hard startup error on every install that has
run it and its copy cannot be corrected in place: this paragraph is where the correction lives.
No second document was ever written. One report is produced per import, and the diff §10 asks a
re-import for is the rebuild set this report carries (decisions 162 and 163).

A report has three severities and only one of them stops an import:

* `fail`   — a landmine rule was violated. The import does not proceed.
* `warn`   — something the operator must see but which does not invalidate the data.
* `note`   — a counted fact (row counts, duplicate counts, the shared-pair count). Notes are
             the diff material for a re-import, so they are recorded even when everything is fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["fail", "warn", "note"]


@dataclass
class Finding:
    severity: Severity
    rule: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "detail": self.detail,
        }


# `render` prints a failure's `detail` and these are the limits that keep that readable. Every
# shipped detail value is a scalar or a list of names -- the denied tables, the columns a mapping
# names and the bundle lacks, the first offending title ids -- and the referential checks count
# rows in the thousands, so five names and the total say where to look without pasting a corpus
# into a console. An excerpt that admits its own size is still §10's migration report; a wall of
# hundred ids is one nobody reads.
_DETAIL_ITEMS = 5
_DETAIL_CHARS = 96


def _detail_value(value: Any) -> str:
    """One short line's worth of a `Finding.detail` value, for `ImportReport.render`.

    Deterministic on purpose: this text is what two import runs get compared by, so a set is
    sorted rather than left in whatever order it happened to iterate in, while a list keeps the
    order its producer chose — rule 7 sorts its table names before it reports them, and that is
    the order the operator should see them in.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        text = ", ".join(str(v) for v in items[:_DETAIL_ITEMS])
        tail = f", ... ({len(items)} total)" if len(items) > _DETAIL_ITEMS else ""
    else:
        text, tail = str(value), ""
    if len(text) > _DETAIL_CHARS:
        text = f"{text[: _DETAIL_CHARS - 3]}..."
    return f"{text}{tail}"


# The house voice writes report messages with section citations and em dashes -- `rating_source is
# missing - it is mandatory in every bundle` is one of sixty-three -- while `render`'s docstring
# promises ASCII. Both were true of the FRAME and only of the frame: measured on the shipped
# fixture, `render()` returned eight lines carrying U+2014 with this file's own ASCII test green,
# because that test builds its report out of hand-written ASCII and so can only ever measure the
# frame. A map rather than `encode("ascii", "replace")`, because a `?` where a section number was is
# a report that has lost the one token an operator would search the spec for, and replacing hides
# the next glyph instead of showing it: a codepoint outside this map has to arrive as a red test
# rather than as a UnicodeEncodeError on somebody's console. The backstop that does that is
# `test_import_report.py::test_every_message_this_package_writes_folds_to_ascii`, which reads the
# `fail`/`warn`/`note` call sites under `backend/spielplan` and fails naming the file, the line
# and the codepoint. It is NOT `test_render_encodes_to_ascii`, which this comment named for one
# cycle: that test builds its report out of hand-written ASCII and so can only ever measure the
# frame, which is the half of the defect above that was already green.
# [M4.14 cycle 2 close-out, finding 2.24; cycle 3, m414-c3-rec-05]
_ASCII_FOLD = {
    "\u2014": "-",         # em dash, the separator most messages use
    "\u2013": "-",         # en dash
    "\u00a7": "section ",  # the spec citation, spelled out rather than dropped
    "\u00b7": "-",         # the header's middle dot, if a message ever grows one
    "\u2713": "+",         # the note glyph, same
}


def _ascii(text: str) -> str:
    """`render`'s last step, so that function's first sentence is true of its output too."""
    for glyph, plain in _ASCII_FOLD.items():
        text = text.replace(glyph, plain)
    return text


def _identity(finding: Finding) -> tuple:
    """What makes two findings the same statement, for `ImportReport._record`."""
    return (
        finding.severity,
        finding.rule,
        finding.message,
        tuple(sorted((key, repr(value)) for key, value in finding.detail.items())),
    )


@dataclass
class ImportReport:
    bundle_version: str | None = None
    vocabulary_version: str | None = None
    findings: list[Finding] = field(default_factory=list)
    table_counts: dict[str, int] = field(default_factory=dict)
    unmapped_columns: dict[str, list[str]] = field(default_factory=dict)
    # §10 wants "counts per table" for the *bundle's* tables, not for the ones this app happens
    # to map. `unmapped_columns` covered columns inside a mapped table, so a shipped table
    # nothing mapped had no line anywhere and three of them were dropped for five milestones.
    skipped_tables: dict[str, str] = field(default_factory=dict)

    def _record(self, severity: Severity, rule: str, message: str, detail: dict[str, Any]) -> None:
        """One statement, recorded once.

        A report is the SET of statements an import made about a bundle, and this list is what
        `render()` prints and what `artifact_bundle.report` stores - so the same sentence twice
        is not two facts, it is one fact an operator counts twice on the Data tab.

        The shape that produces it is deliberate and stays. This milestone reads
        `corrections_v1.tsv` from `validate()` so decision 247's header check happens before
        anything is staged, and `load_corrections` reads it again because the rows it parses are
        the rows it writes; two READINGS of one implementation is the design, since a second
        implementation is how two readers come to disagree about one file. Two report LINES is
        not: measured on a ledger with one bad title_id, the import produced four `corrections`
        findings for two problems.

        Identity is the whole finding - severity, rule, message and `detail` - so two counts that
        differ only in their `detail` stay two lines and a per-file message stays one line per
        file. Compared through `repr` rather than by `==`, because a `detail` value need not be a
        scalar and an array compares elementwise into something that is not a truth value.
        [M4.14 cycle 1, M414-REV-247-04]
        """
        candidate = Finding(severity, rule, message, detail)
        key = _identity(candidate)
        if any(_identity(f) == key for f in self.findings):
            return
        self.findings.append(candidate)

    def fail(self, rule: str, message: str, **detail: Any) -> None:
        self._record("fail", rule, message, detail)

    def warn(self, rule: str, message: str, **detail: Any) -> None:
        self._record("warn", rule, message, detail)

    def note(self, rule: str, message: str, **detail: Any) -> None:
        self._record("note", rule, message, detail)

    def skip_table(self, table: str, reason: str) -> None:
        """Record a shipped table this app deliberately does not load, and why.

        A note as well as a dict entry: `render()` is what the wizard and the Data tab show,
        and a decision the operator cannot read is indistinguishable from an oversight.
        """
        self.skipped_tables[table] = reason
        self.note("table-skipped", f"`{table}` not loaded: {reason}", table=table)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle_version": self.bundle_version,
            "vocabulary_version": self.vocabulary_version,
            "ok": self.ok,
            "counts": self.table_counts,
            "unmapped_columns": self.unmapped_columns,
            "skipped_tables": self.skipped_tables,
            "findings": [f.as_dict() for f in self.findings],
        }

    def render(self) -> str:
        """A human-readable report — this is what the wizard and the Data tab show.

        ASCII only, deliberately. CLAUDE.md's rule is load-bearing for this string in particular:
        it is the message of every `assert report.ok, report.render()` in the suite and the text
        an ops script prints, and a Windows cp1252/cp850 console raises UnicodeEncodeError on a
        decorative glyph — the header's middle dot and the note line's check mark are why
        `ops/m45_exit_criterion.py` works around this report by never printing it. The structured
        findings carry a glyph map of their own in `BundleImport.svelte`, where a browser renders
        it and it costs nothing, so no legibility is lost spending `-` and `+` here.
        [M4.14 finding 2.24]

        THE FRAME WAS NOT THE ONLY SURFACE. That repair took the glyphs out of the header and the
        note line and left the MESSAGES alone, and the messages are written in the house voice:
        measured on the shipped fixture afterwards, this function still returned eight lines
        carrying U+2014. So the paragraph above was true of the code and false of the string, which
        is the half a reader checks. `_ascii` is what makes it true of both, and it is applied to
        the joined text rather than to the messages because those are also
        `as_dict()["findings"]` - the browser surface the paragraph above deliberately exempts.
        [M4.14 cycle 2 close-out, finding 2.24]
        """
        lines = [
            f"bundle {self.bundle_version or '(unknown)'} - "
            f"vocabulary {self.vocabulary_version or '(unknown)'}",
            "",
        ]
        for severity, glyph in (("fail", "x"), ("warn", "!"), ("note", "+")):
            group = [f for f in self.findings if f.severity == severity]
            for f in group:
                lines.append(f"{glyph} {f.rule}: {f.message}")
                # A failure's `detail` is its remediation information, and dropping it left the
                # operator holding rule 7's "N denied table(s)" with no table named to go and fix,
                # while the names sat in a dict only the JSON surfaces ever read. §10 calls a
                # re-import "a planned admin event with a migration report", and an event whose
                # readable record omits what went wrong is not one anybody can act on. Warnings
                # and notes keep their single line: a clean import already renders fourteen
                # findings, and only a failure is a surface anyone has to act on.
                # [M4.14 finding 2.22]
                if severity == "fail":
                    for key in sorted(f.detail):
                        lines.append(f"    {key}: {_detail_value(f.detail[key])}")
        if self.table_counts:
            lines.append("")
            lines.append("rows:")
            width = max(len(t) for t in self.table_counts)
            for table, n in sorted(self.table_counts.items()):
                lines.append(f"  {table.ljust(width)}  {n:>10,}")
        return _ascii("\n".join(lines))
