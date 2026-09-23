"""§8 stage 4's reviews gate. Spec v2.1 §8 stage 4, §6.6; decisions 335, 336.

§8 stage 4 is one sentence - "pack requires plot + multi-source reviews >=50 words; if thin,
retry window 30 days (new releases accrue reviews over weeks)" - and decision 335 is what turns
it into a predicate a machine can answer. This file holds that predicate to the decision, at the
two layers the decision has:

* **The arithmetic, with no database.** `passes` and `reason` are pure, so every boundary the
  decision names can be asserted at the value rather than through a fixture: one source against
  two, forty-nine words against fifty, a plot present against absent. A boundary asserted
  through an INSERT is a boundary asserted once, because writing the fifty-first case costs a
  round trip; asserted at the value it costs a line, and this is the rule whose off-by-one parks
  a title a household could have watched.
* **The measurement, against real rows.** `measure` is the half that can be wrong about the
  SCHEMA rather than about the rule - whether `count(DISTINCT source)` counts what the decision
  means by a source, whether the word total is the one Postgres computed or one Python
  recounted, whether a title with no reviews at all yields zeros instead of no row. None of that
  is visible without the real table, so those tests take `db`.

THE `word_count` ASSERTION IS THE ONE THAT CANNOT BE FAKED, and it is why one test inserts a
body full of ragged whitespace. Decision 335 says the total is "read off the generated stored
column, so the measurement is taken BEFORE any normalisation": `review_store.review.word_count`
is a generated stored column over `btrim(body)` (`0003_content.sql:249-251`). A gate that
recounted the words in Python would agree with that column on tidy prose and disagree on exactly
the bodies a scraped page produces, which is every body this pipeline will ever fetch - so the
test asserts the count the column produced AND that the recount disagrees with it.

AND THE FIXTURE HAS TO BE ONE THE TWO REALLY DISAGREE ON, which for one review cycle it was not.
The body's raggedness was leading and trailing SPACES, which is precisely the class the
one-argument `btrim` removes - the fact this file makes load-bearing two tests down for
`BLANK_PLOT`'s newline - so the column said 4 and `body.split()` said 4, and the only recount
that disagreed was `split(" ")`, which is not a recount anybody writes. A gate patched to
`sum(len(r["body"].split()) for r in bodies)` - literally the thing the paragraph above forbids -
passed every assertion in this file. The body now opens with a newline behind its spaces, so the
column reads 5 against Python's 4 and the assertion has the teeth its docstring claimed.
[M5.3 review cycle 1, M53-335-02]

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from spielplan.derive import gate

# §4.1's partition: the app's own writes start at 1e9, and every title in this file is one §8
# acquired rather than one the bundle imported. Ids are written out rather than minted, because
# this file is about the gate and a fixture that had to walk stage 1 would fail for the driver's
# reasons. `test_acquisition_board.py` takes the same shape one package over.
THIN = 1_000_000_801
ENOUGH = 1_000_000_802
ONE_SOURCE = 1_000_000_803
NO_PLOT = 1_000_000_804
BLANK_PLOT = 1_000_000_805
RAGGED = 1_000_000_806

PLOT = "A cook, his wife, and the ramen shop they will not let close."

# A body with every shape of whitespace a scraped review page carries into the store: leading
# spaces AND A LEADING NEWLINE, a tab, a run of spaces, a paragraph break and trailing spaces.
# Five words, as Postgres counts them; four, as any Python recount counts them.
#
# THE NEWLINE AFTER THE TWO SPACES IS THE WHOLE FIXTURE and it was not here. This body used to
# open with spaces alone, which is the ONE class of whitespace the one-argument `btrim` strips -
# so the column and Python agreed on it at 4, and the only recount that disagreed was
# `split(" ")`, which nobody writes. `btrim` leaves the newline, `regexp_split_to_array` then
# yields an empty leading element, and the column reads 5 while `body.split()` reads 4: the
# divergence is now between the column and the recount a later author would ACTUALLY reach for.
# A `\n` behind leading indentation is also the commonest thing a scraped `<p>` text node hands
# this store, so the fixture got more faithful as it got sharper.
# [M5.3 review cycle 1, M53-335-02]
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


# --- the arithmetic, at the value ---------------------------------------------------------------


def test_the_gate_clears_only_when_the_plot_and_both_counts_are_there():
    """Decision 335's predicate is a conjunction of three, and all three are load-bearing."""
    assert gate.passes(gate.GateCounts(has_plot=True, sources=2, words=50))


