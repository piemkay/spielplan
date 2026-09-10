# Testing

The suite exists to make the spec falsifiable. Every layer answers a different question, and
the coverage map is the contract that says which requirement each milestone owes a test.

## The six layers

| Layer | Command | Needs | Answers |
|---|---|---|---|
| **backend** | `pytest backend/tests` | nothing | pure logic: secrets, auth, validation rules, mapping, mojibake repair |
| **static guards** | (part of pytest) | nothing | rules no runtime can enforce — weights are never filters, the two DNA tiers are never unioned |
| **schema** | (part of pytest) | node | the migrations apply to a real Postgres engine (PGlite, wasm), and produce the structure §4.1 requires |
| **integration** | `TEST_DATABASE_URL=… pytest` | Postgres 16 | anything that only exists against a real server — COPY resolves encoders from destination column types |
| **e2e** | `node e2e/run.mjs` | the compose stack | what actually ships: the real backend serving the real PWA in a real browser, desktop and phone |
| **frontend units** | `npm --prefix frontend test` | nothing | client helpers with real edge cases (query building, error classification), and — under jsdom, in `*.svelte.test.js` files — a mounted component whose state a browser cannot be held still enough to observe |

Two of those layers exist because of bugs that reached the running app and could not have been
caught anywhere cheaper: SQLite integer booleans hitting Postgres `boolean` columns (integration),
and `json` columns arriving as text and being iterated character by character (e2e).

### Two test doubles that are not mocks

M1 added both, and the distinction matters: each one can *refuse*, which is what makes the
assertions above it capable of failing.

- **`ops/fake_jellyfin.py`** — a real HTTP server implementing the handful of Jellyfin ≥ 10.9
  routes §7.1 names. It runs two ways: mounted in-process through `httpx.ASGITransport` for the
  integration tests, and as a compose service (`ops/compose.e2e.yml`) for the browser tests.
  **It refuses the admin API key on `/UserPlayedItems` on purpose.** A real Jellyfin would
  accept it — the key is admin-equivalent with no read-only variant (§14.3) — so §7.3's
  per-user-token rule is enforced by this app's code and nothing else. Refusing it here is what
  turns that restraint into something a test can break.
- **`backend/tests/fixtures/soft_authenticator.py`** — a software WebAuthn authenticator that
  signs genuine CTAP2 structures with a real P-256 key. It makes the interesting cases
  reachable: an assertion for the wrong origin, one for the wrong rp_id, and a replay whose
  signature verifies perfectly and whose counter has not moved. In the browser, Chromium's
  virtual authenticator (CDP `WebAuthn` domain) plays the same part.

**The e2e origin is load-bearing.** WebAuthn binds a credential to the origin (§2, §14.4), so
the suite runs against `PUBLIC_URL`'s origin — `http://localhost:8080`, not `http://127.0.0.1:8080`.
Same host, same port, different origin, and a passkey registered under one is correctly refused
under the other.

## Running it

```bash
# everything that needs nothing
python -m pytest backend/tests -q
npm --prefix frontend test

# the integration layer
docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db
docker compose exec db createdb -U spielplan spielplan_test
echo 'TEST_DATABASE_URL=postgresql://spielplan:<pw>@127.0.0.1:5432/spielplan_test' > .env.test
python -m pytest backend/tests -q

# the same command on a machine that has .env.test and does not want a database this time
python -m pytest backend/tests -q --no-db

# the whole stack, from a cold start — run.mjs brings it up itself, fake Jellyfin included
node e2e/run.mjs
```

**`pytest backend/tests -q` appears twice in that block, and it is the same command both times** —
which layer it ran depends on a file the repository does not track. `conftest.py` loads `.env.test`
if it is there, so the one command is a pure-logic run on one machine and an integration run on the
next: creating and dropping databases, and through `test_backup.py` shelling out to `docker exec`,
against whatever host that untracked file happens to name. So every run now opens with one line
saying which it was, and it is the first line of output at `-q`, which is the verbosity this file
and `ci.yml` both use:

```
integration layer: ARMED against 127.0.0.1:5432/spielplan_test_p37812 (source: .env.test)
integration layer: UNARMED (TEST_DATABASE_URL is unset) -- db/app/pg_url tests skip
integration layer: UNARMED (--no-db) -- db/app/pg_url tests skip
```

