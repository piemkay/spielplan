"""The import report. Spec v2.1 §10.

"Importer enforces every §4.1 landmine rule and produces a migration report (counts per table,
validation failures, vocabulary version). Bundle re-import … is a planned admin event with a
diff report — never a silent sync."

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

    def fail(self, rule: str, message: str, **detail: Any) -> None:
        self.findings.append(Finding("fail", rule, message, detail))

    def warn(self, rule: str, message: str, **detail: Any) -> None:
        self.findings.append(Finding("warn", rule, message, detail))

    def note(self, rule: str, message: str, **detail: Any) -> None:
        self.findings.append(Finding("note", rule, message, detail))

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
        """A human-readable report — this is what the wizard and the Data tab show."""
        lines = [
            f"bundle {self.bundle_version or '(unknown)'} · "
            f"vocabulary {self.vocabulary_version or '(unknown)'}",
            "",
        ]
        for severity, glyph in (("fail", "x"), ("warn", "!"), ("note", "✓")):
            group = [f for f in self.findings if f.severity == severity]
            for f in group:
                lines.append(f"{glyph} {f.rule}: {f.message}")
        if self.table_counts:
            lines.append("")
            lines.append("rows:")
            width = max(len(t) for t in self.table_counts)
            for table, n in sorted(self.table_counts.items()):
                lines.append(f"  {table.ljust(width)}  {n:>10,}")
        return "\n".join(lines)
