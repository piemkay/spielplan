/**
 * Shared session + app-config state. Spec v2.1 §3.1, §3.2.
 *
 * `bootstrap()` answers three questions in two requests, and the answers decide the whole
 * shell: is a setup wizard owed (no admin exists), is anyone signed in, and is there an
 * artifact bundle. A bundle-less app is a legal state (§3.1) — `hasBundle: false` is a value
 * the UI renders, never an error it catches.
 *
 * A read that did not answer is a third state, and collapsing it into `false` is what signed a
 * live member out of an appliance that was only restarting: §3.1 asks for "an explicit state
 * instead of erroring", and "I could not ask" is not that state — it is the absence of one. So
 * `offline` is carried beside the answers, and `hasBundle` stays null until `/config` has
 * actually said. [M4.15 finding 15; decision 271]
 */

import { get, post, ApiError } from '$lib/api.js';

export const session = $state({
  // `loading` is only true until the FIRST bootstrap resolves. Later refreshes must not flip
  // it: the shell blanks the page while loading, and blanking it mid-flow destroys whatever
  // component asked for the refresh — which reset the setup wizard to step 1 every time the
  // bundle import finished.
  loading: true,
  booted: false,
  // Everything `/api/auth/me` returns. It listed four fields while the route had grown to ten
  // across M1 and M2, so `svelte-check` reported an error at every use of the other six — forty
  // of the repo's forty-two, which is how a type that is not maintained stops being read at all.
  /**
   * @type {null | {
   *   id: number,
   *   name: string,
   *   role: string,
   *   must_change_password: boolean,
   *   show_model?: boolean,
   *   has_pin?: boolean,
   *   passkeys?: number,
   *   jellyfin?: any,
   *   auth_method?: string,
   *   nav?: {
   *     surfaces: {key: string, href: string, label: string, milestone?: string}[],
   *     account: {key: string, href: string, label: string}[]
   *   },
   *   admin_reauth_required?: boolean
   * }}
   */
  user: null,
  // Everything past `required` and `note` is optional because it genuinely is: sec-14 cuts the
  // payload to those two for an anonymous caller, and a type that promised `has_admin` to every
  // reader is what let the wizard decide an anonymous visitor's screen from it (fe-46).
  /**
   * @type {null | {
   *   required: boolean, note: string, steps?: {step:string,done:boolean}[],
   *   has_admin?: boolean, member_count?: number, bundle?: any
   * }}
   */
  setup: null,
  // Tri-state, and the third state is the one the app was missing: null means `/config` has not
  // answered yet or did not answer at all. The header asserted "no bundle imported" from the
  // initial `false` whenever that read failed — a claim about the household's library made from
  // a request that never arrived, and the one line of the shell that is a statement of fact
  // rather than a rendering of one (decision 271).
  /** @type {boolean | null} */
  hasBundle: null,
  /** @type {any} */
  bundle: null,
  publicUrl: '',
  // Not "the browser says navigator.onLine": this is "the appliance did not answer the one
  // request that decides who is holding the phone". A LAN box that is restarting, a Tailscale
  // route that has dropped and a phone with no signal are the same fact to the shell, and §3.1
  // wants that fact rendered rather than guessed at. [M4.15 finding 15]
  offline: false
});

