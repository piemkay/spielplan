import { expect, test } from '@playwright/test';

import { kindToggle, signedIn } from '../helpers.js';

/**
 * Decision 558: Only in library is on by default; switched off, a search answers in three sections,
 * In your library, More in Spielplan and From TMDB, and a TMDB-only title opens a short card whose
 * Want it mints a title for the wish. The fake TMDB (`ops/fake_tmdb.py`) holds two films Spielplan
 * does not: the desktop pass wants one and the phone pass the other, because a want leaves its
 * minted title behind (it then shows under More in Spielplan, by design). Each pass takes its want
 * back. The key stays set: the admin route keeps a saved key when it is sent empty.
 */

const FILMS = {
  desktop: {
    tmdbId: 910001, name: 'Harbour Lights', query: 'harbour', year: '2024', genres: 'Drama, romance'
  },
  phone: {
    tmdbId: 910002, name: 'Glass Orchard', query: 'glass', year: '2025', genres: 'Science fiction, thriller'
  }
};

const search = (page) => page.getByTestId('home-search');

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  const saved = await page.request.put('/api/admin/connectors/tmdb', { data: { api_key: 'e2e-tmdb-key' } });
  expect(saved.ok(), 'the admin saves the household TMDB key (decision 452)').toBeTruthy();
  await page.goto('/');
});

/** Only in library off, through its switch in Filters, from its default of on. */
async function beyondTheLibrary(page) {
  await page.getByTestId('filter-toggle').click();
  const owned = page.getByTestId('filter-owned');
  await expect(owned, 'Only in library is on by default').toHaveAttribute('aria-checked', 'true');
  await owned.click();
  await expect(owned).toHaveAttribute('aria-checked', 'false');
  await page.getByTestId('filter-toggle').click();
  await expect(page.getByTestId('owned-filter-chip')).toContainText('Beyond your library');
}

async function mine(page) {
  const res = await page.request.get('/api/wish');
  expect(res.ok()).toBeTruthy();
  return (await res.json()).mine;
}

test('a search beyond the library answers in three sections', async ({ page, isMobile }) => {
  const film = FILMS[isMobile ? 'phone' : 'desktop'];
  await expect(search(page)).toHaveAttribute('placeholder', /^Search your \d+ films?$/);
  await beyondTheLibrary(page);
  await expect(search(page)).toHaveAttribute('placeholder', 'Search all films');

  await search(page).fill(film.query);
  await expect(page.getByTestId('library-section-head')).toHaveText('In your library');
  const catalogue = page.getByTestId('beyond-catalogue');
  await expect(catalogue.getByRole('heading', { name: 'More in Spielplan' })).toBeVisible();
  await expect(catalogue).toContainText('Not in your library yet');
  const tmdb = page.getByTestId('beyond-tmdb');
  await expect(tmdb.getByRole('heading', { name: 'From TMDB' })).toBeVisible();
  await expect(tmdb).toContainText("Spielplan doesn't know these films yet");
  const hit = tmdb.getByTestId('tmdb-hit').filter({ hasText: film.name });
  await expect(hit).toHaveCount(1);
  await expect(hit).toContainText(`${film.year} · TMDB`);

  // Two letters ask TMDB nothing, so the section goes.
  await search(page).fill(film.query.slice(0, 2));
  await expect(page.getByTestId('beyond-tmdb')).toHaveCount(0);
  await expect(page.getByTestId('beyond-catalogue')).toBeVisible();
});

test('TMDB titles sit two to a line at 1024x768, and the page does not widen', async ({ page, isMobile }) => {
  test.skip(isMobile, 'a phone lists them in one column');
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('/');
  await kindToggle(page, 'Both').click();
  await expect(kindToggle(page, 'Both')).toHaveAttribute('aria-pressed', 'true');
  await beyondTheLibrary(page);
  // Before either pass has wanted it: "lights" finds Harbour Lights and the series Northern Lights.
  await search(page).fill('lights');
  const tmdb = page.getByTestId('beyond-tmdb');
  await expect(tmdb).toContainText("Spielplan doesn't know these titles yet");
  const hits = tmdb.getByTestId('tmdb-hit');
  await expect(hits.filter({ hasText: 'Northern Lights' })).toHaveCount(1);
  await expect(hits.filter({ hasText: 'Harbour Lights' })).toHaveCount(1);
  const [first, second] = [await hits.nth(0).boundingBox(), await hits.nth(1).boundingBox()];
  expect(Math.abs(first.y - second.y), 'two TMDB titles to a line').toBeLessThan(2);
  expect(second.x).toBeGreaterThan(first.x + first.width);
  const across = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(across, 'the document scrolls sideways').toBeLessThanOrEqual(1);
});

