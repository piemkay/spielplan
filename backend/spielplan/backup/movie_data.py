"""The movie-data archive (§10, decision 162): content spine, naming layer and reviews without user
state, as COPY streams in a zip. Columns and sequences come from the catalog so ids survive; the
snapshot and the restore lock guard against the worker minting ids concurrently.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from spielplan.db import pool as db_pool

# Bumped when the layout changes in a way a reader cannot infer. A table leaving TABLES goes to
# `RETIRED` instead and does not bump this (decision 309).
FORMAT = 1
MANIFEST = "manifest.json"


class RestoreRefused(RuntimeError):
    """The install cannot take this archive. Raised before anything is written."""


@dataclass(frozen=True)
class Table:
    schema: str
    name: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


# Replay order: a table never precedes one it references (no DEFERRABLE FKs). No user state, no
# secret custody, nothing derived from a bundle (§10).
TABLES: tuple[Table, ...] = (
    Table("public", "title"),
    Table("public", "person"),
    Table("public", "title_meta"),
    Table("public", "title_alias"),
    Table("public", "title_genre"),
    Table("public", "title_keyword"),
    Table("public", "title_country"),
    Table("public", "title_company"),
    Table("public", "title_video"),
    Table("public", "credit"),
    Table("public", "award"),
    # The MovieLens genome slice is no longer imported (decision 291); see `RETIRED`.

    # §4.1 rule 4 freezes its ids, and the calibration artifacts key on them.
    Table("public", "rating_source"),
    Table("public", "seed_list"),
    Table("public", "dna_vocabulary"),
    Table("public", "dna_facet"),
    Table("public", "dna_term"),
    Table("public", "dna_alias"),
    Table("public", "dna_axis"),
    Table("public", "dna_axis_weight"),
    Table("public", "dna_tag"),
    Table("public", "dna_evidence"),
    Table("public", "dna_projected"),
    # Curated ledgers §8 stage 3 re-applies: without them hand-made fixes silently revert.
    Table("public", "dna_adjudication"),
    Table("public", "credit_correction"),
    # §4.1 rule 3's display-only schema. Backing it up is not importing from it.
    Table("display", "platform_rating"),
    Table("review_store", "review"),
)

# Tables older archives carry and a restore skips (decision 309). Named, so any other unknown
# table is still refused; skipped, not loaded, because this build stopped importing them.
RETIRED: frozenset[str] = frozenset({
    "public.ml_genome_tag", "public.ml_link", "public.ml_genome_score",
    "public.title_language", "public.rating_title_map", "public.watchlist", "public.title_list",
    "public.title_list_membership",
})

# The placement stamp and state stay behind: they name a bundle and a coordinate this archive
# does not carry (§10), so a restore leaves every title honestly unplaced.
DROPPED_COLUMNS: dict[str, frozenset[str]] = {
    "public.title": frozenset({"placement_bundle", "placement", "placement_at"}),
}


@dataclass(frozen=True)
class ArchiveReport:
    path: Path
    tables: dict[str, int]
    sequences: dict[str, int]
    bytes: int
    # What this install still holds that the archive does not carry (`RETIRED`).
    retired: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "rows": sum(self.tables.values()),
            "tables": len(self.tables),
            "sequences": self.sequences,
            "retired": list(self.retired),
        }


@dataclass(frozen=True)
class RestoreReport:
    path: Path
    tables: dict[str, int]
    sequences: dict[str, int]
    seeded: str | None = None
    # What the archive named and this build no longer keeps (`RETIRED`).
    retired: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "rows": sum(self.tables.values()),
            "tables": len(self.tables),
            "sequences": self.sequences,
            "seeded": self.seeded,
            "retired": list(self.retired),
        }


# `is_generated` drops GENERATED columns, which COPY FROM refuses. `pg_get_serial_sequence` finds
# every sequence, bigserial or OWNED BY, so a new id-bearing table needs no edit.
_COLUMNS = """
SELECT c.table_schema, c.table_name, c.column_name,
       pg_get_serial_sequence(format('%I.%I', c.table_schema, c.table_name),
                              c.column_name) AS sequence
  FROM information_schema.columns c
  JOIN unnest($1::text[], $2::text[]) AS wanted(schema_name, table_name)
    ON wanted.schema_name = c.table_schema AND wanted.table_name = c.table_name
 WHERE c.is_generated = 'NEVER'
 ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""


