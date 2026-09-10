"""The coverage map is a contract, and this is what enforces it.

`spec_coverage.toml` names every testable requirement, the milestone that owes it a test, and
the tests that assert it. Two rules:

  1. Every requirement at or before `current_milestone` names at least one test.
  2. Every named test exists — in pytest, in the Playwright suite, or in the frontend's vitest.

Raising `current_milestone` therefore turns the next milestone's obligations into failures with
names, which is the whole point: a test plan nobody runs is a wish, and a coverage number
nobody reads is worse. A requirement that genuinely should not be tested yet carries an
explicit `waived = "reason"` and shows up in the report rather than disappearing.
"""

from __future__ import annotations

import re
import tomllib
from itertools import zip_longest
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
MAP = TESTS / "spec_coverage.toml"
LEDGER = REPO / "docs" / "TESTING.md"

# M4.5 is not in §12. It exists because §12's M0 importer was verified against a fixture
# that did not resemble the artifact it stands in for, and M5's pipeline cannot be built
# on a placement path that has never met the real feature contract. See
# docs/milestones/M4.5-plan.md.
#
# Nor were M4.9, M4.12 and M4.13 when their first rows landed, and for the same kind of
# reason: the September 2026
# pre-release review found three defects that only the real corpus bundle can express — a
# title card that throws on a keyed each, a fifth of the basis served at e(t) = 0, and a
# pair search that costs a minute. `docs/milestones/ROADMAP-to-M5.md`'s "Start here" table
# takes those three out of their milestones and ships them first, so the rows land here
# ahead of the milestones that own the rest. THE ORDER IS AUTHORED, NOT SORTED:
# `_at_or_before` uses `MILESTONES.index`, and a string sort would put "M4.10" before
# "M4.5". M4.11 and M4.14 through M4.16 are not in the list yet — each is added
# by the milestone that opens it, in one commit with its first row.
#
# Nor was M4.6 in §12: its row was added to the table this week, together with the
# §3.1/§6.2/§6.5/§6.6 amendments that decisions 164 and 166 forced. §6.6 sketched the
# household's user management in one line and §12 scheduled it nowhere, so the only
# account-minting UI ever built is the first-boot wizard's member step — which becomes
# unreachable the moment an admin exists, and which decision 164 removes from §3.1's
# sequence in favour of §6.6's Users screen. It goes before M4.9 because it is a
# milestone rather than one of the three pre-release fixes that ship ahead of theirs.
# See docs/milestones/M4.6-plan.md.
#
# Nor is M4.7, which follows it and is the same shape: §2 promises required config,
# secrets custody with rotation, a nightly dump with rotation 14 and a restore, §5.3
# lists the jobs and §1 pins the image, and §12 scheduled none of it. Decision 181 adds
# the row. It sits between M4.6 and M4.9 because it ships before the release cut and
# because M4.9 through M4.13 consume two seams it owns — the app-level 409 handler and
# the `job_run` table. See docs/milestones/M4.7-plan.md.
#
# Nor is M4.8, and it is M4.5's shape rather than M4.6's and M4.7's: it ships no surface
# and no clause §12 scheduled, so it takes no row in that table and none was added. It is
# the instrument — the bundle fixture, this file's own layer, the two §4.1 landmine
# guards, the browser harness, CI's trigger and installer, and the three exit scripts —
# and it exists because each of those printed as covered while unable to fail: a fixture
# that cannot express the corpus's awkward shapes, a harness with no failure branch, a CI
# trigger that never fires on a milestone branch, and three exit scripts of which two
# cannot report their own failure. It goes after M4.7 and before M4.9 for one reason:
# M4.9 through M4.16 are measured by exactly these instruments, so a milestone that
# repairs them after they have been relied on proves nothing about the work that already
# passed through them. Decision 183 keeps the one job that needs the real corpus on a
# self-hosted runner and off every branch gate, so this milestone's own evidence stays
# reproducible rather than becoming a secret nobody can re-run. See
# docs/milestones/M4.8-plan.md.
#
# M4.9, which follows it, is the one entry above that has since GAINED a §12 row rather
# than staying outside the table: decisions 187-193 add it, because the milestone ships
# surface (the §6.0 credit count line and disclosure, the §6.6 Data card's outstanding
# authoring task, §6.7's rail on every screen) and because what it repairs is M0's own
# exit criterion, asserted for four milestones against a fixture in which the corpus's
# two department spellings, two facet namings and self-qualified term ids do not occur.
# It sits here — after M4.8, before M4.12 — for the reason M4.8's paragraph gives from the
# other side: every row below is measured by the instrument M4.8 repaired, and the three
# pre-release fixes already parked at M4.9 above are the same milestone's first three
# commits. See docs/milestones/M4.9-plan.md.
#
# M4.10 follows it and takes a §12 row too, on M4.9's argument rather than M4.6's and
# M4.7's: §12's M2 row owns the rating view, the Personal Ledger and §6.1's prediction
# reveal and §12's M3 row owns Rank's tiers and its comparison queue, and both were closed
# against surfaces that guard every Ledger write with a read, then some work, then a write,
# on a connection that autocommits each statement — asserted one request at a time, which
# is the only way they hold. Two gathered answers under one sealed pair write two `duel`
# rows in most races with no injected latency, one of those races drawing §13's held-out
# arm; two gathered Rate taps on one card token leave the loser holding a refusal §6.1 does
# not define — since M4.7's handler a 409 naming `rate_observation_seq`, not the plan's 500,
# which is no more actionable by the client — with its Jellyfin write already sent; and both
# Rank routes raise after their observation is durable, so every retry writes another
# append-only row. What it applies is the property `write_txn(lock=...)` (`api/deps.py:73-92`)
# exists for — the winner's write is serialised ahead of the loser's read, so the loser reads
# what the winner committed — at four Ledger seams, in the form each one can carry: the
# two-int `pg_advisory_xact_lock` that `tonight/play.py:611` uses, issued as the first
# statement inside the Rank answer's `write_txn(conn)` and not through that helper's own
# `lock=`, whose single-argument `hashtext(...)::bigint` form is a different lock space that
# could not collide with a number here (`api/deps.py:86-88` says so); a `SELECT ... FOR UPDATE`
# on `rate_session` as the first statement of every Rate write; an `AND card_token IS NULL` on the
# card stash; and a compare-and-set on the tier set a refit fitted against. The fit runs after the
# commit, never under the lock. It sits here, after M4.9, because its rows are measured by
# the instrument M4.8 repaired and read the layer M4.9 corrected: the reveal row is measured
# on the seed list M4.9's loader fills, and the two queue rows read the board its facet work
# renders. Decisions 199-208 record the calls it needed. See docs/milestones/M4.10-plan.md.
MILESTONES = ["M0", "M1", "M2", "M3", "M4", "M4.5", "M4.6", "M4.7", "M4.8", "M4.9", "M4.10",
              "M4.12", "M4.13", "M5", "M6", "M7"]
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


