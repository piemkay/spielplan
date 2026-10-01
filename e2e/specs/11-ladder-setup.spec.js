import { expect, test } from '@playwright/test';

import {
  JELLYFIN,
  createMember,
  markPlayedInJellyfin,
  signInAsMember,
  signedIn
} from '../helpers.js';

/**
 * §6.1's set-up, once (decision 547), through the screens: a member with no ladder finds Rate
 * closed and Home asking, steps from the best down, finds a film by name and finishes; the ladder
 * it made replaces Home's notice. A fresh member per project, since the set-up is once and the
 * phone pass runs on the stack the desktop pass used. The fixture holds six films, so each step's
 * list ends on its first page.
 */
test.describe.configure({ mode: 'serial' });

test.describe('the ladder set-up', () => {
  /** The admin's own page, for the household and Jellyfin. */
  let admin;
  let member;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    admin = await browser.newPage({ baseURL });
    await signedIn(admin);
    member = await createMember(admin, `ladder-${testInfo.project.name}`);
    // A link without a sign-in reads Played; 08-jellyfin left both fake users free.
    const linked = await admin.request.post(`/api/admin/users/${member.id}/jellyfin`, {
      data: { jellyfin_user_id: JELLYFIN.user.patrick }
    });
    expect(linked.ok(), `linking the member to Jellyfin (§3.3): ${linked.status()}`).toBeTruthy();
    await markPlayedInJellyfin(admin.request, JELLYFIN.item.heat);
    // The worker's own sweep holds the lock now and then, and a press meeting it does nothing.
    let swept = null;
    for (let attempt = 0; attempt < 5 && !swept?.users?.includes(member.name); attempt++) {
      const res = await admin.request.post('/api/admin/connectors/jellyfin/sync');
      expect(res.ok(), `Sync now (§7.3): ${res.status()}`).toBeTruthy();
      swept = await res.json();
    }
    expect(swept.users, 'the sweep never reached the member it was linked for').toContain(member.name);
  });

  test.afterAll(async () => {
    // Put back what 08-jellyfin left: both fake users free, Heat not played.
    if (member?.id) await admin?.request.delete(`/api/admin/users/${member.id}/jellyfin`);
    if (admin) await markPlayedInJellyfin(admin.request, JELLYFIN.item.heat, false);
    await admin?.close();
  });

  test('a member with no ladder sets it up step by step and lands on the ladder it made', async ({
    page
  }) => {
    await signInAsMember(page, member);

    await test.step('Rate is one closed card, with nothing earlier to keep', async () => {
      await page.goto('/rate');
      const card = page.getByTestId('rate-before-setup');
      await expect(card).toContainText('Rate on your own ladder.');
      await expect(card).not.toContainText('earlier ratings');
      await expect(page.getByTestId('rate-card')).toHaveCount(0);
    });

    await test.step('Home asks for the set-up over its shelves', async () => {
      await page.goto('/');
      await expect(page.getByTestId('home-setup-notice')).toContainText('Set up your ladder.');
    });

    const film = (name) => page.locator(`[data-testid="setup-film"][aria-label="${name}"]`);

    await test.step('the first step opens on the best-rated films, with Watched marks', async () => {
      await page.goto('/rate');
      await page.getByTestId('rate-setup-cta').click();
      await expect(page).toHaveURL(/\/rate\/setup$/);
      await expect(page.getByTestId('setup-flow')).toContainText('Step 1 of 7');
      await expect(page.getByTestId('setup-step')).toHaveText('All-time favourite');
      // Heat alone is well-known and scored: it leads the list.
      await expect(page.getByTestId('setup-film').first()).toHaveAccessibleName('Heat');
      await expect(page.getByTestId('setup-film')).toHaveCount(6);
      await expect(page.getByTestId('setup-end')).toHaveText(
        "That's the end of the list. Search finds any other film."
      );
      await expect(page.getByTestId('setup-more')).toHaveCount(0);
      // Played in Jellyfin, swept in: the mark is the member's own.
      await expect(film('Heat').getByTestId('setup-watched')).toBeVisible();
      await expect(film('Prisoners').getByTestId('setup-watched')).toHaveCount(0);
      await expect(page.getByTestId('setup-undo')).toBeDisabled();
      await expect(page.getByTestId('setup-next')).toHaveText('None for All-time favourite');
    });

    await test.step('a pick is pressed, and leaves the next step for its strip', async () => {
      await film('Heat').click();
      await expect(film('Heat')).toHaveAttribute('aria-pressed', 'true');
      await page.getByTestId('setup-next').click();
      await expect(page.getByTestId('setup-step')).toHaveText('Loved it');
      await expect(page.getByTestId('setup-strip')).toContainText('Not quite these');
      await expect(page.getByTestId('setup-film')).toHaveCount(5);
      await expect(film('Heat')).toHaveCount(0);
    });

    await test.step('a search finds a film by name and puts it on this step', async () => {
      await page.getByTestId('setup-find').click();
      await page.getByTestId('setup-search').fill('Prison');
      const found = page.getByTestId('setup-hit').first();
      await expect(found).toHaveAccessibleName(/^Prisoners/);
      await found.click();
      await expect(page.getByTestId('setup-film').first()).toHaveAccessibleName('Prisoners');
      await expect(film('Prisoners')).toHaveAttribute('aria-pressed', 'true');
    });

    await test.step('the empty steps pass, and Finish is the last', async () => {
      for (const word of ['Liked it', 'It was fine', 'Not really for me', "Didn't like it"]) {
        await page.getByTestId('setup-next').click();
        await expect(page.getByTestId('setup-step')).toHaveText(word);
        await expect(page.getByTestId('setup-next')).toHaveText(`None for ${word}`);
      }
      await page.getByTestId('setup-next').click();
      await expect(page.getByTestId('setup-flow')).toContainText('Step 7 of 7');
      await expect(page.getByTestId('setup-next')).toHaveCount(0);
      await page.getByTestId('setup-finish').click();
    });

    await test.step('the done screen shows the ladder, a step to a row', async () => {
      const done = page.getByTestId('setup-done');
      await expect(done.getByRole('heading', { name: 'Your ladder is ready' })).toBeVisible();
      await expect(done).toContainText('2 films are on it.');
      const rungs = done.getByRole('listitem');
      await expect(rungs).toHaveCount(7);
      await expect(rungs.nth(0)).toContainText('All-time favourite');
      await expect(rungs.nth(0)).toContainText('1 film');
      await expect(rungs.nth(1)).toContainText('Loved it');
      await expect(rungs.nth(1)).toContainText('1 film');
      await expect(rungs.nth(2)).not.toContainText('film');
      // Nothing rated before, so nothing kept as history.
      await expect(done).not.toContainText('earlier ratings');
      await expect(page.getByRole('link', { name: 'Start rating' })).toHaveAttribute('href', '/rate');
    });

    await test.step('Done goes Home, and the notice is gone for them', async () => {
      const landed = page.waitForResponse((res) => res.url().includes('/api/home?'));
      await page.getByRole('link', { name: 'Done', exact: true }).click();
      await landed;
      await expect(page.getByTestId('home-title')).toBeVisible();
      await expect(page.getByTestId('home-setup-notice')).toHaveCount(0);
      const state = await (await page.request.get('/api/ladder/setup')).json();
      expect(state.done, 'the set-up is the cut-over, recorded once').toBe(true);
    });
  });
});
