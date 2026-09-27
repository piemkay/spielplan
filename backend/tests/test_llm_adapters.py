"""The three provider adapters through a real `Fetcher` over `httpx.MockTransport` (§9). Envelopes
are the providers' published ones, never written to agree with the parser. No database."""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import fetch
from spielplan.dna.verify import Vocabulary, verify_tags
from spielplan.llm import anthropic, client, contract, gemini, openai
from tests.test_dna_verify import _NoVerdicts

LLM_PACKAGE = Path(__file__).resolve().parents[1] / "spielplan" / "llm"

# A key no response, url or log line could contain by accident.
KEY = "sk-test-KEY-4f1c9a0b7e"

# The corpus's DEFAULT_MODELS, the models whose envelopes the corpus measured.
MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra", "gemini": "gemini-3.7-flash"}

URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent",
}

TAGS = [{"term": "mood.bleak", "salience": 3, "source": "imdb:1",
         "quote": "a bleak and unforgiving portrait of a town"}]


def _anthropic_ok(payload, *, usage=None):
    """https://docs.anthropic.com/en/api/messages and
    https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use:
    `stop_reason` "tool_use"."""
    return {
        "id": "msg_01Aq9w938a90dw8q", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "tool_use", "id": "toolu_01A09q90qw90lq917835lq9",
                     "name": "emit_dna", "input": payload}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": usage or {"input_tokens": 2095, "output_tokens": 503},
    }


def _anthropic_prose(stop_reason):
    return {
        "id": "msg_01Aq9w938a90dw8r", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "I would rather describe this film in prose."}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 2095, "output_tokens": 8000},
    }


def _anthropic_cut_off_mid_call(payload):
    """https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons:
    an incomplete `tool_use` block."""
    return {
        "id": "msg_01Aq9w938a90dw8s", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "EosnCkYICxIMMb3LzNrMu"},
                    {"type": "tool_use", "id": "toolu_01A09q90qw90lq917835lq8", "name": "emit_dna",
                     "input": payload}],
        "stop_reason": "max_tokens", "stop_sequence": None,
        "usage": {"input_tokens": 2095, "output_tokens": 8000,
                  "output_tokens_details": {"thinking_tokens": 7400}},
    }


def _anthropic_refusal():
    """https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback:
    empty and not charged."""
    return {
        "id": "msg_01Aq9w938a90dw8t", "type": "message", "role": "assistant",
        "model": "claude-opus-5", "content": [], "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber",
                         "explanation": "This request was declined because it could enable cyber harm."},
        "stop_sequence": None, "usage": {"input_tokens": 2095, "output_tokens": 0},
    }


def _anthropic_refusal_after_output():
    """"A mid-stream refusal bills the input tokens and the output already streamed at normal rates"."""
    return {
        **_anthropic_refusal(),
        "id": "msg_01Aq9w938a90dw8u",
        "content": [{"type": "thinking", "thinking": "", "signature": "EosnCkYICxIMMb3LzNrMv"}],
        "usage": {"input_tokens": 2095, "output_tokens": 412,
                  "output_tokens_details": {"thinking_tokens": 412}},
    }


def _openai_ok(content, *, usage=None, finish_reason="stop", refusal=None, choices=True):
    """https://platform.openai.com/docs/api-reference/chat/object;
    reasoning is inside `completion_tokens`."""
    message = {"role": "assistant", "content": content, "refusal": refusal, "annotations": []}
    return {
        "id": "chatcmpl-B9MHDbslfkBeAs8l4bebGdFOJ6PeG", "object": "chat.completion",
        "created": 1741570283, "model": "gpt-5.6-terra",
        "choices": [{"index": 0, "message": message, "logprobs": None,
                     "finish_reason": finish_reason}] if choices else [],
        "usage": usage or {
            "prompt_tokens": 1117, "completion_tokens": 3812, "total_tokens": 4929,
            "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 3072, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0,
                                          "rejected_prediction_tokens": 0},
        },
        "service_tier": "default", "system_fingerprint": "fp_fc9f1d7035",
    }


def _gemini_ok(text, *, usage=None, finish_reason="STOP", parts=True):
    """https://ai.google.dev/api/generate-content#v1beta.GenerateContentResponse;
    thoughts bill as output."""
    content = {"parts": [{"text": text}], "role": "model"} if parts else {"role": "model"}
    return {
        "candidates": [{"content": content, "finishReason": finish_reason, "index": 0}],
        "usageMetadata": usage or {"promptTokenCount": 14230, "candidatesTokenCount": 1600,
                                   "thoughtsTokenCount": 2300, "totalTokenCount": 18130},
        "modelVersion": "gemini-3.7-flash", "responseId": "mbLaaMTDOJu7qtsPlcDZ6A0",
    }


def _gemini_blocked():
    """https://ai.google.dev/api/generate-content#PromptFeedback"""
    return {
        "promptFeedback": {"blockReason": "SAFETY", "safetyRatings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH"}]},
        "usageMetadata": {"promptTokenCount": 14230, "totalTokenCount": 14230},
        "modelVersion": "gemini-3.7-flash",
    }


# The error envelopes each provider documents for a 4xx.
ANTHROPIC_401 = {  # https://docs.anthropic.com/en/api/errors
    "type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    "request_id": "req_011CSHoEeqs5C35K2UUqR7Fy",
}
ANTHROPIC_529 = {  # https://docs.anthropic.com/en/api/errors: "529 - overloaded_error"
    "type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"},
}
OPENAI_401 = {  # https://platform.openai.com/docs/guides/error-codes
    "error": {"message": "Incorrect API key provided: sk-test-****7e. You can find your API key "
                         "at https://platform.openai.com/account/api-keys.",
              "type": "invalid_request_error", "param": None, "code": "invalid_api_key"},
}
GEMINI_400 = {  # https://ai.google.dev/gemini-api/docs/troubleshooting
    "error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
              "status": "INVALID_ARGUMENT",
              "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                           "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]},
}
GEMINI_404 = {  # the same page's NOT_FOUND row; `mdc/config.py:152-154` met it on 2.5-flash
    "error": {"code": 404, "message": "models/gemini-2.5-flash is no longer available to new users.",
              "status": "NOT_FOUND"},
}

