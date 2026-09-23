"""One fetch, filed under the two keys M5.1's review cycles paid for. Every adapter goes here.

Spec v2.1 §8 preamble ("All fetched bytes land in the app's own raw store, so re-parsing is free
forever", `spec:398`) and §8's politeness clause (`spec:404`); decisions 334, 340, 345, 372, 373.

PORT VERDICT: **ported with named changes** from `mdc/sources/_htmlutil.py` - `MIN_HTML_BYTES`
(`:25`), `View` (`:29-35`) and `fetch_views` (`:38-97`). `sources/_htmlutil.py`'s own docstring
says where they belong and this is it: "they take a `Ctx`, call `ctx.fetcher.get` and write into
the raw store - and whatever ports them ports them beside the adapters".

WHY ALL EIGHT ADAPTERS GO THROUGH IT AND NOT ONLY THE TWO SCRAPED ONES. `rawstore.store` co-keys
two ways, and its docstring names six of these eight adapters as the exact shape to get both
wrong - one shared endpoint per source with the query as the only thing distinguishing one title
from another. So the co-keying is written ONCE, here:

  * `entity_key=ctx.task.key` and nothing else. `acquire/board.py:93` finds a title's documents
    by joining that column to `acquisition_task.key`, and decision 345 makes that board the only
    window onto the bytes - so a document filed under a provider id is a document §6.6 can never
    show. There is no `entity_key` parameter on either function below, which is the strongest
    form the rule can take: an adapter cannot pass the wrong one.
  * `url=response.request_url`, which is the url the request was MADE against, query included.
    `Response.url` is the LAST hop of a redirect chain, and `Fetcher._validators` reads the
    validators filed under the made-against string - so storing the obvious value turns
    conditional re-fetching off for that source for ever, with no error and no log.

AND THE VALIDATORS TRAVEL IN PAIRS. `etag` and `last_modified` are one fact about one document;
`rawstore.store` takes both and a store that kept one of them conditions half as often for no
reason at all.

CONDITIONAL BY DEFAULT, WHICH IS WHAT THE PAIR ABOVE IS FOR. A stage 2 that is re-run - an
operator retrying a parked job, a second acquisition of a title already in the library - asks the
host whether the bytes it holds are still current instead of asking for them again. This is the
milestone that crawls the open web on a household's own IP address, so the request that is not
made is the cheapest politeness available. A 304 comes back as `Captured(unchanged=True)` with no
document: the caller records that nothing moved and does NOT re-read the identity it wrote last
time off a body it no longer has.

FAILURES ARE STORED WHERE THE CORPUS STORES THEM, and for the corpus's reason: a 404, a refused
slug or a robots refusal is fetch history, and an operator chasing a title stuck at stage 2 reads
it off §6.6's board. `rawstore.latest` filters on `ok`, so a failure row never reaches a parser.
Only a NON-retryable failure is stored, which is `mdc/sources/_htmlutil.py:64`'s own rule: a
timeout or a 503 will be asked again on the next drain, and a row per attempt would turn one
flaky host into a board nobody can read.

A `HostPaused` IS THIS SOURCE'S NOTE, WHICH IS DECISION 422 AND WAS THE OPPOSITE SENTENCE HERE
UNTIL IT WAS MEASURED. This paragraph used to say that the breaker "is a fact about the host
rather than about this title, and decision 336 makes it a park with a deadline that the stage
driver takes - not a note under a source's name". The driver never took that park: `stages.enrich`
catches everything an adapter raises as that source's note and advances, so the re-raise produced
a note either way - one spelled as a class name and an exception message, and one that abandoned
`fetch_views` at its first paused view so the second view was never recorded at all. Decision 422
settles which of the two sentences the build keeps, and it is this one. The required source is
unaffected: stage 2's park is about `tmdb:detail` failing to answer and not about which exception
said so, so a paused api.themoviedb.org still parks with a deadline. What the pause costs a
best-effort source is written out at `stages.enrich`, because that is where the advance happens.
(`mdc/sources/_htmlutil.py:44-46` re-raises, and it can: the corpus's driver is a crawl loop with
no per-title board to write the note on.)

ONE EXPOSURE THIS MODULE CREATES AND CANNOT CLOSE FROM HERE. TMDB's v3 key and OMDb's key are
query parameters - neither API accepts them any other way, and `credentials.tmdb_auth` returns
the v3 arm by decision 377 - so `Response.request_url` carries them, and that string is what the
co-keying rule above requires be stored. `raw_document.url` is therefore plaintext-credential
bearing for those two sources, and `acquire/board.py:221` renders it to an admin. The exposure is
bounded: the reader is the administrator who typed the key, `backup/movie_data.py:97` does not
archive this table, and redacting the stored string would key `_validators` on a url no request
is ever made against. It is recorded here rather than left to be discovered, because the
milestone that adds a v4 bearer token closes half of it for free.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from spielplan.acquire import fetch, rawstore

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

# Any real HTML page from the two hosts §8 stage 2 scrapes is tens of kilobytes; anything under
# this is a challenge page, a stub or an empty bot-block response (`mdc/sources/_htmlutil.py:23-25`,
# verbatim). Recording one as good would hide the block from the board, let the derive parse
# nothing, and stop the host's breaker ever tripping - so a crawl would spend its whole budget on
# a wall. Applied only where a caller asks for it: a JSON API answering `{}` is a short body and a
# perfectly good document.
MIN_HTML_BYTES = 512

# What `rawstore.store` defaults to, restated so that the HTML callers below can override it
# without either value being implicit. The store writes this into `raw_document.content_type` and
# picks the file suffix off it.
JSON = "application/json"
HTML = "text/html"


def json_body(response: fetch.Response) -> str:
    """A body that does not parse as JSON, said once for every caller that asked for JSON.

    `rawstore.store`'s own docstring states the invariant this keeps: "A ROW THAT SAYS `ok` SAYS
    THERE ARE BYTES TO PARSE, and this function is where that is kept." A captive portal, a
    transparent proxy, an ISP hijack page or a Cloudflare interstitial is the likeliest non-fixture
    answer on a home network AND the only one that arrives as a 200 - so without this, every JSON
    source on the box files an interception page `ok = true`, with the portal's ETag, as the newest
    document for its `(source, kind, page)`. What follows is not merely a wrong note on §6.6's
    board: `rawstore.latest` and `derive/rebuild._DOCUMENTS` both filter on `ok`, so the junk row
    DISPLACES the last good document, its label still enters decision 375's delete scope, and a
    re-derive drops that source's rows and re-inserts nothing.

    APPLIED BY `capture` RATHER THAN PASSED BY EACH ADAPTER, for the reason
    `test_sources_adapters.py` already gives about the empty-body floor - it "had to be
    unconditional rather than opt-in". `omdb._refusal` and `wikipedia._missing` each carried this
    decode defensively and the other six call sites did not, including the one decision 334 makes
    required.

    IT COSTS A SECOND `json.loads` OF EVERY GOOD BODY, recorded rather than optimised away: the
    adapter decodes again to read its own answer, so a TMDB `append_to_response` payload is parsed
    twice per fetch. Handing the decoded value back would mean `Captured` carrying it and all eight
    adapters reading it from there, which is a change to this module's contract rather than to a
    verdict - and the parse is microseconds against a request paced in seconds.
    [M5.3 review cycle 1, M53-C1-NET-02]
    """
    return _decode(response)[1]


def json_object(response: fetch.Response) -> str:
    """`json_body`, and the value has to be a JSON object.

    The default, because every adapter but one reads the answer with `.get(...)`: a top-level
    array reaching those lines is an `AttributeError` out of a stage-2 adapter, which decision 334
    says seven of the eight sources may never raise. Trakt's comment pages ARE an array and pass
    `json_body` instead, which is the whole of the exception.
    """
    data, note = _decode(response)
    if note:
        return note
    if not isinstance(data, dict):
        return f"answered HTTP {response.status} with JSON that is not an object"
    return ""


def _decode(response: fetch.Response) -> tuple[Any, str]:
    """The body as JSON, or the sentence saying it is not one. One parse, shared by both verdicts."""
    try:
        return json.loads(response.text), ""
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, f"answered HTTP {response.status} with a body that is not JSON"


@dataclass(frozen=True)
class View:
    """One url to try for a title, tagged with what it is meant to contain.

    `mdc/sources/_htmlutil.py:29-35`, frozen. `name` becomes the suffix of the stored document's
    `kind`, which is how `metacritic:reviews` keeps its critic page and its user page apart in a
    table whose newest-per-(entity, kind, page) view would otherwise collapse them.
    """

    name: str
    url: str
    page: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Captured:
    """What one request did: the row it landed in, the answer, and why there is no row.

    Frozen for `SourceResult`'s reason one layer up - the adapter builds its note from this and
    then decides its own verb from the same object, and two readers of one mutable record is how
    a note shown to an operator stops being the note the adapter acted on.

    `error` empty means the bytes are stored and parsable. `unchanged` means the host answered
    304: there is no document because the one this app already holds is still the document.
    """

    doc_id: int | None = None
    response: fetch.Response | None = None
    error: str = ""
    unchanged: bool = False
    # The `View.name` this came from, so `fetch_views` can report which of two views failed
    # without its caller having to pair two lists up by index.
    name: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def content(self) -> bytes:
        """The bytes, or empty. A caller that needs them has already checked `ok`."""
        return self.response.content if self.response is not None else b""


async def capture(
    ctx: StageContext,
    *,
    source: str,
    kind: str,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    page: int = 0,
    request_meta: dict[str, Any] | None = None,
    content_type: str = JSON,
    min_bytes: int = 0,
    verdict: Callable[[fetch.Response], str] | None = None,
    name: str = "",
) -> Captured:
    """Fetch one url politely and file the answer. The only way bytes enter this app at stage 2.

    Every failure of the request comes back as a `Captured` whose `error` is set, the circuit
    breaker included (decision 422), because decision 334 makes "this source did not answer and
    stage 2 advances anyway" the ordinary outcome of seven of the eight sources - and
    `acquire/stages.py:20-29` records what happens to an ordinary outcome raised as an exception:
    a later `except Exception` swallows it into a failure, which is what the breaker's re-raise
    was doing.

    `verdict` IS FOR A PROVIDER THAT REFUSES INSIDE A 200, which OMDb does - `{"Response":
    "False", "Error": "Movie not found!"}` arrives as a perfectly healthy HTTP answer
    (`mdc/sources/omdb.py:40-52`). It returns an error string or `""`, and it decides only the
    `ok` FLAG: the bytes are stored either way, because they are the honest record of what the
    host said and `rawstore.latest` filters on `ok` so they never reach a parser. That is the
    same shape `min_bytes` already has one line down, and it is why "every response goes into the
    raw store before anything is parsed" survives a source that says no in JSON - a verdict reads
    one field to judge the document and writes no derived row.
    """
    try:
        resp = await ctx.fetcher.get(url, params=params, headers=headers, conditional=True)
    except fetch.HostPaused as exc:
        # NO RAW ROW, which is the retryable arm's rule below and is why this arm is separate from
        # it rather than folded in: no request was made, so there is no fetch history to file, and
        # a row per paused view would fill an operator's board with the same host fact repeated.
        # The sentence is the breaker's own - host and remaining cooldown - plus what follows from
        # it here. [M5.3 review cycle 2, M53-C2-NET-01; decision 422]
        return Captured(error=f"{exc}, so it was not asked", name=name)
    except fetch.FetchError as exc:
        if exc.retryable:
            return Captured(error=str(exc), name=name)
        # THE FAILURE ROW IS FILED UNDER THE BARE URL, not the made-against one, and that is
        # deliberate rather than a shortcut. There is no `Response` to read `request_url` off,
        # and this package imports no HTTP client to rebuild the query string with - the one
        # thing `_htmlutil`'s guard exists to keep true one module over. Nothing is lost:
        # `_validators` reads only rows where `ok`, so a failure row conditions nothing, and the
        # question an operator asks of it - which of this title's sources refused - is answered
        # by `entity_key` and `source`, which are exact. The query is not put in `request_meta`
        # either, because for TMDB and OMDb it is where the credential is.
        doc_id = await rawstore.store(
            ctx.conn, source=source, kind=kind, url=url, content=b"",
            entity_key=ctx.task.key, page=page, http_status=exc.status,
            content_type=content_type, ok=False, error=str(exc), run_id=ctx.run_id,
        )
        return Captured(doc_id=doc_id, error=str(exc), name=name)

    if resp.from_cache:
        # A 304. `rawstore.store` refuses to write this as a good document - "a 304 carries no
        # bytes" - and refusing is right: the bytes this app holds are the document, and a
        # zero-byte row would become the newest good one for this entity.
        return Captured(response=resp, unchanged=True, name=name)

    # `max(min_bytes, 1)`, so a ZERO-length 200 is always a failure and `min_bytes` only raises
    # the floor. Two reasons, and the second is what makes it a rule rather than a caller's
    # option. `rawstore.store` REFUSES to write an empty document as good - "an empty gzip stream
    # decompresses to b'' with no error, so a derive would read it as the document" - and it
    # refuses by RAISING, so without this the one HTTP answer a bot wall most often gives would
    # come out of a stage-2 adapter as an exception, which is precisely what decision 334 says
    # seven of these eight sources may never do. And it is the same judgement `min_bytes` already
    # encodes for HTML: a body too short to be the page is not the page.
    #
    # BEFORE THE VERDICT, because a verdict is a judgement about a DOCUMENT and there is not one.
    # `omdb._refusal` reads an empty body as "a body that is not JSON", which is true and is the
    # wrong sentence to put on §6.6's board: it sends an operator to look for a proxy's
    # interception page when what the host sent was nothing at all.
    #
    # AND A JSON CALLER GETS `json_object` WHEN IT PASSED NO VERDICT OF ITS OWN, which is the same
    # rule one shape over: a caller that declared the media type it asked for has made the claim
    # this checks, and a guard applied at six of eight call sites is a guard applied at the two
    # that happened to be written last. `content_type` is the declaration and not the answer -
    # `resp.content_type` is what the host said, and a host that says `text/html` while returning
    # the JSON that was asked for is not the failure here. The two callers that pass a verdict keep
    # it: both already decode defensively and both say something narrower and truer than this can.
    note = ""
    if len(resp.content) < max(min_bytes, 1):
        note = f"empty body (HTTP {resp.status}, {len(resp.content)}b)"
    elif verdict is not None:
        note = verdict(resp)
    elif "json" in content_type:
        note = json_object(resp)
    if note:
        doc_id = await rawstore.store(
            ctx.conn, source=source, kind=kind, url=resp.request_url, content=resp.content,
            entity_key=ctx.task.key, page=page, http_status=resp.status,
            content_type=resp.content_type or content_type, request_meta=request_meta,
            ok=False, error=note, run_id=ctx.run_id,
        )
        return Captured(doc_id=doc_id, response=resp, error=note, name=name)

    doc_id = await rawstore.store(
        ctx.conn, source=source, kind=kind, url=resp.request_url, content=resp.content,
        entity_key=ctx.task.key, page=page, http_status=resp.status,
        content_type=resp.content_type or content_type, request_meta=request_meta,
        etag=resp.headers.get("etag"), last_modified=resp.headers.get("last-modified"),
        run_id=ctx.run_id,
    )
    return Captured(doc_id=doc_id, response=resp, name=name)


async def fetch_views(
    ctx: StageContext,
    *,
    source: str,
    kind_prefix: str,
    views: Iterable[View],
    content_type: str = HTML,
    min_bytes: int = MIN_HTML_BYTES,
) -> list[Captured]:
    """Fetch each view independently. `mdc/sources/_htmlutil.py:38-97`, with two named changes.

    ONE: NO `entity_key` PARAMETER. The corpus takes it and every call site passes
    `str(row["id"])`; here it is `ctx.task.key` by rule and a parameter would be a way to get it
    wrong. TWO: it returns the `Captured` rows rather than `(n_ok, errors)`. The count and the
    error strings are both derivable from them, and the caller that needs the document id - which
    is every caller that will later read the bytes back out of the store - had no way to reach it
    through the corpus's return.

    Independently, which is the word the corpus's docstring uses and the behaviour that matters:
    Metacritic's critic page and user page are two requests, either one can 404, and a title with
    one of the two is worth more than a title with neither.
    """
    return [
        await capture(
            ctx, source=source, kind=f"{kind_prefix}:{view.name}", url=view.url,
            page=view.page, request_meta=view.meta or None, content_type=content_type,
            min_bytes=min_bytes, name=view.name,
        )
        for view in views
    ]


def notes(captured: Iterable[Captured]) -> str:
    """The failures among `captured`, as the one sentence decision 334 puts on the board.

    Empty when nothing failed, so a caller writes `note=notes(caps) or "..."` and an operator
    reading `acquisition_job.detail` sees a source's name against a reason or nothing at all.
    """
    return "; ".join(
        f"{cap.name}: {cap.error}" if cap.name else cap.error
        for cap in captured if not cap.ok
    )


__all__ = [
    "HTML",
    "JSON",
    "MIN_HTML_BYTES",
    "Captured",
    "View",
    "capture",
    "fetch_views",
    "json_body",
    "json_object",
    "notes",
]
