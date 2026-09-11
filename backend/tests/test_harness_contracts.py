"""Guards over the instrument: the e2e runner, the first-boot spec, and CI's workflow.

None of these files has a runtime that could assert anything about itself. `e2e/run.mjs` runs
once, on a machine with a stack up; `.github/workflows/ci.yml` runs on GitHub. Both encode rules
that §10 and §12 state and that nothing checks -- and both spent this project's whole life
quietly not holding them: a phase 2 entered with no bundle exits 0 because every bundle-dependent
spec skips, and a workflow triggered on one branch reports §12's gates for milestones it never
ran. So the rules are read off the artifacts here, in the suite that actually runs.

Everything below reads TEXT, and that is a convention rather than something a guard enforces:
`test_static_contracts.py`'s `test_every_third_party_import_is_a_declared_dependency` walks
`backend/spielplan` and never `backend/tests`, so an `import yaml` added here would be seen by
nothing -- PyYAML arrives transitively with `uvicorn[standard]` and resolves in CI in silence.
The reason to keep to it is the one that guard is about: a reader of the instrument has to run
wherever the suite runs, including on the partial virtualenv that guard's own escape hatch exists
for, and a check that needs a parser installed before it can speak is one more thing that stops
speaking without saying so. The compose guards in that file read their YAML the same way.

The cost of reading text is that these
readers can be confused by a bracket inside a string literal, which is why each one is anchored
on a statement shape rather than on a bare substring, and why every guard here has a self-test
feeding it a source that regressed: a guard that cannot fail is worse than no guard, because it
reads as coverage. The one exception is the last section, which lifts `e2e/reset.mjs`'s `.env`
reader out of the file and RUNS it: what that parser gets wrong is an order of operations, and a
text guard for it could only spell out the fixed regex and compare strings with itself.

Milestone M4.8, rows `platform-e2e-run-fails-when-no-bundle-loads`,
`platform-e2e-first-boot-cannot-retry-into-a-pass`,
`platform-ci-runs-on-every-branch-and-installs-into-a-venv` and
`platform-e2e-scaffolding-cannot-manufacture-a-verdict`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
CORPUS = WORKFLOWS / "real-bundle.yml"
RUNNER = REPO / "e2e" / "run.mjs"
RESET = REPO / "e2e" / "reset.mjs"
# The `.env` reader moved out of reset.mjs when a checkout per lane made
# run.mjs's and playwright.config.js's hard-coded origin a cross-worktree bug:
# all three now read the stack's own PUBLIC_URL through this one module.
ENV_MJS = REPO / "e2e" / "env.mjs"
SPECS = REPO / "e2e" / "specs"
FIRST_BOOT = SPECS / "01-first-boot.spec.js"
JELLYFIN = SPECS / "08-jellyfin.spec.js"


E2E_OVERLAY = REPO / "ops" / "compose.e2e.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _services_whose_code_is_a_bind_mount(compose: str) -> set[str]:
    """Services in a compose file whose CODE arrives as a mount instead of baked into the image.

    One pattern, because the overlay has one: `- ./ops/fake_jellyfin.py:/ops/...:ro` under a
    service's `volumes:`. A data directory is not code and is deliberately not matched -- the
    question this answers is "does a process here have to be restarted for a source edit to take",
    and only a mounted `.py` makes the answer yes.
    """
    found: set[str] = set()
    service: str | None = None
    for line in compose.splitlines():
        if re.match(r"^ {2}[A-Za-z0-9_.-]+:\s*$", line):
            service = line.strip().rstrip(":")
        elif service and re.search(r"-\s+\./[^:\s]+\.py:", line):
            found.add(service)
    return found


def test_the_e2e_reset_restarts_every_service_whose_code_is_a_bind_mount():
    """A mounted double is only as current as the process that read it.

    `ops/compose.e2e.yml` says of the fake Jellyfin that "the file itself is mounted, so editing
    the fake does not mean rebuilding an image" -- true, and it is half the rule. uvicorn reads
    that file once, at process start, and `docker compose up -d` leaves a running container alone
    because neither its image nor its config changed. So a container started before the edit serves
    the PREVIOUS double, out of memory, for every later run, and the suite silently measures the
    double the last session happened to boot.

    Measured, which is why this is a guard and not a note: M4.11 made section 7.2's library read
    keyless (`client.all_items(None)`) and the fake it replaced declared `userId` required on
    `/Items`, so against a stale container every sweep answered 422, `seen.sync_all` took section
    3.3's unreachable path, and `08-jellyfin.spec.js`'s adopt direction failed on a title nothing
    had adopted -- with the diagnosis three files away from the failure.

    Derived from the overlay rather than naming the service, so the next mounted double is covered
    by having been added.
    """
    mounted = _services_whose_code_is_a_bind_mount(_read(E2E_OVERLAY))
    assert mounted, (
        "ops/compose.e2e.yml no longer mounts any source file into a service: this guard is "
        "reading nothing, and the rule it holds has either moved or stopped applying"
    )
    reset = _read(RESET)
    missing = [
        name for name in sorted(mounted)
        if not re.search(rf"'restart',\s*'{re.escape(name)}'", reset)
    ]
    assert missing == [], (
        f"e2e/reset.mjs never restarts {missing}, whose code it bind-mounts: an edit to that "
        "source reaches the container only when the process is restarted, so the suite would run "
        "against the previous version of the double"
    )


@pytest.mark.parametrize(
    "compose, expected",
    [
        pytest.param(
            "services:\n  jellyfin-fake:\n    volumes:\n"
            "      - ./ops/fake_jellyfin.py:/ops/fake_jellyfin.py:ro\n",
            {"jellyfin-fake"},
            id="the overlay's own shape",
        ),
        pytest.param(
            "services:\n  db:\n    volumes:\n      - ./data/pg:/var/lib/postgresql/data\n",
            set(),
            id="a data mount is not code",
        ),
        pytest.param(
            "services:\n  backend:\n    build:\n      context: .\n",
            set(),
            id="a built image needs no restart for a source edit",
        ),
    ],
)
def test_the_bind_mount_reader_sees_mounted_code_and_not_mounted_data(compose, expected):
    """The negative cases are the load-bearing ones: a guard that demanded a restart for every
    mount would demand one for Postgres's data directory, and a rule that fires on everything is
    the same as a rule nobody reads."""
    assert _services_whose_code_is_a_bind_mount(compose) == expected


def _span(source: str, index: int, opener: str, closer: str) -> tuple[int, int]:
    """(start, end) of the bracket-matched span opening at the first `opener` at or after `index`.

    `end` is exclusive; (-1, -1) when there is no opener left. Three readers below need the
    argument list or the block of one statement rather than "the next N characters", because the
    thing they are checking is what is *inside* one call.
    """
    start = source.find(opener, index)
    if start < 0:
        return (-1, -1)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == opener:
            depth += 1
        elif source[i] == closer:
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return (start, len(source))


def _line_of(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


# --- §10: the runner's two phases ------------------------------------------------------

# The deadline may be spelled as the signal `fetch` accepts or as an explicit `signal:` property;
# what is forbidden is neither.
_DEADLINE = re.compile(r"AbortSignal\.timeout\(|\bsignal\s*:")
_FETCH = re.compile(r"\bfetch\s*\(")
_EXIT = re.compile(r"process\.exit\(\s*([^)]*)\)")
# Anchored on the log STATEMENT, not on the words: `run.mjs` argues about phase 2 in three
# comments and in the failure message itself, and a substring search finds those first -- which
# would put the anchor above the branch it is looking for and pass a runner that has none.
_PHASE_TWO_LOG = re.compile(r"console\.log\([^)]*phase 2")
# What the loop concluded, recorded: `loaded = true` inside the branch that read the body.
_FLAG_SET = re.compile(r"\b(?P<name>[A-Za-z_$][\w$]*)\s*=\s*true\b")


def _fetches_without_a_deadline(source: str) -> list[str]:
    """Every `fetch(...)` that passes no request deadline, by line.

    `fetch` has no timeout of its own: a backend that accepts the connection and never answers
    holds one iteration for ever, which turns a 60-attempt budget into no budget at all. The
    caller then reports "did not become healthy", hours late, having measured nothing.
    """
    bad = []
    for match in _FETCH.finditer(source):
        start, end = _span(source, match.end() - 1, "(", ")")
        if start < 0 or not _DEADLINE.search(source[start:end]):
            bad.append(f"line {_line_of(source, match.start())}: fetch() with no request deadline")
    return bad


def _health_fetch_index(source: str) -> int:
    """Where the health poll is, found by its arguments rather than by a substring."""
    for match in _FETCH.finditer(source):
        start, end = _span(source, match.end() - 1, "(", ")")
        if start >= 0 and "/api/health" in source[start:end]:
            return match.start()
    return -1


def _condition_of(source: str, head: re.Match[str]) -> str:
    """The `( ... )` of one `if`, bracket-matched so a call inside it keeps its own parens."""
    start, end = _span(source, head.end() - 1, "(", ")")
    return " ".join(source[start:end].split()) if start >= 0 else ""


def _failure_branch(source: str, health: int, limit: int) -> re.Match[str] | None:
    """The first `if` between the health poll and the phase-2 log that exits non-zero."""
    for match in re.finditer(r"\bif\s*\(", source):
        if not health < match.start() < limit:
            continue
        # A brace-less branch first: `if (!loaded) process.exit(1);` is one line and has no block.
        line_end = source.find("\n", match.start())
        head = source[match.start(): line_end if line_end > 0 else len(source)]
        if any(code.strip() != "0" for code in _EXIT.findall(head)):
            return match
        start, end = _span(source, match.end(), "{", "}")
        # A block that swallows the phase 2 log is not a branch *before* it.
        if start < 0 or end > limit:
            continue
        if any(code.strip() != "0" for code in _EXIT.findall(source[start:end])):
            return match
    return None


def _failure_branch_problems(source: str) -> list[str]:
    """Whether a runner that loaded no bundle can still reach phase 2.

    §10's swap sequence ends in "restart backend + worker", so a bundle imported in phase one is
    not loaded until the services come back. Nine spec files skip themselves on `!has_bundle`,
    and Playwright exits 0 for a run of nothing but skips -- so a phase 2 entered without a
    bundle is a green suite that proved nothing at all, including §12's M0 exit criterion.

    Which is a property of what the branch *tests*, not of a branch existing: asking only whether
    some `if` in the span exits non-zero passes `let loaded = true`, `if (false)` and a poll that
    stopped reading the bundle bit -- three tidy-ups that each restore the defect exactly. So the
    chain is walked instead: the flag starts false, is set only where the health body reports a
    bundle, and is what the failure branch reads.
    """
    problems = []
    health = _health_fetch_index(source)
    phase_two = _PHASE_TWO_LOG.search(source)
    if health < 0:
        problems.append("the runner polls no /api/health after the restart")
    if not phase_two:
        problems.append("the runner logs no phase 2")
    if health < 0 or not phase_two:
        return problems
    if phase_two.start() < health:
        return ["phase 2 is entered before the restarted stack's health is read at all"]

    branch = _failure_branch(source, health, phase_two.start())
    if branch is None:
        return [
            "nothing between the post-restart health poll and phase 2 exits non-zero: a run that "
            "loaded no bundle enters phase 2, where the specs that need one skip and exit 0"
        ]

    assignment = _FLAG_SET.search(source, health, phase_two.start())
    if not assignment:
        return [
            "the health poll records nothing between the fetch and phase 2, so the failure "
            "branch is reading something the poll never wrote"
        ]
    flag = assignment.group("name")
    guards = [m for m in re.finditer(r"\bif\s*\(", source) if health < m.start() < assignment.start()]
    if not guards or "bundle" not in _condition_of(source, guards[-1]):
        condition = _condition_of(source, guards[-1]) if guards else "nothing"
        return [
            f"`{flag}` is set on {condition} rather than on the health body reporting a bundle: "
            "a backend that answers is not a backend that loaded one, and §10's restart is the "
            "whole reason the two differ"
        ]
    if not re.search(rf"\b(?:let|var|const)\s+{re.escape(flag)}\s*=\s*false\b", source[:health]):
        return [
            f"`{flag}` starts true, or is never declared false before the poll: the failure "
            "branch below cannot fire whatever the restarted backend answered"
        ]
    if not re.search(rf"\b{re.escape(flag)}\b", _condition_of(source, branch)):
        return [
            f"the failure branch tests {_condition_of(source, branch)} and does not read the "
            f"flag `{flag}` the health poll sets: it does not depend on what the poll concluded"
        ]
    return problems


def _inverted_tag(source: str) -> str | None:
    match = re.search(r"'--grep-invert'\s*,\s*'([^']+)'", source)
    return match.group(1) if match else None


def _describe_tags(source: str) -> set[str]:
    titles = re.findall(r"test\.describe\(\s*['\"]([^'\"]*)['\"]", source)
    return {tag for title in titles for tag in re.findall(r"@[\w-]+", title)}


def test_the_e2e_runner_exits_when_no_bundle_loads():
    """§10: the swap sequence ends in a restart, and the run has failed if nothing came back
    with a bundle. `e2e/reset.mjs`'s own health loop was already written this way; this one was
    not, and the difference is the whole difference between a gate and a report."""
    assert _failure_branch_problems(_read(RUNNER)) == []


def test_every_health_fetch_in_the_runner_carries_a_deadline():
    """Both halves of the harness, because `run.mjs` calls `reset.mjs` and inherits its stall:
    an unbounded fetch in either one spends the other's budget."""
    assert _fetches_without_a_deadline(_read(RUNNER)) == []
    assert _fetches_without_a_deadline(_read(RESET)) == []


