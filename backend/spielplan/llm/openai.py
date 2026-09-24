"""OpenAI's Chat Completions API, structured by a strict JSON schema. Spec v2.1 §9, §8 stage 6.

`response_format` of type `json_schema` with `strict: true` guarantees the SHAPE of the answer, and
strict mode rejects the array-length and numeric-range keywords - so the salience bounds
`contract.EXTRACTION_SCHEMA` declares are stripped from the copy sent here, and a salience outside
{1,2,3} is refused by M5.4's validator instead, exactly as it is for the other two providers (§9:
"the schema is a cost-saving device, not the guarantee").

PORT VERDICT: **ported with named changes** from `mdc/llm/client.py:102-113` and `:213-254`
(`_STRICT_UNSUPPORTED`, `_strip_keywords`, `_openai`). Taken verbatim: `_STRICT_UNSUPPORTED` and
`_strip_keywords`; the request body with `max_completion_tokens` and its comment (`:217-229`); the
endpoint; the three refusals of an answer - no choices (retryable), `message.refusal` (final, and
distinct from empty content), and empty content (retryable only on `finish_reason` "length")
(`:236-248`); and the fence-tolerant parse. What changed:

  1. **The POST goes through `client.post`**, which maps a fetch failure and a documented 4xx
     envelope to `LLMError` with its status (client named change 5) and returns the decoded
     envelope; the answer carries the bytes, url and status the raw store keeps (client named
     change 3).
  2. **The bearer header is `auth_headers`**, shared with the free models-list probe (decision
     433), so the paid call and the test button cannot put the key in two places.
  3. **The usage is read first, typed, and carried by every error**: `client.count` and
     `client.billed`, so a refused 200 is metered at what it billed and a malformed member is an
     `LLMError` (client named changes 5 and 10).
  4. **The prompt's cache breakdown is read** -- see below.

THE BILL IS `completion_tokens` AS REPORTED, and that is exit check 6. OpenAI folds its reasoning
tokens into `completion_tokens` and itemises them under `completion_tokens_details.reasoning_tokens`
(https://platform.openai.com/docs/guides/reasoning) - so the itemised figure is a breakdown of the
billed one and not an addition to it, and adding it would bill the reasoning twice. Gemini is the
provider that reports its thinking apart; see `gemini.py`.

AND THE INPUT IS THREE PRICES, NOT ONE. "Prompt caching is enabled by default for supported OpenAI
models", and "For GPT-5.6 and later, cache writes cost 1.25x the standard, uncached input-token
rate" while reads "cost only 0.1x" (https://developers.openai.com/api/docs/guides/prompt-caching).
With no `prompt_cache_options` sent, the mode "Defaults to implicit" and the breakpoint goes at the
end of the latest user message (the Chat Completions reference,
https://developers.openai.com/api/reference/resources/chat) -- which here carries the title's pack,
so every stage-6 prompt is written whole, and attempt 2, whose user message runs on past attempt 1's,
is written again. The usage reports it inside `prompt_tokens`, as
`prompt_tokens_details.cache_write_tokens` ("The unadjusted number of prompt tokens written to
cache") beside `cached_tokens`, and this adapter read `prompt_tokens` alone, so the meter charged a
written prompt at the flat rate: about 11% of a default call unmetered. Both are carried to
`pricing.usd` now. Sending `mode: "explicit"` with no breakpoint would switch the write off, but the
parameter is documented only for "gpt-5.6 and later models" and an admin may type any model, so the
bill is read rather than a parameter guessed at. [M5.5 review cycle 1, M55-DBL-02, M55-METER-07,
M55-DOC-05]
"""

from __future__ import annotations

import json
from typing import Any

from spielplan.acquire import fetch
from spielplan.llm import client

URL = "https://api.openai.com/v1/chat/completions"

# https://platform.openai.com/docs/api-reference/models/list: free and unpaginated.
MODELS_URL = "https://api.openai.com/v1/models"

# §6.6's caption word for this provider's mechanism, which the admin read returns.
STRUCTURED_OUTPUT = "strict schema"

_STRICT_UNSUPPORTED = {"minItems", "maxItems", "minimum", "maximum",
                       "minLength", "maxLength", "pattern", "format",
                       "default"}

