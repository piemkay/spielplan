import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  DECISIVE_COPY,
  DECISIVE_LABEL,
  HOLD_MS,
  LEARNING_CURVE_COPY,
  MODES,
  PAIR_QUESTION,
  PAIR_SELECTION_COPY,
  UNDO_KIND_LABELS,
  modeName,
  commit,
  counterLine,
  findTitles,
  finder,
  hueOf,
  kindLabel,
  metaLine,
  pendingHead,
  rate,
  rateTitle,
  ratingsLabel,
  revealLine,
  runtimeLabel,
  setHead,
  sharePct,
  undoKindLabel,
  undoMessage,
  load,
  verdict,
  undo,
  skip,
  duel,
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

  it('builds §6.1 counter with proposal 46 partition and the mode that was chosen', () => {
    // The server's own counter with context appended, never recomputed: Undo's depth is counted in it.
    expect(
      counterLine({ counter: '7 / 15', serving: 'battle' }, ['movie'], 'mix')
    ).toBe('7 / 15 this block · film · Mixed');
    expect(counterLine({ counter: '2 / 15', serving: 'sweep' }, ['series'], 'battle')).toBe(
      '2 / 15 this block · series · Pairs'
    );
    expect(counterLine(null, ['movie'], 'mix')).toBe('');
  });

  it('formats runtime per kind and joins the meta line without stray spacing', () => {
    expect(runtimeLabel({ kind: 'movie', runtime_min: 170 })).toBe('2h 50m');
    expect(runtimeLabel({ kind: 'series', runtime_min: 48 })).toBe('48m/ep');
    expect(runtimeLabel({ kind: 'movie' })).toBe(null);
    expect(metaLine({ kind: 'movie', year: 1995, runtime_min: 170 })).toBe('1995 · 2h 50m');
    expect(metaLine({ kind: 'movie', runtime_min: 170 })).toBe('— · 2h 50m');
  });

  it('says 45m rather than 0h 45m, on every surface that asks', () => {
    expect(runtimeLabel({ kind: 'movie', runtime_min: 45 })).toBe('45m');
    expect(runtimeLabel({ kind: 'movie', runtime_min: 59 })).toBe('59m');
    // The hour boundary is where an `h ? ...` branch goes wrong in the other direction.
    expect(runtimeLabel({ kind: 'movie', runtime_min: 60 })).toBe('1h 0m');
    // A series is per-episode whatever its length: the kind branch outranks the zero-hour one.
    expect(runtimeLabel({ kind: 'series', runtime_min: 45 })).toBe('45m/ep');
    expect(runtimeLabel({ kind: 'series', runtime_min: 170 })).toBe('170m/ep');
    expect(runtimeLabel(null)).toBe(null);
    expect(metaLine({ kind: 'movie', year: 2019, runtime_min: 45 })).toBe('2019 · 45m');
  });

  it('gives the same title the same hue every render', () => {
    // Pinned to a literal: the hue must be stable across releases, not only equal to itself.
    expect(hueOf('Heat')).toBe(179);
    expect(hueOf('Prisoners')).toBe(54);
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
  // Kept off `globalThis.fetch`'s DOM type so the mock keeps its vitest shape for the type checker.
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
    // The next card is already in this response, so the swap costs no request.
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

  it('says which answer is in flight until the server has taken it', async () => {
    /** @type {(response: any) => void} */
    let answer = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const tapped = verdict(2);
    expect(rate.busy).toBe(true);
    expect(rate.pending).toBe('verdict-2');
    answer(ok(envelope({ reveal: { available: false, reason: 'no guess yet' } })));
    await tapped;
    expect(rate.pending).toBe(null);
    commit();

    fetchMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const picked = duel('TIE');
    expect(rate.pending).toBe('duel-TIE');
    answer(conflict({ reason: 'stale_card', message: 'that card has already been answered' }));
    fetchMock.mockResolvedValue(ok(envelope()));
    await picked;
    expect(rate.pending).toBe(null);
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

  it('answers the card the gesture started on, or no card at all', async () => {
    // A long press is a delayed write, and `load()` can swap the card within those 500ms.
    fetchMock.mockResolvedValue(ok(envelope()));
    await duel('A', { decisive: true, token: 't1' });
    const sent = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(sent.card_token, 'the pressed token is the one that is answered').toBe('t1');
    expect(sent.decisive).toBe(true);

    // Now the card moves under the press, exactly as a reload would move it.
    fetchMock.mockResolvedValueOnce(ok(envelope({ card: { ...envelope().card, token: 't2' } })));
    await load({ quiet: true });
    expect(rate.card.token).toBe('t2');
    fetchMock.mockClear();

    await duel('A', { decisive: true, token: 't1' });
    expect(fetchMock, 'the long press answered the card that replaced the pressed one')
      .not.toHaveBeenCalled();
  });

  it('still answers the live card when the caller names no token', async () => {
    // The strip buttons and the keyboard capture no token; absent means the card on the table.
    fetchMock.mockResolvedValue(ok(envelope()));
    await duel('TIE');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).card_token).toBe('t1');
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

  it('drops a pin once its card is on the table, so a skip does not bring it straight back', async () => {
    // The server serves a pin even over a skip, so an answered pin would come straight back.
    setHead([1, 9]);
    fetchMock.mockResolvedValue(ok(envelope()));
    await load({ quiet: true });
    expect(pendingHead()).toEqual([9]);

    fetchMock.mockClear();
    await skip();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).head).toEqual([9]);
    setHead([]);
  });
});