def _tag_problems(runner: str, spec: str) -> list[str]:
    """Whether the tag phase 2 inverts is the tag phase 1's file actually carries.

    Phase 1 selects one file by path and phase 2 takes everything else by inverting a tag, so
    the two halves only partition the suite while the tag and the path name the same file.
    """
    tag = _inverted_tag(runner)
    if not tag:
        return ["e2e/run.mjs's phase 2 inverts no tag at all"]
    problems = []
    if "specs/01-first-boot.spec.js" not in runner:
        problems.append("phase 1 no longer selects the first-boot file")
    if tag not in _describe_tags(spec):
        problems.append(
            f"phase 2 inverts {tag!r}, which 01-first-boot.spec.js's describe title does not "
            "carry: phase 1's file would run in phase 2 as well"
        )
    return problems


def test_phase_two_inverts_the_tag_the_first_boot_file_carries():
    """The two phases partition the suite, so the tag one inverts has to be the tag the other
    selects. `run.mjs` inverted `@needs-db`, a name that promised a database-dependence split
    nothing implemented: the tag existed in exactly two places, that line and this one file's
    describe title, so the invert was a synonym for "not the first-boot file" and the convention
    it advertised was a trap. A second spec written to it -- a DB-dependent test tagged
    `@needs-db` -- would have been inverted out of phase 2 and selected by neither phase, which
    is a registered test that never runs. The tag is `@first-boot` now, and this is what keeps
    the runner's spelling and the file's the same one."""
    runner = _read(RUNNER)
    assert _tag_problems(runner, _read(FIRST_BOOT)) == []
    tag = _inverted_tag(runner)
    carriers = {p.name for p in sorted(SPECS.glob("*.spec.js")) if tag in _read(p)}
    assert carriers == {"01-first-boot.spec.js"}, (
        f"{tag} also appears in {sorted(carriers - {'01-first-boot.spec.js'})}: phase 2 inverts "
        "those files out too, so they run in neither phase"
    )


# A runner shaped like the one this milestone found: it polls, it breaks out of the loop, and it
# announces phase 2 whatever the answer was.
_UNGUARDED_RUNNER = """
const base = process.env.BASE_URL ?? 'http://localhost:8080';
let loaded = false;
for (let i = 0; i < 60; i++) {
  try {
    const res = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(5000) });
    if (res.ok && (await res.json()).bundle) break;
  } catch { /* not up yet */ }
  await new Promise((r) => setTimeout(r, 1000));
}

console.log('\\n-- phase 2: everything else --');
const rest = play(['--grep-invert', '@first-boot']);
process.exit(rest.status ?? 1);
"""

_GUARDED_RUNNER = _UNGUARDED_RUNNER.replace(
    "if (res.ok && (await res.json()).bundle) break;",
    "if (res.ok && (await res.json()).bundle) { loaded = true; break; }",
).replace(
    "console.log('\\n-- phase 2",
    "if (!loaded) {\n  console.error('no bundle');\n  process.exit(1);\n}\n\nconsole.log('\\n-- phase 2",
)


@pytest.mark.parametrize(
    "source, needle",
    [
        pytest.param(_UNGUARDED_RUNNER, "exits non-zero", id="no failure branch at all"),
        pytest.param(
            _GUARDED_RUNNER.replace("process.exit(1);", "process.exit(0);"),
            "exits non-zero",
            id="the branch exits zero",
        ),
        pytest.param(
            _GUARDED_RUNNER.replace("process.exit(1);\n", ""),
            "exits non-zero",
            id="the branch survived but only logs",
        ),
        pytest.param(
            _GUARDED_RUNNER.replace(", { signal: AbortSignal.timeout(5000) }", ""),
            "deadline",
            id="the branch is right and the poll can hang for ever",
        ),
        # Three shapes that keep the branch and take away what it is a branch *on*. Each reads
        # as a tidy-up ("the poll is flaky in CI, default it and let phase 2 decide"), each
        # restores a phase 2 entered with nothing imported, and a guard that only asked whether
        # some `if` between the poll and the log exits non-zero passed all three.
        pytest.param(
            _GUARDED_RUNNER.replace("let loaded = false;", "let loaded = true;"),
            "starts true",
            id="the flag is defaulted true, so the branch can never fire",
        ),
        pytest.param(
            _GUARDED_RUNNER.replace("if (!loaded) {", "if (false) {"),
            "does not read the flag",
            id="the branch stops reading what the loop concluded",
        ),
        pytest.param(
            _GUARDED_RUNNER.replace("if (res.ok && (await res.json()).bundle)", "if (res.ok)"),
            "bundle",
            id="the flag stops meaning a bundle and starts meaning an answer",
        ),
    ],
)
def test_the_runner_guard_sees_a_health_loop_with_no_failure_branch(source, needle):
    """The guard's own failing case. The shape it has to catch is not an obvious one -- the
    unguarded loop reads as complete, `break`s on success and falls through on failure -- so
    "it passes against the tree" is no evidence that it would ever say anything. The last two
    are the regressions a later edit produces: a branch kept and its exit dropped, and a repair
    that leaves the poll itself unbounded."""
    problems = _failure_branch_problems(source) + _fetches_without_a_deadline(source)
    assert problems, "the guard passed a runner that enters phase 2 with no bundle"
    assert needle in " ".join(problems), problems
    assert not _failure_branch_problems(_GUARDED_RUNNER) + _fetches_without_a_deadline(
        _GUARDED_RUNNER
    ), "and it passes the runner as repaired"


# One runner and one spec that agree, for the guard above to be shown disagreeing about. The
# rename is the regression: `@needs-db` was in two places, so a one-sided edit of either is what
# puts a spec in neither phase, and the whole-tree assertion in the shipped test cannot be fed a
# synthetic pair -- it globs the real directory.
_TAG_RUNNER = """
const first = play(['specs/01-first-boot.spec.js']);
const rest = play(['--grep-invert', '@first-boot']);
"""
_TAG_SPEC = "test.describe('first boot @first-boot', () => {\n});\n"


@pytest.mark.parametrize(
    "runner, spec, needle",
    [
        pytest.param(
            _TAG_RUNNER.replace("'@first-boot'", "'@needs-db'"), _TAG_SPEC, "does not carry",
            id="the runner's tag is renamed and the file's is not",
        ),
        pytest.param(
            _TAG_RUNNER, _TAG_SPEC.replace(" @first-boot", ""), "does not carry",
            id="the describe title loses the tag",
        ),
        pytest.param(
            _TAG_RUNNER.replace("'--grep-invert', '@first-boot'", ""), _TAG_SPEC, "inverts no tag",
            id="phase 2 stops inverting anything and runs phase 1's file twice",
        ),
        pytest.param(
            _TAG_RUNNER.replace("specs/01-first-boot.spec.js", "specs/0*.spec.js"), _TAG_SPEC,
            "no longer selects", id="phase 1 stops selecting the first-boot file by path",
        ),
    ],
)
def test_the_tag_guard_sees_a_runner_and_a_spec_that_disagree(runner, spec, needle):
    """The guard's own failing case, and it needs one for a reason the others do not: every
    assertion it makes holds against the tree it was written to condemn, because that tree's
    defect was a misleading tag *name* rather than a mismatch. So passing against the repository
    is no evidence at all here -- only a synthetic pair that disagrees shows it can speak."""
    problems = _tag_problems(runner, spec)
    assert problems, "the guard passed a runner and a spec whose tags disagree"
    assert needle in " ".join(problems), problems
    assert _tag_problems(_TAG_RUNNER, _TAG_SPEC) == [], "and it passes the pair that agrees"


# --- §3.1, §12: the first boot cannot be retried into a pass ---------------------------

_CONFIGURE = re.compile(r"test\.describe\.configure\(\s*\{(?P<body>[^}]*)\}\s*\)")

# `configure` configures the scope it is written in, and Playwright resolves both settings by
# walking outwards from the innermost suite and taking the first one it finds
# (`playwright/lib/common/index.js:2088`). So a second `configure` inside the `first boot` group
# governs that group and the file-level line at 01-first-boot.spec.js:25 never applies to it --
# which the two existential checks below cannot see, because "some configure in this file says
# `retries: 0`" stays true while the group is being retried. `retries: 2` inside a describe is
# the single most ordinary answer to "this group is flaky in CI", and it is the answer this rule
# exists to refuse. [M4.8 review cycle 2]
#
# And on the whole value, not on a literal digit. `\d+` matched nothing at all in `retries:
# process.env.CI ? 1 : 0`, which is not a contrived spelling: it is `e2e/playwright.config.js:33`
# verbatim, three files away, and it is what "give CI its retry back for this group" is copied
# from. Measured against the installed Playwright 1.62.1, that nested line turns a failed first
# boot from `1 failed` / exit 1 into `1 flaky` / exit 0, which `run.mjs:38` reads as phase 1
# having imported a bundle. So anything that is not the literal `0` is reported and the value is
# quoted back; `[^,}]+` is the whole value because `_CONFIGURE`'s body already stops at `}`.
# `_MODE` takes either quote for the same reason -- a one-character difference that made a nested
# `mode: "parallel"` invisible -- though that half is the lesser one: Playwright's own `_configure`
# throws on a parallel group nested inside a serial one, and has no such backstop for retries.
# [M4.8 review cycle 3: m48-c3-e2e-01]
_RETRIES = re.compile(r"\bretries\s*:\s*(?P<value>[^,}]+)")
_MODE = re.compile(r"\bmode\s*:\s*['\"](?P<mode>\w+)['\"]")


def _retry_problems(source: str) -> list[str]:
    """Whether the first-boot group can be retried, and so scored `flaky` rather than failed.

    Every step in that file has a side effect that persists: once the admin has been created the
    admin exists, so a retry does not re-run the sequence, it meets an app past first boot and
    can only take the `beforeAll` skip. Playwright scores a [failed, then skipped] test as flaky
    and exits 0, `run.mjs` reads that 0 as phase 1 having imported a bundle, and §12's M0 exit
    criterion ("bundle imports clean") is reported green by a run whose import failed.
    """
    problems = []
    # Whole commented-out lines first, and for the reason `_beforeall_skip` gives below: a
    # configure commented out is a configure that is not there, and commenting it out is how a
    # bisector asks whether it is the cause of a phase-1 failure. Deleting the line was already
    # reported; `//`-ing it satisfied both existential checks off a line Playwright never reads,
    # and the group inherited `playwright.config.js:33`'s CI retry again. The two halves of this
    # rule are now defended against the same gesture. [M4.8 review cycle 3: m48-c3-e2e-02]
    live = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )
    bodies = [m.group("body") for m in _CONFIGURE.finditer(live)]
    if not bodies:
        return ["the first-boot file configures no describe group at all"]
    if not any(re.search(r"\bmode\s*:\s*'serial'", b) for b in bodies):
        problems.append("the first-boot group is not serial: its steps do not share a page")
    if not any(re.search(r"\bretries\s*:\s*0\b", b) for b in bodies):
        problems.append(
            "the first-boot group does not disable retries: a failure after the admin exists "
            "retries into a skip, which scores flaky and exits 0"
        )
    for body in bodies:
        retries = _RETRIES.search(body)
        if retries and retries.group("value").strip() != "0":
            problems.append(
                f"a describe.configure sets a non-zero retries ({retries.group('value').strip()}): "
                "the enclosing group is retried whatever the file-level line says"
            )
        mode = _MODE.search(body)
        if mode and mode.group("mode") != "serial":
            problems.append(
                f"a describe.configure sets mode: '{mode.group('mode')}': the enclosing group "
                "stops sharing its page whatever the file-level line says"
            )
    return problems


def _beforeall_skip(source: str) -> str:
    """The body of the first `test.beforeAll(...)`, or "" when it has no live `test.skip`.

    Whole commented-out lines are dropped first, and only whole ones: a skip commented out is a
    skip that is not there, and that is exactly the shape this rule is taken away in. Anything
    narrower than a full-line `//` would have to reason about the `//` in a URL, which a text
    reader cannot and does not need to.
    """
    match = re.search(r"test\.beforeAll\s*\(", source)
    if not match:
        return ""
    start, end = _span(source, match.end() - 1, "(", ")")
    if start < 0:
        return ""
    body = "\n".join(
        line for line in source[start:end].splitlines() if not line.lstrip().startswith("//")
    )
    return body if "test.skip(" in body else ""


