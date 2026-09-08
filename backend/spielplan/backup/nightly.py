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
import re
import shutil
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

# Half an hour. `_tick` awaits due jobs one after another, so a `pg_dump` blocked behind an
# import's exclusive lock does not merely miss a night: it stops §7.3's one-minute playback poll,
# the fold-in tick and the Jellyfin sync for as long as it hangs, and `subprocess.run` with no
# `timeout=` hangs for ever. The number is far past the 35 s the corpus dump measures and far
# short of a night, so the only thing it can cut off is a dump that is never going to finish.
# [M4.7 ops-07]
DUMP_TIMEOUT_SECONDS = 1800

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
    # True when tonight's dump already existed and this call wrote nothing (see `run`). §6.6's
    # backup line reads `bytes` and the row's `finished_at` either way — both describe the file
    # that is on disk — but the `job_run` row (0017_ops.sql) is the only record of what the loop
    # did, and "the dump is from tonight" and "this run made it" are different facts to an
    # operator reading job health. [M4.7 ops-03]
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
    """§2's target. `/data/backups` is mounted on the worker alone (docker-compose.yml): the
    backend serves §6's anonymous SPA fallback and has no reason to hold every night's dump
    (§14.3), so this path exists only in the process that writes it. [M4.7 sec-08]"""
    return settings().data_dir / "backups"


def dump_name(now: datetime) -> str:
    """UTC and second-resolution, so the name sorts chronologically as a string.

    Rotation orders by name rather than by mtime on purpose: an operator who copies the
    directory off-box and back rewrites every mtime, and the fourteen kept would then be
    whichever fourteen the copy happened to touch last.
    """
    return f"{PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"


# Rotation deletes, so "ours" has to mean the exact name `dump_name` produces and nothing else.
# The glob that used to answer the question — `spielplan-*.dump` — also matches
# `spielplan-2026-08-14-before-upgrade.dump`, which is what an operator calls the copy they take
# before an upgrade, in the one directory §2 and the compose file tell them to keep it in. Worse
# than matching: `-` (0x2D) sorts below `0`, so by `dumps()`'s own name order that file is the
# *oldest* of the set and is the first thing `prune` unlinks — on the day they are about to need
# it. [M4.7 dd-deploy-rotation-glob; `prune` has promised "only files this job's own naming
# produced" since it was written]
_OWN_NAME = re.compile(
    rf"{re.escape(PREFIX)}(?P<stamp>\d{{8}}T\d{{6}}Z){re.escape(SUFFIX)}"
    rf"(?:{re.escape(PARTIAL)})?"
)
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _is_own(name: str) -> bool:
    """Whether `dump_name` could have produced `name`, finished or interrupted.

    One matcher for both states on purpose: the two globs below already separate them, and a
    second pattern is a second place for the naming to drift away from `dump_name`.
    """
    return _OWN_NAME.fullmatch(name) is not None


