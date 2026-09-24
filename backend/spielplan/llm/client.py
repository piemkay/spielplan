"""The shape the three provider adapters share, and the one call stage 6 makes. Spec v2.1 §9, §8.

Everything between the two rules below is `mdc/llm/client.py`'s own module docstring, carried
across verbatim at the corpus's own line width, because it is the argument for having three
adapters at all. Its one reference that is the corpus's rather than this app's is the validator it
names: here the count and the contract are enforced by M5.4's `dna/verify.verify_payload` and the
two-attempt loop, and `contract.py` is where the retry is worded.

--- `mdc/llm/client.py:1-20`, verbatim ------------------------------------------------------

Provider-agnostic structured-output call.

Each provider enforces JSON shape differently, and the differences are not
cosmetic:

* **Anthropic** has no ``response_format``; the reliable route is to declare
  a single tool whose ``input_schema`` is the contract and force it with
  ``tool_choice``.  The answer then arrives as the tool's already-parsed
  input rather than as text to re-parse.
* **OpenAI** has ``json_schema`` with ``strict: true``, which guarantees the
  shape - but strict mode rejects the array-length and numeric-range keywords
  this schema uses, so those are stripped and the count is enforced by
  ``aspects.prompt.validate`` and its retry instead.
* **Gemini** takes an OpenAPI-subset ``responseSchema`` which keeps array
  lengths but rejects ``additionalProperties``, and wants its ``type`` values
  upper-cased.

So each provider gets its own schema adapter, and every one of them is
followed by the same validator regardless - the schema is a cost-saving
device, not the guarantee.  The guarantee is the validator.

--- end of the ported text; everything below is this port's ---------------------------------

PORT VERDICT: **ported with named changes** from `mdc/llm/client.py` (324 lines). Taken verbatim:
`PROVIDERS` (`:34`), `TIMEOUT_S` and its comment (`:36-39`), `LLMError`'s retryable flag
(`:44-47`), `estimate_tokens` (`:90-97`), and `_FENCE` and `_loads` (`:306-324`). The three
adapter bodies (`:160-303`) are ported into their own modules, each with its own verdict. What
changed here, each argued at the line it changes:

  1. **The three adapters are three modules.** One file held all three in the corpus; here each
     carries its own port verdict and its own reference citations, and reaches this module for
     the shape they share. `complete` reaches them at call time (`_adapter`), because they import
     this module and an import cycle resolved by import order is one reorder away from breaking.
  2. **`resolve()` is rewritten out.** It read the provider, the model and the key off a process
     `Config`; §2 puts all three in `connector_config` behind the DEK, so the caller reads them
     there and passes them in (decision 324's `extraction_provider`, each provider's own row).
     Its three refusals are kept, in `complete`, before any request is made.
  3. **`LLMResult` gains `content`, `request_url` and `http_status`, and loses `cost_usd`.** §8:
     "All fetched bytes land in the app's own raw store", and a paid answer is fetched bytes --
     stage 6 stores it (decision 430's `response_document_id`) under the url the request was made
     against, which is `fetch.Response.request_url`'s whole argument. `cost_usd` answered 0.0 for
     a model with no price, which is the invented number decision 343 refuses: a price is
     `llm/pricing`'s to state and a cost `llm/spend`'s to compute.
  4. **`LLMError` gains `status`.** Decision 431 fails stage 6 for good on a provider 4xx and
     keeps a 5xx on the queue's curve, and §6.6's park reason names what the provider said.
  5. **Every failure `complete` can meet is an `LLMError`.** The corpus let a fetch failure or an
     unreadable envelope escape as whatever type raised it. Decision 431 decides permanent or
     curve off `retryable` and nothing else, so an exception of any other type would skip that
     decision entirely. See `post` and `envelope`.
  6. **A 5xx is retryable even when the fetcher does not retry it.** See `_servers_own`.
  7. **`MAX_OUTPUT_TOKENS` is a constant**, where the corpus read `LLM_MAX_OUTPUT_TOKENS` from
     the environment with this default (`mdc/config.py:490`). §2 keeps connector settings out of
     env vars, and a ceiling nobody is shown on §6.6 is not a setting an admin can own.
  8. **No message carries the key.** See `_redacted`.
  9. **`probe` and `open_fetcher` are new** (decision 433): the free models-list read §6.6's test
     button makes, and the one place a fetcher is built outside a drain.
 10. **An error says what the attempt was billed** (decision 436 (3)). `LLMError` carries the
     `answer` -- an `LLMResult` with no payload, holding the usage, bytes, url and status -- when a
     200 envelope's usage block read, whatever the adapter then refused about the answer; and
     `unbilled` when the provider's own error status, or a request that never left, says no work
     was done. Neither, and the answer was lost: stage 6 keeps the attempt at its write-ahead
     ceiling. The corpus raised with the flag alone, so every refused 200 was metered at zero while
     the provider billed it. [M5.5 review cycle 1, M55-METER-01, M55-SPEND-04]
 11. **An account refusal is marked `account`**, and read in the provider's own words: see
     `_account_refusal`. [M5.5 review cycle 1, M55-BUDGET-07, M55-DBL-06]
 12. **An answer carries the price multiplier its envelope reports** (`LLMResult.rate`), and a
     write-ahead ceiling counts what the provider bills beyond the prompt text (`ceiling_input`,
     `ceiling_rate`). See each. [M5.5 review cycle 2, M55-C2-METER-01, M55-CAP-C2-01]
 13. **Two refusals that are settings and not the title** are marked: a breaker pause that sent
     nothing (`paused_for`) and a 404 naming the model (`model_refused`). See `post`. [M5.5 review
     cycle 2, C2-PAID-02, DBL-C2-05]

KEYS NEVER IN A URL. §9 names "header-only API key so credentials never hit logs", and this app
runs all three machines a url with a key in it would reach: httpx writes one INFO line per request
with the full url (`push/send.py` installs a filter against exactly that for its own credential),
`raw_document.url` keeps the url a document was fetched under, and `fetch_host_state` is keyed on
the host of it. So every adapter builds a bare url, puts its key in the header its provider
documents, and passes no `params=` at all -- which `test_llm_adapters.py` holds by reading these
modules as well as by reading the requests they make.

EVERY REQUEST IS ONE CALL TO M5.1's FETCHER, and nothing here opens a client of its own: §9's "one
POST per provider through the rate-limited fetcher". The per-host rate, the 429 rule, the backoff
and the breaker are `acquire/fetch.py`'s and the three provider hosts' numbers are
`acquire/hosts.py`'s, so a provider call is paced, retried and refused exactly as every other
request this household makes -- and it carries the fetcher's declared User-Agent, which a caller
may not replace (decision 340).
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

from spielplan.acquire import fetch

if TYPE_CHECKING:
    import asyncpg

PROVIDERS = ("anthropic", "openai", "gemini")

# Generation is slow: a couple of thousand output tokens can take well over a
# minute on a loaded endpoint, and a timeout that fires mid-generation costs
# the full price of the call for nothing.
TIMEOUT_S = 300.0

# `emit_aspects` in the corpus, whose contract emitted aspect phrases. This one emits DNA tags, and
# the name is sent to all three providers - as Anthropic's tool and as OpenAI's schema name - so
# it says what the call returns.
TOOL_NAME = "emit_dna"

# Named change 7. Every default model reasons before it answers and bills the reasoning as output
# (`mdc/config.py:181-186` measured ~3.8k and ~3.9k billed on this prompt), and both
# OpenAI's `max_completion_tokens` and Gemini's `maxOutputTokens` count the reasoning against this
# ceiling - so it is twice the measured bill, not twice the visible JSON.
MAX_OUTPUT_TOKENS = 8000

# Named change 5. The statuses a provider documents a JSON error envelope for: a malformed request,
# a refused key, a forbidden resource, a retired model -- and the billing, conflict, size and rate
# refusals, which is where the list was short. Handed to the fetcher as `allow_status` so the
# provider's own words reach the error: "invalid x-api-key" is a thing an admin can act on and
# "HTTP 401" is not, and a household out of credit could not tell Anthropic's "402 - billing_error"
# or OpenAI's 429 `credit_balance_exhausted` from a rate limit while each read "HTTP 402" and
# "HTTP 429" (https://platform.claude.com/docs/en/api/errors,
# https://developers.openai.com/api/docs/guides/error-codes). A 429 in this list is still the
# fetcher's to pace, re-send and count toward the breaker: `allow_status` hands its body back only
# once the attempts are spent (`fetch.get`'s docstring). Every status here but 429 is final; a 429
# stays on the queue's curve unless `_account_refusal` reads it as the account's.
# [M5.5 review cycle 1, M55-DBL-06]
ERROR_STATUS = (400, 401, 402, 403, 404, 409, 413, 429)

# The statuses after which a generation may have finished and been billed although no answer came
# back: a gateway's timeout in front of the provider -- Anthropic's "504 - timeout_error: The request
# timed out while processing" (https://platform.claude.com/docs/en/api/errors) and Cloudflare's 524 --
# and Cloudflare's 520, which it sends "when the origin server returns an empty, unknown, or unexpected
# response" (https://developers.cloudflare.com/support/troubleshooting/http-status-codes/
# cloudflare-5xx-errors/error-520/): an origin that took the request and crashed or closed the
# connection, the same lost answer as 524, and Cloudflare fronts the direct Claude API. 520 was settled
# to zero here, and the meter read low on it. An attempt answered with any other status is settled to
# zero (decision 436 (3)) -- and for Anthropic and OpenAI that is decision 436's reading and not
# their word: neither error page says what a 500, a 529 or a 503 bills, and the reading is that a
# status the provider's own API sends in its documented error envelope answers the request, where
# these three are a gateway's word that the answer was lost. Only Google publishes the zero ("If your
# request fails with a 400 or 500 error, you won't be charged", https://ai.google.dev/gemini-api/docs/
# billing), which is why its adapter passes an empty set: see `gemini.LOST_STATUS`. This comment
# called every other status "the provider's statement that it served nothing", which two of the three
# never made. [M5.5 review cycle 2, M55-C2-METER-02, DBL-C2-04]
LOST_STATUS = frozenset({504, 520, 524})

# How much of a provider's own words an error carries: the corpus's 300 characters, which is how
# much of a response every error in `mdc/llm/client.py` quotes.
_SHOWN = 300


class LLMError(Exception):
    """Every failure `complete` can meet (named change 5), and what it was billed (named change 10).

    `answer` is set when a 200 envelope's usage block read: the attempt is settled to that usage and
    the envelope kept. `unbilled` is set when the provider's own error status, or a request that
    never left, says no work was done: settled to zero. Neither, and the answer was lost, which
    leaves the attempt at its write-ahead ceiling -- the default, because an error nobody classified
    is not evidence that nothing was charged. `account` marks a refusal of the household's account
    rather than of this title (`_account_refusal`). `paused_for` is the seconds left on the fetcher's
    breaker when it refused to send at all, and `model_refused` a 404, which a paid endpoint answers
    about the model it was asked for and nothing else (named change 13).
    """

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None,
                 answer: LLMResult | None = None, unbilled: bool = False, account: bool = False,
                 paused_for: float | None = None, model_refused: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.answer = answer
        self.unbilled = unbilled
        self.account = account
        self.paused_for = paused_for
        self.model_refused = model_refused


@dataclass
class LLMResult:
    provider: str
    model: str
    payload: Any                      # parsed JSON from the model
    tokens_in: int = 0
    # BILLED output, which is not the same number in every envelope: each adapter says how its
    # provider reports it, and Gemini's is a sum (§9: counting visible JSON understates ~5x).
    tokens_out: int = 0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    # Named change 3: the bytes the provider sent, the url they were fetched under and the status,
    # which is what the raw store keeps for a paid answer.
    content: bytes = b""
    request_url: str = ""
    http_status: int = 0
    # The part of `tokens_in` the provider wrote to its prompt cache, and the part it read from it,
    # each billed at its own rate (`pricing.usd`). Only OpenAI reports them for a request that sends
    # no cache directive: GPT-5.6 and later write the prompt by default (see `openai.py`).
    cache_written: int = 0
    cache_read: int = 0
    # The multiplier over the table's standard price that the envelope says this attempt was billed
    # at: Anthropic's US-only inference, OpenAI's Fast tier. Each provider applies an account-level
    # default to a request that names none and reports on the envelope what it applied, and a meter
    # that priced every call at the standard row held half of what a Fast project was billed.
    # Named change 12; each adapter says what it reads. [M5.5 review cycle 2, M55-C2-METER-01]
    rate: float = 1.0


def estimate_tokens(text: str) -> int:
    """Rough token count, ~3.6 characters per token, the corpus's figure and its heuristic.

    The corpus's docstring said this "is never used for anything but showing a price before a run
    starts", which was true there and is not here: it sizes decision 325's reservation, which
    decides whether stage 6 may bill at all, and decision 436's write-ahead ceiling, which is what
    the meter holds for a call whose answer never arrives. So what it misses is money the gate does
    not reserve. It sees the prompt text and not the schema an adapter sends beside it, and it reads
    English prose at the corpus's average -- which Anthropic's newer tokenizer outruns: "Claude 4.7
    and later models ... use a newer tokenizer ... This tokenizer produces approximately 30% more
    tokens for the same text" (https://platform.claude.com/docs/en/about-claude/pricing), where the
    same page's own baseline is about four characters a token, so the count here runs perhaps 15%
    short on Claude. Both gaps are the estimate error `llm/spend.py` bounds in the reservation. They
    reach the meter only through a write-ahead ceiling left standing, which is why the ceiling does
    not count with this alone: `ceiling_input` adds what each adapter sends and bills beyond the
    prompt text. [M5.5 review cycle 1, M55-DOC-09; review cycle 2, M55-C2-METER-03]
    """
    return int(len(text) / 3.6) + 1


def ceiling_input(provider: str, system: str, user: str, schema: dict[str, Any]) -> int:
    """The input tokens a write-ahead ceiling counts for one attempt (decision 436 (2)): the prompt and
    the schema sent beside it at `estimate_tokens`, times the adapter's `CEILING_TOKENIZER`, plus its
    `CEILING_OVERHEAD`.

    THE CEILING IS WHAT THE METER HOLDS FOR AN ANSWER THAT NEVER ARRIVED, so it has to bound the bill
    and not estimate it. It counted the prompt text alone, which on Claude missed three things the
    pricing page bills as input: the `tools` block, "a special system prompt for the model that enables
    tool use" (474 tokens on Claude Sonnet 5 under a forced tool), and "approximately 30% more tokens
    for the same text" on the newer tokenizer (https://platform.claude.com/docs/en/about-claude/
    pricing). A cut-off spends every one of `MAX_OUTPUT_TOKENS`, so nothing on the output side made up
    the shortfall, and a lost Sonnet 5 cut-off stood in the month about 6% below its bill on a spec-size
    pack. Every adapter now names its own margin. [M5.5 review cycle 2, M55-CAP-C2-01,
    M55-C2-METER-03]"""
    adapter = _adapter(provider)
    counted = estimate_tokens(system) + estimate_tokens(user) + estimate_tokens(json.dumps(schema))
    return math.ceil(counted * adapter.CEILING_TOKENIZER) + adapter.CEILING_OVERHEAD


def ceiling_rate(provider: str) -> float:
    """The price multiplier a write-ahead ceiling is held at: the dearest the adapter's request can be
    billed at under any account setting it does not pin (named change 12)."""
    return _adapter(provider).CEILING_RATE


def header_key(key: str) -> str | None:
    """The key as a header can carry it: whitespace around it taken off, and None when what is left
    still holds a character no HTTP header value may -- a control character, whitespace inside it, or
    anything outside printable ASCII.

    A key pasted with a trailing newline -- a quoted `.env` value, a card's text field -- used to reach
    httpx as typed. h11 refused the header with `LocalProtocolError("Illegal header value
    b'<key>\\n'")`, the fetcher re-sent it, and the bytes repr in that message is a spelling of the key
    `_redacted` did not know, so the whole working key went into `llm_call.error`, the board and the
    test card. Whitespace AROUND a key is no part of it; a character INSIDE one cannot be mended
    without guessing, and is refused before any request with a sentence that names the fault and never
    the key. [M5.5 review cycle 1, KEYS-C1-01]
    """
    trimmed = key.strip()
    if not trimmed or not all("!" <= ch <= "~" for ch in trimmed):
        return None
    return trimmed


def count(block: Any, name: str, *, required: bool = False) -> int | None:
    """One usage count as a provider reports it: a whole number of at least zero. None when it is not
    one -- a string, a negative, a fraction, a `true` -- or when a `required` count is absent; an
    optional count that is absent is zero, as Gemini leaves out `thoughtsTokenCount` for a model
    that did not think. The adapters read usage through this and nothing else, because a bare
    `int()` on a provider's member raised `ValueError` out of `complete` on "1,600" and let a billed
    attempt escape unmetered (named change 5). [M5.5 review cycle 1, M55-METER-04]"""
    if not isinstance(block, dict):
        return None
    value = block.get(name)
    if value is None:
        return None if required else 0
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0 or (isinstance(value, float) and not value.is_integer()):
        return None
    return int(value)


def billed(provider: str, model: str, resp: fetch.Response, data: dict[str, Any], *,
           tokens_in: int | None, tokens_out: int | None, cache_written: int | None = 0,
           cache_read: int | None = 0, rate: float = 1.0) -> LLMResult | None:
    """The bill a 200 envelope reported, as an `LLMResult` with no payload yet -- or None when any of
    its counts did not read, which leaves an attempt at its write-ahead ceiling rather than at a
    figure half made up (decision 436 (3)). Built before the answer is judged, so the adapters can
    raise with it from every branch that refuses the answer."""
    counts = (tokens_in, tokens_out, cache_written, cache_read)
    if any(n is None for n in counts):
        return None
    model_said = data.get("model") or data.get("modelVersion")
    return LLMResult(
        provider=provider, model=model_said if isinstance(model_said, str) and model_said else model,
        payload=None, tokens_in=tokens_in, tokens_out=tokens_out, raw=data, content=resp.content,
        request_url=resp.request_url, http_status=resp.status, cache_written=cache_written,
        cache_read=cache_read, rate=rate,
    )


def shown(text: str, key: str) -> str:
    """A provider's or a proxy's words as an error quotes them: the key taken out of the WHOLE text
    and only then cut to the corpus's 300 characters. Cut first, and a key that straddled character
    300 was never matched, so its prefix -- most of the key at the right offset -- survived into the
    message. [M5.5 review cycle 1, KEYS-C1-02]"""
    return _redacted(text, key)[:_SHOWN]


def open_fetcher(conn: asyncpg.Connection | None) -> fetch.Fetcher:
    """The one fetcher built outside a drain, for §6.6's provider test button. Decision 433.

    Module-level so a test replaces this one function, the way `registry.make_client` is replaced,
    rather than threading a transport through a route; the registry's provider test calls it by
    this name and enters it with `async with`. Built on the caller's connection so the probe reads
    and flushes `fetch_host_state` like any drain: a breaker a paid call opened is a breaker the
    test button honours, and a 429 the button meets is one the next paid call waits out.

    No `jellyfin_host`, because the only hosts this reaches are the three provider rows, and
    `hosts.policy_for` lets a declared row outrank the Jellyfin exemption whatever url an admin
    typed - so there is no value it could take that would change a provider's policy.
    """
    return fetch.Fetcher(conn=conn)


def _adapter(provider: str) -> ModuleType:
    """The adapter module for a provider, or the corpus's refusal for one that is not among the
    three. Imported here rather than at the top, for named change 1's reason."""
    from spielplan.llm import anthropic, gemini, openai

    adapters = {"anthropic": anthropic, "openai": openai, "gemini": gemini}
    if provider not in adapters:
        raise LLMError(f"unknown provider {provider!r}; "
                       f"expected one of {', '.join(PROVIDERS)}",
                       retryable=False)
    return adapters[provider]


