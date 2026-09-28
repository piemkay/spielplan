/**
 * @vitest-environment jsdom
 */

import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn() }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  return { page: writable({ url: new URL('http://localhost/admin'), state: {} }) };
});

import * as stores from '$app/stores';
import { get } from '$lib/api.js';
import { session } from '$lib/session.svelte.js';
import { spend } from '$lib/spendGuard.svelte.js';
import Layout from './+layout.svelte';

const page = /** @type {any} */ (stores.page);
const SYSTEM = { jobs: [], acquisition: { parked: 96, failed: 0, ready: 12 } };

let target;
let mounts;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  mounts = 0;
  vi.mocked(get).mockReset();
  vi.mocked(get).mockImplementation((path) =>
    Promise.resolve(path === '/admin/system' ? SYSTEM : { meter: null })
  );
  session.user = /** @type {any} */ ({ id: 1, name: 'admin', admin_reauth_required: false });
});

afterEach(() => {
  target.remove();
  session.user = null;
  spend.llm = null;
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open(path) {
  page.set({ url: new URL(`http://localhost${path}`), state: {} });
  const children = createRawSnippet(() => ({
    render: () => '<div data-section></div>',
    setup: () => {
      mounts += 1;
    }
  }));
  const app = mount(Layout, { target, props: { children } });
  await settle();
  return app;
}

const back = () => target.querySelector('[data-testid="admin-back"]');
const current = () => target.querySelector('nav[aria-label="Admin"] [aria-current="page"]');

describe('the admin frame (decision 527)', () => {
  it('goes back to Spielplan from Overview, to Admin from a section, to People from a person', async () => {
    for (const [path, href, label, section] of [
      ['/admin', '/', 'Spielplan', 'Overview'],
      ['/admin/system', '/admin', 'Admin', 'System'],
      ['/admin/people/3', '/admin/people', 'People', 'People']
    ]) {
      const app = await open(path);
      try {
        expect(back().getAttribute('href'), path).toBe(href);
        expect(back().textContent.trim(), path).toBe(label);
        expect(current().textContent.trim(), path).toBe(section);
      } finally {
        unmount(app);
      }
    }
  });

  it("badges New titles with the waiting count, read from the whole board", async () => {
    const app = await open('/admin');
    try {
      const titles = target.querySelector('nav[aria-label="Admin"] a[href="/admin/titles"]');
      expect(titles.textContent.replace(/\s+/g, ' ').trim()).toBe('New titles 96 waiting');
    } finally {
      unmount(app);
    }
  });

  it('re-reads the count on every section change, and follows Budget for its dot', async () => {
    const app = await open('/admin');
    try {
      const nav = target.querySelector('nav[aria-label="Admin"]');
      const titles = () =>
        nav.querySelector('a[href="/admin/titles"]').textContent.replace(/\s+/g, ' ').trim();
      expect(titles()).toBe('New titles 96 waiting');
      const fewer = { ...SYSTEM, acquisition: { ...SYSTEM.acquisition, parked: 3 } };
      vi.mocked(get).mockImplementation((path) =>
        Promise.resolve(path === '/admin/system' ? fewer : { meter: null })
      );
      page.set({ url: new URL('http://localhost/admin/titles'), state: {} });
      await settle();
      expect(titles(), 'counted again on the section change').toBe('New titles 3 waiting');

      const budget = () => nav.querySelector('a[href="/admin/budget"] [aria-label="needs attention"]');
      expect(budget()).toBeNull();
      spend.llm = { meter: { cap_usd: null, remaining_usd: null, spent_usd: '0.00' } };
      flushSync();
      expect(budget(), "Budget's own read, without a trip to Overview").not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('asks every refused read again once the re-prompt is answered', async () => {
    session.user.admin_reauth_required = true;
    const app = await open('/admin');
    try {
      expect(mounts).toBe(1);
      const reads = vi.mocked(get).mock.calls.length;
      session.user.admin_reauth_required = false;
      await settle();
      expect(mounts, 'the section mounted again').toBe(2);
      expect(vi.mocked(get).mock.calls.length).toBeGreaterThan(reads);
    } finally {
      unmount(app);
    }
  });
});
