"""Invariants in files with no runtime: CPU-only torch, one process, one plain-HTTP port, the
/data bind mounts, the frozen rating_source ids, and member copy free of spec references."""

from __future__ import annotations

import ast
import html
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend" / "src"
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "ops" / "backend.Dockerfile"
PYPROJECT = REPO / "backend" / "pyproject.toml"
MIGRATIONS = REPO / "backend" / "migrations"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# Read by block structure, so a commented-out mount or port is absent.
# `test_worker_registry.py` imports these readers.

# §1 names /data/pg, /data/raw, /data/artifacts and /data/cache; §2's nightly pg_dump adds
# /data/backups.
SPEC_VOLUMES = {
    "/var/lib/postgresql/data", "/data/raw", "/data/artifacts", "/data/cache", "/data/backups",
}

_MOUNT = re.compile(
    r"^\s*-\s*(?P<host>[^\s:]+):(?P<container>/[^\s:]*)(?::(?P<mode>[A-Za-z,]+))?\s*$", re.M
)


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _nested(text: str, key: str) -> str:
    """The lines nested under `key:`, by indentation, with comments removed (header included)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        head = line.split("#", 1)[0].rstrip()
        if head.strip() != f"{key}:" and not head.strip().startswith(f"{key}: "):
            continue
        indent = len(head) - len(head.lstrip())
        body = [head]
        for follow in lines[i + 1:]:
            if follow.strip() and len(follow) - len(follow.lstrip()) <= indent:
                break
            body.append(follow.split("#", 1)[0].rstrip())
        return "\n".join(body)
    return ""


def _service_names(compose: str) -> list[str]:
    assert "\nservices:\n" in compose, "docker-compose.yml declares no services at all"
    body = compose[compose.index("\nservices:\n") + 1:]
    return re.findall(r"^  ([A-Za-z][\w-]*):\s*$", body, re.M)


def _service(compose: str, name: str) -> str:
    """One service's block, from its own key to the next service's."""
    body = compose[compose.index("\nservices:\n") + 1:]
    blocks = list(re.finditer(r"^  (?P<name>[A-Za-z][\w-]*):\s*$", body, re.M))
    for i, match in enumerate(blocks):
        if match.group("name") != name:
            continue
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(body)
        return body[match.start():end]
    raise AssertionError(f"docker-compose.yml declares no `{name}` service")


def _anchor(compose: str, name: str) -> str:
    """The block a `&name` anchor holds."""
    match = re.search(rf"^(?P<key>[A-Za-z][\w-]*): &{re.escape(name)}\s*$", compose, re.M)
    assert match, f"docker-compose.yml defines no `&{name}` anchor"
    return _nested(compose[match.start():], match.group("key"))


def _mounts(compose: str, service: str) -> set[tuple[str, str]]:
    """(container path, access mode) for every bind mount `service` actually receives."""
    block = _nested(_service(compose, service), "volumes")
    alias = re.search(r"volumes:\s*\*([\w-]+)", block)
    if alias:
        block = _anchor(compose, alias.group(1))
    return {(m.group("container"), m.group("mode") or "rw") for m in _MOUNT.finditer(block)}


def _published_ports(compose: str) -> list[str]:
    """Every port this stack publishes to the host, in both YAML sequence spellings."""
    entries: list[str] = []
    for name in _service_names(compose):
        body = _nested(_service(compose, name), "ports").partition("ports:")[2]
        if "[" in body:
            assert "]" in body, f"the `{name}` service's ports sequence is never closed"
            inner = body[body.index("[") + 1:body.rindex("]")]
            entries += [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
            continue
        for line in body.splitlines():
            item = line.strip()
            if item.startswith("- "):
                entries.append(item[2:].strip().strip("\"'"))
    return entries


def test_the_app_publishes_one_plain_http_port_and_terminates_no_tls():
    """§2: plain HTTP on one internal port; the operator's Traefik terminates TLS."""
    compose = _compose()
    published = _published_ports(compose)
    assert len(published) == 1, f"expected exactly one published app port, found {published}"
    assert published[0].endswith(":8080"), (
        f"the one published port must reach the app's internal 8080, not {published[0]}"
    )
    assert "443" not in compose
    for tls in ("letsencrypt", "certresolver", "ssl_certificate", "traefik.http.routers"):
        assert tls not in compose.lower(), f"the app must not configure TLS itself ({tls})"


def test_every_data_volume_the_spec_names_is_mounted():
    """A missing mount is data that does not survive a container replacement."""
    compose = _compose()
    mounted = {path for name in _service_names(compose) for path, _ in _mounts(compose, name)}
    missing = SPEC_VOLUMES - mounted
    assert not missing, f"not mounted anywhere in the stack: {sorted(missing)}"