def _written_at(name: str) -> datetime | None:
    """The UTC instant `dump_name` encoded in `name`, or None if it encoded no real one.

    `_is_own` matches the *shape* of the stamp and not the calendar, so a file an operator
    dropped into `/data/backups` as `spielplan-20241301T030000Z.dump` passes it. Nothing this job
    wrote can carry an impossible date, so such a name can never be tonight's dump — and this
    runs inside the nightly job, which must not raise over a file somebody else put in its
    directory. Rotation's view of the same name is deliberately left alone: what `prune` may
    delete is `_is_own`'s question and not this one. [M4.7 ops-03]
    """
    match = _OWN_NAME.fullmatch(name)
    if match is None:  # pragma: no cover - `dumps()` filters by `_is_own` first
        return None
    try:
        return datetime.strptime(match.group("stamp"), _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def dumps(directory: Path) -> list[Path]:
    """This job's own dumps, oldest first. Nothing else in the directory is ours."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if p.is_file() and _is_own(p.name)
    )


def interrupted(directory: Path) -> list[Path]:
    """The debris a killed `pg_dump` leaves: named by this job, never finished.

    `dump()` unlinks its own partial on a non-zero exit, but a SIGKILL, a container stop or a
    power cut inside the nightly window does not return from `subprocess.run` at all — and the
    file it leaves ends in `.partial`, so `dumps()`'s `*{SUFFIX}` glob never matches it again.
    """
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob(f"{PREFIX}*{SUFFIX}{PARTIAL}") if p.is_file() and _is_own(p.name)
    )


def todays_dump(directory: Path, local: datetime) -> Path | None:
    """This job's dump for `local`'s **household** calendar date, if one is already there.

    One date basis with `worker.Job.anchor_hour`, which is the whole of this function's contract.
    The two guards disagreed while this one asked the UTC date: `anchor_hour` fires an anchored
    job once per *local* calendar date, so on a household far enough from UTC two consecutive
    local dates can share one UTC date — at UTC+13 every local time before 13:00 falls on
    yesterday's UTC day. A dump that landed in the local evening (a first boot, or a retry that
    finally succeeded after the anchor) was then stamped with a UTC date the next local night also
    maps to, and that night's scheduled dump was refused as "already dumped today" while
    `job_run.ok` stayed true — a night missing from §2's fourteen with nothing anywhere reporting
    it, and the further from UTC the household sits the more of them.

    `dump_name` stays UTC and deliberately so: the names are sorted as strings and rotation
    depends on that order (see `dump_name`). So the question is asked of the instant each name
    encodes, converted into the household's zone — which is what makes the file names answer a
    question about nights rather than about UTC days.

    `local.tzinfo` is None on `worker._now_local`'s bad-TZ fallback, and `astimezone(None)` is the
    system zone — the same idiom `worker._seed_schedule` uses, so both sides of the comparison
    agree in that branch too. [M4.7 ops-03, cs-10]
    """
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

    The netloc is rebuilt textually, out of `parts.netloc`, rather than out of the parsed pieces:
    the parsed pieces are lossy in exactly the case §2 calls supported above — "an install
    pointing at a Postgres outside this compose file", which is where an IP literal appears.
    `urlsplit().hostname` returns an IPv6 literal without its brackets and lower-cased, so
    `postgresql://u:p@[::1]:5432/db` was handed to libpq as `postgresql://u@::1:5432/db` and
    rejected with `invalid integer value ":1:5432" for connection option "port"` — an install
    with a working app and zero backups, because asyncpg parses the bracketed original fine and
    every other job runs normally. Dropping the `:<password>` segment is the same split
    `urlsplit` itself performs (rpartition on `@`, then partition on `:`), so brackets, a
    percent-encoded username and anything else in there survive untouched.
    [M4.7 dd-deploy-ipv6]
    """
    parts = urlsplit(database_url)
    if not parts.password:
        return database_url, {}
    userinfo, _, hostport = parts.netloc.rpartition("@")
    username = userinfo.partition(":")[0]
    netloc = f"{username}@{hostport}" if username else hostport
    return urlunsplit(parts._replace(netloc=netloc)), {"PGPASSWORD": unquote(parts.password)}


def dump(database_url: str, path: Path) -> int:
    """Run `pg_dump` into `path`, atomically. Returns the size in bytes. Blocking.

    `--no-owner --no-privileges` because §2's restore target is a fresh `docker compose up`
    whose `POSTGRES_USER` the operator is free to have changed; ownership statements naming a
    role that install does not have turn a restore into a wall of errors.

    The binary is resolved before the partial is opened, which is the whole of that first block.
    It used to be the other way round, so an install without `postgresql-client-16` leaked a
    0-byte `<name>.dump.partial` on every attempt: the `FileNotFoundError` came from inside the
    `with`, past the `partial.unlink` that only runs on a non-zero exit, and `run()` prunes only
    after `dump()` has returned — one file a night, for ever, in the directory §2 asks the
    operator to copy off-box. The refusal names the package rather than reporting ENOENT, because
    the answer to a missing client is never "retry". [M4.7 dd-backup-missing-pg-dump]
    """
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
        # `subprocess.run` has already killed the child and reaped it by the time this is raised,
        # so what is left is the half-written file, and it is debris of exactly the kind the
        # `.partial` suffix exists to keep out of the fourteen.
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
    """The nightly job. `local` is the household's wall clock (§2's `TZ`); the rest is config.

    The argument is required rather than defaulted because it is the whole of `todays_dump`'s
    contract: the caller that decided this job was due for a local calendar date is the caller
    that has to say which date that was. `worker._nightly_backup` passes `worker._now_local()`,
    the same reading `due` is judged against. [M4.7 ops-03]

    `pg_dump` is a blocking child process that can run for minutes on the full corpus, so it
    goes to a thread. That keeps the event loop itself responsive; it does not decouple this job
    from the others, because `worker._tick` awaits the due jobs one after another — a dump that
    runs for a minute delays §7.3's one-minute playback poll by a minute whether or not it holds
    the loop thread. What bounds that is `DUMP_TIMEOUT_SECONDS`, not the thread. [M4.7 ops-04]

    A dump already carrying this household night's date makes the whole call a no-op, and that is
    a retention rule rather than an optimisation. §2's "rotation 14" is a promise about fourteen
    *nights*, and with `last_run` in-process every worker start fired this job: fourteen restarts
    inside an hour spent all fourteen slots and left the household with no history at all.
    `worker.due`'s local-date anchoring is the first half of that fix; this is the half that
    holds when the loop is restarted, when a retry lands after a failure that was not the dump's,
    and when an operator runs the job by hand. It also retires `dump_name`'s same-second
    collision — two dumps started in the same second produced one name, and the second replaced
    the first silently. [M4.7 ops-03, cs-10]
    """
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
