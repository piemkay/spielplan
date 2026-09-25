"""The title card's own answer to a title. Spec v2.1 §6.0, §6.1, §7.3; decisions 35, 212, 487.

§6.0 gave the card two actions and no way to say what the person thought of the film in front
of them, so the 2026-09-25 user test found that the only road from "I know this one" to a
verdict was Mark seen, then waiting for the title to come round in Rate behind whatever card was
parked there. Decision 487 puts §6.1's four sweep answers - Liked / Fine / Disliked / Not seen -
on the card itself.

Decision 212 refused exactly this shape once, for the finish prompt, and its reason is the design
constraint here rather than an obstacle: inline chips "would put a second observation writer on
Home with no rate session, no card token and no §6.1 block counter". So there is no second
writer. The answer goes through `rate.session` like every other sweep answer: the title is put
on the person's own table as a sweep card under a fresh token, and that token is answered with
`session.record_verdict` / `session.record_not_seen`. What that buys is everything §6.1 and
decision 35 attach to a sweep answer - the journal row Undo reverses, the block counter that
bounds it, the §7.3 push after the commit, the incremental refit, the §6.7 rail line, and the
prediction reveal computed strictly before the write - with none of it restated here.

What the title card does NOT go through is the queue. `rate.queue` decides what to ASK, and it
rightly never re-asks a rated title, a title answered "not seen" this sitting, or a title outside
the session's kinds; a person who taps a verdict on a card has already chosen the question. So the
card is built for the title directly, and a verdict on a rated title supersedes the earlier one,
which is §4.2's rule for every re-rating (`observations.record_verdict`).
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

# The sweep card's own "queued because:" line, for the one place a title-card card is ever read
# back: Undo restores the journal's card verbatim (decision 35), so after an undo on Rate this is
# the reason that card shows. Plain words, because it is member copy (decision 486).
REASON = "queued because: you rated it from its title card"
SOURCE = "title_card"


async def live_verdict(
    conn: asyncpg.Connection, *, user_id: int, title_id: int
) -> dict[str, Any] | None:
    """The person's current verdict on one title, read through the one definition of it.

    `LIVE_LABEL` rather than `superseded_by IS NULL`, for the reason `rate/__init__.py` gives: after
    §13's silent re-ask the only un-superseded row is the re-ask, and the card must show the
    person's real answer, not the instrument's.
    """
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
    """Replace whatever card is standing with a sweep card for `title_id`, under a fresh token.

    Under the session row's lock, because this replaces a card another device may be holding and
    `session._claim_card` takes the same lock as the first statement of every answer: a tap that
    is mid-transaction on the old card finishes first, and its device's next tap on the old token
    is then §6.1's ordinary stale-card 409. A drawn card that is thrown away costs nothing durable,
    so the card this replaces is simply drawn again later.
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
        await conn.execute("SELECT 1 FROM rate_session WHERE id = $1 FOR UPDATE", s.id)
        return await session.stash_card(conn, s, card)


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
) -> session.Outcome:
    """Answer `title_id` from its card: one of `ANSWERS`, written as §6.1's sweep answer.

    Raises `LookupError` for a title that does not exist and `ValueError` for an answer outside
    `ANSWERS`; `session.StaleCard` when another tap replaced the card between the stash and the
    answer, which the route turns into §6.1's 409 like any other stale token.
    """
    if choice not in ANSWERS:
        raise ValueError(f"answer must be one of {ANSWERS}, not {choice!r}")
    s = await session.open_or_resume(conn, user_id=user_id)
    s = await _put_on_table(conn, s, title_id=title_id)
    token = str(s.card_token)
    if choice == "not_seen":
        return await session.record_not_seen(conn, s, card_token=token, jf=jf)
    return await session.record_verdict(
        conn,
        s,
        card_token=token,
        value=VERDICT_LABELS.index(choice),
        hp=hp,
        embeddings=embeddings,
        bundle_version=bundle_version,
        jf=jf,
    )


__all__ = ["ANSWERS", "REASON", "SOURCE", "answer", "live_verdict"]
