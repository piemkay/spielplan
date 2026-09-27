"""The raw store's invariants against a real disk and Postgres (§8): one file and two rows per repeated
fetch, no HTTP client, containment, and bytes the backend container cannot open. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import ast
import gzip
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
    """The module takes no root argument on purpose, so
    `DATA_DIR` is set and `settings()` cleared both ways."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _anchor_mounts(compose: str, anchor: str) -> list[str]:
    """Text, not YAML: a parser resolves the aliases, and the question is what the anchor itself carries."""
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
    """Read off the worker's mount, so a renamed host directory cannot leave the guard asserting nothing."""
    hosts = [mount.split(":")[0] for mount in _anchor_mounts(compose, "x-worker-volumes")
             if mount.split(":")[1] == "/data/raw"]
    assert hosts, "no worker mount delivers /data/raw, so the raw store has no host directory"
    return hosts[0]


def _delivers(host: str, root: str) -> bool:
    """`test_static_contracts.py`'s predicate, restated rather than imported."""
    host = host.rstrip("/") or "/"
    return root == host or root.startswith(f"{host}/")


def _backend_mounts_holding_the_raw_store(compose: str) -> list[str]:
    """Both sides of the colon: `./data/raw:/raw` is the same
    directory under another name. No `:ro` exemption."""
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


async def test_two_fetches_of_one_url_leave_one_file_and_two_rows(db, raw_root):
    """`_tmp_name` is the write's only route to disk, so
    making it raise proves the second store did not write."""
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
    """A store that returned early on any existing entity
    would pass the test above and lose the document."""
    url = "https://www.metacritic.com/movie/arrival"
    a = await rawstore.store(db, source="metacritic", kind="page", url=url, content=b"<html>a")
    b = await rawstore.store(db, source="metacritic", kind="page", url=url, content=b"<html>b")

    assert len(_files(raw_root)) == 2
    rows = await db.fetch("SELECT id, content_sha256, content_path FROM raw_document ORDER BY id")
    assert [row["id"] for row in rows] == [a, b]
    assert rows[0]["content_sha256"] != rows[1]["content_sha256"]
    assert rows[0]["content_path"] != rows[1]["content_path"]


async def test_a_read_returns_the_stored_bytes_byte_for_byte(db, raw_root):
    """Not UTF-8, with a NUL and a CRLF: a round trip through `str` would corrupt the HTML scrapes."""
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

    # A missing row is a KeyError, never b"": a derive would write a title with no metadata.
    with pytest.raises(KeyError):
        await rawstore.read(db, 10_000_000)


async def test_the_re_parse_path_issues_no_request(db, raw_root, monkeypatch):
    """Dynamic and static: a lazy re-fetch reached only on a miss would still need an import."""
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

    # A guard that parses nothing is green forever, so it must see these three.
    assert {"gzip", "asyncpg", "spielplan"} <= _module_imports()

    network = {"httpx", "urllib", "urllib3", "requests", "socket", "http", "aiohttp"}
    assert not _module_imports() & network, (
        f"the raw store imports a network client: {sorted(_module_imports() & network)}. A read "
        "that can fall back to a fetch makes 'free forever' a promise about the common case, and "
        "the case it stops covering is the one the store exists for"
    )


async def test_a_zero_length_file_under_a_good_digest_is_rewritten(db, raw_root):
    """No `fsync`: a power cut can leave an empty or short
    file under a good digest; gzip's ISIZE catches both."""
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
    """A 304's empty body stored `ok = true` would be read as the page; the rule lives at the write."""
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

    # The fetch is still recordable: it is fetch history, which is what §6.6's board reads.
    noted = await rawstore.store(
        db, source="tmdb", kind="detail", url=url, content=b"", entity_key="tmdb:329865",
        http_status=304, ok=False, etag='"v1"',
    )
    assert noted is not None
    rows = await db.fetch("SELECT ok, http_status FROM raw_document WHERE url = $1 ORDER BY id", url)
    assert [(row["ok"], row["http_status"]) for row in rows] == [(True, 200), (False, 304)], (
        "a 304 became a good document, and every later derive read b'' as the page"
    )


async def test_a_write_that_fails_leaves_no_temporary_file_behind(db, raw_root, monkeypatch):
    """A raise runs the `finally`; a killed writer does leave its `.tmp`, admitted in `store`'s comment."""
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


async def test_the_row_records_what_the_board_shows(db, raw_root):
    """Decision 345's five fields are on the row; `byte_size`
    is the RAW length, chosen to differ from disk."""
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
    # The suffix is for whoever lists the directory; nothing reads it back.
    assert row["content_path"].startswith("trakt/comments/")
    assert row["content_path"].endswith(".json.gz")


