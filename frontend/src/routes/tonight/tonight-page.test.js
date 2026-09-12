/**
 * @vitest-environment jsdom
 *
 * What the Tonight surface DOES with the two controls this milestone adds, and the one thing it
 * must stop doing when it is navigated away from. Spec v2.1 §6.2 (rewritten: 54e), §6 preamble;
 * M4.12 findings 13 and 19.
 *
 * NAMED `tonight-page.test.js` and not `+page.svelte.test.js`, which is the name the convention
 * in `src/lib` would give it: SvelteKit reserves the `+` prefix inside `src/routes` and
 * `vite build` fails outright on any other `+`-named file. Same reason as `rate-page.test.js`.
 *
 * MOUNTED RATHER THAN IN THE STORE, for two claims that are only true of the rendered page.
 * Finding 19 is a lifecycle order — `onDestroy` runs before the continuation of an `onMount` that
 * is still awaiting — and no store test can produce it, because the bug is that the component
 * outlives its own bootstrap. Finding 13's hand-off is asserted at the store layer as well
 * (`tonight.svelte.test.js`), and the half that only exists here is that the control is DRAWN,
 * host-only, inside 54e's ballot: the store can be right while the screen offers nothing.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import TonightPage from './+page.svelte';
import { session } from '$lib/session.svelte.js';
import { leave, tonight } from '$lib/tonight.svelte.js';

/** Every socket the page opened. The page is the only caller of `connect` here, so a non-empty
 * list after an unmount is precisely the leak. */
const sockets = [];
class FakeSocket {
  constructor(url) {
    this.url = url;
    sockets.push(this);
  }
  close() {}
}

const guestSeat = {
  participant_id: 12,
  seat: 2,
  role: 'guest',
  user_id: null,
  name: 'Guest 1',
  ended_by: null
};
const hostSeat = {
  participant_id: 11,
  seat: 1,
  role: 'member',
  user_id: 1,
  name: 'Mia',
  ended_by: null
};

function reply(body) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers(),
    text: async () => JSON.stringify(body)
  };
}

let target;
let app;
let fetchMock;

beforeEach(() => {
  sockets.length = 0;
  vi.stubGlobal('WebSocket', FakeSocket);
  fetchMock = vi.fn(async () => reply({ rooms: [] }));
  vi.stubGlobal('fetch', fetchMock);
  leave();
  tonight.booted = true;
  tonight.loading = false;
  session.user = { id: 1, name: 'Mia', role: 'member', must_change_password: false };
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  vi.useRealTimers();
  if (app) unmount(app);
  app = null;
  target.remove();
  leave();
  session.user = null;
  vi.unstubAllGlobals();
});

describe('leaving the surface while it is still booting (finding 19)', () => {
  it('opens no channel for a page that has already been destroyed', async () => {
    // `onMount` is async: a navigation away can land between its await and its call to `watch`.
    // `onDestroy` goes first, while `disconnect` is still the no-op default, and the
    // continuation then opens a socket into a closure nobody will ever call — a live channel
    // still mutating `tonight.rooms` / `lobby` / `step` from a page that no longer exists, one
    // per fast navigation, for the life of the tab.
    /** @type {any} */
    let release;
    tonight.booted = false;
    fetchMock.mockImplementation(
      () => new Promise((resolve) => (release = () => resolve(reply({ rooms: [] }))))
    );

    app = mount(TonightPage, { target });
    flushSync();
    // The bootstrap is genuinely in flight, or the assertion at the end is vacuous: a page that
    // never started reading opens no socket for reasons that have nothing to do with the guard.
    expect(fetchMock, 'the bootstrap never started, so nothing below is tested').toHaveBeenCalled();
    expect(sockets, 'the bootstrap has not resolved, so nothing is watched yet').toHaveLength(0);

    unmount(app);
    app = null;
    release();
    // A real macrotask rather than a counted number of microtask turns: the continuation runs
    // through `fetch`, `res.text()`, `loadRooms` and `bootstrap` before it reaches `watch`, and a
    // test that stopped counting one turn early would assert the leak had not happened YET.
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(tonight.booted, 'the continuation never ran, so the guard was never reached').toBe(true);
    expect(sockets, 'a destroyed page opened a channel nothing can close').toHaveLength(0);
  });
});

