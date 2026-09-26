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

from spielplan.tonight import ballot as ballot_rules
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import copy as copy_rules
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
    show_model: bool = False,
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
        # Decision 486's register, applied where the payload is built: D is a model number and
        # "the axis is zeroed" is model vocabulary, so a member with Show the model off is sent
        # the plain sentences and never the number (`copy.for_member`).
        "conflict": row["conflict"] if show_model else copy_rules.for_member(row["conflict"]),
        # 54d: the reserved slot is "**labelled as such**". `conflict` beside it says the household
        # is split and names the facet; this says which of the three cards is the other side of it,
        # which is the half the clause asks for and the half nothing carried. A plain bool rather
        # than the copy, because §6.2 fixes the headline verbatim and `copy.py` keeps it out of
        # reach — the words belong to the client's label, the fact belongs here. [decision 220]
        "reserved": bool(row["reserved"]),
        # Decision 479's person reservation, which is a different claim from the axis one above
        # and carries its own label on the client: "{name}'s pick". The seat and its display name,
        # never the seat's scores.
        "reserved_for": (
            None if row["reserved_for"] is None
            else {"participant_id": row["reserved_for"], "name": row["reserved_name"]}
        ),
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
    show_model: bool = False,
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
               r.reserved, r.reserved_for,
               coalesce(u.name, 'Guest ' || (p.seat - 1)) AS reserved_name,
               t.name, t.year, t.runtime_min, t.poster_path, t.jellyfin_id
          FROM session_result r JOIN title t ON t.id = r.title_id
          LEFT JOIN session_participant p ON p.id = r.reserved_for
          LEFT JOIN app_user u ON u.id = p.user_id
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
            show_model=show_model,
        )
        for row in rows
        # Belt and braces now that the predicate is in the statement: `ON_THE_BALLOT` is what
        # `card` and the three lists below are written against, and the two spellings of one rule
        # are three lines apart where a reader can see them agree.
        if row["slot"] in ON_THE_BALLOT
    ]
    winner = next((c for c in cards if c["title_id"] == outcome["chosen_title_id"]), None)
    # ONE PLACE PER CARD. The runners-up were every ballot card but the winner, wildcard included,
    # and the wildcard also has a block of its own below — so the second household evening's reveal
    # listed Everything Everywhere All at Once twice, once as "0 approved" under Runners-up and once
    # as the Wildcard. §6.2 step 7 names three things: the winner, the runners-up and one wildcard.
    # The runners-up are the finalists that lost, and the wildcard is shown once, in its own block
    # with its own count, unless it won, in which case the winner card is where it is.
    runners_up = sorted(
        (
            c for c in cards
            if c["slot"] == combine_rules.SLOT_FINALIST
            and (winner is None or c["title_id"] != winner["title_id"])
        ),
        key=lambda c: (-c["approvals"], c["rank"]),
    )
    wildcard = next((c for c in cards if c["slot"] == combine_rules.SLOT_WILDCARD), None)
    if wildcard is not None and winner is not None and wildcard["title_id"] == winner["title_id"]:
        wildcard = None
    return {
        "session_id": session_id,
        "beat": BEAT,
        "winner": winner,
        "approval_share": outcome["approval_share"],
        "participants": outcome["participants"],
        # "Unanimous." used to stand here, and it was literally true over an evening where one
        # member said yes to all four titles and the other to one — the winner being her only
        # yes. Approval share is the number §13 evaluates on and it stays; beside it the reveal
        # now says how broad each person's yes was, which step 6's "then they are revealed
        # together" permits once every ballot is in and not a moment before.
        "breadth": await breadth(conn, session_id, winner_id=outcome["chosen_title_id"]),
        "runners_up": runners_up,
        "wildcard": wildcard,
        "finalists": [c for c in cards if c["slot"] == combine_rules.SLOT_FINALIST],
    }


async def breadth(
    conn: asyncpg.Connection, session_id: int, *, winner_id: int | None
) -> list[dict[str, Any]]:
    """Each seat's approval breadth, for the reveal: how many of the ballot they said yes to, and
    whether the winner was their only yes.

    Refused with the ballot's own reason until every seat has submitted, IN THIS FUNCTION, for
    `ballot.tally`'s reason: 54e's blindness is a property of the read, and the per-seat counts are
    the most direct leak of who voted how that this module could produce. The route reaches here
    only after `tally` has already refused, and the guard is repeated so a second caller cannot
    forget it.
    """
    if not await ballot_rules.everyone_submitted(conn, session_id):
        raise ballot_rules.BallotError(
            "still_voting", "approvals stay hidden until everyone has submitted"
        )
    rows = await conn.fetch(
        """
        SELECT p.id, p.seat, coalesce(u.name, 'Guest ' || (p.seat - 1)) AS name,
               count(*) FILTER (WHERE b.approved) AS yes, count(b.id) AS of,
               coalesce(bool_or(b.approved AND b.title_id = $2), false) AS chose_winner
          FROM session_participant p
          LEFT JOIN app_user u ON u.id = p.user_id
          LEFT JOIN session_ballot b ON b.participant_id = p.id
         WHERE p.session_id = $1
         GROUP BY p.id, p.seat, u.name
         ORDER BY p.seat
        """,
        session_id, winner_id,
    )
    return [
        {
            "participant_id": r["id"], "name": r["name"],
            "approved": int(r["yes"]), "of": int(r["of"]),
            "only_yes": int(r["yes"]) == 1 and bool(r["chose_winner"]),
        }
        for r in rows
    ]


__all__ = ["BEAT", "ON_THE_BALLOT", "breadth", "card", "slate"]