def test_one_source_never_clears_the_gate_however_long_its_review_is():
    """"multi-source" is the word §8 uses, and decision 335 spends it as `count(DISTINCT source)
    >= 2`. A single reviewer's culture is not a spread, which is the whole reason the pack is
    built across sources (`mdc/dna/packs.py:46`: "so no single reviewer culture dominates")."""
    assert not gate.passes(gate.GateCounts(has_plot=True, sources=1, words=500))
    assert not gate.passes(gate.GateCounts(has_plot=True, sources=1, words=50))
    assert gate.passes(gate.GateCounts(has_plot=True, sources=2, words=50))


@pytest.mark.parametrize(
    ("words", "clears"),
    [(0, False), (49, False), (50, True), (51, True)],
)
def test_fifty_words_is_the_floor_and_forty_nine_is_below_it(words, clears):
    """">=50 words" is >= and not >, and the boundary is asserted from both sides.

    §8's own number, and decision 335 makes it a TOTAL across the title's reviews rather than a
    floor per review - so forty-nine is thin and fifty is not, whatever the split between the
    two sources that produced them.
    """
    assert gate.passes(gate.GateCounts(has_plot=True, sources=2, words=words)) is clears


def test_a_title_with_no_plot_never_clears_the_gate_however_many_reviews_it_has():
    """The pack is "a plot summary plus a spread of reviews" (`mdc/dna/packs.py:3`), so a title
    with no plot cannot be packed at all - the reviews are the spread around something."""
    assert not gate.passes(gate.GateCounts(has_plot=False, sources=9, words=9000))


def test_a_title_with_no_reviews_at_all_is_thin_rather_than_an_error():
    """The commonest shape of the park this gate exists to take: a film released last week."""
    assert not gate.passes(gate.GateCounts(has_plot=True, sources=0, words=0))


# --- the reason, which an operator reads --------------------------------------------------------


def test_the_reason_opens_with_the_gate_and_carries_both_counts():
    """`acquisition_job.reason` is "shown verbatim on the admin board" (`0005_ledger.sql:138`),
    so the first clause is what an operator scanning a board column actually reads, and decision
    335 requires both counts in it rather than only the one that failed."""
    said = gate.reason(gate.GateCounts(has_plot=True, sources=2, words=38))
    assert said.startswith("reviews gate: 2 sources, 38 words - retry window 30 days")


def test_the_reason_takes_its_window_from_the_constant_the_queue_is_deferred_by():
    """E3's one truth. The sentence on the board and the instant the task leases again are two
    writes of one fact, so the number in the prose is read off `REVIEW_WINDOW` and not typed."""
    assert f"retry window {gate.REVIEW_WINDOW.days} days" in gate.reason(
        gate.GateCounts(has_plot=True, sources=0, words=0)
    )


def test_the_reason_says_one_source_and_one_word_in_the_singular():
    """A board that says "1 sources" is a board a household stops reading carefully."""
    said = gate.reason(gate.GateCounts(has_plot=True, sources=1, words=1))
    assert "1 source," in said
    assert "1 sources" not in said
    assert "1 word " in said
    assert "1 words" not in said


def test_the_reason_names_the_missing_plot_and_stays_silent_when_the_plot_is_there():
    """The gate has three conditions and the park reason names the one kind of failure a count
    cannot show. A reason listing a condition that PASSED reads as a complaint about it, and an
    operator acting on "no plot" for a title that has one is an operator sent to the wrong
    source."""
    thin = gate.reason(gate.GateCounts(has_plot=False, sources=2, words=300))
    assert "no plot yet" in thin
    assert "2 sources, 300 words" in thin
    assert "plot" not in gate.reason(gate.GateCounts(has_plot=True, sources=0, words=0))


def test_every_reason_this_gate_can_produce_is_ascii():
    """CLAUDE.md's console rule: a Windows cp1252 console crashes on a decorative glyph, and this
    string reaches one through the worker's log as well as through §6.6's board."""
    for has_plot in (True, False):
        for sources, words in ((0, 0), (1, 1), (2, 49), (3, 512)):
            said = gate.reason(gate.GateCounts(has_plot=has_plot, sources=sources, words=words))
            assert said.isascii(), said


# --- the window -----------------------------------------------------------------------------


def test_the_review_window_is_thirty_days_and_the_deadline_is_tz_aware_utc():
    """§8 stage 4's own number, and decision 336's park with a deadline: a park carrying no
    instant is terminal (`stages.waiting_on_the_world`), so this function is what keeps stage 4
    a defer rather than a skip. A naive datetime is how a 30-day window becomes a 31-day or a
    29-day one on a box whose clock is not UTC, and the columns it is written into -
    `acquisition_task.run_after` and `acquisition_job.retry_after` - are both timestamptz.
    """
    assert gate.REVIEW_WINDOW.days == 30
    assert (gate.REVIEW_WINDOW.seconds, gate.REVIEW_WINDOW.microseconds) == (0, 0)

    before = datetime.now(UTC)
    deadline = gate.window_deadline()
    after = datetime.now(UTC)

    assert deadline.tzinfo is not None
    assert deadline.utcoffset() == timedelta(0)
    assert before + gate.REVIEW_WINDOW <= deadline <= after + gate.REVIEW_WINDOW


