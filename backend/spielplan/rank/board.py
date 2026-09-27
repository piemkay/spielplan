"""§6.3's tier board and its badges (tier + neighbourhood, straddle, tension). Pure.

The latest `tier_edit` decides where a title renders; the model decides only when there is none.
A tension badge replaces the straddle badge while it holds (proposal 71).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

from spielplan.ledger import model
from spielplan.ledger.hyperparams import Hyperparams

# Pure arithmetic, so this module still opens no connection.
from spielplan.ledger.observations import rescale_level


@dataclass(frozen=True)
class Item:
    """One rated title, as the board's arithmetic needs it.

    `sigma` is the DISPLAYED σ (`sigma_eff`, freshness-inflated). `assigned_tier` is the latest
    `tier_edit`'s tier or None, never a model tier.
    """

    title_id: int
    name: str
    s: float
    sigma: float
    assigned_tier: int | None = None
    # Holds the model tier inside the verdict's band (decision 508).
    verdict: int | None = None
    year: int | None = None


@dataclass(frozen=True)
class Entry:
    title_id: int
    name: str
    s: float
    sigma: float
    model_tier: int             # where the ledger puts it
    assigned_tier: int | None   # where the person put it, if they have
    tier: int                   # where the board renders it — the one above that exists
    straddle: int | None        # the adjacent tier the posterior also reaches
    straddle_badge: str | None  # "A or S?" from `model_tier`, suppressed while a tension badge holds
    above: str | None
    below: str | None
    badge: str
    tension: str | None
    year: int | None = None

    def public(self) -> dict[str, object]:
        """The projection that may reach a client: no `s`/`sigma`, which decision 117 gates."""
        return {
            "title_id": self.title_id,
            "name": self.name,
            "tier": self.tier,
            "assigned_tier": self.assigned_tier,
            "straddle": self.straddle,
            "straddle_badge": self.straddle_badge,
            "badge": self.badge,
            "tension": self.tension,
            "year": self.year,
        }


@dataclass(frozen=True)
class Tier:
    index: int                  # index into the tier set, ascending (0 = worst)
    label: str
    entries: tuple[Entry, ...]
    verdict: str                # the verdict class it stands for, in words (decision 508)


# Indexed by verdict class, the order of `model.verdict_tiers`' rows.
VERDICT_WORDS = ("Disliked", "Fine", "Liked")


def straddles(item: Item, *, cuts: np.ndarray, hp: Hyperparams) -> int | None:
    """§6.3's one predicate: the adjacent tier the posterior also reaches, or None.

    Both the badge and `queue.eligible` read it, so badge and queue eligibility cannot drift.
    """
    return _placed(item, np.asarray(cuts, dtype=float), hp)[1]


def _placed(item: Item, cuts: np.ndarray, hp: Hyperparams) -> tuple[int, int | None]:
    """The model's tier and the adjacent tier its posterior reaches, with decision 508's hold.

    A dropped title is not held: tension is measured against its raw model tier.
    """
    s = np.array([item.s])
    tier = model.tier_of(s, cuts)
    reached = model.straddle(s, np.array([item.sigma]), cuts, hp)
    if item.assigned_tier is None and item.verdict is not None:
        tier, reached = model.hold_to_verdict(
            tier, reached, np.array([int(item.verdict)]), cuts.size + 1
        )
    return int(tier[0]), (None if int(reached[0]) < 0 else int(reached[0]))


def _band(tier: int, cuts: np.ndarray) -> tuple[float, float]:
    """The `s` interval a tier occupies, with the ends open."""
    ordered = np.sort(np.asarray(cuts, dtype=float))
    low = float(ordered[tier - 1]) if tier > 0 else -np.inf
    high = float(ordered[tier]) if tier < ordered.size else np.inf
    return low, high


def tension_of(
    item: Item, *, model_tier: int, cuts: np.ndarray, tier_set: Sequence[str], hp: Hyperparams
) -> str | None:
    """§6.3's "disagrees strongly": the assigned tier's band and the credible interval are DISJOINT."""
    if item.assigned_tier is None or item.assigned_tier == model_tier:
        return None
    low, high = _band(int(item.assigned_tier), cuts)
    z = hp.tension_z()
    lower, upper = item.s - z * item.sigma, item.s + z * item.sigma
    if high > lower and upper > low:          # the intervals meet: not tension
        return None
    return (
        f"You put it in {tier_set[int(item.assigned_tier)]} — "
        f"your other answers still point to {tier_set[int(model_tier)]}"
    )


def why_line(
    *,
    rated: int,
    compared: int,
    placed_by_you: int,
    fitting: bool,
    tier_set: Sequence[str],
) -> str:
    """§6.8's one-line why for the board (decision 486): the counts, then what the letters mean.

    `compared` includes the held-out tenth, so no single answer is singled out (§13).
    """
    if fitting and rated == 0:
        return "tiers are still being fitted"
    counts = f"{rated} rated · {compared} compared"
    if placed_by_you:
        counts = f"{counts} · {placed_by_you} placed by you"
    return f"{counts} · {_band_words(tier_set)}"


def _band_words(tier_set: Sequence[str]) -> str:
    """Decision 508's rule in the person's own letters, best-first like the board."""
    labels = list(tier_set)
    bands = model.verdict_tiers(len(labels))
    (d_low, d_high), (f_low, f_high), (l_low, l_high) = (tuple(int(x) for x in b) for b in bands)
    liked = f"liked in {labels[l_low]}" if l_low == l_high else f"liked from {labels[l_low]} up"
    fine = (
        f"fine in {labels[f_low]}"
        if f_low == f_high
        else f"fine in {labels[f_low]} to {labels[f_high]}"
    )
    disliked = (
        f"disliked in {labels[d_high]}"
        if d_low == d_high
        else f"disliked from {labels[d_high]} down"
    )
    return f"{liked}, {fine}, {disliked}"


def _badge(label: str, above: str | None, below: str | None) -> str:
    """§6.3's "tier + neighbourhood"; the ends of a tier get their own phrasing."""
    if above and below:
        return f"{label} — between {above} and {below}"
    if below:
        return f"{label} — just above {below}"
    if above:
        return f"{label} — just below {above}"
    return f"{label} — the only one"


