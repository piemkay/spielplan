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
                         Both jobs are §6.3's, on §6.3's scale. `tonight/round.py` asks a
                         question with the same shape about a different quantity and does NOT
                         share this threshold: decision 214 gives it BOUNDARY_Z, because a
                         Ledger posterior against learned cutpoints and a standardised §5.1
                         score against the rank-3/4 cut cannot be calibrated by one multiple.
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

# `rescale_level` and nothing else. It is pure arithmetic over §6.3's measured level shares, so
# this module still opens no connection and reads no clock; it is imported rather than re-spelled
# because the clamp below used to be the third independent copy of one rule, and a board whose
# bucket and whose badge disagree about the assigned tier is worse than the crash it replaced.
# [M4.13 step 15, dd06]
from spielplan.ledger.observations import rescale_level


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
    # The person's live verdict on the title, which holds its model tier inside the verdict's
    # band (decision 508). None where there is none; a drop decides placement on its own.
    verdict: int | None = None


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
    straddle_badge: str | None  # "A/S" from `model_tier`, suppressed while a tension badge holds
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

    The identity is between those two, and the threshold is what makes it worth anything: at
    `straddle_z` 1.0 a fitted board badged 120 of 120, so "queue-eligible" named the whole board
    and the 70% boundary arm drew from the same set the 20% exploration arm did. Decision 214
    retunes it to 0.15 — the same board badges 31 of 120 — and moves §6.2's round off it.

    `model.straddle` never returns the title's own tier, which is proposal 76's "S never
    renders S/S" falling out of the arithmetic rather than being clamped afterwards.
    """
    return _placed(item, np.asarray(cuts, dtype=float), hp)[1]


def _placed(item: Item, cuts: np.ndarray, hp: Hyperparams) -> tuple[int, int | None]:
    """The model's tier for the title and the adjacent tier its posterior reaches.

    Decision 508's hold is applied HERE, once, so the chip and the queue's eligibility stay the one
    predicate §6.3 makes them: a title held inside its verdict's band reaches toward `s`, and that
    is what both the badge and `queue.eligible` read. A dropped title is not held - the drop
    decides where it renders, and its model tier is what tension is measured against.
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
    # The member register (decision 486): "the ledger" is the model's noun, and what disagrees
    # with the drop is everything else the person has said about the title - their rating and
    # their comparisons - which is what the model tier is fitted from.
    return (
        f"you put it in {tier_set[int(item.assigned_tier)]} — "
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
    """§6.8's one-line why for the board, in the member register (decision 486).

    It used to read "{n} rated · learned cutpoints, refit nightly", which was proposal 81's
    suggested wording and false on the first real household: nobody had moved a title, the
    cutpoints learn from `tier_edit` alone (§5.2's tier arm), so they were exactly the prior
    shape, and the board had been moving on every answer rather than nightly (§6.3, "incremental
    immediately"). So it says what is true of this board, in plain words: how much the person has
    told it, and what the letters mean.

    What they mean is decision 508's rule, and the line states it because it is the one thing a
    person needs to read the board: "liked from A up, fine in B, disliked from C down" on §6.3's
    seven, spelled from the person's own labels on any other set. The "typical split" it named
    before was the prior §2.8 of the M3 open points had flagged, and it was what put fine films in
    A and disliked ones in B.

    `compared` counts every question answered, the held-out tenth included, so the number moves
    after every answer and never singles one out (§13, M4.10 finding 16). Decision 209's copy is
    kept for its window: with a fit owed and nothing yet readable the line says so, and gives no
    number and no duration.
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
        #
        # Through `rescale_level` with `k_from=None`, which is the helper's clamp and only its
        # clamp. An `Item` carries a level and nothing about the set it was chosen under, so
        # "unknown board" is the honest argument — `read.items` has already mapped the stored
        # index by cumulative prior mass against the person's current K, using the same helper,
        # and what reaches here is what the column can hold but no set can index. Not a fourth
        # copy of the arithmetic, and not a re-map either: mapping twice would move a level the
        # query already moved. [M4.13 step 15, dd06]
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
            from_model = model_tiers[item.title_id]
            # BOTH HALVES OF THE CHIP ARE THE POSTERIOR'S. `index` is where the row renders,
            # which is the person's drop whenever they made one (§6.3's "stays in the assigned
            # tier") and therefore says nothing about what the model believes. Leading with it
            # paired the drop tier with the model's reach: s = 0.9, σ = 1.0, model tier A,
            # dropped into S rendered "S/B" — two levels the interval does not span, omitting
            # the one it occupies. And the mirror, a drop into the very tier the model reached,
            # suppressed the chip while `straddles()` went on returning non-None, so a
            # queue-eligible title wore no badge and §6.3's one-sentence identity broke.
            #
            # Suppression is therefore `reached == from_model`, which `model.straddle`'s
            # adjacency makes unreachable — kept as a statement of the invariant rather than a
            # branch, because a badge naming one tier twice is the bug proposal 76 recorded.
            #
            # Proposal 71: the two chips compete for the same corner, and tension wins while it
            # holds. `reached` is untouched — eligibility is the predicate, not the string.
            badge = (
                f"{labels[from_model]}/{labels[reached]}"
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
                )
            )
        tiers.append(Tier(index=index, label=labels[index], entries=tuple(entries)))

    # §6.3 lists the tiers ascending (F … S); the board renders them best-first (proposal 82).
    return tuple(reversed(tiers))


__all__ = ["Entry", "Item", "Tier", "build", "straddles", "tension_of", "why_line"]
