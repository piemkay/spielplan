"""The shape the three provider adapters share, and the one call stage 6 makes (§9).

Each provider enforces JSON shape differently, so each has its own adapter; the schema only saves
cost, the validator is the guarantee. Keys only ever travel in headers, never in a url.
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
from spielplan.core.logs import scrub

if TYPE_CHECKING:
    import asyncpg

PROVIDERS = ("anthropic", "openai", "gemini")

# Generation is slow: a couple of thousand output tokens can take well over a
# minute on a loaded endpoint, and a timeout that fires mid-generation costs
# the full price of the call for nothing.
TIMEOUT_S = 300.0

# Sent to every provider as the tool/schema name, so it says what the call returns.
TOOL_NAME = "emit_dna"

# Reasoning bills as output and counts against this ceiling; twice the measured ~3.9k bill.
MAX_OUTPUT_TOKENS = 8000

# Statuses with a documented JSON error envelope, so the provider's own words reach the error.
# All final except 429, which the fetcher still paces and which `_account_refusal` may reclassify.
ERROR_STATUS = (400, 401, 402, 403, 404, 409, 413, 429)

# Gateway statuses after which a generation may have been billed although no answer arrived.
# Every other error status is settled to zero (decision 436).
LOST_STATUS = frozenset({504, 520, 524})

# Characters of a provider's words an error quotes.
_SHOWN = 300


class LLMError(Exception):
    """Every failure `complete` can meet, and what it was billed.

    `answer`: settled to that usage. `unbilled`: settled to zero. Neither: the attempt stays at its
    write-ahead ceiling. `account`, `paused_for` and `model_refused` mark refusals of a setting.
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
    payload: Any  # parsed JSON from the model
    tokens_in: int = 0
    # Billed output, reasoning included; Gemini's is a sum.
    tokens_out: int = 0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    # What the raw store keeps for a paid answer.
    content: bytes = b""
    request_url: str = ""
    http_status: int = 0
    # Prompt-cache writes and reads within `tokens_in`, each billed at its own rate.
    cache_written: int = 0
    cache_read: int = 0
    # Multiplier over the standard price the envelope reports (US-only inference, Fast tier).
    rate: float = 1.0


def estimate_tokens(text: str) -> int:
    """Rough token count, ~3.6 characters per token.

    Sizes the cap reservation, so misses are money the gate does not reserve; runs ~15% short on
    Claude's newer tokenizer. `ceiling_input` adds the rest.
    """
    return int(len(text) / 3.6) + 1


def ceiling_input(provider: str, system: str, user: str, schema: dict[str, Any]) -> int:
    """The input tokens a write-ahead ceiling counts for one attempt (decision 436 (2)): the prompt and
    the schema at `estimate_tokens`, times the adapter's `CEILING_TOKENIZER`, plus its
    `CEILING_OVERHEAD`. It must bound the bill, not estimate it.
    """
    adapter = _adapter(provider)
    counted = estimate_tokens(system) + estimate_tokens(user) + estimate_tokens(json.dumps(schema))
    return math.ceil(counted * adapter.CEILING_TOKENIZER) + adapter.CEILING_OVERHEAD


def ceiling_rate(provider: str) -> float:
    """The dearest price multiplier the adapter's request can be billed at under any account setting."""
    return _adapter(provider).CEILING_RATE


def header_key(key: str) -> str | None:
    """The key as a header can carry it: surrounding whitespace stripped, None if what is left holds a
    character no header value may. Never quoted in the refusal.
    """
    trimmed = key.strip()
    if not trimmed or not all("!" <= ch <= "~" for ch in trimmed):
        return None
    return trimmed


def count(block: Any, name: str, *, required: bool = False) -> int | None:
    """One usage count as a provider reports it: a whole number >= 0, or None. An absent optional
    count is zero. The adapters read usage only through this.
    """
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
    """The bill a 200 envelope reported, as an `LLMResult` with no payload yet, or None when any count
    did not read (the attempt then stays at its ceiling).
    """
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
    """A provider's words as an error quotes them: the key removed from the whole text, then cut to 300.
    """
    return scrub(text, key)[:_SHOWN]


def open_fetcher(conn: asyncpg.Connection | None) -> fetch.Fetcher:
    """The one fetcher built outside a drain, for §6.6's provider test button. Decision 433.

    Module-level so tests can replace it. Shares `fetch_host_state` with drains, breaker included.
    """
    return fetch.Fetcher(conn=conn)


def _adapter(provider: str) -> ModuleType:
    """The adapter module for a provider. Imported lazily: the adapters import this module."""
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

    Missing provider, model or key are final refusals before any request. The answer is unjudged.
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
    """The adapters' one POST, translating everything it can meet into `LLMError`.

    Each error says what was billed: never sent or a provider-chosen status is `unbilled`; a lost
    answer is neither. A breaker pause carries `paused_for`, and a 404 is `model_refused`.
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
        # httpx encodes header values as ASCII before sending.
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
    """The decoded envelope of an answer, or a retryable error when the body is not a JSON object."""
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

    Labels: `type` (Anthropic), `code` then `type` (OpenAI), `status` (Gemini).
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
    return scrub(text, key)


# OpenAI's documented refusals of the household's account; a rate limit's type is never
# `insufficient_quota`.
ACCOUNT_CODES = frozenset({
    "credit_balance_exhausted", "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
})
OPENAI_QUOTA_TYPE = "insufficient_quota"
# Gemini's quota status; a daily quota is told by a `QuotaFailure` whose `quotaId` has "PerDay".
GEMINI_QUOTA_STATUS = "RESOURCE_EXHAUSTED"
GEMINI_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
GEMINI_PER_DAY = "PerDay"
# Anthropic's tier spend cap (429) and the household's own usage limit (400, message prefix).
ANTHROPIC_SPEND_CAP = "enforced_spend_limit_reached"
ANTHROPIC_OWN_LIMIT = ("You have reached your specified API usage limits",
                       "You have reached your specified workspace API usage limits")


def _account_refusal(status: int, err: dict[str, Any] | None) -> bool:
    """Whether a refusal is of the household's account (balance, spend limit, quota), which lifts
    with time or a top-up, so stage 6 parks rather than fails (decision 439). Every 402 is one.
    """
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
    """Any 5xx is retryable (e.g. Anthropic's 529), keeping it on the queue's curve (decision 431)."""
    return status is not None and status >= 500


# --- the test button -------------------------------------------------------


async def probe(fetcher: fetch.Fetcher, provider: str, *, key: str,
                model: str | None) -> dict[str, object]:
    """§6.6's test button for a provider card: its documented, free models-list read. Decision 433.

    Returns `ok`, `status`, `error`, `model` and `model_listed` (None when unsettled). Listed is not
    usable: providers keep retired models in the list.
    """
    adapter = _adapter(provider)
    answer: dict[str, object] = {"ok": False, "status": None, "error": None,
                                 "model": model or None, "model_listed": None}
    if not key:
        return {**answer, "error": f"no API key is configured for {provider}"}
    usable = header_key(key)
    if usable is None:
        # `complete`'s refusal, in the button's shape.
        return {**answer, "error": f"the {provider} key holds a character no HTTP header can carry"
                                   " (whitespace inside it, a control character or a non-ASCII"
                                   " letter); type it again"}
    try:
        # The fetcher's own timeout, not `TIMEOUT_S`, which is sized for a generation.
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
    """Parse JSON, tolerating a markdown fence."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = _FENCE.sub("", text.strip())
    try:
        return json.loads(stripped)
    except ValueError:
        raise LLMError(f"response was not JSON: {shown(text, key)}") from None
