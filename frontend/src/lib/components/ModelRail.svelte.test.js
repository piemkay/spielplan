/**
 * @vitest-environment jsdom
 *
 * The drawer's payload, across a close. Spec v2.1 §6.7; decision 117; M4.9 finding 27.
 *
 * §6.7: "an ephemeral log (last ~15 events, never persisted)". That clause does not ask a
 * reopened rail to come back empty — the server's in-process deque hands back the same last
 * fifteen lines, and keeping them there for the life of the process is what "never persisted"
 * means at that end. What it forbids is THIS component holding a payload across a close:
 * between the reopen and the refetch those lines are a claim about now made out of then, under
 * a header that says the log is live, and on a shared device they are somebody else's activity.
 * Both cases below are about the drawer's state; neither says anything about the deque.
 *
 * HERE RATHER THAN IN PLAYWRIGHT. The stale render lasts exactly one round trip, so the
 * assertion needs the reply held open — and `page.route` cannot hold it. The app is a PWA:
 * `src/service-worker.js` is registered on every render, and although it refuses to cache
 * anything under `/api` it is still what the request passes through, which is enough for
 * Playwright never to see it. Interception would need a context created with
 * `serviceWorkers: 'block'`, and `e2e/specs/02-shell.spec.js` is one file in a filename-ordered
 * suite that signs in once, so a fresh context is not its to spend. Held here, the reply is a
 * promise nobody has resolved yet: it costs nothing and it cannot flake. `02-shell.spec.js`
 * keeps the half only a real server can answer — that the reopened drawer fills again, from the
 * live deque, through the service worker the app actually ships.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ModelRail from './ModelRail.svelte';

const RAIL = '[data-testid="model-rail"]';
const EVENT = '[data-testid="model-rail-event"]';
const EMPTY = '[data-testid="model-rail-empty"]';

/** One `/api/model-log` body — §6.7's own worked example, in the shape decision 117 sends. */
const log = (id, text) => ({
  show_model: true,
  kinds: ['verdict'],
  events: [{ id, kind: 'verdict', scope: 'jenny', at: '2026-09-10T20:14:00.000Z', text }]
});

const FIRST = log(1, 'verdict(jenny, Heat) = liked -> ordered-logit arm, refit 31 ms');
const SECOND = log(2, 'verdict(jenny, Sicario) = loved -> ordered-logit arm, refit 28 ms');

/** The requests in flight, each unanswered until a case says otherwise. */
let pending = [];
let target;

beforeEach(() => {
  pending = [];
  target = document.createElement('div');
  document.body.appendChild(target);
  // `api.js` reads `res.text()` and parses it, so this is the whole surface of a reply — and
  // the resolver is kept rather than called, which is the hold the e2e layer could not take.
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

/** Let everything the drawer has queued settle: state, then the render it causes. */
async function settle() {
  // A macrotask, not a counted number of microtask turns: a reply crosses `fetch`, `res.text()`
  // and the component's own `.then` before it is state, and counting those hops is a test that
  // breaks when `api.js` gains a line.
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

/** Answer the nth request the drawer has made — by position, because which one matters. */
async function answer(nth, body) {
  const reply = pending[nth];
  expect(reply, `request ${nth} was never made`).toBeTruthy();
  reply(body);
  await settle();
}

/** Mount the one drawer with reactive props, closed, the way the shell mounts it. */
function openable() {
  // `onClose` is the shell's, and every case below closes through the prop rather than the
  // button: the claim is about what `open` going false does to the payload, and the button is
  // `10-home.spec.js`'s and `02-shell.spec.js`'s subject.
  const props = $state({ open: false, onClose: () => (props.open = false) });
  return { props, rail: mount(ModelRail, { target, props }) };
}

const lines = () => [...target.querySelectorAll(EVENT)].map((li) => li.textContent);

/** The drawer's text — and a failure that names an absent drawer rather than throwing. */
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
      // The empty-log line is a payload too — it says the deque answered and had nothing, which
      // is as false as a list of stale events while the request is still in flight.
      expect(target.querySelector(EMPTY)).toBeNull();

      // And it does finish reading: a drawer wedged on the loading line would satisfy every
      // assertion above and be useless.
      await answer(1, SECOND);
      expect(lines()).toEqual([expect.stringContaining('Sicario')]);
    } finally {
      unmount(rail);
    }
  });

  it("does not let the closed open's reply land in the one that replaced it", async () => {
    // The other way a payload crosses a close, and the one clearing `log` cannot reach: the
    // first open's request is still in flight when the drawer shuts, so its `.then` fires into
    // a component that has since reopened and asked again. Without the teardown's `cancelled`
    // flag the reopened drawer fills with the closed open's events — the same forbidden render
    // as the case above, arriving from the other side, and this one is not one frame long but
    // stays until the second reply overwrites it.
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
