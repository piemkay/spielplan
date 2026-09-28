/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import TonightPage from './+page.svelte';
import { session } from '$lib/session.svelte.js';
import { leave, tonight } from '$lib/tonight.svelte.js';
import { topbar } from '$lib/topbar.svelte.js';

// Recorded rather than stubbed: "the link was consumed" is the observable half of decision 481.
// `pushState` feeds the page store, so a sheet sees its own history entry and stays open.
const navigation = vi.hoisted(() => ({ replaced: [], state: {}, runs: new Set() }));
vi.mock('$app/navigation', () => ({
  replaceState: vi.fn((url) => {
    navigation.replaced.push(url);
  }),
  pushState: vi.fn((url, state) => {
    navigation.state = state;
    for (const run of navigation.runs) run({ url: new URL('http://localhost/tonight'), state });
  })
}));
vi.mock('$app/stores', () => ({
  page: {
    subscribe: (run) => {
      run({ url: new URL('http://localhost/tonight'), state: navigation.state });
      navigation.runs.add(run);
      return () => navigation.runs.delete(run);
    }
  }
}));

// The page is the only caller of `connect`, so a socket after an unmount is the leak.
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
  navigation.state = {};
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
    // A navigation away can land between `onMount`'s await and its `watch`; `onDestroy` runs first.
    /** @type {any} */
    let release;
    tonight.booted = false;
    fetchMock.mockImplementation(
      () => new Promise((resolve) => (release = () => resolve(reply({ rooms: [] }))))
    );

    app = mount(TonightPage, { target });
    flushSync();
    // The bootstrap must be in flight, or the final assertion is vacuous.
    expect(fetchMock, 'the bootstrap never started, so nothing below is tested').toHaveBeenCalled();
    expect(sockets, 'the bootstrap has not resolved, so nothing is watched yet').toHaveLength(0);

    unmount(app);
    app = null;
    release();
    // A macrotask, not counted microtasks: stopping one turn early would only assert "not yet".
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(tonight.booted, 'the continuation never ran, so the guard was never reached').toBe(true);
    expect(sockets, 'a destroyed page opened a channel nothing can close').toHaveLength(0);
  });
});

