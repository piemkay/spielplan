# CLAUDE.md

Household media graph: FastAPI backend + worker (same codebase), Postgres 16, SvelteKit PWA
served by the backend. Architecture and status: [README.md](README.md).

**The spec is the authority.** `docs/spielplan-spec_v2.1.md` is normative; where code and spec
disagree, the code is the bug. Cite sections as `§N.M` (or `decision N` / `proposal N` from
`docs/spec-v2.2-proposals.md`) in comments and commit bodies — every non-obvious choice here
does, and work that can't cite its clause reads as off-convention.

## Commands

```bash
backend/.venv/Scripts/python -m pytest backend/tests -q   # POSIX: .venv/bin/python
# never bare `pytest` from repo root: pytest config lives in backend/pyproject.toml
# (asyncio_mode=auto); without the path argument async tests error

ruff check .              # from repo root — root ruff.toml widens scope to ops/
npm --prefix frontend test          # also: run dev (:5173), run build, run check
npm --prefix e2e run fresh          # canonical full e2e (= node e2e/run.mjs)
python ops/devstub.py               # fixture-backed API on :8080, no Docker needed
```

- Integration tests need Postgres and **skip silently** without `TEST_DATABASE_URL`
  (auto-loaded from `.env.test`); the PGlite schema tests skip without
  `backend/tests/pglite/node_modules`. A green no-DB run has NOT run those layers — CI will.
  DB up: `docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db`
  (overlays are never auto-loaded; pass each `-f`).
- Plain `playwright test` against a used stack makes first-boot/bundle specs skip and
  fake-pass; `run.mjs`'s two phases + backend restart are load-bearing. `e2e/reset.mjs` is
  destructive (drops the DB, wipes `data/artifacts`).
- `ruff format` is not enforced (CI runs it informationally) — don't reformat wholesale.
  Line length 108 is deliberate: spec sentences are quoted inline and must not wrap.

## Testing contract

`backend/tests/spec_coverage.toml` maps spec requirements to milestones and tests;
`test_spec_coverage.py` fails the build when a shipped-milestone row lacks a test or a named
test no longer exists — so register new spec-driven tests there, and renaming/deleting a
registered test breaks the build. An open milestone's red gate IS the test plan; never
silence it with waivers or by lowering `current_milestone`. Full routine: `docs/TESTING.md`
(also the milestone ledger — read it rather than assuming status).

`e2e/specs/05-milestones.spec.js` asserts placeholders on unbuilt surfaces and **fails by
design** when a surface ships: delete the placeholder assertion and point the coverage rows
at real tests. Test doubles are refusers, not mocks (`ops/fake_jellyfin.py` rejects the admin
key on the Played write on purpose — don't "fix" it). `ops/devstub.py` is a harness, not the
app: `backend/spielplan/api/` wins on any disagreement.

## Conventions

- Rules live in the domain packages under `backend/spielplan/` (ledger, rate, scoring,
  placement, home, sync, connectors, importer); `api/` decides only HTTP shapes.
  `ledger/model.py` is numpy-only by contract — no DB, no clock, no torch.
- Frontend: Svelte 5 runes, JS not TS. Shared state in `.svelte.js` modules with colocated
  `*.test.js`. Always relative `/api` URLs (Vite proxies in dev; backend serves the SPA in prod).
- Comments argue why and cite the spec; they never narrate code. Commits:
  `feat(M2):`-style prefix, lowercase subject, narrative body citing sections.
- Diffs are surgical: every changed line traces to the request. Don't reformat, refactor,
  or "improve" adjacent code; no speculative abstractions or unrequested configurability.
- Keep console/test output ASCII — Windows cp1252 consoles crash on decorative glyphs.

## Gotchas

- **Never edit an applied migration** (`backend/migrations/NNNN_*.sql` — sha256-checksummed;
  a mismatch is a hard startup error). Add a new numbered file.
- Static guard tests read source files and reject spec-violating edits: CPU-only torch index
  in the Dockerfile, frozen `rating_source` ids, one plain-HTTP port, named `/data/*` volumes.
  A guard failure after touching compose/Dockerfile/deps is the contract working, not flake.
- WebAuthn binds passkeys to the **origin**: e2e runs against `http://localhost:8080`
  (= `PUBLIC_URL`), never `127.0.0.1:8080` — same server, different origin, passkeys fail.
- E2E specs are stateful, filename-ordered, one worker. Number new files into the sequence;
  don't parallelize. The `phone` project (iPhone 13, WebKit) is the primary form factor.
