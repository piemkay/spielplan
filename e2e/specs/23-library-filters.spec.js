import { expect, test } from '@playwright/test';

import { signedIn, waitForBoard } from '../helpers.js';

/**
 * Decision 557's filters on the fixture vocabulary, read-only: a term found by its alias and
 * included, a leave-out, a person, the quoted-first fold, an empty grid's drop and Rank's term
 * picker. Each fixture term sits on one or two titles (`make_bundle.py`'s EXTRACTED and PROJECTED),
 * and after 08's sync the two Chungking titles are beyond the library.
 */

const cards = (page) => page.locator('.grid .card-wrap');
const card = (page, name) => page.locator('.grid .card-wrap', { hasText: name });

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  await page.goto('/');
});

/** The Filters open, then the term picker: its cell's field and a popover on a desktop, a sheet on a phone. */
async function openTermPicker(page) {
  const toggle = page.getByTestId('filter-toggle');
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
  await page.getByTestId('filter-terms').click();
  const picker = page.getByRole('dialog', { name: "What it's like" });
  await expect(picker).toBeVisible();
  return picker;
}

/** Done on a phone's sheet, Escape on a desktop's popover; the address is mirrored once it shuts. */
async function closePicker(page, picker, isMobile) {
  if (isMobile) await picker.getByRole('button', { name: 'Done' }).click();
  else await page.keyboard.press('Escape');
  await expect(picker).toHaveCount(0);
}

test('includes a term found by its alias, and a tap on its chip leaves it out', async ({ page, isMobile }) => {
  const picker = await openTermPicker(page);
  await page.getByTestId('term-search').fill('cozy');
  // The vocabulary's label is "cosy"; the row names the alias that found it.
  await expect(picker.getByTestId('term-row').filter({ hasText: 'via cozy' })).toBeVisible();
  await picker.getByRole('button', { name: 'Include cosy' }).click();
  await closePicker(page, picker, isMobile);

  const chip = page.getByTestId('term-chip');
  await expect(chip).toHaveAttribute('data-mode', 'in');
  await expect(page).toHaveURL(/[?&]term=mood\.cosy(&|$)/);
  await expect(card(page, 'Paddington 2')).toBeVisible();
  await expect(card(page, 'Heat')).toHaveCount(0);

  await chip.getByRole('button', { name: 'Switch cosy to leave out' }).click();
  await expect(chip).toHaveAttribute('data-mode', 'out');
  await expect(page).toHaveURL(/[?&]not_term=mood\.cosy(&|$)/);
  await expect(card(page, 'Heat')).toBeVisible();
  await expect(card(page, 'Paddington 2')).toHaveCount(0);
});

test('a leave-out reads our read too, as a veto does', async ({ page, isMobile }) => {
  // Heat carries period by our read alone, and leaving period out still drops it (decision 557 item 3).
  const picker = await openTermPicker(page);
  await page.getByTestId('term-search').fill('period');
  await picker.getByRole('button', { name: 'Leave out period' }).click();
  await closePicker(page, picker, isMobile);
  await expect(page.getByTestId('term-chip')).toHaveAttribute('data-mode', 'out');
  await expect(card(page, 'Prisoners')).toBeVisible();
  await expect(card(page, 'Heat')).toHaveCount(0);
});

test('adds a person from People, and the grid is their work in the library', async ({ page, isMobile }) => {
  await page.getByTestId('filter-toggle').click();
  await page.getByTestId('filter-people').click();
  // On a desktop the cell is the field, and what a name finds drops under it.
  await page.getByTestId('people-search').fill('vill');
  const picker = page.getByRole('dialog', { name: 'People' });
  await picker.getByRole('button', { name: 'Add Denis Villeneuve' }).click();
  await closePicker(page, picker, isMobile);

  await expect(page.getByTestId('person-chip')).toContainText('Denis Villeneuve');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-reason', 'person');
  await expect(card(page, 'Prisoners')).toBeVisible();
  await expect(cards(page)).toHaveCount(1);
  await expect(page).toHaveURL(/[?&]person=\d+(&|$)/);
});

