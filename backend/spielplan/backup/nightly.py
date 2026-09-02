"""§2's nightly database dump. Spec v2.1 §2, §5.3, §14.3.

§2, in full: "nightly `pg_dump` to `/data/backups`, rotation 14; the artifact bundle and raw
store are already immutable files. Dumps contain ciphertext only — back up the env file
(`SECRETS_KEY`) alongside them, or a restored dump cannot decrypt connector config."

The ciphertext half is not this module's work and deliberately so: connector secrets are sealed
by `core/secrets.py` before they ever reach a row, so every dump of every format is ciphertext
by construction rather than by a flag somebody could forget. What this module owes is the file,
in the right place, and the retention — and the retention is the part with teeth, because it
deletes.

**Where `pg_dump` comes from, and why.** The app image carries `postgresql-client-16` and the
worker runs the binary itself (ops/backend.Dockerfile). The alternative was to drive the dump
through the `db` service, which already mounts the same host directory at `/backups`, and it
was rejected for three reasons. It would need the worker to reach the Docker daemon — a socket
mount that hands root on the host to a process whose whole threat model (§14.3) is that the
credential it holds is admin-equivalent, which is a far larger blast radius than a client
package. The rotation, the report and §6.6's "backup status" line all live in the app, and a job
that cannot see the file it wrote cannot prune it or report on it. And `DATABASE_URL` is
configuration (§2): an install pointing at a Postgres outside this compose file would have no
`db` service to drive at all, and would silently have no backups.

The client is pinned to 16 to match §1's server pin. `pg_dump` refuses to dump a server newer
than itself, so a client one major behind is not a degraded backup, it is no backup.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from spielplan.core.config import settings

# §2: "rotation 14".
KEEP = 14

# The dump binary, as a command rather than a bare name, so a development box with no libpq
# client can point the tests at the one living in the database container.
PG_DUMP: tuple[str, ...] = ("pg_dump",)

PREFIX = "spielplan-"
SUFFIX = ".dump"
# The dump is written here first. A dump interrupted mid-write is a file of the right name and
# the wrong length, and rotation counts files — so a truncated dump would occupy one of the
# fourteen slots and look like a backup for a fortnight.
PARTIAL = ".partial"


@dataclass(frozen=True)
class BackupReport:
    path: Path
    bytes: int
    pruned: tuple[str, ...]
    kept: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "kept": self.kept,
            "pruned": list(self.pruned),
        }


def backups_dir() -> Path:
    """§2's target. `/data/backups` is mounted on backend and worker (docker-compose.yml)."""
    return settings().data_dir / "backups"


def dump_name(now: datetime) -> str:
    """UTC and second-resolution, so the name sorts chronologically as a string.

    Rotation orders by name rather than by mtime on purpose: an operator who copies the
    directory off-box and back rewrites every mtime, and the fourteen kept would then be
    whichever fourteen the copy happened to touch last.
    """
    return f"{PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"


def dumps(directory: Path) -> list[Path]:
    """This job's own dumps, oldest first. Nothing else in the directory is ours."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if p.is_file())


def interrupted(directory: Path) -> list[Path]:
    """The debris a killed `pg_dump` leaves: named by this job, never finished.

    `dump()` unlinks its own partial on a non-zero exit, but a SIGKILL, a container stop or a
    power cut inside the nightly window does not return from `subprocess.run` at all — and the
    file it leaves ends in `.partial`, so `dumps()`'s `*{SUFFIX}` glob never matches it again.
    """
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{PREFIX}*{SUFFIX}{PARTIAL}") if p.is_file())


def prune(directory: Path, keep: int = KEEP) -> list[str]:
    """§2's "rotation 14". Returns the names removed.

    Only files this job's own naming produced are candidates: `/data/backups` is a directory an
    operator can also put things in, and the copy someone saved before an upgrade is exactly the
    file they will want on the day this runs.

    Interrupted dumps are removed outright rather than counted against the fourteen: a truncated
    dump is not a backup, and one leaked per kill accumulates without bound in the directory §2
    asks the operator to copy off-box. Rotation is the only thing here that deletes, so it is the
    only place the cleanup can live — and it runs after `dump()` has renamed its own partial
    away, so what it finds is always somebody else's abandoned attempt.
    """
    existing = dumps(directory)
    doomed = existing[: max(0, len(existing) - keep)] + interrupted(directory)
    for path in doomed:
        path.unlink()
    return [path.name for path in doomed]


def _connection(database_url: str) -> tuple[str, dict[str, str]]:
    """Split `DATABASE_URL` into a DSN safe to put on argv, and the environment to run under.

    §14.3 is about the Jellyfin key, but its reasoning is about custody, and the database
    superuser credential this appliance holds is the other admin-equivalent secret in the file
    §2 tells the operator to back up. A command line is not private: `/proc/<pid>/cmdline` is
    world-readable, so every `ps` on the host — and every process listing in a support log —
    reads the password out of a nightly job that runs unattended. libpq's own answer is
    `PGPASSWORD`, which is why it exists.

    The value is percent-decoded on the way out: libpq decodes a URI's password field, so a
    password containing `/` or `@` — which must be encoded to survive the URL at all — would
    otherwise be handed to the server verbatim and fail to authenticate.
    """
    parts = urlsplit(database_url)
    if not parts.password:
        return database_url, {}
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}@{host}" if parts.username else host
    return urlunsplit(parts._replace(netloc=netloc)), {"PGPASSWORD": unquote(parts.password)}


def dump(database_url: str, path: Path) -> int:
    """Run `pg_dump` into `path`, atomically. Returns the size in bytes. Blocking.

    `--no-owner --no-privileges` because §2's restore target is a fresh `docker compose up`
    whose `POSTGRES_USER` the operator is free to have changed; ownership statements naming a
    role that install does not have turn a restore into a wall of errors.
    """
    partial = path.with_name(path.name + PARTIAL)
    dsn, credential = _connection(database_url)
    argv = [*PG_DUMP, "--format=custom", "--no-owner", "--no-privileges", dsn]
    with partial.open("wb") as handle:
        done = subprocess.run(
            argv, stdout=handle, stderr=subprocess.PIPE, check=False,
            env={**os.environ, **credential},
        )
    if done.returncode != 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"pg_dump exited {done.returncode}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()[-2000:]}"
        )
    partial.replace(path)
    return path.stat().st_size


async def run() -> BackupReport:
    """The nightly job. No arguments: the loop calls it with none, and §2's inputs are config.

    `pg_dump` is a blocking child process that can run for minutes on the full corpus, so it
    goes to a thread — the worker's other jobs share this event loop, and §7.3's one-minute
    playback poll is the one that would notice.
    """
    cfg = settings()
    directory = backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / dump_name(datetime.now(UTC))

    size = await asyncio.to_thread(dump, cfg.database_url, path)
    pruned = await asyncio.to_thread(prune, directory)
    return BackupReport(
        path=path, bytes=size, pruned=tuple(pruned), kept=len(dumps(directory))
    )


__all__ = [
    "KEEP", "BackupReport", "backups_dir", "dump", "dump_name", "dumps", "interrupted", "prune",
    "run",
]
