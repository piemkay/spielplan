"""The numeric core's layering contract, as a test instead of a convention.

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
from pathlib import Path

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
