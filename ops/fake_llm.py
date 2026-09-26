"""A fake of the three LLM providers (§9), mounted by tests through `httpx.ASGITransport`; never shipped.
It answers and refuses as each provider's published reference says, never stricter. Its default
answer is schema-valid with one fabricated term, so the validator is what gets tested.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROVIDERS = ("anthropic", "openai", "gemini")

HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
}

# None of these is a real credential (decision 435).
KEYS = {
    "anthropic": os.environ.get("FAKE_LLM_ANTHROPIC_KEY", "sk-ant-fake-double-anthropic-key"),
    "openai": os.environ.get("FAKE_LLM_OPENAI_KEY", "sk-fake-double-openai-key"),
    "gemini": os.environ.get("FAKE_LLM_GEMINI_KEY", "fake-double-gemini-key"),
}

# A model outside this list is a 404, as on the real providers. Gemini 2.5 Flash is listed but
# retired: `generateContent` answers 404 "no longer available to new users".
MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-sonnet-5", "claude-opus-5", "claude-opus-5-5", "claude-fable-5-1",
                  "claude-mythos-5-1"),
    "openai": ("gpt-5.6-terra", "gpt-5-mini"),
    "gemini": ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash"),
}
RETIRED: dict[str, frozenset[str]] = {"gemini": frozenset({"gemini-2.5-flash"})}

# Anthropic's errors page: these answer a forced `tool_choice` with a 400.
FORCED_TOOL_REFUSED = frozenset({"claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"})
# The ones with safety classifiers that can return `refusal`; on any other model it answers normally.
CLASSIFIED = frozenset({"claude-opus-5", "claude-opus-5-5", "claude-fable-5-1"})

# The corpus's retry opening. Not imported from the app; `test_fake_llm.py` holds the two equal.
RETRY_MARKER = "Your previous answer was rejected:"

CONTENTS = ("fabricate", "unquotable", "salience", "clean")
POSTURES = ("comply", "stubborn")
# Each provider's documented ways for an answer to go wrong, and only those. Anthropic's `refusal`
# is a 200 with empty content whose usage is reported and NOT billed; Gemini's `safety` ends a
# candidate after generation, `blocked` refuses the prompt before it.
ENVELOPES: dict[str, tuple[str, ...]] = {
    "anthropic": ("normal", "max_tokens", "prose", "refusal"),
    "openai": ("normal", "refusal", "max_tokens"),
    "gemini": ("normal", "max_tokens", "blocked", "safety"),
}

# The server errors each provider documents, answered instead of an answer when a scenario names
# one, and billed nothing (only Google says so; for the other two it is decision 436's choice).
# `fault_when` "retry" fails only a retry. Gemini's 429 is a daily quota refused, with the
# `QuotaFailure` detail whose `quotaId` the adapter reads for "PerDay".
FAULTS: dict[str, tuple[int, ...]] = {
    "anthropic": (500, 529),
    "openai": (500, 503),
    "gemini": (429, 500, 503, 504),
}
GEMINI_DAILY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"

# Account-level price settings applied to a request that names none, and reported on the envelope:
# OpenAI's project `service_tier` ("fast" is reported as "priority"), Anthropic's `inference_geo`.
PROJECT_TIERS = ("default", "flex", "priority")
GEOS = ("global", "us")
_SERVED_TIER = {"default": "default", "flex": "flex", "priority": "priority", "fast": "priority"}

# Anthropic's pricing page: the 4.7+ tokenizer makes ~30% more tokens than 4 chars/token, and tool
# use adds a system prompt per model, as (auto/none, any/tool) token counts.
ANTHROPIC_TOKENIZER = 1.3
ANTHROPIC_TOOL_PROMPT: dict[str, tuple[int, int]] = {
    "claude-sonnet-5": (354, 474),
    "claude-opus-5": (286, 406),
}
FAULT_WHEN = ("always", "retry")

# OpenAI models that bill a prompt-cache write (GPT-5.6 and later, on by default, 1,024-token minimum).
CACHE_WRITES = frozenset({"gpt-5.6-terra"})
CACHE_MIN_TOKENS = 1024
# Models whose reasoning effort can be `none`, the one setting under which a temperature is taken.
REASONING_NONE = frozenset({"gpt-5.6-terra"})

# Rule 2's "10-15 words is ideal", at its floor.
REAL_TAGS = 3
QUOTE_WORDS = 10


@dataclass
class Scenario:
    """How one provider answers. Usage defaults are the corpus's gemini-3.6-flash measurement;
    `prompt_tokens` None estimates from the request's length."""

    content: str = "fabricate"
    posture: str = "comply"
    envelope: str = "normal"
    thoughts: bool = True
    prompt_tokens: int | None = None
    output_tokens: int = 1600
    thought_tokens: int = 2300
    fault: int | None = None
    fault_when: str = "always"
    project_tier: str = "default"
    geo: str = "global"


class State:
    def __init__(self) -> None:
        self.scenarios: dict[str, Scenario] = {p: Scenario() for p in PROVIDERS}
        self.requests: list[dict[str, Any]] = []
        # Keyed on the whole request text: only an identical prompt reads an implicit cache write.
        self.cached: set[str] = set()

    def reset(self) -> None:
        self.__init__()


