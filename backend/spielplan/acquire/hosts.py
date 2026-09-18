"""Per-host crawl policy, as data. Spec v2.1 §8 (the politeness clause), decision 340.

§8 now says the fetcher "carries per-host rate and concurrency policies as data rather than as
constants, so §6.6 can show them" and that "an override of a host's robots.txt is permitted only
where it is documented with its reasoning beside the policy it changes" (`spec:404`). This module
is that data, and `declared_policies()` is the read §6.6 will render.

**Port verdict: `mdc/config.py`'s `HostPolicy` and `HOST_POLICIES`, taken WITH NAMED CHANGES.**
Every measured number is the corpus's, unchanged and not re-derived here: an `rps` in this table
is the rate a real crawl of that host survived, and re-guessing it from an armchair would throw
away the only evidence anyone has. The changes are four, each a consequence of this app not being
that CLI:

  1. **The three LLM hosts are dropped** - `api.anthropic.com`, `api.openai.com` and
     `generativelanguage.googleapis.com`. M5.1 adds no provider dependency at all, and those rows
     are not politeness policy: the corpus's own comment says so ("politeness is not the
     constraint - the provider's own rate limit is"). They belong with §8 stage 6's connector
     layer at M5.5, which is also where the spend cap that gates that stage lives. Declaring a
     rate here for a host nothing in the tree can reach would be config for an absent feature.
  2. **`note` is a field rather than a code comment.** The corpus documents its one robots
     override in a comment above the row; decision 340 makes the documented reasoning part of the
     clause, so it has to be readable by the thing that shows the policy. `undocumented_overrides`
     is the read that refuses a `respect_robots=False` row carrying no note, which is the clause
     made falsifiable rather than aspirational.
  3. **`host_policy` moves off the config object** and becomes `policy_for(host, jellyfin_host=)`.
     The corpus reads its own Jellyfin URL out of a process-wide `Config`; this app keeps that URL
     in `connector_config` behind the registry, and a module that reached for it would couple the
     fetcher to the connector layer and to the secrets boundary for one hostname comparison. The
     caller passes the value it already holds - AND THE URL-TO-HOST DERIVATION COMES WITH IT.
     `mdc/config.py:352-354` is a property, `urlparse(self.jellyfin_url).netloc.lower()`, applied
     once so that both sides of `mdc/config.py:363`'s comparison carry one spelling. Dropping the
     config object dropped the property with it, and left a parameter named for a host whose only
     possible argument in this tree is a URL: `connectors/registry.py:85` seeds
     `{"url": cfg.jellyfin_url.rstrip("/")}`, `:225` reads `config ->> 'url'`, and `.env.example`
     documents `JELLYFIN_URL=http://jellyfin.local:8096`. `jellyfin_key` below is that property,
     ported. [M5.1 review cycle 2, port-JF-01, M51-C2-340-04]
  4. **The corpus's global robots kill switch (`MDC_RESPECT_ROBOTS=0`) is not ported.** An env var
     that turns robots off install-wide makes decision 340's clause unfalsifiable: the behaviour a
     reviewer is asked to fail would depend on a variable nothing in the tree asserts. Overrides
     are per host, in this table, with their reasoning next to them, or they do not exist.

**No editor, deliberately.** These are measured constants an operator reads, not state the app
writes; the writable per-host state is `fetch_host_state` (the robots cache and the breaker).
An admin surface that edits rates is M5.7's territory and is probably out of scope entirely
(`docs/milestones/M5.1-plan.md` §4, phase B2).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

# The scheme defaults a host name does not need to carry. A port equal to its scheme's default is
# the same host written twice, and two spellings of one host are two buckets and two breakers.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# The corpus's `MDC_DEFAULT_RPS` default, kept as a constant rather than as config. One request a
# second is the rate a host nobody has measured gets: slow enough that an unlisted host cannot be
# hurt by a stage that reaches it before anyone tuned it, and fast enough that a single-title probe
# still finishes. A host worth crawling faster than this earns a row below with a number behind it.
DEFAULT_RPS = 1.0


@dataclass(frozen=True)
class HostPolicy:
    """Rate limit and failure policy for one hostname.

    Frozen because these are declarations and not state: a policy a caller could mutate would make
    "what policy was this host crawled under" unanswerable after the fact, which is the one
    question an operator asks after a host starts refusing.
    """

    rps: float
    burst: int = 2
    max_concurrency: int = 2
    # Consecutive failures before this host is parked. Consecutive and not cumulative: a host that
    # fails one request an hour is flaky rather than hostile, and parking it for that would be the
    # crawl punishing itself.
    breaker_threshold: int = 8
    breaker_cooldown_s: float = 300.0
    respect_robots: bool = True
    # Decision 340's "documented with its reasoning". Required on any row that sets
    # `respect_robots=False`; optional, and useful, on the rest.
    note: str = ""


# Robots.txt is a crawler protocol and these rows are not crawls: each is a documented programmatic
# interface reached on its own published terms, with its own rate limit, and several of them serve
# a robots.txt written to keep search engines out of the HTML site that shares the hostname.
# Stated once here and cited per row rather than repeated ten times.
_API_TERMS = (
    "a documented API, not the HTML site robots.txt governs; the API's own published terms and "
    "rate limit apply, and are what the rps beside this note is set under"
)


# Tuned per host by the corpus, against real crawls. API hosts get real throughput; HTML hosts stay
# at the "one request per second, politely" level, and the two §8 stage 2 names stay slower still.
HOST_POLICIES: dict[str, HostPolicy] = {
    # --- APIs: §8 stage 2's resolve and detail sources ---
    "api.themoviedb.org": HostPolicy(rps=18.0, burst=20, max_concurrency=12,
                                     respect_robots=False, note=_API_TERMS),
    "image.tmdb.org": HostPolicy(rps=10.0, burst=10, max_concurrency=6,
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
    # The corpus's one documented override, restated with its reasoning rather than copied blind,
    # because decision 340 makes the reasoning the thing that licenses the override. Wikimedia
    # disallows /w/ to keep crawlers off the expensive script endpoints and `wbgetentities` sits
    # under it - so the default robots check parked every `wikidata:entity` task as skipped and the
    # awards, box-office and country claims never arrived at all. The action API is the sanctioned
    # programmatic route to exactly that data, it is batched 50 entities to a request, and this is
    # the same judgement already made for query.wikidata.org above.
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
    # --- HTML connectors: deliberately slow ---
    "letterboxd.com": HostPolicy(rps=0.8, burst=1, max_concurrency=1, breaker_cooldown_s=600),
    # The two hosts §8 stage 2 names by hand - "rt:page" and "metacritic:page->reviews". They are
    # read at seven-tenths of a request a second, one at a time, and a host that starts refusing is
    # left alone for a quarter of an hour rather than the usual five minutes. That is the rate the
    # corpus crawled them at without being blocked, and it is the number decision 340's clause is
    # about: politeness a reviewer can measure rather than a claim in a docstring.
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


# §8 (`spec:404`): "The household's own Jellyfin server is exempt: it is reached with the
# household's own key and is not a third party to be polite to." The corpus makes the same
# exception from the other side and says why it is not optional - Jellyfin ships a restrictive
# robots.txt that would otherwise block the whole owned-library sync. The numbers are a server on
# the household's own LAN, not a stranger's.
#
# It is a policy here rather than an absence of one because §7.1's client does its own HTTP and
# keeps doing it (`connectors/jellyfin.py`, a shipped and hardened file that M5.2 owns): this row
# is what answers correctly if a later stage reaches the household's server through this layer.
JELLYFIN_POLICY = HostPolicy(
    rps=8.0, burst=8, max_concurrency=4, respect_robots=False,
    note="the household's own server, reached with the household's own key - not a third party, "
         "and its stock robots.txt would block the owned-library sync outright (spec section 8)",
)


def normalise_host(netloc: str, *, scheme: str = "https") -> str:
    """The one spelling of a host: the key its policy, bucket, breaker and state row are filed by.

    `urlparse(url).netloc` is NOT that key, and using it as one was a defect in both directions.
    A netloc keeps the port and any userinfo, so `www.rottentomatoes.com:443` and
    `anyuser@www.rottentomatoes.com` miss every row in this table and take `DEFAULT_RPS` - 1.0
    rps, burst 2, a five-minute cooldown - instead of the 0.7/1/900 measured for that host. Worse
    than the rate: they key a SECOND runtime for one physical host, so the breaker whose whole
    purpose is that it "stops every task reaching that host and not merely the one that tripped
    it" (`fetch.py`) stops only the tasks that spelled the host the first way, and
    `fetch_host_state` grows two rows for one host so §6.6's board can show neither number.
    `policy_for` below already compares WHOLE hosts so a lookalike cannot slip through; the key
    the table is looked up by needed the same care. [M5.1 review cycle 1, M51-340-04]

    Lowercased, userinfo dropped, and the port kept ONLY when it is not the scheme's default: a
    household Jellyfin on `192.168.1.10:8096` needs its port in the key, or two services on one
    box would share one exemption.

    TAKES A NETLOC AND NOT A URL, and this paragraph used to name a URL - `https://jelly.example:443`
    - as the value M5.2 would store, while the function returns `'https'` for exactly that string.
    A url reaches the key space through `jellyfin_key` below, which parses it under its own scheme
    and then comes back here. [M5.1 review cycle 2, port-JF-01, M51-C2-340-04]
    """
    if not netloc:
        return ""
    try:
        parts = urlsplit(f"//{netloc}")
        hostname, port = parts.hostname, parts.port
    except ValueError:
        # A malformed port is not a host this table knows; it is also not worth raising over here,
        # because the caller is about to hand the whole url to httpx, which will say so properly.
        return netloc.strip().lower()
    if not hostname:
        return netloc.strip().lower()
    host = hostname.lower()
    # AND THE DNS ROOT LABEL, which is the third spelling of one authority and the one this
    # function did not collapse. `www.rottentomatoes.com.` and `www.rottentomatoes.com` resolve to
    # the same server, so they are the same authority to every rule in this file - but the dotted
    # form missed `HOST_POLICIES`, took `DEFAULT_RPS`, and keyed a SECOND runtime with its own
    # bucket and its own breaker, which is verbatim the harm the port and userinfo rules above
    # were added for. It reaches the key space without an adapter ever spelling it: `get`'s hop
    # block re-keys on the host a `Location` names, and httpx preserves the dot through both the
    # parse and the join. Worse than the rate: `policy_for` guards the Jellyfin exemption with
    # `host not in HOST_POLICIES`, so a spelling the table does not hold is a spelling an admin's
    # configured url can capture - a declared host at 8 rps with robots off, the exemption widened
    # by a VALUE rather than by config. `rstrip` and not a slice, because `x..` is the same host
    # again; `or host` keeps a netloc that is nothing BUT the root label, which is not a host and
    # belongs in no table. An IPv6 literal carries no trailing dot, so this is a no-op before the
    # bracketing below. [M5.1 review cycle 4 second pass, M51-C4-340-08]
    host = host.rstrip(".") or host
    if ":" in host:
        # An IPv6 literal, which `urlsplit` hands back without the brackets the URL form needs.
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get((scheme or "https").lower()):
        return f"{host}:{port}"
    return host


def jellyfin_key(value: str) -> str:
    """The household's own server as a host key, from a URL or from a bare netloc.

    THE ONLY JELLYFIN LOCATION THIS APP STORES IS A URL. `connectors/registry.py:85` seeds
    `{"url": cfg.jellyfin_url.rstrip("/")}`, `:225` reads it back as `config ->> 'url'`, `:344`
    writes `{"url": merged.url}`, and `.env.example` documents `JELLYFIN_URL=http://...`. Handed
    straight to `normalise_host` - which takes a NETLOC and does `urlsplit(f"//{netloc}")` - that
    url yields the literal string `'http'`, and it does so silently: no raise, no log, just a
    §8 clause (`spec:404`, "the household's own Jellyfin server is exempt") that quietly stops
    applying while the household's own server is filed under DEFAULT_RPS with its stock robots.txt
    honoured. The inverse is the same miss read the other way: a host whose key really is `http`
    would collect JELLYFIN_POLICY - the exemption widened by a VALUE rather than by config, which
    is the one thing §8 permits nobody. [M5.1 review cycle 2, port-JF-01, M51-C2-340-04]

    A URL IS PARSED UNDER ITS OWN SCHEME, which is the half that also fixes the asymmetry. `get`
    keys a request with the scheme it was actually made under, so `http://jelly.lan:80/Items` keys
    as `jelly.lan`; a configured value normalised under `normalise_host`'s https default keeps the
    80 and misses. A url carries the scheme that decides which port is redundant, so taking it is
    not a convenience - it is the only way the two sides can agree.

    A BARE NETLOC CARRIES NO SCHEME, so `:80` and `:443` are both read as "the default, written
    out" rather than as a distinguishing port. That is the honest reading of a value with nothing
    to take a default from, and it is what keeps a hand-configured `jelly.lan:80` matching a
    request this fetcher makes over http.
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
        # A malformed port. `normalise_host` says the same thing about it: this table does not
        # know that host, and httpx will refuse the url properly when the caller hands it over.
        return normalise_host(value)
    return normalise_host(value, scheme="http" if port == 80 else "https")


