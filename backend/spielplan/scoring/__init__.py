"""The §5.1 serving stack. Spec v2.1 §5.1, §4.1 rule 5, §4.3, §5.3, §6.0, §10, §12.

Three modules, split by what they are allowed to touch:

* `backbone` — the frozen basis from `backbone.npz` and the per-title coordinate. numpy only.
* `foldin`   — the 64-d user fold-in and the per-label-count blend weight (§5.3, nightly).
* `serve`    — the materialised `title_prior` / `user_score` rows, the ranked read, and the
               §6.0 model line. All the SQL lives here.

`serve` never imports `foldin`, so the dependency runs one way: serve → backbone, and
foldin → {backbone, serve}. The nightly job is therefore the only place where a fit and a
write meet, which is what keeps the serving read free of any fitting code.
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
