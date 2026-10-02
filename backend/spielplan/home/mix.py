"""Film arithmetic (decisions 559 and 560): a recipe of liked and less-liked films, whole or by group,
ranked over an in-memory term table. numpy only: no database, no clock.

Likeness is decision 513's cosine of idf-weighted term presence over both tiers, idf read over the
owned titles of the kind ranked. The naming rank (`TERM_WEIGHT`) orders what a line names and never
enters a score (§4.1 rule 2).
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np

# Decision 560 item 2: register is no group; it stays in whole-film likeness and the term filter.
GROUPS: dict[str, tuple[str, ...]] = {
    "mood": ("mood", "sensibility"),
    "look": ("visual",),
    "sound": ("sound",),
    "pace": ("pacing",),
    "storytelling": ("structure",),
    "setting": ("place", "era"),
    "characters": ("characters",),
    "themes": ("themes",),
}
GROUP_NAMES = {
    "mood": "Mood", "look": "Look", "sound": "Sound", "pace": "Pace", "storytelling": "Storytelling",
    "setting": "Setting", "characters": "Characters", "themes": "Themes",
}

LESS = 0.7
REVIEW = 0.3
RARE_IDF = 1.5
RARE_SHARED = 2
MAX_FILMS = 4
MAX_LENDING = 2
GROUP_MIN_TERMS = 2
DIRECTOR_CAP = 2
THIN_UNDER = 10
TWIST_MIN_LIBRARY = THIN_UNDER
TWISTS = 3
TWIST_TERMS = 3
WELL_KNOWN_VOTES = 25_000
# A film the picker offers, and so a recipe takes, carries at least this many terms (decision 559).
MIN_TERMS = 2
# Copy, not tuned: a why names two terms per film, a chip six, the derived line three a side.
NAMED = 2
FILM_TERMS = 6
DERIVED = 3


@dataclass(frozen=True)
class Ingredient:
    """One recipe film: whole (`groups` empty) or lending the named groups, liked or less liked."""

    title_id: int
    groups: tuple[str, ...] = ()
    like: bool = True


class MixRefused(ValueError):
    """A recipe outside decision 560's limits, or one that cannot be read."""

    def __init__(self, reason: str, message: str, *, title_id: int | None = None,
                 group: str | None = None) -> None:
        super().__init__(message)
        self.reason, self.message, self.title_id, self.group = reason, message, title_id, group

    def detail(self) -> dict[str, object]:
        return {"reason": self.reason, "message": self.message, "title_id": self.title_id,
                "group": self.group}


def ingredient(value: str, *, like: bool) -> Ingredient:
    """`245` or `6087:mood,sound`, as Home's URL carries a recipe film."""
    head, colon, tail = value.partition(":")
    groups = tuple(g.strip() for g in tail.split(",") if g.strip())
    try:
        title_id = int(head)
    except ValueError:
        title_id = None
    if title_id is None or (colon and not groups):
        raise MixRefused("bad_ingredient", f"{value!r} is not a film id with optional groups.")
    return Ingredient(title_id, groups, like)


@dataclass(frozen=True, eq=False)
class Operand:
    """One title's terms, carried to rank a table of either kind: every table shares one term axis."""

    title_id: int
    cols: np.ndarray
    quoted: np.ndarray
    rank: np.ndarray
    review: np.ndarray | None


@dataclass(frozen=True, eq=False)
class Fold:
    """The page's films past a director's cap, in the third one's place."""

    directors: tuple[int, ...]
    rows: list[int]


