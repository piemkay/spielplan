"""`ops/fake_llm.py`, the refusing double: its envelopes and refusals are the providers' own, and its
content comes from the request, judged by `verify_tags`. No database."""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import fetch
from spielplan.dna import packs
from spielplan.dna.verify import Rejection, Vocabulary, verify_tags
from spielplan.llm import client, contract, gemini, openai
from tests.test_dna_verify import _NoVerdicts

REPO = Path(__file__).resolve().parents[2]
DOUBLE = REPO / "ops" / "fake_llm.py"

MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra", "gemini": "gemini-3.7-flash"}
URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent",
}
DOCUMENTED_HEADER = {"anthropic": "x-api-key", "openai": "authorization", "gemini": "x-goog-api-key"}

# Six terms over four facets, so an answer's three real tags leave spare terms.
PROMPT_VOC = contract.PromptVocabulary(
    version="v1",
    facets=("mood", "themes", "pacing", "place"),
    terms=(
        ("mood.bleak", "mood", "hopeless, grey, unconsoled"),
        ("mood.tense", "mood", None),
        ("pacing.slow_burn", "pacing", "patient build that pays off late"),
        ("place.harbour_town", "place", None),
        ("themes.revenge", "themes", "a wrong answered in kind"),
        ("themes.robots", "themes", None),
    ),
)
VOC = Vocabulary.build("v1", {term: facet for term, facet, _gloss in PROMPT_VOC.terms},
                       PROMPT_VOC.facets)

PACK, _INFO = packs.render_pack(
    7, "Grey Harbour", 2021, "film", None, None,
    "A fisherman returns to the harbour town that exiled him and slowly takes his revenge on the men "
    "who drowned his brother.",
    [("imdb", "It is a **bleak** and unforgiving portrait of a town that has decided what it will not "
              "remember, shot in grey light by a director who refuses every consolation."),
     ("tmdb", "The tension builds patiently across two hours and never once releases, a slow burn "
              "that rewards anyone willing to stay with its long and silent scenes.")],
)
SYSTEM = contract.system_prompt(PROMPT_VOC)
USER = contract.user_prompt(PACK)


@pytest.fixture
def double():
    """Registered in `sys.modules` before it runs: Pydantic
    resolves the control model's annotations there."""
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


class _Clock:
    """The fetcher's clock and sleeper together, so pacing is waited out at once."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


def _fetcher(double) -> fetch.Fetcher:
    clock = _Clock()
    return fetch.Fetcher(transport=httpx.ASGITransport(app=double.app), clock=clock, sleep=clock.sleep,
                         jitter=lambda low, high: 0.0)


async def _complete(double, provider, *, user=USER, key=None, model=None):
    async with _fetcher(double) as fetcher:
        return await client.complete(
            fetcher, provider=provider, key=double.KEYS[provider] if key is None else key,
            model=MODELS[provider] if model is None else model, system=SYSTEM, user=user,
            schema=contract.EXTRACTION_SCHEMA)


async def _judge(payload, pack=PACK):
    tags = payload.get("tags") if isinstance(payload, dict) else None
    return await verify_tags(7, tags, pack=pack, voc=VOC, ledger=_NoVerdicts())


async def _direct(double, method, url, **kwargs) -> httpx.Response:
    """A request the adapters never make, sent straight to the double, to show a refusal is the server's."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=double.app)) as http:
        return await http.request(method, url, **kwargs)


async def _scenario(double, **fields) -> httpx.Response:
    return await _direct(double, "POST", "http://fake-llm/_test/scenario", json=fields)


