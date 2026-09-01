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
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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

# (title_id, term, facet, salience, quote) — the extracted tier, every tag with its quote.
EXTRACTED = [
    (1, "themes.obsession", "themes", 3, "the work eats the man and he lets it"),
    (1, "characters.morally_grey", "characters", 2, "nobody here is owed your sympathy"),
    (2, "mood.dread", "mood", 3, "a low hum of dread that outlasts the final scene"),
    (2, "sensibility.bleak", "sensibility", 2, "offers no consolation"),
    (3, "mood.cosy", "mood", 3, "wraps you in a blanket"),
    (4, "visual.neon", "visual", 2, "lit entirely by signage and rain"),
    (6, "themes.surveillance", "themes", 3, "everyone is being watched"),
    (7, "pacing.relentless", "pacing", 3, "never once lets the audience sit down"),
    (8, "register.deadpan", "register", 2, "funny with an entirely straight face"),
]

# The projected tier deliberately re-derives three pairs that also exist above: §4.1 rule 1's
# "14,181 (title,term) pairs exist in both and must stay distinguishable", in miniature.
#   (title_id, term, facet, n_sources, sources json)
PROJECTED = [
    (1, "themes.obsession", "themes", 2, '["keyword:obsession", "keyword:heist"]'),
    (2, "mood.dread", "mood", 1, '["keyword:suspense"]'),
    (3, "mood.cosy", "mood", 1, '["keyword:family"]'),
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
# gate = n/(n+10) has a value near 1, a value near 0, and something in between.
ITEM_SUPPORT = {1: 4218, 2: 900, 3: 120, 4: 30, 5: 6, 6: 240, 7: 55, 8: 0}
# Titles the Backbone actually has a row for. Title 8 is deliberately absent: §5.1's cold
# branch has to be reachable, and §12's M2 exit criterion is about exactly those titles.
BACKBONE_TITLES = (1, 2, 3, 4, 5, 6, 7)

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


def make_bundle(root: Path, *, version: str = "test-v1") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_content(root / "content.sqlite")
    _write_reviews(root / "reviews.sqlite")
    _write_artifacts(root / "artifacts", version)
    return root


def _write_content(path: Path) -> None:
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
        TITLES,
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
    db.executemany("INSERT INTO title_genre (title_id, source, genre) VALUES (?,?,?)", GENRES)
    db.executemany("INSERT INTO title_keyword (title_id, source, keyword) VALUES (?,?,?)", KEYWORDS)
    db.executemany(
        "INSERT INTO title_country (title_id, source, country) VALUES (?,?,?)",
        [(t[0], "tmdb", t[9]) for t in TITLES],
    )
    db.executemany(
        "INSERT INTO title_language (title_id, source, language, is_primary) VALUES (?,?,?,1)",
        [(t[0], "tmdb", t[8]) for t in TITLES],
    )
    db.executemany("INSERT INTO person (id, name) VALUES (?,?)", PEOPLE)
    db.executemany(
        "INSERT INTO credit (title_id, person_id, source, department, job, character,"
        " billing_order, role_class) VALUES (?,?,?,?,?,?,?,?)",
        CREDITS,
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
    for title_id, term, facet, salience, quote in EXTRACTED:
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
        PROJECTED,
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


def _contract_columns() -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    """The nine content blocks, each named the way the shipped contract names them.

    This is the whole point of the M4.5 rewrite: `dna:`, `g:`, `genre:`, `kw:`, `p:<role>:`,
    `country:`, `award:`, and a `meta` block of one-hot buckets — not `<block>:<n>`, which is
    what the fixture used to declare and which reduces to whatever bare key the builder happened
    to emit.
    """
    extracted_terms = sorted({t for _, t, _, _, _ in EXTRACTED})
    projected_terms = sorted({t for _, t, _, _, _ in PROJECTED})
    genres = sorted({g.lower() for _, _, g in GENRES})
    keywords = sorted({k for _, _, k in KEYWORDS})
    people = {p_id: name for p_id, name in PEOPLE}
    credits = sorted({f"{role}:{people[pid]}" for _, pid, _, _, _, _, _, role in CREDITS})
    countries = sorted({t[9] for t in TITLES})
    languages = sorted({t[8] for t in TITLES})
    decades = sorted({(t[4] // 10) * 10 for t in TITLES})

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


def _write_artifacts(root: Path, version: str) -> None:
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
    (root / "ledger_hyperparams.json").write_text(
        json.dumps(
            {
                "lambda_ridge": 3.0, "lambda_bt": 1.0, "steps": 200, "lr": 0.1,
                "margin_weighting": True, "margin_form": "margin/mean(margin)",
                "tie_prior_delta0": 0.22, "b_i_tau": 1.0,
                "sigma_inflation_c": 0.05, "sigma_inflation_cap": "prior",
                # §6.3's two thresholds, shipped rather than defaulted. Proposal 157: "any
                # threshold that is a bare σ constant belongs in ledger_hyperparams.json".
                "straddle_z": 1.0, "tension_credible_mass": 0.80,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    blocks, _ = _contract_columns()
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
    _write_model_artifacts(root, content_dim)
    (root / "seed_list.json").write_text(
        json.dumps([{"title_id": t[0], "decade": (t[4] // 10) * 10} for t in TITLES]),
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
    # BUNDLE.json at the bundle root is where the corpus records identity; artifacts/manifest.json
    # does not carry it, and reading the version from there named every real bundle "unknown".
    (root.parent / "BUNDLE.json").write_text(
        json.dumps({"bundle_version": version, "vocabulary_version": "v1",
                    "title_count": len(TITLES)}, indent=1),
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


def _write_model_artifacts(root: Path, content_dim: int) -> None:
    """backbone.npz, cold_tower.pt, review_text_emb.npz, content_X.npz.

    Deterministic: a seeded generator, so a fit that changes is a code change and never a
    fixture that happened to be drawn differently.
    """
    rng = np.random.default_rng(20260830)

    # `title_ids`, plural — the name the corpus ships. The app demanded `title_id` and would
    # have found nothing in a real bundle.
    ids = np.array(BACKBONE_TITLES, dtype=np.int32)
    e = rng.normal(scale=0.35, size=(ids.size, EMBED_DIM)).astype(np.float32)
    np.savez(
        root / "backbone.npz",
        title_ids=ids,
        E=e,
        E_full=e,
        E_hat=e,
        b_i=rng.normal(scale=0.6, size=ids.size).astype(np.float32),
        b_hat=rng.normal(scale=0.6, size=ids.size).astype(np.float32),
        cold_mask=np.zeros(ids.size, dtype=bool),
        mu=np.float32(0.12),
        item_n=np.array([ITEM_SUPPORT[i] for i in ids], dtype=np.int32),
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
    all_ids = np.array([t[0] for t in TITLES], dtype=np.int32)
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
    import torch                                                          # noqa: PLC0415
    from torch import nn                                                  # noqa: PLC0415

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
