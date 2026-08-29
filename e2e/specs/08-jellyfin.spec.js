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
 * §7.3, end to end in a browser, against a Jellyfin that actually answers.
 *
 * M1's exit criterion in §12 is "seen states flow both ways for both users", and both
 * directions are here: a flag set in Jellyfin arriving in the app, and a tap in the app
 * arriving in Jellyfin. The fake refuses the admin API key on `/UserPlayedItems`, so the second
 * direction also proves §7.3's least-privilege path rather than merely exercising it.
 *
 * Serial, and one page: the connector is stateful and each step builds on the last.
 */
test.describe.configure({ mode: 'serial' });

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
    await page.goto('/admin/connectors');
    await expect(page.getByRole('heading', { name: 'Connectors' })).toBeVisible();

    await page.getByLabel('SERVER URL').fill(JELLYFIN.url);
    await page.getByLabel('API KEY').fill(JELLYFIN.apiKey);
    await page.getByRole('button', { name: 'Save' }).click();

    await page.getByRole('button', { name: 'Test connection' }).click();
    const probe = page.locator('[data-probe]');
    await expect(probe).toHaveAttribute('data-probe', 'ok');
    await expect(probe).toContainText('Fake Jellyfin');
  });

  test('the api key never comes back out of the form', async () => {
    // §14.3: the key is admin-equivalent on the whole media server, so the form shows a mask
    // and the page never receives the value.
    await page.reload();
    await expect(page.getByLabel('API KEY')).toHaveAttribute('placeholder', /stored/);
    await expect(page.getByLabel('API KEY')).toHaveValue('');
    expect(await page.content()).not.toContain(JELLYFIN.apiKey);
  });

  test('an account links to one jellyfin user, with that user own sign-in', async () => {
    const row = page.locator('tr[data-user]').first();
    await row.getByRole('combobox').selectOption({ label: 'patrick' });
    await row.getByPlaceholder('jellyfin username').fill('patrick');
    await row.getByPlaceholder('password (once)').fill(JELLYFIN.password);
    await row.getByRole('button', { name: 'Link' }).click();

    await expect(row.locator('[data-link-state]')).toHaveAttribute('data-link-state', 'linked');
    await expect(row).toContainText('token stored');
  });

  test('a flag set in jellyfin arrives in the app', async () => {
    await markPlayedInJellyfin(page.request, JELLYFIN.item.heat);

    await page.goto('/admin/connectors');
    await page.getByRole('button', { name: 'Sync now' }).click();
    await expect(page.locator('[data-sync="done"]')).toBeVisible();

    const panel = await openTitle(page, 'Heat');
    await expect(panel.getByRole('button', { name: 'Seen', exact: true })).toHaveAttribute(
      'data-seen',
      'seen'
    );
  });

  test('a tap in the app arrives in jellyfin, under the per-user token', async () => {
    const panel = await openTitle(page, 'Heat');
    await panel.getByRole('button', { name: 'Seen', exact: true }).click();

    await expect(panel.getByRole('button', { name: 'Mark seen' })).toBeVisible();
    await expect(panel.locator('.syncnote')).toHaveText('synced to Jellyfin');

    const state = await jellyfinState(page.request);
    expect(state.played[JELLYFIN.user.patrick]).not.toContain(JELLYFIN.item.heat);
    // The fake refuses the admin key on this route, so a write that happened at all is proof
    // the per-user token was used (§7.3, §14.3).
    expect(state.writes.at(-1)).toEqual({
      user: JELLYFIN.user.patrick,
      item: JELLYFIN.item.heat,
      played: false
    });
  });

  test('a second sync reads back our own write and changes nothing', async () => {
    // The loop guard (§7.3: "jf_synced_at prevents loops"), visible from outside.
    const before = (await jellyfinState(page.request)).writes.length;
    await page.goto('/admin/connectors');
    await page.getByRole('button', { name: 'Sync now' }).click();
    await expect(page.locator('[data-sync="done"]')).toBeVisible();

    const after = await jellyfinState(page.request);
    expect(after.writes.length).toBe(before);

    const panel = await openTitle(page, 'Heat');
    await expect(panel.getByRole('button', { name: 'Mark seen' })).toBeVisible();
  });

  test('finishing a title arms a prompt that surfaces on the next app open', async () => {
    // §7.3: ">= 90% playback … arms a per-user prompt", and with push undeliverable the prompt
    // "queues and surfaces as an in-app banner on next open. The banner path is the whole M1
    // behaviour."
    await playInJellyfin(page.request, JELLYFIN.item.severance, 0.96);
    const polled = await page.request.post('/api/admin/connectors/jellyfin/poll');
    expect(polled.ok()).toBeTruthy();
    expect((await polled.json()).armed).toBe(1);

    await page.goto('/');
    const prompt = page.locator('[data-finish-prompt]');
    await expect(prompt).toBeVisible();
    await expect(prompt).toContainText('Did you finish Severance?');
    // Exactly one title, not a list — that is what separates it from §6.0's pending-verdicts
    // banner (decision-doc proposal 150).
    await expect(prompt).toHaveCount(1);
  });

  test('the prompt marks nothing until it is tapped', async () => {
    const panel = await openTitle(page, 'Severance');
    await expect(panel.getByRole('button', { name: 'Mark seen' })).toBeVisible();
  });

  test('the first tap writes seen, and the card does not come back', async () => {
    await page.goto('/');
    const prompt = page.locator('[data-finish-prompt]');
    await expect(prompt).toBeVisible();
    await prompt.getByRole('button', { name: 'Yes — mark it seen' }).click();
    await expect(prompt).toHaveCount(0);

    const panel = await openTitle(page, 'Severance');
    await expect(panel.getByRole('button', { name: 'Seen', exact: true })).toHaveAttribute(
      'data-seen',
      'seen'
    );

    await page.goto('/');
    await expect(page.locator('[data-finish-prompt]')).toHaveCount(0);
  });

  test('repeated polls of one viewing do not re-arm it', async () => {
    // The poll runs every minute and a film sits above 90% for its last ten.
    for (let i = 0; i < 3; i++) {
      const res = await page.request.post('/api/admin/connectors/jellyfin/poll');
      expect((await res.json()).armed).toBe(0);
    }
    await page.goto('/');
    await expect(page.locator('[data-finish-prompt]')).toHaveCount(0);
  });

  test('the account page reports the link', async () => {
    await page.goto('/account');
    await expect(page.locator('[data-jellyfin="linked"]')).toBeVisible();
  });

  test('unlinking leaves a working account', async () => {
    // §3.3: the link is optional; removing it must break nothing.
    await page.goto('/admin/connectors');
    await page.locator('tr[data-user]').first().getByRole('button', { name: 'Unlink' }).click();
    await expect(page.locator('tr[data-user]').first().getByRole('combobox')).toBeVisible();

    await page.goto('/account');
    await expect(page.locator('[data-jellyfin="unlinked"]')).toBeVisible();

    const panel = await openTitle(page, 'Heat');
    await panel.getByRole('button', { name: 'Mark seen' }).click();
    await expect(panel.getByRole('button', { name: 'Seen', exact: true })).toBeVisible();
  });
});
