# Spielplan

A household media graph: your Jellyfin library, a taste model that learns from three-class
verdicts and comparisons, and a Tonight session that resolves what to watch without an argument.
Standalone by design — backend, database, front end, no cloud, CPU only.

**The spec is the authority.** [`docs/spielplan-spec_v2.1.md`](docs/spielplan-spec_v2.1.md) is
normative; where this code and the spec disagree, the spec wins and the code is a bug.
[`docs/spec-v2.2-proposals.md`](docs/spec-v2.2-proposals.md) is the decision record (entries 162
onward are numbered owner decisions). [`docs/TESTING.md`](docs/TESTING.md) says how to run the
tests.

## Shape

```
docker compose
  db        postgres 16
  backend   fastapi + uvicorn — REST, auth, scoring, serves the built PWA
  worker    same codebase, queue consumer — sync, acquisition, extraction, nightly refits
```

**The backend runs as exactly one process.** Do not add `--workers 2`, `WEB_CONCURRENCY`, a
gunicorn wrapper or a second replica: Tonight's lobby, the transparency rail and the push in-flight
set are in-process state. The app refuses to start if it detects either setting. The app speaks
plain HTTP on one port; TLS is the operator's reverse proxy.

## Running it

```bash
install -m 600 .env.example .env    # fill in PUBLIC_URL, SESSION_SECRET, SECRETS_KEY, POSTGRES_PASSWORD
mkdir -p data/raw data/artifacts data/cache data/import data/backups
sudo chown -R 1000:1000 data/raw data/artifacts data/cache data/import data/backups
docker compose up -d --build
docker compose logs -f backend    # Ctrl+C here leaves the stack running
```

`POSTGRES_PASSWORD` is read only when `data/pg` is first created, and so are the data checksums
compose asks for; set it before the first `up`.

The app containers run as uid 1000 (§14.3) and those five directories are host bind mounts they
write. `data/pg` belongs to the `db` service's own user and is left alone.

The bundle comes from the corpus project and carries the trained models (§10); nothing here trains
one. Content seeds once (decision 162), later imports bring models and a migration report, and a
bundle with a different DNA vocabulary version is refused (decision 163). It is imported from
`data/import`; there is no upload route, and that directory belongs to uid 1000, so:

```bash
sudo install -o 1000 -g 1000 -m 644 spielplan-bundle.tar.zst data/import/
```

An unpacked bundle directory works as well. With no `BUNDLE.json` in `/data/import` and exactly one
`.tar`/`.tar.zst`, that archive is opened; two archives is a refusal.

Then open `PUBLIC_URL` and walk the first-boot wizard. Until the admin account exists, anyone who
can reach `PUBLIC_URL` can create it, so do that step before the hostname is reachable from outside
your LAN or tailnet. Import the bundle **before** saving Jellyfin: once Jellyfin is saved, new
library items start becoming titles, and one made before the import stays a second copy of a film
the bundle brings. Leave the `JELLYFIN_*` seeds in `.env` empty on a first boot for the same reason.
Everyone else is added from **Admin > People**. A bundle-less app is a legal state (§3.1): every
artifact-dependent surface says "no movie data yet".

### Behind Traefik

`BIND_ADDR` stays `127.0.0.1`. A Traefik on the host (or with `network_mode: host`) proxies to
`http://127.0.0.1:8080`. A Traefik in its own container cannot reach the host's loopback: attach
`backend` to its network in an untracked `docker-compose.override.yml` (compose reads it by itself)
and point the router at `http://backend:8080`:

```yaml
services:
  backend:
    networks: [default, proxy]
    labels:
      - traefik.enable=true
      - traefik.http.routers.spielplan.rule=Host(`spielplan.example.tld`)
      - traefik.http.services.spielplan.loadbalancer.server.port=8080
      - traefik.docker.network=proxy
networks:
  proxy:
    external: true
```

Never set `BIND_ADDR=0.0.0.0` on a host with a public interface: Docker's published ports bypass
ufw and firewalld. A Content-Security-Policy added at the proxy must allow `script-src 'self'
'unsafe-inline'` (the page boots from one inline script whose contents change every build), and a
Permissions-Policy must not deny `publickey-credentials-get`/`-create`.

## Configuration

`.env` is read by `docker-compose.yml`; `.env.example` documents every variable.

| variable | |
|---|---|
| `PUBLIC_URL` | required. WebAuthn binds passkeys to this origin; changing it invalidates them all |
| `SESSION_SECRET` | required, at least 32 characters. Rotating it only ends sessions |
| `SECRETS_KEY` | required, at least 32 characters. Wraps the key that encrypts connector secrets |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | the database; `DATABASE_URL` is built from them |
| `BIND_ADDR`, `PORT` | where the backend is published (default `127.0.0.1:8080`) |
| `TZ` | household time zone; the nightly dump runs at 06:00 in it |
| `JELLYFIN_*`, `TMDB_*`, `OMDB_*`, `TRAKT_*`, `*_API_KEY` | optional connector seeds (§2) |

