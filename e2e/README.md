# End-to-end tests

Playwright, driving the real app in a real browser.

```bash
npm --prefix e2e install
npm --prefix e2e run install-browsers   # chromium AND webkit; the phone project is WebKit

npm --prefix e2e run fresh              # = node e2e/run.mjs: stack up, reset, run everything
```

`fresh` rebuilds the fixture bundle into `data/import`, starts the stack with all three compose
files (the app, the published database port, and `ops/compose.e2e.yml`'s fake Jellyfin), resets it
to a first boot, runs `01-first-boot.spec.js` alone, restarts the backend and worker, waits for the
imported bundle to load, and runs everything else. Those two phases are load-bearing: plain
`playwright test` against a used stack skips the first-boot and bundle specs and reports green.
`e2e/reset.mjs` is destructive — it drops the database and empties `data/artifacts`.

## Where it points

`BASE_URL` defaults to `PUBLIC_URL` from `.env`, else `http://localhost:8080`. It must be the
app's **origin**: WebAuthn binds credentials to it, so a passkey registered at
`http://localhost:8080` is refused at `http://127.0.0.1:8080`.

## Files

Specs are stateful, run in filename order on one worker. Number a new file into the sequence.

`desktop` (1400x900) runs everything. `phone` (iPhone 13, WebKit) runs the shell, library,
responsive, rank, Tonight, admin Data and connectors files: the phone is the primary form factor.
`09-passkeys.spec.js` is desktop-only because it drives Chromium's virtual authenticator over CDP.

`05-milestones.spec.js` asserts that unbuilt surfaces are placeholders reached only by URL. It
fails when such a surface ships; replace it with the surface's real tests.
