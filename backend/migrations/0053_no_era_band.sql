-- 0053_no_era_band — the era band is removed from §5.1's content head (decision 571 retires 569).
--
-- 0052 shipped two hinges against a band of years measured from the member's own good placements,
-- to stop a shelf drifting into old cinema. Measured on this household after it was deployed, it
-- did the opposite: Patrick's top forty owned films averaged 1998 with the block and 2001 with no
-- year feature at all, at equal held-out agreement (0.918 against 0.929). The fitted hinge never
-- learned "older is worse" either — with the collinear decade columns removed its `older`
-- coefficient was +0.001, and the signal it did find was "newer than my band is worse", which is a
-- true pattern for a member who placed a run of 2026 films at C and D, and no answer to the drift.
--
-- The decade one-hots stay: they cost nothing, read the same for every member, and carry whatever
-- the year is worth. Three columns a nothing writes would claim a measure the model does not have.
ALTER TABLE user_content_fit
    DROP COLUMN era_centre,
    DROP COLUMN era_spread,
    DROP COLUMN era_used;
