"""Build a synthetic artifact bundle shaped like the one the corpus actually exports.

The corpus project is not vendored into this repo, so the importer is tested against a bundle
this module generates. It reproduces the *shapes and the landmines*, not the volume: the two
DNA tiers with overlapping (title,term) pairs, the frozen rating_source ids, duplicate
tmdb_ids across a movie/series pair, NULL alias PK components, non-ASCII text, and an
extracted tag that carries its evidence quote.

**The shapes are not invented here.** Until M4.5 they were, and that is what let the whole
import layer be verified against this repo's reading of §10 rather than against the artifact:
the fixture declared `title.name` where the corpus ships `primary_title`, a feature column
`credit:3` where the corpus ships `p:director:Michael Mann`, and a `corrections_v1.tsv` header
with a `field` column that does not exist. Every structure below is now taken from
`tests/fixtures/real_bundle_shapes.json`, which `ops/bundle_shapes.py` extracts from a real
bundle, and `test_bundle_shapes.py` fails if the two drift apart.

`make_bundle(dir)` produces a clean bundle. The `break_*` helpers produce bundles that violate
one rule each, so the validator can be tested on the failures it exists to catch.

Volume is the one exception, and it is opt-in: `make_bundle(dir, pool_titles=700)` appends
generated owned movies drawn entirely from the authored vocabulary. Eight titles cannot seed the
pool the Tonight selector's replay cost is visible over -- the real bundle owns 696 movies at
§6's default room. The pair search alone is guarded without a bundle, on a synthesized belief
board (e87deed); what only a fixture can supply is a seeded pool of that size, which is what
M4.12's exit script measures the round over. A measurement nothing can fail is what M4.8 exists
to end.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

import numpy as np

# §4.1 rule 4 — the frozen ids.
RATING_SOURCE_IDS = (1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31)

# The corpus's own per-field precedence (`mdc/export.py`'s SOURCE_PRIORITY), abbreviated to the
# sources this fixture carries. It is data here, not a constant in the app: decision 162 makes
# the app the consumer of an order the corpus owns.
SOURCE_PRIORITY = ("tmdb", "omdb", "trakt", "tvmaze", "wikipedia")

# id, kind, primary_title, original_title, year, runtime_min, imdb_id, tmdb_id, language, country
TITLES = [
    (1, "movie", "Heat", None, 1995, 170, "tt0113277", 949, "en", "United States of America"),
    (2, "movie", "Prisoners", None, 2013, 153, "tt1392214", 146233, "en", "United States of America"),
    # imdb_id NULL — the 21% case §4.1 names as the reason title.id is the join key.
    (3, "movie", "Paddington 2", None, 2017, 103, None, 346648, "en", "United Kingdom"),
    (4, "movie", "Chungking Express", "重慶森林", 1994, 102, "tt0109424", 11104, "yue", "Hong Kong"),
    # CJK primary title AND a duplicate tmdb_id with title 4 — §4.1 rule 6's legitimate
    # duplicate, which must survive an import that adds no UNIQUE constraint.
    (5, "movie", "重慶森林", "Chungking Express", 1994, 102, None, 11104, "yue", "Hong Kong"),
    (6, "series", "Severance", None, 2022, 48, "tt11280740", 95396, "en", "United States of America"),
    (7, "series", "The Bear", None, 2022, 30, "tt14452776", 136315, "en", "United States of America"),
    (8, "movie", "Tampopo", None, 1985, 114, "tt0092048", 11081, "ja", "Japan"),
]

# title_meta is per source, and §4.1 keeps the rows because "one block = one droppable source".
# The sources here are deliberately COMPLEMENTARY rather than ranked copies: tmdb has a tagline
# and a plot and a poster, omdb has a poster and no tagline, wikipedia is the only source with
# plot_short. A whole-block precedence rule blanks fields another source has — which is why the
# corpus resolves per field, and why this fixture can tell the two rules apart.
#   (title_id, source, tagline, plot_short, plot_full, poster_url, backdrop_url)
META = [
    (1, "tmdb", "A Los Angeles crime saga.", None,
     "Heat — a synthetic plot with emoji 🎬 and a ZWSP​.", "/heat.jpg", "/heat-bd.jpg"),
    (1, "omdb", None, None, "A shorter synthetic plot.", "/heat-omdb.jpg", None),
    (1, "wikipedia", None, "A one-line synthetic summary.", None, None, None),
    # No tmdb row: the preferred source is simply absent, and the next one carries the fields.
    (2, "omdb", None, None, "Prisoners — a synthetic plot.", "/prisoners.jpg", None),
    (2, "wikipedia", None, "A one-line synthetic summary.", None, None, None),
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

# The vocabulary term IS `facet.term` — the corpus ships `dna:mood.bittersweet`, so the facet is
# already inside the id and a builder that prepends it again produces `mood.mood.dread`.
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

# The corpus's *extraction* labels, as shipped in `dna_tag.facet` and `dna_projected.facet`.
# The term ids and every vocabulary file carry the short facet id the term is prefixed with
# (`mood.dread` -> `mood`); the two DNA tables carry the label the extraction pass ran under,
# and the three below are the three the review measured on the real bundle. 29,188 of 31,540
# `dna_tag` rows and 206,151 of 223,136 `dna_projected` rows mismatch there.
#
# `0004_dna.sql:73-90` and `:104-116` give neither column a foreign key to `dna_facet` — the one
# `dna_term:38` and `dna_axis:58` both carry — which is exactly why the mismatch is silent: the
# claim is about two tables, so it cites both, and the range that shipped covered `dna_tag`
# alone. `importer/dna.py:295-310` and `:365-380` copy the shipped column
# verbatim while `load_vocabulary` derives its facet from the prefix, so the join is empty and
# every facet renders in the neutral colour with nothing raised anywhere. Three measured labels
# plus the identical remainder IS the shape — a fourth, invented label would be the
# fixture-invents-a-structure failure M4.5 exists to end. The repair is M4.9's.
EXTRACTION_LABELS = {
    "mood": "mood_tone", "themes": "narrative_themes", "characters": "character_dynamics",
}


def shipped_facet(term: str) -> str:
    """The `facet` column a bundle carries for `term` — the extraction label where one was
    measured, the term's own prefix everywhere else. The literals below are written out row by
    row because they are data; this is the rule the generated pool applies."""
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

# The projected tier deliberately re-derives three pairs that also exist above: §4.1 rule 1's
# "14,181 (title,term) pairs exist in both and must stay distinguishable", in miniature.
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
# `role_class` is what the contract's `p:<role_class>:<name>` grammar is built from, and it is
# a column the corpus normalises rather than a string the app re-derives from `job`.
CREDITS = [
    (1, 1, "tmdb", "Directing", "Director", None, 0, "director"),
    # The same credit from two sources — §4.1: "dedupe at read time, never at import".
    (1, 1, "omdb", "Directing", "Director", None, 0, "director"),
    (1, 4, "tmdb", "Acting", "Actor", "Vincent Hanna", 1, "cast"),
    # The SAME (title, person, job) filed under a second department spelling. TMDB records
    # leads under both `Acting` and `Actor`, and the real export carries 7,918 such triples
    # across 1,216 of its 19,071 titles — 816 of them inside the twelve credits §6.0's card
    # renders. §4.1 says "dedupe at read time, never at import", so the collision is meant to
    # reach the read layer intact and be collapsed there; until this row the only bundle in the
    # suite gave every credit a distinct (title, person, department, job), so the shape existed
    # only where `test_import_integration.py` inserted one by hand. A test that proves the
    # query while the fixture cannot produce its input is a statement about the fixture. M4.9
    # owns the render-side repair -- shipped at `ee35d52`, which groups `credits_for` by
    # (person, job) -- and it is not touched here.
    (1, 4, "tmdb", "Actor", "Actor", "Vincent Hanna", 1, "cast"),
    (2, 2, "tmdb", "Directing", "Director", None, 0, "director"),
    (4, 3, "tmdb", "Directing", "Director", None, 0, "director"),
    (2, 5, "tmdb", "Writing", "Writer", None, 0, "writer"),      # a film…
    (6, 5, "tmdb", "Writing", "Writer", None, 0, "writer"),      # …and a series
    (8, 6, "tmdb", "Sound", "Original Music Composer", None, 0, "composer"),
]

PEOPLE = [
    (1, "Michael Mann"), (2, "Denis Villeneuve"), (3, "Wong Kar-wai"), (4, "Al Pacino"),
    # Credited on a film AND a series. At catalog scale the cross-kind credit is the common
    # case, and a fixture without one cannot falsify the person-filter rule.
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

# §5.1's gate input, n_t. Deliberately spread: title 1 is well covered, title 8 has nothing, so
# gate = n/(n+10) has a value near 1, a value near 0, and something in between. Title 8's own
# Backbone row carries `COLD_BACKBONE_ROWS[8]` instead — a flagged row's crowd count is real
# where its coordinate is not — and the 0 here is what the generated pool inherits, which is what
# keeps one title in eight out of the basis altogether.
ITEM_SUPPORT = {1: 4218, 2: 900, 3: 120, 4: 30, 5: 6, 6: 240, 7: 55, 8: 0}
# Titles the Backbone has a ROW for, which §4.3 does not make the same thing as the titles it
# carries a coordinate for. Title 8's row is the flagged one below, so it is excluded from the
# basis exactly as an absent row would be: §5.1's cold branch stays reachable and §12's M2 exit
# criterion is still about those titles, while the file now has the shape the corpus's file has.
BACKBONE_TITLES = (1, 2, 3, 4, 5, 6, 7, 8)

# The rows the export flags in `cold_mask`: title id -> the crowd count the FILE carries for it.
# v20260828 flags 2,879 of 14,397 rows — E is written as zeros for every one of them and the
# coordinate the corpus does have lives in `E_hat`/`b_hat` — and 1,915 of those clear
# `scoring.backbone.WARM_SUPPORT` (90), which is the whole of cs-01: on support alone they were
# stamped warm, excused from the Cold Tower sweep that exists to give them a coordinate, and
# served at e(t) = 0 for ever. So the count here is deliberately ABOVE the threshold and
# deliberately not `ITEM_SUPPORT[8]`; a fixture where the two agreed would be passed by a reader
# that ignores the mask entirely, which is the reader this repository shipped. The row lives here
# rather than in a second npz because the M4.13 plan's §8 says it must.
# [M4.13 cycle 1, M413-REV-02]
COLD_BACKBONE_ROWS = {8: 900}

EMBED_DIM = 64
REVIEW_SVD_DIMS = 256

# The corpus's runtime buckets, from the shipped contract's meta block.
RUNTIME_BUCKETS = ("<80", "80-105", "105-130", "130-160", ">160")


def runtime_bucket(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    if minutes < 80:
        return "<80"
    if minutes < 105:
        return "80-105"
    if minutes < 130:
        return "105-130"
    # `< 160`, not `<= 160`: `mdc/ratings/features.py:89` puts 160 itself in `>160`, and the
    # tower was trained on that binning. An off-by-one here moves every 160-minute film into a
    # column it was not trained in.
    if minutes < 160:
        return "130-160"
    return ">160"


# --- the pool: the one thing this fixture reproduces by volume rather than by shape -----------

# The generated ids sit far below `importer.bundle.APP_ID_MIN` (1,000,000,000): decision 162
# partitions the corpus's namespace from the household's, and a pool that reached into the
# app's half would make `break_title_id_in_app_range` unfalsifiable.
POOL_ID_BASE = 1_001
POOL_TMDB_BASE = 900_000

# The one attribute the pool spreads on purpose, and the proportion is measured rather than
# chosen. §6's default room is 130 minutes; the real bundle yields 696 owned movies inside it
# against 716 at 200, so 97% of the owned pool fits and only one title in thirty-six does not.
# A pool spread evenly across the runtime buckets would hand M4.12 a third fewer titles than
# the room really holds and understate the selector's cost by exactly that much. Everything
# else a generated title carries is drawn from the authored rows below, which is what keeps the
# feature contract's width and every block's grammar identical at any pool size.
POOL_RUNTIMES = (88, 96, 102, 108, 114, 120, 124, 128)
POOL_LONG_RUNTIME = 165
POOL_LONG_EVERY = 36


class _Rows(NamedTuple):
    """The rows one bundle is written from.

    Four writers used to read the module-level lists directly, which is why a pool could not
    exist: the eight authored titles were a global rather than an argument. `_rows(0)` returns
    those same objects, so the default bundle is the one all 26 call sites already build.
    """

    titles: list
    genres: list
    keywords: list
    credits: list
    extracted: list
    projected: list
    item_support: dict[int, int]
    backbone_titles: tuple[int, ...]


def _rows(pool_titles: int) -> _Rows:
    """The authored rows, plus `pool_titles` generated owned movies.

    Every generated attribute is *drawn from* the authored rows — the same genres, keywords,
    people, terms, years, languages and countries — so `_contract_columns` derives the identical
    column list at any pool size. That is the property the pool has to have: a wider contract is
    a different `input_dim`, and the tower a timing test loads would no longer be the tower the
    rest of the suite loads.
    """
    if pool_titles <= 0:
        return _Rows(TITLES, GENRES, KEYWORDS, CREDITS, EXTRACTED, PROJECTED,
                     ITEM_SUPPORT, BACKBONE_TITLES)

    titles, genres, keywords = list(TITLES), list(GENRES), list(KEYWORDS)
    credits, extracted, projected = list(CREDITS), list(EXTRACTED), list(PROJECTED)
    support, backbone = dict(ITEM_SUPPORT), list(BACKBONE_TITLES)

    # Derived, not restated: a second literal list would be a second vocabulary, free to drift
    # from the authored one and widen a block without anything noticing.
    years = sorted({t[4] for t in TITLES})
    origins = sorted({(t[8], t[9]) for t in TITLES})
    genre_names = sorted({g for _, _, g in GENRES})
    keyword_names = sorted({k for _, _, k in KEYWORDS})
    roles = sorted({(pid, dept, job, role) for _, pid, _, dept, job, _, _, role in CREDITS})
    x_terms = sorted({t for _, t, _, _, _ in EXTRACTED})
    p_terms = sorted({t for _, t, _, _, _ in PROJECTED})
    # The authored support values, in title order — so the pool inherits the warm/cold split
    # §5.1's gate branches on rather than a flat one: one title in eight has n_t = 0 and no
    # Backbone row, which is the cold branch §12's M2 criterion is about.
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
        # salience cycles 1..3 because §8 stage 7's trust boundary is `salience IN (1,2,3)` and
        # the validator counts every row outside it.
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
    """A clean bundle. `pool_titles` appends that many generated owned movies to the authored
    eight, and defaults to 0 because 26 call sites and two browser specs assert the small
    counts — `test_import_integration.py:258` reads `movie_total == 6` and
    `e2e/specs/10-home.spec.js` says "the fixture bundle owns six"."""
    root.mkdir(parents=True, exist_ok=True)
    rows = _rows(pool_titles)
    _write_content(root / "content.sqlite", rows)
    _write_reviews(root / "reviews.sqlite")
    _write_artifacts(root / "artifacts", version, rows)
    # Last, because BUNDLE.json inventories the files: the corpus writes it at the end of the
    # export for the same reason.
    _write_identity(root, version)
    return root


def _write_content(path: Path, rows: _Rows) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    # The DDL below is the corpus's, column for column. Types and NOT NULLs included, because
    # a fixture that relaxes them cannot reproduce a constraint failure the real bundle would.
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
    # rule 6: a NULL region inside the primary key, which the importer must coalesce to ''.
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


# --- the feature contract, built from the fixture's own rows in the corpus's grammar ----------


def _contract_columns(rows: _Rows) -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    """The nine content blocks, each named the way the shipped contract names them.

    This is the whole point of the M4.5 rewrite: `dna:`, `g:`, `genre:`, `kw:`, `p:<role>:`,
    `country:`, `award:`, and a `meta` block of one-hot buckets — not `<block>:<n>`, which is
    what the fixture used to declare and which reduces to whatever bare key the builder happened
    to emit.
    """
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
        # The genome block ships columns and no data: §4.3 zero-imputes it, and the corpus
        # reaches it through MovieLens ids this fixture deliberately does not carry.
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
                # The shipped manifest.json carries the fitted cut-points and nothing else; the
                # bundle's identity lives in BUNDLE.json at the root (see below).
                "fitted_cuts": {str(i): [3.5, 7.5] for i in RATING_SOURCE_IDS},
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (root / "equating_map.json").write_text(json.dumps({"version": 1, "maps": {}}), encoding="utf-8")
    # §4.3's "tuned constants of the §5.2 recipe", under the corpus's own names. The fixture
    # declared `lambda_ridge`, `lambda_bt`, `lr`, `margin_form`, `b_i_tau`, `sigma_inflation_c`
    # and `sigma_inflation_cap` — the names `ledger/hyperparams.py` reads — and the corpus ships
    # `anchor_ridge_lambda`, `bt_weight_lam_bt`, `learning_rate`, `margin_weight_form` and a
    # nested `sigma_inflation` object, plus three constants this app has no field for at all.
    # Under the old names the fixture agreed with the reader about a spelling neither shares
    # with the artifact, so `from_mapping` could never report the real bundle's unknown keys.
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
                # §6.3's two thresholds, shipped rather than defaulted. Proposal 157: "any
                # threshold that is a bare σ constant belongs in ledger_hyperparams.json". The
                # corpus does not ship them yet, so `test_bundle_shapes.py` carries them as a
                # declared exception (`PROPOSAL_157_NOT_YET_SHIPPED`) rather than silently.
                "straddle_z": 1.0, "tension_credible_mass": 0.80,
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
    # The shipped entry keys: kind, pct_dislike, pct_like, pct_ok, raters, title, title_id,
    # year. There is no `decade` — §4.3's "decade-stratified" is a property of the selection,
    # not a column, and the importer read `item["decade"]` until M4.5, so the real list loaded
    # with the one property it exists for NULL on every row.
    #
    # `TITLES`, not `rows.titles`, and deliberately: §4.3's onboarding list is "100-title
    # decade-stratified" whatever the catalog's size, so it is the one artifact a pool must NOT
    # grow. A 706-entry seed list would be a shape no export has ever produced.
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
    # The shipped header: kind, title_id, value, evidence, note. There is no `field` column, and
    # the app read one until M4.5 — a KeyError rather than a validation failure.
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


# The corpus stamps the export time; a literal keeps `make_bundle` byte-reproducible, for the
# same reason `_write_model_artifacts` draws from a seeded generator.
CREATED_AT = "2026-08-28T16:20:19.433025+00:00"


def _write_identity(root: Path, version: str) -> None:
    """`BUNDLE.json` — the corpus's own record of what a bundle is, in the shape it writes it.

    The fixture used to write three keys, two of which (`vocabulary_version`, `title_count`) no
    bundle has ever carried, and omitted every one that a bundle does: `tables`, `files`,
    `validations`, `source_provenance`, `filtered_tables`, `display_only_tables`,
    `nullable_pk_columns`, `frozen_rating_source_ids`. That is the invention this milestone
    exists to end, one file outside `artifacts/` — and it is load-bearing, because `bundle.py`
    reads the vocabulary version from here (decision 163's refusal has nothing to compare
    without it) and against a real bundle resolves it only through the fallback to the
    `dna_vocab/<version>/` directory name.
    """
    files: dict[str, dict[str, object]] = {}
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        total_bytes += len(payload)
        files[path.relative_to(root).as_posix()] = {
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        }

    tables: dict[str, int] = {}
    for db_name in ("content.sqlite", "reviews.sqlite"):
        db = sqlite3.connect(root / db_name)
        try:
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                tables[name] = db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        finally:
            db.close()

    (root / "BUNDLE.json").write_text(
        json.dumps(
            {
                "bundle_version": version,
                "created_at": CREATED_AT,
                "spec": "docs/spielplan-spec_v2.1.md §10",
                "tables": tables,
                "files": files,
                "total_bytes": total_bytes,
                # The corpus ships `imdb_ratings` and `ml_link` filtered to the bundle's titles.
                # This fixture carries neither table, and naming a table it does not ship would
                # be the same invention in a smaller font.
                "filtered_tables": [],
                # §4.1 rule 5: platform ratings are display-only, never a Ledger input.
                "display_only_tables": [t for t in ("platform_rating",) if t in tables],
                "frozen_rating_source_ids": list(RATING_SOURCE_IDS),
                # rule 6's landmine, declared where the corpus declares it: the fixture's
                # `title_alias` carries a NULL `region` in a PK component on purpose.
                "nullable_pk_columns": {
                    "title_alias": [{"column": "region", "affinity": "TEXT"}],
                },
                "nullable_pk_note": "SQLite allows NULL in PK components; the Postgres importer "
                                    "coalesces TEXT-affinity components to '' (§4.1 rule 6).",
                "watchlist_note": "built live at export time; this fixture ships no watchlist",
                # Upstream prep artifacts the export was built from. This fixture has none, and
                # an invented digest of a file that does not exist is worse than an empty record.
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
    """`dna_vocab/v1/` in the corpus's own file set: per-facet vocab TSVs, a combined one, the
    alias map, and a per-TITLE adjudications ledger. The app read terms.tsv / aliases.tsv /
    adjudications.tsv, none of which the corpus ships."""
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
    # Keyed per TITLE, not per term: the app's `ON CONFLICT (version, term) DO UPDATE` would
    # have collapsed 817 per-title verdicts onto one row per term.
    (vocab / "adjudications_v1.tsv").write_text(
        "scope\ttitle_id\tterm\taction\ttarget\tquote\tsource\tnote\n"
        "title\t1\tmood.cosy\tdrop\t\t\ttrakt:comment\twrong film\n"
        "global\t\tcozy\trename\tmood.cosy\t\t\tspelling\n",
        encoding="utf-8",
    )
    (vocab / "s_matrix_v1.tsv").write_text(
        "facet\ta\tb\ts\nmood\tmood.dread\tmood.cosy\t-0.8\n", encoding="utf-8"
    )
    # §6.4's axis definitions. The corpus ships no axis TSVs (proposal 140 asks for them), so
    # the fixture ships them under the name the app reads and the gap is recorded in the plan.
    axes = vocab / "axes"
    axes.mkdir(exist_ok=True)
    for facet, (left, right, weights) in AXES.items():
        body = f"{left}\t{right}\n" + "".join(f"{t}\t{w}\n" for t, w in weights.items())
        (axes / f"{facet}.tsv").write_text(body, encoding="utf-8")


def _identity_tokens(ids: np.ndarray, titles: list) -> np.ndarray:
    """decision 162's identity column, row-aligned to `title_ids`.

    Range partitioning stops two minters colliding; it cannot see the corpus *merging* two
    titles, which changes what an id means without changing the id. The token names the axis it
    is asserting on — `imdb:<imdb_id>` where the spine has one, else `tmdb:<tmdb_id>:<kind>` —
    and titles 3 and 5 exercise the fallback, which is the 21% case §4.1 names.

    The corpus does not ship this array yet; the exporter has to add it. The fixture ships it
    because the row requires the importer to check it, and `test_bundle_shapes.py` declares the
    gap rather than hiding it.
    """
    spine = {t[0]: t for t in titles}
    tokens = []
    for title_id in ids.tolist():
        _, kind, _, _, _, _, imdb_id, tmdb_id, _, _ = spine[int(title_id)]
        tokens.append(f"imdb:{imdb_id}" if imdb_id else f"tmdb:{tmdb_id}:{kind}")
    return np.array(tokens, dtype="<U64")


def _write_model_artifacts(root: Path, content_dim: int, rows: _Rows) -> None:
    """backbone.npz, cold_tower.pt, review_text_emb.npz, content_X.npz.

    Deterministic: a seeded generator, so a fit that changes is a code change and never a
    fixture that happened to be drawn differently.
    """
    rng = np.random.default_rng(20260830)

    # `title_ids`, plural — the name the corpus ships. The app demanded `title_id` and would
    # have found nothing in a real bundle.
    ids = np.array(rows.backbone_titles, dtype=np.int32)
    cold = np.isin(ids, np.array(sorted(COLD_BACKBONE_ROWS), dtype=np.int32))

    # The drawn rows are the ones that HAVE a coordinate, and the flagged rows are spliced in as
    # zeros — which is what the export writes for them, and the reason `cold_mask` is not a
    # courtesy: zeros read as a coordinate to anything that takes E at face value. Sizing the
    # draws to `~cold` rather than to `ids` is what keeps every other row of E, b_i and b_hat —
    # and every array drawn after them, down to `content_X` — bit for bit what they were before
    # a flagged row existed. A generator whose stream moves when a row is added makes "the fit
    # changed" and "the fixture was drawn differently" look the same, which is the one thing the
    # seeded generator is here to prevent.
    kept = int((~cold).sum())
    e = np.zeros((ids.size, EMBED_DIM), dtype=np.float32)
    e[~cold] = rng.normal(scale=0.35, size=(kept, EMBED_DIM)).astype(np.float32)
    b_i = np.zeros(ids.size, dtype=np.float32)
    b_i[~cold] = rng.normal(scale=0.6, size=kept).astype(np.float32)
    b_hat = np.zeros(ids.size, dtype=np.float32)
    b_hat[~cold] = rng.normal(scale=0.6, size=kept).astype(np.float32)

    # `E_hat` is a different array from E and sits at the corpus's scale (median ||E_hat|| 27.05
    # over every row against a median ||E|| of 0.3835 over the rows that carry a coordinate at
    # all; x10 puts this fixture's ~2.8 near 28). It was E itself, which made the
    # one array that holds a flagged row's real coordinate indistinguishable from the array that
    # by construction does not hold it. Nothing in the app reads E_hat — decision 236 sends the
    # scale question upstream — so what matters is that the two are distinct and differently
    # scaled, not the ratio they land at. The flagged rows draw theirs from a generator of their
    # own, for the reason above: `rng` must not be asked for anything on their account.
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

    # content_X.npz is a bare scipy CSR upstream — no ids, positional only. It is written the
    # same way here so nothing in this repo can quietly start depending on an id vector that a
    # real bundle does not carry.
    all_ids = np.array([t[0] for t in rows.titles], dtype=np.int32)
    dense = (rng.random((all_ids.size, content_dim)) < 0.15).astype(np.float32)
    # CSR assembled with numpy rather than scipy: scipy is not a declared dependency and
    # `test_every_third_party_import_is_a_declared_dependency` would fail on one added for a
    # fixture. The five arrays below are exactly what `scipy.sparse.save_npz` writes, which is
    # what the corpus ships.
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
    # `content_items.npz` is deliberately NOT written. The corpus ships it with fifteen arrays
    # of per-item statistics and nothing under `backend/spielplan/` reads any of them; a
    # two-array stand-in would be a shape this fixture invented, which is the whole failure
    # M4.5 exists to end. When something reads it, it gets written then — faithfully.

    _write_cold_tower(root, content_dim + EMBED_DIM)


def _write_cold_tower(root: Path, input_dim: int) -> None:
    """cold_tower.pt — saved the way the corpus's exporter saves it: a **bare state_dict**.

    §4.3 calls this "the live model; the exporter must ship v2", and the app required a wrapper
    carrying `version`, `arch` and `input_dim`. The corpus ships `torch.save(model.state_dict())`
    and nothing else, so the architecture has to be read out of the tensor shapes — which it
    can be, unambiguously: `trunk.0.weight` is (hidden, input_dim) and `head_e.weight` is
    (embed_dim, hidden).

    §1 is CPU-only, and this is built and saved on the CPU with no device in the state dict.
    """
    import torch  # noqa: PLC0415
    from torch import nn  # noqa: PLC0415

    torch.manual_seed(20260830)

    class ColdTower(nn.Module):
        def __init__(self, in_dim: int, out_dim: int = EMBED_DIM) -> None:
            super().__init__()
            # Named `trunk.0` / `trunk.3` by the Sequential index, exactly as upstream: Linear,
            # ReLU, Dropout, Linear, ReLU.
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


# --- deliberately broken bundles, one rule each -----------------------------------------------


def break_rating_source_ids(root: Path) -> None:
    """rule 4 — renumber a frozen id."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE rating_source SET id = 99 WHERE id = 31")
    db.commit()
    db.close()


