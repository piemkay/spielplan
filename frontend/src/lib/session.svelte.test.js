import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authMethodLine, bootstrap, refreshUser, session } from './session.svelte.js';

/**
 * fe-13: the account chip printed the constant 'passkey + PIN' to everybody, so §3.2's example
 * string was rendered as a claim about credentials a brand-new member did not have.
 */
describe('authMethodLine', () => {
  it('names the password when there is no passkey, because §3.2 always keeps one', () => {
    // Not the empty string: an account with neither a passkey nor a PIN still has a password.
    expect(authMethodLine({ passkeys: 0, has_pin: false })).toBe('password');
  });

  it('names the PIN only when one is set', () => {
    expect(authMethodLine({ passkeys: 0, has_pin: true })).toBe('password + PIN');
  });

  it('reproduces the example in §3.2 for the account that has both', () => {
    expect(authMethodLine({ passkeys: 2, has_pin: true })).toBe('passkey + PIN');
  });

  it('drops the password once a passkey exists, because §3.2 makes passkeys primary', () => {
    expect(authMethodLine({ passkeys: 1, has_pin: false })).toBe('passkey');
  });

  it('reads a missing count as no passkey rather than as NaN', () => {
    // `/auth/me` carries both fields, but the chip renders during bootstrap from whatever
    // `session.user` holds at that instant.
    expect(authMethodLine({})).toBe('password');
  });

  it('says nothing at all when nobody is signed in', () => {
    expect(authMethodLine(null)).toBe('');
  });
});

/**
 * M4.15 finding 15 (fe-07): an offline boot signed a live member out.
 *
 * `bootstrap()` rethrew every non-401 `/auth/me` failure — after its own `finally` had set
 * `booted` — so the shell's `onMount` guard was skipped, the promise rejected unhandled, and the
 * already-armed `$effect` routed from `user === null`. Offline, the service worker served the
 * cached shell and what it opened was a login form whose POST could not leave the device, with
 * the session cookie untouched the whole time. §3.1 asks for an explicit state instead of an
 * error, and these say which state each read produces: an answer, a refusal, or nothing.
 *
 * Unregistered in `spec_coverage.toml` by decision 274 — `npm --prefix e2e run fresh`, the suite
 * a milestone closes on, does not run vitest, so a row discharged by a vitest id would be a row
 * nothing in the gate executes. The rules themselves are asserted at the e2e and static layers;
 * this is where the branch arithmetic is falsifiable in a second.
 */
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

  /** One settled Response double; `wire()` builds every answer it gives from this. */
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

  /**
   * The two reads of `/auth/me` overlapping, in the order decision 283's timer produces them:
   * the boot's is dispatched first and hangs, the sign-in's is dispatched second and answers.
   * Returns the settle function for the first one, so the case decides when the stale read dies.
   */
  function overlappingReads(late) {
    // Given a body rather than left undefined: `npm run check` reads the executor as something
    // that may not have run, and a checker that always fails is a checker nobody reads (finding 26).
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
    // The store is a module-level singleton, exactly as it is in the app, so each case states
    // the whole world it starts from rather than inheriting the last one's.
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

    // The half of this that is a regression guard: the 401 branch is deliberately unchanged,
    // because a refusal IS a sign-out and the fix must not swallow it into "unknown" too.
    expect(session.user).toBeNull();
    // Not offline: the box answered. A retry that reaches a restarted appliance holding a pruned
    // session has to reach /login, not sit on the unreachable card for ever.
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
    // The declaration is the defect, not the assignment: `bootstrap()` swallows a failed
    // `/config` into null and never assigns, so the header rendered its badge from whatever the
    // store was born holding. Born `false`, that badge asserted an empty library to any
    // household whose `/config` read failed — the one line of the shell that states a fact
    // about the collection rather than rendering one. A fresh module is the only place the
    // initial value can be read, because every case below has already moved it (271).
    vi.resetModules();
    const fresh = await import('./session.svelte.js');
    expect(fresh.session.hasBundle).toBeNull();
  });

  it('does not downgrade a bundle it already knows about when /config fails', async () => {
    session.hasBundle = true;
    wire({ ...ANSWERED, '/config': SILENT });

    await bootstrap();

    // A regression guard rather than evidence of a fix: `bootstrap()` has always assigned only
    // inside `if (config)`. It is here because the tri-state above is worth nothing if a later
    // rewrite starts writing the swallowed null through.
    expect(session.hasBundle).toBe(true);
  });

  it('keeps a settled /setup/state answer when a later boot cannot read it', async () => {
    // The sibling of the case above, and it had no guard at all: the swallowed null was written
    // straight through, so a SECOND boot destroyed an answer this document already held and the
    // shell then rendered decision 286's card, whose sentence says the answer "did not arrive" to
    // a device that had it. `required: false` is the half that keeps for ever - `api/setup.py`
    // defines it as "no admin exists" and `api/admin.py` refuses to demote, disable or delete the
    // last one, so once false it is false on any box and in any session.
    session.setup = { required: false, note: 'ready' };
    wire({ ...ANSWERED, '/setup/state': SILENT });

    await bootstrap();

    expect(session.setup).toEqual({ required: false, note: 'ready' });
  });

  it('drops a required: true it can no longer confirm, because that bit does go stale', async () => {
    // The asymmetry is deliberate. An admin created on ANOTHER device makes `required: true`
    // false, and `setup/+page.svelte` reads that bit as `hasAdmin` under fe-46's default - a
    // payload not read yet counts as an admin existing, because the failure worth defaulting
    // against is offering "create the admin account" to somebody who must not see it.
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
  });

  // `refreshUser()` is the other reader of /auth/me, and the offline flag is one fact about the
  // appliance rather than one fact per function: a read that got through is proof it answered,
  // whoever made it. `land()` on the sign-in page and §3.1's forced password change both call
  // this and then navigate, so without the clear a member whose phone lost the appliance and got
  // it back typed the right password and met the unreachable card.
  it('drops the offline state when refreshUser gets an answer', async () => {
    wire({ ...ANSWERED, '/auth/me': SILENT });
    await bootstrap();
    expect(session.offline).toBe(true);

    wire(ANSWERED);
    await refreshUser();

    expect(session.user?.name).toBe('Ada');
    expect(session.offline).toBe(false);
  });

  // THE TWO WRITERS OF /auth/me, OVERLAPPING. `+layout.svelte`'s `retrying` serialises boot
  // against boot and cannot see `refreshUser()` at all, so on the journey decision 283's timer
  // exists for - the card up, a tick armed, the box coming back, the member signing in on the
  // /login form that renders over the card - the boot's twenty-second-old read landed on top of
  // the sign-in. `api.js` gives each request ten seconds and a boot spends two of them in series,
  // which is how a read gets that old. [review cycle 3: M415-C3-SESS-01]
  it('lets the newest read of /auth/me decide, not the one that answers last', async () => {
    const { settle, reads } = overlappingReads(ANSWERED['/auth/me']);

    const booting = bootstrap();
    // The boot's `/auth/me` is dispatched only after `/config` and `/setup/state` have settled,
    // so the sign-in below has to wait for it or it would be the FIRST read rather than the
    // second and the case would be testing the opposite order.
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
    // The narrower half, and the worse one: the timer's request left before the sign-in cookie
    // existed, so the appliance answers it honestly with a 401. Unguarded that wrote
    // `session.user = null` over the person who had just signed in, and `guard()` then routed
    // them back to /login. [review cycle 3: M415-C3-SESS-01]
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
