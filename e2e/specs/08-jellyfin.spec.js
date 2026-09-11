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
 * WHAT M4.11 CHANGED UNDER THESE NAMES, because four tests below now drive a different mechanism
 * than they did when they were written:
 *
 *   - **A television session is an EPISODE.** Jellyfin never plays a Series — `/Sessions` carries
 *     the episode's own id with a `SeriesId` for the folder — and `ops/fake_jellyfin.py` sends that
 *     shape now (the LAST episode of the series, because decision 210(c) makes only that a finish).
 *     So "finishing a title" below finishes an episode and the card it arms names the SHOW.
 *   - **"No" is an answer that writes** (decision 211): a decline is an explicit `unseen`, because
 *     the absent row was what the 15-minute sweep adopted Jellyfin's Played flag into.
 *   - **The answered card hands off** (decision 212): §7.3's "offers the verdict flow" is a link
 *     into §6.1's queue with that title at its head, and the banner it just filled is re-read.
 *   - **`data-sync` has a second value.** The sweep is serialised on an advisory lock, so the
 *     route can answer `already_running` and the card says so instead of printing counters —
 *     which is why every press of Sync now goes through `sweepFromTheCard` below.
 *
 * Serial, and one page: the connector is stateful and each step builds on the last.
 */
test.describe.configure({ mode: 'serial' });

/**
 * Press §7.3's sweep on §6.6's connector card and return the result line it printed.
 *
 * One press is no longer certain to be a sweep. §5.3 fires `jellyfin-seen-sync` every fifteen
 * minutes and M4.11 serialises the two callers on a session advisory lock, so a press that lands
 * while the worker holds it is answered `already_running` with every counter zero — a correct
 * answer, and a card that says so (`data-sync="already-running"`) rather than printing a result
 * nobody swept. A test that read that as the result line would fail for a reason that has nothing
 * to do with what it asserts, so the press is retried. The fresh `goto` is what makes the retry
 * honest: the result is component state, so the card carries no `[data-sync]` at all until this
 * press answers, and the attribute read below can only be this press's own.
 */