Connector seeds are written only for connectors that have no row yet; after that the database wins
and **Admin > Connectors** edits them. Rotating `SECRETS_KEY` is `spielplan-secrets rewrap`, and
`.env.example` gives the exact order. Back up `.env` alongside the dumps: they carry ciphertext only.

## Recovery

### Backup

The worker writes one `pg_dump` a night at 06:00 household time into `data/backups` and keeps
fourteen (§2). Names are `spielplan-<UTC timestamp>.dump`. Check them:

```bash
ls -l data/backups
docker compose exec db pg_restore --list /backups/spielplan-20260906T040012Z.dump | head
```

A file ending `.partial` is an interrupted dump, not a backup. **Admin > System** shows the last
successful dump and the newest run of every job, and warns when nothing has succeeded for 36 hours.

The dumps sit on the same disk as `data/pg`, so a dump that never leaves the machine is no backup,
and nothing scheduled copies anything off it. **Copying off the machine is yours**: `.env`,
`data/backups`, `data/raw` (paid LLM answers and fetched pages, in no dump; write-once, so rsync is
incremental) and `data/artifacts` (or the bundle archive). For example, after the 06:00 dump (host
cron runs in the host's zone, not `.env`'s `TZ`):

```bash
30 7 * * * rsync -a /srv/spielplan/.env /srv/spielplan/data/backups /srv/spielplan/data/raw /srv/spielplan/data/artifacts backuphost:/backup/spielplan/
```

Never copy `data/pg` while `db` runs; the dump is its backup.

This install is the only copy of its movie data (decision 162). Write a restorable archive of the
content spine, the naming layer and the review bodies, with no user state in it:

```bash
docker compose exec worker spielplan-movie-data write /data/backups/movie-data.zip
```

Restoring it refuses an install that already holds movie data and locks every archived table:

```bash
docker compose stop backend worker
docker compose run --rm worker spielplan-movie-data restore /data/backups/movie-data.zip
docker compose start backend worker
```

