/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The title card is a sheet, which pushes a history entry as it opens.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  beforeNavigate: () => {},
  afterNavigate: () => {}
}));

import HomePage from './+page.svelte';
import PAGE_SOURCE from './+page.svelte?raw';
import { homeKept } from '$lib/home.svelte.js';
import { session } from '$lib/session.svelte.js';
import { hideToast, toast } from '$lib/toast.svelte.js';
import { topbar } from '$lib/topbar.svelte.js';

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
  hideToast();
  if (app) unmount(app);
  app = null;
  Object.assign(session, { user: null, hasBundle: null, restartRequired: null });
  Object.assign(homeKept, { user: null, epoch: 0, kinds: null, payload: null, scrollY: 0 });
  vi.unstubAllGlobals();
  target.remove();
});

const countLine = () => target.querySelector('[data-testid="count-line"]').textContent;
const placeholder = () => target.querySelector('[data-testid="home-search"]').placeholder;
const card = () => target.querySelector('.empty.card');

describe('Home with no movie data', () => {
  it('tells a member in their own words and offers no admin door', async () => {
    await open({ user: MEMBER });
    expect(countLine()).toBe('No movie data yet');
    expect(placeholder()).toBe('Search');
    expect(card().textContent).toContain('There is no movie data yet');
    expect(card().textContent).not.toContain('bundle');
    expect(card().querySelector('a')).toBeNull();
  });

  it('tells an admin the same, and adds the door to Movie data', async () => {
    await open({ user: ADMIN });
    expect(countLine()).toBe('No movie data yet');
    expect(card().textContent).toContain('No movie data yet. Import it in Movie data');
    expect(card().textContent).not.toMatch(/bundle|artifact/);
    expect(card().querySelector('a[href="/admin/movie-data"]').textContent).toBe('Open Movie data');
  });

  it('never says there is no movie data while it waits for a restart', async () => {
    await open({ user: ADMIN, restartRequired: true });
    expect(countLine()).toBe('Waiting for a restart');
    expect(card().textContent).toContain('New movie data is waiting for a restart');
    expect(card().textContent).not.toContain('No movie data yet');
    expect(card().querySelector('a[href="/admin/movie-data"]').textContent).toBe('Open Movie data');
    unmount(app);

    await open({ user: MEMBER, restartRequired: true });
    expect(countLine()).toBe('Waiting for a restart');
    expect(card().textContent).toContain('waiting for a restart');
    expect(card().textContent).not.toContain('bundle');
    expect(card().querySelector('a')).toBeNull();
  });
});

function backend({
  titles = (/** @type {URLSearchParams} */ _params) => ({ items: [], total: 0, hidden: {} }),
  facets = (/** @type {string[]} */ _kinds) => ({ genres: [], decades: [] }),
  home = (/** @type {string[]} */ _kinds) => ({}),
  wish = () => ({ mine: [], others: [], copy_text: '' })
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
      else if (u.pathname === '/api/home') payload = home(u.searchParams.getAll('kind'));
      else if (u.pathname === '/api/wish') payload = wish();
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
const calls = (method, path) =>
  vi.mocked(globalThis.fetch).mock.calls.filter(
    ([url, init]) => (init?.method ?? 'GET') === method && String(url) === path
  );

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
    // It names the panel only while the panel exists.
    expect(toggle.hasAttribute('aria-controls')).toBe(false);
    toggle.click();
    flushSync();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(document.getElementById(toggle.getAttribute('aria-controls'))).toBe(
      $('[data-testid="filter-panel"]')
    );
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
    expect(chip.textContent).toContain('In my library');
    chip.click();
    await tick();
    expect($('[data-testid="filter-toggle"]').textContent.trim()).toBe('Filters');
    expect($('[data-testid="home-mode"]').dataset.mode).toBe('shelves');
  });
});

