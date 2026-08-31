import { expect, test } from '@playwright/test';

import { createMember, seedFilmLedger, signInAsMember, signedIn, waitForPool } from '../helpers.js';

/**
 * The TV kiosk route. Spec v2.1 §6.2 step 8 (v2.1 numbering), §12's M4 row ("+ TV route").
 *
 *   "Optional **TV kiosk route** (`/tv`, room code): lobby, progress, result."
 *
 * `tonight-rank-tv-kiosk-route`. §6.2 calls it a nice-to-have and §12 lists it in M4's contents,
 * so it ships — and it is the screen where the blind rule matters most, because the TV is the
 * one display everybody in the room can see. A phone leaking an answer leaks it to its owner;
 * this one leaks it to the household.
 */
test.describe.configure({ mode: 'serial' });

let member;
let code;

test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage();
  await signedIn(page);
  member = await createMember(page, 'tonight-tv');
  await signInAsMember(page, member);
  await seedFilmLedger(page);
  await waitForPool(page);

  await page.goto('/tonight');
  await page.getByTestId('tonight-rewatches').check();
  await page.getByTestId('tonight-open').click();
  await expect(page.getByTestId('tonight-lobby')).toBeVisible();
  code = (await page.getByTestId('tonight-room-code').innerText()).trim();
  await page.close();
});

test('a code that names no live room is refused rather than showing an empty room', async ({
  page
}) => {
  await signedIn(page);
  await page.goto('/tv');
  await expect(page.getByTestId('tv-surface')).toBeVisible();
  await page.getByTestId('tv-code').fill('ZZ-9999');
  await page.getByTestId('tv-attach').click();
  await expect(page.getByTestId('tv-error')).toBeVisible();
  await expect(page.getByTestId('tv-lobby')).toHaveCount(0);
});

test('the TV attaches by room code and shows the lobby', async ({ page }) => {
  await signedIn(page);
  await page.goto('/tv');
  await page.getByTestId('tv-code').fill(code);
  await page.getByTestId('tv-attach').click();

  await expect(page.getByTestId('tv-lobby')).toBeVisible();
  await expect(page.getByTestId('tv-lobby')).toContainText(code);
  await expect(page.getByTestId('tv-lobby')).toContainText(member.name);
});

test('the TV takes the code from the URL, because a television has no keyboard', async ({
  page
}) => {
  await signedIn(page);
  await page.goto(`/tv?code=${code}`);
  await expect(page.getByTestId('tv-lobby')).toBeVisible({ timeout: 15_000 });
});

test('the TV shows progress and no answers while the round runs', async ({ page, browser }) => {
  // The state §6.2 step 8 names between the lobby and the result, and the one where a leak
  // would be worst: the answers are on the wall.
  const phone = await browser.newContext();
  const phonePage = await phone.newPage();
  await signedIn(phonePage);
  await signInAsMember(phonePage, member);
  await phonePage.goto('/tonight');
  await phonePage.getByTestId('tonight-code').fill(code);
  await phonePage.getByTestId('tonight-join').click();
  await expect(phonePage.getByTestId('tonight-lobby')).toBeVisible();
  await phonePage.getByTestId('tonight-start').click();
  await expect(phonePage.getByTestId('tonight-round')).toBeVisible();
  await phonePage.getByTestId('tonight-pick-A').click();

  await signedIn(page);
  await page.goto(`/tv?code=${code}`);
  const progress = page.getByTestId('tv-progress');
  await expect(progress).toBeVisible({ timeout: 15_000 });
  await expect(progress).toContainText(code);

  const shown = await page.getByTestId('tv-surface').innerText();
  for (const leak of ['EITHER', 'NEITHER', 'approved']) {
    expect(shown, `the TV is showing ${leak} mid-round`).not.toContain(leak);
  }
  await expect(page.getByTestId('tv-result')).toHaveCount(0);

  // Finish the round and the ballot, and only then does the TV show a winner.
  for (let i = 0; i < 22; i++) {
    if (!(await phonePage.getByTestId('tonight-round').isVisible())) break;
    await phonePage.getByTestId('tonight-pick-A').click();
    await phonePage.waitForTimeout(120);
  }
  await expect(phonePage.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
  const option = await phonePage
    .locator('[data-testid^="tonight-approve-"]')
    .first()
    .getAttribute('data-testid');
  await phonePage.getByTestId(option).click();
  await phonePage.getByTestId('tonight-submit-ballot').click();

  await expect(page.getByTestId('tv-result')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('tv-approval')).toContainText(/\d+ of \d+ approved/);
  await phone.close();
});