def break_kind(root: Path) -> None:
    """rule 5 — a null kind."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE title SET kind = NULL WHERE id = 3")
    db.commit()
    db.close()


def break_evidence(root: Path) -> None:
    """rule 1 — an extracted tag without its quote."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute(
        "DELETE FROM dna_evidence WHERE title_id = 1 AND term = 'themes.obsession'"
    )
    db.commit()
    db.close()


def break_denylist(root: Path) -> None:
    """rule 7 — a %_bak% table in the export."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("CREATE TABLE title_bak (id INTEGER)")
    db.commit()
    db.close()


def break_merged_tiers(root: Path) -> None:
    """rule 1 — the export merged the two tiers into one table."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("DROP TABLE dna_projected")
    db.commit()
    db.close()


def break_salience(root: Path) -> None:
    """rule 2 / §8 stage 7 — salience outside {1,2,3}."""
    db = sqlite3.connect(root / "content.sqlite")
    db.execute(
        "UPDATE dna_tag SET salience = 7 WHERE title_id = 1 AND term = 'characters.morally_grey'"
    )
    db.commit()
    db.close()


def break_title_id_in_app_range(root: Path, app_min: int) -> None:
    """decision 162 — a bundle reaching into the range Spielplan mints from.

    The failure the whole id partition exists to make impossible: two minters in one namespace.
    A bundle carrying an id at or above `app_min` claims a title the household may already have
    acquired, and nothing downstream can tell the two apart.
    """
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE title SET id = ? WHERE id = 8", (app_min + 7,))
    db.commit()
    db.close()


