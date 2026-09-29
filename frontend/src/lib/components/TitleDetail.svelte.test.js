/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { get as read } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));

// The sheet is a history entry: `pushState` adds it and Back (here `history.back`) takes it away.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import { get, post } from '$lib/api.js';
import { hideToast, toast } from '$lib/toast.svelte.js';
import TitleDetail from './TitleDetail.svelte';

const NOTE = '[data-testid="title-series-unseen-note"]';
const SYNCNOTE = '.syncnote';
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
  my_verdict: null,
  actions: { play_on_jellyfin: null, play_reason: 'no_server', show_on_map: null }
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
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

const tapSeen = async () => {
  const button = target.querySelector('button.seen');
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
    vi.mocked(post).mockResolvedValue({
      state: 'unseen',
      synced: true,
      reason: 'series unseen is app-only'
    });
    const app = await open();
    try {
      await tapSeen();
      const note = target.querySelector(SYNCNOTE).textContent;
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
      await tapSeen();
      expect(target.querySelector(SYNCNOTE).textContent).toContain('Jellyfin is up to date');
    } finally {
      unmount(app);
    }
  });

  it('sits under the actions and outside the credits, as a quiet line', async () => {
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: false, reason: 'not on Jellyfin' });
    const app = await open(
      { kind: 'movie', seen_state: 'unseen' },
      { credits: [{ person_id: 1, name: 'Michael Mann', job: 'Director', sources: ['tmdb'] }] }
    );
    try {
      await tapSeen();
      const note = target.querySelector(SYNCNOTE);
      expect(note.textContent).toContain("isn't in your Jellyfin library");
      expect(note.closest('section'), 'the note is inside a section').toBeNull();
      expect(note.closest('.actions'), 'the note is inside the button row').toBeNull();
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
      await tapSeen();
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
      expect(target.querySelector('[data-answer]'), 'answers before the read').toBeNull();

      land(payload({ kind: 'movie', name: 'Heat', year: 1995, runtime_min: 170, seen_state: 'unseen' }));
      await settle();
      expect(target.querySelector('.title-1').textContent).toBe('Heat');
      expect(target.querySelector('[data-answer="liked"]')).not.toBeNull();
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

  it('links the trailer by what it is and never prints its key', async () => {
    const app = await open({ trailer_key: 'F-eMt3SrfFU' });
    try {
      const link = target.querySelector('a.trailer');
      expect(link.textContent.trim()).toBe('Trailer');
      expect(link.getAttribute('aria-label')).toBe('Watch the trailer on YouTube');
      expect(link.getAttribute('href')).toBe('https://www.youtube.com/watch?v=F-eMt3SrfFU');
      expect(link.textContent).not.toContain('F-eMt3SrfFU');
    } finally {
      unmount(app);
    }
  });
});

