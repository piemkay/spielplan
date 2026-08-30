import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  HOLD_MS,
  commit,
  counterLine,
  hueOf,
  kindLabel,
  metaLine,
  rate,
  revealLine,
  runtimeLabel,
  setHead,
  sharePct,
  undoMessage,
  load,
  verdict,
  undo,
  skip,
  reset
} from './rate.svelte.js';

const envelope = (over = {}) => ({
  session: {
    id: 1,
    mode: 'mix',
    kinds: ['movie', 'series'],
    decisive: false,
    block: { index: 0, slot: 1, size: 15, counter: '1 / 15', serving: 'sweep' }
  },
  card: {
    type: 'sweep',
    token: 't1',
    kind: 'movie',
    title: { id: 1, kind: 'movie', name: 'Heat', year: 1995, runtime_min: 170 },
    reason: 'queued because: 72% likely you have seen it',
    p_seen: 0.72,
    substituted_for: null,
    verdict_labels: [
      [0, 'disliked'],
      [1, 'fine'],
      [2, 'liked']
    ],
    controls: ['verdict', 'not_seen', 'skip']
  },
  drained: null,
  class_balance: {
    counts: [1, 2, 3],
    shares: [1 / 6, 2 / 6, 3 / 6],
    labels: ['disliked', 'fine', 'liked'],
    total: 6,
    warn: false,
    copy: null,
    threshold: 0.6
  },
  undo: { available: false, kind: null, reason: 'empty' },
  reveal: null,
  ledger: null,
  log: [],
  ...over
});

function ok(body) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers(),
    text: async () => JSON.stringify(body)
  };
}

function conflict(detail) {
  return {
    ok: false,
    status: 409,
    statusText: 'Conflict',
    headers: new Headers(),
    text: async () => JSON.stringify({ detail })
  };
}

describe('pure helpers', () => {
  it('names the active partition the way the counter does', () => {
    expect(kindLabel(['movie'])).toBe('film');
    expect(kindLabel(['series'])).toBe('series');
    expect(kindLabel(['movie', 'series'])).toBe('film + series');
    expect(kindLabel([])).toBe('');
  });

  it('builds §6.1 counter with proposal 46 partition and the card type', () => {
    // Decision 35: this is the number Undo's depth is measured in, so it is the server's own
    // `counter` string with context appended, never a recomputation.
    expect(
      counterLine({ counter: '7 / 15', serving: 'sweep' }, ['movie'])
    ).toBe('7 / 15 this block · film · sweep');
    expect(counterLine(null, ['movie'])).toBe('');
  });

  it('formats runtime per kind and joins the meta line without stray spacing', () => {
    expect(runtimeLabel({ kind: 'movie', runtime_min: 170 })).toBe('2h 50m');
    expect(runtimeLabel({ kind: 'series', runtime_min: 48 })).toBe('48m/ep');
    expect(runtimeLabel({ kind: 'movie' })).toBe(null);
    expect(metaLine({ kind: 'movie', year: 1995, runtime_min: 170 })).toBe('1995 · 2h 50m');
    expect(metaLine({ kind: 'movie', runtime_min: 170 })).toBe('— · 2h 50m');
  });

  it('gives the same title the same hue every render', () => {
    expect(hueOf('Heat')).toBe(hueOf('Heat'));
    expect(hueOf('Heat')).not.toBe(hueOf('Prisoners'));
  });

  it('rounds shares for the three-segment bar', () => {
    expect(sharePct(0.6667)).toBe(67);
    expect(sharePct(undefined)).toBe(0);
  });

  it('explains a disabled Undo rather than leaving it silent', () => {
    expect(undoMessage({ available: true })).toBe('');
    expect(undoMessage({ available: false, reason: 'empty' })).toBe(
      'nothing to undo in this block'
    );
    expect(undoMessage({ available: false, reason: 'block_boundary' })).toBe(
      'undo reaches back to the start of this block of 15 and no further'
    );
  });

  it('suppresses the reveal rather than banding it before the first fit', () => {
    // Proposal 153: a guess drawn from someone else's thresholds is not a prediction.
    expect(revealLine(null)).toBe(null);
    expect(revealLine({ available: false, reason: 'no fitted ranking yet' })).toEqual({
      available: false,
      text: 'no fitted ranking yet',
      agreed: false
    });
    expect(
      revealLine({ available: true, agreed: true, text: "we'd have guessed the same · cdf 0.71" })
    ).toEqual({ available: true, agreed: true, text: "we'd have guessed the same · cdf 0.71" });
  });

  it('keeps only numeric head ids, so the banner CTA cannot send nonsense', () => {
    expect(setHead(['4', 9, 'x', null])).toEqual([4, 9]);
    expect(setHead([])).toEqual([]);
  });
});

