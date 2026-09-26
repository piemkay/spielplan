import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authMethodLine, bootstrap, refreshUser, roleWord, session } from './session.svelte.js';

describe('authMethodLine', () => {
  it('names the password when there is no passkey, because §3.2 always keeps one', () => {
    // Not the empty string: an account with neither a passkey nor a PIN still has a password.
    expect(authMethodLine({ passkeys: 0, has_pin: false })).toBe('signs in with a password');
  });

  it('names the PIN only when one is set, and says what it is for', () => {
    expect(authMethodLine({ passkeys: 0, has_pin: true })).toBe(
      'signs in with a password · PIN set for quick switching'
    );
  });

  it('states both for the account that has both, as §3.2 example does', () => {
    expect(authMethodLine({ passkeys: 2, has_pin: true })).toBe(
      'signs in with a passkey · PIN set for quick switching'
    );
  });

  it('drops the password once a passkey exists, because §3.2 makes passkeys primary', () => {
    expect(authMethodLine({ passkeys: 1, has_pin: false })).toBe('signs in with a passkey');
  });

  it('reads a missing count as no passkey rather than as NaN', () => {
    // The chip renders during bootstrap from whatever `session.user` holds then.
    expect(authMethodLine({})).toBe('signs in with a password');
  });

  it('names the role as a word', () => {
    expect(roleWord('member')).toBe('Member');
    expect(roleWord('admin')).toBe('Admin');
  });

  it('says nothing at all when nobody is signed in', () => {
    expect(authMethodLine(null)).toBe('');
  });
});

