/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// A page store that follows `pushState`, so a sheet stays open once it has pushed its entry.
const nav = vi.hoisted(() => {
  let value = { url: new URL('http://localhost/admin/services'), state: {} };
  const runs = new Set();
  return {
    page: {
      subscribe(run) {
        runs.add(run);
        run(value);
        return () => runs.delete(run);
      }
    },
    push(state) {
      value = { ...value, state };
      for (const run of runs) run(value);
    },
    // Back, as the browser's popstate would: the newest sheet's entry goes.
    back() {
      this.push({ ...value.state, sheets: (value.state.sheets ?? []).slice(0, -1) });
    }
  };
});

vi.mock('$app/stores', () => ({ page: nav.page }));
vi.mock('$app/navigation', () => ({ goto: vi.fn(), pushState: (_url, state) => nav.push(state) }));
vi.mock('$lib/api.js', () => ({
  api: vi.fn(),
  get: vi.fn(),
  post: vi.fn(),
  ApiError: class extends Error {}
}));
vi.mock('$lib/jellyfin.js', () => ({ jellyfinDirectory: vi.fn() }));
// The page reads `session.publicUrl` for the webhook address.
vi.mock('$lib/session.svelte.js', () => ({
  session: { publicUrl: 'http://localhost:8080', user: null }
}));

import { api, get, post } from '$lib/api.js';
import { jellyfinDirectory } from '$lib/jellyfin.js';
import ServicesPage from './+page.svelte';

const SYNC = '[data-sync]';
const FAILURE = '[data-sync-failure]';
const VERDICT = '[data-server-supported]';
const JELLYFIN = '[data-testid="connector-jellyfin"]';

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

/** `GET /api/admin/connectors/jellyfin/libraries`, decision 364's envelope. */
const libraries = (over = {}) => ({
  ok: true,
  libraries: [
    { id: 'lib-films', name: 'Films' },
    { id: 'lib-series', name: 'Series' }
  ],
  ...over
});

/** `GET /api/admin/connectors`: the three keyed sources as booleans and the keyless five. */
const sources = () => ({
  sources: [
    { name: 'tmdb', has_api_key: true, secrets_unreadable: false, required: true, used_by: 'tmdb:detail' },
    { name: 'omdb', has_api_key: false, secrets_unreadable: false, required: false, used_by: 'omdb' },
    {
      name: 'trakt',
      has_client_id: true,
      has_client_secret: false,
      secrets_unreadable: false,
      required: false,
      used_by: 'trakt'
    }
  ],
  keyless: ['wikidata', 'wikipedia', 'tvmaze', 'rottentomatoes', 'metacritic']
});

const users = () => [
  {
    id: 1,
    name: 'admin',
    role: 'admin',
    jellyfin_user_id: 'jf-1',
    jellyfin_link_state: 'linked',
    has_jellyfin_token: true
  },
  { id: 2, name: 'jenny', role: 'member', jellyfin_user_id: null, has_jellyfin_token: false }
];

/** Every GET the page makes, by path, so each test overrides only the one it is about. */
function wire(over = {}) {
  const answers = {
    '/admin/users': users,
    '/admin/connectors': sources,
    '/admin/connectors/jellyfin/libraries': libraries,
    '/admin/system': () => ({ last_syncs: [{ name: 'jellyfin-seen-sync', at: null }] }),
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
  nav.push({});
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(jellyfinDirectory).mockReset();
  wire();
  vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg(), users: [{ id: 'jf-1', name: 'pat' }] });
});

