"""Guards over what the operator follows rather than what the app runs.

`test_static_contracts.py` reads the shipped stack — the compose file, the image, the dependency
spec. This file reads the two things beside it that no runtime can check and that a review found
wrong in the same week M4.7 shipped: the runbook, which is four documents that must say the same
thing, and `e2e/`'s reset harness, which undid the one host-side step the image's `USER spielplan`
made mandatory.

Both are static by necessity. A restore precondition that is missing from README is missing at the
one moment nobody is reading tests, and a reset that removes a bind mount fails in the e2e job on
Linux only — which is CI, not this suite, and not any developer's box. So each rule is asserted
against the file the operator actually gets, and each guard is handed a synthetic violation before
it is trusted with the real one (docs/TESTING.md: "a guard that cannot fail reads as coverage while
providing none").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# --- §2 Backups: the restore precondition, in the four places a restore is described ------------

# One sentence, quoted verbatim by every document that tells an operator to run `pg_restore`.
# Verbatim rather than paraphrased on purpose: four paraphrases drift, and the half that drifts is
# always the one the operator happens to be holding. [M4.7 ops-01]
RESTORE_RULE = "a dump restores only into the image that wrote it"

# README carries the procedure; docker-compose.yml carries it beside the `/backups` mount that
# makes it possible; .env.example carries it beside the SECRETS_KEY the same restore needs; and
# docs/TESTING.md's release checklist is where it is read while a drill is being run.
RESTORE_DOCUMENTS = (
    "README.md",
    "docker-compose.yml",
    ".env.example",
    "docs/TESTING.md",
)


def _documents_missing_the_restore_rule(texts: dict[str, str]) -> list[str]:
    return sorted(name for name, text in texts.items() if RESTORE_RULE not in text)


def _runbook() -> dict[str, str]:
    return {name: (REPO / name).read_text(encoding="utf-8") for name in RESTORE_DOCUMENTS}


def test_every_document_that_describes_a_restore_states_what_it_restores_into():
    """§2 makes restore an operator action, and `pg_restore --clean --if-exists` is only half of it.

    `--clean` drops what the *archive* holds, so an object a later release added is not in the
    archive, is not dropped, and is still there when the migration runner reaches the file that
    creates it: a dump taken before `0017_ops.sql`, restored into a stack running it, leaves the
    backend crash-looping on `DuplicateTableError: relation "job_run" already exists` — a table
    name, with nothing in it about the dump. Reproduced in
    `test_upgrade_drill.py::test_a_dump_from_an_older_release_leaves_ddl_the_next_boot_cannot_apply`,
    which is what keeps this sentence a description of the real failure rather than a prediction.

    The rule shipped in none of the four documents. This is the guard that would have said so.
    [M4.7 ops-01]
    """
    missing = _documents_missing_the_restore_rule(_runbook())
    assert not missing, (
        f"these describe a restore without its precondition: {missing}. Every one of them must "
        f'carry the sentence "{RESTORE_RULE}" verbatim.'
    )


def test_the_restore_rule_guard_sees_a_document_that_dropped_the_sentence():
    """The state all four were in before this review: the procedure, without its precondition."""
    texts = _runbook()
    texts["README.md"] = texts["README.md"].replace(RESTORE_RULE, "")
    assert _documents_missing_the_restore_rule(texts) == ["README.md"]


# --- §14.3: the image runs as uid 1000, so the host bind mounts are load-bearing -----------------

RESET = REPO / "e2e" / "reset.mjs"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# `x-worker-volumes` in docker-compose.yml, as host paths. Every one of them is a directory the
# container writes as uid 1000 and the host owns.
BIND_MOUNTS = ("raw", "artifacts", "cache", "import", "backups")

# An `rm*` whose whole first argument is a mount source, which is deletion of the *directory*
# rather than of what is in it. The argument is resolved rather than matched: written as one
# regex over `rmSync(join(ROOT, 'data', '<mount>')`, the guard saw only the spelling its own
# self-test constructed, and the fix it was written to protect had by then introduced a
# `const artifacts = join(ROOT, 'data', 'artifacts')` two lines above the loop — so
# `rmSync(artifacts, …)`, the most natural way to reintroduce the bug, resolved to nothing. So
# did `join(ROOT, 'data/artifacts')`. What the rule is about is the path, and JavaScript has
# several ways to write one. [M4.7 sec-08]
_BINDING = re.compile(r"const\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?P<expr>join\([^()]*\))")
_REMOVES = re.compile(r"\brm(?:Sync|dirSync)?\(\s*(?P<arg>join\([^()]*\)|[A-Za-z_$][\w$]*)")
_LITERAL = re.compile(r"^['\"](?P<text>[^'\"]*)['\"]$")

# What a segment the guard cannot read as a literal is called in its report. Kept rather than
# skipped: a removal one level under `./data` is a mount root or nothing, so `join(ROOT, 'data',
# mount)` inside a loop over the five is a violation the guard must not pass just because the
# last segment is a variable.
UNREADABLE = "<unreadable>"


def _segments(expr: str, bindings: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """The path an expression names, relative to ROOT, with `UNREADABLE` for a non-literal."""
    expr = expr.strip()
    if expr in bindings:
        return bindings[expr]
    if expr == "ROOT":
        return ()
    literal = _LITERAL.match(expr)
    if literal:
        return tuple(part for part in literal.group("text").split("/") if part)
    if expr.startswith("join("):
        pieces = expr[expr.index("(") + 1:expr.rindex(")")].split(",")
        return tuple(part for piece in pieces for part in _segments(piece, bindings))
    return (UNREADABLE,)


def _mount_roots_removed(script: str) -> list[str]:
    """The bind mounts this script deletes outright rather than empties."""
    bindings: dict[str, tuple[str, ...]] = {}
    for binding in _BINDING.finditer(script):
        # In file order, so a const built out of an earlier one resolves through it.
        bindings[binding.group("name")] = _segments(binding.group("expr"), bindings)

    removed: set[str] = set()
    for call in _REMOVES.finditer(script):
        parts = _segments(call.group("arg"), bindings)
        if len(parts) != 2 or parts[0] != "data":
            continue  # `join(artifacts, entry)` is a version inside the mount, which is the rule
        if parts[1] in BIND_MOUNTS or parts[1] == UNREADABLE:
            removed.add(parts[1])
    return sorted(removed)


def test_the_e2e_reset_empties_the_bind_mount_it_clears_and_removes_none_of_them():
    """Docker recreates a missing bind-mount source as root, and the app containers are uid 1000.

    `e2e/reset.mjs` clears the staged artifacts and then starts the app services again, so a reset
    that removed `data/artifacts` handed the directory back to Docker to recreate — owned by root,
    under a backend that can no longer create `/data/artifacts/<version>`. Phase one's bundle
    import is what fails, on every Linux run, with a permission error nothing connects to the
    reset; and CI's `chown -R 1000:1000` has by then already been spent, because it runs before the
    stack starts and the reset happens in the middle. Emptying the directory keeps the ownership.
    [M4.7 sec-08]
    """
    script = RESET.read_text(encoding="utf-8")
    removed = _mount_roots_removed(script)
    assert not removed, (
        f"e2e/reset.mjs removes the bind mount(s) {removed} rather than emptying them; Docker "
        "recreates the mount source as root and the uid-1000 containers can no longer write it "
        f"({UNREADABLE!r} is a removal directly under ./data whose target is not a literal, "
        "which is a mount root or nothing)"
    )
    # The other half of the same line: a reset that stopped clearing the directory would leave
    # 01-first-boot.spec.js testing a stack that already has a bundle, which is the fake pass
    # `e2e/run.mjs`'s two phases exist to prevent.
    assert "readdirSync" in script and "'artifacts'" in script, (
        "e2e/reset.mjs no longer clears the staged artifacts, so first boot is not first boot"
    )


@pytest.mark.parametrize(
    ("name", "line", "removed"),
    [
        ("the line M4.7's review replaced, put back verbatim",
         "rmSync(join(ROOT, 'data', 'artifacts'), { recursive: true, force: true });",
         ["artifacts"]),
        # The form the fix itself made available, and the one a tidy-up of the new loop reaches
        # for first: the const is already two lines above it.
        ("the local the fix introduced",
         "rmSync(artifacts, { recursive: true, force: true });",
         ["artifacts"]),
        ("a const of its own",
         "const dumps = join(ROOT, 'data', 'backups');\nrmSync(dumps, { force: true });",
         ["backups"]),
        ("one path segment instead of two",
         "rmSync(join(ROOT, 'data/artifacts'), { recursive: true });",
         ["artifacts"]),
        # Not resolvable to a name, and reported anyway: everything one level under ./data is a
        # mount source, so a loop that removes them all must not pass for being written in a loop.
        ("a loop over the mounts",
         "for (const m of ['artifacts', 'cache']) rmSync(join(ROOT, 'data', m));",
         [UNREADABLE]),
    ],
)
def test_the_reset_guard_sees_the_directory_being_removed(name, line, removed):
    """Five spellings of one line, four of which the regex form resolved to nothing.

    The guard shipped matching only `rmSync(join(ROOT, 'data', '<mount>')` written inline —
    which is precisely what this self-test used to append, so the instrument and its proof were
    the same sentence and neither could see the others. [M4.7 sec-08]
    """
    regressed = RESET.read_text(encoding="utf-8") + f"\n{line}\n"
    assert _mount_roots_removed(regressed) == removed, f"{name} went unnoticed"


def _chown_precedes_the_stack(workflow: str) -> bool:
    """Does the e2e job give the container's uid its mounts before Docker can create them as root?"""
    chown = workflow.find("chown -R 1000:1000")
    up = workflow.find("up -d --build")
    return chown != -1 and up != -1 and chown < up


