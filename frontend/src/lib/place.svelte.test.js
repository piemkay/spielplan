import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { answer, narrowing, notSeen, place, reset, resultLine, start } from './place.svelte.js';

const heat = { id: 1, name: 'Heat', year: 1995 };
const zodiac = { id: 2, name: 'Zodiac', year: 2007 };
const sicario = { id: 3, name: 'Sicario', year: 2015 };

const pair = (over = {}) => ({
  kind: 'movie',
  token: 't1',
  tier: 'A',
  left: { ...sicario, outcome: 'A' },
  right: { ...zodiac, outcome: 'B' },
  progress: { low: 1, high: 150, size: 150, asked: 0, estimate: 8 },
  ...over
});

let fetchMock;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  reset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function respond(payload, status = 200) {
  fetchMock.mockResolvedValueOnce({
    ok: status < 400,
    status,
    headers: { get: () => null },
    text: async () => JSON.stringify(payload)
  });
}

const sent = (i) => ({ url: fetchMock.mock.calls[i][0], body: JSON.parse(fetchMock.mock.calls[i][1].body) });

describe('the flow follows the server', () => {
  it('opens on the title and its kind, and holds the pair it is given', async () => {
    respond(pair());
    await start(3, 'series');
    expect(sent(0)).toEqual({ url: '/api/rank/place', body: { title_id: 3, kind: 'series' } });
    expect(place.pair.right.name).toBe('Zodiac');
    expect(place.done).toBeNull();
  });

  it('answers under the sealed token, "Much more" as decisive, and ends on where it sits', async () => {
    respond(pair());
    await start(3, 'movie');
    respond(pair({ token: 't2', progress: { low: 1, high: 75, size: 150, asked: 1, estimate: 8 } }));
    const first = answer('A', true);
    expect(place.pending).toBe('duel-A-much');
    await first;
    expect(sent(1)).toEqual({
      url: '/api/rank/place/answer',
      body: { token: 't1', outcome: 'A', decisive: true }
    });
    respond({ done: true, tier: 'A', title_id: 3, above: heat, below: zodiac, asked: 2, around: [heat, sicario, zodiac] });
    await answer('TIE', false);
    expect(sent(2).body).toEqual({ token: 't2', outcome: 'TIE', decisive: false });
    expect(place.pair).toBeNull();
    expect(place.done.above.name).toBe('Heat');
    expect(place.pending).toBeNull();
  });

  it('sends Not seen for the neighbour as a skip, marked on its side', async () => {
    respond(pair());
    await start(3, 'movie');
    respond(pair({ token: 't2', right: { ...heat, outcome: 'B' } }));
    const skipping = notSeen();
    expect(place.pending).toBe('correction-right');
    await skipping;
    expect(sent(1)).toEqual({ url: '/api/rank/place/skip', body: { token: 't1' } });
    expect(place.pair.right.name).toBe('Heat');
  });

  it('sends one answer at a time', async () => {
    respond(pair());
    await start(3, 'movie');
    respond(pair({ token: 't2' }));
    const first = answer('B', false);
    await answer('A', false);
    await first;
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps the pair and says why when an answer is refused', async () => {
    respond(pair());
    await start(3, 'movie');
    respond({ detail: { reason: 'stale_pair', message: 'that pair has already been answered' } }, 409);
    await answer('A', false);
    expect(place.error).toBe('that pair has already been answered');
    expect(place.pair.token).toBe('t1');
    expect(place.busy).toBe(false);
  });
});

describe('the words', () => {
  it('narrows to a window of the tier and counts the questions', () => {
    const bar = narrowing(pair({ progress: { low: 58, high: 88, size: 150, asked: 3, estimate: 8 } }));
    expect(bar.where).toBe('Somewhere between #58 and #88 of 150 in A');
    expect(bar.count).toBe('Question 4 of about 8');
    expect(bar.from).toBeCloseTo(57 / 150);
    expect(bar.span).toBeCloseTo(31 / 150);
    expect(narrowing(pair())).toMatchObject({ from: 0, span: 1 });
  });

  it('names the neighbours, or the edge of the tier', () => {
    expect(resultLine({ tier: 'A', above: heat, below: zodiac, asked: 8 })).toBe(
      'Between Heat and Zodiac — 8 questions'
    );
    expect(resultLine({ tier: 'A', above: null, below: heat, asked: 1 })).toBe(
      'At the top of A, above Heat — 1 question'
    );
    expect(resultLine({ tier: 'A', above: zodiac, below: null, asked: 3 })).toBe(
      'At the bottom of A, below Zodiac — 3 questions'
    );
    expect(resultLine({ tier: 'A', above: null, below: null, asked: 0 })).toBe('The only one in A');
  });
});
