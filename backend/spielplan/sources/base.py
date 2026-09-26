"""§8 stage 2's handler registry: how a source adapter declares itself, and what it answers with.

Adapters return a `SourceResult` instead of raising: a source not answering is ordinary (decision
334), and the stage-2 driver maps the set of them onto one `stages.Outcome`.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only: a runtime import would close a cycle with `acquire/stages.py`.
    from spielplan.acquire.stages import StageContext


@dataclass(frozen=True)
class SourceResult:
    """What ONE source did. `note` is shown verbatim on the admin board.

    `ran=False` claims no request left the box, so a required source that never ran is not parked
    (decision 334). Default True, the conservative answer.
    """

    source: str
    kind: str
    ok: bool
    doc_id: int | None = None
    note: str = ""
    ran: bool = True


Handler = Callable[["StageContext"], Awaitable[SourceResult]]


@dataclass(frozen=True)
class HandlerSpec:
    kind: str
    fn: Handler
    source: str
    # A `connector_config` name for the three keyed sources, None for the keyless ones.
    requires: str | None
    description: str
    # Lower runs first, so `wikidata:resolve` yields slugs before the scrapers need them.
    default_priority: int
    phase: str
    # True when draining this kind bills a metered API.
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

    A second handler for one kind raises; re-registering the same function (a reload) is a no-op.
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
    """Task kinds that can run right now, ordered by `default_priority` then kind.

    Paid kinds are opt-in: "drain everything" must never bill an API by accident.
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

# Scaffolding modules, named rather than derived.
_NOT_ADAPTERS = frozenset({"base", "credentials"})


def _is_adapter(name: str) -> bool:
    return not name.startswith("_") and name not in _NOT_ADAPTERS


def load_all() -> list[str]:
    """Import every adapter module so the registry is populated. Returns what it imported.

    Discovered, not listed, so the registry never names an adapter. Idempotent.
    """
    imported: list[str] = []
    for info in pkgutil.iter_modules([str(_PACKAGE_DIR)]):
        if not _is_adapter(info.name):
            continue
        importlib.import_module(f"{__package__}.{info.name}")
        imported.append(info.name)
    return sorted(imported)


def json_get(obj: Any, *path: Any, default: Any = None) -> Any:
    """Walk `path` into a decoded JSON document, answering `default` wherever it does not go."""
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
