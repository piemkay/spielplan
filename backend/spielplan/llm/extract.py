"""Stage 6 for one title: call, store, meter, verify, retry once, merge, write (§8, §9).

Each attempt is metered at its ceiling before it is sent and its answer stored before it is judged
(decision 436). A second contract violation, or any failed run, writes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from spielplan.acquire import rawstore
from spielplan.db.dna_terms import active_version
from spielplan.derive import ledgers
from spielplan.dna import packs, verify
from spielplan.llm import client, consensus, contract, pricing, spend

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import asyncpg

    from spielplan.acquire import fetch

# What `extract_title` can answer, and what each means to the stage that maps it to a verb.
WRITTEN = "written"              # every run accepted, merged and written
NO_PACK = "no_pack"              # nothing to extract from; stage 5 builds it (decision 432)
NO_VOCABULARY = "no_vocabulary"  # nothing to verify against (§3.1: a bundle-less app is legal)
PLAN = "plan"                    # decisions 324/343 name no callable, priced, keyed provider
VIOLATED = "violated"            # a second contract violation: final (decision 431)
REFUSED = "refused"              # a non-retryable provider failure: final (decision 431)
TRANSIENT = "transient"          # a retryable provider failure: the queue's curve (decision 431)
# The household's account was refused; waits for the period or a top-up (decision 336).
ACCOUNT = "account"
# The breaker refused before anything was billed; waits out the pause without spending an attempt.
PAUSED = "paused"

# Raw-store source for a paid answer, one per provider.
SOURCE_PREFIX = "llm:"
KIND = "dna:extract"

# Attempt 1 is the ask; attempt 2 names what attempt 1 got wrong.
ATTEMPTS = (1, 2)

_ZERO = Decimal("0.000000")


@dataclass(frozen=True)
class Extraction:
    """What one title's extraction came to, in plain values.

    `detail` holds JSON values only (money as digit strings); `calls` equals the `llm_call` rows added.
    """

    status: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    n_tags: int = 0
    calls: int = 0


@dataclass
class _Run:
    """One run's result: the tags it was accepted with, or the status and reason it ended on."""

    tags: list[verify.VerifiedTag] | None = None
    status: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    calls: int = 0
    usd: Decimal = _ZERO


@dataclass(frozen=True)
class _Asked:
    """Read once before the first call and shared by every attempt of every run."""

    title_id: int
    task_key: str | None
    run_id: int | None
    voc: verify.Vocabulary
    pack: str
    pack_document_id: int
    system: str
    user: str
    prompt_sha: str


_PACK_ROW = "SELECT pack_sha, raw_document_id FROM dna_pack WHERE title_id = $1 AND version = $2"


