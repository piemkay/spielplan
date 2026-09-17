"""The movie-data archive. Spec v2.1 §4.1, §4.3, §10; decision 162.

Decision 162 made the corpus a one-time seed for content: "movie data is exported once by a
compatible exporter and imported once; every later title is acquired by Spielplan itself".
That sentence is what makes this module necessary rather than a convenience. Before it, a lost
database meant re-running the importer against a bundle the corpus still had; after it, the
household's copy of the content spine, the naming layer and the review bodies is the only copy
there is, and §2's whole-database dump is the wrong shape for moving it — it carries every
verdict, session and passkey with it.

So: an archive of the movie data and nothing else, in a format that does not need a `pg_dump`
binary at either end (COPY streams in a zip, written and read through the same connection the
app already holds). Two things in it are easy to leave out and impossible to notice afterwards:

- **The sequence positions.** 0015_seed.sql declines to position `title_id_seq` and says why —
  a fresh install has an empty `title`, so `setval(max(id))` would yield 1. The seed import
  positions it instead, and the seed import by definition never runs again. An archive that
  carries rows but not `setval` therefore restores an install whose next acquired title re-mints
  an id the archive already used, and §7.2 lands a new film on top of an existing one.
- **`title.origin`.** It defaults to 'bundle' (0008_placement.sql), so an archive that dropped
  the column restores looking entirely correct with §10's rebuild set — "Cold Tower re-placement
  of every app-acquired title" — permanently empty.

Both are covered by taking the columns and the sequences from the catalog rather than from a
hand-written list, which is also what keeps this from drifting the way the importer's MAPPINGS
did.

`main` is the operator's way in: `spielplan-movie-data write|restore <path>`, the two gestures
decision 162 makes necessary, as a command rather than an admin route. Until M4.7 there was no
caller at all outside this module's own tests — and that absence was load-bearing, because both
halves below were written as if nothing else could be touching the database while they ran. The
realistic writer is the worker: §7.1's Jellyfin sync fires at worker start and `sync/resolve.py`
inserts a title for a library item nobody has seen before, so the moment an operator can run
`docker compose exec worker spielplan-movie-data write` against a live stack, an archive is
being written underneath a process that mints ids. The snapshot in `write_archive` and the lock
in `restore_archive` are what that fact costs.

The worker, and not the backend as this paragraph used to say: `sec-08` took `./data/backups`
off `x-backend-volumes` in this same milestone, because the process serving §6's anonymous SPA
fallback has no business holding every night's dump (§14.3) — so the backend container cannot
write the archive at all, and the container it *can* be run in is the very one whose loop mints
the ids above. README's Recovery block has said `worker` since it was written.
[M4.7 spec-07, data-04, dd18, data-12, sec-08; decisions 181, 182]
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

# Bumped when the layout changes in a way a reader cannot infer. A restore refuses a format it
# does not know rather than mis-reading it: half a restored content spine is worse than none.
#
# A table LEAVING `TABLES` is not such a change and does not bump this: `RETIRED` below names
# every one that has, so an older archive is read exactly as far as this build still has a use
# for it and the reader infers nothing. A table JOINING `TABLES` is a different fact -- the older
# archive really is missing a layer, and `missing [...]` is a substantive answer rather than a
# nominal one. [decision 309]
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


# The archive, in an order a restore can replay: a table never precedes one it references.
# There are no DEFERRABLE foreign keys in this schema, so the order is the mechanism, not a
# tidiness preference.
#
# What is NOT here is the point of the artifact: no user state (§4.2's whole block), no secret
# custody (`connector_config`, `data_encryption_key`, `app_setting`), and nothing derived from a
# bundle — §10 is explicit that "everything expressed in the old Backbone's basis is garbage
# against a new one", so `title_placement` and `title_prior` are rebuilt, never carried.
TABLES: tuple[Table, ...] = (
    # The spine (§4.1). `title` and `person` first: nearly everything below references one.
    Table("public", "title"),
    Table("public", "person"),
    Table("public", "title_meta"),          # multi-source, per-source rows kept
    Table("public", "title_alias"),
    Table("public", "title_genre"),
    Table("public", "title_keyword"),
    Table("public", "title_language"),
    Table("public", "title_country"),
    Table("public", "title_company"),
    Table("public", "title_video"),
    Table("public", "credit"),
    Table("public", "award"),
    # The MovieLens genome + link slice stood here while the importer filled it. Decision 291
    # stops importing it, upholding `media-graph-spec_v1.1.md:175` -- "validation artifact only,
    # never shipped or imported into the app" -- so `0003_content.sql`'s three tables stay in the
    # schema and are empty on every install THIS BUILD seeds. Three entries writing three empty
    # COPY streams would cost nothing but say something false: `manifest.json` is what a restore
    # reads back, and a table named there is a table the household is told travelled with its
    # spine. Zero-imputation is the path those 983 columns take wherever the rows are absent,
    # which is the measurement decision 291 rests on -- and on a box seeded BEFORE 291 they are
    # not absent, which is why this paragraph says what this build imports rather than a property
    # of every install. `RETIRED` below carries what the subtraction costs that box; this half
    # said it unconditionally until M4.16 cycle 4 and contradicted its own file fifty lines down.
    # [decisions 291, 309 and 311]

    # §10 calls `rating_source` "mandatory always", and §4.1 rule 4 freezes its ids — they key
    # `fitted_cuts`, `equating_map` and the dataset arrays, so losing the table loses the
    # meaning of every calibration artifact that survives alongside it.
    Table("public", "rating_source"),
    Table("public", "rating_title_map"),
    # The lists that travel with the bundle (§4.3, §10): the onboarding list, the wanted list,
    # and the corpus's own list registry with its membership rows (0015_seed.sql section 3).
    Table("public", "seed_list"),
    Table("public", "watchlist"),
    Table("public", "title_list"),
    Table("public", "title_list_membership"),
    # The naming layer. §4.1 rule 1: the two tiers are separate tables, never merged — they are
    # separate entries here for the same reason, and `dna_evidence` follows `dna_tag` because
    # "a tag without its quote is unfalsifiable".
    Table("public", "dna_vocabulary"),
    Table("public", "dna_facet"),
    Table("public", "dna_term"),
    Table("public", "dna_alias"),
    Table("public", "dna_axis"),
    Table("public", "dna_axis_weight"),
    Table("public", "dna_tag"),
    Table("public", "dna_evidence"),
    Table("public", "dna_projected"),
    # The curated ledgers §8 stage 3 re-applies at every derive: regenerating rows without them
    # silently reverts hand-made fixes.
    Table("public", "dna_adjudication"),
    Table("public", "credit_correction"),
    # §4.1 rule 3's display-only schema. Backing it up is not importing from it.
    Table("display", "platform_rating"),
    # §10: the review bodies, "needed for future re-extraction and text embedding".
    Table("review_store", "review"),
)

# Tables this archive used to carry and no longer does.
#
# An archive outlives the build that wrote it, and that is the whole point of it: decision 162
# makes it the household's copy of its content, and README's Recovery block has the operator write
# one to a stick and restore it into a rebuilt box -- a build at least as new as the writer and
# usually newer. So `TABLES` narrowing is a version boundary rather than an edit. The manifest's
# table set is checked below in both directions, and without this set every archive written by
# every shipped build up to M4.15 is refused by this one as "unknown ['public.ml_genome_score',
# 'public.ml_genome_tag', 'public.ml_link'], missing []" -- a sentence that reads as a corrupt or
# foreign file and is neither, on the one recovery gesture decision 162 leaves the household.
#
# Named rather than tolerated by class, and skipped rather than loaded. Named, because the other
# direction of that check is load-bearing: an archive arrives on a stick, over a channel nobody
# controls, and a table this build does not archive has no business being COPYed into the install
# from a file. Skipped, because decision 291's ruling is that the slice is not imported, and a
# recovery path that loaded it anyway would reverse a decision through the back door.
# `0003_content.sql` keeps the three tables and they are empty on every install THIS BUILD seeds,
# so a restore that passes over them leaves precisely the install a post-291 archive would have
# produced. On an install seeded BEFORE decision 291 the rows survive and `placement/features.py`
# still reads them, so this subtraction does change that box's placement inputs from populated to
# zero -- which is the restore doing what decision 309 rules, not a silent loss, and is why §4.1
# and §4.3 now state what this build IMPORTS rather than a property of every install.
# [decisions 291, 309, 311; M4.16 cycle 4, M416-C4-GEN-01]
RETIRED: frozenset[str] = frozenset(
    {"public.ml_genome_tag", "public.ml_link", "public.ml_genome_score"}
)

# Columns of an archived table that the archive deliberately does not carry.
#
# `title.placement_bundle` REFERENCES `artifact_bundle(version)` (0008_placement.sql:59) and this
# archive holds no bundle row, because §10 is explicit that "everything expressed in the old
# Backbone's basis is garbage against a new one" — the placement basis is rebuilt, never carried.
# Carrying the stamp anyway makes an archive taken from any install that has ever placed a title
# impossible to restore into the empty install this artifact exists to fill: every value names a
# version the fresh install has no row for. The column's own `ON DELETE SET NULL` is the schema
# making the same claim — which basis a coordinate was computed in is not part of a title's
# identity — so dropping it restores exactly the state a pruned bundle would have left.
# `title.placement` and `title.placement_at` go with it, one step further along the same
# argument. They are the denormalised state of a coordinate that lives in `title_placement`
# (0008_placement.sql), which this archive does not carry — so keeping them restores an install
# whose titles say 'warm' or 'cold_tower' while nothing holds a coordinate at all. §12's M2
# criterion is "every owned title has a coordinate", read as
# `count(*) FROM title WHERE is_owned AND placement = 'unplaced'` — the predicate
# 0008_placement.sql:64 indexes — and `reconcile.py`'s sweep only resets rows it can see are
# stale ('warm' with no Backbone row), so a restored 'cold_tower' would never be re-examined.
# The install would report zero titles waiting to be placed, on an install with no basis to
# place them in. Both columns are defaulted or nullable, so dropping them restores the honest
# state: every title unplaced, waiting for the model bundle decision 162 puts on top of the
# content. [M4.7 data-12]
DROPPED_COLUMNS: dict[str, frozenset[str]] = {
    "public.title": frozenset({"placement_bundle", "placement", "placement_at"}),
}


@dataclass(frozen=True)
class ArchiveReport:
    path: Path
    tables: dict[str, int]
    sequences: dict[str, int]
    bytes: int
    # What this install still HOLDS and this archive does not carry (`RETIRED`). Decision 309
    # gave the restore leg the named set, the field and the operator's line, on the ground
    # that a count reporting 31 tables for an archive that named 34 is a record that is true
    # and describes the wrong thing. This leg is where that mismatch is the COMMON case
    # rather than the rare one: 309's own is an archive written by an older build, while any
    # box seeded up to M4.15 still holds the slice (decision 311) and hands this build three
    # populated tables to pass over. It is also the case 309's mechanism cannot reach --
    # `RestoreReport.retired` is read off the MANIFEST, so the post-291 archive written here
    # carries nothing to name on the way back either. [decisions 291, 309, 311]
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
    # What the archive named and this build no longer keeps (`RETIRED`). Reported rather than
    # passed over in silence: the operator handed in an archive naming 34 tables, and a line
    # saying 31 with no account of the other three is the quiet half of the defect 309 repairs
    # -- a record that is true and describes the wrong thing. [decision 309]
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


# The columns of the archived tables, from the catalog. `is_generated` is the load-bearing
# filter: `review_store.review.word_count` is GENERATED ALWAYS … STORED, so COPY emits it and
# COPY FROM refuses it — an archive written with `SELECT *` cannot be restored at all.
#
# `pg_get_serial_sequence` in the same pass is what carries decision 162's id positions: it
# names the sequence behind a column whether it arrived as `bigserial` or as the explicit
# `OWNED BY` in 0015_seed.sql, so a new id-bearing table needs no edit here.
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
    """The archived columns, the sequences behind them, and which column owns each sequence.

    The third element exists for `_position`: bounding a sequence by the rows it minted needs
    the table and column it is attached to, and the catalog has already said so in the same
    pass. Deriving it a second time from a hand-written map is how the importer's MAPPINGS drift
    started.
    """
    rows = await conn.fetch(
        _COLUMNS, [t.schema for t in TABLES], [t.name for t in TABLES]
    )
    columns: dict[str, list[str]] = {}
    sequences: set[str] = set()
    owners: dict[str, tuple[str, str]] = {}
    dropped: set[str] = set()
    for row in rows:
        key = f"{row['table_schema']}.{row['table_name']}"
        if row["column_name"] in DROPPED_COLUMNS.get(key, frozenset()):
            dropped.add(f"{key}.{row['column_name']}")
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
    unmatched = {
        f"{table}.{column}" for table, names in DROPPED_COLUMNS.items() for column in names
    } - dropped
    if unmatched:
        # A rename would silently re-carry the column, and what it carries is a foreign key into
        # a table this archive does not hold — the restore would fail at the far end, on an
        # install with nothing in it, which is the worst place to discover it.
        raise RuntimeError(
            "the movie-data archive drops columns this schema does not have: "
            f"{', '.join(sorted(unmatched))}"
        )
    return columns, sorted(sequences), owners


def _rows(status: str) -> int:
    """asyncpg hands back the COPY command tag; the count is the only interesting half."""
    return int(status.rsplit(" ", 1)[-1])


def _entry(table: str) -> str:
    return f"tables/{table}.copy"


async def _seed_record(conn: asyncpg.Connection, created_at: str) -> dict[str, object] | None:
    """What the restored install must know to say "this install has been seeded".

    Decision 162's refusal is keyed on `artifact_bundle WHERE kind = 'seed'` — an install with
    no such row has, as far as the importer can tell, never taken content. A restore that loads
    the whole spine and leaves that row absent therefore hands the operator an install that will
    accept a content bundle over it: two minters in one id namespace, which is the precise
    failure decision 162 exists to prevent, arriving through the recovery path.

    The real seed row is preferred because it is provenance the household should not lose. An
    install that holds content without one (nothing in production produces that today, but a
    restore's guarantee should not depend on that staying true) gets a record naming this archive
    instead — the honest answer to "what seeded you", and one no bundle version can collide with.
    An install with no titles gets no record at all: it has not been seeded, and saying otherwise
    would refuse the content import it is still waiting for.
    """
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
    """Where each sequence stands, for the report.

    `is_called` still travels in the manifest and not here: it is the difference between a
    sequence that has minted `last_value` and one that is merely sitting on it, which matters to
    the restore and to nothing that reads a log line.
    """
    return {name: int(pos["last_value"]) for name, pos in positions.items()}


async def _position(
    conn: asyncpg.Connection, sequence: str, owner: tuple[str, str]
) -> dict[str, object]:
    """Where `sequence` must stand so the restored install cannot re-mint an archived id.

    The sequence's own position is the answer whenever the rows were minted through it. It is
    not when they were not: 0015_seed.sql declines to position `title_id_seq`, and the seed
    import writes the corpus's ids explicitly, so an install that has taken content and acquired
    nothing since carries ids that the sequence has never heard of. The bound over the archived
    maximum is the belt decision 162 deserves — the recorded position is then never below an id
    this archive holds, whatever route put the row there.

    The two conditions are not one condition: a sequence mints `last_value + 1` once it has been
    called and `last_value` itself before that, so a never-called sequence sitting exactly on the
    highest archived id would hand that id out again. And a never-called sequence sitting *above*
    it must be left alone — that is the fresh install whose `title_id_seq` starts at the app's
    mint floor while the archive holds corpus ids far below it, and winding it down to the corpus
    maximum would mint app titles inside the range decision 162 keeps disjoint.
    """
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
    """Write the movie-data archive at `path`.

    Streamed table by table into the zip rather than buffered: §10 sizes the review store at
    312 MB with bodies, and an appliance with 6-8 GB of RAM does not get to hold that twice.

    One `repeatable_read` read-only transaction around every read below, and the sequence
    positions taken *after* the rows rather than before them. Both are about the same concurrent
    writer — the worker's Jellyfin sync, which mints a title the moment it meets a library item
    it does not know. A COPY per archived table with no enclosing transaction is a snapshot
    per table, and a title minted between two of them lands in the archive as a `credit` row
    referencing a `title` that is not there: the restore's single transaction aborts on the
    foreign key, at the far end, on an install with nothing in it. Sequence state is not
    transactional, so reading the positions after the loop is what makes each one an upper bound
    on the ids the snapshot carried instead of a lower one — and a position below an archived id
    is precisely decision 162's collision, the restored install re-minting an id it already
    holds. `pg_current_snapshot()` is recorded because "this archive is one snapshot" is
    otherwise a claim nobody can check after the fact. Streaming is unchanged, and it stays a
    COPY rather than a `pg_dump`: the whole design is that neither end needs a binary. [M4.7
    data-04]

    Written into `<name>.partial` and renamed onto `path` only once the manifest is in and the
    zip is closed — `nightly.dump`'s discipline, for the reason `nightly.py`'s `PARTIAL` states:
    a file interrupted mid-write is a file of the right name and the wrong length. Here that is
    worse than a wasted night, because `zipfile.ZipFile(path, "w")` truncates in place and the
    COPY loop below runs for minutes: the target it was destroying was last week's archive, and
    decision 162 makes this the household's only copy of its movie data. The realistic operator
    gesture is `spielplan-movie-data write` over the archive already on the stick, so an
    interruption anywhere in the loop — the stack stopped under it, a full disk, the connection
    dropped — cost both copies at once. And it cost them silently: `close()` writes a central
    directory over whatever got through, so the truncated result opens, lists tables and reports
    a plausible size, and is discovered on the day it is restored. [M4.7
    movie-data-write-truncates-the-previous-archive]
    """
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
                    # force_zip64 because the review table alone can pass the 4 GB entry limit on
                    # a household with the full corpus, and zipfile only discovers that after the
                    # fact.
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

                # Inside the same repeatable_read snapshot as the counts above, so the two
                # halves of the sentence `_run` prints are one reading of one install rather
                # than two taken a minute apart. What is at stake is the record and not the
                # rows: section 2's nightly `pg_dump` is the whole database with no
                # `--exclude` and carries these three, which is where the printed line sends
                # the operator -- this archive could not carry them without reversing
                # decision 291, and the restore leg would decline them if it did.
                # Interpolated from this module's own literals, the way `_LOCK` below is
                # built and the restore's occupancy scan is, never from anything an operator
                # types; a migration dropping one of these tables edits `RETIRED` in the same
                # change, which is the rule `TABLES` already lives under.
                # [decisions 291, 309, 311; M4.16 cycle 4, M416-C4-GEN-08]
                for qualified in sorted(RETIRED):
                    schema, _, table = qualified.partition(".")
                    if await conn.fetchval(
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
        # Inside the guard, not after it. The rename is the one step that can fail with the whole
        # archive already on disk, and it is the step whose argument the operator typed: `write
        # /data/backups/archives` where `archives/` is the folder they keep archives in answers
        # EISDIR on Linux and WinError 5 here, both `OSError`, both caught by `_run` and printed
        # as `refusing:`. Left outside, that sentence was false in the only way that matters —
        # nothing was produced, and 312 MB of `<name>.partial` stayed in the directory section 2
        # asks them to copy off-box, where README tells them a `.partial` is cleaned up by the
        # next successful run and `nightly.prune` matches only dump names. [cycle 3 finding 10]
        partial.replace(path)
    except BaseException:
        # BaseException and not Exception: the operator stopping the stack mid-write arrives as
        # a `CancelledError`, and that is the interruption this exists for. Leaving the debris
        # behind would be its own quiet cost — `.partial` matches no reader here, so nothing
        # would ever remove it from the directory section 2 asks the operator to copy off-box.
        partial.unlink(missing_ok=True)
        raise

    return ArchiveReport(
        path=path,
        tables=counts,
        sequences=_positioned(positions),
        bytes=path.stat().st_size,
        retired=tuple(retired),
    )


# Every archived table, locked before the occupancy check and held until the restore commits.
# EXCLUSIVE and not ACCESS EXCLUSIVE: it blocks INSERT/UPDATE/DELETE, which is the writer this
# has to shut out, while a plain SELECT still reads — a restore takes minutes on the real corpus
# and there is no reason for the app's read path to stall behind it. One statement in TABLES
# order, so two restores queue rather than deadlock. [M4.7 dd18]
_LOCK = (
    "LOCK TABLE "
    + ", ".join(f'"{t.schema}"."{t.name}"' for t in TABLES)
    + " IN EXCLUSIVE MODE"
)


def _shape(manifest: dict[str, object]) -> tuple[list[dict], dict[str, dict]]:
    """The manifest's shape, or a refusal — before the restore has touched the database.

    `restore_archive`'s docstring promises it "refuses before it writes", and every other
    refusal in it kept that promise while the shape did not: `manifest["tables"]`,
    `entry["schema"]` and `manifest["sequences"]` were read straight, so a hand-edited manifest
    raised a bare `KeyError` naming a JSON key, from inside a module the operator has never
    read. The person editing a manifest by hand is by definition the person whose install is
    already broken, and the difference between "this archive is not usable" and "the restore
    crashed — is my install half-loaded?" is the whole value of the promise. [M4.7 dd18]
    """
    tables = manifest.get("tables")
    if not isinstance(tables, list) or not tables:
        raise RestoreRefused("the archive's manifest carries no `tables` list")
    for entry in tables:
        if not isinstance(entry, dict):
            raise RestoreRefused(
                f"the archive's manifest holds a table entry that is not an object: {entry!r}"
            )
        for key in ("schema", "name"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise RestoreRefused(
                    f"a table entry in the archive's manifest names no `{key}`: {entry!r}"
                )
        columns = entry.get("columns")
        if not isinstance(columns, list) or not columns or not all(
            isinstance(column, str) and column for column in columns
        ):
            raise RestoreRefused(
                f"{entry['schema']}.{entry['name']} names no column list in the archive's manifest"
            )
    sequences = manifest.get("sequences")
    if not isinstance(sequences, dict):
        raise RestoreRefused("the archive's manifest carries no `sequences` object")
    for name, position in sequences.items():
        if (
            not isinstance(position, dict)
            or not isinstance(position.get("last_value"), int)
            or not isinstance(position.get("is_called"), bool)
        ):
            raise RestoreRefused(
                f"sequence {name} in the archive's manifest carries no (last_value, is_called) "
                "position, and a restore that guessed one would re-mint an id it just loaded"
            )
    seed = manifest.get("seed")
    if seed is not None and (
        not isinstance(seed, dict)
        or not isinstance(seed.get("version"), str)
        or not all(isinstance(seed.get(key), str) for key in ("manifest", "report"))
    ):
        raise RestoreRefused(
            "the archive's manifest holds a `seed` record that is not one: it names the row that "
            "makes the restored install say it has been seeded, and decision 162's refusal keys "
            "on that row"
        )
    return tables, sequences


async def restore_archive(conn: asyncpg.Connection, path: Path) -> RestoreReport:
    """Load a movie-data archive into an install that holds no movie data.

    Refuses before it writes, in one transaction afterwards. §10's swap sequence is
    "validate -> stage -> recompute -> flip" precisely because the alternative — discovering the
    problem halfway through — leaves an install that is neither the old one nor the new one, and
    a content spine is not something an operator can eyeball for completeness.

    "Before it writes" and "in one transaction" have to mean the same instant, and they did not.
    The occupancy check ran outside the transaction and took no lock, so it read a fact that was
    only true when it was read: with the COPY paused and two titles minted on a second
    connection, the restore completed with four title rows in a table it had declared empty, the
    absolute `setval` then wound `title_id_seq` back under both of them, and the next
    acquisition raised a duplicate key on `title_pkey`. The lock is what makes the refusal a
    decision rather than an observation — held from before the check until the commit, so no row
    can appear between the check, the COPY and the `setval`. That is also why the `setval` stays
    absolute: it is the archive's own position, and with the lock held nothing else has minted
    from these sequences in between. [M4.7 dd18]
    """
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        # The manifest is the last thing `write_archive` puts in, so "no manifest" is precisely
        # the shape an interrupted write used to leave on top of the previous archive — the file
        # an operator would reach for first. `archive.read` answers a missing member with a
        # `KeyError`, which is neither of the two exceptions `_run` catches, so the documented
        # "refusing: ..." was a traceback naming a zip path instead. [M4.7
        # movie-data-restore-keyerror-not-restore-refused]
        if MANIFEST not in members:
            raise RestoreRefused(
                f"the archive carries no {MANIFEST}: it is not a movie-data archive, or it is "
                "one whose write was interrupted before the manifest was added"
            )
        # And "no manifest" is not the only shape a hand-edited archive takes. `json.loads`
        # answers a member that is not parseable with a `JSONDecodeError` — a `ValueError`, which
        # is none of the three exceptions `_run` catches, so the operator who opened the manifest
        # to see what it said and saved it with a stray comma got a decoder traceback out of
        # `json/decoder.py` instead of the refusal this docstring promises. Same gesture, same
        # person, one step further in than the branch above. [cycle 3 finding 11]
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except ValueError as exc:
            raise RestoreRefused(
                f"the archive's {MANIFEST} is not readable JSON: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise RestoreRefused("the archive's manifest is not a JSON object")
        if manifest.get("format") != FORMAT:
            raise RestoreRefused(
                f"archive format {manifest.get('format')!r} is not {FORMAT}: this build cannot "
                "read it, and reading it wrongly would load a content spine nobody can check"
            )

        entries, carried_positions = _shape(manifest)
        known = {t.qualified for t in TABLES}
        # Subtracted once, here, so that everything below -- the table-set check, the member
        # check, the occupancy scan and the COPY loop -- sees the archive as this build would
        # have written it. The occupancy scan going with them is deliberate and not a hole: an
        # install holding genome rows holds titles too (the importer never filled one without
        # the other), `title` is scanned either way, and the refusal names it. [decision 309]
        retired = sorted({f"{e['schema']}.{e['name']}" for e in entries} & RETIRED)
        entries = [e for e in entries if f"{e['schema']}.{e['name']}" not in RETIRED]
        named = [f"{e['schema']}.{e['name']}" for e in entries]
        # Both directions. A table this build does not archive has no business being COPYed
        # into the install from a file, and a table it does archive going missing from the
        # manifest is how a restore quietly produces a spine with no reviews in it.
        if set(named) != known:
            raise RestoreRefused(
                "the archive's table set does not match this build's: "
                f"unknown {sorted(set(named) - known)}, missing {sorted(known - set(named))}"
            )

        # The manifest describes the zip; it is not the zip. Everything above validates the
        # description, and `archive.open` below is where the archive itself gets a say — with a
        # `KeyError` for a member that is not there, raised inside the transaction, at the far
        # end of a lock held over every archived table. The rollback keeps the promise, but what
        # the operator reads is a traceback, and "the restore crashed" and "this archive is not
        # usable" are not the same sentence. Checked here rather than at the top so the answer
        # names the table: the manifest's own table set is what says which entries to expect.
        # [M4.7 movie-data-restore-keyerror-not-restore-refused]
        absent = [name for name in named if _entry(name) not in members]
        if absent:
            raise RestoreRefused(
                "the archive's manifest names tables the archive does not carry: "
                f"{', '.join(sorted(absent))}"
            )

        # The manifest is an input, not a fact: an archive arrives on a stick, over a channel
        # nobody controls, or edited by an operator who was told it would help. Every other value
        # read out of it is bounded by the table set above, but the sequence names are executed
        # by `setval` — so they are constrained to the ones this build's own archived tables own,
        # read from the catalog on this side. Winding `app_user_id_seq` back is not a content
        # problem, and nothing downstream of a restore would have reported it.
        #
        # Both directions, for the same reason the table set is checked both ways: a missing
        # sequence is decision 162's quiet catastrophe, an install that re-mints ids it holds.
        #
        # `_layout` reads this install's own catalog, and its two `RuntimeError`s are refusals in
        # everything but type: a database the migrations have not reached carries none of the
        # archived tables. That is the shape of the restore README's Recovery block describes — a
        # rebuilt box, the archive to hand, the backend that applies migrations not started yet —
        # and it arrived as a traceback naming every archived table, for the same reason the
        # manifest branches above did. [cycle 3 finding 11]
        try:
            owned = set((await _layout(conn))[1])
        except RuntimeError as exc:
            raise RestoreRefused(str(exc)) from exc
        carried = set(carried_positions)
        if carried != owned:
            raise RestoreRefused(
                "the archive's sequences are not the ones this build's tables own: "
                f"unknown {sorted(carried - owned)}, missing {sorted(owned - carried)}"
            )

        seed = manifest.get("seed")
        counts: dict[str, int] = {}
        async with conn.transaction():
            await conn.execute(_LOCK)
            # Inside the transaction and behind the lock, because "this install holds no movie
            # data" is a fact with a lifetime: read outside, it was true when it was read and
            # false by the time the COPY relied on it.
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

            # Decision 162's other half. Without this the restored install mints from the
            # sequence's declared START and collides with the ids it just loaded.
            for sequence, position in carried_positions.items():
                await conn.execute(
                    "SELECT setval($1::regclass, $2::bigint, $3::boolean)",
                    sequence, position["last_value"], position["is_called"],
                )

            if seed is not None:
                # 'superseded' rather than 'active': the archive carries rows, never the
                # artifacts tree, and `ArtifactStore.load_active` reads an active row whose files
                # are absent as a broken install. The restored install is seeded and has no
                # basis — which is exactly decision 162's order, content first and then a model
                # bundle on top of it. No ON CONFLICT: a version already present means this
                # archive does not belong to this install, and the transaction should say so
                # rather than complete with the seed marker silently missing.
                #
                # `::text::jsonb` and not `::jsonb`: the latter types the parameter as jsonb, and
                # `db/pool.py` registers `json.dumps` as the jsonb encoder — so the app's own
                # connection would encode the manifest a second time and store a JSON *string*
                # where every reader expects an object.
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
    """The app's own connection setup, for a command that runs beside the app.

    `db/pool.open_pool` rather than a bare `asyncpg.connect`, because this module is written for
    a connection that has the pool's codecs: the restore's `::text::jsonb` cast (see below)
    exists precisely to survive `db/pool.py` registering `json.dumps` as the jsonb encoder. A
    command that opened a codec-less connection would exercise a path the app never runs, and
    the cast would be dead code defending against nothing an operator could hit.
    """
    pool = await db_pool.open_pool(min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        # Not the app's process: the pool is this command's, and leaving it open would hold a
        # connection against a database an operator may be about to stop.
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
                    # Conditional, for the reason the restore branch below is: a box this
                    # build seeded holds nothing to name, and a clause printed on every write
                    # is a clause nobody reads by the third one. Named rather than counted --
                    # "3 tables were passed over" is a number the operator cannot check
                    # against the install in front of them. [decisions 291, 309, 311]
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
            # A refusal is the documented outcome, not a crash: decision 162 makes "this install
            # already holds movie data" an ordinary thing for an operator to discover.
            #
            # `OSError` beside the two, because the destination is the one thing the operator
            # types and `write_archive` opens it before anything else it does. Every way of
            # getting it wrong arrives here: /data/backups is mounted on the worker alone, so the
            # same command run in the backend container is uid 1000 against a root-owned /data
            # and raises PermissionError from `path.parent.mkdir`; an unmounted stick, a
            # read-only mount and a full disk are the same shape. Uncaught, each was a traceback
            # out of a module the operator has never read. "refusing" is the honest word for all
            # of them: `write_archive` renames `<name>.partial` onto the target only when the
            # archive is whole, so nothing was produced and last week's copy is untouched.
            # [M4.7 cycle 2 finding 13]
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
    """`spielplan-movie-data`. Spec v2.1 §2, §10; decision 162.

    An operator command and not an admin route, for now: the restore is a stop-the-stack event
    (it locks every archived table for its duration) and the write is minutes of COPY, neither
    of which belongs behind a request. §6.6's System tab can grow a button on top of these two
    functions once it exists. [M4.7 spec-07]
    """
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
