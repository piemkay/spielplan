-- 0015_seed — the corpus becomes a one-time seed and a models-only upstream.
-- Spec v2.1 §4.1, §4.3, §10; decisions 162 and 163 (docs/spec-v2.2-proposals.md).
--
-- Decision 162: "the corpus supplies trained models; movie data is exported once and imported
-- once; every later title is acquired by Spielplan itself; Spielplan owns all ids."
--
-- Everything here is a new file. No applied migration is edited (they are sha256-checksummed
-- and a mismatch is a hard startup error).

-- ---------------------------------------------------------------------------
-- 1. The id partition. Decision 162's load-bearing constant.
-- ---------------------------------------------------------------------------
-- The corpus mints `title.id` with AUTOINCREMENT and its sqlite_sequence currently reads
-- 21442, so "mint above the imported maximum" would start this app at exactly the id the
-- corpus mints next. Two minters, one namespace, and the model bundle still carries content
-- ids in backbone.npz and review_text_emb.npz — so a single newly crawled film would make a
-- later bundle assert things about a title this household acquired. Nothing downstream could
-- tell: backbone.py checks only that the ids ascend.
--
-- A disjoint range makes the collision arithmetically impossible instead of contingent on the
-- corpus standing still. 1e9 leaves the corpus a billion ids and this app 1.1 billion, and the
-- ceiling is not arbitrary: `title.id` is `integer`, `ledger_fit.title_ids` is a numpy int32
-- blob, and thirty-odd queries cast `$1::int[]`. Anything at or above 2^31 breaks all of them.
CREATE SEQUENCE title_id_seq  AS integer MINVALUE 1000000000 START 1000000000 NO CYCLE;
CREATE SEQUENCE person_id_seq AS integer MINVALUE 1000000000 START 1000000000 NO CYCLE;

ALTER TABLE title  ALTER COLUMN id SET DEFAULT nextval('title_id_seq');
ALTER TABLE person ALTER COLUMN id SET DEFAULT nextval('person_id_seq');

-- The sequences are OWNED BY the columns so a DROP TABLE takes them with it, but they are
-- deliberately NOT positioned here: a fresh install has an empty `title`, so `setval(max(id))`
-- would yield 1 and the first acquired title would collide with corpus title 1. Positioning is
-- the seed importer's job, and the MINVALUE above is what makes a missed positioning loud: a
-- `setval` below 1e9 is rejected by the sequence itself rather than quietly minting into the
-- corpus's half of the namespace.
ALTER SEQUENCE title_id_seq  OWNED BY title.id;
ALTER SEQUENCE person_id_seq OWNED BY person.id;

-- The partition, as a constraint rather than a convention. A bundle reaching into the app's
-- range is refused by the importer with the offending id named (§10's validation report); this
-- is the backstop for every other write path, including §8 stage 1.
COMMENT ON SEQUENCE title_id_seq IS
    'decision 162: app-minted title ids start at 1e9; corpus ids stay below it. Positioned by '
    'the seed import, never by this migration.';

-- ---------------------------------------------------------------------------
-- 2. `credit` — the columns the corpus actually exports.
-- ---------------------------------------------------------------------------
-- `role_class` is the corpus's own normalisation (director|writer|dp|composer|editor|
-- prod_designer|cast) and it is what the feature contract's `p:<role_class>:<name>` grammar is
-- built from. Without it the credit block has to re-derive a role from free-text `job`, which
-- drifts from the vocabulary the tower was trained against one job title at a time.
ALTER TABLE credit RENAME COLUMN ord TO billing_order;
ALTER TABLE credit ADD COLUMN role_class text;
CREATE INDEX credit_role_class ON credit (role_class) WHERE role_class IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. The seed-list collision.
-- ---------------------------------------------------------------------------
-- §4.3's `seed_list.json` is the 100-title decade-stratified ONBOARDING list. §10's manifest
-- also ships a `seed_list` TABLE, which is the corpus's 238-row list REGISTRY (id, slug, name,
-- source, kind, category, weight, …) — a different thing wearing the same name. The importer
-- mapped one onto the other, which COPYs 238 all-NULL rows into a NOT NULL primary key.
--
-- The onboarding list keeps the name the app already reads (`rate/queue.py`); the registry gets
-- its own table and its own membership rows, so both survive and neither is silently dropped.
CREATE TABLE title_list (
    id          integer PRIMARY KEY,        -- the corpus's own list id, carried over
    slug        text NOT NULL,
    name        text NOT NULL DEFAULT '',
    source      text NOT NULL DEFAULT '',
    kind        text,
    category    text,
    weight      real,
    item_count  integer,
    notes       text
);

