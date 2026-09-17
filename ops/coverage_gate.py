"""The executed-coverage gate: whether the tests a row names actually RAN.

`backend/tests/spec_coverage.toml` is the contract and `test_spec_coverage.py` is its judge, but
the judge answers two questions only -- does a row at or before `current_milestone` name a test,
and does that test EXIST. Neither asks whether the named test executed. It is not a theoretical
hole, and it is not small: the `CORPUS_BUNDLE_DIR` family closes 3 rows -- two tests, one of
which (`test_bundle_validation.py::test_the_validator_reports_over_a_real_bundle`) closes two
rows on its own, which is why counting tests and counting rows give different answers and why
these three figures are derived rather than remembered. The `pg_dump` family closes 3 rows,
through eight tests in `test_backup.py` and `test_restore_drill.py` that reach `_client` and
skip when neither `pg_dump` is on PATH nor a postgres:16 container publishes
TEST_DATABASE_URL's port. The `test_migrations.py` family closes 9 rows, which skip without
`backend/tests/pglite/node_modules`. On a checkout missing any of the
three, the map prints those milestones as fully covered while their evidence
asserted nothing -- the same defect one level up from the one M4.12 removed from the Tonight
suite (`test_spec_coverage.py::test_the_testing_ledger_does_not_claim_a_skip_this_milestone_did
_not_take`).

So this reads the reports the runners write, not the source they were parsed out of:

    python ops/coverage_gate.py --junit <pytest.xml> [--junit ...] --playwright <report.json>

and fails when a test named by a row at or before `current_milestone` is ABSENT from every report
it was given, or present with a result that is not an EXECUTION -- a pytest `<skipped>`, which is
also how an xfail arrives; a Playwright `skipped`; or any Playwright status outside
`EXECUTED_STATUSES`, which is where `interrupted` and every word a later reporter invents land
(decision 314). That is the whole rule; everything below is the mapping between three spellings
of a test's name.

What this gate does NOT decide is whether a row's evidence sits at the LAYER its `kind` declares:
nothing here reads `kind` at all. That half is `platform-coverage-rows-are-proven-at-their-kind`,
held by the kind guard in `backend/tests/test_spec_coverage.py`, and the one-sentence criterion
-- "counts only rows whose named tests actually executed at or above the row's declared kind" --
resolves to the two instruments together. The split is deliberate and the owners differ on
purpose: this one cannot live inside the suite it audits, that one cannot live outside it.
[decision 312]

**It is a release-workflow leg and not a pytest test, and that is deliberate.** A check on whether
the suite ran cannot live inside the suite: the run that skipped the evidence would skip the check
with it. `.github/workflows/release.yml` runs it as leg 2 of 5, over leg 1's JUnit and over the
browser report `ci.yml` uploaded for the same commit. [§12 exit criteria; docs/TESTING.md, "The
coverage map", rule 2; `platform-coverage-counts-only-executed-tests`]

Three mappings, each of which is a fact about a runner rather than a preference:

  * **pytest.** The config lives in `backend/pyproject.toml`, so pytest's rootdir is `backend/`
    and every node id in the JUnit is relative to it: `classname="tests.test_x"`,
    `name="test_y[param]"`. The map spells the same test `backend/tests/test_x.py::test_y`. A
    `file` attribute is preferred WHERE A REPORT CARRIES ONE, because a test inside a class puts
    the class name in `classname` and nothing in `file` -- but pytest's default
    `junit_family = xunit2` allows only `classname`, `name` and `time` on a `<testcase>` and
    `backend/pyproject.toml` pins no family, so no report this repository writes carries it and
    `_module_from_classname` is the live path rather than the fallback.
    [M4.16 cycle 2, M416-C2-GATE-01]
  * **Playwright.** The JSON reporter nests `suites[].specs[].title` with `suites[].suites[]` for
    each `describe`, and a spec's `file` is relative to a root that has moved between Playwright
    versions. `_playwright_ids` in `test_spec_coverage.py` builds the map's ids out of the spec
    FILE and the `test(...)` title with no describe prefix, so this reads the same two things and
    anchors the path on the basename: `e2e/playwright.config.js` sets `testDir: './specs'` and
    every spec file sits directly in it, so the basename is the part of the path that cannot be
    wrong. A spec that ran under two projects counts as executed when either project ran it --
    which project proved a row is `platform-coverage-rows-are-proven-at-their-kind`'s question,
    not this one's.
  * **vitest.** The frontend suite writes no report this gate is given, and 47 named ids live in
    `frontend/src`. Decision 226 admits a vitest id BESIDE a backend or Playwright one and never
    instead of one, so every row carrying one also carries evidence this gate can see; those ids
    are counted and listed rather than silently passed over. A row whose named tests are ALL
    invisible here is a row this gate cannot speak for, and it fails rather than passing quietly.

The milestone order is read out of `backend/tests/test_spec_coverage.py`'s own `MILESTONES` list
rather than restated here. That list is AUTHORED, NOT SORTED -- a string sort puts "M4.10" before
"M4.5" and re-dates every row -- and a second copy of it in this file would be a second thing to
keep true, which is the failure mode this whole milestone exists to close.

Output is ASCII and stays inside 108 columns: this prints to whatever console the operator has,
and a Windows cp1252 terminal crashes on a decorative glyph.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "backend" / "tests" / "spec_coverage.toml"
JUDGE = ROOT / "backend" / "tests" / "test_spec_coverage.py"

# Where each runner's ids live, and therefore which report can answer for them. A prefix rather
# than the runner's name, because the map spells every id as a repo-relative path and the path is
# what says which suite owns it.
PLAYWRIGHT_PREFIX = "e2e/specs/"
VITEST_PREFIX = "frontend/"

# A JUnit `<testcase>` carrying one of these children did not run its body. `xfail` arrives as a
# `<skipped type="pytest.xfail">` and is the same fact for this gate's purpose: the assertions
# were not evaluated. A `<failure>` or an `<error>`, by contrast, IS an execution -- the suite
# that produced it has already failed leg 1, and reporting it here as well would diagnose one
# defect twice.
SKIPPED = "skipped"

# Playwright's own words for a result that RAN, and the only words this file reads as one. A
# report whose status is `interrupted` -- the run cancelled, the worker killed, a shutdown
# mid-file -- or a word no version of the reporter has taught this gate is ABSENT EVIDENCE, and
# absent is the polarity it must take, the same one the two branches in `_walk_playwright` below
# already take for a registered spec with nothing under it and for a test with no results. The
# default for an unknown word cannot be "this ran" in the one instrument whose entire subject is
# that absent evidence must not read as proof, and the reporter that supplies those words is a
# caret-ranged dependency whose vocabulary nothing here pins. `failed` and `timedOut` stay
# executed for the reason the `<failure>`/`<error>` note above gives for pytest: they ran, and
# leg 1 or the browser leg has already failed on them, so refusing them here would diagnose one
# defect twice. [decision 314; M4.16 cycle 4, M416-C4-GATE-01]
EXECUTED_STATUSES = frozenset({"passed", "failed", "timedOut"})


def console(text: str) -> str:
    """One line of output, safe for whatever console the operator actually has.

    The docstring above promises ASCII, and the green path keeps that promise because it prints
    only literals. The FAILURE path prints DATA: every failure line interpolates a test id taken
    straight out of `spec_coverage.toml`, and two of those ids carry a section mark and a right
    arrow. `test_no_console_output_leaves_the_oem_code_page` cannot see it -- it reads string
    LITERALS out of this file, and the character arrives at run time from the map -- so the first
    hand-run of this gate on the box the corpus lives on printed 45 of 141 rows and then a
    `UnicodeEncodeError` raised from the `print` it was reporting from. The exit code stayed 1, so
    red never became green; what was lost was the half that says WHICH requirement is uncovered,
    which is the whole reason the row is named rather than counted.

    Escaped rather than dropped or replaced, for the reason `_not_encodable` gives about itself
    and six `ops/*_exit_criterion.py` scripts give about text they did not author: `\\u2192` still
    names the test a maintainer has to go and look at. [M4.16 cycle 1, CG-02]
    """
    return text.encode("ascii", "backslashreplace").decode("ascii")


def authored_milestones() -> list[str]:
    """`MILESTONES` as `test_spec_coverage.py` authors it, in order, read with `ast`.

    Parsed rather than imported: importing the judge pulls in pytest and the whole `tests`
    package for one list, and this script runs in a workflow leg that has the venv but no
    collected suite. Parsed rather than regexed because the list wraps across four lines.
    """
    tree = ast.parse(JUDGE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "MILESTONES" for t in node.targets):
            continue
        return [str(v) for v in ast.literal_eval(node.value)]
    raise SystemExit(f"no MILESTONES list in {JUDGE}: this gate cannot order the map without one")


def _repo_path(raw: str) -> str:
    """A path out of a runner's report, spelled the way the map spells it.

    pytest's rootdir is `backend/` (the config is in `backend/pyproject.toml`), so every path it
    reports is relative to that directory and every path the map holds is relative to the
    repository. Rewriting the one prefix that can occur is exact; guessing by probing the
    filesystem would make the gate's answer depend on the checkout it runs in.
    """
    path = raw.replace("\\", "/").removeprefix("./")
    if path.startswith("tests/"):
        return "backend/" + path
    return path


def _module_from_classname(classname: str) -> str:
    """`tests.test_x` -> `backend/tests/test_x.py`, dropping a class segment if there is one.

    Not a fallback in practice but the LIVE path: `xunit2`, pytest's default family and this
    repository's, writes no `file` attribute at all, so every report leg 2 is handed arrives here.
    A test defined inside a class arrives as `tests.test_x.TestThing`, and the convention that
    tells the two apart is the one pytest's own collector uses: modules are files named
    `test_*.py`, classes are `Test*` in CamelCase. [M4.16 cycle 2, M416-C2-GATE-01]
    """
    parts = classname.split(".")
    while parts and parts[-1][:1].isupper():
        parts.pop()
    return _repo_path("/".join(parts) + ".py") if parts else ""


def junit_outcomes(paths: list[Path]) -> dict[str, set[str]]:
    """`backend/tests/test_x.py::test_y` -> the outcomes every report gave it.

    A set rather than one value because a test can appear in several reports -- the release
    workflow feeds this the suite's JUnit and could feed it a second leg's -- and because a
    parameterised test contributes one entry per case. Executed once anywhere is executed.
    """
    out: dict[str, set[str]] = {}
    for path in paths:
        root = ElementTree.parse(path).getroot()
        for case in root.iter("testcase"):
            reported = case.get("file") or ""
            module = _repo_path(reported) if reported else _module_from_classname(
                case.get("classname") or ""
            )
            name = (case.get("name") or "").partition("[")[0]
            if not module or not name:
                continue
            outcome = SKIPPED if case.find("skipped") is not None else "executed"
            out.setdefault(f"{module}::{name}", set()).add(outcome)
    return out


def _walk_playwright(suite: dict, inherited: str, out: dict[str, set[str]]) -> None:
    """One suite of the JSON report, and every `describe` nested under it.

    The describe title is deliberately not part of the id: `_playwright_ids` builds the map's ids
    by regexing `test(...)` calls out of the spec source, where a describe wrapper is invisible,
    so a gate that prefixed one would report every grouped test as absent.
    """
    where = suite.get("file") or inherited
    for spec in suite.get("specs", []):
        spec_file = spec.get("file") or where
        title = spec.get("title")
        if not spec_file or not title:
            continue
        key = PLAYWRIGHT_PREFIX + Path(str(spec_file)).name
        outcomes = out.setdefault(f"{key}::{title}", set())
        tests = spec.get("tests") or []
        if not tests:
            # The same absence as the branch four lines down, one level up -- a spec the reporter
            # registered and under which nothing ran at all. Registering the id with an EMPTY set
            # and stopping there made `_resolve` answer with something that is neither `None` nor
            # `{SKIPPED}`, so `check()` fell past both failure arms and COUNTED it: absent
            # evidence read as proof, in the one instrument whose entire subject is that it must
            # not be. The wildcard arm already answers this correctly (`return seen or None`) and
            # the direct arm is the one every live row uses, so this was the only reachable half.
            # It costs nothing when the reporter behaves: 1.62.1's `_serializeTestSpec` always
            # emits one entry, and a guard that only holds while a third-party reporter keeps a
            # shape nothing here asserts is a version pin rather than a rule.
            # [M4.16 cycle 1, CG-01]
            outcomes.add(SKIPPED)
        for test in tests:
            results = test.get("results") or []
            if not results:
                # A test Playwright never started -- filtered out by a project's `testMatch`, or
                # the run stopped before it -- is absent evidence, and absent is what it must read
                # as. `status` on the test is the AGGREGATE ("expected"/"skipped"), which would
                # read a never-run test as a decided one.
                outcomes.add(SKIPPED)
            for result in results:
                # Membership of a known set, not inequality with one word. The rule this replaced
                # read every string that was not literally "skipped" as an execution, so a
                # cancelled run and a status invented by a later reporter both closed the row.
                # [decision 314]
                status = str(result.get("status") or SKIPPED)
                outcomes.add("executed" if status in EXECUTED_STATUSES else SKIPPED)
    for child in suite.get("suites", []):
        _walk_playwright(child, where, out)


def playwright_outcomes(paths: list[Path]) -> dict[str, set[str]]:
    """`e2e/specs/NN-x.spec.js::title` -> the outcomes every report gave it."""
    out: dict[str, set[str]] = {}
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        for suite in report.get("suites", []):
            _walk_playwright(suite, "", out)
    return out


def _resolve(test_id: str, outcomes: dict[str, set[str]]) -> set[str] | None:
    """What a report says about one named test, or None when it says nothing at all.

    The `path::*` wildcard is the map's spelling for a parameterised Playwright loop
    (`05-milestones.spec.js` builds its titles from a template, which `_playwright_ids` registers
    as a file-level wildcard). It is satisfied by any test from that file, because the whole point
    of the wildcard is that no individual title can be named.
    """
    if test_id in outcomes:
        return outcomes[test_id]
    path, _, title = test_id.partition("::")
    if title != "*":
        return None
    seen = {o for key, value in outcomes.items() if key.startswith(f"{path}::") for o in value}
    return seen or None


def check(reports: dict[str, set[str]]) -> tuple[list[tuple[str, str]], list[str], int]:
    """The rule, over every row at or before `current_milestone`.

    Returns one (row id, line) pair per piece of evidence that did not run, the ids no report this
    gate was given can answer for, and the number of (row, named test) claims it did confirm.

    The row id travels BESIDE the line rather than only inside it, because the two are not the
    same count and the summary printed one as the other: a row naming six unrun tests is one
    uncovered requirement and six lines, and `len(failures)` under the noun "row(s)" reported
    more uncovered requirements than the map has rows at all. MEASURED on this branch on
    2026-09-17, over the scoped run `docs/RELEASE.md` section 2.1 records: 2,090 lines across 306
    rows, out of 334 rows in the map and 309 at or before `current_milestone`. Nor is that the
    pathological case -- 295 of those 309 name more than one visible test, so every ordinary
    partial report multiplies. The detail lines were each right and the headline above them was
    not, which is the shape of wrongness an operator believes.
    [M4.16 cycle 4, M416-C4-GATE-05]
    """
    coverage = tomllib.loads(MAP.read_text(encoding="utf-8"))
    order = authored_milestones()
    current = coverage["current_milestone"]
    if current not in order:
        raise SystemExit(f"current_milestone {current!r} is not in {JUDGE.name}'s MILESTONES list")

    failures: list[tuple[str, str]] = []
    invisible: list[str] = []
    confirmed = 0
    for row in coverage["requirement"]:
        milestone = row["milestone"]
        if milestone not in order or order.index(milestone) > order.index(current):
            continue
        named = row.get("tests") or []
        visible = [t for t in named if not t.startswith(VITEST_PREFIX)]
        invisible += [t for t in named if t.startswith(VITEST_PREFIX)]
        if named and not visible:
            failures.append((
                row["id"],
                f"{row['id']} ({milestone}) names only ids no report here can answer for: "
                + ", ".join(named),
            ))
            continue
        for test_id in visible:
            outcomes = _resolve(test_id, reports)
            if outcomes is None:
                failures.append((
                    row["id"],
                    f"{row['id']} ({milestone}) is closed by a test no report ran: {test_id}",
                ))
            elif outcomes == {SKIPPED}:
                failures.append((
                    row["id"],
                    f"{row['id']} ({milestone}) is closed by a test that SKIPPED: {test_id}",
                ))
            else:
                confirmed += 1
    return failures, invisible, confirmed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit", action="append", default=[], type=Path,
                        help="a pytest --junitxml report; repeatable")
    parser.add_argument("--playwright", action="append", default=[], type=Path,
                        help="a Playwright JSON report; repeatable")
    args = parser.parse_args(argv)

    if not args.junit and not args.playwright:
        # Not an empty pass. A gate handed nothing would confirm nothing and exit 0, which is the
        # shape of failure it exists to refuse.
        print("coverage gate: no reports given; pass --junit and --playwright", file=sys.stderr)
        return 2
    for path in [*args.junit, *args.playwright]:
        if not path.is_file():
            print(f"coverage gate: no such report: {path}", file=sys.stderr)
            return 2

    reports = junit_outcomes(list(args.junit))
    reports.update(playwright_outcomes(list(args.playwright)))

    failures, invisible, confirmed = check(reports)
    print(f"coverage gate: {len(reports)} test result(s) read from "
          f"{len(args.junit)} JUnit and {len(args.playwright)} Playwright report(s)")
    # A (row, named test) PAIR and not a test: one test closes several rows across this map, so
    # this is how many claims were proven rather than how many functions ran. Spelled that way
    # since review cycle 5, because `confirmed` and `len(failures)` count the same kind of thing
    # and printing one as "test(s)" and the other as "row(s)" meant at most one could be right.
    # [M4.16 cycle 4, M416-C4-GATE-05]
    print(f"coverage gate: {confirmed} row-and-test pair(s) confirmed executed")
    if invisible:
        print(f"coverage gate: {len(invisible)} named vitest id(s) not visible here "
              "(decision 226: no row rests on one alone)")
    if not failures:
        print("coverage gate: every row at or before current_milestone ran the tests it names")
        return 0
    print("")
    # The rows are the headline and the lines are the detail, because they are different numbers
    # and only one of them is a count of uncovered requirements. [M4.16 cycle 4, M416-C4-GATE-05]
    rows = {row for row, _ in failures}
    print(f"coverage gate: {len(rows)} row(s) name evidence that did not run, "
          f"in {len(failures)} line(s):")
    for _, failure in failures:
        print(f"  {console(failure)}")
    print("")
    print("A row is covered when its tests RAN. Un-skip the test, or move the row's milestone;")
    print("never close it by renaming the test or by lowering current_milestone.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
