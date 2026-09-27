# End-to-end tests

Playwright, driving the real app in a real browser.

```bash
npm --prefix e2e install
npm --prefix e2e run install-browsers   # chromium AND webkit; the phone project is WebKit

npm --prefix e2e run fresh              # = node e2e/run.mjs: stack up, reset, run everything
```

`fresh` rebuilds the fixture bundle into `data/import`, starts the stack with all three compose
files (the app, the published database port, and `ops/compose.e2e.yml`'s fake Jellyfin), resets it
to a first boot, and runs one Playwright pass. `01-first-boot.spec.js` is the `first-boot` project,
which imports the bundle and on which every other project depends, so a failed first boot runs
nothing else. It needs a fresh database: plain `playwright test` against a used stack skips it.
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
