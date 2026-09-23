"""§8 stage 2's handler registry: how a source adapter declares itself, and what it answers with.

Spec v2.1 §8 stage 2 (`spec:365-368`) and §8's preamble (`spec:359`); decisions 334, 372, 373,
374.

PORT VERDICT: **ported with named changes** from `mdc/sources/base.py` (129 lines). Taken in
substance: `HandlerSpec` (`:42-54`) with every one of its eight fields, the `@handler(...)`
decorator (`:60-71`) and the module-level `REGISTRY` it writes (`:57`), `paid_kinds` (`:74-75`),
`available_kinds` (`:78-98`) and `json_get` (`:117-129`). What is being ported is a property
rather than a hundred lines of code: every adapter declares itself HERE, so the stage-2 driver
selects by kind and never names a module - adding a source is adding a file, and the set of
kinds this registry holds is what makes "every one of stage 2's sources went through the polite
fetcher" a claim something can count (decision 374).

`paid` is carried although all eight of §8 stage 2's sources are free, because
`mdc/sources/base.py:51-53` says what it is for: "at corpus scale the difference between them is
roughly a hundred euros a click". §8 stage 6's LLM extraction is M5.5's and is the billed kind
the flag was written for, and a flag added after the first billed handler ships is a flag added
after the mistake it exists to prevent.

FOUR THINGS ARE DELIBERATELY NOT TAKEN, each because this app already answers it somewhere:

  * `Ctx` (`:23-36`). M5.1 ported it as `acquire/stages.StageContext` - "The corpus's `Ctx`,
    minus the fetcher and plus the title" - and decision 373 gives it the fetcher back. A second
    context here would be a second answer to "what is a handler handed".
  * `title_row` (`:110-114`). It is `sqlite3`, and the app reads `title` through asyncpg and
    resolves identity through `connectors/resolve`, which §7.1 makes the one implementation.
  * `Skip` and `Permanent` (`:14-19`) as the control flow. See the paragraph below.
  * `load_all`'s hand-written module list (`:103-106`). See `load_all`.

THE ONE STRUCTURAL CHANGE: THE FIVE EXCEPTIONS BECOME ONE RETURN VALUE, one level below where
M5.1 already made the same change. The corpus signals with `Skip`, `Permanent`, `HostPaused`,
`RobotsDisallowed` and `FetchError`, and `mdc/runner.py:176-193` is the five arms that catch
them.
`acquire/stages.py:20-29` records why the STAGE contract stopped doing that - "§8's stages fail
in ways that are not exceptional at all ... and a normal outcome raised as an exception is one a
later `except Exception` swallows into a failure" - and the argument reaches one level further
down here, because decision 334 makes "this source did not answer, and stage 2 advances anyway"
the ordinary outcome of seven of the eight. So an adapter returns a `SourceResult` and the
stage-2 driver turns the set of them into one `stages.Outcome`.

THAT IS TWO VOCABULARIES AND NOT THREE, which is the thing to check when reading it:
`SourceResult` says what one source did, `Outcome` says what the stage did, and decision 334 is
the whole of the mapping between them - `tmdb:detail` failing parks the stage, any other source
failing is a note under its own name and the stage still advances.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only, and it has to be: §8 stage 2 lives in `acquire/stages.py`, which drives this
    # registry, so a runtime import here would close a cycle. It also keeps this module what it
    # is - metadata about callables - rather than something every adapter pulls the ten-stage
    # machine in to describe itself with.
    from spielplan.acquire.stages import StageContext


@dataclass(frozen=True)
class SourceResult:
    """What ONE source did. The corpus's five exceptions, as the one value a driver can read.

    Frozen for `stages.Outcome`'s reason: the stage-2 driver writes §6.6's board `detail` from
    these and then decides its own verb from the same objects, and two readers of one mutable
    record is how a note shown to an operator stops being the note the stage acted on.

    `note` is written for a person, because decision 334 puts it in `acquisition_job.detail`
    under this source's name (`0005_ledger.sql:137`) and, for the one required source, inside
    the park `reason` one line below it, which that migration calls "shown verbatim on the
    admin board".
    `doc_id` is the `raw_document` row the bytes landed in - the row and never the bytes, which
    is decision 345 - and it is None exactly when nothing was stored.

    `ran` IS DECISION 334'S WORD "RAN", AS SOMETHING THE DRIVER CAN READ. That decision parks
    stage 2 for "a required source that RAN and did not answer" and gives a note to a required
    source that never ran, and without this flag the two arrive at `stages.enrich` as the same
    value - `ok=False` - so a title TMDB has no record of parked at stage 2 with a reason telling
    an operator to check a connector that is working. The default is True because it is the
    answer for every return that follows a fetch AND because it is the conservative one: a
    handler that raised, or one that has nothing to say about the question, must park the
    required source rather than walk past it. `False` is therefore an explicit claim, made only
    where the adapter can see that no request left the box.
    Read in exactly one branch - `stages.enrich`'s required-source park - so `sources/tmdb.py` is
    the module obliged to keep it exact today, and an adapter that becomes required inherits that
    obligation with `REQUIRED_KIND`. [M5.3 review cycle 1, M53-334-01]
    """

    source: str
    kind: str
    ok: bool
    doc_id: int | None = None
    note: str = ""
    ran: bool = True


# One argument, not the corpus's `(ctx, task)`: `StageContext` already carries the task M5.1 put
# on it, so passing both would hand an adapter two answers to "which task is this".
Handler = Callable[["StageContext"], Awaitable[SourceResult]]


@dataclass(frozen=True)
class HandlerSpec:
    """One registered source kind, and everything the driver needs to decide whether to run it.

    Frozen where the corpus's is not. The registry is process-global and read by every drain; a
    spec mutated at runtime would be a per-process fork of what a source IS, visible in a board
    `detail` and in nothing that could be diffed.
    """

    kind: str
    fn: Handler
    source: str
    # The capability this kind needs enabled - a `connector_config` name for the three keyed
    # sources (`tmdb`, `omdb`, `trakt`), None for the five keyless ones. `available_kinds` reads
    # it against a capability map the driver builds from `credentials`, which is decision 377's
    # narrow read; the corpus read `cfg.enabled_sources`, a config file this app does not have.
    requires: str | None
    description: str
    # Lower runs first, which is the corpus's own convention (`mdc/queue.py:132` orders
    # `priority ASC`) and is why `available_kinds` sorts on it rather than on the name: §8 says
    # `wikidata:resolve` "halves guessing" because it yields the MC/RT/Letterboxd slugs, so the
    # corpus's numbers put it at 15 and `rt:page` at 76 and `metacritic:page` at 77. That
    # ordering is the difference between reading a slug and guessing one.
    default_priority: int
    # §8's own stage names. Everything this package registers is stage 2, which §8 calls
    # `enrich`; the corpus also has `seed`, `reviews` and `aspects`, and the field stays free
    # text rather than an enum so a later stage's kinds need no change here.
    phase: str
    # True when draining this kind bills a metered API. Every crawl task is free; the LLM passes
    # are not, and at corpus scale the difference between them is roughly a hundred euros a
    # click (`mdc/sources/base.py:51-53`).
    paid: bool = False


REGISTRY: dict[str, HandlerSpec] = {}


def _identity(fn: Handler) -> tuple[str, str]:
    """What makes two registrations the same handler across a module reload."""
    return (getattr(fn, "__module__", ""), getattr(fn, "__qualname__", ""))


def handler(kind: str, *, source: str, requires: str | None = None,
            description: str = "", priority: int = 100,
            phase: str = "enrich",
            paid: bool = False) -> Callable[[Handler], Handler]:
    """Register one source kind. The decorator is the only way into `REGISTRY`.

    A SECOND HANDLER FOR ONE KIND IS REFUSED, where the corpus overwrites (`:65`). The corpus
    can afford the overwrite: its kinds are enqueued rows and a duplicate shows up as a task
    that ran the wrong code. Here the kind is how the stage-2 driver names a source at all, so a
    second registration would retire the first with no row, no log and no failure - and the
    source §8 requires would be missing from a stage that still reported success.

    Re-registering the SAME function is a no-op, so a module reload (which builds a new function
    object with the same qualified name) is not an error. That is the one case the refusal must
    not catch, because it is not two handlers.
    """
    def deco(fn: Handler) -> Handler:
        existing = REGISTRY.get(kind)
        if existing is not None and _identity(existing.fn) != _identity(fn):
            was = ".".join(p for p in _identity(existing.fn) if p)
            now = ".".join(p for p in _identity(fn) if p)
            raise RuntimeError(
                f"two handlers claim the source kind {kind!r}: {was} registered it and {now} "
                "would replace it. §8 stage 2 names each source once and the driver selects by "
                "kind, so the second registration would retire the first in silence"
            )
        REGISTRY[kind] = HandlerSpec(
            kind=kind, fn=fn, source=source, requires=requires,
            description=description or (fn.__doc__ or "").strip().split("\n")[0],
            default_priority=priority, phase=phase, paid=paid,
        )
        return fn
    return deco


def paid_kinds() -> set[str]:
    return {k for k, spec in REGISTRY.items() if spec.paid}


def available_kinds(capabilities: Mapping[str, bool], phase: str | None = None, *,
                    include_paid: bool = True) -> list[str]:
    """Task kinds that can run right now, in the order §8 wants them run.

    ``include_paid=False`` is the default for an unqualified run. A crawl and an extraction pass
    look identical from the queue's point of view, and that symmetry is a trap: "drain
    everything" is a reasonable thing to click when every task is a free HTTP fetch, and an
    expensive mistake the moment some of them are billed API calls. Paid kinds are opt-in by
    name only. (`mdc/sources/base.py:80-89`, kept verbatim in substance because the reason is
    the same one §8 stage 6 will meet.)

    TWO NAMED CHANGES. `capabilities` replaces the corpus's `cfg.enabled_sources`: this app has
    no config file of enabled sources, and decision 377 makes the three keyed sources' presence
    a fact about `connector_config` that `credentials` reads. And the result is ordered by
    `default_priority` rather than alphabetically, because in the corpus the ordering was the
    queue's (`mdc/queue.py:132`) and here the driver runs what this returns: sorted by name,
    `metacritic:page` would run before `wikidata:resolve` and scrape a guessed slug for a source
    whose real one was one request away (§8 stage 2, "halves guessing"). Ties break on the kind
    so the order is total and a failure message is stable.
    """
    out: list[HandlerSpec] = []
    for spec in REGISTRY.values():
        if phase and spec.phase != phase:
            continue
        if spec.requires and not capabilities.get(spec.requires, False):
            continue
        if spec.paid and not include_paid:
            continue
        out.append(spec)
    return [spec.kind for spec in sorted(out, key=lambda s: (s.default_priority, s.kind))]


_PACKAGE_DIR = Path(__file__).resolve().parent

# The modules in this package that are scaffolding rather than sources. Named rather than
# derived, so a module added here on purpose is a line in this set instead of a silent import.
_NOT_ADAPTERS = frozenset({"base", "credentials"})


def _is_adapter(name: str) -> bool:
    """Is `name` a module `load_all` should import to populate the registry?"""
    return not name.startswith("_") and name not in _NOT_ADAPTERS


def load_all() -> list[str]:
    """Import every adapter module so the registry is populated. Returns what it imported.

    DISCOVERED RATHER THAN LISTED, where the corpus spells eleven module names out (`:103-106`).
    Two reasons, and the second is the one that matters. The adapters land later in this
    milestone, so a hand-written list here would name modules that do not exist yet and this
    function would be broken until the last of them arrived. And a list of adapter names inside
    the registry's own module is the thing the registry exists to avoid: "the driver never names
    an adapter" is repealed just as thoroughly by naming them one import below it.

    Idempotent, because `importlib.import_module` returns the module already in `sys.modules`
    and `handler` refuses only a DIFFERENT function for a kind it already holds.
    """
    imported: list[str] = []
    for info in pkgutil.iter_modules([str(_PACKAGE_DIR)]):
        if not _is_adapter(info.name):
            continue
        importlib.import_module(f"{__package__}.{info.name}")
        imported.append(info.name)
    return sorted(imported)


def json_get(obj: Any, *path: Any, default: Any = None) -> Any:
    """Walk `path` into a decoded JSON document, answering `default` wherever it does not go.

    Ported from `mdc/sources/base.py:117-129`, with the step's `if`/`else` written as the
    ternary this repository's linter asks for and nothing else changed. Five ways a path can
    miss and one answer to all of them, which is what lets a parser read a provider's payload as
    a sentence instead of as a nest of guards - and what keeps a provider quietly dropping a
    field from raising out of a derive that had every other field it needed.
    """
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            cur = cur[key] if isinstance(key, int) else cur.get(key)
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
    return cur if cur is not None else default


__all__ = [
    "REGISTRY",
    "Handler",
    "HandlerSpec",
    "SourceResult",
    "available_kinds",
    "handler",
    "json_get",
    "load_all",
    "paid_kinds",
]
