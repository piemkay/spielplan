-- 0034_art_lookup - what TMDB answered when asked for the poster of a title that had none.
-- Spec v2.1 §6.8 ("Poster-forward 2:3 cards"), §1 (the worker's acquisition), §8's politeness
-- clause; decisions 483 and 484; owner instruction of 2026-09-25 after the first household test.
--
-- NUMBERING. 0034 is this workstream's allocation in the user-test wave; 0030 is the vocabulary
-- labels', and 0031-0033 are other streams' of the same wave. `db/migrate.py` keys
-- `schema_migration` on the filename stem and sorts a glob, so a gap costs nothing.
--
-- WHY A TABLE AND NOT `title.poster_path`. 9,194 of the install's 19,085 titles have no poster the
-- art route may serve (9,045 NULL, 149 IMDb-hosted only), 2,064 of them warm and served by Rate,
-- and every one carries a `tmdb_id` or an `imdb_id` TMDB can answer by. Decision 372 keeps every
-- card field `poster_path` included as §8 stage 3's to write from the raw store, and decision 162
-- seeds content once, so the answer to "what is this title's poster" is kept here, beside the
-- title and never on it: `title`, `title_meta` and the identity columns are untouched, the has:poster
-- feature `placement/features.py` derives from `poster_path` is untouched, and dropping this table
-- costs one TMDB call per posterless title on its next view.
--
-- ONE ROW PER TITLE, AND IT IS BOTH THE QUEUE AND THE ANSWER. The art route files a row when it is
-- asked for a title with no servable poster (`outcome` NULL: owed), and the worker's `art-lookup`
-- job - never the web process, because §1 puts acquisition in the worker and decision 340 gives
-- `api.themoviedb.org` one bucket, the worker's - asks TMDB and records what it said: `found` with
-- the w342 URL, `none` (TMDB holds no poster; asked again after 30 days), or `failed` (the host did
-- not answer; asked again after a day). The job also files the placed titles nobody has viewed yet,
-- warm first, so Rate's cards have their art before a member meets them.
--
-- The URL CHECK is the allow-list's TMDB half written into the schema: a lookup answers with TMDB's
-- own image host and nothing else, so no row here can name a host `art/hosts.py` would refuse.

CREATE TABLE art_lookup (
    title_id     integer PRIMARY KEY REFERENCES title(id) ON DELETE CASCADE,
    requested_at timestamptz NOT NULL DEFAULT now(),
    looked_up_at timestamptz,
    outcome      text CHECK (outcome IN ('found', 'none', 'failed')),
    poster_url   text CHECK (poster_url LIKE 'https://image.tmdb.org/t/p/%'),
    note         text,
    CHECK ((outcome IS NULL) = (looked_up_at IS NULL)),
    CHECK (poster_url IS NULL OR outcome = 'found'),
    CHECK (outcome IS DISTINCT FROM 'found' OR poster_url IS NOT NULL)
);

-- The job's read: what is owed, oldest first. Partial, because a `found` row is never asked again
-- and is most of the table once the backlog has drained.
CREATE INDEX art_lookup_owed ON art_lookup (requested_at) WHERE outcome IS DISTINCT FROM 'found';
