"""Backup and restore (§2 Backups, §10, decision 162): the nightly `pg_dump` (ciphertext only, rotation 14)
and the movie-data archive (content without user state, restorable into a fresh install). Dump tests need a
real `pg_dump` and carry a positive content control. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
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
from tests.helpers import create_database, drop_database, sibling

pytestmark = pytest.mark.anyio

# Planted in the tables the archive must not touch; ASCII so a failure prints on a cp1252 console.
MARKER_SECRET = "MARKER-JELLYFIN-ADMIN-KEY-3f9c"
MARKER_VERDICT = "MARKER-VERDICT-SOURCE"
MARKER_USER = "MARKER-MEMBER"
MARKER_SESSION = "MARKER-SESSION-COOKIE"
MARKER_PASSKEY = "MARKER-PASSKEY-LABEL"
MARKER_PUSH = "MARKER-PUSH-ENDPOINT"

# The positive control: content that MUST survive into every artifact under test.
CONTENT_MARKER = "Waechter der Naecht"

# Where the app-minted range starts (decision 162);
# positioned by hand here, since the importer is another row.
APP_ID_FLOOR = 1_000_000_000

# Grouped by reason; together with the archive's table list this covers the schema exhaustively.

USER_STATE = {
    "app_user", "user_title", "verdict", "duel", "tier_edit", "ledger_state",
    "ledger_cutpoints", "user_vector", "ledger_fit", "user_score", "playback_event",
    "acquisition_job", "rate_session", "rate_observation", "session", "session_participant",
    "session_answer", "session_ballot", "session_result", "session_outcome", "auth_session",
    "webauthn_credential", "webauthn_challenge", "push_subscription",
}
SECRET_CUSTODY = {"connector_config", "data_encryption_key", "app_setting"}
# §10: recomputed by the rebuild set; carrying them would ship a stale basis.
BUNDLE_DERIVED = {"artifact_bundle", "title_placement", "title_prior"}
# Install bookkeeping and this box's own view of its library, queue and spend: not movie data.
APP_STATE = {"schema_migration", "setup_step", "flywheel_item", "flywheel_batch", "job_run",
             "title_jellyfin_item"}

# Decision 291: the genome slice is no longer imported; `movie_data.RETIRED` covers pre-291 archives.
GENOME_NOT_IMPORTED = {"ml_genome_tag", "ml_link", "ml_genome_score"}
GENOME_RETIRED = tuple(sorted(f"public.{name}" for name in GENOME_NOT_IMPORTED))

# This box's queued work, robots cache and raw-store pointers, which a restore would name and not have.
ACQUISITION_SPINE = {"acquisition_task", "raw_document", "fetch_host_state"}

# One Jellyfin server's item ids and events, meaningless on another install.
JELLYFIN_INTAKE = {"jellyfin_intake"}

# Extracted DNA travels; what this box refused and its raw-store pack index do not.
DNA_EXTRACTION = {"dna_reject", "dna_pack"}

# This box's own spend, citing raw documents the archive does not carry.
LLM_SPEND = {"llm_call"}

# A cache of TMDB answers this box's key obtained; a restore re-asks.
ART_CACHE = {"art_lookup"}

EXCLUDED = (USER_STATE | SECRET_CUSTODY | BUNDLE_DERIVED | APP_STATE
            | GENOME_NOT_IMPORTED | ACQUISITION_SPINE | JELLYFIN_INTAKE | DNA_EXTRACTION
            | LLM_SPEND | ART_CACHE)


@functools.lru_cache(maxsize=1)
def _postgres_container() -> str | None:
    """The running postgres:16 container that publishes TEST_DATABASE_URL's own port, found by that port."""
    url = os.environ.get("TEST_DATABASE_URL") or ""
    port = urlsplit(url).port or 5432
    try:
        done = subprocess.run(
            ["docker", "ps", "--filter", "ancestor=postgres:16",
             "--format", "{{.Names}}	{{.Ports}}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    # Match the host port before the arrow, so a container's internal port cannot answer.
    hits = [
        name for name, _, ports in (line.partition("	") for line in done.stdout.splitlines())
        if name and re.search(rf":{port}->\d+/tcp", ports)
    ]
    return hits[0] if len(hits) == 1 else None


def _inside_the_container(url: str) -> str:
    """`docker exec` runs inside the container, which cannot see the host's published port mapping."""
    if not _postgres_container() or shutil.which("pg_dump"):
        return url
    parts = urlsplit(url)
    userinfo = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:5432" if userinfo
                                     else "127.0.0.1:5432"))


