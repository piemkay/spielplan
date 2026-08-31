import { expect, test } from '@playwright/test';

import {
  createMember,
  loginAsMember,
  seedFilmLedger,
  signInAsMember,
  signedIn,
  waitForPool
} from '../helpers.js';

/**
 * Tonight with two people in two browsers. Spec v2.1 §6.2 steps 2, 6 and 7 (rewritten 54c–54e).
 *
 * This is the first spec in the suite that needs **two clients at once**, and that is the whole
 * point of it: three of M4's rows are claims about what one device shows while another device
 * is doing something, and a single context cannot fail any of them.
 *
 *   `tonight-rank-open-rooms-discovery` — §6.2 step 2's list is "visible to every household
 *   device", with "tappable empty seats". One browser opens a room; the other has to see it and
 *   be able to sit down without typing a code.
 *
 *   `tonight-rank-lobby-live-over-the-session-channel` — §6.2 step 2's live banner "over the
 *   WebSocket". The falsifiable half is *without a reload*: a lobby that needed a refresh to
 *   show an arrival passes every server test and is the thing a household talks over.
 *
 *   `tonight-rank-result-card-inventory` — §6.2 step 7 and proposal 60. The prototype's result
 *   screen was a poster, the word "Unanimous." and two buttons; the share, the match lines, the
 *   runners-up and the wildcard were all specified and none was drawn.
 *
 * Serial and one worker, like every spec here: the household is shared state.
 */
test.describe.configure({ mode: 'serial' });

let host;
let guestMember;

test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage();
  await signedIn(page);
  host = await createMember(page, 'tonight-host');
  guestMember = await createMember(page, 'tonight-mate');
  await page.close();
});

/** Two signed-in pages, one per member — two cookie jars, which is what makes this a household
 * rather than one person with two tabs. */
async function household(browser) {
  const first = await browser.newContext();
  const second = await browser.newContext();
  const a = await first.newPage();
  const b = await second.newPage();

  await signedIn(a);
  await signInAsMember(a, host);
  await seedFilmLedger(a);
  await waitForPool(a);

  await loginAsMember(b, guestMember).catch(async () => {
    await signedIn(b);
    await signInAsMember(b, guestMember);
  });
  await seedFilmLedger(b);
  await waitForPool(b);

  return { a, b, close: async () => Promise.all([first.close(), second.close()]) };
}

test('a room one member opens appears on the other device, live, with a tappable seat', async ({
  browser
}) => {
  const { a, b, close } = await household(browser);
  try {
    await b.goto('/tonight');
    await expect(b.getByTestId('tonight-no-rooms')).toBeVisible();

    await a.goto('/tonight');
    await a.getByTestId('tonight-rewatches').check();
    await a.getByTestId('tonight-open').click();
    await expect(a.getByTestId('tonight-lobby')).toBeVisible();
    const code = (await a.getByTestId('tonight-room-code').innerText()).trim();

    // WITHOUT A RELOAD. The second device is sitting on the door screen it loaded before the
    // room existed; the channel is what puts the room on it.
    const row = b.getByTestId(`tonight-room-${code}`);
    await expect(row).toBeVisible({ timeout: 20_000 });

    // §6.2 step 2's own example string, on the row.
    await expect(row).toContainText(code);
    await expect(row).toContainText(host.name);
    await expect(row).toContainText('Film');
    await expect(row).toContainText('min');

    // "with tappable empty seats" — no code typed, no notification involved.
    await b.getByTestId(`tonight-seat-${code}`).click();
    await expect(b.getByTestId('tonight-lobby')).toBeVisible();
    await expect(b.getByTestId('tonight-room-code')).toContainText(code);

    // And the host's lobby learns about the arrival over the same channel.
    await expect(a.getByTestId('tonight-seats').locator('li')).toHaveCount(2, {
      timeout: 20_000
    });
  } finally {
    await close();
  }
});