CREATE TABLE title_list_membership (
    list_id  integer NOT NULL REFERENCES title_list(id) ON DELETE CASCADE,
    title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    rank     integer,
    PRIMARY KEY (list_id, title_id)
);
CREATE INDEX title_list_membership_title ON title_list_membership (title_id);

-- ---------------------------------------------------------------------------
-- 4. A feature block that never hits is not "present".
-- ---------------------------------------------------------------------------
-- `features.py` counted the keys a block produced that the contract does not declare, and
-- `reconcile.py`'s upsert threw the count away. So a block whose keys ALL miss — which is what
-- every one of the nine did against the real contract — was indistinguishable from a block
-- that filled every column it had. Persisting the counts is what makes §5.3's badge and §8.4's
-- flywheel able to tell "thin because the title has no data" from "empty because the builder
-- and the contract disagree about the key".
ALTER TABLE title_placement
    ADD COLUMN blocks_unmapped jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN blocks_empty    text[] NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- 5. Seed versus model bundles. Decision 162.
-- ---------------------------------------------------------------------------
-- Content arrives once; models re-import. The board has to be able to say which kind of import
-- produced a version, and the importer has to be able to refuse a second content seed.
ALTER TABLE artifact_bundle ADD COLUMN kind text NOT NULL DEFAULT 'seed'
    CHECK (kind IN ('seed', 'model'));

-- Backfill BEFORE the unique index, or this migration cannot apply to any install that has
-- already run §10's re-import: `ADD COLUMN ... DEFAULT 'seed'` stamps every existing row, and
-- an install with two bundle rows then fails the index with a duplicate key — at boot, inside
-- `db/migrate.py`, with no way forward because 0015 is checksummed the moment it lands.
--
-- The oldest bundle is the seed by construction: it is the one that brought content into an
-- empty install. Every later row was a re-import, which under decision 162 carries models.
UPDATE artifact_bundle SET kind = 'model'
 WHERE version <> (SELECT version FROM artifact_bundle ORDER BY imported_at, version LIMIT 1);

-- Exactly one content seed, ever. A partial unique index rather than application logic: the
-- rule is about the whole table's history, and "already seeded" must survive a restart, a
-- concurrent import and a developer with psql.
CREATE UNIQUE INDEX artifact_bundle_one_seed ON artifact_bundle ((kind)) WHERE kind = 'seed';

-- Decision 163: a vocabulary change is a migration, not an import. Recording the vocabulary a
-- bundle carries is what lets the importer refuse a change instead of activating it and leaving
-- both DNA tiers stranded at the old version — empty is not an error anywhere in the read path.
ALTER TABLE artifact_bundle ADD COLUMN vocabulary_version text;

-- ---------------------------------------------------------------------------
-- 6. A fold-in vector is not owned by the bundle it was computed against.
-- ---------------------------------------------------------------------------
-- `user_vector.bundle_version` is nullable and was ON DELETE CASCADE, so deleting an
-- artifact_bundle row deleted the whole user_vector row — the 64-d fold-in, blend_beta,
-- label_count, mu, all of it — where the sibling `ledger_fit.bundle_version` correctly uses
-- SET NULL. Nothing deletes bundle rows today, which is why it has never fired; this is the
-- migration that makes them prunable, so it is the migration that owes the fix.
--
-- SET NULL is right rather than merely safer: §10 says a vector expressed in the old basis is
-- garbage, and a NULL stamp is exactly how every read already recognises that (`foldin.py`
-- treats a differing version as stale). Deleting the row instead throws away `label_count`,
-- which is vocabulary-independent and expensive to recover.
ALTER TABLE user_vector DROP CONSTRAINT user_vector_bundle_version_fkey;
ALTER TABLE user_vector ADD CONSTRAINT user_vector_bundle_version_fkey
    FOREIGN KEY (bundle_version) REFERENCES artifact_bundle(version) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- 7. The adjudication ledger is keyed per title.
