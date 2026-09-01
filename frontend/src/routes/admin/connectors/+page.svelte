<script>
  /**
   * Admin → Connectors. Spec v2.1 §6.6, §3.3, §7.
   *
   * §6.6's Jellyfin card in full but for two pieces: "URL, API key, library pick,
   * user-mapping table, test button, sync now, webhook status". The library pick and the
   * webhook belong to §7.2's acquisition trigger, which is M5, and say so rather than
   * appearing as controls that do nothing.
   *
   * The API key is never displayed. §14.3: "Jellyfin API keys are unscoped and
   * admin-equivalent — no read-only variant exists", so the field posts empty to mean "keep
   * the stored one" and the page can only tell you *whether* there is a key.
   *
   * LLM providers, TMDB, OMDb and Trakt are the rest of §6.6's Connectors card and arrive
   * with M5.
   */
  import { onMount } from 'svelte';
  import { get, post, api } from '$lib/api.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';

  let cfg = $state(null);
  let url = $state('');
  let apiKey = $state('');
  let probe = $state(null);
  let jfUsers = $state([]);
  let appUsers = $state([]);
  let syncResult = $state(null);
  let error = $state('');
  let busy = $state('');

  // Per app-user link form: the Jellyfin user to map to, plus §7.3's optional one-time
  // password entry that buys the least-privilege write path.
  let form = $state({});

  onMount(refresh);

  async function refresh() {
    error = '';
    try {
      cfg = await get('/admin/connectors/jellyfin');
      url = cfg.url ?? '';
      appUsers = await get('/admin/users');
      if (cfg.configured) {
        jfUsers = (await get('/admin/connectors/jellyfin/users').catch(() => [])) ?? [];
      }
    } catch (err) {
      error = err.message;
    }
  }

  async function save() {
    error = '';
    busy = 'save';
    try {
      await api('/admin/connectors/jellyfin', { method: 'PUT', body: { url, api_key: apiKey } });
      apiKey = '';
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

  /**
   * Complete an existing mapping with that Jellyfin user's own sign-in (§7.3's least-privilege
   * write path). Same route as Link — the mapping is unchanged, only the token is new.
   */
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
</script>

<AdminTabs active="connectors" />

<h1>Connectors</h1>

{#if error}<p class="err" role="alert">{error}</p>{/if}

<section class="card">
  <h2>Jellyfin</h2>
  <p class="why">
    The API key grants full server access — Jellyfin has no read-only variant. This app uses it
    for reads and per-user Played writes only, and the Played writes go out under each linked
    person's own token.
  </p>

  <div class="grid">
    <label>
      <span class="data">SERVER URL</span>
      <input type="text" bind:value={url} placeholder="http://jellyfin.local:8096" />
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

  {#if probe}
    <div class="data probe" data-probe={probe.ok ? 'ok' : 'fail'}>
      {#if probe.ok}
        {probe.server_name} · {probe.version} · {probe.user_count} users
        {#if !probe.supported}· below the pinned 10.9 — reads may miss fields{/if}
      {:else}
        failed: {probe.error}
      {/if}
    </div>
  {/if}

  {#if syncResult}
    <div class="data probe" data-sync="done">
      pushed {syncResult.pushed} · adopted {syncResult.adopted} · unchanged
      {syncResult.unchanged}
      {#if syncResult.needs_relink?.length}· re-link needed: {syncResult.needs_relink.join(', ')}{/if}
      {#if syncResult.owed_unreachable}· {syncResult.owed_unreachable} owed write(s) for titles
        no longer in the library{/if}
    </div>
  {/if}

  <p class="why milestone">Library pick and webhook status arrive with M5 (§7.2).</p>
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
              <!-- A linked row with no stored token is §7.3's needs_relink state, and the
                   credential inputs beside it are useless without something that posts them.
                   The mapping is already chosen, so this only completes it. -->
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
  .milestone {
    color: var(--ink-4);
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
