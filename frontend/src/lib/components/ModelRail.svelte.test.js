/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ModelRail from './ModelRail.svelte';

const RAIL = '[data-testid="model-rail"]';
/** The shell's trigger, by the testid `+layout.svelte` gives it and four e2e specs already use. */
const OPENER = 'model-rail-open';
const EVENT = '[data-testid="model-rail-event"]';
const EMPTY = '[data-testid="model-rail-empty"]';

/** One `/api/model-log` body — §6.7's own worked example, in the shape decision 117 sends. */
const log = (id, text) => ({
  show_model: true,
  kinds: ['verdict'],
  events: [{ id, kind: 'verdict', at: '2026-09-10T20:14:00.000Z', text }]
});

const FIRST = log(1, 'verdict(jenny, Heat) = liked -> ordered-logit arm, refit 31 ms');
const SECOND = log(2, 'verdict(jenny, Sicario) = loved -> ordered-logit arm, refit 28 ms');

let pending = [];
let target;

beforeEach(() => {
  pending = [];
  target = document.createElement('div');
  document.body.appendChild(target);
  // The resolver is kept, not called, so each reply is held open until a case answers it.
  vi.stubGlobal(
    'fetch',
    vi.fn(
      () =>
        new Promise((resolve) => {
          pending.push((body) =>
            resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(body)) })
          );
        })
    )
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  target.remove();
});

async function settle() {
  // A macrotask, not counted microtasks: a reply crosses fetch, `res.text()` and the component's `.then`.
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function answer(nth, body) {
  const reply = pending[nth];
  expect(reply, `request ${nth} was never made`).toBeTruthy();
  reply(body);
  await settle();
}

function openable() {
  const props = $state({ open: false, onClose: () => (props.open = false) });
  return { props, rail: mount(ModelRail, { target, props }) };
}

const lines = () => [...target.querySelectorAll(EVENT)].map((li) => li.textContent);

// jsdom ships no PointerEvent, and the action reads none of it.
const tap = (/** @type {Element} */ el) =>
  el.dispatchEvent(new Event('pointerdown', { bubbles: true, cancelable: true }));

const press = (/** @type {string} */ key) =>
  document.body.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));

// The hint span is what a finger lands on, so a guard matching only the button would miss it.
function opener() {
  const button = document.createElement('button');
  button.dataset.testid = OPENER;
  const hint = document.createElement('span');
  button.appendChild(hint);
  document.body.appendChild(button);
  return { button, hint };
}

function railText() {
  const el = target.querySelector(RAIL);
  expect(el, 'the drawer is not on screen at all').not.toBeNull();
  return el.textContent;
}

describe('the drawer across a close', () => {
  it('drops the log, so the reopen reads the journal rather than replaying the last one', async () => {
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      await answer(0, FIRST);
      expect(lines(), 'the first open never rendered a line').toHaveLength(1);

      props.open = false;
      flushSync();
      expect(target.querySelector(RAIL), 'the closed drawer is absent, not hidden').toBeNull();

      // The reopen, with its refetch still unanswered. This is the frame the clause is about.
      props.open = true;
      flushSync();
      expect(
        globalThis.fetch,
        'the second open did not refetch: the drawer is serving a cache'
      ).toHaveBeenCalledTimes(2);
      expect(railText()).toContain('reading the journal');
      expect(lines(), "the reopened drawer is showing the previous open's events").toHaveLength(0);
      // The empty-log line claims the log answered empty, which is as false while the request is in flight.
      expect(target.querySelector(EMPTY)).toBeNull();

      // And it does finish reading: a drawer wedged on the loading line would pass the rest.
      await answer(1, SECOND);
      expect(lines()).toEqual([expect.stringContaining('Sicario')]);
    } finally {
      unmount(rail);
    }
  });

  it("does not let the closed open's reply land in the one that replaced it", async () => {
    // The first open's reply lands after the reopen; only the teardown's `cancelled` flag stops it.
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      props.open = false;
      flushSync();
      props.open = true;
      flushSync();
      expect(globalThis.fetch, 'each open asks once').toHaveBeenCalledTimes(2);

      await answer(0, FIRST);
      expect(
        lines(),
        "the closed open's reply landed in the drawer that replaced it"
      ).toHaveLength(0);
      expect(railText()).toContain('reading the journal');

      await answer(1, SECOND);
      expect(lines(), 'the live request never landed').toEqual([
        expect.stringContaining('Sicario')
      ]);
    } finally {
      unmount(rail);
    }
  });
});

describe('the drawer dismisses', () => {
  it('closes when the pointer lands outside it', () => {
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      expect(target.querySelector(RAIL)).not.toBeNull();

      tap(document.body);
      flushSync();
      expect(props.open, 'the drawer has no outside-tap dismissal').toBe(false);
    } finally {
      unmount(rail);
    }
  });

  it('stays open when the pointer lands inside it', () => {
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      tap(target.querySelector(RAIL));
      flushSync();
      expect(props.open, 'reading the log closed it').toBe(true);
    } finally {
      unmount(rail);
    }
  });

  it("leaves the shell's own trigger able to close it", () => {
    const { button, hint } = opener();
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();

      tap(hint);
      flushSync();
      expect(props.open, 'the trigger can no longer close the drawer it opened').toBe(true);
    } finally {
      unmount(rail);
      button.remove();
    }
  });

  it('closes on Escape, and on no other key', () => {
    // `m` is the shell's shortcut; a drawer closing on any key would race it.
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      press('m');
      flushSync();
      expect(props.open, 'a key that is not Escape closed the drawer').toBe(true);

      press('Escape');
      flushSync();
      expect(props.open, 'Escape does not close the drawer').toBe(false);
    } finally {
      unmount(rail);
    }
  });

  it('stops listening once it is closed', () => {
    // Shell chrome that mounts on every open: a leaked listener is one per open for the tab's life.
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      props.open = false;
      flushSync();

      const closes = vi.fn();
      props.onClose = closes;
      tap(document.body);
      press('Escape');
      expect(closes, 'the closed drawer is still listening on document').not.toHaveBeenCalled();
    } finally {
      unmount(rail);
    }
  });
});

describe("the drawer's header", () => {
  it('states its pinned depth and cites no section, switch on or not (decision 486)', () => {
    const { props, rail } = openable();
    try {
      props.open = true;
      flushSync();
      const header = target.querySelector(`${RAIL} header`).textContent;
      expect(header).toContain('last 15 events');
      expect(header).not.toMatch(/§\s?\d/);
    } finally {
      unmount(rail);
    }
  });
});
