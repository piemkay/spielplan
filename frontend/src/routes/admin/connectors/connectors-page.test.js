/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({
  api: vi.fn(),
  get: vi.fn(),
  post: vi.fn(),
  // `session.svelte.js` imports it, and the page reads `session.publicUrl` for the webhook path.
  ApiError: class extends Error {}
}));
vi.mock('$lib/jellyfin.js', () => ({ jellyfinDirectory: vi.fn() }));
// The page reads `session.publicUrl` for the webhook path; the wizard reads `session.setup`.
vi.mock('$lib/session.svelte.js', () => ({
  session: { setup: { required: false, steps: [] }, publicUrl: 'http://localhost:8080', user: null },
  bootstrap: vi.fn(),
  setUser: vi.fn()
}));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import { jellyfinDirectory } from '$lib/jellyfin.js';
import ConnectorsPage from './+page.svelte';
import SetupPage from '../../setup/+page.svelte';

const SYNC = '[data-sync]';
const FAILURE = '[data-sync-failure]';
const VERDICT = '[data-server-supported]';
const PROBE = '[data-probe]';

/** `GET /api/admin/connectors/jellyfin`, including §7.1's stored verdict. */
const cfg = (over = {}) => ({
  url: 'http://jellyfin.test',
  has_api_key: true,
  configured: true,
  library_ids: [],
  secrets_unreadable: false,
  server_version: '10.9.11',
  server_supported: true,
  ...over
});

/** `SyncReport.as_dict()`, every key, as `api/admin.py` returns it. */
const report = (over = {}) => ({
  pushed: 0,
  adopted: 0,
  unchanged: 87,
  needs_relink: [],
  owed_unreachable: 0,
  owed_no_token: 0,
  push_failed: 0,
  push_errors: [],
  wrote: [],
  unowned: 0,
  resolve: {},
  users: ['patrick', 'jenny'],
  completed: ['patrick', 'jenny'],
  failed_users: [],
  skipped_no_link: false,
  already_running: false,
  ...over
});

/** `GET /api/admin/connectors/jellyfin/libraries`, M5.2's envelope (decision 364). */
const libraries = (over = {}) => ({
  ok: true,
  libraries: [
    { id: 'lib-films', name: 'Films' },
    { id: 'lib-series', name: 'Series' }
  ],
  ...over
});

/** `GET /api/admin/llm`, every key `api/llm._read` answers, trimmed to one provider's detail. */
const llm = () => ({
  providers: ['anthropic', 'openai', 'gemini'].map((name) => ({
    name,
    configured: name === 'gemini',
    has_api_key: name === 'gemini',
    secrets_unreadable: false,
    model: `${name}-model`,
    structured_output: { anthropic: 'forced tool-use', openai: 'strict schema', gemini: 'responseSchema' }[
      name
    ],
    price: { input: 1, output: 2, valid_until: null },
    models: [`${name}-model`],
    price_basis: {
      provider: name,
      model: `${name}-model`,
      source: 'table',
      input: 1,
      output: 2,
      valid_until: null,
      then: null
    }
  })),
  settings: { extraction_provider: null, parallel: null, parallel_providers: null, passes: null, cap_usd: null },
  meter: {
    spent_usd: '0',
    unsettled_usd: '0',
    cap_usd: null,
    remaining_usd: null,
    period_start: '2026-09-01T00:00:00+02:00',
    period_end: '2026-10-01T00:00:00+02:00',
    tz: 'Europe/Berlin'
  },
  estimate: {
    per_title_usd: 'unknown',
    input_tokens_assumed: 23500,
    output_tokens_assumed: 3900,
    passes: null,
    providers: [],
    reason: 'no extraction provider is assigned',
    basis: []
  },
  projected: {
    window_days: 30,
    titles: 0,
    ever_filed: false,
    monthly_usd: null,
    remaining_usd: null,
    exceeds_remaining: null,
    reason: 'there is no acquisition history yet'
  },
  batch: { available: false, reason: 'batch endpoints are not used at M5 (decision 338)' }
});

