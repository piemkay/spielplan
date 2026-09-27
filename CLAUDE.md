# CLAUDE.md

Household media graph: FastAPI backend + worker (same codebase), Postgres 16, SvelteKit PWA
served by the backend. Operating it: [README.md](README.md). Tests: [docs/TESTING.md](docs/TESTING.md).

**The spec is the authority.** `docs/spielplan-spec_v2.1.md` is the one normative document;
where code and spec disagree, the code is the bug. It is amended **in place**, never forked
(decision 288). `docs/spec-v2.2-proposals.md` is the decision record: proposals 1-161 are dated
reasoning, citable as provenance only; entries 162 onward are numbered owner decisions, normative
from the day each is taken until the amendment it mandates lands in the spec.

## Commands

```bash
backend/.venv/Scripts/python -m pytest backend/tests -q   # POSIX: .venv/bin/python
# never bare `pytest` from repo root: pytest config lives in backend/pyproject.toml
# (asyncio_mode=auto); without the path argument async tests error

ruff check .                        # from repo root; root ruff.toml widens scope to ops/
npm --prefix frontend test          # also: run dev (:5173, proxies /api to :8080), run build, run check
npm --prefix e2e run fresh          # canonical full e2e (= node e2e/run.mjs)
```

- Integration tests need Postgres and **skip silently** without `TEST_DATABASE_URL`
  (auto-loaded from `.env.test`); the PGlite schema tests skip without
  `backend/tests/pglite/node_modules`. A green no-DB run has NOT run those layers; CI will.
  DB up: `docker compose -f ops/compose.test.yml up -d` (port 5442; see docs/TESTING.md).
- Frontend dev runs against the real backend: the compose stack or a hand-run uvicorn on :8080.
- `ruff format` is not enforced; don't reformat wholesale. Line length is 108.

## Conventions

- Rules live in the domain packages under `backend/spielplan/` (ledger, rate, scoring,
  placement, home, sync, connectors, importer, acquire, llm); `api/` decides only HTTP shapes.
  `ledger/model.py` is numpy-only by contract: no DB, no clock, no torch.
- Frontend: Svelte 5 runes, JS not TS. Shared state in `.svelte.js` modules with colocated
  `*.test.js`. Always relative `/api` URLs.
- Comments only where the code cannot speak. Cite a section or decision (`§N.M`, `decision N`) in
  at most one short clause, and only when the reason is not obvious. No narration, no history.
- Tests assert behaviour. Write no new static guard (a test that greps source or docs) unless a
  real regression needs one.
- Test doubles refuse rather than mock: `ops/fake_jellyfin.py` rejects the admin key on the
  Played write on purpose; don't "fix" it.
- Commits: `feat(M2):`-style prefix, lowercase subject, a short body.
- Diffs are surgical: every changed line traces to the request. No speculative abstractions or
  unrequested configurability.
- Keep console/test output ASCII: Windows cp1252 consoles crash on decorative glyphs.

## Gotchas

- **Never edit an applied migration** (`backend/migrations/NNNN_*.sql` is sha256-checksummed;
  a mismatch is a hard startup error). Add a new numbered file.
- The static guards that remain pin real invariants: the CPU-only torch index in the Dockerfile,
  the frozen `rating_source` ids, one plain-HTTP port, the `/data/*` bind mounts, the layering
  between packages. A guard failure after touching those is the contract working, not flake.
- WebAuthn binds passkeys to the **origin**: e2e runs against `http://localhost:8080`
  (= `PUBLIC_URL`), never `127.0.0.1:8080`; same server, different origin, passkeys fail.
- E2E specs are stateful, filename-ordered, one worker. Number new files into the sequence;
  don't parallelize. `run.mjs`'s two phases and the restart between them are load-bearing: plain
  `playwright test` against a used stack skips the first-boot and bundle specs and fake-passes.
  `e2e/reset.mjs` is destructive (drops the DB, wipes `data/artifacts`). The `phone` project
  (iPhone 13, WebKit) is the primary form factor.
