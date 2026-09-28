import { expect, test } from '@playwright/test';

import { kindToggle, openTitle, signedIn } from '../helpers.js';

/** §6.0 the catalog; §4.1 rule 5: one kind or both, never neither (decisions 18, 474). */

// The count is the search field's placeholder (decision 528).
const search = (page) => page.getByTestId('home-search');

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  await page.goto('/');
});

test('films only, and the search says how many films the library holds', async ({ page }) => {
  // §6.0 (decision 527): the count is of the kind shown; the kind not shown is not counted.
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'false');
  await expect(search(page)).toHaveAttribute('placeholder', /^Search \d+ films?$/);
});

test('both kinds on shows everything and nothing is reported hidden', async ({ page }) => {
  await kindToggle(page, 'Both').click();
  await expect(kindToggle(page, 'Both')).toHaveAttribute('aria-pressed', 'true');
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'false');
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'false');
  await expect(search(page)).toHaveAttribute('placeholder', /^Search \d+ titles?$/);
});

test('the kind switch selects one kind or both, never neither', async ({ page }) => {
  // Decision 474: Series switches to series; no tap reaches an empty selection (§4.1 rule 5).
  const pressed = async () =>
    (
      await page
        .getByRole('group', { name: 'Kind' })
        .getByRole('button', { pressed: true })
        .allTextContents()
    ).map((t) => t.trim());

  // Polled: `allTextContents` does not wait for the switch to be drawn.
  await expect.poll(pressed).toEqual(['Films']);
  await kindToggle(page, 'Series').click();
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(pressed).toEqual(['Series']);
  await kindToggle(page, 'Series').click(); // the position already held: still Series
  await expect.poll(pressed).toEqual(['Series']);
  await kindToggle(page, 'Both').click();
  await expect(kindToggle(page, 'Both')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(pressed).toEqual(['Both']);
});

test('the API refuses an empty kind selection outright', async ({ page }) => {
  const res = await page.request.get('/api/titles?limit=5', { failOnStatusCode: false });
  expect(res.status(), 'kind is required, not defaulted').toBe(422);
});

test('the facet vocabulary follows the selection', async ({ page }) => {
  // A genre that only exists in the kind switched off must not linger in the control.
  await page.getByTestId('filter-toggle').click();
  const genre = page.getByLabel('Genre');
  const filmGenres = await genre.locator('option').allTextContents();

  await kindToggle(page, 'Both').click();
  await expect(async () => {
    const bothGenres = await genre.locator('option').allTextContents();
    expect(bothGenres.length).toBeGreaterThan(filmGenres.length);
  }).toPass();
});

test('the genre control offers one canonical name per genre', async ({ page }) => {
  // Decision 473: TMDB's genre names; the fixture's "Sci-Fi" is offered as "Science Fiction".
  await kindToggle(page, 'Both').click();
  await page.getByTestId('filter-toggle').click();
  const genre = page.getByLabel('Genre');
  await expect(genre.locator('option', { hasText: 'Science Fiction' })).toHaveCount(1);
  const names = (await genre.locator('option').allTextContents()).map((t) => t.trim().toLowerCase());
  expect(names).not.toContain('sci-fi');
  expect(new Set(names).size).toBe(names.length);
});

test('an exact title is the first search hit', async ({ page }) => {
  // Decision 472: best match first; "heat" also matches "theatre" and unrelated aliases.
  await page.getByLabel('Search titles').fill('heat');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.locator('.grid .card-wrap').first()).toContainText('Heat');
});

test('owned titles are marked in the catalog and one pill narrows to them', async ({ page }) => {
  await page.getByTestId('filter-toggle').click();
  await page.getByTestId('filter-owned').click();
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.getByTestId('filter-owned')).toHaveAttribute('aria-checked', 'true');
  await expect(search(page)).toHaveAttribute('placeholder', /^Search \d+ films? in your library$/);
  const cards = page.locator('.grid .card-wrap');
  await expect(cards.first()).toBeVisible();
  const n = await cards.count();
  await expect(page.locator('.grid [data-testid="owned-chip"]')).toHaveCount(n);
  await page.getByTestId('filter-toggle').click();
  await expect(page.getByTestId('filter-toggle')).toHaveText('Filters · 1');
  await expect(page.getByTestId('owned-filter-chip')).toBeVisible();
});

test('search matches an alias, not just the title', async ({ page }) => {
  // The fixture's CJK title carries its English name only as an alias.
  await page.getByLabel('Search titles').fill('chungking');
  await expect(page.locator('.grid .card-wrap').first()).toBeVisible();
});

test('a query with no matches says so instead of showing an empty grid', async ({ page }) => {
  await page.getByLabel('Search titles').fill('zzzzzzzz');
  await expect(page.getByRole('heading', { name: 'No matches' })).toBeVisible();
  await expect(page.locator('.grid .card-wrap')).toHaveCount(0);
});

