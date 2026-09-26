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
// The one place a runtime is formatted ("1h 57m", "45m/ep"), which the reveal already reads.
import { runtimeLabel } from '$lib/rate.svelte.js';

/** §6.2 step 1's controls. The slider's bounds and default are proposal 57's, because §6.2
 * gives none and a slider needs them. */
export const BUDGET_MIN = 60;
export const BUDGET_MAX = 200;
export const BUDGET_STEP = 5;
export const BUDGET_DEFAULT = 130;

/** §6.2 step 1: the budget is soft — "the pool admits up to budget + 40 min" — and the slider now
 * says so where it is set. The spec's number, mirrored from `tonight/pool.py`'s
 * `BUDGET_GRACE_MIN` the way the bounds above mirror the route's. */
export const BUDGET_GRACE_MIN = 40;

/**
 * The line under the slider, which is what makes the soft budget legible before the evening
 * rather than after it. The second household evening set 120 and was offered Wicked "runs 40 min
 * over": the pool honoured §6.2 step 1 exactly, and nothing on the screen where 120 was chosen had
 * said that a longer film can still come up. On a series night the bound is per episode
 * (decision 219), and the line says which number it is about.
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
 * The budget this member last used for this kind on this device, or null.
 *
 * Per kind, because the number means different things on the two nights (decision 219: minutes
 * per episode on a series night), so a film night's 120 must not open a series night at "120 min
 * per episode". Per member, because the account chip can switch who is holding a shared phone.
 * Browser storage and nothing more: a convenience for the person holding this phone, which may be
 * absent (a private window, cleared site data) — then the slider opens at §6.2's default.
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
 * The controls as this member last left them: the kind as it stands and that kind's remembered
 * budget, else §6.2's 130 - "for that kind on that device, else 130" (decision 506). The second
 * household evening set 120, and the next visit opened at 130 again. With nothing remembered the
 * slider does not keep what it holds: that is the other kind's number, or another member's, and a
 * film night's 120 read "120 min per episode" on a series night (review finding R3-SPEC-02).
 * @param {number|null|undefined} userId
 */
export function restoreBudget(userId) {
  tonight.controls.runtime_budget_min =
    rememberedBudget(userId, tonight.controls.kind) ?? BUDGET_DEFAULT;
}

/** The kind pill. Switching kind brings that kind's own remembered budget with it, else the
 * default, for `rememberedBudget`'s reason. @param {string} kind @param {number|null|undefined} userId */
export function chooseKind(kind, userId) {
  tonight.controls.kind = kind;
  restoreBudget(userId);
}

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
 * are not.
 *
 * It named a fourth channel until now. Decision 165 retires the TV client — results are
 * phone-only — and a caption that advertises a route which no longer answers is the surface
 * sending the household to a screen that will 404. The room code and the banner are the two
 * channels §6.2 step 2 still has, and they are the two that were ever reliable. [finding 43] */
export const JOIN_CAPTION =
  'A phone notification can go missing. The room code, the link and the open-rooms list always reach the same room.';

/** The lobby's line about how people get in, true since the link exists (decision 481). It read
 * "or send the link" when there was no link to send and nothing on the screen that could make
 * one; the QR §6.2 step 2 also names is still owed and is not promised here. */
export const SHARE_CAPTION = 'Read the code out, or share the link.';

/** Decision 479's label for a finalist the slate reserved for one person — their own pick, placed
 * because none of their own top three made the plain ranking. Not RESERVED_LABEL, which is the axis
 * counterweight's and would print "the other side of the split" over somebody's favourite. */
export function pickLabel(name) {
  return `${name}'s pick`;
}

/** Decision 480's "not tonight" chips: at most this many per member (decision 505), which the
 * server enforces too. */
export const MAX_VETOES = 3;

/** What the lobby says under the chips (decisions 504 and 505), and it has to be true of both:
 * each member holds their own three, and a title that may contain what anyone ruled out is left
 * out for everyone — "may", because the pool reads the inferred tier as well as the quoted one.
 * By the kind the room is for: on a series night the vetoes leave out series (review finding
 * UX-8), as the door's budget line already says episodes there. @param {string} kind */
export function vetoCaption(kind) {
  const title = kind === 'series' ? 'A series' : 'A film';
  return `Each of you can rule out up to three. ${title} that may contain any of them is left out for everyone tonight.`;
}

