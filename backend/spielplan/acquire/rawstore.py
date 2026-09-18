"""The content-addressed raw store -- §8's "crawl once, re-parse forever". Spec v2.1 §8, §1
(`/data/raw`), §6.6; decisions 162, 181, 345.

Every byte that came off the network is written here once, gzipped, named by the SHA-256 of its
content, and never touched again. `raw_document` rows point at those files, and two fetches that
return identical bytes share one file and get two rows, so the fetch history stays visible
without duplicating bulk. §8 says in one sentence what that buys (`spec:402`): *"All fetched
bytes land in the app's own raw store, so re-parsing is free forever."* The corpus's own module
says the same thing from the other side (`mdc/rawstore.py:1-11`): *"parsers, filters and schemas
are all expected to change, and none of those changes may require another crawl."*

That is the whole argument for keeping raw and derived apart. A derive that read a field wrong is
a bug this app repairs by re-reading bytes it already owns; without the store it is a bug it can
only repair by asking eight third parties for the same pages again, at their pace and with their
permission -- and §8 stage 2 names two of those as deliberately slow HTML scrapes.

**PORT VERDICT: the algorithm verbatim, the storage seam rewritten.** That is the verdict the
milestone brief expected and this module confirms it after reading all 170 lines of
`mdc/rawstore.py`.

The algorithm is the corpus's, unchanged, because every part of it is load-bearing:

  * gzip at compresslevel 6, written under a temporary name and `replace`d into place, so a crash
    leaves either the whole old file or the whole new one and never half of one -- which `replace`
    buys for the DIRECTORY ENTRY and not for the bytes, since this module issues no `fsync`, so
    `_holds_the_document` hashes what is already there before it trusts it (`store` argues it at
    the line, and gzip's trailer, which that check used to ask instead, survives a damaged body);
  * the digest is the SHA-256 of the RAW content and never of the gzip, so the same bytes address
    the same file across a compression-level change or a difference in gzip's header;
  * the layout is the corpus's `<source>/<kind>/<aa>/<bb>/<digest><suffix>.gz` fan-out, kept
    exactly -- two hex digits of the digest and then two more, so no directory grows past what an
    operator can list on the box, which is the only way anyone reads these files (see Custody);
  * `_safe_component` sanitises the directory NAME alone: kinds are namespaced with a colon
    ("reviews:critics"), which is a legal Postgres value and an illegal Windows path component,
    and the row keeps the real kind;
  * write-once. If the file is already there it is not rewritten. The second fetch gets a second
    row and no second file, and that pair -- one file, two rows -- is the invariant, not a
    side effect of it.

The seam is rewritten because it has to be: the corpus is `sqlite3`, synchronous, and reads
`get_config()`; this app is asyncpg, async, and reads `core.config.settings()`. Nothing about
that translation is interesting, and five things about the rest of it are:

1. **`fetched_at` is Postgres's `now()`** -- the column default in `0024_acquisition.sql` --
   rather than the process's `time.time()`. Every other timestamp in this schema is the
   database's, and a store whose clock disagreed with `acquisition_task.next_attempt_at` would
   make "which of these happened first" unanswerable on the one table an operator reads to find
   out why a title is stuck.
2. **`etag` and `last_modified` are accepted and stored.** The corpus keeps its validators in a
   separate `http_cache` keyed on the url's hash; `0024` folded them into this row and argues
   why -- a second table keyed on the same url is a second answer to "what did we last see
   here", which is §14 risk 5's shape. `latest_for_url` below reads the index that fold added and
   answers "what happened at this url last"; the `If-None-Match` read is `fetch._validators`,
   which filters on `ok` and belongs to the layer that builds the request. Two functions, two
   questions -- and this file used to claim `latest_for_url` was also the second one, which is how
   the fold's own argument would have been defeated from inside. [M5.1 review cycle 1, port-05]
3. **A resolved-path containment check the corpus has no equivalent of.** `_safe_component`
   already strips separators out of `source` and `kind`, but `read_path` takes a `content_path`
   read back out of a row, and a row is not a constant. M4.14 paid for the shape of this: the
   admin bundle route held its path to `str(target).startswith(str(data_dir))` under a comment
   claiming that was a boundary, and with the shipped `DATA_DIR=/data` it admitted `/database`
   and `/data.bak` (`api/artifacts.py:118-130`). `resolve` below compares RESOLVED PATHS and
   never prefixes.
4. **The temporary file carries a name unique to the WRITE.** The corpus is one process by
   construction; this store is a bind mount, and two processes that raced on one digest would
   otherwise open the same `.tmp` name and interleave their bytes into a file `replace` then
   publishes as good. The digest names the destination, so that race is exactly as likely as two
   fetches returning identical bytes -- which is the case this module exists to handle rather than
   an exotic one. It carried the writer's PID until review cycle 4's second pass, which is the one
   token that is not unique across the containers sharing this mount: each worker's loop is PID 1
   in its own namespace. See `_tmp_name`.
5. **Two of the corpus's entry points are deliberately not ported.** `StoredDocument` carried a
   `path()` method resolving to a file on disk, and an object that hands its caller a path is an
   invitation to open it in the backend, which has no such file (see Custody); callers here get
   the row and `read()`. `stats()` is §6.6's board read, and it belongs to the milestone that
   builds that card, written against a row shape nobody has decided yet.

**The store starts empty on every install.** `data/export_bundle/v20260828/` ships `BUNDLE.json`,
`artifacts/`, `content.sqlite` and `reviews.sqlite` and no `data/raw/`, so §8's promise is about
bytes this app fetches from M5 onward and never about a head start the corpus supplies. M5.1
re-crawls nothing and imports nothing into this tree. An install that has never run the pipeline
has an empty store, and that is the healthy state rather than a missing import.

**Custody: the worker writes here and the backend cannot read it.** `docker-compose.yml:45` is
`- ./data/raw:/data/raw` under `x-worker-volumes` (`:44`); the backend's own anchor at `:57`
omits it, and the file states the reason in its own words at `:53-56` -- *"nothing in the backend
reads /data/backups or /data/raw at all, and a file that is not in the container cannot be served
out of it. [M4.7 sec-08]"*, which is decision 181's milestone. That list is pinned against the
nightly DUMPS by `test_static_contracts.py` and against `/data/raw` by
`test_acquire_rawstore.py::test_the_backend_container_cannot_open_what_this_module_writes`, which
is this milestone's to write precisely because this module is that mount's FIRST user. This file
must not move the mount; it is not its author.

So §6.6's board -- which the backend serves -- renders the `raw_document` ROW and never the
document: url, http status, sha256, byte size, fetched_at, all of them in Postgres (decision
345). The bytes are reachable by an operator on the box and by nothing that answers an HTTP
request. Stated here as well as in the compose file because this is the module a later reader
reaches for when they want to put a document on a page, and the refusal has to be where the
temptation is.

**Decision 162, in one line.** Nothing in this module writes the content spine. A bad derive is
repaired by re-reading these bytes; a bad write into `title` and its derived tables can only be
undone by dropping the database, because the corpus is no longer somewhere the content can be
fetched from again. That asymmetry is why the expensive thing to protect is the store and not the
parse, and why this file's failure modes are refusing to write and refusing to hand back bytes
that are not the document the row names -- a derive reading a healthy-looking empty document is
exactly the bad derive that asymmetry says must be made loud. [M5.1 review cycle 4, M51-C4-RAW-01]
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


# Ported verbatim from `mdc/rawstore.py`, control characters included. A `kind` is namespaced with
# a colon and a `source` is whatever the adapter calls itself; neither is a path, and the row
# keeps the real value either way. The control range is not defensive decoration -- a component
# carrying a newline makes a directory no operator can name at a shell, which is the one interface
# these files have (see the module docstring's Custody paragraph).
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(s: str) -> str:
    return _UNSAFE.sub("-", s).strip(". ") or "_"


def _relative_path(source: str, kind: str, digest: str, suffix: str) -> str:
    return (f"{_safe_component(source)}/{_safe_component(kind)}/"
            f"{digest[:2]}/{digest[2:4]}/{digest}{suffix}.gz")


def _suffix_for(content_type: str | None) -> str:
    """The corpus's map, unchanged. The suffix is for the operator and never for the parser.

    Nothing reads it back: `read` decompresses whatever the row points at and §8 stage 3 knows
    what it asked for. It exists so that `ls` in a fan-out directory says what is in there, which
    is the difference between a store an operator can audit and forty thousand opaque blobs.
    """
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

    THE NAME OF THIS FUNCTION IS THE QUESTION AND IT USED TO ASK A DIFFERENT ONE. It was
    `_is_whole` and it compared gzip's ISIZE trailer - the uncompressed length mod 2^32 - to the
    length of the bytes in hand. That catches the empty and short shapes a crash between `replace`
    and the page-cache flush leaves, which is what it was written for, and it cannot catch damage
    that preserves the length: a member whose deflate body is broken carries an intact trailer, so
    it answered True, `store` skipped the rewrite, and because `_relative_path` derives the
    destination from the digest, NO later fetch of the same bytes could repair it. Every one added
    another `raw_document` row pointing at a file that raises on every read, which falsifies
    `spec:402`'s "re-parsing is free forever" for that document for the life of the install - and
    the only repair is an operator reconstructing the path from section 6.6's sha256 column,
    because decision 345 keeps `content_path` off the board.

    SO IT ASKS THE DIGEST, which is the one question with no residue: the file's own decompressed
    bytes, hashed, against the name they are filed under. `_is_whole`'s "re-hashing would make
    every store pay for a case this module has never seen" is still honoured - a NEW document's
    file does not exist, so nothing is decompressed - and what is paid is one gunzip per RE-store
    of a document already held, which by this module's one-file-two-rows invariant is once per
    re-fetch, beside an HTTP request that has already cost more. Comparing the gzip CRC32 trailer
    instead would NOT do: like ISIZE it is a claim about the intended content and survives this
    damage intact.

    Every failure answers False and the file is rewritten from the bytes in hand, which is why
    this errs toward rewriting: an `OSError` opening or reading it, a `BadGzipFile` from a failed
    CRC, an `EOFError` from a truncated member, and `zlib.error` from a broken deflate stream -
    which is not an `OSError` and is the shape a lost middle page produces.
    [M5.1 review cycle 2, port-10; M5.1 review cycle 4 second pass, M51-C4-RAW-03; decision 361]
    """
    try:
        with gzip.open(dest, "rb") as fh:
            return sha256_bytes(fh.read()) == digest
    except (OSError, EOFError, zlib.error):
        return False


