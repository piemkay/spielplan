"""The content-addressed raw store: §8's "crawl once, re-parse forever" (decisions 162, 345).

Gzipped once under the SHA-256 of the raw content, write-once; two identical fetches share one file
and get two rows. `/data/raw` is mounted on the worker only, so the backend shows rows, never bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import zlib
from pathlib import Path
from typing import Any

import asyncpg

from spielplan.core.config import settings


def sha256_bytes(data: bytes) -> str:
    """The digest that names the file: SHA-256 of the RAW content, never of the gzip."""
    return hashlib.sha256(data).hexdigest()


# Directory names only (kinds contain colons); control characters would make unlistable dirs.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(s: str) -> str:
    return _UNSAFE.sub("-", s).strip(". ") or "_"


def _relative_path(source: str, kind: str, digest: str, suffix: str) -> str:
    return (f"{_safe_component(source)}/{_safe_component(kind)}/"
            f"{digest[:2]}/{digest[2:4]}/{digest}{suffix}.gz")


def _suffix_for(content_type: str | None) -> str:
    """The corpus's map, unchanged. The suffix is for the operator and never for the parser."""
    if not content_type:
        return ".bin"
    ct = content_type.split(";")[0].strip().lower()
    return {
        "application/json": ".json",
        "application/ld+json": ".json",
        "text/json": ".json",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "text/plain": ".txt",
        "text/tab-separated-values": ".tsv",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/csv": ".csv",
        "application/sparql-results+json": ".json",
    }.get(ct, ".bin")


def _holds_the_document(dest: Path, digest: str) -> bool:
    """Is the file already at `dest` the document `digest` names?

    Hashes the decompressed bytes: gzip's length and CRC trailers survive a damaged body. Any failure
    answers False, so the file is rewritten from the bytes in hand. Paid only on a re-store.
    """
    try:
        with gzip.open(dest, "rb") as fh:
            return sha256_bytes(fh.read()) == digest
    except (OSError, EOFError, zlib.error):
        return False


def _tmp_name(dest: Path) -> Path:
    """A temporary name beside `dest` that no other writer of this digest can have chosen.

    Random, per write: every worker container's loop is PID 1, and they share this mount.
    """
    return dest.with_name(f"{dest.name}.{os.urandom(8).hex()}.tmp")


def resolve(rel_path: str) -> Path:
    """`rel_path` as a real path under the raw root, or `ValueError`.

    Compared as resolved paths, never as string prefixes (`/data/rawer` is not under `/data/raw`).
    """
    root = settings().raw_dir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"raw store path escapes {root}: {rel_path!r}")
    return target


async def store(
    conn: asyncpg.Connection,
    *,
    source: str,
    kind: str,
    url: str,
    content: bytes,
    entity_key: str | None = None,
    http_status: int | None = 200,
    content_type: str | None = "application/json",
    page: int = 0,
    request_meta: dict[str, Any] | None = None,
    ok: bool = True,
    error: str | None = None,
    run_id: int | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> int:
    """Write `content` into the store and record it. Returns the new `raw_document` id.

    `entity_key` must be the acquisition task's key and `url` the fetcher's `request_url` (with
    `last_modified` beside `etag`), or the board and conditional re-fetching break silently. No
    transaction of its own. An `ok` row must carry bytes: a 304 or empty `ok` document is refused.
    """
    if ok and http_status == 304:
        raise ValueError(
            "a 304 carries no bytes, so it cannot be stored as a good document: the bytes this "
            "app already holds are still current and the derive re-reads them. Store it with "
            "ok=False if the fetch itself is worth recording."
        )
    if ok and not content:
        raise ValueError(
            f"refusing to store an empty document for {url!r} as good: an empty gzip stream "
            "decompresses to b'' with no error, so a derive would read it as the document"
        )
    digest = sha256_bytes(content)
    rel = _relative_path(source, kind, digest, _suffix_for(content_type))
    dest = resolve(rel)
    # Write-once trusts the name, but a crash without fsync can leave a damaged file under a good digest,
    # so check the digest of what is there before skipping the write.
    if not _holds_the_document(dest, digest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        # A per-write temp name, `replace` into place, and `finally` removes the temp on any raise. A
        # killed
        # worker still leaves one orphan `.tmp`; admitted, not swept.
        tmp = _tmp_name(dest)
        try:
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                fh.write(content)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
    return await conn.fetchval(
        """INSERT INTO raw_document
             (source, kind, entity_key, url, http_status, content_sha256, content_path,
              content_type, byte_size, page, etag, last_modified, request_meta, ok, error, run_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
           RETURNING id""",
        source, kind, entity_key, url, http_status, digest, rel, content_type,
        # The RAW size: what the derive will parse, not what is on disk.
        len(content),
        page, etag, last_modified,
        # `jsonb NOT NULL DEFAULT '{}'`; the pool's codec takes a dict. `None` would violate the column.
        request_meta if request_meta is not None else {},
        ok, error, run_id,
    )


async def read(conn: asyncpg.Connection, doc_id: int) -> bytes:
    """The re-parse path: the stored bytes off the disk, with no request issued.

    Never falls back to a fetch. Checks the bytes against the row's `content_sha256`, since a re-parse
    is the one path that never repairs a damaged file.
    """
    row = await conn.fetchrow(
        "SELECT content_path, content_sha256 FROM raw_document WHERE id = $1", doc_id
    )
    if row is None or row["content_path"] is None:
        raise KeyError(f"raw_document {doc_id} not found")
    data = read_path(row["content_path"])
    digest = sha256_bytes(data)
    if digest != row["content_sha256"]:
        raise OSError(
            f"raw_document {doc_id} decompressed to {len(data)} bytes whose sha256 is {digest} "
            f"and not the {row['content_sha256']} this row names: the file under "
            f"{row['content_path']} is not this row's document, and a re-parse never re-fetches "
            "it (spec section 8)"
        )
    return data


def read_path(rel_path: str) -> bytes:
    """The same read, for a caller that already holds the row. Takes no connection on purpose.

    The caller owes the digest check `read` makes.
    """
    with gzip.open(resolve(rel_path), "rb") as fh:
        return fh.read()


async def read_text(conn: asyncpg.Connection, doc_id: int) -> str:
    """Decoded with `replace`, as the corpus does it: a mislabelled page is a parse problem, not a read
    one.
    """
    return (await read(conn, doc_id)).decode("utf-8", "replace")


async def read_json(conn: asyncpg.Connection, doc_id: int) -> Any:
    return json.loads(await read_text(conn, doc_id))
