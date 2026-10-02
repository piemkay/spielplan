/**
 * @vitest-environment jsdom
 */

import { get as read } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Shallow routing on a store: `replaceState` rewrites the entry in place, as SvelteKit's does.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/rank'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  goto: vi.fn(),
  replaceState: vi.fn((_url, state) => nav.page.update((p) => ({ ...p, state })))
}));

import { goto, replaceState } from '$app/navigation';
import { jumpHome, returningCard } from './cardJump.js';

const state = () => read(nav.page).state;
const setState = (next) => nav.page.update((p) => ({ ...p, state: next }));

let steps;

beforeEach(() => {
  steps = [];
  setState({});
  vi.mocked(replaceState).mockClear();
  vi.mocked(goto).mockReset();
  vi.mocked(goto).mockImplementation(async (href) => {
    steps.push(['goto', href, state()]);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('jumpHome', () => {
  it('stamps the page it leaves with the card, then goes to Home', async () => {
    await jumpHome('/?person=1&kind=movie&kind=series', { titleId: 5, from: 'rank' });
    expect(steps).toEqual([
      ['goto', '/?person=1&kind=movie&kind=series', { returnCard: { titleId: 5, from: 'rank' } }]
    ]);
  });

  it("first walks back past the sheets still open under the card, onto the page's own entry", async () => {
    setState({ sheets: ['you', 'wish-list'] });
    const go = vi.spyOn(history, 'go').mockImplementation((delta) => {
      setState({ sheets: state().sheets.slice(0, delta) });
      setTimeout(() => dispatchEvent(new PopStateEvent('popstate')));
    });
    await jumpHome('/?like=1&kind=movie&filters=open', { titleId: 1, from: 'you' });
    expect(go).toHaveBeenCalledWith(-2);
    expect(steps).toEqual([
      ['goto', '/?like=1&kind=movie&filters=open', { sheets: [], returnCard: { titleId: 1, from: 'you' } }]
    ]);
  });
});

describe('returningCard', () => {
  it('names the card for the surface that left it, once, and keeps the rest of the entry', () => {
    setState({ sheets: [], returnCard: { titleId: 5, from: 'rank' } });
    expect(returningCard('you')).toBeNull();
    expect(state().returnCard).toEqual({ titleId: 5, from: 'rank' });
    expect(returningCard('rank')).toBe(5);
    expect(state()).toEqual({ sheets: [] });
    expect(returningCard('rank')).toBeNull();
  });

  it('writes nothing where there is no stamp', () => {
    expect(returningCard('taste')).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
