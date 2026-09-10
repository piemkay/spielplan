import { expect, test } from '@playwright/test';

import { kindToggle, openTitle, signedIn } from '../helpers.js';

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
  //
  // §6.0 M2 made shelves Home's default surface, so the catalog is no longer on screen at load
  // and the title has to be asked for. Asking for it in its own script is the stronger test
  // anyway: the query makes the same round trip as the row, through a different code path.
  await page.getByRole('searchbox', { name: 'Search titles' }).fill('重慶');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.getByText('重慶森林')).toBeVisible();
});

test('a person filter keeps the kind partition and can be cleared', async ({ page }) => {
  // Owner decision 18: the filter does NOT suspend the partition — turning both kinds on is
  // how a whole filmography is seen.
  // Heat by name: the first card in the grid is the newest film, Paddington 2, which carries
  // no credits in the fixture — so `.person` would never appear and the timeout would look
  // like a broken person filter.
  const panel = await openTitle(page, 'Heat', { ensureKinds: ['Films'] });

  const person = panel.locator('.person').first();
  const name = (await person.locator('.pname').textContent())?.trim();
  await person.click();

  const chip = page.locator('.filters .pill.on');
  await expect(chip).toContainText(name ?? '');
  await expect(kindToggle(page, 'Films')).toHaveAttribute('aria-pressed', 'true');

  await chip.click();
  await expect(page.locator('.filters .pill.on')).toHaveCount(0);
});

test('the no-matches state names only controls that exist', async ({ page }) => {
  // §6.0 + M4.9 finding 19. The empty state read "try a DNA term … or check the Map's
  // compositional search" — proposal 23's copy, adopted from the prototype ahead of the routes
  // it describes. `list_titles` has no `dna` parameter at all and its search predicate is
  // `lower(t.name) LIKE` or the same over `title_alias`; the Map is §6.4 and renders M6's
  // placeholder. The one screen whose job is to rescue a failed search was sending people to
  // two dead ends, which is worse than saying nothing.
  //
  // Both halves are asserted, because either alone is satisfiable by the wrong copy: the
  // absent controls must not be named, AND every dimension `list_titles` really has must be —
  // an empty state that names nothing would pass a ban and teach nobody anything.
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

  // The dimensions §6.0's M0 catalog really has, in the order the copy walks them. `alias` is
  // named because it is the half of the search predicate a person cannot guess: the fixture's
  // CJK title is reachable only by its English alias, and nothing else on the screen says so.
  for (const dimension of ['alias', 'kind', 'genre', 'decade', 'seen']) {
    expect(copy, `the empty state does not name ${dimension}`).toContain(dimension);
  }

  // And every control it names is on the screen it is naming them from. A rescue line that
  // sends someone to a control this build does not ship is the defect, not the wording.
  await expect(page.getByRole('group', { name: 'Kind' })).toBeVisible();
  await expect(page.getByTestId('filter-genre')).toBeVisible();
  await expect(page.getByTestId('filter-decade')).toBeVisible();
  await expect(page.getByTestId('filter-seen')).toBeVisible();
});
