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
  import { jellyfinDirectory } from '$lib/jellyfin.js';
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
      const directory = await jellyfinDirectory();
      cfg = directory.cfg;
      jfUsers = directory.users;
      url = cfg.url ?? '';
      appUsers = await get('/admin/users');
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

  {#if cfg?.secrets_unreadable}
    <!--
      Not the same message as "not configured": the connector row is there and its credentials
      are real, they are sealed under a SECRETS_KEY this install no longer holds — a restored
      dump without its env file, or a regenerated key (M4.7 dd03). Members keep working (§3.3),
      so the only person who can act on this is the admin standing in front of this card, and
      the action is Save, which re-seals under a fresh key.

      The last sentence is the cost of that shortcut, and it is here because this card is the
      only place the shortcut is offered. Saving retires the DEK it cannot open
      (`connectors/registry.save_jellyfin`), which is what lets a fresh one be minted — and
      every *other* secret sealed under the retired key stays unreadable until the original
      .env comes back. An admin who reads only "paste the key again" repairs Jellyfin and quietly
      leaves the web-push pair and any other connector behind, with the System card the only
      surface that still says so.
    -->
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

  <!-- §7.1's pin, as this install last measured it — without pressing Test. Save and Test both
       store the probed version and its verdict beside the URL now, so a 10.8 server says so every
       day rather than only in the minute after a probe. `null` is "nobody has probed yet" and must
       not read as a refusal: that is the state a fresh install is in. [M4.11 finding 16] -->
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
        <!-- It was "reads may miss fields", which names the wrong half: by §7.1 and the client's
             own pin the 10.9-only route is the per-user Played WRITE. Reads degrade; the write does
             not exist, and an admin who read this line had no way to know the app -> Jellyfin
             direction was dead. [M4.11 finding 16] -->
        <!-- `=== false`, not `!supported`: `null` is "this server did not report a version",
             which `played_write_refusal` does not refuse on and this line must not accuse. A
             200 with no parseable version — a forward-auth portal, a hardening rule on
             /System/Info/Public — read as "below the pin" and named no version to check it
             against. [review cycle 1: m411-rev-jf-04] -->
        {#if probe.supported === false}· below the pinned 10.9 — the per-user Played write (POST
          /UserPlayedItems) does not exist here, so seen states cannot reach Jellyfin{/if}
      {:else}
        failed: {probe.error}
      {/if}
    </div>
  {/if}

  {#if syncResult?.already_running}
    <!-- §5.3 fires the sweep every fifteen minutes and this button is the other caller; the
         advisory lock answers "a sweep is already running" rather than sweeping the same people
         against two different snapshots. Pressing again once it finishes sweeps for real. -->
    <div class="data probe" data-sync="already-running">
      a sweep is already running — this press did nothing. Try again in a moment.
    </div>
  {:else if syncResult}
    <!-- `ok` is a claim about a sweep that RAN, and one state made it a claim about a sweep that
         did not. §3.3 makes an unreachable Jellyfin a degraded sync rather than a broken app, so
         `seen.sync_all` returns the report as it stands when `client.all_items(None)` raises — and
         every counter in it is zero, including `push_failed`. Printed through the rule below that
         is indistinguishable from the quiet healthy household, which is M4.11 finding 3's own
         sentence one layer up: that finding separated "owed nothing" from "lost every write" and
         left "never read the library" reading as the first. Measured: against a media server whose
         `/Items` refused the sweep's read, this card printed "pushed 0 · adopted 0 · unchanged 0"
         with `data-sync-health="ok"` while NEITHER direction of §7.3 had run.
         `users` is the signal because `sync_all` appends to it per linked member inside the loop
         the failed read returns before — and `skipped_no_link` is what separates it from the
         household that has no connector or no link at all, which is a legal §3.1 state and not a
         failure. [§7.3, §3.3, §6.6; M4.11 finding 3]

         `failed_users` is the same fault arriving by the other door, which the rule above missed:
         the library read is keyless and each member's is not, so a Jellyfin account that was
         deleted or renamed 404s that member's `/Items` for ever while the household read keeps
         succeeding. `sync_all` swallows it per member, so `users` is full, every counter is zero
         and this card printed "pushed 0 · adopted 0 · unchanged 0" in green for a sweep that
         reconciled nobody. [review cycle 1: seen-02] -->
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
      <!-- A count, not a list. `SyncReport.as_dict` sends `resolve.unmatched` as `len(...)` and the
           names separately as `unmatched_names`, so `.length` on it was `undefined` and this clause
           could never render — §7.2's refused matches, which M5's acquisition pipeline consumes,
           had no surface at all. The vitest beside this file asserted it against a fabricated array,
           which is the assertion becoming the implementation compared to itself. [§7.2, §6.6] -->
      {#if syncResult.resolve?.unmatched}· {syncResult.resolve.unmatched} library
        item(s) matched no title{/if}
      {#if syncResult.push_failed}
        <!-- A failure, not a count in a row of counts. This card printed
             "pushed 0 · adopted 0 · unchanged 87" while every Played write in the sweep was being
             refused, which reads as a quiet household rather than as a dead direction — the whole
             of M4.11 finding 3. The reason is the sweep's own first distinct one. -->
        <div class="alert" role="alert" data-sync-failure={syncResult.push_failed}>
          {syncResult.push_failed} Played write(s) failed — nothing reached Jellyfin for them.
          {syncResult.push_errors?.[0] ?? 'no reason was reported'}
        </div>
      {/if}
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