# --- the call --------------------------------------------------------------


async def complete(fetcher: fetch.Fetcher, *, provider: str, key: str, model: str, system: str,
                   user: str, schema: dict[str, Any],
                   max_tokens: int = MAX_OUTPUT_TOKENS) -> LLMResult:
    """One structured-output call: one POST through `fetcher`, answered or raised as `LLMError`.

    The three refusals `resolve()` made are made here, before the fetcher is touched, and each is
    final: no retry supplies a key nobody configured (named change 2). The answer is returned
    unjudged -- the payload is what the model said, and whether it is TRUE is `verify_payload`'s
    question, asked by the caller (§9: "the guarantee is the validator").
    """
    adapter = _adapter(provider)
    if not key:
        raise LLMError(f"no API key for {provider}: spec section 6.6 sets it on the provider's card",
                       retryable=False, unbilled=True)
    usable = header_key(key)
    if usable is None:
        raise LLMError(
            f"the {provider} key holds a character no HTTP header can carry (whitespace inside it,"
            " a control character or a non-ASCII letter), so it was not sent; type it again on the"
            " provider's card (spec section 6.6)",
            retryable=False, unbilled=True,
        )
    if not model:
        raise LLMError(f"no model configured for {provider}", retryable=False, unbilled=True)
    started = time.monotonic()
    result = await adapter.call(fetcher, usable, model, system, user, schema, max_tokens)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