def _tmp_name(dest: Path) -> Path:
    """A temporary name beside `dest` that no other writer of this digest can have chosen.

    PER WRITE AND NOT PER PROCESS, and named change 4's own sentence is why: "this store is a bind
    mount, and two processes that raced on one digest would otherwise open the same `.tmp` name and
    interleave their bytes into a file `replace` then publishes as good". The mitigation was
    `os.getpid()`, which is not unique for exactly that case - `docker-compose.yml:141` starts the
    worker as an exec-form `command` with no init, so each worker container's loop is PID 1 in its
    OWN pid namespace, and `:45` gives every one of them the same host directory. `queue.lease`
    calls two loops polling one table "the ordinary state" during a rolling restart and
    `app.py`'s `_refuse_multiple_workers` guards the BACKEND only, so nothing refuses the second
    worker. `queue.worker_id` settles which half of the pid pair does what: it builds an owner as
    `hostname:pid` because the pid separates two processes in ONE container and the hostname is
    what separates containers. This name took only the half that does not cross the mount.

    Random rather than `hostname:pid`, because the question here is narrower than the lease's: a
    temporary file needs to be distinguishable from every other writer's, not attributable to one,
    and a name built from the host would still collide with a second write of the same digest in
    the same process. It also makes the `finally: tmp.unlink(missing_ok=True)` below true rather
    than approximately true - it can now only ever remove this writer's own file.
    [M5.1 review cycle 4 second pass, M51-C4-RAW-05]
    """
    return dest.with_name(f"{dest.name}.{os.urandom(8).hex()}.tmp")