# The double's envelope must be a SUBSET of these and carry every field each adapter reads.
DOCUMENTED = {
    # https://docs.anthropic.com/en/api/messages; thinking breakdown per the thinking-steering page.
    "anthropic": {
        # `stop_details`: "Structured information about a refusal", null otherwise.
        "top": {"id", "type", "role", "content", "model", "stop_reason", "stop_details",
                "stop_sequence", "usage", "container"},
        # `inference_geo`: where inference ran
        # (https://platform.claude.com/docs/en/manage-claude/data-residency).
        "usage": {"input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens", "cache_creation", "server_tool_use", "service_tier",
                  "output_tokens_details", "inference_geo"},
        # https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use
        "block": {"type", "id", "name", "input"},
        # https://platform.claude.com/docs/en/build-with-claude/thinking: `type`, the (omitted, so
        # empty) `thinking` text and its `signature`
        "thinking": {"type", "thinking", "signature"},
    },
    # https://platform.openai.com/docs/api-reference/chat/object
    "openai": {
        "top": {"id", "object", "created", "model", "choices", "usage", "service_tier",
                "system_fingerprint"},
        "choice": {"index", "message", "logprobs", "finish_reason"},
        "message": {"role", "content", "refusal", "annotations", "tool_calls", "audio",
                    "function_call"},
        "usage": {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details",
                  "completion_tokens_details"},
    },
    # https://ai.google.dev/api/generate-content#v1beta.GenerateContentResponse
    "gemini": {
        "top": {"candidates", "promptFeedback", "usageMetadata", "modelVersion", "responseId",
                "modelStatus"},
        "candidate": {"content", "finishReason", "safetyRatings", "citationMetadata", "tokenCount",
                      "groundingAttributions", "groundingMetadata", "avgLogprobs", "logprobsResult",
                      "urlContextMetadata", "index", "finishMessage"},
        "usage": {"promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount",
                  "toolUsePromptTokenCount", "thoughtsTokenCount", "totalTokenCount",
                  "promptTokensDetails", "cacheTokensDetails", "candidatesTokensDetails",
                  "toolUsePromptTokensDetails"},
    },
}


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_each_provider_answers_in_its_published_envelope(double, provider):
    result = await _complete(double, provider)
    raw, doc = result.raw, DOCUMENTED[provider]
    assert set(raw) <= doc["top"], set(raw) - doc["top"]
    if provider == "anthropic":
        assert {"id", "type", "role", "content", "model", "stop_reason", "usage"} <= set(raw)
        assert (raw["type"], raw["role"], raw["stop_reason"]) == ("message", "assistant", "tool_use")
        assert "stop_sequence" in raw and raw["stop_sequence"] is None
        # Sonnet 5 thinks by default, so the adapter finds the tool call by type, not position.
        thinking, block = raw["content"]
        assert set(thinking) <= doc["thinking"] and thinking["type"] == "thinking"
        assert thinking["thinking"] == "" and thinking["signature"]
        assert set(block) <= doc["block"] and block["type"] == "tool_use"
        assert block["name"] == client.TOOL_NAME and block["id"].startswith("toolu_")
        assert set(raw["usage"]) <= doc["usage"]
        # Where inference ran, which is what US-only inference is billed on.
        assert raw["usage"]["inference_geo"] == "global"
    elif provider == "openai":
        assert (raw["object"], len(raw["choices"])) == ("chat.completion", 1)
        [choice] = raw["choices"]
        assert set(choice) <= doc["choice"] and choice["finish_reason"] == "stop"
        assert set(choice["message"]) <= doc["message"]
        assert choice["message"]["refusal"] is None and choice["message"]["role"] == "assistant"
        assert json.loads(choice["message"]["content"]) == result.payload
        assert set(raw["usage"]) <= doc["usage"]
        assert set(raw["usage"]["completion_tokens_details"]) == {"reasoning_tokens"}
        # The tier the answer was served at, which the app pins to the standard one.
        assert raw["service_tier"] == "default"
    else:
        [candidate] = raw["candidates"]
        assert set(candidate) <= doc["candidate"] and candidate["finishReason"] == "STOP"
        assert candidate["content"]["role"] == "model"
        assert json.loads(candidate["content"]["parts"][0]["text"]) == result.payload
        assert set(raw["usageMetadata"]) <= doc["usage"]
        assert raw["modelVersion"] == MODELS["gemini"]
    assert result.provider == provider and result.model == MODELS[provider]
    assert result.request_url == URLS[provider] and result.http_status == 200


@pytest.mark.parametrize("content", ["fabricate", "unquotable", "salience", "clean"])
async def test_the_three_adapters_read_identical_tags_and_verdicts_from_the_double(double, content):
    await _scenario(double, content=content)
    seen = {}
    for provider in client.PROVIDERS:
        result = await _complete(double, provider)
        judged = await _judge(result.payload)
        seen[provider] = (
            result.payload,
            [(t.term, t.salience, t.quote) for t in judged.tags],
            [(r.reason, r.term) for r in judged.rejects],
        )
    assert seen["anthropic"] == seen["openai"] == seen["gemini"], seen


