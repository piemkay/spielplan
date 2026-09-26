"""The §5.1 serving stack: `backbone` (basis + coordinate), `foldin` (nightly user fit), `serve` (SQL).

`serve` never imports `foldin`, so a fit and a write meet only in the nightly job.
"""

from __future__ import annotations

from spielplan.scoring.backbone import (
    EMBED_DIM,
    EVIDENCE_K,
    Backbone,
    BackboneError,
    Coordinate,
    coordinate,
    gate,
    load_for,
    pack_vec,
    unpack_vec,
)

__all__ = [
    "EMBED_DIM",
    "EVIDENCE_K",
    "Backbone",
    "BackboneError",
    "Coordinate",
    "coordinate",
    "gate",
    "load_for",
    "pack_vec",
    "unpack_vec",
]