def resolve(rel_path: str) -> Path:
    """`rel_path` as a real path under the raw root, or `ValueError`.

    The one boundary a `content_path` is held to, and it is held on the way in as well as on the
    way out: `store` resolves before it writes and `read_path` before it reads, so a row whose
    `content_path` was written by something other than `_relative_path` cannot be turned into a
    read of an arbitrary file on the worker's disk. §8 stage 3 will parse whatever the row names,
    and the rows will eventually be written by eight source adapters (M5.3) rather than by this
    module alone.

    Compared as PATHS and never as prefixes, which is the distinction M4.14 paid for and the
    reason this function exists at all: `str(target).startswith(str(root))` accepts `<data>/rawer`
    for a root of `<data>/raw`, and an admin route shipped with exactly that line under a comment
    claiming it was a boundary (`api/artifacts.py:118-130`). `is_relative_to` asks the question
    the sentence means.
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

    `entity_key` IS THE ACQUISITION TASK'S KEY FOR A PER-TITLE DOCUMENT, and there is no second
    spelling of it. `acquire/board.py` finds a title's documents by joining this column to
    `acquisition_task.key`, so a stage that files under the provider id - the obvious choice, and
    the one `pipeline.key_for_item` itself produces for a provider-keyed task - writes documents
    §6.6's board can never show, and decision 345 makes that board the only window onto those
    bytes. Use `ctx.task.key`. It stays optional because the corpus stores documents that belong
    to no title at all (a library listing, a chart, an award list) and those are correctly keyless.
    [M5.1 review cycle 2, M51-C2-CUSTODY-01]

    `url` IS CO-KEYED WITH `acquire/fetch` THE SAME WAY, and that rule had nowhere to live until
    now. `Fetcher._validators` reads the ETag and Last-Modified filed under the url a request was
    MADE against, `params` are handed to httpx separately, and `Response.url` is the LAST hop's
    url - so an adapter storing the obvious value silently turned conditional re-fetching off for
    that source for ever, and one storing the bare url of a shared endpoint conditioned one
    document's request on another document's ETag. `mdc/http.py` could not make either mistake
    because one function held the read and the write on one variable (`:229`, `:289`); named
    change 1 deleted the write and this module inherited half a pair. Pass
    `response.request_url`, which is that string carried on the answer, and pass `last_modified`
    beside `etag` - two validators are one fact about one document and a store that keeps one of
    them conditions half as often for no reason. [M5.1 review cycle 3, port-C3-01]

    Write-once and content-addressed, which together *are* the two-fetches-one-file-two-rows
    rule: the file is named by the digest, so identical bytes resolve to a path that already
    exists and are not written again, while the INSERT happens either way. Collapsing the second
    fetch into the first row instead would save one row and lose the only record of when this app
    last saw this url -- the question a conditional re-fetch and §6.6's board both ask, and the
    reason `0024` leaves `content_sha256` deliberately not unique.

    NOT A TRANSACTION OF ITS OWN, AND THE DRIVER DOES NOT OPEN ONE EITHER. `pipeline.run_task`
    hands a stage a pooled connection in autocommit (`worker.py`'s `_acquisition_drain` takes
    `pool.acquire()` and not `pool.transaction()`), so every statement a stage issues commits by
    itself. A stage that needs all-or-nothing across several writes opens `conn.transaction()`
    ITSELF; this function stays out of the way so that it can, rather than committing a fetch
    inside a boundary the stage around it owns. The sentence here used to say the caller "is about
    to roll back", which asserted a transaction no stage has and would have told an M5.3 author
    that a rollback was available to them. [M5.1 review cycle 1, M51-CRASH-06]

    What that costs is nothing, and this is the half of the old argument that was true: the file is
    addressed by its content, so bytes whose row never committed are bytes the next fetch of the
    same page simply finds already written.

    A ROW THAT SAYS `ok` SAYS THERE ARE BYTES TO PARSE, and this function is where that is kept.
    `latest_for_url` states the rule - "a 304 is stored `ok = false` and carries the error page's
    validators" - in the docstring of a READ, which is the one place that cannot enforce it: the
    default here is `ok=True`, nothing looked at `http_status`, and `rawstore.latest` filters on
    `ok` alone, so an M5.3 adapter storing the 304 the way the fetcher hands it back - status 304,
    `content = b""` - made a zero-byte document the newest good row for that entity, and every
    later derive read `b""` as the title's page. Silently: a zero-length gzip decompresses without
    raising, which is the same property the zero-length guard below already exists for, and under
    decision 162 the thinned title it writes cannot be rewritten. A `ValueError` and not a
    coercion, because a caller that did not mean it is a caller with a bug to fix.
    [M5.1 review cycle 2, port-304-01]
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
    # WRITE-ONCE TRUSTS THE NAME, AND THE NAME IS A PROMISE ABOUT BYTES NOBODY RE-READS. That is
    # the corpus's rule and it is kept, because hashing a stored file on every store would make
    # the common path pay for the rare one. What it cannot be allowed to trust is a file the crash
    # this module admits to can leave behind: `replace` is atomic against the DIRECTORY ENTRY and
    # this module issues no `fsync`, so a power cut between the rename and the page-cache flush
    # can leave a file that is SHORT under a good digest - and gzip is lenient in the one
    # direction that matters, since an empty stream decompresses to `b""` with no exception at
    # all. A derive would then read a healthy-looking document rather than failing loudly.
    #
    # THE ZERO-LENGTH TEST COVERED ONE OF THE TWO OUTCOMES and the module docstring promised both
    # ("a crash leaves either the whole old file or the whole new one and never half of one"). A
    # non-zero short file was trusted for ever, with no repair path: the digest names the
    # destination, so every later fetch of the same bytes skipped the write and added another row
    # pointing at the broken file.
    #
    # AND THE TRAILER IS NOT THE QUESTION EITHER, which was the same defect one shape further in.
    # `_is_whole` compared gzip's ISIZE to the length in hand, and ISIZE is a claim about the
    # INTENDED content: a member whose deflate body is damaged while its length is not carries an
    # intact trailer, answered True, and inherited the whole of the no-repair-path paragraph above.
    # `_holds_the_document` asks the only question with no residue - the file's own decompressed
    # bytes against the digest that named it - and the caller is already holding both.
    # [M5.1 review cycle 2, port-10; M5.1 review cycle 1, port-09; M5.1 review cycle 4 second
    # pass, M51-C4-RAW-03]
    if not _holds_the_document(dest, digest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The temporary name is unique to this write and never reaches the destination: change 4
        # in the module docstring. `replace` is the atomic half; the unique name is the other. The
        # `finally` is the third, and it covers a RAISE from `gzip.open`, from `fh.write` or from
        # `replace` - a full disk, a permission error - which used to leave one orphan per failed
        # write on a volume with no sweeper, and nothing reads or counts a `.tmp`.
        #
        # IT DOES NOT COVER A KILLED WORKER, and this comment used to say it did. No `finally` runs
        # when a process is killed; that is what the word means, and `queue.reclaim_expired` is
        # this package's own statement that a lease expiry "is the only recovery that survives a
        # `kill -9`". The block below contains no `await` either, so `_tick`'s `asyncio.wait_for`
        # cannot deliver a cancellation into it, and `worker.py`'s SIGTERM and SIGINT handlers only
        # set an event - so nothing short of an exception unwinds this. MEASURED: a child process
        # killed between the first bytes and the rename left one `<digest>.<pid>.tmp` behind. That
        # is admitted rather than swept: the cost is one file per killed write on `/data/raw`,
        # invisible to §6.6's board by decision 345 and reachable only by an operator on the box,
        # and a sweeper is a new behaviour that owes its own argument about which `.tmp` files a
        # concurrent writer still needs. [M5.1 review cycle 4, M51-C4-RAW-02]
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
        # The RAW size, as the corpus records it: what the derive will parse, not what is on disk.
        # A gzipped size would make §8 stage 4's "is this pack thin" a question about compression.
        len(content),
        page, etag, last_modified,
        # `request_meta` is `jsonb NOT NULL DEFAULT '{}'`, and the pool registers a jsonb codec
        # (`db/pool.py:36-42`), so a dict goes in as a dict. `None` would violate the column.
        request_meta if request_meta is not None else {},
        ok, error, run_id,
    )


async def read(conn: asyncpg.Connection, doc_id: int) -> bytes:
    """The re-parse path: the stored bytes off the disk, with no request issued.

    This module imports no HTTP client and must not acquire one. A `read` that could fall back to
    a fetch would make "re-parsing is free forever" a promise about the common case, and the case
    it quietly stopped covering would be the one the store exists for -- the source that has gone
    away, changed its markup, or started refusing us.

    AND THE ROW IS ASKED WHETHER THE FILE IS STILL THE DOCUMENT IT NAMES. `_holds_the_document`
    asks the same question and is consulted only by `store`, whose repair is "the next fetch of
    the same bytes rewrites it" -- and a re-parse is precisely the path that never fetches, which
    is §8's whole promise (`spec:402`) and this milestone's exit-criterion check 7. So the guard
    sat on the one path the damaged case cannot take. The damaged case is the crash `store`'s own
    comment budgets for: the row committed through Postgres's fsynced WAL while this module issues
    none, the file's data blocks lost. A SHORT file raises `EOFError` loudly and a zero-length one
    does not -- an empty gzip stream decompresses to `b""` with no exception -- so §8 stage 2's
    two HTML scrapes would parse the empty string as the page, and nothing would ever re-fetch it.

    THE DIGEST AND NOT THE LENGTH, which is the correction review cycle 4's second pass made to
    this paragraph. It compared `len(data)` to `byte_size`, so a file of the right SIZE under the
    right digest -- somebody else's document, or damage that happened to preserve the length --
    was handed to a derive as this row's bytes, and under decision 162 what the derive then writes
    cannot be taken back. The two columns are not even equally available: `0024` makes
    `content_sha256 text NOT NULL` and leaves `byte_size` nullable, so the guard stood on the one
    column the schema permits to be absent. `content_sha256` is already on the row and the bytes
    are already decompressed, so the question costs one hash over data in hand -- a fraction of the
    gunzip above it and of the round trip above that, which is what "the common path must not pay
    for the rare one" was protecting. The length test goes with it: a digest that matches is a
    length that matches. [M5.1 review cycle 4, M51-C4-RAW-01; M5.1 review cycle 4 second pass,
    M51-C4-RAW-04; decision 361]
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

    IT HOLDS THE ROW, SO IT OWES THE CHECK. This path cannot ask the row anything -- it has no
    connection by design -- and `read` above says why a stored file may disagree with the row that
    names it. A caller reaching here from `latest` already has `content_sha256` in hand and should
    compare `sha256_bytes` of what comes back to it; a caller that only has the path should use
    `read`. [M5.1 review cycle 4, M51-C4-RAW-01; M5.1 review cycle 4 second pass, M51-C4-RAW-04]
    """
    with gzip.open(resolve(rel_path), "rb") as fh:
        return fh.read()


async def read_text(conn: asyncpg.Connection, doc_id: int) -> str:
    """Decoded with `replace`, as the corpus does it.

    A page that is mislabelled `utf-8` is a parse problem and never a read problem: the bytes on
    disk are untouched, so a decode that raised here would make a whole document unreadable over
    one character while the fix -- read it again with the right codec -- is still available.
    """
    return (await read(conn, doc_id)).decode("utf-8", "replace")


async def read_json(conn: asyncpg.Connection, doc_id: int) -> Any:
    return json.loads(await read_text(conn, doc_id))


async def latest(
    conn: asyncpg.Connection, source: str, kind: str, entity_key: str
) -> asyncpg.Record | None:
    """The newest successful document for one entity -- what a per-title derive reads.

    `ok` only, and that filter is ported rather than inherited: a row recording a 404 or a
    truncated body is fetch history, not something to parse, and §8 stage 3 asking for "the tmdb
    detail for this title" means the last one that worked. Reads `raw_document_entity`.

    `, id DESC` for the reason `latest_for_url` states below.
    """
    return await conn.fetchrow(
        """SELECT * FROM raw_document
            WHERE source = $1 AND kind = $2 AND entity_key = $3 AND ok
            ORDER BY fetched_at DESC, id DESC LIMIT 1""",
        source, kind, entity_key,
    )


async def latest_for_url(conn: asyncpg.Connection, url: str) -> asyncpg.Record | None:
    """The last thing this app saw at this url, whatever it was. Fetch history, not a validator.

    The reader of `raw_document_url`, the one index `0024` added that the corpus has no need for
    because it keeps validators in a separate table. No `ok` filter, so a 404 or a truncated body
    is visible here: the question this answers is "what happened at this url last", which is what
    §6.6's board and an operator chasing a stuck title ask.

    IT IS NOT THE CONDITIONAL RE-FETCH'S READ, and saying that it was put two answers to one
    question in one package. `fetch._validators` owns the `If-None-Match` read and filters on `ok`,
    arguing - correctly - that "conditioning a request on one taken from an error page is how a
    store comes to hold a 503 under the name of the document". A 304 is stored `ok = false` and
    carries the error page's validators, so an adapter that built a conditional request from this
    row would keep re-sending a validator for bytes it does not hold while the fetcher, on the same
    url, had already stopped. One url, two code paths, two beliefs about what was last seen there -
    which is the `http_cache`-as-second-table shape `0024`'s fold exists to prevent. The module
    docstring's change 2 is corrected with it. [M5.1 review cycle 1, port-05]

    "A 304 IS STORED `ok = false`" IS NOW A RULE `store` KEEPS rather than a sentence this read
    asserted about a write it does not make. It was neither defaulted nor checked anywhere, so the
    only thing making it true was an adapter remembering - and M5.3 writes eight of them.
    [M5.1 review cycle 2, port-304-01]

    `, id DESC` because `fetched_at` alone does not order these rows: its default is `now()`, which
    is TRANSACTION start time, so rows written for one url inside one stage's transaction tie
    exactly and `LIMIT 1` returns whichever the plan reaches first - the OLDEST, under an index
    scan. The corpus needed no tiebreaker because `mdc/rawstore.py:120` stamped each INSERT from
    `time.time()`; named change 1 replaced that clock and left the query. `board.py:99` already
    carries the tiebreaker over this same table. [M5.1 review cycle 1, port-03]
    """
    return await conn.fetchrow(
        "SELECT * FROM raw_document WHERE url = $1 ORDER BY fetched_at DESC, id DESC LIMIT 1", url
    )
