"""CLAUDE.md's layering rules as tests: `ledger/model.py` is numpy-only, no domain package imports
`spielplan.api`, the HTTP layer's raw SQL may not grow, and every route is behind a session."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from starlette.routing import Mount

from spielplan.api import deps
from spielplan.app import create_app

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"

# The files the numpy-only contract covers; add a new solver module here rather than widening `ALLOWED`.
GUARDED = ("ledger/model.py",)

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


def _package_of(relative: str) -> str:
    return ".".join(("spielplan", *Path(relative).parent.parts))


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """`from .refit import x` inside `spielplan.ledger` is an import of `spielplan.ledger.refit`."""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    base = ".".join(parts[: len(parts) - node.level + 1])
    return f"{base}.{node.module}" if node.module else base


def _imported_modules(source: str, *, package: str) -> set[str]:
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
    return sorted(m for m in _imported_modules(source, package=package) if not _permitted(m))


def test_the_ledger_model_imports_nothing_but_numpy_and_its_own_constants():
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


def _sql_strings(node: ast.AST):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield node.lineno, node.value
        return
    if isinstance(node, ast.JoinedStr):
        yield node.lineno, "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " "
            for v in node.values
        )
        for interpolated in node.values:
            if isinstance(interpolated, ast.FormattedValue):
                yield from _sql_strings(interpolated)
        return
    for child in ast.iter_child_nodes(node):
        yield from _sql_strings(child)


# A string is a statement when it OPENS with a verb, optionally behind its own SQL comments; a
# non-DML verb is read with the object it acts on, so "Drop one device" stays English.
_SQL_OBJECT = (
    r"(?:table|index|view|schema|sequence|function|trigger|type|extension|materialized\s+view)"
)
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

# The raw SQL statement count each HTTP-layer module may hold: a ceiling. A module not named here
# holds zero, and `app.py` is scanned because it declares `/api/health`.
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


def _is_api_module(module: str) -> bool:
    """`spielplan.api`, or anything under it. Not `spielplan.apis`, not `spielplan.api_shapes`."""
    return module == "spielplan.api" or module.startswith("spielplan.api.")


def _api_dependencies(source: str, *, package: str) -> list[str]:
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
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.relative_to(PACKAGE).parts[0] != "api"
        and path.relative_to(PACKAGE).as_posix() != "app.py"
    ]


def _residue() -> dict[str, int]:
    root = PACKAGE / "api"
    sources = {path.relative_to(root).as_posix(): path for path in sorted(root.rglob("*.py"))}
    sources["app.py"] = PACKAGE / "app.py"
    counts: dict[str, int] = {}
    for key, path in sources.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        counts[key] = sum(1 for _, text in _sql_strings(tree) if _SQL_HEAD.match(text))
    return counts


def test_no_domain_package_imports_from_the_api_layer():
    covered = {path.relative_to(PACKAGE).as_posix() for path in _domain_modules()}
    assert "push/send.py" in covered, "the scan does not reach push/send.py"

    offenders = {}
    for path in _domain_modules():
        relative = path.relative_to(PACKAGE).as_posix()
        hits = _api_dependencies(path.read_text(encoding="utf-8"), package=_package_of(relative))
        if hits:
            offenders[f"spielplan/{relative}"] = hits
    assert not offenders, (
        f"a domain package imports from the HTTP layer: {offenders}. `api/` may reach any domain "
        "package and none may reach back. Move what is wanted into the domain package that owns it "
        "(CLAUDE.md Conventions)."
    )


def test_the_api_layer_holds_no_more_raw_sql_than_it_did():
    measured = _residue()
    assert sum(measured.values()), "the scanner found no SQL in api/ at all - it measures itself"

    grown = {
        module: (count, ALLOWED_RESIDUE.get(module, 0))
        for module, count in measured.items()
        if count > ALLOWED_RESIDUE.get(module, 0)
    }
    assert not grown, (
        f"new raw SQL in the HTTP layer: {grown} (module: measured, allowed). A query is a rule "
        "about the data, so it belongs in the domain package that owns the rule; `api/` decides "
        "only HTTP shapes (CLAUDE.md Conventions)."
    )


# A WebSocket that authenticates in its own body counts as unguarded: nothing is in `route.dependant`.
# FastAPI 0.141 stops flattening `include_router`; `original_router.routes` carries the WebSocket.

_GATES = ("active_user", "admin_user", "active_user_ws", "active_user_brief")
_SESSION_ONLY = ("current_user",)

_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# Every route that answers a caller with no session, with what makes that the right answer.
ANONYMOUS = {
    ("POST", "/api/auth/login"): "the password door itself; a session is what it issues",
    ("POST", "/api/auth/passkey/login/options"): "the WebAuthn challenge the door needs first",
    ("POST", "/api/auth/passkey/login"): "the passkey door itself",
    ("POST", "/api/auth/logout"): "clears the cookie for whoever holds it, and grants nothing",
    ("GET", "/api/health"): "the container probe, answered before anyone can sign in",
    ("GET", "/api/config"): "the origin and whether a bundle exists, for the shell's first paint",
    ("POST", "/api/setup/admin"): "first boot has no account to authenticate as",
    ("GET", "/api/setup/state"): "whether this box still owes a wizard, cut to that one bit",
    # Token-authed in the handler body for the Jellyfin plugin (decision 332), so the dependant is empty.
    ("POST", "/events/jellyfin"): "token-authed for a server plugin that cannot hold a cookie",
}

# Behind a session but deliberately NOT behind `active_user`: decision 179's ways out of the lock.
CURRENT_ONLY = {
    ("GET", "/api/auth/me"): "a locked account must be able to see whose lock it is",
    ("POST", "/api/auth/password"): "the way out of the lock",
    ("POST", "/api/auth/switch"): "the shared-device chip, reachable while one profile is locked",
}


def _route_leaves(routes):
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
    return any(sub.call is target or _resolves(sub, target) for sub in dependant.dependencies)


def _verdict(route) -> str:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return "unguarded"
    if any(_resolves(dependant, getattr(deps, name)) for name in _GATES):
        return "guarded"
    if any(_resolves(dependant, getattr(deps, name)) for name in _SESSION_ONLY):
        return "session-only"
    return "unguarded"


def _route_verdicts(app) -> dict[tuple[str, str], str]:
    verdicts: dict[tuple[str, str], str] = {}
    for route in _route_leaves(app.routes):
        verdict = _verdict(route)
        methods = sorted(m for m in (getattr(route, "methods", None) or ()) if m in _METHODS)
        path = getattr(route, "path", "")
        for method in methods or ["WS"]:
            verdicts[(method, path)] = verdict
    return verdicts


def _named(keys) -> str:
    return "\n".join(f"    {method:<7} {path}" for method, path in sorted(keys))


def test_every_route_the_app_registers_is_behind_a_session_or_named_anonymous():
    verdicts = _route_verdicts(create_app())
    assert verdicts, "the walk found no routes at all - it is measuring itself"

    channel = ("WS", "/api/tonight/channel")
    assert verdicts.get(channel) == "guarded", (
        f"the Tonight channel is {verdicts.get(channel, 'not registered at all')}: the session "
        "socket is what the blind vote's integrity rests on (decision 225)"
    )

    unguarded = {key for key, verdict in verdicts.items() if verdict == "unguarded"}
    assert not unguarded - set(ANONYMOUS), (
        "these routes resolve neither active_user nor admin_user and are not named anonymous:\n"
        + _named(unguarded - set(ANONYMOUS))
        + "\nAdd the gate (§3.2 puts every route behind a session), or - if a stranger who "
        "can reach the origin really may have this - name it in ANONYMOUS with the reason."
    )

    session_only = {key for key, verdict in verdicts.items() if verdict == "session-only"}
    assert not session_only - set(CURRENT_ONLY), (
        "these routes are behind a session but not behind §3.1's first-login lock:\n"
        + _named(session_only - set(CURRENT_ONLY))
        + "\nThe set that may skip the lock is decision 179's ways out of it. Use ActiveUser, or "
        "name the route in CURRENT_ONLY with what makes it a way out."
    )


def test_neither_route_allow_list_outlives_the_routes_it_names():
    """A stale allow-list entry silently exempts whatever route is next written at that path."""
    verdicts = _route_verdicts(create_app())
    stale_anonymous = {key for key in ANONYMOUS if verdicts.get(key) != "unguarded"}
    assert not stale_anonymous, (
        "ANONYMOUS names routes that are no longer anonymous (gated since, or deleted):\n"
        + _named(stale_anonymous)
    )
    stale_current_only = {key for key in CURRENT_ONLY if verdicts.get(key) != "session-only"}
    assert not stale_current_only, (
        "CURRENT_ONLY names routes that no longer sit between the two gates:\n"
        + _named(stale_current_only)
    )
