# Release record

**Status: the measured half of this project's own claims (decision 296).** `docs/TESTING.md` says
how to run the suite and what it covers. This file says something the suite cannot: what has
actually been run against a real bundle, a real household or a real stack; what that run produced
and where the output lives; and what nobody has ever measured at all.

It exists because "the suite is green" had been standing in for §12's exit criteria, and those are
different statements. A green suite answers *is the map covered*. §12 asks *does this build ship*.
Counted on this branch rather than inherited. §12 carries **sixteen** rows. **Six of them have
never been run at all**, and the six are not one kind: M0, M2, M4.6, M4.7 and M4.10 have no script
under `ops/` at all and never have, while M5.1's instrument was written in the same change set as
the surface it measures and has still never been run, because the lane that built both had neither
a container nor a port. Three describe milestones that do not exist yet (M5, M6, M7). The remaining
seven have been measured by one of the nine scripts under `ops/`, and **not one of those nine had a
committed output** until M4.16 wrote the first. M4.5's own eighteen checks
meanwhile contained one whose predicate was the literal `True`. So the second question had no evidence behind it, only a habit of assuming
the first implied it.

**Nothing here is signed.** Every `Owner verdict` is the owner's, and this milestone fills none of
them. What it fills is the column to the left of it: the fact of a run, or the fact of its absence.

## How to read this file, and the contract a guard holds it to

Three field shapes recur, and they are meant to be machine-readable as well as readable:

- **`Output file:`** either a path that exists in this tree, or `none`.
- **`Status:`** one of `MEASURED` (a run happened and its output is committed here), `RUN, OUTPUT
  NOT COMMITTED` (a script exists and has been run, but no record of the run is in the tree),
  `UNMEASURED` (never run), or `NOT BUILT`.
- **`Owner verdict:`** either the owner's word, or `________` — unfilled.

The rule, which `platform-exit-criteria-are-closed-by-a-committed-measurement` states and a static
guard should enforce: **every §12 row either names an output file that exists in the tree, or is
recorded as `UNMEASURED` / `NOT BUILT` / `RUN, OUTPUT NOT COMMITTED` with an unfilled verdict.**

M2 is the row that rule was written for (decision 297). Its criterion is §12's own gate — "the
first real-user validation of the whole corpus project" — and it has never been measured. Its
verdict may therefore be filled only by deleting the `UNMEASURED` marker, which takes a run. A
guard asserting M2's verdict is unfilled goes red on the day the owner honestly signs it, and the
repair that day is to delete the guard together with its coverage-row entry, in the same change.
That is `test_the_owed_device_checks_are_recorded_and_still_unsigned`'s polarity, deliberately:
a pre-signed line is worse than a gap, because it tells the next reader to stop looking.

---

## 1. §12's build order, measured

One row per §12 milestone, plus M4.5 — which has no §12 row (it is named nowhere in the table) and
is here because it is the one criterion this milestone actually ran.

| Milestone | Status | Output file | Blocking | Owner verdict |
|---|---|---|---|---|
| M0 | UNMEASURED | none | yes | `________` |
| M1 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M2 | UNMEASURED | none | owner's call | `________` |
| M3 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M4 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M4.5 | MEASURED | `docs/milestones/M4.5-exit.txt` | yes | `________` |
| M4.6 | UNMEASURED | none | yes | `________` |
| M4.7 | UNMEASURED | none | yes | `________` |
| M4.9 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M4.10 | UNMEASURED | none | yes | `________` |
| M4.11 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M4.12 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M4.14 | RUN, OUTPUT NOT COMMITTED | none | yes | `________` |
| M5.1 | UNMEASURED | none | yes | `________` |
| M5 | NOT BUILT | none | no | `________` |
| M6 | NOT BUILT | none | no | `________` |
| M7 | NOT BUILT | none | no | `________` |

The criterion each row is measured against is quoted below, because §12's cells run to several
hundred characters and a table cell cannot wrap inside 108 columns.

### M0 — compose skeleton, schema, wizard, auth, importer, artifacts, Library

**Criterion (§12, verbatim):** "bundle imports clean; Library list and title card render imported
titles"

**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes. **Owner verdict:** `________`

Closed against the fixture, in which none of the corpus's awkward shapes occur — §12's own M4.9
and M4.14 paragraphs say so in as many words. Both halves have since been measured, but by other
milestones and against their own criteria, not against this row: `ops/m45_exit_criterion.py` for
"imports clean" and `ops/m49_exit_criterion.py` for "the title card renders". There is no M0 exit
script and no run record. What the row needs is not new work but an owner's reading of whether
M4.5's and M4.9's outputs discharge it.

### M1 — Jellyfin connector, user linking, two-way seen sync, passkeys

**Criterion (§12, verbatim):** "seen states flow both ways for both users"

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

Closed against one member, one copy per title, no series and no second phone — §12's M4.11
paragraph lists what that missed. `ops/m411_exit_criterion.py` re-measures it against a two-member
household and a fake Jellyfin, and has been run; no output is committed. Note that the script's own
docstring opens "M4.11 is not in §12", which was true when it was written and is not true now: §12
carries an M4.11 row. The script measures more than this row asks and is the natural evidence for
it.

### M2 — Rate, Personal Ledger, nightly refit, scoring, Home shelves, model-log rail, placement

**Criterion (§12, verbatim):** "50–100 verdicts each produce visibly personal rankings — the first
real-user validation of the whole corpus project; every owned title has a coordinate (warm Backbone
row or Cold Tower placement)"

**Status:** UNMEASURED. **Output file:** none. **Blocking:** owner's call.
**Owner verdict:** `________`

See §3 below. The second clause has effectively been measured — `ops/m45_exit_criterion.py`'s
"owned titles still unplaced after import" check reads 0 against the real bundle, and it is a real
check rather than a tautology since M4.8 repaired it. The first clause, which is the one §12 calls
"the gate", has never been run by anybody.

### M3 — Rank view: tiers, filters, drag-drop, comparison queue

**Criterion (§12, verbatim):** "stable tier lists both users endorse"

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

