"""A synthetic artifact bundle shaped like the corpus export: its landmines, not its volume. Every
structure follows `real_bundle_shapes.json` (held by `test_bundle_shapes.py`); `break_*` helpers
violate one rule each, and `pool_titles` opts into volume."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

import numpy as np

# §4.1 rule 4: the frozen ids.
RATING_SOURCE_IDS = (1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31)

# The corpus's per-field precedence; data here, because decision 162 makes the corpus own it.
SOURCE_PRIORITY = ("tmdb", "omdb", "trakt", "tvmaze", "wikipedia")

# id, kind, primary_title, original_title, year, runtime_min, imdb_id, tmdb_id, language, country
TITLES = [
    (1, "movie", "Heat", None, 1995, 170, "tt0113277", 949, "en", "United States of America"),
    (2, "movie", "Prisoners", None, 2013, 153, "tt1392214", 146233, "en", "United States of America"),
    # imdb_id NULL: the 21% case §4.1 names as the reason title.id is the join key.
    (3, "movie", "Paddington 2", None, 2017, 103, None, 346648, "en", "United Kingdom"),
    (4, "movie", "Chungking Express", "重慶森林", 1994, 102, "tt0109424", 11104, "yue", "Hong Kong"),
    # CJK primary title AND a duplicate tmdb_id with title 4: §4.1 rule 6's legitimate duplicate.
    (5, "movie", "重慶森林", "Chungking Express", 1994, 102, None, 11104, "yue", "Hong Kong"),
    (6, "series", "Severance", None, 2022, 48, "tt11280740", 95396, "en", "United States of America"),
    (7, "series", "The Bear", None, 2022, 30, "tt14452776", 136315, "en", "United States of America"),
    (8, "movie", "Tampopo", None, 1985, 114, "tt0092048", 11081, "ja", "Japan"),
]

# Sources are complementary, not ranked copies, so per-field and whole-block precedence differ.
# No URL here is one the app would fetch: e2e builds from this module and the art route fetches for real.
#   (title_id, source, tagline, plot_short, plot_full, poster_url, backdrop_url)
META = [
    (1, "tmdb", "A Los Angeles crime saga.", None,
     "Heat — a synthetic plot with emoji 🎬 and a ZWSP​.", "/heat.jpg", "/heat-bd.jpg"),
    (1, "omdb", None, None, "A shorter synthetic plot.",
     "https://m.media-amazon.com/images/M/heat-omdb.jpg", None),
    (1, "wikipedia", None, "Heat — a one-line synthetic summary.", None, None, None),
    # No tmdb row: the preferred source is absent, and the next one carries the fields.
    (2, "omdb", None, None, "Prisoners — a synthetic plot.",
     "https://m.media-amazon.com/images/M/prisoners.jpg", None),
    (2, "wikipedia", None, "Prisoners — a one-line synthetic summary.", None, None, None),
    (3, "tmdb", "This bear has manners.", None, "Paddington 2 — a synthetic plot.",
     "/pad2.jpg", None),
    (4, "tmdb", None, None, "Chungking Express — a synthetic plot.", "/ce.jpg", None),
    (5, "tmdb", None, None, "重慶森林 — 合成されたあらすじ。", "/ce5.jpg", None),
    (6, "tmdb", "Work-life balance, surgically.", None, "Severance — a synthetic plot.",
     "/sev.jpg", None),
    (7, "tmdb", None, None, "The Bear — a synthetic plot.", "/bear.jpg", None),
    # Title 8 has no meta row at all: the card must render without these fields, not error.
]

# (title_id, source, key, site, type)
VIDEOS = [
    (1, "tmdb", "heat-trailer-key", "YouTube", "Trailer"),
    (3, "tmdb", "pad2-trailer-key", "YouTube", "Trailer"),
]

# The term IS `facet.term`, so a builder that prepends the facet produces `mood.mood.dread`.
#   (term, facet, gloss)
VOCAB = [
    ("mood.dread", "mood", "a low hum of dread that outlasts the final scene"),
    ("mood.cosy", "mood", "wraps you in a blanket and never curdles into sugar"),
    ("themes.obsession", "themes", "the work eats the man and he lets it"),
    ("themes.surveillance", "themes", "everyone is being watched and half of them know it"),
    ("pacing.patient", "pacing", "trusts you to wait, and the waiting pays"),
    ("pacing.relentless", "pacing", "never once lets the audience sit down"),
    ("structure.procedural", "structure", "built out of process: forms, interviews, dead ends"),
    ("visual.neon", "visual", "lit entirely by signage and rain"),
    ("sound.score_forward", "sound", "the score is arguing with the picture"),
    ("characters.morally_grey", "characters", "nobody here is owed your sympathy"),
    ("place.domestic", "place", "kitchens, hallways, and the arguments they hold"),
    ("era.period", "era", "the past rendered as a working place, not a costume"),
    ("sensibility.bleak", "sensibility", "offers no consolation and does not pretend to"),
    ("register.deadpan", "register", "funny with an entirely straight face"),
]

# The corpus's extraction labels as shipped in the DNA
# tables; three measured, and no fourth may be invented.
EXTRACTION_LABELS = {
    "mood": "mood_tone", "themes": "narrative_themes", "characters": "character_dynamics",
}


def shipped_facet(term: str) -> str:
    """The extraction label where one was measured, else
    the term's own prefix: the rule the pool applies."""
    prefix = term.split(".", 1)[0]
    return EXTRACTION_LABELS.get(prefix, prefix)