`--no-db` disarms the layer deliberately: a quick run on a machine where `.env.test` exists, which
before the flag meant either an integration run nobody asked for or unsetting the variable by hand
at every invocation. It creates no database. **A green UNARMED run
has not run the integration layer** — every test behind `db`, `app` or `pg_url` skipped, `-q` prints
each of those as an `s` and counts them in a summary line nobody weighs, and CI runs all of them.
That was always true; the difference is that the run now says so instead of leaving it to be
inferred from a count that reads the same either way. The schema layer is silent in
the same way for its own reason: the PGlite tests skip without `backend/tests/pglite/node_modules`,
and no line announces that one. (`--no-db` is registered in `backend/tests/conftest.py`, so it needs
the path argument CLAUDE.md already mandates for a different reason; `pytest --no-db` from the
repository root reports "unrecognized arguments".)

`e2e/run.mjs` runs in two phases on purpose. §10's swap sequence ends in "restart backend +
worker", so a bundle imported in phase one is not *loaded* until the services come back. Without
that restart between the phases, every spec that needs an imported bundle skips — which looks
like a pass and proves nothing. As of M4.8 the runner refuses to enter phase 2 at all if the
restarted backend does not report a loaded bundle in 60 attempts — up to six minutes, since each
attempt carries its own 5 s request deadline: skipping is what a run with nothing imported used to
do instead, and it exits 0.

**CI runs on every branch push** (`.github/workflows/ci.yml`): `push: branches: ['**']`, because
until M4.8 the trigger was `push: main` and four milestones reached main having never run on Linux,
so §12's gates were first evaluated on the merge commit. A run on the default branch that is
already in flight is no longer cancelled by the next push — `cancel-in-progress` is now conditional
on the ref, since the one commit whose result the gates are read off is exactly the one whose run
must complete. That is the whole of what it buys: the group is keyed on the ref and carries no
`queue:` key, so GitHub's default still supersedes a run left *pending* behind it, and a third push
inside one run's window leaves the middle commit with no completed run at all. Read a gate off a
completed run rather than off the absence of a red one. Five jobs run there:
lint, backend, integration, frontend and e2e.

**The corpus check has a workflow of its own**, `.github/workflows/real-bundle.yml`. It is the only
place `CORPUS_BUNDLE_DIR` is set and therefore the only place the two tests that key off it stop
skipping; decision 183 puts it on a self-hosted runner with the ~1.15 GB export already on
disk rather than behind a download URL in a secret, so it carries `workflow_dispatch` and a weekly
`schedule` and no push path at all. It gates no branch and nothing depends on it, but the waiting
is not free: until a runner labelled `spielplan-corpus` is registered, GitHub cancels the job once
it has sat in the queue for 24 hours, and a cancelled job denies its run a success conclusion —
which is a reason to register it, not a reason for `continue-on-error`. A second file rather than a
sixth job in `ci.yml`, because `concurrency:` is workflow-level and the group is held until a run's
*last* job finishes: as one job in `ci.yml` the corpus check answered the same weekly tick as the
five hosted jobs, on the default branch, and held `ci-refs/heads/main` for that entire day — so
main's next push was created pending behind the group and the push after it superseded the pending
one, which is the missing-run symptom `cancel-in-progress` was made conditional to remove, back
one day a week. A job-level `if:` cannot fix that, because the run holding the group is the one
where the gate passes. That runner has to be **Linux with Docker**:
the job uses a `services:` Postgres container, which GitHub only provides on a Linux runner, and
its two shell steps are `sh`. The corpus lives on the household's Windows workstation, so the
label belongs on a runner registered inside WSL or a Linux VM there that can see the export, with
the repository variable `CORPUS_BUNDLE_DIR` set to the path that machine reads it at — a
Windows-native runner fails during container initialisation, before the first step runs.

## The coverage map

`backend/tests/spec_coverage.toml` holds one row per testable requirement:

```toml
current_milestone = "M2"

[[requirement]]
id = "data-rules-dna-evidence-required"
spec = "§4.1 rule 1"
milestone = "M0"
kind = "backend"
what = "Bundle validation fails when dna_evidence is absent, or when any dna_tag row has no
        evidence quote, and the report names the orphan count."
why = "a tag without its quote is unfalsifiable."
tests = ["backend/tests/test_bundle_validation.py::test_tag_without_evidence_fails"]
```

`test_spec_coverage.py` enforces two rules:

1. **Every requirement at or before `current_milestone` names at least one test.**
2. **Every named test exists** — checked against the real pytest functions, Playwright titles and
   vitest titles, so a renamed or deleted test breaks the build rather than silently uncovering a
   requirement.

A row that genuinely should not be tested yet carries `waived = "an honest reason"` and appears
in the report as waived rather than vanishing.

