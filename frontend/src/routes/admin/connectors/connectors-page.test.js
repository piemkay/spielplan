/**
 * @vitest-environment jsdom
 *
 * What §6.6's Jellyfin card says after a sweep. Spec v2.1 §6.6, §7.1, §7.3; M4.11 findings 3 and 16.
 *
 * Two sentences on this card were untrue in the same direction — both of them made a dead
 * app -> Jellyfin path read as a quiet household.
 *
 *   - The sweep line printed `pushed 0 · adopted 0 · unchanged 87` while every Played write in the
 *     sweep was being refused. There was no counter for a failed push at all, so the one number that
 *     would have said so did not exist; `owed_no_token`, `unowned` and the unmatched-item count were
 *     added by the same milestone and had nowhere to render either.
 *   - The version line said "below the pinned 10.9 — reads may miss fields", which names the wrong
 *     half. By §7.1 and the client's own pin the 10.9-only route is the per-user Played WRITE: reads
 *     degrade, the write does not exist. An admin reading that line had no way to know that nothing
 *     this app marked could reach Jellyfin.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT, and named `connectors-page.test.js` because SvelteKit reserves
 * the `+` prefix inside `src/routes` (`rate-page.test.js` carries the same note). A card that reports
 * a failure can only be asserted against a sweep that failed, and the e2e stack's fake Jellyfin
 * accepts the per-user token — reaching the all-writes-refused state from outside means breaking the
 * connector for every spec after it in a filename-ordered suite. The payload is the fixture here, so
 * each state is exact.
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
// The page reads `session.publicUrl` for the webhook path, and the first-boot wizard below reads
// `session.setup` to decide which step it is on; `goto` is the wizard's Finish.
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
  linked_users: 2,
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
      // The sweep's own first distinct reason, not a generic sentence: §6.8's register again.
      expect(failure.textContent).toContain('GET /Users/jf-1/Items -> 404');
    } finally {
      unmount(app);
    }
  });

  it('names the other three debts the sweep can now count', async () => {
    // `resolve.unmatched` is a COUNT and `unmatched_names` is the list — `SyncReport.as_dict`'s own
    // shape, because this fixture had it as an array and the card read `.length` off it: both sides
    // agreed on a payload `api/admin.py` never sends, so the clause rendered here and nowhere else.
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
      // Whitespace-collapsed: the sentence is wrapped across source lines to stay inside the
      // 108-column rule, so the DOM carries the newlines the markup does.
      const line = target.querySelector(SYNC).textContent.replace(/\s+/g, ' ');
      expect(line).toContain('2 owed write(s) with no stored sign-in');
      expect(line).toContain('3 title(s) no longer in the library');
      expect(line).toContain('2 library item(s) matched no title');
      // Still a completed sweep: nothing failed, so the health attribute must not cry wolf.
      expect(target.querySelector(SYNC).getAttribute('data-sync-health')).toBe('ok');
      expect(target.querySelector(FAILURE)).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('refuses to call a sweep that never read the library healthy', async () => {
    // §3.3 makes an unreachable Jellyfin a degraded sync, so `seen.sync_all` returns the report as
    // it stands — every counter zero, `push_failed` included, and the per-user loop never entered,
    // which is why `users` is empty while `skipped_no_link` is false. Through the old rule that
    // printed as `ok`: the same line a quiet healthy household gets, for a sweep in which NEITHER
    // direction of §7.3 ran. It is the state `08-jellyfin.spec.js`'s adopt direction failed under,
    // and the attribute is what turns that into a diagnosis. [M4.11 finding 3]
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
    // The same fault through the other door, which the rule above could not see: the library read
    // is keyless and each member's is not, so a Jellyfin account that was deleted or renamed 404s
    // that member's `/Items` while the household read keeps working. `sync_all` swallows it per
    // member, so `users` is full, `completed` is short and every counter is zero — and the old
    // rule, keyed on an empty `users`, printed that in green. [review cycle 1: seen-02]
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
    // The negative control, and the reason `skipped_no_link` is in the rule: §3.1 makes a
    // half-configured install legal, so "no connector, or no linked account" is a sweep that
    // correctly did nothing and must not be reported as an outage.
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
    // §5.3 fires this job every fifteen minutes and this button is the other caller; the advisory
    // lock answers with an all-zero report, which printed as a sweep that found nothing to do.
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
    // The negative control: `null` is "nobody has probed", which is the state of a fresh install
    // and must not render as a refusal.
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

// --- M5.7: the card's two missing halves, and the cards beside it (decisions 364, 410, 418, 455) --

const JELLYFIN = '[data-testid="connector-jellyfin"]';

/** A button whose whole name is `label`, inside `scope`: several cards now carry a Save. */
function button(label, scope = JELLYFIN) {
  const root = target.querySelector(scope);
  expect(root, `no ${scope} on the page`).not.toBeNull();
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
  expect(found, `no button named exactly ${label} in ${scope}`).toBeDefined();
  return found;
}

/** The library pick's checkbox for one library, by its label text. */
function library(name) {
  const label = [...target.querySelectorAll('[data-library-pick] label')].find((l) =>
    l.textContent.includes(name)
  );
  expect(label, `no library named ${name} in the pick`).toBeDefined();
  return label.querySelector('input[type="checkbox"]');
}

/** Every body the page has PUT to the Jellyfin connector, in order. */
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
      // A pick the admin has changed and not saved: exactly the state a Save sent on every press
      // would carry into the stored boundary.
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
    // Decision 364 reads `[]` as "the whole server", so a pick built from a list that did not
    // arrive would widen the acquisition boundary to everything in one press.
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

  // `save_jellyfin` mints only for a connector that is configured, and a Generate offered on any
  // other -- a fresh install, a URL saved without its key, a key this SECRETS_KEY cannot open --
  // was answered 200 with `webhook_token: null`, which the card read as "a token already existed"
  // where none did, and then hid the press for the rest of the visit, a Save included.
  // [M5.7 review cycle 1, M57-JFSYS-02]
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
    // The key cleared between the read and the press: the server mints nothing and says so by
    // answering `null` beside a connector that is no longer configured.
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

    // Another tab minted first: `null` beside a token the server now holds is the one case the
    // sentence is true of.
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

describe("the first-boot wizard's two placeholder rows (plan C3)", () => {
  it('link to this page now that its cards exist, and leave the Jellyfin row alone', async () => {
    const app = mount(SetupPage, { target });
    await settle();
    try {
      const rows = [...target.querySelectorAll('.rows li')];
      const row = (name) => rows.find((li) => li.textContent.includes(name));
      for (const name of ['LLM providers', 'TMDB / OMDb / Trakt']) {
        const link = row(name).querySelector('a');
        expect(link, `${name} is a link`).not.toBeNull();
        expect(link.getAttribute('href')).toBe('/admin/connectors');
      }
      expect(target.textContent).not.toContain('configure in Admin · M5');
      expect(row('Jellyfin').textContent).toContain('configure in Admin · M1');
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
