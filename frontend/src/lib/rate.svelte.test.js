import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ECHO_MS,
  continueRating,
  hueOf,
  load,
  metaLine,
  notSeen,
  pendingHead,
  place,
  preloadArt,
  rate,
  reset,
  runtimeLabel,
  sentenceCase,
  setHead,
  setKind,
  undo
} from './rate.svelte.js';

const WORDS = [
  'Hated it', "Didn't like it", 'Not really for me', 'It was fine', 'Liked it', 'Loved it', 'All-time favourite'
];
const film = (id, name) => ({ id, name, original_name: name, original_language: 'en', poster_path: null });
const shelves = () =>
  [6, 5, 4, 3, 2, 1, 0].map((tier) => ({
    tier,
    word: WORDS[tier],
    count: tier === 4 ? 2 : 0,
    films: tier === 4 ? [film(9, 'Zodiac')] : []
  }));
const card = (over = {}) => ({
  token: 't1',
  kind: 'movie',
  title: { ...film(1, 'Heat'), year: 1995, runtime_min: 170 },
  reason: "It's in your library.",
  shelves: shelves(),
  ...over
});

/** One `GET /api/rate` envelope, in the shape `api/rate.py` sends. */
const envelope = (over = {}) => ({
  setup: { done: true, earlier_ratings: 0, rated_before: 0 },
  session: { kinds: ['movie'], kind: 'movie', block: { slot: 1, size: 15, counter: '1 of 15' } },
  card: card(),
  preload: [],
  echo: null,
  done: null,
  drained: null,
  undo: { available: false, kind: null, name: null },
  ...over
});
const CLOSED = {
  setup: { done: false, earlier_ratings: 104, rated_before: 0 },
  session: null,
  card: null,
  preload: [],
  echo: null,
  done: null,
  drained: null,
  undo: { available: false, kind: null, name: null }
};

function ok(body) {
  return { ok: true, status: 200, statusText: 'OK', headers: new Headers(), text: async () => JSON.stringify(body) };
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
  it('writes a label the way a member reads it', () => {
    expect(sentenceCase('loved it')).toBe('Loved it');
    expect(sentenceCase(null)).toBe('');
  });

  it('formats runtime per kind and joins the meta line without stray spacing', () => {
    expect(runtimeLabel({ kind: 'movie', runtime_min: 170 })).toBe('2h 50m');
    expect(runtimeLabel({ kind: 'movie', runtime_min: 45 })).toBe('45m');
    expect(runtimeLabel({ kind: 'movie', runtime_min: 60 })).toBe('1h');
    expect(runtimeLabel({ kind: 'series', runtime_min: 170 })).toBe('170m/ep');
    expect(runtimeLabel(null)).toBe(null);
    expect(metaLine({ kind: 'movie', year: 1995, runtime_min: 170 })).toBe('1995 · 2h 50m');
    expect(metaLine({ kind: 'movie', runtime_min: 170 })).toBe('— · 2h 50m');
  });

  it('gives the same title the same hue every render', () => {
    expect(hueOf('Heat')).toBe(179);
    expect(hueOf('Prisoners')).toBe(54);
  });

  it('keeps only numeric head ids, so the banner CTA cannot send nonsense', () => {
    expect(setHead(['41', 'x', null, 0, 7])).toEqual([41, 7]);
    setHead([]);
  });

  it("preloads the card's film, every shelf's posters, and the server's list for the next card", () => {
    const made = [];
    vi.stubGlobal(
      'Image',
      class {
        constructor() {
          made.push(this);
        }
      }
    );
    preloadArt({ card: card(), preload: ['/api/art/2/poster'] });
    preloadArt(null);
    expect(made.map((image) => image.src)).toEqual(['/api/art/2/poster', '/api/art/1/poster', '/api/art/9/poster']);
    vi.unstubAllGlobals();
  });
});

