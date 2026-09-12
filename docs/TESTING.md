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
> M4   41/41  covered
> M4.5   18/18  covered
> M4.6   12/12  covered
> M4.7   17/17  covered
> M4.8   10/10  covered
> M4.9   23/23  covered
> M4.10   10/10  covered
> M4.11   17/17  covered
> M4.12   18/18  covered
> M4.13   17/17  covered
> M4.15   11/11  covered
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

**M4.15 is the milestone this block was last re-pasted for, and its `11/11` is the red list it
opened with, closed.** Eleven rows were written before any source edit and `current_milestone` was
raised in the same change, each naming the exact tests it owes rather than leaving `tests` off —
M4.9's and M4.13's opening rather than M4.10's and M4.11's, because a row with no tests tells you
only that a row is bare, while a row naming a test that does not exist yet tells you which stage
owes what. That mattered more here than in either of them: the names spread over
**49 ids across three pytest files and five e2e specs**, and half of these rules can be asserted at the
source or nowhere — `env()` resolves to 0 in every engine this suite runs unless one is asked to
report an inset, and a coarse pointer is not something a headless desktop has — so a bare list would
have hidden which layer each rule is held at. Three of the first twenty-five already existed and were repaired rather than written:
`06-responsive`'s touch sweep and `13-rank`'s board sweep, which stop asserting 44 under titles that
say 48, and the mono/display guard the M0 palette row also cites, widened to hold `.why` as well as
`.data`. A fourth existed and is not touched at all — `01-first-boot`'s opening test, which the
browser gate registered onto the 401 row (decision 282) after proving that row's first sentence
false on a first boot: the assertion that caught the defect, at the layer that caught it, and the
fifth e2e spec in the count above. The milestone wrote the other twenty-one, plus the accent guard
it adds to that M0 row, and those **twenty-two names were the test plan**
(`docs/milestones/M4.15-plan.md`). The figure moved while they were being written, so the series is
published in full here rather than left as a total a reader has to take on trust (decision 184):
**21 ids** written against the plan's own rows, **3 ids** registered by the stages that wrote them
onto rows that already stood, **1 id** the browser gate added, **10 ids** the first adversarial
review cycle registered, **8 ids** the second and **6 ids** the third. That decomposition stood at "six more" for the
first cycle until review cycle 2 added it up — a figure published as measured that the map had
overtaken, which is the id total's own defect one granularity down, and
`test_the_testing_ledger_decomposition_sums_to_the_count_it_publishes` now sums the parts against
the map rather than leaving the arithmetic to the reader. Cycle 1's ten are also what bring the
SECOND AND THIRD pytest files into the count, and each of them is a rule that was already shipped
and held by nothing: four controls sitting under `--touch` on an axis or a pointer no sweep in this
suite reaches; the title panel's heading standing under its own close control; the phone project's
`testMatch` deciding which specs reach an iPhone 13 with no gate evaluating it (decision 267, and
the one id in `test_harness_contracts.py`); `svelte.config.js`'s poll interval and the shell's
`beforeNavigate` (finding 23), which no engine here can exercise at all; the logout row's own
sentence, which claimed unconditionally what decision 272's amendment makes true on one branch of
two; the innocent half of the why-register guard's own reach, since a widening measured only on a
clean tree publishes the half of the trade that pays and not the half that costs; the tinted
composite the contrast guard had been measuring past; the licensed focus ring, read by value rather
than by selector; the status-bar inset injected over CDP; and the owed device checks themselves,
which nothing in the tree read at all (the one id in `test_spec_coverage.py`). The second cycle's
seven are argued in the rows' own comments. What the ledger guards below publish is a count of rows
that NAME tests, not of rows whose tests pass, so the `11/11` above is
not by itself the claim that eleven rules hold: the browser gate is the other half of it, and the
six facts below are the part of it that no run in this repository can settle. **No waiver was
added and the milestone was not lowered.**

**The milestone is the box every surface sits inside, and it takes no §12 row: like M4.5, M4.8 and
M4.13 it ships no new surface.** §6's preamble - "responsive PWA, phone-first (48 px targets,
one-handed, swipe), desktop as progressive enhancement, installable, service-worker shell cache" — is
normative, is the only sentence in the document that describes that box, and has never had an owner.
§12 schedules screens; every surface milestone built its own correctly and left the chrome alone; and
until these eleven rows this map named `06-responsive.spec.js` in no row at all. Its three
`02-shell.spec.js` ids were held by M0's session-cookie contract and M4.9's model rail, neither of
them the preamble, so nobody owed the box a test [review cycle 3: M415-C3-COV-01]. What the box
turned out to contain is the
milestone: an installed app whose header renders under the status bar and whose bottom tab bar
renders under Safari's toolbar, because `app.html` asks for `viewport-fit=cover` and a translucent
status bar while the only `env(safe-area-inset-*)` in the tree is the nav rail's bottom padding and
the shell is `100vh`, which on iOS Safari is the LARGE viewport; form controls at 10-13 px on every
screen a member touches, which that browser zooms on focus and does not zoom back; a `--touch` token
that says 48 px against a stack of 33 px links to /account and a full-bleed overlay whose only exit
is 32 px wide; menus and panels with no outside tap and no Escape at all (proposal 131); quiet
reasons set in the data face §6.8 reserves for model numbers and IDs, at an ink token measuring
2.82:1; one accent spent on six meanings across fifteen files; and the one module that knows the
wire holding no deadline, no 401 branch and no case for a pydantic 422. One item on that list did
not survive being measured, and it is corrected here rather than left standing: the plan read three
declared font weights whose `src` is the 400 file and concluded nothing in the app had ever
rendered bold, but both families ship a VARIABLE woff2 — `fvar` gives Space Grotesk a wght axis of
300..700 and JetBrains Mono one of 400..800, and the css2 endpoint returns a single URL for all of
them; the run that regenerated the four committed files matched every one, by sha256, against what
Google serves today, and no binary in the repository changed. So the 500 and 700 faces are real
instances and always were. Deleting them would have handed bold to the browser's synthesiser and
committing six files would have pushed the same bytes to the phone three times, so what shipped
instead is `ops/fetch-fonts.py` — the generator `fonts.css:4` had named since M0 and which did not
exist — and a guard that asserts the rule rather than the bytes: a face declared against a file that
does not carry that weight in its name is honest only where the file carries a wght axis. Decisions
**267-286** record the calls it needed, four of them refusals, and it writes **no migration** — 0022
is the highest applied, 0023 is M4.14's and 0024 is M4.16's. Two rows the map already had are
amended in place rather than duplicated: M0's palette row, which forbade "no facet colour or user
identity colour reuses the ember accent" while its own named guard whitelists `facet-mood` in as
many words and reads `design.css` alone (decision 276), and M3's
`tonight-rank-tap-to-tier-and-cancel`, whose text does not change at all while the test it names
stops asserting 44 under a title that says 48.

