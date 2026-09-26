import { expect, test } from '@playwright/test';

import { createMember, seedFilmLedger, signInAsMember, signedIn, waitForPool } from '../helpers.js';

/**
 * Tonight with two people in two browsers (§6.2 steps 2, 6, 7): the open-rooms list and the lobby
 * update live without a reload, the round is blind, and the result card carries its whole
 * inventory. Two pages for the file, built once. Desktop only.
 */
test.describe('tonight together', () => {
  /** @type {import('@playwright/test').Page} */
  let a;
  /** @type {import('@playwright/test').Page} */
  let b;
  const contexts = [];

  test.beforeAll(async ({ browser, baseURL }) => {
    // Waits on the worker's minute tick for each member, longer than the config's 60 s.
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
      await signInAsMember(page, await createMember(page, `tonight-${label}`, { reuse: true }));
      await seedFilmLedger(page);
      await waitForPool(page);
      if (label === 'host') a = page;
      else b = page;
    }
  });

  test.afterAll(async () => {
    for (const context of contexts) await context.close();
  });

  /** Back to the door: the surface restores a seated device into its room. */
  async function toDoor(page) {
    await page.goto('/tonight');
    // `ssr = false`: "no placeholder" is also true of a page not rendered yet.
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    const back = page.getByTestId('tonight-back');
    const controls = page.getByTestId('tonight-controls');
    // Settle on one of the two first: there is a moment where neither is drawn.
    await expect(back.or(controls).first()).toBeVisible();
    if (await back.isVisible()) await back.click();
    await expect(controls).toBeVisible();
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

  /**
   * Answer one person's round to its end, wherever it has got to. On the fixture's six films
   * one answer can end a round (decision 214), so the round may already be over: settle on the
   * round, 54c's progress view or 54e's ballot.
   */
  async function playOut(page) {
    const round = page.getByTestId('tonight-round');
    await expect(
      round.or(page.getByTestId('tonight-waiting')).or(page.getByTestId('tonight-ballot')).first()
    ).toBeVisible({ timeout: 20_000 });
    for (let i = 0; i < 24; i++) {
      if (!(await round.isVisible())) break;
      // Wait on the write: §6.2's round is one POST per pair.
      await Promise.all([
        page.waitForResponse(
          (res) =>
            res.request().method() === 'POST' && /\/api\/tonight\/seats\/\d+\/answer$/.test(res.url()),
          { timeout: 15_000 }
        ),
        page.getByTestId('tonight-pick-A').click()
      ]);
    }
  }

  test('a room one member opens appears on the other device, live, with a tappable seat', async () => {
    // Loaded BEFORE the room existed: only the channel can put the room on it.
    await toDoor(b);
    await expect(b.getByTestId('tonight-rooms')).toBeVisible();

    const code = await room({ join: 'none' });
    const row = b.getByTestId(`tonight-room-${code}`);
    await expect(row).toBeVisible({ timeout: 25_000 });

    await expect(row).toContainText(code);
    await expect(row).toContainText('Film');
    await expect(row).toContainText('min');

    await b.getByTestId(`tonight-seat-${code}`).click();
    await expect(b.getByTestId('tonight-lobby')).toBeVisible();
    await expect(b.getByTestId('tonight-room-code')).toContainText(code);

    await expect(a.getByTestId('tonight-seats').locator('li')).toHaveCount(2, {
      timeout: 25_000
    });

    await a.getByTestId('tonight-start').click();
    await playOut(a);
    await playOut(b);
  });

  test('the round is blind: neither device shows the other any answer', async () => {
    // 54c: "Someone who finishes early sees the others' progress and never their answers."
    // Falsifiable at the API: b asks for a's seat with its own cookie and must be refused.
    await room();
    await a.getByTestId('tonight-start').click();
    await expect(a.getByTestId('tonight-round')).toBeVisible();
    await a.getByTestId('tonight-pick-A').click();

    const sessionId = await b.evaluate(async () => {
      const rooms = await (await fetch('/api/tonight/rooms')).json();
      return rooms.rooms.find((r) => r.viewer_seated).session_id;
    });
    const seats = await (await b.request.get(`/api/tonight/sessions/${sessionId}`)).json();
    const mine = seats.me.participant_id;
    const theirs = seats.seats.find((s) => s.participant_id !== mine).participant_id;

    const refused = await b.request.get(`/api/tonight/seats/${theirs}/round`);
    expect(refused.status(), "one seat read another seat's round").toBe(403);
    expect((await b.request.get(`/api/tonight/seats/${mine}/round`)).status()).toBe(200);

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

    await expect(a.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(b.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    const options = a.locator('[data-testid^="tonight-approve-"]');
    const count = await options.count();
    expect(count, 'the ballot is over the finalists and the wildcard').toBeGreaterThan(0);
    expect(count).toBeLessThanOrEqual(4);

    const first = await options.first().getAttribute('data-testid');
    await a.getByTestId(first).click();
    await a.getByTestId('tonight-submit-ballot').click();

    await expect(a.getByTestId('tonight-reveal')).toHaveCount(0);

    await b.getByTestId(first).click();
    await b.getByTestId('tonight-submit-ballot').click();

    for (const page of [a, b]) {
      await expect(page.getByTestId('tonight-reveal')).toBeVisible({ timeout: 25_000 });
      // Proposal 60: the beat comes BEFORE the winner.
      const beat = await page.getByTestId('tonight-beat').boundingBox();
      const winner = await page.getByTestId('tonight-winner').boundingBox();
      expect(beat.y).toBeLessThan(winner.y);
      await expect(page.getByTestId('tonight-beat')).toHaveText('VOTES REVEALED TOGETHER');

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
    // Not "Unanimous.", which says nothing about how broad each yes was.
    await expect(a.getByTestId('tonight-unanimous')).toHaveCount(0);
    await expect(a.getByTestId('tonight-breadth')).toContainText(/said yes to 1 of \d/);
    await expect(a.getByTestId('tonight-only-yes')).toHaveCount(2);
  });

  test('a ?room= link lands the other device in the room, and the lobby offers the link', async () => {
    // Decision 481: the room's URL seats a signed-in member with no code typed.
    await room({ join: false });
    await expect(a.getByTestId('tonight-share')).toBeVisible();
    await expect(a.getByTestId('tonight-share-caption')).not.toContainText('send the link');
    const code = (await a.getByTestId('tonight-room-code').textContent()).trim();

    await b.goto(`/tonight?room=${code}`);
    await expect(b.getByTestId('tonight-lobby')).toBeVisible({ timeout: 15_000 });
    await expect(b.getByTestId('tonight-room-code')).toHaveText(code);
    await expect(b).toHaveURL(/\/tonight$/);
    await expect(a.getByTestId('tonight-seats').locator('li')).toHaveCount(2, { timeout: 15_000 });
    await a.getByTestId('tonight-end-room').click();
    await a.getByTestId('tonight-end-room-confirm').click();
  });

  test("each member rules out their own three, and one member's three leave the other theirs", async () => {
    // Decision 505: up to three vetoes each, named on the other phone, the union on the row.
    // Ended, not started, so the small pool never has to survive the vetoes.
    const code = await room();
    for (const key of ['violence', 'horror', 'harrowing']) {
      await a.getByTestId(`tonight-veto-${key}`).click();
      await expect(a.getByTestId(`tonight-veto-${key}`)).toHaveAttribute('aria-pressed', 'true');
    }
    await expect(a.getByTestId('tonight-veto-sexual_violence')).toBeDisabled();
    await expect(a.getByTestId('tonight-mood-caption')).toContainText('Neither pulls me tonight');

    await expect(b.getByTestId('tonight-others-vetoes')).toContainText('violence, horror, harrowing', {
      timeout: 15_000
    });
    const other = b.getByTestId('tonight-veto-sexual_violence');
    await expect(other).toBeEnabled();
    await other.click();
    await expect(other).toHaveAttribute('aria-pressed', 'true');
    await expect(b.getByTestId('tonight-veto-violence')).toHaveAttribute('aria-pressed', 'false');

    await toDoor(b);
    await expect(b.getByTestId(`tonight-room-${code}`)).toContainText(
      'not tonight: violence, sexual violence, horror, harrowing',
      { timeout: 15_000 }
    );
    await a.getByTestId('tonight-end-room').click();
    await a.getByTestId('tonight-end-room-confirm').click();
  });
});
