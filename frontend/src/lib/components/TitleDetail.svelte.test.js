/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { get as read } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

// The sheet is a history entry: `pushState` adds it and Back (here `history.back`) takes it away.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  goto: vi.fn(),
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));
vi.mock('$lib/cardJump.js', () => ({ jumpHome: vi.fn() }));

import { goto } from '$app/navigation';
import { api, get, post } from '$lib/api.js';
import { jumpHome } from '$lib/cardJump.js';
import { hideToast, toast } from '$lib/toast.svelte.js';
import { wishes } from '$lib/wish.svelte.js';
import TitleDetail from './TitleDetail.svelte';

const NOTE = '[data-testid="title-series-unseen-note"]';
const SEEN_NOTE = '[data-testid="title-seen-note"]';
const JELLYFIN_WHY = '[data-testid="title-jellyfin-why"]';

/** One `GET /api/titles/{id}` payload, in the shape `api/library.py` sends. */
const payload = (over = {}) => ({
  title: {
    id: 6,
    name: 'Severance',
    kind: 'series',
    year: 2022,
    runtime_min: 24,
    seen_state: 'seen',
    original_name: null,
    overview: null,
    trailer_key: null,
    ...over
  },
  // No `model_line`: the server omits it while Show the model is off, the default here.
  genres: [],
  credits: [],
  platform_ratings: { items: [], note: 'display-only' },
  dna: { extracted: [], projected: [] },
  shares: [],
  actions: { play_on_jellyfin: null, play_reason: 'no_server' }
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(goto).mockReset();
  vi.mocked(jumpHome).mockReset();
  nav.page.update((p) => ({ ...p, state: {} }));
  vi.spyOn(history, 'back').mockImplementation(() =>
    nav.page.update((p) => ({ ...p, state: { ...p.state, sheets: (p.state.sheets ?? []).slice(0, -1) } }))
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

// `over` patches the title; `extra` patches the body and, under `props`, the component's props.
async function open(over = {}, extra = {}) {
  const { props: extraProps = {}, ...body } = extra;
  vi.mocked(get).mockResolvedValue({ ...payload(over), ...body });
  const app = mount(TitleDetail, {
    target,
    props: {
      titleId: 6,
      onClose: () => {},
      onPerson: () => {},
      onStateChange: () => {},
      ...extraProps
    }
  });
  await settle();
  return app;
}

const byTestId = (testid) => target.querySelector(`[data-testid="${testid}"]`);

const tapWatched = async () => {
  const button = byTestId('title-watched');
  expect(button, 'the seen control is not on screen').not.toBeNull();
  button.click();
  await settle();
};

describe("decisions 210(a) and 533's why-line", () => {
  it('warns before the tap that a series stays in this app, watched or not', async () => {
    for (const seen_state of ['seen', 'unseen']) {
      const app = await open({ seen_state });
      try {
        expect(target.querySelector(NOTE).textContent).toContain('kept in Spielplan only');
      } finally {
        unmount(app);
      }
    }
  });

  it('is absent on a film', async () => {
    const app = await open({ kind: 'movie' });
    try {
      expect(target.querySelector(NOTE)).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('the sync note', () => {
  it('prints the reason a successful write gives, rather than claiming a push', async () => {
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: true, reason: 'series seen is app-only' });
    const app = await open({ seen_state: 'unseen' });
    try {
      await tapWatched();
      const note = byTestId('title-seen-note').textContent;
      expect(note).toContain('Jellyfin keeps its own episode history');
      expect(note).not.toContain('up to date');
    } finally {
      unmount(app);
    }
  });

  it('still says so when the push really did land', async () => {
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: true, reason: null });
    const app = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      await tapWatched();
      expect(byTestId('title-seen-note').textContent).toContain('Jellyfin is up to date');
    } finally {
      unmount(app);
    }
  });

  it('sits under the pair and outside the credits, as a quiet line', async () => {
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: false, reason: 'not on Jellyfin' });
    const app = await open(
      { kind: 'movie', seen_state: 'unseen' },
      { credits: [{ person_id: 1, name: 'Michael Mann', job: 'Director', sources: ['tmdb'] }] }
    );
    try {
      await tapWatched();
      const note = target.querySelector(SEEN_NOTE);
      expect(note.textContent).toContain("isn't in your Jellyfin library");
      expect(note.getAttribute('role')).toBe('status');
      expect(note.closest('section'), 'the note is inside a section').toBeNull();
      expect(note.closest('.pair'), 'the note is inside the button row').toBeNull();
      const pair = byTestId('title-not-seen').closest('.pair');
      expect(pair.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(note.classList.contains('footnote')).toBe(true);
    } finally {
      unmount(app);
    }
  });
});

describe("§6.0's second action, on a screen with no hover", () => {
  it('says why Play on Jellyfin is disabled, in the quiet-reason register', async () => {
    const app = await open({}, { actions: { play_on_jellyfin: null, play_reason: 'no_server' } });
    try {
      const why = target.querySelector(JELLYFIN_WHY);
      expect(why, 'the disabled action carries no reason on the screen').not.toBeNull();
      expect(why.textContent).toContain("Jellyfin isn't connected");
      expect(why.textContent, 'a milestone label in member copy (decision 486)').not.toMatch(
        /\bM\d\b/
      );
      expect(why.classList.contains('footnote')).toBe(true);
      expect(why.tagName).toBe('P');

      const button = target.querySelector('button.btn-primary[disabled]');
      expect(button, 'the disabled action is gone entirely').not.toBeNull();
      expect(
        button.getAttribute('title'),
        'the reason is still only in a hover tooltip as well'
      ).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('drops the line once a server is linked', async () => {
    const app = await open(
      {},
      { actions: { play_on_jellyfin: 'http://jf.lan/web/#/details?id=1', play_reason: null } }
    );
    try {
      expect(target.querySelector(JELLYFIN_WHY)).toBeNull();
      expect(target.querySelector('a.btn-primary')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('the card is a sheet (decision 527)', () => {
  const sheets = () => read(nav.page).state.sheets ?? [];
  const dialog = () => target.querySelector('[role="dialog"]');

  it('opens as a history entry and closes by its button, the scrim, Escape and Back', async () => {
    for (const leave of [
      () => target.querySelector('button.close').click(),
      () => target.querySelector('.scrim').click(),
      () => dialog().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })),
      () => history.back()
    ]) {
      const onClose = vi.fn();
      const app = await open({}, { props: { onClose } });
      try {
        expect(dialog().getAttribute('aria-label')).toBe('Title detail');
        expect(sheets(), 'the sheet pushed no history entry').toHaveLength(1);
        leave();
        await settle();
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(sheets(), 'the history entry outlived the sheet').toHaveLength(0);
      } finally {
        unmount(app);
      }
    }
  });

  it('stays open for a tap inside it and for another key', async () => {
    const onClose = vi.fn();
    vi.mocked(post).mockResolvedValue({ synced: true });
    const app = await open({}, { props: { onClose } });
    try {
      dialog().dispatchEvent(new KeyboardEvent('keydown', { key: 'm', bubbles: true }));
      await tapWatched();
      expect(onClose).not.toHaveBeenCalled();
      expect(sheets()).toHaveLength(1);
    } finally {
      unmount(app);
    }
  });

  it('closes itself before a credit filters the library, so Back has nothing left to undo', async () => {
    const calls = [];
    const credit = { person_id: 1, name: 'Michael Mann', job: 'Director', role_class: 'director' };
    const app = await open(
      { kind: 'movie' },
      {
        credits: [credit],
        props: {
          onClose: () => calls.push(['close', sheets().length]),
          onPerson: (c) => calls.push(['person', c.name, sheets().length])
        }
      }
    );
    try {
      target.querySelector('.person').click();
      await settle();
      expect(calls).toEqual([['close', 0], ['person', 'Michael Mann', 0]]);
    } finally {
      unmount(app);
    }
  });
});

describe('the DNA card and the credits after the 2026-09-25 user test', () => {
  it('marks a quote cut mid-sentence and draws a one-source projection fainter, dropping none', async () => {
    // A projected weight is never a filter (§4.1 rule 2): the one-source chip stays, fainter.
    const app = await open(
      {},
      {
        dna: {
          extracted: [
            {
              term: 'mood.gritty',
              facet: 'mood',
              salience: 2,
              provider: '',
              evidence: [{ quote: 'not this serious, gritty crime epic', source: 'trakt:1' }]
            }
          ],
          projected: [
            { term: 'characters.teen_protagonist', facet: 'characters', weight: 1, via: 'movielens' },
            { term: 'themes.obsession', facet: 'themes', weight: 4, via: 'keyword' }
          ]
        }
      }
    );
    try {
      expect(target.querySelector('.quote').textContent).toBe(
        '“…not this serious, gritty crime epic…”'
      );
      // Read by label, not position: the one-source guess is folded beside the other.
      const chips = [...target.querySelectorAll('.chip')];
      expect(chips).toHaveLength(2);
      const faint = Object.fromEntries(
        chips.map((c) => [c.dataset.weight, c.classList.contains('faint')])
      );
      expect(faint).toEqual({ 1: true, 4: false });
    } finally {
      unmount(app);
    }
  });

  it('keys the credit list by person and class, so one person in two classes is two rows', async () => {
    // `credits_for` folds per (person, role class), and the card's key follows it.
    const credit = (over) => ({ name: 'Michael Mann', department: 'Directing', sources: ['tmdb'], ...over });
    const app = await open(
      {},
      {
        credits: [
          credit({ person_id: 1, role_class: 'director', job: 'Director' }),
          credit({ person_id: 1, role_class: 'writer', job: 'Writer', jobs: ['Writer', 'Screenplay'] })
        ]
      }
    );
    try {
      expect(target.querySelectorAll('.people .person')).toHaveLength(2);
    } finally {
      unmount(app);
    }
  });
});

describe('Play says which of its two reasons it is', () => {
  it('names a title outside the library as that, not as a missing server', async () => {
    const app = await open(
      { kind: 'movie' },
      { actions: { play_on_jellyfin: null, play_reason: 'not_in_library' } }
    );
    try {
      const why = target.querySelector(JELLYFIN_WHY).textContent;
      expect(why).toContain('Not in your Jellyfin library');
      expect(why).not.toContain('server');
    } finally {
      unmount(app);
    }
  });
});

describe("the model line is Show the model's", () => {
  it('is absent when the payload carries none, and drawn when it does', async () => {
    let app = await open();
    expect(target.querySelector('[data-testid="title-model-line"]')).toBeNull();
    unmount(app);
    app = await open(
      {},
      {
        model_line: {
          available: true,
          text: 'b(t) 0.52 · β 0.20 · gate 0.93',
          e_source: 'cold_tower',
          bundle: 'v20260926b'
        }
      }
    );
    try {
      const line = target.querySelector('[data-testid="title-model-line"]').textContent;
      expect(line).toContain('b(t) 0.52');
      expect(line).toContain("Placed by what it's about");
      expect(line).not.toMatch(/cold_tower|bundle|v20260926b/);
    } finally {
      unmount(app);
    }
  });

  it('says in a sentence when there are no numbers for the title', async () => {
    const app = await open({}, { model_line: { available: false, reason: 'no such title' } });
    try {
      expect(target.querySelector('[data-testid="title-model-line"]').textContent.trim()).toBe(
        'No numbers for this one — no such title'
      );
    } finally {
      unmount(app);
    }
  });
});

describe('a card opened from a poster (decision 530)', () => {
  it('shows the tapped poster and its name before the read lands, and the rest once it has', async () => {
    /** @type {(body: any) => void} */
    let land = () => {};
    vi.mocked(get).mockReturnValue(new Promise((resolve) => (land = resolve)));
    const seed = { id: 6, kind: 'movie', name: 'Heat', year: 1995, runtime_min: 170 };
    const app = mount(TitleDetail, {
      target,
      props: { titleId: 6, seed, onClose: () => {}, onPerson: () => {}, onStateChange: () => {} }
    });
    try {
      await settle();
      expect(target.querySelector('.title-1').textContent).toBe('Heat');
      expect(target.querySelector('.sub').textContent).toMatch(/^1995 · 2h/);
      expect(target.querySelector('[data-testid="rate-poster"]').getAttribute('data-title-id')).toBe('6');
      expect(target.textContent, 'a loading line instead of the film').not.toContain('Loading');
      expect(byTestId('title-not-seen'), 'answers before the read').toBeNull();

      land(payload({ kind: 'movie', name: 'Heat', year: 1995, runtime_min: 170, seen_state: 'unseen' }));
      await settle();
      expect(target.querySelector('.title-1').textContent).toBe('Heat');
      expect(byTestId('title-not-seen')).not.toBeNull();
      expect(target.querySelector('[data-testid="title-more"]').hidden).toBe(false);
    } finally {
      unmount(app);
    }
  });
});

describe('the header', () => {
  it('draws the poster primitive for this title, inert and nameless', async () => {
    const app = await open();
    try {
      const poster = target.querySelector('[data-testid="rate-poster"]');
      expect(poster, 'no poster on the title card').not.toBeNull();
      expect(poster.getAttribute('data-title-id')).toBe('6');
      expect(poster.querySelector('.name'), 'the name is printed twice').toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('reads the year and the runtime, then who directed it, and no column value', async () => {
    const app = await open(
      { kind: 'movie', seen_state: 'unseen', year: 2004, runtime_min: 120 },
      { credits: [{ person_id: 1, name: 'Michael Mann', job: 'Director', role_class: 'director' }] }
    );
    try {
      const sub = target.querySelector('.sub').textContent;
      expect(sub).toMatch(/^2004 · 2h/);
      expect(sub).not.toContain('movie');
      expect(target.querySelector('[data-testid="title-directed"]').textContent).toBe(
        'Directed by Michael Mann'
      );
    } finally {
      unmount(app);
    }
  });

  it('names its first two genres under the year, and no line when it has none', async () => {
    let app = await open({}, { genres: ['Crime', 'Thriller', 'Drama'] });
    try {
      expect(target.querySelector('[data-testid="title-genres"]').textContent).toBe('Crime, thriller');
    } finally {
      unmount(app);
    }
    app = await open();
    try {
      expect(target.querySelector('[data-testid="title-genres"]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('links the trailer by what it is, on a row of its own, and never prints its key', async () => {
    const app = await open({ trailer_key: 'F-eMt3SrfFU' });
    try {
      const link = target.querySelector('a.trailer');
      expect(link.closest('.pair'), 'the trailer shares a row').toBeNull();
      expect(link.textContent.trim()).toBe('Trailer');
      expect(link.getAttribute('aria-label')).toBe('Watch the trailer on YouTube');
      expect(link.getAttribute('href')).toBe('https://www.youtube.com/watch?v=F-eMt3SrfFU');
      expect(link.textContent).not.toContain('F-eMt3SrfFU');
    } finally {
      unmount(app);
    }
  });
});

describe('Show on map waits with the Map (decision 548)', () => {
  it('is on no card', async () => {
    const app = await open();
    try {
      expect(target.textContent).not.toContain('Show on map');
      expect(target.querySelector('a[href^="/map"]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('the DNA card in the member register', () => {
  const dna = {
    extracted: [
      {
        term: 'era.wwii',
        facet: 'era',
        label: 'World War II',
        gloss: 'set during the war',
        provider: '',
        evidence: [
          {
            quote: 'not this serious, gritty crime epic that is being attempted here',
            source: 'trakt:4'
          },
          { quote: 'A war film.', source: 'wiki:music' }
        ]
      }
    ],
    projected: [
      { term: 'characters.teen_protagonist', facet: 'characters', label: 'teen lead', weight: 1 },
      { term: 'place.los_angeles', facet: 'place', label: 'Los Angeles', weight: 4 }
    ]
  };

  it('names terms by label, drops the weights and the raw ids, and names sources', async () => {
    const app = await open({}, { dna });
    try {
      expect(target.querySelector('.tag .term').textContent.trim()).toBe('World War II');
      const text = target.textContent;
      expect(text).not.toContain('era.wwii');
      expect(text).not.toContain('characters.teen_protagonist');
      expect(text).not.toMatch(/\bsal\b/);
      expect(text).not.toMatch(/DNA|EXTRACTED|PROJECTED/);
      const sources = [...target.querySelectorAll('.src')].map((s) => s.textContent.trim());
      expect(sources).toEqual(['Trakt', 'Wikipedia · music']);
    } finally {
      unmount(app);
    }
  });

  it('marks a quote cut from a longer sentence at the end it was cut', async () => {
    const app = await open({}, { dna });
    try {
      const quotes = [...target.querySelectorAll('.quote')].map((q) => q.textContent);
      expect(quotes[0]).toBe(
        '“…not this serious, gritty crime epic that is being attempted here…”'
      );
      expect(quotes[1]).toBe('“A war film.”');
    } finally {
      unmount(app);
    }
  });

  it('keeps every inferred term, folds the one-source guess, and leaves the count to Show the model', async () => {
    // The source count is Show the model's; the one-source guess is folded, never dropped.
    const app = await open({}, { dna });
    try {
      const chips = [...target.querySelectorAll('.chips .chip')];
      expect(chips, '§4.1 rule 2: a weight is never a filter').toHaveLength(2);
      const strong = target.querySelector('.chips .chip:not(.faint)');
      expect(strong.querySelector('.chiplabel').textContent.trim()).toBe('Los Angeles');
      expect(strong.closest('[data-testid="title-weak-chips"]')).toBeNull();
      const fold = target.querySelector('[data-testid="title-weak-chips"]');
      expect(fold.tagName).toBe('DETAILS');
      expect(fold.open).toBe(false);
      expect(fold.querySelector('summary').textContent.trim()).toBe('Show 1 more');
      const weak = fold.querySelector('.chip');
      expect(weak.querySelector('.chiplabel').textContent.trim()).toBe('teen lead');
      expect(weak.classList.contains('faint')).toBe(true);
      expect(target.querySelectorAll('.chip .n'), 'a count reached a member').toHaveLength(0);
    } finally {
      unmount(app);
    }
  });
});

describe('the card leads with what a member opens it for', () => {
  const credits = Array.from({ length: 8 }, (_, i) => ({
    person_id: i + 1,
    name: `Person ${i + 1}`,
    job: i ? 'Actor' : 'Director',
    role_class: i ? 'cast' : 'director',
    sources: ['tmdb']
  }));

  it('says why the title is suggested, and nothing when the payload gives no reason', async () => {
    let app = await open({ kind: 'movie' }, { why: 'Because you liked Heat' });
    const why = target.querySelector('[data-testid="title-why"]');
    expect(why.textContent.trim()).toBe('Because you liked Heat');
    // It comes before Play, the pair and the synopsis.
    const pair = byTestId('title-not-seen');
    expect(why.compareDocumentPosition(pair) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    unmount(app);
    for (const absent of [{}, { why: null }, { why: '  ' }]) {
      app = await open({ kind: 'movie' }, absent);
      expect(target.querySelector('[data-testid="title-why"]')).toBeNull();
      unmount(app);
    }
  });

  it('puts Play, the ranking rows, the pair and the trailer above the synopsis, in that order', async () => {
    const app = await open(
      { kind: 'movie', overview: 'A thief and a cop.', trailer_key: 'x' },
      { ranking: { tier: null, tension: null, tiers: [] } }
    );
    try {
      const order = ['.play', '[data-testid="rank-card-tier"]', '[data-testid="title-watched"]', 'a.trailer', '.overview'];
      for (let i = 1; i < order.length; i++) {
        const [before, after] = [order[i - 1], order[i]].map((sel) => target.querySelector(sel));
        expect(before.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING, order[i]).toBeTruthy();
      }
    } finally {
      unmount(app);
    }
  });

  it('shows the first few names and folds the rest, the scores and both tiers behind one disclosure', async () => {
    const app = await open(
      { kind: 'movie' },
      {
        credits,
        platform_ratings: {
          items: [{ platform: 'imdb', metric: 'user_score', score: 7.8, scale: 10 }],
          note: 'For reference only'
        },
        dna: {
          extracted: [
            {
              term: 'mood.tense',
              facet: 'mood',
              provider: '',
              evidence: [{ quote: 'Tense.', source: 'imdb:1' }]
            }
          ],
          projected: [{ term: 'place.los_angeles', facet: 'place', weight: 4 }]
        }
      }
    );
    try {
      const more = target.querySelector('[data-testid="title-more"]');
      expect(more.tagName).toBe('DETAILS');
      expect(more.open, 'the fold is open by default').toBe(false);
      expect(more.querySelector('summary').textContent.trim()).toBe('More about this film');
      const people = [...target.querySelectorAll('.people .person')];
      expect(people).toHaveLength(8);
      const outside = people
        .filter((p) => !more.contains(p))
        .map((p) => p.querySelector('.pname').textContent);
      expect(outside).toEqual(['Person 1', 'Person 2', 'Person 3', 'Person 4', 'Person 5']);
      for (const sel of ['.scores', '.tag', '.chip', '[data-testid="credit-count"]']) {
        expect(more.contains(target.querySelector(sel)), `${sel} is not behind the fold`).toBe(true);
      }
      expect(target.querySelector('[data-testid="credit-count"]').textContent).toBe('8 of 8');
    } finally {
      unmount(app);
    }
  });

  it('names a series fold as a series', async () => {
    const app = await open({ kind: 'series' });
    try {
      expect(target.querySelector('[data-testid="title-more"] summary').textContent.trim()).toBe(
        'More about this series'
      );
    } finally {
      unmount(app);
    }
  });

  it('draws no verdict tiles: the ladder rates (decision 536)', async () => {
    const app = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      expect(target.querySelector('[data-answer]')).toBeNull();
      expect(target.textContent).not.toMatch(/Disliked|Liked|You haven't rated this yet/);
    } finally {
      unmount(app);
    }
  });
});

describe('the DNA card says each thing once', () => {
  it('prints one block per quoted term with each quote once, and never repeats it as a guess', async () => {
    const quote = { quote: 'the cinematic master poet of nocturnal Los Angeles', source: 'metacritic:1' };
    const la = { term: 'place.los_angeles', facet: 'place', label: 'Los Angeles' };
    const app = await open(
      {},
      {
        dna: {
          extracted: [
            { ...la, provider: '', evidence: [quote, { ...quote }] },
            { ...la, provider: 'terra', evidence: [quote, { quote: 'the nighttime LA', source: 'imdb:2' }] }
          ],
          projected: [
            { term: 'place.los_angeles', facet: 'place', label: 'Los Angeles', weight: 3 },
            { term: 'themes.loneliness', facet: 'themes', label: 'loneliness', weight: 2 }
          ]
        }
      }
    );
    try {
      const tags = [...target.querySelectorAll('.tag')];
      expect(tags).toHaveLength(1);
      // The one chip's quotes, each once, on the evidence card beneath the chips.
      const card = target.querySelector('[data-testid="title-evidence"]');
      expect([...card.querySelectorAll('.quote')].map((q) => q.textContent)).toEqual([
        '“…the cinematic master poet of nocturnal Los Angeles…”',
        '“…the nighttime LA…”'
      ]);
      const chips = [...target.querySelectorAll('.chip .chiplabel')].map((c) => c.textContent.trim());
      expect(chips).toEqual(['loneliness']);
    } finally {
      unmount(app);
    }
  });

  it('shows the quotes of the one chip a member picks', async () => {
    const tag = (term, label, quote) => ({
      term, facet: term.split('.')[0], label, provider: '', evidence: [{ quote, source: 'trakt:1' }]
    });
    const app = await open(
      {},
      {
        dna: {
          extracted: [tag('mood.tense', 'tense', 'Tense.'), tag('place.los_angeles', 'Los Angeles', 'Nocturnal LA.')],
          projected: []
        }
      }
    );
    try {
      const card = () => target.querySelector('[data-testid="title-evidence"]');
      const chips = [...target.querySelectorAll('.tag')];
      expect(chips.map((c) => c.getAttribute('aria-pressed'))).toEqual(['true', 'false']);
      expect(card().textContent).toContain('Tense.');
      chips[1].click();
      flushSync();
      expect(chips.map((c) => c.getAttribute('aria-pressed'))).toEqual(['false', 'true']);
      expect(card().textContent).toContain('Nocturnal LA.');
      expect(card().textContent).not.toContain('Tense.');
    } finally {
      unmount(app);
    }
  });

  it("folds a guess that contradicts the title's own quoted pace", async () => {
    const app = await open(
      {},
      {
        dna: {
          extracted: [
            { term: 'pacing.frenetic', facet: 'pacing', label: 'frenetic', provider: '', evidence: [] }
          ],
          projected: [
            { term: 'pacing.slow_burn', facet: 'pacing', label: 'slow burn', weight: 3 },
            { term: 'mood.tense', facet: 'mood', label: 'tense', weight: 3 }
          ]
        }
      }
    );
    try {
      const fold = target.querySelector('[data-testid="title-weak-chips"]');
      expect([...fold.querySelectorAll('.chiplabel')].map((c) => c.textContent.trim())).toEqual([
        'slow burn'
      ]);
      const open = [...target.querySelectorAll('.chips .chip')].filter((c) => !fold.contains(c));
      expect(open.map((c) => c.querySelector('.chiplabel').textContent.trim())).toEqual(['tense']);
    } finally {
      unmount(app);
    }
  });
});

describe('the name a German viewer knows (decision 516)', () => {
  const langs = Object.getOwnPropertyDescriptor(Navigator.prototype, 'languages');
  afterEach(() => {
    if (langs) Object.defineProperty(Navigator.prototype, 'languages', langs);
  });
  const speak = (tags) =>
    Object.defineProperty(Navigator.prototype, 'languages', { configurable: true, get: () => tags });

  it('leads with the German original on a German phone and keeps the English beside it', async () => {
    speak(['de-DE', 'de']);
    const title = { name: 'Wonderfully Beautiful', original_name: 'Wunderschön', original_language: 'de' };
    const app = await open(title);
    try {
      expect(target.querySelector('h2').textContent).toBe('Wunderschön');
      expect(target.querySelector('[data-testid="title-alt-name"]').textContent).toBe(
        'Wonderfully Beautiful'
      );
    } finally {
      unmount(app);
    }
  });

  it('keeps the English name in front everywhere else', async () => {
    speak(['en-US']);
    const app = await open({
      name: 'Wonderfully Beautiful',
      original_name: 'Wunderschön',
      original_language: 'de'
    });
    try {
      expect(target.querySelector('h2').textContent).toBe('Wonderfully Beautiful');
      expect(target.querySelector('[data-testid="title-alt-name"]').textContent).toBe('Wunderschön');
    } finally {
      unmount(app);
    }
  });
});

describe('credits', () => {
  it('print one row per person and role, with only the jobs that are different credits', async () => {
    const credits = [
      {
        person_id: 1, name: 'Elliot Goldenthal', job: 'Original Music Composer',
        role_class: 'composer', jobs: ['Composer', 'Original Music Composer'],
        sources: ['tmdb', 'wikidata']
      },
      {
        person_id: 2, name: 'Michael Mann', job: 'Writer', role_class: 'writer',
        jobs: ['Screenplay', 'Writer'], sources: ['tmdb', 'wikidata']
      },
      {
        person_id: 2, name: 'Michael Mann', job: 'Director', role_class: 'director',
        jobs: ['Director'], sources: ['tmdb']
      },
      {
        person_id: 3, name: 'Somebody', job: 'Screenplay', role_class: 'writer',
        jobs: ['Novel', 'Screenplay'], sources: ['tmdb']
      }
    ];
    const app = await open({}, { credits });
    try {
      const rows = [...target.querySelectorAll('.person .job')].map((r) => r.textContent.trim());
      expect(rows).toEqual(['Original Music Composer', 'Writer', 'Director', 'Screenplay · Novel']);
      // The source count is the operator's provenance (decision 486).
      expect(target.textContent).not.toContain('2 sources');
    } finally {
      unmount(app);
    }
  });
});

describe('faces (decision 528)', () => {
  it("print an actor's character and a crew member's job under a photo or their initials", async () => {
    const credits = [
      { person_id: 1, name: 'Michael Mann', job: 'Director', role_class: 'director', photo: true },
      {
        person_id: 2, name: 'Al Pacino', job: 'Actor', role_class: 'cast',
        character: 'Vincent Hanna', photo: true
      },
      { person_id: 3, name: 'Jon Voight', job: 'Actor', role_class: 'cast', character: 'Nate' }
    ];
    const app = await open({}, { credits });
    try {
      const people = [...target.querySelectorAll('.strip .person')];
      expect(people.map((p) => p.querySelector('.job').textContent.trim())).toEqual([
        'Director',
        'Vincent Hanna',
        'Nate'
      ]);
      expect(people[1].querySelector('img').getAttribute('src')).toBe('/api/art/person/2');
      expect(people[2].querySelector('img')).toBeNull();
      expect(people[2].querySelector('.face').textContent.trim()).toBe('JV');
    } finally {
      unmount(app);
    }
  });
});

describe('Watched and Not seen', () => {
  const pressed = (testid) => byTestId(testid).getAttribute('aria-pressed');

  it('Not seen writes through the Rate session, says so, and reads the card again', async () => {
    vi.mocked(post).mockResolvedValue({});
    const onStateChange = vi.fn();
    const app = await open({ kind: 'movie', seen_state: 'seen' }, { props: { onStateChange } });
    try {
      expect(byTestId('title-watched').textContent.trim()).toBe('Watched');
      expect([pressed('title-watched'), pressed('title-not-seen')]).toEqual(['true', 'false']);
      // The server's own reading after the write, why line and all (decision 515).
      vi.mocked(get).mockResolvedValue({
        ...payload({ kind: 'movie', seen_state: 'unseen' }),
        why: 'Because you liked Heat'
      });
      byTestId('title-not-seen').click();
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/rate/title/6', { answer: 'not_seen' });
      expect(vi.mocked(get)).toHaveBeenCalledTimes(2);
      expect(byTestId('title-seen-note').textContent).toBe('Saved — marked not seen.');
      expect(byTestId('title-watched').textContent.trim()).toBe('Mark as watched');
      expect([pressed('title-watched'), pressed('title-not-seen')]).toEqual(['false', 'true']);
      expect(byTestId('title-why').textContent).toBe('Because you liked Heat');
      expect(onStateChange).toHaveBeenCalledWith(6, 'unseen');
    } finally {
      unmount(app);
    }
  });

  it('Watched marks the title seen and drops the why line', async () => {
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: true, reason: null });
    const onStateChange = vi.fn();
    const app = await open(
      { kind: 'movie', seen_state: 'unseen' },
      { why: 'Because you liked Heat', props: { onStateChange } }
    );
    try {
      expect(byTestId('title-watched').textContent.trim()).toBe('Mark as watched');
      await tapWatched();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/titles/6/state', { state: 'seen' });
      expect(byTestId('title-watched').textContent.trim()).toBe('Watched');
      expect(pressed('title-watched')).toBe('true');
      expect(byTestId('title-why')).toBeNull();
      expect(onStateChange).toHaveBeenCalledWith(6, 'seen');
    } finally {
      unmount(app);
    }
  });

  it('writes nothing for a tap on the state that already stands', async () => {
    vi.mocked(post).mockResolvedValue({});
    let app = await open({ kind: 'movie', seen_state: 'seen' });
    try {
      await tapWatched();
      expect(vi.mocked(post)).not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }
    app = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      byTestId('title-not-seen').click();
      await settle();
      expect(vi.mocked(post)).not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }
  });

  it('keeps the note on the card when a write fails', async () => {
    vi.mocked(post).mockRejectedValue(new Error('the server is busy'));
    const app = await open({ kind: 'movie', seen_state: 'seen' });
    try {
      byTestId('title-not-seen').click();
      await settle();
      expect(byTestId('title-seen-note').textContent).toBe('Could not save that — the server is busy');
      expect(pressed('title-watched')).toBe('true');
    } finally {
      unmount(app);
    }
  });
});

describe('the ranking rows and the ladder sheet (decisions 531, 545 and 550)', () => {
  // `api/library.py`'s `ranking`: the person's tier set, best first, each with its word and count.
  const TIERS = [
    ['S', 'All-time favourite', 1],
    ['A+', 'Loved it', 0],
    ['A', 'Liked it', 3],
    ['B', 'It was fine', 2],
    ['C', 'Not really for me', 0],
    ['D', "Didn't like it", 0],
    ['F', 'Hated it', 0]
  ].map(([label, word, count], i) => ({ index: 6 - i, label, word, count, entries: [] }));
  const ranking = (tier, { tension = null, set_up = true } = {}) => ({ set_up, tier, tension, tiers: TIERS });
  const heat = { kind: 'movie', name: 'Heat' };
  const zodiac = { id: 31, name: 'Zodiac', original_name: null, original_language: null, poster_path: null };
  const SHELVES = {
    title_id: 6,
    kind: 'movie',
    current: null,
    shelves: TIERS.map((t) => ({ tier: t.index, word: t.word, count: t.count, films: t.index === 4 ? [zodiac] : [] }))
  };

  const sheet = () => byTestId('ladder-sheet');
  const shelf = (index) => target.querySelector(`[data-testid="ladder-shelf"][data-tier="${index}"]`);
  // The card reads its title; the sheet reads its shelves.
  function serveShelves(title) {
    vi.mocked(get).mockImplementation(async (path) => (path.startsWith('/rate/shelves') ? SHELVES : title));
  }
  async function openLadder() {
    byTestId('rank-card-tier').click();
    await settle();
  }

  beforeEach(() => hideToast());

  it('names the tier by its letter and word, and its sheet moves the title with Undo', async () => {
    vi.mocked(post).mockResolvedValue({});
    const onStateChange = vi.fn();
    const app = await open({ ...heat, seen_state: 'seen' }, { ranking: ranking(4), props: { onStateChange } });
    try {
      const row = byTestId('rank-card-tier');
      expect(row.getAttribute('aria-label')).toBe('In your ranking: A, Liked it');
      expect(row.querySelector('.letter').textContent).toBe('A');
      expect(row.textContent).toContain('Liked it');
      expect(byTestId('rank-card-place').getAttribute('href')).toBe('/rank/place/6?kind=movie');
      expect(byTestId('rank-card-place').textContent).toContain('2 quick questions');

      serveShelves({ ...payload({ ...heat, seen_state: 'seen' }), ranking: ranking(4) });
      await openLadder();
      expect(vi.mocked(get)).toHaveBeenCalledWith('/rate/shelves?title_id=6');
      const dialog = target.querySelector('[role="dialog"][aria-label="In your ranking"]');
      expect(dialog.querySelector('h2').textContent).toBe('Heat');
      expect(dialog.querySelector('.footnote').textContent).toBe('Liked it');
      expect([...sheet().querySelectorAll('.word')].map((w) => w.textContent)).toEqual(TIERS.map((t) => t.word));
      expect(shelf(4).getAttribute('aria-current')).toBe('true');
      expect(shelf(4).getAttribute('aria-label')).toBe('Liked it, with Zodiac');
      expect(sheet().textContent, 'no letter on the shelves').not.toMatch(/\b(S|A\+|A|B|C|D|F)\b/);

      shelf(2).click();
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/rank/drop?kind=movie&per_tier=1', {
        title_id: 6, tier: 2, via: 'explicit'
      });
      expect(toast.message).toBe('Heat moved to C');
      expect(toast.actionLabel).toBe('Undo');
      expect(sheet(), 'a shelf tap closes the sheet').toBeNull();
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: C, Not really for me');
      expect(onStateChange, 'a rated, seen title moved touches Home not at all').not.toHaveBeenCalled();

      await toast.action();
      expect(vi.mocked(post)).toHaveBeenLastCalledWith('/rank/drop?kind=movie&per_tier=1', { title_id: 6, tier: 4 });
    } finally {
      unmount(app);
    }
  });

  it('draws the shelf frames and words before the shelves land', async () => {
    const app = await open({ ...heat, seen_state: 'seen' }, { ranking: ranking(4) });
    try {
      vi.mocked(get).mockImplementation((path) =>
        path.startsWith('/rate/shelves') ? new Promise(() => {}) : Promise.resolve(payload(heat))
      );
      await openLadder();
      const tiers = [...sheet().querySelectorAll('[data-testid="ladder-shelf"]')].map((s) => s.dataset.tier);
      expect(tiers).toEqual(['6', '5', '4', '3', '2', '1', '0']);
      expect(shelf(6).getAttribute('aria-label')).toBe('All-time favourite');
    } finally {
      unmount(app);
    }
  });

  it('names no letter for a title not on the board, and one tap places it as seen and rated', async () => {
    vi.mocked(post).mockResolvedValue({});
    const onStateChange = vi.fn();
    const app = await open(
      { ...heat, seen_state: 'unseen' },
      { ranking: ranking(null), why: 'Because you liked Drive', props: { onStateChange } }
    );
    try {
      const row = byTestId('rank-card-tier');
      expect(row.getAttribute('aria-label')).toBe('In your ranking: not placed yet');
      expect(row.querySelector('.letter'), "the model's guess reached the card").toBeNull();
      expect(byTestId('rank-card-place')).toBeNull();
      expect(byTestId('rank-card-tension')).toBeNull();

      // Read again after the drop: the board takes a first placement at its next refit.
      serveShelves({ ...payload({ ...heat, seen_state: 'seen' }), ranking: ranking(null) });
      await openLadder();
      const header = target.querySelector('[role="dialog"][aria-label="In your ranking"] .footnote');
      expect(header.textContent).toBe('Not placed yet');
      expect(sheet().querySelector('[aria-current]')).toBeNull();
      shelf(4).click();
      await settle();

      expect(vi.mocked(post)).toHaveBeenCalledWith('/rank/drop?kind=movie&per_tier=1', {
        title_id: 6, tier: 4, via: 'explicit'
      });
      expect(toast.message).toBe('Heat placed in A');
      expect(toast.actionLabel, 'a first placement has no tier to go back to').toBe('');
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: A, Liked it');
      expect(byTestId('title-watched').textContent.trim()).toBe('Watched');
      expect(byTestId('title-why')).toBeNull();
      expect(onStateChange).toHaveBeenCalledWith(6, 'seen');
    } finally {
      unmount(app);
    }
  });

  it('places with the keys 1 to 7 from the top, and closes with Done or Escape writing nothing', async () => {
    vi.mocked(post).mockResolvedValue({});
    const app = await open({ ...heat, seen_state: 'seen' }, { ranking: ranking(4) });
    const press = (key) => window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    try {
      serveShelves(payload({ ...heat, seen_state: 'seen' }));
      await openLadder();
      byTestId('ladder-done').click();
      await settle();
      expect(sheet()).toBeNull();
      await openLadder();
      press('Escape');
      await settle();
      expect(sheet()).toBeNull();
      expect(byTestId('title-watched'), 'Escape closed the ladder alone').not.toBeNull();
      expect(vi.mocked(post)).not.toHaveBeenCalled();

      press('7');
      await settle();
      expect(vi.mocked(post), 'no key places with the sheet closed').not.toHaveBeenCalled();
      await openLadder();
      press('7');
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/rank/drop?kind=movie&per_tier=1', {
        title_id: 6, tier: 0, via: 'explicit'
      });
      expect(sheet()).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('counts a placed title marked not seen as seen again', async () => {
    vi.mocked(post).mockResolvedValue({});
    const onStateChange = vi.fn();
    const app = await open({ ...heat, seen_state: 'unseen' }, { ranking: ranking(3), props: { onStateChange } });
    try {
      await openLadder();
      shelf(4).click();
      await settle();
      expect(byTestId('title-watched').textContent.trim()).toBe('Watched');
      expect(onStateChange).toHaveBeenCalledWith(6, 'seen');
    } finally {
      unmount(app);
    }
  });

  it("hands the move to Rank's own when Rank opened the card", async () => {
    const onMove = vi.fn().mockResolvedValue(true);
    const tension = 'You put it in A — your other answers still point to C';
    const app = await open({ ...heat, seen_state: 'seen' }, { ranking: ranking(4, { tension }), props: { onMove } });
    try {
      expect(byTestId('rank-card-tension').textContent).toBe(tension);
      await openLadder();
      shelf(2).click();
      await settle();
      expect(onMove).toHaveBeenCalledWith(
        { title_id: 6, name: 'Heat', kind: 'movie', tier: 4 },
        expect.objectContaining({ index: 2, label: 'C', word: 'Not really for me' })
      );
      expect(vi.mocked(post)).not.toHaveBeenCalled();
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: C, Not really for me');
      expect(byTestId('rank-card-tension'), 'the line described the old placement').toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('says a custom label once, as its own word', async () => {
    const custom = ['good', 'ok', 'bad'].map((label, i) => ({ index: 2 - i, label, word: label, count: 2, entries: [] }));
    const app = await open(
      { ...heat, seen_state: 'seen' },
      { ranking: { set_up: true, tier: 2, tension: null, tiers: custom } }
    );
    try {
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: good');
      expect(byTestId('rank-card-tier').querySelectorAll('.footnote')).toHaveLength(0);
    } finally {
      unmount(app);
    }
  });

  it('reads "Set up your ladder first" before the set-up, and goes to Rate', async () => {
    const app = await open({ ...heat, seen_state: 'seen' }, { ranking: ranking(4, { set_up: false }) });
    try {
      const row = byTestId('rank-card-tier');
      expect(row.getAttribute('aria-label')).toBe('In your ranking: set up your ladder first');
      expect(row.textContent).toContain('Set up your ladder first');
      expect(row.querySelector('.letter'), 'no letter before the set-up').toBeNull();
      expect(byTestId('rank-card-place')).toBeNull();

      row.click();
      await settle();
      expect(sheet()).toBeNull();
      expect(target.querySelector('[role="dialog"][aria-label="Title detail"]'), 'the card closes first').toBeNull();
      expect(vi.mocked(goto)).toHaveBeenCalledWith('/rate');
    } finally {
      unmount(app);
    }
  });

  it('draws no row when the payload carries no ranking', async () => {
    const app = await open(heat, { ranking: null });
    try {
      expect(byTestId('rank-card-tier')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('Shares a lot with (decisions 541 and 550)', () => {
  const share = (title_id, name, seen, term) => ({
    title_id, kind: 'movie', name, original_name: null, original_language: null, year: 2004,
    runtime_min: 120, poster_path: null, seen, term
  });
  const SHARES = [
    share(11, 'Collateral', true, { term: 'place.los_angeles', facet: 'place', label: 'Los Angeles' }),
    share(12, "Ocean's Eleven", false, { term: 'themes.heist', facet: 'themes', label: 'Heist' }),
    share(13, 'Thief', false, { term: 'mood.tense', facet: 'mood', label: 'tense' })
  ];
  const credits = [{ person_id: 1, name: 'Michael Mann', job: 'Director', role_class: 'director' }];
  const dialogs = () => [...target.querySelectorAll('[role="dialog"][aria-label="Title detail"]')];
  const sheets = () => read(nav.page).state.sheets ?? [];

  /** The outer card is title 6; every other read is the Shares title asked for. */
  function serve(outer) {
    vi.mocked(get).mockImplementation((path) =>
      Promise.resolve(
        path === '/titles/6'
          ? outer
          : { ...payload({ id: Number(path.split('/').pop()), name: 'Collateral', kind: 'movie' }), credits }
      )
    );
  }

  it('is absent when the payload names none', async () => {
    const app = await open({ kind: 'movie' });
    try {
      expect(byTestId('title-shares')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('shows each title with its seen mark and shared term, between the cast and the fold', async () => {
    const app = await open({ kind: 'movie' }, { shares: SHARES, credits });
    try {
      const row = byTestId('title-shares');
      expect(row.querySelector('h3').textContent).toBe('Shares a lot with');
      expect(row.textContent).toContain('From your library, closest first');
      const items = [...row.querySelectorAll('[data-testid="title-share"]')];
      expect(items.map((i) => i.querySelector('.sharename').textContent)).toEqual([
        'Collateral', "Ocean's Eleven", 'Thief'
      ]);
      expect(items.map((i) => !!i.querySelector('[aria-label="Seen"]'))).toEqual([true, false, false]);
      expect(items[0].querySelector('.shareterm').textContent.trim()).toBe('Los Angeles');
      expect(items[0].querySelector('.dot').style.background).toBe('var(--facet-place)');
      expect(items[2].querySelector('.shareterm').textContent.trim()).toBe('tense');
      const cast = target.querySelector('.cast');
      const more = byTestId('title-more');
      expect(cast.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(row.compareDocumentPosition(more) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    } finally {
      unmount(app);
    }
  });

  it('opens a tapped title as a card on top, and Back walks back to this one', async () => {
    serve({ ...payload({ kind: 'movie' }), shares: SHARES });
    const onClose = vi.fn();
    const app = mount(TitleDetail, {
      target,
      props: { titleId: 6, onClose, onPerson: () => {}, onStateChange: () => {} }
    });
    try {
      await settle();
      byTestId('title-share').click();
      await settle();
      expect(vi.mocked(get)).toHaveBeenLastCalledWith('/titles/11');
      expect(dialogs()).toHaveLength(2);
      expect(sheets()).toHaveLength(2);
      expect(dialogs()[1].querySelector('.title-1').textContent).toBe('Collateral');

      history.back();
      await settle();
      expect(dialogs()).toHaveLength(1);
      expect(sheets()).toHaveLength(1);
      expect(onClose).not.toHaveBeenCalled();
      expect(dialogs()[0].querySelector('.title-1').textContent).toBe('Severance');
    } finally {
      unmount(app);
    }
  });

  it('closes the whole stack before a person on the nested card filters the library', async () => {
    serve({ ...payload({ kind: 'movie' }), shares: SHARES });
    const calls = [];
    const app = mount(TitleDetail, {
      target,
      props: {
        titleId: 6,
        onClose: () => calls.push(['close', sheets().length]),
        onPerson: (c) => calls.push(['person', c.name, sheets().length]),
        onStateChange: () => {}
      }
    });
    try {
      await settle();
      byTestId('title-share').click();
      await settle();
      dialogs()[1].querySelector('.person').click();
      await settle();
      expect(calls).toEqual([['close', 0], ['person', 'Michael Mann', 0]]);
      expect(dialogs()).toHaveLength(0);
    } finally {
      unmount(app);
    }
  });
});

describe('a title the household does not have (decision 544)', () => {
  const tiers = ['S', 'A+', 'A', 'B', 'C', 'D', 'F'].map((label, i) => ({
    index: 6 - i, label, word: label, count: 0, entries: []
  }));
  const unowned = { kind: 'movie', name: 'Prisoners', is_owned: false, seen_state: 'unseen' };
  const opened = (extra = {}) =>
    open(unowned, {
      actions: { play_on_jellyfin: null, play_reason: 'not_in_library' },
      ranking: { set_up: true, tier: null, tension: null, tiers },
      ...extra
    });

  it("puts the panel where Play stands, and keeps the ranking row and the pair", async () => {
    const app = await opened();
    try {
      const panel = byTestId('title-unowned');
      expect(panel.textContent).toContain('Not in the library');
      expect(target.querySelector('.play'), 'Play beside the panel').toBeNull();
      expect(byTestId('title-want').textContent.trim()).toBe('Want it');
      expect(byTestId('title-want').getAttribute('aria-pressed')).toBe('false');
      expect(byTestId('title-want').classList.contains('btn-primary')).toBe(true);
      expect(byTestId('title-not-for-me').getAttribute('aria-pressed')).toBe('false');
      expect(byTestId('rank-card-tier')).not.toBeNull();
      expect(byTestId('title-not-seen')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('says who else would likely enjoy it, and nothing when nobody would', async () => {
    vi.mocked(api).mockResolvedValue({ state: 'want' });
    let app = await opened({ wish: { state: null, likely_too: ['Jenny'] } });
    try {
      expect(byTestId('title-likely-too').textContent).toBe('Jenny would likely enjoy it too.');
      byTestId('title-want').click();
      await settle();
      expect(byTestId('title-likely-too').textContent, 'a want keeps the line').toBe(
        'Jenny would likely enjoy it too.'
      );
    } finally {
      unmount(app);
    }
    app = await opened({ wish: { state: null, likely_too: [] } });
    try {
      expect(byTestId('title-likely-too')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('Want it puts the title on the wish list and a second tap takes it off', async () => {
    vi.mocked(api).mockResolvedValueOnce({ state: 'want' }).mockResolvedValueOnce({ state: null });
    const app = await opened();
    const epoch = wishes.epoch;
    try {
      byTestId('title-want').click();
      await settle();
      expect(vi.mocked(api)).toHaveBeenLastCalledWith('/wish/6', { method: 'PUT', body: { state: 'want' } });
      expect(wishes.epoch, 'Home and an open sheet re-read the list').toBe(epoch + 1);
      expect(byTestId('title-want').textContent.trim()).toBe('On the wish list');
      expect(byTestId('title-want').getAttribute('aria-pressed')).toBe('true');
      byTestId('title-want').click();
      await settle();
      expect(vi.mocked(api)).toHaveBeenLastCalledWith('/wish/6', { method: 'DELETE' });
      expect(byTestId('title-want').textContent.trim()).toBe('Want it');
    } finally {
      unmount(app);
    }
  });

  it('Not for me is a toggle that starts from the standing state', async () => {
    vi.mocked(api).mockResolvedValue({ state: null });
    const app = await opened({ wish: { state: 'not_for_me' } });
    try {
      expect(byTestId('title-not-for-me').getAttribute('aria-pressed')).toBe('true');
      byTestId('title-not-for-me').click();
      await settle();
      expect(vi.mocked(api)).toHaveBeenCalledWith('/wish/6', { method: 'DELETE' });
      expect(byTestId('title-not-for-me').getAttribute('aria-pressed')).toBe('false');
      vi.mocked(api).mockResolvedValue({ state: 'not_for_me' });
      byTestId('title-not-for-me').click();
      await settle();
      expect(vi.mocked(api)).toHaveBeenLastCalledWith('/wish/6', {
        method: 'PUT', body: { state: 'not_for_me' }
      });
      expect(byTestId('title-not-for-me').getAttribute('aria-pressed')).toBe('true');
    } finally {
      unmount(app);
    }
  });

  it('Seen it, rate it opens the ladder, or goes to Rate before the set-up', async () => {
    let app = await opened();
    try {
      byTestId('title-seen-rate').click();
      await settle();
      expect(byTestId('ladder-sheet')).not.toBeNull();
      expect(target.querySelectorAll('[data-testid="ladder-shelf"]')).toHaveLength(7);
    } finally {
      unmount(app);
    }
    app = await opened({ ranking: { set_up: false, tier: null, tension: null, tiers } });
    try {
      byTestId('title-seen-rate').click();
      await settle();
      expect(byTestId('ladder-sheet')).toBeNull();
      expect(vi.mocked(goto)).toHaveBeenCalledWith('/rate');
    } finally {
      unmount(app);
    }
  });

  it('says so when the wish list cannot be reached', async () => {
    vi.mocked(api).mockRejectedValue(new Error('the server is busy'));
    const app = await opened();
    try {
      byTestId('title-want').click();
      await settle();
      expect(byTestId('title-unowned').querySelector('[role="status"]').textContent).toBe(
        'Could not save that — the server is busy'
      );
      expect(byTestId('title-want').textContent.trim()).toBe('Want it');
    } finally {
      unmount(app);
    }
  });

  it('is not on a card the library holds', async () => {
    const app = await open({ kind: 'movie', is_owned: true });
    try {
      expect(byTestId('title-unowned')).toBeNull();
      expect(target.querySelector('.play')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('a credit and a term lead to Home (decision 557)', () => {
  const sheets = () => read(nav.page).state.sheets ?? [];
  const dialogs = () => [...target.querySelectorAll('[role="dialog"][aria-label="Title detail"]')];
  // One human folded across two person rows, as `fold_credits` sends it.
  const mann = { person_id: 1, person_ids: [1, 9], name: 'Michael Mann', job: 'Director', role_class: 'director' };
  const quoted = (term, label) => ({
    term, facet: term.split('.')[0], label, provider: '', evidence: [{ quote: `${label}.`, source: 'trakt:1' }]
  });
  const DNA = {
    extracted: [quoted('themes.obsession', 'obsession'), quoted('place.los_angeles', 'Los Angeles')],
    projected: [{ term: 'era.period', facet: 'era', label: 'period piece', weight: 3 }]
  };
  const SHARES = [11, 12, 13].map((title_id) => ({
    title_id, kind: 'movie', name: `Share ${title_id}`, original_name: null, original_language: null,
    year: 2004, runtime_min: 120, poster_path: null, seen: false,
    term: { term: 'themes.heist', facet: 'themes', label: 'heist' }
  }));
  // A card opened anywhere but Home: no handler of Home's, and the surface that reopens it.
  const ELSEWHERE = { onPerson: undefined, from: 'rank' };

  /** Each close and each jump, with the sheets still open at that moment. */
  function record(calls) {
    vi.mocked(jumpHome).mockImplementation(async (href, card) => {
      calls.push(['jump', href, card, sheets().length]);
    });
    return () => calls.push(['close', sheets().length]);
  }

  it('hands a credit to Home once the card has closed, and jumps from anywhere else', async () => {
    const onPerson = vi.fn();
    let app = await open({ kind: 'movie' }, { credits: [mann], props: { onPerson } });
    try {
      target.querySelector('.person').click();
      await settle();
      expect(onPerson).toHaveBeenCalledWith(mann);
      expect(jumpHome).not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }

    const calls = [];
    app = await open({ kind: 'movie' }, { credits: [mann], props: { ...ELSEWHERE, onClose: record(calls) } });
    try {
      target.querySelector('.person').click();
      await settle();
      // A person filter lists both kinds, and the folded person is one chip.
      expect(calls).toEqual([
        ['close', 0],
        ['jump', '/?person=1,9&kind=movie&kind=series', { titleId: 6, from: 'rank' }, 0]
      ]);
    } finally {
      unmount(app);
    }
  });

  it('closes the whole stack before a nested card jumps, and names the card it was tapped on', async () => {
    vi.mocked(get).mockImplementation((path) =>
      Promise.resolve(
        path === '/titles/6'
          ? { ...payload({ kind: 'movie' }), shares: SHARES }
          : { ...payload({ id: 11, name: 'Share 11', kind: 'movie' }), credits: [mann] }
      )
    );
    const calls = [];
    const app = mount(TitleDetail, {
      target,
      props: { titleId: 6, from: 'taste', onClose: record(calls), onStateChange: () => {} }
    });
    try {
      await settle();
      byTestId('title-share').click();
      await settle();
      dialogs()[1].querySelector('.person').click();
      await settle();
      expect(dialogs()).toHaveLength(0);
      expect(calls).toEqual([
        ['close', 0],
        ['jump', '/?person=1,9&kind=movie&kind=series', { titleId: 11, from: 'taste' }, 0]
      ]);
    } finally {
      unmount(app);
    }
  });

  it("offers Filter Home by this on the quoted term whose quotes are shown", async () => {
    const onTerm = vi.fn();
    const onClose = vi.fn();
    let app = await open({ kind: 'movie' }, { dna: DNA, props: { onTerm, onClose } });
    try {
      const filter = byTestId('title-term-filter');
      expect(byTestId('title-evidence').contains(filter)).toBe(true);
      expect(filter.textContent.trim()).toBe('Filter Home by this');
      // Picking another term still shows its quotes; the action follows the pick.
      target.querySelectorAll('.tag')[1].click();
      flushSync();
      byTestId('title-term-filter').click();
      await settle();
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(onTerm).toHaveBeenCalledWith({ term: 'place.los_angeles', label: 'Los Angeles', facet: 'place' });
      expect(jumpHome).not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }

    app = await open({ kind: 'movie' }, { dna: DNA, props: ELSEWHERE });
    try {
      byTestId('title-term-filter').click();
      await settle();
      expect(jumpHome).toHaveBeenCalledWith('/?term=themes.obsession&kind=movie', { titleId: 6, from: 'rank' });
    } finally {
      unmount(app);
    }
  });

  it('makes an our-read chip a button that filters by its term at once', async () => {
    const onTerm = vi.fn();
    const app = await open({ kind: 'movie' }, { dna: DNA, props: { onTerm } });
    try {
      const chip = target.querySelector('.chip.ourread');
      expect(chip.tagName).toBe('BUTTON');
      chip.click();
      await settle();
      expect(onTerm).toHaveBeenCalledWith({ term: 'era.period', label: 'period piece', facet: 'era' });
      expect(dialogs()).toHaveLength(0);
    } finally {
      unmount(app);
    }
  });
});
