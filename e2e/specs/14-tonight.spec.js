import { expect, test } from '@playwright/test';

import { createMember, seedFilmLedger, signInAsMember, signedIn, waitForPool } from '../helpers.js';

/**
 * §6.2's Tonight surface, driven in a browser. Spec v2.1 §6.2 as rewritten (54a–54g), §6.8.
 *
 * Two coverage rows closed here at M4 and neither could close anywhere cheaper.
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
 * Three M4.12 rows close here too, and each for the same kind of reason — a claim about one
 * screen, on one device, that nothing below the browser can hold.
 *
 * `tonight-a-guest-seat-reaches-the-reveal` — 54e's reveal waits on `submitted_count`, which
 * counts guest seats, so a room opened with any guest could never reach it at all: Submit was
 * bound to the viewer's own seat and a guest seat carries `user_id NULL`. The backend already
 * permitted the write (a host may write to a guest seat in a session they host), so what was
 * missing was a *control* — and a control is only ever missing from a browser.
 *
 * `tonight-the-device-remembers-which-seat-it-is-playing` — a household frame is a real event
 * sent by another device, and the clobber it used to cause happened inside the handler that
 * read it. Nothing else in this suite has both a live socket and a rendered guest turn to lose.
 *
 * `tonight-the-answer-latency-is-a-measurement` — `session_answer.latency_ms` was computed one
 * statement after it was started, so every row ever written is 0. The value is the CLIENT's, so
 * the only place the repair can be checked is the request the client actually sends; and §6's
 * preamble budgets the two card writes this app makes ("<2 s per sweep card, <1.5 s per
 * battle"), which nothing measured on the phone project at all.
 *
 * ONE PAGE FOR THE FILE, like `13-rank.spec.js`: §4.2's observations are append-only, so a
 * shared account cannot be rewound between runs — and Playwright's default context-per-test
 * would throw away the member the seeding built. It runs on the phone project as well as
 * desktop, because §6.2 step 2's hand-the-phone is a claim about a phone.
 *
 * ONE SECOND CONTEXT, for one claim: a `rooms.changed` frame is broadcast to every device in
 * the household when any of them opens a room, and a single context cannot send itself one
 * about a room it is not in.
 */

/** §6.2's round is one POST per pair, so the thing to wait on is the write itself. */
const ANSWER = /\/api\/tonight\/seats\/\d+\/answer$/;

/** How long the pair is deliberately left on the screen before it is answered, and the margin
 * allowed against it. This is the QUANTITY being measured rather than a wait for anything:
 * `latency_ms` is how long a card was in front of a person, so an instrument for it has to
 * spend time in front of one. The margin is there because the clock is the phone's. */
const DWELL_MS = 400;
const CLOCK_SLOP_MS = 100;

/** §6's preamble, verbatim: "<2 s per sweep card, <1.5 s per battle". A Tonight pair is a
 * battle; §6.1's sweep card is the other question this app asks. */
const BATTLE_BUDGET_MS = 1_500;
const SWEEP_BUDGET_MS = 2_000;

