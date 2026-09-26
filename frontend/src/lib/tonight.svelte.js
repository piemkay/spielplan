// The card is a sealed single-use token this module never opens; the pool is never sent here;
// the waiting view shows counts only. Channel frames are nudges to re-read, never payloads.

import { ApiError, get, post } from '$lib/api.js';
import { runtimeLabel } from '$lib/rate.svelte.js';

// Proposal 57's bounds and default: §6.2 gives none.
export const BUDGET_MIN = 60;
export const BUDGET_MAX = 200;
export const BUDGET_STEP = 5;
export const BUDGET_DEFAULT = 130;

// Mirrors `tonight/pool.py`'s `BUDGET_GRACE_MIN` (§6.2 step 1).
export const BUDGET_GRACE_MIN = 40;

/**
 * Makes the soft budget legible before the evening; on a series night the bound is per episode.
 * @param {string} kind
 */
export function budgetSoftLine(kind) {
  return kind === 'series'
    ? `episodes up to ${BUDGET_GRACE_MIN} min longer can still come up, marked with how far over`
    : `films up to ${BUDGET_GRACE_MIN} min longer can still come up, marked with how far over`;
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
  { value: 'NEITHER', label: 'Neither pulls me tonight' }
];

export const ESCAPE_LABEL = 'just pick for us';

/** 54e/proposal 60: "shipping the property without the moment ships half of it." */
export const REVEAL_BEAT = 'VOTES REVEALED TOGETHER';

// Push can go missing (§6 preamble), so the caption names the channels that cannot.
export const JOIN_CAPTION =
  'A phone notification can go missing. The room code, the link and the open-rooms list always reach the same room.';

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
  "In a particular mood? In each pair, pick the one that fits it, and tap “Neither pulls me tonight” when neither does. The pairs learn your mood as you answer.";

// 54d's reserved finalist; the fact itself is the payload's `reserved` flag (decision 220).
export const RESERVED_LABEL = 'the other side of the split';

// `SoloBody.offset`'s bound (`le=64` in `api/tonight.py`), which the client cannot discover.
const SOLO_OFFSET_MAX = 64;

// The reshuffle walk wraps; say so, or a repeat of the same three looks broken.
export const WRAPPED_LINE = 'back round to the top of the ranking';

export const tonight = $state({
  loading: true,
  booted: false,
  busy: false,
  error: '',
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
  /** @type {any} 54f's solo picks */
  solo: null,
  /** @type {any[]} 54f's sharpen round, carried by the client because §6.2 step 8 mints no
   * session row and therefore no `session_answer` to hold them */
  soloAnswers: [],
  soloOffset: 0,
  /** @type {string} the join link, shown once Share fell back to the clipboard or nothing */
  shareUrl: ''
});

function fail(err) {
  tonight.error =
    err instanceof ApiError ? err.detail?.message || err.message : 'something went wrong';
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
      tonight.error = 'this evening has ended';
      return;
    }
    // After the abandoned branch: an ended room is never stale, and the newer read may fail.
    if (mine !== refreshSeq) return;      // a newer read has already answered
    tonight.lobby = seen;
    tonight.progress = seen.progress;
    tonight.ballot = seen.ballot;
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

// Step out to the door keeping the seat (it lives on the server). The room's state goes too, or
// the next frame's `refresh` would drag the device back in.
export function leave() {
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
    tonight.ballot = await get(`/tonight/sessions/${tonight.lobby.session_id}/ballot`);
    tonight.step = tonight.ballot.revealed ? 'reveal' : 'ballot';
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
    tonight.result = await get(`/tonight/sessions/${tonight.lobby.session_id}/result`);
    tonight.step = 'reveal';
    tonight.error = '';
  } catch (err) {
    // 409 here is the blind rule holding, not a failure: somebody has not submitted yet.
    if (!(err instanceof ApiError && err.status === 409)) fail(err);
  }
}

// Solo lands directly on the picks, no round first (54f).
export async function loadSolo({ sharpen = false, reshuffle = false } = {}) {
  tonight.busy = true;
  // Where the walk is, so a refused press can be taken back below.
  const walked = tonight.soloOffset;
  try {
    // Never past what the route serves (`SOLO_OFFSET_MAX`).
    if (reshuffle) tonight.soloOffset = Math.min(tonight.soloOffset + 1, SOLO_OFFSET_MAX);
    if (!sharpen && !reshuffle) {
      tonight.soloAnswers = [];
      tonight.soloOffset = 0;
    }
    tonight.solo = await post('/tonight/solo', {
      ...tonight.controls,
      offset: tonight.soloOffset,
      answers: tonight.soloAnswers,
      // Only "sharpen this" asks for a pair search (54f).
      sharpen
    });
    tonight.step = 'solo';
    tonight.error = '';
  } catch (err) {
    fail(err);
    // A refused walk has not walked.
    tonight.soloOffset = walked;
  } finally {
    tonight.busy = false;
  }
}

// The answers live here because §6.2 step 8 mints no session row; the pair is shown before asking.
export async function sharpen(value) {
  const pair = tonight.solo?.pair;
  if (!pair || !ANSWERS.some((a) => a.value === value)) return;
  tonight.soloAnswers = [
    ...tonight.soloAnswers,
    {
      seq: tonight.soloAnswers.length + 1,
      title_a: pair.a.title_id,
      title_b: pair.b.title_id,
      answer: value
    }
  ];
  await loadSolo({ sharpen: true });
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

/** §6.2 step 2's row: "MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen". */
export function roomLine(room) {
  const age = minutesAgo(room.started_at);
  return [
    room.room_code,
    `hosted by ${room.host}`,
    age === null ? null : `${age} min ago`,
    room.kind === 'movie' ? 'Film' : 'Series',
    // On a series night the budget bounds minutes per episode (decision 219).
    room.kind === 'series'
      ? `${room.runtime_budget_min} min per episode`
      : `${room.runtime_budget_min} min`,
    room.skips_seen ? 'skips seen' : 'includes rewatches',
    // Decision 480: what the room has ruled out, so somebody deciding whether to join knows.
    room.vetoes?.length ? `not tonight: ${room.vetoes.map((v) => v.label).join(', ')}` : null
  ]
    .filter(Boolean)
    .join(' · ');
}

export function minutesAgo(iso) {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.round((Date.now() - then) / 60000));
}

// Counts only (54c); past the typical round the server sends no estimate (decision 507).
export function progressLine(progress) {
  const parts = progress.map((p) =>
    p.finished
      ? `${p.name} ${p.answered}/${p.answered} done`
      : p.expected == null
        ? `${p.name} ${p.answered} so far`
        : `${p.name} ${p.answered}/~${p.expected}`
  );
  const waiting = progress.filter((p) => !p.finished).length;
  return waiting ? `${parts.join(' · ')} · waiting for ${waiting}` : parts.join(' · ');
}

/** §6.8's data voice: a model number never appears bare. */
export function approvalShare(result) {
  if (!result) return '';
  const approved = Math.round(result.approval_share * result.participants);
  return `${approved} of ${result.participants} approved`;
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
  if (n <= typical) return `pair ${n} · often about ${typical}`;
  return [`pair ${n}`, 'longer than most', round.cap ? `max ${round.cap}` : null]
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
  return (result?.breadth ?? []).filter((b) => b.only_yes).map((b) => `the only one ${b.name} said yes to`);
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
