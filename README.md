# Spielplan

A household media graph: your Jellyfin library, a taste model that learns from three-class
verdicts and comparisons, and a Tonight session that resolves what to watch without an argument.

Standalone by design — backend, database, front end, no cloud, CPU only. Home Assistant is a
later, additive integration and nothing in the core flows depends on it.

**The spec is the authority.** [`docs/spielplan-spec_v2.1.md`](docs/spielplan-spec_v2.1.md) is
normative; where this code and the spec disagree, the spec wins and the code is a bug. Every
non-obvious decision in the source cites the section that mandates it.

- [`docs/spielplan-spec_v2.1.md`](docs/spielplan-spec_v2.1.md) — current spec
- [`docs/spec-v2.2-proposals.md`](docs/spec-v2.2-proposals.md) — proposed amendments from the
  UI-prototype review. Proposals, not spec; 161 of them. All seven owner decisions were taken on
  2026-08-29 and are recorded at the end, along with **§6.2 — Tonight, rewritten**, which
  replaces the fixed ten-vote round with an adaptive one
- [`docs/media-graph-spec_v1.1.md`](docs/media-graph-spec_v1.1.md) — superseded, vendored because
  v2.1 cites its surviving interaction designs by section

## Shape

```
docker compose
  db        postgres 16
  backend   fastapi + uvicorn — REST, auth, scoring, serves the built PWA
  worker    same codebase, queue consumer — sync, acquisition, extraction, nightly refits
```

Spec §1 draws a fourth `frontend` service; the PWA is a *static* build and §1 explicitly permits
it to be "served by backend", so it is compiled in a node stage of `ops/backend.Dockerfile` and
served by the backend. One fewer process on a 4-vCPU box, nothing lost.

**The backend runs as exactly one process.** Do not add `--workers 2`, `WEB_CONCURRENCY`, or a
gunicorn wrapper, and do not run two replicas of the service: Tonight's lobby, §6.7's transparency
rail and the push in-flight set are in-process state, so a second worker splits one household into
two lobbies and two rails — half the phones in a session the other half cannot see. The app
refuses to start if it detects either setting, and a test pins the shipped `CMD`. A busy evening
is answered with more CPU on the box, not more app processes. The `worker` service is a separate
process on purpose and is not affected; its own known consequence is that a nightly refit narrates
itself to nobody, because the rail buffer it would write to lives in the backend
(`backend/spielplan/home/rail.py`).

The app itself speaks plain HTTP on one internal port. TLS is the operator's existing
Traefik + Cloudflare. `PUBLIC_URL` is required config, not decoration: WebAuthn binds passkeys to
that origin, and changing it later invalidates every registered credential.

## Running it

```bash
cp .env.example .env    # fill in PUBLIC_URL, SESSION_SECRET, SECRETS_KEY
mkdir -p data/raw data/artifacts data/cache data/import data/backups
sudo chown -R 1000:1000 data/raw data/artifacts data/cache data/import data/backups
docker compose up
```

The `chown` is not decoration: the app containers run as uid 1000 rather than root (§14.3), and
those five directories are host bind mounts they write. `data/pg` is deliberately absent — it
belongs to the `db` service's own user. [What lives under `data/`](#what-lives-under-data) says
what each one holds.

It has one consequence worth knowing before the wizard's third step rather than during it: the
bundle is imported from `data/import` and there is no upload route, so it has to be on that host
directory first — and the directory now belongs to uid 1000, which the operator is not. Creating
an entry in it therefore takes a `sudo`, and the file has to be readable by the uid that opens it:

```bash
sudo install -o 1000 -g 1000 -m 644 spielplan-bundle.tar.zst data/import/
```

Then open `PUBLIC_URL` and walk the first-boot wizard: create admin → connectors → import the
bundle. It ends there (decision 164): everyone else is added from **Admin > Users**, and each
member's phone is walked through PWA install and push on its own first run.

**A bundle-less app is a legal state** (§3.1). Boot with no artifact bundle and the app runs:
the wizard and admin routes work, and every artifact-dependent surface renders an explicit
"no bundle imported" state instead of erroring. The bundle step is skippable.

Back up `.env` alongside the nightly `pg_dump`s. Dumps contain ciphertext only — a restored dump
cannot decrypt connector config without `SECRETS_KEY`.