`ops/m3_exit_criterion.py` measures it against two constructed people and 30 verdicts each. It was
run against the **8-title synthetic fixture**, which is six films and a six-row board; §6.3's own
budgets ("~10-20 comparisons place a new title", "the first *stable* tier list arrives at
~1,500-3,000 comparisons") are untouched at that scale, and §13's agreement figure read 1.00 on
n = 1. `M3-open-points.md` §1.4 asks the owner whether that is acceptable evidence; it is still
unanswered and is recorded again in §8 below.

### M4 — Tonight: lobby, discovery, push join, the round, guest hand-off, combine, reveal, solo

**Criterion (§12, verbatim):** "a real Friday night resolved by the app"

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

`ops/m4_exit_criterion.py` ran over a six-candidate fixture pool, where — as `M4-open-points.md` §4
records — both seats answer all fifteen distinct pairs, the round ends by exhaustion, convergence
has no room to fire and the twenty-pair cap is never reached. `ops/m412_exit_criterion.py` is the
one that refuses to run on a fixture pool and measures the shipped 696-title owned pool; §12's M4.12
row names it. Neither has a committed output.

### M4.5 — the real bundle importable, and who owns the ids

**Criterion:** M4.5 has **no §12 row**. Its own stated goal is "make the real bundle importable, and
settle who owns the ids", and `ops/m45_exit_criterion.py` measures that in six numbered sections.

**Status:** MEASURED. **Output file:** `docs/milestones/M4.5-exit.txt`. **Blocking:** yes.
**Owner verdict:** `________`

Run at this milestone, on 2026-09-17, against `CORPUS_BUNDLE_DIR=.../export_bundle/v20260828` and a
live Postgres 16, by a script this milestone did not modify. **Result: `17/17 checks passed`, exit
code 0**, in 158 s of which 126 s is the import itself. **This is the first exit-criterion output
ever committed to this repository.**

**The count is 17 and the M4.5 record says 18. The difference is a repair, not a regression.**
Commit `260902c` is titled "the exit criterion passes 18/18"; commit `eef036c` ("test(M4.8): the
instrument, repaired") removed one of the eighteen, because its predicate was the literal `True` —
a summary of the loop above it recorded as a PASS, which is a check that cannot fail counting
toward a published score. It is now printed as a reading rather than scored
(`ops/m45_exit_criterion.py:301-307`). The predicate that *can* fail still fires inside that loop.
So **17/17 is a stronger statement than the 18/18 it replaces**, and the `18/18` under
`M4.5-plan.md`'s "Against `v20260828`, **18/18**:" heading (`:320` as this milestone closes) is
superseded by this file rather than by an edit to the plan (decision 296). Anchored on the heading
for section 7.3's reason: this citation said `:334-335`, which is the `Owned unplaced PASS 0` line
and the fence under it — a check that survived, offered as the one that was dropped.

**Decision 291 costs this criterion nothing, which was measured rather than assumed.** The genome
slice is now skipped, so section 2 reports `29 shipped, 9 skipped with a reason` where it used to
report six skipped, and section 5 reports `genome=0` where it used to report `genome 781`. Both
still pass: the accounted-table check asks for a count **or** a reason, and the empty-block check at
`:277` already excluded `genome` by name. The check count is unchanged by it.

Numbers worth having in one place, since §12's M0 row is asserted over them: 19,071 titles, 46,318
`title_meta`, 281,655 credits, 84,881 people, 31,540 `dna_tag`, 223,136 `dna_projected`; 11,003
overviews, 6,827 taglines, 10,021 posters, 8,303 trailers; max seeded title id 21,442 against a
1e9 floor; and **owned titles still unplaced after import: 0**.

**The file is committed as UTF-8, and the run did not produce UTF-8.** Python writes a redirected
stdout in the console's locale encoding, so the script's six `§` characters arrived as cp1252
byte `0xA7` and the file did not decode as UTF-8 at all. Every guard in `backend/tests/` reads with
`encoding="utf-8"`, so committing it as produced would have made this record unreadable by the gate
that is supposed to check it. It was re-encoded to UTF-8 — **the same characters, a different
encoding**; no line of the measurement was altered, and the CRLF endings are the run's own.

### M4.6 — user management, the two-role account table, account creation off the wizard

**Criterion (§12, verbatim):** "an admin adds a third member, resets that member's password, then
disables and deletes them, from the Users tab alone; no account can be created anywhere else, and
the last admin cannot lock the household out"

**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes. **Owner verdict:** `________`

No exit script exists. Every clause is asserted by the suite — integration tests for the roster
route and the last-admin refusal, e2e for the Users tab — but the criterion is a sequence performed
end to end by one admin, and nothing performs it as a sequence. This is the cheapest of the
unmeasured rows to close: it needs a stack and a script, not a decision.

### M4.7 — the box: config, secrets, health, jobs, backups, restore, image

**Criterion (§12, verbatim):** "a dump restored by the README procedure boots and serves, and the
same dump under a changed `SECRETS_KEY` boots too - every member write still commits with a reason
naming the key, no route 500s, and the operator can re-enter the credential from the UI"

**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes. **Owner verdict:** `________`

No exit script. The first half is asserted in-process by `backend/tests/test_restore_drill.py`,
which dumps and restores a real database in a second process. The **changed-`SECRETS_KEY` pass is
asserted nowhere end to end**, and it is the half the criterion exists for: it is the day the
household's keys are wrong and the route that would fix it is the one that 500s. Release leg 4
is WRITTEN to perform the README gestures at stack level and has never been dispatched (§2.1);
the second pass under a changed key stays in
`docs/TESTING.md`'s human checklist, because it needs an operator re-entering a credential in the
UI.

### M4.9 — from the corpus to the card

**Criterion (§12, abbreviated; the cell is one sentence of about 600 characters):** against the real
export bundle, every `dna_tag` and `dna_projected` row carries a facet the eleven-facet vocabulary
knows; every title card renders past its credits and prints how many it hides; the catalogue paged
end to end under a concurrent rewrite returns each title exactly once; no projected DNA term
outranks an extracted one; and no shipped table is skipped unaccounted.

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

`ops/m49_exit_criterion.py` measures it against a real corpus bundle and has been run. The last
clause — "no shipped table is skipped unaccounted" — is the one decision 291 moves: three tables
are now deliberately skipped **with** an accounted reason, which is what that clause asks for.

### M4.10 — the verdict path and the tier board

**Criterion (§12, abbreviated):** two simultaneous taps on one card token, and two simultaneous
answers under one sealed pair, each leave exactly one observation and one refusal that names why,
eight repetitions running; with the refit forced to refuse and a 300-character title name no Rank
or Rate route raises after its row has committed; and a member's first sitting gets a prediction
reveal on every tap but the first.

**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes. **Owner verdict:** `________`

No exit script. This criterion is a concurrency statement and the suite asserts it directly — the
eight-repetition races are integration tests against a real Postgres, which is the right instrument
for it and arguably a better one than a hand-run script. What is missing is not the measurement but
the committed record that it was made at release time rather than at merge time.

### M4.11 — seen states both ways, for a real household

**Criterion (§12, abbreviated):** two members, a duplicated copy and a running series, two phones
each: an explicit "not seen" survives two sweeps with nothing adopted and both copies clear; a new
episode does not un-mark the show; a revoked token stays flagged while a 404 on every write is
counted rather than hidden; the prompt arms once on an Episode session and stays answered; and both
notifications carry a distinct tag and a destination.

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

`ops/m411_exit_criterion.py`, against a two-member household and `ops/fake_jellyfin.py`. Its
docstring's "M4.11 is not in §12" is stale, as noted under M1.

### M4.12 — Tonight, end to end on the real pool

**Criterion (§12, abbreviated):** a real evening resolves on the shipped 696-title owned pool — two
members and one guest seat on one phone reach the blind reveal with an approval share over three
seats and one `session_outcome` row; a guest seat's first pair and a zero-label member's first pair
and first answer are each under 1.5 s; an unrelated read answers while an answer is in flight; a
`play.finish` made to raise once still reaches the ballot on the next read; and pools of exactly two
and exactly three candidates reach the ballot rather than waiting. The cell names the instrument:
"Measured end to end by `ops/m412_exit_criterion.py`, which refuses to run on a fixture pool".

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

**§12 names the script and the tree holds no output of it.** That is the narrowest form of the gap
this file exists for: the normative document asserts a measurement whose record does not exist.

### M4.14 — bundle import and artifact custody

**Criterion (§12, abbreviated):** a 1.04 GB real-bundle archive imports through the Data tab with
`POST /api/admin/bundle/import` returning in under 5 s and progress polled to completion, surviving
a proxy that cuts at 100 s, with `/api/health` answering throughout; every file listed in
`BUNDLE.json` sha256-verified before a single row is written; and each of eight failure shapes ends
as a report line with the install byte-identical to before. The cell names the instrument:
"Measured end to end by `ops/m414_exit_criterion.py`, thirteen numbered checks, which refuses to run
on the fixture".

**Status:** RUN, OUTPUT NOT COMMITTED. **Output file:** none. **Blocking:** yes.
**Owner verdict:** `________`

Same shape as M4.12: §12 names the script, the tree holds no output. `ops/m414_exit_criterion.py`'s
own docstring says "§12 gives M4.14 no row of its own until this milestone writes one" — M4.14 did
write one, so that sentence is now stale in the same way M4.11's is.

### M5.1 — the acquisition spine: queue, raw store, polite fetcher, ten-stage driver

**Criterion (§12, abbreviated):** with stages 2-8 declared no-ops, a task injected for a Jellyfin
item carrying provider ids walks stage 1 to 9 to 10 — a title minted above 1e9 with `origin =
'acquired'`, placed by the Cold Tower, on Home with the cold badge; a worker killed mid-lease has
its task reclaimed by the next worker and completed exactly once; one URL fetched twice writes one
file under `/data/raw` and two `raw_document` rows, and the second parse issues no request; an item
with no provider id parks at stage 1 with that reason and mints nothing; and `/data/raw` is
readable by the worker and absent from the backend container. The cell names the instrument:
"Measured end to end by `ops/m51_exit_criterion.py`, twelve numbered checks, which refuses to run
on the fixture".

**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes. **Owner verdict:** `________`

The row was written as the milestone opened, under decision 321's split of M5 into seven and decision
331's rule that each sub-milestone writes its own §12 row when it opens rather than having one
pre-written for it. **The status moved from NOT BUILT to UNMEASURED inside the same change set**, and
the two words are not the same claim: `ops/m51_exit_criterion.py` is now in this tree, 1,540 lines and
twelve numbered checks, and it has never been run. The lane that built the spine could start no server
and no container by construction, which is why the instrument was written to degrade rather than to
assume: checks 9 (`/data/raw` absent from the backend image) and 11 (`POST /events/nothing` answering
404 through the app that ships) print "NOT MEASURED HERE" and the run exits 3 - neither a pass nor a
failure - rather than counting an unasked question as an answer. This row therefore joins M0, M2,
M4.6, M4.7 and M4.10 in the column this file exists for, and it is the only one of the six that an
owner can close by running something rather than by first writing it.

### M5 — acquisition pipeline, admin connector UI, LLM layer, extraction flywheel

**Criterion (§12, verbatim):** "a new Jellyfin add reaches 'ready' unattended — and, measured with it
by `ops/m5_exit_criterion.py`: the flywheel appends a naming failure at the moment it happens and an
admin launches exactly the batch they selected; the spend cap parks a paid stage with its reason
instead of billing for it, and never auto-retries past it; a provider response that violates the
extraction contract is retried exactly once with the violation named, and a second violation fails
the stage and writes nothing; a re-derive of a title carrying a curated correction still carries it
afterwards; and a burst of adds for one series yields one job for the show rather than one per
episode (decision 331)"

**Status:** NOT BUILT. **Output file:** none. **Blocking:** no. **Owner verdict:** `________`

Decision 331 amended the criterion above in place rather than forking it: clause one is the sentence
this row has always carried, and the rest is the half `ops/m5_exit_criterion.py` will measure, written
down so a build that satisfies only the first cannot be signed for the whole. M5 is now the umbrella
over M5.1 through M5.7, and each of those writes its own row here as it opens.

### M6 — Map, compositional search, taste comparison viz

**Criterion (§12, verbatim):** "—" (§12 states none)

**Status:** NOT BUILT. **Output file:** none. **Blocking:** no. **Owner verdict:** `________`

### M7 — acquisition lens, UMAP similarity lens, HA hooks, comfort-shelf carryover

**Criterion (§12, verbatim):** "—" (§12 states none)

**Status:** NOT BUILT. **Output file:** none. **Blocking:** no. **Owner verdict:** `________`

M7 also owes §11's three designated seams and §7.3's `POST /events/playback` (decision 290).

---

## 2. The exit scripts, and what each has produced

Eight scripts exist under `ops/`. **Exactly one committed output exists in this tree**, and this
milestone wrote it.

| Script | Measures | §12 row | Committed output |
|---|---|---|---|
| `ops/m3_exit_criterion.py` | M3, against the 8-title fixture | M3 | none |
| `ops/m4_exit_criterion.py` | M4, over a six-candidate pool | M4 | none |
| `ops/m45_exit_criterion.py` | M4.5, against the real bundle | none | `docs/milestones/M4.5-exit.txt` |
| `ops/m49_exit_criterion.py` | M4.9, against the real bundle | M4.9 | none |
| `ops/m411_exit_criterion.py` | M4.11, two members + fake Jellyfin | M4.11 | none |
| `ops/m412_exit_criterion.py` | M4.12, the 696-title owned pool | M4.12 | none |
| `ops/m413_exit_criterion.py` | M4.13, its own stated goal | none | none |
| `ops/m414_exit_criterion.py` | M4.14, the 1.04 GB archive | M4.14 | none |

**Five §12 rows have no script that measures them at all:** M0, M2, M4.6, M4.7 and M4.10. M1's row
has no script of its own either, but `ops/m411_exit_criterion.py` measures a superset of it, which is
why it is not in that list. M4.13 has a script and no §12 row, which is correct — §12 does not carry
M4.13, M4.8, M4.15 or M4.16.

**Two script docstrings are stale about §12 and are recorded here rather than repaired**, because
they are not this milestone's files and the sentences are historical rather than load-bearing:
`ops/m411_exit_criterion.py:3` says "M4.11 is not in §12" and `ops/m414_exit_criterion.py:3` says
"§12 gives M4.14 no row of its own until this milestone writes one". Both rows now exist.

**One repair is already discharged and is recorded so nobody looks for it again.**
`M4.16-plan.md` carries forward a finding that `ops/m3_exit_criterion.py` returns 0 unconditionally,
so a failed M3 measurement would exit green. **It does not.** `ops/m3_exit_criterion.py:298` reads
`return 1 if problems else 0` over an explicitly built `problems` list, and its neighbours carry the
`[M4.8 ti-m3-and-m4-exit-scripts-cannot-report-their-own-failures]` citation. M4.8 fixed it.

**A second is discharged the same way.** The plan directs this milestone to delete a `check(True,
...)` in `ops/m45_exit_criterion.py` and to change `check(placed == 0 or True, ...)`. Neither
survives: `grep -n 'check(True' ops/m45_exit_criterion.py` is empty, and `:330` reads
`check(placed == 0, ...)` with the comment "The predicate is the query's own answer, with no literal
disjoined onto it". M4.8 repaired both under
`[M4.8 finding 19; ti-m45-exit-criterion-two-checks-cannot-fail]`. **The script was therefore run
unmodified by this milestone**, which is the stronger outcome: the output committed here was
produced by a script nobody touched in the same change, so the number cannot have been arranged.

That second check is worth naming twice, because it is the only place in the repository where §12's
M2 criterion is executed rather than described: `SELECT count(*) FROM title WHERE is_owned AND
placement = 'unplaced'` is verbatim M2's "every owned title has a coordinate", and it reads 0
against the real bundle. See §3.

### 2.1 The release workflow itself — **never dispatched**

**Status:** UNMEASURED. **Output file:** none. **Blocking:** owner's call.
**Owner verdict:** `________`

This section exists because the rest of this file would otherwise be silent about the one
instrument the milestone was written to add — and silence in a record whose stated job is "the fact
of a run, or the fact of its absence" reads as the first.

| Instrument | What it decides | Committed output |
|---|---|---|
| `.github/workflows/release.yml` | whether this build ships: five legs, in order, with an exit code | none |
| `ops/coverage_gate.py` | whether the tests a row names actually RAN, over real reports | none |
| `backend/tests/test_spec_coverage.py`'s kind guard | whether a row's evidence sits at the LAYER its `kind` declares | the block `docs/TESTING.md` pastes |

Three instruments and not two, because the one-sentence criterion — "counts only rows whose named
tests actually executed **at or above the row's declared kind**" — is two claims with two owners.
`ops/coverage_gate.py` has no notion of `kind` at all and says so in its own module docstring; the
kind guard cannot ask whether a test ran, because a run that skipped the evidence would skip the
check with it. Keeping them apart is decision 312's call; naming both here is the price of keeping
them apart, so that a reader starting from this table is never told the criterion resolves to one
instrument reading in one direction.

**`.github/workflows/release.yml` has never run, and no leg of it has ever been deliberately broken
and watched to fail.** It could not have: the file has never been pushed, and GitHub cannot dispatch
a workflow it has never held. Nor could it run today if it were pushed. It carries `runs-on:
[self-hosted, spielplan-corpus]`, and decision 183 already settles what that means for a workflow
nobody has registered a runner for — the job queues and is cancelled after 24 hours. No such runner
exists; **Running it** in `docs/TESTING.md` says so of `real-bundle.yml`, which wants the same
label, and this workflow inherits that sentence whole.

What HAS been shown failing is each of the guards' own rules, and that is a different and weaker
statement than the criterion asks for. `backend/tests/test_release_gate.py` pairs every rule it
holds over the workflow's text with a synthetic violation, and the coverage gate with failure
self-tests of its own, so the RULES bite. **The JOB has never returned an exit code.**

Per leg, and read down the second column rather than the first: the substance of two of these legs
has been run whole from this lane by hand and two more in part, which is why their milestones' rows
above are what they are — but a leg run by hand is not a leg run in order, by one job, with the next
leg consuming what it left behind, and that ordering is the whole of what the workflow adds.

| Leg | Substance run from this lane | By this job |
|---|---|---|
| 1 — the full suite, with `--junitxml` | yes, every cycle; the JUnit itself never | never |
| 2 — `ops/coverage_gate.py` over real reports | partial — one scoped JUnit, by hand, from this lane | never |
| 3 — the real-bundle legs and M4.5's criterion | partial — `ops/m45_exit_criterion.py` against v20260828 (`docs/milestones/M4.5-exit.txt`); the two `-k real_bundle` tests never, they skip in this lane | never |
| 4 — the restore drill at stack level | no; recorded UNMEASURED under M4.7 above | never |
| 5 — the two-phase phone e2e | yes, every cycle | never |

**Leg 3 is two commands and only the second of them has ever run.** The first is `pytest
backend/tests/test_bundle_shapes.py backend/tests/test_bundle_validation.py -k real_bundle`, and
both of those tests are `skipif`-gated on `CORPUS_BUNDLE_DIR`. Run verbatim in this lane it selects
those two and reports both as skipped, which is what it reports on every machine in this project:
`docs/TESTING.md` records `real-bundle.yml` as the only place that variable is set, and no runner
carrying its label has ever been registered. `ops/m45_exit_criterion.py` overlaps the validator
half — it calls `bundle_import.validate()` over v20260828 and `M4.5-exit.txt` records the PASS —
but it never calls `render()` and never touches `ops/bundle_shapes.py`, so the live-versus-committed
manifest comparison `real_bundle_shapes.json` exists for has never been made by anything. That is
the cell decision 183 has been owing since M4.8, and it is recorded here as a partial rather than
carried as a `yes`. [M4.16 cycle 4, REL-C4-10]

Leg 2 is the one with the thinnest fallback reading. `e2e/playwright.config.js` adds the `json`
reporter only under `CI`, so no run made in this lane has ever produced the BROWSER report the gate
parses; `e2e/.results/` here holds nothing but Playwright's own `.last-run.json`.

**The pytest half is no longer only synthetic** (decision 313). Once, from this lane, scoped —
never the whole suite. **Re-run and re-measured on this branch on 2026-09-18:**

```
pytest backend/tests/test_release_gate.py -q --junitxml=<scratch>/real-junit.xml   # 103 passed
python ops/coverage_gate.py --junit <scratch>/real-junit.xml                       # exit 1
  coverage gate: 42 test result(s) read from 1 JUnit and 0 Playwright report(s)
  coverage gate: 42 row-and-test pair(s) confirmed executed
  coverage gate: 47 named vitest id(s) not visible here (decision 226: no row rests on one alone)
  coverage gate: 312 row(s) name evidence that did not run, in 2252 line(s): ...
```

**Four of those five figures are re-derived rather than remembered**, which is why this block is
dated and not merely pasted. It was pasted once and went stale inside the diff that published it —
it read 92 and 2071 against a command already printing 95 and 2076, because the review cycle that
added tests to this file did not re-run the one command this section prints. Every other count
this milestone publishes has a guard that counts it off the live map, and this one had none.
`test_the_leg_two_transcript_publishes_the_figures_this_map_would_print` now derives what the
parser READ (one result per test function in that file), what it CONFIRMED (one per row-and-test
pair), the vitest line and both halves of the failure summary, so the block reddens the day the map
moves instead of quietly ceasing to be true. It has done that twice already, which is the only
evidence that it works: the §10 carrier sweep registered five guards on two rows, the line figure
went to 2095, and the two commands above were re-run rather than the number edited (M4.16 cycle
5, M416-C4D2-SPEC-01 and M416-C5-ATTR-01); then the build-context rule registered two guards in
THIS file, which moves the read and confirmed figures as well, and the commands were re-run again
(M4.16 cycle 5, M416-C5-DOCKER-01). Neither of those two moved the ROW figure, because every row
involved already named evidence this scoped run never touches. **M5.1 is the third time and the
first to move it**, because it added ROWS and not only ids: six new requirements, closed by ids
in seven files this scoped run does not select, take 306 rows to 312. Its review cycles then moved
the line figure a fourth, a fifth and a sixth time without moving the row figure at all - every id
they registered went onto a row that was already being counted - and the commands above were re-run
on each occasion rather than the numbers edited. The sixth says plainly what this guard is for: the
fix rounds that grew the map each ran the test files they had touched, and this figure is held in a
file none of them touched, so nothing went red until a run over every file the milestone changed.
That is the third, fourth, fifth and sixth pieces of evidence that this guard does what it was
written to do, and the first showing it holds the row figure as well as the line figure. The line
figure is NOT decomposed in this paragraph any more: the decomposition was a second copy of a
measurement, it went stale in the same cycle that moved the figure, and that is what the sentence
below strikes decision 313's own copy for. [M5.1, green pass; M5.1 review cycle 1, green pass;
M5.1 review cycle 4, green pass; M5.1 review cycle 4 second pass, green pass] The fifth, pytest's
own `N passed` line, counts parameterised CASES; no static rule can take it without evaluating every
`parametrize` list, so it stays what it is — a dated console reading, with nothing below claiming
anything about it. It is NAMED here rather than restated: this paragraph carried a second copy of
that figure, and the copy went stale across both re-runs above while the block itself stayed
true - which is the reason decision 313's own copy was struck rather than re-synced.
[M4.16 cycle 5, green pass]

**What that proves**, and it is four things rather than a green: the parser reads a report pytest
actually wrote; `junit_family = xunit2` emitted no `file=` attribute on ANY `<testcase>` element in
it, so `_module_from_classname` is the LIVE branch and it mapped `tests.test_release_gate`
back to `backend/tests/test_release_gate.py::<name>`; parameterised cases folded, every element
collapsing to the 42 base ids the map spells; and every outcome it read was `executed`, with the 42
ids this map names in that file exactly the 42 it confirmed — the set difference is empty, checked
against the map rather than counted off the console.

**What it does not prove**, stated because a partial recorded as a pass is the defect this whole
file exists to refuse: the run was SCOPED, so the exit code is 1 by design — the rows listed under
that last line name evidence the run never touched, which is absence rather than a finding, and
they are counted as rows AND as lines because a row naming six unrun tests is one uncovered
requirement and six lines (the summary printed the second figure under the first one's name until
review cycle 4). No Playwright report was
involved, so the browser half of leg 2 is still exercised only by the synthetic reports its
self-tests build out of the live map. And **the JOB has still never returned an exit code**: a
command run by hand from a worktree is not a leg run in order, by one job, over the artifacts the
leg before it left behind.

**What a first dispatch takes**, stated so that the work is specified rather than lost: the branch
pushed; a runner registered with the `spielplan-corpus` label on a Linux box with Docker and the
~1.15 GB corpus already on its disk; the repository variable `CORPUS_BUNDLE_DIR` pointed at it; a
successful `ci.yml` run for the same commit, because leg 2 refuses without one; then one
`workflow_dispatch`, and a second with one leg deliberately broken to confirm the job exits
non-zero rather than skipping green. Both runs' artifacts belong in the tree beside
`docs/milestones/M4.5-exit.txt`, and this section's Status becomes `MEASURED` on the day the first
of them is committed — at which point the guard holding the verdict unfilled is deleted in the same
change, which is decision 297's polarity and the reason it reads that way.

---

## 3. M2's criterion: unmeasured, and what a measurement would take

Recorded under **decision 297**. §12 calls M2 "the gate — and the moment the corpus project's
biggest open caveat ('does any of this transfer to two real people?') gets its answer". Its
criterion, as written:

> 50–100 verdicts each produce visibly personal rankings — the first real-user validation of the
> whole corpus project; every owned title has a coordinate (warm Backbone row or Cold Tower
> placement)

**It has never been measured, by anybody, in any form.** The second clause has been: M4.5's exit
script asserts zero owned titles unplaced after a real import, and since M4.8 that check can fail.
The first clause has not.

**What a measurement would take**, so that the size of the ask is on the record rather than in
somebody's head:

1. A seeded stack with the real bundle imported, and **two members** — not two constructed
   personas on one account.
2. **50 or more verdicts each through `/api/rate`**, the real route, so that the anchoring rules,
   the class balance and the undo window are the ones a household meets. `ops/m3_exit_criterion.py`
   already does this shape at `VERDICTS_EACH = 30`; the loop is reusable and the constant is the
   change.
3. Then `/api/home` **and** `/api/rank` for both members — §12's clause is about rankings the
   household sees, and Home's shelves are named in the same M2 row as Rank's board. m3's script
   reads `/api/rank` only.
4. **Spearman rho between the two members' orderings, below a threshold**, and **both orderings
   differing from `title_prior.b`**. The second half is the one m3's script does not do at all, and
   it is the half that distinguishes "personal" from "the crowd chart with letters on it". Without
   it a run could report two low-correlation boards that are both simply noisy.
5. A threshold the owner sets. There is no defensible number in the spec; §0 row 1's crowd-derived
   expectation curves are named as the M2 yardstick and §13 asks for per-user held-out Spearman
   against them.

The natural home is a new `ops/m2_exit_criterion.py` beside the others, or an extension of
`ops/m3_exit_criterion.py`. **It is the largest single unbuilt item on the road to M5** and it is
the only one that needs a seeded stack and real corpus data at once. It is not built here.

**Owner verdict: `________`** — unfilled, and fillable only by a run.

---

## 4. Corrections owed to the milestone records

Recorded here and **not** applied to the plans themselves (decision 296): `docs/milestones/*.md` are
dated records of what was believed at the time, and editing one destroys the evidence of the drift.
Each row below gives the file, the line, the claim as written, and the truth measured on this branch
on 2026-09-17.

### 4.1 `M4.5-plan.md:169` — the backend image's Postgres client

**Claim as written:** the second half of the backup criterion "needs a `pg_dump` binary the backend
image does not carry (`ops/backend.Dockerfile` installs `libpq5` and `curl` only)", and the
consequence is "a Phase 2 decision to be recorded when it is made".

**Measured:** `ops/backend.Dockerfile:31` does install `libpq5 curl ca-certificates gnupg` — and
`:33-40` then adds the PGDG apt key and repository and installs **`postgresql-client-16`** at
`:39`. The image carries `pg_dump`. The Phase 2 decision was taken and shipped at M4.7, and the
nightly dump job runs against it.

### 4.2 `M4.5-plan.md:366-370` — `ArtifactStore.summary()` and the Data tab's title count

**Claim as written:** "`ArtifactStore.summary()` still reads title counts from
`artifacts/manifest.json`, which is the ratings-model manifest and carries none. ... The §6.6 Data
tab will render no title count on a real bundle."

**Measured, and half of it is now false.** `backend/spielplan/models/artifacts.py:458` reads
`self.identity.get("tables", {}).get("title")` — `identity` is `BUNDLE.json` as
`artifact_bundle.manifest` holds it (`:237`, `:316`, `:318`), filled by `importer/bundle.py`'s
`_identity` at `:200`. The comment above `:458` records the repair and its reason. Against the real
bundle the Data tab reads **19,071**.

**The half that is still true, and is why this row is kept rather than struck:** no coverage row
asserts that count against a real bundle. The repair is held by the comment and by the reader's good
faith, which is the shape this milestone exists to close. The plan's own sentence "it wants a row of
its own" is still owed.

### 4.3 `M3-open-points.md:389-392` — the backup waiver

**Claim as written:** the standing waiver `platform-backup-rotation-and-ciphertext` holds because
"no nightly `pg_dump` job exists. `grep -rn 'backup|pg_dump'` over `ops/`, `docker-compose.yml` and
`worker.py` still finds only the two volume mounts."

**Measured:** the waiver was discharged at M4.5 and the job shipped at M4.7.
`backend/spielplan/worker.py:1254` registers `Job("nightly-backup", "M0", "nightly", "minutes",
_nightly_backup, every=86400, ...)`, and `backend/spielplan/backup/nightly.py:43` reads `KEEP = 14`
under the comment `# §2: "rotation 14".`. That waiver is gone from
`backend/tests/spec_coverage.toml`. **Exactly one standing waiver remains there**, and it is not
this section's: M0's `data-rules-platform-rating-display-only`, whose premise a grep still upholds
and which was re-read at M4.16 rather than inherited.

This paragraph said **two** while it was being written, and the second was retired inside the same
change set before the milestone closed - so the sentence above went stale under its own author, and
kept instructing a reader to perform an edit that had already landed. See §9's note on
`M4-open-points.md` §5, which records that retirement rather than owing it. `docs/TESTING.md`'s
current-set paragraph and the report the instrument prints are the two readings this one is held
against.

### 4.4 `M4-plan.md:368-373` — what ends an idle Tonight room

**Claim as written:** "States `open → voting → ballot → resolved`, plus `abandoned`; **a worker job
expires sessions idle beyond 6 h**. That job is not in §5.3's table — named out loud in its
docstring rather than smuggled in".

**Measured: nothing sweeps an idle Tonight room, and no such job was ever written.** `worker.JOBS`
carries `Job("session-prune", "M0", "hourly", "ms", _prune_expired_sessions, ...)` at
`worker.py:1009`, and `_prune_expired_sessions` at `:98-103` executes
`DELETE FROM auth_session WHERE expires_at < now()` — **expired authentication sessions, not Tonight
rooms.** The two share the word "session" and nothing else.

`backend/spielplan/tonight/rooms.py:422` states the position that actually shipped, and states it as
a refusal: "Not a 6 h idle sweep — that is M4.7's worker registry if it is built at all, and a
household that wants its evening back wants it now." What M4.12 shipped instead (decision 169) is a
host control in the lobby plus `play.settle`, which moves a room whose seats have all ended. This is
the one correction in this section where the record described a mechanism that never existed at all,
rather than one that has since changed.

### 4.5 `M4.16-plan.md:858` — exit criterion 1's third grep cannot be empty

**Claim as written:** the criterion lists three greps and ends "All three empty."

**Measured on 2026-09-17, all three run verbatim.** The first
(`tv kiosk|/tv\b|member-account creation|three existing seams` over the normative file) exits 1 with
no output. The second (`carried over verbatim`) exits 1 with no output. `NEITHER` hits once and
`uniform_holdout` three times, as the criterion also asks. The third, `grep -rn 'not spec' docs/`,
**exits 0 and structurally cannot exit 1** — its own command line, at `M4.16-plan.md:858`, contains
the string it searches for, and decision 296 forbids editing that file. Eight hits outside this
record, enumerated below; writing them down adds three more (this section's own quotation of the
command and of two of the hits), for **eleven in all on the day this was measured**. That is not a
miscount, it is the property: the line cannot be made empty by any edit, including this one, which
is why it is signed as named hits and never as "empty".

| Hit | What it is |
|---|---|
| `M3-plan.md:220`, `M4-plan.md:402` | a "**Not building**" heading carrying the sentence decision 288 retired — true when written, and decision 296 routes corrections away from the plans |
| `M4.16-plan.md:71`, `:467`, `:858` | the criterion's own text, quoting the sentence decision 288 retired and the grep that looks for it |
| `M4.6-plan.md:460` | "Do not special-case 0 as disabled" — the substring, in a sentence about a config value |
| `ROADMAP-to-M5.md:545` | a dated quotation of the header that has since been replaced |
| `spec-v2.2-proposals.md:135` | "the rendering is not specified" — the substring again |

**What the criterion is actually about is held mechanically and is green.** `_provisional_records`
in `backend/tests/test_static_contracts.py` anchors on the exact retired sentence — spelled once,
in that guard, and deliberately nowhere else under docs/ — and skips `docs/milestones/`, for the
reason its own comment gives: four plans and the roadmap quote that sentence as history and
decision 296 forbids editing them. That
rule, not the bare grep, is what a signature on criterion 1 can honestly rest on — and it returns
nothing. **The bare third line is signed as "benign hits, named above", never as "empty"**: a
gate that cannot reach its pass state is the shape this milestone exists to remove, and fudging the
line would be that shape one level up. (Decision 296; M4.16 cycle 4, M416-C4-SPEC-06.)

### 4.6 `M4.16-plan.md:522,874` — the kind guard's allow-list holds one row, not three

**Claim as written:** land the kind guard "with a **dated allow-list of exactly the three known
unit-only rows**" — `platform-key-rotation-semantics`,
`data-rules-model-artifacts-load-from-the-shipped-bundle`,
`data-rules-validation-reports-rather-than-raises` — and exit criterion 2 restates it as "holds
exactly the three unit-only rows and is asserted not stale".

**Measured on 2026-09-17: `UNIT_ONLY_ROWS` holds ONE**, and that is the plan's own instruction
carried out rather than a shortfall. The plan says in the same breath that the three "are resolved
by decision 168 and by writing the missing DB-level assertions, not by waiving", and two of them
since have been: `platform-key-rotation-semantics` through decision 289's `test_secrets_custody.py`,
which takes `db` and `pg_url`, and `data-rules-validation-reports-rather-than-raises` through
`test_dna_import.py`. `test_the_unit_only_allow_list_is_not_stale` then makes keeping them an
ERROR — an entry must still be an offence with the list emptied — so the only green state is the
one that ships. The surviving entry is
`data-rules-model-artifacts-load-from-the-shipped-bundle`, dated, whose `kind` integrates against
`CORPUS_BUNDLE_DIR` rather than Postgres.

**Why it is recorded rather than left to be re-derived.** A reader auditing exit criterion 2
against the tree finds one where the criterion says three and cannot tell a shrinking allow-list —
the intended direction — from a guard somebody narrowed. The count is not restated here as a
number to keep true: the tuple is the count, and the staleness test is what holds it.
(Decision 296; M4.16 cycle 4.)

### 4.7 `M4.16-plan.md:651` — the CC BY-SA credit links no material

**Claim as written:** Phase J asks for "Wikipedia and TVmaze — CC BY-SA credit, linking to the
article/show where `title_meta` carries it".

**Measured on 2026-09-17.** The credit ships and the link does not.
`frontend/src/lib/components/DataSources.svelte` names the contributors for each source and links
the CC BY-SA 4.0 deed, which is the licence's URI and not the material's. There is no reader for a
material URI anywhere: `grep -rn homepage backend/spielplan frontend/src` is empty,
`importer/load.py`'s `title` mapping does not carry `wikipedia_title`, and `importer/meta.py` keeps
the corpus row as `payload` jsonb that `resolve_title_fields` reads back by field name and never by
this one. `api/library.py`'s title payload is id/kind/name/original_name/year/runtime_min/overview,
so `/api/titles/{id}` exposes no field an anchor could be built from, and `TitleDetail.svelte` —
the surface the component's own paragraph forwards a reader to — renders no source link at all.

**The plan's condition could not be settled in this lane and is not signed either way.** The only
corpus either checkout holds is `backend/tests/fixtures/make_bundle.py`'s synthetic bundle: ten
`title_meta` rows with `homepage` NULL across all three sources, eight titles with
`wikipedia_title` NULL. `backend/tests/fixtures/real_bundle_shapes.json` says of itself "Shapes
only -- no values". So this entry records what is true of the BUILD — it holds no article
identifier it reads — and makes no claim about what the real corpus carries.

**Why it is recorded rather than repaired.** Decision 320 rules the link neither scheduled nor
built, on two grounds beyond the missing reader: for TVmaze the corpus field is `officialSite`, the
show's marketing site rather than the TVmaze page, so an anchor there would credit the wrong work
under a CC BY-SA notice; and CC BY-SA 4.0 qualifies the material URI "to the extent reasonably
practicable" under a chapeau attaching to what the Licensor supplied.
`map-taste-data-sources-are-attributed`'s `what` is widened to carry the record and is not narrowed
to the code. **Held by** `test_the_cc_by_sa_credit_records_the_material_link_it_does_not_carry`
(`backend/tests/test_static_contracts.py`), which re-runs that reading rather than restating it and
goes red the day either column is read — at which point this section is the false record and the
two are deleted together. (Decisions 296 and 320; M4.16 cycle 5, M416-C5-ATTR-01.)

---

## 5. Known unmet promises

Promises a normative document makes that the shipped app does not keep — the spec, and the
coverage map, which is the other document this project answers to. Each names the milestone that
owns the surface. The first three are not M4.16's to repair (decisions 289, 301, 303); the last
three are claims M4.16's own records make and this tree does not hold, escalated rather than
edited under `M4-open-points.md:212-213` and written down here because `spec_coverage.toml`'s
M4.16 header block says they are. Either way this milestone's job is that they stop being
invisible.

### 5.1 §6.8 renders the backend's words to the household under its own name (owns: M4.10)

An anonymous 401 reaches a surface that renders `err.message` raw, and that string is
`backend/spielplan/api/deps.py:119`'s `"not signed in"` — a developer's sentence, shown to a member
inside §6.8's copy register as though the household had written it. §6.8 is normative about the
register every user-facing string sits in, and this is a string that never passed through it.

**The path, measured end to end on this branch:** `frontend/src/lib/rate.svelte.js:298` assigns
`rate.error = err.message` with no register in between, and `frontend/src/routes/rate/+page.svelte:162`
renders it into a `role="alert"` banner. The same shape is at `rank.svelte.js:130` and at
`routes/+page.svelte:127` and `:146` for Home's two reads, so the surface is Rate's but the pattern is
not only Rate's.

**Filed by M4.15 and not repaired there** because the surface belongs to another milestone
(decision 285). Still true today, re-verified at this branch point.

### 5.2 A first read that FAILED counts as booted (owns: M4.10, M4.12)

`frontend/src/lib/rate.svelte.js:298-302` sets `rate.booted = true` inside a `finally`, so a boot
whose only read threw is indistinguishable from one that succeeded, and the loading state never
returns. **This is what makes 5.1 durable rather than a flash**: without the latch the bad string
appears and is replaced; with it, the surface settles into a booted state whose only content is the
backend's error.

**The same latch is in two more stores, and the brief's count was wrong in a way worth recording.**
Measured on this branch:

| store | line | shape |
|---|---|---|
| `frontend/src/lib/rate.svelte.js` | 301 | `booted = true` in a `finally` — the defect |
| `frontend/src/lib/tonight.svelte.js` | 218 | `booted = true` in a `finally` — the defect |
| `frontend/src/lib/session.svelte.js` | 172 | `booted = true` in a `finally` — the defect |
| `frontend/src/lib/rank.svelte.js` | 120 | success path only, inside `apply()` — **not** the defect |
| `frontend/src/lib/home.svelte.js` | — | carries no `booted` at all |

So it is Rate, Tonight and the Tonight **session** store, not "Rank, Tonight and Home". `rank.fail()`
at `rank.svelte.js:125-131` sets `rank.error` and returns without touching `booted`, which is the
correct polarity; it does leave `rank.loading` true, which is a different defect and is not filed
here because nobody has measured what it does to the surface.

### 5.3 §2 promises an operator-facing key rotation that has no surface (owns: M4.7)

Narrowed rather than scheduled, under **decision 289**. The premise this milestone inherited was
that §2's rotation is unimplemented. It is not: `backend/spielplan/core/secrets_cli.py` is a
declared console script (`backend/pyproject.toml:49`, `spielplan-secrets`) whose `rewrap` subcommand
calls `core/secrets.py`'s `rewrap_dek` at `secrets_cli.py:104`, shipped at M4.7 under decision 181
and named by `.env.example` and README's Recovery block.

**What is actually absent is narrower: an admin-facing rotation action.** There is no route, no
button and no §6.6 control; rotation is a command an operator runs on the box with both keys in
hand. §2 has been amended to say what exists rather than to promise what does not, and the absence
of a surface is recorded here rather than scheduled at an invented milestone.

### 5.4 The per-source licence text is not reachable by a member (owns: unassigned — decision 307)

**The promise.** Decision 298 ships the Data sources block with "the per-source licence text read
out of `rating_source`"; decision 292's cost paragraph says the surface "reads its licence text out
of `rating_source` where it can rather than hard-coding it, which is what those four columns were
added for"; and `backend/tests/spec_coverage.toml`'s `map-taste-data-sources-are-attributed` carries
the clause in its `what`, escalated there rather than edited.

**What ships.** `frontend/src/lib/components/DataSources.svelte` imports nothing from
`$lib/api.js`, makes no request and renders five notices this app wrote;
`frontend/src/routes/account/+page.svelte:332` mounts it with no props. The only route serving
`url` / `license` / `version` / `notes` is `backend/spielplan/api/admin.py:735`'s
`GET /api/admin/data/sources`, declared `async def data_sources(_: AdminUser, conn: DB)`.

**Why the block does not simply fetch it.** `AdminUser` answers 403 to a member — the member this
block exists for — and 401 with `X-Spielplan-Reauth: admin` to an admin past §3.2's 24 hours, which
`api.js` turns into the admin re-prompt. A member surface that fetched an admin route would render
nothing for most of the household and put an admin re-authentication prompt on the one page every
member reaches. The component argues that in its own header and hard-codes the five notices instead,
which is the correct call for the five and leaves the eleven datasets' own text where it is.

**What closing it would take.** A member-readable route serving the eleven `rating_source` rows'
four licence columns and nothing else — no keys, no counts, no per-title joins — on the boundary
`test_every_route_outside_the_anonymous_allow_list_refuses_a_signed_out_caller` already reads, plus
the fetch and the two states the block does not have today. M4.16 owns no route: its one code-and-UI
item was the block itself. Which milestone owes it is the owner's call, and §10 now says the route
**is not built** rather than implying the promise is kept (decision 307).

**Held by** `test_the_spec_does_not_promise_member_licence_text_nothing_serves`
(`backend/tests/test_static_contracts.py`), which refuses the normative file that promise for as
long as nothing under `frontend/src` reads such a route — and goes red the day one lands, so this
record is deleted in the same change as the debt.

### 5.5 A bundle hot-swap requires no explicit confirmation (owns: M4.14)

**The promise.** `map-taste-admin-bundle-report-and-diff`'s `what` ends "only the hot-swap step
flips artifact_bundle.active, and it requires explicit confirmation", written from §10's "a planned
admin event with a migration report — never a silent sync".

**What ships.** `POST /api/admin/bundle/import` (`backend/spielplan/api/artifacts.py:367`,
`async def import_bundle(body: BundleRef, conn: DB, _: AdminUser)`) takes a `BundleRef` and no
confirmation token of any kind, and no test asserts one. The route is admin-gated and the import is
a deliberate action, so what is missing is the second gesture the clause names rather than the first.

**Escalated, not edited.** The map does not get to strike a claim to fit the code
(`M4-open-points.md:212-213`), and M4.16 owns no route. The two ways out are an owner decision
adding a `confirm` field with a 409 on its absence, or an owner decision striking the clause.
Neither is taken here.

### 5.6 The comment-path exception list holds three entries and its row says one (owns: M4.16)

**The promise.** `platform-comments-name-files-that-exist`'s `what` says the guard carries ONE
standing exception.

**What ships.** Three: (1) `backend/spielplan/db/dna.py`, the entry the `what` names, repaired by a
docstring in `db/library.py` because `0004_dna.sql` is applied and checksummed; (2)
`frontend/static/tmdb-logo.svg`, decision 298's owed asset, which is a comment about an ABSENCE
rather than a citation to follow and is deleted together with section 7.1 the day the owner drops
the file in; (3) `sync/resolve.py`, named at `backend/spielplan/backup/movie_data.py:32` and
`backend/tests/test_backup.py:1378` — a genuine uncorrected citation, since the module is
`connectors/resolve.py`.

**Escalated, not edited.** The owner's call is whether (3) is repaired and (2) amended in decision
298's idiom, or the `what` restated as "a dated exception list, each entry held to the absence it
describes". Neither is taken here, and the row stays red prose rather than being narrowed to fit
the guard.

---

## 6. Known latent gaps

Not live today, and recorded so that the thing keeping them dormant is written down beside them
rather than remembered.

### 6.1 `/login` is public with no pull back to `/setup` (owns: M4.15's routing rule)

`frontend/src/routes/+layout.svelte:93` reads
`const PUBLIC = ['/login', '/setup', '/account/password'];`. Nothing pulls a visitor at `/login`
back to `/setup` while `setup.required` is true, so a spurious arrival there on a box that still
owes a wizard is unrecoverable by navigation alone.

**Why it is not live:** after decision 282, nothing reaches `/login` in that state — `landingRoute()`
reads `setup.required` and routes a signed-out visitor on an unconfigured box to `/setup`. Closing
the gap would mean adding a routing rule nobody asked for, so it is recorded (decision 303) and not
closed.

### 6.2 The upgrade leg has no previous tag to run against

`test-13` asks for a sixth release leg: the previous tag's image booted against a seeded volume,
then the candidate against the same volume. **`git tag` is empty on this branch** — there has never
been a tagged release — so the leg would be a no-op that reports green, which is the exact defect
`.github/workflows/release.yml`'s "a step that skips its whole body fails the job" rule exists to
prevent. **It is owed, blocked on the first tagged release**, and is deliberately not written as a
leg that passes by having nothing to do.

### 6.3 No leg restores a movie-data archive (owns: M4.7's restore criterion)

`backend/tests/test_restore_drill.py` dumps and restores a real database in a second process, and
release leg 4 performs README's *Restore a dump* gestures at stack level. Both are §2's `pg_dump`.
**Nothing in CI restores decision 162's movie-data archive at all**, and nothing restores one
written by an earlier build — which is the gesture README's *Recovery* block actually describes: an
archive off a stick, into a rebuilt box.

**Why it is not live:** the version boundary is asserted in-process instead, by
`test_a_restore_reads_an_archive_written_before_the_genome_slice_was_retired`, which writes an
archive with the pre-decision-291 `TABLES` and restores it with this build's. That test is the one
that found the boundary CLOSED — decision 291 struck three tables from `TABLES` and every archive
any shipped build had written was refused as "unknown [...], missing []" — and it is what decision
309 reopened it against. Closing the gap at stack level needs a second install and an archive from
a tagged build, which section 6.2 records this branch as not having. **It is owed, blocked on the
same first tagged release.**

---

## 7. Owed artefacts and checks

### 7.1 The TMDB logo (decision 298)

`frontend/static/` holds `fonts/`, `icon-192.png`, `icon-512.png` and `manifest.webmanifest`, and no
logo. TMDB's terms ask for the logo beside the attribution, and **no agent may fabricate a
trademarked asset**, so the /account Data sources block renders it as a named slot filled when
`frontend/static/tmdb-logo.svg` exists and renders nothing when it does not.

**Owed: the owner drops the file in from TMDB's own brand page.** The PLAYWRIGHT half of the e2e
row deliberately asserts neither its presence (which would outrun the evidence) nor its absence
(which would go red the day the debt is paid). Two other guards assert the absence ON PURPOSE and
come out in the same change as this section: `data-sources.test.js`'s "carries TMDB's not-endorsed
notice and renders no logo this tree does not hold", which counts `img` elements and expects zero,
and `test_static_contracts.py`'s `assert not TMDB_LOGO.exists()`, whose own docstring says the
repair is to delete it with its row entry. So paying the debt is a THREE-file change and arrives as
two deliberate reds, not as a neutral drop-in — the glob that fills the slot resolves the moment the
asset lands. The five notices themselves ship now, verbatim, and are asserted.

### 7.2 The unsigned device checks (decisions 281, 284, 302)

Several facts in M4.15's exit criterion cannot be produced by any run in this suite, because `env()`
resolves to 0 in every engine the suite drives and Playwright's viewport **is** the visible viewport.

**They are carried here by reference and by reference only, and this section therefore states no
list and no count.** The checks themselves, the reason each is unrunnable and the single signature
line all live in one place — `docs/TESTING.md`'s M4.15 entry in the milestone ledger, under
"cannot be produced by any run in this suite" — and that line is unfilled.
`test_the_owed_device_checks_are_recorded_and_still_unsigned` asserts that exactly one such line
exists in that file and that every field in it is still underscores. **Nothing is copied here and no
second signature line is written anywhere**, because a second one would let the debt be discharged
in one file while the guard reads the other — and a copied LIST has the same failure one step
earlier: the first draft of this section carried three bullets over a ledger that had six, and
called one of them retired.

Nothing has been retired. Review cycle 2's CDP work put the ARITHMETIC of the status-bar check into
CI — `06-responsive.spec.js` injects an inset into Chromium through
`Emulation.setSafeAreaInsetsOverride` and measures the header at both widths — and decision 281's
own amendment says of exactly that work that not one of the owed facts is discharged by it and the
signature line does not move. The DEVICE fact is what the line owes, and desktop Chromium with a
number injected has no standalone web view, no real inset and no rotation.

### 7.3 The README restore procedure — **discharged, not owed**

Checked rather than assumed, because §12's M4.7 criterion is defined over it: `README.md` carries a
`## Recovery` section with `### Restore a dump`, `### Upgrade` and `### Verify a backup` under it
(`:94`, `:102`, `:231`, `:322` as this milestone closes). The procedure release leg 4 is written to perform is the
one written there; that leg has never been dispatched (§2.1). **Nothing is owed here**, and the plan's conditional - "if it is missing at close,
RELEASE.md records the restore leg's documentation as absent" - does not fire.

Those four numbers moved once while this file was being written, because a sibling stage was editing
README in the same wave. **Headings are the anchor and line numbers are the convenience** - in this
file and in every record like it, which is half of why the corrections in §4 were needed at all.

---

## 8. `M3-open-points.md` §1-§3, with a verdict each

The M3 record's own three sections, each heading carried forward with what is true on this branch.
A verdict of OPEN is not a defect being ignored; it is a defect being *published* rather than
forgotten, which is the whole reason this list is reproduced instead of linked.

### §1 Decisions needed from the owner

| Heading | Verdict |
|---|---|
| 1.1 The straddle badge names which tier? | **ANSWERED** — decision 295 (proposal 76) |
| 1.2 Does §6 preamble's "undo everywhere" oblige Rank? | **OPEN** — no row names Rank undo |
| 1.3 Does `tier_edit.via = 'explicit'` get a producer? | **OPEN** — value still dead |
| 1.4 Scale of the exit-criterion evidence | **OPEN** — see §1's M3 row above |

**1.1** is closed in the direction of the record's option C plus a code change. §6.3 now states the
end-of-scale rule — "the badge names the single adjacent tier that exists — S straddles down to A+,
F up to D — and never repeats the title's own tier, which falls out of the arithmetic rather than
being clamped afterwards (decision 295, proposal 76)" — and `straddle_z` shipped at **0.15** rather
than 1.0 (decisions 175 and 214), which removes the wide-σ regime the conflict lived in. §2.3 below
is the same question and closes with it.

**1.2** and **1.3** are unchanged since M3 and neither has been asked again. **1.4** is now sharper
rather than resolved: M3's numbers are still the fixture's, and §1 of this file records the M3 row
as RUN, OUTPUT NOT COMMITTED for exactly that reason.

### §2 Spec defects for v2.2

| Heading | Verdict |
|---|---|
| 2.1 `tier_edit.tier` records an index into a mutable set | **OPEN** — high severity, no column added |
| 2.2 §6.3 gives the queue's shares and not its construction | **OPEN** — still in docstrings |
| 2.3 §6.3's straddle example contradicts proposal 76 | **ANSWERED** — with 1.1, decision 295 |
| 2.4 `via = 'explicit'` has no producer | **OPEN** — with 1.3 |
| 2.5 §6.3 is silent about the ends of a tier | **OPEN** — argued in `rank/drop.py` only |
| 2.6 Decision 11 adds a §5.3 job §5.3's table does not have | **OPEN** — job ships, table silent |
| 2.7 §13's re-ask stream has no owner on the Rank surface | **OPEN** — σ measured on §6.1's pairs |
| 2.8 The measured tier shape is on an unstated scale | **OPEN** — corpus project's call |

**2.1** is the one to read first: it is marked high severity in the record and nothing has changed.
No migration adds a tier-set-size column to `tier_edit` (checked across `0005_ledger.sql`,
`0010_ledger_fit.sql`, `0012_rank.sql`, `0018_read_layer.sql`, `0022_model_basis.sql`), so decision
11's guarantee that tier edits "survive the change" is still honoured in row count and violated in
meaning on a grow.

**2.6** is confirmed by the code rather than assumed: `worker.py:1045` registers
`Job("tier-set-refit", "M3", "tier-set change", "seconds", ...)` and §5.3's job table still has no
row for it. M3 named it out loud rather than smuggling it in, and the spec side is still owed.

### §3 Known defects — the tier model and the selector

| Heading | Verdict |
|---|---|
| 3.1 The boundary arm has no memory | **HALF CLOSED** — see below |
| 3.2 A boundary nobody straddles is never targeted | **OPEN** |
| 3.3 The exploration arm pairs two equally-uncertain titles | **OPEN** |
| 3.4 `model.straddle` prefers the downward reach unconditionally | **OPEN** |
| 3.5 The equal-mass re-initialisation is discarded by its own refit | **OPEN** |
| 3.6 Tension can be erased by time but never created by it | **OPEN** — confirmed below |
| 3.7 On a young board everything straddles | **CLOSED** — `straddle_z` is 0.15 |
| 3.8 The held-out agreement figure is a bare conditional accuracy | **OPEN** |
| 3.9 `equal_mass_quantiles` can put the whole board in the top tier | **OPEN** — low |
| 3.10 `model.straddle`'s two ends use different strictness | **OPEN** — low |

**3.1 is half closed and the halves are worth separating.** The *exploration* arm now takes an
`asked` set and never re-serves an answered pair — `backend/spielplan/rank/queue.py:206` cites this
finding by name, and `tonight/round.py`'s `select` takes the same set for the same reason. The
*boundary* arm's own repetition is untouched and `queue.py:274` says so explicitly: "The boundary
arm's own repetition is M3-open-points §3.1's remaining half and not this milestone's."

**3.6 is confirmed rather than inherited.** `rank/board.py:171` computes the tension band as
`item.s ± hp.tension_z() * item.sigma`, and `board.py:67` states what `item.sigma` is: "the
*displayed* σ — `ledger_state.sigma_eff`, which carries §5.2's freshness". So the overlap test still
reads a monotonically inflating σ and can only switch tension **off** with time. The badge's
credible-mass constant is now adopted (decision 295, proposal 71, `ledger/hyperparams.py:148`), which
makes the multiplier defensible and leaves the quantity it is applied to exactly as this finding
describes it.

**3.7 is closed by a constant, not by a rewrite.** The finding's arithmetic is about
`straddle_z = 1.0` against cutpoint gaps of 1.10-1.34; the shipped value is 0.15 and §6.3 now argues
it in place. The derived consequence the finding names — the 20% exploration arm falling through to
the full pool and mislabelling its draws — follows from the same constant and goes with it.

---

## 9. `M4-open-points.md`, with a verdict each

| Heading | Verdict |
|---|---|
| 1.1 The round is not adaptive in length. Should it be? | **ANSWERED** — option A, measured |
| 1.2 What ends a room nobody finished? | **CLOSED** — decision 169 |
| 2.1 §6.2 step 5's centring cancels | **OPEN** — checked, see below |
| 2.2 §6.2 never describes a pool that runs out of pairs | **PART CLOSED** — see below |
| 2.3 §6.2 step 8 forbids the table its own risk register requires | **OPEN** |
| 2.4 `DNA_MODEL.md` is not vendored | **OPEN** — see below |
| 3.1 A started room is never abandoned | **CLOSED** — with 1.2 |
| 3.2 The evaluation's agreement figure is thin by construction | **OPEN** |
| 3.3 The round's constants are unmeasured | **PART MEASURED** — see below |
| 4. What was measured | **SUPERSEDED** — see below |
| 5. Debt and waivers | **PART SUPERSEDED** — see below |
| 6.1 The hold-out pair is redrawn on every read of the round | **CLOSED** — decision 223 |
| 6.2 Two coverage rows describe things that do not exist | **CLOSED** — both rows repaired |
| 6.3 What neither audit read | **OPEN** — standing note |

**1.1 was answered as option A — leave the prior — and answered with a measurement rather than a
preference.** `backend/spielplan/tonight/round.py:233-247` records it: over the same 20 seeded rounds
`BOUNDARY_Z` is calibrated against, narrowing `prior_var` to 0.25 *reduces* convergence (0-1 in 20 at
z = 1.0; 17/13/16 down to 13/9/10 at z = 0.6 over pools of 12/20/40), because the boundary moves as
far as the intervals shrink. The threshold, not the prior, is the controlling variable. Decision 214
records the finding as refuted, and the docstring ends "do not narrow this without a new
measurement". §6.2 step 4 now reads "The round (adaptive length)".

**1.2 and 3.1 are one closure.** Decision 169 ships a host control in the lobby together with
`play.settle`, which moves a room whose seats have all ended; `rooms.py:422` argues why that is the
answer rather than the 6 h sweep the M4 plan announced. See §4.4 above — the sweep is also the
subject of a correction, because the plan recorded it as shipped.

**2.1 is open, and I checked it rather than assuming the §6.2 rewrite had swept it up.** The fold
rewrote step 4 around this sentence and left the sentence itself: §6.2 step 4 still reads "the tilt
is chosen-minus-rejected DNA **centred on the candidate-pool mean** (the measured centring lever)",
and an additive centring term cancels in a difference exactly, so as written it is still a no-op and
still cannot be the lever §0 row 4 measured. What decision 218 added is a different rule - a term the
candidate's own vector does not carry is skipped rather than centred against a zero it never had.
The code has known the answer the whole time and says so: `backend/spielplan/tonight/tilt.py:16-21`
states that "centred on the pool mean, read as subtraction alone, is a no-op" and that "centring on
the pool's mean **and scaling by the pool's own spread** is the reading under which every word of
§6.2 holds", and `Frame` at `:70-103` computes both a `mean` and a `spread`. **The spec sentence and
the shipped code still disagree, and under CLAUDE.md that makes the spec the bug.** Owed: one clause.

**2.2 is half closed, and the code says which half.** The deadlock is fixed - §6.2 step 4 now carries
decision 215's "a pool too small to ask anything is not refused ... a seat with no pair to serve ends
itself with its stop reason". What §2.2 actually asked for was narrower: name the terminal state, or
say the cap covers it. Neither sentence is in the spec. `round.py:1082-1089` still records an
exhausted pool as `CAP` under a comment ending "Reported as a v2.2 spec defect", and §6.2 still fixes
`ended_by` at three values without saying that one of them is doing double duty.

**2.4 is still open and decision 294 did not close it.** What was vendored is
`docs/ARCHITECTURE-extracts.md` — §3 and Appendix C of the corpus project's `ARCHITECTURE.md`, under
a provenance header. `DNA_MODEL.md` is **not** in this repository, and `docs/spielplan-spec_v2.1.md`
still cites it normatively in three places: §0's Group row and §6.2 step 5 both bind conflict copy to
"DNA_MODEL §5.3", and §6.3 initialises the tier shape from "DNA_MODEL §4.5's measured quantile
shape". The divergence `D` that step 5 thresholds at 0.20 therefore still cannot be checked against
its source. Decision 294's own argument — "a normative pointer nobody here can read is not
normative" — applies here unchanged and was spent on the other document.

**3.3 is part measured.** `BETA = 0.5` (`round.py:137`) and `GUEST_VAR_FACTOR = 4.0` (`:142`) are
unchanged and still undefended from the spec. `prior_var = 1.0` is no longer in that set: it was
measured at M4.12 (see 1.1) and the argument is written where the constant is. §14 risk 6 still says
nobody tunes the cap or the escape until winner satisfaction has been compared against the solo
baseline on a household's own evenings, and **that comparison has still not been made**.

**§4 "What was measured" is superseded as evidence, and the numbers themselves stand.** The
simulation table (convergence 0-2 in 60 at every pool size, median 20 pairs) was taken before
`straddle_z` and `BOUNDARY_Z` were retuned and before the fallback fix it names; M4.12's own sweep
over the shipped 696-title pool is the current reading. The section's last paragraph — "the fixture
library cannot show any of this ... it is a claim about a real library, and it needs one" — is
exactly what `ops/m412_exit_criterion.py` was built to answer, and §1 above records that its output
is not committed.

**§5 "Debt and waivers" is part superseded.** Its claim that "no backup job exists anywhere under
`ops/`" is the subject of correction §4.3 — the job shipped at M4.7. Its second waiver,
`library-rate-model-line-no-bundle`, **was retired inside this milestone and the retirement has
landed.** Its stated premise - "with no bundle there are no titles, so there is no card to open" -
is false as of this branch. `backend/tests/test_restore_drill.py` carries
`test_a_restored_install_with_no_model_bundle_says_so_on_the_title_card`, which restores content
without an artifact bundle, opens a card through the front door, and reads back
`available: false` with the reason `no artifact bundle imported`. The row now names that test
beside three others and carries no `waived` key at all, and the map says in place why the waiver
was wrong rather than merely stale. Nothing is owed here: this paragraph asked for an edit while
that edit was being made in the same change set, and it is corrected rather than left standing as
an instruction to redo work already done. The catalogue of weaker-than-they-looked test shapes in
the same section is superseded by nothing and remains the best short list in this repository of
what to look for.

**6.2 is closed and both halves were checked.** `tonight-rank-join-channels-equivalent`'s `what` no
longer claims a join link: it now reads "joining by room code, or from the open-rooms list", which is
the set §6.2 step 2 and the code both have. `tonight-rank-conflict-copy-bounded` was renamed
`tonight-rank-conflict-copy-bounds` and its clause 3 moved to
`tonight-rank-match-line-terms-are-carried`, whose `what` now records the owner's answer in place -
"the no-pull line names the term that works against them, so it cannot serve a participant no term
reaches (owner decision at the M4 merge)" - and both rows name tests that exist. The open point's
own condition was that the owner choose rather than that the code be edited to fit, and that is what
happened.
