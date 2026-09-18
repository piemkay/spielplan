"""§6.6's acquisition board, read side. Spec v2.1 §6.6, §8; decisions 322, 336, 345.

§6.6 gives the admin an Acquisition surface: "the pipeline board (per-title stage, status,
reason), retry / retry-from-stage / abandon". This module is the READ half of that sentence and
deliberately nothing else. The three controls are proposal 109's, adopted or struck by number
under decision 330, which is M5.6's to take - and a read module that also knew how to retry
would be the place someone adds the fourth control nobody decided on.

WHY THE QUERIES ARE HERE AND NOT IN THE ROUTER. `api/` decides only HTTP shapes and the rules
live in the domain packages (CLAUDE.md Conventions), and on this surface the rule is not
cosmetic: what the board may show is decided by decision 345, and the shape of the join that
finds a title's documents is a claim about how the pipeline files them. Both belong beside the
pipeline that writes those rows, where the next stage author will read them, rather than in a
route module they would have to go looking in.

**DECISION 345 IS ENFORCED BY THIS MODULE AND NOWHERE ELSE.** The board "does not show the
document. The board links to the `raw_document` METADATA row - url, http status, sha256, byte
size, fetched_at, all in Postgres - and the bytes are reachable only by an operator on the box."
That is not a restraint the API layer could impose even if it wanted to: `/data/raw` is mounted
on the worker alone (`docker-compose.yml`, M4.7 sec-08), so the backend process serving this
read cannot open the file at any price. What this module must therefore not do is hand back
something that invites a later route to try - which is why `content_path` is NOT selected below,
although it is the most obvious column in the row. The rawstore port refused to bring the
corpus's `StoredDocument.path()` across for the same reason, and decision 345 enumerates the
five fields the board shows; a path is not one of them and would read as an offer.

THE JOIN FROM A TITLE TO ITS DOCUMENTS IS A SEAM M5.2 ONWARD HAS TO HONOUR. `raw_document`
carries no `title_id` and correctly so: the store is written by stages that fetch, and at the
moment of the fetch the thing being fetched is identified by the acquisition task's own key
(`pipeline.key_for_item` - `jellyfin:<id>`, `imdb:<id>`, `tmdb:<id>`), which may predate the
title row entirely under decision 322. So a document belongs to a title when it is filed under
the key of a task that belongs to that title, and `documents_for_title` below says exactly that
in SQL. A fetching stage that invents its own `entity_key` spelling files documents this board
will never find, and `test_acquisition_board.py` is where that becomes a failing test rather
than an empty list nobody questions.

`payload ->> 'title_id'` IS THE TASK-TO-TITLE LINK, and it is complete in both directions a task
can arrive: `pipeline.enqueue_title` writes `{"title_id": n}` at enqueue time for the inbox rows
`placement/reconcile._park_thin` has been leaving since M4.13, and `pipeline._remember_title`
writes the same key onto a `jellyfin:`-keyed task the moment stage 1 establishes which title it
turned out to be. A task that has neither has not identified anything yet, and it has no board
row to be read from either (decision 323: an item with no provider id parks at stage 1 and mints
nothing) - which is the one thing an operator looking for such a title will not find here, and
the queue is where it is.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

# How much of the board one read returns. A household's Jellyfin library is tens of thousands of
# titles and §8 runs the pipeline over the ones it does not already have, so the board is small
# by construction - but "small by construction" is a statement about today's install, and an
# unbounded read on an admin surface is the one that falls over on somebody else's. A default
# rather than a query parameter: the caller that wants a different page is M5.6's, and it arrives
# with the paging decision rather than with a number any URL can set.
BOARD_LIMIT = 200

# Per title, same argument at a different scale: the documents are per fetch, so a title
# re-derived a dozen times over a year carries a dozen rows per source. Newest first, so the cut
# falls on the rows an operator debugging a stuck title is least likely to want.
DOCUMENT_LIMIT = 100

_BOARD = """
SELECT j.title_id, j.stage, j.status, j.reason, j.retry_after, j.updated_at,
       t.name, t.year, t.kind, t.origin
  FROM acquisition_job j
  JOIN title t ON t.id = j.title_id
 ORDER BY j.updated_at DESC
 LIMIT $1
"""

_JOB = """
SELECT j.title_id, j.stage, j.status, j.reason, j.retry_after, j.updated_at, j.detail,
       t.name, t.year, t.kind, t.origin
  FROM acquisition_job j
  JOIN title t ON t.id = j.title_id
 WHERE j.title_id = $1
"""

_TASKS = """
SELECT id, kind, key, state, attempts, max_attempts, next_attempt_at,
       last_error, result_note, paid, created_at, updated_at
  FROM acquisition_task
 WHERE payload ->> 'title_id' = $1::text
 ORDER BY id
"""

_DOCUMENTS = """
SELECT d.id, d.source, d.kind, d.entity_key, d.url, d.http_status, d.content_sha256,
       d.byte_size, d.content_type, d.fetched_at, d.ok, d.error
  FROM raw_document d
 WHERE d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.fetched_at DESC, d.id DESC
 LIMIT $2