async def _layout(
    conn: asyncpg.Connection,
) -> tuple[dict[str, list[str]], list[str], dict[str, tuple[str, str]]]:
    """The archived columns, their sequences, and which column owns each (for `_position`)."""
    rows = await conn.fetch(
        _COLUMNS, [t.schema for t in TABLES], [t.name for t in TABLES]
    )
    columns: dict[str, list[str]] = {}
    sequences: set[str] = set()
    owners: dict[str, tuple[str, str]] = {}
    for row in rows:
        key = f"{row['table_schema']}.{row['table_name']}"
        if row["column_name"] in DROPPED_COLUMNS.get(key, frozenset()):
            continue
        columns.setdefault(key, []).append(row["column_name"])
        if row["sequence"]:
            sequences.add(row["sequence"])
            owners[row["sequence"]] = (key, row["column_name"])
    missing = [t.qualified for t in TABLES if t.qualified not in columns]
    if missing:
        raise RuntimeError(
            f"the movie-data archive names tables this schema does not have: {', '.join(missing)}"
        )
    return columns, sorted(sequences), owners


def _rows(status: str) -> int:
    return int(status.rsplit(" ", 1)[-1])


def _entry(table: str) -> str:
    return f"tables/{table}.copy"


async def _seed_record(conn: asyncpg.Connection, created_at: str) -> dict[str, object] | None:
    """The seed row decision 162's refusal keys on, or a stand-in naming this archive; None without
    titles. Without it a restored install would accept a second content seed."""
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM title)"):
        return None
    row = await conn.fetchrow(
        "SELECT version, manifest::text AS manifest, report::text AS report, vocabulary_version "
        "FROM artifact_bundle WHERE kind = 'seed' ORDER BY imported_at LIMIT 1"
    )
    if row is not None:
        return dict(row)
    return {
        "version": f"movie-data-archive:{created_at}",
        "manifest": json.dumps({"kind": "movie-data", "created_at": created_at}),
        "report": "{}",
        "vocabulary_version": None,
    }


def _positioned(positions: dict[str, dict[str, object]]) -> dict[str, int]:
    return {name: int(pos["last_value"]) for name, pos in positions.items()}


async def _position(
    conn: asyncpg.Connection, sequence: str, owner: tuple[str, str]
) -> dict[str, object]:
    """At least the archived maximum: the seed import writes explicit ids. A never-called sequence above
    it is left alone, being the app's mint floor, which decision 162 keeps disjoint from corpus ids."""
    table, column = owner
    schema, name = table.split(".", 1)
    row = await conn.fetchrow(f"SELECT last_value, is_called FROM {sequence}")
    last_value, is_called = int(row["last_value"]), bool(row["is_called"])
    highest = await conn.fetchval(f'SELECT max("{column}") FROM "{schema}"."{name}"')
    if highest is not None and (
        int(highest) > last_value or (int(highest) == last_value and not is_called)
    ):
        last_value, is_called = int(highest), True
    return {"last_value": last_value, "is_called": is_called}


