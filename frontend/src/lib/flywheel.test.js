/**
 * @vitest-environment jsdom
 *
 * §6.6 Data's extraction queue: the selection, the quote it asks for and the rule that arms Launch.
 * Spec v2.1 §8.4, §6.6 Data; decisions 441, 442 and 443.
 *
 * Named BESIDE `test_flywheel_batch.py`, `test_flywheel_launch.py` and `20-admin-data.spec.js` on
 * the launch's coverage row, never instead of them (decision 226). What only this layer can hold is
 * the out-of-order case: a quote asked for one selection landing after the operator has moved on to
 * another, which the browser suite cannot produce on demand and which must never arm the button.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import FlywheelQueue from './components/FlywheelQueue.svelte';
import {
  PENDING,
  acceptQuote,
  defaultPlan,
  dollars,
  kindLabel,
  launchBody,
  launchState,
  orderedProviders,
  passChoices,
  queuedMarker,
  quoteKey,
  quotePath,
  roomLeft,
  titlesOf,
  toggle
} from './flywheel.svelte.js';

const NO_CAP = 'no spend cap is configured, and a paid stage never auto-retries past one';

const items = [
  { id: 11, kind: 'thin_facet', reason: 'no pacing term named', est_titles: 1, status: 'queued' },
  { id: 12, kind: 'thin_facet', reason: 'no era term named', est_titles: null, status: 'queued' },
  {
    id: 13,
    kind: 'empty_predicate',
    reason: 'no owned title carries robots',
    est_titles: 7,
    status: 'queued'
  }
];

describe('the selection', () => {
  it('toggles a row in and out as a new set', () => {
    const none = new Set();
    const one = toggle(none, 11);
    expect(one).not.toBe(none);
    expect([...one]).toEqual([11]);
    expect([...toggle(one, 11)]).toEqual([]);
  });

  it('counts the selected rows titles, a row with no figure as the one title it names', () => {
    expect(titlesOf(items, new Set())).toBe(0);
    expect(titlesOf(items, new Set([11]))).toBe(1);
    expect(titlesOf(items, new Set([11, 12]))).toBe(2);
    expect(titlesOf(items, new Set([11, 12, 13]))).toBe(9);
  });

  it('labels the two feeds whose producer is M6 as such', () => {
    expect(kindLabel('thin_facet')).toBe('thin facet');
    expect(kindLabel('empty_predicate')).toMatch(/M6/);
    expect(kindLabel('uncovered_frontier')).toMatch(/M6/);
  });
});

// M56-DATA-01: section 8.4 as v2.1.3 amends it puts each failure in the queue "with its reason and a
// 'queued just now' marker read off the row's own creation time" (decision 330, proposal 135's
// adjustment). The route carried `created_at` and the card dropped it, so a row written the moment a
// walk finished stage 8 looked like one queued weeks before.
describe('the queued marker', () => {
  const now = Date.parse('2026-09-24T12:00:00Z');

  it('reads queued just now off the rows own creation time, and its age after that', () => {
    expect(queuedMarker('2026-09-24T12:00:00+00:00', now)).toBe('queued just now');
    expect(queuedMarker('2026-09-24T11:59:01Z', now)).toBe('queued just now');
    expect(queuedMarker('2026-09-24T12:00:20Z', now), 'a server clock a little ahead').toBe(
      'queued just now'
    );
    expect(queuedMarker('2026-09-24T11:55:00Z', now)).toBe('queued 5 min ago');
    expect(queuedMarker('2026-09-24T11:00:00Z', now)).toBe('queued 1 hour ago');
    expect(queuedMarker('2026-09-24T09:00:00Z', now)).toBe('queued 3 hours ago');
    expect(queuedMarker('2026-09-20T12:00:00Z', now)).toBe('queued 4 days ago');
    expect(queuedMarker(null, now), 'no time, no marker').toBe(null);
    expect(queuedMarker('not a time', now)).toBe(null);
  });
});

// M56-DATA-02: the cap the selection is held against, stated from the reading in hand. A quote for
// the selection on screen is one; the queue's own `meter` is the other, and "no cap" is said only by
// a reading whose cap is null - never by the absence of a reading.
describe('the room left this month', () => {
  it('states the cap from the quote, else from the queue read, and no cap only when none is set', () => {
    const quoted = { cap_usd: '5.00', remaining_usd: '1.25' };
    const meter = { cap_usd: '5.00', remaining_usd: '3.75' };
    expect(roomLeft(quoted, meter)).toBe('$1.25 of $5.00');
    expect(roomLeft(null, meter)).toBe('$3.75 of $5.00');
    expect(roomLeft(null, { cap_usd: null, remaining_usd: null })).toBe('no cap');
    expect(roomLeft({ cap_usd: null, remaining_usd: null }, meter)).toBe('no cap');
    expect(roomLeft(null, {}), 'a reading that says nothing about the cap').toBe('unknown');
    expect(roomLeft(null, null)).toBe('unknown');
  });
});

describe('the plan and the quote it asks for', () => {
  it("starts from the stored plan, or from no provider and one pass when it cannot be made", () => {
    expect(defaultPlan({ defaults: { providers: ['gemini'], passes: 2 } })).toEqual({
      providers: ['gemini'],
      passes: 2
    });
    expect(defaultPlan({ defaults: { providers: null, passes: null, reason: 'no key' } })).toEqual({
      providers: [],
      passes: 1
    });
    expect(passChoices(7)).toContain(7);
    expect(passChoices(2)).toEqual([1, 2, 3, 4, 5]);
  });

  it('spells one selection one way whatever order the providers were ticked in', () => {
    const listed = [{ name: 'gemini' }, { name: 'anthropic' }, { name: 'openai' }];
    expect(orderedProviders(listed, ['openai', 'gemini'])).toEqual(['gemini', 'openai']);
    expect(quoteKey({ titles: 2, providers: ['gemini', 'openai'], passes: 1 })).toBe('2|gemini,openai|1');
  });

  it('asks the quote route with every parameter present, an empty provider list included', () => {
    expect(quotePath({ titles: 2, providers: ['gemini', 'anthropic'], passes: 2 })).toBe(
      '/admin/flywheel/quote?titles=2&providers=gemini%2Canthropic&passes=2'
    );
    expect(quotePath({ titles: 0, providers: [], passes: 1 })).toBe(
      '/admin/flywheel/quote?titles=0&providers=&passes=1'
    );
  });

  it('keeps a quote only when it answers the selection on screen now', () => {
    const asked = { titles: 1, providers: ['gemini'], passes: 1 };
    const now = { titles: 2, providers: ['gemini'], passes: 1 };
    expect(acceptQuote(now, asked, { launchable: true })).toBe(null);
    expect(acceptQuote(asked, asked, { launchable: true })).toEqual({
      key: '1|gemini|1',
      body: { launchable: true }
    });
  });
});

describe('Launch', () => {
  const key = '2|gemini|1';

  it('is dark with a reason until the quote for this selection is in hand', () => {
    expect(launchState(null, key, false)).toEqual({ disabled: true, reason: PENDING });
    const stale = { key: '1|gemini|1', body: { launchable: true, reason: null } };
    expect(launchState(stale, key, false)).toEqual({ disabled: true, reason: PENDING });
  });

  it("is dark with the server's own sentence whenever the quote refuses", () => {
    const refused = { key, body: { launchable: false, reason: NO_CAP } };
    expect(launchState(refused, key, false)).toEqual({ disabled: true, reason: NO_CAP });
    const silent = { key, body: {} };
    expect(launchState(silent, key, false).disabled).toBe(true);
  });

  it('presses when this selection is launchable, and not while a launch is in flight', () => {
    const fits = { key, body: { launchable: true, reason: null } };
    expect(launchState(fits, key, false)).toEqual({ disabled: false, reason: null });
    expect(launchState(fits, key, true).disabled).toBe(true);
  });

  it('posts exactly the selected ids, the providers and the passes, and no figure', () => {
    const body = launchBody(new Set([13, 11]), { providers: ['gemini'], passes: 2, titles: 8 });
    expect(body).toEqual({ item_ids: [11, 13], providers: ['gemini'], passes: 2 });
  });

  it("prints the server's decimal strings and does no arithmetic on them", () => {
    expect(dollars('0.012000')).toBe('$0.012000');
    expect(dollars(null)).toBe('unknown');
    expect(dollars(null, 'no cap')).toBe('no cap');
  });
});

describe('the mounted queue', () => {
  let target;

  beforeEach(() => {
    target = document.createElement('div');
    document.body.appendChild(target);
    vi.mocked(get).mockReset();
    vi.mocked(post).mockReset();
  });

  afterEach(() => {
    target.remove();
  });

  const envelope = (over = {}) => ({
    items,
    providers: [
      { name: 'gemini', configured: true, reason: null },
      { name: 'anthropic', configured: false, reason: 'the anthropic connector holds no API key' }
    ],
    defaults: { providers: ['gemini'], passes: 1, reason: null },
    meter: {},
    input_tokens_assumed: 6000,
    ...over
  });

  const quote = (over = {}) => ({
    titles: 0,
    providers: ['gemini'],
    passes: 1,
    per_title_usd: '0.010000',
    total_usd: '0.000000',
    reserved_usd: '0.000000',
    cap_usd: '5.00',
    remaining_usd: '5.00',
    launchable: false,
    reason: 'select rows first',
    ...over
  });

  const flush = async (ms = 0) => {
    if (ms) await new Promise((resolve) => setTimeout(resolve, ms));
    for (let i = 0; i < 40; i++) await Promise.resolve();
    flushSync();
  };
  const launchButton = () => target.querySelector('[data-testid="flywheel-launch"]');
  const reasonText = () => target.querySelector('[data-testid="flywheel-launch-reason"]')?.textContent;
  const tick = (id) => {
    const box = target.querySelector(`input[aria-label="Select row ${id}"]`);
    box.click();
  };

  it('keeps Launch dark with the reason as text, even over an empty queue', async () => {
    vi.mocked(get).mockImplementation(async (path) =>
      path === '/admin/flywheel'
        ? envelope({ items: [] })
        : quote({ cap_usd: null, remaining_usd: null, reason: NO_CAP })
    );
    const app = mount(FlywheelQueue, { target, props: {} });
    await flush(260);
    try {
      expect(launchButton().disabled).toBe(true);
      expect(reasonText()).toBe(NO_CAP);
      expect(vi.mocked(get)).toHaveBeenCalledWith(
        '/admin/flywheel/quote?titles=0&providers=gemini&passes=1'
      );
      const anthropic = [...target.querySelectorAll('.provider input')][1];
      expect(anthropic.disabled, 'a provider with no key is not a choice').toBe(true);
      expect(target.textContent).toContain('the anthropic connector holds no API key');
    } finally {
      unmount(app);
    }
  });

  it('never arms Launch with a quote for a selection that is no longer on screen', async () => {
    const pending = [];
    vi.mocked(get).mockImplementation((path) => {
      if (path === '/admin/flywheel') return Promise.resolve(envelope());
      return new Promise((resolve) => pending.push({ path, resolve }));
    });
    const app = mount(FlywheelQueue, { target, props: {} });
    await flush(260);
    try {
      expect(pending.map((p) => p.path)).toEqual([
        '/admin/flywheel/quote?titles=0&providers=gemini&passes=1'
      ]);
      tick(11);
      await flush(260);
      expect(pending[1].path).toBe('/admin/flywheel/quote?titles=1&providers=gemini&passes=1');
      pending[1].resolve(quote({ titles: 1, launchable: false, reason: 'over spend cap: not this one' }));
      await flush();
      // The answer for the empty selection lands last and says launchable: it is not this batch's.
      pending[0].resolve(quote({ titles: 0, launchable: true, reason: null }));
      await flush();
      expect(launchButton().disabled).toBe(true);
      expect(reasonText()).toBe('over spend cap: not this one');
    } finally {
      unmount(app);
    }
  });

  it('asks again when the passes change, and launches exactly the rows ticked', async () => {
    vi.mocked(get).mockImplementation(async (path) => {
      if (path === '/admin/flywheel') return envelope();
      const passes = Number(new URL(`http://x${path}`).searchParams.get('passes'));
      const titles = Number(new URL(`http://x${path}`).searchParams.get('titles'));
      const reserved_usd = `0.0${2 * passes}0000`;
      return quote({ titles, passes, reserved_usd, launchable: true, reason: null });
    });
    vi.mocked(post).mockResolvedValue({ batch: { id: 5 }, items: [11, 12] });
    const app = mount(FlywheelQueue, { target, props: {} });
    await flush(260);
    try {
      tick(11);
      tick(12);
      await flush(260);
      expect(target.querySelector('[data-testid="flywheel-reserved"]').textContent).toBe('$0.020000');
      const select = target.querySelector('.passes select');
      select.value = '2';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      await flush(260);
      expect(vi.mocked(get)).toHaveBeenLastCalledWith(
        '/admin/flywheel/quote?titles=2&providers=gemini&passes=2'
      );
      expect(target.querySelector('[data-testid="flywheel-reserved"]').textContent).toBe('$0.040000');
      expect(launchButton().disabled).toBe(false);
      launchButton().click();
      await flush();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/admin/flywheel/launch', {
        item_ids: [11, 12],
        providers: ['gemini'],
        passes: 2
      });
    } finally {
      unmount(app);
    }
  });

  // M56-DATA-01: the marker is on the card, per row, read off each row's own `created_at`.
  it('marks a row queued just now and an older row with its age, off each rows own time', async () => {
    const fresh = new Date().toISOString();
    const older = new Date(Date.now() - 5 * 60_000 - 5_000).toISOString();
    vi.mocked(get).mockImplementation(async (path) =>
      path === '/admin/flywheel'
        ? envelope({
            items: [
              { ...items[0], created_at: fresh },
              { ...items[1], created_at: older }
            ]
          })
        : quote()
    );
    const app = mount(FlywheelQueue, { target, props: {} });
    await flush(260);
    try {
      const markers = [...target.querySelectorAll('[data-testid="flywheel-queued"]')];
      expect(markers.map((m) => m.textContent)).toEqual(['queued just now', 'queued 5 min ago']);
    } finally {
      unmount(app);
    }
  });

  // M56-DATA-02: with no quote for the selection in hand - the debounce and round trip after every
  // tap, or for good while the quote route fails - the card read "no cap of no cap" over a cap of
  // five dollars. The queue's own read carries the meter, and that is what it states then.
  it('states the cap from the queue read while no quote for the selection is in hand', async () => {
    const meter = { cap_usd: '5.00', remaining_usd: '3.75', spent_usd: '1.25', unsettled_usd: '0' };
    vi.mocked(get).mockImplementation(async (path) => {
      if (path === '/admin/flywheel') return envelope({ meter });
      throw new Error('the quote route is down');
    });
    const app = mount(FlywheelQueue, { target, props: {} });
    await flush(260);
    try {
      const room = [...target.querySelectorAll('.figures div')]
        .find((d) => d.querySelector('dt').textContent === 'left this month')
        .querySelector('dd').textContent;
      expect(room).toBe('$3.75 of $5.00');
      expect(target.textContent).not.toContain('no cap');
      expect(launchButton().disabled, 'no quote in hand, so nothing arms Launch').toBe(true);
    } finally {
      unmount(app);
    }
  });
});