## Recovery

The three things that go wrong on a household box — a restore, an upgrade, and a backup that was
never happening — and the movie data that has no second copy anywhere. Every command below can be
checked against this repository: the service names, the mounts and the paths are
`docker-compose.yml`'s, and `spielplan-secrets` and `spielplan-movie-data` are two of the three
console scripts `backend/pyproject.toml` declares and the image installs.

### Restore a dump

```bash
docker compose stop backend worker
docker compose exec db pg_restore --clean --if-exists --no-owner \
    -U spielplan -d spielplan /backups/spielplan-20260906T060000Z.dump
docker compose start backend worker
```

`-U` and `-d` are `POSTGRES_USER` and `POSTGRES_DB` from `.env`. `/backups` inside the `db`
container is `./data/backups` on the host, so there is nothing to copy first.

`--clean --if-exists` is the load-bearing part, and stopping the app first is what makes it safe.
A restore happens on a box that is already running, and `docker compose up` completes §3.1's first
boot before anyone can restore anything — so the target holds rows the dump also holds. Restored
without those flags it exits 1 with several hundred ignored errors and the result *looks* healthy:
`title` and `app_user` come back and logins work, while `connector_config` ends with zero rows (its
COPY fails on the foreign key to `data_encryption_key`) and `app_setting` keeps the fresh install's
push keypair (its COPY fails on the primary key), so every restored subscription is bound to a key
the server no longer holds. `--no-owner` covers a dump taken on a box with a different role name.
The same three commands restore onto a new box whose database nothing has ever touched; that case
simply never needed the two flags that make this one safe.

**And a dump restores only into the image that wrote it.** `--clean` drops what the *archive*
holds, so a table a later release added is not in the archive, is not dropped, and is still there
when the app comes back up and the migration runner reaches the file that creates it. Restoring a
dump taken before `0017_ops.sql` into a stack running it leaves the backend crash-looping on
`asyncpg.exceptions.DuplicateTableError: relation "job_run" already exists` — a table name, with
nothing in it to connect the failure to the dump. The other direction refuses in words: a dump from
a *newer* release brings `schema_migration` rows this build has no file for, and the runner stops
with "schema_migration records 1 migration(s) with no file in …".

An older dump therefore needs a database with nothing newer in it, and the way to be sure of that
is to throw the old one away — which a restore is doing anyway. Drop and recreate it while the app
is stopped, restore into that, and let the current image apply the missing migrations on its way up:

```bash
docker compose stop backend worker
docker compose exec db psql -U spielplan -d postgres -c 'DROP DATABASE spielplan WITH (FORCE);'
docker compose exec db psql -U spielplan -d postgres -c 'CREATE DATABASE spielplan;'
docker compose exec db pg_restore --no-owner -U spielplan -d spielplan /backups/<dump>
docker compose start backend worker
docker compose ps backend
docker compose logs --since 2m backend | grep 'applied migrations'
```

The last two lines are the confirmation, and they are two because either one alone can be read
wrong. `docker compose ps` says whether the container is `Up` or `Restarting`: `docker compose
start` exits 0 for a backend that then crash-loops on the restored schema, which is exactly what
skipping the DROP above produces. And `--since` bounds the grep to this boot — `start` reuses the
existing container, whose log still holds every earlier boot, so an unbounded grep will happily
hand back the line from the `up -d --build` that preceded the restore and call it the answer.

What it should print is one line naming every migration written since that dump, or `applied
migrations: (none pending)` when the dump came from this build. The backend logs one of the two on
every boot, so no line at all is never the good news: it means the boot did not reach the migration
runner.

Going back to the older release instead does **not** work, and it is worth knowing why before it is
tried under
pressure: that image refuses to start against a database carrying a migration it has no file for
(the orphan refusal above), and restoring first does not help either — the newer release's tables
are still standing, and the second upgrade dies on them exactly as it did the first time.

Two things a dump does not contain:

