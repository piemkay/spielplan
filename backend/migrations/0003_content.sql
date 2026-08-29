-- 0003_content — the content spine imported from the corpus bundle, then maintained by the app.
-- Spec v2.1 §4.1. Every numbered rule below is quoted from that section; the DDL is the
-- enforcement, and the importer (spielplan/importer/) enforces what DDL cannot.
--
-- SHAPE NOTE: §4.1 says "tables mirror the corpus export (§10 manifest)". The corpus project is
-- not vendored here, so the column sets below are the app's canonical shape derived from the
-- spec's named tables and rules. The importer maps bundle columns onto these by name and its
-- validation report lists every bundle column it could not place — an unmapped column is a
-- report line, never a silent drop and never an import failure.

-- Rule 3: "platform_rating lives in a display-only schema the feature builder cannot import
-- from." A separate schema makes that a grantable boundary rather than a comment: the feature
-- builder connects with a search_path/role that has no USAGE on `display`.
CREATE SCHEMA display;
-- "review (separate schema or DB — 312 MB with bodies)"
CREATE SCHEMA review_store;

-- ---------------------------------------------------------------------------
-- title — the spine.
-- Rule: "canonical key: title.id integer — carried over verbatim; imdb_id is NULL on 21% of
-- titles and must never be the join key."
-- Rule 5: "kind (movie/series) is non-null, indexed, and every ranking surface partitions by it
-- (measured: the unpartitioned crowd top-10 is 8/10 TV series)."
-- Rule 6: "do not add UNIQUE constraints on tmdb_id/trakt_id/slugs (315/171/… duplicate values
-- exist, mostly legitimate movie/series pairs)." — hence plain indexes below.
-- ---------------------------------------------------------------------------
CREATE TABLE title (
    id            integer PRIMARY KEY,          -- carried over verbatim from the corpus
    kind          text NOT NULL CHECK (kind IN ('movie', 'series')),
    name          text NOT NULL,
    original_name text,
    year          smallint,
    runtime_min   integer,
    imdb_id       text,                          -- NULL on 21% of titles; never a join key
    tmdb_id       integer,
    tvdb_id       integer,
    trakt_id      integer,
    trakt_slug    text,
    letterboxd_slug text,
    rt_slug       text,
    metacritic_slug text,
    jellyfin_id   text,
    -- §7.2: "mark removed titles is_owned = false (flag re-derived from Jellyfin, never trusted
    -- stale — the corpus flag is equivalent to jellyfin_id IS NOT NULL today but goes stale the
    -- moment the library changes)."
    is_owned      boolean NOT NULL DEFAULT false,
    owned_checked_at timestamptz,
    overview      text,
    tagline       text,
    poster_path   text,
    backdrop_path text,
    trailer_key   text,
    -- §8 stage 10: "appears in ranking/search/explore with a 'new — model placement, no crowd
    -- data' badge until ratings accrue."
    placement     text NOT NULL DEFAULT 'unplaced'
                  CHECK (placement IN ('unplaced', 'cold_tower', 'warm')),
    placement_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX title_kind ON title (kind);                       -- rule 5
CREATE INDEX title_kind_name ON title (kind, lower(name));
CREATE INDEX title_year ON title (year);
CREATE INDEX title_runtime ON title (runtime_min);
CREATE INDEX title_owned ON title (is_owned) WHERE is_owned;
CREATE INDEX title_imdb ON title (imdb_id) WHERE imdb_id IS NOT NULL;
CREATE INDEX title_tmdb ON title (tmdb_id) WHERE tmdb_id IS NOT NULL;   -- NOT unique (rule 6)
CREATE INDEX title_trakt ON title (trakt_id) WHERE trakt_id IS NOT NULL; -- NOT unique (rule 6)
CREATE INDEX title_jellyfin ON title (jellyfin_id) WHERE jellyfin_id IS NOT NULL;

-- "title_meta (multi-source, per-source rows kept — 'one block = one droppable source')"
CREATE TABLE title_meta (
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    source     text NOT NULL,                   -- tmdb | omdb | trakt | wikidata | tvmaze | …
    fetched_at timestamptz,
    payload    jsonb NOT NULL,
    PRIMARY KEY (title_id, source)
);

-- Rule 6: "coalesce NULLable PK components (title_alias.region etc.) to ''".
CREATE TABLE title_alias (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    alias    text NOT NULL,
    region   text NOT NULL DEFAULT '',
    language text NOT NULL DEFAULT '',
    kind     text NOT NULL DEFAULT '',
    PRIMARY KEY (title_id, alias, region, language, kind)
);
CREATE INDEX title_alias_lookup ON title_alias (lower(alias));

CREATE TABLE title_genre (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    genre    text NOT NULL,
    source   text NOT NULL DEFAULT '',
    PRIMARY KEY (title_id, genre, source)
);
CREATE INDEX title_genre_genre ON title_genre (genre);

CREATE TABLE title_keyword (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    keyword  text NOT NULL,
    source   text NOT NULL DEFAULT '',
    PRIMARY KEY (title_id, keyword, source)
);
CREATE INDEX title_keyword_keyword ON title_keyword (keyword);

CREATE TABLE title_language (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    language text NOT NULL,
    role     text NOT NULL DEFAULT '',
    PRIMARY KEY (title_id, language, role)
);

CREATE TABLE title_country (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    country  text NOT NULL,
    PRIMARY KEY (title_id, country)
);

