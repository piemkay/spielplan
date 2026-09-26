"""Guards over the instrument: the e2e runner, the first-boot spec and CI's workflows, read as TEXT so they
run wherever the suite runs. Each guard has a self-test fed a regressed source; the `.env` reader is run."""

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
# The `.env` reader lives in `env.mjs`, so every harness file reads the stack's own PUBLIC_URL.
ENV_MJS = REPO / "e2e" / "env.mjs"
SPECS = REPO / "e2e" / "specs"
FIRST_BOOT = SPECS / "01-first-boot.spec.js"
JELLYFIN = SPECS / "08-jellyfin.spec.js"


E2E_OVERLAY = REPO / "ops" / "compose.e2e.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _services_whose_code_is_a_bind_mount(compose: str) -> set[str]:
    """A mounted `.py` needs a restart for an edit to take; a data directory does not."""
    found: set[str] = set()
    service: str | None = None
    for line in compose.splitlines():
        if re.match(r"^ {2}[A-Za-z0-9_.-]+:\s*$", line):
            service = line.strip().rstrip(":")
        elif service and re.search(r"-\s+\./[^:\s]+\.py:", line):
            found.add(service)
    return found


def test_the_e2e_reset_restarts_every_service_whose_code_is_a_bind_mount():
    """uvicorn reads a mounted double once at start, so a stale container serves the previous double."""
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
    """A rule that fires on every mount, data directories included, is a rule nobody reads."""
    assert _services_whose_code_is_a_bind_mount(compose) == expected


def _span(source: str, index: int, opener: str, closer: str) -> tuple[int, int]:
    """(start, end) of the bracket-matched span opening at
    the first `opener` at or after `index`; end exclusive."""
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


# The deadline may be `AbortSignal.timeout(` or an explicit `signal:`; neither is forbidden.
_DEADLINE = re.compile(r"AbortSignal\.timeout\(|\bsignal\s*:")
_FETCH = re.compile(r"\bfetch\s*\(")
_EXIT = re.compile(r"process\.exit\(\s*([^)]*)\)")
# Anchored on the log STATEMENT: the words also appear in comments above the branch.
_PHASE_TWO_LOG = re.compile(r"console\.log\([^)]*phase 2")
# What the loop concluded, recorded: `loaded = true` inside the branch that read the body.
_FLAG_SET = re.compile(r"\b(?P<name>[A-Za-z_$][\w$]*)\s*=\s*true\b")


def _fetches_without_a_deadline(source: str) -> list[str]:
    """`fetch` has no timeout of its own, so a silent backend would hold an iteration for ever."""
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
        # A brace-less branch first: `if (!loaded) process.exit(1);` has no block.
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
    """A phase 2 entered with no bundle is all skips and exits
    0, so the flag chain is walked, not just an `if`."""
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
    """§10's swap ends in a restart; the run has failed if nothing came back with a bundle."""
    assert _failure_branch_problems(_read(RUNNER)) == []


def test_every_health_fetch_in_the_runner_carries_a_deadline():
    """`run.mjs` calls `reset.mjs`, so an unbounded fetch in either spends the other's budget."""
    assert _fetches_without_a_deadline(_read(RUNNER)) == []
    assert _fetches_without_a_deadline(_read(RESET)) == []


def _tag_problems(runner: str, spec: str) -> list[str]:
    """Phase 2 inverts the tag phase 1's file carries, so tag and path must name the same file."""
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
    """A tag and a path naming different files would leave a spec in neither phase."""
    runner = _read(RUNNER)
    assert _tag_problems(runner, _read(FIRST_BOOT)) == []
    tag = _inverted_tag(runner)
    carriers = {p.name for p in sorted(SPECS.glob("*.spec.js")) if tag in _read(p)}
    assert carriers == {"01-first-boot.spec.js"}, (
        f"{tag} also appears in {sorted(carriers - {'01-first-boot.spec.js'})}: phase 2 inverts "
        "those files out too, so they run in neither phase"
    )


# It polls, breaks, and announces phase 2 whatever the answer was.
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
        # Three tidy-ups that keep the branch but take away what it branches on.
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
    """The unguarded loop reads as complete, so the guard must be shown catching it."""
    problems = _failure_branch_problems(source) + _fetches_without_a_deadline(source)
    assert problems, "the guard passed a runner that enters phase 2 with no bundle"
    assert needle in " ".join(problems), problems
    assert not _failure_branch_problems(_GUARDED_RUNNER) + _fetches_without_a_deadline(
        _GUARDED_RUNNER
    ), "and it passes the runner as repaired"


