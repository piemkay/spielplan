import { afterEach, describe, expect, it, vi } from 'vitest';

import { putAway } from './notices.js';
import { hideToast, toast } from './toast.svelte.js';

function answering(body, status = 200) {
  const fetch = vi.fn(async (/** @type {string} */ _url, /** @type {any} */ _init) => ({
    ok: status < 400,
    status,
    statusText: '',
    headers: { get: () => null },
    text: async () => JSON.stringify(body)
  }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

const settle = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

afterEach(() => {
  hideToast();
  vi.unstubAllGlobals();
});

describe("a sticky notice's x (decision 554)", () => {
  it('hides it until tomorrow, and Undo brings it back and re-reads Home', async () => {
    const fetch = answering({ notice: 'pending', hidden_at: '2026-10-02T21:30:00Z' });
    const onBack = vi.fn();
    await putAway('pending', onBack);

    expect(fetch.mock.calls.map(([url, init]) => [url, init.method])).toEqual([
      ['/api/home/notices/pending', 'PUT']
    ]);
    expect([toast.message, toast.actionLabel]).toEqual(['Hidden until tomorrow', 'Undo']);
    expect(onBack).not.toHaveBeenCalled();

    answering({ notice: 'pending', hidden_at: null });
    toast.action();
    await settle();
    const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect([url, init.method]).toEqual(['/api/home/notices/pending', 'DELETE']);
    expect(onBack).toHaveBeenCalledOnce();
  });

  it('shows no toast when the server refuses', async () => {
    answering({ detail: 'nope' }, 422);
    await expect(putAway('wish_list')).rejects.toThrow();
    expect(toast.message).toBe('');
  });
});
