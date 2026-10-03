/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ goto: null, before: null }));
vi.mock('$app/navigation', () => {
  nav.goto = vi.fn();
  return {
    afterNavigate: vi.fn(),
    beforeNavigate: (fn) => (nav.before = fn),
    goto: nav.goto,
    pushState: vi.fn()
  };
});

import SetupPage from './+page.svelte';

const HINT = "Films you've watched first, then popular ones. Tap the ones you remember well.";
const STEPS = [
  { tier: 5, word: 'All-time favourite', hint: HINT },
  { tier: 4, word: 'Excellent', hint: HINT }
];
const film = (id, name, seen = false) => ({ id, name, year: 2000, poster_path: null, seen });
const FILMS = [film(1, 'Heat', true), film(2, 'Zodiac'), film(3, 'Sicario')];

let target;
let app;
let replies;
let posted;

/** The window's width, read by the page as it mounts. */
function width(px) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true });
}

beforeEach(() => {
  width(390);
  posted = [];
  replies = {
    '/api/ladder/setup': () => ({ done: false, earlier_ratings: 4, steps: STEPS }),
    '/api/ladder/setup/films': (params) => {
      const out = (params.get('exclude') ?? '').split(',');
      return { films: FILMS.filter((f) => !out.includes(String(f.id))), more: false };
    },
    '/api/ladder/setup/finish': () => ({
      done: true,
      placed: 2,
      tiers: [
        { tier: 5, word: 'All-time favourite', count: 1, first: { id: 1, name: 'Heat', poster_path: null } },
        { tier: 4, word: 'Excellent', count: 1, first: { id: 2, name: 'Zodiac', poster_path: null } },
        { tier: 3, word: 'Very good', count: 0, first: null }
      ],
      earlier_ratings: 4,
      rated_before: 3
    })
  };
  vi.stubGlobal(
    'fetch',
    vi.fn((url, init) => {
      const u = new URL(String(url), 'http://localhost');
      if (init?.body) posted.push(JSON.parse(init.body));
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: async () => JSON.stringify(replies[u.pathname]?.(u.searchParams) ?? {})
      });
    })
  );
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 6; i++) await tick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function open() {
  app = mount(SetupPage, { target });
  await settle();
}

const $ = (id) => /** @type {HTMLElement} */ (document.querySelector(`[data-testid="${id}"]`));
const all = (id) => [...document.querySelectorAll(`[data-testid="${id}"]`)];
const cell = (name) => all('setup-film').find((el) => el.getAttribute('aria-label') === name);

async function click(el) {
  el.click();
  await settle();
}

/** Types into the search, opening it first, past the field's 200 ms pause. */
async function type(text) {
  if (!$('setup-search')) await click($('setup-find'));
  const field = /** @type {HTMLInputElement} */ ($('setup-search'));
  field.value = text;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 250));
  await settle();
}