# A runner and a spec that agree, for the guard to be shown disagreeing about.
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
    """The tree passes, so only a synthetic disagreeing pair shows the guard can speak."""
    problems = _tag_problems(runner, spec)
    assert problems, "the guard passed a runner and a spec whose tags disagree"
    assert needle in " ".join(problems), problems
    assert _tag_problems(_TAG_RUNNER, _TAG_SPEC) == [], "and it passes the pair that agrees"


_CONFIGURE = re.compile(r"test\.describe\.configure\(\s*\{(?P<body>[^}]*)\}\s*\)")

# Playwright resolves `configure` from the innermost suite outwards, so a nested one governs the group.
# Any value but the literal `0` is reported: `process.env.CI ? 1 : 0` turns a failed first boot `flaky`.
_RETRIES = re.compile(r"\bretries\s*:\s*(?P<value>[^,}]+)")
_MODE = re.compile(r"\bmode\s*:\s*['\"](?P<mode>\w+)['\"]")


def _retry_problems(source: str) -> list[str]:
    """A retried first boot meets an app past first boot, skips, is scored `flaky`, and exits 0."""
    problems = []
    # Commented-out lines are dropped first: a commented-out configure is not there.
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
    """Only whole-line `//` comments are dropped; a `//` in a URL is left alone."""
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
    """Decision 185: per-file `retries: 0`, since `failOnFlakyTests` needs a newer Playwright."""
    assert _retry_problems(_read(FIRST_BOOT)) == []


def test_the_first_boot_beforeall_skip_survives():
    """The skip keeps an ad-hoc run against a used database skipping rather than failing."""
    body = _beforeall_skip(_read(FIRST_BOOT))
    assert body, "01-first-boot.spec.js's beforeAll no longer skips on a database that is not fresh"
    assert "!state.required" in body, (
        "the beforeAll skip no longer reads /api/setup/state's `required`: an anonymous caller "
        "gets that bit and the note, and nothing else (sec-14)"
    )


def test_the_jellyfin_spec_is_not_given_a_has_bundle_guard():
    """08-jellyfin must FAIL with no bundle: it is the only proof of the two-way sync."""
    guarded = {p.name for p in sorted(SPECS.glob("*.spec.js")) if _has_a_has_bundle_guard(_read(p))}
    assert not _has_a_has_bundle_guard(_read(JELLYFIN)), (
        "08-jellyfin.spec.js now skips itself when no bundle is loaded, so a stack that imported "
        "nothing has no spec left that fails"
    )
    # A floor, not the exact set: retiring a spec must not turn this red.
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
        # A second configure inside the group, which Playwright resolves first.
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
        # The nested configure spelled as `playwright.config.js` spells CI's retry.
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
        # Commented out by a bisector and never restored.
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
    """Each regression is a plausible tidy-up, so the guards must be shown catching them."""
    problems = reader(source)
    assert problems, "the guard passed a first-boot file that can retry into a pass"
    assert needle in " ".join(problems)
    assert reader(_SERIAL_SPEC) == [], "and it passes the file as it stands"


# Indentation-based text reading, comments stripped: a commented-out trigger is not a trigger.


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


# Jobs whose runner labels are not GitHub image names run on a household box.
_HOSTED_IMAGE = re.compile(r"\b(?:ubuntu|windows|macos)-[\w.]+\b", re.IGNORECASE)

# A job-level `if:` pinning a job to one ref narrows the trigger like `branches:`.
_ONLY_THE_DEFAULT_BRANCH = re.compile(
    r"github\.ref(?:_name)?\s*==\s*'[^']+'|'[^']+'\s*==\s*github\.ref(?:_name)?"
)


def _push_trigger_problems(text: str) -> list[str]:
    """Every branch push runs the hosted jobs, and nothing pushes onto a self-hosted machine."""
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
        # `branches-ignore` and `paths` narrow a trigger too.
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
        # Four spaces exactly: a job-level `if:`, not a step's at eight.
        gate = re.search(r"^ {4}if:\s*(?P<expr>.+)$", block, re.M)
        expr = gate.group("expr") if gate else ""
        # Read for every job, before the runner is looked at.
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


