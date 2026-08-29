import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import { ApiError, api, qs } from './api.js';

describe('qs', () => {
  it('drops empty values so the URL stays readable', () => {
    expect(qs({ a: 1, b: null, c: undefined, d: '', e: false, f: 'x' })).toBe('?a=1&f=x');
  });

  it('returns an empty string when nothing survives', () => {
    expect(qs({ a: null, b: '' })).toBe('');
  });

  it('expands an array into repeated parameters', () => {
    // FastAPI's `list[...]` expects `?kind=movie&kind=series`; a comma-joined value would
    // arrive as one nonsense literal.
    expect(qs({ kind: ['movie', 'series'] })).toBe('?kind=movie&kind=series');
  });

  it('keeps a single-element array repeated rather than scalar', () => {
    expect(qs({ kind: ['movie'] })).toBe('?kind=movie');
  });

  it('omits an empty array entirely, which the API then rejects', () => {
    // Deliberate: an empty selection must be unrepresentable in the URL, so it surfaces as a
    // 422 rather than a silent "everything" (§4.1 rule 5).
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
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const respond = (status, body, ok = status < 400) =>
    Promise.resolve({
      ok,
      status,
      statusText: 'x',
      text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
    });

  it('sends the session cookie', async () => {
    fetch.mockReturnValue(respond(200, { ok: true }));
    await api('/health');
    expect(fetch).toHaveBeenCalledWith('/api/health', expect.objectContaining({ credentials: 'include' }));
  });

  it('serialises a body and sets the content type', async () => {
    fetch.mockReturnValue(respond(200, {}));
    await api('/x', { method: 'POST', body: { a: 1 } });
    const [, opts] = fetch.mock.calls[0];
    expect(opts.body).toBe('{"a":1}');
    expect(opts.headers['content-type']).toBe('application/json');
  });

  it('returns null on 204 without trying to parse it', async () => {
    fetch.mockReturnValue(Promise.resolve({ ok: true, status: 204 }));
    await expect(api('/x')).resolves.toBeNull();
  });

  it('raises ApiError with the server detail', async () => {
    fetch.mockReturnValue(respond(422, { detail: 'select at least one kind' }, false));
    await expect(api('/titles')).rejects.toMatchObject({
      status: 422,
      message: 'select at least one kind',
    });
  });

  it('flags an unauthenticated error so the shell can redirect', async () => {
    fetch.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    const err = await api('/auth/me').catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.isUnauthenticated).toBe(true);
  });

  it('recognises the forced password change, which is a 403 the UI must not treat as denial', async () => {
    fetch.mockReturnValue(
      respond(403, { detail: 'password change required before this account can be used' }, false)
    );
    const err = await api('/titles').catch((e) => e);
    expect(err.needsPasswordChange).toBe(true);
  });

  it('survives a non-JSON error body', async () => {
    fetch.mockReturnValue(respond(500, '<html>gateway</html>', false));
    const err = await api('/x').catch((e) => e);
    expect(err.status).toBe(500);
    expect(err).toBeInstanceOf(ApiError);
  });

  it('carries the structured detail an import failure returns', async () => {
    // The bundle importer answers 422 with {report, text}; the Data tab renders the report,
    // so the client must not flatten it to a string.
    fetch.mockReturnValue(respond(422, { detail: { report: { ok: false }, text: 'x' } }, false));
    const err = await api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.detail.report.ok).toBe(false);
  });
});
