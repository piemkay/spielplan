"""§6.2 step 3's candidate pool. Spec v2.1 §6.2 steps 1 and 3, §0 row 3, §4.1 rule 5, §5.1, §10.

    "**Candidate pool (internal — never shown as a step):** owned titles passing the
     kind/budget/rewatch filters, ranked by the **plain average** of member Ledger scores
     (measured: nothing dominates averaging; dominance rules cost −0.012). Guests contribute no
     taste term unless they have a grid profile."

Two halves, split by what they can be falsified with. The **arithmetic** — which title outranks
which, and how far over budget one runs — is a function of numbers and lives at the top of this
file, pure. The **membership** is a query and lives at the bottom, because §7.2 re-derives
`is_owned` from Jellyfin and a stale flag is exactly the failure a pure test cannot see.

PLAIN, AND WHAT IT RULES OUT. §0 row 3 is the measurement: "no aggregation rule dominates plain
averaging, and dominance rules cost −0.012" against a documented noise floor of 0.003–0.008. So
the mean here is not a default that a cleverer rule may later replace — max-min and the Nash
product were the two v1.1 proposed, and both are *measured worse*. `group_score` therefore takes
a bare map of scores and returns their mean, with no seat weights and nowhere to put one.

AND WHAT ORDERS IT IS THE LEDGER ALONE. §0 row 4: the stored mood profile is worth **0.000** for
choose-tonight, and within-evening re-ranking is worth 0.000 as well. The tilt is something the
*round* learns and adds to a participant's tonight score; it never reaches the prior the round
starts from, which is why nothing in this module accepts one.

THE BUDGET IS SOFT, AND SAYS SO. §6.2 step 1: "a **runtime budget slider** (soft — the pool
admits up to budget + 40 min; over-budget results are labelled 'runs N min over')". Admission
and labelling are one pass (`with_budget`) so the two cannot disagree about where the boundary
is, and N is measured from the budget the person set rather than from the +40 bound they never
saw.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

# §6.2 step 1: "the pool admits up to budget + 40 min". The spec's number, not a tunable — it
# is not a constant of the §5.2 recipe, so §4.3's `ledger_hyperparams.json` is not where it
# belongs (the same reasoning `rank/queue.py` applies to §6.3's 70/20/10 shares).
BUDGET_GRACE_MIN = 40


@dataclass(frozen=True)
class Seat:
    """One participant of a session, as the pool needs them.

    `is_member` is the taste question and not the account question: §6.2 step 3 says "Guests
    contribute no taste term **unless they have a grid profile**", so a persistent guest who
    has filled in the 60-title grid is a member for this purpose and an ephemeral one is not.
    The grid itself is M7 (§12), so today the two coincide — but the pool asks the question it
    means, so that M7 changes one predicate rather than every read.
    """

    participant_id: int
    user_id: int | None
    is_member: bool


@dataclass(frozen=True)
class Candidate:
    """One title the evening could resolve to.

    `scores` is per **participant id**, not per user id: a session is seats, and the round's
    arithmetic is over seats. `over_budget_min` and `fit_line` are stamped by `with_budget`
    once, at session open, because three surfaces render them (the pair card, the result card,
    solo) and three recomputations drift.
    """

    title_id: int
    kind: str
    name: str
    runtime_min: int | None
    scores: Mapping[int, float]
    over_budget_min: int | None = None
    fit_line: str = ""
    year: int | None = None
    poster_path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def group_score(self) -> float:
        return group_score(self.scores)


# --- the plain average -------------------------------------------------------------------


def group_score(scores: Mapping[int, float]) -> float:
    """§6.2 step 3's "plain average of member Ledger scores".

    Unweighted, and structurally unweightable: there is no second argument. A seat's label
    count, its seniority and its hosting the room are all irrelevant, which is what "plain"
    means and what §0 row 3 measured as undominated.
    """
    if not scores:
        return 0.0
    return sum(scores.values()) / len(scores)


def score_for_seats(
    member_scores: Mapping[int, float], seats: Sequence[Seat]
) -> dict[int, float]:
    """Keep only the seats that contribute a taste term.

    A guest is *omitted*, not zeroed. Zeroing looks identical on a single title and is a
    different rule: it drags every candidate toward the bottom by the same amount, which
    preserves the order but destroys the scale the D threshold (§6.2 step 5) is measured on.
    """
    contributing = {s.participant_id for s in seats if s.is_member}
    return {pid: v for pid, v in member_scores.items() if pid in contributing}


def order(candidates: Iterable[Candidate]) -> list[Candidate]:
    """The pool, best first.

    Ties break by `title_id` rather than by input order: the pool is computed once at session
    open and carried through the round (§6.2 step 6 — nothing re-ranks within the evening), so
    a second build over the same numbers has to produce the same list, and a stable sort over
    an unordered query result is not stable at all.
    """
    return sorted(candidates, key=lambda c: (-c.group_score, c.title_id))


# --- the soft budget ---------------------------------------------------------------------


def admits(*, runtime_min: int | None, budget_min: int) -> bool:
    """§6.2 step 1's soft bound.

    A title of unknown runtime is admitted. `title.runtime_min` is nullable and the corpus has
    gaps; the budget is soft by design, so a title nobody can measure is not evidence that it
    runs long, and dropping it would remove a watchable film from the evening over a missing
    metadata field.
    """
    if runtime_min is None:
        return True
    return runtime_min <= budget_min + BUDGET_GRACE_MIN


def over_budget_by(*, runtime_min: int | None, budget_min: int) -> int | None:
    """How far past the slider a title runs, or None when it fits (or cannot be measured).

    Measured from the budget the person set, never from the +40 admission bound they never
    saw: the label exists to make the softness legible, and a label counting from a hidden
    number would make it less so.
    """
    if runtime_min is None or runtime_min <= budget_min:
        return None
    return runtime_min - budget_min


def fit_line(*, runtime_min: int | None, budget_min: int) -> str:
    """§6.2 step 7's two branches, verbatim: "fits your 130 min" / "runs 21 min over"."""
    if runtime_min is None:
        return "runtime unknown"
    over = over_budget_by(runtime_min=runtime_min, budget_min=budget_min)
    if over is None:
        return f"fits your {budget_min} min"
    return f"runs {over} min over"