// A failed `/auth/me` is an explicit state, not a sign-out: an answer, a refusal, or nothing.
describe('bootstrap', () => {
  const fetchMock = vi.fn();

  const ANSWERED = {
    '/config': { has_bundle: true, bundle: { id: 'b1' }, public_url: 'http://spielplan.local' },
    '/setup/state': { required: false, note: 'ready' },
    '/auth/me': { id: 7, name: 'Ada', role: 'member', must_change_password: false }
  };

  /** The socket that is open and silent, which is how a phone leaves the house. */
  const SILENT = new TypeError('Failed to fetch');
  /** The appliance answering that this session is over, which is a fact and not a gap. */
  const REFUSED = { status: 401, body: { detail: 'not signed in' } };

  function answered(answer) {
    const status = answer.status ?? 200;
    return Promise.resolve({
      ok: status < 400,
      status,
      statusText: 'x',
      headers: new Headers(),
      text: () => Promise.resolve(JSON.stringify(answer.body ?? answer))
    });
  }

  function wire(routes) {
    fetchMock.mockImplementation((url) => {
      const path = String(url).replace(/^\/api/, '');
      const answer = routes[path];
      if (answer === undefined) throw new Error(`the test wired no answer for ${path}`);
      if (answer instanceof Error) return Promise.reject(answer);
      return answered(answer);
    });
  }

  // The boot's `/auth/me` hangs while the sign-in's answers; returns the settle for the stale one.
  function overlappingReads(late) {
    // Given a body so svelte-check does not flag `settle` as possibly unassigned.
    /** @type {(answer: any) => void} */
    let settle = () => {};
    const hanging = new Promise((resolve, reject) => {
      settle = (answer) => (answer instanceof Error ? reject(answer) : resolve(answered(answer)));
    });
    let reads = 0;
    fetchMock.mockImplementation((url) => {
      const path = String(url).replace(/^\/api/, '');
      if (path !== '/auth/me') return answered(ANSWERED[path]);
      reads += 1;
      return reads === 1 ? hanging : answered(late);
    });
    return { settle, reads: () => reads };
  }

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    // The store is a module singleton, so each case states its whole starting world.
    Object.assign(session, {
      loading: true,
      booted: false,
      user: null,
      setup: null,
      hasBundle: null,
      restartRequired: null,
      bundle: null,
      publicUrl: '',
      offline: false
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('resolves when the appliance does not answer, instead of rejecting past the guard', async () => {
    wire({ ...ANSWERED, '/auth/me': SILENT });
    await expect(bootstrap()).resolves.toBeUndefined();
    expect(session.booted).toBe(true);
  });

  it('leaves a signed-in member signed in when the read fails', async () => {
    session.booted = true;
    session.user = { id: 7, name: 'Ada', role: 'member', must_change_password: false };
    wire({ ...ANSWERED, '/auth/me': SILENT });

    await bootstrap();

    expect(session.offline).toBe(true);
    // The cookie is HttpOnly and untouched; nothing here learnt otherwise.
    expect(session.user?.name).toBe('Ada');
  });

  it('still signs the member out when the appliance refuses, because that is an answer', async () => {
    session.user = { id: 7, name: 'Ada', role: 'member', must_change_password: false };
    wire({ ...ANSWERED, '/auth/me': REFUSED });

    await bootstrap();

    expect(session.user).toBeNull();
    // Not offline: the box answered, so a retry must be able to reach /login.
    expect(session.offline).toBe(false);
  });

  it('drops the offline state as soon as a read gets through again', async () => {
    wire({ ...ANSWERED, '/auth/me': SILENT });
    await bootstrap();
    expect(session.offline).toBe(true);

    wire(ANSWERED);
    await bootstrap();

    expect(session.offline).toBe(false);
    expect(session.user?.name).toBe('Ada');
  });

  it('starts with the bundle unknown rather than absent', async () => {
    // A fresh module is the only place the initial value can be read.
    vi.resetModules();
    const fresh = await import('./session.svelte.js');
    expect(fresh.session.hasBundle).toBeNull();
  });

  it('does not downgrade a bundle it already knows about when /config fails', async () => {
    session.hasBundle = true;
    wire({ ...ANSWERED, '/config': SILENT });

    await bootstrap();

    expect(session.hasBundle).toBe(true);
  });

  it('keeps a settled /setup/state answer when a later boot cannot read it', async () => {
    // `required: false` never goes stale: the last admin cannot be removed.
    session.setup = { required: false, note: 'ready' };
    wire({ ...ANSWERED, '/setup/state': SILENT });

    await bootstrap();

    expect(session.setup).toEqual({ required: false, note: 'ready' });
  });

  it('drops a required: true it can no longer confirm, because that bit does go stale', async () => {
    // Asymmetric: an admin made on another device turns `required: true` stale.
    session.setup = { required: true, note: 'first boot' };
    wire({ ...ANSWERED, '/setup/state': SILENT });

    await bootstrap();

    expect(session.setup).toBeNull();
  });

  it('takes the answer when /setup/state gives one', async () => {
    session.setup = { required: false, note: 'stale' };
    wire(ANSWERED);

    await bootstrap();

    expect(session.setup.note).toBe('ready');
  });

  it('takes the answer when /config gives one', async () => {
    wire(ANSWERED);

    await bootstrap();

    expect(session.hasBundle).toBe(true);
    expect(session.publicUrl).toBe('http://spielplan.local');
    // A /config without the field is a backend from before decision 497, which never owed one.
    expect(session.restartRequired).toBe(false);
  });

  it('carries a restart the backend says is owed, and clears it when the backend stops saying so', async () => {
    wire({ ...ANSWERED, '/config': { ...ANSWERED['/config'], has_bundle: false, restart_required: true } });
    await bootstrap();
    expect(session.hasBundle).toBe(false);
    expect(session.restartRequired).toBe(true);

    wire(ANSWERED);
    await bootstrap();
    expect(session.restartRequired).toBe(false);
  });

  // A read that got through proves the appliance answered, whoever made it.
  it('drops the offline state when refreshUser gets an answer', async () => {
    wire({ ...ANSWERED, '/auth/me': SILENT });
    await bootstrap();
    expect(session.offline).toBe(true);

    wire(ANSWERED);
    await refreshUser();

    expect(session.user?.name).toBe('Ada');
    expect(session.offline).toBe(false);
  });

  // The boot's slow `/auth/me` must not land on top of a sign-in's newer read.
  it('lets the newest read of /auth/me decide, not the one that answers last', async () => {
    const { settle, reads } = overlappingReads(ANSWERED['/auth/me']);

    const booting = bootstrap();
    // Wait for the boot's read, or the sign-in would be the first read, not the second.
    await vi.waitFor(() => expect(reads()).toBe(1));

    await refreshUser();
    expect(session.user?.name, 'the sign-in was not read back at all').toBe('Ada');

    // Ten seconds later, on `api.js`'s own deadline, the boot's read dies.
    settle(new TypeError('Failed to fetch'));
    await booting;

    expect(
      session.offline,
      'a read older than the sign-in put the unreachable card over a working shell'
    ).toBe(false);
    expect(session.user?.name).toBe('Ada');
  });

  it('does not let a stale 401 sign out the member who has just signed in', async () => {
    // The stale request predates the sign-in cookie, so its honest 401 must not sign the member out.
    const { settle, reads } = overlappingReads(ANSWERED['/auth/me']);

    const booting = bootstrap();
    await vi.waitFor(() => expect(reads()).toBe(1));

    await refreshUser();
    expect(session.user?.name).toBe('Ada');

    settle(REFUSED);
    await booting;

    expect(session.user?.name, 'a 401 for a request older than the sign-in signed it out').toBe(
      'Ada'
    );
    expect(session.offline).toBe(false);
  });

  it('drops the offline state when refreshUser is refused, because a refusal is an answer', async () => {
    wire({ ...ANSWERED, '/auth/me': SILENT });
    await bootstrap();
    expect(session.offline).toBe(true);

    wire({ ...ANSWERED, '/auth/me': REFUSED });
    await refreshUser();

    expect(session.user).toBeNull();
    expect(session.offline).toBe(false);
  });
});
