"""The raw store's invariants, asserted against a real disk and a real Postgres. Spec v2.1 §8.

One file, two rows. That pair is `data-rules-fetched-bytes-are-kept-once-and-re-read` and it is
the whole reason the store is content-addressed: §8 promises "All fetched bytes land in the app's
own raw store, so re-parsing is free forever" (`spec:402`), and a store that rewrote the file on
the second fetch, or collapsed the second fetch into the first row, would break one half of that
promise each. So both halves are asserted in the direction that catches their repair -- the file
is proved *not* rewritten by tampering with it between the two writes, because two `store` calls
that both wrote leave a directory indistinguishable from two that did not.

The other three claims here are absences, which is the kind of thing a later reader repairs on
sight: this module imports no HTTP client (a `read` that could re-fetch is not a re-parse path),
it refuses a `content_path` that resolves outside the root (a row is not a constant), and the
bytes it writes are on a volume the backend container does not have (decision 345).

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import gzip
import re
import socket
import zlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from spielplan.acquire import rawstore
from spielplan.core.config import settings

# backend/spielplan/acquire/rawstore.py -> the worktree root, where docker-compose.yml lives.
REPO_ROOT = Path(rawstore.__file__).resolve().parents[3]


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """A raw store of this test's own, reached the way the worker reaches it.

    `DATA_DIR` and not a parameter: `settings().raw_dir` is the store's root in production and
    the module takes no root argument on purpose, because a root that can be passed in is a root
    a caller can pass wrong -- and the one caller that matters runs in a container where the
    directory is a bind mount (see `rawstore`'s Custody paragraph). `settings()` is `lru_cache`d,
    so the cache is cleared on both sides; `test_backup.py` uses the same idiom for `/data`.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _anchor_mounts(compose: str, anchor: str) -> list[str]:
    """The list under one YAML anchor, read as text rather than parsed.

    A YAML parser resolves the `*worker-volumes` aliases and would answer for the services; the
    question here is what the anchor itself carries, because that is where an edit lands.
    """
    lines = compose.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{anchor}:"))
    mounts = []
    for line in lines[start + 1:]:
        if not line.startswith("  - "):
            break
        mounts.append(line[4:].strip())
    assert mounts, f"{anchor} is no longer a list of mounts where this reads it"
    return mounts


def _raw_store_host_dir(compose: str) -> str:
    """The host directory this module's bytes land in, as the compose file spells it.

    Read off the worker's own mount rather than named here as a literal, for the reason
    `_dumps_host_dir` gives at `test_static_contracts.py:949-955`: the rule decision 345 states is
    about that DIRECTORY, and a host directory renamed while `/data/raw` stays where it is would
    leave a guard written around the literal asserting nothing.
    """
    hosts = [mount.split(":")[0] for mount in _anchor_mounts(compose, "x-worker-volumes")
             if mount.split(":")[1] == "/data/raw"]
    assert hosts, "no worker mount delivers /data/raw, so the raw store has no host directory"
    return hosts[0]


def _delivers(host: str, root: str) -> bool:
    """Would mounting `host` put everything in `root` inside the container?

    `test_static_contracts.py:957-960`'s predicate, restated rather than imported. That module
    owns the M0 and M4.7 compose guards and reads the file through its own mount parser; this
    pair deliberately lives beside the module that writes the bytes, because M5.1 is the mount's
    first user and so the one that owes the assertion (decision 345). Three lines are the cheaper
    of the two couplings.
    """
    host = host.rstrip("/") or "/"
    return root == host or root.startswith(f"{host}/")


def _backend_mounts_holding_the_raw_store(compose: str) -> list[str]:
    """The backend mounts that put this module's bytes inside the process that answers HTTP.

    BOTH SIDES OF THE COLON, which is the whole of the backups guard's lesson and not the half
    this file first took. Asked of the container path alone, `- ./data/raw:/raw` is green: it is
    the same host directory under a name the filter cannot see, it is the exact shape the `db`
    service already carries one service down (`- ./data/backups:/backups`), and it is the line
    somebody adds the day an admin route wants to "just read the file".
    `test_static_contracts.py:994-1000` records that shape defeating the dumps guard's first
    form, which is why `_delivers` exists there and why the host side is asked here.
    [M5.1 review cycle 1, M51-REV-CUSTODY-02]

    No mode clause, unlike `_backend_mounts_holding_the_dumps`. The dumps' rule is that the HTTP
    process holds no WRITABLE copy, so `:ro` satisfies it; decision 345's is that the backend
    cannot open the file at any price, and a read-only mount opens it.

    A function over the compose text rather than an expression inside the guard, so the self-tests
    below drive the rule that actually runs: the predicate written inline was re-typed into its
    own proof, and a narrowing of the one that ran left the one that proved it passing.
    [M5.1 review cycle 1, M51-REV-CUSTODY-05]
    """
    raw = _raw_store_host_dir(compose)
    return sorted(
        mount for mount in _anchor_mounts(compose, "x-backend-volumes")
        if _delivers(mount.split(":")[0], raw)
        or PurePosixPath("/data/raw").is_relative_to(PurePosixPath(mount.split(":")[1]))
    )


def _backend_gains(compose: str, mount: str) -> str:
    """The same compose file with one more line under the backend's volume anchor."""
    anchor = "x-backend-volumes: &backend-volumes\n"
    assert anchor in compose, "the backend's volume anchor is no longer where this reads it"
    return compose.replace(anchor, f"{anchor}  - {mount}\n", 1)


def _module_imports() -> set[str]:
    tree = ast.parse(Path(rawstore.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    return imported


# --- §8: one file, two rows -----------------------------------------------------------


async def test_two_fetches_of_one_url_leave_one_file_and_two_rows(db, raw_root):
    """`mdc/rawstore.py:1-11`: "Two fetches that return identical bytes share one file but get
    two rows, so the fetch history stays visible without duplicating bulk."

    Both halves fail in a way the other cannot see. A store that rewrote the file would still
    leave one file and two rows, so the second call has to be watched rather than inspected: the
    store is handed content whose digest already names a file on disk, and whether it touched that
    file is the whole assertion.

    The second half -- two rows -- is what `0024` leaves `content_sha256` deliberately not unique
    for. Collapsing them would make "when did we last see this url" unanswerable, which is the
    question both a conditional re-fetch and §6.6's board ask.

    HOW THE WRITE IS WATCHED CHANGED, AND THE REASON IS WORTH KEEPING. This test used to tamper
    with the file between the two calls and read the tampered bytes back, resting on the store
    having "no cheap way to doubt" a file of the right length - gzip's ISIZE trailer was all it
    asked. It is not the right question, and a same-length tamper is now exactly what
    `_holds_the_document` catches and REWRITES on purpose, which is
    `test_a_stored_file_whose_body_is_damaged_is_rewritten_by_the_next_store`. So the proof moved
    to the write block's only route to the disk: `_tmp_name` is called once per write and by
    nothing else, so a `_tmp_name` that raises turns "did it write" into something the store
    cannot pass by accident - and it no longer asserts write-once by requiring the store to be
    fooled. [M5.1 review cycle 2, port-10; M5.1 review cycle 4 second pass, M51-C4-RAW-03]
    """
    body = b'{"title": "Arrival", "year": 2016}'
    url = "https://api.themoviedb.org/3/movie/329865"
    first = await rawstore.store(
        db, source="tmdb", kind="detail", url=url, content=body, entity_key="tmdb:329865",
    )
    [path] = _files(raw_root)
    intact = path.read_bytes()

    def refuse(_dest):
        raise AssertionError(
            "the second fetch of identical bytes wrote the file again - write-once is what makes "
            "the store safe to point two rows at, and rewriting it is a write to a path other "
            "rows already name"
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rawstore, "_tmp_name", refuse)
        second = await rawstore.store(
            db, source="tmdb", kind="detail", url=url, content=body, entity_key="tmdb:329865",
        )

    assert _files(raw_root) == [path], "the second fetch wrote a second file for identical bytes"
    assert path.read_bytes() == intact, "the bytes on disk are the first write's, to the byte"
    rows = await db.fetch("SELECT * FROM raw_document ORDER BY id")
    assert [row["id"] for row in rows] == [first, second]
    assert rows[0]["content_path"] == rows[1]["content_path"]
    assert rows[0]["content_sha256"] == rows[1]["content_sha256"] == rawstore.sha256_bytes(body)
    assert rawstore.read_path(rows[1]["content_path"]) == body


async def test_different_bytes_leave_two_files(db, raw_root):
    """The other direction, which is the one a dedupe bug makes green.

    A `store` that returned early on any existing entity would pass the test above and lose the
    second document entirely. Content addressing means the digest decides, so bytes that differ
    by one character are a different file and a different row even at the same url -- which is
    what makes a re-fetch of a page that changed worth doing at all.
    """
    url = "https://www.metacritic.com/movie/arrival"
    a = await rawstore.store(db, source="metacritic", kind="page", url=url, content=b"<html>a")
    b = await rawstore.store(db, source="metacritic", kind="page", url=url, content=b"<html>b")

    assert len(_files(raw_root)) == 2
    rows = await db.fetch("SELECT id, content_sha256, content_path FROM raw_document ORDER BY id")
    assert [row["id"] for row in rows] == [a, b]
    assert rows[0]["content_sha256"] != rows[1]["content_sha256"]
    assert rows[0]["content_path"] != rows[1]["content_path"]


# --- §8: re-parsing is free forever ---------------------------------------------------


async def test_a_read_returns_the_stored_bytes_byte_for_byte(db, raw_root):
    """Byte-for-byte, over content that is not text, because "re-parse forever" is a promise
    about bytes and what they mean is §8 stage 3's problem.

    The content here is deliberately not valid UTF-8 and carries a NUL and a CRLF: a store that
    round-tripped through `str` anywhere would pass on JSON and corrupt the two HTML scrapes §8
    stage 2 names. `read_text`'s lossy decode is asserted beside it so that `replace` is visible
    as the *text* reader's choice and never the byte reader's.
    """
    body = b'{"a": 1}\r\n\x00\xff\xfe binary tail'
    doc_id = await rawstore.store(
        db, source="rt", kind="page", url="https://www.rottentomatoes.com/m/arrival_2016",
        content=body, content_type="text/html; charset=utf-8",
    )
    assert await rawstore.read(db, doc_id) == body
    assert "binary tail" in await rawstore.read_text(db, doc_id)

    json_id = await rawstore.store(
        db, source="tmdb", kind="detail", url="https://api.themoviedb.org/3/movie/329865",
        content=b'{"id": 329865}',
    )
    assert await rawstore.read_json(db, json_id) == {"id": 329865}

    # A row that is not there is a KeyError and never an empty document: a derive handed b"" for
    # a missing id would write a title with no metadata and call the stage done.
    with pytest.raises(KeyError):
        await rawstore.read(db, 10_000_000)


async def test_the_re_parse_path_issues_no_request(db, raw_root, monkeypatch):
    """§8: "All fetched bytes land in the app's own raw store, so re-parsing is free forever."

    Free means no request, so the assertion is that a read *cannot* make one -- both dynamically,
    with sockets taken away, and statically, because a lazy re-fetch added to `read` later would
    need an import and would still pass a socket-less test if it were only reached on a miss.

    The static half names modules rather than grepping for "http": `urllib` and `socket` open
    connections without the word appearing anywhere, and the claim is that this module has no way
    to reach the network at all rather than that it avoids one library.
    """
    body = b"<html>cached</html>"
    doc_id = await rawstore.store(
        db, source="wikipedia", kind="article", url="https://en.wikipedia.org/wiki/Arrival",
        content=body, content_type="text/html",
    )
    rel = await db.fetchval("SELECT content_path FROM raw_document WHERE id = $1", doc_id)

    def no_sockets(*args, **kwargs):
        raise AssertionError("the raw store opened a socket to re-read bytes it already has")

    monkeypatch.setattr(socket, "socket", no_sockets)
    assert rawstore.read_path(rel) == body

    # A guard that parses nothing is green forever, and this one is a set intersection - the
    # cheapest way for it to stop working is for `_module_imports` to start returning nothing.
    assert {"gzip", "asyncpg", "spielplan"} <= _module_imports()

    network = {"httpx", "urllib", "urllib3", "requests", "socket", "http", "aiohttp"}
    assert not _module_imports() & network, (
        f"the raw store imports a network client: {sorted(_module_imports() & network)}. A read "
        "that can fall back to a fetch makes 'free forever' a promise about the common case, and "
        "the case it stops covering is the one the store exists for"
    )


async def test_latest_reads_the_last_document_that_worked(db, raw_root):
    """What §8 stage 3 asks for: "the tmdb detail for this title", meaning the newest good one.

    The `ok` filter is ported from `mdc/rawstore.py`'s `latest` rather than inherited by accident,
    and this asserts it in the direction that matters: a failed fetch written *after* a good one
    is fetch history and must not become the thing the derive parses. The two rows are separated
    by an explicit interval rather than by whatever the clock did between two statements.
    """
    key = "tmdb:329865"
    good = await rawstore.store(
        db, source="tmdb", kind="detail", url="u1", content=b'{"ok": 1}', entity_key=key,
    )
    bad = await rawstore.store(
        db, source="tmdb", kind="detail", url="u2", content=b"upstream 500", entity_key=key,
        http_status=500, ok=False, error="500 from tmdb",
    )
    await db.execute(
        "UPDATE raw_document SET fetched_at = fetched_at + interval '1 minute' WHERE id = $1", bad
    )

    row = await rawstore.latest(db, "tmdb", "detail", key)
    assert row["id"] == good
    assert await rawstore.latest(db, "tmdb", "detail", "tmdb:nothing-here") is None


async def test_latest_for_url_carries_the_validators_a_conditional_request_needs(db, raw_root):
    """The reader of `raw_document_url`, the index `0024` added when it folded `http_cache` in.

    No `ok` filter, and the asymmetry with `latest` above is the assertion: the question here is
    "what happened at this url last", which is what §6.6's board and an operator chasing a stuck
    title ask, and a 304 or a 404 answers it as well as a 200 does.

    IT IS NOT THE `If-None-Match` READ, although this module used to say it was. That read is
    `fetch._validators`, which filters on `ok` and argues the opposite case correctly - a validator
    taken from an error page is how a store comes to hold a 503 under the name of the document. Two
    functions over one url with two beliefs about what was last seen there is the
    `http_cache`-as-second-table shape the fold exists to prevent, so the prose is one answer now
    and the behaviour of each function is unchanged. [M5.1 review cycle 1, port-05]
    """
    url = "https://www.omdbapi.com/?i=tt2543164"
    await rawstore.store(db, source="omdb", kind="detail", url=url, content=b"{}", etag='W/"1"')
    newest = await rawstore.store(
        db, source="omdb", kind="detail", url=url, content=b"", http_status=304, ok=False,
        etag='W/"2"', last_modified="Wed, 17 Sep 2026 00:00:00 GMT",
    )
    await db.execute(
        "UPDATE raw_document SET fetched_at = fetched_at + interval '1 minute' WHERE id = $1",
        newest,
    )

    row = await rawstore.latest_for_url(db, url)
    assert row["id"] == newest
    assert row["etag"] == 'W/"2"'
    assert row["last_modified"] == "Wed, 17 Sep 2026 00:00:00 GMT"


# --- review cycle 1 ------------------------------------------------------------------------


async def test_two_documents_written_in_one_transaction_are_still_ordered(db, raw_root):
    """`fetched_at` alone does not order these rows, and all three readers ordered on it alone.

    Its default is Postgres's `now()`, which is TRANSACTION start time - so two documents a stage
    stores for one url inside its own transaction carry a byte-identical timestamp, and
    `ORDER BY fetched_at DESC LIMIT 1` returns whichever the plan reaches first, which under an
    index scan is the OLDEST. `store`'s own docstring designs for that usage: "Not a transaction
    of its own. The caller is a stage of §8's pipeline and owns that boundary."

    The corpus needed no tiebreaker because `mdc/rawstore.py:120` stamped each INSERT from
    `time.time()`; named change 1 replaced that clock with a per-transaction one and kept the
    query, which is a corpus invariant the port changed quietly. `acquire/board.py:99` already
    reads this same table with `fetched_at DESC, id DESC` - one milestone, one table, two answers.
    [M5.1 review cycle 1, port-03]
    """
    url = "https://api.themoviedb.org/3/movie/603"
    key = "tmdb:603"
    async with db.transaction():
        first = await rawstore.store(
            db, source="tmdb", kind="detail", url=url, content=b'{"n": 1}', entity_key=key,
            etag='"OLD"',
        )
        second = await rawstore.store(
            db, source="tmdb", kind="detail", url=url, content=b'{"n": 2}', entity_key=key,
            etag='"NEW"',
        )

    stamps = [r["fetched_at"] for r in await db.fetch(
        "SELECT fetched_at FROM raw_document WHERE url = $1 ORDER BY id", url
    )]
    assert stamps[0] == stamps[1], "the fixture is only interesting while the timestamps tie"

    assert (await rawstore.latest_for_url(db, url))["id"] == second
    assert (await rawstore.latest(db, "tmdb", "detail", key))["id"] == second
    assert first != second


async def test_a_zero_length_file_under_a_good_digest_is_rewritten(db, raw_root):
    """Write-once trusts the name, and the one shape of damage it cannot be allowed to trust.

    `replace` is atomic against the DIRECTORY ENTRY; this module issues no `fsync`, so a power cut
    between the rename and the page-cache flush leaves the row committed in Postgres and an empty
    file under a perfectly good digest. An empty gzip stream decompresses to `b""` with NO
    exception - so a derive would read a healthy-looking empty document rather than failing, which
    is the silent half of the failure and the one `BadGzipFile` does not cover.

    Hashing every stored file on every store would make the common path pay for the rare one; a
    zero-length test cannot misfire, because no valid gzip is zero bytes.
    [M5.1 review cycle 1, port-09]

    AND THE SHORT FILE, which is the same crash's other outcome and was trusted for ever. The
    guard covered exactly one of the two shapes the missing `fsync` admits, while the module
    docstring promised both - "a crash leaves either the whole old file or the whole new one and
    never half of one". A truncated-but-non-empty file has no repair path at all: the digest names
    the destination, so every later fetch of the same bytes skipped the write and added another
    row pointing at the broken file, and every derive of it raises `EOFError` until an operator
    deletes a path they have to read out of `raw_document`. The test now asks gzip's own ISIZE
    trailer, which is four bytes and answers the question the digest cannot.
    [M5.1 review cycle 2, port-10]
    """
    body = b'{"detail": "the real bytes"}'
    doc = await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/1", content=body, entity_key="tmdb:1",
    )
    rel = await db.fetchval("SELECT content_path FROM raw_document WHERE id = $1", doc)
    path = rawstore.resolve(rel)
    path.write_bytes(b"")
    assert path.stat().st_size == 0

    await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/1", content=body, entity_key="tmdb:1",
    )
    assert rawstore.read_path(rel) == body

    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])
    assert 0 < path.stat().st_size < len(whole), "this test needs a short file, not an empty one"
    with pytest.raises(EOFError):
        rawstore.read_path(rel)

    await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/1", content=body, entity_key="tmdb:1",
    )
    assert rawstore.read_path(rel) == body, (
        "a file truncated by a crash was trusted for ever - the digest names the destination, so "
        "no later fetch of the same bytes can ever repair it"
    )


