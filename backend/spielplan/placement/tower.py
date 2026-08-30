"""The Cold Tower's forward pass. Spec v2.1 §1, §4.3, §8 stage 9, §5.1.

§4.3: "`cold_tower.pt` — from `data/prep/cold_tower2.pt` (the live model; the earlier
`cold_tower` run is superseded — the exporter must ship v2)."
§8 stage 9: "feature vector per the feature contract → Cold Tower → ê(t), b̂(t)".
§1: "A content encoder (**Cold Tower**, torch CPU) places titles *without* ratings into that
space from their DNA + metadata — validated: it recovers 67% of the oracle's personal signal on
unrated titles."

Two rules shape this module.

**CPU, everywhere, with no escape hatch.** §2: "No GPU anywhere. Torch CPU wheels only. The
image must build and run on a GPU-less VM." Everything below loads with `map_location="cpu"`,
there is no device argument to pass, and the thread count is pinned to the §2 reference box.

**A width mismatch is loud.** The checkpoint records the input width it was trained at and the
contract states the width the app builds. If they disagree, the honest outcomes are a crash or
thirteen thousand plausible-looking wrong coordinates — so this module raises, naming both
widths and both hashes, rather than broadcasting into nonsense.

torch is imported inside the functions: a bundle-less boot (§3.1) and every request-path import
would otherwise pay for a 200 MB library that only the worker uses.
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

# §4.3: "the exporter must ship v2". v1 checkpoints exist in the corpus project and are a
# different, superseded model; loading one silently would place the whole library against it.
SUPPORTED_VERSIONS = (2,)
ARCHITECTURES = ("cold_tower_v2",)

# §5.1's e(t) and every consumer of it are 64-d: §5.2's "64-d user vector", §4.2's
# `user_vector.vec`, and `title_placement.dim CHECK (dim = 64)`.
EMBED_DIM = 64


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

    def place(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(N, input_dim) float32 in; (N, 64) float32 coordinates and (N,) float64 priors out.

        §5.1 wants both halves of the cold branch: ê(t), the coordinate that goes into
        ⟨v_u, e(t)⟩, and b̂(t), "the shrunk item prior … from the Cold Tower for cold titles".
        """
        import torch

        x = np.ascontiguousarray(x, dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != self.input_dim:
            raise TowerError(
                f"cold tower expects (N, {self.input_dim}) and was handed {x.shape}"
            )
        if self.module.training:
            # A module left in training mode places the same title differently on every sweep.
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
    """§2's reference box is 4 vCPU, and the box also runs Postgres, the API and the worker.
    Letting torch claim every core makes the nightly sweep starve the request path."""
    return max(1, min(4, os.cpu_count() or 1))


# Per (path, mtime, contract) — §5.3's "<1 s/title" is steady-state work, and module load is
# not per-title work. The cache is keyed by the contract hash too, so a re-import against a
# contract with a different column set cannot reuse a tower verified against the old one.
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
        "cold tower loaded: arch=%s input_dim=%d embed_dim=%d threads=%d sha256=%s",
        tower.arch, tower.input_dim, tower.embed_dim, tower_threads(), tower.sha256[:12],
    )
    return tower


def _load(path: Path, contract: FeatureContract) -> Tower:
    import torch

    torch.set_num_threads(tower_threads())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    # §1 forbids CUDA anywhere; map_location pins every tensor to the CPU regardless of the
    # device the corpus project saved from. `weights_only` refuses to unpickle anything but
    # tensors and plain data — the bundle is the operator's own artifact, but a model file that
    # can execute code on load is not a property worth having.
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(checkpoint, dict) or "state_dict" not in checkpoint:
        raise TowerError(
            f"{path.name} is not a v2 Cold Tower checkpoint (no `state_dict`); §4.3 says the "
            "exporter must ship v2"
        )
    version = int(checkpoint.get("version", 0))
    if version not in SUPPORTED_VERSIONS:
        raise TowerError(
            f"{path.name} declares version {version}; §4.3: 'the earlier cold_tower run is "
            f"superseded — the exporter must ship v2' (supported: {list(SUPPORTED_VERSIONS)})"
        )
    arch = str(checkpoint.get("arch") or "")
    if arch not in ARCHITECTURES:
        raise TowerError(
            f"{path.name} declares architecture {arch!r}, which this app cannot reconstruct "
            f"(known: {list(ARCHITECTURES)})"
        )

    state = dict(checkpoint["state_dict"])
    input_dim = int(checkpoint.get("input_dim", 0))
    embed_dim = int(checkpoint.get("embed_dim", EMBED_DIM))
    module = _cold_tower_v2(state, input_dim, embed_dim)

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
        input_dim=input_dim, embed_dim=embed_dim, arch=arch, version=version,
        sha256=sha, module=module,
    )


def _cold_tower_v2(state: dict[str, Any], input_dim: int, embed_dim: int) -> Any:
    """Rebuild the `cold_tower_v2` module around the checkpoint's own weights.

    The checkpoint ships a `state_dict`, not a scripted module, so the topology has to live
    somewhere; it lives here, once, keyed by the `arch` tag the exporter writes, with every
    width read from the weights rather than assumed. A ReLU trunk feeding two heads — ê(t) and
    b̂(t) — is what §8 stage 9 and §5.1 between them describe, and it is what the exporter
    saves.

    Dropout is deliberately absent. §5.3's "absent blocks dropped — the tower's dropout training
    anticipates this" is a statement about *training*; at inference the module is in eval mode
    where dropout is the identity, so reconstructing it would add a number this app does not
    have (the checkpoint records no rate) to compute exactly nothing. `place()` asserts the
    module is not in training mode, which is the property that actually matters.
    """
    import torch
    from torch import nn

    trunk_indices = sorted(
        int(k.split(".")[1]) for k in state if k.startswith("trunk.") and k.endswith(".weight")
    )
    if not trunk_indices:
        raise TowerError("checkpoint has no `trunk.*.weight` layers")
    for head in ("embed", "prior"):
        if f"{head}.weight" not in state:
            raise TowerError(f"checkpoint has no `{head}` head; §5.1 needs both ê(t) and b̂(t)")

    first = state[f"trunk.{trunk_indices[0]}.weight"]
    if int(first.shape[1]) != input_dim:
        raise TowerError(
            f"checkpoint says input_dim={input_dim} but its first layer takes "
            f"{int(first.shape[1])} columns — the checkpoint disagrees with itself"
        )
    if int(state["embed.weight"].shape[0]) != embed_dim:
        raise TowerError(
            f"checkpoint says embed_dim={embed_dim} but its embed head emits "
            f"{int(state['embed.weight'].shape[0])}"
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
            self.embed = nn.Linear(
                int(state["embed.weight"].shape[1]), int(state["embed.weight"].shape[0])
            )
            self.prior = nn.Linear(int(state["prior.weight"].shape[1]), 1)

        def forward(self, x):
            for layer in self.trunk:
                x = torch.relu(layer(x))
            return self.embed(x), self.prior(x).squeeze(-1)

    module = ColdTowerV2()
    with torch.no_grad():
        for slot, i in enumerate(trunk_indices):
            module.trunk[slot].weight.copy_(state[f"trunk.{i}.weight"])
            module.trunk[slot].bias.copy_(state[f"trunk.{i}.bias"])
        for head in ("embed", "prior"):
            getattr(module, head).weight.copy_(state[f"{head}.weight"])
            getattr(module, head).bias.copy_(state[f"{head}.bias"])
    module.eval()
    return module
