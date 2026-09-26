"""Per-host crawl policy, as data (§8's politeness clause, decision 340).

Every measured rate is the corpus's, from real crawls. A robots override must carry its reasoning
in `note` (`undocumented_overrides` enforces it). No global robots switch and no editor.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

# A port equal to its scheme's default is the same host; two spellings would be two buckets.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# The rate an unmeasured host gets.
DEFAULT_RPS = 1.0


@dataclass(frozen=True)
class HostPolicy:
    """Rate limit and failure policy for one hostname. Frozen: declarations, not state."""

    rps: float
    burst: int = 2
    max_concurrency: int = 2
    # Consecutive, not cumulative: a flaky host is not a hostile one.
    breaker_threshold: int = 8
    breaker_cooldown_s: float = 300.0
    respect_robots: bool = True
    # Decision 340's "documented with its reasoning"; required when `respect_robots=False`.
    note: str = ""


# Robots.txt governs crawlers; these are documented APIs on their own published terms.
_API_TERMS = (
    "a documented API, not the HTML site robots.txt governs; the API's own published terms and "
    "rate limit apply, and are what the rps beside this note is set under"
)

# The three LLM providers' override: paid calls with the household's key, paced by the provider's
# own rate limit.
_LLM_TERMS = (
    "a paid LLM endpoint reached with the household's own key, not a crawl robots.txt governs: "
    "politeness is not the constraint, the provider's own rate limit is, and these numbers sit "
    "below any tier's limit so a 429 pauses the call rather than failing it; the breaker cooldown "
    "is long because a 429 storm is worth backing off from"
)


# Tuned per host by the corpus, against real crawls. API hosts get real throughput; HTML hosts stay
# slow, and the two §8 stage 2 scrapes slower still.
HOST_POLICIES: dict[str, HostPolicy] = {
    # --- APIs: §8 stage 2's resolve and detail sources ---
    "api.themoviedb.org": HostPolicy(rps=18.0, burst=20, max_concurrency=12,
                                     respect_robots=False, note=_API_TERMS),
    "image.tmdb.org": HostPolicy(rps=10.0, burst=10, max_concurrency=6,
                                 respect_robots=False, note=_API_TERMS),
    # Decision 483's second poster host; a row so a missed robots.txt cannot block TVmaze posters until
    # a restart (the art fetcher lives as long as the web process).
    "static.tvmaze.com": HostPolicy(rps=2.0, burst=2, max_concurrency=2,
                                    respect_robots=False, note=_API_TERMS),
    "www.omdbapi.com": HostPolicy(rps=8.0, burst=8, max_concurrency=4,
                                  respect_robots=False, note=_API_TERMS),
    "api.trakt.tv": HostPolicy(rps=2.5, burst=3, max_concurrency=2,
                               respect_robots=False, note=_API_TERMS),
    "api.tvmaze.com": HostPolicy(rps=2.0, burst=2, max_concurrency=2,
                                 respect_robots=False, note=_API_TERMS),
    "en.wikipedia.org": HostPolicy(
        rps=6.0, burst=8, max_concurrency=4, respect_robots=False,
        note="Wikimedia's robots.txt is aimed at search engines indexing article HTML; the "
             "article fetch uses the action API, which Wikimedia governs by its own User-Agent "
             "policy instead - satisfied by the agent this app declares (decision 340)"),
    "query.wikidata.org": HostPolicy(
        rps=0.5, burst=1, max_concurrency=1, respect_robots=False,
        note="the sanctioned SPARQL endpoint, and half a request a second is what pays for it: "
             "the service is free, shared and expensive to query, so the override buys the "
             "access and the rate is the manners"),
    # Wikimedia disallows /w/ for crawlers, but the action API is the sanctioned programmatic route.
    "www.wikidata.org": HostPolicy(
        rps=1.0, burst=2, max_concurrency=1, respect_robots=False,
        note="robots.txt disallows /w/ to keep crawlers off the script endpoints; the batched "
             "action API (wbgetentities, 50 entities a request) is the sanctioned route to the "
             "same claims, and honouring the blanket rule lost the awards, box-office and "
             "country data entirely in the corpus"),
    "datasets.imdbws.com": HostPolicy(
        rps=1.0, burst=1, max_concurrency=1, respect_robots=False,
        note="a published bulk dataset download - a handful of files fetched whole, not a crawl "
             "of a site"),
    "files.grouplens.org": HostPolicy(
        rps=1.0, burst=1, max_concurrency=1, respect_robots=False,
        note="a published bulk dataset download - a handful of files fetched whole, not a crawl "
             "of a site"),
    # --- LLM APIs: §8 stage 6's three providers ---
    # Set below any tier's limit; the 429 rule does the rest. Long cooldown for 429 storms.
    "api.anthropic.com": HostPolicy(rps=2.0, burst=4, max_concurrency=4, respect_robots=False,
                                    breaker_cooldown_s=120, note=_LLM_TERMS),
    "api.openai.com": HostPolicy(rps=2.0, burst=4, max_concurrency=4, respect_robots=False,
                                 breaker_cooldown_s=120, note=_LLM_TERMS),
    "generativelanguage.googleapis.com": HostPolicy(
        rps=1.5, burst=3, max_concurrency=3, respect_robots=False, breaker_cooldown_s=120,
        note=_LLM_TERMS),
    # --- HTML connectors: deliberately slow ---
    "letterboxd.com": HostPolicy(rps=0.8, burst=1, max_concurrency=1, breaker_cooldown_s=600),
    # The two hosts §8 stage 2 scrapes: the rate the corpus crawled them at without being blocked.
    "www.rottentomatoes.com": HostPolicy(rps=0.7, burst=1, max_concurrency=1,
                                         breaker_cooldown_s=900),
    "www.metacritic.com": HostPolicy(rps=0.7, burst=1, max_concurrency=1,
                                     breaker_cooldown_s=900),
    "www.theyshootpictures.com": HostPolicy(rps=0.5, burst=1, max_concurrency=1),
    # Independent blogs are small sites run by individuals; the WordPress API returns 100 posts
    # per request, so a slow rate is still fast in practice.
    "alternateending.com": HostPolicy(rps=0.5, burst=1, max_concurrency=1),
    "letsgotothemovies.com": HostPolicy(rps=0.5, burst=1, max_concurrency=1),
    "thefilm.blog": HostPolicy(rps=0.5, burst=1, max_concurrency=1),
    # ~28k pages fetched one at a time from a small German site: keep it slow.
    "www.film-rezensionen.de": HostPolicy(rps=0.5, burst=1, max_concurrency=1),
}


# §8: the household's own Jellyfin is exempt, and its stock robots.txt would block the library
# sync. A policy rather than an absence, for any later stage reaching it through this layer.
JELLYFIN_POLICY = HostPolicy(
    rps=8.0, burst=8, max_concurrency=4, respect_robots=False,
    note="the household's own server, reached with the household's own key - not a third party, "
         "and its stock robots.txt would block the owned-library sync outright (spec section 8)",
)


def normalise_host(netloc: str, *, scheme: str = "https") -> str:
    """The one spelling of a host: the key its policy, bucket, breaker and state row are filed by.

    Takes a netloc, not a url. Lowercased, userinfo dropped, default port dropped, trailing dot dropped;
    otherwise one host gets two runtimes and misses its declared row.
    """
    if not netloc:
        return ""
    try:
        parts = urlsplit(f"//{netloc}")
        hostname, port = parts.hostname, parts.port
    except ValueError:
        # A malformed port: not a known host; httpx will refuse the url properly.
        return netloc.strip().lower()
    if not hostname:
        return netloc.strip().lower()
    host = hostname.lower()
    # The DNS root label: `x.com.` is `x.com`; unstripped it would miss its row (and could capture the
    # Jellyfin exemption). `rstrip`, since `x..` is the same host.
    host = host.rstrip(".") or host
    if ":" in host:
        # An IPv6 literal, which `urlsplit` hands back without the brackets the URL form needs.
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get((scheme or "https").lower()):
        return f"{host}:{port}"
    return host


def jellyfin_key(value: str) -> str:
    """The household's own server as a host key, from a URL or from a bare netloc.

    The stored Jellyfin location is a URL, parsed under its own scheme so its default port drops as
    `get` drops it. A bare netloc treats `:80` and `:443` as defaults.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "//" in value:
        parts = urlsplit(value)
        return normalise_host(parts.netloc, scheme=parts.scheme or "https")
    try:
        port = urlsplit(f"//{value}").port
    except ValueError:
        # A malformed port: same answer as `normalise_host`.
        return normalise_host(value)
    return normalise_host(value, scheme="http" if port == 80 else "https")


