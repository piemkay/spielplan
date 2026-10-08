"""The content head: §5.1's third term, fitted per member over facts about the film (decision 568).

    score_u(t) = μ_u + (1−w_u)·[(1−β_u)·z(b(t)) + β_u·⟨v_u, d(t)⟩] + w_u·con_u(t)

The first two terms are `foldin`'s and are untouched. `con_u` is a ridge over one shared feature
space: the DNA vocabulary, canonical genre, decade, a few meta columns, the crowd prior, the
platform score, how much the member likes the people who made it, and `cf` itself.
Until this head existed the personal score read a 64-d crowd coordinate and nothing about the film,
so the imported DNA could say "this is like that" and never "you will like this".

**One feature space for every member.** The column order, the scalings and the recipe are identical
for everyone; only the weights differ, and `Layout.digest` pins the layout so a fit from another one
is refitted rather than served. That is what makes two members' weights comparable, and `match`
reads them that way (decision 570).

`w_u` is chosen per member by held-out Spearman under `foldin.NOISE_FLOOR`: an improvement inside
pipeline variance is a tie, and a tie must not buy personalisation (§0). At `w_u = 0` the score is
the one `foldin` already serves, to the bit, so a member this head cannot help is not harmed by it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

log = logging.getLogger("spielplan.scoring.content")

# Wider than the fold-in's: this design has ~600 columns against 64, and the top of the grid is
# what holds a sparse DNA block down when a member has few labels.
LAMBDA_GRID: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0)

# w_u above.
WEIGHT_GRID: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

# A ceiling, for §5.1's own reason for BETA_MAX: at a hundred labels no single cross-validation can
# tell a real gain of a few hundredths from the noise of searching 77 cells, so the fold-in and the
# crowd prior keep half the score whatever the search found. A head admitted in error is then
# bounded rather than decisive.
WEIGHT_MAX = 0.5

# And the weight is gated on evidence the way §5.1 gates a coordinate: n/(n+k). The search can only
# ever be as trustworthy as the number of answers behind it, so a member with 80 placements buys at
# most 0.4 of the score and one with 400 approaches the ceiling. This is the gate that makes "a tie
# must not buy personalisation" hold at small n, where the noise floor alone cannot carry it.
WEIGHT_GATE_K = 120.0

# The search is over 77 cells and the statistic is measured on ~100 labels, where one pass has a
# sampling sd an order of magnitude above the noise floor. Averaging the table over several fold
# assignments is what makes the gate a decision about the member rather than about the shuffle.
CV_REPEATS = 6

MIN_LABELS_FOR_CV = 8   # below this the design is fitted but never trusted with weight
LOO_BELOW = 25          # leave-one-out under this many labels, 5 folds at or above, as §5.1 does

# Role weights for the people column. Who directed it carries most: on this household's own
# placements a director with two or more films predicts the step better than any other credit.
ROLE_WEIGHT: dict[str, float] = {"director": 3.0, "writer": 1.5, "composer": 1.0, "dp": 1.0}
CAST_DEPTH = 5          # billing orders 0..4 count, decaying
PERSON_PRIOR = 2.0      # empirical-Bayes denominator: one film by one director moves little

DECADES: tuple[int, ...] = (1920, 1930, 1940, 1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020)

# Stamped on every fit; a fit carrying another value reads as absent rather than being served.
FEATURES_VERSION = "content-2"

# The member-relative tail of the design, always last and always in this order. These are the
# columns whose meaning depends on whose fit it is, so `match` leaves them out.
MEMBER_KEYS: tuple[str, ...] = ("people:affinity", "crowd:cf")


@dataclass(frozen=True)
class Layout:
    """The shared column order. `digest` pins it; the member tail is the last `MEMBER_KEYS`."""

    keys: tuple[str, ...]

    @property
    def member_from(self) -> int:
        return len(self.keys) - len(MEMBER_KEYS)

    @property
    def width(self) -> int:
        return len(self.keys)

    @property
    def digest(self) -> str:
        raw = f"{FEATURES_VERSION}\n" + "\n".join(self.keys)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Features:
    """The title half of the design, built once per run and read by every member's fit.

    Member-independent by construction: the same rows serve every member, which is what lets their
    fitted weights be compared at all.
    """

    layout: Layout
    title_ids: np.ndarray                      # (n,) int64, ascending
    x: np.ndarray                              # (n, member_from) float64, title columns only
    people: Mapping[int, Sequence[tuple[int, float]]] = field(default_factory=dict)
    row_of: Mapping[int, int] = field(default_factory=dict)

    def rows(self, title_ids: Sequence[int]) -> np.ndarray:
        return np.asarray([self.row_of[int(t)] for t in title_ids], dtype=np.int64)


@dataclass(frozen=True, eq=False)
class Fit:
    """One (user, kind) content head."""

    w: np.ndarray            # (width,) float64, already divided by con_sd
    weight: float            # w_u: how much of the score this head carries
    lam: float
    cv_rho: float            # held-out top-band agreement of the blended score at (λ, w)
    base_rho: float          # the same at w = 0 — the fold-in alone, the thing to beat
    con_sd: float            # the PRE-normalisation sd of the raw prediction over the reference
    con_mean: float
    digest: str
    label_count: int
    used: int = 0
    dropped: int = 0
    folds: int = 0

    @property
    def gain(self) -> float:
        return self.cv_rho - self.base_rho

    def as_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight, "lambda": self.lam, "cv_rho": round(self.cv_rho, 4),
            "base_rho": round(self.base_rho, 4), "gain": round(self.gain, 4),
            "con_sd": self.con_sd, "digest": self.digest,
            "label_count": self.label_count, "used": self.used, "dropped": self.dropped,
            "folds": self.folds,
        }


@dataclass(frozen=True)
class Match:
    """Two members read against each other through their own fitted weights (decision 570)."""

    agreement: float                               # Spearman of the two scores over the reference
    shared: tuple[tuple[str, float, float], ...]   # columns both pull the same way, strongest first
    opposed: tuple[tuple[str, float, float], ...]  # columns they pull against each other
    comparable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "agreement": round(self.agreement, 4),
            "shared": [[k, round(a, 4), round(b, 4)] for k, a, b in self.shared],
            "opposed": [[k, round(a, 4), round(b, 4)] for k, a, b in self.opposed],
            "comparable": self.comparable,
        }


# --- the arithmetic (numpy only) ---------------------------------------------------------------


def layout_for(terms: Sequence[str], genres: Sequence[str]) -> Layout:
    """The shared column order: sorted vocabularies, then the fixed blocks, then the member tail."""
    keys = [f"dna:{t}" for t in sorted(terms)]
    keys += [f"genre:{g}" for g in sorted(genres)]
    keys += [f"decade:{d}" for d in DECADES]
    keys += ["meta:runtime", "meta:english", "meta:votes", "meta:platform", "crowd:prior"]
    keys += list(MEMBER_KEYS)
    return Layout(keys=tuple(keys))


def person_affinity(
    people: Mapping[int, Sequence[tuple[int, float]]],
    labelled: Sequence[tuple[int, float]],
    title_ids: Sequence[int],
) -> np.ndarray:
    """One column: how much this member has liked the people who made each title.

    Empirical-Bayes against the member's own mean step, so a director with one film barely moves and
    one with seven moves a lot. Read from `labelled` alone, which is what keeps it honest inside a
    cross-validation fold.
    """
    if not labelled:
        return np.zeros(len(title_ids))
    mu = float(np.mean([step for _t, step in labelled]))
    acc: dict[int, float] = {}
    cnt: dict[int, float] = {}
    for title_id, step in labelled:
        for person, weight in people.get(int(title_id), ()):
            acc[person] = acc.get(person, 0.0) + weight * (step - mu)
            cnt[person] = cnt.get(person, 0.0) + weight
    affinity = {p: acc[p] / (cnt[p] + PERSON_PRIOR) for p in acc}
    out = np.zeros(len(title_ids), dtype=np.float64)
    for i, title_id in enumerate(title_ids):
        num = den = 0.0
        for person, weight in people.get(int(title_id), ()):
            value = affinity.get(person)
            if value is not None:
                num += weight * value
                den += weight
        if den > 0.0:
            out[i] = num / den
    return out


def design(
    features: Features,
    rows: np.ndarray,
    *,
    affinity: np.ndarray,
    cf: np.ndarray,
) -> np.ndarray:
    """The title columns for `rows`, with this member's tail appended in `MEMBER_KEYS` order."""
    return np.hstack([
        features.x[rows],
        np.asarray(affinity, dtype=np.float64).reshape(-1, 1),
        np.asarray(cf, dtype=np.float64).reshape(-1, 1),
    ])


