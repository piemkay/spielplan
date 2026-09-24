"""A fake of the three LLM providers M5.5 speaks to. Test infrastructure -- never shipped in the app image.

Spec v2.1 §9, §8 stage 6; M5.5-plan.md phase F (F1, F2) and the first of its §9 risks; decisions 431,
432 and 435.

§9 puts the guarantee in one sentence -- "the schema is a cost-saving device, not the guarantee - the
guarantee is the validator" -- and it can only be tested against an answer that satisfies the request
schema and is still wrong. The plan's F2 adds that "identical across three adapters" is untested
unless one server speaks all three envelopes. Neither can be checked with a mock inside the process
that makes the call: a mock hands back what its author believed a provider sends, and M5.2's last
review cycle measured the price of that belief -- its Jellyfin double agreed with the code where the
real server did not, and every real webhook add was dropped. So this is a real HTTP app, in the spirit
of `ops/fake_jellyfin.py` refusing the admin key on the Played write, that answers the way each
provider's PUBLISHED reference says it answers and refuses what that reference says it refuses.

ONE WAY IN. The backend tests and `ops/m55_exit_criterion.py` mount it through `httpx.ASGITransport`
inside a real `acquire.fetch.Fetcher`, so the adapters keep their production urls and every request
still passes the fetcher's host policy, pacing and breaker. There is no compose service and no port
(decision 435): no provider key is ever needed to run it, and nothing outside a test can reach it.
The Host header each request carries is how this app knows which provider it is being, which is
how the real three are told apart too -- `/v1/models` is two different resources on two hosts, and a
chat completion posted to Anthropic's host is a 404 there, not an answer.

WHERE EACH SHAPE IS READ FROM, and each envelope carries only fields these pages document:

  * Anthropic -- the Messages API reference (https://docs.anthropic.com/en/api/messages), forced tool
    use (https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use), the error
    shapes (https://docs.anthropic.com/en/api/errors) and the models list
    (https://docs.anthropic.com/en/api/models-list); refusals and what they bill
    (https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback).
  * OpenAI -- the chat completion object (https://platform.openai.com/docs/api-reference/chat/object),
    Structured Outputs' supported schemas and refusals
    (https://platform.openai.com/docs/guides/structured-outputs), reasoning tokens
    (https://platform.openai.com/docs/guides/reasoning), error codes
    (https://platform.openai.com/docs/guides/error-codes) and the models list
    (https://platform.openai.com/docs/api-reference/models/list).
  * Gemini -- generateContent and its response, usage and prompt feedback
    (https://ai.google.dev/api/generate-content), the Schema object `responseSchema` is parsed as
    (https://ai.google.dev/api/caching#Schema), thinking (https://ai.google.dev/gemini-api/docs/thinking),
    the error table (https://ai.google.dev/gemini-api/docs/troubleshooting) and the models list
    (https://ai.google.dev/api/models#method:-models.list).

Where the corpus measured a provider against the real server and a reference is silent, the corpus is
the evidence and is cited at the line: `mdc/llm/client.py` was exercised against all three, and its
comments are what a real 400 taught it.

WHAT IT REFUSES, and each refusal is a thing the real server does, never a thing this app happens to
avoid. A double stricter than its server is M5.2's failure turned round -- it fails a request the
provider would have answered, and the code is bent to please the double -- so where the evidence is
thin the double answers rather than refuses. Gemini's documented `?key=` is the sharpest case: the real
API takes the key in the query string, so this does too, and §9's header-only rule is held by reading
what the app SENT (`/_test/state`'s `key_in_url`) rather than by a refusal no server makes.

THE ONE PLACE THIS PARTS FROM ITS BRIEF: `minimum`, `maximum`, `minItems`, `maxItems`, `pattern` and
`format` are ANSWERED under OpenAI's strict mode, not refused. `mdc/llm/client.py:10-13` records that
strict mode rejected them, and it did in 2024; the Structured Outputs guide has since listed all six
as "Supported properties" (quoted in prism-php/prism#533, 2025-08-01), and the refusals reported
since name other keywords. What strict mode still refuses is refused here, in the real server's words:
"'minLength' is not permitted" (coder/mux#1220, 2025-12-18) and "'uniqueItems' is not permitted"
(mastra-ai/mastra#23321, 2026-09-08) are the two reported refused since the guide changed, beside its
structural rules -- every object `additionalProperties: false`, every property `required`
(openai/openai-python#2740 quotes the first) and an object at the root -- and the composition keywords
the guide itself lists as not yet supported (`OPENAI_NOT_PERMITTED`). So the app's stripping of the
six is harmless and no longer necessary; a double that refused them would be the stricter-than-server
shape the paragraph above rules out, and would have said nothing about the code.

THE CONTENT IS BUILT FROM THE REQUEST, never from the app's code. The term ids are parsed out of the
system prompt's vocabulary block (one id per line under `## facet` headings, which is what a model
reads), every quote is cut verbatim out of the pack the user message carries, and every source is one
of that pack's `[source:n]` markers. Nothing here imports `spielplan`: the double stays importable
without the backend's settings (`ops/fake_jellyfin.py`'s constraint), and an answer built from the
app's own vocabulary loader would be the app agreeing with itself.

THE DEFAULT ANSWER IS WRONG ON PURPOSE. The plan's first §9 risk: "A schema-conforming response
containing a fabricated term is the normal case this milestone exists to handle ... the refusing double
must produce one by default rather than on request." So a first attempt is schema-valid and carries
exactly ONE tag whose term the vocabulary does not carry, beside real tags that verify. A request
whose user message carries the corpus's retry opening (`RETRY_MARKER`) is a retry: in the default
`comply` posture the answer drops exactly the tags the retry names, the way a model that read the
violation would; in `stubborn` it repeats the violation, which is the case decision 431 fails
permanently.
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

# One key per provider, env-overridable for the same reason `FAKE_JELLYFIN_API_KEY` is. None of them is
# a real credential, and the exit script never reads a real one from the environment (decision 435).
KEYS = {
    "anthropic": os.environ.get("FAKE_LLM_ANTHROPIC_KEY", "sk-ant-fake-double-anthropic-key"),
    "openai": os.environ.get("FAKE_LLM_OPENAI_KEY", "sk-fake-double-openai-key"),
    "gemini": os.environ.get("FAKE_LLM_GEMINI_KEY", "fake-double-gemini-key"),
}

# The models each provider serves here: the corpus's DEFAULT_MODELS (`mdc/config.py:169-173`), the two
# models its reasoning-token measurement names (`:181-185`), and Gemini 2.5 Flash, which the corpus met
# "still [appearing] in ListModels but `generateContent` answers 404 'no longer available to new users'"
# (`:152-154`). A model outside this list is a 404 on every provider, as on the real three.
#
# Anthropic's list also carries the four current models an admin can pick on §6.6 whose behaviour
# stage 6 depends on, because a double that 404ed them hid both. Claude Opus 5, whose safety
# classifiers can answer `refusal` and which accepts a forced tool; and Claude Opus 5.5, Claude Fable
# 5.1 and Claude Mythos 5.1, which "don't support forced tool use" and answer it with a 400 on every
# request (https://platform.claude.com/docs/en/api/errors, "Forced tool use not supported") -- so the
# test button read `model_listed` False here while the real list says True, and a paid call through
# the real provider failed a title for good where this double said the model did not exist.
# [M5.5 review cycle 1, M55-DBL-04]
MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-sonnet-5", "claude-opus-5", "claude-opus-5-5", "claude-fable-5-1",
                  "claude-mythos-5-1"),
    "openai": ("gpt-5.6-terra", "gpt-5-mini"),
    "gemini": ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash"),
}
RETIRED: dict[str, frozenset[str]] = {"gemini": frozenset({"gemini-2.5-flash"})}

# The Anthropic models above that refuse `tool_choice` "tool" or "any", in the errors page's words:
# "tool_choice: type "tool" and "any" are not supported for this model." (see `MODELS`).
FORCED_TOOL_REFUSED = frozenset({"claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"})
# The ones served here that "include safety classifiers that can decline a request" -- the page names
# Claude Fable 5.1, Claude Fable 5, Claude Opus 5.5 and Claude Opus 5
# (https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback). Sonnet 5 is not among
# them, so the `refusal` envelope set on Sonnet 5 is answered as that model answers: normally.
CLASSIFIED = frozenset({"claude-opus-5", "claude-opus-5-5", "claude-fable-5-1"})

# The corpus's retry opening, `mdc/aspects/prompt.py:674`, sent after the original user prompt under the
# same system prompt (`mdc/sources/llm.py:86-88`). Held here rather than imported, for the reason the
# module docstring gives; `test_fake_llm.py` asserts the app still sends these words.
RETRY_MARKER = "Your previous answer was rejected:"

CONTENTS = ("fabricate", "unquotable", "salience", "clean")
POSTURES = ("comply", "stubborn")
# Each provider's documented ways for an answer to go wrong, and only those: OpenAI's `message.refusal`
# is OpenAI's, a prose answer to a forced tool call is Anthropic's, a prompt blocked before generation is
# Gemini's `promptFeedback.blockReason`, and every one of the three documents a cut-off at the cap.
#
# Anthropic's `refusal` is the stop reason its safety classifiers return "as a normal HTTP 200
# response, not an error" (https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons),
# in the shape the refusals page publishes -- `content` empty, `stop_details` naming the category, and
# `usage` reported -- and it is NOT billed: "You are not billed for a refusal that arrives before any
# output. `content` is empty, and token counts appear in `usage` but are not charged"
# (https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback). This entry used to be
# answered with a thinking block and billed output, a refusal the provider charges for, which made the
# meter's settle to reported usage look right where the provider bills nothing; it is now answered only
# on a model in `CLASSIFIED`, and the log records it as billed nothing. [M5.5 review cycle 1,
# M55-DBL-08]
#
# And Gemini's `safety` is a candidate that ends at `finishReason` SAFETY with no parts -- the one
# refusal Gemini reports AFTER generation, beside `blocked`'s refusal before it
# (https://ai.google.dev/api/generate-content, `FinishReason`). Both were missing, and a meter tested
# only against the envelopes it already read correctly says nothing about the ones it read as free.
# [M5.5 review cycle 1, M55-METER-01]
ENVELOPES: dict[str, tuple[str, ...]] = {
    "anthropic": ("normal", "max_tokens", "prose", "refusal"),
    "openai": ("normal", "refusal", "max_tokens"),
    "gemini": ("normal", "max_tokens", "blocked", "safety"),
}

# The server errors each provider documents for a request it failed to serve, which the double answers
# INSTEAD of an answer when a scenario names one: Anthropic's "500 - `api_error`" and "529 -
# `overloaded_error`" (https://platform.claude.com/docs/en/api/errors), OpenAI's "503 - Model
# temporarily overloaded" and "500 - The server had an error while processing your request"
# (https://developers.openai.com/api/docs/guides/error-codes), and Gemini's 500, 503 UNAVAILABLE and
# 504 (https://ai.google.dev/gemini-api/docs/api-errors). The log bills each of them nothing, and only
# Google publishes that: "If your request fails with a 400 or 500 error, you won't be charged for the
# tokens used" (https://ai.google.dev/gemini-api/docs/billing). Anthropic's and OpenAI's error pages
# say nothing either way about what a 5xx bills -- the one billing sentence on each is about a billing
# or quota refusal -- so for those two the zero here is decision 436's choice, which the meter's
# settle rule makes too, and not a provider's statement; the same silence is why a 504, 520 or 524
# keeps its ceiling there. A test whose SUM matches this log on a 5xx therefore shows the meter
# follows that choice, and nothing more. This comment said each page stated it, and quoted OpenAI's
# heading wrong. They exist so a test can count what reached the wire: the fetcher used to re-send a
# paid POST on every one of them, and a double that never failed could not show it. `fault_when`
# "retry" fails only a request carrying the retry opening, which is the shape of a provider that
# answered attempt 1 and then had a bad afternoon. [M5.5 review cycle 1, M55-BUDGET-01, M55-BUDGET-05;
# review cycle 2, DBL-C2-04]
#
# Gemini's 429 is the one fault here that is not a server failing: it is a quota refused. The
# rate-limits page says "the API returns a `429 RESOURCE_EXHAUSTED` error" and "Requests per day (RPD)
# quotas reset at midnight Pacific time" (https://ai.google.dev/gemini-api/docs/rate-limits), and the
# body is generateContent's google.rpc.Status -- an integer `code` and a `status` string, not the
# Interactions API's string `code` -- with the `QuotaFailure` detail Google's error model defines for
# exactly this: "if a daily limit was exceeded for the calling project, a service could respond with a
# QuotaFailure detail" (https://github.com/googleapis/googleapis/blob/master/google/rpc/
# error_details.proto). The `quotaId` below is the per-day id Gemini has been reported to send
# (UKGovernmentBEIS/inspect_ai#5526); no Google page publishes the id list, which is why the adapter
# reads only its "PerDay". The fetcher re-sends a 429 under its own pacing, so every send is answered
# with it. [M5.5 review cycle 2, DBL-C2-03]
FAULTS: dict[str, tuple[int, ...]] = {
    "anthropic": (500, 529),
    "openai": (500, 503),
    "gemini": (429, 500, 503, 504),
}
GEMINI_DAILY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"

# The account-level price settings each provider applies to a request that does not name its own, and
# reports on the envelope. OpenAI: a request with no `service_tier` is "auto", which "will be processed
# with the service tier configured in the Project settings", and the response carries "the
# `service_tier` value based on the processing mode actually used to serve the request" (the Chat
# Completions reference, https://developers.openai.com/api/reference/resources/chat) -- Fast mode
# reported as "priority" "whether the request specifies `priority` or `fast`"
# (https://developers.openai.com/api/docs/guides/fast-mode). Anthropic: `default_inference_geo` "Sets
# the fallback geo when `inference_geo` is omitted from a request", and "The response `usage` object
# includes an `inference_geo` field indicating where inference ran"
# (https://platform.claude.com/docs/en/manage-claude/data-residency). The double left both fields out,
# so a meter that priced every call at the standard row agreed with it where each provider bills more.
# [M5.5 review cycle 2, M55-C2-METER-01, DBL-C2-01]
PROJECT_TIERS = ("default", "flex", "priority")
GEOS = ("global", "us")
_SERVED_TIER = {"default": "default", "flex": "flex", "priority": "priority", "fast": "priority"}

# What Anthropic bills as input beyond the prompt text, so a ceiling that counted the text alone could
# be seen falling short. "Claude 4.7 and later models ... use a newer tokenizer ... This tokenizer
# produces approximately 30% more tokens for the same text", against the same page's "1 token is
# approximately 4 characters"; tool use is "priced based on ... The total number of input tokens sent to
# the model (including in the `tools` parameter)", and "the API also automatically includes a special
# system prompt for the model that enables tool use" -- `(auto/none, any/tool)` below, from that page's
# table (https://platform.claude.com/docs/en/about-claude/pricing). Every Anthropic model served here is
# 4.7 or later. The double counted `(len(system) + len(user)) // 4`, below any estimate the app makes,
# so no assertion that a ceiling is at least the bill could fail against it.
# [M5.5 review cycle 2, M55-CAP-C2-01, M55-C2-METER-03]
ANTHROPIC_TOKENIZER = 1.3
ANTHROPIC_TOOL_PROMPT: dict[str, tuple[int, int]] = {
    "claude-sonnet-5": (354, 474),
    "claude-opus-5": (286, 406),
}
FAULT_WHEN = ("always", "retry")

# The OpenAI models served here that bill a prompt-cache write: "For GPT-5.6 and later, cache writes
# cost 1.25x the standard, uncached input-token rate" and "Prompt caching is enabled by default"
# (https://developers.openai.com/api/docs/guides/prompt-caching), with the implicit breakpoint at the
# end of the latest user message unless `prompt_cache_options.mode` is "explicit" (the Chat
# Completions reference, https://developers.openai.com/api/reference/resources/chat) and a minimum of
# "1,024 tokens for GPT-5.6 and later". gpt-5-mini is older and carries "No additional cache-write
# charge", so the double keeps no cache for it. [M5.5 review cycle 1, M55-DBL-02]
CACHE_WRITES = frozenset({"gpt-5.6-terra"})
CACHE_MIN_TOKENS = 1024
# The OpenAI models served here whose model page lists `none` among its reasoning efforts, the one
# setting under which a temperature is taken (see `_openai_answer`). [M5.5 review cycle 2, DBL-C2-07]
REASONING_NONE = frozenset({"gpt-5.6-terra"})

# How many real tags an answer carries beside its violation, and how long a quote is: rule 2's "10-15
# words is ideal", at its floor.
REAL_TAGS = 3
QUOTE_WORDS = 10


@dataclass
class Scenario:
    """How one provider answers. The usage defaults are the corpus's measurement of gemini-3.6-flash on
    this prompt -- "~1.6k output plus ~2.3k thoughts" (`mdc/config.py:181-185`) -- which is exit check 5,
    and `prompt_tokens` None means an estimate from the request's own length."""

    content: str = "fabricate"
    posture: str = "comply"
    envelope: str = "normal"
    thoughts: bool = True
    prompt_tokens: int | None = None
    output_tokens: int = 1600
    thought_tokens: int = 2300
    fault: int | None = None
    fault_when: str = "always"
    # The OpenAI project's service tier and the Anthropic workspace's default geo: what serves a request
    # that names neither (see `PROJECT_TIERS`).
    project_tier: str = "default"
    geo: str = "global"


