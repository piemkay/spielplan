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

import ast
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
# "M4.5". M4.14 through M4.16 are not in the list yet — each is added
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
#
# M4.11 follows it and takes a §12 row on M4.9's and M4.10's argument rather than M4.6's and
# M4.7's: the row it repairs is one the table already had. §12's M1 row closes on "seen states
# flow both ways for both users", and that was asserted against one member, one copy per title,
# no series and no second phone — not the household §3.3 describes. Against two members, a
# library holding 'Movies' and 'Movies 4K' copies of one film, and a show whose next season
# lands: a link made without Jellyfin credentials aborts that member's sweep at its first owed
# row for the life of the install, so none of their history is ever adopted; a duplicate copy
# marked Played reverts the explicit "not seen" the person typed; a DELETE against a Series
# folder is a recursive MarkUnplayed that erases every episode's resume position, and the same
# computed folder flag un-marks a show the day it grows; a title deleted from the library keeps
# `is_owned = true` and stays in Tonight's pool; a revoked token is promoted back to "linked"
# by any sweep with nothing to push, while a 404 on every write reads exactly like a healthy
# one; and no Episode session arms a finish prompt at all, because the double emitted a shape
# no server sends. It sits after M4.10 rather than beside it because the one leak from this
# territory into the rest of the app — the Jellyfin round trip inside the verdict transaction —
# was M4.10's to move, and both milestones would otherwise edit `rate/session.py`'s push path
# in the same lines. Decisions 210-212 record the calls it needed; decision 172 had already
# ruled two of them. See docs/milestones/M4.11-plan.md.
#
# M4.12 follows it and is that shape a fourth time: it takes a §12 row (decision 224) on
# M4.9's, M4.10's and M4.11's argument rather than M4.6's and M4.7's, because what it repairs
# is the row this table has had the longest. §12's M4 criterion — "a real Friday night resolved
# by the app" — was closed against a six-title candidate pool, a household of one account and a
# guest seat no screen could take to the reveal. Against two real accounts and the shipped
# 696-title owned pool the same evening does not resolve: the pair search costs 36 s for a member
# and 126 s for a guest seat against the §6 preamble's 1.5 s per battle; the voting -> ballot
# transition exists only inside the answer handler, so one transient failure leaves the room in
# `voting` for ever with its data intact and unreachable; a pool of two or three candidates
# converges at zero answers and no route can close the room; a member who joins while the host is
# building the pool is seated into a snapshot that has no scores for them and deadlocks the same
# way; nothing ends a started room, so every other stall is permanent and recovery means SQL; two
# answers gathered on one card token are a 500 rather than a 409, and an undo interleaved with an
# answer makes every later tap a 500 for ever; the session WebSocket authenticates outside the
# dependency graph and awaits its sends inside an acquired connection, so ten stalled sockets pin
# the whole ten-connection pool; a room with any guest seat can never reach the reveal; and every
# `session_answer.latency_ms` ever written is 0, which is the instrument §14 risk 6 requires
# before anyone re-tunes the round. Its name was already in this list before it opened — three of
# its findings were pre-release fixes that ship ahead of the milestone owning the rest, which is
# what the M4.5 paragraph at the head of this block describes — so opening it moves
# `current_milestone` and adds no entry here. It sits before M4.13 because the two were built in
# parallel, in separate worktrees, and `ROADMAP-to-M5.md`'s table marks them workable in either
# order; the position is the one the pre-release rows already fixed. Decisions 214-226 record the
# calls it needed, and decisions 175 and 205 had already ruled the first of them. See
# docs/milestones/M4.12-plan.md.
MILESTONES = ["M0", "M1", "M2", "M3", "M4", "M4.5", "M4.6", "M4.7", "M4.8", "M4.9", "M4.10",
              "M4.11", "M4.12", "M4.13", "M5", "M6", "M7"]
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


# Bound rather than folded straight into the union: decision 226's guard below has to ask
# which RUNNER answers to a name, and a path prefix would be a guess about that where this
# reader is the thing that decides it.
VITEST_IDS = _vitest_ids()
KNOWN_TESTS = _pytest_ids() | _playwright_ids() | VITEST_IDS


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


