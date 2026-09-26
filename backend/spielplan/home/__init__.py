"""§6.0's M2 Home and §6.7's transparency rail.

Five modules, and the split is the design:

* `why.py`     — the vocabulary machinery. Terms are chosen FIRST and membership is derived
                 from them, so §6.0's "a shelf that cannot say why it exists doesn't ship" —
                 and proposal 24's "nor does one that says the wrong why" — hold by
                 construction rather than by review.
* `shelves.py` — the six shelves, the greeting, the pending-verdicts banner, and the payload.
                 A shelf has `sections`, never `items`: §4.1 rule 5 as decision 18 reads it is
                 unrepresentable to violate, not merely discouraged.
* `rail.py`    — §6.7's model log and decision 117's single gate. The gate is a DELETION from
                 the payload, because a number hidden by CSS is still on the wire.
* `taste.py`   — what a member's own verdicts say they do not enjoy (decision 512), which
                 every ranking shelf leaves out.
* `suggest.py` — the title card's one line on why a title is suggested (decision 515).

`spielplan.api.home` exposes all of it over HTTP and exports `router`.
"""

from spielplan.home import rail, shelves, suggest, taste, why

__all__ = ["rail", "shelves", "suggest", "taste", "why"]