state = State()
router = APIRouter()


# --- what was sent, recorded without the secret -----------------------------------------------------


def _scrub(text: str) -> tuple[str, bool]:
    """The text with the double's keys redacted, and whether one was in it (§9's header-only rule).
    Read decoded, because `%2D` is still a hyphen to a server."""
    decoded = unquote(text)
    found = any(key and key in decoded for key in KEYS.values())
    if found:
        text = decoded
        for key in KEYS.values():
            text = text.replace(key, "[redacted]") if key else text
    return text, found


def _record(request: Request, provider: str | None, status: int, **extra: Any) -> None:
    url, key_in_url = _scrub(str(request.url))
    state.requests.append({
        "provider": provider,
        "method": request.method,
        "url": url,
        "headers": sorted({name.lower() for name in request.headers}),
        "key_in_url": key_in_url,
        "status": status,
        **extra,
    })


def _provider_of(request: Request) -> str | None:
    host = (request.url.hostname or "").lower()
    return next((p for p, h in HOSTS.items() if h == host), None)


# --- reading the request the way a model reads it ---------------------------------------------------

_TERM_LINE = re.compile(r"^([A-Za-z0-9_]+\.[A-Za-z0-9_\-]+)(?=\s|$)")
_FACET_LINE = re.compile(r"^## facet ([A-Za-z0-9_\-]+)")
# A pack section marker, `[imdb:3]`; not the index-less `[type]` or `[note]` header lines.
_MARKER = re.compile(r"^\[([A-Za-z0-9_\-]+:\d+)\]\s*$")
_WORD = re.compile(r"\S+")

Note = dict[str, Any]


@dataclass
class _Request:
    system: str
    user: str
    pack: str
    retry: str | None
    vocabulary: list[tuple[str, str]] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block["text"] for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return []