test('the round is blind: neither device shows the other any answer', async ({ browser }) => {
  const { a, b, close } = await household(browser);
  try {
    await a.goto('/tonight');
    await a.getByTestId('tonight-rewatches').check();
    await a.getByTestId('tonight-open').click();
    const code = (await a.getByTestId('tonight-room-code').innerText()).trim();

    await b.goto('/tonight');
    await b.getByTestId('tonight-code').fill(code);
    await b.getByTestId('tonight-join').click();
    await expect(b.getByTestId('tonight-lobby')).toBeVisible();

    await a.getByTestId('tonight-start').click();
    await expect(a.getByTestId('tonight-round')).toBeVisible();

    // One device answers; the other's screen must show a count and nothing else.
    await a.getByTestId('tonight-pick-A').click();
    await b.reload();
    const shown = await b.locator('[data-testid="tonight-surface"]').innerText();
    expect(shown).not.toMatch(/Either is fine.*answered|answered A|answered B/i);

    // Play both rounds out.
    for (const page of [a, b]) {
      for (let i = 0; i < 22; i++) {
        if (!(await page.getByTestId('tonight-round').isVisible())) break;
        await page.getByTestId('tonight-pick-A').click();
        await page.waitForTimeout(120);
      }
    }
    await expect(a.getByTestId('tonight-ballot')).toBeVisible({ timeout: 20_000 });
  } finally {
    await close();
  }
});

test('the reveal beat precedes the winner, and the card carries its whole inventory', async ({
  browser
}) => {
  const { a, b, close } = await household(browser);
  try {
    await a.goto('/tonight');
    await a.getByTestId('tonight-rewatches').check();
    await a.getByTestId('tonight-open').click();
    const code = (await a.getByTestId('tonight-room-code').innerText()).trim();

    await b.goto('/tonight');
    await b.getByTestId('tonight-code').fill(code);
    await b.getByTestId('tonight-join').click();
    await a.getByTestId('tonight-start').click();

    for (const page of [a, b]) {
      await expect(page.getByTestId('tonight-round')).toBeVisible({ timeout: 20_000 });
      for (let i = 0; i < 22; i++) {
        if (!(await page.getByTestId('tonight-round').isVisible())) break;
        await page.getByTestId('tonight-pick-A').click();
        await page.waitForTimeout(120);
      }
    }

    // 54e: the ballot is a multi-select over the finalists and the wildcard.
    await expect(a.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(b.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    const options = a.locator('[data-testid^="tonight-approve-"]');
    const count = await options.count();
    expect(count, 'the ballot is over the three finalists and the wildcard').toBeGreaterThan(0);
    expect(count).toBeLessThanOrEqual(4);

    const first = await options.first().getAttribute('data-testid');
    await a.getByTestId(first).click();
    await a.getByTestId('tonight-submit-ballot').click();

    // BLIND: one submission is not every submission, so nothing is revealed here.
    await expect(a.getByTestId('tonight-reveal')).toHaveCount(0);

    await b.getByTestId(first).click();
    await b.getByTestId('tonight-submit-ballot').click();

    for (const page of [a, b]) {
      const reveal = page.getByTestId('tonight-reveal');
      await expect(reveal).toBeVisible({ timeout: 25_000 });
      // proposal 60: the beat comes BEFORE the winner. Asserted as document order, because
      // "renders somewhere on the page" is not what the sentence says.
      const beatBox = await page.getByTestId('tonight-beat').boundingBox();
      const winnerBox = await page.getByTestId('tonight-winner').boundingBox();
      expect(beatBox.y).toBeLessThan(winnerBox.y);
      await expect(page.getByTestId('tonight-beat')).toHaveText('VOTES REVEALED TOGETHER');

      // §6.2 step 7's inventory, item by item.
      await expect(page.getByTestId('tonight-approval-share')).toContainText(/\d+ of \d+ approved/);
      await expect(page.getByTestId('tonight-match-lines').locator('li')).not.toHaveCount(0);
      await expect(page.getByTestId('tonight-fit-line')).toContainText(
        /fits your \d+ min|runs \d+ min over/
      );
      await expect(page.getByTestId('tonight-runners-up')).toBeVisible();
      await expect(page.getByTestId('tonight-play')).toBeVisible();
    }
    // Unanimous, because both approved the same title — §6.2 step 7 calls it out when it occurs.
    await expect(a.getByTestId('tonight-unanimous')).toBeVisible();
  } finally {
    await close();
  }
});