describe('the set-up, step by step', () => {
  it('names the step by its word, never a letter, over a grid of its films', async () => {
    await open();
    expect(target.textContent).toContain('Step 1 of 2');
    expect($('setup-step').textContent).toBe('All-time favourite');
    expect(target.textContent).toContain(HINT);
    expect($('setup-note'), 'no second line for a member with films of their own').toBeNull();
    expect(target.textContent).not.toMatch(/\bS\b|A\+/);
    expect(all('setup-film').map((el) => el.getAttribute('aria-label'))).toEqual(['Heat', 'Zodiac', 'Sicario']);
    expect($('setup-find').textContent.trim()).toBe('A film you have seen');
    expect($('setup-search'), 'the field opens in the top row, on a tap').toBeNull();
    expect($('setup-end').textContent.trim()).toBe(
      "That's the end of the list. Search finds any other film."
    );
    expect($('setup-more')).toBeNull();
    expect($('setup-strip'), 'the first step has no step above it').toBeNull();
    expect($('setup-undo').hasAttribute('disabled')).toBe(true);
  });

  it("puts the step's note under its hint for a member who has watched nothing yet", async () => {
    const note = "Jellyfin has nothing you've watched yet, so you start with theirs.";
    replies['/api/ladder/setup'] = () => ({
      done: false,
      earlier_ratings: 0,
      steps: STEPS.map((step) => ({ ...step, note }))
    });
    await open();
    expect($('setup-note').textContent).toBe(note);
    expect($('setup-note').previousElementSibling.textContent).toBe(HINT);
  });

  it('marks the films Jellyfin says were watched', async () => {
    await open();
    expect(cell('Heat').querySelector('[data-testid="setup-watched"]')).not.toBeNull();
    expect(cell('Heat').getAttribute('aria-describedby')).toBe('setup-watched');
    expect(document.getElementById('setup-watched').textContent).toBe('Watched');
    expect(cell('Zodiac').querySelector('[data-testid="setup-watched"]')).toBeNull();
  });

  it('toggles a pick, says None for an empty step, and shows the step above as the strip', async () => {
    await open();
    expect($('setup-next').textContent.trim()).toBe('None for All-time favourite');
    expect($('setup-undo').parentElement.contains($('setup-next')), 'on a phone it is in the dock').toBe(false);
    await click(cell('Heat'));
    expect(cell('Heat').getAttribute('aria-pressed')).toBe('true');
    expect($('setup-next').textContent.trim()).toBe('Next');

    await click($('setup-next'));
    expect(target.textContent).toContain('Step 2 of 2');
    expect($('setup-step').textContent).toBe('Excellent');
    expect($('setup-strip').textContent).toContain('Not quite these · All-time favourite');
    expect($('setup-strip').querySelectorAll('[data-testid="rate-poster"]')).toHaveLength(1);
    expect(cell('Heat'), "an earlier step's pick leaves this list").toBeUndefined();
    expect($('setup-undo').hasAttribute('disabled')).toBe(false);
  });

  it('finds a film by name, with its year, and a hit joins this step first and picked', async () => {
    replies['/api/titles'] = () => ({
      items: [{ id: 7, kind: 'movie', name: 'Prisoners', year: 2013, poster_path: null, seen_state: 'unseen' }]
    });
    await open();
    await type('Pris');
    const found = $('setup-hit');
    expect(found.getAttribute('aria-label')).toBe('Prisoners, 2013');
    expect(found.textContent).toContain('2013');
    await click(found);
    expect($('setup-search'), 'a hit ends the search').toBeNull();
    expect(document.activeElement).toBe($('setup-find'));
    expect(all('setup-film')[0].getAttribute('aria-label')).toBe('Prisoners');
    expect(all('setup-film')[0].getAttribute('aria-pressed')).toBe('true');
  });

  it('searches in the top row, its hits over the step and no dock, and Cancel brings the step back', async () => {
    replies['/api/titles'] = () => ({
      items: [{ id: 7, kind: 'movie', name: 'Prisoners', year: 2013, poster_path: null, seen_state: 'unseen', match: 'strong' }]
    });
    await open();
    await click($('setup-find'));
    const field = $('setup-search');
    expect(document.activeElement, 'the field takes focus in the tap').toBe(field);
    expect(field.closest('[role="search"]').textContent).toContain('Cancel');
    expect($('setup-leave'), 'the field takes the top row').toBeNull();
    expect($('setup-undo')).toBeNull();
    expect($('setup-next'), 'no dock while searching').toBeNull();
    expect($('setup-hits').textContent.trim()).toBe('Type at least two letters');

    await type('Pris');
    expect(all('setup-hit')).toHaveLength(1);
    await click($('setup-cancel'));
    expect($('setup-search')).toBeNull();
    expect($('setup-hits')).toBeNull();
    expect($('setup-step').textContent).toBe('All-time favourite');
    expect($('setup-next').textContent.trim()).toBe('None for All-time favourite');
    expect(document.activeElement, 'focus goes back to what opened the search').toBe($('setup-find'));
    expect(all('setup-film').map((el) => el.getAttribute('aria-label'))).toEqual(['Heat', 'Zodiac', 'Sicario']);
  });

  it('holds Finish until one film is on the ladder', async () => {
    await open();
    await click($('setup-next'));
    const finish = /** @type {HTMLButtonElement} */ ($('setup-finish'));
    expect(finish.disabled).toBe(true);
    expect(document.getElementById(finish.getAttribute('aria-describedby')).textContent.trim()).toBe(
      'Put at least one film on your ladder to finish.'
    );
    await click(cell('Zodiac'));
    expect(finish.disabled).toBe(false);
    expect(finish.hasAttribute('aria-describedby')).toBe(false);
  });

  it('leaves at once with nothing picked, and asks first once a film is', async () => {
    await open();
    await click($('setup-leave'));
    expect(nav.goto).toHaveBeenCalledWith('/rate');
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    nav.goto.mockClear();
    await click(cell('Heat'));
    await click($('setup-leave'));
    expect(nav.goto).not.toHaveBeenCalled();
    const sheet = document.querySelector('[role="dialog"]');
    expect(sheet.textContent).toContain("Leave the set-up? Your picks so far aren't kept.");
    const go = [...sheet.querySelectorAll('[role="menuitem"]')].find((b) => b.textContent.includes('Leave the set-up'));
    await click(go);
    expect(nav.goto).toHaveBeenCalledWith('/rate');
  });

  it('ends on the ladder it made: each step, its count and its first film, and what waits', async () => {
    await open();
    await click(cell('Heat'));
    await click($('setup-next'));
    await click(cell('Zodiac'));
    await click($('setup-finish'));
    expect(posted.at(-1)).toEqual({ picks: [{ title_id: 1, tier: 5 }, { title_id: 2, tier: 4 }] });

    const done = $('setup-done');
    expect(done.querySelector('h2').textContent).toBe('Your ladder is ready');
    expect(done.textContent).toContain('2 films are on it. From now on, one tap puts each film you rate on it.');
    expect($('setup-learning').textContent).toBe(
      'Your suggestions get about three times more personal between 5 and 100 ratings.'
    );
    const rungs = [...done.querySelectorAll('li')];
    expect(rungs.map((li) => li.querySelector('.rung-word').textContent)).toEqual([
      'All-time favourite', 'Excellent', 'Very good'
    ]);
    expect(rungs[0].textContent).toContain('1 film');
    expect(rungs[0].querySelector('[data-testid="rate-poster"]').getAttribute('data-title-id')).toBe('1');
    expect(rungs[2].classList.contains('empty')).toBe(true);
    expect(rungs[2].querySelector('[data-testid="rate-poster"]')).toBeNull();
    expect(done.textContent).toContain(
      'Your 4 earlier ratings are kept as history. The 3 others you rated before come back on Rate, ' +
        'one at a time, to find their step.'
    );
    expect(document.querySelector('a[href="/rate"]').textContent).toBe('Start rating');
    expect(document.querySelector('a[href="/"]').textContent).toBe('Done');
  });
});