OK = {
    "anthropic": lambda payload: _anthropic_ok(payload),
    "openai": lambda payload: _openai_ok(json.dumps(payload)),
    "gemini": lambda payload: _gemini_ok(json.dumps(payload)),
}


class _Clock:
    """The token bucket still sees the time it waited pass."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


def _serve(status, body, seen):
    content = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=content,
                              headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def _fetcher(status, body, seen):
    clock = _Clock()
    return fetch.Fetcher(transport=_serve(status, body, seen), clock=clock, sleep=clock.sleep,
                         jitter=lambda low, high: 0.0)


async def _complete(provider, body, *, status=200, seen=None, model=None, key=KEY):
    seen = [] if seen is None else seen
    async with _fetcher(status, body, seen) as fetcher:
        return await client.complete(
            fetcher, provider=provider, key=key, model=MODELS[provider] if model is None else model,
            system="SYSTEM PROMPT", user="USER PROMPT", schema=contract.EXTRACTION_SCHEMA,
        )


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_each_adapter_posts_once_through_the_fetcher_with_the_key_in_its_documented_header(
    provider, caplog
):
    """The key travels only in the documented header; the url carries no query and the INFO line no key."""
    caplog.set_level(logging.INFO, logger="httpx")
    seen: list[httpx.Request] = []
    result = await _complete(provider, OK[provider]({"tags": TAGS}), seen=seen)

    assert len(seen) == 1, [str(r.url) for r in seen]
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == URLS[provider]
    assert KEY not in str(request.url)
    assert request.url.query == b""
    assert request.headers["user-agent"] == fetch.USER_AGENT
    if provider == "anthropic":
        assert request.headers["x-api-key"] == KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
    elif provider == "openai":
        assert request.headers["authorization"] == f"Bearer {KEY}"
    else:
        assert request.headers["x-goog-api-key"] == KEY
        assert "authorization" not in request.headers

    assert result.request_url == URLS[provider]
    assert result.http_status == 200
    assert result.content == json.dumps(OK[provider]({"tags": TAGS})).encode("utf-8")
    assert json.loads(result.content) == result.raw

    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert any(URLS[provider] in line for line in lines), lines
    assert not [line for line in lines if KEY in line], lines


async def test_the_anthropic_request_forces_the_one_tool_and_sends_no_temperature():
    """`temperature` is a hard 400 on the current Sonnet/Opus models that no retry can fix."""
    seen: list[httpx.Request] = []
    await _complete("anthropic", _anthropic_ok({"tags": TAGS}), seen=seen)
    body = _body(seen[0])

    assert body["tools"] == [{"name": "emit_dna", "description": "Return the extracted DNA tags.",
                              "input_schema": contract.EXTRACTION_SCHEMA}]
    assert body["tool_choice"] == {"type": "tool", "name": "emit_dna"}
    assert "temperature" not in body
    assert body["max_tokens"] == client.MAX_OUTPUT_TOKENS == 8000
    assert body["system"] == "SYSTEM PROMPT"
    assert body["messages"] == [{"role": "user", "content": "USER PROMPT"}]


async def test_the_openai_request_is_strict_and_uses_max_completion_tokens():
    """The reasoning-capable models reject `max_tokens` outright."""
    seen: list[httpx.Request] = []
    await _complete("openai", _openai_ok(json.dumps({"tags": TAGS})), seen=seen)
    body = _body(seen[0])

    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "emit_dna", "strict": True,
                        "schema": openai._strip_keywords(contract.EXTRACTION_SCHEMA,
                                                         openai._STRICT_UNSUPPORTED)},
    }
    assert body["max_completion_tokens"] == 8000
    assert "max_tokens" not in body
    assert body["messages"] == [{"role": "system", "content": "SYSTEM PROMPT"},
                                {"role": "user", "content": "USER PROMPT"}]


async def test_the_gemini_request_carries_the_response_schema_under_generation_config():
    seen: list[httpx.Request] = []
    await _complete("gemini", _gemini_ok(json.dumps({"tags": TAGS})), seen=seen)
    body = _body(seen[0])

    assert body == {
        "systemInstruction": {"parts": [{"text": "SYSTEM PROMPT"}]},
        "contents": [{"role": "user", "parts": [{"text": "USER PROMPT"}]}],
        "generationConfig": {
            "temperature": 0, "maxOutputTokens": 8000, "responseMimeType": "application/json",
            "responseSchema": gemini._gemini_schema(contract.EXTRACTION_SCHEMA),
        },
    }


async def test_a_gemini_model_name_stays_one_path_segment_and_never_opens_a_query():
    """The model comes from an admin's text field: a `?` would open the query, a `/` another resource."""
    seen: list[httpx.Request] = []
    with pytest.raises(client.LLMError):
        await _complete("gemini", GEMINI_404, status=404, seen=seen,
                        model="gemini-3.7-flash?key=x/../y")
    assert seen[0].url.query == b""
    assert seen[0].url.raw_path == (
        b"/v1beta/models/gemini-3.7-flash%3Fkey%3Dx%2F..%2Fy:generateContent"), seen[0].url
    assert KEY not in str(seen[0].url)