def _read(system: str, user: str) -> _Request:
    """The retry text is cut off before the pack is read: it is appended to the same user message."""
    pack, marker, tail = user.partition(RETRY_MARKER)
    req = _Request(system=system, user=user, pack=pack, retry=(marker + tail) if marker else None)
    in_vocabulary, facet = False, ""
    for line in system.splitlines():
        if line.startswith("VOCABULARY."):
            in_vocabulary = True
        elif in_vocabulary and (heading := _FACET_LINE.match(line)):
            facet = heading.group(1)
        elif in_vocabulary and facet and (term := _TERM_LINE.match(line.strip())):
            req.vocabulary.append((facet, term.group(1)))
    sections: list[tuple[str, list[str]]] = []
    for line in pack.splitlines():
        if found := _MARKER.match(line.strip()):
            sections.append((found.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    req.sections = [(source, "\n".join(lines).strip()) for source, lines in sections
                    if _WORD.search("\n".join(lines))]
    return req


def _span(text: str, skip: int, count: int = QUOTE_WORDS) -> str:
    """`count` words from word `skip`, cut from the text itself so the quote is verbatim (rule 2)."""
    words = list(_WORD.finditer(text))
    if not words:
        return ""
    start = min(skip, max(len(words) - count, 0))
    chosen = words[start:start + count]
    return text[chosen[0].start():chosen[-1].end()]


def _invented_term(req: _Request) -> str:
    """A term id under a real facet whose tail no vocabulary term has, so the prefix repair cannot
    rescue it: the verdict is `unknown_term`."""
    facets = [facet for facet, _term in req.vocabulary]
    head = facets[0] if facets else "themes"
    tails = {term.partition(".")[2] for _facet, term in req.vocabulary}
    candidates = ["mecha", "kaiju_attack", "heist_crew"] + [f"invented_{n}" for n in range(1, 99)]
    return f"{head}.{next(tail for tail in candidates if tail not in tails)}"


def _tags(req: _Request, content: str) -> list[dict[str, Any]]:
    """Real tags that verify, plus at most one EXTRA tag breaking the one rule `content` names, so a
    retry that drops it leaves a clean answer."""
    facets = list(dict.fromkeys(facet for facet, _term in req.vocabulary))
    first = {facet: next(t for f, t in req.vocabulary if f == facet) for facet in facets}
    sections = req.sections or [("", req.pack)]
    tags: list[dict[str, Any]] = []
    for i, facet in enumerate(facets[:REAL_TAGS]):
        source, text = sections[i % len(sections)]
        tags.append({"term": first[facet], "salience": (2, 3, 1)[i % 3], "source": source,
                     "quote": _span(text, (i // len(sections)) * QUOTE_WORDS)})
    if content == "clean":
        return tags
    used = {tag["term"] for tag in tags}
    spare = next((term for _facet, term in req.vocabulary if term not in used), None)
    source, text = sections[-1]
    quote = _span(text, QUOTE_WORDS)
    if content == "fabricate" or spare is None:
        tags.append({"term": _invented_term(req), "salience": 2, "source": source, "quote": quote})
    elif content == "unquotable":
        invented = f"{quote} and then the double wrote the rest"
        while invented in req.pack:
            invented += " again"
        tags.append({"term": spare, "salience": 2, "source": source, "quote": invented})
    else:
        tags.append({"term": spare, "salience": 4, "source": source, "quote": quote})
    return tags


def _named(term: str, retry: str) -> bool:
    """A whole id: `mood.tense` and not inside `mood.tense_x`."""
    return re.search(rf"(?<![A-Za-z0-9_.]){re.escape(term)}(?![A-Za-z0-9_.\-])", retry) is not None


def _answer(req: _Request, sc: Scenario) -> list[dict[str, Any]]:
    tags = _tags(req, sc.content)
    if req.retry is not None and sc.posture == "comply":
        tags = [tag for tag in tags if not _named(tag["term"], req.retry)]
    return tags


def _note(req: _Request, model: str, envelope: str, tags: list[dict[str, Any]],
          usage: dict[str, Any] | None = None, service_tier: str | None = None) -> Note:
    """What the log keeps about an answer. `usage` is what the provider bills (None where it bills
    nothing), so a test holds the meter to this log (decision 436)."""
    return {"model": model, "retry": req.retry, "envelope": envelope,
            "terms": [tag["term"] for tag in tags] if envelope in ("normal", "unforced") else [],
            "usage": usage, "service_tier": service_tier}


def _faulted(req: _Request, sc: Scenario, provider: str) -> JSONResponse | None:
    if sc.fault is None or (sc.fault_when == "retry" and req.retry is None):
        return None
    if provider == "anthropic":
        if sc.fault == 529:
            return _anthropic_error(529, "overloaded_error", "Overloaded")
        return _anthropic_error(sc.fault, "api_error", "Internal server error")
    if provider == "openai":
        # The page publishes the 503's type and code and neither for the 500.
        if sc.fault == 503:
            return _openai_error(503, "The requested model is temporarily overloaded.",
                                 kind="service_unavailable_error", code="server_is_overloaded")
        return _openai_error(sc.fault, "The server had an error while processing your request.",
                             kind="server_error")
    if sc.fault == 429:
        return _gemini_error(
            429, "RESOURCE_EXHAUSTED",
            "You exceeded your current quota, please check your plan and billing details.",
            details=[{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                "quotaId": GEMINI_DAILY_QUOTA_ID,
                "quotaDimensions": {"location": "global", "model": "gemini-3.7-flash"},
                "quotaValue": "250"}]}])
    grpc = {500: "INTERNAL", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}[sc.fault]
    return _gemini_error(sc.fault, grpc, "The service is currently unavailable.")


def _prompt_tokens(req: _Request, sc: Scenario) -> int:
    if sc.prompt_tokens is not None:
        return sc.prompt_tokens
    return max(1, (len(req.system) + len(req.user)) // 4)


def _anthropic_prompt_tokens(req: _Request, sc: Scenario, model: str, tools: list[dict[str, Any]],
                             forced: bool) -> int:
    if sc.prompt_tokens is not None:
        return sc.prompt_tokens
    chars = len(req.system) + len(req.user) + (len(json.dumps(tools)) if tools else 0)
    tool_prompt = ANTHROPIC_TOOL_PROMPT.get(model, (0, 0))[1 if forced else 0] if tools else 0
    return max(1, -(-int(chars * ANTHROPIC_TOKENIZER) // 4)) + tool_prompt


async def _body(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


# --- Anthropic: POST /v1/messages ---------------------------------------------------------------------


def _anthropic_error(status: int, kind: str, message: str) -> JSONResponse:
    """https://docs.anthropic.com/en/api/errors"""
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message},
                         "request_id": f"req_{uuid.uuid4().hex[:24]}"}, status_code=status)


def _anthropic_auth(request: Request) -> JSONResponse | None:
    key = request.headers.get("x-api-key")
    if not key:
        return _anthropic_error(401, "authentication_error", "x-api-key header is required")
    if key != KEYS["anthropic"]:
        return _anthropic_error(401, "authentication_error", "invalid x-api-key")
    if not request.headers.get("anthropic-version"):
        return _anthropic_error(400, "invalid_request_error", "anthropic-version: header is required")
    return None


@router.post("/v1/messages")
async def anthropic_messages(request: Request) -> JSONResponse:
    if _provider_of(request) != "anthropic":
        return _not_here(request)
    refused = _anthropic_auth(request)
    resp, note = (refused, {}) if refused else await _anthropic_answer(request)
    _record(request, "anthropic", resp.status_code, key_header="x-api-key" in request.headers, **note)
    return resp


async def _anthropic_answer(request: Request) -> tuple[JSONResponse, Note]:
    body = await _body(request)
    if not isinstance(body, dict):
        return _anthropic_error(400, "invalid_request_error", "the request body is not valid JSON"), {}
    for required in ("model", "max_tokens", "messages"):
        if required not in body:
            return _anthropic_error(400, "invalid_request_error", f"{required}: Field required"), {}
    if body["model"] not in MODELS["anthropic"]:
        return _anthropic_error(404, "not_found_error", f"model: {body['model']}"), {}
    # The Messages reference, for every model served here: temperature only 1.0, top_p only >= 0.99,
    # top_k never. The wording is the double's; the page publishes none.
    if "temperature" in body and body["temperature"] != 1:
        return _anthropic_error(400, "invalid_request_error",
                                "temperature: is not supported for this model"), {}
    if "top_p" in body and not (isinstance(body["top_p"], int | float) and body["top_p"] >= 0.99):
        return _anthropic_error(400, "invalid_request_error", "top_p: is not supported for this model"), {}
    if "top_k" in body:
        return _anthropic_error(400, "invalid_request_error", "top_k: is not supported for this model"), {}
    tools = [tool for tool in body.get("tools") or [] if isinstance(tool, dict)]
    names = [tool.get("name") for tool in tools]
    for i, tool in enumerate(tools):
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return _anthropic_error(400, "invalid_request_error",
                                    f"tools.{i}.input_schema.type: Input should be 'object'"), {}
    choice = body.get("tool_choice") if isinstance(body.get("tool_choice"), dict) else {"type": "auto"}
    forced: str | None = None
    if choice.get("type") == "tool":
        if choice.get("name") not in names:
            return _anthropic_error(400, "invalid_request_error",
                                    f"tool_choice.name: no tool named {choice.get('name')!r}"), {}
        forced = choice["name"]
    elif choice.get("type") == "any" and names:
        forced = names[0]
    # 4.7 and later removed manual extended thinking: a 400 whatever `tool_choice` says.
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return _anthropic_error(
            400, "invalid_request_error",
            '"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and'
            ' "output_config.effort" to control thinking behavior.'), {}
    if choice.get("type") in ("tool", "any") and body["model"] in FORCED_TOOL_REFUSED:
        return _anthropic_error(400, "invalid_request_error",
                                'tool_choice: type "tool" and "any" are not supported for this model.'), {}

    messages = body["messages"] if isinstance(body["messages"], list) else []
    user = "\n\n".join(text for m in messages if isinstance(m, dict) and m.get("role") == "user"
                       for text in _texts(m.get("content")))
    req = _read("\n\n".join(_texts(body.get("system"))), user)
    sc = state.scenarios["anthropic"]
    if faulted := _faulted(req, sc, "anthropic"):
        return faulted, _note(req, body["model"], f"fault {sc.fault}", [])
    tags = _answer(req, sc)
    envelope = sc.envelope if forced else "unforced"
    if envelope == "refusal" and body["model"] not in CLASSIFIED:
        envelope = "normal"
    # Thinking is on by default: an empty `thinking` block comes first in `content`, and its tokens
    # are INSIDE `output_tokens`, itemised under `output_tokens_details.thinking_tokens`.
    thought = sc.thought_tokens if sc.thoughts else 0
    thinking = ([{"type": "thinking", "thinking": "", "signature": f"Eo{uuid.uuid4().hex}"}]
                if thought else [])
    stop, out = "tool_use", sc.output_tokens + thought
    if envelope == "normal":
        content: list[dict[str, Any]] = thinking + [
            {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:24]}", "name": forced,
             "input": {"tags": tags}}]
    elif envelope == "max_tokens":
        # Billed in full. Under a forced tool the truncated answer is an incomplete `tool_use`
        # block, never text: here the first tag only.
        stop, out = "max_tokens", int(body["max_tokens"])
        content = thinking + [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:24]}",
                               "name": forced, "input": {"tags": tags[:1]}}]
    elif envelope == "prose":
        stop = "end_turn"
        content = thinking + [{"type": "text", "text": "I would rather describe this film in prose "
                                                       "than call the tool: it is a tense, patient "
                                                       "piece of work."}]
    elif envelope == "refusal":
        # Before any output: no content, `output_tokens` 0, the prompt counted and not charged.
        stop, out = "refusal", 0
        content = []
    else:
        # No tool was forced, so the JSON comes as text, not as a `tool_use` block.
        stop = "end_turn"
        content = thinking + [{"type": "text", "text": json.dumps({"tags": tags})}]
    geo = body.get("inference_geo") if body.get("inference_geo") in GEOS else sc.geo
    usage: dict[str, Any] = {
        "input_tokens": _anthropic_prompt_tokens(req, sc, body["model"], tools, forced is not None),
        "output_tokens": out, "inference_geo": geo}
    if thought and out:
        usage["output_tokens_details"] = {"thinking_tokens": min(thought, out)}
    # `stop_details` is null for every stop reason but `refusal`, and always present.
    details = ({"type": "refusal", "category": "general_harms",
                "explanation": "This request was declined because it conflicts with Anthropic's Usage"
                               " Policy."} if stop == "refusal" else None)
    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "model": body["model"], "content": content, "stop_reason": stop, "stop_details": details,
        "stop_sequence": None, "usage": usage,
    }), _note(req, body["model"], envelope, tags, None if stop == "refusal" else usage)