- **`.env`.** It has to be the file that was current when the dump was taken: `SECRETS_KEY` wraps
  the data-encryption key, and the dump carries only ciphertext (§2). A restore under a different
  key is survivable rather than fatal, deliberately — the app boots, every member's seen-state,
  verdict, not-seen and finish-prompt write still returns 200 with a reason naming `SECRETS_KEY`,
  and **Admin > System** and **Admin > Connectors** both report the custody failure. The way out is
  to give up the unreadable ciphertext and re-enter the credential:

  ```bash
  docker compose up -d          # only if you just edited .env — see below
  docker compose exec backend spielplan-secrets reset
  ```

  `exec` runs with the environment the container was *created* with, so a `SECRETS_KEY` edited in
  `.env` reaches it only after compose recreates the container — and `reset` refuses outright
  unless the key is set, because without one to try it cannot tell an unreadable row from a
  readable one. It then retires the key rows it cannot open, clears the ciphertexts that named
  them, and prints each cleared row by name. `app_setting/push.vapid` in that list means the web-push
  keypair was removed rather than emptied, so the next boot mints a fresh one: run
  `docker compose restart backend worker`, after which push works again and every phone must
  subscribe once more. Re-enter the Jellyfin API key on **Admin > Connectors**. Rotating a key you
  still *hold* is the other subcommand and loses nothing; `.env.example` has it.

- **`/data/artifacts`.** The bundle is ~1 GB of files and §2's dump is the database only. The
  database still names a version as active, so a box that lost the directory boots with
  `artifact_bundle <version> is active but /data/artifacts/<version> does not exist` in
  `docker compose logs backend` and serves every artifact-dependent surface in its no-bundle state
  (§3.1). Re-import that same bundle version from **Admin > Data**. (§2 calls the bundle and the
  raw store "already immutable files" as the reason for dumping Postgres alone. The staged bundle
  is neither immutable nor backed up — a re-import of the same version overwrites it — so treat
  that clause as "re-importable", which is what this paragraph is.)

### Upgrade

```bash
# once, on an install that predates M4.7 — the paragraph after next says why
sudo chown -R 1000:1000 data/raw data/artifacts data/cache data/import data/backups

git pull
docker compose up -d --build
docker compose ps
docker compose logs --since 2m backend | grep 'applied migrations'
```

`--build` is not optional. Both app services declare `build:` with no `image:` tag, so a plain
`docker compose up` after a `git pull` starts the image already on the box: new code absent, new
migrations unapplied, and nothing said about either.

The `chown` is the one step an upgrade needs that a fresh install gets by following "Running it".
Both app processes ran as root until M4.7, so it made no difference whether the five bind mounts
were owned by root — Docker's, when it creates a missing mount source — or by whoever ran the
`mkdir`. Neither is uid 1000, which is what the image runs as from this release on (§14.3), and an
upgrade changes the image and not the directories. Every write into them then fails, and the HTTP
surface says nothing: the backend still answers `/api/health`, so `docker compose ps backend`
reports a healthy container over a worker that cannot write the nightly dump into `/data/backups`,
an importer that cannot stage a bundle under `/data/artifacts`, and a
`/data/cache/worker.heartbeat` that is never created. That last one is why the confirmation above
is `docker compose ps` and not `docker compose ps backend`: the heartbeat's age is what the
worker's healthcheck reads, so a worker with no heartbeat file reports `unhealthy` for ever rather
than for a night, and the one WARNING it logs about the failed write is written once per outage
rather than once per tick.

The grep is the confirmation, and the backend writes one line either way: the versions it applied,
or `applied migrations: (none pending)`. So a *missing* line is not "no migration ran" — it is a
boot that never reached the runner, which is what a config refusal, a mistyped service name and a
container still starting all look like from here, and `docker compose ps` is what tells those from
a healthy one. After a pull that added a file to `backend/migrations/`, the line names it.
`--since` is not decoration either: compose leaves a container it did not have to recreate exactly
where it was, and the log it kept still carries the previous boot's line.

That line only became visible at M4.7: under uvicorn's own logging configuration the root logger
sits at WARNING with no handlers, and every INFO record this app wrote went nowhere at all.

Two refusals belong to *this* upgrade in particular, and both present as that missing line with a
crash-looping backend under it. Neither is what `spielplan-secrets reset` — the custody command
the Restore block above names — is for: the first kills that command with the same error it kills
the app with, and on the second it reports that custody is intact and exits 0, correctly.