describe('the set-up on a desktop', () => {
  it('puts Next and Finish in the top row beside Undo, once each, with the reason Finish waits', async () => {
    width(1280);
    await open();
    const beside = (id) => $('setup-undo').parentElement.contains($(id));
    expect(all('setup-next')).toHaveLength(1);
    expect(beside('setup-next')).toBe(true);
    expect($('setup-next').textContent.trim()).toBe('None for All-time favourite');

    await click(cell('Heat'));
    await click($('setup-next'));
    expect(all('setup-finish')).toHaveLength(1);
    expect(beside('setup-finish')).toBe(true);
    const finish = /** @type {HTMLButtonElement} */ ($('setup-finish'));
    expect(finish.disabled, 'a pick on the step above is on the ladder').toBe(false);

    await click($('setup-undo'));
    await click(cell('Heat'));
    await click($('setup-next'));
    expect(/** @type {HTMLButtonElement} */ ($('setup-finish')).disabled).toBe(true);
    expect(document.getElementById($('setup-finish').getAttribute('aria-describedby')).textContent).toBe(
      'Put at least one film on your ladder to finish.'
    );

    await click(cell('Zodiac'));
    await click($('setup-finish'));
    expect(all('setup-done')).toHaveLength(1);
    expect(document.querySelectorAll('a[href="/rate"]'), 'Start rating, once').toHaveLength(1);
  });

  it('asks before a tap on the rail drops the picks, then goes where it was tapped', async () => {
    width(1280);
    await open();
    nav.goto.mockClear();
    const rail = async (path) => {
      const tap = { type: 'link', to: { url: new URL(path, 'http://localhost') }, cancel: vi.fn() };
      nav.before(tap);
      await settle();
      return tap;
    };
    const sheet = () => document.querySelector('[role="dialog"]');
    const option = (label) => [...sheet().querySelectorAll('button')].find((b) => b.textContent.includes(label));

    expect((await rail('/rank')).cancel, 'nothing picked, nothing to lose').not.toHaveBeenCalled();
    await click(cell('Heat'));
    expect((await rail('/rank')).cancel).toHaveBeenCalled();
    expect(sheet().textContent).toContain("Leave the set-up? Your picks so far aren't kept.");
    await click(option('Cancel'));
    expect(nav.goto).not.toHaveBeenCalled();

    await click($('setup-leave'));
    await click(option('Leave the set-up'));
    expect(nav.goto, 'Leave itself still goes back to Rate').toHaveBeenLastCalledWith('/rate');

    await rail('/rank');
    await click(option('Leave the set-up'));
    expect(nav.goto).toHaveBeenLastCalledWith(new URL('http://localhost/rank'));
  });
});