def _client(binary: str) -> tuple[str, ...]:
    found = shutil.which(binary)
    if found:
        return (found,)
    container = _postgres_container()
    if container:
        # `-e PGPASSWORD` forwards the variable, as `dump()` passes the password there, not on argv (§14.3).
        return ("docker", "exec", "-i", "-e", "PGPASSWORD", container, binary)
    pytest.skip(
        f"{binary} is not on PATH and no postgres:16 container publishes "
        f"TEST_DATABASE_URL's port: "
        f"the nightly dump (spec section 2) cannot be exercised without the {binary} binary"
    )


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    done = subprocess.run(argv, capture_output=True, timeout=600, check=False, **kwargs)
    assert done.returncode == 0, (
        f"{argv[:3]} exited {done.returncode}: "
        f"{done.stderr.decode('utf-8', 'replace')[-2000:]}"
    )
    return done


@pytest.fixture
def backup_env(pg_url, tmp_path, monkeypatch):
    """DATABASE_URL and DATA_DIR as the container sets them; the clock is `run()`'s one argument."""
    # The worker's DATABASE_URL, addressed for whichever pg_dump `_client` resolved.
    monkeypatch.setenv("DATABASE_URL", _inside_the_container(pg_url))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(nightly, "PG_DUMP", _client("pg_dump"))
    settings.cache_clear()
    yield tmp_path
    settings.cache_clear()


@pytest.fixture
async def empty_install(pg_url):
    """Never seeded, so only it can show the archive carries
    sequence positions; `db/pool.py`'s json codecs included."""
    import asyncpg

    admin, name, url = sibling(pg_url, "_restore")
    await create_database(admin, name)
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
        await drop_database(admin, name)


@pytest.fixture
async def blank_url(pg_url):
    """`pg_restore` of a whole-database dump wants a target with no schema at all."""
    admin, name, url = sibling(pg_url, "_pgr")
    await create_database(admin, name)
    try:
        yield url
    finally:
        await drop_database(admin, name)


async def _seed_movie_data(conn) -> None:
    """A small world with a row in each layer: spine, DNA, reviews."""
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
    """A marker in every free-text column; `duel`, `tier_edit`,
    `ledger_state` have none, so the table list is asserted."""
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
    """Decompressed: a secret leaked inside compressed bytes would be invisible for the wrong reason."""
    blob = bytearray()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            blob += info.filename.encode("utf-8")
            blob += zf.read(info.filename)
    return bytes(blob)


