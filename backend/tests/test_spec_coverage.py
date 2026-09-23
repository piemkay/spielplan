"""The coverage map is a contract, and this is what enforces it.

`spec_coverage.toml` names every testable requirement, the milestone that owes it a test, and
the tests that assert it. Five rules:

  1. Every requirement at or before `current_milestone` names at least one test.
  2. Every named test exists — in pytest, in the Playwright suite, or in the frontend's vitest.
  3. A pytest file `git ls-files` does not list holds no test this map can see.
  4. An `integration` row's tests reach the layer the row declares.
  5. A shipped row cites a section, a numbered decision or a document in this tree - and never a
     bare proposal. Both halves are asserted: the negative one alone left `spec = "TBD"` legal.

Rules 3-5 are M4.16's, and each closes a way a row could read as covered while asserting nothing
at the altitude it claims: a test only its author's working tree holds, a unit test of a pure
function standing in for a database, and a requirement whose authority is a document that
disclaims itself. Every rule is also fed its own violation by
`test_every_rule_this_gate_enforces_can_fail`, because none of them had a failure branch anybody
had exercised — a regex that stops matching or a counter reading the wrong field would have
printed a green map for ever, which is the defect one altitude up from the ones the rules catch.

Raising `current_milestone` therefore turns the next milestone's obligations into failures with
names, which is the whole point: a test plan nobody runs is a wish, and a coverage number
nobody reads is worse. A requirement that genuinely should not be tested yet carries an
explicit `waived = "reason"` and shows up in the report rather than disappearing.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from functools import cache
from itertools import zip_longest
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
MAP = TESTS / "spec_coverage.toml"
LEDGER = REPO / "docs" / "TESTING.md"
REGISTER = REPO / "docs" / "spec-v2.2-proposals.md"

# M4.5 is not in §12. It exists because §12's M0 importer was verified against a fixture
# that did not resemble the artifact it stands in for, and M5's pipeline cannot be built
# on a placement path that has never met the real feature contract. See
# docs/milestones/M4.5-plan.md.
#
# Nor were M4.9, M4.12 and M4.13 when their first rows landed, and for the same kind of
# reason: the September 2026
# pre-release review found three defects that only the real corpus bundle can express — a
# title card that throws on a keyed each, a fifth of the basis served at e(t) = 0, and a
# pair search that costs a minute. `docs/milestones/ROADMAP-to-M5.md`'s "Start here" table
# takes those three out of their milestones and ships them first, so the rows land here
# ahead of the milestones that own the rest. THE ORDER IS AUTHORED, NOT SORTED:
# `_at_or_before` uses `MILESTONES.index`, and a string sort would put "M4.10" before
# "M4.5". M4.16 was added by the milestone that opened it, in one
# commit with its first rows, and M5.1 added the seven names M5 ships as in the same
# shape and in one commit of its own; its entry closes this block.
#
# Nor was M4.6 in §12: its row was added to the table this week, together with the
# §3.1/§6.2/§6.5/§6.6 amendments that decisions 164 and 166 forced. §6.6 sketched the
# household's user management in one line and §12 scheduled it nowhere, so the only
# account-minting UI ever built is the first-boot wizard's member step — which becomes
# unreachable the moment an admin exists, and which decision 164 removes from §3.1's
# sequence in favour of §6.6's Users screen. It goes before M4.9 because it is a
# milestone rather than one of the three pre-release fixes that ship ahead of theirs.
# See docs/milestones/M4.6-plan.md.
#
# Nor is M4.7, which follows it and is the same shape: §2 promises required config,
# secrets custody with rotation, a nightly dump with rotation 14 and a restore, §5.3
# lists the jobs and §1 pins the image, and §12 scheduled none of it. Decision 181 adds
# the row. It sits between M4.6 and M4.9 because it ships before the release cut and
# because M4.9 through M4.13 consume two seams it owns — the app-level 409 handler and
# the `job_run` table. See docs/milestones/M4.7-plan.md.
#
# Nor is M4.8, and it is M4.5's shape rather than M4.6's and M4.7's: it ships no surface
# and no clause §12 scheduled, so it takes no row in that table and none was added. It is
# the instrument — the bundle fixture, this file's own layer, the two §4.1 landmine
# guards, the browser harness, CI's trigger and installer, and the three exit scripts —
# and it exists because each of those printed as covered while unable to fail: a fixture
# that cannot express the corpus's awkward shapes, a harness with no failure branch, a CI
# trigger that never fires on a milestone branch, and three exit scripts of which two
# cannot report their own failure. It goes after M4.7 and before M4.9 for one reason:
# M4.9 through M4.16 are measured by exactly these instruments, so a milestone that
# repairs them after they have been relied on proves nothing about the work that already
# passed through them. Decision 183 keeps the one job that needs the real corpus on a
# self-hosted runner and off every branch gate, so this milestone's own evidence stays
# reproducible rather than becoming a secret nobody can re-run. See
# docs/milestones/M4.8-plan.md.
#
# M4.9, which follows it, is the one entry above that has since GAINED a §12 row rather
# than staying outside the table: decisions 187-193 add it, because the milestone ships
# surface (the §6.0 credit count line and disclosure, the §6.6 Data card's outstanding
# authoring task, §6.7's rail on every screen) and because what it repairs is M0's own
# exit criterion, asserted for four milestones against a fixture in which the corpus's
# two department spellings, two facet namings and self-qualified term ids do not occur.
# It sits here — after M4.8, before M4.12 — for the reason M4.8's paragraph gives from the
# other side: every row below is measured by the instrument M4.8 repaired, and the three
# pre-release fixes already parked at M4.9 above are the same milestone's first three
# commits. See docs/milestones/M4.9-plan.md.
#
# M4.10 follows it and takes a §12 row too, on M4.9's argument rather than M4.6's and
# M4.7's: §12's M2 row owns the rating view, the Personal Ledger and §6.1's prediction
# reveal and §12's M3 row owns Rank's tiers and its comparison queue, and both were closed
# against surfaces that guard every Ledger write with a read, then some work, then a write,
# on a connection that autocommits each statement — asserted one request at a time, which
# is the only way they hold. Two gathered answers under one sealed pair write two `duel`
# rows in most races with no injected latency, one of those races drawing §13's held-out
# arm; two gathered Rate taps on one card token leave the loser holding a refusal §6.1 does
# not define — since M4.7's handler a 409 naming `rate_observation_seq`, not the plan's 500,
# which is no more actionable by the client — with its Jellyfin write already sent; and both
# Rank routes raise after their observation is durable, so every retry writes another
# append-only row. What it applies is the property `write_txn(lock=...)` (`api/deps.py:73-92`)
# exists for — the winner's write is serialised ahead of the loser's read, so the loser reads
# what the winner committed — at four Ledger seams, in the form each one can carry: the
# two-int `pg_advisory_xact_lock` that `tonight/play.py:611` uses, issued as the first
# statement inside the Rank answer's `write_txn(conn)` and not through that helper's own
# `lock=`, whose single-argument `hashtext(...)::bigint` form is a different lock space that
# could not collide with a number here (`api/deps.py:86-88` says so); a `SELECT ... FOR UPDATE`
# on `rate_session` as the first statement of every Rate write; an `AND card_token IS NULL` on the
# card stash; and a compare-and-set on the tier set a refit fitted against. The fit runs after the
# commit, never under the lock. It sits here, after M4.9, because its rows are measured by
# the instrument M4.8 repaired and read the layer M4.9 corrected: the reveal row is measured
# on the seed list M4.9's loader fills, and the two queue rows read the board its facet work
# renders. Decisions 199-208 record the calls it needed. See docs/milestones/M4.10-plan.md.
#
# M4.11 follows it and takes a §12 row on M4.9's and M4.10's argument rather than M4.6's and
# M4.7's: the row it repairs is one the table already had. §12's M1 row closes on "seen states
# flow both ways for both users", and that was asserted against one member, one copy per title,
# no series and no second phone — not the household §3.3 describes. Against two members, a
# library holding 'Movies' and 'Movies 4K' copies of one film, and a show whose next season
# lands: a link made without Jellyfin credentials aborts that member's sweep at its first owed
# row for the life of the install, so none of their history is ever adopted; a duplicate copy
# marked Played reverts the explicit "not seen" the person typed; a DELETE against a Series
# folder is a recursive MarkUnplayed that erases every episode's resume position, and the same
# computed folder flag un-marks a show the day it grows; a title deleted from the library keeps
# `is_owned = true` and stays in Tonight's pool; a revoked token is promoted back to "linked"
# by any sweep with nothing to push, while a 404 on every write reads exactly like a healthy
# one; and no Episode session arms a finish prompt at all, because the double emitted a shape
# no server sends. It sits after M4.10 rather than beside it because the one leak from this
# territory into the rest of the app — the Jellyfin round trip inside the verdict transaction —
# was M4.10's to move, and both milestones would otherwise edit `rate/session.py`'s push path
# in the same lines. Decisions 210-212 record the calls it needed; decision 172 had already
# ruled two of them. See docs/milestones/M4.11-plan.md.
#
# M4.12 follows it and is that shape a fourth time: it takes a §12 row (decision 224) on
# M4.9's, M4.10's and M4.11's argument rather than M4.6's and M4.7's, because what it repairs
# is the row this table has had the longest. §12's M4 criterion — "a real Friday night resolved
# by the app" — was closed against a six-title candidate pool, a household of one account and a
# guest seat no screen could take to the reveal. Against two real accounts and the shipped
# 696-title owned pool the same evening does not resolve: the pair search costs 36 s for a member
# and 126 s for a guest seat against the §6 preamble's 1.5 s per battle; the voting -> ballot
# transition exists only inside the answer handler, so one transient failure leaves the room in
# `voting` for ever with its data intact and unreachable; a pool of two or three candidates
# converges at zero answers and no route can close the room; a member who joins while the host is
# building the pool is seated into a snapshot that has no scores for them and deadlocks the same
# way; nothing ends a started room, so every other stall is permanent and recovery means SQL; two
# answers gathered on one card token are a 500 rather than a 409, and an undo interleaved with an
# answer makes every later tap a 500 for ever; the session WebSocket authenticates outside the
# dependency graph and awaits its sends inside an acquired connection, so ten stalled sockets pin
# the whole ten-connection pool; a room with any guest seat can never reach the reveal; and every
# `session_answer.latency_ms` ever written is 0, which is the instrument §14 risk 6 requires
# before anyone re-tunes the round. Its name was already in this list before it opened — three of
# its findings were pre-release fixes that ship ahead of the milestone owning the rest, which is
# what the M4.5 paragraph at the head of this block describes — so opening it moves
# `current_milestone` and adds no entry here. It sits before M4.13 because the two were built in
# parallel, in separate worktrees, and `ROADMAP-to-M5.md`'s table marks them workable in either
# order; the position is the one the pre-release rows already fixed. Decisions 214-226 record the
# calls it needed, and decisions 175 and 205 had already ruled the first of them. See
# docs/milestones/M4.12-plan.md.
#
# M4.13 is the last of that cluster, and what it became is not what the paragraph above parked at
# M4.9: its three pre-release commits shipped ahead of the milestone out of the same "Start here"
# table — the zeroed Backbone row (`65614ae`), the 54c pair search (`e87deed`) and β's printed
# optimum (`c4e74e6`, decision 167) — so what is left is the spine under them, a fit that records
# the basis it was computed in, where "basis" reads three ways and each is wrong in a way the other
# two cannot see: the bundle version nobody threads, the tier-set K no column records, and the
# coordinate a title is fitted at against the one it is served at. It takes no §12 row, for M4.5's
# and M4.8's reason rather than M4.9's: it ships no surface, its gate is its own exit script (six
# checks and one report, decision 240), and what it repairs is a row §12 already has — M2's, whose
# Personal Ledger was closed against a basis the app inferred from whichever `artifact_bundle` row
# happened to be active rather than one its caller threaded. Decisions 234-241 record the calls it
# needed, four of them refusals, and its one migration is 0022_model_basis.sql. It sits after M4.12
# because this list needs a total order and `ROADMAP-to-M5.md`'s table supplies one, NOT because
# either milestone depends on the other: that table marks the two workable in parallel in both
# directions, and they were built that way, in two worktrees on two branches.
# See docs/milestones/M4.13-plan.md.
#
# M4.14 follows it and takes a §12 row (decision 259) on M4.6's and M4.7's argument and
# M4.9's at once. §5.3 files "Bundle import validation + hot swap" as an admin-triggered job
# with a minutes budget, and it is not one: it is a 127-second `await` inside
# `POST /api/admin/bundle/import`, on the backend's event loop, inside one transaction, with
# the CPU-bound rebuild on the request loop — behind an ingress that cuts a proxied request at
# 100 s, so the operator is told the import failed while it completes and flips, and the retry
# hits decision 162's seed-once refusal and reads as corruption. §12 scheduled none of that,
# which is M4.6's and M4.7's shape. The other half is M4.9's: §12's M0 row owns "bundle
# importer + validation report" and its criterion is "bundle imports clean", closed against a
# fixture in which none of the real artifact's awkward shapes occur. The shipped `BUNDLE.json`
# carries 42 files with bytes and sha256, 68 exporter self-checks and 1,042,461,726 total
# bytes; the importer reads three keys and none of the hashes, so a zeroed `equating_map.json`
# validates clean and a truncated `reviews.sqlite` validates and then dies inside the import
# transaction as a 500 with the staged tree left behind; `_unpack` reuses a half-extracted tree
# it never checked; the `DATA_DIR` boundary is a string prefix under which `/database` passes
# for `/data`; referential integrity is never validated, so nine orphan variants validate clean
# and raise inside the transaction; the vocabulary version is derived four ways that disagree;
# and a models-only re-import — the only kind decision 162 says will arrive again — loads none
# of the four curated ledgers it carries while the validator reads two of them off that same
# bundle and discards them. It sits after M4.13 because this list needs a total order and
# `ROADMAP-to-M5.md`'s table supplies one, NOT because it depends on M4.13: that table marks
# M4.14 workable in parallel with M4.15, and the two are being built that way, in two worktrees
# on two branches. Decisions 247-266 and 287 record the calls it needed, and decision 171 had
# already ruled the first of them. Its one migration is 0023_import_state.sql (decision 250).
#
# THE ORDER IS AUTHORED, NOT SORTED is worth restating at this entry rather than only at the head
# of the block, because this is the entry that breaks a reader's arithmetic: "M4.14" is a later
# milestone than "M4.5" and sorts BEFORE it in every ordering a machine would reach for: `sorted()`
# compares '1' against '5' and `float`-ing the tail compares 4.14 against 4.5. `_at_or_before`
# asks `MILESTONES.index`, so the sequence here IS the schedule; an alphabetised or numerically
# "tidied" list silently re-dates every row in the map.
#
# The sibling lane adds "M4.15" after this entry, on its own branch, while this one is being
# written. Whichever merges second resolves the list to
# [..., "M4.13", "M4.14", "M4.15", "M5", ...] -- both names, in that order -- rather than taking
# one side of the conflict: the two are separate milestones that both shipped, and dropping either
# name makes every row of the lost milestone fail `test_every_requirement_is_well_formed` with an
# unknown milestone while `test_every_milestone_is_represented` says nothing, because the
# requirements are the side that went missing from MILESTONES rather than the other way round.
# See docs/milestones/M4.14-plan.md.
#
# M4.15 is not in §12 either, and it is M4.5's and M4.8's shape rather than M4.6's: it ships no
# new surface, so it takes no row in that table and none was added. It exists because §6's
# preamble — "responsive PWA, phone-first (48 px targets, one-handed, swipe), desktop as
# progressive enhancement, installable, service-worker shell cache" — is normative, is the only
# sentence in the document describing the box every surface sits inside, and has never had an
# owner: §12 schedules the screens and every surface milestone built its own correctly and left
# the chrome alone, so nobody owed the box a test: `06-responsive` was held by nothing in the map
# until M4.15's own two rows took it. The map's three `02-shell` ids were held by M0's
# session-cookie contract and M4.9's model rail, neither of them the preamble [review cycle 3:
# M415-C3-COV-01; re-scoped at M4.16, when raising `current_milestone` put those two rows outside
# the current-milestone exclusion that had been hiding them]. What the box turned out to contain
# is the milestone: an installed app whose header renders under the status bar and whose bottom tab
# bar renders under
# Safari's toolbar, form controls at 10-13 px that iOS Safari zooms on focus and does not zoom
# back, a 48 px token no menu entry and no overlay exit meets on its narrow axis, no outside-tap
# and no Escape on any menu or full-bleed panel, quiet reasons set in the data face §6.8 reserves
# for model numbers at an ink token measuring 2.82:1, one accent spent on six meanings across
# fifteen files, a stylesheet naming a generator nobody had written, and the one module that
# knows the wire holding no deadline, no 401 branch and no case for a pydantic 422. One item on
# that list did not survive being measured and is stated here as measured rather than as read:
# the plan counted three declared font weights whose src is the 400 file and concluded nothing
# had ever rendered bold, but both families ship a VARIABLE woff2, so the 500 and 700 faces are
# real instances of a wght axis and always were. What was actually missing is ops/fetch-fonts.py,
# which fonts.css:4 had named since M0 and which did not exist -- the drift, not the weights. See
# the row platform-shipped-font-weights-are-real, which carries the measurement. It sits after
# M4.13 and LAST among the pre-release frontend milestones because the surfaces had to stop
# moving before they were reskinned: M4.6's Users screen, M4.9's title card, M4.10's board and
# M4.12's round each rewrote one of the screens it reskins, and this milestone changes every
# surface's appearance at once. M4.14 is being built in parallel in a second worktree and inserts
# itself between M4.13 and this entry when the two branches merge, which is expected and is not
# this lane's to pre-empt. Decisions 267-286 record the calls it needed, and it writes no
# migration. See docs/milestones/M4.15-plan.md and §6's preamble.
#
# M4.16 is last, it is not in §12, and it is the only milestone in this list whose subject is
# this file. It ships no new screen - one block on an existing one, decision 293's /account Data
# sources notice - and what it repairs is the project's record of itself: a normative document
# describing a TV client, a wizard step, a key rotation and three Home Assistant seams the code
# does not have; fourteen shipped rows resting their authority on a document that disclaims
# itself; a report that hid exactly the partial waivers it was built to show; and rule 2, which
# asks whether a named test EXISTS and never whether it RAN. It runs LAST because its whole
# content is what shipped: arch-13's per-module SQL baseline was stale the moment M4.12 emptied
# `api/tonight.py`, and the release gate is defined over the spec and e2e inventory M4.12's TV
# deletion changed, so neither could be written green before every code milestone landed. It
# runs ALONE because it holds this file, the structural half of `spec_coverage.toml` - the
# waivers, the §12 table and `current_milestone` - and the two markdown documents every other
# milestone cites; eleven agents amending one normative file in parallel is the worst merge
# surface in this repository, and a conflicted paragraph in a spec is two readings of a
# requirement rather than a merge conflict. Its rows land with NO `tests` key, which is M4.10's
# and M4.11's opening rather than M4.15's: THE RED LIST IS THE TEST PLAN, closed by writing
# those tests, never by a waiver, never by renaming a registered test, and never by lowering
# `current_milestone`. Decisions 288-320 record the calls it needed (304-306 in review cycle 1,
# 307-309 in cycle 2, 310 in cycle 3, 311-318 in cycle 4), and it writes NO migration:
# 0024 is not claimed (decision 292 - decision 178's `rating_source` licence columns landed in
# 0018_read_layer.sql) and 0019 stays permanently unused. See docs/milestones/M4.16-plan.md.
#
# M5.1 opens the seven that M5 ships as, and it is the second entry here whose subject is this
# list. Section 12 gave M5 one row naming four subsystems, and its criterion - "a new Jellyfin
# add reaches 'ready' unattended" - can be closed while three of them are absent, so decision 321
# splits it into M5.1 through M5.7 and decision 331 amends that criterion in place rather than
# leaving the gap unstated. The seven names sit between "M4.16" and "M5" POSITIONALLY:
# `_at_or_before` asks `MILESTONES.index`, so the umbrella has to sort AFTER its parts, or a row
# filed at "M5" reads as shipped while one of its parts is still current. "M5" stays in the list
# for exactly one row, the umbrella criterion decision 331 amends, and decision 321's last clause
# is what forces that row to exist at all: re-pointing all eleven pre-written `milestone = "M5"`
# rows at the sub-milestone that owns each leaves the name holding zero rows, and
# `test_every_milestone_is_represented` goes red on a name with none. The seven names,
# `current_milestone = "M5.1"` and this milestone's first rows landed in ONE commit, which is the
# M4.6 rule stated from the other side: a milestone in one and not the other fails
# `covered != set(MILESTONES)` for every lane at once, and six other lanes are blocked on this one.
#
# M5.1 itself is the part of M5 with no product of its own. Section 8's preamble calls the raw
# store, the durable `(kind,key)` queue and the per-host rate-limited HTTP layer "ported
# skeleton", and this tree carried none of the three when the milestone opened - nothing named
# `raw_document`, `job_run` was an outcome log with no `(kind,key)` identity, no attempts, no
# lease and no scheduled retry, and the only outbound HTTP was a bare client per call with no
# rate limit, no backoff and no breaker - while two stages of section 8 are already built and
# have never had a caller: `placement/reconcile.py`'s `app_acquired` scope, which appears
# nowhere outside its own file, and the "New in the library" shelf, which has no producer
# because nothing in the tree stamps `title.origin = 'acquired'`. It runs first and alone
# because every other M5 lane plugs into the seams it publishes - the stage contract, the queue,
# the raw store - and because it holds this file, the structural half of `spec_coverage.toml`
# and section 12's table, which no second milestone may hold at the same time. Two of the eleven
# re-pointed rows cite bare proposals and GO ON citing them: decision 295's rule is scoped to
# `current_milestone`, M5.5 and M5.6 sort after M5.1, and re-pointing is therefore what keeps
# those two out of its scope until decision 330 settles proposals 104, 107, 109 and 135 by
# number. Renaming `proposals` to `decisions` is the repair `_laundered_decision_citations`
# exists to catch. Decisions 321-361 record the calls it needed - 321, 322, 323 and 331 gate its
# first commit and its migration, and 332, 336, 340 and 345 are the four whose answers its code
# would otherwise have had to guess; 347, 348 and 349 were taken as it closed, over the drain's
# per-tick bound, a paid stage's refusal to run uncapped, and the archive's exclusion of the
# spine's three tables. Its one migration is 0024_acquisition.sql.
# See docs/milestones/M5.1-plan.md and ROADMAP-M5.md.
#
# M5.4 opened second, in one wave with M5.2 and M5.3, and it adds no name to the list: M5.1 landed all seven
# positionally in one commit, so what this entry owes a reader is why "M5.4" sits where it already
# does and what is now behind it. It sits BEFORE "M5.5" because stage 7 is the trust boundary that
# judges what stage 6 returns - §9's "the schema is a cost-saving device, not the guarantee, the
# guarantee is the validator" - so the milestone that writes the validator has to be readable as
# shipped before the milestone that first calls a provider, and M5.5 is written against what this
# one publishes. It sits AFTER "M5.3" and depends on it barely: the install already holds the
# bundle's review bodies and per-source `title_meta`, so the pack this milestone builds has
# something to be built from before stage 2's fetchers exist, which is what makes the three lanes
# of this wave parallel rather than notionally so. It does NOT hold `current_milestone`; M5.1 does,
# and its five rows are appended without raising the scalar, which is the M4.6 rule read from the
# other side - a scalar raised in a lane that is not holding it reddens every other lane at once.
# Three of the five are new and land with no `tests` key, which is M4.10's, M4.11's and M4.16's
# opening: the red list is the test plan, closed by writing those tests and never by a waiver, by
# renaming a registered test, or by lowering the scalar. The other two are the pair M5.1 re-pointed
# here - the verify trust boundary and the per-title projection budget - and the second of them
# stays measured against the exported callable rather than against a `Job` registration this
# milestone deliberately does not make (decision 387). Decisions 341 and 382-403 record the calls
# it needed, and its one migration is 0027_dna_extraction.sql.
# See docs/milestones/M5.4-plan.md and ROADMAP-M5.md.
MILESTONES = ["M0", "M1", "M2", "M3", "M4", "M4.5", "M4.6", "M4.7", "M4.8", "M4.9", "M4.10",
              "M4.11", "M4.12", "M4.13", "M4.14", "M4.15", "M4.16", "M5.1", "M5.2", "M5.3",
              "M5.4", "M5.5", "M5.6", "M5.7", "M5", "M6", "M7"]
KINDS = {"backend", "integration", "e2e", "static"}
# No `frontend` kind, and its absence is a decision rather than an oversight: adding one is an
# owner scope call and not an instrument repair (M4.16-plan.md Phase F 7), and decision 226
# already settles what a vitest id may do -- stand BESIDE a backend or Playwright test, never
# instead of one, which `test_no_requirement_rests_on_a_vitest_id_alone` holds. A kind is the
# layer rule 4 measures a row's evidence against, and there is no layer a vitest id could be
# measured at that `npm --prefix e2e run fresh` would then run. docs/TESTING.md says so in prose.


def load() -> dict:
    return tomllib.loads(MAP.read_text(encoding="utf-8"))


COVERAGE = load()
REQUIREMENTS = COVERAGE["requirement"]
CURRENT = COVERAGE["current_milestone"]


# ASCII, and stated once: this is the message a developer meets when the gate refuses to answer,
# and it prints to whatever console they have.
_NO_GIT = (
    "the coverage gate cannot reach `git ls-files` in {repo}, so it cannot tell a committed "
    "test from one only this working tree holds, and rule 3 has no fallback: globbing the tree "
    "is the hole the rule closes. Run it inside the checkout with git on PATH. ({detail})"
)


def _tracked_files(repo: Path = REPO) -> frozenset[str]:
    """Every path `git ls-files` lists, repo-relative and posix — or a hard failure.

    Rule 3. `git ls-files` reads the INDEX, so a file is visible here the moment it is staged
    and not a commit later; the cost of a new test file counting is `git add`, which is what
    "this test exists for everyone" means in a repository.

    No fallback, and that is the point rather than an omission. The review that opened this
    milestone measured 1305 known ids of which 69 came from reviewers' untracked trees: every
    one of those closed a shipped row on the machine that wrote it and on no other machine in
    the world, and the map said so to nobody. A gate that quietly widens to the working tree
    when its instrument is missing reports a coverage number nobody can reproduce — the same
    shape as the report that hid its own waivers and as the exit script whose eighteenth check
    was `check(True, ...)`. So this raises rather than degrades, and it raises at import,
    because every rule below reads the set it returns. [M4.16, test-06 (1)]
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # no git, or too slow to answer
        raise RuntimeError(_NO_GIT.format(repo=repo, detail=exc)) from exc
    if listed.returncode != 0:
        raise RuntimeError(_NO_GIT.format(repo=repo, detail=listed.stderr.strip() or "no output"))
    return frozenset(line.strip() for line in listed.stdout.splitlines() if line.strip())


