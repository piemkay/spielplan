import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ANSWERS,
  BUDGET_DEFAULT,
  BUDGET_MAX,
  BUDGET_MIN,
  BUDGET_STEP,
  ESCAPE_LABEL,
  JOIN_CAPTION,
  MAX_GUESTS,
  RECONNECT_MAX_MS,
  REVEAL_BEAT,
  answer,
  approvalShare,
  ballotTurns,
  connect,
  endRoom,
  escape,
  handBallot,
  leave,
  loadRooms,
  loadRound,
  loadSolo,
  minutesAgo,
  progressLine,
  reconnectDelay,
  refresh,
  roomLine,
  submitBallot,
  toggleApproval,
  tonight,
  undo
} from './tonight.svelte.js';

/**
 * The Tonight client's pure helpers. Spec v2.1 §6.2 (rewritten), §6.8.
 *
 * Three of these render a spec sentence, so the test is what keeps the sentence from drifting:
 * §6.2 step 2's open-rooms row, 54c's waiting line, and §6.8's data voice on the approval share.
 * The fourth — the four answers — is decision 154's whole content.
 */

describe('the open-rooms row (§6.2 step 2)', () => {
  const room = {
    room_code: 'MX-2210',
    host: 'Mia',
    started_at: new Date(Date.now() - 3 * 60000).toISOString(),
    kind: 'movie',
    runtime_budget_min: 60,
    skips_seen: true
  };

  it('reads the way the spec writes it', () => {
    // §6.2's own example: "MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen".
    expect(roomLine(room)).toBe('MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen');
  });

  it('says the opposite when rewatches are in', () => {
    expect(roomLine({ ...room, skips_seen: false })).toContain('includes rewatches');
  });

  it('names the kind the way the controls do', () => {
    expect(roomLine({ ...room, kind: 'series' })).toContain('Series');
  });

  it('drops the age rather than printing a lie when the timestamp is missing', () => {
    // A row is still a row without an age; "NaN min ago" is worse than one fewer facet.
    expect(roomLine({ ...room, started_at: null })).not.toContain('min ago');
    expect(roomLine({ ...room, started_at: null })).toContain('MX-2210');
    expect(minutesAgo('not-a-date')).toBeNull();
    expect(minutesAgo(null)).toBeNull();
  });

  it('never reports a negative age from a clock that is slightly ahead', () => {
    const ahead = new Date(Date.now() + 30000).toISOString();
    expect(minutesAgo(ahead)).toBe(0);
  });
});

describe('the waiting line (54c)', () => {
  // The rows carry an answer and the pair it was about, which the live payload does not: 54c
  // keeps both off the wire. They are here so the claim below can fail — grepping a line built
  // from `name`, `answered`, `expected` and `finished` for words no field can supply asserts
  // nothing about the renderer, only about the fixture. [M4.10 finding 33]
  const progress = [
    { name: 'Patrick', answered: 6, expected: 20, finished: true, answer: 'NEITHER', pair: 'Heat' },
    { name: 'Jenny', answered: 9, expected: 20, finished: false, answer: 'EITHER', pair: 'Drive' },
    { name: 'Mia', answered: 4, expected: 20, finished: false, answer: 'A', pair: 'Sicario' }
  ];

  it('shows counts and names, and nothing that could be an answer', () => {
    // 54c: "progress and never their answers". The payload cannot carry them; this is the
    // second half — the renderer must not draw them even when they are handed to it.
    const line = progressLine(progress);
    expect(line).toContain('Patrick 6/6 done');
    expect(line).toContain('Jenny 9/~20');
    expect(line).toContain('waiting for 2');
    expect(line, "a seat's answer reached the waiting line").not.toMatch(/EITHER|NEITHER/i);
    expect(line, 'the pair a seat answered about reached the waiting line').not.toMatch(
      /Heat|Drive|Sicario/
    );
  });

  it('stops saying "waiting" once everybody has finished', () => {
    const done = progress.map((p) => ({ ...p, finished: true }));
    expect(progressLine(done)).not.toContain('waiting for');
  });

  it('is empty rather than wrong with nobody seated', () => {
    expect(progressLine([])).toBe('');
  });
});

describe('the approval share (§6.8, §13)', () => {
  it('is a count next to its name, never a bare number', () => {
    // §6.8: model numbers appear in the data voice next to their name, never bare. §13 makes
    // this the headline metric for the whole feature.
    expect(approvalShare({ approval_share: 0.75, participants: 4 })).toBe('3 of 4 approved');
    expect(approvalShare({ approval_share: 1, participants: 2 })).toBe('2 of 2 approved');
    expect(approvalShare({ approval_share: 0, participants: 3 })).toBe('0 of 3 approved');
  });

  it('says nothing at all before there is a result', () => {
    expect(approvalShare(null)).toBe('');
  });
});

