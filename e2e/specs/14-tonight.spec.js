import { expect, test } from '@playwright/test';

import { createMember, seedFilmLedger, signInAsMember, signedIn, waitForPool } from '../helpers.js';

/**
 * §6.2's Tonight surface, driven in a browser. Spec v2.1 §6.2 as rewritten (54a–54g), §6.8.
 *
 * Two coverage rows close here and neither can close anywhere cheaper.
 *
 * `tonight-rank-solo-lands-on-picks` — 54f: "lands **directly on three picks and a wildcard** …
 * the fastest path to a film must not be slower than browsing Home." The picks themselves are
 * asserted at the integration layer; what only a browser can prove is that no round and no
 * ballot stands between the door and them. The prototype forced the question round first, and
 * that is a *navigation* defect: every server-side test of the picks passes while it is there.
 *
 * `tonight-rank-guest-hand-off-sequential-and-blind` — §6.2 step 2: "Guests use the initiator's
 * phone after the initiator finishes." On one device the blind property is not protected by the
 * transport: the previous participant's answers have already been delivered to that client. So
 * the hand-off is the only place it can be enforced, or lost, and the loss is a *rendering*
 * fact — which is why this is e2e and not an API test.
 *
 * ONE PAGE FOR THE FILE, like `13-rank.spec.js`: §4.2's observations are append-only, so a
 * shared account cannot be rewound between runs — and Playwright's default context-per-test
 * would throw away the member the seeding built. It runs on the phone project as well as
 * desktop, because §6.2 step 2's hand-the-phone is a claim about a phone.
 */
