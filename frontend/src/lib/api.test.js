import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import { ApiError, api, onUnauthenticated, qs } from './api.js';
import { session } from './session.svelte.js';

describe('qs', () => {
  it('drops empty values so the URL stays readable', () => {
    expect(qs({ a: 1, b: null, c: undefined, d: '', e: false, f: 'x' })).toBe('?a=1&f=x');
  });

  it('returns an empty string when nothing survives', () => {
    expect(qs({ a: null, b: '' })).toBe('');
  });

  it('expands an array into repeated parameters', () => {
    // A comma-joined value would arrive as one nonsense literal.
    expect(qs({ kind: ['movie', 'series'] })).toBe('?kind=movie&kind=series');
  });

  it('keeps a single-element array repeated rather than scalar', () => {
    expect(qs({ kind: ['movie'] })).toBe('?kind=movie');
  });

  it('omits an empty array entirely, which the API then rejects', () => {
    // Deliberate: an empty selection surfaces as a 422, never a silent "everything".
    expect(qs({ kind: [] })).toBe('');
  });

  it('encodes values that need it', () => {
    expect(qs({ q: 'a b&c' })).toBe('?q=a+b%26c');
  });

  it('keeps a zero, which is a real value', () => {
    expect(qs({ offset: 0 })).toBe('?offset=0');
  });
});

