"""Anthropic's Messages API, structured by forced tool-use. Spec v2.1 §9, §8 stage 6.

Anthropic has no `response_format`, so the contract is declared as the `input_schema` of one tool
and `tool_choice` forces the model to call it; the answer arrives as that tool call's parsed input.
The schema is `contract.EXTRACTION_SCHEMA` exactly as it stands - Anthropic's tool schema takes
JSON Schema with nothing stripped - and what the model puts in it is judged by M5.4's validator
like any other provider's answer (§9: "the guarantee is the validator").

PORT VERDICT: **ported with named changes** from `mdc/llm/client.py:160-210` (`_anthropic` and
`_first_text`). Taken verbatim: the request body with its no-`temperature` comment (`:163-179`),
the endpoint, the `anthropic-version` pin, the read of the `tool_use` block named `TOOL_NAME`
(`:186-191`), and the named error for a forced tool call answered as prose, retryable only on
`max_tokens` (`:192-198`). What changed:

  1. **The tool's description names DNA tags** ("Return the extracted DNA tags.") where the corpus
     said aspects, with `client.TOOL_NAME`'s rename: the model reads the description.
  2. **The POST goes through `client.post`**, which maps a fetch failure and a documented 4xx
     envelope to `LLMError` with its status (client named change 5) and returns the decoded
     envelope; the answer carries the bytes, url and status the raw store keeps (client named
     change 3).
  3. **The two auth headers are `auth_headers`**, shared with the free models-list probe
     (decision 433), so the paid call and the test button cannot put the key in two places.
  4. **The usage is read first, typed, and carried by every error** (`:199-203` read it last, with
     a bare `int()`): `client.count` and `client.billed`, so a refused 200 is metered at what it
     billed and a malformed member is an `LLMError` (client named changes 5 and 10).
  5. **The answer is the block at `stop_reason` "tool_use" and nothing else.** See below.

THE BILL IS `usage.output_tokens` AS REPORTED, AND IT HOLDS THE THINKING. This paragraph used to
say the request never thinks, because forced tool use is unsupported with extended thinking. That
is true of MANUAL extended thinking and false of the default model: "On Claude Opus 5.5, Claude
Opus 5, Claude Sonnet 5 ... thinking is already on and needs no configuration", and "Adaptive
thinking, including on models where thinking is on by default, supports forced tool use, except on
Claude Opus 5.5, Claude Fable 5.1, and Claude Mythos 5.1"
(https://platform.claude.com/docs/en/build-with-claude/thinking). The thinking is "billed as output
tokens, even when the thinking text isn't returned to you, and they count toward `max_tokens`" (the
same page), and "`output_tokens` remains the inclusive, authoritative total used for billing", with
`output_tokens_details.thinking_tokens` "a read-only breakdown for observability"
(https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost). So
`output_tokens` is taken as it comes -- adding the breakdown Gemini-style would bill the thinking
twice -- and the thinking block in front of the tool call is passed over by type. Because the
thinking spends the same `client.MAX_OUTPUT_TOKENS`, a cut-off is an ordinary outcome here and not
a corner. [M5.5 review cycle 1, M55-METER-06, M55-DBL-03]

A CUT-OFF ENDS IN THE TOOL CALL ITSELF. Under a forced tool the model "will not emit a natural
language response or explanation before tool_use"
(https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools), so a response cut at
the cap is not the prose the corpus's branch expected: "the truncated response contains an
incomplete tool use block" (https://platform.claude.com/docs/en/build-with-claude/handling-stop-
reasons). The corpus returned whatever `input` that block held, and a prefix of tags that verified
would have been written as the whole tier. So the call is an answer only at `stop_reason`
"tool_use", and any other stop is the named error the corpus raised for prose, retryable only on
`max_tokens` (plan B2) -- a `refusal` stop included, which that page documents as "a normal HTTP 200
response, not an error". Either way the error carries the envelope's bill (client named change 10),
which for a refusal that came before any output is nothing: see `call`.
[M5.5 review cycle 1, M55-DBL-01, M55-METER-01, M55-DBL-08]

THREE CURRENT MODELS REFUSE THE MECHANISM ITSELF. "Claude Opus 5.5, Claude Fable 5.1, and Claude
Mythos 5.1 don't support forced tool use" and answer a forced `tool_choice` with a 400 on every
request (https://platform.claude.com/docs/en/api/errors, "Forced tool use not supported"). Such a
400 fails a title for good (decision 431), so a model an admin picked from Anthropic's own list would
have failed every title in the queue one by one, each needing a retry once the model was changed.
`refuses_forced_tool` names them, and `llm/spend.extraction_plan` parks on one before anything is
sent, in the shape of its unpriced-model park. [M5.5 review cycle 1, M55-DBL-04]
"""

from __future__ import annotations

from typing import Any

from spielplan.acquire import fetch
from spielplan.llm import client

URL = "https://api.anthropic.com/v1/messages"