def ridge(x: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """Ridge weights, solved in the dual because there are always far fewer labels than columns.

    w = Xᵀ(XXᵀ + λI)⁻¹y is the same vector as (XᵀX + λI)⁻¹Xᵀy and costs an n×n solve instead of a
    600×600 one, which is what keeps this inside §5.3's seconds with leave-one-out below 25 labels.
    """
    xd = np.asarray(x, dtype=np.float64)
    yd = np.asarray(y, dtype=np.float64)
    gram = xd @ xd.T + float(lam) * np.eye(xd.shape[0])
    return xd.T @ np.linalg.solve(gram, yd)


def _fold_assignment(n: int, seed: int) -> np.ndarray:
    """Leave-one-out below 25 labels, 5 folds at or above — §5.1's rule, so the two fits agree."""
    if n < LOO_BELOW:
        return np.arange(n)
    folds = np.arange(n) % 5
    np.random.default_rng(seed).shuffle(folds)
    return folds


def fit_user(
    labels: Sequence[tuple[int, int]],
    features: Features,
    base: Mapping[int, float],
    cf: Mapping[int, float],
    *,
    seed: int = 0,
    n_levels: int = 6,
) -> Fit:
    """Fit one (user, kind)'s content head and choose how much of the score it carries.

    `base` is the fold-in's score per title and `cf` its personal half, both over the whole
    reference population, so the standardisation here does not depend on what the member has rated.
    `n_levels` is the member's own ladder size; the top band is its top two steps (decision 561).
    """
    from spielplan.scoring.foldin import NOISE_FLOOR, spearman

    layout = features.layout
    ordered = sorted(labels, key=lambda pair: int(pair[0]))
    kept = [(int(t), float(step)) for t, step in ordered if int(t) in features.row_of]
    dropped = len(labels) - len(kept)
    n = len(kept)
    if n == 0:
        return Fit(
            w=np.zeros(layout.width), weight=0.0, lam=LAMBDA_GRID[-1], cv_rho=0.0, base_rho=0.0,
            con_sd=0.0, con_mean=0.0, digest=layout.digest, label_count=len(labels), used=0,
            dropped=dropped,
        )

    title_ids = [t for t, _s in kept]
    y_raw = np.asarray([s for _t, s in kept], dtype=np.float64)
    rows = features.rows(title_ids)

    # Centred on the reference population, as §5.1's halves are, not on what the member rated: a
    # score must not depend on the filter the member has typed, and the centre must be one shared
    # thing for two members' weights to mean the same (decision 570).
    ref_rows = np.arange(features.title_ids.size)
    ref_ids = [int(t) for t in features.title_ids]
    ref_affinity = person_affinity(features.people, kept, ref_ids)
    ref_cf = np.asarray([cf.get(t, 0.0) for t in ref_ids], dtype=np.float64)
    ref_x = design(features, ref_rows, affinity=ref_affinity, cf=ref_cf)
    ref_x -= ref_x.mean(axis=0)

    x = ref_x[rows]
    mu = float(y_raw.mean())
    y = y_raw - mu
    z_base = np.asarray([base.get(t, 0.0) for t in title_ids], dtype=np.float64)

    lam, weight, cv_rho, base_rho, folds = LAMBDA_GRID[-1], 0.0, 0.0, 0.0, 0
    if n >= MIN_LABELS_FOR_CV:
        lam, weight, cv_rho, base_rho, folds = _cross_validate(
            x, y, y_raw, z_base, ref_x, seed=seed, spearman=spearman, noise_floor=NOISE_FLOOR,
            n_levels=n_levels,
        )

    w = ridge(x, y, lam)
    raw = ref_x @ w
    con_sd = float(raw.std())
    if con_sd < 1e-9:
        # The head orders nothing over the reference; say so with a weight of zero.
        return Fit(
            w=np.zeros(layout.width), weight=0.0, lam=lam, cv_rho=base_rho,
            base_rho=base_rho, con_sd=0.0, con_mean=0.0, digest=layout.digest,
            label_count=len(labels), used=n, dropped=dropped, folds=folds,
        )
    return Fit(
        w=w / con_sd, weight=weight, lam=lam, cv_rho=cv_rho, base_rho=base_rho,
        con_sd=con_sd, con_mean=float(raw.mean() / con_sd), digest=layout.digest,
        label_count=len(labels), used=n, dropped=dropped, folds=folds,
    )


def top_band_auc(score: np.ndarray, band: np.ndarray) -> float:
    """P(a film in the member's top two steps outscores one below them). 0.5 is a coin flip.

    The product question is "would they place this at A or S" (decision 568), so that is what the
    search is scored on. It is also far steadier than Spearman at these label counts, because it
    reads every pair rather than the exact position of the C/D boundary nobody asks about.
    """
    s = np.asarray(score, dtype=np.float64)
    pos = np.asarray(band, dtype=bool)
    n1 = int(pos.sum())
    n0 = int((~pos).sum())
    if n1 == 0 or n0 == 0:
        return 0.5
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(s.size, dtype=np.float64)
    ranks[order] = np.arange(1, s.size + 1, dtype=np.float64)
    sorted_s = s[order]
    i = 0
    while i < s.size:                               # tie-average, as a coin flip on a tie
        j = i
        while j + 1 < s.size and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i : j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _held_out(
    x: np.ndarray, y: np.ndarray, ref_x: np.ndarray, lam: float, fold: np.ndarray
) -> np.ndarray:
    """One fold assignment's out-of-fold `con`, standardised as serving standardises it."""
    z = np.zeros(x.shape[0])
    for f in range(int(fold.max()) + 1):
        held = fold == f
        if held.all():
            continue
        w_f = ridge(x[~held], y[~held], lam)
        sd_f = float((ref_x @ w_f).std())
        if sd_f >= 1e-9:
            z[held] = x[held] @ w_f / sd_f
    return z


def _cross_validate(
    x: np.ndarray,
    y: np.ndarray,
    y_raw: np.ndarray,
    z_base: np.ndarray,
    ref_x: np.ndarray,
    *,
    seed: int,
    spearman: Any,
    noise_floor: float,
    n_levels: int,
) -> tuple[float, float, float, float, int]:
    """Choose (λ, w) by held-out agreement on the member's own top band, averaged over `CV_REPEATS`
    fold assignments.

    The baseline is w = 0, which is the fold-in's own score. A cell that does not clear it by more
    than the noise floor returns weight 0: the head is fitted, recorded, and not served. Averaging
    is what makes that a decision about the member rather than about one shuffle, and `WEIGHT_MAX`
    bounds what a cell admitted in error can do.
    """
    n = x.shape[0]
    band = y_raw >= float(n_levels - 2)
    degenerate = not band.any() or band.all()

    def agreement(score: np.ndarray) -> float:
        # With nobody in the top band yet there is no band to read, so fall back to the ordering.
        return spearman(score, y_raw) if degenerate else top_band_auc(score, band)

    # The two caps are applied to the GRID, not to the winner, so that the agreement recorded with
    # a fit is the agreement at the weight actually served.
    cap = min(WEIGHT_MAX, n / (n + WEIGHT_GATE_K))
    weights = [w for w in WEIGHT_GRID if w <= cap] or [0.0]

    folds = [_fold_assignment(n, seed + r) for r in range(CV_REPEATS)]
    n_folds = int(folds[0].max()) + 1
    totals: dict[tuple[float, float], float] = {}
    for lam in LAMBDA_GRID:
        for fold in folds:
            z_con = _held_out(x, y, ref_x, lam, fold)
            for weight in weights:
                key = (lam, weight)
                totals[key] = totals.get(key, 0.0) + agreement(
                    (1.0 - weight) * z_base + weight * z_con
                )
    table = {k: v / len(folds) for k, v in totals.items()}

    at_zero = {round(v, 12) for (_lam, weight), v in table.items() if weight == 0.0}
    assert len(at_zero) == 1, "w = 0 must not depend on λ"
    base = at_zero.pop()
    best = max(table.values())
    if best - base <= noise_floor:
        return LAMBDA_GRID[-1], 0.0, base, base, n_folds

    # Within noise of the best, prefer the smallest weight, then the strongest penalty at it.
    within = [k for k, v in table.items() if best - v <= noise_floor]
    weight = min(k[1] for k in within)
    lam = max(k[0] for k in within if k[1] == weight)
    return lam, weight, table[(lam, weight)], base, n_folds


def score_many(
    fit: Fit,
    features: Features,
    base: Mapping[int, float],
    cf: Mapping[int, float],
    labelled: Sequence[tuple[int, float]],
    title_ids: Sequence[int],
) -> list[tuple[int, float, float]]:
    """(title_id, score, con) for `title_ids`, in the order given, in one matvec.

    `title_ids` is the fold-in's own reference order rather than this design's, so a title the
    design has no row for keeps its fold-in score and a `con` of zero instead of losing a score.
    At `fit.weight == 0` every row is `base` unchanged, which is the §5.1 score the fold-in wrote.
    """
    ids = [int(t) for t in title_ids]
    base_arr = np.asarray([base.get(t, 0.0) for t in ids], dtype=np.float64)
    if fit.weight <= 0.0 or fit.con_sd < 1e-9:
        return [(t, float(s), 0.0) for t, s in zip(ids, base_arr, strict=True)]

    affinity = person_affinity(features.people, labelled, [int(t) for t in features.title_ids])
    ref_cf = np.asarray(
        [cf.get(int(t), 0.0) for t in features.title_ids], dtype=np.float64
    )
    ref_rows = np.arange(features.title_ids.size)
    ref_x = design(features, ref_rows, affinity=affinity, cf=ref_cf)
    con_ref = (ref_x - ref_x.mean(axis=0)) @ fit.w

    con = np.zeros(len(ids), dtype=np.float64)
    score = base_arr.copy()
    for i, title_id in enumerate(ids):
        row = features.row_of.get(title_id)
        if row is None:
            continue
        con[i] = con_ref[row]
        score[i] = (1.0 - fit.weight) * base_arr[i] + fit.weight * con[i]
    return [(t, float(s), float(c)) for t, s, c in zip(ids, score, con, strict=True)]


def match(
    a: Fit,
    b: Fit,
    layout: Layout,
    scores: tuple[Sequence[float], Sequence[float]] | None = None,
    *,
    top: int = 8,
) -> Match:
    """How alike two members' taste is, read off the one shared feature space (decision 570).

    The headline is the agreement between what the two heads predict over the same reference
    population: layout-independent, and the thing a shared shelf actually needs. The columns behind
    it come from the weights, each normalised to unit length first so that one member having a
    louder fit than the other does not decide the comparison. The member-relative tail is left out:
    "older than my own band" does not mean the same thing in two different fits.

    One column is a PARTIAL effect, not a preference. A ridge weight is what that column adds once
    the other six hundred have spoken, so a member who loves epic fantasy can carry a negative
    `genre:Fantasy` because the DNA terms already said it. Read these as where two members diverge
    having agreed on everything else, and leave the "do you like fantasy" question to §6.5's chart,
    which reads the placements directly (decision 549).
    """
    from spielplan.scoring.foldin import spearman

    agreement = 0.0
    if scores is not None and len(scores[0]) == len(scores[1]) and len(scores[0]) > 1:
        agreement = spearman(np.asarray(scores[0]), np.asarray(scores[1]))
    if not (a.digest == b.digest == layout.digest):
        return Match(agreement=agreement, shared=(), opposed=(), comparable=False)

    cut = layout.member_from
    wa = np.asarray(a.w[:cut], dtype=np.float64)
    wb = np.asarray(b.w[:cut], dtype=np.float64)
    na, nb = float(np.linalg.norm(wa)), float(np.linalg.norm(wb))
    if na < 1e-12 or nb < 1e-12:
        return Match(agreement=agreement, shared=(), opposed=(), comparable=True)
    ua, ub = wa / na, wb / nb
    keys = layout.keys[:cut]

    rows = list(zip(keys, ua, ub, ua * ub, strict=True))
    shared = sorted(
        ((k, float(p), float(q)) for k, p, q, sign in rows if sign > 0),
        key=lambda row: -(abs(row[1]) + abs(row[2])),
    )[:top]
    opposed = sorted(
        ((k, float(p), float(q)) for k, p, q, sign in rows if sign < 0),
        key=lambda row: -(abs(row[1]) + abs(row[2])),
    )[:top]
    return Match(agreement=agreement, shared=tuple(shared), opposed=tuple(opposed), comparable=True)


# --- the job -----------------------------------------------------------------------------------


# Constants, not fitted: one member's design has to be the next member's for the weights to be
# comparable at all (decision 570). The centres are the catalogue's own rough middles.
RUNTIME_CENTRE, RUNTIME_SCALE = 110.0, 40.0
VOTES_CENTRE, VOTES_SCALE = 4.5, 1.5
PLATFORM_CENTRE, PLATFORM_SCALE = 0.68, 0.08


async def build_features(
    conn: Any, *, title_ids: Sequence[int], bundle_version: str
) -> Features:
    """The title half of the design, built once per run (§5.3's seconds).

    Every member's fit reads these same rows; the member-relative columns are appended per member in
    `design`, never here.
    """
    from spielplan.db import dna_terms, library
    from spielplan.db import genres as genre_vocab

    ids = sorted({int(t) for t in title_ids})
    vocab = await dna_terms.active_version(conn) if ids else None
    terms: list[str] = []
    if vocab is not None:
        terms = [
            r["term"] for r in await conn.fetch(
                "SELECT DISTINCT term FROM dna_term WHERE version = $1 ORDER BY term", vocab
            )
        ]
    layout = layout_for(terms, genre_vocab.CANONICAL)
    if not ids:
        return Features(
            layout=layout, title_ids=np.zeros(0, dtype=np.int64),
            x=np.zeros((0, layout.member_from)),
        )

    row_of = {t: i for i, t in enumerate(ids)}
    col_of = {k: i for i, k in enumerate(layout.keys)}
    x = np.zeros((len(ids), layout.member_from), dtype=np.float64)

    for r in await conn.fetch(
        "SELECT id, year, runtime_min, original_language FROM title WHERE id = ANY($1::int[])", ids
    ):
        i = row_of[int(r["id"])]
        if r["year"] is not None:
            decade = min(DECADES[-1], max(DECADES[0], (int(r["year"]) // 10) * 10))
            x[i, col_of[f"decade:{decade}"]] = 1.0
        if r["runtime_min"] is not None:
            x[i, col_of["meta:runtime"]] = (float(r["runtime_min"]) - RUNTIME_CENTRE) / RUNTIME_SCALE
        if r["original_language"] == "en":
            x[i, col_of["meta:english"]] = 1.0

    if terms:
        # `dna_terms.TERM_WEIGHT` over both tiers, not `dna_projected.weight`: measured on this
        # household's own placements it reads the top band better (pairs .938 against .922, and
        # Jenny's top ten 7.8 against 6.8), because it separates a hand-verified term from a
        # projected one instead of counting how many passes asserted it.
        for r in await conn.fetch(
            f"""
            SELECT d.title_id, d.term, max({dna_terms.TERM_WEIGHT}) AS weight
              FROM dna_tagged d
             WHERE d.title_id = ANY($1::int[]) AND d.version = $2
             GROUP BY d.title_id, d.term
            """,
            ids, vocab,
        ):
            col = col_of.get(f"dna:{r['term']}")
            if col is not None:                      # a term the vocabulary no longer carries
                x[row_of[int(r["title_id"])], col] = float(r["weight"])

    for r in await conn.fetch(
        """
        SELECT title_id, array_agg(DISTINCT lower(genre)) AS raw
          FROM title_genre
         WHERE title_id = ANY($1::int[]) AND NOT (source = ANY($2::text[]))
         GROUP BY title_id
        """,
        ids, list(genre_vocab.EXCLUDED_SOURCES),
    ):
        i = row_of[int(r["title_id"])]
        for name in genre_vocab.facet(list(r["raw"])):
            x[i, col_of[f"genre:{name}"]] = 1.0

    for r in await conn.fetch(
        f"SELECT title_id, score, votes FROM ({library._PLATFORM_SCORE}) s"
        " WHERE title_id = ANY($1::int[])",
        ids,
    ):
        i = row_of[int(r["title_id"])]
        if r["score"] is not None:
            x[i, col_of["meta:platform"]] = (float(r["score"]) - PLATFORM_CENTRE) / PLATFORM_SCALE
        x[i, col_of["meta:votes"]] = (
            float(np.log10(max(float(r["votes"] or 0.0), 1.0))) - VOTES_CENTRE
        ) / VOTES_SCALE

    priors = await conn.fetch(
        "SELECT title_id, b FROM title_prior"
        " WHERE title_id = ANY($1::int[]) AND bundle_version = $2 AND b IS NOT NULL",
        ids, bundle_version,
    )
    if priors:
        b = np.asarray([float(r["b"]) for r in priors], dtype=np.float64)
        mean, sd = float(b.mean()), float(b.std())
        if sd < 1e-9:                                # a flat crowd orders nothing
            sd = 1.0
        prior_col = col_of["crowd:prior"]
        for r, value in zip(priors, b, strict=True):
            x[row_of[int(r["title_id"])], prior_col] = (value - mean) / sd

    # DISTINCT, not the raw rows: a credit carries one row per source, so one director appears up to
    # a dozen times on one film and would outweigh every other name by repetition alone.
    best: dict[int, dict[int, float]] = {}
    for r in await conn.fetch(
        """
        SELECT DISTINCT title_id, role_class, person_id, billing_order
          FROM credit
         WHERE title_id = ANY($1::int[])
           AND (role_class = ANY($2::text[])
                OR (role_class = 'cast' AND billing_order IS NOT NULL AND billing_order < $3))
        """,
        ids, list(ROLE_WEIGHT), CAST_DEPTH,
    ):
        if r["role_class"] == "cast":
            weight = max(0.3, 1.2 - 0.2 * int(r["billing_order"] or CAST_DEPTH))
        else:
            weight = ROLE_WEIGHT.get(r["role_class"], 0.0)
        if weight <= 0.0:
            continue
        per_title = best.setdefault(int(r["title_id"]), {})
        person = int(r["person_id"])
        per_title[person] = max(per_title.get(person, 0.0), weight)

    return Features(
        layout=layout, title_ids=np.asarray(ids, dtype=np.int64), x=x,
        people={t: tuple(sorted(d.items())) for t, d in best.items()}, row_of=row_of,
    )


def pack(w: np.ndarray) -> bytes:
    """float32 little-endian, the layout's own order; `width` and `digest` are what read it back."""
    return np.asarray(w, dtype="<f4").reshape(-1).tobytes()


def unpack(blob: bytes, width: int) -> np.ndarray:
    vec = np.frombuffer(blob, dtype="<f4")
    if vec.size != width:
        raise ValueError(f"expected {width} weights, got {vec.size}")
    return vec.astype(np.float64)


async def write_fit(
    conn: Any, *, user_id: int, kind: str, bundle_version: str, fit: Fit, width: int
) -> None:
    """One `user_content_fit` row per (user, kind), written even at weight zero: that a head was
    fitted and then declined is exactly what a reviewer needs to be able to see."""
    await conn.execute(
        """
        INSERT INTO user_content_fit (
            user_id, kind, bundle_version, w, width, digest, features_version, weight,
            content_lambda, con_sd, con_mean, cv_rho, base_rho,
            label_count, used, dropped, folds, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                now())
        ON CONFLICT (user_id, kind) DO UPDATE
           SET bundle_version = EXCLUDED.bundle_version, w = EXCLUDED.w, width = EXCLUDED.width,
               digest = EXCLUDED.digest, features_version = EXCLUDED.features_version,
               weight = EXCLUDED.weight, content_lambda = EXCLUDED.content_lambda,
               con_sd = EXCLUDED.con_sd, con_mean = EXCLUDED.con_mean, cv_rho = EXCLUDED.cv_rho,
               base_rho = EXCLUDED.base_rho,
               label_count = EXCLUDED.label_count, used = EXCLUDED.used,
               dropped = EXCLUDED.dropped, folds = EXCLUDED.folds, updated_at = now()
        """,
        user_id, kind, bundle_version, pack(fit.w), width, fit.digest, FEATURES_VERSION,
        fit.weight, fit.lam, fit.con_sd, fit.con_mean, fit.cv_rho, fit.base_rho,
        fit.label_count, fit.used, fit.dropped, fit.folds,
    )


async def read_fit(conn: Any, *, user_id: int, kind: str) -> Fit | None:
    """The stored head for one (user, kind). A fit from another feature version reads as absent: it
    is the refit's job to replace it, not a reader's to reinterpret it."""
    row = await conn.fetchrow(
        """
        SELECT w, width, digest, features_version, weight, content_lambda, con_sd, con_mean,
               cv_rho, base_rho, label_count, used, dropped, folds
          FROM user_content_fit WHERE user_id = $1 AND kind = $2
        """,
        user_id, kind,
    )
    if row is None or row["features_version"] != FEATURES_VERSION:
        return None
    return Fit(
        w=unpack(row["w"], int(row["width"])),
        weight=float(row["weight"]), lam=float(row["content_lambda"]),
        cv_rho=float(row["cv_rho"] or 0.0), base_rho=float(row["base_rho"] or 0.0),
        con_sd=float(row["con_sd"]), con_mean=float(row["con_mean"]), digest=row["digest"],
        label_count=int(row["label_count"]), used=int(row["used"]), dropped=int(row["dropped"]),
        folds=int(row["folds"]),
    )


async def match_members(conn: Any, *, kind: str, a: int, b: int, top: int = 8) -> Match:
    """Two members' taste, read off the one shared feature space (decision 570).

    The agreement runs over every title both members hold a score for, which is the whole reference
    population of the kind, so it answers "how alike would your two shelves be".
    """
    from spielplan.db import dna_terms
    from spielplan.db import genres as genre_vocab
    from spielplan.scoring.foldin import spearman

    fit_a = await read_fit(conn, user_id=a, kind=kind)
    fit_b = await read_fit(conn, user_id=b, kind=kind)
    rows = await conn.fetch(
        """
        SELECT x.score AS sa, y.score AS sb
          FROM user_score x
          JOIN user_score y ON y.title_id = x.title_id AND y.kind = x.kind
         WHERE x.user_id = $1 AND y.user_id = $2 AND x.kind = $3
         ORDER BY x.title_id
        """,
        a, b, kind,
    )
    scores = ([float(r["sa"]) for r in rows], [float(r["sb"]) for r in rows])
    if fit_a is None or fit_b is None:
        agreement = (
            spearman(np.asarray(scores[0]), np.asarray(scores[1])) if len(rows) > 1 else 0.0
        )
        return Match(agreement=agreement, shared=(), opposed=(), comparable=False)

    vocab = await dna_terms.active_version(conn)
    terms = (
        [
            r["term"] for r in await conn.fetch(
                "SELECT DISTINCT term FROM dna_term WHERE version = $1 ORDER BY term", vocab
            )
        ]
        if vocab is not None
        else []
    )
    return match(fit_a, fit_b, layout_for(terms, genre_vocab.CANONICAL), scores, top=top)
