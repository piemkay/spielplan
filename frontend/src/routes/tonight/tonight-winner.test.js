/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import TonightPage from './+page.svelte';
import { session } from '$lib/session.svelte.js';
import { leave, tonight } from '$lib/tonight.svelte.js';

let target;
let app;

beforeEach(() => {
  vi.stubGlobal('WebSocket', class {
    close() {}
  });
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers(),
    text: async () => JSON.stringify({ rooms: [] })
  })));
  leave();
  tonight.booted = true;
  tonight.loading = false;
  session.user = { id: 1, name: 'Mia', role: 'member', must_change_password: false };
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
  leave();
  session.user = null;
  vi.unstubAllGlobals();
});

it("draws the winner's poster on the reveal, keyed on the payload's title_id", async () => {
  app = mount(TonightPage, { target });
  for (let i = 0; i < 20; i++) await Promise.resolve();
  tonight.step = 'reveal';
  tonight.result = {
    participants: 2,
    approval_share: 1,
    unanimous: true,
    winner: {
      title_id: 949, name: 'Heat', kind: 'movie', year: 1995, runtime_min: 170,
      fit_line: '', match_lines: []
    },
    runners_up: [],
    wildcard: null
  };
  flushSync();
  const winner = target.querySelector('[data-testid="tonight-winner"]');
  expect(winner.querySelector('img').getAttribute('src')).toBe('/api/art/949/poster');
});