afterEach(() => {
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(ServicesPage, { target });
  await settle();
  return app;
}

/** A button whose whole name is `label`, inside `scope`. */
function button(label, scope = JELLYFIN) {
  const root = target.querySelector(scope);
  expect(root, `no ${scope} on the page`).not.toBeNull();
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
  expect(found, `no button named exactly ${label} in ${scope}`).toBeDefined();
  return found;
}

const press = async (label, scope) => {
  button(label, scope).click();
  await settle();
};

/** Open the sheet behind a list row, the way a tap does. */
async function row(label) {
  const found = [...target.querySelectorAll('.list-row')].find((r) =>
    r.textContent.trim().startsWith(label)
  );
  expect(found, `no row named ${label}`).toBeDefined();
  found.click();
  await settle();
  return found;
}

const text = (selector) => target.querySelector(selector)?.textContent.replace(/\s+/g, ' ') ?? '';

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

describe('the sweep line', () => {
  it('reports a sweep whose Played writes all failed as a failure', async () => {
    vi.mocked(post).mockResolvedValue(
      report({ push_failed: 4, push_errors: ['GET /Users/jf-1/Items -> 404'] })
    );
    const app = await open();
    try {
      await press('Sync now');
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('failing');
      expect(text(FAILURE)).toContain("4 watched marks didn't reach Jellyfin");
      expect(text(SYNC)).not.toContain('Synced just now');
      // The server's reason, verbatim, one tap down.
      expect(text('[data-sync-report]')).toContain('GET /Users/jf-1/Items -> 404');
    } finally {
      unmount(app);
    }
  });

  it('names the other debts the sweep counts, in plain words', async () => {
    vi.mocked(post).mockResolvedValue(
      report({
        owed_no_token: 2,
        unowned: 3,
        resolve: { unmatched: 2, unmatched_names: ['Home Movies 2019', 'jf-91'] }
      })
    );
    const app = await open();
    try {
      await press('Sync now');
      const line = text(SYNC);
      expect(line).toContain("2 marks wait for their person's own Jellyfin sign-in");
      expect(line).toContain('3 titles are no longer in the library');
      expect(line).toContain('2 library items matched no title');
      expect(text('[data-unmatched-names]')).toContain('Home Movies 2019, jf-91');
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
      expect(text(SYNC)).not.toContain('Synced just now');
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
      expect(text('[data-sync-member-failed]')).toContain('but not for patrick');
      // The other alert is about a library read that never happened, which is not what this is.
      expect(target.querySelector('[data-sync-unreachable]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('calls a household with nothing linked neither healthy nor unreachable', async () => {
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
      expect(text(SYNC)).toContain('already running');
      expect(target.querySelector('[data-sync-report]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('reads the watched-status row off the last seen sync that reached Jellyfin', async () => {
    const at = new Date(Date.now() - 4 * 60_000).toISOString();
    wire({ '/admin/system': () => ({ last_syncs: [{ name: 'jellyfin-seen-sync', at }] }) });
    const app = await open();
    try {
      const watched = [...target.querySelectorAll('.list-row')].find((r) =>
        r.textContent.includes('Watched status')
      );
      expect(watched.textContent).toContain('Synced 4 min ago');
    } finally {
      unmount(app);
    }
  });
});

describe("§7.1's pin", () => {
  it('says plainly that marks cannot reach an old server, and names the write one tap down', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ server_version: '10.8.13', server_supported: false }),
      users: []
    });
    const app = await open();
    try {
      expect(text('[data-below-pin]')).toContain('older than 10.9');
      const verdict = target.querySelector(VERDICT);
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
      expect(target.querySelector('[data-below-pin]')).toBeNull();
      expect(text(JELLYFIN)).toContain('Connected');
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
      expect(target.querySelector('[data-probe]').getAttribute('data-probe')).toBe('ok');
      expect(text('[data-probe]')).toContain('Fake Jellyfin answered');
      expect(text('[data-below-pin]')).toContain('older than 10.9');
      expect(text('[data-probe-detail]')).toContain('POST /UserPlayedItems');
    } finally {
      unmount(app);
    }
  });
});

describe('the library pick (decisions 364, 410, 455)', () => {
  it('is its own write: the server Save sends the URL and the key and never the pick', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg({ library_ids: ['lib-films'] }), users: [] });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      // A changed, unsaved pick: the server's Save must not carry it into the stored boundary.
      await row('Libraries');
      library('Series').click();
      await settle();
      await row('Server address');
      await press('Save');
      expect(jellyfinPuts()).toEqual([{ url: 'http://jellyfin.test', api_key: '' }]);
    } finally {
      unmount(app);
    }
  });

  it('sends only library_ids from its own button, and only once the selection changed', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg({ library_ids: ['lib-films'] }), users: [] });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      expect(text(JELLYFIN), 'the row names the stored pick').toContain('Films');
      await row('Libraries');
      expect(library('Films').checked, 'the stored pick is what the boxes start from').toBe(true);
      expect(button('Save libraries').disabled, 'nothing changed, nothing to send').toBe(true);
      library('Series').click();
      await settle();
      expect(button('Save libraries').disabled).toBe(false);
      await press('Save libraries');
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
    vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg({ library_ids: ['lib-films'] }), users: [] });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      await row('Libraries');
      const pick = target.querySelector('[data-library-pick]');
      expect(pick.getAttribute('data-library-pick')).toBe('unavailable');
      expect(pick.textContent).toContain('Jellyfin answered 401');
      const save = button('Save libraries');
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
      await row('Libraries');
      const stale = target.querySelector('[data-library-stale]');
      expect(stale.textContent).toContain('lib-gone');
      expect(stale.textContent).not.toContain('lib-films');
    } finally {
      unmount(app);
    }
  });
});

