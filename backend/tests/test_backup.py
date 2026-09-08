"""Backup and restore. Spec v2.1 §2 (Backups), §4.1, §4.3, §10; decision 162.

Two artifacts, and they are deliberately not the same thing.

§2's nightly `pg_dump` is the whole database, user state included, written to `/data/backups`
and rotated to fourteen. Its contract is a negative one: "Dumps contain ciphertext only — back
up the env file (`SECRETS_KEY`) alongside them, or a restored dump cannot decrypt connector
config." §14.3 is why that sentence has teeth — a Jellyfin API key is unscoped and
admin-equivalent, so a dump carrying one in the clear is a media-server credential lying in a
directory the operator rsyncs off-box.

The movie-data archive is the other half of decision 162. The corpus supplies content once and
never again, so the household's copy of the movie data is the only copy: it has to come out on
its own, without the user state, and go back into a fresh install. Two failure modes make that
testable rather than obvious, and both are silent. The id sequences are positioned by the seed
import, an event that by definition never runs again, so a restore that does not carry `setval`
mints id 1 and the restored install cannot acquire a single title. And `title.origin` defaults
to 'bundle' (`0008_placement.sql:47`), so a restore that drops the column re-labels every
app-acquired title and §10's rebuild set stops naming them.

The dump tests need a real `pg_dump`, and they name the binary when they cannot find one. They
also carry a positive control — a known content string that MUST be in the artifact — because
"the secret is not in this file" is satisfied for free by a file dumped from the wrong database,
or by no file at all.

Three more things are only visible from the far side of a restore, and each was found by
reading rather than by a failing test. A restored install has no `artifact_bundle` row, so
decision 162's "content seeds once" refusal has nothing to fire on and the install is
re-seedable — the two-minters problem, reintroduced by the recovery path. `title.placement_bundle`
points at a bundle that fresh install does not have, so the archive that carries it cannot be
restored at all. And the archive's manifest is an input: an install must not execute a sequence
name because a file asked it to.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from spielplan.backup import movie_data, nightly
from spielplan.core import secrets as core_secrets
from spielplan.core.config import settings
from spielplan.db import migrate
from spielplan.importer.bundle import Bundle, refuse_on_install_state
from spielplan.importer.report import ImportReport

# The compose readers, borrowed rather than re-written, the way `test_worker_registry.py` borrows
# them: the question "can the container this command is documented in write the archive" is a
# question about the mount list the service resolves, alias and comments included. [M4.7 spec-07]
from tests.test_static_contracts import COMPOSE, _mounts

_REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.anyio

# Planted in the tables the archive must not touch. Distinct enough that a substring search over
# a whole artifact is meaningful, and ASCII so a failure prints on a cp1252 console.
MARKER_SECRET = "MARKER-JELLYFIN-ADMIN-KEY-3f9c"
MARKER_VERDICT = "MARKER-VERDICT-SOURCE"
MARKER_USER = "MARKER-MEMBER"
MARKER_SESSION = "MARKER-SESSION-COOKIE"
MARKER_PASSKEY = "MARKER-PASSKEY-LABEL"
MARKER_PUSH = "MARKER-PUSH-ENDPOINT"

# The positive control: content that MUST survive into every artifact under test.
CONTENT_MARKER = "Waechter der Naecht"

# Where the app-minted range starts (0015_seed.sql, decision 162). The seed import positions the
# sequences inside it; these tests position them by hand, because the importer is another row.
APP_ID_FLOOR = 1_000_000_000

# --- what the archive must leave behind -------------------------------------------------------
#
# Grouped by the reason, because the reason is the interesting part. Together with the archive's
# own table list this covers the schema exhaustively, and the guard below fails when a new table
# belongs to neither set — which is this milestone's own lesson: an unmapped *table* was
# invisible, because the import report only tracked unmapped columns within mapped tables.

USER_STATE = {
    "app_user", "user_title", "verdict", "duel", "tier_edit", "ledger_state",
    "ledger_cutpoints", "user_vector", "ledger_fit", "user_score", "playback_event",
    "acquisition_job", "rate_session", "rate_observation", "session", "session_participant",
    "session_answer", "session_ballot", "session_result", "session_outcome", "auth_session",
    "webauthn_credential", "webauthn_challenge", "push_subscription",
}
SECRET_CUSTODY = {"connector_config", "data_encryption_key", "app_setting"}
# §10: "everything expressed in the old Backbone's basis is garbage against a new one" — these
# are recomputed by the rebuild set, so carrying them would ship a stale basis into a restore.
BUNDLE_DERIVED = {"artifact_bundle", "title_placement", "title_prior"}
# Install bookkeeping and §8.4's work queue: app state, not movie data. `job_run` (0017_ops.sql)
# joins them for the same reason — §6.6 System reads it to say when last night's dump succeeded on
# *this* box, and a restore that carried another install's job history would report backups that
# never happened here.
APP_STATE = {"schema_migration", "setup_step", "flywheel_item", "job_run"}

EXCLUDED = USER_STATE | SECRET_CUSTODY | BUNDLE_DERIVED | APP_STATE


# --- the postgres client binaries -------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _postgres_container() -> str | None:
    """The one running postgres:16 container, if there is exactly one.

    The app image carries `postgresql-client-16` (ops/backend.Dockerfile) so the worker can run
    §2's dump; a development box need not, and this one does not. The database behind
    TEST_DATABASE_URL *is* that container, and inside it 127.0.0.1:5432 names the same server the
    URL does — so the same binary, reached through `docker exec`, dumps the same database.
    Exactly one match or nothing: guessing which of several servers to dump would turn a wrong
    answer into a green test.
    """
    try:
        done = subprocess.run(
            ["docker", "ps", "--filter", "ancestor=postgres:16", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = [name for name in done.stdout.split() if name]
    return names[0] if done.returncode == 0 and len(names) == 1 else None


def _client(binary: str) -> tuple[str, ...]:
    found = shutil.which(binary)
    if found:
        return (found,)
    container = _postgres_container()
    if container:
        # `-e PGPASSWORD` with no value forwards the variable from this process. §14.3 is why
        # `dump()` puts the password there rather than on the command line, and a `docker exec`
        # stand-in that dropped it would exercise a path production does not have.
        return ("docker", "exec", "-i", "-e", "PGPASSWORD", container, binary)
    pytest.skip(
        f"{binary} is not on PATH and no single postgres:16 container is running: "
        f"the nightly dump (spec section 2) cannot be exercised without the {binary} binary"
    )


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    done = subprocess.run(argv, capture_output=True, timeout=600, check=False, **kwargs)
    assert done.returncode == 0, (
        f"{argv[:3]} exited {done.returncode}: "
        f"{done.stderr.decode('utf-8', 'replace')[-2000:]}"
    )
    return done


# --- fixtures ---------------------------------------------------------------------------------


def _sibling(pg_url: str, suffix: str) -> tuple[str, str, str]:
    """(admin url, database name, url) for a database next to the test one."""
    parts = urlsplit(pg_url)
    name = (parts.path.lstrip("/") + suffix)[:62]
    return (
        urlunsplit(parts._replace(path="/postgres")),
        name,
        urlunsplit(parts._replace(path=f"/{name}")),
    )


async def _recreate(admin: str, name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop(admin: str, name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture
def backup_env(pg_url, tmp_path, monkeypatch):
    """The worker's view of the world: DATABASE_URL and DATA_DIR as the container sets them.

    Everything but the household clock is configuration, exactly as it is in production; the
    clock is `run()`'s one argument, and these tests hand it `datetime.now(UTC)` because a
    household on UTC is the case where nothing about the date basis is interesting.
    """
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(nightly, "PG_DUMP", _client("pg_dump"))
    settings.cache_clear()
    yield tmp_path
    settings.cache_clear()


@pytest.fixture
async def empty_install(pg_url):
    """A second, freshly migrated database — the "empty install" the restore has to land in.

    Restoring over the source database would prove nothing: the rows are already there and the
    sequence is already positioned. Only a database that has never seen the seed can show that
    the archive carries the positions rather than assuming them.

    It carries `db/pool.py`'s json codecs because the app's connection does. Without them a
    restore that hands a JSON *string* to a jsonb column looks correct here and stores a
    double-encoded manifest in production — a test connection that is not shaped like the real
    one tests a code path nobody runs.
    """
    import asyncpg

    admin, name, url = _sibling(pg_url, "_restore")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    try:
        await migrate.apply_all(conn)
        for typename in ("json", "jsonb"):
            await conn.set_type_codec(
                typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
        yield conn
    finally:
        await conn.close()
        await _drop(admin, name)


@pytest.fixture
async def blank_url(pg_url):
    """An empty database with no schema at all, and no connection held open on it.

    `pg_restore` of a whole-database dump wants a target it can create schemas in; a migrated
    one already has them, and dropping them from inside a live connection is a different test's
    accident waiting to happen.
    """
    admin, name, url = _sibling(pg_url, "_pgr")
    await _recreate(admin, name)
    try:
        yield url
    finally:
        await _drop(admin, name)


async def _seed_movie_data(conn) -> None:
    """A small world with a row in each layer the row names: spine, DNA, reviews."""
    await conn.execute(
        "INSERT INTO title (id, kind, name, year, origin) VALUES "
        "(11, 'movie', $1, 1979, 'bundle'), (12, 'series', 'Der Zweite', 1988, 'bundle')",
        CONTENT_MARKER,
    )
    await conn.execute(
        "INSERT INTO person (id, name) VALUES (5, 'Ada Lovelace'), (6, 'Nino Rota')"
    )
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job) "
        "VALUES (11, 5, 'Directing', 'Director'), (11, 6, 'Sound', 'Composer')"
    )
    await conn.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES (11, 'tmdb', $1::jsonb)",
        json.dumps({"tagline": "a tagline"}),
    )
    await conn.execute("INSERT INTO title_genre (title_id, genre) VALUES (11, 'Drama')")
    await conn.execute(
        "INSERT INTO display.platform_rating (title_id, platform, score) "
        "VALUES (11, 'imdb', 7.4)"
    )
    await conn.execute(
        "INSERT INTO review_store.review (title_id, source, body) "
        "VALUES (11, 'trakt', 'the ferry scene is the whole film')"
    )
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 1)"
    )
    await conn.execute("INSERT INTO dna_facet (version, facet, ord) VALUES ('v1', 'mood', 0)")
    await conn.execute(
        "INSERT INTO dna_term (version, term, facet) VALUES ('v1', 'melancholy', 'mood')"
    )
    tag_id = await conn.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) "
        "VALUES (11, 'v1', 'melancholy', 'mood', 3) RETURNING id"
    )
    await conn.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'trakt:comment')",
        tag_id, "the ferry scene is the whole film",
    )
    await conn.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES (12, 'v1', 'melancholy', 'mood', 0.4, 'keyword:rain')"
    )


async def _seed_user_state(conn) -> None:
    """One row in every table the movie-data archive must not carry.

    Where a table has free text the row plants a marker, so the exclusion can be checked byte by
    byte rather than by trusting the table list. `duel`, `tier_edit` and `ledger_state` have no
    free text at all — every column is an id, an enum or a number — so for those the table list
    is the only assertion available, and it is made explicitly.
    """
    user_id = await conn.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, 'member') RETURNING id", MARKER_USER
    )
    await conn.execute(
        "INSERT INTO verdict (user_id, title_id, value, source) VALUES ($1, 11, 2, $2)",
        user_id, MARKER_VERDICT,
    )
    await conn.execute(
        "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
        "VALUES ($1, 11, 12, 'A', 'profile_battle')",
        user_id,
    )
    await conn.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, 11, 5, 'explicit')",
        user_id,
    )
    await conn.execute(
        "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma) "
        "VALUES ($1, 11, 'movie', 0.8, 0.1)",
        user_id,
    )
    await conn.execute(
        "INSERT INTO auth_session (id, user_id, expires_at, auth_method) "
        "VALUES ($1, $2, now() + interval '1 day', 'passkey')",
        MARKER_SESSION, user_id,
    )
    await conn.execute(
        "INSERT INTO webauthn_credential (credential_id, user_id, public_key, label, rp_id) "
        "VALUES ($1, $2, $3, $4, 'localhost')",
        b"cred-1", user_id, b"pubkey-1", MARKER_PASSKEY,
    )
    await conn.execute(
        "INSERT INTO push_subscription (user_id, endpoint, p256dh, auth) "
        "VALUES ($1, $2, 'p', 'a')",
        user_id, f"https://push.example/{MARKER_PUSH}",
    )


async def _seed_connector_secret(conn, monkeypatch) -> bytes:
    """A real sealed connector secret, through the real §2 custody path."""
    monkeypatch.setenv("SECRETS_KEY", "test-secrets-key-not-a-real-one-at-all")
    settings.cache_clear()
    await core_secrets.put_connector_secrets(
        conn, "jellyfin", {"url": "http://jellyfin.local:8096"}, {"api_key": MARKER_SECRET}
    )
    return bytes(
        await conn.fetchval(
            "SELECT secrets_encrypted FROM connector_config WHERE name = 'jellyfin'"
        )
    )


def _archive_bytes(path: Path) -> bytes:
    """Every byte the archive holds, decompressed, entry names included.

    Searching the zip file itself would search compressed bytes, where a leaked plaintext secret
    is invisible for the wrong reason.
    """
    blob = bytearray()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            blob += info.filename.encode("utf-8")
            blob += zf.read(info.filename)
    return bytes(blob)


# --- the movie-data archive (platform-movie-data-backup-and-restore) --------------------------


async def test_the_archive_carries_the_content_spine_the_dna_layer_and_the_review_store(
    db, tmp_path
):
    """Decision 162: content arrives once, so the household's copy is the only copy.

    An archive that quietly held the spine and skipped the reviews would restore an install that
    can never re-extract or re-embed anything — §10 ships the review bodies for exactly that —
    and nothing downstream would say so.
    """
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    for table in ("public.title", "public.person", "public.credit", "public.title_meta",
                  "public.dna_tag", "public.dna_evidence", "public.dna_projected",
                  "display.platform_rating", "review_store.review"):
        assert report.tables.get(table), f"{table} is missing or empty in the archive"

    assert CONTENT_MARKER.encode("utf-8") in _archive_bytes(report.path)


async def test_the_archive_carries_nothing_user_specific(db, tmp_path, monkeypatch):
    """The row's negative half, asserted twice over.

    The table list is the structural claim; the byte search is the one that survives a mistake in
    the table list — a join that dragged a verdict along would satisfy the first and fail the
    second. The content marker is the control: without it, an empty archive passes every absence
    check below for free.
    """
    await _seed_movie_data(db)
    await _seed_user_state(db)
    ciphertext = await _seed_connector_secret(db, monkeypatch)

    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    archived = {name.split(".", 1)[1] for name in report.tables}
    assert not archived & EXCLUDED, sorted(archived & EXCLUDED)

    blob = _archive_bytes(report.path)
    assert CONTENT_MARKER.encode("utf-8") in blob, "the archive is empty; every absence is free"
    for marker in (MARKER_SECRET, MARKER_VERDICT, MARKER_USER, MARKER_SESSION,
                   MARKER_PASSKEY, MARKER_PUSH):
        assert marker.encode("utf-8") not in blob, marker
    assert ciphertext not in blob


async def test_every_table_is_either_archived_or_deliberately_left_out(db):
    """M4.5's own lesson, applied to this artifact: an unmapped *table* is invisible.

    `title_meta` (46,318 rows), `title_list_membership` and `imdb_ratings` were loaded by nothing
    and reported by nothing, because the import report tracked unmapped columns within mapped
    tables and had no way to say "a whole table went missing". A backup has the same hole one
    milestone later, so a new table has to be classified rather than defaulted.
    """
    rows = await db.fetch(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' "
        "  AND table_schema IN ('public', 'display', 'review_store')"
    )
    present = {f"{r['table_schema']}.{r['table_name']}" for r in rows}
    archived = {f"{t.schema}.{t.name}" for t in movie_data.TABLES}
    excluded = {name for name in present if name.split(".", 1)[1] in EXCLUDED}

    assert archived <= present, f"the archive names tables the schema lacks: {archived - present}"
    unclassified = present - archived - excluded
    assert not unclassified, (
        "a table is neither in the movie-data archive nor deliberately left out of it: "
        f"{sorted(unclassified)}. Add it to spielplan/backup/movie_data.py's TABLES, or to this "
        "file's EXCLUDED set with the reason, but do not let it vanish without either."
    )


async def test_a_restore_into_an_empty_install_reproduces_the_title_person_and_dna_rows(
    db, tmp_path, empty_install
):
    """The row's positive half, end to end and across two databases."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    restored = await movie_data.restore_archive(empty_install, report.path)
    assert restored.tables == report.tables

    for query in (
        "SELECT id, kind, name, year FROM title ORDER BY id",
        "SELECT id, name FROM person ORDER BY id",
        "SELECT title_id, version, term, facet, salience FROM dna_tag ORDER BY title_id, term",
        "SELECT title_id, version, term, weight FROM dna_projected ORDER BY title_id, term",
        "SELECT quote, source FROM dna_evidence ORDER BY quote",
        "SELECT title_id, source, body FROM review_store.review ORDER BY title_id, source",
        "SELECT title_id, source, payload::text FROM title_meta ORDER BY title_id, source",
    ):
        before = [dict(r) for r in await db.fetch(query)]
        after = [dict(r) for r in await empty_install.fetch(query)]
        assert before == after, query

    assert await empty_install.fetchval("SELECT name FROM title WHERE id = 11") == CONTENT_MARKER


