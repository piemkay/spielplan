import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ROUND_END_TITLE,
  ROUND_SIZE,
  TIER_THRESHOLD,
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
  searchHint,
  showAll,
  showLess,
  spot,
  typed
} from './rank.svelte.js';
import { hideToast, toast } from './toast.svelte.js';

const board = (over = {}) => ({
  kind: 'movie',
  tier_set: ['F', 'D', 'C', 'B', 'A', 'A+', 'S'],
  tiers: [
    {
      index: 6,
      label: 'S',
      verdict: 'Liked',
      entries: [
        {
          title_id: 1,
          name: 'Heat',
          year: 1995,
          tier: 6,
          assigned_tier: null,
          straddle: 5,
          straddle_badge: 'S or A+?',
          badge: 'S — the only one',
          tension: null
        }
      ]
    },
    { index: 5, label: 'A+', verdict: 'Liked', entries: [] },
    {
      index: 4,
      label: 'A',
      verdict: 'Liked',
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
    }
  ],
  rated: 3,
  rated_total: 3,
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
  rank.expanded = [];
  rank.perTier = null;
  // `draft` is module state, so reset it or a test depends on the last one's filters.
  draft.q = '';
  draft.genre = '';
  draft.decade = '';
  draft.runtime_max = '';
  draft.seen = 'any';
  draft.dna = '';
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
    respond(board({ tiers: [{ index: 0, label: 'F', entries: [] }], rated: 0, rated_total: 0 }));
    respond(board());
    await load('movie');
    expect(rank.tiers).toHaveLength(1);
    expect(rank.tiers[0].label).toBe('F');
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
    await move(rank.tiers[2].entries[1], 6, 1, null);
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

  it("records the tier sheet's pick as explicit (decision 533)", async () => {
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

  it('offers no undo for a new spot in the same tier', async () => {
    respond(board());
    await move(rank.tiers[2].entries[0], 4, 3, null);
    expect(toast.message).toBe('Drive moved to A');
    expect(toast.actionLabel).toBe('');
  });

  it('writes nothing when the title stays where it is', async () => {
    await move(rank.tiers[0].entries[0], 6);
    await move(rank.tiers[2].entries[0], 4, null, 3);
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
  const heat = { title_id: 1, name: 'Heat', kind: 'series', tier: 6 };
  const tierC = { index: 2, label: 'C', verdict: 'Disliked' };

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
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ title_id: 1, tier: 6, undoes: 52 });
  });

  it('offers no Undo for a first placement, and writes nothing for the tier it holds', async () => {
    expect(await cardMove(heat, { index: 6, label: 'S' })).toBe(false);
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
    expect(neighboursAt(5, { title_id: 1 }, 0)).toEqual({ above: null, below: null });
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
  it('names the handoff to Rate with the real count', () => {
    apply(board({ rated: 0, rated_total: 12 }));
    const state = emptyState();
    expect(state.kind).toBe('thin');
    expect(state.text).toContain(`about ${TIER_THRESHOLD} titles`);
    expect(state.text).toContain("you're at 12");
  });

  it('distinguishes "no match" from "not enough yet"', () => {
    apply(board({ rated: 0, rated_total: 40, filters: { dna: 'cosy' } }));
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

  it('still tells a member who has rated nothing that they have rated nothing', () => {
    apply(board({ rated: 0, rated_total: 0, tiers: [], fitting: false }));
    const state = emptyState();
    expect(state.kind).toBe('unrated');
    expect(state.text).toContain("you're at 0");
    expect(state.cta).toBe('Rate some titles');
  });
});

describe('filters', () => {
  it('are sent as query parameters and dropped when empty', async () => {
    draft.dna = 'mood.cosy';
    draft.runtime_max = '110';
    respond(board());
    await load('movie');
    const url = fetchMock.mock.calls[0][0];
    expect(url).toContain('dna=mood.cosy');
    expect(url).toContain('runtime_max=110');
    expect(url).not.toContain('genre=');
  });

  it('show what the Filters control holds as chips, and a chip clears only its own', async () => {
    draft.q = 'heat';
    draft.genre = 'Thriller';
    draft.decade = '1990';
    draft.runtime_max = '110';
    draft.seen = 'unseen';
    draft.dna = 'cosy';
    // The search keeps its own box, so it is no chip.
    expect(filterChips().map((c) => c.text)).toEqual([
      'Thriller',
      '1990s',
      'Up to 1h 50m',
      'Not seen',
      'cosy'
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
    expect(filterChips().map((c) => c.key)).toEqual(['genre', 'runtime_max', 'dna']);
  });

  it('clear back to nothing', async () => {
    draft.dna = 'cosy';
    respond(board());
    await clearFilters();
    expect(draft.dna).toBe('');
    expect(fetchMock.mock.calls[0][0]).not.toContain('dna=');
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
      tiers: [{ index: 6, label: 'S', verdict: 'Liked', count: 3, entries: [board().tiers[0].entries[0]] }]
    });
  const rest = { index: 6, count: 3, offset: 1, entries: [{ title_id: 7 }, { title_id: 8 }] };

  it('asks for the first entries of each tier, and for the rest once one is opened', async () => {
    rank.perTier = 8;
    respond(paged());
    await load('movie');
    expect(fetchMock.mock.calls[0][0]).toContain('per_tier=8');
    expect(rank.tiers[0].entries).toHaveLength(1);

    respond(paged());
    respond(rest);
    await showAll(6);
    const tierRead = fetchMock.mock.calls[2][0];
    expect(tierRead).toContain('/api/rank/tier?');
    expect(tierRead).toContain('index=6');
    expect(tierRead).toContain('offset=1');
    expect(tierRead).not.toContain('per_tier');
    expect(rank.tiers[0].entries.map((e) => e.title_id)).toEqual([1, 7, 8]);
  });

  it('keeps an opened tier whole across a drop, and Show less closes it without a read', async () => {
    rank.expanded = [6];
    respond(paged());
    respond(rest);
    await drop({ title_id: 3, tier: 6 });
    expect(rank.tiers[0].entries).toHaveLength(3);

    showLess(6);
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
    await drop({ title_id: 2, tier: 6 });
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
    expect(spot('A+', neighboursAt(5, { title_id: 1 }))).toEqual({ chip: 'A+', said: 'In A+.' });
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
    openTitle(rank.tiers[2].entries[0]);
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
    draft.dna = 'mood.cosy';
    respond({ genres: ['Drama'], decades: [2000] });
    respond(board({ kind: 'series' }));
    await chooseKind('series');

    expect(draft.genre).toBe('');
    expect(draft.decade).toBe('');
    expect(draft.q).toBe('heat');
    expect(draft.dna).toBe('mood.cosy');
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
    closeQueue();
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

describe('after an answer the sheet names where both titles sit', () => {
  it('keeps the placement the answer route returned', async () => {
    rank.queueOpen = true;
    rank.pair = pairN(1);
    const placed = [
      { title_id: 1, name: 'Heat', tier: 6, badge: 'S — just above Drive' },
      { title_id: 2, name: 'Drive', tier: 6, badge: 'S — just below Heat' }
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
    openTitle(rank.tiers[2].entries[0]);
    expect(rank.opened).toBe(2);
    expect(fetchMock).not.toHaveBeenCalled();
    closeTitle();
    expect(rank.opened).toBeNull();
  });
});

describe('the surface speaks the member register (decision 486)', () => {
  it('names the two DNA tiers in words', () => {
    expect(dnaTierText(['extracted', 'projected'])).toBe('quoted + our read');
  });
});