describe('the door opens on no large title (decision 528)', () => {
  it("hands the shell's top row its name and draws no heading of its own", () => {
    topbar.host = true;
    try {
      app = mount(TonightPage, { target });
      flushSync();
      expect(target.querySelector('[data-testid="tonight-controls"]')).not.toBeNull();
      expect(topbar.content, 'the shell was handed no title').not.toBeNull();
      expect(target.querySelector('h1'), 'the door drew its own title as well').toBeNull();
    } finally {
      topbar.host = false;
    }
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
    inTheBallot();
    app = mount(TonightPage, { target });
    flushSync();

    expect(byTestId('tonight-ballot')).not.toBeNull();
    expect(byTestId('tonight-ballot-to-12'), "the guest's turn is not on the ballot").not.toBeNull();
    expect(byTestId('tonight-ballot-to-12').textContent).toContain('Pass to Guest 1');
    // Whose ballot is on screen, because on this phone it is not always its owner's.
    expect(byTestId('tonight-ballot-seat').textContent).toContain('Mia');
  });

  it("opens the guest's ballot blind, on the seat the phone is now holding", () => {
    // Blind across the hand-off: the guest must not open on someone else's ticks.
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
    // The seat the POST names is the whole claim, and only the URL carries it.
    inTheBallot();
    const posted = [];
    fetchMock.mockImplementation(async (path, opts = {}) => {
      if ((opts.method ?? 'GET') !== 'POST') return reply({ rooms: [] });
      posted.push(path);
      return reply({ submitted: 1, seated: 2, revealed: false });
    });
    app = mount(TonightPage, { target });
    flushSync();

    // The binding is only wrong once `activeSeat` and `me` disagree, so walk the whole gesture.
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
    // Guest seats belong to the host's phone; another member's device holds no guest vote.
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
  /** Answered by a router, not a queue: the remount re-reads the same card. */
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

  /** `latency_ms` is the client's number, so the request is where it can be read. */
  let posted;

  function inTheRound() {
    posted = [];
    fetchMock.mockImplementation(async (path, opts = {}) => {
      const method = opts.method ?? 'GET';
      if (method === 'POST') posted.push({ path, body: JSON.parse(opts.body) });
      // The rooms list names its host by name, where the lobby carries the host's seat.
      if (path === '/api/tonight/rooms') {
        return reply({ rooms: [{ ...room, host: 'Mia', viewer_seated: true }] });
      }
      if (path === '/api/tonight/sessions/7') return reply(room);
      if (/\/api\/tonight\/seats\/11\/(round|answer)$/.test(path)) return reply(round);
      throw new Error(`no fixture for ${method} ${path}`);
    });
  }

  /** A macrotask; only Date is faked, so the wait ends and `Date.now()` does not move across it. */
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
    flushSync();
  };

  it('charges the answer the read after the remount, not the time the page was gone', async () => {
    // A tap out and back re-reads the same sealed card; the clock must not charge the absence.
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
  /** Only `pair` differs: Reshuffle's `sharpen: false` gets `pair: null`, which looks converged. */
  const picks = [
    { title_id: 1, name: 'Heat', why: 'Slow burn', fit_line: 'Fits your time' },
    { title_id: 2, name: 'Drive', why: 'Neon', fit_line: 'Fits your time' },
    { title_id: 3, name: 'Tampopo', why: 'Noodles', fit_line: 'Fits your time' }
  ];
  const pair = {
    selection: 'straddle',
    reason: 'both near your line',
    a: { title_id: 1, name: 'Heat', year: 1995, fit_line: 'Fits your time' },
    b: { title_id: 2, name: 'Drive', year: 2011, fit_line: 'Fits your time' }
  };
  const solo = (over = {}) => ({
    picks,
    wildcard: null,
    provenance: 'Tilted by your 1 answer · fits in 2h 10m',
    empty: null,
    answered: 1,
    sharpened: true,
    wrapped: false,
    pair: null,
    ...over
  });

  const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);

  it('comes back to the picks rather than reporting a round that is out of questions', async () => {
    // Reshuffle asks for no pair, so no pair back is not a converged round.
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

  it('offers Play on every pick and the wildcard, and says so when there is no link', () => {
    // Decision 527: every solo pick carries Play, not the first one only.
    const linked = (t) => ({ ...t, play_url: `http://jf.test/web/#/details?id=jf-${t.title_id}` });
    tonight.solo = solo({
      picks: picks.map(linked),
      wildcard: { ...linked({ title_id: 4, name: 'Hamnet', fit_line: '15 min over' }), why: 'A step outside your usual' }
    });
    tonight.step = 'solo';
    app = mount(TonightPage, { target });
    flushSync();

    for (const id of [1, 2, 3, 4]) {
      expect(byTestId(`tonight-play-${id}`)?.getAttribute('href'), `no Play on ${id}`).toBe(
        `http://jf.test/web/#/details?id=jf-${id}`
      );
    }
    expect(byTestId('tonight-solo-wildcard').textContent).toContain('Wildcard');
    unmount(app);

    // Two different sentences: no server, and a title the server does not hold.
    tonight.solo = solo({
      picks: [{ ...picks[0], play_reason: 'not_in_library' }, ...picks.slice(1).map((t) => ({ ...t, play_reason: 'no_server' }))]
    });
    app = mount(TonightPage, { target });
    flushSync();
    expect(byTestId('tonight-play-1'), 'a Play with nowhere to go').toBeNull();
    const hero = byTestId('tonight-pick-1').querySelector('button.play');
    expect(hero.disabled, 'an unavailable Play that still reads as a button to press').toBe(true);
    expect(document.getElementById(hero.getAttribute('aria-describedby')).textContent).toBe(
      'Not in your Jellyfin library.'
    );
    const row = byTestId('tonight-pick-2').querySelector('button');
    expect(row.disabled).toBe(true);
    expect(row.getAttribute('aria-label')).toContain("Jellyfin isn't connected");
  });
});

describe("the budget the household is setting, on a series night (decision 219)", () => {
  const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);

  it('says the number it is setting is per episode once the kind is series', () => {
    // The control that sets the number sits under the Series thumb, so it must say "per episode".
    app = mount(TonightPage, { target });
    flushSync();
    byTestId('tonight-settings').click();
    flushSync();

    const readout = () => byTestId('tonight-budget-value').textContent;
    expect(readout(), 'a film session counts the whole evening').not.toContain('per episode');

    byTestId('tonight-kind-series').click();
    flushSync();
    expect(readout(), 'the number under the Series thumb still reads as the evening').toContain(
      'per episode'
    );
    expect(byTestId('tonight-summary').textContent, 'and so does the row it opened from').toContain(
      'per episode'
    );

    // And back: the qualifier belongs to the kind, not the slider.
    byTestId('tonight-kind-movie').click();
    flushSync();
    expect(readout()).not.toContain('per episode');
  });

  it('counts guests with a stepper that stops at none and at the most one phone holds', () => {
    app = mount(TonightPage, { target });
    flushSync();
    byTestId('tonight-settings').click();
    flushSync();

    expect(byTestId('tonight-guests-less').disabled, 'fewer than no guests').toBe(true);
    byTestId('tonight-guests-more').click();
    flushSync();
    expect(byTestId('tonight-guests').textContent).toBe('1');
    expect(byTestId('tonight-summary').parentElement.textContent).toContain('1 guest');
    tonight.controls.guests = 0;
  });
});

describe('the first household evening, on the screen (owner instruction of 2026-09-25)', () => {
  const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);
  const room = {
    session_id: 7,
    room_code: 'QC-4397',
    state: 'voting',
    host: { user_id: 1, name: 'Mia' },
    seats: [hostSeat],
    me: hostSeat,
    vetoes: [],
    veto_options: [
      { key: 'violence', label: 'violence' },
      { key: 'harrowing', label: 'harrowing' }
    ]
  };

  it('draws the pair as two shared posters, never as buttons wearing the global 2:3 frame', () => {
    // A button with class "poster" would take design.css's 2:3 frame and fill the phone.
    tonight.lobby = room;
    tonight.round = {
      participant_id: 11, answered: 0, cap: 20, typical: 10, ended_by: null, stop_reason: null,
      escape_available: false, card_token: 'card-11',
      pair: { a: { title_id: 5, name: 'Heat', year: 1995 }, b: { title_id: 6, name: 'Drive' } }
    };
    tonight.step = 'round';
    app = mount(TonightPage, { target });
    flushSync();

    for (const [side, id] of [['A', '5'], ['B', '6']]) {
      const pick = byTestId(`tonight-pick-${side}`);
      expect(pick.classList.contains('poster'), 'the button wears the global 2:3 frame').toBe(false);
      const art = pick.querySelector('[data-testid="rate-poster"]');
      expect(art, 'no shared poster inside the pick').not.toBeNull();
      expect(art.getAttribute('data-title-id'), 'the poster was not keyed on the title').toBe(id);
    }
    expect(byTestId('tonight-round-count').textContent).toContain('Pair 1 · usually about 10');
    expect(byTestId('tonight-round-count').textContent).not.toContain('cap');
  });

  it('makes the ballot options and Submit two different objects, and Submit counts its picks', () => {
    tonight.lobby = { ...room, state: 'ballot' };
    tonight.ballot = {
      slate: [
        { title_id: 1, slot: 'finalist', name: 'Heat' },
        { title_id: 2, slot: 'wildcard', name: 'Tampopo' }
      ],
      submitted: 0,
      seated: 2
    };
    tonight.activeSeat = 11;
    tonight.step = 'ballot';
    app = mount(TonightPage, { target });
    flushSync();

    const submit = byTestId('tonight-submit-ballot');
    expect(submit.classList.contains('pill'), 'Submit looks like one more option').toBe(false);
    expect(submit.textContent).toContain('none of these');
    byTestId('tonight-approve-1').click();
    flushSync();
    expect(byTestId('tonight-approve-1').getAttribute('aria-pressed')).toBe('true');
    expect(submit.textContent).toContain('Submit 1 pick');
    expect(byTestId('tonight-approve-2').textContent).toContain('A step outside your usual');
  });

  it('shows the ballot status after this phone has voted, never the round counts', () => {
    tonight.lobby = { ...room, state: 'ballot' };
    tonight.ballot = { slate: [], submitted: 1, seated: 2 };
    tonight.progress = [{ name: 'Mia', answered: 1, expected: 10, finished: true }];
    tonight.step = 'waiting';
    app = mount(TonightPage, { target });
    flushSync();

    expect(byTestId('tonight-ballot-waiting').textContent).toBe('Your vote is in · waiting for 1 more');
    expect(byTestId('tonight-progress'), 'the round counts under a ballot that is waiting').toBeNull();
  });

  it('offers the phone to one guest at a time, and names who follows', () => {
    // One next step: guests take their turns in seat order (§6.2 step 2).
    const guest = (n) => ({ ...guestSeat, participant_id: 20 + n, seat: 1 + n, name: `Guest ${n}` });
    tonight.lobby = { ...room, seats: [{ ...hostSeat, ended_by: 'converged' }, guest(1), guest(2), guest(3)] };
    tonight.round = { participant_id: 11, answered: 6, pair: null };
    tonight.activeSeat = 11;
    tonight.step = 'waiting';
    app = mount(TonightPage, { target });
    flushSync();

    const primaries = target.querySelectorAll('[data-testid="tonight-waiting"] .btn-primary');
    expect(primaries, 'more than one primary action on the screen').toHaveLength(1);
    expect(byTestId('tonight-hand-to-21').textContent).toBe('Pass the phone to Guest 1');
    expect(byTestId('tonight-hand-to-22'), 'a later guest offered ahead of their turn').toBeNull();
    expect(byTestId('tonight-later-turns').textContent.trim()).toBe('Then: Guest 2, Guest 3');
  });

  it("says where each of the others is, beside that person's own avatar", () => {
    // One person, one colour: keyed on the account (a stored colour first), never on the name.
    const jenny = { ...hostSeat, participant_id: 13, seat: 2, user_id: 2, name: 'Jenny', account_role: 'admin' };
    const patrick = { ...hostSeat, participant_id: 14, seat: 3, user_id: 3, name: 'Patrick', colour: '#7a55b8' };
    tonight.lobby = { ...room, seats: [hostSeat, jenny, patrick, { ...guestSeat, seat: 4 }] };
    tonight.progress = [
      { participant_id: 11, name: 'Mia', answered: 2, expected: 10, finished: false },
      { participant_id: 13, name: 'Jenny', answered: 3, expected: 10, finished: false, answer: 'NEITHER' },
      { participant_id: 14, name: 'Patrick', answered: 5, expected: 10, finished: true },
      { participant_id: 12, name: 'Guest 1', answered: 0, expected: 10, finished: false }
    ];
    tonight.round = {
      participant_id: 11, answered: 2, typical: 10, escape_available: false, card_token: 'card-11',
      pair: { a: { title_id: 5, name: 'Heat' }, b: { title_id: 6, name: 'Drive' } }
    };
    tonight.activeSeat = 11;
    tonight.step = 'round';
    app = mount(TonightPage, { target });
    flushSync();

    const rows = [...byTestId('tonight-round-progress').querySelectorAll('li')];
    expect(rows.map((r) => r.lastElementChild.textContent)).toEqual([
      'Jenny 3/~10',
      'Patrick 5/5 done',
      'Guest 1 0/~10'
    ]);
    const tint = (row) => row.querySelector('.avatar').style.background;
    expect(tint(rows[0]), 'the admin reads graphite wherever they appear').toBe('rgb(107, 99, 91)');
    expect(tint(rows[1]), "a person's stored colour").toBe('rgb(122, 85, 184)');
    expect(tint(rows[2]), 'a guest has no account to take a colour from').toBe('');
  });

  it("reveals each person's breadth and a seat's own pick, and never a bare 'Unanimous'", () => {
    tonight.lobby = { ...room, state: 'resolved' };
    tonight.result = {
      beat: "Tonight's pick",
      approval_share: 1,
      participants: 2,
      winner: {
        title_id: 5, name: 'Eternal Sunshine', approvals: 2, match_lines: [], fit_line: 'fits',
        reserved: false, reserved_for: { participant_id: 12, name: 'Jenny' }
      },
      breadth: [
        { participant_id: 11, name: 'Patrick', approved: 4, of: 4, said_yes: true, only_yes: false },
        { participant_id: 12, name: 'Jenny', approved: 1, of: 4, said_yes: true, only_yes: true }
      ],
      runners_up: [],
      wildcard: null,
      finalists: []
    };
    tonight.step = 'reveal';
    app = mount(TonightPage, { target });
    flushSync();

    expect(target.textContent).not.toContain('Unanimous');
    expect(byTestId('tonight-breadth').textContent).toBe(
      'Patrick said yes to 4 of 4 · Jenny said yes to 1 of 4'
    );
    expect(byTestId('tonight-only-yes').textContent).toBe('The only one Jenny said yes to');
    expect(byTestId('tonight-approval-share').textContent).toBe('Both of you said yes');
    expect(byTestId('tonight-reserved-for').textContent).toContain("Jenny's pick");
    expect(byTestId('tonight-reserved'), 'the axis label over a seat pick').toBeNull();
    expect(byTestId('tonight-winner').querySelector('[data-testid="rate-poster"]')).not.toBeNull();
    expect(target.querySelector('.reveal.playing'), "a reload replays the evening's beat").toBeNull();
  });

  it('draws the stage before the result lands, so the last voter never sees an empty flow', () => {
    // The last ballot sets the step before the result read answers.
    tonight.lobby = { ...room, state: 'ballot' };
    tonight.ballot = { slate: [{ title_id: 1, slot: 'finalist', name: 'Heat' }], submitted: 1, seated: 2 };
    tonight.activeSeat = 11;
    tonight.step = 'ballot';
    app = mount(TonightPage, { target });
    flushSync();

    tonight.step = 'reveal';
    flushSync();
    expect(byTestId('tonight-beat').textContent).toBe("Tonight's pick");
    expect(byTestId('tonight-reveal'), 'drawn before there is a result').toBeNull();

    tonight.result = {
      participants: 2, approval_share: 1, breadth: [], runners_up: [], wildcard: null,
      winner: { title_id: 1, name: 'Heat', match_lines: [], fit_line: '' }
    };
    flushSync();
    expect(byTestId('tonight-winner').textContent).toContain('Heat');
    expect(target.querySelector('.reveal.playing'), 'this page saw the votes land').not.toBeNull();
  });

  it('offers the lobby its share link and the not-tonight chips', () => {
    // The chips are this phone's own (decision 505); the other member's are named beneath.
    const violence = [{ key: 'violence', label: 'violence' }];
    const theirs = ['horror', 'harrowing', 'sexual_violence'].map((k) => ({ key: k, label: k }));
    const other = { ...hostSeat, participant_id: 13, user_id: 2, name: 'Jenny', vetoes: theirs };
    tonight.lobby = {
      ...room,
      state: 'open',
      vetoes: [...violence, ...theirs],
      seats: [{ ...hostSeat, vetoes: violence }, other],
      me: { ...hostSeat, vetoes: violence }
    };
    tonight.step = 'lobby';
    app = mount(TonightPage, { target });
    flushSync();

    expect(byTestId('tonight-share')).not.toBeNull();
    expect(byTestId('tonight-share-caption').textContent).not.toContain('send the link');
    expect(byTestId('tonight-veto-violence').getAttribute('aria-pressed')).toBe('true');
    expect(byTestId('tonight-veto-harrowing').getAttribute('aria-pressed')).toBe('false');
    expect(
      byTestId('tonight-veto-harrowing').disabled,
      "another member's three do not use up this phone's own"
    ).toBe(false);
    expect(byTestId('tonight-others-vetoes').textContent).toBe(
      'Jenny: horror, harrowing, sexual_violence'
    );
    expect(byTestId('tonight-mood-caption').textContent).toContain('Neither tonight');
  });

  it('describes each title on a pair card for somebody who does not know it', () => {
    tonight.lobby = room;
    tonight.round = {
      participant_id: 11, answered: 12, cap: 20, typical: 10, ended_by: null, stop_reason: null,
      escape_available: true, card_token: 'card-11',
      pair: {
        a: { title_id: 5, name: 'Warriors of the Wind', year: 1984, kind: 'movie',
             runtime_min: 117, genres: ['Adventure', 'Animation'] },
        b: { title_id: 6, name: 'Wicked', year: 2024, kind: 'movie', runtime_min: 160,
             over_budget_min: 40, fit_line: '40 min over', genres: ['Drama', 'Fantasy'] }
      }
    };
    tonight.step = 'round';
    app = mount(TonightPage, { target });
    flushSync();

    const facts = (side) =>
      [...target.querySelectorAll(`[data-testid="tonight-pair-fact-${side}"]`)].map((n) => n.textContent);
    expect(facts('A')).toEqual(['1984 · 1h 57m', 'Adventure, Animation']);
    expect(facts('B')).toEqual(['2024 · 2h 40m', '40 min over', 'Drama, Fantasy']);
    expect(byTestId('tonight-round-count').textContent).toBe('Pair 13 · longer than most · max 20');
  });

  it('says under the slider that the budget is soft', () => {
    tonight.step = 'door';
    tonight.booted = true;
    app = mount(TonightPage, { target });
    flushSync();
    byTestId('tonight-settings').click();
    flushSync();
    expect(byTestId('tonight-budget-soft').textContent).toContain('A little over is fine');
  });

  it('lists the wildcard once, with its own count, and on the winner card when it won', () => {
    const base = {
      beat: "Tonight's pick", approval_share: 1, participants: 2, breadth: [],
      finalists: []
    };
    tonight.lobby = { ...room, state: 'resolved' };
    tonight.result = {
      ...base,
      winner: { title_id: 1, name: 'Raiders', approvals: 2, match_lines: [], label: null },
      runners_up: [{ title_id: 2, name: 'Kiki', approvals: 1, slot: 'finalist' }],
      wildcard: { title_id: 4, name: 'Everything Everywhere All at Once', approvals: 0,
                  label: 'A step outside your usual', slot: 'wildcard' }
    };
    tonight.step = 'reveal';
    app = mount(TonightPage, { target });
    flushSync();
    expect(byTestId('tonight-runner-up-4'), 'the wildcard among the runners-up').toBeNull();
    expect(byTestId('tonight-wildcard-line').textContent.trim()).toBe(
      'A step outside your usual · 0 of 2 said yes'
    );
    expect(byTestId('tonight-winner-label')).toBeNull();
    unmount(app);

    tonight.result = {
      ...base,
      winner: { title_id: 4, name: 'Everything Everywhere All at Once', approvals: 2,
                match_lines: [], label: 'A step outside your usual', slot: 'wildcard' },
      runners_up: [{ title_id: 2, name: 'Kiki', approvals: 0, slot: 'finalist' }],
      wildcard: null
    };
    app = mount(TonightPage, { target });
    flushSync();
    expect(byTestId('tonight-wildcard')).toBeNull();
    expect(byTestId('tonight-winner-label').textContent).toBe('A step outside your usual');
  });

  it('joins the room a ?room= link names, and takes the link off the address bar', async () => {
    navigation.replaced.length = 0;
    window.history.replaceState({}, '', '/tonight?room=QC-4397');
    tonight.booted = false;
    const posts = [];
    fetchMock.mockImplementation(async (path, opts = {}) => {
      if ((opts.method ?? 'GET') === 'POST') {
        posts.push({ path, body: JSON.parse(opts.body) });
        return reply({ session_id: 7, participant_id: 11, lobby: { ...room, state: 'open' } });
      }
      return reply({ rooms: [] });
    });
    try {
      app = mount(TonightPage, { target });
      await new Promise((resolve) => setTimeout(resolve, 10));
      flushSync();
    } finally {
      window.history.replaceState({}, '', '/');
    }

    expect(posts).toEqual([
      { path: '/api/tonight/sessions/join', body: { session_id: null, room_code: 'QC-4397' } }
    ]);
    expect(navigation.replaced, 'the followed link stayed in the address bar').toEqual(['/tonight']);
    expect(byTestId('tonight-lobby'), 'the link did not land in the room').not.toBeNull();
    expect(sockets.at(-1)?.url, 'and the device is not watching the room it joined').toContain(
      'session_id=7'
    );
  });
});