async def test_the_archive_carries_the_content_spine_the_dna_layer_and_the_review_store(
    db, tmp_path
):
    """Decision 162: the household's copy is the only copy; the reviews are needed to re-extract."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    for table in ("public.title", "public.person", "public.credit", "public.title_meta",
                  "public.dna_tag", "public.dna_evidence", "public.dna_projected",
                  "display.platform_rating", "review_store.review"):
        assert report.tables.get(table), f"{table} is missing or empty in the archive"

    assert CONTENT_MARKER.encode("utf-8") in _archive_bytes(report.path)


async def test_the_archive_carries_nothing_user_specific(db, tmp_path, monkeypatch):
    """The table list and a byte search; the content marker keeps an empty archive from passing."""
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


def test_the_genome_slice_is_left_out_of_the_archive():
    """Decision 291: an entry for a table nothing writes would claim a genome was preserved."""
    archived = {t.name for t in movie_data.TABLES}
    assert not archived & GENOME_NOT_IMPORTED, (
        f"the archive carries a table the importer no longer fills: "
        f"{sorted(archived & GENOME_NOT_IMPORTED)}"
    )
    assert GENOME_NOT_IMPORTED <= EXCLUDED, "left out on purpose, and the reason is written down"
    # The struck names must reappear in `RETIRED` (decision 309), or old archives are refused.
    assert {f"public.{name}" for name in GENOME_NOT_IMPORTED} <= movie_data.RETIRED, (
        "struck from TABLES and not named in RETIRED: every archive written before decision 291 "
        "names these three in its manifest, and a restore reads that manifest"
    )
    assert not movie_data.RETIRED & {t.qualified for t in movie_data.TABLES}, (
        "a table named in RETIRED is skipped by every restore, so re-archiving one while it is "
        "still listed there would write its rows and load none of them back"
    )


# A real archive with `TABLES` as an older build had it, in its original order.
def _pre_291_tables() -> tuple[movie_data.Table, ...]:
    retired = tuple(
        movie_data.Table("public", name)
        for name in ("ml_genome_tag", "ml_link", "ml_genome_score")
    )
    at = [t.name for t in movie_data.TABLES].index("award") + 1
    return movie_data.TABLES[:at] + retired + movie_data.TABLES[at:]


async def test_a_restore_reads_an_archive_written_before_the_genome_slice_was_retired(
    db, tmp_path, empty_install, monkeypatch
):
    """Decision 309: archives outlive builds; pre-291 archives
    restore, and the slice is skipped, not loaded."""
    await _seed_movie_data(db)
    # The slice as the pre-291 importer left it, with a real title id.
    await db.execute("INSERT INTO ml_genome_tag (tag_id, tag) VALUES (1, 'melancholy')")
    await db.execute(
        "INSERT INTO ml_link (ml_movie_id, title_id, imdb_id, tmdb_id) "
        "VALUES (1, 11, 'tt0079944', 101)"
    )
    await db.execute(
        "INSERT INTO ml_genome_score (ml_movie_id, tag_id, relevance) VALUES (1, 1, 0.75)"
    )

    with monkeypatch.context() as older_build:
        older_build.setattr(movie_data, "TABLES", _pre_291_tables())
        report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    assert "public.ml_genome_score" in report.tables, "the fixture is not a pre-291 archive"

    restored = await movie_data.restore_archive(empty_install, report.path)

    assert await empty_install.fetchval("SELECT name FROM title WHERE id = 11") == CONTENT_MARKER
    assert await empty_install.fetchval("SELECT count(*) FROM review_store.review") == 1
    # Named in the report: the operator is told which three tables this build no longer keeps.
    assert restored.retired == GENOME_RETIRED
    assert not set(restored.tables) & movie_data.RETIRED
    for table in sorted(GENOME_NOT_IMPORTED):
        assert await empty_install.fetchval(f"SELECT count(*) FROM {table}") == 0, table


async def test_a_restore_still_refuses_a_table_this_build_neither_archives_nor_retired(
    db, tmp_path, empty_install
):
    """`RETIRED` is a named set, not a tolerance; any other unknown table is refused."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    def plant(manifest):
        manifest["tables"].append(
            {"schema": "public", "name": "app_user", "columns": ["id", "name"], "rows": 0}
        )

    tampered = _with_edited_manifest(report.path, tmp_path / "tampered.zip", plant)
    with pytest.raises(movie_data.RestoreRefused, match="app_user"):
        await movie_data.restore_archive(empty_install, tampered)
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


