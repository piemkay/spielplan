"""Stage 6's extraction for one title against `ops/fake_llm.py` behind a real `Fetcher` (§9): retried once
with the violation named, failed for good on a second, identical on all three adapters, metered as
billed, and no key anywhere. Needs TEST_DATABASE_URL."""

from __future__ import annotations

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

PROVIDERS = ("anthropic", "openai", "gemini")
MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5.6-terra", "gemini": "gemini-3.7-flash"}
# The documented endpoint of each paid call, which is what `raw_document.url` must equal.
URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent",
}

TITLE = 7
TASK_KEY = "title:7"
RUN_ID = 4242

# The double's first answer: three real tags and one term it invented under the first facet.
REAL_TERMS = ["mood.bleak", "pacing.slow_burn", "themes.revenge"]
INVENTED = "mood.mecha"
# The first term no real tag uses, which the `unquotable` and `salience` scenarios ride on.
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

# Each content scenario, the rule `verify_tags` refuses it under, and the term its refusal names.
VIOLATIONS = {
    "fabricate": ("unknown_term", INVENTED),
    "unquotable": ("quote_unverified", SPARE),
    "salience": ("schema", SPARE),
}


@pytest.fixture
def double():
    """Registered in `sys.modules` before it runs, for Pydantic's sake."""
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


def test_the_double_recognises_the_retry_opening_the_app_sends(double):
    assert double.RETRY_MARKER == contract.RETRY_MARKER


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
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
    text, info = packs.render_pack(TITLE, "Grey Harbour", 2021, "film", None, None, PLOT, REVIEWS)
    return await packs.store_pack(db, TITLE, "v1", text, info, entity_key=TASK_KEY)


async def _bundle_row(db) -> None:
    tag = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) "
        "VALUES ($1, 'v1', 'themes.robots', 'themes', 2) RETURNING id", TITLE,
    )
    await db.execute("INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'imdb:1')",
                     tag, "a director who refuses every consolation")


async def _keys(db, double, **overrides) -> None:
    for provider in PROVIDERS:
        await registry.save_connector(db, provider, api_key=overrides.get(provider, double.KEYS[provider]))


async def _assign(db, provider="gemini", **settings_) -> None:
    fields = {"extraction_provider": provider, "parallel": False, "passes": 1}
    await registry.save_connector(db, "llm", **{**fields, **settings_})


@pytest.fixture
async def packed(db, data_dir, secrets_key, double) -> int:
    """Returns the pack's raw document id."""
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
    """`transport` wraps the double for a test that needs the network to misbehave around a real answer."""
    clock = _Clock()
    async with fetch.Fetcher(conn=db, transport=transport or httpx.ASGITransport(app=double.app),
                             clock=clock, sleep=clock.sleep, jitter=lambda low, high: 0.0) as fetcher:
        return await extract.extract_title(db, title_id=TITLE, fetcher=fetcher, task_key=TASK_KEY,
                                           run_id=RUN_ID)


async def _scenario(double, **fields) -> None:
    """The double's control route refuses an envelope a provider does not document."""
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


# Published per-1M prices (input, cache write, cache read, output):
# https://platform.claude.com/docs/en/about-claude/pricing, https://developers.openai.com/api/docs/pricing,
# https://ai.google.dev/gemini-api/docs/pricing.
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
    """Read off the usage block the double SENT, rounded per call to six places, at the multiplier the
    envelope reports (US-only inference 1.1x; OpenAI "priority" twice Standard)."""
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
    return sum((_billed(provider, r.get("usage"), r.get("service_tier"))[2]
                for r in _sent(double, provider)), Decimal("0"))


async def _ceiling(db, provider: str, user: str | None = None) -> tuple[int, Decimal]:
    """An attempt's write-ahead ceiling (decision 436): the
    prompt with the adapter's margin plus `max_tokens`."""
    voc = await contract.load_prompt_vocabulary(db, "v1")
    pack = await verify.read_pack(db, TITLE, "v1")
    tokens_in = client.ceiling_input(provider, contract.system_prompt(voc),
                                     user if user is not None else contract.user_prompt(pack),
                                     contract.EXTRACTION_SCHEMA)
    price = pricing.price_for(provider, MODELS[provider])
    return tokens_in, pricing.ceiling(tokens_in, client.MAX_OUTPUT_TOKENS, price,
                                      rate=client.ceiling_rate(provider))


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_fabricated_term_is_rejected_and_retried_once_with_the_rule_and_the_term_named(
    db, packed, double, provider
):
    """The retry is read off the double's request log, as the provider received it."""
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


