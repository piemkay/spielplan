"""What `docker-compose.yml` and `ops/backend.Dockerfile` argue, beside what they declare.

`test_static_contracts.py` guards what those two files *set*: the one published port, the mounts
each service receives, the non-root `USER`, the pinned installer. These guards are the other
half, and the M4.7 review found the same break in both files at once — a line whose stated reason
and actual effect had come apart, with nothing in this suite able to see it. The backend's
`stop_grace_period` was argued from a bundle import's transaction, which `import_bundle` commits
inside the request and which is therefore never open at the moment the comment describes; and the
runtime user was handed the whole install tree by a `chown` sitting under a comment about host
bind mounts that are not in it. One existing guard asserts the grace-period key is present and
none reads its value or its argument, and nothing anywhere read the `chown` at all.

`CLAUDE.md` makes a comment that argues something false a defect rather than a cosmetic, and both
of these were load-bearing: the first tells a maintainer that thirty seconds covers work §5.3
budgets in minutes, and the second undoes half of `sec-08`'s hardening while reading as part of
it. [M4.7 cycle 2 findings 10 and 14]
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_static_contracts import COMPOSE, DOCKERFILE, _final_stage, _service

BUNDLE = Path(__file__).resolve().parents[1] / "spielplan" / "importer" / "bundle.py"


# --- the backend's stop grace period ----------------------------------------------------

# The one sentence the backend's grace period must carry, verbatim, the way the restore
# precondition is required verbatim in four documents (`test_operator_runbook.py`). A banned word
# cannot be the rule here, because the honest comment quotes the claim it is correcting; what a
# reader needs is the exclusion stated, and 30s is the value it is stated about. [M4.7 cycle 2 f10]
GRACE_EXCLUSION = "a stop during an import is not covered"


def _reason_above(block: str, key: str) -> str:
    """The comment lines immediately above `key:` — the argument this file makes for its value.

    Read as one string rather than line by line because the argument wraps: no sentence in this
    compose file fits in the column, and a per-line check would be a check on where the author
    happened to break them.
    """
    lines = block.splitlines()
    at = next((i for i, line in enumerate(lines) if line.strip().startswith(f"{key}:")), None)
    assert at is not None, f"no `{key}:` in this block"
    reason: list[str] = []
    for line in reversed(lines[:at]):
        if not line.strip().startswith("#"):
            break
        reason.append(line.strip().lstrip("#").strip())
    return " ".join(reversed(reason))


def _import_bundle_body() -> str:
    source = BUNDLE.read_text(encoding="utf-8")
    rest = source[source.index("async def import_bundle("):]
    end = re.search(r"^(?:async )?def ", rest[1:], re.M)
    return rest[: end.start() + 1] if end else rest


def test_the_backends_grace_period_says_what_it_covers_and_what_it_does_not():
    """§10's swap sequence ends in "restart backend + worker", and the number was argued from it:
    "a gesture the operator makes right after an import, while a bundle import's transaction may
    still be open". No transaction is open then. `import_bundle` opens and commits its own inside
    the request, so by the time the Data tab has answered and the operator types the restart there
    is nothing left for a grace period to hold — which is the first assertion, because it is the
    fact the comment's real argument rests on and a refactor that moved that transaction out to
    the caller would change what 30 s means.

    The second is the disclosure that argument owes. The one shape 30 s genuinely cannot cover is
    a stop *during* an import, and §5.3 (spec line 205) budgets that import at "minutes" — the
    old comment named the import and called it covered, which is worse than saying nothing,
    because a maintainer reading 30 s beside §10's restart would not know to look.
    [M4.7 cycle 2 finding 10]
    """
    assert "async with conn.transaction():" in _import_bundle_body(), (
        "import_bundle no longer commits inside the request, and the compose comment this "
        "guards is written on the fact that it does"
    )
    backend = _service(COMPOSE.read_text(encoding="utf-8"), "backend")
    reason = _reason_above(backend, "stop_grace_period")
    assert reason, "the backend's stop_grace_period declares a number and argues nothing"
    assert GRACE_EXCLUSION in reason, (
        f'the backend\'s stop_grace_period must say "{GRACE_EXCLUSION}" verbatim: section 5.3 '
        f"budgets the bundle import in minutes and this value is 30s"
    )


def test_the_grace_period_guard_sees_the_argument_it_was_written_for():
    """The synthetic violation is the historical one: the comment this finding replaced, which
    named the import and claimed it covered. A guard whose only case is the file it already
    passes on is the instrument M4.7 exists to repair (docs/TESTING.md)."""
    historical = (
        "\nservices:\n"
        "  backend:\n"
        "    # Docker's default is 10s to SIGKILL, and section 10's swap sequence ends in\n"
        "    # \"restart backend + worker\" - which is a gesture the operator makes right after\n"
        "    # an import, while a bundle import's transaction may still be open. 30s is enough\n"
        "    # for a request in flight and for the pool to close.\n"
        "    stop_grace_period: 30s\n"
    )
    reason = _reason_above(_service(historical, "backend"), "stop_grace_period")
    assert reason, "the self-test's own comment block was not read"
    assert GRACE_EXCLUSION not in reason


# --- the bundle import job's own budget ----------------------------------------------------

# The measurement the registry entry has to be argued from: the seconds
# `ops/m414_exit_criterion.py` recorded for the JOB, in all three runs, and the number
# `docs/TESTING.md` carries. Required verbatim for `GRACE_EXCLUSION`'s reason - what a reader
# needs is the fact stated, and a banned phrase would only forbid one spelling of arguing the
# budget against a number this milestone superseded. [M4.14 cycle 4, m414-c4-waveE-03]
IMPORT_JOB_MEASUREMENT = "213"

WORKER = Path(__file__).resolve().parents[1] / "spielplan" / "worker.py"

_GRACE_UNITS = {"s": 1, "m": 60, "h": 3600}


def _seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", value.strip())
    assert match, f"a stop_grace_period this guard cannot read: {value!r}"
    return float(match.group(1)) * _GRACE_UNITS[match.group(2)]


def _registry_reason(source: str, entry: str) -> str:
    """The comment block immediately above one `Job(...)` row in `worker.py`'s registry."""
    lines = source.splitlines()
    at = next((i for i, line in enumerate(lines) if line.strip().startswith(f"Job({entry}")), None)
    assert at is not None, f"no `Job({entry}` row in worker.py's registry"
    reason: list[str] = []
    for line in reversed(lines[:at]):
        if not line.strip().startswith("#"):
            break
        reason.append(line.strip().lstrip("#").strip())
    return " ".join(reversed(reason))


