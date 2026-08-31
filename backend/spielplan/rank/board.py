"""§6.3's tier board and its badges. Spec v2.1 §6.3, §5.2, §4.3; proposals 71, 76, 78, 82.

Pure: numpy and the fit's output, no database and no clock. §6.3 is four bullets long and
three of them are statements about a function of `(s, sigma, cuts, tier_set, the last drop)`,
so that is what this module is.

THREE BADGES, AND WHY THEY ARE ONE MODULE

  tier + neighbourhood   §6.3: "Badge shows tier + neighbourhood ("A — between Heat and
                         Prisoners")". The neighbours are the titles above and below *inside
                         the same tier*: the badge already names the tier, so a neighbour from
                         another one contradicts the letter beside it, and the ordering the
                         claim is made over has to be the ordering the board renders.
  straddle               §6.3: "a straddling title shows "A/S" and becomes queue-eligible" —
                         ONE predicate doing both jobs, which is why `queue.eligible` calls
                         `straddles()` here rather than re-deriving a threshold of its own.
  tension                §6.3: "if the model disagrees strongly, the title's badge shows the
                         tension rather than snapping back."

PLACEMENT, AND THE SENTENCE THAT DECIDES IT

§6.3 forbids snapping back, and forbids it in both directions: a title whose assigned tier
falls outside the posterior's interval "stays in the assigned tier", and a one-level
disagreement inside the interval "produces neither a badge nor a move". Both halves say the
same thing about placement — **the most recent `tier_edit` decides where a title renders, and
the model decides it only when there is no edit.** When the two agree, which is the normal case
once the refit has absorbed the drop, they are the same number and nothing is decided at all.

This is not "drag-and-drop is an override" (§5.2 says it is not). The edit is an observation:
it moves `s`, it moves the cutpoints, and it moves every other title's tier through the shared
latent. What it additionally does is stay put on screen, so that when the model disagrees the
person sees a disagreement instead of a title that slid back under their thumb.

BADGE PRECEDENCE. A straddling title is queue-eligible and a title in tension is *also* worth
comparing, and both chips want the same corner of the same poster. Proposal 71 resolves it: the
tension badge replaces the straddle badge while it holds. Eligibility is untouched — that is a
property of `straddles()`, not of which string got rendered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

from spielplan.ledger import model
from spielplan.ledger.hyperparams import Hyperparams


@dataclass(frozen=True)
class Item:
    """One rated title, as the board's arithmetic needs it.

    `sigma` is the *displayed* σ — `ledger_state.sigma_eff`, which carries §5.2's freshness
    inflation. The badges are a statement about how sure the model is *now*, and the fitted σ
    of a title nobody has touched for two years is not that.

    `assigned_tier` is the tier of the person's most recent `tier_edit`, or None. It is not a
    tier the model produced and must never be filled in from one.
    """

    title_id: int
    name: str
    s: float
    sigma: float
    assigned_tier: int | None = None


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
    straddle_badge: str | None  # "A/S", suppressed while a tension badge holds
    above: str | None
    below: str | None
    badge: str
    tension: str | None

    def public(self) -> dict[str, object]:
        """The projection that may reach a client.

        `s` and `sigma` are absent: decision 117 gates every inline numeric annotation behind
        the model-log toggle, and `home.rail.redact` removes them from a `model` block. Putting
        them at the top level of a board row would route around that gate, so they are not put
        there — the route assembles the gated block itself.
        """
        return {
            "title_id": self.title_id,
            "name": self.name,
            "tier": self.tier,
            "assigned_tier": self.assigned_tier,
            "straddle": self.straddle,
            "straddle_badge": self.straddle_badge,
            "badge": self.badge,
            "tension": self.tension,
        }


@dataclass(frozen=True)
class Tier:
    index: int                  # index into the tier set, ascending (0 = worst)
    label: str
    entries: tuple[Entry, ...]


def straddles(item: Item, *, cuts: np.ndarray, hp: Hyperparams) -> int | None:
    """§6.3's one predicate: the adjacent tier the posterior also reaches, or None.

    Both the badge and `queue.eligible` come through here. The prototype had two thresholds —
    badge at σ > .13, queue at σ > .09 — so a title at .11 was queue-eligible and wore no
    badge, and the badge could not be the queue's entry point (proposal 157). One function is
    the only way that identity survives a later edit to either side.

    `model.straddle` never returns the title's own tier, which is proposal 76's "S never
    renders S/S" falling out of the arithmetic rather than being clamped afterwards.
    """
    reached = model.straddle(
        np.array([item.s]), np.array([item.sigma]), np.asarray(cuts, dtype=float), hp
    )[0]
    return None if int(reached) < 0 else int(reached)


def _band(tier: int, cuts: np.ndarray) -> tuple[float, float]:
    """The `s` interval a tier occupies, with the ends open."""
    ordered = np.sort(np.asarray(cuts, dtype=float))
    low = float(ordered[tier - 1]) if tier > 0 else -np.inf
    high = float(ordered[tier]) if tier < ordered.size else np.inf
    return low, high


def tension_of(
    item: Item, *, model_tier: int, cuts: np.ndarray, tier_set: Sequence[str], hp: Hyperparams
) -> str | None:
    """§6.3's "disagrees strongly", in the reading proposal 71 gives it: the tier the person
    assigned and the posterior's credible interval are **disjoint**.

    A tier is an interval between cutpoints and the posterior is an interval on `s`, so
    "outside" is a statement about two intervals and the only coherent reading of it is that
    they do not meet. A one-level difference whose bands still overlap is a difference, not a
    disagreement — badging those would badge most of a young board, which is how a signal
    becomes wallpaper.
    """
    if item.assigned_tier is None or item.assigned_tier == model_tier:
        return None
    low, high = _band(int(item.assigned_tier), cuts)
    z = hp.tension_z()
    lower, upper = item.s - z * item.sigma, item.s + z * item.sigma
    if high > lower and upper > low:          # the intervals meet: not tension
        return None
    return (
        f"you put it in {tier_set[int(item.assigned_tier)]} — "
        f"the ledger still reads {tier_set[int(model_tier)]}"
    )


def _badge(label: str, above: str | None, below: str | None) -> str:
    """§6.3's "tier + neighbourhood", in §6.8's quiet-reasons register.

    The ends of a tier get their own phrasing rather than "between X and (nothing)": a
    neighbourhood claim with a hole in it reads as a bug, and a title at the top of A genuinely
    has only one neighbour.
    """
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
    """The whole board: every tier in the set, best-first, empty ones kept.

    Empty tiers stay because they are still drop targets (proposal 82) — a board that hides
    the tier nobody has used yet is a board you cannot put the first title into.
    """
    cuts = np.asarray(cuts, dtype=float)
    labels = list(tier_set)
    buckets: dict[int, list[Item]] = {i: [] for i in range(len(labels))}
    model_tiers: dict[int, int] = {}
    tensions: dict[int, str | None] = {}

    for raw in items:
        # Decision 11 keeps `tier_edit` rows across a change in K, so a level that no longer
        # exists is a state this board is *guaranteed* to meet — and it is the only consumer
        # that indexes the cutpoint array by that level. `ledger.observations` clamps the same
        # rows for the fit and logs that it did; the board did not, and `_band` walked off the
        # end of a shrunk array, taking the whole surface down with a 500 until the person
        # re-dropped every affected title.
        #
        # Clamped ONCE, here, rather than at each use: the bucket and the badge have to agree
        # about which tier the person assigned, and two clamps in two places is how they stop
        # agreeing. Clamping rather than dropping keeps decision 11's promise — the edit is
        # still an observation, still says "the top tier they had", exactly as the fit reads it.
        item = (
            raw
            if raw.assigned_tier is None
            else replace(raw, assigned_tier=max(0, min(len(labels) - 1, int(raw.assigned_tier))))
        )
        model_tier = int(model.tier_of(np.array([item.s]), cuts)[0])
        model_tiers[item.title_id] = model_tier
        tensions[item.title_id] = tension_of(
            item, model_tier=model_tier, cuts=cuts, tier_set=labels, hp=hp
        )
        # §6.3: "stays in the assigned tier" / "neither a badge nor a move". The person's drop
        # decides placement whenever there is one; the model decides it otherwise.
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
            # Proposal 71: the two chips compete for the same corner, and tension wins while it
            # holds. `reached` is untouched — eligibility is the predicate, not the string.
            badge = (
                f"{labels[index]}/{labels[reached]}"
                if reached is not None and reached != index and tension is None
                else None
            )
            entries.append(
                Entry(
                    title_id=item.title_id,
                    name=item.name,
                    s=item.s,
                    sigma=item.sigma,
                    model_tier=model_tiers[item.title_id],
                    assigned_tier=item.assigned_tier,
                    tier=index,
                    straddle=reached,
                    straddle_badge=badge,
                    above=above,
                    below=below,
                    badge=_badge(labels[index], above, below),
                    tension=tension,
                )
            )
        tiers.append(Tier(index=index, label=labels[index], entries=tuple(entries)))

    # §6.3 lists the tiers ascending (F … S); the board renders them best-first (proposal 82).
    return tuple(reversed(tiers))


__all__ = ["Entry", "Item", "Tier", "build", "straddles", "tension_of"]
