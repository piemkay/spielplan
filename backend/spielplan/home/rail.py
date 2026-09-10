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

# Declared, coloured by `ModelRail.svelte`, and produced by nobody — on purpose, and named here
# so "nobody produces it" is a recorded state rather than something a reader has to discover by
# grepping. All five are WORKER-side writes: the nightly MAP refit, the incremental refit the
# worker runs, the fold-in, the blend-weight fit and the Cold Tower placement sweep. §6.7 says
# the log is "never persisted", so it is an in-process ring buffer (see the module docstring) —
# and an event recorded in the worker process therefore reaches no web request's rail. Narrating
# them would take a cross-process channel this milestone does not build, and deleting them would
# throw away the colour rules and the renderers (`refit_line`, `placement_line`) that the
# milestone which does build it will need. So they stay declared, and this tuple is the thing a
# guard can read. [decision 189, M4.9 finding 24]
#
# `bundle_swap` and `reconcile` are deliberately NOT here: both are written inside the WEB
# process, at `importer/bundle.py`'s hot swap and its in-request rebuild sweep, so they reach a
# rail today and are held to the "has a producer" half of the guard.
AWAITING_PRODUCER: tuple[str, ...] = (
    "ledger_refit",
    "ledger_incremental",
    "foldin",
    "blend_weight",
    "placement",
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

# A tier label is a choice and is REFUSED; a display name is data and is ELIDED. Both bounds
# guard the same MAX_LINE, and `rank/tiers.py:56-62` already records what happens when neither
# does: "a long enough label turned every drop into that tier into a 500 with the observation
# already durable, and each retry wrote another". The same hole reaches here by the other door —
# `title.name` is free text out of the bundle, so there is no person to refuse, and both Rank
# routes compose their line AFTER `record_duel` / `drop` has committed (`api/rank.py`,
# `rank/drop.py`). A renderer that can refuse is therefore a route that can 500 over a durable
# row, which is finding 8's third raise site. So every renderer whose line a committed write
# composes shortens the names it interpolates, and `record` keeps its refusal for the two lines
# that are programming errors rather than data: an empty line and an unknown kind.
# [M4.10 finding 8]
#
# THREE renderers and not two, which is what this comment first claimed. The rule was written for
# the Rank pair, because finding 8's three raise sites are all on the Rank routes — and
# `verdict_line`, §6.7's commonest line and the only one on the surface this milestone is named
# for, interpolates two names and elided neither. `rate/session.py`'s `payload` records it after
# `record_verdict`'s transaction has committed, so the same door stood open on Rate: at the 64
# characters `AccountName` allows, 431 characters for a 300-character title — below the 300 the
# exit criterion tests Rank with. `session_answer_line` and `placement_line` interpolate a name and
# are deliberately NOT elided: Tonight's passes `str(seat["id"])` (`api/tonight.py`), a bigint, and
# `placement` has no producer at all (`AWAITING_PRODUCER`), so neither has an over-long input to
# shorten — and the milestone that gives placement a producer inherits this paragraph rather than a
# silent habit. [M4.10 cycle 1, M410-R1-01]
#
# 120, because the longest chrome any renderer can compose around its names is 95 characters and
# every piece of it is bounded elsewhere — `duel(a vs b) = TIE → Davidson arm, profile_battle
# · uniform-random, held out` is 74 with outcome and context bounded by 0005's CHECKs and the arm
# by `ARM_PHRASES`; the tier edit's 95 assumes `tiers.MAX_LABEL`; the verdict's 71 assumes
# `VERDICT_LABELS`' longest word and a six-figure millisecond count, each further digit costing
# one character against an 89-character margin. So 2 × 120 + 74 = 314, 120 + 95 = 215 and
# 2 × 120 + 71 = 311, all inside MAX_LINE with room for a renderer that grows a clause. The
# arithmetic is pinned by a test rather than trusted.
MAX_NAME_IN_LINE = 120


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

    THE CAP IS APPLIED BEFORE THE MERGE, and that is the whole of finding 26. Two deques of
    `RAIL_LIMIT` were concatenated and only then sliced, so `?limit=50` answered with up to
    thirty events on a surface whose spec sentence is "last ~15" — the buffer was bounded and
    the response was not. `keep` bounds both reads and the result, so no argument reaches past
    §6.7's number and the route's `le=RAIL_LIMIT` refuses at the edge rather than depending on
    this function to truncate. Taking the newest `keep` of each deque first is not an
    optimisation with a different answer: an event in the final slice is among the newest `keep`
    of the deque it came from, because ids increase with time in one process-wide counter.
    """
    keep = min(max(int(limit), 0), RAIL_LIMIT)
    if keep == 0:
        return []
    with _LOCK:
        mine = list(_BUFFERS.get(user_id, ()))[-keep:]
        ours = list(_BUFFERS.get(HOUSEHOLD, ()))[-keep:]
    merged = sorted(mine + ours, key=lambda e: e["id"], reverse=True)
    return [dict(e) for e in merged[:keep]]


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


def _elide(name: str, limit: int = MAX_NAME_IN_LINE) -> str:
    """One display name, shortened to `limit` characters with the marker inside the budget.

    `…` rather than `...` because the rail is UI copy read in a browser and that is the register
    the app's own surfaces already use (`BundleImport.svelte:57`, `ModelRail.svelte:75`); the
    string it lands in carries `→` and `·` anyway. Nothing here reaches a console log.
    """
    if len(name) <= limit:
        return name
    return name[: limit - 1].rstrip() + "…"


def verdict_line(user_name: str, title_name: str, label: str, *, refit_ms: float | None = None) -> str:
    """`verdict(jenny, Heat) = liked → ordered-logit arm, incremental refit 31 ms` (§6.7).

    Total on BOTH names, for the reason the comment above `MAX_NAME_IN_LINE` gives: the caller is
    `rate/session.py`'s `payload`, which records this line after the verdict and its journal row
    are durable, so a refusal here is a 500 over an append-only row the person cannot retry —
    `_claim_card` has already nulled the card token, so the retry is a 409 saying the card was
    answered. Neither name is a choice somebody made: the title's is bundle free text and the
    member's is bounded only by `AccountName`'s 64. [M4.10 finding 8, cycle 1 M410-R1-01]
    """
    tail = "ordered-logit arm"
    if refit_ms is not None:
        tail += f", incremental refit {refit_ms:.0f} ms"
    return f"verdict({_elide(user_name)}, {_elide(title_name)}) = {label} → {tail}"


def tier_edit_line(title_name: str, tier: str, *, via: str, neighbour_duels: int = 0) -> str:
    """`tier_edit(Drive → A, via=drag_drop) + 2 margin-less duels vs new neighbours` (§6.7).

    §6.3: dropping a title *between* two titles emits the edit plus two margin-less duels, and
    the rail is where "drag-and-drop is data, not override" becomes legible.

    Total on `title_name`: the produced line is at most `MAX_LINE`, so `rank/drop.py`'s caller
    cannot be handed a line `record` will refuse after the edit has committed.
    """
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

    The **arm is not a constant in this string**. The prototype's tier-queue handler pushed
    "boundary-targeted pair (70/20/10 policy)" unconditionally, so on every tenth pair the log
    asserted boundary-targeting about the one stream §13 forbids selecting adaptively — a log
    that misreports the evaluation stream defeats the guard it is supposed to make legible
    (proposal 120, proposal 146). `ARM_PHRASES` is exhaustive over `duel.selection`'s CHECK, so
    a new arm added to the column without a phrase here fails loudly rather than rendering as
    whatever the previous branch happened to say.

    An unknown arm is still a refusal and a long pair of names is not: the arm is this
    process's own vocabulary while the names are bundle data, and this line is composed after
    the duel row is durable (`api/rank.py`). Total on `a` and `b`: the produced line is at
    most `MAX_LINE`.
    """
    if selection not in ARM_PHRASES:
        raise RailError(f"unknown selection arm {selection!r} — add it to ARM_PHRASES")
    return (
        f"duel({_elide(a)} vs {_elide(b)}) = {outcome} → Davidson arm, "
        f"{context} · {ARM_PHRASES[selection]}"
    )


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
