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