# THE TIER IS PART OF THE PRICE, AND THE REQUEST NAMES IT. With no `service_tier` a request is "auto",
# which "will be processed with the service tier configured in the Project settings"; "default" "will be
# processed with the standard pricing and performance for the selected model"; and the response carries
# "the `service_tier` value based on the processing mode actually used to serve the request. This
# response value may be different from the value set in the parameter" (the Chat Completions
# reference, https://developers.openai.com/api/reference/resources/chat). A project whose tier is Fast
# -- reported as "priority" (https://developers.openai.com/api/docs/guides/fast-mode) and priced
# "gpt-5.6-terra | $4.00 | $0.40 | $5.00 | $24.00", twice the Standard row
# (https://developers.openai.com/api/docs/pricing) -- was billed twice what the meter held for every
# call. So the request pins the standard tier the table prices, and the answer is metered at the tier
# the response names: the standard row for "default" (and for "flex", which bills half of it and so is
# over-read rather than guessed), and twice it for "priority" or any tier this table does not price,
# which is the dearest multiple the page publishes. A response that names none was served at the tier
# pinned. [M5.5 review cycle 2, M55-C2-METER-01, DBL-C2-01]
SERVICE_TIER = "default"
TIER_RATE = {"default": 1.0, "flex": 1.0}
FAST_RATE = 2.0

# The write-ahead ceiling's margin (`client.ceiling_input`): none on the count, which at 3.6 characters
# a token already reads more tokens than the four characters a token OpenAI's own tokenizer guidance
# gives English, and the standard rate, because the request pins the tier. [M5.5 review cycle 2,
# M55-CAP-C2-01]
CEILING_TOKENIZER = 1.0
CEILING_OVERHEAD = 0
CEILING_RATE = 1.0


def _strip_keywords(node: Any, drop: set[str]) -> Any:
    if isinstance(node, dict):
        return {k: _strip_keywords(v, drop) for k, v in node.items()
                if k not in drop}
    if isinstance(node, list):
        return [_strip_keywords(v, drop) for v in node]
    return node


def auth_headers(key: str) -> dict[str, str]:
    """The key as a bearer token, as the corpus sends it (`:233`)."""
    return {"Authorization": f"Bearer {key}"}


async def call(fetcher: fetch.Fetcher, key: str, model: str, system: str,
               user: str, schema: dict[str, Any],
               max_tokens: int) -> client.LLMResult:
    strict_schema = _strip_keywords(schema, _STRICT_UNSUPPORTED)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        # `max_completion_tokens` rather than the deprecated `max_tokens`:
        # the reasoning-capable models reject the latter outright.
        "max_completion_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": client.TOOL_NAME, "strict": True,
                            "schema": strict_schema},
        },
        "service_tier": SERVICE_TIER,
    }
    resp, data = await client.post(
        fetcher, URL, key=key, body=body,
        headers={**auth_headers(key), "content-type": "application/json"},
    )
    usage = data.get("usage")
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    tier = data.get("service_tier") or SERVICE_TIER
    answer = client.billed(
        "openai", model, resp, data, tokens_in=client.count(usage, "prompt_tokens", required=True),
        tokens_out=client.count(usage, "completion_tokens", required=True),
        cache_written=client.count(details, "cache_write_tokens") if details is not None else 0,
        cache_read=client.count(details, "cached_tokens") if details is not None else 0,
        rate=TIER_RATE.get(tier, FAST_RATE) if isinstance(tier, str) else FAST_RATE)
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not all(isinstance(c, dict) for c in choices):
        raise client.LLMError("response choices are not a list of objects", answer=answer)
    if not choices:
        raise client.LLMError(f"no choices in response: {client.shown(str(data), key)}",
                              answer=answer)
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise client.LLMError("response message is not an object", answer=answer)
    if message.get("refusal"):
        # A refusal is final whatever type the field arrived as: an object where the documented
        # string belonged used to raise `KeyError` out of the slice and put a refusal on the
        # queue's curve, billing it again on every walk. [M5.5 review cycle 1, M55-METER-04]
        refusal = message["refusal"]
        said = refusal if isinstance(refusal, str) else json.dumps(refusal)
        raise client.LLMError(f"refused: {client.shown(said, key)}", retryable=False, answer=answer)
    text = message.get("content") or ""
    if not isinstance(text, str):
        raise client.LLMError("response content is not text", answer=answer)
    if not text:
        # (This port: quoted through `shown`, a field a proxy's echo can carry the key in.
        # [M5.5 review cycle 2, M55-KEYS-C2-03])
        raise client.LLMError(
            f"empty content (finish_reason={client.shown(str(choice.get('finish_reason')), key)})",
            retryable=choice.get("finish_reason") == "length", answer=answer)
    if answer is None:
        raise client.LLMError(f"response usage did not read: {client.shown(str(usage), key)}")
    try:
        answer.payload = client._loads(text, key)
    except client.LLMError as exc:
        # Truncated JSON at the cap is billed like any other cut-off, and says so.
        raise client.LLMError(str(exc), answer=answer) from None
    return answer


def listed_models(data: dict[str, Any]) -> tuple[list[str], bool]:
    """The model ids the models list carries; it has no second page."""
    return [str(m.get("id")) for m in data.get("data") or [] if isinstance(m, dict)], False