async def post(fetcher: fetch.Fetcher, url: str, *, key: str, headers: dict[str, str],
               body: dict[str, Any],
               lost: frozenset[int] = LOST_STATUS) -> tuple[fetch.Response, dict[str, Any]]:
    """The adapters' one POST, and named change 5's translation of everything it can meet.

    A `FetchError` keeps the fetcher's own verdict on whether it is worth asking again, and its
    status; a provider 4xx the fetcher hands back under `ERROR_STATUS` is read out of the error
    envelope that provider documents and is final -- a 429 excepted, which keeps the curve -- and
    the envelope of an answer is handed back decoded, or refused by `envelope`.

    AND EACH SAYS WHAT WAS BILLED (named change 10). The fetcher sends this POST once unless it
    provably never reached the provider (`fetch.IDEMPOTENT_METHODS`), so a `FetchError` is one of
    two things: a request that never left, or a status the provider chose -- both of which did no
    work, `unbilled` -- or a request that went out and whose answer did not come back, a read that
    failed or one of the `lost` statuses, after which the generation may have finished and been
    charged. The cause is not carried (`from None`): a chained `FetchError` is a second message
    `_redacted` never saw.

    AND TWO REFUSALS ARE A SETTING'S AND NOT THE TITLE'S (named change 13). The breaker's `HostPaused`
    is raised before anything is sent, so it carries the pause left (`paused_for`): eight account
    refusals in a row open it -- an exhausted balance answers every title with a 429 the fetcher sends
    four times -- and every later title in the drain met it as a transient failure and spent an
    attempt. And a 404 is `model_refused`: each paid url is fixed but for the model (Gemini's names it,
    the other two carry it in the body), so the one resource a provider can fail to find is the model
    an admin chose, and decision 431 had failed every title for good on a retired one.
    [M5.5 review cycle 2, C2-PAID-02, DBL-C2-05]
    """
    try:
        resp = await fetcher.get(url, method="POST", json_body=body, headers=headers,
                                 timeout=TIMEOUT_S, allow_status=ERROR_STATUS)
    except fetch.FetchError as exc:
        unbilled = fetch.never_sent(exc) or (exc.status is not None and exc.status not in lost)
        raise LLMError(shown(str(exc), key),
                       retryable=exc.retryable or _servers_own(exc.status),
                       status=exc.status, unbilled=unbilled,
                       paused_for=exc.remaining if isinstance(exc, fetch.HostPaused) else None,
                       ) from None
    except UnicodeEncodeError:
        # httpx encodes header values as ASCII before anything is sent; `header_key` refuses such a
        # key in `complete`, and this is the same refusal for any other header an adapter builds.
        raise LLMError("a request header could not be encoded for sending, so nothing was sent",
                       retryable=False, unbilled=True) from None
    if resp.status in ERROR_STATUS:
        data = _error_envelope(resp)
        account = _account_refusal(resp.status, data)
        raise LLMError(error_text(resp, key), retryable=resp.status == 429 and not account,
                       status=resp.status, unbilled=True, account=account,
                       model_refused=resp.status == 404)
    return resp, envelope(resp, key)