CREATE TABLE title_company (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    company  text NOT NULL,
    role     text NOT NULL DEFAULT '',
    PRIMARY KEY (title_id, company, role)
);

CREATE TABLE title_video (
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    site     text NOT NULL DEFAULT '',
    key      text NOT NULL,
    type     text NOT NULL DEFAULT '',
    official boolean,
    PRIMARY KEY (title_id, site, key)
);

CREATE TABLE person (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    imdb_id    text,
    tmdb_id    integer,
    birth_year smallint,
    profile_path text
);
CREATE INDEX person_name ON person (lower(name));

-- Rule: "credit (dedupe at read time, never at import)" — so no unique constraint on the
-- natural key, and `source` is part of the identity of a row.
CREATE TABLE credit (
    id         bigserial PRIMARY KEY,
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    person_id  integer NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    department text NOT NULL DEFAULT '',
    job        text NOT NULL DEFAULT '',
    character  text,
    ord        integer,
    source     text NOT NULL DEFAULT ''
);
CREATE INDEX credit_title ON credit (title_id);
CREATE INDEX credit_person ON credit (person_id);

CREATE TABLE award (
    id        bigserial PRIMARY KEY,
    title_id  integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    body      text NOT NULL,
    category  text NOT NULL DEFAULT '',
    year      smallint,
    won       boolean,
    person_id integer REFERENCES person(id) ON DELETE SET NULL
);
CREATE INDEX award_title ON award (title_id);

-- ---------------------------------------------------------------------------
-- Rule 3, enforced by schema separation.
-- "Aggregate platform scores are a popularity conduit and are banned as model features
--  (measured: popularity penalty −0.010 Spearman for nothing)."
-- ---------------------------------------------------------------------------
CREATE TABLE display.platform_rating (
    title_id integer NOT NULL,      -- deliberately NOT a FK across the schema boundary:
    platform text NOT NULL,         -- display rows must never make the feature builder's
    score    double precision,      -- planner touch this schema.
    votes    bigint,
    fetched_at timestamptz,
    PRIMARY KEY (title_id, platform)
);
COMMENT ON SCHEMA display IS
    'Spec §4.1 rule 3: display-only. The feature builder connects without USAGE here. '
    'Nothing in this schema may become a model feature.';

-- ---------------------------------------------------------------------------
-- MovieLens genome slice
-- ---------------------------------------------------------------------------
CREATE TABLE ml_genome_tag (
    tag_id integer PRIMARY KEY,
    tag    text NOT NULL
);

CREATE TABLE ml_link (
    ml_movie_id integer PRIMARY KEY,
    title_id    integer REFERENCES title(id) ON DELETE SET NULL,
    imdb_id     text,
    tmdb_id     integer
);
CREATE INDEX ml_link_title ON ml_link (title_id);

CREATE TABLE ml_genome_score (
    ml_movie_id integer NOT NULL,
    tag_id      integer NOT NULL REFERENCES ml_genome_tag(tag_id),
    relevance   real NOT NULL,
    PRIMARY KEY (ml_movie_id, tag_id)
);

-- ---------------------------------------------------------------------------
-- Rating sources.
-- Rule 4: "rating_source.id values (1,2,3,4,7,11,21,23,26,28,31) are FROZEN — they key
-- fitted_cuts, equating_map, and the dataset arrays. Never renumber."
-- The CHECK is the guard: an import that renumbers fails loudly instead of quietly
-- invalidating every calibration artifact.
-- ---------------------------------------------------------------------------
CREATE TABLE rating_source (
    id    smallint PRIMARY KEY
          CHECK (id IN (1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31)),
    name  text NOT NULL,
    scale text NOT NULL DEFAULT ''
);

CREATE TABLE rating_title_map (
    source_id  smallint NOT NULL REFERENCES rating_source(id),
    source_key text NOT NULL,
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, source_key)
);
CREATE INDEX rating_title_map_title ON rating_title_map (title_id);

-- ---------------------------------------------------------------------------
-- Reviews — separate schema, bodies included (needed for re-extraction and text embedding).
-- Rule 8: "UTF-8 everywhere; never 'clean' non-ASCII … the 73 known-mojibake review rows are
-- fixed individually in the importer."
-- ---------------------------------------------------------------------------
CREATE TABLE review_store.review (
    id        bigserial PRIMARY KEY,
    title_id  integer NOT NULL,
    source    text NOT NULL,
    author    text,
    url       text,
    rating    double precision,
    published_at timestamptz,
    is_critic boolean,
    body      text NOT NULL,
    word_count integer GENERATED ALWAYS AS
        (array_length(regexp_split_to_array(btrim(body), E'\\s+'), 1)) STORED
);
CREATE INDEX review_title ON review_store.review (title_id);
CREATE INDEX review_source ON review_store.review (source);

-- ---------------------------------------------------------------------------
-- Onboarding + wanted lists that travel with the bundle (§4.3, §10).
-- ---------------------------------------------------------------------------
CREATE TABLE seed_list (
    position smallint PRIMARY KEY,      -- 100-title decade-stratified onboarding list
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    decade   smallint
);

CREATE TABLE watchlist (
    title_id integer PRIMARY KEY REFERENCES title(id) ON DELETE CASCADE,
    source   text NOT NULL DEFAULT '',
    added_at timestamptz
);