async def test_a_restore_passes_over_a_table_migration_0039_dropped(db, tmp_path, empty_install):
    """Decision 309: an archive written before 0039 still names the five tables it dropped."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    def plant(manifest):
        manifest["tables"].append(
            {"schema": "public", "name": "watchlist", "columns": ["title_id"], "rows": 0}
        )

    older = _with_edited_manifest(report.path, tmp_path / "pre-0039.zip", plant)
    restored = await movie_data.restore_archive(empty_install, older)
    assert restored.retired == ("public.watchlist",)
    assert await empty_install.fetchval("SELECT name FROM title WHERE id = 11") == CONTENT_MARKER


async def test_every_table_is_either_archived_or_deliberately_left_out(db):
    """An unmapped table is invisible, so every new table must be classified."""
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
    """The positive half, end to end and across two databases."""
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
    """The seed import never runs again, so the archive must carry `setval` or ids are re-minted."""
    await _seed_movie_data(db)
    # What the seed import leaves behind: the sequences inside the app's own range.
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

    # Twice: a sequence rewound by one still mints one free id first.
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
    """`origin` defaults to 'bundle', so dropping it would silently empty the rebuild set."""
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
    """COPY into a populated table fails halfway, so the refusal names the table first."""
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
    """`placement_bundle` references a bundle row the
    archive does not carry, so it is not carried either."""
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
    # Which bundle seeded this household is provenance worth keeping.
    assert restored.seeded == "v20260828"
    assert await empty_install.fetchval(
        "SELECT jsonb_typeof(manifest) FROM artifact_bundle WHERE kind = 'seed'"
    ) == "object", "the seed record's manifest was encoded twice"


async def test_a_restored_install_refuses_a_second_content_seed(db, tmp_path, empty_install):
    """A restore with no seed row would let a second content seed in: two minters in one namespace."""
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
    # Listed, not filtered, so a third rule arriving here is noticed.
    assert [f.rule for f in refusal.failures] == ["seed-once", "vocabulary-migration"]

    # Seeded, not *active*: an active row with no files is the "broken install" branch.
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
    """Sequence names are executed via `setval`, so ones the archive does not own are refused."""
    await _seed_movie_data(db)
    await db.execute("INSERT INTO app_user (name, role) VALUES ($1, 'member')", MARKER_USER)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    def plant(manifest):
        manifest["sequences"]["public.app_user_id_seq"] = {"last_value": 1, "is_called": False}

    tampered = _with_edited_manifest(report.path, tmp_path / "tampered.zip", plant)
    with pytest.raises(movie_data.RestoreRefused, match="app_user_id_seq"):
        await movie_data.restore_archive(empty_install, tampered)

    # Refused before it wrote.
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


def _fake_dumps(directory: Path, count: int) -> list[Path]:
    """Dated well before today, so a real dump beside them is unambiguously newest."""
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for nth in range(1, count + 1):
        path = directory / f"spielplan-2024{nth:02d}01T030000Z.dump"
        path.write_bytes(b"PGDMP-not-really")
        made.append(path)
    return made


def test_rotation_keeps_the_newest_fourteen(tmp_path):
    """§2: "rotation 14", checked without a database or `pg_dump`."""
    directory = tmp_path / "backups"
    made = _fake_dumps(directory, 20)

    pruned = nightly.prune(directory)

    survivors = sorted(p.name for p in directory.glob("*.dump"))
    assert nightly.KEEP == 14
    assert survivors == sorted(p.name for p in made[-14:])
    assert sorted(pruned) == sorted(p.name for p in made[:6])


def test_rotation_leaves_files_it_did_not_write_alone(tmp_path):
    """A delete that guesses at what it owns removes the operator's own copy."""
    directory = tmp_path / "backups"
    _fake_dumps(directory, 20)
    (directory / "before-the-upgrade.dump.keep").write_bytes(b"mine")
    (directory / "notes.txt").write_bytes(b"mine")

    nightly.prune(directory)

    assert (directory / "before-the-upgrade.dump.keep").exists()
    assert (directory / "notes.txt").exists()


def test_rotation_removes_interrupted_dumps(tmp_path):
    """`*.dump.partial` from a killed `pg_dump` matches nothing else, so rotation must remove it."""
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
    """argv is world-readable; PGPASSWORD carries the DECODED password, since libpq decodes the URI."""
    url = "postgresql://spielplan:s3cr3t%2Fp%40ss@db.local:5432/spielplan"
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env")
        kwargs["stdout"].write(b"PGDMP")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    # `dump` refuses when the binary is not on PATH; any real executable satisfies that.
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
    """§2: "nightly `pg_dump` to `/data/backups`, rotation 14", as the worker runs it."""
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
    """Searched in SQL expanded from the dump, with two controls proving it is this database's dump."""
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
    """§2: without `SECRETS_KEY` a restored dump's connector secret must stay undecryptable."""
    import asyncpg

    await _seed_movie_data(db)
    await _seed_connector_secret(db, monkeypatch)

    report = await nightly.run()
    with report.path.open("rb") as fh:
        _run([*_client("pg_restore"), "--dbname", _inside_the_container(blank_url)], stdin=fh)

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


# An operator's pre-upgrade copy matches `spielplan-*.dump`, and `-` sorts below `0`.
OPERATOR_COPY = "spielplan-2026-08-14-before-upgrade.dump"


def _nights(directory: Path, count: int, month: str = "202608") -> list[Path]:
    """Dated so they sort *after* the operator's hand-named copy, which exposes the bug."""
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for day in range(1, count + 1):
        path = directory / f"spielplan-{month}{day:02d}T030000Z.dump"
        path.write_bytes(b"PGDMP-not-really")
        made.append(path)
    return made


@pytest.fixture
def backups(tmp_path, monkeypatch):
    """`DATA_DIR` alone; these tests never reach the database."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield tmp_path / "data" / "backups"
    settings.cache_clear()


def test_rotation_leaves_the_operators_own_dated_copy_alone(tmp_path):
    """Fourteen nights plus one hand-saved copy: the old glob deleted the operator's."""
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
    """Partials are removed outright, so a wrongly claimed name is gone on the first prune."""
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
    """`prune` deletes from `dumps()`, so membership is the whole safety property."""
    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / name).write_bytes(b"not ours")
    (directory / f"{name}{nightly.PARTIAL}").write_bytes(b"nor this")

    assert nightly.dumps(directory) == []
    assert nightly.interrupted(directory) == []
    assert nightly.prune(directory) == []