# The value must SAY "not the default branch"; merely mentioning `github.ref` is not enough.
_NOT_THE_DEFAULT_BRANCH = re.compile(
    r"github\.ref(?:_name)?\s*!=\s*'[^']+'|'[^']+'\s*!=\s*github\.ref(?:_name)?"
)


def _cancellation_problems(text: str) -> list[str]:
    """A cancelled default-branch run leaves the merge commit with no result."""
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


# A column-0 `concurrency:` is held by the whole run, and an unclaimed self-hosted job queues for 24 h.


def _self_hosted_group_problems(workflows: dict[str, str]) -> list[str]:
    """Read over the whole directory: the pairing of a queueing job and main's push group is what costs."""
    problems = []
    for name, text in sorted(workflows.items()):
        # Column 0: a job-level `concurrency:` holds only that job's runs.
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


# `pip3` and any spacing: pip ignores the `[tool.uv.*]` CPU torch index and resolves CUDA.
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
_BARE_PIP_INSTALL = re.compile(r"(?<!uv )\bpip3?\s+install\b")


def _interpreter_problems(text: str) -> list[str]:
    """§1's CPU torch comes from the pyproject's index, which only a venv install through uv honours."""
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
    """A cancelled run is no evidence, on the one commit the ledger quotes."""
    assert _cancellation_problems(_read(CI)) == []


def test_no_ci_job_pip_installs_into_the_runner_interpreter():
    """Every job installs the way the lint, backend and integration jobs do."""
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
        # A job-level `if:` on the ref, which used to be skipped.
        pytest.param(
            "  backend:\n    runs-on: ubuntu-latest",
            "  backend:\n    if: github.event_name == 'pull_request' || "
            "github.ref == 'refs/heads/main'\n    runs-on: ubuntu-latest",
            _push_trigger_problems, "gated to one branch by its own `if:`",
            id="a hosted job is pinned to the default branch by its own if",
        ),
        # `runs-on` of the one custom label reaches the household box without naming `self-hosted`.
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
        # `pip3`, the numbered spelling an author reaches for without the venv.
        pytest.param(
            "      - run: uv pip install -e \"backend[dev]\"\n\n  real-bundle:",
            "      - run: pip3 install -e \"backend[dev]\"\n\n  real-bundle:",
            _interpreter_problems, "outside a uv venv",
            id="the install is spelled pip3 rather than python -m pip",
        ),
        # The only shape that reaches the PEP-668 arm.
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
    """The workflow guards can never observe a GitHub run, so each must be shown failing."""
    assert reader(_CLEAN_WORKFLOW) == [], "the guard does not pass the workflow it describes"
    assert old in _CLEAN_WORKFLOW, f"the fixture no longer contains {old!r}"
    problems = reader(_CLEAN_WORKFLOW.replace(old, new))
    assert problems, "the guard passed a regressed workflow"
    assert needle in " ".join(problems), problems


def _workflow_files() -> dict[str, str]:
    """Every workflow file, so a third workflow is not missed."""
    return {
        path.name: _read(path)
        for pattern in ("*.yml", "*.yaml")
        for path in sorted(WORKFLOWS.glob(pattern))
    }


def test_no_self_hosted_job_queues_inside_the_group_a_push_to_main_waits_in():
    """A job nobody picks up waits a day; the group it waits in must not be main's."""
    assert _self_hosted_group_problems(_workflow_files()) == []


# The corpus job alone: two triggers, no group, no push path.
_CORPUS_ALONE = """\
on:
  workflow_dispatch:
  schedule:
    - cron: '17 5 * * 1'

jobs:
  real-bundle:"""


