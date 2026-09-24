"""The acquisition spine's three tables, asserted by trying to violate them. Spec v2.1 §8.

`test_migrations.py` checks that every migration applies and what shape it leaves behind; this
file checks what that shape *refuses*, for the reason `test_schema_contracts.py` states at its
own head: a CHECK constraint that is never tried is a comment with punctuation.

Two of `0024_acquisition.sql`'s load-bearing claims are absences, which is the kind of decision a
later reader repairs on sight. `acquisition_task` carries **no foreign key to `title`** because
§8 stage 1 is what mints the title (decision 322, and `connectors/resolve.py:216-222`'s refusal
to invent one), and `raw_document.content_sha256` carries **no UNIQUE** because two fetches that
return identical bytes share one file and must still leave two rows of history. Neither absence
can be asserted by reading the DDL, so each is asserted here in the direction that catches its
repair: the task outlives the title, and one hash keeps two rows.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncpg
import pytest


async def _title(db, title_id: int = 700, kind: str = "movie") -> int:
    await db.execute(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, 'x') ON CONFLICT DO NOTHING",
        title_id, kind,
    )
    return title_id


async def _index_defs(db, table: str) -> str:
    rows = await db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = $1 AND schemaname = 'public'", table
    )
    return " ".join(row["indexdef"] for row in rows)


# --- decision 322: the durable (kind, key) queue --------------------------------------


async def test_enqueueing_the_same_kind_and_key_twice_is_refused(db):
    """`mdc/queue.py:1-8`: "Task identity is (kind, key). Enqueueing the same pair twice is a
    no-op."

    The no-op will be the writer's `ON CONFLICT`; what is asserted here is the constraint
    underneath it, because a uniqueness enforced by the writer is a uniqueness that holds until
    two writers run -- and §8.4's flywheel is the second writer, enqueueing titles the nightly
    sweep is enqueueing at the same time.

    The second half is the half that says what the identity *is*: the same key under a different
    kind is a different task, so a title can be at `identify` and `enrich` in the same table
    without either collapsing into the other.
    """
    await db.execute("INSERT INTO acquisition_task (kind, key) VALUES ('identify', 'jf:a1')")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("INSERT INTO acquisition_task (kind, key) VALUES ('identify', 'jf:a1')")
    await db.execute("INSERT INTO acquisition_task (kind, key) VALUES ('enrich', 'jf:a1')")
    assert await db.fetchval("SELECT count(*) FROM acquisition_task WHERE key = 'jf:a1'") == 2


async def test_the_state_check_refuses_a_state_the_drain_cannot_read(db):
    """The queue and the board have different vocabularies, and the confusion is one word wide.

    `acquisition_job.status` is `queued|running|parked|ready|failed` (`0005_ledger.sql:136`) and
    is what §6.6's board shows; `acquisition_task.state` is the corpus's
    `pending|leased|done|failed|skipped`, which is what the drain's `WHERE state = 'pending'`
    reads. They overlap on `failed` only. A writer that reaches for the board's spelling while
    holding the queue's row has to be refused here, because the row it would write is one no
    lease query would ever see again -- work that is silently invisible rather than loudly
    wrong. [decision 322; decision 336 on what `parked` means and where it lives]
    """
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO acquisition_task (kind, key, state) VALUES ('identify', 'jf:a2', $1)",
            "queued",
        )


async def test_a_new_task_is_free_and_immediately_claimable(db):
    """Two defaults the drain depends on, asserted together because they are one statement.

    `paid` is false, so a generic drain that leases whatever is ready cannot bill the household
    on its first tick: §8's paid stage (6) arrives at M5.5 and sets the flag itself, and until
    then every task in this table is a crawl. `mdc/sources/base.py:51-53` carries the same flag
    for the same reason -- at corpus scale the difference between a free pass and a paid one is
    roughly a hundred euros a click.

    `next_attempt_at` is NOT NULL with a default of now(), where the corpus defaults to 0. Both
    mean "due immediately"; what matters is that it is never NULL, because the lease query reads
    `next_attempt_at <= now()` over `acquisition_task_ready` and a NULL there is a row that is
    enqueued, indexed, and never leased by anybody.
    """
    row = await db.fetchrow(
        "INSERT INTO acquisition_task (kind, key) VALUES ('identify', 'jf:a3') "
        "RETURNING paid, state, attempts, next_attempt_at"
    )
    assert row["paid"] is False, "a new task must be free until the stage that spends says so"
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] is not None, (
        "a NULL next_attempt_at is a task the lease query can never see; it is due now or it is "
        "deferred to a time, never to nothing"
    )


async def test_a_leased_task_cannot_be_written_without_a_lease_to_expire(db):
    """The one state that must never be half-written. §8's "killed and resumed" is this row.

    Recovery is by lease expiry and not by a shutdown handler -- "the only way that actually
    survives `kill -9`" (`mdc/queue.py:1-8`) -- so the reaper's whole mechanism is
    `lease_expires < now()`. A row marked `leased` with no expiry is work nothing can ever
    reclaim: not the worker that took it (it is gone) and not the next one (it sees a lease that
    never ends).

    The second half asserts the direction the constraint deliberately leaves open. It is stated
    as an implication and not a biconditional, so whether a finished row keeps the owner that
    finished it stays the drain's business -- provenance a later admin surface may well want.
    """
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO acquisition_task (kind, key, state) "
            "VALUES ('identify', 'jf:a4', 'leased')"
        )
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, state, lease_owner, lease_expires) "
        "VALUES ('identify', 'jf:a5', 'done', 'host:123', now())"
    )


async def test_the_queue_is_indexed_for_the_lease_and_for_the_reaper(db):
    """The two reads the drain makes every tick, per §5.3's sequential single-process loop.

    Asserted because an index is the first thing a later migration drops when it looks redundant.
    The lease FILTERS on (state, next_attempt_at) -- the corpus's own `ix_task_ready`, whose third
    column `priority` is along for the ride rather than serving the sort; the reaper reads the
    leases that have expired, which is a partial index because a row that is not leased can never
    be reclaimed.

    WHAT THIS DOES NOT PIN is the lease's ORDER BY, and `queue.lease`'s own paragraph used to say
    it did. A substring match on `pg_indexes` cannot see a sort key, so it stays green for any
    ORDER BY whatsoever; the sibling test below is what reddens when that line moves.
    [M5.1 review cycle 4, M51-C4-QUEUE-01]
    """
    defs = await _index_defs(db, "acquisition_task")
    assert "(state, next_attempt_at, priority)" in defs, (
        "the lease query has no index for its filter: claimable rows, then the ones whose time "
        "has come"
    )
    assert "(lease_expires)" in defs and "state = 'leased'" in defs, (
        "the reaper has no index; reclaiming abandoned work would scan the whole queue"
    )


def test_the_lease_orders_by_a_key_no_index_can_serve():
    """The pin `queue.lease` claims and the test above cannot give it. No database needed.

    Three sentences in this tree asserted that `acquisition_task_ready (state, next_attempt_at,
    priority)` serves `ORDER BY priority, id`. It cannot: `next_attempt_at <= now()` is a RANGE
    predicate, so a btree scan under it is ordered by `next_attempt_at` and not by `priority`, and
    `id` is in no index on this table at all. The sort is therefore a Sort node under every plan
    shape - which is the corpus's own accepted cost at 19,000 titles and is fine at `DRAIN_LIMIT`
    tasks once per half-hour tick - but the CLAIM was false, and the cross-reference that said a
    test held the ORDER BY and the index together was false about a test.

    So this is that test, and it is a source read rather than an `EXPLAIN` deliberately: a plan
    read against a unit-sized fixture measures the planner's cost model, which correctly prefers a
    sequential scan of a few hundred rows and would fail an assertion about index use for the
    right reason. What matters is that a later milestone changing this ORDER BY - M5.2's flywheel
    priorities, M5.6's admin actions - is made to read the paragraph that explains why no index
    follows it, instead of finding a green build and a comment telling them one does.
    [M5.1 review cycle 4, M51-C4-QUEUE-01]
    """
    import inspect
    import pathlib

    from spielplan.acquire import queue

    source = inspect.getsource(queue.lease)
    statement = source[source.index("UPDATE acquisition_task"):]
    assert "ORDER BY priority, id LIMIT" in statement, (
        "queue.lease's ORDER BY has moved. No index on acquisition_task can serve a sort key, so "
        "the paragraph in queue.lease arguing why the Sort is accepted has to move with it - and "
        "if an index is being added to serve the new key, 0024's checksum moves and every box "
        "that applied it must drop its database"
    )
    assert "state = $4 AND next_attempt_at <= now()" in statement, (
        "queue.lease's filter has moved away from the columns acquisition_task_ready leads with"
    )
    ddl = pathlib.Path(__file__).resolve().parents[2] / "backend/migrations/0024_acquisition.sql"
    indexes = [
        line for line in ddl.read_text(encoding="utf-8").splitlines()
        if "ON acquisition_task" in line
    ]
    assert indexes, "0024 no longer creates any index on acquisition_task"
    assert not any("(priority" in line for line in indexes), (
        "an index now leads with priority, so the lease's sort may be served after all: correct "
        "queue.lease's paragraph, which states as a fact that it is not"
    )


async def test_deleting_a_title_keeps_its_task_and_takes_its_board_row(db):
    """Decision 322, in the only place it can be observed: the two tables behave differently.

    `acquisition_job` is the per-title board and cascades from `title`
    (`0005_ledger.sql:134`); that CASCADE is pinned by
    `test_schema_contracts.py::test_a_title_carrying_only_derived_rows_still_deletes` and is not
    this file's to restate. `acquisition_task` is the queue and has no foreign key at all,
    because it is keyed on the Jellyfin item or the provider id and has to be able to hold work
    for a title that does not exist yet -- §8 stage 1's job is to mint that title, and
    `connectors/resolve.py:216-222` says the boundary in as many words: "An unresolved item is
    *reported*, never invented ... Acquiring genuinely new titles is §8's pipeline."

    So the assertion is the surviving row. A later reader who adds the foreign key that looks
    missing fails here, which is cheaper than discovering at M5.2 that nothing can be enqueued
    until the thing the enqueue exists to create already exists.
    """
    target = await _title(db, 700)
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status) VALUES ($1, 2, 'parked')", target
    )
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) "
        "VALUES ('identify', 'jf:item-700', $1)",
        '{"title_id": 700}',
    )

    await db.execute("DELETE FROM title WHERE id = $1", target)

    board = await db.fetchval("SELECT count(*) FROM acquisition_job WHERE title_id = $1", target)
    assert board == 0, "the board row cascades with its title, and that CASCADE is pinned"
    queued = await db.fetchval(
        "SELECT count(*) FROM acquisition_task WHERE key = 'jf:item-700'"
    )
    assert queued == 1, (
        "the queue must not cascade from title: a task is disposable and a title row is not "
        "(decision 162, content seeds once), and the task is what mints the title in the first "
        "place"
    )


# --- §8: the raw store's index --------------------------------------------------------


async def test_two_fetches_of_identical_bytes_share_a_file_and_keep_two_rows(db):
    """`mdc/rawstore.py:1-11`, which is the requirement §8 restates as "All fetched bytes land in
    the app's own raw store, so re-parsing is free forever" (spec:398).

    "Two fetches that return identical bytes share one file but get two rows, so the fetch
    history stays visible without duplicating bulk." A UNIQUE on `content_sha256` would collapse
    the history into the storage and make "when did we last see this" unanswerable -- which is
    the question a conditional re-fetch and §6.6's board both ask. The store's own dedupe is the
    content-addressed path, and that is asserted here too: one path, two rows.
    """
    for _ in range(2):
        await db.execute(
            "INSERT INTO raw_document (source, kind, entity_key, url, http_status, "
            "content_sha256, content_path, byte_size) "
            "VALUES ('tmdb', 'movie_detail', 'tt0111161', 'https://example.test/x', 200, "
            "'deadbeef', 'tmdb/movie_detail/de/deadbeef.gz', 12)"
        )

    rows = await db.fetch(
        "SELECT content_path FROM raw_document WHERE content_sha256 = 'deadbeef'"
    )
    assert len(rows) == 2, "a UNIQUE on the hash would have made the second fetch unrecordable"
    assert len({row["content_path"] for row in rows}) == 1, (
        "both rows must point at the one file the content address names"
    )


async def test_the_raw_store_is_indexed_for_the_derive_and_for_the_store(db):
    """The two reads the plan requires (`docs/milestones/M5.1-plan.md` §5, step A2).

    The derive reads by entity to re-parse one title without a crawl; the store reads by hash to
    find the file it already has. Both are on the hot path of the promise that a bad derive is
    cheap to fix, which is the whole reason the bytes are kept.
    """
    defs = await _index_defs(db, "raw_document")
    assert "(entity_key, source, kind)" in defs, "the per-title re-parse has no index"
    assert "(content_sha256)" in defs, "the store cannot find the file it already holds"


# --- decision 340: the fetcher's per-host memory ---------------------------------------


async def test_the_robots_cache_holds_one_row_per_host(db):
    """Per host, because that is what robots.txt is, and what a circuit breaker must be.

    One row per host is what keeps a breaker opened by a hostile host from burning the whole run
    and from leaking into another host's pacing. A second row for the same host would be a second
    answer to "may we fetch this", and §14 risk 5 is about exactly that shape.

    The second half asserts that the robots columns are nullable: a host whose robots.txt has not
    been fetched yet, or whose fetch failed, still has to be recordable -- the breaker's counters
    are the first thing written about a host that is refusing connections, and requiring a robots
    body first would make the failing case the one that cannot be written down.
    """
    await db.execute(
        "INSERT INTO fetch_host_state (host, robots_txt, robots_status, robots_fetched_at) "
        "VALUES ('example.test', 'User-agent: *', 200, now())"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("INSERT INTO fetch_host_state (host) VALUES ('example.test')")

    await db.execute(
        "INSERT INTO fetch_host_state (host, consecutive_failures, paused_until) "
        "VALUES ('slow.test', 3, now() + interval '5 minutes')"
    )
    unasked = await db.fetchval(
        "SELECT robots_txt FROM fetch_host_state WHERE host = 'slow.test'"
    )
    assert unasked is None, "a host can be paused before anyone has asked it for robots.txt"


def test_this_package_cites_a_test_by_name_and_a_column_comment_by_its_own_line():
    """CLAUDE.md makes a citation load-bearing - "work that can't cite its clause reads as
    off-convention" - so a citation that resolves to the wrong thing is a false statement in an
    argument and not a typo. Two of them shipped in this milestone, and both are the same species.

    A TEST IS CITED BY NAME. `acquire/__init__.py` pointed at `test_layering_guards.py:564` for the
    guard that stops this package importing `spielplan.api`. That was the right line at HEAD, and
    M5.1's own edits to that file - the `_COUNT_WORDS` import and the eleven-line ALLOWED_RESIDUE
    paragraph - pushed the guard to 574, where 553 is the middle of the SQL-residue helper, a
    different rule entirely. So a stage author looking for the guard that would stop them reaching
    into `api/` found a comprehension and could reasonably conclude it had been deleted. Every one
    of the fourteen other test citations in `backend/spielplan/` already uses `file.py::test_name`,
    which is the spelling a commit to the cited file cannot falsify.

    A COLUMN COMMENT IS CITED WHERE IT IS. `0005_ledger.sql:137` was quoted in eleven places -
    five in this package, five in tests and one in `spec_coverage.toml`, where an auditor reads it
    - as saying `acquisition_job.reason` is "shown verbatim on the admin board". Line 137 is
    `detail jsonb NOT NULL DEFAULT '{}'::jsonb,` and carries no comment at all; the sentence is on
    138. 0005 is applied and sha256-checksummed and has exactly one commit in its history, so the
    coordinate was wrong the day it was written rather than drifted, and `M5.3-plan.md:285` and
    `M5.6-plan.md:118` - the two plans that will be read against this code - already say 138.
    One of those eleven is interpolated into a live assertion message, so the wrong coordinate is
    what a failing board test printed at an operator.

    Measured rather than pinned: the line is found by searching the migration for the sentence, so
    this test says "the citations agree with the file" and never "the file says what I remember".
    [M5.1 review cycle 4 second pass, M51-C4-CITE-02, M51-C4-CITE-03]
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    package = sorted((root / "backend/spielplan/acquire").glob("*.py"))
    assert len(package) >= 8, "the acquire package is not where this test thinks it is"

    for path in package:
        text = path.read_text(encoding="utf-8")
        stale = re.findall(r"test_[A-Za-z0-9_]+\.py:\d+", text)
        assert not stale, (
            f"{path.name} cites a test by line number ({stale}), which the next commit to that "
            "file falsifies. This tree's convention is file.py::test_name"
        )

    ddl = (root / "backend/migrations/0005_ledger.sql").read_text(encoding="utf-8")
    verbatim = [
        n for n, line in enumerate(ddl.splitlines(), 1)
        if "shown verbatim on the admin board" in line
    ]
    assert verbatim == [138], f"0005_ledger.sql moved the comment this package quotes: {verbatim}"

    quoting = package + [
        root / "backend/tests/spec_coverage.toml",
        root / "backend/tests/test_acquire_pipeline.py",
        root / "backend/tests/test_acquisition_board.py",
    ]
    for path in quoting:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"0005_ledger\.sql:(\d+)", text):
            window = text[max(0, match.start() - 160):match.end() + 160]
            if "verbatim" not in window:
                continue
            assert int(match.group(1)) == verbatim[0], (
                f"{path.name} cites 0005_ledger.sql:{match.group(1)} for the 'shown verbatim' "
                f"comment, which is on line {verbatim[0]} - {match.group(1)} is a different column"
            )
