"""The instrument that decides whether this build ships, held to its own contract.

Three things live here, and they are one subject: nothing in this repository could answer the
question "is what exists today releasable?". `current_milestone` was the only lever the build had,
and raising it answers whether the MAP is covered -- never whether the evidence behind it ran, and
never whether the four things §12's criteria are actually about (a real bundle, a real dump on a
real stack, a built image, a real phone) still work.

  1. `ops/coverage_gate.py`, fed synthetic reports built out of the live map. Rule 2 of the
     coverage map asks whether a named test EXISTS; the gate asks whether it RAN, which is the
     half `test_spec_coverage.py` structurally cannot ask -- a check on whether the suite ran
     cannot live inside the suite. The `CORPUS_BUNDLE_DIR` family closes 3 rows, the `pg_dump`
     family closes 3 rows and the `test_migrations.py` family closes 9 rows, every one of
     which reads as covered on the wrong checkout while its evidence asserted nothing. The
     three figures are a dated reading (decision 460), not counts a guard keeps current.
  2. `.github/workflows/release.yml`, read as text. Its five legs run in order, each uploads an
     artifact, none carries `continue-on-error`, and no leg can pass by skipping its whole body.
     This file is the only thing that can hold those properties: no test anywhere can observe a
     GitHub run, which is exactly why a workflow guard that has never been shown failing reads as
     coverage and is not.
  3. `e2e/run.mjs`'s fixture rebuild (decision 299). Only CI ever rebuilt `data/import`; the
     local harness measured whatever the directory happened to hold, and a stale fixture cost one
     session six round trips. An instrument that can silently measure the wrong input is this
     milestone's subject at the harness's own altitude.

Every guard here is paired with a synthetic violation, in the idiom `test_harness_contracts.py`
established for the CI guards and for the same reason: these run on every machine and can never
observe the thing they are about, so being shown failing is the whole of their standing.

[§12 exit criteria; docs/TESTING.md "The coverage map"; decisions 299 and 300;
`platform-coverage-counts-only-executed-tests`, `platform-release-gate-runs-every-leg`,
`platform-the-browser-harness-rebuilds-the-fixture-it-measures`]
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

from tests.test_harness_contracts import _jobs, _nested, _read, _span

# `.dockerignore`'s own reader, imported rather than re-spelled: the rule at the bottom of this
# file is about the same file the build-context guards there hold, and two parsers of one ignore
# file is two ways to disagree about what it excludes. [M4.16 cycle 5, M416-C5-DOCKER-01]
from tests.test_static_contracts import _ignored

REPO = Path(__file__).resolve().parents[2]
RELEASE = REPO / ".github" / "workflows" / "release.yml"
# Read by two guards a long way apart -- the retention rule at the bottom of this file, and the
# published-count rule above it since review cycle 4 -- so the path is spelled once here rather
# than twice where it is used. [M4.16 cycle 4, M416-C4-CI-02]
CI = REPO / ".github" / "workflows" / "ci.yml"
RUNNER = REPO / "e2e" / "run.mjs"
MAP = REPO / "backend" / "tests" / "spec_coverage.toml"

# `ops/` is not a package and is not importable from the suite's own path, which is why every
# other guard over a script in it reads the file rather than importing it. This one has to CALL
# the gate -- the property is its exit code, not its text -- so the directory goes on the path
# once, the way `ops/m45_exit_criterion.py` puts `backend/` on its own.
sys.path.insert(0, str(REPO / "ops"))

import coverage_gate  # noqa: E402

# --- the executed-coverage gate ---------------------------------------------------------


def _live_ids() -> tuple[list[str], list[str]]:
    """The pytest and Playwright ids every row at or before `current_milestone` names.

    Built out of the live map rather than out of a fixture, because the gate's subject IS the live
    map: a fixture would let the two drift, and drift between a record and the thing it records is
    the defect class this milestone exists to close.
    """
    coverage = tomllib.loads(MAP.read_text(encoding="utf-8"))
    order = coverage_gate.authored_milestones()
    current = order.index(coverage["current_milestone"])
    pytest_ids: list[str] = []
    playwright_ids: list[str] = []
    for row in coverage["requirement"]:
        if order.index(row["milestone"]) > current:
            continue
        for test_id in row.get("tests", []):
            if test_id.startswith("backend/tests/"):
                pytest_ids.append(test_id)
            elif test_id.startswith("e2e/specs/"):
                playwright_ids.append(test_id)
    return sorted(set(pytest_ids)), sorted(set(playwright_ids))


def _row_that_names(test_id: str) -> str:
    coverage = tomllib.loads(MAP.read_text(encoding="utf-8"))
    return next(r["id"] for r in coverage["requirement"] if test_id in (r.get("tests") or []))


def _withheld(absent: str | list[str] | None) -> frozenset[str]:
    """The ids a fixture leaves out of the report, as a set.

    A LIST since review cycle 5, and the reason is a count rather than a convenience: every
    fixture here could withhold exactly one id, so no report this file can build ever made one
    row fail twice -- and the summary line that counts rows was printing the number of LINES.
    A defect a fixture is structurally unable to produce is a defect no assertion can catch.
    [M4.16 cycle 4, M416-C4-GATE-05]
    """
    if absent is None:
        return frozenset()
    return frozenset([absent] if isinstance(absent, str) else absent)


def _junit(ids: list[str], *, skipped: str | None = None,
           absent: str | list[str] | None = None,
           file_attribute: bool = False, skip_type: str = "pytest.skip",
           errored: str | None = None) -> str:
    """A pytest `--junitxml` report, spelled the way THIS repository's pytest spells one.

    The spelling is the point of the fixture. pytest's rootdir is `backend/` (the config is in
    `backend/pyproject.toml`), so `classname` arrives as `tests.test_x` while the map says
    `backend/tests/test_x.py::test_y`. A gate written against the map's spelling and fed the
    runner's reports every row as absent.

    AND NO `file` ATTRIBUTE BY DEFAULT, since review cycle 2. pytest's default `junit_family` is
    `xunit2`, whose `<testcase>` allow-list is `classname`, `name` and `time`: the `file` key is
    built and then filtered out. `backend/pyproject.toml` pins no family and neither workflow
    passes one, so every JUnit leg 1 can produce lacks it and `_module_from_classname` is the only
    branch the release gate ever takes -- while every gate test here carried `file=` and proved
    the other one. The fixture models the runner; `file_attribute=True` keeps a case for the
    report that does carry it, which is the more exact answer when it is there.
    [M4.16 cycle 2, M416-C2-GATE-01]

    AND THE OTHER TWO ELEMENTS PYTEST WRITES, since review cycle 5. The gate's `SKIPPED` comment
    makes two positive rulings -- an `xfail` arrives as `<skipped type="pytest.xfail">` and is the
    same fact for this gate, and a `<failure>` or an `<error>` IS an execution because reporting it
    here would diagnose one defect twice -- and the fixture could write neither, so both rulings
    were held by the comment alone. `skip_type` writes the second spelling of the first element;
    `errored` writes the setup/teardown/collection failure pytest reports as `<error>`, which is
    the live half: nothing in this suite xfails today, and an error does happen.
    [decision 314; M4.16 cycle 5, M416-C5-GATE-02]
    """
    cases = []
    withheld = _withheld(absent)
    for test_id in ids:
        if test_id in withheld:
            continue
        path, _, name = test_id.partition("::")
        inside = path.removeprefix("backend/")
        classname = inside.removesuffix(".py").replace("/", ".")
        body = f'<skipped type="{skip_type}" message="a reason"/>' if test_id == skipped else ""
        if test_id == errored:
            body = '<error message="failed on setup">a fixture raised</error>'
        where = f' file="{inside}"' if file_attribute else ""
        cases.append(f'<testcase classname="{classname}" name="{name}"{where}>'
                     f"{body}</testcase>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="pytest">'
            + "".join(cases) + "</testsuite></testsuites>\n")


def _playwright(ids: list[str], *, skipped: str | None = None,
                absent: str | list[str] | None = None,
                empty: str | None = None, status: tuple[str, list[str]] | None = None) -> str:
    """A Playwright JSON report: `suites[].specs[].title`, `file` relative to the reporter's root
    rather than to the repository, and one `results` entry per project that ran the spec.

    `empty` is the third shape, and it is the one the fixture could not previously say: a spec
    the reporter REGISTERED with no `tests` under it at all. 1.62.1 does not emit it, which is
    exactly why the branch went unexercised while reading as covered.

    `status` is the fourth, and the one cycle 4 found reading as PROOF: `(test_id, [words])`
    spells one victim's results verbatim, so a case can say `interrupted`, a word the reporter's
    vocabulary does not hold today, or the PAIR a cancelled two-project run writes -- one project
    interrupted, the other never reached. A list rather than a word because that compound is the
    reachable shape and it resolved to neither `None` nor `{SKIPPED}`.
    [decision 314; M4.16 cycle 4, M416-C4-GATE-01]
    """
    files: dict[str, list[dict]] = {}
    victim, words = status or (None, [])
    withheld = _withheld(absent)
    for test_id in ids:
        if test_id in withheld:
            continue
        path, _, title = test_id.partition("::")
        where = path.removeprefix("e2e/")
        if test_id == victim:
            results = [{"status": word} for word in words]
        else:
            results = [{"status": "skipped" if test_id == skipped else "passed"}]
        tests = [] if test_id == empty else [{"results": results}]
        files.setdefault(where, []).append(
            {"title": title, "file": where, "tests": tests}
        )
    return json.dumps(
        {"suites": [{"title": name, "file": name, "specs": specs, "suites": []}
                    for name, specs in files.items()]}
    )


@pytest.fixture
def reports(tmp_path: Path):
    """The two report files, and a way to rewrite either with one test's outcome changed."""
    pytest_ids, playwright_ids = _live_ids()

    def write(*, skipped: str | None = None, absent: str | list[str] | None = None,
              empty: str | None = None, status: tuple[str, list[str]] | None = None,
              skip_type: str = "pytest.skip", errored: str | None = None) -> list[str]:
        junit = tmp_path / "junit.xml"
        browser = tmp_path / "report.json"
        junit.write_text(
            _junit(pytest_ids, skipped=skipped, absent=absent, skip_type=skip_type,
                   errored=errored),
            encoding="utf-8",
        )
        browser.write_text(
            _playwright(playwright_ids, skipped=skipped, absent=absent, empty=empty,
                        status=status),
            encoding="utf-8",
        )
        return ["--junit", str(junit), "--playwright", str(browser)]

    write.pytest_ids = pytest_ids
    write.playwright_ids = playwright_ids
    return write


def test_the_gate_passes_when_every_named_test_ran(reports, capsys):
    """The direction that keeps the three below from being vacuous: over reports in which every
    named test executed, the gate exits 0 and says how many it confirmed."""
    assert coverage_gate.main(reports()) == 0
    printed = capsys.readouterr().out
    assert "every row at or before current_milestone ran the tests it names" in printed
    # Per confirmation, not per test: one test closes several rows across this map, and the count
    # a maintainer wants is how many CLAIMS were proven rather than how many functions ran. The
    # LINE says that since review cycle 5. It read "named test(s)" over the same (row, test) pair
    # count the failure summary two lines down printed as "row(s)", so at most one of those two
    # nouns could be right and a reader had no way to tell which. [M4.16 cycle 4, M416-C4-GATE-05]
    confirmed = int(re.search(r"(\d+) row-and-test pair\(s\) confirmed", printed).group(1))
    distinct = len(reports.pytest_ids) + len(reports.playwright_ids)
    assert confirmed >= distinct, (printed, distinct)


@pytest.mark.parametrize("skip_type", ["pytest.skip", "pytest.xfail"])
def test_the_gate_names_the_row_whose_named_test_skipped(reports, capsys, skip_type):
    """§12's exit criterion, in one assertion: fed a synthetic report in which one named test is
    `skipped`, the gate exits non-zero AND names the row.

    Naming the row is the half that makes it actionable. A gate that said "some evidence did not
    run" would send a maintainer through 1,800 ids; the row is what says which requirement is
    uncovered, and therefore which milestone's claim is wrong.

    The victim is chosen off the map rather than hard-coded, so this keeps working when the rows
    move and never pins a test another milestone is free to rename.

    BOTH SPELLINGS OF THE ELEMENT, since review cycle 5. The gate reads `case.find("skipped")` and
    never the `type` attribute, and its own comment rules that an `xfail` -- which pytest writes as
    `<skipped type="pytest.xfail"/>` -- "is the same fact for this gate's purpose: the assertions
    were not evaluated". That ruling was held by the comment alone: the fixture wrote one spelling,
    so narrowing the read to `pytest.skip` would have turned every xfailed test in the suite into
    evidence with nothing in this file noticing. The `type` is on the element for a reason, which
    is exactly why the tightening is plausible. [decision 314; M4.16 cycle 5, M416-C5-GATE-02]
    """
    victim = reports.pytest_ids[0]
    assert coverage_gate.main(reports(skipped=victim, skip_type=skip_type)) != 0
    printed = capsys.readouterr().out
    assert "SKIPPED" in printed and victim in printed
    assert _row_that_names(victim) in printed


def test_the_gate_names_the_row_whose_named_test_never_ran(reports, capsys):
    """The other half of the same rule, and the one the corpus tests would hit first: a test that
    is absent from every report is not evidence either. `skipif` produces a `<skipped>` element;
    a spec filtered out by a project's `testMatch`, a file deselected by `-k`, or a run that ended
    early produce nothing at all, and a gate that only read the first spelling would pass a report
    of one testcase."""
    victim = reports.playwright_ids[0]
    assert coverage_gate.main(reports(absent=victim)) != 0
    printed = capsys.readouterr().out
    assert "no report ran" in printed and victim in printed
    assert _row_that_names(victim) in printed


def test_the_gate_names_the_row_whose_spec_the_reporter_registered_and_never_ran(reports, capsys):
    """The third shape a report can take, and the one that read as PROOF.

    A spec entry carrying no `tests` at all registered the id with an empty outcome set, which is
    neither `None` nor `{SKIPPED}` -- so `check()` fell past both failure arms and counted the row
    confirmed. Fed a whole report in that shape the gate printed "every row at or before
    current_milestone ran the tests it names" and exited 0 over a run in which no browser test had
    executed, which is the exact answer it exists to refuse.

    It is not reachable from Playwright 1.62.1, and that is the point rather than a defence: what
    stood between this gate and a false green was a third-party reporter's current internals, and
    nothing in this repository asserts them. The gate already fails CLOSED on the other schema
    drift it knows about -- a spec path whose basename it does not recognise resolves to `None` --
    so failing OPEN on this one was an asymmetry inside one file. [M4.16 cycle 1, CG-01]
    """
    victim = reports.playwright_ids[0]
    assert coverage_gate.main(reports(empty=victim)) != 0
    printed = capsys.readouterr().out
    assert "SKIPPED" in printed and coverage_gate.console(victim) in printed
    assert _row_that_names(victim) in printed


@pytest.mark.parametrize("words", [
    ["interrupted"],
    ["did-not-run"],
    ["interrupted", "skipped"],
])
def test_the_gate_names_the_row_whose_spec_came_back_under_a_status_that_is_not_an_execution(
        reports, capsys, words):
    """The fourth shape, and the second one to read as PROOF: every word but `skipped`.

    The rule was `SKIPPED if status == SKIPPED else "executed"` -- inequality with one word rather
    than membership of a known set -- so `interrupted` (the run cancelled, the worker killed, a
    shutdown mid-file) and any status a later reporter invents both closed the row. The third case
    is the compound a cancelled two-project run actually writes: desktop `interrupted`, phone
    never reached, which resolved to `{"executed", "skipped"}` and fell past BOTH of `check()`'s
    failure arms exactly as the empty-spec shape above did.

    The unrecognised half is the live one and it is unbounded: `e2e/package.json` pins the
    reporter at `^1.49.0`, so the vocabulary this gate's default depended on is a caret range,
    and the file's own CG-01 comment already rules that a guard holding only while a third-party
    reporter keeps a shape nothing here asserts is a version pin rather than a rule. The other
    direction is `test_the_gate_passes_when_every_named_test_ran`, whose reports are all `passed`;
    `failed` and `timedOut` stay executions on purpose, because leg 1 or the browser leg has
    already failed on them. [decision 314; M4.16 cycle 4, M416-C4-GATE-01]
    """
    victim = reports.playwright_ids[0]
    assert coverage_gate.main(reports(status=(victim, words))) != 0
    printed = capsys.readouterr().out
    assert "SKIPPED" in printed and coverage_gate.console(victim) in printed
    assert _row_that_names(victim) in printed


@pytest.mark.parametrize("word", ["failed", "timedOut"])
def test_the_gate_still_counts_a_spec_that_ran_and_lost(reports, word):
    """The other direction of the same rule, so the repair above cannot be satisfied by refusing
    everything.

    A narrowing that also swallowed `failed` and `timedOut` would be the same defect mirrored: it
    would report a browser suite that RAN as evidence that did not, and send a maintainer looking
    for a skip in a suite whose real problem leg 1 has already named. Decision 314 keeps both as
    executions for that reason, and this is the case that holds it -- one green assertion against
    the temptation to tighten one more word. [decision 314]
    """
    assert coverage_gate.main(reports(status=(reports.playwright_ids[0], [word]))) == 0


def test_the_gate_still_counts_a_pytest_case_that_errored(reports):
    """The same green assertion on the JUnit side, which had neither direction.

    The gate's `SKIPPED` comment rules that a `<failure>` or an `<error>` IS an execution, because
    "the suite that produced it has already failed leg 1, and reporting it here as well would
    diagnose one defect twice". Nothing held that. `<error>` is the live half of the pair -- pytest
    writes it for a setup, teardown or collection failure, which happens -- and a later cycle
    tightening the read to refuse it would make leg 2 name a row as uncovered whose test ran and
    blew up, sending a maintainer to look for a skip that is not there. The mirror of
    `test_the_gate_still_counts_a_spec_that_ran_and_lost`, one runner over.
    [decision 314; M4.16 cycle 5, M416-C5-GATE-02]
    """
    assert coverage_gate.main(reports(errored=reports.pytest_ids[0])) == 0


def test_the_gate_names_a_row_without_crashing_the_console_it_prints_to(reports, capsys):
    """The failure path prints DATA, and the map's data is not all ASCII.

    Three rows at or before `current_milestone` name a Playwright title carrying a section mark or
    a right arrow. Interpolated straight into `print`, those crash any stdout Python has
    locale-encoded to cp1252 -- a Windows console, or a pipe, or `| tee`, which `release.yml`
    itself uses -- from the line the gate was reporting from: the run that found this printed 45
    rows of 141 and then a traceback. The exit code is not the casualty, the DIAGNOSIS is, and a
    gate that says "some evidence did not run" without saying which sends a maintainer through
    1,800 ids.

    Asserted over the LIVE map rather than a fixture, because the offending ids are the live map's
    and a fixture would let the two drift. [M4.16 cycle 1, CG-02]
    """
    victim = next((t for t in reports.playwright_ids if not t.isascii()), None)
    assert victim is not None, (
        "no row at or before current_milestone names a non-ASCII test id any more, so this guard "
        "has stopped being about anything: delete it with the escaping it holds, or keep both"
    )
    assert coverage_gate.main(reports(absent=victim)) != 0
    printed = capsys.readouterr().out
    assert printed.isascii(), (
        "the coverage gate printed a character a cp1252 console cannot encode; on the box the "
        "corpus lives on that is a traceback where the report should have been"
    )
    # Escaped, not dropped: the row is only actionable if the line still names the test.
    assert coverage_gate.console(victim) in printed
    assert _row_that_names(victim) in printed