VIOLATIONS = [
    ("fabricate", "unknown_term", "is not in vocabulary v1"),
    ("unquotable", "quote_unverified", "is not in this title's pack"),
    ("salience", "schema", "stated level 4 is outside the declared domain"),
]


async def test_the_default_first_answer_carries_exactly_one_term_the_vocabulary_does_not(double):
    """No term in the vocabulary has the fabricated term's tail, so prefix repair cannot rescue it."""
    result = await _complete(double, "gemini")
    tags = result.payload["tags"]
    invented = [t["term"] for t in tags if t["term"] not in VOC.terms]
    assert len(invented) == 1, tags
    assert VOC.resolve(invented[0]) is None
    judged = await _judge(result.payload)
    assert [(r.reason, r.term) for r in judged.rejects] == [("unknown_term", invented[0])]
    assert len(judged.tags) == len(tags) - 1 == 3


@pytest.mark.parametrize(("content", "reason", "fragment"),
                         VIOLATIONS + [("clean", None, None)], ids=[v[0] for v in VIOLATIONS] + ["clean"])
async def test_each_content_scenario_breaks_exactly_the_rule_it_names(double, content, reason, fragment):
    await _scenario(double, content=content)
    result = await _complete(double, "anthropic")
    judged = await _judge(result.payload)
    if reason is None:
        assert judged.rejects == [] and len(judged.tags) == 3
        return
    [reject] = judged.rejects
    assert reject.reason == reason
    assert fragment in contract.violation_prompt(judged.rejects, version="v1")
    assert len(judged.tags) == 3


@pytest.mark.parametrize("content", ["fabricate", "unquotable", "salience", "clean"])
async def test_every_quote_the_double_did_not_embellish_is_cut_verbatim_from_the_pack_it_was_sent(
    double, content
):
    """Character-for-character substrings, not merely equal
    under `norm()`, so nothing is a canned answer."""
    await _scenario(double, content=content)
    markers = {line.strip("[]") for line in PACK.splitlines() if line.startswith("[") and ":" in line}
    tags = (await _complete(double, "openai")).payload["tags"]
    for tag in tags:
        embellished = content == "unquotable" and tag is tags[-1]
        assert (tag["quote"] in PACK) is not embellished, tag
        assert tag["source"] in markers, tag

    other, _ = packs.render_pack(
        8, "Salt Road", 2019, "film", None, None,
        "Two sisters drive the length of a coast road to scatter their mother's ashes and argue the "
        "whole way about who she really was.", [])
    others = (await _complete(double, "openai", user=contract.user_prompt(other))).payload["tags"]
    assert all(t["quote"] in other and t["quote"] not in PACK for t in others[:3]), others


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_a_retry_in_comply_drops_the_named_tag_and_in_stubborn_repeats_it(double, provider):
    first = await _judge((await _complete(double, provider)).payload)
    [reject] = first.rejects
    retry = f"{USER}\n\n{contract.violation_prompt(first.rejects, version='v1')}"

    complied = await _judge((await _complete(double, provider, user=retry)).payload)
    assert complied.rejects == []
    assert complied.tags == first.tags

    await _scenario(double, provider=provider, posture="stubborn")
    stubborn = await _judge((await _complete(double, provider, user=retry)).payload)
    assert [(r.reason, r.term) for r in stubborn.rejects] == [(reject.reason, reject.term)]

    log = [r for r in double.state.requests if r["provider"] == provider]
    assert [r["retry"] is not None for r in log] == [False, True, True]
    assert f"unknown_term: '{reject.term}' is not in vocabulary v1" in log[1]["retry"]
    assert reject.term not in log[1]["terms"] and reject.term in log[2]["terms"]


def test_the_retry_opening_the_double_recognises_is_the_one_the_app_sends(double):
    """The double holds the corpus's retry opening rather
    than importing the app's; this pins that they agree."""
    refused = Rejection(7, "mood.mecha", "unknown_term", "not in vocabulary")
    retry = contract.violation_prompt([refused], version="v1")
    assert double.RETRY_MARKER == contract.RETRY_MARKER
    assert retry.startswith(double.RETRY_MARKER)


