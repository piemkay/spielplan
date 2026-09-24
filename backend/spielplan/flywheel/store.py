"""The flywheel's queue: the row writer, the admin queue's read, and the two feeds M6 produces.

Spec v2.1 §8.4 (the queue, its three feeds and "the enqueue is immediate and visible"), §6.6 Data
(the extraction queue, each feed "labelled with its reason"), §6.4 (the compositional search whose
empty predicates "land in the flywheel with their reason"); decisions 328, 330, 344, 440 and 441.

THE READER IS THE ADMIN AND THE REASON IS THE PRODUCT. `flywheel_item.reason` is "shown verbatim
in the admin queue" (`0004_dna.sql:163`), so every writer here composes a sentence for a person
deciding whether to spend on it, and the read hands it back untouched - no truncation and no
friendlier rewording on the way out, for the reason `acquire/board._job_row` gives for the board's
own column. ASCII where it can be, because the same string reaches a Windows console in a log line.

NO COST IS STORED AT ENQUEUE (decision 441). `est_cost_usd` stays NULL on every row: a figure
priced at one pass count is stale the moment the admin picks another (plan C6), so the Launch total
is computed when it is asked, over the batch's own providers and passes, and the `flywheel_batch`
row records the figures a launch was held against.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import asyncpg

from spielplan.db import dna_terms

# 0029's CHECK, after decision 328 struck the "unnamed taste" residual feed.
KINDS = ("empty_predicate", "uncovered_frontier", "thin_facet")
EMPTY_PREDICATE, UNCOVERED_FRONTIER, THIN_FACET = KINDS

# Every status that is not closed, which is 0029's one-open-row index's own definition: `approved`
# is unused (decision 443) and is in the set so that no later use of it can open a second row.
OPEN = ("queued", "approved", "running")

# `acquire/board.BOARD_LIMIT`'s argument at the same scale: a household's queue is small while
# only walked titles feed it (decision 440), and an unbounded read on an admin surface is the one
# that falls over on somebody else's install. Newest first, so the cut falls on the oldest rows.
QUEUE_LIMIT = 200

_ENQUEUE = """
INSERT INTO flywheel_item (kind, detail, reason, title_id, est_titles)
VALUES ($1, $2::text::jsonb, $3, $4, $5)
RETURNING id
"""

# Newest first, read through `0004_dna.sql`'s `flywheel_status (status, created_at DESC)`, the
# index built beside the table for exactly this read. The two LEFT JOINs are what make a row
# legible rather than an id: the title it is about, and - for a row whose batch is running - the
# board row that says why its title is still waiting (§6.6 Data: every reason verbatim).
_QUEUE = """
SELECT f.id, f.kind, f.reason, f.detail, f.est_titles, f.status, f.created_at, f.batch_id,
       f.title_id, t.name, t.year, j.stage AS job_stage, j.status AS job_status,
       j.reason AS job_reason
  FROM flywheel_item f
  LEFT JOIN title t ON t.id = f.title_id
  LEFT JOIN acquisition_job j ON j.title_id = f.title_id
 WHERE f.status = ANY($1::text[])
 ORDER BY f.created_at DESC, f.id DESC
 LIMIT $2
"""

_KNOWN_TERMS = "SELECT count(DISTINCT term) FROM dna_term WHERE version = $1 AND term = ANY($2::text[])"


async def enqueue(
    conn: asyncpg.Connection,
    *,
    kind: str,
    detail: dict[str, Any],
    reason: str,
    title_id: int | None = None,
    est_titles: int | None = None,
) -> int:
    """Append one queued row and return its id. The kind is 0029's CHECK's to refuse.

    `$2::text::jsonb` with `json.dumps` and not `$2::jsonb`, for `acquire/queue.enqueue`'s reason:
    `db/pool.py` registers `json.dumps` as the jsonb encoder, so a dict bound against a jsonb
    parameter is encoded twice and stored as a JSON string every reader then iterates character by
    character.

    A thin-facet row does not come through here. Its writer is `flywheel.thin.observe_title`, whose
    statement refreshes the title's one open row instead of appending a second, which 0029's index
    would refuse.
    """
    return await conn.fetchval(_ENQUEUE, kind, json.dumps(detail), reason, title_id, est_titles)


# A launched title taken back out of its batch (decision 448). `running` to `queued` keeps the
# title's one open row - 0029's index counts both as open, so no second row can be born of it - and
# `batch_id` is cleared because the row is no longer in one: 0029 ties a batch to `running` and to
# nothing else, and `flywheel_batch` keeps the figures the launch was held against.
_RELEASE = """
UPDATE flywheel_item SET status = 'queued', batch_id = NULL
 WHERE kind = $1 AND title_id = $2 AND status = 'running'