# --- OpenAI: POST /v1/chat/completions ----------------------------------------------------------------


def _openai_error(status: int, message: str, *, kind: str = "invalid_request_error",
                  param: str | None = None, code: str | None = None) -> JSONResponse:
    """https://platform.openai.com/docs/guides/error-codes"""
    return JSONResponse({"error": {"message": message, "type": kind, "param": param, "code": code}},
                        status_code=status)


def _openai_auth(request: Request) -> JSONResponse | None:
    auth = request.headers.get("authorization", "")
    if not auth:
        return _openai_error(401, "You didn't provide an API key. You need to provide your API key in "
                                  "an Authorization header using Bearer auth (i.e. Authorization: "
                                  "Bearer YOUR_KEY).")
    # The real message quotes a masked key; this quotes none, so no fragment reaches a CI log.
    if auth != f"Bearer {KEYS['openai']}":
        return _openai_error(401, "Incorrect API key provided. You can find your API key at "
                                  "https://platform.openai.com/account/api-keys.",
                             code="invalid_api_key")
    return None


# The keywords OpenAI's strict mode still refuses. `minimum`, `maximum`, `minItems`, `maxItems`,
# `pattern` and `format` are supported now; `anyOf` is the one composition keyword it takes.
# `maxLength` and `patternProperties` are refused on thin evidence: the adapter strips them anyway.
OPENAI_NOT_PERMITTED = frozenset({
    "minLength", "maxLength",
    "patternProperties", "unevaluatedProperties", "propertyNames", "minProperties", "maxProperties",
    "unevaluatedItems", "contains", "minContains", "maxContains", "uniqueItems",
    "allOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else",
})