### This is what "updated every milestone" means

The update is mechanical, and it happens *before* the code:

1. **Open the milestone.** Change `current_milestone` in `spec_coverage.toml` to the milestone
   you are starting. Run `pytest backend/tests/test_spec_coverage.py`.
2. **Read the failure.** It lists every requirement that milestone now owes, with its spec
   section, its kind, and what it has to assert. That list is the milestone's test plan, and you
   did not have to write it.
3. **Write the tests first**, at the `kind` the row names. Fill in `tests = [...]` as each lands.
4. **Review it adversarially before closing it.** A green suite proves the tests pass, not that
   the tests are the right ones. M1's review ran six independent lenses — auth, the connector,
   security, the data layer, test quality, the front end — and put every finding through a
   skeptic told to refute it. What survived was real: a duplicate Jellyfin item silently
   erasing a person's explicit `seen`, three inputs rendering white-on-white, a broken link
   reporting itself healthy. None of them would have failed a test that existed.
5. **The milestone is closed when the suite is green** — which is a stronger statement than
   §12's exit criterion, because every requirement behind it is named.

Two things happen on the way that are easy to miss:

- **`e2e/specs/05-milestones.spec.js` asserts that unbuilt surfaces say which milestone owes
  them.** Those assertions are designed to *fail* when the surface ships. That failure is the
  reminder to delete the placeholder test and point the map's rows at the real ones.
- **The `waived` rows come back.** A waiver is scoped to the milestone that wrote it; when you
  open the next one, re-read them, because "no infrastructure for this yet" usually stops being
  true.

### Current state

```
> M0   34/35  covered (1 waived)
> M1   10/10  covered
> M2   27/27  covered
> M3   15/15  covered
> M4   42/42  covered
> M4.5   18/18  covered
> M4.6   12/12  covered
> M4.7   17/17  covered
> M4.8   10/10  covered
> M4.9   23/23  covered
  M4.12    1/1   covered
  M4.13    2/2   covered
  M5    0/10  covered
  M6    0/12  covered
  M7    0/1   covered
```

**M4.5's other 18/18 is now one check short, and this is the whole of what is known about it.**
The `18/18` above is a coverage count and is unaffected; the collision is a coincidence worth
naming, because `docs/milestones/M4.5-plan.md:320` publishes a second **18/18** — the checks
`ops/m45_exit_criterion.py` printed against `v20260828`. Two of those eighteen had a constant for a
predicate: §12's M2 criterion was asserted as `placed == 0 or True`, and the count-encoded content
blocks as a literal `True`. M4.8 makes the first a real check and turns the second into a plain
print of the numbers it was summarising, so the script now prints one fewer check and one report.
Nothing was hidden — `placed` is genuinely 0 on `v20260828`, which is why the disjunction went
unnoticed for a milestone — but "18/18" no longer describes what the script prints, and no corrected
count is invented here: restating it needs the 1.15 GB bundle, a scratch database and a ten-minute
import, so **the count is restated at the next real run** (decision 184). The same note is owed by
hand at `M4.5-plan.md:320`, which the milestone workflow may not edit.

**M4.9 is the open milestone, and unlike M4.6, M4.7 and M4.8 it has a §12 row of its own.** The
argument is at spec line 425: M0's exit criterion is "bundle imports clean; Library list and title
card render imported titles", and it was closed against a fixture in which the corpus's awkward
shapes do not occur — so the row existed and was asserted about the wrong artifact. The real export
ships two department spellings for one job, two facet namings for one vocabulary, and a term id
that already carries its own facet, and the whole chain that reads them (`importer/*` →
`db/library.py` → `home/*` → the cards) was written against a fixture where none of that is true.
The card throws inside the client render on 1,216 of 19,071 real titles; 29,188 of 31,540 extracted
DNA rows carry a facet that joins nothing; the catalogue pages over an order that is not total; four
shipped tables are dropped or misreported. **Twenty-three rows were written before the code and
`current_milestone` was raised in the same change**, arming all twenty-three at once — the red list
that run printed, thirty-nine test names of which one existed, is the test plan
(`docs/milestones/M4.9-plan.md`), and it was closed by writing the other thirty-eight. Migration
`0018_read_layer.sql` backfills the facet on both DNA tiers, arms `dna_tag`'s unique index by
making `provider` NOT NULL, and re-keys `title_company` and `title_video` per source. Decisions
**187–193** are numbered in `docs/spec-v2.2-proposals.md`. **No waiver was added and the milestone
was never lowered.**