async def test_a_restore_carries_the_sequence_positions_forward(db, tmp_path, empty_install):
    """Decision 162's quiet catastrophe.

    `title_id_seq` is positioned by the seed import — 0015_seed.sql says so out loud and refuses
    to position it itself — and the seed import by definition never runs again. A restore that
    does not carry `setval` leaves the sequence at its `START 1000000000`, so the first
    acquisition after the restore re-mints an id the archive already used: a §7.2 add landing on
    top of a film the household already owns.
    """
    await _seed_movie_data(db)
    # What the seed import leaves behind: the sequences sitting inside the app's own range.
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    await db.execute("SELECT setval('person_id_seq', $1, true)", APP_ID_FLOOR)
    acquired_id = await db.fetchval(
        "INSERT INTO title (kind, name, origin) VALUES ('movie', 'Acquired', 'acquired') "
        "RETURNING id"
    )
    acquired_person = await db.fetchval(
        "INSERT INTO person (name) VALUES ('Minted After The Seed') RETURNING id"
    )
    assert (acquired_id, acquired_person) == (APP_ID_FLOOR + 1, APP_ID_FLOOR + 1)

    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    await movie_data.restore_archive(empty_install, report.path)

    restored_ids = {r["id"] for r in await empty_install.fetch("SELECT id FROM title")}
    restored_people = {r["id"] for r in await empty_install.fetch("SELECT id FROM person")}

    # Twice, because a sequence that rewound by one still mints one free id before it lands on
    # the row it already restored — and "the first acquisition works" is exactly the check that
    # would have let that through.
    for nth in range(2):
        minted = await empty_install.fetchval(
            "INSERT INTO title (kind, name) VALUES ('movie', $1) RETURNING id",
            f"After The Restore {nth}",
        )
        assert minted not in restored_ids, f"acquisition {nth} re-minted title id {minted}"
        assert minted > acquired_id
        minted_person = await empty_install.fetchval(
            "INSERT INTO person (name) VALUES ($1) RETURNING id", f"After The Restore {nth}"
        )
        assert minted_person not in restored_people, f"re-minted person id {minted_person}"
        assert minted_person > APP_ID_FLOOR