class State:
    """Everything mutable, in one object so `/_test/reset` is a single call."""

    def __init__(self) -> None:
        self.scenarios: dict[str, Scenario] = {p: Scenario() for p in PROVIDERS}
        self.requests: list[dict[str, Any]] = []
        # The prompts OpenAI's implicit breakpoint has written, keyed on the whole request text: a
        # lookup only matches at a message's end, so only an identical prompt reads what an earlier
        # one wrote (the prompt-caching guide's "A shared prefix is not always a cached prefix").
        self.cached: set[str] = set()

    def reset(self) -> None:
        self.__init__()


state = State()
router = APIRouter()


# --- what was sent, recorded without the secret -----------------------------------------------------


def _scrub(text: str) -> tuple[str, bool]:
    """The text with every one of the double's keys taken out, and whether one was in it.

    §9's header-only rule is a claim about the URL the app SENT, so the log keeps the full url with its
    query -- and a key the app put there would otherwise land in this log, which a failing test prints.
    The value is replaced and the fact kept, so a test can assert on the fact without the log ever
    holding the secret. Read decoded, because `%2D` is still a hyphen to a server; a url that carried a
    key is therefore logged decoded.
    """
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

# A term line under a `## facet` heading: the id first, then (optionally) the gloss after whitespace.
_TERM_LINE = re.compile(r"^([A-Za-z0-9_]+\.[A-Za-z0-9_\-]+)(?=\s|$)")
_FACET_LINE = re.compile(r"^## facet ([A-Za-z0-9_\-]+)")
# A pack section marker, `[imdb:3]` -- and not the `[type]` or `[note]` header lines, which carry no
# index and quote nothing.
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
    """A message's text, whether its provider sent a string or a list of text blocks or parts."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block["text"] for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return []


def _read(system: str, user: str) -> _Request:
    """The vocabulary a model was shown and the pack it was asked about, and the retry if there is one.

    The retry text is cut off before the pack is read, because the corpus's loop appends it to the
    same user message (`mdc/sources/llm.py:86-88`) and a quote cut out of the violation list would be a
    quote of the app's own words rather than of any source."""
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
    """`count` words of `text` from word `skip`, cut out of the text itself so the quote is verbatim --
    punctuation, markup and spacing exactly as the source published them (rule 2: "character for
    character")."""
    words = list(_WORD.finditer(text))
    if not words:
        return ""
    start = min(skip, max(len(words) - count, 0))
    chosen = words[start:start + count]
    return text[chosen[0].start():chosen[-1].end()]