def _row_named_by_several_tests_of_its_own() -> tuple[str, list[str]]:
    """A shipped row naming more than one pytest id that no other shipped row names.

    Exclusive on purpose. Withholding an id two rows share would fail both of them, and the
    property below is precisely that rows and lines are counted apart -- so the fixture has to be
    able to produce a report in which exactly ONE row fails and fails more than once.
    [M4.16 cycle 4, M416-C4-GATE-05]
    """
    coverage = tomllib.loads(MAP.read_text(encoding="utf-8"))
    order = coverage_gate.authored_milestones()
    current = order.index(coverage["current_milestone"])
    shipped = [r for r in coverage["requirement"] if order.index(r["milestone"]) <= current]
    owners: dict[str, set[str]] = {}
    for row in shipped:
        for test_id in row.get("tests", []):
            owners.setdefault(test_id, set()).add(row["id"])
    for row in shipped:
        mine = sorted({t for t in row.get("tests", [])
                       if t.startswith("backend/tests/") and owners[t] == {row["id"]}})
        if len(mine) > 1:
            return row["id"], mine
    return "", []


def test_the_gate_counts_the_rows_that_failed_and_not_the_lines_it_printed(reports, capsys):
    """The failure summary is the only sentence an operator reads when the build is refused, and
    it counted the wrong thing.

    `check()` appends once per (row, named test), so a row naming six unrun tests contributes six
    entries -- and the headline printed `len(failures)` under the noun "row(s)". Over the scoped
    JUnit decision 313 records, that reads as a count of uncovered requirements several times
    larger than the number of requirements there are: MEASURED on this branch on 2026-09-17,
    2,090 lines across 306 rows, out of 334 in the map and 309 at or before `current_milestone`.
    It is not the pathological case either -- 295 of those 309 name more than one visible test,
    so every ordinary partial report multiplies.

    The detail lines were never wrong, which is what made it believable: the number above them was
    read as a row count by its own author and written into `docs/RELEASE.md` and into decision
    313 as one. This is the case no fixture here could build until `_withheld` -- every one of
    them withheld exactly one id, so one row could never fail twice.
    [decision 184; M4.16 cycle 4, M416-C4-GATE-05]
    """
    row_id, victims = _row_named_by_several_tests_of_its_own()
    assert len(victims) > 1, (
        "no shipped row names two pytest ids of its own any more, so this guard cannot build the "
        "report it is about: the rows-versus-lines distinction it holds would go unasserted"
    )
    assert coverage_gate.main(reports(absent=victims)) != 0
    printed = capsys.readouterr().out
    summary = re.search(
        r"coverage gate: (\d+) row\(s\) name evidence that did not run, in (\d+) line\(s\)",
        printed,
    )
    assert summary is not None, (
        "the gate no longer publishes its failure summary as rows AND lines: " + printed
    )
    assert (int(summary.group(1)), int(summary.group(2))) == (1, len(victims)), (
        f"one row ({row_id}) named {len(victims)} tests no report ran, and the gate summarised "
        f"that as {summary.group(0)!r}"
    )
    assert printed.count(row_id) >= len(victims), printed


# --- M4.16 review cycle 5: decision 312's split, held to the only thing it buys -----------------
#
# The one-sentence criterion is "counts only rows whose named tests actually executed AT OR ABOVE
# THE ROW'S DECLARED KIND", and it is two claims with two owners: this gate reads no `kind` at
# all, and the kind guard cannot ask whether a test ran, because a run that skipped the evidence
# would skip the check with it. Decision 312 kept them apart and paid for the split in prose --
# each instrument names the other, in four places -- and that prose was read by nothing. The
# gate's own paragraph is sixty lines of module docstring a later maintainer has every reason to
# tighten, and deleting it leaves a criterion that resolves, for a reader starting at the gate, to
# one instrument reading in one direction, with both rows' `what` still asserting the naming in
# the present tense. A cross-reference nobody checks is the defect class this milestone exists to
# close, one altitude down from the counts above.
#
# The row id is required IN THE PARAGRAPH THAT CITES DECISION 312 and not merely in the file: it
# occurs a second time in the Playwright bullet ("which project proved a row is
# `platform-coverage-rows-are-proven-at-their-kind`'s question, not this one's"), so a rule over
# the whole file would pass the exact edit it is written to catch. Measured, as the self-test
# below.
# [decision 312; M4.16 cycle 5, M416-C5-GATE-01]
KIND_GUARD_ROW = "platform-coverage-rows-are-proven-at-their-kind"
EXECUTED_ROW = "platform-coverage-counts-only-executed-tests"


def _row_what(row_id: str) -> str:
    """One row's `what`, off the live map."""
    coverage = tomllib.loads(MAP.read_text(encoding="utf-8"))
    return next((r.get("what", "") for r in coverage["requirement"] if r["id"] == row_id), "")


def _split_problems(gate: str, executed_what: str, kind_what: str) -> list[str]:
    """Every way decision 312's two halves can stop naming each other."""
    problems = []
    paragraphs = [" ".join(part.split()) for part in gate.split("\n\n")]
    if not any(KIND_GUARD_ROW in part and "decision 312" in part for part in paragraphs):
        problems.append(
            "ops/coverage_gate.py has no paragraph naming `" + KIND_GUARD_ROW + "` and citing "
            "decision 312, so a reader starting at the gate is told the criterion resolves to one "
            "instrument reading in one direction"
        )
    if not executed_what:
        problems.append("the map holds no row `" + EXECUTED_ROW + "`, so this guard reads nothing")
    elif KIND_GUARD_ROW not in executed_what:
        problems.append(
            "`" + EXECUTED_ROW + "`'s `what` no longer names `" + KIND_GUARD_ROW + "` as the half "
            "the gate does not carry"
        )
    if not kind_what:
        problems.append("the map holds no row `" + KIND_GUARD_ROW + "`, so this guard reads half")
    elif "ops/coverage_gate.py" not in kind_what:
        problems.append(
            "`" + KIND_GUARD_ROW + "`'s `what` no longer names `ops/coverage_gate.py` as the half "
            "it does not carry"
        )
    return problems


def test_each_half_of_the_executed_at_its_kind_criterion_names_the_other():
    """Decision 312's cost, and the only thing that cost buys.

    Both sentences are true today and neither is read by any of the tests either row names, so
    the naming survives exactly as long as nobody tidies a docstring. The row that asserts it --
    "the gate reads no `kind` at all and names `platform-coverage-rows-are-proven-at-their-kind`
    as the half it does not carry" -- states a checkable property of another file in the present
    tense, which is exit criterion 2's shape: a `what` that outruns its named tests.
    [decision 312; M4.16 cycle 5, M416-C5-GATE-01]
    """
    problems = _split_problems(
        _read(REPO / "ops" / "coverage_gate.py"), _row_what(EXECUTED_ROW), _row_what(KIND_GUARD_ROW)
    )
    assert not problems, "\n  ".join(
        ["decision 312's split is only legible while each instrument names the other:", *problems]
    )


def test_the_split_guard_sees_a_docstring_tightened_and_a_clause_dropped():
    """The three edits nothing would have caught, each one a plausible tidy-up."""
    gate = _read(REPO / "ops" / "coverage_gate.py")
    executed, kind = _row_what(EXECUTED_ROW), _row_what(KIND_GUARD_ROW)
    assert _split_problems(gate, executed, kind) == [], "the guard does not pass the tree it holds"

    tightened = "\n\n".join(
        part for part in gate.split("\n\n")
        if not (KIND_GUARD_ROW in part and "decision 312" in part)
    )
    assert KIND_GUARD_ROW in tightened, (
        "the gate no longer names the kind guard anywhere outside its decision-312 paragraph, so "
        "this case has stopped proving that a rule over the whole file would pass the edit"
    )
    assert _split_problems(tightened, executed, kind), "the guard passed a tightened docstring"
    assert _split_problems(gate, executed.replace(KIND_GUARD_ROW, "some other row"), kind)
    assert _split_problems(gate, executed, kind.replace("ops/coverage_gate.py", "the gate"))


# --- M4.16 review cycle 2: the numbers this instrument publishes about itself -------------------
#
# Three sentences in three files size the hole this gate exists to cover, and all three were typed
# rather than counted: `ops/coverage_gate.py`'s docstring, this module's own docstring and
# `spec_coverage.toml`'s `why` for `platform-coverage-counts-only-executed-tests`. The map's `why`
# was wrong under every reading -- "two rows ... and two more" over three and three -- and the two
# docstrings were wrong about the corpus family for a subtler reason: they assumed one test closes
# one row, and `test_bundle_validation.py::test_the_validator_reports_over_a_real_bundle` closes
# two. A maintainer sizing the hole on a checkout with no corpus bundle and no `pg_dump` read four
# rows, which is the difference between an edge case and most of the data layer. Same failure as
# the vitest count above, same repair: derive it.
# [decision 184; M4.16 cycle 2, M416-C2-CG-04]
#
# THE TOTAL WAS THE ONE FIGURE IN THIS BLOCK THE GUARD COULD NOT SEE, and it was wrong: the
# sentence said fourteen while the three families close fifteen rows between them. Fourteen was
# 3 + 3 + 8, the arithmetic of the pre-cycle-4 migrations figure, typed into the comment whose
# closing words are "derive it" -- the third time this milestone has found that defect, in the
# sentence a maintainer reads when deciding whether provisioning a runner is worth it. The
# repair is not to edit the number. `_FAMILY_TOTAL` below reads this sentence, so it is written
# in the form that rule can match and on one line, because a phrase wrapped across two comment
# lines arrives with a `#` in the middle of it:
#
#     the three silent-skip families close 15 rows between them
#
# and that figure reddens with the next row added to any of the three. It is a UNION and not a
# sum, which is why `_skip_families` hands back row ids -- a row named by two families would
# otherwise be counted twice by the very guard that publishes the figure.
# [decision 184; M4.16 cycle 4, REL-C4-06]

# `_client` is where the pg_dump skip lives (`test_backup.py`: neither `pg_dump` on PATH nor a
# postgres:16 container publishing TEST_DATABASE_URL's port), and `test_restore_drill.py` imports
# it. It is a plain helper rather than a fixture, which is exactly why `_registered_tests_that_skip`
# one file over cannot see it: that reader walks a test's own body and its `skipif` decorator, and
# this skip is two calls deep. So the closure is walked here, over both modules at once because the
# import makes them one namespace for this purpose.
BACKUP_SKIP_MODULES = ("test_backup.py", "test_restore_drill.py")
BACKUP_SKIP_HELPER = "_client"

# The published form, in all three files: "`<family>` family closes <N> rows". A fixed clause
# rather than a loose sweep, for `_STRUCK_SURFACES`' reason one file over -- a rule over every way
# a count can be phrased is a rule over prose.
_FAMILY_SIZE = re.compile(
    r"`?(?P<family>CORPUS_BUNDLE_DIR|pg_dump|test_migrations\.py)`? family closes "
    r"(?P<count>\d+) rows"
)
# And the published form of their union, which no rule could read while it was spelled as a word.
# Held for CORRECTNESS wherever it appears and for PRESENCE in this module, which is where the
# sentence sizing the whole hole lives: the other four files size the families their own readers
# are about. [M4.16 cycle 4, REL-C4-06]
_FAMILY_TOTAL = re.compile(
    r"the three silent-skip families close (?P<count>\d+) rows between them"
)


def _reaches(name: str, nodes: dict, seen: frozenset[str] = frozenset()) -> bool:
    """Whether a function reaches `_client`, through its own body or its fixture parameters."""
    node = nodes.get(name)
    if node is None or name in seen:
        return False
    if any(isinstance(n, ast.Name) and n.id == BACKUP_SKIP_HELPER for n in ast.walk(node)):
        return True
    return any(_reaches(arg.arg, nodes, seen | {name}) for arg in node.args.args)