def _ignored(dockerignore: str) -> set[str]:
    """The patterns `.dockerignore` applies. Imported by `test_release_gate.py`."""
    return {
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_the_image_pulls_torch_from_the_cpu_index_only():
    """Torch is pinned to an `explicit` CPU index in pyproject.toml, and no Dockerfile index strategy
    picks a winner by version."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    uv = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {})

    cpu = [
        entry.get("name")
        for entry in uv.get("index", [])
        if entry.get("url", "").rstrip("/") == "https://download.pytorch.org/whl/cpu"
    ]
    assert cpu, "backend/pyproject.toml declares no CPU-only pytorch index"
    named = {entry.get("name"): entry for entry in uv.get("index", [])}
    assert named[cpu[0]].get("explicit") is True, (
        f"index `{cpu[0]}` must be `explicit`, or it competes with PyPI for every package it carries"
    )
    assert uv.get("sources", {}).get("torch", {}).get("index") == cpu[0], (
        f"torch must be sourced from `{cpu[0]}`, not merely offered it as an extra index"
    )
    assert "unsafe-best-match" not in dockerfile, (
        "an index strategy picks the highest version across indexes, which is the CPU wheel only "
        "while the CPU index is ahead of PyPI"
    )
    for cuda in ("cu118", "cu121", "cu124", "nvidia", "--gpus"):
        assert cuda not in dockerfile.lower(), f"the image must stay CPU-only ({cuda})"
        assert cuda not in pyproject_text.lower(), f"the dependency spec must stay CPU-only ({cuda})"


def _final_stage(dockerfile: str) -> str:
    """The stage the container runs: its `CMD` comes from the last `FROM` alone."""
    stages = list(re.finditer(r"^FROM\s", dockerfile, re.M))
    return dockerfile[stages[-1].start():] if stages else dockerfile


def _multi_worker_reason(dockerfile: str) -> str | None:
    """Why this image would start more than one application process, or None."""
    if re.search(r"\bgunicorn\b", dockerfile):
        return "a gunicorn wrapper forks workers"
    if "WEB_CONCURRENCY" in dockerfile:
        return "WEB_CONCURRENCY is uvicorn's own multi-worker setting"
    command = re.search(r"^CMD\s+(?P<argv>.+)$", _final_stage(dockerfile), re.M)
    if command is None:
        return "no CMD at all"
    if re.search(r"--workers\b|(?<![\w-])-w(?![\w-])", command.group("argv")):
        return f"the CMD asks for workers: {command.group('argv')}"
    return None


def test_the_image_starts_exactly_one_application_process():
    """The rail, the lobby hub and the push set are process-global, so `--workers 2` splits them.
    `app.py` refuses at boot for settings this file cannot see."""
    reason = _multi_worker_reason(DOCKERFILE.read_text(encoding="utf-8"))
    assert reason is None, f"ops/backend.Dockerfile starts more than one process: {reason}"


def test_frozen_rating_source_ids_match_the_spec():
    from spielplan.importer.validate import FROZEN_RATING_SOURCE_IDS

    assert {1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31} == FROZEN_RATING_SOURCE_IDS
    ddl = (MIGRATIONS / "0003_content.sql").read_text(encoding="utf-8")
    assert "CHECK (id IN (1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31))" in ddl


# Decision 486: members never see "§N", "decision N", "proposal N" or M0-M7. Read: every route
# but `admin/` and `setup/` and what they import. Not read: comments, styles, docstrings, logger
# arguments, OpenAPI `description=`, SQL comments.

_SPEC_REFERENCE = re.compile(
    r"§\s?\d+(?:\.\d+)*|\b(?:[Dd]ecision|[Pp]roposal)s?\s+\d+|\bM[0-7](?:\.\d+)?\b"
)
_OPERATOR_ROUTES = ("admin", "setup")
_LOCAL_IMPORT = re.compile(r"""\b(?:from|import)\s*['"](\$lib/[^'"]+|\.{1,2}/[^'"]+)['"]""")
_SVG_GEOMETRY = re.compile(r"""\s(?:d|points)\s*=\s*(?:"[^"]*"|'[^']*'|\{[^{}]*\})""")
_STYLE_ATTRIBUTE = re.compile(r"""\sstyle\s*=\s*(?:"[^"]*"|'[^']*')""")
_PATH_DATA = re.compile(r"\s*[Mm][MmLlHhVvCcSsQqTtAaZz\d.,\s+-]*")
_PATH_NUMBER = re.compile(r"\d*\.?\d+")
_LOGGER_CALLS = {"debug", "info", "warning", "error", "exception", "critical"}
_SQL_COMMENT = re.compile(r"--[^\n]*")
_JS_WORD = re.compile(r"[\w$]+")

_SPIELPLAN = REPO / "backend" / "spielplan"
_MEMBER_COPY_PACKAGES = ("home", "rate", "rank", "taste", "tonight")
_MEMBER_ROUTERS = ("home", "library", "rank", "rate", "taste", "tonight")
_MEMBER_COPY_CONSTANTS = {"api/deps.py": ("RESTART_REQUIRED", "RESTORE_REQUIRED")}


def _docstrings(tree: ast.AST) -> set[int]:
    """`id()` of every docstring constant: an `ast.Expr` heading a module/class/function body."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
            continue
        if isinstance(first.value.value, str):
            out.add(id(first.value))
    return out


def _is_path_data(text: str) -> bool:
    return bool(_PATH_DATA.fullmatch(text)) and len(_PATH_NUMBER.findall(text)) >= 2


def _blank(match: re.Match[str]) -> str:
    """A removed span, kept as its newlines so every later line number still points at its line."""
    return "\n" * match.group(0).count("\n")


def _js_literals(source: str) -> list[tuple[int, str]]:
    """A lexer, not a pattern: `//` inside a string and an apostrophe inside a comment must not
    confuse it. A `/` opens a regex where an operand is expected."""
    out: list[tuple[int, str]] = []
    templates: list[int] = []  # the brace depth each open `${` returns to
    depth = 0
    operand_expected = True
    i, n = 0, len(source)

    def line(at: int) -> int:
        return source.count("\n", 0, at) + 1

    def template_text(start: int) -> int:
        """Read template text from `start` to the closing backtick or the next `${`."""
        j = start
        while j < n and source[j] != "`" and not source.startswith("${", j):
            j += 2 if source[j] == "\\" else 1
        out.append((line(start), source[start:j]))
        if j < n and source[j] == "`":
            return j + 1
        templates.append(depth)
        return j + 2

    while i < n:
        char = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char == "/" and operand_expected:
            j, in_class = i + 1, False
            while j < n and source[j] != "\n" and (in_class or source[j] != "/"):
                if source[j] == "\\":
                    j += 1
                elif source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                j += 1
            i, operand_expected = j + 1, False
        elif char in "'\"":
            j = i + 1
            while j < n and source[j] not in (char, "\n"):
                j += 2 if source[j] == "\\" else 1
            out.append((line(i), source[i + 1:j]))
            i, operand_expected = j + 1, False
        elif char == "`":
            i, operand_expected = template_text(i + 1), False
        elif char == "}" and templates and templates[-1] == depth:
            templates.pop()
            i, operand_expected = template_text(i + 1), False
        elif char.isalpha() or char in "_$":
            word = _JS_WORD.match(source, i).group(0)
            operand_expected = word in {"return", "typeof", "case", "in", "of", "void", "throw", "else"}
            i += len(word)
        else:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            if not char.isspace():
                operand_expected = char not in ")]" and not char.isdigit()
            i += 1
    return out


def _references(text: str) -> list[str]:
    return [found.group(0) for found in _SPEC_REFERENCE.finditer(text)]


def _frontend_register_leaks(path: Path) -> list[str]:
    """Every spec or milestone reference a member-facing Svelte or JavaScript file can render."""
    source = _src(path)
    label = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name
    leaks: list[str] = []
    scripts = [(source.count("\n", 0, m.start(1)), m.group(1))
               for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", source, re.S)]
    if path.suffix != ".svelte":
        scripts = [(0, source)]
    for offset, script in scripts:
        for line, text in _js_literals(script):
            if _is_path_data(text):
                continue
            leaks += [f"{label}:{offset + line}: {ascii(ref)} in {ascii(text.strip()[:90])}"
                      for ref in _references(text)]
    if path.suffix == ".svelte":
        markup = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", _blank, source,
                        flags=re.S)
        markup = _STYLE_ATTRIBUTE.sub(_blank, _SVG_GEOMETRY.sub(_blank, markup))
        markup = html.unescape(markup)
        for found in _SPEC_REFERENCE.finditer(markup):
            line = markup.count("\n", 0, found.start()) + 1
            context = markup[max(0, found.start() - 50):found.end() + 30].split("\n")
            near = " ".join(part.strip() for part in context)
            leaks.append(f"{label}:{line}: {ascii(found.group(0))} in the markup, near {ascii(near)}")
    return leaks


def _member_sources(routes: Path = FRONTEND / "routes", lib: Path = FRONTEND / "lib") -> list[Path]:
    """The member routes and every module they import, transitively (decision 486 clause 1)."""
    todo = [
        path.resolve() for path in routes.rglob("*")
        if path.is_file() and path.suffix in {".svelte", ".js"} and not path.name.endswith(".test.js")
        and path.relative_to(routes).parts[0] not in _OPERATOR_ROUTES
    ]
    seen: set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for spec in _LOCAL_IMPORT.findall(_src(path)):
            base = lib / spec[len("$lib/"):] if spec.startswith("$lib/") else path.parent / spec
            for candidate in (base, base.with_name(base.name + ".js")):
                if candidate.is_file() and candidate.suffix in {".svelte", ".js"}:
                    todo.append(candidate.resolve())
                    break
    return sorted(seen)


def _python_register_leaks(path: Path, names: tuple[str, ...] | None = None) -> list[str]:
    """`names` narrows a module to those constants, for a module whose other strings are admin copy."""
    tree = ast.parse(_src(path))
    skip = _docstrings(tree)
    roots: list[ast.AST] = [tree]
    if names is not None:
        roots = [
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
        ]
        assert len(roots) == len(names), f"{path.name} no longer defines all of {names}"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        exempt = [kw.value for kw in node.keywords if kw.arg == "description"]
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in _LOGGER_CALLS
                and isinstance(func.value, ast.Name) and func.value.id in {"log", "logger"}):
            exempt += [*node.args, *(kw.value for kw in node.keywords)]
        skip |= {id(sub) for arg in exempt for sub in ast.walk(arg)}
    label = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name
    return [
        f"{label}:{sub.lineno}: {ascii(ref)} in {ascii(sub.value.strip()[:90])}"
        for root in roots
        for sub in ast.walk(root)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in skip
        for ref in _references(_SQL_COMMENT.sub("", sub.value))
    ]


def _member_copy_modules() -> list[tuple[Path, tuple[str, ...] | None]]:
    modules: list[tuple[Path, tuple[str, ...] | None]] = [
        (path, None) for package in _MEMBER_COPY_PACKAGES
        for path in sorted((_SPIELPLAN / package).glob("*.py"))
    ]
    modules += [(_SPIELPLAN / "api" / f"{name}.py", None) for name in _MEMBER_ROUTERS]
    modules += [(_SPIELPLAN / rel, names) for rel, names in _MEMBER_COPY_CONSTANTS.items()]
    return modules


def test_member_surfaces_render_no_spec_or_milestone_reference():
    """The walk is asserted before the scan: a scan over nothing passes."""
    sources = _member_sources()
    read = {path.relative_to(FRONTEND).as_posix() for path in sources}
    expected = {
        "routes/+layout.svelte", "routes/+page.svelte", "routes/account/+page.svelte",
        "routes/rate/+page.svelte", "routes/rank/+page.svelte", "routes/tonight/+page.svelte",
        "lib/components/TitleDetail.svelte", "lib/rate.svelte.js", "lib/tonight.svelte.js",
    }
    assert expected <= read, f"the import walk no longer reaches {sorted(expected - read)}"
    operator = sorted(p for p in read if p.startswith(("routes/admin/", "routes/setup/")))
    assert not operator, f"the walk read operator routes as member surfaces: {operator}"

    leaks = [leak for path in sources for leak in _frontend_register_leaks(path)]
    leaks += [leak for path, names in _member_copy_modules()
              for leak in _python_register_leaks(path, names)]
    assert not leaks, (
        "a member surface renders a spec reference or a milestone label, which decision 486 "
        "clause 2 forbids whether or not Show the model is on. Move the citation into a comment "
        "and say the thing itself in the member's words:\n  " + "\n  ".join(leaks)
    )


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("Text.svelte", "<p>Random pairs (§6.3) teach it fastest.</p>\n"),
        ("Attr.svelte", '<button title="an admin links one in Admin (M1)">Play</button>\n'),
        ("Entity.svelte", "<p>see &sect;6.1</p>\n"),
        ("Expr.svelte", "<span>{built ? '' : 'arrives with M6'}</span>\n"),
        ("Script.svelte", "<script>\n  const note = 'as decision 170 says';\n</script>\n<p>{note}</p>\n"),
        ("Point.svelte", "<p>Shipped in M4.9</p>\n"),
        ("store.svelte.js", "export const REASON = `from proposal 22 ${n} ratings`;\n"),
        ("inner.svelte.js", "const s = `${ok ? 'fine' : 'see §7.3'}`;\n"),
        ("url.svelte.js", "const u = 'https://example.org/'; const s = 'see §7.3';\n"),
        ("aside.svelte.js", "// the card's own copy\nconst s = 'decision 4 says so';\n"),
    ],
)
def test_the_member_register_guard_catches_a_real_violation(tmp_path, name, source):
    """Each way a reference reaches the screen, so the guard above cannot pass by reading nothing."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    assert _frontend_register_leaks(path), f"the guard did not see the reference in {source!r}"
