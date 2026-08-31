# M4 — open points

Everything M4 left open, in one place. Written at close (branch `m4`, coverage map 42/42, six
test layers green).

The same four kinds of thing M3's collects, and they are **not** interchangeable:

- **§1 Decisions** — questions only the owner can settle. Work is blocked or guessing without them.
- **§2 Spec defects** — places v2.1 is wrong, silent, or self-contradictory. Input to v2.2.
- **§3 Known defects** — real problems, found and deliberately not fixed, with the reason.
- **§4–5 Measurements, debt and waivers** — what the round actually does, and what is weaker
  than it looks.

Everything in §3 came out of M4's adversarial review (six lenses, then skeptics told to refute
each finding). What is listed here **survived** a skeptic; refuted findings are not here.

---

## 1. Decisions needed from the owner

### 1.1 The round is not adaptive in length. Should it be?

**What 54c says.** *"The round ends for a person when the shortlist boundary is resolved …
subject to a hard cap of 20 pairs."* Convergence is written as the normal exit and the cap as
the backstop. §14 risk 6 asks for the rate of each.

**What it does.** Measured over simulated rounds with realistic answer noise, on pools of 8 to
60 candidates: `converged` fires on **2–3% of rounds** at every pool size, and on **none** at
60. The median round is 20 pairs — the cap — at every size.

**Why, structurally.** A candidate nobody has been asked about keeps its prior variance, so it
straddles any boundary within `z·σ` of its mean forever. With `prior_var = 1.0` on the
tonight-score scale and `straddle_z = 1.0`, that is most of a normally-distributed pool. The
round can therefore only converge by asking about nearly every candidate, and 20 pairs touch at
most 40 of them. For any pool larger than about ten, `converged` is not rare — it is
structurally unreachable.

**Why this is an ask and not a fix.** `prior_var` is not in the spec. Lowering it is a claim
about how much a mood moves a title relative to its Ledger score, and it changes how long a
household's evening is — twenty questions each versus eight. Nothing in §6.2 or §4.3 names the
number, and no coverage row constrains the rate, so the implementation satisfies its contract.
Changing it is a product decision.

**The options.**

| | behaviour | cost |
|---|---|---|
| A | leave it (today) | every participant answers 20 pairs; "adaptive length" is the escape only |
| B | narrow the member prior (e.g. `prior_var = BETA²`) | shorter rounds; the Ledger's ranking is trusted more, so a mood swings less |
| C | keep the prior, cap the *pool* the round ranks over | convergence becomes reachable; the shortlist is drawn from fewer candidates |

The escape from pair 6 (54c) means nobody is trapped either way. This is about what the default
evening costs.

### 1.2 What ends a room nobody finished?

0013 admits `session.state = 'abandoned'` and, as shipped, one thing writes it: opening a second
room as the same host abandons the first if nobody started it (§3.1 explains why that moment).
A room that was *started* and then walked away from stays live forever — on §6.2 step 2's
open-rooms list for every device, with an age that only grows.

§6.2 names no control for ending a room and no timeout. The three candidates are a host control
in the lobby, an age-based sweep in the worker, and leaving it. Each is a surface or a constant
the spec does not give, so none was built.

---

## 2. Spec defects (input to v2.2)

### 2.1 §6.2 step 5's centring cancels

