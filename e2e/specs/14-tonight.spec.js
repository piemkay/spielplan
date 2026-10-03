import { expect, test } from '@playwright/test';

import { createMember, setUpLadder, signInAsMember, signedIn, waitForPool } from '../helpers.js';

/**
 * §6.2's Tonight surface: solo lands on picks with no ballot (decision 532); the guest hand-off on
 * one phone is sequential and blind, which only rendering can enforce (§6.2 step 2); a guest seat
 * reaches the reveal. The fixture's six films hold no member's eight liked films and no film with
 * 30,000 crowd ratings, so every seat here skips the round (decision 539): the round itself, its
 * escape and its cap are covered by the backend and unit tests. One page for the file.
 */

// Solo has no seat: each request re-posts the whole round (§6.2 step 8).
const SOLO = /\/api\/tonight\/solo$/;

test.describe('tonight', () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  let memberName;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    // Waits on the worker's minute tick, longer than the config's 60 s.
    test.setTimeout(360_000);
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    const member = await createMember(page, `tonight-${testInfo.project.name}`, { reuse: true });
    memberName = member.name;
    await signInAsMember(page, member);
    await setUpLadder(page.request);
    await waitForPool(page);
  });

  test.afterAll(async () => {
    // A hook has its own budget, and tearing down a long-lived WebKit context can take ~60 s.
    test.setTimeout(300_000);
    await page?.close();
  });

  /** §6.2 step 1's controls live in a sheet behind the door's summary row (decision 527). */
  async function openSettings() {
    await page.getByTestId('tonight-settings').click();
    await expect(page.getByRole('dialog', { name: "Tonight's settings" })).toBeVisible();
  }

  async function closeSettings() {
    await page.getByTestId('tonight-settings-done').click();
    await expect(page.getByRole('dialog', { name: "Tonight's settings" })).toHaveCount(0);
  }

  /** The surface restores a seated device into its room, so this first steps back out of
   * whatever the last test left live. */
  async function atTheDoor({ rewatches = true, guests = 0 } = {}) {
    await page.goto('/tonight');
    // Surface first: client-rendered, so "no placeholder" is also true of an empty page.
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    const back = page.getByTestId('tonight-back');
    const controls = page.getByTestId('tonight-controls');
    // Settle on one of the two first: there is a moment where neither is drawn.
    await expect(back.or(controls).first()).toBeVisible();
    if (await back.isVisible()) await back.click();
    await expect(controls).toBeVisible();
    if (!rewatches && !guests) return;
    await openSettings();
    if (rewatches) await page.getByTestId('tonight-rewatches').check();
    for (let i = 0; i < guests; i++) await page.getByTestId('tonight-guests-more').click();
    await expect(page.getByTestId('tonight-guests')).toHaveText(String(guests));
    await closeSettings();
  }

  /** The solo picks: the first as the hero card, the rest as rows. */
  const soloPicks = () =>
    page.getByTestId('tonight-picks').locator('[data-testid^="tonight-pick-"]');

  /** Solo's door, landing on the picks: with too few films on the ladder it asks nothing. */
  async function soloPicksAtOnce() {
    await Promise.all([
      page.waitForResponse((res) => res.request().method() === 'POST' && SOLO.test(res.url()), {
        timeout: 15_000
      }),
      page.getByTestId('tonight-solo-door').click()
    ]);
    await expect(page.getByTestId('tonight-solo')).toBeVisible();
    await expect(page.getByTestId('tonight-mood')).toHaveCount(0);
  }

  /** The approvals ticked on the ballot drawn: 54e's blindness is falsified by one. */
  const ticked = (p) => p.locator('[data-testid^="tonight-approve-"][aria-pressed="true"]');

  test('Tonight is built — M4 landed, so it is no longer a placeholder', async () => {
    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByText(/Not built yet/)).toHaveCount(0);
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  });

  test('the session controls sit before the fork and apply to both doors', async () => {
    // §6.2 step 1: kind, a runtime budget slider, and a rewatch toggle, one summary row above
    // neither door in particular (decision 527).
    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-open')).toBeVisible();
    await expect(page.getByTestId('tonight-solo-door')).toBeVisible();
    await expect(page.getByTestId('tonight-solo-door')).toContainText(
      "first a few quick pairs of films you've liked, if your ladder has enough"
    );
    await expect(page.getByTestId('tonight-summary')).toHaveText('Film · up to 2h 10m');

    await openSettings();
    await expect(page.getByTestId('tonight-kind-movie')).toBeVisible();
    await expect(page.getByTestId('tonight-kind-series')).toBeVisible();
    await expect(page.getByTestId('tonight-rewatches')).toBeVisible();

    const slider = page.getByTestId('tonight-budget');
    await expect(slider).toHaveAttribute('min', '60');
    await expect(slider).toHaveAttribute('max', '200');
    const readout = page.getByTestId('tonight-budget-value');
    await expect(readout).toHaveText('2h 10m');

    // Decision 219: on a series night the budget is PER EPISODE, and says so here.
    await page.getByTestId('tonight-kind-series').click();
    await expect(readout).toContainText('per episode');
    await page.getByTestId('tonight-kind-movie').click();
    await expect(readout).not.toContainText('per episode');
    await closeSettings();
  });

  test('solo with too few films on the ladder lands on the picks at once, saying why', async () => {
    // Decision 539: fewer than eight films placed Good or higher, so no round; still no
    // session, no room published and no ballot drawn.
    test.setTimeout(120_000);
    await atTheDoor();
    const roomsBefore = (await (await page.request.get('/api/tonight/rooms')).json()).rooms.length;
    await soloPicksAtOnce();

    await expect(page.getByTestId('tonight-no-round')).toHaveText(
      /^No mood questions tonight — they need 8 films on your ladder at Good or higher, and you have \d\.$/
    );
    await expect(soloPicks()).toHaveCount(3);
    await expect(page.getByTestId('tonight-solo-wildcard')).toBeVisible();
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);
    await expect(page.getByTestId('tonight-ballot')).toHaveCount(0);
    // Earlier tests leave rooms behind: the claim is that solo added none.
    const rooms = (await (await page.request.get('/api/tonight/rooms')).json()).rooms;
    expect(rooms.length, '§6.2 step 8: solo publishes no room').toBe(roomsBefore);
  });

  test('every solo pick carries a why and a budget-fit line', async () => {
    // §6.8's mandatory why, and §6.2 step 8's two fit-line branches.
    test.setTimeout(120_000);
    await atTheDoor();
    await soloPicksAtOnce();

    const cards = await soloPicks().all();
    expect(cards.length, 'no picks means every assertion below is vacuous').toBe(3);
    for (const card of cards) {
      await expect(card.getByTestId('tonight-why')).not.toBeEmpty();
      await expect(card.getByTestId('tonight-fit')).toContainText(/fits your time|\d+ min over/i);
    }
    await expect(page.getByTestId('tonight-solo-wildcard')).toContainText('A step outside your usual');
  });

  test('the provenance line says the picks are the usual favourites, and the budget', async () => {
    // §6.2 step 8: "Your usual favourites · …" with no round.
    test.setTimeout(120_000);
    await atTheDoor({ rewatches: false });
    await soloPicksAtOnce();
    await expect(page.getByTestId('tonight-provenance')).toHaveText(
      'Your usual favourites · fits in 2h 10m'
    );
  });

  test('reshuffle walks the ranking and asks nothing', async () => {
    // §6.2 step 8: a browse gesture that asks nothing.
    test.setTimeout(120_000);
    await atTheDoor();
    await soloPicksAtOnce();
    await expect(page.getByTestId('tonight-picks')).toBeVisible();
    const before = await page.getByTestId('tonight-picks').innerText();
    const provenance = page.getByTestId('tonight-provenance');
    const line = (await provenance.textContent()) ?? '';

    await page.getByTestId('tonight-reshuffle').click();
    await expect
      .poll(async () => page.getByTestId('tonight-picks').innerText(), { timeout: 10_000 })
      .not.toBe(before);
    await expect(page.getByTestId('tonight-mood')).toHaveCount(0);
    await expect(provenance).toHaveText(line);

    // Decision 222: the walk "wraps", and says so. Pressed until it appears: which press wraps
    // depends on the pool's size.
    const wrapped = page.getByTestId('tonight-wrapped');
    for (let press = 0; press < 8 && !(await wrapped.isVisible()); press++) {
      await Promise.all([
        page.waitForResponse(
          (res) => res.request().method() === 'POST' && SOLO.test(res.url()),
          { timeout: 15_000 }
        ),
        page.getByTestId('tonight-reshuffle').click()
      ]);
      await expect(page.getByTestId('tonight-picks')).toBeVisible();
    }
    await expect(
      wrapped,
      'eight presses of a three-wide walk never came back round to the top of the ranking'
    ).toBeVisible();
  });

  test("a room's seats skip the round, and the guest's ballot hand-off stays blind", async () => {
    // §6.2 step 2's hand-the-phone, with no round to play: each seat says why it asked nothing
    // and the room goes on to 54e's ballot, which counts guest seats.
    test.setTimeout(300_000);
    await atTheDoor({ guests: 1 });
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await expect(page.getByTestId('tonight-seats').locator('li')).toHaveCount(2);
    await page.getByTestId('tonight-start').click();

    // The host's seat ends at once, saying why, and only then is the guest's turn offered.
    await expect(page.getByTestId('tonight-waiting')).toBeVisible();
    await expect(page.getByTestId('tonight-no-round')).toContainText('No mood questions tonight');
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);
    const handOff = page.locator('[data-testid^="tonight-hand-to-"]').first();
    await expect(handOff).toBeVisible();
    const guestSeat = (await handOff.getAttribute('data-testid')).replace('tonight-hand-to-', '');
    await handOff.click();

    // No well-known films in the library, so the guest asks nothing either.
    const guest = await (await page.request.get(`/api/tonight/seats/${guestSeat}/round`)).json();
    expect(guest.pair, "the guest was served a pair the fixture cannot make").toBeNull();
    expect(guest.no_round).toContain('well-known films');
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);

    // 54e's ballot opens on the guest's seat, with nothing ticked: the baseline.
    await expect(page.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId('tonight-ballot-seat')).toHaveText('Guest 1');
    await expect(ticked(page)).toHaveCount(0);

    // Put down and picked up: the phone falls back to its owner's ballot, the state 54e's
    // ballot hand-off exists for.
    await page.goto('/tonight');
    await expect(page.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId('tonight-ballot-seat')).toHaveText(memberName);

    const options = page.locator('[data-testid^="tonight-approve-"]');
    const first = await options.first().getAttribute('data-testid');
    await page.getByTestId(first).click();
    await expect(ticked(page), 'the owner voted, or the blindness claim below is vacuous').toHaveCount(1);
    // Submit within reach: the ballot is a full-screen flow over the tab bar (decision 527).
    const fold = page.viewportSize().height;
    const submitBox = await page.getByTestId('tonight-submit-ballot').boundingBox();
    expect(submitBox.y + submitBox.height, 'Submit sits below the fold').toBeLessThanOrEqual(fold + 1);
    // And no option between Submit and the fold, where a near miss would approve it.
    const under = await page.evaluate(
      ({ x, top, bottom }) => {
        const hits = [];
        for (let y = top; y < bottom; y += 4) {
          const hit = document.elementFromPoint(x, y)?.closest('[data-testid^="tonight-approve-"]');
          if (hit) hits.push(hit.dataset.testid);
        }
        return hits;
      },
      { x: submitBox.x + submitBox.width / 2, top: submitBox.y + submitBox.height + 1, bottom: fold }
    );
    expect(under, 'an option is tappable under Submit').toEqual([]);
    await page.getByTestId('tonight-submit-ballot').click();

    // 54e: one submission is not every submission. The hand-off control first, or the absence
    // of the reveal is read before the write lands.
    const handBallot = page.getByTestId(`tonight-ballot-to-${guestSeat}`);
    await expect(handBallot).toBeVisible();
    await expect(page.getByTestId('tonight-reveal')).toHaveCount(0);
    await expect(options, 'the slate is still offered to a seat that has voted').toHaveCount(0);
    await handBallot.click();

    // 54e's blindness across the hand-off: the ticks are held in the client.
    await expect(page.getByTestId('tonight-ballot-seat')).toHaveText('Guest 1');
    await expect(ticked(page), "the guest opened on the owner's selections").toHaveCount(0);

    await page.getByTestId(first).click();
    await page.getByTestId('tonight-submit-ballot').click();

    await expect(page.getByTestId('tonight-reveal')).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId('tonight-approval-share')).toContainText(/said yes/);
  });

  test('the slider says the budget is soft, and opens where this member last left it', async () => {
    // Decision 506: the sheet says a little over is fine where the budget is set, and reopens
    // at the budget last used, per kind.
    test.setTimeout(300_000);
    await atTheDoor({ rewatches: false });
    await openSettings();
    await expect(page.getByTestId('tonight-budget-soft')).toContainText('A little over is fine');
    await page.getByTestId('tonight-budget').fill('120');
    await expect(page.getByTestId('tonight-budget-value')).toHaveText('2h');
    await closeSettings();
    await expect(page.getByTestId('tonight-summary')).toHaveText('Film · up to 2h');
    await soloPicksAtOnce();

    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-summary')).toHaveText('Film · up to 2h');

    // Back to the default, so the file leaves the phone as it found it.
    await openSettings();
    await page.getByTestId('tonight-budget').fill('130');
    await closeSettings();
    await soloPicksAtOnce();
    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-summary')).toHaveText('Film · up to 2h 10m');
  });
});
