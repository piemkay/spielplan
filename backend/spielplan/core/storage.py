"""Whether this app can write where it has to. Spec v2.1 §2 (the `/data` mounts, Backups), §6.6
System, §10 step 1.

The first household install met every unwritable mount one job at a time and never at boot: the
nightly dump failed on `/data/backups/...dump.partial`, the bundle import failed twenty seconds
into the worker on `/data/artifacts/<version>` after validation had said "ok", and the worker's
heartbeat warned once into a log nobody was reading. No component asked the question up front, so
the answer arrived as three unrelated failures, and the cause - host directories nobody had handed
to the uid the containers run as - was in none of them. [C10.2; owner instruction of 2026-09-25
after the first household user test]

A PROBE AND NOT `os.access` where a file can be written, because `os.access` answers from the mode
bits and is wrong under ACLs, rootless Docker and read-only bind mounts - the cases a household box
actually has. The probe creates one dot-prefixed file and removes it in the same call, so nothing
the rotation (`backup/nightly.py`'s `spielplan-*.dump` glob) or a bundle-version listing reads can
ever see it.

Never a refusal to boot: §3.1 keeps a half-configured boot legal, and an app that will not start is
an app whose admin pages cannot tell the operator what is wrong. Each caller decides what an
unwritable mount means for it - a log line at boot, a failed `storage-check` job on §6.6's System
card, a field on `/api/health`, and a refusal at the one place a write is about to be committed to
(`importer/bundle.refuse_on_install_state`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

# §2's five bind mounts under DATA_DIR, by the name `docker-compose.yml` gives each on the host.
MOUNTS = ("raw", "artifacts", "cache", "import", "backups")
# `docker-compose.yml`'s `x-backend-volumes`: the backend is not given raw or backups at all, so it
# can only answer for these three and the worker answers for all five.
BACKEND_MOUNTS = ("artifacts", "cache", "import")
WORKER_MOUNTS = MOUNTS

# Asked by permission only, never by writing into it. `importer/validate.py`'s integrity pass walks
# the bundle root with `rglob("*")` - dot files included - and fails any file BUNDLE.json does not
# list, and the bundle root IS `/data/import` on the live install and in decision 257's layout. A
# probe file that overlapped a validation, or one a crash left behind, would fail every import with
# a misleading integrity finding. The app writes here only when it unpacks an archive, and that
# refusal already reaches the Data tab as a 400 naming the path.
_BY_PERMISSION = frozenset({"import"})

PROBE_PREFIX = ".spielplan-probe-"

# The container user `ops/backend.Dockerfile` creates, and README's own repair for the ownership
# mistake. Named once so every sentence that gives the command gives the same one.
_OWNER = "1000:1000"


def writable(path: Path, *, by_permission: bool = False) -> str | None:
    """None when this process can create a file in `path`, otherwise one sentence saying why not.

    A directory that does not exist yet is not a failure: the importer and the dump both create
    theirs, so what can refuse is the nearest parent that does exist, and that is asked by
    permission rather than by writing next to a mount this app does not own.
    """
    try:
        if not path.exists():
            parent = next((p for p in path.parents if p.exists()), None)
            if parent is not None and os.access(parent, os.W_OK | os.X_OK):
                return None
            return f"{path} does not exist and cannot be created"
        if not path.is_dir():
            return f"{path} is not a directory"
    except OSError as exc:
        # `exists()` raises rather than answering False for a parent this process cannot search,
        # and a probe must report that rather than take a boot or a health check down with it.
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
    """Each named mount under `data_dir`, mapped to None or the reason it cannot be written."""
    return {
        name: writable(data_dir / name, by_permission=name in _BY_PERMISSION) for name in names
    }


def unwritable(result: Mapping[str, str | None]) -> list[str]:
    return [name for name, problem in result.items() if problem]


def remedy(names: Iterable[str]) -> str:
    """README's chown, narrowed to the directories that need it."""
    return f"sudo chown -R {_OWNER} " + " ".join(f"data/{name}" for name in names)


def refusal(result: Mapping[str, str | None]) -> str | None:
    """The one sentence an operator can act on, or None when every mount is writable.

    ASCII only: it is written to a container log a Windows console may print, and into `job_run`,
    which the System card renders as it is.
    """
    bad = unwritable(result)
    if not bad:
        return None
    reasons = "; ".join(str(result[name]) for name in bad)
    return (
        f"{reasons}. This app runs as uid 1000 and has to write there, so give it the "
        f"directories: on the host, in the Spielplan directory, run `{remedy(bad)}`"
    )


class Watch:
    """The backend's own mounts, for `/api/health`, re-probed off the request.

    The health route answers inside a 2 s budget on the one event loop (`app._HEALTH_TIMEOUT_S`),
    so it never waits for a disk: it reads the last result, and a result older than `ttl` starts
    one re-probe in a thread whose answer the NEXT read gets. An operator who runs the chown sees
    the field turn within a minute and a probe per health check is never paid.
    """

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
