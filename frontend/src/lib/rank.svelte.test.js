import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  TIER_THRESHOLD,
  answer,
  apply,
  chipFor,
  chooseKind,
  clearFilters,
  draft,
  drop,
  dropLifted,
  emptyState,
  facets,
  lift,
  load,
  loadFacets,
  neighboursIn,
  putDown,
  rank,
  reset,
  sharpenWhy
} from './rank.svelte.js';

/**
 * §6.3's client rules, at the layer that can actually break them.
 *
 * Three of these are not cosmetic. Cancelling a lift must write *nothing* — proposal 74 calls a
 * modeless lift with no exit the classic tap-to-move failure, and a Cancel that quietly
 * committed would be worse than none. The queue's arm must never be named by the client, or
 * §13's guard becomes advisory. And the board must come from the server, because the client
 * shape of "snapping back" is optimistic re-sorting.
 */

const board = (over = {}) => ({
  kind: 'movie',
  tier_set: ['F', 'D', 'C', 'B', 'A', 'A+', 'S'],
  tiers: [
    {
      index: 6,
      label: 'S',
      entries: [
        {
          title_id: 1,
          name: 'Heat',
          tier: 6,
          assigned_tier: null,
          straddle: 5,
          straddle_badge: 'S/A+',
          badge: 'S — the only one',
          tension: null
        }
      ]
    },
    { index: 5, label: 'A+', entries: [] },
    {
      index: 4,
      label: 'A',
      entries: [
        {
          title_id: 2,
          name: 'Drive',
          tier: 4,
          assigned_tier: 4,
          straddle: null,
          straddle_badge: null,
          badge: 'A — just above Prisoners',
          tension: 'you put it in A — the ledger still reads C'
        },
        {
          title_id: 3,
          name: 'Prisoners',
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
  queue_eligible: 1,
  why: '3 rated · learned cutpoints, refit nightly',
  filters: {},
  dna_tiers: null,
  ...over
});

let fetchMock;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  rank.lifted = null;
  rank.pair = null;
  rank.error = '';
  rank.notice = '';
  rank.log = [];
  // The M3 review found these two sharing state across tests: `draft` is module-level and
  // nothing reset it, so the drop test's query string depended on which filter test ran last.
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

describe('the board comes from the server', () => {
  it('replaces the tiers wholesale rather than merging them', async () => {
    respond(board({ tiers: [{ index: 0, label: 'F', entries: [] }], rated: 0, rated_total: 0 }));
    respond(board());
    await load('movie');
    expect(rank.tiers).toHaveLength(1);
    expect(rank.tiers[0].label).toBe('F');
  });

  it('reads decision 117 as an absence, not an empty object', async () => {
    // `rail.redact` DELETES the gated keys; a client that defaulted `model` to `{}` would
    // render an annotation block with no numbers in it and claim the toggle was on.
    respond(board());
    await load('movie');
    expect(rank.model).toBeNull();

    respond(board({ model: { cutpoints: [0.1], straddle_z: 1, tension_credible_mass: 0.8 } }));
    await load('movie');
    expect(rank.model.cutpoints).toEqual([0.1]);
  });
});

describe('the phone lift (proposals 74, 75)', () => {
  it('lifts a title and puts it down again on a second tap, writing nothing', () => {
    const entry = rank.tiers[2].entries[0];
    lift(entry);
    expect(rank.lifted.title_id).toBe(2);
    lift(entry);
    expect(rank.lifted).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('cancels without writing', () => {
    lift(rank.tiers[2].entries[0]);
    putDown();
    expect(rank.lifted).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('drops the lifted title into the tapped tier, and clears the lift', async () => {
    lift(rank.tiers[0].entries[0]);
    respond(board());
    await dropLifted(4);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/rank/drop');
    expect(url).toContain('kind=movie');
    // §6.3 gives a drop *into* a tier a bare `tier_edit`; the two margin-less duels belong to a
    // drop *between two titles*, which a tap cannot be. [M4.10 finding 17]
    expect(JSON.parse(init.body)).toEqual({ title_id: 1, tier: 4, above: null, below: null });
    expect(rank.lifted).toBeNull();
  });

  it('does nothing at all when nothing is lifted', async () => {
    await dropLifted(4);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('the neighbours a drop lands between', () => {
  it('names nobody for a drop into a tier, however full that tier is', () => {
    // This is finding 17's falsifier. The old body named the tier's current last entry, and
    // `rank/drop.py` then wrote `duel(title_a=<last>, title_b=<dropped>, outcome='A')` — a
    // comparison the person never made, in the same direction every time, on every promotion
    // into a non-empty tier. §6.3 gives a drop into a tier a bare `tier_edit`, and §4.2 is
    // append-only, so each invented duel was permanent. On phones tap-to-tier is the only way
    // to move a title, so "every drop" and "every move" were the same set. [M4.10 finding 17]
    expect(neighboursIn(4, { title_id: 1 })).toEqual({ above: null, below: null });
  });

  it('names none when the tier is empty', () => {
    expect(neighboursIn(5, { title_id: 1 })).toEqual({ above: null, below: null });
  });

  it('never names the title being dropped', () => {
    // The self-filter is still load-bearing on the poster path: without it a drop onto the
    // poster below its own row would name the dragged title as its own new neighbour.
    expect(neighboursIn(4, { title_id: 2 }, 3)).toEqual({ above: null, below: 3 });
    expect(neighboursIn(4, { title_id: 3 })).toEqual({ above: null, below: null });
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
    // The seal is a count of this user's `tier_queue` duels, `duel` is append-only under §4.2, and
    // the winner's row moved that count — so the refusal is PERMANENT for this token, not
    // transient. Leaving the two posters up meant every further tap on them 409'd again and the
    // only way out was Done plus a reopen, which nothing on the surface says. `rate.svelte.js`
    // answers the same seam by re-reading the table; this is that, for the pair.
    // [§6.3, §4.2; cycle 1 m410-rev-03]
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'old', reason: 'x' };
    respond({ detail: { reason: 'stale_pair', message: 'that pair is no longer on the table' } }, 409);
    respond({ kind: 'movie', pair: { title_a: 3, title_b: 4, token: 'fresh', reason: 'y' } });
    await answer('A');

    expect(rank.pair?.token).toBe('fresh');
    expect(fetchMock.mock.calls[1][0]).toContain('/api/rank/queue?');
    // The notice stays: the re-read explains nothing by itself, and a pair that silently changed
    // under the finger is §6.8's register saying nothing about a refused answer.
    expect(rank.notice).toBe('that pair is no longer on the table');

    // And the next tap is answerable, which is the whole of the defect: it used to re-post the
    // token the route had already refused.
    respond({ kind: 'movie', pair: null, reason: 'done' });
    respond(board());
    await answer('B');
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).pair).toBe('fresh');
  });
});

describe('the badge chip (proposal 71)', () => {
  it('gives tension precedence over the straddle badge', () => {
    expect(chipFor({ tension: 'you put it in A — the ledger still reads C', straddle_badge: 'A/S' }))
      .toEqual({ kind: 'tension', text: 'you put it in A — the ledger still reads C' });
  });

  it('falls back to the straddle badge, and to nothing at all', () => {
    expect(chipFor({ tension: null, straddle_badge: 'S/A+' })).toEqual({
      kind: 'straddle',
      text: 'S/A+'
    });
    expect(chipFor({ tension: null, straddle_badge: null })).toBeNull();
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
    // Proposal 80: "say so, with the active filters listed" — the value, not just the field.
    expect(state.text).toBe('Nothing matches DNA term cosy.');
  });

  it('names every active filter with its value', () => {
    apply(board({ rated: 0, rated_total: 40, filters: { dna: 'cosy', runtime_max: 110 } }));
    expect(emptyState().text).toBe('Nothing matches DNA term cosy, under 110 min.');
  });

  it('is absent on a board with titles on it', () => {
    apply(board({ rated: 3, rated_total: 40 }));
    expect(emptyState()).toBeNull();
  });

  /**
   * Decision 209. Between a member's first verdict and the 60 s sweep that fits it there is no
   * `ledger_state` row, so §6.3's board answers `rated_total: 0` — the same payload a member who
   * has never rated anything gets. Two states, one reading: the person who has just rated ten
   * films is told they are at zero, which is §6.8's register saying something untrue about the
   * one thing they just did. The server's `fitting` is what separates them.
   */
  it('says the fit is owed rather than that nobody has started', () => {
    apply(board({ rated: 0, rated_total: 0, tiers: [], fitting: true }));
    const state = emptyState();
    expect(state.kind).toBe('fitting');
    expect(state.text).toContain('still being fitted');
    // "shortly", and no number: the sweep runs every 60 s and a queue ahead of you is allowed,
    // so a duration in this copy would be a promise the app cannot keep.
    expect(state.text).toContain('shortly');
    expect(state.text).not.toContain("you're at 0");
  });

  it('still tells a member who has rated nothing that they have rated nothing', () => {
    // The other half of decision 209: no fit is owed, so the count is the whole truth and
    // proposal 80's handoff is unchanged.
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

  it('clear back to nothing', async () => {
    draft.dna = 'cosy';
    respond(board());
    await clearFilters();
    expect(draft.dna).toBe('');
    expect(fetchMock.mock.calls[0][0]).not.toContain('dna=');
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
});


describe('the neighbours a drop lands between (§6.3)', () => {
  it('names both when the drop lands on a poster', () => {
    // §6.3's "dropping it *between* two titles emits that edit plus two margin-less duels".
    // The M3 review found this case unreachable: both input paths appended to the end, so the
    // second duel — and the ordinal claim it carries — could not be made from the app at all.
    expect(neighboursIn(4, { title_id: 1 }, 3)).toEqual({ above: 2, below: 3 });
  });

  it('names one when the drop lands above the first title in the tier', () => {
    expect(neighboursIn(4, { title_id: 1 }, 2)).toEqual({ above: null, below: 2 });
  });

  it('names neither when no position is given', () => {
    // The other half of finding 17: a drop on the row's empty space is "into this tier" too.
    expect(neighboursIn(4, { title_id: 1 })).toEqual({ above: null, below: null });
  });

  it('ignores a position that is the title being dropped', () => {
    expect(neighboursIn(4, { title_id: 2 }, 2)).toEqual({ above: null, below: null });
  });

  it('names neither when the position is not in that tier any more', () => {
    // A stale board — two tabs, or one read before a nightly refit — names a poster the tier no
    // longer holds. That is not a place between two titles, and guessing the end of the tier
    // there is the same fabrication by another route. [M4.10 findings 17, 18]
    expect(neighboursIn(4, { title_id: 1 }, 999)).toEqual({ above: null, below: null });
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
    // The bug `routes/+page.svelte` already carries a guard and a comment for: tap Series, tap
    // Films, and the Series board lands second under a Films tab.
    let releaseFirst = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise((r) => {
        releaseFirst = () =>
          r({
            ok: true,
            status: 200,
            headers: { get: () => null },
            text: async () => JSON.stringify(board({ kind: 'series', rated: 111 }))
          });
      })
    );
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
  it('drops the lift, because a lift is a pending write naming a bare title id', () => {
    lift(rank.tiers[0].entries[0]);
    expect(rank.lifted).not.toBeNull();
    reset();
    expect(rank.lifted).toBeNull();
    expect(rank.pair).toBeNull();
    expect(rank.booted).toBe(false);
  });

  it('makes an in-flight response land nowhere', async () => {
    let release = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise((r) => {
        release = () =>
          r({
            ok: true,
            status: 200,
            headers: { get: () => null },
            text: async () => JSON.stringify(board({ rated: 999 }))
          });
      })
    );
    const pending = load('movie');
    reset();
    release();
    await pending;
    expect(rank.rated).not.toBe(999);
  });
});

describe('the queue answer keeps its §6.7 line', () => {
  it('survives the board re-read that follows it', async () => {
    // `apply()` blanks `log` because the board GET carries none; the line the answer produced
    // is the only §6.7 narration of a tier-queue duel this page shows.
    rank.pair = { title_a: 1, title_b: 2, arm: 'boundary', token: 'sealed', reason: 'x' };
    respond({ kind: 'movie', pair: null, reason: 'done', log: ['duel(Heat vs Drive) = A'] });
    respond(board());
    await answer('A');
    expect(rank.log).toEqual(['duel(Heat vs Drive) = A']);
  });
});

describe('a drop', () => {
  it('refuses to start a second one while the first is in flight', async () => {
    let release = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise((r) => {
        release = () =>
          r({
            ok: true,
            status: 200,
            headers: { get: () => null },
            text: async () => JSON.stringify(board())
          });
      })
    );
    const first = drop({ title_id: 1, tier: 0 });
    await drop({ title_id: 2, tier: 6 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release();
    await first;
  });
});

describe('the queue answer', () => {
  it('refuses to start a second one while the first is in flight', async () => {
    // Finding 1's client half, and only that half. Two taps on the same sealed pair both passed
    // the old `if (!rank.pair) return;`, both reached `/api/rank/queue/answer`, and each wrote a
    // `duel` row the person answered once — §4.2 is append-only, so the second one stays. The
    // advisory lock in the route is the fix; this stops a double tap being the easy way there.
    rank.pair = { title_a: 1, title_b: 2, token: 'sealed', reason: 'x' };
    let release = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise((r) => {
        release = () =>
          r({
            ok: true,
            status: 200,
            headers: { get: () => null },
            text: async () => JSON.stringify({ kind: 'movie', pair: null, reason: 'done' })
          });
      })
    );
    respond(board());                       // the board re-read the answer ends with
    const first = answer('A');
    await answer('B');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release();
    await first;
  });
});

describe('a kind switch (§4.1 rule 5)', () => {
  it('clears the kind-scoped filters and leaves the rest of the draft alone', async () => {
    // Finding 27: the genre and decade vocabularies are scoped to the kind, so a value from the
    // kind you just left stays in the request, renders blank in a `<select>` that no longer
    // offers it, and comes back as an empty board. `q`, `dna`, `runtime_max` and `seen` are not
    // kind-scoped, and a kind switch is not a reason to throw away what the person typed.
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

  it('keeps the newer kind vocabulary when an earlier facets read answers last', async () => {
    // Finding 21. `load()` has carried a sequence number since M3 and `loadFacets` did not, so
    // two kind taps inside one round trip left whichever request the network happened to finish
    // second — the film genres over a series board, offering a filter that returns nothing and
    // hiding the one that would work. The vocabulary is the half a member reads before they can
    // ask anything, so it is the half where a stale answer costs the most.
    let releaseFilm = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise((r) => {
        releaseFilm = () =>
          r({
            ok: true,
            status: 200,
            headers: { get: () => null },
            text: async () => JSON.stringify({ genres: ['Heist'], decades: [1990] })
          });
      })
    );
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

describe('sharpenWhy', () => {
  // Decision 209's window, one surface further in than `emptyState`. The branch this covers used
  // to tell a member who had rated ten titles that the queue draws from titles they have rated.
  it('names the owed fit rather than the ratings, while the first fit is owed', () => {
    apply({ tiers: [], rated: 0, rated_total: 0, queue_eligible: 0, fitting: true });
    expect(sharpenWhy().kind).toBe('fitting');
    expect(sharpenWhy().text).not.toMatch(/titles you have rated/);
  });

  it('still tells a member who has rated nothing that the queue draws from ratings', () => {
    apply({ tiers: [], rated: 0, rated_total: 0, queue_eligible: 0, fitting: false });
    expect(sharpenWhy().kind).toBe('thin');
    expect(sharpenWhy().text).toMatch(/titles you have rated/);
  });

  it('says the queue is exploring when a rated board straddles nothing', () => {
    apply({ tiers: [], rated: 5, rated_total: 5, queue_eligible: 0, fitting: false });
    expect(sharpenWhy().kind).toBe('exploring');
    expect(sharpenWhy().text).toMatch(/^5 rated/);
  });
});