async def test_a_restore_preserves_title_origin(db, tmp_path, empty_install):
    """§10's rebuild set names "Cold Tower re-placement of every app-acquired title", and
    `reconcile.py` finds them with `WHERE origin = 'acquired'`.

    `origin` defaults to 'bundle', so an archive that dropped the column would restore silently,
    correctly-looking, and with the rebuild set permanently empty — every app-acquired title
    keeping a coordinate computed in a basis §10 calls garbage.
    """
    await _seed_movie_data(db)
    await db.execute(
        "INSERT INTO title (id, kind, name, origin) VALUES ($1, 'movie', 'Acquired', 'acquired')",
        APP_ID_FLOOR + 7,
    )
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    await movie_data.restore_archive(empty_install, report.path)

    acquired = [
        r["id"]
        for r in await empty_install.fetch("SELECT id FROM title WHERE origin = 'acquired'")
    ]
    assert acquired == [APP_ID_FLOOR + 7]
    assert await empty_install.fetchval("SELECT count(*) FROM title WHERE origin = 'bundle'") == 2


async def test_a_restore_refuses_an_install_that_already_holds_movie_data(
    db, tmp_path, empty_install
):
    """COPY into a populated table fails halfway and leaves the install neither one thing nor the
    other. §10's swap sequence is explicit that this kind of event is validated before it writes,
    so the refusal names the table it found rows in."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    await empty_install.execute(
        "INSERT INTO title (id, kind, name) VALUES (99, 'movie', 'Already Here')"
    )

    with pytest.raises(movie_data.RestoreRefused, match="title"):
        await movie_data.restore_archive(empty_install, report.path)
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 1


async def test_a_restore_into_an_empty_install_carries_no_placement_basis(
    db, tmp_path, empty_install
):
    """`title.placement_bundle REFERENCES artifact_bundle(version)` (0008_placement.sql:59) and
    the archive carries no `artifact_bundle` row, because §10 calls a coordinate expressed in the
    old basis garbage against a new one.

    So an archive taken from any install that has ever placed a title cannot be restored at all:
    every carried `placement_bundle` names a version the fresh install has no row for. That is
    not a rare corner — it is every real household, and it makes the recovery path untestable by
    the very fixture that would have caught it, because the fixture never placed anything.
    """
    await _seed_movie_data(db)
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ('v20260828', '{}'::jsonb, 'active', 'seed')"
    )
    await db.execute("UPDATE title SET placement_bundle = 'v20260828', placement = 'warm'")

    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    restored = await movie_data.restore_archive(empty_install, report.path)

    assert await empty_install.fetchval("SELECT count(*) FROM title") == 2
    assert await empty_install.fetchval(
        "SELECT count(*) FROM title WHERE placement_bundle IS NOT NULL"
    ) == 0
    # The bundle is not carried, but which bundle seeded this household is provenance the only
    # surviving copy of the content should not lose.
    assert restored.seeded == "v20260828"
    assert await empty_install.fetchval(
        "SELECT jsonb_typeof(manifest) FROM artifact_bundle WHERE kind = 'seed'"
    ) == "object", "the seed record's manifest was encoded twice"


async def test_a_restored_install_refuses_a_second_content_seed(db, tmp_path, empty_install):
    """Decision 162's refusal is keyed on `artifact_bundle WHERE kind = 'seed'`, and a restore
    that carries rows but no such row leaves the install saying it has never been seeded.

    The importer then accepts a content bundle over a full spine — two minters in one id
    namespace, which is the exact failure decision 162 exists to prevent, arriving through the
    recovery path rather than through the importer.
    """
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    await movie_data.restore_archive(empty_install, report.path)

    refusal = ImportReport()
    await refuse_on_install_state(
        empty_install,
        Bundle(
            root=tmp_path,
            version="v20260901",
            content_db=tmp_path / "content.sqlite",
            reviews_db=None,
            artifacts_dir=tmp_path / "artifacts",
        ),
        refusal,
    )
    assert [f.rule for f in refusal.failures] == ["seed-once"]

    # Seeded, not *active*: the archive carries rows, never the artifacts tree. An 'active' row
    # naming a version with no files under /data/artifacts is `ArtifactStore.load_active`'s
    # "broken install" branch, which is a worse lie than the one being fixed.
    assert await empty_install.fetchval(
        "SELECT state FROM artifact_bundle WHERE kind = 'seed'"
    ) != "active"


def _with_edited_manifest(source: Path, target: Path, edit) -> Path:
    """The archive as a hostile input: same entries, manifest rewritten by hand."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            blob = src.read(info.filename)
            if info.filename == movie_data.MANIFEST:
                manifest = json.loads(blob)
                edit(manifest)
                blob = json.dumps(manifest).encode("utf-8")
            dst.writestr(info.filename, blob)
    return target


async def test_a_restore_refuses_a_sequence_name_the_archive_does_not_own(
    db, tmp_path, empty_install
):
    """The manifest's table set is validated in both directions and its sequence names are then
    executed verbatim through `setval`.

    An archive is a file: it arrives on a USB stick, over a channel nobody controls, or edited by
    an operator who was told it would help. Winding `app_user_id_seq` back to 1 is not a content
    problem, and no part of the restore would have said anything.
    """
    await _seed_movie_data(db)
    await db.execute("INSERT INTO app_user (name, role) VALUES ($1, 'member')", MARKER_USER)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    def plant(manifest):
        manifest["sequences"]["public.app_user_id_seq"] = {"last_value": 1, "is_called": False}

    tampered = _with_edited_manifest(report.path, tmp_path / "tampered.zip", plant)
    with pytest.raises(movie_data.RestoreRefused, match="app_user_id_seq"):
        await movie_data.restore_archive(empty_install, tampered)

    # Refused before it wrote, which is the claim §10's "validate -> stage" ordering makes.
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


# --- §2's nightly dump (platform-backup-rotation-and-ciphertext) -------------------------------


def _fake_dumps(directory: Path, count: int) -> list[Path]:
    """`count` dumps in this job's own naming, oldest first. Dated well before today, so a real
    dump written alongside them is unambiguously the newest."""
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for nth in range(1, count + 1):
        path = directory / f"spielplan-2024{nth:02d}01T030000Z.dump"
        path.write_bytes(b"PGDMP-not-really")
        made.append(path)
    return made


def test_rotation_keeps_the_newest_fourteen(tmp_path):
    """§2: "rotation 14". Pure filesystem, so the retention rule is checked without a database
    and without `pg_dump` — the two things that make the rest of this section skippable."""
    directory = tmp_path / "backups"
    made = _fake_dumps(directory, 20)

    pruned = nightly.prune(directory)

    survivors = sorted(p.name for p in directory.glob("*.dump"))
    assert nightly.KEEP == 14
    assert survivors == sorted(p.name for p in made[-14:])
    assert sorted(pruned) == sorted(p.name for p in made[:6])


def test_rotation_leaves_files_it_did_not_write_alone(tmp_path):
    """`prune` deletes, and a delete that guesses at what it owns is how the operator's own copy
    of the dump they were about to restore disappears."""
    directory = tmp_path / "backups"
    _fake_dumps(directory, 20)
    (directory / "before-the-upgrade.dump.keep").write_bytes(b"mine")
    (directory / "notes.txt").write_bytes(b"mine")

    nightly.prune(directory)

    assert (directory / "before-the-upgrade.dump.keep").exists()
    assert (directory / "notes.txt").exists()


