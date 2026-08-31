"""The Rank surface — §6.3's tier board, its filters, and its comparison queue.

`board` is the board and its three badges (tier + neighbourhood, straddle, tension), `queue`
the boundary-targeted selector and §13 stream (a)'s held-out arm, `tiers` the per-user tier set
(decision 11), `drop` the two write paths drag-and-drop has, `read` the database side of the
board, and `evaluation` the one read path §13 admits for judging the tier model.

The split follows the same rule the ledger package does: `board` and `queue` are pure — no
database, no clock — because everything §6.3 says about them is a statement about a function of
the fit's output, and a rule you can only observe through Postgres is a rule you are observing
something else through.
"""
