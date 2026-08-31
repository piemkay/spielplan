"""The dev harness must not drift from the real API.

`ops/devstub.py` exists so the front end can be developed on a machine with no Postgres. That is
only safe while it answers the same paths as `spielplan.app`; a harness that quietly diverges
teaches the UI a contract the real backend does not honour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _paths(app) -> set[str]:
    return set(app.openapi()["paths"])


@pytest.fixture(scope="module")
def apps():
    sys.path.insert(0, str(ROOT))
    import importlib.util

    from spielplan.app import app as real

    spec = importlib.util.spec_from_file_location("devstub", ROOT / "ops" / "devstub.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed, which is what `import` itself does. Without it the
    # harness's Pydantic models cannot resolve their own annotations: `ops/devstub.py` carries
    # `from __future__ import annotations`, so every annotation is a string that Pydantic
    # resolves through `sys.modules[cls.__module__]` when FastAPI builds the schema — and this
    # module was not there. The symptom is a `class-not-fully-defined` error from `openapi()`
    # naming whichever model happens to be built first, which says nothing about the real
    # problem.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return real, module.app


def test_harness_covers_every_path_the_front_end_uses(apps):
    real, stub = apps
    real_paths = _paths(real)
    stub_paths = _paths(stub)

    # Paths the front end never calls in M0 scope; the harness may skip them.
    exempt = {
        "/api/auth/switch",
        "/api/auth/password",
        "/api/auth/pin",
        "/api/titles/{title_id}/similar-by-term",
        "/api/docs",
    }
    missing = real_paths - stub_paths - exempt
    assert not missing, f"dev harness is missing real routes: {sorted(missing)}"


def test_harness_invents_no_routes(apps):
    real, stub = apps
    invented = _paths(stub) - _paths(real)
    assert not invented, (
        "the dev harness answers paths the real app does not have — the UI would be built "
        f"against a contract that does not exist: {sorted(invented)}"
    )


def test_harness_uses_the_real_validator(apps):
    """The import report the harness renders must be produced by the real code, not mocked —
    otherwise the one screen that shows §4.1 enforcement would be theatre."""
    source = (ROOT / "ops" / "devstub.py").read_text(encoding="utf-8")
    assert "from spielplan.importer import bundle as bundle_import" in source
    assert "bundle_import.validate(" in source
