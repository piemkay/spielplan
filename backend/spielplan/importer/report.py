"""The import report (§10).

`fail` stops an import; `warn` must be seen; `note` is a counted fact, recorded even when clean.
One report per import; the re-import "diff" is the rebuild set it carries (decisions 162, 163).
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


# `render` prints a failure's `detail`; these limits keep it readable.
_DETAIL_ITEMS = 5
_DETAIL_CHARS = 96


def _detail_value(value: Any) -> str:
    """One short line's worth of a `Finding.detail` value. Deterministic: sets are sorted, lists keep
    order.
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


# Messages carry § and dashes while `render` promises ASCII. A map, not "replace", so a section
# number survives; an unmapped glyph fails `test_every_message_this_package_writes_folds_to_ascii`.
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
    # For shipped tables nothing mapped.
    skipped_tables: dict[str, str] = field(default_factory=dict)

    def _record(self, severity: Severity, rule: str, message: str, detail: dict[str, Any]) -> None:
        """One statement, recorded once.

        Identity is the whole finding (severity, rule, message, detail), compared via `repr` since a
        detail need not be a scalar.
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
        """Record a shipped table this app deliberately does not load, and why (as a note too)."""
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

        ASCII only: it reaches Windows consoles and assertion messages. `_ascii` folds the messages too.
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
                # A failure's `detail` is its remediation; warnings and notes stay one line.
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