describe('the constants the spec fixes', () => {
  it('offers exactly decision 154\'s four answers', () => {
    expect(ANSWERS.map((a) => a.value)).toEqual(['A', 'B', 'EITHER', 'NEITHER']);
    // The two level answers are opposite signals, so their copy has to be opposite too — the
    // prototype's "Neither pulls me tonight" is §6.2's own string.
    expect(ANSWERS.find((a) => a.value === 'EITHER').label).toBe('Either is fine');
    expect(ANSWERS.find((a) => a.value === 'NEITHER').label).toBe('Neither pulls me tonight');
  });

  it('bounds the runtime slider', () => {
    expect([BUDGET_MIN, BUDGET_MAX, BUDGET_STEP, BUDGET_DEFAULT]).toEqual([60, 200, 5, 130]);
    expect(BUDGET_DEFAULT % BUDGET_STEP).toBe(0);
  });

  it('caps the guests who share one phone', () => {
    expect(MAX_GUESTS).toBe(6);
  });

  it('keeps the two strings the spec fixes verbatim', () => {
    expect(REVEAL_BEAT).toBe('VOTES REVEALED TOGETHER');
    expect(ESCAPE_LABEL).toBe('just pick for us');
    expect(JOIN_CAPTION).toContain('Push is best effort');
  });
});

describe('stepping back out to the door (§6.2 step 2)', () => {
  it('drops the room, not the seat', () => {
    // The restore that keeps a reload from stranding a participant gives a household with one
    // live room no other door — every visit lands back inside it. `leave` is the way out, and it
    // is a client-side step: nothing here calls the API, because the seat, the answers and the
    // ballot stay on the server for `resume` to come back to.
    Object.assign(tonight, {
      step: 'ballot',
      lobby: { session_id: 7 },
      round: { pair: {} },
      ballot: { slate: [] },
      result: { winner: {} },
      progress: [{ answered: 3 }],
      approved: [11]
    });

    leave();

    expect(tonight.step).toBe('door');
    for (const held of ['lobby', 'round', 'ballot', 'result']) expect(tonight[held]).toBeNull();
    for (const held of ['progress', 'approved']) expect(tonight[held]).toEqual([]);
  });

  it('clears the lobby, because a frame would otherwise drag the device back in', () => {
    // Every channel frame ends in `refresh`, and `refresh` recomputes the step from the server
    // for whoever still holds a lobby. Leaving only the step behind left a device that stepped
    // out being pulled back by the next frame anybody else's device caused — which is how the
    // e2e found it: the back control worked, and then undid itself.
    Object.assign(tonight, { step: 'ballot', lobby: { session_id: 7 } });
    leave();
    expect(tonight.lobby).toBeNull();
  });

  it("keeps the controls, because they are the door's and not the room's", () => {
    // §6.2 step 1 puts the three controls before the fork. Coming back to a door that had
    // forgotten the budget you set would make stepping out cost something.
    tonight.controls.runtime_budget_min = 95;
    tonight.controls.include_rewatches = true;
    leave();
    expect(tonight.controls.runtime_budget_min).toBe(95);
    expect(tonight.controls.include_rewatches).toBe(true);
  });
});

// --- the room this device is in ---------------------------------------------------------------

/**
 * One mutable room, answered by a router rather than by a queue of responses.
 *
 * A queue cannot tell two reads apart, and every claim below is about WHICH seat was read: the
 * defect these cover is a device re-reading the wrong participant, and a fixture that answers the
 * same body to `/seats/11/round` and `/seats/12/round` would pass against it.
 */
let world;
let calls;
/** Held loosely on purpose: assigning a vitest mock to the global narrows it to the DOM `fetch`
 * signature, and every fixture below then reads as an error to the type checker. The same note
 * `rate.svelte.test.js` carries. */
/** @type {any} */
let fetchMock;

const seatOf = (id, over = {}) => ({
  participant_id: id,
  seat: id - 10,
  role: id === 11 ? 'member' : 'guest',
  user_id: id === 11 ? 1 : null,
  name: id === 11 ? 'Mia' : `Guest ${id - 11}`,
  answered_count: 0,
  ended_by: null,
  ...over
});

const roomOf = (over = {}) => ({
  session_id: 7,
  room_code: 'MX-2210',
  state: 'voting',
  kind: 'movie',
  runtime_budget_min: 130,
  include_rewatches: false,
  started_at: null,
  host: { user_id: 1, name: 'Mia' },
  seats: [seatOf(11), seatOf(12)],
  progress: [],
  ballot: { submitted: 0, seated: 2, revealed: false },
  me: seatOf(11),
  ...over
});