/**
 * Which read of `/auth/me` is the current one.
 *
 * That read decides who is holding the phone, and it is the one read in this app with TWO
 * writers: `bootstrap()` here and `refreshUser()` below. `+layout.svelte`'s `retrying` flag
 * serialises boot against boot — the retry button, the `online` event and decision 283's 5 s
 * timer — and cannot see the second writer at all, so nothing stopped an older boot from landing
 * its answer on top of a sign-in.
 *
 * The window is not small. `api.js` gives each request ten seconds and a boot spends two of them
 * in series — `/config` and `/setup/state` together, and only then `/auth/me` — so a boot armed
 * by the timer can still be in flight twenty seconds later, and because each boot can outlast the
 * 5 s interval one is in flight almost continuously while the unreachable card stands. That card
 * is exactly where the journey decision 283 exists for ends: `bare` wins over it in the shell, so
 * /login renders its form, the member signs in the moment the box comes back, and `land()` calls
 * `setUser()` then `refreshUser()` then `goto('/')`. The doomed boot's `/auth/me` then died into
 * `session.offline = true` — the unreachable card over a shell on a network that was back, which
 * also UNMOUNTS the surface underneath it, and `tonight/+page.svelte`'s `onDestroy` closes the
 * live room's socket and stops §4.2's round clock on the way out. On the 401 variant, where the
 * timer's request left before the sign-in cookie existed, it wrote `session.user = null` and
 * `guard()` sent the person who had just signed in back to /login.
 *
 * The house convention rather than an abstraction: a module-local counter and three lines at each
 * of the two writers, the shape `rank.svelte.js`'s `requestSeq` and `+page.svelte`'s `facetSeq`
 * already carry. It covers the `/auth/me` writes and nothing else, deliberately — `/config` and
 * `/setup/state` have one writer between them, and `refreshUser()` does not re-read either, so
 * extending the counter over those lines would discard a fresh answer nobody else supplies.
 * [decision 283; M4.15 finding 15; §3.1; review cycle 3: M415-C3-SESS-01]
 */
let identitySeq = 0;

export async function bootstrap() {
  if (!session.booted) session.loading = true;
  try {
    const [config, setup] = await Promise.all([
      get('/config').catch(() => null),
      get('/setup/state').catch(() => null)
    ]);
    if (config) {
      session.hasBundle = config.has_bundle;
      session.bundle = config.bundle;
      session.publicUrl = config.public_url;
    }
    // The sibling read, with decision 271's rule applied to it. `/config`'s three fields are
    // assigned only inside `if (config)` above precisely so a swallowed failure cannot overwrite
    // a fact this device already holds; this line wrote the swallowed `null` straight through,
    // so a SECOND boot destroyed a `/setup/state` answer the document had already received. With
    // `/auth/me` then answering 401 — the hourly prune, mid-session, while the first pair of
    // reads blipped — `landingUnknown` rendered decision 286's card, whose sentence says that
    // answer "did not arrive" to a device that had it. The destination was knowable and the
    // shell said it was not.
    //
    // Only a `required: false` is kept, and the asymmetry is the spec's rather than a
    // convenience: `api/setup.py`'s `state()` defines `required` as "no admin exists", and
    // `api/admin.py`'s `_refuse_if_last_active_admin` refuses to demote, disable or delete the
    // last one — so once this device has been told false, false it stays, on any box and any
    // session. A `required: true` is the transient half: an admin created on ANOTHER device
    // makes it stale, and `setup/+page.svelte` reads it as `hasAdmin` with fe-46's default
    // ("a payload not read yet counts as an admin existing, because the failure worth defaulting
    // against is showing the form to someone who must not see it"). So it is dropped rather than
    // preserved, which keeps that default and keeps the wizard's own `if (!session.setup)`
    // re-read on the one path where the payload can go stale.
    // [decision 271; decision 286; §3.1; review cycle 2: M415-C2-SESS-01]
    const held = session.setup;
    session.setup = setup ?? (held?.required === false ? held : null);

    // The rethrow this replaces ran AFTER the `finally` below had set `booted`, so the shell's
    // own `onMount` guard never ran, the promise rejected unhandled, and the already-armed
    // `$effect` read `user === null` and sent a signed-in member to a login form whose POST
    // could not leave the device — with the session cookie untouched the whole time. A failed
    // read is not a sign-out: the 401 branch is, and it is the only branch that clears anybody.
    // [M4.15 finding 15, fe-07; §3.1]
    //
    // And taken under the counter above: an answer this boot is no longer the current reader of
    // is not news, whichever way it comes back. `return` rather than a skipped assignment,
    // because nothing follows it here and the `finally` below still ends `loading` and sets
    // `booted` — this boot DID complete, it just lost the right to say who is signed in.
    const seq = ++identitySeq;
    try {
      const me = await get('/auth/me');
      if (seq !== identitySeq) return;
      session.user = me;
      session.offline = false;
    } catch (err) {
      if (seq !== identitySeq) return;
      if (err instanceof ApiError && err.isUnauthenticated) {
        session.user = null;
        // The appliance answered; it said this session is over. That is a fact, not a gap, so
        // it also ends any offline state a previous attempt left standing — otherwise a retry
        // that reaches a restarted server with a pruned session would sit on the unreachable
        // card for ever instead of going to /login.
        session.offline = false;
      } else {
        session.offline = true;
      }
    }
  } finally {
    session.loading = false;
    session.booted = true;
  }
}