def test_no_requirement_rests_on_a_vitest_id_alone():
    """Decision 226: "A row may name a vitest id, and no row may rest on one alone."

    Rule 2 asks whether a named test exists. It cannot ask which runner answers to the name,
    because `KNOWN_TESTS` is one flat union of the three -- so the half of decision 226 that does
    the work was a sentence in a decision and nothing else. Naming a vitest id *instead* of a
    Playwright or a backend one "would rest a clause on a suite `npm --prefix e2e run fresh`
    never runs": the two cheapest assertions in a frontend repair close the requirement, the gate
    prints the row as covered, and the browser suite a milestone's exit criterion is measured on
    has never executed the claim.

    The same family as the defect this milestone found in rule 2 itself -- a `pytest.skip` inside
    a registered test closed a row while asserting nothing -- and repaired at the three sites it
    had reached rather than at the rule. A rule with no guard is a convention, and a convention is
    what a coverage map exists to replace.

    Unscoped, although decision 226's cost paragraph states the rule "is stated here rather than
    applied retroactively to milestones that closed under the older reading": no row of any closed
    milestone rests on a vitest id, so the retroactive half forgives nothing today, and a
    milestone cut-off would be a second thing to keep true for a guard that has never had
    anything to forgive.
    [M4.12 review cycle 1: M412-D8-04]
    """
    resting = [
        f"{r['id']} rests on vitest ids alone: {', '.join(r['tests'])}"
        for r in REQUIREMENTS
        if r.get("tests") and all(t in VITEST_IDS for t in r["tests"])
    ]
    assert not resting, "\n".join(
        [
            *resting,
            "",
            "Decision 226 admits a vitest id BESIDE a Playwright or a backend test and never "
            "instead of one: vitest mounts a component against fixtures, and `npm --prefix e2e "
            "run fresh` -- the suite a milestone closes on -- does not run it at all.",
        ]
    )


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


# The number words the ledger's prose uses. It spells small counts and prints the id total as a
# figure, which is the house voice; both spellings are read here so the guard rules on the count
# rather than on how the sentence chose to write it.
_NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty",
]

# "-- 84 ids across twelve pytest files and two e2e specs --". One sentence per re-paste: the
# block is rewritten by the milestone it is re-pasted for rather than appended to, so a second
# match means two milestones are both claiming it and the reader cannot tell which run produced
# which figure.
_LEDGER_ID_COUNT = re.compile(
    r"(\d+|[A-Za-z]+) ids across (\d+|[A-Za-z]+) pytest files? and (\d+|[A-Za-z]+) e2e specs?"
)


def _spelled(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.index(token.lower()) if token.lower() in _NUMBER_WORDS else None


def test_the_testing_ledger_counts_the_ids_the_map_actually_holds():
    """The sibling above holds the fenced block; this holds the sentence beside it.

    Both publish a measurement and only one of them was mechanical, so the prose drifted exactly
    as the block had before M4.8: `docs/TESTING.md` said "84 ids across twelve pytest files and
    two e2e specs" for a map that held 89, because five tests were registered by a review cycle
    after the paragraph was written and nothing compared the two. The failure is the one decision
    184 legislates against from the other side -- it refuses to invent a count that no run
    produced, and this refuses to leave one standing that the map has since overtaken -- and it
    matters because CLAUDE.md sends the next reader to this file "rather than assuming status":
    an auditor checking whether a review cycle's tests were registered counts the map, reads the
    ledger, and cannot tell a stale sentence from ids added with no row to hold them.

    Scoped to `current_milestone` because the paragraph is always about the milestone the block
    was last re-pasted for, and that is what raising `current_milestone` means.
    [M4.11 review cycle 2: m411-c2-ledger-02]
    """
    matches = _LEDGER_ID_COUNT.findall(LEDGER.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"docs/TESTING.md publishes {len(matches)} 'N ids across ... pytest files and ... e2e "
        f"specs' sentences; the block is re-pasted per milestone, so exactly one is owed"
    )
    published = [_spelled(token) for token in matches[0]]
    assert None not in published, (
        f"a count in that sentence is neither a figure nor a number word: {matches[0]}"
    )

    ids = {t for r in REQUIREMENTS if r["milestone"] == CURRENT for t in r.get("tests", [])}
    files = {t.partition("::")[0] for t in ids}
    held = [
        len(ids),
        len({f for f in files if f.startswith("backend/tests/")}),
        len({f for f in files if f.startswith("e2e/specs/")}),
    ]
    assert published == held, (
        f"docs/TESTING.md's {CURRENT} paragraph publishes "
        f"{published[0]} ids across {published[1]} pytest files and {published[2]} e2e specs; "
        f"the map holds {held[0]} distinct ids across {held[1]} pytest files and {held[2]} e2e "
        "specs. Restate the sentence -- a count nobody re-derived is decision 184's defect."
    )


# "...and it takes the last `pytest.skip` out of a registered Tonight test". The qualifier is
# OPTIONAL in this pattern on purpose. The sentence is read for the scope it claims, and the
# guard below then holds exactly that scope: widen the wording back to "a registered test" and
# the guard widens with it. That is the only arrangement in which the claim and the thing it
# claims cannot drift apart -- and the drift is what happened, because the unqualified sentence
# shipped over a tree in which four registered tests still skip, none of them Tonight's.
_LEDGER_LAST_SKIP = re.compile(
    r"takes the last `pytest\.skip` out of a registered\s+(?:(\w+)\s+)?test\b"
)


def _registered_tests_that_skip(scope: str | None) -> list[str]:
    """Every registered backend test that can report a green run having asserted nothing.

    A `pytest.skip` call and a `skipif` marker are one defect with two spellings: rule 2 above
    asks only whether a named test EXISTS, so either one closes its row while the run it closed
    it on executed no assertion. The sentence names the call because the call is what this
    milestone removed; the guard reads both because a maintainer repairing the one and leaving
    the other would have satisfied the letter of a claim about silent coverage.

    `scope` is a substring of the file stem -- "tonight" for the Tonight suite, None for every
    registered backend test. Read with `ast` rather than a regex for the reason the argument
    against `pytest.skip` is made at all: `test_tonight_integration.py:1460` and
    `test_tonight_routes.py:898` both write "pytest.skip" inside a docstring arguing against it,
    and a guard that counted those would be reporting its own documentation as the defect.

    A named file that is not there is not this guard's to report -- `test_every_named_test_exists`
    is the rule for that, and two failures for one cause is one of them mis-diagnosed.
    """
    wanted: dict[str, set[str]] = {}
    for requirement in REQUIREMENTS:
        for test_id in requirement.get("tests", []):
            path, _, name = test_id.partition("::")
            if not path.startswith("backend/tests/") or not name:
                continue
            if scope is not None and scope not in Path(path).stem.lower():
                continue
            wanted.setdefault(path, set()).add(name)

    out: list[str] = []
    for path, names in sorted(wanted.items()):
        source = REPO / path
        if not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in names:
                continue
            for decorator in node.decorator_list:
                if ast.unparse(decorator).startswith("pytest.mark.skip"):
                    out.append(f"{path}::{node.name} carries a skip marker at line {node.lineno}")
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub.func) == "pytest.skip":
                    out.append(f"{path}::{node.name} calls pytest.skip at line {sub.lineno}")
    return sorted(out)