const roundOf = (participantId, over = {}) => ({
  participant_id: participantId,
  answered: 0,
  cap: 20,
  ended_by: null,
  stop_reason: null,
  escape_available: false,
  card_token: `card-${participantId}`,
  pair: { a: { title_id: 1, name: 'Heat' }, b: { title_id: 2, name: 'Drive' } },
  ...over
});

function reply(body, status = 200) {
  return {
    ok: status < 400,
    status,
    statusText: status < 400 ? 'OK' : 'Conflict',
    headers: new Headers(),
    text: async () => JSON.stringify(body)
  };
}

function router() {
  return vi.fn(async (path, opts = {}) => {
    const method = opts.method ?? 'GET';
    calls.push({ method, path, body: opts.body ? JSON.parse(opts.body) : null });
    const onSeat = path.match(/^\/api\/tonight\/seats\/(\d+)\/(round|answer|undo|escape|ballot)$/);
    if (onSeat) {
      const id = Number(onSeat[1]);
      if (onSeat[2] === 'ballot') return reply({ submitted: 1, seated: 2, revealed: false });
      if (onSeat[2] === 'escape') return reply(roundOf(id, { pair: null, ended_by: 'escape' }));
      return reply(roundOf(id));
    }
    if (/^\/api\/tonight\/sessions\/\d+\/end$/.test(path)) {
      world.room = roomOf({ state: 'abandoned' });
      return reply({ session_id: 7, state: 'abandoned' });
    }
    if (/^\/api\/tonight\/sessions\/\d+\/ballot$/.test(path)) {
      return reply({
        session_id: 7,
        slate: [{ title_id: 1, slot: 'finalist', name: 'Heat' }],
        submitted: 0,
        seated: 2,
        revealed: false
      });
    }
    if (/^\/api\/tonight\/sessions\/\d+$/.test(path)) return reply(world.room);
    if (path === '/api/tonight/rooms') return reply({ rooms: [] });
    if (path === '/api/tonight/solo') return reply({ picks: [], provenance: 'x', pair: null });
    throw new Error(`no fixture for ${method} ${path}`);
  });
}

/** Every socket `connect` has opened, in order. Declared at module scope because the Svelte
 * plugin's `perf_avoid_nested_class` check reads test files too, and a warning on every run is
 * how a real one stops being read. */
const sockets = [];
class FakeSocket {
  constructor(url) {
    this.url = url;
    sockets.push(this);
  }
  close() {}
}

/** The paths of every request of one shape, in order: the instrument for "which seat was read". */
const read = (re) => calls.filter((c) => re.test(c.path)).map((c) => c.path);
const posted = (path) => calls.find((c) => c.method === 'POST' && c.path === path)?.body ?? null;

function freshStore() {
  leave();
  tonight.solo = null;
  tonight.soloAnswers = [];
  tonight.soloOffset = 0;
  tonight.busy = false;
}

beforeEach(() => {
  world = { room: roomOf() };
  calls = [];
  fetchMock = router();
  vi.stubGlobal('fetch', fetchMock);
  freshStore();
});