async def extract_title(
    conn: asyncpg.Connection,
    *,
    title_id: int,
    fetcher: fetch.Fetcher | None = None,
    task_key: str | None,
    run_id: int | None = None,
    open_fetcher: Callable[[], Awaitable[fetch.Fetcher]] | None = None,
    batch: Mapping[str, Any] | None = None,
) -> Extraction:
    """Extract, verify and write one title's DNA through every provider decision 324 plans.

    Every read that could refuse comes before any call; `open_fetcher` is asked only then. The spend
    cap is checked by the driver's gate, not here. `batch` is passed to the planner as the gate saw it.
    """
    plan = await spend.extraction_plan(conn, batch=batch)
    if isinstance(plan, spend.Refusal):
        return Extraction(PLAN, plan.reason, dict(plan.detail))

    version = await active_version(conn)
    voc = None if version is None else await verify.load_vocabulary(conn, version)
    prompt_voc = None if version is None else await contract.load_prompt_vocabulary(conn, version)
    if voc is None or prompt_voc is None:
        # No vocabulary: decline to check rather than reject every tag.
        return Extraction(
            NO_VOCABULARY,
            "no DNA vocabulary is active on this install, so no answer could be verified and no"
            " provider is called (section 3.1: a bundle-less install is a legal state). Import the"
            " bundle, and this title resumes here",
            {"version": version},
        )

    row = await conn.fetchrow(_PACK_ROW, title_id, version)
    pack = None if row is None else await verify.read_pack(conn, title_id, version)
    if row is None or pack is None:
        return Extraction(
            NO_PACK,
            f"no DNA pack is stored for this title under vocabulary {version}, so there is nothing"
            " to extract from and no provider is called. Section 8 stage 5 builds the pack, and"
            " this walk resumed past it (decision 432); once this park's deadline passes the title"
            " re-enters at stage 5, which stores one before stage 6 asks again (decision 467)",
            {"version": version},
        )
    if packs.sha(pack) != row["pack_sha"]:
        # The pack was rebuilt between the row read and the text read; fail and retry on the curve.
        raise OSError(
            f"the dna_pack row for title {title_id} changed while stage 6 read it; the call would "
            "cite a pack it did not send (decision 430)"
        )

    asked = _Asked(
        title_id=title_id, task_key=task_key, run_id=run_id, voc=voc, pack=pack,
        pack_document_id=row["raw_document_id"],
        system=contract.system_prompt(prompt_voc), user=contract.user_prompt(pack),
        prompt_sha=contract.prompt_sha(prompt_voc),
    )

    if fetcher is None:
        if open_fetcher is None:
            raise ValueError("stage 6 was handed neither the drain's Fetcher nor its supply (decision 373)")
        fetcher = await open_fetcher()
    runs: dict[str, list[verify.VerifiedTag]] = {}
    calls, spent = 0, _ZERO
    for planned in plan.providers:
        for pass_index in range(1, plan.passes + 1):
            run = await _run(conn, fetcher, asked, planned, pass_index, billed_before=spent > 0)
            calls += run.calls
            spent += run.usd
            if run.tags is None:
                detail = {**run.detail, "calls": calls, "usd": str(spent), "runs": plan.runs,
                          "runs_accepted": len(runs)}
                return Extraction(run.status, run.reason, detail, calls=calls)
            runs[consensus.pass_id_for(planned.provider, pass_index)] = run.tags

    merged, n_runs = consensus.merge_passes(runs)
    async with conn.transaction():
        written = await consensus.store_title(conn, title_id, version, merged, n_runs=n_runs)
        ruled = await ledgers.apply_adjudications(conn, title_id)
    providers = [planned.provider for planned in plan.providers]
    return Extraction(
        WRITTEN,
        f"{written} tag row(s) written for vocabulary {version} from {n_runs} run(s) of"
        f" {', '.join(providers)}, in {calls} paid call(s)",
        {"version": version, "providers": providers, "passes": plan.passes, "runs": n_runs,
         "terms": len(merged), "rows": written, "calls": calls, "usd": str(spent),
         "adjudications": ruled},
        n_tags=written, calls=calls,
    )


