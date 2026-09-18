"""CLAUDE.md's three layering rules, as tests instead of conventions.

Three sentences say where this codebase's code is allowed to live -- "Rules live in the domain
packages under `backend/spielplan/` ...; `api/` decides only HTTP shapes", "`ledger/model.py` is
numpy-only by contract", and §3.2's session in front of every route -- and until M4.16 the suite
read none of them. Each is repealed by one absent-minded line, and each repeal passes the whole
suite green: the inversion `push/send.py` carried was a single import, a domain rule moves into
`api/` as a single query, and a route ships ungated by leaving one annotation off a signature.
So every layering repair this release made could regress in silence, which is the finding this
file closes. [M4.16 finding 18 / arch-13]

One file for three rules, because they share a subject -- what `api/` and `ledger/` are each
allowed to reach -- and because the ast helpers below would otherwise move to a fourth file that
none of the three owns. Each rule carries its own self-tests, for the reason rule 1's last
paragraph gives. NONE OF THE THREE LICENSES AN ARCHITECTURE: there is no repository layer, no
service layer and no middleware here, and rule 2 in particular records where the SQL is rather
than proposing that it move (moving it is the roadmap's DEFERRED-C, and is nobody's work here).

--- rule 1: the numeric core is numpy-only -------------------------------------------------

CLAUDE.md's Conventions say `ledger/model.py` is numpy-only by contract -- no DB, no clock, no
torch -- and §5.2 says why: the fit is a pure function of the observations and the bundle's
constants. That purity is what makes §5.3's budgets measurable at all (a solver that opens a
connection is timed through Postgres), what lets `test_ledger_contract.py` run the solver over a
distribution of synthetic households at no I/O cost, and what keeps a refit reproducible from the
rows that fed it rather than from the wall clock it happened to run at.

[M4.13 finding 28] The contract held and nothing stated it. `model.py`'s only imports are
`__future__`, `dataclasses`, `numpy` and `spielplan.ledger.hyperparams`, and a grep over
`backend/tests` found no test asserting that, so a stray `from datetime import datetime` for a
freshness stamp or an `import torch` for a faster solver would have passed the entire suite. Both
were live temptations in this milestone rather than hypotheticals: the freshness stamps and
`rescale_level` landed in `observations.py` and `refit.py` precisely because they do not belong
here.

The guard is an ast walk, not a grep, and it looks at more than the top of the file:

  * `Import` and `ImportFrom` anywhere in the tree -- module level, inside a function, inside a
    class -- because an import moved into the one function that needs it is the same dependency.
  * `TYPE_CHECKING` blocks as well. The contract is about what the module depends on, not about
    what it executes: a module whose annotations name a connection pool has been designed to take
    one, and the edit that actually passes it in is then a one-liner nothing would catch.
  * relative imports resolved to their absolute names, so `from .refit import refit_user` cannot
    walk past a guard that compares module strings.

Two limits, stated rather than papered over. The guard reads modules, not names, so
`from spielplan.ledger import hyperparams` is reported even though it reaches the allowed leaf --
spell the leaf, as the file already does. And it reads import statements, so an
`importlib.import_module("torch")` would evade it; the edit this exists to catch is the
absent-minded one, and a deliberate one is what review is for.

The allow-list is a list of modules; the guarded set is a list of files. arch-12's solver split is
deferred, so the guarded set has exactly one entry today. WHEN THAT SPLIT LANDS, ADD THE NEW
MODULE TO `GUARDED` RATHER THAN WIDENING `ALLOWED`: the contract is that the numeric core is
numpy-only, and an allow-list that grows to fit each new file measures nothing.

Every rejection the guard can make is exercised below. A guard that cannot report a violation is
a green line rather than a proof -- the argument M4.8 made for the guards in
`test_landmine_guards.py`, and the reason the six subjects the review probed (asyncpg, torch,
datetime, time, `spielplan.db.pool` and `spielplan.ledger.refit`) are each spelled out here.
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from pathlib import Path

from starlette.routing import Mount

from spielplan.api import deps
from spielplan.app import create_app
from spielplan.core.config import settings

# The prose readers, borrowed rather than re-written: `_comment_paragraphs` is this tree's notion
# of what a reader reads - tokenized comments and docstrings off the AST, so a count inside a
# string literal is not prose - with a run of adjacent `#` lines glued back into the paragraph it
# was written as, and `_COUNT_WORDS` is its vocabulary for a hand-spelled one. Borrowed from there
# rather than from `test_worker_schedule.py`'s sibling reader, which would drag `worker` and torch
# into a file that imports the app and nothing else. The paragraph reader rather than
# `_comment_prose` because this file's own note wrapped its count onto the next line and escaped
# the guard below for it. [M5.1 review cycle 2; M5.1 review cycle 3, M51-C3-REG-02]
from tests.test_static_contracts import _COUNT_WORDS, _comment_paragraphs

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"

# The files the numpy-only contract covers, relative to the package root. One today; see the
# docstring on what a later solver split must do to this tuple.
GUARDED = ("ledger/model.py",)

# `typing` and `math` are in the list although `model.py` imports neither: they are the two
# additions that would still leave the fit a pure function of observations and constants, and a
# guard whose allow-list is exactly today's imports would fail on a legal edit and be widened in
# a hurry -- which is how allow-lists stop meaning anything. Everything else is a finding.
ALLOWED = frozenset(
    {
        "__future__",
        "dataclasses",
        "typing",
        "math",
        "numpy",
        "spielplan.ledger.hyperparams",
    }
)

# A module that imports only what the contract allows, in the shapes the real file uses. The
# self-tests append one offending statement to it, so each of them isolates its own subject
# rather than asserting against a source that was never clean.
LEGAL = """\
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from spielplan.ledger.hyperparams import Hyperparams
"""


def _package_of(relative: str) -> str:
    """The dotted package a guarded file lives in, for resolving its relative imports."""
    return ".".join(("spielplan", *Path(relative).parent.parts))


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """`from .refit import x` inside `spielplan.ledger` is an import of `spielplan.ledger.refit`.

    Resolved rather than skipped: the relative spelling is the one a developer reaches for when
    adding a sibling import, and a guard blind to it would be green on the likeliest violation.
    """
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    base = ".".join(parts[: len(parts) - node.level + 1])
    return f"{base}.{node.module}" if node.module else base


def _imported_modules(source: str, *, package: str) -> set[str]:
    """Every module `source` imports, by absolute name, from anywhere in the tree."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(_absolute(node, package))
    return modules


def _permitted(module: str) -> bool:
    """`numpy.linalg` is numpy; `spielplan.ledger.refit` is not `spielplan.ledger.hyperparams`."""
    return any(module == ok or module.startswith(f"{ok}.") for ok in ALLOWED)


def _violations(source: str, *, package: str = "spielplan.ledger") -> list[str]:
    """The imports the allow-list does not cover, sorted so a failure message is stable."""
    return sorted(m for m in _imported_modules(source, package=package) if not _permitted(m))


# --- the guard -----------------------------------------------------------------------


def test_the_ledger_model_imports_nothing_but_numpy_and_its_own_constants():
    """CLAUDE.md's Conventions; §5.2's fit as a pure function of observations and constants."""
    for relative in GUARDED:
        source = (PACKAGE / relative).read_text(encoding="utf-8")
        package = _package_of(relative)
        imports = _imported_modules(source, package=package)
        assert imports, (
            f"spielplan/{relative}: the guard parsed no import at all, which means it is "
            "measuring the parser rather than the file"
        )
        offenders = _violations(source, package=package)
        assert not offenders, (
            f"spielplan/{relative} is numpy-only by contract (CLAUDE.md Conventions, §5.2) and "
            f"these imports break it: {offenders}. A DB, clock or torch dependency in the solver "
            "makes the fit a function of more than its observations; put the new code in "
            "ledger/observations.py or ledger/refit.py, which may have all three."
        )


# --- self-tests: a guard that cannot report a violation is not a guard ----------------


def test_the_guard_rejects_a_database_import():
    """The two spellings a fit that wanted "just one query" would take, and the clean baseline.

    The allow-list half matters as much as the rejection: a guard that reported every import
    would also be green forever, because the first person to hit it would delete it.
    """
    assert _violations(LEGAL) == []
    assert _violations(LEGAL + "import asyncpg\n") == ["asyncpg"]
    assert _violations(LEGAL + "def fit():\n    from spielplan.db.pool import acquire\n") == [
        "spielplan.db.pool"
    ]


def test_the_guard_rejects_a_clock_import():
    """Inside a function and inside a `TYPE_CHECKING` block -- both are clock dependencies.

    §5.2's fit is a function of observations and constants, so a refit run twice over the same
    rows must produce the same board. A `datetime.now()` anywhere in the solver breaks that, and
    an annotation naming a clock is the edit before the one that calls it.
    """
    assert _violations(LEGAL + "def refit():\n    from datetime import datetime\n") == ["datetime"]
    assert _violations(LEGAL + "if TYPE_CHECKING:\n    import time\n") == ["time"]


