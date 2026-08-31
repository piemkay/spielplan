"""The Tonight surface — §6.2's session, its round, its combine and its blind ballot.

Spec v2.1 §6.2 as rewritten by the owner on 2026-08-29 (v2.2 §54a-54g) and decision 154. The
rewrite is an owner decision, not a proposal: it sits in the same table as decisions 11, 18, 35
and 117, all four of which this codebase already ships, and the coverage map's M4 rows are
written against it. Where this package says "§6.2 step N", N is the rewritten numbering — the
ballot is step 6, the result card step 7, solo step 8.

The split follows the rule the `rank` and `ledger` packages already use: a module is pure
unless it has to touch Postgres, because a rule you can only observe through a socket is a rule
you are observing something else through.

    pool      §6.2 step 3's candidate pool — the filters, and the plain average that orders it
    round     step 4's adaptive round — the per-candidate posterior, the selection rule, the
              stopping rule, and §13's held-out arm
    tilt      the chosen-minus-rejected DNA observation, centred on the candidate-pool mean
    combine   step 5 — three finalists, the wildcard, and the split's zeroed axis
    copy      the strings: the conflict phrasing's hard bound, the match lines, the fit lines
    ballot    step 6's blind approval ballot and the approval share §13 evaluates on
    solo      step 8 — three picks and a wildcard, no session row
    rooms     the session lifecycle: room codes, seats, the open-rooms list
    channel   the WebSocket every device watches the lobby and the round through

THE GUARD THIS PACKAGE INHERITS. §13: "the 10% uniform-random comparison stream is the *only*
data used to evaluate the tier model — adaptively-selected pairs inflate reliability (measured
effect; the guard is non-negotiable)." 54b binds it to Tonight, and an adaptive round with a
data-dependent stopping rule is the textbook case it exists for: a round that stops when it is
confident looks confident whether or not it is right. So `round` carries the same two rules
`rank/queue.py` carries — the held-out arm never receives a fallback, and an arm is reported as
the arm that drew it — plus a third that is specific to a *stopping* rule: held-out answers
never reach the posterior that selection and stopping read.
"""
