import { defineConfig, devices } from '@playwright/test';
import { baseUrl } from './env.mjs';

// Must be the app's own PUBLIC_URL origin: WebAuthn binds passkeys to the origin, so 127.0.0.1
// against an rp_id of `localhost` is refused.
const BASE_URL = baseUrl();
const DESKTOP = { ...devices['Desktop Chrome'], viewport: { width: 1400, height: 900 } };
const FIRST_BOOT = /01-first-boot\.spec\.js/;

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
    // It imports the bundle every other spec reads, so a failed first boot runs nothing else.
    { name: 'first-boot', testMatch: FIRST_BOOT, use: DESKTOP },
    { name: 'desktop', dependencies: ['first-boot'], testIgnore: FIRST_BOOT, use: DESKTOP },
    {
      // The primary form factor (§6 preamble).
      name: 'phone',
      dependencies: ['first-boot'],
      use: { ...devices['iPhone 13'] },
      // Playwright runs the whole desktop pass first on the same stack, so a spec here reads its
      // starting state and puts back what it changed. 15-tonight-group needs two contexts and
      // stays on desktop. `shell` matches 02-shell and 19-phone-shell (decision 267).
      testMatch: /(shell|library|responsive|11-ladder-setup|13-rank|14-tonight|20-admin-data|21-connectors|22-taste)\.spec\.js/,
    },
  ],
});
