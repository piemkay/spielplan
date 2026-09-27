"""A verdict from the title card (decision 487), written through `rate.session` as a sweep answer.

No second observation writer (decision 212): the title is stashed as a sweep card and answered.
"""

from __future__ import annotations

from typing import Any, Literal

import asyncpg

from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import EmbeddingSource
from spielplan.rate import LIVE_LABEL, VERDICT_LABELS, session

Answer = Literal["disliked", "fine", "liked", "not_seen"]
ANSWERS: tuple[str, ...] = (*VERDICT_LABELS, "not_seen")

# Undo restores the journal's card verbatim, so this is the reason shown after an undo.
REASON = "You rated it from its title card."
SOURCE = "title_card"


async def live_verdict(
    conn: asyncpg.Connection, *, user_id: int, title_id: int
) -> dict[str, Any] | None:
    """The person's current verdict, via `LIVE_LABEL` so a silent re-ask never shows."""
    row = await conn.fetchrow(
        f"SELECT l.value FROM ({LIVE_LABEL}) l WHERE l.title_id = $2", user_id, title_id
    )
    if row is None:
        return None
    value = int(row["value"])
    return {"value": value, "label": VERDICT_LABELS[value]}


async def _put_on_table(
    conn: asyncpg.Connection, s: session.RateSession, *, title_id: int
) -> session.RateSession:
    """Replace the standing card with a sweep card for `title_id`, under a fresh token.

    Under the session row's lock, which every answer takes first: a mid-flight tap on the old
    card finishes, and its next tap is §6.1's stale-card 409.
    """
    kind = await observations.kind_of(conn, title_id)
    card = {
        "type": "sweep",
        "kind": kind,
        "title_id": title_id,
        "reason": REASON,
        "p_seen": None,
        "source": SOURCE,
        "reask_of": None,
    }
    async with conn.transaction():
        token = await conn.fetchval(
            "SELECT card_token FROM rate_session WHERE id = $1 FOR UPDATE", s.id
        )
        return await session.stash(conn, s, card, expected=token)


async def answer(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    choice: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    jf: session.Jellyfin | None = None,
    later: session.Later | None = None,
) -> session.Outcome:
    """Answer `title_id` from its card with one of `ANSWERS`.

    Raises `LookupError` (no such title), `ValueError` (bad answer), `session.StaleCard` (409).
    """
    if choice not in ANSWERS:
        raise ValueError(f"answer must be one of {ANSWERS}, not {choice!r}")
    s = await session.open_or_resume(conn, user_id=user_id)
    s = await _put_on_table(conn, s, title_id=title_id)
    token = str(s.card_token)
    if choice == "not_seen":
        return await session.record_not_seen(conn, s, card_token=token, jf=jf, later=later)
    return await session.record_verdict(
        conn,
        s,
        card_token=token,
        value=VERDICT_LABELS.index(choice),
        hp=hp,
        embeddings=embeddings,
        bundle_version=bundle_version,
        jf=jf,
        later=later,
    )


__all__ = ["ANSWERS", "REASON", "SOURCE", "answer", "live_verdict"]