def test_the_e2e_job_chowns_the_bind_mounts_before_the_stack_can_create_them():
    """The GitHub runner's user is not uid 1000, and `./data/*` are bind mounts (§14.3).

    Order is the whole content of the rule: Docker creates a missing mount source as root at the
    moment the container starts, and a `chown` afterwards would be repairing directories the
    importer and the nightly dump have already failed to write. README's "Running it" carries the
    same two lines for a household box. [M4.7 sec-08]
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert _chown_precedes_the_stack(workflow), (
        "the e2e job's chown must run before `docker compose up`, or Docker creates the bind "
        "mounts as root first"
    )
    chown = next(line for line in workflow.splitlines() if "chown -R 1000:1000" in line)
    missing = [name for name in BIND_MOUNTS if f"data/{name}" not in chown]
    assert not missing, f"the e2e job leaves these mounts owned by the runner: {missing}"


def test_the_chown_ordering_guard_sees_the_step_moved_after_the_stack():
    """The arrangement the reset used to force: chown first, and undone before it is needed."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    chown = next(line for line in workflow.splitlines() if "chown -R 1000:1000" in line)
    moved = workflow.replace(chown, "") + chown
    assert not _chown_precedes_the_stack(moved)


def _hands_the_mount_to_the_runtime_uid(script: str) -> bool:
    """Does the reset make `data/artifacts` writable by uid 1000 before the services come back?

    Keeping the directory (the guard above) decides only that CI's chown is not spent. It says
    nothing about the case CI never sees: a checkout that has never run CI has a `data/artifacts`
    owned by whoever cloned it, and nothing between the clone and the import ever changes that.
    """
    fix = max(script.find("chown -R 1000:1000 /data/artifacts"),
              script.find("chmod -R 777 /data/artifacts"))
    up = script.find("'up', '-d', 'backend', 'worker'")
    return fix != -1 and up != -1 and fix < up


