"""The poster cache under `/data/cache/art`. Spec v2.1 §1 (`/data/cache` is a volume both services
mount), §6.8; decisions 483 and 484.

One file of bytes and one JSON sidecar per title, both named by the title id: `{id}.img` and
`{id}.json`. The sidecar is the whole of what the route knows about an answer it gave before -
where the bytes came from, what they are, when they stop being trusted - so a sidecar with no
bytes beside it is an answer too: "this title has no image anywhere I may look" (`missing`) or
"the host did not answer" (`error`), each kept for its own length of time so the next view of a
posterless card costs a disk read rather than an upstream request.

THE SIDECAR NAMES THE SOURCES IT WAS ANSWERED FROM (`sig`), and an entry answers only a request
whose sources are the same. The URL names a title and not a file, so a title that became owned
(a Jellyfin id), was re-derived (a new `poster_path`) or had its lookup land (decision 484) is a
different question with the same key; comparing the list of sources is what makes it a miss
rather than 180 days of the old answer.

Droppable by construction: nothing here is the only copy of anything, and `rm -r
/data/cache/art` costs one upstream fetch per title on its next view. Every write is a temp file
and an `os.replace`, so a reader never sees half an image and a crash leaves at worst a stray
`.tmp` beside a whole entry.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
MISSING = "missing"
ERROR = "error"


@dataclass(frozen=True)
class Entry:
    """One title's cached answer, as its sidecar records it."""

    status: str
    sig: str
    expires_at: float
    source: str | None = None
    content_type: str | None = None
    etag: str | None = None


class ArtCache:
    """The directory, read and written a title at a time. Blocking I/O: call it off the loop."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self._clock = clock

    def _paths(self, title_id: int) -> tuple[Path, Path]:
        return self.root / f"{int(title_id)}.img", self.root / f"{int(title_id)}.json"

    def read(self, title_id: int, sig: str) -> Entry | None:
        """The entry for `title_id` if it answers these sources and is still fresh, else None.

        A sidecar that will not parse, names other sources, has expired, or claims bytes that are
        not on disk is a miss and never an error: the cache is droppable, so the worst any damage
        to it can cost is a fetch.
        """
        image, meta = self._paths(title_id)
        try:
            raw = json.loads(meta.read_text(encoding="utf-8"))
            entry = Entry(
                status=str(raw["status"]),
                sig=str(raw["sig"]),
                expires_at=float(raw["expires_at"]),
                source=raw.get("source"),
                content_type=raw.get("content_type"),
                etag=raw.get("etag"),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if entry.sig != sig or entry.expires_at <= self._clock():
            return None
        if entry.status == OK and not image.is_file():
            return None
        return entry

    def bytes_of(self, title_id: int) -> bytes:
        return self._paths(title_id)[0].read_bytes()

    def store(
        self, title_id: int, sig: str, data: bytes, *, content_type: str, source: str, ttl: float
    ) -> Entry:
        """Write the bytes, then the sidecar that vouches for them - in that order, so a reader
        that finds an `ok` sidecar always finds the bytes it describes."""
        image, meta = self._paths(title_id)
        self.root.mkdir(parents=True, exist_ok=True)
        _replace(image, data)
        entry = Entry(
            status=OK,
            sig=sig,
            expires_at=self._clock() + ttl,
            source=source,
            content_type=content_type,
            etag='"' + hashlib.sha256(data).hexdigest()[:32] + '"',
        )
        _replace(meta, _sidecar(entry))
        return entry

    def store_negative(self, title_id: int, sig: str, *, status: str, ttl: float) -> Entry:
        """Remember that there was nothing to serve, for `ttl` seconds. Old bytes are removed
        with it: a sidecar saying `missing` beside a stale image would be two answers."""
        image, meta = self._paths(title_id)
        self.root.mkdir(parents=True, exist_ok=True)
        entry = Entry(status=status, sig=sig, expires_at=self._clock() + ttl)
        _replace(meta, _sidecar(entry))
        with contextlib.suppress(FileNotFoundError):
            image.unlink()
        return entry


def _sidecar(entry: Entry) -> bytes:
    return json.dumps(
        {
            "status": entry.status,
            "sig": entry.sig,
            "expires_at": entry.expires_at,
            "source": entry.source,
            "content_type": entry.content_type,
            "etag": entry.etag,
        },
        sort_keys=True,
    ).encode("utf-8")


def _replace(path: Path, data: bytes) -> None:
    # The pid in the temp name because the web process and a test can share a directory, and two
    # writers of one title must not truncate each other's half-written file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


__all__ = ["ERROR", "MISSING", "OK", "ArtCache", "Entry"]