def _strict_refusal(schema: Any, name: str) -> str | None:
    """The first rule a strict schema breaks, in the real server's words, or None."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        got = schema.get("type") if isinstance(schema, dict) else type(schema).__name__
        return (f"Invalid schema for response_format '{name}': schema must be a JSON Schema of "
                f"'type: \"object\"', got 'type: \"{got}\"'.")
    return _strict_walk(schema, (), name)


def _strict_walk(node: Any, path: tuple[Any, ...], name: str) -> str | None:
    if not isinstance(node, dict):
        return None
    where = f"Invalid schema for response_format '{name}': In context={path!r}, "
    for keyword in node:
        if keyword in OPENAI_NOT_PERMITTED:
            return where + f"'{keyword}' is not permitted."
    if node.get("type") == "object" or "properties" in node:
        if node.get("additionalProperties") is not False:
            return where + "'additionalProperties' is required to be supplied and to be false."
        required = node.get("required")
        for prop in node.get("properties") or {}:
            if not isinstance(required, list) or prop not in required:
                return where + ("'required' is required to be supplied and to be an array including "
                                f"every key in properties. Missing '{prop}'.")
    children: list[tuple[tuple[Any, ...], Any]] = []
    for key in ("properties", "$defs", "definitions"):
        if isinstance(node.get(key), dict):
            children += [((key, prop), sub) for prop, sub in node[key].items()]
    if isinstance(node.get("items"), dict):
        children.append((("items",), node["items"]))
    for key in ("anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            children += [((key, i), sub) for i, sub in enumerate(node[key])]
    for step, sub in children:
        if found := _strict_walk(sub, path + step, name):
            return found
    return None


@router.post("/v1/chat/completions")
async def openai_chat(request: Request) -> JSONResponse:
    if _provider_of(request) != "openai":
        return _not_here(request)
    refused = _openai_auth(request)
    resp, note = (refused, {}) if refused else await _openai_answer(request)
    _record(request, "openai", resp.status_code,
            key_header=request.headers.get("authorization", "").startswith("Bearer "), **note)
    return resp


async def _openai_answer(request: Request) -> tuple[JSONResponse, Note]:
    body = await _body(request)
    if not isinstance(body, dict):
        return _openai_error(400, "We could not parse the JSON body of your request."), {}
    for required in ("model", "messages"):
        if required not in body:
            return _openai_error(400, f"Missing required parameter: '{required}'.", param=required,
                                 code="missing_required_parameter"), {}
    if body["model"] not in MODELS["openai"]:
        return _openai_error(404, f"The model `{body['model']}` does not exist or you do not have "
                                  "access to it.", code="model_not_found"), {}
    # Reasoning models refuse `max_tokens`, and any non-default temperature unless effort is `none`.
    if "max_tokens" in body:
        return _openai_error(400, "Unsupported parameter: 'max_tokens' is not supported with this "
                                  "model. Use 'max_completion_tokens' instead.",
                             param="max_tokens", code="unsupported_parameter"), {}
    reasons = not (body["model"] in REASONING_NONE and body.get("reasoning_effort") == "none")
    if "temperature" in body and body["temperature"] != 1 and reasons:
        return _openai_error(400, f"Unsupported value: 'temperature' does not support "
                                  f"{body['temperature']} with this model. Only the default (1) value "
                                  "is supported.", param="temperature", code="unsupported_value"), {}
    fmt = body.get("response_format") if isinstance(body.get("response_format"), dict) else {}
    if fmt.get("type") == "json_schema":
        spec = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else {}
        if not spec.get("name"):
            return _openai_error(400, "Missing required parameter: 'response_format.json_schema.name'.",
                                 param="response_format.json_schema.name",
                                 code="missing_required_parameter"), {}
        if spec.get("strict") and (refusal := _strict_refusal(spec.get("schema"), spec["name"])):
            return _openai_error(400, refusal, param="response_format"), {}

    system: list[str] = []
    user: list[str] = []
    for message in body["messages"] if isinstance(body["messages"], list) else []:
        if not isinstance(message, dict):
            continue
        if message.get("role") in ("system", "developer"):
            system += _texts(message.get("content"))
        elif message.get("role") == "user":
            user += _texts(message.get("content"))
    req = _read("\n\n".join(system), "\n\n".join(user))
    sc = state.scenarios["openai"]
    if faulted := _faulted(req, sc, "openai"):
        return faulted, _note(req, body["model"], f"fault {sc.fault}", [])
    tags = _answer(req, sc)
    # `completion_tokens` INCLUDES the reasoning; `reasoning_tokens` itemises it.
    reasoning = sc.thought_tokens if sc.thoughts else 0
    completion = sc.output_tokens + reasoning
    finish, content, refusal_text = "stop", json.dumps({"tags": tags}), None
    if sc.envelope == "refusal":
        content, refusal_text = None, "I'm sorry, I cannot assist with that request."
    elif sc.envelope == "max_tokens":
        # The cap spent on reasoning before any answer: nothing visible, every token billed.
        finish, content = "length", ""
        completion = reasoning = int(body.get("max_completion_tokens") or completion)
    prompt = _prompt_tokens(req, sc)
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": prompt + completion,
             "prompt_tokens_details": _openai_cache(body, req, prompt),
             "completion_tokens_details": {"reasoning_tokens": reasoning}}
    tier = _SERVED_TIER.get(body.get("service_tier"), sc.project_tier)
    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:29]}", "object": "chat.completion",
        "created": int(time.time()), "model": body["model"],
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content, "refusal": refusal_text},
                     "logprobs": None, "finish_reason": finish}],
        "usage": usage, "service_tier": tier,
    }), _note(req, body["model"], sc.envelope, tags, usage, tier)


def _openai_cache(body: dict[str, Any], req: _Request, prompt: int) -> dict[str, int]:
    """`cached_tokens` and `cache_write_tokens`, both INSIDE `prompt_tokens`. The first sight of a
    prompt writes all of it and an identical later one reads all of it."""
    options = body.get("prompt_cache_options")
    explicit = isinstance(options, dict) and options.get("mode") == "explicit"
    if body["model"] not in CACHE_WRITES or explicit or prompt < CACHE_MIN_TOKENS:
        return {"cached_tokens": 0, "cache_write_tokens": 0}
    seen = f"{req.system}\x00{req.user}"
    if seen in state.cached:
        return {"cached_tokens": prompt, "cache_write_tokens": 0}
    state.cached.add(seen)
    return {"cached_tokens": 0, "cache_write_tokens": prompt}


# --- Gemini: POST /v1beta/models/{model}:generateContent -----------------------------------------------


def _gemini_error(status: int, grpc: str, message: str, reason: str | None = None,
                  details: list[dict[str, Any]] | None = None) -> JSONResponse:
    """https://ai.google.dev/gemini-api/docs/troubleshooting"""
    error: dict[str, Any] = {"code": status, "message": message, "status": grpc}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                             "domain": "googleapis.com",
                             "metadata": {"service": "generativelanguage.googleapis.com"}}]
    if details:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status)