def _vitest_ids() -> set[str]:
    """`path::title` for every vitest test in the frontend.

    The third runner, and until M4.9 the map could not name one. §6.7's rail is where that
    stopped being a formatting detail: the drawer must drop its log on close, the frame in which
    it would show the previous open's events is one round trip long, and Playwright cannot hold a
    response the app's service worker mediates -- so the only layer that can assert the clause is
    a mounted component, and a row pointing at it would have failed rule 2 for naming a test that
    "does not exist". A map that can only see two of the three suites pushes every claim it
    cannot name either into a suite that cannot fail on it or out of the map. [M4.9 finding 27]

    Single and double quotes only. Playwright's reader admits a backtick and covers the template
    titles that follow with a `::*` wildcard; nothing here writes one, and admitting the
    character without that fallback would register ids no runner answers to.
    """
    ids: set[str] = set()
    src = REPO / "frontend" / "src"
    if not src.is_dir():
        return ids
    for path in sorted(src.rglob("*.test.js")):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:it|test)\(\s*(['\"])(.+?)\1", text, re.M | re.S):
            ids.add(f"{rel}::{match.group(2)}")
    return ids


KNOWN_TESTS = _pytest_ids() | _playwright_ids() | _vitest_ids()


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


def _report_lines() -> list[str]:
    """One line per milestone, as the report prints them.

    A function rather than a block inside `test_report` because the ledger guard below reads
    the same lines back out of `docs/TESTING.md`: a second copy of this formatting would let
    the document agree with a formatter nothing else uses.
    """
    by_milestone: dict[str, list[dict]] = {}
    for r in REQUIREMENTS:
        by_milestone.setdefault(r["milestone"], []).append(r)

    lines = []
    for milestone in MILESTONES:
        rows = by_milestone.get(milestone, [])
        covered = sum(1 for r in rows if r.get("tests"))
        waived = sum(1 for r in rows if r.get("waived") and not r.get("tests"))
        # ASCII markers: this prints to whatever console the developer has, and a Windows
        # cp1252 terminal turns a decorative glyph into a crash.
        marker = ">" if _at_or_before(milestone) else " "
        note = f" ({waived} waived)" if waived else ""
        lines.append(f"  {marker} {milestone}  {covered:>3}/{len(rows):<3} covered{note}")
    return lines


def test_report(capsys):
    """Not an assertion - the map's current state, printed with -s so it is readable."""
    with capsys.disabled():
        print("\n".join([f"\nspec coverage - current milestone {CURRENT}", "", *_report_lines()]))


def test_the_testing_ledger_publishes_the_counts_the_gate_prints():
    """`docs/TESTING.md`'s "Current state" block is this report, or it is a number nobody ran.

    CLAUDE.md sends readers to that file for milestone status "rather than assuming status", so
    a count published there is read as measured. M4.8's own block said `M4.8 9/9` while this
    file printed `10/10`: it was pasted from a run made before review cycle 1 added the tenth
    row, and nothing compared the two -- so a later milestone auditing that no milestone had
    lost a test would have reconciled against a baseline one row low, and deleting the tenth
    row would have made the document true. Decision 184 refuses to invent a number in that
    ledger; this refuses to leave one there that a run has since overtaken.
    [M4.8 review cycle 2: m48-rev2-testing-ledger-publishes-a-count-the-instrument-does-not-print]
    """
    block = re.search(
        r"### Current state\s*\n+```\n(.*?)\n```", LEDGER.read_text(encoding="utf-8"), re.S
    )
    assert block, "docs/TESTING.md has no `### Current state` block for the ledger to publish"
    published = block.group(1).splitlines()
    # The block is the report with the two-space indent stripped, which is how it is pasted.
    printed = [line.removeprefix("  ") for line in _report_lines()]
    drift = [
        f"  line {i + 1}: published {p!r}, printed {q!r}"
        for i, (p, q) in enumerate(zip_longest(published, printed))
        if p != q
    ]
    assert not drift, (
        "docs/TESTING.md's ledger no longer matches this file's report. Re-paste the block "
        "`pytest backend/tests/test_spec_coverage.py -q -s` prints:\n" + "\n".join(drift)
    )