-- ---------------------------------------------------------------------------
-- `adjudications_v1.tsv` ships (scope, title_id, term, action, target, quote, source, note) and
-- most of its verdicts are scoped to a single title. `dna_adjudication`'s PRIMARY KEY
-- (version, term) cannot hold them: the importer's ON CONFLICT (version, term) DO UPDATE keeps
-- the last verdict for a term and discards every other title's — silently, with no count, and
-- in the direction that loses data.
--
-- The ledger's identity is the row. It is an authored list re-applied at every derive (§8
-- stage 3), and §6.6's editor writes the same TSV back, so every shipped column has to survive
-- the round trip and no unique constraint may invent a key the file does not have. `title_id`
-- carries no FK for the same reason `credit_correction.title_id` does not: a curated verdict is
-- authored upstream against a corpus id and must outlive a title this install has not acquired.
ALTER TABLE dna_adjudication DROP CONSTRAINT dna_adjudication_pkey;
ALTER TABLE dna_adjudication ADD COLUMN id bigserial PRIMARY KEY;
ALTER TABLE dna_adjudication ADD COLUMN scope text NOT NULL DEFAULT 'global';
ALTER TABLE dna_adjudication ADD COLUMN title_id integer;
ALTER TABLE dna_adjudication ADD COLUMN quote text;
ALTER TABLE dna_adjudication ADD COLUMN source text;
CREATE INDEX dna_adjudication_term ON dna_adjudication (version, term);
CREATE INDEX dna_adjudication_title ON dna_adjudication (title_id) WHERE title_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 8. A credit correction keeps the link that makes it checkable.
-- ---------------------------------------------------------------------------
-- `corrections_v1.tsv` is (kind, title_id, value, evidence, note). Four of those map onto the
-- columns already here — kind is the credit field, value the asserted truth — and `evidence`
-- has nowhere to go. §6.6 has the app writing this same file back, so dropping the column on
-- import would mean exporting a ledger whose claims can no longer be checked; §4.1 rule 1 makes
-- the same argument one table over ("a tag without its quote is unfalsifiable").
ALTER TABLE credit_correction ADD COLUMN evidence text;


-- ---------------------------------------------------------------------------
-- 9. Three tables the app keyed more coarsely than the corpus does.
-- ---------------------------------------------------------------------------
-- Owner decision, 2026-09-02. §4.1 opens "tables mirror the corpus export" and says of
-- `title_meta`: "multi-source, per-source rows kept — one block = one droppable source". That
-- rule was applied to `title_meta` and silently not to its three siblings, which this app keyed
-- without `source`. Measured against the shipped bundle, that is not a style difference:
--
--     title_language    47,302 rows   17,342 duplicate groups under (title_id, language)
--     title_country     50,037 rows   19,092 duplicate groups under (title_id, country)
--     platform_rating  165,678 rows   32,463 duplicate groups under (title_id, source)
--
-- so COPY dies on a unique violation and the seed import rolls back. The milestone's headline —
-- "make the real bundle importable" — is false until these three carry the corpus's own key.
--
-- `role` on title_language survives beside `source`: it is this app's is_primary flag, a
-- different fact from which source said so.
ALTER TABLE title_language DROP CONSTRAINT title_language_pkey;
ALTER TABLE title_language ADD COLUMN source text NOT NULL DEFAULT '';
ALTER TABLE title_language ADD PRIMARY KEY (title_id, source, language, role);

ALTER TABLE title_country DROP CONSTRAINT title_country_pkey;
ALTER TABLE title_country ADD COLUMN source text NOT NULL DEFAULT '';
ALTER TABLE title_country ADD PRIMARY KEY (title_id, source, country);

-- `metric` as well as `source`: the corpus records user_score, critic_score and popularity
-- separately per source, and collapsing them keeps whichever COPY happened to arrive last.
-- Still the display-only schema (§4.1 rule 3) — nothing here becomes a model feature.
ALTER TABLE display.platform_rating DROP CONSTRAINT platform_rating_pkey;
ALTER TABLE display.platform_rating ADD COLUMN metric text NOT NULL DEFAULT 'user_score';
ALTER TABLE display.platform_rating ADD COLUMN scale real;
ALTER TABLE display.platform_rating ADD PRIMARY KEY (title_id, platform, metric);

-- ---------------------------------------------------------------------------
-- 10. The one column the meta block's `lang:` production is built from.
-- ---------------------------------------------------------------------------
-- §4.3's meta block carries one `lang:` column per title, and the corpus builds it from
-- `title.original_language` — a single value — not from `title_language`, which holds every
-- language every source reported. This app had no such column at all, so the meta block set one
-- `lang:` bit per language row and lit several columns where the checkpoint was trained to see
-- one. 13,766 of the shipped bundle's 19,071 titles carry a value.
ALTER TABLE title ADD COLUMN original_language text;
