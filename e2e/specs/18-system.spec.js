import { expect, test } from '@playwright/test';

import { JELLYFIN, signedIn } from '../helpers.js';

/**
 * Admin > System, end to end. Spec v2.1 §6.6, §2 (Backups, Configuration), §14.3;
 * decisions 181, 182, 454.
 *
 * This file is the reader half of M4.7. The milestone gives `job_run` its rows and secrets
 * custody a readable state, and until this card existed nothing read either: "did last night's
 * dump happen" was a question only `psql` could answer, on the install least likely to have
 * anyone able to ask it. §6.6 names five things for this card; decision 182 shipped three and
 * left queue depth, last syncs and logs to M5, and M5.7's decision 454 ships those as three more
 * keys - the acquisition queue by state, each connector job's last SUCCESSFUL run, and the web
 * process's own recent log lines. The card is still read-only, with one control that narrows
 * what was already read, so the assertions below are as much about what is NOT here - a key
 * past the six, a control that writes, a filter that asks the server anything - as about what
 * is. Six keys failed this file's three-key assertion on the day they landed, by design (plan
 * E3): the FACTS array and the coverage row moved together, with no waiver.
 *
 * A browser test rather than a `curl` transcript because the claim is about a surface: a route
 * that returns a fingerprint proves nothing about whether an operator can find it, and the
 * three facts are worth nothing separately from the tab that leads to them. `test_api_gating`
 * sweeps the route's role gate and `test_admin_system.py` proves the fingerprint is a
 * fingerprint against a key that test chose; neither can say the card is reachable.
 *
 * Every DOM assertion below reads the response THE PAGE RENDERED FROM, not a second call made
 * beside it. The worker fires jobs every sixty seconds, so a card rendered at :07 and a payload
 * fetched at :09 legitimately disagree about a row's outcome — and a suite that reports that
 * disagreement as a defect is worse than one that does not look.
 *
 * ONE PAGE FOR THE FILE. Playwright hands each test a fresh context and this file's admin
 * session is worth keeping across every read below. Desktop only, for the reason 15-tonight-group
 * and 17-users are: the phone project exists for the one-handed gestures §6's preamble is
 * about, and this surface is read-only — it has no gesture to be primary about. Its one control
 * is measured against the 48 px floor on the phone by 21-connectors, which walks both admin
 * pages on that project (M5.7's plan §7 check 14).
 *
 * The last test is the Connectors card and not this one, on purpose. Custody has two warnings
 * and they are one thing: this card says that some sealed row will not open, that card is where
 * the repair is offered and is the only place in the product that says what the repair costs
 * the secrets it is not about. Both were written and neither was read; the second stays beside
 * the first here rather than in 08-jellyfin, whose subject is a connector that answers and
 * whose configuration every later spec inherits.
 */
test.describe.configure({ mode: 'serial' });

