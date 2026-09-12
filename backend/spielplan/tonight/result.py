"""§6.2 step 7's reveal, assembled from the slate the combine wrote.

Spec v2.1 §6.2 step 7 (rewritten, 54d-54e), §6.0, §6.4, §7.1, §13.

    "**7. Reveal & winner card:** ... Match lines appear on the winner card and each runner-up."

THIS WAS A CLOSURE INSIDE THE ROUTER, and four of the reveal's rules lived in it: which stored
rows reach the screen at all, the wildcard's label, the budget the fit line is measured against,
and the order of the runners-up. None of the four was reachable without the ASGI app, and three
of them had been wrong at least once — the `runner_up` rows (the candidate pool's tail, on no
ballot and therefore always "0 approved") were drawn in place of the two losing finalists, the
order was `-rank` so the closest runner-up came last and the wildcard came first, and the label
was spelled by the client while `combine.WILDCARD_LABEL` had no reader. `api/rank.py` and
`api/rate.py` decide HTTP shapes and nothing else; this is the module that lets `api/tonight.py`
do the same. [M4.12 arch-06]

THE SLOT SAYS WHAT A CARD IS; THE RANK ONLY ORDERS IT. 54d fixes the ballot at three finalists
plus a wildcard, and `session_result` also stores the pool's tail so §14 risk 6 can compare the
slate against what it beat. §6.2 step 7's "runners-up" are the titles that RAN — the rest of the
ballot, ordered by how close they came — so the filter is on the slot and the tie-break on the
rank, which is 1-best and therefore ascending.

THE DEEP LINK IS HANDED IN. §7.1's `{jf_url}/web/#/details?id={jellyfin_id}` needs the
connector's configured URL, which is configuration rather than arithmetic, so the caller brings
a link-maker or brings nothing. Nothing is the configured-less case §6.0 asks for: absent rather
than guessed, which is why the argument is optional rather than a function returning a bare path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import asyncpg

from spielplan.tonight import combine as combine_rules
from spielplan.tonight import pool as pool_rules

# 54e/proposal 60: the reveal opens with an explicit beat before the winner appears — "shipping
# the property without the moment ships half of it". It is copy the client renders verbatim, so
# it lives beside the assembly that sends it rather than in the component that draws it.
BEAT = "VOTES REVEALED TOGETHER"

ON_THE_BALLOT = (combine_rules.SLOT_FINALIST, combine_rules.SLOT_WILDCARD)


def card(
    row: Mapping[str, Any],
    *,
    kind: str | None,
    budget_min: int,
    approvals: int,
    play_url: Callable[[str], str] | None,
) -> dict[str, Any]:
    """One card on the reveal, from one `session_result` row joined to its title.

    Assembled from named fields rather than by serialising the row: §6.2 step 3's pool is
    "internal — never shown as a step", and `session_result` carries `group_score`, which is the
    pool's own ranking under another name.
    """
    return {
        "title_id": row["title_id"], "rank": row["rank"], "slot": row["slot"],
        # `kind` travels with `runtime_min` because the runtime does not mean the same thing
        # without it: §6.0's label reads a series in minutes per EPISODE, and the reveal was the
        # one card that held a runtime and no kind, so it printed a series' 45 minutes the way it
        # prints a film's. Every other slate payload already carries it (`tonight/play.py:160`).
        # It is the SESSION's kind and not each row's: 0013 makes `session.kind` single-valued
        # and says why ("an evening resolves to ONE title"), so §4.1 rule 5's partition happened
        # when the pool was built and every row on the slate is of this kind.
        # [M4.9 finding 37; review cycle 1: M49-CARD-2]
        "kind": kind,
        "name": row["name"], "year": row["year"], "runtime_min": row["runtime_min"],
        "poster_path": row["poster_path"],
        "approvals": approvals,
        "match_lines": list((row["per_user_match"] or {}).values()),
        "conflict": row["conflict"],
        # 54d: the reserved slot is "**labelled as such**". `conflict` beside it says the household
        # is split and names the facet; this says which of the three cards is the other side of it,
        # which is the half the clause asks for and the half nothing carried. A plain bool rather
        # than the copy, because §6.2 fixes the headline verbatim and `copy.py` keeps it out of
        # reach — the words belong to the client's label, the fact belongs here. [decision 220]
        "reserved": bool(row["reserved"]),
        # §6.4's "honestly labelled", from the one place that holds the words. The client spelled
        # them itself, which left `combine.WILDCARD_LABEL` with no reader and the two free to
        # drift — and §6.4 is a claim about what the person is told, so the copy is the rule
        # rather than decoration around it.
        "label": (
            combine_rules.WILDCARD_LABEL
            if row["slot"] == combine_rules.SLOT_WILDCARD else None
        ),
        # The session's kind, for the reason `kind` is on this card at all: 54h's budget is
        # per episode on a series night and the label has to say so, and this was the one
        # fit line built outside `with_budget` and therefore outside the candidate that
        # carries its kind. [decision 219]
        "fit_line": pool_rules.fit_line(
            runtime_min=row["runtime_min"], budget_min=budget_min, kind=kind
        ),
        "play_url": (
            play_url(row["jellyfin_id"])
            if play_url is not None and row["jellyfin_id"] else None
        ),
    }


async def slate(
    conn: asyncpg.Connection,
    session_id: int,
    counted: Sequence[Mapping[str, Any]],
    outcome: Mapping[str, Any],
    *,
    play_url: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """§6.2 step 7's winner card, its runners-up, and the beat before them.

    `counted` and `outcome` arrive from `ballot.tally` and `ballot.resolve` rather than being
    re-derived here. That is deliberate: the tally refuses until every seat has submitted and the
    resolve is idempotent, and both of those are the *ballot's* promises — a second call from
    this module would be a second place deciding when the evening is revealed, which is the
    shape 54e's simultaneity cannot survive.
    """
    # THE FILTER IS IN THE STATEMENT, where `ballot.slate_of` already keeps it. `session_result`
    # holds the POOL — `combine.sequence` is a permutation of every candidate and `finish` writes a
    # row per element, so §14 risk 6 can compare the slate against what it beat — which is 696 rows
    # on the shipped owned pool, joined to `title` and carrying `per_user_match` jsonb, to put four
    # cards on a screen. Selecting them and discarding all but four in Python also left two readers
    # of one table disagreeing about where the rule lives, which is the shape arch-06 lifted this
    # module out of the router to remove. [M4.12 review cycle 1: M412-RESULT-4]
    rows = await conn.fetch(
        """
        SELECT r.title_id, r.rank, r.slot, r.group_score, r.per_user_match, r.conflict,
               r.reserved,
               t.name, t.year, t.runtime_min, t.poster_path, t.jellyfin_id
          FROM session_result r JOIN title t ON t.id = r.title_id
         WHERE r.session_id = $1 AND r.slot IN ('finalist', 'wildcard')
         ORDER BY r.rank
        """,
        session_id,
    )
    approvals = {r["title_id"]: r["approvals"] for r in counted}
    # Both controls off the one row that holds them, and `runtime_budget_min` is what the fit
    # line is measured against — the budget the person set, never the +40 admission bound they
    # never saw (`pool.over_budget_by`).
    controls = await conn.fetchrow(
        "SELECT kind, runtime_budget_min FROM session WHERE id = $1", session_id
    )
    budget = (controls["runtime_budget_min"] if controls else None) or pool_rules.DEFAULT_BUDGET_MIN
    cards = [
        card(
            row,
            kind=controls["kind"] if controls else None,
            budget_min=budget,
            approvals=approvals.get(row["title_id"], 0),
            play_url=play_url,
        )
        for row in rows
        # Belt and braces now that the predicate is in the statement: `ON_THE_BALLOT` is what
        # `card` and the three lists below are written against, and the two spellings of one rule
        # are three lines apart where a reader can see them agree.
        if row["slot"] in ON_THE_BALLOT
    ]
    winner = next((c for c in cards if c["title_id"] == outcome["chosen_title_id"]), None)
    runners_up = sorted(
        (c for c in cards if winner is None or c["title_id"] != winner["title_id"]),
        key=lambda c: (-c["approvals"], c["rank"]),
    )
    return {
        "session_id": session_id,
        "beat": BEAT,
        "winner": winner,
        "approval_share": outcome["approval_share"],
        "participants": outcome["participants"],
        "unanimous": winner is not None and winner["approvals"] == outcome["participants"],
        "runners_up": runners_up,
        "wildcard": next(
            (c for c in cards if c["slot"] == combine_rules.SLOT_WILDCARD), None
        ),
        "finalists": [c for c in cards if c["slot"] == combine_rules.SLOT_FINALIST],
    }


__all__ = ["BEAT", "ON_THE_BALLOT", "card", "slate"]