def _has_a_has_bundle_guard(source: str) -> bool:
    return "has_bundle" in source


def test_the_first_boot_group_runs_with_retries_disabled():
    """Decision 185: `retries: 0` on this one file rather than `failOnFlakyTests`. The key first
    appears in Playwright 1.52.0 and 1.62.1 accepts an unknown config key in silence, so at the
    shipped ^1.49.0 floor a config-based adoption is a no-op that reads as a fix; and 14/15/16
    lean on the retry the config grants CI, so turning flaky into failure globally would make
    their measured intermittents red rather than measured."""
    assert _retry_problems(_read(FIRST_BOOT)) == []


def test_the_first_boot_beforeall_skip_survives():
    """The other half of the same rule, and the one a tidy-up takes away. `retries: 0` only says
    a failure stays a failure; this says an ad-hoc run against a used database still SKIPS rather
    than failing on a wizard that is already past. Both together are what make the file's red
    mean "the import broke" and nothing else."""
    body = _beforeall_skip(_read(FIRST_BOOT))
    assert body, "01-first-boot.spec.js's beforeAll no longer skips on a database that is not fresh"
    assert "!state.required" in body, (
        "the beforeAll skip no longer reads /api/setup/state's `required`: an anonymous caller "
        "gets that bit and the note, and nothing else (sec-14)"
    )


def test_the_jellyfin_spec_is_not_given_a_has_bundle_guard():
    """The tidy-up that must not happen. Nine spec files skip on `!has_bundle`, and adding the
    tenth here would look like consistency: 08-jellyfin needs titles, so it fails when nothing
    imported. But §7.3's two-way sync is proved nowhere else, and a `playwright test` against a
    stack with no bundle would then report every spec as skipped and the job as green. The
    failure is the signal; making it a skip deletes the signal."""
    guarded = {p.name for p in sorted(SPECS.glob("*.spec.js")) if _has_a_has_bundle_guard(_read(p))}
    assert not _has_a_has_bundle_guard(_read(JELLYFIN)), (
        "08-jellyfin.spec.js now skips itself when no bundle is loaded, so a stack that imported "
        "nothing has no spec left that fails"
    )
    # A floor, not the exact nine: decision 165 retires 16-tonight-tv, and pinning the set would
    # make that removal red here for no reason. What this asserts is that the convention still
    # exists and 08 is the deliberate exception to it, not that it vanished everywhere.
    assert len(guarded) >= 5, f"the !has_bundle convention has all but disappeared: {sorted(guarded)}"


_SERIAL_SPEC = """
test.describe.configure({ mode: 'serial', retries: 0 });

test.describe('first boot @first-boot', () => {
  test.beforeAll(async ({ browser, baseURL }) => {
    const state = await setupState(page.request);
    test.skip(!state.required, 'needs a fresh database');
  });
});
"""


@pytest.mark.parametrize(
    "source, reader, needle",
    [
        pytest.param(
            _SERIAL_SPEC.replace(", retries: 0", ""),
            _retry_problems,
            "does not disable retries",
            id="retries left to the config, which grants CI two",
        ),
        pytest.param(
            _SERIAL_SPEC.replace("mode: 'serial', ", ""),
            _retry_problems,
            "not serial",
            id="the group stops sharing its page",
        ),
        # The shape neither of the two above can see, because both ask whether ANY configure in
        # the file carries the setting: a second one inside the group, which is what Playwright
        # resolves first and what "make this group less flaky" writes.
        pytest.param(
            _SERIAL_SPEC.replace(
                "test.describe('first boot @first-boot', () => {",
                "test.describe('first boot @first-boot', () => {\n"
                "  test.describe.configure({ retries: 2 });",
            ),
            _retry_problems,
            "non-zero retries",
            id="a nested configure retries the group the file-level line disabled",
        ),
        pytest.param(
            _SERIAL_SPEC.replace(
                "test.describe('first boot @first-boot', () => {",
                "test.describe('first boot @first-boot', () => {\n"
                "  test.describe.configure({ mode: 'parallel' });",
            ),
            _retry_problems,
            "stops sharing its page",
            id="a nested configure takes the group out of serial mode",
        ),
        # The same nested configure written the way `playwright.config.js:33` writes it. A
        # literal-digit reader matched nothing here at all, so this -- the spelling the
        # repository's own config teaches, and the one "give CI its retry back for this group"
        # is copied from -- was the shape both nested cases above could not see.
        pytest.param(
            _SERIAL_SPEC.replace(
                "test.describe('first boot @first-boot', () => {",
                "test.describe('first boot @first-boot', () => {\n"
                "  test.describe.configure({ retries: process.env.CI ? 1 : 0 });",
            ),
            _retry_problems,
            "non-zero retries",
            id="a nested configure hands the group CI's retry back by expression",
        ),
        pytest.param(
            _SERIAL_SPEC.replace(
                "test.describe('first boot @first-boot', () => {",
                "test.describe('first boot @first-boot', () => {\n"
                '  test.describe.configure({ mode: "parallel" });',
            ),
            _retry_problems,
            "stops sharing its page",
            id="and the same mode change in the other quote",
        ),
        # The rule taken away by the gesture the beforeAll half is already defended against: a
        # bisector comments the line out to see whether it is the cause and does not put it back.
        pytest.param(
            _SERIAL_SPEC.replace(
                "test.describe.configure({ mode: 'serial', retries: 0 });",
                "// test.describe.configure({ mode: 'serial', retries: 0 });",
            ),
            _retry_problems,
            "configures no describe group at all",
            id="the file-level configure commented out rather than deleted",
        ),
        pytest.param(
            _SERIAL_SPEC.replace("test.skip(!state.required, 'needs a fresh database');", ""),
            lambda s: [] if _beforeall_skip(s) else ["the beforeAll no longer skips"],
            "no longer skips",
            id="the beforeAll skip tidied away",
        ),
        pytest.param(
            _SERIAL_SPEC.replace("    test.skip(!state", "    // test.skip(!state"),
            lambda s: [] if _beforeall_skip(s) else ["the beforeAll no longer skips"],
            "no longer skips",
            id="the beforeAll skip commented out rather than deleted",
        ),
        pytest.param(
            _SERIAL_SPEC.replace(
                "const state", "test.skip(!config.has_bundle, 'needs a bundle');\n    const state"
            ),
            lambda s: ["a has_bundle guard was added"] if _has_a_has_bundle_guard(s) else [],
            "has_bundle",
            id="a has_bundle guard added to a file that must fail instead",
        ),
    ],
)
def test_the_retries_guard_sees_a_group_that_can_retry_into_a_skip(source, reader, needle):
    """Each of the regressions above is a plausible tidy-up rather than a mistake, which is
    exactly why the guards need to have been shown saying something."""
    problems = reader(source)
    assert problems, "the guard passed a first-boot file that can retry into a pass"
    assert needle in " ".join(problems)
    assert reader(_SERIAL_SPEC) == [], "and it passes the file as it stands"


# --- §12, §1: CI runs where the gates are read off -------------------------------------

# Text, and by indentation. The same reasoning as the compose guards in test_static_contracts.py:
# PyYAML is not a test dependency and this repository will not add one to read six lines. What
# these need is the block structure and the values, which indentation gives for free, and
# comments are stripped on the way through -- a trigger that was commented out is not a trigger.


