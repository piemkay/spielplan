"""The `word_count` column is generated over `btrim(body)`; a Python recount disagrees on ragged
bodies, so the tests assert the column's count and that the recount differs."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from spielplan.derive import gate

# §4.1's partition: app writes start at 1e9. Ids are written out so no stage-1 walk is needed.
THIN = 1_000_000_801
ENOUGH = 1_000_000_802
ONE_SOURCE = 1_000_000_803
NO_PLOT = 1_000_000_804
BLANK_PLOT = 1_000_000_805
RAGGED = 1_000_000_806

PLOT = "A cook, his wife, and the ramen shop they will not let close."

# Five words to Postgres, four to Python: one-argument `btrim` strips spaces but not the
# newline, so the split yields an empty leading element.
RAGGED_BODY = "  \nthe\tquick   brown \n\n fox  "


def _words(n: int) -> str:
    """A body of exactly `n` words as `word_count` counts them: `n` tokens, single spaces."""
    return " ".join(f"w{i}" for i in range(n))


async def _title(conn, title_id: int, overview: str | None) -> None:
    await conn.execute(
        "INSERT INTO title (id, kind, name, origin, overview)"
        " VALUES ($1, 'movie', $2, 'acquired', $3)",
        title_id, f"title {title_id}", overview,
    )


async def _review(conn, title_id: int, source: str, body: str) -> None:
    await conn.execute(
        "INSERT INTO review_store.review (title_id, source, body) VALUES ($1, $2, $3)",
        title_id, source, body,
    )


def test_the_gate_clears_only_when_the_plot_and_both_counts_are_there():
    assert gate.passes(gate.GateCounts(has_plot=True, sources=2, words=50))


def test_one_source_never_clears_the_gate_however_long_its_review_is():
    """Decision 335: `count(DISTINCT source) >= 2`."""
    assert not gate.passes(gate.GateCounts(has_plot=True, sources=1, words=500))
    assert not gate.passes(gate.GateCounts(has_plot=True, sources=1, words=50))
    assert gate.passes(gate.GateCounts(has_plot=True, sources=2, words=50))


@pytest.mark.parametrize(
    ("words", "clears"),
    [(0, False), (49, False), (50, True), (51, True)],
)
def test_fifty_words_is_the_floor_and_forty_nine_is_below_it(words, clears):
    """A total across the title's reviews, not a per-review floor (decision 335)."""
    assert gate.passes(gate.GateCounts(has_plot=True, sources=2, words=words)) is clears


def test_a_title_with_no_plot_never_clears_the_gate_however_many_reviews_it_has():
    assert not gate.passes(gate.GateCounts(has_plot=False, sources=9, words=9000))


def test_a_title_with_no_reviews_at_all_is_thin_rather_than_an_error():
    assert not gate.passes(gate.GateCounts(has_plot=True, sources=0, words=0))


def test_the_reason_opens_with_the_gate_and_carries_both_counts():
    """`acquisition_job.reason` is shown verbatim on the admin board (`0005_ledger.sql:138`)."""
    said = gate.reason(gate.GateCounts(has_plot=True, sources=2, words=38))
    assert said.startswith("reviews gate: 2 sources, 38 words - retry window 30 days")


def test_the_reason_takes_its_window_from_the_constant_the_queue_is_deferred_by():
    """The number in the prose is read off `REVIEW_WINDOW`, the same fact the deferral uses."""
    assert f"retry window {gate.REVIEW_WINDOW.days} days" in gate.reason(
        gate.GateCounts(has_plot=True, sources=0, words=0)
    )


def test_the_reason_says_one_source_and_one_word_in_the_singular():
    said = gate.reason(gate.GateCounts(has_plot=True, sources=1, words=1))
    assert "1 source," in said
    assert "1 sources" not in said
    assert "1 word " in said
    assert "1 words" not in said


def test_the_reason_names_the_missing_plot_and_stays_silent_when_the_plot_is_there():
    """A reason naming a condition that passed sends an operator to the wrong source."""
    thin = gate.reason(gate.GateCounts(has_plot=False, sources=2, words=300))
    assert "no plot yet" in thin
    assert "2 sources, 300 words" in thin
    assert "plot" not in gate.reason(gate.GateCounts(has_plot=True, sources=0, words=0))


def test_every_reason_this_gate_can_produce_is_ascii():
    """It reaches a cp1252 console through the worker's log."""
    for has_plot in (True, False):
        for sources, words in ((0, 0), (1, 1), (2, 49), (3, 512)):
            said = gate.reason(gate.GateCounts(has_plot=has_plot, sources=sources, words=words))
            assert said.isascii(), said