def _invented_term(req: _Request) -> str:
    """A term id no line of the vocabulary carries, under a facet heading it does carry.

    No TAIL of any vocabulary term either, so M5.4's prefix repair -- which rewrites a head it does not
    know onto the one term with that body -- has nothing to rescue it with: the verdict on it is
    `unknown_term`, the rule the two-attempt row names first."""
    facets = [facet for facet, _term in req.vocabulary]
    head = facets[0] if facets else "themes"
    tails = {term.partition(".")[2] for _facet, term in req.vocabulary}
    candidates = ["mecha", "kaiju_attack", "heist_crew"] + [f"invented_{n}" for n in range(1, 99)]
    return f"{head}.{next(tail for tail in candidates if tail not in tails)}"


def _tags(req: _Request, content: str) -> list[dict[str, Any]]:
    """The answer to one request: real tags that verify, and at most one that breaks one rule.

    Real tags take the first term of each of the first facets, a quote from a different pack section
    each, and a salience in {1,2,3}. The violation is always an EXTRA tag, so every content scenario
    breaks exactly the one rule it names and a retry that drops it leaves a clean answer: `fabricate`
    names an invented term; `unquotable` embellishes a quote past anything the pack says; `salience`
    states a level of 4. The last two ride on a term no real tag uses, so no duplicate is added. With
    no vocabulary in the prompt there is no real term to ride on, and every term is invented.
    """
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
        # An embellished quote, which is what a model half-remembering a review writes.
        invented = f"{quote} and then the double wrote the rest"
        while invented in req.pack:
            invented += " again"
        tags.append({"term": spare, "salience": 2, "source": source, "quote": invented})
    else:
        tags.append({"term": spare, "salience": 4, "source": source, "quote": quote})
    return tags