def _nested(text: str, key: str) -> str:
    """The header line for `key:` plus everything indented under it, comments removed."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        head = line.split("#", 1)[0].rstrip()
        bare = head.strip()
        if bare != f"{key}:" and not bare.startswith(f"{key}: "):
            continue
        indent = len(head) - len(head.lstrip())
        body = [head]
        for follow in lines[i + 1:]:
            stripped = follow.split("#", 1)[0].rstrip()
            if stripped and len(stripped) - len(stripped.lstrip()) <= indent:
                break
            body.append(stripped)
        return "\n".join(body)
    return ""


def _jobs(text: str) -> dict[str, str]:
    body = _nested(text, "jobs")
    heads = list(re.finditer(r"^  (?P<name>[A-Za-z][\w-]*):\s*$", body, re.M))
    out = {}
    for i, match in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        out[match.group("name")] = body[match.start():end]
    return out


# Which jobs the second half of the rule below is about: the ones that do not run on a machine
# GitHub owns. Selecting them on the token `self-hosted` was the whole test, and `runs-on` matches
# any runner carrying every label it names -- the `self-hosted` label is applied to a registered
# runner automatically, so `runs-on: spielplan-corpus` reaches the same household box while
# dropping out of this rule, and the `if:` deletion that follows lands green. What actually
# distinguishes that machine is that its labels are not GitHub's image names, so that is what is
# read. A job whose `runs-on` is an expression matches nothing here either and is gated like the
# corpus job: for a rule about whose electricity bill runs the push path, the false positive is
# the side to fail on. [M4.8 review cycle 2]
_HOSTED_IMAGE = re.compile(r"\b(?:ubuntu|windows|macos)-[\w.]+\b", re.IGNORECASE)

# A job-level `if:` that pins the job to one branch narrows the trigger exactly as `branches:`
# does, one job at a time, and it was the fourth member of a set whose other three (`branches`,
# `branches-ignore`, `paths`) this guard already reads. Only every hosted job was short-circuited
# out of the read below before its `if:` was ever looked at, so `if: github.event_name ==
# 'pull_request' || github.ref == 'refs/heads/main'` -- the canonical Actions idiom for "only run
# the expensive job where it matters", and the first thing minute-cost pressure writes on the e2e
# job -- left all three CI guards silent. Measured: with all five hosted jobs gated that way a
# milestone branch's push runs literally nothing and this function returned []. It is the ref
# comparison and not the mention that is read, for the reason `_NOT_THE_DEFAULT_BRANCH` below
# gives about its own mirror image, and only `==` against a ref: `ci.yml:17-20`'s own comment
# contemplates gating the hosted jobs off the weekly tick with "five more `if:` conditions", and
# a gate on the event name narrows no branch. [M4.8 review cycle 3: m48-c3-ci-01]
_ONLY_THE_DEFAULT_BRANCH = re.compile(
    r"github\.ref(?:_name)?\s*==\s*'[^']+'|'[^']+'\s*==\s*github\.ref(?:_name)?"
)


def _push_trigger_problems(text: str) -> list[str]:
    """Whether a push to a branch that is not the default one runs anything.

    Eleven runs existed when this was written and all eleven were `push main`: m3, m4, m45 and m5
    each reached main having never run on Linux, so §12's gates were first evaluated on the merge
    commit, after the work, with the whole milestone in one diff. The second half of the same
    property is that widening the trigger does not put every branch's push on somebody's
    self-hosted machine -- so a job that runs on one has to be gated off the push path.
    """
    problems = []
    push = _nested(_nested(text, "on"), "push")
    if not push:
        problems.append("the workflow has no push trigger: nothing runs on a branch at all")
    else:
        branches = _nested(push, "branches")
        if branches:
            listed = set(re.findall(r"[\w*./-]+", branches.split(":", 1)[1]))
            if "**" not in listed:
                problems.append(
                    f"the push trigger is restricted to {sorted(listed)}: a branch that is not "
                    "one of those runs nothing until it is merged"
                )
        # `branches` is not the only key that narrows a trigger, and reading it alone made the
        # guard blind to the two narrowings a "stop building docs commits" edit reaches for
        # first. A milestone branch whose commits touch only e2e/, ops/ or docs/ then runs
        # nothing on Linux until the merge, which is finding 10's symptom under another key.
        if _nested(push, "branches-ignore"):
            problems.append(
                "the push trigger excludes branches by name (branches-ignore): a branch matching "
                "one of those patterns runs nothing until it is merged"
            )
        for narrowing in ("paths", "paths-ignore"):
            if _nested(push, narrowing):
                problems.append(
                    f"the push trigger carries a {narrowing} filter: a branch whose commits touch "
                    "nothing it lists runs nothing until it is merged"
                )
    for name, block in _jobs(text).items():
        # Four spaces exactly: a job-level `if:`, not a step's `if: failure()` at eight.
        gate = re.search(r"^ {4}if:\s*(?P<expr>.+)$", block, re.M)
        expr = gate.group("expr") if gate else ""
        # Read for every job, before the runner is looked at: see `_ONLY_THE_DEFAULT_BRANCH`.
        if _ONLY_THE_DEFAULT_BRANCH.search(expr):
            problems.append(
                f"job `{name}` is gated to one branch by its own `if:`: a push to a milestone "
                "branch does not run it, whatever the trigger above allows"
            )
        runs_on = _nested(block, "runs-on")
        if not runs_on or _HOSTED_IMAGE.search(runs_on):
            continue
        events = set(re.findall(r"github\.event_name\s*==\s*'([a-z_]+)'", expr))
        if not events:
            problems.append(
                f"job `{name}` runs on a runner GitHub does not own behind no github.event_name "
                "gate: every branch push would queue on that machine"
            )
        elif events & {"push", "pull_request"}:
            problems.append(f"job `{name}` runs on a runner GitHub does not own on the push path")
    return problems


# The value has to *say* "not the default branch", in either spelling of the ref. Asking only
# whether the expression mentions `github.ref` was one character from useless: `${{ github.ref ==
# 'refs/heads/main' }}` mentions it, passes, and cancels precisely the run this rule protects and
# no other; so does `${{ true || github.ref }}`, which tests nothing at all.
_NOT_THE_DEFAULT_BRANCH = re.compile(
    r"github\.ref(?:_name)?\s*!=\s*'[^']+'|'[^']+'\s*!=\s*github\.ref(?:_name)?"
)


def _cancellation_problems(text: str) -> list[str]:
    """Whether the run of the commit the gates are read off can be cancelled by the next push.

    The M4 merge run was cancelled 71 s in by the push after it, which left that merge commit
    with no completed run at all. Superseding a branch's own in-flight run is what the setting is
    for; superseding the default branch's is how a gate produces no result.
    """
    concurrency = _nested(text, "concurrency")
    if not concurrency:
        return []
    match = re.search(r"cancel-in-progress:\s*(?P<value>.+)", concurrency)
    if not match:
        return []
    value = match.group("value").strip()
    if value == "false" or _NOT_THE_DEFAULT_BRANCH.search(value):
        return []
    return [
        f"cancel-in-progress is {value}, which does not read as `not the default branch`: a push "
        "to the default branch cancels the run of the commit the gates are read off"
    ]


# The setting above protects a run that is RUNNING. This one is about the run that never starts.
# `concurrency:` at column 0 is WORKFLOW-level, so the group is held by the whole run until its
# last job finishes -- and a job on a runner GitHub does not own does not finish in minutes when
# nobody has registered its label: it queues, and GitHub cancels it after 24 h. `real-bundle` and
# the five hosted jobs answered the same workflow-level weekly `schedule` out of one file, on the
# default branch, in group `ci-refs/heads/main`. So for a day a week main's next push was created
# PENDING behind that group and the push after it superseded the pending one, which is finding
# 10's own symptom -- a commit on the branch the milestone ledger quotes gate results from, with
# no completed run at all -- restored by the job this milestone added. The job-level `if:` buys
# nothing here, because the run that holds the group is the one where the gate PASSES; and the
# guard written about the same 24 h stopped one step short of this, asking what `needs:` the
# corpus job, finding nothing, and never reading the group. The remedy taken is a run of its own,
# which is what `.github/workflows/real-bundle.yml` is; an event dimension on the group key is
# the weaker one -- a `workflow_dispatch` of the corpus job would still share the dispatch
# group -- so it is not accepted as an escape here. [M4.8 review cycle 4, m48-c4-ci-01]


def _self_hosted_group_problems(workflows: dict[str, str]) -> list[str]:
    """Whether a job that can queue for a day holds the group a push to the default branch waits in.

    Read over the whole directory rather than over `ci.yml`, because the fix moved the one job
    this is about into a second file and a rule that reads one file cannot see the third one
    somebody adds. A workflow with no `concurrency:` block queues nothing behind it, and one no
    push creates runs of has only its own runs to delay; the pairing is what costs.
    """
    problems = []
    for name, text in sorted(workflows.items()):
        # Column 0, because the word in the message is "workflow-level" and it has to be true:
        # a `concurrency:` under a job is that job's own group and holds nothing else in the run.
        if not re.search(r"^concurrency:", text, re.M):
            continue
        triggers = _nested(text, "on")
        if not (_nested(triggers, "push") or _nested(triggers, "pull_request")):
            continue
        for job, block in _jobs(text).items():
            runs_on = _nested(block, "runs-on")
            if not runs_on or _HOSTED_IMAGE.search(runs_on):
                continue
            problems.append(
                f"{name}: job `{job}` runs on a runner GitHub does not own inside a workflow a "
                "push creates runs of, under one workflow-level concurrency group -- so its "
                "queue time is charged to that group and a push to the default branch is left "
                "pending behind it"
            )
    return problems


# `pip3`, and any run of spaces: `python3 -m pip install` was caught and `pip3 install` was not,
# and the CPU torch index lives under `[tool.uv.*]` keys in the pyproject that no pip reads, so
# either one resolves the 2.8 GB CUDA wheel §1 forbids. The numbered spellings are what an author
# who has lost the venv reaches for, and nothing stops them landing: ubuntu-24.04 does ship a bare
# `python` (python-is-python3 over the image's /usr 3.12), which is why the e2e job's own
# `python -m pip install` ran for this project's whole life instead of failing loudly -- the
# GITHUB_PATH line puts the venv's bin FIRST, it does not make `python` exist. A rule that
# depended on the runner having no `python` would be a rule about a runner-image detail; this one
# is about the interpreter the install lands in either way.
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
_BARE_PIP_INSTALL = re.compile(r"(?<!uv )\bpip3?\s+install\b")


def _interpreter_problems(text: str) -> list[str]:
    """Whether any job installs this package into the runner's own interpreter.

    §1 requires the CPU torch build, and the pyproject's CPU index is what delivers it -- 187 MiB
    rather than about 2.8 GB of CUDA wheels. The e2e job used a bare `python -m pip install`
    against the ubuntu-24.04 image's externally managed /usr interpreter: the same one uv refused
    at the M3 merge, which is why the other three jobs have a venv. It worked only because that
    runner image ships an /etc/pip.conf carrying `break-system-packages = true`, a pip-only
    setting uv never reads -- so the one job that proves what actually ships was a runner-image
    change away from failing at its first step.
    """
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        if _BARE_PIP_INSTALL.search(code):
            problems.append(
                f"ci.yml:{number}: `pip install` outside a uv venv, into the runner's own "
                "externally managed interpreter"
            )
        elif re.search(r"--break-system-packages|--user\b", code) and _PIP_INSTALL.search(code):
            problems.append(f"ci.yml:{number}: an install that overrides PEP 668 rather than using a venv")
    return problems


def test_ci_runs_on_every_branch_push():
    """§12's gates are read off a milestone branch, so they have to have run on one."""
    assert _push_trigger_problems(_read(CI)) == []


def test_a_run_on_the_default_branch_is_not_cancelled_by_the_next_push():
    """A cancelled run is not a red one and not a green one: it is no evidence, on the one commit
    whose evidence the milestone ledger quotes."""
    assert _cancellation_problems(_read(CI)) == []


def test_no_ci_job_pip_installs_into_the_runner_interpreter():
    """Every job installs the same way the lint, backend and integration jobs already do."""
    assert _interpreter_problems(_read(CI)) == []


_CLEAN_WORKFLOW = """\
name: ci

on:
  push:
    branches: ['**']
  pull_request:
  workflow_dispatch:
  schedule:
    - cron: '17 5 * * 1'

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}

jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: astral-sh/setup-uv@v5
      - run: uv venv
      - run: uv pip install -e "backend[dev]"

  real-bundle:
    if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'
    runs-on: [self-hosted, spielplan-corpus]
    steps:
      - run: uv pip install -e "backend[dev]"
"""


@pytest.mark.parametrize(
    "old, new, reader, needle",
    [
        pytest.param(
            "    branches: ['**']", "    branches: [main]", _push_trigger_problems, "restricted to",
            id="the trigger goes back to one branch",
        ),
        pytest.param(
            "  push:\n    branches: ['**']\n", "", _push_trigger_problems, "no push trigger",
            id="the push trigger is removed entirely",
        ),
        pytest.param(
            "    if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'\n",
            "", _push_trigger_problems, "no github.event_name gate",
            id="the self-hosted job loses its event gate",
        ),
        pytest.param(
            "    branches: ['**']", "    branches: ['**']\n    paths:\n      - 'backend/**'",
            _push_trigger_problems, "paths",
            id="a paths filter narrows the trigger without narrowing the branches",
        ),
        pytest.param(
            "    branches: ['**']", "    branches-ignore: ['m4*']",
            _push_trigger_problems, "branches-ignore",
            id="branches are excluded by name instead of included by pattern",
        ),
        pytest.param(
            "    if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'",
            "    if: github.event_name == 'push'",
            _push_trigger_problems, "on the push path",
            id="the self-hosted job is gated onto the push path",
        ),
        # The narrowing that is not written on the trigger at all. Every hosted job was
        # short-circuited past the `if:` read, so this -- the canonical "only run the expensive
        # job where it matters" -- left all three guards silent while a milestone branch's push
        # ran four cheap jobs and never the one that proves the shipped image.
        pytest.param(
            "  backend:\n    runs-on: ubuntu-latest",
            "  backend:\n    if: github.event_name == 'pull_request' || "
            "github.ref == 'refs/heads/main'\n    runs-on: ubuntu-latest",
            _push_trigger_problems, "gated to one branch by its own `if:`",
            id="a hosted job is pinned to the default branch by its own if",
        ),
        # The same gate deleted, but by an author who first simplified the runner to the one
        # label that identifies the machine. `self-hosted` is a label GitHub adds, not one the
        # job has to name, so this pair reaches the household box exactly as the two-label form
        # does -- and until the guard read the runner rather than the token, it reported nothing.
        pytest.param(
            "    if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'\n"
            "    runs-on: [self-hosted, spielplan-corpus]",
            "    runs-on: spielplan-corpus",
            _push_trigger_problems, "no github.event_name gate",
            id="the runner is named by its own label and the event gate goes with it",
        ),
        pytest.param(
            "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}",
            "cancel-in-progress: true", _cancellation_problems, "not the default branch",
            id="cancellation goes back to unconditional",
        ),
        pytest.param(
            "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}",
            "cancel-in-progress: ${{ github.ref == 'refs/heads/main' }}",
            _cancellation_problems, "not the default branch",
            id="the ref test is inverted, so main is the only run cancelled",
        ),
        pytest.param(
            "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}",
            "cancel-in-progress: ${{ true || github.ref }}",
            _cancellation_problems, "not the default branch",
            id="an expression that mentions the ref without testing it",
        ),
        pytest.param(
            "      - run: uv pip install -e \"backend[dev]\"\n\n  real-bundle:",
            "      - run: python -m pip install -e \"backend[dev]\"\n\n  real-bundle:",
            _interpreter_problems, "outside a uv venv",
            id="a job installs into the runner interpreter",
        ),
        pytest.param(
            "      - run: uv pip install -e \"backend[dev]\"\n\n  real-bundle:",
            "      - run: pip install --break-system-packages -e \"backend[dev]\"\n\n  real-bundle:",
            _interpreter_problems, "outside a uv venv",
            id="and the same install with PEP 668 overridden",
        ),
        # `pip3`, the numbered spelling an author reaches for once the venv is out of the picture.
        # `python3 -m pip` was caught and `pip3 install` was not, and the pyproject's CPU torch
        # index lives under `[tool.uv.*]` keys pip does not read, so either one resolves the CUDA
        # wheel §1 forbids -- quietly, on the image's own 3.12, which is there to be installed into.
        pytest.param(
            "      - run: uv pip install -e \"backend[dev]\"\n\n  real-bundle:",
            "      - run: pip3 install -e \"backend[dev]\"\n\n  real-bundle:",
            _interpreter_problems, "outside a uv venv",
            id="the install is spelled pip3 rather than python -m pip",
        ),
        # And the PEP-668 arm, fed the only shape that reaches it: `pip install
        # --break-system-packages` is caught one branch earlier with the other message, so until
        # this case existed that arm had never been shown saying anything.
        pytest.param(
            "runs-on: [self-hosted, spielplan-corpus]\n    steps:\n      - run: uv pip install",
            "runs-on: [self-hosted, spielplan-corpus]\n    steps:\n"
            "      - run: uv pip install --break-system-packages",
            _interpreter_problems, "overrides PEP 668",
            id="a venv install that overrides PEP 668 anyway",
        ),
    ],
)
def test_the_ci_guards_see_a_workflow_that_regressed(old, new, reader, needle):
    """Six regressions, each of which is what the file said before this milestone or one edit
    away from it. The workflow guards are the ones most at risk of reading as coverage: they run
    on every machine and can never observe a GitHub run, so the only thing standing between them
    and a vacuous pass is having been shown failing."""
    assert reader(_CLEAN_WORKFLOW) == [], "the guard does not pass the workflow it describes"
    assert old in _CLEAN_WORKFLOW, f"the fixture no longer contains {old!r}"
    problems = reader(_CLEAN_WORKFLOW.replace(old, new))
    assert problems, "the guard passed a regressed workflow"
    assert needle in " ".join(problems), problems


def _workflow_files() -> dict[str, str]:
    """Every workflow in the directory, by file name. The rule below is about an arrangement of
    files rather than about one file's contents, so reading only `ci.yml` would go quiet on the
    third workflow somebody adds -- which is exactly how the group went unread the first time."""
    return {
        path.name: _read(path)
        for pattern in ("*.yml", "*.yaml")
        for path in sorted(WORKFLOWS.glob(pattern))
    }


