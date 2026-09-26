<script>
  // No stored credential is ever displayed: an empty key field means "keep the stored one" (§14.3).
  // Spend settings are stored only by a Confirm carrying the previewed figure (decision 450).
  import { onMount } from 'svelte';
  import { get, post, api } from '$lib/api.js';
  import { jellyfinDirectory } from '$lib/jellyfin.js';
  import { session } from '$lib/session.svelte.js';
  import { KEYLESS_LABELS, openSpendGuard, spend } from '$lib/spendGuard.svelte.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';
  import SpendMeter from '$lib/components/SpendMeter.svelte';
  import ExtractionSettings from '$lib/components/ExtractionSettings.svelte';
  import LlmProviderCard from '$lib/components/LlmProviderCard.svelte';
  import SourceConnectorCard from '$lib/components/SourceConnectorCard.svelte';

  let cfg = $state(null);
  let url = $state('');
  // Set once typed, so refresh's late first answer cannot overwrite the URL being typed.
  let urlTyped = $state(false);
  let apiKey = $state('');
  let probe = $state(null);
  let jfUsers = $state([]);
  let appUsers = $state([]);
  let syncResult = $state(null);
  let error = $state('');
  let busy = $state('');

  // Per app-user link form: the Jellyfin user plus the optional one-time sign-in (§7.3).
  let form = $state({});

  // The pick is sent only by its own button, and only when it differs from what is stored.
  let libraries = $state(null);
  let picked = $state([]);
  let librarySeq = 0;

  // The one-time token reveal, held only here: leaving the page loses it (decision 418).
  let minted = $state(null);
  // Answered with no token while none is held: nothing was minted, so the press stays offered.
  let unminted = $state(false);
  // `save_jellyfin` mints only for a configured connector whose key this SECRETS_KEY opens.
  const mintable = $derived(Boolean(cfg?.configured) && !cfg?.secrets_unreadable);

  const sorted = (ids) => JSON.stringify([...(ids ?? [])].sort());
  const sameIds = (a, b) => sorted(a) === sorted(b);
  const pickChanged = $derived(Boolean(cfg) && !sameIds(picked, cfg.library_ids));
  // A picked id the server no longer lists is a fault (decision 410); only a loaded list can say.
  const stale = $derived(
    libraries?.ok
      ? (cfg?.library_ids ?? []).filter((id) => !libraries.libraries.some((lib) => lib.id === id))
      : []
  );
  const listed = $derived(
    libraries?.ok
      ? libraries.libraries
      : (cfg?.library_ids ?? []).map((id) => ({ id, name: id }))
  );
  const webhookUrl = $derived(
    `${(session.publicUrl || '<PUBLIC_URL>').replace(/\/+$/, '')}/events/jellyfin`
  );

  onMount(() => {
    refresh();
    openSpendGuard();
  });

  async function refresh() {
    error = '';
    try {
      const directory = await jellyfinDirectory();
      // An unsaved pick survives a Sync or a Test; only a pick nobody touched follows the server.
      const keepPick = pickChanged;
      cfg = directory.cfg;
      if (!keepPick) picked = [...(cfg.library_ids ?? [])];
      loadLibraries(cfg.configured);
      jfUsers = directory.users;
      if (!urlTyped) url = cfg.url ?? '';
      appUsers = await get('/admin/users');
    } catch (err) {
      error = err.message;
    }
  }

  // Not awaited: it has a 45s budget and the mapping table must not wait. A throw means no list.
  async function loadLibraries(configured) {
    const mine = ++librarySeq;
    if (!configured) {
      libraries = null;
      return;
    }
    let answer;
    try {
      answer = await get('/admin/connectors/jellyfin/libraries');
    } catch (err) {
      answer = { ok: false, error: err.message, libraries: [] };
    }
    if (mine === librarySeq) libraries = answer;
  }

  function pick(id, on) {
    const next = new Set(picked);
    if (on) next.add(id);
    else next.delete(id);
    // Listed order first, then the stale ids, so the stored pick reads the way the card does.
    const order = [...listed.map((lib) => lib.id), ...stale];
    picked = order.filter((each) => next.has(each));
  }

  // `[]` widens the boundary to the whole server (decision 364), so never send it from a failed list.
  async function savePick() {
    if (!libraries?.ok || !pickChanged) return;
    error = '';
    busy = 'pick';
    try {
      const answer = await api('/admin/connectors/jellyfin', {
        method: 'PUT',
        body: { library_ids: [...picked] }
      });
      // The pick as stored: the route keeps the server's GUID spelling (decision 410).
      picked = [...(answer?.library_ids ?? picked)];
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  // A `null` answer is either a token another tab minted or no token at all; the next read decides.
  async function mintToken() {
    error = '';
    unminted = false;
    busy = 'mint';
    try {
      const answer = await api('/admin/connectors/jellyfin', {
        method: 'PUT',
        body: { mint_webhook_token: true }
      });
      if (answer?.webhook_token) minted = { token: answer.webhook_token };
      await refresh();
      if (!minted) {
        if (cfg?.has_webhook_token) minted = { token: null };
        else unminted = true;
      }
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function save() {
    error = '';
    busy = 'save';
    try {
      await api('/admin/connectors/jellyfin', { method: 'PUT', body: { url, api_key: apiKey } });
      apiKey = '';
      urlTyped = false;
      unminted = false;
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function test() {
    error = '';
    busy = 'test';
    probe = null;
    try {
      probe = await post('/admin/connectors/jellyfin/test');
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function link(user) {
    error = '';
    busy = `link-${user.id}`;
    const entry = form[user.id] ?? {};
    try {
      await post(`/admin/users/${user.id}/jellyfin`, {
        jellyfin_user_id: entry.jellyfin_user_id,
        jellyfin_username: entry.username || null,
        jellyfin_password: entry.password || null
      });
      form = { ...form, [user.id]: {} };
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  // Completes an existing mapping with the user's own sign-in; same route as Link.
  async function storeSignIn(user) {
    form = {
      ...form,
      [user.id]: { ...(form[user.id] ?? {}), jellyfin_user_id: user.jellyfin_user_id }
    };
    await link(user);
  }

  async function unlink(user) {
    error = '';
    busy = `link-${user.id}`;
    try {
      await api(`/admin/users/${user.id}/jellyfin`, { method: 'DELETE' });
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  async function syncNow() {
    error = '';
    busy = 'sync';
    syncResult = null;
    try {
      syncResult = await post('/admin/connectors/jellyfin/sync');
      await refresh();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  function set(userId, field, value) {
    form = { ...form, [userId]: { ...(form[userId] ?? {}), [field]: value } };
  }

  const stamp = (iso) => (iso ? new Date(iso).toLocaleString() : null);

  function ago(iso) {
    if (!iso) return '';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
    const days = Math.round(hours / 24);
    return `${days} days ago`;
  }

  const when = (iso) => (iso ? `${stamp(iso)} · ${ago(iso)}` : null);

  /** The poll's newest run in one word; a run with no verdict yet is in flight or was killed. */
  function pollOutcome(poll) {
    if (!poll?.last_run_at) return 'never';
    if (poll.last_run_ok === true) return 'ok';
    if (poll.last_run_ok === false) return 'failed';
    return 'unfinished';
  }
</script>

<AdminTabs active="connectors" />

<h1>Connectors</h1>

{#if error}<p class="err" role="alert">{error}</p>{/if}

<section class="card" data-testid="connector-jellyfin">
  <h2>Jellyfin</h2>
  <p class="why">
    The API key grants full server access — Jellyfin has no read-only variant. This app uses it
    for reads and per-user Played writes only, and the Played writes go out under each linked
    person's own token.
  </p>

  {#if cfg?.secrets_unreadable}
    <!-- Saving re-seals under a fresh key but retires the old one, so every other secret sealed
         under it stays unreadable: the copy says so. -->
    <p class="alert" role="alert" data-secrets="unreadable">
      The stored credentials cannot be decrypted with this install's <code>SECRETS_KEY</code>.
      Restoring the <code>.env</code> that was current when the backup was taken recovers
      everything. Pasting the API key again below stores it under a new key and fixes Jellyfin
      only: the old key is retired, and every other secret sealed under it — web push, any other
      connector — stays unreadable until that <code>.env</code> returns. To clear those out
      instead, run <code>spielplan-secrets reset</code> and set each one up again.
    </p>
  {/if}

  <div class="grid">
    <label>
      <span class="data">SERVER URL</span>
      <input
        type="text"
        bind:value={url}
        oninput={() => (urlTyped = true)}
        placeholder="http://jellyfin.local:8096"
      />
    </label>
    <label>
      <span class="data">API KEY</span>
      <input
        type="password"
        bind:value={apiKey}
        placeholder={cfg?.has_api_key ? '•••••••• (stored)' : 'paste an API key'}
      />
    </label>
  </div>

  <div class="row">
    <button class="btn-primary" onclick={save} disabled={busy === 'save'}>Save</button>
    <button class="btn-ghost" onclick={test} disabled={!cfg?.configured || busy === 'test'}>
      {busy === 'test' ? 'Testing…' : 'Test connection'}
    </button>
    <button class="btn-ghost" onclick={syncNow} disabled={!cfg?.configured || busy === 'sync'}>
      {busy === 'sync' ? 'Syncing…' : 'Sync now'}
    </button>
  </div>

  <!-- Stored at every Save and Test; null means nobody has probed yet, not a refusal. -->
  {#if cfg?.server_version || cfg?.server_supported === false}
    <div
      class="data probe"
      data-server-supported={cfg.server_supported === null || cfg.server_supported === undefined
        ? 'unknown'
        : String(cfg.server_supported)}
    >
      server version {cfg.server_version || 'not reported'}
      {#if cfg.server_supported === false}
        · below the pinned 10.9: the per-user Played route (POST /UserPlayedItems) does not exist
        on this server, so nothing this app marks can reach Jellyfin. Reads still work.
      {:else if cfg.server_supported}· Played writes supported{/if}
    </div>
  {/if}

  {#if probe}
    <div class="data probe" data-probe={probe.ok ? 'ok' : 'fail'}>
      {#if probe.ok}
        {probe.server_name} · {probe.version} · {probe.user_count} users
        <!-- The pin is on the per-user Played write; reads merely degrade (§7.1). -->
        <!-- `=== false`: `null` means no version was reported, which is not below the pin. -->
        {#if probe.supported === false}· below the pinned 10.9 — the per-user Played write (POST
          /UserPlayedItems) does not exist here, so seen states cannot reach Jellyfin{/if}
      {:else}
        failed: {probe.error}
      {/if}
    </div>
  {/if}

  {#if syncResult?.already_running}
    <!-- The advisory lock answers a concurrent sweep instead of running it. -->
    <div class="data probe" data-sync="already-running">
      a sweep is already running — this press did nothing. Try again in a moment.
    </div>
  {:else if syncResult}
    <!-- An unreachable Jellyfin yields an all-zero report: an empty `users` (unless skipped_no_link)
         or any `failed_users` means nothing was reconciled, not a quiet household. -->
    <div
      class="data probe"
      data-sync="done"
      data-sync-health={syncResult.push_failed
        ? 'failing'
        : !syncResult.skipped_no_link &&
            (!syncResult.users?.length || syncResult.failed_users?.length)
          ? 'unreachable'
          : 'ok'}
    >
      pushed {syncResult.pushed} · adopted {syncResult.adopted} · unchanged
      {syncResult.unchanged}
      {#if !syncResult.skipped_no_link && !syncResult.users?.length}
        <div class="alert" role="alert" data-sync-unreachable>
          this sweep never read the library, so no seen state moved in either direction — Jellyfin
          did not answer. The app keeps working and the next sweep tries again (§3.3).
        </div>
      {:else if syncResult.failed_users?.length}
        <div class="alert" role="alert" data-sync-member-failed={syncResult.failed_users.length}>
          Jellyfin answered for the library but not for {syncResult.failed_users.join(', ')}, so
          nothing was reconciled for
          {syncResult.failed_users.length === 1 ? 'that member' : 'those members'} in either
          direction. A Jellyfin account that was deleted or renamed stays like this until the
          mapping is corrected (§3.3).
        </div>
      {/if}
      {#if syncResult.needs_relink?.length}· re-link needed: {syncResult.needs_relink.join(', ')}{/if}
      {#if syncResult.owed_no_token}· {syncResult.owed_no_token} owed write(s) with no stored
        sign-in{/if}
      {#if syncResult.owed_unreachable}· {syncResult.owed_unreachable} owed write(s) for titles
        no longer in the library{/if}
      {#if syncResult.unowned}· {syncResult.unowned} title(s) no longer in the library{/if}
      <!-- `resolve.unmatched` is a count; the names come separately as `unmatched_names`. -->
      {#if syncResult.resolve?.unmatched}· {syncResult.resolve.unmatched} library
        item(s) matched no title{/if}
      <!-- `SyncReport.as_dict` caps the names at twenty. -->
      {#if syncResult.resolve?.unmatched_names?.length}
        <div class="unmatched" data-unmatched-names>
          {syncResult.resolve.unmatched > syncResult.resolve.unmatched_names.length
            ? `the first ${syncResult.resolve.unmatched_names.length}: `
            : ''}{syncResult.resolve.unmatched_names.join(', ')}
        </div>
      {/if}
      {#if syncResult.push_failed}
        <!-- A failure, not one count among others: zero pushed can hide every write refused. -->
        <div class="alert" role="alert" data-sync-failure={syncResult.push_failed}>
          {syncResult.push_failed} Played write(s) failed — nothing reached Jellyfin for them.
          {syncResult.push_errors?.[0] ?? 'no reason was reported'}
        </div>
      {/if}
    </div>
  {/if}

  {#if cfg?.configured}
    <!-- Checkboxes are outside design.css's coarse block, so each label is the 48px target. -->
    <div
      class="pick"
      data-library-pick={libraries === null ? 'loading' : libraries.ok ? 'ready' : 'unavailable'}
    >
      <span class="data">LIBRARY PICK</span>
      {#if libraries && !libraries.ok}
        <p class="alert" role="alert">
          The library list did not load: {libraries.error ?? 'no reason was reported'}. The pick
          stays as stored until it does, and nothing is sent from here.
        </p>
      {:else if libraries === null}
        <span class="data">asking Jellyfin for its libraries…</span>
      {/if}
      {#each listed as lib (lib.id)}
        <label class="check">
          <input
            type="checkbox"
            checked={picked.includes(lib.id)}
            disabled={!libraries?.ok || busy === 'pick'}
            onchange={(e) => pick(lib.id, e.currentTarget.checked)}
          />
          <span>{lib.name || lib.id}</span>
        </label>
      {/each}
      {#if stale.length}
        <div class="stale" data-library-stale>
          <p class="alert">
            Picked but no longer listed by the server: {stale.join(', ')}. Adds that no listed
            picked library holds are held until the pick is saved again (decision 410).
          </p>
          {#each stale as id (id)}
            <label class="check">
              <input
                type="checkbox"
                checked={picked.includes(id)}
                disabled={busy === 'pick'}
                onchange={(e) => pick(id, e.currentTarget.checked)}
              />
              <span>{id} (no longer listed)</span>
            </label>
          {/each}
        </div>
      {/if}
      <p class="why">
        {#if picked.length === 0}
          Nothing picked: every library on the server is acquired (decision 364).
        {:else}
          Only what is added to the picked libraries is acquired; an add anywhere else is recorded
          and left alone (decision 364).
        {/if}
      </p>
      <div class="row">
        <button
          class="btn-ghost"
          onclick={savePick}
          disabled={!libraries?.ok || !pickChanged || busy === 'pick'}
        >
          Save library pick
        </button>
      </div>
    </div>
  {/if}

  <!-- Facts of both paths, not a mode: the last ItemAdded is not the last delivery. An older
       backend sends no `trigger`. -->
  {#if cfg?.trigger}
    {@const webhook = cfg.trigger.webhook ?? {}}
    {@const poll = cfg.trigger.delta_poll ?? {}}
    <div
      class="data probe"
      data-trigger="webhook"
      data-item-added={webhook.last_item_added_at ? 'received' : 'none'}
    >
      webhook · last ItemAdded {when(webhook.last_item_added_at) ?? 'none received yet'}
      · last delivery of any kind {when(webhook.last_delivery_at) ?? 'none'} ·
      {webhook.deliveries_7d ?? 0} in the last 7 days
      {#if webhook.last_refusal}
        <div>newest refusal {when(webhook.last_refusal.at)}: {webhook.last_refusal.reason}</div>
      {/if}
    </div>
    <div class="data probe" data-trigger="delta-poll" data-poll-outcome={pollOutcome(poll)}>
      delta poll · newest run {when(poll.last_run_at) ?? 'never'} · {pollOutcome(poll)}
      {#if poll.last_filed !== null && poll.last_filed !== undefined}· filed {poll.last_filed}{/if}
      · last ok {when(poll.last_ok_at) ?? 'never'} · reads from {stamp(poll.watermark) ?? 'the start'}
    </div>
  {/if}

  <!-- Minted only by the press below, shown once, never rotated (decision 418). -->
  {#if minted}
    <div class="token" data-webhook-token-state="revealed">
      {#if minted.token}
        <p class="alert" role="alert">
          Copy this into the Jellyfin Webhook plugin now: it is shown once, here, and never again.
        </p>
        <code class="data-lg" data-webhook-token>{minted.token}</code>
      {:else}
        <p class="why">
          A token already existed, so none was minted, and it cannot be shown again.
        </p>
      {/if}
      <div class="data">
        header X-Spielplan-Token · POST {webhookUrl} · template ops/jellyfin-webhook-template.json
      </div>
    </div>
  {:else if cfg?.has_webhook_token === false}
    <!-- Offered only where the server would mint; otherwise say what it is waiting for. -->
    <div class="token" data-webhook-token-state={mintable ? 'none' : 'unconfigured'}>
      {#if unminted}
        <p class="alert" role="alert" data-webhook-unminted>
          Nothing was minted, and no token is stored: the server mints one only for a connector
          saved with its URL and an API key it can read.
        </p>
      {/if}
      <p class="why">
        {#if mintable}
          No webhook token yet, so the Webhook plugin cannot deliver and the delta poll carries every
          add. Generating one shows it once; nothing can show it again.
        {:else}
          No webhook token yet. One can be generated once the server URL and API key are saved: the
          server mints it only for a configured connector, and shows it once.
        {/if}
      </p>
      <div class="data">
        header X-Spielplan-Token · POST {webhookUrl} · template ops/jellyfin-webhook-template.json
      </div>
      {#if mintable}
        <div class="row">
          <button class="btn-ghost" onclick={mintToken} disabled={busy === 'mint'}>
            Generate webhook token
          </button>
        </div>
      {/if}
    </div>
  {:else if cfg?.has_webhook_token}
    <p class="why" data-webhook-token-state="exists">
      A webhook token exists. It was shown once, when it was generated, and cannot be shown again;
      the delta poll carries every add without it.
    </p>
  {/if}
</section>

<section class="card">
  <h2>User mapping</h2>
  <p class="why">
    Optional and one-to-one. A link adds two-way watched state; signing in as that Jellyfin
    user once stores their own token so Played writes never use the admin key.
  </p>

  <table>
    <thead>
      <tr><th>Account</th><th>Jellyfin user</th><th>Sign-in</th><th></th></tr>
    </thead>
    <tbody>
      {#each appUsers as u (u.id)}
        <tr data-user={u.name}>
          <td>
            <div>{u.name}</div>
            <div class="data">{u.role}</div>
          </td>
          <td>
            {#if u.jellyfin_user_id}
              <div class="data" data-link-state={u.jellyfin_link_state}>
                {jfUsers.find((j) => j.id === u.jellyfin_user_id)?.name ?? u.jellyfin_user_id}
                {u.jellyfin_link_state === 'needs_relink' ? '· needs sign-in' : '· linked'}
              </div>
            {:else}
              <select
                value={form[u.id]?.jellyfin_user_id ?? ''}
                onchange={(e) => set(u.id, 'jellyfin_user_id', e.currentTarget.value)}
                aria-label={`Jellyfin user for ${u.name}`}
              >
                <option value="">not linked</option>
                {#each jfUsers as j (j.id)}<option value={j.id}>{j.name}</option>{/each}
              </select>
            {/if}
          </td>
          <td>
            {#if !u.has_jellyfin_token}
              <div class="creds">
                <input
                  type="text"
                  placeholder="jellyfin username"
                  value={form[u.id]?.username ?? ''}
                  oninput={(e) => set(u.id, 'username', e.currentTarget.value)}
                />
                <input
                  type="password"
                  placeholder="password (once)"
                  value={form[u.id]?.password ?? ''}
                  oninput={(e) => set(u.id, 'password', e.currentTarget.value)}
                />
              </div>
            {:else}
              <span class="data">token stored</span>
            {/if}
          </td>
          <td class="actions">
            {#if u.jellyfin_user_id}
              <!-- A linked row with no stored token needs a button to post the sign-in beside it. -->
              {#if !u.has_jellyfin_token}
                <button
                  class="btn-primary"
                  onclick={() => storeSignIn(u)}
                  disabled={!form[u.id]?.username ||
                    !form[u.id]?.password ||
                    busy === `link-${u.id}`}
                >
                  Store sign-in
                </button>
              {/if}
              <button class="btn-ghost" onclick={() => unlink(u)} disabled={busy === `link-${u.id}`}>
                Unlink
              </button>
            {:else}
              <button
                class="btn-primary"
                onclick={() => link(u)}
                disabled={!form[u.id]?.jellyfin_user_id || busy === `link-${u.id}`}
              >
                Link
              </button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</section>

<SpendMeter />
<ExtractionSettings />
{#each spend.llm?.providers ?? [] as card (card.name)}
  <LlmProviderCard {card} />
{/each}

{#if spend.sourcesError}<p class="err" role="alert">{spend.sourcesError}</p>{/if}
{#each spend.sources?.sources ?? [] as source (source.name)}
  <SourceConnectorCard {source} />
{/each}
{#if spend.sources?.keyless?.length}
  <p class="why" data-keyless>
    {spend.sources.keyless.map((name) => KEYLESS_LABELS[name] ?? name).join(', ')} need no key: a
    stage-2 failure from one of them is not fixed on this page.
  </p>
{/if}

<style>
  h1 {
    margin: 0 0 12px;
    font-size: 19px;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 6px;
    font-size: 15px;
    font-weight: 600;
  }
  .card {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .probe {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  .pick {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .stale {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .check {
    flex-direction: row;
    align-items: center;
    gap: 10px;
    min-height: var(--touch);
    min-width: var(--touch);
    cursor: pointer;
    font-size: 13.5px;
  }
  .check input {
    width: 18px;
    height: 18px;
    margin: 0;
  }
  .check input:disabled + span {
    color: var(--ink-4);
  }
  .token {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .token code {
    word-break: break-all;
  }
  .unmatched {
    margin-top: 4px;
  }
  .alert {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--ember-lift);
    border-radius: var(--r-sm);
    color: var(--ember-lift);
    font-size: 12.5px;
    line-height: 1.45;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    font-weight: 500;
    color: var(--ink-4);
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding-bottom: 6px;
  }
  td {
    border-top: 1px solid var(--line);
    padding: 9px 8px 9px 0;
    vertical-align: top;
  }
  .creds {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .creds input {
    min-width: 130px;
    flex: 1;
  }
  td.actions {
    text-align: right;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }

  @media (max-width: 720px) {
    .grid {
      grid-template-columns: 1fr;
    }
    table,
    thead,
    tbody,
    tr,
    td,
    th {
      display: block;
    }
    thead {
      display: none;
    }
    td {
      padding: 6px 0;
    }
    tr {
      border-top: 1px solid var(--line);
      padding: 8px 0;
    }
    td.actions {
      text-align: left;
    }
  }
</style>