**Four of its facts cannot be produced by any run in this suite, and they are recorded as owed
rather than signed (decision 281).** A green Playwright run is not evidence for them and never was:
`06-responsive.spec.js` has been green through every milestone in which the first two were false,
because the engine it drives has no status bar, no dynamic toolbar and no focus zoom. The fourth
joined them at the second browser gate, on measurement rather than by argument: Playwright's WebKit
answers `page.reload` with "WebKit encountered an internal error" while the context is offline,
nine milliseconds in and before the worker holding the cached shell is asked, so the phone project
skips that test and says so rather than asserting an offline boot no engine here can perform
(decision 284). So they are written here as an outstanding check, in the shape decision 184
requires of any measurement — the run that produced it, or nothing:

- **the header clears the status bar** in installed standalone mode (Add to Home Screen, not a
  Safari tab), at the top of every surface and with the phone rotated. Review cycle 1 took half of
  this out of the dark and review cycle 2 finished the half it had only claimed:
  `06-responsive.spec.js` injects a 47 px top inset into Chromium
  (`Emulation.setSafeAreaInsetsOverride`) and measures the header at the desktop project's 1400 px
  AND again with that same page resized to 390, where `@media (max-width: 720px)` is live and the
  phone override's own `min-height` is what decides the box — so the ARITHMETIC of both rules is in
  CI. Until that resize the test ran at 1400 px only, where the phone block is inert, and this line
  claimed a composition nothing was performing. The DEVICE fact is not in CI, and is what this line
  owes —
  desktop Chromium with a number injected has no standalone web view, no real inset and no
  rotation, and the `phone` project's WebKit has no CDP to inject one with;
- **the tab bar stays above Safari's toolbar at every scroll position**, in a tab and installed,
  including mid-scroll while the toolbar is collapsing;
- **no form control zooms the page on focus**, on the device rather than in an emulated viewport —
  /login's field first, because it is the first thing a new member touches;