def test_no_self_hosted_job_queues_inside_the_group_a_push_to_main_waits_in():
    """A job nobody can pick up waits a day; the group it waits in must not be main's.

    `cancel-in-progress: false` on the default branch protects the run in flight. It cannot
    protect a run that never starts, and a workflow-level group is held by its slowest job -- so
    the weekly tick that puts a queueing self-hosted job in main's group turns the pending-run
    case from a three-push race into a two-push certainty, one day a week, until the runner is
    registered. [M4.8 review cycle 4, m48-c4-ci-01]
    """
    assert _self_hosted_group_problems(_workflow_files()) == []


# The header the corpus job gets once it is alone: two triggers, no group, no push path.
_CORPUS_ALONE = """\
on:
  workflow_dispatch:
  schedule:
    - cron: '17 5 * * 1'

jobs:
  real-bundle:"""


def test_the_group_guard_sees_the_arrangement_that_shipped():
    """The regressed workflow this one is shown failing on is the file as the milestone wrote it.

    `_CLEAN_WORKFLOW` is the shape `ci.yml` had until this finding: one file, one group, the
    push path and the corpus job together. It is the fixture the three guards above are proved
    against, which is the point -- all three passed it, and the cost was a day of main's pushes
    a week.
    """
    problems = _self_hosted_group_problems({"ci.yml": _CLEAN_WORKFLOW})
    assert problems, "the guard passed one workflow carrying both the push path and the corpus job"
    assert "pending behind it" in " ".join(problems), problems

    # Both exits, so the rule is not "no self-hosted runners". The split that shipped: the corpus
    # job in a file with no group and no push trigger, and `ci.yml` keeping both without it.
    hosted_only, _, corpus = _CLEAN_WORKFLOW.partition("  real-bundle:")
    assert corpus, "the fixture no longer carries the corpus job"
    own_file = _CORPUS_ALONE + corpus
    assert _self_hosted_group_problems({"ci.yml": hosted_only, "real-bundle.yml": own_file}) == []
    # And a group is fine on that file too, as long as no push creates runs of it: what costs is
    # the pairing, and a guard that forbade the key outright would be read as superstition.
    grouped = "concurrency:\n  group: real-bundle\n\n" + own_file
    assert _self_hosted_group_problems({"real-bundle.yml": grouped}) == []
    # A group under one job holds that job's own runs and nothing else in the run, so a
    # reader that took `concurrency:` at any indent would report an arrangement costing
    # nobody anything -- and a guard that cries on a clean file is one somebody deletes.
    per_job = _CLEAN_WORKFLOW.replace(
        "concurrency:\n  group: ci-${{ github.ref }}\n"
        "  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}\n",
        "",
    ).replace("  backend:\n", "  backend:\n    concurrency:\n      group: backend\n", 1)
    assert "\nconcurrency:" not in per_job and "      group: backend" in per_job, per_job
    assert _self_hosted_group_problems({"ci.yml": per_job}) == []


# --- what the workflow is SAID to do, where the two claims outran it --------------------
#
# The three guards above read the workflow. These two read the sentences about it, because a
# reader deciding whether to register the corpus runner, or whether a gate commit has a result,
# reads those and not the YAML -- and both sentences described a GitHub the documentation does
# not describe. Neither is a string compared with a copy of itself: each is a claim the workflow
# has to earn, and the earning is read out of the workflow one assertion above.
# [M4.8 review cycle 3: m48-c3-ci-02, m48-c3-ci-03]

LEDGER = REPO / "docs" / "TESTING.md"
COVERAGE_MAP = REPO / "backend" / "tests" / "spec_coverage.toml"


def _flat(text: str) -> str:
    """One line, comment markers dropped: these claims run across wrapped comment lines."""
    return " ".join(line.strip().lstrip("#").strip() for line in text.splitlines())


_QUEUE_CLAIM = re.compile(r"\bqueue[sd]?\b|\bqueuing\b|\bqueueing\b", re.I)


def _unclaimed_queue_claims(text: str, label: str) -> list[str]:
    """Sentences that say a job waits for a runner without saying the waiting ends."""
    problems = []
    for sentence in re.split(r"(?<=\.)\s", _flat(text)):
        if "spielplan-corpus" not in sentence or "registered" not in sentence:
            continue
        if _QUEUE_CLAIM.search(sentence) and "cancel" not in sentence.lower():
            problems.append(f"{label}: {sentence.strip()!r}")
    return problems


def test_the_weekly_tick_says_what_an_unclaimed_self_hosted_job_does_to_the_run():
    """A job nobody can pick up does not simply wait, and what the waiting costs has to be said.

    GitHub cancels a job that has sat in the queue for 24 hours, and a cancelled job denies its
    run a success conclusion. Decision 183 keeps the weekly schedule; what it does not license is
    telling the two readers who decide whether to register that runner that the consequence is
    nothing. `continue-on-error` is the fix that is refused: a job that cannot fail is what this
    milestone exists to remove. [M4.8 review cycle 3: m48-c3-ci-02]

    What that day costs is now bounded to this workflow, which is the whole reason it is a
    workflow: it used to be one run of six jobs on the default branch, five passing and one
    hanging, holding `ci-refs/heads/main` for a day a week while main's pushes queued behind it.
    The `needs:` read this test used to carry was standing in for that reach and never got
    there -- `needs:` is not what a workflow-level group blocks -- so it is gone, and
    `test_no_self_hosted_job_queues_inside_the_group_a_push_to_main_waits_in` holds the property
    for real. What is left here is the shape the split has to keep: two triggers and no push
    path, one job so nothing else's conclusion rides on a run that ends cancelled, and `ci.yml`
    still answering its own copy of the tick. [M4.8 review cycle 4, m48-c4-ci-01]
    """
    workflow = _read(CORPUS)
    triggers = _nested(workflow, "on")
    assert _nested(triggers, "schedule"), "the weekly tick is gone from the corpus trigger"
    assert _nested(triggers, "workflow_dispatch"), "the corpus check can no longer be asked for"
    reachable = [event for event in ("push", "pull_request") if _nested(triggers, event)]
    assert not reachable, f"{reachable} would put a branch's push on the household box"
    jobs = _jobs(workflow)
    assert list(jobs) == ["real-bundle"], (
        f"{sorted(jobs)}: a second job here shares the conclusion of a run that ends cancelled"
    )
    corpus = jobs["real-bundle"]
    assert not _HOSTED_IMAGE.search(_nested(corpus, "runs-on")), corpus
    gate = re.search(r"^ {4}if:\s*(?P<expr>.+)$", corpus, re.M)
    assert gate and "'schedule'" in gate.group("expr"), corpus
    assert "continue-on-error" not in corpus, "the corpus job was made unable to fail"

    # The tick `ci.yml` keeps is the free weekly regression check on main, and it is only free
    # while the run it creates is five hosted jobs that finish.
    hosted = _jobs(_read(CI))
    assert _nested(_nested(_read(CI), "on"), "schedule"), "ci.yml lost the weekly tick in the split"
    unconditional = [name for name, block in hosted.items() if not re.search(r"^ {4}if:", block, re.M)]
    assert unconditional, "every job is gated: the tick no longer produces a run of hosted jobs"

    claims = [
        problem
        for path in (CI, CORPUS, LEDGER)
        for problem in _unclaimed_queue_claims(_read(path), path.name)
    ]
    assert not claims, (
        "a queued self-hosted job is cancelled after 24 h and takes the run's conclusion with "
        "it; these say the waiting is free:\n  " + "\n  ".join(claims)
    )

    # Shown failing, on the sentence that shipped in both files.
    shipped = (
        "It gates nothing, blocks no merge, and until a runner labelled `spielplan-corpus` is "
        "registered it simply queues."
    )
    assert _unclaimed_queue_claims(shipped, "shipped"), shipped


# The setting protects a run that is RUNNING. GitHub's default for the group -- `queue: single`,
# which is what a concurrency block without a `queue:` key gets -- cancels an existing PENDING
# run when a new one is queued, whatever `cancel-in-progress` says, and `queue: max` is the
# documented opt-out. Adding that key is a live-service change with no way to exercise it from
# here and is the owner's call; what is not is claiming the property unqualified.
_CANCELLATION_CLAIM = re.compile(r"\b(?:not|never|no longer)\s+cancell?(?:able|ed)\b", re.I)
_BOUNDED = re.compile(r"\bpending\b|\bin[- ]flight\b", re.I)


def _unqualified_cancellation_claims(text: str, label: str) -> list[str]:
    """Claims that main's run survives a later push, made without the pending case."""
    flat = _flat(text)
    problems = []
    for match in _CANCELLATION_CLAIM.finditer(flat):
        window = flat[max(0, match.start() - 180):match.end() + 220]
        if not _BOUNDED.search(window):
            problems.append(f"{label}: {window.strip()!r}")
    return problems


def test_the_default_branch_claim_is_bounded_to_the_run_in_flight():
    """`cancel-in-progress: false` buys one push of protection, not every push.

    With run A in flight on main, B pending behind the group and C arriving, B is cancelled
    while pending and B's commit ends with no completed run -- the M4 merge symptom the setting
    was added to close, one push later. The milestone's own exit criterion measures the two-push
    case, which is the case that passes. So the guard the row registers is right about what it
    reads and the prose around it was not: this holds the two artefacts that state the OUTCOME
    to the bound the workflow actually has, and lifts as soon as a `queue:` key gives them the
    unbounded one. [M4.8 review cycle 3: m48-c3-ci-03]
    """
    concurrency = _nested(_read(CI), "concurrency")
    assert "group:" in concurrency and "github.ref" in concurrency, concurrency
    if re.search(r"^\s*queue:\s*max\b", concurrency, re.M):
        pytest.skip("the group queues rather than superseding: the unbounded claim is now true")

    claims = [
        problem
        for path in (LEDGER, COVERAGE_MAP)
        for problem in _unqualified_cancellation_claims(_read(path), path.name)
    ]
    assert not claims, (
        "the concurrency block carries no `queue:` key, so a run pending in the group is "
        "superseded by the next push; these claim more than that:\n  " + "\n  ".join(claims)
    )

    # Shown failing, on the sentence that shipped in the ledger.
    shipped = "A run on the default branch is no longer cancellable by the next push."
    assert _unqualified_cancellation_claims(shipped, "shipped"), shipped


# --- Review cycle 1: the scaffolding cannot manufacture a verdict -----------------------
#
# Both defects below were in this milestone's own new code, and both are its thesis turned back
# on it: a harness reporting something the app never did. `reset.mjs`'s `.env` reader refused a
# line `docker compose` accepts, so the gate could not be run at all and the refusal blamed the
# operator's value rather than the parser; `14-tonight.spec.js`'s "nothing was sent" watcher
# attached its handler after the click it was racing, so a slow click failed the file with a
# timeout naming no assertion. Neither is visible in the text of the artifact, so unlike every
# guard above this section RUNS the parser -- lifted out of the shipped file and executed by the
# runtime that executes the shipped file -- and reads the spec only for the shape no runtime here
# can reach. [M4.8 review, E2E-1 and E2E-2]

_ENV_DRIVER = """\
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
const ROOT = process.argv[2];
__PARSER__
console.log(JSON.stringify(env(process.argv[3]) ?? null));
"""

# The five forms this repository, its example and its workflow between them write, plus the two
# that say the comment rule is a rule about ` #` and not about `#`, plus the one that says a key
# assigned twice resolves to its LAST assignment. Each of the first three worked on its own
# before review cycle 1; it was the composite -- quotes AND a comment, the shape compose reads
# without blinking -- that matched neither arm.
_ENV_LINES = [
    pytest.param('PUBLIC_URL="http://localhost:8080" # dev',
                 "PUBLIC_URL", "http://localhost:8080", id="double-quoted with a comment"),
    pytest.param("export PUBLIC_URL='http://localhost:8080' # dev",
                 "PUBLIC_URL", "http://localhost:8080", id="exported, single-quoted, commented"),
    pytest.param("PUBLIC_URL=http://localhost:8080 # dev",
                 "PUBLIC_URL", "http://localhost:8080", id="bare with a comment"),
    pytest.param('PUBLIC_URL="http://localhost:8080"',
                 "PUBLIC_URL", "http://localhost:8080", id="quoted, no comment"),
    pytest.param("export PUBLIC_URL=http://localhost:8080",
                 "PUBLIC_URL", "http://localhost:8080", id="exported, bare (this repo's .env)"),
    pytest.param('POSTGRES_PASSWORD="pa#ss" # set by ops',
                 "POSTGRES_PASSWORD", "pa#ss", id="a quoted value keeps its own hash"),
    pytest.param("POSTGRES_PASSWORD=pa#ss",
                 "POSTGRES_PASSWORD", "pa#ss", id="an unquoted hash with no space before it"),
    # `_parse_with` writes one file per case, so a case can be more than one line: this is the
    # shape appending produces, and the only one where "the value the stack booted on" and "the
    # first line that mentions the key" are different strings.
    pytest.param("PUBLIC_URL=http://localhost:8080\n"
                 "# the household's real stack, appended when it went live:\n"
                 "PUBLIC_URL=https://spielplan.example",
                 "PUBLIC_URL", "https://spielplan.example",
                 id="a key assigned twice: the last assignment is the one compose uses"),
]