@pytest.mark.parametrize("content", sorted(VIOLATIONS))
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_second_violation_fails_for_good_and_leaves_the_tier_as_it_was(
    db, packed, double, provider, content
):
    """Over a tier that holds a row, so "unchanged" is not "still empty"."""
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
    """One comparison, not three passing tests: the outcomes must be the same tuple."""
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


@pytest.mark.parametrize(("thoughts", "billed"), [(True, 1_600 + 2_300), (False, 1_600)])
async def test_a_gemini_call_is_metered_at_its_candidate_plus_its_thought_tokens(
    db, packed, double, thoughts, billed
):
    """§9: "counting visible JSON understates cost ~5x"."""
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
    """Adding `reasoning_tokens` to `completion_tokens` would charge the reasoning twice."""
    await _assign(db, "openai")
    await _scenario(double, provider="openai", content="clean", thoughts=thoughts)
    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.WRITTEN, 1)
    [call] = await _metered(db)
    assert call["tokens_out_billed"] == completion
    price = pricing.price_for("openai", MODELS["openai"])
    assert call["usd"] == pricing.usd(call["tokens_in"], completion, price)


# Failure envelopes a provider reports usage for and charges nothing, with a model that can send them.
UNCHARGED = {("anthropic", "refusal"): "claude-opus-5"}


@pytest.mark.parametrize(("provider", "envelope", "status"), [
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
    """Every such 200 was billed, so the row is the bill;
    an Anthropic refusal before output charges nothing."""
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
    """`SUM(llm_call.usd)` must equal the double's own bill;
    only a lost answer may exceed it, at its ceiling."""
    # Not Gemini's 429: the fetcher re-sends a 429 in-process, and it bills nothing either way.
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
    """Each cut-off now moves the sum by what it billed, so the gate refuses before the fifth walk."""
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
    """`after` sees the double's response and may raise, stall
    or rewrite it; the double's log still holds the bill."""

    def __init__(self, double, after):
        self.inner = httpx.ASGITransport(app=double.app)
        self.after = after

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        await response.aread()
        return await self.after(request, response)


async def test_an_attempt_cancelled_mid_call_stays_in_the_meter_at_its_ceiling(db, packed, double):
    """Decision 436 (2): the row is committed at its ceiling
    before the POST, so a cancel leaves it in the month."""
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
    """Sent once, and with no answer to settle it the row stays at its ceiling."""
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
    """Gemini 5xx is free per https://ai.google.dev/gemini-api/docs/billing;
    other 5xx are decision 436's reading."""
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
    """A 504 or Cloudflare 520 may follow a finished generation, so the attempt keeps its ceiling."""
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
    """Tokens are settled before the envelope is stored, so a raw-store failure keeps the row."""
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
    """Written ahead, so a title deleted mid-call is nulled by SET NULL and the spend stays."""
    await _assign(db, "gemini")
    # A cut-off, so nothing tries to write a tier for a title that is gone.
    await _scenario(double, provider="gemini", envelope="max_tokens")

    async def deleted(request, response):
        await db.execute("DELETE FROM title WHERE id = $1", TITLE)
        return response

    assert (await _extract(db, double, _Wrapped(double, deleted))).status == extract.TRANSIENT

    [sent] = _sent(double, "gemini")
    [call] = await _metered(db)
    assert call["title_id"] is None
    assert call["usd"] == _billed("gemini", sent["usage"])[2] > 0


@pytest.mark.parametrize(("provider", "account"), [
    ("openai", {"project_tier": "priority"}), ("anthropic", {"geo": "us"}),
], ids=["openai-project-fast", "anthropic-workspace-us"])
async def test_an_account_default_tier_or_geo_is_metered_at_what_the_provider_billed(
    db, packed, double, provider, account
):
    """An unpinned request takes the account's tier or
    geo; each adapter prices what the envelope reports."""
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
    """Distinct at its start, so no review folds into another."""
    text = ("The harbour town keeps its secrets behind shuttered windows, and the film lets each one "
            "surface slowly, in long grey takes that trust the audience to wait for what it has earned. ")
    return [(source, f"Review {n} from {source}: " + text * 20)
            for source in ("imdb", "tmdb", "letterboxd", "rt", "mubi") for n in range(packs.MAX_PER_SOURCE)]


@pytest.mark.parametrize("pack", ["short", "full"])
async def test_a_lost_anthropic_cut_off_stays_in_the_meter_at_no_less_than_its_bill(
    db, packed, double, pack
):
    """A cut-off whose answer is lost stays at its ceiling, which the tokenizer margin keeps >= the bill."""
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
    """A 404 from a paid endpoint is always the model's, so it parks naming the model."""
    await _assign(db, "gemini")
    await registry.save_connector(db, "gemini", model="gemini-2.5-flash")

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.PLAN, 1), outcome
    assert "gemini-2.5-flash" in outcome.reason and "Admin" in outcome.reason, outcome.reason
    [call] = await _metered(db)
    assert (call["ok"], call["usd"]) == (False, 0)
    assert await _tier(db) == [(*BUNDLE_ROW, 2, None, None)]


async def test_a_gemini_daily_quota_waits_as_the_accounts_through_the_double(db, packed, double):
    """generateContent refuses a daily quota as google.rpc.Status
    429 RESOURCE_EXHAUSTED with `QuotaFailure`."""
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
    """A cancelled attempt must not keep saying it is in flight."""
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
    """A proxy that copies the credential header into a readable 200."""

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
    """The key is taken out of a provider's bytes before the raw store keeps them."""
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
    """An account refusal lifts later, so it parks rather than failing every title."""
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
    """Attempt 2 extends the user message, so it writes the cache again rather than reading."""
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
    """Each attempt is priced on its own local day."""
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
    """A re-run of a refused key only bills the household for being told again."""
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


async def test_parallel_mode_writes_one_row_per_provider_for_a_term_both_found(db, packed, double):
    """Four calls: each run is its own two-attempt loop."""
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
    """A failed run makes the merge unfinishable, so the next provider is never paid for."""
    await _assign(db, "gemini", parallel=True, parallel_providers=["gemini", "anthropic"])
    await _scenario(double, provider="gemini", posture="stubborn")

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.VIOLATED, 2), outcome
    assert (outcome.detail["runs"], outcome.detail["runs_accepted"]) == (2, 0)
    assert len(_sent(double, "gemini")) == 2 and _sent(double, "anthropic") == []
    assert {c["provider"] for c in await _metered(db)} == {"gemini"}
    assert await _tier(db) == [(*BUNDLE_ROW, 2, None, None)]


