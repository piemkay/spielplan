<script>
  // No stored credential is ever displayed: an empty key field means "keep the stored one" (§14.3).
  import { onMount } from 'svelte';
  import { get, post, api } from '$lib/api.js';
  import { jellyfinDirectory } from '$lib/jellyfin.js';
  import { session } from '$lib/session.svelte.js';
  import { KEYLESS_LABELS, SOURCE_LABELS, loadSources, spend } from '$lib/spendGuard.svelte.js';
  import { showToast } from '$lib/toast.svelte.js';
  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import SourceConnectorCard from '$lib/components/SourceConnectorCard.svelte';
  import Icon from '$lib/components/Icon.svelte';

  let cfg = $state(null);
  let url = $state('');
  // Set once typed, so refresh's late first answer cannot overwrite the URL being typed.
  let urlTyped = $state(false);
  let apiKey = $state('');
  let probe = $state(null);
  let jfUsers = $state([]);
  let appUsers = $state([]);
  let syncResult = $state(null);
  // The seen sync's newest run that reached Jellyfin; undefined until the system read answers.
  let lastSync = $state(undefined);
  let error = $state('');
  let busy = $state('');
  // Which sheet is open: 'server', 'libraries', 'alerts', 'people', or a film-information source.
  let sheet = $state('');
  let unlinking = $state(null);

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

  const status = $derived(
    !cfg
      ? null
      : cfg.secrets_unreadable
        ? { tone: 'bad', word: "Key can't be read" }
        : !cfg.configured
          ? { tone: 'warn', word: 'Not set up' }
          : probe && !probe.ok
            ? { tone: 'bad', word: 'Not answering' }
            : cfg.server_supported === false || probe?.supported === false
              ? { tone: 'warn', word: 'Too old' }
              : { tone: 'ok', word: 'Connected' }
  );
  // `=== false`: `null` means no version was reported, which is not below the pin.
  const belowPin = $derived(cfg?.server_supported === false || probe?.supported === false);

  // An unreachable Jellyfin yields an all-zero report: an empty `users` (unless skipped_no_link)
  // or any `failed_users` means nothing was reconciled, not a quiet household.
  const unreachable = $derived(
    Boolean(syncResult) && !syncResult.skipped_no_link && !syncResult.users?.length
  );
  const syncHealth = $derived(
    !syncResult || syncResult.already_running
      ? null
      : syncResult.push_failed
        ? 'failing'
        : unreachable || syncResult.failed_users?.length
          ? 'unreachable'
          : 'ok'
  );

  const webhook = $derived(cfg?.trigger?.webhook ?? {});
  const poll = $derived(cfg?.trigger?.delta_poll ?? {});
  const linked = $derived(appUsers.filter((u) => u.jellyfin_user_id).length);
  const source = $derived(spend.sources?.sources?.find((s) => s.name === sheet) ?? null);

  const librariesValue = $derived.by(() => {
    const ids = cfg?.library_ids ?? [];
    if (!ids.length) return 'All';
    if (!libraries?.ok) return `${ids.length} picked`;
    return ids.map((id) => libraries.libraries.find((lib) => lib.id === id)?.name || id).join(', ');
  });
  const alertsValue = $derived(
    !cfg?.trigger
      ? ''
      : cfg.has_webhook_token === false
        ? 'Not set up'
        : webhook.last_item_added_at
          ? ago(webhook.last_item_added_at)
          : 'None yet'
  );
  const watchedValue = $derived(
    syncHealth
      ? syncHealth === 'ok'
        ? 'Synced just now'
        : "Didn't sync"
      : lastSync
        ? `Synced ${ago(lastSync)}`
        : lastSync === null
          ? 'Not synced yet'
          : ''
  );

  onMount(() => {
    refresh();
    loadLastSync();
    loadSources();
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

  // Silent: the row just goes without a time when the system read fails.
  async function loadLastSync() {
    try {
      const system = await get('/admin/system');
      lastSync = system?.last_syncs?.find((s) => s.name === 'jellyfin-seen-sync')?.at ?? null;
    } catch {
      lastSync = undefined;
    }
  }

  // Not awaited: it has a 45s budget and the mapping must not wait. A throw means no list.
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
    // Listed order first, then the stale ids, so the stored pick reads the way the list does.
    const order = [...listed.map((lib) => lib.id), ...stale];
    picked = order.filter((each) => next.has(each));
  }

  // `[]` widens the boundary to the whole server (decision 364), so never send it from a failed list.
  async function savePick() {
    if (!libraries?.ok || !pickChanged) return false;
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
      return true;
    } catch (err) {
      error = err.message;
      return false;
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

  async function copyToken() {
    try {
      await navigator.clipboard.writeText(minted.token);
      showToast('Token copied');
    } catch {
      showToast("Couldn't copy it: select the token and copy it by hand");
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
      return !error;
    } catch (err) {
      error = err.message;
      return false;
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

  // `unlinking` is cleared by the action sheet's own close, which is one history entry.
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
      loadLastSync();
    } catch (err) {
      error = err.message;
    } finally {
      busy = '';
    }
  }

  function set(userId, field, value) {
    form = { ...form, [userId]: { ...(form[userId] ?? {}), [field]: value } };
  }

  function host(address) {
    try {
      return new URL(address).host;
    } catch {
      return address;
    }
  }

  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const stamp = (iso) => (iso ? new Date(iso).toLocaleString() : null);

  function ago(iso) {
    if (!iso) return '';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  const when = (iso) => (iso ? `${stamp(iso)} · ${ago(iso)}` : null);

  /** The poll's newest run in one word; a run with no verdict yet is in flight or was killed. */
  function pollOutcome(run) {
    if (!run?.last_run_at) return 'never';
    if (run.last_run_ok === true) return 'ok';
    if (run.last_run_ok === false) return 'failed';
    return 'unfinished';
  }

  function linkLine(user) {
    const name = jfUsers.find((j) => j.id === user.jellyfin_user_id)?.name ?? user.jellyfin_user_id;
    if (user.jellyfin_link_state === 'needs_relink') return `Linked to ${name}. Needs their sign-in again.`;
    if (user.has_jellyfin_token) return `Linked to ${name}, signed in`;
    return `Linked to ${name}. Their sign-in isn't saved yet.`;
  }

  function sourceBadge(s) {
    if (s.secrets_unreadable) return { tone: 'bad', word: "Key can't be read" };
    if (s.name === 'trakt') {
      if (s.has_client_id && s.has_client_secret) return { tone: 'ok', word: 'Connected' };
      if (s.has_client_id) return { tone: 'warn', word: 'Secret missing' };
      return { tone: '', word: 'Not set up' };
    }
    if (s.has_api_key) return { tone: 'ok', word: 'Connected' };
    return s.required ? { tone: 'warn', word: 'Key missing' } : { tone: '', word: 'Not set up' };
  }

  function sourceNote(s) {
    if (s.required) return 'Required';
    if (s.name === 'trakt' && s.has_client_id && !s.has_client_secret) return 'Client ID saved';
    return '';
  }

  const keyless = $derived(
    (spend.sources?.keyless ?? []).map((name) => KEYLESS_LABELS[name] ?? name)
  );
  const keylessLine = $derived(
    keyless.length > 1 ? `${keyless.slice(0, -1).join(', ')} and ${keyless.at(-1)}` : keyless[0]
  );
</script>

{#snippet bar(title, close)}
  <div class="bar">
    <h2>{title}</h2>
    <button class="btn-plain" onclick={close}>Done</button>
  </div>
{/snippet}

{#snippet chevron()}
  <span class="chev"><Icon name="chevron-right" size={16} /></span>
{/snippet}

{#snippet badge(b)}
  <span class="badge {b.tone}"><span class="dot" aria-hidden="true"></span>{b.word}</span>
{/snippet}

{#snippet setup()}
  <dl class="setup">
    <div><dt>Address</dt><dd class="code">POST {webhookUrl}</dd></div>
    <div><dt>Header</dt><dd class="code">X-Spielplan-Token</dd></div>
    <div><dt>Template</dt><dd class="code">ops/jellyfin-webhook-template.json</dd></div>
  </dl>
{/snippet}

<div class="page">
  <h1 class="large-title">Services</h1>

  {#if error && !sheet}<p class="err" role="alert">{error}</p>{/if}

  <section class="jellyfin" data-testid="connector-jellyfin" aria-label="Jellyfin">
    <div class="card status">
      <div class="head">
        <h2>Jellyfin</h2>
        {#if status}{@render badge(status)}{/if}
      </div>
      <p class="footnote">
        {cfg?.url ? host(cfg.url) : 'Not set up yet'}{cfg?.server_version
          ? ` · version ${cfg.server_version}`
          : ''}
      </p>

      {#if cfg?.secrets_unreadable}
        <!-- Saving re-seals under a fresh key but retires the old one, so every other secret sealed
             under it stays unreadable: the copy says so. -->
        <p class="alert" role="alert" data-secrets="unreadable">
          The saved credentials can't be read with this install's <span class="code">SECRETS_KEY</span>.
          Restoring the <span class="code">.env</span> that was current when the backup was taken
          recovers everything. Pasting the API key again stores it under a new key and fixes Jellyfin
          only: the old key is retired, and every other secret sealed under it — web push, any other
          service — stays unreadable until that <span class="code">.env</span> returns. To clear
          those out instead, run <span class="code">spielplan-secrets reset</span> and set each one up
          again.
        </p>
      {/if}

      {#if belowPin}
        <p class="note warn" data-below-pin>
          This Jellyfin is older than 10.9, so what people mark as watched here can't reach it.
          Reading the library still works.
        </p>
      {/if}

      {#if probe}
        <p class="note" data-probe={probe.ok ? 'ok' : 'fail'}>
          {#if probe.ok}
            {probe.server_name} answered, version {probe.version}, {count(
              probe.user_count,
              'account',
              'accounts'
            )}.
          {:else}
            Jellyfin didn't answer: {probe.error}
          {/if}
        </p>
      {/if}

      {#if syncResult?.already_running}
        <!-- The advisory lock answers a concurrent sweep instead of running it. -->
        <p class="note" data-sync="already-running">
          A sync was already running, so this press did nothing. Try again in a moment.
        </p>
      {:else if syncResult}
        <div class="note" data-sync="done" data-sync-health={syncHealth}>
          {#if unreachable}
            <p class="alert" role="alert" data-sync-unreachable>
              Jellyfin didn't answer, so nothing moved in either direction. Spielplan keeps working
              and the next sync tries again.
            </p>
          {:else if syncResult.failed_users?.length}
            <p class="alert" role="alert" data-sync-member-failed={syncResult.failed_users.length}>
              Jellyfin answered for the library but not for {syncResult.failed_users.join(', ')}, so
              nothing moved for {syncResult.failed_users.length === 1 ? 'them' : 'those people'}. A
              Jellyfin account that was deleted or renamed stays like this until its link is fixed.
            </p>
          {:else if !syncResult.push_failed}
            <p>
              Synced just now{syncResult.pushed || syncResult.adopted
                ? `: ${syncResult.pushed} sent to Jellyfin, ${syncResult.adopted} taken from it.`
                : ', already in step.'}
            </p>
          {/if}
          {#if syncResult.push_failed}
            <!-- A failure, not one count among others: zero pushed can hide every write refused. -->
            <p class="alert" role="alert" data-sync-failure={syncResult.push_failed}>
              {count(syncResult.push_failed, 'watched mark', 'watched marks')} didn't reach Jellyfin.
            </p>
          {/if}
          {#if syncResult.needs_relink?.length}
            <p>{syncResult.needs_relink.join(', ')} need to sign in to Jellyfin again.</p>
          {/if}
          {#if syncResult.owed_no_token}
            <p>{count(syncResult.owed_no_token, 'mark waits', 'marks wait')} for their person's own Jellyfin sign-in.</p>
          {/if}
          {#if syncResult.owed_unreachable}
            <p>{count(syncResult.owed_unreachable, 'mark is', 'marks are')} for titles no longer in the library.</p>
          {/if}
          {#if syncResult.unowned}
            <p>{count(syncResult.unowned, 'title is', 'titles are')} no longer in the library.</p>
          {/if}
          <!-- `resolve.unmatched` is a count; `SyncReport.as_dict` caps the names at twenty. -->
          {#if syncResult.resolve?.unmatched}
            <p>
              {count(syncResult.resolve.unmatched, 'library item', 'library items')} matched no
              title{#if syncResult.resolve.unmatched_names?.length}<span data-unmatched-names
                  >{syncResult.resolve.unmatched > syncResult.resolve.unmatched_names.length
                    ? `, among them: `
                    : ': '}{syncResult.resolve.unmatched_names.join(', ')}</span
                >{/if}.
            </p>
          {/if}
        </div>
      {/if}

      <div class="actions">
        <button class="btn-secondary" onclick={test} disabled={!cfg?.configured || busy === 'test'}>
          {busy === 'test' ? 'Testing…' : 'Test connection'}
        </button>
        <button class="btn-secondary" onclick={syncNow} disabled={!cfg?.configured || busy === 'sync'}>
          {busy === 'sync' ? 'Syncing…' : 'Sync now'}
        </button>
      </div>
    </div>

    <div class="list-group">
      <button class="list-row" onclick={() => (sheet = 'server')}>
        <span class="grow">Server address</span>
        <span class="value">{cfg?.url ? host(cfg.url) : 'Not set'}</span>
        {@render chevron()}
      </button>
      <button class="list-row" onclick={() => (sheet = 'server')}>
        <span class="grow">API key</span>
        <span class="value">
          {cfg?.secrets_unreadable ? "Can't be read" : cfg?.has_api_key ? 'Saved' : 'Not saved'}
        </span>
        {@render chevron()}
      </button>
      {#if cfg?.configured}
        <button class="list-row" onclick={() => (sheet = 'libraries')}>
          <span class="grow">Libraries</span>
          <span class="value clip">{librariesValue}</span>
          {@render chevron()}
        </button>
      {/if}
      <button class="list-row" onclick={() => (sheet = 'alerts')}>
        <span class="grow">New-title alerts</span>
        <span class="value">{alertsValue}</span>
        {@render chevron()}
      </button>
      <div class="list-row">
        <span class="grow">Watched status</span>
        <span class="value">{watchedValue}</span>
      </div>
      <button class="list-row" onclick={() => (sheet = 'people')}>
        <span class="grow">People linked</span>
        <span class="value">{linked} of {appUsers.length}</span>
        {@render chevron()}
      </button>
    </div>

    <details class="tech">
      <summary>Technical details</summary>
      <div class="code lines">
        <!-- Stored at every Save and Test; null means nobody has probed yet, not a refusal. -->
        {#if cfg?.server_version || cfg?.server_supported === false}
          <p
            data-server-supported={cfg.server_supported === null ||
            cfg.server_supported === undefined
              ? 'unknown'
              : String(cfg.server_supported)}
          >
            server version {cfg.server_version || 'not reported'}
            {#if cfg.server_supported === false}
              · below the pinned 10.9: the per-user Played route (POST /UserPlayedItems) does not
              exist on this server, so nothing this app marks can reach Jellyfin. Reads still work.
            {:else if cfg.server_supported}· Played writes supported{/if}
          </p>
        {/if}
        {#if probe}
          <p data-probe-detail>
            {#if probe.ok}
              {probe.server_name} · {probe.version} · {probe.user_count} users
              {#if probe.supported === false}· below the pinned 10.9 — the per-user Played write
                (POST /UserPlayedItems) does not exist here{/if}
            {:else}
              failed: {probe.error}
            {/if}
          </p>
        {/if}
        {#if syncResult && !syncResult.already_running}
          <p data-sync-report>
            pushed {syncResult.pushed} · adopted {syncResult.adopted} · unchanged
            {syncResult.unchanged}{#if syncResult.push_errors?.length}
              · {syncResult.push_errors[0]}{/if}
          </p>
        {/if}
        <!-- Facts of both paths, not a mode: the last ItemAdded is not the last delivery. An older
             backend sends no `trigger`. -->
        {#if cfg?.trigger}
          <p data-trigger="webhook" data-item-added={webhook.last_item_added_at ? 'received' : 'none'}>
            webhook · last ItemAdded {when(webhook.last_item_added_at) ?? 'none received yet'}
            · last delivery of any kind {when(webhook.last_delivery_at) ?? 'none'} ·
            {webhook.deliveries_7d ?? 0} in the last 7 days
            {#if webhook.last_refusal}
              · newest refusal {when(webhook.last_refusal.at)}: {webhook.last_refusal.reason}
            {/if}
          </p>
          <p data-trigger="delta-poll" data-poll-outcome={pollOutcome(poll)}>
            delta poll · newest run {when(poll.last_run_at) ?? 'never'} · {pollOutcome(poll)}
            {#if poll.last_filed !== null && poll.last_filed !== undefined}· filed {poll.last_filed}{/if}
            · last ok {when(poll.last_ok_at) ?? 'never'} · reads from {stamp(poll.watermark) ??
              'the start'}
          </p>
        {/if}
        {#if !cfg?.server_version && !cfg?.trigger && !probe && !syncResult}
          <p>nothing reported yet</p>
        {/if}
      </div>
    </details>

    <Sheet open={sheet === 'server'} onClose={() => (sheet = '')} label="Jellyfin server">
      {#snippet children(close)}
        {@render bar('Jellyfin server', close)}
        <div class="form">
          {#if error}<p class="err" role="alert">{error}</p>{/if}
          <label class="field">
            <span>Server address</span>
            <input
              type="text"
              bind:value={url}
              oninput={() => (urlTyped = true)}
              placeholder="http://jellyfin.local:8096"
            />
          </label>
          <label class="field">
            <span>API key</span>
            <input
              type="password"
              autocomplete="off"
              bind:value={apiKey}
              placeholder={cfg?.has_api_key ? 'Saved' : 'Paste an API key'}
            />
          </label>
          <p class="footnote">
            The API key gives full access to the server: Jellyfin has no read-only kind. Spielplan
            only reads the library, and marks what someone watched with that person's own sign-in.
            Leave the key empty to keep the saved one.
          </p>
          <button
            class="btn-primary"
            onclick={async () => (await save()) && close()}
            disabled={busy === 'save'}
          >
            Save
          </button>
        </div>
      {/snippet}
    </Sheet>

    <Sheet open={sheet === 'libraries'} onClose={() => (sheet = '')} label="Libraries">
      {#snippet children(close)}
        {@render bar('Libraries', close)}
        <div
          class="form"
          data-library-pick={libraries === null ? 'loading' : libraries.ok ? 'ready' : 'unavailable'}
        >
          {#if error}<p class="err" role="alert">{error}</p>{/if}
          {#if libraries && !libraries.ok}
            <p class="alert" role="alert">
              The library list didn't load: {libraries.error ?? 'no reason was given'}. The pick
              stays as saved until it does.
            </p>
          {:else if libraries === null}
            <p class="footnote">Asking Jellyfin for its libraries…</p>
          {/if}
          <div class="list-group">
            {#each listed as lib (lib.id)}
              <label class="list-row check">
                <span class="grow">{lib.name || lib.id}</span>
                <input
                  type="checkbox"
                  checked={picked.includes(lib.id)}
                  disabled={!libraries?.ok || busy === 'pick'}
                  onchange={(e) => pick(lib.id, e.currentTarget.checked)}
                />
              </label>
            {/each}
            {#each stale as id (id)}
              <label class="list-row check">
                <span class="grow">{id} (no longer on the server)</span>
                <input
                  type="checkbox"
                  checked={picked.includes(id)}
                  disabled={busy === 'pick'}
                  onchange={(e) => pick(id, e.currentTarget.checked)}
                />
              </label>
            {/each}
          </div>
          {#if stale.length}
            <p class="alert" data-library-stale>
              Picked but no longer on the server: {stale.join(', ')}. Titles added elsewhere wait
              until the pick is saved again.
            </p>
          {/if}
          <p class="footnote">
            {#if picked.length === 0}
              Nothing picked: new titles from every library are added.
            {:else}
              Only titles added to the picked libraries are added here; anything else in Jellyfin is
              left alone.
            {/if}
          </p>
          <button
            class="btn-primary"
            onclick={async () => (await savePick()) && close()}
            disabled={!libraries?.ok || !pickChanged || busy === 'pick'}
          >
            Save libraries
          </button>
        </div>
      {/snippet}
    </Sheet>

    <Sheet open={sheet === 'alerts'} onClose={() => (sheet = '')} label="New-title alerts">
      {#snippet children(close)}
        {@render bar('New-title alerts', close)}
        <div class="form">
          {#if error}<p class="err" role="alert">{error}</p>{/if}
          <p class="why">
            When Jellyfin adds a film or series, its Webhook plugin tells Spielplan at once. Without
            it, Spielplan still looks for new titles every 15 minutes.
          </p>
          {#if cfg?.trigger}
            <div class="list-group">
              <div class="list-row">
                <span class="grow">Last announced</span>
                <span class="value">
                  {webhook.last_item_added_at ? ago(webhook.last_item_added_at) : 'None yet'}
                </span>
              </div>
              <div class="list-row">
                <span class="grow">Last look</span>
                <span class="value">
                  {pollOutcome(poll) === 'never'
                    ? 'Not yet'
                    : pollOutcome(poll) === 'failed'
                      ? `Failed ${ago(poll.last_run_at)}`
                      : ago(poll.last_run_at)}
                </span>
              </div>
            </div>
          {/if}

          <!-- Minted only by the press below, shown once, never rotated (decision 418). -->
          {#if minted}
            <div class="token" data-webhook-token-state="revealed">
              {#if minted.token}
                <p class="alert" role="alert">
                  Copy this into Jellyfin's Webhook plugin now. It's shown once, here, and never
                  again.
                </p>
                <code class="code secret" data-webhook-token>{minted.token}</code>
                <button class="btn-secondary" onclick={copyToken}>
                  <Icon name="copy" size={18} />Copy
                </button>
              {:else}
                <p class="why">A token already existed, so none was made, and it can't be shown again.</p>
              {/if}
              {@render setup()}
            </div>
          {:else if cfg?.has_webhook_token === false}
            <!-- Offered only where the server would mint; otherwise say what it is waiting for. -->
            <div class="token" data-webhook-token-state={mintable ? 'none' : 'unconfigured'}>
              {#if unminted}
                <p class="alert" role="alert" data-webhook-unminted>
                  Nothing was made, and no token is saved: one is made only once the server address
                  and an API key Spielplan can read are saved.
                </p>
              {/if}
              <p class="why">
                {#if mintable}
                  No token yet, so the Webhook plugin can't announce new titles. A new token is shown
                  once, here.
                {:else}
                  A token can be made once the server address and API key are saved. It is shown
                  once, here.
                {/if}
              </p>
              {@render setup()}
              {#if mintable}
                <button class="btn-primary" onclick={mintToken} disabled={busy === 'mint'}>
                  Create token
                </button>
              {/if}
            </div>
          {:else if cfg?.has_webhook_token}
            <div class="token" data-webhook-token-state="exists">
              <p class="why">
                A token exists. It was shown once, when it was made, and can't be shown again.
              </p>
              {@render setup()}
            </div>
          {/if}
        </div>
      {/snippet}
    </Sheet>

    <Sheet open={sheet === 'people'} onClose={() => (sheet = '')} label="People linked">
      {#snippet children(close)}
        {@render bar('People linked', close)}
        <div class="form">
          {#if error}<p class="err" role="alert">{error}</p>{/if}
          <div class="list-group">
            {#each appUsers as u (u.id)}
              <div class="person" data-user={u.name}>
                <div class="who">
                  <span class="name">{u.name}</span>
                  {#if u.jellyfin_user_id}
                    <span
                      class="footnote"
                      data-link-state={u.jellyfin_link_state}
                      data-has-token={String(Boolean(u.has_jellyfin_token))}
                    >
                      {linkLine(u)}
                    </span>
                  {/if}
                </div>
                {#if !u.jellyfin_user_id}
                  <select
                    value={form[u.id]?.jellyfin_user_id ?? ''}
                    onchange={(e) => set(u.id, 'jellyfin_user_id', e.currentTarget.value)}
                    aria-label={`Jellyfin user for ${u.name}`}
                  >
                    <option value="">Not linked</option>
                    {#each jfUsers as j (j.id)}<option value={j.id}>{j.name}</option>{/each}
                  </select>
                {/if}
                {#if !u.has_jellyfin_token}
                  <input
                    type="text"
                    autocomplete="off"
                    aria-label={`Jellyfin username for ${u.name}`}
                    placeholder="Jellyfin username"
                    value={form[u.id]?.username ?? ''}
                    oninput={(e) => set(u.id, 'username', e.currentTarget.value)}
                  />
                  <input
                    type="password"
                    autocomplete="off"
                    aria-label={`Jellyfin password for ${u.name}`}
                    placeholder="Jellyfin password, used once"
                    value={form[u.id]?.password ?? ''}
                    oninput={(e) => set(u.id, 'password', e.currentTarget.value)}
                  />
                {/if}
                <div class="actions">
                  {#if u.jellyfin_user_id}
                    {#if !u.has_jellyfin_token}
                      <button
                        class="btn-secondary"
                        onclick={() => storeSignIn(u)}
                        disabled={!form[u.id]?.username ||
                          !form[u.id]?.password ||
                          busy === `link-${u.id}`}
                      >
                        Save sign-in
                      </button>
                    {/if}
                    <button
                      class="btn-secondary btn-destructive"
                      onclick={() => (unlinking = u)}
                      disabled={busy === `link-${u.id}`}
                    >
                      Unlink
                    </button>
                  {:else}
                    <button
                      class="btn-secondary"
                      onclick={() => link(u)}
                      disabled={!form[u.id]?.jellyfin_user_id || busy === `link-${u.id}`}
                    >
                      Link
                    </button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
          <p class="footnote">
            Optional, one Jellyfin account per person. A link keeps watched status in step both
            ways; their own sign-in, typed once, is what marks things watched in Jellyfin, never the
            admin key.
          </p>
        </div>
      {/snippet}
    </Sheet>
  </section>

  <section class="group" aria-labelledby="film-info">
    <h2 class="list-header" id="film-info">Film information</h2>
    {#if spend.sourcesError}<p class="err" role="alert">{spend.sourcesError}</p>{/if}
    {#if spend.sources?.sources?.length}
      <div class="list-group">
        {#each spend.sources.sources as s (s.name)}
          <button class="list-row" data-source-row={s.name} onclick={() => (sheet = s.name)}>
            <span class="grow stack">
              <span>{SOURCE_LABELS[s.name] ?? s.name}</span>
              {#if sourceNote(s)}<span class="sub">{sourceNote(s)}</span>{/if}
            </span>
            {@render badge(sourceBadge(s))}
            {@render chevron()}
          </button>
        {/each}
      </div>
    {/if}
    {#if keyless.length}
      <p class="list-footer" data-keyless>{keylessLine} need no key.</p>
    {/if}
  </section>

  <section class="group" aria-labelledby="ai">
    <h2 class="list-header" id="ai">AI</h2>
    <div class="list-group">
      <a class="list-row" href="/admin/budget">
        <span class="grow">Providers and budget</span>
        {@render chevron()}
      </a>
    </div>
  </section>
</div>

<Sheet
  open={Boolean(source)}
  onClose={() => (sheet = '')}
  label={SOURCE_LABELS[source?.name] ?? 'Film information'}
>
  {#snippet children(close)}
    {@render bar(SOURCE_LABELS[source?.name] ?? '', close)}
    {#if source}<SourceConnectorCard {source} />{/if}
  {/snippet}
</Sheet>

<ActionSheet
  open={Boolean(unlinking)}
  title={unlinking ? `Unlink ${unlinking.name} from Jellyfin? Their saved sign-in is deleted.` : ''}
  options={unlinking
    ? [{ label: 'Unlink', destructive: true, onSelect: () => unlink(unlinking) }]
    : []}
  onClose={() => (unlinking = null)}
/>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 32px;
    max-width: 720px;
  }
  .large-title {
    padding-top: 8px;
    margin-bottom: -16px;
  }
  .jellyfin,
  .group {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .group {
    gap: 0;
  }
  .status {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .head h2 {
    flex: 1;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .footnote,
  .note,
  .note p {
    margin: 0;
  }
  .note {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .note.warn {
    color: var(--warning);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding-top: 4px;
  }
  .badge .dot {
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
    background: currentColor;
  }
  .grow {
    flex: 1;
    min-width: 0;
  }
  .stack {
    display: flex;
    flex-direction: column;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .list-row .value {
    margin-left: 0;
    text-align: right;
  }
  .clip {
    max-width: 55%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chev {
    display: grid;
    color: rgba(245, 240, 232, 0.35);
    margin-right: -4px;
  }
  a.list-row {
    color: var(--text);
  }
  .tech summary {
    display: flex;
    align-items: center;
    min-height: var(--touch);
    padding: 0 var(--gutter);
    list-style: none;
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    cursor: pointer;
  }
  .tech summary::-webkit-details-marker {
    display: none;
  }
  .lines {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
    overflow-wrap: anywhere;
  }
  .lines p {
    margin: 0;
  }
  .alert {
    margin: 0;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--warning-tint);
    color: var(--text);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }

  .bar {
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 4px 0 8px;
    background: var(--bg-elevated);
  }
  .bar h2 {
    flex: 1;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .bar .btn-plain {
    margin-right: -8px;
    font-weight: 600;
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding-top: 8px;
  }
  .form > .btn-primary {
    width: 100%;
    min-height: 50px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: var(--fs-footnote);
    color: var(--text-2);
  }
  .check {
    cursor: pointer;
  }
  .check input {
    width: 20px;
    height: 20px;
    margin: 0;
  }
  .why {
    margin: 0;
  }
  .token {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .secret {
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
    word-break: break-all;
  }
  .setup {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .setup div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .setup dt {
    font-size: var(--fs-footnote);
    color: var(--text-3);
  }
  .setup dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .person {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px var(--gutter);
  }
  .person + .person {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .who {
    display: flex;
    flex-direction: column;
  }
  .name {
    font-size: var(--fs-body);
    line-height: 22px;
  }
</style>
