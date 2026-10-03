import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ROUND_END_TITLE,
  ROUND_SIZE,
  TYPING_PAUSE_MS,
  answer,
  apply,
  cardMove,
  chooseKind,
  clearFilter,
  clearFilters,
  closeQueue,
  closeTitle,
  dnaTierText,
  draft,
  drop,
  emptyState,
  facets,
  filterChips,
  finish,
  flipTerm,
  keepGoing,
  load,
  loadFacets,
  move,
  neighboursAt,
  openQueue,
  openTitle,
  rank,
  reset,
  roundLine,
  setTerm,
  tierLegend,
  searchHint,
  showAll,
  showLess,
  spot,
  typed,
  undoMove
} from './rank.svelte.js';
import { hideToast, toast } from './toast.svelte.js';

/** @typedef {{id: string, label: string, facet: string, mode: 'in' | 'out'}} Term */
/** @type {Term} */
const COSY = { id: 'mood.cosy', label: 'cozy & mellow', facet: 'mood', mode: 'in' };
/** @type {Term} */
const HEIST = { id: 'themes.heist', label: 'heist', facet: 'themes', mode: 'out' };

const board = (over = {}) => ({
  kind: 'movie',
  tier_set: ['E', 'D', 'C', 'B', 'A', 'S'],
  tiers: [
    {
      index: 5,
      label: 'S',
      word: 'All-time favourite',
      entries: [
        {
          title_id: 1,
          name: 'Heat',
          year: 1995,
          tier: 5,
          assigned_tier: null,
          straddle: 4,
          straddle_badge: 'S or A?',
          badge: 'S — the only one',
          tension: null
        }
      ]
    },
    {
      index: 4,
      label: 'A',
      word: 'Excellent',
      entries: [
        {
          title_id: 2,
          name: 'Drive',
          year: 2011,
          tier: 4,
          assigned_tier: 4,
          straddle: null,
          straddle_badge: null,
          badge: 'A — just above Prisoners',
          tension: 'You put it in A — your other answers still point to C'
        },
        {
          title_id: 3,
          name: 'Prisoners',
          year: 2013,
          tier: 4,
          assigned_tier: null,
          straddle: null,
          straddle_badge: null,
          badge: 'A — just below Drive',
          tension: null
        }
      ]
    },
    { index: 3, label: 'B', word: 'Very good', entries: [] }
  ],
  rated: 3,
  rated_total: 3,
  set_up: true,
  guessing: true,
  filters: {},
  dna_tiers: null,
  ...over
});

/** A served queue pair, numbered so a test can tell which one is on the table. */
function pairN(i) {
  return { title_a: 1, title_b: 2, token: `t${i}`, reason: 'x', name_a: 'Heat', name_b: 'Drive' };
}

let fetchMock;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  hideToast();
  rank.kind = 'movie';
  rank.opened = null;
  rank.pair = null;
  rank.error = '';
  rank.notice = '';
  rank.log = [];
  rank.busy = false;
  rank.queueOpen = false;
  rank.roundAnswered = 0;
  rank.roundDone = false;
  rank.placed = [];
  rank.moves = [];
  rank.expanded = [];
  rank.perTier = null;
  // `draft` is module state, so reset it or a test depends on the last one's filters.
  draft.q = '';
  draft.genre = '';
  draft.decade = '';
  draft.runtime_max = '';
  draft.seen = 'any';
  draft.terms = [];
  apply(board());
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

function held(payload) {
  let release = () => {};
  fetchMock.mockReturnValueOnce(
    new Promise((r) => {
      release = () =>
        r({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(payload) });
    })
  );
  return () => release();
}

describe('the board comes from the server', () => {
  it('replaces the tiers wholesale rather than merging them', async () => {
    respond(board({ tiers: [{ index: 0, label: 'E', entries: [] }], rated: 0, rated_total: 0 }));
    respond(board());
    await load('movie');
    expect(rank.tiers).toHaveLength(1);
    expect(rank.tiers[0].label).toBe('E');
  });

  it('reads decision 117 as an absence, not an empty object', async () => {
    // The server deletes the gated keys; defaulting `model` to {} would claim the toggle was on.
    respond(board());
    await load('movie');
    expect(rank.model).toBeNull();

    respond(board({ model: { cutpoints: [0.1], straddle_z: 1, tension_credible_mass: 0.8 } }));
    await load('movie');
    expect(rank.model.cutpoints).toEqual([0.1]);
  });

  it('carries whether the ladder is set up and whether the order is still a guess (decision 550)', () => {
    expect([rank.setUp, rank.guessing]).toEqual([true, true]);
    apply(board({ set_up: false, guessing: false }));
    expect([rank.setUp, rank.guessing]).toEqual([false, false]);
    reset();
    expect([rank.setUp, rank.guessing]).toEqual([true, false]);
  });
});