describe('the envelope', () => {
  // Held separately from `globalThis.fetch` so the mock keeps its vitest shape: assigning it to
  // the global narrows it to the DOM `fetch` signature, and every `mockResolvedValue` below
  // then reads as an error to the type checker.
  /** @type {any} */
  let fetchMock;

  beforeEach(async () => {
    vi.useFakeTimers();
    reset();
    fetchMock = vi.fn().mockResolvedValue(ok(envelope()));
    globalThis.fetch = fetchMock;
    await load();
    fetchMock.mockClear();
  });

  afterEach(() => {
    reset();
    vi.useRealTimers();
    delete globalThis.fetch;
  });

  it('opens on the served card with its counter, balance and undo state', () => {
    expect(rate.card.token).toBe('t1');
    expect(rate.session.block.counter).toBe('1 / 15');
    expect(rate.balance.total).toBe(6);
    expect(rate.undo).toEqual({ available: false, kind: null, reason: 'empty' });
    expect(rate.reveal).toBe(null);
  });

  it('holds the answered card and its counter while the reveal shows, then swaps in the preloaded one', async () => {
    // Proposal 42: the reveal is attached to the card just rated, for ~1.2 s. The next card is
    // already in this response, so the swap costs no request.
    fetchMock.mockResolvedValue(
      ok(
        envelope({
          session: {
            ...envelope().session,
            block: { index: 0, slot: 2, size: 15, counter: '2 / 15', serving: 'battle' }
          },
          card: { ...envelope().card, token: 't2', title: { ...envelope().card.title, id: 2 } },
          reveal: { available: true, agreed: true, text: "we'd have guessed the same · cdf 0.71" },
          undo: { available: true, kind: 'verdict', reason: null }
        })
      )
    );
    await verdict(2);

    expect(rate.holding).toBe(true);
    expect(rate.card.token).toBe('t1');                 // still the card just rated
    expect(rate.frozenBlock.counter).toBe('1 / 15');    // and its counter
    expect(rate.reveal.text).toContain("we'd have guessed");
    expect(rate.undo.available).toBe(true);             // Undo is reachable immediately

    vi.advanceTimersByTime(HOLD_MS);
    expect(rate.holding).toBe(false);
    expect(rate.card.token).toBe('t2');
    expect(rate.frozenBlock).toBe(null);
    expect(rate.reveal).toBe(null);
  });

  it('will not answer a card it is only showing', async () => {
    fetchMock.mockResolvedValue(
      ok(envelope({ reveal: { available: false, reason: 'no fit yet' } }))
    );
    await verdict(2);
    fetchMock.mockClear();

    // Mid-hold the strip is the reveal, not the buttons — and the token on screen is spent.
    await verdict(0);
    await skip();
    expect(fetchMock).not.toHaveBeenCalled();

    expect(commit()).toBe(true);
    expect(commit()).toBe(false);
  });

  it('reports a stale card and re-reads the table instead of guessing', async () => {
    fetchMock
      .mockResolvedValueOnce(conflict({ reason: 'stale_card', message: 'that card has already been answered' }))
      .mockResolvedValueOnce(ok(envelope({ card: { ...envelope().card, token: 't9' } })));
    await verdict(1);
    expect(rate.notice).toBe('that card has already been answered');
    expect(rate.card.token).toBe('t9');
    expect(rate.error).toBe('');
  });

  it('surfaces the block boundary as a reason rather than a silent no-op', async () => {
    fetchMock
      .mockResolvedValueOnce(
        conflict({
          reason: 'block_boundary',
          message: 'undo reaches back to the start of this block of 15 and no further'
        })
      )
      .mockResolvedValueOnce(ok(envelope()));
    await undo();
    expect(rate.notice).toMatch(/no further/);
  });

  it('drops a held reveal when Undo takes the observation back', async () => {
    fetchMock.mockResolvedValue(
      ok(envelope({ reveal: { available: true, agreed: false, text: "we'd have guessed fine" } }))
    );
    await verdict(2);
    expect(rate.holding).toBe(true);

    fetchMock.mockResolvedValue(ok(envelope({ card: { ...envelope().card, token: 't1' } })));
    await undo();
    expect(rate.holding).toBe(false);
    expect(rate.reveal).toBe(null);
    expect(rate.card.token).toBe('t1');
  });
});
