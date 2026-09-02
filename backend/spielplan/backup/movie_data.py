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
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

# Bumped when the layout changes in a way a reader cannot infer. A restore refuses a format it
# does not know rather than mis-reading it: half a restored content spine is worse than none.
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
    # The MovieLens genome + link slice named in §10's manifest.
    Table("public", "ml_genome_tag"),
    Table("public", "ml_link"),
    Table("public", "ml_genome_score"),
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
DROPPED_COLUMNS: dict[str, frozenset[str]] = {
    "public.title": frozenset({"placement_bundle"}),
}


@dataclass(frozen=True)
class ArchiveReport:
    path: Path
    tables: dict[str, int]
    sequences: dict[str, int]
    bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "bytes": self.bytes,
            "rows": sum(self.tables.values()),
            "tables": len(self.tables),
            "sequences": self.sequences,
        }


@dataclass(frozen=True)
class RestoreReport:
    path: Path
    tables: dict[str, int]
    sequences: dict[str, int]
    seeded: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "rows": sum(self.tables.values()),
            "tables": len(self.tables),
            "sequences": self.sequences,
            "seeded": self.seeded,
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


async def _layout(conn: asyncpg.Connection) -> tuple[dict[str, list[str]], list[str]]:
    rows = await conn.fetch(
        _COLUMNS, [t.schema for t in TABLES], [t.name for t in TABLES]
    )
    columns: dict[str, list[str]] = {}
    sequences: set[str] = set()
    dropped: set[str] = set()
    for row in rows:
        key = f"{row['table_schema']}.{row['table_name']}"
        if row["column_name"] in DROPPED_COLUMNS.get(key, frozenset()):
            dropped.add(f"{key}.{row['column_name']}")
            continue
        columns.setdefault(key, []).append(row["column_name"])
        if row["sequence"]:
            sequences.add(row["sequence"])
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
    return columns, sorted(sequences)


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


async def write_archive(conn: asyncpg.Connection, path: Path) -> ArchiveReport:
    """Write the movie-data archive at `path`.

    Streamed table by table into the zip rather than buffered: §10 sizes the review store at
    312 MB with bodies, and an appliance with 6-8 GB of RAM does not get to hold that twice.
    """
    columns, sequences = await _layout(conn)
    created_at = datetime.now(UTC).isoformat()
    seed = await _seed_record(conn, created_at)
    positions: dict[str, dict[str, object]] = {}
    for sequence in sequences:
        row = await conn.fetchrow(f"SELECT last_value, is_called FROM {sequence}")
        positions[sequence] = {"last_value": row["last_value"], "is_called": row["is_called"]}

    counts: dict[str, int] = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for table in TABLES:
            # force_zip64 because the review table alone can pass the 4 GB entry limit on a
            # household with the full corpus, and zipfile only discovers that after the fact.
            with archive.open(_entry(table.qualified), "w", force_zip64=True) as stream:
                status = await conn.copy_from_table(
                    table.name,
                    schema_name=table.schema,
                    columns=columns[table.qualified],
                    output=stream,
                )
            counts[table.qualified] = _rows(status)

        archive.writestr(
            MANIFEST,
            json.dumps(
                {
                    "format": FORMAT,
                    "kind": "movie-data",
                    "created_at": created_at,
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

    return ArchiveReport(
        path=path,
        tables=counts,
        sequences=_positioned(positions),
        bytes=path.stat().st_size,
    )


async def restore_archive(conn: asyncpg.Connection, path: Path) -> RestoreReport:
    """Load a movie-data archive into an install that holds no movie data.

    Refuses before it writes, in one transaction afterwards. §10's swap sequence is
    "validate -> stage -> recompute -> flip" precisely because the alternative — discovering the
    problem halfway through — leaves an install that is neither the old one nor the new one, and
    a content spine is not something an operator can eyeball for completeness.
    """
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get("format") != FORMAT:
            raise RestoreRefused(
                f"archive format {manifest.get('format')!r} is not {FORMAT}: this build cannot "
                "read it, and reading it wrongly would load a content spine nobody can check"
            )

        known = {t.qualified for t in TABLES}
        named = [f"{e['schema']}.{e['name']}" for e in manifest["tables"]]
        # Both directions. A table this build does not archive has no business being COPYed
        # into the install from a file, and a table it does archive going missing from the
        # manifest is how a restore quietly produces a spine with no reviews in it.
        if set(named) != known:
            raise RestoreRefused(
                "the archive's table set does not match this build's: "
                f"unknown {sorted(set(named) - known)}, missing {sorted(known - set(named))}"
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
        owned = set((await _layout(conn))[1])
        carried = set(manifest["sequences"])
        if carried != owned:
            raise RestoreRefused(
                "the archive's sequences are not the ones this build's tables own: "
                f"unknown {sorted(carried - owned)}, missing {sorted(owned - carried)}"
            )

        for entry in manifest["tables"]:
            occupied = await conn.fetchval(
                f'SELECT EXISTS (SELECT 1 FROM "{entry["schema"]}"."{entry["name"]}")'
            )
            if occupied:
                raise RestoreRefused(
                    f"{entry['schema']}.{entry['name']} already holds rows: a movie-data restore "
                    "targets an install with no movie data in it (decision 162 — content seeds "
                    "once), and merging two spines is not something this can do silently"
                )

        counts: dict[str, int] = {}
        async with conn.transaction():
            for entry in manifest["tables"]:
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
            for sequence, position in manifest["sequences"].items():
                await conn.execute(
                    "SELECT setval($1::regclass, $2::bigint, $3::boolean)",
                    sequence, position["last_value"], position["is_called"],
                )

            seed = manifest.get("seed")
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
                    seed["vocabulary_version"],
                )

    return RestoreReport(
        path=path,
        tables=counts,
        sequences=_positioned(manifest["sequences"]),
        seeded=None if seed is None else seed["version"],
    )


__all__ = [
    "TABLES",
    "ArchiveReport",
    "RestoreRefused",
    "RestoreReport",
    "Table",
    "restore_archive",
    "write_archive",
]