@dataclass(frozen=True, eq=False)
class Table:
    """One kind's titles with DNA, as a CSR matrix over the active vocabulary's terms."""

    terms: tuple[str, ...]
    labels: tuple[str, ...]
    facets: tuple[str, ...]
    colours: Mapping[str, str | None]
    term_facet: np.ndarray
    ids: np.ndarray
    indptr: np.ndarray
    cols: np.ndarray
    quoted: np.ndarray
    rank: np.ndarray
    votes: np.ndarray
    # Director indices per row; `people[i]` is director i's (person ids, name), one human per entry.
    directors: tuple[tuple[int, ...], ...]
    people: tuple[tuple[tuple[int, ...], str], ...]
    review: np.ndarray
    review_row: np.ndarray
    owned: np.ndarray
    n_owned: int
    idf: np.ndarray
    fnorm2: np.ndarray
    row_of: dict[int, int] = field(repr=False)
    nnz_row: np.ndarray = field(repr=False)

    @classmethod
    def build(
        cls,
        *,
        terms: Sequence[str],
        labels: Sequence[str],
        term_facets: Sequence[str],
        facets: Sequence[str],
        colours: Mapping[str, str | None],
        rows: Sequence[tuple[int, Sequence[int], Sequence[bool], Sequence[float]]],
        votes: Mapping[int, int],
        directors: Mapping[int, Sequence[int]],
        people: Sequence[tuple[tuple[int, ...], str]],
        review: Mapping[int, np.ndarray],
        owned: Iterable[int],
        n_owned: int,
    ) -> Table:
        """`rows` ascending by title id, each with its term indices ascending."""
        facet_index = {f: i for i, f in enumerate(facets)}
        lengths = np.array([len(r[1]) for r in rows], dtype=np.int64)
        indptr = np.zeros(len(rows) + 1, dtype=np.int64)
        np.cumsum(lengths, out=indptr[1:])
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        covered = [i for i, t in enumerate(ids) if int(t) in review]
        review_row = np.full(len(rows), -1, dtype=np.int64)
        review_row[covered] = np.arange(len(covered))
        vectors = np.zeros((len(covered), _width(review)), dtype=np.float32)
        for k, i in enumerate(covered):
            v = np.asarray(review[int(ids[i])], dtype=np.float32)
            vectors[k] = v / (float(np.linalg.norm(v)) or 1.0)
        table = cls(
            terms=tuple(terms),
            labels=tuple(labels),
            facets=tuple(facets),
            colours=dict(colours),
            term_facet=np.array([facet_index[f] for f in term_facets], dtype=np.int64),
            ids=ids,
            indptr=indptr,
            cols=np.array([c for r in rows for c in r[1]], dtype=np.int64),
            quoted=np.array([q for r in rows for q in r[2]], dtype=bool),
            rank=np.array([w for r in rows for w in r[3]], dtype=np.float32),
            votes=np.array([votes.get(int(t), 0) for t in ids], dtype=np.int64),
            directors=tuple(tuple(directors.get(int(t), ())) for t in ids),
            people=tuple(people),
            review=vectors,
            review_row=review_row,
            owned=np.zeros(len(rows), dtype=bool),
            n_owned=0,
            idf=np.zeros(len(terms), dtype=np.float32),
            fnorm2=np.zeros((len(rows), len(facets))),
            row_of={int(t): i for i, t in enumerate(ids)},
            nnz_row=np.repeat(np.arange(len(rows), dtype=np.int64), lengths),
        )
        return table.with_owned(owned, n_owned)

    def with_owned(self, owned: Iterable[int], n_owned: int) -> Table:
        """The library's share of the table: owned rows, idf and norms. The terms are not refetched."""
        mask = np.isin(self.ids, np.fromiter(owned, dtype=np.int64))
        df = np.bincount(self.cols[mask[self.nnz_row]], minlength=len(self.terms))
        n = max(n_owned, 1)
        # A term no owned title carries weighs as one carried by a single title (decision 513).
        idf = np.log(n / np.maximum(df, 1)).astype(np.float32)
        fnorm2 = np.bincount(
            self.nnz_row * len(self.facets) + self.term_facet[self.cols],
            weights=idf[self.cols].astype(np.float64) ** 2,
            minlength=len(self.ids) * len(self.facets),
        ).reshape(len(self.ids), len(self.facets))
        return replace(self, owned=mask, n_owned=n_owned, idf=idf, fnorm2=fnorm2)

    def operand(self, title_id: int) -> Operand:
        row = self.row_of[title_id]
        span = slice(self.indptr[row], self.indptr[row + 1])
        at = self.review_row[row]
        return Operand(title_id, self.cols[span], self.quoted[span], self.rank[span],
                       self.review[at] if at >= 0 else None)

    def facet_mask(self, facets: Iterable[str]) -> np.ndarray:
        wanted = set(facets)
        return np.array([f in wanted for f in self.facets], dtype=bool)

    @property
    def rare(self) -> np.ndarray:
        return self.idf >= RARE_IDF


