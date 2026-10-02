import { expect, test } from '@playwright/test';

import { openTitle, signedIn } from '../helpers.js';

/**
 * Like these films and More like this, but... (decisions 559 and 560): Home's recipe, its chips,
 * a film's group sheet and the limits. The fixture puts each DNA term on one title, so every recipe
 * here is thin or empty and no twist is ever offered; ranking is `backend/tests/test_mix*.py`'s.
 * The recipe lives in Home's URL alone, so nothing here writes and both projects share the stack.
 * Back to the card a jump came from is 26-library-card-taps'.
 */

const HEAT = 1;
const PRISONERS = 2;

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

const chips = (page) => page.getByTestId('recipe-chip');
const chipBody = (page, label) => page.getByRole('button', { name: new RegExp(`^${label}[.:]`) });
const sheetOf = (page, name) => page.getByRole('dialog', { name: `${name} in your recipe` });

/** Like these films: the cell's own field from 721 px, a row that opens the picker on a phone. */
async function likeFilm(page, isMobile, name, sign = 'Like') {
  if (isMobile) await page.getByTestId('filter-like').click();
  await page.getByTestId('film-search').fill(name);
  const row = page.getByTestId('film-row').filter({ hasText: name }).first();
  await row.getByRole('button', { name: sign, exact: true }).click();
  await expect(row.getByRole('button', { name: sign, exact: true })).toHaveAttribute('aria-pressed', 'true');
  if (isMobile) {
    await page.getByRole('dialog', { name: 'Like these films' }).getByRole('button', { name: 'Done' }).click();
  } else {
    await page.keyboard.press('Escape');
  }
}

test("More like this, but... on Home's card starts a recipe with the Filters open", async ({ page }) => {
  const panel = await openTitle(page, 'Heat', { ensureKinds: ['Films'] });
  await panel.getByRole('button', { name: /^More like this, but/ }).click();
  await expect(page.getByRole('dialog', { name: 'Title detail' })).toHaveCount(0);

  const mode = page.getByTestId('home-mode');
  await expect(mode).toHaveAttribute('data-reason', 'recipe');
  await expect(chips(page)).toHaveCount(1);
  await expect(chipBody(page, 'Like Heat')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`[?&]like=${HEAT}(&|$)`));
  await expect(page).toHaveURL(/[?&]filters=open/);
  await expect(page.getByTestId('filter-like')).toBeVisible();
  // The search that found Heat does not narrow Heat's own recipe.
  await expect(page.getByTestId('home-search')).toHaveValue('');
});

test("a recipe's films, a film's groups and the limits", async ({ page, isMobile }) => {
  await page.goto(`/?like=${HEAT}&kind=movie&filters=open`);
  await expect(chipBody(page, 'Like Heat')).toBeVisible();

  // The fixture's terms each sit on one title: nothing shares two with Heat, and no twist fits.
  await expect(page.getByTestId('recipe-count').first()).toHaveText(/in your library fits?$/);
  await expect(page.getByTestId('twist-row')).toHaveCount(0);

  await likeFilm(page, isMobile, 'Prisoners');
  await expect(chips(page)).toHaveCount(2);
  await expect(page).toHaveURL(new RegExp(`[?&]like=${PRISONERS}(&|$)`));

  // Prisoners' mood is two quoted terms; its storytelling one guess; the rest nothing.
  await chipBody(page, 'Like Prisoners').click();
  const sheet = sheetOf(page, 'Prisoners');
  await expect(sheet).toBeVisible();
  const mood = sheet.getByRole('checkbox', { name: /^Mood/ });
  await expect(mood).toHaveAttribute('aria-disabled', 'false');
  await expect(mood).toContainText('dread, bleak');
  const story = sheet.getByRole('checkbox', { name: /^Storytelling/ });
  await expect(story).toHaveAttribute('aria-disabled', 'true');
  await expect(story).toContainText('Only one term here: procedural');
  await expect(sheet.getByRole('checkbox', { name: /^Sound/ })).toHaveAttribute('aria-disabled', 'true');

  await mood.click();
  await expect(sheet.getByTestId('recipe-sheet-label')).toHaveText('Mood like Prisoners');
  await expect(sheet.getByTestId('recipe-sheet-note')).toHaveText('Heat keeps everything but its own mood.');
  await sheet.getByRole('button', { name: 'Apply' }).click();
  await expect(sheet).toHaveCount(0);
  await expect(chipBody(page, 'Mood like Prisoners')).toBeVisible();
  await expect(page.getByTestId('recipe-sentence')).toHaveText("Heat, with Prisoners' mood in place of its own");
  await expect(page).toHaveURL(new RegExp(`[?&]like=${PRISONERS}:mood(&|$)`));

  // Less like keeps the group and moves the film to the other side.
  await chipBody(page, 'Mood like Prisoners').click();
  await sheet.getByRole('button', { name: 'Less like' }).click();
  await expect(sheet.getByTestId('recipe-sheet-label')).toHaveText('Mood less like Prisoners');
  await sheet.getByRole('button', { name: 'Apply' }).click();
  await expect(chipBody(page, 'Mood less like Prisoners')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`[?&]less=${PRISONERS}:mood(&|$)`));

  // Taking every film out returns the shelves.
  for (let n = 2; n > 0; n--) {
    await chips(page).first().getByRole('button', { name: /^Remove / }).click();
    await expect(chips(page)).toHaveCount(n - 1);
  }
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
});

test('a fifth film is refused, with its reason', async ({ page, isMobile }) => {
  // The fixture has four titles a recipe can take (two or more terms each); all four fill it.
  const pickable = [];
  for (const name of ['Heat', 'Prisoners', 'Severance', 'Tampopo']) {
    const res = await page.request.get(`/api/mix/films?q=${name}&limit=8`);
    expect(res.ok(), `the title picker answers for ${name}`).toBeTruthy();
    pickable.push((await res.json()).items.find((t) => t.name === name)?.id);
  }
  expect(pickable.every(Boolean), `the picker offers all four: ${pickable}`).toBeTruthy();
  await page.goto(`/?${pickable.map((id) => `like=${id}`).join('&')}&kind=movie&filters=open`);
  await expect(chips(page)).toHaveCount(4);

  if (isMobile) await page.getByTestId('filter-like').click();
  await page.getByTestId('film-search').fill('heat');
  await expect(page.getByTestId('film-full')).toHaveText(
    'A recipe takes four films at most. Remove one to add another.'
  );
});

test('at 1024x768 the chips, the sheet and the picker stay inside the page', async ({ page, isMobile }) => {
  test.skip(isMobile, 'the phone pass has its own width');
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto(`/?like=${HEAT}&less=${PRISONERS}&kind=movie&filters=open`);
  await expect(chips(page)).toHaveCount(2);

  await chipBody(page, 'Less like Prisoners').click();
  const sheet = sheetOf(page, 'Prisoners');
  const box = await sheet.boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(1024);
  expect(box.y + box.height).toBeLessThanOrEqual(768);
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveCount(0);

  await page.getByTestId('film-search').fill('heat');
  await expect(page.getByRole('dialog', { name: 'Films to like or less like' })).toBeVisible();
  const across = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(across).toBeLessThanOrEqual(1);
});
