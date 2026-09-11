"""The Rate surface's routes. Spec v2.1 §6.1, §6.7, §7.3, §13, decision 35.

Thin by design: every rule this surface has lives in `spielplan.rate.session`, and the only
things decided here are the HTTP shapes.

Two of those shapes are load-bearing.

  * **A write names a `card_token`, never a title.** §6.1 puts the card in the server's hands
    (the prediction may not travel with it, the re-ask may not be visible in it, the block
    counter may not be the client's), and a route that accepted `{"title_id": 41, "value": 2}`
    would give all three back. A token that no longer matches is a 409 with a reason, which is
    also the double-tap guard.
  * **Every response is the same envelope, and it carries the next card.** §6 preamble:
    "<2 s per sweep card, <1.5 s per battle, undo everywhere, next card preloaded."

Exported as `router`; `app.py` registers it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from spielplan.api.artifacts import RESTART_REQUIRED, RESTORE_REQUIRED
from spielplan.api.deps import DB, ActiveUser
from spielplan.connectors import registry
from spielplan.ledger import hyperparams, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import EmbeddingSource
from spielplan.models import artifacts
from spielplan.rate import session

log = logging.getLogger("spielplan.api.rate")

router = APIRouter(prefix="/api/rate", tags=["rate"])

Head = Annotated[list[int], Field(default_factory=list)]


class ControlsBody(BaseModel):
    """§6.1's three controls. Every field is optional: the same route starts a session and
    changes one knob on an existing one."""

    mode: Literal["mix", "sweep", "battle"] | None = None
    kinds: list[Literal["movie", "series"]] | None = None
    decisive: bool | None = None
    restart: bool = False
    head: Head


class VerdictBody(BaseModel):
    card_token: str
    value: Literal[0, 1, 2]
    latency_ms: int | None = None
    head: Head


class CardBody(BaseModel):
    card_token: str
    latency_ms: int | None = None
    head: Head


class DuelBody(BaseModel):
    card_token: str
    outcome: Literal["A", "B", "TIE"]
    # One answer's override of the session's persistent toggle — §6.1's long-press accelerator.
    decisive: bool | None = None
    latency_ms: int | None = None
    head: Head


class CorrectionBody(BaseModel):
    card_token: str
    side: Literal["left", "both", "right"]


def _hyperparams(request: Request) -> Hyperparams:
    """§4.3's constants, from the active bundle when there is one.

    `app.state.hyperparams` is where the lifespan's one read lands (`app.py`), and that is the
    normal path: §10 makes a bundle swap a restart, so nothing in a running process can change
    these numbers and re-reading the file per tap bought nothing but a file read, a
    `from_mapping` validation and a discarded note list on every request.

    The fallback stays because tests construct the app without its lifespan, and because §3.1
    makes a bundle-less household legal — there the defaults are the honest answer. What it no
    longer does is let `from_mapping`'s `ValueError` out: a hand-edited or badly restored
    constant turned every Rate write and every Rank board into a 500 for everyone, with nothing
    on screen saying why. Refused with a 503 instead, and deliberately NOT defaulted: the
    defaults have a different `hp_digest`, so serving them would invalidate every cached fit in
    the install and re-fit the whole household behind a number nobody chose. [M4.10 finding 10]

    `OSError` beside `ValueError` because `hyperparams.load` no longer reads a constants file that
    is present and unopenable — a path that is a directory, a dangling symlink — as an absent one:
    the refusal this clause exists to render is the same refusal whether the file cannot be parsed
    or cannot be opened, and only a silent fall back to DEFAULTS would be worse than either.
    [M4.10 cycle 1, M410-R1-03 / M410-R1-06]
    """
    cached = getattr(request.app.state, "hyperparams", None)
    if cached is not None:
        return cached
    try:
        hp, notes = hyperparams.load(getattr(request.app.state, "artifacts", None))
    except (ValueError, OSError) as exc:
        log.error("ledger_hyperparams.json is unusable: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "ledger constants unreadable - see backend log"
        ) from exc
    for note in notes:
        log.debug("hyperparameters: %s", note)
    return hp


async def _assert_active_basis(request: Request, conn: asyncpg.Connection) -> None:
    """§10's invariant, on the request path: 409 rather than a fit in a basis nobody serves.

    "No process may score or refit with a loaded bundle version different from the active row."
    This process pins its store and its Backbone at boot (`app.py`), and §10's swap sequence ends
    in "restart backend and worker" precisely because nothing in a running process can re-pin
    them - so between the flip and the restart `app.state.backbone` is the OUTGOING basis while
    every `title_placement` row, every `user_vector` and every `ledger_fit` stamp the flip made
    visible is the incoming one. Left unguarded, the first tap per (user, kind) ran a full MAP fit
    over half-and-half coordinates and stamped it with the NEW version, which `load_cache` then
    trusted until the next nightly: measured ||v_tap - v_correct|| = 0.643 against
    ||v_correct|| = 0.782.

    There was no 409 to raise; this is where it is minted, and it carries
    `api/artifacts.py::RESTART_REQUIRED` verbatim so the sentence the import screen showed the
    admin is the sentence the refusal shows. 409 and not 503: the conflict is between two
    versions of the world, the state is recoverable by the restart the message names, and §3.1's
    genuinely bundle-less install is unaffected because None == None.

    Called at the TOP of every route that fits, before `_resume` and before any write. §6.1's
    anchoring rule and M4.10 finding 8 both forbid the other placement: a refusal raised after
    `record_verdict` has committed loses the tap and invites a retry that writes a second row.
    [M4.13, arch-03, data-01]

    AND THE SECOND ARM, WHICH THE VERSION COMPARISON CANNOT SEE. `load_active` carries a broken
    install's own version so its stamp is honest, and that is precisely what makes `assert_matches`
    PASS for a store that cannot produce one coordinate -- the trap
    `test_a_broken_store_carries_the_active_version_and_still_refuses_on_its_own_flag` exists to
    name. data-03 wired the refusal into the worker and nowhere else, so on a broken install every
    model job refused while these five routes kept fitting: `hyperparams.load` returns DEFAULTS for
    an empty store, its digest equals the stamped one whenever the bundle shipped no
    `ledger_hyperparams.json`, `load_cache` therefore accepted a fit computed in a REAL basis, and
    the tap rewrote `ledger_state` against `e = 0` -- measured on the fixture at s 1.5735/1.5699
    healthy against 0.8001/0.8001 broken, two rated titles collapsed onto one number. Refused here
    rather than queued, because §10's invariant is "score OR refit" and the reveal this route
    carries is a score; refused before the write, so the tap is declined and never lost; and
    `is_empty` still reads True, so §3.1's SURFACES keep rendering their no-bundle state, which is
    the line `models/artifacts.py::is_empty` draws. A different sentence from the swap's, because
    restarting this process would fix nothing: the files are gone.
    [M4.13 cycle 1, m413-c1-dim1-broken-bundle-refusal-is-worker-only]
    """
    store = getattr(request.app.state, "artifacts", None)
    if store is None:
        return
    try:
        store.assert_matches(await artifacts.active_bundle_version(conn))
    except RuntimeError as exc:
        log.error("refusing to score or refit: %s", exc)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "bundle_swapped", "message": RESTART_REQUIRED},
        ) from exc
    try:
        store.assert_not_broken()
    except RuntimeError as exc:
        log.error("refusing to score or refit: %s", exc)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "bundle_broken", "message": RESTORE_REQUIRED},
        ) from exc


def _basis(request: Request) -> Any:
    """Which bundle THIS PROCESS is fitting in — the other half of `_embeddings`, stated.

    `_assert_active_basis` above compares it to the active row at the top of the route; this hands
    the same version to the fit, which re-asks after it has taken the board lock. The two are not
    one check: a tap that waits out an import passed the first and would fail the second, and the
    seconds between them are exactly when §10's flip becomes visible. `refit.BASIS_UNSTATED` when
    this process has no store at all, which is not §3.1's bundle-less install (`None`) but "there
    is nothing here to compare" — the state a test app or a boot without artifacts is in.
    [M4.13 cycle 1, finding 15]
    """
    store = getattr(request.app.state, "artifacts", None)
    return refit.BASIS_UNSTATED if store is None else store.version


def _embeddings(request: Request, conn: asyncpg.Connection) -> EmbeddingSource:
    """§5.1's coordinate source: the warm Backbone row first, the Cold Tower placement second.

    One definition, in `observations.standard_embeddings`, because the nightly job and §10's
    rebuild both got this wrong by passing the placement source alone.

    The version travels with the two sources it describes: `app.state.artifacts.version` is the
    bundle whose Backbone `app.state.backbone` holds, so the `title_placement` rows read for the
    cold half are the same bundle's and the `ledger_fit` stamp this source's fit earns says so.
    `_assert_active_basis` has already refused the case where that version is not the active row.
    [M4.13, data-01]
    """
    store = getattr(request.app.state, "artifacts", None)
    return observations.standard_embeddings(
        conn,
        getattr(request.app.state, "backbone", None),
        bundle_version=None if store is None else store.version,
    )


async def _jellyfin(conn: asyncpg.Connection) -> session.Jellyfin:
    cfg = await registry.load_jellyfin(conn)
    return session.Jellyfin(client=registry.make_client(cfg), cfg=cfg)


def _stale(exc: session.StaleCard) -> HTTPException:
    """§6.1's card is the server's. Refusing an answer to a card that is no longer on the table
    is what makes that true over HTTP — and it is the double-tap guard."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason": exc.reason,
            "message": {
                "no_card": "there is no card on the table",
                "stale_card": "that card has already been answered",
                "wrong_card_type": "that answer does not fit the card on the table",
            }.get(exc.reason, "the card token is not current"),
        },
    )


