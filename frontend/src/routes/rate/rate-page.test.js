/**
 * @vitest-environment jsdom
 *
 * What the Rate surface SAYS, on the two payloads whose copy this milestone moved. Spec v2.1
 * §6.1, §6.8; proposals 37 and 46; M4.10 findings 20 and 21, cycle 1 M410-D8-01 and M410-D8-07.
 *
 * Both defects were in the markup and in nothing else. `GET /api/rate` already reported WHY there
 * was no card (`drained.cause`, one of queue / pool / both) and already marked a card the counter
 * did not call for (`substituted_for`), and the store assigned both verbatim — so every assertion
 * that stopped at `rate.drained` or at `card.substituted_for` passed while the screen said
 * something untrue. §6.8 makes a line the app states about its own state a matter of honesty, and
 * the only layer that can be held to it is the rendered one.
 *
 * NAMED `rate-page.test.js` and not `+page.svelte.test.js`, which is the name the convention in
 * `src/lib` would give it: SvelteKit reserves the `+` prefix inside `src/routes` and `vite build`
 * fails outright on any other `+`-named file ("Files prefixed with + are reserved"), so the
 * obvious name costs the production build.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT. Both states need a payload a seeded stack does not hand out
 * on demand: the pool cause needs an account with a standing session, zero verdicts and Battle
 * selected, and the substituted card needs the §6.0 banner's head redraw to land on a battle slot.
 * Reaching either through the suite means writing observations to get there, which is how
 * `11-rate.spec.js` — stateful, filename-ordered, one worker — stops being able to assert anything
 * about a fresh account. Mounted, the payload is the fixture and the render is exact.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The page reads `$page.url` synchronously at init — "`onMount` runs ahead of the effect below,
// so a deep link arriving as `/rate?head=41&head=57` would otherwise open its first card with no
// head at all" — so the store has to answer on subscribe. That one read is the whole of this
// module's surface here, and these cases carry no `?head=`.
vi.mock('$app/stores', () => {
  const url = new URL('http://localhost/rate');
  return {
    page: {
      subscribe: (run) => {
        run({ url });
        return () => {};
      }
    }
  };
});

import RatePage from './+page.svelte';
import { rate, rateTitle } from '$lib/rate.svelte.js';

const DRAINED = '[data-testid="rate-drained"]';
const SUBSTITUTED = '[data-testid="rate-substituted"]';

/** One `GET /api/rate` envelope, in the shape `rate/session.payload` sends. */
const envelope = (over = {}) => ({
  session: {
    id: 7,
    mode: 'battle',
    kinds: ['movie'],
    decisive: false,
    block: { index: 0, slot: 1, size: 15, counter: '1 / 15', serving: 'battle' }
  },
  card: null,
  drained: null,
  class_balance: {
    counts: [0, 0, 0],
    shares: [0, 0, 0],
    labels: ['disliked', 'fine', 'liked'],
    total: 0,
    warn: false,
    copy: 'rate a few more',
    threshold: 0.6
  },
  undo: { available: false, kind: null, reason: 'empty' },
  reveal: null,
  ledger: null,
  log: [],
  ...over
});

/** `rate/session.DRAINED_CAUSES`, verbatim — the server's sentence, which the page only renders. */
const CAUSES = {
  queue: {
    cause: 'queue',
    text: "You've rated everything we can queue right now. Battles sharpen what you've already said."
  },
  pool: {
    cause: 'pool',
    text:
      'A battle compares two titles you rated the same way, and there is no new pair to ' +
      'compare yet. Rate a few more in Sweep and the pairs start arriving.'
  },
  both: {
    cause: 'both',
    text:
      "You've rated everything we can queue right now, and there is no new pair of your " +
      'ratings left to compare either.'
  }
};

