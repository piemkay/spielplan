import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ECHO_MS,
  LEARNING_CURVE_COPY,
  MODES,
  PAIR_QUESTION,
  PAIR_SELECTION_COPY,
  UNDO_KIND_LABELS,
  armingLine,
  heavyClass,
  modeName,
  continueRating,
  findTitles,
  finder,
  hueOf,
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
  reset,
  sentenceCase
} from './rate.svelte.js';

const envelope = (over = {}) => ({
  session: {
    id: 1,
    mode: 'mix',
    kinds: ['movie', 'series'],
    block: { index: 0, slot: 1, size: 15, counter: '1 of 15', serving: 'sweep' }
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
  it('writes a class label the way a member reads it', () => {
    expect(sentenceCase('disliked')).toBe('Disliked');
    expect(sentenceCase('')).toBe('');
    expect(sentenceCase(null)).toBe('');
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
    // Whole hours drop the zero minutes, as the boards and Tonight's budget write them.
    expect(runtimeLabel({ kind: 'movie', runtime_min: 60 })).toBe('1h');
    expect(metaLine({ kind: 'movie', year: 2004, runtime_min: 120 })).toBe('2004 · 2h');
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
    expect(undoMessage({ available: false, reason: 'empty' })).toBe('Nothing to undo yet');
    expect(undoMessage({ available: false, reason: 'block_boundary' })).toBe(
      'Undo only goes back to the start of these 15'
    );
  });

  it('says when the balance check starts, and only until it does', () => {
    const balance = { arms_at: 15, total: 3, warn: false };
    expect(armingLine(balance)).toBe('A balance check starts at 15 ratings.');
    expect(armingLine({ ...balance, total: 15 })).toBe('');
    expect(armingLine({ ...balance, warn: true })).toBe('');
    expect(armingLine(null)).toBe('');
  });

  it('names the class the balance warning is about, and none while it is quiet', () => {
    const labels = ['disliked', 'fine', 'liked'];
    expect(heavyClass({ labels, counts: [2, 3, 12], warn: true })).toBe('liked');
    expect(heavyClass({ labels, counts: [13, 3, 2], warn: true })).toBe('disliked');
    expect(heavyClass({ labels, counts: [2, 3, 12], warn: false })).toBe('');
    expect(heavyClass(null)).toBe('');
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
    expect(rate.session.block.counter).toBe('1 of 15');
    expect(rate.balance.total).toBe(6);
    expect(rate.undo).toEqual({ available: false, kind: null, reason: 'empty' });
    expect(rate.echo).toBe(null);
  });

  it('puts the next card up with the reply and echoes the guess for the one just rated', async () => {
    // The next card is already in this response, so the swap costs no request (decision 530).
    fetchMock.mockResolvedValue(
      ok(
        envelope({
          session: {
            ...envelope().session,
            block: { index: 0, slot: 2, size: 15, counter: '2 of 15', serving: 'battle' }
          },
          card: { ...envelope().card, token: 't2', title: { ...envelope().card.title, id: 2 } },
          reveal: { available: true, agreed: true, text: "we'd have guessed the same · cdf 0.71" },
          undo: { available: true, kind: 'verdict', reason: null }
        })
      )
    );
    await verdict(2);

    expect(rate.card.token).toBe('t2');
    expect(rate.session.block.counter).toBe('2 of 15');
    expect(rate.echo).toEqual({
      name: 'Heat',
      said: 'liked',
      available: true,
      agreed: true,
      text: "we'd have guessed the same · cdf 0.71"
    });
    expect(rate.undo.available).toBe(true);

    vi.advanceTimersByTime(ECHO_MS - 1);
    expect(rate.echo).not.toBe(null);
    vi.advanceTimersByTime(1);
    expect(rate.echo).toBe(null);
    expect(rate.card.token).toBe('t2');
  });

  it('holds nothing: the next card answers at once, and that tap clears the echo', async () => {
    fetchMock.mockResolvedValue(
      ok(envelope({ reveal: { available: false, reason: 'no guess yet - rate a few more first' } }))
    );
    await verdict(2);
    expect(rate.echo.text).toBe('no guess yet - rate a few more first');
    fetchMock.mockClear();

    /** @type {(response: any) => void} */
    let answer = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const tapped = skip();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(rate.echo).toBe(null);
    answer(ok(envelope()));
    await tapped;
    expect(rate.echo).toBe(null);
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

    fetchMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const picked = duel('TIE');
    expect(rate.pending).toBe('duel-TIE');
    answer(conflict({ reason: 'stale_card', message: 'that card has already been answered' }));
    fetchMock.mockResolvedValue(ok(envelope()));
    await picked;
    expect(rate.pending).toBe(null);
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
    // The server's wording names its mechanism; the member reads the same line as the chip's.
    expect(rate.notice).toBe('Undo only goes back to the start of these 15');
  });

  it('sends "Much more" as decisive and names that step while it is in flight', async () => {
    /** @type {(response: any) => void} */
    let answer = () => {};
    fetchMock.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    const picked = duel('B', true);
    expect(rate.pending).toBe('duel-B-much');
    const sent = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(sent).toMatchObject({ card_token: 't1', outcome: 'B', decisive: true });
    answer(ok(envelope()));
    await picked;
    expect(rate.pending).toBe(null);
  });

  it('sends More and Same as not decisive', async () => {
    fetchMock.mockResolvedValue(ok(envelope()));
    await duel('A');
    await duel('TIE');
    const sent = fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body));
    expect(sent.map((b) => [b.outcome, b.decisive])).toEqual([
      ['A', false],
      ['TIE', false]
    ]);
  });

  it('drops the echo when Undo takes the observation back, and the card comes back', async () => {
    fetchMock.mockResolvedValue(
      ok(
        envelope({
          card: { ...envelope().card, token: 't2' },
          reveal: { available: true, agreed: false, text: "we'd have guessed fine" }
        })
      )
    );
    await verdict(2);
    expect(rate.echo.text).toBe("we'd have guessed fine");

    fetchMock.mockResolvedValue(ok(envelope({ card: { ...envelope().card, token: 't1' } })));
    await undo();
    expect(rate.echo).toBe(null);
    expect(rate.back).toBe(true);
    expect(rate.card.token).toBe('t1');
  });

  it("ends a block on a screen of its own at the reply, until Undo takes it back", async () => {
    const at = (index, slot) => ({
      ...envelope().session,
      block: { index, slot, size: 15, counter: `${slot} of 15`, serving: 'sweep' }
    });
    fetchMock.mockResolvedValue(ok(envelope({ session: at(0, 15) })));
    await load({ quiet: true });

    fetchMock.mockResolvedValue(
      ok(
        envelope({
          session: at(1, 1),
          card: { ...envelope().card, token: 't2' },
          reveal: { available: true, agreed: true, text: "we'd have guessed the same" },
          undo: { available: true, kind: 'verdict', reason: null }
        })
      )
    );
    await verdict(2);
    // The end screen names the block it finished, and echoes the fifteenth card's guess.
    expect(rate.done.counter).toBe('15 of 15');
    expect(rate.echo.text).toBe("we'd have guessed the same");
    expect(rate.card.token).toBe('t2');

    // A quiet re-read of the new block does not dismiss it; an Undo into the old block does.
    fetchMock.mockResolvedValue(
      ok(envelope({ session: at(1, 1), card: { ...envelope().card, token: 't2' } }))
    );
    await load({ quiet: true });
    expect(rate.done).not.toBe(null);
    fetchMock.mockResolvedValue(ok(envelope({ session: at(0, 15) })));
    await undo();
    expect(rate.done).toBe(null);

    // An answer inside a block is no end, and moving on leaves the card the last answer brought.
    fetchMock.mockResolvedValue(
      ok(envelope({ session: at(1, 1), card: { ...envelope().card, token: 't3' } }))
    );
    await skip();
    expect(rate.done.counter).toBe('15 of 15');
    continueRating();
    expect(rate.done).toBe(null);
    expect(rate.card.token).toBe('t3');
    fetchMock.mockResolvedValue(ok(envelope({ session: at(1, 2) })));
    await skip();
    expect(rate.done).toBe(null);
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
