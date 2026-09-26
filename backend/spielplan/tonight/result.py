"""§6.2 step 7's reveal, assembled from the slate the combine wrote.

The slot says what a card is; the rank only orders it. The Jellyfin deep link is handed in, and
absent (§6.0) when there is no connector.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import asyncpg

from spielplan.tonight import ballot as ballot_rules
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import copy as copy_rules
from spielplan.tonight import pool as pool_rules

# 54e/proposal 60: the explicit beat before the winner, rendered verbatim by the client.
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
    """One reveal card from one `session_result` row; named fields, so `group_score` never ships."""
    return {
        "title_id": row["title_id"], "rank": row["rank"], "slot": row["slot"],
        # The session's kind: a series runtime is per episode (§6.0).
        "kind": kind,
        "name": row["name"], "year": row["year"], "runtime_min": row["runtime_min"],
        "poster_path": row["poster_path"],
        "approvals": approvals,
        "match_lines": list((row["per_user_match"] or {}).values()),
        # Decision 486: without Show the model, plain sentences and no D (`copy.for_member`).
        "conflict": row["conflict"] if show_model else copy_rules.for_member(row["conflict"]),
        # 54d's reserved slot, "labelled as such"; the words are the client's (decision 220).
        "reserved": bool(row["reserved"]),
        # Decision 479's person reservation: the seat and its name, never its scores.
        "reserved_for": (
            None if row["reserved_for"] is None
            else {"participant_id": row["reserved_for"], "name": row["reserved_name"]}
        ),
        # §6.4's "honestly labelled", from the one place that holds the words.
        "label": (
            combine_rules.WILDCARD_LABEL
            if row["slot"] == combine_rules.SLOT_WILDCARD else None
        ),
        # The session's kind, for 54h's per-episode label (decision 219).
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

    `counted` and `outcome` come from `ballot.tally` and `ballot.resolve`, which own the timing.
    """
    # The ballot filter is in the statement: `session_result` holds the whole pool.
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
    # The budget the person set, never the +40 admission bound (`pool.over_budget_by`).
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
        if row["slot"] in ON_THE_BALLOT
    ]
    winner = next((c for c in cards if c["title_id"] == outcome["chosen_title_id"]), None)
    # One place per card: runners-up are the losing finalists; the wildcard shows once, or as winner.
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
        # How broad each person's yes was, beside the approval share §13 evaluates on.
        "breadth": await breadth(conn, session_id, winner_id=outcome["chosen_title_id"]),
        "runners_up": runners_up,
        "wildcard": wildcard,
        "finalists": [c for c in cards if c["slot"] == combine_rules.SLOT_FINALIST],
    }


async def breadth(
    conn: asyncpg.Connection, session_id: int, *, winner_id: int | None
) -> list[dict[str, Any]]:
    """Each seat's approval breadth for the reveal, refused until every seat has submitted (54e)."""
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