async def test_a_304_cannot_be_stored_as_a_good_document(db, raw_root):
    """`latest_for_url`'s docstring states the rule - "a 304 is stored `ok = false`" - and nothing
    kept it.

    That sentence is the argument for the whole two-readers arrangement that replaced the corpus's
    separate `http_cache` table, and it is a rule about a WRITE stated in the docstring of a READ.
    `store` defaults `ok=True`, applies no rule to `http_status` and none to an empty body, while
    `rawstore.latest` - the per-title derive's read - filters on `ok` alone. So an M5.3 adapter
    storing the 304 the way `fetch.get` hands it back (status 304, `content = b""`,
    `from_cache=True`) made a zero-byte row the newest good document for that entity, and every
    later derive read `b""` as the title's page.

    SILENTLY, which is what makes it worth a refusal rather than a comment: a zero-length gzip
    decompresses to `b""` with no exception, the same property `test_a_zero_length_file_...`
    already exists for, and under decision 162 the thinned title a derive then writes cannot be
    rewritten. The rule now lives where the write is. [M5.1 review cycle 2, port-304-01]
    """
    url = "https://api.themoviedb.org/3/movie/329865"
    await rawstore.store(
        db, source="tmdb", kind="detail", url=url, content=b'{"title": "Arrival"}',
        entity_key="tmdb:329865",
    )

    with pytest.raises(ValueError, match="304"):
        await rawstore.store(
            db, source="tmdb", kind="detail", url=url, content=b"", entity_key="tmdb:329865",
            http_status=304, etag='"v1"',
        )
    with pytest.raises(ValueError, match="empty document"):
        await rawstore.store(
            db, source="tmdb", kind="detail", url=url, content=b"", entity_key="tmdb:329865",
        )

    # The fetch is still recordable - it is fetch history, which is what §6.6's board reads.
    noted = await rawstore.store(
        db, source="tmdb", kind="detail", url=url, content=b"", entity_key="tmdb:329865",
        http_status=304, ok=False, etag='"v1"',
    )
    assert noted is not None
    newest = await rawstore.latest(db, "tmdb", "detail", "tmdb:329865")
    assert newest["byte_size"] == len(b'{"title": "Arrival"}'), (
        "a 304 became the newest good document, and every later derive read b'' as the page"
    )
    assert (await rawstore.latest_for_url(db, url))["http_status"] == 304, (
        "the fetch history still shows what happened at this url last"
    )