def envelope(resp: fetch.Response, key: str = "") -> dict[str, Any]:
    """The decoded envelope of an answer, or a retryable error when there is none.

    A 200 whose body is not a JSON object is not the provider's answer at all -- it is a proxy's
    or a load balancer's page in front of it - so it is worth asking again, where a
    `JSONDecodeError` raised out of `resp.json()` would have been a type decision 431 cannot read.
    Its body is quoted through `shown`, because a page that echoes the request echoes its headers.
    """
    try:
        data = resp.json()
    except ValueError:
        raise LLMError(f"response envelope was not JSON (HTTP {resp.status}): "
                       f"{shown(resp.text, key)}") from None
    if not isinstance(data, dict):
        raise LLMError(f"response envelope was {type(data).__name__}, not an object "
                       f"(HTTP {resp.status})")
    return data


def _error_envelope(resp: fetch.Response) -> dict[str, Any] | None:
    """The `error` object of a documented error body, or None when the body has none."""
    try:
        data = json.loads(resp.content)
    except ValueError:
        return None
    err = data.get("error") if isinstance(data, dict) else None
    return err if isinstance(err, dict) else None


def error_text(resp: fetch.Response, key: str) -> str:
    """A provider's 4xx in its own words, bounded and with the key taken out.

    The three document one shape between them: an `error` object with a `message`, labelled by
    `type` (Anthropic, https://docs.anthropic.com/en/api/errors), by `code` and then `type`
    (OpenAI, https://platform.openai.com/docs/guides/error-codes), or by `status` (Gemini,
    https://ai.google.dev/gemini-api/docs/troubleshooting, whose `code` is the number). The first
    label that is text wins, in the order that finds each provider's most specific one. A body
    that does not have the shape still yields its status. The key is taken out of the whole text
    before it is cut (`shown`).
    """
    err = _error_envelope(resp)
    label = message = ""
    if err is not None:
        label = next((value for name in ("status", "code", "type")
                      if isinstance(value := err.get(name), str) and value), "")
        message = str(err.get("message") or "")
    text = f"HTTP {resp.status}" + (f" {label}" if label else "")
    if message:
        text += f": {shown(message, key)}"
    return _redacted(text, key)


