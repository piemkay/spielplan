import { expect, test } from '@playwright/test';

import { ADMIN, createAdminThroughWizard, health, setupState } from '../helpers.js';

/**
 * §3.1 — first boot is a defined sequence, and a bundle-less app is a legal state.
 * §10 — the swap sequence: validate → stage → load → transactionally flip → restart.
 * §12 — the M0 exit criterion: "bundle imports clean".
 *
 * This file needs a database with no admin. `node e2e/reset.mjs` does that, and
 * `node e2e/run.mjs` runs the whole suite from a cold start. Without a reset the file skips
 * rather than pretending to have passed.
 *
 * One page for the whole file, on purpose: this is a *sequence*, and Playwright's default of a
 * fresh browser context per test would throw away the session the wizard just established —
 * which is not a thing that happens to a real operator.
 */
test.describe.configure({ mode: 'serial' });

test.describe('first boot @needs-db', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }) => {
    page = await browser.newPage({ baseURL });
    const state = await setupState(page.request);
    test.skip(state.has_admin, 'needs a fresh database — run node e2e/reset.mjs');
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('a bundle-less app boots, serves the wizard, and says so', async () => {
    // §3.1: "the app boots with /data/artifacts and artifact_bundle empty, serving the setup
    // wizard and admin routes" — the absence of a bundle is reported, not an error.
    const before = await health(page.request);
    expect(before.ok, 'the app must be healthy with no bundle').toBe(true);
    expect(before.bundle).toBeNull();

    await page.goto('/');
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByText('first boot · a bundle-less app is a legal state')).toBeVisible();
  });

  test('the first step warns that PUBLIC_URL is load-bearing for passkeys', async () => {
    // §14.4: changing PUBLIC_URL invalidates every registered credential. The wizard has to
    // say so before anyone registers one.
    await page.goto('/setup');
    // \s+ rather than a literal space: the paragraph wraps in the source, so the rendered text
    // node carries a newline mid-sentence.
    await expect(
      page.getByText(/Passkeys are bound to the public origin\.\s+Changing PUBLIC_URL/)
    ).toBeVisible();
    await expect(page.getByText(/invalidates every\s+registered credential/)).toBeVisible();
  });

  test('creating the admin signs them in and lands on Home', async () => {
    await createAdminThroughWizard(page);
    await expect(page.locator('.chip')).toContainText(ADMIN.name);
  });

  test('an admin cannot be created twice', async ({ playwright, baseURL }) => {
    // Otherwise this is a privilege-escalation endpoint reachable by anyone who can see the
    // setup page. Asked anonymously, because that is who would ask.
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
    // Two places say it, and both are deliberate: the header chip is the standing reminder,
    // the count line is the answer to "why is this list empty". Assert each explicitly rather
    // than letting a bare text match hit both and trip strict mode.
    await expect(page.getByRole('link', { name: 'no bundle imported' })).toBeVisible();
    await expect(page.locator('.count')).toContainText('no bundle imported');
  });

  test('validation reports every §4.1 landmine rule before anything is written', async () => {
    await page.goto('/admin/data');
    await page.getByRole('button', { name: 'Validate bundle' }).click();
    await expect(page.locator('.verdict')).toHaveText('valid');

    // Each of these is a rule the corpus paid for. The report is where they become visible.
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

  test('import runs the swap sequence and asks for the restart it needs', async () => {
    await page.goto('/admin/data');
    await page.getByRole('button', { name: 'Validate bundle' }).click();
    await expect(page.locator('.verdict')).toHaveText('valid');
    await page.getByRole('button', { name: 'Import and activate' }).click();

    await expect(page.locator('.finding', { hasText: 'artifacts staged to' })).toBeVisible();
    await expect(page.locator('.finding', { hasText: 'vocabulary v1' })).toBeVisible();
    await expect(page.locator('.finding', { hasText: 'authored axis definition' })).toBeVisible();

    // §10: the flip is real in the database and invisible to this process until a restart.
    // Saying so is the difference between "it worked" and "did it work?".
    await page.reload();
    await expect(page.locator(".bundle-active")).toContainText('active: test-v1');
    await expect(page.locator('.warn')).toContainText(/restart backend and worker/);

    // The process has not loaded it yet — which is exactly what the banner claims.
    expect((await health(page.request)).bundle).toBeNull();
  });
});