def _named(term: str, retry: str) -> bool:
    """Whether the retry names this term as a whole id -- `mood.tense` and not inside `mood.tense_x`."""
    return re.search(rf"(?<![A-Za-z0-9_.]){re.escape(term)}(?![A-Za-z0-9_.\-])", retry) is not None


def _answer(req: _Request, sc: Scenario) -> list[dict[str, Any]]:
    """The tags this provider sends: the scenario's, less what a complying retry was told to drop."""
    tags = _tags(req, sc.content)
    if req.retry is not None and sc.posture == "comply":
        tags = [tag for tag in tags if not _named(tag["term"], req.retry)]
    return tags


def _note(req: _Request, model: str, envelope: str, tags: list[dict[str, Any]],
          usage: dict[str, Any] | None = None, service_tier: str | None = None) -> Note:
    """What the log keeps about an answer: the model asked for, the retry text the app sent -- the part
    exit check 2 reads, the violated rule and the offending value as the provider RECEIVED them -- the
    envelope, the terms answered (none when the envelope carried no answer), and the usage block the
    envelope reported, which is what the provider bills: a test reads the household's bill off this log
    and holds the meter to it, rather than off what the meter wrote (decision 436). None where the
    provider charges nothing for what it reported, which is Anthropic's refusal (see `ENVELOPES`).
    `service_tier` is the OpenAI tier the answer was served and billed at, which the envelope reports
    beside its usage rather than inside it (see `PROJECT_TIERS`)."""
    return {"model": model, "retry": req.retry, "envelope": envelope,
            "terms": [tag["term"] for tag in tags] if envelope in ("normal", "unforced") else [],
            "usage": usage, "service_tier": service_tier}


