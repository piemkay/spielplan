/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  get: vi.fn(),
  api: vi.fn()
}));
// See all is a sheet, which pushes a history entry as it opens.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import { api, get } from '$lib/api.js';
import { session } from '$lib/session.svelte.js';
import ShelfRow from './ShelfRow.svelte';

const COLD_NOTE = '[data-testid="shelf-cold-note"]';

/** One shelf card in `GET /api/home`'s shape; warm by default, since the badge is the exception. */
const card = (overrides = {}) => ({
  title_id: 1,
  kind: 'movie',
  name: 'Paddington',
  year: 2014,
  runtime_min: 95,
  poster_path: null,
  placement: 'warm',
  item_n: 480,
  e_source: 'backbone',
  seen: false,
  rank: 1,
  tier: null,
  model: null,
  ...overrides
});

const section = (items) => ({
  kind: 'movie',
  title: 'Because you liked Paddington',
  heading: 'Films',
  why: 'warm comedies, gentle pacing',
  caption: null,
  items
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  target.remove();
});

function render(items) {
  // `onSelect` is a required prop, and omitting it fails `npm run check`.
  const app = mount(ShelfRow, {
    target,
    props: {
      section: section(items),
      shelfId: 'because-you',
      onSelect: () => {}
    }
  });
  flushSync();
  return app;
}

