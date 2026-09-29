// The card is a sealed single-use token this module never opens; the pool is never sent here;
// the waiting view shows counts only. Channel frames are nudges to re-read, never payloads.

import { ApiError, get, post } from '$lib/api.js';
import { runtimeLabel } from '$lib/rate.svelte.js';
import { showToast } from '$lib/toast.svelte.js';

// Proposal 57's bounds and default: §6.2 gives none.
export const BUDGET_MIN = 60;
export const BUDGET_MAX = 200;
export const BUDGET_STEP = 5;
export const BUDGET_DEFAULT = 130;

/**
 * The budget is soft, said where it is set; on a series night it is per episode (decision 527).
 * @param {string} kind
 */
export function budgetSoftLine(kind) {
  return kind === 'series'
    ? "A little over is fine — we'll say by how much, per episode."
    : "A little over is fine — we'll say by how much.";
}

/** "2h 10m", "2h", "45m": the one runtime format; a series night adds "per episode" itself. */
export function budgetLabel(minutes) {
  return runtimeLabel({ runtime_min: minutes }) ?? '';
}

/** The summary row: "Film · up to 2h 10m", per episode on a series night. @param {any} controls */
export function settingsTitle({ kind, runtime_budget_min }) {
  const per = kind === 'series' ? ' per episode' : '';
  return `${kind === 'series' ? 'Series' : 'Film'} · up to ${budgetLabel(runtime_budget_min)}${per}`;
}

/** "No rewatches, no guests"; a room's seats say who is in, so it passes no guests. */
export function settingsDetail({ include_rewatches, guests = null }) {
  const rewatches = include_rewatches ? 'Rewatches included' : 'No rewatches';
  if (guests === null) return rewatches;
  return `${rewatches}, ${guests ? `${guests} ${guests === 1 ? 'guest' : 'guests'}` : 'no guests'}`;
}

/** Where this device remembers each member's last budget, per kind (decision 506). */
const BUDGET_MEMORY = 'spielplan.tonight.budget';

/**
 * Per kind, because a film's 120 is not an episode's (decision 219); per member, because the chip
 * can switch who holds a shared phone. Storage may be absent, and then the default applies.
 * @param {number|null|undefined} userId @param {string} kind
 */
export function rememberedBudget(userId, kind) {
  if (userId == null) return null;
  try {
    const all = JSON.parse(localStorage.getItem(`${BUDGET_MEMORY}.${userId}`) ?? '{}');
    const minutes = Number(all?.[kind]);
    return Number.isFinite(minutes) && minutes >= BUDGET_MIN && minutes <= BUDGET_MAX
      ? minutes
      : null;
  } catch {
    return null;
  }
}

/** Remember the budget an evening was actually opened with. @param {number|null|undefined} userId */
export function rememberBudget(userId, kind, minutes) {
  if (userId == null) return;
  try {
    const key = `${BUDGET_MEMORY}.${userId}`;
    const all = JSON.parse(localStorage.getItem(key) ?? '{}') ?? {};
    localStorage.setItem(key, JSON.stringify({ ...all, [kind]: minutes }));
  } catch {
    // Storage refused (a private window, a full quota): the evening still opens.
  }
}

/**
 * With nothing remembered the default applies, never the number the slider holds (decision 506).
 * @param {number|null|undefined} userId
 */
export function restoreBudget(userId) {
  tonight.controls.runtime_budget_min =
    rememberedBudget(userId, tonight.controls.kind) ?? BUDGET_DEFAULT;
}

/**
 * Switching kind brings that kind's remembered budget, else the default.
 * @param {string} kind @param {number|null|undefined} userId
 */
export function chooseKind(kind, userId) {
  tonight.controls.kind = kind;
  restoreBudget(userId);
}

/** §6.2 step 1: "members and/or N guests", who share the initiator's phone. */
export const MAX_GUESTS = 6;

/** Decision 154's four answers: `EITHER` lifts both, `NEITHER` lowers both. */
export const ANSWERS = [
  { value: 'A', label: 'This one' },
  { value: 'B', label: 'That one' },
  { value: 'EITHER', label: 'Either is fine' },
  { value: 'NEITHER', label: 'Neither tonight' }
];

