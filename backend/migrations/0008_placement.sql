-- 0008_placement — where a title's coordinate lives. Spec v2.1 §4.3, §5.3, §8 stages 9–10, §10.
--
-- §12's M2 exit criterion is "every owned title has a coordinate (warm Backbone row or Cold
-- Tower placement)". Half of that already exists: a warm title's coordinate IS the row
-- `backbone.npz` ships, and copying those rows into Postgres would make a bundle re-import
-- recompute five things where §10 says four. So warm titles get `title.placement = 'warm'`
-- and no row here; this table holds only what the app itself computed.

CREATE TABLE title_placement (
    title_id        integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    -- §10: "everything expressed in the old Backbone's basis is garbage against a new one."
    -- Stamping the version is what lets a re-import rebuild rather than reinterpret, and what
    -- lets the previous bundle's rows survive a rollback.
    bundle_version  text NOT NULL REFERENCES artifact_bundle(version) ON DELETE CASCADE,
    e_hat           bytea NOT NULL,             -- 64 × float32 LE, the same convention as user_vector.vec
    b_hat           double precision NOT NULL,  -- the Cold Tower's item prior, §5.1's b̂(t)
    dim             smallint NOT NULL DEFAULT 64 CHECK (dim = 64),

    -- Provenance. §4.3: "§8 stage 9 builds vectors from this file and nothing else" — so the
    -- file's identity is part of the result. A placement whose contract hash differs from the
    -- active contract's is stale by construction rather than by guess.
    contract_sha256 text NOT NULL,
    tower_sha256    text NOT NULL,
    input_dim       integer NOT NULL,

    -- §5.3: "thin ones (2 lack keywords, 3 lack any DNA row) are still placed, badged, and
    -- parked as acquisition jobs for M5 enrichment." These arrays are the badge's evidence.
    -- `dropped` and `imputed` are both zeros in the vector and differ only in bookkeeping —
    -- but that difference is the whole of §4.3's "genome zero-imputed; absent blocks dropped
    -- rather than defaulted", so it is recorded rather than re-derived.
    blocks_present  text[] NOT NULL,
    blocks_dropped  text[] NOT NULL,
    blocks_imputed  text[] NOT NULL,
    nnz             integer NOT NULL,           -- the cheap "did we build an empty vector" check
    build_ms        integer,                    -- §5.3's budgets, observable in production
    place_ms        integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (title_id, bundle_version),
    CONSTRAINT title_placement_vector_width CHECK (octet_length(e_hat) = dim * 4)
);
CREATE INDEX title_placement_bundle ON title_placement (bundle_version);

-- §10's rebuild set names "Cold Tower re-placement of every app-acquired title". Nothing
-- distinguished a title the bundle brought from one §7.2 acquired, so that set could not be
-- scoped. The importer stamps 'bundle'; §8 stage 1 will stamp 'acquired' at M5.
ALTER TABLE title ADD COLUMN origin text NOT NULL DEFAULT 'bundle'
    CHECK (origin IN ('bundle', 'acquired'));
CREATE INDEX title_acquired ON title (id) WHERE origin = 'acquired';

-- `title.placement` (unplaced|cold_tower|warm) already exists and stays the denormalised state
-- §8 stage 10's badge reads. What it lacked was the basis it was computed in: without this a
-- post-import 'warm' is indistinguishable from a pre-import one.
ALTER TABLE title ADD COLUMN placement_bundle text
    REFERENCES artifact_bundle(version) ON DELETE SET NULL;

-- §12's M2 exit criterion, as one index-only query:
--   SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'   -- must be 0
CREATE INDEX title_unplaced_owned ON title (id) WHERE is_owned AND placement = 'unplaced';