def test_the_bundle_imports_budget_is_argued_from_the_job_it_bounds():
    """The same defect as the grace period above, one file over: a stated reason and an actual
    effect that had come apart.

    The registry entry argued "300 s is 2.4x the 127 s measured on the real bundle". 127 s is
    M4.5's measurement of the work INSIDE THE REQUEST; the job this budget bounds is M4.14's,
    which `ops/m414_exit_criterion.py` measured at 213 s from the press in all three recorded
    runs. So the real margin was about 1.4x, not 2.4x, and a box a third slower than the reference
    one could not finish an import at all: `_tick` cancels at the budget, the staged tree is
    dropped, `_reap_abandoned_import` closes the row, and the retry reproduces it exactly.

    The first assertion is the PIN, and it is what makes this pair one edit: the budget is fixed
    to the worker's `stop_grace_period` rather than sized against a measurement, because a budget
    past the grace would promise time `docker compose stop` takes away. M4.14 could only honour
    the grace, because the grace was M4.7's and plan §8 put it outside that milestone; decision
    300 moves both to 600 s in one diff, for the box `.github/workflows/release.yml` imports the
    real bundle on. So both halves of M4.14 step E2 now hold - 2.8x the measured 213 s, and still
    equal to the grace - and this assertion is what stops a later edit taking one without the
    other. A maintainer reading the entry has to be able to see which of the two the number is.
    [M4.14 cycle 4, m414-c4-waveE-03; decision 300]
    """
    from spielplan import worker

    worker_block = _service(COMPOSE.read_text(encoding="utf-8"), "worker")
    grace = _seconds(worker_block.split("stop_grace_period:")[1].splitlines()[0])
    assert grace == worker.BUNDLE_IMPORT_TIMEOUT, (
        f"the registry argues this budget as the worker's stop_grace_period, which is {grace:g}s "
        f"while BUNDLE_IMPORT_TIMEOUT is {worker.BUNDLE_IMPORT_TIMEOUT:g}s"
    )

    reason = _registry_reason(WORKER.read_text(encoding="utf-8"), "BUNDLE_IMPORT_JOB")
    assert reason, "the bundle import job declares a timeout and argues nothing"
    assert "stop_grace_period" in reason, (
        "the budget is pinned to the stop grace rather than chosen for margin, and the entry has "
        "to say so: " + reason
    )
    assert IMPORT_JOB_MEASUREMENT in reason, (
        f'the entry must argue this budget against the {IMPORT_JOB_MEASUREMENT}s this milestone '
        f"measured for the JOB, not against a number measured for work that is no longer in it: "
        + reason
    )


