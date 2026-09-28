import { expect, test } from '@playwright/test';

import {
  JELLYFIN,
  jellyfinState,
  markPlayedInJellyfin,
  openTitle,
  playInJellyfin,
  resetJellyfin,
  signedIn
} from '../helpers.js';

/**
 * §7.3 end to end against a Jellyfin that answers: seen state flows both ways (§12's M1 exit).
 * The fake refuses the admin key on the Played write, so the app-to-Jellyfin direction proves the
 * per-user token. Serial, one page: each step builds on the last.
 */
test.describe.configure({ mode: 'serial' });

/** The Jellyfin section of Services, whose sheets open inside it. */
const jellyfinCard = (page) => page.getByTestId('connector-jellyfin');

/** Open the sheet behind one of the Jellyfin rows, the way a tap does. */
async function openRow(page, name) {
  await jellyfinCard(page).getByRole('button', { name: new RegExp(`^${name}`) }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
}

/** The mapping lives in the People linked sheet; each account is one `[data-user]` block. */
async function openPeople(page) {
  await page.goto('/admin/services');
  await openRow(page, 'People linked');
  return page.locator('[data-user]').first();
}

/**
 * Press Sync now and return the result line. Retried: the worker's own sweep holds an advisory
 * lock, and a press meeting it answers `already_running`. The fresh `goto` makes the line this
 * press's own.
 */
async function sweepFromTheCard(page, attempts = 3) {
  for (let attempt = 1; attempt <= attempts; attempt++) {
    await page.goto('/admin/services');
    const card = jellyfinCard(page);
    await card.getByRole('button', { name: 'Sync now' }).click();
    const line = card.locator('[data-sync]');
    await expect(line).toBeVisible();
    if ((await line.getAttribute('data-sync')) === 'done') return line;
  }
  throw new Error(
    `Sync now met a sweep already in progress ${attempts} presses running - worker wedged?`
  );
}

test.describe('jellyfin', () => {
  let page;

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await resetJellyfin(page.request);
    await signedIn(page);
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('the connector is configured and tested from the admin view', async () => {
    await page.goto('/admin/services');
    await expect(page.getByRole('heading', { name: 'Services', level: 1 })).toBeVisible();

    const card = jellyfinCard(page);
    await openRow(page, 'Server address');
    await card.getByLabel('Server address', { exact: true }).fill(JELLYFIN.url);
    await card.getByLabel('API key', { exact: true }).fill(JELLYFIN.apiKey);
    await card.getByRole('button', { name: 'Save', exact: true }).click();
    // Saved, the sheet closes on the page it was opened from.
    await expect(page.getByRole('dialog')).toHaveCount(0);

    await card.getByRole('button', { name: 'Test connection' }).click();
    const probe = card.locator('[data-probe]');
    await expect(probe).toHaveAttribute('data-probe', 'ok');
    await expect(probe).toContainText('Fake Jellyfin');
  });

  test('the api key never comes back out of the form', async () => {
    // §14.3: the key is admin-equivalent, so the page never receives it.
    await page.reload();
    await openRow(page, 'API key');
    const key = jellyfinCard(page).getByLabel('API key', { exact: true });
    await expect(key).toHaveAttribute('placeholder', 'Saved');
    await expect(key).toHaveValue('');
    expect(await page.content()).not.toContain(JELLYFIN.apiKey);
  });

  test('an account links to one jellyfin user, with that user own sign-in', async () => {
    const row = await openPeople(page);
    await row.getByRole('combobox').selectOption({ label: 'patrick' });
    await row.getByPlaceholder('Jellyfin username').fill('patrick');
    await row.getByPlaceholder('Jellyfin password, used once').fill(JELLYFIN.password);
    await row.getByRole('button', { name: 'Link', exact: true }).click();

    const link = row.locator('[data-link-state]');
    await expect(link).toHaveAttribute('data-link-state', 'linked');
    // The per-user token is stored, so Played writes never use the admin key (§7.3).
    await expect(link).toHaveAttribute('data-has-token', 'true');
  });

  test('a flag set in jellyfin arrives in the app', async () => {
    await markPlayedInJellyfin(page.request, JELLYFIN.item.heat);

    const swept = await sweepFromTheCard(page);
    // `ok` means nothing failed: all-zero counters also describe refused writes and an unreadable
    // library (§3.3), which the health attribute tells apart.
    await expect(swept).toHaveAttribute('data-sync-health', 'ok');

    const panel = await openTitle(page, 'Heat');
    await expect(panel.getByRole('button', { name: 'Watched', exact: true })).toHaveAttribute(
      'data-seen',
      'seen'
    );
  });

  test('a tap in the app arrives in jellyfin, under the per-user token', async () => {
    const panel = await openTitle(page, 'Heat');
    await panel.getByRole('button', { name: 'Watched', exact: true }).click();

    await expect(panel.getByRole('button', { name: 'Mark as watched' })).toBeVisible();
    await expect(panel.locator('.syncnote')).toHaveText('Saved, and Jellyfin is up to date.');

    const state = await jellyfinState(page.request);
    expect(state.played[JELLYFIN.user.patrick]).not.toContain(JELLYFIN.item.heat);
    // The fake refuses the admin key here, so any write proves the per-user token (§7.3).
    expect(state.writes.at(-1)).toEqual({
      user: JELLYFIN.user.patrick,
      item: JELLYFIN.item.heat,
      played: false
    });
  });

  test('a second sync reads back our own write and changes nothing', async () => {
    // The loop guard (§7.3: "jf_synced_at prevents loops"), visible from outside.
    const before = (await jellyfinState(page.request)).writes.length;
    const swept = await sweepFromTheCard(page);
    // Nothing owed and every write refused both leave the log alone; only this tells them apart.
    await expect(swept).toHaveAttribute('data-sync-health', 'ok');

    const after = await jellyfinState(page.request);
    expect(after.writes.length).toBe(before);

    const panel = await openTitle(page, 'Heat');
    await expect(panel.getByRole('button', { name: 'Mark as watched' })).toBeVisible();
  });

  test('finishing a title arms a prompt that surfaces on the next app open', async () => {
    // §7.3: ">= 90% playback … arms a per-user prompt". What plays is Severance's LAST episode, so
    // an empty `unresolved` and a card naming the SHOW prove the episode resolved to its series.
    await playInJellyfin(page.request, JELLYFIN.item.severance, 0.96);
    const polled = await page.request.post('/api/admin/connectors/jellyfin/poll');
    expect(polled.ok()).toBeTruthy();
    const report = await polled.json();
    expect(report.armed).toBe(1);
    expect(report.unresolved).toEqual([]);

    await page.goto('/');
    const prompt = page.locator('[data-finish-prompt]');
    await expect(prompt).toBeVisible();
    await expect(prompt).toContainText('Did you finish Severance?');
    // One title, not a list: that separates it from §6.0's pending-verdicts banner.
    await expect(prompt).toHaveCount(1);
  });

  test('the prompt marks nothing until it is tapped', async () => {
    const panel = await openTitle(page, 'Severance');
    await expect(panel.getByRole('button', { name: 'Mark as watched' })).toBeVisible();
  });

  test('the first tap writes seen, and the card does not come back', async () => {
    await page.goto('/');
    const prompt = page.locator('[data-finish-prompt]');
    await expect(prompt).toBeVisible();
    const titleId = await prompt.getAttribute('data-finish-prompt');
    // Read BEFORE the tap, from the route: the refresh below is only a fact if it was absent.
    const { banner } = await (await page.request.get('/api/home?kind=series')).json();
    expect((banner?.named ?? []).map((entry) => entry.name)).not.toContain('Severance');

    await prompt.getByRole('button', { name: 'Yes — mark it seen' }).click();
    await expect(prompt).toHaveCount(0);

    // Decision 212: the answer hands off to §6.1's queue with this title at its head, and the
    // pending banner is re-read. Both here, before any navigation.
    const handoff = page.locator(`[data-finish-handoff="${titleId}"]`);
    await expect(handoff).toBeVisible();
    await expect(handoff).toHaveAttribute('data-answer', 'seen');
    await expect(page.getByTestId('finish-prompt-cta')).toHaveAttribute(
      'href',
      `/rate?head=${titleId}`
    );
    // No reload: that would prove the route and not the wiring (`onAnswered`).
    await expect(page.getByTestId('pending-verdicts')).toContainText('Severance');
    await expect(page.getByTestId('pending-verdicts-count')).toHaveText(
      /^\d+ titles? you watched (is|are) waiting for your rating\.$/
    );

    const panel = await openTitle(page, 'Severance');
    await expect(panel.getByRole('button', { name: 'Watched', exact: true })).toHaveAttribute(
      'data-seen',
      'seen'
    );

    await page.goto('/');
    await expect(page.locator('[data-finish-prompt]')).toHaveCount(0);
  });

  test('repeated polls of one viewing do not re-arm it', async () => {
    for (let i = 0; i < 3; i++) {
      const res = await page.request.post('/api/admin/connectors/jellyfin/poll');
      expect((await res.json()).armed).toBe(0);
    }
    await page.goto('/');
    await expect(page.locator('[data-finish-prompt]')).toHaveCount(0);
  });

  test('a declined viewing stays declined, and only a new viewing asks again', async () => {
    // Decision 211: a decline writes an explicit `unseen`, which the sweep cannot adopt over, and
    // the viewing it closed stays closed. A second series and a cleared session list, so `armed`
    // counts this show alone.
    const BEAR = 'jf-7';
    const cleared = await page.request.post(`${JELLYFIN.control}/_test/sessions/clear`);
    expect(cleared.ok()).toBeTruthy();
    await playInJellyfin(page.request, BEAR, 0.96, 'sess-e2e-bear-1');

    const first = await (await page.request.post('/api/admin/connectors/jellyfin/poll')).json();
    // The worker's own poll may arm it first, so the sum is what is true of the viewing.
    expect(first.armed + first.already_armed).toBe(1);
    expect(first.unresolved).toEqual([]);

    await page.goto('/');
    const prompt = page.locator('[data-finish-prompt]');
    await expect(prompt).toContainText('Did you finish The Bear?');
    const titleId = await prompt.getAttribute('data-finish-prompt');
    await prompt.getByRole('button', { name: 'No — not seen' }).click();
    await expect(prompt).toHaveCount(0);

    const handoff = page.locator(`[data-finish-handoff="${titleId}"]`);
    await expect(handoff).toHaveAttribute('data-answer', 'unseen');
    // Nothing to rate after a "no", so no queue link.
    await expect(page.getByTestId('finish-prompt-cta')).toHaveCount(0);
    // Decision 210(a): un-marking a series is app-only, and the surface says so.
    await expect(handoff).toContainText(
      'Saved here only — Jellyfin keeps its own episode history.'
    );

    const state = await (await page.request.get(`/api/titles/${titleId}/state`)).json();
    expect(state.state, 'a declined prompt is an explicit action, not an absence').toBe('unseen');
    // An absent row also reads `unseen`; the timestamp says a row was written.
    expect(state.state_changed_at).not.toBeNull();
    // No DELETE on the Series folder: Jellyfin would reset every episode under it (decision 210(a)).
    const writes = (await jellyfinState(page.request)).writes;
    expect(writes.filter((write) => write.item === BEAR)).toEqual([]);

    // The credits keep running and the poll keeps reading the same session.
    for (let i = 0; i < 3; i++) {
      const again = await (await page.request.post('/api/admin/connectors/jellyfin/poll')).json();
      expect(again.armed).toBe(0);
    }
    await page.goto('/');
    await expect(page.locator('[data-finish-prompt]')).toHaveCount(0);

    // A different viewing is a different question: the guard is keyed on the session id.
    await playInJellyfin(page.request, BEAR, 0.96, 'sess-e2e-bear-2');
    const second = await (await page.request.post('/api/admin/connectors/jellyfin/poll')).json();
    // Each of the two sessions counted once.
    expect(second.armed + second.already_armed).toBe(2);

    await page.goto('/');
    const asked = page.locator('[data-finish-prompt]');
    await expect(asked).toContainText('Did you finish The Bear?');
    // Answered, so later specs open a Home with no question on it.
    await asked.getByRole('button', { name: 'No — not seen' }).click();
    await expect(asked).toHaveCount(0);
  });

  test('the account page reports the link', async () => {
    await page.goto('/account');
    await expect(page.locator('[data-jellyfin="linked"]')).toBeVisible();
  });

  test('unlinking leaves a working account', async () => {
    // §3.3: the link is optional; removing it must break nothing.
    const row = await openPeople(page);
    await row.getByRole('button', { name: 'Unlink', exact: true }).click();
    // It asks first: the saved sign-in goes with the link.
    await page.getByRole('menuitem', { name: 'Unlink', exact: true }).click();
    await expect(row.getByRole('combobox')).toBeVisible();

    await page.goto('/account');
    await expect(page.locator('[data-jellyfin="unlinked"]')).toBeVisible();

    const panel = await openTitle(page, 'Heat');
    await panel.getByRole('button', { name: 'Mark as watched' }).click();
    await expect(panel.getByRole('button', { name: 'Watched', exact: true })).toBeVisible();
  });

  test("an owned title wears the household's own poster, from this app's origin", async () => {
    // Decision 483. The fake serves a real PNG, so the image must decode, and the stack runs with
    // no egress, so no internet host is asked.
    const found = await page.request.get('/api/titles?kind=movie&q=Heat');
    const heat = (await found.json()).items.find((t) => t.name === 'Heat');
    const art = await page.request.get(`/api/art/${heat.id}/poster`);
    expect(art.status(), 'the owned title has art on the household server').toBe(200);
    expect(art.headers()['content-type']).toBe('image/png');
    expect(art.headers()['cache-control']).toBe('private, max-age=15552000');

    const panel = await openTitle(page, 'Heat');
    const img = panel.locator('[data-testid="rate-poster"] img');
    await expect(img).toHaveAttribute('src', `/api/art/${heat.id}/poster`);
    await expect
      .poll(() => img.evaluate((el) => el.complete && el.naturalWidth), {
        message: 'the title card drew a poster that did not decode'
      })
      .toBeGreaterThan(0);

    // And no image anywhere on the page names another origin: the art route is the only door.
    const origin = new URL(page.url()).origin;
    const sources = await page.locator('img').evaluateAll((els) => els.map((el) => el.src));
    expect(sources.filter((src) => new URL(src, origin).origin !== origin)).toEqual([]);
  });
});