- **the installed app opens with the appliance unreachable** — aeroplane mode, then launch from the
  home screen: the shell cache serves the document, the unreachable card says so, the session
  survives, and the shell comes back by itself within a few seconds of the network returning
  (decision 283's retry), with no reload gesture used.

Two more join them at the first review cycle, and they are owed for a sharper reason than the four
above: the copy already asserts them. `Onboarding.svelte`'s iOS branch tells a member in as many
words that the home-screen icon keeps its own sign-in, and offers a passkey as the thing that
shortens the second sign-in that follows — which only means anything if a passkey registered in the
Safari tab answers inside the installed app. The plan's finding 22 said to verify both on a device
before the copy asserts them, and the milestone built decision 281's mechanism for exactly this
shape of claim and then did not use it for either:

- **the home-screen icon opens on /login**, so the installed app really does hold its own session
  rather than inheriting the tab's — Add to Home Screen while signed in, then launch the icon;
- **a passkey registered in the Safari tab signs in inside the home-screen app** — register from
  `/account?welcome=1` in the tab, then sign in from the icon with no password typed. If it does
  not, the sentence offering the passkey "before you go" comes out rather than being hedged.

`verified on iPhone ________, iOS ________, by ________, on ________` — **unfilled.** Nothing in
this repository can fill it; the owner does, after the browser gate, and until then this milestone
closes with a visible debt rather than a quiet one.

**One of its ten exit measures is not reachable on this branch at all, and that is stated rather
than engineered around (decision 273).** `npm --prefix frontend run check` stood at 28 errors and 1
warning on a clean tree while the only static signal the frontend has was buried among them, and
this milestone adds the step that runs it in CI. Twenty-seven of those errors are this lane's and
all twenty-seven are gone: the branch closes at **1 error and 1 warning across 275 files**, the
error being `src/routes/admin/data/+page.svelte:14`, which M4.14 owns in the sibling worktree this
wave and which this lane did not open, and the warning `PosterCard.svelte`'s `line-clamp`, which
the criterion does not count. So the frontend CI job is RED here until the two branches merge, and
the check is neither narrowed nor suppressed to make the number look right: clause 10 of the exit
criterion is met on the merge and not before. M4.14 is being built in parallel on its own stack and
inserts itself into `MILESTONES` between M4.13 and M4.15 when the branches meet; the one-line
conflict here and in that list is expected.

**M4.13 shipped before it, and its `17/17` closed the red list it
opened with.** Fifteen new rows were written before any source edit and `current_milestone` was
raised in the same change, each naming the exact tests it owes rather than leaving `tests` off —
M4.9's opening rather than M4.10's and M4.11's, because a row with no tests tells you only that a
row is bare, while a row naming a test that does not exist yet tells you which stage owes what. That
list, 77 names across seven files, *was* the test plan (`docs/milestones/M4.13-plan.md`), and it was
closed by writing those tests under those names: all seventeen rows now name tests that exist —
**98 ids in seventeen pytest files and no e2e spec**, the last six added by review cycle 2 —
five to three rows whose sentences claimed more than their tests held, and one to §10's refusal
row, whose sentence named a silence the worker does not leave — and the two ledger guards below were red
for exactly as long as the block above was M4.11's, which is the honest state of an opening rather
than an omission. What those two publish is a count of rows that NAME tests, not of rows whose tests
pass, so pasting a `17/17` before the tests existed would have published a number nobody ran, which
is what decision 184 refuses; they are re-pasted here, last. The zero is the first one this sentence
has carried: the plan's layer table says "e2e: nothing", because M4.13 ships no surface and there is
no browser fact for a spec to hold. **No waiver was added and the milestone was not lowered.** The
one standing M0 waiver, the feature-builder DB role, was re-read rather than skipped: it is about a
role the compose stack does not create and has nothing to do with this milestone, so it stays as it
is.

**The milestone is the model basis: a fit that knows the basis it was computed in.** §10 says "no
process may score or refit with a loaded bundle version different from the active row", and the one
function that enforces it had no production caller at all — two rows in the map cited its unit tests
as evidence for a refusal no code could perform. Underneath that, `placement_embeddings` accepts a
bundle version `standard_embeddings` never passed, so §10 step 3's pre-flip rebuild fitted against
the OUTGOING bundle and stamped the outgoing version; an active bundle row whose directory is absent
read as an EMPTY store rather than a broken one, so every board was fitted from zero embeddings and
stamped as current; the fit and the serve took different coordinates for a third of the covered
rows; a `tier_edit` kept across a K change carried no record of the K it was written under; a
verdict recorded during a refit was reverted to the unobserved prior; the 60-second fold-in tick
rewrote a whole partition every pass; and seven observation tables plus §13's outcome row declared
ON DELETE CASCADE against §10's promise that observations always survive. M4.13's range in
`docs/spec-v2.2-proposals.md` is **234-246**: decisions **234-241** taken as the milestone opened,
exporter asks **242-243**, and decisions **244-246** taken as it closed (the commit split, 0022
corrected in place, and when the exit criterion is re-run). Four of the opening eight are refusals,
including the two Backbone-scale questions, which go upstream to the corpus project under §4.1's
"carried over verbatim" rather than being answered here (decision 236). The one migration is
**`0022_model_basis.sql`** (decision 239).
Seven rows the map already had were amended in place rather than duplicated — six gaining a clause
and the ids that assert it, the seventh only its citation — and the milestone takes
**no §12 row**: like M4.5 and M4.8 it ships no surface, and what it repairs is a row §12 already
has — M2's Personal Ledger, closed against a basis the app inferred rather than one it was told.

**Six of the plan's steps were already on main when the milestone opened, and they are named here
so the next reader does not read six absences as six omissions (decision 241).** Step 12 is the
cold-mask exclusion (`65614ae`, now `scoring/backbone.py::cold_row_mask`, its coverage row already
green), step 16 the β copy correction (`c4e74e6`, decision 167, which answered the plan's D1 the
other way and cancelled its second migration), step 25 `clear_refit_request`'s timestamp predicate
(M4.10 finding 5), step 29's worker half the per-item isolation in `worker._tier_set_refits` (M4.10
finding 6), step 33 `model.straddle`'s adjacency clamp (M4.10 finding 13 — so `ledger/model.py`
takes no edit at all this milestone, which is what makes its numpy-only ast guard safe to land
early), and step 37 the LIKE escaping now in `db/library.py::_like_needle` (M4.9 finding 12). Each
was verified in the tree rather than taken from a commit message. One line out of those six was
genuinely missing at that point and is this milestone's own: the
`PostgresConnectionError`/`InterfaceError` re-raise, which now sits at `ledger/refit.py:847`,
`worker.py:493` and `worker.py:621` — three sites, not the two the reconciliation expected, because
the refit loop's handler had to be widened here as well as read. So the six close with nothing
dangling. [M4.13 cycle 1, M413-DOC-02]

**Its exit criterion is `ops/m413_exit_criterion.py`, and it is six checks and one report rather
than the plan's seven checks (decision 240).** The plan's check 4 — the ratio between the two
weighted halves of §5.1's blend, against a p90 threshold — is D2-gated, and decision 236 sends D2
upstream, so it becomes a printed report of that distribution and of `max abs(user_score)` for a
fitted member, with a line naming decision 236 as what it is waiting on. That is M4.8's own
precedent, taken for the same reason: a check whose predicate is a constant is what
`test_no_milestone_exit_check_has_a_constant_predicate` exists to fail, and a script that can never
print green is a gate nobody can pass. It is the **sixth** `ops/m*_exit_criterion.py`, so the five
guards in `test_static_contracts.py` that open with `assert len(EXIT_SCRIPTS) == …` move to 6 in
this lane — and to 7 once M4.12's script merges, since both milestones add one.