def test_rotation_removes_interrupted_dumps(tmp_path):
    """§2's "rotation 14" counts finished dumps, and `dumps()` globs `*.dump` — so the
    `*.dump.partial` a killed `pg_dump` leaves behind matches nothing and is never deleted.

    A worker killed inside the nightly window leaks one file per attempt, forever, in the one
    directory §2 asks the operator to copy off-box. Rotation is the only thing in this module
    that deletes, so it is the only thing that can clean up after a kill.
    """
    directory = tmp_path / "backups"
    _fake_dumps(directory, 3)
    killed = [directory / f"spielplan-20250{nth}01T030000Z.dump.partial" for nth in (1, 2)]
    for path in killed:
        path.write_bytes(b"half a dump")

    pruned = nightly.prune(directory)

    assert not list(directory.glob("*.partial")), "an interrupted dump survived rotation"
    assert sorted(pruned) == sorted(p.name for p in killed)
    assert len(nightly.dumps(directory)) == 3


def test_the_dump_keeps_the_database_password_off_the_command_line(tmp_path, monkeypatch):
    """§14.3: the credential this appliance holds is admin-equivalent, and `DATABASE_URL` is the
    other one — an argv element is world-readable to anything that can run `ps` on the host.

    The percent-encoded password is the case that matters: libpq decodes a URI's password, so
    PGPASSWORD has to carry the decoded value or the dump authenticates against nothing on the
    one night the operator's password has a `/` in it.
    """
    url = "postgresql://spielplan:s3cr3t%2Fp%40ss@db.local:5432/spielplan"
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env")
        kwargs["stdout"].write(b"PGDMP")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    # M4.7 (dd-backup-missing-pg-dump) made `dump` refuse before it opens the partial when the
    # named binary is not on PATH, and this developer box has no libpq client. Any real
    # executable satisfies the resolution; `subprocess.run` never reaches it.
    monkeypatch.setattr(nightly, "PG_DUMP", (sys.executable,))
    monkeypatch.setattr(nightly.subprocess, "run", fake_run)
    nightly.dump(url, tmp_path / "spielplan-20260101T030000Z.dump")

    joined = " ".join(str(part) for part in seen["argv"])
    assert "s3cr3t" not in joined and "%2F" not in joined, joined
    assert seen["env"] is not None, "pg_dump inherited the parent environment"
    assert seen["env"]["PGPASSWORD"] == "s3cr3t/p@ss"
    assert "postgresql://spielplan@db.local:5432/spielplan" in joined, joined


async def test_the_nightly_job_writes_a_dump_into_the_backups_directory_and_prunes(
    db, backup_env
):
    """§2: "nightly `pg_dump` to `/data/backups`, rotation 14", as the worker actually runs it."""
    await _seed_movie_data(db)
    directory = settings().data_dir / "backups"
    _fake_dumps(directory, 14)

    report = await nightly.run(datetime.now(UTC))

    assert report.path.parent == directory
    assert report.path.exists() and report.bytes > 0
    assert report.path.read_bytes().startswith(b"PGDMP"), "not a pg_dump custom archive"
    assert len(list(directory.glob("*.dump"))) == nightly.KEEP
    assert report.pruned, "the fifteenth dump pruned nothing"
    assert not list(directory.glob("*.partial")), "a partial dump was left behind"


async def test_the_dump_contains_no_plaintext_connector_secret(db, backup_env, monkeypatch):
    """§2: "Dumps contain ciphertext only".

    The search runs over the archive expanded back to SQL, not over the file: the custom format
    compresses its data blocks, and a leaked secret hidden behind zlib would be absent for a
    reason that has nothing to do with §2. The title name and the stored ciphertext are the two
    controls — together they say this is a dump of this database, with this table's data in it.
    """
    await _seed_movie_data(db)
    ciphertext = await _seed_connector_secret(db, monkeypatch)

    report = await nightly.run(datetime.now(UTC))
    with report.path.open("rb") as fh:
        sql = _run([*_client("pg_restore"), "-f", "-"], stdin=fh).stdout

    assert CONTENT_MARKER.encode("utf-8") in sql, "the dump is not of this database"
    assert ciphertext.hex().encode("ascii") in sql.lower(), "connector_config data is not in it"
    assert MARKER_SECRET.encode("utf-8") not in sql


async def test_a_dump_restored_without_secrets_key_leaves_connector_config_undecryptable(
    db, backup_env, blank_url, monkeypatch, tmp_path
):
    """§2: "back up the env file (`SECRETS_KEY`) alongside them, or a restored dump cannot decrypt
    connector config."

    The restored install has the ciphertext and the wrapped DEK and no way to unwrap it: the row
    survives, the secret does not. Undecryptable rather than usable is the whole point — a dump
    that restored a working Jellyfin admin key would make every off-box copy of the backup an
    admin credential for the media server (§14.3).
    """
    import asyncpg

    await _seed_movie_data(db)
    await _seed_connector_secret(db, monkeypatch)

    report = await nightly.run(datetime.now(UTC))
    with report.path.open("rb") as fh:
        _run([*_client("pg_restore"), "--dbname", blank_url], stdin=fh)

    conn = await asyncpg.connect(blank_url)
    try:
        row = await conn.fetchrow(
            "SELECT config, secrets_encrypted FROM connector_config WHERE name = 'jellyfin'"
        )
        assert row is not None and row["secrets_encrypted"], "the restore lost connector config"
        assert MARKER_SECRET.encode("utf-8") not in bytes(row["secrets_encrypted"])

        # The operator who copied the dumps and not the env file (§2's exact warning).
        monkeypatch.delenv("SECRETS_KEY", raising=False)
        monkeypatch.chdir(tmp_path)          # Settings reads .env from the working directory
        settings.cache_clear()
        with pytest.raises(RuntimeError, match="SECRETS_KEY"):
            await core_secrets.get_connector_secrets(conn, "jellyfin")
    finally:
        await conn.close()


# --- M4.7: the job's own correctness ----------------------------------------------------------
#
# Everything below is filesystem and string work, with no database and no `pg_dump`, because
# every defect here is one an install meets on the day it needs the backup and never before.
#
# (platform-backup-rotation-owns-only-its-own-names, and the same-date half of
# platform-nightly-is-a-night-not-an-uptime.)

# What an operator names the copy they take before an upgrade — the compose file's restore
# comment and README both say the dumps live here, so this file lands in this directory. The
# name matters twice: it matches `spielplan-*.dump`, and `-` (0x2D) sorts below `0`, so under
# the old glob it was the oldest of the set and the first thing `prune` unlinked.
OPERATOR_COPY = "spielplan-2026-08-14-before-upgrade.dump"


def _nights(directory: Path, count: int, month: str = "202608") -> list[Path]:
    """`count` dumps in this job's own naming, dated in `month` so they sort *after* a name the
    operator wrote by hand in the same year. `_fake_dumps` dates its dumps in 2024, which would
    put the operator's 2026 copy at the newest end and hide the bug this reproduces."""
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for day in range(1, count + 1):
        path = directory / f"spielplan-{month}{day:02d}T030000Z.dump"
        path.write_bytes(b"PGDMP-not-really")
        made.append(path)
    return made