def test_the_guard_rejects_a_torch_import():
    """§4.3 keeps torch behind the Cold Tower, on a CPU-only index; §5.2's solver is numpy."""
    assert _violations(LEGAL + "import torch\n") == ["torch"]
    assert _violations(LEGAL + "from torch import nn\n") == ["torch"]


def test_the_guard_rejects_an_import_of_the_refit_module():
    """Absolute and relative, because the sibling import is the one that gets written.

    `ledger/refit.py` is the layer above: it reads rows, holds the lock and writes the partition.
    An import of it from `model.py` would be a cycle and a database dependency in one line.
    """
    assert _violations(LEGAL + "from spielplan.ledger.refit import refit_user\n") == [
        "spielplan.ledger.refit"
    ]
    assert _violations(LEGAL + "from .refit import refit_user\n") == ["spielplan.ledger.refit"]


# --- the other direction: the leaf that keeps `ledger -> scoring` acyclic --------------


def _first_party(source: str, *, package: str) -> list[str]:
    """The `spielplan.*` modules `source` imports, sorted."""
    return sorted(
        m for m in _imported_modules(source, package=package) if m.split(".")[0] == "spielplan"
    )


def test_the_hyperparams_leaf_imports_nothing_but_the_standard_library():
    """M4.13's new `ledger -> scoring` edge closes no cycle, and this is the reason it does not.

    `observations.standard_embeddings` imports `scoring.backbone`, and step 34d gave
    `scoring/backbone.py` and `scoring/foldin.py` a module-level `from spielplan.ledger.hyperparams
    import DEFAULTS` -- so the graph really does run `ledger.observations -> scoring.backbone ->
    ledger.hyperparams` and back into `ledger`. What terminates it is that the leaf imports nothing
    first-party and `ledger/__init__.py` imports no submodule, so arriving at `spielplan.ledger`
    executes a docstring.

    Asserted rather than argued because the comment that argued it was already wrong when it was
    written (it said `backbone.py` imports only numpy, in the same diff that gave it a ledger
    import), and because the edit that would close the cycle is the plausible one: step 34d has
    just made `hyperparams.py` the home of the constants `scoring/` reads, so the next constant
    that wants a `scoring` name lands here. A cycle surfaces as an ImportError during `app.py`'s
    lifespan -- a boot failure, not a test failure -- which is the failure this test converts.

    NOT a new entry in `GUARDED`: that tuple is the numpy-only contract over `ledger/model.py` and
    the plan freezes it at one file until arch-12's solver split. This is a different property of a
    different file. [M4.13 cycle 1, M413-REV-03]
    """
    leaf = _first_party(
        (PACKAGE / "ledger" / "hyperparams.py").read_text(encoding="utf-8"),
        package="spielplan.ledger",
    )
    assert leaf == [], (
        f"ledger/hyperparams.py imports {leaf}: it is the leaf `scoring.backbone` reaches through "
        "`ledger.observations`, so a first-party import here can close that path into a cycle. "
        "Put the constant's reader somewhere that is not imported by `scoring/`."
    )
    root = _first_party(
        (PACKAGE / "ledger" / "__init__.py").read_text(encoding="utf-8"),
        package="spielplan.ledger",
    )
    assert root == [], (
        f"ledger/__init__.py imports {root}: importing `spielplan.ledger.hyperparams` then runs "
        "them, so the leaf stops being a leaf"
    )
    # The guard can report the violation it exists for, in the shape it would arrive in.
    assert _first_party(
        "from spielplan.scoring.backbone import EVIDENCE_K\n", package="spielplan.ledger"
    ) == ["spielplan.scoring.backbone"]


# --- rule 2: `api/` decides only HTTP shapes ------------------------------------------
#
# CLAUDE.md's Conventions: "Rules live in the domain packages under `backend/spielplan/` (ledger,
# rate, scoring, placement, home, sync, connectors, importer); `api/` decides only HTTP shapes."
# Two halves, because the rule fails in two directions and neither half sees the other's failure.
#
# The FIRST half is the import direction. `api/` is the outermost layer: it may reach any domain
# package, and no domain package may reach it. An import the other way is not a style complaint --
# it is a cycle waiting for its second edge, it makes the domain package untestable without
# FastAPI, and it is how a rule ends up living in a route module by accident. This one was real:
# `push/send.py` imported `device_handle` out of `api/push.py` to label a device, and naming a
# device is a rule. The import is gone and survives only as the comment recording why, which is
# exactly why this guard reads the AST rather than the text -- a grep for the module name would
# report that comment as a violation, and a grep for the absence of the phrase would be satisfied
# by a module that mentions it in a comment and performs it three lines lower.
#
# The SECOND half is the raw SQL that never moved. `api/` still writes its own queries in eleven
# modules, and that residue is the rule's live counter-example. It is recorded rather than moved:
# moving it is the roadmap's DEFERRED-C, it would touch files other milestones own, and this
# milestone's job is to make the contract measurable rather than to spend its diff on an
# architecture nobody asked for. So the baseline below says what is there today and holds the line
# in BOTH directions -- a count that grows is a new rule written into the HTTP layer, and a count
# that drops below its entry without the entry moving is a baseline quietly turning into an
# allowance. The stale half matters more: a ratchet that only tightens one way becomes an excuse
# the moment a module is emptied, and this exact baseline WAS stale before it was ever written --
# the plan's draft counts (`tonight.py 7`) predate M4.12 emptying most of that module into
# `tonight/`. Every number below was measured on this tree at write time, not copied.


