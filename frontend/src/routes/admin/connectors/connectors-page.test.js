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

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/jellyfin.js', () => ({ jellyfinDirectory: vi.fn() }));

import { get, post } from '$lib/api.js';
import { jellyfinDirectory } from '$lib/jellyfin.js';
import ConnectorsPage from './+page.svelte';

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

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(jellyfinDirectory).mockReset();
  vi.mocked(get).mockResolvedValue([]); // `/admin/users`
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