/** A sweep card the §6.0 banner's head redraw produced: a battle slot, a pool that can be full. */
const substitutedSweep = {
  type: 'sweep',
  token: 'tok-1',
  kind: 'movie',
  title: { id: 41, name: 'Heat', year: 1995, runtime_min: 170, poster_path: null, recall_aid: null },
  reason: 'queued because: you marked this seen and gave it no verdict',
  p_seen: 0.9,
  substituted_for: 'battle',
  verdict_labels: [
    [0, 'disliked'],
    [1, 'fine'],
    [2, 'liked']
  ],
  controls: ['verdict', 'not_seen', 'skip']
};

let fetchMock;
let target;
let app;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  // Module-level store, shared by every case in the file — the same reset `rank.svelte.test.js`
  // takes, for the same reason: without it a case asserts the previous case's envelope.
  rate.booted = false;
  rate.loading = true;
  rate.busy = false;
  rate.card = null;
  rate.drained = null;
  rate.session = null;
  rate.notice = '';
  rate.error = '';
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  vi.unstubAllGlobals();
  target.remove();
});

function respond(payload, status = 200) {
  fetchMock.mockResolvedValueOnce({
    ok: status < 400,
    status,
    headers: { get: () => null },
    text: async () => JSON.stringify(payload)
  });
}

/** A macrotask, not a counted number of microtask turns — `api.js` is three hops deep. */
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

/** Mount the page the way the router does, and let its `onMount(load)` land. */
async function open(payload) {
  respond(payload);
  app = mount(RatePage, { target });
  await settle();
}