def test_every_name_the_job_generates_is_one_it_owns(tmp_path):
    """A matcher drifting from `dump_name` would stop rotation seeing its own files."""
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
    """Rebuilding from `hostname` strips IPv6 brackets, which libpq then rejects."""
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
    """A characterisation test: libpq decodes the username itself."""
    dsn, credential = nightly._connection("postgresql://ops%40house:p%2Fw@db.local/spielplan")

    assert dsn == "postgresql://ops%40house@db.local/spielplan"
    assert credential["PGPASSWORD"] == "p/w"


def test_a_missing_pg_dump_is_this_modules_own_error_and_leaves_no_debris(tmp_path, monkeypatch):
    """A missing client used to leak a 0-byte partial per attempt."""
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
    """Jobs run in sequence, so an unbounded dump would stop the one-minute poll for ever."""
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


async def test_a_dump_from_yesterday_does_not_stop_tonights(backups, monkeypatch):
    backups.mkdir(parents=True)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    (backups / nightly.dump_name(yesterday)).write_bytes(b"PGDMP-the-night-before")

    def fake_dump(database_url: str, path: Path) -> int:
        path.write_bytes(b"PGDMP-tonight")
        return path.stat().st_size

    monkeypatch.setattr(nightly, "dump", fake_dump)

    report = await nightly.run()

    assert report.path.name.startswith(f"spielplan-{datetime.now(UTC):%Y%m%d}T")
    assert report.path.read_bytes() == b"PGDMP-tonight"
    assert report.kept == 2 and report.pruned == ()


# `spielplan-movie-data` exposes these to a live worker, so
# the archive must be a snapshot and the restore locked.


class _PausingConnection:
    """asyncpg offers no seam inside a COPY, so the connection delegates and awaits a hook first."""

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
    """Paused between `title` and its children, where a per-table snapshot let orphan rows in."""
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
    """Reading `last_value` before the rows lets `setval` wind back under an archived id."""
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
    """The position is bounded by `max(id)` per table, for rows the sequence never minted."""
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
    """`LOCK TABLE ... IN EXCLUSIVE MODE` makes "holds no movie data"
    a decision; `lock_timeout` turns the wait into an error."""
    await _seed_movie_data(db)
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    other = await asyncpg.connect(sibling(pg_url, "_restore")[2])

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
    """A hand-edited manifest is a refusal, not a `KeyError`."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    tampered = _with_edited_manifest(report.path, tmp_path / "tampered.zip", edit)

    with pytest.raises(movie_data.RestoreRefused):
        await movie_data.restore_archive(empty_install, tampered)
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


async def test_a_restore_leaves_no_title_claiming_a_placement_it_has_no_basis_for(
    db, tmp_path, empty_install
):
    """No `title_placement` is carried, so `title.placement` must not claim a coordinate."""
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
    """`ZipFile(path, "w")` truncates in place, so the
    write goes to a partial; failure is placed mid-loop."""
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
        # Whole: the manifest is written last, so a truncated archive lacks it.
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
    """A missing member raises `KeyError`, which the command did not catch."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    truncated = _without_member(report.path, tmp_path / "truncated.zip", member)

    with pytest.raises(movie_data.RestoreRefused) as refusal:
        await movie_data.restore_archive(empty_install, truncated)
    assert named in str(refusal.value), "the refusal does not say what is missing"
    assert await empty_install.fetchval("SELECT count(*) FROM title") == 0


async def _run_cli(*argv: str) -> int:
    """In a thread because `main` calls `asyncio.run`; driving `main` is the point."""
    return await asyncio.to_thread(movie_data.main, list(argv))


async def test_the_operator_command_writes_an_archive_and_restores_it(
    db, pg_url, tmp_path, monkeypatch, capsys
):
    """Through `db/pool.open_pool`, whose jsonb codec the module's `::text::jsonb` cast exists for."""
    await _seed_movie_data(db)
    await db.execute("SELECT setval('title_id_seq', $1, true)", APP_ID_FLOOR)
    archive = tmp_path / "movie-data.zip"

    admin, name, url = sibling(pg_url, "_cli")
    await create_database(admin, name)
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

        # A second restore is decision 162's refusal: an exit code, not a traceback.
        assert await _run_cli("restore", str(archive)) == 1
        assert "refusing" in capsys.readouterr().out
        assert await _run_cli("restore", str(tmp_path / "nowhere.zip")) == 1
    finally:
        await conn.close()
        await drop_database(admin, name)
        settings.cache_clear()


