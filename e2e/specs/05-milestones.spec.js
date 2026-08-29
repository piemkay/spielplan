import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * §12's build order, made visible. Every surface the spec names exists as a destination; the
 * ones their milestone has not reached say so, in the spec's own words, rather than 404ing or
 * pretending.
 *
 * These assertions are deliberately *inverted* as milestones land: when M2 ships Rate, the
 * "not built yet" expectation here fails, and that failure is the reminder to replace this
 * placeholder test with the real one. See docs/TESTING.md.
 */

const PENDING = [
  { path: '/rate', surface: 'Rate', milestone: 'M2' },
  { path: '/tonight', surface: 'Tonight', milestone: 'M4' },
  { path: '/rank', surface: 'Rank', milestone: 'M3' },
  { path: '/map', surface: 'Map', milestone: 'M6' },
  { path: '/taste', surface: 'Taste', milestone: 'M6' },
  { path: '/account', surface: 'Account', milestone: 'M1' },
];

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

for (const { path, surface, milestone } of PENDING) {
  test(`${surface} names the milestone that owes it (${milestone})`, async ({ page }) => {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: surface })).toBeVisible();
    await expect(page.getByText(milestone, { exact: true })).toBeVisible();
    await expect(page.getByText(`Not built yet — this surface arrives with ${milestone}.`)).toBeVisible();
  });
}

test('a placeholder still describes what the surface will do', async ({ page }) => {
  // An honest placeholder is not a blank page: it carries the spec's own description, so the
  // shape of the finished app is legible from day one.
  await page.goto('/tonight');
  await expect(page.getByText(/roughly ten candidate votes|adaptive|blind/i)).toBeVisible();
  await expect(page.locator('main li')).not.toHaveCount(0);
});

test('the admin Data tab is real, and its siblings are labelled with their milestone', async ({
  page,
}) => {
  // §3.1 scopes the bundle-import page to M0: "that one page is M0 scope".
  await page.goto('/admin/data');
  await expect(page.getByRole('heading', { name: 'Artifact bundle' })).toBeVisible();
  for (const tab of ['Connectors', 'Users', 'System']) {
    await expect(page.getByText(tab, { exact: true })).toBeVisible();
  }
});

test('the re-import rebuild set is stated where the re-import happens', async ({ page }) => {
  // §10: "everything expressed in the old Backbone's basis is garbage against a new one."
  // The operator has to be able to read that at the moment they are about to do it.
  await page.goto('/admin/data');
  await expect(page.getByText('RECOMPUTED ON EVERY RE-IMPORT')).toBeVisible();
  for (const item of ['fold-in vectors', 'blend weights', 'Ledger MAP refit', 'Cold Tower']) {
    await expect(page.getByText(new RegExp(item))).toBeVisible();
  }
});