def _pytest_ids(tracked: frozenset[str], root: Path = TESTS, repo: Path = REPO) -> set[str]:
    """`path::name` for every test function in a TRACKED file of the backend suite.

    Parsed rather than collected: importing pytest's collector from inside a test run is
    fragile, and a regex over `def test_*` cannot itself fail in a way that hides a gap — a
    missed function shows up as a *missing* test, which fails loudly.

    `root` and `repo` are the self-test's seam, in the idiom `test_static_contracts.py` uses for
    the same purpose (`_invented_paddings(root)`): a rule is worth nothing until something has
    watched it refuse, and the only honest way to watch this one refuse is to build an untracked
    file for it.

    Only the pytest reader intersects. `_playwright_ids` and `_vitest_ids` do not, because the
    row that states this rule states it of `_pytest_ids`, and widening a guard past the sentence
    the map holds it to is an amendment to that row rather than an implementation of it. The
    same hole stands open for a spec file nobody staged; it is disclosed here rather than
    quietly half-closed.
    """
    ids: set[str] = set()
    for path in root.rglob("test_*.py"):
        rel = path.relative_to(repo).as_posix()
        if rel not in tracked:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_\w+)", text, re.M):
            ids.add(f"{rel}::{match.group(1)}")
    return ids


def _playwright_ids() -> set[str]:
    """`path::title` for every Playwright test."""
    ids: set[str] = set()
    specs = REPO / "e2e" / "specs"
    if not specs.is_dir():
        return ids
    for path in specs.glob("*.spec.js"):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*test\(\s*(['\"`])(.+?)\1", text, re.M | re.S):
            ids.add(f"{rel}::{match.group(2)}")
        # `for (…) { test(\`… ${x} …\`) }` — template titles cannot be matched literally, so a
        # file that uses them registers a wildcard the map can point at.
        if re.search(r"^\s*test\(\s*`[^`]*\$\{", text, re.M):
            ids.add(f"{rel}::*")
    return ids


def _vitest_ids() -> set[str]:
    """`path::title` for every vitest test in the frontend.

    The third runner, and until M4.9 the map could not name one. §6.7's rail is where that
    stopped being a formatting detail: the drawer must drop its log on close, the frame in which
    it would show the previous open's events is one round trip long, and Playwright cannot hold a
    response the app's service worker mediates -- so the only layer that can assert the clause is
    a mounted component, and a row pointing at it would have failed rule 2 for naming a test that
    "does not exist". A map that can only see two of the three suites pushes every claim it
    cannot name either into a suite that cannot fail on it or out of the map. [M4.9 finding 27]

    Single and double quotes only. Playwright's reader admits a backtick and covers the template
    titles that follow with a `::*` wildcard; nothing here writes one, and admitting the
    character without that fallback would register ids no runner answers to.
    """
    ids: set[str] = set()
    src = REPO / "frontend" / "src"
    if not src.is_dir():
        return ids
    for path in sorted(src.rglob("*.test.js")):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:it|test)\(\s*(['\"])(.+?)\1", text, re.M | re.S):
            ids.add(f"{rel}::{match.group(2)}")
    return ids


# One `git ls-files` per session: it is a process, and rule 3 asks the same question of every
# pytest id in the map.
TRACKED = _tracked_files()

# Bound rather than folded straight into the union: decision 226's guard below has to ask
# which RUNNER answers to a name, and a path prefix would be a guess about that where this
# reader is the thing that decides it.
VITEST_IDS = _vitest_ids()
KNOWN_TESTS = _pytest_ids(TRACKED) | _playwright_ids() | VITEST_IDS


def _at_or_before(milestone: str) -> bool:
    return MILESTONES.index(milestone) <= MILESTONES.index(CURRENT)


# --- the map itself is well-formed ----------------------------------------------------


def test_current_milestone_is_a_real_milestone():
    assert CURRENT in MILESTONES


def test_requirement_ids_are_unique():
    ids = [r["id"] for r in REQUIREMENTS]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate requirement ids: {duplicates}"


def _malformed(rows: list[dict]) -> list[str]:
    """Every field-level offence in `rows`.

    Split out of the test below for the reason every rule in this file now is: a rule nothing
    has watched fail is a rule nobody knows still works, and the only way to watch this one fail
    is to hand it a row that breaks it. [M4.16, test-06 (4)]
    """
    problems = []
    for r in rows:
        for field in ("id", "spec", "milestone", "kind", "what", "why"):
            if not r.get(field):
                problems.append(f"{r.get('id', '?')}: missing {field}")
        if r.get("milestone") not in MILESTONES:
            problems.append(f"{r['id']}: unknown milestone {r.get('milestone')!r}")
        if r.get("kind") not in KINDS:
            problems.append(f"{r['id']}: unknown kind {r.get('kind')!r}")
        if len(r.get("what", "")) < 30:
            problems.append(f"{r['id']}: `what` is too vague to be a test")
    return problems


def test_every_requirement_is_well_formed():
    problems = _malformed(REQUIREMENTS)
    assert not problems, "\n".join(problems)


def test_every_milestone_is_represented():
    """A milestone with no requirements has no exit criterion anyone can check."""
    covered = {r["milestone"] for r in REQUIREMENTS}
    assert covered == set(MILESTONES), f"no requirements for: {sorted(set(MILESTONES) - covered)}"


# --- rule 2: named tests must exist ---------------------------------------------------


def _missing_tests(rows: list[dict], known: set[str]) -> list[str]:
    """Rule 2, over any map and any inventory of what the runners answer to.

    The file-level wildcard is honoured for an id the MAP writes as `path::*` and for nothing
    else, which is a narrowing: the old reading accepted any literal title under a file that
    happened to register a wildcard, and the only file that registers one is
    `e2e/specs/05-milestones.spec.js` -- the file whose whole purpose is to fail when a surface
    ships. So a row naming a title that file no longer holds printed as covered, certified by the
    parameterised loop at its head: the deletion `05-milestones` exists to refuse, wearing the
    map's own signature. The wildcard itself stays: that loop's titles are template literals and
    nothing can match them literally. Zero rows relied on the widened form -- the one row naming a
    title in that file names one the file really has -- so the narrowing costs nothing today, and
    it is free only today. [M4.16, ds11 (b)]
    """
    return [
        f"{r['id']} names a test that does not exist: {test_id}"
        for r in rows
        for test_id in r.get("tests", [])
        if test_id not in known
    ]


def test_every_named_test_exists():
    missing = _missing_tests(REQUIREMENTS, KNOWN_TESTS)
    assert not missing, "\n".join(
        [
            *missing,
            "",
            "A pytest id `git ls-files` does not list is one of these: the file is not staged, "
            "so the test closes a row on this machine and on no other (rule 3). `git add` it, or "
            "point the row at a test the rest of the household can run.",
        ]
    )


def test_no_requirement_rests_on_a_vitest_id_alone():
    """Decision 226: "A row may name a vitest id, and no row may rest on one alone."

    Rule 2 asks whether a named test exists. It cannot ask which runner answers to the name,
    because `KNOWN_TESTS` is one flat union of the three -- so the half of decision 226 that does
    the work was a sentence in a decision and nothing else. Naming a vitest id *instead* of a
    Playwright or a backend one "would rest a clause on a suite `npm --prefix e2e run fresh`
    never runs": the two cheapest assertions in a frontend repair close the requirement, the gate
    prints the row as covered, and the browser suite a milestone's exit criterion is measured on
    has never executed the claim.

    The same family as the defect this milestone found in rule 2 itself -- a `pytest.skip` inside
    a registered test closed a row while asserting nothing -- and repaired at the three sites it
    had reached rather than at the rule. A rule with no guard is a convention, and a convention is
    what a coverage map exists to replace.

    Unscoped, although decision 226's cost paragraph states the rule "is stated here rather than
    applied retroactively to milestones that closed under the older reading": no row of any closed
    milestone rests on a vitest id, so the retroactive half forgives nothing today, and a
    milestone cut-off would be a second thing to keep true for a guard that has never had
    anything to forgive.
    [M4.12 review cycle 1: M412-D8-04]
    """
    resting = [
        f"{r['id']} rests on vitest ids alone: {', '.join(r['tests'])}"
        for r in REQUIREMENTS
        if r.get("tests") and all(t in VITEST_IDS for t in r["tests"])
    ]
    assert not resting, "\n".join(
        [
            *resting,
            "",
            "Decision 226 admits a vitest id BESIDE a Playwright or a backend test and never "
            "instead of one: vitest mounts a component against fixtures, and `npm --prefix e2e "
            "run fresh` -- the suite a milestone closes on -- does not run it at all.",
        ]
    )


# --- rule 1: shipped milestones owe their tests ---------------------------------------


def _uncovered(rows: list[dict]) -> list[dict]:
    """Rule 1: a shipped requirement with neither a test nor a waiver."""
    return [
        r
        for r in rows
        if _at_or_before(r["milestone"]) and not r.get("tests") and not r.get("waived")
    ]


def test_shipped_requirements_are_covered():
    owed = _uncovered(REQUIREMENTS)
    if owed:
        lines = [
            f"{len(owed)} requirement(s) at or before {CURRENT} have no test.",
            "Write one, or add `waived = \"why not\"` to the row and say so out loud.",
            "",
        ]
        lines += [f"  [{r['milestone']} {r['kind']:11}] {r['id']}\n      {r['what'][:110]}" for r in owed]
        pytest.fail("\n".join(lines))


def _unexplained_waivers(rows: list[dict]) -> list[str]:
    """A waiver is a sentence the next reader has to be able to disprove, or it is a shrug."""
    return [r["id"] for r in rows if r.get("waived") and len(str(r["waived"])) < 20]


def test_waivers_are_explained():
    """Rule 7, both halves: a waiver states a reason, and the reason does not lean on a test
    this map registers nowhere. The second half is not a nicety -- the map's one standing waiver
    names the two tests that assert the half it does NOT waive, and while they were named in
    prose alone a rename left the waiver asserting a coverage nothing held.
    [M4.16 cycle 2, M416-C2-COV-03]"""
    bad = _unexplained_waivers(REQUIREMENTS)
    assert not bad, f"a waiver needs a real reason: {bad}"
    leaning = _waivers_leaning_on_unregistered_tests(REQUIREMENTS)
    assert not leaning, "\n".join(
        [
            *leaning,
            "",
            "Rule 2 reads `tests` and nothing else, so a test named only inside a waiver is a "
            "test the next rename deletes in silence -- with the waiver still claiming the half "
            "it does not waive is covered. Register it in the row's `tests`; a waived row that "
            "also names tests still prints as waived.",
        ]
    )

# --- rule 4: a row is proven at the layer it declares ----------------------------------

# The fixtures that reach Postgres, and there are exactly four: `pg_url` builds the database,
# `db` opens the connection, `app` builds the application against it and `app_client` drives
# that app (conftest.py). Nothing else in this suite touches the server, so a test whose fixture
# closure misses all four asserted against imports and temporary files, whatever its row said.
DB_FIXTURES = frozenset({"db", "app", "app_client", "pg_url"})

# The rows genuinely proven below their declared kind. MEASURED at M4.16 on 2026-09-17 over the
# whole map rather than copied from the plan, which named three: the other two have since gained
# tests that reach the database -- `platform-key-rotation-semantics` through decision 289's
# `test_secrets_custody.py`, which takes `db` and `pg_url`, and
# `data-rules-validation-reports-rather-than-raises` through `test_dna_import.py`. Each entry is
# dated and each is held to its own premise by `test_the_unit_only_allow_list_is_not_stale`: an
# entry whose row has since gained a DB test FAILS until the entry is removed, which is what
# makes this an allow-list rather than a waiver. A waiver can outlive its premise; this cannot.
UNIT_ONLY_ROWS = {
    "data-rules-model-artifacts-load-from-the-shipped-bundle": (
        "2026-09-17: every named test reads the real bundle off disk and asserts what loads out of "
        "it. The clause is about an artifact's shape and there is no row in any table that could "
        "carry it, so `kind` names the thing it integrates against -- CORPUS_BUNDLE_DIR -- rather "
        "than Postgres, and no database assertion is owed."
    ),
}


def _module_parameters(path: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(tests, fixtures) for one module: the parameters every function of each kind asks for.

    Read with `ast` rather than by collecting: pytest resolves fixtures at run time and the
    question here is what the SOURCE asks for, which is the only form an auditor can check
    against a row. `@pytest.mark.usefixtures("db")` is read too, because it asks for a fixture
    without naming a parameter and a reader counting only parameters would call such a test
    unit-only -- the false negative that would make this rule worth disabling.
    """
    tests: dict[str, list[str]] = {}
    fixtures: dict[str, list[str]] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
        for decorator in node.decorator_list:
            head = ast.unparse(decorator).split("(", 1)[0]
            if head.rpartition(".")[2] == "fixture":
                fixtures[node.name] = parameters
            if head.endswith("usefixtures") and isinstance(decorator, ast.Call):
                parameters += [a.value for a in decorator.args if isinstance(a, ast.Constant)]
        if node.name.startswith("test_"):
            tests[node.name] = parameters
    return tests, fixtures


def _rows_below_their_kind(
    rows: list[dict],
    allowed=UNIT_ONLY_ROWS,
    repo: Path = REPO,
    conftest: Path = TESTS / "conftest.py",
) -> dict[str, str]:
    """Every row whose named tests never reach the layer its `kind` declares, by id.

    `kind` was enforced as membership of four strings and nothing else, so a row could declare
    the layer its `what` names and be closed by a unit test of a pure function -- which is the
    map reporting a layer the evidence never reached, one indirection away from the waiver the
    report used to hide. `docs/TESTING.md` has said "write the tests first, at the `kind` the
    row names" since M3, and that sentence had no enforcement at all.

    An e2e test satisfies an `integration` row: e2e is ABOVE integration -- it drives the
    running application through a browser, database included -- so a row named by one is proven
    at more than it claimed rather than less.

    AND `e2e` IS HELD TOO, which is the half the rule shipped without. `backend` and `static` are
    the floor and nothing can sit below them; `integration` was pointed at; `e2e` is the ceiling
    and the one kind whose `what` names a SURFACE, and it was the kind neither instrument read --
    `ops/coverage_gate.py` has no notion of `kind` at all and hands that half to this rule by
    name. So a row declaring `e2e` over a screen, closed by one backend route test, read as
    covered at the highest layer the map can express with no browser having opened, which is the
    shape this function exists to refuse one kind above where it was aimed. The mirror of the
    exemption three lines up: an `e2e` row has to name at least one `e2e/specs/` id. Measured on
    this tree over all 34 `e2e` rows, every one of them does, so the rule lands green and asserts
    a property the map already has. [docs/TESTING.md M3: "a row whose `what` names a surface has
    to be tested through that surface"; M4.16-plan.md:165-167; M4.16 cycle 2, M416-C2-KIND-01]

    A row whose named tests cannot be resolved at all is left to rule 2, which owns that
    failure: two failures for one cause is one of them mis-diagnosed, and the argument is the
    one `_registered_tests_that_skip` makes below for the same reason.

    Unscoped by milestone, deliberately. The other rules ask what a SHIPPED row owes; this one
    asks whether the evidence a row names is what the row says it is, and that is as true of a
    row registered early as of one closed years ago.
    """
    modules: dict[Path, tuple[dict[str, list[str]], dict[str, list[str]]]] = {}

    def parameters(path: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        if path not in modules:
            modules[path] = _module_parameters(path)
        return modules[path]

    _, shared = parameters(conftest)

    def closure(path: Path, names: list[str]) -> set[str]:
        """The fixtures a test pulls in transitively, across its own module and conftest.py.

        A test asks for `app_client`, which asks for `app`, which asks for `db`, which asks for
        `pg_url`: only the last of those is a database by name, so a reader that stopped at the
        parameter list would call every route test in this suite a unit test.
        """
        _, local = parameters(path)
        seen: set[str] = set()
        pending = list(names)
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            pending += local.get(name, shared.get(name, []))
        return seen

    offenders: dict[str, str] = {}
    for r in rows:
        if not r.get("tests"):
            continue
        if r.get("kind") == "e2e":
            if any(t.startswith("e2e/specs/") for t in r["tests"]) or r["id"] in allowed:
                continue
            offenders[r["id"]] = (
                f"{r['milestone']} row, kind = e2e, and not one of its named tests is a browser "
                f"test: {', '.join(sorted(r['tests']))}"
            )
            continue
        if r.get("kind") != "integration":
            continue
        if any(t.startswith("e2e/specs/") for t in r["tests"]):
            continue
        resolved, reached = 0, False
        for test_id in r["tests"]:
            path, _, name = test_id.partition("::")
            source = repo / path
            if not path.startswith("backend/tests/") or not source.is_file():
                continue
            tests, _fixtures = parameters(source)
            if name not in tests:
                continue
            resolved += 1
            reached = reached or bool(closure(source, tests[name]) & DB_FIXTURES)
        if reached or not resolved or r["id"] in allowed:
            continue
        offenders[r["id"]] = (
            f"{r['milestone']} row, kind = integration, and not one of its {resolved} resolvable "
            f"tests reaches db, app, app_client or pg_url: {', '.join(sorted(r['tests']))}"
        )
    return offenders


def test_the_gate_refuses_to_answer_when_git_cannot_be_reached(tmp_path):
    """Rule 3's second clause, and the one a fallback would have made invisible.

    The tempting shape is `except: return everything` -- the gate keeps working on a machine
    with no git, and silently answers a different question on it. That is the failure this
    milestone is made of, one instrument down: an exit script whose eighteenth check was
    `check(True, ...)` still printed `18/18`. So the refusal is asserted rather than assumed,
    against a directory that is not a checkout, which is the only way to reach the branch
    without uninstalling git.
    """
    with pytest.raises(RuntimeError) as refusal:
        _tracked_files(tmp_path)
    message = str(refusal.value)
    assert "git ls-files" in message and "no fallback" in message, message
    assert message.isascii(), f"the refusal must print on a cp1252 console: {message}"


def test_every_row_is_tested_at_or_above_its_kind():
    """Rule 4. What `kind` claims about the evidence has to be true of the evidence.

    The measurement that opened this milestone found eight rows declared `integration` whose
    named tests never reach Postgres, five of them also named by e2e tests -- above integration,
    so no work -- and three backed only by unit tests of pure functions. Two of the three have
    since been repaired by tests rather than by forgiveness, which is the shape this rule is for:
    the allow-list is the exception a repair empties, not a second waiver register.
    [M4.16, ds02, test-06 (3)]

    Both non-vacuous kinds, since review cycle 2. The rule shipped reading `integration` alone
    while its own name said "every row", and `e2e` is the kind a `what` naming a screen declares
    -- so a row could claim the browser and be closed by a route test with nothing in this project
    able to say otherwise. [M4.16 cycle 2, M416-C2-KIND-01]
    """
    offenders = _rows_below_their_kind(REQUIREMENTS)
    assert not offenders, "\n".join(
        [
            f"{len(offenders)} row(s) declare a layer their evidence never reaches:",
            *(f"  {row_id}: {why}" for row_id, why in sorted(offenders.items())),
            "",
            "Write the assertion at the layer the row names -- an `e2e` row is closed through a "
            "browser and an `integration` row through the database -- or, if the clause genuinely "
            "has no such half, add the id to UNIT_ONLY_ROWS with the date and the argument. "
            "Do not change `kind` to fit the tests: the kind is what the row claims.",
        ]
    )


def test_the_unit_only_allow_list_is_not_stale():
    """The other half, and the half that makes an allow-list different from a waiver.

    An entry forgives a row for asserting below its declared layer. The day that row gains a
    test which does reach the database, the entry stops describing anything and starts being a
    standing permission nobody re-read -- exactly the shape of the waiver M4.16 retired, whose
    premise ("with no bundle there are no titles") was true of a fresh install and false of a
    restored one. So an entry is held to its own absence: it must still be an offence with the
    list emptied, and a repair deletes the entry rather than outliving it.
    """
    offences = _rows_below_their_kind(REQUIREMENTS, allowed={})
    stale = sorted(set(UNIT_ONLY_ROWS) - set(offences))
    assert not stale, (
        "UNIT_ONLY_ROWS forgives rows that no longer need forgiving, so the list is now an "
        "allowance rather than a record: " + ", ".join(stale) + ". Either the row gained a test "
        "that reaches the database, or its kind changed, or the id is gone. Delete the entry."
    )


# --- rule 5: authority is a section or a numbered decision ------------------------------

# A proposal number cited as AUTHORITY, and two spellings in this map are not that. `decision 295
# (proposal 71)` carries the number as PROVENANCE behind the decision that adopted it, which is
# the form decision 295 itself prescribes for the rows it re-cited; and `proposals 1-161` is a
# RANGE, a statement about the register's own structure rather than a citation -- nobody rests one
# requirement on a hundred and sixty-one proposals. Both are stripped before the search, so the
# rule catches what it is about: a bare number standing where a section or a decision should be.
#
# THE PARENTHETICAL HAS TO BE THE DECISION'S OWN, which is what `\s*` says and `[^()]*` did not.
# That gap crossed clause boundaries, so one citation deleted every character up to the next
# parenthetical anywhere in the field: `decision 288 retired the header; drag-and-drop (proposal
# 71)` stripped to a single space and the proposal in the second clause was never read. Measured
# over this map, adjacency strips six rows where the gap stripped seven, and the seventh is
# `library-rate-undo-block-depth`, whose whole field went -- section citation and all -- because
# the decision it cites carries no parenthetical of its own and the next one belonged to the
# section. No row's verdict moves. [M4.16 cycle 1, M416-C1-COV-02]
#
# AND THE NUMBER HAS TO BE A DECISION THAT ADOPTED THE PROPOSAL. Cycle 1 read this sentence as one
# repair and shipped half of it: `_is_a_decision` refuses a number below 162 and admits every
# number above it without opening the entry, so `decision 1 (proposal 71)` was caught while
# `decision 295 (proposal 999)` -- named in the same breath as the other spelling the rule is
# about -- was still deleted unread. A proposal nobody adopted laundered by attaching it to a
# decision that exists, in the milestone whose subject is a record claiming more than it holds,
# with this comment telling the next author the door was shut. [M4.16 cycle 2, M416-C2-COV-01]
#
# So the strip is gated on SUBSTANCE rather than on the number's range: the parenthetical is
# deleted only where the register's own entry for that decision cites every proposal inside it,
# read with the same reader that reads the map -- the entry has to name the proposal the way a row
# does. Measured over this tree: entry 295's prose names 71, 74, 75, 76 and 157, which is every
# number the three live `decision 295 (...)` citations carry, so the rule lands green on the map
# as it stands. Where the register heads NO entry -- 168-178 and 228-233 were taken in
# `docs/milestones/ROADMAP-to-M5.md` -- the adoption cannot be read at all, and the row is named
# out loud rather than forgiven in silence, which is the direction `_BARE_PROPOSAL`'s own comment
# argues for eight lines down. No live row spells that today; the first that wants to moves its
# provenance into `why`, or the owner writes the entry.
# [decision 295's second half, M4.16-plan.md:542-544; M4.16 cycle 1, M416-C1-COV-01]
#
# AND THE HEAD IS A LIST, since review cycle 5, for the reason cycle 2 gave one rule over. This map
# legitimately writes multi-decision citations -- `decisions 164, 165, 288`, `decisions 175 and
# 205`, enumerated in `_DECISION_CITATION`'s own comment -- and it legitimately writes provenance,
# `decision 295 (proposal 71)`. The two could not be combined: the strip required a SINGLE number
# immediately in front of the parenthetical, so `decisions 295 and 311 (proposals 107, 109)` did
# not strip and the row was reported as resting on a bare proposal it cites through an adopting
# decision. The author who is stopped by that is told to write the thing they wrote, and the cheap
# repair from there is to widen the gap back to `[^()]*` -- cycle 1's M416-C1-COV-02 defect
# reopened, which is this file's own recorded worry that "a rule like that is narrowed by the first
# person it stops". The separators are `_DECISION_CITATION`'s, so the two halves of one rule agree
# about what a list is; the adjacency cycle 1 bought is kept, because the list still has to stand
# immediately in front of the parenthetical. ANY of the cited decisions adopting it is enough --
# the parenthetical is carried if one of them carries it, and a number that adopted nothing adds
# no licence. No live row spells this today, so the rule lands green on the map as it stands.
# [M4.16 cycle 5, M416-C4-COV-09]
_CARRIED_BY_A_DECISION = re.compile(
    r"\bdecisions?\s+(\d+(?:\s*(?:,|\+|and)\s*\d+)*)\s*\(([^)]*)\)", re.I
)
# The hyphen and only the hyphen, which is how every range in this map is written. An en dash in
# this pattern would be non-ASCII inside a string a failure message prints, and
# `test_static_contracts.py::test_no_console_output_leaves_the_oem_code_page` refuses that for the
# reason CLAUDE.md gives -- it caught this line and the line was repaired rather than the guard.
# The cost is a false POSITIVE if a range is ever written with an en dash, which is the safe
# direction: the rule then names a row out loud instead of forgiving one in silence.
#
# `\b` AFTER THE CAPTURE, because without it the exemption held only for a range whose first
# number is one digit long. `(\d+)` gave digits back until the lookahead was satisfied, so
# `proposals 104-109` matched "10" and the rule reported a row citing "proposal 10" -- a number
# nobody wrote, sending the next author to look up an entry that has nothing to do with the row,
# with the comment above telling them ranges were handled. Every hyphenated range is now exempt,
# including one a row might try to rest on; that half is held by `_uncited_authority` below, which
# admits a section, a decision or a document path and not a range. [M4.16 cycle 1, M416-C1-COV-05]
_BARE_PROPOSAL = re.compile(r"\bproposals?\s+(\d+)\b(?!\s*-\s*\d)", re.I)
# AND A LIST CONTINUES THE CITATION, which the word requirement above cannot say on its own. The
# word has to lead each number, so `proposals 107, 109` and `proposals 74/75` read as a citation
# of their first number and the second stood unseen -- so the rule discharged on a one-number
# repair while the row went on resting on the other: `decision 295 (proposal 107) and 109`
# measures green with 109 still standing as authority. The mirror rule twelve lines down solved
# this for itself (`_DECISION_CITATION` consumes `, + and`), so the two halves of one rule
# disagreed about what a list is. The separators are the ones the register and this map actually
# write, and the range lookahead is repeated per element because `proposals 71, 104-109` continues
# into a range. [M4.16 cycle 2, M416-C2-COV-04]
_PROPOSAL_TAIL = re.compile(r"\s*(?:,|\+|/|and)\s*(\d+)\b(?!\s*-\s*\d)", re.I)


def _proposal_citations(text: str) -> list[tuple[int, int]]:
    """Every proposal number `text` cites, each with the offset of the citation it belongs to.

    The HEAD's offset and not each number's, because the escape phrase below is bound to the
    citation it excuses and a list is one citation: "rests only on unsettled proposals 138, 139"
    would otherwise be forgiven its first number and reported for its second, over the comma the
    escape cannot cross. [M4.16 cycle 2, M416-C2-COV-04]
    """
    found: list[tuple[int, int]] = []
    for head in _BARE_PROPOSAL.finditer(text):
        found.append((int(head.group(1)), head.start()))
        position = head.end()
        while (tail := _PROPOSAL_TAIL.match(text, position)) is not None:
            found.append((int(tail.group(1)), head.start()))
            position = tail.end()
    return found
# The honest form, which the map already carried twice before this rule existed: "rests only on
# unsettled proposal 138" SAYS that the requirement has no owner-agreed authority, which is a
# disclosure rather than a citation.
#
# BOUND TO THE CITATION IT EXCUSES rather than to the clause around it, and bound by ADJACENCY
# rather than by punctuation. Read as a phrase anywhere in the clause, it forgave every proposal in
# that clause whatever the phrase was attached to: "drag-and-drop, which rests only on the section,
# and proposal 71" is an ordinary map sentence in which the escape attaches to the section, and it
# discharged the rule for a proposal fifty characters further on. Cycle 1 bound it to punctuation,
# which is one CLAUSE wide rather than one citation wide -- and the same sentence with its two
# commas deleted walked straight back through, measured: "drag-and-drop rests only on the section
# and proposal 71" offended no rule at all. A bind that two keystrokes discharge is a form of
# words.
#
# So the phrase must run INTO the number, and must publish a debt on the way. Decision 315 asks
# that a row using the escape mark the proposal UNSETTLED in the same field; this is that clause
# written at the one position where "unsettled" can only be about the number it stands in front of.
# What the pattern admits is the idiom the map's two honest rows already use and nothing else --
# "rests only on unsettled proposal 138", "rests only on unsettled proposals 138, 139" -- so an
# escape attached to a section, an escape publishing no debt, and an honest disclosure carrying a
# second undisclosed proposal as a free rider are three spellings this refuses and cycle 1's did
# not. Measured over the map: no shipped row uses the escape at all, and the legitimate citations
# that carry numbers in their own prose stay green -- three of them registered below, in
# `test_the_synthetic_row_offends_no_rule` -- so the tightening costs no live spelling. It still
# does not stop a row that writes the phrase and then says something else entirely; that is a row
# lying about its own authority rather than mis-citing one, and no grep reaches it.
# [decisions 295 and 315; M4.16 cycle 1, M416-C1-COV-04; M4.16 cycle 4, M416-C4-COV-01]
_PROVENANCE_ESCAPE = re.compile(r"rests only on unsettled\s+$", re.I)

# Where the register's two halves divide, in its header's own words: "**Proposals 1-161** are
# dated reasoning ... citable as provenance and nothing more" and "**Entries 162 onward are
# numbered owner decisions**". Seven of the proposals were settled on the spot; six of those carry
# a `Decided (owner, ...)` line inline and are read off the file below rather than listed here.
_FIRST_DECISION = 162
_SETTLED_INLINE = "Decided (owner"
# `_REGISTER_DECISION` heads an entry and is shared with the range guard at the foot of this file
# rather than copied: one reading of `### <n>.` here, so the register changing shape moves one
# line. Defined below, which Python resolves at call time.


@cache
def _register_entries() -> dict[int, str]:
    """The register sliced into its numbered entries, each as its own text, by number.

    One reading of `### <n>.` for both rules that need the file, which is what the comment above
    `_REGISTER_DECISION` asks for: the settled-proposal reader below wants each entry's heading
    line and the provenance gate wants its body, and a second slicing would be a second thing to
    keep true. [M4.16 cycle 2, M416-C2-COV-01]
    """
    body = REGISTER.read_text(encoding="utf-8")
    heads = list(_REGISTER_DECISION.finditer(body))
    entries: dict[int, str] = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(body)
        entries[int(head.group(1))] = body[head.start():end]
    return entries


def _settled_proposals() -> frozenset[int]:
    """The proposals among 1-161 the owner settled where they stand, as their own numbers.

    Read off the register, because the alternative is a list of six numbers in a test file that
    nobody re-derives -- which is the defect this milestone exists to close, one document over.
    """
    return frozenset(
        number
        for number, entry in _register_entries().items()
        if number < _FIRST_DECISION and _SETTLED_INLINE in entry
    )


def _is_a_decision(number: int) -> bool:
    """Whether a number cited as `decision N` is one the owner has actually taken.

    An entry is NOT demanded for 162 and above: the numbering "is neither contiguous nor confined
    here", as the register's header says -- 168-178 were taken in `docs/milestones/ROADMAP-to-M5.md`
    and this map cites 169, 171 and 175, none of which has a heading in the register at all. The
    rule is about a proposal laundered into a decision, not about a typo in a number.
    """
    return number >= _FIRST_DECISION or number in _settled_proposals()


def _adopted_by(number: int, parenthetical: str) -> bool:
    """Whether decision `number`'s own register entry cites every proposal `parenthetical` carries.

    An entry is DEMANDED here, and this is the one place `_is_a_decision`'s "no entry needed" rule
    does not carry: that rule is about a number a row CITES, where a missing heading is a typo in
    the register's own bookkeeping, and this is about an adoption a row CLAIMS. A decision with no
    entry -- 168-178 and 228-233 -- can still be cited and still cannot launder a proposal behind
    itself, because there is nothing to read the adoption out of.

    The entry has to name the proposal the way a row does, read with the same function: a number in
    an answer table and nowhere else is a cell, and the four live adoptions are all argued in the
    entry's prose. A decision that adopts one without saying so reddens until it says so, which is
    the loud direction. The limit in the other direction is stated rather than hidden: an entry
    that names a proposal in order to REFUSE it reads the same to this rule as one that adopts it,
    and no grep separates those two -- which is the same limit `_PROVENANCE_ESCAPE` records about
    itself, a row lying about its authority rather than mis-citing one.
    [M4.16 cycle 2, M416-C2-COV-01]
    """
    carried = {n for n, _ in _proposal_citations(parenthetical)}
    if not carried:
        return True
    entry = _register_entries().get(number)
    if entry is None:
        return False
    return carried <= {n for n, _ in _proposal_citations(entry)}


def _strip_provenance(spec: str) -> str:
    """`decision 295 (proposal 71)` with the adopted number's provenance removed.

    Only where the citation is a decision AND the decision adopted what the parenthetical carries.
    A parenthetical hanging off a number the register heads as a proposal is not provenance behind
    an adoption; neither is one naming a proposal that decision never mentions. Both are the thing
    rule 5 is looking for. [M4.16 cycle 2, M416-C2-COV-01]

    The head may be a LIST since review cycle 5, and one adopting decision in it is enough: the
    parenthetical is carried if any cited decision carries it, and a number that adopted nothing
    adds no licence. The alternative was a rule that forbade citing two decisions and keeping the
    provenance, which is the shape M5's two bare-proposal rows will need the day they are re-cited.
    [M4.16 cycle 5, M416-C4-COV-09]
    """
    def provenance(match: re.Match) -> str:
        numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
        adopted = any(
            _is_a_decision(number) and _adopted_by(number, match.group(2))
            for number in numbers
        )
        return " " if adopted else match.group(0)

    return _CARRIED_BY_A_DECISION.sub(provenance, spec)


# Every way this map spells a decision citation, measured over `spec` rather than assumed:
# `decision 11`, `decisions 164, 165, 288`, `decisions 175 and 205`, `decision 162) + decision
# 248`, and `decision 84, adopted consequence`, where the comma introduces prose rather than a
# second number. `decision-doc proposals 107, 109` is deliberately NOT one of them -- the hyphen
# ends the word, so the two M5 rows keep citing proposals until the milestone that owns them
# re-cites them, which is the whole reason rule 5 is scoped to `current_milestone`.
_DECISION_CITATION = re.compile(r"\bdecisions?\s+(\d+(?:\s*(?:,|\+|and)\s*\d+)*)", re.I)


def _bare_proposal_citations(rows: list[dict]) -> list[str]:
    """Every shipped row whose `spec` rests on a proposal number nobody adopted.

    `docs/spec-v2.2-proposals.md` said of itself, until decision 288 retired the sentence, that
    nothing in it was normative -- so fourteen shipped rows cited as their authority a document
    that disclaimed itself, and four of them pinned a constant no section states: proposal 71's
    80% credible mass, proposal 157's shared `straddles()` for badge and queue, proposal 76's
    end-of-scale clamp, proposals 74/75's Cancel control. Decision 295 answers all fourteen --
    three rows adopt their constants into the spec and cite `decision 295 (proposal N)`, eleven
    move the number into `why` as provenance -- and this is what stops the door reopening.

    Read out of `spec` and out of nothing else, which is the distinction the decision draws:
    `spec` is where a row says what it answers to, and `why` is where it may say where the
    thinking came from. Scoped to `current_milestone` because a row not yet owed a test is not
    yet owed an authority either; the two M5 rows that still cite bare proposals are the whole
    reason that scoping is stated rather than assumed. [M4.16, cs-30, decision 295]

    The field is read whole rather than split on `;`. The split existed to scope the escape to
    one clause, and the escape is now bound to the citation it excuses by standing immediately in
    front of it, so a second scoping would only be a second thing to keep true -- and a clause is
    the wrong unit anyway, which is what cycle 4 measured when two deleted commas discharged the
    rule over a clause the escape was never about.
    [decisions 295 and 315; M4.16 cycle 1, M416-C1-COV-02, M416-C1-COV-04;
     M4.16 cycle 4, M416-C4-COV-01]

    EVERY number the row cites, in one line. A citation of a list names all of them, because a
    rule that reported only the head could be discharged by repairing the head -- and the row then
    rests on the number nobody read. [M4.16 cycle 2, M416-C2-COV-04]
    """
    offenders = []
    for r in rows:
        if not _at_or_before(r["milestone"]):
            continue
        stripped = _strip_provenance(r["spec"])
        cited = [
            number
            for number, start in _proposal_citations(stripped)
            if not _PROVENANCE_ESCAPE.search(stripped[:start])
        ]
        if cited:
            numbers = ", ".join(str(n) for n in dict.fromkeys(cited))
            offenders.append(
                f"{r['id']} ({r['milestone']}) cites proposal{'s' if len(cited) > 1 else ''} "
                f"{numbers} as its authority: {r['spec']}"
            )
    return offenders


def _laundered_decision_citations(rows: list[dict]) -> list[str]:
    """Every shipped row citing as a DECISION a number the register heads as a proposal.

    The mirror of the rule above, and without it that rule is a rule about a noun. It searched for
    the word `proposal` and for nothing else, so the cheapest way to discharge it was never to
    gain an authority: it was to write `decisions 107, 109` where the row said `proposals 107,
    109`. One word, no owner involved, and it reads as house style beside the eighteen shipped
    rows that legitimately cite entries 11, 18, 35, 84, 117 and 154 as `decision N` -- all six of
    which the owner settled inline on 2026-08-29, measured, so this rule lands green over the map
    as it is. The next milestone is where it bites: M5 opens with exactly two rows citing bare
    proposals, and it has no plan document to argue with.
    [decision 295's second half, M4.16-plan.md:542-544; M4.16 cycle 1, M416-C1-COV-01]
    """
    offenders = []
    for r in rows:
        if not _at_or_before(r["milestone"]):
            continue
        for match in _DECISION_CITATION.finditer(r["spec"]):
            for number in (int(n) for n in re.findall(r"\d+", match.group(1))):
                if _is_a_decision(number):
                    continue
                offenders.append(
                    f"{r['id']} ({r['milestone']}) cites decision {number}, which the register "
                    f"heads as a proposal rather than as a decision: {r['spec']}"
                )
    return offenders


# What counts as an authority a reader can go and check: a section of the normative file, a
# numbered owner decision, or a document in this repository named by path. Anything else is prose.
_AUTHORITY = re.compile(
    r"§\d|\bdecisions?\s+\d+|[\w.-]+\.(?:md|txt|toml|py|js|svelte|sql|yml|css)\b"
)


def _uncited_authority(rows: list[dict]) -> list[str]:
    """Every shipped row whose `spec` names no authority at all.

    The POSITIVE half of rule 5, and the half that was written into a coverage row's `what` -- "the
    requirement's authority is a section or a numbered decision" -- while nothing asserted it.
    `_bare_proposal_citations` above only REMOVES one spelling; `_malformed` only asks that the
    field be non-empty, so `spec = "because I said so"`, `spec = "TBD"` and `spec = "see the
    mockup"` all shipped as covered. In the one milestone whose subject is that a record may not
    promise more than it holds, that clause outran its own tests.

    Measured before it was written, the way this map asks, and re-measured since: over this tree
    all 315 shipped rows name a section, a numbered decision or a document path, so the rule lands
    green and asserts a property the map already has rather than inventing work. A document path is
    admitted because many rows answer to `CLAUDE.md`'s conventions or to `docs/TESTING.md` rather
    than to a §, and a rule that reddened those would be narrowed by the first person it stopped.
    Decision 305's entry records 308, the count cycle 1 took before its own decision 306 added a
    row; a dated entry is superseded rather than edited (decision 304), so the figure that moves is
    this one, and `test_the_authority_rule_publishes_the_count_it_holds` derives it.
    [decision 305; M4.16 cycle 1, M416-C1-COV-03; M4.16 cycle 3, M416-C3-COV-03]
    """
    return [
        f"{r['id']} ({r['milestone']}) names no authority a reader can check: {r['spec']!r}"
        for r in rows
        if _at_or_before(r["milestone"]) and not _AUTHORITY.search(r["spec"])
    ]


# --- M4.16 review cycle 3: the figure decision 305 was taken on ---------------------------------
#
# "all 308 shipped rows" was true the moment it was measured in review cycle 1 and false by the end
# of it: decision 306's own row landed under the measurement and made the map 309. The SUBSTANCE
# still holds -- every shipped row names an authority, which is the whole of what the rule asserts
# -- but the figure was pasted into three places and derived in none, which is the defect
# `test_the_testing_ledger_counts_the_ids_the_map_actually_holds` sits ten screens above this one
# to catch. Decision 305's entry in the register keeps the 308 it was taken with: a dated entry is
# superseded rather than edited, which is decision 304's own mechanism and the reason entries are
# numbered. The two LIVE copies are the ones that say "over this tree", so those are the two held
# to it. [decision 184; decision 304; M4.16 cycle 3, M416-C3-COV-03]
_AUTHORITY_SIZE = re.compile(
    r"all (?P<count>\d+) shipped rows (?:already )?name a section, a numbered decision or a "
    r"document path"
)


def test_the_authority_rule_publishes_the_count_it_holds():
    """Rule 5's positive half, held to the map it says it was measured over.

    The number is not decoration: it is the whole argument for asserting the positive rather than
    narrowing the `what` (decision 305, "MEASURED before it was written, which is what makes this
    the cheap option"). A reader who cannot reproduce it cannot tell whether the rule lands green
    because the map is clean or because the sentence stopped describing the map.
    [decision 305; M4.16 cycle 3, M416-C3-COV-03]
    """
    shipped = [r for r in REQUIREMENTS if _at_or_before(r["milestone"])]
    named = [r for r in shipped if _AUTHORITY.search(r["spec"])]
    assert len(named) == len(shipped), (
        f"{len(shipped) - len(named)} shipped row(s) name no authority, so the sentence these two "
        "files publish is no longer the claim rule 5 asserts. `_uncited_authority` above names "
        "them; this guard only holds the figure."
    )

    wrong, missing = [], []
    for path in (MAP, TESTS / "test_spec_coverage.py"):
        # The map carries the sentence as a wrapped TOML comment, so the `#` that opens each
        # continuation line is stripped before the clause is read. Flattening whitespace alone
        # leaves it mid-sentence, which is a guard that reads nothing and reports green.
        body = re.sub(r"(?m)^[ \t]*#[ \t]?", "", path.read_text(encoding="utf-8"))
        claims = [int(m.group("count")) for m in _AUTHORITY_SIZE.finditer(" ".join(body.split()))]
        if not claims:
            missing.append(path.name)
        wrong += [f"{path.name} publishes {n}" for n in claims if n != len(shipped)]
    assert not missing, (
        f"{missing} no longer publish the count rule 5 was measured over, so this guard reads "
        'less than it says it does. The published form is "all <N> shipped rows name a section, '
        'a numbered decision or a document path".'
    )
    assert not wrong, (
        f"the map holds {len(shipped)} shipped rows and:\n  "
        + "\n  ".join(dict.fromkeys(wrong))
        + "\n\nRe-derive it rather than adjusting whichever figure looks wrong. Decision 305's own "
        "entry is NOT one of these two: a dated entry records the tree it was taken on and is "
        "superseded rather than edited (decision 304)."
    )



# --- M4.16 review cycle 2: rule 5 resolves the authority it reads -------------------------------
#
# `_AUTHORITY` above matches the two characters `§6` and asks nothing further, so the map shipped
# `decision 293 (§6.6 / §6.9 Data sources)` on the single new surface this milestone builds -- and
# v2.1 has never had a §6.9, while §6.6 is the admin view decision 293 explicitly ruled AGAINST
# ("the admin Data card is admin-only, which would leave the household's non-admin member ... with
# no reachable notice at all"). The section that carries the sentence, §10, was not cited at all.
# Decision 305's own entry says the rule is about "authority a reader can go and check"; a reader
# who goes and checks finds nothing, which is the shape check failing at the one thing it is for.
#
# The sentence half is the same defect one field over and was found by the same sweep: a `spec`
# that QUOTES a document must quote something the document holds. `platform-coverage-report-counts
# -every-waiver` quoted "appears in the report as waived rather than vanishing" from a passage THIS
# WAVE deleted, and two more rows anchored line numbers that had moved -- which is why the anchors
# went with the repair rather than being restated: `spec_coverage.toml`'s own convention, written
# at M4.10, is that "the sentence it rests on is stable where its line number is not".
# [decision 305; M4.16 cycle 2, SPEC-C2-03, M416-C2-CG-03]

README = REPO / "README.md"


# Two spellings that resolve to something other than a heading, both predating M4.16 by many rows.
# §14.N is one of §14's numbered risks -- §14 is an ordered list, not a run of subsections -- and
# §54 is the 54a-54h Tonight fold, which lives in the register and which the spec's Status block
# names as a fold. Measured before the rule was written: without these two, seventeen citations
# across ten pre-M4.16 rows redden, and a rule like that is narrowed by the first person it stops.
#
# ANCHORED, since review cycle 4. They were written as PREFIXES and a prefix forgives more than the
# convention has: `54` admitted §547 and `14.` admitted §14.99, so a transposition of §5.4 and a
# risk the list does not hold both read as an authority a reader can go and check -- the exact
# failure decision 305 states and the one this rule was built to end, surviving inside the escape
# it shipped with. Measured through the whole reader stack: both passed every rule, while the
# control §6.9 was caught. The risk bound is DERIVED off the file rather than written here for
# `test_the_section_conventions_this_rule_allows_are_not_stale`'s own reason -- a 1-7 typed into
# this module is a number that goes stale the first time a risk is added, which is the staleness
# that test exists to refuse. [decision 184; M4.16 cycle 4, M416-C4-COV-07]
def _resolves_by_convention(number: str) -> bool:
    """Whether a cited section resolves to one of the two things that are not headings."""
    if number == "54":
        return True
    risk = re.fullmatch(r"14\.(\d+)", number)
    return risk is not None and 1 <= int(risk.group(1)) <= _numbered_risks()

_CITED_SECTION = re.compile(r"§(\d+(?:\.\d+)?)")
_SPEC_HEADING = re.compile(r"^#{2,4} +(\d+(?:\.\d+)?)[.\s]", re.M)
# A document this repository holds, named by path from the root. Bare filenames are not resolved:
# `CLAUDE.md` and `M4-open-points.md` are cited without a directory all over this map, and guessing
# which of three `docs/milestones` files a bare name means is how a guard starts being wrong.
_CITED_DOCUMENT = re.compile(
    r"\b((?:docs|ops|e2e|backend|frontend)/[\w./-]+\.(?:md|txt|toml|py|js|svelte|sql|yml|css))\b"
)
# A sentence a `spec` field quotes. Twenty characters because `"kind"` and `'The coverage map'`
# are labels rather than quotations, and the rule is about a claim a reader is sent to verify.
# Straight quotes only, and the two halves of that are one decision: no shipped row's `spec`
# uses a typographic quote, measured over the whole map, and a curly quote written into this
# pattern is a character a cp1252 console cannot print -- which
# `test_no_console_output_leaves_the_oem_code_page` refuses of every string this suite reports
# through. A row that ever needs one states its sentence in straight quotes instead.
#
# The word-boundary lookarounds are what keep an apostrophe out of it: `the app's` and
# `a household's` are two apostrophes far enough apart to read as a quotation, and a rule that
# read them as one would send an author hunting for a sentence nobody wrote.
_CITED_SENTENCE = re.compile(r"""(?<!\w)["']([^"']{20,})["'](?!\w)""")


@cache
def _normative_path() -> Path:
    """The file README calls normative.

    Resolved through README rather than named here, for `test_static_contracts.py::_normative_file`'s
    reason: hard-coding `spielplan-spec_v2.1.md` would make the rule agree with itself, and decision
    288's mechanism is that the file is amended in place rather than replaced. Six lines duplicated
    rather than imported, because importing that module to read them would pull ten thousand lines
    of unrelated collection into this one.
    """
    claim = re.search(
        r"\*\*The spec is the authority\.\*\*(.*?)(?:\r?\n\r?\n|\Z)",
        README.read_text(encoding="utf-8"),
        re.S,
    )
    assert claim, "README no longer opens with `**The spec is the authority.**`"
    named = re.search(r"docs/[\w.-]+\.md", claim.group(1))
    assert named, f"README's authority paragraph names no file under docs/:\n  {claim.group(1)}"
    path = REPO / named.group(0)
    assert path.exists(), f"README names {named.group(0)} as normative and it is not in the tree"
    return path


@cache
def _normative_sections() -> frozenset[str]:
    """Every `## N.` / `### N.M` heading the file README calls normative."""
    return frozenset(_SPEC_HEADING.findall(_normative_path().read_text(encoding="utf-8")))


@cache
def _numbered_risks() -> int:
    """How many risks §14 lists, counted off the normative file.

    The bound on the §14.N convention above, and it is a count rather than a constant because a
    constant is what goes stale: §14 is an ordered markdown list, so the risks are there to be
    counted, and decision 184's rule -- a published figure is the one a run produced -- applies to
    a guard's own allowances as much as to a document's. Zero when the section cannot be found at
    all, which refuses every §14.N rather than forgiving it, because a reader cannot follow a
    citation into a list this guard could not read. [decision 184; M4.16 cycle 4, M416-C4-COV-07]
    """
    body = re.search(
        r"^## 14\..*?(?=^## |\Z)", _normative_path().read_text(encoding="utf-8"), re.M | re.S
    )
    if body is None:
        return 0
    return max((int(n) for n in re.findall(r"^(\d+)\. ", body.group(0), re.M)), default=0)


def _unresolvable_sections(rows: list[dict], sections: frozenset[str]) -> list[str]:
    """Every shipped row citing a section the normative file has no heading for."""
    return [
        f"{r['id']} ({r['milestone']}) cites §{n}, and the normative file has no such "
        f"heading: {r['spec']!r}"
        for r in rows
        if _at_or_before(r["milestone"])
        for n in dict.fromkeys(_CITED_SECTION.findall(r["spec"]))
        if n not in sections and not _resolves_by_convention(n)
    ]


def _unresolvable_quotations(rows: list[dict]) -> list[str]:
    """Every shipped row quoting a sentence no document it names actually carries.

    Markdown emphasis is stripped from the haystack and whitespace collapsed on both sides: the
    ledger writes `**Write the tests first**, at the ...` and wraps its paragraphs at about column
    100, so a quotation is as likely to arrive re-flowed and un-bolded as not. A row naming no
    document under a known directory is not checked -- see `_CITED_DOCUMENT`.
    """
    offenders = []
    for r in rows:
        if not _at_or_before(r["milestone"]):
            continue
        paths = [p for p in dict.fromkeys(_CITED_DOCUMENT.findall(r["spec"])) if (REPO / p).exists()]
        if not paths:
            continue
        bodies = {p: (REPO / p).read_text(encoding="utf-8").replace("*", "") for p in paths}
        for quoted in dict.fromkeys(_CITED_SENTENCE.findall(r["spec"])):
            loose = re.compile(r"\s+".join(map(re.escape, quoted.replace("*", "").split())))
            if not any(loose.search(body) for body in bodies.values()):
                offenders.append(
                    f"{r['id']} ({r['milestone']}) quotes {quoted!r}, and none of {paths} "
                    "carries that sentence"
                )
    return offenders


# And the line numbers go with the repair rather than being restated. The map argued this at
# M4.10 in its own words -- "A row whose citation drifts to an unrelated paragraph is a row
# whose argument nobody can check, and the sentence it rests on is stable where its line number
# is not" -- and then went on carrying four anchors, three of which had drifted by the time the
# milestone that wrote them closed: `:103` landed on a paragraph about GitHub concurrency
# groups and `:92-93` on a passage this same wave deleted. Restating four numbers buys one
# edit of accuracy; refusing them makes the SENTENCE the citation, which the rule above then
# holds. Measured: four rows carried an anchor and none does now.
# [M4.16 cycle 2, M416-C2-CG-03]
_LINE_ANCHOR = re.compile(r"\b([\w./-]+\.(?:md|txt|toml|py|js|svelte|sql|yml|css)):(\d+)")


def _line_anchored_citations(rows: list[dict]) -> list[str]:
    """Every shipped row whose `spec` cites a document at a line number."""
    return [
        f"{r['id']} ({r['milestone']}) cites {path}:{line}, and a line number is the half of "
        "a citation that goes stale first"
        for r in rows
        if _at_or_before(r["milestone"])
        for path, line in dict.fromkeys(_LINE_ANCHOR.findall(r["spec"]))
    ]


# --- M4.16 review cycle 3: and the kind decision 305 named first resolves too -------------------
#
# That entry names three spellings the map printed covered -- "`TBD`, `because I said so` or a
# decision number that does not exist" -- and the shipped rule closed the first two. `_AUTHORITY`
# matches the characters `decision 999` and asks nothing further, and `_is_a_decision` admits every
# integer at or above 162 without opening a file, so a shipped row citing `decision 999` fired
# NONE of this gate's readers: measured, over all twelve of them. `decision 230` fires none either,
# and the register's own header says 228-233 "were reserved and never spent". Review cycle 2 made
# the section and the quotation resolvable on exactly this argument -- a reader sent to check has
# to find something -- and this is that argument applied to the kind the decision named first.
#
# The set is enumerable from two files this suite already reads, because the register says where
# the numbering lives: entries here, plus `### Decision N` in `docs/milestones/ROADMAP-to-M5.md`,
# where 168-178 were taken. Measured before it was written, the way this map asks: the shipped
# rows cite 40 distinct numbers and all 40 resolve, 12 of them only through the roadmap. So the
# rule lands green and asserts the map as it is rather than inventing work.
#
# BELOW 162 THIS RULE IS SILENT, and that is a division of labour rather than a gap:
# `_laundered_decision_citations` owns that range, where the offence is a proposal renamed rather
# than a number nobody took, and its message says so. A rule reporting both would print two
# sentences about one row and disagree with itself about which.
#
# The failure this must not become is the other direction -- a decision taken in a THIRD document
# reddening a legitimate citation. So the message names the two files it read rather than calling
# the number wrong, and the roadmap half is held to its premise beside the rule: a file that stops
# heading its decisions that way would empty half the set in silence, which is the staleness
# `platform-coverage-rows-are-proven-at-their-kind` already refuses of its own allow-list.
# [decision 305; M4.16 cycle 3, M416-C3-COV-01]
ROADMAP = REPO / "docs" / "milestones" / "ROADMAP-to-M5.md"
_ROADMAP_DECISION = re.compile(r"^### Decision (\d+)\b", re.M)


@cache
def _decisions_taken() -> frozenset[int]:
    """Every number either register heads an entry under."""
    return frozenset(_register_entries()) | frozenset(
        int(n) for n in _ROADMAP_DECISION.findall(ROADMAP.read_text(encoding="utf-8"))
    )


def _unresolvable_decision_citations(rows: list[dict]) -> list[str]:
    """Every shipped row citing a `decision N` at or above 162 that neither register heads."""
    taken = _decisions_taken()
    return [
        f"{r['id']} ({r['milestone']}) cites decision {number}, and neither "
        "docs/spec-v2.2-proposals.md nor docs/milestones/ROADMAP-to-M5.md heads an entry with "
        f"that number: {r['spec']!r}"
        for r in rows
        if _at_or_before(r["milestone"])
        for match in _DECISION_CITATION.finditer(r["spec"])
        for number in dict.fromkeys(int(n) for n in re.findall(r"\d+", match.group(1)))
        if number >= _FIRST_DECISION and number not in taken
    ]


def test_the_roadmap_half_of_the_decision_register_is_not_stale():
    """The premise `_decisions_taken` rests on, asserted rather than assumed.

    The register's header states it -- the numbering "is neither contiguous nor confined here:
    168-178 were taken in `docs/milestones/ROADMAP-to-M5.md`" -- and twelve of the forty numbers
    the shipped rows cite resolve only there. A roadmap that stopped heading its decisions
    `### Decision N` would leave the rule above reading one register, silently forgiving nothing
    and reddening eleven live citations at once. [M4.16 cycle 3, M416-C3-COV-01]
    """
    roadmap = frozenset(
        int(n) for n in _ROADMAP_DECISION.findall(ROADMAP.read_text(encoding="utf-8"))
    )
    assert roadmap, (
        f"{ROADMAP.name} heads no `### Decision N` entries any more, so `_decisions_taken` is "
        "the register alone and the rule above reads half the numbering the register says exists"
    )
    outside = sorted(roadmap - frozenset(_register_entries()))
    assert outside, (
        "every decision the roadmap heads is now also headed in the register, so this half of "
        "the reader holds nothing. Re-read it against the two files rather than keeping it: a "
        "guard whose premise has gone is a line nobody can remove honestly."
    )


# --- M5.1 review cycle 4: the same rule, turned on the one document that IS normative ----------
#
# `_unresolvable_decision_citations` above is scoped to `spec_coverage.toml` ROWS, and decision 305
# was taken because a row could cite "a decision number that does not exist" and still be printed
# covered. The normative file cites decisions too -- 96 distinct numbers across its fourteen
# sections and its Status block -- and nothing in this tree read one of them. It cited 327, which
# `docs/milestones/ROADMAP-M5.md` heads as a QUESTION under "Decisions the owner must take",
# beneath a preamble that says "**They are not taken.** Transcribing them into
# `docs/spec-v2.2-proposals.md` is an owner act", and which the register's own M5.1 preamble lists
# in the same change set among the numbers that "stay unspent here". So the one file CLAUDE.md
# calls normative named a decision the owner had not taken, in the one direction nothing covered:
# `test_static_contracts.py::_unnamed_wave_decisions` splits the body at `## 0.` and never sees the
# Status block, and `_numbers_named` treats every three-digit token on the point-release line as a
# number that line NAMES rather than a citation to resolve, so 327 passed through both halves.
#
# Held here rather than beside that file's other guards because the resolver is here: one reading
# of `### <n>.` and one of the roadmap's `### Decision N`, which is what the comment over
# `_register_entries` asks for. Scoped to 162 and above for `_is_a_decision`'s reason -- below it
# the offence is a proposal renamed rather than a number nobody took, and
# `_laundered_decision_citations` owns that range and says so in its own message.
#
# Three digits at the head, because `owner decision 2026-08-29` is how this file spells a dated
# ruling carrying no number at all, and a reader that took `2026` for a citation would send an
# author hunting for an entry nobody ever wrote. [decision 305; M5.1 cycle 4, M51-C4-SPEC-01]
_SPEC_DECISION_CITATION = re.compile(
    r"\bdecisions?\s+(\d{1,3}(?:\s*(?:,|and|\+|\u2013|-)\s*(?:and\s+)?\d{1,3})*)\b", re.I
)


def _spec_decision_citations(text: str) -> dict[int, int]:
    """Every `decision N` the normative file cites, by number, with the line it first stands on."""
    cited: dict[int, int] = {}
    for number, line in enumerate(text.splitlines(), 1):
        for match in _SPEC_DECISION_CITATION.finditer(line):
            body = match.group(1)
            numbers = {int(n) for n in re.findall(r"\d{1,3}", body)}
            for low, high in re.findall(r"(\d{1,3})\s*[-\u2013]\s*(\d{1,3})", body):
                numbers.update(range(int(low), int(high) + 1))
            for cite in numbers:
                cited.setdefault(cite, number)
    return cited


def test_the_normative_file_cites_no_decision_the_owner_has_not_taken():
    """Decision 305's rule, turned on the document that rule's own authority comes from.

    A reader who follows `decision 327` out of the normative file and into the decision record
    finds nothing under that heading -- and unlike a coverage row, nothing stands between them and
    the sentence. CLAUDE.md draws the line the citation crosses: `decision N` is taken and
    normative, a numbered proposal is provenance and "a requirement resting only on one rests on
    nothing the owner agreed to". A planner's numbered question written up as a decision is that
    category error in the one file where it reads as settled law, and the register said so about
    the same number in the same wave.

    The message names the two files it read rather than calling the number wrong, for the reason
    `_unresolvable_decision_citations` gives: a decision taken in a THIRD document must be able to
    redden this guard's premise instead of a legitimate sentence.
    [decision 305; row `platform-the-normative-file-describes-the-shipped-surface`;
     M5.1 review cycle 4, M51-C4-SPEC-01]
    """
    spec = _normative_path()
    taken = _decisions_taken()
    cited = _spec_decision_citations(spec.read_text(encoding="utf-8"))
    assert cited, (
        f"{spec.relative_to(REPO).as_posix()} cites no `decision N` anywhere any more, so this "
        "guard reads nothing. Either the file stopped citing the register decision 288 makes its "
        "companion, or the citation spelling moved and this reader did not come with it."
    )
    unresolvable = [
        f"{spec.relative_to(REPO).as_posix()}:{line}: cites decision {number}"
        for number, line in sorted(cited.items())
        if number >= _FIRST_DECISION and number not in taken
    ]
    assert not unresolvable, (
        "the normative file names a decision neither docs/spec-v2.2-proposals.md nor "
        "docs/milestones/ROADMAP-to-M5.md heads an entry under:\n  "
        + "\n  ".join(unresolvable)
        + "\n\nA number a planner allocated is not a decision until the owner takes it. Name it "
        "for what it is -- a numbered question in the roadmap that owns it -- rather than "
        "borrowing the word this project reserves for a ruling."
    )


def test_the_spec_citation_reader_tells_a_numbered_decision_from_a_dated_one():
    """Both ends of the reader, because a false hit here costs as much as a miss.

    The normative file spells an unnumbered ruling `owner decision 2026-08-29` three times, and a
    reader that took the year for a citation would report `decision 2026`, an entry nobody can
    write. It also collapses whole waves into ranges (`decisions 214-226`), so a range has to
    expand or the rule forgives eleven numbers at once.
    [M5.1 review cycle 4, M51-C4-SPEC-01]
    """
    read = _spec_decision_citations(
        "a. state: unseen | seen (owner decision 2026-08-29: no 'forgotten' state)\n"
        "b. as rewritten by the 54a-54h fold (decisions 175, 214-226)\n"
        "c. restated under decision 288 and decision 305\n"
    )
    assert 2026 not in read and 8 not in read, read
    assert read[175] == 2 and read[214] == 2 and read[220] == 2 and read[226] == 2
    assert read[288] == 3 and read[305] == 3


# --- M4.16 review cycle 2: rule 7 reads the tests a waiver leans on -----------------------------
#
# A waiver is an honest reason, and the honest reason for the map's one standing waiver is that
# HALF of §4.1 rule 3 can be asserted today: "The schema-separation half is meanwhile already
# asserted by test_rule3_platform_ratings_land_in_the_display_schema_only and
# test_display_only_schema_exists_and_holds_only_platform_ratings." Both exist. Neither was in any
# row's `tests`, and `_missing_tests` reads `tests` and nothing else -- so rename or delete either
# and the build stays green while the waiver goes on claiming the half it does not waive is
# covered. Same argument as `test_the_join_key_ban_kept_the_guard_its_deleted_row_left_behind`, on
# the one waiver the milestone whose subject is checkable records had audited by hand.
# [M4.16 cycle 2, M416-C2-COV-03]
_WAIVER_TEST_NAME = re.compile(r"\btest_[a-z0-9_]{12,}\b")


def _waivers_leaning_on_unregistered_tests(rows: list[dict]) -> list[str]:
    """Every waiver naming a test function no row in this map registers."""
    registered = {t.rpartition("::")[2] for r in rows for t in (r.get("tests") or [])}
    return [
        f"{r['id']} ({r['milestone']}) waives on {name!r}, which no row's `tests` names"
        for r in rows
        if r.get("waived")
        for name in dict.fromkeys(_WAIVER_TEST_NAME.findall(str(r["waived"])))
        if name not in registered
    ]


def test_the_section_conventions_this_rule_allows_are_not_stale():
    """The two exemptions, held to the thing that makes them exemptions rather than holes.

    §14.N resolves because §14 is a numbered risk list and §54 because the Tonight fold is headed
    `### 54a.` in the register. If either stops being true -- §14 gains real subsections, the fold
    is renumbered into the spec -- the exemption is forgiving a citation that should now resolve,
    which is the staleness `platform-coverage-rows-are-proven-at-their-kind` already refuses of its
    own allow-list. [M4.16 cycle 2, SPEC-C2-03]
    """
    sections = _normative_sections()
    assert "14" in sections and "14.3" not in sections, (
        "the normative file now heads §14 subsections of its own, so `14.` is no longer a "
        "convention this rule has to forgive: drop it and let the citations resolve."
    )
    assert "54" not in sections, "the 54a-54h fold has landed as a §54 heading; drop the exemption"
    assert re.search(r"^### 54a\. ", REGISTER.read_text(encoding="utf-8"), re.M), (
        "the register no longer heads the Tonight fold at `### 54a.`, so `§54` resolves to "
        "nothing at all and the exemption is forgiving a dangling citation."
    )
    # And the other way an exemption goes stale, which is not by outliving its subject but by
    # being wider than it ever was: a prefix forgives every number that BEGINS like the convention.
    # The bound is re-derived here rather than restated, so a risk added to §14 widens the rule and
    # a risk removed narrows it, in the same run. [M4.16 cycle 4, M416-C4-COV-07]
    risks = _numbered_risks()
    assert risks, (
        "§14's numbered risks could not be counted off the normative file, so the §14.N convention "
        "is bounded by nothing readable: the section was renamed, or its list stopped being one."
    )
    assert not _resolves_by_convention(f"14.{risks + 1}"), (
        f"§14 lists {risks} risks and the convention forgives §14.{risks + 1} as well, so a "
        "citation past the end of the list reads as an authority a reader can go and check."
    )
    assert _resolves_by_convention(f"14.{risks}") and _resolves_by_convention("54"), (
        "the conventions no longer resolve the two things they exist for, so ten pre-M4.16 rows "
        "are about to redden over citations that are not dangling."
    )
    assert not _resolves_by_convention("547") and not _resolves_by_convention("14"), (
        "a number that only BEGINS like a convention is being forgiven by it: §547 is a "
        "transposition of §5.4, and §14 itself is a heading this rule must resolve normally."
    )


def test_shipped_rows_cite_a_decision_rather_than_a_bare_proposal():
    """Rule 5, four halves now, and the last two are what "a reader can go and check" means.

    A requirement resting only on a proposal rests on nothing the owner agreed to; one resting
    on nothing at all rests on less; and one resting on a section the normative file does not
    have, or on a sentence the document it names does not carry, rests on something that reads
    like an authority and is not one. The last two are the shape check catching up with its own
    docstring (decision 305). [M4.16 cycle 2: SPEC-C2-03, M416-C2-CG-03]
    """
    uncited = _uncited_authority(REQUIREMENTS)
    assert not uncited, "\n".join(
        [
            *uncited,
            "",
            "`spec` is where a row says what it answers to: a section of the normative file, a "
            "numbered owner decision, or a document in this tree by path. Provenance and "
            "reasoning belong in `why`.",
        ]
    )
    offenders = _bare_proposal_citations(REQUIREMENTS)
    assert not offenders, "\n".join(
        [
            *offenders,
            "",
            "Cite the section the requirement answers to, or the numbered decision that adopted "
            "it -- `decision N (proposal M)` keeps the provenance, and only where the register's "
            "entry for N cites M: the parenthetical is read rather than taken on trust. A "
            "proposal nobody adopted may be named in `why`, or in `spec` inside a phrase that "
            "says the row rests only on it.",
        ]
    )
    laundered = _laundered_decision_citations(REQUIREMENTS)
    assert not laundered, "\n".join(
        [
            *laundered,
            "",
            "Renaming a proposal `decision N` is the one-word way to discharge the rule above "
            "without gaining an authority. Cite the section, or take the decision.",
        ]
    )
    dangling = _unresolvable_sections(REQUIREMENTS, _normative_sections())
    assert not dangling, "\n".join(
        [
            *dangling,
            "",
            "A section number is an authority only where the section exists. Cite the section "
            "that carries the sentence -- the one this rule was written over pointed at a "
            "§6.9 the file has never had, and at the §6.6 the decision beside it "
            "ruled against.",
        ]
    )
    misquoted = _unresolvable_quotations(REQUIREMENTS)
    assert not misquoted, "\n".join(
        [
            *misquoted,
            "",
            "Re-read the document and quote what it says now, or drop the quotation. A sentence "
            "a wave deleted is the one citation shape an auditor cannot tell from a typo.",
        ]
    )
    anchored = _line_anchored_citations(REQUIREMENTS)
    assert not anchored, "\n".join(
        [
            *anchored,
            "",
            "Quote the sentence instead. This map already says so at "
            "`platform-every-http-route-is-named-by-a-test`: the sentence a row rests on is "
            "stable where its line number is not.",
        ]
    )
    phantom = _unresolvable_decision_citations(REQUIREMENTS)
    assert not phantom, "\n".join(
        [
            *phantom,
            "",
            "A decision number is an authority only where the decision was taken. Check the "
            "number against the register -- 228-233 were reserved and never spent, so a "
            "transposition lands on one of them without looking wrong -- or, if the decision was "
            "taken in a document neither of those two, say so here and widen the reader to it.",
        ]
    )


def test_the_register_reader_finds_the_proposals_the_owner_settled_where_they_stand():
    """The reach assertion the mirror rule rests on, in rule 2's idiom one file over.

    `_is_a_decision` admits 162 and above outright, so everything it can actually refuse is a
    number at or below 161 -- and the six the owner settled inline are the only ones down there
    that a row may legitimately cite. A reader that parsed nothing would call all six laundered
    and fail the live map loudly, which is the safe direction; a reader that matched the whole
    register would forgive every proposal there is, which is silent. Both are excluded by reading
    the count off the file and comparing it with the header's own sentence.
    [decision 295's second half, M4.16-plan.md:542-544; M4.16 cycle 1, M416-C1-COV-01]
    """
    settled = _settled_proposals()
    assert settled, (
        "no entry in the register carries an inline `Decided (owner, ...)` line, so the mirror "
        "rule would refuse every `decision N` citation below 162 in the map"
    )
    assert max(settled) < _FIRST_DECISION, settled
    # Against the header's own published figure rather than against a number written here: the
    # register says "six carry a **Decided (owner, 2026-08-29)** line inline", and a reader that
    # found five would be reading a file whose shape has moved under it. (The header says SEVEN
    # were settled; the seventh, 54, replaced its question with a redesign and carries no line.)
    published = re.search(r"\b(\w+) carry a \*\*Decided \(owner", REGISTER.read_text("utf-8"))
    assert published, "the register's header no longer publishes how many entries carry the line"
    assert published.group(1).lower() == "six" and len(settled) == 6, sorted(settled)


def test_a_range_of_proposals_is_never_read_as_a_citation_of_a_number_nobody_wrote():
    """The exemption the comment above `_BARE_PROPOSAL` claims, held to every spelling of a range.

    It held only for `proposals 1-161`, and only by accident: `(\\d+)` gave digits back until the
    lookahead was satisfied, which it cannot be when the first number is one digit long. Every
    other range was reported -- `proposals 104-109` as "proposal 10" -- so the rule named a row
    over an entry nobody had cited, with the comment telling the next reader ranges were handled.
    [M4.16 cycle 1, M416-C1-COV-05]
    """
    for spelling in ("proposals 1-161", "proposals 9-11", "proposals 74-75", "proposals 104-109"):
        row = _synthetic(spec=f"the register's own shape ({spelling})")
        assert not _bare_proposal_citations([row]), spelling
    # And the rule still bites on the citation a range is not: one number, standing as authority.
    assert _bare_proposal_citations([_synthetic(spec="drag-and-drop (proposal 104)")])


def test_a_list_of_proposals_is_read_as_a_citation_of_every_number_in_it():
    """The other shape a citation takes, and the one the word requirement could not see.

    `_BARE_PROPOSAL` needs the word in front of each number, so a list was a citation of
    its head: the failure message named one number while the row cited two, and repairing the
    named one turned the rule green over a row still resting on the other. The two M5 rows carry
    exactly this spelling -- `decision-doc proposals 107, 109` and `proposals 135, 104` -- so the
    milestone with no plan document to argue with is the one where it would have bitten.
    [decision 295; M4.16 cycle 2, M416-C2-COV-04]
    """
    for spelling, numbers in (
        ("proposals 107, 109", "107, 109"),
        ("proposals 74/75", "74, 75"),
        ("proposals 58, 60 and 68", "58, 60, 68"),
    ):
        offenders = _bare_proposal_citations([_synthetic(spec=f"drag-and-drop ({spelling})")])
        assert offenders and numbers in offenders[0], (spelling, offenders)
    # And the escape still covers the whole list rather than its head alone: a row saying out loud
    # that it rests on two unsettled proposals is a disclosure, and the comma is inside it.
    assert not _bare_proposal_citations(
        [_synthetic(spec="the badge rests only on unsettled proposals 138, 139")]
    )


def test_a_decision_cannot_carry_a_proposal_its_own_entry_never_adopted():
    """The reach assertion the provenance strip rests on, in the idiom of the reader above it.

    `decision N (proposal M)` is the form decision 295 prescribes, and the strip that admits it
    deleted the parenthetical unread for every N from 162 up -- so any real decision number
    laundered any proposal, and the map's own comment said the spelling was closed. The three live
    citations are honest against entry 295 and are measured here rather than assumed, which is
    also what stops a reader that parsed nothing from passing this file in silence.
    [decision 295's second half; M4.16 cycle 2, M416-C2-COV-01]
    """
    for honest in (
        "decision 295 (proposal 71)",
        "decision 295 (proposals 74 and 75)",
        "decision 295 (proposals 157 and 76)",
    ):
        assert _strip_provenance(honest).strip() == "", honest
    # The same decision, carrying a number its entry never mentions.
    assert _strip_provenance("decision 295 (proposal 999)") == "decision 295 (proposal 999)"
    # And a decision the register heads no entry for: 169 is real, taken in ROADMAP-to-M5.md and
    # cited by this map, and it still cannot stand behind an adoption nobody can go and read.
    assert _is_a_decision(169) and 169 not in _register_entries()
    assert _strip_provenance("decision 169 (proposal 3)") == "decision 169 (proposal 3)"
    # A parenthetical carrying no proposal at all is untouched by the substance rule -- three live
    # rows spell one, including `decision 293 (S6.6 / S6.9 Data sources)`, whose section marks are
    # digits to anything reading numbers rather than citations.
    assert _strip_provenance('decision 11 ("the settings control")').strip() == ""


# --- the gate's own failure branch ------------------------------------------------------


def _synthetic(**fields) -> dict:
    """One well-formed row, for the self-test below to spoil a field at a time.

    Well-formed is the load-bearing half: a row that fails two rules proves nothing about the
    one under test, so every case below differs from this row in exactly the field its own rule
    reads. `test_the_synthetic_row_offends_no_rule` holds that.
    """
    row = {
        "id": "synthetic-row-the-gate-feeds-itself",
        "spec": "§0",
        "milestone": "M0",
        "kind": "static",
        "what": "a synthetic requirement, long enough that the well-formedness rule reads it",
        "why": "a gate with no failure branch is documentation",
        "tests": ["backend/tests/test_spec_coverage.py::test_requirement_ids_are_unique"],
    }
    row.update(fields)
    return row


def _an_untracked_test_file(tmp_path: Path) -> list[str]:
    """Rule 3's violation: a real test file, in no index, named by a row."""
    (tmp_path / "test_only_this_machine_has_it.py").write_text(
        "def test_it_asserts_something():\n    assert True\n", encoding="utf-8"
    )
    known = _pytest_ids(frozenset(), root=tmp_path, repo=tmp_path)
    row = _synthetic(tests=["test_only_this_machine_has_it.py::test_it_asserts_something"])
    return _missing_tests([row], known)


@pytest.mark.parametrize(
    ("rule", "offend"),
    [
        (
            "1, a shipped row with no test and no waiver",
            lambda _t: _uncovered([_synthetic(tests=[])]),
        ),
        (
            "2, a named test nothing answers to",
            lambda _t: _missing_tests(
                [_synthetic(tests=["backend/tests/test_spec_coverage.py::test_no_such_thing"])],
                KNOWN_TESTS,
            ),
        ),
        (
            "2, a literal title under the one file that registers a wildcard",
            lambda _t: _missing_tests(
                [_synthetic(tests=["e2e/specs/05-milestones.spec.js::a title it never held"])],
                KNOWN_TESTS,
            ),
        ),
        ("3, a test file git does not list", _an_untracked_test_file),
        (
            "well-formedness, a kind that is not one of the four",
            lambda _t: _malformed([_synthetic(kind="frontend")]),
        ),
        (
            "waivers, a reason too short to disprove",
            lambda _t: _unexplained_waivers([_synthetic(waived="later")]),
        ),
        (
            "4, an integration row proven by a unit test",
            lambda _t: _rows_below_their_kind([_synthetic(kind="integration")], allowed={}),
        ),
        (
            "4, an e2e row proven by a backend test",
            lambda _t: _rows_below_their_kind([_synthetic(kind="e2e")], allowed={}),
        ),
        (
            "5, a requirement resting on a bare proposal",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="§6.3 drag-and-drop (proposal 71)")]
            ),
        ),
        (
            "5, a requirement resting on nothing at all",
            lambda _t: _uncited_authority([_synthetic(spec="because I said so")]),
        ),
        # Review cycle 1, three spellings that discharged rule 5 without gaining an authority.
        # The first is the one a person actually writes, because it needs no new sentence: the
        # word `proposals` becomes `decisions` and the row reads like the eighteen legitimate
        # `decision N` citations two hundred lines away. [decision 295; M4.16-plan.md:542-544]
        (
            "5, a proposal renamed a decision",
            lambda _t: _laundered_decision_citations([_synthetic(spec="decisions 107, 109")]),
        ),
        # The spelling this case was labelled for and did not hold: `decision 1 (proposal 71)` is
        # caught because 1 is not a decision, and cycle 1's comment claimed the other half with
        # it. Both are cases now, and this is the one a real author writes, because 295 is the
        # decision the map already cites. [M4.16 cycle 2, M416-C2-COV-01]
        (
            "5, a decision citation carrying a proposal it never adopted",
            lambda _t: _bare_proposal_citations([_synthetic(spec="decision 295 (proposal 999)")]),
        ),
        (
            "5, a proposal number carried behind a number that is not a decision",
            lambda _t: _bare_proposal_citations([_synthetic(spec="decision 1 (proposal 71)")]),
        ),
        # And the same thing behind a LIST of them, which is the direction the widened head has to
        # keep: reading the head as a list must not mean that any list carries anything. Neither
        # 305 nor 306 names proposal 999, so the parenthetical is not provenance behind an
        # adoption and the row is still resting on a number nobody adopted.
        # [M4.16 cycle 5, M416-C4-COV-09]
        (
            "5, a list of decisions carrying a proposal none of them adopted",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="decisions 305 and 306 (proposal 999)")]
            ),
        ),
        (
            "5, the second number of a list nobody adopted",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="decision 295 (proposals 71, 109)")]
            ),
        ),
        (
            "5, a citation in one clause swallowing the proposal in the next",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="decision 288 retired the header; drag-and-drop (proposal 71)")]
            ),
        ),
        (
            "5, an escape phrase attached to something other than the proposal",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="drag-and-drop, which rests only on the section, and proposal 71")]
            ),
        ),
        # Review cycle 4, and the three spellings the punctuation bind admitted. The first is the
        # case directly above with its two commas deleted -- one ordinary English sentence, no rule
        # offended, and the row is back to resting on proposal 71's 80% credible mass, which is the
        # pre-decision-295 state rule 5 exists to refuse. The second is decision 315's direction: an
        # escape that publishes no debt is a licence rather than a disclosure. The third is why the
        # two are one rule and not two -- decision 315's same-field test is satisfied by the honest
        # half of a sentence whose other half carries an undisclosed number.
        # [decisions 295 and 315; M4.16 cycle 4, M416-C4-COV-01]
        (
            "5, an escape phrase in a clause with no punctuation to stop it",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="drag-and-drop rests only on the section and proposal 71")]
            ),
        ),
        (
            "5, an escape recording no debt against the number it excuses",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="drag-and-drop rests only on proposal 71")]
            ),
        ),
        (
            "5, a disclosed proposal carrying an undisclosed one as a free rider",
            lambda _t: _bare_proposal_citations(
                [_synthetic(spec="the badge rests only on unsettled proposal 138 and on proposal 71")]
            ),
        ),
        # Review cycle 2. Rule 5 matched the two characters `§6` and asked nothing further,
        # so a citation of a section v2.1 has never had shipped on this milestone's one new
        # surface; and a `spec` quoting a document was never read against the document, so a row
        # went on quoting a sentence this same wave had deleted. Rule 7 read the waiver's LENGTH
        # and never the tests its reason leans on.
        # [M4.16 cycle 2: SPEC-C2-03, M416-C2-CG-03, M416-C2-COV-03]
        (
            "5, a section the normative file has no heading for",
            lambda _t: _unresolvable_sections(
                [_synthetic(spec="decision 293 (§6.9 Data sources)")], _normative_sections()
            ),
        ),
        (
            "5, a citation anchored on a line number",
            lambda _t: _line_anchored_citations(
                [_synthetic(spec="docs/TESTING.md:103 (the kind rule)")]
            ),
        ),
        (
            "5, a sentence quoted from a document that does not carry it",
            lambda _t: _unresolvable_quotations(
                [_synthetic(spec='docs/TESTING.md ("a sentence this ledger has never held")')]
            ),
        ),
        (
            "7, a waiver leaning on a test the map registers nowhere",
            lambda _t: _waivers_leaning_on_unregistered_tests(
                [_synthetic(waived="the privilege half needs a second DB role, which no "
                                   "migration creates; the schema half is asserted by "
                                   "test_nothing_in_this_map_registers_this_one")]
            ),
        ),
        # Review cycle 3, and it is the third spelling decision 305's own entry names. The two
        # above it were closed by the same argument a wave earlier; this one admitted every
        # integer at or above 162 without opening a file. 230 is the case to be afraid of: the
        # register says 228-233 "were reserved and never spent", so a transposition lands there
        # without looking wrong. [decision 305; M4.16 cycle 3, M416-C3-COV-01]
        (
            "5, a decision number nobody has taken",
            lambda _t: _unresolvable_decision_citations([_synthetic(spec="decision 999")]),
        ),
        (
            "5, a decision number the register reserved and never spent",
            lambda _t: _unresolvable_decision_citations(
                [_synthetic(spec="decision 230 (the queue's straddle band)")]
            ),
        ),
        # Review cycle 4, and the same shape one escape over: the two section conventions were
        # written as prefixes, so the rule that refuses a citation a reader cannot resolve forgave
        # a transposition of §5.4 and a risk §14's list does not hold. Both were measured green
        # through every reader in this module. [M4.16 cycle 4, M416-C4-COV-07]
        (
            "5, a section number that only begins like the fold the rule forgives",
            lambda _t: _unresolvable_sections(
                [_synthetic(spec="§547 the section nobody wrote")], _normative_sections()
            ),
        ),
        (
            "5, a numbered risk past the end of the list section 14 holds",
            lambda _t: _unresolvable_sections(
                [_synthetic(spec=f"§14.{_numbered_risks() + 1} the risk nobody wrote")],
                _normative_sections(),
            ),
        ),
    ],
)
def test_every_rule_this_gate_enforces_can_fail(tmp_path, rule, offend):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    The gate has read this map since M0 and not one of its rules had ever been watched refuse
    anything. A rule quietly weakened -- a regex that stops matching, a counter reading the wrong
    field, an intersection against an empty set -- would have printed a green map for ever, and
    the milestone that found the report undercounting its own waivers found it by reading the two
    lines rather than by any test. That is the defect one altitude above the ones the rules catch,
    and this is the only altitude left to catch it from.

    One case per rule, each a well-formed row spoiled in exactly the field its rule reads.
    [M4.16, test-06 (4)]
    """
    assert offend(tmp_path), f"rule {rule} was handed its own violation and said nothing"


def test_the_join_key_ban_kept_the_guard_its_deleted_row_left_behind():
    """A row deleted WITH its code is the honest outcome; a rule left behind by one is not.

    Decision 291 deleted `data-rules-ml-link-imdb-join-is-unambiguous` together with
    `_resolve_ml_links` and its imdb_id join -- correctly, because the join it was written about
    stopped existing. What survived the deletion is §4.1's canonical-key sentence, which is still
    normative, and its only remaining enforcement:
    `test_load_mapping.py::test_no_loader_path_joins_titles_on_imdb_id`, which rejects the join
    coming back under any name. That test appeared in this map ONLY inside the deletion comment --
    never in a `tests` list -- so rule 2 could not hold it, and the comment pointed a later reader
    at a row id that has never existed. Rename the test and the build stays green with §4.1's ban
    enforced by nothing, which is the outcome the deletion comment says it is trying to avoid.
    [decision 291; M4.16 cycle 1, M416-291-04]
    """
    survivor = "backend/tests/test_load_mapping.py::test_no_loader_path_joins_titles_on_imdb_id"
    holders = [
        r["id"] for r in REQUIREMENTS
        if _at_or_before(r["milestone"]) and survivor in (r.get("tests") or [])
    ]
    assert holders, (
        f"no shipped row names {survivor}, so §4.1's \"`imdb_id` ... must never be the join key\" "
        "has nothing holding it: rule 2 only fails over tests a row NAMES, and a rule named only "
        "in a comment is a rule the next rename deletes silently."
    )


# --- M5.1 review cycle 4: the mount assertions rule 2 can only hold once a row names them -------
#
# The guard above is that rule stated of one id, and this is the same rule stated of a reading,
# because the typed form is what let the class recur. `platform-every-declared-router-is-mounted`
# names the two guards that compare PATH SETS and did not name the third, which asserts the mount
# itself -- and rule 2 walks the map's `tests` lists, so a test no row names can be renamed or
# deleted with this whole gate green. The third is not a spare: `api/events.py` declares no paths
# (decision 332), `_unmounted_routers` filters on `paths - served` being non-empty, and an empty
# set is never non-empty, so nothing else in the tree can see `app.include_router` go missing for
# it. M5.2 adds the webhook by adding a route to that file, and would discover the mount gone
# instead of inheriting it.
#
# READ OFF THE SOURCE rather than out of a list here, for the reason the row itself is not enough:
# a list of ids inside a guard goes stale exactly the way a `tests` list does. `_unmounted_routers(`
# and `original_router` are what an assertion about what the APPLICATION mounts is written with.
# `include_router` is deliberately not in the set -- a test that mounts a router of its own is the
# scaffold clause of the same row, and its guards are a different claim about a different subject.
# [decision 332; M5.1 review cycle 4, M51-C4-COV-01]
_MOUNT_WITNESS = re.compile(r"_unmounted_routers\(|original_router")
STATIC_CONTRACTS = TESTS / "test_static_contracts.py"


def _mount_guards_no_row_names(named: set[str] | None = None) -> list[str]:
    """Every test in `test_static_contracts.py` asserting a mount that no shipped row names."""
    source = STATIC_CONTRACTS.read_text(encoding="utf-8")
    if named is None:
        named = {
            test_id
            for r in REQUIREMENTS
            if _at_or_before(r["milestone"])
            for test_id in (r.get("tests") or [])
        }
    found = [
        f"backend/tests/test_static_contracts.py::{node.name}"
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and _MOUNT_WITNESS.search(ast.get_source_segment(source, node) or "")
    ]
    return [test_id for test_id in found if test_id not in named]


def test_every_guard_over_a_router_this_app_mounts_is_named_by_a_row():
    """The one assertion in this tree that a zero-path router is mounted was held by nothing.

    Rule 2 refuses a row naming a test that does not exist; NOTHING refuses a test that exists and
    no row names, and the difference is invisible while the test passes. The map's own `why` for
    `platform-the-suite-says-whether-the-integration-layer-ran` records this shape from an earlier
    cycle -- "the one test this milestone left registered on no row" -- which is why it is stated
    here as a rule over the file rather than repaired one id at a time.
    [M5.1 review cycle 4, M51-C4-COV-01]
    """
    unheld = _mount_guards_no_row_names()
    assert not unheld, (
        "these tests assert that the application mounts a router and no shipped row names them, "
        "so rule 2 cannot see them renamed or deleted: "
        + ", ".join(unheld)
        + ". Register each on platform-every-declared-router-is-mounted, where the guards making "
        "the same claim through the served path set already sit."
    )


def test_the_mount_assertion_reader_finds_the_guards_the_static_file_holds():
    """The other half: a reader matching nothing would pass this file for ever.

    Same argument as `test_the_synthetic_row_offends_no_rule` below -- a guard is two claims, what
    it must say and what it must not. A floor rather than an equality, because the next milestone
    adding a mount assertion is the case this rule exists for and must not have to restate a count
    to add one. [M5.1 review cycle 4, M51-C4-COV-01]
    """
    found = _mount_guards_no_row_names(named=set())
    assert len(found) >= 3, (
        "the mount-assertion reader finds fewer than the three guards test_static_contracts.py "
        "holds over what the application mounts, so the rule above is passing on a file it can "
        f"no longer read: {found}"
    )


def test_the_synthetic_row_offends_no_rule(tmp_path):
    """The other half: a rule that always fires makes all eight cases above vacuous.

    Same argument as `test_the_card_padding_guard_leaves_innocent_files_alone` -- a guard is two
    claims, what it must say and what it must not, and only the pair is worth anything.
    """
    clean = _synthetic()
    assert not _uncovered([clean])
    assert not _missing_tests([clean], KNOWN_TESTS)
    assert not _malformed([clean])
    assert not _unexplained_waivers([clean])
    assert not _rows_below_their_kind([clean], allowed={})
    assert not _bare_proposal_citations([clean])
    assert not _uncited_authority([clean])
    assert not _unresolvable_sections([clean], _normative_sections())
    assert not _unresolvable_quotations([clean])
    assert not _line_anchored_citations([clean])
    assert not _waivers_leaning_on_unregistered_tests([clean])
    assert not _laundered_decision_citations([clean])
    assert not _unresolvable_decision_citations([clean])
    # And the two live spellings the new reader must leave alone: a decision headed only in the
    # roadmap, and one below 162, which is `_laundered_decision_citations`' range and not this
    # rule's. [M4.16 cycle 3, M416-C3-COV-01]
    assert not _unresolvable_decision_citations([_synthetic(spec="decision 169 (ending a room)")])
    assert not _unresolvable_decision_citations([_synthetic(spec="§4.1 rule 5 (decision 18)")])
    # And the two forms rule 5 is written to admit, which the cases above would otherwise let
    # drift into offences: a decision carrying its own provenance, and the disclosure that says
    # out loud what the row rests on. Both are live spellings in the map. [decision 295]
    assert not _bare_proposal_citations([_synthetic(spec="decision 295 (proposal 71)")])
    assert not _bare_proposal_citations([_synthetic(spec="decision 295 (proposals 74 and 75)")])
    assert not _bare_proposal_citations([_synthetic(spec="decision 295 (proposals 157 and 76)")])
    # And the two spellings that combine the map's OTHER live form -- a citation of several
    # decisions -- with provenance. The strip required a single number in front of the
    # parenthetical, so both of these were reported as resting on a bare proposal that decision 295
    # adopted in so many words, and the author told to write what they had written. That is how a
    # rule gets widened back to the shape cycle 1 narrowed. [M4.16 cycle 5, M416-C4-COV-09]
    assert not _bare_proposal_citations([_synthetic(spec="decisions 295, 305 (proposal 71)")])
    assert not _bare_proposal_citations([_synthetic(spec="decisions 295 and 305 (proposal 71)")])
    assert not _bare_proposal_citations(
        [_synthetic(spec="the badge rests only on unsettled proposal 138")]
    )
    # Decision 315's OTHER direction, asked for by name: a legitimate citation that happens to
    # carry a number in its prose must pass. This is the half a tightening breaks first -- rule 5
    # reads the word standing in front of a number and not the numbers in a sentence, and a bind
    # that reddened these would be narrowed by the first person it stopped, which is how a rule
    # stops being one. Measured against the escape's new adjacency bind rather than asserted.
    # [decision 315; M4.16 cycle 4, M416-C4-COV-01]
    for carries_a_number in (
        "§4.3's 983 genome columns are zero-imputed for every title (decision 291)",
        "decision 295 (proposal 71), and §6.3's 80% credible mass with 14 rows behind it",
        "the badge rests only on unsettled proposals 138, 139, and §6.3 carries 2 of its clauses",
    ):
        assert not _bare_proposal_citations([_synthetic(spec=carries_a_number)]), carries_a_number
    assert not _laundered_decision_citations([_synthetic(spec="§4.1 rule 5 (decision 18)")])
    (tmp_path / "test_tracked_here.py").write_text(
        "def test_it_asserts_something():\n    assert True\n", encoding="utf-8"
    )
    tracked = frozenset({"test_tracked_here.py"})
    assert _pytest_ids(tracked, root=tmp_path, repo=tmp_path) == {
        "test_tracked_here.py::test_it_asserts_something"
    }

# --- the report ------------------------------------------------------------------------


def _report_lines(requirements: list[dict] | None = None) -> list[str]:
    """One line per milestone, as the report prints them.

    A function rather than a block inside `test_report` because the ledger guard below reads
    the same lines back out of `docs/TESTING.md`: a second copy of this formatting would let
    the document agree with a formatter nothing else uses. `requirements` defaults to the map
    and is an argument for the same reason every rule above became one -- the counters below
    were wrong for four milestones and nothing could be handed a row to prove it.
    """
    by_milestone: dict[str, list[dict]] = {}
    for r in REQUIREMENTS if requirements is None else requirements:
        by_milestone.setdefault(r["milestone"], []).append(r)

    lines = []
    for milestone in MILESTONES:
        rows = by_milestone.get(milestone, [])
        # A waived row counts as waived whether or not it also names tests, and never as
        # covered. The old pair asked `waived and not tests`, so the honest and common case -- a
        # requirement half of which can be asserted today, waived with a sentence saying which
        # half -- printed as fully covered with no marker at all, and the one mechanism built to
        # make partial coverage visible lost exactly the partial cases. docs/TESTING.md pastes
        # this block, so the document and the instrument agreed on the same wrong number and
        # neither could catch the other. [M4.16, ddocs-05]
        covered = sum(1 for r in rows if r.get("tests") and not r.get("waived"))
        waived = sorted(r["id"] for r in rows if r.get("waived"))
        # ASCII markers: this prints to whatever console the developer has, and a Windows
        # cp1252 terminal turns a decorative glyph into a crash. The ids ride on the milestone's
        # own line rather than under it, because the ledger guard below compares this block with
        # the one docs/TESTING.md publishes line for line -- and they are printed at all because
        # M4.16's criterion is that no standing waiver survives whose premise a grep disproves,
        # and nobody greps a premise they were never shown.
        marker = ">" if _at_or_before(milestone) else " "
        note = f" ({len(waived)} waived: {', '.join(waived)})" if waived else ""
        lines.append(f"  {marker} {milestone}  {covered:>3}/{len(rows):<3} covered{note}")
    return lines


def test_a_waived_row_that_also_names_tests_still_prints_as_waived():
    """The defect this counter had, fed to the counter.

    `waived and not tests` lost exactly the honest case: a requirement half of which can be
    asserted today, waived with a sentence saying which half, named the tests for the half it
    has and printed as fully covered with no marker. The map's own retired waiver was that
    shape -- one e2e test and a waiver saying the other half was unasserted -- so the report
    read `34/35 covered (1 waived)` over a tree in which the honest reading was 33 and 2.

    The ids are asserted with the count, because a waiver is a sentence to be re-read rather
    than a number to be accepted, and M4.16's criterion is that no standing waiver survives
    whose premise a grep disproves. ASCII too: this line prints to a Windows console.
    [M4.16, ddocs-05, ti-coverage-report-undercounts-standing-waivers]
    """
    partial = _synthetic(
        id="half-of-this-one-can-be-asserted",
        milestone="M0",
        waived="the privilege half needs a second DB role, which no migration creates",
    )
    line = next(line for line in _report_lines([partial]) if line.split()[1] == "M0")
    assert "0/1" in line, line
    assert "(1 waived: half-of-this-one-can-be-asserted)" in line, line
    assert line.isascii(), line


def test_report(capsys):
    """Not an assertion - the map's current state, printed with -s so it is readable."""
    with capsys.disabled():
        print("\n".join([f"\nspec coverage - current milestone {CURRENT}", "", *_report_lines()]))