describe('a move (decision 528)', () => {
  it('drops the title into the chosen tier, naming no neighbour, and says so', async () => {
    respond(board());
    await move(rank.tiers[0].entries[0], 4);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/rank/drop');
    expect(url).toContain('kind=movie');
    // A drop into a tier is a bare `tier_edit`; the two duels belong to a drop between two titles.
    expect(JSON.parse(init.body)).toEqual({ title_id: 1, tier: 4, above: null, below: null });
    expect(toast.message).toBe('Heat moved to A');
    expect(toast.actionLabel).toBe('Undo');
  });

  it('undoes by taking the tier back, naming no neighbour and the edit it undoes', async () => {
    respond({ ...board(), tier_edit_id: 41 });
    await move(rank.tiers[1].entries[1], 5, 1, null);
    respond(board());
    await toast.action();
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      title_id: 3,
      tier: 4,
      above: null,
      below: null,
      undoes: 41
    });
  });

  it("records the tier sheet's pick as explicit (decision 534)", async () => {
    respond(board());
    await move(rank.tiers[0].entries[0], 4, null, null, 'explicit');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      title_id: 1,
      tier: 4,
      above: null,
      below: null,
      via: 'explicit'
    });
  });

  it('places a title that had no tier, offering no undo (decision 531)', async () => {
    respond(board());
    await move({ title_id: 9, name: 'Collateral', tier: null }, 4, null, null, 'explicit');
    expect(toast.message).toBe('Collateral placed in A');
    expect(toast.actionLabel).toBe('');
  });

  it('offers no undo for a new spot in the same tier', async () => {
    respond(board());
    await move(rank.tiers[1].entries[0], 4, 3, null);
    expect(toast.message).toBe('Drive moved to A');
    expect(toast.actionLabel).toBe('');
  });

  it('writes nothing when the title stays where it is', async () => {
    await move(rank.tiers[0].entries[0], 5);
    await move(rank.tiers[1].entries[0], 4, null, 3);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(toast.message).toBe('');
  });

  it('claims no move the server refused', async () => {
    respond({ detail: 'database error' }, 500);
    await move(rank.tiers[0].entries[0], 4);
    expect(rank.error).not.toBe('');
    expect(toast.message).toBe('');
  });
});

describe('a move from a title card off Rank (decision 531)', () => {
  const heat = { title_id: 1, name: 'Heat', kind: 'series', tier: 5 };
  const tierC = { index: 2, label: 'C', word: 'Good' };

  it('drops the title for its own kind and leaves the board on Rank alone', async () => {
    const before = JSON.stringify(rank.tiers);
    respond({ ...board({ kind: 'series', tiers: [] }), tier_edit_id: 52 });
    expect(await cardMove(heat, tierC)).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/rank/drop?kind=series&per_tier=1');
    expect(JSON.parse(init.body)).toEqual({ title_id: 1, tier: 2, via: 'explicit' });
    expect(JSON.stringify(rank.tiers)).toBe(before);
    expect(toast.message).toBe('Heat moved to C');

    respond(board());
    await toast.action();
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ title_id: 1, tier: 5, undoes: 52 });
  });

  it('offers no Undo for a first placement, and writes nothing for the tier it holds', async () => {
    expect(await cardMove(heat, { index: 5, label: 'S' })).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    respond(board());
    await cardMove({ ...heat, tier: null }, tierC);
    expect(toast.message).toBe('Heat placed in C');
    expect(toast.actionLabel).toBe('');
  });

  it('says so when the server refuses, and claims no move', async () => {
    respond({ detail: 'database error' }, 500);
    expect(await cardMove(heat, tierC)).toBe(false);
    expect(toast.message).toMatch(/^Could not move Heat — /);
    expect(toast.actionLabel).toBe('');
  });
});

describe('the neighbours a drop lands between', () => {
  it('names nobody for a drop into a tier, however full that tier is', () => {
    // Naming the tier's last entry would write a duel the person never made, on every move.
    expect(neighboursAt(4, { title_id: 1 })).toEqual({ above: null, below: null });
  });

  it('names none when the tier is empty', () => {
    expect(neighboursAt(3, { title_id: 1 }, 0)).toEqual({ above: null, below: null });
  });

  it('counts the spots without the title being dropped', () => {
    expect(neighboursAt(4, { title_id: 2 }, 0).above).toBeNull();
    expect(neighboursAt(4, { title_id: 2 }, 0).below.title_id).toBe(3);
    expect(neighboursAt(4, { title_id: 3 }, 1).above.title_id).toBe(2);
  });
});

