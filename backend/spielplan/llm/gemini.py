"""Gemini's generateContent, structured by `responseSchema`. Spec v2.1 §9, §8 stage 6.

Gemini takes an OpenAPI-subset schema under `generationConfig.responseSchema`: it keeps the
numeric bounds OpenAI's strict mode strips, rejects `additionalProperties`, and wants upper-cased
`type` names, so `_gemini_schema` projects `contract.EXTRACTION_SCHEMA` into that dialect and
M5.4's validator judges the answer exactly as it judges the other two.

THE KEY GOES IN THE `x-goog-api-key` HEADER, AND THIS IS THE ONE PROVIDER WHERE THAT IS A CHOICE.
§9 says it in so many words - "Gemini responseSchema + header-only API key so credentials never hit
logs" - because Gemini also documents a `?key=` query parameter, which reads as the simpler of
the two. It is the trap plan 2.5 names: a url with a key in it is written by httpx into one INFO
line per request (the line `push/send.py` installs a filter against for its own
credential), stored in `raw_document.url` beside the answer, and carried into the host key
`fetch_host_state` files. So the header form is ported as the corpus wrote it and must not be
"simplified" to the documented query form; `test_llm_adapters.py` reads what the fetcher and the
log actually recorded, and reads this module for any string that builds such a url.

PORT VERDICT: **ported with named changes** from `mdc/llm/client.py:116-132` and `:257-303`
(`_gemini_schema`, `_gemini`). Taken verbatim: `_gemini_schema`; the request body (`:260-269`);
the endpoint shape; the header and its comment (`:274-277`); the two refusals of an answer - no
candidates (final, naming `promptFeedback.blockReason`) and an empty candidate (retryable only on
`finishReason` "MAX_TOKENS") (`:280-291`); `modelVersion` as the model; and the billed-output sum
with its measurement (`:292-303`). What changed:

  1. **The model is quoted into the path.** The corpus interpolates it from its own `.env`; here
     it is whatever an admin typed on §6.6's card, and a `?` in it would open the query string -
     the one place the paragraph above says a credential must never go - while a `/` would address
     a different resource. Quoting keeps it one path segment by construction, and every model id
     Gemini publishes is letters, digits, dots and hyphens, which quoting leaves as they are.
  2. **The POST goes through `client.post`**, which maps a fetch failure and a documented 4xx
     envelope to `LLMError` with its status (client named change 5) and returns the decoded
     envelope; the answer carries the bytes, url and status the raw store keeps (client named
     change 3).
  3. **The header is `auth_headers`**, shared with the free models-list probe (decision 433), so
     the paid call and the test button cannot put the key in two places.
  4. **The usage is read first, typed, and carried by every error**: `client.count` and
     `client.billed`, so a refused 200 is metered at what it billed and a malformed member is an
     `LLMError` (client named changes 5 and 10). The sum and its comment are the corpus's.

`temperature: 0` IS PLAN B4's, ported from the corpus's measurements on 2.5 and 3.6, and it is kept
knowing what Google now says of it: "we strongly recommend keeping them at their default values for
Gemini 3.x models", naming looping as the failure below 1.0
(https://ai.google.dev/gemini-api/docs/troubleshooting). A loop runs to `maxOutputTokens` and is
billed as output; decision 436 meters that cut-off at what it billed and decision 325's cap bounds
it, so the risk is money inside the cap and not money outside it. Dropping the parameter is an owner
decision against plan B4, recorded as owed, and not a change made silently here.
[M5.5 review cycle 1, M55-DBL-07]

§6.6 captions this provider "responseSchema + batch"; `STRUCTURED_OUTPUT` says "responseSchema"
alone, because decision 338 ships no batch mode at M5 and a caption naming it would describe
something the install does not have. The register records that caption's amendment as owed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from spielplan.acquire import fetch
from spielplan.llm import client

BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# https://ai.google.dev/api/models#method:-models.list: free, paginated at 50 by default and at
# most 1000, so one page answers `client.probe`'s "is the model listed" for any key today.
MODELS_URL = f"{BASE}?pageSize=1000"

# §6.6's caption word for this provider's mechanism, which the admin read returns.
STRUCTURED_OUTPUT = "responseSchema"

# The statuses after which a Gemini attempt may have been billed with no answer back: none. Google's
# billing page says "If your request fails with a 400 or 500 error, you won't be charged for the
# tokens used" (https://ai.google.dev/gemini-api/docs/billing), its 504 included -- "The request didn't
# finish within the deadline" (https://ai.google.dev/gemini-api/docs/api-errors) -- so every status
# settles to zero, where `client.LOST_STATUS` keeps a 504 at its ceiling for the providers that say
# nothing of it. Leaving a Gemini 504 at the ceiling charged the household's cap for calls Google
# documents as free, and a day's outage could spend most of a month on them (decision 436 (3)).
# [M5.5 review cycle 1, M55-SPEND-04]
LOST_STATUS: frozenset[int] = frozenset()

# The write-ahead ceiling's margin (`client.ceiling_input`): none on the count, because "For Gemini
# models, a token is equivalent to about 4 characters" (https://ai.google.dev/gemini-api/docs/tokens)
# and `estimate_tokens` counts 3.6; no overhead beyond the schema the count already includes; and the
# standard rate, since Gemini's pricing names no account-level multiplier. [M5.5 review cycle 2,
# M55-CAP-C2-01]
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
    # credential in it ends up in logs, the http_cache table and the raw
    # store's `url` column.
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
    # - measured at 59% of billable output missing on this prompt, because the
    # answer is short and the reasoning is not.  OpenAI folds its reasoning
    # tokens into `completion_tokens` already; Gemini does not.
    # §9 states the same correction from the spend side - "Gemini bills thinking tokens as output
    # - counting visible JSON understates cost ~5x" - and `llm_call.tokens_out_billed` is this sum.
    # Read before the answer is judged, so a refused candidate carries it too (client named change
    # 10). `promptTokenCount` includes any `cachedContentTokenCount`, which Gemini bills at its
    # lower cached rate: charged here at the full input rate, which over-reads a cached prompt and
    # never under-reads one.
    answer = client.billed(
        "gemini", model, resp, data,
        tokens_in=client.count(usage, "promptTokenCount", required=True),
        tokens_out=(None if candidate_tokens is None or thought_tokens is None
                    else candidate_tokens + thought_tokens))
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list) or not all(isinstance(c, dict) for c in candidates):
        raise client.LLMError("response candidates are not a list of objects", answer=answer)
    if not candidates:
        # (This port: `blockReason` and `finishReason` below are quoted through `shown` like the
        # envelope beside them, fields a proxy's echo can carry the key in. [M5.5 review cycle 2,
        # M55-KEYS-C2-03])
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
    """The model ids on one page of the models list, and whether the list continues past it.
    Gemini names a model `models/<id>`; the id an admin types is the part after the slash."""
    ids = [str(m.get("name") or "").removeprefix("models/")
           for m in data.get("models") or [] if isinstance(m, dict)]
    return ids, bool(data.get("nextPageToken"))
