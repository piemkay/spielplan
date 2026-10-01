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
  MAX_VETOES,
  MOOD_CAPTION,
  RECONNECT_MAX_MS,
  REVEAL_BEAT,
  SOLO_ESCAPE_LABEL,
  answer,
  answerSolo,
  approvalShare,
  ballotTurns,
  ballotWaitingLine,
  breadthLine,
  budgetLabel,
  budgetSoftLine,
  chooseKind,
  connect,
  endRoom,
  escape,
  escapeSolo,
  followLink,
  handBallot,
  leave,
  linkedRoom,
  loadRooms,
  loadRound,
  loadSolo,
  minutesAgo,
  myVetoKeys,
  onlyYesLines,
  othersVetoLines,
  pairFacts,
  pickLabel,
  progressLines,
  reconnectDelay,
  refresh,
  rememberBudget,
  rememberedBudget,
  restoreBudget,
  roomEvening,
  roomLine,
  roomVetoLine,
  roundHeader,
  settingsDetail,
  settingsTitle,
  shareLink,
  shareRoom,
  submitBallot,
  submitLabel,
  toggleApproval,
  toggleVeto,
  tonight,
  undo,
  undoSolo,
  vetoCaption,
  waitingLine
} from './tonight.svelte.js';
import { hideToast, toast } from './toast.svelte.js';
import { runtimeLabel } from './rate.svelte.js';

describe('the open-rooms row (§6.2 step 2)', () => {
  const room = {
    room_code: 'MX-2210',
    host: 'Mia',
    started_at: new Date(Date.now() - 3 * 60000).toISOString(),
    kind: 'movie',
    runtime_budget_min: 60,
    skips_seen: true
  };

  it("names the code, how long ago, and what evening it is, under the host's name", () => {
    // Two rooms from one host differ by their code, which is also what is read out across a room.
    expect(roomLine(room)).toBe('MX-2210 · Started 3 min ago');
    expect(roomEvening(room)).toBe('Film up to 1h, no rewatches');
  });

  it('says the opposite when rewatches are in', () => {
    expect(roomEvening({ ...room, skips_seen: false })).toContain('rewatches included');
  });

  it('names the kind the way the controls do', () => {
    expect(roomEvening({ ...room, kind: 'series' })).toContain('Series');
  });

  it('drops the age rather than printing a lie when the timestamp is missing', () => {
    // A row is still a row without an age; "NaN min ago" is worse than one fewer facet.
    expect(roomLine({ ...room, started_at: null })).not.toContain('min ago');
    expect(roomLine({ ...room, started_at: null })).toBe('MX-2210');
    expect(minutesAgo('not-a-date')).toBeNull();
    expect(minutesAgo(null)).toBeNull();
  });

  it('never reports a negative age from a clock that is slightly ahead', () => {
    const ahead = new Date(Date.now() + 30000).toISOString();
    expect(minutesAgo(ahead)).toBe(0);
  });
});

describe('the waiting lines (54c)', () => {
  // The rows carry an answer and a pair on purpose, so the claim below can fail.
  const progress = [
    { participant_id: 1, name: 'Patrick', answered: 6, expected: 10, finished: true, answer: 'NEITHER', pair: 'Heat' },
    { participant_id: 2, name: 'Jenny', answered: 11, expected: null, finished: false, answer: 'EITHER', pair: 'Drive' },
    { participant_id: 3, name: 'Mia', answered: 3, expected: 10, finished: false, answer: 'A', pair: 'Sicario' }
  ];

  it('says where each of the others is in words, and nothing that could be an answer', () => {
    // The renderer must not draw answers even when they are handed to it (54c).
    const lines = progressLines(progress, 3).map((p) => p.line);
    expect(lines).toEqual(['Patrick 6/6 done', 'Jenny 11 so far']);
    const all = lines.join(' ');
    expect(all, "a seat's answer reached the waiting lines").not.toMatch(/EITHER|NEITHER/i);
    expect(all, 'the pair a seat answered about reached the waiting lines').not.toMatch(
      /Heat|Drive|Sicario/
    );
  });

  it('keys each line on its seat, so the avatar beside it is that person', () => {
    expect(progressLines(progress, 1).map((p) => p.participant_id)).toEqual([2, 3]);
  });

  it('is empty rather than wrong with nobody else seated', () => {
    expect(progressLines([])).toEqual([]);
    expect(progressLines(progress.slice(0, 1), 1)).toEqual([]);
  });

  it('estimates with the typical round until a seat reaches it, and never with the cap', () => {
    // §6.2 step 4: "Patrick 6/6 done · Jenny 12 so far · Mia 4/~10 · waiting for 2" (decision 507).
    expect(progressLines(progress).map((p) => p.line)).toEqual([
      'Patrick 6/6 done',
      'Jenny 11 so far',
      'Mia 3/~10'
    ]);
    expect(waitingLine(progress)).toBe('Waiting for 2');
    expect(waitingLine(progress.slice(0, 1)), 'nobody left to wait for').toBe('');
  });
});

