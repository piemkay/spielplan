# End-to-end tests

Playwright, driving the real app in a real browser.

```bash
npm --prefix e2e install
npm --prefix e2e run install-browsers

docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d
npm --prefix e2e run fresh          # reset to first boot, then run everything
```

`fresh` matters: `01-first-boot.spec.js` tests the first-boot sequence, which only exists on a
database with no admin. Without a reset it skips rather than pretending to have passed. Every
other file adapts — it signs in if an admin exists, and skips the bundle-dependent assertions if
nothing has been imported.

## Where it points

`BASE_URL` defaults to `http://127.0.0.1:8080` — the real backend serving the built PWA, which
is the only configuration that proves what ships. For faster UI iteration you can point it at
the Vite dev server in front of the fixture harness, but that path does not touch Postgres and
therefore cannot prove an import:

```bash
python ops/devstub.py &
npm --prefix frontend run dev
BASE_URL=http://127.0.0.1:5173 npm --prefix e2e test
```

## Two projects

`desktop` (1400×900) runs everything. `phone` (iPhone 13, touch) runs the shell, library and
responsive files — §6 makes the phone the primary form factor, not a variant, so it gets its own
run rather than a few resize calls inside a desktop test.

## When a milestone lands

`05-milestones.spec.js` asserts that unbuilt surfaces say which milestone owes them. Those
assertions are designed to **fail** when the surface ships — that failure is the reminder to
replace the placeholder with the real tests. See `docs/TESTING.md`.
