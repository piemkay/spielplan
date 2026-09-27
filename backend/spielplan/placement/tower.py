"""The Cold Tower's forward pass (§8 stage 9): CPU only (§2), and a width mismatch raises.

torch is imported inside the functions so a bundle-less boot and the request path never load it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from spielplan.placement.contract import FeatureContract

log = logging.getLogger("spielplan.placement.tower")

EMBED_DIM = 64


# The exporter's tensor names. A bare `state_dict` carries no metadata, so these ARE the contract.
TRUNK_FIRST = "trunk.0.weight"
EMBED_HEAD = "head_e"
PRIOR_HEAD = "head_b"


class TowerError(RuntimeError):
    """The checkpoint cannot be used as §8 stage 9's placer."""


@dataclass(frozen=True)
class Tower:
    input_dim: int
    embed_dim: int
    arch: str
    version: int
    sha256: str
    module: Any = field(repr=False)
    # What was assumed rather than checked (a bare state_dict's version), for the import report.
    notes: tuple[str, ...] = ()

    def place(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(N, input_dim) float32 in; ê (N, 64) float32 and b̂ (N,) float64 out."""
        import torch

        x = np.ascontiguousarray(x, dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != self.input_dim:
            raise TowerError(
                f"cold tower expects (N, {self.input_dim}) and was handed {x.shape}"
            )
        if self.module.training:
            raise TowerError("cold tower is in training mode; §8 stage 9 is inference only")
        with torch.inference_mode():
            embedding, prior = self.module(torch.from_numpy(x))
        e_hat = np.array(embedding.detach().cpu().numpy(), dtype=np.float32)
        b_hat = np.array(prior.detach().cpu().numpy(), dtype=np.float64).reshape(-1)
        if e_hat.shape != (x.shape[0], self.embed_dim) or b_hat.shape != (x.shape[0],):
            raise TowerError(
                f"cold tower returned {e_hat.shape} / {b_hat.shape} for {x.shape[0]} titles; "
                f"§8 stage 9 expects ê(t) of width {self.embed_dim} and one b̂(t) per title"
            )
        return e_hat, b_hat


def tower_threads() -> int:
    """Half the cores, at most two: the sweep runs on the loop thread and must not starve requests."""
    return max(1, min(2, (os.cpu_count() or 1) // 2))


# Keyed by the contract hash too, so a tower verified against another contract is never reused.
_CACHE: dict[tuple[str, int, str], Tower] = {}


def load_tower(store: Any, contract: FeatureContract) -> Tower:
    """Load and verify the active bundle's Cold Tower."""
    if getattr(store, "is_empty", True):
        raise TowerError("no artifact bundle is loaded, so there is no Cold Tower")
    if not store.present.get("cold_tower.pt"):
        raise TowerError(
            f"bundle {store.version} ships no cold_tower.pt — §8 stage 9 cannot place anything, "
            "and §12's M2 exit criterion (every owned title has a coordinate) is unreachable"
        )
    path = Path(store.path("cold_tower.pt"))
    key = (str(path), path.stat().st_mtime_ns, contract.sha256)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    tower = _load(path, contract)
    _CACHE[key] = tower
    log.info(
        "cold tower loaded: arch=%s input_dim=%d embed_dim=%d threads=%d sha256=%s%s",
        tower.arch, tower.input_dim, tower.embed_dim, tower_threads(), tower.sha256[:12],
        "".join(f"; {note}" for note in tower.notes),
    )
    return tower


def _load(path: Path, contract: FeatureContract) -> Tower:
    import torch

    torch.set_num_threads(tower_threads())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    # CPU regardless of where it was saved (§1); `weights_only` so loading cannot execute code.
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    # §4.3: the exporter ships v2 as a bare state_dict, so the dims come from the tensor shapes.
    if (
        not isinstance(checkpoint, dict) or not checkpoint
        or not all(hasattr(v, "shape") for v in checkpoint.values())
    ):
        raise TowerError(
            f"{path.name} is not a bare state_dict of tensors, the form the corpus ships the v2 "
            "Cold Tower in; §8 stage 9 has nothing to place with"
        )
    input_dim, embed_dim = _dims_from_state(checkpoint, path.name)
    module = _cold_tower_v2(checkpoint)

    if input_dim != contract.input_dim:
        raise TowerError(
            f"{path.name} was trained on {input_dim} input columns and "
            f"feature_contract.json (sha256 {contract.sha256[:12]}) defines "
            f"{contract.input_dim} ({contract.content_width} content + {contract.text_used} "
            f"review-text). §4.3 makes the contract the exhaustive definition of this tower's "
            "input, so one of the two files is from another bundle — refusing to place rather "
            "than broadcasting into a wrong coordinate."
        )
    if embed_dim != EMBED_DIM:
        raise TowerError(
            f"{path.name} emits a {embed_dim}-d embedding; the Backbone is 64-d everywhere "
            "(§5.1, §5.2, title_placement.dim)"
        )
    return Tower(
        input_dim=input_dim, embed_dim=embed_dim, arch="cold_tower_v2", version=2,
        sha256=sha, module=module, notes=(
            f"{path.name} declares no version or architecture; assumed v2 cold_tower_v2 from its "
            "tensor names - the corpus ships a bare state_dict, so this is an assumption and not "
            "a check (section 4.3)",
        ),
    )


def _dims_from_state(state: dict, name: str) -> tuple[int, int]:
    """(input_dim, embed_dim), read out of the weight shapes. Never guessed."""
    first = state.get(TRUNK_FIRST)
    head = state.get(f"{EMBED_HEAD}.weight")
    if first is None or head is None:
        raise TowerError(
            f"{name} has no `{TRUNK_FIRST}`/`{EMBED_HEAD}.weight`; this app reconstructs the v2 "
            f"tower from those names and found {sorted(state)[:6]}"
        )
    return int(first.shape[1]), int(head.shape[0])


def _cold_tower_v2(state: dict[str, Any]) -> Any:
    """Rebuild the `cold_tower_v2` module (a ReLU trunk and heads ê, b̂) around the checkpoint's weights.

    No dropout layers: at inference in eval mode dropout is the identity.
    """
    import torch
    from torch import nn

    trunk_indices = sorted(
        int(k.split(".")[1]) for k in state if k.startswith("trunk.") and k.endswith(".weight")
    )
    if f"{PRIOR_HEAD}.weight" not in state:
        raise TowerError(
            f"checkpoint has no `{PRIOR_HEAD}` head; §5.1 needs both ê(t) and b̂(t), and the "
            f"exporter names them {EMBED_HEAD}/{PRIOR_HEAD}"
        )

    class ColdTowerV2(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.trunk = nn.ModuleList(
                nn.Linear(
                    int(state[f"trunk.{i}.weight"].shape[1]),
                    int(state[f"trunk.{i}.weight"].shape[0]),
                )
                for i in trunk_indices
            )
            self.head_e = nn.Linear(
                int(state[f"{EMBED_HEAD}.weight"].shape[1]),
                int(state[f"{EMBED_HEAD}.weight"].shape[0]),
            )
            self.head_b = nn.Linear(int(state[f"{PRIOR_HEAD}.weight"].shape[1]), 1)

        def forward(self, x):
            for layer in self.trunk:
                x = torch.relu(layer(x))
            return self.head_e(x), self.head_b(x).squeeze(-1)

    module = ColdTowerV2()
    with torch.no_grad():
        for slot, i in enumerate(trunk_indices):
            module.trunk[slot].weight.copy_(state[f"trunk.{i}.weight"])
            module.trunk[slot].bias.copy_(state[f"trunk.{i}.bias"])
        for head in (EMBED_HEAD, PRIOR_HEAD):
            getattr(module, head).weight.copy_(state[f"{head}.weight"])
            getattr(module, head).bias.copy_(state[f"{head}.bias"])
    module.eval()
    return module
