-- 0039_drop_write_only_tables - five content tables nothing reads.
-- The importer reports them as skipped and the movie-data archive names them in RETIRED, so an
-- archive written before this file still restores. The tower's `lang:` reads
-- title.original_language, and the onboarding list is seed_list.

DROP TABLE title_list_membership;
DROP TABLE title_list;
DROP TABLE title_language;
DROP TABLE rating_title_map;
DROP TABLE watchlist;
