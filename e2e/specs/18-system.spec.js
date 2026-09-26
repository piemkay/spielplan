import { expect, test } from '@playwright/test';

import { JELLYFIN, signedIn } from '../helpers.js';

/**
 * Admin > System (§6.6, §2, §14.3; decisions 181, 182, 454): six read-only facts and one control
 * that narrows what was already read, so what is NOT here matters as much as what is.
 * Every DOM assertion reads the response THE PAGE RENDERED FROM: the worker's jobs run every
 * minute, so a second call may legitimately disagree. One page for the file; desktop only.
 * The last test is the Connectors card's custody warning, the twin of this card's.
 */
test.describe.configure({ mode: 'serial' });

// Decisions 182 and 454, sorted as the route's keys are compared.
const FACTS = ['backup', 'jobs', 'last_syncs', 'logs', 'queue', 'secrets'];

test.describe('the System card', () => {
  /** @type {import('@playwright/test').Page} */
  let admin;
  const contexts = [];

  test.beforeAll(async ({ browser, baseURL }) => {
    const context = await browser.newContext({ baseURL });
    contexts.push(context);
    admin = await context.newPage();
    await signedIn(admin);
  });

  test.afterAll(async () => {
    for (const context of contexts) await context.close();
  });

  async function render() {
    const [res] = await Promise.all([
      admin.waitForResponse(
        (r) => r.url().endsWith('/api/admin/system') && r.request().method() === 'GET'
      ),
      admin.goto('/admin/system')
    ]);
    return res.json();
  }

  // --- 1 ------------------------------------------------------------------------------------

  test('the System tab is a link to a card of six facts and no control that writes', async () => {
    await admin.goto('/admin/data');
    await admin.getByRole('link', { name: 'System' }).click();
    await expect(admin.getByRole('heading', { name: 'System' })).toBeVisible();
    await expect(admin).toHaveURL(/\/admin\/system$/);

    for (const fact of FACTS) {
      await expect(admin.getByTestId(`system-${fact}`)).toBeVisible();
    }

    // "and no more", on the route's keys, where a new fact would arrive first.
    const body = await (await admin.request.get('/api/admin/system')).json();
    expect(Object.keys(body).sort()).toEqual(FACTS);

    // Inside the six facts only (the shell has buttons of its own): the one control is the log
    // level select.
    const controls = admin
      .locator(FACTS.map((f) => `[data-testid="system-${f}"]`).join(', '))
      .locator('button, input, select, [role=button]');
    await expect(controls).toHaveCount(1);
    const level = admin.getByTestId('system-logs').getByLabel('LOG LEVEL');
    await expect(admin.getByTestId('system-logs').locator('select')).toHaveCount(1);
    await expect(level).toBeVisible();

    // It asks the server nothing (decision 454). The closing round trip gives a stray request
    // time to leave; `admin.request` is not the page's network.
    const asked = [];
    const watch = (request) => {
      const path = new URL(request.url()).pathname;
      if (request.method() !== 'GET' || path === '/api/admin/system') {
        asked.push(`${request.method()} ${path}`);
      }
    };
    admin.on('request', watch);
    try {
      for (const value of ['warning', 'error', 'all']) {
        await level.selectOption(value);
        await expect(level).toHaveValue(value);
      }
      expect((await admin.request.get('/api/admin/system')).ok()).toBeTruthy();
    } finally {
      admin.off('request', watch);
    }
    expect(asked, 'the log level filter sent a request').toEqual([]);
  });

  // --- 2 ------------------------------------------------------------------------------------

  test('custody is reported as a fingerprint and a key_id, never as the key', async () => {
    const { secrets } = await render();
    // The whole field list: no fifth field may carry key material (§14.3).
    expect(Object.keys(secrets).sort()).toEqual([
      'configured',
      'fingerprint',
      'key_id',
      'unreadable'
    ]);
    expect(secrets.configured, 'the e2e stack sets SECRETS_KEY').toBe(true);
    expect(secrets.fingerprint).toMatch(/^[0-9a-f]{12}$/);
    expect(typeof secrets.key_id, 'the active data-encryption key row').toBe('string');

    const card = admin.getByTestId('system-secrets');
    await expect(card).toContainText(secrets.fingerprint);
    await expect(card).toContainText(secrets.key_id);
    expect(secrets.unreadable).toBe(false);
    await expect(admin.getByTestId('system-secrets-unreadable')).toHaveCount(0);
  });

  // --- 3 ------------------------------------------------------------------------------------

  test('the backup fact names the newest successful dump, and warns when there is none', async () => {
    const { backup } = await render();
    expect(
      backup.stale_after_hours,
      '§2 promises one dump a night; 36 h is a night plus half a day of slack'
    ).toBe(36);

    const card = admin.getByTestId('system-backup');
    if (backup.at === null) {
      await expect(card).toContainText('no dump has ever completed');
      expect(backup.stale, 'never having dumped is stale by the same rule').toBe(true);
    } else {
      // The age, not the locale-formatted timestamp.
      await expect(card).not.toContainText('no dump has ever completed');
      await expect(card).toContainText(/ago/);
    }
    await expect(admin.getByTestId('system-backup-stale')).toHaveCount(backup.stale ? 1 : 0);
  });

  // --- 4 ------------------------------------------------------------------------------------

  test('every job the worker has recorded is listed with its outcome', async () => {
    // Polled, so a stopped worker is reported as that rather than as an empty list.
    await expect
      .poll(async () => (await (await admin.request.get('/api/admin/system')).json()).jobs.length, {
        message: 'the worker has recorded no job_run row at all — is the worker container up?',
        timeout: 90_000,
        intervals: [3000]
      })
      .toBeGreaterThan(0);

    const { jobs } = await render();
    const rows = admin.getByTestId('system-job');
    await expect(rows).toHaveCount(jobs.length);

    for (const job of jobs) {
      const row = admin.locator(`[data-job="${job.name}"]`);
      await expect(row).toBeVisible();
      // No `finished_at` is its own fact (a kill, an OOM), not a guessed outcome.
      const expected = job.finished_at === null ? 'unfinished' : job.ok ? 'ok' : 'failed';
      await expect(row).toHaveAttribute('data-outcome', expected);
    }
    // The minutely job proves a live worker rather than a seeded table.
    expect(jobs.map((j) => j.name)).toContain('jellyfin-sessions-poll');
  });

  // --- 5 ------------------------------------------------------------------------------------

  test('the queue depth is reported by state and kind', async () => {
    // Decision 454: all five states, zeros included, so "nothing failed" is a 0 and not an absence.
    const { queue } = await render();
    const states = ['pending', 'leased', 'done', 'failed', 'skipped'];
    expect(Object.keys(queue.by_state).sort()).toEqual([...states].sort());

    const card = admin.getByTestId('system-queue');
    for (const state of states) {
      // Word-bounded, because "pending 1" is a substring of "pending 10".
      await expect(card).toContainText(new RegExp(`\\b${state} ${queue.by_state[state]}\\b`));
      const rows = queue.by_kind.filter((row) => row.state === state);
      expect(rows.reduce((sum, row) => sum + row.count, 0), `${state} total`).toBe(
        queue.by_state[state]
      );
    }
    if (queue.by_kind.length === 0) {
      await expect(card.locator('[data-empty="queue"]')).toBeVisible();
    }
    for (const row of queue.by_kind) {
      await expect(card.locator(`[data-queue-kind="${row.kind}"]`)).toContainText(
        new RegExp(`\\b${row.state} ${row.count}\\b`)
      );
    }
    // Nothing here drains or retries: that is the board's.
    await expect(card.locator('button, input, select, [role=button]')).toHaveCount(0);
  });

  // --- 6 ------------------------------------------------------------------------------------

  test('the last successful sync of each connector is listed', async () => {
    // Decision 454: the last SUCCESS, which `jobs`' newest attempt cannot say. Every connector job
    // is listed, succeeded or not.
    const { jobs, last_syncs } = await render();
    expect(last_syncs.map((sync) => sync.name)).toEqual([
      'jellyfin-seen-sync',
      'jellyfin-delta-poll',
      'jellyfin-intake-sweep',
      'jellyfin-sessions-poll',
      'acquisition-drain'
    ]);
    for (const sync of last_syncs) {
      const row = admin.locator(`[data-last-sync="${sync.name}"]`);
      await expect(row).toHaveAttribute('data-synced', sync.at ? 'yes' : 'never');
      await expect(row).toContainText(sync.connector);
      await expect(row).toContainText(sync.at ? /ago/ : /never succeeded/);
      // A newest row that reached its server bounds the last sync from below. A row closed ok
      // that asked nobody (no report, or `reached: false`) is not a sync.
      const newest = jobs.find((job) => job.name === sync.name);
      if (newest?.ok === true && newest.detail != null && newest.detail.reached !== false) {
        expect(sync.at, `${sync.name} succeeded and is listed as never`).not.toBeNull();
        expect(Date.parse(sync.at)).toBeGreaterThanOrEqual(Date.parse(newest.finished_at));
      }
    }
  });

  // --- 7 ------------------------------------------------------------------------------------

  test('the recent log lines are listed and filter by level', async () => {
    // Decision 454: the web process's own lines, INFO and up, at most 200, redacted. The card
    // says the worker's are in its container log.
    const { logs } = await render();
    expect(logs.scope).toBe('web process');
    expect(logs.records.length).toBeLessThanOrEqual(200);
    for (const record of logs.records) {
      expect(Object.keys(record).sort()).toEqual(['at', 'level', 'logger', 'message']);
      // Never httpx, where a query-string key would have been written.
      expect(record.logger === 'spielplan' || record.logger.startsWith('spielplan.')).toBe(true);
    }
    expect(JSON.stringify(logs)).not.toContain(JELLYFIN.apiKey);

    const card = admin.getByTestId('system-logs');
    await expect(card).toContainText('web process');
    await expect(card).toContainText('container log');

    const RANK = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
    const lines = card.locator('[data-log-level]');
    const level = card.getByLabel('LOG LEVEL');
    for (const [value, floor] of [['all', 0], ['warning', 30], ['error', 40], ['all', 0]]) {
      await level.selectOption(value);
      const expected = logs.records.filter((r) => (RANK[r.level] ?? 0) >= floor).length;
      await expect(lines, `lines at ${value}`).toHaveCount(expected);
      await expect(card.locator('[data-empty="logs"]')).toHaveCount(expected === 0 ? 1 : 0);
    }
  });

  // --- 8 ------------------------------------------------------------------------------------

  test('the card is admin-only and the schema is not published to anonymous callers', async ({
    request
  }) => {
    // The `request` fixture has no cookies.
    expect((await request.get('/api/admin/system')).status()).toBe(401);

    // sec-12: the schema enumerates every admin route; it is not published.
    expect((await request.get('/api/docs')).status()).toBe(404);

    // Outside `/api` the SPA fallback answers 200 with the shell, so assert no schema came back.
    for (const path of ['/openapi.json', '/redoc']) {
      const body = await (await request.get(path)).text();
      expect(body, `${path} served the OpenAPI document`).not.toContain('"openapi"');
      expect(body, `${path} served the OpenAPI document`).not.toContain('"paths"');
    }
  });

  // --- 9 ------------------------------------------------------------------------------------

  test('custody says so when SECRETS_KEY no longer opens every stored secret', async () => {
    // A healthy stack cannot break custody, so the live payload is served with `unreadable`
    // flipped. The route's half is `test_admin_system.py`'s; this is the rendering half.
    const live = await (await admin.request.get('/api/admin/system')).json();
    expect(live.secrets.unreadable, 'the stack under test has intact custody').toBe(false);

    await admin.route('**/api/admin/system', (route) =>
      route.fulfill({ json: { ...live, secrets: { ...live.secrets, unreadable: true } } })
    );
    try {
      const { secrets } = await render();
      expect(secrets.unreadable).toBe(true);

      const warning = admin.getByTestId('system-secrets-unreadable');
      await expect(warning).toBeVisible();
      // Both ways back, named: the right `.env`, or the command that retires what will not open.
      await expect(warning).toContainText('.env');
      await expect(warning).toContainText('spielplan-secrets reset');
      await expect(admin.getByTestId('system-secrets')).toContainText(live.secrets.fingerprint);
    } finally {
      await admin.unroute('**/api/admin/system');
    }
  });

  // --- 10 -----------------------------------------------------------------------------------

  test('the Connectors card says what its own custody repair costs', async () => {
    // The twin, and where the repair is offered: the only place that says Save fixes Jellyfin
    // by retiring the DEK it cannot open, stranding web push with it (§14.3).

    async function load() {
      const [res] = await Promise.all([
        admin.waitForResponse(
          (r) =>
            r.url().endsWith('/api/admin/connectors/jellyfin') && r.request().method() === 'GET'
        ),
        admin.goto('/admin/connectors')
      ]);
      return res.json();
    }

    const live = await load();
    expect(live.secrets_unreadable, 'the stack under test has intact custody').toBe(false);
    // A mapping row means `cfg` was assigned, so the absence below is not a page still loading.
    await expect(admin.locator('tr[data-user]').first()).toBeVisible();
    // The Jellyfin card only: other cards have Saves, and names match as substrings.
    const card = admin.getByTestId('connector-jellyfin');
    await expect(card.locator('[data-secrets="unreadable"]')).toHaveCount(0);

    // The shape the backend really produces: an unreadable row degrades to the URL alone, with
    // `configured` false.
    await admin.route('**/api/admin/connectors/jellyfin', (route) =>
      route.fulfill({
        json: {
          url: live.url,
          has_api_key: false,
          configured: false,
          library_ids: [],
          linked_users: 0,
          secrets_unreadable: true
        }
      })
    );
    try {
      const degraded = await load();
      expect(degraded.secrets_unreadable).toBe(true);
      const warning = card.locator('[data-secrets="unreadable"]');
      await expect(warning).toBeVisible();
      await expect(warning).toContainText('.env');
      await expect(warning).toContainText('spielplan-secrets reset');
      // Clause by clause: the repair is partial, the old key is retired, and web push goes with it.
      await expect(warning).toContainText(/fixes Jellyfin\s+only/);
      await expect(warning).toContainText('the old key is retired');
      await expect(warning).toContainText('web push');
      // Save stays reachable although `configured: false` disables Test and Sync.
      await expect(card.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
    } finally {
      await admin.unroute('**/api/admin/connectors/jellyfin');
    }
  });
});
