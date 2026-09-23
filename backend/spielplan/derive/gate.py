"""§8 stage 4's reviews gate: the predicate, the sentence it parks with, and the window.

Spec v2.1 §8 stage 4 - "pack requires plot + multi-source reviews >=50 words; if thin, retry
window 30 days (new releases accrue reviews over weeks)" - and §6.6's board; decisions 335, 336.

PORT VERDICT: **new code. The corpus has no gate, and the number this one shares with it belongs
to a different rule.** `mdc/dna/packs.py:48` declares `MIN_WORDS = 50` and spends it at
`:165-167` as a per-review floor inside the pack query - "below this a 'review' is a rating with
a sentence" - so the corpus drops the short reviews and builds a pack out of whatever is left,
including nothing at all. It never refuses to build, because a wholesale crawl has nothing to
refuse FOR. §8 splits that into two stages: a gate deciding whether a title is worth packing
(stage 4, this module) and the pack itself (stage 5, M5.4). Decision 335 makes this module's
fifty a TOTAL across the title's reviews rather than a floor per review, and the reading it
declines is the expensive one: ">= 50 words each" fails a title carrying one 200-word review
and one 30-word one, which is a perfectly acquirable title.

WHAT M5.4 INHERITS, SAID HERE BECAUSE NOTHING ELSE WILL SAY IT. Because the two fifties are
different rules, clearing this gate does not promise stage 5 a pack with any review in it: two
25-word reviews total fifty and pass here, and `packs.py`'s per-review floor then discards both.
That is decision 335 working as decided rather than a defect in it - this gate asks whether a
title has accrued enough review PROSE to be worth a paid stage, and the pack asks which of that
prose is a review rather than a rating - but the milestone that ports `packs.py` is the one that
has to decide what a plot-only pack means, and it should read this paragraph before concluding
the gate was measuring the wrong thing.

THE PLOT HALF IS A RESOLVED FIELD AND IS NOT RESOLVED AGAIN HERE. Decision 335 names
`title.overview`, which `importer/meta.resolve_title_fields` already fills from `plot_full` then
`plot_short` through `meta.best` and `SOURCE_PRIORITY`, and which
`test_title_meta.py::test_the_overview_falls_back_from_plot_full_to_plot_short` holds. A gate
reading the `title_meta` payloads itself would be a second implementation of that fallback, and
the two would part company the day `SOURCE_PRIORITY` moves - this gate parking a title whose
card shows a plot, which is the shape of defect nobody reports because each half looks right on
its own. One definition, and the gate reads its output.

THE SPLIT BETWEEN `measure` AND `passes`/`reason` IS FOR THE BOUNDARY, not for tidiness. The
rule is three comparisons and an off-by-one in any of them parks a title a household could have
watched tonight; the query is a claim about the schema. Asserting the comparisons through
INSERTs costs a round trip per case, so the fifty-first case never gets written - so the
arithmetic is pure and asserted at the value, and the query is asserted against real rows for
the things only real rows can be wrong about.

DECISION 336 FRAMES THE OUTCOME, and the outcome is stage 4's to take rather than this module's:
a thin title is `parked` with a deadline and never `failed`. Nothing raised and a plain retry
today would produce the same answer; what changes is the world, over weeks. `REVIEW_WINDOW` is
the one place the thirty days is written, because the window is TWO writes - `queue.defer` puts
the task back at an instant, `acquisition_job.retry_after` is the board's copy of the same
instant for §6.6 to render - and two writes of one fact spelled twice is how a board promises a
date the queue will not honour.

THE WINDOW RE-ASKS THE SOURCES WHEN IT CLOSES, which is decision 421 and which this build did not
do for one review cycle. `pipeline._resume_index` answered the BOARD's stage, so a task made due
again at stage 4 re-entered at stage 4 and re-ran the query below over the rows day one wrote: the
two stages that can move these counts - stage 2, which fetches reviews, and stage 3, which writes
`review_store.review` - were behind the resume point, so §8's "new releases accrue reviews over
weeks" could not be observed by construction. Stage 4 now declares `reask_from=2` in
`pipeline.STAGES`, and a park here whose deadline has PASSED re-enters there; the same park made
due early is an operator's retry and still asks no host anything. This module is where a reader
meets it first, because `reason` is the sentence an operator reads.
[M5.3 review cycle 2, m53-c2-gate-01, M53-C2-NET-03; decision 421]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

# Decision 335's two floors. Named for what they measure rather than `MIN_WORDS`, which is the
# corpus's own constant for the per-review floor the PORT VERDICT above separates this one from:
# two rules sharing a number is survivable, two rules sharing a name in a tree that will hold
# both by M5.4 is not.
MIN_SOURCES = 2
MIN_TOTAL_WORDS = 50

# §8 stage 4's "retry window 30 days (new releases accrue reviews over weeks)", as a duration
# because the window is counted from the moment the gate found the title thin rather than from a
# release date this app does not always have.
REVIEW_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class GateCounts:
    """What the gate measured about one title. Three facts, and the reason names all three.

    Frozen for the reason `stages.Outcome` is: the stage writes a board row from this and defers
    a queue task from it, and two readers of one mutable record is how the counts on a board stop
    being the counts the task was deferred on.
    """

    has_plot: bool
    sources: int
    words: int


# Decision 335's predicate, as one statement. Three things it does deliberately:
#
#   * `count(DISTINCT source)` and not `count(*)`: §8 says "multi-source", and two reviews
#     scraped from one page are one source's opinion twice. `mdc/dna/packs.py:46` states the same
#     rule from the pack's side - "so no single reviewer culture dominates".
#   * `sum(word_count)` off the GENERATED STORED column (`0003_content.sql:249-251`), which is
#     decision 335's "before any normalisation". Postgres has already computed it over
#     `btrim(body)`; recomputing it in Python would add a second definition of a word that agrees
#     with the column on tidy prose and disagrees on the ragged bodies a scraped page produces.
#   * A LEFT JOIN, so a title with no reviews measures as zeros rather than as no row. That is
#     the commonest case this gate sees - a film released last week - and a query returning
#     nothing for it would make the park that stage 4 exists to take look like a missing title.
#
# THE PLOT TEST IS "HOLDS A NON-WHITESPACE CHARACTER" AND NOT `btrim(...) <> ''`, which is what
# it was written as first. Decision 335 says non-empty, and `IS NOT NULL` is plainly not that -
# an overview a source wrote as a space is a title with no plot. But the one-argument `btrim` is
# not that either: it strips SPACES and nothing else, so an overview of a single newline survives
# it and reads as a plot. That is the form this gate will actually meet, because the thing that
# writes `overview` is a parser pulling a field out of a scraped page. The generated `word_count`
# column btrims for a different purpose - it is counting tokens, and the split on `\s+` handles
# the rest - so copying its expression here copies a decision that was never about emptiness.
_MEASURE = r"""
    SELECT coalesce(t.overview, '') ~ '\S' AS has_plot,
           coalesce(r.sources, 0)          AS sources,
           coalesce(r.words, 0)            AS words
      FROM title t
      LEFT JOIN (SELECT title_id,
                        count(DISTINCT source) AS sources,
                        sum(word_count)        AS words
                   FROM review_store.review
                  WHERE title_id = $1
                  GROUP BY title_id) r ON r.title_id = t.id
     WHERE t.id = $1
