/**
 * The Tonight surface's client. Spec v2.1 §6.2 (rewritten: 54a–54g), §6.7, §6.8; decision 154.
 *
 * Four rules this module encodes, each of which is a way the surface could quietly stop obeying
 * §6.2:
 *
 *   * **The card is opaque and single-use.** 54b makes `session_answer.selection` the
 *     discriminator §13's evaluation depends on, so the pair arrives sealed and this module
 *     never reads, reconstructs or invents one. `answer()` posts the token back and nothing
 *     else — the same property the Rank queue's pair has, for a sharper reason.
 *   * **Nothing here draws the pool.** §6.2 step 3: the candidate pool is "internal — never
 *     shown as a step". The server does not send it; this module does not ask for it, cache it,
 *     or derive a ranking of its own from the pairs it has seen.
 *   * **The waiting view shows counts.** 54c: "progress and never their answers". The payload
 *     cannot carry them, and this module renders what the payload has rather than remembering
 *     what it saw.
 *   * **The reveal is a moment, not a state.** 54e/proposal 60: the beat comes before the
 *     winner. The client waits for the reveal frame, then fetches — it does not poll for a
 *     result it might get early.
 *
 * The channel is a WebSocket (§6.2 step 2, §1). It carries no answer and no result: every frame
 * is a nudge to re-read, so a dropped frame costs a stale lobby and never a wrong one.
 */

import { ApiError, get, post } from '$lib/api.js';

/** §6.2 step 1's controls. The slider's bounds and default are proposal 57's, because §6.2
 * gives none and a slider needs them. */
export const BUDGET_MIN = 60;
export const BUDGET_MAX = 200;
export const BUDGET_STEP = 5;
export const BUDGET_DEFAULT = 130;

/** §6.2 step 1: "members and/or N guests", who share the initiator's phone. */
export const MAX_GUESTS = 6;

/** Decision 154's four answers, in the order the card offers them. `either` lifts both,
 * `neither` lowers both — opposite signals, not two names for a shrug. */
export const ANSWERS = [
  { value: 'A', label: 'This one' },
  { value: 'B', label: 'That one' },
  { value: 'EITHER', label: 'Either is fine' },
  { value: 'NEITHER', label: 'Neither pulls me tonight' }
];

/** 54c's control, by the name 54c gives it. */
export const ESCAPE_LABEL = 'just pick for us';

/** 54e/proposal 60: "shipping the property without the moment ships half of it." */
export const REVEAL_BEAT = 'VOTES REVEALED TOGETHER';

/** §6.2 step 2's caption. Push is best-effort (§6 preamble), so the lobby says which channels
 * are not. */
export const JOIN_CAPTION =
  'Push is best effort. The room code, the in-app banner and the TV route all reach the same session.';

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
  /** @type {any} the round state for MY seat; `pair` is null once my round has ended */
  round: null,
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
  /** @type {any[]} §6.7's rail, when decision 117's toggle is on */
  rail: []
});

function fail(err) {
  tonight.error =
    err instanceof ApiError ? err.detail?.message || err.message : 'something went wrong';
}

/** §6.2 step 2's open-rooms list. */
export async function loadRooms() {
  try {
    tonight.rooms = (await get('/tonight/rooms')).rooms;
    tonight.error = '';
  } catch (err) {
    fail(err);
  }
}

export async function bootstrap() {
  tonight.loading = true;
  try {
    await loadRooms();
  } finally {
    tonight.loading = false;
    tonight.booted = true;
  }
}

/** §6.2 step 1: the initiator opens a room with the three controls. */
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

/**
 * §6.2 step 2: "Join channels, all equivalent." One function behind the code, the open-rooms
 * tap and the banner, so "equivalent" is a fact about this module rather than a claim about
 * three call sites.
 */
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

/** The lobby, the progress and the ballot state, in one read. */
export async function refresh() {
  if (!tonight.lobby) return;
  try {
    const seen = await get(`/tonight/sessions/${tonight.lobby.session_id}`);
    tonight.lobby = seen;
    tonight.progress = seen.progress;
    tonight.ballot = seen.ballot;
    if (seen.ballot?.revealed) await loadResult();
    else if (seen.state === 'ballot') await loadBallot();
    else if (seen.state === 'voting' && seen.me) await loadRound(seen.me.participant_id);
  } catch (err) {
    fail(err);
  }
}

/** §6.2 step 2's join window closes here — "Anyone who joins before you start is in." */
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

export async function loadRound(participantId) {
  try {
    tonight.round = await get(`/tonight/seats/${participantId}/round`);
    tonight.step = tonight.round.pair ? 'round' : 'waiting';
  } catch (err) {
    fail(err);
  }
}