@pytest.fixture
def backups(tmp_path, monkeypatch):
    """`DATA_DIR` alone: the rest of `run()`'s inputs are config and its clock is an argument —
    and the tests that take this never reach the database, because the job answers before it
    would."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield tmp_path / "data" / "backups"
    settings.cache_clear()


def test_rotation_leaves_the_operators_own_dated_copy_alone(tmp_path):
    """The file `prune`'s own docstring exists to protect, in the form an operator writes it.

    Fourteen real nights plus one hand-saved copy is fifteen candidates under the old glob, so
    rotation deleted exactly one — and by string order that one was the operator's, the day
    before the upgrade they saved it for.
    """
    directory = tmp_path / "backups"
    nights = _nights(directory, 14)
    keeper = directory / OPERATOR_COPY
    keeper.write_bytes(b"the copy they will want")

    assert sorted(p.name for p in directory.iterdir())[0] == OPERATOR_COPY, (
        "this reproduction needs the operator's copy to sort oldest, which is why it went first"
    )

    pruned = nightly.prune(directory)

    assert keeper.exists(), "rotation deleted the copy the operator took before the upgrade"
    assert pruned == [], f"rotation deleted {pruned} out of fourteen nights plus one stranger"
    assert [p.name for p in nightly.dumps(directory)] == [p.name for p in nights]


def test_rotation_leaves_a_partial_it_did_not_write_alone(tmp_path):
    """The same rule on the other glob, where the consequence is worse.

    Interrupted dumps are removed outright rather than counted against the fourteen, so a name
    `interrupted()` wrongly claims is not rotated early — it is gone on the first prune.
    """
    directory = tmp_path / "backups"
    _nights(directory, 2)
    theirs = directory / f"{OPERATOR_COPY}{nightly.PARTIAL}"
    theirs.write_bytes(b"an interrupted copy of their own")
    ours = directory / f"spielplan-20260814T030000Z.dump{nightly.PARTIAL}"
    ours.write_bytes(b"half a dump")

    pruned = nightly.prune(directory)

    assert theirs.exists(), "rotation deleted a partial file it did not write"
    assert not ours.exists(), "the debris this job leaves must still be cleaned up"
    assert pruned == [ours.name]


@pytest.mark.parametrize("name", [
    OPERATOR_COPY,
    "spielplan-20260814.dump",              # a date with no time
    "spielplan-20260814T0300Z.dump",        # seconds dropped
    "spielplan-20260814T030000.dump",       # the UTC marker dropped
    "spielplan-.dump",                      # the prefix and the suffix and nothing between
    "spielplan-backup.dump",
])
def test_a_name_this_job_could_not_have_written_is_not_this_jobs(tmp_path, name):
    """`dumps()` is what `prune` deletes from, so membership is the whole safety property."""
    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / name).write_bytes(b"not ours")
    (directory / f"{name}{nightly.PARTIAL}").write_bytes(b"nor this")

    assert nightly.dumps(directory) == []
    assert nightly.interrupted(directory) == []
    assert nightly.prune(directory) == []


def test_every_name_the_job_generates_is_one_it_owns(tmp_path):
    """The other direction, which is the one that fails silently: a matcher that drifts away
    from `dump_name` stops rotation seeing its own files, and the directory §2 asks the operator
    to copy off-box grows without bound instead of holding fourteen."""
    directory = tmp_path / "backups"
    directory.mkdir()
    made = []
    for moment in (
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 8, 14, 3, 0, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
    ):
        name = nightly.dump_name(moment)
        (directory / name).write_bytes(b"PGDMP")
        (directory / f"{name}{nightly.PARTIAL}").write_bytes(b"half a dump")
        made.append(name)

    assert [p.name for p in nightly.dumps(directory)] == sorted(made)
    assert [p.name for p in nightly.interrupted(directory)] == sorted(
        f"{name}{nightly.PARTIAL}" for name in made
    )


def test_an_ipv6_database_url_keeps_the_brackets_libpq_needs():
    """§2 calls "a Postgres outside this compose file" supported, and that is where an IP literal
    appears. The old netloc was rebuilt from `urlsplit().hostname`, which strips the brackets, so
    `postgresql://u:p@[::1]:5432/db` reached libpq as `postgresql://u@::1:5432/db` and was
    rejected with `invalid integer value ":1:5432" for connection option "port"` — a working app
    with zero backups, because asyncpg parses the bracketed original fine.

    The assertion is that round trip: the port parses as an integer again, and the host is the
    literal rather than the first fragment of one.
    """
    dsn, credential = nightly._connection(
        "postgresql://spielplan:s3cr3t@[fd00::2]:5432/spielplan"
    )

    assert credential["PGPASSWORD"] == "s3cr3t"
    assert "s3cr3t" not in dsn, dsn
    parsed = urlsplit(dsn)
    assert parsed.hostname == "fd00::2", dsn
    assert parsed.port == 5432, dsn
    assert parsed.username == "spielplan" and parsed.password is None


def test_the_dsn_keeps_a_percent_encoded_username():
    """A characterisation test rather than a repair: the username survived the old rebuild too,
    and the textual one must not lose it. A `@` in a username is what an install authenticating
    against a managed Postgres has, and libpq decodes the field itself — decoding it here would
    authenticate as a user that does not exist."""
    dsn, credential = nightly._connection("postgresql://ops%40house:p%2Fw@db.local/spielplan")

    assert dsn == "postgresql://ops%40house@db.local/spielplan"
    assert credential["PGPASSWORD"] == "p/w"


def test_a_missing_pg_dump_is_this_modules_own_error_and_leaves_no_debris(tmp_path, monkeypatch):
    """The partial used to be opened first, so a box without `postgresql-client-16` leaked a
    0-byte file per attempt: `FileNotFoundError` came from inside the `with`, past the unlink
    that only runs on a non-zero exit, and `run()` prunes only after `dump()` returns."""
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setattr(nightly, "PG_DUMP", ("pg_dump-that-is-not-installed",))

    with pytest.raises(RuntimeError) as raised:
        nightly.dump(
            "postgresql://u:p@db.local/spielplan",
            directory / "spielplan-20260814T030000Z.dump",
        )

    assert "pg_dump-that-is-not-installed" in str(raised.value)
    assert "ops/backend.Dockerfile" in str(raised.value), "the message does not say where from"
    assert list(directory.iterdir()) == [], "a refused attempt left a file behind"


def test_a_pg_dump_that_never_returns_is_killed_and_leaves_no_debris(tmp_path, monkeypatch):
    """`_tick` runs due jobs one after another, so a dump blocked behind an import's lock stops
    §7.3's one-minute poll for as long as it hangs — and with no `timeout=`, that is for ever."""
    directory = tmp_path / "backups"
    directory.mkdir()
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["stdout"].write(b"PGDMP-half-of-one")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(nightly, "PG_DUMP", (sys.executable,))
    monkeypatch.setattr(nightly.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="1800"):
        nightly.dump(
            "postgresql://u:p@db.local/spielplan",
            directory / "spielplan-20260814T030000Z.dump",
        )

    assert seen["timeout"] == nightly.DUMP_TIMEOUT_SECONDS == 1800
    assert list(directory.iterdir()) == [], "the half-written dump stayed in the directory"


async def test_a_second_dump_on_the_same_date_is_refused(backups, monkeypatch):
    """§2's "rotation 14" is a promise about fourteen nights, and these file names are what keeps
    it. With `last_run` in-process every worker start fired this job, so fourteen restarts inside
    an hour spent all fourteen slots; `due()`'s anchoring is the other half of the fix, and this
    is the half that holds across a restart, across a retry, and when an operator runs the job by
    hand. It also retires `dump_name`'s same-second collision."""
    backups.mkdir(parents=True)
    last_night = backups / nightly.dump_name(datetime.now(UTC).replace(hour=3, second=0))
    last_night.write_bytes(b"PGDMP-last-night")

    def refuse(*args, **kwargs):
        raise AssertionError("pg_dump ran on a date this job had already dumped")

    monkeypatch.setattr(nightly, "dump", refuse)

    report = await nightly.run(datetime.now(UTC))

    assert report.skipped is True and report.as_dict()["skipped"] is True
    assert report.path == last_night and report.bytes == last_night.stat().st_size
    assert report.pruned == () and report.kept == 1


async def test_a_dump_from_yesterday_does_not_stop_tonights(backups, monkeypatch):
    """The counterpart, so the refusal cannot quietly become "never dump again"."""
    backups.mkdir(parents=True)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    (backups / nightly.dump_name(yesterday)).write_bytes(b"PGDMP-the-night-before")

    def fake_dump(database_url: str, path: Path) -> int:
        path.write_bytes(b"PGDMP-tonight")
        return path.stat().st_size

    monkeypatch.setattr(nightly, "dump", fake_dump)

    report = await nightly.run(datetime.now(UTC))

    assert report.skipped is False
    assert report.path.name.startswith(f"spielplan-{datetime.now(UTC):%Y%m%d}T")
    assert report.path.read_bytes() == b"PGDMP-tonight"
    assert report.kept == 2 and report.pruned == ()


# Two households whose local date and UTC date come apart in opposite directions. At UTC+13 every
# local time before 13:00 falls on yesterday's UTC day; at UTC-11 every local time from 13:00
# falls on tomorrow's. Both are real households (Kiritimati and Niue), and both make one local
# night span two UTC dates while two local nights can share one.
#
# Fixed offsets rather than named zones for the reason the rest of these tests give: a Windows
# checkout has no system tz database, and `worker._now_local`'s fallback exists for exactly that.
FAR_FROM_UTC = pytest.mark.parametrize(
    "offset", [-11, 13], ids=["utc-minus-11", "utc-plus-13"]
)


@FAR_FROM_UTC
def test_last_local_nights_dump_does_not_refuse_this_ones(tmp_path, offset):
    """`Job.anchor_hour` fires once per **local** date; this guard asked the **UTC** date.

    A dump that landed in the local evening — a first boot after the anchor hour, or a retry that
    finally succeeded — was stamped with a UTC date that the *next* local night also maps to, so
    the next night's scheduled dump was refused as "already dumped today". `job_run.ok` stayed
    true and `BackupReport.skipped` is not an error, so nothing anywhere reported the missing
    night; §2's fourteen quietly became thirteen, and again on the next such evening.

    `dump_name` stays UTC and must: the names are sorted as strings and rotation depends on that
    order. What changes is which of those names counts as tonight's, not what they are called.
    """
    tz = timezone(timedelta(hours=offset))
    last_night = datetime(2026, 9, 7, 20, 0, tzinfo=tz)
    tonight = datetime(2026, 9, 8, 6, 0, tzinfo=tz)
    assert last_night.astimezone(UTC).date() == tonight.astimezone(UTC).date(), (
        "the case only exists where two local nights share one UTC date"
    )

    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / nightly.dump_name(last_night.astimezone(UTC))).write_bytes(b"PGDMP-last-night")

    assert nightly.todays_dump(directory, tonight) is None, (
        "last night's dump counted as tonight's, so this night gets none"
    )


@FAR_FROM_UTC
def test_a_dump_from_earlier_in_this_local_night_is_still_this_nights(tmp_path, offset):
    """The mirror of the same disagreement, and the one that costs a retention slot.

    One local night spans two UTC dates for these households, so a retry hours after the anchor —
    which is what `_tick` does when the first attempt failed — asked about a different UTC date
    than the attempt that had already written a dump, and wrote a second one for the same night.
    Two dumps for one night is two of §2's fourteen slots for thirteen nights of history.
    """
    tz = timezone(timedelta(hours=offset))
    at_the_anchor = datetime(2026, 9, 8, 6, 0, tzinfo=tz)
    a_retry_that_evening = datetime(2026, 9, 8, 20, 0, tzinfo=tz)
    assert at_the_anchor.astimezone(UTC).date() != a_retry_that_evening.astimezone(UTC).date(), (
        "the case only exists where one local night spans two UTC dates"
    )

    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / nightly.dump_name(at_the_anchor.astimezone(UTC))).write_bytes(b"PGDMP-tonight")

    already = nightly.todays_dump(directory, a_retry_that_evening)
    assert already is not None and already.read_bytes() == b"PGDMP-tonight", (
        "a dump from earlier in this same local night was not recognised as tonight's"
    )


