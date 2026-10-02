"""The poster cache under `/data/cache/art`: `{id}.img` plus a `{id}.json` sidecar per title.

A sidecar without bytes is a cached negative answer. An entry answers only a request with the same
sources (`sig`). Droppable: every write is temp file + `os.replace`.
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
    status: str
    sig: str
    expires_at: float
    source: str | None = None
    content_type: str | None = None
    etag: str | None = None


class ArtCache:
    """Blocking I/O: call it off the loop."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self._clock = clock

    def _paths(self, key: int | str) -> tuple[Path, Path]:
        """An id, or a TMDB file name the route has already held to its pattern."""
        name = key if isinstance(key, str) else int(key)
        return self.root / f"{name}.img", self.root / f"{name}.json"

    def read(self, title_id: int, sig: str) -> Entry | None:
        """The entry if it answers these sources and is still fresh, else None; damage is a miss, never an
        error.
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
        """Bytes first, then the sidecar, so an `ok` sidecar always has its bytes."""
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
        """Remember there was nothing to serve for `ttl` seconds; old bytes go with it."""
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
    # The pid so two writers sharing the directory never truncate each other's temp file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


__all__ = ["ERROR", "MISSING", "OK", "ArtCache", "Entry"]
