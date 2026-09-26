<script>
  // §6.6's floors are enforced on the controls as well as at the route (409), and a disabled control
  // says which floor it stands on. A one-time password renders only from the response that issued it.
  import { onMount } from 'svelte';
  import { api, get, post } from '$lib/api.js';
  import { jellyfinDirectory } from '$lib/jellyfin.js';
  import { session } from '$lib/session.svelte.js';
  import AdminTabs from '$lib/components/AdminTabs.svelte';

  let rows = $state([]);
  let error = $state('');
  let busy = $state('');
  let loaded = $state(false);
  // One row editor at a time: the floors are read per row.
  let open = $state(null);
  let confirming = $state(null);
  let issued = $state(null);
  let draft = $state({});
  let newName = $state('');
  let newRole = $state('member');
  // The roster carries a count, but revoke takes a credential id, so the open row lists them.
  let passkeys = $state({});
  let jellyfin = $state(null);
  let link = $state({});

  const activeAdmins = $derived(rows.filter((u) => u.role === 'admin' && u.is_active).length);

  // For the button only: the server counts again inside the writing transaction.
  const isLastActiveAdmin = (u) => u.role === 'admin' && u.is_active && activeAdmins === 1;
  const isSelf = (u) => u.id === session.user?.id;

  const FLOOR = (verb) =>
    `the last active admin cannot be ${verb} (§6.6: at least one active admin always exists) — ` +
    'promote another account first';
  const SELF = (what) => `an admin cannot ${what} from the Users tab (§6.6)`;
  const FLOOR_ORDER = ['demote', 'delete', 'disable', 'reset-password', 'reset-pin'];

  // The floor is checked before the self rule, in the route's order, so the reason matches its 409.
  function blocked(u, action) {
    if (action === 'demote') return isLastActiveAdmin(u) ? FLOOR('demoted') : '';
    if (action === 'delete') return isLastActiveAdmin(u) ? FLOOR('deleted') : '';
    if (action === 'disable') {
      if (isLastActiveAdmin(u)) return FLOOR('disabled');
      return isSelf(u) ? SELF('disable their own account') : '';
    }
    if (action === 'reset-password') return isSelf(u) ? SELF('reset their own password') : '';
    if (action === 'reset-pin') return isSelf(u) ? SELF('reset their own PIN') : '';
    return '';
  }

  onMount(load);

  async function load() {
    try {
      rows = (await get('/admin/users')) ?? [];
      loaded = true;
    } catch (err) {
      error = err.message || String(err);
    }
  }

  // Every write re-reads the roster rather than reconciling the fragments each route returns.
  async function run(key, fn) {
    error = '';
    busy = key;
    try {
      await fn();
      await load();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = '';
    }
  }

  function create() {
    return run('create', async () => {
      const made = await post('/admin/users', { name: newName.trim(), role: newRole });
      issued = { name: made.name, password: made.one_time_password, note: made.note };
      newName = '';
      newRole = 'member';
    });
  }

  function saveRow(u) {
    const d = draft[u.id] ?? {};
    const body = {};
    if (d.name?.trim() && d.name.trim() !== u.name) body.name = d.name.trim();
    if (d.role && d.role !== u.role) body.role = d.role;
    return run(`save-${u.id}`, async () => {
      // The route refuses an edit naming neither field (400), so send nothing.
      if (!Object.keys(body).length) return;
      await api(`/admin/users/${u.id}`, { method: 'PATCH', body });
      delete draft[u.id];
    });
  }

  function resetPassword(u) {
    return run(`pw-${u.id}`, async () => {
      const res = await post(`/admin/users/${u.id}/reset-password`);
      issued = { name: u.name, password: res.one_time_password, note: res.note };
    });
  }

  const resetPin = (u) => run(`pin-${u.id}`, () => post(`/admin/users/${u.id}/reset-pin`));

  const setActive = (u, is_active) =>
    run(`active-${u.id}`, () => post(`/admin/users/${u.id}/active`, { is_active }));

  const remove = (u) =>
    run(`del-${u.id}`, async () => {
      await api(`/admin/users/${u.id}`, { method: 'DELETE' });
      confirming = null;
      open = null;
    });

  const revokePasskey = (u, credentialId) =>
    run(`key-${u.id}`, async () => {
      await api(`/admin/users/${u.id}/passkeys/${encodeURIComponent(credentialId)}`, {
        method: 'DELETE'
      });
      passkeys[u.id] = (await get(`/admin/users/${u.id}/passkeys`)) ?? [];
    });

  function linkJellyfin(u) {
    const entry = link[u.id] ?? {};
    return run(`jf-${u.id}`, async () => {
      await post(`/admin/users/${u.id}/jellyfin`, {
        jellyfin_user_id: entry.jellyfin_user_id,
        // Optional: the user's own sign-in for §7.3's least-privilege writes; without it, "needs sign-in".
        jellyfin_username: entry.username || null,
        jellyfin_password: entry.password || null
      });
      link[u.id] = { ...entry, username: '', password: '' };
    });
  }

  const unlinkJellyfin = (u) =>
    run(`jf-${u.id}`, async () => {
      await api(`/admin/users/${u.id}/jellyfin`, { method: 'DELETE' });
      link[u.id] = { jellyfin_user_id: '', username: '', password: '' };
    });

  const day = (iso) => (iso ? new Date(iso).toLocaleDateString() : 'never');

  // Read when a row opens, not with the roster: a household reads one row at a time.
  async function loadRow(u) {
    try {
      passkeys[u.id] = (await get(`/admin/users/${u.id}/passkeys`)) ?? [];
      jellyfin ??= await jellyfinDirectory();
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function edit(u) {
    open = open === u.id ? null : u.id;
    confirming = null;
    if (open === null) return;
    if (!draft[u.id]) draft[u.id] = { name: u.name, role: u.role };
    if (!link[u.id]) {
      link[u.id] = { jellyfin_user_id: u.jellyfin_user_id ?? '', username: '', password: '' };
    }
    loadRow(u);
  }
</script>

<AdminTabs active="users" />

<h1>Users</h1>
<p class="why">
  §6.6: the only place accounts are made. An admin never sees, sets or types anybody's
  password — creating an account and resetting one both issue a one-time password, shown once,
  which the account exchanges for its own at first login.
</p>

<!-- The origin itself, not the variable's name, so the admin can compare it with the phone's. -->
<div class="warn" data-testid="users-public-url">
  Passkeys are bound to the public origin
  <code class="data-lg">{session.publicUrl || 'PUBLIC_URL is not set'}</code>. Changing it
  invalidates every registered credential in the list below, on every device, at once.
</div>

{#if error}<div class="err" role="alert">{error}</div>{/if}

{#if issued}
  <!-- Shown exactly once: the server keeps only an argon2 hash. -->
  <div class="otp card" role="status" data-testid="user-otp">
    <div><strong>{issued.name}</strong></div>
    <div class="data-lg">one-time password · <code>{issued.password}</code></div>
    <div class="why">{issued.note}</div>
    <button class="btn-ghost" onclick={() => (issued = null)}>Done — it is written down</button>
  </div>
{/if}

<section class="card create" data-testid="user-create">
  <h2>Add an account</h2>
  <div class="addrow">
    <input type="text" bind:value={newName} placeholder="name" aria-label="New account name" />
    <select bind:value={newRole} aria-label="New account role">
      <option value="member">member</option>
      <option value="admin">admin</option>
    </select>
    <button class="btn-primary" onclick={create} disabled={busy === 'create' || !newName.trim()}>
      {busy === 'create' ? 'Creating…' : 'Create'}
    </button>
  </div>
</section>

{#if !loaded}
  <p class="data">loading…</p>
{:else}
  <ul class="roster" data-testid="users-roster">
    {#each rows as u (u.id)}
      <li class:off={!u.is_active} data-testid="user-row" data-user-id={u.id}>
        <button class="rowhead" onclick={() => edit(u)} aria-expanded={open === u.id}>
          <span class="who">
            <span class="name">{u.name}</span>
            {#if isSelf(u)}<span class="data">you</span>{/if}
          </span>
          <span class="data facts" data-user-facts>
            {u.role} · {u.passkeys} passkey{u.passkeys === 1 ? '' : 's'} · PIN {u.has_pin
              ? 'set'
              : 'unset'} · Jellyfin {u.jellyfin_user_id
              ? u.jellyfin_link_state === 'needs_relink'
                ? 'needs sign-in'
                : 'linked'
              : 'unlinked'} · {u.is_active ? 'active' : 'disabled'}
          </span>
        </button>

        {#if open === u.id}
          <div class="editor">
            <div class="addrow">
              <input
                type="text"
                value={draft[u.id]?.name ?? u.name}
                oninput={(e) => (draft[u.id] = { ...draft[u.id], name: e.currentTarget.value })}
                aria-label={`Name for ${u.name}`}
              />
              <select
                value={draft[u.id]?.role ?? u.role}
                onchange={(e) => (draft[u.id] = { ...draft[u.id], role: e.currentTarget.value })}
                aria-label={`Role for ${u.name}`}
                disabled={!!blocked(u, 'demote')}
              >
                <option value="member">member</option>
                <option value="admin">admin</option>
              </select>
              <button
                class="btn-primary"
                onclick={() => saveRow(u)}
                disabled={busy === `save-${u.id}`}
              >
                Save
              </button>
            </div>

            <div class="acts">
              <button
                class="btn-ghost"
                onclick={() => resetPassword(u)}
                disabled={!!blocked(u, 'reset-password') || busy === `pw-${u.id}`}
              >
                Reset password
              </button>
              <button
                class="btn-ghost"
                onclick={() => resetPin(u)}
                disabled={!!blocked(u, 'reset-pin') || busy === `pin-${u.id}`}
              >
                Reset PIN
              </button>
              {#if u.is_active}
                <button
                  class="btn-ghost"
                  onclick={() => setActive(u, false)}
                  disabled={!!blocked(u, 'disable') || busy === `active-${u.id}`}
                >
                  Disable
                </button>
              {:else}
                <button
                  class="btn-ghost"
                  onclick={() => setActive(u, true)}
                  disabled={busy === `active-${u.id}`}
                >
                  Re-enable
                </button>
              {/if}
              {#if confirming === u.id}
                <button
                  class="btn-ghost danger"
                  onclick={() => remove(u)}
                  disabled={busy === `del-${u.id}`}
                >
                  Confirm delete
                </button>
                <button class="btn-ghost" onclick={() => (confirming = null)}>Cancel</button>
              {:else}
                <button
                  class="btn-ghost danger"
                  onclick={() => (confirming = u.id)}
                  disabled={!!blocked(u, 'delete')}
                >
                  Delete
                </button>
              {/if}
            </div>

            {#each FLOOR_ORDER as action (action)}
              {#if blocked(u, action)}
                <p class="why floor" data-floor={action}>{blocked(u, action)}</p>
              {/if}
            {/each}

            {#if confirming === u.id}
              <p class="why floor">
                Delete keeps nothing: the Ledger goes, and every Tonight session this account
                hosted goes with it, including the other members' answers. Disable keeps both.
              </p>
            {/if}

            <div class="sub" data-testid="user-passkeys">
              <h3 class="data">PASSKEYS</h3>
              {#if !passkeys[u.id]}
                <p class="why">reading this account's credentials…</p>
              {:else if passkeys[u.id].length === 0}
                <p class="why" data-empty="passkeys">No passkey registered on this account.</p>
              {:else}
                <ul class="list">
                  {#each passkeys[u.id] as c (c.id)}
                    <li class:dead={!c.usable} data-testid="user-passkey">
                      <div>
                        <div class="name">{c.label ?? 'Unnamed passkey'}</div>
                        <div class="data meta">
                          added {day(c.created_at)} · last used {day(c.last_used_at)} · {c.rp_id}
                          {#if !c.usable}· registered for a different address — no longer
                            usable{/if}
                        </div>
                      </div>
                      <button
                        class="btn-ghost danger"
                        onclick={() => revokePasskey(u, c.id)}
                        disabled={busy === `key-${u.id}`}
                      >
                        Revoke
                      </button>
                    </li>
                  {/each}
                </ul>
              {/if}
              <p class="why">
                §3.2 keeps password sign-in always available, so revoking the last passkey
                strands nobody — and §3.2 is equally clear that a password reset leaves every
                passkey registered, so a lost device needs this control and not that one.
              </p>
            </div>

            <!-- Here as well as on Connectors: this is the row that just said "needs sign-in". -->
            <div class="sub" data-testid="user-jellyfin">
              <h3 class="data">JELLYFIN</h3>
              {#if !jellyfin}
                <p class="why">reading the connector…</p>
              {:else if !jellyfin.cfg.configured}
                <p class="why">
                  Jellyfin has no URL or API key yet; §3.3's map needs both, and the Connectors
                  tab is where they are set.
                </p>
              {:else}
                <div class="addrow">
                  <select
                    value={link[u.id]?.jellyfin_user_id ?? ''}
                    onchange={(e) =>
                      (link[u.id] = { ...link[u.id], jellyfin_user_id: e.currentTarget.value })}
                    aria-label={`Jellyfin user for ${u.name}`}
                  >
                    <option value="">not linked</option>
                    {#each jellyfin.users as j (j.id)}<option value={j.id}>{j.name}</option>{/each}
                  </select>
                  <button
                    class="btn-primary"
                    onclick={() => linkJellyfin(u)}
                    disabled={!link[u.id]?.jellyfin_user_id || busy === `jf-${u.id}`}
                  >
                    {u.jellyfin_user_id ? 'Re-link' : 'Link'}
                  </button>
                  {#if u.jellyfin_user_id}
                    <button
                      class="btn-ghost"
                      onclick={() => unlinkJellyfin(u)}
                      disabled={busy === `jf-${u.id}`}
                    >
                      Unlink
                    </button>
                  {/if}
                </div>
                {#if jellyfin.users.length === 0}
                  <p class="why">
                    Jellyfin answered with no user list — test the connection on the Connectors
                    tab before picking one here.
                  </p>
                {/if}
                {#if u.has_jellyfin_token}
                  <p class="why" data-jellyfin="token">
                    That Jellyfin user's own token is stored, so Played writes go out under it
                    rather than under the admin key (§7.3).
                  </p>
                {:else}
                  <div class="addrow">
                    <input
                      type="text"
                      placeholder="jellyfin username"
                      value={link[u.id]?.username ?? ''}
                      oninput={(e) =>
                        (link[u.id] = { ...link[u.id], username: e.currentTarget.value })}
                      aria-label={`Jellyfin username for ${u.name}`}
                    />
                    <input
                      type="password"
                      placeholder="password (once)"
                      value={link[u.id]?.password ?? ''}
                      oninput={(e) =>
                        (link[u.id] = { ...link[u.id], password: e.currentTarget.value })}
                      aria-label={`Jellyfin password for ${u.name}`}
                    />
                  </div>
                {/if}
              {/if}
            </div>
          </div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  h1 {
    margin: 0 0 8px;
    font-size: 19px;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 600;
  }
  .warn {
    margin: 12px 0;
    padding: 10px 13px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13px;
  }
  .create {
    margin: 14px 0;
  }
  .addrow {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .addrow input {
    flex: 1;
    min-width: 150px;
  }
  .roster {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .roster li {
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  .roster li.off {
    opacity: 0.6;
  }
  .rowhead {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 12px;
    padding: 11px 13px;
    background: none;
    border: none;
    color: var(--ink-2);
    text-align: left;
    cursor: pointer;
    min-height: var(--touch);
  }
  .who {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .name {
    font-size: 13.5px;
  }
  .facts {
    flex: 1;
  }
  .editor {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 12px 13px 13px;
    border-top: 1px solid var(--line);
  }
  .acts {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .sub {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sub h3 {
    margin: 0;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.08em;
  }
  /* `.sub h3` outranks design.css's coarse `.data` floor, so restate it here, after the rule. */
  @media (pointer: coarse) {
    .sub h3 {
      font-size: 11px;
    }
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 11px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  .list li.dead {
    opacity: 0.62;
  }
  .meta {
    margin-top: 2px;
  }
  .danger {
    color: var(--ember-lift);
  }
  .floor {
    margin: 0;
  }
  .otp {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
    margin: 12px 0;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
    margin: 8px 0;
  }
</style>