describe('the shelves (decision 527)', () => {
  it('count the library of the shown kind in the search field, and nothing of the kind not shown', async () => {
    backend({
      home: (kinds) => ({ kinds, library: { movie: 759, series: 127 }, shelves: [], shelves_total: 0 })
    });
    await openHome();
    expect(placeholder()).toBe('Search 759 films');
    expect($('[data-testid="count-line"]'), 'the count is the placeholder (decision 528)').toBeNull();
    $('[data-testid="kind-both"]').click();
    await tick();
    expect(placeholder()).toBe('Search 886 titles');
  });

  it('head each shelf one level under its kind on Both, and at the top on one kind', async () => {
    const section = (kind) => ({
      kind,
      heading: kind === 'movie' ? 'Films' : 'Series',
      title: 'Your top picks',
      why: 'For you',
      items: [{ title_id: kind === 'movie' ? 1 : 2, kind, name: 'T', seen: false }]
    });
    backend({
      home: (kinds) => ({
        kinds,
        library: {},
        shelves: [{ id: 'top_of_ledger', sections: kinds.map(section) }],
        shelves_total: 1
      })
    });
    await openHome();
    expect($('h2[data-testid="shelf-title"]')).not.toBeNull();
    $('[data-testid="kind-both"]').click();
    await tick();
    const regions = [...target.querySelectorAll('[data-testid="kind-region"]')];
    expect(regions.map((r) => r.querySelector('h2').textContent)).toEqual(['Films', 'Series']);
    for (const region of regions) {
      expect(region.querySelector('h3[data-testid="shelf-title"]').textContent).toBe('Your top picks');
      expect(region.querySelector('h2[data-testid="shelf-title"]')).toBeNull();
    }
  });
});

describe('the top of Home (decision 528)', () => {
  it('hands the kind switch to the shell and names the page only for a screen reader', async () => {
    topbar.host = true;
    try {
      backend();
      await openHome();
      expect(topbar.content, 'the shell was handed no row').not.toBeNull();
      expect($('[role="group"][aria-label="Kind"]'), 'the switch is drawn twice').toBeNull();
      const h1 = target.querySelectorAll('h1');
      expect(h1).toHaveLength(1);
      expect(h1[0].textContent).toBe('Home');
      expect(h1[0].classList.contains('sr-only')).toBe(true);
    } finally {
      topbar.host = false;
    }
  });

  const banner = {
    count: 2,
    named: [
      { title_id: 1, name: 'Heat', kind: 'movie' },
      { title_id: 2, name: 'Zodiac', kind: 'movie' }
    ],
    head_title_ids: [1, 2],
    copy: { headline: 'Rate 2 you watched', names: 'Heat · Zodiac' },
    cta: { label: 'Rate', route: '/rate?head=1&head=2' }
  };

  it('shows what waits for a verdict as one row: the count, the names and one Rate link', async () => {
    backend({ home: (kinds) => ({ kinds, library: {}, shelves: [], shelves_total: 0, banner }) });
    await openHome();
    const row = $('[data-testid="pending-verdicts"]');
    expect(row.querySelector('[data-testid="pending-verdicts-copy"]').textContent).toBe(
      'Rate 2 you watched'
    );
    expect(row.querySelector('[data-testid="pending-verdicts-names"]').textContent).toBe(
      'Heat · Zodiac'
    );
    // One 28 px thumb (decision 554), not the two small posters it had.
    expect(row.querySelectorAll('[data-testid="rate-poster"]')).toHaveLength(1);
    const links = row.querySelectorAll('a');
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe('/rate?head=1&head=2');
    expect(links[0].textContent.trim()).toBe('Rate');
  });

  it('puts the row away until tomorrow with its x, and Undo brings it back (decision 554)', async () => {
    let away = false;
    backend({
      home: (kinds) => ({ kinds, library: {}, shelves: [], shelves_total: 0, banner: away ? null : banner })
    });
    await openHome();
    away = true;
    $('[data-testid="pending-verdicts"] [aria-label="Hide until tomorrow"]').click();
    flushSync();
    expect($('[data-testid="pending-verdicts"]'), 'gone at once').toBeNull();
    await tick();
    expect(calls('PUT', '/api/home/notices/pending')).toHaveLength(1);
    expect([toast.message, toast.actionLabel]).toEqual(['Hidden until tomorrow', 'Undo']);

    away = false;
    toast.action();
    await tick();
    expect(calls('DELETE', '/api/home/notices/pending')).toHaveLength(1);
    expect($('[data-testid="pending-verdicts"]')).not.toBeNull();
  });
});