def policy_for(host: str, *, jellyfin_host: str = "") -> HostPolicy:
    """The policy this host is crawled under. An unknown host gets the slow default.

    The Jellyfin test comes first and is a whole-host comparison rather than a suffix match: a
    third-party host that merely ends in the household's domain is still a third party, and the
    exemption turns off both the throttle and the robots check at once.

    The configured side goes through `jellyfin_key` and the request side through `normalise_host`,
    because they arrive in different shapes: the caller's `host` is already the key `get` built
    from a parsed url, and `jellyfin_host` is whatever `connector_config` holds. Normalising both
    sides with one function was the defect, not the fix - it re-normalised an already-normalised
    key under a scheme it was not made with.

    BUT A DECLARED ROW OUTRANKS THE CONNECTOR URL, and that is the one direction the exemption may
    not run. `jellyfin_host` is whatever an admin typed into §6.6's Jellyfin card - `api/admin.py`
    bounds its LENGTH and nothing else, and `registry.save_jellyfin` commits the value before the
    probe that would disagree with it - so with the test unconditionally first, a text field could
    replace any row in this table: point it at one of the two hosts §8 stage 2 scrapes, by a typo
    or by a reverse-proxy name that fronts both, and that host is crawled at 8 rps with its
    robots.txt ignored on the household's IP. Silently, because `declared_policies()` and
    `undocumented_overrides()` take no `jellyfin_host` and would both keep reporting the measured
    numbers - §8's documented override landing on a host it was never written about. A declared row
    is a measurement somebody took against that host; no household runs Jellyfin on one of these
    names, so the guard costs the exemption nothing and it still wins everywhere else.
    [M5.1 review cycle 3, M51-C3-340-03]
    """
    if (
        host and jellyfin_host and host not in HOST_POLICIES
        and normalise_host(host) == jellyfin_key(jellyfin_host)
    ):
        return JELLYFIN_POLICY
    return HOST_POLICIES.get(host, HostPolicy(rps=DEFAULT_RPS))


def declared_policies() -> list[dict[str, object]]:
    """The table as §6.6 will show it: one row per declared host, sorted, with its reasoning.

    Sorted by host rather than by rate, so the list an operator reads twice is the same list both
    times. `policy_for`'s default and the Jellyfin exemption are deliberately NOT in here: neither
    is a declaration about a named host, and inventing rows for them would make the board claim
    policies for hosts nobody configured.
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
    """Hosts whose robots.txt is overridden with no reasoning recorded. Decision 340.

    §8 permits an override "only where it is documented with its reasoning beside the policy it
    changes". A permission with no enforcement is a comment, so this is the read the guard makes:
    the answer is the empty list, and a row added later without a note is what makes it not be.
    """
    return sorted(
        host for host, policy in HOST_POLICIES.items()
        if not policy.respect_robots and not policy.note.strip()
    )