/**
 * §6.2 step 4's answers carry the mood, and this is the lobby saying so (the second household
 * evening: "there is no way to say what mood you are in"). A stored mood profile is measured at
 * 0.000 for choosing tonight (§0), and the round's tilt already learns the evening's mood from the
 * answers, so the remedy is to say how to use them rather than to add a question.
 */
export const MOOD_CAPTION =
  "In a particular mood? In each pair, pick the one that fits it, and tap “Neither pulls me tonight” when neither does. The pairs learn your mood as you answer.";

/** 54d's reserved finalist, "labelled as such": the card carrying the other pole of the
 * contested axis, so a household told "here's one of each" can see which one is the other each.
 * The words live here and the fact lives on the payload's `reserved` flag — §6.2 step 5 fixes
 * the split's own headline verbatim and the server holds that one. [decision 220] */
export const RESERVED_LABEL = 'the other side of the split';

/** How far down the ranking 54f's walk can ask to go, which is `SoloBody.offset`'s own bound
 * (`api/tonight.py`: `ge=0, le=64`). Named here because the client cannot discover it and a
 * gesture that can only be refused is not a gesture — every press past it was a 422, and the
 * counter kept it. Whether the walk should reach further than 64 * 3 ranks is a question about
 * the route's bound and is not decided here. Module-private: it is this module's agreement with
 * one route, not a number any surface should be able to set. [M4.12 review cycle 1: M412-SOLO-06] */
const SOLO_OFFSET_MAX = 64;

/** 54f's reshuffle "walks further down the ranking", and the walk wraps. `wrapped` has been on
 * the payload since M4 with no reader anywhere; rendering it is what makes a fourth press that
 * returns the same three titles legible instead of broken. [decision 222] */
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
  /** @type {number|null} the seat this device is acting FOR — its own, or a guest's for the
   * length of their turn on the initiator's phone (§6.2 step 2). Every re-read goes through it
   * and `me` is only the fallback, because a device that re-read `me` replaced a guest's round
   * with the host's on every household frame. [finding 14] */
  activeSeat: null,
  /** @type {number[]} the seats this device has already cast a ballot for — its own, and its
   * guests' (§6.2 step 2 seats them on this phone). [finding 13] */
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
  /** @type {string} the room's join link, shown in the lobby once Share had to fall back to the
   * clipboard or to nothing — a link the person cannot see is a link they cannot read out */
  shareUrl: ''
});

function fail(err) {
  tonight.error =
    err instanceof ApiError ? err.detail?.message || err.message : 'something went wrong';
}

/**
 * When the pair now on screen arrived, for §4.2's `latency_ms`.
 *
 * Module-level rather than a local, and that IS the repair: `answer()` took
 * `performance.now()` into a local and then read the elapsed time inside the object literal one
 * statement later, so every Tonight row ever written holds 0. `session_answer` is append-only,
 * so the evenings already played cannot be re-measured — and §14 risk 6 wants the round
 * instrumented before anyone re-tunes it, while a column that is constant reads as a
 * measurement rather than as a bug. Same shape as `rate.svelte.js:106` on purpose: one §4.2
 * column of the same name must not mean two things on two surfaces. [finding 41]
 */
let shownAt = 0;

/** Milliseconds the pair was on screen before the tap. Null and never 0 when nothing has been
 * shown yet: the column is nullable, and "not measured" is not "answered instantly". */
function latency() {
  return shownAt ? Math.max(0, Date.now() - shownAt) : null;
}

/**
 * The pair is not in front of anybody any more.
 *
 * `leave()` says this for the door, and the door is not the only way off the screen: this module
 * outlives the page, the nav rail renders over a live round (/tonight is not in the layout's
 * `bare` list), and §6.2 step 4 keeps the phone in the person's hand for twenty pairs — so a tap
 * on Rank and back is an ordinary event mid-round. Without a stop there the clock ran through the
 * whole absence and the next answer carried it: two minutes on another screen and a one-second
 * read wrote 121000 into a column §4.2 makes append-only and §14 risk 6 wants to re-tune the
 * round from. Worse than the constant 0 this milestone replaced, because it reads as data.
 *
 * Stopping is the whole of it — `loadRound` re-arms on the way back in, so what the answer after
 * a remount reports is the time the person spent with the pair on THIS visit, which is what
 * `rate.svelte.js` reports for its own card. One §4.2 column of the same name must not mean two
 * things on two surfaces. [finding 41; M4.12 review cycle 1: M412-FE-1]
 */
export function stopClock() {
  shownAt = 0;
}

