import { defineConfig, devices } from '@playwright/test';
import { baseUrl } from './env.mjs';

/**
 * End-to-end tests against the real stack, in the two phases `node e2e/run.mjs` implements:
 * `@first-boot` names the file phase 1 owns, and nothing else.
 *
 * `08-jellyfin.spec.js` deliberately has no `config.has_bundle` skip: it is the spec that fails
 * when no bundle was imported, so such a run cannot pass as a suite of skips.
 */
// Must be the app's own PUBLIC_URL origin: WebAuthn binds passkeys to the origin, so 127.0.0.1
// against an rp_id of `localhost` is refused.
const BASE_URL = baseUrl();

export default defineConfig({
  testDir: './specs',
  outputDir: './.results',
  // Stateful: files run in order, one worker.
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
    colorScheme: 'dark',
  },

  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 900 } },
    },
    {
      // The primary form factor (§6 preamble).
      name: 'phone',
      use: { ...devices['iPhone 13'] },
      // Playwright runs the whole desktop pass first on the same stack, so a spec here reads its
      // starting state and puts back what it changed. 15-tonight-group needs two contexts and
      // stays on desktop. `shell` matches 02-shell and 19-phone-shell (decision 267).
      testMatch: /(shell|library|responsive|13-rank|14-tonight|20-admin-data|21-connectors)\.spec\.js/,
    },
  ],
});