def policy_for(host: str, *, jellyfin_host: str = "") -> HostPolicy:
    """The policy this host is crawled under. An unknown host gets the slow default.

    Jellyfin matches a whole host, never a suffix, and a declared row always outranks it: an admin's
    typo must not exempt a scraped host from robots and pacing.
    """
    if (
        host and jellyfin_host and host not in HOST_POLICIES
        and normalise_host(host) == jellyfin_key(jellyfin_host)
    ):
        return JELLYFIN_POLICY
    return HOST_POLICIES.get(host, HostPolicy(rps=DEFAULT_RPS))


def declared_policies() -> list[dict[str, object]]:
    """The table as §6.6 will show it: one row per declared host, sorted, with its reasoning.

    The default and the Jellyfin exemption are not declarations and are not listed.
    """
    return [
        {
            "host": host,
            "rps": policy.rps,
            "burst": policy.burst,
            "max_concurrency": policy.max_concurrency,
            "breaker_threshold": policy.breaker_threshold,
            "breaker_cooldown_s": policy.breaker_cooldown_s,
            "respect_robots": policy.respect_robots,
            "note": policy.note,
        }
        for host, policy in sorted(HOST_POLICIES.items())
    ]


def undocumented_overrides() -> list[str]:
    """Hosts whose robots.txt is overridden with no reasoning recorded. Decision 340."""
    return sorted(
        host for host, policy in HOST_POLICIES.items()
        if not policy.respect_robots and not policy.note.strip()
    )
