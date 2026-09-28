/**
 * @vitest-environment jsdom
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
  // `goto` is `guard()`'s whole observable output, so it is recorded, not stubbed away.
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
      restartRequired: null,
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

      // No event is owed when a LAN box comes back with the interface up, so the shell asks again itself.
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
    // On a first boot every authenticated route answers 401 honestly, and the wizard is owed.
    nav.url = new URL('http://localhost/');
    wire({
      '/config': { ...CONFIG, has_bundle: false },
      '/setup/state': { required: true, note: 'first boot' },
      '/auth/me': REFUSED
    });

    const app = await open();
    try {
      // De-duplicated: `guard()` runs from `onMount` and again from the pathname effect.
      expect([...new Set(nav.gone)]).toEqual(['/setup']);
      expect(target.querySelector('[data-testid="landing-unknown"]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it.each([
    [true, '/setup'],
    [false, '/login']
  ])('mounts no surface for a person the appliance says is signed out (setup required: %s)', async (
    required,
    where
  ) => {
    // Signed-out loads of / used to fire Home's reads and log four 401s before `guard()` routed away.
    nav.url = new URL('http://localhost/');
    wire({
      '/config': CONFIG,
      '/setup/state': { required, note: required ? 'first boot' : 'ready' },
      '/auth/me': REFUSED
    });
    const app = await open();
    try {
      expect(session.user).toBeNull();
      expect(target.querySelector('[data-surface]')).toBeNull();
      expect(target.querySelector('header')).toBeNull();
      expect([...new Set(nav.gone)]).toEqual([where]);
    } finally {
      unmount(app);
    }
  });

  it('still sends a lapsed member to the sign-in page when both reads answer', async () => {
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
    // The device read `/setup/state` earlier; a swallowed null must not overwrite the answer in hand.
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
    // A cold launch lands here with `session.user` null, so the card must not claim a session.
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
    // A failed read is not a sign-out, so the name is still in the chip.
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
    // `online` is registered before the boot starts, so the first boot must count as in flight.
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
    // `landingRoute()` needs `setup.required` only while nobody is signed in.
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
    // `hasBundle` is null here; only `=== false` keeps the header from calling a full library empty.
    wire({ '/config': SILENT, '/setup/state': READY, '/auth/me': ME });

    const app = await open();
    try {
      expect(session.hasBundle).toBeNull();
      expect(session.offline).toBe(false);
      const header = target.querySelector('header');
      expect(header).not.toBeNull();
      expect(header.textContent).not.toMatch(/no movie data yet/i);

      // And it is absent because the read failed, not because this build never draws it.
      session.hasBundle = false;
      flushSync();
      expect(target.querySelector('header').textContent).toMatch(/no movie data yet/i);
      expect(target.querySelector('header').textContent).not.toContain('bundle');
    } finally {
      unmount(app);
    }
  });

  it('says a restart is owed rather than that nothing was imported', async () => {
    // A bundle is imported but could not load: a member gets plain words, an admin a link to Movie data.
    const stuck = { ...CONFIG, has_bundle: false, restart_required: true };
    wire({ '/config': stuck, '/setup/state': READY, '/auth/me': ME });
    const member = await open();
    try {
      const header = target.querySelector('header');
      expect(header.textContent).toMatch(/waiting for a restart/i);
      expect(header.textContent).not.toContain('no bundle imported');
      expect(header.querySelector('a.badge')).toBeNull();
    } finally {
      unmount(member);
    }

    const ADMIN = { ...ME, role: 'admin', nav: { surfaces: [], account: [{ key: 'admin' }] } };
    wire({ '/config': stuck, '/setup/state': READY, '/auth/me': ADMIN });
    const admin = await open();
    try {
      const link = target.querySelector('header a.badge');
      expect(link.getAttribute('href')).toBe('/admin/movie-data');
      expect(link.textContent).toMatch(/restart needed/i);
    } finally {
      unmount(admin);
    }
  });
});