# (title_id, term, facet, salience, quote) — the extracted tier, every tag with its quote.
EXTRACTED = [
    (1, "themes.obsession", "narrative_themes", 3, "the work eats the man and he lets it"),
    (1, "characters.morally_grey", "character_dynamics", 2, "nobody here is owed your sympathy"),
    (2, "mood.dread", "mood_tone", 3, "a low hum of dread that outlasts the final scene"),
    (2, "sensibility.bleak", "sensibility", 2, "offers no consolation"),
    (3, "mood.cosy", "mood_tone", 3, "wraps you in a blanket"),
    (4, "visual.neon", "visual", 2, "lit entirely by signage and rain"),
    (6, "themes.surveillance", "narrative_themes", 3, "everyone is being watched"),
    (7, "pacing.relentless", "pacing", 3, "never once lets the audience sit down"),
    (8, "register.deadpan", "register", 2, "funny with an entirely straight face"),
]

# Three pairs re-derived from above: §4.1 rule 1's overlapping (title, term) pairs, in miniature.
#   (title_id, term, facet, n_sources, sources json)
PROJECTED = [
    (1, "themes.obsession", "narrative_themes", 2, '["keyword:obsession", "keyword:heist"]'),
    (2, "mood.dread", "mood_tone", 1, '["keyword:suspense"]'),
    (3, "mood.cosy", "mood_tone", 1, '["keyword:family"]'),
    (1, "era.period", "era", 1, '["keyword:1990s"]'),
    (2, "structure.procedural", "structure", 2, '["keyword:investigation"]'),
    (5, "visual.neon", "visual", 1, '["keyword:hong-kong"]'),
    (6, "pacing.patient", "pacing", 1, '["keyword:slow-burn"]'),
    (8, "place.domestic", "place", 1, '["keyword:cooking"]'),
]

# (title_id, person_id, source, department, job, character, billing_order, role_class)
# `role_class` is normalised by the corpus, not re-derived by the app from `job`.
CREDITS = [
    (1, 1, "tmdb", "Directing", "Director", None, 0, "director"),
    # The same credit from two sources: §4.1 "dedupe at read time, never at import".
    (1, 1, "omdb", "Directing", "Director", None, 0, "director"),
    (1, 4, "tmdb", "Acting", "Actor", "Vincent Hanna", 1, "cast"),
    # The SAME (title, person, job) under a second department spelling, as TMDB files leads.
    (1, 4, "tmdb", "Actor", "Actor", "Vincent Hanna", 1, "cast"),
    (2, 2, "tmdb", "Directing", "Director", None, 0, "director"),
    (4, 3, "tmdb", "Directing", "Director", None, 0, "director"),
    (2, 5, "tmdb", "Writing", "Writer", None, 0, "writer"),      # a film…
    (6, 5, "tmdb", "Writing", "Writer", None, 0, "writer"),      # …and a series
    (8, 6, "tmdb", "Sound", "Original Music Composer", None, 0, "composer"),
]

PEOPLE = [
    (1, "Michael Mann"), (2, "Denis Villeneuve"), (3, "Wong Kar-wai"), (4, "Al Pacino"),
    # Credited on a film AND a series, so the person-filter rule can be falsified.
    (5, "Ada Cross-Kind"), (6, "Kunihiko Murai"),
]

# (title_id, source, award, category, year, result)
AWARDS = [
    (2, "imdb", "Academy Awards", "Best Cinematography", 2014, "nominated"),
    (8, "imdb", "Mainichi Film Awards", "Best Screenplay", 1986, "won"),
]

GENRES = [
    (1, "tmdb", "Crime"), (2, "tmdb", "Thriller"), (3, "tmdb", "Family"),
    (4, "tmdb", "Romance"), (5, "tmdb", "Romance"), (6, "tmdb", "Sci-Fi"),
    (7, "tmdb", "Drama"), (8, "tmdb", "Comedy"),
]

KEYWORDS = [
    (1, "tmdb", "heist"), (2, "tmdb", "investigation"), (3, "tmdb", "family"),
    (8, "tmdb", "cooking"),
]

AXES = {
    # facet -> (left pole, right pole, {term: weight})
    "mood": ("heavy", "light", {"mood.dread": -1.0, "mood.cosy": 1.0}),
    "pacing": ("patient", "propulsive", {"pacing.patient": -1.0, "pacing.relentless": 0.8}),
    "sensibility": ("bleak", "playful", {"sensibility.bleak": -1.0, "register.deadpan": 0.6}),
}

# §5.1's gate input n_t, spread near 1, near 0 and between;
# title 8's Backbone row uses `COLD_BACKBONE_ROWS`.
ITEM_SUPPORT = {1: 4218, 2: 900, 3: 120, 4: 30, 5: 6, 6: 240, 7: 55, 8: 0}
# Titles with a Backbone ROW; title 8's row is flagged, so it is out of the basis as an absent row would be.
BACKBONE_TITLES = (1, 2, 3, 4, 5, 6, 7, 8)

# Rows flagged in `cold_mask`: the count is deliberately
# ABOVE WARM_SUPPORT, or a mask-ignoring reader would pass.
COLD_BACKBONE_ROWS = {8: 900}

EMBED_DIM = 64
REVIEW_SVD_DIMS = 256

# The corpus's runtime buckets, from the shipped contract's meta block.
RUNTIME_BUCKETS = ("<80", "80-105", "105-130", "130-160", ">160")


# Far below `APP_ID_MIN`, or `break_title_id_in_app_range` would be unfalsifiable.
POOL_ID_BASE = 1_001
POOL_TMDB_BASE = 900_000

# 97% of the real owned pool fits §6's 130-minute room, so the runtimes cluster under it.
POOL_RUNTIMES = (88, 96, 102, 108, 114, 120, 124, 128)
POOL_LONG_RUNTIME = 165
POOL_LONG_EVERY = 36


class _Rows(NamedTuple):
    """`_rows(0)` returns the module-level lists themselves, so the default bundle is unchanged."""

    titles: list
    genres: list
    keywords: list
    credits: list
    extracted: list
    projected: list
    item_support: dict[int, int]
    backbone_titles: tuple[int, ...]


