"""Image hosts cover art may be fetched from, and TMDB's size segment (decision 483).

https only, host matched exactly; IMDb's host is deliberately not servable.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

SERVABLE_HOSTS = ("image.tmdb.org", "static.tvmaze.com")

# TMDB's documented size tokens; any other segment 404s.
_TMDB_SIZE = re.compile(r"(?:w|h)\d+|original")
_TMDB_PATH = re.compile(r"^/t/p/[^/]+/")


def servable(url: str | None) -> bool:
    """True when `url` is an https URL on one of `SERVABLE_HOSTS`, matched exactly."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    # `netloc == hostname` also refuses a port, a userinfo and a capitalised host.
    return (
        parts.scheme == "https"
        and parts.hostname in SERVABLE_HOSTS
        and parts.netloc == parts.hostname
    )


def tmdb_size(url: str, size: str = "w342") -> str:
    """`url` with TMDB's `/t/p/<size>/` segment rewritten to `size`; any other URL unchanged."""
    if not _TMDB_SIZE.fullmatch(size):
        raise ValueError(f"not a TMDB image size: {size!r}")
    if not servable(url):
        return url
    parts = urlsplit(url)
    if parts.hostname != "image.tmdb.org" or not _TMDB_PATH.match(parts.path):
        return url
    return urlunsplit(parts._replace(path=_TMDB_PATH.sub(f"/t/p/{size}/", parts.path, count=1)))


__all__ = ["SERVABLE_HOSTS", "servable", "tmdb_size"]