async def write_archive(conn: asyncpg.Connection, path: Path) -> ArchiveReport:
    """Streamed, not buffered (the review store is ~312 MB). One repeatable-read snapshot, positions read
    after the rows so each bounds the ids carried. Written to `.partial`, renamed only when whole."""
    columns, sequences, owners = await _layout(conn)
    created_at = datetime.now(UTC).isoformat()
    positions: dict[str, dict[str, object]] = {}
    counts: dict[str, int] = {}
    retired: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                snapshot = await conn.fetchval("SELECT pg_current_snapshot()::text")
                seed = await _seed_record(conn, created_at)
                for table in TABLES:
                    # zip64: the review table alone can pass the 4 GB entry limit.
                    with archive.open(_entry(table.qualified), "w", force_zip64=True) as stream:
                        status = await conn.copy_from_table(
                            table.name,
                            schema_name=table.schema,
                            columns=columns[table.qualified],
                            output=stream,
                        )
                    counts[table.qualified] = _rows(status)

                for sequence in sequences:
                    positions[sequence] = await _position(conn, sequence, owners[sequence])

                # In the same snapshot, so the printed counts are one reading of one install.
                for qualified in sorted(RETIRED):
                    schema, _, table = qualified.partition(".")
                    # 0039 dropped five of them; the genome slice is still held (decision 311).
                    if await conn.fetchval("SELECT to_regclass($1)", qualified) and await conn.fetchval(
                        f'SELECT EXISTS (SELECT 1 FROM "{schema}"."{table}")'
                    ):
                        retired.append(qualified)

            archive.writestr(
                MANIFEST,
                json.dumps(
                    {
                        "format": FORMAT,
                        "kind": "movie-data",
                        "created_at": created_at,
                        "snapshot": snapshot,
                        "seed": seed,
                        "tables": [
                            {
                                "schema": t.schema,
                                "name": t.name,
                                "columns": columns[t.qualified],
                                "rows": counts[t.qualified],
                            }
                            for t in TABLES
                        ],
                        "sequences": positions,
                    },
                    indent=1,
                ),
            )
        # Inside the guard: a rename onto a directory fails with the whole archive already on disk.
        partial.replace(path)
    except BaseException:
        # BaseException: stopping the stack mid-write arrives as CancelledError.
        partial.unlink(missing_ok=True)
        raise

    return ArchiveReport(
        path=path,
        tables=counts,
        sequences=_positioned(positions),
        bytes=path.stat().st_size,
        retired=tuple(retired),
    )


# EXCLUSIVE blocks writers, not readers. One statement in TABLES order, so restores queue, not deadlock.
_LOCK = (
    "LOCK TABLE "
    + ", ".join(f'"{t.schema}"."{t.name}"' for t in TABLES)
    + " IN EXCLUSIVE MODE"
)


