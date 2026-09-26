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
import PAGE_SOURCE from './+page.svelte?raw';
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

// --- the second household test: Home is phone-first (decision 516) ------------------------------

/**
 * A scriptable backend: `titles(params)` answers `/api/titles`, `facets(kinds)` answers
 * `/api/facets`, and every request's URL is kept for the assertions.
 */
function backend({
  titles = (/** @type {URLSearchParams} */ _params) => ({ items: [], total: 0, hidden: {} }),
  facets = (/** @type {string[]} */ _kinds) => ({ genres: [], decades: [] })
} = {}) {
  const seen = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url) => {
      seen.push(String(url));
      const u = new URL(String(url), 'http://localhost');
      let payload = {};
      if (u.pathname === '/api/titles') payload = titles(u.searchParams);
      else if (u.pathname === '/api/facets') payload = facets(u.searchParams.getAll('kind'));
      else if (u.pathname.startsWith('/api/prompts/finish')) payload = [];
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: async () => JSON.stringify(payload)
      });
    })
  );
  return seen;
}

const tick = async (ms = 0) => {
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, ms));
  flushSync();
};

async function openHome() {
  Object.assign(session, { user: MEMBER, hasBundle: true, restartRequired: false });
  app = mount(HomePage, { target });
  await tick();
}

const $ = (sel) => target.querySelector(sel);
const film = (id, name) => ({ id, kind: 'movie', name, year: 2000 });

async function type(text) {
  const box = $('[data-testid="home-search"]');
  box.value = text;
  box.dispatchEvent(new Event('input', { bubbles: true }));
  await tick(80);
  await tick(80);
}

describe('Home opens on the shelves, with the filters behind one control', () => {
  it('shows the kind switch and the search, and the four filters only when asked', async () => {
    backend();
    await openHome();
    expect($('[data-testid="home-search"]')).not.toBeNull();
    expect($('[role="group"][aria-label="Kind"]')).not.toBeNull();
    for (const id of ['filter-genre', 'filter-decade', 'filter-seen', 'filter-owned']) {
      expect($(`[data-testid="${id}"]`), `${id} is on the first screen`).toBeNull();
    }
    const toggle = $('[data-testid="filter-toggle"]');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    toggle.click();
    flushSync();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    for (const id of ['filter-genre', 'filter-decade', 'filter-seen', 'filter-owned']) {
      expect($(`[data-testid="${id}"]`), `${id} is not in the panel`).not.toBeNull();
    }
  });

  it('counts what is set on the control, and keeps each set filter as a chip once it is shut', async () => {
    backend();
    await openHome();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    $('[data-testid="filter-owned"]').click();
    await tick();
    expect($('[data-testid="filter-toggle"]').textContent.trim()).toBe('Filters · 1');
    expect($('[data-testid="owned-filter-chip"]'), 'a chip beside the open panel').toBeNull();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    const chip = $('[data-testid="owned-filter-chip"]');
    expect(chip.textContent).toContain('in my library');
    chip.click();
    await tick();
    expect($('[data-testid="filter-toggle"]').textContent.trim()).toBe('Filters');
    expect($('[data-testid="home-mode"]').dataset.mode).toBe('shelves');
  });
});

describe('a filtered grid is read in the order the server says it used', () => {
  it('offers For you and Newest with the echoed order pressed, and asks for the other on a tap', async () => {
    const seen = backend({
      titles: (p) => ({
        items: [film(1, 'Heat')],
        total: 1,
        hidden: {},
        sort: p.get('sort') ?? 'for_you'
      })
    });
    await openHome();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    $('[data-testid="filter-owned"]').click();
    await tick();
    expect($('[data-testid="sort-for_you"]').getAttribute('aria-pressed')).toBe('true');
    expect($('[data-testid="sort-newest"]').getAttribute('aria-pressed')).toBe('false');
    $('[data-testid="sort-newest"]').click();
    await tick();
    expect(seen.some((u) => u.includes('/api/titles') && u.includes('sort=newest'))).toBe(true);
    expect($('[data-testid="sort-newest"]').getAttribute('aria-pressed')).toBe('true');
  });

  it('offers no order control where the server names none, or on a search', async () => {
    backend({ titles: () => ({ items: [film(1, 'Heat')], total: 1, hidden: {} }) });
    await openHome();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    $('[data-testid="filter-owned"]').click();
    await tick();
    expect($('[data-testid="home-mode"]').dataset.mode).toBe('grid');
    expect($('[aria-label="Order"]')).toBeNull();
    unmount(app);
    app = null;

    backend({ titles: () => ({ items: [film(1, 'Heat')], total: 1, hidden: {}, sort: 'for_you' }) });
    await openHome();
    await type('heat');
    expect($('[data-testid="home-mode"]').dataset.reason).toBe('search');
    expect($('[aria-label="Order"]'), 'a search is best match first').toBeNull();
  });
});

