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
import { rate } from '$lib/rate.svelte.js';

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
      'A battle compares two titles you have already rated the same way, and there are not two ' +
      'of them yet. Rate a few in Sweep and the pairs start arriving.'
  },
  both: {
    cause: 'both',
    text:
      "You've rated everything we can queue right now, and no two of your ratings sit in the " +
      'same band, so there is no pair left to compare either.'
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
    expect(block.textContent).toContain('there are not two of them yet');
    // The heading and the CTA are the milestone's own contradiction: the sweep queue is full.
    expect(block.querySelector('h2').textContent).not.toMatch(/left to queue/i);
    expect(block.textContent).not.toMatch(/comparison queue sharpens/);
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
    // already given still sharpenable, §6.3 the place that does it.
    await open(envelope({ session: { mode: 'sweep' }, drained: CAUSES.queue }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('h2').textContent).toBe('Nothing left to queue');
    expect(block.textContent).toContain('comparison queue sharpens');
    expect(block.querySelector('a[href="/rank"]')).toBeTruthy();
  });

  it('keeps it for the both-empty cause, where the queue is spent as well', async () => {
    await open(envelope({ drained: CAUSES.both }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('h2').textContent).toBe('Nothing left to queue');
    expect(block.querySelector('a[href="/rank"]')).toBeTruthy();
  });
});

describe('the substitution line (finding 21, M410-D8-07)', () => {
  it('says the counter wanted a battle without claiming the pool is empty', async () => {
    // `substituted_for` carries the TYPE the counter called for and nothing about why the flip
    // happened. Until M4.10 one site set it — the thin-pool substitution — so the line could name
    // that cause and be right; now the §6.0 banner's head redraw and both correction fallbacks set
    // it too, and on the banner path the battle pool can be demonstrably full.
    await open(envelope({ card: substitutedSweep }));

    const line = target.querySelector(SUBSTITUTED);
    expect(line).toBeTruthy();
    expect(line.textContent).toMatch(/battle/);
    expect(line.textContent).toMatch(/sweep/);
    expect(line.textContent).not.toMatch(/no battle pair|not two|pool/i);
  });

  it('says nothing at all when the card is the one the counter called for', async () => {
    await open(envelope({ card: { ...substitutedSweep, substituted_for: null } }));
    expect(target.querySelector(SUBSTITUTED)).toBeNull();
  });
});