**One earlier count moved, and it is not a milestone gaining a test.** The block above reads
`M2 27/27` where M4.8's read `26/26`: `library-rate-home-greeting-uses-the-household-clock` states a
§6.0 M2 clause, so it is filed under the milestone whose clause it states rather than under the one
that repaired it — the same way the two M4.13 rows already sit. Nothing was added to M2's scope and
nothing was removed from M4.9's.

**Two of M4.9's browser cases intercept the response they assert against, and that is worth knowing
before trusting them.** `04-title-card.spec.js::the credit list says how many it is hiding and the
disclosure reveals them` needs a title with more than twelve credits, and the fixture's richest
title collapses to two; `10-home.spec.js::a shelf term chip prints its term once and wears its facet
colour` needs a section whose cards share a DNA term, and `why.common_terms` is an intersection over
three or more cards that no three of the fixture's eight titles satisfy. Both take the server's own
payload and extend the one list the fixture is too small to fill — rows of the same shape for the
first, and a term and facet read back from `/api/titles/{id}` for a card genuinely on that shelf for
the second. Neither invents a shape the corpus does not ship, and both are noted here rather than in
a comment nobody re-reads, because the honest fix is in `backend/tests/fixtures/make_bundle.py`,
which M4.8 owns.

**The M4.9 exit criterion is written and has not been run.** `ops/m49_exit_criterion.py` follows
`ops/m45_exit_criterion.py`'s shape — it creates and drops its own database, imports the real bundle,
connects through `db/pool.py` rather than a bare `asyncpg.connect` (the json/jsonb codec
`title_meta.payload` needs), and **refuses to run on the fixture on purpose**, because all twelve of
its measures are zero on eight titles. Run it with:

```bash
CORPUS_BUNDLE_DIR=.../export_bundle/v20260828 \
TEST_DATABASE_URL=postgresql://... \
  backend/.venv/Scripts/python ops/m49_exit_criterion.py
```

Its measure 2 is deliberately not the plan's: §7 writes it as 1,216 payloads mounted through the
real `TitleDetail` in vitest/jsdom, and when the script was written this repository shipped neither
`jsdom` nor `@testing-library/svelte` (finding 27 has since brought the first in, for §6.7's drawer;
the card has still never been mounted), so it is implemented as a key-injectivity check over the
dumped payloads
— exactly what Svelte 5's `each_key_duplicate` checks — with the render itself covered in a real
browser by `04-title-card.spec.js::the worst cross-department titles open without a console error`.
The script says so in its own docstring. **No count is published here** until a real run prints one,
which is decision 184's rule applied to the milestone that follows it.

**M4.8 shipped before it, and it is not in §12 — it was about this file's own subject.** The
eleven milestones between M4.5 and M5 are all failure-driven, and the review that produced them
found that the instrument reporting on them could not report: `ci.yml` ran on `push: main` alone, so
four milestones reached main having never run on Linux; the fixture every importer and placement
test is written against reproduced neither the two-department credit nor the extraction-label facet
the real corpus carries, so two live defects were unreachable at every layer; the two §4.1 landmine
guards missed every merge shape but the one they were written from; `e2e/run.mjs` entered phase 2
with nothing loaded and exited 0 on a suite of skips; and two of the three milestone exit scripts
could not return a failure at all. Nine rows were written before the code and `current_milestone`
was raised in the same change, arming all nine at once; the red list that run printed was the test
plan (`docs/milestones/M4.8-plan.md`), and it was closed by writing the tests. A tenth row arrived
in review, for the two defects this milestone's own new code committed — `e2e/reset.mjs`'s value
parser and 14-tonight's stray-write watcher — which is why the block above reads **10/10**. That
block used to be pasted by hand, and this one was pasted from a run made before the tenth row
landed: it published `9/9`, a count no run produced, four lines above the paragraph where decision
184 refuses to invent one. `test_spec_coverage.py::test_the_testing_ledger_publishes_the_counts_the_gate_prints`
now reads it back against the report, so the ledger CLAUDE.md sends readers to cannot drift from the
gate again. No waiver, and the milestone never lowered. Decisions **183–186** are numbered in `docs/spec-v2.2-proposals.md`. It
writes no migration and amends no normative clause, which is why §12 gained no row for it — M4.5's
shape rather than M4.6's and M4.7's.

