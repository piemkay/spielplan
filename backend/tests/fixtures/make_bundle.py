"""Build a synthetic artifact bundle shaped like the §10 manifest.

The corpus project is not vendored into this repo, so the importer is tested against a bundle
this module generates. It reproduces the *shapes and the landmines*, not the volume: the two
DNA tiers with overlapping (title,term) pairs, the frozen rating_source ids, duplicate
tmdb_ids across a movie/series pair, NULL alias PK components, non-ASCII text, and an
extracted tag that carries its evidence quote.

`make_bundle(dir)` produces a clean bundle. The `break_*` helpers produce bundles that violate
one rule each, so the validator can be tested on the failures it exists to catch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# §4.1 rule 4 — the frozen ids.
RATING_SOURCE_IDS = (1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31)

TITLES = [
    # id, kind, name, year, runtime, imdb, tmdb
    (1, "movie", "Heat", 1995, 170, "tt0113277", 949),
    (2, "movie", "Prisoners", 2013, 153, "tt1392214", 146233),
    (3, "movie", "Paddington 2", 2017, 103, None, 346648),          # imdb_id NULL (21% case)
    (4, "movie", "Chungking Express", 1994, 102, "tt0109424", 11104),
    (5, "movie", "重慶森林", 1994, 102, None, 11104),                 # CJK + duplicate tmdb_id
    (6, "series", "Severance", 2022, 48, "tt11280740", 95396),
    (7, "series", "The Bear", 2022, 30, "tt14452776", 136315),
    (8, "movie", "Tampopo", 1985, 114, "tt0092048", 11081),
]

VOCAB = [
    ("dread", "mood", "a low hum of dread that outlasts the final scene"),
    ("cosy", "mood", "wraps you in a blanket and never curdles into sugar"),
    ("obsession", "themes", "the work eats the man and he lets it"),
    ("surveillance", "themes", "everyone is being watched and half of them know it"),
    ("patient", "pacing", "trusts you to wait, and the waiting pays"),
    ("relentless", "pacing", "never once lets the audience sit down"),
    ("procedural", "structure", "built out of process: forms, interviews, dead ends"),
    ("neon", "visual", "lit entirely by signage and rain"),
    ("score-forward", "sound", "the score is arguing with the picture"),
    ("morally-grey", "character", "nobody here is owed your sympathy"),
    ("domestic", "place", "kitchens, hallways, and the arguments they hold"),
    ("period", "era", "the past rendered as a working place, not a costume"),
    ("bleak", "sensibility", "offers no consolation and does not pretend to"),
    ("deadpan", "register", "funny with an entirely straight face"),
]

# (title_id, term, facet, salience, quote) — the extracted tier, every tag with its quote.
EXTRACTED = [
    (1, "obsession", "themes", 3, "the work eats the man and he lets it"),
    (1, "morally-grey", "character", 2, "nobody here is owed your sympathy"),
    (2, "dread", "mood", 3, "a low hum of dread that outlasts the final scene"),
    (2, "bleak", "sensibility", 2, "offers no consolation"),
    (3, "cosy", "mood", 3, "wraps you in a blanket"),
    (4, "neon", "visual", 2, "lit entirely by signage and rain"),
    (6, "surveillance", "themes", 3, "everyone is being watched"),
    (7, "relentless", "pacing", 3, "never once lets the audience sit down"),
    (8, "deadpan", "register", 2, "funny with an entirely straight face"),
]

# The projected tier deliberately re-derives three pairs that also exist above: §4.1 rule 1's
# "14,181 (title,term) pairs exist in both and must stay distinguishable", in miniature.
PROJECTED = [
    (1, "obsession", "themes", 0.8, "keyword:obsession"),       # shared with extracted
    (2, "dread", "mood", 0.6, "keyword:suspense"),              # shared with extracted
    (3, "cosy", "mood", 0.7, "keyword:family"),                 # shared with extracted
    (1, "period", "era", 0.4, "keyword:1990s"),
    (2, "procedural", "structure", 0.5, "keyword:investigation"),
    (5, "neon", "visual", 0.6, "keyword:hong-kong"),
    (6, "patient", "pacing", 0.5, "keyword:slow-burn"),
    (8, "domestic", "place", 0.3, "keyword:cooking"),
]

AXES = {
    # facet -> (left pole, right pole, {term: weight})
    "mood": ("heavy", "light", {"dread": -1.0, "cosy": 1.0}),
    "pacing": ("patient", "propulsive", {"patient": -1.0, "relentless": 0.8}),
    "sensibility": ("bleak", "playful", {"bleak": -1.0, "deadpan": 0.6}),
}


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
    db.executescript(
        """
        CREATE TABLE title (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, original_name TEXT,
            year INTEGER, runtime_min INTEGER, imdb_id TEXT, tmdb_id INTEGER, tvdb_id INTEGER,
            trakt_id INTEGER, trakt_slug TEXT, letterboxd_slug TEXT, rt_slug TEXT,
            metacritic_slug TEXT, jellyfin_id TEXT, is_owned INTEGER, overview TEXT,
            tagline TEXT, poster_path TEXT, backdrop_path TEXT, trailer_key TEXT);
        CREATE TABLE title_alias (title_id INTEGER, alias TEXT, region TEXT, language TEXT, kind TEXT);
        CREATE TABLE title_genre (title_id INTEGER, genre TEXT, source TEXT);
        CREATE TABLE title_keyword (title_id INTEGER, keyword TEXT, source TEXT);
        CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT, imdb_id TEXT, tmdb_id INTEGER,
            birth_year INTEGER, profile_path TEXT);
        CREATE TABLE credit (title_id INTEGER, person_id INTEGER, department TEXT, job TEXT,
            character TEXT, ord INTEGER, source TEXT);
        CREATE TABLE rating_source (id INTEGER PRIMARY KEY, name TEXT, scale TEXT);
        CREATE TABLE platform_rating (title_id INTEGER, platform TEXT, score REAL, votes INTEGER);
        CREATE TABLE dna_tag (id INTEGER PRIMARY KEY, title_id INTEGER, term TEXT, facet TEXT,
            salience INTEGER, confidence REAL, n_sources INTEGER, provider TEXT);
        CREATE TABLE dna_evidence (id INTEGER PRIMARY KEY, dna_tag_id INTEGER, quote TEXT,
            source TEXT, source_ref TEXT);
        CREATE TABLE dna_projected (title_id INTEGER, term TEXT, facet TEXT, weight REAL, via TEXT);
        """
    )
    db.executemany(
        "INSERT INTO title (id, kind, name, year, runtime_min, imdb_id, tmdb_id, is_owned, overview)"
        " VALUES (?,?,?,?,?,?,?,1,?)",
        [(i, k, n, y, r, im, tm, f"{n} — a synthetic overview with emoji 🎬 and a ZWSP​.")
         for i, k, n, y, r, im, tm in TITLES],
    )
    # rule 6: NULL PK components that the importer must coalesce to ''.
    db.executemany(
        "INSERT INTO title_alias (title_id, alias, region, language, kind) VALUES (?,?,?,?,?)",
        [
            (4, "Chung Hing sam lam", None, "yue", None),
            (5, "Chungking Express", "HK", None, "original"),
            (1, "Heat", None, None, None),
        ],
    )
    db.executemany(
        "INSERT INTO title_genre (title_id, genre, source) VALUES (?,?,?)",
        [(1, "Crime", "tmdb"), (2, "Thriller", "tmdb"), (3, "Family", "tmdb"),
         (4, "Romance", "tmdb"), (5, "Romance", None), (6, "Sci-Fi", "tmdb"),
         (7, "Drama", "tmdb"), (8, "Comedy", "tmdb")],
    )
    db.executemany(
        "INSERT INTO title_keyword (title_id, keyword, source) VALUES (?,?,?)",
        [(1, "heist", "tmdb"), (2, "investigation", "tmdb"), (3, "family", "tmdb")],
    )
    db.executemany(
        "INSERT INTO person (id, name) VALUES (?,?)",
        [(1, "Michael Mann"), (2, "Denis Villeneuve"), (3, "Wong Kar-wai"), (4, "Al Pacino"),
         # Credited on a film AND a series. At catalog scale the cross-kind credit is the
         # common case, and a fixture without one cannot falsify the person-filter rule.
         (5, "Ada Cross-Kind")],
    )
    # The same credit from two sources — dedupe happens at READ time, never at import.
    db.executemany(
        "INSERT INTO credit (title_id, person_id, department, job, character, ord, source)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            (1, 1, "Directing", "Director", None, 0, "tmdb"),
            (1, 1, "Directing", "Director", None, 0, "omdb"),
            (1, 4, "Acting", "Actor", "Vincent Hanna", 1, "tmdb"),
            (2, 2, "Directing", "Director", None, 0, "tmdb"),
            (4, 3, "Directing", "Director", None, 0, "tmdb"),
            (2, 5, "Writing", "Writer", None, 0, "tmdb"),      # a film…
            (6, 5, "Writing", "Writer", None, 0, "tmdb"),      # …and a series
        ],
    )
    db.executemany(
        "INSERT INTO rating_source (id, name, scale) VALUES (?,?,?)",
        [(i, f"source-{i}", "1-10") for i in RATING_SOURCE_IDS],
    )
    db.executemany(
        "INSERT INTO platform_rating (title_id, platform, score, votes) VALUES (?,?,?,?)",
        [(1, "imdb", 8.3, 700000), (1, "metacritic", 76.0, None), (3, "imdb", 7.8, 200000)],
    )
    for tag_id, (title_id, term, facet, salience, quote) in enumerate(EXTRACTED, start=1):
        db.execute(
            "INSERT INTO dna_tag (id, title_id, term, facet, salience, confidence, n_sources,"
            " provider) VALUES (?,?,?,?,?,?,?,?)",
            (tag_id, title_id, term, facet, salience, 0.4 + 0.1 * salience, salience, "gemini"),
        )
        db.execute(
            "INSERT INTO dna_evidence (dna_tag_id, quote, source, source_ref) VALUES (?,?,?,?)",
            (tag_id, quote, "trakt:comment", f"c{tag_id}"),
        )
    db.executemany(
        "INSERT INTO dna_projected (title_id, term, facet, weight, via) VALUES (?,?,?,?,?)",
        PROJECTED,
    )
    db.commit()
    db.close()


def _write_reviews(path: Path) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE review (id INTEGER PRIMARY KEY, title_id INTEGER, source TEXT, author TEXT,"
        " url TEXT, rating REAL, published_at TEXT, is_critic INTEGER, body TEXT)"
    )
    db.executemany(
        "INSERT INTO review (title_id, source, author, body, is_critic) VALUES (?,?,?,?,?)",
        [
            (1, "metacritic", "critic", "A city film that keeps its distance and earns it.", 1),
            (2, "trakt", "user", "Bleak, and it does not blink.", 0),
            (5, "letterboxd", "user", "王家衛の映像は今も新しい。", 0),
        ],
    )
    db.commit()
    db.close()


def _write_artifacts(root: Path, version: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_version": version,
                "vocabulary_version": "v1",
                "title_count": len(TITLES),
                "owned_count": len(TITLES),
                # §4.3: "manifest.json (fitted 3-class cut-points per source)"
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
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (root / "feature_contract.json").write_text(
        json.dumps(
            {
                "blocks": {
                    "dna_x": 433, "dna_p": 556, "genome": 983, "genre": 179, "keyword": 3884,
                    "credit": 244, "country": 97, "award": 2, "meta": 57,
                },
                "review_text": {"svd_dims": 256, "used": 64, "order": "singular-value"},
                "text_scale": 0.031_25,
                "genome_imputation": "zero",
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (root / "seed_list.json").write_text(
        json.dumps({"titles": [{"title_id": t[0], "decade": (t[3] // 10) * 10} for t in TITLES]}),
        encoding="utf-8",
    )
    (root / "audit.json").write_text(json.dumps({"generated_by": "tests.fixtures"}), encoding="utf-8")
    (root / "corrections_v1.tsv").write_text(
        "title_id\tperson_name\tfield\told_value\tnew_value\tnote\n"
        "1\tMichael Mann\tjob\tWriter\tDirector\tcredited twice upstream\n",
        encoding="utf-8",
    )
    (root / "judgement_set_v1.tsv").write_text("title_a\ttitle_b\n1\t2\n", encoding="utf-8")

    vocab = root / "dna_vocab" / "v1"
    vocab.mkdir(parents=True, exist_ok=True)
    (vocab / "terms.tsv").write_text(
        "term\tfacet\tgloss\n" + "".join(f"{t}\t{f}\t{g}\n" for t, f, g in VOCAB),
        encoding="utf-8",
    )
    (vocab / "aliases.tsv").write_text(
        "alias\tterm\nslow-burn\tpatient\ncozy\tcosy\n", encoding="utf-8"
    )
    (vocab / "adjudications.tsv").write_text(
        "term\tverdict\ttarget\tnote\ncozy\trename\tcosy\tspelling\n", encoding="utf-8"
    )
    axes = vocab / "axes"
    axes.mkdir(exist_ok=True)
    for facet, (left, right, weights) in AXES.items():
        body = f"{left}\t{right}\n" + "".join(f"{t}\t{w}\n" for t, w in weights.items())
        (axes / f"{facet}.tsv").write_text(body, encoding="utf-8")


# --- deliberately broken bundles, one rule each ---------------------------------


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
    db.execute("DELETE FROM dna_evidence WHERE dna_tag_id = 1")
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
    db.execute("UPDATE dna_tag SET salience = 7 WHERE id = 2")
    db.commit()
    db.close()
