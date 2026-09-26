import { beforeEach, describe, expect, it } from 'vitest';

import {
  closeRail,
  followShowModel,
  modelRail,
  publishSuppressed,
  toggleRail
} from './rail.svelte.js';

beforeEach(() => {
  // Module state persists across cases; start each from the shipped default.
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
    // The payload drops `suppressed` with the toggle off, so `undefined` is the ordinary case.
    publishSuppressed([{ shelf: 'shared_terms', kind: 'series', reason: 'fewer than three' }]);
    publishSuppressed(undefined);
    expect(modelRail.suppressed).toEqual([]);
    publishSuppressed(null);
    expect(modelRail.suppressed).toEqual([]);
  });
});