def test_the_group_guard_sees_the_arrangement_that_shipped():
    """The shape `ci.yml` had before the split, which the other guards all passed."""
    problems = _self_hosted_group_problems({"ci.yml": _CLEAN_WORKFLOW})
    assert problems, "the guard passed one workflow carrying both the push path and the corpus job"
    assert "pending behind it" in " ".join(problems), problems

    # Both exits: a separate file, or `ci.yml` without the corpus job.
    hosted_only, _, corpus = _CLEAN_WORKFLOW.partition("  real-bundle:")
    assert corpus, "the fixture no longer carries the corpus job"
    own_file = _CORPUS_ALONE + corpus
    assert _self_hosted_group_problems({"ci.yml": hosted_only, "real-bundle.yml": own_file}) == []
    # A group is fine where no push creates runs; only the pairing costs.
    grouped = "concurrency:\n  group: real-bundle\n\n" + own_file
    assert _self_hosted_group_problems({"real-bundle.yml": grouped}) == []
    # A job-level group holds only that job's runs.
    per_job = _CLEAN_WORKFLOW.replace(
        "concurrency:\n  group: ci-${{ github.ref }}\n"
        "  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}\n",
        "",
    ).replace("  backend:\n", "  backend:\n    concurrency:\n      group: backend\n", 1)
    assert "\nconcurrency:" not in per_job and "      group: backend" in per_job, per_job
    assert _self_hosted_group_problems({"ci.yml": per_job}) == []


# These read the workflow's sentences, which a reader deciding on the runner acts on.


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
    """GitHub cancels a job queued 24 h, so the workflow must say so; the corpus job runs alone."""
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

    # `ci.yml`'s tick is free only while its run is hosted jobs that finish.
    hosted = _jobs(_read(CI))
    assert _nested(_nested(_read(CI), "on"), "schedule"), "ci.yml lost the weekly tick in the split"
    unconditional = [name for name, block in hosted.items() if not re.search(r"^ {4}if:", block, re.M)]
    assert unconditional, "every job is gated: the tick no longer produces a run of hosted jobs"

    claims = [
        problem
        for path in (CI, CORPUS)
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


# The `.env` parser is RUN, lifted out of the shipped file; the spec is read for what no runtime reaches.

_ENV_DRIVER = """\
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
const ROOT = process.argv[2];
__PARSER__
console.log(JSON.stringify(env(process.argv[3]) ?? null));
"""

# The forms compose accepts, the ` #` comment rule, and a key assigned twice resolving to its LAST value.
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
    # One file per case, so the appended-duplicate shape can be expressed.
    pytest.param("PUBLIC_URL=http://localhost:8080\n"
                 "# the household's real stack, appended when it went live:\n"
                 "PUBLIC_URL=https://spielplan.example",
                 "PUBLIC_URL", "https://spielplan.example",
                 id="a key assigned twice: the last assignment is the one compose uses"),
]

# The shipped reader differed in an order of operations, which a text guard could not see.
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
    """Lifted by bracket matching and run with a tiny driver; importing the module would drop a database."""
    start = source.find("function env(")
    assert start >= 0, "e2e/env.mjs no longer defines env(): this guard is reading nothing"
    _, end = _span(source, start, "{", "}")
    assert end > 0, "e2e/env.mjs's env() has unbalanced braces"
    return source[start:end]


def _parse_with(parser: str, tmp_path: Path, line: str, key: str):
    node = shutil.which("node")
    if node is None:
        # The one honest skip: `node` runs the file under test.
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
    """The reset guard's prefix test runs on this value, so it must agree with compose."""
    assert _parse_with(_env_parser(_read(ENV_MJS)), tmp_path, line, key) == value


def test_the_reset_parser_harness_sees_the_order_that_shipped(tmp_path):
    """Fed the shipped reader, the harness reproduces the refusal end to end."""
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


# A wait later handed to `expect(...).rejects`, on any receiver (`page`, `a`, `admin`): its rejection is
# the normal outcome, so its handler must be attached before any `await`.
_WAIT_BINDING = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>\w+)\s*=\s*[\w.$]+\.waitFor(?:Request|Response|Event)\s*\("
)


def _unattached_rejection_problems(source: str) -> list[str]:
    problems = []
    for match in _WAIT_BINDING.finditer(source):
        name = match.group("name")
        rest = source[match.end():]
        # Bounded by `;`, so a later `.rejects` in another statement is not paired with this name.
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
    """A negative proved by a timeout must be watched from
    before the action, or a slow click rejects into nobody."""
    for path in sorted(SPECS.glob("*.spec.js")):
        assert _unattached_rejection_problems(_read(path)) == [], path.name


# The statement as it shipped, and as it now stands.
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

