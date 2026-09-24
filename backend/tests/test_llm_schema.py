"""What `0028_llm_spend.sql` refuses, and what it keeps when its neighbours go. Spec v2.1 §8, §9
and §6.6's spend guard; decisions 325, 337, 343 and 430.

`test_migrations.py` checks that every migration applies; this file checks what `llm_call`
refuses and what it survives, for the reason `test_acquire_schema.py` states at its own head: a
CHECK constraint that is never tried is a comment with punctuation.

The table is the meter's only input. Decision 325 makes the cap a SUM over these rows for one
calendar month rather than a counter anybody increments, which moves every property a cap needs
onto the rows themselves: a row that should not exist, a row that vanishes, and a row whose figure
is not the one billed each hand the check a wrong number, and §8's "paid stages (6) never
auto-retry past the spend cap" is then kept against a total that is not the spend. So each refusal
below is asserted by the NAME of the constraint that refuses it - a write refused by the wrong
constraint is a constraint missing - and the two survivals are asserted as sums, which is what
the cap will read.

Raw documents are written by direct INSERT rather than through `acquire/rawstore.py`: the columns
under test are plain references, and the store's files would need a DATA_DIR this file has no use
for.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

# A Gemini extraction at the introductory gemini-3.7-flash price the corpus ships
# (mdc/config.py:157-159, $0.75 in / $3.75 out per million; decision 343): 12,000 prompt tokens and
# 1,600 candidate + 2,300 thought tokens billed as output - the exit criterion's own figures. The
# cost is exact at six places, which is the precision the column promises.
_PROMPT_TOKENS = 12_000
_BILLED_OUT = 1_600 + 2_300
_GEMINI_USD = Decimal("0.023625")  # 12000 * 0.75e-6 + 3900 * 3.75e-6


async def _title(db, title_id: int) -> int:
    await db.execute(
        "INSERT INTO title (id, kind, name) VALUES ($1, 'movie', 'x') ON CONFLICT DO NOTHING",
        title_id,
    )
    return title_id


async def _document(db, url: str, source: str = "pack", kind: str = "dna") -> int:
    return await db.fetchval(
        "INSERT INTO raw_document (source, kind, url, content_sha256, content_path) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        source, kind, url, "ab" * 32, f"ab/{url.replace(':', '_')}",
    )


async def _call(db, **overrides) -> int:
    """One `llm_call` row with every NOT NULL column filled, and any of them overridden.

    Columns are named rather than positional so that each test states only the value it is about,
    and a column the migration drops or renames fails here loudly rather than shifting the rest.
    """
    if "pack_document_id" not in overrides:
        overrides["pack_document_id"] = await _document(db, "pack:title:700")
    row = {
        "provider": "gemini",
        "model": "gemini-3.7-flash",
        "task": "extraction",
        "title_id": None,
        "pass_index": 1,
        "attempt": 1,
        "tokens_in": _PROMPT_TOKENS,
        "tokens_out_billed": _BILLED_OUT,
        "usd": _GEMINI_USD,
        "ok": True,
        "error": None,
        **overrides,
    }
    columns = ", ".join(row)
    params = ", ".join(f"${n}" for n in range(1, len(row) + 1))
    return await db.fetchval(
        f"INSERT INTO llm_call ({columns}) VALUES ({params}) RETURNING id", *row.values()
    )


async def _refused_by(db, constraint: str, **overrides) -> None:
    with pytest.raises(asyncpg.CheckViolationError) as refused:
        await _call(db, **overrides)
    assert refused.value.constraint_name == constraint, (
        f"{overrides} was refused by {refused.value.constraint_name}, not {constraint}: the "
        "constraint this test is about did not do the refusing, which is the same as it missing"
    )


# --- §9's two-attempt pattern, and decision 337's run ---------------------------------------


async def test_a_third_attempt_is_a_write_the_schema_refuses(db):
    """§9: "Two-attempt pattern: retry once with the specific contract violation named."

    Plan C3 makes both attempts paid rows, so the meter sees the retry; the CHECK makes a third
    attempt unwritable, so a loop that forgot its bound fails at its first unmetered call rather
    than billing a third pass the reservation never counted (decision 325 reserves 2 x passes x
    providers, and 2 is this number). Zero is refused beside it: attempts are counted from one,
    and a row numbered zero is one no reservation arithmetic can place.
    """
    await _call(db, attempt=1)
    await _call(db, attempt=2)
    await _refused_by(db, "llm_call_attempt_check", attempt=3)
    await _refused_by(db, "llm_call_attempt_check", attempt=0)
    assert await db.fetchval("SELECT count(*) FROM llm_call") == 2


async def test_a_run_is_numbered_from_one(db):
    """Decision 337: a run is a (provider, pass-index) pair, pooled across providers.

    The pass index is half of what a run IS, so it is NOT NULL, and it counts from one because
    `passes` does (decision 324: absent means 1) and so does decision 325's reservation. A second
    pass is an ordinary row, which is the half that proves the column is not a boolean in
    disguise.
    """
    await _call(db, pass_index=1)
    await _call(db, pass_index=2)
    await _refused_by(db, "llm_call_pass_index_check", pass_index=0)
    with pytest.raises(asyncpg.NotNullViolationError):
        await _call(db, pass_index=None)


async def test_a_task_or_a_provider_nothing_calls_is_refused(db):
    """§9 names three providers, and plan §8 adds no LLM call anywhere outside stage 6.

    `query_parsing` is refused by name because it is the plausible one: §6.6 lists it beside
    extraction as a per-task slot, but it has no caller before M6 and decision 339 - which slots
    ship - is M5.7's. A row for a task nothing calls is money the meter cannot attribute to any
    stage, and the milestone that gives the slot a caller widens the CHECK in its own migration.
    `mistral` is refused for the same reason on the other column: an adapter §9 does not name has
    no price in decision 343's table, so a row for it would be spend priced by nobody.
    """
    await _refused_by(db, "llm_call_task_check", task="query_parsing")
    await _refused_by(db, "llm_call_provider_check", provider="mistral")
    for provider in ("anthropic", "openai", "gemini"):
        await _call(db, provider=provider)
    assert await db.fetchval("SELECT count(DISTINCT provider) FROM llm_call") == 3


async def test_no_count_or_cost_can_go_negative(db):
    """A negative row is a refund the meter would subtract from the month.

    No provider bills negative tokens and none of this app's adapters computes a negative cost on
    purpose, so the only writer of one is a bug - an unsigned field read as signed, a subtraction
    the wrong way round - and decision 325's cap would then admit exactly that much more spend.
    Zero is allowed and asserted: a call that failed before it billed anything is still a row,
    for the corpus's reason that a run's failure modes are the most interesting thing about it.
    """
    await _refused_by(db, "llm_call_tokens_in_check", tokens_in=-1)
    await _refused_by(db, "llm_call_tokens_out_billed_check", tokens_out_billed=-1)
    await _refused_by(db, "llm_call_usd_check", usd=Decimal("-0.000001"))
    await _call(db, tokens_in=0, tokens_out_billed=0, usd=Decimal("0"), ok=False,
                error="transport: connection refused")


async def test_a_failed_call_names_its_failure_and_a_good_one_names_none(db):
    """`ok` and `error` are one fact written twice, so the schema holds them to each other.

    A failed call still cost money and still appears in the month's SUM, and §6.6's board is
    where an operator reads why; a failure with no reason is a charge nobody can explain, and a
    success carrying an error is a row two readers would count two ways.
    """
    await _refused_by(db, "llm_call_error_names_a_failure", ok=False, error=None)
    await _refused_by(db, "llm_call_error_names_a_failure", ok=True, error="max_tokens")
    await _call(db, ok=False, error="contract: unknown_term 'themes.mecha' is not in v1")
    await _call(db, ok=True, error=None)


# --- decision 325: the meter is a SUM, so the rows must add up ------------------------------


async def test_the_meter_keeps_billed_tokens_and_cost_exactly(db):
    """§9: "Gemini bills thinking tokens as output - counting visible JSON understates cost ~5x".

    Two figures the cap is computed from, each stored as given. `tokens_out_billed` holds the
    billed sum, candidates plus thoughts, and is read back unchanged. `usd` is exact at six places,
    and a SUM of it is exact at any row count: ten calls at ten cents are 1.000000 in numeric(12, 6)
    and 0.9999999999999999 in double precision (measured on this cluster), so a float column
    would put a cap set at a round figure one rounding error away from admitting one call more or
    one fewer, depending on how many calls the month happened to hold. A cap computed from the
    wrong number is not a cap.
    """
    await _call(db, tokens_out_billed=_BILLED_OUT, usd=_GEMINI_USD)
    row = await db.fetchrow("SELECT tokens_out_billed, usd FROM llm_call")
    assert row["tokens_out_billed"] == 3_900, (
        "the billed output came back changed; it is candidates plus thoughts as the provider "
        "reported them, and the meter reads it as given"
    )
    assert row["usd"] == Decimal("0.023625"), f"usd came back as {row['usd']!r}, not exact"

    await db.execute("DELETE FROM llm_call")
    for _ in range(10):
        await _call(db, usd=Decimal("0.1"))
    total = await db.fetchval("SELECT SUM(usd) FROM llm_call")
    assert total == Decimal("1"), (
        f"ten calls at ten cents summed to {total!r}. The meter is a SUM (decision 325) and a "
        "column whose SUM drifts with its row count moves the cap by the rounding error"
    )


async def test_a_deleted_title_leaves_its_spend_in_the_meter(db):
    """Decision 430: `title_id` is ON DELETE SET NULL, because the meter is a SUM.

    Every other extraction table cascades with its title - `dna_reject` and `dna_pack` are facts
    about one title's extraction and mean nothing once it is gone - and a reader following that
    pattern here would write CASCADE. That hands the month back its money: the calls were made and
    billed, and deleting the title they were about would lower the running total the cap is
    checked against, so a household that removes a title mid-month would be let spend its cost a
    second time. The rows stay, their title becomes NULL, and the SUM does not move; a title that
    was not deleted keeps its row exactly as it was.
    """
    gone, kept = await _title(db, 700), await _title(db, 701)
    pack = await _document(db, "pack:title:700")
    await _call(db, title_id=gone, pack_document_id=pack, attempt=1, ok=False,
                error="contract: salience 4 is outside {1,2,3}")
    await _call(db, title_id=gone, pack_document_id=pack, attempt=2, usd=Decimal("0.024000"))
    await _call(db, title_id=kept, pack_document_id=await _document(db, "pack:title:701"),
                usd=Decimal("0.010000"))
    before = await db.fetchval("SELECT SUM(usd) FROM llm_call")

    await db.execute("DELETE FROM title WHERE id = $1", gone)

    assert await db.fetchval("SELECT count(*) FROM llm_call") == 3, (
        "deleting a title deleted its calls: the month's SUM just fell by what they billed"
    )
    assert await db.fetchval("SELECT SUM(usd) FROM llm_call") == before
    assert await db.fetchval("SELECT count(*) FROM llm_call WHERE title_id IS NULL") == 2
    assert await db.fetchval("SELECT count(*) FROM llm_call WHERE title_id = $1", kept) == 1
    assert await db.fetchval("SELECT count(*) FROM raw_document WHERE id = $1", pack) == 1, (
        "the pack the calls read went with the title; decision 430 cites it as the call's evidence"
    )


# --- decision 430: the call cites the bytes it read and the bytes it got back ----------------


async def test_a_raw_document_a_call_cites_cannot_be_deleted(db):
    """Decision 430: the pack and the response are cited ON DELETE RESTRICT.

    The pack is cited by the immutable raw document and not by `dna_pack`'s row, which
    `store_pack` upserts in place on every rebuild, so what a paid call read stays readable after
    the title's pack has moved on. RESTRICT rather than SET NULL for 0027's reason: a nulled pointer
    turns "this call read that pack" into "this call read nothing". A document no call cites still
    deletes, which is the half that shows the rule is about the citation and not a freeze on the
    raw store. And a call citing no pack at all is refused, because stage 6 makes no paid call
    without one (decision 432).
    """
    pack = await _document(db, "pack:title:700")
    response = await _document(
        db, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent",
        source="gemini", kind="extraction",
    )
    loose = await _document(db, "pack:title:702")
    await _call(db, pack_document_id=pack, response_document_id=response)

    for cited in (pack, response):
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await db.execute("DELETE FROM raw_document WHERE id = $1", cited)
    await db.execute("DELETE FROM raw_document WHERE id = $1", loose)
    assert await db.fetchval("SELECT count(*) FROM raw_document") == 2

    with pytest.raises(asyncpg.NotNullViolationError):
        await _call(db, pack_document_id=None)


async def test_the_table_carries_the_billed_name_and_no_weight_or_pack_custody(db):
    """Four absences, asserted in the direction that catches their repair.

    `tokens_out` is the corpus's column name and the one a port reaches for; it is absent because
    the name is the point - a column called `tokens_out` invites the visible JSON, which §9 says
    understates the bill about fivefold. `pack_sha` is absent because decision 430 cites the
    pack's raw document instead, and decision 382 says one migration holds custody and the other
    cites it. The three weights are absent because §4.1 rule 2 makes them properties of a merged
    tag (decision 337 writes them to `dna_tag`), and a bill is not a tag. And there is no cap
    column: the cap is the `llm` row of `connector_config` (decision 325), and a second place to
    keep it would be a second answer to what the cap is.
    """
    columns = {
        row["column_name"]: row
        for row in await db.fetch(
            "SELECT column_name, data_type, numeric_scale FROM information_schema.columns "
            " WHERE table_schema = 'public' AND table_name = 'llm_call'"
        )
    }
    assert "tokens_out_billed" in columns
    assert columns["usd"]["data_type"] == "numeric" and columns["usd"]["numeric_scale"] == 6
    assert columns["at"]["data_type"] == "timestamp with time zone"
    for absent in ("tokens_out", "pack_sha", "confidence", "salience", "n_sources", "cap_usd"):
        assert absent not in columns, f"llm_call carries `{absent}`; see this test's docstring"