def _skip_families() -> dict[str, set[str]]:
    """Which shipped rows each silent-skip family closes, read off the live map.

    Row IDS rather than counts since review cycle 4, because the figure that sizes the whole
    hole is a union and a union may not be summed: a row closed by two families would be
    counted twice by the guard whose whole subject is a published count nobody re-derived.
    [decision 184; M4.16 cycle 4, REL-C4-06]
    """
    coverage = tomllib.loads(MAP.read_text(encoding="utf-8"))
    order = coverage_gate.authored_milestones()
    current = order.index(coverage["current_milestone"])
    shipped = [r for r in coverage["requirement"] if order.index(r["milestone"]) <= current]

    corpus: set[str] = set()
    nodes: dict[str, object] = {}
    backup_tests: dict[str, str] = {}
    for path in sorted((REPO / "backend" / "tests").glob("test_*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if path.name in BACKUP_SKIP_MODULES:
                nodes[node.name] = node
                if node.name.startswith("test_"):
                    backup_tests[node.name] = path.name
            for decorator in node.decorator_list:
                text = ast.unparse(decorator)
                if text.startswith("pytest.mark.skipif") and "CORPUS_BUNDLE_DIR" in text:
                    corpus.add(f"backend/tests/{path.name}::{node.name}")

    pg_dump = {
        f"backend/tests/{module}::{name}"
        for name, module in backup_tests.items()
        if _reaches(name, nodes)
    }

    def rows(predicate) -> set[str]:
        return {r["id"] for r in shipped if any(predicate(t) for t in r.get("tests", []))}

    return {
        "CORPUS_BUNDLE_DIR": rows(lambda t: t in corpus),
        "pg_dump": rows(lambda t: t in pg_dump),
        "test_migrations.py": rows(lambda t: t.startswith("backend/tests/test_migrations.py::")),
    }


# --- M4.16 review cycle 2: leg 2's input outlives the cadence that consumes it ------------------
#
# Leg 2 downloads the `playwright-report` artifact `ci.yml` uploaded FOR THIS COMMIT, and the
# lookup is `head_sha=$GITHUB_SHA&status=success&per_page=1` with no recency bound. Both workflows
# run weekly on Monday, twenty-four minutes apart, and `ci.yml` kept that artifact for seven days
# -- exactly one cadence, so the only fallback a scheduled release run can find is an artifact
# expiring the same morning. On a Monday when `ci.yml` is red the lookup falls through to the
# previous Monday's successful run and SUCCEEDS, so the step's careful "push the commit and let ci
# finish" refusal never fires; `actions/download-artifact` then fails on an expired artifact and
# the gate reports a red build for a reason that is not the build. Every artifact release.yml
# UPLOADS is kept thirty days, so the asymmetry was in the input rather than in a house convention.
# Held against the cadence rather than against a number, because the cadence is what makes a
# retention wrong. [M4.16 cycle 2, M416-C2-REL-04]
_CRON_DAY = re.compile(r"cron:\s*'[^']*\s(\S+)'")
_CONSUMED_ARTIFACT = re.compile(r"download-artifact@v\d.{0,300}?name:\s*([\w-]+)", re.S)
_RETENTION = re.compile(r"retention-days:\s*(\d+)")


def _weekly(text: str) -> bool:
    """Whether every schedule a workflow carries is weekly -- a day-of-week field that is not `*`."""
    days = _CRON_DAY.findall(text)
    return bool(days) and all(day != "*" for day in days)


def _uploads_named(ci: str, artifact: str) -> list[int]:
    """How long each `ci.yml` step uploading EXACTLY `artifact` keeps it, a step with no
    `retention-days` counting as zero.

    The name is anchored to the end of its own line rather than closed with `\\b`, because a word
    boundary sits between `t` and `-`: a search for `playwright-report` was satisfied by an upload
    renamed `playwright-report-html`, and release.yml's `download-artifact` names the whole string.
    An upload split in two -- one report for a human, one for the gate -- is how that gets written,
    and the guard would then read a step leg 2 does not consume. [M4.16 cycle 5, M416-C5-REL-03]
    """
    uploads = []
    for upload in re.finditer(rf"name:\s*{re.escape(artifact)}[ \t]*$", ci, re.M):
        step = re.split(r"\n {6}- ", ci[upload.end():], maxsplit=1)[0]
        kept = _RETENTION.search(step)
        uploads.append(int(kept.group(1)) if kept else 0)
    return uploads


def test_the_browser_report_leg_two_downloads_outlives_the_gates_own_cadence():
    """An input that expires exactly one cadence out has no margin at all.

    The failure it produces is the worst kind a gate can have: red, for a reason that is not the
    build, on the first Monday `ci.yml` happens to be red -- and red in a way that looks like the
    coverage gate refusing the commit rather than like an artifact that is no longer there.
    [M4.16 cycle 2, M416-C2-REL-04]
    """
    release, ci = _read(RELEASE), _read(CI)
    consumed = _CONSUMED_ARTIFACT.search(release)
    assert consumed, (
        "release.yml no longer downloads a named artifact, so leg 2 has stopped consuming the "
        "browser report and this guard reads nothing"
    )
    artifact = consumed.group(1)
    assert _weekly(release) and _weekly(ci), (
        "one of the two workflows no longer runs on a weekly cron, so re-read this guard against "
        f"whatever cadence replaced it: {_CRON_DAY.findall(release)} / {_CRON_DAY.findall(ci)}"
    )
    uploads = _uploads_named(ci, artifact)
    assert uploads, f"ci.yml no longer uploads `{artifact}`, which is leg 2's only input"
    assert all(days >= 14 for days in uploads), (
        f"ci.yml keeps `{artifact}` for {uploads} day(s) and release.yml asks for it once a week, "
        "so the only fallback a scheduled run can find expires the morning it looks. Leg 2's "
        "lookup takes the newest SUCCESSFUL ci run for the commit and reads no date, so a red "
        "Monday silently selects an artifact that is gone -- and the gate goes red for a reason "
        "that is not the build. Two cadences is the floor; release.yml keeps its own thirty."
    )
    # Both renames, because only one of them was refused. A name that shares the consumed one's
    # PREFIX is the reachable spelling -- an upload split in two, `playwright-report-html` for a
    # human and something else for the gate -- and it is exactly the one `\b` admitted, since a
    # word boundary sits between `t` and `-`. The retention rule above then measured a step leg 2
    # does not consume while release.yml went on downloading a name ci.yml no longer uploads, and
    # leg 2 dies in its download step: red, on the first dispatch, for a reason that is not the
    # build -- which is word for word the failure this guard exists to prevent.
    # [M4.16 cycle 5, M416-C5-REL-03]
    for renamed in (f"{artifact}-html", "browser-report"):
        mutation = ci.replace(f"name: {artifact}", f"name: {renamed}")
        assert mutation != ci, renamed
        assert not _uploads_named(mutation, artifact), renamed


def test_no_ci_job_can_be_made_unable_to_fail_the_conclusion_leg_two_selects_by():
    """Leg 2 selects a `ci.yml` run BY ITS CONCLUSION, so ci.yml's ability to conclude failure is
    part of this gate and not of that workflow alone.

    The lookup is `head_sha=$GITHUB_SHA&status=success&per_page=1`, and `continue-on-error: true`
    is the declared way to stop a failed job failing its run. One such key on the `e2e` job and
    every push concludes success with the browser suite red: the report is still uploaded (`if:
    always()`), leg 2 still selects it, and `failed` is correctly an EXECUTION here -- decision
    314 keeps it one -- so nothing downstream notices. What that costs is not leg 2's precondition
    (the suite did run, and leg 2 reports that truthfully); it is ci.yml's own e2e gate, with leg 2
    merely the last place the loss would have shown.

    `release.yml` is held at all three indents by `_ADVISORY` and `real-bundle.yml`'s corpus job by
    `test_harness_contracts.py`'s `assert "continue-on-error" not in corpus`. The workflow that
    produces leg 2's only downloaded input was held by neither, which is the same asymmetry
    between two workflows that `_ADVISORY`'s own comment calls the hole.
    [M4.16 cycle 4, M416-C4-CI-03]

    AND THE SAME ASYMMETRY ONE MECHANISM OVER, since review cycle 5. `continue-on-error` is a KEY,
    and the row's `what` is about a capability rather than about a key: two shell spellings make
    the e2e job unable to fail without writing it, and both are already in this repository --
    `|| true`, eight jobs above on `ruff format --check`, and `if ! <cmd>; then echo '::warning::';
    fi`, which `_GUARDED_HEAD`'s own comment names as the spelling reached for when a step is
    flaky. `release.yml` is held against both; `ci.yml` was held against neither, measured: both
    mutations of the shipped file passed every rule in this module while the three
    `continue-on-error` positions went red. The cost is not leg 2's precondition but ci.yml's own
    browser gate, with leg 2 the last place the loss would have shown -- decision 314 keeps
    `failed` an execution, so leg 2 would print all 119 e2e ids confirmed over a suite nothing
    failed on. [M4.16 cycle 5, M416-C5-CI-01]
    """
    problems = _ci_conclusion_problems(_read(CI))
    assert not problems, (
        "a ci.yml job can conclude success with its work failed -- and leg 2 selects a ci.yml run "
        "by that conclusion, then reads the browser report of a suite nothing failed on:\n  "
        + "\n  ".join(problems)
    )
    # The rule has to be able to refuse, at each of the three positions the key can occupy -- the
    # job's own key, a step's key written after another, and the same key written first in a step.
    for mutation in (
        "  e2e:\n    continue-on-error: true\n    runs-on: ubuntu-latest\n",
        "  e2e:\n    steps:\n      - run: node e2e/run.mjs\n        continue-on-error: true\n",
        "  e2e:\n    steps:\n      - continue-on-error: true\n        run: node e2e/run.mjs\n",
    ):
        assert _ADVISORY.search(mutation), mutation
    # And the two shell spellings, applied to the file that ships rather than to a fragment,
    # because the whole defect was that a fragment-sized rule read a fragment-sized mechanism.
    # Each is one edit to one line, and each is asserted to have LANDED before it is judged --
    # a mutation that did not apply passes every guard for the wrong reason.
    shipped = f"run: {_CI_BROWSER_COMMAND}"
    for spelling in (
        f"run: {_CI_BROWSER_COMMAND} || true",
        f"run: if ! {_CI_BROWSER_COMMAND}; then echo '::warning::the browser suite failed'; fi",
        f'run: echo "::group::{_CI_BROWSER_COMMAND}"',
    ):
        mutation = _read(CI).replace(shipped, spelling)
        assert mutation != _read(CI), spelling
        assert _ci_conclusion_problems(mutation), spelling
    # And the other direction, so the rule is about the capability and not about the layout: the
    # same command written as `      - run:`, which is how `ci.yml` spells most of its steps, is
    # still a step that runs and can fail. The baseline above is the rest of that direction --
    # `npm --prefix e2e ci || npm --prefix e2e install` is a choice between two ways of installing
    # node modules rather than a step handing its answer away, and it ships in this same job.
    inline = _read(CI).replace(f"        {shipped}", f"      - {shipped}")
    assert inline != _read(CI) and not _ci_conclusion_problems(inline)
    # And the admitted line is held to its own existence, in `COMMENT_PATH_EXCEPTIONS`'s idiom: an
    # exception that outlives the step it describes stops being an exception and becomes an
    # allow-list, waiting for the next `|| true` somebody writes with the same words.
    stale = sorted(line for line in _CI_ADVISORY_LINES if line not in _read(CI))
    assert not stale, (
        "these lines are admitted as informational and ci.yml no longer runs them, so the entry "
        "exempts nothing and waits to exempt something else: " + ", ".join(stale)
    )


def test_the_junit_fixture_is_the_shape_this_repository_writes(tmp_path):
    """The fixture models the runner, or the gate is proved against a report nobody produces.

    Ten tests above feed this gate a JUnit and every one of them carried a `file` attribute --
    which pytest's default `xunit2` family filters out of every `<testcase>` it writes. So the
    branch the release workflow's leg 2 actually takes, `_module_from_classname`, was exercised by
    nothing, while the branch nothing produces was exercised by everything. The failure that
    leaves is not a false green -- it is leg 2 printing 1,700 rows as evidence that never ran over
    a build that is fine -- and a gate that cannot be believed when it says no is the same defect
    as one that cannot say it. [M4.16 cycle 2, M416-C2-GATE-01]
    """
    config = tomllib.loads((REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    assert "junit_family" not in config["tool"]["pytest"]["ini_options"], (
        "backend/pyproject.toml now pins junit_family, so what a report carries is that family's "
        "business rather than xunit2's: re-measure before trusting the shape below"
    )
    ids = ["backend/tests/test_x.py::test_y"]
    runner = tmp_path / "xunit2.xml"
    runner.write_text(_junit(ids), encoding="utf-8")
    assert "file=" not in runner.read_text(encoding="utf-8"), (
        "the fixture writes a `file` attribute again, so these tests prove the gate against a "
        "report this repository's pytest cannot produce"
    )
    assert coverage_gate.junit_outcomes([runner]) == {ids[0]: {"executed"}}
    # And the other branch, kept rather than deleted: a report that DOES carry `file` is the more
    # exact answer, because a test inside a class puts the class name in `classname`.
    carrying = tmp_path / "with-file.xml"
    carrying.write_text(_junit(ids, file_attribute=True), encoding="utf-8")
    assert 'file="tests/test_x.py"' in carrying.read_text(encoding="utf-8")
    assert coverage_gate.junit_outcomes([carrying]) == {ids[0]: {"executed"}}
    assert coverage_gate._module_from_classname("tests.test_x.TestThing") == "backend/tests/test_x.py"


def test_the_gate_refuses_to_answer_with_no_reports(capsys):
    """A gate handed nothing confirms nothing, and the one thing it must not then do is exit 0 --
    which is the exact shape of failure it was built to refuse, one level up. Two, not one: a
    usage error is not a coverage failure, and a workflow reading the code has to be able to tell
    "the gate says no" from "the gate was called wrong"."""
    assert coverage_gate.main([]) == 2
    assert "no reports given" in capsys.readouterr().err


def test_the_gate_refuses_a_report_file_that_is_not_there(tmp_path, capsys):
    """The same rule as `git ls-files` in rule 2's committed-tests check and as the interpreter
    check in `run.mjs`: an instrument that cannot read its input says so rather than proceeding
    over whatever it does have. A missing JUnit path silently ignored would report every pytest
    row as absent -- which reads as a catastrophic build failure and is a typo."""
    assert coverage_gate.main(["--junit", str(tmp_path / "nothing.xml")]) == 2
    assert "no such report" in capsys.readouterr().err


def test_the_gate_orders_milestones_the_way_the_map_authors_them():
    """One authored order in this repository, not two.

    `MILESTONES` is AUTHORED, NOT SORTED -- a string sort puts "M4.10" before "M4.5" and re-dates
    every row -- so the gate reads the list out of `test_spec_coverage.py` with `ast` rather than
    restating it. A second copy would be a second thing to keep true, which is the defect this
    milestone exists to close rather than to commit.
    """
    from tests import test_spec_coverage

    assert coverage_gate.authored_milestones() == test_spec_coverage.MILESTONES
    assert coverage_gate.authored_milestones() != sorted(test_spec_coverage.MILESTONES), (
        "the authored order and a string sort now agree, so this assertion has stopped proving "
        "that the gate read the authored one"
    )


def test_a_row_whose_only_evidence_this_gate_cannot_see_fails(tmp_path, monkeypatch, capsys):
    """Decision 226 admits a vitest id BESIDE a backend or Playwright test and never instead of
    one, because `npm --prefix e2e run fresh` -- the suite a milestone closes on -- does not run
    vitest at all. 47 named ids live in `frontend/src` and no report here answers for them.

    So they are counted and listed rather than passed over, and a row resting on them ALONE fails
    here as well as in `test_spec_coverage.py`. Two guards for one rule is deliberate: that one
    reads the map, this one reads a run, and the failure they are about -- a row closed by
    evidence nobody executed -- is the same failure at both altitudes.
    """
    synthetic = tmp_path / "map.toml"
    synthetic.write_text(
        'current_milestone = "M0"\n\n'
        "[[requirement]]\n"
        'id = "only-a-vitest-id"\n'
        'spec = "decision 226"\n'
        'milestone = "M0"\n'
        'kind = "backend"\n'
        'what = "a row whose entire evidence is a suite this gate is never given"\n'
        'why = "the shape decision 226 forbids"\n'
        'tests = ["frontend/src/lib/rate.svelte.test.js::a title"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(coverage_gate, "MAP", synthetic)
    junit = tmp_path / "junit.xml"
    junit.write_text(_junit([]), encoding="utf-8")
    assert coverage_gate.main(["--junit", str(junit)]) != 0
    assert "names only ids no report here can answer for" in capsys.readouterr().out


# --- the release workflow --------------------------------------------------------------

# Text, and by indentation, for the reason the CI guards in `test_harness_contracts.py` give:
# PyYAML is not a test dependency and this repository will not add one to read a workflow. The
# reader is imported from there rather than copied, so a third workflow does not acquire a third
# reading of the same file format.
#
# COMMENTS ARE STRIPPED ON THE WAY THROUGH, and here that is load-bearing rather than incidental:
# `release.yml`'s own header states that `continue-on-error` appears nowhere in it, and a guard
# that searched the raw text for that word would fail the file over the sentence promising the
# property it has. `_jobs` already strips them.
LEG_COUNT = 5
_LEG = re.compile(r"^\s*-?\s*name:\s*leg (?P<n>\d) of 5 - (?P<rest>.+)$", re.M)

# WHAT EACH LEG IS, as opposed to what its label says it is. A step name is a string somebody
# types; these are the commands §12's criteria actually live in, and `platform-release-gate-runs-
# every-leg`'s `what` enumerates them one by one. A guard reading only labels leaves that `what`
# unheld and lets any leg be hollowed out to `run: true` while five correctly-named steps still
# upload five correctly-named artifacts -- and `actions/upload-artifact`'s default
# `if-no-files-found: warn` does not fail a step whose file never appeared, so the hollow leg is
# green at runtime as well as statically. Leg 1's `--junitxml` path is listed because leg 2 reads
# that exact path, which makes the two legs one claim.
# [M4.16 cycle 1, M416-REL-03]
_LEG_COMMANDS: dict[int, tuple[str, ...]] = {
    1: ("pytest", "--junitxml=.reports/junit-release.xml"),
    2: ("ops/coverage_gate.py", "--junit", "--playwright"),
    3: ("ops/m45_exit_criterion.py",),
    4: ("pg_restore", "/api/health"),
    5: ("node e2e/run.mjs",),
}
# CLAUDE.md: "Plain `playwright test` against a used stack makes first-boot/bundle specs skip and
# fake-pass". Leg 4 leaves a stack up, so the one simplification leg 5 must never take is the one
# that looks like a simplification. `playwright install` is a different string and stays legal.
_LEG_FORBIDDEN: dict[int, tuple[str, ...]] = {5: ("playwright test",)}

# The `on:` block's events, and the job-level condition that may only restate them. A job whose
# `if:` evaluates false is reported SKIPPED rather than failed, so the run's conclusion is not a
# failure -- the same defect `_skip_green_problems` refuses one indent level in, at a scope it
# cannot see because `_steps` slices the job's own keys away before it reads.
# [M4.16 cycle 1, M416-REL-04]
_TRIGGER = re.compile(r"^  (?P<event>[a-z_]+):", re.M)
_EVENT_EQ = re.compile(r"github\.event_name\s*==\s*'(?P<event>[a-z_]+)'")
# The events that would put this job on the push path, which is the one thing decision 183 says it
# must never be on. `pull_request_target` is here beside the two that were: it is a third event
# with the same consequence and a name a person reaches for precisely because the other two are
# refused. [decision 183; M4.16 cycle 2, M416-C2-REL-03]
_UNATTENDED_TRIGGERS = ("push", "pull_request", "pull_request_target")


def _declared_events(text: str) -> set[str]:
    """Every event `on:` declares, in each of the three spellings YAML gives that block.

    `_TRIGGER` reads BLOCK MAPPING style -- one two-space-indented key per event -- and neither of
    the other two writes such a key: the flow sequence `on: [push, workflow_dispatch]` and the
    block sequence `on:` / `  - push`. That was not cosmetic. An unreadable block read as an EMPTY
    SET, which made `_job_gate_problems`'s "the `if:` must restate every declared event" vacuously
    true, while the push guard searched the same block for the same key and found nothing. So a
    release gate normalised to flow style AND given a push trigger passed every rule in this file
    at once, measured. One reader for all three, used by both rules, because two readings of one
    block is how the two came to disagree about it. A fourth shape returns nothing, and both
    callers then refuse rather than answering over a block they did not parse.
    [M4.16 cycle 2, M416-C2-REL-03]
    """
    block = _nested(text, "on")
    inline = block.split("\n", 1)[0].partition("on:")[2].strip()
    if inline:
        flow = re.fullmatch(r"\[(?P<items>[^\]]*)\]", inline)
        if flow is not None:
            return set(re.findall(r"[a-z_]+", flow.group("items")))
        # `on: schedule`, the one-event scalar. Anything else on that line -- an expression, an
        # anchor -- is a shape this reader does not know, and it returns nothing so the callers
        # refuse rather than answer over a block they did not parse.
        return set(re.findall(r"^[a-z_]+$", inline))
    listed = re.findall(r"^  -\s*(?P<event>[a-z_]+)\s*$", block, re.M)
    return set(listed) or {m.group("event") for m in _TRIGGER.finditer(block)}
# The forms that turn a failed command into a passing step, as a leg's BODY spells them. `|| :`
# and a trailing `; true` were added in review cycle 1 with `|| echo`, because the three spellings
# the rule started with were the three somebody had already thought of -- `|| exit 0` was in the
# pattern while a bare `exit 0`, the plainest of the set, was not. [M4.16 cycle 1, M416-REL-02]
#
# `continue-on-error` LEFT this pattern in review cycle 4 and became `_ADVISORY` below. It was the
# only member of the set that is a KEY rather than a shell form, and it was being applied where
# the shell forms are applied -- to leg-labelled steps only, after `if label is None: continue` --
# which is one scope narrower than the key can be written. [M4.16 cycle 4, M416-C4-REL-01]
_SWALLOWS_FAILURE = re.compile(
    r"\|\|\s*(?:true\b|exit 0\b|echo\b|:(?!\S))|;\s*(?:true\b|:(?!\S))\s*$",
    re.M,
)

# The declared way to say yes for a step that said no, at EVERY indent it can be written at.
# `release.yml`'s own header states that `continue-on-error` "appears nowhere" in the file and
# names this module as what holds that; the rule held it for leg-labelled steps. Three positions,
# and the guard was reading one of them:
#
#   ` {8}continue-on-error:`  a step's key, written after another key
#   ` {6}- continue-on-error:` the same key written FIRST in the step, which is what you get by
#                              adding it at the top of the block -- a YAML mapping's keys are
#                              unordered, so this is not an exotic spelling, it is the other one
#   ` {4}continue-on-error:`  THE JOB's own key. This workflow has exactly one job, so a run whose
#                              every leg failed still reports success -- the precise answer the
#                              gate exists to refuse, one indent above the level `_steps` can see,
#                              because `_steps` slices from the first `- ` and the job's keys are
#                              in the head it discards.
#
# The sibling self-hosted workflow is already held to the whole-job rule
# (`test_harness_contracts.py`'s `assert "continue-on-error" not in corpus`, over `real-bundle.yml`
# including its head), and the asymmetry between the two was the hole.
# [M4.16 cycle 4, M416-C4-REL-01]
_ADVISORY = re.compile(r"^(?: {4}| {6}- | {8})continue-on-error:", re.M)

# A step's condition, at either column its key can occupy, for `_ADVISORY`'s reason one key over:
# `      - if: vars.CORPUS_BUNDLE_DIR != ''` followed by `        name: leg 3 of 5 - ...` is the
# same gate this file already carries a mutation case for, written as the step's FIRST key. A
# step whose `if:` is false is reported SKIPPED, and a job whose steps all skipped or passed
# concludes success. The job's own `if:` is at four spaces and is `_job_gate_problems`'s, which
# reads `_job_head` rather than a step, so it is deliberately not in this alternation.
# [M4.16 cycle 4, M416-C4-REL-02]
_STEP_GATE = re.compile(r"^(?: {6}- | {8})if:\s*(?P<expr>.+)$", re.M)

# What a leg does INSTEAD of swallowing a failure, which is the half the pattern above cannot see:
# those forms are about a command's exit code, and these are about never reaching the command.
# Measured against the real workflow rather than the fixture -- seven mutations of `release.yml`,
# each one edit and each the edit that gets written under time pressure, all seven green before
# this: a guard clause returning 0 unless RUN_FULL_SUITE is set, the same leg wrapped in an
# if/else, the coverage gate left in the file behind a `false &&`, `|| :`, `set -u` softened to
# `set +e`, a trailing `; true`, and the browser pass made opt-in.
# [M4.16 cycle 1, M416-REL-02]
#
# REVIEW CYCLE 2, and the three spellings cycle 1's own sentence predicted. `exit\s+0` wants a
# literal zero, so a BARE `exit` -- which returns the status of the command before it, and after
# the `echo` that explains the skip that status is 0 -- walked past the rule that had just been
# widened to catch `exit 0`. `set\s+\+e\b` cannot match `set +o errexit`, the same switch spelled
# long. And a `trap 'exit 0' ERR` is `-e` turned off by a third name, at a position no anchor here
# reads: the quote sits where `^|;|&&|\|\|` wants a command boundary. All three were measured
# green against the file that ships and all three exit 0 under `bash -e -o pipefail` where the leg
# exits 1. [M4.16 cycle 2, M416-C2-REL-01]
_ABANDONS_THE_BODY = (
    (re.compile(r"(?:^|;|&&|\|\|)\s*exit\b(?:\s+0\b|\s*(?:;|$))"),
     "returns success before its work: a leg reports green by REACHING THE END of its body, so "
     "an explicit `exit 0` inside one can only be an early return, and a bare `exit` is the same "
     "return wearing the status of whatever ran before it"),
    (re.compile(r"(?:^|;|&&|\|\|)\s*set\s+\+(?:e\b|o\s+errexit\b)"),
     "turns off the `-e` this job's `defaults.run.shell: bash` supplies, which is what makes a "
     "failing command anywhere in the body fail the step -- `set +e` and `set +o errexit` are "
     "one switch with two spellings"),
    (re.compile(r"\btrap\b[^\n]*\bERR\b"),
     "installs an ERR trap, which decides what a failing command does INSTEAD of failing the "
     "step: `trap 'exit 0' ERR` is the same `-e` turned off by a third name, and it is written "
     "at no command position, so the two anchored rules above cannot see it"),
)

# And the one `||` a leg's own command may never be handed to, which is the half `_SWALLOWS_FAILURE`
# cannot reach. That pattern is a deny-list of FOUR right-hand sides -- `true`, `exit 0`, `echo`,
# `:` -- and a fifth walks past it: `| tee .reports/m45-exit.txt || printf advisory` is a leg whose
# script can fail while the step exits 0, and so are `|| cat`, `|| logger` and `|| tee -a`. The
# rule that does not enumerate is structural rather than lexical: whatever stands to the right of
# it, a leg's OWN command may not hand its exit code to a `||`, because that exit code is the
# leg's answer and this gate exists so that a leg can say no. Leg 5's
# `npm --prefix e2e ci || npm --prefix e2e install` stays legal and must: it is a fallback between
# two ways of installing node modules, not one of `_LEG_COMMANDS`, so the leg's answer is still
# `node e2e/run.mjs`'s. [M4.16 cycle 2, M416-C2-REL-01]
_HANDS_OFF_ITS_EXIT = "||"

# A step's `run:` block, and the shell structure inside it. The openers are read at a command
# position -- start of line, or after `;`, `&&`, `||`, `do` or `then` -- and heredoc bodies are
# skipped entirely, because leg 4 writes three Python programs that way: `sys.exit(0 if
# state["ok"] else 1)` is not a shell `if`, and a Python `if` at the head of such a line would
# otherwise open a block that never closes and make every statement after it read as conditional.
_RUN = re.compile(r"^ {8}run:(?P<inline>.*)$", re.M)
# AND A FUNCTION DEFINITION, since review cycle 4. A function's body is written once and run
# where it is CALLED, so `run_gate() {` opens a block for the same reason `if` does -- and it
# was not one of the openers, so leg 2's whole invocation moved into a function called only
# `if [ -f .reports/junit-release.xml ]`, measured, and every rule in this file stayed green.
# The closer is a brace ALONE on its line, which in shell is a function close and in no other
# construct: a `}` read anywhere on a line would count `${VAR}` as one and unbalance every
# body that expands a variable. The one-line form, `f() { cmd; }`, does not raise the depth --
# it opens and closes on one line, like the one-line `if` above -- and is refused by
# `_NAMES_RATHER_THAN_RUNS` below instead. [M4.16 cycle 4, M416-C4-REL-03]
_BLOCK_OPENS = re.compile(
    r"(?:^|;|&&|\|\||\bdo\b|\bthen\b)\s*(?:if|for|while|until|case)\b"
    r"|^\s*[\w-]+\s*\(\)\s*\{\s*$"
)
_BLOCK_CLOSES = re.compile(r"\b(?:fi|done|esac)\b|^\s*\}\s*$")
_HEREDOC = re.compile(r"<<-?\s*'?(?P<tag>[A-Za-z_]\w*)'?")
# What sits between the head of a line and a command when the command is not the line's own work.
# `&&` and `||` are the operators that can skip it; `then`, `else` and `do` are the one-line forms
# of the blocks `_BLOCK_OPENS` counts, which open and close on the same line and so never raise
# the depth a later line is read at.
#
# AND THE OPENERS THEMSELVES, since review cycle 2. A command in an `if`, `while` or `until`
# CONDITION is reached and IS run, and its failure is -- by definition of that position -- not
# fatal under `-e`. So `if ! python -m pytest ...; then echo "::warning::the suite failed"; fi`,
# the spelling GitHub's own documentation reaches for when a step is flaky, runs the suite, lets
# it fail, exits 0, and leaves legs 2 to 5 measuring a build whose tests are red. Every rule in
# this file read it as a leg that runs its command, because `then` landed in the TAIL while the
# head was a clean `if ! python -m `. Nothing downstream catches it either: `ops/coverage_gate.py`
# says in its own comment that a `<failure>` IS an execution, so a suite made advisory still
# writes a JUnit leg 2 calls fully covered. [M4.16 cycle 2, M416-C2-REL-01]
_GUARDED_HEAD = re.compile(r"&&|\|\||\bthen\b|\belse\b|\bdo\b|\bif\b|\bwhile\b|\buntil\b")

# And what sits between the head of a line and a command when the line NAMES the command
# instead of running it. `_GUARDED_HEAD` is a deny-list of conditionals only, so it asks
# whether a line is reached and never whether the match is at a command position -- and
# `echo "::group::node e2e/run.mjs"` is a line that is reached, runs, and runs `echo`. Three
# mutations of the file that ships were measured green on that alone: leg 5 behind a
# `RUN_BROWSER` branch, leg 1 inside a retry loop, and leg 1 hollowed out to a single line
# telling a maintainer to run the suite by hand -- each with the leg still named, still in
# order, still uploading its artifact and still free of `continue-on-error`. That is the
# "label rather than a leg" this rule's own failure message describes, reached by the form of
# words the rule reads for.
#
# A DENY-LIST of heads rather than a rule that the command must begin the line, because
# `_LEG_COMMANDS` holds flag fragments (`--junitxml=`, `--junit`, `--playwright`) and paths
# that only ever appear mid-line (`/api/health` inside `"$BASE_URL/api/health"`,
# `pg_restore` after `$STACK exec -T db `), so a command-position rule would redden the file
# that ships -- measured. The tail is bounded by `[^;&|]*$` so that the rule is about the
# LAST command position on the line: `echo x; node e2e/run.mjs` really does run leg 5.
# The second alternative is a function DEFINITION, which is the same defect with braces:
# the command is written there and run somewhere else, or nowhere.
# [M4.16 cycle 4, M416-C4-REL-03]
_NAMES_RATHER_THAN_RUNS = re.compile(
    r"(?:^|;|&&|\|\||\bdo\b|\bthen\b|\belse\b)\s*(?::|echo|printf)\s[^;&|]*$"
    r"|\w\s*\(\)\s*\{[^;&|]*$"
)

# What leg 5 may never be handed. `run.mjs` spreads `process.argv.slice(2)` into BOTH of its
# `npx playwright test` invocations, so one appended argument narrows what the release gate
# measures while the two-phase structure, the restart between them and the step's own name -- "the
# two-phase browser pass over both projects" -- all survive intact. `--project=desktop` on a box
# where WebKit is the slow half drops section 6's phone-first layout, the primary form factor and
# the one the 48 px floor is written for, out of the instrument that decides whether this build
# ships; leg 2 cannot compensate, because it reads the report `ci.yml` uploaded rather than this
# leg's own run. A path or a `--grep` narrows it the same way.
# [CLAUDE.md, "the `phone` project (iPhone 13, WebKit) is the primary form factor";
#  M4.16 cycle 2, M416-C2-REL-02]
_LEG_NARROWED: dict[int, re.Pattern[str]] = {
    5: re.compile(r"node e2e/run\.mjs\s+[^\s;&|)]")
}


def _run_lines(step: str) -> list[str]:
    """A step's `run:` block as its own lines, the YAML key off the front.

    Comments are already gone -- `_jobs` strips them on the way through -- and here that is
    load-bearing rather than incidental in the other direction too: a body commented out line by
    line arrives EMPTY, and an empty body runs none of the leg's commands, which is the verdict it
    should get.
    """
    match = _RUN.search(step)
    if match is None:
        return []
    inline = match.group("inline").strip()
    if inline and inline not in ("|", ">", "|-", ">-", "|+", ">+"):
        return [inline]
    lines = []
    for line in step[match.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) < 10:
            break
        lines.append(line)
    return lines


def _unconditional(lines: list[str]) -> list[str]:
    """The lines of a shell body that run whatever happens, in the order they are written.

    A command reached only inside a branch, or only on some pass of a retry loop, is not one the
    body is obliged to run -- and that is the shape a skipped leg takes when nobody is willing to
    write `exit 0`: `if [ -n "$SKIP" ]; then echo skipped; else <the leg>; fi`. Leg 4's own
    `if [ "$phase" != "active" ]; then ... exit 1; fi` is untouched by this, which is the point of
    reading position rather than banning the keyword: that branch EXITS NON-ZERO, and the poll it
    closes is a precondition of the drill rather than the drill.
    """
    out, depth, heredoc = [], 0, None
    for line in lines:
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        if depth == 0:
            out.append(line)
        depth = max(0, depth + len(_BLOCK_OPENS.findall(line)) - len(_BLOCK_CLOSES.findall(line)))
        opener = _HEREDOC.search(line)
        if opener is not None:
            heredoc = opener.group("tag")
    return out


def _runs_the_command(command: str, lines: list[str]) -> bool:
    """Whether the body REACHES `command`, rather than merely containing it.

    Both halves are a real mutation: `if [ -n "$SKIP" ]; then ... else <command>; fi` leaves the
    command in the file on a line the body may never reach, and `echo skipping; false && <command>`
    leaves it on a line that is reached and never run. Either way the step exits 0 and the leg's
    name is still true of its label.

    The head is read as well as the depth, because the one-line spelling of the same wrapper --
    `if [ -n "$SKIP" ]; then <command>; fi` -- opens and closes on one line and so leaves the depth
    where it was.

    And a THIRD family since review cycle 4, which is neither of those: a line that is reached,
    runs, and does not run the command, because the command is standing in it as an argument or
    as a function body rather than at a command position. `echo "::group::node e2e/run.mjs"`
    satisfied every rule in this file. [M4.16 cycle 4, M416-C4-REL-03]
    """
    for line in _unconditional(lines):
        head, found, _rest = line.partition(command)
        if not found:
            continue
        if not _GUARDED_HEAD.search(head) and not _NAMES_RATHER_THAN_RUNS.search(head):
            return True
    return False


def _steps(job: str) -> list[str]:
    """Each step of a job, as its own block of text, comments already gone."""
    heads = [m.start() for m in re.finditer(r"^ {6}- ", job, re.M)]
    return [job[start:(heads[i + 1] if i + 1 < len(heads) else len(job))]
            for i, start in enumerate(heads)]


# The one line in `ci.yml` that may hand its exit code away, quoted WHOLE rather than exempting
# its job. CLAUDE.md states that `ruff format` is not enforced and that CI runs it
# informationally, and `ci.yml`'s own svelte-check comment names this step as the contrast -- that
# step's "exit code is NOT discarded the way `ruff format --check`'s is in the lint job ... which
# is the whole difference between a gate and a report". Exempting the LINE is what stops the
# exception growing into its job: `_ci_conclusion_problems` below also asks whether a job carrying
# an admitted line still runs a command whose exit code it keeps, so the day `ruff check .`
# acquires a `|| true` the lint job stops being a gate and the rule says so.
# [CLAUDE.md Commands; M4.16 cycle 5, M416-C5-CI-01]
_CI_ADVISORY_LINES = {"ruff format --check . || true"}

# The command `ci.yml`'s e2e job exists to run, and the one producer of leg 2's only input.
_CI_BROWSER_COMMAND = "node e2e/run.mjs"


def _ci_run_lines(step: str) -> list[str]:
    """A `ci.yml` step's shell body, whichever column its `run:` key sits at.

    `_run_lines` reads `^ {8}run:`, which is the only spelling `release.yml` has: every leg there
    carries a `name:` first, so the key never sits on the step's own dash. `ci.yml` writes most of
    its steps as a bare `      - run: <command>`, and a reader that knew one spelling would answer
    "this job runs nothing" over the job that runs everything -- which is the polarity this rule
    exists to refuse. Block scalars are followed in both spellings for the same reason.
    """
    match = re.search(r"^(?: {6}- | {8})run:(?P<inline>.*)$", step, re.M)
    if match is None:
        return []
    inline = match.group("inline").strip()
    if inline and inline not in ("|", ">", "|-", ">-", "|+", ">+"):
        return [inline]
    lines = []
    for line in step[match.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) < 10:
            break
        lines.append(line)
    return lines


def _ci_conclusion_problems(text: str) -> list[str]:
    """Every way a job in `ci.yml` can be made unable to fail the conclusion leg 2 selects by.

    Three mechanisms, because that is how many `release.yml` is held to and `ci.yml` was held to
    one. `_ADVISORY` reads the declared KEY at all three indents. The other two are shell, so they
    live inside a step's body where no key rule looks: `_SWALLOWS_FAILURE` is the `|| true` family,
    already written eight jobs above the e2e job, and `_runs_the_command` is the family that never
    reaches the command at all -- `if ! node e2e/run.mjs; then echo '::warning::'; fi`, which this
    file's own `_GUARDED_HEAD` comment calls "the spelling GitHub's own documentation reaches for
    when a step is flaky", and `echo "::group::node e2e/run.mjs"`, which names the command instead
    of running it. Both were measured green against the file that ships while the three
    `continue-on-error` positions went red, which is the same two-workflow asymmetry `_ADVISORY`'s
    own comment calls the hole, one mechanism over.

    The second rule is over EVERY job, because the row's `what` is over every job: leg 2 selects a
    ci run by its conclusion, and any job able to conclude success while its work failed is a job
    that can hand leg 2 a report of a suite nothing failed on. The third is over the e2e job
    alone, because it is about one named command. A step whose `run:` this reader cannot find is
    reported rather than forgiven -- the loud direction, and the only one available to a rule
    whose subject is a silence.

    `_ABANDONS_THE_BODY` is deliberately NOT applied here, and the reason is the file rather than
    the rule: `ci.yml`'s health loop writes `curl ... && exit 0` and closes with `exit 1`, which is
    a poll succeeding early rather than a leg returning green before its work, so importing that
    family would redden the workflow that ships. What it leaves open is a `set +e` or an ERR trap
    written into a step body, which on this file's one-command browser step changes nothing on its
    own -- the step still exits with that command's status -- and which is recorded here rather
    than guessed at. [M4.16 cycle 5, M416-C5-CI-01]
    """
    problems = [
        f"ci.yml carries `{key}`, so a run whose job failed can still conclude success"
        for key in sorted({m.group(0).strip() for m in _ADVISORY.finditer(text)})
    ]
    jobs = _jobs(text)
    if not jobs:
        return problems + ["ci.yml declares no jobs, so this guard is reading nothing"]
    for name, job in sorted(jobs.items()):
        admitted, gating = False, False
        for step in _steps(job):
            lines = _ci_run_lines(step)
            if not lines:
                continue
            swallowed = [line.strip() for line in lines if _SWALLOWS_FAILURE.search(line)]
            for line in swallowed:
                if line in _CI_ADVISORY_LINES:
                    admitted = True
                else:
                    problems.append(
                        f"ci.yml's `{name}` job hands a step's exit code away in the shell, which "
                        f"`continue-on-error` says in YAML and this rule was reading only there: "
                        f"{line}"
                    )
            if not swallowed:
                gating = True
        if admitted and not gating:
            problems.append(
                f"ci.yml's `{name}` job runs nothing whose exit code it keeps, so the one line "
                "this rule admits as informational is now the whole job"
            )
    if "e2e" not in jobs:
        return problems + ["ci.yml declares no `e2e` job, so leg 2's only input has no producer"]
    reaches = any(
        _runs_the_command(_CI_BROWSER_COMMAND, _ci_run_lines(step))
        for step in _steps(jobs["e2e"])
    )
    if not reaches:
        problems.append(
            f"ci.yml's `e2e` job no longer REACHES `{_CI_BROWSER_COMMAND}` at a command position, "
            "so the browser suite is named rather than run and the job concludes success without "
            "it"
        )
    return problems


def _job_head(job: str) -> str:
    """The job's own keys -- everything `_steps` throws away on its way to the first `- `.

    This exists because throwing them away was a hole rather than a simplification: `runs-on`,
    `services`, `defaults` and the job-level `if:` all live here, and the last of those decides
    whether any step runs at all.
    """
    first = re.search(r"^ {6}- ", job, re.M)
    return job[:first.start()] if first else job


def _release_job(text: str) -> str:
    jobs = _jobs(text)
    assert jobs, "release.yml declares no jobs, so this guard is reading nothing"
    return next(iter(jobs.values()))


def _leg_problems(text: str) -> list[str]:
    """Whether five legs run, in order, each uploading what it produced.

    The order is the whole claim. §12's criteria are not five independent questions: leg 2 reads
    the report leg 1 wrote, leg 4 restores what leg 4's own import created, and a gate whose legs
    can be reordered by a `needs:` edit is a gate whose result depends on the arrangement rather
    than on the build. Steps of one job is what makes that a fact rather than a diagram.
    """
    problems: list[str] = []
    job = _release_job(text)
    steps = _steps(job)
    seen: dict[int, int] = {}
    uploads: dict[int, str] = {}
    bodies: dict[int, str] = {}
    for index, step in enumerate(steps):
        leg = _LEG.search(step)
        if leg is None:
            continue
        number = int(leg.group("n"))
        seen.setdefault(number, index)
        bodies.setdefault(number, []).append(_run_lines(step))
        if "actions/upload-artifact" in step:
            uploads[number] = step

    for number in range(1, LEG_COUNT + 1):
        if number not in seen:
            problems.append(f"no step is named `leg {number} of 5 - ...`")
            continue
        blocks = bodies[number]
        for command in _LEG_COMMANDS[number]:
            if not any(_runs_the_command(command, lines) for lines in blocks):
                problems.append(
                    f"leg {number} does not run `{command}` where its body has to reach it: the "
                    "leg IS its command, and a command that is absent, or behind a condition, or "
                    "behind an operator that can skip it, is a label rather than a leg"
                )
        for banned in _LEG_FORBIDDEN.get(number, ()):
            if any(banned in line for lines in blocks for line in lines):
                problems.append(
                    f"leg {number} runs `{banned}`: the two-phase harness is load-bearing, and a "
                    "plain browser pass over a used stack skips the specs it exists to run"
                )
        narrowing = _LEG_NARROWED.get(number)
        if narrowing is not None:
            for line in (line for lines in blocks for line in lines):
                if narrowing.search(line):
                    problems.append(
                        f"leg {number} narrows its own pass, at `{line.strip()}`: the harness "
                        "passes its arguments through to Playwright, so a project, a `--grep` or "
                        "a spec path here shrinks what the release gate measures while the step's "
                        "name goes on describing the whole of it"
                    )
    order = [seen[n] for n in sorted(seen)]
    if order != sorted(order):
        problems.append(
            "the legs do not run in their numbered order: leg 2 reads what leg 1 wrote and leg 4 "
            "restores what leg 4 imported, so the order is the claim"
        )
    for number in range(1, LEG_COUNT + 1):
        step = uploads.get(number)
        if step is None:
            problems.append(
                f"leg {number} uploads no artifact: a leg whose evidence is only in the run log "
                "cannot be read after the log expires"
            )
            continue
        artifact = re.search(r"^ {10}name:\s*(?P<value>\S+)", step, re.M)
        if artifact is None or not artifact.group("value").startswith(f"release-leg-{number}-"):
            problems.append(
                f"leg {number}'s upload is not named `release-leg-{number}-...`: five artifacts "
                "that cannot be told apart are one artifact"
            )
    return problems


def _skip_green_problems(text: str) -> list[str]:
    """Whether any step can report success without doing its work.

    Three spellings, and they are one defect: `continue-on-error` on the step, `|| true` inside
    it, and an `if:` that can be false. The third is the one that arrives innocently -- a leg
    gated on `vars.CORPUS_BUNDLE_DIR != ''` reads as prudence and turns the release gate into a
    workflow that passes on a machine without the corpus, which is every machine but one.
    `always()` is the single admitted expression: it cannot be false, and it is what makes a
    failing leg still hand back the evidence of its failure.

    And a fourth, which is the one the three could not see: a body that never reaches its work.
    Nothing here read a leg's `run:` block, so an early `exit 0` or a `set +e` -- neither of them
    a spelling of failure-swallowing, both of them a leg reporting green having run nothing --
    passed every rule in this file. The other half of that hole is `_leg_problems`'s command
    check, which now asks whether the body REACHES the command rather than whether the file
    contains it. [M4.16 cycle 1, M416-REL-02]

    The two KEY rules -- the `if:` and `continue-on-error` -- are read at every indent a step can
    write them at, and before the leg label is consulted. Both were anchored on one column and one
    scope, which made them rules about typing habits rather than about the workflow: a step is a
    YAML mapping and key order is free, so `      - if: vars.CORPUS_BUNDLE_DIR != ''` above the
    `name:` is the same condition written the other legal way round, and `^ {8}if:` cannot see it
    while `_LEG` -- which tolerates the name at either column by design -- goes on reporting the
    leg as present, in order, with its command reached and its artifact named. Measured on the
    file that ships: leg 3 and leg 5 each gated that way left all five readers here empty.
    [M4.16 cycle 4, M416-C4-REL-02 and M416-C4-REL-01]
    """
    problems = []
    job = _release_job(text)
    for step in _steps(job):
        label = _LEG.search(step)
        name = ("leg " + label.group("n") if label
                else (re.search(r"name:\s*(.+)", step) or re.match(r"\s*- (.*)", step)).group(1))
        gate = _STEP_GATE.search(step)
        if gate and gate.group("expr").strip() not in ("always()", "${{ always() }}"):
            problems.append(
                f"step `{name.strip()}` carries `if: {gate.group('expr').strip()}`: a condition "
                "that can be false is a leg that reports green having run nothing"
            )
        if _ADVISORY.search(step):
            problems.append(
                f"step `{name.strip()}` swallows a failure with `continue-on-error`: the whole "
                "point of a release gate is that a leg can say no, and this key says yes for it"
            )
        if label is None:
            continue
        swallow = _SWALLOWS_FAILURE.search(step)
        if swallow:
            problems.append(
                f"leg {label.group('n')} swallows a failure with `{swallow.group(0).strip()}`: "
                "the whole point of a release gate is that a leg can say no"
            )
        body = _run_lines(step)
        for pattern, why in _ABANDONS_THE_BODY:
            offending = next((line for line in body if pattern.search(line)), None)
            if offending is not None:
                problems.append(
                    f"leg {label.group('n')} {why}, at `{offending.strip()}`"
                )
        for command in _LEG_COMMANDS.get(int(label.group("n")), ()):
            for line in body:
                _head, found, rest = line.partition(command)
                if found and _HANDS_OFF_ITS_EXIT in rest:
                    problems.append(
                        f"leg {label.group('n')} hands `{command}`'s own exit code to `||`, at "
                        f"`{line.strip()}`: whatever stands on the right of it, the leg's answer "
                        "is the fallback's from then on and the step reports green over a command "
                        "that said no"
                    )
    return problems


def _job_gate_problems(text: str) -> list[str]:
    """Whether the JOB's own `if:` can be false, which skips all five legs at once.

    `_skip_green_problems` reads `^ {8}if:` -- step level -- over blocks `_steps` produced, and
    `_steps` starts at the first `- `, so the job's own keys are in the head it discards. The
    condition at `^ {4}if:` was therefore read by nothing, while the file's own header asserted
    the property in prose: "It can never be false as the file stands, so it cannot turn this job
    into a skipped-green one." A guard that is a sentence is the thing this file exists to refuse.

    The rule is narrow on purpose. No job-level `if:` at all is the safe state and reports
    nothing. One that IS there may only restate the events `on:` declares -- each as
    `github.event_name == '<event>'`, joined by `||`, with every declared event present -- which
    is a tautology and therefore cannot skip. Anything else, `vars.CORPUS_BUNDLE_DIR != ''` first
    among them, is a condition with a false branch; GitHub reports such a job as skipped rather
    than failed, so the run's conclusion is not a failure. [M4.16 cycle 1, M416-REL-04]

    And the job's OTHER key with the same consequence, which cycle 1 left behind while closing the
    first: `continue-on-error` at four spaces. `jobs.<id>.continue-on-error` prevents a workflow
    run from failing when the job fails, and this workflow has exactly one job -- so a run whose
    every leg went red still reports success. The edit reads as prudence on a household's own
    runner ("do not let a flaky box redden the branch") and is exactly the one
    `.github/workflows/real-bundle.yml` already records somebody wanting to make. It is read here
    rather than beside the step rule because `_steps` slices from the first `- `, so the job's own
    keys are only ever in the head. [M4.16 cycle 4, M416-C4-REL-01]
    """
    job = _release_job(text)
    head = _job_head(job)
    problems = []
    if _ADVISORY.search(head):
        problems.append(
            "the job itself carries `continue-on-error`: this workflow has one job, so a run "
            "whose every leg failed still reports success -- the answer this gate exists to "
            "refuse, one indent above the level `_steps` can see"
        )
    gate = re.search(r"^ {4}if:\s*(?P<expr>.+)$", head, re.M)
    if gate is None:
        return problems
    expr = gate.group("expr").strip()
    declared = _declared_events(text)
    if not declared:
        problems.append(
            "this guard cannot read release.yml's `on:` block, so the tautology rule below is "
            "vacuous: a condition it cannot compare against a declared event is one it cannot "
            "call a tautology, and a gate that answers over a block it did not parse is worse "
            "than one that says it could not"
        )
    restated = set(_EVENT_EQ.findall(expr))
    residue = _EVENT_EQ.sub("", expr).replace("||", "")
    residue = residue.replace("${{", "").replace("}}", "").strip()
    if residue:
        problems.append(
            f"the job carries `if: {expr}`, which tests something other than the event that "
            "triggered it: a job whose condition can be false is reported SKIPPED rather than "
            "failed, so all five legs report green having run nothing"
        )
    missing = sorted(declared - restated)
    if missing:
        problems.append(
            f"the job's `if:` does not admit every trigger `on:` declares: {missing}. A trigger "
            "the condition forgets is a dispatch that skips the whole gate."
        )
    return problems


def _staging_problems(text: str) -> list[str]:
    """Whether leg 4 stages a bundle the importer can open, and gives the directory back after.

    Two facts about one directory, both of which a run would have found and no reading of the
    step's LABEL ever could.

    The copy: `api/artifacts._resolve(None)` answers `cfg.import_dir`, and
    `importer/bundle.Bundle.open` falls back only through `_archive_within`, which matches
    `.tar`/`.tar.zst` FILES and never a single child directory. `cp -a "$DIR" data/import/` into a
    directory that already exists copies the directory INTO it, so the bundle lands one level down
    and `validate_for_install` refuses on a root holding no BUNDLE.json -- leg 4 could only fail,
    and legs 4 and 5 could never run.

    The ownership: `run.mjs` phase 0 rebuilds `data/import` with `make_bundle` as the runner's own
    user (decision 299), and creating a file in a directory needs write on the DIRECTORY. Leg 4
    hands that directory to uid 1000 and the file says so twice in its own comments, so unless it
    is handed back, leg 5 dies at its first statement and the gate reports on the runner instead
    of on the build. [M4.16 cycle 1, M416-C1-D3-02 and M416-REL-05]
    """
    job = _release_job(text)
    problems = []
    copies = re.findall(r"^\s*cp -a\s+(?P<src>\S+)\s+data/import/?\s*$", job, re.M)
    if not copies:
        problems.append(
            "leg 4 never copies a bundle into data/import: the restore drill's subject is a real "
            "import, and there is nothing to import"
        )
    for src in copies:
        if not src.rstrip('"').endswith("/."):
            problems.append(
                f"leg 4 stages `{src}` into data/import without a trailing `/.`: cp copies the "
                "DIRECTORY into an existing destination, and Bundle.open does not descend into a "
                "single child directory, so the import can only refuse"
            )
    taken = [m.end() for m in re.finditer(r"^\s*sudo chown -R 1000:1000[^\n]*\bdata/import\b",
                                          job, re.M)]
    given_back = [
        m.end()
        for m in re.finditer(r"^\s*sudo chown (?:-R )?\"\$\(id -u\)[^\n]*\bdata/import\b", job, re.M)
    ]
    if taken and not any(back > max(taken) for back in given_back):
        problems.append(
            "leg 4 hands data/import to uid 1000 and never hands it back: leg 5's `run.mjs` "
            "rebuilds that directory as the runner (decision 299), and a directory it cannot "
            "write is a browser leg that dies before Playwright starts"
        )
    return problems


def test_the_release_workflow_runs_five_legs_in_order():
    """The full suite, the executed-coverage gate, the real-bundle legs, the restore drill and the
    two-phase browser pass -- each producing something a person can read afterwards."""
    assert _leg_problems(_read(RELEASE)) == []


def test_no_release_leg_can_pass_by_skipping_its_body():
    """"The suite is green" was documented as a stronger statement than §12's exit criterion. It
    is not stronger, it is different, and a leg that skips green would make the new instrument the
    same kind of claim."""
    assert _skip_green_problems(_read(RELEASE)) == []


def test_no_condition_on_the_job_itself_can_skip_all_five_legs():
    """One indent level above the rule above it, which is where the property was only asserted."""
    assert _job_gate_problems(_read(RELEASE)) == []


def test_leg_four_stages_a_bundle_the_importer_can_open_and_gives_the_directory_back():
    """The two facts about `data/import` that decide whether legs 4 and 5 can run at all."""
    assert _staging_problems(_read(RELEASE)) == []


def test_the_release_gate_reaches_the_corpus_before_its_first_leg():
    """The one precondition that would otherwise turn three legs into silent skips.

    `CORPUS_BUNDLE_DIR` unset does not fail anything: `test_bundle_shapes.py` and
    `test_bundle_validation.py` skip, `ops/m45_exit_criterion.py` refuses, and leg 4 has no bundle
    to import. `.github/workflows/real-bundle.yml` learned this first -- a job whose entire
    purpose is to un-skip two tests must not be able to report green having skipped them -- and
    the check is stated here in the same shape, before leg 1 rather than inside it, because leg 2
    is then entitled to call those two rows uncovered.
    """
    job = _release_job(_read(RELEASE))
    steps = _steps(job)
    guard = next((i for i, s in enumerate(steps) if "CORPUS_BUNDLE_DIR" in s and "exit 1" in s), None)
    assert guard is not None, (
        "no step refuses when CORPUS_BUNDLE_DIR is unset or is not a directory"
    )
    first_leg = next(i for i, s in enumerate(steps) if _LEG.search(s))
    assert guard < first_leg, "the corpus check runs after a leg that needs the corpus"


def _trigger_problems(text: str) -> list[str]:
    """Whether anything this workflow runs on can put it on the push path.

    Read as EVENTS rather than as text. The rule searched for the keys `push:` and `pull_request:` over
    everything before `jobs:`, which is a search for a block-style key: `on: [push,
    workflow_dispatch]` declares the same trigger and writes neither, and the compensating rule
    one function down -- the job's `if:` must restate every declared event -- read the same block
    with the same reader and was therefore satisfied by anything. Measured: the two together
    passed a release gate that runs on every branch push. [decision 183; M4.16 cycle 2,
    M416-C2-REL-03]
    """
    block = _nested(text, "on")
    if not block.strip():
        return ["release.yml declares no `on:` block, so this guard is reading nothing"]
    declared = _declared_events(text)
    if not declared:
        return [
            "this guard cannot read release.yml's `on:` block, so it can name no trigger at all: "
            f"a shape neither spelling covers is one it must refuse rather than pass. {block!r}"
        ]
    return [
        f"release.yml triggers on {event}, which queues a self-hosted job on every branch push: "
        "the corpus reaches this runner by already being on it (decision 183), and legs 4 and 5 "
        "hold the household's own machine for the length of a real bundle import"
        for event in sorted(declared.intersection(_UNATTENDED_TRIGGERS))
    ]


def test_the_release_workflow_never_runs_on_a_push():
    """Decision 183, read from the other side. This job runs on the household's own machine
    because the corpus reaches a runner by already being on it; a `push` or `pull_request` trigger
    would put every branch's push on that box, and legs 4 and 5 hold it for the length of a real
    bundle import."""
    assert _trigger_problems(_read(RELEASE)) == []


# A workflow with the five properties above, small enough to regress one at a time. It is
# `release.yml`'s own shape and not a sketch: the guards read indentation, so a fixture written at
# other column counts would pass rules the real file could still break.
_CLEAN_RELEASE = """\
name: release

on:
  workflow_dispatch:
  schedule:
    - cron: '41 5 * * 1'

jobs:
  release:
    if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'
    runs-on: [self-hosted, spielplan-corpus]
    steps:
      - uses: actions/checkout@v4
      - name: the corpus bundle must actually be on this runner's disk
        run: |
          if [ ! -d "${CORPUS_BUNDLE_DIR:-}" ]; then
            echo "CORPUS_BUNDLE_DIR is unset"
            exit 1
          fi
      - name: leg 1 of 5 - the full suite against Postgres 16
        run: python -m pytest backend/tests -q --junitxml=.reports/junit-release.xml
      - name: leg 1 of 5 - artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: release-leg-1-suite
          path: .reports/junit-release.xml
      - name: leg 2 of 5 - the executed-coverage gate
        run: |
          python ops/coverage_gate.py --junit .reports/junit-release.xml \
            --playwright .reports/browser/.results/report.json
      - name: leg 2 of 5 - artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: release-leg-2-coverage-gate
          path: .reports/coverage-gate.txt
      - name: leg 3 of 5 - the real-bundle legs against CORPUS_BUNDLE_DIR
        run: python -u ops/m45_exit_criterion.py
      - name: leg 3 of 5 - artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: release-leg-3-real-bundle
          path: .reports/m45-exit.txt
      - name: leg 4 of 5 - the restore drill at stack level
        run: |
          mkdir -p data/import
          cp -a "$CORPUS_BUNDLE_DIR/." data/import/
          sudo chown -R 1000:1000 data/artifacts data/import
          docker compose up -d
          sudo rm -rf data/import/*
          sudo chown -R "$(id -u):$(id -g)" data/import
          docker compose exec -T db pg_restore --clean -d "$POSTGRES_DB" /backups/x.dump
          curl -fsS "$BASE_URL/api/health"
      - name: leg 4 of 5 - artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: release-leg-4-restore-drill
          path: .reports/restore-drill-health.json
      - name: leg 5 of 5 - the two-phase browser pass over both projects
        run: node e2e/run.mjs
      - name: leg 5 of 5 - artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: release-leg-5-browser
          path: e2e/.results
"""


# Leg 4 whole, which is what "a leg deleted because it is slow" looks like as a diff: both of its
# steps, since the artifact step carries the leg's name too and leaving it behind regresses a
# different rule.
_LEG_FOUR = _CLEAN_RELEASE[
    _CLEAN_RELEASE.index("      - name: leg 4 of 5 - the restore drill"):
    _CLEAN_RELEASE.index("      - name: leg 5 of 5 -")
]


@pytest.mark.parametrize(
    "reader, old, new, needle",
    [
        pytest.param(_leg_problems, _LEG_FOUR, "",
                     "no step is named `leg 4 of 5",
                     id="a leg deleted"),
        pytest.param(_leg_problems,
                     "      - name: leg 4 of 5 - artifact\n"
                     "        if: always()\n"
                     "        uses: actions/upload-artifact@v4\n"
                     "        with:\n"
                     "          name: release-leg-4-restore-drill\n"
                     "          path: .reports/restore-drill-health.json\n",
                     "",
                     "leg 4 uploads no artifact",
                     id="a leg that leaves its evidence in the log"),
        pytest.param(_leg_problems,
                     "          name: release-leg-3-real-bundle\n",
                     "          name: release-artifact\n",
                     "leg 3's upload is not named",
                     id="two legs sharing an artifact name"),
        pytest.param(_skip_green_problems,
                     "        run: python -u ops/m45_exit_criterion.py\n",
                     "        run: python -u ops/m45_exit_criterion.py || true\n",
                     "swallows a failure",
                     id="a leg made quiet with || true"),
        pytest.param(_skip_green_problems,
                     "      - name: leg 3 of 5 - the real-bundle legs against CORPUS_BUNDLE_DIR\n",
                     "      - name: leg 3 of 5 - the real-bundle legs against CORPUS_BUNDLE_DIR\n"
                     "        if: vars.CORPUS_BUNDLE_DIR != ''\n",
                     "a condition that can be false",
                     id="a leg gated on the thing it is supposed to require"),
        pytest.param(_skip_green_problems,
                     "      - name: leg 5 of 5 - the two-phase browser pass over both projects\n"
                     "        run: node e2e/run.mjs\n",
                     "      - name: leg 5 of 5 - the two-phase browser pass over both projects\n"
                     "        continue-on-error: true\n"
                     "        run: node e2e/run.mjs\n",
                     "swallows a failure",
                     id="the browser leg made advisory"),
        # M4.16 cycle 1. The first four are the file as it stood before this cycle or one edit
        # from it, and every one of them left all five guards above green: five correctly-named
        # steps in order, five correctly-named uploads, no `if:`, no `|| true`.
        pytest.param(_leg_problems,
                     "        run: node e2e/run.mjs\n",
                     "        run: npx --prefix e2e playwright test\n",
                     "does not run `node e2e/run.mjs`",
                     id="the two-phase harness simplified to a plain browser pass"),
        pytest.param(_leg_problems,
                     "        run: python -u ops/m45_exit_criterion.py\n",
                     "        run: true\n",
                     "does not run `ops/m45_exit_criterion.py`",
                     id="a leg hollowed out to a body that cannot fail"),
        pytest.param(_job_gate_problems,
                     "    if: github.event_name == 'workflow_dispatch'"
                     " || github.event_name == 'schedule'\n",
                     "    if: vars.CORPUS_BUNDLE_DIR != ''\n",
                     "tests something other than the event that triggered it",
                     id="the whole job gated on the thing it is supposed to require"),
        pytest.param(_job_gate_problems,
                     "    if: github.event_name == 'workflow_dispatch'"
                     " || github.event_name == 'schedule'\n",
                     "    if: github.event_name == 'workflow_dispatch'\n",
                     "does not admit every trigger",
                     id="a declared trigger the job's condition forgets"),
        pytest.param(_staging_problems,
                     '          cp -a "$CORPUS_BUNDLE_DIR/." data/import/\n',
                     '          cp -a "$CORPUS_BUNDLE_DIR" data/import/\n',
                     "without a trailing `/.`",
                     id="the bundle staged one directory too deep"),
        pytest.param(_staging_problems,
                     '          sudo chown -R "$(id -u):$(id -g)" data/import\n',
                     "",
                     "never hands it back",
                     id="the import directory left owned by the container's uid"),
        # M4.16 cycle 2. The trigger block had no case at all, in either guard, and the two rules
        # that read it read it the same wrong way: a `push` in flow style is invisible to both.
        # [M4.16 cycle 2, M416-C2-REL-03]
        pytest.param(_trigger_problems,
                     "on:\n  workflow_dispatch:\n  schedule:\n    - cron: '41 5 * * 1'\n",
                     "on: [push, workflow_dispatch]\n",
                     "triggers on push",
                     id="a push trigger written in flow style"),
        pytest.param(_trigger_problems,
                     "on:\n  workflow_dispatch:\n",
                     "on:\n  pull_request_target:\n  workflow_dispatch:\n",
                     "triggers on pull_request_target",
                     id="the third event with the same consequence as the two that are named"),
        pytest.param(_trigger_problems,
                     "on:\n  workflow_dispatch:\n  schedule:\n    - cron: '41 5 * * 1'\n",
                     "on: ${{ fromJSON(vars.RELEASE_TRIGGERS) }}\n",
                     "cannot read release.yml's `on:` block",
                     id="a trigger block in a shape this reader does not know"),
        pytest.param(_job_gate_problems,
                     "on:\n  workflow_dispatch:\n  schedule:\n    - cron: '41 5 * * 1'\n",
                     "on: ${{ fromJSON(vars.RELEASE_TRIGGERS) }}\n",
                     "vacuous",
                     id="the tautology rule over a trigger block nobody could parse"),
        # M4.16 cycle 4, and the pair is one defect read at two indents: a key the guard held at
        # ONE of the three columns it can be written at. The job-head case is the whole workflow
        # reporting success with all five legs red, and it is one line that reads like prudence on
        # a household runner; the `- ` cases are the same keys written FIRST in the step, which is
        # what you get by adding them at the top of the block and which `^ {8}` cannot see while
        # `_LEG` -- position-tolerant by design -- goes on calling the leg present and in order.
        # Every one of these was measured green against the file that ships.
        # [M4.16 cycle 4, M416-C4-REL-01 and M416-C4-REL-02]
        pytest.param(_job_gate_problems,
                     "    runs-on: [self-hosted, spielplan-corpus]\n",
                     "    runs-on: [self-hosted, spielplan-corpus]\n"
                     "    continue-on-error: true\n",
                     "the job itself carries `continue-on-error`",
                     id="the whole job made advisory, one indent above the step rule"),
        pytest.param(_skip_green_problems,
                     "      - name: leg 3 of 5 - the real-bundle legs against CORPUS_BUNDLE_DIR\n",
                     "      - if: vars.CORPUS_BUNDLE_DIR != ''\n"
                     "        name: leg 3 of 5 - the real-bundle legs against CORPUS_BUNDLE_DIR\n",
                     "a condition that can be false",
                     id="the same gate written as the step's first key"),
        pytest.param(_skip_green_problems,
                     "      - name: the corpus bundle must actually be on this runner's disk\n",
                     "      - name: the corpus bundle must actually be on this runner's disk\n"
                     "        continue-on-error: true\n",
                     "swallows a failure with `continue-on-error`",
                     id="the corpus precondition made advisory, which no leg label covers"),
        pytest.param(_skip_green_problems,
                     "      - name: the corpus bundle must actually be on this runner's disk\n",
                     "      - continue-on-error: true\n"
                     "        name: the corpus bundle must actually be on this runner's disk\n",
                     "swallows a failure with `continue-on-error`",
                     id="the same key on that step written first, where the column rule ended"),
    ],
)
def test_the_release_guards_see_the_workflow_they_are_about(reader, old, new, needle):
    """Each case is one edit from the file that ships and each is the edit somebody makes
    under pressure: a leg deleted because it is slow, an artifact name reused, a noisy leg quieted
    with `|| true`, an expensive leg gated on the variable it exists to consume, and a flaky
    browser pass made advisory. A workflow guard can never observe a GitHub run, so being shown
    failing is the whole of its standing. [the idiom of `test_harness_contracts.py`]"""
    assert reader(_CLEAN_RELEASE) == [], "the guard does not pass the workflow it describes"
    assert old in _CLEAN_RELEASE, f"the fixture no longer contains {old!r}"
    problems = reader(_CLEAN_RELEASE.replace(old, new))
    assert problems, "the guard passed a regressed workflow"
    assert needle in " ".join(problems), problems


# The same discipline against the file that actually decides whether this build ships. The fixture
# above is `release.yml`'s SHAPE -- one command per leg, no loop, no pipeline, no heredoc -- and
# the rules below read shell structure, so a guard proved only against it is proved against a body
# the real workflow does not have. Every mutation here was measured GREEN under the guards as they
# stood in review cycle 1: five correctly named legs, in order, five correctly named uploads, no
# `if:`, no `|| true`, and a job that runs nothing. [M4.16 cycle 1, M416-REL-02]
_BS = chr(92)
_BODY_MUTATIONS = [
    pytest.param(
        _skip_green_problems,
        "        run: python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "        run: |\n"
        '          if [ -z "${RUN_FULL_SUITE:-}" ]; then echo "skipping the suite"; exit 0; fi\n'
        "          python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "returns success before its work",
        id="the suite behind a guard clause that exits 0",
    ),
    pytest.param(
        _leg_problems,
        "        run: python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "        run: |\n"
        '          if [ -n "${SKIP_SUITE:-}" ]; then\n'
        "            echo 'suite skipped'\n"
        "          else\n"
        "            python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n"
        "          fi\n",
        "leg 1 does not run `pytest`",
        id="the suite on the far side of an if/else",
    ),
    pytest.param(
        _leg_problems,
        "          python ops/coverage_gate.py " + _BS + "\n",
        "          echo skipping\n          false && python ops/coverage_gate.py " + _BS + "\n",
        "leg 2 does not run `ops/coverage_gate.py`",
        id="the coverage gate left in the file behind a false",
    ),
    pytest.param(
        _skip_green_problems,
        "          python -u ops/m45_exit_criterion.py | tee .reports/m45-exit.txt\n",
        "          python -u ops/m45_exit_criterion.py | tee .reports/m45-exit.txt || :\n",
        "swallows a failure",
        id="the real-bundle leg quieted with a colon",
    ),
    pytest.param(
        _skip_green_problems,
        "          set -u\n",
        "          set +e\n",
        "turns off the `-e`",
        id="the restore drill with errexit turned off",
    ),
    pytest.param(
        _skip_green_problems,
        "          $STACK logs --no-color --since 5m backend | tee -a .reports/restore-drill-health.json\n",
        "          $STACK logs --no-color --since 5m backend"
        " | tee -a .reports/restore-drill-health.json; true\n",
        "swallows a failure",
        id="the drill's last command given a trailing true",
    ),
    pytest.param(
        _leg_problems,
        "          node e2e/run.mjs\n",
        '          if [ -n "${RUN_BROWSER:-}" ]; then\n            node e2e/run.mjs\n          fi\n',
        "leg 5 does not run `node e2e/run.mjs`",
        id="the browser pass made opt-in",
    ),
    # The one-line spelling of the wrapper above, which opens and closes on a single line and so
    # never raises the depth the next line is read at. `_GUARDED_HEAD` is what sees it.
    pytest.param(
        _leg_problems,
        "        run: python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "        run: |\n"
        '          if [ -n "${SKIP_SUITE:-}" ]; then echo skipped; else python -m pytest '
        "backend/tests -q -rs --junitxml=.reports/junit-release.xml; fi\n",
        "leg 1 does not run `pytest`",
        id="the suite behind a one-line if/else",
    ),
    # M4.16 CYCLE 2. Seven more, each one edit from the file that ships and every one of them
    # measured GREEN under all four readers as they stood at the end of cycle 1. The first is the
    # one with a reason behind it and the one that would do the most damage: leg 1 is the slow leg,
    # `if ! <cmd>; then echo "::warning::"; fi` is the spelling GitHub's own documentation gives
    # for making a step advisory, and legs 2 to 5 would then run against a build whose tests are
    # red while the job's conclusion stays success. [M4.16 cycle 2, M416-C2-REL-01, M416-C2-REL-02]
    pytest.param(
        _leg_problems,
        "        run: python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "        run: |\n"
        "          if ! python -m pytest backend/tests -q -rs "
        "--junitxml=.reports/junit-release.xml; then\n"
        '            echo "::warning::the suite failed; see the artifact"\n'
        "          fi\n",
        "leg 1 does not run `pytest`",
        id="the suite made advisory the way GitHub documents",
    ),
    pytest.param(
        _skip_green_problems,
        "        run: python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "        run: |\n"
        '          if [ -z "${RUN_FULL_SUITE:-}" ]; then echo "skipping the suite"; exit; fi\n'
        "          python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "returns success before its work",
        id="the suite behind a guard clause that exits bare",
    ),
    pytest.param(
        _skip_green_problems,
        "          set -u\n",
        "          set +o errexit\n",
        "turns off the `-e`",
        id="the restore drill with errexit turned off the long way",
    ),
    pytest.param(
        _skip_green_problems,
        "          set -u\n",
        "          set -u\n          trap 'exit 0' ERR\n",
        "installs an ERR trap",
        id="the restore drill's failures handed to a trap",
    ),
    pytest.param(
        _skip_green_problems,
        "          python -u ops/m45_exit_criterion.py | tee .reports/m45-exit.txt\n",
        "          python -u ops/m45_exit_criterion.py | tee .reports/m45-exit.txt"
        " || printf advisory\n",
        "hands `ops/m45_exit_criterion.py`'s own exit code to `||`",
        id="the real-bundle leg quieted with a right-hand side nobody enumerated",
    ),
    pytest.param(
        _leg_problems,
        "          node e2e/run.mjs\n",
        "          node e2e/run.mjs --project=desktop\n",
        "leg 5 narrows its own pass",
        id="the browser pass narrowed to the faster project",
    ),
    pytest.param(
        _leg_problems,
        "          node e2e/run.mjs\n",
        "          node e2e/run.mjs specs/01-first-boot.spec.js\n",
        "leg 5 narrows its own pass",
        id="the browser pass narrowed to one spec",
    ),
    # M4.16 CYCLE 4, and all three are the same word: a leg whose command is in its body at no
    # command position. Each was measured GREEN under all six readers, and each is caught
    # without its `echo` -- so the echo is the whole of the rescue, which is what makes this a
    # rule about a form of words rather than about an execution. The first is the edit a
    # maintainer makes when the browser leg is slow on the household box; the second is the same
    # leg with no branch at all, one line telling the next person to run the suite by hand; the
    # third needs no echo, because a function definition was not one of the blocks
    # `_unconditional` counted. [M4.16 cycle 4, M416-C4-REL-03]
    pytest.param(
        _leg_problems,
        "          node e2e/run.mjs\n",
        '          echo "::group::node e2e/run.mjs"\n'
        '          if [ "${RUN_BROWSER:-}" = "1" ]; then\n'
        "            node e2e/run.mjs\n"
        "          fi\n",
        "leg 5 does not run `node e2e/run.mjs`",
        id="the browser pass named by an echo and run behind a variable",
    ),
    pytest.param(
        _leg_problems,
        "        run: python -m pytest backend/tests -q -rs --junitxml=.reports/junit-release.xml\n",
        "        run: |\n"
        '          echo "the box is busy: run python -m pytest backend/tests -q -rs"' + "\n"
        '          echo "with --junitxml=.reports/junit-release.xml, then dispatch again"' + "\n",
        "leg 1 does not run `pytest`",
        id="the suite replaced by a line naming it",
    ),
    pytest.param(
        _leg_problems,
        "          python ops/coverage_gate.py " + _BS + "\n"
        "            --junit .reports/junit-release.xml " + _BS + "\n"
        "            --playwright .reports/browser/.results/report-phase-1.json " + _BS + "\n"
        "            --playwright .reports/browser/.results/report.json " + _BS + "\n"
        "            | tee .reports/coverage-gate.txt\n",
        "          run_gate() {\n"
        "            python ops/coverage_gate.py " + _BS + "\n"
        "              --junit .reports/junit-release.xml " + _BS + "\n"
        "              --playwright .reports/browser/.results/report-phase-1.json " + _BS + "\n"
        "              --playwright .reports/browser/.results/report.json " + _BS + "\n"
        "              | tee .reports/coverage-gate.txt\n"
        "          }\n"
        "          if [ -f .reports/junit-release.xml ]; then run_gate; fi\n",
        "leg 2 does not run `ops/coverage_gate.py`",
        id="the coverage gate moved into a function called only if its input exists",
    ),
]


@pytest.mark.parametrize("reader, old, new, needle", _BODY_MUTATIONS)
def test_no_release_leg_can_report_green_having_run_nothing(reader, old, new, needle):
    """Fifteen bodies that report green having run nothing, against the real `release.yml`.

    This is the rule `platform-release-gate-runs-every-leg`'s `what` has always claimed and the
    file has not held: the guards read step NAMES, a step-level `if:` and three spellings of
    failure-swallowing, and none of them read what a leg's body does. The plan states the property
    twice in its own words -- "A step that skips its whole body fails the job" -- and legs 3, 4 and
    5 produce nothing any later leg reads, so a hollow one of those is green at runtime as well as
    here. [M4.16-plan.md:634; M4.16 cycle 1, M416-REL-02]
    """
    text = _read(RELEASE)
    assert reader(text) == [], "the guard does not pass the workflow that ships"
    assert old in text, f"release.yml no longer contains {old!r}"
    problems = reader(text.replace(old, new))
    assert problems, "the guard passed a leg that runs nothing"
    assert needle in " ".join(problems), problems


def test_the_body_reader_does_not_read_a_heredoc_as_shell():
    """The other half of the rule above, and the reason leg 4 does not fail it by accident.

    Leg 4 writes three Python programs with `cat > ... <<'PY'`, and a reader that counted shell
    keywords inside those bodies would meet a Python `if` at the head of a line, open a block that
    never closes, and report every statement after it -- `pg_restore` and the health check among
    them -- as conditional. A guard that fails on the file it describes is removed by the first
    person it stops, which is the shape this whole file is written against.
    [M4.16 cycle 1, M416-REL-02]
    """
    inside_a_heredoc = _read(RELEASE).replace(
        "          import json, sys\n",
        "          import json, sys\n          if len(sys.argv) > 1:\n              pass\n",
    )
    assert inside_a_heredoc != _read(RELEASE), "leg 4 no longer writes a Python heredoc"
    assert _leg_problems(inside_a_heredoc) == []
    assert _skip_green_problems(inside_a_heredoc) == []


# --- decision 299: the harness rebuilds the fixture it measures -------------------------

# Anchored on the two calls and their order rather than on any word of the prose around them. The
# property is "the fixture is rebuilt BEFORE the stack is reset", and both halves have a statement
# in the file: `make_bundle` is what rebuilds, `reset.mjs` is what resets.
#
# THE STATEMENTS, WHICH IS WHAT THE PARAGRAPH ABOVE SAID AND THE PATTERNS DID NOT. They were the
# bare words `make_bundle` and `reset.mjs` searched over the whole file, and `run.mjs` argues about
# both in its comments: the first `make_bundle` is comment line 34 and the first `reset.mjs` is the
# comment at line 90 that says "Before `reset.mjs`". So the ordering rule compared one sentence
# with another, and every regression that leaves the constants in place -- the call wrapped in a
# try/catch, switched to `spawnSync` with the status never read, made conditional on the directory
# being absent, commented out, or moved below the reset -- passed. `test_harness_contracts.py:179`
# had already diagnosed this exact defect on this exact file: "Anchored on the log STATEMENT, not
# on the words". At the START of a line, because that is what makes it a statement rather than a
# clause: `try {` and `if (...)` both put something in front of it.
# [decision 299; M4.16 cycle 1, M416-C1-D3-01]
_REBUILD = re.compile(r"^execFileSync\(PYTHON, \['-c', BUILD_FIXTURE\]", re.M)
_RESET = re.compile(r"^execFileSync\('node', \[join\(HERE, 'reset\.mjs'\)\]", re.M)
# And what the rebuild builds, WHERE the harness then measures it. The guard read neither: a
# rebuild writing `data/scratch` satisfied every word of this rule while the browser suite went on
# importing whatever `data/import` held, which is the state decision 299 was taken to end.
_BUILDS_THE_IMPORT_DIR = re.compile(r"make_bundle\(pathlib\.Path\('data/import'\)\)")
# And the directory is EMPTIED before it is rebuilt, which is the half a rebuild does not imply.
# `make_bundle` OVERLAYS: it `mkdir(exist_ok=True)`s its root and its `artifacts/` and unlinks
# only `content.sqlite` and `reviews.sqlite`, so any other file a DIFFERENT bundle left in
# `data/import` survives -- and because `_write_identity` runs LAST, `_inventory` rglobs the
# merged tree and the BUNDLE.json the rebuild writes describes the hybrid exactly. That is what
# makes the mixture quiet rather than loud: `validate`'s `unlisted` rule, the one thing that
# would refuse a foreign file, is disarmed by the rebuild's own manifest. Measured on this tree
# with the modules themselves: a planted `artifacts/cold_eval.json` survived the rebuild, was
# inventoried, validated clean and came back through `ArtifactStore.present` as fixture content
# -- which `home/shelves.py` then prints against. `release.yml` empties the same directory by
# hand between its corpus leg and leg 5; this is that line at the altitude `docs/TESTING.md`
# calls the canonical run, where an operator's staged corpus export is the bundle at risk.
# [decision 299; M4.16 cycle 1, M416-C1-D3-03]
_EMPTIES = re.compile(r"^for \(const entry of readdirSync\(IMPORT_DIR\)\) \{$", re.M)
_REMOVES = re.compile(
    r"^\s+rmSync\(join\(IMPORT_DIR, entry\), \{ recursive: true, force: true \}\);$", re.M
)
_THE_TARGET = re.compile(r"^const IMPORT_DIR = join\(ROOT, 'data', 'import'\);$", re.M)
# And the clearing REFUSES rather than reports. The rule above asks that the loop and its `rmSync`
# be there, which is a rule about the happy path: `force: true` suppresses ENOENT and nothing
# else, so a `.unpacked-*` tree the importer left behind as uid 1000 -- `ci.yml` chowns this mount
# before the stack starts -- makes `rmSync` throw for real. That is the arm somebody has a reason
# to soften, and softening it is invisible to every other rule here: the loop still reads the
# directory, the rebuild still runs, `make_bundle` overlays the root-owned leftover,
# `_write_identity` runs LAST so `_inventory` lists it into the BUNDLE.json the rebuild writes,
# `validate`'s `unlisted` rule is disarmed by that same manifest, and the post-condition below is
# satisfied by construction. The browser gate then reports its pass against a tree nobody built,
# which is the hybrid M416-C1-D3-03 was measured on. Anything that leaves the process is accepted,
# not the one spelling: a catch that says no is what the rule is about.
# [decision 299; M4.16 cycle 2, M416-C2-D3-01]
_CLEARING_CATCH = re.compile(r"\bcatch\b")
_CLEARING_REFUSES = re.compile(r"\bthrow\b|\bprocess\.exit\(")
# And a post-condition, because the rebuild's own report is an unconditional `print` at the end
# of BUILD_FIXTURE: it says the bundle was written whether or not one was. `BUNDLE.json` is the
# file `Bundle.open` looks for, and a root without one falls through to `_archive_within` and
# imports whatever archive is lying there -- decision 299's fallback, reached the other way.
_POST_CONDITION = re.compile(
    r"^if \(!existsSync\(join\(IMPORT_DIR, 'BUNDLE\.json'\)\)\) \{$", re.M
)
# And what each of phase 0's three refusals DOES, which is the half a presence check cannot see.
# The rules above ask that `if (!existsSync(PYTHON))`, `if (!existsSync(MAKE_BUNDLE))` and the
# BUNDLE.json post-condition be THERE; nothing in this repository read the branch each one opens.
# Measured: changing those three `process.exit(1)` to `process.exit(0)` -- and nothing else --
# leaves every rule here green and every rule in `test_harness_contracts.py` green, while
# `node e2e/run.mjs` ends the entire browser gate with a success status on a machine that has no
# venv, no fixture module, or a rebuild that wrote nothing. `ci.yml`'s e2e job and `release.yml`'s
# leg 5 both END on that command, so the harness's status IS the leg's, and the release gate then
# says ship over a browser pass that executed zero specs.
#
# This is `test_harness_contracts.py::_failure_branch`'s own rule -- it holds the `!loaded` branch
# to a non-zero exit and its synthetic violation is literally that replacement -- but its scan is
# bounded to the run between the post-restart health poll and the phase-2 log, so all three of
# phase 0's refusals sit above it and nobody looked. The edit is the one somebody makes after the
# interpreter refusal fires on a runner, which it has already done once (M416-C1-D3-04).
#
# A BARE `process.exit()` is not a refusal: Node exits with `process.exitCode`, which is 0 unless
# something set it, so it is the same green wearing a shorter name.
# [decision 299; M4.16 cycle 4, M416-C4-D3-01]
_EXIT_STATUS = re.compile(r"process\.exit\(\s*([^)]*)\)")
_THROWS = re.compile(r"\bthrow\b")


def _refuses(source: str, head: int) -> bool:
    """Whether the block opening at or after `head` leaves the process with a non-zero status."""
    start, end = _span(source, head, "{", "}")
    if start < 0:
        return False
    body = source[start:end]
    return bool(_THROWS.search(body)) or any(
        code.strip() not in ("0", "") for code in _EXIT_STATUS.findall(body)
    )


def _rebuild_problems(source: str) -> list[str]:
    """Whether `e2e/run.mjs` builds the fixture it then measures, and refuses if it cannot.

    Three things, and the third is the one that matters. Rebuilding is easy; the failure branch is
    what makes it an instrument. A harness that tried the venv, found nothing and carried on would
    be exactly the state decision 299 was taken to end -- measuring last month's fixture -- with
    one more line of code claiming otherwise.

    Two more since review cycle 1, and they are one sentence about the rebuild's own edges: it
    empties the directory first, because `make_bundle` overlays rather than replaces, and it
    checks that a bundle appeared, because BUILD_FIXTURE's report is a `print` that runs whatever
    happened. Neither is implied by rebuilding, and both are ways of measuring a tree the harness
    did not build. [M4.16 cycle 1, M416-C1-D3-03]

    And since review cycle 4, what those three refusals DO rather than only that they are written.
    All three were held to their presence alone, which made the sentence above -- "the failure
    branch is what makes it an instrument" -- a claim this function did not assert.
    [M4.16 cycle 4, M416-C4-D3-01]
    """
    problems = []
    rebuild = _REBUILD.search(source)
    reset = _RESET.search(source)
    if rebuild is None:
        problems.append(
            "run.mjs runs no unconditional fixture build: the statement decision 299 is about is "
            "gone, or it is now inside a try, a branch or a comment - any of which leaves the "
            "harness measuring whatever data/import happens to hold, with the constants above it "
            "still saying otherwise"
        )
    if _BUILDS_THE_IMPORT_DIR.search(source) is None:
        problems.append(
            "run.mjs's build program no longer writes make_bundle's bundle into data/import: the "
            "harness imports that directory, so a rebuild aimed anywhere else is a rebuild of "
            "something nothing measures"
        )
    empties = _EMPTIES.search(source)
    if empties is None or _REMOVES.search(source) is None:
        problems.append(
            "run.mjs does not empty data/import before rebuilding it: make_bundle overlays rather "
            "than replaces, so a file another bundle left there survives the rebuild, is "
            "inventoried into the BUNDLE.json the rebuild writes, validates clean, and is read as "
            "fixture content by everything downstream"
        )
    if _THE_TARGET.search(source) is None:
        problems.append(
            "run.mjs's IMPORT_DIR is no longer data/import: the harness imports that directory, "
            "so a clearing aimed anywhere else leaves the overlay exactly where it was"
        )
    if empties and rebuild:
        clearing = source[empties.start():rebuild.start()]
        if _CLEARING_CATCH.search(clearing) and not _CLEARING_REFUSES.search(clearing):
            problems.append(
                "run.mjs catches a failure to clear data/import and carries on: `force: true` "
                "suppresses ENOENT and nothing else, so what reaches that catch is a file the "
                "container wrote, and a rebuild overlaid on it is inventoried into its own "
                "BUNDLE.json, validates clean and is measured as fixture content. The catch "
                "exists to name the command that fixes it, not to continue past it"
            )
    if empties and rebuild and empties.start() > rebuild.start():
        problems.append(
            "run.mjs empties data/import AFTER rebuilding it: the clearing has then deleted the "
            "fixture it was there to make room for"
        )
    post = _POST_CONDITION.search(source)
    if post is None:
        problems.append(
            "run.mjs takes no post-condition on the rebuild: BUILD_FIXTURE ends in an "
            "unconditional print, so a build that wrote nothing still reports a bundle and the "
            "import falls through to whatever archive is lying in the directory"
        )
    elif not _refuses(source, post.end() - 1):
        problems.append(
            "run.mjs finds no BUNDLE.json after the rebuild and leaves the process with a "
            "success status: the check then only PRINTS that nothing was built, and the harness "
            "goes on to end the browser gate green over a fixture that does not exist"
        )
    if post and rebuild and post.start() < rebuild.start():
        problems.append(
            "run.mjs checks for BUNDLE.json BEFORE the rebuild that writes it: the check then "
            "reads the previous run's bundle and says nothing about this one"
        )
    if reset is None:
        problems.append("run.mjs no longer runs reset.mjs, so this guard is reading nothing")
    if rebuild and reset and rebuild.start() > reset.start():
        problems.append(
            "run.mjs rebuilds the fixture AFTER resetting the stack: the reset is what makes the "
            "next boot a first boot, so a bundle written after it is one the wizard has been "
            "offered already"
        )
    for interpreter in ("Scripts", "bin"):
        if f"'{interpreter}'" not in source:
            problems.append(
                f"run.mjs does not name the {interpreter} half of the backend venv: the fixture "
                "must be built by the interpreter the package is installed into, on both platforms"
            )
    # Both, separately. One check standing for two was the first draft, and softening either one
    # left it satisfied by the other -- a guard that cannot see half of what it is about.
    for subject in ("PYTHON", "MAKE_BUNDLE"):
        refusal = f"if (!existsSync({subject}))"
        head = source.find(refusal)
        if head < 0:
            problems.append(
                f"run.mjs does not check that {subject} is there before it runs it: a harness "
                "that falls back to what is on disk is the defect itself"
            )
        elif not _refuses(source, head + len(refusal)):
            problems.append(
                f"run.mjs finds no {subject} and leaves the process with a success status: the "
                "refusal is then a message rather than a refusal, and `node e2e/run.mjs` -- which "
                "is the whole of ci.yml's e2e job and of release.yml's leg 5 -- reports a green "
                "browser pass having executed no specs at all"
            )
    return problems


def test_the_browser_harness_rebuilds_the_fixture_it_measures():
    """Only CI ever rebuilt `data/import`. The local harness -- the one `docs/TESTING.md` makes
    the canonical full run, and the one every milestone's browser gate is read off -- imported
    whatever was there, so "is the fixture current?" lived in a person's head and a stale one cost
    a single session six round trips. [decision 299]"""
    assert _rebuild_problems(_read(RUNNER)) == []


def test_the_fixture_rebuild_uses_the_backend_venv_and_not_whatever_python_resolves_to():
    """`make_bundle` builds a torch-shaped bundle out of the installed package, and the `python`
    on PATH is a household's system interpreter as often as not. The path is chosen on
    `process.platform`, because a Windows venv puts its interpreter in `Scripts` and every other
    platform in `bin` -- the same split `docs/TESTING.md`'s command line carries."""
    source = _read(RUNNER)
    assert "process.platform === 'win32'" in source
    assert "python.exe" in source and "'.venv'" in source
    # And in the other checkout, which is `env.mjs`'s lesson one value over. The roadmap's
    # parallel lanes put a WORKTREE per milestone on one machine and a worktree need not carry
    # its own venv -- the M4.16 lane has none and runs on the main checkout's, so a harness that
    # looked only beside itself would refuse to start in the lane it was written for.
    assert "--git-common-dir" in source, (
        "run.mjs looks for the interpreter only in its own checkout: a lane that shares the main "
        "checkout's venv cannot run the browser suite at all"
    )


# The one statement decision 299 is about, as it stands in the file, so the mutations below are
# edits to it rather than to a paraphrase of it.
_THE_REBUILD = "execFileSync(PYTHON, ['-c', BUILD_FIXTURE], { cwd: ROOT, stdio: 'inherit' });"
_THE_RESET = "execFileSync('node', [join(HERE, 'reset.mjs')], { stdio: 'inherit' });"
_THE_TARGET_DECL = "const IMPORT_DIR = join(ROOT, 'data', 'import');"
_THE_CLEARING = "for (const entry of readdirSync(IMPORT_DIR)) {"
_THE_REMOVAL = "    rmSync(join(IMPORT_DIR, entry), { recursive: true, force: true });"
_THE_BUNDLE_CHECK = "if (!existsSync(join(IMPORT_DIR, 'BUNDLE.json'))) {"


def _phase_zero_branch(source: str, refusal: str, replacement: str) -> str:
    """`refusal`'s branch with its `process.exit(1)` rewritten, and nothing else in the file.

    Bracket-matched rather than a whole-file `str.replace`, because `run.mjs` holds three more
    `process.exit` calls and one of them -- the `!loaded` refusal -- is the one
    `test_harness_contracts.py` already holds. A mutation that broke that one too would be proved
    by the wrong guard. [M4.16 cycle 4, M416-C4-D3-01]
    """
    head = source.index(refusal)
    start, end = _span(source, head + len(refusal), "{", "}")
    body = source[start:end].replace("process.exit(1)", replacement)
    return source[:start] + body + source[end:]


@pytest.mark.parametrize(
    "mutate, needle",
    [
        pytest.param(lambda s: s.replace("make_bundle", "nothing_at_all"),
                     "no longer writes make_bundle's bundle into data/import",
                     id="the rebuild removed"),
        pytest.param(lambda s: s.replace("if (!existsSync(PYTHON))", "if (false)"),
                     "does not check that PYTHON is there",
                     id="the interpreter check softened"),
        # Review cycle 1. Every one of these leaves `MAKE_BUNDLE`, `BUILD_FIXTURE` and both
        # `existsSync` refusals exactly where they are, which is why the word-search version of
        # this guard passed all of them -- and each is an edit somebody has a reason to make.
        # [M4.16 cycle 1, M416-C1-D3-01]
        pytest.param(lambda s: s.replace(
                         _THE_REBUILD,
                         "try {\n  " + _THE_REBUILD + "\n} catch {\n"
                         "  console.warn('no rebuild; measuring whatever data/import holds');\n}"),
                     "runs no unconditional fixture build",
                     id="the rebuild failure swallowed"),
        pytest.param(lambda s: s.replace("execFileSync(PYTHON, ['-c', BUILD_FIXTURE]",
                                         "spawnSync(PYTHON, ['-c', BUILD_FIXTURE]"),
                     "runs no unconditional fixture build",
                     id="the rebuild switched to spawnSync with nobody reading the status"),
        pytest.param(lambda s: s.replace(
                         _THE_REBUILD,
                         "if (!existsSync(join(ROOT, 'data', 'import'))) " + _THE_REBUILD),
                     "runs no unconditional fixture build",
                     id="the rebuild made conditional on the directory being absent"),
        pytest.param(lambda s: s.replace(_THE_REBUILD, "// " + _THE_REBUILD),
                     "runs no unconditional fixture build",
                     id="the rebuild commented out"),
        pytest.param(lambda s: s.replace("fx.make_bundle(pathlib.Path('data/import'))",
                                         "fx.make_bundle(pathlib.Path('data/scratch'))"),
                     "no longer writes make_bundle's bundle into data/import",
                     id="the rebuild aimed at a directory nothing imports"),
        pytest.param(lambda s: (s.replace(_THE_REBUILD + "\n", "")
                                 .replace(_THE_RESET, _THE_RESET + "\n" + _THE_REBUILD)),
                     "rebuilds the fixture AFTER resetting the stack",
                     id="the rebuild moved below the reset"),
        # ROUND 3, on the rebuild's two edges. Each of these leaves the rebuild unconditional,
        # loud on both refusals and aimed at data/import -- every rule above passes them -- and
        # each ends with the harness measuring a tree it did not build. The first two are the
        # file as it stood before this cycle. [M4.16 cycle 1, M416-C1-D3-03]
        pytest.param(lambda s: s.replace(_THE_CLEARING, "for (const entry of []) {"),
                     "does not empty data/import before rebuilding it",
                     id="the clearing loop reads nothing"),
        pytest.param(lambda s: s.replace(
                         _THE_REMOVAL,
                         "    if (entry !== 'BUNDLE.json') " + _THE_REMOVAL.strip()),
                     "does not empty data/import before rebuilding it",
                     id="the clearing made conditional to spare a staged bundle"),
        pytest.param(lambda s: s.replace(_THE_TARGET_DECL,
                                         "const IMPORT_DIR = join(ROOT, 'data', 'scratch');"),
                     "IMPORT_DIR is no longer data/import",
                     id="the clearing aimed at a directory nothing imports"),
        pytest.param(lambda s: (s.replace(_THE_REBUILD + "\n", "", 1)
                                 .replace(_THE_TARGET_DECL,
                                          _THE_REBUILD + "\n" + _THE_TARGET_DECL)),
                     "empties data/import AFTER rebuilding it",
                     id="the clearing moved below the rebuild"),
        pytest.param(lambda s: s.replace(_THE_BUNDLE_CHECK, "if (false) {"),
                     "takes no post-condition on the rebuild",
                     id="the post-condition removed"),
        # Review cycle 2, and the arm a person has a reason to soften: EACCES on a developer's box
        # is annoying, and the cheapest way to stop being annoyed is to stop refusing.
        # [M4.16 cycle 2, M416-C2-D3-01]
        pytest.param(lambda s: s.replace("    throw new Error(", "    console.warn("),
                     "catches a failure to clear data/import and carries on",
                     id="the clearing's failure swallowed"),
        pytest.param(lambda s: (s.replace(_THE_REBUILD + "\n", "", 1)
                                 .replace(_THE_RESET,
                                          _THE_REBUILD + "\n" + _THE_RESET)),
                     "checks for BUNDLE.json BEFORE the rebuild",
                     id="the post-condition left above the rebuild"),
        # Review cycle 4, and it is the same edit the interpreter case above is about, made one
        # word further in: not "stop asking" but "stop minding the answer". Every rule above this
        # cycle passed all four -- the refusals are still written, still loud on the console, still
        # aimed at the right subjects -- while `node e2e/run.mjs` ends the browser gate with a
        # success status having executed nothing. This is
        # `test_harness_contracts.py::_failure_branch`'s synthetic violation, at the one phase its
        # scan begins above. [decision 299; M4.16 cycle 4, M416-C4-D3-01]
        pytest.param(lambda s: _phase_zero_branch(s, "if (!existsSync(PYTHON))",
                                                  "process.exit(0)"),
                     "finds no PYTHON and leaves the process with a success status",
                     id="the interpreter refusal's exit status turned green"),
        pytest.param(lambda s: _phase_zero_branch(s, "if (!existsSync(MAKE_BUNDLE))",
                                                  "console.warn('carrying on')"),
                     "finds no MAKE_BUNDLE and leaves the process with a success status",
                     id="the fixture-module refusal demoted to a warning"),
        pytest.param(lambda s: _phase_zero_branch(
                         s, "if (!existsSync(join(IMPORT_DIR, 'BUNDLE.json')))",
                         "process.exit(0)"),
                     "finds no BUNDLE.json after the rebuild and leaves the process with a "
                     "success status",
                     id="the post-condition's exit status turned green"),
    ],
)
def test_the_rebuild_guard_sees_a_harness_that_measures_what_it_finds(mutate, needle):
    """The synthetic violations are the file as it stood before this milestone and one edit from
    it. The second is the one to be afraid of: `existsSync` returning false on a developer's box
    is annoying, and the cheapest way to stop being annoyed is to stop asking."""
    assert _rebuild_problems(_read(RUNNER)) == [], "the guard does not pass the runner it describes"
    problems = _rebuild_problems(mutate(_read(RUNNER)))
    assert problems, "the guard passed a regressed runner"
    assert needle in " ".join(problems), problems


# Where a workflow's `uv venv` puts the interpreter it then installs the package into, and where
# `run.mjs` looks for one. The two sets were disjoint: every `uv venv` in this directory is bare,
# so it creates `<workspace>/.venv`, and the first draft of decision 299's block probed
# `backend/.venv` and nothing else. `run.mjs` therefore exited 1 at its first statement on every
# runner in the project -- ci.yml's e2e job on every push and the release gate's leg 5 on every
# dispatch -- for a reason no guard here could see, because both of the existing rebuild guards
# read the two CALLS and their order rather than whether the interpreter can be found.
# [M4.16 cycle 1, M416-C1-D3-04]
_UV_VENV = re.compile(r"^\s*-\s*run:\s*uv venv\b(?P<arg>[^\n#]*)$", re.M)
_VENV_DIRS = re.compile(r"^const VENV_DIRS = (?P<body>\[.+\]);$", re.M)


def _venv_dirs_probed(source: str) -> set[str]:
    """The venv directories `run.mjs` names, each as a repo-relative path."""
    match = _VENV_DIRS.search(source)
    if match is None:
        return set()
    return {"/".join(re.findall(r"'([^']+)'", group))
            for group in re.findall(r"\[([^\[\]]+)\]", match.group("body"))}


def _venv_dirs_created(workflow: str) -> set[str]:
    """The venv directories a workflow's `uv venv` calls create. Bare means the checkout root."""
    return {(m.group("arg").strip() or ".venv") for m in _UV_VENV.finditer(workflow)}


def _harnesses():
    """Every workflow in this repository that runs the browser harness."""
    return [p for p in sorted((REPO / ".github" / "workflows").glob("*.yml"))
            if "node e2e/run.mjs" in _read(p)]


def test_the_harness_finds_the_interpreter_every_workflow_that_runs_it_installs():
    """Decision 299 says the harness refuses rather than falling back. That is only an instrument
    if the thing it looks for is the thing the runner has.

    Read off the workflows rather than restated here: a job that moves its venv, or a third
    workflow that learns to run the harness, is then covered by this the day it lands instead of
    the day somebody dispatches it and reads `no backend interpreter at ...`.
    """
    probed = _venv_dirs_probed(_read(RUNNER))
    assert probed, "e2e/run.mjs no longer declares VENV_DIRS, so this guard is reading nothing"
    harnesses = _harnesses()
    assert harnesses, "no workflow runs `node e2e/run.mjs`, so this guard is reading nothing"
    for workflow in harnesses:
        created = _venv_dirs_created(_read(workflow))
        missing = sorted(created - probed)
        assert not missing, (
            f"{workflow.name} installs the backend into {missing} and e2e/run.mjs probes only "
            f"{sorted(probed)}: decision 299's rebuild refuses rather than falling back, so the "
            "harness exits 1 at its first statement and this leg never reaches Playwright"
        )


def test_the_interpreter_guard_sees_a_harness_that_looks_in_one_place():
    """The shape that shipped, kept as the synthetic violation it was."""
    source = _read(RUNNER)
    assert _venv_dirs_probed(source) == {"backend/.venv", ".venv"}
    narrowed = source.replace("const VENV_DIRS = [['backend', '.venv'], ['.venv']];",
                              "const VENV_DIRS = [['backend', '.venv']];")
    assert narrowed != source, "e2e/run.mjs no longer spells VENV_DIRS the way this guard reads it"
    assert _venv_dirs_probed(narrowed) == {"backend/.venv"}
    for workflow in _harnesses():
        assert _venv_dirs_created(_read(workflow)) - _venv_dirs_probed(narrowed), (
            f"{workflow.name} creates no venv the narrowed harness would miss, so the guard above "
            "has stopped being about anything"
        )


# A sentence in this suite that still reasons from the harness leaving `data/import` where it
# found it. Decision 299 inverted that, and two tests had built their assertions ON it: the
# fixture's two absent artifacts were argued from it in `test_artifact_store.py`, and
# `test_bundle_shapes.py` told the next reader to compare shapes by hand BECAUSE the harness
# supposedly never imported what `make_bundle` writes. A milestone that ships a guard requiring a
# comment to name a file that exists has the same defect one altitude down when a comment names a
# behaviour the same milestone reversed.
#
# Over the whole file rather than line by line, and the gap admits a line break, a comment marker
# and a backtick: the sentence this was written for wrapped -- "`e2e/run.mjs` does not rebuild" at
# the end of one comment line and "`data/import`" at the start of the next -- so a per-line reader
# would have passed the very comment it exists to catch. Measured: restored verbatim, a per-line
# version found one of the two and reported it as a clean sweep.
#
# The pattern cannot match its own source: the gap here is a character CLASS, and `[` is not in
# it, so the line you are reading is not one of its own offenders -- which is why this can scan
# every file in the directory including this one.
# [decision 299; M4.16 cycle 1, M416-C1-D3-06]
_STALE_REBUILD = re.compile(r"does not rebuild[\s#`'\"]*data/import", re.I)


def test_no_test_in_this_suite_reasons_from_a_harness_that_leaves_the_fixture_alone():
    """The two sentences decision 299 falsified, and the ones after them.

    Scoped to the directories that reason about the fixture -- `backend/tests` and `ops` -- because
    that is where the claim was made and where an assertion can be built on it. `e2e/run.mjs` is
    not scanned: it is the file the claim is ABOUT, and `_rebuild_problems` above holds it.
    """
    offenders = []
    for folder in ("backend/tests", "ops"):
        for path in sorted((REPO / folder).glob("*.py")):
            source = _read(path)
            for match in _STALE_REBUILD.finditer(source):
                line = source[: match.start()].count("\n") + 1
                # Collapsed, because a wrapped claim would otherwise print its own line break
                # into the middle of the report it is being named in.
                offenders.append(f"{path.name}:{line}: {' '.join(match.group(0).split())}")
    assert not offenders, "\n".join(
        [
            "these lines still tell a reader the browser harness leaves data/import as it found",
            "it, which decision 299 ended:",
            *(f"  {line}" for line in offenders),
            "",
            "run.mjs phase 0 rebuilds that directory from make_bundle.py before every browser run,",
            "so a test reasoning from the old behaviour is reasoning from a premise this",
            "repository retired - cite decision 299 and say what the rebuild means for the claim.",
        ]
    )


def test_the_harness_keeps_phase_ones_report_for_the_gate():
    """Playwright empties `outputDir` at the START of a run, and `run.mjs` runs it twice, so
    phase 1 alone closes 8 shipped rows whose report phase 2's own start would delete. Without
    this the release gate would read every one of them as evidence that never ran -- and would be
    right about the report while wrong about the build, which is the worst answer an instrument
    can give."""
    source = _read(RUNNER)
    assert "report-phase-1.json" in source, (
        "run.mjs does not preserve phase 1's JSON report; ops/coverage_gate.py is fed both"
    )
    assert source.index("writeFileSync(PHASE_ONE_REPORT") > source.index("--grep-invert"), (
        "phase 1's report is written back before phase 2 runs, so phase 2's own start deletes it"
    )


# --- M4.16 review cycle 4: the fourth count these instruments publish about themselves ---------
#
# `_FAMILY_SIZE` above is a fixed clause over three NAMED families, so a sentence sizing phase 1
# escapes it even inside this module -- and three files carry that sentence: the docstring above,
# `e2e/run.mjs` over the two report paths, and `release.yml` over the gate invocation. All three
# said seven, which is how many distinct first-boot IDS the map names -- seven ids over fourteen
# references, because four rows name `a bundle-less app boots, serves the wizard, and says so`
# alone. What the sentence claims is ROWS, and the rows are eight -- the same eight at `HEAD`, so
# this was wrong on the day it was typed rather than stale after a milestone. Same defect as the
# three above and the same repair, because the figure IS the argument for the write-back: a
# maintainer deciding whether that complexity still earns its place reads it, and nothing held it.
# [decision 184; decision 299; M4.16 cycle 4, CG-C4-COUNT-02]
PHASE_ONE_SPEC = "e2e/specs/01-first-boot.spec.js"


# --- M4.16 review cycle 5: the one published figure block nothing re-derived --------------------
#
# Decision 313 ran leg 2 once over a JUnit a real pytest wrote and pasted the console into
# `docs/RELEASE.md` section 2.1, and that transcript went stale inside the diff that published it:
# it said 92 and 2071 over a command already printing 95 and 2076, because the cycle that added
# tests to the file leg 2 was run over never re-ran the command the block prints. Every other
# count this milestone publishes is re-derived off the live map - the vitest ids, the three
# silent-skip family sizes, phase 1's size - and this one, in the section whose stated job is that
# a partial recorded as a pass is refused, was held by nothing. A maintainer auditing exit
# criterion 3 re-runs the one command the record prints, gets different numbers, and the block's
# own argument then reads as arithmetic nobody checked.
#
# Four of the five console figures are derivable here and are derived: what the parser READ is one
# result per test function in this file, what it CONFIRMED is one per (row, named test) pair, the
# vitest line is the map's own invisible count, and the failure summary is `check()` run over
# exactly that confirmation set. The fifth - pytest's `N passed` - is a count of parameterised
# CASES, which no static rule can take without evaluating every `parametrize` list; it stays
# inside the dated console paste, and the prose around it claims nothing about it.
# [decision 184; decision 313; M4.16 cycle 5: M416-C5-REL-01, REL-C4-09]
RECORD = REPO / "docs" / "RELEASE.md"


# --- M4.16 review cycle 3: the shell three legs' exit codes rest on -----------------------------
#
# A fifth way a leg stops being able to say no, and the only one written outside the leg's own
# command: a pipe. `python ops/coverage_gate.py ... | tee .reports/coverage-gate.txt` exits with
# TEE's status and not the gate's, and GitHub's default shell for a `run:` body is `bash -e {0}`,
# which supplies no `pipefail`. Under it leg 2 reports green over a coverage gate that exited 1,
# and legs 3 and 4 do the same over the real-bundle scripts and over the drill's health read.
# `defaults.run.shell: bash` is the whole of what turns that into `bash --noprofile --norc -eo
# pipefail {0}`; release.yml argues it in three comment lines of its own, and nothing held it.
#
# MEASURED on the file that ships: with the `defaults:` block deleted, `_leg_problems`,
# `_trigger_problems`, `_job_gate_problems` and `_skip_green_problems` all returned [] -- the one
# setting three legs' exit codes rest on could be dropped in silence while every guard in this
# file stayed green. That is this file's own subject at the one altitude it had not looked: not
# what a leg runs, but what reads the answer.
#
# Conditional on the pipe rather than a flat demand for the block, in `_LEG_NARROWED`'s idiom: a
# workflow that stopped teeing would not need the setting, and a guard that asked for it anyway
# would be a line nobody could remove honestly. The premise is asserted beside the rule, so a file
# whose legs stop piping says so rather than going on holding a dead setting.
# [M4.16 cycle 3, M416-C3-REL-01]
_PIPES_INTO = re.compile(r"(?<!\|)\|(?!\|)")


def _logical_lines(lines: list[str]) -> list[str]:
    """A shell body's commands, each continuation joined back into the line it continues.

    Leg 2's invocation is five physical lines ending in backslashes and its `| tee` is the last of
    them, so a per-line read finds the command on one line and the pipe on another and connects
    neither to the other.
    """
    out: list[str] = []
    pending = ""
    for line in lines:
        text = line.strip()
        if pending:
            text, pending = f"{pending} {text}", ""
        if text.endswith("\\"):
            pending = text[:-1].strip()
            continue
        out.append(text)
    if pending:
        out.append(pending)
    return out


# A step's OWN `shell:`, at either column its key can occupy, read for `_STEP_GATE`'s reason: a
# step-level `shell:` overrides the job default for that step's body, and the override wins. The
# custom-shell form `bash {0}` is matched too -- it is the one that gives plain bash with NEITHER
# `-e` nor `pipefail`, so a sixty-line leg becomes a body whose every failing command is
# non-fatal and whose status is the last command's. [M4.16 cycle 4, M416-C4-REL-03]
_STEP_SHELL = re.compile(r"^(?: {6}- | {8})shell:\s*(?P<name>.+?)\s*$", re.M)


def _piped_legs(job: str) -> dict[int, tuple[str, str | None]]:
    """Leg number -> (the leg's own command a pipe stands in front of, that STEP's own `shell:`).

    `_unconditional` first, for `_runs_the_command`'s reason one rule over: a pipe inside a branch
    the body may never reach is not one the step's exit code depends on.

    The step's shell travels with the command rather than being looked up afterwards, because the
    fact is about a step: a leg is two steps here -- the body and its artifact upload -- and the
    only shell that answers for the pipe is the one declared on the step the pipe is in.
    """
    piped: dict[int, tuple[str, str | None]] = {}
    for step in _steps(job):
        leg = _LEG.search(step)
        if leg is None:
            continue
        number = int(leg.group("n"))
        shell = _STEP_SHELL.search(step)
        for line in _logical_lines(_unconditional(_run_lines(step))):
            for command in _LEG_COMMANDS.get(number, ()):
                _head, found, rest = line.partition(command)
                if found and _PIPES_INTO.search(rest):
                    piped.setdefault(number, (command, shell.group("name") if shell else None))
    return piped


def _default_shell(job: str) -> str | None:
    """The `shell:` this job gives every `run:` body that declares none of its own.

    Read off `_job_head`, which is where a job default lives. It is only HALF the answer and the
    rule below says so: this function's first draft was the whole rule, on the argument that "a
    step-level `shell:` answers for one body, and the three bodies that pipe are three different
    steps" -- which is the reason to read the steps AS WELL, not instead. A step's own `shell:`
    overrides the default for exactly the body whose exit code the pipe decides, so the one
    setting legs 2, 3 and 4 rest on could be dropped for precisely the leg that matters, in the
    step itself, with every guard in this file green. Measured, on the file that ships.
    [M4.16 cycle 4, M416-C4-REL-03]

    Blank lines pass through the block because `_jobs` BLANKS comment lines rather than removing
    them, and the three lines arguing this setting sit between `run:` and the `shell:` they
    introduce.
    """
    block = re.search(r"^    defaults:\n(?P<body>(?:^(?: {6}.*)?\n)*)", _job_head(job), re.M)
    if block is None:
        return None
    run = re.search(r"^      run:\n(?P<body>(?:^(?: {8}.*)?\n)*)", block.group("body"), re.M)
    if run is None:
        return None
    shell = re.search(r"^ {8}shell:\s*(?P<name>\S+)\s*$", run.group("body"), re.M)
    return shell.group("name") if shell else None


def _shell_problems(job: str) -> list[str]:
    """Whether every leg that pipes its own command runs under a shell that supplies `pipefail`.

    The EFFECTIVE shell, which is the step's own when it declares one and the job's default
    otherwise. Exactly `bash` passes: GitHub expands that to `bash --noprofile --norc -eo pipefail
    {0}`, and every other spelling loses one half or both -- `sh` is `sh -e {0}`, errexit without
    pipefail, so a teed command answers with `tee`'s status; and the custom-shell form `bash {0}`
    is plain bash with neither, so a leg's every failing command is non-fatal and the step's
    status is its last command's.
    """
    piped = _piped_legs(job)
    if not piped:
        return [
            "no leg hands its own command's exit code to a pipe any more, so this rule holds a "
            "setting the workflow no longer needs. Re-read it against the file rather than "
            "keeping it: a guard whose premise has gone is a line nobody can remove honestly."
        ]
    default = _default_shell(job)
    problems = []
    for number, (command, declared) in sorted(piped.items()):
        if (default if declared is None else declared) == "bash":
            continue
        where = (
            f"this job declares `defaults.run.shell: {default}`, and GitHub's own default for a "
            "`run:` body is `bash -e {0}`"
            if declared is None else
            f"leg {number}'s own STEP declares `shell: {declared}`, which overrides the job "
            "default for exactly the body whose exit code the pipe decides"
        )
        problems.append(
            f"leg {number} pipes its own command (`{command}`) and {where} -- so no `pipefail` "
            "reaches it, the runner reads `tee`'s exit code, and that leg passes over a command "
            "that said no. Leg 2 is the loudest of the three, because the executed-coverage gate "
            "is the one instrument this milestone adds. `shell: bash`, and nothing else, is what "
            "supplies `-eo pipefail`."
        )
    return problems


def test_the_release_job_declares_the_shell_its_piped_legs_need():
    """A leg that pipes its command answers with the PIPE's exit code unless `pipefail` is on.

    [section 12 exit criteria; docs/RELEASE.md; row `platform-release-gate-runs-every-leg`]
    """
    assert _shell_problems(_release_job(_read(RELEASE))) == []


# The rule fed its own violation, five ways, in `_BODY_MUTATIONS`' idiom. Each was run against the
# real file first and left every other guard here green, which is why this is a new rule rather
# than a widening of one that already looked. [M4.16 cycle 3, M416-C3-REL-01]
#
# The last two are review cycle 4's, and they are the rule's own scope rather than its subject:
# the job default is still `bash` and `_default_shell` still answers `'bash'`, while the leg whose
# exit code the pipe decides runs under something else. Leg 2 is the one with the least margin --
# `docs/RELEASE.md` records it as "the one with no fallback reading at all" -- and leg 4 is the
# worst body to lose `-e` in, because its last statement is itself a `tee`.
# [M4.16 cycle 4, M416-C4-REL-03]
_SHELL_MUTATIONS = (
    ("the whole defaults block deleted",
     re.compile(r"^    defaults:\n      run:\n(?:        #[^\n]*\n)*        shell: bash\n", re.M),
     "", None),
    ("the default shell demoted to one without pipefail",
     re.compile(r"^        shell: bash$", re.M), "        shell: sh", "sh"),
    ("defaults kept and `run:` dropped from under it",
     re.compile(r"^      run:\n(?:        #[^\n]*\n)*        shell: bash\n", re.M),
     "      env:\n        TZ: UTC\n", None),
    ("the coverage gate's own step opted out of the job default",
     re.compile(r"^      - name: leg 2 of 5 - the executed-coverage gate\n", re.M),
     "      - name: leg 2 of 5 - the executed-coverage gate\n        shell: sh\n", "bash"),
    ("the restore drill given the custom-shell form, which has neither -e nor pipefail",
     re.compile(r"^      - name: leg 4 of 5 - the restore drill[^\n]*\n", re.M),
     "      - name: leg 4 of 5 - the restore drill at stack level\n        shell: bash {0}\n",
     "bash"),
)


@pytest.mark.parametrize(
    "pattern, replacement, expected",
    [mutation[1:] for mutation in _SHELL_MUTATIONS],
    ids=[mutation[0] for mutation in _SHELL_MUTATIONS],
)
def test_the_shell_guard_reads_the_declaration_the_runner_reads(pattern, replacement, expected):
    """Five ways the declaration goes away, and the reader has to see each of them.

    The piped legs are re-asserted inside the mutation, because a mutation that also removed them
    would satisfy the rule by emptying its premise instead of by breaking its subject. And the
    RULE is asserted failing, not only the reader: the last two leave `_default_shell` answering
    `'bash'`, which is the whole of what they demonstrate.
    """
    text = _read(RELEASE)
    mutated = pattern.sub(replacement, text)
    assert mutated != text, (
        "this mutation no longer matches release.yml, so it proves the rule against a file shape "
        "that has gone -- restate it from the file rather than deleting it"
    )
    job = _release_job(mutated)
    assert _piped_legs(job), "the mutation also removed the piped legs, so it proves nothing"
    assert _default_shell(job) == expected
    assert _shell_problems(job), "the shell rule passed a workflow whose piped leg lost pipefail"


# --- the build context the workflow's own output lands in ---------------------------------


# Every repository-root dot-directory the workflow reads or writes, found in the workflow rather
# than named here: the day it writes somewhere else this asks about the new directory instead of
# going on holding the old one. The lookbehind is what keeps it to the ROOT -- `.reports/browser/
# .results/report.json` is one tree and one entry, not two, and `.dockerignore` has no anchoring
# syntax so a nested name would be a rule about the wrong thing. [M4.16 cycle 5, M416-C5-DOCKER-01]
_WORKFLOW_SCRATCH = re.compile(r"(?<![\w./-])(\.[a-z][\w-]*)/")


def test_no_directory_the_release_workflow_writes_enters_the_build_context():
    """`.gitignore` gained `.reports/` this milestone and `.dockerignore` did not.

    Legs 1-3 write `.reports/junit-release.xml`, the Playwright report leg 2 downloads and three
    tee'd logs; leg 4 then runs `docker compose up -d --build` from that same workspace, and
    `context: .` ships whatever is not ignored. So the whole tree goes to the daemon on the
    self-hosted corpus runner, every scheduled run, for no build purpose. Nothing reaches the
    IMAGE -- `ops/backend.Dockerfile` COPYs four named paths -- which is why this is context weight
    and host test state rather than a leak, and it is precisely the class `.dockerignore`'s own
    first line exists for: "keep the build context small and keep host state out of the image".

    Latent while the workflow has never been dispatched (docs/RELEASE.md section 2.1), which is why
    it has cost nothing yet rather than why the file is right. Held as a rule over the workflow's
    own paths, so `e2e/run.mjs`'s `up -d` without `--build` needs no exception written for it.
    [M4.16 cycle 5, M416-C5-DOCKER-01]
    """
    text = _read(RELEASE)
    assert "--build" in text, (
        "the release workflow no longer builds an image, so nothing it writes enters a build "
        "context and this rule is about nothing -- delete it with the leg that stopped building"
    )
    ignore = _ignored((REPO / ".dockerignore").read_text(encoding="utf-8"))
    leaked = sorted(
        name
        for name in set(_WORKFLOW_SCRATCH.findall(text))
        if not ({name, f"{name}/"} & ignore)
    )
    assert not leaked, (
        f"the release workflow writes into {leaked} and `.dockerignore` does not exclude them, so "
        "leg 4's `up -d --build` uploads the run's own reports to the docker daemon. Add each to "
        "the \"not needed to build or run\" block beside `e2e/` and `.github/`."
    )


def test_the_build_context_guard_sees_the_workflows_report_directory_come_back():
    """The state this repaired, restored: the entry deleted from `.dockerignore` alone.

    Asserted against the file rather than a string, because the entry is one line and the rule is
    that its absence is visible -- which is exactly what was true of it until this cycle.
    [M4.16 cycle 5, M416-C5-DOCKER-01]
    """
    text = _read(RELEASE)
    ignore_text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    dropped = re.sub(r"(?m)^\.reports/$\n", "", ignore_text)
    assert dropped != ignore_text, (
        "`.dockerignore` no longer carries `.reports/`, which is the entry this case removes "
        "to prove the rule -- the state it restores IS the tree, and the guard above is what "
        "says so"
    )
    assert ".reports" not in _ignored(dropped), "only the one line went"
    leaked = sorted(
        name
        for name in set(_WORKFLOW_SCRATCH.findall(text))
        if not ({name, f"{name}/"} & _ignored(dropped))
    )
    assert leaked == [".reports"], f"the guard read a context carrying the run's reports: {leaked}"