# The documented refusals of the household's ACCOUNT, as against refusals of one request. OpenAI's
# 429 codes: "Your organization has no prepaid credits remaining", "reached its enforced spend
# limit" (organization and project) and "reached its OpenAI-assigned usage limit", with "Retrying
# billing, spend, or quota errors won't restore API access"
# (https://developers.openai.com/api/docs/guides/error-codes) -- and the same page's "The broader
# `error.type` can still be `insufficient_quota`", which is the class where each code is one cause:
# a billing 429 under a code outside these four, the legacy `insufficient_quota` among them, read as a
# rate limit and burned its walks. A rate limit's type is `requests` or `tokens`, never that one.
# [M5.5 review cycle 2, DBL-C2-06]
ACCOUNT_CODES = frozenset({
    "credit_balance_exhausted", "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
})
OPENAI_QUOTA_TYPE = "insufficient_quota"
# Gemini's spent daily quota. generateContent answers errors as google.rpc.Status -- an integer `code`
# and a `status` name, as `error_text` reads them -- and a quota as "a `429 RESOURCE_EXHAUSTED` error",
# the per-day one resetting "at midnight Pacific time" (https://ai.google.dev/gemini-api/docs/
# rate-limits). Google's error model gives the detail that tells a daily limit from a per-minute one,
# "if a daily limit was exceeded for the calling project, a service could respond with a QuotaFailure
# detail" (https://github.com/googleapis/googleapis/blob/master/google/rpc/error_details.proto), whose
# violations name the quota in `quotaId`; no Google page lists Gemini's ids, and the per-day ones seen
# carry "PerDay" (UKGovernmentBEIS/inspect_ai#5526), which is all this reads. This used to match a string
# `code` "quota_exceeded", the Interactions API's shape (https://ai.google.dev/gemini-api/docs/
# api-errors), which the endpoint this app calls never sends -- so the account park was dead for Gemini.
# [M5.5 review cycle 2, DBL-C2-03]
GEMINI_QUOTA_STATUS = "RESOURCE_EXHAUSTED"
GEMINI_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
GEMINI_PER_DAY = "PerDay"
# Anthropic's tier spend cap: a 429 whose "`error.details.error_code` is
# `enforced_spend_limit_reached`. Use it to tell this response apart from a rate limit"; and a spend
# limit the household set itself: "requests return HTTP 400 with error type `invalid_request_error`.
# The message begins `You have reached your specified API usage limits`, or `You have reached your
# specified workspace API usage limits`" (https://platform.claude.com/docs/en/api/rate-limits).
ANTHROPIC_SPEND_CAP = "enforced_spend_limit_reached"
ANTHROPIC_OWN_LIMIT = ("You have reached your specified API usage limits",
                       "You have reached your specified workspace API usage limits")


