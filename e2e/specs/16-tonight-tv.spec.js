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
 *
 * TWO PAGES: the television and the phone driving the room. Built once in `beforeAll` for the
 * reason `13-rank.spec.js` keeps one page — a fresh context per test would throw away both the
 * seeded member and the live room.
 */
test.describe('tonight on the tv', () => {
  /** @type {import('@playwright/test').Page} */
  let tv;
  /** @type {import('@playwright/test').Page} */
  let phone;
  const contexts = [];
  let code;
  let seat;

  test.beforeAll(async ({ browser, baseURL }) => {
    // §5.3's fold-in tick writes `user_score` once a minute, and this hook waits for it for
    // each member in turn. The file's own budget is the config's 60 s test timeout, which is
    // shorter than the thing being waited for.
    test.setTimeout(360_000);
    for (const which of ['tv', 'phone']) {
      const context = await browser.newContext({ baseURL });
      contexts.push(context);
      const page = await context.newPage();
      await signedIn(page);
      if (which === 'tv') {
        const config = await (await page.request.get('/api/config')).json();
        test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
        tv = page;
      } else {
        phone = page;
      }
    }
    // The phone is a household member with a ledger; the TV is signed in as the admin, which
    // is the closest this suite gets to "a device in the living room".
    await signInAsMember(phone, await createMember(phone, 'tonight-tv-member'));
    await seedFilmLedger(phone);
    await waitForPool(phone);

    await phone.goto('/tonight');
    await phone.getByTestId('tonight-rewatches').check();
    await phone.getByTestId('tonight-open').click();
    await expect(phone.getByTestId('tonight-lobby')).toBeVisible();
    code = (await phone.getByTestId('tonight-room-code').innerText()).trim();
    seat = await phone
      .getByTestId('tonight-seats')
      .locator('li')
      .first()
      .innerText();
  });

  test.afterAll(async () => {
    for (const context of contexts) await context.close();
  });

  test('a code that names no live room is refused rather than showing an empty room', async () => {
    await tv.goto('/tv');
    await expect(tv.getByTestId('tv-surface')).toBeVisible();
    await tv.getByTestId('tv-code').fill('ZZ-9999');
    await tv.getByTestId('tv-attach').click();
    await expect(tv.getByTestId('tv-error')).toBeVisible();
    await expect(tv.getByTestId('tv-lobby')).toHaveCount(0);
  });

  test('the TV attaches by room code and shows the lobby', async () => {
    await tv.goto('/tv');
    await tv.getByTestId('tv-code').fill(code);
    await tv.getByTestId('tv-attach').click();

    await expect(tv.getByTestId('tv-lobby')).toBeVisible();
    await expect(tv.getByTestId('tv-lobby')).toContainText(code);
    expect(seat, 'the seat list is what the TV renders').toBeTruthy();
  });

  test('the TV takes the code from the URL, because a television has no keyboard', async () => {
    await tv.goto(`/tv?code=${code}`);
    await expect(tv.getByTestId('tv-lobby')).toBeVisible({ timeout: 15_000 });
  });

  test('the TV shows progress and no answers while the round runs', async () => {
    // The state §6.2 step 8 names between the lobby and the result, and the one where a leak
    // would be worst: the answers are on the wall.
    await phone.getByTestId('tonight-start').click();
    await expect(phone.getByTestId('tonight-round')).toBeVisible();
    await phone.getByTestId('tonight-pick-A').click();

    await tv.goto(`/tv?code=${code}`);
    const progress = tv.getByTestId('tv-progress');
    await expect(progress).toBeVisible({ timeout: 20_000 });
    await expect(progress).toContainText(code);

    const shown = await tv.getByTestId('tv-surface').innerText();
    for (const leak of ['EITHER', 'NEITHER', 'approved']) {
      expect(shown, `the TV is showing ${leak} mid-round`).not.toContain(leak);
    }
    await expect(tv.getByTestId('tv-result')).toHaveCount(0);
  });

  test('the TV shows the winner only once the ballot has closed', async () => {
    for (let i = 0; i < 24; i++) {
      if (!(await phone.getByTestId('tonight-round').isVisible())) break;
      await phone.getByTestId('tonight-pick-A').click();
      await phone.waitForTimeout(120);
    }
    await expect(phone.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });

    // Still nothing on the wall: the round is over but nobody has voted.
    await tv.goto(`/tv?code=${code}`);
    await expect(tv.getByTestId('tv-result')).toHaveCount(0);

    const option = await phone
      .locator('[data-testid^="tonight-approve-"]')
      .first()
      .getAttribute('data-testid');
    await phone.getByTestId(option).click();
    await phone.getByTestId('tonight-submit-ballot').click();

    await expect(tv.getByTestId('tv-result')).toBeVisible({ timeout: 30_000 });
    await expect(tv.getByTestId('tv-approval')).toContainText(/\d+ of \d+ approved/);
  });
});