The convention it leaves behind, because the next ad-hoc test database should be a decision rather
than an accident: **a pytest process owns `<base>_p<pid>`**, `pg_url` creates it at session start
and drops it in a `finally` — the one frame a terminated session never reaches, which is how 34 of
them and 4,151 MB came to sit in the same cluster the household's own database and the restore
drills need free space in. Each session now sweeps first: every `<base>_p<digits>` whose named
process is gone is dropped before a new one is taken. **Liveness, never connection count** — the
`db` fixture closes between tests, so an idle database is the normal state of a *running* session
and reaping on that would destroy the concurrent run the pid scheme exists to protect. On Windows
the probe is `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`, because `os.kill(pid, 0)` there is
`TerminateProcess` and the POSIX no-op liveness question would kill what it asked about. **A name
without a pid is left alone on purpose**: `spielplan_test_ledger`, `spielplan_test_scoring`, a
`_pgr` from `test_backup.py`, an xdist `_gw0` that carries no pid at all — a name a person chose is
not this convention's to reclaim, and the reaper never raises, because a tidy-up that could not run
is not a suite that failed. If you need a database that outlives its session, give it a name with no
`_p<digits>` and it will survive.

**M4.7 shipped before it, and §12 gained a row for that one.** Decision 181 put that row between
M4.6 and M5, with the argument at spec line 422: §12 scheduled the product and left the box it runs
in unscheduled, so every §2 promise about required configuration, secrets custody with rotation, a
nightly dump with rotation 14 and a restore was documented, coverage-mapped, and either
unimplemented or implemented in a way that failed on the day it was needed. Seventeen rows were
written before the code and `current_milestone` was raised in the same change, arming all seventeen
at once, and all seventeen are closed. The last to close was
`platform-image-runs-unprivileged-and-installs-the-project`, which stood red the longest because
its two guards described an image nobody had changed yet: `ops/backend.Dockerfile` ran its
processes as root, and installed this app's *dependencies* rather than the app, so the three
console scripts `backend/pyproject.toml` declares did not exist inside the container while
README's Recovery block was already naming two of them. Its four clauses now have a guard each and
a self-test each — including the one that reads the declared scripts back and checks each names a
module that really defines the function it points at, because an entry point that exists and dies
on import is the same outage with a better error.

Two of its rows are worth naming for what they cost rather than for what they assert, and both are
drills rather than tests. `platform-restore-drill-boots-and-writes` builds a household through the
routes, takes a real `pg_dump` of it and restores that into a database the stack has already
booted — because the M0 row it stands beside proved its property against a database nobody
restores into, with a bare connection and no route, and shipped both of the live failure modes
past itself. `platform-upgrade-applies-migrations-over-populated-tables` applies
all but the newest migrations, seeds a row in every table those newest ones rewrite, and only then
applies them: the state every existing install is in on its first upgrade, and a path no layer
here had ever executed, since every other layer applies the migrations to an empty database. It
ran `0015_seed.sql`'s backfill over pre-existing rows for the first time in this repository's
history. Five M0 rows were made true rather than narrowed on the way, and M4.7 adds no waiver.

M4.7 also turned this file's own guard rule on the guards that had been exempt from it. Three
static contracts could not fail: the volume guard passed on a compose file of pure comments, the
one-published-port guard could not see a second published port, and the dependency guard merged the
`[dev]` extra into what the image installs, so a production `import cbor2` built a healthy image,
died on first import, and passed the guard whose docstring names exactly that scenario. Each now
carries the synthetic violation it has to catch. The same milestone deleted five router-mount
scaffolds in the test files rather than repairing them: they re-mounted the router the test then
exercised, so `app.py` dropping an `include_router` shipped Home and the whole Rate surface as 404
with the pytest layer green.

**M4.6 is not in §12 either.** Owner decisions 164 and 166
(2026-09-03) put the household's whole user management in §6.6 Users and cut the account table to
two roles. It is its own milestone because §6.6 sketched user management in one line and §12
scheduled it nowhere, while the first-boot wizard — the only path that creates an account today —
is unreachable the moment an admin exists. Nothing writes `role` or `is_active` in the shipped
code, so a forgotten password has no in-app cure. Its twelve rows were written before its code and
`current_milestone` was raised to `M4.6` in the same change, which armed all twelve at once: the red
list that run printed was the test plan (`docs/milestones/M4.6-plan.md`), and it was closed by writing
the tests — no waiver, and the milestone never lowered. `test_account_security.py` is where the rows
that are about an account boundary rather than about §6.6's roster landed, and
`e2e/specs/17-users.spec.js` is the milestone's exit criterion.

