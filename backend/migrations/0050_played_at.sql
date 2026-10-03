-- 0050_played_at - a member's last real play of a title (decision 562): Jellyfin's LastPlayedDate
-- unless the app's own Played write or a bulk mark set it, or a finish the member confirmed.
-- NULL: no real play known, which the rewatch rows read as long ago.
ALTER TABLE user_title ADD COLUMN played_at timestamptz;
