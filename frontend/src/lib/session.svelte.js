// A read that did not answer is a third state, not `false`: `offline` rides beside the answers,
// and `hasBundle` stays null until `/config` has said (§3.1).

import { get, post, ApiError } from '$lib/api.js';

export const session = $state({
  // True only until the first bootstrap resolves: the shell blanks while loading, and a later
  // refresh flipping it would destroy the component that asked for it.
  loading: true,
  booted: false,
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
  // Only `required` and `note` reach an anonymous caller; the rest is genuinely optional.
  /**
   * @type {null | {
   *   required: boolean, note: string, steps?: {step:string,done:boolean}[],
   *   has_admin?: boolean, member_count?: number, bundle?: any
   * }}
   */
  setup: null,
  // Null while `/config` has not answered, so no claim about the library either way.
  /** @type {boolean | null} */
  hasBundle: null,
  // A bundle is imported but could not load (decision 497); null until `/config` says.
  /** @type {boolean | null} */
  restartRequired: null,
  /** @type {any} */
  bundle: null,
  publicUrl: '',
  // The version an app-minted title's poster URL carries (`art.js`); null until `/config` says.
  /** @type {string | null} */
  artEpoch: null,
  // The appliance did not answer the read that decides who holds the phone; not navigator.onLine.
  offline: false
});

// `/auth/me` has two writers (`bootstrap` and `refreshUser`), and a boot's read can land seconds
// after a sign-in. The newest read decides; `/config` and `/setup/state` have one writer.
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
      session.restartRequired = config.restart_required === true;
      session.bundle = config.bundle;
      session.publicUrl = config.public_url;
      session.artEpoch = config.art_epoch ?? null;
    }
    // A failed read must not overwrite an answer already held. Only `required: false` is kept: the
    // last admin cannot be removed, while `required: true` goes stale when an admin is made elsewhere.
    const held = session.setup;
    session.setup = setup ?? (held?.required === false ? held : null);

    // A failed read is not a sign-out; only the 401 clears anybody. A superseded read says nothing.
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
        // The appliance answered, so also end any offline state, or a retry would never reach /login.
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

// Sign-in and switch responses carry identity only; `/auth/me` carries the nav. A read that got
// through also ends `offline`, whichever function made it.
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

// The password is named when there is no passkey: §3.2 keeps it always available.
export function authMethodLine(user) {
  if (!user) return '';
  return [
    user.passkeys > 0 ? 'signs in with a passkey' : 'signs in with a password',
    user.has_pin ? 'PIN set for quick switching' : null
  ]
    .filter(Boolean)
    .join(' · ');
}

export function roleWord(role) {
  return role === 'admin' ? 'Admin' : role === 'member' ? 'Member' : String(role ?? '');
}

export function landingRoute() {
  if (session.setup?.required) return '/setup';
  if (!session.user) return '/login';
  if (session.user.must_change_password) return '/account/password';
  return '/';
}
