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
  { path: '/map', surface: 'Map', milestone: 'M6' },
  { path: '/taste', surface: 'Taste', milestone: 'M6' }
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
  // shape of the finished app is legible from day one. Moved from /tonight to /map when M4
  // shipped — the routine below.
  await page.goto('/map');
  await expect(page.getByText(/axis scatter|explore|wander|connections/i)).toBeVisible();
  await expect(page.locator('main li')).not.toHaveCount(0);
});

test('Rate is built — M2 landed, so it is no longer a placeholder', async ({ page }) => {
  // Same routine as Account below, one milestone on: the placeholder assertion failed the day
  // Rate shipped, and that failure was the reminder to write this. 11-rate.spec.js is the real
  // test; this one only asserts the surface stopped naming a milestone it no longer owes.
  await page.goto('/rate');
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
  await expect(page.getByTestId('rate-counter')).toBeVisible();
});

test('Rank is built — M3 landed, so it is no longer a placeholder', async ({ page }) => {
  // The routine again, one milestone on. The `/rank` placeholder assertion above failed the
  // day the board shipped, and that failure was the reminder to write this. 13-rank.spec.js is
  // the real test; this one only asserts the surface stopped naming a milestone it no longer
  // owes — and that §6.3's board and its queue control are both actually on the page.
  await page.goto('/rank');
  await expect(page.getByTestId('rank-surface')).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
  await expect(page.getByTestId('rank-board')).toBeVisible();
  await expect(page.getByTestId('rank-sharpen')).toBeVisible();
});

test('Tonight is built — M4 landed, so it is no longer a placeholder', async ({ page }) => {
  // The routine again, one milestone on. The `/tonight` placeholder assertion above failed the
  // day the surface shipped, and that failure was the reminder to write this. 14-tonight,
  // 15-tonight-group and 16-tonight-tv are the real tests; this one only asserts the surface
  // stopped naming a milestone it no longer owes, and that §6.2 step 1's controls and both of
  // its doors are actually on the page.
  await page.goto('/tonight');
  await expect(page.getByTestId('tonight-surface')).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
  await expect(page.getByTestId('tonight-controls')).toBeVisible();
  await expect(page.getByTestId('tonight-open')).toBeVisible();
  await expect(page.getByTestId('tonight-solo-door')).toBeVisible();
});


test('Account is built — M1 landed, so it is no longer a placeholder', async ({ page }) => {
  // This test replaces the placeholder assertion Account carried at M0. The routine in
  // docs/TESTING.md is exactly this: the placeholder fails when the surface ships, and that
  // failure is the reminder to write the real test. 09-passkeys.spec.js is that real test.
  await page.goto('/account');
  await expect(page.getByRole('heading', { name: 'Account' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Passkeys' })).toBeVisible();
  await expect(page.getByText(/Not built yet/)).toHaveCount(0);
});

test('the admin Data and Connectors tabs are real; the rest name their milestone', async ({
  page,
}) => {
  // §3.1 scopes the bundle-import page to M0: "that one page is M0 scope". §6.6's Jellyfin
  // card is M1; its LLM/TMDB half and the Users and System cards are M5.
  await page.goto('/admin/data');
  await expect(page.getByRole('heading', { name: 'Artifact bundle' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Connectors' })).toBeVisible();
  for (const tab of ['Users', 'System']) {
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