/**
 * What the last open-rooms read put in the error slot, so a later one can take back its own
 * complaint and nobody else's.
 *
 * This is the one read on the surface that nobody asks for: `connect`'s `onopen` fires it, and so
 * does every `rooms.changed` frame — §6.2 step 2 makes a household frame a nudge to re-read — so
 * it lands in the middle of whatever the person is reading. Clearing the slot on success wiped
 * sentences it had not written: §6.8's one-line why for a refused guests box stood for forty
 * milliseconds on the phone and then went, because WebKit finishes the channel handshake AFTER
 * the tap where Chromium finishes it before — and on a settled device any other member opening a
 * room does the same thing at any moment. A read that worked is not evidence that the refusal
 * before it has stopped being true. [finding 18's re-read on open; §6.8]
 */
let roomsComplaint = '';

/** §6.2 step 2's open-rooms list. */
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

/**
 * Come back to whatever this device was already part of.
 *
 * §6.2 step 4 puts each participant on their own device for a round that runs to twenty pairs,
 * so a reload, a backgrounded phone or an ordinary navigation away and back are all ordinary
 * events — and none of them may cost somebody their evening. Without this the surface reopened
 * on the door with no way back in: the open-rooms row for a room you are seated in is not a
 * join control, and 54e's reveal waits for every seat, so one reload deadlocked the household.
 * The review found it.
 */
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

/**
 * The seat this device should re-read, decided from the room it has just read.
 *
 * `activeSeat` is the seat this phone is acting for, which on the initiator's phone is a
 * guest's for the length of their turn (§6.2 step 2: "Guests use the initiator's phone"). Every
 * re-read named `seen.me.participant_id` before this existed — and a re-read happens on every
 * `rooms.changed` frame, every lobby and reveal frame and every socket reconnect, which a
 * screen lock is enough to cause — so any of them replaced the guest's round with the host's
 * mid-turn. It was not even a race: 54c's escape ended with one of those reads. [finding 14]
 *
 * `done` is what "this turn is over" means on the screen asking: an ended round while voting, a
 * submitted ballot on 54e's. Falling back then is the hand-BACK — the phone belongs to its
 * owner again, and the next guest is passed it from a screen only the owner's seat draws — and
 * falling back when `activeSeat` names no seat of this room covers the reload, the `leave` and
 * the device that has never loaded a round at all.
 *
 * @param {any} seen @param {(seat: any) => boolean} done
 */
function seatToRead(seen, done) {
  const held = (seen.seats ?? []).find((s) => s.participant_id === tonight.activeSeat);
  if (held && !done(held)) return held.participant_id;
  return seen.me?.participant_id ?? null;
}

/**
 * The guest seats this device still owes a ballot for.
 *
 * §6.2 step 2 seats guests on the initiator's phone, and `ballot.submitted_count` counts every
 * seated participant, guests included, so the reveal waits for a vote that a Submit bound to
 * the viewer's own seat could never cast: a room opened with any guest could not reach 54e's
 * reveal at all, ever. [finding 13]
 *
 * Host-only, asked of the lobby's own two fields rather than of the session store, because it
 * is a question about the room and not about the browser: the guest seats belong to whoever
 * opened it, and a second member's device offering "pass to Guest 1" would offer a vote that
 * phone is not holding.
 *
 * Which seats are done is this device's own record and not the room's — the payload carries a
 * count, not a per-seat flag, and adding one is a backend change this does not need. A reload
 * mid-ballot therefore forgets and offers the turn again; `ballot.submit` replaces rather than
 * doubles, so the cost is one repeated question and never a miscounted room.
 */
export function ballotTurns() {
  const lobby = tonight.lobby;
  const host = lobby?.host?.user_id ?? null;
  if (host === null || lobby?.me?.user_id !== host) return [];
  return (lobby.seats ?? []).filter(
    (s) => s.role === 'guest' && !tonight.submittedSeats.includes(s.participant_id)
  );
}

/**
 * True once this device has voted for every seat it votes on — its own AND its guests'.
 *
 * Both halves, because either alone leaves a vote nobody can cast. Without the guest half a
 * progress frame re-opened 54e's ballot on a phone that had already submitted, where a second
 * Submit REPLACES the ballot it had cast rather than adding to it. Without the owner's half a
 * phone whose guest happened to vote first — 54c's escape can leave the guest as the active seat
 * when the room settles — would go to the waiting screen with its owner's vote still outstanding.
 */
function ballotDone() {
  const mine = tonight.lobby?.me?.participant_id ?? null;
  if (mine === null || !tonight.submittedSeats.includes(mine)) return false;
  return ballotTurns().length === 0;
}