def test_the_testing_ledger_publishes_the_counts_the_gate_prints():
    """`docs/TESTING.md`'s "Current state" block is this report, or it is a number nobody ran.

    CLAUDE.md sends readers to that file for milestone status "rather than assuming status", so
    a count published there is read as measured. M4.8's own block said `M4.8 9/9` while this
    file printed `10/10`: it was pasted from a run made before review cycle 1 added the tenth
    row, and nothing compared the two -- so a later milestone auditing that no milestone had
    lost a test would have reconciled against a baseline one row low, and deleting the tenth
    row would have made the document true. Decision 184 refuses to invent a number in that
    ledger; this refuses to leave one there that a run has since overtaken.
    [M4.8 review cycle 2: m48-rev2-testing-ledger-publishes-a-count-the-instrument-does-not-print]
    """
    block = re.search(
        r"### Current state\s*\n+```\n(.*?)\n```", LEDGER.read_text(encoding="utf-8"), re.S
    )
    assert block, "docs/TESTING.md has no `### Current state` block for the ledger to publish"
    published = block.group(1).splitlines()
    # The block is the report with the two-space indent stripped, which is how it is pasted.
    printed = [line.removeprefix("  ") for line in _report_lines()]
    drift = [
        f"  line {i + 1}: published {p!r}, printed {q!r}"
        for i, (p, q) in enumerate(zip_longest(published, printed))
        if p != q
    ]
    assert not drift, (
        "docs/TESTING.md's ledger no longer matches this file's report. Re-paste the block "
        "`pytest backend/tests/test_spec_coverage.py -q -s` prints:\n" + "\n".join(drift)
    )