afterEach(() => {
  freshStore();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('the seat this device is playing (§6.2 step 2; findings 13, 14, 15)', () => {
  it("keeps a guest's round on screen when a household frame re-reads the room", async () => {
    // §6.2 step 2 puts guests on the initiator's phone, and every re-read named `seen.me` — the
    // phone's OWNER. A re-read happens on every rooms.changed frame, every lobby and reveal frame
    // and after every socket reconnect, so a second room opening anywhere in the household
    // replaced the guest's pair with the host's mid-turn. [finding 14]
    tonight.lobby = { session_id: 7 };
    await loadRound(12);
    calls.length = 0;

    await refresh();

    expect(read(/\/round$/), 'the refresh re-read a seat this phone is not playing').toEqual([
      '/api/tonight/seats/12/round'
    ]);
    expect(tonight.round.participant_id).toBe(12);
    expect(tonight.activeSeat).toBe(12);
  });

  it("falls back to this device's own seat when nothing is on screen", async () => {
    // The other half of the rule, and the one that makes the restore work: a reload, a `leave`
    // and a device that has never loaded a round all arrive here with no active seat, and the
    // seat they should read is their own. Passes against the old code by construction — it read
    // `me` unconditionally — and is here because the repair must not lose it.
    tonight.lobby = { session_id: 7 };

    await refresh();

    expect(read(/\/round$/)).toEqual(['/api/tonight/seats/11/round']);
  });

  it('hands the phone back to its owner once the seat it was playing has ended', async () => {
    // The hand-off is over when the turn is: the next guest is passed the phone from a screen
    // only the owner's seat draws, so a device that stayed on an ended guest seat for ever would
    // trade one stuck surface for another.
    tonight.lobby = { session_id: 7 };
    await loadRound(12);
    world.room = roomOf({ seats: [seatOf(11), seatOf(12, { ended_by: 'cap' })] });
    calls.length = 0;

    await refresh();

    expect(read(/\/round$/)).toEqual(['/api/tonight/seats/11/round']);
    expect(tonight.activeSeat).toBe(11);
  });

  it('lands a reload into an open room in its lobby and not at the door', async () => {
    // `bootstrap` finds the room, sets the lobby and calls this; with no branch for `open` the
    // step stayed 'door', so the device held a lobby while drawing the two doors — the exact
    // state the restore exists to prevent. [finding 15]
    tonight.lobby = { session_id: 7 };
    world.room = roomOf({ state: 'open' });

    await refresh();

    expect(tonight.step).toBe('lobby');
  });

  it("leaves the escaped seat on screen rather than the phone owner's round", async () => {
    // 54c's "just pick for us" ends the round of whoever tapped it. The write ends the seat, and
    // an ended seat is exactly the case whose fallback hands the phone back — right from the next
    // frame, wrong for this one. It was deterministic, not a race. [finding 14]
    tonight.lobby = { session_id: 7 };
    await loadRound(12);
    tonight.round = { ...tonight.round, escape_available: true };
    world.room = roomOf({ seats: [seatOf(11), seatOf(12, { ended_by: 'escape' })] });
    calls.length = 0;

    await escape();

    expect(tonight.round.participant_id).toBe(12);
    expect(tonight.activeSeat).toBe(12);
    expect(read(/\/round$/)).toEqual(['/api/tonight/seats/12/round']);
  });

  it('forgets the seat and the ballots it cast when the device steps out', async () => {
    // Both are about ONE room. Carrying either into the next evening hands it a participant id
    // from the last one, and `submittedSeats` would silently hide a real seat's hand-off.
    tonight.lobby = { session_id: 7 };
    await loadRound(12);
    tonight.submittedSeats = [11];

    leave();

    expect(tonight.activeSeat).toBeNull();
    expect(tonight.submittedSeats).toEqual([]);
  });
});

describe("54e's ballot, on a phone that seats a guest (finding 13)", () => {
  const inTheBallot = async () => {
    tonight.lobby = { session_id: 7 };
    world.room = roomOf({ state: 'ballot' });
    await refresh();
  };

  it('does not finish the ballot on a phone that still holds a guest vote', async () => {
    // `ballot.submitted_count` counts every seated participant, guests included, and the reveal
    // waits for all of them — so a room opened with any guest could never reach it while Submit
    // was bound to the viewer's own seat. The phone is not done when its owner has voted.
    await inTheBallot();
    expect(tonight.step).toBe('ballot');
    expect(tonight.activeSeat).toBe(11);
    toggleApproval(1);

    await submitBallot(tonight.activeSeat);

    expect(posted('/api/tonight/seats/11/ballot')).toEqual({ approved: [1] });
    expect(tonight.submittedSeats).toEqual([11]);
    expect(tonight.step, 'the guest still owes a vote and the screen that casts it closed').toBe(
      'ballot'
    );
    expect(tonight.approved, "the next person opens on the last person's ticks").toEqual([]);
  });

  it("clears the previous person's ticks when the phone changes hands", async () => {
    // 54e's blindness broken by the control that exists to carry it: the incoming guest would
    // open on ticks somebody else made, and one tap on Submit would cast them as theirs.
    await inTheBallot();
    toggleApproval(1);

    handBallot(12);

    expect(tonight.activeSeat).toBe(12);
    expect(tonight.approved).toEqual([]);
    expect(tonight.step).toBe('ballot');
  });

  it('stops offering a seat once this phone has voted for it', async () => {
    await inTheBallot();
    expect(ballotTurns().map((s) => s.participant_id)).toEqual([12]);

    await submitBallot(11);
    handBallot(12);
    await submitBallot(12);

    expect(ballotTurns()).toEqual([]);
    expect(tonight.step, 'both votes are in, so this phone waits like any other').toBe('waiting');
  });

  it("keeps the ballot open when the guest voted first and the phone's owner has not", async () => {
    // 54c's escape can leave a guest as the active seat at the moment the room settles, so the
    // guest's vote is not always the second one. A rule that asked only "does a guest still owe
    // a vote" would send this phone to the waiting screen with its OWNER's vote outstanding, and
    // the reveal would wait on a ballot no screen was offering.
    await inTheBallot();
    handBallot(12);

    await submitBallot(12);

    expect(tonight.step).toBe('ballot');
    expect(tonight.activeSeat, "the phone's owner is who it comes back to").toBe(11);
  });

  it("does not offer another member the guest's ballot", async () => {
    // The guest seats belong to whoever opened the room. A second member's device offering
    // "pass to Guest 1" would offer a vote that phone is not holding, and the guest would be
    // voted for twice by two people who each thought it was theirs to cast.
    tonight.lobby = { session_id: 7 };
    world.room = roomOf({
      state: 'ballot',
      seats: [seatOf(11), seatOf(12), seatOf(13, { role: 'member', user_id: 2, name: 'Jenny' })],
      me: seatOf(13, { role: 'member', user_id: 2, name: 'Jenny' })
    });
    await refresh();

    expect(ballotTurns()).toEqual([]);
  });
});

describe("the answer's latency is a measurement (§4.2; §14 risk 6; finding 41)", () => {
  it('records how long the pair was on screen rather than zero', async () => {
    // The elapsed time was read inside the object literal one statement after the clock was
    // started, so every Tonight row ever written holds 0. The rows are append-only, so the
    // evenings already played cannot be re-measured, and §14 risk 6 wants the instrument in
    // place before anyone re-tunes the round.
    vi.useFakeTimers();
    tonight.lobby = { session_id: 7 };
    await loadRound(11);

    vi.advanceTimersByTime(250);
    await answer('A');
    expect(posted('/api/tonight/seats/11/answer').latency_ms).toBeGreaterThanOrEqual(250);

    // Re-armed by the answer itself: most pairs of a twenty-pair round arrive on that path and
    // never pass through `loadRound` at all. Bounded from ABOVE as well, because a lower bound
    // alone is satisfied by a clock armed once at the door and never re-armed after -- the very
    // shape this describe block's next test is about. Measured: with the re-arm in `answer`
    // deleted, this pair reports 650 ms rather than 400 and every assertion in this test passed
    // anyway, so the row's "measured from when the pair reached the screen" rested on the
    // Playwright case alone. [M4.12 review cycle 2]
    calls.length = 0;
    vi.advanceTimersByTime(400);
    await answer('B');
    const second = posted('/api/tonight/seats/11/answer').latency_ms;
    expect(second).toBeGreaterThanOrEqual(400);
    expect(second, 'the answer did not re-arm the clock, so the wait is both pairs').toBeLessThan(
      500
    );

    // And by an undo, which re-opens the seq and re-issues the same card: the time spent on it
    // after the undo is the latency of the answer about to be given. The wait BEFORE the undo is
    // what makes that claim falsifiable -- it is time spent on a pair the person then took back,
    // so it must not be charged to the answer that replaces it. Without it the undo landed in the
    // same instant as the answer before it, and deleting `undo`'s re-arm changed no number here.
    vi.advanceTimersByTime(900);
    await undo();
    calls.length = 0;
    vi.advanceTimersByTime(120);
    await answer('EITHER');
    const third = posted('/api/tonight/seats/11/answer').latency_ms;
    expect(third).toBeGreaterThanOrEqual(120);
    expect(third, 'the undo did not re-arm the clock, so the wait is the whole round').toBeLessThan(
      500
    );
  });

  it('is not restarted by a household frame that re-reads the pair already on screen', async () => {
    // Every channel frame ends in `refresh`, and `refresh` ends in `loadRound` — so a clock
    // armed on each re-read measured the time since the last thing ANYBODY did, which on a
    // two-device evening is the other person's tap. `start` alone sends the host's own device
    // two frames, so it was the first pair of every round: on the M4.12 gate a card held for
    // 400 ms was written as 176. [finding 41]
    vi.useFakeTimers();
    tonight.lobby = { session_id: 7 };
    await loadRound(11);

    vi.advanceTimersByTime(300);
    await refresh();
    vi.advanceTimersByTime(100);
    calls.length = 0;
    await answer('A');

    expect(posted('/api/tonight/seats/11/answer').latency_ms).toBeGreaterThanOrEqual(400);
  });

  it('is restarted by a card that is new, which the hand-off puts on the phone', async () => {
    // The other direction, so the rule above cannot be satisfied by a clock that never re-arms:
    // §6.2 step 2's guest turn draws a different seat's pair on the same device, and the time
    // the previous person spent is not this one's. The token is what tells them apart.
    vi.useFakeTimers();
    tonight.lobby = { session_id: 7 };
    await loadRound(11);

    vi.advanceTimersByTime(5_000);
    await loadRound(12);
    vi.advanceTimersByTime(120);
    calls.length = 0;
    await answer('A');

    const handed = posted('/api/tonight/seats/12/answer').latency_ms;
    expect(handed).toBeGreaterThanOrEqual(120);
    expect(handed, "the guest was charged the host's whole turn").toBeLessThan(5_000);
  });

  it('sends nothing at all rather than zero before a pair has been shown', async () => {
    // §4.2's column is nullable, and "not measured" is not "answered instantly". A constant 0 is
    // worse than a null, because it reads as a measurement. Reachable because `leave` — which
    // `beforeEach` runs — stops the clock: a device at the door has no pair on screen.
    tonight.round = roundOf(11);

    await answer('A');

    expect(posted('/api/tonight/seats/11/answer').latency_ms).toBeNull();
  });
});

describe('the reconnect (§6 preamble; finding 18)', () => {
  it('grows the wait between attempts and caps it near thirty seconds', () => {
    // A fixed 1.5 s retry with no cap opened 40 sockets and fired 117 HTTP reads in 60 simulated
    // seconds — and §10's swap sequence ends in exactly the backend restart that hammers.
    const mid = () => 0.5;
    expect([0, 1, 2, 3, 4, 5, 6, 20].map((n) => reconnectDelay(n, mid))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000
    ]);
    // The jitter is narrower than the doubling on purpose: the waits still grow strictly, so
    // "backoff" stays a claim something can check.
    expect(reconnectDelay(1, () => 0)).toBeGreaterThan(reconnectDelay(0, () => 1));
    expect(reconnectDelay(0, () => 0)).toBeLessThan(reconnectDelay(0, () => 1));
    expect(reconnectDelay(9, () => 1)).toBeLessThanOrEqual(RECONNECT_MAX_MS * 1.2);
  });

  it('re-reads once per connection that opens, and starts over after one does', async () => {
    // Two halves, and the old four lines got both wrong: the wait was fixed, and the re-read
    // fired from the TIMER rather than from a socket that opened — so every failed attempt
    // charged the server two reads that could not have been told anything new.
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    sockets.length = 0;
    // `stubGlobal` rather than a bare assignment: node has a real `WebSocket` and no `location`
    // at all, and both are restored by `unstubAllGlobals` in `afterEach` above.
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.stubGlobal('location', { protocol: 'http:', host: 'host' });

    const stop = connect(7);
    try {
      expect(sockets).toHaveLength(1);
      expect(sockets[0].url).toBe('ws://host/api/tonight/channel?session_id=7');
      expect(calls, 'the socket had not opened and the device read the world anyway').toEqual([]);

      sockets[0].onclose();
      vi.advanceTimersByTime(1400);
      expect(sockets, 'the first wait is a second, not the old fixed 1.5 s').toHaveLength(2);
      expect(calls, 'a scheduled retry is not a connection, and it re-read anyway').toEqual([]);

      // A second failure with nothing open in between: the wait doubles.
      sockets[1].onclose();
      vi.advanceTimersByTime(1400);
      expect(sockets, 'the second wait did not grow').toHaveLength(2);
      vi.advanceTimersByTime(700);
      expect(sockets).toHaveLength(3);

      // A connection that OPENS is what pays for the re-read, and what resets the wait.
      await sockets[2].onopen();
      expect(read(/rooms$/)).toHaveLength(1);
      sockets[2].onclose();
      vi.advanceTimersByTime(1400);
      expect(sockets, 'the next outage started at the ceiling instead of at a second').toHaveLength(
        4
      );
    } finally {
      stop();
    }
  });

  it("takes back its own complaint and leaves everybody else's standing", async () => {
    // The re-read above is a BACKGROUND read — the socket fires it the moment it opens, and every
    // `rooms.changed` frame fires it again — so it lands while the person is reading a sentence
    // about something else entirely. Clearing the error slot on success wiped those: a refused
    // guests box said "guests: Input should be a valid integer" and lost it forty milliseconds
    // later to a handshake that had just finished. Only the phone met it, because WebKit opens
    // the channel after the tap where Chromium opens it before — but nothing about the defect is
    // WebKit's, and on a settled device it is any other member opening a room. [§6.8; finding 18]
    tonight.error = 'guests: Input should be a valid integer';
    await loadRooms();
    expect(
      tonight.error,
      'a read that worked took away a refusal it had nothing to do with'
    ).toBe('guests: Input should be a valid integer');

    // And the half the clear is actually for: an open-rooms read that FAILED leaves a sentence,
    // and the read that recovers has to take that one back or the door complains for ever.
    tonight.error = '';
    fetchMock.mockImplementationOnce(async () => {
      throw new TypeError('Load failed');
    });
    await loadRooms();
    expect(tonight.error, 'a rooms read that failed said nothing at all').not.toBe('');
    await loadRooms();
    expect(tonight.error, 'the read that recovered left its own complaint standing').toBe('');
  });
});

describe('the copy and the controls this milestone moved', () => {
  it('names only the join channels that still exist', () => {
    // Decision 165 retires the TV client. A caption advertising a route that no longer answers
    // sends the household to a screen that will 404. [finding 43]
    expect(JOIN_CAPTION).toContain('Push is best effort');
    expect(JOIN_CAPTION).not.toMatch(/TV/i);
    expect(JOIN_CAPTION).toContain('room code');
    expect(JOIN_CAPTION).toContain('banner');
  });

  it('says which minutes a series room is counting', () => {
    // 54h / decision 219: on a series night the budget bounds minutes PER EPISODE, and this row
    // is the one place §6.2 step 2 prints it. Unqualified it read as the length of the thing
    // being chosen.
    const room = {
      room_code: 'MX-2210',
      host: 'Mia',
      started_at: null,
      kind: 'series',
      runtime_budget_min: 130,
      skips_seen: true
    };
    expect(roomLine(room)).toContain('130 min per episode');
    expect(roomLine({ ...room, kind: 'movie' })).toContain('130 min');
    expect(roomLine({ ...room, kind: 'movie' })).not.toContain('per episode');
  });

  it('asks the solo round for a pair only when the person asks to sharpen', async () => {
    // 54f: the door lands "directly on three picks and a wildcard", and the pair search is what
    // "sharpen this" asks for and what nothing else does. The field has been on the request model
    // since the round was made optional and this module never sent it, so the door and every
    // Reshuffle paid for a round nobody had asked for.
    await loadSolo();
    expect(posted('/api/tonight/solo').sharpen).toBe(false);

    calls.length = 0;
    await loadSolo({ reshuffle: true });
    expect(posted('/api/tonight/solo').sharpen).toBe(false);

    calls.length = 0;
    await loadSolo({ sharpen: true });
    expect(posted('/api/tonight/solo').sharpen).toBe(true);
  });

  it('does not advance the walk when the reshuffle it asked for was refused', async () => {
    // The counter is raised before the POST and nothing ever put it back, so one failed press --
    // a dropped connection, a 500, the route's own bound -- moved the walk by a step the person
    // never saw, and every later press asked for the one after that. 54f's Reshuffle "walks
    // further down the ranking": what it has walked is what came back.
    // [M4.12 review cycle 1: M412-SOLO-06]
    const offsets = [];
    let refuse = true;
    fetchMock.mockImplementation(async (path, opts = {}) => {
      const body = JSON.parse(opts.body ?? '{}');
      offsets.push(body.offset);
      if (body.offset === 2 && refuse) {
        refuse = false;
        return reply({ detail: { message: 'nope' } }, 500);
      }
      return reply({ picks: [], provenance: 'x', pair: null });
    });

    await loadSolo();
    await loadSolo({ reshuffle: true });
    await loadSolo({ reshuffle: true });
    await loadSolo({ reshuffle: true });

    expect(offsets, 'a refused walk moved it anyway, so the next press skipped a step').toEqual([
      0, 1, 2, 2
    ]);
    expect(tonight.soloOffset).toBe(2);
  });

  it('never asks for a walk further down the ranking than the route will serve', async () => {
    // `SoloBody.offset` is bounded `le=64` (`api/tonight.py`), and the client had no idea: press
    // 65 was a 422 error banner, and because the number that caused it had already been stored so
    // was every press after it -- the control was dead until Back, which drops the evening's
    // sharpen answers with it. A request that can only be refused is not a gesture.
    // [M4.12 review cycle 1: M412-SOLO-06]
    const offsets = [];
    fetchMock.mockImplementation(async (path, opts = {}) => {
      const body = JSON.parse(opts.body ?? '{}');
      offsets.push(body.offset);
      if (body.offset > 64) return reply({ detail: { message: 'unprocessable' } }, 422);
      return reply({ picks: [], provenance: 'x', pair: null });
    });

    await loadSolo();
    for (let press = 0; press < 70; press++) await loadSolo({ reshuffle: true });

    expect(
      Math.max(...offsets),
      'the walk asked the route for an offset it refuses, and kept asking'
    ).toBe(64);
    expect(tonight.error, 'the walk reported a refusal the person cannot act on').toBe('');
  });

  it('leaves a room the host has ended rather than sitting in it', async () => {
    // Decision 169's other half. `abandoned` is terminal: the room leaves §6.2 step 2's list and
    // releases its code, so a device still holding its lobby is holding a screen that answers
    // nothing and a Start that 404s.
    tonight.lobby = { session_id: 7 };
    tonight.step = 'lobby';
    world.room = roomOf({ state: 'abandoned' });

    await refresh();

    expect(tonight.step).toBe('door');
    expect(tonight.lobby).toBeNull();
    expect(tonight.error).toMatch(/ended/);
  });

  it('ends the room through the host-only route and lands at the door', async () => {
    // The one control a room that has stopped progressing needs: `STATE_ABANDONED` has a single
    // writer in the backend and no worker job touches `session`, so a `voting` room that loses a
    // seat is otherwise live for ever. The rows stay — §14 risk 6 reads exactly the evenings a
    // household cut short.
    tonight.lobby = { session_id: 7 };
    tonight.step = 'waiting';

    await endRoom();

    expect(posted('/api/tonight/sessions/7/end')).toEqual({});
    expect(tonight.step).toBe('door');
    expect(tonight.lobby).toBeNull();
  });
});

/**
 * Finding 21. `refresh` is the busiest read in the app — §6.2 step 2 makes every channel frame a
 * nudge to re-read, and `answer`, `escape` and `start` end in one too — so several are in flight
 * at once on any evening with more than one device in it. Until this milestone none of them
 * carried the sequence number `rank.svelte.js` and Home have had since M3.
 */
describe('overlapping reads land in order (finding 21)', () => {
  /** A fetch whose first session read is held open, so the test decides which answer lands last. */
  function heldSessionRead(stale, fresh) {
    let release = () => {};
    let seen = 0;
    fetchMock.mockImplementation((path, opts = {}) => {
      calls.push({ method: opts.method ?? 'GET', path, body: null });
      if (/^\/api\/tonight\/sessions\/\d+$/.test(path)) {
        seen += 1;
        if (seen === 1) return new Promise((r) => (release = () => r(stale())));
        return Promise.resolve(fresh());
      }
      if (/^\/api\/tonight\/sessions\/\d+\/ballot$/.test(path)) {
        return Promise.resolve(
          reply({
            session_id: 7,
            slate: [{ title_id: 1, slot: 'finalist', name: 'Heat' }],
            submitted: 0,
            seated: 2,
            revealed: false
          })
        );
      }
      if (/^\/api\/tonight\/seats\/(\d+)\/round$/.test(path)) {
        return Promise.resolve(reply(roundOf(Number(path.match(/seats\/(\d+)/)[1]))));
      }
      if (path === '/api/tonight/rooms') return Promise.resolve(reply({ rooms: [] }));
      throw new Error(`no fixture for ${path}`);
    });
    return () => release();
  }

  it('does not put a device back into the round once the ballot has opened', async () => {
    // The failure this exists for: two frames arrive close together, the `voting` read answers
    // after the `ballot` one, and the device that has been handed 54e's ballot is dragged back
    // to a pair. It then never submits, and `ballot.submitted_count` never reaches `seated` —
    // so 54e's reveal is blocked for EVERY seat in the room by one device's stale read.
    tonight.lobby = roomOf({ state: 'voting' });
    const release = heldSessionRead(
      () => reply(roomOf({ state: 'voting' })),
      () => reply(roomOf({ state: 'ballot' }))
    );

    const overtaken = refresh();
    await refresh();
    expect(tonight.step).toBe('ballot');

    release();
    await overtaken;

    expect(tonight.step, 'a superseded read put the device back into the round').toBe('ballot');
    expect(tonight.lobby.state).toBe('ballot');
  });

  it('still leaves a room the host ended when the read that overtook it failed', async () => {
    // Why the sequence check sits AFTER the abandoned branch and not before it. Decision 169's
    // door is terminal — the server does not un-abandon a session — and the newer read is not
    // guaranteed to arrive at all: here it 500s. A guard placed above the branch would discard
    // the only answer that saw the end of the evening, and the device would sit in a lobby whose
    // Start 404s with no way out but a reload.
    tonight.lobby = roomOf();
    tonight.step = 'lobby';
    const release = heldSessionRead(
      () => reply(roomOf({ state: 'abandoned' })),
      () => reply({ detail: { message: 'gone' } }, 500)
    );

    const overtaken = refresh();
    await refresh();

    release();
    await overtaken;

    expect(tonight.step, 'the end of the evening was discarded as stale').toBe('door');
    expect(tonight.lobby).toBeNull();
    expect(tonight.error).toMatch(/ended/);
  });

  it('keeps the newer pair on screen when an earlier round read answers last', async () => {
    // `loadRound` is the tail of `refresh` AND the retry `answer` makes on a 409, so two are in
    // flight whenever a frame lands mid-answer. The older one landing last puts a spent
    // `card_token` on screen; §4.2's seal is single-use, so the next tap posts a card the route
    // refuses and the person reads a 409 for a pair they are looking at.
    let release = () => {};
    let seen = 0;
    fetchMock.mockImplementation((path) => {
      seen += 1;
      if (seen === 1) {
        return new Promise(
          (r) => (release = () => r(reply(roundOf(11, { card_token: 'card-overtaken' }))))
        );
      }
      return Promise.resolve(reply(roundOf(12, { card_token: 'card-newest' })));
    });

    const overtaken = loadRound(11);
    await loadRound(12);
    expect(tonight.round.card_token).toBe('card-newest');

    release();
    await overtaken;

    expect(tonight.round.card_token, 'a spent card landed over the live one').toBe('card-newest');
    expect(tonight.activeSeat, 'the seat followed the stale card').toBe(12);
  });
});