def break_vocabulary_version(root: Path, version: str = "v2") -> None:
    """decision 163 — a bundle whose vocabulary version differs from the active one.

    Deferred as a migration, refused in the meantime: swapping it would leave `dna_tag` and
    `dna_projected` at the old version while the feature builder filters on the active one, so
    both DNA blocks empty for every title — and empty is not an error anywhere in the read path.
    """
    path = root / "BUNDLE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["vocabulary_version"] = version
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def break_contract_block_grammar(root: Path) -> None:
    """§4.3 — a contract whose credit columns are keyed by person id rather than by name.

    This is the shape the fixture itself used to declare, and it is what made the defect
    invisible: a builder emitting `person_id::text` agrees with it perfectly.
    """
    path = root / "artifacts" / "feature_contract.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = payload["feature_names"]
    payload["feature_names"] = [
        f"credit:{i}" if n.startswith("p:") else n for i, n in enumerate(names)
    ]
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def break_straddle_z(root: Path, value: float = 0.0) -> None:
    """§4.3 / §6.3 — a non-positive straddle threshold.

    The only §6.3 rule a *bundle* can violate. At z = 0 no posterior ever reaches a neighbour,
    so no title is ever badged and the comparison queue draws from an empty pool: the surface
    looks calm and is broken, which is the failure mode `from_mapping`'s refusal exists for.
    """
    path = root / "artifacts" / "ledger_hyperparams.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["straddle_z"] = value
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def break_tension_credible_mass(root: Path, value: float = 1.0) -> None:
    """§4.3 / §6.3 — a credible mass that is not a probability.

    At 1.0 the interval is the whole line, so no assigned tier is ever outside it and the
    tension badge silently stops existing — §6.3's "shows the tension rather than snapping
    back" turns off with no error anywhere.
    """
    path = root / "artifacts" / "ledger_hyperparams.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tension_credible_mass"] = value
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def break_corrections_header(root: Path) -> None:
    """§4.3 / §8 stage 3 — a corrections ledger whose header the parser does not recognise.

    The real file is `kind, title_id, value, evidence, note`; the app read a `field` column that
    no shipped ledger has, so the ledger raised KeyError instead of failing the report.
    """
    (root / "artifacts" / "corrections_v1.tsv").write_text(
        "title_id\tperson_name\tfield\told_value\tnew_value\tnote\n"
        "1\tMichael Mann\tjob\tWriter\tDirector\tan invented shape\n",
        encoding="utf-8",
    )