def _faulted(req: _Request, sc: Scenario, provider: str) -> JSONResponse | None:
    """The server error a scenario names, in its provider's documented shape, or None. See `FAULTS`."""
    if sc.fault is None or (sc.fault_when == "retry" and req.retry is None):
        return None
    if provider == "anthropic":
        if sc.fault == 529:
            return _anthropic_error(529, "overloaded_error", "Overloaded")
        return _anthropic_error(sc.fault, "api_error", "Internal server error")
    if provider == "openai":
        # The page publishes the 503's type and code and neither for the 500 (see `FAULTS`).
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
    """Anthropic's `input_tokens` for this request: the prompt and the `tools` block at the newer
    tokenizer's rate, plus the tool-use system prompt for the model and the tool choice (see
    `ANTHROPIC_TOKENIZER`). A scenario's `prompt_tokens` still wins."""
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
    """https://docs.anthropic.com/en/api/errors: `type` "error", an `error` object naming its type and
    message, and the request id."""
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
    # `mdc/llm/client.py:166-170`, measured against the real server: "It is deprecated on the current
    # Sonnet/Opus models and sending it is a hard 400 that no retry can fix." The Messages reference
    # (https://platform.claude.com/docs/en/api/messages/create) draws the line exactly, for every model
    # served here, each released after Claude Opus 4.6: of `temperature`, "A value of 1.0 will be
    # accepted for backwards compatibility, all other values will be rejected with a 400 error"; of
    # `top_p`, "A value >= 0.99 will be accepted"; of `top_k`, "any value will be rejected". So the
    # default is answered and only the rest refused -- this used to refuse `temperature` whatever it
    # said, 1.0 included, which is the stricter-than-server shape the module docstring rules out, and
    # to answer any `top_p` or `top_k`. The wording is the double's; the page publishes none.
    # [M5.5 review cycle 1, M55-DBL-09]
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
    # Manual extended thinking is gone from every model served here, whatever `tool_choice` says: "Claude
    # 4.7 and later models have removed extended thinking. Sending `thinking: {"type": "enabled"}` to any
    # of these models returns a 400", in the words below (the errors page, "Extended thinking not
    # supported"). This used to refuse `enabled` only beside a forced tool, in a sentence of the
    # double's own that the extended-thinking page gives the models before 4.7, and to answer it beside
    # `auto`. [M5.5 review cycle 1, M55-DBL-09]
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return _anthropic_error(
            400, "invalid_request_error",
            '"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" and'
            ' "output_config.effort" to control thinking behavior.'), {}
    # The mechanism the Anthropic adapter IS, refused by three current models on every request (the
    # errors page, "Forced tool use not supported"), and answered by `auto` and `none`. See `MODELS`.
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
    # THE MODEL THINKS BY DEFAULT, and the thinking is billed inside `output_tokens`. The thinking page
    # (https://platform.claude.com/docs/en/build-with-claude/thinking): "On Claude Opus 5.5, Claude Opus
    # 5, Claude Sonnet 5 ... thinking is already on and needs no configuration", `display` defaults to
    # "omitted" so the block arrives "with an empty `thinking` field" and its `signature`, and "Adaptive
    # thinking, including on models where thinking is on by default, supports forced tool use". The
    # steering page (https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost):
    # "`output_tokens` remains the inclusive, authoritative total used for billing", itemised under
    # `output_tokens_details.thinking_tokens`. This double used to ignore `thoughts` for Anthropic, so
    # no test could see an adapter that summed the two, or one that read `content[0]` and found the
    # thinking block there. [M5.5 review cycle 1, M55-METER-06, M55-DBL-03]
    thought = sc.thought_tokens if sc.thoughts else 0
    thinking = ([{"type": "thinking", "thinking": "", "signature": f"Eo{uuid.uuid4().hex}"}]
                if thought else [])
    stop, out = "tool_use", sc.output_tokens + thought
    if envelope == "normal":
        content: list[dict[str, Any]] = thinking + [
            {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:24]}", "name": forced,
             "input": {"tags": tags}}]
    elif envelope == "max_tokens":
        # The cap reached mid-argument and billed in full, thinking included. Under forced tool use the
        # truncated answer is the tool call itself, cut short: the stop-reasons page says "the truncated
        # response contains an incomplete tool use block" and checks `stop_reason == "max_tokens"`
        # beside `content[-1].type == "tool_use"`, and the define-tools page says the model "will not
        # emit a natural language response or explanation before tool_use" when a tool is forced. So
        # the cut-off is a `tool_use` block holding the first tag only -- well-formed, and not the whole
        # answer. This double used to send a text block here, a shape the provider never sends under a
        # forced tool, and the adapter's missing `stop_reason` check passed against it.
        # [M5.5 review cycle 1, M55-DBL-01]
        stop, out = "max_tokens", int(body["max_tokens"])
        content = thinking + [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:24]}",
                               "name": forced, "input": {"tags": tags[:1]}}]
    elif envelope == "prose":
        stop = "end_turn"
        content = thinking + [{"type": "text", "text": "I would rather describe this film in prose "
                                                       "than call the tool: it is a tense, patient "
                                                       "piece of work."}]
    elif envelope == "refusal":
        # The safety classifiers' stop before any output, as the refusals page prints it: no content,
        # `output_tokens` 0, the prompt counted and not charged. See `ENVELOPES`.
        stop, out = "refusal", 0
        content = []
    else:
        # No tool was forced, so the model answered in text -- with the JSON the prompt asked for, which
        # is still not a `tool_use` block.
        stop = "end_turn"
        content = thinking + [{"type": "text", "text": json.dumps({"tags": tags})}]
    # Where inference ran: the request's own `inference_geo`, else the workspace default (see
    # `PROJECT_TIERS`), which is what US-only inference is billed on.
    geo = body.get("inference_geo") if body.get("inference_geo") in GEOS else sc.geo
    usage: dict[str, Any] = {
        "input_tokens": _anthropic_prompt_tokens(req, sc, body["model"], tools, forced is not None),
        "output_tokens": out, "inference_geo": geo}
    if thought and out:
        usage["output_tokens_details"] = {"thinking_tokens": min(thought, out)}
    # `stop_details` is "Structured information about a refusal" -- `type`, `category`, `explanation` --
    # and "`null` for all stop reasons other than `refusal`" (the Messages reference and the
    # stop-reasons page), so every answer carries it.
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
    """https://platform.openai.com/docs/guides/error-codes: one `error` object with message, type, param
    and code."""
    return JSONResponse({"error": {"message": message, "type": kind, "param": param, "code": code}},
                        status_code=status)