/**
 * Overlapping reads land out of order, and this is the busiest read in the app: three frame
 * kinds end in it (§6.2 step 2 makes every frame a nudge to re-read), so does every reconnect,
 * and so do `answer()`, `escape()` and `start()`. Without a sequence number a slow earlier
 * response writes the state it saw over a newer one's — the ballot flips back into the round on
 * one device, that device then never submits, and 54e's reveal waits on a vote nobody can cast.
 * `rank.svelte.js` and `routes/+page.svelte` carry the same guard with the same argument; this
 * one is separate from `roundSeq` below because the two reads have different triggers and an
 * invalidation shared between them would discard answers that are still the newest. [finding 21]
 */
let refreshSeq = 0;

/**
 * The lobby, the progress and the ballot state, in one read.
 *
 * `seat` names the seat this particular read is about, for the one caller that knows: 54c's
 * escape ends the round of whoever tapped it, and the screen has to say so. Every other caller
 * leaves it out and the seat is `activeSeat`, falling back to this device's own.
 */
export async function refresh({ seat = null } = {}) {
  if (!tonight.lobby) return;
  const mine = ++refreshSeq;
  try {
    const seen = await get(`/tonight/sessions/${tonight.lobby.session_id}`);
    // Decision 169: the host ended the evening, here or on another device. There is nothing
    // left to render — the room keeps its rows for §14 risk 6 and leaves §6.2 step 2's list —
    // so the honest move is out to the door carrying the reason, rather than a lobby that
    // answers nothing and a Start that 404s. [finding 7's client half]
    if (seen.state === 'abandoned') {
      leave();
      await loadRooms();
      tonight.error = 'this evening has ended';
      return;
    }
    // The sequence check sits AFTER the abandoned branch on purpose. Decision 169's door is
    // terminal — the server does not un-abandon a session, so this answer cannot be made wrong
    // by a newer one — and the read that superseded this one may yet fail, in which case
    // discarding this `leave()` would strand the device inside a room that has ended.
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
      // The branch that was missing. `bootstrap` finds the room, sets the lobby and lands here,
      // and with no `open` case the step stayed `'door'` — so a reload while a room was open but
      // not yet started put the device back at the two doors while it was holding a lobby, which
      // is the exact state the restore exists to prevent. [finding 15]
      tonight.step = 'lobby';
    }
  } catch (err) {
    fail(err);
  }
}

/**
 * Step back out to the door without giving up the seat.
 *
 * The restore above keeps a reload from stranding a participant, but a household with one live
 * room then has no other door: every visit to /tonight lands back inside it. Leaving the screen
 * is not leaving the session — the seat, the answers and the ballot all live on the server, and
 * the open-rooms row for a room you are seated in is a `resume` control.
 *
 * It has to drop the room's own state, not just the step. Every channel frame ends in `refresh`,
 * which recomputes the step from the server, so a device that stepped out while still holding a
 * lobby was dragged straight back in by the next frame anybody else's device caused.
 */