/** Decision 182's three facts and decision 454's three, sorted as the route's keys are compared,
 *  and the fact that there are six. */
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

  /** Load the card and return the very payload it rendered from. */
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

    // "and no more" is the half a screenshot cannot check: §6.6's five things are now decision
    // 182's three and decision 454's three, and a seventh would be a claim this card never made.
    // Asserted on the route's own keys, because a new fact would arrive there before it arrived
    // on the page.
    const body = await (await admin.request.get('/api/admin/system')).json();
    expect(Object.keys(body).sort()).toEqual(FACTS);

    // Read-only, asserted inside the six facts rather than over the whole document - the shell's
    // nav rail and account chip are buttons, and they are not this card's. Rotation is
    // `spielplan-secrets`, the dump is the worker's and draining the queue is the board's (plan
    // E5); §2 makes the first two the operator's. The one control inside the facts is the log
    // level filter, so the count is one and that one is the logs section's select.
    const controls = admin
      .locator(FACTS.map((f) => `[data-testid="system-${f}"]`).join(', '))
      .locator('button, input, select, [role=button]');
    await expect(controls).toHaveCount(1);
    const level = admin.getByTestId('system-logs').getByLabel('LOG LEVEL');
    await expect(admin.getByTestId('system-logs').locator('select')).toHaveCount(1);
    await expect(level).toBeVisible();

    // And that control writes nothing and asks nothing: it narrows the lines the one read already
    // returned (decision 454). Watched from before the first change, and closed by a round trip
    // to the same server, so a request the filter fired has had the time to leave the page. The
    // round trip is `admin.request`, which is not the page's network and is not seen here.
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
    // The whole field list, not a subset: §14.3 makes the stored connector credential
    // admin-equivalent and SECRETS_KEY is what opens it, so the assertion that matters is that
    // no fifth field could be carrying key material to this browser.
    expect(Object.keys(secrets).sort()).toEqual([
      'configured',
      'fingerprint',
      'key_id',
      'unreadable'
    ]);
    expect(secrets.configured, 'the e2e stack sets SECRETS_KEY').toBe(true);
    // Twelve hex characters is what `core/secrets.key_fingerprint` promises: enough to compare
    // this install against the `.env` beside the dumps, no use for anything else.
    expect(secrets.fingerprint).toMatch(/^[0-9a-f]{12}$/);
    // §2's first boot mints the DEK, so an install that has ever had a SECRETS_KEY has a row
    // for the fingerprint to be about.
    expect(typeof secrets.key_id, 'the active data-encryption key row').toBe('string');

    const card = admin.getByTestId('system-secrets');
    await expect(card).toContainText(secrets.fingerprint);
    await expect(card).toContainText(secrets.key_id);
    // A restored dump under the wrong key is what the warning exists for; on a stack whose
    // `.env` never moved it must not be claiming a problem.
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
      // The state a stack is in before its first anchored night, and the state an install whose
      // dump has failed since Tuesday is in. They are one problem to whoever needs a backup, so
      // the card says the same thing about both.
      await expect(card).toContainText('no dump has ever completed');
      expect(backup.stale, 'never having dumped is stale by the same rule').toBe(true);
    } else {
      // The age and not the timestamp: the rendered time is formatted by the browser's locale
      // and this process's is not necessarily the same one, but "N hours ago" is arithmetic.
      await expect(card).not.toContainText('no dump has ever completed');
      await expect(card).toContainText(/ago/);
    }
    // Either way the warning is the route's own `stale` bit rendered, not a second opinion.
    await expect(admin.getByTestId('system-backup-stale')).toHaveCount(backup.stale ? 1 : 0);
  });

  // --- 4 ------------------------------------------------------------------------------------

  test('every job the worker has recorded is listed with its outcome', async () => {
    // The worker writes a `job_run` row per fired job and §7.3's poll fires every minute, so by
    // this point in the suite there are rows — but the poll below is what keeps this reporting
    // "the worker has recorded nothing" rather than "the list is empty", which are different
    // failures with different owners.
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
      // The row is opened before the job runs, so a missing `finished_at` is its own fact — a
      // kill, an OOM or a power cut — and not an outcome the page may guess at.
      const expected = job.finished_at === null ? 'unfinished' : job.ok ? 'ok' : 'failed';
      await expect(row).toHaveAttribute('data-outcome', expected);
    }
    // §5.3's registry is what fires them, and the minute-by-minute one is the row that proves
    // this card is reading a live worker rather than a table somebody seeded.
    expect(jobs.map((j) => j.name)).toContain('jellyfin-sessions-poll');
  });

  // --- 5 ------------------------------------------------------------------------------------

  test('the queue depth is reported by state and kind', async () => {
    // Decision 454's first key: `acquire.queue.stats` per kind and state, and the per-state totals
    // beside it. All five states arrive, zeros included, so "nothing failed" is a 0 on the card
    // rather than an absence - and an absence is what an operator cannot tell from a card that
    // never asked.
    const { queue } = await render();
    const states = ['pending', 'leased', 'done', 'failed', 'skipped'];
    expect(Object.keys(queue.by_state).sort()).toEqual([...states].sort());

    const card = admin.getByTestId('system-queue');
    for (const state of states) {
      // Word-bounded, because "pending 1" is a substring of "pending 10".
      await expect(card).toContainText(new RegExp(`\\b${state} ${queue.by_state[state]}\\b`));
      // The totals are the rows' sum, not a second count that could drift from them.
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
    // A read, and nothing beside it drains or retries: that is the board's (plan E5).
    await expect(card.locator('button, input, select, [role=button]')).toHaveCount(0);
  });

  // --- 6 ------------------------------------------------------------------------------------

  test('the last successful sync of each connector is listed', async () => {
    // Decision 454's second key, and the question `jobs` cannot answer: its row is the newest
    // ATTEMPT, which on the install whose sync has failed since Tuesday is the failure. Every job
    // that talks to a connector is listed whether or not it ever succeeded, because a job left
    // off the list reads as one that does not exist.
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
      // A job whose newest row reached its server has a last sync no older than that row. Not
      // equal: the two are separate reads, and the minute poll can finish between them. A row
      // closed ok that asked nobody is not a sync -- no report (the drain with nothing leased, the
      // sweep with nothing ripe) or a report saying the server never answered. [M57-JFSYS-01]
      const newest = jobs.find((job) => job.name === sync.name);
      if (newest?.ok === true && newest.detail != null && newest.detail.reached !== false) {
        expect(sync.at, `${sync.name} succeeded and is listed as never`).not.toBeNull();
        expect(Date.parse(sync.at)).toBeGreaterThanOrEqual(Date.parse(newest.finished_at));
      }
    }
  });

  // --- 7 ------------------------------------------------------------------------------------

  test('the recent log lines are listed and filter by level', async () => {
    // Decision 454's third key: the web process's own `spielplan` lines since it started, at INFO
    // and above, at most 200, redacted before they were kept (`core/logs.py`). The worker's lines
    // stay in its container log, and the card says so rather than implying a log it lacks.
    const { logs } = await render();
    expect(logs.scope).toBe('web process');
    expect(logs.records.length).toBeLessThanOrEqual(200);
    for (const record of logs.records) {
      expect(Object.keys(record).sort()).toEqual(['at', 'level', 'logger', 'message']);
      // Never httpx or uvicorn: the first is where a query-string key would have been written.
      expect(record.logger === 'spielplan' || record.logger.startsWith('spielplan.')).toBe(true);
    }
    // 08-jellyfin put this key into the connector, and every sweep since has sent it.
    expect(JSON.stringify(logs)).not.toContain(JELLYFIN.apiKey);

    const card = admin.getByTestId('system-logs');
    await expect(card).toContainText('web process');
    await expect(card).toContainText('container log');

    // Python's level numbers, which are what `core/logs` records the names of.
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
    // The `request` fixture is a separate context with no cookies, which is what "anonymous"
    // has to mean here. Role gating across every admin route is `test_api_gating.py`'s sweep;
    // this is the one route this milestone added, asked at the edge.
    expect((await request.get('/api/admin/system')).status()).toBe(401);

    // sec-12: both renderers and the schema were registered before the SPA catch-all and behind
    // no auth, and the document enumerates every admin and setup route with its body shapes —
    // the reconnaissance step for every other finding in this milestone.
    expect((await request.get('/api/docs')).status()).toBe(404);

    // `/openapi.json` and `/redoc` sit outside the `/api` namespace, so the SPA catch-all
    // answers them with the app shell rather than with 404 — `app.py`'s `SpaFallback` declines
    // only `/api`. The property sec-12 is about is not the status code but that no schema comes
    // back, so that is what is asserted: none of the document's own top-level keys.
    for (const path of ['/openapi.json', '/redoc']) {
      const body = await (await request.get(path)).text();
      expect(body, `${path} served the OpenAPI document`).not.toContain('"openapi"');
      expect(body, `${path} served the OpenAPI document`).not.toContain('"paths"');
    }
  });

  // --- 9 ------------------------------------------------------------------------------------

  test('custody says so when SECRETS_KEY no longer opens every stored secret', async () => {
    // Fact 2's other half, and the half a healthy stack cannot reach. `unreadable` turns true
    // when some sealed row is sealed under a key this install no longer holds; the e2e `.env`
    // is the one every ciphertext here was written under, no gesture in the product breaks
    // custody, and SECRETS_KEY cannot change mid-suite. So the seam is the response itself —
    // the live payload with one boolean flipped, handed back to the page that asked for it.
    //
    // That the ROUTE turns it true is proven where it can be: `test_admin_system.py`'s
    // `test_a_wrong_secrets_key_is_reported_rather_than_raised`, and the repair case beside it,
    // both against a key those tests chose. What had no test anywhere was this half. The whole
    // `{#if card.secrets.unreadable}` block could be deleted and every assertion in this file,
    // in `test_admin_system.py` and in `test_secrets_custody.py` stayed green — a warning that
    // is never rendered is the one thing worse than no warning, because the card then reports
    // healthy custody on the install whose custody is broken.
    const live = await (await admin.request.get('/api/admin/system')).json();
    expect(live.secrets.unreadable, 'the stack under test has intact custody').toBe(false);

    await admin.route('**/api/admin/system', (route) =>
      route.fulfill({ json: { ...live, secrets: { ...live.secrets, unreadable: true } } })
    );
    try {
      // Still the response the page rendered from, which is this file's rule — it is only this
      // test that decides what that response says.
      const { secrets } = await render();
      expect(secrets.unreadable).toBe(true);

      const warning = admin.getByTestId('system-secrets-unreadable');
      await expect(warning).toBeVisible();
      // Both ways back, named in the warning itself: the `.env` that was current when the dump
      // was taken, or the operator command that retires what will not open. §2 makes both the
      // operator's, and a warning that says only "something is wrong" leaves them where it
      // found them — on the screen they reached because something was wrong.
      await expect(warning).toContainText('.env');
      await expect(warning).toContainText('spielplan-secrets reset');
      // And the fact stays a fact underneath the warning: the fingerprint is what the operator
      // compares against the `.env` beside the dumps, which is the next thing they do.
      await expect(admin.getByTestId('system-secrets')).toContainText(live.secrets.fingerprint);
    } finally {
      await admin.unroute('**/api/admin/system');
    }
  });

  // --- 10 -----------------------------------------------------------------------------------

  test('the Connectors card says what its own custody repair costs', async () => {
    // The test above's twin, and it had exactly the same hole: every `secrets_unreadable`
    // assertion in the repository is against the route's JSON, so `{#if cfg?.secrets_unreadable}`
    // and the paragraph under it could be deleted with `pytest backend/tests`, `npm --prefix
    // frontend test` and all eighteen spec files still green. This is the worse place for that
    // hole to be. The System card only reports the state; this card is where the repair is
    // offered, and its paragraph is the only statement anywhere in the product that Save fixes
    // Jellyfin *by retiring the DEK it cannot open* — which strands web push and every other
    // secret sealed under that key (§14.3, M4.7 dd03). An admin who is shown "not configured"
    // instead pastes the key, repairs one connector and is never told what it cost.

    /** The Connectors card, and the very payload it rendered from — this file's rule. */
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
    // An absence is only worth asserting once the page has something to be absent from, and
    // `toHaveCount(0)` is happiest against a page that has not finished asking. The mapping
    // table is the anchor: `refresh()` assigns `cfg` before it awaits `/admin/users`, so a row
    // on screen means the branch above was evaluated and declined.
    await expect(admin.locator('tr[data-user]').first()).toBeVisible();
    // The Jellyfin card and not the page: M5.7 put six more cards beside it, each with a Save
    // of its own, and Playwright matches a name as a substring (decision 455).
    const card = admin.getByTestId('connector-jellyfin');
    await expect(card.locator('[data-secrets="unreadable"]')).toHaveCount(0);

    // Not the live payload with one boolean flipped, which is what the test above does: this
    // route cannot answer that shape. `registry.load_jellyfin` degrades an unreadable row to
    // the stored URL and nothing else — no key, no libraries, no tokens — so `configured` is
    // false and every caller takes its "Jellyfin is not set up" path with a different reason
    // attached. A payload that kept `configured` true would put the page in a state the backend
    // never produces, and `configured` is what decides which controls this card offers.
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
      // The two ways back, named the same way the System card names them: the `.env` that was
      // current when the dump was taken, or the command that retires what will not open.
      await expect(warning).toContainText('.env');
      await expect(warning).toContainText('spielplan-secrets reset');
      // And the sentence that exists only here, asserted clause by clause because each one is a
      // separate fact about the Save button six lines below it: the repair is partial, the key
      // it needs is retired to get it, and what that retires is named rather than left as "other
      // secrets" — §2 seals the VAPID keypair under the same DEK, and a household whose push
      // silently degrades to §6's in-app banner is owed the sentence that predicted it.
      await expect(warning).toContainText(/fixes Jellyfin\s+only/);
      await expect(warning).toContainText('the old key is retired');
      await expect(warning).toContainText('web push');
      // The repair the paragraph points at has to be reachable from the state that needs it.
      // An unreadable row reads as `configured: false`, which is what disables Test and Sync;
      // a Save gated on the same bit would leave the advice with nothing to act on.
      await expect(card.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
    } finally {
      await admin.unroute('**/api/admin/connectors/jellyfin');
    }
  });
});
