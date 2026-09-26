"""§2's nightly `pg_dump` to `/data/backups`, rotation 14. The worker runs the binary itself
(postgresql-client-16, pinned to §1's server: pg_dump refuses a newer one) rather than drive the
`db` container, which would need the Docker socket.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from spielplan.core.config import settings

KEEP = 14

# A command, so a box without libpq can point the tests at the database container's binary.
PG_DUMP: tuple[str, ...] = ("pg_dump",)

# Due jobs run one after another, so a hung dump would stall every other job. Far past the
# corpus's 35 s dump.
DUMP_TIMEOUT_SECONDS = 1800

PREFIX = "spielplan-"
SUFFIX = ".dump"
# Written here first, so a truncated dump never occupies one of the fourteen slots.
PARTIAL = ".partial"


@dataclass(frozen=True)
class BackupReport:
    path: Path
    bytes: int
    pruned: tuple[str, ...]
    kept: int
    # Tonight's dump already existed and this call wrote nothing (see `run`).
    skipped: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "kept": self.kept,
            "pruned": list(self.pruned),
            "skipped": self.skipped,
        }


def backups_dir() -> Path:
    """Mounted on the worker alone (§14.3): the backend has no reason to hold every night's dump."""
    return settings().data_dir / "backups"


def dump_name(now: datetime) -> str:
    """UTC, second resolution, so names sort chronologically. Rotation orders by name,
    since an off-box copy rewrites mtimes."""
    return f"{PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"


# Rotation deletes, so "ours" is exactly `dump_name`'s shape: `spielplan-*.dump` would also match
# an operator's `spielplan-before-upgrade.dump`, which sorts oldest and would go first.
_OWN_NAME = re.compile(
    rf"{re.escape(PREFIX)}(?P<stamp>\d{{8}}T\d{{6}}Z){re.escape(SUFFIX)}"
    rf"(?:{re.escape(PARTIAL)})?"
)
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _is_own(name: str) -> bool:
    return _OWN_NAME.fullmatch(name) is not None


def _written_at(name: str) -> datetime | None:
    """None for a shape-valid but impossible date (an operator's file); the nightly job must not raise."""
    match = _OWN_NAME.fullmatch(name)
    if match is None:  # pragma: no cover - `dumps()` filters by `_is_own` first
        return None
    try:
        return datetime.strptime(match.group("stamp"), _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def dumps(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if p.is_file() and _is_own(p.name)
    )


def interrupted(directory: Path) -> list[Path]:
    """Partials a SIGKILL or power cut left behind; `dumps()`'s glob never matches them."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob(f"{PREFIX}*{SUFFIX}{PARTIAL}") if p.is_file() and _is_own(p.name)
    )


def todays_dump(directory: Path, local: datetime) -> Path | None:
    """Compares household-local dates, as `worker.Job.anchor_hour` does. Names stay UTC for sorting, so
    each name's instant is converted to `local.tzinfo` (None: the system zone)."""
    return next(
        (
            p
            for p in dumps(directory)
            if (at := _written_at(p.name)) is not None
            and at.astimezone(local.tzinfo).date() == local.date()
        ),
        None,
    )


def prune(directory: Path, keep: int = KEEP) -> list[str]:
    """§2's rotation 14, over this job's own names only. Interrupted partials are removed outright."""
    existing = dumps(directory)
    doomed = existing[: max(0, len(existing) - keep)] + interrupted(directory)
    for path in doomed:
        path.unlink()
    return [path.name for path in doomed]


def _connection(database_url: str) -> tuple[str, dict[str, str]]:
    """The password moves to PGPASSWORD (argv is world-readable), percent-decoded as libpq would. The
    netloc is rebuilt textually: `urlsplit().hostname` strips an IPv6 literal's brackets."""
    parts = urlsplit(database_url)
    if not parts.password:
        return database_url, {}
    userinfo, _, hostport = parts.netloc.rpartition("@")
    username = userinfo.partition(":")[0]
    netloc = f"{username}@{hostport}" if username else hostport
    return urlunsplit(parts._replace(netloc=netloc)), {"PGPASSWORD": unquote(parts.password)}


def dump(database_url: str, path: Path) -> int:
    """Atomic via `.partial`; returns bytes; blocking. `--no-owner --no-privileges` so a restore into an
    install with another POSTGRES_USER works. The binary is checked before the partial is opened."""
    if shutil.which(PG_DUMP[0]) is None:
        raise RuntimeError(
            f"{PG_DUMP[0]}: no such binary on PATH. The nightly dump (spec section 2) needs the "
            f"postgresql-client-16 package the app image installs (ops/backend.Dockerfile)"
        )
    partial = path.with_name(path.name + PARTIAL)
    dsn, credential = _connection(database_url)
    argv = [*PG_DUMP, "--format=custom", "--no-owner", "--no-privileges", dsn]
    try:
        with partial.open("wb") as handle:
            done = subprocess.run(
                argv, stdout=handle, stderr=subprocess.PIPE, check=False,
                env={**os.environ, **credential}, timeout=DUMP_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as expired:
        # `subprocess.run` has killed and reaped the child; only the partial is left.
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"pg_dump did not finish within {DUMP_TIMEOUT_SECONDS}s and was killed"
        ) from expired
    if done.returncode != 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"pg_dump exited {done.returncode}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()[-2000:]}"
        )
    partial.replace(path)
    return path.stat().st_size


async def run(local: datetime) -> BackupReport:
    """`local` is the household's wall clock, the date `todays_dump` is judged on. A dump already there
    for this night makes the call a no-op: rotation 14 promises fourteen nights, not fourteen starts."""
    cfg = settings()
    directory = backups_dir()
    directory.mkdir(parents=True, exist_ok=True)

    already = todays_dump(directory, local)
    if already is not None:
        return BackupReport(
            path=already, bytes=already.stat().st_size, pruned=(),
            kept=len(dumps(directory)), skipped=True,
        )

    path = directory / dump_name(datetime.now(UTC))
    size = await asyncio.to_thread(dump, cfg.database_url, path)
    pruned = await asyncio.to_thread(prune, directory)
    return BackupReport(
        path=path, bytes=size, pruned=tuple(pruned), kept=len(dumps(directory))
    )


__all__ = [
    "DUMP_TIMEOUT_SECONDS", "KEEP", "BackupReport", "backups_dir", "dump", "dump_name", "dumps",
    "interrupted", "prune", "run", "todays_dump",
]