def test_the_import_budget_guard_sees_the_argument_it_was_written_for():
    """The synthetic violation is the historical one, the way the grace-period guard's is: the
    sentence this finding replaced, which argued a 2.4x margin over a superseded measurement."""
    historical = (
        "    # 300 s is 2.4x the 127 s measured on the real bundle, and it is also this service's\n"
        "    # `stop_grace_period` in `docker-compose.yml` - a budget past the grace would promise\n"
        "    # time that `docker compose stop` takes away.\n"
        "    Job(BUNDLE_IMPORT_JOB, \"M0\", \"admin action\", \"minutes\", _bundle_import,\n"
    )
    reason = _registry_reason(historical, "BUNDLE_IMPORT_JOB")
    assert reason, "the self-test's own comment block was not read"
    assert "stop_grace_period" in reason, "the historical comment did make the pin claim"
    assert IMPORT_JOB_MEASUREMENT not in reason


# --- the box the 600 s is sized for, which nobody has ever run anything on -----------------

# Decision 300's number is right and the reason six records gave for it was not. They stated, as
# present fact, a measured property of a machine that has never existed: "this runner is slower
# than the reference box", "the box this release is now measured on", "now runs that same import
# on a runner slower than the reference one". `.github/workflows/release.yml:81` is
# `runs-on: [self-hosted, spielplan-corpus]` -- the household's OWN Windows workstation reached
# through a runner registered inside WSL or a Linux VM (`docs/TESTING.md`, "Running it") -- and
# `docs/RELEASE.md` section 2.1 records that no runner carrying that label has ever been
# registered. So nobody has timed anything on it, and the comparison could not have been made.
#
# Worse under either reading of the other half: "the reference box" is a DEFINED term here for
# §2's 4 vCPU GPU-less VM (`scoring/tower.py`, `importer/validate.py`), while M4.14's 213 s was
# measured on the dev NVMe workstation -- so the sentence either names the wrong machine for the
# measurement or compares against one nobody ran the release on.
#
# The BUDGET is untouched: 600 s stands on the 213 s measurement plus the unmeasured cost of
# containerised I/O under a hypervisor, and on the pin above. What this refuses is the comparative
# claim, which is decision 184's rule ("a figure published as measured must be derived") applied to
# a comparison rather than to a number. It stops applying the day a runner is registered and timed,
# which is the day `docs/RELEASE.md` section 2.1 stops saying no such runner exists -- and this
# guard comes out with that sentence. [decisions 300, 316; M4.16 cycle 4, REL-C4-01]
#
# SIX SENTENCES IN FIVE FILES, since review cycle 4, and the sixth is why the list is a list of
# files rather than a copy of decision 316's census. `ops/m414_exit_criterion.py` argued its own
# `IMPORT_DEADLINE_S` from decision 300's retired reason and was invisible here twice over: the
# file was not read, and the claim was spelled the other way round. The two spellings are one
# claim -- "this runner is slower than X" states it as a predicate, "the release workflow's
# slower box" states it as a fact already agreed -- and a rule that knew only the first is the
# shape decision 304's Cost paragraph names, a guard over four of five files one cycle later.
# `worker.py`'s own "once claimed the release runner WAS slower than a machine ..." stays
# admitted: a sentence recording the retired claim as history is what the repair reads like.
# [decisions 300, 316; M4.16 cycle 4]
_RUNNER_SPEED_RECORDS = (
    ".github/workflows/release.yml",
    "backend/spielplan/worker.py",
    "docker-compose.yml",
    "backend/tests/spec_coverage.toml",
    "ops/m414_exit_criterion.py",
)
_RUNNER_SPEED_CLAIM = re.compile(
    r"(?:runner|box) (?:is |that is )?slower than|slower than (?:the reference|this box)"
    r"|slower (?:box|runner)\b",
    re.I,
)
_NO_RUNNER = "No such runner exists"