async def _resume(conn: asyncpg.Connection, user_id: int) -> session.RateSession:
    return await session.open_or_resume(conn, user_id=user_id)


@router.get("")
async def current(
    conn: DB,
    user: ActiveUser,
    head: list[int] = Query(
        default=[],
        description="§7.3/§6.0: title ids the pending-verdicts banner pinned to the front.",
    ),
) -> dict[str, Any]:
    """Open or resume, then serve the card. Idempotent: a second GET returns the same card
    under the same token, because a card that redrew under a refresh would make every "next
    card preloaded" promise in §6 a lie."""
    s = await _resume(conn, user.id)
    s = await session.ensure_card(conn, s, head=head)
    return await session.payload(conn, s, user=user)


@router.post("/session")
async def controls(
    body: ControlsBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """§6.1's mode and kind controls, plus the persistent decisive toggle.

    A fresh session opens in Mix — §6.1 makes it the default, and every entry point into the
    surface lands on the same card type as a result.
    """
    try:
        s = await session.open_or_resume(
            conn, user_id=user.id, kinds=body.kinds, restart=body.restart
        )
        if body.mode is not None or body.kinds is not None or body.decisive is not None:
            s = await session.set_controls(
                conn, s, mode=body.mode, kinds=body.kinds, decisive=body.decisive
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    s = await session.ensure_card(conn, s, head=body.head)
    return await session.payload(conn, s, user=user)


@router.delete("/session")
async def end(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """Close the live session. The journal stays: §4.2 is append-only and the rows are the
    record of what the person actually said."""
    return {"ended": await session.end_session(conn, user_id=user.id)}


@router.post("/verdict")
async def verdict(
    body: VerdictBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """§6.1: `Liked / Fine / Disliked`. Verdict implies `seen`.

    The reveal rides on this response and on no other, which is §6.1's anchoring rule
    (Cosley 2003) expressed as a route: the card carried no belief, the answer to the card
    carries it.
    """
    await _assert_active_basis(request, conn)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_verdict(
            conn,
            s,
            card_token=body.card_token,
            value=body.value,
            hp=_hyperparams(request),
            embeddings=_embeddings(request, conn),
            bundle_version=_basis(request),
            jf=await _jellyfin(conn),
            latency_ms=body.latency_ms,
            head=body.head,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, reveal=outcome.reveal, log=outcome.log,
        ledger=outcome.ledger, event_kind="verdict", user=user,
    )


@router.post("/not-seen")
async def not_seen(body: CardBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§6.1's one seen-state control. Owner decision 2026-08-29: a title you cannot remember is
    plain `unseen`, and the verdict and duel rows survive the flip (§4.2)."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_not_seen(
            conn,
            s,
            card_token=body.card_token,
            jf=await _jellyfin(conn),
            latency_ms=body.latency_ms,
            head=body.head,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, event_kind="not_seen", user=user
    )


@router.post("/skip")
async def skip(body: CardBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_skip(
            conn, s, card_token=body.card_token, latency_ms=body.latency_ms, head=body.head
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(conn, outcome.session, log=outcome.log, user=user)


@router.post("/duel")
async def duel(body: DuelBody, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """§6.1's battle answer, `Tie` included — one duel row, never a dropped one."""
    await _assert_active_basis(request, conn)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_duel(
            conn,
            s,
            card_token=body.card_token,
            outcome=body.outcome,
            decisive=body.decisive,
            hp=_hyperparams(request),
            embeddings=_embeddings(request, conn),
            bundle_version=_basis(request),
            latency_ms=body.latency_ms,
            head=body.head,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, ledger=outcome.ledger, event_kind="duel", user=user
    )


@router.post("/correction")
async def correction(body: CorrectionBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§6.1's corrections row. Writes no duel row, does not advance the counter, syncs §7.3."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_correction(
            conn, s, card_token=body.card_token, side=body.side, jf=await _jellyfin(conn)
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, event_kind="not_seen", user=user
    )


@router.post("/undo")
async def undo(conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """Decision 35. Refused at the block boundary with a reason, never silently no-opped — the
    chip has to be able to disable visibly, and `GET /api/rate` carries the same
    `undo.available` flag so it can do that before the tap."""
    await _assert_active_basis(request, conn)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.undo(
            conn,
            s,
            hp=_hyperparams(request),
            embeddings=_embeddings(request, conn),
            bundle_version=_basis(request),
            jf=await _jellyfin(conn),
        )
    except session.UndoUnavailable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": exc.reason,
                "message": {
                    "empty": "nothing to undo in this block",
                    "block_boundary": (
                        "undo reaches back to the start of this block of 15 and no further"
                    ),
                }[exc.reason],
            },
        ) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, ledger=outcome.ledger, event_kind="undo", user=user
    )


@router.get("/balance")
async def class_balance(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§5.2's running class balance on its own, for the widget's own poll.

    Not partitioned by kind: §4.1 rule 5 binds surfaces that *rank*, and this one ranks
    nothing — it describes a labelling habit, and the 5x lever is about the labeller.
    """
    s = await _resume(conn, user.id)
    return (await session.payload(conn, s, user=user))["class_balance"]