export function leave() {
  tonight.lobby = null;
  tonight.round = null;
  tonight.ballot = null;
  tonight.result = null;
  tonight.progress = [];
  tonight.approved = [];
  // Both of these are about ONE room: which seat this phone was acting for and which of that
  // room's seats it has already voted for. Carrying either into the next room would hand the
  // next evening a seat id from the last one. [findings 13, 14]
  tonight.activeSeat = null;
  tonight.submittedSeats = [];
  // The link on the screen belongs to the room being left. [decision 481]
  tonight.shareUrl = '';
  // No pair is on screen any more, so the clock behind §4.2's `latency_ms` is not running. A
  // device that steps out and comes back re-enters through `loadRound`, which re-arms it — so
  // leaving this set would charge the first answer of the next round the whole of the time the
  // person spent at the door. Said once, above, because the destroy hook says it too and two
  // spellings of one rule are two rules. [finding 41]
  stopClock();
  tonight.step = 'door';
  tonight.error = '';
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

/**
 * Its own counter, for the same reason `refresh` has one: this function is the tail of `refresh`
 * AND the retry `answer` makes on a 409, so two reads of a round can be in flight at once — and
 * the older one landing last puts a stale `card_token` on screen, which the next answer posts
 * and the route refuses. Separate from `refreshSeq` because a superseded `refresh` returns before
 * it ever reaches here, so sharing one number would only let the two invalidate each other for
 * free. [finding 21]
 */
let roundSeq = 0;

export async function loadRound(participantId) {
  const mine = ++roundSeq;
  try {
    const seen = await get(`/tonight/seats/${participantId}/round`);
    if (mine !== roundSeq) return;        // a newer read has already answered
    // §4.2's clock is armed BY A NEW CARD, not by a re-read of the one already on screen.
    //
    // This function is not only how a pair arrives: it is the tail of `refresh`, and `refresh`
    // is what every channel frame ends in (§6.2 step 2). So an unconditional re-arm measured the
    // time since the last thing anyone in the household did, and the pair the person is actually
    // reading is the one quantity it could not report. It is not a rare interleaving — `start`
    // sends a `lobby` frame AND a `rooms.changed` frame to the host's own device, so the very
    // first pair of every round was re-armed a few hundred milliseconds in: measured on the
    // M4.12 gate, a card held for 400 ms was written as 176. Every later frame — another room
    // opening or ending, the settle, a socket that reconnects when a phone unlocks — does it
    // again, to whichever pair is on screen at the time.
    //
    // The token is the identity of the card: `api/tonight.py:_seal` signs (seat, pair, seq) with
    // no timestamp, so the same pair re-read is the same string and a new pair never is. Which
    // makes this the same rule `answer` and `undo` already keep, said once for the path that
    // cannot assume it. §14 risk 6 wants the round instrumented before anyone re-tunes it, and a
    // column that means "time since the other person tapped" is worse than the zero finding 41
    // replaced, because it looks like data. [finding 41]
    //
    // OR ONTO A SCREEN THAT WAS NOT SHOWING ONE, which is the other half of the same rule and the
    // half the token alone cannot state: a page destroyed and mounted again re-reads the card it
    // left behind, and that card is new to the screen even though it is not new to the round.
    // `stopClock` is what the destroy hook calls, so an unarmed clock here means the pair arrived
    // rather than stayed. [M4.12 review cycle 1: M412-FE-1]
    if (seen.card_token !== tonight.round?.card_token || !shownAt) shownAt = Date.now();
    tonight.round = seen;
    // The seat this device is now acting for. Recorded HERE and not at the three call sites,
    // because the hand-off control, the restore and every re-read all land in this function and
    // a device that recorded it in two places out of three would drift apart from itself in the
    // one case — a guest's turn — that the record exists for. [finding 14]
    tonight.activeSeat = participantId;
    tonight.step = seen.pair ? 'round' : 'waiting';
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
  try {
    const next = await post(`/tonight/seats/${state.participant_id}/answer`, {
      card_token: state.card_token,
      answer: value,
      latency_ms: latency()
    });
    tonight.round = next;
    // The next pair is on screen from here, so the clock for it starts here. The answer path
    // carries its own card, so the measurement has to be re-armed on it and not only in
    // `loadRound` — most pairs of a twenty-pair round arrive this way. [finding 41]
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

/** §6's preamble: "undo everywhere". Your own last answer, while your own round runs. */
export async function undo() {
  const state = tonight.round;
  if (!state || tonight.busy) return;
  tonight.busy = true;
  try {
    tonight.round = await post(`/tonight/seats/${state.participant_id}/undo`, {});
    // An undo re-opens the seq and re-issues the same card, so the pair the person is looking at
    // is new to the screen even though it is not new to the round: the time they spend on it
    // after the undo is the latency of the answer they are about to give. [finding 41]
    shownAt = Date.now();
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
  // Held before the write, because the write ENDS this seat and `refresh`'s fallback hands an
  // ended seat's phone back to its owner. That is right from the next frame onwards and wrong
  // for this one: "just pick for us" ends the round of whoever tapped it, and on a guest's turn
  // the host's seat is not it — the old code re-read `me` here and landed a guest on the host's
  // round deterministically, which is how the review found the whole defect. [finding 14]
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

/** 54e: an approval ballot, not a ranking — tick everything you would be happy with. */
export function toggleApproval(titleId) {
  tonight.approved = tonight.approved.includes(titleId)
    ? tonight.approved.filter((t) => t !== titleId)
    : [...tonight.approved, titleId];
}

/**
 * 54e's hand-off: the phone changes hands between two ballots.
 *
 * `approved` is cleared on every hand-off, which is the half that is easy to leave out and
 * expensive to get wrong: the incoming guest would otherwise open on the previous person's
 * ticks, and 54e's blindness would be broken by the control that exists to carry it. The round's
 * hand-off gets this for free because `loadRound` replaces the whole round payload; the ballot's
 * selections are held here, so this has to say it.
 */
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
    // Recorded before the step is decided, because what is left to do is the question that
    // decides it: this phone is not finished when its owner has voted if a guest still has to
    // vote on it, and the ballot screen is the only place that hand-off can be offered.
    // `approved` is dropped for the same reason it is dropped on a hand-off. [finding 13]
    tonight.submittedSeats = [...tonight.submittedSeats, participantId];
    tonight.approved = [];
    // Back to this phone's OWNER if their own vote is still outstanding, which it is whenever a
    // guest voted first. Deliberately asymmetric: it never advances to a guest, because that
    // would put a guest's blind ballot on screen while the previous person is still holding the
    // phone — the hand-off has to be a tap by someone who has actually passed it on.
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

/**
 * Decision 169: the host ends the evening.
 *
 * The one control a room that has stopped progressing needs. `STATE_ABANDONED` has a single
 * writer in the whole backend and no worker job touches `session` at all, so a room that reaches
 * `voting` and loses a seat is otherwise live for ever — on §6.2 step 2's list on every
 * household device, and holding its code against the live-code unique index. The rows stay: §14
 * risk 6 reads exactly the evenings a household cut short, so this is an ending and never a
 * delete.
 *
 * `refresh` is what turns the write into a screen — the room now reads `abandoned` and that
 * branch takes this device to the door with the reason. A second tap is a 404 and lands in
 * `fail`, which is the truth and is therefore not special-cased.
 */
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

/**
 * 54f: solo lands **directly** on the picks. No round first — "the fastest path to a film must
 * not be slower than browsing Home".
 */
export async function loadSolo({ sharpen = false, reshuffle = false } = {}) {
  tonight.busy = true;
  // Where the walk actually is, so a press that never landed can be taken back below: the counter
  // was raised before the POST and nothing ever lowered it, so one refused request moved the walk
  // by a step the person never saw and every later press asked for the one after that.
  // [M4.12 review cycle 1: M412-SOLO-06]
  const walked = tonight.soloOffset;
  try {
    // AND NEVER PAST WHAT THE ROUTE WILL SERVE. `SoloBody.offset` is `ge=0, le=64`
    // (`api/tonight.py`), so press 65 was a 422 the person reads as a broken control — and since
    // the number that caused it had already been stored, so was every press after it: the walk
    // was dead until Back, which drops the evening's sharpen answers with it. The client cannot
    // discover the bound, so it names it and cites the field that owns it rather than sending a
    // request that can only be refused. [M4.12 review cycle 1: M412-SOLO-06]
    if (reshuffle) tonight.soloOffset = Math.min(tonight.soloOffset + 1, SOLO_OFFSET_MAX);
    if (!sharpen && !reshuffle) {
      tonight.soloAnswers = [];
      tonight.soloOffset = 0;
    }
    tonight.solo = await post('/tonight/solo', {
      ...tonight.controls,
      offset: tonight.soloOffset,
      answers: tonight.soloAnswers,
      // 54f: solo lands "**directly** on three picks and a wildcard", and the pair search is
      // what "sharpen this" asks for and what nothing else does. The request model has carried
      // the field since the round was made optional and this module never sent it, so the door
      // and every Reshuffle paid for a round nobody had asked for — against the one screen 54f
      // says "must not be slower than browsing Home".
      sharpen
    });
    tonight.step = 'solo';
    tonight.error = '';
  } catch (err) {
    fail(err);
    // A refused walk has not walked. 54f's Reshuffle "walks further down the ranking", and what
    // it has walked is what came back — the picks on the screen are still the ones this offset
    // drew. [M4.12 review cycle 1: M412-SOLO-06]
    tonight.soloOffset = walked;
  } finally {
    tonight.busy = false;
  }
}

/**
 * 54f's sharpen round. The answers live here because §6.2 step 8 mints no session row.
 *
 * `value` is the person's, and the surface shows them the pair before asking: an earlier
 * version had one button that posted `A` without drawing either title, so every tap recorded a
 * preference nobody expressed and then re-ranked their picks by it. §6.2 step 4's question is
 * "Which one tonight?" — it has to be asked.
 */
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

/** The reconnect's first wait and its ceiling. A fixed 1.5 s retry with no cap and no jitter
 * opened 40 sockets and fired 117 HTTP reads in 60 simulated seconds of downtime — and §10's
 * swap sequence ends in exactly the backend restart that hammers, so the storm arrives at the
 * moment the server is least able to take it. [finding 18] */
export const RECONNECT_MIN_MS = 1000;
export const RECONNECT_MAX_MS = 30000;
/** Plus or minus a fifth, so a household's phones do not all come back in the same instant.
 * Narrower than the doubling deliberately: a jitter wide enough to reorder two consecutive
 * waits would make "backoff" a claim nothing could check. */
const RECONNECT_JITTER = 0.2;

/** The wait before attempt `attempt` (0-based). Exported because it is the whole rule, and a
 * rule that can only be observed by running a socket for a minute is a rule nothing tests. */
export function reconnectDelay(attempt, random = Math.random) {
  const base = Math.min(RECONNECT_MIN_MS * 2 ** attempt, RECONNECT_MAX_MS);
  return Math.round(base * (1 - RECONNECT_JITTER + random() * 2 * RECONNECT_JITTER));
}

/**
 * The session channel (§6.2 step 2, §1). Every frame is a nudge to re-read rather than a
 * payload, so a dropped frame costs a stale lobby and never a wrong one — and no answer, vote
 * or result ever travels over it.
 */
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
    // The re-read belongs to a connection that actually OPENED, not to a retry that was merely
    // scheduled. A frame missed while the socket was down is a frame nobody re-sends, so the
    // re-read has to happen — but firing it from the timer beside `open()` charged two HTTP
    // reads to every FAILED attempt too, which is the half of the storm the backoff below does
    // not fix. Resetting the count here rather than in `onclose` is what makes the next outage
    // start at one second again instead of at the ceiling. [finding 18]
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
        // A household frame also means a room's seat list moved. A device already sitting in a
        // lobby has to re-read it, because the session-scoped frame that would have told it can
        // be missed: `onMount`'s connect and a tap on "Together" race, and whichever loses
        // leaves this device in the household group and not the room's. Re-reading on the frame
        // it did get is what makes the lobby live either way, and it is still the WebSocket
        // doing it — no poll.
        if (tonight.lobby) await refresh();
      } else if (frame.kind === 'progress') tonight.progress = frame.participants;
      else if (frame.kind === 'ballot') applyBallotCount(frame);
      else if (frame.kind === 'lobby' || frame.kind === 'reveal') await refresh();
    };
    // A phone that locks, a laptop that sleeps and a proxy that times out all close the socket
    // without telling anyone. Without a retry the device stays connected in name only, and the
    // lobby it is looking at quietly stops being live — the failure §6's preamble makes the
    // in-app channel the guaranteed answer to. So the retry stays, and stays a retry: polling
    // would cost the household a request per device per interval for ever, where a socket that
    // is up costs nothing. What changes is the cadence — see `reconnectDelay`. [finding 18]
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
    // 54h: on a series night §6.2 step 1's budget is a bound on minutes PER EPISODE, and this
    // row is the one place step 2 prints it. Unqualified, "Series · 130 min" read as the length
    // of the thing being chosen, which is the one thing it is not. [decision 219]
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

/** 54c's waiting line: "Patrick 6/6 ✓ · Jenny 9/~10 · waiting for 2". Counts only. Once a seat is
 * past the typical round the server sends no estimate (decision 507), and the line gives the count
 * alone: "Jenny 12/~13" moved with every tap and promised an end one pair away. */
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

/**
 * 54e's submitted count, from the ballot frame. The ballot screen prints "1 of 2 submitted", and
 * the only frame a submit pushed was the ROUND's progress, which lands in `tonight.progress` — so
 * the other phone read "0 of 2" until the reveal (the first household evening: still 0 of 2
 * eighteen seconds after the vote was in). Two integers; the frame carries nothing else.
 */
export function applyBallotCount(frame) {
  if (!tonight.ballot) return;
  tonight.ballot = { ...tonight.ballot, submitted: frame.submitted, seated: frame.seated };
}

/**
 * The round's header: which pair this is and what to expect, never the cap as the plan.
 *
 * AN ESTIMATE THAT STAYS TRUE (decision 507). It read "usually about 10" on every pair, so the
 * second household evening's thirteenth pair still promised ten. Up to the typical round it says
 * how rounds often go; past it, that this one is running long, with the cap that does end it —
 * which is the useful number from there, and "just pick for us" is on the screen beside it.
 *
 * ONE LINE AT 390 PX. The line is 12 px mono at 0.14em, about 8.9 px a character, so 353 px of
 * phone holds 39. "pair 11 · a longer round than most · at most 20" is 47: it wrapped, and the
 * 16 px it added pushed Undo and "just pick for us" under the bottom bar on any pair with an
 * over-budget card (review finding UX-4). The page also keeps the line to one.
 */
export function roundHeader(round) {
  if (!round) return '';
  const n = (round.answered ?? 0) + 1;
  const typical = round.typical ?? 10;
  if (n <= typical) return `pair ${n} · often about ${typical}`;
  return [`pair ${n}`, 'longer than most', round.cap ? `max ${round.cap}` : null]
    .filter(Boolean)
    .join(' · ');
}

/** How many characters of genres fit one line of a pair card, which is half a phone wide: two
 * genres when they fit ("Animation, Family"), else the first alone ("Adventure, Science Fiction"
 * wrapped the card at 390 px and pushed the answers down). */
const PAIR_GENRE_CHARS = 22;

/**
 * What a pair card says about a title under its name, for somebody who does not know it (the
 * second household evening: "Warriors of the Wind" and "Perfect Days" said "1984 · fits your 120
 * min" and nothing else). The year and the runtime; "runs N min over" when it does, on its own
 * line (§6.2 step 1's label); and the title's genres in plain words. One short line each, so both
 * options and every answer still fit an iPhone 13's screen with the longest titles.
 */
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

/** After this phone has voted: the ballot's own status, never the round's counts. */
export function ballotWaitingLine(ballot) {
  if (!ballot) return '';
  const left = Math.max(0, (ballot.seated ?? 0) - (ballot.submitted ?? 0));
  return left ? `Your vote is in · waiting for ${left} more` : 'Your vote is in';
}

/** Submit says what it will cast, so it can never be mistaken for one more option to tick. */
export function submitLabel(count) {
  if (!count) return 'Submit — none of these';
  return `Submit ${count} ${count === 1 ? 'pick' : 'picks'}`;
}

/**
 * How broad each person's yes was, after the reveal. "Unanimous." stood here and was literally
 * true over an evening where one member approved four titles and the other one — the winner being
 * her only yes. 54e reveals the approvals together once every ballot is in, which is when this
 * payload exists at all.
 */
export function breadthLine(result) {
  const rows = result?.breadth ?? [];
  return rows.map((b) => `${b.name} said yes to ${b.approved} of ${b.of}`).join(' · ');
}

/** "the only one Jenny said yes to", for each person whose one yes became the winner. */
export function onlyYesLines(result) {
  return (result?.breadth ?? []).filter((b) => b.only_yes).map((b) => `the only one ${b.name} said yes to`);
}

/**
 * This phone's seat in the room: the lobby read's own `me` where it has one, else the seat the
 * signed-in member holds (the open and join replies carry the seats and no `me`).
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
 * The other members' chips, one line per member who has any: "Jenny: violence, horror". Each
 * member holds their own three (decision 505), so a phone shows its own as chips and names whose
 * the rest are — the second household evening's second member found the room's chips taken and no
 * way to tell they were somebody else's.
 * @param {any} lobby @param {number|null|undefined} userId
 */
export function othersVetoLines(lobby, userId) {
  const me = mySeat(lobby, userId);
  return (lobby?.seats ?? [])
    .filter((s) => s.participant_id !== me?.participant_id && (s.vetoes ?? []).length)
    .map((s) => `${s.name}: ${s.vetoes.map((v) => v.label).join(', ')}`);
}

/**
 * Decision 480's "not tonight" chips: this member's whole set, replaced, so two taps on one phone
 * cannot leave half of each — and another member's set is never touched (decision 505). Any
 * seated member, before Start; the server refuses the rest.
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

/** The room code a `?room=` link names, or null. */
export function linkedRoom(search) {
  const code = new URLSearchParams(search ?? '').get('room');
  return code && code.trim() ? code.trim() : null;
}

/**
 * Share the lobby's link: the phone's own share sheet where there is one, the clipboard where
 * there is not, and the link written on the screen either way the sheet did not open — a link the
 * person cannot see is one they cannot read out.
 */
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

/**
 * A `?room=` link, followed (decision 481). The device joins that room — `join` is idempotent, so
 * a member who is already seated gets their seat back — unless it is the room this device is
 * already showing, which needs nothing.
 */
export async function followLink(code) {
  if (!code) return null;
  const here = tonight.lobby?.room_code;
  if (here && here.toUpperCase() === code.toUpperCase()) return tonight.lobby.session_id;
  const joined = await join({ roomCode: code });
  return joined ? joined.session_id : null;
}