describe('the approval share (§13)', () => {
  it('is said as the people it counts, never a bare number', () => {
    expect(approvalShare({ approval_share: 0.75, participants: 4 })).toBe('3 of 4 said yes');
    expect(approvalShare({ approval_share: 1, participants: 2 })).toBe('Both of you said yes');
    expect(approvalShare({ approval_share: 1, participants: 3 })).toBe('All 3 of you said yes');
    expect(approvalShare({ approval_share: 0, participants: 3 })).toBe('0 of 3 said yes');
  });

  it('says nothing at all before there is a result', () => {
    expect(approvalShare(null)).toBe('');
  });
});

describe('the constants the spec fixes', () => {
  it('offers exactly decision 154\'s four answers', () => {
    expect(ANSWERS.map((a) => a.value)).toEqual(['A', 'B', 'EITHER', 'NEITHER']);
    // Opposite signals, so opposite copy.
    expect(ANSWERS.find((a) => a.value === 'EITHER').label).toBe('Either is fine');
    expect(ANSWERS.find((a) => a.value === 'NEITHER').label).toBe('Neither tonight');
  });

  it('bounds the runtime slider', () => {
    expect([BUDGET_MIN, BUDGET_MAX, BUDGET_STEP, BUDGET_DEFAULT]).toEqual([60, 200, 5, 130]);
    expect(BUDGET_DEFAULT % BUDGET_STEP).toBe(0);
  });

  it('caps the guests who share one phone', () => {
    expect(MAX_GUESTS).toBe(6);
  });

  it('keeps the strings the spec fixes verbatim', () => {
    expect(REVEAL_BEAT).toBe("Tonight's pick");
    // §6.2 step 4's "just pick for us", sentence-cased as a control.
    expect(ESCAPE_LABEL).toBe('Just pick for us');
    // The join caption is household copy, held to the member register rather than verbatim.
    expect(JOIN_CAPTION).not.toMatch(/push|best effort/i);
  });
});

describe('stepping back out to the door (§6.2 step 2)', () => {
  it('drops the room, not the seat', () => {
    // A client-side step: the seat, the answers and the ballot stay on the server for `resume`.
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
    // Every frame ends in `refresh`, which would pull back any device still holding a lobby.
    Object.assign(tonight, { step: 'ballot', lobby: { session_id: 7 } });
    leave();
    expect(tonight.lobby).toBeNull();
  });

  it("keeps the controls, because they are the door's and not the room's", () => {
    // The controls sit before the fork (§6.2 step 1), so stepping out keeps them.
    tonight.controls.runtime_budget_min = 95;
    tonight.controls.include_rewatches = true;
    leave();
    expect(tonight.controls.runtime_budget_min).toBe(95);
    expect(tonight.controls.include_rewatches).toBe(true);
  });
});

// A router, not a queue: every claim below is about which seat was read.
let world;
let calls;
// Held loosely so the mock keeps its vitest type for the checker.
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

// At module scope: svelte's `perf_avoid_nested_class` check reads test files too.
const sockets = [];
class FakeSocket {
  constructor(url) {
    this.url = url;
    sockets.push(this);
  }
  close() {}
}

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
    // A re-read used to name the phone's owner, replacing a guest's pair mid-turn.
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
    // With no active seat, a device reads its own.
    tonight.lobby = { session_id: 7 };

    await refresh();

    expect(read(/\/round$/)).toEqual(['/api/tonight/seats/11/round']);
  });

  it('hands the phone back to its owner once the seat it was playing has ended', async () => {
    // Once the guest's seat has ended, the phone goes back to its owner.
    tonight.lobby = { session_id: 7 };
    await loadRound(12);
    world.room = roomOf({ seats: [seatOf(11), seatOf(12, { ended_by: 'cap' })] });
    calls.length = 0;

    await refresh();

    expect(read(/\/round$/)).toEqual(['/api/tonight/seats/11/round']);
    expect(tonight.activeSeat).toBe(11);
  });

  it('lands a reload into an open room in its lobby and not at the door', async () => {
    tonight.lobby = { session_id: 7 };
    world.room = roomOf({ state: 'open' });

    await refresh();

    expect(tonight.step).toBe('lobby');
  });

  it("leaves the escaped seat on screen rather than the phone owner's round", async () => {
    // The escape ends the tapping seat, but that seat stays on screen for this frame.
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
    // Both belong to one room; carried into the next they would hide a real seat.
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
    // The reveal waits for guests too, so the phone is not done when its owner has voted.
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
    // The incoming guest must not open on someone else's ticks (54e).
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
    // A guest may vote first; the phone's owner still owes one.
    await inTheBallot();
    handBallot(12);

    await submitBallot(12);

    expect(tonight.step).toBe('ballot');
    expect(tonight.activeSeat, "the phone's owner is who it comes back to").toBe(11);
  });

  it("does not offer another member the guest's ballot", async () => {
    // Guest seats belong to the host's phone.
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
    // The rows are append-only, so the latency must be measured, not 0.
    vi.useFakeTimers();
    tonight.lobby = { session_id: 7 };
    await loadRound(11);

    vi.advanceTimersByTime(250);
    await answer('A');
    expect(posted('/api/tonight/seats/11/answer').latency_ms).toBeGreaterThanOrEqual(250);

    // Re-armed by the answer itself, and bounded above so a never-re-armed clock fails.
    calls.length = 0;
    vi.advanceTimersByTime(400);
    await answer('B');
    const second = posted('/api/tonight/seats/11/answer').latency_ms;
    expect(second).toBeGreaterThanOrEqual(400);
    expect(second, 'the answer did not re-arm the clock, so the wait is both pairs').toBeLessThan(
      500
    );

    // And by an undo: time spent on the pair taken back is not charged to its replacement.
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
    // A frame re-reading the same card must not restart the clock.
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
    // A new card (a guest's) restarts it; the token tells them apart.
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
    // Nullable, and "not measured" is not "answered instantly".
    tonight.round = roundOf(11);

    await answer('A');

    expect(posted('/api/tonight/seats/11/answer').latency_ms).toBeNull();
  });
});