test('a TMDB title opens the short card, and Want it puts it on the wish list', async ({
  page,
  isMobile
}) => {
  const film = FILMS[isMobile ? 'phone' : 'desktop'];
  await beyondTheLibrary(page);
  await search(page).fill(film.query);
  const hit = page.getByTestId('beyond-tmdb').getByTestId('tmdb-hit').filter({ hasText: film.name });
  // The row's name opens the card; its other button is "Want {name}".
  await hit.getByRole('button', { name: new RegExp(`^${film.name}`) }).click();

  const card = page.getByRole('dialog', { name: film.name });
  await expect(card.getByRole('heading', { name: film.name })).toBeVisible();
  await expect(card.getByTestId('tmdb-card-genres')).toHaveText(film.genres);
  await expect(card).toContainText('Not in Spielplan yet');
  await expect(card).toContainText('Details from TMDB');
  await expect(card.getByTestId('tmdb-link')).toHaveAttribute(
    'href',
    `https://www.themoviedb.org/movie/${film.tmdbId}`
  );
  // No taste, ranking or seen rows: Want it is the one action.
  for (const id of ['title-watched', 'title-not-seen', 'rank-card-tier', 'title-not-for-me', 'title-more']) {
    await expect(card.getByTestId(id), id).toHaveCount(0);
  }
  const want = card.getByTestId('tmdb-want');
  await expect(want).toHaveText('Want it');
  await want.click();
  await expect(want).toHaveText('On the wish list');
  await expect(want).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(async () => (await mine(page)).map((item) => item.name)).toContain(film.name);

  await page.keyboard.press('Escape');
  await expect(card).toBeHidden();
  await expect(hit.getByTestId('tmdb-hit-want')).toHaveText('On the wish list');

  // Clearing the search switches Only in library back on, and the shelves return.
  await search(page).fill('');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(search(page)).toHaveAttribute('placeholder', /^Search your \d+ films?$/);
  await expect(page.getByTestId('owned-filter-chip')).toHaveCount(0);

  // The wish list holds it.
  await page.getByTestId('home-wish-open').click();
  const list = page.getByRole('dialog', { name: 'Wish list' });
  await expect(list.getByTestId('wish-item').filter({ hasText: film.name })).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(list).toBeHidden();
});

test('the wished title shows under More in Spielplan and keeps the short card', async ({
  page,
  isMobile
}) => {
  const film = FILMS[isMobile ? 'phone' : 'desktop'];
  await beyondTheLibrary(page);
  await search(page).fill(film.query);
  // Spielplan holds it now, so TMDB's one hit for the query is dropped and the catalogue lists it.
  const catalogue = page.getByTestId('beyond-catalogue');
  const poster = catalogue.locator('.card-wrap', { hasText: film.name });
  await expect(poster).toHaveCount(1);
  await expect(page.getByTestId('beyond-tmdb')).toContainText('Nothing else on TMDB matches.');
  await expect(page.getByTestId('beyond-tmdb').getByTestId('tmdb-hit')).toHaveCount(0);

  await poster.click();
  const card = page.getByRole('dialog', { name: 'Title detail' });
  await expect(card.getByTestId('tmdb-card')).toBeVisible();
  await expect(card.getByTestId('title-watched')).toHaveCount(0);
  const want = card.getByTestId('tmdb-want');
  await expect(want).toHaveText('On the wish list');

  // Put back: the want goes, the minted title stays (decision 558).
  await want.click();
  await expect(want).toHaveText('Want it');
  await expect.poll(async () => (await mine(page)).map((item) => item.name)).not.toContain(film.name);
  await page.keyboard.press('Escape');
  await expect(card).toBeHidden();
  await search(page).fill('');
  await expect(search(page)).toHaveAttribute('placeholder', /^Search your \d+ films?$/);
});