def test_the_e2e_reset_hands_the_artifacts_mount_to_the_uid_that_writes_it():
    """Emptying the directory is necessary and not sufficient; it must also be writable.

    Measured on Docker Desktop against the M4.7 image: phase one's `POST /api/admin/bundle/import`
    answered 500 with `PermissionError: [Errno 13] Permission denied: '/data/artifacts/test-v1'`,
    and `01-first-boot.spec.js` reported only a missing "artifacts staged to" finding — a
    permission error read as a broken importer. The gesture has to come from a root container
    rather than the host: `chown` on the host needs privileges the script does not have, and under
    Docker Desktop's Windows file sharing it does not reach the container's view at all, because
    the mode seen inside the container is the one the VM holds. [M4.7 sec-08]
    """
    assert _hands_the_mount_to_the_runtime_uid(RESET.read_text(encoding="utf-8")), (
        "e2e/reset.mjs no longer gives ./data/artifacts to uid 1000 before starting the app "
        "services, so a checkout that has never run CI fails phase one's bundle import with a "
        "permission error that names neither this line nor the USER directive that needs it"
    )


@pytest.mark.parametrize(
    ("name", "script"),
    [
        ("the step deleted outright", "docker([...COMPOSE, 'up', '-d', 'backend', 'worker']);"),
        # The ordering failure is the one CI already made once, in the other direction: a fix that
        # runs after the stack is up has been spent on a directory Docker has already decided.
        ("the step moved after the stack",
         "docker([...COMPOSE, 'up', '-d', 'backend', 'worker']);\n"
         "docker([...COMPOSE, 'run', '--rm', '--user', '0', '--entrypoint', 'sh', 'backend',\n"
         "  '-c', 'chown -R 1000:1000 /data/artifacts']);"),
    ],
)
def test_the_mount_ownership_guard_sees_the_step_removed_or_reordered(name, script):
    assert not _hands_the_mount_to_the_runtime_uid(script), f"{name} went unnoticed"


