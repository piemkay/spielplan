/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import PosterCard, { isColdPlaced } from './PosterCard.svelte';

const title = (overrides = {}) => ({
  id: 1,
  kind: 'movie',
  name: 'Heat',
  year: 1995,
  runtime_min: 170,
  placement: 'warm',
  item_n: 35000,
  e_source: 'backbone',
  seen_state: 'unseen',
  ...overrides
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  target.remove();
});

function render(t) {
  const app = mount(PosterCard, { target, props: { title: t, onSelect: () => {} } });
  flushSync();
  return app;
}

describe('the New badge', () => {
  it('is never worn by a title the crowd has rated, whatever placed it', () => {
    expect(isColdPlaced(title({ e_source: 'cold_tower', placement: 'cold_tower', item_n: 192061 })))
      .toBe(false);
    expect(isColdPlaced(title({ e_source: 'cold_tower', item_n: 0 }))).toBe(true);
    expect(isColdPlaced(title({ e_source: null, placement: 'cold_tower', item_n: null }))).toBe(true);
  });

  it('explains itself in words a member can check', () => {
    const app = render(title({ e_source: 'cold_tower', item_n: 0 }));
    const badge = target.querySelector('[data-testid="new-badge"]');
    expect(badge.textContent).toBe('New');
    expect(badge.getAttribute('title')).not.toContain('Cold Tower');
    expect(badge.closest('.poster'), 'the badge sits on the art').not.toBeNull();
    unmount(app);
  });
});

describe('the "in library" chip', () => {
  it('marks a catalog card the household owns', () => {
    const app = render(title({ is_owned: true }));
    expect(target.querySelector('[data-testid="owned-chip"]').textContent).toBe('In library');
    unmount(app);
  });

  it('is absent from an unowned card and from a card that does not say', () => {
    // A shelf card never passes `is_owned`: every shelf is owned-only.
    for (const t of [title({ is_owned: false }), title()]) {
      const app = render(t);
      expect(target.querySelector('[data-testid="owned-chip"]')).toBeNull();
      unmount(app);
    }
  });
});

describe('what a card says under its art (decision 527)', () => {
  it('is the name and "year · runtime", and a seen title wears a check on its art', () => {
    const app = render(title({ seen_state: 'seen' }));
    const names = [...target.querySelectorAll('.meta > span')].map((el) => el.textContent);
    expect(names).toEqual(['Heat', '1995 · 2h 50m']);
    const seen = target.querySelector('[data-testid="seen-badge"]');
    expect(seen.getAttribute('aria-label')).toBe('Seen');
    expect(seen.closest('.poster')).not.toBeNull();
    unmount(app);
  });
});

describe('a card beyond the library (decision 544)', () => {
  it('wears an ember bookmark on its art when the viewer wanted it, and nothing otherwise', () => {
    let app = render(title({ wanted: true }));
    const mark = target.querySelector('[data-testid="wanted-mark"]');
    expect(mark.getAttribute('aria-label')).toBe('On your wish list');
    expect(mark.closest('.poster'), 'the mark sits on the art').not.toBeNull();
    unmount(app);
    app = render(title({ wanted: false }));
    expect(target.querySelector('[data-testid="wanted-mark"]')).toBeNull();
    unmount(app);
  });

  it('names the liked film it is like as a third line', () => {
    const app = render(title({ like: { title_id: 9, name: 'Collateral', terms: ['night city'] } }));
    const lines = [...target.querySelectorAll('.meta > span')].map((el) => el.textContent);
    expect(lines).toEqual(['Heat', '1995 · 2h 50m', 'Like Collateral']);
    unmount(app);
  });
});

describe('the name on the card (decision 516)', () => {
  const langs = Object.getOwnPropertyDescriptor(Navigator.prototype, 'languages');
  afterEach(() => {
    if (langs) Object.defineProperty(Navigator.prototype, 'languages', langs);
  });
  const speak = (tags) =>
    Object.defineProperty(Navigator.prototype, 'languages', { configurable: true, get: () => tags });
  const wunder = { name: 'Wonderfully Beautiful', original_name: 'Wunderschön', original_language: 'de' };

  it('is the German original on a German phone, with the English one in the label', () => {
    speak(['de-DE']);
    const app = render(title(wunder));
    expect(target.querySelector('.name').textContent).toBe('Wunderschön');
    expect(target.querySelector('.card-wrap').getAttribute('title')).toBe(
      'Wunderschön (Wonderfully Beautiful)'
    );
    unmount(app);
  });

  it('is the name everywhere else, and on a card that does not carry the language', () => {
    speak(['en-GB']);
    let app = render(title(wunder));
    expect(target.querySelector('.name').textContent).toBe('Wonderfully Beautiful');
    unmount(app);
    speak(['de-DE']);
    app = render(title({ name: 'Wonderfully Beautiful', original_name: 'Wunderschön' }));
    expect(target.querySelector('.name').textContent).toBe('Wonderfully Beautiful');
    unmount(app);
  });
});