The archive carries the review bodies under their terms: it is this household's second copy, never
a hand-off to another one (see [Data terms](#data-terms)).

### Restore

```bash
docker compose stop backend worker
docker compose exec db pg_restore --clean --if-exists --no-owner \
    -U spielplan -d spielplan /backups/spielplan-20260906T040012Z.dump
docker compose start backend worker
```

`-U` and `-d` are `POSTGRES_USER` and `POSTGRES_DB`; `/backups` in the `db` container is
`./data/backups` on the host. `--clean --if-exists` and stopping the app first are load-bearing:
without them `connector_config` comes back empty and `app_setting` keeps the fresh install's push
keypair, while logins still work and the result looks healthy.

**And a dump restores only into the image that wrote it.** `--clean` drops what the archive holds,
so a table a later release added survives and the migration runner dies on it
(`DuplicateTableError: relation "job_run" already exists`). A dump from a newer release is refused
("schema_migration records 1 migration(s) with no file in …"). An older dump goes into a fresh
database, and the current image applies the missing migrations on its way up:

```bash
docker compose stop backend worker
docker compose exec db psql -U spielplan -d postgres -c 'DROP DATABASE spielplan WITH (FORCE);'
docker compose exec db psql -U spielplan -d postgres -c 'CREATE DATABASE spielplan;'
docker compose exec db pg_restore --no-owner -U spielplan -d spielplan /backups/<dump>
docker compose start backend worker
docker compose ps backend
docker compose logs --since 2m backend | grep 'applied migrations'
```

`ps` must say `Up`, not `Restarting`. The grep must print one line: the migrations applied, or
`applied migrations: (none pending)`. No line means the boot never reached the migration runner.
Going back to the older image instead does not work: it refuses a database carrying a migration it
has no file for.

**Restored under a different `SECRETS_KEY`**, the app boots, member writes still return 200 with a
reason naming the key, and **Admin > System** reports the custody failure. Give up the unreadable
ciphertext and re-enter the credentials:

```bash
docker compose up -d          # only if you just edited .env
docker compose exec backend spielplan-secrets reset
```

`reset` retires the key rows it cannot open and prints each ciphertext it cleared. If
`app_setting/push.vapid` is among them, run `docker compose restart backend worker`; every phone
must subscribe to push again. Re-enter the Jellyfin API key on **Admin > Connectors**.

**Lost `data/artifacts`**: the backend logs `artifact_bundle <version> is active but
/data/artifacts/<version> does not exist` and the model jobs refuse to run. Put the files back:

```bash
sudo cp -a /mnt/backup/artifacts/<version> data/artifacts/
sudo chown -R 1000:1000 data/artifacts/<version>
docker compose restart backend worker
```

…or re-import the same version from **Admin > Data**, which restages its files without loading any
content. `artifact_bundle` rows are provenance and never deleted; superseded versions' directories
under `data/artifacts` may be removed, the active one never.

### Upgrade

```bash
# once, on an install that predates M4.7 (the app then started running as uid 1000)
sudo chown -R 1000:1000 data/raw data/artifacts data/cache data/import data/backups

git pull
docker compose up -d --build
docker compose ps
docker compose logs --since 2m backend | grep 'applied migrations'
docker image prune -f
```

`--build` is not optional: both app services build locally, and a plain `up` starts the old image.
Each build leaves the previous image dangling; the prune takes it back.
`ps` must show both app services healthy; the grep reads as under Restore.

Two boot refusals an old install can meet, both a crash-looping backend with no migration line:

- **`SECRETS_KEY must be at least 32 characters`.** Rotate to a longer key with the new value in
  `.env` first; `.env.example`'s `SECRETS_KEY` block has the exact order.
- **`could not create unique index "data_encryption_key_one_active"`.** Two un-retired key rows from
  an old first-boot race. Retire the older one; nothing is lost, since a retired row still opens
  every ciphertext naming it:

  ```bash
  docker compose exec db psql -U spielplan -d spielplan -c \
      'SELECT key_id, created_at FROM data_encryption_key WHERE retired_at IS NULL ORDER BY created_at;'
  docker compose exec db psql -U spielplan -d spielplan -c \
      "UPDATE data_encryption_key SET retired_at = now() WHERE key_id = '<the older key_id>';"
  ```

  The backend restarts by itself and applies the migration. If those rows are also unreadable (a
  dump restored without its `.env`), run `reset` in a fresh container instead:

  ```bash
  docker compose run --rm backend spielplan-secrets reset
  ```

The backend loads an imported bundle by itself (decision 497). Restart only when the Data tab, the
wizard or the header says it could not load it:

```bash
docker compose restart backend worker
```

### What lives under `data/`

- `data/pg` — Postgres's own directory.
- `data/backups` — the nightly dumps and the movie-data archive. Mounted on `db` and the worker.
- `data/artifacts/<version>` — the staged bundle every scoring surface reads. Not in any dump.
- `data/import` — where a bundle goes to be imported. Validating an archive extracts it to
  `data/import/.unpacked-<filename>/`; a committed import deletes that tree, a failed or unrun one
  keeps it, and removing it is yours:

  ```bash
  sudo rm -rf data/import/.unpacked-spielplan-bundle.tar.zst
  ```

- `data/cache` — the model cache and `worker.heartbeat`, whose age the worker's healthcheck reads.
- `data/raw` — the acquisition raw store (§8). Worker only. In no dump: copy it with the dumps, since
  restored `raw_document` and `dna_pack` rows point at its files and nothing re-fetches a missing one.

All but `data/pg` are owned by uid 1000. Skip the `chown` and the worker cannot write a dump nor the
importer stage a bundle, each with a permission error in `docker compose logs`.

## Developing

```bash
# the database on its own, with its port published for host-run code
docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db

# backend
cd backend && uv venv .venv && uv pip install --python .venv -e ".[dev]"

# front end on :5173, proxying /api to the backend on 127.0.0.1:8080 (API_ORIGIN overrides)
npm --prefix frontend run dev
```

The front end needs a real backend on :8080: the compose stack, or uvicorn run by hand
(`python -m uvicorn spielplan.app:app --port 8080` from `backend/`, with `DATABASE_URL` pointing at
the published database). A hand-run backend reads `.env` from its working directory and refuses a
missing or short `PUBLIC_URL`/`SESSION_SECRET`/`SECRETS_KEY` by name; `SPIELPLAN_INSECURE_DEV=1`
lifts those refusals for a process nobody else can reach, and `docker-compose.yml` never forwards it.

## Data terms

The bundle is **private household data**, assembled under personal and non-commercial terms.
Private household use is permitted by every source in it; **publishing it, shipping it as a release
asset, or handing the `/data/backups` movie-data archive to another household is not** (§10,
decision 292). `LICENSE` is MIT over this repository's code and covers nothing in the bundle.

| what the bundle carries | the terms it travels under |
|---|---|
| MovieLens genome and link tables (GroupLens) | no redistribution without separate permission |
| scraped IMDb reviews and IMDb-derived tables | personal, non-commercial; no republishing into a database |
| Metacritic and Trakt review bodies | their `rating_source` rows read "not redistributed" |
| whole critic articles from four blogs | whole articles, not excerpts |
| MPST synopses | research dataset |
| OMDb plot and metadata text | CC BY-NC |
| TMDB overviews and poster URLs | non-commercial; "not endorsed, certified, or otherwise approved by TMDB" |
| Wikipedia plots and overviews, TVmaze `title_meta` rows | CC BY-SA, credit required |

The per-dataset terms travel with the data in `rating_source` (`url`, `license`, `version`,
`notes`), and the notices they require are on /account's **Data sources** block. `reviews.sqlite`
also carries Rotten Tomatoes bodies for which no record here states terms; that is a hole in the
record, not a permission. `/data/` is excluded by `.gitignore` and `.dockerignore`.