@pytest.mark.parametrize(("provider", "status", "fragment"), [
    ("anthropic", 401, "authentication_error: invalid x-api-key"),
    ("openai", 401, "invalid_api_key: Incorrect API key provided"),
    ("gemini", 400, "INVALID_ARGUMENT: API key not valid"),
])
async def test_a_wrong_key_is_refused_in_each_providers_documented_error_shape(
    double, provider, status, fragment
):
    """Gemini answers a bad key with 400, not 401, as its troubleshooting page says."""
    with pytest.raises(client.LLMError) as caught:
        await _complete(double, provider, key="not-the-key")
    assert caught.value.status == status and caught.value.retryable is False
    assert fragment in str(caught.value), str(caught.value)


@pytest.mark.parametrize(("url", "status", "field", "value"), [
    (URLS["anthropic"], 401, ("error", "message"), "x-api-key header is required"),
    (URLS["openai"], 401, ("error", "message"), "You didn't provide an API key"),
    (URLS["gemini"], 403, ("error", "status"), "PERMISSION_DENIED"),
])
async def test_a_missing_key_is_refused_before_anything_is_read(double, url, status, field, value):
    """The adapters refuse an empty key before any request, so this is sent directly."""
    resp = await _direct(double, "POST", url, json={"model": "m", "messages": [], "contents": []})
    assert resp.status_code == status
    assert value in resp.json()[field[0]][field[1]]


async def test_anthropic_refuses_what_its_reference_and_the_corpus_say_it_refuses(double):
    """Accepted values are answered: the double must not be stricter than the server."""
    key = {"x-api-key": double.KEYS["anthropic"]}
    version = {**key, "anthropic-version": "2023-06-01"}
    body = {"model": MODELS["anthropic"], "max_tokens": 10, "system": SYSTEM,
            "messages": [{"role": "user", "content": USER}],
            "tools": [{"name": "emit_dna", "input_schema": contract.EXTRACTION_SCHEMA}],
            "tool_choice": {"type": "tool", "name": "emit_dna"}}
    enabled = {"type": "enabled", "budget_tokens": 1024}
    removed = ('"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and'
               ' "output_config.effort" to control thinking behavior.')
    cases = [
        (key, body, 400, "anthropic-version: header is required"),
        (version, {**body, "model": "claude-nonesuch"}, 404, "model: claude-nonesuch"),
        (version, {**body, "temperature": 0}, 400, "temperature"),
        (version, {**body, "top_p": 0.7}, 400, "top_p"),
        (version, {**body, "top_k": 5}, 400, "top_k"),
        (version, {**body, "thinking": enabled}, 400, removed),
        (version, {**body, "thinking": enabled, "tool_choice": {"type": "auto"}}, 400, removed),
        (version, {k: v for k, v in body.items() if k != "max_tokens"}, 400, "max_tokens: Field required"),
    ]
    for headers, sent, status, message in cases:
        resp = await _direct(double, "POST", URLS["anthropic"], json=sent, headers=headers)
        assert resp.status_code == status, (message, resp.text)
        assert resp.json()["type"] == "error" and message in resp.json()["error"]["message"]
    for answered in (body, {**body, "temperature": 1}, {**body, "temperature": 1.0},
                     {**body, "top_p": 0.99}, {**body, "thinking": {"type": "adaptive"}}):
        ok = await _direct(double, "POST", URLS["anthropic"], json=answered, headers=version)
        assert ok.status_code == 200 and ok.json()["stop_reason"] == "tool_use", ok.text


def _openai_body(**extra):
    body = {"model": MODELS["openai"], "max_completion_tokens": 100,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]}
    return {**body, **extra}


def _strict(schema):
    return {"type": "json_schema", "json_schema": {"name": "emit_dna", "strict": True, "schema": schema}}