def _account_refusal(status: int, err: dict[str, Any] | None) -> bool:
    """Named change 11: whether a refusal is of the household's account -- a balance that ran out, a
    spend limit or a quota reached -- and so lifts when the period rolls over or the household tops
    up, rather than of this request. Decision 431 failed every such title for good on a 4xx and
    burned four walks each on a 429, so a household that set a monthly limit in the provider's
    console lost every title that reached stage 6 for the rest of the month; stage 6 parks on this
    instead (decision 439), which is decision 336's "waiting on something that may change". A 402 is
    the account's on every provider: Anthropic's "402 - billing_error" and Gemini's "Your Prepay
    credit balance is depleted ... Don't retry". The rest are read off the documented codes and
    message openings above and nothing looser: a per-minute rate limit is not the account's.
    [M5.5 review cycle 1, M55-BUDGET-07; review cycle 2, C2-PAID-04]"""
    if status == 402:
        return True
    if err is None:
        return False
    code, details = err.get("code"), err.get("details")
    message = err.get("message") if isinstance(err.get("message"), str) else ""
    if status == 429:
        return ((isinstance(code, str) and code in ACCOUNT_CODES)
                or err.get("type") == OPENAI_QUOTA_TYPE
                or (isinstance(details, dict) and details.get("error_code") == ANTHROPIC_SPEND_CAP)
                or (err.get("status") == GEMINI_QUOTA_STATUS and _per_day_quota(details)))
    return status == 400 and message.startswith(ANTHROPIC_OWN_LIMIT)