async def test_a_write_that_fails_leaves_no_temporary_file_behind(db, raw_root, monkeypatch):
    """The `.tmp` is unique to the write and nothing in the tree ever removes one.

    `/data/raw` is a bind mount with no sweeper, so a write that RAISED - a full disk, a
    permission error, the `replace` this fixture breaks - used to leave one orphan per failed
    write, never read, never counted, never named. Two lines of `finally` remove it.
    [M5.1 review cycle 1, port-09]

    A RAISE AND NOT A KILL, which is what this docstring used to claim and what the fixture never
    built. No `finally` runs when a process is killed, so a killed writer does leave its `.tmp`
    behind - measured, and admitted in `store`'s own comment rather than swept. What this test
    pins is the case that unwinds. [M5.1 review cycle 4, M51-C4-RAW-02]
    """
    written: list[Path] = []

    def never_lands(self, _target):
        written.append(self)
        raise RuntimeError("the worker was killed between the write and the rename")

    monkeypatch.setattr(Path, "replace", never_lands)
    with pytest.raises(RuntimeError):
        await rawstore.store(
            db, source="tmdb", kind="detail", url="https://x/2", content=b"never lands",
            entity_key="tmdb:2",
        )
    monkeypatch.undo()

    assert written and written[0].name.endswith(".tmp"), "the fixture killed the rename"
    assert list(settings().raw_dir.rglob("*.tmp")) == [], (
        f"a killed write left {written[0].name} on a volume with no sweeper"
    )