/**
 * One answer. Posts the sealed card back and nothing else — this module cannot name a title or
 * an arm even by mistake, which is what keeps §13's held-out stream a server fact.
 */
export async function answer(value) {
  const state = tonight.round;
  if (!state?.card_token || tonight.busy) return;
  tonight.busy = true;
  const started = performance.now();
  try {
    const next = await post(`/tonight/seats/${state.participant_id}/answer`, {
      card_token: state.card_token,
      answer: value,
      latency_ms: Math.round(performance.now() - started)
    });
    tonight.round = next;
    tonight.rail = next.rail ?? [];
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

/** §6's preamble: "undo everywhere". Your own last answer, while your own round runs. */
export async function undo() {
  const state = tonight.round;
  if (!state || tonight.busy) return;
  tonight.busy = true;
  try {
    tonight.round = await post(`/tonight/seats/${state.participant_id}/undo`, {});
    tonight.step = tonight.round.pair ? 'round' : 'waiting';
    tonight.error = '';
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

/** 54c's escape, available from pair 6 — and the availability comes from the server, so the
 * client is not a second implementation of the rule. */
export async function escape() {
  const state = tonight.round;
  if (!state?.escape_available || tonight.busy) return;
  tonight.busy = true;
  try {
    tonight.round = await post(`/tonight/seats/${state.participant_id}/escape`, {});
    tonight.step = 'waiting';
    await refresh();
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

/** 54e: an approval ballot, not a ranking — tick everything you would be happy with. */
export function toggleApproval(titleId) {
  tonight.approved = tonight.approved.includes(titleId)
    ? tonight.approved.filter((t) => t !== titleId)
    : [...tonight.approved, titleId];
}

export async function submitBallot(participantId) {
  if (tonight.busy) return;
  tonight.busy = true;
  try {
    const out = await post(`/tonight/seats/${participantId}/ballot`, {
      approved: tonight.approved
    });
    tonight.ballot = { ...tonight.ballot, ...out };
    tonight.step = out.revealed ? 'reveal' : 'waiting';
    if (out.revealed) await loadResult();
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

/**
 * 54f: solo lands **directly** on the picks. No round first — "the fastest path to a film must
 * not be slower than browsing Home".
 */
export async function loadSolo({ sharpen = false, reshuffle = false } = {}) {
  tonight.busy = true;
  try {
    if (reshuffle) tonight.soloOffset += 1;
    if (!sharpen && !reshuffle) {
      tonight.soloAnswers = [];
      tonight.soloOffset = 0;
    }
    tonight.solo = await post('/tonight/solo', {
      ...tonight.controls,
      offset: tonight.soloOffset,
      answers: tonight.soloAnswers
    });
    tonight.step = 'solo';
    tonight.error = '';
  } catch (err) {
    fail(err);
  } finally {
    tonight.busy = false;
  }
}

/** 54f's sharpen round. The answers live here because §6.2 step 8 mints no session row. */
export async function sharpen(value) {
  const pair = tonight.solo?.pair;
  if (!pair) return;
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

/**
 * The session channel (§6.2 step 2, §1). Every frame is a nudge to re-read rather than a
 * payload, so a dropped frame costs a stale lobby and never a wrong one — and no answer, vote
 * or result ever travels over it.
 */
export function connect(sessionId = null) {
  if (typeof WebSocket === 'undefined') return () => {};
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const suffix = sessionId ? `?session_id=${sessionId}` : '';
  const socket = new WebSocket(`${proto}//${location.host}/api/tonight/channel${suffix}`);
  socket.onmessage = async (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    if (frame.kind === 'rooms.changed') await loadRooms();
    else if (frame.kind === 'progress') tonight.progress = frame.participants;
    else if (frame.kind === 'lobby' || frame.kind === 'reveal') await refresh();
  };
  return () => socket.close();
}

/** §6.2 step 2's row: "MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen". */
export function roomLine(room) {
  const age = minutesAgo(room.started_at);
  return [
    room.room_code,
    `hosted by ${room.host}`,
    age === null ? null : `${age} min ago`,
    room.kind === 'movie' ? 'Film' : 'Series',
    `${room.runtime_budget_min} min`,
    room.skips_seen ? 'skips seen' : 'includes rewatches'
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

/** 54c's waiting line: "Patrick 6/6 ✓ · Jenny 9/~12 · waiting for 2". Counts only. */
export function progressLine(progress) {
  const parts = progress.map((p) =>
    p.finished ? `${p.name} ${p.answered}/${p.answered} done` : `${p.name} ${p.answered}/~${p.expected}`
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
