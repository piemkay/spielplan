/**
 * Shared session + app-config state. Spec v2.1 §3.1, §3.2.
 *
 * `bootstrap()` answers three questions in two requests, and the answers decide the whole
 * shell: is a setup wizard owed (no admin exists), is anyone signed in, and is there an
 * artifact bundle. A bundle-less app is a legal state (§3.1) — `hasBundle: false` is a value
 * the UI renders, never an error it catches.
 */

import { get, post, ApiError } from '$lib/api.js';

export const session = $state({
  // `loading` is only true until the FIRST bootstrap resolves. Later refreshes must not flip
  // it: the shell blanks the page while loading, and blanking it mid-flow destroys whatever
  // component asked for the refresh — which reset the setup wizard to step 1 every time the
  // bundle import finished.
  loading: true,
  booted: false,
  /** @type {null | {id:number,name:string,role:string,must_change_password:boolean}} */
  user: null,
  /** @type {null | {required:boolean, steps:{step:string,done:boolean}[], has_admin:boolean, member_count:number, bundle:any, note:string}} */
  setup: null,
  hasBundle: false,
  /** @type {any} */
  bundle: null,
  publicUrl: ''
});

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
    session.setup = setup;

    try {
      session.user = await get('/auth/me');
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthenticated) session.user = null;
      else throw err;
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

/** Where the shell should send someone, given what bootstrap found. */
export function landingRoute() {
  if (session.setup?.required) return '/setup';
  if (!session.user) return '/login';
  if (session.user.must_change_password) return '/account/password';
  return '/';
}
