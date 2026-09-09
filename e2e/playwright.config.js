import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests against the real stack.
 *
 * `BASE_URL` points at whatever is serving the app:
 *   - `docker compose up` — the real backend serving the built PWA on :8080 (the default, and
 *     the only configuration that proves the thing we ship)
 *   - `npm --prefix frontend run dev` + `python ops/devstub.py` on :5173 — faster for iterating
 *     on the UI, but it does not exercise Postgres, so it cannot prove an import
 *
 * The suite runs in TWO PHASES, and `node e2e/run.mjs` is what implements them: phase 1 runs
 * `specs/01-first-boot.spec.js` alone against an empty database, the services restart so the
 * bundle it imported is loaded (§10), and phase 2 runs everything else with
 * `--grep-invert @first-boot`. So `@first-boot` is a statement about which phase owns a file and
 * nothing else. A test that needs the real backend does not need a tag; it needs the phase 2
 * the runner gives it.
 *
 * Most of the files that need an IMPORTED bundle skip themselves on `config.has_bundle`; the
 * rest need no bundle and carry no guard, and two of those skip on `browserName`, which is a
 * different axis. `08-jellyfin.spec.js` is the deliberate exception and has to stay one: it is
 * the spec that still FAILS on a stack that imported nothing, which is all that stands between
 * such a run and a suite of skips reported as a green job — so giving it the guard for
 * consistency is the tidy-up `test_the_jellyfin_spec_is_not_given_a_has_bundle_guard` fails the
 * build over. [M4.8 review cycle 3: m48-c3-e2e-03]
 */
// Must be the app's own PUBLIC_URL origin, not merely an address that reaches it. WebAuthn
// binds credentials to the origin (§2, §14.4), so a passkey registered from
// http://127.0.0.1:8080 against an rp_id of `localhost` is refused — correctly, and
// confusingly. Same host, same port, different origin.
const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080';

export default defineConfig({
  testDir: './specs',
  outputDir: './.results',
  // The suite drives a first-boot wizard and imports a bundle; those are stateful and must
  // not race each other. Files run in order, one worker.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [['github'], ['html', { open: 'never' }], ['list']]
    : [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: process.env.CI ? 'retain-on-failure' : 'off',
    // The app is dark-first and single-theme (§6.8); pinning this keeps screenshots stable.
    colorScheme: 'dark',
  },

  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 900 } },
    },
    {
      // §6 preamble: phone-first, 48 px targets, one-handed. The phone is not a variant of
      // the desktop layout here — it is the primary one, so it gets its own project rather
      // than a handful of resize calls.
      name: 'phone',
      use: { ...devices['iPhone 13'] },
      // §6.3's tap-to-tier is a phone gesture — "tap a title (it lifts), tap a tier (it
      // drops)" — so 13-rank runs here as well as on desktop. It seeds a member per
      // project, because §4.2's observations are append-only and the two runs would
      // otherwise share a board.
      //
      // 14-tonight joins them for the same reason: §6.2 step 2's hand-the-phone is a
      // statement about a PHONE ("Guests use the initiator's phone"), and solo is the
      // one-tap path §6's preamble is written around. 15-tonight-group needs two browser
      // contexts and 16-tonight-tv is a television, so both stay on desktop.
      testMatch: /(shell|library|responsive|13-rank|14-tonight)\.spec\.js/,
    },
  ],
});