async def test_the_operator_restoring_a_pre_291_archive_is_told_what_was_passed_over(
    db, pg_url, tmp_path, empty_install, monkeypatch, capsys
):
    """`_run`'s printed line is the only place `retired`
    is shown, so it is asserted by name and in ASCII."""
    await _seed_movie_data(db)
    with monkeypatch.context() as older_build:
        older_build.setattr(movie_data, "TABLES", _pre_291_tables())
        report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")
    assert "public.ml_genome_score" in report.tables, "the fixture is not a pre-291 archive"

    # `empty_install`'s own database, addressed the way the command addresses it.
    monkeypatch.setenv("DATABASE_URL", sibling(pg_url, "_restore")[2])
    settings.cache_clear()
    try:
        assert await _run_cli("restore", str(report.path)) == 0
    finally:
        settings.cache_clear()

    printed = capsys.readouterr().out
    unnamed = sorted(name for name in GENOME_RETIRED if name not in printed)
    assert not unnamed, f"the restore line does not name {unnamed}: {printed!r}"
    assert "decision 291" in printed, printed
    assert printed.isascii(), f"a restore line a cp1252 console cannot print: {printed!r}"


async def test_an_archive_written_on_a_pre_291_install_names_what_it_leaves_behind(
    db, pg_url, tmp_path, monkeypatch, capsys
):
    """Pre-291 data archived by THIS build: the line names the passed-over tables and points at the dump."""
    await _seed_movie_data(db)

    # The negative direction first, on the install THIS build seeds: nothing to name, nothing said.
    clean = await movie_data.write_archive(db, tmp_path / "post-291.zip")
    assert clean.retired == ()
    assert clean.as_dict()["retired"] == []

    # The slice as the pre-291 importer left it, the same three rows the restore's fixture uses.
    await db.execute("INSERT INTO ml_genome_tag (tag_id, tag) VALUES (1, 'melancholy')")
    await db.execute(
        "INSERT INTO ml_link (ml_movie_id, title_id, imdb_id, tmdb_id) "
        "VALUES (1, 11, 'tt0079944', 101)"
    )
    await db.execute(
        "INSERT INTO ml_genome_score (ml_movie_id, tag_id, relevance) VALUES (1, 1, 0.75)"
    )

    report = await movie_data.write_archive(db, tmp_path / "pre-291-data.zip")
    assert report.retired == GENOME_RETIRED
    assert report.as_dict()["retired"] == list(GENOME_RETIRED)
    assert not set(report.tables) & movie_data.RETIRED, "the slice must still not be archived"

    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    try:
        assert await _run_cli("write", str(tmp_path / "by-hand.zip")) == 0
    finally:
        settings.cache_clear()

    printed = capsys.readouterr().out
    unnamed = sorted(name for name in GENOME_RETIRED if name not in printed)
    assert not unnamed, f"the write line does not name {unnamed}: {printed!r}"
    assert "decision 291" in printed, printed
    assert printed.isascii(), f"a write line a cp1252 console cannot print: {printed!r}"


async def test_a_destination_the_command_cannot_write_is_a_refusal_and_not_a_traceback(
    db, pg_url, tmp_path, monkeypatch, capsys
):
    """A destination the command cannot write is a `refusing:` line; the target is never replaced."""
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
    """A failed rename must also remove the whole-archive `.partial`."""
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
    """The archive with its manifest written back verbatim, which `_with_edited_manifest` cannot express."""
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
    """A `JSONDecodeError` is a `ValueError`, which `_run` did not catch."""
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

    # Through the entry point, where the refusal becomes a sentence; ahead of any destination question.
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
    """A catalog without the archived tables raises `RuntimeError`, which must be a refusal."""
    await _seed_movie_data(db)
    report = await movie_data.write_archive(db, tmp_path / "movie-data.zip")

    admin, name, url = sibling(pg_url, "_bare")
    await create_database(admin, name)
    unmigrated = await asyncpg.connect(url)
    try:
        with pytest.raises(movie_data.RestoreRefused, match="tables this schema does not have"):
            await movie_data.restore_archive(unmigrated, report.path)
    finally:
        await unmigrated.close()
        await drop_database(admin, name)