Step 5 says the group score is computed on DNA vectors *"centred on the pool mean"*. Tilt is a
**difference** — chosen minus rejected — and an additive centring term cancels in a difference
exactly. So the sentence as written describes a no-op, and cannot be the +0.088 AUC lever §0
row 4 measured. What ships centres and then **standardises** (divides by the pool's own spread),
which does not cancel and is the only reading under which the measured lever exists.

### 2.2 §6.2 never describes a pool that runs out of pairs

`select` refuses to re-serve a pair the participant has already answered (M3-open-points §3.1's
reliability inflation, arriving by a different door). A household library small enough to
exhaust its distinct pairs therefore reaches a state 54c does not name: nothing left to ask and
no reason to stop. It is recorded as `cap` — the same terminal state, the round ended without
resolving the boundary — rather than as a fourth `ended_by` value nobody defined. v2.2 should
either name it or say the cap covers it.

### 2.3 §6.2 step 8 forbids the table its own risk register requires

Step 8: solo *"mints no session row"*. §14 risk 6: *"log every vote"*. Solo's sharpen round
produces votes and has nowhere to put them, so they are carried by the client for the length of
the screen and lost. Either solo's votes are outside risk 6 or step 8 needs a row that is not a
room.

### 2.4 `DNA_MODEL.md` is not vendored

§6.2 step 5's divergence `D` is defined by reference to a document that is not in the repository
and not quoted in the spec. What ships is `mean − min` across seats, chosen because it is the
only reading that makes step 5's own threshold sentence (*"above 0.20, surface the split"*)
behave the way the surrounding copy describes. The formula cannot be checked against its source.

---

## 3. Known defects, deliberately not fixed

### 3.1 A started room is never abandoned

See §1.2. The writer that exists covers the un-started case only, because the second tap is the
one moment a host's intent is unambiguous and needs no new control to read. A started room holds
its seats, so every participant's device restores into it until it resolves.

### 3.2 The evaluation's agreement figure is thin by construction

§13's hold-out is one pair in ten and a round is at most twenty pairs, so a two-person evening
contributes **at most four** held-out answers. `evaluation.report` carries its own denominator
for exactly this reason and returns `rate = None` on an empty sample, but a household reading
the number after one evening is reading noise. It becomes a measurement over a season, not an
evening, and nothing in the surface says so.

### 3.3 The round's constants are unmeasured

`BETA = 0.5`, `prior_var = 1.0`, `GUEST_VAR_FACTOR = 4.0`. §14 risk 7 says the round is
owner-designed rather than corpus-measured and must be instrumented before anyone tunes it —
which M4 does (§13's stream, `ended_by` rates, shortlist agreement). These three numbers are the
first things that instrumentation should be pointed at, and none of them is defensible from the
spec today.

---

## 4. What was measured

Simulated rounds, real `replay`, realistic answer noise (σ = 0.4 on the tonight-score scale):

| pool | `converged` | `cap` | median pairs |
|---|---|---|---|
| 8 | 1/60 | 59/60 | 20 |
| 15 | 2/60 | 58/60 | 20 |
| 30 | 2/60 | 58/60 | 20 |
| 60 | 0/60 | 60/60 | 20 |

Before the selection fallback fix (`7c47213`), `cap` also fired at pair 10–14 on most seeds,
which made this table unreadable: the rate §14 risk 6 asks for was reporting the cap for rounds
that had six pairs of budget left and two candidates still unplaced.

The fixture library cannot show any of this. `ops/m4_exit_criterion.py` runs over a pool of
**six** candidates — the eight-title fixture is six films and two series, and the harness opens a
`movie` room, so §4.1 rule 5's kind partition drops the two series — and both seats
answer all fifteen distinct pairs plus one hold-out, sixteen in total, and the round ends by
exhaustion (§2.2), recorded as `cap`. Convergence has no room to fire and the twenty-pair cap is
never reached, so neither number in §1.1 is observable against the fixture. It is a claim about
a real library, and it needs one.

The exit-criterion numbers — wall clock, per-answer latency, approval share, agreement and its
n — are in the milestone report rather than here, because they are one evening's reading and
this file is the standing list.

---

## 5. Debt and waivers

**M4 adds no waiver.** The two standing M0 waivers (backup rotation, the no-bundle model line on
the title card) were re-read at open and both still hold, checked rather than assumed: no backup
job exists anywhere under `ops/`, and a locally acquired title still cannot outlive a deactivated
bundle before M5.

**Tests that were weaker than they looked**, found by the review and rewritten rather than
extended — recorded here because the pattern is what to look for next time:

- an assertion whose whole body sat under `if x is None`, so an implementation that always set
  `x` took the other branch and passed;
- a uniqueness test that compared two values drawn from a space thousands wide, which is true of
  an implementation with no uniqueness rule at all;
- a selection test that compared the served pair against alternatives **using the function the
  selector minimises**, which can only catch a selector that does not call it;
- a blindness test that asserted the second device's screen did not *contain* an answer, when no
  template draws one.

Each is now asserted against something the implementation does not itself provide: a database
constraint, an independently computed entropy, an HTTP refusal.

A second audit ran at merge time, over the whole branch diff, with a skeptic on every finding.
Ten survived; six were fixed, and the four below are what it leaves behind. The pattern it added
to the list above is a fifth: **a test that scores the implementation with the same function the
implementation optimises**. `test_the_pair_served_is_the_one_that_resolves_the_most_straddlers`
ranks the served pair by `expected_straddlers` — the function `select` minimises — so it can
only catch a selector that fails to call it, never one that calls it with a wrong weighting.
`test_a_surfaced_split_reserves_the_third_slot_for_the_opposite_pole` did the same in miniature:
`opposite[-1]` for `opposite[0]` left all twenty-nine combine tests green. Both now have a
companion asserting the thing from outside — an entropy computed in the test, and a board where
the reservation has to choose.

---

## 6. Left for later, from the merge-time audit

### 6.1 The hold-out pair is redrawn on every read of the round

`select` branches to `_holdout` before the `asked` set is built, `_holdout` draws with the
request's own rng, and `record_answer` validates only that the seq is next. So a reload at pair
10 or 20 serves a different uniform-random pair, and a client can reload until it likes the one
it is asked — which is the selection bias §13's arm exists to exclude, arriving through the one
door that arm cannot close.

Not fixed because the harm is bounded to instrumentation: hold-out answers are excluded from the
tilt, from `replay` and from the posterior, and the shortlist does not exist until every seat
has finished, so nothing a re-roll touches can move the evening. What it can move is
`evaluation.shortlist_agreement`, over a sample §3.2 already calls too thin to read. The fix is
to seal the hold-out draw against the seq the way the pair token is sealed, which is a change to
what the round persists rather than a line.

### 6.2 Two coverage rows describe things that do not exist

Both need the owner, because the rule is that a row's `spec` and `what` are never edited to
match what was built.

* `tonight-rank-join-channels-equivalent` lists "joining by room code, from the open-rooms list,
  or **by following a join link**". No join link exists: the Tonight surface reads no query
  parameter, and the invitation payload carries no `url`. §6.2 step 2 does not name one either —
  it enumerates push, the room code and QR, the in-app banner, the open-rooms list and the TV
  route. So the row claims coverage of a channel neither the spec nor the code has. Nobody is
  stranded (the notification body carries the code), but the row is wrong about the world.
* `tonight-rank-conflict-copy-bounded` clause 3 says a participant with no term reaching either
  sign "gets the stated no-pull line". The code gives that participant a **fourth** line —
  "nothing here reads either way for {name} yet" — and reserves `NO_PULL_LINE` for a different
  case. One of the two is wrong and it is not mine to choose. Both branches also have no test
  reader at all: mutating the sign comparison in `play.py` leaves 109 tests green.

### 6.3 What neither audit read

Recorded so the next milestone knows where the thin ice is, not as a defect:

* **The two migrations were never reviewed as DDL.** Both audits worked from Python and docs.
  They are covered by tests — `test_migrations.py`, `test_schema_contracts.py` and the PGlite
  layer assert the constraints and the checksums — but nobody read the 201 lines of
  `0013_tonight.sql` asking what a good schema would have done differently. They are
  sha256-checksummed the moment they land, so anything wrong in them needs a follow-up migration
  forever.
* **The push sender's cryptography** was flagged as unread by the merge-time audit, which did
  not know it had already been through two lenses at build time — `_encrypt` was verified against
  RFC 8291 Appendix A's published test vector, byte-identical, and the four findings that pass
  produced are decisions 19–23 in `M4-plan.md`. Not a gap.
