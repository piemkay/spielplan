"""The genre facet's vocabulary, normalised at read time. Spec v2.1 §6.0 M0, §6.3, §4.1;
decision 473.

§6.0 M0 names genre as a filter dimension and never names its vocabulary, so the facet was a raw
`SELECT DISTINCT genre` over every source's rows: 434 values for films on the first household
install, "Action" beside "action", Wikidata's free-text labels (591 of them, 169 used once, the
adult ones among them), and a 37-character option that pushed Home wider than the phone it is
built for. Picking "Action" also missed every title only trakt had tagged "action".

Decision 473 fixes one vocabulary - TMDB's own genre names, the movie list without "TV Movie"
plus the four its TV list adds - and reads every structured source through this one table.
NOTHING HERE WRITES `title_genre`: §4.1 keeps one block per droppable source, and the Cold Tower's
genre block is built from those rows as they were imported (`placement/features.py`), so
rewriting them at import would move placements. The mapping is a read.

A label no row below names (biography, sport, film-noir, superhero, game-show, holiday, short,
adult, ...) is simply not a facet value. Its title stays in the catalog; only the label leaves the
control. Wikidata is excluded outright, from the facet and from the predicate.
"""

from __future__ import annotations

# TMDB's movie genres without "TV Movie", then the four its TV list adds. Sorted here because the
# control lists them in this order and a set would make the order an accident.
CANONICAL: tuple[str, ...] = (
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family",
    "Fantasy", "History", "Horror", "Music", "Mystery", "News", "Reality", "Romance",
    "Science Fiction", "Soap", "Talk", "Thriller", "War", "Western",
)

# lower(raw label) -> the canonical genres it answers. TMDB's combined TV genres count as both
# halves; the rest are one source's spelling of a name above. Keys are lower-case because the
# sources disagree on case ("Action" from tmdb, "action" from trakt) and case is not a genre.
GENRE_CANON: dict[str, tuple[str, ...]] = {
    **{name.lower(): (name,) for name in CANONICAL},
    "action & adventure": ("Action", "Adventure"),
    "sci-fi & fantasy": ("Science Fiction", "Fantasy"),
    "war & politics": ("War",),
    "sci-fi": ("Science Fiction",),
    "science-fiction": ("Science Fiction",),
    "anime": ("Animation",),
    "donghua": ("Animation",),
    "kids": ("Family",),
    "children": ("Family",),
    "musical": ("Music",),
    "suspense": ("Thriller",),
    "reality-tv": ("Reality",),
    "talk-show": ("Talk",),
}

# Free text, not a vocabulary: the source of every adult label on the live install, and of every
# label longer than a phone's select can show.
EXCLUDED_SOURCES: tuple[str, ...] = ("wikidata",)

_BY_LOWER = {name.lower(): name for name in CANONICAL}


def canonical(genre: str) -> str:
    """The canonical spelling of `genre`, or ValueError for a genre outside the vocabulary.

    A refusal rather than an empty result: a stale raw label ("heist film") left in a client would
    otherwise answer "no titles", which reads as an empty library rather than a wrong question.
    """
    found = _BY_LOWER.get(genre.strip().lower())
    if found is None:
        raise ValueError(f"unknown genre {genre!r}; the facet offers {', '.join(CANONICAL)}")
    return found


def raw_labels(genre: str) -> list[str]:
    """Every lower-cased source label that answers `genre`. Empty for a genre outside the
    vocabulary, so a predicate built on it matches nothing rather than everything."""
    found = _BY_LOWER.get(genre.strip().lower())
    return sorted(raw for raw, names in GENRE_CANON.items() if found in names) if found else []


def facet(raw: set[str] | list[str]) -> list[str]:
    """The canonical genres a set of lower-cased source labels answers, in `CANONICAL` order."""
    hit = {name for label in raw for name in GENRE_CANON.get(label, ())}
    return [name for name in CANONICAL if name in hit]


def predicate(param: str, excluded_param: str) -> str:
    """`title_genre` membership for one canonical genre, over alias `t`.

    `param` binds `raw_labels(genre)` and `excluded_param` binds `EXCLUDED_SOURCES`. One spelling,
    imported by the catalog's `_filters` and by `scoring/serve.py`'s, because two copies of the
    genre predicate are how "Action" came to mean two different sets on two surfaces.
    """
    return (
        "EXISTS (SELECT 1 FROM title_genre g WHERE g.title_id = t.id"
        f" AND g.source <> ALL({excluded_param}::text[]) AND lower(g.genre) = ANY({param}::text[]))"
    )


__all__ = ["CANONICAL", "EXCLUDED_SOURCES", "GENRE_CANON", "canonical", "facet", "predicate",
           "raw_labels"]
