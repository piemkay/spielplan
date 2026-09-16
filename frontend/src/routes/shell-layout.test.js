/**
 * @vitest-environment jsdom
 *
 * What the shell does when it cannot tell where a person belongs. Spec v2.1 §3.1, §6 preamble;
 * M4.15 decision 286.
 *
 * `guard()` routes from two reads and both of them can fail. `bootstrap()` swallows a failed
 * `/setup/state` into null and `landingRoute()` reads `setup.required` to tell §3.1's first admin
 * from a signed-out member — so with that read missing and `/auth/me` having ANSWERED 401, /setup
 * and /login are both live readings of the same state and nothing in the store separates them.
 * Refusing to route there is right. Refusing and then rendering the AUTHED SHELL was not: the
 * header, an account chip reading "signed out", no nav links, and the surface underneath printing
 * `api/deps.py`'s sentence, with Log out the only way forward and no timer armed, because
 * `session.offline` is false — the appliance answered.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT, and for the reason `account-page.test.js` gives: the state
 * needs one read to fail while the next succeeds, inside one boot. No spec in `e2e/` drives a
 * failed `/setup/state` at all, and putting a server into that shape for one assertion would cost
 * the suite a fixture it has no other use for. Unregistered by decision 274 — the gate does not
 * run vitest — so the rule's standing check is the static guard beside it; this is where the
 * branch arithmetic is falsifiable in a second.
 *
 * Named `shell-layout.test.js` and not `+layout.svelte.test.js`: SvelteKit reserves the `+`
 * prefix inside `src/routes` and `vite build` fails outright on any other `+`-named file.
 */

import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ url: new URL('http://localhost/rank'), gone: [] }));

vi.mock('$app/stores', () => ({
  page: {
    subscribe: (run) => {
      run({ url: nav.url });
      return () => {};
    }
  },
  updated: {
    subscribe: (run) => {
      run(false);
      return () => {};
    }
  }
}));

vi.mock('$app/navigation', () => ({
  // `goto` is the whole observable output of `guard()`, so it is recorded rather than stubbed
  // away: "nothing routed" and "routed to /login" are the two answers this file is about.
  goto: vi.fn(async (to) => {
    nav.gone.push(to);
  }),
  beforeNavigate: () => {}
}));

import { session } from '$lib/session.svelte.js';
import Layout from './+layout.svelte';

/** The socket that is open and silent, which is how a phone leaves the house. */
const SILENT = new TypeError('Failed to fetch');
/** The appliance answering that nobody is signed in here, which is a fact and not a gap. */
const REFUSED = { status: 401, body: { detail: 'not signed in' } };
const READY = { required: false, note: 'ready' };
const ME = { id: 7, name: 'Ada', role: 'member', must_change_password: false, nav: {} };
const CONFIG = { has_bundle: true, bundle: null, public_url: 'http://spielplan.local' };

const fetchMock = vi.fn();
let target;

function wire(routes) {
  fetchMock.mockImplementation((url) => {
    const path = String(url).replace(/^\/api/, '').replace(/\?.*$/, '');
    const answer = routes[path];
    if (answer === undefined) throw new Error(`the test wired no answer for ${path}`);
    if (answer instanceof Error) return Promise.reject(answer);
    const status = answer.status ?? 200;
    return Promise.resolve({
      ok: status < 400,
      status,
      statusText: 'x',
      headers: new Headers(),
      text: () => Promise.resolve(JSON.stringify(answer.body ?? answer))
    });
  });
}

async function open() {
  const app = mount(Layout, {
    target,
    props: { children: createRawSnippet(() => ({ render: () => '<main data-surface></main>' })) }
  });
  for (let i = 0; i < 30; i++) await Promise.resolve();
  flushSync();
  return app;
}