**It has been RUN, against `CORPUS_BUNDLE_DIR=v20260828`, and it prints
`6/6 checks passed, plus one report with no verdict`** — so the number below is a measurement
and not decision 184's invented one. Run again at the end of review cycle 1, because that cycle
edited three of the six things it measures — `load_cache`, which now compares the CALLER's basis as
well as the active row; `assert_matches`' request-path callers, which now ask
`assert_not_broken` beside it; and a fifth caller of `rescale_level` in `rank/drop.py` — and
decision 246 makes a stage that touches what the script measures re-run it end to end and restate
every figure in the same change. It printed 6/6 again and the figures below are that second run's;
one moved, and only because it had been published one digit short (the shrink's mean is 4.9525,
which the script has always printed as `5.0`). In order: the rebuild's fit is stamped with the bundle it was
computed in and the first tap after the flip reports `refit = False`; the scoring entrypoint answers
409 and the refit entrypoint raises, each naming both versions; **0** of the install's 1,288 owned
titles are served at a zero coordinate, of which 398 carry a cold-masked Backbone row and 375 of
those ship `item_n >= 90` — that is the population the 182 came out of, and the count is now zero
over a larger one; 200 tier edits survive both K changes with a mean rendered-tier meaning shift of
**1.8** points growing to twelve and **5.0** shrinking to four (the plan's simulation read 20.1, and
its max of 11.0 points for the shrink is reproduced to the decimal); a `DELETE FROM title` against a
verdict, a duel and a `session_outcome` is refused with all three rows intact while a title carrying
only derived rows still deletes; and the fitted coordinate equals the served one for all
**11,934** titles that have one, largest difference exactly **0**. The report prints
`((1-g)*||e_hat||)/(g*||E||)` over the 208 rows §5.1's middle line applies to at **p10 90.7,
median 396.1, p90 4,783.0**, and the fitted member's 9,724 owned scores at **-13.73..+25.59** —
numbers with no verdict beside them, waiting on decision 236.

**The run found two things no reading of the tree had.** The first is an exporter ask and is
numbered 243: a models-only re-import of v20260828 is REFUSED, because decision 162 requires
`backbone.npz` to carry a `title_identity` array row-aligned to `title_ids` and `mdc export-bundle`
writes none — so §10's swap sequence, which is what this whole milestone is about, is unreachable
with any bundle the corpus has built, and every test of it runs on a fixture written to the
contract. The script supplies the array from the install's own spine so that check 1 can measure the
stamp, and says so where it does it. The second was in the script: check 5's first definition of "in
tension" asked whether the refitted board renders any title at the dropped level, which a
corpus-scale board cannot satisfy however the rescale behaves — a 7-level history maps to at most 7
of 12 levels, and 1,288 owned titles the person has never rated cluster their `s` at mu. Measured,
that definition called 52% of the drop stream tense on a tree where the rescale is working. It now
asks what the plan's sentence actually says — whether the level is more than one tier from
everything the kept history can be rendered at — which the clamp fails at 32% and the rescale
passes at 0%, and the board's own tiers are printed beside it with nothing gated on them.