def test_a_name_of_the_right_shape_but_an_impossible_date_is_never_tonights(tmp_path):
    """`/data/backups` is a directory an operator can also put things in.

    `_is_own` matches the shape of the stamp and not the calendar — it is the regex `prune`'s
    own contract is written as — so a hand-made `spielplan-20241301T030000Z.dump` reaches this
    question, and asking the household's date of it means parsing it. Answering with an
    exception would take the nightly job down over a file it did not write and would not delete;
    the answer is that nothing this job wrote can carry month 13, so it is not tonight's.
    """
    directory = tmp_path / "backups"
    directory.mkdir()
    impossible = directory / "spielplan-20241301T030000Z.dump"
    impossible.write_bytes(b"PGDMP-not-really")

    assert nightly.todays_dump(directory, datetime(2024, 12, 1, 3, tzinfo=UTC)) is None
    assert impossible in nightly.dumps(directory), "rotation's own view is unchanged"


# --- M4.7: the archive is a snapshot, and the restore holds a lock ----------------------------
#
# (platform-movie-data-archive-is-a-snapshot-and-a-locked-restore)
#
# Every test above writes and restores with nothing else touching the database, which was a fair
# model of production for exactly as long as nothing could call these functions: M4.5 shipped
# `write_archive` and `restore_archive` with no caller outside this file. `spielplan-movie-data`
# ends that, and the writer the entry point exposes them to is the worker — §7.1's Jellyfin sync
# fires at worker start and `sync/resolve.py` inserts a title for a library item it does not know.
# So the pause below is not a contrivance: it is the operator running the command on a live stack.


class _PausingConnection:
    """`db`, with a hook that runs immediately before a named COPY.

    asyncpg offers no seam inside a COPY, so the seam is the connection object: every attribute
    delegates untouched and the two COPY methods await the hook first. The module under test
    keeps its shape — a production seam added for a test is a test that passes because it was
    built to.
    """

    def __init__(self, conn, hook) -> None:
        self._conn = conn
        self._hook = hook

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def copy_from_table(self, table_name, **kwargs):
        await self._hook(table_name)
        return await self._conn.copy_from_table(table_name, **kwargs)

    async def copy_to_table(self, table_name, **kwargs):
        await self._hook(table_name)
        return await self._conn.copy_to_table(table_name, **kwargs)


def _once(table: str, body):
    """A hook that fires the first time `table` is about to be copied, and never again."""
    fired: list[str] = []

    async def hook(table_name: str) -> None:
        if fired or table_name != table:
            return
        fired.append(table_name)
        await body()

    return hook, fired


async def test_a_title_minted_during_the_write_is_wholly_out_of_the_archive(
    db, pg_url, tmp_path, empty_install
):
    """Thirty-three COPYs with no enclosing transaction are thirty-three snapshots.

    The pause is placed after `title` and before its children because that is the gap the second
    snapshot arrived in: `title` copied two rows, the sync minted two more with a genre and a
    credit each, and `title_genre` and `credit` — read moments later, from a snapshot that now
    had them — carried rows pointing at titles the archive does not hold. Nothing said so. The
    archive verified, the manifest counted the rows it had written, and the failure surfaced at
    the far end as a foreign-key violation on an install with nothing in it, which is the worst
    possible place to learn that last week's archive was never whole.
    """
    await _seed_movie_data(db)
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    other = await asyncpg.connect(pg_url)
    minted: list[int] = []

    async def mint() -> None:
        for nth in range(2):
            new_id = await other.fetchval(
                "INSERT INTO title (kind, name, origin) VALUES ('movie', $1, 'acquired') "
                "RETURNING id",
                f"Minted Mid Write {nth}",
            )
            await other.execute(
                "INSERT INTO title_genre (title_id, genre) VALUES ($1, 'Thriller')", new_id
            )
            await other.execute(
                "INSERT INTO credit (title_id, person_id, department, job) "
                "VALUES ($1, 5, 'Directing', 'Director')",
                new_id,
            )
            minted.append(new_id)

    hook, fired = _once("person", mint)
    try:
        report = await movie_data.write_archive(
            _PausingConnection(db, hook), tmp_path / "movie-data.zip"
        )
    finally:
        await other.close()

    assert fired and len(minted) == 2, "the concurrent writer never ran"
    assert await db.fetchval("SELECT count(*) FROM title") == 4, "and it did write"

    # The restore is the assertion: its single transaction is what an inconsistency lands on.
    restored = await movie_data.restore_archive(empty_install, report.path)

    assert restored.tables["public.title"] == 2
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 2
    assert await empty_install.fetchval("SELECT count(*) FROM credit") == 2
    assert await empty_install.fetchval("SELECT count(*) FROM title_genre") == 1


async def test_the_recorded_sequence_position_is_never_below_an_archived_id(
    db, pg_url, tmp_path, empty_install
):
    """The other half of the same gap, and the one decision 162 is actually about.

    Reading `last_value` before the rows records where the sequence stood, not how far the
    archive reaches: a title minted between the two is copied with an id above the recorded
    position, and the restored install's `setval` winds the sequence back under a row it has just
    loaded. The next acquisition re-mints an id the household already holds — §7.2 landing a new
    film on top of an existing one.

    So the pause is before `title` here: under the old order the sequence had already been read
    and the rows had not.
    """
    await _seed_movie_data(db)
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    other = await asyncpg.connect(pg_url)
    minted: list[int] = []

    async def mint() -> None:
        for nth in range(2):
            minted.append(
                await other.fetchval(
                    "INSERT INTO title (kind, name, origin) VALUES ('movie', $1, 'acquired') "
                    "RETURNING id",
                    f"Minted Before The Copy {nth}",
                )
            )

    hook, fired = _once("title", mint)
    try:
        report = await movie_data.write_archive(
            _PausingConnection(db, hook), tmp_path / "movie-data.zip"
        )
    finally:
        await other.close()

    assert fired and minted == [APP_ID_FLOOR + 1, APP_ID_FLOOR + 2]

    with zipfile.ZipFile(report.path) as archive:
        manifest = json.loads(archive.read(movie_data.MANIFEST))
    position = manifest["sequences"]["public.title_id_seq"]
    assert position["last_value"] >= max(minted), (
        "the archive records a sequence position below an id minted before its rows were read"
    )
    assert manifest["snapshot"], "the archive does not say which snapshot it was read in"

    await movie_data.restore_archive(empty_install, report.path)
    held = {r["id"] for r in await empty_install.fetch("SELECT id FROM title")}
    for nth in range(2):
        fresh = await empty_install.fetchval(
            "INSERT INTO title (kind, name) VALUES ('movie', $1) RETURNING id",
            f"After The Restore {nth}",
        )
        assert fresh not in held, f"acquisition {nth} re-minted title id {fresh}"
        held.add(fresh)


async def test_the_recorded_position_is_bounded_by_rows_the_sequence_never_minted(
    db, tmp_path, empty_install
):
    """The belt to the snapshot's braces: the position is bounded by `max(id)`, per table.

    0015_seed.sql leaves `title_id_seq` unpositioned deliberately and says positioning is the
    seed import's job; its MINVALUE makes a *missed* positioning loud only for a `setval` below
    1e9, which says nothing about a row sitting exactly on START with the sequence never called.
    An install in that state holds an id the sequence has never heard of, and copying the
    sequence's own answer forward hands the restored install a position that mints the id it just
    loaded. The write is the one moment where the whole namespace is visible at once.
    """
    await _seed_movie_data(db)
    await db.execute(
        "INSERT INTO title (id, kind, name, origin) "
        "VALUES ($1, 'movie', 'Hand Loaded', 'acquired')",
        APP_ID_FLOOR,
    )
    assert await db.fetchval("SELECT is_called FROM title_id_seq") is False

    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    await movie_data.restore_archive(empty_install, report.path)

    minted = await empty_install.fetchval(
        "INSERT INTO title (kind, name) VALUES ('movie', 'After The Restore') RETURNING id"
    )
    assert minted == APP_ID_FLOOR + 1