def _rows(pool_titles: int) -> _Rows:
    """Every generated attribute is drawn from the authored rows, so the contract width never changes."""
    if pool_titles <= 0:
        return _Rows(TITLES, GENRES, KEYWORDS, CREDITS, EXTRACTED, PROJECTED,
                     ITEM_SUPPORT, BACKBONE_TITLES)

    titles, genres, keywords = list(TITLES), list(GENRES), list(KEYWORDS)
    credits, extracted, projected = list(CREDITS), list(EXTRACTED), list(PROJECTED)
    support, backbone = dict(ITEM_SUPPORT), list(BACKBONE_TITLES)

    # Derived, not restated: a second literal list would be a second vocabulary.
    years = sorted({t[4] for t in TITLES})
    origins = sorted({(t[8], t[9]) for t in TITLES})
    genre_names = sorted({g for _, _, g in GENRES})
    keyword_names = sorted({k for _, _, k in KEYWORDS})
    roles = sorted({(pid, dept, job, role) for _, pid, _, dept, job, _, _, role in CREDITS})
    x_terms = sorted({t for _, t, _, _, _ in EXTRACTED})
    p_terms = sorted({t for _, t, _, _, _ in PROJECTED})
    # The authored support values, so the pool inherits the warm/cold split.
    supports = [ITEM_SUPPORT[t[0]] for t in TITLES]

    for i in range(pool_titles):
        title_id = POOL_ID_BASE + i
        language, country = origins[i % len(origins)]
        runtime = (POOL_LONG_RUNTIME if i % POOL_LONG_EVERY == POOL_LONG_EVERY - 1
                   else POOL_RUNTIMES[i % len(POOL_RUNTIMES)])
        titles.append((title_id, "movie", f"Pool Title {i:04d}", None, years[i % len(years)],
                       runtime, f"tt9{i:06d}", POOL_TMDB_BASE + i, language, country))
        genres.append((title_id, "tmdb", genre_names[i % len(genre_names)]))
        keywords.append((title_id, "tmdb", keyword_names[i % len(keyword_names)]))
        person_id, department, job, role_class = roles[i % len(roles)]
        credits.append((title_id, person_id, "tmdb", department, job, None, 0, role_class))
        term = x_terms[i % len(x_terms)]
        # Salience cycles 1..3: §8 stage 7's boundary is `salience IN (1,2,3)`.
        extracted.append((title_id, term, shipped_facet(term), 1 + i % 3,
                          "a synthetic quote, because a tag without one is unfalsifiable"))
        projected_term = p_terms[i % len(p_terms)]
        projected.append((title_id, projected_term, shipped_facet(projected_term), 1,
                          '["keyword:pool"]'))
        support[title_id] = supports[i % len(supports)]
        if support[title_id]:
            backbone.append(title_id)

    return _Rows(titles, genres, keywords, credits, extracted, projected, support,
                 tuple(backbone))


def make_bundle(root: Path, *, version: str = "test-v1", pool_titles: int = 0) -> Path:
    """`pool_titles` defaults to 0 because many tests and e2e specs assert the eight-title counts."""
    root.mkdir(parents=True, exist_ok=True)
    rows = _rows(pool_titles)
    _write_content(root / "content.sqlite", rows)
    _write_reviews(root / "reviews.sqlite")
    _write_artifacts(root / "artifacts", version, rows)
    # Last, because BUNDLE.json inventories the files, as the corpus writes it.
    _write_identity(root, version)
    return root