def test_the_review_window_is_thirty_days_and_the_deadline_is_tz_aware_utc():
    """A park with no instant is terminal. A naive datetime drifts a day on a non-UTC box, and both
    columns are timestamptz."""
    assert gate.REVIEW_WINDOW.days == 30
    assert (gate.REVIEW_WINDOW.seconds, gate.REVIEW_WINDOW.microseconds) == (0, 0)

    before = datetime.now(UTC)
    deadline = gate.window_deadline()
    after = datetime.now(UTC)

    assert deadline.tzinfo is not None
    assert deadline.utcoffset() == timedelta(0)
    assert before + gate.REVIEW_WINDOW <= deadline <= after + gate.REVIEW_WINDOW


async def test_two_sources_totalling_fortynine_words_do_not_clear_the_gate(db):
    await _title(db, THIN, PLOT)
    await _review(db, THIN, "tmdb", _words(24))
    await _review(db, THIN, "trakt", _words(25))

    counts = await gate.measure(db, THIN)
    assert counts == gate.GateCounts(has_plot=True, sources=2, words=49)
    assert not gate.passes(counts)
    assert "2 sources, 49 words" in gate.reason(counts)


async def test_two_sources_totalling_fifty_words_clear_the_gate(db):
    await _title(db, ENOUGH, PLOT)
    await _review(db, ENOUGH, "tmdb", _words(25))
    await _review(db, ENOUGH, "trakt", _words(25))

    counts = await gate.measure(db, ENOUGH)
    assert counts == gate.GateCounts(has_plot=True, sources=2, words=50)
    assert gate.passes(counts)


async def test_five_hundred_words_from_one_source_do_not_clear_the_gate(db):
    """A `count(*)` would read two rows from one source as a spread."""
    await _title(db, ONE_SOURCE, PLOT)
    await _review(db, ONE_SOURCE, "trakt", _words(250))
    await _review(db, ONE_SOURCE, "trakt", _words(250))

    counts = await gate.measure(db, ONE_SOURCE)
    assert counts == gate.GateCounts(has_plot=True, sources=1, words=500)
    assert not gate.passes(counts)


async def test_a_title_with_reviews_and_no_plot_does_not_clear_the_gate(db):
    """Both spellings of "no plot". The newline in `BLANK_PLOT` matters: one-argument `btrim`
    strips spaces only."""
    await _title(db, NO_PLOT, None)
    await _review(db, NO_PLOT, "tmdb", _words(400))
    await _review(db, NO_PLOT, "trakt", _words(400))
    await _title(db, BLANK_PLOT, "   \n ")
    await _review(db, BLANK_PLOT, "tmdb", _words(400))
    await _review(db, BLANK_PLOT, "trakt", _words(400))

    for title_id in (NO_PLOT, BLANK_PLOT):
        counts = await gate.measure(db, title_id)
        assert counts == gate.GateCounts(has_plot=False, sources=2, words=800)
        assert not gate.passes(counts)


async def test_the_word_total_is_the_one_postgres_generated_and_not_a_python_recount(db):
    """The column counts 5 where `body.split()` and `re.split` both count 4; that disagreement is
    what gives `counts.words == stored` teeth."""
    await _title(db, RAGGED, PLOT)
    await _review(db, RAGGED, "rt", RAGGED_BODY)

    stored = await db.fetchval(
        "SELECT word_count FROM review_store.review WHERE title_id = $1", RAGGED
    )
    assert stored == 5

    counts = await gate.measure(db, RAGGED)
    assert counts.words == stored
    assert len(RAGGED_BODY.split()) == 4, "the recount a later author would actually write"
    assert len(re.split(r"\s+", RAGGED_BODY.strip())) == 4, "and the other one"


async def test_a_title_with_no_reviews_at_all_measures_zero_rather_than_nothing(db):
    """The LEFT JOIN: no review rows must measure (plot, 0, 0), or stage 4 raises on new releases."""
    await _title(db, THIN, PLOT)

    counts = await gate.measure(db, THIN)
    assert counts == gate.GateCounts(has_plot=True, sources=0, words=0)
    assert not gate.passes(counts)
    assert "0 sources, 0 words" in gate.reason(counts)


async def test_measuring_a_title_that_is_not_there_refuses_rather_than_reporting_zeros(db):
    """Zeros would claim the title is thin when it is absent; decision 336's `failed` is honest."""
    with pytest.raises(LookupError, match="1000000899"):
        await gate.measure(db, 1_000_000_899)