async def test_the_restore_holds_the_archived_tables_against_a_concurrent_write(
    db, pg_url, tmp_path, empty_install
):
    """"This install holds no movie data" is a fact with a lifetime.

    Read outside the transaction and behind no lock, it was true when it was read and false by
    the time the COPY relied on it: with the COPY paused and titles minted on a second
    connection, the restore completed with four title rows in a table it had declared empty, the
    absolute `setval` wound `title_id_seq` back under both of them, and the next acquisition
    raised a duplicate key on `title_pkey`. `LOCK TABLE ... IN EXCLUSIVE MODE` is what turns the
    refusal from an observation into a decision.

    `lock_timeout` rather than a test clock: the server decides that the writer is blocked, and
    the wait becomes an error this can assert on instead of a hang it has to time out.
    """
    await _seed_movie_data(db)
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    other = await asyncpg.connect(_sibling(pg_url, "_restore")[2])

    async def mint() -> None:
        await other.execute("SET lock_timeout = '750ms'")
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await other.execute(
                "INSERT INTO title (kind, name, origin) "
                "VALUES ('movie', 'Minted Mid Restore', 'acquired')"
            )

    hook, fired = _once("title", mint)
    try:
        await movie_data.restore_archive(_PausingConnection(empty_install, hook), report.path)
    finally:
        await other.close()

    assert fired, "the concurrent writer never ran"
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 2
    minted = await empty_install.fetchval(
        "INSERT INTO title (kind, name) VALUES ('movie', 'After The Restore') RETURNING id"
    )
    assert minted == APP_ID_FLOOR + 1


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda m: m.pop("tables"), id="no-tables"),
        pytest.param(lambda m: m["tables"][0].pop("schema"), id="entry-without-a-schema"),
        pytest.param(lambda m: m["tables"][0].update(columns="id,name"), id="columns-as-text"),
        pytest.param(lambda m: m.update(sequences=[]), id="sequences-as-a-list"),
        pytest.param(
            lambda m: m["sequences"]["public.title_id_seq"].pop("is_called"),
            id="position-without-is-called",
        ),
        pytest.param(lambda m: m.update(seed="v20260828"), id="seed-as-text"),
    ],
)
async def test_a_hand_edited_manifest_is_a_refusal_and_not_a_keyerror(
    db, tmp_path, empty_install, edit
):
    """`restore_archive`'s docstring promises it refuses "before anything is written".

    Every refusal in it kept that promise except the shape: the manifest's keys were read
    straight, so an edited file raised a bare `KeyError` naming a JSON key from inside a module
    the operator has never read. The person editing a manifest by hand is by definition the
    person whose install is already broken, and "this archive is not usable" and "the restore
    crashed — is my install half-loaded?" are not the same sentence.
    """
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    tampered = _with_edited_manifest(report.path, tmp_path / "tampered.zip", edit)

    with pytest.raises(movie_data.RestoreRefused):
        await movie_data.restore_archive(empty_install, tampered)
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


async def test_a_restore_leaves_no_title_claiming_a_placement_it_has_no_basis_for(
    db, tmp_path, empty_install
):
    """`title.placement` is the denormalised state of a coordinate in `title_placement`, and the
    archive carries no `title_placement` row — §10 calls a coordinate expressed in the old
    Backbone's basis garbage against a new one.

    Carrying the column forward therefore restores an install whose titles say 'warm' or
    'cold_tower' while nothing holds a coordinate at all. §12's M2 criterion is "every owned
    title has a coordinate", counted as `is_owned AND placement = 'unplaced'` over the index
    0008_placement.sql:64 exists for — so the restored install reports zero titles waiting to be
    placed, and `reconcile.py` only re-examines rows it can see are stale, which a 'cold_tower'
    row is not.
    """
    await _seed_movie_data(db)
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ('v20260828', '{}'::jsonb, 'active', 'seed')"
    )
    await db.execute(
        "UPDATE title SET is_owned = true, placement = 'cold_tower', "
        "placement_bundle = 'v20260828', placement_at = now()"
    )

    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    await movie_data.restore_archive(empty_install, report.path)

    rows = await empty_install.fetch("SELECT placement, placement_at FROM title ORDER BY id")
    assert [(r["placement"], r["placement_at"]) for r in rows] == [("unplaced", None)] * 2
    assert await empty_install.fetchval("SELECT count(*) FROM title_placement") == 0
    assert await empty_install.fetchval(
        "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
    ) == 2, "the restored install claims a placement for a basis it does not hold"


async def test_an_interrupted_write_leaves_the_previous_archive_where_it_was(db, tmp_path):
    """`zipfile.ZipFile(path, "w")` truncates in place, and the COPY loop runs for minutes.

    The gesture the entry point creates is `spielplan-movie-data write` over the archive that is
    already on the stick, and decision 162 makes that archive the household's only copy of its
    movie data. So a write that fails halfway did not merely fail: it had already destroyed the
    file it was replacing, and what it left behind still opens — `close()` writes a central
    directory over whatever got through — so nothing reports the loss until the day of the
    restore, when both copies are gone.

    The failure is placed at `credit` because it has to be somewhere past the first COPY: the
    point is an interruption *inside* the loop, which is where the minutes are.
    """
    await _seed_movie_data(db)
    archive = tmp_path / "movie-data.zip"
    await movie_data.write_archive(db, archive)
    before = archive.read_bytes()

    async def die() -> None:
        raise RuntimeError("the stack went down mid-write")

    hook, fired = _once("credit", die)
    with pytest.raises(RuntimeError, match="mid-write"):
        await movie_data.write_archive(_PausingConnection(db, hook), archive)

    assert fired, "the failure never fired: the write did not reach the table it was placed at"
    assert archive.read_bytes() == before, "the failed write replaced last week's archive"
    assert list(tmp_path.glob("*.partial")) == [], "and left its own debris in the directory"
    with zipfile.ZipFile(archive) as survivor:
        # Whole, not merely unchanged: a truncated archive is missing the manifest, which
        # `write_archive` adds last, and the entries the loop never reached.
        assert movie_data.MANIFEST in survivor.namelist()
        assert len(survivor.namelist()) == len(movie_data.TABLES) + 1


def _without_member(source: Path, target: Path, member: str) -> Path:
    """The archive as an interrupted write left it: every other entry, one of them gone."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            if info.filename != member:
                dst.writestr(info.filename, src.read(info.filename))
    return target


@pytest.mark.parametrize(
    ("member", "named"),
    [
        pytest.param(movie_data.MANIFEST, movie_data.MANIFEST, id="no-manifest"),
        pytest.param("tables/public.credit.copy", "public.credit", id="no-table-entry"),
    ],
)
async def test_an_archive_missing_a_member_is_a_refusal_and_not_a_keyerror(
    db, tmp_path, empty_install, member, named
):
    """The manifest describes the zip; it is not the zip, and only the description was checked.

    `archive.read`/`archive.open` answer a missing member with a `KeyError`, which is neither of
    the two exceptions the command catches — so an archive that lost an entry produced a
    traceback out of a module the operator has never read, and for the table entry it produced
    it from inside the transaction, with every archived table locked. The rollback keeps the
    "before anything is written" promise; the operator does not get to know that. Both shapes
    are what a killed writer used to leave on top of the previous archive: the manifest goes in
    last, so it is the first thing missing.
    """
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    truncated = _without_member(report.path, tmp_path / "truncated.zip", member)

    with pytest.raises(movie_data.RestoreRefused) as refusal:
        await movie_data.restore_archive(empty_install, truncated)
    assert named in str(refusal.value), "the refusal does not say what is missing"
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


# --- M4.7: the operator's way in (platform-movie-data-backup-and-restore) ---------------------


async def _run_cli(*argv: str) -> int:
    """`spielplan-movie-data` as an operator runs it, argparse and `asyncio.run` included.

    In a thread because `main` calls `asyncio.run`, which refuses to nest inside the loop pytest
    is already running. Driving `main` is the point: M4.5's row says "an operator-triggered (CLI)
    movie-data backup" and all ten tests it named called the two functions directly — which is
    how the module came to have no caller at all.
    """
    return await asyncio.to_thread(movie_data.main, list(argv))


async def test_the_operator_command_writes_an_archive_and_restores_it(
    db, pg_url, tmp_path, monkeypatch, capsys
):
    """Both subcommands, end to end, over the pool the app itself opens.

    Through `db/pool.open_pool` and not a bare connection on purpose: the restore writes
    `artifact_bundle.manifest`, and the pool registers `json.dumps` as the jsonb encoder — the
    `::text::jsonb` cast in the module exists for that connection and only that connection. A
    command that opened a codec-less one would leave the cast defending against nothing.
    """
    await _seed_movie_data(db)
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    archive = tmp_path / "movie-data.zip"

    admin, name, url = _sibling(pg_url, "_cli")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    try:
        await migrate.apply_all(conn)

        monkeypatch.setenv("DATABASE_URL", pg_url)
        settings.cache_clear()
        assert await _run_cli("write", str(archive)) == 0
        assert archive.is_file()

        monkeypatch.setenv("DATABASE_URL", url)
        settings.cache_clear()
        assert await _run_cli("restore", str(archive)) == 0

        assert await conn.fetchval("SELECT name FROM title WHERE id = 11") == CONTENT_MARKER
        assert await conn.fetchval("SELECT count(*) FROM app_user") == 0
        assert await conn.fetchval(
            "SELECT jsonb_typeof(manifest) FROM artifact_bundle WHERE kind = 'seed'"
        ) == "object", "the seed record's manifest was encoded twice"
        assert await conn.fetchval(
            "INSERT INTO title (kind, name) VALUES ('movie', 'Acquired After') RETURNING id"
        ) == APP_ID_FLOOR + 1

        # A second restore is decision 162's refusal, which is an exit code and not a traceback.
        assert await _run_cli("restore", str(archive)) == 1
        assert "refusing" in capsys.readouterr().out
        assert await _run_cli("restore", str(tmp_path / "nowhere.zip")) == 1
    finally:
        await conn.close()
        await _drop(admin, name)
        settings.cache_clear()


async def test_a_destination_the_command_cannot_write_is_a_refusal_and_not_a_traceback(
    db, pg_url, tmp_path, monkeypatch, capsys
):
    """The one argument the operator types is the destination, and `_run` handled none of it.

    `write_archive` begins `path.parent.mkdir(parents=True, exist_ok=True)`, and `_run` caught
    only `RestoreRefused` and `BadZipFile` — so every way of getting the destination wrong came
    out as a traceback from a module the operator has never read, for a command whose whole
    promise is that a failure it can foresee is a sentence. The realistic one is not exotic:
    `/data/backups` is mounted on the worker alone (§14.3), so the same command in the backend
    container is uid 1000 against a root-owned `/data` and raises `PermissionError`; a stick that
    is not mounted, a read-only mount and a full disk are the same shape.

    A file standing where a directory has to be is that shape, portably: ENOTDIR on Linux,
    WinError 183 here, `OSError` in both. The archive itself is unharmed either way — the write
    goes to `<name>.partial` and is renamed only when whole — so "refusing" is the honest word:
    nothing was produced and nothing was replaced. [M4.7 cycle 2 finding 13]
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the destination's parent has to be", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    try:
        assert await _run_cli("write", str(blocked / "movie-data.zip")) == 1
        assert "refusing" in capsys.readouterr().out
    finally:
        settings.cache_clear()