def test_the_testing_ledger_does_not_claim_a_skip_this_milestone_did_not_take():
    """The third ledger guard, and the first about a claim rather than a count.

    Two halves. The first is M4.12's own repair, held permanently and independently of any
    prose: no registered test in the Tonight suite skips. Defect 44's four sites were all in
    that suite -- the integration-layer case of 54d gave up when the fixture stopped dividing
    the household and reported that regression as `1 skipped` and a green file -- and that is
    the property worth keeping true after the paragraph below has been re-pasted for M4.13.

    The second is the sentence. `docs/TESTING.md` published the repair as "it takes the last
    `pytest.skip` out of a registered test", which is a universal, and the universal is false:
    `test_the_default_branch_claim_is_bounded_to_the_run_in_flight` (M4.8) calls `pytest.skip`
    outright, and three more registered tests carry a `skipif` -- the tz test M4.7 registered
    is one, and it skips on this checkout. All four are deliberate and argued, so the defect is
    entirely one of the RECORD: CLAUDE.md sends the next reader to that file "rather than
    assuming status", and an auditor who reads the class as closed stops looking. The two
    milestones that own those tests are not this one's to repair, which is why the repair is to
    scope the sentence rather than to widen the rule.

    The sentence is optional: it belongs to M4.12's paragraph and the paragraph is rewritten by
    the milestone it is re-pasted for. What the guard refuses is the sentence standing with a
    scope the tree does not support -- and because the scope is read out of the prose, a
    maintainer who widens the wording gets the wider rule with it.
    [M4.12 review cycle 2: M412-C2-LEDGER-01]
    """
    survivors = _registered_tests_that_skip("tonight")
    assert not survivors, (
        "a registered Tonight test skips, so a row of this milestone closes on a run that "
        "asserted nothing:\n  " + "\n  ".join(survivors)
    )

    claims = _LEDGER_LAST_SKIP.findall(LEDGER.read_text(encoding="utf-8"))
    assert len(claims) <= 1, (
        f"docs/TESTING.md makes {len(claims)} 'last pytest.skip' claims; the paragraph is "
        "re-pasted per milestone, so at most one milestone can be claiming it"
    )
    for claim in claims:
        scoped = _registered_tests_that_skip(claim.lower() or None)
        assert not scoped, (
            f"docs/TESTING.md claims the last `pytest.skip` was taken out of "
            f"{'a registered ' + claim + ' test' if claim else 'a registered test'}, and "
            f"{len(scoped)} still skip:\n  " + "\n  ".join(scoped)
            + "\n  Scope the sentence to what the milestone did, or name the survivors."
        )