**M4.12 shipped before it, and its `18/18` is a gate that was
red on purpose until the last stage closed it.** Seventeen rows were written before the code
with no `tests` key on any of them and `current_milestone` was raised in the same change: the
list that run printed — those seventeen ids
— *is* the test plan (`docs/milestones/M4.12-plan.md`), the way M4.10's and M4.11's openings
worked. The eighteenth row was already green the day the milestone opened, because the vectorised
pair search shipped ahead of it under `ROADMAP-to-M5.md`'s "Start here" table, and the first of the
seventeen to be filled in is the boundary constant, the second is the lifecycle owner that lets a
read move a room the combine left behind, and the third is the group of three that keep a seat
from stranding — the join/start claim, the seat that ends itself when the round has nothing to ask
it, and the refusal that names the unscored member instead of blaming the budget. The fourth is the
pair that make a seat's writes serialise: the answer, the undo and the escape behind the seat's own
row lock, and the ballot behind an advisory lock on its participant. The fifth takes the pair
search off the event loop and out of the answer's transaction, and stops every write route paying
for the round a second time to build the card it had already drawn. The sixth is the way out of a
room that has stopped progressing at all: decision 169's host-only end control, which writes
`abandoned` rather than deleting anything, and the seats of an ended room, which kept being served a
pair and a fresh card token after the host had closed them. The seventh is the session channel,
where the repair is mostly about where things are declared: the socket takes `deps.active_user_ws`
instead of reading the cookie in its own body -- so §3.1's lock is now enumerable rather than
merely enforced -- and it takes no `deps.db`, because a yield dependency on a socket would hold one
of the pool's ten connections for as long as a phone watched the lobby. The eighth seals §13's
hold-out: the pair the arm draws is a function of a nonce frozen with the pool rather than of the
request that asked, so twelve reads of one card return one pair and one token where they returned
twelve, and the arm itself becomes 54b's *rate* rather than every tenth slot -- which is what gives
a round a household cut short at pair six any hold-out rows at all. The ninth is the combine's
split branch, where a leading candidate that carried no term on the contested axis silenced a
surfaceable split and shipped a ranking nobody is shown, a pool of exactly two candidates reached a
`next()` with no default, the wildcard and every persisted rank came from a ranking the finalists
did not come from, and 54d's reserved third slot -- "**labelled as such**" -- was labelled nowhere
between the rule and the screen, which migration 0021's `session_result.reserved` now fixes. The
tenth is 54f's solo screen, which the spec puts on "the fastest path to a film": its door and its
Reshuffle ran the whole adaptive pair search on every tap without ever drawing a pair, its walk
could not reach the last-ranked title of a pool of four or seven, its tilt counted answers the
replay twenty lines above had deliberately skipped, and a malformed answer left the route as a 500
rather than a 422 naming the field. The same stage answers decision 218 -- an untagged candidate is
a zero and no longer the pool's anti-title, which is a third of the shipped library moving as a
block -- and decision 219, which makes a series night's budget say that its minutes are per
episode. The eleventh and last is the browser, where three of these rows live and could live
nowhere cheaper: a room opened with any guest seat could not reach 54e's reveal at all, because
`ballot.submitted_count` counts guests and Submit was bound to the viewer's own seat, so the
count stopped one short for ever and what was missing was a *control*; a household frame from
another device — broadcast on every open, start and end — replaced a guest mid-turn with the
phone owner's ended round, and the clobber happened inside the handler that read the frame; and
every `session_answer.latency_ms` ever written is 0, because the elapsed time was evaluated
inside the object literal one statement after it was started, which is the instrument §14 risk 6
makes the precondition for re-tuning the round. The same stage retires the TV client under
decision 165 — the kiosk route's own directory under `frontend/src/routes/`,
`e2e/specs/16-tonight-tv.spec.js` and the `tonight-rank-tv-kiosk-route` row in one change,
because each was the others' red gate — and it takes the last `pytest.skip` out of a registered
Tonight test: the one integration-layer case of 54d over real rows gave up when the fixture
stopped dividing the household, and with `combine.contested_facet` patched to return None it
reported that regression as `1 skipped` and a green file. The qualifier in that sentence is part
of the claim rather than decoration — registered tests in other suites still skip deliberately,
and an auditor who read the class as closed everywhere would stop looking — so
`test_the_testing_ledger_does_not_claim_a_skip_this_milestone_did_not_take` reads the scope out
of this paragraph and names the survivors the moment it is widened. So the map today holds
107 ids in ten pytest files and one e2e spec for M4.12, and the count includes the vitest
ids decision 226 admits as supporting evidence beside a Playwright or backend test but never
instead of one. The ninth file arrived from review cycle 1: decision 214's re-tune was inert on
every stack this repository can boot, because the corpus ships neither §6.3 threshold and
`make_bundle.py` -- the only writer of the only bundle `npm --prefix e2e run fresh` and
`ops/devstub.py` can load -- still emitted the retired `straddle_z = 1.0`, while the test grading
the clause built its board from `DEFAULTS` and could not see the override. A real household is
unaffected, and that is how it survived a milestone: an imported corpus bundle carries no
threshold to override the default with. Two others arrived from the browser gate rather than from
the plan: the clock the latency row is about is re-armed by `loadRound`, which is the tail of `refresh` and therefore
of every channel frame, so it was measuring the time since the last thing anybody in the
household did — a card deliberately held for 400 ms was recorded as 176 — and the pair the person
is reading is identified by its sealed card token, in both directions, because a clock that never
re-arms passes the first assertion on its own. Cycle 1's surface pass adds six more. Two are
mounted pages, because both claims are about what the component does and neither can be produced
from the store: the same clock ran on through intervals in which the page did not exist -- the nav
rail renders over a live round, so one tap on Rank and back charged the next answer the whole
absence, 181200 ms for a 900 ms read -- and Reshuffle pressed inside the sharpen round left the
local flag standing, so the screen announced a round that was out of questions and hid the only
control that could ask for one. Two more hold the walk that control advances: its counter may not
run past `SoloBody.offset`'s own bound, nor move on a press the route refused. The last two are
legs on browser cases this file already had, which is where decision 222's wrap line is finally
read by something that renders it. The tenth file is `test_static_contracts.py`, and it
arrived with cycle 1's backend pass for a reason worth stating: three of this milestone's defects
were defects of the RECORD rather than of the code — a causal claim about a seam `app.py`
contradicts, four citations into a client module the same diff rewrote, and a line of copy decision
222 ships that nothing asserted at any layer — and none of them could go red. Each guard weighs the
claim against the artifact that settles it, which is the only reason a comment or a rendered string
can be held at all. Cycle 2's backend pass adds four more, and every one of them is the
same shape as the defects above: a rule whose caller was missing. The round read was the one handler
that neither settled nor pushed a frame, and on a pool of two or three candidates it is the only
thing that can end a seat — so a household with three owned shows reached the ballot only when
somebody reloaded, while the domain tests for that clause called `play.settle` themselves and stayed
green. `ballot.submit`'s per-participant lock met `resolve` on nothing at all, so a changed-mind
re-submit could commit between the tally and the row it is stored as and leave §13's share
describing a ballot §14 risk 6's own rows contradict. The socket gate decision 225 added was the one
acquire in `api/deps.py` with no bound, so a saturated pool left the handshake pending with nothing
sent and nothing logged while every HTTP surface answered 503 in ten seconds. And the pair search
evaluated the error function four times per pair under a comment block that concludes three — a
defect of the record, on the paragraph that is step 1's own performance argument. Cycle 2's
surface pass adds one more and repairs two assertions that could not fail on what they claim. The
seat a ballot Submit NAMES was asserted nowhere below Playwright -- the page cases read the drawn
controls and the store cases pass the seat in as an argument -- so putting the pre-milestone
`submitBallot(me?.participant_id)` back left every frontend test green while a guest's vote
re-wrote the host's; the mounted page now reads the seat off the request. The browser's sharpen
case asserted that the FIRST answer counts, which decision 223 turned into a property of whichever
account id `run.mjs` happens to mint -- 6, 21, 43, 48 and 58 hold out at seq 1 -- so it answers
until the line moves instead, bounded, the way the route's own tests compute the first held-out
seq rather than assume it. The per-episode row gains the readout that SETS the number, which is
the one label decision 219's first pass did not reach. Three rows this table already had were amended in the same change rather than
duplicated: `tonight-rank-holdout-one-in-ten`'s `what` stated the hold-out's *schedule*
rather than 54b's rate, which is exactly why the gap it describes was invisible to the build; the
guest hand-off row covered the round hand-off and not the ballot, which is the half no screen could
reach; and the escape row said nothing about a seat that has already ended. The §12 row it takes is
at spec line 416, on M4.9's, M4.10's and M4.11's argument rather than M4.6's and M4.7's: M4's exit
criterion, "a real Friday night resolved by the app", was closed against a six-title candidate
pool, a household of one account and a guest seat no screen could take to the reveal. Decisions
**214-226** are numbered in `docs/spec-v2.2-proposals.md`, and the first of them was ruled before
this plan was read — decision 175 gives Tonight its own boundary constant and decision 205 deferred
that work to here. It is taken as measured rather than argued, and these are the numbers, over pools of
12/20/40 at the shipped owned-pool spread: at the borrowed `straddle_z = 1.0`, `converged` fires
0-2 times in 20 simulated evenings and every median round is the cap of 20; at `BOUNDARY_Z = 0.6`
it fires 13-17 times in 20 with a median of 8.5-13 pairs, which is §6.2's "~10 candidate votes";
and the retuned badge constant of 0.15 leaves **31 of a fitted 120-title board** straddling where
1.0 left 120 of 120. Narrowing `prior_var`, which `M4-open-points §1.1` proposed instead,
converges 0-1 of 20 at the old threshold and *fewer* rounds at the new one on every one of those
pools, so it is recorded as refuted. Those ranges were first published as single figures taken
from one pool size and two of them did not reproduce, so review cycle 1 re-ran the sweep and
`test_tonight_round.py::test_the_sweep_this_constant_was_calibrated_against_still_reads_this_way`
now holds this paragraph and the comment beside `BOUNDARY_Z` to the harness that produced them. **One migration**, `0021_tonight_reserved_slot.sql`, which labels 54d's
counterweight; `0019` stays the permanent gap it has been since M4.10 and `0022` belongs to M4.13,
built in parallel with this one. **No waiver was added and the milestone was not lowered.** The
mechanical id sentence moved here from M4.11's paragraph below, which states the same count in a
phrasing the guard does not read: `test_the_testing_ledger_counts_the_ids_the_map_actually_holds`
is scoped to `current_milestone`, and exactly one sentence in this file may carry that shape.