def _gemini_auth(request: Request) -> JSONResponse | None:
    """`x-goog-api-key` or `?key=`, both of which the real API accepts; the log records which."""
    key = request.headers.get("x-goog-api-key") or request.query_params.get("key")
    if not key:
        return _gemini_error(403, "PERMISSION_DENIED",
                             "Method doesn't allow unregistered callers (callers without established "
                             "identity). Please use API Key or other form of API consumer identity to "
                             "call this API.")
    if key != KEYS["gemini"]:
        return _gemini_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.",
                             reason="API_KEY_INVALID")
    return None


# `responseSchema` is parsed as a protocol buffer: any other name (`additionalProperties`) is a 400,
# and each field is taken in both its camelCase and snake_case spelling.
GEMINI_SCHEMA_FIELDS = frozenset({
    "type", "format", "title", "description", "nullable", "enum", "maxItems", "minItems",
    "properties", "required", "minProperties", "maxProperties", "minLength", "maxLength", "pattern",
    "example", "anyOf", "propertyOrdering", "default", "items", "minimum", "maximum",
})
_GEMINI_NAMES = GEMINI_SCHEMA_FIELDS | {re.sub(r"([A-Z])", r"_\1", name).lower()
                                        for name in GEMINI_SCHEMA_FIELDS}


def _gemini_schema_refusal(node: Any, path: str) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in node:
        if key not in _GEMINI_NAMES:
            return f"Invalid JSON payload received. Unknown name \"{key}\" at '{path}': Cannot find field."
    props = node.get("properties")
    children = [(f"{path}.properties[{i}].value", sub)
                for i, sub in enumerate(props.values() if isinstance(props, dict) else [])]
    children.append((f"{path}.items", node.get("items")))
    children += [(f"{path}.any_of[{i}]", sub)
                 for i, sub in enumerate(node.get("anyOf") or node.get("any_of") or [])]
    for where, sub in children:
        if found := _gemini_schema_refusal(sub, where):
            return found
    return None