async def test_openai_refuses_max_tokens_and_a_temperature_its_reasoning_models_do_not_take(double):
    """"When reasoning effort is not `none`, remove `temperature`"
    (https://developers.openai.com/api/docs/guides/latest-model)."""
    auth = {"Authorization": f"Bearer {double.KEYS['openai']}"}
    old = await _direct(double, "POST", URLS["openai"], headers=auth, json={
        **{k: v for k, v in _openai_body().items() if k != "max_completion_tokens"}, "max_tokens": 100})
    assert old.status_code == 400
    assert old.json()["error"] == {
        "message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use "
                   "'max_completion_tokens' instead.",
        "type": "invalid_request_error", "param": "max_tokens", "code": "unsupported_parameter"}
    warm = await _direct(double, "POST", URLS["openai"], headers=auth, json=_openai_body(temperature=0))
    assert warm.status_code == 400 and warm.json()["error"]["code"] == "unsupported_value"
    reasoning = await _direct(double, "POST", URLS["openai"], headers=auth,
                              json=_openai_body(temperature=0, reasoning_effort="low"))
    assert reasoning.status_code == 400 and reasoning.json()["error"]["code"] == "unsupported_value"
    plain = await _direct(double, "POST", URLS["openai"], headers=auth,
                          json=_openai_body(temperature=0, reasoning_effort="none"))
    assert plain.status_code == 200, plain.text
    mini = await _direct(double, "POST", URLS["openai"], headers=auth,
                         json=_openai_body(model="gpt-5-mini", temperature=0, reasoning_effort="none"))
    assert mini.status_code == 400 and mini.json()["error"]["code"] == "unsupported_value"


async def test_openai_strict_mode_refuses_what_its_subset_lacks_and_answers_what_it_now_carries(double):
    """Refused and answered exactly as https://developers.openai.com/api/docs/guides/structured-outputs
    lists: a lenient double and a stricter-than-server double are both failures."""
    auth = {"Authorization": f"Bearer {double.KEYS['openai']}"}
    item = contract.EXTRACTION_SCHEMA["properties"]["tags"]["items"]

    def schema_with(**changes):
        tags = {"type": "array", "items": {**item, **changes.pop("item", {})}, **changes.pop("tags", {})}
        return {"type": "object", "properties": {"tags": tags}, "required": ["tags"],
                "additionalProperties": False, **changes}

    quote_min = {"properties": {**item["properties"], "quote": {"type": "string", "minLength": 1}}}
    loose = {k: v for k, v in item.items() if k != "additionalProperties"}
    partial = {**item, "required": ["term", "salience", "source"]}
    refused = [
        (schema_with(item=quote_min), "In context=('properties', 'tags', 'items', 'properties', "
                                      "'quote'), 'minLength' is not permitted."),
        (schema_with(tags={"uniqueItems": True}), "In context=('properties', 'tags'), 'uniqueItems' is "
                                                  "not permitted."),
        ({**schema_with(), "properties": {"tags": {"type": "array", "items": loose}}},
         "In context=('properties', 'tags', 'items'), 'additionalProperties' is required to be supplied "
         "and to be false."),
        ({**schema_with(), "properties": {"tags": {"type": "array", "items": partial}}},
         "Missing 'quote'."),
        ({"type": "array", "items": item}, "schema must be a JSON Schema of 'type: \"object\"'"),
        (schema_with(tags={"allOf": [{"minItems": 1}]}), "In context=('properties', 'tags'), 'allOf' is "
                                                         "not permitted."),
        (schema_with(item={"not": {"required": ["term"]}}), "'not' is not permitted."),
        (schema_with(item={"if": {"required": ["term"]}, "then": {"required": ["quote"]}}),
         "'if' is not permitted."),
        (schema_with(item={"dependentRequired": {"term": ["quote"]}}), "'dependentRequired' is not "
                                                                       "permitted."),
    ]
    for schema, fragment in refused:
        resp = await _direct(double, "POST", URLS["openai"], headers=auth,
                             json=_openai_body(response_format=_strict(schema)))
        assert resp.status_code == 400, (fragment, resp.text)
        error = resp.json()["error"]
        assert error["message"].startswith("Invalid schema for response_format 'emit_dna': ")
        assert fragment in error["message"] and error["param"] == "response_format"

    bounded = schema_with(tags={"minItems": 1, "maxItems": 47})
    assert "minimum" in json.dumps(bounded)
    ok = await _direct(double, "POST", URLS["openai"], headers=auth,
                       json=_openai_body(response_format=_strict(bounded)))
    assert ok.status_code == 200, ok.text
    stripped = openai._strip_keywords(contract.EXTRACTION_SCHEMA, openai._STRICT_UNSUPPORTED)
    sent = await _direct(double, "POST", URLS["openai"], headers=auth,
                         json=_openai_body(response_format=_strict(stripped)))
    assert sent.status_code == 200, sent.text


