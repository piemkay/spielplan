"""§6.7's transparency rail, and decision 117's single gate.

Spec v2.1 §6.7, §6.0, §6.8, decision 117.

§6.7: "A per-user toggle (default off) reveals an ephemeral log (last ~15 events, never
persisted) narrating **every model write** in one human-readable line … It is 'drag-and-drop is
data, not override' made visible, and the primary M2 debugging instrument."

Decision 117: "**One, global per user, in the account dropdown, default off.** A debugging
instrument reached often and briefly. It governs the rail and every inline annotation; the
title card's model line stays ungated."

THE GATE IS A DELETION, NOT A CLASS. `redact()` removes the gated keys from the payload rather
than marking them hidden, because "hidden by CSS" makes the promise cosmetic: the numbers would
still be on the wire, in the browser's network tab, in the service-worker cache, and in
anything that logs a response. One function does the removal for the whole payload so no route
can gate three of four annotations and forget the fourth — and one test can walk the redacted
payload for every forbidden key rather than enumerating call sites.

WHAT IS *NOT* GATED, and why each survives:

* the **title card's model line** (`b(t) · β · gate`) — proposal 19 and decision 117 both say
  so in as many words; it is §6.0's M0 transparency promise and predates this toggle. It is
  served by `scoring.serve.model_line` on the title route, which this module never touches.
* the **shelf why-line**, including the β it prints. §6.0's own table gives shelf 2's why as
  "clean item prior + your fold-in, blended at β 0.8" — the number is the mandated copy of a
  shelf that must be able to say why it exists, not an annotation about this viewer.
* the **tier badge** on a shelf card. Proposal 29 makes rank + seen dot + tier the shelf card's
  chrome and §6.3's tier vocabulary "ambient on Home". The letter is chrome; the score, σ and
  CDF behind it are the annotation, and only those are removed.

ON "NEVER PERSISTED". §6.7 says "an ephemeral log (last ~15 events, **never persisted**)", and
that is implemented literally: an in-process ring buffer, `RAIL_LIMIT` entries per user, gone
when the process restarts. Nothing here touches the database.

This module first shipped writing to a `model_event` table, on the argument that a nightly MAP
refit and a Cold Tower placement are model writes with no row of their own and a rail derived
from the observation tables would omit exactly what a person turns the rail on to see. That
argument is sound and it is not the spec's. "Never persisted" is a normative sentence about a
debugging instrument, and it decides the question: the rail narrates what the model just did in
front of you, which is why §6.7's own four examples are all interactive writes. The cost is
real and worth naming — an event recorded in the worker process never reaches the web process's
buffer, so a nightly refit narrates itself to nobody. §6.7 is about the interactive surface and
calls the rail "the primary M2 debugging instrument"; a durable audit log of model writes would
be a different feature, and the spec does not ask for one.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

# §6.7: "an ephemeral log (last ~15 events, never persisted)".
RAIL_LIMIT = 15

# A closed set, so a typo in a caller is a loud error rather than a line nobody can filter on.
EVENT_KINDS: tuple[str, ...] = (
    "verdict",
    "duel",
    "tier_edit",
    # §6.7's fourth worked example, `session_answer(p, pair 4) = A — pool-centred tilt`. The
    # renderer below has existed since M2; M4 is the milestone that produces the write.
    "session_answer",
    "not_seen",
    "undo",
    "ledger_refit",
    "ledger_incremental",
    "foldin",
    "blend_weight",
    "placement",
    "reconcile",
    "bundle_swap",
)

# Decision 117's inventory, and the only thing `redact` knows about. `model` is the per-card
# annotation block; `rail` is §6.7's log; `suppressed` is the shelf-by-shelf account of what did
# not ship and why, which is a debugging instrument by the same argument.
# Decision 117 governs "the event rail **and** every inline numeric annotation". `log` and
# `ledger` are §6.1's half of that: the rate surface's own §6.7 lines and the incremental-refit
# delta. `reveal` is deliberately absent — §6.1 requires the predicted class and its data-voice
# score *after the tap*, which is the product rather than the debugging.
GATED_KEYS: tuple[str, ...] = ("model", "rail", "suppressed", "log", "ledger")

MAX_LINE = 400  # Enforced here so a caller learns at the write rather than at the render.


class RailError(ValueError):
    """A line this journal will not accept."""


# --- the gate ----------------------------------------------------------------------------------


def visible_to(user: Any) -> bool:
    """Decision 117's one question, asked in one place.

    Takes the session user rather than a bare flag so a route cannot accidentally consult a
    request parameter, a role, or a query string: the toggle is a per-user preference and
    nothing else may open the rail.
    """
    return bool(getattr(user, "show_model", False))


def redact(payload: Any, *, show_model: bool) -> Any:
    """Return `payload` with every decision-117 key removed when the toggle is off.

    Recursive and key-based rather than schema-aware on purpose: a shelf builder that adds a
    seventh annotation under `model` inherits the gate, and a builder that invents a new
    top-level numeric block does not — which is why the test walks the result for forbidden
    keys instead of trusting this function's list.
    """
    if show_model:
        return payload
    if isinstance(payload, dict):
        return {
            key: redact(value, show_model=False)
            for key, value in payload.items()
            if key not in GATED_KEYS
        }
    if isinstance(payload, list):
        return [redact(item, show_model=False) for item in payload]
    return payload


# --- the journal -------------------------------------------------------------------------------


# §6.7: "an **ephemeral** log … never persisted". One bounded deque per user plus one for the
# household, so a noisy account cannot push another account's events out of its own rail, and
# the whole structure is bounded by RAIL_LIMIT × (members + 1) entries.
#
# Guarded by a lock because uvicorn serves concurrent requests on one loop and `record` is
# called from inside request handlers; a deque append is atomic under the GIL but the read in
# `recent` walks two of them and merges.
_LOCK = threading.Lock()
_BUFFERS: dict[int | None, deque[dict[str, Any]]] = {}
_SEQ = itertools.count(1)

HOUSEHOLD = None


def record(
    *,
    kind: str,
    line: str,
    user_id: int | None = None,
    title_id: int | None = None,
    detail: dict[str, Any] | None = None,
    bundle_version: str | None = None,
    at: datetime | None = None,
) -> int:
    """Append one narrated model write. Returns its sequence number.

    `line` is rendered by the caller, at write time, so the rail shows what the model believed
    when it acted rather than a sentence recomposed later from numbers that have since moved.

    `user_id` is optional because a nightly `refit_all` or a placement sweep belongs to the
    household rather than to a person; decision 117 scopes the *toggle* per user, not the
    events.
    """
    if kind not in EVENT_KINDS:
        raise RailError(f"unknown model-event kind {kind!r}; one of {EVENT_KINDS}")
    text = line.strip()
    if not text or len(text) > MAX_LINE:
        raise RailError(f"a rail line must be 1..{MAX_LINE} characters, got {len(text)}")

    with _LOCK:
        seq = next(_SEQ)
        buf = _BUFFERS.get(user_id)
        if buf is None:
            buf = _BUFFERS[user_id] = deque(maxlen=RAIL_LIMIT)
        buf.append(
            {
                "id": seq,
                "at": at or datetime.now(UTC),
                "kind": kind,
                "text": text,
                "title_id": title_id,
                "detail": detail or {},
                "bundle": bundle_version,
                "scope": "household" if user_id is None else "you",
            }
        )
    return seq


def recent(*, user_id: int, limit: int = RAIL_LIMIT) -> list[dict[str, Any]]:
    """The last ~15 events this person is entitled to see, newest first.

    Household-wide events are included because the nightly refit and the bundle swap are the
    writes that explain a Home page changing overnight; another *person's* events are not,
    because §6.7's rail narrates this user's model and decision 117 turns the toggle on for one
    account only.
    """
    with _LOCK:
        mine = list(_BUFFERS.get(user_id, ()))
        ours = list(_BUFFERS.get(HOUSEHOLD, ()))
    merged = sorted(mine + ours, key=lambda e: e["id"], reverse=True)
    return [dict(e) for e in merged[:limit]]


def forget(*, user_id: int | None = None) -> int:
    """Drop the buffer. Returns how many events went.

    A process restart does this anyway — §6.7's "never persisted" is the guarantee, and this is
    the same guarantee offered as an action rather than as a consequence.
    """
    with _LOCK:
        if user_id is None:
            gone = sum(len(b) for b in _BUFFERS.values())
            _BUFFERS.clear()
            return gone
        buf = _BUFFERS.pop(user_id, None)
        return len(buf) if buf else 0


# --- the four line shapes §6.7 names --------------------------------------------------------


def verdict_line(user_name: str, title_name: str, label: str, *, refit_ms: float | None = None) -> str:
    """`verdict(jenny, Heat) = liked → ordered-logit arm, incremental refit 31 ms` (§6.7)."""
    tail = "ordered-logit arm"
    if refit_ms is not None:
        tail += f", incremental refit {refit_ms:.0f} ms"
    return f"verdict({user_name}, {title_name}) = {label} → {tail}"


def tier_edit_line(title_name: str, tier: str, *, via: str, neighbour_duels: int = 0) -> str:
    """`tier_edit(Drive → A, via=drag_drop) + 2 margin-less duels vs new neighbours` (§6.7).

    §6.3: dropping a title *between* two titles emits the edit plus two margin-less duels, and
    the rail is where "drag-and-drop is data, not override" becomes legible.
    """
    line = f"tier_edit({title_name} → {tier}, via={via})"
    if neighbour_duels:
        line += f" + {neighbour_duels} margin-less duels vs new neighbours"
    return line


ARM_PHRASES: dict[str, str] = {
    "boundary": "boundary-targeted",
    "exploration": "exploration",
    "uniform_holdout": "uniform-random, held out",
    "random": "random",
}


def duel_line(a: str, b: str, outcome: str, *, context: str, selection: str) -> str:
    """`duel(Heat vs Drive) = A → Davidson arm, tier_queue · boundary-targeted` (§6.7, §6.3).

    The **arm is not a constant in this string**. The prototype's tier-queue handler pushed
    "boundary-targeted pair (70/20/10 policy)" unconditionally, so on every tenth pair the log
    asserted boundary-targeting about the one stream §13 forbids selecting adaptively — a log
    that misreports the evaluation stream defeats the guard it is supposed to make legible
    (proposal 120, proposal 146). `ARM_PHRASES` is exhaustive over `duel.selection`'s CHECK, so
    a new arm added to the column without a phrase here fails loudly rather than rendering as
    whatever the previous branch happened to say.
    """
    if selection not in ARM_PHRASES:
        raise RailError(f"unknown selection arm {selection!r} — add it to ARM_PHRASES")
    return f"duel({a} vs {b}) = {outcome} → Davidson arm, {context} · {ARM_PHRASES[selection]}"


def session_answer_line(participant: str, pair: int, answer: str) -> str:
    """`session_answer(p, pair 4) = A — pool-centred tilt` (§6.7, §6.2 step 5's centring lever)."""
    return f"session_answer({participant}, pair {pair}) = {answer} — pool-centred tilt"


def parse_line(predicate: str, survivors: int) -> str:
    """`parse → predicate has(robots) · 0 survivors → flywheel` (§6.7, §6.4, §8.4)."""
    tail = " → flywheel" if survivors == 0 else ""
    return f"parse → predicate {predicate} · {survivors} survivors{tail}"


def refit_line(kind: str, *, n_titles: int, seconds: float, rho: float | None = None) -> str:
    """The nightly MAP refit — a model write with no observation row of its own (0012)."""
    line = f"ledger_refit({kind}) = {n_titles} titles, {seconds:.2f} s"
    if rho is not None:
        line += f", ρ {rho:.3f}"
    return line


def placement_line(title_name: str, *, b_hat: float, gate: float) -> str:
    """§8 stage 10: a Cold Tower placement, in the data voice §6.8 requires."""
    return f"placement({title_name}) = cold_tower · b̂ {b_hat:.2f} · gate {gate:.2f}"


def kinds_present(events: Iterable[dict[str, Any]]) -> list[str]:
    """The kind chips the rail's filter row is built from — only kinds actually present, so the
    filter never offers an empty bucket."""
    return sorted({str(e["kind"]) for e in events})