describe("54e's ballot on the initiator's phone (finding 13)", () => {
  const inTheBallot = () => {
    tonight.lobby = {
      session_id: 7,
      room_code: 'MX-2210',
      state: 'ballot',
      host: { user_id: 1, name: 'Mia' },
      seats: [hostSeat, guestSeat],
      me: hostSeat
    };
    tonight.ballot = {
      slate: [{ title_id: 1, slot: 'finalist', name: 'Heat' }],
      submitted: 0,
      seated: 2
    };
    tonight.activeSeat = 11;
    tonight.step = 'ballot';
  };

  const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);

  it("draws the guest's turn inside the ballot, where the phone actually changes hands", () => {
    // The round had this control and the ballot did not, so a phone could carry a guest through
    // twenty pairs and then had no screen that could cast their vote — while
    // `ballot.submitted_count` counted them and the reveal waited. A room opened with any guest
    // could not finish.
    inTheBallot();
    app = mount(TonightPage, { target });
    flushSync();

    expect(byTestId('tonight-ballot')).not.toBeNull();
    expect(byTestId('tonight-ballot-to-12'), "the guest's turn is not on the ballot").not.toBeNull();
    expect(byTestId('tonight-ballot-to-12').textContent).toContain('pass to Guest 1');
    // Whose ballot is on screen, because on this phone it is not always its owner's.
    expect(byTestId('tonight-ballot-seat').textContent).toContain('Mia');
  });

  it("opens the guest's ballot blind, on the seat the phone is now holding", () => {
    // 54e's blindness across the hand-off and not only across the room: the incoming guest would
    // otherwise open on ticks somebody else made, and one tap on Submit would cast them as
    // theirs.
    inTheBallot();
    tonight.approved = [1];
    app = mount(TonightPage, { target });
    flushSync();

    byTestId('tonight-ballot-to-12').click();
    flushSync();

    expect(tonight.activeSeat).toBe(12);
    expect(tonight.approved, "the guest opened on the host's ticks").toEqual([]);
    expect(byTestId('tonight-approve-1').getAttribute('aria-pressed')).toBe('false');
    expect(byTestId('tonight-ballot-seat').textContent).toContain('Guest 1');
  });

  it("writes the ballot to the seat this phone is holding, not to the seat that owns it", async () => {
    // The other half of finding 13, and the half nothing below the browser could see: Submit was
    // bound to `tonight.lobby.me.participant_id`, so a guest's tap re-wrote the phone owner's
    // ballot, `submitted_count` stopped one short, and 54e's reveal waited on a vote no screen
    // could cast. Restoring that one expression at the click handler left the ENTIRE frontend
    // suite green -- the two tests above assert the controls are DRAWN, and the store's four call
    // `submitBallot` with an explicit argument, which is the binding's other side. So the seat
    // the write NAMES is asserted here, at the only layer besides Playwright that has both a
    // rendered hand-off and a request to read it off. §6.2 step 2 seats the guest on this phone;
    // which seat the POST addresses is the whole of the clause.
    // [finding 13; M4.12 review cycle 2: M412-FE-6]
    inTheBallot();
    /** Every POST the page made, by path: the seat the write names is in the URL and nowhere
     * else, which is why this is read off the request rather than off the store. */
    const posted = [];
    fetchMock.mockImplementation(async (path, opts = {}) => {
      if ((opts.method ?? 'GET') !== 'POST') return reply({ rooms: [] });
      posted.push(path);
      return reply({ submitted: 1, seated: 2, revealed: false });
    });
    app = mount(TonightPage, { target });
    flushSync();

    // The whole gesture, because the binding is only wrong once `activeSeat` and `me` disagree:
    // the phone changes hands, the guest ticks a card, and the guest taps Submit.
    byTestId('tonight-ballot-to-12').click();
    flushSync();
    byTestId('tonight-approve-1').click();
    flushSync();
    byTestId('tonight-submit-ballot').click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    flushSync();

    expect(posted, "Submit wrote the phone owner's seat, not the guest's").toEqual([
      '/api/tonight/seats/12/ballot'
    ]);
  });

  it('offers no hand-off to a member who is not hosting the room', () => {
    // The guest seats belong to whoever opened the room; a second member's device offering
    // "pass to Guest 1" would offer a vote that phone is not holding.
    inTheBallot();
    const jenny = { participant_id: 13, seat: 3, role: 'member', user_id: 2, name: 'Jenny' };
    tonight.lobby = { ...tonight.lobby, seats: [hostSeat, guestSeat, jenny], me: jenny };
    tonight.activeSeat = 13;
    session.user = { id: 2, name: 'Jenny', role: 'member', must_change_password: false };
    app = mount(TonightPage, { target });
    flushSync();

    expect(byTestId('tonight-ballot')).not.toBeNull();
    expect(byTestId('tonight-ballot-to-12')).toBeNull();
  });
});

