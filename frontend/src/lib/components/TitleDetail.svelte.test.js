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
  model_line: { available: false, reason: 'no bundle' },
  credits: [],
  platform_ratings: { items: [], note: 'display-only' },
  dna: { extracted: [], projected: [] },
  actions: { play_on_jellyfin: null, show_on_map: { title_id: 6 } }
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
      expect(target.querySelector(SYNCNOTE).textContent).toContain('series unseen is app-only');
    } finally {
      unmount(app);
    }
  });

  it('still says so when the push really did land', async () => {
    // The negative control `08-jellyfin.spec.js` asserts on the movie path: a successful push with
    // nothing to explain must keep reading "synced to Jellyfin".
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: true, reason: null });
    const app = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      await tapSeen();
      expect(target.querySelector(SYNCNOTE).textContent).toContain('synced to Jellyfin');
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
    const app = await open();
    try {
      const why = target.querySelector(JELLYFIN_WHY);
      expect(why, 'the disabled action carries no reason on the screen').not.toBeNull();
      expect(why.textContent).toContain('Jellyfin server');
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
      { actions: { play_on_jellyfin: 'http://jf.lan/web/#/details?id=1', show_on_map: { title_id: 6 } } }
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