**The M4.12 exit criterion is written and has not been run.** `ops/m412_exit_criterion.py` is the
sixth `ops/m*_exit_criterion.py` and follows the shape of the five before it: it creates and drops
its own DATABASE, connects through the app's own `db/pool.py` rather than a bare
`asyncpg.connect` (the json/jsonb codec `session.context` needs — the frozen pool this whole
milestone is about is jsonb), plays the evening through the real ASGI app over HTTP with three
real cookie jars, and **refuses to run on a fixture pool on purpose**. The refusal is the point:
checks 2, 3 and 4 — a guest seat's first pair, a zero-label member's first pair and first answer,
and an unrelated read served while an answer is in flight — are the three no fixture in this
repository can falsify, because every candidate pool in `backend/tests` is four to twelve titles
and a pool that small serves its first pair in milliseconds whatever the selector costs. It takes
M4.6's account routes for the two members (there is no other way to obtain a second cookie, since
`POST /api/setup/admin` is first-boot only), §5.3's own `foldin.run` for their scores rather than
an INSERT into `user_score` — §6.2 step 3's candidate pool *is* that table, and a harness that
seeded it would be measuring its own idea of a score — and a floor of 600 scored owned movies
under the shipped 696, below which it refuses rather than reporting a budget nobody was near:

```bash
CORPUS_BUNDLE_DIR=.../export_bundle/v20260828 \
TEST_DATABASE_URL=postgresql://... \
  backend/.venv/Scripts/python ops/m412_exit_criterion.py
```

Three of its twelve checks are source reads and the script's own docstring says so rather than
dressing them as runtime facts: decision 165's TV retirement (no route, no spec file, no coverage
row, no normative sentence), the single lifecycle owner (`play.finish` called from no route), and
the coverage map's close. That third one deliberately does not shell out to `pytest` or to
`npm --prefix e2e run fresh`; both commands are at the top of this file, and a harness running the
suite would report its own subprocess's opinion of a tree it had just changed. What it checks
instead is the half a green suite cannot show afterwards — that the milestone was closed at
`current_milestone = "M4.12"`, that every M4.12 row names a test, and that the set of waived rows
is still the two this milestone inherited. Being the sixth script, the five guards in
`test_static_contracts.py` that open with `assert len(EXIT_SCRIPTS) == …` now carry 6, and the
number moved only after each of their rules had been read against it and come back empty: no
printed literal outside cp850, no `check()` predicate settled before the run, a computed terminal
verdict, no component read with its comments in. M4.11's paragraph below records why that order is
the only one in which bumping a tripwire is not the same thing as disarming it.

**The shipped corpus ships no axes, so §6.2 step 5's split surfacing is inert on release data, and
a green split test is not a statement about release behaviour.** The real export's
`artifacts/dna_vocab/v1/` contains no authored axis definition (M4.5-plan decision 4 declared the
gap, and decision 173 leaves shipping them as corpus-side work — proposal 140, not in this
repository). The consequence runs the length of the chain: `dna_axis_weight` stays empty, so
`tonight/dna.axes_for` returns `{}`, so `combine.contested_facet` iterates zero axes and returns
None, so `session_result.conflict` is NULL on every evening a household plays and §14 risk 6's
split rate reads a permanent 0 that says nothing — and `round._axis_span` is 0.0 for every pair,
so 54c's widest-axis tie-break is dead too. §6.4's Map has nothing to plot for the same reason.
So every test in `test_tonight_combine.py`'s M4.12 section hand-seeds its axes and the section's
own header says so: they assert what the **rule** does when a contested axis exists, which is a
different claim from what a household sees this Friday, and reading the first as the second is
how four defects in one branch survived a milestone. M4.12 repairs the branch anyway — a neutral
leader no longer silences a surfaceable split, a two-candidate pool no longer reaches a `next()`
with no default, the wildcard comes from the ranking the finalists came from, and 54d's reserved
card is labelled — so that the day the axes arrive is not also the day all four surface on real
pools. What the milestone made honest in the meantime is the reporting: decision 173 moves the
loader onto §6.4's own flat path, the import warn names **both** disabled surfaces rather than the
Map alone, and §6.6's Data card renders `spielplan.api.admin.AXES_DISABLES` instead of sending an
operator to a directory the app no longer reads.