@router.post("/v1beta/models/{target}")
async def gemini_generate(target: str, request: Request) -> JSONResponse:
    if _provider_of(request) != "gemini":
        return _not_here(request)
    model, _sep, method = target.rpartition(":")
    note: Note = {}
    if method != "generateContent" or not model:
        resp = _gemini_error(404, "NOT_FOUND", f"Method not found: {target}")
    elif refused := _gemini_auth(request):
        resp = refused
    else:
        resp, note = await _gemini_answer(request, model)
    _record(request, "gemini", resp.status_code, key_header="x-goog-api-key" in request.headers, **note)
    return resp


async def _gemini_answer(request: Request, model: str) -> tuple[JSONResponse, Note]:
    if model not in MODELS["gemini"]:
        return _gemini_error(404, "NOT_FOUND",
                             f"models/{model} is not found for API version v1beta, or is not supported "
                             "for generateContent. Call ListModels to see the list of available models "
                             "and their supported methods."), {}
    if model in RETIRED.get("gemini", frozenset()):
        return _gemini_error(404, "NOT_FOUND", f"models/{model} is no longer available to new users."), {}
    body = await _body(request)
    if not isinstance(body, dict):
        return _gemini_error(400, "INVALID_ARGUMENT", "Invalid JSON payload received."), {}
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        return _gemini_error(400, "INVALID_ARGUMENT",
                             "* GenerateContentRequest.contents: contents is not specified"), {}
    config = body.get("generationConfig") or body.get("generation_config") or {}
    schema = config.get("responseSchema", config.get("response_schema"))
    if refusal := _gemini_schema_refusal(schema, "generation_config.response_schema"):
        return _gemini_error(400, "INVALID_ARGUMENT", refusal), {}

    instruction = body.get("systemInstruction") or body.get("system_instruction") or {}
    system = "\n\n".join(_texts(instruction.get("parts")) if isinstance(instruction, dict) else [])
    user = "\n\n".join(text for c in contents if isinstance(c, dict) and c.get("role", "user") == "user"
                       for text in _texts(c.get("parts")))
    req = _read(system, user)
    sc = state.scenarios["gemini"]
    if faulted := _faulted(req, sc, "gemini"):
        return faulted, _note(req, model, f"fault {sc.fault}", [])
    tags = _answer(req, sc)
    prompt = _prompt_tokens(req, sc)
    # `thoughtsTokenCount` is apart from `candidatesTokenCount`, billed as output, and absent when
    # the model did not think.
    usage: dict[str, int] = {"promptTokenCount": prompt, "candidatesTokenCount": sc.output_tokens}
    if sc.thoughts:
        usage["thoughtsTokenCount"] = sc.thought_tokens
    answer: dict[str, Any] = {}
    if sc.envelope == "blocked":
        answer["promptFeedback"] = {"blockReason": "SAFETY", "safetyRatings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH"}]}
        usage = {"promptTokenCount": prompt}
    elif sc.envelope == "max_tokens":
        # The thinking spent the whole budget: a candidate with no parts, and the cap billed as thought.
        cap = int(config.get("maxOutputTokens") or config.get("max_output_tokens") or sc.thought_tokens)
        answer["candidates"] = [{"content": {"role": "model"}, "finishReason": "MAX_TOKENS", "index": 0}]
        usage = {"promptTokenCount": prompt, "thoughtsTokenCount": cap}
    elif sc.envelope == "safety":
        # Stopped by the safety filter after the model had thought: no parts, the thoughts billed.
        answer["candidates"] = [{"content": {"role": "model"}, "finishReason": "SAFETY", "index": 0,
                                 "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                                                    "probability": "HIGH"}]}]
        usage = {"promptTokenCount": prompt}
        if sc.thoughts:
            usage["thoughtsTokenCount"] = sc.thought_tokens
    else:
        answer["candidates"] = [{"content": {"parts": [{"text": json.dumps({"tags": tags})}],
                                             "role": "model"},
                                 "finishReason": "STOP", "index": 0}]
    usage["totalTokenCount"] = sum(usage.values())
    answer.update({"usageMetadata": usage, "modelVersion": model, "responseId": uuid.uuid4().hex[:24]})
    return JSONResponse(answer), _note(req, model, sc.envelope, tags, usage)


# --- the free models lists, which §6.6's test button reads (decision 433) ------------------------------


@router.get("/v1/models")
async def models_list(request: Request) -> JSONResponse:
    """Anthropic's and OpenAI's lists share a path, so the Host header decides."""
    provider = _provider_of(request)
    if provider == "anthropic":
        resp = _anthropic_auth(request) or _anthropic_models(request)
        key_header = "x-api-key" in request.headers
    elif provider == "openai":
        resp = _openai_auth(request) or JSONResponse({"object": "list", "data": [
            {"id": model, "object": "model", "created": 1753833600, "owned_by": "system"}
            for model in MODELS["openai"]]})
        key_header = request.headers.get("authorization", "").startswith("Bearer ")
    else:
        return _not_here(request)
    _record(request, provider, resp.status_code, key_header=key_header)
    return resp


def _anthropic_models(request: Request) -> JSONResponse:
    """https://docs.anthropic.com/en/api/models-list"""
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        return _anthropic_error(400, "invalid_request_error", "limit: Input should be a valid integer")
    if not 1 <= limit <= 1000:
        return _anthropic_error(400, "invalid_request_error",
                                "limit: Input should be greater than or equal to 1 and less than or "
                                "equal to 1000")
    data = [{"type": "model", "id": model, "display_name": model.replace("-", " ").title(),
             "created_at": "2026-08-01T00:00:00Z"} for model in MODELS["anthropic"]]
    shown = data[:limit]
    return JSONResponse({"data": shown, "has_more": len(data) > limit,
                         "first_id": shown[0]["id"] if shown else None,
                         "last_id": shown[-1]["id"] if shown else None})


@router.get("/v1beta/models")
async def gemini_models(request: Request) -> JSONResponse:
    """https://ai.google.dev/api/models#method:-models.list"""
    if _provider_of(request) != "gemini":
        return _not_here(request)
    resp = _gemini_auth(request)
    if resp is None:
        models = [{"name": f"models/{model}", "version": "001",
                   "displayName": model.replace("-", " ").title(),
                   "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                   "supportedGenerationMethods": ["generateContent", "countTokens"]}
                  for model in MODELS["gemini"]]
        try:
            size = max(min(int(request.query_params.get("pageSize", "50")), 1000), 1)
        except ValueError:
            size = 50
        answer: dict[str, Any] = {"models": models[:size]}
        if len(models) > size:
            answer["nextPageToken"] = "page-2"
        resp = JSONResponse(answer)
    _record(request, "gemini", resp.status_code, key_header="x-goog-api-key" in request.headers)
    return resp


def _not_here(request: Request) -> JSONResponse:
    _record(request, _provider_of(request), 404, key_header=False)
    return JSONResponse({"error": {"message": f"no such route on {request.url.hostname}"}},
                        status_code=404)


# --- control surface (no provider counterpart) --------------------------------------------------------


class ScenarioControl(BaseModel):
    provider: str | None = None
    content: str | None = None
    posture: str | None = None
    envelope: str | None = None
    thoughts: bool | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    fault: int | None = None
    fault_when: str | None = None
    project_tier: str | None = None
    geo: str | None = None


control = APIRouter(prefix="/_test")


@control.post("/scenario")
async def set_scenario(body: ScenarioControl) -> JSONResponse:
    if body.provider is not None and body.provider not in PROVIDERS:
        return JSONResponse({"detail": f"unknown provider {body.provider!r}"}, status_code=422)
    if body.content is not None and body.content not in CONTENTS:
        return JSONResponse({"detail": f"content must be one of {list(CONTENTS)}"}, status_code=422)
    if body.posture is not None and body.posture not in POSTURES:
        return JSONResponse({"detail": f"posture must be one of {list(POSTURES)}"}, status_code=422)
    targets = PROVIDERS if body.provider is None else (body.provider,)
    # An envelope a provider does not document is refused rather than invented.
    for provider in targets:
        if body.envelope is not None and body.envelope not in ENVELOPES[provider]:
            return JSONResponse({"detail": f"{provider} documents no {body.envelope!r} envelope; it has "
                                           f"{list(ENVELOPES[provider])}"}, status_code=422)
        if body.fault is not None and body.fault not in FAULTS[provider]:
            return JSONResponse({"detail": f"{provider} documents no {body.fault} fault here; it has "
                                           f"{list(FAULTS[provider])}"}, status_code=422)
    if body.fault_when is not None and body.fault_when not in FAULT_WHEN:
        return JSONResponse({"detail": f"fault_when must be one of {list(FAULT_WHEN)}"}, status_code=422)
    if body.project_tier is not None and body.project_tier not in PROJECT_TIERS:
        return JSONResponse({"detail": f"project_tier must be one of {list(PROJECT_TIERS)}"},
                            status_code=422)
    if body.geo is not None and body.geo not in GEOS:
        return JSONResponse({"detail": f"geo must be one of {list(GEOS)}"}, status_code=422)
    # `prompt_tokens` and `fault` sent as null go back to their defaults: the estimate, and no fault.
    changes = {name: getattr(body, name) for name in body.model_fields_set
               if name != "provider"
               and (getattr(body, name) is not None or name in ("prompt_tokens", "fault"))}
    for provider in targets:
        state.scenarios[provider] = replace(state.scenarios[provider], **changes)
    return JSONResponse({"scenarios": {p: asdict(sc) for p, sc in state.scenarios.items()}})


@control.post("/reset")
async def reset() -> dict[str, bool]:
    state.reset()
    return {"ok": True}


@control.get("/state")
async def dump() -> dict[str, Any]:
    return {"scenarios": {p: asdict(sc) for p, sc in state.scenarios.items()},
            "requests": state.requests}


def create_app() -> FastAPI:
    app = FastAPI(title="Fake LLM providers", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.include_router(control)
    return app


app = create_app()