describe('the shell when it cannot tell where a person belongs', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    target = document.createElement('div');
    document.body.appendChild(target);
    nav.url = new URL('http://localhost/rank');
    nav.gone = [];
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    Object.assign(session, {
      loading: true,
      booted: false,
      user: null,
      setup: null,
      hasBundle: null,
      bundle: null,
      publicUrl: '',
      offline: false
    });
  });

  afterEach(() => {
    target.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('states the missing read rather than rendering the shell to somebody it was told is out', async () => {
    wire({ '/config': CONFIG, '/setup/state': SILENT, '/auth/me': REFUSED });

    const app = await open();
    try {
      expect(session.user).toBeNull();
      // The distinguishing bit: `/auth/me` ANSWERED, so this is not the offline card's state.
      expect(session.offline).toBe(false);
      expect(target.querySelector('[data-testid="landing-unknown"]')).not.toBeNull();
      expect(target.querySelector('header')).toBeNull();
      expect(target.querySelector('[data-surface]')).toBeNull();
      // And nothing routed, because /setup and /login are both live readings of this state.
      expect(nav.gone).toEqual([]);
    } finally {
      unmount(app);
    }
  });

  it('lands the person the moment the missing read arrives', async () => {
    wire({ '/config': CONFIG, '/setup/state': SILENT, '/auth/me': REFUSED });
    const app = await open();
    try {
      expect(target.querySelector('[data-testid="landing-unknown"]')).not.toBeNull();

      // Decision 283's timer, armed for this state by decision 286: no event is owed when a LAN
      // box comes back with the interface up the whole time, so the shell asks again itself.
      wire({ '/config': CONFIG, '/setup/state': READY, '/auth/me': REFUSED });
      await vi.advanceTimersByTimeAsync(5_000);
      for (let i = 0; i < 30; i++) await Promise.resolve();
      flushSync();

      expect(nav.gone).toContain('/login');
    } finally {
      unmount(app);
    }
  });

  it('keeps sending §3.1s first admin to the wizard, not to a form for an account nobody has', async () => {
    // The half decision 286 must not undo. On a first boot every authenticated route answers 401
    // honestly, and the wizard is owed — so the answer here is /setup, never /login.
    nav.url = new URL('http://localhost/');
    wire({
      '/config': { ...CONFIG, has_bundle: false },
      '/setup/state': { required: true, note: 'first boot' },
      '/auth/me': REFUSED
    });

    const app = await open();
    try {
      // De-duplicated: `guard()` runs from `onMount` and again from the `$effect` that watches
      // the pathname, which is the shipped shape and not this test's business. WHERE it routes is.
      expect([...new Set(nav.gone)]).toEqual(['/setup']);
      expect(target.querySelector('[data-testid="landing-unknown"]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('still sends a lapsed member to the sign-in page when both reads answer', async () => {
    // The ordinary case, unchanged: with `/setup/state` in hand there is no ambiguity to state.
    wire({ '/config': CONFIG, '/setup/state': READY, '/auth/me': REFUSED });

    const app = await open();
    try {
      expect([...new Set(nav.gone)]).toEqual(['/login']);
      expect(target.querySelector('[data-testid="landing-unknown"]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('sends a lapsed member to /login when this device already knows the wizard is done', async () => {
    // The second-boot shape, and the one decision 286's card must not claim. This document read
    // `/setup/state` once; the hourly prune then took the cookie while the first pair of reads
    // blipped - `api.js`'s deadline is up to ten seconds wide and `/auth/me` goes out only after
    // they settle, which is the window decision 286's own text names. `bootstrap()` wrote the
    // swallowed null over the answer in hand, so the destination was knowable and the shell said
    // it was not. [review cycle 2: M415-C2-SESS-01]
    session.setup = { required: false, note: 'ready' };
    wire({ '/config': SILENT, '/setup/state': SILENT, '/auth/me': REFUSED });

    const app = await open();
    try {
      expect(target.querySelector('[data-testid="landing-unknown"]')).toBeNull();
      expect([...new Set(nav.gone)]).toEqual(['/login']);
    } finally {
      unmount(app);
    }
  });

  it('does not tell a cold boot that it is still signed in', async () => {
    // `manifest.webmanifest` starts the installed icon at `/`, which is not `bare`, so this is
    // the branch a cold launch with the appliance unreachable lands in - and module `$state` does
    // not survive a document load, so `session.user` is null every time. Said unconditionally,
    // the card told somebody who had just tapped Log out the opposite of what the tap did.
    // [decision 271; review cycle 2: M415-C2-SHELL-01]
    wire({ '/config': SILENT, '/setup/state': SILENT, '/auth/me': SILENT });

    const app = await open();
    try {
      const card = target.querySelector('[data-testid="appliance-unreachable"]');
      expect(card).not.toBeNull();
      expect(session.user).toBeNull();
      expect(card.textContent).not.toContain('still signed in');
      expect(card.textContent).toContain('signed anybody out');
    } finally {
      unmount(app);
    }
  });

  it('keeps the sentence where the shell really is holding a session', async () => {
    // The warm half, unchanged: a live tab whose `/auth/me` stops answering mid-session. A failed
    // read is not a sign-out, so the name is still in the chip and the sentence is a rendering of
    // something this device read.
    session.user = { ...ME, nav: { surfaces: [], account: [] } };
    wire({ '/config': CONFIG, '/setup/state': READY, '/auth/me': SILENT });

    const app = await open();
    try {
      const card = target.querySelector('[data-testid="appliance-unreachable"]');
      expect(card).not.toBeNull();
      expect(card.textContent).toContain('still signed in');
    } finally {
      unmount(app);
    }
  });

  it('does not open a second boot when the interface flaps during the first', async () => {
    // `online` is registered before the boot starts, so the initial boot was a fourth way in that
    // `reconnect()`'s single-flight guard could not see. Two `bootstrap()`s then wrote into one
    // `session` object: the second painted a working shell and the first one's stale `/auth/me`
    // rejected into `session.offline` on top of it, putting the unreachable card over a shell on
    // a network that was back. [decision 283; review cycle 2: M415-C2-SHELL-02]
    const asked = [];
    fetchMock.mockImplementation((url) => new Promise(() => asked.push(String(url))));

    const app = await open();
    try {
      const first = asked.length;
      expect(first, 'the boot issued no reads at all').toBeGreaterThan(0);
      window.dispatchEvent(new Event('online'));
      for (let i = 0; i < 30; i++) await Promise.resolve();
      expect(asked.length, `a second boot started: ${asked.join(', ')}`).toBe(first);
    } finally {
      unmount(app);
    }
  });

  it('leaves a signed-in member alone when only the wizard read fails', async () => {
    // `landingRoute()` needs `setup.required` only while nobody is signed in, so a failed
    // `/setup/state` is not this person's problem and must not put a card in front of them.
    wire({ '/config': CONFIG, '/setup/state': SILENT, '/auth/me': ME });

    const app = await open();
    try {
      expect(target.querySelector('[data-testid="landing-unknown"]')).toBeNull();
      expect(target.querySelector('header')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('does not say the library is empty from a read that never answered', async () => {
    // The same rule as the card above, on the one line of the shell that STATES a fact about the
    // household rather than rendering one. `/config` fails while `/auth/me` answers, so
    // `hasBundle` is null and `offline` is false: the shell renders, and `=== false` is all that
    // stands between a household whose library is full and a header telling it the library is
    // empty. No spec in `e2e/` can tell the two spellings apart - the offline branch draws no
    // header at all, and a genuinely bundle-less boot renders the badge under either.
    // [decision 271; review cycle 2: M415-C2-COV-01]
    wire({ '/config': SILENT, '/setup/state': READY, '/auth/me': ME });

    const app = await open();
    try {
      expect(session.hasBundle).toBeNull();
      expect(session.offline).toBe(false);
      const header = target.querySelector('header');
      expect(header).not.toBeNull();
      expect(header.textContent).not.toContain('no bundle imported');

      // And it is absent because the read failed, not because this build never draws it.
      session.hasBundle = false;
      flushSync();
      expect(target.querySelector('header').textContent).toContain('no bundle imported');
    } finally {
      unmount(app);
    }
  });
});
