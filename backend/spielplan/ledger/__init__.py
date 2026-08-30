"""The Personal Ledger — §5.2's one write-path for taste.

`hyperparams` holds the constants (all from the bundle), `model` the maths (numpy only, no
database and no clock), `observations` the SQL that decides what the fit sees, and `refit` the
jobs §5.3 schedules. The split is deliberate: §5.3's budgets are properties of `model`, so they
have to be measurable without a database in the way.
"""