describe('the member register (decisions 486 and 491)', () => {
  it('names the undo in words and keeps the raw kind for the data attribute', () => {
    expect(undoKindLabel('verdict')).toBe('rating');
    expect(undoKindLabel('not_seen')).toBe('not seen');
    expect(undoKindLabel('correction')).toBe('not seen');
    expect(undoKindLabel('duel')).toBe('pick');
    expect(undoKindLabel('tie')).toBe('tie');
    expect(undoKindLabel('skip')).toBe('skip');
    expect(undoKindLabel(null)).toBe('');
    expect(Object.values(UNDO_KIND_LABELS).join(' ')).not.toMatch(/_/);
  });

  it('counts ratings, one or many', () => {
    expect(ratingsLabel(1)).toBe('1 rating');
    expect(ratingsLabel(0)).toBe('0 ratings');
    expect(ratingsLabel(15)).toBe('15 ratings');
  });

  it('names the kind it counts when only one kind is selected', () => {
    // The count is per kind on purpose: each kind is its own model.
    expect(ratingsLabel(9, ['series'])).toBe('9 series ratings');
    expect(ratingsLabel(1, ['movie'])).toBe('1 film rating');
    expect(ratingsLabel(58, ['movie', 'series'])).toBe('58 ratings');
  });

  it('carries no section number, milestone or model noun in the copy it ships', () => {
    const shipped = [
      PAIR_SELECTION_COPY,
      LEARNING_CURVE_COPY,
      DECISIVE_COPY,
      DECISIVE_LABEL,
      PAIR_QUESTION,
      ...MODES.flatMap(([, name, why]) => [name, why])
    ];
    for (const line of shipped) {
      expect(line).not.toMatch(/§|decision \d|proposal \d|\bM[0-7]\b/);
      expect(line).not.toMatch(/\blabels?\b|\bcdf\b|\bledger\b|\bmargin\b/);
    }
    expect(PAIR_SELECTION_COPY).toContain('Sharpen my ranking');
    expect(LEARNING_CURVE_COPY).toContain('ratings');
    expect(MODES.find(([key]) => key === 'mix')[2]).toContain('the pairs start at 15 ratings');
  });

  it('names the modes by what they ask, and the pair card asks its question', () => {
    // The keys stay the wire's (decision 519).
    expect(MODES.map(([key]) => key)).toEqual(['mix', 'sweep', 'battle']);
    expect(MODES.map(([key]) => modeName(key))).toEqual(['Mixed', 'Singles', 'Pairs']);
    for (const [, name, why] of MODES) {
      expect(`${name} ${why}`).not.toMatch(/\b(sweep|battle)\b/i);
    }
    expect(PAIR_QUESTION).toBe('Which did you enjoy more?');
    expect(DECISIVE_COPY).toMatch(/resets for the next pair/);
    expect(DECISIVE_COPY).not.toMatch(/decisive|hesitant|teaches/);
  });
});

describe('"a title you know" (C5.2)', () => {
  /** @type {any} */
  let fetchMock;

  beforeEach(() => {
    reset();
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock;
  });

  afterEach(() => {
    reset();
    setHead([]);
    delete globalThis.fetch;
  });

  it('asks nothing for one character, and lets the newest query win', async () => {
    await findTitles('h');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(finder.items).toEqual([]);

    /** @type {(value: any) => void} */
    let answerSlow = () => {};
    fetchMock
      .mockReturnValueOnce(new Promise((resolve) => (answerSlow = resolve)))
      .mockResolvedValueOnce(ok({ q: 'heat', items: [{ id: 41, name: 'Heat', rated: null }] }));
    const slow = findTitles('he');
    await findTitles('heat');
    answerSlow(ok({ q: 'he', items: [{ id: 99, name: 'Hell', rated: null }] }));
    await slow;

    expect(finder.items.map((h) => h.id)).toEqual([41]);
    expect(finder.searched).toBe('heat');
    expect(fetchMock.mock.calls[1][0]).toContain('/api/rate/search?q=heat');
  });

  it('never pins a title the person already rated', async () => {
    expect(await rateTitle({ id: 42, name: 'The Heat', rated: 'fine' })).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(pendingHead()).toEqual([]);
  });

  it('pins a pick, and says so when the table could not take it', async () => {
    fetchMock.mockResolvedValueOnce(ok(envelope()));
    expect(await rateTitle({ id: 1, name: 'Heat', rated: null })).toBe(true);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/rate?head=1');
    expect(rate.notice).toBe('');
    expect(pendingHead()).toEqual([]);

    fetchMock.mockResolvedValueOnce(ok(envelope()));
    expect(await rateTitle({ id: 5, name: 'Drive', rated: null })).toBe(false);
    expect(rate.notice).toBe("Drive can't be rated right now.");
    expect(pendingHead(), 'a pick that did not land is not left pinned').toEqual([]);
  });
});
