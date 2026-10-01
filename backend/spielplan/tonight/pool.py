"""§6.2 step 3's candidate pool: owned titles past the kind/budget/rewatch filters, plain average.

Plain because §0 row 3 measured no rule dominates it; ordered by the Ledger alone (§0 row 4). The
budget is soft and, on a series night, per episode (decision 219).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any

import asyncpg

from spielplan.db import genres as genre_vocab
from spielplan.ledger import ladder, model, observations
from spielplan.tonight import round as round_rules

# One scale for every member, frozen with the pool (decision 477): §5.1 scores are not standardised
# over the owned pool. A rule that changes takes a new marker.
SCALE_MARKER = "rank_normal_sd1"
# sd 1.0: the median round is shortest there, and it is decision 214's `prior_var` (decision 477).
SCALE_SD = 1.0

_NORMAL = NormalDist()

# §6.2 step 1's "not tonight" control (decisions 480, 505): presence predicates over vocabulary-v1
# terms; an unknown term matches nothing, so a re-import can only make a veto remove less.
VETOES: dict[str, tuple[str, tuple[str, ...]]] = {
    "violence": ("violence", ("mood.violent", "themes.violence", "mood.gory")),
    "sexual_violence": ("sexual violence", ("themes.sexual_violence",)),
    "horror": ("horror", ("mood.terrifying", "themes.slasher", "themes.body_horror")),
    "harrowing": (
        "harrowing",
        ("sensibility.harrowing", "sensibility.emotionally_devastating", "mood.devastating",
         "mood.bleak"),
    ),
}
MAX_VETOES = 3

# Both tiers, each by name (§4.1 rules 1-2): a veto wants recall over precision (decision 504).
VETO_TIERS: tuple[str, ...] = ("extracted", "projected")


def veto_terms(keys: Iterable[str]) -> list[str]:
    """The vocabulary terms a set of veto keys removes; an unknown key removes nothing."""
    return sorted({term for key in keys if key in VETOES for term in VETOES[key][1]})


def veto_labels(keys: Iterable[str]) -> list[str]:
    return [VETOES[k][0] for k in VETOES if k in set(keys)]


def _unvetoed(terms: str, version: str, tiers: str) -> str:
    """No vetoed term on `t` in either tier (decision 504); with no vocabulary version, nothing is."""
    return f"""(
        cardinality({terms}::text[]) = 0 OR {version}::text IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM dna_tagged d
             WHERE d.title_id = t.id AND d.version = {version} AND d.tier = ANY({tiers}::text[])
               AND d.term = ANY({terms}::text[])
        )
    )"""

# §6.2 step 1: "the pool admits up to budget + 40 min"; the spec's number, not a tunable.
BUDGET_GRACE_MIN = 40

# 54h: a series' `runtime_min` is per episode, and every label with a number says so (decision 219).
KIND_SERIES = "series"
PER_EPISODE = " per episode"


@dataclass(frozen=True)
class Seat:
    """One participant of a session, as the pool needs them.

    `is_member` asks the taste question (a grid profile, §6.2 step 3), not the account question.
    """

    participant_id: int
    user_id: int | None
    is_member: bool


@dataclass(frozen=True)
class Candidate:
    """One title the evening could resolve to; `scores` is keyed by participant id, not user id."""

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
    """§6.2 step 3's "plain average of member Ledger scores": unweighted, and unweightable."""
    if not scores:
        return 0.0
    return sum(scores.values()) / len(scores)


def rank_normal(scores: Mapping[int, float], *, sd: float = SCALE_SD) -> dict[int, float]:
    """One member's scores over tonight's pool, as normal quantiles of their own ranks.

    Φ⁻¹((r − 0.5)/n)·sd, ties by title id; monotone, so only the spread changes (decision 477).
    """
    items = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    if n < 2:
        return {t: 0.0 for t, _ in items}
    return {t: sd * _NORMAL.inv_cdf((i + 0.5) / n) for i, (t, _) in enumerate(items)}