describe('before the set-up (decision 550)', () => {
  const notice = {
    headline: 'Set up your ladder.',
    why: 'Rating is one tap now, on seven steps of your own. The set-up takes about a minute',
    cta: { label: 'Set up my ladder', route: '/rate/setup' }
  };
  const section = {
    kind: 'movie',
    heading: 'Films',
    title: 'Your top picks',
    why: 'For you',
    items: [{ title_id: 1, kind: 'movie', name: 'Heat', seen: false }]
  };

  it('puts the notice over the shelves, with the one way into the set-up', async () => {
    backend({
      home: (kinds) => ({
        kinds,
        library: {},
        shelves: [{ id: 'top_of_ledger', sections: [section] }],
        shelves_total: 1,
        setup_notice: notice
      })
    });
    await openHome();
    const card = $('[data-testid="home-setup-notice"]');
    expect(card.querySelector('h2').textContent).toBe('Set up your ladder.');
    expect(card.querySelector('.line').textContent).toBe(notice.why);
    const link = card.querySelector('a');
    expect(link.getAttribute('href')).toBe('/rate/setup');
    expect(link.textContent).toBe('Set up my ladder');
    expect($('[data-testid="shelf-title"]').textContent).toBe('Your top picks');

    $('[data-testid="home-setup-notice"] [aria-label="Hide until tomorrow"]').click();
    await tick();
    expect(calls('PUT', '/api/home/notices/setup')).toHaveLength(1);
    expect(toast.message).toBe('Hidden until tomorrow');
    expect($('[data-testid="shelf-title"]').textContent, 'the shelves stay').toBe('Your top picks');
  });

  it('asks for no ratings in an empty shelf list while the notice stands, and is gone after it', async () => {
    let payload = { library: {}, shelves: [], shelves_total: 0, setup_notice: notice };
    backend({ home: (kinds) => ({ kinds, ...payload }) });
    await openHome();
    expect($('[data-testid="shelves-empty"]'), 'Rate is closed until the set-up').toBeNull();

    unmount(app);
    payload = { library: {}, shelves: [], shelves_total: 0, setup_notice: null };
    Object.assign(homeKept, { user: null, payload: null });
    await openHome();
    expect($('[data-testid="home-setup-notice"]')).toBeNull();
    expect($('[data-testid="shelves-empty"]')).not.toBeNull();
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
    // A search is best match first whatever the filtered grid was put in.
    await type('heat');
    const last = seen.filter((u) => u.includes('/api/titles')).at(-1);
    expect(last).toContain('q=heat');
    expect(last).not.toContain('sort=');
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

  it('offers no For you before the member has an order of their own, and says when it comes', async () => {
    backend({
      titles: () => ({
        items: [film(1, 'Heat')], total: 1, hidden: {}, sort: 'newest', for_you_available: false
      })
    });
    await openHome();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    $('[data-testid="filter-owned"]').click();
    await tick();
    expect($('[aria-label="Order"]')).toBeNull();
    expect($('[data-testid="sort-waiting"]').textContent.trim()).toBe(
      'Newest first. Your own order arrives once your ratings rank these.'
    );
  });
});

describe("a grid in the member's own order with both kinds", () => {
  it('heads each kind where it starts and says films come first', async () => {
    const series = { id: 3, kind: 'series', name: 'Severance', year: 2022 };
    backend({
      titles: (p) => ({
        items: p.getAll('kind').length > 1 ? [film(1, 'Heat'), film(2, 'Up'), series] : [film(1, 'Heat')],
        total: 3,
        hidden: {},
        sort: 'for_you',
        for_you_available: true
      })
    });
    await openHome();
    $('[data-testid="filter-toggle"]').click();
    flushSync();
    $('[data-testid="filter-owned"]').click();
    await tick();
    expect($('[data-testid="grid-kind-movie"]'), 'one kind, nothing to divide').toBeNull();
    expect($('[data-testid="grid-partition"]')).toBeNull();

    $('[data-testid="kind-both"]').click();
    await tick();
    expect($('[data-testid="grid-partition"]').textContent.trim()).toBe('Films first, then series.');
    const grid = [...$('.grid').children].map((el) =>
      el.matches('h2') ? `# ${el.textContent.trim()}` : el.querySelector('.name')?.textContent
    );
    expect(grid).toEqual(['# Films', 'Heat', 'Up', '# Series', 'Severance']);
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
    expect($('[data-testid="kind-filter-note"]').textContent).toBe('Musical cleared — no series match it.');

    // The note belongs to the kind switch; the next list asked for clears it.
    await type('heat');
    expect($('[data-testid="kind-filter-note"]'), 'a search after the switch').toBeNull();
  });
});

describe('a list for another question never stands in for the answer (decision 530)', () => {
  it('shows no catalog under a first search letter, and dims a refined search until it lands', async () => {
    backend({
      titles: (p) => ({ items: p.get('q') ? [film(2, 'Heat')] : [film(1, 'Up')], total: 1, hidden: {} })
    });
    await openHome();
    const box = $('[data-testid="home-search"]');
    const letter = (text) => {
      box.value = text;
      box.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
    };
    const names = () => [...target.querySelectorAll('.card-wrap .name')].map((n) => n.textContent);

    letter('h');
    expect(names(), 'the catalog under the first letter').toEqual([]);
    await tick(80);
    await tick(80);
    expect(names()).toEqual(['Heat']);

    letter('he');
    expect(names()).toEqual(['Heat']);
    expect($('.card-wrap').closest('[aria-busy]').getAttribute('aria-busy')).toBe('true');
    await tick(80);
    await tick(80);
    expect($('.card-wrap').closest('[aria-busy]').getAttribute('aria-busy')).toBe('false');
  });
});

describe('Home keeps its place across tabs (decision 530)', () => {
  it('comes back to the last shelves and kind at once, and re-reads them quietly', async () => {
    const section = {
      kind: 'series',
      heading: 'Series',
      title: 'Your top picks',
      why: 'For you',
      items: [{ title_id: 2, kind: 'series', name: 'Dark', seen: false }]
    };
    backend({
      home: (kinds) => ({
        kinds,
        library: {},
        shelves: kinds.includes('series') ? [{ id: 'top_of_ledger', sections: [section] }] : [],
        shelves_total: 1
      })
    });
    await openHome();
    $('[data-testid="kind-series"]').click();
    await tick();
    unmount(app);

    // The re-read never answers here: whatever shows is what Home kept.
    const reads = vi.fn((url) => (String(url).includes('/api/home') ? new Promise(() => {}) : route(url)));
    vi.stubGlobal('fetch', reads);
    app = mount(HomePage, { target });
    await tick();
    expect($('[data-testid="kind-series"]').getAttribute('aria-pressed')).toBe('true');
    expect($('[data-testid="shelves-loading"]'), 'a cold reload').toBeNull();
    expect($('[data-testid="shelf-title"]').textContent).toBe('Your top picks');
    expect($('[data-testid="shelves"]').getAttribute('aria-busy')).toBe('false');
    expect(reads.mock.calls.some(([url]) => String(url).includes('/api/home?kind=series'))).toBe(true);
  });
});

describe('a wanted film arrives, and the household wish list (decision 544)', () => {
  const worth = {
    id: 'worth_getting',
    sections: [{
      kind: 'movie', heading: 'Films', title: 'Worth getting',
      why: 'Not in the library yet, close to what you love',
      items: [{ title_id: 7, kind: 'movie', name: 'Collateral', seen: false,
                like: { title_id: 9, name: 'Heat', terms: [] } }]
    }]
  };

  it('says the film is here with its Play, and dismissing asks the server and re-reads Home', async () => {
    const arrived = [{
      title_id: 2, name: 'Prisoners', poster_path: null, since: '2026-09-12T10:00:00Z',
      play_url: 'http://jellyfin.test/web/#/details?id=jf-2'
    }];
    const seen = backend({ home: (kinds) => ({ kinds, library: {}, shelves: [], arrived }) });
    await openHome();
    const banner = $('[data-testid="home-arrived"]');
    expect(banner.querySelector('.headline').textContent).toBe('Prisoners is here');
    expect(banner.textContent).toContain("On your wish list since 12 Sep. It's in the library now.");
    expect(banner.querySelector('a').getAttribute('href')).toBe(arrived[0].play_url);

    const reads = seen.filter((u) => u.startsWith('/api/home')).length;
    banner.querySelector('[aria-label="Dismiss Prisoners"]').click();
    await tick();
    expect(calls('POST', '/api/wish/2/dismiss')).toHaveLength(1);
    expect($('[data-testid="home-arrived"]')).toBeNull();
    expect(seen.filter((u) => u.startsWith('/api/home')).length).toBe(reads + 1);

    // For good, with Undo on its toast (decision 554): the want comes back with its own date.
    expect([toast.message, toast.actionLabel]).toEqual(['Removed', 'Undo']);
    toast.action();
    await tick();
    const [restore] = calls('POST', '/api/wish/2/restore');
    expect(JSON.parse(String(restore[1].body))).toEqual({ since: arrived[0].since });
    expect($('[data-testid="home-arrived"]')).not.toBeNull();
  });

  it('keeps the row under Worth getting and opens the list as yours, then others\'', async () => {
    const patrick = { id: 1, name: 'Patrick', role: 'admin' };
    const sam = { id: 6, name: 'Sam' };
    const listing = {
      mine: [{ title_id: 3, kind: 'movie', name: 'Princess Mononoke', year: 1997,
               since: '2026-09-14T08:00:00Z', mine: true, likely: null, likely_too: ['Sam'],
               wanters: [{ id: 5, name: 'Jenny' }, patrick], link: null }],
      others: [{ title_id: 4, kind: 'movie', name: 'Wolf Children', year: 2012,
                 since: '2026-09-30T08:00:00Z', mine: false, likely: 'likely', likely_too: [],
                 wanters: [patrick, sam], link: null }],
      copy_text: 'Princess Mononoke (1997)\nWolf Children (2012)'
    };
    backend({
      home: (kinds) => ({ kinds, library: {}, shelves: [worth], shelves_total: 1,
                          wish: { wanted: 4, both: 1, members: 2 } }),
      wish: () => listing
    });
    const clipboard = { writeText: vi.fn(async () => {}) };
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: clipboard });
    await openHome();

    const row = $('[data-testid="home-wish-row"]');
    expect(row.textContent).toContain('Wish list');
    expect(row.textContent).toContain('4 wanted, 1 by both of you');
    expect(row.previousElementSibling.dataset.shelf, 'directly under Worth getting').toBe('worth_getting');
    row.querySelector('[data-testid="home-wish-open"]').click();
    await tick();

    const sheet = $('[data-testid="wish-list-sheet"]');
    expect([...sheet.querySelectorAll('h3')].map((h) => h.textContent)).toEqual([
      'You want', 'Others want'
    ]);
    const [mine, theirs] = [...sheet.querySelectorAll('[data-testid="wish-item"]')];
    expect(mine.textContent).toContain('1997 · since 14 Sep');
    expect(mine.textContent).toContain('Sam would likely enjoy it too.');
    expect(mine.querySelector('.faces').getAttribute('aria-label')).toBe('Also wanted by Patrick');
    expect(theirs.textContent).toContain('2012 · since 30 Sep · you: likely too');
    expect(theirs.querySelector('.faces').getAttribute('aria-label')).toBe('Wanted by Patrick and Sam');
    expect(theirs.querySelectorAll('.faces .avatar')).toHaveLength(2);

    mine.querySelector('[aria-label="Remove Princess Mononoke"]').click();
    await tick();
    expect(calls('DELETE', '/api/wish/3')).toHaveLength(1);
    theirs.querySelector('button.metoo').click();
    await tick();
    const [put] = calls('PUT', '/api/wish/4');
    expect(JSON.parse(String(put[1].body))).toEqual({ state: 'want' });

    sheet.querySelector('[data-testid="wish-copy"]').click();
    await tick();
    expect(clipboard.writeText).toHaveBeenCalledWith(listing.copy_text);
    Reflect.deleteProperty(navigator, 'clipboard');
  });

  it('shows no row while there is no Worth getting shelf and nothing is wanted', async () => {
    backend({ home: (kinds) => ({ kinds, library: {}, shelves: [], wish: { wanted: 0, both: 0 } }) });
    await openHome();
    expect($('[data-testid="home-wish-row"]')).toBeNull();
    unmount(app);
    app = null;

    backend({ home: (kinds) => ({ kinds, library: {}, shelves: [], wish: { wanted: 2, both: 0 } }) });
    await openHome();
    expect($('[data-testid="home-wish-row"]').textContent).toContain('2 wanted');
  });

  it('puts the row away until tomorrow, and the server says so on the next read', async () => {
    let hidden = false;
    backend({
      home: (kinds) => ({ kinds, library: {}, shelves: [worth], shelves_total: 1,
                          wish: { wanted: 2, both: 0, members: 2, hidden } })
    });
    await openHome();
    hidden = true;
    $('[data-testid="home-wish-row"] [aria-label="Hide until tomorrow"]').click();
    flushSync();
    expect($('[data-testid="home-wish-row"]'), 'gone at once').toBeNull();
    await tick();
    expect(calls('PUT', '/api/home/notices/wish_list')).toHaveLength(1);
    expect(toast.message).toBe('Hidden until tomorrow');
    expect($('[data-testid="home-wish-row"]')).toBeNull();
    expect($('[data-shelf="worth_getting"]'), 'Worth getting stays').not.toBeNull();
  });
});

describe('the document never scrolls under the shell', () => {
  it('keeps the shelves marker in flow, where the scroller holds it', async () => {
    // Absolutely placed, it escaped `main` and made the whole document scroll on a phone.
    backend();
    await openHome();
    // jsdom applies no component styles, so the rule is read from the source.
    const marker = $('[data-testid="home-mode"]');
    expect(marker.classList.contains('sr-only')).toBe(true);
    const rule = PAGE_SOURCE.match(/\.sr-only\s*\{([^}]*)\}/)[1];
    expect(rule).not.toMatch(/position\s*:\s*(absolute|fixed)/);
    expect(rule).toMatch(/height\s*:\s*1px/);
  });
});