describe("§6.1's empty state, by cause (finding 20, M410-D8-01)", () => {
  it('does not tell a person with no pairs that there is nothing left to queue', async () => {
    // The first-week path: Battle selected, zero verdicts, so no class holds two titles and no
    // pair can be drawn. `ensure_card` substitutes a sweep only when the mode is not `battle`,
    // so this is the one state where the surface has a cause and no card at all.
    await open(envelope({ drained: CAUSES.pool }));

    const block = target.querySelector(DRAINED);
    expect(block).toBeTruthy();
    expect(block.textContent).toContain('no new pair to compare yet');
    // The heading and the CTA are the milestone's own contradiction: the sweep queue is full.
    expect(block.querySelector('h2').textContent).not.toMatch(/left to queue/i);
    expect(block.textContent).not.toMatch(/Sharpen my ranking/);
  });

  it('sends a person with no pairs to Sweep rather than to an empty tier board', async () => {
    await open(envelope({ drained: CAUSES.pool }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('a[href="/rank"]')).toBeNull();

    // The CTA has to be the action the server's own sentence names ("Rate a few in Sweep"), and
    // on this surface that is a mode change rather than a link: proposal 36 makes the mode sticky
    // from an explicit change, which is exactly what this is.
    respond(envelope({ session: { mode: 'sweep' }, card: substitutedSweep }));
    block.querySelector('[data-testid="rate-drained-cta"]').click();
    await settle();

    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toContain('/api/rate/session');
    expect(JSON.parse(init.body).mode).toBe('sweep');
  });

  it("keeps proposal 37's end state for the queue that really is spent", async () => {
    // Proposal 37 was written for this cause and for no other: the queue drained, the ratings
    // already given still sharpenable, §6.3 the place that does it -- named by the control a
    // person taps there, never by its section number (decision 486).
    await open(envelope({ session: { mode: 'sweep' }, drained: CAUSES.queue }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('h2').textContent).toBe('Nothing left to queue');
    expect(block.textContent).toContain('"Sharpen my ranking" on the Rank page');
    expect(block.textContent).not.toMatch(/§|\bM[0-7]\b/);
    expect(block.querySelector('a[href="/rank"]')).toBeTruthy();
  });

  it('keeps it for the both-empty cause, where the queue is spent as well', async () => {
    await open(envelope({ drained: CAUSES.both }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('h2').textContent).toBe('Nothing left to queue');
    expect(block.querySelector('a[href="/rank"]')).toBeTruthy();
  });
});

describe('the substitution line (finding 21, M410-D8-07; C4.5 of the household test)', () => {
  it('prints nothing about the slot over a sweep card that stands in for a battle', async () => {
    // `substituted_for` stays on the wire, and the sweep card no longer reads it out: "a battle
    // was due in this slot" told a member about the block machine rather than about the film, and
    // on a searched-for title it answered the pick with an apology. The counter names the card's
    // own type, which is the one fact the person needs (decision 486).
    await open(envelope({ card: substitutedSweep }));

    expect(target.querySelector('[data-testid="rate-sweep-card"]')).toBeTruthy();
    expect(target.querySelector(SUBSTITUTED)).toBeNull();
    expect(target.textContent).not.toMatch(/was due in this slot/);
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toMatch(/· sweep$/);
  });

  it('says in plain words why a battle stands in for a sweep', async () => {
    const battle = {
      type: 'battle',
      token: 'tok-2',
      kind: 'movie',
      left: { id: 1, name: 'Heat', year: 1995, runtime_min: 170, outcome: 'A' },
      right: { id: 2, name: 'Drive', year: 2011, runtime_min: 100, outcome: 'B' },
      reason: 'queued because: you rated both liked · random pairs build your profile best',
      substituted_for: 'sweep',
      outcomes: ['A', 'B', 'TIE'],
      corrections: { label: 'not seen', sides: ['left', 'both', 'right'] },
      controls: ['duel', 'correction', 'skip']
    };
    await open(envelope({ card: battle }));

    const line = target.querySelector(SUBSTITUTED);
    expect(line.textContent).toContain("Nothing new to rate right now - comparing titles you've");
    expect(line.textContent).not.toMatch(/sweep queue|drained/);
  });
});

describe('"a title you know" (C5.2 of the household test)', () => {
  const hits = {
    q: 'heat',
    items: [
      { id: 41, kind: 'movie', name: 'Heat', year: 1995, rated: null, is_owned: true },
      { id: 42, kind: 'movie', name: 'The Heat', year: 2013, rated: 'fine', is_owned: false }
    ]
  };
  const other = { ...substitutedSweep, title: { ...substitutedSweep.title, id: 7, name: 'Else' } };

  it('finds a title, offers only the unrated one, and pins the pick into the queue', async () => {
    await open(envelope({ session: { mode: 'mix' }, card: other }));
    target.querySelector('[data-testid="rate-find-toggle"]').click();
    flushSync();

    const input = target.querySelector('[data-testid="rate-find-input"]');
    expect(input).toBeTruthy();
    respond(hits);
    input.value = 'heat';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    // The query waits a beat after the last keystroke, then lands.
    await new Promise((resolve) => setTimeout(resolve, 300));
    await settle();

    const [searchUrl] = fetchMock.mock.calls[1];
    expect(searchUrl).toContain('/api/rate/search?q=heat');
    const buttons = target.querySelectorAll('[data-testid="rate-find-hit"]');
    expect(buttons).toHaveLength(2);
    expect(buttons[0].disabled).toBe(false);
    expect(buttons[1].disabled).toBe(true);
    expect(buttons[1].textContent).toContain('you rated it fine');

    respond(envelope({ session: { mode: 'mix' }, card: { ...substitutedSweep, substituted_for: null } }));
    buttons[0].click();
    await settle();

    const [pinUrl] = fetchMock.mock.calls[2];
    expect(pinUrl).toContain('/api/rate?head=41');
    expect(target.querySelector('[data-testid="rate-card-title"]').textContent).toBe('Heat');
    expect(target.querySelector('[data-testid="rate-find"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-notice"]')).toBeNull();
  });

  it('says so when a pick could not be put on the table', async () => {
    await open(envelope({ session: { mode: 'mix' }, card: other }));

    // The server kept the card it had: the pick was rated a moment ago on another device.
    respond(envelope({ session: { mode: 'mix' }, card: other }));
    await rateTitle(hits.items[0]);
    await settle();
    expect(target.querySelector('[data-testid="rate-notice"]').textContent).toBe(
      "Heat can't be rated right now."
    );
  });
});