# The reader as it shipped, and the reason this section executes anything at all: the difference
# between it and the one in the tree is the ORDER of two operations and a lazy quantifier, which
# no text guard could notice without simply spelling out the fixed regex and comparing strings.
_PARSER_THAT_SHIPPED = r"""function env(key) {
  const file = join(ROOT, '.env');
  if (!existsSync(file)) return undefined;
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const [k, ...rest] = line.replace(/^\s*export\s+/, '').split('=');
    if (k.trim() !== key) continue;
    const raw = rest.join('=').trim();
    const quoted = /^(['"])([\s\S]*)\1$/.exec(raw);
    return quoted ? quoted[2] : raw.replace(/\s+#.*$/, '').trim();
  }
  return undefined;
}
"""


def _env_parser(source: str) -> str:
    """`env.mjs`'s `env()` as text, to be run rather than read.

    Lifted by bracket matching rather than copied, so the thing under test is the shipped
    function and a later edit to it is what these cases meet. Its body reaches nothing but
    `ROOT`, `join`, `existsSync` and `readFileSync`, which is why a four-line driver can host
    it -- importing the module instead would run `docker compose` and drop a database.
    """
    start = source.find("function env(")
    assert start >= 0, "e2e/env.mjs no longer defines env(): this guard is reading nothing"
    _, end = _span(source, start, "{", "}")
    assert end > 0, "e2e/env.mjs's env() has unbalanced braces"
    return source[start:end]


def _parse_with(parser: str, tmp_path: Path, line: str, key: str):
    node = shutil.which("node")
    if node is None:
        # The one honest skip: `node` is the runtime that runs the file under test, so a machine
        # without it cannot run `e2e/reset.mjs` either. Every CI runner ships it.
        pytest.skip("node is required -- it is the runtime that runs e2e/reset.mjs")
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    (tmp_path / "parse.mjs").write_text(
        _ENV_DRIVER.replace("__PARSER__", parser), encoding="utf-8"
    )
    out = subprocess.run(
        [node, str(tmp_path / "parse.mjs"), str(tmp_path), key],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("line, key, value", _ENV_LINES)
def test_the_reset_parser_reads_the_values_compose_reads(tmp_path, line, key, value):
    """The parser and the stack it resets have to agree about what PUBLIC_URL *is*, because the
    guard standing between an operator and their data is a prefix test on what this returns. A
    value compose accepts and this mangles is a refusal that cannot be argued with."""
    assert _parse_with(_env_parser(_read(ENV_MJS)), tmp_path, line, key) == value


def test_the_reset_parser_harness_sees_the_order_that_shipped(tmp_path):
    """And the same harness, fed the reader as it shipped, reproduces the refusal end to end.

    Without this the cases above are seven assertions that have never been shown failing --
    the exact shape this milestone exists to end. The chain is followed to its consequence
    rather than stopped at the return value: the quotes come back attached, and the prefix
    guard at `reset.mjs:53` then reports a development stack as not one and exits 1.
    """
    line, key, value = _ENV_LINES[0].values
    shipped = _parse_with(_PARSER_THAT_SHIPPED, tmp_path, line, key)
    assert shipped == '"http://localhost:8080"', (
        "the harness no longer reproduces the parse that shipped, so the cases above prove "
        "nothing about having caught it"
    )
    assert not re.match(r"^https?://(localhost|127\.0\.0\.1)", shipped), (
        "reset.mjs:53's prefix guard would have accepted the mangled value after all"
    )
    assert _parse_with(_env_parser(_read(ENV_MJS)), tmp_path, line, key) == value


# A wait bound to a name, where the name is later handed to `expect(...).rejects`. Only that
# combination matters: a wait expected to RESOLVE rejects solely on a timeout, which is a run
# that was failing anyway, while a wait expected to reject rejects on its normal outcome -- so
# leaving its handler until after an `await` is a race between two ordinary paths.
#
# Any receiver, not the literal `page`. Two of the eighteen spec files bind their page object
# under another name -- `15-tonight-group.spec.js:31-33` opens two contexts as `a` and `b`, and
# `18-system.spec.js:46,52` binds `admin` and already calls `admin.waitForResponse(` at `:63` and
# `:272` -- so the rule the coverage row states for every browser spec was enforced on the files
# that happen to spell it `page`. `15-tonight-group.spec.js:177` already carries the read-side
# twin of 14-tonight's negative, which makes that file the natural home of the next write-side
# one. The `.rejects` pairing below is what keeps the widening quiet: measured over all eighteen
# specs it reports nothing. A computed receiver (`pages[0]`) is still missed, which no spec
# writes today. [M4.8 review cycle 3: m48-c3-guard-page-only]
_WAIT_BINDING = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>\w+)\s*=\s*[\w.$]+\.waitFor(?:Request|Response|Event)\s*\("
)


def _unattached_rejection_problems(source: str) -> list[str]:
    problems = []
    for match in _WAIT_BINDING.finditer(source):
        name = match.group("name")
        rest = source[match.end():]
        # Bounded by `;` so the search stays inside one statement: a `.rejects` further down the
        # file belongs to some other assertion, and pairing it with this name would invent a
        # violation rather than find one.
        attach = re.search(rf"expect\(\s*{re.escape(name)}\s*(?:,[^;]*?)?\)\s*\.rejects\b", rest)
        if not attach:
            continue
        if re.search(r"\bawait\b", rest[:attach.start()]):
            problems.append(
                f"`{name}`'s rejection is the expected outcome, but its `.rejects` handler is "
                "attached after an `await`: a wait that times out first rejects with nothing "
                "listening, and the worker's unhandledRejection hook fails whichever test is "
                "running and then stops the worker"
            )
    return problems


def test_no_wait_whose_rejection_is_expected_is_attached_after_an_await():
    """A negative proved by a timeout has to be watched from before the action it is watching.

    `14-tonight.spec.js` proves that no control on the guest's screen can WRITE to the host's
    seat by letting a `waitForRequest` time out, which is the right instrument -- the sleep it
    replaced would have passed a retraction that took one millisecond longer. But the promise
    was created, then the Undo click was awaited, then the handler attached; a click slower
    than the 2 s window -- an actionability retry under CI load, a re-render -- rejects into
    nobody, and Playwright's worker turns that into a red naming no assertion, on a run in
    which nothing was written. The fix is the idiom the same file already uses around its
    answer clicks: attach where the promise is made.
    """
    for path in sorted(SPECS.glob("*.spec.js")):
        assert _unattached_rejection_problems(_read(path)) == [], path.name


# The statement as it shipped, and as it now stands. The regressed form is not hypothetical: it
# is what review cycle 1 read out of the file.
_ATTACHED_AFTER_THE_ACTION = """
    const strayWrite = page.waitForRequest(
      (req) => req.method() === 'POST' && req.url().includes(seatPrefix),
      { timeout: 2_000 }
    );
    if (await undo.isVisible().catch(() => false)) await undo.click();
    await expect(strayWrite, "the guest's screen wrote to the host's seat").rejects.toThrow();
"""

_ATTACHED_AT_CREATION = """
    const strayWrite = expect(
      page.waitForRequest(
        (req) => req.method() === 'POST' && req.url().includes(seatPrefix),
        { timeout: 2_000 }
      ),
      "the guest's screen wrote to the host's seat"
    ).rejects.toThrow();
    if (await undo.isVisible().catch(() => false)) await undo.click();
    await strayWrite;
"""

# A wait expected to resolve, awaited after an action: the pattern six sites in `11-rate` and
# `13-rank` use, and which this guard must never start reporting.
_RESOLUTION_EXPECTED = """
    const answer = page.waitForResponse((res) => res.status() === 200);
    await page.getByTestId('tonight-pick-A').click();
    await answer;
"""


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(_ATTACHED_AFTER_THE_ACTION, True, id="the statement as it shipped"),
        pytest.param(_ATTACHED_AT_CREATION, False, id="the statement as it now stands"),
        pytest.param(_RESOLUTION_EXPECTED, False, id="a wait expected to resolve, left alone"),
        # The same statement in the file that would host it next. `15-tonight-group.spec.js`
        # binds its two pages as `a` and `b` and already carries the read-side twin of this
        # negative at `:177`; the guard read the receiver `page` and nothing else, so the rule
        # its coverage row states for every browser spec held on sixteen of the eighteen.
        pytest.param(
            _ATTACHED_AFTER_THE_ACTION.replace("page.waitForRequest", "b.waitForRequest"),
            True,
            id="the same statement in a file whose page object is not called page",
        ),
    ],
)
def test_the_unattached_rejection_guard_sees_a_wait_that_can_out_run_its_handler(source, expected):
    """Both directions, because a guard that reported every deferred wait would be uninhabitable
    in this suite -- `Promise.all` and the six resolve-expected waits are the normal way to watch
    a request -- and one that reports none reads as coverage."""
    problems = _unattached_rejection_problems(source)
    assert bool(problems) is expected, problems
    if expected:
        assert "attached after an `await`" in " ".join(problems)


# --- Review cycle 2: what the harness says it did ---------------------------------------
#
# Two more of the same kind, and both in prose this milestone itself wrote: a comment claiming a
# fidelity to `docker compose` the parser under it did not have, and a failure message naming a
# budget six times smaller than the loop above it can spend. Neither is a wrong assertion about
# the app -- they are a harness describing itself inaccurately, which is the one thing a harness
# has to get right, because it is all an operator has to read. [M4.8 review cycle 2, E2E-1/E2E-2]

# The reader as it stood between cycle 1 and cycle 2: quoting and comments handled, and `return`
# still inside the loop.
_PARSER_THAT_RETURNED_THE_FIRST_MATCH = r"""function env(key) {
  const file = join(ROOT, '.env');
  if (!existsSync(file)) return undefined;
  for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const [k, ...rest] = line.replace(/^\s*export\s+/, '').split('=');
    if (k.trim() !== key) continue;
    const raw = rest.join('=').trim();
    const quoted = /^(['"])([\s\S]*?)\1\s*(?:#.*)?$/.exec(raw);
    return quoted ? quoted[2] : raw.replace(/\s+#.*$/, '').trim();
  }
  return undefined;
}
"""

_APPENDED_TWICE = (
    "PUBLIC_URL=http://localhost:8080\n"
    "# the household's real stack, appended when it went live:\n"
    "PUBLIC_URL=https://spielplan.example"
)

# `reset.mjs`'s development-stack test, copied because the consequence is what this section is
# about: a stale value that passes it is a `DROP DATABASE` decided from a line the stack is not
# running on, and the same guard fed the live value refuses.
_DEV_STACK = re.compile(r"^https?://(localhost|127\.0\.0\.1)")


def test_the_reset_parser_resolves_a_key_the_way_the_stack_that_booted_did(tmp_path):
    """The last assignment wins, because it is the one `docker compose` starts the stack on.

    compose's dotenv, `python-dotenv` (the reader the app's own settings come through) and `sh`
    all build a map in file order, so a second `PUBLIC_URL=` appended under the first is the
    value the running stack has; returning the first match read a line the stack is not on.
    Followed to its consequence rather than stopped at the return value: on the file below the
    first match passes the development-stack guard and the last one does not, so the difference
    between the two readers is whether `DROP DATABASE` runs against a production database.
    """
    stale = _parse_with(_PARSER_THAT_RETURNED_THE_FIRST_MATCH, tmp_path, _APPENDED_TWICE,
                        "PUBLIC_URL")
    assert stale == "http://localhost:8080", (
        "the harness no longer reproduces the reader that returned the first match, so this "
        "case proves nothing about having caught it"
    )
    assert _DEV_STACK.match(stale), (
        "the stale value would have been refused anyway, so this file cannot show what reading "
        "the first assignment costs"
    )
    live = _parse_with(_env_parser(_read(ENV_MJS)), tmp_path, _APPENDED_TWICE, "PUBLIC_URL")
    assert live == "https://spielplan.example"
    assert not _DEV_STACK.match(live), "the guard above `DROP DATABASE` accepts the live value"


# A duration a console message claims, in the spellings these two files could reach for. `60s`,
# `60 s`, `60 seconds` and `6 minutes` all reduce to seconds; `1000ms` is not a claim (`m` is not
# at a word boundary there) and neither is `60 attempts`, which is the honest thing to name.
_DURATION_CLAIM = re.compile(r"(?P<n>\d+)\s*(?P<unit>seconds?|minutes?|min|[sm])\b")
_LOOP_HEAD = re.compile(r"for\s*\(\s*(?:let|var)\s+(?P<var>\w+)\s*=\s*0\s*;\s*(?P=var)\s*<\s*"
                        r"(?P<count>\d+)\s*;")
_TIMEOUT_MS = re.compile(r"AbortSignal\.timeout\(\s*(\d+)\s*\)")
_SLEEP_MS = re.compile(r"setTimeout\(\s*[^,]*,\s*(\d+)\s*\)")


