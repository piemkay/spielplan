import { expect, test } from '@playwright/test';

import { kindToggle, openAccountMenu, signedIn } from '../helpers.js';

/**
 * Your taste and Compare (§6.5; decisions 540, 548, 549): opened from You, never a tab. The fixture
 * places a handful of films, puts every DNA term on one carrier and leaves every member under 20
 * placed films, so this spec holds the entry points, the gates and the empty states; the chart's
 * reading is `backend/tests/test_taste.py`'s. Nothing here writes, so both projects share the stack.
 */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

test('You opens Your taste, which is no tab', async ({ page }) => {
  const menu = await openAccountMenu(page);
  await menu.getByTestId('taste-row').click();
  await expect(page).toHaveURL(/\/taste$/);
  await expect(page.getByRole('heading', { name: 'Your taste', level: 1 })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link')).toHaveCount(4);
});

test('with no term on four placed films, the page says what it waits for, per kind', async ({ page }) => {
  await page.goto('/taste');
  const empty = page.getByTestId('taste-empty');
  await expect(empty).toContainText('at least 4 of the films on your ladder');
  await expect(empty.getByRole('link', { name: 'Go to Rate' })).toHaveAttribute('href', '/rate');
  await expect(page.getByTestId('taste-term')).toHaveCount(0);

  await kindToggle(page, 'Series').click();
  await expect(kindToggle(page, 'Series')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('taste-empty')).toContainText('at least 4 of the series on your ladder');
});

test('Compare opens with its seats, and the picker names why someone cannot be picked', async ({ page }) => {
  await page.goto('/taste');
  await page.getByTestId('taste-compare').click();
  await expect(page).toHaveURL(/\/taste\/compare$/);
  await expect(page.getByRole('heading', { name: 'Compare', level: 1 })).toBeVisible();
  await expect(page.getByTestId('taste-compare-gate')).toContainText(
    'Comparing films opens once two of you have each placed 20 films.'
  );

  const seat = page.getByTestId('taste-seat').first();
  await expect(seat).toHaveAccessibleName(/^Seat 1: You/);
  await seat.click();
  const picker = page.getByTestId('taste-picker');
  await expect(picker).toBeVisible();
  const you = picker.getByRole('radio', { name: /^You/ });
  await expect(you).toHaveAttribute('aria-disabled', 'true');
  await expect(you).toContainText('Not enough films placed yet');
  await expect(picker).toContainText('The films behind a term show only to the two being compared.');

  await page.getByRole('dialog', { name: 'Choose two to compare' }).getByRole('button', { name: 'Done' }).click();
  await expect(picker).toHaveCount(0);
  await expect(page).toHaveURL(/\/taste\/compare$/);
});

test('the Map answers by address with its placeholder, and nothing leads there', async ({ page }) => {
  await page.goto('/map');
  await expect(page.getByRole('heading', { name: 'Map', level: 1 })).toBeVisible();
  await expect(page.getByTestId('placeholder')).toContainText('Not built yet');
  await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Map' })).toHaveCount(0);
});