# --- §2 Configuration: the upgrade an install that was running yesterday follows -----------------

README = REPO / "README.md"
ENV_EXAMPLE = REPO / ".env.example"

CHOWN = "chown -R 1000:1000"


def _readme_section(heading: str) -> str:
    """The body of one README section, up to the next heading of any level."""
    text = README.read_text(encoding="utf-8")
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    following = re.search(r"^#{2,3} ", text[start:], re.M)
    return text[start:start + following.start()] if following else text[start:]


def _upgrade_chown_problems(section: str) -> list[str]:
    """Does the Upgrade block hand the bind mounts to uid 1000 before the rebuild that needs it?"""
    chown = section.find(CHOWN)
    if chown == -1:
        return ["the Upgrade block never hands the bind mounts to uid 1000"]
    line = next(row for row in section.splitlines() if CHOWN in row)
    problems = [f"data/{name} is not in the chown" for name in BIND_MOUNTS
                if f"data/{name}" not in line]
    rebuild = section.find("up -d --build")
    if rebuild != -1 and chown > rebuild:
        problems.append("the chown runs after the rebuild that makes it necessary")
    return problems


def test_the_upgrade_block_hands_the_bind_mounts_to_the_uid_the_new_image_runs_as():
    """`USER spielplan` is new in M4.7, and the upgrade is where it lands on directories that exist.

    "Running it" carries the same two lines, and an install following it has never had a mount
    Docker created for a root container. An install being *upgraded* has five of them: both app
    processes ran as root until this milestone, so `./data/*` is root-owned and stays that way
    across `git pull && docker compose up -d --build`. Nothing in the HTTP surface reports it —
    the backend answers `/api/health` either way — while the worker cannot write `/data/backups`,
    the importer cannot stage under `/data/artifacts`, and `/data/cache/worker.heartbeat` is never
    created, which is the file the worker's healthcheck reads. [M4.7 sec-08]
    """
    problems = _upgrade_chown_problems(_readme_section("### Upgrade"))
    assert not problems, (
        f"README's Upgrade block does not survive the USER directive: {problems}. It is the only "
        "procedure an existing install follows, and it is the one that needs the chown most."
    )