def _openai_auth(request: Request) -> JSONResponse | None:
    auth = request.headers.get("authorization", "")
    if not auth:
        return _openai_error(401, "You didn't provide an API key. You need to provide your API key in "
                                  "an Authorization header using Bearer auth (i.e. Authorization: "
                                  "Bearer YOUR_KEY).")
    # The real message quotes a masked form of the refused key; this one quotes none of it, because a
    # fragment of a key in a test's output is a fragment in a CI log.
    if auth != f"Bearer {KEYS['openai']}":
        return _openai_error(401, "Incorrect API key provided. You can find your API key at "
                                  "https://platform.openai.com/account/api-keys.",
                             code="invalid_api_key")
    return None


# Structured Outputs' supported-schemas section as it stands: the keywords strict mode still refuses.
# See the module docstring for why `minimum`, `maximum`, `minItems`, `maxItems`, `pattern` and `format`
# are not among them.
#
# The composition keywords are the guide's own list: "Some type-specific keywords are not yet
# supported -- Composition: `allOf`, `not`, `dependentRequired`, `dependentSchemas`, `if`, `then`,
# `else`", and "If you turn on Structured Outputs by supplying `strict: true` and call the API with an
# unsupported JSON Schema, you will receive an error"
# (https://developers.openai.com/api/docs/guides/structured-outputs). The double answered all seven and
# walked `allOf` as a valid branch, so an adapter change sending one would have passed here and met a
# 400 from OpenAI -- M5.2's failure. `anyOf` is the one composition the page supports.
#
# `minLength`, `maxLength` and `patternProperties` rest on thinner ground, and it is stated rather than
# hidden. The page's "Supported properties" name only `pattern` and `format` for a string and no
# object keyword beyond the structural ones, while its "not yet supported" list names these three only
# "For fine-tuned models" -- the page disagrees with itself. The real server's words settle
# `minLength`: "'minLength' is not permitted" (coder/mux#1220, 2025-12-18). `maxLength` is stripped
# beside it in that same fix with no refusal quoted, and `patternProperties` has no report either way;
# both are refused on the supported-list reading, because the adapter strips all three anyway
# (`openai._STRICT_UNSUPPORTED`), so a refusal here costs the app nothing, where a double that answered
# them and was wrong would hide a 400 on every request. [M5.5 review cycle 2, DBL-C2-07]
OPENAI_NOT_PERMITTED = frozenset({
    "minLength", "maxLength",
    "patternProperties", "unevaluatedProperties", "propertyNames", "minProperties", "maxProperties",
    "unevaluatedItems", "contains", "minContains", "maxContains", "uniqueItems",
    "allOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else",
})