describe('the comparison queue', () => {
  it('sends the sealed token back and never an arm', async () => {
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'sealed', reason: 'x' };
    respond({ kind: 'movie', pair: null, reason: 'done', log: ['line'] });
    respond(board());
    await answer('A');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/rank/queue/answer');
    const body = JSON.parse(init.body);
    expect(body).toEqual({ pair: 'sealed', outcome: 'A', decisive: false });
    expect(Object.keys(body)).not.toContain('arm');
    expect(Object.keys(body)).not.toContain('title_a');
  });

  it('re-reads the board after an answer, because the model refits immediately', async () => {
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'sealed', reason: 'x' };
    respond({ kind: 'movie', pair: null, reason: 'done' });
    respond(board({ rated: 9 }));
    await answer('TIE');
    expect(fetchMock.mock.calls[1][0]).toContain('/api/rank?');
    expect(rank.rated).toBe(9);
  });

  it('does nothing with no pair on the table', async () => {
    rank.pair = null;
    await answer('A');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('surfaces a stale pair as a notice rather than an error', async () => {
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'old', reason: 'x' };
    respond({ detail: { reason: 'stale_pair', message: 'that pair is no longer on the table' } }, 409);
    respond({ kind: 'movie', pair: null, reason: 'done' });
    await answer('A');
    expect(rank.notice).toBe('that pair is no longer on the table');
    expect(rank.error).toBe('');
  });

  it('takes the refused pair off the table and re-reads, so the loser is not stuck', async () => {
    // A refused seal is permanent for this token, so re-read rather than leave the pair up.
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'old', reason: 'x' };
    respond({ detail: { reason: 'stale_pair', message: 'that pair is no longer on the table' } }, 409);
    respond({ kind: 'movie', pair: { title_a: 3, title_b: 4, token: 'fresh', reason: 'y' } });
    await answer('A');

    expect(rank.pair?.token).toBe('fresh');
    expect(fetchMock.mock.calls[1][0]).toContain('/api/rank/queue?');
    // The notice stays: a pair that silently changed would say nothing about the refusal.
    expect(rank.notice).toBe('that pair is no longer on the table');

    // And the next tap is answerable, with the fresh token.
    respond({ kind: 'movie', pair: null, reason: 'done' });
    respond(board());
    await answer('B');
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).pair).toBe('fresh');
  });
});

describe('Needs a look (§6.3)', () => {
  it('counts what the server says Sharpen would ask about, and nothing when it says nothing', () => {
    apply(board({ straddling: 12 }));
    expect(rank.straddling).toBe(12);
    apply(board());
    expect(rank.straddling).toBe(0);
  });
});

describe("proposal 80's states", () => {
  it('claims nothing on a young board, whose guess line is in Needs a look (decision 550)', () => {
    apply(board({ rated: 3, rated_total: 3 }));
    expect(emptyState()).toBeNull();
  });

  it('distinguishes "no match" from "not enough yet"', () => {
    apply(board({ rated: 0, rated_total: 40, filters: { terms: ['mood.cosy'] } }));
    const state = emptyState();
    expect(state.kind).toBe('no-match');
    // The chips above the board name each filter; the sentence points at them.
    expect(state.text).toBe('Nothing on your list matches these filters.');
    expect(state.cta).toBe('Clear filters');
  });

  it('names the search when the search alone found nothing', () => {
    apply(board({ rated: 0, rated_total: 40, filters: { q: 'Taxi' } }));
    expect(emptyState().text).toBe('Nothing on your list matches “Taxi”.');
  });

  it('is absent on a board with titles on it', () => {
    apply(board({ rated: 3, rated_total: 40 }));
    expect(emptyState()).toBeNull();
  });

  // Before the first fit the board also says `rated_total: 0`; `fitting` tells the two apart.
  it('says the fit is owed rather than that nobody has started', () => {
    apply(board({ rated: 0, rated_total: 0, tiers: [], fitting: true }));
    const state = emptyState();
    expect(state.kind).toBe('fitting');
    expect(state.text).toContain('still being worked out');
    // "shortly", no duration: the sweep runs every 60s, but a queue may be ahead.
    expect(state.text).toContain('shortly');
    expect(state.text).not.toContain("you're at 0");
  });

  it('claims nothing before the set-up, whose card is the way on, but a filter can still miss', () => {
    for (const over of [
      { rated: 0, rated_total: 0 },
      { rated: 5, rated_total: 5 },
      { rated: 0, rated_total: 0, fitting: true }
    ]) {
      apply(board({ set_up: false, tiers: [], ...over }));
      expect(emptyState(), JSON.stringify(over)).toBeNull();
    }
    apply(board({ set_up: false, rated: 0, rated_total: 5, filters: { q: 'Taxi' } }));
    expect(emptyState().kind).toBe('no-match');
  });

  it('still tells a member who has rated nothing that they have rated nothing', () => {
    apply(board({ rated: 0, rated_total: 0, tiers: [], fitting: false }));
    const state = emptyState();
    expect(state.kind).toBe('unrated');
    expect(state.text).toBe('Your tiers fill in as you place films on Rate.');
    expect(state.cta).toBe('Rate some titles');
  });
});

