"""Anthropic's Messages API, structured by `output_config.format` (§9, decision 535).

`usage.output_tokens` already includes thinking. The JSON is the first `text` block, after any
thinking. Only `stop_reason` "end_turn" is an answer: a prefix of the tags is not the tags.
"""

from __future__ import annotations

from typing import Any

from spielplan.acquire import fetch
from spielplan.llm import client

URL = "https://api.anthropic.com/v1/messages"

# Free; up to 1000 per page, so one page answers `client.probe`.
MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"

# §6.6's caption word for this provider's mechanism.
STRUCTURED_OUTPUT = "structured outputs"

# US-only inference bills 1.1x; read off the envelope's `usage.inference_geo`, never pinned in the
# request (a workspace may refuse the geo). Over-meters older models by a tenth, the safe direction.
US_GEO = "us"
US_GEO_RATE = 1.1

# Write-ahead ceiling margins: the newer tokenizer yields ~30% more tokens, the structured-output
# system prompt is unpublished (804, the dearest forced-tool prompt, stands in for it), and the geo is
# unknown before the answer.
CEILING_TOKENIZER = 1.35
CEILING_OVERHEAD = 804
CEILING_RATE = US_GEO_RATE


def auth_headers(key: str) -> dict[str, str]:
    """The key in `x-api-key` and the API version pinned."""
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


async def call(fetcher: fetch.Fetcher, key: str, model: str, system: str,
               user: str, schema: dict[str, Any],
               max_tokens: int) -> client.LLMResult:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        # No `temperature`.  It is deprecated on the current Sonnet/Opus
        # models and sending it is a hard 400 that no retry can fix.
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": client.strict_schema(schema)}},
    }
    resp, data = await client.post(
        fetcher, URL, key=key, body=body,
        headers={**auth_headers(key), "content-type": "application/json"},
    )
    usage = data.get("usage")
    geo = usage.get("inference_geo") if isinstance(usage, dict) else None
    answer = client.billed(
        "anthropic", model, resp, data, tokens_in=client.count(usage, "input_tokens", required=True),
        tokens_out=client.count(usage, "output_tokens", required=True),
        rate=US_GEO_RATE if geo == US_GEO else 1.0)
    blocks = data.get("content")
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        raise client.LLMError("response content is not a list of blocks", answer=answer)
    if answer is None:
        raise client.LLMError(f"response usage did not read: {client.shown(str(usage), key)}")
    stop = data.get("stop_reason")
    if stop == "refusal" and not blocks:
        # A refusal before any output is not billed, though `usage` carries counts; settle it to zero.
        answer.tokens_in = answer.tokens_out = 0
    text = _first_text(blocks)
    if stop != "end_turn":
        # A refusal is final; a cut-off is billed and worth the one retry.
        raise client.LLMError(f"no answer (stop_reason={client.shown(str(stop), key)}):"
                              f" {client.shown(text, key)}",
                              retryable=stop == "max_tokens", answer=answer)
    try:
        answer.payload = client._loads(text, key)
    except client.LLMError as exc:
        raise client.LLMError(str(exc), answer=answer) from None
    return answer


def _first_text(blocks: list[dict[str, Any]]) -> str:
    for b in blocks:
        if b.get("type") == "text":
            text = b.get("text")
            return text if isinstance(text, str) else ""
    return ""


def listed_models(data: dict[str, Any]) -> tuple[list[str], bool]:
    """The model ids on one page of the models list, and whether the list continues past it."""
    ids = [str(m.get("id")) for m in data.get("data") or [] if isinstance(m, dict)]
    return ids, bool(data.get("has_more"))
