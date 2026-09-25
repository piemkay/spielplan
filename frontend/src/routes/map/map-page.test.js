/**
 * @vitest-environment jsdom
 *
 * What the two unbuilt surfaces say to whoever reaches them by URL. Spec v2.1 §6 (the surface
 * names are normative), §12 (the build order); decisions 486 and 488.
 *
 * Until the 2026-09-25 user test these pages read their milestone out of `/auth/me`'s nav
 * payload and printed it - "Not built yet - this surface arrives with M6" - so that §12's order
 * was stated once [ds08-nav-rail-milestone-claim-is-false-and-the-value-is-duplicated]. Two
 * members met that sentence from the tab bar, over predicate syntax and artifact names. Decision
 * 488 took the two surfaces out of navigation and decision 486 took milestone labels out of
 * every member surface, so what is asserted now is the member register: the placeholder says
 * what the surface will do and that it is not built, and names no milestone and no spec clause.
 *
 * BOTH PAGES IN ONE FILE because they are one component and one rule. Named `map-page.test.js`
 * and not `+page.svelte.test.js` for the reason `rate-page.test.js` states: SvelteKit reserves the
 * `+` prefix inside `src/routes` and `vite build` fails on any other `+`-named file.
 *
 * `05-milestones.spec.js` is the end-to-end half, and it still fails by design on the day a
 * surface ships.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import MapPage from './+page.svelte';
import TastePage from '../taste/+page.svelte';

/** Section signs, decision/proposal numbers and milestone labels: decision 486 clause 2. */
const REFERENCE = /§\s?\d|decision \d|proposal \d|\bM[0-7](\.\d+)?\b/i;

/** The spec's own vocabulary that the placeholders used to carry to members. */
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