def _sql_strings(node: ast.AST):
    """Every string literal in `node`, counting an f-string once rather than once per chunk.

    `ast.walk` yields a JoinedStr AND each of its `Constant` pieces, so the one statement in
    `api/library.py`'s `neighbours()` -- an f-string, because the tier table is interpolated --
    was counted twice by the obvious version of this. Recursing by hand and stopping at a
    JoinedStr is what makes the count a count of statements.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield node.lineno, node.value
        return
    if isinstance(node, ast.JoinedStr):
        # An interpolation becomes a space: `FROM {table} o` must not read as `FROM o`, and what
        # the caller looks at is the head of the string anyway.
        yield node.lineno, "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " "
            for v in node.values
        )
        # AND INTO THE INTERPOLATIONS, since review cycle 4. Stopping at the JoinedStr made an
        # f-string a hiding place for whole statements rather than for the tier table it was
        # written about: `f"{'SELECT id FROM user_title WHERE member = $1' if owned else 'SELECT
        # id FROM title'} ORDER BY id"` yields `['  ORDER BY id']` and counts ZERO, where the same
        # two statements written as constants count two -- so the ratchet's not-stale half, which
        # the paragraph above says pushes the next query out of the module it was written for,
        # could be satisfied by a two-branch f-string. That is reach by NODE, after cycles 1 and 2
        # closed reach by directory and reach by file, and it is the shape the docstring argument
        # above applies to most directly: a scanner that skips a node is green because it never
        # looked. The joined text is built only from the `Constant` pieces, so a statement nested
        # inside a `FormattedValue` cannot be counted twice by this.
        # [row `platform-api-layer-holds-no-domain-rules`; M4.16 cycle 4, M416-C4-ARCH13-02]
        for interpolated in node.values:
            if isinstance(interpolated, ast.FormattedValue):
                yield from _sql_strings(interpolated)
        return
    for child in ast.iter_child_nodes(node):
        yield from _sql_strings(child)


# A string is a statement when it OPENS with a verb, optionally behind its own SQL comment block.
# Anchored at the head deliberately: `"no such title"` and a docstring saying "the SELECT below"
# are prose, and a scanner that counted the word anywhere would be measuring English. The comment
# prefix is not a nicety -- `api/library.py`'s DNA-neighbour statement opens with fourteen lines of
# `--` argument before `WITH mine AS`, and dropping it would under-count the one module whose SQL
# carries the most reasoning.
#
# THE NON-DML HALF, and why each of its verbs is anchored on the OBJECT rather than standing
# alone. The set was `select|insert|update|delete|with`, so a statement opening CREATE, TRUNCATE,
# LOCK, ALTER, DROP, COPY, MERGE or REFRESH counted as ZERO and the not-grown half of the ratchet
# could not see it -- while `platform-api-layer-holds-no-domain-rules` sells the count as "raw SQL
# statements", which is wider than what was measured. Two of those spellings are live runtime SQL
# in this codebase already (`importer/load.py`'s `CREATE TEMP TABLE _import_title ...` and
# `backup/movie_data.py`'s `LOCK TABLE`), so they are shapes this project writes rather than
# shapes it might; and `api/deps.py` and `api/rank.py` already take locks from the HTTP layer,
# spelled `SELECT pg_advisory_xact_lock(...)` and therefore counted, which is what makes
# `LOCK TABLE` the spelling that would have slipped through.
#
# A bare alternation was the obvious widening and it is the wrong one, MEASURED: this scanner
# reads docstrings as well as expressions -- on purpose, so a docstring is not a hiding place --
# and CREATE, DROP, COPY, MERGE and LOCK all open ordinary English sentences where SELECT and
# INSERT do not. Bare verbs record `api/home.py`'s banner key `"copy"` and `api/push.py`'s "Drop
# one device, scoped to the member who owns it." as raw SQL, which puts a 1 against the one module
# `ALLOWED_RESIDUE` holds up as the shape the rule wants and turns the ratchet red on arrival --
# whose only green repair is to write that 1 into the baseline, i.e. exactly the allowance the
# paragraph below exists to deny it. So each non-DML verb is read with what it acts on, which is
# the head anchoring's own argument one verb further in: a scanner that counted the word alone
# would be measuring English. Over `api/` and `app.py` the widening is nil -- the same twelve
# keys, the same counts, the same total of sixty. [M4.16 cycle 3, M416-C3-ARCH13-01]
_SQL_OBJECT = (
    r"(?:table|index|view|schema|sequence|function|trigger|type|extension|materialized\s+view)"
)
#
# AND THE OTHER COMMENT SYNTAX, since review cycle 4. The prefix read `--` and nothing else, so
# `"/* decision 12 */ DELETE FROM job_run WHERE finished_at < now()"` counted ZERO while the same
# statement without its comment counted one -- a DML statement read at the verb, which is what the
# row's `what` says is counted, hidden behind the standard block comment. `api/library.py` opens a
# statement with fourteen `--` lines of argument, so an author with a heavily argued statement and
# a count that may not grow has the spelling in front of them. `(?s:...)` is scoped to the block
# rather than set on the whole pattern, because `--[^\n]*` means a line comment and must go on
# meaning one. [M4.16 cycle 4, M416-C4-ARCH13-02]
_SQL_HEAD = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|(?s:/\*.*?\*/)\s*)*(?:"
    r"(?:select|insert|update|delete|with)\b"
    r"|create\s+(?:or\s+replace\s+|unique\s+|temp\w*\s+|global\s+|local\s+|unlogged\s+)*"
    + _SQL_OBJECT + r"\b"
    r"|(?:alter|drop)\s+" + _SQL_OBJECT + r"\b"
    r"|truncate\s+(?:table\s+)?[\"\w]"
    r"|lock\s+table\b"
    r"|copy\s+[\"\w.]+[\"\s]*(?:\(|from\b|to\b)"
    r"|merge\s+into\b"
    r"|refresh\s+materialized\s+view\b"
    r")",
    re.IGNORECASE,
)

# The raw SQL statement count of every module under `api/`, AT ANY DEPTH, MEASURED AT M4.16
# (2026-09-17) by the scanner above, over docstrings as well as expressions -- a statement does not
# stop being one by being quoted in a docstring, and a scanner that skipped them would be offering
# a hiding place. Modules not named here hold zero, and that is asserted rather than assumed:
# `api/home.py` and `api/rate.py` are both at zero today and both are the shape the rule wants, so
# a first query appearing in either has to fail rather than arrive under a missing key. A module in
# a subpackage is keyed by its path -- `admin/queries.py` -- for the same reason: the day `api/`
# grows a package, its queries are new SQL in the HTTP layer rather than SQL nobody counted.
#
#
# AND `app.py`, which is HTTP-layer code for THIS half of the rule. `_domain_modules` excludes it
# from the import half and says why -- it is the composition root, so it imports every mounted
# router by definition -- and that argument is the import rule's alone. `app.py` also DECLARES A
# ROUTE, `@app.get("/api/health")`, whose handler carries a query: measured, one `SELECT 1`. So
# the HTTP layer's real residue was sixty statements and this dict recorded fifty-nine, with the
# sixtieth in the one file the ratchet could not see. That matters because of what the ratchet
# does to an author: its not-stale half pushes the next query out of the module it was going to
# live in, and `app.py` is where routes are already declared and where one query already sits.
# `worker.py` sits at the same package root and is deliberately NOT scanned -- it serves no
# request, so a route's query has no reason to land there. [M4.16 cycle 2, M416-C2-ARCH13-01]
#
# AND M5.1 ADDS TWO MODULES AND NOT ONE NUMBER, which is the outcome this dict's convention is
# written for rather than an omission. `api/acquisition.py` (§6.6's acquisition board, two admin
# reads) and `api/events.py` (§7.2's namespace, mounted with no routes yet) both measure ZERO:
# every statement the board runs lives in `spielplan/acquire/board.py`, where the rule about what
# decision 345 lets the board show belongs. So neither gets a key. A `0` row would say the same
# thing in a form the grown half already says better -- a module absent from this dict holds zero
# BY ASSERTION, and the first query to appear in either file fails as `(1, 0)` rather than
# arriving under a key somebody wrote in advance. The figures below are therefore unchanged by
# that milestone, and that is the measurement rather than a decision not to measure.
# [M5.1, plan step F5; decisions 332 and 345]
#
# Sixty statements in twelve modules. The total is re-stated by hand below so this paragraph
# can be falsified; `test_the_recorded_residue_totals_what_this_file_claims` is what falsifies it.
ALLOWED_RESIDUE = {
    "admin.py": 17,
    "auth.py": 9,
    "setup.py": 9,
    "artifacts.py": 6,
    "push.py": 5,
    "library.py": 4,
    "tonight.py": 4,
    "state.py": 2,
    "app.py": 1,
    "deps.py": 1,
    "passkeys.py": 1,
    "rank.py": 1,
}
RESIDUE_TOTAL = 60


def _is_api_module(module: str) -> bool:
    """`spielplan.api`, or anything under it. Not `spielplan.apis`, not `spielplan.api_shapes`."""
    return module == "spielplan.api" or module.startswith("spielplan.api.")


def _api_dependencies(source: str, *, package: str) -> list[str]:
    """Every way `source` names something in `spielplan.api`, as absolute module names.

    Four spellings, because the rule is about the dependency and not about a line of text:
    `import spielplan.api.push`, `from spielplan.api.push import device_handle`, the relative
    `from ..api.push import device_handle` a sibling package reaches for, and `from spielplan
    import api` -- which names the package in the ALIAS rather than the module, so its module
    string alone is `spielplan` and a check over modules would miss it entirely.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if _is_api_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            if _is_api_module(module):
                found.add(module)
            elif module == "spielplan":
                found.update(
                    f"spielplan.{alias.name}" for alias in node.names if alias.name == "api"
                )
    return sorted(found)


def _domain_modules() -> list[Path]:
    """Every package file the import rule covers: `spielplan/` minus `api/` and `app.py`.

    `app.py` is excluded because it is the composition root -- it exists to mount the routers, so
    it imports every one of them by definition. `api/` is excluded because the rule is about what
    reaches INTO it, and its own modules legitimately import `api/deps.py`.
    """
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.relative_to(PACKAGE).parts[0] != "api"
        and path.relative_to(PACKAGE).as_posix() != "app.py"
    ]


def _residue(api: Path | None = None) -> dict[str, int]:
    """The raw SQL statement count of every module under `api/`, zeroes included, at any depth.

    `rglob`, in step with `_domain_modules()` seven lines above, and keyed by the path relative to
    `api/` so a module in a subpackage has a name of its own. The flat `glob` this replaced left
    the not-grown half of the ratchet a one-directory escape: a query written under
    `api/admin/queries.py` was never counted, never compared against `ALLOWED_RESIDUE` and never
    reported, while the guard went on printing a total and reading as a proof. That is not a
    hypothetical route in -- `api/admin.py` is the largest entry at seventeen statements, and
    splitting a module that size into a package is what M4.12 did to `api/tonight.py`. The stale
    half makes it worse rather than better: on the day of the split it fires, and the only way to
    make it green is to delete the entry, which is exactly the edit that blinds it.

    `api` is an argument for the reason `_residue_findings` is one -- a scanner only ever run over
    the real tree is a scanner never seen to reach anything. Measured when this landed the change
    was nil: the same keys, the same counts, the same total. The tree carried eleven modules and
    fifty-nine statements that day and carries what `ALLOWED_RESIDUE` and `RESIDUE_TOTAL` above
    say today; those two are the figures a reader should reconcile against, and this sentence
    read as a statement about the current tree until M4.16 cycle 4, by which time it was neither.
    [M4.16 cycle 1, M416-REV-ARCH13-01; re-dated cycle 4]

    AND `app.py`, which declares `/api/health` and whose handler runs a query. It is exempt from
    the import rule for a reason that is the import rule's alone -- the composition root imports
    every router by definition -- and it was exempt from this one by accident, because the scan
    roots at `api/`. So the ratchet counted fifty-nine of the HTTP layer's sixty statements and
    could not see the file where routes are already declared. Only when the real tree is scanned:
    a caller that passes `api` is testing the scanner and gets exactly what it handed over.
    [M4.16 cycle 2, M416-C2-ARCH13-01]
    """
    root = PACKAGE / "api" if api is None else api
    sources = {path.relative_to(root).as_posix(): path for path in sorted(root.rglob("*.py"))}
    if api is None:
        sources["app.py"] = PACKAGE / "app.py"
    counts: dict[str, int] = {}
    for key, path in sources.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        counts[key] = sum(1 for _, text in _sql_strings(tree) if _SQL_HEAD.match(text))
    return counts