export const ESCAPE_LABEL = 'Just pick for us';
export const SOLO_ESCAPE_LABEL = 'Just pick for me';

/** 54e/proposal 60: "shipping the property without the moment ships half of it." */
export const REVEAL_BEAT = "Tonight's pick";

// Push can go missing (§6 preamble), so the caption names the channels that cannot.
export const JOIN_CAPTION =
  'Missed a notification? The code and the link always reach the same room.';

// §6.4's wildcard, honestly labelled wherever it is offered.
export const WILDCARD_LINE = 'A step outside your usual';

// The QR that §6.2 step 2 also names is still owed and not promised here.
export const SHARE_CAPTION = 'Read the code out, or share the link.';

// Not RESERVED_LABEL, the axis counterweight's, which would mislabel somebody's favourite.
export function pickLabel(name) {
  return `${name}'s pick`;
}

// Per member (decision 505); the server enforces it too.
export const MAX_VETOES = 3;

/**
 * Each member holds their own three; "may contain" because the pool also reads the inferred tier.
 * @param {string} kind
 */
export function vetoCaption(kind) {
  const title = kind === 'series' ? 'A series' : 'A film';
  return `Each of you can rule out up to three. ${title} that may contain any of them is left out for everyone tonight.`;
}

// The round's answers already carry the mood, so the lobby says how rather than adding a question.
export const MOOD_CAPTION =
  'In a particular mood? In each pair, pick the one that fits it, and tap “Neither tonight” when neither does. Your answers steer the pick.';

// 54d's reserved finalist; the fact itself is the payload's `reserved` flag (decision 220).
export const RESERVED_LABEL = 'The other side of the split';

// `SoloBody.offset`'s bound (`le=64` in `api/tonight.py`), which the client cannot discover.
const SOLO_OFFSET_MAX = 64;

// The reshuffle walk wraps; say so, or a repeat of the same three looks broken.
export const WRAPPED_LINE = 'Back round to the top of the ranking';

export const tonight = $state({
  loading: true,
  booted: false,
  busy: false,
  error: '',
  /** News for the door, not a failure: the host ended the evening. */
  notice: '',
  /** 'door' | 'lobby' | 'round' | 'waiting' | 'ballot' | 'reveal' | 'solo' */
  step: 'door',
  controls: {
    kind: 'movie',
    runtime_budget_min: BUDGET_DEFAULT,
    include_rewatches: false,
    guests: 0
  },
  /** @type {any[]} §6.2 step 2's open-rooms list, live over the channel. */
  rooms: [],
  /** @type {any} */
  lobby: null,
  /** @type {any} the round state for the seat this device is playing; `pair` is null once that
   * seat's round has ended */
  round: null,
  /** @type {number|null} the seat this device is acting for: its own, or a guest's for the length
   * of their turn on the host's phone. Every re-read goes through it. */
  activeSeat: null,
  /** @type {number[]} the seats this device has cast a ballot for, its own and its guests' */
  submittedSeats: [],
  /** @type {any[]} */
  progress: [],
  /** @type {any} */
  ballot: null,
  /** @type {number[]} what I have ticked, before I submit */
  approved: [],
  /** @type {any} */
  result: null,
  /** @type {any} the last solo reply: the round while it carries a `pair`, then the picks */
  solo: null,
  /** @type {any[]} solo's round, carried by the client because §6.2 step 8 mints no session row
   * and therefore no `session_answer` to hold them. Each keeps the pair it answered, for undo. */
  soloAnswers: [],
  soloOffset: 0,
  /** @type {string} the join link, shown once Share fell back to the clipboard or nothing */
  shareUrl: ''
});

function fail(err) {
  tonight.error =
    err instanceof ApiError ? err.detail?.message || err.message : 'Something went wrong.';
}

// When the pair on screen arrived, for §4.2's `latency_ms` (module-level: a local read in the
// same statement wrote 0 on every row).
let shownAt = 0;