async def restore_archive(conn: asyncpg.Connection, path: Path) -> RestoreReport:
    """Refuses before it writes, then one transaction. The lock is held from the occupancy check to the
    commit, so no row can appear in between and the absolute `setval` stays safe."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get("format") != FORMAT:
            raise RestoreRefused(
                f"archive format {manifest.get('format')!r} is not {FORMAT}: this build cannot "
                "read it, and reading it wrongly would load a content spine nobody can check"
            )

        entries, carried_positions = manifest["tables"], manifest["sequences"]
        known = {t.qualified for t in TABLES}
        # Subtracted once, so everything below sees the archive as this build would have written it.
        retired = sorted({f"{e['schema']}.{e['name']}" for e in entries} & RETIRED)
        entries = [e for e in entries if f"{e['schema']}.{e['name']}" not in RETIRED]
        named = [f"{e['schema']}.{e['name']}" for e in entries]
        # Both directions: no foreign table COPYed in, no archived table silently missing.
        if set(named) != known:
            raise RestoreRefused(
                "the archive's table set does not match this build's: "
                f"unknown {sorted(set(named) - known)}, missing {sorted(known - set(named))}"
            )

        seed = manifest.get("seed")
        counts: dict[str, int] = {}
        async with conn.transaction():
            await conn.execute(_LOCK)
            # Inside the transaction and behind the lock, so "holds no movie data" stays true.
            for entry in entries:
                occupied = await conn.fetchval(
                    f'SELECT EXISTS (SELECT 1 FROM "{entry["schema"]}"."{entry["name"]}")'
                )
                if occupied:
                    raise RestoreRefused(
                        f"{entry['schema']}.{entry['name']} already holds rows: a movie-data "
                        "restore targets an install with no movie data in it (decision 162 — "
                        "content seeds once), and merging two spines is not something this can "
                        "do silently"
                    )

            for entry in entries:
                qualified = f"{entry['schema']}.{entry['name']}"
                with archive.open(_entry(qualified)) as stream:
                    status = await conn.copy_to_table(
                        entry["name"],
                        schema_name=entry["schema"],
                        columns=entry["columns"],
                        source=stream,
                    )
                counts[qualified] = _rows(status)

            # Without this the restored install mints from START and collides with the ids just loaded.
            for sequence, position in carried_positions.items():
                await conn.execute(
                    "SELECT setval($1::regclass, $2::bigint, $3::boolean)",
                    sequence, position["last_value"], position["is_called"],
                )

            if seed is not None:
                # 'superseded': the archive has no artifacts tree, and an active row without
                # files reads as broken. `::text::jsonb`: the pool's codec would encode twice.
                await conn.execute(
                    "INSERT INTO artifact_bundle "
                    "       (version, manifest, report, state, kind, vocabulary_version) "
                    "VALUES ($1, $2::text::jsonb, $3::text::jsonb, 'superseded', 'seed', $4)",
                    seed["version"], seed["manifest"], seed["report"],
                    seed.get("vocabulary_version"),
                )

    return RestoreReport(
        path=path,
        tables=counts,
        sequences=_positioned(carried_positions),
        seeded=None if seed is None else seed["version"],
        retired=tuple(retired),
    )


@asynccontextmanager
async def _connection() -> AsyncIterator[asyncpg.Connection]:
    """Through `db/pool`, so the jsonb codec the restore's cast defends against is really present."""
    pool = await db_pool.open_pool(min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        await db_pool.close_pool()


async def _run(command: str, path: Path) -> int:
    if command == "restore" and not path.is_file():
        print(f"refusing: no archive at {path}")
        return 1
    async with _connection() as conn:
        try:
            if command == "write":
                written = await write_archive(conn, path)
                print(
                    f"wrote {written.path}: {sum(written.tables.values())} rows from "
                    f"{len(written.tables)} tables, {written.bytes} bytes. It carries no user "
                    "state and no connector secret -- section 2's nightly dump is what backs "
                    "those up."
                    + (
                        " This install still holds rows in "
                        + ", ".join(written.retired)
                        + ", loaded by a build before decision 291; this archive does not "
                        "carry them, and that same nightly dump is where they are."
                        if written.retired
                        else ""
                    )
                )
                return 0
            restored = await restore_archive(conn, path)
            print(
                f"restored {restored.path}: {sum(restored.tables.values())} rows into "
                f"{len(restored.tables)} tables, sequences positioned"
                + (f", seeded by {restored.seeded}" if restored.seeded else "")
                + (
                    ". This archive was written by a build that still archived "
                    f"{', '.join(restored.retired)}; this one does not keep them, and "
                    "their entries were passed over (decision 291)"
                    if restored.retired
                    else ""
                )
                + ". Restart the backend and the worker."
            )
            return 0
        except (RestoreRefused, zipfile.BadZipFile, OSError) as exc:
            # A refusal is the documented outcome. OSError too: the path is operator-typed (wrong container,
            # read-only mount, full disk), and `.partial` means nothing was produced.
            print(f"refusing: {exc}")
            return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spielplan-movie-data",
        description=(
            "The movie-data archive (decision 162): the content spine, the naming layer and the "
            "review bodies, without any user state. Write one, or restore one into an install "
            "that holds no movie data."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="write an archive of this install's movie data")
    write.add_argument("path", type=Path, help="e.g. /data/backups/movie-data.zip")
    restore = sub.add_parser(
        "restore", help="load an archive into an install with no movie data in it"
    )
    restore.add_argument("path", type=Path, help="the archive to read")
    return parser


def main(argv: list[str] | None = None) -> int:
    """An operator command, not a route: restore locks every archived table; write is minutes of COPY."""
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args.command, args.path))


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    sys.exit(main())


__all__ = [
    "RETIRED",
    "TABLES",
    "ArchiveReport",
    "RestoreRefused",
    "RestoreReport",
    "Table",
    "main",
    "restore_archive",
    "write_archive",
]
