"""The image hosts this app may fetch cover art from, and TMDB's size segment. Decision 483.

An `<img src>` in this app never points at a third-party host: the art route fetches upstream and
serves the bytes from its own origin, so a phone's IP and Referer never reach the host and the
route can be session-gated. Two hosts are servable - TMDB's image CDN and TVmaze's static store -
and IMDb's (`m.media-amazon.com`) is not. `https` only and the host matched exactly: a scheme, a
port, a userinfo or a subdomain the tuple does not name is a different origin and is refused
rather than normalised into one it resembles.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

SERVABLE_HOSTS = ("image.tmdb.org", "static.tvmaze.com")

# TMDB serves one file at several widths under `/t/p/<size>/<file>`. The size tokens it documents
# are `w<n>`, `h<n>` and `original`; anything else would build a path TMDB answers with 404.
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
    # `netloc == hostname` refuses a port and a userinfo in one comparison; `hostname` is already
    # lower-cased, so a netloc spelled in capitals is refused too rather than silently folded.
    return (
        parts.scheme == "https"
        and parts.hostname in SERVABLE_HOSTS
        and parts.netloc == parts.hostname
    )


def tmdb_size(url: str, size: str = "w342") -> str:
    """`url` with TMDB's `/t/p/<size>/` segment rewritten to `size`; any other URL unchanged.

    A rewrite of the segment that is there and never a prefix: a URL already at `size` comes back
    identical, and a URL with no `/t/p/<size>/` segment - a bare `/abc.jpg`, a TVmaze URL - is not
    turned into `/t/p/w342/t/p/w500/abc.jpg` or given a host it did not name.
    """
    if not _TMDB_SIZE.fullmatch(size):
        raise ValueError(f"not a TMDB image size: {size!r}")
    if not servable(url):
        return url
    parts = urlsplit(url)
    if parts.hostname != "image.tmdb.org" or not _TMDB_PATH.match(parts.path):
        return url
    return urlunsplit(parts._replace(path=_TMDB_PATH.sub(f"/t/p/{size}/", parts.path, count=1)))


__all__ = ["SERVABLE_HOSTS", "servable", "tmdb_size"]
