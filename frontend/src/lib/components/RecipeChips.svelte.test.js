/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  beforeNavigate: () => {}
}));

import { homeFilters, resetHomeFilters } from '$lib/homeFilters.svelte.js';
import { remember } from '$lib/recipe.svelte.js';
import RecipeChips from './RecipeChips.svelte';

const t = (label, facet = 'mood') => ({ term: `${facet}.${label}`, label, facet });
const row = (group, name, offered, quoted, inferred = []) => ({
  group, name, colour: '#c8613a', offered, quoted: quoted.map((l) => t(l)), inferred: inferred.map((l) => t(l))
});
const SHEET = [
  row('mood', 'Mood', true, ['dark comedy', 'deadpan & dry'], ['absurdist', 'dark', 'bleak', 'witty', 'macabre']),
  row('look', 'Look', true, ['muted & desaturated'], ['stylized']),
  row('sound', 'Sound', false, [], []),
  row('pace', 'Pace', false, [], ['slow-paced']),
  row('storytelling', 'Storytelling', true, ['genre-bending', 'tightly plotted']),
  row('setting', 'Setting', true, ['deep winter', 'small town']),
  row('characters', 'Characters', true, ['female-led', 'quirky ensemble']),
  row('themes', 'Themes', true, ['crime gone wrong', 'greed'])
];

let target;
let app;

beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true });
  for (const [id, name] of [[245, 'Knives Out'], [275, 'Fargo'], [11, 'Star Wars'], [6087, 'Obsession']]) {
    remember({ id, name, year: 1996, sheet: SHEET });
  }
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  resetHomeFilters();
  target.remove();
});

function open(like, less = []) {
  Object.assign(homeFilters, { like, less });
  app = mount(RecipeChips, { target });
  flushSync();
}

const chips = () => [...target.querySelectorAll('[data-testid="recipe-chip"]')];
const chipBody = (name) => chips().find((c) => c.textContent.includes(name)).querySelector('.body');
const dialog = () => target.querySelector('[role="dialog"]');
const checkbox = (name) =>
  [...dialog().querySelectorAll('[role="checkbox"]')].find((b) => b.querySelector('.name').textContent === name);
const click = (el) => {
  el.click();
  flushSync();
};
const button = (name) => [...dialog().querySelectorAll('button')].find((b) => b.textContent.trim() === name);

describe("the recipe's chips", () => {
  it('name each film with its sign, filled for like and outlined for less like', () => {
    open(['245', '6087:mood,sound,look'], ['11']);
    expect(chips().map((c) => c.querySelector('.label').textContent)).toEqual([
      'Like Knives Out', 'Mood, Sound & Look like Obsession', 'Less like Star Wars'
    ]);
    expect(chips()[2].classList.contains('less')).toBe(true);
    expect(chips()[1].querySelector('.terms').textContent).toBe('dark comedy, muted & desaturated, deadpan & dry, stylized, absurdist, dark');
    expect(chips()[1].querySelectorAll('.dot')).toHaveLength(3);
  });

  it('take a film out with the x', () => {
    open(['245', '275']);
    click(chips()[1].querySelector('.x'));
    expect(homeFilters.like).toEqual(['245']);
    expect(chips()).toHaveLength(1);
  });
});