@pytest.mark.parametrize(
    ("name", "regress", "expected"),
    [
        ("the step deleted outright",
         lambda section, line: section.replace(line, ""),
         "never hands the bind mounts"),
        # The arrangement that reads as fixed and is not: Docker has already started the
        # containers, and the worker has already failed its first tick.
        ("the step moved below the rebuild",
         lambda section, line: section.replace(line, "") + line,
         "after the rebuild"),
        ("one mount left out of the list",
         lambda section, line: section.replace(line, line.replace(" data/backups", "")),
         "data/backups is not in the chown"),
    ],
)
def test_the_upgrade_chown_guard_sees_each_way_the_step_can_be_lost(name, regress, expected):
    section = _readme_section("### Upgrade")
    line = next(row for row in section.splitlines() if CHOWN in row)
    problems = _upgrade_chown_problems(regress(section, line))
    assert any(expected in problem for problem in problems), f"{name} went unnoticed: {problems}"


# Two boot refusals M4.7 can produce on a box that was running yesterday, each named by the string
# the operator reads in `docker compose logs backend` — because on a crash-looping container that
# string is the only thing they have to search with — and each paired with what the section owes
# it. The SECRETS_KEY half is a pointer rather than a fourth copy of the recipe: `.env.example`
# owns the rotation, and this file's whole thesis is that four paraphrases drift.
UPGRADE_REFUSALS = {
    "the SECRETS_KEY floor (cs-44)": (
        "SECRETS_KEY must be at least 32 characters",
        ".env.example",
    ),
    "the DEK rows that raced before 0017 (sec-10)": (
        'could not create unique index "data_encryption_key_one_active"',
        "UPDATE data_encryption_key SET retired_at = now() WHERE key_id =",
    ),
}


def _refusals_the_upgrade_block_leaves_unanswered(section: str) -> list[str]:
    return sorted(
        f"{name}: {phrase!r}"
        for name, phrases in UPGRADE_REFUSALS.items()
        for phrase in phrases
        if phrase not in section
    )


def test_the_upgrade_block_names_the_two_refusals_this_milestone_can_produce():
    """Both stop the boot before the migration runner, so both look like the missing grep line.

    And the one custody command README otherwise names repairs neither: `spielplan-secrets` cannot
    construct `Settings` under the short key at all, and over two rows it *can* open it reports
    "Custody is intact" and exits 0 — correctly, since it destroys only what it cannot read
    (`test_upgrade_drill.py`'s `test_reset_declines_the_race_it_was_asked_to_repair_...`).
    So the Upgrade section has to carry the two repairs itself, or the box has none.
    [M4.7 cs-44, sec-10]
    """
    unanswered = _refusals_the_upgrade_block_leaves_unanswered(_readme_section("### Upgrade"))
    assert not unanswered, (
        f"README's Upgrade section does not answer: {unanswered}. This is where the operator of a "
        "crash-looping backend lands, and neither refusal is reachable from anywhere else."
    )


@pytest.mark.parametrize(
    "phrase", [phrase for phrases in UPGRADE_REFUSALS.values() for phrase in phrases]
)
def test_the_upgrade_refusal_guard_sees_each_half_go_missing(phrase):
    section = _readme_section("### Upgrade").replace(phrase, "")
    assert any(repr(phrase) in problem
               for problem in _refusals_the_upgrade_block_leaves_unanswered(section))


# The paragraph a stranded install has to find, anchored on the phrase that distinguishes it from
# the refusal it is the way out of ("under 32 characters", which the block states already).
STRANDED_ANCHOR = "SHORTER than 32 characters"


def _stranded_rotation(env_example: str) -> str:
    """`.env.example`'s recipe for the install the floor refuses to boot, or "" if there is none."""
    start = env_example.find(STRANDED_ANCHOR)
    if start == -1:
        return ""
    end = env_example.find("\nSECRETS_KEY=", start)
    return env_example[start:end if end != -1 else len(env_example)]


