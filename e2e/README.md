# End-to-end tests

Playwright, driving the real app in a real browser.

```bash
npm --prefix e2e install
npm --prefix e2e run install-browsers   # chromium AND webkit — the phone project is WebKit

npm --prefix e2e run fresh              # brings the stack up, resets, runs everything
```

`fresh` starts the stack itself, with all three compose files — the app, the published database
port, and `ops/compose.e2e.yml`'s fake Jellyfin. §7.3's two-way sync cannot be shown in a
browser without a media server that answers.

`fresh` matters: `01-first-boot.spec.js` tests the first-boot sequence, which only exists on a
database with no admin. Without a reset it skips rather than pretending to have passed. Every
other file adapts — it signs in if an admin exists, and skips the bundle-dependent assertions if
nothing has been imported.

## The bundle it imports

`fresh` does **not** build one. `data/import` is untracked runtime state, so the suite imports
whatever bundle is sitting there — and a fixture that has moved on since that file was written is
tested against the old shapes, silently. `.github/workflows/ci.yml` rebuilds it on every run;
locally it is one command, and it is what to run after any change to
`backend/tests/fixtures/make_bundle.py`:

```bash
backend/.venv/Scripts/python -c "import sys, pathlib; sys.path.insert(0, 'backend'); \
  from tests.fixtures.make_bundle import make_bundle; make_bundle(pathlib.Path('data/import'))"
# POSIX: .venv/bin/python. No `pool_titles`: 10-home and test_import_integration count what
# this writes, so the scale mode stays off here for the same reason it is off in CI.
```

A case whose subject is a fixture shape says so rather than passing vacuously.
`04-title-card.spec.js`'s cross-department credit case fails its own precondition — "no title in
this bundle carries a cross-department credit - nothing here is under test" — against a bundle
built before M4.8 gave `make_bundle` the second department spelling (`eef036c`). That message
means this command, not a broken read layer.

## Where it points

`BASE_URL` defaults to `http://localhost:8080` — the real backend serving the built PWA, which
is the only configuration that proves what ships. It must be `PUBLIC_URL`'s **origin** and not
merely an address that reaches the same server: WebAuthn binds credentials to the origin (§2,
§14.4), so a passkey registered at `http://localhost:8080` is correctly refused at
`http://127.0.0.1:8080`. Same host, same port, different origin.

For faster UI iteration you can point it at the Vite dev server in front of the fixture
harness, but that path does not touch Postgres and therefore cannot prove an import:

```bash
python ops/devstub.py &
npm --prefix frontend run dev
BASE_URL=http://localhost:5173 npm --prefix e2e test
```

## Two projects

`desktop` (1400×900) runs everything. `phone` (iPhone 13, touch, WebKit) runs the shell, library
and responsive files — §6 makes the phone the primary form factor, not a variant, so it gets its
own run rather than a few resize calls inside a desktop test.

`09-passkeys.spec.js` is desktop-only for a reason rather than an omission: it drives Chromium's
virtual authenticator over CDP, which is how the ceremony can be genuine — real CBOR, a real
P-256 signature, a real origin check — with only the biometric simulated.

## When a milestone lands

`05-milestones.spec.js` asserts that unbuilt surfaces say which milestone owes them. Those
assertions are designed to **fail** when the surface ships — that failure is the reminder to
replace the placeholder with the real tests. See `docs/TESTING.md`.
