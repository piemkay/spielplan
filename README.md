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

The app itself speaks plain HTTP on one internal port. TLS is the operator's existing
Traefik + Cloudflare. `PUBLIC_URL` is required config, not decoration: WebAuthn binds passkeys to
that origin, and changing it later invalidates every registered credential.

## Running it

```bash
cp .env.example .env    # fill in PUBLIC_URL, SESSION_SECRET, SECRETS_KEY
docker compose up
```

Then open `PUBLIC_URL` and walk the first-boot wizard: create admin → connectors → import the
bundle → member accounts → onboard the phones.

**A bundle-less app is a legal state** (§3.1). Boot with no artifact bundle and the app runs:
the wizard and admin routes work, and every artifact-dependent surface renders an explicit
"no bundle imported" state instead of erroring. The bundle step is skippable.

Back up `.env` alongside the nightly `pg_dump`s. Dumps contain ciphertext only — a restored dump
cannot decrypt connector config without `SECRETS_KEY`.

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
Tonight · `M5` acquisition + LLM layer · `M6` Map + Taste · `M7` guest profiles, HA hooks.

Full table with exit criteria: spec §12.

### Status

**M0** is in place and verified end to end against Postgres 16 in Docker: `docker compose up`
brings up db + backend + worker, the first-boot wizard creates the admin, the importer runs the
full §10 swap sequence (validate → stage → load → transactional flip → restart), and the Library
and title detail card render the imported titles. Both §12 exit criteria are met.

**M1** is in place: the Jellyfin connector (≥ 10.9 routes, the corpus field set), optional
one-to-one user linking with per-user access tokens, two-way seen-state sync, the ≥ 90%
playback watcher and its in-app finish prompt, and WebAuthn passkeys. §12's exit criterion —
"seen states flow both ways" — is asserted in a browser against a Jellyfin that answers
(`ops/fake_jellyfin.py`) and that **refuses the admin API key on the Played write**, so §7.3's
per-user-token rule is something a test can break rather than a comment. Passkeys are exercised
as real ceremonies: a software authenticator in the backend tests, Chromium's virtual
authenticator in the browser.

The corpus bundle does not exist in this repo, so the importer is verified against a synthetic
fixture reproducing the landmines — both DNA tiers with overlapping pairs, the frozen
`rating_source` ids, duplicate `tmdb_id`, NULL alias PK components, CJK/emoji/ZWSP — plus six
deliberately broken bundles, one per rule. `mdc export-bundle` is still a corpus-project
deliverable (§10).