**M4.11 closed the red list it opened with, and the `17/17` above is that close.** Seventeen rows
were written before the code and `current_milestone` was raised in the same change, with no `tests`
key on any of them — M4.10's opening repeated because it worked: the
list that run printed, those seventeen ids, *was* the test plan (`docs/milestones/M4.11-plan.md`).
All seventeen now name tests that exist — 103 ids in twelve pytest files and two e2e specs — and
seven rows this table already had were amended in the same change rather than duplicated: the three
§7.3 M1 rows, the §7.1 upsert row, the banner-path row, §3.3's link row and M2's push-subscription
row each gained the assertions this milestone's household makes possible, which is the point of
amending rather than adding — a second row asserting the same clause against a harder fixture leaves
the easy one standing as a claim about the app. The §12 row it takes is at spec line
416, on M4.9's and M4.10's argument rather than M4.6's and M4.7's: M1's exit criterion, "seen states
flow both ways for both users", was closed against one member, one copy per title, no series and no
second phone — not the household §3.3 describes. Decisions **210-212** are numbered in
`docs/spec-v2.2-proposals.md`, and two of them were half-ruled already by decision 172, which is why
the one migration — `0020_jellyfin_items.sql`, the Jellyfin copy set — does not carry the fifth
`prompt_state` the plan sketched: the sweep stops adopting under an open prompt instead, so there is
no sync-closed prompt left to label. **No waiver was added and the milestone was not lowered.** No
count beyond the block above is published here until a run prints one, which is decision 184's rule.
The id count that used to stand in this paragraph's first sentence now stands in M4.13's
block instead: `test_the_testing_ledger_counts_the_ids_the_map_actually_holds` admits exactly one
such sentence per file and re-derives it from the map, so it is rewritten by the milestone the block
was last re-pasted for rather than appended to — which is what keeps two milestones from both
claiming it with figures from different runs.

**Two registered tests changed name, and both changes were forced by a decision rather than by
taste.** `test_declining_the_prompt_writes_nothing_and_closes_it` became
`test_declining_the_prompt_writes_unseen_and_closes_it`, because decision 211 makes the declining tap
an explicit action: "nothing" was the defect — an empty `user_title` row is what let the next sweep
adopt Jellyfin's Played flag over the person's answer within fifteen minutes. And
`test_a_rewatch_on_the_same_device_asks_again` became `test_a_rewatch_in_a_new_viewing_asks_again`,
which the plan did not foresee: the dismissal guard is keyed on `jf_session_id`, and
`0006_jellyfin.sql:29-37` records that Jellyfin derives that id from the client and device, so the
same television's rewatch is the cost the guard takes and the old name asserted the opposite. Four
tests this map already named were rewritten **in place with their names kept**, because each name
still describes what its body asserts: `test_an_open_prompt_closes_when_the_state_arrives_another_way`
(the sweep is no longer one of the ways the state can arrive, so the other way is now an explicit
`seen.set_state`), `test_a_duplicate_copy_marked_in_jellyfin_is_adopted`,
`test_retract_puts_back_exactly_the_flag_the_forward_action_set` and
`test_a_null_identity_column_is_filled`. A fifth, `test_a_clean_sync_clears_a_stale_re_link_flag`,
was rewritten the same way and is registered here for the first time — it used to promote a link on a
sweep that pushed nothing, which is the defect the row beside it now names three silences against.
A rename is the expensive kind of change in this map — it breaks `test_every_named_test_exists` until
the map follows it, in a file no test stage owns — so the rule the milestone worked to is that a body
may be rewritten freely and a name only when a decision made the name false.