test('a search that finds only the other kind says where, and switches there', async ({ page }) => {
  // Decision 516: a match hidden by the kind switch is named, not reported as no match.
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');
  await page.getByLabel('Search titles').fill('severance');
  await expect(page.getByTestId('found-elsewhere')).toHaveText('Found in Series: Severance');
  await expect(page.getByRole('heading', { name: 'No matches' })).toHaveCount(0);
  await page.getByTestId('found-elsewhere-switch').click();
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.grid .card-wrap').first()).toContainText('Severance');
});

test('a search keeps its close matches in view and folds the looser ones', async ({ page }) => {
  // Decision 516: "p" starts Prisoners and Paddington 2 and only sits inside Tampopo.
  await page.getByLabel('Search titles').fill('p');
  const card = (name) => page.locator('.grid .card-wrap', { hasText: name });
  await expect(card('Prisoners')).toBeVisible();
  await expect(card('Paddington 2')).toBeVisible();
  const more = page.getByTestId('weak-matches-toggle');
  await expect(more).toHaveText(/^Show \d+ looser match(es)?$/);
  await expect(card('Tampopo')).toHaveCount(0);
  await more.click();
  await expect(page.getByTestId('weak-matches-head')).toBeVisible();
  await expect(card('Tampopo')).toBeVisible();
});

test('a filtered grid names the order it is in, with the other one tap away', async ({ page }) => {
  // Without a fitted score the server answers newest whatever is asked (`for_you_available`),
  // and the page must then offer no "For you" that does nothing.
  const probe = await (
    await page.request.get('/api/titles?kind=movie&owned_only=true&limit=1')
  ).json();
  await page.getByTestId('filter-toggle').click();
  await page.getByTestId('filter-owned').click();
  const order = page.getByRole('group', { name: 'Order' });
  if (probe.for_you_available) {
    await expect(order).toBeVisible();
    await expect(order.getByRole('button', { pressed: true })).toHaveCount(1);
    const newest = page.getByTestId('sort-newest');
    await newest.click();
    await expect(newest).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('sort-for_you')).toHaveAttribute('aria-pressed', 'false');
  } else {
    await expect(page.getByTestId('sort-waiting')).toHaveText(/^Newest first\./);
    await expect(order).toHaveCount(0);
  }
  // A search is best match first and offers no other order.
  await page.getByLabel('Search titles').fill('heat');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-reason', 'search');
  await expect(order).toHaveCount(0);
});

test('non-ASCII titles survive to the screen', async ({ page }) => {
  // §4.1 rule 8: never "clean" non-ASCII. Searched in its own script, so the query round-trips too.
  await page.getByRole('searchbox', { name: 'Search titles' }).fill('重慶');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.getByText('重慶森林')).toBeVisible();
});

test('a person filter keeps the kind partition and can be cleared', async ({ page }) => {
  // Decision 18: the filter does NOT suspend the partition. Heat, because the fixture's
  // Paddington 2 has no credits.
  const panel = await openTitle(page, 'Heat', { ensureKinds: ['Films'] });

  const person = panel.locator('.person').first();
  const name = (await person.locator('.pname').textContent())?.trim();
  await person.click();

  const chip = page.getByTestId('person-chip');
  await expect(chip).toContainText(name ?? '');
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');

  await chip.click();
  await expect(chip).toHaveCount(0);
});

test('the no-matches state names only controls that exist', async ({ page }) => {
  // No dead ends: no route searches DNA and the Map is unbuilt. Both halves are asserted, because
  // an empty state that names nothing would pass the ban alone.
  await page.getByLabel('Search titles').fill('zzzzzzzz');
  await expect(page.getByRole('heading', { name: 'No matches' })).toBeVisible();

  const help = page.getByTestId('no-matches-help');
  await expect(help).toBeVisible();
  const copy = ((await help.textContent()) ?? '').toLowerCase();

  expect(copy, 'the empty state points at DNA search, which no route implements').not.toMatch(
    /\bdna\b/
  );
  expect(copy, 'the empty state points at the Map, which renders M6 placeholder').not.toMatch(
    /\bmap\b/
  );

  for (const dimension of ['alias', 'kind', 'genre', 'decade', 'seen']) {
    expect(copy, `the empty state does not name ${dimension}`).toContain(dimension);
  }

  // And every control it names is on this screen (decision 516).
  await expect(page.getByRole('group', { name: 'Kind' })).toBeVisible();
  await page.getByTestId('filter-toggle').click();
  await expect(page.getByTestId('filter-genre')).toBeVisible();
  await expect(page.getByTestId('filter-decade')).toBeVisible();
  await expect(page.getByTestId('filter-seen')).toBeVisible();
});
