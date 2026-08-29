"""The coverage map is a contract, and this is what enforces it.

`spec_coverage.toml` names every testable requirement, the milestone that owes it a test, and
the tests that assert it. Two rules:

  1. Every requirement at or before `current_milestone` names at least one test.
  2. Every named test exists — in pytest or in the Playwright suite.

Raising `current_milestone` therefore turns the next milestone's obligations into failures with
names, which is the whole point: a test plan nobody runs is a wish, and a coverage number
nobody reads is worse. A requirement that genuinely should not be tested yet carries an
explicit `waived = "reason"` and shows up in the report rather than disappearing.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
MAP = TESTS / "spec_coverage.toml"

MILESTONES = ["M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"]
KINDS = {"backend", "integration", "e2e", "static"}


def load() -> dict:
    return tomllib.loads(MAP.read_text(encoding="utf-8"))


COVERAGE = load()
REQUIREMENTS = COVERAGE["requirement"]
CURRENT = COVERAGE["current_milestone"]


def _pytest_ids() -> set[str]:
    """`path::name` for every test function in the backend suite.

    Parsed rather than collected: importing pytest's collector from inside a test run is
    fragile, and a regex over `def test_*` cannot itself fail in a way that hides a gap — a
    missed function shows up as a *missing* test, which fails loudly.
    """
    ids: set[str] = set()
    for path in TESTS.rglob("test_*.py"):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_\w+)", text, re.M):
            ids.add(f"{rel}::{match.group(1)}")
    return ids


def _playwright_ids() -> set[str]:
    """`path::title` for every Playwright test."""
    ids: set[str] = set()
    specs = REPO / "e2e" / "specs"
    if not specs.is_dir():
        return ids
    for path in specs.glob("*.spec.js"):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*test\(\s*(['\"`])(.+?)\1", text, re.M | re.S):
            ids.add(f"{rel}::{match.group(2)}")
        # `for (…) { test(\`… ${x} …\`) }` — template titles cannot be matched literally, so a
        # file that uses them registers a wildcard the map can point at.
        if re.search(r"^\s*test\(\s*`[^`]*\$\{", text, re.M):
            ids.add(f"{rel}::*")
    return ids


KNOWN_TESTS = _pytest_ids() | _playwright_ids()


def _at_or_before(milestone: str) -> bool:
    return MILESTONES.index(milestone) <= MILESTONES.index(CURRENT)


# --- the map itself is well-formed ----------------------------------------------------


def test_current_milestone_is_a_real_milestone():
    assert CURRENT in MILESTONES


def test_requirement_ids_are_unique():
    ids = [r["id"] for r in REQUIREMENTS]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate requirement ids: {duplicates}"


def test_every_requirement_is_well_formed():
    problems = []
    for r in REQUIREMENTS:
        for field in ("id", "spec", "milestone", "kind", "what", "why"):
            if not r.get(field):
                problems.append(f"{r.get('id', '?')}: missing {field}")
        if r.get("milestone") not in MILESTONES:
            problems.append(f"{r['id']}: unknown milestone {r.get('milestone')!r}")
        if r.get("kind") not in KINDS:
            problems.append(f"{r['id']}: unknown kind {r.get('kind')!r}")
        if len(r.get("what", "")) < 30:
            problems.append(f"{r['id']}: `what` is too vague to be a test")
    assert not problems, "\n".join(problems)


def test_every_milestone_is_represented():
    """A milestone with no requirements has no exit criterion anyone can check."""
    covered = {r["milestone"] for r in REQUIREMENTS}
    assert covered == set(MILESTONES), f"no requirements for: {sorted(set(MILESTONES) - covered)}"


# --- rule 2: named tests must exist ---------------------------------------------------


def test_every_named_test_exists():
    missing = []
    for r in REQUIREMENTS:
        for test_id in r.get("tests", []):
            if test_id in KNOWN_TESTS:
                continue
            # A file-level wildcard covers parameterised Playwright titles.
            path, _, _title = test_id.partition("::")
            if f"{path}::*" in KNOWN_TESTS:
                continue
            missing.append(f"{r['id']} names a test that does not exist: {test_id}")
    assert not missing, "\n".join(missing)


# --- rule 1: shipped milestones owe their tests ---------------------------------------


def test_shipped_requirements_are_covered():
    owed = [
        r
        for r in REQUIREMENTS
        if _at_or_before(r["milestone"]) and not r.get("tests") and not r.get("waived")
    ]
    if owed:
        lines = [
            f"{len(owed)} requirement(s) at or before {CURRENT} have no test.",
            "Write one, or add `waived = \"why not\"` to the row and say so out loud.",
            "",
        ]
        lines += [f"  [{r['milestone']} {r['kind']:11}] {r['id']}\n      {r['what'][:110]}" for r in owed]
        pytest.fail("\n".join(lines))


def test_waivers_are_explained():
    bad = [r["id"] for r in REQUIREMENTS if r.get("waived") and len(str(r["waived"])) < 20]
    assert not bad, f"a waiver needs a real reason: {bad}"


# --- the report ------------------------------------------------------------------------


def test_report(capsys):
    """Not an assertion - the map's current state, printed with -s so it is readable."""
    by_milestone: dict[str, list[dict]] = {}
    for r in REQUIREMENTS:
        by_milestone.setdefault(r["milestone"], []).append(r)

    lines = [f"\nspec coverage — current milestone {CURRENT}", ""]
    for milestone in MILESTONES:
        rows = by_milestone.get(milestone, [])
        covered = sum(1 for r in rows if r.get("tests"))
        waived = sum(1 for r in rows if r.get("waived") and not r.get("tests"))
        # ASCII markers: this prints to whatever console the developer has, and a Windows
        # cp1252 terminal turns a decorative glyph into a crash.
        marker = ">" if _at_or_before(milestone) else " "
        note = f" ({waived} waived)" if waived else ""
        lines.append(f"  {marker} {milestone}  {covered:>3}/{len(rows):<3} covered{note}")
    with capsys.disabled():
        print("\n".join(lines))
