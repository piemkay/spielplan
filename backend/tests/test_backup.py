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

import functools
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from spielplan.backup import movie_data, nightly
from spielplan.core import secrets as core_secrets
from spielplan.core.config import settings
from spielplan.db import migrate
from spielplan.importer.bundle import Bundle, refuse_on_install_state
from spielplan.importer.report import ImportReport

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
# Install bookkeeping and §8.4's work queue: app state, not movie data.
APP_STATE = {"schema_migration", "setup_step", "flywheel_item"}

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

    `nightly.run()` takes no arguments on purpose — the job the loop calls takes none — so the
    environment is the seam here, exactly as it is in production.
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
    monkeypatch.setenv("SECRETS_KEY", "test-secrets-key-not-a-real-one")
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

    report = await nightly.run()

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

    report = await nightly.run()
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

    report = await nightly.run()
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
