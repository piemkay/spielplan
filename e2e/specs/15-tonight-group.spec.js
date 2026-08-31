import { expect, test } from '@playwright/test';

import { createMember, seedFilmLedger, signInAsMember, signedIn, waitForPool } from '../helpers.js';

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
 * TWO PAGES FOR THE FILE, built once. Playwright hands each test a fresh context, which would
 * throw away both seeded members — the same reason `13-rank.spec.js` keeps one page for its
 * file. Desktop only: two contexts is what this file is about, and the phone project runs
 * `14-tonight` for the one-device gestures.
 */
test.describe('tonight together', () => {
  /** @type {import('@playwright/test').Page} */
  let a;
  /** @type {import('@playwright/test').Page} */
  let b;
  const contexts = [];

  test.beforeAll(async ({ browser, baseURL }) => {
    // §5.3's fold-in tick writes `user_score` once a minute, and this hook waits for it for
    // each member in turn. The file's own budget is the config's 60 s test timeout, which is
    // shorter than the thing being waited for.
    test.setTimeout(360_000);
    for (const label of ['host', 'mate']) {
      const context = await browser.newContext({ baseURL });
      contexts.push(context);
      const page = await context.newPage();
      await signedIn(page);
      if (label === 'host') {
        const config = await (await page.request.get('/api/config')).json();
        test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
      }
      await signInAsMember(page, await createMember(page, `tonight-${label}`));
      await seedFilmLedger(page);
      await waitForPool(page);
      if (label === 'host') a = page;
      else b = page;
    }
  });

  test.afterAll(async () => {
    for (const context of contexts) await context.close();
  });

  /** A fresh room, hosted by `a`, with `b` joined by the channel the test names.

   * The surface restores a device into a room it is still seated in (a reload must not strand
   * a participant mid-round), so opening the next room starts by stepping back out to the door
   * — the same control a household uses to look at the open-rooms list without leaving. */
  async function toDoor(page) {
    await page.goto('/tonight');
    // `ssr = false`, so wait for the surface before asserting the placeholder is gone —
    // otherwise "no placeholder" is true of a page that has not rendered anything yet.
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    const back = page.getByTestId('tonight-back');
    if (await back.isVisible().catch(() => false)) await back.click();
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  }

  async function room({ join = 'code' } = {}) {
    await toDoor(a);
    await a.getByTestId('tonight-rewatches').check();
    await a.getByTestId('tonight-open').click();
    await expect(a.getByTestId('tonight-lobby')).toBeVisible();
    const code = (await a.getByTestId('tonight-room-code').innerText()).trim();

    if (join === 'code') {
      await toDoor(b);
      await b.getByTestId('tonight-code').fill(code);
      await b.getByTestId('tonight-join').click();
      await expect(b.getByTestId('tonight-lobby')).toBeVisible();
    }
    return code;
  }

  /** Answer through one person's whole round. */
  async function playOut(page) {
    await expect(page.getByTestId('tonight-round')).toBeVisible({ timeout: 20_000 });
    for (let i = 0; i < 24; i++) {
      if (!(await page.getByTestId('tonight-round').isVisible())) break;
      await page.getByTestId('tonight-pick-A').click();
      await page.waitForTimeout(120);
    }
  }

  test('a room one member opens appears on the other device, live, with a tappable seat', async () => {
    // The second device sits on the door screen it loaded BEFORE the room existed; the channel
    // is what puts the room on it. A reload here would prove nothing.
    await toDoor(b);
    await expect(b.getByTestId('tonight-rooms')).toBeVisible();

    const code = await room({ join: 'none' });
    const row = b.getByTestId(`tonight-room-${code}`);
    await expect(row).toBeVisible({ timeout: 25_000 });

    // §6.2 step 2's own example string, on the row.
    await expect(row).toContainText(code);
    await expect(row).toContainText('Film');
    await expect(row).toContainText('min');

    // "with tappable empty seats" — no code typed, no notification involved.
    await b.getByTestId(`tonight-seat-${code}`).click();
    await expect(b.getByTestId('tonight-lobby')).toBeVisible();
    await expect(b.getByTestId('tonight-room-code')).toContainText(code);

    // And the host's lobby learns about the arrival over the same channel, without a reload.
    await expect(a.getByTestId('tonight-seats').locator('li')).toHaveCount(2, {
      timeout: 25_000
    });

    // Resolve the room so the next test's open-rooms list is its own.
    await a.getByTestId('tonight-start').click();
    await playOut(a);
    await playOut(b);
  });

  test('the round is blind: neither device shows the other any answer', async () => {
    // 54c: "Someone who finishes early sees the others' progress and never their answers."
    //
    // Asserting that the second screen does not happen to *contain* an answer is close to
    // vacuous — no template draws one, so the assertion holds on a build with no blindness rule
    // at all. What makes this falsifiable is going after the data instead: the second device
    // asks the API for the first device's seat, with its own session cookie, and the answer has
    // to be a refusal rather than the round.
    await room();
    await a.getByTestId('tonight-start').click();
    await expect(a.getByTestId('tonight-round')).toBeVisible();
    await a.getByTestId('tonight-pick-A').click();

    // b's own view of the room names the seats, so it knows the id to ask for.
    const sessionId = await b.evaluate(async () => {
      const rooms = await (await fetch('/api/tonight/rooms')).json();
      return rooms.rooms.find((r) => r.viewer_seated).session_id;
    });
    const seats = await (await b.request.get(`/api/tonight/sessions/${sessionId}`)).json();
    const mine = seats.me.participant_id;
    const theirs = seats.seats.find((s) => s.participant_id !== mine).participant_id;

    const refused = await b.request.get(`/api/tonight/seats/${theirs}/round`);
    expect(refused.status(), "one seat read another seat's round").toBe(403);
    // And it is a refusal about the seat, not about being logged out: b's own seat still reads.
    expect((await b.request.get(`/api/tonight/seats/${mine}/round`)).status()).toBe(200);

    // What b DOES get is the count, live, and the payload it arrives in carries no answer.
    const progress = await b.evaluate(async (id) => {
      const seen = await (await fetch(`/api/tonight/sessions/${id}`)).json();
      return seen.progress;
    }, sessionId);
    const them = progress.find((p) => p.participant_id === theirs);
    expect(them.answered, 'the first device answered, so the count moved').toBeGreaterThan(0);
    expect(JSON.stringify(progress)).not.toMatch(/EITHER|NEITHER|"answer"|card_token/);

    await playOut(a);
    await playOut(b);
    await expect(a.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
  });

  test('the reveal beat precedes the winner, and the card carries its whole inventory', async () => {
    await room();
    await a.getByTestId('tonight-start').click();
    await playOut(a);
    await playOut(b);

    // 54e: the ballot is a multi-select over the three finalists and the wildcard.
    await expect(a.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(b.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    const options = a.locator('[data-testid^="tonight-approve-"]');
    const count = await options.count();
    expect(count, 'the ballot is over the finalists and the wildcard').toBeGreaterThan(0);
    expect(count).toBeLessThanOrEqual(4);

    const first = await options.first().getAttribute('data-testid');
    await a.getByTestId(first).click();
    await a.getByTestId('tonight-submit-ballot').click();

    // BLIND: one submission is not every submission, so nothing is revealed here.
    await expect(a.getByTestId('tonight-reveal')).toHaveCount(0);

    await b.getByTestId(first).click();
    await b.getByTestId('tonight-submit-ballot').click();

    for (const page of [a, b]) {
      await expect(page.getByTestId('tonight-reveal')).toBeVisible({ timeout: 25_000 });
      // proposal 60: the beat comes BEFORE the winner. Asserted as document order, because
      // "renders somewhere on the page" is not what the sentence says.
      const beat = await page.getByTestId('tonight-beat').boundingBox();
      const winner = await page.getByTestId('tonight-winner').boundingBox();
      expect(beat.y).toBeLessThan(winner.y);
      await expect(page.getByTestId('tonight-beat')).toHaveText('VOTES REVEALED TOGETHER');

      // §6.2 step 7's inventory, item by item.
      await expect(page.getByTestId('tonight-approval-share')).toContainText(
        /\d+ of \d+ approved/
      );
      await expect(page.getByTestId('tonight-match-lines').locator('li')).not.toHaveCount(0);
      await expect(page.getByTestId('tonight-fit-line')).toContainText(
        /fits your \d+ min|runs \d+ min over/
      );
      await expect(page.getByTestId('tonight-runners-up')).toBeVisible();
      await expect(page.getByTestId('tonight-play')).toBeVisible();
    }
    // Both approved the same title, so §6.2 step 7's unanimity is called out.
    await expect(a.getByTestId('tonight-unanimous')).toBeVisible();
  });
});