test('leads an include with its quoted matches and folds the rest behind one row', async ({ page }) => {
  // Neon is quoted on Chungking Express and our read alone on its CJK twin; both are beyond the
  // library, so the address switches Only in library off (decision 558).
  await page.goto('/?term=visual.neon&kind=movie&owned=off');
  await expect(page.getByTestId('owned-filter-chip')).toBeVisible();
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page).first()).toContainText('Chungking Express');
  const fold = page.getByTestId('weak-matches-toggle');
  await expect(fold).toHaveText('Show 1 more that might fit');
  await fold.click();
  await expect(page.getByTestId('weak-matches-head')).toContainText('Might also fit');
  await expect(page.getByTestId('weak-matches-head')).toContainText('Our read · less certain');
  await expect(cards(page)).toHaveCount(2);
});

test("names an empty grid's chips and drops one with what that leaves", async ({ page }) => {
  // Cosy is Paddington 2's and obsession Heat's: nothing in the library carries both.
  await page.goto('/?term=mood.cosy&term=themes.obsession&kind=movie');
  await expect(page.getByTestId('no-matches-line')).toHaveText(
    'Nothing in your library is cosy and obsession.'
  );
  const drops = page.getByTestId('grid-drop');
  await expect(drops.filter({ hasText: 'Without cosy' })).toHaveText('Without cosy: 1 film');
  const drop = drops.filter({ hasText: 'Without obsession' });
  await expect(drop).toHaveText('Without obsession: 1 film');
  await drop.click();
  await expect(card(page, 'Paddington 2')).toBeVisible();
  await expect(page.getByTestId('term-chip')).toHaveCount(1);
  await expect(page).toHaveURL(/\?term=mood\.cosy&kind=movie$/);
});

test('the term picker hangs under its field at 1024x768 too', async ({ page, isMobile }) => {
  test.skip(isMobile, 'a phone opens it as a sheet');
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('/');
  const picker = await openTermPicker(page);
  const [cell, box] = await Promise.all([page.getByTestId('filter-terms').boundingBox(), picker.boundingBox()]);
  expect(box.y, 'the popover covers its field').toBeGreaterThanOrEqual(cell.y + cell.height);
  expect(box.x + box.width, 'the popover leaves the screen').toBeLessThanOrEqual(1024 + 1);
  expect(box.y + box.height, 'the popover runs past the screen').toBeLessThanOrEqual(768 + 1);
  await page.getByTestId('term-search').fill('dread');
  await picker.getByRole('button', { name: 'Include dread' }).click();
  await closePicker(page, picker, false);
  await expect(card(page, 'Prisoners')).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("Rank's Filters include a term, and the board says our read admitted the survivor", async ({
  page,
  isMobile
}) => {
  // The admin's board holds Heat (10-home's set-up), and period is Heat's by our read alone.
  await waitForBoard(page);
  await page.goto('/rank');
  await page.getByTestId('rank-filters').click();
  const filters = page.getByRole('dialog', { name: 'Filters' });
  await filters.getByTestId('rank-terms').click();
  // A sheet over the Filters on a phone; on a desktop the terms list under the row (board B8).
  const picker = isMobile ? page.getByRole('dialog', { name: "What it's like" }) : filters;
  await expect(picker).toBeVisible();
  await expect(page.getByRole('dialog', { name: "What it's like" })).toHaveCount(isMobile ? 1 : 0);
  await picker.getByTestId('term-search').fill('period');
  const read = page.waitForResponse(
    (res) => res.url().includes('/api/rank?') && res.url().includes('term=era.period')
  );
  await picker.getByRole('button', { name: 'Include period' }).click();
  await read;
  if (isMobile) {
    await picker.getByRole('button', { name: 'Done' }).click();
    await expect(picker).toHaveCount(0);
  }
  await expect(filters.getByTestId('rank-sheet-term-chip')).toHaveAttribute('data-mode', 'in');
  await filters.getByRole('button', { name: 'Done' }).click();
  await expect(filters).toHaveCount(0);

  await expect(page.getByTestId('rank-term-chip')).toHaveAttribute('data-mode', 'in');
  await expect(page.getByTestId('rank-tier-legend')).toHaveText('Our read says period; no review does');
  const tiles = page.getByTestId('rank-board').locator('[data-title]');
  await expect(tiles).toHaveCount(1);
  await expect(tiles.first()).toHaveAttribute('aria-label', /^Heat, 1995, .*period by our read$/);
});