/** `GET /api/admin/connectors`: the three keyed sources as booleans and the keyless five. */
const sources = () => ({
  sources: [
    { name: 'tmdb', has_api_key: true, secrets_unreadable: false, required: true, used_by: 'tmdb:detail' },
    { name: 'omdb', has_api_key: false, secrets_unreadable: false, required: false, used_by: 'omdb' },
    {
      name: 'trakt',
      has_client_id: false,
      has_client_secret: false,
      secrets_unreadable: false,
      required: false,
      used_by: 'trakt'
    }
  ],
  keyless: ['wikidata', 'wikipedia', 'tvmaze', 'rottentomatoes', 'metacritic']
});

/** Every GET the page makes, by path, so each test overrides only the one it is about. */
function wire(over = {}) {
  const answers = {
    '/admin/users': () => [],
    '/admin/llm': llm,
    '/admin/connectors': sources,
    '/admin/connectors/jellyfin/libraries': libraries,
    ...over
  };
  vi.mocked(get).mockImplementation(async (path) => {
    const answer = answers[path];
    if (!answer) throw new Error(`unexpected GET ${path}`);
    return answer();
  });
}

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(jellyfinDirectory).mockReset();
  wire();
  vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg(), users: [] });
});

afterEach(() => {
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(ConnectorsPage, { target });
  await settle();
  return app;
}

const press = async (label) => {
  const button = [...target.querySelectorAll('button')].find((b) => b.textContent.includes(label));
  expect(button, `no button named ${label}`).toBeDefined();
  button.click();
  await settle();
};

describe('the sweep line', () => {
  it('reports a sweep whose Played writes all failed as a failure', async () => {
    vi.mocked(post).mockResolvedValue(
      report({ push_failed: 4, push_errors: ['GET /Users/jf-1/Items -> 404'] })
    );
    const app = await open();
    try {
      await press('Sync now');
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('failing');
      const failure = target.querySelector(FAILURE);
      expect(failure).not.toBeNull();
      expect(failure.textContent).toContain('4 Played write(s) failed');
      expect(failure.textContent).toContain('GET /Users/jf-1/Items -> 404');
    } finally {
      unmount(app);
    }
  });

  it('names the other three debts the sweep can now count', async () => {
    // `resolve.unmatched` is a count and `unmatched_names` the list, as `SyncReport.as_dict` sends.
    vi.mocked(post).mockResolvedValue(
      report({
        owed_no_token: 2,
        unowned: 3,
        resolve: { unmatched: 2, unmatched_names: ['jf-90', 'jf-91'] }
      })
    );
    const app = await open();
    try {
      await press('Sync now');
      // Whitespace-collapsed: the sentence is wrapped across source lines.
      const line = target.querySelector(SYNC).textContent.replace(/\s+/g, ' ');
      expect(line).toContain('2 owed write(s) with no stored sign-in');
      expect(line).toContain('3 title(s) no longer in the library');
      expect(line).toContain('2 library item(s) matched no title');
      // Nothing failed, so the health attribute must not cry wolf.
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('ok');
      expect(target.querySelector(FAILURE)).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('refuses to call a sweep that never read the library healthy', async () => {
    // An unreachable Jellyfin returns an all-zero report with `users` empty and nothing skipped.
    vi.mocked(post).mockResolvedValue(report({ unchanged: 0, users: [], completed: [] }));
    const app = await open();
    try {
      await press('Sync now');
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('unreachable');
      expect(target.querySelector('[data-sync-unreachable]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('refuses to call a sweep that lost a member healthy either', async () => {
    // A deleted member account 404s that member's read while the library read still works.
    vi.mocked(post).mockResolvedValue(
      report({ unchanged: 0, completed: ['jenny'], failed_users: ['patrick'] })
    );
    const app = await open();
    try {
      await press('Sync now');
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('unreachable');
      const alert = target.querySelector('[data-sync-member-failed]');
      expect(alert).not.toBeNull();
      expect(alert.textContent.replace(/\s+/g, ' ')).toContain('but not for patrick');
      // The other alert is about a library read that never happened, which is not what this is.
      expect(target.querySelector('[data-sync-unreachable]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('calls a household with nothing configured neither healthy nor unreachable', async () => {
    // A half-configured install is legal (§3.1): a sweep that correctly did nothing is not an outage.
    vi.mocked(post).mockResolvedValue(
      report({ unchanged: 0, users: [], completed: [], skipped_no_link: true })
    );
    const app = await open();
    try {
      await press('Sync now');
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('ok');
      expect(target.querySelector('[data-sync-unreachable]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('says a sweep was already running rather than printing its empty counters', async () => {
    // The advisory lock answers a concurrent sweep with an all-zero report.
    vi.mocked(post).mockResolvedValue(
      report({ unchanged: 0, users: [], completed: [], already_running: true })
    );
    const app = await open();
    try {
      await press('Sync now');
      expect(target.querySelector(SYNC).getAttribute('data-sync')).toBe('already-running');
      expect(target.querySelector(SYNC).textContent).toContain('a sweep is already running');
      expect(target.querySelector(SYNC).textContent).not.toContain('pushed');
    } finally {
      unmount(app);
    }
  });
});

describe("§7.1's pin", () => {
  it('names the write, not the reads, when the stored verdict is below the pin', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ server_version: '10.8.13', server_supported: false }),
      users: []
    });
    const app = await open();
    try {
      const verdict = target.querySelector(VERDICT);
      expect(verdict).not.toBeNull();
      expect(verdict.getAttribute('data-server-supported')).toBe('false');
      expect(verdict.textContent).toContain('POST /UserPlayedItems');
      expect(verdict.textContent).toContain('10.8.13');
    } finally {
      unmount(app);
    }
  });

  it('says nothing at all until something has probed', async () => {
    // `null` is "nobody has probed", a fresh install's state, not a refusal.
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ server_version: '', server_supported: null }),
      users: []
    });
    const app = await open();
    try {
      expect(target.querySelector(VERDICT)).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('names the write in the test button answer too', async () => {
    vi.mocked(post).mockResolvedValue({
      ok: true,
      server_name: 'Fake Jellyfin',
      version: '10.8.13',
      supported: false,
      min_version: '10.9',
      user_count: 2
    });
    const app = await open();
    try {
      await press('Test connection');
      const probe = target.querySelector(PROBE);
      expect(probe.textContent).toContain('POST');
      expect(probe.textContent).toContain('/UserPlayedItems');
      expect(probe.textContent).not.toContain('reads may miss fields');
    } finally {
      unmount(app);
    }
  });
});

const JELLYFIN = '[data-testid="connector-jellyfin"]';

/** A button whose whole name is `label`, inside `scope`: several cards now carry a Save. */
function button(label, scope = JELLYFIN) {
  const root = target.querySelector(scope);
  expect(root, `no ${scope} on the page`).not.toBeNull();
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
  expect(found, `no button named exactly ${label} in ${scope}`).toBeDefined();
  return found;
}

function library(name) {
  const label = [...target.querySelectorAll('[data-library-pick] label')].find((l) =>
    l.textContent.includes(name)
  );
  expect(label, `no library named ${name} in the pick`).toBeDefined();
  return label.querySelector('input[type="checkbox"]');
}

const jellyfinPuts = () =>
  vi
    .mocked(api)
    .mock.calls.filter(
      ([path, opts]) => path === '/admin/connectors/jellyfin' && opts?.method === 'PUT'
    )
    .map(([, opts]) => opts.body);

describe("the Jellyfin card's library pick (decisions 364, 410, 455)", () => {
  it('is its own write: the plain Save sends the URL and the key and never the pick', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ library_ids: ['lib-films'] }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      // A changed, unsaved pick: the plain Save must not carry it into the stored boundary.
      library('Series').click();
      await settle();
      button('Save').click();
      await settle();
      expect(jellyfinPuts()).toEqual([{ url: 'http://jellyfin.test', api_key: '' }]);
    } finally {
      unmount(app);
    }
  });

  it('sends only library_ids from its own button, and only once the selection changed', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ library_ids: ['lib-films'] }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      expect(library('Films').checked, 'the stored pick is what the boxes start from').toBe(true);
      expect(button('Save library pick').disabled, 'nothing changed, nothing to send').toBe(true);
      library('Series').click();
      await settle();
      expect(button('Save library pick').disabled).toBe(false);
      button('Save library pick').click();
      await settle();
      expect(jellyfinPuts()).toEqual([{ library_ids: ['lib-films', 'lib-series'] }]);
    } finally {
      unmount(app);
    }
  });

  it('a library list that failed disables the pick and never sends []', async () => {
    // Decision 364 reads `[]` as the whole server, so a pick from a failed list would widen it.
    wire({
      '/admin/connectors/jellyfin/libraries': () =>
        libraries({ ok: false, error: 'Jellyfin answered 401', libraries: [] })
    });
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ library_ids: ['lib-films'] }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      const pick = target.querySelector('[data-library-pick]');
      expect(pick.getAttribute('data-library-pick')).toBe('unavailable');
      expect(pick.textContent).toContain('Jellyfin answered 401');
      const save = button('Save library pick');
      expect(save.disabled).toBe(true);
      save.click();
      await settle();
      expect(jellyfinPuts()).toEqual([]);
      for (const box of pick.querySelectorAll('input[type="checkbox"]')) {
        expect(box.disabled).toBe(true);
      }
    } finally {
      unmount(app);
    }
  });

  it('shows a picked library the server no longer lists as stale (decision 410)', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ library_ids: ['lib-films', 'lib-gone'] }),
      users: []
    });
    const app = await open();
    try {
      const stale = target.querySelector('[data-library-stale]');
      expect(stale).not.toBeNull();
      expect(stale.textContent).toContain('lib-gone');
      expect(stale.textContent).not.toContain('lib-films');
    } finally {
      unmount(app);
    }
  });
});