describe('the reconnect (§6 preamble; finding 18)', () => {
  it('grows the wait between attempts and caps it near thirty seconds', () => {
    // Backoff capped near 30s: a fixed short retry hammers a restarting backend.
    const mid = () => 0.5;
    expect([0, 1, 2, 3, 4, 5, 6, 20].map((n) => reconnectDelay(n, mid))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000
    ]);
    // Jitter narrower than the doubling, so the waits still grow strictly.
    expect(reconnectDelay(1, () => 0)).toBeGreaterThan(reconnectDelay(0, () => 1));
    expect(reconnectDelay(0, () => 0)).toBeLessThan(reconnectDelay(0, () => 1));
    expect(reconnectDelay(9, () => 1)).toBeLessThanOrEqual(RECONNECT_MAX_MS * 1.2);
  });

  it('re-reads once per connection that opens, and starts over after one does', async () => {
    // The re-read fires from a socket that opened, not from the timer.
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    sockets.length = 0;
    // Node has a real `WebSocket` and no `location`; both are restored in `afterEach`.
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
    // A background read must not clear a refusal it had nothing to do with.
    tonight.error = 'guests: Input should be a valid integer';
    await loadRooms();
    expect(
      tonight.error,
      'a read that worked took away a refusal it had nothing to do with'
    ).toBe('guests: Input should be a valid integer');

    // But the read that recovers must take back its own complaint.
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
    // The TV client is retired (decision 165); the link is a channel since decision 481.
    expect(JOIN_CAPTION).toContain('Missed a notification');
    expect(JOIN_CAPTION).not.toMatch(/TV/i);
    expect(JOIN_CAPTION).toContain('code');
    expect(JOIN_CAPTION).toContain('link');
  });

  it('says which minutes a series room is counting', () => {
    // On a series night the budget bounds minutes per episode.
    const room = {
      room_code: 'MX-2210',
      host: 'Mia',
      started_at: null,
      kind: 'series',
      runtime_budget_min: 130,
      skips_seen: true
    };
    expect(roomEvening(room)).toContain('Series up to 2h 10m per episode');
    expect(roomEvening({ ...room, kind: 'movie' })).toContain('Film up to 2h 10m');
    expect(roomEvening({ ...room, kind: 'movie' })).not.toContain('per episode');
  });

  it('asks for a pair at the door, and for none when Reshuffle walks the picks', async () => {
    // Solo asks first (decision 532); a walk only re-reads the ranking the answers tilted.
    await loadSolo();
    expect(posted('/api/tonight/solo').sharpen, 'the door went straight to the picks').toBe(true);

    calls.length = 0;
    await loadSolo({ reshuffle: true });
    expect(posted('/api/tonight/solo').sharpen, 'a reshuffle reopened the round').toBe(false);
  });

  it('does not advance the walk when the reshuffle it asked for was refused', async () => {
    // What the walk has walked is what came back, so a refused press does not advance it.
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
    // `SoloBody.offset` is bounded `le=64`; a request that can only be refused is not a gesture.
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
    // `abandoned` is terminal: the code is released and Start would 404.
    tonight.lobby = { session_id: 7 };
    tonight.step = 'lobby';
    world.room = roomOf({ state: 'abandoned' });

    await refresh();

    expect(tonight.step).toBe('door');
    expect(tonight.lobby).toBeNull();
    expect(tonight.notice, 'news, not a failure').toMatch(/ended/);
    expect(tonight.error).toBe('');
  });

  it('ends the room through the host-only route and lands at the door', async () => {
    // A stalled `voting` room is otherwise live for ever: nothing else writes `abandoned`.
    tonight.lobby = { session_id: 7 };
    tonight.step = 'waiting';

    await endRoom();

    expect(posted('/api/tonight/sessions/7/end')).toEqual({});
    expect(tonight.step).toBe('door');
    expect(tonight.lobby).toBeNull();
  });
});

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
    // A stale `voting` read landing last would drag a device back from the ballot and block the reveal.
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

  it('keeps the ballot on screen while a re-read of the room lands', async () => {
    // The room's read carries the counts, not the slate; dropping the slate blanked the ballot.
    tonight.lobby = roomOf({ state: 'ballot' });
    tonight.step = 'ballot';
    tonight.ballot = {
      session_id: 7,
      slate: [{ title_id: 1, slot: 'finalist', name: 'Heat' }],
      submitted: 0,
      seated: 2,
      revealed: false
    };
    world.room = roomOf({ state: 'ballot', ballot: { submitted: 1, seated: 2, revealed: false } });
    const base = router();
    let held = false;
    let release = () => {};
    fetchMock.mockImplementation(async (path, opts) => {
      if (/\/ballot$/.test(path)) {
        held = true;
        await new Promise((r) => (release = () => r(undefined)));
      }
      return base(path, opts);
    });

    const pending = refresh();
    await vi.waitFor(() => expect(held).toBe(true));

    expect(tonight.ballot.slate, 'the re-read blanked the ballot').toHaveLength(1);
    expect(tonight.ballot.submitted).toBe(1);
    release();
    await pending;
  });

  it('does not pull a device back into the room it has just left', async () => {
    // A read in flight when the person taps Leave used to land after it and restore the room.
    tonight.lobby = roomOf({ state: 'ballot' });
    tonight.step = 'ballot';
    const release = heldSessionRead(
      () => reply(roomOf({ state: 'ballot' })),
      () => reply(roomOf({ state: 'ballot' }))
    );

    const late = refresh();
    leave();
    release();
    await late;

    expect(tonight.step, 'a late read put the device back into the room').toBe('door');
    expect(tonight.lobby).toBeNull();
  });

  it('still leaves a room the host ended when the read that overtook it failed', async () => {
    // The check sits after the abandoned branch: the end of the evening is never stale.
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
    expect(tonight.notice).toMatch(/ended/);
  });

  it('keeps the newer pair on screen when an earlier round read answers last', async () => {
    // An older round read landing last would put a spent `card_token` on screen.
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

describe('the first household evening (owner instruction of 2026-09-25)', () => {
  it('heads the round with what to expect, and names the cap only once the round runs long', () => {
    // The cap joins the header only once the round runs long (decision 507).
    expect(roundHeader({ answered: 0, cap: 20, typical: 10 })).toBe('Pair 1 · usually about 10');
    expect(roundHeader({ answered: 9, cap: 20, typical: 10 })).toBe('Pair 10 · usually about 10');
    expect(roundHeader({ answered: 9, cap: 20, typical: 10 })).not.toContain('20');
    expect(roundHeader({ answered: 12, cap: 20, typical: 10 })).toBe(
      'Pair 13 · longer than most · max 20'
    );
    expect(roundHeader({ answered: 12, cap: 20, typical: 10 })).not.toContain('about 10');
    expect(roundHeader(null)).toBe('');
  });

  it('keeps the header to what one line of a 390 px phone holds', () => {
    // 36 characters fit the round's bar between Leave and Undo on a 390px phone.
    for (const answered of [0, 9, 10, 18, 98]) {
      const line = roundHeader({ answered, cap: 99, typical: 10 });
      expect(line.length, line).toBeLessThanOrEqual(36);
    }
  });

  it("files the ballot's count under the ballot, which is the number the screen prints", async () => {
    // A submit's frame carries the ballot count; the slate stays.
    vi.stubGlobal('WebSocket', FakeSocket);
    vi.stubGlobal('location', { protocol: 'http:', host: 'host' });
    sockets.length = 0;
    tonight.ballot = { slate: [{ title_id: 1 }], submitted: 0, seated: 2, revealed: false };
    const stop = connect(7);
    try {
      await sockets[0].onmessage({
        data: JSON.stringify({ kind: 'ballot', session_id: 7, submitted: 1, seated: 2 })
      });
      expect(tonight.ballot.submitted).toBe(1);
      expect(tonight.ballot.slate, 'the frame replaced the ballot rather than its count').toEqual([
        { title_id: 1 }
      ]);
      expect(calls, 'a count needs no re-read').toEqual([]);
    } finally {
      stop();
    }
  });

  it('says the vote is in, and how many are still out, once this phone has voted', () => {
    expect(ballotWaitingLine({ submitted: 1, seated: 2 })).toBe('Your vote is in · waiting for 1 more');
    expect(ballotWaitingLine({ submitted: 2, seated: 2 })).toBe('Your vote is in');
    expect(ballotWaitingLine(null)).toBe('');
  });

  it('labels Submit with what it will cast', () => {
    expect(submitLabel(0)).toBe('Submit — none of these');
    expect(submitLabel(1)).toBe('Submit 1 pick');
    expect(submitLabel(3)).toBe('Submit 3 picks');
  });

  it("says how broad each person's yes was, and whose only yes won", () => {
    const result = {
      breadth: [
        { participant_id: 1, name: 'Patrick', approved: 4, of: 4, only_yes: false },
        { participant_id: 2, name: 'Jenny', approved: 1, of: 4, only_yes: true }
      ]
    };
    expect(breadthLine(result)).toBe('Patrick said yes to 4 of 4 · Jenny said yes to 1 of 4');
    expect(onlyYesLines(result)).toEqual(['The only one Jenny said yes to']);
    expect(breadthLine({})).toBe('');
  });

  it("labels a seat's own pick by name", () => {
    expect(pickLabel('Jenny')).toBe("Jenny's pick");
  });

  it('shows what a room has ruled out on its open-rooms row', () => {
    const room = {
      room_code: 'QC-4397', host: 'Patrick', started_at: null, kind: 'movie',
      runtime_budget_min: 130, skips_seen: true,
      vetoes: [{ key: 'violence', label: 'violence' }, { key: 'horror', label: 'horror' }]
    };
    expect(roomVetoLine(room)).toBe('Not tonight: violence, horror');
    expect(roomVetoLine({ ...room, vetoes: [] }), 'nothing ruled out, nothing said').toBe('');
  });

  it('sets the whole veto set, and never a fourth', async () => {
    // A replace, not a toggle; each member's own three (decision 505).
    const other = seatOf(12, { role: 'member', user_id: 2, name: 'Jenny' });
    const full = ['violence', 'horror', 'harrowing'].map((k) => ({ key: k, label: k }));
    world.room = roomOf({
      state: 'open',
      vetoes: full,
      seats: [seatOf(11, { vetoes: [] }), { ...other, vetoes: full }],
      me: seatOf(11, { vetoes: [] })
    });
    tonight.lobby = { ...world.room };
    fetchMock.mockImplementation(async (path, opts = {}) => {
      calls.push({ method: opts.method ?? 'GET', path, body: opts.body ? JSON.parse(opts.body) : null });
      const keys = JSON.parse(opts.body).vetoes;
      const mine = keys.map((k) => ({ key: k, label: k }));
      return reply({
        session_id: 7,
        vetoes: [...full, ...mine],
        seats: [seatOf(11, { vetoes: mine }), { ...other, vetoes: full }]
      });
    });
    expect(myVetoKeys(tonight.lobby, 1), 'the other member used all three of theirs').toEqual([]);
    expect(othersVetoLines(tonight.lobby, 1)).toEqual(['Jenny: violence, horror, harrowing']);
    await toggleVeto('sexual_violence', 1);
    expect(posted('/api/tonight/sessions/7/vetoes')).toEqual({ vetoes: ['sexual_violence'] });
    expect(myVetoKeys(tonight.lobby, 1)).toEqual(['sexual_violence']);
    expect(tonight.lobby.me.vetoes.map((v) => v.key)).toEqual(['sexual_violence']);
    expect(tonight.lobby.vetoes).toHaveLength(4);

    tonight.lobby = {
      ...tonight.lobby,
      me: seatOf(11, { vetoes: [{ key: 'a' }, { key: 'b' }, { key: 'c' }] })
    };
    calls = [];
    await toggleVeto('violence', 1);
    expect(calls, 'a fourth of my own went to the server').toEqual([]);
    expect(MAX_VETOES).toBe(3);
  });

  it("finds this phone's seat from the seats when a reply carries no me", () => {
    // The open and join replies carry the room's seats and no `me`; the lobby read carries both.
    const lobby = roomOf({ me: undefined, seats: [seatOf(11, { vetoes: [{ key: 'horror' }] })] });
    expect(myVetoKeys(lobby, 1)).toEqual(['horror']);
    expect(myVetoKeys(lobby, 99)).toEqual([]);
    expect(othersVetoLines(null, 1)).toEqual([]);
  });

  it('builds the join link from the page origin, and reads one back', () => {
    expect(shareLink('QC-4397', 'https://spielplan.home')).toBe(
      'https://spielplan.home/tonight?room=QC-4397'
    );
    expect(linkedRoom('?room=QC-4397')).toBe('QC-4397');
    expect(linkedRoom('?room=%20')).toBeNull();
    expect(linkedRoom('')).toBeNull();
  });

  it('shows the link on the screen when the phone has no share sheet', async () => {
    vi.stubGlobal('location', { protocol: 'https:', host: 'spielplan.home', origin: 'https://spielplan.home' });
    vi.stubGlobal('navigator', {});
    tonight.lobby = roomOf({ room_code: 'QC-4397' });
    expect(await shareRoom()).toBe('shown');
    expect(tonight.shareUrl).toBe('https://spielplan.home/tonight?room=QC-4397');
    leave();
    expect(tonight.shareUrl, 'the link outlived the room it belongs to').toBe('');
  });

  it('says so when the link went to the clipboard', async () => {
    vi.stubGlobal('location', { protocol: 'https:', host: 'spielplan.home', origin: 'https://spielplan.home' });
    vi.stubGlobal('navigator', { clipboard: { writeText: async () => {} } });
    tonight.lobby = roomOf({ room_code: 'QC-4397' });
    expect(await shareRoom()).toBe('copied');
    expect(toast.message).toBe('Link copied');
    hideToast();
  });

  it('follows a link by joining that room, and does nothing for the room already on screen', async () => {
    fetchMock.mockImplementation(async (path, opts = {}) => {
      calls.push({ method: opts.method ?? 'GET', path, body: opts.body ? JSON.parse(opts.body) : null });
      return reply({ session_id: 9, participant_id: 31, lobby: roomOf({ session_id: 9, room_code: 'QC-4397' }) });
    });
    expect(await followLink('qc-4397')).toBe(9);
    expect(posted('/api/tonight/sessions/join')).toEqual({ session_id: null, room_code: 'qc-4397' });

    calls = [];
    expect(await followLink('QC-4397'), 'the room on screen is joined already').toBe(9);
    expect(calls).toEqual([]);
    expect(await followLink(null)).toBeNull();
  });
});

describe('the second household evening (owner instruction of 2026-09-26)', () => {
  // Map-backed; one below refuses, as a private window does.
  const storage = () => {
    const held = new Map();
    return {
      getItem: (k) => (held.has(k) ? held.get(k) : null),
      setItem: (k, v) => held.set(k, String(v))
    };
  };

  it('says under the slider that the budget is soft, per episode on a series night', () => {
    // §6.2 step 1's softness line, where the budget is set (decision 527).
    expect(budgetSoftLine('movie')).toBe("A little over is fine — we'll say by how much.");
    expect(budgetSoftLine('series')).toContain('per episode');
  });

  it('writes the budget and the summary row the way the app writes a runtime', () => {
    expect([130, 120, 45].map(budgetLabel)).toEqual(['2h 10m', '2h', '45m']);
    // The same minutes read the same on a poster, a title card and a Rank row.
    for (const m of [130, 120, 45]) expect(budgetLabel(m)).toBe(runtimeLabel({ runtime_min: m }));
    expect(settingsTitle({ kind: 'movie', runtime_budget_min: 130 })).toBe('Film · up to 2h 10m');
    expect(settingsTitle({ kind: 'series', runtime_budget_min: 45 })).toBe(
      'Series · up to 45m per episode'
    );
    expect(settingsDetail({ include_rewatches: false, guests: 0 })).toBe('No rewatches, no guests');
    expect(settingsDetail({ include_rewatches: true, guests: 1 })).toBe('Rewatches included, 1 guest');
    // A room's seats already say who is in.
    expect(settingsDetail({ include_rewatches: false })).toBe('No rewatches');
  });

  it('opens the slider at the budget this member last used for this kind', () => {
    // Per member and per kind: a film's 120 is not an episode's (decision 506).
    vi.stubGlobal('localStorage', storage());
    rememberBudget(1, 'movie', 120);
    rememberBudget(1, 'series', 70);
    rememberBudget(2, 'movie', 180);
    expect(rememberedBudget(1, 'movie')).toBe(120);
    expect(rememberedBudget(1, 'series')).toBe(70);
    expect(rememberedBudget(2, 'movie'), 'another member on the same phone').toBe(180);
    expect(rememberedBudget(null, 'movie'), 'nobody signed in').toBeNull();

    tonight.controls.kind = 'movie';
    tonight.controls.runtime_budget_min = 130;
    restoreBudget(1);
    expect(tonight.controls.runtime_budget_min).toBe(120);
    chooseKind('series', 1);
    expect(tonight.controls.runtime_budget_min, 'the kind brings its own number').toBe(70);
    // Nothing remembered means the default, never the number the slider held.
    chooseKind('movie', 3);
    expect(tonight.controls.runtime_budget_min, 'nothing remembered is the default').toBe(
      BUDGET_DEFAULT
    );
    rememberBudget(4, 'movie', 120);
    tonight.controls.kind = 'movie';
    restoreBudget(4);
    chooseKind('series', 4);
    expect(
      tonight.controls.runtime_budget_min,
      "a film night's 120 does not open a series night at 120 min per episode"
    ).toBe(BUDGET_DEFAULT);
    tonight.controls.kind = 'movie';
    tonight.controls.runtime_budget_min = 130;
  });

  it('opens at the default when the stored value is out of range or storage refuses', () => {
    const store = storage();
    store.setItem('spielplan.tonight.budget.1', JSON.stringify({ movie: 9999, series: 'x' }));
    vi.stubGlobal('localStorage', store);
    expect(rememberedBudget(1, 'movie')).toBeNull();
    expect(rememberedBudget(1, 'series')).toBeNull();
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      }
    });
    expect(rememberedBudget(1, 'movie')).toBeNull();
    expect(() => rememberBudget(1, 'movie', 120), 'a refused write stops no evening').not.toThrow();
  });

  it('describes a pair card title for somebody who does not know it', () => {
    expect(
      pairFacts({ year: 1984, kind: 'movie', runtime_min: 117, genres: ['Adventure', 'Animation'] })
    ).toEqual(['1984 · 1h 57m', 'Adventure, Animation']);
    // Over budget: the spec's label on a line of its own, so the card does not wrap mid-phrase.
    expect(
      pairFacts({
        year: 2024, kind: 'movie', runtime_min: 160, over_budget_min: 40,
        fit_line: '40 min over', genres: ['Drama']
      })
    ).toEqual(['2024 · 2h 40m', '40 min over', 'Drama']);
    // Two genres only when they fit half a phone's line; else the first.
    expect(pairFacts({ year: 2009, genres: ['Adventure', 'Science Fiction'] })).toEqual([
      '2009',
      'Adventure'
    ]);
    expect(pairFacts({ year: 2023 }), 'no genres, no empty line').toEqual(['2023']);
    expect(pairFacts(null)).toEqual([]);
  });

  it('tells the lobby what a veto does and how a mood is said, in plain words', () => {
    // Each member's own three, and "may contain" because the pool reads inferred terms too.
    expect(vetoCaption('movie')).toContain('Each of you can rule out up to three');
    expect(vetoCaption('movie')).toContain('A film that may contain');
    // On a series night the vetoes leave out series, and the caption says so (review UX-8).
    expect(vetoCaption('series')).toContain('A series that may contain');
    expect(vetoCaption('series')).not.toContain('film');
    expect(MOOD_CAPTION).toContain(ANSWERS.find((a) => a.value === 'NEITHER').label);
    for (const copy of [vetoCaption('movie'), vetoCaption('series'), MOOD_CAPTION]) {
      expect(copy).not.toMatch(/tier|projected|extracted|tilt|decision|§/i);
    }
  });
});

