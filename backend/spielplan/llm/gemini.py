"""Gemini's generateContent, structured by `responseSchema` (§9).

The key goes in `x-goog-api-key`, never the documented `?key=` form: urls reach logs and the raw
store. `temperature: 0` is kept deliberately despite Google's advice (plan B4).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from spielplan.acquire import fetch
from spielplan.llm import client

BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Free; up to 1000 per page, so one page answers `client.probe`.
MODELS_URL = f"{BASE}?pageSize=1000"

# §6.6's caption word for this provider's mechanism.
STRUCTURED_OUTPUT = "responseSchema"

# None: Google bills no failed request, 504 included, so every error settles to zero.
LOST_STATUS: frozenset[int] = frozenset()

# No margin: Gemini averages ~4 characters a token against our 3.6, and has no rate multiplier.
CEILING_TOKENIZER = 1.0
CEILING_OVERHEAD = 0
CEILING_RATE = 1.0


def _gemini_schema(node: Any) -> Any:
    """OpenAPI subset: no additionalProperties, upper-case type names."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "additionalProperties":
                continue
            if k == "type" and isinstance(v, str):
                out[k] = v.upper()
                continue
            out[k] = _gemini_schema(v)
        if "properties" in out and "propertyOrdering" not in out:
            out["propertyOrdering"] = list(out["properties"])
        return out
    if isinstance(node, list):
        return [_gemini_schema(v) for v in node]
    return node


def auth_headers(key: str) -> dict[str, str]:
    # The key goes in a header, never the query string - a URL with a
    # credential in it ends up in logs and the raw store's `url` column.
    return {"x-goog-api-key": key}


async def call(fetcher: fetch.Fetcher, key: str, model: str, system: str,
               user: str, schema: dict[str, Any],
               max_tokens: int) -> client.LLMResult:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(schema),
        },
    }
    url = f"{BASE}/{quote(model, safe='')}:generateContent"
    resp, data = await client.post(
        fetcher, url, key=key, body=body,
        headers={**auth_headers(key), "content-type": "application/json"},
        lost=LOST_STATUS,
    )
    usage = data.get("usageMetadata")
    candidate_tokens = client.count(usage, "candidatesTokenCount")
    thought_tokens = client.count(usage, "thoughtsTokenCount")
    # `thoughtsTokenCount` is billed as output but reported apart from
    # `candidatesTokenCount`, so counting only the latter understates the bill
    # - measured at 59% of billable output missing on this prompt. Cached prompt tokens
    # are charged at the full input rate: over-reads, never under-reads.
    answer = client.billed(
        "gemini", model, resp, data,
        tokens_in=client.count(usage, "promptTokenCount", required=True),
        tokens_out=(None if candidate_tokens is None or thought_tokens is None
                    else candidate_tokens + thought_tokens))
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list) or not all(isinstance(c, dict) for c in candidates):
        raise client.LLMError("response candidates are not a list of objects", answer=answer)
    if not candidates:
        feedback = data.get("promptFeedback")
        block = ((feedback.get("blockReason") if isinstance(feedback, dict) else None)
                 or data)
        raise client.LLMError(f"no candidates: {client.shown(str(block), key)}", retryable=False,
                              answer=answer)
    cand = candidates[0]
    content = cand.get("content") or {}
    parts = (content.get("parts") if isinstance(content, dict) else None) or []
    if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
        raise client.LLMError("response parts are not a list of objects", answer=answer)
    text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))
    if not text:
        raise client.LLMError(
            f"empty candidate (finishReason={client.shown(str(cand.get('finishReason')), key)})",
            retryable=cand.get("finishReason") == "MAX_TOKENS", answer=answer)
    if answer is None:
        raise client.LLMError(f"response usage did not read: {client.shown(str(usage), key)}")
    try:
        answer.payload = client._loads(text, key)
    except client.LLMError as exc:
        # JSON cut off at `maxOutputTokens` is billed like any other cut-off, and says so.
        raise client.LLMError(str(exc), answer=answer) from None
    return answer


def listed_models(data: dict[str, Any]) -> tuple[list[str], bool]:
    """The model ids on one page of the list, as the part after `models/`, and whether it continues."""
    ids = [str(m.get("name") or "").removeprefix("models/")
           for m in data.get("models") or [] if isinstance(m, dict)]
    return ids, bool(data.get("nextPageToken"))