test.describe('tonight', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    // §5.3's fold-in tick writes `user_score` once a minute, and this hook waits for it for
    // each member in turn. The file's own budget is the config's 60 s test timeout, which is
    // shorter than the thing being waited for.
    test.setTimeout(360_000);
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    const config = await (await page.request.get('/api/config')).json();
    test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
    await signInAsMember(page, await createMember(page, `tonight-${testInfo.project.name}`));
    await seedFilmLedger(page);
    // §5.3 writes `user_score` on the fold-in tick, not on the verdict — without this every
    // assertion below would fail on §6.2's empty pool for a reason unrelated to what it tests.
    await waitForPool(page);
  });

  test.afterAll(async () => {
    await page?.close();
  });

  /** §6.2 step 1's controls, set once per test that needs the whole fixture library.
   *
   * The surface restores a device into a room it is still seated in — a reload must not strand
   * a participant mid-round — so reaching the door starts by stepping back out of whatever the
   * last test left live. That is the same control a household uses to look at the open-rooms
   * list without leaving its own room.
   */
  async function atTheDoor({ rewatches = true, guests = 0 } = {}) {
    await page.goto('/tonight');
    // The surface holds a placeholder until the restore lands, so the door it paints is the
    // settled one — reading `tonight-back` before that would race the swap. The app is
    // client-rendered (`ssr = false`), so the placeholder is not in the served HTML: wait for
    // the surface to exist FIRST, or "no placeholder" is true of an empty page.
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    const back = page.getByTestId('tonight-back');
    if (await back.isVisible().catch(() => false)) await back.click();
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
    if (rewatches) await page.getByTestId('tonight-rewatches').check();
    if (guests) await page.getByTestId('tonight-guests').fill(String(guests));
  }

  test('Tonight is built — M4 landed, so it is no longer a placeholder', async () => {
    // The routine docs/TESTING.md describes: `05-milestones.spec.js`'s placeholder assertion
    // failed the day this surface shipped, and that failure was the reminder to write this.
    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByText(/Not built yet/)).toHaveCount(0);
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  });

  test('the session controls sit before the fork and apply to both doors', async () => {
    // §6.2 step 1: kind, a runtime budget slider, and a rewatch toggle. One control row above
    // both doors — a joiner inherits the host's, so a second copy inside the lobby would be a
    // second source of truth.
    await atTheDoor({ rewatches: false });

    await expect(page.getByTestId('tonight-kind-movie')).toBeVisible();
    await expect(page.getByTestId('tonight-kind-series')).toBeVisible();
    await expect(page.getByTestId('tonight-rewatches')).toBeVisible();
    await expect(page.getByTestId('tonight-open')).toBeVisible();
    await expect(page.getByTestId('tonight-solo-door')).toBeVisible();

    const slider = page.getByTestId('tonight-budget');
    await expect(slider).toHaveAttribute('min', '60');
    await expect(slider).toHaveAttribute('max', '200');
    await expect(page.getByTestId('tonight-budget-value')).toContainText('130 min');
  });

  test('solo lands on three picks and a wildcard with no round and no ballot', async () => {
    // 54f's inversion of the prototype's state transition, and the row's own words: "no pair
    // round and no ballot anywhere in the flow".
    await atTheDoor();
    await page.getByTestId('tonight-solo-door').click();

    await expect(page.getByTestId('tonight-solo')).toBeVisible();
    // One tap from the door. Nothing asked a question on the way.
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);
    await expect(page.getByTestId('tonight-ballot')).toHaveCount(0);
    await expect(page.getByTestId('tonight-picks').locator('li')).toHaveCount(3);
    await expect(page.getByTestId('tonight-solo-wildcard')).toBeVisible();
  });

  test('every solo pick carries a why and a budget-fit line', async () => {
    // §6.8 makes the one-line why mandatory for every recommendation, and §6.2 step 8 fixes
    // both branches of the fit line. Solo is where a person goes for a film in one tap, so an
    // unexplained pick fails the register at its cheapest point.
    await atTheDoor();
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-solo')).toBeVisible();

    const cards = await page.getByTestId('tonight-picks').locator('li').all();
    expect(cards.length, 'no picks means every assertion below is vacuous').toBe(3);
    for (const card of cards) {
      await expect(card.locator('.why')).not.toBeEmpty();
      await expect(card.locator('.data')).toContainText(/fits your \d+ min|runs \d+ min over/);
    }
    await expect(page.getByTestId('tonight-solo-wildcard')).toContainText('a stretch');
  });

  test('the provenance line names the budget and the filter', async () => {
    // 54f: "the provenance line then reads 'tilted by your N answers' instead of 'unseen
    // first'" — so before any sharpen answer it says the other thing.
    await atTheDoor({ rewatches: false });
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-provenance')).toContainText('130 min budget');
    await expect(page.getByTestId('tonight-provenance')).toContainText('unseen first');
  });

  test('reshuffle walks the ranking and asks nothing', async () => {
    // §6.2 step 8's control. A browse gesture, not an observation — the write-path assertion
    // is at the integration layer; what a browser proves is that it does not interrupt the
    // picks with a question.
    await atTheDoor();
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-picks')).toBeVisible();
    const before = await page.getByTestId('tonight-picks').innerText();

    await page.getByTestId('tonight-reshuffle').click();
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);
    await expect
      .poll(async () => page.getByTestId('tonight-picks').innerText(), { timeout: 10_000 })
      .not.toBe(before);
  });

  test('the round offers all four answers and locks the escape until pair 6', async () => {
    // Decision 154's four, and 54c's escape. The escape's availability comes from the server;
    // what a browser proves is that the control is not on the screen before it is real — a
    // control that renders and refuses is worse than one that is not there.
    await atTheDoor();
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await page.getByTestId('tonight-start').click();

    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await expect(page.getByTestId('tonight-pick-A')).toBeVisible();
    await expect(page.getByTestId('tonight-pick-B')).toBeVisible();
    await expect(page.getByTestId('tonight-answer-EITHER')).toBeVisible();
    await expect(page.getByTestId('tonight-answer-NEITHER')).toBeVisible();
    await expect(page.getByTestId('tonight-escape-locked')).toBeVisible();
    await expect(page.getByTestId('tonight-escape')).toHaveCount(0);

    // Leave the room resolved rather than live, so the next test's open-rooms list is its own.
    for (let i = 0; i < 24; i++) {
      if (!(await page.getByTestId('tonight-round').isVisible())) break;
      await page.getByTestId('tonight-pick-A').click();
      await page.waitForTimeout(120);
    }
  });

  test('the guest hand-off clears the screen and offers nothing early', async () => {
    // §6.2 step 2, and the reason this row is e2e: on one device the previous participant's
    // pairs have already been delivered to the client. Nothing on the incoming screen may
    // reach them.
    await atTheDoor({ guests: 1 });
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await expect(page.getByTestId('tonight-seats').locator('li')).toHaveCount(2);
    await page.getByTestId('tonight-start').click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();

    // The initiator's own round is on screen; the guest's turn is not offered yet.
    await expect(page.locator('[data-testid^="tonight-hand-to-"]')).toHaveCount(0);

    for (let i = 0; i < 24; i++) {
      if (!(await page.getByTestId('tonight-round').isVisible())) break;
      await page.getByTestId('tonight-pick-A').click();
      await page.waitForTimeout(120);
    }
    await expect(page.getByTestId('tonight-waiting')).toBeVisible();

    // Now — and only now — the phone offers the guest's turn.
    const handOff = page.locator('[data-testid^="tonight-hand-to-"]').first();
    await expect(handOff).toBeVisible();
    await handOff.click();

    const guestRound = page.getByTestId('tonight-round');
    await expect(guestRound).toBeVisible();
    // The screen is the guest's, not a continuation of the host's: the counter restarts, and
    // the host's own model-log lines are not on it.
    await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    await expect(page.getByTestId('tonight-rail')).toHaveCount(0);
  });
});
