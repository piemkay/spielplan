/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import RateShelves from './RateShelves.svelte';

const film = (id, name) => ({ id, name, original_name: name, original_language: 'en', poster_path: null });
/** Best first, as `rate/shelves.shelves_for` sends them. */
const SHELVES = [
  {
    tier: 6,
    word: 'All-time favourite',
    count: 5,
    films: [film(1, 'Zodiac'), film(2, 'Heat'), film(3, 'Se7en'), film(4, 'Alien')]
  },
  { tier: 5, word: 'Loved it', count: 1, films: [film(5, 'Drive')] },
  { tier: 4, word: 'Liked it', count: 2, films: [film(6, 'Up'), film(7, 'Coco')] },
  { tier: 3, word: 'It was fine', count: 0, films: [] },
  { tier: 2, word: 'Not really for me', count: 0, films: [] },
  { tier: 1, word: "Didn't like it", count: 0, films: [] },
  { tier: 0, word: 'Hated it', count: 1, films: [film(8, 'Cats')] }
];

let target;
let app;
let placed;

beforeEach(() => {
  placed = [];
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
  vi.useRealTimers();
});

function open(over = {}) {
  /** @type {any} */
  const props = $state({
    shelves: SHELVES,
    name: 'Collateral',
    film: film(99, 'Collateral'),
    onPlace: (tier) => placed.push(tier),
    ...over
  });
  app = mount(RateShelves, { target, props });
  flushSync();
  // jsdom lays nothing out: 72px shelves from the top, 358px wide.
  const list = target.querySelector('[role="group"]');
  list.getBoundingClientRect = () => /** @type {any} */ ({ top: 0, bottom: 504, left: 0, right: 358 });
  rows().forEach((row, i) => {
    row.getBoundingClientRect = () => /** @type {any} */ ({ top: i * 72, bottom: (i + 1) * 72, left: 0, right: 358 });
  });
  list.setPointerCapture = () => {};
  return { props, list };
}

const rows = () => [...target.querySelectorAll('[data-testid="rate-shelf"]')];
const pointer = (type, y, x = 100) =>
  Object.assign(new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 }), { pointerId: 1 });

describe('the shelves (decisions 545, 550 and 551)', () => {
  it('draws one button per tier, best first, with up to four of the person\'s films and the word', () => {
    open();
    expect(target.querySelector('[role="group"]').getAttribute('aria-label')).toBe('Where does Collateral sit?');
    expect(rows().map((r) => r.dataset.tier)).toEqual(['6', '5', '4', '3', '2', '1', '0']);
    expect(rows().map((r) => r.querySelector('.word').textContent)).toEqual(SHELVES.map((s) => s.word));
    expect(rows()[0].getAttribute('aria-label')).toBe('All-time favourite, with Zodiac, Heat, Se7en and Alien');
    expect(rows()[2].getAttribute('aria-label')).toBe('Liked it, with Up and Coco');
    expect(rows()[0].querySelectorAll('[data-testid="rate-poster"]')).toHaveLength(4);
    // An empty shelf keeps its word and still places.
    expect(rows()[3].getAttribute('aria-label')).toBe('It was fine');
    expect(rows()[3].querySelectorAll('[data-testid="rate-poster"]')).toHaveLength(0);
    expect(target.textContent, 'no tier letter on Rate').not.toMatch(/\b(S|A\+|A|B|C|D|F)\b/);
  });

  it('places with one tap, empty shelves included', () => {
    open();
    rows()[1].click();
    rows()[3].click();
    expect(placed).toEqual([5, 3]);
  });

  it('lights the chosen shelf, puts the film on it first, and takes no second answer', () => {
    const { props } = open();
    props.lit = 6;
    flushSync();
    expect(rows()[0].classList.contains('lit')).toBe(true);
    const posters = [...rows()[0].querySelectorAll('[data-testid="rate-poster"]')];
    expect(posters.map((p) => p.dataset.titleId)).toEqual(['99', '1', '2', '3']);
    expect(target.querySelector('[role="group"]').classList.contains('deciding')).toBe(true);
    rows()[4].click();
    expect(placed).toEqual([]);
  });

  it('holds a shelf larger, slides to another, and places it on release', () => {
    vi.useFakeTimers();
    const { list } = open();
    list.dispatchEvent(pointer('pointerdown', 20));
    flushSync();
    expect(target.querySelector('.look'), 'a tap opens nothing').toBeNull();
    vi.advanceTimersByTime(250);
    flushSync();
    const look = () => target.querySelector('.look');
    expect(look().querySelector('.look-word').textContent).toBe('All-time favourite');
    expect(look().querySelector('.look-count').textContent).toBe('5 films');
    expect([...look().querySelectorAll('.cell-name')].map((n) => n.textContent)).toEqual([
      'Zodiac', 'Heat', 'Se7en', 'Alien'
    ]);

    list.dispatchEvent(pointer('pointermove', 160));
    flushSync();
    expect(look().querySelector('.look-word').textContent).toBe('Liked it');
    expect(look().querySelector('.look-count').textContent).toBe('2 films');
    list.dispatchEvent(pointer('pointerup', 160));
    flushSync();
    expect(look()).toBeNull();
    expect(placed).toEqual([4]);
    // The click the release brings with it is not a second answer.
    rows()[2].click();
    expect(placed).toEqual([4]);
  });

  it('opens the larger view at once when the finger slides first, and places nothing outside', () => {
    vi.useFakeTimers();
    const { list } = open();
    list.dispatchEvent(pointer('pointerdown', 450));
    list.dispatchEvent(pointer('pointermove', 470));
    flushSync();
    expect(target.querySelector('.look .look-word').textContent).toBe('Hated it');
    expect(target.querySelector('.look .look-count').textContent).toBe('1 film');
    list.dispatchEvent(pointer('pointerup', 470, 500));
    flushSync();
    expect(target.querySelector('.look')).toBeNull();
    expect(placed).toEqual([]);
  });

  it("counts a series shelf in series, and marks the shelf the title already sits on", () => {
    vi.useFakeTimers();
    const { list } = open({ kind: 'series', current: 5, testid: 'rate-shelf' });
    expect(rows()[1].getAttribute('aria-current')).toBe('true');
    expect(rows()[0].hasAttribute('aria-current')).toBe(false);
    list.dispatchEvent(pointer('pointerdown', 20));
    vi.advanceTimersByTime(250);
    flushSync();
    expect(target.querySelector('.look .look-count').textContent).toBe('5 series');
  });
});