def _width(review: Mapping[int, np.ndarray]) -> int:
    return len(next(iter(review.values()))) if review else 0


# --- the arithmetic ------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _Part:
    """An ingredient as the recipe reads it: its facets and its terms inside them."""

    ing: Ingredient
    facets: np.ndarray
    terms: np.ndarray
    whole: np.ndarray


@dataclass(frozen=True, eq=False)
class _View:
    """Some of a table's rows, with their non-zeros renumbered locally."""

    rows: np.ndarray
    cols: np.ndarray
    local: np.ndarray


def _view(t: Table, mask: np.ndarray | None = None) -> _View:
    if mask is None:
        return _View(np.arange(len(t.ids)), t.cols, t.nnz_row)
    keep = mask[t.nnz_row]
    local = np.cumsum(mask) - 1
    return _View(np.flatnonzero(mask), t.cols[keep], local[t.nnz_row[keep]])


def _present(t: Table, op: Operand) -> np.ndarray:
    out = np.zeros(len(t.terms), dtype=bool)
    out[op.cols] = True
    return out


def _dense(t: Table, op: Operand, values: np.ndarray) -> np.ndarray:
    out = np.zeros(len(t.terms), dtype=values.dtype)
    out[op.cols] = values
    return out


def _parts(t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand]) -> list[_Part]:
    """Replace (decision 560 item 4): a lent group leaves every whole-film like; a group less-like reads
    only its terms no liked film carries (item 5); a whole-film less-like reads all of its own."""
    claimed = t.facet_mask(f for i in recipe if i.like for g in i.groups for f in GROUPS[g])
    carried = np.zeros(len(t.terms), dtype=bool)
    for i in recipe:
        if i.like:
            carried |= _present(t, operands[i.title_id])
    parts = []
    for i in recipe:
        if i.groups:
            facets = t.facet_mask(f for g in i.groups for f in GROUPS[g])
        else:
            facets = ~claimed if i.like else np.ones(len(t.facets), dtype=bool)
        whole = _present(t, operands[i.title_id])
        terms = whole & facets[t.term_facet]
        if i.groups and not i.like:
            terms &= ~carried
        parts.append(_Part(i, facets, terms, whole))
    return parts


