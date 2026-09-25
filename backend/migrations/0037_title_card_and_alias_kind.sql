-- 0037_title_card_and_alias_kind - the seeded install brought to what the importer now writes.
-- Spec v2.1 §4.1 (title_meta "one block = one droppable source"; credit's closed role_class), §6.0,
-- §6.8, §8 stage 8; decisions 162, 163, 335, 383, 483, 499, 500, 501; owner instruction of
-- 2026-09-25 after the first household user test.
--
-- SQL, AND NOT A CALL TO `resolve_title_fields`: db/migrate.py applies .sql files and nothing else,
-- and a second copy of the per-field walk is what decision 335 and importer/meta.py's docstring
-- forbid. Neither is needed. Each card statement below writes exactly what
-- importer/meta.resolve_title_fields now answers for the rows it touches, and all of them were
-- measured against the seeded install (bundle v20260925) by replaying the new walk over its
-- title_meta: 968 overviews and 157 posters change, and no other card field on any title does. On a
-- fresh install this file runs before any import and touches nothing; the import resolves by the
-- new rule itself.

-- ---------------------------------------------------------------------------
-- 1. The index decision 499's shared-text rule reads through.
-- ---------------------------------------------------------------------------
-- importer/meta.shared_plot_texts asks whether another title carries the same synopsis. A derive
-- asks it about one title, and without an index the answer is a scan of every synopsis in the
-- install (46,000 rows, MPST's up to 45,643 characters). HASH and not btree: a btree entry is capped
-- near 2.7 kB and these texts are not, while a hash index stores a 32-bit hash of any length and
-- equality is the only question asked.
CREATE INDEX title_meta_plot_full_text ON title_meta USING hash (btrim(payload ->> 'plot_full'));
CREATE INDEX title_meta_plot_short_text ON title_meta USING hash (btrim(payload ->> 'plot_short'));

-- ---------------------------------------------------------------------------
-- 2. The overview (decision 499).
-- ---------------------------------------------------------------------------
-- A title that carries plot text and none of it eligible - every text MPST's, or a Wikipedia page
-- another title also carries - is a title the walk now answers NULL for, and that is all this
-- statement writes. On v20260925 that is 965 MPST-only titles (Moulin Rouge 1952 and The Grudge
-- among them) and 3 whose only text was a collided Wikipedia page. A title WITH an eligible text is
-- left to the walk and not touched here; on the seeded install none of them changes, because the
-- old walk reached MPST or a shared page only when no source above it carried a plot. MPST is the
-- one source never eligible, so the shared test below is Wikipedia's.
UPDATE title t
   SET overview = NULL
 WHERE t.overview IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM title_meta m
        WHERE m.title_id = t.id
          AND (coalesce(m.payload ->> 'plot_full', '') <> ''
               OR coalesce(m.payload ->> 'plot_short', '') <> ''))
   AND NOT EXISTS (
       SELECT 1 FROM title_meta m
        WHERE m.title_id = t.id AND m.source <> 'mpst'
          AND ((coalesce(m.payload ->> 'plot_full', '') <> ''
                AND NOT (m.source = 'wikipedia' AND btrim(m.payload ->> 'plot_full') <> ''
                         AND EXISTS (SELECT 1 FROM title_meta o
                                      WHERE btrim(o.payload ->> 'plot_full')
                                            = btrim(m.payload ->> 'plot_full')
                                        AND o.title_id <> m.title_id)))
            OR (coalesce(m.payload ->> 'plot_short', '') <> ''
                AND NOT (m.source = 'wikipedia' AND btrim(m.payload ->> 'plot_short') <> ''
                         AND EXISTS (SELECT 1 FROM title_meta o
                                      WHERE btrim(o.payload ->> 'plot_short')
                                            = btrim(m.payload ->> 'plot_short')
                                        AND o.title_id <> m.title_id)))));

-- ---------------------------------------------------------------------------
-- 3. The art (decision 501, on decision 483's two hosts).
-- ---------------------------------------------------------------------------
-- The seed wrote 157 `https://m.media-amazon.com/` posters, OMDb's, because OMDb ranks above TVmaze
-- and nothing asked which host a URL was on. The walk now skips a host art/hosts.servable refuses,
-- and on v20260925 the only other source carrying a poster for those titles is TVmaze: 8 of them
-- get its `https://static.tvmaze.com/` poster back exactly and 149 have none. Every backdrop on the
-- seeded install is TMDB's, so the second statement changes nothing there and states the rule for
-- an install whose seed differs. The pattern is servable()'s for every value this install holds:
-- https, the exact host, and a path.
UPDATE title t
   SET poster_path = (SELECT m.payload ->> 'poster_url'
                        FROM title_meta m
                       WHERE m.title_id = t.id AND m.source = 'tvmaze'
                         AND m.payload ->> 'poster_url' ~ '^https://static\.tvmaze\.com/')
 WHERE t.poster_path !~ '^https://(image\.tmdb\.org|static\.tvmaze\.com)/';

UPDATE title
   SET backdrop_path = NULL
 WHERE backdrop_path !~ '^https://(image\.tmdb\.org|static\.tvmaze\.com)/';

-- ---------------------------------------------------------------------------
-- 4. Five credits outside the closed class vocabulary.
-- ---------------------------------------------------------------------------
-- 0015_seed.sql closes `credit.role_class` over director|writer|dp|composer|editor|prod_designer|
-- cast, and the corpus's corrections ledger wrote the literal 'crew' onto five composer credits
-- (Alex Heffes on The Regime among them). placement/features.py builds `p:composer:<name>` from the
-- class, so those five scores never reached the feature vector of the three cold titles among them.
-- 'composer' is derive/ids.classify_role's answer for exactly this department and job, and it is
-- the class derive/ledgers.py's NAMED CHANGE 4 already writes for the same correction in-app.
UPDATE credit
   SET role_class = 'composer'
 WHERE role_class = 'crew' AND source = 'correction'
   AND department = 'Sound' AND job = 'Original Music Composer';

-- ---------------------------------------------------------------------------
-- 5. The alias map's `kind` (decision 500).
-- ---------------------------------------------------------------------------
-- Decision 383 added the column and recorded its loader fill as owed; importer/dna._load_aliases
-- now stores it, and this is the install seeded before that line. The pairs are v20260925's own
-- `kind = lexicon` rows, written out because the import deletes its bundle and the alias map is
-- vocabulary tier decision 162 never re-imports; all 84 map onto a register term, which the
-- register proposal keeps to the explicit projection map. The last four are decision 500's:
-- presence keywords the corpus maps onto `characters.teen_protagonist`, a lead-role term ("a
-- teenager is the lead and the film runs on adolescent stakes") that Heat carries off one MovieLens
-- tag for a supporting role. A kind already stored is never overwritten.
UPDATE dna_alias a
   SET kind = 'lexicon'
  FROM (VALUES
    ('80s cheese', 'register.camp'),
    ('abrasive', 'register.provocation'),
    ('audacious', 'register.provocation'),
    ('audacious tone', 'register.provocation'),
    ('b movie', 'register.schlock'),
    ('b movie charm', 'register.schlock'),
    ('b movie energy', 'register.schlock'),
    ('bold tone', 'register.provocation'),
    ('camp', 'register.camp'),
    ('camp sensibility', 'register.camp'),
    ('camp tone', 'register.camp'),
    ('campy', 'register.camp'),
    ('campy charm', 'register.camp'),
    ('campy dialogue', 'register.camp'),
    ('campy fun', 'register.camp'),
    ('campy horror', 'register.camp'),
    ('campy humor', 'register.camp'),
    ('campy melodrama', 'register.camp'),
    ('campy performances', 'register.camp'),
    ('campy tone', 'register.camp'),
    ('campy villainy', 'register.camp'),
    ('campy violence', 'register.camp'),
    ('cheesy', 'register.camp'),
    ('cheesy dialogue', 'register.camp'),
    ('cheesy fun', 'register.camp'),
    ('cheesy humor', 'register.camp'),
    ('cheesy one liners', 'register.camp'),
    ('cheesy tone', 'register.camp'),
    ('confrontational', 'register.provocation'),
    ('confrontational tone', 'register.provocation'),
    ('earnest melodrama', 'register.melodrama'),
    ('edgy', 'register.provocation'),
    ('edgy tone', 'register.provocation'),
    ('fan focused', 'register.fan_service'),
    ('fan service', 'register.fan_service'),
    ('fan service heavy', 'register.fan_service'),
    ('fanservice heavy', 'register.fan_service'),
    ('heavy drama', 'register.melodrama'),
    ('high camp', 'register.camp'),
    ('high drama', 'register.melodrama'),
    ('high melodrama', 'register.melodrama'),
    ('kitsch', 'register.camp'),
    ('kitschy', 'register.camp'),
    ('low budget charm', 'register.schlock'),
    ('lurid', 'register.pulp'),
    ('lurid tone', 'register.pulp'),
    ('melodrama', 'register.melodrama'),
    ('melodramatic', 'register.melodrama'),
    ('melodramatic tone', 'register.melodrama'),
    ('midnight movie energy', 'register.schlock'),
    ('nostalgia bait', 'register.fan_service'),
    ('operatic melodrama', 'register.melodrama'),
    ('overwrought', 'register.melodrama'),
    ('provocative', 'register.provocation'),
    ('provocative tone', 'register.provocation'),
    ('psychotronic', 'register.schlock'),
    ('pulp', 'register.pulp'),
    ('pulp adventure', 'register.pulp'),
    ('pulp sensibility', 'register.pulp'),
    ('pulp thriller', 'register.pulp'),
    ('pulpy', 'register.pulp'),
    ('pulpy adventure', 'register.pulp'),
    ('pulpy tone', 'register.pulp'),
    ('romantic melodrama', 'register.melodrama'),
    ('schlocky', 'register.schlock'),
    ('sensationalist', 'register.pulp'),
    ('shocking', 'register.provocation'),
    ('sleazy', 'register.pulp'),
    ('sleazy atmosphere', 'register.pulp'),
    ('so bad its good', 'register.schlock'),
    ('soap operatic', 'register.melodrama'),
    ('soapy', 'register.melodrama'),
    ('soapy melodrama', 'register.melodrama'),
    ('subversive', 'register.provocation'),
    ('sweeping melodrama', 'register.melodrama'),
    ('taboo breaking', 'register.provocation'),
    ('teen melodrama', 'register.melodrama'),
    ('transgressive', 'register.provocation'),
    ('trash cinema', 'register.schlock'),
    ('trashy', 'register.schlock'),
    ('trashy fun', 'register.schlock'),
    ('unintentional comedy', 'register.schlock'),
    ('unintentionally funny', 'register.schlock'),
    ('unintentionally hilarious', 'register.schlock'),
    ('teenage girl', 'characters.teen_protagonist'),
    ('teenage boy', 'characters.teen_protagonist'),
    ('high school student', 'characters.teen_protagonist'),
    ('teen rebel', 'characters.teen_protagonist')
  ) AS l(alias, term)
 WHERE a.version = 'v1' AND a.alias = l.alias AND a.term = l.term AND a.kind IS NULL;