// Null, never 0, when nothing has been shown: "not measured" is not "answered instantly".
function latency() {
  return shownAt ? Math.max(0, Date.now() - shownAt) : null;
}

// Stop the clock when the pair leaves the screen (a tap on Rank mid-round); `loadRound` re-arms it.
export function stopClock() {
  shownAt = 0;
}

// This background read may clear only the complaint it wrote itself, never someone else's refusal.
let roomsComplaint = '';

export async function loadRooms() {
  try {
    tonight.rooms = (await get('/tonight/rooms')).rooms;
    if (roomsComplaint && tonight.error === roomsComplaint) tonight.error = '';
    roomsComplaint = '';
  } catch (err) {
    fail(err);
    roomsComplaint = tonight.error;
  }
}

// Come back to the room this device is seated in: a reload must not cost anyone their evening.
export async function bootstrap() {
  tonight.loading = true;
  try {
    await loadRooms();
    const mine = tonight.rooms.find((r) => r.viewer_seated);
    if (mine) {
      tonight.lobby = { session_id: mine.session_id };
      await refresh();
      return mine.session_id;
    }
  } finally {
    tonight.loading = false;
    tonight.booted = true;
  }
  return null;
}

export async function openRoom() {
  tonight.busy = true;
  try {
    const room = await post('/tonight/sessions', tonight.controls);
    applyLobby(room.lobby);
    tonight.step = 'lobby';
    tonight.error = '';
    return room;
  } catch (err) {
    fail(err);
    return null;
  } finally {
    tonight.busy = false;
  }
}

// One function behind the code, the open-rooms tap and the link: "all equivalent" (§6.2 step 2).
export async function join({ sessionId = null, roomCode = null } = {}) {
  tonight.busy = true;
  try {
    const joined = await post('/tonight/sessions/join', {
      session_id: sessionId,
      room_code: roomCode
    });
    applyLobby(joined.lobby);
    tonight.step = 'lobby';
    tonight.error = '';
    return joined;
  } catch (err) {
    fail(err);
    return null;
  } finally {
    tonight.busy = false;
  }
}

function applyLobby(lobby) {
  tonight.lobby = lobby;
  tonight.progress = lobby?.progress ?? tonight.progress;
}

/**
 * The seat to re-read: `activeSeat` while its turn is not `done`, else this device's own. Re-reading
 * `me` replaced a guest's round with the host's on every household frame.
 *
 * @param {any} seen @param {(seat: any) => boolean} done
 */
function seatToRead(seen, done) {
  const held = (seen.seats ?? []).find((s) => s.participant_id === tonight.activeSeat);
  if (held && !done(held)) return held.participant_id;
  return seen.me?.participant_id ?? null;
}

// The guest seats this device still owes a ballot for: the reveal waits for guests too. Host-only,
// and tracked locally (a reload re-offers a turn; a ballot submit replaces rather than doubles).
export function ballotTurns() {
  const lobby = tonight.lobby;
  const host = lobby?.host?.user_id ?? null;
  if (host === null || lobby?.me?.user_id !== host) return [];
  return (lobby.seats ?? []).filter(
    (s) => s.role === 'guest' && !tonight.submittedSeats.includes(s.participant_id)
  );
}

// Voted for every seat it holds, its own and its guests': either alone would leave a vote uncast.
function ballotDone() {
  const mine = tonight.lobby?.me?.participant_id ?? null;
  if (mine === null || !tonight.submittedSeats.includes(mine)) return false;
  return ballotTurns().length === 0;
}

// Overlapping refreshes: a slow earlier read must not write over a newer one. Separate from
// `roundSeq`, whose triggers differ.
let refreshSeq = 0;