describe('the clock behind the answer latency, across a navigation away (finding 41)', () => {
  /** One room, mid-round, answered by a router rather than by a queue: the claim is that the
   * remount re-reads the SAME card, and a queue cannot tell two reads apart. */
  const round = {
    participant_id: 11,
    answered: 0,
    cap: 20,
    ended_by: null,
    stop_reason: null,
    escape_available: false,
    card_token: 'card-11',
    pair: { a: { title_id: 1, name: 'Heat' }, b: { title_id: 2, name: 'Drive' } }
  };
  const room = {
    session_id: 7,
    room_code: 'MX-2210',
    state: 'voting',
    kind: 'movie',
    runtime_budget_min: 130,
    include_rewatches: false,
    started_at: null,
    host: { user_id: 1, name: 'Mia' },
    seats: [hostSeat, guestSeat],
    progress: [],
    ballot: { submitted: 0, seated: 2, revealed: false },
    me: hostSeat
  };

  /** Every POST the page made. `latency_ms` is the CLIENT's number — the backend writes whatever
   * arrives — so the request is where the instrument can be read at all. */
  let posted;

  function inTheRound() {
    posted = [];
    fetchMock.mockImplementation(async (path, opts = {}) => {
      const method = opts.method ?? 'GET';
      if (method === 'POST') posted.push({ path, body: JSON.parse(opts.body) });
      if (path === '/api/tonight/rooms') return reply({ rooms: [{ ...room, viewer_seated: true }] });
      if (path === '/api/tonight/sessions/7') return reply(room);
      if (/\/api\/tonight\/seats\/11\/(round|answer)$/.test(path)) return reply(round);
      throw new Error(`no fixture for ${method} ${path}`);
    });
  }

  /** A real macrotask, for the reason the finding-19 case spends one: the bootstrap runs through
   * `fetch`, `res.text()`, `loadRooms`, `refresh` and `loadRound` before a pair is on the screen,
   * and a counted number of microtask turns would assert against a page still arriving. Only the
   * clock is faked here, so the wait itself still ends — and `Date.now()` does not move across
   * it, which is what makes the measurement below an exact number rather than a range.
   */
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
    flushSync();
  };

  it('charges the answer the read after the remount, not the time the page was gone', async () => {
    // §6.2 step 4 keeps the phone in the person's hand for twenty pairs, and the nav rail is on
    // screen throughout: /tonight is not in the layout's `bare` list, so Rank and Home are one
    // tap from a live pair. A tap out and back destroys the page while the module keeps its
    // state, and the re-read that brings the pair back carries the same sealed card token — so
    // the clock armed before the tap was still running, and the next answer carried the whole
    // absence into an append-only §4.2 column that §14 risk 6 wants to re-tune the round from.
    vi.useFakeTimers({ toFake: ['Date'] });
    inTheRound();
    tonight.booted = false;

    app = mount(TonightPage, { target });
    await settle();
    expect(
      target.querySelector('[data-testid="tonight-round"]'),
      'the pair never reached the screen, so nothing below is measured'
    ).not.toBeNull();

    vi.setSystemTime(Date.now() + 300);
    unmount(app);
    app = null;
    flushSync();
    vi.setSystemTime(Date.now() + 180_000);

    app = mount(TonightPage, { target });
    await settle();
    expect(
      target.querySelector('[data-testid="tonight-round"]'),
      'the remount did not come back to the pair, so nothing below is measured'
    ).not.toBeNull();
    vi.setSystemTime(Date.now() + 900);
    posted.length = 0;
    target.querySelector('[data-testid="tonight-pick-A"]').click();
    await settle();

    const sent = posted.find((c) => c.path.endsWith('/seats/11/answer'));
    expect(sent, 'the answer never reached the network').toBeTruthy();
    expect(
      sent.body.latency_ms,
      'the answer was charged the time the page was not on the screen'
    ).toBeLessThan(60_000);
    expect(
      sent.body.latency_ms,
      "the remount did not re-arm the clock, so the 900 ms read went unmeasured"
    ).toBeGreaterThanOrEqual(900);
  });
});