describe("§6.0's Show on map waits for the Map (decision 488)", () => {
  it('is absent while the payload carries no target', async () => {
    const app = await open();
    try {
      expect(target.textContent).not.toContain('Show on map');
    } finally {
      unmount(app);
    }
  });

  it('links to the map on the day the server sends one', async () => {
    const app = await open({}, { actions: { play_on_jellyfin: null, show_on_map: { title_id: 6 } } });
    try {
      const link = [...target.querySelectorAll('a')].find((a) =>
        a.textContent.includes('Show on map')
      );
      expect(link?.getAttribute('href')).toBe('/map?title=6');
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
    // It comes before the answers, Play and the synopsis.
    const rate = target.querySelector('[data-testid="title-rate"]');
    expect(why.compareDocumentPosition(rate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    unmount(app);
    for (const absent of [{}, { why: null }, { why: '  ' }]) {
      app = await open({ kind: 'movie' }, absent);
      expect(target.querySelector('[data-testid="title-why"]')).toBeNull();
      unmount(app);
    }
  });

  it('puts the answers and Play above the synopsis', async () => {
    const app = await open({ kind: 'movie', overview: 'A thief and a cop.' });
    try {
      const overview = target.querySelector('.overview');
      for (const sel of ['[data-testid="title-rate"]', '.actions']) {
        const el = target.querySelector(sel);
        expect(el.compareDocumentPosition(overview) & Node.DOCUMENT_POSITION_FOLLOWING, sel).toBeTruthy();
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

  it('reads the answers worst to best, as Rate does', async () => {
    const app = await open({ kind: 'movie' });
    try {
      const order = [...target.querySelectorAll('[data-answer]')].map((b) => b.dataset.answer);
      expect(order).toEqual(['disliked', 'fine', 'liked', 'not_seen']);
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

describe("the card's own answer (decision 487)", () => {
  it('writes through the Rate session and says what it saved and what we guessed', async () => {
    vi.mocked(post).mockResolvedValue({
      reveal: { available: true, agreed: true, predicted_label: 'liked', cdf: 0.71, text: 'x' }
    });
    const onStateChange = vi.fn();
    const app = await open({ kind: 'movie', seen_state: 'unseen' }, { props: { onStateChange } });
    try {
      const liked = target.querySelector('[data-answer="liked"]');
      expect(liked.getAttribute('aria-pressed')).toBe('false');
      liked.click();
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/rate/title/6', { answer: 'liked' });
      const note = target.querySelector('[data-testid="title-rate-note"]').textContent;
      expect(note).toContain('you liked it');
      expect(note).toContain("We'd have guessed the same.");
      expect(note.replace(/\s+/g, ' ').trim()).toBe("Saved — you liked it. We'd have guessed the same.");
      expect(note, "the reveal's number is Show the model's").not.toContain('0.71');
      expect(target.querySelector('[data-answer="liked"]').getAttribute('aria-pressed')).toBe(
        'true'
      );
      expect(target.querySelector('button.seen').textContent.trim()).toBe('Watched');
      expect(onStateChange).toHaveBeenCalledWith(6, 'seen');
    } finally {
      unmount(app);
    }
  });

  it('stops saying why a title is suggested once the member has rated it or marked it seen', async () => {
    // The server omits the line on the next open; the open card must drop it too.
    const why = { why: 'Because you liked Heat' };
    const line = () => target.querySelector('[data-testid="title-why"]');
    vi.mocked(post).mockResolvedValue({ reveal: null });
    let app = await open({ kind: 'movie', seen_state: 'unseen' }, why);
    try {
      target.querySelector('[data-answer="not_seen"]').click();
      await settle();
      expect(line(), 'Not seen leaves an unseen title unseen').not.toBeNull();
    } finally {
      unmount(app);
    }
    app = await open({ kind: 'movie', seen_state: 'unseen' }, why);
    try {
      expect(line()).not.toBeNull();
      target.querySelector('[data-answer="liked"]').click();
      await settle();
      expect(line(), 'rated from the card').toBeNull();
    } finally {
      unmount(app);
    }
    vi.mocked(post).mockResolvedValue({ synced: true });
    app = await open({ kind: 'movie', seen_state: 'unseen' }, why);
    try {
      await tapSeen();
      expect(line(), 'marked seen from the card').toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('writes nothing for a tap on the answer that already stands', async () => {
    vi.mocked(post).mockResolvedValue({ reveal: null });
    const rated = await open(
      { kind: 'movie', seen_state: 'seen' },
      { my_verdict: { value: 2, label: 'liked' } }
    );
    try {
      target.querySelector('[data-answer="liked"]').click();
      await settle();
      expect(target.querySelector('[data-answer="liked"]').hasAttribute('data-flick')).toBe(true);
      expect(vi.mocked(post)).not.toHaveBeenCalled();
      expect(target.querySelector('[data-answer="liked"]').getAttribute('aria-pressed')).toBe(
        'true'
      );
      // A change of mind still writes.
      target.querySelector('[data-answer="fine"]').click();
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/rate/title/6', { answer: 'fine' });
    } finally {
      unmount(rated);
    }
    vi.mocked(post).mockClear();
    const unseen = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      target.querySelector('[data-answer="not_seen"]').click();
      await settle();
      expect(vi.mocked(post)).not.toHaveBeenCalled();
    } finally {
      unmount(unseen);
    }
  });

  it('shows the standing verdict pressed, and Not seen flips the state and keeps it', async () => {
    vi.mocked(post).mockResolvedValue({ reveal: null });
    const app = await open(
      { kind: 'movie', seen_state: 'seen' },
      { my_verdict: { value: 1, label: 'fine' } }
    );
    try {
      expect(target.querySelector('[data-answer="fine"]').getAttribute('aria-pressed')).toBe(
        'true'
      );
      target.querySelector('[data-answer="not_seen"]').click();
      await settle();
      expect(vi.mocked(post)).toHaveBeenCalledWith('/rate/title/6', { answer: 'not_seen' });
      expect(target.querySelector('button.seen').textContent.trim()).toBe('Mark as watched');
      // The verdict survives the flip (§4.2) but is not what the person just said.
      expect(target.querySelector('[aria-pressed="true"][data-answer]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('the ranking rows on every card (decision 531)', () => {
  // `api/library.py`'s `ranking`: the person's tier set, best first, with each tier's count.
  const TIERS = [
    ['S', 'Liked', 1],
    ['A+', 'Liked', 0],
    ['A', 'Liked', 3],
    ['B', 'Fine', 2],
    ['C', 'Disliked', 0],
    ['D', 'Disliked', 0],
    ['F', 'Disliked', 0]
  ].map(([label, verdict, count], i) => ({ index: 6 - i, label, verdict, count, entries: [] }));
  const ranking = (tier, tension = null) => ({ tier, tension, tiers: TIERS });
  const heat = { kind: 'movie', name: 'Heat' };

  const byTestId = (testid) => target.querySelector(`[data-testid="${testid}"]`);
  const sheet = (name) => target.querySelector(`[role="dialog"][aria-label="${name}"]`);
  const options = (name) => [...sheet(name).querySelectorAll('[role="menuitem"]')];
  async function choose(name, label) {
    byTestId('rank-card-tier').click();
    await settle();
    options(name).find((o) => o.querySelector('.label').textContent === label).click();
    await settle();
  }

  beforeEach(() => hideToast());

  it('names the tier on a card opened from Home, moves it there, and undoes the move', async () => {
    vi.mocked(post).mockResolvedValue({});
    const onStateChange = vi.fn();
    const app = await open(
      { ...heat, seen_state: 'seen' },
      { my_verdict: { value: 2, label: 'liked' }, ranking: ranking(4), props: { onStateChange } }
    );
    try {
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: A, Liked');
      expect(byTestId('rank-card-place').getAttribute('href')).toBe('/rank/place/6?kind=movie');
      expect(byTestId('rank-card-place').textContent).toContain('2 quick questions');

      byTestId('rank-card-tier').click();
      await settle();
      const shown = options('Move Heat');
      expect(shown.map((o) => o.querySelector('.label').textContent)).toEqual(['S', 'A+', 'A', 'B', 'C', 'D', 'F']);
      expect(shown[0].textContent).toContain('Liked');
      expect(shown[2].getAttribute('aria-current')).toBe('true');
      shown[4].click();
      await settle();

      expect(vi.mocked(post)).toHaveBeenCalledWith('/rank/drop?kind=movie&per_tier=1', { title_id: 6, tier: 2, via: 'explicit' });
      expect(toast.message).toBe('Heat moved to C');
      expect(toast.actionLabel).toBe('Undo');
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: C, Disliked');
      expect(onStateChange, 'a rated, seen title moved touches Home not at all').not.toHaveBeenCalled();

      await toast.action();
      expect(vi.mocked(post)).toHaveBeenLastCalledWith('/rank/drop?kind=movie&per_tier=1', { title_id: 6, tier: 4 });
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

      row.click();
      await settle();
      expect(options('Rank Heat').some((o) => o.hasAttribute('aria-current'))).toBe(false);
      options('Rank Heat')[2].click();
      await settle();

      expect(vi.mocked(post)).toHaveBeenCalledWith('/rank/drop?kind=movie&per_tier=1', { title_id: 6, tier: 4, via: 'explicit' });
      expect(toast.message).toBe('Heat placed in A');
      expect(toast.actionLabel, 'a first placement has no tier to go back to').toBe('');
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: A, Liked');
      expect(target.querySelector('[data-answer="liked"]').getAttribute('aria-pressed')).toBe('true');
      expect(target.querySelector('button.seen').textContent.trim()).toBe('Watched');
      expect(byTestId('title-why')).toBeNull();
      expect(onStateChange).toHaveBeenCalledWith(6, 'seen');
    } finally {
      unmount(app);
    }
  });

  it('counts a rated title marked not seen as seen again, and keeps its verdict', async () => {
    vi.mocked(post).mockResolvedValue({});
    const onStateChange = vi.fn();
    const app = await open(
      { ...heat, seen_state: 'unseen' },
      { my_verdict: { value: 1, label: 'fine' }, ranking: ranking(3), props: { onStateChange } }
    );
    try {
      await choose('Move Heat', 'A');
      expect(target.querySelector('[data-answer="fine"]').getAttribute('aria-pressed')).toBe('true');
      expect(target.querySelector('button.seen').textContent.trim()).toBe('Watched');
      expect(onStateChange).toHaveBeenCalledWith(6, 'seen');
    } finally {
      unmount(app);
    }
  });

  it("hands the move to Rank's own when Rank opened the card", async () => {
    const onMove = vi.fn().mockResolvedValue(true);
    const app = await open(
      { ...heat, seen_state: 'seen' },
      {
        my_verdict: { value: 2, label: 'liked' },
        ranking: ranking(4, 'You put it in A — your other answers still point to C'),
        props: { onMove }
      }
    );
    try {
      expect(byTestId('rank-card-tension').textContent).toBe('You put it in A — your other answers still point to C');
      await choose('Move Heat', 'C');
      expect(onMove).toHaveBeenCalledWith(
        { title_id: 6, name: 'Heat', kind: 'movie', tier: 4 },
        expect.objectContaining({ index: 2, label: 'C' })
      );
      expect(vi.mocked(post)).not.toHaveBeenCalled();
      expect(byTestId('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: C, Disliked');
      expect(byTestId('rank-card-tension'), 'the line described the old placement').toBeNull();
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