# A wait expected to resolve, which this guard must never report.
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
        # The same statement on a receiver not named `page`.
        pytest.param(
            _ATTACHED_AFTER_THE_ACTION.replace("page.waitForRequest", "b.waitForRequest"),
            True,
            id="the same statement in a file whose page object is not called page",
        ),
    ],
)
def test_the_unattached_rejection_guard_sees_a_wait_that_can_out_run_its_handler(source, expected):
    """Both directions: reporting every deferred wait would be uninhabitable."""
    problems = _unattached_rejection_problems(source)
    assert bool(problems) is expected, problems
    if expected:
        assert "attached after an `await`" in " ".join(problems)


# These guards hold the harness's own prose to what the code does.

# The reader between cycles: quoting handled, `return` still inside the loop.
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

# `reset.mjs`'s development-stack test, copied because a stale value decides a `DROP DATABASE`.
_DEV_STACK = re.compile(r"^https?://(localhost|127\.0\.0\.1)")


def test_the_reset_parser_resolves_a_key_the_way_the_stack_that_booted_did(tmp_path):
    """compose, `python-dotenv` and `sh` all take the LAST
    assignment; the first match could drop production."""
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


# Durations a message claims, reduced to seconds; `1000ms` and `60 attempts` are not claims.
_DURATION_CLAIM = re.compile(r"(?P<n>\d+)\s*(?P<unit>seconds?|minutes?|min|[sm])\b")
_LOOP_HEAD = re.compile(r"for\s*\(\s*(?:let|var)\s+(?P<var>\w+)\s*=\s*0\s*;\s*(?P=var)\s*<\s*"
                        r"(?P<count>\d+)\s*;")
_TIMEOUT_MS = re.compile(r"AbortSignal\.timeout\(\s*(\d+)\s*\)")
_SLEEP_MS = re.compile(r"setTimeout\(\s*[^,]*,\s*(\d+)\s*\)")


def _health_loop_bound(source: str) -> tuple[int, int, int] | None:
    """An attempt costs its request deadline plus the sleep,
    so the bound is the product; found by its `fetch`."""
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
    """With a 5 s deadline, sixty attempts are six minutes, not sixty seconds."""
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
    """A message misstating its budget sends the reader after a slow boot instead of a stall."""
    assert _wait_message_problems(_read(RUNNER)) == []
    assert _wait_message_problems(_read(RESET)) == []


# The loop, with the shipped message, an attempts message, and a correct minutes one.
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
    """The guard reads the loop, so raising the attempt count cannot make the message dishonest again."""
    problems = _wait_message_problems(_LOOP % (count, message))
    assert bool(problems) is expected, problems
    if expected:
        assert "claims" in " ".join(problems), problems


# Claims in the files maintainers read as the harness contract, weighed against the tree.
CONFIG = REPO / "e2e" / "playwright.config.js"
HELPERS = REPO / "e2e" / "helpers.js"
WORKER = REPO / "backend" / "spielplan" / "worker.py"
SPEC_DOC = REPO / "docs" / "spielplan-spec_v2.1.md"

# The leading block comment is what the config teaches.
_HEADER = re.compile(r"/\*\*(?P<body>.*?)\*/", re.S)
# A universal over the spec directory, within one sentence.
_UNIVERSAL_OVER_SPECS = re.compile(r"\b(?:every|all)\b[^.]{0,40}\bspecs?\b")


def _unguarded_specs() -> set[str]:
    """The spec files carrying no `config.has_bundle` skip at all."""
    return {
        path.name for path in sorted(SPECS.glob("*.spec.js"))
        if not _has_a_has_bundle_guard(_read(path))
    }


def _has_bundle_claim_problems(source: str, unguarded: set[str]) -> list[str]:
    """Half the specs carry no `has_bundle` guard, and
    08-jellyfin must never get one: the header must say so."""
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
    """This catches the sentence that recommends the forbidden edit."""
    assert _has_bundle_claim_problems(_read(CONFIG), _unguarded_specs()) == []


# The shipped sentence, the fix, the half-fix, and a header silent on `has_bundle`.
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
    """Naming the exception while keeping the universal is still the same recommendation."""
    problems = _has_bundle_claim_problems(_CONFIG_HEADER % tail, {"08-jellyfin.spec.js"})
    assert bool(problems) is expected, problems


