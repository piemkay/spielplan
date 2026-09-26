"""What `0028_llm_spend.sql` refuses and what it keeps (§9, decision 325). The meter is a SUM over
these rows, so refusals are asserted by constraint NAME and survivals as sums. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

# A Gemini extraction at $0.75 in / $3.75 out per million: 12,000 in, 1,600 + 2,300 billed out.
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
    """Columns are named, so a dropped or renamed column fails loudly rather than shifting the rest."""
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


async def test_a_third_attempt_is_a_write_the_schema_refuses(db):
    """Decision 325 reserves 2 x passes x providers; a third attempt would bill past the reservation."""
    await _call(db, attempt=1)
    await _call(db, attempt=2)
    await _refused_by(db, "llm_call_attempt_check", attempt=3)
    await _refused_by(db, "llm_call_attempt_check", attempt=0)
    assert await db.fetchval("SELECT count(*) FROM llm_call") == 2


async def test_a_run_is_numbered_from_one(db):
    await _call(db, pass_index=1)
    await _call(db, pass_index=2)
    await _refused_by(db, "llm_call_pass_index_check", pass_index=0)
    with pytest.raises(asyncpg.NotNullViolationError):
        await _call(db, pass_index=None)


async def test_a_task_or_a_provider_nothing_calls_is_refused(db):
    """`query_parsing` has no caller before M6; a row for it would be spend the meter cannot attribute."""
    await _refused_by(db, "llm_call_task_check", task="query_parsing")
    await _refused_by(db, "llm_call_provider_check", provider="mistral")
    for provider in ("anthropic", "openai", "gemini"):
        await _call(db, provider=provider)
    assert await db.fetchval("SELECT count(DISTINCT provider) FROM llm_call") == 3


async def test_no_count_or_cost_can_go_negative(db):
    """A negative row is a refund the meter would subtract from the month; zero is allowed."""
    await _refused_by(db, "llm_call_tokens_in_check", tokens_in=-1)
    await _refused_by(db, "llm_call_tokens_out_billed_check", tokens_out_billed=-1)
    await _refused_by(db, "llm_call_usd_check", usd=Decimal("-0.000001"))
    await _call(db, tokens_in=0, tokens_out_billed=0, usd=Decimal("0"), ok=False,
                error="transport: connection refused")


async def test_a_failed_call_names_its_failure_and_a_good_one_names_none(db):
    """`ok` and `error` are one fact written twice, so the schema holds them to each other."""
    await _refused_by(db, "llm_call_error_names_a_failure", ok=False, error=None)
    await _refused_by(db, "llm_call_error_names_a_failure", ok=True, error="max_tokens")
    await _call(db, ok=False, error="contract: unknown_term 'themes.mecha' is not in v1")
    await _call(db, ok=True, error=None)


async def test_the_meter_keeps_billed_tokens_and_cost_exactly(db):
    """Ten calls at ten cents are 1.000000 in numeric(12, 6) and 0.9999999999999999 in double precision."""
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
    """Decision 430: SET NULL, not CASCADE, or deleting a title would hand the month back its money."""
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


async def test_a_raw_document_a_call_cites_cannot_be_deleted(db):
    """RESTRICT, not SET NULL: a nulled pointer turns "read that pack" into "read nothing"."""
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
    """`tokens_out` would invite the visible JSON count, which §9 says understates the bill fivefold."""
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
