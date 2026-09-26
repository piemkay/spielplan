"""OpenAI's Chat Completions API, structured by a strict JSON schema (§9).

The bill is `completion_tokens` as reported (reasoning already included). Prompt-cache writes and
reads are priced apart: every stage-6 prompt is written to the cache by default.
"""

from __future__ import annotations

import json
from typing import Any

from spielplan.acquire import fetch
from spielplan.llm import client

URL = "https://api.openai.com/v1/chat/completions"

# Free and unpaginated.
MODELS_URL = "https://api.openai.com/v1/models"

# §6.6's caption word for this provider's mechanism.
STRUCTURED_OUTPUT = "strict schema"

_STRICT_UNSUPPORTED = {"minItems", "maxItems", "minimum", "maximum",
                       "minLength", "maxLength", "pattern", "format",
                       "default"}

# Pins the standard tier the table prices; the answer is metered at the tier the response names
# (2x for "priority" or any unpriced tier).
SERVICE_TIER = "default"
TIER_RATE = {"default": 1.0, "flex": 1.0}
FAST_RATE = 2.0

# No margin: 3.6 characters a token already over-counts, and the tier is pinned.
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
    """The key as a bearer token."""
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
        # A refusal is final whatever type the field arrived as.
        refusal = message["refusal"]
        said = refusal if isinstance(refusal, str) else json.dumps(refusal)
        raise client.LLMError(f"refused: {client.shown(said, key)}", retryable=False, answer=answer)
    text = message.get("content") or ""
    if not isinstance(text, str):
        raise client.LLMError("response content is not text", answer=answer)
    if not text:
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