def _health_loop_bound(source: str) -> tuple[int, int, int] | None:
    """(where the health loop's body starts, its attempt count, milliseconds per attempt).

    An attempt costs its request deadline plus the sleep at the tail of the loop, so the bound
    the loop actually carries is the product -- not the count of seconds it sleeps for. The loop
    is found by the `fetch` inside it rather than by the URL that fetch names: `reset.mjs` polls
    a `URL` object built before the drop, so its call site says `fetch(health, ...)` and a search
    for the path string finds nothing there at all.
    """
    if "/api/health" not in source:
        return None
    fetches = [m.start() for m in _FETCH.finditer(source)]
    for head in reversed(list(_LOOP_HEAD.finditer(source))):
        start, end = _span(source, head.end(), "{", "}")
        if start < 0 or not any(start < at < end for at in fetches):
            continue
        body = source[start:end]
        deadline = max((int(ms) for ms in _TIMEOUT_MS.findall(body)), default=0)
        sleep = max((int(ms) for ms in _SLEEP_MS.findall(body)), default=0)
        return start, int(head.group("count")), deadline + sleep
    return None


def _wait_message_problems(source: str) -> list[str]:
    """Whether any message the health loop's failure path prints understates what it waited.

    The messages predate the request deadline, and the deadline is what made their number wrong:
    an attempt against a backend that accepts and never answers cost nothing measurable before
    (`fetch` returned on ECONNREFUSED at once, or hung for ever), and now costs the full 5 s.
    Sixty of those is six minutes under a sentence saying sixty seconds -- and the operator who
    reads it raises the attempt count, because the run looks like a slow boot rather than like
    sixty consecutive stalls. Which is the diagnosis the deadline was added to make visible.
    """
    bound = _health_loop_bound(source)
    if bound is None:
        return ["no health loop found, so this guard is reading nothing"]
    body, count, per_attempt = bound
    worst = count * per_attempt / 1000
    problems = []
    for call in re.finditer(r"console\.(?:error|log)\s*\(", source):
        if call.start() < body:
            continue
        start, end = _span(source, call.end() - 1, "(", ")")
        if start < 0:
            continue
        for claim in _DURATION_CLAIM.finditer(source[start:end]):
            seconds = int(claim.group("n")) * (60 if claim.group("unit")[0] == "m" else 1)
            if seconds < worst:
                problems.append(
                    f"line {_line_of(source, call.start())}: the health loop can spend "
                    f"{worst:g}s ({count} attempts of up to {per_attempt} ms), and this message "
                    f"claims {claim.group(0)!r}"
                )
    return problems


def test_no_health_loop_claims_a_shorter_wait_than_it_can_spend():
    """Both halves of the harness, because both print the sentence an operator debugs from.

    `run.mjs` reports the restart that section 10's swap sequence ends in, and `reset.mjs`
    reports the boot after the drop; each is the only output of a branch that exists so a
    failure is diagnosable at all. A message that misstates its own budget by six times sends
    the reader after a slow boot instead of after a stalled one.
    """
    assert _wait_message_problems(_read(RUNNER)) == []
    assert _wait_message_problems(_read(RESET)) == []


# The loop as it stands, with the messages that could sit under it: the one that shipped, the one
# that names attempts, and the one that names minutes correctly for a count since raised.
_LOOP = """
for (let i = 0; i < %d; i++) {
  try {
    const res = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(5000) });
    if (res.ok && (await res.json()).bundle) { loaded = true; break; }
  } catch { /* not up yet */ }
  await new Promise((r) => setTimeout(r, 1000));
}
console.error(%s);
"""


@pytest.mark.parametrize(
    "count, message, expected",
    [
        pytest.param(60, "'the backend did not become healthy within 60s'", True,
                     id="the message as it shipped"),
        pytest.param(60, "'the backend did not become healthy in 60 attempts (up to 6 minutes)'",
                     False, id="the message as it now stands"),
        pytest.param(120, "'the backend did not become healthy in 120 attempts (up to 6 minutes)'",
                     True, id="the count is raised and the ceiling beside it is not"),
        pytest.param(60, "'the backend did not become healthy within 360 seconds'", False,
                     id="a number that is simply true"),
    ],
)
def test_the_wait_message_guard_sees_a_budget_that_is_not_the_loops(count, message, expected):
    """Both directions, and the raised-count case for the reason the guard reads the loop rather
    than the string: a message can only be honest about a bound it is measured against, and the
    edit most likely to make it dishonest again is the one the comment above the loop invites --
    raising the attempt count because a real corpus bundle needs longer."""
    problems = _wait_message_problems(_LOOP % (count, message))
    assert bool(problems) is expected, problems
    if expected:
        assert "claims" in " ".join(problems), problems


# --- Review cycle 3: the harness's own account of the harness ---------------------------

# Three sentences this milestone wrote into the two files a maintainer opens to learn what the
# harness guarantees, and none of them survived being measured against the tree it describes.
# They are guarded together because the failure is one failure: `e2e/playwright.config.js` and
# `e2e/helpers.js` are read as the contract, so a rule stated there is acted on -- and one of the
# three argues, in the file opened first, for exactly the edit
# `test_the_jellyfin_spec_is_not_given_a_has_bundle_guard` fails the build over. Nothing here
# reads a runtime; each guard weighs a claim against the directory, the normative document or the
# module that owns the number, which is the only reason a comment can be held at all.
CONFIG = REPO / "e2e" / "playwright.config.js"
HELPERS = REPO / "e2e" / "helpers.js"
WORKER = REPO / "backend" / "spielplan" / "worker.py"
SPEC_DOC = REPO / "docs" / "spielplan-spec_v2.1.md"

# The leading block comment, which is the whole of what the config teaches: everything below it
# is `defineConfig`, and a reader who has reached `projects` has already taken the convention.
_HEADER = re.compile(r"/\*\*(?P<body>.*?)\*/", re.S)
# A claim over the spec DIRECTORY rather than over a named file. The dot-free span keeps it to
# one sentence: "every other spec", "all the specs", and not two sentences either side of a full
# stop that happen to contain both words.
_UNIVERSAL_OVER_SPECS = re.compile(r"\b(?:every|all)\b[^.]{0,40}\bspecs?\b")


def _unguarded_specs() -> set[str]:
    """The spec files carrying no `config.has_bundle` skip at all."""
    return {
        path.name for path in sorted(SPECS.glob("*.spec.js"))
        if not _has_a_has_bundle_guard(_read(path))
    }


def _has_bundle_claim_problems(source: str, unguarded: set[str]) -> list[str]:
    """What the config's header says about `has_bundle`, weighed against the spec directory.

    Two things are wrong with a universal here and the arithmetic is the lesser one. It is false
    -- measured, nine of the eighteen files carry no such guard, and two of those skip on
    `browserName` instead, which is a different axis -- but the reason a false rule matters in
    THIS file is that `08-jellyfin.spec.js` must never acquire the guard. It is the one spec left
    that FAILS on a stack that imported nothing, and without it such a run is a suite of skips
    reported as green. So the header may not teach the convention without naming the exception,
    and may not generalise over a directory that contradicts it.

    The naming leg reads the whole block rather than a window: a maintainer reads the paragraph,
    not a span, and the guard's own name is itself the longest `has_bundle` mention the block can
    contain. The universal leg keeps its window, because a quantifier three paragraphs away is
    about something else.
    """
    header = _HEADER.search(source)
    if header is None:
        return ["e2e/playwright.config.js has no header block, so this guard is reading nothing"]
    body = header.group("body")
    if "has_bundle" not in body:
        return []
    problems = []
    if "08-jellyfin" not in body:
        problems.append(
            f"line {_line_of(source, header.start('body'))}: the header teaches the has_bundle "
            "convention and does not name 08-jellyfin.spec.js, the file that must not have it"
        )
    for claim in _UNIVERSAL_OVER_SPECS.finditer(body):
        window = body[max(0, claim.start() - 200):claim.end() + 200]
        if "has_bundle" in window and unguarded:
            problems.append(
                f"line {_line_of(source, header.start('body') + claim.start())}: "
                f"{claim.group(0)!r} states the has_bundle convention over the whole directory, "
                f"and {len(unguarded)} files carry no such guard: {sorted(unguarded)}"
            )
    return problems


def test_the_config_does_not_teach_the_guard_08_jellyfin_must_not_have():
    """The convention as the file that teaches it states it, held to the directory it is about.

    `test_the_jellyfin_spec_is_not_given_a_has_bundle_guard` catches the edit; this catches the
    sentence that recommends it, which is the earlier and cheaper place. A maintainer who reads
    an unqualified rule here, opens the spec that fails when nothing imported and adds the guard
    for consistency is doing what the file told them to -- and the seven OTHER unguarded specs,
    which no guard protects at all, are where that same reading does land.
    """
    assert _has_bundle_claim_problems(_read(CONFIG), _unguarded_specs()) == []


# The header as it stands with the sentence that closes it substituted: the one that shipped, the
# one that names the exception, the half-fix that names it and keeps the universal, and a header
# that teaches nothing about `has_bundle` at all.
_CONFIG_HEADER = """
/**
 * End-to-end tests against the real stack.
 *
 * The suite runs in TWO PHASES: phase 1 runs `specs/01-first-boot.spec.js` alone against an
 * empty database and phase 2 runs everything else with `--grep-invert @first-boot`.%s
 */
export default defineConfig({ testDir: './specs' });
"""
_UNIVERSAL = (
    " So `@first-boot` says which phase owns a file, and every other spec guards itself on "
    "`config.has_bundle` instead."
)


@pytest.mark.parametrize(
    "tail, expected",
    [
        pytest.param(_UNIVERSAL, True, id="the sentence as it shipped"),
        pytest.param(
            " Most of the files that need an imported bundle skip themselves on "
            "`config.has_bundle`; `08-jellyfin.spec.js` deliberately does not.",
            False, id="the exception named, no claim over the directory",
        ),
        pytest.param(
            _UNIVERSAL + " `08-jellyfin.spec.js` is the exception.",
            True, id="the exception named and the universal kept",
        ),
        pytest.param(" A test that needs the backend needs phase 2, not a tag.", False,
                     id="the header teaches nothing about has_bundle"),
    ],
)
def test_the_has_bundle_claim_guard_sees_a_universal_the_directory_contradicts(tail, expected):
    """Both legs and both directions. The third case is the one worth writing down: naming the
    exception while keeping the universal reads as a fix and is the same recommendation, because
    a reader who has just been told the rule is universal takes one named file for an oversight
    rather than for the rule."""
    problems = _has_bundle_claim_problems(_CONFIG_HEADER % tail, {"08-jellyfin.spec.js"})
    assert bool(problems) is expected, problems


# `fold-in tick`, `folds in every 60 s`, `fold in` -- the spellings a comment reaches for.
_FOLD_IN = re.compile(r"\bfolds?[ -]in\b", re.I)
_FOLD_IN_SECTION = "\u00a75.3"
# The spec files that carried the same shorthand before this milestone and still do. They are
# comments rather than printed messages and their files are M4.6's, so widening the guard onto
# them would be an edit this milestone was not asked to make; what it does instead is refuse to
# let the set grow, which is the half a guard can honestly hold.
#
# It named a third until decision 165 retired the TV client with `16-tonight-tv.spec.js`. Kept in
# step deliberately: the set is consumed as `grown <= ...`, so a dead entry only widens a
# permission nothing claims -- but a sentence that has stopped being true is how the next reader
# learns to distrust the rest of it. [M4.12 review cycle 1: M412-FE-4]
_FOLD_IN_SHORTHAND_PREDATING_M48 = {
    "14-tonight.spec.js",
    "15-tonight-group.spec.js",
}


def _fold_in_cadence_in_the_spec() -> str:
    """The cadence section 5.3's own jobs table gives the fold-in, read out of the spec."""
    for line in _read(SPEC_DOC).splitlines():
        if line.startswith("|") and "Fold-in" in line:
            return line.split("|")[2].strip()
    raise AssertionError("section 5.3's jobs table no longer has a fold-in row")


def _fold_in_tick_period() -> int:
    """The `every=` of the worker's `fold-in-tick` job, read out of the module that owns it."""
    match = re.search(r'Job\(\s*"fold-in-tick".*?every=(\d+)', _read(WORKER), re.S)
    assert match, "worker.py no longer registers a fold-in-tick job"
    return int(match.group(1))


def _fold_in_miscitations(source: str, cadence: str) -> list[str]:
    """Every place naming the fold-in beside section 5.3 without that table's word for it.

    The tick is not the spec's. `worker.py` says so where it registers it -- "Not in section
    5.3's table ... section 5.3 gives the fold-in a nightly cadence" -- and registers
    `fold-in-user-vectors` at `every=86400` two lines above, which IS the row the table has. So a
    comment writing "section 5.3's fold-in tick" has not rounded a citation off; it has named the
    wrong one of two registered jobs whose cadences differ by a factor of 1440.

    What the guard requires is the correction rather than the absence: a passage may cite the
    section beside the tick as long as it also carries the cadence the table actually gives,
    which is the only way to write that sentence truthfully. The window is a passage rather than
    a sentence because the citation itself carries the punctuation a splitter would cut on.
    """
    problems = []
    for match in _FOLD_IN.finditer(source):
        window = source[max(0, match.start() - 220):match.end() + 220]
        if _FOLD_IN_SECTION not in window or cadence in window:
            continue
        problems.append(
            f"line {_line_of(source, match.start())}: the fold-in is cited to section 5.3, whose "
            f"table gives it {cadence!r} -- the 60 s tick is the worker's own job"
        )
    return problems