def _stranded_rotation_problems(env_example: str) -> list[str]:
    recipe = _stranded_rotation(env_example)
    if not recipe:
        return ["no way out of the SECRETS_KEY floor is written down at all"]
    edit = recipe.find("set SECRETS_KEY")
    command = recipe.find("rewrap --old-key")
    problems = []
    if edit == -1:
        problems.append("the recipe never says to put the new value in .env")
    if command == -1:
        problems.append("the recipe never reaches rewrap")
    if edit != -1 and command != -1 and edit > command:
        problems.append("the recipe runs rewrap before the new key is in .env, which cannot work")
    return problems


def test_the_secrets_key_floor_is_documented_with_the_order_that_gets_out_of_it():
    """The one install M4.7's floor strands, and the one order that rescues it.

    A pre-M4.7 `.env` carrying a hand-typed short `SECRETS_KEY` stops booting on upgrade, and the
    rotation block above this paragraph gives the order for a *held* key — run the command, then
    edit the file — which is exactly backwards here: the container to `exec` into is crash-looping
    and the command itself dies constructing `Settings`. `--old-key` is deliberately exempt from
    the floor so this door stays open (`core/secrets_cli._rewrap`); the door is usable in one
    direction only, and until now that direction was written down in a test docstring and nowhere
    an operator could reach. [M4.7 cs-44, spec-08]
    """
    problems = _stranded_rotation_problems(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert not problems, (
        f".env.example's SECRETS_KEY block: {problems}. It is the only rotation procedure this "
        "repository gives an operator, and it is read while the app will not start."
    )


def test_the_stranded_rotation_guard_sees_the_paragraph_go_missing():
    """The state the block shipped in: the refusal stated twice, and no way out of it."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8").replace(STRANDED_ANCHOR, "under 32 characters")
    assert _stranded_rotation_problems(text) == [
        "no way out of the SECRETS_KEY floor is written down at all"
    ]


def test_the_stranded_rotation_guard_sees_the_order_that_cannot_work():
    """And the state it would ship in if the exception were paraphrased back into the rule."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    recipe = _stranded_rotation(text)
    edit = next(row for row in recipe.splitlines() if "set SECRETS_KEY" in row)
    reordered = recipe.replace(edit + "\n", "") + edit + "\n"
    problems = _stranded_rotation_problems(text.replace(recipe, reordered))
    assert any("cannot work" in problem for problem in problems), problems


def test_the_rewrap_command_cannot_run_before_the_new_key_reaches_the_environment(monkeypatch):
    """Why that ordering is a constraint and not a preference — measured, not assumed.

    `spielplan-secrets` builds the same `Settings` the app refuses to boot with, one statement
    before it opens a connection (`core/secrets_cli._connect`), and the `--new-key` floor check
    above it passes. So on the stranded install the command dies on the value sitting in `.env`
    rather than on either argument it was handed, with a message naming SECRETS_KEY — which reads
    as "the key you just typed is too short" and is not. A runbook command that does not work is
    worse than no runbook, because it is read under pressure. [M4.7 cs-44, spec-08]
    """
    from pydantic import ValidationError

    from spielplan.core import secrets_cli
    from spielplan.core.config import settings

    # 16 characters: over everything this repository checked before M4.7, and under the floor now.
    monkeypatch.setenv("SECRETS_KEY", "spielplan-secret")
    settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="SECRETS_KEY must be at least"):
            secrets_cli.main(["rewrap", "--old-key", "spielplan-secret", "--new-key", "n" * 40])
    finally:
        settings.cache_clear()


# --- §14.3: the two gestures the chown takes away from the host operator -------------------------