describe('new-title alerts (decisions 418, 455)', () => {
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

  it('reports what arrived and what the poll did, verbatim one tap down', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: true, trigger: trigger() }),
      users: []
    });
    const app = await open();
    try {
      const webhook = target.querySelector('[data-trigger="webhook"]');
      expect(webhook.closest('details'), 'the facts sit under Technical details').not.toBeNull();
      expect(webhook.getAttribute('data-item-added')).toBe('received');
      expect(text('[data-trigger="webhook"]')).toContain('9 in the last 7 days');
      // The refusal is `acquire/intake`'s own sentence, rendered as it was recorded.
      expect(webhook.textContent).toContain('not an ItemAdded: PlaybackStart');
      const poll = target.querySelector('[data-trigger="delta-poll"]');
      expect(poll.getAttribute('data-poll-outcome')).toBe('ok');
      expect(text('[data-trigger="delta-poll"]')).toContain('filed 2');
      expect(text(JELLYFIN)).toMatch(/New-title alerts\s*\d+ (h|days) ago/);
    } finally {
      unmount(app);
    }
  });

  it('says when no ItemAdded has ever arrived, whatever else was delivered', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({
        has_webhook_token: true,
        trigger: trigger({ webhook: { ...trigger().webhook, last_item_added_at: null } })
      }),
      users: []
    });
    const app = await open();
    try {
      const webhook = target.querySelector('[data-trigger="webhook"]');
      expect(webhook.getAttribute('data-item-added')).toBe('none');
      expect(webhook.textContent).toContain('none received yet');
      expect(text(JELLYFIN)).toContain('New-title alerts None yet');
    } finally {
      unmount(app);
    }
  });

  it('still renders a card read without the trigger, as an older payload is', async () => {
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
      await row('API key');
      expect(button('Save').disabled).toBe(false);
      // `has_webhook_token` absent is not `false`: no Create is offered on a guess.
      await row('New-title alerts');
      expect(target.textContent).not.toContain('Create token');
    } finally {
      unmount(app);
    }
  });

  it('mints a token only on Create, shows it once with Copy, and offers no second press', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
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
      await row('New-title alerts');
      await press('Create token');
      expect(jellyfinPuts()).toEqual([{ mint_webhook_token: true }]);
      expect(text('[data-webhook-token]')).toContain('tok-once-only-7f3a');
      expect(target.textContent).toContain('X-Spielplan-Token');
      // `PUBLIC_URL` as the wizard shows it: the origin the plugin has to post to.
      expect(target.textContent).toContain('http://localhost:8080/events/jellyfin');
      expect(target.innerHTML.split('tok-once-only-7f3a').length - 1, 'shown once').toBe(1);
      expect(target.textContent).not.toContain('Create token');
      await press('Copy');
      expect(writeText).toHaveBeenCalledWith('tok-once-only-7f3a');
    } finally {
      unmount(app);
    }
  });

  it('offers no Create once a token exists, and says it cannot be shown again', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({
      cfg: cfg({ has_webhook_token: true, trigger: trigger() }),
      users: []
    });
    const app = await open();
    try {
      await row('New-title alerts');
      expect(target.textContent).not.toContain('Create token');
      const state = target.querySelector('[data-webhook-token-state]');
      expect(state.getAttribute('data-webhook-token-state')).toBe('exists');
      expect(state.textContent).toContain("can't be shown again");
    } finally {
      unmount(app);
    }
  });

  it('never sends the mint flag from the server Save', async () => {
    vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: cfg({ has_webhook_token: false }), users: [] });
    vi.mocked(api).mockResolvedValue({});
    const app = await open();
    try {
      await row('Server address');
      await press('Save');
      expect(jellyfinPuts()).toEqual([{ url: 'http://jellyfin.test', api_key: '' }]);
    } finally {
      unmount(app);
    }
  });

  // `save_jellyfin` mints only for a configured connector; elsewhere it answers `webhook_token: null`.
  it('offers no Create until the connector is configured, the only state it mints in', async () => {
    const unconfigured = [
      { url: '', has_api_key: false, configured: false },
      { has_api_key: false, configured: false },
      { has_api_key: false, configured: false, secrets_unreadable: true }
    ];
    for (const over of unconfigured) {
      nav.push({});
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ ...over, has_webhook_token: false, trigger: trigger() }),
        users: []
      });
      const app = await open();
      try {
        await row('New-title alerts');
        expect(target.textContent).not.toContain('Create token');
        const state = target.querySelector('[data-webhook-token-state]');
        expect(state.getAttribute('data-webhook-token-state')).toBe('unconfigured');
        expect(state.textContent.replace(/\s+/g, ' ')).toContain(
          'once the server address and API key are saved'
        );
        expect(target.textContent).not.toContain('already existed');
      } finally {
        unmount(app);
      }
    }

    // Saved, and so configured: the press is offered on the same visit.
    nav.push({});
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
      await row('Server address');
      await press('Save');
      await row('New-title alerts');
      expect(button('Create token').disabled).toBe(false);
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
      await row('New-title alerts');
      await press('Create token');
      expect(target.textContent).not.toContain('already existed');
      expect(target.querySelector('[data-webhook-token-state="revealed"]')).toBeNull();
      expect(text('[data-webhook-unminted]')).toContain('Nothing was made');

      // Configured again by a Save: the press is back, and the note about the last one is gone.
      vi.mocked(api).mockResolvedValue({});
      vi.mocked(jellyfinDirectory).mockResolvedValue({
        cfg: cfg({ has_webhook_token: false, trigger: trigger() }),
        users: []
      });
      await row('Server address');
      await press('Save');
      await row('New-title alerts');
      expect(button('Create token').disabled).toBe(false);
      expect(target.querySelector('[data-webhook-unminted]')).toBeNull();
    } finally {
      unmount(app);
    }

    // Another tab minted first: the one case "already existed" is true of.
    nav.push({});
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
      await row('New-title alerts');
      await press('Create token');
      const state = target.querySelector('[data-webhook-token-state]');
      expect(state.getAttribute('data-webhook-token-state')).toBe('revealed');
      expect(state.textContent).toContain('already existed');
      expect(target.querySelector('[data-webhook-token]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('people linked', () => {
  it('counts the links, and asks before an unlink drops a saved sign-in', async () => {
    vi.mocked(api).mockResolvedValue({ ok: true });
    const back = vi.spyOn(history, 'back').mockImplementation(() => nav.back());
    const app = await open();
    try {
      expect(text(JELLYFIN)).toContain('People linked 1 of 2');
      await row('People linked');
      const admin = target.querySelector('[data-user="admin"]');
      expect(admin.querySelector('[data-link-state]').getAttribute('data-has-token')).toBe('true');
      expect(target.querySelector('[data-user="jenny"] select')).not.toBeNull();

      await press('Unlink');
      expect(api, 'nothing is unlinked on the first tap').not.toHaveBeenCalled();
      const confirm = [...document.querySelectorAll('button[role="menuitem"]')].find(
        (b) => b.textContent.trim() === 'Unlink'
      );
      /** @type {HTMLButtonElement} */ (confirm).click();
      await settle();
      expect(api).toHaveBeenCalledWith('/admin/users/1/jellyfin', { method: 'DELETE' });
    } finally {
      unmount(app);
      back.mockRestore();
    }
  });
});

describe('film information and the rest of the page', () => {
  it('lists the three keyed sources with their state in words, and the keyless five', async () => {
    const app = await open();
    try {
      const rows = [...target.querySelectorAll('[data-source-row]')];
      expect(rows.map((r) => r.getAttribute('data-source-row'))).toEqual(['tmdb', 'omdb', 'trakt']);
      expect(rows[0].textContent).toContain('Required');
      expect(rows[0].textContent).toContain('Connected');
      expect(rows[1].textContent).toContain('Not set up');
      expect(rows[2].textContent).toContain('Secret missing');
      expect(text('[data-keyless]')).toContain('Metacritic need no key');
      expect(text('[data-keyless]')).toContain('Rotten Tomatoes');

      rows[0].click();
      await settle();
      expect(target.querySelector('[data-source="tmdb"]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('keeps the Jellyfin card on the page when the sources read fails', async () => {
    wire({
      '/admin/connectors': () => {
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

  it('points AI providers at Budget and AI', async () => {
    const app = await open();
    try {
      expect(target.querySelector('a[href="/admin/budget"]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });
});