def _per_day_quota(details: Any) -> bool:
    """Whether a google.rpc.Status's `details` hold a `QuotaFailure` naming a per-day quota."""
    if not isinstance(details, list):
        return False
    for detail in details:
        if not isinstance(detail, dict) or detail.get("@type") != GEMINI_QUOTA_FAILURE:
            continue
        violations = detail.get("violations")
        for violation in violations if isinstance(violations, list) else []:
            quota = violation.get("quotaId") if isinstance(violation, dict) else None
            if isinstance(quota, str) and GEMINI_PER_DAY in quota:
                return True
    return False


def _servers_own(status: int | None) -> bool:
    """Named change 6: a 5xx is the provider failing, not the provider answering.

    The fetcher's `RETRYABLE_STATUS` is the corpus's set verbatim and has no 529, which Anthropic
    documents as `overloaded_error` (https://docs.anthropic.com/en/api/errors) - so `fetch.get`
    raises it at once with `retryable=False`, the flag it gives a real answer. Passed through, an
    overloaded provider would fail stage 6 PERMANENTLY under decision 431, and a title would need
    an admin's retry to recover from the provider's bad afternoon. Decision 431 says an exhausted
    5xx is "an ordinary fail on the queue's curve", so that is what any 5xx becomes here -- and the
    queue's curve is the retry, not this layer: nothing is re-sent from here, and every re-run
    passes stage 6's cap check first.
    """
    return status is not None and status >= 500


