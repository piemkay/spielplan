"""Genre facet vocabulary, normalised at read time (decision 473): TMDB's genre names.
Nothing here writes `title_genre`: the Cold Tower's genre block reads those rows as imported.
"""

from __future__ import annotations

# TMDB's movie genres without "TV Movie", plus the four its TV list adds, in control order.
CANONICAL: tuple[str, ...] = (
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family",
    "Fantasy", "History", "Horror", "Music", "Mystery", "News", "Reality", "Romance",
    "Science Fiction", "Soap", "Talk", "Thriller", "War", "Western",
)

# lower(raw label) -> the canonical genres it answers; the sources disagree on case.
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

# Free text, not a vocabulary: the source of adult and overlong labels.
EXCLUDED_SOURCES: tuple[str, ...] = ("wikidata",)

_BY_LOWER = {name.lower(): name for name in CANONICAL}


def canonical(genre: str) -> str:
    """A refusal, so a stale raw label reads as a wrong question rather than an empty library."""
    found = _BY_LOWER.get(genre.strip().lower())
    if found is None:
        raise ValueError(f"unknown genre {genre!r}; the facet offers {', '.join(CANONICAL)}")
    return found


def raw_labels(genre: str) -> list[str]:
    """Empty for an unknown genre, so a predicate built on it matches nothing."""
    found = _BY_LOWER.get(genre.strip().lower())
    return sorted(raw for raw, names in GENRE_CANON.items() if found in names) if found else []


def facet(raw: set[str] | list[str]) -> list[str]:
    hit = {name for label in raw for name in GENRE_CANON.get(label, ())}
    return [name for name in CANONICAL if name in hit]


def predicate(param: str, excluded_param: str) -> str:
    """Over alias `t`; `param` binds `raw_labels(genre)`, `excluded_param` binds `EXCLUDED_SOURCES`.
    Shared by the catalog and `scoring/serve.py` so "Action" means one set everywhere."""
    return (
        "EXISTS (SELECT 1 FROM title_genre g WHERE g.title_id = t.id"
        f" AND g.source <> ALL({excluded_param}::text[]) AND lower(g.genre) = ANY({param}::text[]))"
    )


__all__ = ["CANONICAL", "EXCLUDED_SOURCES", "GENRE_CANON", "canonical", "facet", "predicate",
           "raw_labels"]