def with_budget(candidates: Iterable[Candidate], *, budget_min: int) -> list[Candidate]:
    """Apply the budget and stamp the label in one pass.

    One pass because a title admitted by one rule and labelled by another that disagrees about
    the boundary is the failure mode a soft budget invites — and it is invisible until someone
    reads "fits your 130 min" on a 171-minute film.
    """
    import dataclasses

    out = []
    for c in candidates:
        if not admits(runtime_min=c.runtime_min, budget_min=budget_min):
            continue
        out.append(
            dataclasses.replace(
                c,
                over_budget_min=over_budget_by(runtime_min=c.runtime_min, budget_min=budget_min),
                fit_line=fit_line(runtime_min=c.runtime_min, budget_min=budget_min),
            )
        )
    return out


# --- membership: the query ---------------------------------------------------------------


async def build(
    conn: asyncpg.Connection,
    *,
    seats: Sequence[Seat],
    kind: str,
    budget_min: int,
    include_rewatches: bool,
    bundle_version: str,
) -> list[Candidate]:
    """§6.2 step 3's pool, ordered.

    Four filters, and each one is a sentence:

      * **owned** — "owned titles passing the …". §7.2 re-derives `is_owned` from Jellyfin, so
        an unowned title on the winner card is a Play-on-Jellyfin CTA that opens nothing.
      * **kind** — §4.1 rule 5 binds every ranking surface, and an evening resolves to ONE
        title, so the session picked a side rather than rendering two sections.
      * **rewatch** — §6.2 step 1's default excludes titles *every* participant has seen. The
        quantifier is the rule: "any participant has seen" would strip the household's shared
        favourites out of every evening, and is the only reading under which a rewatch toggle
        would be off by default.
      * **budget** — applied in `with_budget` above, after the query, because the label is
        arithmetic and the admission bound is the same arithmetic.

    §10's invariant binds `bundle_version` into the read: a score from a superseded basis is
    not returned as a stale number, it is not returned at all.
    """
    member_user_ids = [s.user_id for s in seats if s.is_member and s.user_id is not None]
    by_user = {s.user_id: s.participant_id for s in seats if s.user_id is not None}
    if not member_user_ids:
        return []

    rows = await conn.fetch(
        """
        SELECT t.id AS title_id, t.kind, t.name, t.year, t.runtime_min, t.poster_path,
               us.user_id, us.score
          FROM user_score us
          JOIN title t ON t.id = us.title_id
         WHERE us.user_id = ANY($1::bigint[])
           AND us.kind = $2
           AND us.bundle_version = $3
           AND t.is_owned
           AND (
                $4::boolean
                OR EXISTS (
                    SELECT 1 FROM unnest($1::bigint[]) AS m(user_id)
                     WHERE NOT EXISTS (
                        SELECT 1 FROM user_title ut
                         WHERE ut.user_id = m.user_id AND ut.title_id = t.id
                           AND ut.state = 'seen'
                     )
                )
           )
        """,
        member_user_ids, kind, bundle_version, include_rewatches,
    )

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(
            row["title_id"],
            {
                "title_id": row["title_id"],
                "kind": row["kind"],
                "name": row["name"],
                "year": row["year"],
                "runtime_min": row["runtime_min"],
                "poster_path": row["poster_path"],
                "scores": {},
            },
        )
        seat_id = by_user.get(row["user_id"])
        if seat_id is not None:
            entry["scores"][seat_id] = float(row["score"])

    # A title only one member has a score for is not comparable to one both do: the mean of a
    # single score is that score, which would let a title nobody else can see outrank the
    # household's actual agreement. Every seated member must have scored it.
    wanted = {by_user[u] for u in member_user_ids}
    candidates = [
        Candidate(
            title_id=e["title_id"], kind=e["kind"], name=e["name"], year=e["year"],
            runtime_min=e["runtime_min"], poster_path=e["poster_path"],
            scores=score_for_seats(e["scores"], seats),
        )
        for e in grouped.values()
        if set(e["scores"]) >= wanted
    ]
    return order(with_budget(candidates, budget_min=budget_min))


__all__ = [
    "BUDGET_GRACE_MIN",
    "Candidate",
    "Seat",
    "admits",
    "build",
    "fit_line",
    "group_score",
    "order",
    "over_budget_by",
    "score_for_seats",
    "with_budget",
]
