/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The page reads `$page.url` synchronously at init, so the store answers on subscribe.
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

/** `rate/session.DRAINED_CAUSES`, verbatim. */
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
  // The store is module state shared by every case, so reset it.
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

// A macrotask, not counted microtasks: `api.js` is three hops deep.
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function open(payload) {
  respond(payload);
  app = mount(RatePage, { target });
  await settle();
}

describe("§6.1's empty state, by cause (finding 20, M410-D8-01)", () => {
  it('does not tell a person with no pairs that there is nothing left to queue', async () => {
    // Battle selected with zero verdicts: the one state with a cause and no card at all.
    await open(envelope({ drained: CAUSES.pool }));

    const block = target.querySelector(DRAINED);
    expect(block).toBeTruthy();
    expect(block.textContent).toContain('no new pair to compare yet');
    expect(block.querySelector('h2').textContent).not.toMatch(/left to queue/i);
    expect(block.textContent).not.toMatch(/Sharpen my ranking/);
  });

  it('sends a person with no pairs to Sweep rather than to an empty tier board', async () => {
    await open(envelope({ drained: CAUSES.pool }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('a[href="/rank"]')).toBeNull();

    // The CTA is a mode change, not a link: the server's sentence names Sweep.
    respond(envelope({ session: { mode: 'sweep' }, card: substitutedSweep }));
    block.querySelector('[data-testid="rate-drained-cta"]').click();
    await settle();

    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toContain('/api/rate/session');
    expect(JSON.parse(init.body).mode).toBe('sweep');
  });

  it("keeps proposal 37's end state for the queue that really is spent", async () => {
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
    await open(envelope({ card: substitutedSweep }));

    expect(target.querySelector('[data-testid="rate-sweep-card"]')).toBeTruthy();
    expect(target.querySelector(SUBSTITUTED)).toBeNull();
    expect(target.textContent).not.toMatch(/was due in this slot/);
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toMatch(/· Pairs$/);
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

describe('the second household test on Rate (2026-09-26)', () => {
  const pairCard = {
    type: 'battle',
    token: 'tok-3',
    kind: 'movie',
    left: { id: 1, name: 'Heat', year: 1995, runtime_min: 170, outcome: 'A' },
    right: { id: 2, name: 'Drive', year: 2011, runtime_min: 100, outcome: 'B' },
    reason: 'You rated both of these liked.',
    substituted_for: null,
    outcomes: ['A', 'B', 'TIE'],
    corrections: { label: 'not seen', sides: ['left', 'both', 'right'] },
    controls: ['duel', 'correction', 'skip']
  };
  const mixed = (over = {}) =>
    envelope({
      session: {
        id: 7,
        mode: 'mix',
        kinds: ['movie', 'series'],
        decisive: false,
        block: { index: 0, slot: 4, size: 15, counter: '4 / 15', serving: 'battle' }
      },
      ...over
    });

  it('names the modes plainly and puts the chosen one in the header (A2, A3, policy h)', async () => {
    await open(mixed({ card: pairCard }));

    const pills = [...target.querySelectorAll('[data-testid^="rate-mode-"]')].filter(
      (el) => el.tagName === 'BUTTON'
    );
    expect(pills.map((el) => el.textContent.trim())).toEqual(['Mixed', 'Singles', 'Pairs']);
    expect(target.querySelector('[data-testid="rate-mode-mix"]').getAttribute('aria-pressed')).toBe(
      'true'
    );
    // The card on the table is a pair, and the header still says what was chosen.
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toBe(
      '4 / 15 this block · film + series · Mixed'
    );
    expect(target.querySelector('[data-testid="rate-mode-note"]').textContent).toMatch(/^Mixed: /);
    expect(target.textContent).not.toMatch(/\b(sweep|battle)\b/i);
  });

  it('asks the pair question and says the clear-favourite switch is for this pair (A1, A6)', async () => {
    await open(mixed({ card: pairCard }));

    expect(target.querySelector('[data-testid="rate-battle-question"]').textContent).toBe(
      'Which did you enjoy more?'
    );
    expect(target.querySelector('[data-testid="rate-decisive"]').textContent).toContain(
      'clear favourite'
    );
    expect(target.querySelector('[data-testid="rate-decisive-why"]').textContent).toMatch(
      /resets for the next pair/
    );
    expect(target.querySelector('[data-testid="rate-battle-reason"]').textContent).toBe(
      'You rated both of these liked.'
    );
  });

  it('lights the answer in flight while the rest of the card waits (A4)', async () => {
    await open(mixed({ card: { ...substitutedSweep, substituted_for: null } }));

    /** @type {(response: any) => void} */
    let answer = () => {};
    fetchMock.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    target.querySelector('[data-testid="rate-verdict-2"]').click();
    flushSync();

    const liked = target.querySelector('[data-testid="rate-verdict-2"]');
    expect(liked.classList.contains('picked')).toBe(true);
    expect(liked.getAttribute('aria-busy')).toBe('true');
    expect(target.querySelector('[data-testid="rate-verdict-0"]').classList.contains('picked')).toBe(
      false
    );

    answer({
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: async () => JSON.stringify(mixed({ card: pairCard, reveal: null }))
    });
    await settle();
    expect(target.querySelector('.picked')).toBeNull();
  });

  it('names the kind the spread counts when only one is selected (A5)', async () => {
    await open(
      envelope({
        session: {
          id: 7,
          mode: 'sweep',
          kinds: ['series'],
          decisive: false,
          block: { index: 0, slot: 2, size: 15, counter: '2 / 15', serving: 'sweep' }
        },
        card: { ...substitutedSweep, substituted_for: null },
        class_balance: { ...envelope().class_balance, counts: [2, 3, 4], total: 9 }
      })
    );
    expect(target.querySelector('[data-testid="rate-balance-total"]').textContent).toBe(
      '9 series ratings'
    );
    expect(target.querySelector('[data-testid="rate-label-count"]').textContent).toContain(
      '9 series ratings'
    );
  });
});