describe('the cold-placement note', () => {
  it('is absent from a row every card of which has crowd data behind it', () => {
    const app = render([card(), card({ title_id: 2, name: 'Arrival' })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    unmount(app);
  });

  it('appears once when a card on the row was placed with no crowd ratings behind it', () => {
    const app = render([
      card(),
      card({ title_id: 2, name: 'Tampopo', e_source: 'cold_tower', item_n: 0 })
    ]);
    const notes = target.querySelectorAll(COLD_NOTE);
    expect(notes).toHaveLength(1);
    expect(notes[0].textContent).toContain('no outside ratings yet');
    expect(notes[0].textContent).not.toContain('Cold Tower');
    expect(notes[0].className).toContain('footnote');
    unmount(app);
  });

  it('says it once for the row and not once per card', () => {
    const app = render([
      card({ title_id: 1, e_source: 'cold_tower', item_n: 0 }),
      card({ title_id: 2, e_source: 'cold_tower', item_n: 0 }),
      card({ title_id: 3, e_source: 'cold_tower', item_n: 0 })
    ]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(1);
    unmount(app);
  });

  it('is absent when the Cold Tower placed a title the crowd has rated', () => {
    // The evaluation holdout serves crowd-rated rows from the Cold Tower.
    const app = render([card({ e_source: 'cold_tower', placement: 'cold_tower', item_n: 192061 })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    expect(target.querySelector('[data-testid="new-badge"]')).toBeNull();
    unmount(app);
  });

  it('is not said a second time on the shelf whose why-line already says it', () => {
    const cold = [card({ title_id: 1, e_source: 'cold_tower', item_n: 0 })];
    const app = mount(ShelfRow, {
      target,
      props: { section: section(cold), shelfId: 'new_in_library', onSelect: () => {} }
    });
    flushSync();
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    expect(target.querySelector('[data-testid="new-badge"]').textContent).toBe('New');
    unmount(app);
  });

  it('reads the same fields the badge does, so the row and the card cannot disagree', () => {
    // `e_source` decides where present: a crowd-rated title can still be stamped `cold_tower`.
    const app = render([card({ placement: 'cold_tower', item_n: 55, e_source: 'backbone' })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    unmount(app);

    // And without `e_source` the placement stamp is the fallback, as it is on the card.
    const fallback = render([card({ placement: 'cold_tower', item_n: null, e_source: null })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(1);
    unmount(fallback);
  });
});

function renderSection(overrides) {
  const app = mount(ShelfRow, {
    target,
    props: { section: { ...section([card()]), ...overrides }, shelfId: 'x', onSelect: () => {} }
  });
  flushSync();
  return app;
}

describe('what a row says about itself', () => {
  it('is a heading and one reason line, with no chips of terms, kinds or counts', () => {
    const app = renderSection({
      why_terms: [{ term: 'era.wwii', facet: 'era', tier: 'extracted', label: 'World War II' }]
    });
    const header = target.querySelector('header');
    expect([...header.children].map((el) => el.dataset.testid)).toEqual(['shelf-title', 'shelf-why']);
    expect(target.textContent).not.toContain('World War II');
    expect(target.textContent).not.toContain('FILMS');
    unmount(app);
  });

  // Tier letters live on Rank (decision 527).
  it('prints no rank number and no tier letter on a card, whatever the payload carries', () => {
    const app = render([
      card({ title_id: 1, name: 'Heat', tier: 'S', rank: 1 }),
      card({ title_id: 2, name: 'Up', tier: 'B', rank: 2, seen: true })
    ]);
    const cards = [...target.querySelectorAll('[data-testid="shelf-card"]')];
    expect(cards.map((c) => c.querySelector('.meta').textContent.replace(/\s+/g, ' ').trim())).toEqual([
      'Heat 2014 · 1h 35m',
      'Up 2014 · 1h 35m'
    ]);
    for (const c of cards) expect(c.textContent).not.toMatch(/\b(S|B)\b/);
    expect(cards[1].querySelector('[data-testid="seen-badge"]')).not.toBeNull();
    unmount(app);
  });

  it('prints the model numbers only when the payload carries them', () => {
    const off = renderSection({});
    expect(target.querySelector('[data-testid="shelf-numbers"]')).toBeNull();
    unmount(off);
    const on = renderSection({ why_numbers: { beta: 0.62, gate_k: 10, beta_fitted: true } });
    expect(target.querySelector('[data-testid="shelf-numbers"]').textContent).toBe(
      'β 0.62 · gate k 10'
    );
    unmount(on);
  });
});

describe('a row pages itself and leaves the wheel to the page (decision 528)', () => {
  const films = () => [1, 2, 3, 4, 5].map((id) => card({ title_id: id, name: `Film ${id}` }));

  /** A row wider than its box, which jsdom does not lay out. */
  function overflowing(app) {
    const row = target.querySelector('[data-testid="shelf-items"]');
    Object.defineProperty(row, 'scrollWidth', { configurable: true, value: 1000 });
    Object.defineProperty(row, 'clientWidth', { configurable: true, value: 300 });
    row.dispatchEvent(new Event('scroll'));
    flushSync();
    return { app, row };
  }

  it('never takes a vertical wheel from the page, even over a row that scrolls sideways', () => {
    const { app, row } = overflowing(render(films()));
    const wheel = new WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true });
    expect(row.dispatchEvent(wheel), 'the row cancelled the page scroll').toBe(true);
    expect(row.scrollLeft).toBe(0);
    unmount(app);
  });

  it('pages with Previous and Next, each disabled at its own end', () => {
    const { app, row } = overflowing(render(films()));
    const prev = target.querySelector('[aria-label="Previous page"]');
    const next = target.querySelector('[aria-label="Next page"]');
    expect(prev.disabled).toBe(true);
    expect(next.disabled).toBe(false);
    next.click();
    flushSync();
    expect(row.scrollLeft).toBe(240);
    expect(prev.disabled).toBe(false);
    unmount(app);
  });

  it('opens every poster of the shelf in a sheet from See all, each one a title card', () => {
    const onSelect = vi.fn();
    const app = mount(ShelfRow, {
      target,
      props: { section: section(films()), shelfId: 'because-you', onSelect }
    });
    flushSync();
    expect(target.querySelector('[role="dialog"]')).toBeNull();
    target.querySelector('[data-testid="shelf-see-all"]').click();
    flushSync();
    const sheet = target.querySelector('[role="dialog"]');
    expect(sheet.getAttribute('aria-label')).toBe('Because you liked Paddington');
    const names = [...sheet.querySelectorAll('.card-wrap .name')].map((n) => n.textContent);
    expect(names).toEqual(['Film 1', 'Film 2', 'Film 3', 'Film 4', 'Film 5']);
    sheet.querySelector('.card-wrap').click();
    // The poster as drawn travels with the id, so the card opens on it (decision 530).
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 1, name: 'Film 1' }));
    unmount(app);
  });
});

describe('Worth getting (decision 544)', () => {
  const heat = { title_id: 9, name: 'Heat', terms: ['night city', 'cat and mouse'] };
  const worthSection = {
    ...section([
      card({ title_id: 1, name: 'Collateral', like: heat }),
      card({ title_id: 2, name: 'Thief', like: heat })
    ]),
    title: 'Worth getting',
    why: 'Not in the library yet, close to what you love'
  };
  const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  };

  afterEach(() => vi.mocked(get).mockReset());

  function renderWorth() {
    const app = mount(ShelfRow, {
      target,
      props: { section: worthSection, shelfId: 'worth_getting', onSelect: () => {} }
    });
    flushSync();
    return app;
  }

  it('draws larger cards, each naming the liked film it is like', () => {
    const app = renderWorth();
    expect(target.querySelector('[data-testid="shelf-items"]').classList.contains('worth')).toBe(true);
    const likes = [...target.querySelectorAll('[data-testid="like-line"]')].map((el) => el.textContent);
    expect(likes).toEqual(['Like Heat', 'Like Heat']);
    unmount(app);
  });

  it('opens the whole list from See all, for you or whomever you pick, with Want on every row', async () => {
    session.user = { id: 1, name: 'Patrick', role: 'admin', must_change_password: false };
    const wanted = new Set([1]);
    const row = (title_id, name, like = heat) => ({
      title_id, kind: 'movie', name, year: 2004, runtime_min: 120, like, wanted: wanted.has(title_id)
    });
    const members = [
      { id: 1, name: 'Patrick', role: 'admin', pickable: true, reason: null },
      { id: 2, name: 'Jenny', role: 'member', pickable: true, reason: null },
      { id: 3, name: 'Sam', role: 'member', pickable: false, reason: 'Not enough films rated yet' }
    ];
    vi.mocked(get).mockImplementation(async (path) => {
      if (path.endsWith('for=everyone')) {
        return { kind: 'movie', for: 'everyone', members, items: [row(3, 'Ronin', null)] };
      }
      if (path.endsWith('for=2')) {
        return { kind: 'movie', for: { id: 2, name: 'Jenny' }, members, items: [row(4, 'Heat II')] };
      }
      return { kind: 'movie', for: { id: 1, name: 'Patrick' }, members,
               items: [row(1, 'Collateral'), row(2, 'Thief')] };
    });
    vi.mocked(api).mockImplementation(async (path) => {
      wanted.add(Number(path.split('/').pop()));
      return { state: 'want' };
    });
    const app = renderWorth();
    target.querySelector('[data-testid="shelf-see-all"]').click();
    await settle();

    const sheet = target.querySelector('[data-testid="worth-getting-sheet"]');
    expect(vi.mocked(get)).toHaveBeenCalledWith('/home/worth-getting?kind=movie');
    expect(sheet.closest('[role="dialog"]').getAttribute('aria-label')).toBe('Worth getting');
    expect([...sheet.querySelectorAll('.reason')].map((el) => el.textContent)).toEqual([
      'Like Heat · night city, cat and mouse',
      'Like Heat · night city, cat and mouse'
    ]);
    // A row with no art names the title on its panel; one with art leaves the name beside it.
    const panels = () => [...sheet.querySelectorAll('[data-testid="rate-poster"]')];
    expect(panels().map((p) => p.querySelector('.name'))).toEqual([null, null]);
    panels()[0].querySelector('img').dispatchEvent(new Event('error'));
    flushSync();
    expect(panels().map((p) => p.querySelector('.name')?.textContent ?? null)).toEqual(['Collateral', null]);
    const wants = () => [...sheet.querySelectorAll('[data-testid="worth-getting-want"]')];
    expect(wants().map((b) => [b.textContent.trim(), b.getAttribute('aria-pressed')])).toEqual([
      ['Wanted', 'true'],
      ['Want', 'false']
    ]);

    const reads = vi.mocked(get).mock.calls.length;
    wants()[1].click();
    await settle();
    expect(vi.mocked(api)).toHaveBeenCalledWith('/wish/2', { method: 'PUT', body: { state: 'want' } });
    // A wish written anywhere, a card over the sheet included, re-reads the list.
    expect(vi.mocked(get).mock.calls.slice(reads)).toEqual([['/home/worth-getting?kind=movie']]);
    expect(wants()[1].getAttribute('aria-pressed')).toBe('true');

    const seat = target.querySelector('[data-testid="worth-getting-for"]');
    expect(seat.textContent).toContain('For you');
    seat.click();
    await settle();
    // You first, the others, then Everyone; a member whose ratings do not open it is dimmed with why.
    const options = () => [...target.querySelectorAll('[data-testid="worth-getting-picker"] [role="radio"]')];
    expect(
      options().map((o) => [
        o.querySelector('.name').textContent,
        o.getAttribute('aria-disabled'),
        o.getAttribute('aria-checked')
      ])
    ).toEqual([
      ['You', 'false', 'true'],
      ['Jenny', 'false', 'false'],
      ['Sam', 'true', 'false'],
      ['Everyone', 'false', 'false']
    ]);
    expect(options()[2].textContent).toContain('Not enough films rated yet');
    expect(options()[3].textContent).toContain('You and Jenny');

    const before = vi.mocked(get).mock.calls.length;
    options()[2].click();
    await settle();
    expect(vi.mocked(get).mock.calls.length, 'Sam cannot be picked').toBe(before);

    options()[3].click();
    await settle();
    expect(vi.mocked(get)).toHaveBeenLastCalledWith('/home/worth-getting?kind=movie&for=everyone');
    expect(seat.textContent).toContain('For everyone');
    const reasons = () => [...sheet.querySelectorAll('.reason')].map((el) => el.textContent);
    expect(reasons()).toEqual([]);
    expect(sheet.querySelector(':scope > p.footnote').textContent).toContain('Seen one already?');

    options()[1].click();
    await settle();
    expect(vi.mocked(get)).toHaveBeenLastCalledWith('/home/worth-getting?kind=movie&for=2');
    expect(seat.textContent).toContain('For Jenny');
    expect(target.querySelector('.lede').textContent).toContain('the ones Jenny rates highest');
    expect(reasons(), "the like line stays the viewer's own").toEqual([
      'Like Heat · night city, cat and mouse'
    ]);
    expect(sheet.querySelector(':scope > p.footnote'), 'rating it leaves only your own list').toBeNull();
    unmount(app);
    session.user = null;
  });
});