describe("the Jellyfin card's webhook (decisions 418, 455)", () => {
  const trigger = (over = {}) => ({
    webhook: {
      last_item_added_at: '2026-09-23T20:15:00+00:00',
      last_delivery_at: '2026-09-24T08:00:00+00:00',
      deliveries_7d: 9,
      last_refusal: { at: '2026-09-24T08:00:00+00:00', reason: 'not an ItemAdded: PlaybackStart' }
    },
    delta_poll: {
      watermark: '2026-09-24T07:40:00+00:00',
      last_run_at: '2026-09-24T07:45:00+00:00',
      last_run_ok: true,
      last_ok_at: '2026-09-24T07:45:02+00:00',
      last_filed: 2
    },
    ...over
  });

  it('reports what arrived and what the poll did, as facts', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg({ trigger: trigger() }), users: [] });
    const app = await open();
    try {
      const webhook = target.querySelector('[data-trigger="webhook"]');
      expect(webhook.getAttribute('data-item-added')).toBe('received');
      expect(webhook.textContent.replace(/\s+/g, ' ')).toContain('9 in the last 7 days');
      // The refusal is `acquire/intake`'s own sentence, rendered as it was recorded.
      expect(webhook.textContent).toContain('not an ItemAdded: PlaybackStart');
      const poll = target.querySelector('[data-trigger="delta-poll"]');
      expect(poll.getAttribute('data-poll-outcome')).toBe('ok');
      expect(poll.textContent.replace(/\s+/g, ' ')).toContain('filed 2');
    } finally {
      unmount(app);
    }
  });

  it('says when no ItemAdded has ever arrived, whatever else was delivered', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({
        trigger: trigger({ webhook: { ...trigger().webhook, last_item_added_at: null } })
      }),
      users: []
    });
    const app = await open();
    try {
      const webhook = target.querySelector('[data-trigger="webhook"]');
      expect(webhook.getAttribute('data-item-added')).toBe('none');
      expect(webhook.textContent).toContain('none received yet');
    } finally {
      unmount(app);
    }
  });

  it('still renders a card read without the trigger, as the M4.7-era payload is', async () => {
    // `18-system.spec.js` test 7 fulfils this GET with exactly this shape.
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: {
        url: 'http://jellyfin.test',
        has_api_key: false,
        configured: false,
        library_ids: [],
        linked_users: 0,
        secrets_unreadable: true
      },
      users: []
    });
    const app = await open();
    try {
      expect(target.querySelector('[data-secrets="unreadable"]')).not.toBeNull();
      expect(target.querySelector('[data-trigger]')).toBeNull();
      expect(button('Save').disabled).toBe(false);
      // `has_webhook_token` absent is not `false`: no Generate is offered on a guess.
      expect(target.textContent).not.toContain('Generate webhook token');
    } finally {
      unmount(app);
    }
  });

  it('mints a token only on Generate, shows it once, and offers no second press', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: false, trigger: trigger() }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({ webhook_token: 'tok-once-only-7f3a' });
    const app = await open();
    try {
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ has_webhook_token: true, trigger: trigger() }),
        users: []
      });
      button('Generate webhook token').click();
      await settle();
      expect(jellyfinPuts()).toEqual([{ mint_webhook_token: true }]);
      const reveal = target.querySelector('[data-webhook-token]');
      expect(reveal.textContent).toContain('tok-once-only-7f3a');
      expect(target.textContent).toContain('X-Spielplan-Token');
      // `PUBLIC_URL` as the wizard shows it: the origin the plugin has to post to.
      expect(target.textContent).toContain('http://localhost:8080/events/jellyfin');
      expect(target.innerHTML.split('tok-once-only-7f3a').length - 1, 'shown once').toBe(1);
      expect(target.textContent).not.toContain('Generate webhook token');
    } finally {
      unmount(app);
    }
  });

  it('offers no Generate once a token exists, and says it cannot be shown again', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: true, trigger: trigger() }),
      users: []
    });
    const app = await open();
    try {
      expect(target.textContent).not.toContain('Generate webhook token');
      const state = target.querySelector('[data-webhook-token-state]');
      expect(state.getAttribute('data-webhook-token-state')).toBe('exists');
      expect(state.textContent).toContain('cannot be shown again');
    } finally {
      unmount(app);
    }
  });

  it('never sends the mint flag from the plain Save', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: false }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      button('Save').click();
      await settle();
      expect(jellyfinPuts()).toEqual([{ url: 'http://jellyfin.test', api_key: '' }]);
    } finally {
      unmount(app);
    }
  });

  // `save_jellyfin` mints only for a configured connector; elsewhere it answers `webhook_token: null`.
  it('offers no Generate until the connector is configured, the only state it mints in', async () => {
    const unconfigured = [
      { url: '', has_api_key: false, configured: false },
      { has_api_key: false, configured: false },
      { has_api_key: false, configured: false, secrets_unreadable: true }
    ];
    for (const over of unconfigured) {
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ ...over, has_webhook_token: false, trigger: trigger() }),
        users: []
      });
      const app = await open();
      try {
        expect(target.textContent).not.toContain('Generate webhook token');
        const state = target.querySelector('[data-webhook-token-state]');
        expect(state.getAttribute('data-webhook-token-state')).toBe('unconfigured');
        expect(state.textContent.replace(/\s+/g, ' ')).toContain(
          'once the server URL and API key are saved'
        );
        expect(target.textContent).not.toContain('already existed');
      } finally {
        unmount(app);
      }
    }

    // Saved, and so configured: the press is offered on the same visit.
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ url: '', has_api_key: false, configured: false, has_webhook_token: false }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ has_webhook_token: false, trigger: trigger() }),
        users: []
      });
      button('Save').click();
      await settle();
      expect(button('Generate webhook token').disabled).toBe(false);
    } finally {
      unmount(app);
    }
  });

  it('never says a token already existed unless the server says one does', async () => {
    // The key was cleared between the read and the press, so nothing is minted.
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: false, trigger: trigger() }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({ has_api_key: false, configured: false, webhook_token: null });
    let app = await open();
    try {
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ has_api_key: false, configured: false, has_webhook_token: false, trigger: trigger() }),
        users: []
      });
      button('Generate webhook token').click();
      await settle();
      expect(target.textContent).not.toContain('already existed');
      expect(target.querySelector('[data-webhook-token-state="revealed"]')).toBeNull();
      expect(target.querySelector('[data-webhook-unminted]').textContent).toContain(
        'Nothing was minted'
      );

      // Configured again by a Save: the press is back, and the note about the last one is gone.
      vi.mocked(api).mockResolvedValue({});
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ has_webhook_token: false, trigger: trigger() }),
        users: []
      });
      button('Save').click();
      await settle();
      expect(button('Generate webhook token').disabled).toBe(false);
      expect(target.querySelector('[data-webhook-unminted]')).toBeNull();
    } finally {
      unmount(app);
    }

    // Another tab minted first: the one case "already existed" is true of.
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: false, trigger: trigger() }),
      users: []
    });
    vi.mocked(api).mockResolvedValue({ webhook_token: null });
    app = await open();
    try {
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ has_webhook_token: true, trigger: trigger() }),
        users: []
      });
      button('Generate webhook token').click();
      await settle();
      const state = target.querySelector('[data-webhook-token-state]');
      expect(state.getAttribute('data-webhook-token-state')).toBe('revealed');
      expect(state.textContent).toContain('already existed');
      expect(target.querySelector('[data-webhook-token]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('the sweep line names what it could not identify (D4)', () => {
  it('lists resolve.unmatched_names under the unmatched count', async () => {
    vi.mocked(post).mockResolvedValue(
      report({ resolve: { unmatched: 2, unmatched_names: ['Home Movies 2019', 'jf-91'] } })
    );
    const app = await open();
    try {
      await press('Sync now');
      const names = target.querySelector('[data-unmatched-names]');
      expect(names).not.toBeNull();
      expect(names.textContent).toContain('Home Movies 2019');
      expect(names.textContent).toContain('jf-91');
    } finally {
      unmount(app);
    }
  });
});