- **`SECRETS_KEY must be at least 32 characters`.** Nothing before M4.7 checked the length, so a
  hand-typed short value ran perfectly well for as long as it existed; it is refused at
  construction now, because it wraps a Jellyfin key §14.3 calls admin-equivalent. The way out is a
  rotation, and it is the one case that runs in the opposite order from the usual one — the new
  value goes into `.env` *before* the command, because `spielplan-secrets` builds the same
  `Settings` this refusal comes from before it opens a connection, and would otherwise die on the
  value it is being run to replace. `.env.example`'s `SECRETS_KEY` block carries that recipe.

- **`could not create unique index "data_encryption_key_one_active"`.** `0017_ops.sql` makes §2's
  "the one DEK row" a fact of the schema, and an install that lost the first-boot race before that
  index existed carries two un-retired rows, so the statement cannot build and takes its migration
  and the boot with it. `spielplan-secrets reset` answers "Custody is intact" here and is right
  to: both rows open under this `SECRETS_KEY`, and it destroys only what it cannot open. The
  repair is to retire one, which loses nothing — `load_dek` finds a retired row by id, so every
  ciphertext naming it still opens, and the only thing the choice decides is where *new* seals go.
  The app already takes the newest un-retired row, so retiring the older one keeps the arrangement
  the install was running before it stopped:

  ```bash
  docker compose exec db psql -U spielplan -d spielplan -c \
      'SELECT key_id, created_at FROM data_encryption_key WHERE retired_at IS NULL ORDER BY created_at;'
  docker compose exec db psql -U spielplan -d spielplan -c \
      "UPDATE data_encryption_key SET retired_at = now() WHERE key_id = '<the older key_id>';"
  ```

  `exec db` and not `exec backend`: the backend is the container that is restarting, and
  `docker compose exec` needs one that is up. Both app services carry `restart: unless-stopped`,
  so the next attempt after the `UPDATE` is the one that applies the migration; the two
  confirmation lines above are how to read it. If those rows are *also* unreadable — a restored
  dump whose `.env` was not restored — then `reset` is the repair after all, because it retires
  what it cannot open and that frees the index too. Reach it with `run --rm`, for the same reason
  as `exec db`: a fresh container rather than the restarting one.

  ```bash
  docker compose run --rm backend spielplan-secrets reset
  ```

§10's swap sequence ends in a restart the importer does not perform. After any bundle import:

```bash
docker compose restart backend worker
```

The Data tab's banner names that command until it happens.

### Verify a backup

```bash
ls -l data/backups
docker compose exec db pg_restore --list /backups/spielplan-20260906T060000Z.dump | head
```

The worker writes one dump a night at 06:00 household time (`TZ`) and keeps fourteen (§2). Names
are `spielplan-<UTC timestamp>.dump`: UTC because the name is the whole record and rotation orders
by it, so the newest name is the newest dump whatever the host clock has been through. Three things
to know when reading that listing:

- A file ending `.partial` is **not** a backup — it is a `pg_dump` that was interrupted, kept out
  of the fourteen by that suffix and deleted by the next successful run.
- A file of the right name can still be a truncated one. `pg_restore --list` prints the archive's
  table of contents and fails on a dump that is not readable, which is the cheapest real check.
- **Admin > System** is the same facts without a shell: the last successful dump with its size and
  age, a warning when nothing has succeeded for 36 hours, and the newest run of every job with its
  outcome. It reads `job_run`, a row the worker opens before a job runs and closes when it ends.

### The movie data

Decision 162 makes this install the only copy of its movie data: the corpus seeds it once, every
later title is acquired here, and Spielplan owns the ids. `spielplan.backup.movie_data` writes a
restorable archive of the content spine, the naming layer and the review bodies, with no user state
in it:

```bash
docker compose exec worker spielplan-movie-data write /data/backups/movie-data.zip
```

The worker and not the backend: `/data/backups` is mounted on the worker alone, because the process
that serves §6's anonymous SPA fallback has no reason to hold every night's dump (§14.3).

The restore refuses an install that already holds movie data, and it locks every archived table for
its duration, so it is a stop-the-stack event:

```bash
docker compose stop backend worker
docker compose run --rm worker spielplan-movie-data restore /data/backups/movie-data.zip
docker compose start backend worker
```

