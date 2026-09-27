# Testing

| layer | command | needs |
|---|---|---|
| backend logic and static guards | `python -m pytest backend/tests -q` | nothing |
| schema (PGlite) | same command | `backend/tests/pglite/node_modules` |
| integration | same command | Postgres 16 and `TEST_DATABASE_URL` |
| frontend units | `npm --prefix frontend test` | nothing |
| frontend types | `npm --prefix frontend run check` | nothing |
| e2e | `node e2e/run.mjs` | Docker (it brings the stack up itself) |

Run pytest with the path argument: its config (`asyncio_mode=auto`) lives in
`backend/pyproject.toml`, and a bare `pytest` from the repository root errors on async tests.

## Backend

```bash
python -m pytest backend/tests -q
```

With nothing else set up this runs the logic tests and the static guards. Two layers skip
silently:

- **Schema.** The migrations applied to a real Postgres engine compiled to wasm. Install once:
  `npm --prefix backend/tests/pglite ci`.
- **Integration.** Everything behind the `db`, `app` or `pg_url` fixtures. Give it a database:

  ```bash
  docker compose -f ops/compose.test.yml up -d
  echo 'TEST_DATABASE_URL=postgresql://spielplan:spielplan@127.0.0.1:5442/spielplan_test' > .env.test
  ```

  `conftest.py` loads `.env.test`, migrates a template database once per run and clones it for
  each test. The names are per checkout and xdist worker, so a run reclaims what a killed one
  left, and two runs in one checkout at once collide. `docker compose -f ops/compose.test.yml
  down -v` drops them all. `-rs` names the skips: a green run without the URL has not run the
  integration layer; CI does.

Two test doubles refuse rather than mock. `ops/fake_jellyfin.py` answers the Jellyfin routes §7.1
names and **rejects the admin API key on the Played write on purpose**, so §7.3's per-user-token
rule is something a test can break. `backend/tests/fixtures/soft_authenticator.py` signs genuine
WebAuthn assertions with a real P-256 key. `ops/fake_llm.py` stands in for the three LLM providers.

Fixtures reproduce the corpus's landmines, not its volume: `backend/tests/fixtures/make_bundle.py`
builds an eight-title bundle carrying every trap, plus `break_*` helpers that violate one rule
each. `make_bundle(dir, pool_titles=N)` adds generated owned movies when a test needs scale.

## Real bundle

Two tests compare a real corpus bundle against the committed shape manifest
(`tests/fixtures/real_bundle_shapes.json`). They skip unless `CORPUS_BUNDLE_DIR` points at one:

```bash
CORPUS_BUNDLE_DIR=/path/to/bundle python -m pytest backend/tests/test_bundle_shapes.py \
    backend/tests/test_bundle_validation.py -k real_bundle -q -rs
```

## Restore drill

`test_restore_drill.py` and `test_upgrade_drill.py` drive `pg_restore` against a real Postgres. A
drill on a real stack follows README's Restore section with a dump this build's worker wrote,
because a dump restores only into the image that wrote it. `.github/workflows/release.yml` runs
that drill at stack level on the self-hosted corpus runner.

## Frontend

```bash
npm --prefix frontend test          # vitest; *.svelte.test.js files mount components under jsdom
npm --prefix frontend run check     # svelte-check
npm --prefix frontend run build
```

## E2E

```bash
npm --prefix e2e install
npm --prefix e2e run install-browsers
node e2e/run.mjs                    # = npm --prefix e2e run fresh
```

See [`e2e/README.md`](../e2e/README.md). The runner's two phases are load-bearing; plain
`playwright test` against a used stack skips the first-boot and bundle specs and reports green.

## CI

`.github/workflows/ci.yml` runs on every push: ruff, the backend suite without and with Postgres,
the frontend tests, check and build, and the e2e suite against the compose stack.
`real-bundle.yml` and `release.yml` run on a self-hosted runner labelled `spielplan-corpus` that
holds the corpus bundle (decision 183); until one is registered they queue and are cancelled.
