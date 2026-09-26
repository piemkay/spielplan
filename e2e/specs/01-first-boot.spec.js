import { expect, test } from '@playwright/test';

import { ADMIN, createAdminThroughWizard, health, setupState } from '../helpers.js';

/**
 * §3.1 first boot, §10 the swap sequence, §12 "bundle imports clean". Needs a database with no
 * admin (`node e2e/reset.mjs`), else it skips. One page for the whole file: it is a sequence.
 */
// Never retried: a retry meets an app past first boot and skips, which Playwright scores as flaky
// and exits 0 for, so a failed import would pass (decision 185).
test.describe.configure({ mode: 'serial', retries: 0 });

test.describe('first boot @first-boot', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }) => {
    page = await browser.newPage({ baseURL });
    const state = await setupState(page.request);
    // `required`: an anonymous /api/setup/state carries only that bit and the note (sec-14).
    test.skip(!state.required, 'needs a fresh database — run node e2e/reset.mjs');
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('a bundle-less app boots, serves the wizard, and says so', async () => {
    const before = await health(page.request);
    expect(before.ok, 'the app must be healthy with no bundle').toBe(true);
    expect(before.bundle).toBeNull();

    await page.goto('/');
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByText('first boot · a bundle-less app is a legal state')).toBeVisible();
  });

  test('the first step warns that PUBLIC_URL is load-bearing for passkeys', async () => {
    await page.goto('/setup');
    // \s+: the paragraph wraps in the source, so the text node carries a newline.
    await expect(
      page.getByText(/Passkeys are bound to the public origin\.\s+Changing PUBLIC_URL/)
    ).toBeVisible();
    await expect(page.getByText(/invalidates every\s+registered credential/)).toBeVisible();

    // The rendered origin must be the server's actual PUBLIC_URL, not the literal token.
    const config = await page.request.get('/api/config');
    expect(config.ok()).toBeTruthy();
    const publicUrl = (await config.json()).public_url;
    expect(publicUrl, 'the app must know its own origin').toMatch(/^https?:\/\/\S+$/);
    await expect(page.getByTestId('setup-public-url')).toHaveText(publicUrl);
  });

  test('creating the admin signs them in and lands on Home', async () => {
    await createAdminThroughWizard(page);
    await expect(page.locator('.chip')).toContainText(ADMIN.name);
  });

  test('the wizard asks for no push permission and runs no install walkthrough', async () => {
    // Push and install onboarding belong on each member's own device (decision 164).
    await page.addInitScript(() => {
      window.__pushAsks = 0;
      if (window.Notification) {
        window.Notification.requestPermission = () => {
          window.__pushAsks += 1;
          return Promise.resolve('denied');
        };
      }
    });
    await page.goto('/setup');

    // Every step, reached through the progress dots.
    for (const title of ['Create the admin account', 'Connectors', 'Import the bundle']) {
      await page.getByRole('button', { name: new RegExp(`^${title}`) }).click();
      await expect(page.getByRole('heading', { name: title })).toBeVisible();
      await expect(page.getByTestId('onboarding')).toHaveCount(0);
      await expect(page.getByText(/Add to Home Screen/i)).toHaveCount(0);
    }
    expect(await page.evaluate(() => window.__pushAsks)).toBe(0);
  });

  test('an admin cannot be created twice', async ({ playwright, baseURL }) => {
    // Asked anonymously, because that is who would try the escalation.
    const anonymous = await playwright.request.newContext({ baseURL });
    const res = await anonymous.post('/api/setup/admin', {
      data: { name: 'second-admin', password: 'another-long-password' },
      failOnStatusCode: false,
    });
    expect(res.status()).toBe(409);
    await anonymous.dispose();
  });

  test('Home renders the no-bundle state rather than an error', async () => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Nothing to show yet' })).toBeVisible();
    await expect(page.getByText(/That is a legal state/)).toBeVisible();
    await expect(page.getByRole('link', { name: 'Import a bundle' })).toBeVisible();
    // Both places say it; each is asserted apart so a bare text match does not trip strict mode.
    await expect(page.getByRole('link', { name: 'no bundle imported' })).toBeVisible();
    await expect(page.locator('.count')).toContainText('no bundle imported');
  });

  test('validation reports every §4.1 landmine rule before anything is written', async () => {
    await page.goto('/admin/data');
    await page.getByRole('button', { name: 'Validate bundle' }).click();
    await expect(page.locator('.verdict')).toHaveText('valid');

    for (const rule of [
      'rule7-denylist',
      'rule5-kind',
      'rule6-no-unique',
      'rule6-coalesce',
      'rule4-frozen-ids',
      'rule1-two-tiers',
      'rule1-evidence',
      'rule2-weights',
      'rule8-utf8',
    ]) {
      await expect(page.locator('.finding .rule', { hasText: rule }).first()).toBeVisible();
    }

    // §4.1 rule 1: shared (title,term) pairs are counted, never deduped.
    await expect(
      page.locator('.finding', { hasText: 'pairs exist in both tiers and stay distinguishable' })
    ).toBeVisible();

    // Validation writes nothing.
    expect((await health(page.request)).bundle).toBeNull();
  });

  test('import runs the swap sequence and serves the bundle without a restart', async () => {
    // The import is a worker job (decision 253): the request answers 202 and the load runs on
    // the next 20 s tick, so this one test waits longer than the config default.
    test.setTimeout(180_000);

    await page.goto('/admin/data');
    await page.getByRole('button', { name: 'Validate bundle' }).click();
    await expect(page.locator('.verdict')).toHaveText('valid');
    await page.getByRole('button', { name: 'Import and activate' }).click();

    // `running` is always observable: the request creates the `job_run` row, so the first poll
    // cannot find it terminal.
    const box = page.locator('[data-phase]');
    await expect(box).toHaveAttribute('data-phase', 'running', { timeout: 30_000 });
    // A second press would race §10's staging tree.
    await expect(page.getByRole('button', { name: 'Import and activate' })).toBeDisabled();

    // The findings below are the worker's load report, not the 202's validation.
    await expect(box).toHaveAttribute('data-phase', 'imported', { timeout: 120_000 });

    await expect(page.locator('.finding', { hasText: 'artifacts staged to' })).toBeVisible();
    await expect(page.locator('.finding', { hasText: 'vocabulary v1' })).toBeVisible();
    await expect(page.locator('.finding', { hasText: 'authored axis definition' })).toBeVisible();

    // Decision 497: the backend loads the flipped bundle itself, so no restart is asked for.
    await expect(page.locator('[data-served="live"]')).toContainText('test-v1 is live');
    await expect(page.locator('[data-served="restart"]')).toHaveCount(0);

    await page.reload();
    await expect(page.locator('.bundle-active')).toContainText('active: test-v1');
    await expect(page.locator('.bundle-active .warn')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'no bundle imported' })).toHaveCount(0);
    expect((await health(page.request)).bundle).toBe('test-v1');
  });
});