`run --rm` rather than `exec`, because `exec` needs a running container and the worker is the
process being kept out of the way.

### What lives under `data/`

- `data/pg` — Postgres's own directory. Nothing else writes it.
- `data/backups` — the nightly dumps, worker only.
- `data/artifacts/<version>` — the staged bundle every scoring surface reads. Not in any dump.
- `data/import` — where a bundle goes to be imported. Validating an *archive* extracts it to
  `data/import/.unpacked-<name>/`, a full second copy including `content.sqlite` and
  `reviews.sqlite` — 790 MB of a 1042 MB bundle. A committed import deletes that tree; a failed
  one keeps it, because the retry needs it, and a bundle validated but never imported keeps it
  too. That last one is yours to delete — with `sudo`, and so is putting the bundle there in the
  first place. The chown below hands this directory to uid 1000, and creating or removing an entry
  in a directory needs write on the directory: the operator can still list it and read what is in
  it, and can do neither of the two things this bullet is about without borrowing root.

  ```bash
  sudo install -o 1000 -g 1000 -m 644 spielplan-bundle.tar.zst data/import/
  sudo rm -rf data/import/.unpacked-spielplan-bundle.tar
  ```

- `data/cache` — the model cache, and `worker.heartbeat`, whose age is what the worker's
  healthcheck reads. `docker compose ps` reports the worker unhealthy when the loop stops going
  round; nothing restarts it on that, by design.

(A sixth, `data/raw`, is mounted on the worker and nothing writes it yet — §2's raw store has no
producer before M5.)