**M4.5 is not in §12.** It exists because the row above it was a lie of a particular kind: the
importer was written against a schema nobody had opened, and verified against a fixture that
reproduced every measured landmine and invented every structure around them. The real corpus
bundle — built 2026-08-28, sitting on the same disk the whole time — could not be imported at
all, and `README.md` said it did not exist. Nine of nine Cold Tower feature blocks missed every
column they declared, silently, because the fixture's contract named them the way the builder
keyed them. `test_placement.py` asserted `contract.block("credit").column("3")`; the shipped
contract calls that column `p:director:Michael Mann`.

That is M4's lesson one level up. §5 of `M4-open-points.md` lists five shapes of a test that
cannot fail; a *fixture* written from the same reading as the code is the sixth, and it fails
everything above it at once. The instrument that ends it is
`tests/fixtures/real_bundle_shapes.json` — a committed, data-free manifest of the real bundle's
shapes, extracted by `ops/bundle_shapes.py`, which `test_bundle_shapes.py` holds the fixture to
on every run and holds a real bundle to whenever `CORPUS_BUNDLE_DIR` is set. A column reaches
it as `p:<s>:<s>`, never as anyone's name.

M4.5 also **discharges** the older of the two standing M0 waivers. `platform-backup-rotation-and-
ciphertext` was waived because "the nightly dump job does not exist yet"; it exists now, so the
waiver is removed rather than narrowed and the row owes real tests. One waiver stands: the
feature-builder DB role, re-read again and still true — `grep -rni "create role|grant |revoke "`
over `backend/migrations/` is still empty.

Two owner decisions were taken during it and are numbered in `docs/spec-v2.2-proposals.md`:
**162** (the corpus supplies models; movie data seeds once; Spielplan owns all ids, minted from a
range disjoint from the corpus's) and **163** (a DNA vocabulary change is a data migration, not
an import, and is refused until that migration exists — because a models-only bundle carrying a
new vocabulary would leave both DNA tiers stranded at the old version, and empty is not an error
anywhere in the read path).

Decision 162 rewrote two shipped-M0 rows rather than being weakened to fit them. Both said a
second *content* bundle imports over the first; under 162 a re-import carries models, so the
swap sequence, the diff report and the rebuild set all survive and only the content half goes.
The rows were changed with the owner's approval, never to match what was built.

M2's gate was red for most of the milestone, on purpose: that is what an in-progress milestone
looks like here, and the open rows were the plan. The work landed on `m2` and merged when the
gate closed, which is the rule this section describes rather than an exception to it.

The two standing M0 waivers are backup rotation (no backup job exists yet) and the title
card's no-bundle model line (unreachable until M5, when a locally acquired title can outlive a
deactivated bundle). M1 closed the third — connector env-seeding, which was an implementation
gap, not a test gap, and now has both. M3 re-read both survivors and both still hold, checked
rather than assumed: `grep -rn "backup\|pg_dump"` over `ops/`, `docker-compose.yml` and the
worker still finds only the two volume mounts, and `grep -rni "create role\|grant \|revoke "`
over the migrations is still empty, so nothing can raise `insufficient_privilege`. **M3 adds
no waiver.** M4 re-read both a third time and both still hold — the same two greps, the same
two answers — and **M4 adds no waiver either**. Everything M4 left open is a decision, a spec
defect or a named defect, and those live in `docs/milestones/M4-open-points.md` rather than in
a row that claims to be covered.

M1's tenth row was added *during* the milestone, by the review: §3.1's sequence ends "and
passkey registration is prompted afterwards", and the map — written from the spec at M0 — had
no row for that clause. The map is a contract, not a ceiling; a requirement the spec states and
the map missed is added when it is found.

M2 added two the same way, and they are worth naming because they widened the milestone rather
than sharpening it. §12's M2 row lists "member PWA-install/push onboarding" and the map had no
row for any of it — no service worker, no subscribe route, no screen, and
`POST /api/setup/onboarding/complete` sitting there with nothing on earth calling it. Read as a
gap in the map, that is M2 work; read as prose, it is M4's. It was built, and it is flagged here
because the map going 25/25 should not quietly stand in for a scope decision somebody else might
have made differently. The push *sender* was M4's, and shipped there: §2's VAPID keypair is
generated at first boot, and §7.3's prompt is delivered as a real web push with the in-app
banner still the guaranteed fallback.

M3 added six — four before any code, two from the review. The first four came from reading
§6.3 and §12's M3 row clause by clause against the map. §12 lists "filters" among M3's contents and no row had one; §6.3's
neighbourhood badge ("A — between Heat and Prisoners") and its "learned cutpoints, **not
percentile cuts**" had none either; and nothing asserted that a person can reach the comparison
queue at all, though §6.3 names the control and §12's exit criterion rests on it. Unlike M2's
two, none of these widened the milestone — they are clauses of sections the map already claimed
to cover.