RETURNING id
"""


async def release_title(conn: asyncpg.Connection, title_id: int) -> int:
    """Queue a title's launched rows again, out of their batch; how many there were (decision 448).

    The board's abandon calls it, in its own transaction, beside removing the batch plan from the
    title's tasks (`acquire/actions.abandon`): a launched row otherwise stays `running` until a
    stage-8 observation a parked walk never reaches, and a running row is one no relaunch may take.
    """
    return len(await conn.fetch(_RELEASE, THIN_FACET, title_id))


def _decoded(detail: Any) -> dict[str, Any]:
    # `acquire/board.job`'s rule: a pooled connection decodes jsonb, a bare `asyncpg.connect` does
    # not, and both read this table.
    if isinstance(detail, str):
        detail = json.loads(detail) if detail else {}
    return detail or {}


async def queue(conn: asyncpg.Connection, *, limit: int = QUEUE_LIMIT) -> list[dict[str, Any]]:
    """The open rows, newest first: the admin queue §8.4's flow starts from.

    Every feed is here, each with its reason as written (§6.6 Data); a row whose producer is M6's
    is shown and labelled although no M5 launch can act on it (decision 443). `title` and `board`
    are None for a row that names no title, and `board` is None for a title the pipeline has never
    walked.
    """
    rows = await conn.fetch(_QUEUE, list(OPEN), limit)
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "reason": row["reason"],
            "detail": _decoded(row["detail"]),
            "est_titles": row["est_titles"],
            "status": row["status"],
            "created_at": row["created_at"],
            "batch_id": row["batch_id"],
            "title_id": row["title_id"],
            "title": None if row["name"] is None else {"name": row["name"], "year": row["year"]},
            "board": None if row["job_status"] is None else {
                "stage": row["job_stage"], "status": row["job_status"], "reason": row["job_reason"],
            },
        }
        for row in rows
    ]


async def _vocabulary_carries(conn: asyncpg.Connection, terms: Sequence[str]) -> str | None:
    """The active vocabulary's version when it carries every one of `terms`; otherwise None.

    None for no terms and for no vocabulary as well: a failure that names nothing the vocabulary
    has is a failure no batch can direct spend at (decision 344).
    """
    named = sorted(set(terms))
    if not named:
        return None
    version = await dna_terms.active_version(conn)
    if version is None:
        return None
    known = await conn.fetchval(_KNOWN_TERMS, version, named)
    return version if known == len(named) else None


def _spoken(terms: Sequence[str]) -> str:
    """`themes.robots` as §6.4's example writes it - "robots" - and several joined by " + "."""
    return " + ".join(term.split(".", 1)[-1].replace("_", " ") for term in dict.fromkeys(terms))


async def enqueue_empty_predicate(
    conn: asyncpg.Connection, *, query: str, predicate: str, terms: Sequence[str]
) -> int | None:
    """§6.4: "Empty predicates land in the flywheel with their reason ("no owned title carries robots
    + gladiatorial")". Returns the row's id, or None when nothing was written.

    PRODUCER: M6's compositional search (§6.4), which does not exist yet. This writer is
    deliberately UNCALLED at M5 - the shape `worker.py` gives `explore-frontier-cache`, a job
    registered under M6 with no `run` - so that `home/rail.parse_line`'s " -> flywheel" tail is a
    promise the app can keep the day M6 calls it, rather than a write M6 has to invent.

    THE CALLER'S CONTRACT (decision 344): when any of `terms` is outside the active vocabulary -
    or there is no vocabulary, or no term at all - this writes nothing and returns None, and the
    caller reports the case in its own parse line. Extraction is bound to the frozen vocabulary and
    no batch can add a term (decision 163), so such a row's only control, Spend, would buy nothing.
    `terms` are the vocabulary ids the predicate names; `query` and `predicate` are what the member
    typed and what it was parsed into, kept in `detail` for the operator.
    """
    version = await _vocabulary_carries(conn, terms)
    if version is None:
        return None
    detail = {"query": query, "predicate": predicate, "terms": sorted(set(terms)), "version": version}
    return await enqueue(
        conn, kind=EMPTY_PREDICATE, detail=detail,
        reason=f"no owned title carries {_spoken(terms)}",
    )


async def enqueue_uncovered_frontier(
    conn: asyncpg.Connection, *, region: str, terms: Sequence[str]
) -> int | None:
    """§8.4's second feed: an explore frontier no owned title's vocabulary covers. Returns the row's
    id, or None when nothing was written.

    PRODUCER: M6's explore-frontier cache - `worker.py`'s `Job("explore-frontier-cache", "M6", ...)`,
    registered with no `run` callable. UNCALLED at M5 for the reason `enqueue_empty_predicate`
    gives, and under the same contract (decision 344): a term outside the active vocabulary, no
    vocabulary or no term writes nothing and returns None. `region` is the frontier cache's own
    label for the region and `terms` the vocabulary ids it found uncovered there.
    """
    version = await _vocabulary_carries(conn, terms)
    if version is None:
        return None
    detail = {"region": region, "terms": sorted(set(terms)), "version": version}
    return await enqueue(
        conn, kind=UNCOVERED_FRONTIER, detail=detail,
        reason=f"the explore frontier at {region} has no owned title carrying {_spoken(terms)}",
    )