test.describe('tonight', () => {
  /** @type {import('@playwright/test').Page} */
  let page;
  /** The second household device, never seated in the room `page` is in.
   * @type {import('@playwright/test').Page} */
  let other;
  /** @type {import('@playwright/test').BrowserContext} */
  let otherContext;
  /** The member's own name, which is what `tonight-ballot-seat` renders for its seat. */
  let memberName;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    // §5.3's fold-in tick writes `user_score` once a minute, and this hook waits for it for
    // each member in turn. The file's own budget is the config's 60 s test timeout, which is
    // shorter than the thing being waited for.
    test.setTimeout(360_000);
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    const config = await (await page.request.get('/api/config')).json();
    test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
    // One account per project, reused across runs rather than minted anew: nothing ever removed
    // the timestamped ones, and §6.6's roster on a box that has run this suite all week is the
    // evidence. [M4.8, finding 8]
    const member = await createMember(page, `tonight-${testInfo.project.name}`, { reuse: true });
    memberName = member.name;
    await signInAsMember(page, member);
    await seedFilmLedger(page);
    // §5.3 writes `user_score` on the fold-in tick, not on the verdict — without this every
    // assertion below would fail on §6.2's empty pool for a reason unrelated to what it tests.
    await waitForPool(page);

    // The second household device, signed in as the admin the way `15-tonight-group.spec.js`
    // keeps its second page. The account only has to be IN the household and out of the room:
    // `rooms.changed` is broadcast household-wide on every open, start and end, and the defect
    // it used to cause — the guest's turn replaced by the phone owner's ended seat — happens
    // inside the handler that reads it. [M4.12 finding 14]
    otherContext = await browser.newContext({ baseURL });
    other = await otherContext.newPage();
    await signedIn(other);
  });

  test.afterAll(async () => {
    // A hook has its own budget: `test.setTimeout` sets the RUNNING slot's timeout, so the 300 s
    // the eleventh test raises is that test's, and the teardown after it is still charged the
    // config's 60 s. It does not fit in one. `page.close()` -- the page holding §6.2's live
    // socket -- returned in 41 ms; `otherContext.close()` was 59.5 s into a call that had not
    // returned when the clock expired, on a context idle on Home since `beforeAll`. Nothing in
    // the app is holding it open: the identical eleven tests cost 7.3 s on desktop and 332.3 s
    // on the phone project, and on that same project the files that open a context per test
    // (02-shell, 03-library, 06-responsive, 19-phone-shell) run at desktop speed while the two
    // that keep ONE PAGE FOR THE FILE are the only slow ones. What is paid for here is a
    // thirteen-minute-old WebKit context being torn down, so this is a ceiling for the harness
    // and not a claim about the app -- the same 300 s the four tests above already carry.
    test.setTimeout(300_000);
    await page?.close();
    await otherContext?.close();
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
    const controls = page.getByTestId('tonight-controls');
    // Settle on ONE of the two before reading either. `isVisible()` is a point-in-time read, and
    // the surface has a moment where neither is drawn — the step has moved into a room but that
    // room's payload has not arrived, so the door is gone and its replacement is not there yet.
    // Reading `back` inside that window answers "not visible", the click is skipped, and the
    // room paints a beat later over an assertion that is already waiting for the door.
    await expect(back.or(controls).first()).toBeVisible();
    if (await back.isVisible()) await back.click();
    await expect(controls).toBeVisible();
    if (rewatches) await page.getByTestId('tonight-rewatches').check();
    if (guests) await page.getByTestId('tonight-guests').fill(String(guests));
  }

  /** Answer through whichever round is on the screen, to its end.
   *
   * The write, not a guess at how long it takes: §6.2's round is one POST per pair, so the
   * thing to wait for is the answer itself — the 120 ms sleep this pattern replaced was long
   * enough on the machine it was written on and a race everywhere slower, and a round that fell
   * behind it met the next pair with a click meant for the last one. [M4.8, finding 8]
   *
   * Named here because the work M4.12 adds needs it three more times. `the round offers all
   * four answers` keeps its own copy, where the loop is that test's cleanup rather than its
   * subject and its comment argues for it in place.
   */
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

  /** How many approvals are ticked on the ballot currently drawn.
   *
   * Asked as a locator count rather than by reading `aria-pressed` off each button, because the
   * claim 54e's hand-off makes is about the whole screen: "none of the previous person's are
   * visible" is falsified by one. */
  const ticked = (p) => p.locator('[data-testid^="tonight-approve-"][aria-pressed="true"]');

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
    const readout = page.getByTestId('tonight-budget-value');
    await expect(readout).toContainText('130 min');

    // Decision 219: on a series night the budget bounds minutes PER EPISODE, and the labels that
    // say so are downstream of this row -- the candidate's fit line and the open-rooms line both
    // report the number after the household has already read it here, one row under the Series
    // pill. Kind and budget are set in the same card, so the pairing is what this test is for.
    // [M4.12 review cycle 2: M412-FE-5]
    await page.getByTestId('tonight-kind-series').click();
    await expect(readout).toContainText('130 min per episode');
    // Back to the kind the rest of this file's fixture library is, and the qualifier with it.
    await page.getByTestId('tonight-kind-movie').click();
    await expect(readout).not.toContainText('per episode');
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

    // 54f's walk "wraps", and decision 222 says so out loud beside this control — which is the
    // whole reason `wrapped` was kept on the payload rather than deleted: a press that returns
    // titles the household was looking at a moment ago is legible instead of broken. The flag is
    // asserted per press at the integration layer; what a browser proves is that the household is
    // shown anything at all.
    //
    // PRESSED UNTIL IT APPEARS rather than a counted number of times. The first press that wraps
    // is a function of the pool the seeded ledger admits (the walk is three titles wide, so it is
    // the second press on this fixture's six owned films), and a hard-coded count would be an
    // assertion about the fixture's size rather than about the rule. Once it turns on it stays
    // on, so a press spent against a screen that had already wrapped costs nothing.
    // [decision 222; M4.12 review cycle 1: M412-SOLO-03]
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
      // The write, not a guess at how long it takes. §6.2's round is one POST per pair, so the
      // answer to wait for is the answer itself: the 120 ms sleep this replaces was long enough
      // on the machine it was written on and a race everywhere slower, and a round that fell
      // behind it met the next pair with a click meant for the last one. [M4.8, finding 8]
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
    // §6.2 step 2, and the reason this row is e2e: on one device the previous participant's
    // pairs have already been delivered to the client. Nothing on the incoming screen may
    // reach them.
    //
    // It runs to 54e's REVEAL from M4.12 on, because `ballot.submitted_count` counts guest
    // seats and Submit was bound to the viewer's own seat: a room opened with any guest could
    // not reach the reveal at all, ever, and the count stopped one short for the life of the
    // evening. Two rounds and two ballots is more than the config's 60 s.
    // [M4.12 finding 13]
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
    // The screen is the guest's, not a continuation of the host's: the counter restarts, and
    // the host's own model-log lines are not on it.
    await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    await expect(page.getByTestId('tonight-rail')).toHaveCount(0);

    // "no control on that screen, browser back and in-round history entries included, reaches
    // them". Asserting that the host's pairs are not *drawn* is close to vacuous — no template
    // draws them. What is falsifiable is whether a control on the guest's screen can still
    // WRITE to the host's round: Undo at the guest's pair 1 has no answer of its own to take
    // back, and an undo scoped to "the last answer in this session" rather than "your own last
    // live answer" would silently retract the host's twentieth.
    const answered = async (participantId) => {
      const seen = await page.evaluate(async () => {
        const rooms = await (await fetch('/api/tonight/rooms')).json();
        const id = rooms.rooms.find((r) => r.viewer_seated).session_id;
        return (await (await fetch(`/api/tonight/sessions/${id}`)).json()).progress;
      });
      return seen.find((p) => p.participant_id === participantId).answered;
    };
    const undo = page.getByTestId('tonight-undo');
    // Both seats by id, so every claim below names the write it forbids rather than forbidding
    // writes in general — the guest's own seat is allowed to answer and to retract, and the
    // positive half of this check is about exactly that seat.
    const seats = await page.evaluate(async () => {
      const rooms = await (await fetch('/api/tonight/rooms')).json();
      const id = rooms.rooms.find((r) => r.viewer_seated).session_id;
      return (await (await fetch(`/api/tonight/sessions/${id}`)).json()).seats;
    });
    const hostSeat = seats.find((s) => s.seat === 1).participant_id;
    const guestSeat = seats.find((s) => s.role === 'guest').participant_id;
    const hostAnswers = await answered(hostSeat);
    expect(hostAnswers, 'the host answered, or the assertion below is vacuous').toBeGreaterThan(0);
    // Undo is rendered unconditionally (`+page.svelte`'s round block draws it beside the escape,
    // with no guard), so this is an assertion and not a guard — the `if (await
    // undo.isVisible().catch(() => false))` it replaces could only ever do two things: click, or
    // silently skip the one click every assertion below is about, which a screen with no Undo at
    // all did while passing. (The panel's other suggestion for this block — `toHaveCount(0)` on
    // Undo — would fail against a correct app for the same reason, and is deliberately NOT
    // applied.) Made BEFORE the window below opens, so waiting for the control cannot spend it.
    // [M4.12 finding 46]
    await expect(undo).toBeVisible();
    // A negative cannot be proven by sleeping: the 300 ms wait this replaces would have passed a
    // retraction that took 301 ms, and it was the only thing standing between an undo scoped to
    // "the last answer in this session" and a silent green. Watch for the write instead and let
    // the TIMEOUT be the proof — a POST to the host's seat RESOLVES this promise, and resolving
    // is the failure. The window is also longer than the sleep was, so the count re-read below
    // settles for longer than before, not less. [M4.8, finding 8]
    //
    // The handler is attached where the promise is MADE, not after the click, because here the
    // rejection is the expected outcome rather than a symptom of a run already failing. A click
    // that out-runs the 2 s window — an actionability retry under load, a re-render — rejects
    // with nothing listening, and Playwright's worker turns an unhandled rejection into a
    // failure of whatever test is running and then stops the worker, so the whole file would go
    // red reporting "Timeout 2000ms exceeded" against no assertion on a run in which nothing was
    // written to the host's seat at all. That is the inversion this milestone exists to prevent,
    // committed by its own new code. Same shape as `:191`'s `Promise.all`. [M4.8 review, E2E-2]
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
    // The positive half, and the half the negative above cannot carry: "nothing was written to
    // the host's seat" is satisfied by a guest screen that wrote nowhere at all, or by an Undo
    // that was never clicked. What the hand-off promises is that Undo on this screen is about
    // THIS seat's round — which at pair 1 has nothing behind it, so the guest's own count is
    // still 0 whether the retraction was refused or was a no-op. [M4.12 finding 46]
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
    // `goBack` left the phone on whatever it came from, and the surface restores a device into
    // the room it is still seated in — which is also how this evening carries on. The restore
    // is the honest way back rather than a convenience: it forgets which seat the phone was
    // holding, `refresh` falls back to this device's own, and that seat has ended, so the
    // waiting screen offers the guest's turn again. A phone that locks does the same thing.
    await page.goto('/tonight');
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    const backToGuest = page.getByTestId(`tonight-hand-to-${guestSeat}`);
    await expect(backToGuest).toBeVisible();
    await backToGuest.click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await playOut();

    // Every seat has ended, so the room settles and 54e's ballot opens — on the seat this phone
    // was acting for, which is still the guest's. Blind on arrival: nobody has voted at all
    // yet, so nothing here is anybody's leaked tick, and the assertion is the baseline the
    // hand-off below is measured against.
    await expect(page.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId('tonight-ballot-seat')).toHaveText('Guest 1');
    await expect(ticked(page)).toHaveCount(0);

    // The phone is put down and picked up again. It forgets which seat it was holding — the
    // submitted seats are this device's own record, not the room's, since the payload carries a
    // count and not a per-seat flag — so `refresh` falls back to its owner's ballot. That is
    // the state 54e's hand-off exists for, and before this milestone the ballot block had no
    // control in it at all: the guest's vote was unreachable and the room waited for it for
    // ever. [M4.12 finding 13]
    await page.goto('/tonight');
    await expect(page.getByTestId('tonight-ballot')).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId('tonight-ballot-seat')).toHaveText(memberName);

    const options = page.locator('[data-testid^="tonight-approve-"]');
    const first = await options.first().getAttribute('data-testid');
    await page.getByTestId(first).click();
    await expect(ticked(page), 'the owner voted, or the blindness claim below is vacuous').toHaveCount(1);
    await page.getByTestId('tonight-submit-ballot').click();

    // One submission is not every submission (54e). The hand-off control is waited for FIRST,
    // because it is the screen the submit produces: asserting the absence of the reveal before
    // the write has landed would pass against a build that reveals on one vote, which is the
    // one thing 54e forbids.
    const handBallot = page.getByTestId(`tonight-ballot-to-${guestSeat}`);
    await expect(handBallot).toBeVisible();
    await expect(page.getByTestId('tonight-reveal')).toHaveCount(0);
    await expect(options, 'the slate is still offered to a seat that has voted').toHaveCount(0);
    await handBallot.click();

    // 54e's blindness ACROSS the hand-off, which is the half the fix itself could have broken:
    // the approvals are held in the client, so an incoming guest handed this phone would have
    // opened on the previous person's ticks — and on one device the transport protects nothing,
    // because those ticks were never sent anywhere.
    await expect(page.getByTestId('tonight-ballot-seat')).toHaveText('Guest 1');
    await expect(ticked(page), "the guest opened on the owner's selections").toHaveCount(0);

    await page.getByTestId(first).click();
    await page.getByTestId('tonight-submit-ballot').click();

    // Every seat has submitted, so the room reveals — which is the thing a room with any guest
    // seat could not do at all.
    await expect(page.getByTestId('tonight-reveal')).toBeVisible({ timeout: 25_000 });
    await expect(page.getByTestId('tonight-approval-share')).toContainText(/\d+ of \d+ approved/);
  });

  test('a household frame does not take the guest off the phone', async () => {
    // §6.2 step 2's hand-the-phone, against §6.2 rewrite step 2's live channel. `refresh()`
    // re-read the VIEWER's own seat on every `rooms.changed` frame — broadcast household-wide
    // whenever any device opens, starts or ends a room — on every lobby and reveal frame, and
    // after every socket reconnect, which a screen lock is enough to cause. Nothing recorded
    // which seat this device was acting for, so the one surface pass-the-phone exists for had
    // no representation at all and a guest mid-turn was replaced by the phone owner's ended
    // round. [M4.12 finding 14]
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

    // The frame: a second household device opening a room of its own, which is the ordinary
    // event, not a contrived one. What is asserted is the RE-READ the frame provokes and not
    // the screen alone — the clobber happened inside that handler, so the falsifiable claim is
    // which seat this device asks about next, and a screen assertion on its own would settle
    // against the old DOM a beat before the replacement arrived.
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

    // And the guest's pair is still the one on the screen afterwards.
    await expect(page.getByTestId('tonight-round')).toBeVisible();
    await expect(page.getByTestId('tonight-round-count')).toContainText('pair 1');
    await expect(page.getByTestId('tonight-waiting')).toHaveCount(0);
    expect(
      await page.getByTestId('tonight-round').innerText(),
      "the guest's pair was replaced on the way through the frame"
    ).toBe(showing);

    // Both rooms closed, so the next test's open-rooms list is its own. Decision 169's host-only
    // control is the household's own way out of a room that has stopped progressing, and this
    // one has: the guest is one pair in and nobody is going to finish it.
    await other.request.post(`/api/tonight/sessions/${elsewhere.session_id}/end`);
    await page.getByTestId('tonight-end-room').click();
    await page.getByTestId('tonight-end-room-confirm').click();
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  });

  test('sharpen runs the round in place and says so in the provenance line', async () => {
    // 54f: solo's round is optional and it re-ranks the picks that are already on screen —
    // "'sharpen this' runs the adaptive round and re-ranks in place, after which the provenance
    // line reads 'tilted by your N answers' instead of 'unseen first'". The integration layer
    // owns the re-ranking; what a browser proves is that it happens on the same screen, with no
    // ballot and no room, and that the line changes.
    // Measured at 1.0 minutes on the phone project against the config's 60 s, in the run before
    // this line existed and the run after -- so the failure it produced was the timeout and not
    // the assertion the report named. M4.12 gave this test two more blocks of round trips (the
    // Reshuffle-from-inside-the-round claim, and M412-SOLO-01's press), on top of the door, the
    // four-answer sweep and up to three counted presses: nine POSTs through the selector on
    // WebKit. Same budget and same reason as the two tests above it. [M4.12 finding 13's shape]
    test.setTimeout(300_000);
    // Rewatches in, because the fixture library is entirely seen and the unseen-only pool is
    // empty — the provenance line's other form is asserted by the test above. Which filter
    // clause it starts from does not matter here: what 54f fixes is that the tilt arrives
    // "instead of" the filter, not appended to it, so this asserts the clause is *gone*.
    await atTheDoor();
    const roomsBefore = (await (await page.request.get('/api/tonight/rooms')).json()).rooms.length;
    await page.getByTestId('tonight-solo-door').click();
    await expect(page.getByTestId('tonight-picks').locator('li')).toHaveCount(3);
    await expect(page.getByTestId('tonight-provenance')).toContainText('rewatches included');

    await page.getByTestId('tonight-sharpen').click();
    await expect(page.getByTestId('tonight-sharpen-pair')).toBeVisible();
    // All four answers, here as everywhere (decision 154).
    for (const answer of ['A', 'B', 'EITHER', 'NEITHER']) {
      await expect(page.getByTestId(`tonight-sharpen-${answer}`)).toBeVisible();
    }

    // ANSWERED UNTIL THE LINE MOVES, bounded, rather than exactly once. 54b's hold-out arm is a
    // RATE drawn from a stable key now rather than every tenth seq (decision 223), and solo keys
    // it on `user.id` -- so whether the FIRST answer counts is a property of which id this
    // project's account happened to be minted with and not of the code under test. Account 6,
    // 21, 43, 48 or 58 holds out at seq 1; nothing pins the number (`createMember` reuses by
    // NAME and `run.mjs` restarts the identity sequence), so one account added to or removed
    // from an earlier spec slides this one onto a hazardous id and a correct 54b hold-out --
    // which costs one of the twenty and moves nothing -- is reported here as a regression. A
    // press spent on one costs a press; two consecutive draws is one in a hundred and no id below
    // 400 draws both, so three presses bound a one-in-a-thousand coincidence. Same shape as
    // `reshuffle walks the ranking` above, and as the backend tests that compute the first
    // held-out seq rather than assume it. [decision 223; M4.12 review cycle 2: M412-SOLO-07]
    const provenance = page.getByTestId('tonight-provenance');
    const tilted = /tilted by your \d+ answers/;
    for (let press = 0; press < 3 && !tilted.test((await provenance.textContent()) ?? ''); press++) {
      // A round that has run out of questions has no button to press, and the assertion below
      // settles the render before this is read, so this is the loop's exit and not a race.
      if (!(await page.getByTestId('tonight-sharpen-pair').isVisible())) break;
      await page.getByTestId('tonight-sharpen-A').click();
      // The new payload is on the screen when the round's own controls come back: `sharpen` holds
      // `tonight.busy` across the post and the answer buttons are disabled for its length, so a
      // line read before this is the line the press replaced. `sharpen-done` is the other end of
      // the same render -- a round that converged on this answer has no button to re-enable.
      await expect(
        page.getByTestId('tonight-sharpen-A').or(page.getByTestId('tonight-sharpen-done')).first()
      ).toBeEnabled();
    }
    await expect(provenance, 'three answers and the line counted none of them').toContainText(
      tilted
    );
    await expect(provenance).not.toContainText('rewatches included');
    // Read back rather than spelled, because how many of the presses counted is the draw's and
    // the walk below has to come back to THIS line.
    const line = (await provenance.textContent()) ?? '';
    // In place: still the picks screen, still three picks, and no room was opened for it.
    await expect(page.getByTestId('tonight-solo')).toBeVisible();
    await expect(page.getByTestId('tonight-picks').locator('li')).toHaveCount(3);
    await expect(page.getByTestId('tonight-ballot')).toHaveCount(0);

    // And Reshuffle, pressed from inside the round, is still the browse gesture it is at the
    // door. It posts `sharpen: false`, so the server draws no pair — it was not asked for one —
    // and the screen used to read that as the round having converged: it printed "nothing left to
    // ask" over a round with pairs still in it and hid the only control that could ask for one,
    // leaving Back, which discards the evening's answers. The answers survive the walk, which is
    // what lets the round resume on the next tap. [M4.12 review cycle 1: M412-SOLO-01]
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
    // AND THE CONTROL THAT CAME BACK ANSWERS THE PRESS, with whichever of the two the round has
    // left. Which one that is belongs to the pool and not to this spec: 54c ends a round "when
    // the shortlist boundary is resolved", and the fixture library is six films, so the two
    // titles flanking the cut between ranks 3 and 4 sit 0.09 from it under a unit prior --
    // inside BOUNDARY_Z's 0.6-sigma band, and the only two inside it. One answer between them
    // resolves the boundary, and `POST /api/tonight/solo` on this stack comes back
    // `stop_reason: converged` from the first counted answer: driven against it, the door
    // serves the 5-vs-8 pair and every press after that one answer serves none. Demanding a
    // pair here asserted the fixture's pool size rather than the defect above, and reported a
    // round the app had legitimately finished as a broken screen. So the claim is the screen
    // against what the round actually served -- a pair draws the question, a resolved round
    // says so -- and what M412-SOLO-01 left behind is neither of them. [54c; decision 214]
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
    // Earlier tests in this file leave live rooms behind, so the claim is that solo added none
    // — not that the household has none.
    const rooms = (await (await page.request.get('/api/tonight/rooms')).json()).rooms;
    expect(rooms.length, '§6.2 step 8: solo publishes no room').toBe(roomsBefore);
  });

  test('an answer carries the time it took, and the card budgets hold', async () => {
    // §4.2's `session_answer.latency_ms`, and §6's preamble around it: "<2 s per sweep card,
    // <1.5 s per battle". The elapsed time was evaluated INSIDE the object literal one
    // statement after it was started, so every row ever written is 0 — and §4.2 is append-only,
    // so evenings already played can never be re-measured. §14 risk 6 makes this instrument the
    // precondition for re-tuning the round, and a zero reads as a measurement rather than as a
    // bug. The value is the CLIENT's, so the only place the repair can be checked is the
    // request the client actually sends. [M4.12 findings 41, 47]
    //
    // Measured at 61.2 s on the phone project against the config's 60 s, in a run where every
    // assertion below PASSED and the clock expired in the after-hooks -- the same failure the
    // sharpen test above records, on the same budget and for the same reason. The cost is the
    // file's shared page rather than this test's work: §4.2's observations are append-only, so the
    // context is opened once in `beforeAll` and lives for the file, and the median click in it
    // grows monotonically from 148 ms in the first test to 4811 ms in this one, the eleventh,
    // while the backend answers at a 7 ms median throughout. What this test claims about speed is
    // §6's two card budgets below, and both are read off resource timing rather than off the wall
    // clock -- so the ceiling is a budget for WebKit's actionability round trips and for nothing
    // else.
    test.setTimeout(300_000);
    await atTheDoor();
    await page.getByTestId('tonight-open').click();
    await expect(page.getByTestId('tonight-lobby')).toBeVisible();
    await page.getByTestId('tonight-start').click();
    await expect(page.getByTestId('tonight-round')).toBeVisible();

    // NOT a wait for anything to happen — this is the quantity under measurement. The pair is
    // on the screen and the person is reading it; `latency_ms` is how long that lasts, so an
    // instrument for it has to spend time in front of the card and then ask what was recorded.
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
    // And it is THIS card's time rather than the evening's: a clock armed once at the door
    // would grow without bound across a twenty-pair round and satisfy the line above on every
    // one of them.
    expect(sent.latency_ms, 'the clock is the session\'s and not the card\'s').toBeLessThan(60_000);

    // §6's battle budget, on the phone project, against the fixture — the round trip rather
    // than the paint, because what §6 budgets is the card cadence and the paint is a frame
    // after the payload. Read from the resource timing so the assertion is about the app and
    // not about Playwright's actionability retries around the click.
    // `responseEnd` is -1 until the body has actually arrived: Playwright fills the timing in as
    // the response completes, and `waitForResponse` resolves on the HEADERS. Reading it straight
    // off the resolved response measured nothing and said so -- which is what the guard below is
    // for, and why it is written before the budget rather than after it.
    await answered.finished();
    const battle = answered.request().timing().responseEnd;
    expect(battle, 'no resource timing was recorded, so the budget below is vacuous').toBeGreaterThan(0);
    expect(battle, '§6 budgets a battle at 1.5 s').toBeLessThan(BATTLE_BUDGET_MS);

    // The other half of §6's line, on the same phone. §6.1's sweep card is the app's other
    // question, and this member's FILMS are all rated by `seedFilmLedger` while the fixture's
    // two series are not — which is what makes a card available here without seeding anything
    // new. The verdict is taken back at the end, so the sweep pool is left exactly as this test
    // found it and a re-run measures the same thing rather than draining towards a skip.
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

    // §6's "undo everywhere", used here to give back what this test borrowed: undo is the only
    // code permitted to delete a verdict, so the title returns to §6.1's queue and this member's
    // seen-state is where it was.
    const undone = await page.request.post('/api/rate/undo');
    expect(undone.ok(), `taking the measured verdict back (§6.1): ${undone.status()}`).toBeTruthy();
    await page.request.delete('/api/rate/session');

    // And the room this test opened is one answer in, with nobody to finish it.
    await page.goto('/tonight');
    await expect(page.getByTestId('tonight-surface')).toBeVisible();
    await expect(page.getByTestId('tonight-booting')).toHaveCount(0);
    await page.getByTestId('tonight-end-room').click();
    await page.getByTestId('tonight-end-room-confirm').click();
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
  });
});
