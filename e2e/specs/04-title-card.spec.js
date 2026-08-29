import { expect, test } from '@playwright/test';

import { openTitle, signedIn } from '../helpers.js';

/**
 * §6.0's title detail card, and the §4.1 rules that are only observable at the last step —
 * the two DNA tiers staying distinguishable, credits deduped at read time, and platform
 * scores carrying their display-only caption.
 */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  const config = await (await page.request.get('/api/config')).json();
  test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
  // By name. Clicking the first card opens whatever the grid shows before the debounced
  // search lands — the catalog is ordered by year descending, so that is Paddington 2, and
  // every Heat-specific assertion below then fails for a reason that has nothing to do with
  // what it is testing.
  await openTitle(page, 'Heat');
});

test('the card carries metadata, overview and the model line', async ({ page }) => {
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('heading', { name: 'Heat' })).toBeVisible();
  await expect(panel.locator('.sub')).toContainText('1995');
  await expect(panel.locator('.sub')).toContainText('movie');
  // §6.0: the model line is in the data voice and is NOT gated by the show-the-model toggle
  // (decision 117) — it is the M0 transparency promise.
  await expect(panel.locator('.modelline')).toContainText(/bundle test-v1|model line unavailable/);
});

test('both actions are present, and a missing one is disabled rather than absent', async ({
  page,
}) => {
  // §6.0 requires two actions. The prototype shipped only "Show on map"; a missing Jellyfin
  // link must read as "not configured", not as "this film cannot be played".
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('button', { name: 'Play on Jellyfin' })).toBeDisabled();
  await expect(panel.getByRole('link', { name: 'Show on map' })).toBeVisible();
});

test('the two DNA tiers are visibly distinct and a shared term appears in both', async ({
  page,
}) => {
  // §4.1 rule 1: "14,181 (title,term) pairs exist in both and must stay distinguishable."
  // The fixture reproduces that overlap in miniature; this is where it becomes visible.
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByText('DNA — EXTRACTED')).toBeVisible();
  await expect(panel.getByText('DNA — PROJECTED (INFERRED)')).toBeVisible();

  const extracted = panel.locator('.tag .term');
  const projected = panel.locator('.chip');
  await expect(extracted.filter({ hasText: 'themes.obsession' })).toBeVisible();
  await expect(projected.filter({ hasText: 'themes.obsession' })).toBeVisible();
});

test('every extracted tag shows its evidence quote and source', async ({ page }) => {
  // §4.1 rule 1: "a tag without its quote is unfalsifiable."
  const panel = page.getByLabel('Title detail');
  const tags = panel.locator('.tag');
  await expect(tags.first()).toBeVisible();

  for (const tag of await tags.all()) {
    const quotes = tag.locator('.quote');
    await expect(quotes.first()).toBeVisible();
    // The json-codec bug rendered ~80 empty quotes per tag; a non-empty first quote and a
    // sane count are what would have caught it.
    await expect(quotes.first()).not.toHaveText('“”');
    expect(await quotes.count()).toBeLessThan(6);
    await expect(tag.locator('.src').first()).toContainText(/:/); // e.g. trakt:comment
  }
});

test('salience is shown, and nothing is filtered by it', async ({ page }) => {
  // §4.1 rule 2: weights, never filters. Salience is visible next to the tag it weights.
  const panel = page.getByLabel('Title detail');
  await expect(panel.locator('.tag').first().getByText(/sal [123]/)).toBeVisible();
});

test('credits are deduped at read time and cite their sources', async ({ page }) => {
  // §4.1: "credit (dedupe at read time, never at import)". The fixture stores the director
  // twice, from tmdb and omdb; the card must show one row that says so.
  const panel = page.getByLabel('Title detail');
  const director = panel.locator('.person', { hasText: 'Michael Mann' });
  await expect(director).toHaveCount(1);
  await expect(director).toContainText('2 sources');
});

test('platform scores travel with their display-only caption', async ({ page }) => {
  // §4.1 rule 3: aggregate platform scores are a popularity conduit and are banned as model
  // features. The caption is the only thing stopping a reader assuming otherwise.
  const panel = page.getByLabel('Title detail');
  await expect(panel.locator('.scores')).toBeVisible();
  await expect(panel.getByText(/display-only schema.*never model features/)).toBeVisible();
});

test('tapping a second poster re-fetches instead of showing the first', async ({ page }) => {
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('heading', { name: 'Heat' })).toBeVisible();

  await page.getByLabel('Search titles').fill('');
  await page.locator('.card-wrap').filter({ hasText: 'Prisoners' }).click();
  await expect(panel.getByRole('heading', { name: 'Prisoners' })).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Heat' })).toHaveCount(0);
});
