/**
 * @vitest-environment jsdom
 *
 * Home's bundle-less state, in the two registers decision 486 gives it. Spec v2.1 §3.1, §6.8;
 * decisions 486 (clause 6) and 497.
 *
 * §3.1 names the state "no bundle imported" and that name is the operator's. The header already
 * spoke to each reader in their own words; Home's count line and its empty card still told a
 * member about a bundle and offered them the admin's door, and told everyone "No artifact bundle
 * has been imported" while a bundle was imported and waiting for a restart.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT: the e2e stack reaches the bundle-less state once, as the
 * first admin (`01-first-boot`), and never as a member or with a restart owed.
 *
 * Named `home-page.test.js`, not `+page.svelte.test.js`, for `rank-page.test.js`'s reason:
 * SvelteKit reserves the `+` prefix inside `src/routes`.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HomePage from './+page.svelte';
import { session } from '$lib/session.svelte.js';

const MEMBER = { id: 5, name: 'Jenny', role: 'member', nav: { account: [{ key: 'account' }] } };
const ADMIN = {
  id: 1, name: 'Patrick', role: 'admin', nav: { account: [{ key: 'account' }, { key: 'admin' }] }
};

let target;
let app;

function route(url) {
  let payload = {};
  if (url.includes('/api/titles')) payload = { items: [], total: 0, hidden: {} };
  else if (url.includes('/api/facets')) payload = { genres: [], decades: [] };
  else if (url.includes('/api/prompts/finish')) payload = [];
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => null },
    text: async () => JSON.stringify(payload)
  });
}

async function open({ user, restartRequired = false }) {
  Object.assign(session, { user, hasBundle: false, restartRequired });
  app = mount(HomePage, { target });
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(route));
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  Object.assign(session, { user: null, hasBundle: null, restartRequired: null });
  vi.unstubAllGlobals();
  target.remove();
});

const countLine = () => target.querySelector('[data-testid="count-line"]').textContent;
const card = () => target.querySelector('.empty.card');

describe('Home with no movie data', () => {
  it('tells a member in their own words and offers no admin door', async () => {
    await open({ user: MEMBER });
    expect(countLine()).toContain('no movie data yet');
    expect(countLine()).not.toContain('bundle');
    expect(card().textContent).toContain('There is no movie data yet');
    expect(card().textContent).not.toContain('bundle');
    expect(card().querySelector('a[href="/admin/data"]')).toBeNull();
  });

  it("keeps the operator's name for the state, and the door, for an admin", async () => {
    await open({ user: ADMIN });
    expect(countLine()).toContain('no bundle imported');
    expect(card().textContent).toContain('No artifact bundle has been imported');
    expect(card().querySelector('a[href="/admin/data"]').textContent).toBe('Import a bundle');
  });

  it('never says no bundle is imported while one waits for a restart', async () => {
    await open({ user: ADMIN, restartRequired: true });
    expect(countLine()).toContain('bundle imported · restart needed');
    expect(countLine()).not.toContain('no bundle imported');
    expect(card().textContent).not.toContain('No artifact bundle has been imported');
    expect(card().querySelector('a[href="/admin/data"]').textContent).toBe('Open the Data tab');
    unmount(app);

    await open({ user: MEMBER, restartRequired: true });
    expect(countLine()).toContain('waiting for a restart');
    expect(card().textContent).toContain('waiting for a restart');
    expect(card().textContent).not.toContain('bundle');
  });
});