# --- §6.6: the row is what the board gets ---------------------------------------------


async def test_the_row_records_what_the_board_shows(db, raw_root):
    """Decision 345: §6.6's board links to the `raw_document` metadata row -- "url, http status,
    sha256, byte size, fetched_at, all in Postgres" -- and never to the bytes.

    So each of those five is asserted to be on the row rather than derivable from a file the
    backend cannot open. `byte_size` is the RAW length and is asserted to differ from the size on
    disk, because a gzipped size would make §8 stage 4's "is this pack thin" a question about
    compression; the content is chosen to compress so that the two numbers cannot coincide.

    `fetched_at` is checked for being the database's own `now()` rather than against a Python
    instant: it is asserted timezone-aware and within the minute, which catches the column not
    being populated without turning a clock skew between this process and Postgres into a red
    build.
    """
    body = b"a" * 4096
    doc_id = await rawstore.store(
        db, source="trakt", kind="comments", url="https://api.trakt.tv/movies/arrival/comments",
        content=body, entity_key="trakt:arrival", http_status=200,
        content_type="application/json", page=2, request_meta={"attempt": 1}, run_id=77,
    )

    row = await db.fetchrow("SELECT * FROM raw_document WHERE id = $1", doc_id)
    assert row["url"] == "https://api.trakt.tv/movies/arrival/comments"
    assert row["http_status"] == 200
    assert row["content_type"] == "application/json"
    assert row["byte_size"] == len(body)
    assert row["content_sha256"] == rawstore.sha256_bytes(body)
    assert row["fetched_at"].tzinfo is not None
    assert abs((row["fetched_at"] - datetime.now(UTC)).total_seconds()) < 60
    assert (row["source"], row["kind"], row["entity_key"]) == ("trakt", "comments", "trakt:arrival")
    assert (row["page"], row["ok"], row["error"], row["run_id"]) == (2, True, None, 77)
    assert row["request_meta"] == {"attempt": 1}

    on_disk = rawstore.resolve(row["content_path"]).stat().st_size
    assert on_disk < row["byte_size"], (
        "byte_size is the size of what the derive will parse, not the size of the gzip - the two "
        "are equal only when nothing compressed, and a test that cannot tell them apart passes "
        "on either"
    )
    # The suffix is for whoever lists the directory on the box, which is the one interface these
    # files have; nothing reads it back. `.json` here, and the fan-out is the corpus's.
    assert row["content_path"].startswith("trakt/comments/")
    assert row["content_path"].endswith(".json.gz")