# The spellings a comment reaches for.
_FOLD_IN = re.compile(r"\bfolds?[ -]in\b", re.I)
_FOLD_IN_SECTION = "\u00a75.3"
# Pre-existing shorthand in older specs may not grow; a retired spec was removed from the set.
_FOLD_IN_SHORTHAND_PREDATING_M48 = {
    "14-tonight.spec.js",
    "15-tonight-group.spec.js",
}


def _fold_in_cadence_in_the_spec() -> str:
    """The cadence §5.3's own jobs table gives the fold-in, read out of the spec."""
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
    """§5.3's fold-in is nightly; the 60 s tick is not the
    spec's, so a citation must carry the table's cadence."""
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
    """`waitForPool`'s message is printed, so its citation must be right; both premises are read."""
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


# `NN-name` with or without the suffix; a letter after the number excludes dates.
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
    """A deleted spec takes the prose that cites it with it."""
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
    """Naming the module is not the repair; the cadence is."""
    problems = _fold_in_miscitations(text, "nightly")
    assert bool(problems) is expected, problems


# A spec declaring either half of the seeding pair itself rather than importing it.
_SEEDING_PAIR = re.compile(r"\bfunction\s+(?:createMember|signInAsMember)\b")


def _specs_with_a_private_seeding_copy() -> set[str]:
    return {
        path.name for path in sorted(SPECS.glob("*.spec.js"))
        if _SEEDING_PAIR.search(_read(path))
    }


def _seeding_reach_problems(source: str, copies: set[str]) -> list[str]:
    """`11-rate.spec.js` keeps a deliberate private copy,
    so the helper must not claim to be the only path."""
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
    """The next person fixing a seeding bug reads this comment."""
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
    """Once the copies are gone the sentence is true and the guard is silent."""
    problems = _seeding_reach_problems(text, copies)
    assert bool(problems) is expected, problems


@pytest.mark.parametrize("name", ["run.mjs", "playwright.config.js"])
def test_the_harness_takes_its_origin_from_the_stack_it_is_driving(name):
    """A literal origin drives another checkout's stack; only PUBLIC_URL from the `.env` is right."""
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
    """The fake's published port is per lane; inside the
    compose network it is always `jellyfin-fake:8096`."""
    source = _read(HELPERS)
    assert "127.0.0.1:8096" not in source, (
        "e2e/helpers.js carries a literal control address; the fake's published port is "
        "JELLYFIN_FAKE_PORT and must be resolved through e2e/env.mjs's env()"
    )
    assert "JELLYFIN_FAKE_PORT" in source, (
        "e2e/helpers.js no longer reads the port ops/compose.e2e.yml publishes the fake on"
    )
    # The service name must NOT move: it is the compose network's, identical in every lane.
    assert "jellyfin-fake:8096" in source


def test_the_fake_jellyfin_port_guard_sees_the_constant_that_shipped():
    """The synthetic violation."""
    regressed = "  control: process.env.FAKE_JELLYFIN_CONTROL ?? 'http://127.0.0.1:8096',"
    assert "127.0.0.1:8096" in regressed and "JELLYFIN_FAKE_PORT" not in regressed


# `e2e/` has no linter; an extraction left unused file readers on `reset.mjs`'s import line.
_NAMED_IMPORT = re.compile(r"^import\s*\{([^}]*)\}\s*from\s*'[^']*';", re.MULTILINE)


def _unused_named_imports(source: str) -> list[str]:
    """Import statements are cut out before searching; a name used only in prose reads as used."""
    bindings: list[str] = []
    for match in _NAMED_IMPORT.finditer(source):
        for binding in match.group(1).split(","):
            # `{ a as b }` binds b.
            name = binding.strip().split(" as ")[-1].strip()
            if name:
                bindings.append(name)
    body = _NAMED_IMPORT.sub("", source)
    return [name for name in bindings if not re.search(rf"\b{re.escape(name)}\b", body)]


@pytest.mark.parametrize(
    "name", ["env.mjs", "reset.mjs", "run.mjs", "helpers.js", "playwright.config.js"]
)
def test_the_harness_imports_only_what_it_uses(name):
    """All five: moving code between these modules is where leftovers happen."""
    unused = _unused_named_imports(_read(REPO / "e2e" / name))
    assert unused == [], (
        f"e2e/{name} imports {', '.join(unused)} and uses none of them -- e2e/ is the one "
        "directory no linter reaches, so nothing else will say so"
    )