async def test_a_content_path_that_escapes_the_root_is_refused(db, raw_root):
    """`<data>/rawer` shares every character of `<data>/raw`, so paths are compared, not prefixes."""
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
    """A fresh install has no `data/raw/`, which is healthy and must not crash."""
    assert not raw_root.exists(), "nothing creates the raw root before the first fetch"

    await rawstore.store(db, source="tmdb", kind="detail", url="u", content=b"{}")

    assert len(_files(raw_root)) == 1


def test_the_backend_container_cannot_open_what_this_module_writes():
    """Decision 345: `/data/raw` is mounted on the worker only, checked on both sides of the colon."""
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
    """The mutation is fed to the guard's own predicate, not a re-typed copy of it."""
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
        # The `db` service's own `./data/backups:/backups` shape, one service up.
        ("the raw store under a container path of its own", "./data/raw:/raw"),
        ("the raw store delivered beside itself", "./data/raw:/data/rawcopy"),
        # An ancestor of the host directory delivers it too, under any container name.
        ("the whole data directory under another name", "./data:/srv"),
        # Read-only is not an exemption: decision 345 is about the file being openable at all.
        ("the raw store read-only", "./data/raw:/raw:ro"),
    ],
)
def test_the_custody_guard_sees_the_raw_store_delivered_under_another_container_path(name, mount):
    """Each hands the HTTP process a readable copy, and nothing else in the tree catches them."""
    compose = _backend_gains(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"), mount
    )
    assert mount in _anchor_mounts(compose, "x-backend-volumes"), (
        "the mutation did not land where this reads the anchor"
    )
    assert _backend_mounts_holding_the_raw_store(compose) == [mount], (
        f"the backend receiving {name} went unnoticed"
    )


async def test_a_zero_length_file_is_refused_by_the_read_and_not_only_by_the_next_write(
    db, raw_root
):
    """A re-parse never writes, so the read itself must refuse a zero-length file via `byte_size`."""
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
    # `read_text` is the one that mattered: the HTML scrapes would parse "".
    with pytest.raises(OSError):
        await rawstore.read_text(db, doc)

    # A VALID empty gzip member: 33 bytes on disk, b"" out of the reader.
    path.write_bytes(gzip.compress(b""))
    assert path.stat().st_size > 0 and rawstore.read_path(rel) == b""
    with pytest.raises(OSError):
        await rawstore.read(db, doc)

    # The control: the store's own repair still works, and a whole file reads clean.
    await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/9", content=body, entity_key="tmdb:9",
    )
    assert await rawstore.read(db, doc) == body


async def test_a_file_the_digest_does_not_name_is_refused_by_the_read(db, raw_root):
    """`content_sha256` is NOT NULL and `byte_size` nullable, so the read checks the digest."""
    body = b'{"detail": "the real bytes", "pad": "' + b"x" * 400 + b'"}'
    doc = await rawstore.store(
        db, source="tmdb", kind="detail", url="https://x/44", content=body, entity_key="tmdb:44",
    )
    rel = await db.fetchval("SELECT content_path FROM raw_document WHERE id = $1", doc)
    path = rawstore.resolve(rel)
    assert await rawstore.read(db, doc) == body

    # Somebody else's bytes, the same length, in a valid gzip member.
    other = b'{"detail": "SOMEBODY ELSES BYTES", "pad": "' + b"y" * 394 + b'"}'
    assert len(other) == len(body), "the substitute must be the same length or this proves nothing"
    path.write_bytes(gzip.compress(other))

    with pytest.raises(OSError) as caught:
        await rawstore.read(db, doc)
    assert "byte" in str(caught.value), str(caught.value)
    assert rel in str(caught.value), "the operator is told which file to remove"

    # The control: the real bytes back under the same digest read clean.
    path.write_bytes(gzip.compress(body))
    assert await rawstore.read(db, doc) == body


async def test_a_stored_file_whose_body_is_damaged_is_rewritten_by_the_next_store(db, raw_root):
    """ISIZE survives a damaged body, so re-storing a held document compares digests and rewrites it."""
    # Barely compressible, so the member is large enough to damage mid-stream.
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
    # `zlib.error` is not an `OSError`, which is why `_holds_the_document` catches both.
    with pytest.raises((OSError, zlib.error)):
        await rawstore.read(db, doc)

    # The repair is the next fetch of the same bytes.
    again = await rawstore.store(
        db, source="rt", kind="page", url="https://x/45", content=body, entity_key="rt:45",
    )
    assert again != doc, "one file, two rows: the second fetch is still its own row"
    assert await rawstore.read(db, doc) == body
    assert await rawstore.read(db, again) == body
    assert len(list(path.parent.iterdir())) == 1, "one file, and no orphan tmp"


def test_two_writers_racing_on_one_digest_choose_two_temporary_names():
    """Each worker is PID 1 in its own namespace on a shared
    mount, so the temp name needs the hostname too."""
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