async function sweepFromTheCard(page, attempts = 3) {
  for (let attempt = 1; attempt <= attempts; attempt++) {
    await page.goto('/admin/connectors');
    await page.getByRole('button', { name: 'Sync now' }).click();
    const line = page.locator('[data-sync]');
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

    const swept = await sweepFromTheCard(page);
    // The other half of this sweep, which the card could not say before: nothing failed. It
    // printed "pushed 0 · adopted 0 · unchanged 87" while every Played write in a sweep was being
    // refused — a dead app -> Jellyfin direction reading as a quiet household — so a sweep that
    // owed nothing and lost nothing now states which of the two it was. [M4.11 finding 3]
    //
    // And `ok` now excludes a third state it used to cover, which is why this line is worth more
    // than it was: a sweep that could not READ the library at all. §3.3 makes that a degraded sync,
    // so `seen.sync_all` returns with every counter zero — `push_failed` included — and the card
    // printed the quiet-household line for a sweep in which neither direction ran. That is the
    // state this spec actually failed under, on the assertion below rather than on this one, with
    // the cause three files away (`e2e/reset.mjs` never restarted the mounted double).
    await expect(swept).toHaveAttribute('data-sync-health', 'ok');

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
    const swept = await sweepFromTheCard(page);
    // A sweep that owes nothing writes nothing, which is a different sentence from a sweep whose
    // writes were all refused: both leave the write log where it was, and only this attribute
    // tells them apart.
    await expect(swept).toHaveAttribute('data-sync-health', 'ok');

    const after = await jellyfinState(page.request);
    expect(after.writes.length).toBe(before);

    const panel = await openTitle(page, 'Heat');
    await expect(panel.getByRole('button', { name: 'Mark seen' })).toBeVisible();
  });

  test('finishing a title arms a prompt that surfaces on the next app open', async () => {
    // §7.3: ">= 90% playback … arms a per-user prompt", and with push undeliverable the prompt
    // "queues and surfaces as an in-app banner on next open. The banner path is the whole M1
    // behaviour."
    //
    // WHAT IS PLAYING IS AN EPISODE, and asking for a series by name is how this spec says so:
    // the fake answers `/Sessions` with Severance's LAST episode — its own `Id`, `Type: Episode`
    // and a `SeriesId` — which is the only shape a real server sends for television and the shape
    // `dd05-fake-and-e2e` records this double as having faked wrong. So `unresolved` being empty
    // is as load-bearing here as `armed` being 1: the app resolved `item_id` alone, so every
    // television session in the field was reported unresolved and §7.3's prompt never armed for
    // the Series partition at all. And the card naming the SHOW is what proves the episode
    // resolved to the show rather than to an episode §4.1 rule 5 gives the app no title for.
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
    // Exactly one title, not a list — that is what separates it from §6.0's pending-verdicts
    // banner (proposal 150, kept by decision 212).
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
    const titleId = await prompt.getAttribute('data-finish-prompt');
    // Read BEFORE the tap, because the refresh asserted below is only a fact if the banner did not
    // already say it: §6.0's population is "seen, and no verdict", and this show is neither yet.
    // Asked of the route rather than of the screen, because the screen's honest answer here is no
    // banner at all for a household with nothing pending, and "the element is missing" is a
    // different statement from "the population does not name this title".
    const before = await (await page.request.get('/api/home/pending-verdicts')).json();
    expect(before.named.map((entry) => entry.name)).not.toContain('Severance');

    await prompt.getByRole('button', { name: 'Yes — mark it seen' }).click();
    await expect(prompt).toHaveCount(0);

    // Decision 212, and both halves have to be asserted here or not at all: the question is over,
    // so a navigation takes the answer's own surface with it.
    //
    // §7.3 promises the tap "offers the verdict flow" and the card offered nothing — it posted,
    // dropped the row and left the person on Home. The link is §6.0's banner-CTA shape, entering
    // §6.1's queue with this title at its head, because "a prompt that names titles and then
    // presents a different one is worse than no prompt" (proposal 150).
    const handoff = page.locator(`[data-finish-handoff="${titleId}"]`);
    await expect(handoff).toBeVisible();
    await expect(handoff).toHaveAttribute('data-answer', 'seen');
    await expect(page.getByTestId('finish-prompt-cta')).toHaveAttribute(
      'href',
      `/rate?head=${titleId}`
    );
    // The second half: the banner below is the SERVER's sentence, so the only way to refresh a
    // population this answer just moved a title into is to ask again. Deliberately no reload — a
    // reload would prove the route and say nothing about the wiring, which is the thing that was
    // missing (`onAnswered`, `+page.svelte`).
    await expect(page.getByTestId('pending-verdicts')).toContainText('Severance');

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

  test('a declined viewing stays declined, and only a new viewing asks again', async () => {
    // Decision 211, in a browser. "No" used to write nothing at all, and nothing is what the
    // 15-minute sweep adopts Jellyfin's own Played flag into — so the state a person had just
    // declined arrived anyway inside the quarter hour — while the show sat above §7.3's threshold
    // for the rest of the credits and every poll armed a fresh card and sent a fresh push
    // (measured: two rows, two pushes). A decline is an explicit `unseen` now, and the viewing it
    // closed stays closed.
    //
    // A second series, and a cleared session list, because this test is arithmetic about one
    // show: Severance is still playing at 96% above and `armed` has to mean what it says. `jf-7`
    // is spelled here rather than added to `helpers.js`'s `JELLYFIN.item` — that constant names
    // the items its shared callers need, and this is the only caller that needs a third.
    const BEAR = 'jf-7';
    const cleared = await page.request.post(`${JELLYFIN.control}/_test/sessions/clear`);
    expect(cleared.ok()).toBeTruthy();
    await playInJellyfin(page.request, BEAR, 0.96, 'sess-e2e-bear-1');

    const first = await (await page.request.post('/api/admin/connectors/jellyfin/poll')).json();
    // One session, so exactly one arming event — counted as `armed` by whichever of the two
    // callers sees it first. §5.3's own one-minute poll runs beside this one and can be that
    // caller, in which case this press reports the same event as `already_armed`; the sum is what
    // is true of the viewing either way.
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
    // Nothing to rate after a "no", so decision 212's queue link is not offered: there is no
    // verdict to give a title nobody finished.
    await expect(page.getByTestId('finish-prompt-cta')).toHaveCount(0);
    // Decision 210(a) on the same line, and this is the only layer that can assert it: a series
    // is app-only in the un-marking direction, and the surface says so instead of claiming a push
    // that never happened.
    await expect(handoff).toContainText('series unseen is app-only');

    const state = await (await page.request.get(`/api/titles/${titleId}/state`)).json();
    expect(state.state, 'a declined prompt is an explicit action, not an absence').toBe('unseen');
    // The timestamp, and not the state alone: §4.2's default for an ABSENT row is `unseen` too,
    // and that absence is precisely what decision 211 replaces — so `state_changed_at` is the half
    // that says a row was written, which is the half the sweep can no longer adopt over.
    expect(state.state_changed_at).not.toBeNull();
    // And no DELETE against the Series folder, which is the write decision 210(a) refuses:
    // Jellyfin's `Folder.MarkUnplayed` resets `Played`, `PlayCount` and the position on every
    // recursive child, and §4.1 rule 5 leaves the app no episode identity to put any of it back.
    const writes = (await jellyfinState(page.request)).writes;
    expect(writes.filter((write) => write.item === BEAR)).toEqual([]);

    // The credits keep running and the poll keeps reading the same session.
    for (let i = 0; i < 3; i++) {
      const again = await (await page.request.post('/api/admin/connectors/jellyfin/poll')).json();
      expect(again.armed).toBe(0);
    }
    await page.goto('/');
    await expect(page.locator('[data-finish-prompt]')).toHaveCount(0);

    // A different viewing is a different question. The guard is keyed on the Jellyfin session id
    // (`0006_jellyfin.sql`) and not on (member, title): keyed the other way it would refuse every
    // later viewing from every device, for good.
    await playInJellyfin(page.request, BEAR, 0.96, 'sess-e2e-bear-2');
    const second = await (await page.request.post('/api/admin/connectors/jellyfin/poll')).json();
    // Two sessions in the fake and each counted exactly once: the declined viewing is refused,
    // the new one is armed. Two, not one, and not three — the sum again, for the reason above.
    expect(second.armed + second.already_armed).toBe(2);

    await page.goto('/');
    const asked = page.locator('[data-finish-prompt]');
    await expect(asked).toContainText('Did you finish The Bear?');
    // Answered before leaving, so the specs after this one open a Home with no question standing
    // on it: these files are stateful and filename-ordered.
    await asked.getByRole('button', { name: 'No — not seen' }).click();
    await expect(asked).toHaveCount(0);
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
