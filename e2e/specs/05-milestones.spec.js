import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * §12's build order, made visible. The placeholder assertions FAIL when a surface ships: that is
 * the reminder to replace them with the real test. An unbuilt surface is reached by URL only
 * (decision 488) and names no milestone (decision 486), so the milestone lives in this table.
 */

const PENDING = [
  { path: '/map', surface: 'Map', milestone: 'M6' },
  { path: '/taste', surface: 'Taste', milestone: 'M6' }
];

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

for (const { path, surface, milestone } of PENDING) {
  test(`${surface} is not built yet, and is reached only by its address (${milestone})`, async ({
    page
  }) => {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: surface })).toBeVisible();
    await expect(page.getByText('Not built yet — this is coming in a later update.')).toBeVisible();
    // Decision 486: no milestone label on a member surface, switch on or off.
    await expect(page.getByTestId('placeholder')).not.toContainText(/\bM[0-7](\.\d+)?\b/);
    // Decision 488: nothing in the shell links here while it is a placeholder.
    await expect(page.locator(`a[href="${path}"]`)).toHaveCount(0);
  });
}

test('a placeholder still describes what the surface will do', async ({ page }) => {
  await page.goto('/map');
  await expect(page.getByText(/axis scatter|explore|wander|connections/i)).toBeVisible();
  await expect(page.locator('main li')).not.toHaveCount(0);
});

test('Rate is built — M2 landed, so it is no longer a placeholder', async ({ page }) => {
  // 11-rate.spec.js is the real test.
  await page.goto('/rate');
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
  await expect(page.getByTestId('rate-counter')).toBeVisible();
});

test('Rank is built — M3 landed, so it is no longer a placeholder', async ({ page }) => {
  // 13-rank.spec.js is the real test.
  await page.goto('/rank');
  await expect(page.getByTestId('rank-surface')).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
  await expect(page.getByTestId('rank-board')).toBeVisible();
  await expect(page.getByTestId('rank-sharpen')).toBeVisible();
});

test('Tonight is built — M4 landed, so it is no longer a placeholder', async ({ page }) => {
  // 14-tonight and 15-tonight-group are the real tests.
  await page.goto('/tonight');
  await expect(page.getByTestId('tonight-surface')).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
  await expect(page.getByTestId('tonight-controls')).toBeVisible();
  await expect(page.getByTestId('tonight-open')).toBeVisible();
  await expect(page.getByTestId('tonight-solo-door')).toBeVisible();
});


test('Account is built — M1 landed, so it is no longer a placeholder', async ({ page }) => {
  // 09-passkeys.spec.js is the real test.
  await page.goto('/account');
  await expect(page.getByRole('heading', { name: 'Account' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Passkeys' })).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
});

test('the admin Data, Connectors, Users and System tabs are all real', async ({ page }) => {
  // Links, not labels: 21-connectors and 18-system assert what is behind them.
  await page.goto('/admin/data');
  await expect(page.getByRole('heading', { name: 'Artifact bundle' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Connectors' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Users' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'System' })).toBeVisible();

  // A pending tab renders a milestone token instead of an href.
  await expect(page.locator('.tabs').getByText(/^M\d/)).toHaveCount(0);
  await expect(page.getByText(/^(Users|System): Not built yet/)).toHaveCount(0);
});

test('the re-import rebuild set is stated where the re-import happens', async ({ page }) => {
  // §10: "everything expressed in the old Backbone's basis is garbage against a new one."
  await page.goto('/admin/data');
  await expect(page.getByText('RECOMPUTED ON EVERY RE-IMPORT')).toBeVisible();
  for (const item of ['fold-in vectors', 'blend weights', 'Ledger MAP refit', 'Cold Tower']) {
    await expect(page.getByText(new RegExp(item))).toBeVisible();
  }
});