async def test_gemini_refuses_a_response_schema_field_its_schema_object_does_not_declare(double):
    """The request is parsed as a protocol buffer and the Schema message has no `additionalProperties`."""
    headers = {"x-goog-api-key": double.KEYS["gemini"]}

    def body(schema):
        return {"contents": [{"role": "user", "parts": [{"text": USER}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}}

    raw = await _direct(double, "POST", URLS["gemini"], headers=headers,
                        json=body(contract.EXTRACTION_SCHEMA))
    assert raw.status_code == 400
    assert raw.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert raw.json()["error"]["message"] == (
        "Invalid JSON payload received. Unknown name \"additionalProperties\" at "
        "'generation_config.response_schema': Cannot find field.")
    projected = await _direct(double, "POST", URLS["gemini"], headers=headers,
                              json=body(gemini._gemini_schema(contract.EXTRACTION_SCHEMA)))
    assert projected.status_code == 200, projected.text


async def test_gemini_takes_the_key_in_either_place_the_real_api_does_and_the_log_says_which(double):
    """Refusing `?key=` would be stricter than Google; the log is what holds the app's header-only rule."""
    body = {"contents": [{"role": "user", "parts": [{"text": USER}]}]}
    by_header = await _direct(double, "POST", URLS["gemini"], json=body,
                              headers={"x-goog-api-key": double.KEYS["gemini"]})
    by_query = await _direct(double, "POST", f"{URLS['gemini']}?key={double.KEYS['gemini']}", json=body)
    assert by_header.status_code == by_query.status_code == 200

    header_log, query_log = double.state.requests
    assert (header_log["key_header"], header_log["key_in_url"]) == (True, False)
    assert (query_log["key_header"], query_log["key_in_url"]) == (False, True)
    assert query_log["url"] == f"{URLS['gemini']}?key=[redacted]"
    assert double.KEYS["gemini"] not in json.dumps(double.state.requests)


async def test_the_log_keeps_each_url_and_header_name_the_adapters_sent_and_never_a_key(double, caplog):
    """The double stores header NAMES only."""
    caplog.set_level(logging.INFO, logger="httpx")
    for provider in client.PROVIDERS:
        await _complete(double, provider)
        async with _fetcher(double) as fetcher:
            probe = await client.probe(fetcher, provider, key=double.KEYS[provider],
                                       model=MODELS[provider])
        assert probe["ok"] is True, probe

    kept = json.dumps((await _direct(double, "GET", "http://fake-llm/_test/state")).json())
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    for provider, key in double.KEYS.items():
        assert key not in kept and not [line for line in lines if key in line], provider
    posts = [r for r in double.state.requests if r["method"] == "POST"]
    assert [r["url"] for r in posts] == [URLS[p] for p in client.PROVIDERS]
    for record in double.state.requests:
        assert record["key_header"] is True and record["key_in_url"] is False, record
        assert DOCUMENTED_HEADER[record["provider"]] in record["headers"], record
        assert record["status"] == 200


@pytest.mark.parametrize(("provider", "envelope", "retryable", "fragment"), [
    # The forced call cut short is a `tool_use` block at `max_tokens`, as Anthropic documents it.
    ("anthropic", "max_tokens", True, "tool_use block is incomplete (stop_reason=max_tokens)"),
    ("anthropic", "prose", False, "no tool_use block (stop_reason=end_turn)"),
    # Anthropic's refusal comes from classifiers Opus 5 carries and Sonnet 5 does not.
    ("anthropic", "refusal", False, "no tool_use block (stop_reason=refusal)"),
    ("openai", "refusal", False, "refused: I'm sorry"),
    ("openai", "max_tokens", True, "empty content (finish_reason=length)"),
    ("gemini", "max_tokens", True, "empty candidate (finishReason=MAX_TOKENS)"),
    ("gemini", "blocked", False, "no candidates: SAFETY"),
    ("gemini", "safety", False, "empty candidate (finishReason=SAFETY)"),
])
async def test_each_documented_failure_envelope_reaches_the_adapters_named_error(
    double, provider, envelope, retryable, fragment
):
    await _scenario(double, provider=provider, envelope=envelope)
    model = "claude-opus-5" if (provider, envelope) == ("anthropic", "refusal") else None
    with pytest.raises(client.LLMError) as caught:
        await _complete(double, provider, model=model)
    assert caught.value.retryable is retryable
    assert fragment in str(caught.value), str(caught.value)


@pytest.mark.parametrize(("fault", "published"), [
    (503, {"message": "The requested model is temporarily overloaded.",
           "type": "service_unavailable_error", "code": "server_is_overloaded"}),
    (500, {"message": "The server had an error while processing your request."}),
])
async def test_an_openai_server_error_is_answered_in_the_shape_its_error_page_publishes(
    double, fault, published
):
    """The shapes https://developers.openai.com/api/docs/guides/error-codes
    publishes; a 500 has no type or code."""
    assert (await _scenario(double, provider="openai", fault=fault)).status_code == 200
    auth = {"Authorization": f"Bearer {double.KEYS['openai']}"}

    resp = await _direct(double, "POST", URLS["openai"], headers=auth, json=_openai_body())

    assert resp.status_code == fault
    error = resp.json()["error"]
    assert {field: error.get(field) for field in published} == published, error


async def test_an_anthropic_refusal_is_the_published_empty_one_and_bills_nothing(double):
    """"Token counts appear in `usage` but are not charged": a
    refusal bills nothing, and only where classifiers are."""
    await _scenario(double, provider="anthropic", envelope="refusal")
    with pytest.raises(client.LLMError) as caught:
        await _complete(double, "anthropic", model="claude-opus-5")
    answer = caught.value.answer
    assert answer is not None and (answer.tokens_in, answer.tokens_out) == (0, 0)
    raw = json.loads(answer.content)
    assert (raw["content"], raw["stop_reason"], raw["usage"]["output_tokens"]) == ([], "refusal", 0)
    assert raw["usage"]["input_tokens"] > 0
    assert set(raw["stop_details"]) == {"type", "category", "explanation"}
    assert raw["stop_details"]["type"] == "refusal"
    [sent] = double.state.requests
    assert (sent["envelope"], sent["usage"]) == ("refusal", None)

    answered = await _complete(double, "anthropic")
    assert answered.payload["tags"] and answered.raw["stop_reason"] == "tool_use"
    assert answered.raw["stop_details"] is None
    assert double.state.requests[-1]["usage"]["output_tokens"] > 0


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"])
async def test_a_model_that_refuses_forced_tool_use_is_listed_and_refuses_it_as_published(double, model):
    """https://platform.claude.com/docs/en/api/errors,
    "Forced tool use not supported": listed, and a 400."""
    headers = {"x-api-key": double.KEYS["anthropic"], "anthropic-version": "2023-06-01"}
    page = await _direct(double, "GET", "https://api.anthropic.com/v1/models?limit=1000", headers=headers)
    assert model in {entry["id"] for entry in page.json()["data"]}

    with pytest.raises(client.LLMError) as caught:
        await _complete(double, "anthropic", model=model)
    assert (caught.value.status, caught.value.retryable) == (400, False)
    assert 'tool_choice: type "tool" and "any" are not supported for this model.' in str(caught.value)

    body = {"model": model, "max_tokens": 10, "system": SYSTEM,
            "messages": [{"role": "user", "content": USER}],
            "tools": [{"name": "emit_dna", "input_schema": contract.EXTRACTION_SCHEMA}],
            "tool_choice": {"type": "auto"}}
    auto = await _direct(double, "POST", URLS["anthropic"], json=body, headers=headers)
    assert auto.status_code == 200 and auto.json()["stop_reason"] == "end_turn", auto.text


async def test_the_usage_blocks_bill_thinking_the_way_each_provider_reports_it(double):
    """Gemini reports thoughts apart (1,600 + 2,300 billed); OpenAI folds them into `completion_tokens`."""
    thought = await _complete(double, "gemini")
    usage = thought.raw["usageMetadata"]
    assert (usage["candidatesTokenCount"], usage["thoughtsTokenCount"]) == (1600, 2300)
    assert usage["totalTokenCount"] == usage["promptTokenCount"] + 3900
    assert thought.tokens_out == 3900

    reasoned = await _complete(double, "openai")
    assert reasoned.raw["usage"]["completion_tokens"] == 3900
    assert reasoned.raw["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2300
    assert reasoned.tokens_out == 3900

    # `output_tokens` is Anthropic's inclusive billing
    # total; adding the breakdown would bill thinking twice.
    thought_through = await _complete(double, "anthropic")
    assert thought_through.raw["usage"]["output_tokens"] == 3900
    assert thought_through.raw["usage"]["output_tokens_details"] == {"thinking_tokens": 2300}
    assert thought_through.tokens_out == 3900

    await _scenario(double, thoughts=False, prompt_tokens=14230)
    plain = await _complete(double, "gemini")
    assert "thoughtsTokenCount" not in plain.raw["usageMetadata"]
    assert (plain.tokens_in, plain.tokens_out) == (14230, 1600)
    assert (await _complete(double, "openai")).tokens_out == 1600
    unthought = await _complete(double, "anthropic")
    assert "output_tokens_details" not in unthought.raw["usage"] and unthought.tokens_out == 1600


async def test_a_listed_gemini_model_can_still_refuse_to_generate(double):
    """The 2.5 family is still listed but `generateContent` answers 404: listed is not usable."""
    async with _fetcher(double) as fetcher:
        listed = await client.probe(fetcher, "gemini", key=double.KEYS["gemini"], model="gemini-2.5-flash")
    assert listed["ok"] is True and listed["model_listed"] is True
    with pytest.raises(client.LLMError) as caught:
        await _complete(double, "gemini", model="gemini-2.5-flash")
    assert caught.value.status == 404 and caught.value.retryable is False
    assert "NOT_FOUND: models/gemini-2.5-flash is no longer available to new users" in str(caught.value)

    headers = {"x-api-key": double.KEYS["anthropic"], "anthropic-version": "2023-06-01"}
    page = await _direct(double, "GET", "https://api.anthropic.com/v1/models?limit=1001", headers=headers)
    assert page.status_code == 400 and page.json()["error"]["type"] == "invalid_request_error"


async def test_a_provider_route_answers_only_on_its_own_host(double):
    """The Host header is how the double knows which provider it is being."""
    auth = {"Authorization": f"Bearer {double.KEYS['openai']}"}
    assert (await _direct(double, "POST", "https://api.openai.com/v1/messages", headers=auth,
                          json=_openai_body())).status_code == 404
    assert (await _direct(double, "GET", "https://generativelanguage.googleapis.com/v1/models",
                          headers={"x-goog-api-key": double.KEYS["gemini"]})).status_code == 404


async def test_the_control_surface_refuses_an_envelope_its_provider_does_not_document(double):
    """A scenario is one of each provider's DOCUMENTED ways to fail, never an invented shape."""
    assert (await _scenario(double, provider="gemini", envelope="refusal")).status_code == 422
    assert (await _scenario(double, envelope="prose")).status_code == 422
    assert (await _scenario(double, content="nonsense")).status_code == 422
    # A price setting the provider does not offer, likewise.
    assert (await _scenario(double, project_tier="turbo")).status_code == 422
    assert (await _scenario(double, geo="eu")).status_code == 422
    ok = await _scenario(double, provider="openai", envelope="refusal", posture="stubborn")
    assert ok.status_code == 200
    assert ok.json()["scenarios"]["openai"]["envelope"] == "refusal"
    assert ok.json()["scenarios"]["gemini"]["envelope"] == "normal"
    await _direct(double, "POST", "http://fake-llm/_test/reset")
    assert double.state.scenarios["openai"].envelope == "normal"


def test_the_double_imports_nothing_of_the_app_and_runs_without_its_settings():
    """A double that imported `spielplan` would agree with the code by construction."""
    tree = ast.parse(DOUBLE.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    assert "spielplan" not in imported, imported
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('fake_llm', {str(DOUBLE)!r})\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['fake_llm'] = m\n"
        "spec.loader.exec_module(m)\n"
        "assert 'spielplan' not in sys.modules\n"
        "import asyncio, httpx\n"
        "async def ask():\n"
        "    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app)) as http:\n"
        "        answer = await http.get('http://fake-llm/_test/state')\n"
        "        print(answer.status_code, sorted(answer.json()['scenarios']))\n"
        "asyncio.run(ask())\n"
    )
    env = {name: value for name, value in os.environ.items() if name.upper() != "PYTHONPATH"}
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env,
                          cwd=str(REPO / "ops"), timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "200 ['anthropic', 'gemini', 'openai']", done.stdout