def _shared(t: Table, view: _View, terms: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row and facet: how many of `terms` it carries, how many of those are rare, and their idf^2."""
    hit = terms[view.cols]
    cols = view.cols[hit]
    width = len(t.facets)
    at = view.local[hit] * width + t.term_facet[cols]
    size = len(view.rows) * width
    shared = np.bincount(at, minlength=size).reshape(-1, width)
    rare = np.bincount(at[t.rare[cols]], minlength=size).reshape(-1, width)
    dot = np.bincount(at, weights=t.idf[cols].astype(np.float64) ** 2, minlength=size)
    return shared, rare, dot.reshape(-1, width)


def _cosine(t: Table, view: _View, part: _Part, dot: np.ndarray) -> np.ndarray:
    """Each row's cosine to the part, the row read over the part's facets as well (decision 560 item 4)."""
    own = math.sqrt(float((t.idf[part.terms].astype(np.float64) ** 2).sum()))
    norm = np.sqrt(t.fnorm2[view.rows][:, part.facets].sum(1)) * own
    num = dot[:, part.facets].sum(1)
    return np.divide(num, norm, out=np.zeros(len(view.rows)), where=norm > 0)


def _in_recipe(t: Table, recipe: Sequence[Ingredient]) -> np.ndarray:
    """A recipe film is never its own result."""
    mask = np.zeros(len(t.ids), dtype=bool)
    mask[[t.row_of[i.title_id] for i in recipe if i.title_id in t.row_of]] = True
    return mask


def dna_score(
    t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand]
) -> tuple[np.ndarray, np.ndarray]:
    """(score, gate) per row: the geometric mean of the liked factors minus 0.7 x the closest less-like,
    and whether the row survives. A survivor shares RARE_SHARED rare terms with each whole-film like in
    its unclaimed facets and one term with each lent ingredient; a less-like gates nothing."""
    view = _view(t)
    n = len(t.ids)
    gate = ~_in_recipe(t, recipe)
    liked, less = [], []
    for part in _parts(t, recipe, operands):
        shared, rare, dot = _shared(t, view, part.terms)
        cosine = _cosine(t, view, part, dot)
        if not part.ing.like:
            less.append(cosine)
        elif part.ing.groups:
            liked.append(np.maximum(cosine, 0.0))
            gate &= shared[:, part.facets].sum(1) >= 1
        else:
            liked.append(np.maximum(cosine, 0.0))
            gate &= rare[:, part.facets].sum(1) >= RARE_SHARED
    if not liked:
        return np.zeros(n), np.zeros(n, dtype=bool)
    score = np.prod(liked, axis=0) ** (1.0 / len(liked))
    if less:
        score = score - LESS * np.max(less, axis=0)
    return score, gate


def _review(t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand]) -> np.ndarray | None:
    """The same arithmetic over review text, whole films only (decision 560 item 4); None without any."""
    def cosines(sign: bool) -> list[np.ndarray]:
        out = []
        for i in recipe:
            vector = operands[i.title_id].review
            if i.like == sign and not i.groups and vector is not None:
                full = np.zeros(len(t.ids))
                full[t.review_row >= 0] = t.review @ vector
                out.append(full)
        return out

    if not (t.review_row >= 0).any():
        return None
    liked = cosines(True)
    if not liked:
        return None
    score = np.prod([np.maximum(c, 0.0) for c in liked], axis=0) ** (1.0 / len(liked))
    less = cosines(False)
    return score - LESS * np.max(less, axis=0) if less else score


def _z(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    values = x[mask]
    sd = float(values.std())
    return (x - float(values.mean())) / sd if sd > 0 else np.zeros_like(x)


def rank(t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand]) -> np.ndarray:
    """Best match: the rows a recipe reads (survivors in the library, or beyond it and well known),
    by 0.7 z(DNA) + 0.3 z(review), standardised over all of them; a row without review text on DNA
    alone at full weight. Ties by title id."""
    score, gate = dna_score(t, recipe, operands)
    reads = gate & (t.owned | (t.votes >= WELL_KNOWN_VOTES))
    if not reads.any():
        return np.zeros(0, dtype=np.int64)
    blended = _z(score, reads)
    review = _review(t, recipe, operands)
    covered = reads & (t.review_row >= 0)
    if review is not None and covered.any():
        blended = np.where(covered, (1 - REVIEW) * blended + REVIEW * _z(review, covered), blended)
    rows = np.flatnonzero(reads)
    return rows[np.lexsort((t.ids[rows], -blended[rows]))]


def _top(cols: np.ndarray, strength: np.ndarray, quoted: np.ndarray, k: int,
         *, quoted_first: bool = False) -> list[tuple[int, bool]]:
    keys = (cols, -strength, ~quoted) if quoted_first else (cols, -strength)
    order = np.lexsort(keys)[:k]
    return [(int(cols[j]), bool(quoted[j])) for j in order]


def why(
    t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand], row: int
) -> list[tuple[Ingredient, list[tuple[int, bool]]]]:
    """Per liked ingredient the two strongest terms the row shares inside its facets (idf x the lesser
    naming rank); per less-like, its own terms the row still carries. (term index, quoted on the row)."""
    span = slice(t.indptr[row], t.indptr[row + 1])
    cols, quoted, rank_ = t.cols[span], t.quoted[span], t.rank[span]
    parts = _parts(t, recipe, operands)
    carried = np.zeros(len(t.terms), dtype=bool)
    for part in parts:
        if part.ing.like:
            carried |= part.whole
    out = []
    for part in parts:
        op = operands[part.ing.title_id]
        theirs = _dense(t, op, op.rank)[cols]
        if part.ing.like:
            hit = part.terms[cols]
            strength = t.idf[cols] * np.minimum(rank_, theirs)
        else:
            hit = (part.terms & ~carried)[cols]
            strength = t.idf[cols] * theirs
        named = _top(cols[hit], strength[hit], quoted[hit], NAMED)
        if part.ing.like or named:
            out.append((part.ing, named))
    return out


def derived(
    t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand]
) -> tuple[list[tuple[int, bool]], list[tuple[int, bool]]]:
    """The recipe head's display-only "more: ... · less: ..." terms (decision 559 item 5): each side's
    films name their strongest terms in turn, quoted first within a film, so each film is named; the
    quoted terms lead the line."""
    parts = _parts(t, recipe, operands)
    carried = np.zeros(len(t.terms), dtype=bool)
    for part in parts:
        if part.ing.like:
            carried |= part.whole
    sides = []
    for like in (True, False):
        ranked = []
        for part in parts:
            if part.ing.like != like:
                continue
            op = operands[part.ing.title_id]
            cols = np.flatnonzero(part.terms if like else part.terms & ~carried)
            strength, quoted = (t.idf * _dense(t, op, op.rank))[cols], _dense(t, op, op.quoted)[cols]
            ranked.append(_top(cols, strength, quoted, len(cols), quoted_first=True))
        named: dict[int, bool] = {}
        for turn in itertools.zip_longest(*ranked):
            for term in turn:
                if term is None:
                    continue
                col, quoted = term
                if col in named:
                    named[col] |= quoted
                elif len(named) < DERIVED:
                    named[col] = quoted
        sides.append(sorted(named.items(), key=lambda term: not term[1]))
    return sides[0], sides[1]


def film_terms(t: Table, ing: Ingredient, op: Operand, k: int = FILM_TERMS) -> list[tuple[int, bool]]:
    """A recipe film's strongest terms, or its lent groups' terms, quoted first."""
    inside = t.facet_mask(f for g in ing.groups for f in GROUPS.get(g, ()))[t.term_facet[op.cols]]
    keep = inside if ing.groups else np.ones(len(op.cols), dtype=bool)
    cols = op.cols[keep]
    return _top(cols, (t.idf[cols] * op.rank[keep]), op.quoted[keep], k, quoted_first=True)


def group_terms(t: Table, op: Operand) -> list[tuple[str, bool, list[int], list[int]]]:
    """Each group of GROUPS in order: (group, offered, quoted terms, inferred terms), strongest first;
    a group is offered at GROUP_MIN_TERMS terms (decision 560 item 3)."""
    out = []
    for group, facets in GROUPS.items():
        inside = t.facet_mask(facets)[t.term_facet[op.cols]]
        order = np.lexsort((op.cols[inside], -op.rank[inside]))
        cols, quoted = op.cols[inside][order], op.quoted[inside][order]
        out.append((group, int(inside.sum()) >= GROUP_MIN_TERMS,
                    [int(c) for c in cols[quoted]], [int(c) for c in cols[~quoted]]))
    return out


def _thin(t: Table, op: Operand, group: str) -> bool:
    return int(t.facet_mask(GROUPS[group])[t.term_facet[op.cols]].sum()) < GROUP_MIN_TERMS


def check(t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand]) -> None:
    """Decision 560 item 6's limits, and a film the picker could offer. Raises MixRefused."""
    for i in recipe:
        for g in i.groups:
            if g not in GROUPS:
                raise MixRefused("unknown_group", f"There is no group {g!r}.", title_id=i.title_id,
                                 group=g)
    ids = [i.title_id for i in recipe]
    if len(set(ids)) < len(ids):
        twice = next(x for x in ids if ids.count(x) > 1)
        raise MixRefused("bad_ingredient", "A film is in the recipe twice.", title_id=twice)
    if len(recipe) > MAX_FILMS:
        raise MixRefused("too_many_films", f"A recipe holds at most {MAX_FILMS} films.")
    for i in recipe:
        op = operands.get(i.title_id)
        if op is None or len(op.cols) < MIN_TERMS:
            raise MixRefused("no_dna", f"A recipe film carries at least {MIN_TERMS} taste terms.",
                             title_id=i.title_id)
    if sum(bool(i.groups) for i in recipe) > MAX_LENDING:
        raise MixRefused("too_many_lending", f"At most {MAX_LENDING} films can take groups.")
    taken: set[str] = set()
    for i in recipe:
        for g in i.groups:
            if g in taken:
                raise MixRefused("group_taken_twice",
                                 f"{GROUP_NAMES[g]} is taken twice; one film per group.",
                                 title_id=i.title_id, group=g)
            taken.add(g)
            if _thin(t, operands[i.title_id], g):
                raise MixRefused("group_too_thin",
                                 f"{GROUP_NAMES[g]} needs {GROUP_MIN_TERMS} terms on this film.",
                                 title_id=i.title_id, group=g)


def director_cap(t: Table, rows: Sequence[int]) -> list[int | Fold]:
    """At most DIRECTOR_CAP films per director; a director's later films fold into one cell where the
    first of them stood, and a film counts against each of its directors (decision 559 item 4)."""
    counts: Counter[int] = Counter()
    cells: list[int | Fold] = []
    folds: list[Fold] = []
    for row in rows:
        mine = t.directors[row]
        capped = tuple(d for d in mine if counts[d] >= DIRECTOR_CAP)
        if not capped:
            cells.append(int(row))
            counts.update(mine)
            continue
        fold = next((f for f in folds if set(f.directors) & set(capped)), None)
        if fold is None:
            fold = Fold(capped, [])
            folds.append(fold)
            cells.append(fold)
        fold.rows.append(int(row))
    return cells


def twist_pairs(
    t: Table, recipe: Sequence[Ingredient], films: Sequence[Operand]
) -> list[tuple[Operand, str]]:
    """Every (film, group) a twist may add within the limits (decision 560 item 7): a group of one of
    `films`, read on `t`, that the film carries and no recipe film takes."""
    if not recipe or len(recipe) >= MAX_FILMS or sum(bool(i.groups) for i in recipe) >= MAX_LENDING:
        return []
    taken = {g for i in recipe for g in i.groups}
    inside = {i.title_id for i in recipe}
    return [
        (op, g)
        for op in sorted(films, key=lambda o: o.title_id)
        if op.title_id not in inside
        for g in GROUPS
        if g not in taken and not _thin(t, op, g)
    ]


def twist_count(
    t: Table, recipe: Sequence[Ingredient], operands: Mapping[int, Operand], rows: np.ndarray
) -> Callable[[Operand, str], int]:
    """How many of `rows` pass the recipe's DNA gates once a film's group joins it as a lent like: what
    "Only N films in your library fit" would count for the twisted recipe."""
    view = _view(t, rows & ~_in_recipe(t, recipe))
    claimed = t.facet_mask(f for i in recipe if i.like for g in i.groups for f in GROUPS[g])
    lent = np.ones(len(view.rows), dtype=bool)
    whole: list[np.ndarray] = []
    for part in _parts(t, recipe, operands):
        if not part.ing.like:
            continue
        shared, rare, _ = _shared(t, view, part.whole)
        if part.ing.groups:
            lent &= shared[:, part.facets].sum(1) >= 1
        else:
            whole.append(rare)
    shared_with: dict[int, np.ndarray] = {}

    def count(op: Operand, group: str) -> int:
        facets = t.facet_mask(GROUPS[group])
        unclaimed = ~(claimed | facets)
        ok = lent & (view.rows != t.row_of.get(op.title_id, -1))
        for rare in whole:
            ok &= rare[:, unclaimed].sum(1) >= RARE_SHARED
        if op.title_id not in shared_with:
            shared_with[op.title_id] = _shared(t, view, _present(t, op))[0]
        ok &= shared_with[op.title_id][:, facets].sum(1) >= 1
        return int(ok.sum())

    return count


def twists(
    pairs: Sequence[tuple[str, Operand, str]],
    counts: Mapping[str, Callable[[Operand, str], int]],
    *,
    seed: int,
) -> list[tuple[str, int, str, int]]:
    """Page `seed` of the (kind, film, group, library count) twists that keep TWIST_MIN_LIBRARY films of
    their kind: TWISTS at a time in one fixed order, wrapping round, so each shuffle shows the next ones.
    The order is a fixed shuffle of the pairs taking one group of each film a round; pairs are counted
    only as far as the page needs."""
    shuffled = [pairs[i] for i in np.random.default_rng(0).permutation(len(pairs))]
    turn: Counter[int] = Counter()
    rounds = []
    for _kind, op, _group in shuffled:
        rounds.append(turn[op.title_id])
        turn[op.title_id] += 1
    kept: list[tuple[str, int, str, int]] = []
    end = (seed + 1) * TWISTS
    for at in np.argsort(rounds, kind="stable"):
        kind, op, group = shuffled[at]
        n = counts[kind](op, group)
        if n >= TWIST_MIN_LIBRARY:
            kept.append((kind, op.title_id, group, n))
            if len(kept) == end:
                return kept[-TWISTS:]
    if len(kept) <= TWISTS:
        return kept
    return [kept[(seed * TWISTS + j) % len(kept)] for j in range(TWISTS)]