describe("the first-boot wizard's connector rows (plan C3; user test 2026-09-25)", () => {
  it('all three link to this page, and none carries a milestone tag', async () => {
    const app = mount(SetupPage, { target });
    await settle();
    try {
      const rows = [...target.querySelectorAll('.rows li')];
      const row = (name) => rows.find((li) => li.textContent.includes(name));
      for (const name of ['Jellyfin', 'LLM providers', 'TMDB / OMDb / Trakt']) {
        const link = row(name).querySelector('a');
        expect(link, `${name} is a link`).not.toBeNull();
        expect(link.getAttribute('href')).toBe('/admin/connectors');
        expect(row(name).textContent, `${name} still names a milestone`).not.toMatch(/· M\d/);
      }
    } finally {
      unmount(app);
    }
  });
});

describe('the rest of the Connectors card (plan B, C, D3)', () => {
  it('no longer says the library pick and the webhook arrive later', async () => {
    const app = await open();
    try {
      expect(target.textContent).not.toContain('arrive with M5');
    } finally {
      unmount(app);
    }
  });

  it('mounts the three provider cards and the three source cards beside the Jellyfin card', async () => {
    const app = await open();
    try {
      const providers = [...target.querySelectorAll('[data-provider]')];
      expect(providers.map((c) => c.getAttribute('data-provider'))).toEqual([
        'anthropic',
        'openai',
        'gemini'
      ]);
      const cards = [...target.querySelectorAll('[data-source]')];
      expect(cards.map((c) => c.getAttribute('data-source'))).toEqual(['tmdb', 'omdb', 'trakt']);
      expect(target.querySelector('[data-keyless]').textContent).toContain('Rotten Tomatoes');
      expect(target.querySelector('[data-testid="spend-meter"]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('keeps the Jellyfin card on the page when the LLM read fails', async () => {
    wire({
      '/admin/llm': () => {
        throw new Error('Not Found');
      }
    });
    const app = await open();
    try {
      expect(target.querySelector(JELLYFIN)).not.toBeNull();
      expect(target.textContent).toContain('Not Found');
    } finally {
      unmount(app);
    }
  });
});
