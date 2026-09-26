/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import MapPage from './+page.svelte';
import TastePage from '../taste/+page.svelte';

const REFERENCE = /§\s?\d|decision \d|proposal \d|\bM[0-7](\.\d+)?\b/i;

const MODEL_NOUNS = /has\(|predicate|artifact|bundle|prior|extraction queue|vocabulary/i;

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => target.remove());

function open(Page) {
  const app = mount(Page, { target });
  flushSync();
  return app;
}

describe('a surface §12 has not reached yet', () => {
  for (const [name, Page] of [
    ['Map', MapPage],
    ['Taste', TastePage]
  ]) {
    it(`${name} says it is not built, in plain words, and names no milestone`, () => {
      const page = open(Page);
      try {
        expect(target.querySelector('h1').textContent.trim()).toBe(name);
        expect(target.textContent).toContain('Not built yet');
        expect(target.textContent).toContain('coming in a later update');
        expect(target.textContent).not.toMatch(REFERENCE);
        expect(target.textContent).not.toMatch(MODEL_NOUNS);
        expect(target.querySelectorAll('li').length, 'a placeholder says what it will do').toBeGreaterThan(0);
      } finally {
        unmount(page);
      }
    });
  }
});
