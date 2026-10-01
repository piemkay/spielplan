"""Not seen from the title card (decision 487), written through `rate.session` as a sweep answer.

No second observation writer (decision 212): the title is stashed as a sweep card and answered.
"""

from __future__ import annotations

import asyncpg

from spielplan.ledger import observations
from spielplan.rate import session

# Undo restores the journal's card verbatim, so this is the reason shown after an undo.
REASON = "You rated it from its title card."
SOURCE = "title_card"


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


async def not_seen(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    jf: session.Jellyfin | None = None,
    later: session.Later | None = None,
) -> session.Outcome:
    """Mark `title_id` not seen from its card.

    Raises `LookupError` (no such title), `session.StaleCard` (409).
    """
    s = await session.open_or_resume(conn, user_id=user_id)
    s = await _put_on_table(conn, s, title_id=title_id)
    return await session.record_not_seen(conn, s, card_token=str(s.card_token), jf=jf, later=later)


__all__ = ["REASON", "SOURCE", "not_seen"]