async def test_two_passes_of_one_provider_are_two_runs_of_one_row_each(db, packed, double):
    """One provider's two passes are two runs of ONE row, told apart by `source_ref`."""
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
    """`verify_tags` passes a live term, so the ledger is applied inside the write's transaction."""
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, origin)"
        " VALUES ('v1', 'title', $1, 'mood.bleak', 'drop', 'household')", TITLE)
    await _assign(db, "gemini")

    outcome = await _extract(db, double)

    assert outcome.status == extract.WRITTEN
    assert [term for term, *_ in await _tier(db)] == ["pacing.slow_burn", "themes.revenge"]
    assert outcome.detail["adjudications"]["dropped"] == 1


@pytest.mark.parametrize("missing", ["plan", "vocabulary", "pack"])
async def test_nothing_is_called_without_a_plan_a_vocabulary_or_a_pack(
    db, data_dir, secrets_key, double, missing
):
    """Each is its own answer, with zero requests and nothing metered."""
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


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"])
async def test_the_claude_models_that_refuse_forced_tool_use_extract_by_structured_outputs(
    db, packed, double, model
):
    """Decision 535: these 400 on a forced `tool_choice`, so the schema travels in `output_config`."""
    await _assign(db, "anthropic")
    await registry.save_connector(db, "anthropic", model=model)
    await _scenario(double, provider="anthropic", content="clean")

    outcome = await _extract(db, double)

    assert (outcome.status, outcome.calls) == (extract.WRITTEN, 1), outcome
    assert [r["status"] for r in double.state.requests if r["method"] == "POST"] == [200]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_no_provider_key_reaches_the_raw_store_the_wire_the_log_or_any_row(
    db, packed, double, data_dir, caplog, provider
):
    """What was RECORDED holds the rule: urls, the double's
    headers, httpx's INFO line, every row and byte."""
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
    """The text lands in `llm_call.error` and the board, so it is redacted again at the write."""
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