describe("a film's sheet (decision 560)", () => {
  it('opens under the chip and takes several groups at once, changing nothing until Apply', () => {
    open(['245', '275']);
    click(chipBody('Fargo'));
    expect(dialog().getAttribute('aria-label')).toBe('Fargo in your recipe');
    expect(checkbox('All of Fargo').getAttribute('aria-checked')).toBe('true');
    click(checkbox('Mood'));
    click(checkbox('Look'));
    expect(checkbox('All of Fargo').getAttribute('aria-checked')).toBe('false');
    expect(dialog().querySelector('[data-testid="recipe-sheet-label"]').textContent).toBe('Mood & Look like Fargo');
    expect(dialog().querySelector('[data-testid="recipe-sheet-note"]').textContent).toBe(
      'Knives Out keeps everything but its own mood & look.'
    );
    expect(homeFilters.like).toEqual(['245', '275']);
    click(button('Apply'));
    expect(homeFilters.like).toEqual(['245', '275:mood,look']);
    expect(dialog()).toBeNull();
  });

  it('lists a chosen group\'s quoted terms, then our read with "Show N more"', () => {
    open(['245', '275']);
    click(chipBody('Fargo'));
    click(checkbox('Mood'));
    const group = checkbox('Mood').closest('[data-testid="recipe-group"]');
    expect([...group.querySelectorAll('.term:not(.inferred)')].map((s) => s.textContent)).toEqual(['dark comedy', 'deadpan & dry']);
    expect(group.querySelector('.ours').textContent).toBe('Our read · less certain');
    expect(group.querySelectorAll('.term.inferred')).toHaveLength(4);
    click(button('Show 1 more'));
    expect(group.querySelectorAll('.term.inferred')).toHaveLength(5);
  });

  it('shows a thin group disabled with its term, and an empty one disabled', () => {
    open(['245', '275']);
    click(chipBody('Fargo'));
    expect(checkbox('Pace').getAttribute('aria-disabled')).toBe('true');
    expect(checkbox('Pace').textContent).toContain('Only one term here: slow-paced');
    expect(checkbox('Sound').textContent).toContain('No terms here');
    click(checkbox('Pace'));
    expect(checkbox('Pace').getAttribute('aria-checked')).toBe('false');
  });

  it("disables a third film's groups while two films lend, saying which", () => {
    open(['245', '275:mood', '6087:look']);
    click(chipBody('Knives Out'));
    expect(checkbox('Themes').getAttribute('aria-disabled')).toBe('true');
    expect(checkbox('Themes').textContent).toContain('Fargo and Obsession already lend parts');
    expect(dialog().querySelector('.foot').textContent).toBe(
      'Fargo and Obsession are the 2 films that can lend parts. To take parts of Knives Out, first choose ' +
        'All of Fargo or All of Obsession.'
    );
  });

  it("on a phone puts each group's terms, our read dimmer, or its reason on a line under its name", () => {
    Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
    open(['245', '275']);
    click(chipBody('Fargo'));
    expect(dialog().querySelector('.desktop')).toBeNull();
    const mood = checkbox('Mood');
    expect(mood.querySelector('.lines > .name').textContent).toBe('Mood');
    expect(mood.querySelector('.lines > .preview').textContent).toBe(
      'dark comedy, deadpan & dry, absurdist, dark, bleak, witty, macabre'
    );
    expect(mood.querySelector('.preview .read').textContent).toBe(', absurdist, dark, bleak, witty, macabre');
    expect(checkbox('Pace').querySelector('.lines > .reason').textContent).toBe('Only one term here: slow-paced');
    expect(checkbox('All of Fargo').querySelector('.lines > .preview').textContent).toBe('The whole film, every part');
  });

  it('relabels with Less like, and Apply moves the film to the less side', () => {
    open(['245', '275']);
    click(chipBody('Fargo'));
    click(button('Less like'));
    expect(dialog().querySelector('[data-testid="recipe-sheet-label"]').textContent).toBe('Less like Fargo');
    expect(dialog().querySelector('[data-testid="recipe-sheet-note"]').textContent).toBe('Pushes away films like Fargo.');
    click(button('Apply'));
    expect(homeFilters.like).toEqual(['245']);
    expect(homeFilters.less).toEqual(['275']);
  });

  it('leaves the recipe as it was when shut with Escape', () => {
    open(['245', '275']);
    click(chipBody('Fargo'));
    click(checkbox('Mood'));
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect(dialog()).toBeNull();
    expect(homeFilters.like).toEqual(['245', '275']);
  });

  it('takes a group another film lends over, sending that film back to all of itself', () => {
    open(['245', '275:mood'], ['6087']);
    click(chipBody('Obsession'));
    expect(checkbox('Mood').textContent).toContain('Mood comes from Fargo now');
    click(checkbox('Mood'));
    click(button('Apply'));
    expect(homeFilters.like).toEqual(['245', '275']);
    expect(homeFilters.less).toEqual(['6087:mood']);
  });
});