**The exit criterion is `ops/m411_exit_criterion.py`, and it scored 11/11.** Eleven checks over a
database it creates and drops, with `ops/fake_jellyfin.py` in-process and the app's own `db/pool.py`:
the scenario is two members, a duplicated copy and a running series, and the checks are the clauses
of §12's M1 row read one at a time — an explicit "not seen" surviving two sweeps, a series staying
seen while its folder flag recomputes, a tokenless link that adopts and stays owed, a revoked token
that stays `needs_relink`, ownership falsified for a removed title, a pointer stable over six sweeps,
a 404 on every write counted rather than promoted, an Episode session arming its series prompt, one
row and one push for a declined viewing, a second device listed by handle, and a distinct tag and url
on both notifications. The same script scores **1/11** against the pre-milestone backend (M4.10,
`892c1f2`, with this milestone's `ops/` and migration 0020 alone), which is what makes the eleven
measurements rather than assertions; the one check that passes both ways says so in its own output,
because the browser half of the second-device story belongs to `e2e/specs/12-onboarding.spec.js` and
no browser is faked in an exit script. It is the **fifth** `ops/m*_exit_criterion.py`, so the five
guards in `test_static_contracts.py` that open with `assert len(EXIT_SCRIPTS) == …` now carry 5 — and
the number moved only after each of their rules had been read against the new script and come back
empty, which is the only order in which bumping a tripwire is not the same thing as disarming it. The
count is deliberately asserted rather than inferred, because a guard that globs its own subjects goes
quiet when one of them is renamed out of the glob, and the price of that is remembering to move it:
this milestone paid it late, with all five guards red on the same line for the length of one fix.

**Two of M4.11's e2e edits are in Rank's territory rather than its own, and they are named here
because nothing else in the diff can name them.** `e2e/helpers.js::waitForBoard` and
`e2e/specs/13-rank.spec.js::rateSome` belong to §6.1 and §6.3; the milestone's own e2e work is
`08-jellyfin.spec.js` and `12-onboarding.spec.js`, which its plan names and which its coverage rows
point at. Neither Rank edit changes what a spec asserts — the poll body, timeout and predicate in
`waitForBoard` are unchanged and only the thrown message differs, and `rateSome`'s added
`class_balance.total > 0` fires only where the `waitForBoard` eleven lines later would have spent
120 s and failed regardless, so it cannot redden a run that would otherwise pass. They are here
because M4.11's two-phase run hit the old message and it named the wrong suspects: it offered "the
seeded ledger or a stopped worker" for two states with opposite repairs, and the milestone had just
made the worker half of that transient — finding 17 put `Job.timeout` on `_tick` with `timeout=55`
on `tier-set-refit` itself, so an abandoned sweep is now re-armed rather than permanent and cannot
be read off an empty board. A better diagnostic on somebody else's helper is still somebody else's
helper, which is why this paragraph exists rather than a coverage row: `test_spec_coverage.py`
resolves only `backend/tests/**/test_*.py` and `e2e/specs/*.spec.js`, so `e2e/helpers.js` cannot be
registered, and no static guard reads it. The next Rank milestone owns both files and may lift the
argument into its own. [M4.10 findings 6 and 9; M4.11 finding 17; decision 209]

**M4.10 shipped before it, and it has a §12 row (spec line 415) on M4.9's argument rather than
M4.6's and M4.7's: two rows this table already had, closed against writes that had never been made
twice at once.** §12's M2 row owns the rating view, the Personal Ledger and
§6.1's prediction reveal; its M3 row owns Rank's tiers and its comparison queue. Both were asserted
one request at a time, and every Ledger write on either surface was a read, then some work, then a
write on a connection that autocommits each statement. Measured before the repair: two gathered
answers under one sealed pair wrote two `duel` rows in most races with no injected latency, one of
those races drawing §13's held-out arm; two gathered Rate taps on one card token left the loser
holding a refusal §6.1 does not define, with its Jellyfin write already sent — the plan says 500
there, and M4.7's handler had since made it a 409 naming a database constraint, which is a different
sentence and no more actionable by the client; a 1.5 s Played push held one backend
`idle in transaction` for the whole wait, because the round trip was awaited inside the verdict
transaction; both Rank routes raised after their observation was durable, so every retry wrote
another append-only row; and §6.1's reveal was dark on 50 of 50 taps for both members on the real
bundle. What closed it is one property at four seams, each in the form that seam can carry: the
two-int `pg_advisory_xact_lock` of `tonight/play.py:611`, taken as the first statement inside the
Rank answer's `write_txn(conn)` — not through that helper's own `lock=`, whose single-argument
`hashtext(...)::bigint` form is a different lock space and cannot collide with a number there
(`api/deps.py:86-88` says so); a `SELECT ... FOR UPDATE` on `rate_session` as the first statement of
every Rate write; an `AND card_token IS NULL` on the card stash — joined by an
`AND card_token = $read` one for the §6.0 banner's redraw, which must replace the card it read and
nothing else — where the decision and the write are one statement; and a compare-and-set on the tier set a
refit fitted against, with the request's own timestamp deciding whether clearing it is safe. In
every case the loser either waits or matches no row, and then reads what the winner committed — with
the refit moved after the commit and the Jellyfin round trip moved out of it. **Ten rows were
written before the code and `current_milestone` was raised in the same change, with no `tests` key
on any of them**: the red list that run printed — those ten ids — *is* the test plan
(`docs/milestones/M4.10-plan.md`), and each of the ten stages filled in its own row as its tests
landed. That differs from M4.9's opening, which named thirty-nine test ids in
advance; here the rows were owned by stages running in parallel, and a guessed test name in another
stage's row is a merge conflict rather than a plan. **No waiver was added and the milestone was never
lowered.** Decisions **199–209** are numbered in `docs/spec-v2.2-proposals.md` — the last of them
taken as the milestone closed, on the window finding 9 opened between a member's first verdict and
the sweep that fits it: §6.3's board answered "0 rated" through it, which is what a member who has
never opened Rate reads, so it says `fitting` instead and no route waits for a fit. **No migration** —
every fix uses a column that already exists, so `0019` stays free.

**All eight of M4.10's exit-criterion clauses are asserted in this suite; the browser gate on top of
them is the owner's.** Every gathered assertion repeats eight times rather than once, because the
reproductions behind the two double-write findings failed in four and in five of six attempts — a
race that passes once has not passed. One clause carries a condition worth knowing before it is
measured: since a Ledger cache miss stopped running the MAP fit inside the request, a brand-new
member's first fit arrives with the 60 s `tier-set-refit` sweep, so "the reveal is available from the
second tap" has to be measured with that sweep running, or it measures the absence of a worker rather
than the presence of a reveal. `npm --prefix e2e run fresh` is the owner's, against no inherited red,
so any browser failure is M4.10's; two of its cases moved with the decisions rather than with the
code, and would have failed by design if they had not — `11-rate.spec.js` asserted the fifteenth tap
was already unretractable, which decision 199 reverses, and `13-rank.spec.js` gained the
tap-into-an-occupied-tier case that proves the phone's tap writes no neighbour duel. **No whole-suite
count is published here** until a run prints one, which is decision 184's rule applied again.

**One measurement M4.10 took and deliberately did not act on.** Decision 205 keeps decision 175's
`straddle_z` retune with M4.12, so the straddle badge was measured rather than moved: at
`straddle_z = 1.0` a settled board badges 57% of its titles, and a board nobody has rated badges
**600 of 600** — `b_i_tau` is exactly `straddle_z`, so the prior interval always crosses a cut. What
M4.10 changed is only which tier the badge names (the adjacent one, and the nearer cut when the
posterior reaches both); on a mature board the old and new rules disagree on 2 badges in 1,083, and
on a young one on 1,065 of 1,723. The number that argues for the retune is the 600, and it belongs to
M4.12's findings 31 and 32.

**M4.9 shipped before it, and unlike M4.6, M4.7 and M4.8 it has a §12 row of its own.** The
argument is at spec line 427: M0's exit criterion is "bundle imports clean; Library list and title
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
M4.6 and M5, with the argument at spec line 425: §12 scheduled the product and left the box it runs
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

M4 inherited 18 rows and closed 42; the block above says 41, because M4.12 deletes
`tonight-rank-tv-kiosk-route` with the surface it described (decision 165 — nothing about a
session renders anywhere but a phone). A requirement withdrawn is a deletion and never a waiver,
which is why the count moves rather than the waiver line. Twenty-four were added before any code, from reading
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