describe('filters', () => {
  it('are sent as query parameters and dropped when empty', async () => {
    draft.terms = [COSY, HEIST];
    draft.runtime_max = '110';
    respond(board());
    await load('movie');
    const url = new URL(fetchMock.mock.calls[0][0], 'http://localhost');
    expect(url.searchParams.getAll('term')).toEqual(['mood.cosy']);
    expect(url.searchParams.getAll('not_term')).toEqual(['themes.heist']);
    expect(url.searchParams.get('runtime_max')).toBe('110');
    expect(url.searchParams.has('genre')).toBe(false);
    expect(url.searchParams.has('dna'), "the free-text taste tag is gone (decision 557)").toBe(false);
  });

  it('take a term from the picker, switch it between include and leave out, and drop it', async () => {
    respond(board());
    await setTerm({ term: 'mood.cosy', label: 'cozy & mellow', facet: 'mood' }, 'in');
    expect(draft.terms).toEqual([COSY]);
    respond(board());
    await setTerm({ term: 'mood.cosy', label: 'cozy & mellow', facet: 'mood' }, 'out');
    expect(draft.terms).toEqual([{ ...COSY, mode: 'out' }]);
    respond(board());
    await flipTerm('mood.cosy');
    expect(draft.terms[0].mode).toBe('in');
    respond(board());
    await clearFilter('term:mood.cosy');
    expect(draft.terms).toEqual([]);
    expect(fetchMock.mock.calls.at(-1)[0]).not.toContain('term=');
  });

  it('show what the Filters control holds as chips, and a chip clears only its own', async () => {
    draft.q = 'heat';
    draft.genre = 'Thriller';
    draft.decade = '1990';
    draft.runtime_max = '110';
    draft.seen = 'unseen';
    draft.terms = [HEIST, COSY];
    // The search keeps its own box, so it is no chip; includes lead, then leave-outs.
    expect(filterChips().map((c) => c.text)).toEqual([
      'cozy & mellow',
      'heist',
      'Thriller',
      '1990s',
      'Up to 1h 50m',
      'Not seen'
    ]);

    respond(board());
    await clearFilter('decade');
    expect(draft.decade).toBe('');
    const url = fetchMock.mock.calls[0][0];
    expect(url).toContain('genre=Thriller');
    expect(url).not.toContain('decade=');

    respond(board());
    await clearFilter('seen');
    expect(draft.seen).toBe('any');
    expect(filterChips().map((c) => c.key)).toEqual([
      'term:mood.cosy', 'term:themes.heist', 'genre', 'runtime_max'
    ]);
  });

  it('clear back to nothing', async () => {
    draft.terms = [COSY];
    respond(board());
    await clearFilters();
    expect(draft.terms).toEqual([]);
    expect(fetchMock.mock.calls[0][0]).not.toContain('term=');
  });

  it('read the typed title after one pause, once for a burst of keystrokes (round-2 R5)', async () => {
    vi.useFakeTimers();
    try {
      respond(board());
      for (const text of ['T', 'Ta', 'Tax', 'Taxi']) {
        draft.q = text;
        typed();
      }
      vi.advanceTimersByTime(TYPING_PAUSE_MS - 1);
      expect(fetchMock).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toContain('q=Taxi');
    } finally {
      vi.useRealTimers();
    }
  });

  it('forget a read the last keystroke scheduled when the surface resets', () => {
    vi.useFakeTimers();
    try {
      draft.q = 'Heat';
      typed();
      reset();
      vi.advanceTimersByTime(TYPING_PAUSE_MS * 2);
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the search field's placeholder carries the count (decision 528)", () => {
  it("counts the rated list in the kind's own word, filtered or not", () => {
    apply(board({ rated: 70, rated_total: 70 }));
    expect(searchHint()).toBe('Search 70 rated films');
    apply(board({ rated: 1, rated_total: 1 }));
    expect(searchHint()).toBe('Search 1 rated film');
    apply(board({ rated: 12, rated_total: 70, filters: { genre: 'Thriller' } }));
    expect(searchHint()).toBe('Search 70 rated films');
    rank.kind = 'series';
    apply(board({ rated: 12, rated_total: 12 }));
    expect(searchHint()).toBe('Search 12 rated series');
  });

  it('names no number before there is a list', () => {
    apply(board({ rated: 0, rated_total: 0, tiers: [] }));
    expect(searchHint()).toBe('Search your ranking');
  });
});

describe('a tier pages (decision 528)', () => {
  const paged = () =>
    board({
      tiers: [{ index: 5, label: 'S', word: 'All-time favourite', count: 3, entries: [board().tiers[0].entries[0]] }]
    });
  const rest = { index: 5, count: 3, offset: 1, entries: [{ title_id: 7 }, { title_id: 8 }] };

  it('asks for the first entries of each tier, and for the rest once one is opened', async () => {
    rank.perTier = 8;
    respond(paged());
    await load('movie');
    expect(fetchMock.mock.calls[0][0]).toContain('per_tier=8');
    expect(rank.tiers[0].entries).toHaveLength(1);

    respond(paged());
    respond(rest);
    await showAll(5);
    const tierRead = fetchMock.mock.calls[2][0];
    expect(tierRead).toContain('/api/rank/tier?');
    expect(tierRead).toContain('index=5');
    expect(tierRead).toContain('offset=1');
    expect(tierRead).not.toContain('per_tier');
    expect(rank.tiers[0].entries.map((e) => e.title_id)).toEqual([1, 7, 8]);
  });

  it('keeps an opened tier whole across a drop, and Show less closes it without a read', async () => {
    rank.expanded = [5];
    respond(paged());
    respond(rest);
    await drop({ title_id: 3, tier: 5 });
    expect(rank.tiers[0].entries).toHaveLength(3);

    showLess(5);
    expect(rank.expanded).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('a drop', () => {
  it('leaves the board untouched until the server answers', async () => {
    const before = rank.tiers;
    let resolve = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise((r) => {
        resolve = () =>
          r({
            ok: true,
            status: 200,
            headers: { get: () => null },
            text: async () => JSON.stringify(board({ rated: 99 }))
          });
      })
    );
    const pending = drop({ title_id: 1, tier: 0 });
    expect(rank.tiers).toBe(before);
    expect(rank.busy).toBe(true);
    resolve();
    await pending;
    expect(rank.rated).toBe(99);
    expect(rank.busy).toBe(false);
  });

  it('refuses to start a second one while the first is in flight', async () => {
    const release = held(board());
    const first = drop({ title_id: 1, tier: 0 });
    await drop({ title_id: 2, tier: 5 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release();
    await first;
  });

  it('is not painted over by a read that started before it', async () => {
    // A debounced search or a held drag's tier opener may still be reading the board from before.
    const release = held(board({ rated: 1 }));
    respond(board({ rated: 2 }));
    const read = load('movie');
    await drop({ title_id: 1, tier: 0 });
    release();
    await read;
    expect(rank.rated).toBe(2);
  });
});

describe('the spot a drop lands in (§6.3)', () => {
  it('names both neighbours between two posters, in the label and the live region', () => {
    // §6.3: a drop between two titles emits the edit plus two margin-less duels.
    const between = neighboursAt(4, { title_id: 1 }, 1);
    expect([between.above.title_id, between.below.title_id]).toEqual([2, 3]);
    expect(spot('A', between)).toEqual({
      chip: 'A · between Drive and Prisoners',
      said: 'In A, between Drive and Prisoners.'
    });
  });

  it('says the top and the bottom of a tier at its edges', () => {
    expect(spot('A', neighboursAt(4, { title_id: 1 }, 0)).chip).toBe('top of A');
    expect(spot('A', neighboursAt(4, { title_id: 1 }, 2)).chip).toBe('bottom of A');
    expect(spot('A', neighboursAt(4, { title_id: 1 }, 2)).said).toBe('At the bottom of A.');
  });

  it('names only the tier for a drop on its letter', () => {
    expect(spot('B', neighboursAt(3, { title_id: 1 }))).toEqual({ chip: 'B', said: 'In B.' });
  });
});

describe('the empty state waits for the board', () => {
  it('claims nothing while the first read is in flight', () => {
    reset();
    expect(rank.loading).toBe(true);
    expect(emptyState()).toBeNull();
  });

  it('claims nothing after a failed read', () => {
    apply(board({ rated: 0, rated_total: 0 }));
    rank.error = 'database error';
    expect(emptyState()).toBeNull();
  });
});

describe('overlapping requests', () => {
  it('drops a slow earlier response in favour of the newer one', async () => {
    // Tap Series then Films: the Series board must not land second under a Films tab.
    const releaseFirst = held(board({ kind: 'series', rated: 111 }));
    respond(board({ kind: 'movie', rated: 222 }));

    const slow = load('series');
    const fast = load('movie');
    await fast;
    releaseFirst();
    await slow;

    expect(rank.kind).toBe('movie');
    expect(rank.rated).toBe(222);
  });
});

describe('reset', () => {
  it("forgets one person's board, card, pair and round before the next person's", () => {
    openTitle(rank.tiers[1].entries[0]);
    rank.straddling = 4;
    rank.pair = pairN(1);
    rank.queueOpen = true;
    rank.roundAnswered = 3;
    reset();
    expect(rank.tiers).toEqual([]);
    expect(rank.ratedTotal).toBe(0);
    expect(rank.straddling).toBe(0);
    expect(rank.opened).toBeNull();
    expect(rank.pair).toBeNull();
    expect(rank.queueOpen).toBe(false);
    expect(rank.roundAnswered).toBe(0);
    expect(rank.booted).toBe(false);
  });

  it('keeps the board when only the tab is left', () => {
    const tiers = rank.tiers;
    reset({ board: false });
    expect(rank.tiers).toBe(tiers);
    expect(rank.booted).toBe(false);
  });

  it('makes an in-flight response land nowhere', async () => {
    const release = held(board({ rated: 999 }));
    const pending = load('movie');
    reset();
    release();
    await pending;
    expect(rank.rated).not.toBe(999);
  });
});

describe('the queue answer keeps its §6.7 line', () => {
  it('survives the board re-read that follows it', async () => {
    // `apply()` blanks `log`, and the answer's line is the only narration of this duel.
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'sealed', reason: 'x' };
    respond({ kind: 'movie', pair: null, reason: 'done', log: ['duel(Heat vs Drive) = A'] });
    respond(board());
    await answer('A');
    expect(rank.log).toEqual(['duel(Heat vs Drive) = A']);
  });
});

describe('the queue answer', () => {
  it('refuses to start a second one while the first is in flight', async () => {
    // A double tap on one sealed pair must not write two duels (§4.2 is append-only).
    rank.pair = { title_a: 1, title_b: 2, token: 'sealed', reason: 'x' };
    const release = held({ kind: 'movie', pair: null, reason: 'done' });
    respond(board());                       // the board re-read the answer ends with
    const first = answer('A');
    await answer('B');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release();
    await first;
  });

  it('shows its pick while written, and frees the next pair before the board re-reads', async () => {
    rank.pair = pairN(1);
    respond({ kind: 'movie', pair: pairN(2), reason: '' });
    const release = held(board());
    const answering = answer('TIE');
    expect(rank.pending).toBe('duel-TIE');
    expect(rank.busy).toBe(true);
    await vi.waitFor(() => expect(rank.pair.token).toBe('t2'));
    expect(rank.busy).toBe(false);
    expect(rank.pending).toBeNull();
    release();
    await answering;
  });

  it("keeps the newest answer's line when an older re-read lands last", async () => {
    rank.pair = pairN(1);
    respond({ kind: 'movie', pair: pairN(2), log: ['first'] });
    const release = held(board());
    const first = answer('A');
    await vi.waitFor(() => expect(rank.busy).toBe(false));
    respond({ kind: 'movie', pair: pairN(3), log: ['second'] });
    respond(board());
    await answer('B');
    release();
    await first;
    expect(rank.log).toEqual(['second']);
  });
});

describe('a kind switch (§4.1 rule 5)', () => {
  it('clears the kind-scoped filters and leaves the rest of the draft alone', async () => {
    // Genre and decade vocabularies are kind-scoped; the rest of the draft is not.
    draft.q = 'heat';
    draft.genre = 'Thriller';
    draft.decade = '1990';
    draft.runtime_max = '110';
    draft.seen = 'seen';
    draft.terms = [COSY];
    respond({ genres: ['Drama'], decades: [2000] });
    respond(board({ kind: 'series' }));
    await chooseKind('series');

    expect(draft.genre).toBe('');
    expect(draft.decade).toBe('');
    expect(draft.q).toBe('heat');
    expect(draft.terms, 'a term spans both kinds').toEqual([COSY]);
    expect(draft.runtime_max).toBe('110');
    expect(draft.seen).toBe('seen');

    expect(facets.genres).toEqual(['Drama']);
    const boardUrl = fetchMock.mock.calls[1][0];
    expect(boardUrl).toContain('kind=series');
    expect(boardUrl).toContain('q=heat');
    expect(boardUrl).not.toContain('genre=');
    expect(boardUrl).not.toContain('decade=');
  });

  it('flips on the tap and reads the board and its vocabulary at once', async () => {
    respond({ genres: ['Drama'], decades: [2000] });
    respond(board({ kind: 'series' }));
    const switching = chooseKind('series');
    expect(rank.kind).toBe('series');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(rank.reading).toBe(1);             // the board on screen is stale until this lands
    await switching;
    expect(rank.reading).toBe(0);
  });

  it('keeps the newer kind vocabulary when an earlier facets read answers last', async () => {
    // `loadFacets` carries a sequence number, so the later kind's vocabulary wins.
    const releaseFilm = held({ genres: ['Heist'], decades: [1990] });
    respond({ genres: ['Procedural'], decades: [2010] });

    const film = loadFacets('movie');
    await loadFacets('series');
    expect(facets.genres).toEqual(['Procedural']);

    releaseFilm();
    await film;

    expect(facets.genres, 'a superseded read put the film genres over the series board').toEqual([
      'Procedural'
    ]);
    expect(facets.decades).toEqual([2010]);
  });
});

describe('a sitting is a round of fifteen (decision 495)', () => {
  it('counts every accepted answer, ends the round at fifteen and holds the next pair', async () => {
    respond({ kind: 'movie', pair: pairN(0) });
    await openQueue();
    expect(rank.queueOpen).toBe(true);
    expect(roundLine()).toBe(`1 of ${ROUND_SIZE} this round`);

    for (let i = 1; i <= ROUND_SIZE; i++) {
      respond({ kind: 'movie', pair: pairN(i), reason: '' });
      respond(board());
      await answer(i % 3 === 0 ? 'TIE' : 'A');
    }
    expect(rank.roundAnswered).toBe(ROUND_SIZE);
    expect(rank.roundDone).toBe(true);
    expect(roundLine()).toBe(`${ROUND_SIZE} of ${ROUND_SIZE} this round`);
    // The pair the fifteenth answer brought is kept for Keep going rather than thrown away.
    expect(rank.pair.token).toBe(`t${ROUND_SIZE}`);
    expect(ROUND_END_TITLE).toBe(`That's ${ROUND_SIZE}.`);
  });

  it('Keep going starts a new round over the pair already on the table, with no request', () => {
    rank.queueOpen = true;
    rank.pair = pairN(15);
    rank.roundAnswered = ROUND_SIZE;
    rank.roundDone = true;
    keepGoing();
    expect(rank.roundDone).toBe(false);
    expect(rank.roundAnswered).toBe(0);
    expect(rank.pair.token).toBe('t15');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('closing the queue resets the round, and reopening it starts at one', async () => {
    rank.queueOpen = true;
    rank.roundAnswered = 7;
    rank.placed = [{ title_id: 1, name: 'Heat', badge: 'S — the only one' }];
    respond(board({ moves: [] }));
    await closeQueue();
    expect(rank.roundAnswered).toBe(0);
    expect(rank.roundDone).toBe(false);
    expect(rank.placed).toEqual([]);

    respond({ kind: 'movie', pair: pairN(1) });
    await openQueue();
    expect(roundLine()).toBe(`1 of ${ROUND_SIZE} this round`);
  });

  it('does not count a refused answer', async () => {
    rank.queueOpen = true;
    rank.pair = pairN(1);
    rank.roundAnswered = 4;
    respond({ detail: { reason: 'stale_pair', message: 'that pair is no longer on the table' } }, 409);
    respond({ kind: 'movie', pair: pairN(2) });
    await answer('A');
    expect(rank.roundAnswered).toBe(4);
  });

  it('opening the queue again while it is open is not a new round', async () => {
    // Every chip opens the queue, so a second tap must not reset the count.
    rank.queueOpen = true;
    rank.pair = pairN(3);
    rank.roundAnswered = 5;
    await openQueue();
    expect(rank.roundAnswered).toBe(5);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

const DUNE = { title_id: 2, name: 'Drive', from: 4, to: 5, from_label: 'A', to_label: 'S', tier_edit_id: 71 };
const HEAT = { title_id: 1, name: 'Heat', from: 5, to: 4, from_label: 'S', to_label: 'A', tier_edit_id: 72 };
const sent = (i) => [fetchMock.mock.calls[i][0], JSON.parse(fetchMock.mock.calls[i][1].body ?? 'null')];

describe('a round settles into moves (decision 564)', () => {
  it('settles on the fifteenth answer and lists what moved', async () => {
    rank.queueOpen = true;
    rank.pair = pairN(14);
    rank.roundAnswered = ROUND_SIZE - 1;
    respond({ kind: 'movie', pair: pairN(15) });
    respond(board({ moves: [DUNE] }));
    await answer('A');
    expect(rank.roundDone).toBe(true);
    expect(sent(1)[0]).toMatch(/^\/api\/rank\/queue\/settle\?kind=movie/);
    expect(fetchMock.mock.calls[1][1].method).toBe('POST');
    expect(rank.moves).toEqual([{ ...DUNE, undone: false }]);
  });

  it('Done mid-round shows the end card when something moved', async () => {
    rank.queueOpen = true;
    rank.roundAnswered = 6;
    respond(board({ moves: [DUNE] }));
    expect(await finish()).toBe(true);
    expect(rank.roundDone).toBe(true);
    expect(roundLine()).toBe(`6 of ${ROUND_SIZE} this round`);
    expect(rank.moves).toHaveLength(1);
  });

  it('Done mid-round closes when nothing moved, and the close settles nothing again', async () => {
    rank.queueOpen = true;
    rank.roundAnswered = 6;
    respond(board({ moves: [] }));
    expect(await finish()).toBe(false);
    await closeQueue();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(toast.message).toBe('');
  });

  it('Done with no answers settles nothing', async () => {
    rank.queueOpen = true;
    expect(await finish()).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('Undo takes one move back as a drop that names the edit, and marks the row', async () => {
    rank.moves = [{ ...DUNE, undone: false }];
    respond(board());
    expect(await undoMove(rank.moves[0])).toBe(true);
    expect(sent(0)[0]).toMatch(/^\/api\/rank\/drop/);
    expect(sent(0)[1]).toMatchObject({ title_id: 2, tier: 4, undoes: 71 });
    expect(rank.moves[0].undone).toBe(true);
  });

  it('Keep going and a close clear the moves', async () => {
    rank.moves = [{ ...DUNE, undone: false }];
    rank.roundDone = true;
    keepGoing();
    expect(rank.moves).toEqual([]);
    rank.moves = [{ ...DUNE, undone: false }];
    rank.roundAnswered = ROUND_SIZE;
    rank.roundDone = true;
    await closeQueue();
    expect(rank.moves).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();     // the fifteenth answer already settled
  });

  it('a Back close settles and says one move by name', async () => {
    rank.queueOpen = true;
    rank.roundAnswered = 3;
    respond(board({ moves: [DUNE] }));
    await closeQueue();
    expect(toast.message).toBe('Drive moved to S');
    expect(toast.actionLabel).toBe('Undo');
  });

  it('a Back close counts several moves, and its Undo takes them all back', async () => {
    rank.queueOpen = true;
    rank.roundAnswered = 3;
    respond(board({ moves: [DUNE, HEAT] }));
    await closeQueue();
    expect(toast.message).toBe('2 films changed step');
    respond(board());
    respond(board());
    await toast.action();
    expect(sent(1)[1]).toMatchObject({ title_id: 2, tier: 4, undoes: 71 });
    expect(sent(2)[1]).toMatchObject({ title_id: 1, tier: 5, undoes: 72 });
  });
});

describe('after an answer the sheet names where both titles sit', () => {
  it('keeps the placement the answer route returned', async () => {
    rank.queueOpen = true;
    rank.pair = pairN(1);
    const placed = [
      { title_id: 1, name: 'Heat', tier: 5, badge: 'S — just above Drive' },
      { title_id: 2, name: 'Drive', tier: 5, badge: 'S — just below Heat' }
    ];
    respond({ kind: 'movie', pair: pairN(2), placed });
    respond(board());
    await answer('A');
    expect(rank.placed.map((p) => p.title_id)).toEqual([1, 2]);
    expect(rank.placed[0].badge).toBe('S — just above Drive');
  });

  it('starts a new round with no placement line', () => {
    rank.placed = [{ title_id: 1, name: 'Heat', badge: 'S — the only one' }];
    rank.roundDone = true;
    keepGoing();
    expect(rank.placed).toEqual([]);
  });
});

describe('a tap opens a title (decision 496)', () => {
  it('opens the card and writes nothing', () => {
    openTitle(rank.tiers[1].entries[0]);
    expect(rank.opened).toBe(2);
    expect(fetchMock).not.toHaveBeenCalled();
    closeTitle();
    expect(rank.opened).toBeNull();
  });
});

describe('the surface speaks the member register (decision 486)', () => {
  it('names the tier that admitted a survivor in words', () => {
    expect(dnaTierText('extracted', 'heist')).toBe('heist, quoted');
    expect(dnaTierText('projected', 'heist and cozy & mellow')).toBe('heist and cozy & mellow by our read');
  });

  it("says once what the ring on our read's posters means, only while one is on the board", () => {
    draft.terms = [{ ...COSY, label: 'heist', id: 'themes.heist', facet: 'themes' }, { ...COSY, mode: 'out' }];
    apply(board({ dna_tiers: { 1: 'extracted', 2: 'extracted' } }));
    expect(tierLegend()).toBe('');
    apply(board({ dna_tiers: { 1: 'extracted', 2: 'projected' } }));
    expect(tierLegend(), 'a leave-out admits nothing').toBe('Our read says heist; no review does');
  });
});