describe('asking one person first (owner instruction of 2026-09-29)', () => {
  const pairOf = (n) => ({
    a: { title_id: n * 10 + 1, name: `Heat ${n}` },
    b: { title_id: n * 10 + 2, name: `Drive ${n}` }
  });

  // The route as it plays one seat's round: a pair while the round is asked for and running, drawn
  // afresh on every request as a hold-out pair is, and the picks either way.
  function soloRoute({ until = 20 } = {}) {
    let drawn = 0;
    fetchMock.mockImplementation(async (path, opts = {}) => {
      const body = JSON.parse(opts.body);
      calls.push({ method: opts.method ?? 'GET', path, body });
      const answered = body.answers.length;
      const asking = body.sharpen && answered < until;
      drawn += 1;
      return reply({
        picks: [{ title_id: 900 + answered }],
        provenance: 'x',
        answered,
        cap: 20,
        typical: 10,
        escape_available: asking && answered >= 5,
        pair: asking ? pairOf(drawn) : null
      });
    });
  }

  /** The next request, held open until the test lets it land as `landing` answers it. */
  function holdNext(landing) {
    let release = () => {};
    fetchMock.mockImplementationOnce(
      (path, opts) => new Promise((r) => (release = () => r(landing(path, opts))))
    );
    return () => release();
  }

  const said = () => tonight.soloAnswers.map((a) => a.answer);

  it('names the escape for who is asking: one person, or a room', () => {
    expect(SOLO_ESCAPE_LABEL).toBe('Just pick for me');
    expect(ESCAPE_LABEL).toBe('Just pick for us');
  });

  it('opens the round afresh at the door, whatever the last evening left behind', async () => {
    soloRoute();
    tonight.soloAnswers = [{ seq: 1, title_a: 11, title_b: 12, answer: 'A', pair: pairOf(1) }];
    tonight.soloOffset = 3;

    await loadSolo();

    expect(posted('/api/tonight/solo')).toMatchObject({ offset: 0, answers: [], sharpen: true });
    expect(tonight.soloAnswers).toEqual([]);
    expect(tonight.soloOffset).toBe(0);
    expect(tonight.step).toBe('solo');
    expect(tonight.solo.pair, 'the door landed on the picks without asking').toEqual(pairOf(1));
  });

  it('lands on the picks at once when the pool is too small to ask about', async () => {
    soloRoute({ until: 0 });

    await loadSolo();

    expect(tonight.step).toBe('solo');
    expect(tonight.solo.pair).toBeNull();
    expect(tonight.solo.picks).toHaveLength(1);
  });

  it('carries every answer back to the route, and not the pair it keeps for undo', async () => {
    // Stateless (§6.2 step 8): the route replays what the device sends, in `SoloAnswer`'s shape.
    soloRoute();
    await loadSolo();
    await answerSolo('A');
    calls.length = 0;

    await answerSolo('NEITHER');

    expect(posted('/api/tonight/solo')).toMatchObject({ sharpen: true, offset: 0 });
    expect(posted('/api/tonight/solo').answers).toEqual([
      { seq: 1, title_a: 11, title_b: 12, answer: 'A' },
      { seq: 2, title_a: 21, title_b: 22, answer: 'NEITHER' }
    ]);
    expect(tonight.solo.answered).toBe(2);
    expect(tonight.solo.pair).toEqual(pairOf(3));
  });

  it('takes a refused answer back, so a retry does not count the same pair twice', async () => {
    soloRoute();
    await loadSolo();
    fetchMock.mockImplementationOnce(async () => reply({ detail: { message: 'nope' } }, 500));

    await answerSolo('A');

    expect(said(), 'the refused answer stayed on the list').toEqual([]);
    expect(tonight.solo.pair, 'the pair it answered left the screen').toEqual(pairOf(1));
    expect(tonight.error).toBe('nope');

    calls.length = 0;
    await answerSolo('A');
    expect(posted('/api/tonight/solo').answers).toEqual([
      { seq: 1, title_a: 11, title_b: 12, answer: 'A' }
    ]);
    expect(tonight.error, 'the answer that landed left the refusal standing').toBe('');
  });

  it('ignores a second tap while the first is in flight', async () => {
    soloRoute();
    await loadSolo();
    calls.length = 0;
    const release = holdNext(fetchMock.getMockImplementation());

    const first = answerSolo('A');
    await answerSolo('B');
    await undoSolo();
    release();
    await first;

    expect(calls, 'a tap in flight sent a request of its own').toHaveLength(1);
    expect(said(), 'a tap in flight changed the answers').toEqual(['A']);
    expect(tonight.solo.pair).toEqual(pairOf(2));
  });

  it('sends nothing for an answer or an undo it cannot act on', async () => {
    soloRoute({ until: 1 });
    await loadSolo();
    calls.length = 0;

    await undoSolo();
    await answerSolo('MAYBE');
    expect(calls, 'nothing to take back, and no such answer').toEqual([]);

    await answerSolo('A');
    expect(tonight.solo.pair, 'the round had one pair to ask').toBeNull();
    calls.length = 0;
    await answerSolo('B');
    await undoSolo();

    expect(calls, 'a tap with no pair on screen reached the route').toEqual([]);
    expect(said()).toEqual(['A']);
  });

  it('takes the last answer back and puts the pair it answered back on screen', async () => {
    // A hold-out pair is drawn afresh on every request, so the route cannot say which one it was.
    soloRoute();
    await loadSolo();
    await answerSolo('A');
    await answerSolo('B');
    calls.length = 0;

    await undoSolo();

    expect(posted('/api/tonight/solo')).toMatchObject({
      sharpen: true,
      answers: [{ seq: 1, answer: 'A' }]
    });
    expect(said()).toEqual(['A']);
    expect(tonight.solo.answered).toBe(1);
    expect(tonight.solo.pair, 'the undo showed a fresh pair, not the one taken back').toEqual(pairOf(2));

    calls.length = 0;
    await answerSolo('EITHER');
    expect(posted('/api/tonight/solo').answers.at(-1)).toEqual({
      seq: 2,
      title_a: 21,
      title_b: 22,
      answer: 'EITHER'
    });
  });

  it('puts no pair back when the route has ended the round the undo went into', async () => {
    soloRoute();
    await loadSolo();
    await answerSolo('A');
    fetchMock.mockImplementationOnce(async () =>
      reply({ picks: [{ title_id: 900 }], provenance: 'x', answered: 0, pair: null })
    );

    await undoSolo();

    expect(said()).toEqual([]);
    expect(tonight.solo.pair, 'the undo reopened a round the route has ended').toBeNull();
    expect(tonight.step).toBe('solo');
  });

  it('keeps the answer an undo was refused for, and the pair on screen', async () => {
    soloRoute();
    await loadSolo();
    await answerSolo('A');
    await answerSolo('EITHER');
    fetchMock.mockImplementationOnce(async () => {
      throw new TypeError('Load failed');
    });

    await undoSolo();

    expect(said(), 'a refused undo lost the answer anyway').toEqual(['A', 'EITHER']);
    expect(tonight.solo.pair).toEqual(pairOf(3));
    expect(tonight.error).not.toBe('');
  });

  it('keeps the escape shut until the sixth pair, then ends the round without a request', async () => {
    // The picks in hand already carry every answer, so the escape has nothing to ask.
    soloRoute();
    await loadSolo();
    for (const value of ['A', 'B', 'EITHER', 'NEITHER']) await answerSolo(value);
    expect(tonight.solo.escape_available).toBe(false);
    calls.length = 0;

    escapeSolo();
    expect(tonight.solo.pair, 'the escape opened on the fifth pair').toEqual(pairOf(5));
    expect(calls).toEqual([]);

    await answerSolo('A');
    expect(tonight.solo.escape_available).toBe(true);
    const picks = tonight.solo.picks;
    calls.length = 0;

    escapeSolo();

    expect(calls, 'the escape asked the route for picks it had already sent').toEqual([]);
    expect(tonight.solo.pair).toBeNull();
    expect(tonight.solo.escape_available).toBe(false);
    expect(tonight.solo.picks).toEqual(picks);
    expect(tonight.step).toBe('solo');
    expect(said()).toHaveLength(5);
  });

  it("leaves a refused answer's complaint behind with the round it was about", async () => {
    soloRoute();
    await loadSolo();
    for (const value of ['A', 'B', 'EITHER', 'NEITHER', 'A']) await answerSolo(value);
    fetchMock.mockImplementationOnce(async () => reply({ detail: 'boom' }, 500));
    await answerSolo('B');
    expect(tonight.error).not.toBe('');
    expect(tonight.solo.escape_available).toBe(true);

    escapeSolo();

    expect(tonight.solo.pair).toBeNull();
    expect(tonight.error, 'the picks opened under an error about the round').toBe('');
  });

  it('lands on the picks once the round ends, and Reshuffle walks them with every answer', async () => {
    soloRoute({ until: 2 });
    await loadSolo();
    await answerSolo('A');
    await answerSolo('B');
    expect(tonight.solo.pair).toBeNull();
    expect(tonight.step).toBe('solo');
    calls.length = 0;

    await loadSolo({ reshuffle: true });

    expect(posted('/api/tonight/solo')).toMatchObject({ sharpen: false, offset: 1 });
    const walked = posted('/api/tonight/solo').answers.map((a) => a.answer);
    expect(walked, 'the walk dropped the answers that tilt it').toEqual(['A', 'B']);
    expect(tonight.solo.pair, 'a reshuffle reopened the round').toBeNull();
  });

  it('stays at the door when a solo reply lands after Leave', async () => {
    // Leave is the round's own Back, so a late pair must not pull the device back into it.
    const release = holdNext(async () =>
      reply({ picks: [], provenance: 'x', answered: 0, pair: pairOf(1) })
    );
    const late = loadSolo();
    leave();
    release();
    await late;

    expect(tonight.step, 'a late solo reply pulled the device off the door').toBe('door');
    expect(tonight.busy).toBe(false);

    const refused = holdNext(async () => reply({ detail: { message: 'nope' } }, 500));
    const lateRefusal = loadSolo();
    leave();
    refused();
    await lateRefusal;

    expect(tonight.error, 'a late refusal complained at the door').toBe('');
  });
});