async def test_a_rename_that_cannot_land_leaves_no_archive_behind(db, tmp_path):
    """The one step of the write that sat outside the guard that cleans up after it.

    `write_archive` COPYs into `<name>.partial` and renames it onto the target only when the
    archive is whole — and the rename itself was the exception to its own `except BaseException:
    partial.unlink()`. So the one failure that happens with the *entire* archive already written
    was the one that left it behind: §10 sizes the review store at 312 MB with bodies, and
    nothing in this repository ever deletes a `.partial` that is not a nightly dump
    (`nightly.prune` matches `spielplan-<stamp>.dump` and nothing else), while README tells the
    operator that a file ending `.partial` is cleaned up by the next successful run.

    A destination that is already a directory is that failure, portably and without a fixture:
    an operator who made `archives/` a folder to keep archives in and then typed it as the
    argument. `os.replace` answers EISDIR on Linux and WinError 5 here, `OSError` in both, so
    `_run` prints `refusing:` — while the archive it says was not produced sat in the directory
    section 2 asks them to copy off-box. [cycle 3 finding 10]
    """
    await _seed_movie_data(db)
    destination = tmp_path / "archives"
    destination.mkdir()

    with pytest.raises(OSError):
        await movie_data.write_archive(db, destination)

    assert list(tmp_path.glob("*.partial")) == [], (
        "the whole archive stayed behind as debris nothing in this repository removes"
    )
    assert list(destination.iterdir()) == [], "and the destination is untouched"


def _with_replaced_manifest(source: Path, target: Path, blob: bytes) -> Path:
    """The archive as a text editor left it: every entry, the manifest written back verbatim.

    `_with_edited_manifest` cannot express this. It round-trips the manifest through
    `json.loads`/`json.dumps`, so every case it can build is syntactically valid JSON by
    construction — which is why all six of its params missed the parse itself.
    """
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            dst.writestr(
                info.filename,
                blob if info.filename == movie_data.MANIFEST else src.read(info.filename),
            )
    return target


async def test_a_manifest_that_is_not_json_is_a_refusal_and_not_a_decoder_traceback(
    db, pg_url, tmp_path, empty_install, monkeypatch, capsys
):
    """The same gesture as the hand-edited manifest above, one step further in.

    The manifest's keys go through a shape check, and cycle 2 made a *missing* manifest a refusal
    rather than a `KeyError`. The parse standing in front of both did not: `json.loads` answers a
    member that is not JSON with a `JSONDecodeError`, a `ValueError`, which is none of the three
    exceptions `_run` catches. So the operator whose restore was
    refused, who opened `manifest.json` to see what it said and saved it with a stray comma, got
    a nine-frame traceback ending in `json/decoder.py` and no answer to the only question they
    have — whether the install is now half-loaded.

    A doubled comma at the first one the manifest carries, rather than a hand-written blob: what
    is under test is a real archive whose manifest stopped parsing, and this stays true whatever
    the manifest's contents become. [cycle 3 finding 11]
    """
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    with zipfile.ZipFile(report.path) as archive:
        good = archive.read(movie_data.MANIFEST)
    comma = good.index(b",")
    broken = good[:comma] + b",," + good[comma + 1:]
    with pytest.raises(ValueError):
        json.loads(broken)  # the input really is unparseable, or the rest proves nothing
    tampered = _with_replaced_manifest(report.path, tmp_path / "tampered.zip", broken)

    with pytest.raises(movie_data.RestoreRefused, match="not readable JSON"):
        await movie_data.restore_archive(empty_install, tampered)
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0

    # And through the entry point, which is where the refusal becomes a sentence and where the
    # traceback was. Pointed at the seeded database rather than the empty one on purpose: this
    # refusal is ahead of every question about what the destination already holds.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    try:
        assert await _run_cli("restore", str(tampered)) == 1
        assert f"refusing: the archive's {movie_data.MANIFEST} is not readable JSON" in (
            capsys.readouterr().out
        )
    finally:
        settings.cache_clear()


async def test_a_restore_into_a_schema_without_the_archived_tables_is_a_refusal(
    db, pg_url, tmp_path
):
    """The other uncaught class at the same seam, and the likelier one.

    `_layout` reads this install's catalog and raises `RuntimeError` when the archived tables are
    not in it. That is the database README's Recovery block produces: a rebuilt box, the archive
    to hand, and the backend — the only process that applies migrations — not started yet. A
    `RuntimeError` is none of the three `_run` catches, so the answer was a traceback naming
    every archived table where the docstring promises a refusal. [cycle 3 finding 11]
    """
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    admin, name, url = _sibling(pg_url, "_bare")
    await _recreate(admin, name)
    unmigrated = await asyncpg.connect(url)
    try:
        with pytest.raises(movie_data.RestoreRefused, match="tables this schema does not have"):
            await movie_data.restore_archive(unmigrated, report.path)
    finally:
        await unmigrated.close()
        await _drop(admin, name)


# Every gesture that runs the command against an archive, wherever it is written down. `--help`
# is deliberately not matched: `docs/TESTING.md`'s release checklist runs it in the backend
# container to prove the console script exists, which needs no mount at all.
_ARCHIVE_COMMAND = re.compile(
    r"docker compose (?:exec|run --rm) (?P<service>[\w-]+) spielplan-movie-data (?:write|restore)"
)

# The module's own docstring is a document here: it is where a maintainer reads what the command
# is for, and it named `backend` while README named `worker` two lines apart from the sentence
# explaining why it cannot be the backend.
_ARCHIVE_DOCUMENTS = ("backend/spielplan/backup/movie_data.py", "README.md", "docs/TESTING.md")


def _services_named_for_the_archive(sources: dict[str, str]) -> set[tuple[str, str]]:
    return {
        (name, match.group("service"))
        for name, text in sources.items()
        for match in _ARCHIVE_COMMAND.finditer(text)
    }


def test_every_place_that_names_the_archive_command_names_a_container_that_can_write_it():
    """decision 162 makes this archive the household's only copy of its movie data, and M4.7's
    own `sec-08` took `./data/backups` off `x-backend-volumes` — so the command that writes it
    runs in the worker and nowhere else. `movie_data.py`'s module docstring still said
    `docker compose exec backend spielplan-movie-data write`, which is the one place in the
    repository that did; README says `worker` and explains why in the next paragraph.

    Asserted against the compose file rather than against README's spelling, because what makes
    `worker` right is the mount and not the agreement: if a later milestone moves the dumps, this
    fails where the wrong service is named rather than where the mount changed. Every documented
    destination is under `/data/backups`, which is the directory §2 already tells the operator to
    copy off-box. [M4.7 spec-07, sec-08; decision 182]
    """
    compose = COMPOSE.read_text(encoding="utf-8")
    named = _services_named_for_the_archive(
        {name: (_REPO / name).read_text(encoding="utf-8") for name in _ARCHIVE_DOCUMENTS}
    )
    assert named, "no document names the command at all, which is the state spec-07 found"
    wrong = sorted(
        f"{where} runs it in `{service}`"
        for where, service in named
        if ("/data/backups", "rw") not in _mounts(compose, service)
    )
    assert not wrong, (
        f"these name a container with no writable /data/backups: {wrong}. The archive is "
        f"decision 162's only copy of the movie data and lands on the host or nowhere."
    )


def test_the_archive_command_guard_sees_the_container_that_cannot_write_it():
    """The synthetic violation is the one this finding found: the module's own docstring."""
    named = _services_named_for_the_archive(
        {"synthetic": "docker compose exec backend spielplan-movie-data write /data/backups/a.zip"}
    )
    assert named == {("synthetic", "backend")}
    assert ("/data/backups", "rw") not in _mounts(COMPOSE.read_text(encoding="utf-8"), "backend")
