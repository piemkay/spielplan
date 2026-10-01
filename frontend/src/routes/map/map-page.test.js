/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import MapPage from './+page.svelte';

const REFERENCE = /§\s?\d|decision \d|proposal \d|\bM[0-7](\.\d+)?\b/i;

const MODEL_NOUNS = /has\(|predicate|artifact|bundle|prior|extraction queue|vocabulary/i;

let target;
let page;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  page = mount(MapPage, { target });
  flushSync();
});

afterEach(() => {
  unmount(page);
  target.remove();
});

describe('the Map, which has no chosen form yet', () => {
  it('says it is not built, in plain words, and names no milestone', () => {
    expect(target.querySelector('h1').textContent.trim()).toBe('Map');
    expect(target.textContent).toContain('Not built yet');
    expect(target.textContent).toContain('coming in a later update');
    expect(target.textContent).not.toMatch(REFERENCE);
    expect(target.textContent).not.toMatch(MODEL_NOUNS);
  });

  it('promises no form: no axes, no lenses, no list of what it will do', () => {
    expect(target.querySelectorAll('li')).toHaveLength(0);
    expect(target.textContent).not.toMatch(/\baxis\b|\baxes\b|\blens|colour the map/i);
  });
});