// `seat` names the seat this read is about, for the escape; otherwise `activeSeat`, else our own.
export async function refresh({ seat = null } = {}) {
  if (!tonight.lobby) return;
  const mine = ++refreshSeq;
  try {
    const seen = await get(`/tonight/sessions/${tonight.lobby.session_id}`);
    // The host ended the evening (decision 169): out to the door with the reason.
    if (seen.state === 'abandoned') {
      leave();
      await loadRooms();
      tonight.notice = 'This evening has ended.';
      return;
    }
    // After the abandoned branch: an ended room is never stale, and the newer read may fail.
    if (mine !== refreshSeq) return;      // a newer read has already answered
    tonight.lobby = seen;
    tonight.progress = seen.progress;
    // The room carries the ballot's counts, not its slate: a re-read must not blank the ballot.
    tonight.ballot = seen.ballot && { ...tonight.ballot, ...seen.ballot };
    if (seen.ballot?.revealed) await loadResult();
    else if (seen.state === 'ballot') {
      if (!ballotDone()) {
        tonight.activeSeat = seatToRead(seen, (s) =>
          tonight.submittedSeats.includes(s.participant_id)
        );
        await loadBallot();
      }
    } else if (seen.state === 'voting') {
      const playing = seat ?? seatToRead(seen, (s) => !!s.ended_by);
      if (playing !== null) await loadRound(playing);
    } else if (seen.state === 'open') {
      // A reload into an open, unstarted room lands in its lobby.
      tonight.step = 'lobby';
    }
  } catch (err) {
    fail(err);
  }
}

// Step out to the door keeping the seat (it lives on the server). The room's state goes too, and
// every read in flight is dropped, or a late answer would drag the device back in.
export function leave() {
  refreshSeq += 1;
  roundSeq += 1;
  soloSeq += 1;
  tonight.lobby = null;
  tonight.round = null;
  tonight.ballot = null;
  tonight.result = null;
  tonight.progress = [];
  tonight.approved = [];
  // Both belong to one room.
  tonight.activeSeat = null;
  tonight.submittedSeats = [];
  // The link on the screen belongs to the room being left. [decision 481]
  tonight.shareUrl = '';
  // No pair is on screen, so the latency clock stops.
  stopClock();
  tonight.step = 'door';
  tonight.error = '';
  tonight.notice = '';
}