def test_no_fold_in_cadence_is_attributed_to_a_section_that_does_not_give_it():
    """The shared helper, because it is the file whose citation is PRINTED.

    `waitForPool`'s poll message is what a member reads when a Tonight spec times out, and its
    whole job is to send them somewhere. Sent to section 5.3 they find "nightly", cannot check
    the arithmetic the 120 s ceiling rests on, and raise the ceiling back to a number with
    nothing behind it -- the defect the message was rewritten to remove. Both premises are read
    rather than assumed, so an amended spec or a retuned worker turns this red here instead of
    quietly making the comments right for a new reason.
    """
    cadence = _fold_in_cadence_in_the_spec()
    assert cadence == "nightly", (
        f"section 5.3's fold-in row now reads {cadence!r}: if the spec has taken the tick into "
        "its table, this guard and the comments it holds are both out of date"
    )
    period = _fold_in_tick_period()
    assert period == 60, (
        f"the worker's fold-in tick now runs every {period} s, so the two-tick arithmetic behind "
        "the 120 s ceiling in e2e/helpers.js no longer holds"
    )
    assert _fold_in_miscitations(_read(HELPERS), cadence) == []
    grown = {
        path.name for path in sorted(SPECS.glob("*.spec.js"))
        if _fold_in_miscitations(_read(path), cadence)
    }
    assert grown <= _FOLD_IN_SHORTHAND_PREDATING_M48, (
        "the section 5.3 shorthand has spread to "
        f"{sorted(grown - _FOLD_IN_SHORTHAND_PREDATING_M48)}"
    )


# Every `NN-name` a comment reaches for, with or without the suffix: the config drops it -- "and
# 16-tonight-tv is a television" -- and that is the spelling which outlived the file, so a guard
# anchored on `.spec.js` would have read straight past it. A letter is required after the number
# so that a date (`2026-09-11`) is not read as a spec file.
_SPEC_NAMED = re.compile(r"\b(\d{2}-[a-z][a-z0-9-]*)(?:\.spec\.js)?\b")


def _specs_named_that_are_gone(source: str) -> list[str]:
    """Every spec file a passage names that `e2e/specs` does not have."""
    live = {path.name.removesuffix(".spec.js") for path in SPECS.glob("*.spec.js")}
    return [
        f"line {_line_of(source, match.start())}: names {match.group(1)}.spec.js, which is not "
        "in e2e/specs"
        for match in _SPEC_NAMED.finditer(source)
        if match.group(1) not in live
    ]


def test_the_harness_names_no_spec_file_that_does_not_exist():
    """A deleted surface takes its spec with it, and the prose that funds the spec too.

    Decision 165 retires the TV client, so its route, `e2e/specs/16-tonight-tv` and its coverage
    row went together -- each was the others' red gate. The route's directory is named in the
    coverage map's note and not here: check 9 of `ops/m412_exit_criterion.py` searches
    `backend/tests` for the route literal, and a docstring that spells the path is reported as
    funding a client this milestone deleted. What no gate could see is the
    config's own reasoning: the phone project's `testMatch` is shaped the way it is for two stated
    reasons, and one of them was "16-tonight-tv is a television". That comment is the document a
    maintainer reads when deciding whether a new Tonight spec belongs on the phone, and it cited a
    file that does not exist -- which is how the next reader concludes the matrix was pruned for a
    reason it no longer has, or goes looking for a spec that was deleted on purpose.

    The allowance below is held to the same rule for the same reason: a set of files that "carried
    the same shorthand before this milestone and still do" cannot name one that is gone. It is
    consumed as `grown <= ...`, so a dead entry widens a permission nothing claims -- harmless
    today, and a sentence that has stopped being true either way.
    [decision 165; M4.12 finding 43; M4.12 review cycle 1: M412-FE-4]
    """
    assert _specs_named_that_are_gone(_read(CONFIG)) == []
    dead = _FOLD_IN_SHORTHAND_PREDATING_M48 - {path.name for path in SPECS.glob("*.spec.js")}
    assert not dead, f"the fold-in allowance names spec files that are gone: {sorted(dead)}"


def test_the_spec_name_guard_sees_a_comment_that_outlived_its_file():
    """Both arms, because a reader that finds nothing anywhere is a guard that passes for free."""
    gone = _specs_named_that_are_gone("// 16-tonight-tv is a television, so it stays on desktop")
    assert len(gone) == 1 and "16-tonight-tv" in gone[0], gone
    assert _specs_named_that_are_gone("// 14-tonight.spec.js runs on the phone too") == []
    # A date is not a spec file, and this harness writes them.
    assert _specs_named_that_are_gone("// measured on 2026-09-11, on Docker Desktop") == []


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param("// \u00a75.3 folds in every 60 s, so two ticks have passed", True,
                     id="the message as it shipped"),
        pytest.param("// the worker's fold-in tick is `every=60`, outside \u00a75.3's nightly "
                     "table", False, id="the tick cited where it lives"),
        pytest.param("// \u00a75.3's fold-in tick is `every=60` (`worker.py`)", True,
                     id="the module named and the section still given the number"),
        pytest.param("// the fold-in tick is `every=60` (`worker.py`)", False,
                     id="no section cited at all"),
    ],
)
def test_the_fold_in_citation_guard_sees_a_cadence_the_section_does_not_give(text, expected):
    """The third case is why the guard requires the cadence rather than forbidding the citation:
    the shipped comment already named `backend/spielplan/worker.py` and still called the tick the
    section's, so naming the module is not the repair, and a guard satisfied by it would pass the
    very text it was written against."""
    problems = _fold_in_miscitations(text, "nightly")
    assert bool(problems) is expected, problems


# A private copy of the seeding pair, by the definition that makes it one: a spec declaring
# either half itself rather than importing it. `13-rank.spec.js` had both until decision 186.
_SEEDING_PAIR = re.compile(r"\bfunction\s+(?:createMember|signInAsMember)\b")


def _specs_with_a_private_seeding_copy() -> set[str]:
    return {
        path.name for path in sorted(SPECS.glob("*.spec.js"))
        if _SEEDING_PAIR.search(_read(path))
    }


def _seeding_reach_problems(source: str, copies: set[str]) -> list[str]:
    """Whether the shared helper claims a reach the spec directory gives it.

    Decision 186's own Cost paragraph records that `11-rate.spec.js` keeps a third copy of the
    pair deliberately -- desktop-only, green, named by no finding -- so the copy is not the
    defect; a helper saying the suite has one seeding path while it stands is. The claim is also
    already falsified by this milestone's own diff: `reuse`, the repair against a roster that
    grew by a member per run, landed here and did not reach 11-rate, which still mints a
    timestamped member on every run.

    Naming is read over the whole file rather than beside the claim, because a rewording moves
    the claim and the reader needs the file to name the copy wherever in it they are. The word
    `everywhere` is checked on its own because it is the one word that turned a true statement
    about the importers into a false one about the directory.
    """
    problems = []
    for name in sorted(copies):
        if name.removesuffix(".spec.js") not in source:
            problems.append(
                f"{name} declares its own createMember/signInAsMember and e2e/helpers.js does "
                "not name it, so a repair made here reads as a repair everywhere"
            )
    if copies:
        for claim in re.finditer(r"\beverywhere\b", source):
            problems.append(
                f"line {_line_of(source, claim.start())}: 'everywhere' claims a reach the spec "
                f"directory contradicts: {sorted(copies)}"
            )
    return problems


def test_the_shared_seeding_helper_names_every_private_copy_of_its_pair():
    """The milestone's own thesis, in a file the milestone rewrote: an instrument claiming more
    than it does. The failure it invites is the one decision 186 exists because of -- the M4.6
    re-login that reached 14-tonight and not 13-rank -- and the next person to fix a seeding bug
    reads this comment, not the proposals document."""
    assert _seeding_reach_problems(_read(HELPERS), _specs_with_a_private_seeding_copy()) == []


@pytest.mark.parametrize(
    "text, copies, expected",
    [
        pytest.param("// decision 186 deletes that copy, so there is one seeding path and a "
                     "repair made here is a repair everywhere.", {"11-rate.spec.js"}, True,
                     id="the sentence as it shipped"),
        pytest.param("// the specs that import this file share one path; `11-rate.spec.js` "
                     "keeps a copy of its own.", {"11-rate.spec.js"}, False,
                     id="the remaining copy named"),
        pytest.param("// `11-rate.spec.js` keeps a copy, and a repair made here is a repair "
                     "everywhere.", {"11-rate.spec.js"}, True,
                     id="the copy named and the reach still claimed"),
        pytest.param("// a repair made here is a repair everywhere.", set(), False,
                     id="the claim is true once the last copy is gone"),
    ],
)
def test_the_seeding_reach_guard_sees_a_claim_the_spec_directory_contradicts(text, copies,
                                                                            expected):
    """The last case is the direction that keeps this from being a frozen list: once the copies
    are gone the sentence is true and the guard says nothing, so a later milestone that unifies
    11-rate does not have to come back here to be allowed to say so."""
    problems = _seeding_reach_problems(text, copies)
    assert bool(problems) is expected, problems


@pytest.mark.parametrize("name", ["run.mjs", "playwright.config.js"])
def test_the_harness_takes_its_origin_from_the_stack_it_is_driving(name):
    """Neither may carry a literal origin, because a second checkout is a second stack.

    Both shipped `process.env.BASE_URL ?? 'http://localhost:8080'`, which is correct for one
    checkout and silently wrong for two: with a worktree per lane, the second suite reset its own
    database and then drove the FIRST one's application. It reported 8 skipped in phase one --
    `01-first-boot.spec.js` sees a stack long past first boot and skips, exactly as designed --
    and 13 phase-two failures against an app on another branch. Nothing in either number said
    "wrong stack". `reset.mjs` never had the bug: it had always read PUBLIC_URL from the `.env`
    beside it, which is why it dropped the right database while the suite drove the wrong app.
    [M4.12, the parallel-lane setup]
    """
    source = _read(REPO / "e2e" / name)
    assert "localhost:8080" not in source, (
        f"e2e/{name} carries a literal origin; it must resolve one through e2e/env.mjs's "
        "baseUrl(), which reads the stack's own PUBLIC_URL"
    )
    assert "baseUrl(" in source, f"e2e/{name} no longer resolves its origin through env.mjs"


def test_the_origin_guard_sees_a_literal_put_back():
    """The synthetic violation, because a guard with no failing case is a comment."""
    regressed = "const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080';"
    assert "localhost:8080" in regressed and "baseUrl(" not in regressed


def test_the_harness_reaches_the_fake_jellyfin_on_the_port_its_own_stack_published():
    """The third address of the same class, and the one the browser gate found rather than this
    file.

    `ops/compose.e2e.yml` publishes the fake on `${JELLYFIN_FAKE_PORT:-8096}` so a lane per
    worktree can hold a stack each; inside the compose network it stays `jellyfin-fake:8096` for
    both, which is why only the published half may be parameterised. `e2e/helpers.js` kept
    `http://127.0.0.1:8096`, so the suite set Played on the OTHER lane's fake and the app swept
    its own: §7.3's adopt direction had nothing to adopt, `seen.sync_all` returned healthy with
    every counter zero -- which was the truth -- and "a flag set in jellyfin arrives in the app"
    failed on a seen-state nobody had set. Measured on the M4.12 gate: the fake on 8096 held
    `jf-1` played with no tokens and no writes, the fake on 8097 held the member's token and no
    Played flag.
    """
    source = _read(HELPERS)
    assert "127.0.0.1:8096" not in source, (
        "e2e/helpers.js carries a literal control address; the fake's published port is "
        "JELLYFIN_FAKE_PORT and must be resolved through e2e/env.mjs's env()"
    )
    assert "JELLYFIN_FAKE_PORT" in source, (
        "e2e/helpers.js no longer reads the port ops/compose.e2e.yml publishes the fake on"
    )
    # The service name is the half that must NOT move: it is the compose network's, identical in
    # every lane, and a checkout that parameterised it would be testing a topology nobody ships.
    assert "jellyfin-fake:8096" in source


def test_the_fake_jellyfin_port_guard_sees_the_constant_that_shipped():
    """The synthetic violation, for `test_the_origin_guard_sees_a_literal_put_back`'s reason."""
    regressed = "  control: process.env.FAKE_JELLYFIN_CONTROL ?? 'http://127.0.0.1:8096',"
    assert "127.0.0.1:8096" in regressed and "JELLYFIN_FAKE_PORT" not in regressed