def _write_content(path: Path, rows: _Rows) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    # The corpus's DDL column for column, NOT NULLs included, so real constraint failures reproduce.
    db.executescript(
        """
        CREATE TABLE title (
            id INTEGER PRIMARY KEY AUTOINCREMENT, imdb_id TEXT UNIQUE, tmdb_id INTEGER,
            tvdb_id INTEGER, trakt_id INTEGER, wikidata_id TEXT, jellyfin_id TEXT,
            letterboxd_slug TEXT, rt_slug TEXT, metacritic_slug TEXT, wikipedia_title TEXT,
            kind TEXT NOT NULL, primary_title TEXT, original_title TEXT, year INTEGER,
            end_year INTEGER, runtime_min INTEGER, episode_count INTEGER, season_count INTEGER,
            original_language TEXT, primary_country TEXT,
            is_owned INTEGER NOT NULL DEFAULT 0, in_universe INTEGER NOT NULL DEFAULT 0,
            selection_score REAL DEFAULT 0, selection_reason TEXT, selection_bucket TEXT,
            imdb_rating REAL, imdb_votes INTEGER, tmdb_popularity REAL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE title_meta (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, year INTEGER, runtime_min INTEGER,
            tagline TEXT, plot_short TEXT, plot_full TEXT, status TEXT, original_language TEXT,
            budget INTEGER, revenue INTEGER, poster_url TEXT, backdrop_url TEXT, homepage TEXT,
            content_rating TEXT, episode_count INTEGER, season_count INTEGER,
            first_air_date TEXT, last_air_date TEXT, in_production INTEGER, extra TEXT,
            PRIMARY KEY (title_id, source));
        CREATE TABLE title_video (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, key TEXT NOT NULL, site TEXT,
            type TEXT, name TEXT, PRIMARY KEY (title_id, source, key));
        CREATE TABLE title_alias (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, alias TEXT NOT NULL, region TEXT,
            language TEXT, PRIMARY KEY (title_id, source, alias, region));
        CREATE TABLE title_genre (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, genre TEXT NOT NULL,
            position INTEGER, PRIMARY KEY (title_id, source, genre));
        CREATE TABLE title_keyword (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, keyword TEXT NOT NULL,
            weight REAL DEFAULT 1.0, PRIMARY KEY (title_id, source, keyword));
        CREATE TABLE title_country (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, country TEXT NOT NULL,
            PRIMARY KEY (title_id, source, country));
        CREATE TABLE title_language (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, language TEXT NOT NULL,
            is_primary INTEGER DEFAULT 0, PRIMARY KEY (title_id, source, language));
        CREATE TABLE person (
            id INTEGER PRIMARY KEY AUTOINCREMENT, imdb_id TEXT UNIQUE, tmdb_id INTEGER,
            name TEXT NOT NULL, birth_year INTEGER, death_year INTEGER, gender TEXT,
            profile_path TEXT, known_for TEXT);
        CREATE TABLE credit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL, source TEXT NOT NULL, department TEXT, job TEXT,
            character TEXT, billing_order INTEGER, episode_count INTEGER, role_class TEXT);
        CREATE TABLE award (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title_id INTEGER NOT NULL, source TEXT NOT NULL,
            award TEXT, category TEXT, year INTEGER, result TEXT, person TEXT, count INTEGER);
        CREATE TABLE rating_source (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, family TEXT NOT NULL,
            audience TEXT NOT NULL, origin TEXT NOT NULL, scale_lo REAL NOT NULL,
            scale_hi REAL NOT NULL, url TEXT, license TEXT, version TEXT, notes TEXT,
            loaded_at REAL, load_seconds REAL, n_users INTEGER DEFAULT 0,
            n_ratings INTEGER DEFAULT 0, n_titles INTEGER DEFAULT 0, n_seen INTEGER DEFAULT 0,
            n_unmatched INTEGER DEFAULT 0, n_dropped_thin INTEGER DEFAULT 0,
            n_duplicates INTEGER DEFAULT 0, n_collisions INTEGER DEFAULT 0,
            n_skipped INTEGER DEFAULT 0);
        CREATE TABLE platform_rating (
            title_id INTEGER NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL, value REAL,
            scale REAL, votes INTEGER, PRIMARY KEY (title_id, source, metric));
        CREATE TABLE dna_tag (
            title_id INTEGER NOT NULL, term TEXT NOT NULL, facet TEXT NOT NULL,
            salience INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
            runs_found INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (title_id, term));
        CREATE TABLE dna_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title_id INTEGER NOT NULL, term TEXT NOT NULL,
            pass_id TEXT NOT NULL, src TEXT, quote TEXT NOT NULL);
        CREATE TABLE dna_projected (
            title_id INTEGER NOT NULL, term TEXT NOT NULL, facet TEXT NOT NULL,
            n_sources INTEGER NOT NULL, sources TEXT NOT NULL, PRIMARY KEY (title_id, term));
        """
    )
    db.executemany(
        "INSERT INTO title (id, kind, primary_title, original_title, year, runtime_min,"
        " imdb_id, tmdb_id, original_language, primary_country, is_owned, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,1,0,0)",
        rows.titles,
    )
    db.executemany(
        "INSERT INTO title_meta (title_id, source, tagline, plot_short, plot_full, poster_url,"
        " backdrop_url) VALUES (?,?,?,?,?,?,?)",
        META,
    )
    db.executemany(
        "INSERT INTO title_video (title_id, source, key, site, type) VALUES (?,?,?,?,?)", VIDEOS
    )
    # Rule 6: a NULL region inside the primary key, which the importer must coalesce to ''.
    db.executemany(
        "INSERT INTO title_alias (title_id, source, alias, region, language) VALUES (?,?,?,?,?)",
        [
            (4, "tmdb", "Chung Hing sam lam", None, "yue"),
            (5, "tmdb", "Chungking Express", "HK", None),
            (1, "tmdb", "Heat", None, None),
        ],
    )
    db.executemany("INSERT INTO title_genre (title_id, source, genre) VALUES (?,?,?)", rows.genres)
    db.executemany(
        "INSERT INTO title_keyword (title_id, source, keyword) VALUES (?,?,?)", rows.keywords
    )
    db.executemany(
        "INSERT INTO title_country (title_id, source, country) VALUES (?,?,?)",
        [(t[0], "tmdb", t[9]) for t in rows.titles],
    )
    db.executemany(
        "INSERT INTO title_language (title_id, source, language, is_primary) VALUES (?,?,?,1)",
        [(t[0], "tmdb", t[8]) for t in rows.titles],
    )
    db.executemany("INSERT INTO person (id, name) VALUES (?,?)", PEOPLE)
    db.executemany(
        "INSERT INTO credit (title_id, person_id, source, department, job, character,"
        " billing_order, role_class) VALUES (?,?,?,?,?,?,?,?)",
        rows.credits,
    )
    db.executemany(
        "INSERT INTO award (title_id, source, award, category, year, result)"
        " VALUES (?,?,?,?,?,?)",
        AWARDS,
    )
    db.executemany(
        "INSERT INTO rating_source (id, name, family, audience, origin, scale_lo, scale_hi)"
        " VALUES (?,?,?,?,?,?,?)",
        [(i, f"source-{i}", "movielens", "user", "dataset", 1.0, 10.0) for i in RATING_SOURCE_IDS],
    )
    db.executemany(
        "INSERT INTO platform_rating (title_id, source, metric, value, scale, votes)"
        " VALUES (?,?,?,?,?,?)",
        [
            (1, "imdb", "user_score", 8.3, 10.0, 700000),
            (1, "metacritic", "critic_score", 76.0, 100.0, None),
            (3, "imdb", "user_score", 7.8, 10.0, 200000),
        ],
    )
    for title_id, term, facet, salience, quote in rows.extracted:
        db.execute(
            "INSERT INTO dna_tag (title_id, term, facet, salience, confidence, runs_found)"
            " VALUES (?,?,?,?,?,?)",
            (title_id, term, facet, salience, 0.4 + 0.1 * salience, salience),
        )
        db.execute(
            "INSERT INTO dna_evidence (title_id, term, pass_id, src, quote) VALUES (?,?,?,?,?)",
            (title_id, term, "pass-0", "trakt:comment", quote),
        )
    db.executemany(
        "INSERT INTO dna_projected (title_id, term, facet, n_sources, sources)"
        " VALUES (?,?,?,?,?)",
        rows.projected,
    )
    db.commit()
    db.close()