def score_for_seats(
    member_scores: Mapping[int, float], seats: Sequence[Seat]
) -> dict[int, float]:
    """Keep only the seats that contribute a taste term.

    A guest is omitted, not zeroed: zeroing would shift the scale the D threshold is read on.
    """
    contributing = {s.participant_id for s in seats if s.is_member}
    return {pid: v for pid, v in member_scores.items() if pid in contributing}


def order(candidates: Iterable[Candidate]) -> list[Candidate]:
    """The pool, best first; ties by `title_id` so two builds over the same numbers agree."""
    return sorted(candidates, key=lambda c: (-c.group_score, c.title_id))


# --- the soft budget ---------------------------------------------------------------------


def admits(*, runtime_min: int | None, budget_min: int) -> bool:
    """§6.2 step 1's soft bound; an unknown runtime is admitted, not treated as long."""
    if runtime_min is None:
        return True
    return runtime_min <= budget_min + BUDGET_GRACE_MIN


def over_budget_by(*, runtime_min: int | None, budget_min: int) -> int | None:
    """How far past the slider a title runs, or None when it fits (or cannot be measured).

    Measured from the budget the person set, never from the hidden +40 bound.
    """
    if runtime_min is None or runtime_min <= budget_min:
        return None
    return runtime_min - budget_min


def fit_line(*, runtime_min: int | None, budget_min: int, kind: str) -> str:
    """§6.2 step 8's "Fits your time" / "21 min over", with 54h's " per episode" on a series.

    `kind` is required so the qualifier cannot go missing where a number is stated.
    """
    if runtime_min is None:
        return "Runtime unknown"
    over = over_budget_by(runtime_min=runtime_min, budget_min=budget_min)
    if over is None:
        return "Fits your time"
    return f"{over} min over{PER_EPISODE if kind == KIND_SERIES else ''}"


