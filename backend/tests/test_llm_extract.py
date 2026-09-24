"""Stage 6's extraction for one title, against the refusing double. Spec v2.1 §9, §8 stage 6.

§9: "The schema is a cost-saving device, not the guarantee - the guarantee is the validator ...
Two-attempt pattern: retry once with the specific contract violation named." The coverage row reads
that as three clauses and a fourth about the adapters, and this file asserts each against a server
rather than against the code that talks to it:

  * A SCHEMA-VALID, CONTRACT-VIOLATING ANSWER IS REJECTED AND RETRIED EXACTLY ONCE, with the rule
    and the offending value in the retry as the provider RECEIVED it -- read off the double's own
    request log, not off the string this app built (exit measures 1 and 2).
  * A SECOND VIOLATION FAILS FOR GOOD AND WRITES NOTHING, and "nothing" is a count of `dna_tag` for
    the title taken before and after, over a tier that already holds a row (exit measure 3).
  * IDENTICAL ON ALL THREE ADAPTERS: every case is parametrised over the three providers, and one
    test runs the three in turn and compares what each came to (exit measure 4).
  * THE METER CHARGES WHAT IS BILLED: Gemini's 1,600 candidate plus 2,300 thought tokens is a row
    at 3,900, and OpenAI's `completion_tokens` is taken as it comes (exit measures 5 and 6).
  * NO KEY IN ANY URL, LOG LINE OR ROW -- held by inspecting what the fetcher, the raw store, the
    double and httpx's logger RECORDED, and by sweeping every table and every stored byte for the
    plaintext key after a run (exit measures 10-12).

THE DOUBLE IS `ops/fake_llm.py`, mounted as the transport of a REAL `acquire.fetch.Fetcher` on the
test's own connection, so every call passes the fetcher's host policy, pacing and breaker and the
adapters keep their production urls. Its default first answer is wrong on purpose: schema-valid,
with one term the vocabulary does not carry beside three that verify (plan §9's first risk). Its
content is cut from the request -- terms from the prompt's vocabulary block, quotes from the pack
the prompt carries -- so a tag that verifies here verified against a pack `dna/packs.py` rendered
and the raw store holds.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import asyncio
import gzip
import importlib.util
import json
import logging
import re
import sys
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import fetch
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.dna import packs, verify
from spielplan.llm import client, contract, extract, pricing, spend

REPO = Path(__file__).resolve().parents[2]
DOUBLE = REPO / "ops" / "fake_llm.py"
EXTRACT_SOURCE = REPO / "backend" / "spielplan" / "llm" / "extract.py"

PROVIDERS = ("anthropic", "openai", "gemini")
MODELS = {"anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra", "gemini": "gemini-3.7-flash"}
# The documented endpoint of each paid call, which is what `raw_document.url` must equal.
URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent",
}

TITLE = 7
TASK_KEY = "title:7"
RUN_ID = 4242

# The double's first answer: three real tags -- the first term of each of the first three facets in
# `dna_facet.ord` order -- and one it invented under the first facet's head.
REAL_TERMS = ["mood.bleak", "pacing.slow_burn", "themes.revenge"]
INVENTED = "mood.mecha"
# The first vocabulary term no real tag uses, which the `unquotable` and `salience` scenarios ride on.
SPARE = "mood.tense"
# The row the seed import left, which a failed extraction must leave exactly where it was.
BUNDLE_ROW = ("themes.robots", "")

PLOT = ("A fisherman returns to the harbour town that exiled him and slowly takes his revenge on the "
        "men who drowned his brother.")
REVIEWS = [
    ("imdb", "It is a **bleak** and unforgiving portrait of a town that has decided what it will not "
             "remember, shot in grey light by a director who refuses every consolation."),
    ("tmdb", "The tension builds patiently across two hours and never once releases, a slow burn "
             "that rewards anyone willing to stay with its long and silent scenes."),
]

# Each content scenario, the rule `verify_payload` refuses it under, and the term its refusal names.
VIOLATIONS = {
    "fabricate": ("unknown_term", INVENTED),
    "unquotable": ("quote_unverified", SPARE),
    "salience": ("schema", SPARE),
}


# --- the install ---------------------------------------------------------------------------------


@pytest.fixture
def double():
    """`ops/fake_llm.py`, loaded the way `test_fake_llm.py` loads it -- registered in `sys.modules`
    before it runs, for Pydantic's sake -- so each test holds a fresh module and a fresh log."""
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR`, and so the raw store the pack and every paid answer land in, under this test's
    own tmp_path -- `test_llm_spend.py`'s idiom and its reason."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


async def _vocabulary(db) -> None:
    await db.execute("INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 4, 6)")
    await db.execute("INSERT INTO dna_facet (version, facet, ord) VALUES "
                     "('v1', 'mood', 0), ('v1', 'themes', 1), ('v1', 'pacing', 2), ('v1', 'place', 3)")
    await db.execute(
        "INSERT INTO dna_term (version, term, facet, gloss) VALUES "
        "('v1', 'mood.bleak', 'mood', 'hopeless, grey, unconsoled'), "
        "('v1', 'mood.tense', 'mood', NULL), "
        "('v1', 'pacing.slow_burn', 'pacing', 'patient build that pays off late'), "
        "('v1', 'place.harbour_town', 'place', NULL), "
        "('v1', 'themes.revenge', 'themes', 'a wrong answered in kind'), "
        "('v1', 'themes.robots', 'themes', NULL)"
    )


async def _title(db) -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned)"
        " VALUES ($1, 'movie', 'Grey Harbour', 2021, true)", TITLE,
    )


async def _pack(db) -> int:
    """The pack stage 6 reads, rendered by `dna/packs.py` and kept in the raw store; its raw
    document is what every metered call cites (decision 430)."""
    text, info = packs.render_pack(TITLE, "Grey Harbour", 2021, "film", None, None, PLOT, REVIEWS)
    return await packs.store_pack(db, TITLE, "v1", text, info, entity_key=TASK_KEY)


async def _bundle_row(db) -> None:
    """One extracted-tier row the seed import wrote, with its quote: the tier a failure leaves."""
    tag = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) "
        "VALUES ($1, 'v1', 'themes.robots', 'themes', 2) RETURNING id", TITLE,
    )
    await db.execute("INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'imdb:1')",
                     tag, "a director who refuses every consolation")


async def _keys(db, double, **overrides) -> None:
    """Every provider's card holding the double's key, or the key a test names instead."""
    for provider in PROVIDERS:
        await registry.save_connector(db, provider, api_key=overrides.get(provider, double.KEYS[provider]))


async def _assign(db, provider="gemini", **settings_) -> None:
    fields = {"extraction_provider": provider, "parallel": False, "passes": 1}
    await registry.save_connector(db, "llm", **{**fields, **settings_})


@pytest.fixture
async def packed(db, data_dir, secrets_key, double) -> int:
    """The active vocabulary, the title with its stored pack and one bundle row, and all three
    providers keyed. Returns the pack's raw document id."""
    await _vocabulary(db)
    await _title(db)
    doc = await _pack(db)
    await _bundle_row(db)
    await _keys(db, double)
    return doc


class _Clock:
    """The fetcher's clock and sleeper together, so no pacing is waited out in real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


async def _extract(db, double, transport=None) -> extract.Extraction:
    """Stage 6's call, through a real Fetcher on the test's connection with the double as its
    transport -- the one fetcher a drain builds, and the only door to a provider. `transport` wraps
    the double for a test that needs the network to misbehave around a real answer."""
    clock = _Clock()
    async with fetch.Fetcher(conn=db, transport=transport or httpx.ASGITransport(app=double.app),
                             clock=clock, sleep=clock.sleep, jitter=lambda low, high: 0.0) as fetcher:
        return await extract.extract_title(db, title_id=TITLE, fetcher=fetcher, task_key=TASK_KEY,
                                           run_id=RUN_ID)


async def _scenario(double, **fields) -> None:
    """How the double answers, set through its own control route, which refuses an envelope a
    provider does not document rather than approximating one."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=double.app)) as http:
        resp = await http.post("http://fake-llm/_test/scenario", json=fields)
    assert resp.status_code == 200, resp.text


def _sent(double, provider: str) -> list[dict]:
    return [r for r in double.state.requests if r["provider"] == provider]


async def _tier(db) -> list[tuple]:
    rows = await db.fetch(
        "SELECT term, provider, salience, confidence, n_sources FROM dna_tag WHERE title_id = $1"
        " ORDER BY term, provider", TITLE,
    )
    return [tuple(row) for row in rows]


async def _count(db) -> int:
    return await db.fetchval("SELECT count(*) FROM dna_tag WHERE title_id = $1", TITLE)


async def _metered(db) -> list:
    return await db.fetch("SELECT * FROM llm_call ORDER BY id")


async def _rejects(db) -> list[tuple]:
    rows = await db.fetch(
        "SELECT title_id, run_id, term, rule_violated, provider FROM dna_reject ORDER BY id")
    return [tuple(row) for row in rows]


# What each provider's published page charges per 1M tokens for the default model: input, cache
# write, cache read, output. Anthropic https://platform.claude.com/docs/en/about-claude/pricing
# ("Claude Sonnet 5 | $2 / MTok ... $10 / MTok"); OpenAI https://developers.openai.com/api/docs/pricing
# ("gpt-5.6-terra ... $2.00 | $0.20 | $2.50 | $12.00", input, cached input, cache writes, output);
# Gemini https://ai.google.dev/gemini-api/docs/pricing ("$0.75 through December 31, 2026. $1.50
# starting January 1, 2027" in and "$3.75 ... $7.50" out, "Output price (including thinking tokens)").
def _published(provider: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if provider == "gemini":
        early = date.today() < date(2027, 1, 1)
        rate_in, rate_out = (Decimal("0.75"), Decimal("3.75")) if early else (Decimal("1.50"),
                                                                              Decimal("7.50"))
        return rate_in, rate_in, rate_in, rate_out
    if provider == "openai":
        return Decimal("2.00"), Decimal("2.50"), Decimal("0.20"), Decimal("12.00")
    return Decimal("2"), Decimal("2"), Decimal("2"), Decimal("10")


def _billed(provider: str, usage: dict | None, tier: str | None = None) -> tuple[int, int, Decimal]:
    """One answer's bill as its provider reports it, read off the usage block the double SENT: the
    input the envelope counts, the output it bills and the dollars -- Gemini's candidates plus its
    thoughts, OpenAI's `completion_tokens` as they come with its prompt split into written, read and
    plain, and Anthropic's `output_tokens`, which already hold the thinking. Rounded per call to the
    six places a bill is kept in. An error with no usage block billed nothing.

    AT THE MULTIPLIER THE ENVELOPE SAYS IT WAS BILLED AT: Anthropic's US-only inference "is priced at
    1.1x the standard rate across all token pricing categories"
    (https://platform.claude.com/docs/en/manage-claude/data-residency), reported as
    `usage.inference_geo`; OpenAI's Fast mode, reported as `service_tier` "priority", at the page's
    "gpt-5.6-terra | $4.00 | $0.40 | $5.00 | $24.00", twice its Standard row
    (https://developers.openai.com/api/docs/pricing). [M5.5 review cycle 2, M55-C2-METER-01]"""
    if not usage:
        return 0, 0, Decimal("0")
    rate_in, rate_write, rate_read, rate_out = _published(provider)
    multiplier = Decimal("1")
    if provider == "anthropic" and usage.get("inference_geo") == "us":
        multiplier = Decimal("1.1")
    if provider == "openai" and tier == "priority":
        multiplier = Decimal("2")
    written = read = 0
    if provider == "gemini":
        tokens_in = usage.get("promptTokenCount", 0)
        tokens_out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    elif provider == "openai":
        tokens_in, tokens_out = usage["prompt_tokens"], usage["completion_tokens"]
        details = usage.get("prompt_tokens_details") or {}
        written, read = details.get("cache_write_tokens", 0), details.get("cached_tokens", 0)
    else:
        tokens_in, tokens_out = usage["input_tokens"], usage["output_tokens"]
    cost = ((tokens_in - written - read) * rate_in + written * rate_write + read * rate_read
            + tokens_out * rate_out) / Decimal(1_000_000) * multiplier
    return tokens_in, tokens_out, cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _bill(double, provider: str) -> Decimal:
    """What the double billed the household across every request it answered, on its own log."""
    return sum((_billed(provider, r.get("usage"), r.get("service_tier"))[2]
                for r in _sent(double, provider)), Decimal("0"))


async def _ceiling(db, provider: str, user: str | None = None) -> tuple[int, Decimal]:
    """An attempt's write-ahead ceiling (decision 436 (2)): the prompt stage 6 sends and the schema
    beside it, counted with the adapter's margin (`client.ceiling_input`), plus the request's
    `max_tokens`, at the plan's price and the dearest rate the request can be billed at. It counted
    the prompt the way the reservation does until review cycle 2 (M55-CAP-C2-01); whether the figure
    bounds the bill is held against the double's own bill, not against this helper."""
    voc = await contract.load_prompt_vocabulary(db, "v1")
    pack = await verify.read_pack(db, TITLE, "v1")
    tokens_in = client.ceiling_input(provider, contract.system_prompt(voc),
                                     user if user is not None else contract.user_prompt(pack),
                                     contract.EXTRACTION_SCHEMA)
    price = pricing.price_for(provider, MODELS[provider])
    return tokens_in, pricing.ceiling(tokens_in, client.MAX_OUTPUT_TOKENS, price,
                                      rate=client.ceiling_rate(provider))


# --- exit measures 1 and 2: rejected, retried once, the violation named --------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_fabricated_term_is_rejected_and_retried_once_with_the_rule_and_the_term_named(
    db, packed, double, provider
):
    """The double's default: a schema-valid first answer carrying one invented term, and a retry
    it complies with. Exactly two calls; the first is the plain ask and the second carries the
    corpus's retry opening, the rule and the offending term, as the provider received them. The
    retry's clean answer is merged and written under the provider's name, replacing the tier the
    seed import left; both attempts are metered and both answers are in the raw store under the
    task's key and the documented url; and attempt 1's refusal is recorded (decision 341)."""
    await _assign(db, provider)
    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls, outcome.n_tags) == (extract.WRITTEN, 2, 3), outcome
    sent = _sent(double, provider)
    assert len(sent) == 2, sent
    assert sent[0]["retry"] is None
    retry = sent[1]["retry"]
    assert retry.startswith(contract.RETRY_MARKER), retry
    assert f"unknown_term: '{INVENTED}' is not in vocabulary v1" in retry, retry
    assert sent[0]["terms"] == ["mood.bleak", "themes.revenge", "pacing.slow_burn", INVENTED]
    assert INVENTED not in sent[1]["terms"]

    assert await _tier(db) == [(term, provider, sal, 1.0, 1) for term, sal in
                               (("mood.bleak", 2), ("pacing.slow_burn", 1), ("themes.revenge", 3))]
    refs = await db.fetch(
        "SELECT t.term, e.source_ref FROM dna_evidence e JOIN dna_tag t ON t.id = e.dna_tag_id"
        " WHERE t.title_id = $1 ORDER BY t.term", TITLE)
    assert [tuple(r) for r in refs] == [(term, f"{provider}:1") for term in REAL_TERMS]

    calls = await _metered(db)
    assert [(c["provider"], c["pass_index"], c["attempt"], c["ok"], c["error"]) for c in calls] == [
        (provider, 1, 1, True, None), (provider, 1, 2, True, None)]
    assert {c["pack_document_id"] for c in calls} == {packed}
    assert {c["title_id"] for c in calls} == {TITLE}
    stored = await db.fetch(
        "SELECT id, source, kind, entity_key, url, run_id, request_meta FROM raw_document"
        " WHERE id = ANY($1::bigint[]) ORDER BY id", [c["response_document_id"] for c in calls])
    assert [(d["source"], d["kind"], d["entity_key"], d["url"], d["run_id"]) for d in stored] == [
        (f"llm:{provider}", "dna:extract", TASK_KEY, URLS[provider], RUN_ID)] * 2
    assert [d["request_meta"]["attempt"] for d in stored] == [1, 2]
    assert {d["request_meta"]["model"] for d in stored} == {MODELS[provider]}

    assert await _rejects(db) == [(TITLE, RUN_ID, INVENTED, "unknown_term", provider)]


# --- exit measures 3 and 4: a second violation, identical everywhere -------------------------------


@pytest.mark.parametrize("content", sorted(VIOLATIONS))
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_second_violation_fails_for_good_and_leaves_the_tier_as_it_was(
    db, packed, double, provider, content
):
    """A provider that repeats the violation it was told about: two calls and no third, the stage's
    verdict `violated` (decision 431: final), and `SELECT count(*) FROM dna_tag` for the title the
    same before and after -- over a tier that holds a row, so "unchanged" is not "still empty".
    Both attempts are metered and both refusals recorded; the retry named the rule and the value
    each scenario breaks: an invented term, a quote the pack does not hold, a salience of 4 (which
    decision 386 refuses as `schema` rather than clamping)."""
    await _assign(db, provider)
    await _scenario(double, provider=provider, content=content, posture="stubborn")
    before_count, before = await _count(db), await _tier(db)

    outcome = await _extract(db, double)

    rule, term = VIOLATIONS[content]
    assert (outcome.status, outcome.calls, outcome.n_tags) == (extract.VIOLATED, 2, 0), outcome
    assert outcome.detail["rules"] == {rule: 1}
    assert f"{rule} x1" in outcome.reason
    assert len(_sent(double, provider)) == 2
    assert await _count(db) == before_count == 1
    assert await _tier(db) == before == [(*BUNDLE_ROW, 2, None, None)]

    retry = _sent(double, provider)[1]["retry"]
    assert f"- {rule}: " in retry, retry
    assert f"'{term}'" in retry, retry
    if content == "unquotable":
        refused_quote = await db.fetchval("SELECT quote FROM dna_reject ORDER BY id LIMIT 1")
        assert refused_quote[:60] in retry, retry
    if content == "salience":
        assert "stated level 4" in retry, retry

    assert [(c["attempt"], c["ok"]) for c in await _metered(db)] == [(1, True), (2, True)]
    assert await _rejects(db) == [(TITLE, RUN_ID, term, rule, provider)] * 2


@pytest.mark.parametrize("posture", ["comply", "stubborn"])
@pytest.mark.parametrize("content", sorted(VIOLATIONS))
async def test_the_three_adapters_reach_identical_outcomes_in_every_scenario(
    db, packed, double, content, posture
):
    """Exit measure 4, as one comparison rather than three passing tests: each provider in turn,
    against the same title and the same double, and what each came to -- the verdict, the calls,
    the rows written and their weights, the refusals recorded, and the rules its retry named --
    is the same tuple. Forced tool-use, a strict schema and `responseSchema` are three mechanisms
    and one enforcement."""
    seen = {}
    for provider in PROVIDERS:
        double.state.reset()
        await _assign(db, provider)
        await _scenario(double, provider=provider, content=content, posture=posture)
        outcome = await _extract(db, double)
        written = [row[:1] + row[2:] for row in await _tier(db) if row[1] == provider]
        refused = await db.fetch(
            "SELECT rule_violated, term, count(*) AS n FROM dna_reject WHERE provider = $1"
            " GROUP BY 1, 2 ORDER BY 1, 2", provider)
        named = [re.findall(r"^- (\w+): ", r["retry"] or "", re.MULTILINE)
                 for r in _sent(double, provider)]
        seen[provider] = (outcome.status, outcome.calls, outcome.n_tags, written,
                          [tuple(r) for r in refused], named)

    assert seen["anthropic"] == seen["openai"] == seen["gemini"], seen
    status, calls, _n, _written, _refused, named = seen["gemini"]
    assert calls == 2
    assert named == [[], [VIOLATIONS[content][0]]]
    assert status == (extract.WRITTEN if posture == "comply" else extract.VIOLATED)


# --- exit measures 5 and 6: the meter charges what is billed -----------------------------------


@pytest.mark.parametrize(("thoughts", "billed"), [(True, 1_600 + 2_300), (False, 1_600)])
async def test_a_gemini_call_is_metered_at_its_candidate_plus_its_thought_tokens(
    db, packed, double, thoughts, billed
):
    """§9: "Gemini bills thinking tokens as output - counting visible JSON understates cost ~5x".
    The double reports the corpus's measurement -- 1,600 candidate tokens and 2,300 thought tokens
    -- and the row says 3,900, charged at 3,900 x the output price; a response from a model that
    did not think carries no `thoughtsTokenCount` at all, and the row says 1,600."""
    await _assign(db, "gemini")
    await _scenario(double, provider="gemini", content="clean", thoughts=thoughts)
    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.WRITTEN, 1)
    [call] = await _metered(db)
    assert call["tokens_out_billed"] == billed
    price = pricing.price_for("gemini", MODELS["gemini"])
    assert call["usd"] == pricing.usd(call["tokens_in"], billed, price)
    assert call["tokens_in"] > 0


@pytest.mark.parametrize(("thoughts", "completion"), [(True, 1_600 + 2_300), (False, 1_600)])
async def test_openai_completion_tokens_are_metered_as_they_come(
    db, packed, double, thoughts, completion
):
    """OpenAI folds its reasoning into `completion_tokens` and itemises it under
    `completion_tokens_details.reasoning_tokens` (the reasoning guide), so the bill is the first
    figure as it comes -- adding the second to it would charge the reasoning twice."""
    await _assign(db, "openai")
    await _scenario(double, provider="openai", content="clean", thoughts=thoughts)
    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.WRITTEN, 1)
    [call] = await _metered(db)
    assert call["tokens_out_billed"] == completion
    price = pricing.price_for("openai", MODELS["openai"])
    assert call["usd"] == pricing.usd(call["tokens_in"], completion, price)


# --- decision 431: what a provider's own failure comes to ---------------------------------------


# The failure envelopes a provider reports usage for and charges nothing, each with the model that can
# send it: Anthropic's refusal before any output, from a model with the classifiers. [M55-DBL-08]
UNCHARGED = {("anthropic", "refusal"): "claude-opus-5"}


@pytest.mark.parametrize(("provider", "envelope", "status"), [
    ("anthropic", "prose", extract.REFUSED),
    ("anthropic", "refusal", extract.REFUSED),
    ("anthropic", "max_tokens", extract.TRANSIENT),
    ("openai", "refusal", extract.REFUSED),
    ("openai", "max_tokens", extract.TRANSIENT),
    ("gemini", "blocked", extract.REFUSED),
    ("gemini", "safety", extract.REFUSED),
    ("gemini", "max_tokens", extract.TRANSIENT),
])
async def test_an_envelope_that_refuses_fails_for_good_and_a_cut_off_keeps_the_queues_curve(
    db, packed, double, provider, envelope, status
):
    """Each provider's documented ways for an answer to go wrong. OpenAI's `message.refusal`,
    Anthropic's `refusal` stop, Gemini's `promptFeedback.blockReason` and its SAFETY finish, and a
    forced tool call answered in prose are final (decision 431); a cut-off at the output ceiling --
    for Anthropic, the forced call's own block cut short -- is retryable and stays on the queue's
    curve. Either way: one call and no retry -- there is no verdict to name -- no refusal, and the
    tier untouched.

    AND THE ROW IS THE BILL. Every one of these is a 200 the provider billed: the cut-off the whole
    cap, the refusal what it read and thought, the blocked prompt its input. This test used to assert
    the row at zero tokens and zero dollars with no stored response -- pinning the meter against the
    double's own bill, which a cap that sums `llm_call.usd` then never saw, however many times the
    queue re-ran a cut-off. Decision 436 (3) settles the attempt to the usage the envelope reported
    and keeps the envelope in the raw store, cited by the row; the dollars are the double's bill read
    off its own log at the published price. [M5.5 review cycle 1, M55-METER-01, M55-BUDGET-02,
    M55-SPEND-01, M55-DBL-01]

    EXCEPT WHERE THE PROVIDER SAYS IT CHARGES NOTHING. Anthropic's refusal before any output reports
    its prompt and bills none of it ("token counts appear in `usage` but are not charged", the
    refusals page), so it is settled to zero -- still stored, still cited -- on Claude Opus 5, a model
    that has the classifiers; the double used to bill it on Sonnet 5, which does not.
    [M5.5 review cycle 1, M55-DBL-08]"""
    await _assign(db, provider)
    if (provider, envelope) in UNCHARGED:
        await registry.save_connector(db, provider, model=UNCHARGED[provider, envelope])
    await _scenario(double, provider=provider, envelope=envelope)
    before = await _tier(db)

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls, outcome.n_tags) == (status, 1, 0), outcome
    assert outcome.detail["attempt"] == 1 and outcome.detail["provider"] == provider
    [sent] = _sent(double, provider)
    [call] = await _metered(db)
    tokens_in, tokens_out, billed = _billed(provider, sent["usage"])
    assert (billed > 0) is ((provider, envelope) not in UNCHARGED), sent["usage"]
    assert (call["ok"], call["tokens_in"], call["tokens_out_billed"], call["usd"]) == (
        False, tokens_in, tokens_out, billed)
    assert call["error"] and call["error"] in outcome.reason
    stored = await db.fetchrow("SELECT id, url, http_status, source FROM raw_document"
                               " WHERE source LIKE 'llm:%'")
    assert stored is not None and call["response_document_id"] == stored["id"]
    assert (stored["url"], stored["http_status"], stored["source"]) == (URLS[provider], 200,
                                                                        f"llm:{provider}")
    assert await _rejects(db) == []
    assert await _tier(db) == before


async def test_the_meter_sums_to_the_doubles_own_bill_across_every_scenario(db, packed, double):
    """§9: "a cap computed from the wrong number is not a cap", and the cap is `SUM(llm_call.usd)`
    (decision 325). So the one figure that matters is held to the one bill that exists: every
    scenario the double documents for every provider -- each failure envelope, each server error it
    answers, a violation retried and accepted, a violation repeated -- run one after another on one
    install, and the month's SUM equals what the double's own log says the household was billed at
    the published prices, with one `llm_call` row for every request that reached a provider.

    Every one of these answers arrived. The attempt whose answer was LOST is the one case the SUM is
    allowed to exceed the bill -- it stands at its write-ahead ceiling, which is at least what the
    provider could have charged -- and `test_an_answer_lost_on_the_way_back_is_sent_once_and_metered_
    at_its_ceiling` holds it there. Before decision 436 this SUM read $0 for every refused 200 and
    every cut-off while the double billed each one, and no test on the spend meter's row drove a
    single failure envelope. [M5.5 review cycle 1, M55-DOC-03, M55-DOC-01, M55-METER-01]"""
    # Every fault but Gemini's 429, a quota refused: the fetcher re-sends a 429 in-process (decision
    # 436 (1)), so its requests are not one per attempt, and it bills nothing either way; its own test
    # holds it (`test_a_gemini_daily_quota_waits_as_the_accounts_through_the_double`).
    faults = {p: [f for f in double.FAULTS[p] if f != 429] for p in PROVIDERS}
    ran = 0
    for provider in PROVIDERS:
        await _assign(db, provider)
        scenarios = [{"envelope": envelope} for envelope in double.ENVELOPES[provider]]
        scenarios += [{"fault": fault} for fault in faults[provider]]
        scenarios += [{"posture": "stubborn"}]
        for scenario in scenarios:
            model = UNCHARGED.get((provider, scenario.get("envelope")), MODELS[provider])
            await registry.save_connector(db, provider, model=model)
            await _scenario(double, **{"provider": provider, "content": "fabricate", "posture": "comply",
                                       "envelope": "normal", "fault": None, **scenario})
            await _extract(db, double)
            ran += 1

    requests = [r for r in double.state.requests if r["provider"] in PROVIDERS and r["method"] == "POST"]
    calls = await _metered(db)
    assert ran == sum(len(double.ENVELOPES[p]) + len(faults[p]) + 1 for p in PROVIDERS)
    assert len(calls) == len(requests), (len(calls), len(requests))
    billed = sum((_bill(double, provider) for provider in PROVIDERS), Decimal("0"))
    assert billed > 0
    assert await db.fetchval("SELECT SUM(usd) FROM llm_call") == billed
    for provider in PROVIDERS:
        mine = sum((c["usd"] for c in calls if c["provider"] == provider), Decimal("0"))
        assert mine == _bill(double, provider), provider
    assert not [c for c in calls if (c["error"] or "").startswith(spend.UNSETTLED_PREFIX)]


async def test_a_cut_off_re_run_on_the_queues_curve_is_stopped_by_the_cap(db, packed, double):
    """M55-DOC-01's own reproduction, run against the tree: a provider that stops at its output
    ceiling on every walk, a $0.50 cap, and stage 6's order of events -- `spend.cap_check`, then the
    extraction -- repeated as decision 431's curve repeats a retryable failure.

    The review measured five walks through the gate, the double billing $0.90 at the table's old
    Sonnet price ($0.60 at the published one) while `SUM(llm_call.usd)` read $0: each cut-off was a
    row at zero, so the sum the gate reads never moved, and decision 325's "a call billing more than
    its estimate lands in the meter and the next title sees it" was false for exactly the attempts
    most likely to run long. Settled to its usage (decision 436), each walk moves the sum by what it
    billed, the gate refuses before the fifth, and the month ends no further past the cap than the
    last walk the gate let through -- the bound decision 325's Cost now states. [M5.5 review cycle 1,
    M55-DOC-01]"""
    cap = Decimal("0.5")
    await _assign(db, "anthropic", cap_usd=float(cap))
    await _scenario(double, provider="anthropic", envelope="max_tokens", prompt_tokens=20_000)

    walks, refusal = 0, None
    for _ in range(5):
        refusal = await spend.cap_check(db, title_id=TITLE)
        if refusal is not None:
            break
        outcome = await _extract(db, double)
        assert (outcome.status, outcome.calls) == (extract.TRANSIENT, 1), outcome
        walks += 1

    spent = await db.fetchval("SELECT SUM(usd) FROM llm_call")
    assert refusal is not None and refusal.kind == spend.OVER_CAP, refusal
    assert refusal.reason.startswith(spend.OVER_CAP_PREFIX)
    assert 0 < walks < 5, walks
    assert len(await _metered(db)) == len(_sent(double, "anthropic")) == walks
    assert spent == _bill(double, "anthropic") > 0
    assert spent <= cap + spent / walks, (spent, walks)


class _Wrapped(httpx.AsyncBaseTransport):
    """The double behind a network that misbehaves around its real answer: `after` is awaited with
    the double's response in hand, and may raise, stall or rewrite it. Every request the double
    answered is in its own log, so what the household was billed is still read off that log."""

    def __init__(self, double, after):
        self.inner = httpx.ASGITransport(app=double.app)
        self.after = after

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        await response.aread()
        return await self.after(request, response)


# --- decision 436: every attempt a provider may have billed is a row, from before it is sent ------


async def test_an_attempt_cancelled_mid_call_stays_in_the_meter_at_its_ceiling(db, packed, double):
    """The row used to be written only once the answer was back and stored, so an attempt the
    provider billed and the worker never heard back from left NO row -- and cancellation mid-call is
    the drain's ordinary end, `worker.py` bounding it at 420 s while one call may take `TIMEOUT_S`'s
    300. Decision 436 (2): the row is INSERTed at its ceiling before the POST and committed at once,
    so a cancellation leaves the ceiling in the month -- at least what the provider billed -- and a
    concurrent cap check sees it while the call is still on the wire.
    [M5.5 review cycle 1, M55-METER-03, M55-BUDGET-03, M55-SPEND-02, NBR-01]"""
    await _assign(db, "gemini")
    await _scenario(double, provider="gemini", content="clean")
    held = asyncio.Event()

    async def never(request, response):
        held.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(_extract(db, double, _Wrapped(double, never)))
    await asyncio.wait_for(held.wait(), 10)
    [in_flight] = await _metered(db)
    tokens_in, ceiling = await _ceiling(db, "gemini")
    assert (in_flight["ok"], in_flight["tokens_in"], in_flight["tokens_out_billed"],
            in_flight["usd"]) == (False, tokens_in, client.MAX_OUTPUT_TOKENS, ceiling)
    assert in_flight["error"].startswith(spend.UNSETTLED_PREFIX), in_flight["error"]
    assert await spend.spent(db) == ceiling, "a concurrent cap check reads the call on the wire"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [call] = await _metered(db)
    assert call["usd"] == ceiling >= _bill(double, "gemini") > 0
    assert len(_sent(double, "gemini")) == 1


@pytest.mark.parametrize(("provider", "lost"), [
    ("anthropic", httpx.ReadTimeout), ("openai", httpx.RemoteProtocolError), ("gemini", httpx.ReadError),
])
async def test_an_answer_lost_on_the_way_back_is_sent_once_and_metered_at_its_ceiling(
    db, packed, double, provider, lost
):
    """The provider answered -- the double billed it -- and the reply never arrived. The fetcher
    used to re-send the POST up to four times, buying the generation each time, and the one row read
    zero. Now it is sent once (decision 436 (1)) and, with no answer to settle it, stays at its
    ceiling (436 (3)), which is at least the bill; the title goes to the queue's curve.
    [M5.5 review cycle 1, M55-METER-02, M55-BUDGET-01, NBR-01]"""
    await _assign(db, provider)
    await _scenario(double, provider=provider, content="clean")

    async def dropped(request, response):
        raise lost("the reply was lost", request=request)

    outcome = await _extract(db, double, _Wrapped(double, dropped))

    assert (outcome.status, outcome.calls) == (extract.TRANSIENT, 1), outcome
    assert len(_sent(double, provider)) == 1
    [call] = await _metered(db)
    _tokens, ceiling = await _ceiling(db, provider)
    assert (call["ok"], call["usd"], call["response_document_id"]) == (False, ceiling, None)
    assert call["error"].startswith(spend.UNSETTLED_PREFIX), call["error"]
    assert call["usd"] >= _bill(double, provider) > 0


@pytest.mark.parametrize(("provider", "fault"), [
    ("gemini", 504), ("gemini", 503), ("openai", 503), ("anthropic", 529), ("anthropic", 500),
])
async def test_a_provider_error_status_is_settled_to_what_the_provider_says_it_billed(
    db, packed, double, provider, fault
):
    """A server error from the provider's own API is settled to zero and sent once. For Gemini that is
    Google's word -- "If your request fails with a 400 or 500 error, you won't be charged for the
    tokens used" (https://ai.google.dev/gemini-api/docs/billing) -- and its 504 is among them, where
    decision 436 as first taken left every 504 at its ceiling, charging the cap for calls Google
    documents as free. For Anthropic's 500 and 529 and OpenAI's 503 it is decision 436's reading, not a
    provider statement: neither error page says what a 5xx bills, and this docstring used to call every
    one of them "the provider's own statement that it did not serve the request". [M5.5 review cycle
    1, M55-SPEND-04, M55-BUDGET-01; review cycle 2, DBL-C2-04]"""
    await _assign(db, provider)
    await _scenario(double, provider=provider, fault=fault)

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.TRANSIENT, 1), outcome
    assert len(_sent(double, provider)) == 1
    [call] = await _metered(db)
    assert (call["ok"], call["tokens_in"], call["tokens_out_billed"], call["usd"]) == (False, 0, 0, 0)
    assert not call["error"].startswith(spend.UNSETTLED_PREFIX)
    assert _bill(double, provider) == 0


@pytest.mark.parametrize(("provider", "status"), [
    ("anthropic", 504), ("openai", 504), ("anthropic", 520), ("openai", 520), ("openai", 524),
])
async def test_an_anthropic_or_openai_504_after_sending_stays_at_its_ceiling(
    db, packed, double, provider, status
):
    """A gateway timeout from Anthropic or OpenAI may follow a generation that finished -- Anthropic's
    "504 - timeout_error: The request timed out while processing" (https://platform.claude.com/docs/en/
    api/errors) -- and neither page says it is free, so the attempt keeps its ceiling.

    SO DOES CLOUDFLARE'S 520, which it sends "when the origin server returns an empty, unknown, or
    unexpected response" -- an origin that crashed or closed the connection after taking the request
    (https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/
    error-520/) -- in front of the direct Claude API. The double answered and billed here, and a 520 in
    place of its reply was settled to $0. [M5.5 review cycle 2, M55-C2-METER-02]"""
    await _assign(db, provider)
    await _scenario(double, provider=provider, content="clean")

    async def gateway(request, response):
        return httpx.Response(status, content=b'{"error": {"type": "timeout_error",'
                                              b' "message": "timed out"}}', request=request)

    outcome = await _extract(db, double, _Wrapped(double, gateway))

    assert (outcome.status, outcome.calls) == (extract.TRANSIENT, 1), outcome
    assert len(_sent(double, provider)) == 1
    [call] = await _metered(db)
    _tokens, ceiling = await _ceiling(db, provider)
    assert call["usd"] == ceiling and call["error"].startswith(spend.UNSETTLED_PREFIX), dict(call)
    assert call["usd"] >= _bill(double, provider) > 0


async def test_a_raw_store_that_refuses_the_answer_leaves_the_attempt_settled_to_its_bill(
    db, packed, double, monkeypatch
):
    """`rawstore.store` came BEFORE the meter, so a full or unwritable data volume after a paid 200
    lost the row, and every walk of the title bought the answer again behind a cap that saw none of
    it. Decision 436 (3): tokens are settled before the envelope is stored, so the raise leaves the
    row at the reported usage with no document cited. [M5.5 review cycle 1, M55-BUDGET-04,
    M55-METER-03, M55-SPEND-02]"""
    await _assign(db, "openai")
    await _scenario(double, provider="openai", content="clean")

    async def full(*_args, **kwargs):
        if kwargs.get("kind") == extract.KIND:
            raise OSError(28, "No space left on device")
        return await real(*_args, **kwargs)

    real = extract.rawstore.store
    monkeypatch.setattr(extract.rawstore, "store", full)
    with pytest.raises(OSError):
        await _extract(db, double)

    [sent] = _sent(double, "openai")
    [call] = await _metered(db)
    tokens_in, tokens_out, billed = _billed("openai", sent["usage"])
    assert (call["ok"], call["tokens_in"], call["tokens_out_billed"], call["usd"]) == (
        True, tokens_in, tokens_out, billed)
    assert call["response_document_id"] is None


async def test_a_title_deleted_while_its_call_is_out_keeps_its_spend(db, packed, double):
    """Decision 430: "a deleted title does not delete its spend", held by `ON DELETE SET NULL` -- which
    only protects a row that exists. The row used to be INSERTed after the answer, naming the deleted
    id, so the INSERT raised and the spend was lost. Written ahead, the delete nulls it and the settle
    leaves `title_id` alone. [M5.5 review cycle 1, M55-METER-08]"""
    await _assign(db, "gemini")
    # A cut-off, so the extraction ends at the meter and nothing tries to write a tier for a title
    # that is gone.
    await _scenario(double, provider="gemini", envelope="max_tokens")

    async def deleted(request, response):
        await db.execute("DELETE FROM title WHERE id = $1", TITLE)
        return response

    assert (await _extract(db, double, _Wrapped(double, deleted))).status == extract.TRANSIENT

    [sent] = _sent(double, "gemini")
    [call] = await _metered(db)
    assert call["title_id"] is None
    assert call["usd"] == _billed("gemini", sent["usage"])[2] > 0


# --- M5.5 review cycle 2: the price the provider applied, and a ceiling that bounds the bill -------


@pytest.mark.parametrize(("provider", "account"), [
    ("openai", {"project_tier": "priority"}), ("anthropic", {"geo": "us"}),
], ids=["openai-project-fast", "anthropic-workspace-us"])
async def test_an_account_default_tier_or_geo_is_metered_at_what_the_provider_billed(
    db, packed, double, provider, account
):
    """Each provider applies an account-level price setting to a request that names none. An OpenAI
    project whose tier is Fast serves every unpinned request at twice the Standard row, and an Anthropic
    workspace migrated from the legacy US-only opt-out was "automatically configured with ...
    `default_inference_geo: "us"`", billed at 1.1x
    (https://platform.claude.com/docs/en/manage-claude/data-residency). Neither adapter named a tier or
    a geo, neither read the one the envelope reports, and `pricing.usd` applied the standard row: the
    meter held half the OpenAI bill and 0.909 of the Anthropic one, and the cap admitted spend it never
    recorded. OpenAI's request now pins `service_tier` "default" -- "processed with the standard pricing
    and performance for the selected model" -- and each adapter prices what the envelope says served
    it. [M5.5 review cycle 2, M55-C2-METER-01, DBL-C2-01]"""
    await _assign(db, provider)
    await _scenario(double, provider=provider, content="clean", **account)

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.WRITTEN, 1), outcome
    [sent] = _sent(double, provider)
    [call] = await _metered(db)
    assert call["usd"] == _billed(provider, sent["usage"], sent["service_tier"])[2] > 0
    assert call["usd"] == _bill(double, provider)
    if provider == "openai":
        assert sent["service_tier"] == "default", "the project's Fast default served an unpinned call"
    else:
        assert sent["usage"]["inference_geo"] == "us"


def _full_pack_reviews() -> list[tuple[str, str]]:
    """The largest pack `packs.render_pack` builds: `MAX_REVIEWS` reviews of `MAX_CHARS`, five sources
    at `MAX_PER_SOURCE` each, every one distinct at its start so none folds into another."""
    text = ("The harbour town keeps its secrets behind shuttered windows, and the film lets each one "
            "surface slowly, in long grey takes that trust the audience to wait for what it has earned. ")
    return [(source, f"Review {n} from {source}: " + text * 20)
            for source in ("imdb", "tmdb", "letterboxd", "rt", "mubi") for n in range(packs.MAX_PER_SOURCE)]


@pytest.mark.parametrize("pack", ["short", "full"])
async def test_a_lost_anthropic_cut_off_stays_in_the_meter_at_no_less_than_its_bill(
    db, packed, double, pack
):
    """Decision 436 (3) leaves an attempt whose answer never arrived at its write-ahead ceiling, and
    says the ceiling is at least the bill. On Claude it was not: the ceiling counted the prompt text at
    3.6 characters a token, where Anthropic bills the `tools` block too, a tool-use system prompt it
    injects ("Claude Sonnet 5 | 354 tokens | 474 tokens"), and "approximately 30% more tokens for the
    same text" on the newer tokenizer (https://platform.claude.com/docs/en/about-claude/pricing) -- and
    a cut-off spends every one of the 8,000 output tokens, so nothing on the output side absorbs the
    shortfall. A US-only workspace bills the whole of it at 1.1x. The double now counts Anthropic's
    input as the page does, and a Sonnet 5 cut-off lost on the way back, on a short pack and on the
    largest one `render_pack` builds, is held at no less than what the double billed.
    [M5.5 review cycle 2, M55-CAP-C2-01, M55-C2-METER-03]"""
    if pack == "full":
        text, info = packs.render_pack(TITLE, "Grey Harbour", 2021, "film", None, None, PLOT,
                                       _full_pack_reviews())
        await packs.store_pack(db, TITLE, "v1", text, info, entity_key=TASK_KEY)
    await _assign(db, "anthropic")
    await _scenario(double, provider="anthropic", envelope="max_tokens", geo="us")

    async def dropped(request, response):
        raise httpx.ReadError("the reply was lost", request=request)

    outcome = await _extract(db, double, _Wrapped(double, dropped))

    assert (outcome.status, outcome.calls) == (extract.TRANSIENT, 1), outcome
    [sent] = _sent(double, "anthropic")
    assert sent["usage"]["output_tokens"] == client.MAX_OUTPUT_TOKENS
    [call] = await _metered(db)
    assert call["error"].startswith(spend.UNSETTLED_PREFIX), call["error"]
    assert call["tokens_in"] >= sent["usage"]["input_tokens"], (call["tokens_in"], sent["usage"])
    assert call["usd"] >= _bill(double, "anthropic") > 0, (call["usd"], _bill(double, "anthropic"))


async def test_a_retired_model_parks_naming_the_model_rather_than_failing_the_title(db, packed, double):
    """Google's Gemini 2.5 family "still [appears] in ListModels but `generateContent` answers 404 'no
    longer available to new users'" (`mdc/config.py:152-154`), and it is still priced, so the plan made
    it and the test card called it listed. Decision 431 made the 404 final and each title that reached
    stage 6 failed for good, one by one, each needing a retry once the model was changed -- the harm
    M55-DBL-04 parked for a model that refuses forced tool use. A 404 from a paid endpoint is always the
    model's (the url names nothing else), so it parks naming the model. [M5.5 review cycle 2,
    DBL-C2-05]"""
    await _assign(db, "gemini")
    await registry.save_connector(db, "gemini", model="gemini-2.5-flash")

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.PLAN, 1), outcome
    assert "gemini-2.5-flash" in outcome.reason and "Admin" in outcome.reason, outcome.reason
    [call] = await _metered(db)
    assert (call["ok"], call["usd"]) == (False, 0)
    assert await _tier(db) == [(*BUNDLE_ROW, 2, None, None)]


async def test_a_gemini_daily_quota_waits_as_the_accounts_through_the_double(db, packed, double):
    """generateContent refuses a spent daily quota as google.rpc.Status -- `code` 429, `status`
    RESOURCE_EXHAUSTED and a `QuotaFailure` detail -- and the account refusal was read only off a string
    `code` "quota_exceeded", the Interactions API's shape, which this endpoint never sends: every title
    went to the queue's curve as a rate limit and burned its walks on a quota that resets at midnight
    Pacific. Through the double's own 429, it is the account's and waits. [M5.5 review cycle 2,
    DBL-C2-03]"""
    await _assign(db, "gemini")
    await _scenario(double, provider="gemini", fault=429)

    outcome = await _extract(db, double)

    assert outcome.status == extract.ACCOUNT, outcome
    assert "RESOURCE_EXHAUSTED" in outcome.reason, outcome.reason
    assert {r["status"] for r in _sent(double, "gemini")} == {429}
    [call] = await _metered(db)
    assert (call["ok"], call["usd"]) == (False, 0)


async def test_a_cancelled_attempt_says_it_was_cancelled_and_not_that_it_is_in_flight(
    db, packed, double
):
    """The write-ahead row said "the call is in flight ... until the provider's answer settles it",
    and a cancellation -- the drain's ordinary end -- left that sentence standing for good: weeks later
    the row still claimed a live call, indistinguishable from one on the wire. The cancellation is now
    written into the row before it propagates, and the ceiling stays. [M5.5 review cycle 2,
    M55-C2-METER-04, M55-CAP-C2-05]"""
    await _assign(db, "gemini")
    await _scenario(double, provider="gemini", content="clean")
    held = asyncio.Event()

    async def never(request, response):
        held.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(_extract(db, double, _Wrapped(double, never)))
    await asyncio.wait_for(held.wait(), 10)
    [in_flight] = await _metered(db)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [call] = await _metered(db)
    assert call["usd"] == in_flight["usd"] >= _bill(double, "gemini") > 0
    assert call["error"].startswith(spend.UNSETTLED_PREFIX), call["error"]
    assert "in flight" not in call["error"] and "cancelled" in call["error"], call["error"]


class _Echo(httpx.AsyncBaseTransport):
    """The double behind a proxy that copies the request's credential header into every answer it
    passes back -- a 200 whose usage block still reads, so the attempt is settled and its envelope
    stored (decision 436 (3))."""

    HEADERS = ("x-api-key", "authorization", "x-goog-api-key")

    def __init__(self, double):
        self.inner = httpx.ASGITransport(app=double.app)

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        body = json.loads(await response.aread())
        body["proxy_echo"] = {name: request.headers[name] for name in self.HEADERS
                              if name in request.headers}
        return httpx.Response(response.status_code, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json"}, request=request)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_proxy_that_echoes_the_key_into_a_readable_answer_never_puts_it_in_the_raw_store(
    db, packed, double, data_dir, provider
):
    """`extract._store` takes the key out of a provider's bytes before the raw store keeps them, and
    no test failed without it: the sweep above runs against a double that never echoes. Decision 436
    made a refused 200 storable, which is exactly the envelope a proxy's echo arrives in. So every
    answer here carries the request's key header, on attempts the validator refused and accepted and on
    one the adapter refused (a cut-off), each stored -- and no stored byte holds the key.
    [M5.5 review cycle 2, M55-KEYS-C2-01]"""
    key = double.KEYS[provider]
    await _assign(db, provider)
    assert (await _extract(db, double, _Echo(double))).status == extract.WRITTEN
    await _scenario(double, provider=provider, envelope="max_tokens")
    assert (await _extract(db, double, _Echo(double))).status == extract.TRANSIENT

    stored = await db.fetch("SELECT content_sha256 FROM raw_document WHERE source = $1",
                            f"llm:{provider}")
    assert len(stored) == 3, stored
    files = list(settings().raw_dir.rglob("*.gz"))
    echoed = [path for path in files if b"proxy_echo" in gzip.decompress(path.read_bytes())]
    assert len(echoed) == 3, files
    assert not [path for path in echoed if key.encode() in gzip.decompress(path.read_bytes())]


@pytest.mark.parametrize(("provider", "status", "body"), [
    ("anthropic", 400, {"type": "error", "error": {"type": "invalid_request_error", "message":
     "You have reached your specified API usage limits. You will regain access on 2026-10-01."}}),
    ("openai", 429, {"error": {"message": "Your organization has no prepaid credits remaining.",
                               "type": "insufficient_quota", "param": None,
                               "code": "credit_balance_exhausted"}}),
    ("gemini", 402, {"error": {"code": 402, "message": "Your Prepay credit balance is depleted."}}),
])
async def test_a_refusal_of_the_households_account_waits_rather_than_failing_the_title(
    db, packed, double, provider, status, body
):
    """A spend limit, an exhausted balance or a daily quota refuses the ACCOUNT, and lifts when the
    month rolls over or the household tops up -- decision 336's waiting on something that may change.
    Decision 431 failed every such title for good on a 4xx and burned four walks each on a 429, so a
    careful household that set a limit in the provider's console lost every title that reached stage 6
    for the rest of the month. It is `ACCOUNT` now, which stage 6 parks; nothing was billed.
    [M5.5 review cycle 1, M55-BUDGET-07]"""
    await _assign(db, provider)

    def refused(request):
        return httpx.Response(status, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json"})

    outcome = await _extract(db, double, httpx.MockTransport(refused))

    assert outcome.status == extract.ACCOUNT, outcome
    assert body["error"]["message"][:40] in outcome.reason
    assert "resumes" in outcome.reason and "decision 336" in outcome.reason
    [call] = await _metered(db)
    assert (call["ok"], call["usd"]) == (False, 0)


async def test_openai_cache_writes_are_metered_at_the_published_write_rate(db, packed, double):
    """gpt-5.6-terra writes every stage-6 prompt to OpenAI's cache by default and bills the write at
    1.25x the input rate; attempt 2 extends the user message, so it writes again rather than reading.
    The meter read `prompt_tokens` alone at the flat rate. With the double reporting the writes the
    published object documents, the two rows sum to its bill to the micro-dollar.
    [M5.5 review cycle 1, M55-DBL-02, M55-METER-07, M55-DOC-05]"""
    await _assign(db, "openai")
    await _scenario(double, provider="openai", prompt_tokens=23_500)

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.WRITTEN, 2), outcome
    written = [r["usage"]["prompt_tokens_details"]["cache_write_tokens"] for r in _sent(double, "openai")]
    assert written == [23_500, 23_500]
    calls = await _metered(db)
    assert sum(c["usd"] for c in calls) == _bill(double, "openai")
    assert [c["usd"] for c in calls] == [_billed("openai", r["usage"])[2] for r in _sent(double, "openai")]


async def test_each_attempt_is_priced_on_its_own_day(db, packed, double, monkeypatch):
    """The plan's price was read once and every attempt metered at it, so a title whose calls ran
    past midnight on 2027-01-01 was metered at the introductory Gemini price Google stopped charging
    at that midnight (decision 343's dated table). Each attempt now asks for its own day's price.
    [M5.5 review cycle 1, M55-SPEND-06]"""
    await _assign(db, "gemini")
    await _scenario(double, provider="gemini", content="clean")

    class _Midnight(datetime):
        reads = 0

        @classmethod
        def now(cls, tz=None):
            cls.reads += 1
            day = datetime(2026, 12, 31, 12, tzinfo=UTC) if cls.reads == 1 else datetime(
                2027, 1, 1, 12, tzinfo=UTC)
            return day if tz is None else day.astimezone(tz)

    monkeypatch.setattr(spend, "datetime", _Midnight)
    outcome = await _extract(db, double)

    assert outcome.status == extract.WRITTEN
    [call] = await _metered(db)
    doubled = pricing.price_for("gemini", MODELS["gemini"], on=date(2027, 1, 1))
    assert (doubled.input, doubled.output) == (1.50, 7.50)
    assert call["usd"] == pricing.usd(call["tokens_in"], call["tokens_out_billed"], doubled)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_provider_that_refuses_the_key_fails_for_good_without_a_retry(
    db, packed, double, provider
):
    """A provider 4xx is final (decision 431): a key the provider refused today is refused on the
    queue's next attempt too, and a re-run would only bill the household for being told again. The
    provider's own words reach the row and the reason; the key that was sent reaches neither."""
    wrong = f"not-the-{provider}-key-at-all-000"
    await _keys(db, double, **{provider: wrong})
    await _assign(db, provider)

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.REFUSED, 1), outcome
    assert outcome.detail["http_status"] in (400, 401)
    [call] = await _metered(db)
    assert call["ok"] is False and call["error"].startswith(f"HTTP {outcome.detail['http_status']}")
    assert wrong not in call["error"] and wrong not in outcome.reason
    assert await _tier(db) == [(*BUNDLE_ROW, 2, None, None)]


# --- decision 337: runs, merged and written ---------------------------------------------------


async def test_parallel_mode_writes_one_row_per_provider_for_a_term_both_found(db, packed, double):
    """Two providers at one pass each are two runs; a term both found is two rows, one under each
    provider, each carrying the pooled weights -- agreement 2 of 2 -- and its own provider's quote.
    Four calls: each run is its own two-attempt loop."""
    await _assign(db, "gemini", parallel=True, parallel_providers=["gemini", "anthropic"])
    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls, outcome.n_tags) == (extract.WRITTEN, 4, 6), outcome
    tier = await _tier(db)
    assert [(term, provider) for term, provider, *_ in tier] == [
        (term, provider) for term in REAL_TERMS for provider in ("anthropic", "gemini")]
    assert {(conf, n) for *_, conf, n in tier} == {(1.0, 2)}
    refs = await db.fetch(
        "SELECT t.provider, e.source_ref FROM dna_evidence e JOIN dna_tag t ON t.id = e.dna_tag_id"
        " WHERE t.title_id = $1", TITLE)
    assert {(r["provider"], r["source_ref"]) for r in refs} == {("gemini", "gemini:1"),
                                                                ("anthropic", "anthropic:1")}
    assert [(c["provider"], c["attempt"]) for c in await _metered(db)] == [
        ("gemini", 1), ("gemini", 2), ("anthropic", 1), ("anthropic", 2)]


async def test_one_failed_run_ends_the_extraction_before_the_next_run_is_paid_for(db, packed, double):
    """Parallel mode, and the first provider's run ends `violated`: the second provider is never
    called, because its answer could not be written -- the merge weighs every tag against the
    runs the plan names, and a run that failed leaves that total unfinishable -- and a paid call
    whose answer cannot be used is what the cap exists to prevent. The tier is untouched."""
    await _assign(db, "gemini", parallel=True, parallel_providers=["gemini", "anthropic"])
    await _scenario(double, provider="gemini", posture="stubborn")

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.VIOLATED, 2), outcome
    assert (outcome.detail["runs"], outcome.detail["runs_accepted"]) == (2, 0)
    assert len(_sent(double, "gemini")) == 2 and _sent(double, "anthropic") == []
    assert {c["provider"] for c in await _metered(db)} == {"gemini"}
    assert await _tier(db) == [(*BUNDLE_ROW, 2, None, None)]


async def test_two_passes_of_one_provider_are_two_runs_of_one_row_each(db, packed, double):
    """Decision 337 pools a provider's passes as runs: two passes of Gemini are two runs, so a term
    both found is ONE row (one provider) at agreement 2 of 2, holding both runs' quotes -- told
    apart by `source_ref` -- and the meter holds four rows numbered by pass and attempt."""
    await _assign(db, "gemini", passes=2)
    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls, outcome.n_tags) == (extract.WRITTEN, 4, 3), outcome
    assert {(provider, conf, n) for _t, provider, _s, conf, n in await _tier(db)} == {("gemini", 1.0, 2)}
    refs = await db.fetch(
        "SELECT t.term, e.source_ref FROM dna_evidence e JOIN dna_tag t ON t.id = e.dna_tag_id"
        " WHERE t.title_id = $1 ORDER BY t.term, e.source_ref", TITLE)
    assert [tuple(r) for r in refs] == [(term, f"gemini:{i}") for term in REAL_TERMS for i in (1, 2)]
    assert [(c["pass_index"], c["attempt"]) for c in await _metered(db)] == [(1, 1), (1, 2), (2, 1), (2, 2)]


async def test_a_curated_drop_survives_a_fresh_extraction(db, packed, double):
    """The household ruled `mood.bleak` off this title, and the vocabulary still carries the term,
    so `verify_payload` passes it (its ledger read asks only about terms the vocabulary does not
    carry). `consensus.store_title` hands the ledger to its caller, and this extraction applies it
    inside the write's transaction -- without that, every pipeline run would put the verdict back,
    which is §14 risk 5's reverted-rows scar."""
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, origin)"
        " VALUES ('v1', 'title', $1, 'mood.bleak', 'drop', 'household')", TITLE)
    await _assign(db, "gemini")

    outcome = await _extract(db, double)

    assert outcome.status == extract.WRITTEN
    assert [term for term, *_ in await _tier(db)] == ["pacing.slow_burn", "themes.revenge"]
    assert outcome.detail["adjudications"]["dropped"] == 1


# --- decision 432: nothing is called until everything that could refuse has been read ------------


@pytest.mark.parametrize("missing", ["plan", "vocabulary", "pack"])
async def test_nothing_is_called_without_a_plan_a_vocabulary_or_a_pack(
    db, data_dir, secrets_key, double, missing
):
    """No extraction provider assigned (decision 324: never a guessed one), no active vocabulary
    (§3.1: a bundle-less install declines to verify rather than refusing everything), or no stored
    pack (stage 5 is not wired, decision 432): each is its own answer, with zero requests to any
    provider and nothing metered."""
    await _keys(db, double)
    if missing != "vocabulary":
        await _vocabulary(db)
    await _title(db)
    if missing == "plan":
        await _pack(db)
    else:
        await _assign(db, "gemini")

    outcome = await _extract(db, double)

    expected = {"plan": extract.PLAN, "vocabulary": extract.NO_VOCABULARY, "pack": extract.NO_PACK}
    assert (outcome.status, outcome.calls) == (expected[missing], 0), outcome
    assert double.state.requests == []
    assert await _metered(db) == []
    if missing == "pack":
        assert "stage 5" in outcome.reason and "decision 432" in outcome.reason
    if missing == "plan":
        assert "extraction provider" in outcome.reason


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1",
                                   "claude-opus-5-5-20260801"])
async def test_a_model_that_refuses_forced_tool_use_parks_before_anything_is_sent(
    db, packed, double, model
):
    """Claude Opus 5.5, Fable 5.1 and Mythos 5.1 answer the Anthropic adapter's forced `tool_choice`
    with a 400 on every request (https://platform.claude.com/docs/en/api/errors), and decision 431 makes
    a provider 400 a permanent failure. So an admin who picked one from Anthropic's own list -- priced,
    for Opus 5.5 -- used to have every title in the queue sent once, refused and failed for good, one
    by one. It is a setting to correct: stage 6 parks naming the model, in the shape of the unpriced
    model's park, with no request sent and nothing metered, and resumes once the model is changed. A
    dated snapshot is the same model under `price_for`'s boundary rule. [M5.5 review cycle 1,
    M55-DBL-04]"""
    await _assign(db, "anthropic")
    await registry.save_connector(db, "anthropic", model=model, price_input=4, price_output=20)

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.PLAN, 0), outcome
    assert model in outcome.reason and "forced tool use" in outcome.reason
    assert double.state.requests == []
    assert await _metered(db) == []

    await registry.save_connector(db, "anthropic", model="claude-opus-5", price_input=None,
                                  price_output=None)
    assert (await _extract(db, double)).status == extract.WRITTEN


# --- exit measures 10-12: no key in any url, log line or row --------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_no_provider_key_reaches_the_raw_store_the_wire_the_log_or_any_row(
    db, packed, double, data_dir, caplog, provider
):
    """§9: "header-only API key so credentials never hit logs", held by what was RECORDED.

      * `raw_document.url` for every paid answer is the documented endpoint exactly: no key and no
        query string at all, which is where Gemini's documented `?key=` would have put it.
      * The double saw every request's url without a key and received the key in the header its
        provider documents -- `x-api-key`, `Authorization: Bearer`, `x-goog-api-key`.
      * httpx's own INFO line, one per request with the full url, was written for both calls and
        names the url and never the key.
      * After the run, no row of any table holds the plaintext key -- the meter, the refusals, the
        raw store's `request_meta`, the host state -- and no byte the raw store kept does either.
        `connector_config` holds the key sealed, which is the one place it is meant to be.
    """
    caplog.set_level(logging.INFO, logger="httpx")
    key = double.KEYS[provider]
    await _assign(db, provider)

    outcome = await _extract(db, double)

    assert outcome.status == extract.WRITTEN and outcome.calls == 2
    assert key not in repr(outcome) and key not in outcome.reason

    urls = [r["url"] for r in await db.fetch(
        "SELECT url FROM raw_document WHERE source LIKE 'llm:%' ORDER BY id")]
    assert urls == [URLS[provider]] * 2
    assert all(key not in url and "?" not in url for url in urls), urls

    sent = _sent(double, provider)
    assert [r["url"] for r in sent] == [URLS[provider]] * 2
    assert [(r["key_in_url"], r["key_header"]) for r in sent] == [(False, True)] * 2

    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert sum(URLS[provider] in line for line in lines) == 2, lines
    assert not [line for line in lines if key in line], lines

    holders = []
    tables = await db.fetch(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
        " AND table_schema NOT IN ('pg_catalog', 'information_schema')")
    assert len(tables) > 20, tables
    for table in tables:
        name = f'"{table["table_schema"]}"."{table["table_name"]}"'
        rows = await db.fetch(f"SELECT t::text AS row FROM {name} t")
        holders += [name for row in rows if key in row["row"]]
    assert holders == [], holders
    stored = list(settings().raw_dir.rglob("*.gz"))
    assert len(stored) >= 3, stored
    assert not [path for path in stored if key.encode() in gzip.decompress(path.read_bytes())]


async def test_a_provider_error_that_quotes_the_key_is_written_without_it(db, packed, double, monkeypatch):
    """The adapters redact the key from every error they raise (`client._redacted`), and this
    module redacts it again at the write, because the text lands in `llm_call.error` and in the
    reason §6.6's board shows. The double never echoes a key, so a proxy that did is stood in for
    by one `client.complete` that raises with the key in its words; what is asserted is what the
    row and the outcome RECORDED."""
    key = double.KEYS["gemini"]
    await _assign(db, "gemini")

    async def echoing(*_args, **_kwargs):
        raise client.LLMError(f"HTTP 400: the proxy refused key {key} for this request",
                              retryable=False, status=400)

    monkeypatch.setattr(client, "complete", echoing)
    outcome = await _extract(db, double)

    assert outcome.status == extract.REFUSED
    [call] = await _metered(db)
    assert "[redacted]" in call["error"] and key not in call["error"]
    assert key not in outcome.reason and key not in repr(outcome.detail)


# --- the seams, by reading the module ------------------------------------------------------------


def test_the_extraction_asks_the_validator_and_reimplements_none_of_its_checks():
    """§9: "the guarantee is the validator", so stage 6 calls M5.4's `verify_payload` and holds
    none of its checks: it imports neither the fold nor the alias map the checks are made with, and
    names none of the constants or methods they read. It imports nothing from the driver or the
    stage machine (it answers in plain values), no HTTP client (every request is `client.complete`
    through the fetcher it is handed), and it meters through `spend.record_call`."""
    tree = ast.parse(EXTRACT_SOURCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" for alias in node.names} | {node.module}
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    forbidden = ("spielplan.acquire.pipeline", "spielplan.acquire.stages", "spielplan.dna.norm",
                 "spielplan.dna.aliases", "httpx")
    assert not {m for m in imported if m.startswith(forbidden)}, imported

    named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not named & {"norm", "SALIENCE_LEVELS", "TERM_KEYS", "QUOTE_KEYS", "SOURCE_KEYS",
                        "SALIENCE_KEYS", "resolve", "repair", "alias_of"}, named
    assert {"verify_payload", "record_rejects", "complete", "record_call", "violation_prompt",
            "as_verifier_payload", "merge_passes", "store_title"} <= named