def _strict_refusal(schema: Any, name: str) -> str | None:
    """The first rule a strict schema breaks, in the words the real server uses, or None."""
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
    # Every model served here reasons -- its usage block says so -- and the reasoning models refuse the
    # deprecated `max_tokens` outright (`mdc/llm/client.py:221-222`) and any temperature but the default
    # while they reason: "When reasoning effort is not `none`, remove `temperature`"
    # (https://developers.openai.com/api/docs/guides/latest-model). A model whose effort "supports: none,
    # low, medium (default)" (https://developers.openai.com/api/docs/models/gpt-5.6-terra) answers one
    # sent with `reasoning_effort` "none", where the double refused it on every model; gpt-5-mini lists
    # no `none` and still refuses. [M5.5 review cycle 2, DBL-C2-07]
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
    # `completion_tokens` INCLUDES the reasoning and `reasoning_tokens` itemises it (the reasoning
    # guide), so the bill is the visible answer plus the thinking, and exit check 6 reads it as it comes.
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
    # The tier the request named, else the project's (see `PROJECT_TIERS`), as the response reports it.
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
    """`usage.prompt_tokens_details` as the Chat Completions reference documents it: `cached_tokens`,
    "Cached tokens present in the prompt", and `cache_write_tokens`, "The unadjusted number of prompt
    tokens written to cache" -- both inside `prompt_tokens`, not beside it. On a model in
    `CACHE_WRITES`, with implicit mode left on and a prompt past the minimum, the first sight of a
    prompt writes all of it and an identical later one reads all of it; anything else neither."""
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
    """https://ai.google.dev/gemini-api/docs/troubleshooting: Google's `error` object -- the HTTP code,
    the message and the canonical status name, with the `ErrorInfo` detail where the server sends
    one, or the details a fault names (see `FAULTS`)."""
    error: dict[str, Any] = {"code": status, "message": message, "status": grpc}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                             "domain": "googleapis.com",
                             "metadata": {"service": "generativelanguage.googleapis.com"}}]
    if details:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status)