async def _run(
    conn: asyncpg.Connection,
    fetcher: fetch.Fetcher,
    asked: _Asked,
    planned: spend.ProviderPlan,
    pass_index: int,
    *,
    billed_before: bool = False,
) -> _Run:
    """One run's two-attempt loop. `billed_before` decides whether a breaker pause parks or fails."""
    run = _Run()
    pass_id = consensus.pass_id_for(planned.provider, pass_index)
    where = {"provider": planned.provider, "model": planned.model, "pass_index": pass_index}
    message = asked.user
    for attempt in ATTEMPTS:
        price = spend.attempt_price(planned)
        call = await _write_ahead(conn, asked, planned, pass_index, attempt, message, price)
        run.calls += 1
        try:
            result = await client.complete(
                fetcher, provider=planned.provider, key=planned.key, model=planned.model,
                system=asked.system, user=message, schema=contract.EXTRACTION_SCHEMA,
            )
        except asyncio.CancelledError:
            # Record the cancellation in the row before it propagates; the ceiling stays. Suppressed so a
            # broken connection cannot replace the cancellation.
            with contextlib.suppress(Exception):
                await asyncio.shield(spend.settle_call(conn, call, error=_CANCELLED))
            raise
        except client.LLMError as exc:
            said = _redacted(str(exc), planned.key)
            run.usd += await _settle_failure(conn, asked, planned, pass_index, attempt, call, exc,
                                             said, price)
            run.detail = {**where, "attempt": attempt, "http_status": exc.status}
            if exc.account:
                run.status = ACCOUNT
                run.reason = (
                    f"{planned.provider} refused this household's account for pass {pass_index}"
                    f" (attempt {attempt}): {said}. That is a balance, a spend limit or a quota and"
                    " not this title, so nothing is billed while it waits: this title is asked"
                    " again daily and resumes once the provider accepts the account again"
                    " (decision 336)"
                )
            elif exc.model_refused:
                # A setting to correct (retired or unavailable model), so park rather than fail for good.
                run.status = PLAN
                run.reason = (
                    f"{planned.provider} does not serve model {planned.model!r} to this key (attempt"
                    f" {attempt}): {said}. That is the model setting and not this title, so nothing"
                    f" more is sent: choose another {planned.provider} model in Admin, and this title"
                    " resumes here (decision 431's 404, parked as a setting)"
                )
            elif exc.paused_for is not None and not billed_before and run.usd == 0:
                # Nothing in this extraction was billed, so park out the pause. After a billed attempt,
                # the curve's
                # four walks are what bounds re-buying, so it stays on the curve.
                run.status = PAUSED
                run.detail["paused_for_s"] = exc.paused_for
                run.reason = (
                    f"{planned.provider} was not asked for pass {pass_index} (attempt {attempt}):"
                    f" {said}. Nothing was sent and nothing billed, so this title waits out the pause"
                    " with no attempt spent and is asked again once it ends (decision 336)"
                )
            elif exc.retryable:
                run.status = TRANSIENT
                run.reason = (
                    f"{planned.provider} did not answer the extraction for pass {pass_index}"
                    f" (attempt {attempt}): {said}. It is asked again on the queue's schedule, each"
                    " time behind the spend cap (decisions 325, 431)"
                )
            else:
                run.status = REFUSED
                run.reason = (
                    f"{planned.provider} refused the extraction for pass {pass_index} (attempt"
                    f" {attempt}): {said}. Asking again cannot change that answer, so nothing is"
                    " written and only an admin retry runs this title again (decision 431)"
                )
            return run

        cost = pricing.usd(result.tokens_in, result.tokens_out, price,
                           cache_written=result.cache_written, cache_read=result.cache_read,
                           rate=result.rate)
        await spend.settle_call(conn, call, error=None, tokens_in=result.tokens_in,
                                tokens_out_billed=result.tokens_out, usd=cost)
        run.usd += cost
        document = await _store(conn, asked, planned, pass_index, attempt, result)
        await spend.settle_call(conn, call, error=None, response_document_id=document)

        verdict = await verify.verify_payload(
            contract.as_verifier_payload(asked.title_id, result.payload), pass_id=pass_id,
            voc=asked.voc, packs={asked.title_id: asked.pack}, ledger=conn,
            allowed=[asked.title_id],
        )
        await verify.record_rejects(conn, verdict.rejects, run_id=asked.run_id,
                                    provider=planned.provider)
        if not verdict.rejects:
            run.tags = verdict.tags.get(asked.title_id, [])
            return run
        if attempt == ATTEMPTS[0]:
            named = contract.violation_prompt(verdict.rejects, version=asked.voc.version)
            message = f"{asked.user}\n\n{named}"
            continue
        rules = _counted(verdict.rejects)
        broken = ", ".join(f"{rule} x{n}" for rule, n in rules.items())
        run.status = VIOLATED
        run.detail = {**where, "attempt": attempt, "rules": rules}
        run.reason = (
            f"the {planned.provider} answer for pass {pass_index} broke the extraction contract"
            f" again after the retry that named it ({broken})."
            " Section 9 retries once, so nothing is written for this title and only an admin retry"
            " runs it again (decision 431); the refusals are in the DNA reject review (decision 341)"
        )
        return run
    raise AssertionError("unreachable: the second attempt always returns")