# The number words the ledger's prose uses. It spells small counts and prints the id total as a
# figure, which is the house voice; both spellings are read here so the guard rules on the count
# rather than on how the sentence chose to write it.
_NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty",
]

# "-- 84 ids across twelve pytest files and two e2e specs --". One sentence per re-paste: the
# block is rewritten by the milestone it is re-pasted for rather than appended to, so a second
# match means two milestones are both claiming it and the reader cannot tell which run produced
# which figure.
_LEDGER_ID_COUNT = re.compile(
    r"(\d+|[A-Za-z]+) ids across (\d+|[A-Za-z]+) pytest files? and (\d+|[A-Za-z]+) e2e specs?"
)


def _spelled(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    # "no e2e spec" is how the ledger writes zero when a milestone closed without one, and it is a
    # count like any other. [M4.16 cycle 4, M416-C4-LEDGER-01]
    if token.lower() == "no":
        return 0
    return _NUMBER_WORDS.index(token.lower()) if token.lower() in _NUMBER_WORDS else None


def _milestone_ids(milestone: str) -> list[int]:
    """[distinct ids, pytest files, e2e spec files] one milestone's rows name, off the live map."""
    ids = {t for r in REQUIREMENTS if r["milestone"] == milestone for t in r.get("tests", [])}
    files = {t.partition("::")[0] for t in ids}
    return [
        len(ids),
        len({f for f in files if f.startswith("backend/tests/")}),
        len({f for f in files if f.startswith("e2e/specs/")}),
    ]


def test_the_testing_ledger_counts_the_ids_the_map_actually_holds():
    """The sibling above holds the fenced block; this holds the sentence beside it.

    Both publish a measurement and only one of them was mechanical, so the prose drifted exactly
    as the block had before M4.8: `docs/TESTING.md` said "84 ids across twelve pytest files and
    two e2e specs" for a map that held 89, because five tests were registered by a review cycle
    after the paragraph was written and nothing compared the two. The failure is the one decision
    184 legislates against from the other side -- it refuses to invent a count that no run
    produced, and this refuses to leave one standing that the map has since overtaken -- and it
    matters because CLAUDE.md sends the next reader to this file "rather than assuming status":
    an auditor checking whether a review cycle's tests were registered counts the map, reads the
    ledger, and cannot tell a stale sentence from ids added with no row to hold them.

    Scoped to `current_milestone` because the paragraph is always about the milestone the block
    was last re-pasted for, and that is what raising `current_milestone` means.
    [M4.11 review cycle 2: m411-c2-ledger-02]
    """
    matches = _LEDGER_ID_COUNT.findall(LEDGER.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"docs/TESTING.md publishes {len(matches)} 'N ids across ... pytest files and ... e2e "
        f"specs' sentences; the block is re-pasted per milestone, so exactly one is owed"
    )
    published = [_spelled(token) for token in matches[0]]
    assert None not in published, (
        f"a count in that sentence is neither a figure nor a number word: {matches[0]}"
    )

    held = _milestone_ids(CURRENT)
    assert published == held, (
        f"docs/TESTING.md's {CURRENT} paragraph publishes "
        f"{published[0]} ids across {published[1]} pytest files and {published[2]} e2e specs; "
        f"the map holds {held[0]} distinct ids across {held[1]} pytest files and {held[2]} e2e "
        "specs. Restate the sentence -- a count nobody re-derived is decision 184's defect."
    )


# The same sentence after its milestone closes, with "across" traded for "in". That trade is not
# cosmetic and was never meant to be a discharge: the guard above asserts exactly ONE match of the
# guarded form in the whole file and compares it with `current_milestone`'s map, so a milestone
# handing the form on has to spell its own figure some other way -- and the demoted spelling was
# then read by nothing at all. M4.14's said "134 ids in fourteen pytest files and one e2e spec"
# over a map holding 138, and the four extra were added TO M4.14'S OWN ROWS by M4.16, so the
# sentence did not go stale by neglect: a later milestone moved the thing it counts. Five of the
# six figures the ledger publishes re-derived exactly when this was written; the sixth is what a
# demoted figure is worth when nothing holds it.
#
# Read against the milestone whose BLOCK it sits in rather than against `current_milestone`, which
# is the whole difference: each block opens with its own name in bold at the start of a line, and
# the figure belongs to the block it is inside. [decision 184; M4.16 cycle 4, M416-C4-LEDGER-01]
_LEDGER_DEMOTED_COUNT = re.compile(
    r"(\d+|[A-Za-z]+) ids in (\d+|[A-Za-z]+) pytest files? and (\d+|[A-Za-z]+) e2e specs?"
)
_LEDGER_BLOCK = re.compile(r"^\*\*(M\d+(?:\.\d+)?)\b", re.M)


def test_the_testing_ledger_counts_the_ids_of_every_milestone_it_publishes():
    """The guard above, for the milestones that are no longer current.

    CLAUDE.md sends the next reader to this file "rather than assuming status", and what that
    reader does with a shipped milestone's block is reconcile it against the map to see whether a
    review cycle's tests were registered. They count 138 and read 134, and cannot tell four ids
    added with no row to hold them from a sentence a later milestone overtook. Every one of those
    blocks is a published measurement; a count nobody re-derives is a measurement nobody made
    (decision 184), and "the milestone that published it has closed" is not a reason it stopped
    being about this map. It is still about this map -- which is exactly how M4.16 could move it.
    [decision 184; M4.16 cycle 4, M416-C4-LEDGER-01]
    """
    text = LEDGER.read_text(encoding="utf-8")
    blocks = [(match.start(), match.group(1)) for match in _LEDGER_BLOCK.finditer(text)]
    assert blocks, (
        "docs/TESTING.md no longer opens a milestone's block with its own name in bold at the "
        "start of a line, so this guard cannot tell which map a published figure is about"
    )
    figures = list(_LEDGER_DEMOTED_COUNT.finditer(text))
    assert figures, (
        "docs/TESTING.md publishes no 'N ids in ... pytest files and ... e2e specs' sentence for "
        "any milestone that has closed, so this guard is reading nothing. If the ledger has "
        "stopped publishing those figures, it comes out together with this guard."
    )
    drift = []
    for figure in figures:
        owner = [name for start, name in blocks if start < figure.start()]
        line = text[: figure.start()].count("\n") + 1
        if not owner:
            drift.append(f"line {line}: {figure.group(0)!r} sits above every milestone block")
            continue
        published = [_spelled(token) for token in figure.groups()]
        if None in published:
            drift.append(f"line {line}: {figure.group(0)!r} is neither figures nor number words")
            continue
        held = _milestone_ids(owner[-1])
        if published != held:
            drift.append(
                f"line {line}: the {owner[-1]} block publishes {published[0]} ids in "
                f"{published[1]} pytest files and {published[2]} e2e specs; the map holds "
                f"{held[0]}, {held[1]} and {held[2]}"
            )
    assert not drift, "\n  ".join(
        ["docs/TESTING.md publishes a milestone count the map has overtaken:", *drift]
    )

# "...and the count includes the seventeen vitest ids". The figure the guard above holds is the
# ALL-LAYER total and the clause beside it names two of the three, so a reader reconciling it
# against the twelve pytest files and the one e2e spec is short by exactly the vitest ids. That is
# how M4.14's paragraph came to read "118 ids across twelve pytest files and one e2e spec, with
# seventeen vitest ids beside them" over a figure that already held those seventeen: described
# once inside the count and once as sitting next to it. The guard above cannot see it -- it
# compares the figure with `len(ids)` over all three layers and the file counts over two, so both
# halves pass while they are about different sets. What is held here is the disclosure M4.12's
# paragraph made and M4.14's dropped, that the count INCLUDES them, and the number with it:
# decision 226 admits a vitest id as supporting evidence beside a backend or Playwright test and
# never instead of one, which is the distinction a reader cannot draw without knowing how many of
# the published total are which. Matched word by word, because the ledger wraps its prose and
# this clause is as likely to arrive with a newline inside it as not.
_LEDGER_VITEST_COUNT = re.compile(r"the\s+count\s+includes\s+the\s+(\d+|[A-Za-z]+)\s+vitest\s+ids")


def _ledger_paragraph(pattern: re.Pattern) -> str:
    """The blank-line-delimited block `pattern` matches in -- one re-paste of the banner.

    Scoped to the paragraph rather than the file because the ledger keeps every earlier
    milestone's block below the current one, and M4.12's still carries its own disclosure in the
    demoted "ids in ten pytest files" form.
    """
    text = LEDGER.read_text(encoding="utf-8")
    match = pattern.search(text)
    assert match, f"docs/TESTING.md publishes no sentence matching {pattern.pattern!r}"
    start = text.rfind("\n\n", 0, match.start()) + 2
    end = text.find("\n\n", match.end())
    return text[start: end if end != -1 else len(text)]


def test_the_testing_ledger_says_the_vitest_ids_are_inside_the_figure_it_publishes():
    """The second ledger guard's blind spot: one figure over three layers, two of them named.

    `docs/TESTING.md` published "**118 ids across twelve pytest files and one e2e spec**, with
    seventeen vitest ids beside them" for a map whose 118 is 100 backend ids, 17 vitest ids and
    one Playwright id -- so the thirteen files the sentence names hold 101, and an auditor doing
    the obvious reconciliation (open the twelve pytest files, count the registered ids) is
    seventeen short with no way to tell a stale figure from ids registered in files the sentence
    does not name. Decision 226's point is exactly the distinction the wording blurred: a vitest
    id is supporting evidence BESIDE a backend or Playwright test, never instead of one, and
    "beside them" said that of the figure instead.

    The repair is the disclosure M4.12's own paragraph carried one milestone earlier -- "and the
    count includes the vitest ids" -- with the number added, because a count published alone is
    decision 184's defect and this is the one layer figure nothing else in this file re-derives.
    Read out of the paragraph the figure sits in, so a later milestone re-pasting the banner
    restates its own count rather than inheriting M4.14's.
    [M4.14 cycle 3, m414-c3-rec-07]
    """
    beside = sorted(
        test_id
        for requirement in REQUIREMENTS
        if requirement["milestone"] == CURRENT
        for test_id in requirement.get("tests", [])
        if not test_id.startswith(("backend/tests/", "e2e/specs/"))
    )
    claims = _LEDGER_VITEST_COUNT.findall(_ledger_paragraph(_LEDGER_ID_COUNT))

    if not beside:
        assert not claims, (
            f"docs/TESTING.md's {CURRENT} paragraph discloses vitest ids inside its figure and "
            "the map registers none on this milestone"
        )
        return

    assert len(claims) == 1, (
        f"docs/TESTING.md's {CURRENT} paragraph publishes an id figure that includes "
        f"{len(beside)} vitest id(s) and makes {len(claims)} 'the count includes the N vitest "
        "ids' disclosures. The figure counts three layers and names two of them, so without that "
        "clause the sentence cannot be reconciled against the files it does name."
    )
    published = _spelled(claims[0])
    assert published == len(beside), (
        f"docs/TESTING.md's {CURRENT} paragraph says the count includes {claims[0]} vitest ids "
        f"and the map holds {len(beside)}: {beside}. Restate it -- a count nobody re-derived is "
        "decision 184's defect."
    )


# "**Twelve rows the map already had were amended in place rather than duplicated,**" is the one
# count in that block no field of this map holds. The two guards above re-derive what they check
# from `REQUIREMENTS`; "amended" is a fact about a diff, and the diff stops existing the moment the
# milestone commits -- `HEAD:spec_coverage.toml` is then the amended file itself, so anything
# git-shaped would read zero forever and every historical banner with it. What does survive is an
# enumeration: a count published beside the ids it counts stays checkable at any later date, and a
# count published alone is exactly the number decision 184 refuses. So a banner making this claim
# owes the list, and the list is what this reads back against the map.
# Four alternations where the banner used to have one spelling, because a milestone that
# amended ONE row cannot write this sentence in the plural and had its claim read by nothing.
# The guard below that pair holds the sentence to this form. [M5.1 review cycle 1, M51-REV-REG-03]
_LEDGER_AMENDED = re.compile(
    r"\*\*(\d+|[A-Za-z]+) rows? the map already (?:had|carried) (?:were|was) amended in place"
)
_LEDGER_AMENDED_LIST = "named so an auditor can check each rather than take the count:"
# The ledger wraps its prose, so the phrase introducing the list is as likely to arrive with a
# newline in it as not: matched word by word, and the plain string above is what the failure
# tells the editor to write.
_LEDGER_AMENDED_MARK = re.compile(r"\s+".join(map(re.escape, _LEDGER_AMENDED_LIST.split())))
# A row id is lowercase words joined by hyphens. A test id carries `::` and underscores and a
# milestone carries a digit after `M`, so nothing else the paragraph backticks can be read as one.
_LEDGER_ROW_ID = re.compile(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`")


def test_the_testing_ledger_names_the_rows_it_says_it_amended():
    """The fourth ledger guard: the rows a milestone changed rather than the rows it added.

    `docs/TESTING.md` published "Ten rows the map already had were amended in place" over a tree
    in which eleven were, and the eleventh -- M4.9's rail row, gained in review cycle 1 -- is
    disclosed by name one paragraph above, so the document held the fact while the count beside it
    disagreed. An auditor reconciling which rows a milestone touched counts eleven in this file,
    reads ten in the ledger, and cannot tell a stale sentence from a row amended without being
    recorded: the ambiguity the sibling guards exist to remove, and the same class as M4.8's
    `M4.8 9/9` beside a printed `10/10`.

    The repair is not a bigger number but a checkable one. Nothing in the tree re-derives "was
    amended" once the milestone is committed, so the banner names the ids it counts and this holds
    the two together: the count is the length of its own list, every id in it is a row the map
    holds, none of them sits on `current_milestone` -- a row there is one the milestone ADDED, and
    the block publishes that count separately -- and each names at least one test, which is the
    banner's other clause. [M4.14 review cycle 2: m414-c2-dim-record-03]
    """
    by_id = {r["id"]: r for r in REQUIREMENTS}
    for banner in LEDGER.read_text(encoding="utf-8").split("\n\n"):
        headline = _LEDGER_AMENDED.search(banner)
        if not headline:
            continue
        published = _spelled(headline.group(1))
        assert published is not None, (
            "an amended-rows count in docs/TESTING.md is neither a figure nor a number word: "
            f"{headline.group(1)!r}"
        )
        marker = _LEDGER_AMENDED_MARK.search(banner)
        assert marker, (
            f"docs/TESTING.md publishes '{published} rows the map already had were amended in "
            "place' and names none of them. Nothing re-derives that count once the milestone is "
            "committed, so a stale number cannot be told from a row amended and never recorded -- "
            "which is how 'Ten' outlived an eleventh row. List the ids in that paragraph after "
            f"'{_LEDGER_AMENDED_LIST}'."
        )
        named = _LEDGER_ROW_ID.findall(banner[marker.end() :])
        twice = sorted({i for i in named if named.count(i) > 1})
        assert not twice, f"docs/TESTING.md names the same amended row twice: {twice}"
        unknown = [i for i in named if i not in by_id]
        assert not unknown, (
            f"docs/TESTING.md names amended rows this map does not hold: {unknown}. An id renamed "
            "out from under the ledger is a count nobody can reconcile again."
        )
        added = [i for i in named if by_id[i]["milestone"] == CURRENT]
        assert not added, (
            f"docs/TESTING.md counts rows on {CURRENT} among the ones it amended in place: "
            f"{added}. A row on the current milestone is one this milestone ADDED, and the ledger "
            "publishes those two counts separately."
        )
        bare = [i for i in named if not by_id[i].get("tests")]
        assert not bare, (
            "docs/TESTING.md says every amended row now names the tests that assert the clause it "
            f"gained, and these name none: {bare}"
        )
        assert published == len(named), (
            f"docs/TESTING.md publishes {published} amended rows and names {len(named)}: {named}. "
            "Restate the count against the list -- a number nobody re-derived is decision 184's "
            "defect, and this one was ten over an eleventh row."
        )


# The sentence in any spelling, which is the population the banner above is a subset of. A banner
# is bold, plural and says "had ... were", and M5.1 amended ONE row: there is no grammatical way to
# write that claim in the guarded form -- "One rows the map already had were amended" is the only
# sentence the old pattern matched -- so the milestone wrote the truth, named the row and disclosed
# in its own text that it sat outside the form. All five clauses then ran on nothing: the id
# `platform-compose-http-port-and-volumes` could have been renamed out from under the ledger, or
# been a row on `current_milestone` (which the block counts separately), and the document would
# have gone on reading as recorded. M5.2 through M5.7 are each sized to amend about one row, so the
# blind spot was about to be six claims wide. The banner is widened to the singular rather than the
# prose bent to the plural, which is this tree's own idiom: a row is repaired by widening its guard
# rather than by narrowing its claim.
#
# SCOPED TO `current_milestone`, and the limit is named rather than left to be discovered. Two
# older blocks state the claim outside any banner -- M4.16's "Two rows the map already had are
# amended in place" and M4.13's "Seven rows ... were amended in place", both unbolded -- and
# bringing them in scope means retro-fitting id lists into closed milestones' blocks, which is a
# different change from this one. What this holds is that the milestone whose block is being
# written NOW states it in the form the guard reads. [M4.14's m414-c2-dim-record-03 guard;
# M5.1 review cycle 1, M51-REV-REG-03]
_LEDGER_AMENDED_ANY = "amended in place"


def test_the_amended_rows_claim_of_the_current_milestone_is_read_by_its_guard():
    """The guard above is a conditional, and a claim it cannot see satisfies it for free.

    `docs/TESTING.md` said "One row the map already carried was amended in place rather than
    duplicated, named here rather than counted in the banner form the blocks below use, because
    that form is a plural and this is one row" -- true, complete, and matched by nothing. The
    count-against-list, the ids-are-rows-this-map-holds, the none-of-them-is-on-current-milestone
    and the each-names-a-test clauses all skipped, so the one protection against an id renamed out
    from under the ledger was not running. CLAUDE.md sends the next reader to this file "rather
    than assuming status", and what that reader does with the sentence is reconcile it against the
    map. [M5.1 review cycle 1, M51-REV-REG-03]
    """
    text = LEDGER.read_text(encoding="utf-8")
    blocks = [(match.start(), match.group(1)) for match in _LEDGER_BLOCK.finditer(text)]
    opens = [start for start, name in blocks if name == CURRENT]
    assert len(opens) == 1, (
        f"docs/TESTING.md opens {len(opens)} blocks named {CURRENT} at the start of a line in "
        "bold, and this guard reads the one the milestone is writing now"
    )
    start = opens[0]
    end = min([s for s, _ in blocks if s > start], default=len(text))
    unread = [
        paragraph.strip().splitlines()[0]
        for paragraph in text[start:end].split("\n\n")
        if _LEDGER_AMENDED_ANY in paragraph and not _LEDGER_AMENDED.search(paragraph)
    ]
    assert not unread, (
        f"docs/TESTING.md's {CURRENT} block claims a row was amended in place in a form the guard "
        "over that claim cannot read, so the count, the ids and the tests behind it are checked by "
        "nothing:\n  " + "\n  ".join(unread) + "\nWrite it as the banner: bold, '<count> row(s) "
        f"the map already had/carried was/were amended in place', then '{_LEDGER_AMENDED_LIST}' "
        "and the ids."
    )


def test_the_amended_rows_guard_reads_the_banner_a_one_row_milestone_writes():
    """The sentence M5.1 shipped, in the form the widened pattern asks for.

    Not an invented string: this is the claim that stood in the ledger, rewritten into the banner
    rather than left outside it, and every clause of the guard has to survive the singular. The
    count comes back as a number, the marker is found, and the one id is extracted -- which is the
    whole reading M5.2 through M5.7 will each need. [M5.1 review cycle 1, M51-REV-REG-03]
    """
    banner = (
        "**One row the map already carried was amended in place rather than duplicated,** named "
        "so an auditor can check each rather than take the count: "
        "`platform-compose-http-port-and-volumes`, M0's compose guard."
    )
    headline = _LEDGER_AMENDED.search(banner)
    assert headline, (
        "the amended-rows guard reads a plural banner only, so a milestone that amends ONE row -- "
        "which each of M5.2 through M5.7 is sized to do -- publishes a count, a list and a set of "
        "ids that nothing reconciles against this map"
    )
    assert _spelled(headline.group(1)) == 1
    marker = _LEDGER_AMENDED_MARK.search(banner)
    assert marker, "the singular banner carries the same marker phrase the plural one does"
    assert _LEDGER_ROW_ID.findall(banner[marker.end():]) == [
        "platform-compose-http-port-and-volumes"
    ]


# --- M5.1 review cycle 4: the amended-rows list, re-derived off the map rather than trusted -----
#
# The guard above holds the COUNT to the LIST and cannot hold either to the map, for the reason its
# own docstring gives: "amended" is a fact about a diff, and the diff stops existing the moment the
# milestone commits. That is true of a diff and false of THIS map, because a row another milestone
# changes is changed together with its provenance -- `[M5.1 review cycle 2, M51-REG-JOBS-02]`
# beside the ids it added, or "M5.1 review cycle 1 found ..." inside the `why` it rewrote -- and
# the mark survives the commit exactly as the row does. Measured over the tree this was written
# against: seven rows outside `current_milestone` carried M5.1's mark and the banner named three.
# The four it left out are M4.10's registry row and three of M4.16's, every one of them amended by
# a review cycle -- which is the case the banner's own closing sentence puts inside the count, "a
# review cycle's amendments are amendments like any other", stated and then not applied.
#
# ONE DIRECTION ONLY, and the limit is the point rather than an omission. A marked row must be
# named; a named row need not be marked, because a milestone may change a row without leaving a
# dated provenance line and the banner is still the honest place to say so. This is a floor under
# the list and never a ceiling on it, which is what keeps it from fighting the count guard above
# over rows neither of them can see. An empty mark set is a legitimate answer -- a milestone that
# touched no row it does not own -- and the reader test below is what keeps the reading itself
# exercised in that case, which is the pairing the conditional guard above already needed.
#
# Read from `[[requirement]]` to the next one, which files a trailing comment with the row it
# follows: this map's own layout, and the reason the mark is looked for in the row's SOURCE rather
# than in its parsed fields, since three of the seven carry it in a comment `tomllib` drops.
# [decision 184; M4.14's m414-c2-dim-record-03 guard; M5.1 review cycle 4, M51-C4-LEDGER-01]
_MAP_ROW_HEAD = re.compile(r"^\[\[requirement\]\]\s*$", re.M)
_MAP_ROW_FIELD = re.compile(r'^(id|milestone) = "([^"]+)"', re.M)


def _rows_this_milestone_marked_as_amended() -> dict[str, str]:
    """Every row outside `current_milestone` whose source carries this milestone's review cycle.

    The milestone name is read with a boundary of its own, for `_current_milestone_blocks`' reason
    one file over: `\\b` counts the dot as a boundary, so `M5.1` would match inside `M5.10`.
    """
    text = MAP.read_text(encoding="utf-8")
    mark = re.compile(re.escape(CURRENT) + r"(?![\d.])[^\n]{0,40}?review cycle")
    heads = list(_MAP_ROW_HEAD.finditer(text))
    marked: dict[str, str] = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        block = text[head.start():end]
        fields = dict(_MAP_ROW_FIELD.findall(block))
        hit = mark.search(block)
        if hit is None or "id" not in fields or fields.get("milestone") == CURRENT:
            continue
        line = text[: head.start() + hit.start()].count("\n") + 1
        marked[fields["id"]] = f"{fields.get('milestone', '?')}, spec_coverage.toml:{line}"
    return marked


def _rows_the_current_block_says_it_amended() -> set[str]:
    """The ids every amended-rows banner in `current_milestone`'s own ledger block names."""
    text = LEDGER.read_text(encoding="utf-8")
    blocks = [(match.start(), match.group(1)) for match in _LEDGER_BLOCK.finditer(text)]
    opens = [start for start, name in blocks if name == CURRENT]
    assert len(opens) == 1, (
        f"docs/TESTING.md opens {len(opens)} blocks named {CURRENT} in bold at the start of a "
        "line, and this guard reads the one the milestone is writing now"
    )
    end = min([start for start, _ in blocks if start > opens[0]], default=len(text))
    named: set[str] = set()
    for banner in text[opens[0]:end].split("\n\n"):
        if not _LEDGER_AMENDED.search(banner):
            continue
        marker = _LEDGER_AMENDED_MARK.search(banner)
        if marker:
            named.update(_LEDGER_ROW_ID.findall(banner[marker.end():]))
    return named


def test_the_testing_ledger_names_every_row_this_milestone_marked_as_amended():
    """The floor under that list, and the one thing this map can still prove about a diff.

    `docs/TESTING.md` published "Three rows the map already had were amended in place" over a map
    in which seven rows outside M5.1 carried M5.1's own review-cycle provenance, and the four left
    out are exactly the ones a later auditor cannot reconstruct: three are disclosed only
    obliquely, inside a closed milestone's block as "three of them M5.1's review cycle 3", and the
    fourth sits on M4.10, whose block publishes no id total for any instrument to hold. The count
    guard above stayed green because three equals three, which is the ambiguity every ledger guard
    here exists to remove -- an auditor counts the map, reads the ledger, and cannot tell a stale
    sentence from a row amended and never recorded.

    Scoped to the block being written now, for the reason the conditional guard above is: another
    milestone's block naming a row does not record what THIS one did to it.
    [M4.14 review cycle 2: m414-c2-dim-record-03; M5.1 review cycle 4, M51-C4-LEDGER-01]
    """
    marked = _rows_this_milestone_marked_as_amended()
    named = _rows_the_current_block_says_it_amended() if marked else set()
    unrecorded = sorted(f"{row} ({where})" for row, where in marked.items() if row not in named)
    assert not unrecorded, (
        f"these rows carry a {CURRENT} review cycle's provenance in spec_coverage.toml and no "
        f"amended-rows banner in {CURRENT}'s docs/TESTING.md block names them:\n  "
        + "\n  ".join(unrecorded)
        + "\n\nA row this milestone changed and the ledger does not count is the defect the "
        "banner exists to make checkable, and a count restated without the ids is the half "
        "nobody can re-derive. Add each to the list after "
        f"'{_LEDGER_AMENDED_LIST}' and restate the count against it."
    )


def test_the_amendment_mark_reader_tells_a_review_cycle_from_a_re_pointing():
    """The reader's two ends, on the two shapes this map actually writes.

    A row re-pointed at a sub-milestone says "as M5.1 opened": the milestone key moved and the
    claim did not, and the ledger counts those eleven separately, so it is not an amendment in the
    banner's sense. A row that gained a guard says "M5.1 review cycle N" and is. The distance
    bound is what keeps the two apart, and the boundary is what keeps `M5.10` out.
    [M5.1 review cycle 4, M51-C4-LEDGER-01]
    """
    mark = re.compile(re.escape("M5.1") + r"(?![\d.])[^\n]{0,40}?review cycle")
    assert mark.search("    # protected left alone. [M5.1 review cycle 2, M51-REG-JOBS-02]")
    assert mark.search("# M5.1 review cycle 1 found the same class one document further out")
    assert not mark.search('# Re-pointed from "M5" to M5.4 as M5.1 opened: decision 321 splits')
    assert not mark.search("# `_at_or_before(current_milestone)`, M5.5 sorts after M5.1, and")
    assert not mark.search("# M5.10 review cycle 1 found something else entirely")


# "**21 ids** written against the plan's own rows, **3 ids** registered by the stages ..." -- the
# series the total above decomposes into, each part bolded so the prose can be read as arithmetic.
# The total itself carries "across N pytest files", so the two sentences cannot be confused for
# each other, and a part re-wrapped across a line break stops being read -- which the sum then
# catches, because the parts no longer reach the whole.
_LEDGER_ID_PARTS = re.compile(r"\*\*(\d+) ids?\*\*")


def test_the_testing_ledger_decomposition_sums_to_the_count_it_publishes():
    """The sibling above holds the total; this holds the series a reader reconciles it from.

    The total was honest and the decomposition under it was not: M4.15 published 21 + 3 + 1 + 6 for
    a map that already held 35 by then and 42 now, because the first review cycle registered ten
    ids and the sentence said six -- and the four it left out are the four that brought two further
    pytest files into the count, so the same paragraph also told a reader the wrong cycle bought
    them. That is the defect the guard above was written for, one granularity down: an auditor sent
    here by CLAUDE.md "rather than assuming status" sums the parts, misses the whole by seven, and
    cannot tell a stale sentence from ids registered with no row to hold them.

    Conditional on the ledger making the claim at all. A milestone whose figure never moved owes no
    decomposition and this stays quiet for it; what it may not do is publish a series that does not
    reach its own total. Rounded numerals rather than number words on purpose -- these are addends,
    and the sentence they sit in is doing arithmetic in front of the reader.
    [decision 184; M4.15 review cycle 2: M415-C2-COV-01]
    """
    parts = [int(n) for n in _LEDGER_ID_PARTS.findall(LEDGER.read_text(encoding="utf-8"))]
    if not parts:
        return
    ids = {t for r in REQUIREMENTS if r["milestone"] == CURRENT for t in r.get("tests", [])}
    assert sum(parts) == len(ids), (
        f"docs/TESTING.md decomposes {CURRENT}'s id count as "
        + " + ".join(str(p) for p in parts)
        + f" = {sum(parts)}, and the map holds {len(ids)}. Restate the series from the map: a "
        "reader reconciling the total against these parts is exactly who this paragraph is for, "
        "and a part that no longer adds up tells them nothing about which of the two is stale."
    )


# "...and it takes the last `pytest.skip` out of a registered Tonight test". The qualifier is
# OPTIONAL in this pattern on purpose. The sentence is read for the scope it claims, and the
# guard below then holds exactly that scope: widen the wording back to "a registered test" and
# the guard widens with it. That is the only arrangement in which the claim and the thing it
# claims cannot drift apart -- and the drift is what happened, because the unqualified sentence
# shipped over a tree in which four registered tests still skip, none of them Tonight's.
_LEDGER_LAST_SKIP = re.compile(
    r"takes the last `pytest\.skip` out of a registered\s+(?:(\w+)\s+)?test\b"
)


def _registered_tests_that_skip(scope: str | None) -> list[str]:
    """Every registered backend test that can report a green run having asserted nothing.

    A `pytest.skip` call and a `skipif` marker are one defect with two spellings: rule 2 above
    asks only whether a named test EXISTS, so either one closes its row while the run it closed
    it on executed no assertion. The sentence names the call because the call is what this
    milestone removed; the guard reads both because a maintainer repairing the one and leaving
    the other would have satisfied the letter of a claim about silent coverage.

    `scope` is a substring of the file stem -- "tonight" for the Tonight suite, None for every
    registered backend test. Read with `ast` rather than a regex for the reason the argument
    against `pytest.skip` is made at all: `test_tonight_integration.py:1460` and
    `test_tonight_routes.py:898` both write "pytest.skip" inside a docstring arguing against it,
    and a guard that counted those would be reporting its own documentation as the defect.

    A named file that is not there is not this guard's to report -- `test_every_named_test_exists`
    is the rule for that, and two failures for one cause is one of them mis-diagnosed.
    """
    wanted: dict[str, set[str]] = {}
    for requirement in REQUIREMENTS:
        for test_id in requirement.get("tests", []):
            path, _, name = test_id.partition("::")
            if not path.startswith("backend/tests/") or not name:
                continue
            if scope is not None and scope not in Path(path).stem.lower():
                continue
            wanted.setdefault(path, set()).add(name)

    out: list[str] = []
    for path, names in sorted(wanted.items()):
        source = REPO / path
        if not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in names:
                continue
            for decorator in node.decorator_list:
                if ast.unparse(decorator).startswith("pytest.mark.skip"):
                    out.append(f"{path}::{node.name} carries a skip marker at line {node.lineno}")
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub.func) == "pytest.skip":
                    out.append(f"{path}::{node.name} calls pytest.skip at line {sub.lineno}")
    return sorted(out)


def test_the_testing_ledger_does_not_claim_a_skip_this_milestone_did_not_take():
    """The third ledger guard, and the first about a claim rather than a count.

    Two halves. The first is M4.12's own repair, held permanently and independently of any
    prose: no registered test in the Tonight suite skips. Defect 44's four sites were all in
    that suite -- the integration-layer case of 54d gave up when the fixture stopped dividing
    the household and reported that regression as `1 skipped` and a green file -- and that is
    the property worth keeping true after the paragraph below has been re-pasted for M4.13.

    The second is the sentence. `docs/TESTING.md` published the repair as "it takes the last
    `pytest.skip` out of a registered test", which is a universal, and the universal is false:
    `test_the_default_branch_claim_is_bounded_to_the_run_in_flight` (M4.8) calls `pytest.skip`
    outright, and three more registered tests carry a `skipif` -- the tz test M4.7 registered
    is one, and it skips on this checkout. All four are deliberate and argued, so the defect is
    entirely one of the RECORD: CLAUDE.md sends the next reader to that file "rather than
    assuming status", and an auditor who reads the class as closed stops looking. The two
    milestones that own those tests are not this one's to repair, which is why the repair is to
    scope the sentence rather than to widen the rule.

    The sentence is optional: it belongs to M4.12's paragraph and the paragraph is rewritten by
    the milestone it is re-pasted for. What the guard refuses is the sentence standing with a
    scope the tree does not support -- and because the scope is read out of the prose, a
    maintainer who widens the wording gets the wider rule with it.
    [M4.12 review cycle 2: M412-C2-LEDGER-01]
    """
    survivors = _registered_tests_that_skip("tonight")
    assert not survivors, (
        "a registered Tonight test skips, so a row of this milestone closes on a run that "
        "asserted nothing:\n  " + "\n  ".join(survivors)
    )

    claims = _LEDGER_LAST_SKIP.findall(LEDGER.read_text(encoding="utf-8"))
    assert len(claims) <= 1, (
        f"docs/TESTING.md makes {len(claims)} 'last pytest.skip' claims; the paragraph is "
        "re-pasted per milestone, so at most one milestone can be claiming it"
    )
    for claim in claims:
        scoped = _registered_tests_that_skip(claim.lower() or None)
        assert not scoped, (
            f"docs/TESTING.md claims the last `pytest.skip` was taken out of "
            f"{'a registered ' + claim + ' test' if claim else 'a registered test'}, and "
            f"{len(scoped)} still skip:\n  " + "\n  ".join(scoped)
            + "\n  Scope the sentence to what the milestone did, or name the survivors."
        )


# `## Decisions taken (owner, <date>[, as <milestone> opened])` heads one block of the register and
# `### <n>. <sentence>` heads one decision inside it -- two hashes and three, so the block pattern
# cannot match a decision heading. The register is the primary record and it is not what drifts:
# every decision is present and correct under its block. What drifts is the prose ELSEWHERE that
# publishes the range, which has no author once the block is written.
_REGISTER_BLOCK = re.compile(r"^## Decisions taken \(.*\)\s*$", re.M)
_REGISTER_DECISION = re.compile(r"^### (\d+)\. ", re.M)

# The files that publish a decision RANGE as prose: the ledger's milestone paragraph, the map's
# section header, this file's comment above `MILESTONES`, and the register's own preamble.
# `docs/milestones/*.md` is excluded for the reason test_static_contracts.py gives for the same
# exclusion -- the plan is the plan, the workflow forbids editing it, and a correction owed there
# goes to the owner by hand -- and ROADMAP-to-M5.md is why that exclusion has to be stated rather
# than assumed: it cites a connector's LINE range in this pattern exactly, starting at the number
# M4.15's decisions start at. The first number is what scopes the sweep -- only a range that
# STARTS where this milestone's decisions start is a claim about this milestone -- which is also
# why no range literal is written anywhere else in this file: it would be read as a claim.
_RANGE_PUBLISHERS = (LEDGER, MAP, REGISTER, TESTS / "test_spec_coverage.py")
_PUBLISHED_RANGE = re.compile(r"\b(\d+)-(\d+)\b")

# The register's THIRD spelling of the same claim, and the one that escaped the sweep above by
# writing its endpoint in a phrase instead of a hyphenated pair. The M4.16 block's opening
# sentence named an endpoint three decisions short of the register's own, over blocks it points
# the reader to, and eighteen lines below it the SAME preamble named the right one -- so the
# register contradicted itself about its own range, in the file decision 288 makes normative and
# CLAUDE.md sends the next reader to. A fixed clause rather than a sweep over every way an
# endpoint can be phrased, for `_FAMILY_SIZE`'s reason one file over: a rule over prose is not a
# rule. No literal endpoint is written in this comment, for the reason the paragraph above gives.
# [M4.16 cycle 3: M416-C3-REG-01, M416-C4-REG-01]
_RANGE_RUNS_TO = re.compile(r"the milestone's range runs to (\d+)")


def _current_milestone_blocks() -> list[tuple[str, str, list[int]]]:
    """Every `## Decisions taken` block the register heads for `current_milestone`.

    One tuple per block: the header, the preamble above its first decision, and the numbers under
    it. Scoped to `current_milestone` for the reason the id-count guard above gives -- the block
    in flight is the one this milestone is writing. Two earlier blocks carry exactly this drift
    and neither is repaired here: 2026-09-09 opens "Seven" over twelve decisions and M4.11's opens
    "Three" over four, both with no sentence disclosing the rest. Another milestone's record is
    not this one's to rewrite, and a guard that went red on it would be reporting history rather
    than the diff in front of it.
    """
    body = REGISTER.read_text(encoding="utf-8")
    heads = list(_REGISTER_BLOCK.finditer(body))
    # `M4` must not match the header that says `M4.15`, so the milestone name is read with a
    # boundary of its own: `\b` counts the dot as one and would match the longer name inside it.
    scoped = re.compile(re.escape(CURRENT) + r"(?![\d.])")
    out = []
    for i, head in enumerate(heads):
        if not scoped.search(head.group(0)):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        block = body[head.end():end]
        decisions = list(_REGISTER_DECISION.finditer(block))
        preamble = block[:decisions[0].start()] if decisions else block
        out.append((head.group(0).strip(), preamble, [int(m.group(1)) for m in decisions]))
    return out


def test_every_published_decision_range_ends_where_the_register_does():
    """Four documents publish this milestone's decision range, and only one of them was derived.

    The register numbers the decisions; `docs/TESTING.md`'s ledger paragraph, the map's section
    header and the comment above `MILESTONES` each restate the RANGE in prose -- and prose written
    before a browser gate appends four more decisions stays where it was. M4.15 shipped three
    spellings of one range at once: the ledger's ended at the last decision taken, the map's stopped
    one short of it and this file's comment two, so an auditor reconciling the record against the
    register met neither the decision that replaced logout's exit with `location.assign('/login')`
    nor the one that made 272 conditional on a sign-out the server confirmed.

    Decision 184 refuses to publish a measurement no run produced; this is the same rule from the
    other side, applied to a figure a later run overtook, which is what the two ledger guards above
    already hold for the milestone counts and the id total. Neither of them can see a range.
    [M4.15 review cycle 1: M415-C1-COV-01, m415-c1-e2e-05]

    A MILESTONE THAT DOES NOT HOLD `current_milestone` PUBLISHES ITS RANGE UNHELD, and this guard
    says so rather than implying otherwise. M5.4 is the first milestone to ship rows without the
    scalar, and its first two review cycles took ten decisions while four documents -- the three
    this guard reads and the spec's own §12 row -- went on publishing the range it opened with; the
    guard was scoped to M5.1 and saw none of it. It could not simply be pointed at M5.4 either: a
    range is recognised here by starting at the milestone's lowest number, and M5.4's lowest is 341,
    a number a planner filed against it, so it publishes "341 and 382-N" and no range beginning at
    341 exists to read. Until that milestone holds the scalar, keeping its range current is the
    milestone's own review work and nothing mechanical checks it.
    [M5.4 review cycle 3, M54-C3-EVID-03]
    """
    blocks = _current_milestone_blocks()
    assert blocks, f"the register heads no `## Decisions taken` block for {CURRENT}"
    held = sorted(n for _, _, numbers in blocks for n in numbers)
    assert held, f"the register's {CURRENT} block holds no `### <n>.` decision"
    lo, hi = held[0], held[-1]

    published, stale = 0, []
    for path in _RANGE_PUBLISHERS:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for first, last in _PUBLISHED_RANGE.findall(line):
                if int(first) != lo:
                    continue
                published += 1
                if int(last) != hi:
                    stale.append(
                        f"{path.relative_to(REPO).as_posix()}:{number}: publishes {first}-{last}"
                    )
            for last in _RANGE_RUNS_TO.findall(line):
                published += 1
                if int(last) != hi:
                    stale.append(
                        f"{path.relative_to(REPO).as_posix()}:{number}: says the range runs to "
                        f"{last}"
                    )
    assert published, (
        f"no file publishes {CURRENT}'s decision range, so this guard holds nothing. Either the "
        "ledger paragraph states it, in the shape M4.7's block uses -- the full range, then the "
        "sittings it decomposes into -- or this guard comes out."
    )
    assert not stale, (
        f"{CURRENT}'s decisions are {lo}-{hi} in docs/spec-v2.2-proposals.md, and these publish a "
        "range that stops short of it. A reader reconciling the record against the register never "
        "meets the decisions past the end they name:\n  " + "\n  ".join(stale)
    )


def test_the_register_block_opens_with_the_number_of_decisions_it_holds():
    """The same drift one document in, and the only place it cannot be read as scoped.

    Every dated block in the register opens with the count it holds -- "Ten, taken as M4.10
    opened", "Thirteen, taken as M4.12 opened" -- and M4.15's opened "Fifteen" over a block holding
    twenty, because the four decisions the browser gate produced and the one review cycle 1 took
    were appended under the header that already said fifteen. A reader of the preamble is then told
    a smaller number than the block under it contains, which is the id-count guard's failure in the
    other document: a figure published as measured that a later run overtook.

    Held per block rather than per milestone, because the house may put a milestone's later
    decisions under a dated header of their own (`as M4.13 closed`, `M4.12 review cycle 1`) and
    each header then owns its own count. Either arrangement passes; neither may miscount.
    [M4.15 review cycle 1: M415-C1-COV-01]
    """
    drift = []
    for header, preamble, numbers in _current_milestone_blocks():
        first = preamble.split()[0].strip(",.") if preamble.split() else ""
        spelled = _spelled(first)
        assert spelled is not None, (
            f"the register's block `{header}` opens with {first!r} rather than the count it "
            "holds. Open it the way every dated block above it does, so the count can be read."
        )
        if spelled != len(numbers):
            drift.append(f"{header}: opens {first!r}, holds {len(numbers)} decisions")
    assert not drift, (
        "the register publishes a decision count its own block has overtaken. Restate the count, "
        "and disclose the later sittings inside the sentence the way the 2026-09-10 block does "
        "for its eleventh:\n  " + "\n  ".join(drift)
    )


# --- M5.1 review cycle 4: a number is spent once, by the milestone the roadmap files it under ---
#
# `docs/milestones/ROADMAP-M5.md` numbers M5's open questions from 321 and files each against the
# milestone that must answer it, and this wave's practice is that the question's number IS the
# decision's number: 332, 336, 340 and 345 were transcribed into the register under exactly the
# numbers that table gives them. M5.1's closing sitting then took 346 for the drain's per-tick
# bound -- the first number nothing above it had spent -- while the table files 346 against M5.3,
# for decision 171's owed §4.3 and §10 amendment, and `docs/milestones/M5.3-plan.md` is bound to
# record it under that number in a file no agent may edit.
#
# What that costs is silence rather than a red build, which is why it needed a guard rather than a
# reading. `_register_entries` keys its entries by number, so a second `### 346.` would replace the
# first and every citation of 346 in the tree -- the coverage map's, this file's, two in
# `test_worker_schedule.py` -- would resolve against a rule about `dna_vocab/v1/` while staying
# green, because `_is_a_decision` admits any number at or above 162 without opening a file. The one
# thing that would redden is the register's heading COUNT, in M5.3's lane, with a message about the
# file's size. The register's own M5.1 preamble states the harm in the words this guard enforces:
# a number is taken by the owner, not reserved by a planner, and a number written twice is two
# normative rules under one heading.
#
# The OWNER CELL and not the question: a question may name a milestone in passing, and the last
# cell of the row is where the table files it. A cell naming no milestone at all -- 321's
# "everything", 331's "all seven" -- allocates nothing and is skipped, as is a sitting header from
# before the milestone names went into them. [M5.1 review cycle 4, M51-C4-REG-01]
ROADMAP_M5 = REPO / "docs" / "milestones" / "ROADMAP-M5.md"
_ALLOCATION_ROW = re.compile(r"^\|\s*\*\*(\d{3})\*\*\s*\|(.*)\|\s*$", re.M)
_ALLOCATED_TO = re.compile(r"\bM\d+(?:\.\d+)?\b")


def _numbers_the_roadmap_files_elsewhere(text: str) -> dict[int, frozenset[str]]:
    """Every decision number the M5 roadmap's tables file against a named milestone."""
    allocated = {}
    for row in _ALLOCATION_ROW.finditer(text):
        owners = frozenset(_ALLOCATED_TO.findall(row.group(2).rsplit("|", 1)[-1]))
        if owners:
            allocated[int(row.group(1))] = owners
    return allocated


def _sitting_of_each_decision() -> dict[int, str]:
    """Every register decision, with the header of the `## Decisions taken` block holding it."""
    body = REGISTER.read_text(encoding="utf-8")
    heads = list(_REGISTER_BLOCK.finditer(body))
    held = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(body)
        for decision in _REGISTER_DECISION.finditer(body[head.end():end]):
            held[int(decision.group(1))] = head.group(0).strip()
    return held


def test_the_register_spends_no_number_another_milestone_is_bound_to():
    """An unspent number is a reservation the owner may still fill; a spent one is not.

    M5.1 recorded the drain's per-tick bound as `### 346.` over a roadmap that files 346 against
    M5.3 and an M5.3 plan bound to transcribe it there, and nothing in the tree could say so: the
    register's two hole enumerations and `docs/TESTING.md`'s both list every number M5.1 left
    unspent and omit 346, which is the same reading made three more times. The collision is silent
    until the milestone that owns the number opens, and it surfaces there as a count -- in a lane
    that did not cause it, over a file it may not renumber, because an agent may not edit a plan.

    Held against the roadmap's own allocation because this milestone's code already treats that
    file as authoritative for the same wave: `test_acquire_pipeline.py` pins the ten stages'
    owners with "The owners are `ROADMAP-M5.md`'s allocation of the work and not a guess". Taking
    it as binding for stage ownership and optional for numbering is the inconsistency.
    [row `platform-the-proposal-ledger-counts-itself`; M5.1 review cycle 4, M51-C4-REG-01]
    """
    allocated = _numbers_the_roadmap_files_elsewhere(ROADMAP_M5.read_text(encoding="utf-8"))
    assert allocated, (
        f"{ROADMAP_M5.relative_to(REPO).as_posix()} files no numbered question against a named "
        "milestone any more, so this guard reads nothing. Either its tables changed shape or the "
        "allocation moved, and the rule comes out with the thing it was holding."
    )
    spent = []
    for number, header in sorted(_sitting_of_each_decision().items()):
        owners = allocated.get(number)
        holder = set(_ALLOCATED_TO.findall(header))
        if owners is None or not holder or owners & holder:
            continue
        spent.append(
            f"decision {number} is headed under `{header}` and "
            f"{ROADMAP_M5.relative_to(REPO).as_posix()} files it against "
            + ", ".join(sorted(owners))
        )
    assert not spent, (
        "the register spends a number another milestone's plan is bound to record:\n  "
        + "\n  ".join(spent)
        + "\n\nThe milestone that owns it cannot renumber its own citation, because an agent may "
        "not edit a plan -- so the day it opens the register holds two `### N.` headings under one "
        "number, `_register_entries` keeps the later one, and every citation in the tree resolves "
        "against the wrong rule while staying green. Take the next number no plan claims, and add "
        "this one to the hole lists that say which numbers are unspent."
    )


def test_the_allocation_reader_reads_the_owner_cell_and_not_the_question():
    """Both ends, because either mistake here is a guard that reports the wrong milestone.

    A question may name a milestone in its own text -- 322's asks whether the queue is
    `acquisition_job`, which M5.1 builds -- and the cell that FILES it is the last one. A row that
    files against no milestone at all is 321's "everything" and 331's "all seven": those bind
    nobody and must not be read as binding the first `M`-shaped token on the line.
    [M5.1 review cycle 4, M51-C4-REG-01]
    """
    read = _numbers_the_roadmap_files_elsewhere(
        "| # | question | owner |\n"
        "|---|---|---|\n"
        "| **321** | Does M5 ship as one milestone or as M5.1-M5.7? | everything |\n"
        "| **332** | Where does `/events/jellyfin`'s token live? | M5.1/M5.2 |\n"
        "| **346** | Decision 171's spec amendment. M5.3 touches those clauses | M5.3 |\n"
    )
    assert 321 not in read
    assert read[332] == frozenset({"M5.1", "M5.2"})
    assert read[346] == frozenset({"M5.3"})


# --- M5.3 review cycle 1: a number left unspent is a hole, and a hole has to be named ------------
#
# Decision 346's cost paragraph is what the practice above is for, and this is its other half. M5.1
# spent 346 because "the register's two hole enumerations and `docs/TESTING.md`'s both list every
# number M5.1 left unspent and omit 346", and the collision surfaced in the lane that owned the
# number, over a plan file no agent may edit. The guard above catches a number the ROADMAP files
# elsewhere; nothing caught a number the register neither heads nor mentions at all, which is the
# state that made 346 lootable in the first place.
#
# M5.3's own sitting accounted for 372-378 as spent, for the 324-359 the M5.1 blocks leave unspent
# and for 362-371 and 382-391 as the two sibling lanes' -- and left 379-381, the rest of its own
# block, named nowhere in the file. An auditor reading the register alone meets three numbers under
# the highest entry with nothing said about them, and takes one.
#
# Read off the file's header and the sitting PREAMBLES, because that is where this file enumerates
# holes; a number inside an entry's body is a citation of a rule, not a claim about who owns a
# number. A range opening at the register's own first decision is skipped for the reason
# `test_every_published_decision_range_ends_where_the_register_does` keys on that same number:
# `162-N` is the file's extent, and reading it as an allocation would mark every hole in the file
# accounted for ever after. [decision 346; M5.3 review cycle 1, M53-REG-06]
_ALLOCATION = re.compile(r"(?<![:\d])(\d{3})(?:\s*[-\u2013]\s*(\d{3}))?\b")
_SPAN_IN_WORDS = re.compile(r"between (\d{3}) and (\d{3})")


def _numbers_the_register_speaks_for() -> set[int]:
    """Every decision number the file's header paragraphs and its sitting preambles account for."""
    body = REGISTER.read_text(encoding="utf-8")
    opening = _REGISTER_DECISION.search(body)
    assert opening, "the register heads no `### <n>.` entry at all, so this reads nothing"
    regions = [body[:opening.start()]]
    heads = list(_REGISTER_BLOCK.finditer(body))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(body)
        block = body[head.end():end]
        first = _REGISTER_DECISION.search(block)
        regions.append(head.group(0) + (block[:first.start()] if first else block))
    spoken: set[int] = set()
    for region in regions:
        for first, last in _ALLOCATION.findall(region):
            if int(first) == _FIRST_DECISION and last:
                continue
            spoken.update(range(int(first), int(last or first) + 1))
        for first, last in _SPAN_IN_WORDS.findall(region):
            spoken.update(range(int(first), int(last) + 1))
    return spoken


def test_the_register_accounts_for_every_number_below_the_last_one_it_spends():
    """A hole the file does not enumerate is indistinguishable from a number nobody wanted.

    Held below the highest entry only. Above it there is nothing to read: a number no sitting has
    reached yet is the next one, and a lane's block is orchestration that reaches this file when
    the sitting that spent from it is written. [decision 346; M5.3 review cycle 1, M53-REG-06]
    """
    headed = {number for number in _register_entries() if number >= _FIRST_DECISION}
    assert headed, "the register heads no numbered owner decision, so this guard holds nothing"
    spoken = _numbers_the_register_speaks_for()
    unaccounted = sorted(
        number for number in range(_FIRST_DECISION, max(headed))
        if number not in headed and number not in spoken
    )
    assert not unaccounted, (
        f"the register heads no entry at {unaccounted} and says nothing about those numbers "
        "either, while heading numbers above them. A number neither spent nor named as unspent "
        "reads as free to the next reader, and decision 346 records what that costs: the "
        "milestone that owns it cannot renumber its own citation, so the day it opens the file "
        "holds two `### N.` headings under one number, the later one wins, and every citation in "
        "the tree resolves against the wrong rule while staying green. Name them in the sitting "
        "that left them unspent, the way that sitting already names the sibling lanes' blocks."
    )


# The owed device checks in `docs/TESTING.md`: the bullets, and the signature line under them.
# Read as one shape, because the debt is only a debt while both halves stand -- a filled line over
# the bullets is decision 184's defect wearing a signature, and bullets with no line under them
# are a paragraph nobody is asked to discharge.
_OWED_OPENER = "cannot be produced by any run in this suite"
_OWED_SIGNATURE = re.compile(r"`verified on ([^`\n]*)`")
# "**Four** of its facts cannot be produced ..." and "Two more join them at the first review
# cycle": the two sentences that COUNT the debt. Summed against the bullets under them, for the
# reason the id-count guard sums its three figures -- a bullet added or dropped without restating
# the sentence is the same drift, and this block is the one nothing else in the tree reads.
_OWED_FACT_COUNTS = re.compile(r"\b([A-Za-z]+) (?:of its facts cannot be produced|more join them)\b")

# What a discharged debt looks like when it is written BESIDE the blank rather than into it. Both
# guards over an owed signature -- this one and `test_static_contracts.py`'s M2 verdict -- read the
# PLACEHOLDER and nothing else, so leaving every blank blank and asserting the signature in the
# same breath passed both, measured: "`verified on iPhone ____, ...` -- **unfilled.** (signed on an
# iPhone 13 by pmk, 2026-09-17; see the gate log.)" reads as discharged to every human being while
# the rule that refuses a pre-signed line stays green, and so does the harder spelling that keeps
# "**unfilled.**" as a substring and flips its subject. Decision 281's sentence is that a pre-signed
# line is worse than a gap because it tells the next reader to stop looking -- and a reader stops
# looking at prose, not at an underscore.
#
# The tokens are what a signature is made of: a date, the word itself, the device. Read over the
# PARAGRAPH the blank sits in rather than the physical line, because the shipped line wraps mid
# sentence and a rule pinned to the line would either break on the wrap or stop at it; and with the
# template stripped out first, which is why `verified` may stand inside the blanks and nowhere
# else. Measured over both records as they ship: no hit in either, and every forged spelling above
# caught. It does not reach a verdict written in a DIFFERENT paragraph, above or below, which is a
# limit recorded rather than closed.
#
# The sentence above was true of the comment and not of the reader until review cycle 5:
# `_paragraph_at` sliced forward from the marker, so the paragraph it read began at the blank and
# a signature written one line higher -- the easier spelling, since a signature reads as a lead-in
# -- was outside it on both documents, measured green. The limit was recorded in one direction
# only, and the undisclosed direction was the cheap one.
# [decisions 281, 297, 302; M4.16 cycle 4, M416-C4-COV-04; M4.16 cycle 5, M416-C5-COV-08]
_SIGNED_IN_PROSE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\bsigned\b|\bverified\b|\bPASS\b|\biPhone\s*\d|\biOS\s*\d"
)


def _signature_prose(paragraph: str, template: re.Pattern[str]) -> list[str]:
    """Every signature-shaped token a paragraph carries outside the blank template itself."""
    return _SIGNED_IN_PROSE.findall(template.sub("", paragraph))


def _paragraph_at(text: str, offset: int) -> str:
    """The whole paragraph `offset` falls in -- from the blank line above it to the blank below.

    It read FORWARD from the offset until review cycle 5, so everything on an earlier line of the
    same paragraph was invisible while the comment above `_SIGNED_IN_PROSE` said the rule was read
    over the paragraph. Both callers hand it the offset of the marker rather than of the
    paragraph: `body.index(signature_line)` here, `rows["M2"].index("**Status:**")` in
    `test_static_contracts.py`, and the second of those is not even a line start. So "Signed on an
    iPhone 13 by pmk, 2026-09-20; see the gate log." written one line ABOVE the blank -- or, in
    M2's row, on the same line in front of it -- discharged the debt for every human reader with
    both guards green, measured, while the same sentence written one line BELOW was refused. The
    easier spelling was the unguarded one, because a signature reads naturally as a lead-in.

    The limit that remains is stated rather than closed, and it is now symmetric: a sentence in a
    SEPARATE paragraph, above or below, is outside this and is not caught. Widening the slice is
    free on both documents as they ship -- each offset already sits on its paragraph's first line,
    so the text this returns is byte-identical today. [M4.16 cycle 5, M416-C5-COV-08]
    """
    start = text.rfind("\n\n", 0, offset)
    start = 0 if start == -1 else start + 2
    end = text.find("\n\n", offset)
    return text[start:] if end == -1 else text[start:end]


@pytest.mark.parametrize(
    ("name", "before", "after", "caught"),
    [
        ("nothing beside it", "", "", False),
        ("signed on the line below, inside the paragraph",
         "", "Signed on an iPhone 13 by pmk, 2026-09-20; see the gate log.\n", True),
        ("signed on the same line, in front of it",
         "Signed by pmk 2026-09-20: ", "", True),
        ("signed on the line above, inside the paragraph",
         "Signed on an iPhone 13 by pmk, 2026-09-20; see the gate log.\n", "", True),
        ("a sentence in the paragraph that is not a signature",
         "The four facts below are the ones no run here can produce.\n", "", False),
        ("written in a separate paragraph above -- the recorded limit, in both directions",
         "Signed on an iPhone 13 by pmk, 2026-09-20.\n\n", "", False),
    ],
)
def test_the_beside_the_blank_rule_reads_the_whole_paragraph_the_blank_sits_in(
        name, before, after, caught):
    """Six spellings around one blank, three of them refused, and the third is the one the reader
    shipped unable to see.

    The rule is the only thing standing between decisions 281, 297 and 302's polarity and a
    sentence: the blanks stay blank and the debt is discharged in prose beside them, which reads
    as signed to every human being while the underscore rule stays green. Cycle 4 closed the
    spelling written BELOW the blank; this closes the one written above, which is the easier of
    the two to write because a signature reads naturally as a lead-in.

    The last two cases are what keeps it from being a rule about a paragraph containing words: an
    ordinary sentence in the same paragraph passes, and the separate-paragraph spelling is the
    limit this reader records rather than closes -- stated in both directions now, so nobody
    reads the comment as a promise it does not keep. [M4.16 cycle 5, M416-C5-COV-08]
    """
    blank = "`verified on iPhone ________, iOS ________, build ________`"
    body = f"a preamble paragraph\n\n{before}{blank}\n{after}\nand the paragraph after it\n"
    beside = _signature_prose(_paragraph_at(body, body.index(blank)), _OWED_SIGNATURE)
    assert bool(beside) is caught, (name, beside)


def test_the_owed_device_checks_are_recorded_and_still_unsigned():
    """Decision 281's refusal, held by something other than the next reader's good faith.

    Four facts in M4.15's exit criterion cannot be produced by any run in this suite -- the header
    clearing the status bar and the tab bar clearing Safari's toolbar in installed standalone mode,
    focus zoom on real hardware, and the installed app cold-booting with the appliance unreachable
    -- and review cycle 1 added two more the shipped copy already asserts. They are written into
    `docs/TESTING.md` as an outstanding check with an unfilled signature line, in the shape
    decision 184 requires of any measurement: the run that produced it, or nothing.

    Nothing read that block. The two ledger guards above hold the fenced `Current state` block and
    the id-count sentence, `test_harness_fixtures.py` re-measures what `-q` prints, and no coverage
    row names the device checks at all -- so a later milestone re-pasting this file could have
    closed the debt by writing a device and a date rather than by holding one, and every gate would
    have stayed green. A pre-signed line is worse than a gap because it tells the next reader to
    stop looking, which is decision 281's own sentence.

    The polarity is deliberate and is `05-milestones.spec.js`'s: this goes red on the day the owner
    honestly signs, and the repair is to delete it together with its row entry, in the same change.
    It cannot tell a true signature from a fabricated one -- nothing here can -- so what it buys is
    that filling the line requires deleting a test that quotes decision 281 at you.
    [M4.15 review cycle 1: M415-C1-COV-03]
    """
    body = LEDGER.read_text(encoding="utf-8")
    signature = [line for line in body.splitlines() if _OWED_SIGNATURE.search(line)]
    assert len(signature) == 1, (
        f"docs/TESTING.md holds {len(signature)} `verified on ...` signature lines; decision 281 "
        "owes exactly one, under the facts it covers. It was deleted, or a second milestone is "
        "carrying its own device debt and this guard is reading the wrong one."
    )
    assert "**unfilled.**" in signature[0], (
        "docs/TESTING.md's device check no longer says it is unfilled. Decision 281: the run that "
        f"produced it, or nothing.\n  {signature[0].strip()}"
    )
    filled = []
    for field in _OWED_SIGNATURE.search(signature[0]).group(1).split(", "):
        label, _, value = field.partition(" ")
        if not re.fullmatch(r"_+", value):
            filled.append(f"{label}: {value}")
    assert not filled, (
        "docs/TESTING.md's device check carries a value, and no run in this repository can "
        "produce one: there is no status bar here, no dynamic toolbar, no focus zoom and no "
        "installed web view. Decision 281 refuses a pre-signed line because it tells the next "
        "reader to stop looking. If the owner has signed it on a device, delete this guard and "
        "its entry in spec_coverage.toml in the same change:\n  " + "\n  ".join(filled)
    )
    beside = _signature_prose(_paragraph_at(body, body.index(signature[0])), _OWED_SIGNATURE)
    assert not beside, (
        "docs/TESTING.md's device check leaves its blanks blank and asserts the signature beside "
        "them, which discharges the debt for every reader while satisfying the rule above by "
        f"wording: {', '.join(dict.fromkeys(beside))}. Decision 281 refuses a pre-signed line "
        "because it tells the next reader to stop looking, and this paragraph is where they stop."
    )

    assert body.count(_OWED_OPENER) == 1, (
        f"docs/TESTING.md states {body.count(_OWED_OPENER)} owed-device-check paragraphs; the "
        "block is one debt under one signature line, so exactly one opens it."
    )
    start = body.rindex("\n\n", 0, body.index(_OWED_OPENER))
    region = body[start:body.index(signature[0])]
    # The LIST, and not a markdown style. The count was `startswith("- **")`, so a seventh fact
    # written without the bold lead -- or under a `*` bullet -- was not a bullet to this rule, and
    # the counting sentence and the list went on agreeing while the debt grew. Measured: a
    # "- the splash screen honours the icon, installed" inserted above the six passed, and the same
    # line with its lead bolded was caught. The convention is now ASSERTED one line down rather
    # than relied on, so the block still reads the way the rest of the file does.
    # [decision 302; M4.16 cycle 4, M416-C4-COV-05]
    bullets = [line for line in region.splitlines() if re.match(r"[-*]\s", line)]
    unstyled = [line for line in bullets if not line.startswith("- **")]
    assert not unstyled, (
        "docs/TESTING.md's owed-check block carries a bullet outside its own convention -- each "
        "fact opens `- **` and names itself in bold. A bullet written any other way is the one "
        "the count stopped seeing:\n  " + "\n  ".join(unstyled)
    )
    claimed = [_spelled(word) for word in _OWED_FACT_COUNTS.findall(region)]
    assert claimed and None not in claimed, (
        f"the owed-check block counts its facts as {_OWED_FACT_COUNTS.findall(region)}, which is "
        "not a number word. The count is read out of the prose so that widening one widens the "
        "other; restate it as a word."
    )
    assert sum(claimed) == len(bullets), (
        f"docs/TESTING.md's owed-check block claims {sum(claimed)} facts and lists "
        f"{len(bullets)} of them. A debt published as a count nobody re-derived is decision 184's "
        "defect on the document CLAUDE.md sends the next reader to for status."
    )