# --- the measurement, against real rows ---------------------------------------------------------


async def test_two_sources_totalling_fortynine_words_do_not_clear_the_gate(db):
    """The boundary from below, through the schema: two distinct sources, forty-nine words."""
    await _title(db, THIN, PLOT)
    await _review(db, THIN, "tmdb", _words(24))
    await _review(db, THIN, "trakt", _words(25))

    counts = await gate.measure(db, THIN)
    assert counts == gate.GateCounts(has_plot=True, sources=2, words=49)
    assert not gate.passes(counts)
    assert "2 sources, 49 words" in gate.reason(counts)


async def test_two_sources_totalling_fifty_words_clear_the_gate(db):
    """And from above. One word more than the test above, and the title advances."""
    await _title(db, ENOUGH, PLOT)
    await _review(db, ENOUGH, "tmdb", _words(25))
    await _review(db, ENOUGH, "trakt", _words(25))

    counts = await gate.measure(db, ENOUGH)
    assert counts == gate.GateCounts(has_plot=True, sources=2, words=50)
    assert gate.passes(counts)


async def test_five_hundred_words_from_one_source_do_not_clear_the_gate(db):
    """Two rows, one source: `count(DISTINCT source)` is the count decision 335 names, and a
    `count(*)` would read this title as a two-source spread it is not."""
    await _title(db, ONE_SOURCE, PLOT)
    await _review(db, ONE_SOURCE, "trakt", _words(250))
    await _review(db, ONE_SOURCE, "trakt", _words(250))

    counts = await gate.measure(db, ONE_SOURCE)
    assert counts == gate.GateCounts(has_plot=True, sources=1, words=500)
    assert not gate.passes(counts)


async def test_a_title_with_reviews_and_no_plot_does_not_clear_the_gate(db):
    """Both spellings of "no plot", because only one of them is a NULL.

    `title.overview` is nullable and is also whatever a source wrote into it, and decision 335
    says "non-empty" - so a whitespace-only overview is a title with no plot, and a gate reading
    `overview IS NOT NULL` would send a title whose plot is a space on to the pack.

    THE NEWLINE IN `BLANK_PLOT` IS THE LOAD-BEARING CHARACTER and this test caught the reason:
    the first draft of the gate asked `btrim(overview) <> ''`, copying the expression the
    generated `word_count` column uses, and the one-argument `btrim` strips SPACES only. So a
    plot of one newline - which is what a parser pulling a field out of a scraped page produces
    - passed a test written with spaces alone and reached the pack as a plot.
    """
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
    """Decision 335: "read off the generated stored column, so the measurement is taken BEFORE
    any normalisation".

    The body is the shape a scraped page produces - a leading newline behind leading spaces, a
    tab, a run of spaces, a paragraph break, trailing spaces - and it is chosen so that the column
    and Python DISAGREE on it: `btrim` strips the spaces and leaves the newline, the split on
    `\\s+` then yields an empty leading element, and 5 is the count the measurement is taken
    before. `body.split()` says 4, and so does `re.split(r"\\s+", body.strip())`.

    THE TEETH ARE IN `counts.words == stored`, and they only exist because of that disagreement:
    against a fixture the two agree on, that assertion is 4 == 4 under either implementation and
    the whole file passes over a gate that recounts in Python. The two recounts below are asserted
    by name so the next reader can see WHICH recount this fixture discriminates against - both of
    them, rather than the `split(" ")` nobody would write. [M5.3 review cycle 1, M53-335-02]
    """
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
    """The park this gate takes most often, and the one the LEFT JOIN is there for: a title with
    a plot and no review rows has to measure as (plot, 0, 0) rather than as no row at all, or
    stage 4 raises on every new release instead of parking it."""
    await _title(db, THIN, PLOT)

    counts = await gate.measure(db, THIN)
    assert counts == gate.GateCounts(has_plot=True, sources=0, words=0)
    assert not gate.passes(counts)
    assert "0 sources, 0 words" in gate.reason(counts)


async def test_measuring_a_title_that_is_not_there_refuses_rather_than_reporting_zeros(db):
    """A missing title and a title with no reviews are different facts, and zeros would say the
    second. Decision 336's `failed` is "a stage raised and will raise again", which is the honest
    state for a stage asked about a row that does not exist - and a park reason computed from
    zeros would tell an operator the title is thin when it is absent."""
    with pytest.raises(LookupError, match="1000000899"):
        await gate.measure(db, 1_000_000_899)