def break_backbone_id_array(root: Path) -> None:
    """§4.3 — a Backbone with no id vector at all.

    §4.3 names E, E_full, b_i, mu and item_n and no mapping, so the mapping is exactly what a
    bundle can omit while looking complete. Without it a row of E cannot be attached to a title
    and every coordinate is plausible and wrong.
    """
    path = root / "artifacts" / "backbone.npz"
    z = dict(np.load(path, allow_pickle=False))
    z.pop("title_ids", None)
    np.savez(path, **z)


def break_backbone_ids_unsorted(root: Path) -> None:
    """§4.3 — an id vector that is not strictly increasing.

    Duplicate or unsorted ids make the row lookup ambiguous rather than wrong-and-detectable.
    """
    path = root / "artifacts" / "backbone.npz"
    z = dict(np.load(path, allow_pickle=False))
    ids = z["title_ids"]
    z["title_ids"] = np.concatenate([ids[1:2], ids[:1], ids[2:]])
    np.savez(path, **z)


def break_cold_tower_heads(root: Path) -> None:
    """§4.3 / §8 stage 9 — a checkpoint whose heads this app cannot find.

    The corpus names them `head_e` / `head_b`. A checkpoint naming them anything else cannot be
    reconstructed, and §5.1 needs both halves of the cold branch.
    """
    import torch  # noqa: PLC0415

    path = root / "artifacts" / "cold_tower.pt"
    state = torch.load(path, map_location="cpu", weights_only=True)
    renamed = {k.replace("head_e", "embed").replace("head_b", "prior"): v
               for k, v in state.items()}
    torch.save(renamed, path)


