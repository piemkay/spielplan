import { beforeEach, describe, expect, it } from 'vitest';

import {
  closeRail,
  followShowModel,
  modelRail,
  publishSuppressed,
  toggleRail
} from './rail.svelte.js';

/**
 * The one drawer's state. Spec v2.1 §6.7; decisions 117 and 118; M4.9 finding 25.
 *
 * Three rules live here rather than in the shell, because all three are the kind that survive a
 * screenshot. A rail that opens on a keystroke after the person switched the numbers off is a
 * back door into what §6.7 promises is absent; a rail left open under a vanished control is a
 * panel nothing can close; and a suppressed list that outlives Home is a statement about
 * shelves that are not on screen. Playwright can see the first of those on one route at a time.
 */

beforeEach(() => {
  // Module state is one object for the whole run, which is the point of it — so each case
  // starts from the shipped default rather than from its predecessor's last tap.
  closeRail();
  publishSuppressed([]);
});

describe('the drawer', () => {
  it('opens and closes from the one control that owns it', () => {
    expect(modelRail.open, 'the drawer is closed until someone asks for it').toBe(false);
    toggleRail(true);
    expect(modelRail.open).toBe(true);
    toggleRail(true);
    expect(modelRail.open).toBe(false);
    toggleRail(true);
    closeRail();
    expect(modelRail.open).toBe(false);
  });

  it('refuses to open while the show-the-model preference is off', () => {
    // Proposal 118 puts the rail one keystroke from every render; decision 117 makes the
    // numbers absent by default. The shortcut must not be the way round the second of those.
    toggleRail(false);
    expect(modelRail.open, 'the preference is off, so there is nothing to open').toBe(false);
    toggleRail(undefined);
    expect(modelRail.open).toBe(false);
  });

  it('closes when the preference is turned off under it', () => {
    toggleRail(true);
    expect(modelRail.open).toBe(true);
    followShowModel(false);
    expect(modelRail.open, 'the control that opened it is gone; the panel cannot stay').toBe(
      false
    );
  });

  it('leaves an open drawer alone while the preference is still on', () => {
    // The shell applies this on every session change, not only on the flip, so an assertion
    // that it only ever closes things would pass on a function that closed the drawer on every
    // render of the account chip.
    toggleRail(true);
    followShowModel(true);
    expect(modelRail.open).toBe(true);
  });
});

describe("the suppressed list", () => {
  it("carries Home's rows to a shell that has no Home payload", () => {
    const rows = [{ shelf: 'because_anchor', kind: 'movie', reason: 'nothing to anchor on' }];
    publishSuppressed(rows);
    expect(modelRail.suppressed).toEqual(rows);
  });

  it('is emptied rather than left behind when Home publishes nothing', () => {
    // Decision 117 strips `suppressed` from the payload with the toggle off, so `undefined` is
    // the ordinary case rather than an error — and the drawer renders a list, not a maybe.
    publishSuppressed([{ shelf: 'shared_terms', kind: 'series', reason: 'fewer than three' }]);
    publishSuppressed(undefined);
    expect(modelRail.suppressed).toEqual([]);
    publishSuppressed(null);
    expect(modelRail.suppressed).toEqual([]);
  });
});
