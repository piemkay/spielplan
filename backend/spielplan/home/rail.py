"""§6.7's transparency rail (an in-process ring buffer, never persisted) and decision 117's gate.

The gate is a DELETION: `redact()` removes gated keys from the payload, so hidden numbers never reach
the wire. Events recorded in the worker process reach no web request's rail.
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

# Declared kinds with no `rail.record` call site: all are worker-side writes, which cannot reach a
# web process's buffer (decision 189).
AWAITING_PRODUCER: tuple[str, ...] = (
    "ledger_refit",
    "ledger_incremental",
    "foldin",
    "blend_weight",
    "placement",
    "reconcile",
    "bundle_swap",
)

# Decision 117's inventory, the only thing `redact` knows about. `reveal` is deliberately absent:
# it is the product; `rate/session.viewer_reveal` strips its numbers (decisions 486, 491).
GATED_KEYS: tuple[str, ...] = ("model", "rail", "suppressed", "log", "ledger", "why_numbers")

MAX_LINE = 400  # Enforced here so a caller learns at the write rather than at the render.

# Names are bundle data and are ELIDED, never refused: these lines are composed after a write has
# committed, so a refusal would 500 over a durable row. Two names plus the longest chrome (95
# characters) stay inside MAX_LINE.
MAX_NAME_IN_LINE = 120


class RailError(ValueError):
    """A line this journal will not accept."""


# --- the gate ----------------------------------------------------------------------------------


def visible_to(user: Any) -> bool:
    """Decision 117's one question. Takes the session user so nothing else can open the rail."""
    return bool(getattr(user, "show_model", False))


def redact(payload: Any, *, show_model: bool) -> Any:
    """Return `payload` with every decision-117 key removed, recursively, when the toggle is off."""
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


# One bounded deque per user plus one for the household. Locked because `recent` reads two and merges.
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
    """Append one narrated model write, rendered by the caller at write time. Returns its sequence.

    `user_id` None is a household event.
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
    """This person's and the household's last events, newest first, never more than RAIL_LIMIT."""
    keep = min(max(int(limit), 0), RAIL_LIMIT)
    if keep == 0:
        return []
    with _LOCK:
        mine = list(_BUFFERS.get(user_id, ()))[-keep:]
        ours = list(_BUFFERS.get(HOUSEHOLD, ()))[-keep:]
    merged = sorted(mine + ours, key=lambda e: e["id"], reverse=True)
    return [dict(e) for e in merged[:keep]]


def forget(*, user_id: int | None = None) -> int:
    """Drop the buffer. Returns how many events went."""
    with _LOCK:
        if user_id is None:
            gone = sum(len(b) for b in _BUFFERS.values())
            _BUFFERS.clear()
            return gone
        buf = _BUFFERS.pop(user_id, None)
        return len(buf) if buf else 0


# --- the four line shapes §6.7 names --------------------------------------------------------


def _elide(name: str, limit: int = MAX_NAME_IN_LINE) -> str:
    """One display name, shortened to `limit` characters with the marker inside the budget."""
    if len(name) <= limit:
        return name
    return name[: limit - 1].rstrip() + "…"


def verdict_line(user_name: str, title_name: str, label: str, *, refit_ms: float | None = None) -> str:
    """`verdict(jenny, Heat) = liked → ordered-logit arm, incremental refit 31 ms` (§6.7)."""
    tail = "ordered-logit arm"
    if refit_ms is not None:
        tail += f", incremental refit {refit_ms:.0f} ms"
    return f"verdict({_elide(user_name)}, {_elide(title_name)}) = {label} → {tail}"


def tier_edit_line(title_name: str, tier: str, *, via: str, neighbour_duels: int = 0) -> str:
    """`tier_edit(Drive → A, via=drag_drop) + 2 margin-less duels vs new neighbours` (§6.7)."""
    line = f"tier_edit({_elide(title_name)} → {tier}, via={via})"
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

    Names the arm that actually drew (proposal 120); an unknown arm is refused.
    """
    if selection not in ARM_PHRASES:
        raise RailError(f"unknown selection arm {selection!r} — add it to ARM_PHRASES")
    return (
        f"duel({_elide(a)} vs {_elide(b)}) = {outcome} → Davidson arm, "
        f"{context} · {ARM_PHRASES[selection]}"
    )


def session_answer_line(participant: str, pair: int, answer: str) -> str:
    """`session_answer(p, pair 4) = A — pool-centred tilt` (§6.7, §6.2 step 4's centring lever)."""
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
    """The kind chips for the rail's filter row: only kinds actually present."""
    return sorted({str(e["kind"]) for e in events})