def test_no_record_compares_the_release_runner_against_a_box_nobody_timed_it_on():
    """The positive half first, because it is what makes the negative one temporary rather than a
    ban: while `docs/RELEASE.md` records that no `spielplan-corpus` runner has ever been
    registered, no record may publish that runner's speed relative to anything."""
    repo = Path(__file__).resolve().parents[2]

    def flat(path: Path) -> str:
        """Whitespace and comment furniture off, so a claim that wrapped mid-sentence in a prose
        paragraph or a `#` block still reads as one sentence. Every one of the five sentences this
        rule is about wrapped somewhere, and three wrapped between the two words that matter."""
        return " ".join(path.read_text(encoding="utf-8").replace("#", " ").split())

    assert _NO_RUNNER in flat(repo / "docs" / "RELEASE.md"), (
        "docs/RELEASE.md no longer records that no `spielplan-corpus` runner exists. If one has "
        "been registered and TIMED, the comparison below becomes a measurement and this guard "
        "comes out in the same change as that sentence; if it was merely deleted, put it back"
    )
    guilty = []
    for name in _RUNNER_SPEED_RECORDS:
        found = _RUNNER_SPEED_CLAIM.search(flat(repo / name))
        if found:
            guilty.append(f"{name}: {found.group(0)!r}")
    assert not guilty, (
        "a record states the release runner's speed as a measured fact:\n  "
        + "\n  ".join(guilty)
        + "\n\nNobody has run anything on that runner -- it has never been registered. 600 s is "
        "sized off the 213 s measurement plus an unmeasured virtualisation cost, and off the pin "
        "to the stop grace, not off a comparison (decision 316)."
    )


@pytest.mark.parametrize(
    ("name", "text", "caught"),
    [
        ("the sentence release.yml carried",
         "M4.14 measured that job at 213 s on the reference box, and this runner is slower than "
         "the reference box.", True),
        ("the sentence docker-compose.yml carried",
         "release.yml now runs that same import on a runner slower than the reference one", True),
        ("the honest replacement",
         "that runner has never been registered, so its speed under containerised I/O has never "
         "been measured", False),
        ("a measurement of a box somebody did run on",
         "213 s on the dev NVMe workstation with a warm page cache", False),
        # Cycle 4's sixth carrier, and the spelling that makes the claim without a verb: an
        # adjective in front of the noun asserts it as settled rather than arguing it, which is
        # why it read past a rule anchored on "slower than".
        ("the sentence ops/m414_exit_criterion.py carried",
         "moved it there from 300 s, with the worker's `stop_grace_period`, for the release "
         "workflow's slower box", True),
        ("the same shape about the runner",
         "600 s is what a slower runner needs", True),
        # And the direction that keeps the repair writable: the retired claim NAMED as retired.
        ("the retired claim recorded as history",
         "this comment once claimed the release runner was slower than a machine the measurement "
         "was not taken on", False),
    ],
)
def test_the_runner_speed_guard_reads_the_comparison_and_not_the_measurement(name, text, caught):
    """Both directions. Refusing the two shipped spellings is the repair; ADMITTING the other two
    is what keeps the rule usable, since the budget still has to be argued from a measurement and
    the repair still has to be able to say the runner is unmeasured."""
    assert bool(_RUNNER_SPEED_CLAIM.search(text)) is caught, name


# --- the image's own install tree ---------------------------------------------------------

# `chown`'s first argument is the owner and `chmod`'s is the mode; everything after is a target.
# Stopping at `&&`/`|`/`;` keeps one command's targets from being read as the next one's.
_ADJUSTS = re.compile(r"\b(chown|chmod)\b([^&|;\n]*)")

