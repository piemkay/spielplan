"""Stage 6 for one title: call, store, meter, verify, retry once, merge, write. Spec v2.1 §8, §9.

Decisions 324, 325, 337, 341, 382, 430, 431 and 432.

§9 is this module's whole brief in two sentences: "The schema is a cost-saving device, not the
guarantee - the guarantee is the validator (ported principle). Two-attempt pattern: retry once with
the specific contract violation named." Decision 432 puts the verdict inside stage 6, because the
retry has to be sent before the stage ends and a verdict reached one stage later could not name a
violation to a call that had already returned. So this is the loop that asks, and it decides
nothing about whether an answer is true: every such decision below is somebody else's, called.

THREE SEAMS, CALLED AND NOT REBUILT.

  * EVERY PROVIDER POST IS `client.complete` THROUGH THE DRAIN'S ONE FETCHER (decision 373), so the
    per-host pacing, the 429 rule, the backoff and the breaker are `acquire/fetch.py`'s and the
    three hosts' numbers are `acquire/hosts.py`'s. The fetcher arrives as an argument; nothing here
    builds one, because a second Fetcher in a drain is a second set of token buckets.
  * THE VERDICT IS M5.4's `verify_payload`, asked of every answer, and nothing here folds a quote,
    resolves a term or reads a salience domain. `contract.as_verifier_payload` puts the answer in
    the validator's shape under the title THIS module asked about, and `contract.violation_prompt`
    words the retry from the validator's own refusals.
  * THE METER IS `spend.record_call` and `spend.settle_call`, one `llm_call` row per attempt,
    written ahead of the call at its ceiling and settled to `pricing.usd` of what the provider
    reported as billed (plan C3: attempt 2 is a paid call and the meter must see it; decision 436).

ANY REFUSAL IS A CONTRACT VIOLATION. A `PassResult` carrying one rejection is an answer that
claimed something the vocabulary, the pack or the declared domain does not support, and the
coverage row's clause is exact about what follows: "rejected by the validator and retried exactly
once with the specific violation named in the retry prompt; a second violation fails the stage and
writes nothing". Keeping the tags that did verify out of a second violating answer would be a
third outcome nobody named, and it would write the tier from a provider that has now ignored a
named rule twice. So attempt 2 is attempt 1's user prompt with `violation_prompt` appended -- the
corpus's own composition (`mdc/sources/llm.py:87-88`) -- and a second refusal is `VIOLATED`, which
decision 431 makes permanent: an automatic re-run would bill two more calls against a title whose
provider has twice answered in breach, and "retried exactly once" would be true per walk and false
per title.

EVERY ATTEMPT IS METERED BEFORE IT IS SENT, AND EVERY ANSWER KEPT BEFORE IT IS JUDGED (decision
436). The corpus's header states the rule: "a payload that cost money is never discarded silently"
(`mdc/sources/llm.py:17-19`). So each attempt's `llm_call` row is written BEFORE the POST, at its
ceiling -- the input as `client.ceiling_input` bounds it plus `client.MAX_OUTPUT_TOKENS`, at the
attempt's own day's price and the dearest rate the request can be billed at (review cycle 2,
M55-CAP-C2-01) -- and committed, so a concurrent cap check sees the call on the wire and
a cancellation, a crash or a raise anywhere after it leaves the ceiling standing in the month rather
than nothing. Then the attempt is settled: to the usage the provider reported, then the bytes go to
the raw store (§8: "All fetched bytes land in the app's own raw store"), filed under the task's key
as `rawstore.store` requires and under `request_url`, which carries no key, and the row cites them;
and only then is `verify_payload` asked, whose ledger reads could raise. The tokens are settled
BEFORE the store, because a store that raises must not take the bill with it.

THIS MODULE USED TO WRITE THE ROW AFTER THE ANSWER, AND AFTER THE STORE, and every path around that
lost money the cap never saw: a cancellation mid-call -- the drain's ordinary end, `worker.py`
bounding it at 420 s while one call may take `client.TIMEOUT_S`'s 300 -- left no row and the re-walk
bought attempt 1 again; a full or unwritable raw volume raised between the answer and the meter; and
a title deleted while its call was out made the late INSERT's foreign key refuse it, where the row
written ahead is one 0028's `ON DELETE SET NULL` can keep (decision 430). [M5.5 review cycle 1,
M55-METER-03, M55-BUDGET-03, M55-BUDGET-04, M55-SPEND-02, M55-METER-08, NBR-01]

Every attempt's refusals are recorded as they are reached (decision 341: "a refusal nobody stored
is a refusal nobody can review"), attempt 1's included -- they are what the retry named, and the
reviewer of a title that recovered on attempt 2 still needs to see what its provider first invented.

`llm_call.ok` IS THE CALL'S OUTCOME, NOT THE CONTRACT'S. 0028 pairs it with `error` as "the
attempt's own outcome": an attempt the provider answered is `ok` whatever the validator then said
of it, because the verdict has its own table and a meter row that also carried it would be a second
answer to a question `dna_reject` owns. An attempt that met an `LLMError` is a row with `ok` false
and the provider's reason, SETTLED TO WHAT THE ERROR SAYS WAS BILLED (client named change 10): to
the usage its 200 envelope reported -- a cut-off, a refusal, a blocked prompt, prose where a tool
was forced, truncated JSON -- with that envelope stored and cited; to zero when the provider's own
status, or a request that never left, says no work was done; and otherwise left at its ceiling,
its error opening `spend.UNSETTLED_PREFIX`. This module used to meter every such attempt at zero,
with the adapters dropping the usage block the envelope carried, and a retryable cut-off re-run on
the queue's curve billed the cap in full on every walk behind a cap that read $0. The deferral
written here said the loss was bounded per attempt; it was not bounded per month, because the cap
could not see the attempts. [M5.5 review cycle 1, M55-METER-01, M55-BUDGET-02, M55-SPEND-01]

ONE FAILED RUN ENDS THE EXTRACTION AND WRITES NOTHING. A run is one provider at one pass (decision
337), and the merge's confidence is the share of runs that found a term: merging the runs that
succeeded would weigh every tag against a total the plan never finished, which is the figure
`consensus.merge_passes` refuses to lift by leaving an empty run out. So no row is written unless
every planned run was accepted -- `SELECT count(*) FROM dna_tag` for the title is unchanged by a
failure, which is the exit criterion's third measure -- and the runs after the failed one are not
called at all: their answers could not be written, and a paid call whose answer cannot be used is
the waste the cap exists to prevent. The calls run one after another, never at once, because every
attempt writes through the one connection the stage was handed and asyncpg runs one statement at a
time on it.

A RUN IS KEYED HERE, by `consensus.pass_id_for(provider, pass_index)`, and never by
`PassResult.pass_id`, which a `pass` field in a payload overrides (`dna/verify.py:587`): the key
names the provider a row is written under, and the model does not get to say who it was.

THE WRITE IS ONE TRANSACTION WITH THE LEDGER. `consensus.store_title` replaces the title's
extracted tier for the active version, and its docstring hands the curation ledger to its caller:
`derive/ledgers.apply_adjudications` "rules over the rows a title carries whatever wrote them", so
it runs inside the same transaction, after the write. Without it every fresh extraction would put
back a term the household had ruled off this title while the vocabulary still carries it -- which
`verify_payload`'s ledger read does not catch, since it asks the ledger only about terms the
vocabulary does not carry -- and a curated verdict reverted by the next pipeline run is §14 risk
5's shape.

NO KEY LEAVES THE PLAN. `spend.ProviderPlan.key` is `repr=False` and is handed to
`client.complete` and nowhere else: `request_meta` records the model, the pass, the attempt and
the prompt's sha; the url stored is `request_url`; and a provider's error text is redacted again at
the write, because it lands in `llm_call.error` and in the reason §6.6's board shows, and a
defence held only at the raise sites is a property of the adapters that exist today. Nothing here
logs.

PLAIN VALUES, NOT VERBS. `extract_title` answers with an `Extraction`, and `acquire/stages.
dna_extract` turns it into advance, park or fail. This module imports nothing from the driver or
the stage machine, and its vocabulary for a provider failure is `LLMError.retryable`, which is
decision 431's whole test, and three exceptions to it that are settings and not the title: a refusal
of the household's account -- a balance run out, a spend limit or a quota reached -- is `ACCOUNT`,
which waits, where decision 431 made every provider 4xx final; a 404 for the model is `PLAN`, parked
like the plan's own refusals; and a breaker pause met before anything was billed is `PAUSED`, which
waits out the pause (review cycle 2, DBL-C2-05, C2-PAID-02). The stage machine must not come to
decide a verb on a transport exception's type (decision 373), and this module must not decide one
at all.

PORT VERDICT: **ported with named changes** from `mdc/sources/llm.py:53-136` (`extract_aspects`).
Taken from it: the two-attempt shape and its comment, verbatim ("Attempt 1 is the ask; attempt 2
names what attempt 1 got wrong."); the retry composed as the original user prompt plus the named
violation; a non-retryable `LLMError` recorded and then final, a retryable one handed back to the
queue's backoff; and a second invalid answer recorded and final. What changed:

  1. **The validator is M5.4's `verify_payload`**, where the corpus called its aspect contract's
     `validate`, and its refusals are recorded on every attempt (decision 341) where the corpus
     counted them (`ctx.bump("aspects:invalid")`).
  2. **Every attempt is its own meter row and its own stored response.** The corpus summed both
     attempts' tokens into the one result or failure row it wrote at the end; here attempt 2 is a
     paid call the meter sees on its own (plan C3), and every answer is in the raw store before it
     is judged.
  3. **It answers with an `Extraction` instead of raising `Permanent` or re-raising**, for the
     reason the paragraph above gives; `status` carries what `Permanent` and the re-raise said.
  4. **It runs a plan**: every provider at every pass (decisions 324, 337), each run its own
     two-attempt loop, merged into per-provider rows and written with the ledger applied.
  5. **Not ported:** the title-exists check, since a deleted title takes its `dna_pack` row with
     it and reads here as no pack; `is_extractable`, which is stage 4's reviews gate; and the
     "never pay twice" read of the raw store, because stage 6 runs once per walk and advances on
     a written tier, and an answer re-used after its pack was rebuilt would be verified against
     text it was never given (decision 382).
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
# The provider refused the household's ACCOUNT, not this title: it lifts when the period rolls over
# or the balance is topped up, so the title waits (decision 336) rather than failing for good as
# decision 431 had every provider 4xx do. See `client._account_refusal`. [M55-BUDGET-07]
ACCOUNT = "account"
# The fetcher's breaker refused to send, before anything in this extraction was billed: the title
# waits out the pause (decision 336) rather than spending a queue attempt on a request that never
# left. `detail["paused_for_s"]` is what was left of the pause. See `_run`. [M5.5 review cycle 2,
# C2-PAID-02]
PAUSED = "paused"

# The raw store's filing for a paid answer. `llm:<provider>` is the corpus's own source name for
# the hosted-API worker (`mdc/sources/llm.py:53`, `source="llm"`) with the provider added, because
# one install can hold three providers' answers to one title and §6.6's board tells them apart.
SOURCE_PREFIX = "llm:"
KIND = "dna:extract"

# Attempt 1 is the ask; attempt 2 names what attempt 1 got wrong.
ATTEMPTS = (1, 2)

_ZERO = Decimal("0.000000")


@dataclass(frozen=True)
class Extraction:
    """What one title's extraction came to, in plain values.

    `reason` is written for §6.6's board and `detail` for the board row's jsonb, so the detail holds
    JSON values only -- money as a string of the digits the meter summed. `n_tags` is the `dna_tag`
    rows the merge wrote, before the curation ledger ruled over them (`detail["adjudications"]`
    counts what it changed); `calls` is the provider calls made, which is the `llm_call` rows this
    extraction added, since every call is metered whatever it came to.
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
    """What every attempt of every run shares: the title, the pack, the prompt and the verdict's
    inputs, read once before the first call."""

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

    In this order, and nothing is called until every read that could refuse has been made: the plan
    (decisions 324, 343), the active vocabulary, the stored pack (decision 432), then each run in
    plan order -- providers as the plan lists them, passes from 1 -- and the write. `task_key` is
    the acquisition task's key, which is the only spelling `rawstore.store` accepts for a per-title
    document; `run_id` is the job run the stored responses and the refusals are filed under.

    `fetcher` is the drain's one Fetcher, or `open_fetcher` the drain's supply of it, asked only once
    every read above has passed and a request is next: a title that parks on no pack or no vocabulary
    -- the state decision 432 recorded every title reaching this stage in until stage 5 was wired
    (decision 461) -- builds no client, and a factory that raises cannot turn that park into a
    failure. Nothing here builds a Fetcher either way (decision 373). [M5.5 review cycle 2, NBR-C2-01]

    The spend cap is not asked here. `acquire/pipeline.refuse_uncapped_spend` asks
    `spend.cap_check` before the driver reaches this stage and parks the title when the month
    cannot hold both attempts of every run (decision 325), so a second reading here would be a
    second answer to one question, taken a moment later.

    `batch` is the plan a flywheel launch put on the task - its providers and passes - and it is
    handed to the plan maker exactly as the gate handed it to `cap_check`, so the runs made here
    are the runs the gate reserved for (decision 442). None is the stored settings (decision 324).
    """
    plan = await spend.extraction_plan(conn, batch=batch)
    if isinstance(plan, spend.Refusal):
        return Extraction(PLAN, plan.reason, dict(plan.detail))

    version = await active_version(conn)
    voc = None if version is None else await verify.load_vocabulary(conn, version)
    prompt_voc = None if version is None else await contract.load_prompt_vocabulary(conn, version)
    if voc is None or prompt_voc is None:
        # `verify.load_vocabulary`'s own rule: an install with nothing to check against declines to
        # check, rather than refusing every tag as `unknown_term` -- which would bill two calls and
        # read as an extractor collapsing, when nothing was wrong with the answer.
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
        # The row was read before the text and the pack rebuilt in between, so the document the
        # meter would cite is not the text the prompt carries. Decision 430 makes that citation the
        # pack a call read; a raise is the driver's failure, retried on the queue's curve, and the
        # next attempt reads one pack twice.
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
    """One run's two-attempt loop: accepted tags, or the status the run ended on. `billed_before` is
    whether an earlier run of this extraction was billed, which decides what a breaker pause is."""
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
            # THE CANCELLATION IS WRITTEN INTO THE ROW BEFORE IT PROPAGATES. It is the drain's
            # ordinary end (`worker.py`'s 420 s budget against a 300 s call), and the write-ahead
            # sentence it left standing was a claim about the present that stopped being true when the
            # walk did. The ceiling stays (decision 436 (3)); only the words change. Shielded, the way
            # the drain's own cancel arm awaits `queue.release` inside its handler, and suppressed, so a
            # connection the cancellation broke cannot replace the cancellation with its own error. A
            # crash still leaves the write-ahead sentence, which says as much.
            # [M5.5 review cycle 2, M55-CAP-C2-05, M55-C2-METER-04]
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
                # A SETTING TO CORRECT, NOT A TITLE THAT FAILED: M55-DBL-04's reasoning for a model
                # that refuses forced tool use, applied to one the provider has retired or never gave
                # this key. Decision 431 made the 404 final, so every title that reached this stage
                # failed for good one by one; it parks like the plan's own refusals, naming the model.
                # [M5.5 review cycle 2, DBL-C2-05]
                run.status = PLAN
                run.reason = (
                    f"{planned.provider} does not serve model {planned.model!r} to this key (attempt"
                    f" {attempt}): {said}. That is the model setting and not this title, so nothing"
                    f" more is sent: choose another {planned.provider} model in Admin, and this title"
                    " resumes here (decision 431's 404, parked as a setting)"
                )
            elif exc.paused_for is not None and not billed_before and run.usd == 0:
                # THE BREAKER REFUSED TO SEND, AND NOTHING IN THIS EXTRACTION WAS BILLED: nothing was
                # attempted, so no queue attempt is spent on it. Eight account refusals open a
                # provider host's breaker -- an exhausted balance answers each title with a 429 the
                # fetcher sends four times -- and every later title in the drain met the pause as a
                # transient failure, spent an attempt, and closed as exhausted after four: the harm
                # M55-BUDGET-07's park exists to prevent, arriving through the breaker. It waits out
                # the pause instead (decision 336). A pause met AFTER an attempt of this extraction
                # was billed keeps the queue's curve, whose four walks are what bounds how often a
                # task can re-buy what it already paid for (decision 325's per-task bound, one per key
                # of the title); a park that refunds the attempt would bound nothing. [M5.5 review
                # cycle 2, C2-PAID-02, C2-PAID-03]
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
    """The attempt's `llm_call` row, written before its POST at the most it can bill: the input as
    `client.ceiling_input` bounds it -- the prompt and the schema with the adapter's margin, which on
    Claude is the tool prompt and the newer tokenizer the estimate alone missed -- and every token
    `max_tokens` allows, at the dearest rate the request can be billed at (decision 436 (2)). Returns
    the row's id, which the settle updates. See the module docstring.

    THE SENTENCE HAS TO STAY TRUE AFTER THE WALK ENDS. It said "the call is in flight", which a crash
    or a cancellation left standing for good, so a row weeks old claimed a live call. It now says when
    the figure was written and what settles it, which is as true of a call on the wire as of one
    nobody heard back from. [M5.5 review cycle 2, M55-CAP-C2-01, M55-C2-METER-04]"""
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


# What a cancelled attempt's row says: the ceiling kept, the cause named (see `_run`).
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
    """Settle an attempt that met an `LLMError` to what the error says was billed, and return it:
    the envelope's reported usage, with the envelope stored and cited; zero; or the ceiling left
    standing when no answer arrived. See the module docstring on `ok`."""
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
    The key is taken out of the bytes first: a 200 from a proxy that echoed the request would carry
    it, and the raw store is a place §9 says a credential never reaches. [KEYS-C1-02]"""
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
    """The key taken out of a provider's words before they are written, in every spelling
    `client._redacted` knows -- a key with whitespace around it, and the reprs an exception quotes it
    in. See the module docstring. [M5.5 review cycle 1, KEYS-C1-01]"""
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
