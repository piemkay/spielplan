"""§8.4's thin-facet feed, observed the moment a title's walk finishes stage 8.

Spec v2.1 §8.4 ("A title is thin-facet when at least one facet the active vocabulary declares
carries no extracted-tier term for it ... and its row is written the moment its walk finishes stage
8"), §14 risk 2, §4.1 rule 2; decisions 329, 390, 440 and 441.

THE TEST IS PRESENCE AND NOTHING ELSE (decision 329). A facet is unnamed when
`dna.coverage.facet_coverage` counts zero distinct extracted-tier terms for it, and one unnamed
facet makes the title thin - "every declared facet must be named, eleven on vocabulary v1, and no
count threshold applies". So this module holds no number: a zero in the mapping is the whole test,
and a cut of two terms a facet or three would be the corpus's `/ 3` proxy decision 390 refused to
port. It is a different question from `placement/features.py`'s per-block `is_thin`, which asks
whether a feature block was dropped and says nothing about the vocabulary. The count is of terms
and never of a weight, so §4.1 rule 2 is not in play here at all.

WHERE AND WHEN (decision 440). The driver calls `observe_title` after stage 8 advances, inside the
walk that holds the title's lock, because §8.4 says "the moment its walk finishes stage 8" and a
job cannot keep that: a nightly sweep over the extracted tier passes every naive test and fails the
feed's coverage row, which says "not after a nightly job". Stage 8 has a body since M5 - it projects
an acquired title and leaves a bundle title's projected tier alone (decision 463) - and the
observation stays the driver's, on a flag the stage row carries (`pipeline.Stage.observes_coverage`),
so it fires on both branches. It went live on a real install when stage 5 was wired (decision 461):
until then stage 6 parked every title on a missing pack (decision 432) and no walk reached stage 8.
"""

from __future__ import annotations

import json

import asyncpg

from spielplan.db import dna_terms
from spielplan.dna import coverage
from spielplan.flywheel import store

# A launched batch's rows are `running` until the title's next stage-8 observation, which is how
# they end (decision 443): whatever the batch bought, the walk it made due has now been measured. A
# walk that stops before stage 8 is taken out of its batch by the board's abandon instead, which
# queues its row again (`store.release_title`, decision 448).
_CLOSE_RUNNING = (
    "UPDATE flywheel_item SET status = 'done' WHERE kind = $1 AND title_id = $2 AND status = 'running'"
)

# A title that is no longer thin closes whatever of its row is still open.
_CLOSE_OPEN = (
    "UPDATE flywheel_item SET status = 'done'"
    " WHERE kind = $1 AND title_id = $2 AND status = ANY($3::text[])"
)

# ONE OPEN ROW PER TITLE, refreshed rather than appended: 0029's `flywheel_item_one_open_thin_facet`
# is the arbiter, and the conflict target restates its predicate word for word so Postgres infers
# that index and no other. The refresh keeps `created_at`, because "queued just now" (§8.4) is when
# the title was first found thin, not when it was last walked; it keeps `status` for the same
# reason. `est_titles` is 1 - a thin-facet row is one title - and `est_cost_usd` stays NULL
# (decision 441: a stored figure is stale the moment the pass count changes).
_REFRESH = """
INSERT INTO flywheel_item (kind, title_id, detail, reason, est_titles)
VALUES ('thin_facet', $1, $2::text::jsonb, $3, 1)
ON CONFLICT (title_id) WHERE kind = 'thin_facet' AND status IN ('queued', 'approved', 'running')
DO UPDATE SET reason = EXCLUDED.reason, detail = EXCLUDED.detail
RETURNING id
"""


def _reason(unnamed: list[str], declared: int, version: str) -> str:
    """The queue's sentence: which facets, out of how many, and what a batch would do about it."""
    return (
        f"{len(unnamed)} of the {declared} facets vocabulary {version} declares carry no"
        f" extracted-tier term for this title: {', '.join(unnamed)}. An extraction batch asks its"
        " sources again for these, under terms the vocabulary already has"
    )


async def observe_title(conn: asyncpg.Connection, title_id: int) -> int | None:
    """Record what stage 8's finish says about one title's facets. The open row's id, or None.

    In one transaction, committed when this returns, so the row is readable from the admin queue at
    once with no job tick between (§8.4, decision 440):

      1. this title's `running` rows close `done` - a launched batch's walk has been measured;
      2. with no active vocabulary there is nothing to measure against (§3.1's bundle-less install
         is legal), and nothing else is written;
      3. a title that names every declared facet closes its open row `done`;
      4. otherwise its one open row is written, or refreshed with today's reason and counts.

    Nothing here is caught. A write that fails propagates to the driver, which records the walk's
    failure: a feed that silently missed a title would be the queue nobody can diagnose, which is
    the harm plan §1 names for splitting the queue from what fills it.
    """
    async with conn.transaction():
        await conn.execute(_CLOSE_RUNNING, store.THIN_FACET, title_id)
        version = await dna_terms.active_version(conn)
        if version is None:
            return None
        named = await coverage.facet_coverage(conn, title_id, version=version)
        if not named:
            return None
        unnamed = [facet for facet, terms in named.items() if terms == 0]
        if not unnamed:
            await conn.execute(_CLOSE_OPEN, store.THIN_FACET, title_id, list(store.OPEN))
            return None
        detail = {"title_id": title_id, "version": version, "coverage": named, "unnamed": unnamed}
        return await conn.fetchval(
            _REFRESH, title_id, json.dumps(detail), _reason(unnamed, len(named), version)
        )