export function setUser(user) {
  session.user = user;
}

/**
 * Re-read the signed-in user from the server.
 *
 * Sign-in and profile-switch responses carry identity only; `/auth/me` carries the navigation
 * payload the shell renders from (§6.6: admin entries are a server decision, so the client has
 * no list of its own to fall back on). Anything that changes *who* is signed in calls this.
 *
 * Both answering branches end `offline`, for the same reason `bootstrap()`'s two do and with the
 * same force: a read that got through is proof the appliance answered, whichever function made
 * it. Without it a sign-in that succeeds while an `offline` set by an earlier failed read is
 * still standing lands in the shell's unreachable branch — `land()` at `login/+page.svelte` and
 * §3.1's forced password change both call this and then `goto`, so a member whose phone lost the
 * appliance and got it back typed the right password and was told the appliance is not answering,
 * on the surface §3.1 hands every new member to. Decision 283's 5 s retry clears it within one
 * interval, which is exactly why no browser test would ever be pointed at it.
 *
 * And the second writer of `/auth/me`, which is the whole reason the counter above exists: this
 * is the read that lands DURING a boot the timer armed, and the boot is the one that used to win.
 * The throw is not guarded with the writes, deliberately — a caller whose own read failed still
 * has to be told, whoever else has answered since.
 */
export async function refreshUser() {
  const seq = ++identitySeq;
  try {
    const me = await get('/auth/me');
    if (seq === identitySeq) {
      session.user = me;
      session.offline = false;
    }
  } catch (err) {
    if (!(err instanceof ApiError && err.isUnauthenticated)) throw err;
    if (seq === identitySeq) {
      session.user = null;
      session.offline = false;
    }
  }
  return session.user;
}

/**
 * §6.7, owner decision 2026-08-29: one global per-user "show the model" preference, default
 * off, toggled from the account dropdown. It reveals the transparency rail and the inline
 * numeric annotations; the title card's model line is deliberately outside it (§6.0).
 */
export async function setShowModel(on) {
  if (session.user) session.user = { ...session.user, show_model: on };
  try {
    await post('/auth/preferences', { show_model: on });
  } catch (err) {
    if (session.user) session.user = { ...session.user, show_model: !on };
    throw err;
  }
}

export function clearUser() {
  session.user = null;
}

/**
 * The credentials this account actually holds, for the account chip's second line (fe-13).
 *
 * §3.2's example reads "member · passkey + PIN", and the chip printed that string to everyone
 * — so a brand-new member with neither was told they had both. `/auth/me` has carried
 * `passkeys` and `has_pin` since M1; this reads them. The password is named when there is no
 * passkey rather than omitted, because §3.2 keeps it always available and "no credentials" is
 * not a state any account is ever in.
 */
export function authMethodLine(user) {
  if (!user) return '';
  return [user.passkeys > 0 ? 'passkey' : 'password', user.has_pin ? 'PIN' : null]
    .filter(Boolean)
    .join(' + ');
}

/** Where the shell should send someone, given what bootstrap found. */
export function landingRoute() {
  if (session.setup?.required) return '/setup';
  if (!session.user) return '/login';
  if (session.user.must_change_password) return '/account/password';
  return '/';
}