"""


def _job_row(row: asyncpg.Record) -> dict[str, Any]:
    """One board row, with `reason` carried across untouched.

    `0005_ledger.sql:138` says of that column, in its own words, "shown verbatim on the admin
    board", and this is the function that either keeps that promise or quietly breaks it. No
    truncation, no title-casing, no substitution of a friendlier sentence for a park an operator
    has to act on: §8's reasons are written by the stage that parked, and decision 336 makes
    `reason` the thing that tells "waiting on something that may change" from "this stage raised
    and will raise again" on a board where both read as a row.
    """
    return {
        "title_id": row["title_id"],
        "name": row["name"],
        "year": row["year"],
        "title_kind": row["kind"],
        "origin": row["origin"],
        "stage": row["stage"],
        "status": row["status"],
        "reason": row["reason"],
        "retry_after": row["retry_after"],
        "updated_at": row["updated_at"],
    }


async def board(conn: asyncpg.Connection, *, limit: int = BOARD_LIMIT) -> list[dict[str, Any]]:
    """Every title the pipeline has an opinion about, newest movement first.

    An INNER JOIN to `title`, which is a decision rather than a default. `acquisition_job` is
    keyed on `title_id` with `ON DELETE CASCADE`, so a row whose title is gone cannot exist and a
    LEFT JOIN would be inviting a reader to handle a state the schema forbids. A board entry with
    no name is also not an entry anyone can act on.

    Ordered by `updated_at` rather than by status, because the question this surface answers
    first is "what has the pipeline been doing", and a status ordering would bury the job that
    just failed under three hundred that are ready. `acquisition_job_status (status,
    updated_at DESC)` is the index for the filtered read M5.6 will want when it adds the controls
    that need one.
    """
    return [_job_row(row) for row in await conn.fetch(_BOARD, limit)]


async def job(conn: asyncpg.Connection, title_id: int) -> dict[str, Any] | None:
    """One title's board row, or None when the pipeline has never touched it.

    `detail` comes along here and not in the list above: the pipeline concatenates one key per
    stage into it (`pipeline.write_board`), so it is the record of what the stages BEFORE the
    parked one did - exactly what an operator opening a single job wants, and exactly what would
    make a three-hundred-row list unreadable.
    """
    row = await conn.fetchrow(_JOB, title_id)
    if row is None:
        return None
    # `db/pool.py` registers json.loads as the jsonb decoder, so a pooled connection hands
    # back a dict - but a bare `asyncpg.connect` does not, and both open this table. Decoded
    # here rather than left to the caller, for `acquire/queue.py::Task.from_row`'s reason:
    # ship a string where a mapping was expected and the client iterates it character by
    # character. Discarding a non-dict instead would be the same bug with the evidence gone.
    detail = row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail) if detail else {}
    return {**_job_row(row), "detail": detail or {}}


async def tasks_for_title(conn: asyncpg.Connection, title_id: int) -> list[dict[str, Any]]:
    """The queue rows behind one title: what is scheduled, how often it has been tried, why.

    `lease_owner` and `lease_expires` are deliberately absent. They are the drain's fencing and
    they name a process rather than a fact about the title; an operator reading "leased by
    worker-7fd2" on a board with no control to do anything about it learns only that they cannot
    intervene. `state` already says a task is in flight, and `attempts` against `max_attempts`
    says how much of its life it has spent - the pair decision 336 needs, because a `failed` task
    with attempts left is a plain retry and one without is not.
    """
    rows = await conn.fetch(_TASKS, str(title_id))
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "key": row["key"],
            "state": row["state"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "next_attempt_at": row["next_attempt_at"],
            "last_error": row["last_error"],
            "result_note": row["result_note"],
            "paid": row["paid"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


async def documents_for_title(
    conn: asyncpg.Connection, title_id: int, *, limit: int = DOCUMENT_LIMIT
) -> list[dict[str, Any]]:
    """The `raw_document` METADATA rows for one title. Decision 345, and never the bytes.

    Twelve columns are selected and `content_path` is not one of them; the module docstring
    argues that omission at length. The five decision 345 enumerates are all here - url, http
    status, sha256, byte size, fetched_at - and the rest are what keeps them readable: `source`,
    `kind`, `entity_key`, `content_type`, `ok` and `error`. The last two earn their place, since
    §8's stage 2 fetches from hosts that rate-limit and go down, `0024` gives the row `ok` and
    `error` precisely so a failed fetch is recorded rather than lost, and a board showing only
    successful fetches would answer "why is this title thin" with silence.

    `ok = false` rows are therefore INCLUDED, which is the opposite of what `rawstore.latest`
    does and correct for the opposite reason: that function is looking for bytes to re-parse and
    a failure has none, while this one is looking for what happened.
    """
    rows = await conn.fetch(_DOCUMENTS, str(title_id), limit)
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "kind": row["kind"],
            "entity_key": row["entity_key"],
            "url": row["url"],
            "http_status": row["http_status"],
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "content_type": row["content_type"],
            "fetched_at": row["fetched_at"],
            "ok": row["ok"],
            "error": row["error"],
        }
        for row in rows
    ]
