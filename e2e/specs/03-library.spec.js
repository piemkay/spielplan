import { expect, test } from '@playwright/test';

import { kindToggle, signedIn } from '../helpers.js';

/**
 * §6.0 — the catalog, and §4.1 rule 5 as read by owner decision 18: kind is two independent
 * toggles, either or both active, never neither.
 *
 * Needs an imported bundle. Skips rather than pretending if the app has none.
 */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  const config = await (await page.request.get('/api/config')).json();
  test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
  await page.goto('/');
});

test('films only, and the hidden count names what is missing', async ({ page }) => {
  // §6.0: a toggle that hides things has to say how many. Silent truncation reads as missing
  // data, which is the failure this control was introduced to fix.
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.count')).toContainText(/\d+ films? · \d+ series hidden/);
});

test('both kinds on shows everything and nothing is reported hidden', async ({ page }) => {
  await kindToggle(page, 'Series').click();
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.count')).toContainText(/\d+ titles/);
  await expect(page.locator('.count')).not.toContainText('hidden');
});

test('the last active toggle cannot be turned off', async ({ page }) => {
  // Never neither: an empty selection would silently mean "everything", which is the
  // unpartitioned query §4.1 rule 5 exists to prevent.
  await kindToggle(page, 'Films').click(); // no-op: it is the only one on
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');

  await kindToggle(page, 'Series').click(); // both on
  await kindToggle(page, 'Films').click(); // series only
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'false');
  await kindToggle(page, 'Series').click(); // refused
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'true');
});

test('the API refuses an empty kind selection outright', async ({ page }) => {
  const res = await page.request.get('/api/titles?limit=5', { failOnStatusCode: false });
  expect(res.status(), 'kind is required, not defaulted').toBe(422);
});

test('the facet vocabulary follows the selection', async ({ page }) => {
  // A genre that only exists in the kind you switched off must not linger in the control.
  const genre = page.getByLabel('Genre');
  const filmGenres = await genre.locator('option').allTextContents();

  await kindToggle(page, 'Series').click();
  await expect(async () => {
    const bothGenres = await genre.locator('option').allTextContents();
    expect(bothGenres.length).toBeGreaterThan(filmGenres.length);
  }).toPass();
});

test('search matches an alias, not just the title', async ({ page }) => {
  // §6.0: "filter/search on title/alias". The fixture's CJK title carries its English name
  // only as an alias, which is the case a title-only search silently fails.
  await page.getByLabel('Search titles').fill('chungking');
  await expect(page.locator('.count')).toContainText(/[1-9]/);
  await expect(page.locator('.grid')).toBeVisible();
});

test('a query with no matches says so instead of showing an empty grid', async ({ page }) => {
  await page.getByLabel('Search titles').fill('zzzzzzzz');
  await expect(page.getByRole('heading', { name: 'No matches' })).toBeVisible();
  await expect(page.locator('.count')).toContainText('0 films');
});

test('non-ASCII titles survive to the screen', async ({ page }) => {
  // §4.1 rule 8: never "clean" non-ASCII — the corpus legitimately contains CJK, RTL scripts,
  // ZWSP and emoji. The round trip is only proven at the last step, which is this one.
  await expect(page.getByText('重慶森林')).toBeVisible();
});

test('a person filter keeps the kind partition and can be cleared', async ({ page }) => {
  // Owner decision 18: the filter does NOT suspend the partition — turning both kinds on is
  // how a whole filmography is seen.
  await page.locator('.card-wrap').first().click();
  const panel = page.getByLabel('Title detail');
  await expect(panel).toBeVisible();

  const person = panel.locator('.person').first();
  const name = (await person.locator('.pname').textContent())?.trim();
  await person.click();

  const chip = page.locator('.filters .pill.on');
  await expect(chip).toContainText(name ?? '');
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');

  await chip.click();
  await expect(page.locator('.filters .pill.on')).toHaveCount(0);
});
