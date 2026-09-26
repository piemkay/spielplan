"""Whether this app can write its `/data` mounts (§2, §6.6). A probe file, not `os.access`, which is
wrong under ACLs, rootless Docker and read-only bind mounts. Never a boot refusal (§3.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

MOUNTS = ("raw", "artifacts", "cache", "import", "backups")
# The backend is not given raw or backups (`docker-compose.yml`'s `x-backend-volumes`).
BACKEND_MOUNTS = ("artifacts", "cache", "import")
WORKER_MOUNTS = MOUNTS

# Asked by permission only: a stray probe file would fail the importer's integrity pass, whose
# `rglob` includes dot files.
_BY_PERMISSION = frozenset({"import"})

PROBE_PREFIX = ".spielplan-probe-"

# The container user `ops/backend.Dockerfile` creates.
_OWNER = "1000:1000"


def writable(path: Path, *, by_permission: bool = False) -> str | None:
    """None when writable, else one sentence why. A missing directory is asked of its nearest
    existing parent, by permission."""
    try:
        if not path.exists():
            parent = next((p for p in path.parents if p.exists()), None)
            if parent is not None and os.access(parent, os.W_OK | os.X_OK):
                return None
            return f"{path} does not exist and cannot be created"
        if not path.is_dir():
            return f"{path} is not a directory"
    except OSError as exc:
        # `exists()` raises for an unsearchable parent; report it rather than crash a boot.
        return f"{path} cannot be inspected by this app ({type(exc).__name__}: {exc.strerror or exc})"
    if by_permission:
        if os.access(path, os.W_OK | os.X_OK):
            return None
        return f"{path} is not writable by this app (permission denied)"
    try:
        handle, name = tempfile.mkstemp(dir=path, prefix=PROBE_PREFIX)
    except OSError as exc:
        return f"{path} is not writable by this app ({type(exc).__name__}: {exc.strerror or exc})"
    os.close(handle)
    with contextlib.suppress(OSError):
        os.unlink(name)
    return None


def probe(data_dir: Path, names: Iterable[str]) -> dict[str, str | None]:
    return {
        name: writable(data_dir / name, by_permission=name in _BY_PERMISSION) for name in names
    }


def unwritable(result: Mapping[str, str | None]) -> list[str]:
    return [name for name, problem in result.items() if problem]


def remedy(names: Iterable[str]) -> str:
    return f"sudo chown -R {_OWNER} " + " ".join(f"data/{name}" for name in names)


def refusal(result: Mapping[str, str | None]) -> str | None:
    """ASCII only: it reaches container logs and `job_run` verbatim."""
    bad = unwritable(result)
    if not bad:
        return None
    reasons = "; ".join(str(result[name]) for name in bad)
    return (
        f"{reasons}. This app runs as uid 1000 and has to write there, so give it the "
        f"directories: on the host, in the Spielplan directory, run `{remedy(bad)}`"
    )


class Watch:
    """Never waits on disk inside the health route's 2 s budget: a stale result starts a threaded
    re-probe whose answer the next read gets."""

    def __init__(self, data_dir: Path, names: Iterable[str], *, ttl: float = 60.0) -> None:
        self._data_dir = data_dir
        self._names = tuple(names)
        self._ttl = ttl
        self.result = probe(data_dir, self._names)
        self._at = time.monotonic()
        self._task: asyncio.Task | None = None

    def current(self) -> dict[str, str | None]:
        stale = time.monotonic() - self._at > self._ttl
        if stale and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._refresh())
        return self.result

    async def _refresh(self) -> None:
        try:
            self.result = await asyncio.to_thread(probe, self._data_dir, self._names)
        finally:
            self._at = time.monotonic()

    def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