describe("54f's Reshuffle, pressed inside the sharpen round (M412-SOLO-01)", () => {
  /** The three picks are the screen Reshuffle walks; only `pair` differs between the two
   * payloads, because the flag this milestone added is what decides whether the server selects a
   * pair at all (`sharpen: false` skips the selection, and a skipped selection is `pair: null`
   * with `stop_reason` null — indistinguishable on the wire from a converged round). */
  const picks = [
    { title_id: 1, name: 'Heat', why: 'slow burn', fit_line: 'fits your 130 min' },
    { title_id: 2, name: 'Drive', why: 'neon', fit_line: 'fits your 130 min' },
    { title_id: 3, name: 'Tampopo', why: 'noodles', fit_line: 'fits your 130 min' }
  ];
  const pair = {
    selection: 'straddle',
    reason: 'both near your line',
    a: { title_id: 1, name: 'Heat', year: 1995, fit_line: 'fits your 130 min' },
    b: { title_id: 2, name: 'Drive', year: 2011, fit_line: 'fits your 130 min' }
  };
  const solo = (over = {}) => ({
    picks,
    wildcard: null,
    provenance: 'tilted by your 1 answers',
    empty: null,
    answered: 1,
    sharpened: true,
    wrapped: false,
    pair: null,
    ...over
  });

  const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);

  it('comes back to the picks rather than reporting a round that is out of questions', async () => {
    // Reshuffle is a browse gesture and posts `sharpen: false`, so the server sends no pair back
    // — it was not asked for one. The screen read that as the round having converged: it printed
    // "nothing left to ask" over a round with pairs still in it and hid the only control that
    // could ask for one, leaving Back as the only way out — and Back clears the answers.
    tonight.solo = solo({ pair });
    tonight.step = 'solo';
    fetchMock.mockImplementation(async (path, opts = {}) => {
      if (path !== '/api/tonight/solo') throw new Error(`no fixture for ${path}`);
      const body = JSON.parse(opts.body);
      return reply(body.sharpen ? solo({ pair }) : solo());
    });

    app = mount(TonightPage, { target });
    flushSync();
    byTestId('tonight-sharpen').click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    flushSync();
    expect(byTestId('tonight-sharpen-pair'), 'the round never started').not.toBeNull();

    byTestId('tonight-reshuffle').click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    flushSync();

    expect(byTestId('tonight-picks'), 'the walk did not land back on the picks').not.toBeNull();
    expect(
      byTestId('tonight-sharpen-done'),
      'the screen says the round is out of questions, which the server never said'
    ).toBeNull();
    expect(
      byTestId('tonight-sharpen'),
      'the only control that can ask for a pair is gone, so Back is the only way out'
    ).not.toBeNull();
  });
});

describe("the budget the household is setting, on a series night (decision 219)", () => {
  const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);

  it('says the number it is setting is per episode once the kind is series', () => {
    // Decision 219 qualified every label that states a number on a series session -- the
    // candidate's fit line on both branches, and the open-rooms row -- and left out the one
    // control that SETS the number, which sits one row under the Series pill. The decision's own
    // argument is that "a bare 'fits your 60 min' reads as a promise about the evening", and the
    // measurement behind it is that the shipped series pool is 121 of 121 owned titles at 60,
    // 130 and 200 alike: the qualified labels report the number afterwards, so this readout is
    // where the misreading starts. Asserted here because it is a rendering fact -- the store
    // holds `controls.kind` and `controls.runtime_budget_min` and neither knows they are drawn
    // in the same row. [decision 219; §6.2 step 1 as amended by 54h; M4.12 review cycle 2: M412-FE-5]
    app = mount(TonightPage, { target });
    flushSync();

    const readout = () => byTestId('tonight-budget-value').textContent;
    expect(readout(), 'a film session counts the whole evening').not.toContain('per episode');

    byTestId('tonight-kind-series').click();
    flushSync();
    expect(readout(), 'the number under the Series pill still reads as the evening').toContain(
      'min per episode'
    );

    // And back, because the qualifier is the kind's and not the slider's -- a readout that kept
    // it would say "per episode" over a film night, which is the same defect pointing the other
    // way.
    byTestId('tonight-kind-movie').click();
    flushSync();
    expect(readout()).not.toContain('per episode');
  });
});