def _residue_findings(
    measured: dict[str, int], baseline: dict[str, int]
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """(grown, stale), each `module -> (measured, recorded)`. Factored out so it can be driven.

    A comparison that only ever runs against the real tree is a comparison never seen to fail; the
    self-tests below feed this synthetic pairs instead.
    """
    grown = {
        module: (count, baseline.get(module, 0))
        for module, count in measured.items()
        if count > baseline.get(module, 0)
    }
    stale = {
        module: (measured.get(module, 0), count)
        for module, count in baseline.items()
        if measured.get(module, 0) < count
    }
    return grown, stale


def _residue_table(measured: dict[str, int]) -> str:
    """The measured counts as an ASCII block, for a failure that answers "so what do I write now".

    In full rather than just the offending module: whoever reads this failure has to retype the
    dict above, and a report showing only the delta sends them off to measure the rest by hand.
    """
    rows = sorted(measured.items(), key=lambda item: (-item[1], item[0]))
    return "\n".join(f"    {module:<16} {count}" for module, count in rows if count)


def test_no_domain_package_imports_from_the_api_layer():
    """CLAUDE.md Conventions: rules live in the domain packages; `api/` decides only HTTP shapes.

    No allow-list, and that is the state rather than the ambition: the one inversion this project
    has had -- `push/send.py` reaching for `api/push.device_handle` -- was repaired before this
    guard was written, so the guard arrives green over the whole package and can only tighten. An
    allow-list here would have been a list with one entry that nobody ever removed.
    """
    covered = {path.relative_to(PACKAGE).as_posix() for path in _domain_modules()}
    assert "push/send.py" in covered, (
        "the scan does not reach push/send.py, which is the file that carried the one import "
        "inversion this project has had - a guard that skips it is green because it never looked"
    )

    offenders = {}
    for path in _domain_modules():
        relative = path.relative_to(PACKAGE).as_posix()
        hits = _api_dependencies(path.read_text(encoding="utf-8"), package=_package_of(relative))
        if hits:
            offenders[f"spielplan/{relative}"] = hits
    assert not offenders, (
        f"a domain package imports from the HTTP layer: {offenders}. `api/` may reach any domain "
        "package and none may reach back - the import is a cycle waiting for its second edge, and "
        "it is how a rule ends up living in a route module. Move what is wanted into the domain "
        "package that owns it (CLAUDE.md Conventions)."
    )


def test_the_api_layer_holds_no_more_raw_sql_than_it_did_and_no_less():
    """The residue baseline, in both directions. CLAUDE.md Conventions; M4.16 arch-13.

    Not a ceiling. A module whose count DROPS below its entry fails too, until someone lowers the
    entry, because a baseline that only ratchets down silently is an allowance rather than a
    record -- and this file's own history is the argument: the draft counts written before M4.12
    emptied `api/tonight.py` were wrong in exactly that direction and nothing would have said so.
    """
    measured = _residue()
    assert sum(measured.values()), "the scanner found no SQL in api/ at all - it measures itself"

    grown, stale = _residue_findings(measured, ALLOWED_RESIDUE)
    assert not grown, (
        f"new raw SQL in the HTTP layer: {grown} (module: measured, recorded). A query is a rule "
        "about the data, so it belongs in the domain package that owns the rule; `api/` decides "
        "only HTTP shapes (CLAUDE.md Conventions). If the statement really does belong here, "
        "raise the entry deliberately and say why.\nmeasured today:\n" + _residue_table(measured)
    )
    assert not stale, (
        f"the recorded residue is stale: {stale} (module: measured, recorded). The SQL moved out "
        "and the baseline did not follow, so it is now an allowance for that much NEW SQL rather "
        "than a record of what is there. Lower the entry.\nmeasured today:\n"
        + _residue_table(measured)
    )


def test_the_residue_scanner_reaches_a_module_in_a_subpackage(tmp_path):
    """The reach assertion the ratchet's not-grown half rests on, in rule 2's idiom above.

    Rule 2 asserts `"push/send.py" in covered` because "a guard that skips it is green because it
    never looked", and this half had no equivalent: `sum(measured.values())` proves the scanner
    found SQL somewhere, never that it looked everywhere. A flat `glob` over `api/` satisfies that
    assertion with a whole package unread. Driven over a scratch tree rather than the real one
    because `api/` is flat today -- which is the state this holds, not an argument that it will
    stay that way. [M4.16 cycle 1, M416-REV-ARCH13-01]
    """
    api = tmp_path / "api"
    (api / "reports").mkdir(parents=True)
    (api / "flat.py").write_text('SQL = "update app_user set name = $1"\n', encoding="utf-8")
    (api / "reports" / "__init__.py").write_text("", encoding="utf-8")
    (api / "reports" / "queries.py").write_text(
        'COUNT = "select count(*) from title"\n'
        'PURGE = "delete from job_run where finished_at < now()"\n',
        encoding="utf-8",
    )
    assert _residue(api) == {"flat.py": 1, "reports/__init__.py": 0, "reports/queries.py": 2}
    # And the comparison sees it: a subpackage's SQL is NEW SQL in the HTTP layer, so it fails
    # against a baseline that does not name it rather than arriving under a missing key.
    grown, _stale = _residue_findings(_residue(api), {"flat.py": 1})
    assert grown == {"reports/queries.py": (2, 0)}, grown


def test_the_residue_scanner_reaches_the_module_that_declares_a_route_outside_the_package():
    """The second reach assertion, and the one `app.py` needed.

    The scan roots at `api/`, so the composition root was outside it -- and the composition root
    declares `/api/health` and runs `SELECT 1` inside the handler. Both halves are asserted here
    rather than in the baseline alone: that the file is still the one holding a route's query, and
    that the key it is counted under cannot be taken by a module inside `api/`.
    [M4.16 cycle 2, M416-C2-ARCH13-01]
    """
    measured = _residue()
    assert "app.py" in measured, (
        "the residue scan no longer reaches spielplan/app.py, which declares /api/health: the "
        "ratchet's stale half pushes the next query out of the module it was written for, and "
        "this is the adjacent file where routes already live"
    )
    assert not (PACKAGE / "api" / "app.py").exists(), (
        "api/app.py now exists and would be counted under the same key as the composition root: "
        "give one of them a key of its own before the two totals become one"
    )
    assert measured["app.py"] == 1, measured["app.py"]


def test_the_recorded_residue_totals_what_this_file_claims():
    """The comment above says sixty statements in twelve modules; this is what says so.

    Re-stated by hand in `RESIDUE_TOTAL`, in the idiom `test_api_gating.py`'s `ADMIN_ROUTE_COUNT`
    already uses: an argued paragraph carrying a number nothing checks is how the two documents
    this milestone exists to repair went stale in the first place.
    """
    assert sum(ALLOWED_RESIDUE.values()) == RESIDUE_TOTAL
    assert len(ALLOWED_RESIDUE) == 12


# --- self-tests: rule 2 ---------------------------------------------------------------


def test_the_import_direction_guard_catches_every_spelling_of_the_inversion():
    """The five ways `push/send.py` could reach back into `api/`, and the clean baseline."""
    assert _api_dependencies(LEGAL, package="spielplan.push") == []
    assert _api_dependencies(
        "from spielplan.api.push import device_handle\n", package="spielplan.push"
    ) == ["spielplan.api.push"]
    assert _api_dependencies("import spielplan.api.deps\n", package="spielplan.push") == [
        "spielplan.api.deps"
    ]
    # The alias spelling: the module string is `spielplan`, so a check over modules alone is blind.
    assert _api_dependencies("from spielplan import api\n", package="spielplan.push") == [
        "spielplan.api"
    ]
    # The sibling spelling, which is the one that actually gets typed.
    assert _api_dependencies("from ..api.push import device_handle\n", package="spielplan.push") == [
        "spielplan.api.push"
    ]
    # Inside a function, for the same reason rule 1 looks there: a deferred import is a dependency.
    assert _api_dependencies(
        "def label():\n    from spielplan.api.push import device_handle\n",
        package="spielplan.push",
    ) == ["spielplan.api.push"]


def test_the_import_direction_guard_reads_imports_and_not_prose():
    """`push/send.py` RECORDS the inversion it no longer has, and must stay green anyway.

    Both halves matter. A text guard would report that comment as a violation and be deleted by
    whoever hit it; a text guard looking for the absence of the phrase would be satisfied by a
    module that mentions it in a comment and performs it three lines lower.
    """
    recorded = (
        '"""A domain module that records an import it no longer performs."""\n'
        "# naming a device is a rule, so the `from spielplan.api.push import device_handle` it\n"
        "# replaces was an inversion.\n"
        'NOTE = "from spielplan.api.push import device_handle"\n'
    )
    assert _api_dependencies(recorded, package="spielplan.push") == []
    assert _api_dependencies(
        recorded + "from spielplan.api.push import device_handle\n", package="spielplan.push"
    ) == ["spielplan.api.push"]
    # And the real file is the case this was written for.
    assert (
        _api_dependencies(
            (PACKAGE / "push" / "send.py").read_text(encoding="utf-8"), package="spielplan.push"
        )
        == []
    )


def test_the_sql_scanner_counts_statements_and_not_sentences():
    """Every verb at the head counts; a verb inside prose does not; an f-string counts once."""
    module = ast.parse(
        '"""A docstring that discusses the SELECT below without being one."""\n'
        'a = "SELECT 1 FROM title WHERE id = $1"\n'
        'b = "INSERT INTO job_run (name) VALUES ($1)"\n'
        'c = "UPDATE app_user SET name = $2 WHERE id = $1"\n'
        'd = "DELETE FROM push_subscription WHERE endpoint = $1"\n'
        'e = f"WITH mine AS (SELECT term FROM {table}) SELECT * FROM mine"\n'
        'f = "no such title"\n'
        'g = "  -- why this statement is shaped this way\\n  SELECT 1"\n'
    )
    heads = [text for _, text in _sql_strings(module) if _SQL_HEAD.match(text)]
    assert len(heads) == 6, heads

    # Review cycle 4, and each is a statement the scanner could not SEE rather than one it
    # miscounted. `_sql_strings` stopped at the JoinedStr, so a two-branch f-string yielded
    # only `'  ORDER BY id'` and the two SELECTs inside the interpolation counted zero; and
    # the comment prefix read `--` alone, so a DELETE behind the other SQL comment syntax
    # counted zero as well. Written at column 0 because `ast.parse` reads a module, and the
    # f-string is still counted ONCE for its own head, which is what the JoinedStr branch is
    # for. [M4.16 cycle 4, M416-C4-ARCH13-02]
    hidden = ast.parse(
        """
a = f"{'SELECT id FROM user_title WHERE member = $1' if owned else 'SELECT id'} ORDER BY id"
b = "/* decision 12 */ DELETE FROM job_run WHERE finished_at < now()"
c = f"SELECT {columns} FROM title WHERE id = $1"
"""
    )
    assert len([t for _, t in _sql_strings(hidden) if _SQL_HEAD.match(t)]) == 4, [
        t for _, t in _sql_strings(hidden)
    ]


def test_the_sql_scanner_counts_the_statements_that_are_not_dml():
    """Eight verbs that are not SELECT, and the English the same eight words open.

    Both halves in one test, because the answer to either alone is a different pattern: bare
    verbs take the whole first block AND `"copy"`, `"drop"` and three docstrings with it, while
    DML alone takes none of the first block. The two spellings marked live are in this package
    today (`importer/load.py`, `backup/movie_data.py`), so the first block is what this project
    writes rather than what it might. [M4.16 cycle 3, M416-C3-ARCH13-01]
    """
    statements = ast.parse(
        'a = "CREATE TEMP TABLE _import_title (LIKE title INCLUDING DEFAULTS) ON COMMIT DROP"\n'
        'b = "LOCK TABLE user_title IN SHARE ROW EXCLUSIVE MODE"\n'
        'c = "TRUNCATE home_shelf_cache"\n'
        'd = "ALTER TABLE title ADD COLUMN retired_at timestamptz"\n'
        'e = "DROP INDEX title_name_trgm"\n'
        'f = "COPY title (id, name) FROM STDIN"\n'
        'g = "MERGE INTO title t USING stage s ON t.id = s.id"\n'
        'h = "REFRESH MATERIALIZED VIEW home_shelf"\n'
    )
    assert len([t for _, t in _sql_strings(statements) if _SQL_HEAD.match(t)]) == 8

    prose = ast.parse(
        '"""Drop one device, scoped to the member who owns it."""\n'
        'a = "copy"\n'
        'b = "drop"\n'
        'c = "Create an account first."\n'
        'd = "Merge a partial update into the stored connector."\n'
        'e = "Drop the cache. Section 10 restarts the process on a swap."\n'
        'f = "Truncated to the first 200 characters."\n'
        'g = "updated_at"\n'
    )
    assert [t for _, t in _sql_strings(prose) if _SQL_HEAD.match(t)] == []


def test_the_residue_comparison_fails_in_both_directions():
    """A count that grew, a count that dropped, a module that is new, and the clean case."""
    baseline = {"admin.py": 17, "state.py": 2}
    assert _residue_findings({"admin.py": 17, "state.py": 2}, baseline) == ({}, {})
    grown, stale = _residue_findings({"admin.py": 18, "state.py": 2}, baseline)
    assert grown == {"admin.py": (18, 17)} and stale == {}
    grown, stale = _residue_findings({"admin.py": 17, "state.py": 0}, baseline)
    assert grown == {} and stale == {"state.py": (0, 2)}
    # A module absent from the baseline holds zero by assertion rather than by omission: the first
    # query in `api/home.py` or `api/rate.py` must fail rather than arrive under a missing key.
    grown, stale = _residue_findings({"admin.py": 17, "state.py": 2, "home.py": 1}, baseline)
    assert grown == {"home.py": (1, 0)} and stale == {}


def test_the_residue_table_is_ascii():
    """The failure message prints to a Windows cp1252 console, where a glyph is a crash."""
    _residue_table(_residue()).encode("ascii")


# --- rule 3: every route is behind a session ------------------------------------------
#
# §3.2 puts the household behind a session cookie and §3.1 locks a new account to a password
# change until it makes one, and the app enforces both as dependencies rather than as a habit. The
# thing neither the spec nor the suite could do until now is ENUMERATE: `test_api_gating.py`
# sweeps the routes that ARE gated (does the gate fire? does an admin route refuse a member?) and
# `test_route_inventory.py` asserts every route is NAMED by some test, but a route added with no
# `ActiveUser` at all appears in neither -- it is simply absent from both walks, and absence reads
# as green. That is the shape of the failure this rule exists for: not a gate that stopped firing,
# but a gate nobody wrote.
#
# So this guard walks every route the app registers and classifies it, and the classification is
# exhaustive by construction -- anything that is neither gated nor named below is a finding. The
# two allow-lists carry a REASON per entry rather than a bare path, because an anonymous route is
# a decision about what a stranger who can reach the origin may learn, and a list of paths records
# that a decision happened without recording what it was.
#
# A WebSocket that authenticates in its own body counts as UNGUARDED here, and that is deliberate
# rather than pedantic: the Tonight channel used to read `socket.cookies` and call `load_session`
# itself, on the ground that a socket cannot take a dependency. It can -- only not an HTTP one --
# and until M4.12 wired `active_user_ws`, the channel skipped §3.1's first-login lock and no walk
# in the suite could see it, because a hand-rolled check leaves nothing in `route.dependant`.
# `active_user_ws` is `active_user` in the transport that has no status code to refuse with, so it
# counts as the gate; a socket with no dependant at all, or with a dependant that resolves neither,
# does not. [decision 225; M4.16 arch-13]
#
# Implementation note, because it costs an afternoon otherwise: FastAPI 0.141.1 stops flattening
# `include_router`, so `app.routes` holds an `_IncludedRouter` object per mounted router. Its
# `effective_candidates()` gives HTTP routes with the prefixes applied but flattens the WebSocket
# into a wrapper carrying neither a path nor a dependant -- the Tonight channel simply vanishes
# from that walk, which is the one route this rule most needs to see. `original_router.routes`
# carries both, and the routers declare their own `prefix="/api"`, so the paths are complete.
# `app.openapi()["paths"]` is no use at all: it has neither the dependant nor the websockets.

# The dependencies that ARE the gate, and the weaker one that only proves a session.
_GATES = ("active_user", "admin_user", "active_user_ws")
_SESSION_ONLY = ("current_user", "current_user_ws")

_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# Every route that answers a caller with no session, with what makes that the right answer. Eight,
# and each one is a door, a probe or the first boot -- nothing here reads a person's data.
ANONYMOUS = {
    ("POST", "/api/auth/login"): "the password door itself; a session is what it issues",
    ("POST", "/api/auth/passkey/login/options"): "the WebAuthn challenge the door needs first",
    ("POST", "/api/auth/passkey/login"): "the passkey door itself",
    # Decision 179's fourth way out. It opens the incoming cookie itself and answers `{ok: true}`
    # either way, so it grants nothing and can refuse nothing; `test_api_gating.py` registers it in
    # `AUTHENTICATES_BY_HAND` and holds it live, which is where that half of the claim lives.
    ("POST", "/api/auth/logout"): "clears the cookie for whoever holds it, and grants nothing",
    # The image's HEALTHCHECK, CI's wait loop and `e2e/run.mjs` all probe this before an account
    # exists; the answer is a status code and the body names a bundle version, not a household.
    ("GET", "/api/health"): "the container probe, answered before anyone can sign in",
    # `api/library.py`: "what the shell needs before a user is known ... deliberately free of any
    # user or connector detail".
    ("GET", "/api/config"): "the origin and whether a bundle exists, for the shell's first paint",
    # Section 3.1's first boot: this route CREATES the first account, so there is nobody to be.
    ("POST", "/api/setup/admin"): "first boot has no account to authenticate as",
    # Anonymously it returns `required` and a note and nothing else -- sec-14 cut the rest, because
    # the full payload fingerprints the install to anyone who can reach the origin.
    ("GET", "/api/setup/state"): "whether this box still owes a wizard, cut to that one bit",
}

# Behind a session but deliberately NOT behind `active_user`: decision 179's ways out of section
# 3.1's first-login lock. A locked account must be able to see who it is and set a password, or the
# lock has no exit and the account is bricked. Three, because /logout is the fourth and is
# anonymous above. Not a weaker gate by accident: `test_api_gating.py` asserts this exact set from
# the other direction, so the two files disagree loudly if anyone widens it.
CURRENT_ONLY = {
    ("GET", "/api/auth/me"): "a locked account must be able to see whose lock it is",
    ("POST", "/api/auth/password"): "the way out of the lock",
    ("POST", "/api/auth/switch"): "the shared-device chip, reachable while one profile is locked",
}

# The one surface whose presence depends on configuration rather than on code. `app.py` appends the
# SPA fallback and mounts `/_app` only when `SPIELPLAN_STATIC_DIR` names a built frontend, which no
# pytest run has, so the walk below cannot see either of them. Named rather than skipped silently:
# the fallback serves the app shell and declines the `/api` namespace at match time, so it answers
# with a static file and never with household data, and
# `test_api_gating.py::test_the_spa_fallback_does_not_answer_for_the_api_namespace` is what holds
# that.
#
# IT IS SUBTRACTED IN THE ASSERTION THAT FIRES, which is `_unnamed` below and was the staleness
# check. That was the wrong side of the rule twice over. The staleness check iterates ANONYMOUS, and
# no key of this dict is a key of ANONYMOUS, so `key not in CONFIGURED_SURFACES` was a predicate
# that could not be false -- a `check(True, ...)` in a milestone whose own records name that as a
# defect. Meanwhile the app that SHIPS registers this route: `ops/backend.Dockerfile` sets
# `SPIELPLAN_STATIC_DIR=/app/static` and its own comment calls it "the anonymous SPA fallback", so
# the exhaustiveness rule below was proved of the ninety-one routes pytest builds and would have
# FAILED on the ninety-two the container runs. The dict is the allow-list entry that makes it
# ninety-two routes named with a reason. `test_route_inventory.py:351` already writes its SPA
# exemption in this position, `<= {SPA_FALLBACK}`; this is the same shape.
# [M4.16 cycle 1, M416-REV-ARCH13-02]
CONFIGURED_SURFACES = {
    ("GET", "/{path:path}"): "the SPA shell, present only when a static build is configured",
}


def _route_leaves(routes):
    """Every route object the app actually dispatches on, however deeply the routers nest.

    `Mount` with nothing under it is skipped: `/_app` is a `StaticFiles` mount, which exposes no
    `routes` and serves files rather than answers, and the rule here is about routes. A mount that
    DOES carry routes is recursed into, so mounting a sub-application cannot hide one.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _route_leaves(included.routes)
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from _route_leaves(nested)
            continue
        if isinstance(route, Mount):
            continue
        yield route


def _resolves(dependant, target) -> bool:
    """Whether `target` is anywhere in this route's dependency tree.

    Recursive because nothing declares `admin_user` directly: it depends on `active_user`, which
    depends on `current_user`, and a route asks only for `AdminUser`.
    """
    return any(sub.call is target or _resolves(sub, target) for sub in dependant.dependencies)


def _verdict(route) -> str:
    """One of `guarded`, `session-only` or `unguarded`, from the dependant and nothing else.

    A route with no dependant at all -- a plain Starlette `Route` or `WebSocketRoute` appended to
    `app.router.routes` -- is unguarded by construction: there is no declared dependency to read,
    and whatever it does in its own body is invisible to every walk in this suite.
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return "unguarded"
    if any(_resolves(dependant, getattr(deps, name)) for name in _GATES):
        return "guarded"
    if any(_resolves(dependant, getattr(deps, name)) for name in _SESSION_ONLY):
        return "session-only"
    return "unguarded"


def _route_verdicts(app) -> dict[tuple[str, str], str]:
    """`(method, path) -> verdict` for every route `app` registers.

    A WebSocket has no method, so it is keyed "WS" rather than dropped -- dropping it is precisely
    how the Tonight channel stayed invisible to two sweeps at once.
    """
    verdicts: dict[tuple[str, str], str] = {}
    for route in _route_leaves(app.routes):
        verdict = _verdict(route)
        methods = sorted(m for m in (getattr(route, "methods", None) or ()) if m in _METHODS)
        path = getattr(route, "path", "")
        for method in methods or ["WS"]:
            verdicts[(method, path)] = verdict
    return verdicts


def _named(keys) -> str:
    """A sorted `METHOD path` block, for a failure message someone can act on."""
    return "\n".join(f"    {method:<7} {path}" for method, path in sorted(keys))


def _unnamed(verdicts: dict[tuple[str, str], str]) -> set[tuple[str, str]]:
    """Routes that resolve no gate and that no allow-list names, in either configuration.

    One reading, so the rule cannot be true of the app pytest builds and false of the app the
    container runs -- which is what it was while the configured surface was exempt only from a
    check it could never reach. [M4.16 cycle 1, M416-REV-ARCH13-02]
    """
    unguarded = {key for key, verdict in verdicts.items() if verdict == "unguarded"}
    return unguarded - set(ANONYMOUS) - set(CONFIGURED_SURFACES)


def test_every_route_the_app_registers_is_behind_a_session_or_named_anonymous():
    """Section 3.2's session in front of every route, and section 3.1's lock in front of most.

    The claim is exhaustiveness, which is the half `test_api_gating.py` cannot make: it sweeps the
    routes that already declare a gate, so a route added with no `ActiveUser` is not a failure
    there but an absence. Here every route the app registers is classified, and anything that is
    neither gated nor named with a reason above fails. [M4.16 arch-13]
    """
    verdicts = _route_verdicts(create_app())
    assert verdicts, "the walk found no routes at all - it is measuring itself"

    # The one route this rule was written for, asserted by name: it is the route that used to
    # authenticate inside its own body, and it is the one every other walk in the suite loses.
    channel = ("WS", "/api/tonight/channel")
    assert verdicts.get(channel) == "guarded", (
        f"the Tonight channel is {verdicts.get(channel, 'not registered at all')}: the session "
        "socket is what the blind vote's integrity rests on, and a socket that authenticates in "
        "its own body is invisible to every dependency walk in this suite (decision 225)"
    )

    assert not _unnamed(verdicts), (
        "these routes resolve neither active_user nor admin_user and are not named anonymous:\n"
        + _named(_unnamed(verdicts))
        + "\nAdd the gate (section 3.2 puts every route behind a session), or - if a stranger who "
        "can reach the origin really may have this - name it in ANONYMOUS with the reason."
    )

    session_only = {key for key, verdict in verdicts.items() if verdict == "session-only"}
    assert not session_only - set(CURRENT_ONLY), (
        "these routes are behind a session but not behind section 3.1's first-login lock:\n"
        + _named(session_only - set(CURRENT_ONLY))
        + "\nThe set that may skip the lock is decision 179's ways out of it. Use ActiveUser, or "
        "name the route in CURRENT_ONLY with what makes it a way out."
    )


def test_neither_route_allow_list_outlives_the_routes_it_names():
    """An allow-list entry for a route that is now gated, or gone, is the rot this file is about.

    Kept apart from the test above so the two failures read differently: that one says a route
    escaped a rule, this one says a record outlived what it described. It reads ANONYMOUS and
    CURRENT_ONLY only: `CONFIGURED_SURFACES` names a route no pytest build registers, so holding
    its entry to this walk would report the configuration rather than the record. Its route half
    is held one test down, against the app that build produces; its REASON is held here, with the
    other two lists', because a reason is the same claim wherever the route lives.
    """
    verdicts = _route_verdicts(create_app())
    stale_anonymous = {key for key in ANONYMOUS if verdicts.get(key) != "unguarded"}
    assert not stale_anonymous, (
        "ANONYMOUS names routes that are no longer anonymous (gated since, or deleted):\n"
        + _named(stale_anonymous)
        + "\nRemove the entry. A list that outlives its routes is an allowance for the next one."
    )
    stale_current_only = {key for key in CURRENT_ONLY if verdicts.get(key) != "session-only"}
    assert not stale_current_only, (
        "CURRENT_ONLY names routes that no longer sit between the two gates:\n"
        + _named(stale_current_only)
        + "\nRemove the entry."
    )
    assert all(ANONYMOUS.values()) and all(CURRENT_ONLY.values()), (
        "an allow-list entry carries no reason - a list of paths records that a decision happened "
        "without recording what it was"
    )
    # THREE lists, since review cycle 4. `_unnamed` subtracts `CONFIGURED_SURFACES` in BOTH
    # configurations, so an entry in it silences the exhaustiveness rule exactly as an ANONYMOUS
    # entry does -- and nothing held one: this test read two lists and said so in its own
    # docstring, the ships test below asserted a single hard-coded key rather than the dict, and
    # the ascii test accepts an empty string. Measured: two reasonless entries silenced an
    # unguarded `POST /api/admin/export` and a WebSocket that opens the cookie in its own body --
    # decision 225's shape, of which this file is the only holder -- with all four rule-3 tests
    # green. [M4.16 cycle 1, M416-REV-ARCH13-02; M4.16 cycle 4, M416-C4-ARCH13-01]
    assert all(CONFIGURED_SURFACES.values()), (
        "a CONFIGURED_SURFACES entry carries no reason, and this is the list whose entries are "
        "hardest to re-derive: the route is not in the app a test run builds, so the sentence is "
        "the only record of which configuration registers it and why a stranger may have it"
    )


def test_the_rule_holds_the_app_that_ships_and_not_only_the_one_pytest_builds(tmp_path):
    """The ninety-second route, which no run of this suite had ever classified.

    `create_app()` appends the SPA fallback only when `SPIELPLAN_STATIC_DIR` names a built
    frontend. `ops/backend.Dockerfile` sets it, so the container registers a route that resolves no
    gate -- and the rule above was measured against the ninety-one routes pytest builds, where that
    route does not exist. `CONFIGURED_SURFACES` named it, but only inside a staleness check that
    iterates ANONYMOUS and so could never reach it: the app that ships would have FAILED the
    exhaustiveness rule while a dict in this file said the route was already accounted for.

    Both halves, because an exemption that names nothing is worse than none: the fallback must
    actually be there and actually be unguarded (otherwise the entry is the stale record the test
    above is about), and with it there the rule must still hold. The static build is a directory
    and an index file -- `test_api_gating.py` builds one the same way, and restores the variable
    and the settings cache in a `finally` for the same reason: this process is shared with every
    other test in the run. [M4.16 cycle 1, M416-REV-ARCH13-02]
    """
    build = tmp_path / "static"
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text("<html>shell</html>", encoding="utf-8")

    previous = os.environ.get("SPIELPLAN_STATIC_DIR")
    os.environ["SPIELPLAN_STATIC_DIR"] = str(build)
    settings.cache_clear()
    try:
        verdicts = _route_verdicts(create_app())
    finally:
        if previous is None:
            os.environ.pop("SPIELPLAN_STATIC_DIR", None)
        else:
            os.environ["SPIELPLAN_STATIC_DIR"] = previous
        settings.cache_clear()

    # EVERY entry, rather than the one key this test was written for. `_unnamed` subtracts the
    # whole dict, so a second entry was a permanent exemption held to nothing: not to the route
    # being registered, and not to that route still being unguarded. Both halves were measured
    # green before this -- an entry naming a route the configured app does not have, and an entry
    # for a route that has since been gated -- which is exactly the rot the staleness test above
    # refuses of the other two lists, absent from the third.
    #
    # Against the STATIC build because that is the configuration every entry names today. An entry
    # whose surface some other configuration registers arrives together with the build that
    # produces it, or it cannot be held here at all -- and an exemption nothing can hold is the
    # thing this test exists to refuse. [M4.16 cycle 4, M416-C4-ARCH13-01]
    stale = {
        key: verdicts.get(key, "not registered at all")
        for key in CONFIGURED_SURFACES
        if verdicts.get(key) != "unguarded"
    }
    assert not stale, (
        "CONFIGURED_SURFACES names routes the configured app does not register as unguarded, so "
        "the entry exempts something that no longer needs exempting:\n"
        + "\n".join(
            f"    {method:<7} {path}  ({verdict})"
            for (method, path), verdict in sorted(stale.items())
        )
        + "\nRemove the entry, or - if another configuration registers the route - build that "
        "configuration here too, so the entry is held by something."
    )
    assert not _unnamed(verdicts), (
        "the app that ships registers routes this rule names nowhere:\n" + _named(_unnamed(verdicts))
    )


# --- self-tests: rule 3 ---------------------------------------------------------------


def test_the_route_guard_reports_a_route_that_declares_no_gate():
    """A probe app with one of each, through `include_router` so the nesting is the real one.

    Four routes and exactly four rows: the count is asserted because the failure mode that matters
    here is not a misread verdict but a route the walk never yields, which is how the Tonight
    channel escaped `effective_candidates()`.
    """
    from fastapi import APIRouter, FastAPI, WebSocket
    from starlette.routing import WebSocketRoute

    router = APIRouter(prefix="/api")

    @router.get("/probe/open")
    async def _open() -> dict:
        return {}

    @router.get("/probe/gated")
    async def _gated(user: deps.ActiveUser) -> dict:
        return {"user": user.id}

    @router.websocket("/probe/socket")
    async def _socket(socket: WebSocket) -> None:
        await socket.close()

    async def _raw(socket: WebSocket) -> None:
        await socket.close()

    probe = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    probe.include_router(router)
    probe.router.routes.append(WebSocketRoute("/api/probe/raw", _raw))

    verdicts = _route_verdicts(probe)
    assert len(verdicts) == 4, verdicts
    assert verdicts[("GET", "/api/probe/gated")] == "guarded"
    assert verdicts[("GET", "/api/probe/open")] == "unguarded"
    # A socket with a dependant that resolves no gate, and a socket with no dependant at all.
    assert verdicts[("WS", "/api/probe/socket")] == "unguarded"
    assert verdicts[("WS", "/api/probe/raw")] == "unguarded"


def test_the_route_guard_tells_the_two_gates_apart():
    """`CurrentUser` is a session; `ActiveUser` is a session past section 3.1's lock.

    The distinction is the whole reason `CURRENT_ONLY` is a second list rather than three more
    entries in `ANONYMOUS`: a route on `CurrentUser` IS behind a session, so calling it anonymous
    would be false, and folding it in with the gated routes would let a fifth route join decision
    179's four without anyone noticing -- which is exactly how `POST /api/auth/reauth` once let a
    locked account stamp `admin_verified_at`.
    """
    from fastapi import APIRouter, FastAPI

    router = APIRouter(prefix="/api")

    @router.get("/probe/session")
    async def _session(user: deps.CurrentUser) -> dict:
        return {"user": user.id}

    @router.get("/probe/admin")
    async def _admin(user: deps.AdminUser) -> dict:
        return {"user": user.id}

    probe = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    probe.include_router(router)

    verdicts = _route_verdicts(probe)
    assert verdicts[("GET", "/api/probe/session")] == "session-only"
    assert verdicts[("GET", "/api/probe/admin")] == "guarded"


def test_the_route_failure_messages_are_ascii():
    """They print to a Windows cp1252 console, where one decorative glyph is a crash."""
    _named(set(ANONYMOUS) | set(CURRENT_ONLY)).encode("ascii")
    for reason in (*ANONYMOUS.values(), *CURRENT_ONLY.values(), *CONFIGURED_SURFACES.values()):
        reason.encode("ascii")


# --- M5.1 cycle 2: the routing note describes the tree it is read against ---------------------

# The implementation note above `ANONYMOUS` is the one paragraph in this file written to be acted
# on rather than read: it exists so the next author does not spend an afternoon re-deriving what
# `include_router` does under FastAPI 0.141, and it earns that by describing this tree concretely.
# It described it by counting it, and the count was the one `main` had. M5.1 mounted `acquisition`
# and `events`, so a reader who counts `app.routes` now finds a number the note does not have and
# is back to deciding whether the note is stale or whether the flattening changed under them -
# which IS the afternoon. Nothing read the number: it is a comment, and this file stayed green
# through the milestone that mounted both of them and edited this file for other reasons.
# [M5.1 review cycle 2, M51-REG-ROUTERS-03]
#
# Held to the live count rather than forbidden outright, for the reason the note is written at
# all - a concrete tree is what makes it checkable. Anchored on the NOUN, because the other counts
# in this file are the domain packages `ALLOWED_RESIDUE` is measured over and a rule that read
# every number here would be ruling on a different set; and read as a PLURAL, because "an
# `_IncludedRouter` object per mounted router" is a ratio rather than a tally and stays true as
# routers are added, which is the form the note now uses. No exemption for a dated measurement,
# unlike the registry's reader in `test_worker_schedule.py`: this note is an instruction about the
# tree the reader is looking at, and a note that was true two milestones ago is the note that
# costs the afternoon. The live count comes off `original_router`, the attribute `_route_leaves`
# already walks, rather than off an isinstance against a private FastAPI class this file would
# then have to import.
#
# WIDENED IN CYCLE 3, in both of the two ways the note escaped it. The narrow noun was chosen to
# keep the rule off `ALLOWED_RESIDUE`'s domain packages, and that reason still holds -- but a
# sentence that puts a count in front of the bare plural is a sentence about this set and no
# other, so the bare plural is read as well, and the one sentence here that counted a subset of
# them rather than all of them now names them instead. The other half was the pairing:
# `_comment_prose` yields a comment PER LINE, the note wrapped between "all twelve" and "routers
# by definition", and this reader consequently found NOTHING in the file it polices -- green over
# two sentences that both still said twelve after M5.1 had made that wrong, which is what a guard
# looks like once it has become a decoration. `_comment_paragraphs` glues the run back into the
# paragraph it was written as before the split into sentences, so the pair is read together.
# [M5.1 review cycle 3, M51-C3-REG-02]
_ROUTER_TALLY = re.compile(
    r"\b(\w+(?:-\w+)?)\s+(?:opaque\s+)?"
    r"(?:`?_IncludedRouter`?\s+objects|(?:included\s+)?routers)\b",
    re.IGNORECASE,
)


def _router_tallies(path: Path) -> list[tuple[int, str, str]]:
    """Every sentence of `path`'s prose that counts the mounted routers, and how it spells it."""
    found: list[tuple[int, str, str]] = []
    for line, text in _comment_paragraphs(path):
        for sentence in re.split(r"(?<=[.;]) ", text):
            for match in _ROUTER_TALLY.finditer(sentence):
                word = match.group(1).lower()
                if word.isdigit() or word in _COUNT_WORDS:
                    found.append((line, sentence.strip(), word))
    return found


def test_the_routing_note_counts_the_routers_this_app_actually_mounts():
    """The note is acted on by the next author, so it is held to the tree they will count.

    Asserted against `create_app()` rather than against `app.py`'s `include_router` lines, because
    the sentence is about what `app.routes` HOLDS: a FastAPI that resumed flattening would keep
    every one of those lines and make the note wrong in the other direction, which is the change
    the note exists to warn about and the one a source scan cannot see. The sentence is
    repeated back with `!a` rather than `!r` because it is somebody else's: this file's
    prose carries em dashes, and a failure has to print on a cp1252 console.
    """
    live = len([r for r in create_app().routes if getattr(r, "original_router", None) is not None])
    assert live, "the walk found no included routers at all - it is measuring itself"
    assert live < len(_COUNT_WORDS), (
        f"this app mounts {live} routers and the borrowed vocabulary spells as far as "
        f"{len(_COUNT_WORDS) - 1}: extend `_COUNT_WORDS` rather than leaving the word unread"
    )

    stale = [
        f"line {line} says {word!a} where the app mounts {live}: {sentence!a}"
        for line, sentence, word in _router_tallies(Path(__file__))
        if word not in {str(live), _COUNT_WORDS[live]}
    ]

    assert not stale, (
        "the note states a number of mounted routers that `create_app` does not mount, and it is "
        f"the note that saves the afternoon: say it per mounted router, or say today's: {stale}"
    )


def test_the_router_count_reader_reads_the_note_this_file_shipped(tmp_path):
    """The sentence that went stale, its spellings, the shape it wrapped into, and the ratio.

    The stale sentence is kept verbatim as a string literal, which is not prose: the guard above
    reads comments and docstrings, so it does not read its own fixture. The ratio is the shape the
    note now uses and the shape that must stay silent, or the repair reddens the guard that asked
    for it.

    The second and third blocks are cycle 3's, and they are the two shapes that escaped: a count
    in front of the bare plural, which the narrow noun did not read, and the same sentence broken
    across two `#` lines, which nothing read at all while the guard reported green. The line is
    asserted with the word because the glue has to report the run's FIRST line -- a failure that
    named the line the noun happens to sit on would send the next author to the wrong half of the
    sentence. [M5.1 review cycle 3, M51-C3-REG-02]
    """
    module = tmp_path / "note.py"
    module.write_text(
        "# `app.routes` holds twelve `_IncludedRouter` objects.\n"
        "# The walk sees twelve included routers.\n"
        "# It now holds an `_IncludedRouter` object per mounted router.\n"
        "\n"
        "# A later note says thirteen routers.\n"
        "\n"
        "# The composition root imports all fourteen\n"
        "# routers by definition.\n",
        encoding="utf-8",
    )

    found = _router_tallies(module)

    assert [(line, word) for line, _sentence, word in found] == [
        (1, "twelve"), (1, "twelve"), (5, "thirteen"), (7, "fourteen")
    ], f"the reader misses a count, misses a spelling of the noun, or reads the ratio: {found}"


# --- the sentences the map makes out of rules 2 and 3 ----------------------------------
#
# Both rows were drafted in `docs/milestones/M4.16-plan.md` before any of this was written, and
# both `what` fields shipped byte-identical to that draft. Each then described a rule narrower than
# the guard beside it -- rule 2's sentence carries no scope word at all, although `_domain_modules`
# walks `backend/spielplan` and nothing else, so twenty-three files under `ops/` and
# `backend/tests/` falsify it as written while importing from `spielplan.api` on purpose; and rule
# 3's sentence offered a two-way choice, gate or anonymous allow-list, over a classifier that has
# always had three verdicts and three lists. Decision 179's ways out of §3.1's first-login lock are
# the third class, and a reader who repaired the app to fit the sentence would delete them and
# brick a locked account.
#
# So the two sentences are held to the code they describe, in `_FAMILY_SIZE`'s idiom one file over:
# the names come off `_GATES`, `_SESSION_ONLY` and the three allow-lists rather than out of a
# second copy here, so a class added to the classifier reddens the sentence that stops describing
# it. Rule 1's row is M4.13's and is not this milestone's to restate, which is why only two rows
# are read here. This is the only rule in this file whose subject is a record rather than the
# package, and it is here rather than in `test_spec_coverage.py` because the record's subject is
# these guards.
# [decision 179; decision 225; M4.16 cycle 3: M416-C3-ARCH13-02, M416-C3-ARCH13-03]
MAP = Path(__file__).resolve().parent / "spec_coverage.toml"


def _what(row_id: str) -> str:
    rows = [r for r in tomllib.loads(MAP.read_text(encoding="utf-8"))["requirement"]
            if r["id"] == row_id]
    assert len(rows) == 1, f"the map holds {len(rows)} rows called {row_id!r}, not one"
    return rows[0]["what"]


def test_the_map_states_the_two_rules_these_guards_actually_enforce():
    """What the map promises a reader about rules 2 and 3, against what the walks above decide.

    Not a style rule. A `what` is the sentence an auditor checks the tree against, and both of
    these were false of the tree as written -- one too wide to be true, one too narrow to be the
    rule. The repair direction is the one M4-open-points.md:212-213 fixes: the app is right, the
    guards are right, and the record is what moves. [M4.16 cycle 3]
    """
    scope = PACKAGE.relative_to(Path(__file__).resolve().parents[2]).as_posix()
    imports = _what("platform-api-layer-holds-no-domain-rules")
    assert f"{scope}/" in imports, (
        f"the import rule's `what` claims no module outside `api/` imports from `spielplan.api`, "
        f"and the walk that proves it roots at {scope}/ -- so ops/ and backend/tests/ falsify the "
        "sentence and satisfy the rule. Say which tree the claim is about."
    )

    routes = _what("platform-every-route-is-behind-a-session")
    unnamed = [name for name in (*_GATES, *_SESSION_ONLY) if name not in routes]
    assert not unnamed, (
        f"the route rule's `what` never names {unnamed}, which `_verdict` resolves to decide a "
        "route's class. A sentence that omits a gate reads as forbidding it: `active_user_ws` is "
        "the Tonight channel's gate (decision 225) and `current_user` is decision 179's way out "
        "of the first-login lock, and both are the app as it ships."
    )
    lists = [name for name in ("ANONYMOUS", "CURRENT_ONLY", "CONFIGURED_SURFACES")
             if name not in routes]
    assert not lists, (
        f"the route rule's `what` names no {lists}, and the rule below refuses a route that is in "
        "none of them. An allow-list the record does not mention is an allowance a reader meets "
        "only by failing the guard."
    )