def _redacted(text: str, key: str) -> str:
    """Named change 8: the key taken out of any message this module raises or returns.

    OpenAI's documented 401 quotes the key it refused, masked; a provider that quoted it whole, or
    a proxy that echoed the request, would put it into an exception - which is a log line, an
    `acquisition_job` park reason and a line on §6.6's board. None of those is a place a
    credential may reach (§9), so the whole key is replaced wherever it appears.

    IN EVERY SPELLING AN EXCEPTION GIVES IT, not only as typed. h11 quotes a header value it refuses
    as a bytes repr, `b'<key>\\n'`, a traceback quotes a string as a str repr, and a key pasted with
    a newline after it is the key itself plus one character -- and the exact-string match found none
    of the three, so the whole working key reached the meter's `error` column, the board and the
    test card through the first. Longest first, so a spelling inside another is never left half
    replaced. [M5.5 review cycle 1, KEYS-C1-01]
    """
    if not key:
        return text
    spellings = {key, key.strip(), repr(key)[1:-1],
                 repr(key.encode("utf-8", "backslashreplace"))[2:-1]}
    for spelling in sorted((s for s in spellings if s), key=len, reverse=True):
        text = text.replace(spelling, "[redacted]")
    return text


# --- the test button -------------------------------------------------------


async def probe(fetcher: fetch.Fetcher, provider: str, *, key: str,
                model: str | None) -> dict[str, object]:
    """§6.6's test button for a provider card: its documented, free models-list read. Decision 433.

    A GET, with the key in the header the paid call uses and the url bare of it, through the same
    fetcher, pacing and breaker as a paid call. Nothing is generated and nothing is metered, so
    the button costs the household nothing and needs no cap. Returns `ok` (the provider accepted
    the key and answered with a list), the HTTP `status`, the provider's own `error` when it
    refused, the `model` asked about, and `model_listed`: True or False when the list settles it,
    None when there is no model to ask about, the list continues past the page read, or the key
    was refused.

    LISTED IS NOT USABLE, and the corpus met it the hard way: "Google's entire Gemini 2.5 family
    went from listed to 404 for new keys with no change on our side" (`mdc/config.py:175-179`) -
    the models "still [appear] in ListModels but `generateContent` answers 404 'no longer available
    to new users'" (`:152-154`). A card that read `model_listed: True` as "this model works" would
    be the reassurance that fails on the first paid call. It is also not necessary: a provider may
    resolve an alias its list does not spell out. The answer to "does this model work" is a paid
    call, which is stage 6's and not a button's.
    """
    adapter = _adapter(provider)
    answer: dict[str, object] = {"ok": False, "status": None, "error": None,
                                 "model": model or None, "model_listed": None}
    if not key:
        return {**answer, "error": f"no API key is configured for {provider}"}
    usable = header_key(key)
    if usable is None:
        # `complete`'s refusal, in the button's shape: the key is not sent and not quoted.
        return {**answer, "error": f"the {provider} key holds a character no HTTP header can carry"
                                   " (whitespace inside it, a control character or a non-ASCII"
                                   " letter); type it again"}
    try:
        # The fetcher's own timeout and not `TIMEOUT_S`, which is sized for a generation: a list
        # read that has not answered inside the client's 45-second read is a failed test, and an
        # admin waiting five minutes on a button is the worse way to learn it.
        resp = await fetcher.get(adapter.MODELS_URL, headers=adapter.auth_headers(usable),
                                 allow_status=ERROR_STATUS)
    except fetch.FetchError as exc:
        return {**answer, "status": exc.status, "error": shown(str(exc), key)}
    if resp.status in ERROR_STATUS:
        return {**answer, "status": resp.status, "error": error_text(resp, key)}
    try:
        ids, more = adapter.listed_models(envelope(resp, key))
    except LLMError as exc:
        return {**answer, "status": resp.status, "error": shown(str(exc), key)}
    listed: bool | None = None
    if model:
        listed = True if model in ids else (None if more else False)
    return {**answer, "ok": True, "status": resp.status, "model_listed": listed}


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _loads(text: str, key: str = "") -> Any:
    """Parse JSON, tolerating a markdown fence.

    Schema-constrained responses do not normally arrive fenced, but a model
    that ignores the constraint once should not cost a retry over three
    backticks.

    (This port: the text is quoted through `shown`, named change 8's key taken out before the
    corpus's 300-character cut, and the adapters re-raise the error with the answer's bill.)
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = _FENCE.sub("", text.strip())
    try:
        return json.loads(stripped)
    except ValueError:
        raise LLMError(f"response was not JSON: {shown(text, key)}") from None
