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
-- the seed importer's job, and the CHECK below is what makes a missed positioning loud.
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