export async function start() {
  tonight.busy = true;
  try {
    await post(`/tonight/sessions/${tonight.lobby.session_id}/start`, {});
    await refresh();
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

// Its own counter: this is `refresh`'s tail and `answer`'s 409 retry, so two reads can overlap.
let roundSeq = 0;

export async function loadRound(participantId) {
  const mine = ++roundSeq;
  try {
    const seen = await get(`/tonight/seats/${participantId}/round`);
    if (mine !== roundSeq) return;        // a newer read has already answered
    // Armed by a new card (the token), not by a frame re-reading the one on screen, or onto a screen
    // that was not showing one (`stopClock` ran).
    if (seen.card_token !== tonight.round?.card_token || !shownAt) shownAt = Date.now();
    tonight.round = seen;
    // Recorded here, where every path lands, so the hand-off cannot drift from itself.
    tonight.activeSeat = participantId;
    tonight.step = seen.pair ? 'round' : 'waiting';
  } catch (err) {
    fail(err);
  }
}

// Posts the sealed card back and nothing else, so §13's held-out stream stays a server fact.
export async function answer(value) {
  const state = tonight.round;
  if (!state?.card_token || tonight.busy) return;
  tonight.busy = true;
  try {
    const next = await post(`/tonight/seats/${state.participant_id}/answer`, {
      card_token: state.card_token,
      answer: value,
      latency_ms: latency()
    });
    tonight.round = next;
    // Most pairs arrive on this path, so the clock re-arms here too.
    shownAt = Date.now();
    tonight.step = next.pair ? 'round' : 'waiting';
    tonight.error = '';
    if (!next.pair) await refresh();
  } catch (err) {
    fail(err);
    // A stale card means the world moved — re-read rather than leaving a dead button.
    if (err instanceof ApiError && err.status === 409) await loadRound(state.participant_id);
  } finally {
    tonight.busy = false;
  }
}

export async function undo() {
  const state = tonight.round;
  if (!state || tonight.busy) return;
  tonight.busy = true;
  try {
    tonight.round = await post(`/tonight/seats/${state.participant_id}/undo`, {});
    // An undo re-issues the same card, new to the screen, so the clock re-arms.
    shownAt = Date.now();
    tonight.step = tonight.round.pair ? 'round' : 'waiting';
    tonight.error = '';
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

// Availability comes from the server, so the client is not a second implementation.
export async function escape() {
  const state = tonight.round;
  if (!state?.escape_available || tonight.busy) return;
  // Held before the write: the escape ends this seat, and this frame must still show it.
  const seat = state.participant_id;
  tonight.busy = true;
  try {
    tonight.round = await post(`/tonight/seats/${seat}/escape`, {});
    tonight.step = 'waiting';
    await refresh({ seat });
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

export async function loadBallot() {
  try {
    const ballot = await get(`/tonight/sessions/${tonight.lobby.session_id}/ballot`);
    if (!tonight.lobby) return;
    tonight.ballot = ballot;
    tonight.step = ballot.revealed ? 'reveal' : 'ballot';
    if (tonight.ballot.revealed) await loadResult();
  } catch (err) {
    fail(err);
  }
}

export function toggleApproval(titleId) {
  tonight.approved = tonight.approved.includes(titleId)
    ? tonight.approved.filter((t) => t !== titleId)
    : [...tonight.approved, titleId];
}

// Clears `approved`, or the incoming guest would open on the previous person's ticks.
export function handBallot(participantId) {
  tonight.activeSeat = participantId;
  tonight.approved = [];
  tonight.step = 'ballot';
  tonight.error = '';
}

export async function submitBallot(participantId) {
  if (tonight.busy || participantId === null || participantId === undefined) return;
  tonight.busy = true;
  try {
    const out = await post(`/tonight/seats/${participantId}/ballot`, {
      approved: tonight.approved
    });
    tonight.ballot = { ...tonight.ballot, ...out };
    // Recorded first: whether a guest still owes a vote decides the step.
    tonight.submittedSeats = [...tonight.submittedSeats, participantId];
    tonight.approved = [];
    // Back to the owner if their vote is outstanding; never advanced to a guest without a hand-off tap.
    const mine = tonight.lobby?.me?.participant_id ?? null;
    if (mine !== null && !tonight.submittedSeats.includes(mine)) tonight.activeSeat = mine;
    tonight.step = out.revealed ? 'reveal' : ballotDone() ? 'waiting' : 'ballot';
    if (out.revealed) await loadResult();
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

// A stalled room is otherwise live for ever; the rows stay, so this ends and never deletes.
export async function endRoom() {
  if (!tonight.lobby || tonight.busy) return;
  tonight.busy = true;
  try {
    await post(`/tonight/sessions/${tonight.lobby.session_id}/end`, {});
    await refresh();
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

export async function loadResult() {
  try {
    const result = await get(`/tonight/sessions/${tonight.lobby.session_id}/result`);
    if (!tonight.lobby) return;
    tonight.result = result;
    tonight.step = 'reveal';
    tonight.error = '';
  } catch (err) {
    // 409 here is the blind rule holding, not a failure: somebody has not submitted yet.
    if (!(err instanceof ApiError && err.status === 409)) fail(err);
  }
}

// Bumped by `leave()`, so a solo reply that lands after Back cannot pull the device off the door.
let soloSeq = 0;

// One solo request. `ask` serves the round's next pair; a walk asks for none, so its reply is
// always the picks. False when refused or overtaken.
async function postSolo({ ask, walk = false }) {
  if (tonight.busy) return false;
  tonight.busy = true;
  const mine = ++soloSeq;
  // Where the walk is, so a refused press can be taken back below.
  const walked = tonight.soloOffset;
  // Never past what the route serves (`SOLO_OFFSET_MAX`).
  if (walk) tonight.soloOffset = Math.min(tonight.soloOffset + 1, SOLO_OFFSET_MAX);
  try {
    const reply = await post('/tonight/solo', {
      ...tonight.controls,
      offset: tonight.soloOffset,
      answers: tonight.soloAnswers.map(({ seq, title_a, title_b, answer }) => ({
        seq,
        title_a,
        title_b,
        answer
      })),
      sharpen: ask
    });
    if (mine !== soloSeq) return false;
    tonight.solo = reply;
    tonight.step = 'solo';
    tonight.error = '';
    return true;
  } catch (err) {
    if (mine !== soloSeq) return false;
    fail(err);
    // A refused walk has not walked.
    tonight.soloOffset = walked;
    return false;
  } finally {
    tonight.busy = false;
  }
}

// Solo asks first: the door opens on the round, and the picks follow it (decision 532).
export async function loadSolo({ reshuffle = false } = {}) {
  if (reshuffle) return postSolo({ ask: false, walk: true });
  if (tonight.busy) return false;
  tonight.soloAnswers = [];
  tonight.soloOffset = 0;
  return postSolo({ ask: true });
}

// A refused answer is taken back, or a retry would count the same pair twice.
export async function answerSolo(value) {
  const pair = tonight.solo?.pair;
  if (!pair || tonight.busy || !ANSWERS.some((a) => a.value === value)) return;
  const before = tonight.soloAnswers;
  tonight.soloAnswers = [
    ...before,
    {
      seq: before.length + 1,
      title_a: pair.a.title_id,
      title_b: pair.b.title_id,
      answer: value,
      pair
    }
  ];
  if (!(await postSolo({ ask: true }))) tonight.soloAnswers = before;
}

// No tombstone to write: the last answer leaves the list, and the pair it answered comes back,
// since a hold-out pair is drawn afresh on every request.
export async function undoSolo() {
  const before = tonight.soloAnswers;
  const last = before.at(-1);
  if (!last || !tonight.solo?.pair || tonight.busy) return;
  tonight.soloAnswers = before.slice(0, -1);
  if (!(await postSolo({ ask: true }))) {
    tonight.soloAnswers = before;
    return;
  }
  if (tonight.solo.pair) tonight.solo = { ...tonight.solo, pair: last.pair };
}

// Availability comes from the server. Nothing is sent: the picks in hand carry every answer.
export function escapeSolo() {
  if (!tonight.solo?.escape_available || tonight.busy) return;
  tonight.solo = { ...tonight.solo, pair: null, escape_available: false };
}

// Backoff bounds: a fixed short retry hammers the backend restart a bundle swap ends in.
export const RECONNECT_MIN_MS = 1000;
export const RECONNECT_MAX_MS = 30000;
// Jitter narrower than the doubling, so the waits still grow strictly.
const RECONNECT_JITTER = 0.2;

// Exported: the whole rule, testable without running a socket.
export function reconnectDelay(attempt, random = Math.random) {
  const base = Math.min(RECONNECT_MIN_MS * 2 ** attempt, RECONNECT_MAX_MS);
  return Math.round(base * (1 - RECONNECT_JITTER + random() * 2 * RECONNECT_JITTER));
}

// Frames are nudges to re-read, never payloads, so a dropped frame costs a stale lobby, not a wrong one.
export function connect(sessionId = null) {
  if (typeof WebSocket === 'undefined') return () => {};
  let socket = null;
  let closed = false;
  let retry;
  let attempt = 0;

  const open = () => {
    if (closed) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const suffix = sessionId ? `?session_id=${sessionId}` : '';
    socket = new WebSocket(`${proto}//${location.host}/api/tonight/channel${suffix}`);
    // Re-read only on a connection that opened, and reset the backoff there.
    socket.onopen = async () => {
      if (closed) return;
      attempt = 0;
      await refresh();
      await loadRooms();
    };
    socket.onmessage = async (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      if (frame.kind === 'rooms.changed') {
        await loadRooms();
        // A household frame re-reads the lobby too: the room's own frame is missed if connect raced a join.
        if (tonight.lobby) await refresh();
      } else if (frame.kind === 'progress') tonight.progress = frame.participants;
      else if (frame.kind === 'ballot') applyBallotCount(frame);
      else if (frame.kind === 'lobby' || frame.kind === 'reveal') await refresh();
    };
    // Sockets close silently (a locked phone, a proxy timeout), so retry, with backoff.
    socket.onclose = () => {
      if (closed) return;
      retry = setTimeout(open, reconnectDelay(attempt));
      attempt += 1;
    };
  };

  open();
  return () => {
    closed = true;
    clearTimeout(retry);
    socket?.close();
  };
}

/** §6.2 step 2's row under the host's name: the code read out across the room, and how long ago. */
export function roomLine(room) {
  const age = minutesAgo(room.started_at);
  const started = age === null ? null : age ? `Started ${age} min ago` : 'Started just now';
  return [room.room_code, started].filter(Boolean).join(' · ');
}

/** What evening a listed room is. */
export function roomEvening(room) {
  const kind = room.kind === 'series' ? 'Series' : 'Film';
  // On a series night the budget bounds minutes per episode (decision 219).
  const per = room.kind === 'series' ? ' per episode' : '';
  const rewatches = room.skips_seen ? 'no rewatches' : 'rewatches included';
  return `${kind} up to ${budgetLabel(room.runtime_budget_min)}${per}, ${rewatches}`;
}

/** Decision 480: what a room has ruled out, so somebody deciding whether to join knows. */
export function roomVetoLine(room) {
  return room.vetoes?.length ? `Not tonight: ${room.vetoes.map((v) => v.label).join(', ')}` : '';
}

export function minutesAgo(iso) {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.round((Date.now() - then) / 60000));
}

/**
 * Where everyone but the seat on this screen has got to, and never an answer (54c): "Patrick 6/6
 * done", "Jenny 12 so far", "Mia 4/~10". The ~ is the typical round until the seat reaches it (507).
 * @param {any[]} progress @param {number|null} holding
 */
export function progressLines(progress, holding = null) {
  return progress
    .filter((p) => p.participant_id !== holding)
    .map((p) => ({
      participant_id: p.participant_id,
      name: p.name,
      line: p.finished
        ? `${p.name} ${p.answered}/${p.answered} done`
        : p.expected == null
          ? `${p.name} ${p.answered} so far`
          : `${p.name} ${p.answered}/~${p.expected}`
    }));
}

/** The line's end, "waiting for 2": every seat still answering. @param {any[]} progress */
export function waitingLine(progress) {
  const left = progress.filter((p) => !p.finished).length;
  return left ? `Waiting for ${left}` : '';
}

/** §13's approval share, said as the people it counts. */
export function approvalShare(result) {
  if (!result) return '';
  const n = result.participants;
  const yes = Math.round(result.approval_share * n);
  if (yes === n && n === 2) return 'Both of you said yes';
  if (yes === n && n > 2) return `All ${n} of you said yes`;
  return `${yes} of ${n} said yes`;
}

// The ballot frame carries the submitted count; only the count changes.
export function applyBallotCount(frame) {
  if (!tonight.ballot) return;
  tonight.ballot = { ...tonight.ballot, submitted: frame.submitted, seated: frame.seated };
}

// An estimate that stays true (decision 507), on one line of a 390px phone (36 characters).
export function roundHeader(round) {
  if (!round) return '';
  const n = (round.answered ?? 0) + 1;
  const typical = round.typical ?? 10;
  if (n <= typical) return `Pair ${n} · usually about ${typical}`;
  return [`Pair ${n}`, 'longer than most', round.cap ? `max ${round.cap}` : null]
    .filter(Boolean)
    .join(' · ');
}

// Characters of genres that fit half a phone's width.
const PAIR_GENRE_CHARS = 22;

// Year and runtime, the over-budget label on its own line, and genres in plain words.
export function pairFacts(title) {
  if (!title) return [];
  const genres = title.genres ?? [];
  const both = genres.slice(0, 2).join(', ');
  return [
    [title.year, runtimeLabel(title)].filter(Boolean).join(' · '),
    title.over_budget_min ? title.fit_line : null,
    both.length <= PAIR_GENRE_CHARS ? both : genres[0]
  ].filter(Boolean);
}

export function ballotWaitingLine(ballot) {
  if (!ballot) return '';
  const left = Math.max(0, (ballot.seated ?? 0) - (ballot.submitted ?? 0));
  return left ? `Your vote is in · waiting for ${left} more` : 'Your vote is in';
}

export function submitLabel(count) {
  if (!count) return 'Submit — none of these';
  return `Submit ${count} ${count === 1 ? 'pick' : 'picks'}`;
}

export function breadthLine(result) {
  const rows = result?.breadth ?? [];
  return rows.map((b) => `${b.name} said yes to ${b.approved} of ${b.of}`).join(' · ');
}

export function onlyYesLines(result) {
  return (result?.breadth ?? []).filter((b) => b.only_yes).map((b) => `The only one ${b.name} said yes to`);
}

/**
 * The open and join replies carry the seats but no `me`.
 * @param {any} lobby @param {number|null|undefined} userId
 */
export function mySeat(lobby, userId) {
  return lobby?.me ?? (lobby?.seats ?? []).find((s) => s.user_id === userId && userId != null) ?? null;
}

/** The chips THIS member has on, by key (decision 505). @param {number|null|undefined} userId */
export function myVetoKeys(lobby, userId) {
  return (mySeat(lobby, userId)?.vetoes ?? []).map((v) => v.key);
}

/**
 * Each member holds their own three (decision 505), so the others' are named by member.
 * @param {any} lobby @param {number|null|undefined} userId
 */
export function othersVetoLines(lobby, userId) {
  const me = mySeat(lobby, userId);
  return (lobby?.seats ?? [])
    .filter((s) => s.participant_id !== me?.participant_id && (s.vetoes ?? []).length)
    .map((s) => `${s.name}: ${s.vetoes.map((v) => v.label).join(', ')}`);
}

/**
 * Replaces this member's whole set, so two taps cannot leave half of each (decision 480).
 * @param {string[]} keys @param {number|null|undefined} userId
 */
export async function setVetoes(keys, userId) {
  if (!tonight.lobby || tonight.busy) return;
  tonight.busy = true;
  try {
    const out = await post(`/tonight/sessions/${tonight.lobby.session_id}/vetoes`, { vetoes: keys });
    const me = mySeat(tonight.lobby, userId);
    const seats = out.seats ?? tonight.lobby.seats;
    tonight.lobby = {
      ...tonight.lobby,
      vetoes: out.vetoes,
      seats,
      me: me ? (seats ?? []).find((s) => s.participant_id === me.participant_id) ?? me : me
    };
    tonight.error = '';
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

/** @param {string} key @param {number|null|undefined} userId */
export async function toggleVeto(key, userId) {
  const now = myVetoKeys(tonight.lobby, userId);
  const next = now.includes(key) ? now.filter((k) => k !== key) : [...now, key];
  if (next.length > MAX_VETOES) return;
  await setVetoes(next, userId);
}

/** The room's join link (decision 481): the QR's missing half, and what the push carries too. */
export function shareLink(code, origin = typeof location === 'undefined' ? '' : location.origin) {
  return `${origin}/tonight?room=${encodeURIComponent(code)}`;
}

export function linkedRoom(search) {
  const code = new URLSearchParams(search ?? '').get('room');
  return code && code.trim() ? code.trim() : null;
}

// The share sheet, else the clipboard; the link is shown whenever the sheet did not open.
export async function shareRoom() {
  const code = tonight.lobby?.room_code;
  if (!code) return null;
  const url = shareLink(code);
  const nav = typeof navigator === 'undefined' ? null : navigator;
  try {
    if (nav?.share) {
      await nav.share({ title: 'Tonight', text: `Join the room ${code}`, url });
      return 'shared';
    }
  } catch {
    // Dismissed, or refused by the browser: fall through to the link on the screen.
  }
  tonight.shareUrl = url;
  try {
    if (nav?.clipboard?.writeText) {
      await nav.clipboard.writeText(url);
      showToast('Link copied');
      return 'copied';
    }
  } catch {
    // A clipboard the page may not write is still a link the person can read out.
  }
  return 'shown';
}

// `join` is idempotent, so an already-seated member gets their seat back.
export async function followLink(code) {
  if (!code) return null;
  const here = tonight.lobby?.room_code;
  if (here && here.toUpperCase() === code.toUpperCase()) return tonight.lobby.session_id;
  const joined = await join({ roomCode: code });
  return joined ? joined.session_id : null;
}
