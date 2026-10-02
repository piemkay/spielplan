-- 0047_title_wished - a title minted for a wish, not yet in the library (decision 558, §4.2). §8 stage 1
-- turns it 'acquired' when Jellyfin adds the film.
ALTER TABLE title DROP CONSTRAINT title_origin_check;
ALTER TABLE title ADD CONSTRAINT title_origin_check CHECK (origin IN ('bundle', 'acquired', 'wished'));