Two of M3's rows are worth naming for what they cost rather than for what they assert.
`tonight-rank-cutpoints-learned-not-percentile` cannot be tested by looking at a healthy board:
a percentile implementation reproduces the measured F3/D7/C15/B25/A25/A+17/S8 shape on *every*
board, which looks more right than the truth on a lopsided one, so the two are told apart by
their invariances instead. And `jellyfin-acquisition-eval-uniform-holdout-stream-never-tunes`
turned out to have three read paths rather than one — the fit, the *selector's* comparison
counts, and the evaluation — because §6.3's exploration arm picks the least-compared title, so
a count that included held-out rows would have made the selector a reader of the evaluation
stream in a way no test of the selector alone could see. The static test that enforces "only
these files may read that stream" found two more read paths in `ledger/refit.py` the moment it
was written — and the review then found it blind to `ARM_HOLDOUT`, the name the package
actually imports the value under.

The review added the last two rows, and they are the ones worth reading twice, because both
are requirements the map did not name *and* the code did not meet.
`tonight-rank-board-survives-a-tier-set-change`: decision 11 keeps `tier_edit` rows across a
change in K, so a level outside `0..K-1` is a state the board is guaranteed to meet — and the
board indexed the cutpoint array by it, so any person who used decision 11's own control lost
`/rank` to a 500 until they re-dropped every affected title. The fit already clamps that case
and logs that it did; `test_a_shrunk_tier_set_still_fits_and_the_old_edits_still_count`
performed exactly that setup and stopped one call short of the read. Three independent review
lenses reproduced it, and the shipped e2e spec walks straight into it — so the gate was green
over a surface that was down. `tonight-rank-queue-pair-is-single-use`: the comparison queue's
sealed pair claimed §6.1's `card_token` property in as many words and did not have it, and
§13's agreement figure counts rows, so a replay weighted one judgement N-fold in the only
data admitted to evaluate the tier model.

M4 inherited 18 rows and closed 42. Twenty-four were added before any code, from reading
§6.2 clause by clause: the rewritten step 4 alone (54b/54c) makes claims about the hold-out
stream, the stopping rule, the escape, the guest prior and the four answers that the inherited
rows named in one line between them. None of them widened the milestone; they are clauses of a
section the map already claimed.

The collision worth recording is that all 18 inherited rows are written against §6.2 **as
rewritten by owner decision 2026-08-29** (v2.2 §54a–54g, decision 154) — an adaptive round with
four answers, three finalists and a blind approval ballot — while v2.1's own §6.2 still
describes the visible shortlist and the mood question. Fourteen of the eighteen are untestable
against v2.1, and v2.1's step 6 asks for an approval share it supplies no instrument to
produce. Built to the rows, on the owner's call, and that is what `docs/milestones/M4-plan.md`
records.

M4's review found what M3's lesson predicted and one class M3 did not name: **four tests that
could not fail**. An assertion whose body sat under `if x is None`; a uniqueness test comparing
two draws from a space thousands wide; a selection test scoring the served pair with the very
function the selector minimises; a blindness test asserting a screen did not contain a string
no template draws. Each passed on an implementation with the rule removed. They are listed with
their rewrites in `M4-open-points.md` §5, because the shape is what to look for — a test that
reuses the implementation's own answer as its ground truth is a test of nothing.

The lesson M3 leaves for M4 is narrower than "review harder": **a row whose `what` names a
surface has to be tested through that surface.** Two of M3's worst findings — the crash and
an entirely uncovered HTTP seam — were rows whose named tests exercised a domain function
directly and never reached the layer the row is about. `backend/spielplan/api/rank.py` had no
backend test of any kind until the review said so, and hard-coding `selection="boundary"` in
it — proposal 120's exact bug — passed the entire suite.

`pytest backend/tests/test_spec_coverage.py -s` prints the live version.

## The release checklist

This is not the milestone gate, and the difference is the point of having both. The gate asks
whether every requirement this repository has written down has a test that can fail; it is
mechanical, it runs in CI on every push, and `test_spec_coverage.py` is its judge. The checklist
asks the four questions the suite cannot answer at all, because each one needs something no CI job
has: a real bundle, a real dump on a real stack, a built image, and a real phone. Run it before a
release, not before a merge. A failure here is a finding, not a red build.

