import { expect, test } from '@playwright/test';

import { createMember, seedFilmLedger, signInAsMember, signedIn, waitForPool } from '../helpers.js';

/**
 * §6.2's Tonight surface (54a–54g): solo lands on picks with no round or ballot (54f); the guest
 * hand-off on one phone is sequential and blind, which only rendering can enforce (§6.2 step 2);
 * a guest seat reaches the reveal; a household frame does not clobber the seat being played; and
 * `latency_ms` is measured, against §6's card budgets. One page for the file (observations are
 * append-only), plus one second context to send a `rooms.changed` frame.
 */

// §6.2's round is one POST per pair, so the thing to wait on is the write itself.
const ANSWER = /\/api\/tonight\/seats\/\d+\/answer$/;

// The dwell being measured (not a wait), and a margin for the phone's clock.
const DWELL_MS = 400;
const CLOCK_SLOP_MS = 100;

// §6's preamble: "<2 s per sweep card, <1.5 s per battle". A Tonight pair is a battle.
const BATTLE_BUDGET_MS = 1_500;
const SWEEP_BUDGET_MS = 2_000;

test.describe('tonight', () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  /** A second household device, never seated in `page`'s room.
   * @type {import('@playwright/test').Page} */
  let other;
  /** @type {import('@playwright/test').BrowserContext} */
  let otherContext;
  let memberName;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    // Waits on the worker's minute tick, longer than the config's 60 s.
    test.setTimeout(360_000);
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    const member = await createMember(page, `tonight-${testInfo.project.name}`, { reuse: true });
    memberName = member.name;
    await signInAsMember(page, member);
    await seedFilmLedger(page);
    await waitForPool(page);

    // In the household and out of the room: `rooms.changed` is broadcast household-wide.
    otherContext = await browser.newContext({ baseURL });
    other = await otherContext.newPage();
    await signedIn(other);
  });

  test.afterAll(async () => {
    // A hook has its own budget, and tearing down a long-lived WebKit context can take ~60 s.
    test.setTimeout(300_000);
    await page?.close();
    await otherContext?.close();
  });

  /** §6.2 step 1's controls. The surface restores a seated device into its room, so this first
   * steps back out of whatever the last test left live. */
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
    if (rewatches) await page.getByTestId('tonight-rewatches').check();
    if (guests) await page.getByTestId('tonight-guests').fill(String(guests));
  }

  /** Answer the round on screen to its end, waiting on each answer's write. */
  async function playOut() {
    for (let i = 0; i < 24; i++) {
      if (!(await page.getByTestId('tonight-round').isVisible())) break;
      await Promise.all([
        page.waitForResponse(
          (res) => res.request().method() === 'POST' && ANSWER.test(res.url()),
          { timeout: 15_000 }
        ),
        page.getByTestId('tonight-pick-A').click()
      ]);
    }
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
    // §6.2 step 1: kind, a runtime budget slider, and a rewatch toggle, above both doors.
    await atTheDoor({ rewatches: false });

    await expect(page.getByTestId('tonight-kind-movie')).toBeVisible();
    await expect(page.getByTestId('tonight-kind-series')).toBeVisible();
    await expect(page.getByTestId('tonight-rewatches')).toBeVisible();
    await expect(page.getByTestId('tonight-open')).toBeVisible();
    await expect(page.getByTestId('tonight-solo-door')).toBeVisible();

    const slider = page.getByTestId('tonight-budget');
    await expect(slider).toHaveAttribute('min', '60');
    await expect(slider).toHaveAttribute('max', '200');
    const readout = page.getByTestId('tonight-budget-value');
    await expect(readout).toContainText('130 min');

    // Decision 219: on a series night the budget is PER EPISODE, and says so here.
    await page.getByTestId('tonight-kind-series').click();
    await expect(readout).toContainText('130 min per episode');
    await page.getByTestId('tonight-kind-movie').click();
    await expect(readout).not.toContainText('per episode');
  });

  test('solo lands on three picks and a wildcard with no round and no ballot', async () => {
    // 54f: "no pair round and no ballot anywhere in the flow".
    await atTheDoor();
    await page.getByTestId('tonight-solo-door').click();

    await expect(page.getByTestId('tonight-solo')).toBeVisible();
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);
    await expect(page.getByTestId('tonight-ballot')).toHaveCount(0);
    await expect(page.getByTestId('tonight-picks').locator('li')).toHaveCount(3);
    await expect(page.getByTestId('tonight-solo-wildcard')).toBeVisible();
  });

  test('every solo pick carries a why and a budget-fit line', async () => {
    // §6.8's mandatory why, and §6.2 step 8's two fit-line branches.
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
    // 54f: "unseen first" until a sharpen answer tilts it.
    await atTheDoor({ rewatches: false });
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-provenance')).toContainText('130 min budget');
    await expect(page.getByTestId('tonight-provenance')).toContainText('unseen first');
  });

  test('reshuffle walks the ranking and asks nothing', async () => {
    // §6.2 step 8: a browse gesture that asks nothing.
    await atTheDoor();
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-picks')).toBeVisible();
    const before = await page.getByTestId('tonight-picks').innerText();

    await page.getByTestId('tonight-reshuffle').click();
    await expect(page.getByTestId('tonight-round')).toHaveCount(0);
    await expect
      .poll(async () => page.getByTestId('tonight-picks').innerText(), { timeout: 10_000 })
      .not.toBe(before);

    // Decision 222: the walk "wraps", and says so. Pressed until it appears: which press wraps
    // depends on the pool's size.
    const wrapped = page.getByTestId('tonight-wrapped');
    for (let press = 0; press < 8 && !(await wrapped.isVisible()); press++) {
      await Promise.all([
        page.waitForResponse(
          (res) => res.request().method() === 'POST' && res.url().endsWith('/api/tonight/solo'),
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

  test('the round offers all four answers and locks the escape until pair 6', async () => {
    // Decision 154's four answers; 54c's escape is not drawn before it is available.
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
    await expect(page.getByTestId('tonight-round-count')).toContainText('often about');
    await expect(page.getByTestId('tonight-round-count')).not.toContainText('cap');

    // All four on screen at once, above the bottom bar where the nav is one (a desktop rail's
    // top is not a fold), and the pair side by side.
    const nav = await page.getByRole('navigation', { name: 'Surfaces' }).boundingBox();
    const viewport = page.viewportSize().height;
    const bottom = nav && nav.y > viewport / 2 ? nav.y : viewport;
    for (const id of ['tonight-pick-A', 'tonight-pick-B', 'tonight-answer-EITHER',
      'tonight-answer-NEITHER']) {
      const box = await page.getByTestId(id).boundingBox();
      expect(box, `${id} is not laid out`).not.toBeNull();
      expect(box.y, `${id} starts above the top of the page`).toBeGreaterThanOrEqual(0);
      expect(box.y + box.height, `${id} ends below the fold`).toBeLessThanOrEqual(bottom + 1);
    }
    const [a, b] = [
      await page.getByTestId('tonight-pick-A').boundingBox(),
      await page.getByTestId('tonight-pick-B').boundingBox()
    ];
    expect(Math.abs(a.y - b.y), 'the pair stacked instead of sitting side by side').toBeLessThan(2);

    // Resolve the room, so the next test's open-rooms list is its own.
    for (let i = 0; i < 24; i++) {
      if (!(await page.getByTestId('tonight-round').isVisible())) break;
      await Promise.all([
        page.waitForResponse(
          (res) =>
            res.request().method() === 'POST' && /\/api\/tonight\/seats\/\d+\/answer$/.test(res.url()),
          { timeout: 15_000 }
        ),
        page.getByTestId('tonight-pick-A').click()
      ]);
    }
  });

  test('the guest hand-off clears the screen and offers nothing early', async () => {
    // §6.2 step 2: nothing on the incoming screen may reach the previous participant's pairs.
    // On to 54e's reveal, which counts guest seats. Two rounds and two ballots outrun 60 s.
    test.setTimeout(300_000);
    await atTheDoor({ guests: 1 });
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await expect(page.getByTestId('tonight-seats').locator('li')).toHaveCount(2);
    await page.getByTestId('tonight-start').click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();

    // The initiator's own round is on screen; the guest's turn is not offered yet.
    await expect(page.locator('[data-testid^="tonight-hand-to-"]')).toHaveCount(0);

    await playOut();
    await expect(page.getByTestId('tonight-waiting')).toBeVisible();

    // Now — and only now — the phone offers the guest's turn.
    const handOff = page.locator('[data-testid^="tonight-hand-to-"]').first();
    await expect(handOff).toBeVisible();
    await handOff.click();

    const guestRound = page.getByTestId('tonight-round');
    await expect(guestRound).toBeVisible();
    await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    await expect(page.getByTestId('tonight-rail')).toHaveCount(0);

    // "no control on that screen, browser back and in-round history entries included, reaches
    // them". Falsifiable as a WRITE: Undo at the guest's pair 1 must not retract the host's last.
    const answered = async (participantId) => {
      const seen = await page.evaluate(async () => {
        const rooms = await (await fetch('/api/tonight/rooms')).json();
        const id = rooms.rooms.find((r) => r.viewer_seated).session_id;
        return (await (await fetch(`/api/tonight/sessions/${id}`)).json()).progress;
      });
      return seen.find((p) => p.participant_id === participantId).answered;
    };
    const undo = page.getByTestId('tonight-undo');
    const seats = await page.evaluate(async () => {
      const rooms = await (await fetch('/api/tonight/rooms')).json();
      const id = rooms.rooms.find((r) => r.viewer_seated).session_id;
      return (await (await fetch(`/api/tonight/sessions/${id}`)).json()).seats;
    });
    const hostSeat = seats.find((s) => s.seat === 1).participant_id;
    const guestSeat = seats.find((s) => s.role === 'guest').participant_id;
    const hostAnswers = await answered(hostSeat);
    expect(hostAnswers, 'the host answered, or the assertion below is vacuous').toBeGreaterThan(0);
    // An assertion, not a guard, and before the window below opens so it cannot spend it.
    await expect(undo).toBeVisible();
    // The TIMEOUT is the proof: a POST to the host's seat resolves this, and resolving fails.
    // Attached where the promise is made: a rejection with nothing listening fails the worker.
    const strayWrite = expect(
      page.waitForRequest(
        (req) => req.method() === 'POST' && req.url().includes(`/api/tonight/seats/${hostSeat}/`),
        { timeout: 2_000 }
      ),
      "the guest's screen wrote to the host's seat"
    ).rejects.toThrow();
    await undo.click();
    await strayWrite;
    expect(
      await answered(hostSeat),
      "the guest's screen reached the host's answers"
    ).toBe(hostAnswers);
    // And Undo here is about THIS seat's round, which has nothing behind it at pair 1.
    expect(
      await answered(guestSeat),
      "the guest's Undo moved a count that should have had nothing to take back"
    ).toBe(0);

    // And browser back does not re-render the round the phone just passed on from.
    await page.goBack();
    await page.waitForLoadState('domcontentloaded');
    if (await page.getByTestId('tonight-round').isVisible().catch(() => false)) {
      await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    }

    // --- and on through the guest's ballot to the reveal (M4.12) ----------------------------
    //
    // The restore forgets which seat the phone held and falls back to its own, ended one, so
    // the guest's turn is offered again, as after a screen lock.
    await page.goto('/tonight');
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    const backToGuest = page.getByTestId(`tonight-hand-to-${guestSeat}`);
    await expect(backToGuest).toBeVisible();
    await backToGuest.click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await playOut();

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
    // Submit within reach, above the bottom bar where the nav is one.
    const bar = await page.getByRole('navigation', { name: 'Surfaces' }).boundingBox();
    const height = page.viewportSize().height;
    const fold = bar && bar.y > height / 2 ? bar.y : height;
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
    await expect(page.getByTestId('tonight-approval-share')).toContainText(/\d+ of \d+ approved/);
  });

  test('a household frame does not take the guest off the phone', async () => {
    // A household-wide `rooms.changed` frame must re-read the seat this device is playing, not
    // the viewer's own.
    test.setTimeout(300_000);
    await atTheDoor({ guests: 1 });
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await page.getByTestId('tonight-start').click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await playOut();
    await expect(page.getByTestId('tonight-waiting')).toBeVisible();

    const handOff = page.locator('[data-testid^="tonight-hand-to-"]').first();
    await expect(handOff).toBeVisible();
    const guestSeat = (await handOff.getAttribute('data-testid')).replace('tonight-hand-to-', '');
    await handOff.click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    const showing = await page.getByTestId('tonight-round').innerText();

    // The RE-READ the frame provokes, not the screen alone, which would settle on the old DOM.
    const reread = page.waitForRequest(
      (req) => req.method() === 'GET' && /\/api\/tonight\/seats\/\d+\/round$/.test(req.url()),
      { timeout: 25_000 }
    );
    const elsewhere = await (
      await other.request.post('/api/tonight/sessions', { data: { guests: 0 } })
    ).json();
    expect(elsewhere.session_id, 'the second device opened no room, so no frame was sent').toBeTruthy();
    expect(
      (await reread).url(),
      'a household frame re-read a seat this phone is not playing'
    ).toContain(`/api/tonight/seats/${guestSeat}/round`);

    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    await expect(page.getByTestId('tonight-waiting')).toHaveCount(0);
    expect(
      await page.getByTestId('tonight-round').innerText(),
      "the guest's pair was replaced on the way through the frame"
    ).toBe(showing);

    // Both rooms closed, so the next test's open-rooms list is its own (decision 169).
    await other.request.post(`/api/tonight/sessions/${elsewhere.session_id}/end`);
    await page.getByTestId('tonight-end-room').click();
    await page.getByTestId('tonight-end-room-confirm').click();
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  });

  test('sharpen runs the round in place and says so in the provenance line', async () => {
    // 54f: "'sharpen this' runs the adaptive round and re-ranks in place, after which the
    // provenance line reads 'tilted by your N answers' instead of 'unseen first'". Over 60 s on
    // WebKit.
    test.setTimeout(300_000);
    // Rewatches in: the fixture is all seen. The tilt replaces the filter clause.
    await atTheDoor();
    const roomsBefore = (await (await page.request.get('/api/tonight/rooms')).json()).rooms.length;
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-picks').locator('li')).toHaveCount(3);
    await expect(page.getByTestId('tonight-provenance')).toContainText('rewatches included');

    await page.getByTestId('tonight-sharpen').click();
    await expect(page.getByTestId('tonight-sharpen-pair')).toBeVisible();
    for (const answer of ['A', 'B', 'EITHER', 'NEITHER']) {
      await expect(page.getByTestId(`tonight-sharpen-${answer}`)).toBeVisible();
    }

    // Answered until the line moves, up to three times: 54b's hold-out draw is keyed on the
    // user id (decision 223), so any one answer may legitimately not count.
    const provenance = page.getByTestId('tonight-provenance');
    const tilted = /tilted by your \d+ answers/;
    for (let press = 0; press < 3 && !tilted.test((await provenance.textContent()) ?? ''); press++) {
      if (!(await page.getByTestId('tonight-sharpen-pair').isVisible())) break;
      await page.getByTestId('tonight-sharpen-A').click();
      // The controls re-enable when the new payload is on screen.
      await expect(
        page.getByTestId('tonight-sharpen-A').or(page.getByTestId('tonight-sharpen-done')).first()
      ).toBeEnabled();
    }
    await expect(provenance, 'three answers and the line counted none of them').toContainText(
      tilted
    );
    await expect(provenance).not.toContainText('rewatches included');
    const line = (await provenance.textContent()) ?? '';
    await expect(page.getByTestId('tonight-solo')).toBeVisible();
    await expect(page.getByTestId('tonight-picks').locator('li')).toHaveCount(3);
    await expect(page.getByTestId('tonight-ballot')).toHaveCount(0);

    // Reshuffle inside the round is a browse gesture: no pair drawn is not a converged round, and
    // the answers survive the walk.
    await Promise.all([
      page.waitForResponse(
        (res) => res.request().method() === 'POST' && res.url().endsWith('/api/tonight/solo'),
        { timeout: 15_000 }
      ),
      page.getByTestId('tonight-reshuffle').click()
    ]);
    await expect(page.getByTestId('tonight-sharpen-done')).toHaveCount(0);
    await expect(page.getByTestId('tonight-sharpen')).toBeVisible();
    await expect(provenance, 'the walk dropped the answers the round had already taken').toHaveText(
      line
    );
    // And the control that came back answers the press with a pair or with "done", whichever
    // the round served: on the fixture's six films one answer can resolve the boundary (54c).
    const [asked] = await Promise.all([
      page.waitForResponse(
        (res) => res.request().method() === 'POST' && res.url().endsWith('/api/tonight/solo'),
        { timeout: 15_000 }
      ),
      page.getByTestId('tonight-sharpen').click()
    ]);
    const served = await asked.json();
    const shown = served.pair
      ? page.getByTestId('tonight-sharpen-pair')
      : page.getByTestId('tonight-sharpen-done');
    await expect(
      shown,
      `the round served ${served.stop_reason ?? 'a pair'} and the screen showed neither`
    ).toBeVisible();
    // Earlier tests leave rooms behind: the claim is that solo added none.
    const rooms = (await (await page.request.get('/api/tonight/rooms')).json()).rooms;
    expect(rooms.length, '§6.2 step 8: solo publishes no room').toBe(roomsBefore);
  });

  test('an answer carries the time it took, and the card budgets hold', async () => {
    // §4.2's `session_answer.latency_ms` is the CLIENT's measurement, so it is checked on the
    // request the client sends. The timeout budgets WebKit's slowing shared page, not the app:
    // the card budgets below are read off resource timing.
    test.setTimeout(300_000);
    await atTheDoor();
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await page.getByTestId('tonight-start').click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();

    // NOT a wait: the dwell under measurement.
    await page.waitForTimeout(DWELL_MS);
    const [answered] = await Promise.all([
      page.waitForResponse(
        (res) => res.request().method() === 'POST' && ANSWER.test(res.url()),
        { timeout: 15_000 }
      ),
      page.getByTestId('tonight-pick-A').click()
    ]);
    const sent = answered.request().postDataJSON();
    expect(
      sent.latency_ms,
      'the answer carries no measurement of how long its pair was on the screen'
    ).toBeGreaterThanOrEqual(DWELL_MS - CLOCK_SLOP_MS);
    // THIS card's time, not the evening's.
    expect(sent.latency_ms, 'the clock is the session\'s and not the card\'s').toBeLessThan(60_000);

    // The round trip, from resource timing. `responseEnd` is -1 until the body arrives, and
    // `waitForResponse` resolves on the headers.
    await answered.finished();
    const battle = answered.request().timing().responseEnd;
    expect(battle, 'no resource timing was recorded, so the budget below is vacuous').toBeGreaterThan(0);
    expect(battle, '§6 budgets a battle at 1.5 s').toBeLessThan(BATTLE_BUDGET_MS);

    // A sweep card on a series (the films are all rated), taken back afterwards so a re-run
    // measures the same thing.
    await page.request.post('/api/rate/session', {
      data: { mode: 'sweep', kinds: ['series'], restart: true }
    });
    await page.goto('/rate');
    await expect(page.getByTestId('rate-sweep-card')).toBeVisible();
    const [verdict] = await Promise.all([
      page.waitForResponse(
        (res) => res.request().method() === 'POST' && res.url().includes('/api/rate/verdict'),
        { timeout: 15_000 }
      ),
      page.getByTestId('rate-verdict-1').click()
    ]);
    expect(verdict.ok(), 'the verdict was refused, so the timing below is about nothing').toBeTruthy();
    await verdict.finished();  // as above: waitForResponse resolves on the headers
    const sweep = verdict.request().timing().responseEnd;
    expect(sweep, 'no resource timing was recorded, so the budget below is vacuous').toBeGreaterThan(0);
    expect(sweep, '§6 budgets a sweep card at 2 s').toBeLessThan(SWEEP_BUDGET_MS);

    const undone = await page.request.post('/api/rate/undo');
    expect(undone.ok(), `taking the measured verdict back (§6.1): ${undone.status()}`).toBeTruthy();
    await page.request.delete('/api/rate/session');

    await page.goto('/tonight');
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    await page.getByTestId('tonight-end-room').click();
    await page.getByTestId('tonight-end-room-confirm').click();
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  });

  test('the slider says the budget is soft, and opens where this member last left it', async () => {
    // Decision 506: the door says the budget admits up to 40 min over, and reopens at the budget
    // last used, per kind. Same timeout as above, for the same shared page.
    test.setTimeout(300_000);
    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-budget-soft')).toContainText('up to 40 min longer');
    await page.getByTestId('tonight-budget').fill('120');
    await expect(page.getByTestId('tonight-budget-value')).toContainText('120 min');
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-solo')).toBeVisible();

    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-budget-value')).toHaveText('120 min');

    // Back to the default, so the file leaves the phone as it found it.
    await page.getByTestId('tonight-budget').fill('130');
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-solo')).toBeVisible();
    await atTheDoor({ rewatches: false });
    await expect(page.getByTestId('tonight-budget-value')).toHaveText('130 min');
  });
});