Every one of them is a bind mount, so the host owns it and the container writes as the uid it runs
as — **1000**, the unprivileged `spielplan` account `ops/backend.Dockerfile` creates (§14.3: the
process serving §6's anonymous SPA fallback should not be root). Docker creates a missing mount
source owned by root, and a directory you made yourself is owned by you, so on a first
`docker compose up` either way round the container cannot write it:

```bash
mkdir -p data/raw data/artifacts data/cache data/import data/backups
sudo chown -R 1000:1000 data/raw data/artifacts data/cache data/import data/backups
```

Not `data/pg` — that one belongs to the `db` service's own user, which the `postgres:16` image
sets up itself. Skip this and the symptom is a worker that cannot write a dump and an importer that
cannot stage a bundle, both with a permission error in `docker compose logs`.

## Developing

```bash
# the database on its own, with its port published for host-run code
docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db

# backend
cd backend && uv venv .venv && uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest          # POSIX: .venv/bin/python

# front end, proxying /api to a backend on :8080
npm --prefix frontend run dev
```

A backend started by hand reads `.env` from its own working directory
(`Settings(env_file=".env")`), and §2's config is required rather than defaulted: an empty
`PUBLIC_URL`, one carrying no scheme, a `SESSION_SECRET` under 32 characters, or a `SECRETS_KEY`
that is *present* and under 32 refuses the boot by name — absent stays legal, because §3.1 makes a
half-configured boot one, and what a short value means is that somebody typed it.
Set `SPIELPLAN_INSECURE_DEV=1` to turn those refusals off for a host process nobody else
can reach — it says so at WARNING on every boot, and `docker-compose.yml` deliberately does not
forward it, so it cannot be switched on in the shipped stack by editing `.env`. `ops/devstub.py`
sets it for itself; the test suite sets its own env.

### Tests

Six layers, from pure logic to a browser driving the shipped stack:

```bash
python -m pytest backend/tests -q        # logic, static guards, schema (needs nothing)
npm --prefix frontend test               # client helpers
node e2e/run.mjs                         # the whole stack, desktop and phone
```

The integration layer needs a real Postgres and skips without one; the e2e layer needs the
compose stack. `backend/tests/spec_coverage.toml` is the contract that says which requirement
each milestone owes a test, and `test_spec_coverage.py` fails the build when a shipped
milestone has an uncovered one.

**[`docs/TESTING.md`](docs/TESTING.md)** is the whole picture, including the mechanical routine
for opening a milestone: raise `current_milestone`, run the suite, and the failure *is* the
test plan.

### Without Docker at all

```bash
python ops/devstub.py &                 # fixture-backed API on :8080
npm --prefix frontend run dev
```

`ops/devstub.py` is a harness, not the app: it answers the same paths from the test fixture
bundle, in memory. It calls the *real* validator, so the import report you see is the real one,
and a contract test keeps its route set aligned. If a shape there ever disagrees with
`backend/spielplan/api/`, the real app is right.

## Where the model comes from

Nothing here trains a collaborative model. The 64-d item space, the content encoder, the DNA
vocabulary and the tuned hyperparameters are built and measured in the corpus project and arrive
as a versioned artifact bundle (§10). This app imports it, validates it against every schema
landmine, and fits per-user state on CPU.

Re-importing a bundle is a planned admin event with a diff report, never a silent sync:
everything expressed in the old model's basis is garbage against a new one, so a re-import
recomputes user vectors, blend weights, a full ledger refit, and re-places every locally
acquired title.

## Build order

`M0` compose, schema, wizard, auth, bundle importer, Library · `M1` Jellyfin + seen-state sync +
passkeys · `M2` Rate + the Personal Ledger + Home shelves — **the gate**, and the first real test
of whether any of the corpus measurements transfer to two actual people · `M3` Rank · `M4`
Tonight · `M4.6` user management (§6.6 Users) · `M5` acquisition + LLM layer · `M6` Map + Taste ·
`M7` HA hooks.

Full table with exit criteria: spec §12.

### Status

**M0** is in place and verified end to end against Postgres 16 in Docker: `docker compose up`
brings up db + backend + worker, the first-boot wizard creates the admin, the importer runs the
§10 swap sequence as far as code can take it (validate → stage → load → transactional flip), and
the Library and title detail card render the imported titles. Both §12 exit criteria are met. The
restart §10 ends with is the operator's — `docker compose restart backend worker`, above under
Recovery — and the Data tab says so until it happens.

**M1** is in place: the Jellyfin connector (≥ 10.9 routes, the corpus field set), optional
one-to-one user linking with per-user access tokens, two-way seen-state sync, the ≥ 90%
playback watcher and its in-app finish prompt, and WebAuthn passkeys. §12's exit criterion —
"seen states flow both ways" — is asserted in a browser against a Jellyfin that answers
(`ops/fake_jellyfin.py`) and that **refuses the admin API key on the Played write**, so §7.3's
per-user-token rule is something a test can break rather than a comment. Passkeys are exercised
as real ceremonies: a software authenticator in the backend tests, Chromium's virtual
authenticator in the browser.

**M2** is in place, and it is the gate: §5.2's Personal Ledger (all four arms in one
likelihood, refit nightly and incrementally), §5.1's scoring stack, §5.3's placement
reconciliation, §6.1's Rate surface, §6.0's Home shelves, §6.7's transparency rail behind
decision 117's per-user toggle, and §12's member PWA-install/push onboarding. §12's second exit
criterion — "every owned title has a coordinate" — is one query, and the partial index exists
for it. The first — "50–100 verdicts each produce visibly personal rankings" — is a claim about
a real household with a real bundle, so what ships here is the machinery and the test that the
loop closes: a sitting of verdicts moves the ranking every shelf is built from, within the
sitting rather than overnight.

One caveat worth stating rather than discovering: the Ledger's numbers have never met a real
corpus Backbone — every measurement in this repo is against a synthetic fixture, which is
exactly the caveat §12 says M2 exists to settle. M2's second caveat has since been answered:
the push **sender** was M4's and shipped there, so §2's VAPID keypair is generated at first
boot and §7.3's prompt is carried as a real web push, with the in-app banner still the
guaranteed fallback.

**M4.5** is not in §12. It exists because the sentence that used to stand here — "the corpus
bundle does not exist in this repo" — was false, and the importer had been written accordingly:
against a schema nobody had opened, and verified against a fixture that reproduced every measured
landmine and invented every structure around them. A bundle built by `mdc export-bundle` on
2026-08-28 could not be imported at all, and all nine Cold Tower feature blocks missed every
column they declared, silently, because the fixture's contract named them the way the builder
keyed them.

