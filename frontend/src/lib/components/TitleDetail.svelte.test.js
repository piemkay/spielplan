/**
 * @vitest-environment jsdom
 *
 * What the seen control says about a write that was deliberately never sent. Spec v2.1 §7.3, §6.8;
 * decision 210(a); M4.11.
 *
 * Jellyfin stores no Played flag on a Series — `Folder.FillUserDataDtoValues` computes the folder's
 * from its episodes — so the only way to un-mark one is a recursive DELETE across every episode,
 * which would destroy watch history the app never recorded and cannot put back. Decision 210(a)
 * settles it: `kind='series'` with `seen=False` writes the app side, stamps `jf_synced_at` so the
 * sweep does not re-owe the row, sends nothing, and returns the pair `(True, 'series unseen is
 * app-only')`. "And the surface says so" is the other half of that decision, and this card was
 * printing the opposite: it read `synced` alone, so a reason that arrived WITH a success rendered as
 * "synced to Jellyfin" — a sentence about a request that was never made.
 *
 * MOUNTED because both assertions are about one line of copy against one payload, which is cheaper
 * and more exact than reaching a series title through a stateful browser suite; `08-jellyfin.spec.js`
 * keeps the movie path end to end, where the same line must still read "synced to Jellyfin".
 *
 * M4.15 adds two more claims about this card, both of them §6 preamble's: that §6.0's second action
 * explains itself on a screen with no hover, and that the panel — which IS the screen on a phone —
 * can be dismissed by something other than the one control in its corner. The browser half of the
 * second is `19-phone-shell.spec.js`'s and is what the coverage row names; what is asserted here is
 * the wiring, because a `use:` action that is imported and never applied looks identical to one that
 * works until somebody taps outside. [proposals 127, 131; §6.8]
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
  // No `model_line`: the server leaves it out while Show the model is off (decision 486), and
  // that is the default every case below starts from.
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

/**
 * Mount the card over one payload. `over` patches the title; `extra` patches the rest of the body
 * (`actions`, say) and, under `props`, the props the shell passes in.
 */
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

/** A pointerdown as an engine sends it. jsdom ships no `PointerEvent`; the action reads none of it. */
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
    // The negative control, twice over: the line is about the un-marking direction of a series, and
    // §6.8's register is a quiet line where the consequence is — not a standing disclaimer.
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
      // The reason, in the member register (decision 486) rather than the rail's words.
      expect(note).toContain('Jellyfin keeps its own episode history');
      expect(note).not.toContain('up to date');
    } finally {
      unmount(app);
    }
  });

  it('still says so when the push really did land', async () => {
    // The negative control `08-jellyfin.spec.js` asserts on the movie path: a successful push with
    // nothing to explain must say Jellyfin was told.
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
    // It sat after the series note in the mono data voice at -4 px and read as part of CAST &
    // CREW (user test 2026-09-25).
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
    // The reason used to live in `title="link a Jellyfin server in Admin (M1)"`, and a `title`
    // attribute is a hover tooltip: on §6 preamble's primary form factor it does not exist, so
    // §6.0's second action was a dead button with no explanation reachable anywhere on the device.
    const app = await open({}, { actions: { play_on_jellyfin: null, play_reason: 'no_server' } });
    try {
      const why = target.querySelector(JELLYFIN_WHY);
      expect(why, 'the disabled action carries no reason on the screen').not.toBeNull();
      expect(why.textContent).toContain("Jellyfin isn't connected");
      expect(why.textContent, 'a milestone label in member copy (decision 486)').not.toMatch(
        /\bM\d\b/
      );
      // §6.8's register, not a new one: `.why` is the class design.css sets in the display face.
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
    // The negative control. §6.8's register is a line where the consequence is, not a standing
    // disclaimer under an action that works.
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
    // Every control on this card is inside the panel — the seen toggle, the credits, both
    // actions — so a dismissal that fired on them would make the card unusable rather than
    // dismissible.
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
    // The panel is mounted behind `{#if selected}` and unmounted on every close, so a listener
    // left on `document` is one per title the household has ever opened.
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
    // C9.6 and C9.5. §4.1 rule 2 makes a projected weight a weight and never a filter, so the
    // single-source chip is on the card, only drawn fainter than the four-source one.
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
      const chips = [...target.querySelectorAll('.chip')];
      expect(chips).toHaveLength(2);
      expect(chips.map((c) => c.classList.contains('faint'))).toEqual([true, false]);
    } finally {
      unmount(app);
    }
  });

  it('keys the credit list by person and class, so one person in two classes is two rows', async () => {
    // C9.3: `credits_for` folds per (person, role class) and the card's key follows it.
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

// --- the 2026-09-25 user test: the member register (decision 486), the card's own answer
// (decision 487) and an unbuilt Map (decision 488) ------------------------------------------

describe('Play says which of its two reasons it is', () => {
  it('names a title outside the library as that, not as a missing server', async () => {
    // Live: every unowned title read "Play needs a linked Jellyfin server - an admin links one in
    // Admin (M1)" on an install whose server was linked.
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
    // Decision 483's same-origin art arrives through RatePoster; the card passes the title with
    // its `id`, which is what the art route is keyed on.
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
    // It printed `TRAILER F-eMt3SrfFU` (user test 2026-09-25).
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
    // Heat's mood.gritty quote read as a negation because nothing said it was a fragment.
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

  it('shows how many sources suggest each inferred term, quieter at one, and hides none', async () => {
    const app = await open({}, { dna });
    try {
      const chips = [...target.querySelectorAll('.chips .chip')];
      expect(chips, '§4.1 rule 2: a weight is never a filter').toHaveLength(2);
      const [weak, strong] = chips;
      expect(weak.querySelector('.chiplabel').textContent.trim()).toBe('teen lead');
      expect(weak.classList.contains('faint')).toBe(true);
      expect(weak.querySelector('.n').textContent).toBe('1');
      expect(strong.classList.contains('faint')).toBe(false);
      expect(strong.querySelector('.n').textContent).toBe('4');
    } finally {
      unmount(app);
    }
  });
});

describe('credits', () => {
  it('print one row per person and role, with only the jobs that are different credits', async () => {
    // Heat: Goldenthal as "Original Music Composer" and "Composer", Mann as "Writer" and
    // "Screenplay" (user test 2026-09-25). A Novel credit is a different credit and stays.
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
      // Nothing is pressed on an unseen title: the verdict survives the flip (§4.2) but it is
      // not what the person just said.
      expect(target.querySelector('[aria-pressed="true"][data-answer]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});
