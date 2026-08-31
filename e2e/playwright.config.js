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
 * Tests carry a `@needs-db` tag when they require the real backend; `npm run test:ui-only`
 * skips them.
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