def test_the_unused_import_guard_sees_the_extraction_that_shipped():
    """Fed `reset.mjs`'s own shipped line; the trimmed import is silent."""
    regressed = (
        "import { execFileSync } from 'node:child_process';\n"
        "import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';\n"
        "import { env } from './env.mjs';\n"
        "const publicUrl = env('PUBLIC_URL');\n"
        "execFileSync('docker', ['compose', 'stop']);\n"
        "mkdirSync(artifacts, { recursive: true });\n"
        "for (const entry of readdirSync(artifacts)) rmSync(entry);\n"
    )
    assert _unused_named_imports(regressed) == ["existsSync", "readFileSync"]
    trimmed = regressed.replace("existsSync, ", "").replace("readFileSync, ", "")
    assert _unused_named_imports(trimmed) == []


# The phone project's `testMatch` regex is run against
# the directory; decision 267 keeps the config unedited.
PHONE_SHELL_SPEC = SPECS / "19-phone-shell.spec.js"

# The sentence review cycle 3 falsified, quoted so the guard below names what it is refusing.
UNQUALIFIED_ORDER = "Everything that must not meet that state has already run."

PHONE_PROJECT_SPECS = {
    "02-shell.spec.js",
    "03-library.spec.js",
    "06-responsive.spec.js",
    "13-rank.spec.js",
    "14-tonight.spec.js",
    "19-phone-shell.spec.js",
    # M5.6's Admin · Data surface belongs on the phone project.
    "20-admin-data.spec.js",
    # §6.6's 48 px Connectors and System check is taken on the phone project.
    "21-connectors.spec.js",
}


def test_the_phone_project_selects_the_specs_the_coverage_map_believes_it_runs():
    """The rows discharged on an iPhone 13 depend on the regex, so its matching set is enumerated."""
    source = _read(CONFIG)
    literal = re.search(r"testMatch:\s*/(.+?)/\s*,", source)
    assert literal, "the phone project declares no `testMatch`, so it runs every spec in the suite"
    selector = re.compile(literal.group(1))
    matched = {path.name for path in SPECS.glob("*.spec.js") if selector.search(path.as_posix())}
    assert matched == PHONE_PROJECT_SPECS, (
        f"the phone project's testMatch `{literal.group(1)}` selects {sorted(matched)}, and the "
        f"coverage map is written against {sorted(PHONE_PROJECT_SPECS)}.\n"
        "Missing: " + str(sorted(PHONE_PROJECT_SPECS - matched)) + "\n"
        "Extra:   " + str(sorted(matched - PHONE_PROJECT_SPECS)) + "\n"
        "A spec that drops off this project does not fail -- it quietly stops running where its "
        "rule is the rule, and `pytest` stays green with every row reporting covered."
    )


def test_the_phone_spec_names_the_specs_that_run_after_its_desktop_pass():
    """The desktop project runs every spec before the phone
    project, so the header must name what runs after."""
    source = _read(CONFIG)
    literal = re.search(r"testMatch:\s*/(.+?)/\s*,", source)
    assert literal, "the phone project declares no `testMatch`, so it runs every spec in the suite"
    selector = re.compile(literal.group(1))
    phone = {path.name for path in SPECS.glob("*.spec.js") if selector.search(path.as_posix())}
    followers = sorted(phone - {PHONE_SHELL_SPEC.name})
    assert followers, "the phone project runs this file alone, so there is nothing to name"

    header = _read(PHONE_SHELL_SPEC).split("*/", 1)[0]
    unnamed = [name for name in followers if name.removesuffix(".spec.js") not in header]
    assert not unnamed, (
        f"{PHONE_SHELL_SPEC.name}'s header does not name {unnamed}, which the phone project runs "
        "AFTER the desktop project has already run this whole file. Playwright groups by project "
        "before it orders by filename, so 'everything that must not meet that state has already "
        "run' is true within a project and false across the two this suite drives."
    )
    # And the claim itself must be gone, not only the list added.
    assert UNQUALIFIED_ORDER not in header, (
        f'{PHONE_SHELL_SPEC.name}\'s header still says "{UNQUALIFIED_ORDER}". That is true '
        "WITHIN a project and false across the two this suite drives: the phone project runs "
        f"{followers} after the desktop project has run this file end to end."
    )