def _write_reviews(path: Path) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE review (id INTEGER PRIMARY KEY AUTOINCREMENT, title_id INTEGER NOT NULL,"
        " source TEXT NOT NULL, external_id TEXT, author TEXT, author_kind TEXT,"
        " publication TEXT, rating_raw REAL, rating_scale REAL, rating_norm REAL,"
        " rating_bucket TEXT, headline TEXT, body TEXT NOT NULL, char_count INTEGER,"
        " word_count INTEGER, language TEXT, created_date TEXT, url TEXT, is_spoiler INTEGER,"
        " helpful_yes INTEGER, helpful_total INTEGER, raw_document_id INTEGER)"
    )
    db.executemany(
        "INSERT INTO review (title_id, source, author, author_kind, body) VALUES (?,?,?,?,?)",
        [
            (1, "metacritic", "critic", "critic",
             "A city film that keeps its distance and earns it."),
            (2, "trakt", "user", "user", "Bleak, and it does not blink."),
            (5, "letterboxd", "user", "user", "王家衛の映像は今も新しい。"),
        ],
    )
    db.commit()
    db.close()


def _contract_columns(rows: _Rows) -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    """The nine content blocks named as the shipped contract names them, never `<block>:<n>`."""
    extracted_terms = sorted({t for _, t, _, _, _ in rows.extracted})
    projected_terms = sorted({t for _, t, _, _, _ in rows.projected})
    genres = sorted({g.lower() for _, _, g in rows.genres})
    keywords = sorted({k for _, _, k in rows.keywords})
    people = {p_id: name for p_id, name in PEOPLE}
    credits = sorted({f"{role}:{people[pid]}" for _, pid, _, _, _, _, _, role in rows.credits})
    countries = sorted({t[9] for t in rows.titles})
    languages = sorted({t[8] for t in rows.titles})
    decades = sorted({(t[4] // 10) * 10 for t in rows.titles})

    meta = (
        [f"kind:{k}" for k in ("movie", "series")]
        + [f"decade:{d}" for d in decades]
        + [f"runtime:{b}" for b in RUNTIME_BUCKETS]
        + [f"lang:{lang}" for lang in languages]
    )
    blocks = [
        ("dna_x", [f"dna:{t}" for t in extracted_terms]),
        ("dna_p", [f"dna:{t}" for t in projected_terms]),
        # Genome columns ship with no data: §4.3 zero-imputes the block.
        ("genome", ["g:action", "g:atmospheric", "g:cooking"]),
        ("genre", [f"genre:{g}" for g in genres]),
        ("keyword", [f"kw:{k}" for k in keywords]),
        ("credit", [f"p:{c}" for c in credits]),
        ("country", [f"country:{c}" for c in countries]),
        ("award", ["award:nominated", "award:won"]),
        ("meta", meta),
    ]
    return blocks, {}


def _write_artifacts(root: Path, version: str, rows: _Rows) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                # The shipped manifest.json carries only the fitted
                # cut-points; identity lives in BUNDLE.json.
                "fitted_cuts": {str(i): [3.5, 7.5] for i in RATING_SOURCE_IDS},
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (root / "equating_map.json").write_text(json.dumps({"version": 1, "maps": {}}), encoding="utf-8")
    # §4.3's tuned constants under the corpus's own names, so `from_mapping` sees the real keys.
    (root / "ledger_hyperparams.json").write_text(
        json.dumps(
            {
                "source": "tests/fixtures/make_bundle.py",
                "anchor_ridge_lambda": 3.0, "bt_weight_lam_bt": 0.3,
                "steps": 30, "learning_rate": 0.5,
                "margin_weighting": True,
                "margin_weight_form": "w = margin / mean(margin); 1.0 when disabled",
                "logit_clip": 30.0, "tie_prior_delta0": 0.22,
                "item_prior_shrink": 25.0, "user_offset_shrink_lam": 60.0,
                "sigma_inflation": {
                    "trigger_months": 12, "rate_c_per_sqrt_month": None, "cap": "prior_sigma",
                    "provisional": True, "note": "the trigger and cap are design (spec 5.2)",
                },
                # §6.3's thresholds (proposal 157), not yet shipped by the corpus and
                # declared in `test_bundle_shapes.py`. This literal is the only
                # `straddle_z` any stack here runs on, so it must not be a retired value.
                "straddle_z": 0.15, "tension_credible_mass": 0.80,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    blocks, _ = _contract_columns(rows)
    feature_names = [name for _, names in blocks for name in names]
    content_dim = len(feature_names)
    (root / "feature_contract.json").write_text(
        json.dumps(
            {
                "content_blocks": [{"name": n, "size": len(c)} for n, c in blocks],
                "content_dim": content_dim,
                "feature_names": feature_names,
                "input_dim": content_dim + EMBED_DIM,
                "model_file": "cold_tower.pt",
                "model_source": "tests/fixtures/make_bundle.py",
                "preprocessing": {
                    "genome": "zero-imputed for titles without MovieLens genome",
                    "absent_blocks": "dropped to zeros; the tower's dropout training anticipates"
                                     " missing blocks",
                    "missing_review_text": "zeros when covered=False",
                },
                "text_block": {
                    "source": "review_text_emb.npz:emb",
                    "columns": "0..63",
                    "dim": EMBED_DIM,
                    "order": "singular-value (descending)",
                    "text_absmax": 0.5,
                    "text_scale": 2.0,
                    "eps": 1e-09,
                    "rule": "x_text = emb[:, :64] * text_scale, frozen at export time",
                },
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    _write_model_artifacts(root, content_dim, rows)
    # The shipped entry keys have no `decade`. `TITLES`, not `rows.titles`: the onboarding list is
    # 100 titles whatever the catalog size, so a pool must not grow it.
    (root / "seed_list.json").write_text(
        json.dumps([
            {
                "title_id": t[0], "title": t[2], "year": t[4], "kind": t[1],
                "raters": 200 + t[0], "pct_dislike": 0.1, "pct_ok": 0.3, "pct_like": 0.6,
            }
            for t in TITLES
        ]),
        encoding="utf-8",
    )
    (root / "audit.json").write_text(json.dumps({"generated_by": "tests.fixtures"}), encoding="utf-8")
    # The shipped header: kind, title_id, value, evidence, note. There is no `field` column.
    (root / "corrections_v1.tsv").write_text(
        "kind\ttitle_id\tvalue\tevidence\tnote\n"
        "composer\t8\tKunihiko Murai\thttps://example.invalid/tampopo\tcredited twice upstream\n",
        encoding="utf-8",
    )
    (root / "judgement_set_v1.tsv").write_text(
        "label\tfailure_class\ta_id\ta_title\ta_year\ta_kind\tb_id\tb_title\tb_year\tb_kind\n"
        "pair-1\tnone\t1\tHeat\t1995\tmovie\t2\tPrisoners\t2013\tmovie\n",
        encoding="utf-8",
    )
    _write_vocab(root / "dna_vocab" / "v1")


# A literal keeps `make_bundle` byte-reproducible.
CREATED_AT = "2026-08-28T16:20:19.433025+00:00"


def _inventory(root: Path) -> tuple[dict[str, dict[str, object]], int]:
    """BUNDLE.json is not in its own inventory: the corpus writes it last, over the tree it describes."""
    files: dict[str, dict[str, object]] = {}
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or (path.parent == root and path.name == "BUNDLE.json"):
            continue
        payload = path.read_bytes()
        total_bytes += len(payload)
        files[path.relative_to(root).as_posix()] = {
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return files, total_bytes


def _table_counts(root: Path) -> dict[str, int]:
    """`sqlite3.connect` CREATES a missing file, so a models-only bundle's databases are not opened."""
    tables: dict[str, int] = {}
    for db_name in ("content.sqlite", "reviews.sqlite"):
        if not (root / db_name).is_file():
            continue
        db = sqlite3.connect(root / db_name)
        try:
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                tables[name] = db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        finally:
            db.close()
    return tables


def _write_identity(root: Path, version: str) -> None:
    """`BUNDLE.json` in the corpus's shape; `bundle.py` reads the vocabulary version from here."""
    files, total_bytes = _inventory(root)
    tables = _table_counts(root)

    (root / "BUNDLE.json").write_text(
        json.dumps(
            {
                "bundle_version": version,
                "created_at": CREATED_AT,
                "spec": "docs/spielplan-spec_v2.1.md §10",
                "tables": tables,
                "files": files,
                "total_bytes": total_bytes,
                # The corpus filters `imdb_ratings` and `ml_link`;
                # this fixture ships neither, so names neither.
                "filtered_tables": [],
                # §4.1 rule 5: platform ratings are display-only, never a Ledger input.
                "display_only_tables": [t for t in ("platform_rating",) if t in tables],
                "frozen_rating_source_ids": list(RATING_SOURCE_IDS),
                # Rule 6's landmine: `title_alias` carries a NULL `region` in a PK component on purpose.
                "nullable_pk_columns": {
                    "title_alias": [{"column": "region", "affinity": "TEXT"}],
                },
                "nullable_pk_note": "SQLite allows NULL in PK components; the Postgres importer "
                                    "coalesces TEXT-affinity components to '' (§4.1 rule 6).",
                "watchlist_note": "built live at export time; this fixture ships no watchlist",
                # No upstream prep artifacts; an invented digest would be worse than an empty record.
                "source_provenance": {},
                "validations": (
                    [{"check": "deny_list", "ok": True, "detail": "shipped tables clean; bad=[]"}]
                    + [
                        {"check": f"rows:{t}", "ok": True, "detail": f"src {n} == dst {n}"}
                        for t, n in tables.items()
                    ]
                ),
                "with_reviews": True,
                "with_text_components": True,
            },
            indent=1,
        ),
        encoding="utf-8",
    )


def _write_vocab(vocab: Path) -> None:
    """The corpus's file set: per-facet TSVs, a combined one, the alias map and per-TITLE adjudications."""
    vocab.mkdir(parents=True, exist_ok=True)
    header = ("id\tlabel\tgloss\topposite_gloss\tdf_lb\tdf_ub\thub_ub\taliases"
              "\tpositive_anchor\tnegative_anchor\tnotes\n")
    by_facet: dict[str, list[tuple[str, str, str]]] = {}
    for term, facet, gloss in VOCAB:
        by_facet.setdefault(facet, []).append((term, facet, gloss))
    for facet, rows in by_facet.items():
        body = header + "".join(
            f"{t}\t{t.split('.', 1)[1]}\t{g}\t\t0.01\t0.4\t0.5\t\t\t\t\n" for t, _, g in rows
        )
        (vocab / f"vocab_{facet}_v1.tsv").write_text(body, encoding="utf-8")
    (vocab / "vocab_v1_all.tsv").write_text(
        "facet\t" + header
        + "".join(
            f"{f}\t{t}\t{t.split('.', 1)[1]}\t{g}\t\t0.01\t0.4\t0.5\t\t\t\t\n"
            for t, f, g in VOCAB
        ),
        encoding="utf-8",
    )
    (vocab / "alias_map_v1.tsv").write_text(
        "raw_term\tdf\tfacet\tvocab_term\tvia_concept\tkind\n"
        "slow-burn\t12\tpacing\tpacing.patient\t\talias\n"
        "cozy\t9\tmood\tmood.cosy\t\tspelling\n",
        encoding="utf-8",
    )
    # Keyed per TITLE, not per term.
    (vocab / "adjudications_v1.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        "title\t1\tmood.cosy\tdrop\t\t\ttrakt:comment\twrong film\n"
        "global\t\tcozy\trename\tmood.cosy\t\t\tspelling\n",
        encoding="utf-8",
    )
    (vocab / "s_matrix_v1.tsv").write_text(
        "facet\ta\tb\ts\nmood\tmood.dread\tmood.cosy\t-0.8\n", encoding="utf-8"
    )
    # The corpus ships no axis TSVs yet, so they sit beside
    # the vocabulary files under the name the app reads.
    for facet, (left, right, weights) in AXES.items():
        body = f"{left}\t{right}\n" + "".join(f"{t}\t{w}\n" for t, w in weights.items())
        (vocab / f"{facet}.tsv").write_text(body, encoding="utf-8")


def _identity_tokens(ids: np.ndarray, titles: list) -> np.ndarray:
    """Decision 162's identity column, row-aligned to `title_ids`; not exported by the corpus yet."""
    spine = {t[0]: t for t in titles}
    tokens = []
    for title_id in ids.tolist():
        _, kind, _, _, _, _, imdb_id, tmdb_id, _, _ = spine[int(title_id)]
        tokens.append(f"imdb:{imdb_id}" if imdb_id else f"tmdb:{tmdb_id}:{kind}")
    return np.array(tokens, dtype="<U64")


def _write_model_artifacts(root: Path, content_dim: int, rows: _Rows) -> None:
    """Seeded, so a changed fit is a code change and never a differently drawn fixture."""
    rng = np.random.default_rng(20260830)

    # `title_ids`, plural: the name the corpus ships.
    ids = np.array(rows.backbone_titles, dtype=np.int32)
    cold = np.isin(ids, np.array(sorted(COLD_BACKBONE_ROWS), dtype=np.int32))

    # Flagged rows are spliced in as zeros; draws are sized to `~cold` so no other array's stream moves.
    kept = int((~cold).sum())
    e = np.zeros((ids.size, EMBED_DIM), dtype=np.float32)
    e[~cold] = rng.normal(scale=0.35, size=(kept, EMBED_DIM)).astype(np.float32)
    b_i = np.zeros(ids.size, dtype=np.float32)
    b_i[~cold] = rng.normal(scale=0.6, size=kept).astype(np.float32)
    b_hat = np.zeros(ids.size, dtype=np.float32)
    b_hat[~cold] = rng.normal(scale=0.6, size=kept).astype(np.float32)

    # `E_hat` is distinct from E and at the corpus's scale; flagged rows draw from their own generator.
    cold_rng = np.random.default_rng(20260911)
    e_hat = (e * 10.0).astype(np.float32)
    e_hat[cold] = cold_rng.normal(scale=3.5, size=(int(cold.sum()), EMBED_DIM)).astype(np.float32)
    b_hat[cold] = cold_rng.normal(scale=0.6, size=int(cold.sum())).astype(np.float32)

    np.savez(
        root / "backbone.npz",
        title_ids=ids,
        title_identity=_identity_tokens(ids, rows.titles),
        E=e,
        E_full=e,
        E_hat=e_hat,
        b_i=b_i,
        b_hat=b_hat,
        cold_mask=cold,
        mu=np.float32(0.12),
        item_n=np.array(
            [COLD_BACKBONE_ROWS.get(int(i), rows.item_support[int(i)]) for i in ids],
            dtype=np.int32,
        ),
    )

    text_ids = np.array([1, 2, 5], dtype=np.int32)      # the titles _write_reviews gives text
    np.savez(
        root / "review_text_emb.npz",
        title_ids=text_ids,
        emb=rng.normal(scale=1.0, size=(text_ids.size, REVIEW_SVD_DIMS)).astype(np.float32),
        covered=np.ones(text_ids.size, dtype=bool),
        singular=rng.random(REVIEW_SVD_DIMS).astype(np.float32),
    )
    np.savez(
        root / "review_text_components.npz",
        components=rng.normal(scale=0.1, size=(REVIEW_SVD_DIMS, 32)).astype(np.float32),
        singular=rng.random(REVIEW_SVD_DIMS).astype(np.float32),
        term_df=np.arange(32, dtype=np.int32),
        term_is_verdict=np.zeros(32, dtype=bool),
        term_names=np.array([f"t{i}" for i in range(32)], dtype=object),
    )

    # content_X.npz is a bare positional CSR upstream, so no id vector is written.
    all_ids = np.array([t[0] for t in rows.titles], dtype=np.int32)
    dense = (rng.random((all_ids.size, content_dim)) < 0.15).astype(np.float32)
    # Assembled with numpy: scipy is not a declared dependency. These are the arrays `save_npz` writes.
    rows, cols = np.nonzero(dense)
    np.savez(
        root / "content_X.npz",
        data=dense[rows, cols].astype(np.float32),
        indices=cols.astype(np.int32),
        indptr=np.concatenate(([0], np.cumsum(np.bincount(rows, minlength=dense.shape[0])))
                              ).astype(np.int32),
        shape=np.array(dense.shape, dtype=np.int64),
        format=np.array(b"csr"),
    )
    # `content_items.npz` is not written: nothing reads it, and a stand-in would be an invented shape.

    _write_cold_tower(root, content_dim + EMBED_DIM)


def _write_cold_tower(root: Path, input_dim: int) -> None:
    """A bare state_dict, as the corpus saves it: the
    architecture is read from the tensor shapes. CPU only."""
    import torch  # noqa: PLC0415
    from torch import nn  # noqa: PLC0415

    torch.manual_seed(20260830)

    class ColdTower(nn.Module):
        def __init__(self, in_dim: int, out_dim: int = EMBED_DIM) -> None:
            super().__init__()
            # Named `trunk.0` / `trunk.3` by the Sequential index, exactly as upstream.
            self.trunk = nn.Sequential(
                nn.Linear(in_dim, 128), nn.ReLU(), nn.Dropout(0.1), nn.Linear(128, 96), nn.ReLU()
            )
            self.head_e = nn.Linear(96, out_dim)
            self.head_b = nn.Linear(96, 1)

        def forward(self, x):
            h = self.trunk(x)
            return self.head_e(h), self.head_b(h).squeeze(-1)

    tower = ColdTower(input_dim).eval()
    torch.save(tower.state_dict(), root / "cold_tower.pt")


def reinventory(root: Path) -> None:
    """Rewrites `BUNDLE.json` over the tree as it now stands, so a helper breaks only the rule it names.
    Only `files`, `total_bytes` and `tables` are recomputed; planted declarations are kept."""
    path = root / "BUNDLE.json"
    if not path.is_file():
        # No manifest is itself a shape under test; writing one here would erase the case.
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    files, total_bytes = _inventory(root)
    payload["files"] = files
    payload["total_bytes"] = total_bytes
    payload["tables"] = _table_counts(root)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def break_rating_source_ids(root: Path) -> None:
    """rule 4 — renumber a frozen id."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE rating_source SET id = 99 WHERE id = 31")
    db.commit()
    db.close()
    reinventory(root)


def break_evidence(root: Path) -> None:
    """rule 1 — an extracted tag without its quote."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute(
        "DELETE FROM dna_evidence WHERE title_id = 1 AND term = 'themes.obsession'"
    )
    db.commit()
    db.close()
    reinventory(root)


def break_denylist(root: Path) -> None:
    """rule 7 — a %_bak% table in the export."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("CREATE TABLE title_bak (id INTEGER)")
    db.commit()
    db.close()
    reinventory(root)


def break_merged_tiers(root: Path) -> None:
    """rule 1 — the export merged the two tiers into one table."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("DROP TABLE dna_projected")
    db.commit()
    db.close()
    reinventory(root)


def break_salience(root: Path) -> None:
    """rule 2 / §8 stage 7 — salience outside {1,2,3}."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute(
        "UPDATE dna_tag SET salience = 7 WHERE title_id = 1 AND term = 'characters.morally_grey'"
    )
    db.commit()
    db.close()
    reinventory(root)


def break_title_id_in_app_range(root: Path, app_min: int) -> None:
    """Decision 162: a bundle reaching into the range Spielplan mints from."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE title SET id = ? WHERE id = 8", (app_min + 7,))
    db.commit()
    db.close()
    reinventory(root)


def break_vocabulary_version(root: Path, version: str = "v2") -> None:
    """Decision 163: a vocabulary version other than the
    active one, with the `dna_vocab/` tree moved too."""
    path = root / "BUNDLE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["vocabulary_version"] = version
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    vocab = root / "artifacts" / "dna_vocab"
    shipped = sorted(p for p in vocab.iterdir() if p.is_dir()) if vocab.is_dir() else []
    if not shipped or shipped[0].name == version:
        return
    was = shipped[0].name
    shipped[0].rename(vocab / version)
    for shipped_file in sorted((vocab / version).iterdir()):
        if f"_{was}" in shipped_file.name:
            shipped_file.rename(
                shipped_file.with_name(shipped_file.name.replace(f"_{was}", f"_{version}"))
            )
    reinventory(root)


def break_contract_block_grammar(root: Path) -> None:
    """§4.3: credit columns keyed by person id rather than by name."""
    path = root / "artifacts" / "feature_contract.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = payload["feature_names"]
    payload["feature_names"] = [
        f"credit:{i}" if n.startswith("p:") else n for i, n in enumerate(names)
    ]
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    reinventory(root)


def break_straddle_z(root: Path, value: float = 0.0) -> None:
    """§6.3: at z = 0 no title is ever badged and the queue draws from an empty pool."""
    path = root / "artifacts" / "ledger_hyperparams.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["straddle_z"] = value
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    reinventory(root)


def break_corrections_header(root: Path) -> None:
    """§8 stage 3: a corrections ledger whose header the parser does not recognise."""
    (root / "artifacts" / "corrections_v1.tsv").write_text(
        "title_id\tperson_name\tfield\told_value\tnew_value\tnote\n"
        "1\tMichael Mann\tjob\tWriter\tDirector\tan invented shape\n",
        encoding="utf-8",
    )
    reinventory(root)


def break_backbone_id_array(root: Path) -> None:
    """§4.3: a Backbone with no id vector, so rows cannot be attached to titles."""
    path = root / "artifacts" / "backbone.npz"
    z = dict(np.load(path, allow_pickle=False))
    z.pop("title_ids", None)
    np.savez(path, **z)
    reinventory(root)


def break_backbone_ids_unsorted(root: Path) -> None:
    """§4.3: ids not strictly increasing, so the row lookup is ambiguous."""
    path = root / "artifacts" / "backbone.npz"
    z = dict(np.load(path, allow_pickle=False))
    ids = z["title_ids"]
    z["title_ids"] = np.concatenate([ids[1:2], ids[:1], ids[2:]])
    np.savez(path, **z)
    reinventory(root)


def break_cold_tower_heads(root: Path) -> None:
    """§8 stage 9: heads not named `head_e` / `head_b` cannot be reconstructed."""
    import torch  # noqa: PLC0415

    path = root / "artifacts" / "cold_tower.pt"
    state = torch.load(path, map_location="cpu", weights_only=True)
    renamed = {k.replace("head_e", "embed").replace("head_b", "prior"): v
               for k, v in state.items()}
    torch.save(renamed, path)
    reinventory(root)


def break_identity_missing(root: Path) -> None:
    """Decision 162: no identity column, the state of every bundle exported so far."""
    path = root / "artifacts" / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files if k != "title_identity"}
    np.savez(path, **arrays)
    reinventory(root)


def break_identity_mismatch(root: Path) -> None:
    """Decision 162: an identity column that disagrees with the title it names."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE title SET imdb_id = 'tt0000001' WHERE id = 1")
    db.commit()
    db.close()
    reinventory(root)
