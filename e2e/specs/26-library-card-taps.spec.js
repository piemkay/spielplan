import { expect, test } from '@playwright/test';

import { signedIn, waitForBoard } from '../helpers.js';

/**
 * Decisions 557 item 6 and 559 item 7: a credit or More like this, but... tapped on a card opened
 * outside Home opens Home's grid with that chip alone, its state in Home's URL, and Back returns to
 * the card it was tapped on. The admin's board is 10-home's set-up, refitted by now. Reads only, so
 * the phone pass runs on the same stack.
 */

const card = (page) => page.getByRole('dialog', { name: 'Title detail' });

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  await waitForBoard(page);
});

/** Heat's card, opened from Rank's board. */
async function openHeatOnRank(page) {
  const board = await (await page.request.get('/api/rank?kind=movie')).json();
  const heat = board.tiers.flatMap((tier) => tier.entries).find((entry) => entry.name === 'Heat');
  expect(heat, "Heat is not on the admin's board - 10-home places it in the set-up").toBeTruthy();
  await page.goto('/rank');
  await page.getByTestId(`rank-open-${heat.title_id}`).click();
  await expect(card(page).getByRole('heading', { name: 'Heat' })).toBeVisible();
  return card(page);
}

/** Back from Home's grid: Rank again, with Heat's card open over it, and one more Back closes it. */
async function backToTheCard(page) {
  await page.goBack();
  await expect(page).toHaveURL(/\/rank$/);
  await expect(card(page).getByRole('heading', { name: 'Heat' })).toBeVisible();
  await page.goBack();
  await expect(card(page)).toHaveCount(0);
  await expect(page).toHaveURL(/\/rank$/);
}

test("a credit on Rank's card opens Home's grid with that person alone, and Back reopens the card", async ({
  page
}) => {
  const panel = await openHeatOnRank(page);
  await panel.locator('.person', { hasText: 'Michael Mann' }).click();

  // A person spans both kinds, so the jump lists both.
  await expect(page).toHaveURL(/\/\?person=\d+(,\d+)*&kind=movie&kind=series/);
  await expect(card(page)).toHaveCount(0);
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.getByTestId('person-chip')).toHaveCount(1);
  await expect(page.getByTestId('person-chip')).toContainText('Michael Mann');
  await expect(page.getByTestId('term-chip')).toHaveCount(0);

  await backToTheCard(page);
});

test("More like this, but... on Rank's card starts a recipe of it on Home, and Back reopens the card", async ({
  page
}) => {
  const panel = await openHeatOnRank(page);
  await panel.getByTestId('title-like-more').click();

  await expect(page).toHaveURL(/\/\?like=\d+&kind=movie&filters=open/);
  await expect(card(page)).toHaveCount(0);
  await expect(page.getByTestId('filter-panel')).toBeVisible();
  await expect(page.getByText('Like Heat').first()).toBeVisible();
  await expect(page.getByTestId('person-chip')).toHaveCount(0);

  await backToTheCard(page);
});
