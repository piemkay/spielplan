"""Anthropic's Messages API, structured by forced tool-use (§9).

`usage.output_tokens` already includes thinking. Only `stop_reason` "tool_use" is an answer: a
cut-off ends inside the tool call, and a prefix of the tags is not the tags.
"""

from __future__ import annotations

from typing import Any

from spielplan.acquire import fetch
from spielplan.llm import client

URL = "https://api.anthropic.com/v1/messages"

# Free; up to 1000 per page, so one page answers `client.probe`.
MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"

# §6.6's caption word for this provider's mechanism.
STRUCTURED_OUTPUT = "forced tool-use"

# Models that answer a forced `tool_choice` with a 400 on every request.
FORCED_TOOL_REFUSED = ("claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1")

# US-only inference bills 1.1x; read off the envelope's `usage.inference_geo`, never pinned in the
# request (a workspace may refuse the geo). Over-meters older models by a tenth, the safe direction.
US_GEO = "us"
US_GEO_RATE = 1.1

# Write-ahead ceiling margins: the newer tokenizer yields ~30% more tokens, the dearest tool-use
# system prompt is 804 tokens, and the geo is unknown before the answer.
CEILING_TOKENIZER = 1.35
CEILING_OVERHEAD = 804
CEILING_RATE = US_GEO_RATE


def refuses_forced_tool(model: str) -> bool:
    """Whether `model` is one of `FORCED_TOOL_REFUSED`, under `pricing.price_for`'s name-boundary rule."""
    return any(model == name or model.startswith(name + "-") for name in FORCED_TOOL_REFUSED)


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
        "tools": [{
            "name": client.TOOL_NAME,
            "description": "Return the extracted DNA tags.",
            "input_schema": schema,
        }],
        "tool_choice": {"type": "tool", "name": client.TOOL_NAME},
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
    payload = None
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == client.TOOL_NAME:
            payload = block.get("input")
            break
    if payload is None:
        # A forced tool call that came back as prose means the model hit the
        # output cap mid-argument, or refused.  Both are worth naming.
        text = _first_text(blocks)
        raise client.LLMError(f"no tool_use block (stop_reason={client.shown(str(stop), key)}):"
                              f" {client.shown(text, key)}",
                              retryable=stop == "max_tokens", answer=answer)
    if stop != "tool_use":
        # A prefix of the answer is not the answer.
        raise client.LLMError(
            f"the tool_use block is incomplete (stop_reason={client.shown(str(stop), key)})",
            retryable=stop == "max_tokens", answer=answer)
    answer.payload = payload
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