# https://docs.anthropic.com/en/api/models-list: free, paginated at 20 by default and at most 1000,
# so one page answers `client.probe`'s "is the model listed" for any account that exists today.
MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"

# §6.6's caption word for this provider's mechanism, which the admin read returns.
STRUCTURED_OUTPUT = "forced tool-use"

# The models that answer that mechanism with a 400 on every request. See the module docstring.
FORCED_TOOL_REFUSED = ("claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1")

# WHERE INFERENCE RAN IS PART OF THE PRICE. A request with no `inference_geo` takes the workspace's
# `default_inference_geo`, and organisations that had opted out of global routing were "automatically
# configured with `allowed_inference_geos: ["us"]` and `default_inference_geo: "us"`"; "The response
# `usage` object includes an `inference_geo` field indicating where inference ran", and on "Claude 4.6
# and later models: US-only inference (`inference_geo: "us"`) is priced at 1.1x the standard rate across
# all token pricing categories" (https://platform.claude.com/docs/en/manage-claude/data-residency). So
# the answer carries 1.1 when the envelope says "us". Not pinned in the request: a workspace whose
# `allowed_inference_geos` leaves out the geo a request names "returns an error", and "global" would
# refuse exactly the migrated workspaces. On an older model -- which "Don't support `inference_geo`" and
# bills the standard rate -- a "us" read as 1.1 over-meters by a tenth, the direction a meter may err
# in (decision 436). [M5.5 review cycle 2, M55-C2-METER-01, DBL-C2-01]
US_GEO = "us"
US_GEO_RATE = 1.1

# The write-ahead ceiling's margin (`client.ceiling_input`), each figure from the pricing page
# (https://platform.claude.com/docs/en/about-claude/pricing). The newer tokenizer "produces
# approximately 30% more tokens for the same text" against the page's "1 token is approximately 4
# characters", about 17% above `estimate_tokens`' 3.6, and 1.35 leaves a text that tokenizes a sixth
# denser still inside it. The overhead is the dearest "Tool use system prompt tokens: any, tool" the page
# lists, Claude Opus 4.7's 804 (Sonnet 5's is 474), because an admin may name any model. And the rate is
# US-only inference's, because the workspace default is not visible before the answer.
# [M5.5 review cycle 2, M55-CAP-C2-01, M55-C2-METER-03]
CEILING_TOKENIZER = 1.35
CEILING_OVERHEAD = 804
CEILING_RATE = US_GEO_RATE


def refuses_forced_tool(model: str) -> bool:
    """Whether `model` is one of `FORCED_TOOL_REFUSED`, under `pricing.price_for`'s name-boundary rule:
    a dated snapshot of one is refused with it, and `claude-opus-5` is not refused for sharing a prefix
    with `claude-opus-5-5`."""
    return any(model == name or model.startswith(name + "-") for name in FORCED_TOOL_REFUSED)


def auth_headers(key: str) -> dict[str, str]:
    """The key in `x-api-key` and the API version pinned, as the corpus sends both (`:183`)."""
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


async def call(fetcher: fetch.Fetcher, key: str, model: str, system: str,
               user: str, schema: dict[str, Any],
               max_tokens: int) -> client.LLMResult:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        # No `temperature`.  It is deprecated on the current Sonnet/Opus
        # models and sending it is a hard 400 that no retry can fix - the
        # same class of trap as OpenAI's `max_tokens` -> `max_completion_
        # tokens` rename.  Determinism matters little here anyway: the tool
        # schema constrains the shape, and the validator constrains the rest.
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
        # A REFUSAL BEFORE ANY OUTPUT IS SETTLED TO ZERO, its envelope still kept (decision 436 as
        # amended): "You are not billed for a refusal that arrives before any output. `content` is
        # empty, and token counts appear in `usage` but are not charged", while "A mid-stream refusal
        # bills the input tokens and the output already streamed at normal rates"
        # (https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback). So an empty
        # refusal is the one usage block this adapter reads and must not bill, and a refusal that
        # carries any block came after output and bills what it reported. [M5.5 review cycle 1,
        # M55-DBL-08]
        answer.tokens_in = answer.tokens_out = 0
    payload = None
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == client.TOOL_NAME:
            payload = block.get("input")
            break
    if payload is None:
        # A forced tool call that came back as prose means the model hit the
        # output cap mid-argument, or refused.  Both are worth naming.
        # (This port: `stop_reason` is quoted through `shown` like the text beside it, because a proxy
        # that echoes the request can put the key in any field. [M5.5 review cycle 2, M55-KEYS-C2-03])
        text = _first_text(blocks)
        raise client.LLMError(f"no tool_use block (stop_reason={client.shown(str(stop), key)}):"
                              f" {client.shown(text, key)}",
                              retryable=stop == "max_tokens", answer=answer)
    if stop != "tool_use":
        # The forced call's block is there and the stop says it was not finished. See the module
        # docstring: a prefix of the answer is not the answer.
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