# `WORKDIR /app` makes `.` the install tree, and `useradd --home-dir /app` makes `$HOME` the same
# directory again. Three spellings of one path, because cycle 2's finding 6 is what a guard that
# matches only the spelling in front of it costs.
_INSTALL_TREE = ("/app", ".", "./", "$HOME", "${HOME}")


def _install_tree_handed_to_the_runtime_user(dockerfile: str) -> list[str]:
    """Every instruction in the shipping stage that makes what the image installed writable.

    Comment lines are dropped before the scan: the Dockerfile's own comment explains the `chown`
    it no longer runs, and a guard that reads the explanation as the instruction would demand the
    argument be deleted along with the line. Stage-aware for `_final_stage`'s reason — a `chown`
    in the node build stage is spent when that stage ends and reaches no shipped file.
    """
    lines = [
        line for line in _final_stage(dockerfile).splitlines()
        if not line.lstrip().startswith("#")
    ]
    stage = "\n".join(lines).replace("\\\n", " ")
    handed: list[str] = []
    for match in _ADJUSTS.finditer(stage):
        args = [arg for arg in match.group(2).split() if not arg.startswith("-")]
        if any(t in _INSTALL_TREE or t.startswith(("/app/", "./")) for t in args[1:]):
            handed.append(f"{match.group(1)} {' '.join(args)}")
    return handed


def test_the_image_does_not_hand_its_own_install_tree_to_the_runtime_user():
    """The other half of the `USER` directive, and it used to cancel most of it.

    `chown -R spielplan:spielplan /app` sat under the comment arguing why uid 1000 is fixed —
    "because ./data is a set of host bind mounts" — and ./data is mounted at /data, never under
    /app, so the chown reached none of what it named. What it did reach was /app/spielplan (the
    code), /app/migrations (the DDL every boot applies) and /app/static (the bundle §6's
    unauthenticated fallback serves): an arbitrary write reached through that fallback, which is
    the handler §14.3 and `e2e/specs/07-boundaries.spec.js` are about, could persist across
    restarts. Root-owned 0755 is readable and traversable by uid 1000, which is all the runtime
    asks for — every write this codebase makes is under `settings.data_dir`, and
    `PYTHONDONTWRITEBYTECODE` removes the one Python would attempt on its own.
    [M4.7 sec-08, cycle 2 finding 14]
    """
    handed = _install_tree_handed_to_the_runtime_user(DOCKERFILE.read_text(encoding="utf-8"))
    assert not handed, f"the shipping stage makes the install tree writable: {handed}"


@pytest.mark.parametrize(
    ("name", "dockerfile"),
    [
        (
            "the line this finding removed",
            "FROM python:3.12-slim\nRUN groupadd spielplan \\\n && useradd spielplan \\\n"
            " && chown -R spielplan:spielplan /app\nUSER spielplan\n",
        ),
        ("the same line against the workdir", "FROM python:3.12-slim\nWORKDIR /app\n"
         "RUN chown -R 1000:1000 .\n"),
        ("spelled as the home directory", "FROM python:3.12-slim\nRUN chown -R spielplan $HOME\n"),
        ("one directory rather than the tree", "FROM python:3.12-slim\n"
         "RUN chown -R spielplan:spielplan /app/static\n"),
        ("permissions rather than ownership", "FROM python:3.12-slim\nRUN chmod -R a+w /app\n"),
    ],
)
def test_the_install_tree_guard_catches_a_real_violation(name, dockerfile):
    assert _install_tree_handed_to_the_runtime_user(dockerfile), f"went unnoticed: {name}"


def test_the_install_tree_guard_ignores_a_stage_that_does_not_ship():
    """The node stage builds the PWA as root and ends; nothing it owns reaches the image except
    the files `COPY --from=frontend` brings over, which arrive owned by root either way."""
    two_stage = (
        "FROM node:22-slim AS frontend\nWORKDIR /app\nRUN chown -R node:node /app\n"
        "FROM python:3.12-slim AS runtime\nWORKDIR /app\nUSER spielplan\n"
    )
    assert not _install_tree_handed_to_the_runtime_user(two_stage)