def build(
    items: Sequence[Item],
    *,
    cuts: np.ndarray,
    tier_set: Sequence[str],
    hp: Hyperparams,
) -> tuple[Tier, ...]:
    """The whole board: every tier in the set, best-first, empty ones kept as drop targets."""
    cuts = np.asarray(cuts, dtype=float)
    labels = list(tier_set)
    buckets: dict[int, list[Item]] = {i: [] for i in range(len(labels))}
    model_tiers: dict[int, int] = {}
    tensions: dict[int, str | None] = {}

    for raw in items:
        # Clamp once, here, so bucket and badge agree. `read.items` already mapped the level
        # (decision 11); `k_from=None` only clamps, since mapping twice would move it again.
        item = (
            raw
            if raw.assigned_tier is None
            else replace(
                raw,
                assigned_tier=rescale_level(
                    int(raw.assigned_tier), k_from=None, k_to=len(labels)
                ),
            )
        )
        model_tier = _placed(item, cuts, hp)[0]
        model_tiers[item.title_id] = model_tier
        tensions[item.title_id] = tension_of(
            item, model_tier=model_tier, cuts=cuts, tier_set=labels, hp=hp
        )
        rendered = model_tier if item.assigned_tier is None else int(item.assigned_tier)
        buckets[rendered].append(item)

    tiers: list[Tier] = []
    for index in range(len(labels)):
        ordered = sorted(buckets[index], key=lambda i: (-i.s, i.title_id))
        entries = []
        for position, item in enumerate(ordered):
            above = ordered[position - 1].name if position > 0 else None
            below = ordered[position + 1].name if position + 1 < len(ordered) else None
            reached = straddles(item, cuts=cuts, hp=hp)
            tension = tensions[item.title_id]
            from_model = model_tiers[item.title_id]
            # Both halves of the chip are the posterior's (`from_model`), never the rendered
            # drop tier. Tension suppresses the chip but not eligibility (proposal 71).
            badge = (
                f"{labels[from_model]} or {labels[reached]}?"
                if reached is not None and reached != from_model and tension is None
                else None
            )
            entries.append(
                Entry(
                    title_id=item.title_id,
                    name=item.name,
                    s=item.s,
                    sigma=item.sigma,
                    model_tier=from_model,
                    assigned_tier=item.assigned_tier,
                    tier=index,
                    straddle=reached,
                    straddle_badge=badge,
                    above=above,
                    below=below,
                    badge=_badge(labels[index], above, below),
                    tension=tension,
                    year=item.year,
                )
            )
        tiers.append(
            Tier(
                index=index,
                label=labels[index],
                entries=tuple(entries),
                verdict=VERDICT_WORDS[model.verdict_class_of_tier(index, len(labels))],
            )
        )

    # Tiers are stored ascending (F … S); the board renders best-first (proposal 82).
    return tuple(reversed(tiers))


__all__ = ["VERDICT_WORDS", "Entry", "Item", "Tier", "build", "straddles", "tension_of", "why_line"]