1. **Real-bundle import smoke.** Point `CORPUS_BUNDLE_DIR` at a real bundle and run the backend
   suite, so `test_bundle_shapes.py` holds the actual bundle to the committed manifest; then import
   that bundle through **Admin > Data** on a scratch stack and restart both services. Read the
   import report's counts rather than its "ok" — M4.5 exists because a fixture and the code agreed
   with each other and both were wrong about the corpus, and nine of nine Cold Tower feature blocks
   missed every column they declared while every layer stayed green.
2. **The restore drill.** README's Recovery procedure, on a scratch stack, against a dump the
   worker actually wrote — not one taken by hand for the occasion, and one *this* build wrote:
   a dump restores only into the image that wrote it, and reaching for last month's is how the
   drill turns into a crash-looping backend rather than a pass. It passes when `pg_restore`
   exits 0, `docker compose logs --since 2m backend` shows `applied migrations: (none pending)` on
   the way back up — the line is written either way, so an empty grep is a boot that never reached
   the runner and not a schema that needed nothing — the Jellyfin connector authenticates against
   the real server, and `app_setting`'s push public key is the one in the dump. Then do it again
   with a different `SECRETS_KEY` in `.env`: the container
   still boots, every member write still returns 200 with a reason naming the key, **Admin >
   System** names the key and the active `key_id`, and nothing anywhere returns 500.
   `backend/tests/test_restore_drill.py` asserts those same properties against a real Postgres and
   is not a substitute for the drill: it drives `pg_restore` directly, and nothing in this suite
   has ever executed the compose file the operator is following.
3. **The image, once, on a machine that can build it.** Every assertion about
   `ops/backend.Dockerfile` in this suite is a grep over its text: four guards in
   `test_static_contracts.py` say the file *asks* for a pinned `uv`, a non-root `USER`, the project
   installed rather than only its dependencies, and torch from an `explicit` CPU index, and one in
   `test_box_claims.py` says it does not hand that installed tree back to the user it runs as — and
   not one of them has ever built a layer. `docker compose build backend`, then:
   `docker compose exec backend id` prints uid 1000;
   `docker compose exec backend python -c "import torch; print(torch.__version__)"` ends in `+cpu`;
   `docker compose exec backend spielplan-secrets --help` and `spielplan-movie-data --help` both
   exit 0; `docker compose exec backend touch /app/probe` fails with `Permission denied` while the
   app keeps serving, which is the only place the read-only install tree can be confirmed rather
   than argued (`HOME` is `/app`, so a dependency that writes under `$HOME` surfaces here and
   nowhere else); and the worker writes a dump into the chowned `data/backups` rather than failing
   on permissions, which is the half of the `USER` change that lands on the host rather than in the
   image. `uv pip compile --python-platform x86_64-manylinux_2_28 --python-version 3.12
   --emit-index-annotation --no-cache backend/pyproject.toml` is the cheap standing check for the
   index half and needs no Docker: exactly one line annotated to `download.pytorch.org`.
4. **The phone pass.** e2e's `phone` project (iPhone 13, WebKit) is the primary form factor and it
   runs in CI, but it installs no PWA, receives no real web push, and cannot see a rendering that is
   merely wrong. Walk one member's own phone through it: install to the home screen, allow
   notifications, join a Tonight session a second phone is running, and let a finish prompt arrive
   as a push rather than as the in-app banner that is its guaranteed fallback.

## Writing a test here

- **Assert the behaviour, cite the rule.** Every test in this suite names the spec section it
  defends and, where there is one, the measured fact behind it. A test whose failure message
  does not tell you what broke and why it matters costs more than it saves.
- **Pick the cheapest layer that can actually falsify it.** e2e is slow and flaky; spend it on
  what only a browser can prove. Most rules are cheaper to check in the query layer.
- **A guard needs a self-test.** `test_landmine_guards.py` feeds each of its regexes a synthetic
  violation, because a guard that cannot fail reads as coverage while providing none.
- **Fixtures reproduce the landmines, not the volume.** `tests/fixtures/make_bundle.py` builds a
  bundle with both DNA tiers overlapping, the frozen `rating_source` ids, duplicate `tmdb_id`s,
  NULL alias PK components, CJK/emoji/ZWSP — plus `break_*` helpers that violate one rule each.
  Eight titles that carry every trap beat eleven thousand that carry none. Volume is the one
  exception and it is opt-in: `make_bundle(dir, pool_titles=700)` appends generated owned movies
  drawn entirely from the authored vocabulary, so the feature contract stays byte-identical while
  the owned pool reaches the size a cost measurement needs. Default is 0 — eight titles, the bundle
  all 26 call sites already build.