describe('api', () => {
  // Held in a local so the mock keeps its vitest type for the checker.
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    // The seam is one module-level registration; clear it between cases.
    onUnauthenticated(null);
    // `session` is a module singleton, and the seam reads `session.user`.
    session.user = null;
  });

  // The seam keys on a tab whose `/auth/me` has already answered.
  const SIGNED_IN = { id: 7, name: 'Ada', role: 'member', must_change_password: false };

  const respond = (status, body, ok = status < 400, headers = {}) =>
    Promise.resolve({
      ok,
      status,
      statusText: 'x',
      // The client reads a header, so the double needs real Headers.
      headers: new Headers(headers),
      text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
    });

  // Open and silent, honouring the abort as a real fetch does.
  const never = (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () =>
        reject(new DOMException('The operation was aborted.', 'AbortError'))
      );
    });

  const NO_ANSWER = 'the network did not answer — try again';

  it('sends the session cookie', async () => {
    fetchMock.mockReturnValue(respond(200, { ok: true }));
    await api('/health');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/health',
      expect.objectContaining({ credentials: 'include' })
    );
  });

  it('serialises a body and sets the content type', async () => {
    fetchMock.mockReturnValue(respond(200, {}));
    await api('/x', { method: 'POST', body: { a: 1 } });
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.body).toBe('{"a":1}');
    expect(opts.headers['content-type']).toBe('application/json');
  });

  it('returns null on 204 without trying to parse it', async () => {
    fetchMock.mockReturnValue(Promise.resolve({ ok: true, status: 204 }));
    await expect(api('/x')).resolves.toBeNull();
  });

  it('raises ApiError with the server detail', async () => {
    fetchMock.mockReturnValue(respond(422, { detail: 'select at least one kind' }, false));
    await expect(api('/titles')).rejects.toMatchObject({
      status: 422,
      message: 'select at least one kind',
    });
  });

  it('names the field and the reason when pydantic refuses one', async () => {
    fetchMock.mockReturnValue(
      respond(
        422,
        {
          detail: [
            {
              type: 'int_parsing',
              loc: ['body', 'guests'],
              msg: 'Input should be a valid integer',
              ctx: {},
            },
          ],
        },
        false
      )
    );
    const err = await api('/tonight/round', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe('guests: Input should be a valid integer');
  });

  it('joins several refused fields into one sentence', async () => {
    fetchMock.mockReturnValue(
      respond(
        422,
        {
          detail: [
            { loc: ['body', 'pin'], msg: 'String should match pattern' },
            { loc: ['body', 'guests'], msg: 'Input should be a valid integer' },
          ],
        },
        false
      )
    );
    const err = await api('/auth/pin', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe(
      'pin: String should match pattern; guests: Input should be a valid integer'
    );
  });

  it('says the reason without a label when the whole body is what was refused', async () => {
    // `loc` opens with the source, so `["body"]` names no field.
    fetchMock.mockReturnValue(
      respond(422, { detail: [{ type: 'missing', loc: ['body'], msg: 'Field required' }] }, false)
    );
    const err = await api('/tonight/round', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe('Field required');
  });

  it('does not label a refused list element with its position', async () => {
    // An index is not a field name.
    fetchMock.mockReturnValue(
      respond(
        422,
        {
          detail: [
            {
              type: 'literal_error',
              loc: ['query', 'kind', 0],
              msg: "Input should be 'movie' or 'series'",
            },
          ],
        },
        false
      )
    );
    const err = await api('/titles?kind=nonsense').catch((e) => e);
    expect(err.message).toBe("Input should be 'movie' or 'series'");
  });

  it("states this backend's own refusal envelope rather than the HTTP reason phrase", async () => {
    // `{reason, message}` is this backend's usual envelope, and HTTP/2 carries no reason phrase.
    fetchMock.mockReturnValue(
      respond(409, { detail: { reason: 'round_closed', message: 'the round is already closed' } }, false)
    );
    const err = await api('/tonight/sessions/1/answer', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe('the round is already closed');
    // The importer's `text` wins where both could be read.
    const both = { text: 'row 4 is malformed', message: 'x' };
    fetchMock.mockReturnValue(respond(422, { detail: both }, false));
    const importErr = await api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    expect(importErr.message).toBe('row 4 is malformed');
  });

  it('ends a request that never answers with a sentence, not a parked surface', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    const pending = api('/rate/next').catch((e) => e);
    await vi.advanceTimersByTimeAsync(10_000);
    const err = await pending;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe(NO_ANSWER);
    expect(err.status).toBe(0);
  });

  it('gives the three long routes minutes rather than the ordinary ten seconds', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    let settled = false;
    const pending = api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    pending.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(600_000);
    expect((await pending).message).toBe(NO_ANSWER);
  });

  it('gives a route that reaches Jellyfin the budget the media server itself has', async () => {
    // The media server's client allows 15s per call, spent twice by a probe.
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    let settled = false;
    const pending = api('/admin/connectors/jellyfin/test', { method: 'POST' }).catch((e) => e);
    pending.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(settled, 'ten seconds is shorter than the floor this route inherits').toBe(false);
    await vi.advanceTimersByTimeAsync(15_000);
    expect((await pending).message).toBe(NO_ANSWER);
  });

  it('gives every route of that class the same deadline, and leaves the other two alone', async () => {
    vi.useFakeTimers();
    /** @type {[string, number][]} */
    const budgets = [
      ['/admin/connectors/jellyfin', 45_000],
      ['/admin/connectors/jellyfin/users', 45_000],
      ['/admin/users/7/jellyfin', 45_000],
      ['/admin/connectors/jellyfin/sync', 600_000],
      ['/rate/next', 10_000]
    ];
    for (const [path, deadline] of budgets) {
      fetchMock.mockImplementation(never);
      let done = false;
      const pending = api(path).catch((e) => e);
      pending.then(() => {
        done = true;
      });
      await vi.advanceTimersByTimeAsync(deadline - 1);
      expect(done, `${path} gave up before its own deadline`).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect((await pending).message, path).toBe(NO_ANSWER);
    }
  });

  it('lets a caller set the deadline for itself', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    const pending = api('/rate/next', { timeoutMs: 50 }).catch((e) => e);
    await vi.advanceTimersByTimeAsync(50);
    expect((await pending).message).toBe(NO_ANSWER);
  });

  // `statusText` is empty over HTTP/2, which is how this app ships; the other doubles set 'x'.
  const overH2 = (status, body) =>
    Promise.resolve({
      ok: false,
      status,
      statusText: '',
      headers: new Headers(),
      text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body))
    });

  it('states a sentence when the wire says nothing at all', async () => {
    // Surfaces gate their error line on truthiness, so an empty message shows nothing.
    fetchMock.mockReturnValue(overH2(502, ''));
    const err = await api('/rate/next').catch((e) => e);
    expect(err.message).toBe('the appliance refused this and did not say why');
  });

  it("does not render an edge's own error page as the app's reason", async () => {
    fetchMock.mockReturnValue(overH2(502, '<html><head><title>502 Bad Gateway</title></head></html>'));
    const err = await api('/rate/next').catch((e) => e);
    expect(err.message).not.toContain('<html');
    expect(err.message).toBe('the appliance refused this and did not say why');
    expect(err.detail, 'an unparseable body is a fact about the wire, not this app speaking').toBeNull();
  });

  it('says something rather than nothing for a shape it does not recognise', async () => {
    // A list of bare strings matches neither the pydantic branch (no `msg`) nor the object one.
    fetchMock.mockReturnValue(overH2(400, { detail: ['nope', 'also nope'] }));
    expect((await api('/x').catch((e) => e)).message).toBe(
      'the appliance refused this and did not say why'
    );
  });

  it('still prefers anything the appliance did state', async () => {
    fetchMock.mockReturnValue(overH2(409, { detail: { reason: 'closed', message: 'the round is closed' } }));
    expect((await api('/rank/x').catch((e) => e)).message).toBe('the round is closed');
  });

  it("composes with the caller's own signal rather than replacing it", async () => {
    fetchMock.mockImplementation(never);
    const caller = new AbortController();
    const pending = api('/rate/next', { signal: caller.signal }).catch((e) => e);
    caller.abort();
    const err = await pending;
    expect(err).not.toBeInstanceOf(ApiError);
    expect(err.name).toBe('AbortError');
  });

  it("tells §3.2's admin re-prompt apart from an ordinary sign-out", async () => {
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    const reauth = await api('/admin/users').catch((e) => e);
    expect(reauth.isUnauthenticated).toBe(true);
    expect(reauth.needsAdminReauth).toBe(true);

    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    const plain = await api('/auth/me').catch((e) => e);
    expect(plain.isUnauthenticated).toBe(true);
    expect(plain.needsAdminReauth).toBe(false);
  });

  it("raises §3.2's re-prompt flag on the user, so a long-open tab shows the banner", async () => {
    session.user = {
      id: 1,
      name: 'admin',
      role: 'admin',
      must_change_password: false,
      admin_reauth_required: false
    };
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    await api('/admin/users').catch(() => {});
    expect(session.user.admin_reauth_required).toBe(true);

    // An ordinary 401 is a sign-out, so it must not raise the re-prompt flag.
    session.user = {
      id: 1,
      name: 'admin',
      role: 'admin',
      must_change_password: false,
      admin_reauth_required: false
    };
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await api('/auth/me').catch(() => {});
    expect(session.user.admin_reauth_required).toBe(false);

    // And it must survive nobody being signed in, as on every anonymous fetch.
    session.user = null;
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    await expect(api('/admin/users')).rejects.toBeInstanceOf(ApiError);
  });

  it('flags an unauthenticated error so the shell can redirect', async () => {
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    const err = await api('/auth/me').catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.isUnauthenticated).toBe(true);
  });

  it('recognises the forced password change, which is a 403 the UI must not treat as denial', async () => {
    fetchMock.mockReturnValue(
      respond(403, { detail: 'password change required before this account can be used' }, false)
    );
    const err = await api('/titles').catch((e) => e);
    expect(err.needsPasswordChange).toBe(true);
  });

  it('survives a non-JSON error body', async () => {
    fetchMock.mockReturnValue(respond(500, '<html>gateway</html>', false));
    const err = await api('/x').catch((e) => e);
    expect(err.status).toBe(500);
    expect(err).toBeInstanceOf(ApiError);
  });

  it('carries the structured detail an import failure returns', async () => {
    // The Data tab renders the report, so the detail must not be flattened.
    fetchMock.mockReturnValue(respond(422, { detail: { report: { ok: false }, text: 'x' } }, false));
    const err = await api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.detail.report.ok).toBe(false);
  });

  it('calls the session-loss handler once for a 401 on a surface route', async () => {
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(respond(401, { detail: 'session expired' }, false));
    await expect(api('/rate/next')).rejects.toBeInstanceOf(ApiError);
    expect(seen).toEqual(['signed-out']);
  });

  it('leaves the credential routes alone, where a 401 is the answer and not a sign-out', async () => {
    // With nobody signed in the seam short-circuits, so this case would assert nothing.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    for (const path of [
      '/auth/login',
      '/auth/switch',
      '/auth/logout',
      '/auth/me',
      '/auth/passkey/login',
      '/auth/passkey/login/options',
      '/setup/state',
      '/setup/admin'
    ]) {
      fetchMock.mockReturnValue(respond(401, { detail: 'wrong password' }, false));
      await api(path, { method: 'POST', body: {} }).catch(() => {});
    }
    expect(seen).toEqual([]);
  });

  it('signs a member out when one of the setup routes that CAN 401 does', async () => {
    // Only these two setup routes can 401, and a member reaches the first from /account.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    for (const path of ['/setup/onboarding/complete', '/setup/connectors']) {
      fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
      await api(path, { method: 'POST', body: {} }).catch(() => {});
    }
    expect(seen).toEqual(['signed-out', 'signed-out']);
  });

  it('leaves the three routes that answer 401 for a mistyped current password alone', async () => {
    // A 401 on a credential answers the credential; `session.user` keeps the case from short-circuiting.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    for (const path of ['/auth/password', '/auth/pin', '/auth/reauth']) {
      fetchMock.mockReturnValue(respond(401, { detail: 'wrong current password' }, false));
      await api(path, { method: 'POST', body: {} }).catch(() => {});
    }
    expect(seen).toEqual([]);
  });

  it('leaves the per-member Jellyfin link alone, because that 401 is another server refusing', async () => {
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(
      respond(401, { detail: 'Jellyfin refused that sign-in: 401 Unauthorized' }, false)
    );
    await api('/admin/users/7/jellyfin', { method: 'POST', body: {} }).catch(() => {});
    expect(seen).toEqual([]);
  });

  it('still signs the admin out when the DELETE on that same path is refused', async () => {
    // `unlink_jellyfin` makes no foreign call, so its 401 is a real session loss.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await api('/admin/users/7/jellyfin', { method: 'DELETE' }).catch(() => {});
    expect(seen).toEqual(['signed-out']);
  });

  it('signs nobody out of a session that never existed, which is what a first boot is', async () => {
    // On a first boot every authenticated route answers 401; nobody held a session to lose.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = null;
    for (const path of [
      '/prompts/finish',
      '/titles?kind=movie&limit=60&offset=0',
      '/facets?kind=movie'
    ]) {
      fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
      await api(path).catch(() => {});
    }
    // The surface still gets its error.
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await expect(api('/home?kind=movie')).rejects.toBeInstanceOf(ApiError);
    expect(seen).toEqual([]);
  });

  it("does not sign the admin out of §3.2's 24-hour re-prompt", async () => {
    // A fresh object: the seam writes `admin_reauth_required` through it.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = { ...SIGNED_IN };
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    await api('/admin/users').catch(() => {});
    expect(seen).toEqual([]);
  });

  it('treats a 401 on passkey registration as the session loss it is', async () => {
    // Registration runs inside a session; only the login half is anonymous.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await api('/auth/passkey/register/options', { method: 'POST', body: {} }).catch(() => {});
    expect(seen).toEqual(['signed-out']);
  });

  it('routes §3.1\'s forced password change to the handler rather than a denial banner', async () => {
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    fetchMock.mockReturnValue(
      respond(403, { detail: 'password change required before this account can be used' }, false)
    );
    const err = await api('/titles').catch((e) => e);
    expect(seen).toEqual(['password-change']);
    expect(err.needsPasswordChange).toBe(true);
  });

  it('leaves an ordinary 403 to the surface that asked', async () => {
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    fetchMock.mockReturnValue(respond(403, { detail: 'admin role required' }, false));
    await api('/admin/users').catch(() => {});
    expect(seen).toEqual([]);
  });
});