"""


async def measure(conn: asyncpg.Connection, title_id: int) -> GateCounts:
    """What the gate can see about this title right now.

    Raises `LookupError` when there is no such title, because a missing title and a title with
    no reviews are different facts and the zeros would say the second. A park reason computed
    from them would tell an operator on §6.6's board that a title is thin when it is absent, and
    decision 336 reserves `failed` for exactly this - a stage that raised and will raise again.
    """
    row = await conn.fetchrow(_MEASURE, title_id)
    if row is None:
        raise LookupError(f"reviews gate: no title row for title_id {title_id}")
    return GateCounts(has_plot=row["has_plot"], sources=int(row["sources"]), words=int(row["words"]))


def passes(counts: GateCounts) -> bool:
    """Decision 335's conjunction: a plot, two sources, fifty words across them."""
    return counts.has_plot and counts.sources >= MIN_SOURCES and counts.words >= MIN_TOTAL_WORDS


def reason(counts: GateCounts) -> str:
    """Why this title is not being packed yet, in the register `stages.NOT_PLACED` set.

    `acquisition_job.reason` is "shown verbatim on the admin board" (`0005_ledger.sql:138`), so
    this is a product surface and not a log line. It opens with the gate's name because an
    operator scanning a board column reads the first clause; it carries BOTH counts because
    decision 335 says so and because the one that passed is what tells a reader whether the
    title is close; and it says what happens next, which is the lever: the window asks the
    sources again by itself, and a sentence that only said "retry window 30 days" would read to
    the person holding the retry button as an instruction to press it.

    THE CLAUSE IT USED TO END ON WAS FALSE TWICE, and its replacement is written to be true in
    both places. "Nothing is asked of an operator: new releases accrue reviews over weeks, and
    this title is re-asked by itself when the window closes" promised a re-ask the resume rule did
    not perform - see this module's docstring - on the one column §6.6 renders verbatim, and on an
    install with no TMDB key it also told the operator nothing was asked of them while the counts
    were zero because no source could be asked. The re-ask is real now (decision 421), and the
    sentence says what the window does rather than what an operator need not do - so a key typed
    in during the window is asked on its close, and the sentence stays true either way. The plot
    is still named only when it is MISSING: a reason listing a condition that passed reads as a
    complaint about it, and an operator acting on "no plot" for a title that has one is an
    operator sent to the wrong source.
    [M5.3 review cycle 2, m53-c2-gate-01, M53-C2-NET-03, m53-c2-gate-02; decision 421]

    ASCII, WHICH `acquire/stages.py`'S REASONS ARE NOT, and this sentence used to claim they were.
    Six of that module's park reasons carry the section sign - `UNSUPPORTED_KIND`,
    `NO_ACTIVE_BUNDLE`, `NOT_PLACED`, `NO_FETCHER`, `ENRICH_REQUIRED_FAILED` and cycle 2's
    `ENRICH_REQUIRED_UNASKED` - which costs nothing there because cp1252 holds it at 0xA7 and it
    round-trips. This string is held to plain
    ASCII by `test_every_reason_this_gate_can_produce_is_ascii` because it reaches a Windows
    console through the worker's log as well as the board through Postgres. The citation is
    corrected rather than the six constants, because three of them are M5.1's and their text is
    asserted verbatim elsewhere - and a comment pointing the next author at a convention the
    sibling module does not keep answers their question about a new park reason wrongly.
    [M5.3 review cycle 1, M53-ASCII-05]
    """
    plot = "" if counts.has_plot else "no plot yet, "
    return (
        f"reviews gate: {plot}{_counted(counts.sources, 'source')}, "
        f"{_counted(counts.words, 'word')} - retry window {REVIEW_WINDOW.days} days. When it "
        "closes the sources are asked again, so reviews written in the meantime are counted "
        "without anyone retrying this job"
    )


def window_deadline() -> datetime:
    """The instant a title found thin here is asked again. `stages.waiting_on_the_world`'s shape.

    Both writes take it from this one call: `queue.defer` schedules the task on it and
    `acquisition_job.retry_after` is the board's copy of the same instant. Timezone-aware,
    because both columns are `timestamptz` and a naive value is a window whose length depends on
    where the box thinks it is.
    """
    return datetime.now(UTC) + REVIEW_WINDOW


def _counted(n: int, noun: str) -> str:
    """"1 source", "2 sources". A board that says "1 sources" is a board that gets skim-read."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