# `data/import` ends up uid-1000 mode 0755, and creating or removing an entry in a directory needs
# write on the directory. The operator can list it and read it, and can do neither of the only two
# things they are ever asked to do in it.
IMPORT_GESTURES = (
    # There is no upload route: `api/artifacts._resolve` defaults to `cfg.import_dir` and 404s
    # unless the bundle is already on that host directory.
    "sudo install -o 1000 -g 1000",
    # `importer/bundle._unpack` writes `.unpacked-<stem>` beside the archive as uid 1000, and
    # `_clean_unpacked` removes it only when the import committed.
    "sudo rm -rf data/import/.unpacked-",
)


def _import_gestures_missing(readme: str) -> list[str]:
    return [gesture for gesture in IMPORT_GESTURES if gesture not in readme]


def test_the_document_that_requires_the_chown_says_how_to_reach_the_import_directory():
    """The chown is README's own instruction, and it locks the operator out of two of its steps.

    The wizard's third step is "import the bundle" and the bundle has to be on `./data/import`
    before the app can see it; the `.unpacked-*` tree README hands back to the operator ("that
    last one is yours to delete") is uid-1000 inside a uid-1000 directory. `e2e/reset.mjs` already
    raises with "Run `sudo rm -rf data/artifacts/*`" for the mirror case one directory over.
    [M4.7 sec-08]
    """
    missing = _import_gestures_missing(README.read_text(encoding="utf-8"))
    assert not missing, (
        f"README requires the chown and does not say how to work with it: {missing}. Both "
        "gestures answer `Permission denied` for the operator who followed 'Running it' verbatim."
    )


@pytest.mark.parametrize("gesture", IMPORT_GESTURES)
def test_the_import_gesture_guard_sees_either_half_go_missing(gesture):
    readme = README.read_text(encoding="utf-8").replace(gesture, "")
    assert _import_gestures_missing(readme) == [gesture]


# --- §2 Configuration: the .env CI runs on is the one .env.example produces ----------------------


def _env_the_e2e_job_writes(workflow: str) -> list[str]:
    """The variable names the e2e job puts into the stack's `.env`."""
    body = workflow[workflow.index("cat > .env <<EOF"):]
    body = body[body.index("\n") + 1:]
    end = re.search(r"^\s*EOF\s*$", body, re.M)
    return [row.split("=", 1)[0].strip()
            for row in (body[: end.start()] if end else body).splitlines() if "=" in row]


def test_the_e2e_job_runs_on_the_database_url_the_compose_file_builds():
    """Step 8 made DATABASE_URL a nested compose default, and nothing had ever evaluated it.

    `docker-compose.yml` builds it out of the three POSTGRES_* values so the password is stated
    once (ops-12), and `.env.example` ships the variable commented out — so that expression is
    what a household runs on. CI wrote an explicit `DATABASE_URL=` into the `.env` it generates,
    which short-circuits `${DATABASE_URL:-...}` in every e2e run, so the household's first boot
    was its first evaluation anywhere. Dropping the line is the assertion: the backend connects on
    the built URL or this job fails. [M4.7 ops-12]
    """
    written = _env_the_e2e_job_writes(WORKFLOW.read_text(encoding="utf-8"))
    assert "DATABASE_URL" not in written, (
        "the e2e job pins DATABASE_URL in the .env it writes, so docker-compose.yml's default is "
        "short-circuited and the expression the household actually runs on is never evaluated"
    )
    # Without these three the assertion above is vacuous: the compose default would fall back to
    # its own `spielplan:spielplan` and still not be the household's line under test.
    assert {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} <= set(written), written


def test_the_generated_env_guard_sees_the_pinned_url_come_back():
    """The line as it stood, put back verbatim."""
    pinned = "DATABASE_URL=postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/$POSTGRES_DB"
    workflow = WORKFLOW.read_text(encoding="utf-8").replace(
        "          POSTGRES_DB=$POSTGRES_DB\n",
        f"          POSTGRES_DB=$POSTGRES_DB\n          {pinned}\n",
    )
    assert "DATABASE_URL" in _env_the_e2e_job_writes(workflow)