async def _write_ahead(
    conn: asyncpg.Connection,
    asked: _Asked,
    planned: spend.ProviderPlan,
    pass_index: int,
    attempt: int,
    message: str,
    price: pricing.ModelPrice,
) -> int:
    """The attempt's `llm_call` row, written before its POST at the most it can bill (decision 436 (2)).
    Returns the row's id, which the settle updates.
    """
    tokens_in = client.ceiling_input(planned.provider, asked.system, message,
                                     contract.EXTRACTION_SCHEMA)
    tokens_out = client.MAX_OUTPUT_TOKENS
    rate = client.ceiling_rate(planned.provider)
    return await spend.record_call(
        conn, provider=planned.provider, model=planned.model, title_id=asked.title_id,
        pass_index=pass_index, attempt=attempt, tokens_in=tokens_in, tokens_out_billed=tokens_out,
        usd=pricing.ceiling(tokens_in, tokens_out, price, rate=rate), ok=False,
        error=(f"{spend.UNSETTLED_PREFIX}: written before the call was sent, at its ceiling of"
               f" {tokens_in} tokens in and {tokens_out} out, and held there until an answer settles"
               " it; a row that still reads this after its walk ended is a call whose answer never"
               " arrived (decision 436)"),
        pack_document_id=asked.pack_document_id, response_document_id=None,
    )


# What a cancelled attempt's row says: the ceiling kept, the cause named.
_CANCELLED = (
    f"{spend.UNSETTLED_PREFIX}: cancelled while the call was out -- the drain's budget or a shutdown"
    " ended the walk before an answer arrived -- so it stays at the most it could have billed"
    " (decision 436)"
)


async def _settle_failure(
    conn: asyncpg.Connection,
    asked: _Asked,
    planned: spend.ProviderPlan,
    pass_index: int,
    attempt: int,
    call: int,
    exc: client.LLMError,
    said: str,
    price: pricing.ModelPrice,
) -> Decimal:
    """Settle an attempt that met an `LLMError` to what the error says was billed, and return it."""
    answer = exc.answer
    if answer is not None:
        cost = pricing.usd(answer.tokens_in, answer.tokens_out, price,
                           cache_written=answer.cache_written, cache_read=answer.cache_read,
                           rate=answer.rate)
        await spend.settle_call(conn, call, error=said, tokens_in=answer.tokens_in,
                                tokens_out_billed=answer.tokens_out, usd=cost)
        if answer.content and 200 <= answer.http_status < 300:
            document = await _store(conn, asked, planned, pass_index, attempt, answer)
            await spend.settle_call(conn, call, error=said, response_document_id=document)
        return cost
    if exc.unbilled:
        await spend.settle_call(conn, call, error=said, tokens_in=0, tokens_out_billed=0, usd=_ZERO)
        return _ZERO
    await spend.settle_call(
        conn, call,
        error=(f"{spend.UNSETTLED_PREFIX}: {said}. No answer arrived to settle it, so it stays at"
               " the most it could have billed (decision 436)"),
    )
    return await conn.fetchval("SELECT usd FROM llm_call WHERE id = $1", call)


async def _store(
    conn: asyncpg.Connection,
    asked: _Asked,
    planned: spend.ProviderPlan,
    pass_index: int,
    attempt: int,
    answer: client.LLMResult,
) -> int:
    """A provider's answer in the raw store, filed under the task's key and the url it was asked at.
    The key is removed from the bytes first: a proxy echo could carry it.
    """
    content = answer.content
    for spelling in {planned.key, planned.key.strip()} - {""}:
        content = content.replace(spelling.encode("utf-8", "replace"), b"[redacted]")
    return await rawstore.store(
        conn, source=f"{SOURCE_PREFIX}{planned.provider}", kind=KIND,
        url=answer.request_url, content=content, entity_key=asked.task_key,
        http_status=answer.http_status,
        request_meta={"model": planned.model, "pass_index": pass_index, "attempt": attempt,
                      "prompt_sha": asked.prompt_sha},
        run_id=asked.run_id,
    )


def _counted(rejects: list[verify.Rejection]) -> dict[str, int]:
    """How many of each rule the final answer broke, in `verify.REASONS` order, for the board."""
    counts = {reason: 0 for reason in verify.REASONS}
    for reject in rejects:
        counts[reject.reason] = counts.get(reject.reason, 0) + 1
    return {reason: n for reason, n in counts.items() if n}


def _redacted(text: str, key: str) -> str:
    """The key taken out of a provider's words before they are written, in every spelling."""
    return client._redacted(text, key)


__all__ = [
    "ACCOUNT",
    "KIND",
    "NO_PACK",
    "NO_VOCABULARY",
    "PAUSED",
    "PLAN",
    "REFUSED",
    "SOURCE_PREFIX",
    "TRANSIENT",
    "VIOLATED",
    "WRITTEN",
    "Extraction",
    "extract_title",
]