def test_strip_keywords_removes_exactly_the_corpus_set():
    """Strict mode rejects the array-length and numeric-range keywords at every depth."""
    dropped = openai._STRICT_UNSUPPORTED
    assert dropped == {
        "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern",
        "format", "default",
    }
    schema = {
        "type": "object", "required": ["a"], "additionalProperties": False,
        "properties": {"a": {"type": "array", "minItems": 1, "maxItems": 3, "items": [
            {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
            {"type": "string", "minLength": 1, "maxLength": 9, "pattern": "x", "format": "uri",
             "description": "kept"},
        ]}},
    }
    assert openai._strip_keywords(schema, openai._STRICT_UNSUPPORTED) == {
        "type": "object", "required": ["a"], "additionalProperties": False,
        "properties": {"a": {"type": "array", "items": [
            {"type": "integer"}, {"type": "string", "description": "kept"},
        ]}},
    }


def test_the_adapters_name_their_structured_output_mechanisms():
    """Decision 338: batch mode does not ship, so "batch" would describe something absent."""
    assert anthropic.STRUCTURED_OUTPUT == "forced tool-use"
    assert openai.STRUCTURED_OUTPUT == "strict schema"
    assert gemini.STRUCTURED_OUTPUT == "responseSchema"
    assert client.PROVIDERS == ("anthropic", "openai", "gemini")


async def test_the_gemini_bill_counts_its_thinking_tokens_as_output():
    """§9's ~5x: thoughts bill as output and never appear in the response."""
    thought = await _complete("gemini", _gemini_ok(json.dumps({"tags": TAGS})))
    assert (thought.tokens_in, thought.tokens_out) == (14230, 3900)
    assert thought.model == "gemini-3.7-flash"

    plain = await _complete("gemini", _gemini_ok(json.dumps({"tags": TAGS}), usage={
        "promptTokenCount": 14230, "candidatesTokenCount": 1600, "totalTokenCount": 15830}))
    assert plain.tokens_out == 1600


async def test_openai_completion_tokens_are_used_as_they_come():
    """Adding `reasoning_tokens` to `completion_tokens` would bill the reasoning twice."""
    result = await _complete("openai", _openai_ok(json.dumps({"tags": TAGS})))
    assert (result.tokens_in, result.tokens_out) == (1117, 3812)
    assert result.model == "gpt-5.6-terra"


async def test_anthropic_tokens_come_from_its_usage_block():
    result = await _complete("anthropic", _anthropic_ok({"tags": TAGS}))
    assert (result.tokens_in, result.tokens_out) == (2095, 503)
    assert result.payload == {"tags": TAGS}
    assert result.provider == "anthropic" and result.model == "claude-sonnet-5"


BRANCHES = [
    # (id, provider, status, envelope, retryable, fragment, requests)
    ("anthropic-prose", "anthropic", 200, _anthropic_prose("end_turn"), False,
     "stop_reason=end_turn", 1),
    ("anthropic-cut-off", "anthropic", 200, _anthropic_prose("max_tokens"), True,
     "stop_reason=max_tokens", 1),
    # A prefix of tags that verifies would be written as the whole tier.
    ("anthropic-cut-off-mid-call", "anthropic", 200, _anthropic_cut_off_mid_call({"tags": TAGS}),
     True, "stop_reason=max_tokens", 1),
    ("anthropic-refusal", "anthropic", 200, _anthropic_refusal(), False, "stop_reason=refusal", 1),
    ("anthropic-401", "anthropic", 401, ANTHROPIC_401, False, "authentication_error", 1),
    ("anthropic-529", "anthropic", 529, ANTHROPIC_529, True, "HTTP 529", 1),
    ("openai-no-choices", "openai", 200, _openai_ok("", choices=False), True, "no choices", 1),
    ("openai-refusal", "openai", 200,
     _openai_ok(None, refusal="I'm sorry, I cannot assist with that request."), False,
     "refused: I'm sorry", 1),
    ("openai-empty-length", "openai", 200, _openai_ok("", finish_reason="length"), True,
     "finish_reason=length", 1),
    ("openai-empty-stop", "openai", 200, _openai_ok("", finish_reason="stop"), False,
     "finish_reason=stop", 1),
    ("openai-not-json", "openai", 200, _openai_ok("Here are the tags you asked for."), True,
     "not JSON", 1),
    ("openai-401", "openai", 401, OPENAI_401, False, "invalid_api_key", 1),
    ("gemini-blocked", "gemini", 200, _gemini_blocked(), False, "no candidates: SAFETY", 1),
    ("gemini-cut-off", "gemini", 200, _gemini_ok("", finish_reason="MAX_TOKENS", parts=False), True,
     "finishReason=MAX_TOKENS", 1),
    ("gemini-empty-safety", "gemini", 200, _gemini_ok("", finish_reason="SAFETY", parts=False),
     False, "finishReason=SAFETY", 1),
    ("gemini-not-json", "gemini", 200, _gemini_ok("tags: none"), True, "not JSON", 1),
    ("gemini-400", "gemini", 400, GEMINI_400, False, "INVALID_ARGUMENT", 1),
    ("gemini-404", "gemini", 404, GEMINI_404, False, "NOT_FOUND", 1),
    # ONE request: a paid POST the provider may have generated is never re-sent (decision 436).
    ("openai-503-exhausted", "openai", 503, {"error": {"message": "overloaded"}}, True,
     "HTTP 503", 1),
    ("gemini-envelope-not-json", "gemini", 200, b"<html>502 Bad Gateway</html>", True,
     "envelope was not JSON", 1),
]


@pytest.mark.parametrize(("provider", "status", "envelope", "retryable", "fragment", "requests"),
                         [b[1:] for b in BRANCHES], ids=[b[0] for b in BRANCHES])
async def test_every_error_branch_is_a_named_llm_error_with_its_retryability(
    provider, status, envelope, retryable, fragment, requests
):
    """Decision 431 reads `retryable` and nothing else."""
    seen: list[httpx.Request] = []
    with pytest.raises(client.LLMError) as caught:
        await _complete(provider, envelope, status=status, seen=seen)
    assert caught.value.retryable is retryable, str(caught.value)
    assert fragment in str(caught.value), str(caught.value)
    assert len(seen) == requests
    if status != 200:
        assert caught.value.status == status


# The usage each refused 200 reported, which the provider bills whatever the adapter thought.
BILLED_FAILURES = [
    ("anthropic-prose", "anthropic", _anthropic_prose("end_turn"), (2095, 8000)),
    ("anthropic-cut-off-mid-call", "anthropic", _anthropic_cut_off_mid_call({"tags": TAGS}),
     (2095, 8000)),
    ("anthropic-refusal", "anthropic", _anthropic_refusal(), (0, 0)),
    ("anthropic-refusal-after-output", "anthropic", _anthropic_refusal_after_output(), (2095, 412)),
    ("openai-refusal", "openai", _openai_ok(None, refusal="I'm sorry, I cannot assist."), (1117, 3812)),
    ("openai-length", "openai", _openai_ok("", finish_reason="length"), (1117, 3812)),
    ("openai-truncated-json", "openai", _openai_ok('{"tags": [{"term": "mood.bl', finish_reason="length"),
     (1117, 3812)),
    ("gemini-blocked", "gemini", _gemini_blocked(), (14230, 0)),
    ("gemini-cut-off", "gemini", _gemini_ok("", finish_reason="MAX_TOKENS", parts=False), (14230, 3900)),
    ("gemini-truncated-json", "gemini", _gemini_ok('{"tags": [{"ter', finish_reason="MAX_TOKENS"),
     (14230, 3900)),
    ("gemini-safety", "gemini", _gemini_ok("", finish_reason="SAFETY", parts=False), (14230, 3900)),
]


@pytest.mark.parametrize(("provider", "envelope", "billed"), [b[1:] for b in BILLED_FAILURES],
                         ids=[b[0] for b in BILLED_FAILURES])
async def test_a_failed_answer_carries_the_usage_its_envelope_reported(provider, envelope, billed):
    """Decision 436: a failed attempt is settled to the envelope's reported usage."""
    with pytest.raises(client.LLMError) as caught:
        await _complete(provider, envelope)
    answer = caught.value.answer
    assert answer is not None, str(caught.value)
    assert (answer.tokens_in, answer.tokens_out) == billed
    assert answer.content == json.dumps(envelope).encode("utf-8")
    assert (answer.request_url, answer.http_status) == (URLS[provider], 200)
    assert answer.payload is None


class _Script:
    """Exceptions are raised AFTER the request was handed over."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        step = self.steps[min(len(self.seen), len(self.steps)) - 1]
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("the transport failed here", request=request)
        status, body = step if isinstance(step, tuple) else (step, {"error": {"message": "x"}})
        return httpx.Response(status, content=json.dumps(body).encode("utf-8"),
                              headers={"content-type": "application/json"})


async def _scripted(provider, script, *, key=KEY):
    clock = _Clock()
    async with fetch.Fetcher(transport=httpx.MockTransport(script), clock=clock, sleep=clock.sleep,
                             jitter=lambda low, high: 0.0) as fetcher:
        return await client.complete(fetcher, provider=provider, key=key, model=MODELS[provider],
                                     system="SYSTEM PROMPT", user="USER PROMPT",
                                     schema=contract.EXTRACTION_SCHEMA)


# RFC 9110 9.2.2: POST is not idempotent, so only a connect failure or 408/425/429 is re-sent.
# A Gemini 5xx is unbilled (https://ai.google.dev/gemini-api/docs/billing).
RESENDS = [
    ("read-timeout", "anthropic", httpx.ReadTimeout, 1, False),
    ("read-error", "openai", httpx.ReadError, 1, False),
    ("remote-protocol", "gemini", httpx.RemoteProtocolError, 1, False),
    ("write-error", "anthropic", httpx.WriteError, 1, False),
    ("500", "anthropic", 500, 1, True),
    ("502", "openai", 502, 1, True),
    ("503", "openai", 503, 1, True),
    ("504-anthropic", "anthropic", 504, 1, False),
    ("504-openai", "openai", 504, 1, False),
    ("524-openai", "openai", 524, 1, False),
    # Cloudflare's 520: the request was taken and the answer lost, as with 524.
    ("520-anthropic", "anthropic", 520, 1, False),
    ("520-gemini", "gemini", 520, 1, True),
    ("504-gemini", "gemini", 504, 1, True),
    ("529", "anthropic", 529, 1, True),
    ("connect-error", "gemini", httpx.ConnectError, 4, True),
    ("connect-timeout", "openai", httpx.ConnectTimeout, 4, True),
    ("408", "anthropic", 408, 4, True),
    ("425", "gemini", 425, 4, True),
    ("429", "openai", 429, 4, True),
]


@pytest.mark.parametrize(("provider", "step", "sends", "unbilled"), [r[1:] for r in RESENDS],
                         ids=[r[0] for r in RESENDS])
async def test_a_paid_post_is_sent_again_only_when_it_never_reached_the_provider(
    provider, step, sends, unbilled
):
    """`unbilled` is what the meter's settle reads."""
    script = _Script(step)
    with pytest.raises(client.LLMError) as caught:
        await _scripted(provider, script)
    assert len(script.seen) == sends, [r.method for r in script.seen]
    assert {r.method for r in script.seen} == {"POST"}
    assert caught.value.unbilled is unbilled, str(caught.value)
    assert caught.value.answer is None


async def test_a_connect_failure_that_clears_is_sent_again_and_answers():
    """A connection refused, then an answer, is one answered call and not a stage failure."""
    script = _Script(httpx.ConnectError, (200, _gemini_ok(json.dumps({"tags": TAGS}))))
    result = await _scripted("gemini", script)
    assert len(script.seen) == 2 and result.payload == {"tags": TAGS}


def _usage_of(envelope, provider, **fields):
    key = {"anthropic": "usage", "openai": "usage", "gemini": "usageMetadata"}[provider]
    return {**envelope, key: {**envelope[key], **fields}}


# Wrong member TYPES, as a schema change or a proxy's 200 produces; still an `LLMError`.
MALFORMED = [
    ("anthropic-block-is-a-string", "anthropic", {**_anthropic_ok({"tags": TAGS}), "content": ["oops"]},
     True, (2095, 503)),
    ("anthropic-usage-is-text", "anthropic",
     _usage_of(_anthropic_ok({"tags": TAGS}), "anthropic", output_tokens="1,600"), True, None),
    ("openai-choice-is-a-string", "openai", {**_openai_ok(json.dumps({"tags": TAGS})), "choices": ["x"]},
     True, (1117, 3812)),
    ("openai-usage-is-text", "openai",
     _usage_of(_openai_ok(json.dumps({"tags": TAGS})), "openai", completion_tokens="3.9k"), True, None),
    ("openai-refusal-is-an-object", "openai", _openai_ok(None, refusal={"reason": "policy"}), False,
     (1117, 3812)),
    ("gemini-candidate-is-a-string", "gemini",
     {**_gemini_ok(json.dumps({"tags": TAGS})), "candidates": ["x"]}, True, (14230, 3900)),
    ("gemini-part-is-a-string", "gemini",
     {**_gemini_ok(""), "candidates": [{"content": {"parts": ["x"], "role": "model"},
                                        "finishReason": "STOP"}]}, True, (14230, 3900)),
    ("gemini-usage-is-text", "gemini",
     _usage_of(_gemini_ok(json.dumps({"tags": TAGS})), "gemini", thoughtsTokenCount="2300.0"), True, None),
]


@pytest.mark.parametrize(("provider", "envelope", "retryable", "billed"), [m[1:] for m in MALFORMED],
                         ids=[m[0] for m in MALFORMED])
async def test_a_malformed_answer_is_an_llm_error_and_keeps_the_usage_it_can_read(
    provider, envelope, retryable, billed
):
    """Every failure `complete` can meet is an `LLMError`, or stage 6 re-runs it unmetered."""
    with pytest.raises(client.LLMError) as caught:
        await _complete(provider, envelope)
    assert caught.value.retryable is retryable, str(caught.value)
    answer = caught.value.answer
    assert (None if answer is None else (answer.tokens_in, answer.tokens_out)) == billed


async def test_openai_cache_writes_and_reads_are_read_out_of_the_prompt_breakdown():
    """Cache writes bill 1.25x input (https://developers.openai.com/api/docs/guides/prompt-caching)."""
    usage = {"prompt_tokens": 23_500, "completion_tokens": 3_900, "total_tokens": 27_400,
             "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 23_500},
             "completion_tokens_details": {"reasoning_tokens": 2_300}}
    written = await _complete("openai", _openai_ok(json.dumps({"tags": TAGS}), usage=usage))
    assert (written.tokens_in, written.cache_written, written.cache_read) == (23_500, 23_500, 0)

    usage["prompt_tokens_details"] = {"cached_tokens": 20_000, "cache_write_tokens": 0}
    read = await _complete("openai", _openai_ok(json.dumps({"tags": TAGS}), usage=usage))
    assert (read.tokens_in, read.cache_written, read.cache_read) == (23_500, 0, 20_000)


@pytest.mark.parametrize(("provider", "envelope", "rate"), [
    ("openai", {**_openai_ok(json.dumps({"tags": TAGS})), "service_tier": "default"}, 1.0),
    ("openai", {**_openai_ok(json.dumps({"tags": TAGS})), "service_tier": "priority"}, 2.0),
    ("openai", {**_openai_ok(json.dumps({"tags": TAGS})), "service_tier": "scale"}, 2.0),
    ("anthropic", _anthropic_ok({"tags": TAGS}, usage={"input_tokens": 2095, "output_tokens": 503,
                                                       "inference_geo": "global"}), 1.0),
    ("anthropic", _anthropic_ok({"tags": TAGS}, usage={"input_tokens": 2095, "output_tokens": 503,
                                                       "inference_geo": "us"}), 1.1),
    ("gemini", _gemini_ok(json.dumps({"tags": TAGS})), 1.0),
], ids=["openai-default", "openai-priority", "openai-unpriced-tier", "anthropic-global",
        "anthropic-us", "gemini"])
async def test_each_adapter_reads_the_price_setting_its_envelope_says_it_was_billed_at(
    provider, envelope, rate
):
    """OpenAI reports the tier actually used and Anthropic `usage.inference_geo` (US-only is 1.1x);
    each adapter carries the multiplier its envelope names."""
    seen: list[httpx.Request] = []
    result = await _complete(provider, envelope, seen=seen)
    assert result.rate == rate
    body = _body(seen[0])
    if provider == "openai":
        assert body["service_tier"] == "default"
    assert "inference_geo" not in body, "a pinned geo is refused by a workspace that does not allow it"


# A field the adapter quotes carries the request's key, as an echoing proxy's 200 does.
ECHOED = "{key}" + "z" * 4000
FIELD_ECHOES = [
    ("anthropic-prose-stop", "anthropic",
     lambda key: {**_anthropic_prose("end_turn"), "stop_reason": ECHOED.format(key=key)}),
    ("anthropic-incomplete-stop", "anthropic",
     lambda key: {**_anthropic_ok({"tags": TAGS}), "stop_reason": ECHOED.format(key=key)}),
    ("openai-finish-reason", "openai",
     lambda key: _openai_ok("", finish_reason=ECHOED.format(key=key))),
    ("gemini-block-reason", "gemini",
     lambda key: {**_gemini_blocked(), "promptFeedback": {"blockReason": ECHOED.format(key=key)}}),
    ("gemini-finish-reason", "gemini",
     lambda key: _gemini_ok("", finish_reason=ECHOED.format(key=key), parts=False)),
]


@pytest.mark.parametrize(("provider", "envelope"), [f[1:] for f in FIELD_ECHOES],
                         ids=[f[0] for f in FIELD_ECHOES])
async def test_a_field_an_adapter_quotes_is_redacted_and_bounded_like_the_text_beside_it(
    provider, envelope
):
    """Envelope fields quoted into a message go through `client.shown` like the text beside them."""
    with pytest.raises(client.LLMError) as caught:
        await _complete(provider, envelope(KEY))
    said = str(caught.value)
    assert KEY not in said and "[redacted]" in said, said[:120]
    assert len(said) < 1000, len(said)


async def test_a_markdown_fence_costs_no_retry():
    """A model that ignores the constraint once should not cost a retry over three backticks."""
    fenced = "```json\n" + json.dumps({"tags": TAGS}) + "\n```"
    result = await _complete("openai", _openai_ok(fenced))
    assert result.payload == {"tags": TAGS}


async def test_an_error_message_never_repeats_the_key():
    """An error message is a log line and a park reason on §6.6's board."""
    echoed = {"error": {"message": f"Incorrect API key provided: {KEY}.",
                        "type": "invalid_request_error", "code": "invalid_api_key"}}
    with pytest.raises(client.LLMError) as caught:
        await _complete("openai", echoed, status=401)
    assert KEY not in str(caught.value)
    assert "[redacted]" in str(caught.value)


@pytest.mark.parametrize("pad", [250, 270, 280, 290, 299])
async def test_a_key_quoted_across_the_cut_is_redacted_before_the_message_is_cut(pad):
    """Redacted whole, then cut: a key straddling the cut left its prefix behind."""
    echoed = {"error": {"message": "x" * pad + f" {KEY} was refused",
                        "type": "invalid_request_error", "code": "invalid_api_key"}}
    with pytest.raises(client.LLMError) as caught:
        await _complete("openai", echoed, status=401)
    said = str(caught.value)
    assert not [n for n in range(6, len(KEY) + 1) if KEY[:n] in said], said[-60:]
    async with _fetcher(401, echoed, []) as fetcher:
        answer = await client.probe(fetcher, "openai", key=KEY, model="gpt-5.6-terra")
    assert not [n for n in range(6, len(KEY) + 1) if KEY[:n] in answer["error"]]


async def test_a_proxy_page_that_echoes_the_key_across_the_cut_is_redacted_too():
    page = ("<html>" + "y" * 270 + f" {KEY} </html>").encode()
    with pytest.raises(client.LLMError) as caught:
        await _complete("gemini", page)
    assert not [n for n in range(6, len(KEY) + 1) if KEY[:n] in str(caught.value)]


@pytest.mark.parametrize("spoiled", [f"{KEY}\n", f"{KEY}\r\n", f"  {KEY}\t"])
async def test_a_key_with_whitespace_around_it_is_sent_trimmed(spoiled):
    """h11 refuses a header with a trailing newline and quotes the whole key in its error."""
    seen: list[httpx.Request] = []
    result = await _complete("anthropic", _anthropic_ok({"tags": TAGS}), seen=seen, key=spoiled)
    assert result.payload == {"tags": TAGS}
    assert len(seen) == 1 and seen[0].headers["x-api-key"] == KEY


@pytest.mark.parametrize("spoiled", [f"{KEY[:8]}\n{KEY[8:]}", f"{KEY}\x00x", f"{KEY}\u200b"])
async def test_a_key_no_header_can_carry_is_refused_before_any_request_and_never_quoted(spoiled):
    """Refused before the fetcher is touched, final, unbilled, and naming the fault, not the key."""
    seen: list[httpx.Request] = []
    with pytest.raises(client.LLMError) as caught:
        await _complete("openai", _openai_ok(json.dumps({"tags": TAGS})), seen=seen, key=spoiled)
    assert seen == []
    assert (caught.value.retryable, caught.value.unbilled) == (False, True)
    said = str(caught.value)
    assert KEY[:8] not in said and KEY[8:] not in said, said


def test_every_spelling_of_the_key_an_exception_can_carry_is_redacted():
    """h11 quotes a bytes repr, a traceback a str repr, and a pasted key may carry a newline."""
    key = f"{KEY}\n"
    for text in (f"Illegal header value b'{KEY}\\n'", f"key={key!r}", f"key={KEY} ok", f"{key} ok"):
        assert KEY not in client._redacted(text, key), text


async def test_an_overloaded_anthropic_counts_toward_its_hosts_breaker():
    """529 is the provider saying it is overloaded; it must count toward the breaker."""
    clock = _Clock()
    async with fetch.Fetcher(transport=_serve(529, ANTHROPIC_529, []), clock=clock, sleep=clock.sleep,
                             jitter=lambda low, high: 0.0) as fetcher:
        for _ in range(3):
            with pytest.raises(client.LLMError):
                await client.complete(fetcher, provider="anthropic", key=KEY, model=MODELS["anthropic"],
                                      system="s", user="u", schema=contract.EXTRACTION_SCHEMA)
        [host] = [h for h in fetcher.host_report() if h["host"] == "api.anthropic.com"]
    assert (host["requests"], host["errors"]) == (3, 3), host


# Account-level refusals, each in its provider's documented shape
# (https://platform.claude.com/docs/en/api/errors,
# https://developers.openai.com/api/docs/guides/error-codes,
# https://ai.google.dev/gemini-api/docs/rate-limits): they lift when a limit resets or a balance is paid.
GEMINI_DAILY_QUOTA = {"error": {
    "code": 429, "message": "You exceeded your current quota, please check your plan and billing details.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        "quotaDimensions": {"location": "global", "model": "gemini-3.7-flash"}, "quotaValue": "250"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}}
ACCOUNT_REFUSALS = [
    ("anthropic-402", "anthropic", 402,
     {"type": "error", "error": {"type": "billing_error", "message": "Your credit balance is too low."}},
     "billing_error: Your credit balance is too low.", 1),
    ("anthropic-own-limit", "anthropic", 400,
     {"type": "error", "error": {"type": "invalid_request_error", "message":
      "You have reached your specified API usage limits. You will regain access on 2026-10-01."}},
     "You have reached your specified API usage limits", 1),
    ("anthropic-tier-cap", "anthropic", 429,
     {"type": "error", "error": {"type": "rate_limit_error", "message":
      "You have reached your API usage limits.",
      "details": {"error_code": "enforced_spend_limit_reached"}}},
     "You have reached your API usage limits", 4),
    ("openai-credit", "openai", 429,
     {"error": {"message": "Your organization has no prepaid credits remaining.",
                "type": "insufficient_quota", "param": None, "code": "credit_balance_exhausted"}},
     "credit_balance_exhausted: Your organization has no prepaid credits remaining.", 4),
    ("openai-project-limit", "openai", 429,
     {"error": {"message": "Your project reached its enforced spend limit.", "type": "insufficient_quota",
                "param": None, "code": "project_spend_limit_exceeded"}},
     "project_spend_limit_exceeded", 4),
    ("gemini-402", "gemini", 402,
     {"error": {"code": 402, "message": "Your Prepay credit balance is depleted."}},
     "Your Prepay credit balance is depleted.", 1),
    ("gemini-daily-quota", "gemini", 429, GEMINI_DAILY_QUOTA,
     "RESOURCE_EXHAUSTED: You exceeded your current quota", 4),
    ("openai-legacy-insufficient-quota", "openai", 429,
     {"error": {"message": "You exceeded your current quota, please check your plan and billing details.",
                "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}},
     "insufficient_quota: You exceeded your current quota", 4),
]


@pytest.mark.parametrize(("provider", "status", "envelope", "fragment", "sends"),
                         [a[1:] for a in ACCOUNT_REFUSALS], ids=[a[0] for a in ACCOUNT_REFUSALS])
async def test_a_billing_or_spend_refusal_is_the_accounts_and_says_so_in_the_providers_words(
    provider, status, envelope, fragment, sends
):
    """Marked `account`, so stage 6 parks rather than failing every title, in the provider's own words."""
    seen: list[httpx.Request] = []
    with pytest.raises(client.LLMError) as caught:
        await _complete(provider, envelope, status=status, seen=seen)
    assert caught.value.account is True, str(caught.value)
    assert (caught.value.retryable, caught.value.unbilled) == (False, True)
    assert fragment in str(caught.value), str(caught.value)
    assert len(seen) == sends


@pytest.mark.parametrize(("provider", "status", "envelope", "retryable", "fragment"), [
    ("openai", 429, {"error": {"message": "Rate limit reached for requests", "type": "requests",
                               "param": None, "code": "rate_limit_exceeded"}},
     True, "rate_limit_exceeded: Rate limit reached"),
    # A per-minute Gemini quota is a rate limit: only a per-day `quotaId` is the account's.
    ("gemini", 429, {"error": {**GEMINI_DAILY_QUOTA["error"], "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}},
     True, "RESOURCE_EXHAUSTED: You exceeded your current quota"),
    ("anthropic", 413, {"type": "error", "error": {"type": "request_too_large",
                                                   "message": "Request exceeds the maximum size"}},
     False, "request_too_large: Request exceeds the maximum size"),
    ("anthropic", 409, {"type": "error", "error": {"type": "conflict_error", "message": "Conflict"}},
     False, "conflict_error: Conflict"),
])
async def test_a_rate_limit_or_a_request_refusal_is_not_the_accounts_and_keeps_its_words(
    provider, status, envelope, retryable, fragment
):
    """A rate limit stays on the queue's curve; a refused request stays final."""
    with pytest.raises(client.LLMError) as caught:
        await _complete(provider, envelope, status=status)
    assert caught.value.account is False
    assert caught.value.retryable is retryable
    assert fragment in str(caught.value), str(caught.value)


@pytest.mark.parametrize(("provider", "key", "model", "fragment"), [
    ("mistral", KEY, "m", "unknown provider"),
    ("gemini", "", "gemini-3.7-flash", "no API key"),
    ("openai", KEY, "", "no model"),
])
async def test_a_call_that_cannot_succeed_is_refused_before_any_request(provider, key, model, fragment):
    """Unknown provider, missing key or model: final and costs nothing."""
    seen: list[httpx.Request] = []
    with pytest.raises(client.LLMError) as caught:
        async with _fetcher(200, {}, seen) as fetcher:
            await client.complete(fetcher, provider=provider, key=key, model=model, system="s",
                                  user="u", schema=contract.EXTRACTION_SCHEMA)
    assert caught.value.retryable is False
    assert fragment in str(caught.value)
    assert seen == []


async def test_one_schema_valid_answer_reaches_the_same_verdict_through_all_three_adapters():
    """The mechanism differs and the enforcement does not."""
    payload = {"tags": [
        TAGS[0],
        {"term": "themes.mecha", "salience": 2, "source": "imdb:1", "quote": "unforgiving portrait"},
        {"term": "pacing.slow_burn", "salience": 2, "source": "imdb:2",
         "quote": "a story nobody in this pack ever wrote"},
        {"term": "themes.robots", "salience": 4, "source": "imdb:1", "quote": "portrait of a town"},
    ]}
    voc = Vocabulary.build("v1", {"mood.bleak": "mood", "themes.robots": "themes",
                                  "pacing.slow_burn": "pacing"}, ["mood", "themes", "pacing"])
    pack = ("# Grey Harbour (2021)\n[type] film\n\n[imdb:1]\n"
            "It is a bleak and unforgiving portrait of a town.\n")

    verdicts = {}
    for provider in client.PROVIDERS:
        result = await _complete(provider, OK[provider](payload))
        assert result.payload == payload, provider
        judged = await verify_tags(7, result.payload["tags"], pack=pack, voc=voc,
                                   ledger=_NoVerdicts())
        verdicts[provider] = (
            [(t.term, t.salience, t.quote) for t in judged.tags],
            [(r.reason, r.term) for r in judged.rejects],
            contract.violation_prompt(judged.rejects, version="v1"),
        )

    assert verdicts["anthropic"] == verdicts["openai"] == verdicts["gemini"]
    kept, rejects, retry = verdicts["gemini"]
    assert kept == [("mood.bleak", 3, "a bleak and unforgiving portrait of a town")]
    assert sorted(reason for reason, _ in rejects) == ["quote_unverified", "schema", "unknown_term"]
    assert "unknown_term: 'themes.mecha' is not in vocabulary v1" in retry
    assert "quote_unverified: 'a story nobody in this pack ever wrote'" in retry
    assert "stated level 4 is outside the declared domain" in retry


MODELS_LISTS = {
    # https://docs.anthropic.com/en/api/models-list
    "anthropic": ("https://api.anthropic.com/v1/models?limit=1000", {
        "data": [{"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
                  "created_at": "2026-08-01T00:00:00Z"}],
        "has_more": False, "first_id": "claude-sonnet-5", "last_id": "claude-sonnet-5"}),
    # https://platform.openai.com/docs/api-reference/models/list
    "openai": ("https://api.openai.com/v1/models", {
        "object": "list",
        "data": [{"id": "gpt-5.6-terra", "object": "model", "created": 1753833600,
                  "owned_by": "system"}]}),
    # https://ai.google.dev/api/models#method:-models.list
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", {
        "models": [{"name": "models/gemini-3.7-flash", "version": "001",
                    "displayName": "Gemini 3.7 Flash",
                    "supportedGenerationMethods": ["generateContent", "countTokens"]}],
        "nextPageToken": ""}),
}


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_the_probe_reads_the_free_models_list_with_the_key_in_a_header(provider):
    """`model_listed` is not "usable": a listed model can still refuse to generate."""
    url, listing = MODELS_LISTS[provider]
    seen: list[httpx.Request] = []
    async with _fetcher(200, listing, seen) as fetcher:
        listed = await client.probe(fetcher, provider, key=KEY, model=MODELS[provider])
    async with _fetcher(200, listing, []) as fetcher:
        unlisted = await client.probe(fetcher, provider, key=KEY, model="no-such-model")

    assert listed == {"ok": True, "status": 200, "error": None, "model": MODELS[provider],
                      "model_listed": True}
    assert unlisted["ok"] is True and unlisted["model_listed"] is False
    assert len(seen) == 1 and seen[0].method == "GET"
    assert str(seen[0].url) == url
    assert KEY not in str(seen[0].url)
    header = {"anthropic": "x-api-key", "openai": "authorization", "gemini": "x-goog-api-key"}
    assert KEY in seen[0].headers[header[provider]]


async def test_a_probe_the_provider_refuses_reports_the_status_and_the_reason():
    async with _fetcher(401, ANTHROPIC_401, []) as fetcher:
        answer = await client.probe(fetcher, "anthropic", key=KEY, model="claude-sonnet-5")
    assert answer["ok"] is False and answer["status"] == 401
    assert "authentication_error" in answer["error"]
    assert answer["model_listed"] is None


async def test_a_probe_with_no_key_asks_nothing():
    seen: list[httpx.Request] = []
    async with _fetcher(200, {}, seen) as fetcher:
        answer = await client.probe(fetcher, "openai", key="", model="gpt-5.6-terra")
    assert answer["ok"] is False and "no API key" in answer["error"]
    assert seen == []


def test_open_fetcher_is_one_fetcher_on_the_callers_connection():
    """Module-level, so a test replaces it the way `registry.make_client` is replaced."""
    sentinel = object()
    built = client.open_fetcher(sentinel)
    assert isinstance(built, fetch.Fetcher)
    assert built.conn is sentinel


# The spellings Gemini's reference documents and a "simplification" would reach for.
_KEY_IN_URL = re.compile(r"[?&](?:api_?key|key)\b", re.IGNORECASE)


def _key_in_url_builders(source: str) -> list[str]:
    """Docstrings are skipped: a module may say that Gemini's `?key=` is the trap."""
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else "{}"
                           for part in node.values)
        elif isinstance(node, ast.Call) and any(kw.arg == "params" for kw in node.keywords):
            hits.append(f"params= in {ast.get_source_segment(source, node)}")
            continue
        else:
            continue
        if _KEY_IN_URL.search(text):
            hits.append(text)
    return hits


def test_no_llm_module_builds_a_url_with_a_key_or_passes_params():
    """`params=` lands in `raw_document.url` and the INFO line exactly as a hand-written query would."""
    modules = sorted(LLM_PACKAGE.glob("*.py"))
    names = {path.name for path in modules}
    assert {"__init__.py", "client.py", "anthropic.py", "openai.py", "gemini.py",
            "contract.py"} <= names, f"the walk read {sorted(names)}; it is looking in the wrong place"
    offenders = {path.name: _key_in_url_builders(path.read_text(encoding="utf-8"))
                 for path in modules}
    assert not {name: hits for name, hits in offenders.items() if hits}, offenders


def test_the_url_reader_reports_every_spelling_it_exists_to_catch():
    """A guard that cannot report a violation is a green line rather than a proof."""
    assert _key_in_url_builders('url = f"{BASE}/{model}:generateContent?key={key}"')
    assert _key_in_url_builders('url = BASE + "?api_key=" + key')
    assert _key_in_url_builders('url = "https://x.example/v1?model=m&key=" + key')
    assert _key_in_url_builders('resp = await fetcher.get(url, params={"key": key})')
    assert not _key_in_url_builders('"""Gemini documents a `?key=` query parameter."""\nx = 1')
    assert not _key_in_url_builders('headers = {"x-goog-api-key": key}')