The bundle is still not vendored — it is ~1.15 GB — but it is now the authority. The fixture
reproduces the landmines at eight-title scale **in the corpus's own schema**, and
`tests/fixtures/real_bundle_shapes.json` is a committed, data-free manifest of a real bundle's
shapes that the fixture is held to on every run; point `CORPUS_BUNDLE_DIR` at a real bundle and
the same manifest is checked against it, so a corpus-side format change fails this repo's suite
instead of surfacing as a mystery at import time. A column reaches the manifest as `p:<s>:<s>`,
never as anyone's name.

M4.8 added the two corpus shapes that eight titles were still missing, because a manifest of shapes
does not make the fixture *carry* them: the same credit filed under two department spellings (7,918
such triples across 1,216 real titles), and a `dna_tag.facet` that is the extraction label rather
than the term's own prefix (29,188 of 31,540 rows). Both were live defects and both are now
closed: the credit collision by `ee35d52`, which grouped `credits_for` by (person, job) and keyed
the card on the same pair, and the facet by M4.9 below. Neither was reachable *through the
importer* until the fixture carried it —
the credit shape existed only where `test_import_integration.py` inserts one by hand, which proves
the query and says nothing about the bundle it has to survive. It also added an opt-in
`make_bundle(dir, pool_titles=N)`, generated entirely from the authored vocabulary so the feature
contract does not widen by a single column: eight titles still beat eleven thousand for the traps,
and they cannot measure what a selector costs over the real bundle's 696 owned movies.

Two owner decisions (2026-09-01/02) changed what this project is to that one. **162:** the corpus
supplies trained models; movie data is seeded **once**; every later title is acquired by this app
(§7.2, §8); Spielplan owns all ids, minted from a range disjoint from the corpus's, because the
corpus's own `sqlite_sequence` reads 21442 and "mint above the imported maximum" would have
started this app at exactly the id the corpus mints next. **163:** a DNA vocabulary change is a
data migration, not an import, and is refused until that migration exists.

**M4.9** has a §12 row of its own, and it is a row this table already had — M0's "bundle imports
clean; Library list and title card render imported titles" was closed against the fixture, and the
fixture does not carry the shapes the export actually ships. M4.9 is the repair, end to end: one
facet vocabulary from the loader through migration `0018_read_layer.sql`'s backfill, the SQL
predicate, the chip and §6.8's palette; a title card that renders past its credits on the 1,216
titles whose keyed each used to throw, and says how many of them it is hiding; a catalogue whose
pagination is a total order and whose search treats `%` and `_` as text; two authenticated routes
that partition by kind; a Home whose shelf-1 anchor is a title its owner actually rated and whose
"no crowd data yet" badge reads `e_source` rather than the placement stamp; §6.7's rail mounted
once in the shell so it opens from every surface; and `title_company`, `title_video`,
`rating_source` and the ML link no longer dropped or misreported by the loader. Owner decisions
**187–193** record the seven calls it needed — including that a projected DNA term is bounded by a
saturating weight rather than clamped (188), that posters do not ship here (190), and that
`title_company` loads keyed per source (193).

Its exit criterion is `ops/m49_exit_criterion.py`: twelve measures against the real bundle, which
it **refuses to run without** — every one of them is zero on eight fixture titles. It has been
written and not yet run; `docs/TESTING.md` carries the command and says why no count is published
here until a real run prints one.

### Before the release: M4.6 – M4.16

A full pre-release review of `m45` (2026-09-03/04) produced 439 findings, of which 389 are grouped
into eleven milestones that all land before M5, and 50 are deferred with reasons.
**[`docs/milestones/ROADMAP-to-M5.md`](docs/milestones/ROADMAP-to-M5.md) is the entry point** — it
carries the milestone table, the pre-allocated migration ledger, the twelve decisions taken on
2026-09-04 (**167–178**), the deferred buckets and the sequencing. Each milestone has its own plan
beside it, written to be handed to one implementation agent.

Three further owner decisions (2026-09-03) changed scope and are already amended into the spec.
**164:** accounts are created and managed in §6.6 Users, not the first-boot wizard. **165:** nothing
about a Tonight session renders on the TV — the phone is the only surface, results included; the
`/tv` route is deleted rather than deferred. **166:** a guest is a Tonight session seat with no
account and no profile, the account table keeps **two roles**, at least one active admin always
exists, and the app gains real user management including password reset (that last part is
**M4.6**, a milestone §12 did not have).