describe('an empty search names the other kind', () => {
  it('says "Found in Series: Broadchurch" and switches on a tap', async () => {
    backend({
      titles: (p) =>
        p.getAll('kind').includes('series')
          ? { items: [{ id: 9, kind: 'series', name: 'Broadchurch' }], total: 1, hidden: {} }
          : { items: [], total: 0, hidden: { series: 1 } }
    });
    await openHome();
    await type('broadchurch');
    expect($('[data-testid="found-elsewhere"]').textContent.trim()).toBe('Found in Series: Broadchurch');
    expect(target.textContent).not.toContain('Nothing in the library matches');
    $('[data-testid="found-elsewhere-switch"]').click();
    await tick();
    expect($('[data-testid="kind-series"]').getAttribute('aria-pressed')).toBe('true');
    expect($('.grid .card-wrap').textContent).toContain('Broadchurch');
  });

  it('says plainly that nothing matches when no kind has it', async () => {
    backend({ titles: () => ({ items: [], total: 0, hidden: { series: 0 } }) });
    await openHome();
    await type('zzzz');
    expect($('[data-testid="found-elsewhere"]')).toBeNull();
    expect($('.empty.card').textContent).toContain('Nothing matches.');
  });
});

describe('a search folds its looser matches', () => {
  it('shows the close matches and one button for the rest', async () => {
    backend({
      titles: () => ({
        items: [film(1, 'Up'), film(2, 'Up in the Air'), film(3, 'Superman'), film(4, 'Cupid')],
        total: 4,
        hidden: {}
      })
    });
    await openHome();
    await type('up');
    const names = () => [...target.querySelectorAll('.grid .card-wrap .name')].map((n) => n.textContent);
    expect(names()).toEqual(['Up', 'Up in the Air']);
    const more = $('[data-testid="weak-matches-toggle"]');
    expect(more.textContent.trim()).toBe('Show 2 looser matches');
    more.click();
    flushSync();
    expect(names()).toEqual(['Up', 'Up in the Air', 'Superman', 'Cupid']);
    expect($('[data-testid="weak-matches-head"]')).not.toBeNull();
  });
});

describe('a kind switch keeps the filters the new kind has', () => {
  it('keeps a shared genre and names one it had to clear', async () => {
    backend({
      facets: (kinds) =>
        kinds.includes('series') && !kinds.includes('movie')
          ? { genres: ['Drama'], decades: [] }
          : { genres: ['Drama', 'Musical'], decades: [] },
      titles: () => ({ items: [film(1, 'Heat')], total: 1, hidden: {} })
    });
    await openHome();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    const pick = async (value) => {
      const select = $('[data-testid="filter-genre"]');
      select.value = value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      await tick();
    };
    await pick('Drama');
    $('[data-testid="kind-series"]').click();
    await tick();
    expect($('[data-testid="filter-genre"]').value).toBe('Drama');
    expect($('[data-testid="kind-filter-note"]')).toBeNull();

    $('[data-testid="kind-movie"]').click();
    await tick();
    await pick('Musical');
    $('[data-testid="kind-series"]').click();
    await tick();
    expect($('[data-testid="filter-genre"]').value).toBe('');
    expect($('[data-testid="kind-filter-note"]').textContent).toBe('Musical cleared - no series match it.');
  });
});

describe('the document never scrolls under the shell', () => {
  it('keeps the shelves marker in flow, where the scroller holds it', async () => {
    // Absolutely placed, it escaped `main` (not a containing block) and made the whole document
    // scroll on a phone - the second vertical bar and the sideways scroll of U11.
    backend();
    await openHome();
    // jsdom applies no component styles, so the rule is read where it is written; the browser
    // half is `06-responsive.spec.js`'s "the document never scrolls under the shell".
    const marker = $('[data-testid="home-mode"]');
    expect(marker.classList.contains('sr-only')).toBe(true);
    const rule = PAGE_SOURCE.match(/\.sr-only\s*\{([^}]*)\}/)[1];
    expect(rule).not.toMatch(/position\s*:\s*(absolute|fixed)/);
    expect(rule).toMatch(/height\s*:\s*1px/);
  });
});
