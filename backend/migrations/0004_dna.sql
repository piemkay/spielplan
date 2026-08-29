-- 0004_dna — the naming layer. Spec v2.1 §4.1 rules 1 and 2, §4.3 (dna_vocab/v1), §6.4.
--
-- Rule 1: "dna_tag (extracted, quote-verified; 2,016 titles) and dna_projected (inferred;
-- 11,324 titles) are SEPARATE TABLES, NEVER MERGED, NEVER UNIONED; every read joins carry a
-- `tier` discriminator; 14,181 (title,term) pairs exist in both and must stay distinguishable.
-- dna_evidence ships with the extracted tier — a tag without its quote is unfalsifiable."
--
-- Rule 2: "salience, confidence, n_sources are WEIGHTS, NEVER FILTERS. No `WHERE confidence > x`
-- anywhere (a 0.5 cut would delete 44% of the extracted tier; union recalls 93%, intersection
-- 67%)."  There is no DDL that forbids a WHERE clause, so the guard is threefold: this comment,
-- the read layer in spielplan/db/dna.py which exposes weights only as ORDER BY / score inputs,
-- and a test that greps the codebase for confidence/salience predicates.

-- ---------------------------------------------------------------------------
-- Vocabulary (shipped in the bundle as dna_vocab/<version>/)
-- ---------------------------------------------------------------------------
CREATE TABLE dna_vocabulary (
    version     text PRIMARY KEY,
    imported_at timestamptz NOT NULL DEFAULT now(),
    facet_count integer NOT NULL,
    term_count  integer NOT NULL
);

CREATE TABLE dna_facet (
    version  text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    facet    text NOT NULL,
    ord      smallint NOT NULL,
    colour   text,                         -- §6.8: "a fixed colour per vocabulary facet (11)"
    PRIMARY KEY (version, facet)
);

CREATE TABLE dna_term (
    version    text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    term       text NOT NULL,
    facet      text NOT NULL,
    gloss      text,
    PRIMARY KEY (version, term),
    FOREIGN KEY (version, facet) REFERENCES dna_facet(version, facet) ON DELETE CASCADE
);
CREATE INDEX dna_term_facet ON dna_term (version, facet);

CREATE TABLE dna_alias (
    version  text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    alias    text NOT NULL,
    term     text NOT NULL,
    PRIMARY KEY (version, alias)
);

-- §6.4: "Axis definitions are a shipped, authored artifact: one TSV per vocabulary-v1 facet
-- (left pole, right pole, term → weight ∈ [−1, 1]) … editable in the §6.6 ledger editor."
-- Deterministic — no nightly rebuild, no Procrustes anchoring, no map shift on re-import.
CREATE TABLE dna_axis (
    version    text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    facet      text NOT NULL,
    left_pole  text NOT NULL,
    right_pole text NOT NULL,
    PRIMARY KEY (version, facet),
    FOREIGN KEY (version, facet) REFERENCES dna_facet(version, facet) ON DELETE CASCADE
);

CREATE TABLE dna_axis_weight (
    version text NOT NULL,
    facet   text NOT NULL,
    term    text NOT NULL,
    weight  real NOT NULL CHECK (weight >= -1.0 AND weight <= 1.0),
    PRIMARY KEY (version, facet, term),
    FOREIGN KEY (version, facet) REFERENCES dna_axis(version, facet) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Tier 1: EXTRACTED. Quote-verified. Never unioned with dna_projected.
-- ---------------------------------------------------------------------------
CREATE TABLE dna_tag (
    id         bigserial PRIMARY KEY,
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    version    text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    term       text NOT NULL,
    facet      text NOT NULL,
    salience   smallint NOT NULL CHECK (salience IN (1, 2, 3)),  -- §8 stage 7 trust boundary
    confidence real,                       -- WEIGHT, never a filter (rule 2)
    n_sources  integer,                    -- WEIGHT, never a filter (rule 2)
    provider   text,                       -- which LLM produced it (§6.6 parallel mode)
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (title_id, version, term, provider)
);
CREATE INDEX dna_tag_title ON dna_tag (title_id);
CREATE INDEX dna_tag_term ON dna_tag (version, term);
CREATE INDEX dna_tag_facet ON dna_tag (version, facet);

-- "a tag without its quote is unfalsifiable" — evidence is NOT NULL-able by design, and the
-- importer rejects an extracted tag arriving without at least one evidence row.
CREATE TABLE dna_evidence (
    id         bigserial PRIMARY KEY,
    dna_tag_id bigint NOT NULL REFERENCES dna_tag(id) ON DELETE CASCADE,
    quote      text NOT NULL,
    source     text NOT NULL,              -- e.g. trakt:comment, metacritic:critic
    source_ref text
);
CREATE INDEX dna_evidence_tag ON dna_evidence (dna_tag_id);

-- ---------------------------------------------------------------------------
-- Tier 2: PROJECTED (inferred from keywords via the alias map). Separate table, always.
-- ---------------------------------------------------------------------------
CREATE TABLE dna_projected (
    id         bigserial PRIMARY KEY,
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    version    text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    term       text NOT NULL,
    facet      text NOT NULL,
    weight     real,                       -- WEIGHT, never a filter (rule 2)
    via        text,                       -- the keyword/alias that produced it
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (title_id, version, term)
);
CREATE INDEX dna_projected_title ON dna_projected (title_id);
CREATE INDEX dna_projected_term ON dna_projected (version, term);

-- The two tiers are queried through this view, which exists *only* so that the `tier`
-- discriminator can never be forgotten. It is a UNION ALL of two labelled selects — the
-- rows stay distinguishable, which is exactly what rule 1 requires. Nothing may select
-- from it without carrying `tier` through.
CREATE VIEW dna_tagged AS
    SELECT title_id, version, term, facet, 'extracted'::text AS tier,
           salience::real AS salience, confidence, provider
      FROM dna_tag
    UNION ALL
    SELECT title_id, version, term, facet, 'projected'::text AS tier,
           NULL::real AS salience, weight AS confidence, NULL::text AS provider
      FROM dna_projected;

-- ---------------------------------------------------------------------------
-- Curated ledgers that travel with the bundle and are re-applied at every derive (§8 stage 3).
-- "a derive that regenerates rows without re-applying them silently reverts curated fixes"
-- ---------------------------------------------------------------------------
CREATE TABLE dna_adjudication (
    version   text NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    term      text NOT NULL,
    verdict   text NOT NULL,               -- keep | rename | drop | merge
    target    text,
    note      text,
    decided_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version, term)
);

CREATE TABLE credit_correction (
    id         bigserial PRIMARY KEY,
    title_id   integer,
    person_name text,
    field      text NOT NULL,
    old_value  text,
    new_value  text,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- §6.4/§8.4: the extraction flywheel — naming failures that future LLM spend should fix.
CREATE TABLE flywheel_item (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL
                CHECK (kind IN ('empty_predicate', 'uncovered_frontier',
                                'unnamed_residual', 'thin_facet')),
    detail      jsonb NOT NULL,
    reason      text NOT NULL,             -- shown verbatim in the admin queue
    est_titles  integer,
    est_cost_usd numeric(10, 4),
    status      text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'approved', 'running', 'done', 'dismissed')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX flywheel_status ON flywheel_item (status, created_at DESC);