def with_budget(candidates: Iterable[Candidate], *, budget_min: int) -> list[Candidate]:
    """Apply the budget and stamp the label in one pass, so the two cannot disagree."""
    import dataclasses

    out = []
    for c in candidates:
        if not admits(runtime_min=c.runtime_min, budget_min=budget_min):
            continue
        out.append(
            dataclasses.replace(
                c,
                over_budget_min=over_budget_by(runtime_min=c.runtime_min, budget_min=budget_min),
                # The candidate's own kind, which 0013 makes the session's (§4.1 rule 5).
                fit_line=fit_line(
                    runtime_min=c.runtime_min, budget_min=budget_min, kind=c.kind
                ),
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
    vetoed_terms: Sequence[str] = (),
    dna_version: str | None = None,
) -> list[Candidate]:
    """§6.2 step 3's pool, ordered: owned, of the session's kind, not seen by every participant
    (unless rewatches), within budget, scored under `bundle_version` (§10), and free of vetoed
    terms in either tier (decisions 480, 504). With no vocabulary version nothing is vetoed.
    """
    member_user_ids = [s.user_id for s in seats if s.is_member and s.user_id is not None]
    by_user = {s.user_id: s.participant_id for s in seats if s.user_id is not None}
    if not member_user_ids:
        return []

    rows = await conn.fetch(
        f"""
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
           AND {_unvetoed("$5", "$6", "$7")}
        """,
        member_user_ids, kind, bundle_version, include_rewatches,
        list(vetoed_terms), dna_version, list(VETO_TIERS),
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

    # Every seated member must have scored it: a mean of one score is not agreement.
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


# --- the films a seat is asked about (§6.2 step 4) --------------------------------------------

_FILM = "t.id AS title_id, t.kind, t.name, t.year, t.runtime_min, t.poster_path"


async def liked_films(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    vetoed_terms: Sequence[str] = (),
    dna_version: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """A member's own seen films of the kind placed in the liked band of their ladder (decision 539),
    none carrying a vetoed term, each with its step; and that band's lowest word, for the no-round line."""
    tier_set = await observations.tier_set_of(conn, user_id=user_id, kind=kind)
    k = len(tier_set)
    placed = await ladder.placements(conn, user_id=user_id, kind=kind)
    liked = {t: step for t, step in placed.items() if model.verdict_class_of_tier(step, k) == 2}
    rows = await conn.fetch(
        f"""
        SELECT {_FILM}
          FROM title t
          JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1 AND ut.state = 'seen'
         WHERE t.id = ANY($2::int[]) AND t.kind = $3 AND {_unvetoed("$4", "$5", "$6")}
        """,
        user_id, list(liked), kind, list(vetoed_terms), dna_version, list(VETO_TIERS),
    )
    word = observations.tier_words(tier_set)[int(model.verdict_tiers(k)[2][0])]
    return [{**dict(r), "step": liked[r["title_id"]]} for r in rows], word


async def well_known_films(
    conn: asyncpg.Connection,
    *,
    kind: str,
    bundle_version: str,
    vetoed_terms: Sequence[str] = (),
    dna_version: str | None = None,
) -> list[dict[str, Any]]:
    """A guest's films (decision 539): owned, of the kind, with at least 30,000 crowd ratings, none
    carrying a vetoed term. A guest need not have seen them, so they carry no step."""
    rows = await conn.fetch(
        f"""
        SELECT {_FILM}
          FROM title t
          JOIN title_prior tp ON tp.title_id = t.id AND tp.bundle_version = $2
         WHERE t.is_owned AND t.kind = $1 AND tp.item_n >= $3 AND {_unvetoed("$4", "$5", "$6")}
        """,
        kind, bundle_version, round_rules.WELL_KNOWN_CROWD,
        list(vetoed_terms), dna_version, list(VETO_TIERS),
    )
    return [{**dict(r), "step": None} for r in rows]


async def seen_among(conn: asyncpg.Connection, *, user_id: int, title_ids: Sequence[int]) -> set[int]:
    rows = await conn.fetch(
        "SELECT title_id FROM user_title WHERE user_id = $1 AND title_id = ANY($2::int[]) "
        "AND state = 'seen'",
        user_id, list(title_ids),
    )
    return {r["title_id"] for r in rows}


def reach(stable: Mapping[int, float], *, seen: Iterable[int] = ()) -> list[int]:
    """The candidates the mood may re-rank: a seat's top 30 unseen by stable taste (decision 550)."""
    skip = set(seen)
    ranked = sorted((t for t in stable if t not in skip), key=lambda t: (-stable[t], t))
    return ranked[: round_rules.TILT_REACH]


# Genres a pair card names; two, since a third wraps a half-phone-wide card.
CARD_GENRES = 2


async def genres_of(conn: asyncpg.Connection, title_ids: Sequence[int]) -> dict[int, list[str]]:
    """Each title's genres as a card names them (decision 473's vocabulary); never a DNA term."""
    rows = await conn.fetch(
        "SELECT title_id, lower(genre) AS genre FROM title_genre "
        "WHERE title_id = ANY($1::int[]) AND source <> ALL($2::text[])",
        list(title_ids), list(genre_vocab.EXCLUDED_SOURCES),
    )
    raw: dict[int, set[str]] = {}
    for r in rows:
        raw.setdefault(r["title_id"], set()).add(r["genre"])
    return {t: genre_vocab.facet(labels)[:CARD_GENRES] for t, labels in raw.items()}


__all__ = [
    "BUDGET_GRACE_MIN",
    "CARD_GENRES",
    "KIND_SERIES",
    "MAX_VETOES",
    "PER_EPISODE",
    "SCALE_MARKER",
    "SCALE_SD",
    "VETOES",
    "VETO_TIERS",
    "Candidate",
    "Seat",
    "admits",
    "build",
    "fit_line",
    "genres_of",
    "group_score",
    "liked_films",
    "order",
    "over_budget_by",
    "rank_normal",
    "reach",
    "score_for_seats",
    "seen_among",
    "veto_labels",
    "veto_terms",
    "well_known_films",
    "with_budget",
]