def _gemini_auth(request: Request) -> JSONResponse | None:
    """The key from `x-goog-api-key` or from `?key=`, both of which the real API accepts.

    Refusing the query form would be stricter than Google, and the app's rule against it would then be
    held by a refusal that does not exist in production. The log records which way it came."""
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


# The fields of the Schema object `responseSchema` is parsed into. The request is parsed as a protocol
# buffer and a name the message has no field for is refused -- which is WHY `additionalProperties` is a
# 400 (`mdc/llm/client.py:14-16`: responseSchema "rejects `additionalProperties`"), and why `uniqueItems`
# or `$ref` would be too. Protocol-buffer JSON takes each field under its proto name as well as its
# camelCase one, so both spellings are answered.
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
    # `thoughtsTokenCount` is reported apart from `candidatesTokenCount` and billed as output (the
    # thinking page), and a response from a model that did not think carries no such key at all.
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
    """Anthropic's and OpenAI's lists share a path and nothing else, so the Host header decides."""
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
    """https://docs.anthropic.com/en/api/models-list: paginated, `limit` from 1 to 1000."""
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
    """https://ai.google.dev/api/models#method:-models.list: `pageSize` up to 1000, a larger value
    coerced to 1000, and `nextPageToken` only while the list continues."""
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
    """A provider path asked of a host that does not serve it: a 404 on the real internet too."""
    _record(request, _provider_of(request), 404, key_header=False)
    return JSONResponse({"error": {"message": f"no such route on {request.url.hostname}"}},
                        status_code=404)


# --- control surface (no provider counterpart) --------------------------------------------------------


class ScenarioControl(BaseModel):
    """A change to how one provider answers -- or all three, when `provider` is omitted. A field left out
    keeps its value; `prompt_tokens` sent as null goes back to the estimate."""

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
    # An envelope a provider does not document is refused rather than approximated: a Gemini "refusal"
    # would be a shape this double invented, and a test passing against it would prove nothing.
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