# --- containment and custody ----------------------------------------------------------


async def test_a_content_path_that_escapes_the_root_is_refused(db, raw_root):
    """A `content_path` comes back out of a row, and a row is not a constant.

    The third case is the one M4.14 paid for and the reason `resolve` compares paths rather than
    prefixes: `<data>/rawer` shares every character of `<data>/raw`, so
    `str(target).startswith(str(root))` accepts it -- which is exactly how the admin bundle route
    accepted `/database` for a `DATA_DIR` of `/data`, under a comment claiming it was a boundary
    (`api/artifacts.py:118-130`). Written as a sibling of the real root rather than as another
    `..` walk, because the `..` cases are the ones a prefix check already catches.

    Held on both the read and the write side, since `store` resolves before it opens a file: a
    `source` or `kind` carrying a separator is sanitised by `_safe_component`, but the boundary
    has to be the thing that is true rather than the thing upstream of it.
    """
    ok_rel = "tmdb/detail/ab/cd/abcd.json.gz"
    assert rawstore.resolve(ok_rel) == (raw_root / ok_rel).resolve()

    for escape in (
        "../artifacts/cold_tower.pt",
        "tmdb/../../backups/dump.sql",
        "../rawer/leaked.json.gz",
        str(raw_root.parent / "backups" / "dump.sql"),
    ):
        with pytest.raises(ValueError, match="escapes"):
            rawstore.resolve(escape)
        with pytest.raises(ValueError, match="escapes"):
            rawstore.read_path(escape)


async def test_the_store_starts_empty_and_makes_its_own_tree(db, raw_root):
    """§8's promise is about bytes M5 fetches, not about a head start the bundle supplies.

    `data/export_bundle/v20260828/` ships BUNDLE.json, artifacts/, content.sqlite and
    reviews.sqlite and no `data/raw/`, so a fresh install reaches `store` with the root not even
    created -- which is the healthy state and must not be a crash. Asserted because the opposite
    arrangement (a directory the installer makes) is what a reader assumes, and the first fetch
    on a new box is the worst place to find out it was assumed.
    """
    assert not raw_root.exists(), "nothing creates the raw root before the first fetch"

    await rawstore.store(db, source="tmdb", kind="detail", url="u", content=b"{}")

    assert len(_files(raw_root)) == 1