def break_unknown_table(root: Path) -> None:
    """§10 — a bundle table nothing accounts for.

    Not a denylisted `%_bak%` table: a plausible new table the exporter started shipping. §10
    requires "counts per table", and a table nobody maps used to produce no line at all.
    """
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("CREATE TABLE title_sentiment (title_id INTEGER, score REAL)")
    db.commit()
    db.close()


def break_identity_missing(root: Path) -> None:
    """decision 162 — a model bundle carrying no identity column at all.

    This is the state of every bundle the corpus has exported so far, which is exactly why an
    absent identity vector is a refusal rather than a skipped check: a check that quietly does
    not run on the only bundles in existence is not a check.
    """
    path = root / "artifacts" / "backbone.npz"
    with np.load(path, allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files if k != "title_identity"}
    np.savez(path, **arrays)


def break_identity_mismatch(root: Path) -> None:
    """decision 162 — a model bundle whose identity column disagrees with the title it names.

    Range partitioning stops two minters colliding; it cannot see the corpus *merging* two
    titles, which changes what an existing id means without changing the id. The identity
    vector is the only thing that can.
    """
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE title SET imdb_id = 'tt0000001' WHERE id = 1")
    db.commit()
    db.close()


def break_title_meta_only_source(root: Path) -> None:
    """§4.1 — every per-source meta row dropped.

    The card must render without overview, tagline, poster and trailer rather than erroring:
    §4.1 keeps the rows because "one block = one droppable source", and dropping the last one is
    the limit of that rule.
    """
    db = sqlite3.connect(root / "content.sqlite")
    db.execute("DELETE FROM title_meta")
    db.commit()
    db.close()
