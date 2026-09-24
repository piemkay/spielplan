/**
 * @vitest-environment jsdom
 *
 * §6.6's System card with all five of its things. Spec v2.1 §6.6, §2; decisions 182, 454.
 *
 * Decision 182 shipped three facts and `18-system.spec.js` held the card to exactly three, so the
 * absence of the rest could not drift into a claim. Decision 454 adds queue depth, last syncs and
 * the web process's recent log lines, and keeps the card read-only: the log level filter is its one
 * control and it asks the server nothing. Both halves are asserted here against the payload
 * `api/admin.system_card` answers, because "issues no request" is a count, not a screenshot.
 *
 * Named `system-page.test.js` because SvelteKit reserves the `+` prefix inside `src/routes`.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import SystemPage from './+page.svelte';

const FACTS = ['backup', 'jobs', 'last_syncs', 'logs', 'queue', 'secrets'];

/** `GET /api/admin/system`, the six keys decision 454 settles. */
const card = () => ({
  backup: { at: null, bytes: null, stale: true, stale_after_hours: 36 },
  jobs: [
    {
      name: 'jellyfin-sessions-poll',
      started_at: '2026-09-24T08:00:00+00:00',
      finished_at: '2026-09-24T08:00:01+00:00',
      ok: true,
      detail: {}
    }
  ],
  secrets: { configured: true, fingerprint: '0123456789ab', key_id: 'k1', unreadable: false },
  queue: {
    by_state: { pending: 9, leased: 0, done: 4, failed: 1, skipped: 0 },
    by_kind: [
      { kind: 'acquire', state: 'done', count: 4 },
      { kind: 'acquire', state: 'failed', count: 1 },
      { kind: 'acquire', state: 'pending', count: 9 }
    ]
  },
  last_syncs: [
    {
      name: 'jellyfin-seen-sync',
      connector: 'Jellyfin',
      at: '2026-09-24T07:45:00+00:00',
      detail: {}
    },
    { name: 'acquisition-drain', connector: 'Metadata sources', at: null, detail: null }
  ],
  logs: {
    scope: 'web process',
    since: '2026-09-24T06:00:00+00:00',
    records: [
      { at: '2026-09-24T06:00:01+00:00', level: 'INFO', logger: 'spielplan.app', message: 'booted' },
      {
        at: '2026-09-24T07:00:00+00:00',
        level: 'WARNING',
        logger: 'spielplan.sources',
        message: 'tmdb answered 429'
      },
      {
        at: '2026-09-24T07:30:00+00:00',
        level: 'ERROR',
        logger: 'spielplan.connectors',
        message: 'jellyfin refused the key'
      }
    ]
  }
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(get).mockResolvedValue(card());
});

afterEach(() => {
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(SystemPage, { target });
  await settle();
  return app;
}

const messages = () =>
  [...target.querySelectorAll('[data-testid="system-logs"] li')].map((li) => li.textContent);

describe("the System card's six facts (decision 454)", () => {
  it('renders a section for each of the six keys the route answers', async () => {
    const app = await open();
    try {
      for (const fact of FACTS) {
        expect(target.querySelector(`[data-testid="system-${fact}"]`), fact).not.toBeNull();
      }
      const queue = target.querySelector('[data-testid="system-queue"]');
      expect(queue.textContent).toContain('pending 9');
      expect(queue.textContent).toContain('failed 1');
      const never = target.querySelector('[data-last-sync="acquisition-drain"]');
      expect(never.textContent).toContain('never succeeded');
      const synced = target.querySelector('[data-last-sync="jellyfin-seen-sync"]');
      expect(synced.textContent).toMatch(/ago/);
      // The scope is stated, so the panel does not imply the worker's log.
      expect(target.querySelector('[data-testid="system-logs"]').textContent).toContain(
        'container log'
      );
    } finally {
      unmount(app);
    }
  });

  it('lists the log lines newest first', async () => {
    const app = await open();
    try {
      const shown = messages();
      expect(shown).toHaveLength(3);
      expect(shown[0]).toContain('jellyfin refused the key');
      expect(shown[2]).toContain('booted');
    } finally {
      unmount(app);
    }
  });

  it('narrows the log lines by level and asks the server nothing to do it', async () => {
    const app = await open();
    try {
      expect(get).toHaveBeenCalledTimes(1);
      const select = target.querySelector('[data-testid="system-logs"] select');
      select.value = 'warning';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      flushSync();
      expect(messages().map((m) => m.includes('booted'))).toEqual([false, false]);
      select.value = 'error';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      flushSync();
      expect(messages()).toHaveLength(1);
      expect(messages()[0]).toContain('jellyfin refused the key');
      expect(get, 'the filter is applied to what the one read returned').toHaveBeenCalledTimes(1);
      expect(post).not.toHaveBeenCalled();
      expect(api).not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }
  });

  it('carries no control that writes: the level filter is the only one', async () => {
    const app = await open();
    try {
      const controls = FACTS.flatMap((fact) => [
        ...target
          .querySelector(`[data-testid="system-${fact}"]`)
          .querySelectorAll('button, input, select, [role=button]')
      ]);
      expect(controls.map((c) => c.tagName)).toEqual(['SELECT']);
    } finally {
      unmount(app);
    }
  });
});
