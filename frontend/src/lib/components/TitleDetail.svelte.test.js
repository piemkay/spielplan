/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
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
});

afterEach(() => {
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

// jsdom ships no PointerEvent, and the action reads none of it.
const tap = (/** @type {Element} */ el) =>
  el.dispatchEvent(new Event('pointerdown', { bubbles: true, cancelable: true }));

const press = (/** @type {string} */ key) =>
  document.body.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));

const tapSeen = async () => {
  const button = target.querySelector('button.seen');
  expect(button, 'the seen control is not on screen').not.toBeNull();
  button.click();
  await settle();
};

describe("decision 210(a)'s why-line", () => {
  it('warns before the tap that un-marking a series stays in this app', async () => {
    const app = await open();
    try {
      expect(target.querySelector(NOTE).textContent).toContain('kept in Spielplan only');
    } finally {
      unmount(app);
    }
  });

  it('is absent on a film, and absent on a series that is not seen yet', async () => {
    let app = await open({ kind: 'movie' });
    expect(target.querySelector(NOTE)).toBeNull();
    unmount(app);
    app = await open({ seen_state: 'unseen' });
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

  it('sits under the actions and outside the credits, in the display face', async () => {
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
      expect(note.classList.contains('why')).toBe(true);
      expect(note.classList.contains('data')).toBe(false);
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
      expect(why.classList.contains('why')).toBe(true);
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

describe('the panel dismisses', () => {
  it('closes when the pointer lands outside it', async () => {
    const onClose = vi.fn();
    const app = await open({}, { props: { onClose } });
    try {
      tap(document.body);
      expect(onClose, 'the panel has no outside-tap dismissal').toHaveBeenCalledTimes(1);
    } finally {
      unmount(app);
    }
  });

  it('stays open when the pointer lands inside it', async () => {
    const onClose = vi.fn();
    const app = await open({}, { props: { onClose } });
    try {
      tap(target.querySelector('.panel'));
      tap(target.querySelector('button.seen'));
      expect(onClose, 'tapping the card closed it').not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }
  });

  it('closes on Escape', async () => {
    const onClose = vi.fn();
    const app = await open({}, { props: { onClose } });
    try {
      press('Escape');
      expect(onClose, 'Escape does not close the panel').toHaveBeenCalledTimes(1);
      press('m');
      expect(onClose, 'a key that is not Escape closed it').toHaveBeenCalledTimes(1);
    } finally {
      unmount(app);
    }
  });

  it('stops listening once it is gone', async () => {
    // Mounted per selection, so a leaked listener is one per title ever opened.
    const onClose = vi.fn();
    const app = await open({}, { props: { onClose } });
    unmount(app);
    tap(document.body);
    press('Escape');
    expect(onClose, 'the unmounted panel is still listening on document').not.toHaveBeenCalled();
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
          e_source: 'backbone',
          bundle: 'v1'
        }
      }
    );
    try {
      expect(target.querySelector('[data-testid="title-model-line"]').textContent).toContain(
        'b(t) 0.52'
      );
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

  it('names the kind in words, not by its column value', async () => {
    const app = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      const sub = target.querySelector('.sub').textContent;
      expect(sub).toContain('film');
      expect(sub).not.toContain('movie');
    } finally {
      unmount(app);
    }
  });

  it('links the trailer by what it is and never prints its key', async () => {
    const app = await open({ trailer_key: 'F-eMt3SrfFU' });
    try {
      const link = target.querySelector('a.trailer');
      expect(link.textContent.trim()).toBe('Watch the trailer');
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
      expect(fold.querySelector('summary').textContent.trim()).toBe('1 less certain');
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
      expect([...tags[0].querySelectorAll('.quote')].map((q) => q.textContent)).toEqual([
        '“…the cinematic master poet of nocturnal Los Angeles…”',
        '“…the nighttime LA…”'
      ]);
      const chips = [...target.querySelectorAll('.chip .chiplabel')].map((c) => c.textContent.trim());
      expect(chips).toEqual(['loneliness']);
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
      const rows = [...target.querySelectorAll('.person .data')].map((r) => r.textContent.trim());
      expect(rows).toEqual(['Original Music Composer', 'Writer', 'Director', 'Screenplay · Novel']);
      // The source count is the operator's provenance (decision 486).
      expect(target.textContent).not.toContain('2 sources');
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
      expect(note, "the reveal's number is Show the model's").not.toContain('0.71');
      expect(target.querySelector('[data-answer="liked"]').getAttribute('aria-pressed')).toBe(
        'true'
      );
      expect(target.querySelector('button.seen').textContent.trim()).toBe('Seen');
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
      expect(target.querySelector('button.seen').textContent.trim()).toBe('Mark seen');
      // The verdict survives the flip (§4.2) but is not what the person just said.
      expect(target.querySelector('[aria-pressed="true"][data-answer]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});