describe('the envelope', () => {
  /** @type {any} */
  let fetchMock;
  const sent = (i = 0) => [fetchMock.mock.calls[i][0], JSON.parse(fetchMock.mock.calls[i][1]?.body ?? 'null')];

  beforeEach(async () => {
    vi.useFakeTimers();
    reset();
    setHead([]);
    fetchMock = vi.fn().mockResolvedValue(ok(envelope()));
    globalThis.fetch = fetchMock;
    await load();
    fetchMock.mockClear();
  });

  afterEach(() => {
    reset();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    delete globalThis.fetch;
  });

  it('opens on the served card with its counter, set-up and undo state', () => {
    expect(rate.card.token).toBe('t1');
    expect(rate.card.shelves).toHaveLength(7);
    expect(rate.session.block.counter).toBe('1 of 15');
    expect(rate.setup.done).toBe(true);
    expect(rate.undo).toEqual({ available: false, kind: null, name: null });
    expect(rate.echo).toBe(null);
  });

  it('reads the closed state before the set-up: no session and no card', async () => {
    fetchMock.mockResolvedValue(ok(CLOSED));
    await load({ quiet: true });
    expect(rate.setup).toEqual({ done: false, earlier_ratings: 104, rated_before: 0 });
    expect(rate.session).toBe(null);
    expect(rate.card).toBe(null);
  });

  it('places by tier, puts the next card up with the reply, and echoes the film for 1.6 s', async () => {
    fetchMock.mockResolvedValue(
      ok(
        envelope({
          session: { kinds: ['movie'], kind: 'movie', block: { slot: 2, size: 15, counter: '2 of 15' } },
          card: card({ token: 't2', title: { ...card().title, id: 2, name: 'Drive' } }),
          echo: { title_id: 1, name: 'Heat', tier: 5, word: 'Loved it' },
          undo: { available: true, kind: 'placement', name: 'Heat' }
        })
      )
    );
    await place(5);

    const [url, body] = sent();
    expect(url).toBe('/api/rate/place');
    expect(body).toMatchObject({ card_token: 't1', tier: 5 });
    expect(typeof body.latency_ms).toBe('number');
    expect(rate.card.token).toBe('t2');
    expect(rate.session.block.counter).toBe('2 of 15');
    expect(rate.echo).toEqual({ title_id: 1, name: 'Heat', tier: 5, word: 'Loved it' });
    expect(rate.undo).toEqual({ available: true, kind: 'placement', name: 'Heat' });

    vi.advanceTimersByTime(ECHO_MS - 1);
    expect(rate.echo).not.toBe(null);
    vi.advanceTimersByTime(1);
    expect(rate.echo).toBe(null);
    expect(rate.card.token).toBe('t2');
  });

  it('says which shelf is in flight until the server has taken it', async () => {
    /** @type {(response: any) => void} */
    let answer = () => {};
    fetchMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const tapped = place(4);
    expect(rate.busy).toBe(true);
    expect(rate.pending).toBe('place-4');
    await place(3);
    answer(ok(envelope({ card: card({ token: 't2' }) })));
    await tapped;
    expect(rate.pending).toBe(null);
    expect(fetchMock, 'a second tap while one is in flight sends nothing').toHaveBeenCalledTimes(1);
  });

  it('keeps the lit shelf up for its commit before the next film deals in', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    fetchMock.mockResolvedValue(ok(envelope({ card: card({ token: 't2' }) })));
    const tapped = place(6);
    await vi.advanceTimersByTimeAsync(200);
    expect(rate.card.token, 'the card swapped before its shelf had lit').toBe('t1');
    await vi.advanceTimersByTimeAsync(40);
    await tapped;
    expect(rate.card.token).toBe('t2');
  });

  it('says Not seen with the card token and echoes nothing', async () => {
    fetchMock.mockResolvedValue(
      ok(envelope({ card: card({ token: 't2' }), undo: { available: true, kind: 'not_seen', name: 'Heat' } }))
    );
    await notSeen();
    const [url, body] = sent();
    expect(url).toBe('/api/rate/not-seen');
    expect(body).toMatchObject({ card_token: 't1' });
    expect(rate.echo).toBe(null);
    expect(rate.undo.kind).toBe('not_seen');
  });

  it('takes the last answer back: the echo goes and the film comes back from the left', async () => {
    fetchMock.mockResolvedValue(
      ok(envelope({ card: card({ token: 't2' }), echo: { title_id: 1, name: 'Heat', tier: 4, word: 'Liked it' } }))
    );
    await place(4);
    expect(rate.echo.word).toBe('Liked it');

    fetchMock.mockResolvedValue(ok(envelope({ card: card({ token: 't3' }) })));
    await undo();
    expect(sent(1)[0]).toBe('/api/rate/undo');
    expect(rate.echo).toBe(null);
    expect(rate.back).toBe(true);
    expect(rate.card.token).toBe('t3');
  });

  it('reports a stale card and re-reads the table instead of guessing', async () => {
    fetchMock
      .mockResolvedValueOnce(conflict({ reason: 'stale_card', message: 'that card has already been answered' }))
      .mockResolvedValueOnce(ok(envelope({ card: card({ token: 't9' }) })));
    await place(4);
    expect(rate.notice).toBe('that card has already been answered');
    expect(rate.card.token).toBe('t9');
    expect(rate.error).toBe('');
  });

  it('says plainly when there is nothing to undo, and re-reads the table', async () => {
    fetchMock
      .mockResolvedValueOnce(conflict({ reason: 'nothing_to_undo', message: 'the journal is empty' }))
      .mockResolvedValueOnce(ok(envelope()));
    await undo();
    expect(rate.notice).toBe('Nothing to undo yet');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('falls back to the closed card when the set-up is owed', async () => {
    fetchMock
      .mockResolvedValueOnce(conflict({ reason: 'not_set_up', message: 'set up your ladder first' }))
      .mockResolvedValueOnce(ok(CLOSED));
    await place(4);
    expect(rate.notice).toBe('');
    expect(rate.setup.done).toBe(false);
    expect(rate.card).toBe(null);
  });

  it('ends a block on its own screen until Rate 15 more, and a re-read does not bring it back', async () => {
    const end = { rated_before: 3, noun: 'films' };
    fetchMock.mockResolvedValue(ok(envelope({ card: card({ token: 't2' }), done: end })));
    await place(4);
    expect(rate.done).toEqual(end);
    expect(rate.card.token, 'the next block is already on the table').toBe('t2');

    continueRating();
    expect(rate.done).toBe(null);
    await load({ quiet: true });
    expect(rate.done).toBe(null);

    // An Undo is a write: what it answers stands.
    fetchMock.mockResolvedValue(ok(envelope({ done: end })));
    await undo();
    expect(rate.done).toEqual(end);
    fetchMock.mockResolvedValue(ok(envelope({ done: null })));
    await undo();
    expect(rate.done).toBe(null);
  });

  it('drops a pin once its card is on the table, and keeps sending the rest', async () => {
    setHead([1, 9]);
    fetchMock.mockResolvedValue(ok(envelope()));
    await load({ quiet: true });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/rate?head=1&head=9');
    expect(pendingHead()).toEqual([9]);

    await place(4);
    expect(sent(1)[1].head).toEqual([9]);
    setHead([]);
  });

  it('switches the kind on the table, and asks nothing for the kind already on it', async () => {
    await setKind('movie');
    expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValue(
      ok(envelope({ session: { kinds: ['series'], kind: 'series', block: { slot: 1, size: 15, counter: '1 of 15' } } }))
    );
    await setKind('series');
    const [url, body] = sent();
    expect(url).toBe('/api/rate/session');
    expect(body).toEqual({ kinds: ['series'], head: [] });
    expect(rate.session.kind).toBe('series');
  });
});
