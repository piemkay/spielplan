/**
 * @vitest-environment jsdom
 *
 * Where the two unbuilt surfaces get their milestone from. Spec v2.1 §6 (the surface names are
 * normative), §12 (the build order); M4.15 finding 24
 * [ds08-nav-rail-milestone-claim-is-false-and-the-value-is-duplicated].
 *
 * `api/auth.py`'s `SURFACES` ships a milestone per surface and the nav payload carries it to the
 * browser, where nothing read it: `NavRail.svelte` renders `s.label` and `title={s.label}`, and
 * `s.milestone` appeared nowhere in `frontend/src`. The two placeholder pages wrote "M6" as a
 * literal instead, so §12's order was stated in three places and kept true in one — and this
 * project has moved it more than once (decisions 165, 166, 181, 182). A change made in the
 * server's tuple now reaches the screen, which is the whole of the repair.
 *
 * BOTH PAGES IN ONE FILE because they are one repair and one read; splitting it would duplicate
 * this argument rather than test anything twice. Named `map-page.test.js` and not
 * `+page.svelte.test.js` for the reason `rate-page.test.js` states: SvelteKit reserves the `+`
 * prefix inside `src/routes` and `vite build` fails on any other `+`-named file.
 *
 * `05-milestones.spec.js` is the end-to-end half and asserts the rendered token literally, so the
 * first case below is deliberately the value the server really ships.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// `vi.hoisted` because a `vi.mock` factory is hoisted above every other statement in the file:
// the object is created there so each case can put a different payload on the same reference,
// which is what the pages read.
const store = vi.hoisted(() => ({ session: { user: null } }));
vi.mock('$lib/session.svelte.js', () => ({ session: store.session }));

import MapPage from './+page.svelte';
import TastePage from '../taste/+page.svelte';

/** `/auth/me`'s nav payload, in the shape `api/auth.py`'s `_nav` sends it. */
const signedIn = (map, taste) => {
  store.session.user = {
    nav: {
      surfaces: [
        { key: 'home', href: '/', label: 'Home', milestone: 'M0' },
        { key: 'map', href: '/map', label: 'Map', milestone: map },
        { key: 'taste', href: '/taste', label: 'Taste', milestone: taste }
      ]
    }
  };
};

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  store.session.user = null;
});

afterEach(() => target.remove());

function open(Page) {
  const app = mount(Page, { target });
  flushSync();
  return app;
}

const tag = () => target.querySelector('.tag').textContent.trim();

describe('a surface §12 has not reached yet', () => {
  it('names the milestone the server ships for it, which is still M6', async () => {
    // The guard on `05-milestones.spec.js`: that spec asserts the token exactly, so this read
    // has to land on the same string the payload carries today.
    signedIn('M6', 'M6');
    const map = open(MapPage);
    try {
      expect(tag()).toBe('M6');
      expect(target.textContent).toContain('this surface arrives with M6.');
    } finally {
      unmount(map);
    }
    const taste = open(TastePage);
    try {
      expect(tag()).toBe('M6');
      expect(target.textContent).toContain('this surface arrives with M6.');
    } finally {
      unmount(taste);
    }
  });

  it('follows the payload when §12 moves, instead of the literal it used to hold', async () => {
    // The defect itself: both pages said M6 whatever the server said, so a surface rescheduled
    // in `SURFACES` kept announcing the milestone it no longer belonged to.
    signedIn('M7', 'M8');
    const map = open(MapPage);
    try {
      expect(tag()).toBe('M7');
      expect(target.textContent).toContain('this surface arrives with M7.');
    } finally {
      unmount(map);
    }
    const taste = open(TastePage);
    try {
      expect(tag()).toBe('M8');
      expect(target.textContent).toContain('this surface arrives with M8.');
    } finally {
      unmount(taste);
    }
  });

  it('reads its own key and not the first surface in the list', async () => {
    // The payload is a list and the two pages differ only by the key they match on, which is
    // exactly the line a copy-paste between them would get wrong.
    signedIn('M7', 'M8');
    const taste = open(TastePage);
    try {
      expect(tag()).not.toBe('M0');
      expect(tag()).toBe('M8');
    } finally {
      unmount(taste);
    }
  });
});