def test_the_backend_container_cannot_open_what_this_module_writes():
    """Decision 345, and the custody boundary M4.7 sec-08 drew: `- ./data/raw:/data/raw` is on
    the worker and on nothing else.

    `docker-compose.yml:53-56` states the reason in its own words -- "nothing in the backend reads
    /data/backups or /data/raw at all, and a file that is not in the container cannot be served
    out of it" -- and `test_static_contracts.py` pins the backend's list against the *dumps*. It
    does not pin `/data/raw`, because until this milestone nothing wrote there; this milestone is
    the mount's first user and so it is the one that owes the assertion.

    Asked of the host directory and of the container path, which is the mistake the backups guard
    records in full: `- ./data:/data` is what a simplification of a volume list actually looks
    like and a check for the literal `/data/raw` reads it as absent, while `- ./data/raw:/raw`
    delivers the same directory under a name no container-side filter can see at all. The
    predicate is `_backend_mounts_holding_the_raw_store` above, so that the self-tests below
    exercise it rather than a copy of it.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    worker = _anchor_mounts(compose, "x-worker-volumes")

    assert "./data/raw:/data/raw" in worker, (
        "the worker no longer mounts the raw store, so every byte this module writes dies with "
        "its container"
    )
    holding = _backend_mounts_holding_the_raw_store(compose)
    assert not holding, (
        f"the backend container carries the raw store: {holding}. Decision 345 makes the board "
        "render the raw_document row and never the document, and what enforces that is the file "
        "not being in the process that answers HTTP"
    )


def test_the_custody_guard_sees_the_whole_data_directory_in_one_line():
    """The self-test the guard above is worthless without, and the one shape it exists for.

    `- ./data:/data` is what a simplification of a volume list actually looks like, it puts every
    byte of the raw store inside the HTTP process, and a check for the literal `/data/raw` reads
    it as absent. `test_static_contracts.py` records the same case against the nightly dumps -
    the mistake has been made once in this file already, which is why it is asserted rather than
    assumed.

    The mutation is fed to the GUARD's own predicate. Written first as the `is_relative_to`
    expression re-typed here, this asserted that the mutation had landed and that a rule spelled
    in this function held over it - so narrowing the rule that runs to `container == "/data/raw"`
    left the raw store inside the HTTP container with both tests green, which is the precise
    failure docs/TESTING.md's self-test rule exists to prevent.
    [M5.1 review cycle 1, M51-REV-CUSTODY-05]
    """
    mutated = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace(
        "x-backend-volumes: &backend-volumes\n  - ./data/artifacts:/data/artifacts",
        "x-backend-volumes: &backend-volumes\n  - ./data:/data",
    )
    backend = _anchor_mounts(mutated, "x-backend-volumes")
    assert backend[0] == "./data:/data", "the mutation did not land where this reads the anchor"
    assert _backend_mounts_holding_the_raw_store(mutated) == ["./data:/data"]


@pytest.mark.parametrize(
    ("name", "mount"),
    [
        # The `db` service's own line copied one service up: the same host directory under a
        # container path the filter cannot see. This is not hypothetical - `docker-compose.yml`
        # already carries `- ./data/backups:/backups` on `db`, and that is the line
        # `test_static_contracts.py:994-995` names as the one that defeated the backups guard's
        # first form. It is also the obvious shape of the edit that lets a future admin route
        # "just read the file".
        ("the raw store under a container path of its own", "./data/raw:/raw"),
        ("the raw store delivered beside itself", "./data/raw:/data/rawcopy"),
        # An ancestor of the host directory delivers it too, under any container name at all.
        ("the whole data directory under another name", "./data:/srv"),
        # Read-only is not an exemption here, which is where this rule and the dumps' part
        # company: `_backend_mounts_holding_the_dumps` permits `:ro` because its rule is about a
        # writable copy, and decision 345's is about the file being openable at all.
        ("the raw store read-only", "./data/raw:/raw:ro"),
    ],
)
def test_the_custody_guard_sees_the_raw_store_delivered_under_another_container_path(name, mount):
    """The half of the backups guard's lesson this one did not take at first.

    All four hand the HTTP process a readable copy of every byte this module writes, and none of
    them is caught anywhere else in the tree: the dumps guard is green for the three raw-only
    shapes (their host directory is not `./data/backups`), `_unmounted_spec_volumes` can only see
    a mount that is MISSING, and `ops/m51_exit_criterion.py`'s check 9 runs `ls /data/raw` inside
    the backend, which is the container-side question again.
    [M5.1 review cycle 1, M51-REV-CUSTODY-02]
    """
    compose = _backend_gains(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"), mount
    )
    assert mount in _anchor_mounts(compose, "x-backend-volumes"), (
        "the mutation did not land where this reads the anchor"
    )
    assert _backend_mounts_holding_the_raw_store(compose) == [mount], (
        f"the backend receiving {name} went unnoticed"
    )


CUSTODY_ID = "test_the_backend_container_cannot_open_what_this_module_writes"
REGISTER = REPO_ROOT / "docs" / "spec-v2.2-proposals.md"
COVERAGE = REPO_ROOT / "backend" / "tests" / "spec_coverage.toml"


def _custody_passages() -> dict[str, str]:
    """The two passages that tell a later reader where this boundary is enforced.

    Decision 345 is the normative one -- it is what M5.2-M5.7 read before they touch the board --
    and `rawstore`'s module docstring is the one that decision's own Cost paragraph points them at
    next ("the rawstore module states the boundary where it is enforced"). Sliced rather than
    grepped whole, so a citation somewhere else in either file cannot satisfy this by accident:
    the decision ends where the next owner sitting opens, and the module's claim is its docstring
    rather than any comment further down.
    """
    register = REGISTER.read_text(encoding="utf-8")
    start = register.index("### 345.")
    module = ast.parse(Path(rawstore.__file__).read_text(encoding="utf-8"))
    return {
        "decision 345": register[start : register.index("## Decisions taken", start)],
        "acquire/rawstore.py's module docstring": ast.get_docstring(module) or "",
    }


def test_the_record_names_the_guard_that_actually_pins_this_mount():
    """Decision 345 and this module's docstring both say where the custody boundary is enforced.

    Both said `test_static_contracts.py`, and that file has never pinned `/data/raw`: its only rule
    over the backend's anchor is `_backend_mounts_holding_the_dumps`, whose filter is the host
    directory `./data/backups` and the container path `/data/backups`, and it stays GREEN with
    `- ./data/raw:/data/raw` inserted into `x-backend-volumes`. What pins it is the pair above,
    whose own docstring says so in as many words -- so the chain a later reader follows, decision
    to module to test, dead-ended twice in a file that cannot go red for this.

    That is worth a guard because decision 345 is normative for six milestones and binds one of
    them by name: "an admin action added there may not become a reason to move the mount" is
    addressed to M5.6, and an M5.6 that wanted the document body, read the decision and went to see
    what constrains it would find a guard about database dumps. A record citing the wrong
    enforcement is worse than one citing none, because it is checkable and gets checked.

    The rule is POSITIVE and deliberately not exhaustive. A passage may name
    `test_static_contracts.py` -- it does pin the backend's list against the nightly dumps, and the
    amended sentence says exactly that -- but it must also name the file holding this assertion.
    Which file that is comes off `spec_coverage.toml` rather than off `__file__`, so the guard
    rests on the instrument CLAUDE.md calls the contract rather than on the module it guards
    agreeing with itself. [M5.1 review cycle 3, M51-C3-345-02]
    """
    registered = {
        line.split("::")[0].strip().strip('",')
        for line in COVERAGE.read_text(encoding="utf-8").splitlines()
        if CUSTODY_ID in line
    }
    assert registered == {"backend/tests/test_acquire_rawstore.py"}, (
        f"the coverage map registers {CUSTODY_ID} in {sorted(registered)}; this guard names the "
        "file the map points at, so re-point it here in the same change"
    )
    holder = "test_acquire_rawstore.py"

    for where, passage in _custody_passages().items():
        named = sorted(set(re.findall(r"test_[a-z0-9_]+\.py", passage)))
        assert holder in named, (
            f"{where} says where /data/raw is kept off the backend and credits {named} for it. "
            f"Nothing in those pins this mount: {holder} does, and only since this milestone, "
            "because M5.1 is the mount's first writer and so the milestone that owes the "
            "assertion. Name it, or the decision points the six milestones it binds at a file "
            "that will stay green while the raw store moves into the process that answers HTTP"
        )


# --- review cycle 4: the read path asks the row what the write path asked the trailer ------------


async def test_a_zero_length_file_is_refused_by_the_read_and_not_only_by_the_next_write(
    db, raw_root
):
    """`_is_whole` sits on the write path, and a re-parse is the path that never writes.

    The module argues this crash for itself: `replace` is atomic against the directory entry and
    nothing here issues `fsync`, so a power cut between the rename and the page-cache flush leaves
    the row committed through Postgres's own fsynced WAL and an empty file under a perfectly good
    digest. Its repair is "the next fetch of the same bytes rewrites it" - and §8 (`spec:402`)
    promises that a re-parse issues no fetch at all, which is this milestone's exit-criterion
    check 7. So the guard covered every path except the one the damaged case takes.

    THE ZERO-LENGTH SHAPE IS THE SILENT ONE, which is why it is the shape that mattered: an empty
    gzip stream decompresses to `b""` with no exception, so §8 stage 2's two HTML scrapes would
    parse the empty string as the page and stage 3 would derive from it, while a merely SHORT file
    raises `EOFError` loudly and always did. `byte_size` is what `store` wrote and is already in
    the row, so the question costs one more column on a SELECT `read` already issues and no
    re-hash. [M5.1 review cycle 4, M51-C4-RAW-01]
    """
    body = b'{"detail": "the real bytes"}'
    doc = await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/9", content=body, entity_key="tmdb:9",
    )
    assert await rawstore.read(db, doc) == body
    rel = await db.fetchval("SELECT content_path FROM raw_document WHERE id = $1", doc)
    path = rawstore.resolve(rel)

    path.write_bytes(b"")
    with pytest.raises(OSError) as caught:
        await rawstore.read(db, doc)
    assert "byte" in str(caught.value), str(caught.value)
    # The two readers built on `read` inherit the refusal, and `read_text` is the one that mattered:
    # `read_json` would have raised on `""` by itself, and the HTML scrapes would not.
    with pytest.raises(OSError):
        await rawstore.read_text(db, doc)

    # And a VALID empty gzip member, which is what a crash mid-write can also leave and which no
    # length check on the file itself would catch - 33 bytes on disk, `b""` out of the reader.
    path.write_bytes(gzip.compress(b""))
    assert path.stat().st_size > 0 and rawstore.read_path(rel) == b""
    with pytest.raises(OSError):
        await rawstore.read(db, doc)

    # The control: the store's own repair still works, and a whole file reads clean through the
    # same door - so this is a refusal of damage and not a refusal of the read.
    await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/9", content=body, entity_key="tmdb:9",
    )
    assert await rawstore.read(db, doc) == body


# --- review cycle 4, second pass: the digest names the file, so the file owes the digest ---------


async def test_a_file_the_digest_does_not_name_is_refused_by_the_read(db, raw_root):
    """The module's stated failure modes are "refusing to write and refusing to hand back bytes
    that are not the document the row names". `read` asked a LENGTH question.

    `content_sha256` is on the row, `read` has already decompressed the bytes, and the comparison
    it made was `len(data) != byte_size` - so a file of the right size under the right digest was
    handed to a derive as the document. The two columns are not equally available either:
    `0024_acquisition.sql:133-136` makes `content_sha256 text NOT NULL` and leaves `byte_size`
    nullable, so `read` guarded on the one column the schema permits to be absent and ignored the
    one it guarantees.

    Under decision 162 what a derive writes from those bytes cannot be taken back, which is why
    the check belongs on the path that never re-fetches (`spec:402`, exit-criterion check 7). The
    cost is a sha256 over bytes this function has already decompressed - a fraction of the gunzip
    it just paid and of the round trip above it - so the "the common path must not pay for the
    rare one" argument is still kept. [M5.1 review cycle 4 second pass, M51-C4-RAW-04]
    """
    body = b'{"detail": "the real bytes", "pad": "' + b"x" * 400 + b'"}'
    doc = await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/44", content=body, entity_key="tmdb:44",
    )
    rel = await db.fetchval("SELECT content_path FROM raw_document WHERE id = $1", doc)
    path = rawstore.resolve(rel)
    assert await rawstore.read(db, doc) == body

    # Somebody else's bytes, the same length, in a perfectly valid gzip member: every length
    # question this function could ask answers yes.
    other = b'{"detail": "SOMEBODY ELSES BYTES", "pad": "' + b"y" * 394 + b'"}'
    assert len(other) == len(body), "the substitute must be the same length or this proves nothing"
    path.write_bytes(gzip.compress(other))

    with pytest.raises(OSError) as caught:
        await rawstore.read(db, doc)
    assert "byte" in str(caught.value), str(caught.value)
    assert rel in str(caught.value), "the operator is told which file to remove"

    # The control: the real bytes back under the same digest read clean through the same door, so
    # this is a refusal of a substitution and not a refusal of the store.
    path.write_bytes(gzip.compress(body))
    assert await rawstore.read(db, doc) == body


async def test_a_stored_file_whose_body_is_damaged_is_rewritten_by_the_next_store(db, raw_root):
    """`_is_whole` was named for "is this the WHOLE of the bytes its digest names" and asked gzip's
    ISIZE trailer, which is a claim about the intended length and survives a damaged body intact.

    So a file whose deflate stream is broken while its length and its last four bytes are not
    answered True, `store` skipped the rewrite, and because `_relative_path` derives the
    destination from the digest, NO later fetch of the same bytes could ever repair it: every one
    added another `raw_document` row pointing at the broken file. That is the half
    `test_a_zero_length_file_under_a_good_digest_is_rewritten` does not cover - its shapes are
    empty and short, both of which the trailer catches - and it falsifies `spec:402`'s "re-parsing
    is free forever" for that document for the life of the install, with no in-app repair and a
    `content_path` decision 345 deliberately keeps off section 6.6's board.

    THE ANSWER IS THE QUESTION THE NAME ASKS. The caller is holding `content`, so the
    already-exists path decompresses what is there and compares its digest to the one that named
    the destination. A NEW document's file does not exist, so nothing is decompressed and the
    common path is unchanged; the cost is one gunzip per RE-store of a document already held,
    which by this module's own one-file-two-rows invariant is once per re-fetch - beside an HTTP
    request that has already been paid. [M5.1 review cycle 4 second pass, M51-C4-RAW-03]
    """
    # Barely compressible, so the gzip member is large enough to damage somewhere that is neither
    # the header nor the trailer.
    body = bytes(range(256)) * 400
    doc = await rawstore.store(
        db, source="rt", kind="page", url="https://x/45", content=body, entity_key="rt:45",
    )
    rel = await db.fetchval("SELECT content_path FROM raw_document WHERE id = $1", doc)
    path = rawstore.resolve(rel)
    intact = path.read_bytes()

    damaged = bytearray(intact)
    damaged[len(damaged) // 2:len(damaged) // 2 + 64] = bytes(64)
    path.write_bytes(bytes(damaged))
    assert len(path.read_bytes()) == len(intact), "the length must not change or the trailer would"
    assert path.read_bytes()[-4:] == intact[-4:], "ISIZE is intact, which is all the guard asked"
    # `zlib.error` is not an `OSError`, which is the shape a lost middle page produces and the
    # reason `_holds_the_document` catches both.
    with pytest.raises((OSError, zlib.error)):
        await rawstore.read(db, doc)

    # The repair is the next fetch of the same bytes, which is what `store`'s write-once comment
    # has always promised and what the digest-named destination otherwise makes impossible.
    again = await rawstore.store(
        db, source="rt", kind="page", url="https://x/45", content=body, entity_key="rt:45",
    )
    assert again != doc, "one file, two rows: the second fetch is still its own row"
    assert await rawstore.read(db, doc) == body
    assert await rawstore.read(db, again) == body
    assert len(list(path.parent.iterdir())) == 1, "one file, and no orphan tmp"


def test_two_writers_racing_on_one_digest_choose_two_temporary_names():
    """Named change 4's own reason: "this store is a bind mount, and two processes that raced on
    one digest would otherwise open the same `.tmp` name and interleave their bytes".

    The mitigation was `os.getpid()`, which is not unique for exactly the case it names.
    `docker-compose.yml:141` starts the worker as an exec-form `command` with no init, so each
    worker container's loop is PID 1 in its OWN pid namespace, and `:45` gives every one of them
    the same host directory. `queue.lease`'s docstring calls two loops polling one table "the
    ordinary state" during a rolling restart, and `app.py`'s `_refuse_multiple_workers` guards the
    BACKEND only. So both writers opened the one name, the second `open(..., "wb")` truncated the
    first's inode, and the loser's `replace` - or the winner's `finally: tmp.unlink(missing_ok=
    True)` - took a file the other was still using, out of which an exception escapes `store` and
    `_run_stage` records decision 336's `failed` on a transient race.

    `queue.worker_id` settles which half of the pid pair does what: it builds an owner as
    `hostname:pid` because the pid distinguishes two processes in ONE container and the hostname
    is what separates containers. The temporary name took only the half that does not cross the
    mount. [M5.1 review cycle 4 second pass, M51-C4-RAW-05]
    """
    import inspect

    source = inspect.getsource(rawstore.store)
    assert "getpid" not in source, (
        "the temporary name is a pure function of (destination, pid), and a worker container's "
        "loop is PID 1 in its own namespace: two containers sharing /data/raw choose one name"
    )

    dest = Path("/data/raw/rt/page/aa/bb/" + "a" * 64 + ".html.gz")
    names = {str(rawstore._tmp_name(dest)) for _ in range(200)}
    assert len(names) == 200, "a temporary name is per WRITE, so no two writers can collide"
    for name in names:
        assert name.startswith(str(dest)) and name.endswith(".tmp"), name
