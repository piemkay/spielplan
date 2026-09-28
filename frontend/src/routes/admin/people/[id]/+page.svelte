<script>
  // One person (§6.6). The three floors are enforced here as well as at the route, and a disabled
  // control says which one it stands on; every destructive action asks first.
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { api, get, post } from '$lib/api.js';
  import { jellyfinDirectory } from '$lib/jellyfin.js';
  import { session } from '$lib/session.svelte.js';
  import { showToast } from '$lib/toast.svelte.js';
  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import OneTimePassword from '../OneTimePassword.svelte';
  import { avatarColour, roleWord } from '../people.js';

  const id = $derived(Number($page.params.id));

  let rows = $state(null);
  let passkeys = $state(null);
  let jellyfin = $state(null);
  let error = $state('');
  let busy = $state(false);
  let issued = $state(null);
  /** ActionSheet's props: one choice, or a question with its one answer. */
  /** @type {null | { title: string, options: Array<any> }} */
  let asking = $state(null);
  /** @type {null | 'name' | 'jellyfin'} */
  let editing = $state(null);
  let draftName = $state('');
  let link = $state({ jellyfin_user_id: '', username: '', password: '' });

  const u = $derived(rows?.find((r) => r.id === id) ?? null);
  // For the controls only: the server counts again inside the writing transaction.
  const activeAdmins = $derived((rows ?? []).filter((r) => r.role === 'admin' && r.is_active).length);
  const lastAdmin = $derived(!!u && u.role === 'admin' && u.is_active && activeAdmins === 1);
  const self = $derived(!!u && u.id === session.user?.id);
  const linkedName = $derived(
    jellyfin?.users.find((j) => j.id === u?.jellyfin_user_id)?.name ?? 'a Jellyfin user'
  );

  onMount(async () => {
    await load();
    if (!u) return;
    try {
      [passkeys, jellyfin] = await Promise.all([
        get(`/admin/users/${id}/passkeys`).then((list) => list ?? []),
        jellyfinDirectory()
      ]);
    } catch (err) {
      error = err.message || String(err);
    }
  });

  async function load() {
    try {
      rows = (await get('/admin/users')) ?? [];
    } catch (err) {
      error = err.message || String(err);
    }
  }

  // Every write re-reads the roster rather than reconciling the fragment each route returns.
  async function run(fn, done = '') {
    error = '';
    busy = true;
    try {
      await fn();
      await load();
      if (done) showToast(done);
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = false;
    }
  }

  const ask = (title, label, onSelect) => {
    asking = { title, options: [{ label, destructive: true, onSelect }] };
  };

  function saveName(event) {
    event.preventDefault();
    const name = draftName.trim();
    editing = null;
    if (!name || name === u.name) return;
    run(() => api(`/admin/users/${id}`, { method: 'PATCH', body: { name } }), 'Name changed');
  }

  function chooseRole() {
    const set = (role) => () => {
      if (role !== u.role) run(() => api(`/admin/users/${id}`, { method: 'PATCH', body: { role } }), 'Role changed');
    };
    asking = {
      title: `What can ${u.name} do?`,
      options: [
        { label: 'Member', detail: 'Uses the app', checked: u.role === 'member', onSelect: set('member') },
        { label: 'Admin', detail: 'Also runs it', checked: u.role === 'admin', onSelect: set('admin') }
      ]
    };
  }

  const resetPassword = () =>
    ask(
      `${u.name} is signed out everywhere and gets a one-time password to sign in with.`,
      'Reset password',
      () =>
        run(async () => {
          const res = await post(`/admin/users/${id}/reset-password`);
          issued = { name: u.name, password: res.one_time_password };
        })
    );

  const resetPin = () =>
    ask(`${u.name} can set a new PIN on their account page.`, 'Remove PIN', () =>
      run(() => post(`/admin/users/${id}/reset-pin`), 'PIN removed')
    );

  const revoke = (credential) =>
    ask(
      `${credential.label ?? 'This passkey'} stops working. ${u.name} can still sign in with their password.`,
      'Revoke passkey',
      () =>
        run(async () => {
          await api(`/admin/users/${id}/passkeys/${encodeURIComponent(credential.id)}`, {
            method: 'DELETE'
          });
          passkeys = (await get(`/admin/users/${id}/passkeys`)) ?? [];
        }, 'Passkey revoked')
    );

  function openLink() {
    link = { jellyfin_user_id: u.jellyfin_user_id ?? '', username: '', password: '' };
    editing = 'jellyfin';
  }

  function saveLink(event) {
    event.preventDefault();
    const { jellyfin_user_id, username, password } = link;
    editing = null;
    run(
      () =>
        post(`/admin/users/${id}/jellyfin`, {
          jellyfin_user_id,
          // Optional: with it, watched status can be written as that person (§7.3).
          jellyfin_username: username || null,
          jellyfin_password: password || null
        }),
      'Linked to Jellyfin'
    );
  }

  const unlink = () =>
    ask(`Watched status stops syncing for ${u.name}. Their ratings stay.`, 'Unlink', () =>
      run(() => api(`/admin/users/${id}/jellyfin`, { method: 'DELETE' }), 'Unlinked')
    );

  const disable = () =>
    ask(
      `${u.name} is signed out and can't sign in until you turn the account back on. Their ratings stay.`,
      'Disable account',
      () => run(() => post(`/admin/users/${id}/active`, { is_active: false }), 'Account disabled')
    );

  const enable = () =>
    run(() => post(`/admin/users/${id}/active`, { is_active: true }), 'Account turned back on');

  const remove = () =>
    ask(
      `This removes ${u.name}'s ratings and every Tonight they hosted, with everyone's ` +
        "answers in it. It can't be undone.",
      `Delete ${u.name}`,
      async () => {
        const name = u.name;
        error = '';
        busy = true;
        try {
          await api(`/admin/users/${id}`, { method: 'DELETE' });
          showToast(`${name} deleted`);
          await goto('/admin/people', { replaceState: true });
        } catch (err) {
          error = err.message || String(err);
        } finally {
          busy = false;
        }
      }
    );

  const day = (iso) =>
    iso ? new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : 'never';
</script>

<div class="person">
  {#if !rows}
    {#if error}<p class="err" role="alert">{error}</p>{:else}<p class="footnote">Loading…</p>{/if}
  {:else if !u}
    <h1 class="large-title">Not here</h1>
    <p class="footnote">This person isn't in the household any more.</p>
  {:else}
    <header class="head">
      <span class="avatar" style:background={avatarColour(u)} aria-hidden="true">
        {u.name.charAt(0).toUpperCase()}
      </span>
      <h1 class="title-1">{u.name}</h1>
      <p class="role">{roleWord(u.role)}{u.is_active ? '' : ' · disabled'}</p>
    </header>

    {#if error}<p class="err" role="alert">{error}</p>{/if}

    {#if issued}
      <OneTimePassword name={issued.name} password={issued.password} onDone={() => (issued = null)} />
    {/if}

    <section class="group">
      <h2 class="list-header">Profile</h2>
      <div class="list-group">
        <button
          class="list-row"
          onclick={() => {
            draftName = u.name;
            editing = 'name';
          }}
          disabled={busy}
        >
          <span class="name">Name</span>
          <span class="value">{u.name}</span>
          <span class="chev"><Icon name="chevron-right" size={16} /></span>
        </button>
        <button
          class="list-row"
          aria-label="Role for {u.name}"
          onclick={chooseRole}
          disabled={busy || lastAdmin}
          data-floor={lastAdmin ? 'demote' : undefined}
        >
          <span class="name">Role</span>
          <span class="value">{roleWord(u.role)}</span>
          <span class="chev"><Icon name="chevron-right" size={16} /></span>
        </button>
      </div>
      {#if lastAdmin}
        <p class="list-footer">
          {u.name} is the only admin, so they stay one. Make someone else an admin first.
        </p>
      {/if}
    </section>

    <section class="group" data-testid="user-signin">
      <h2 class="list-header">Sign-in</h2>
      <div class="list-group">
        <button
          class="list-row action"
          onclick={resetPassword}
          disabled={busy || self}
          data-floor={self ? 'reset-password' : undefined}
        >
          Reset password…
        </button>
        {#if u.has_pin}
          <button
            class="list-row"
            onclick={resetPin}
            disabled={busy || self}
            data-floor={self ? 'reset-pin' : undefined}
          >
            <span class="name">PIN for switching</span>
            <span class="value">Set</span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </button>
        {:else}
          <div class="list-row" data-floor={self ? 'reset-pin' : undefined}>
            <span class="name">PIN for switching</span>
            <span class="value">Not set</span>
          </div>
        {/if}
        <div class="list-row" data-testid="user-passkeys">
          <span class="name">Passkeys</span>
          <span class="value">{u.passkeys === 0 ? 'None' : u.passkeys}</span>
        </div>
        {#each passkeys ?? [] as credential (credential.id)}
          <div class="list-row sub-row" class:dead={!credential.usable} data-testid="user-passkey">
            <span class="text">
              <span>{credential.label ?? 'Unnamed passkey'}</span>
              <span class="sub">
                {credential.usable
                  ? `Added ${day(credential.created_at)} · last used ${day(credential.last_used_at)}`
                  : 'Made for a different address, so it no longer works'}
              </span>
            </span>
            <button class="btn-plain btn-destructive" onclick={() => revoke(credential)} disabled={busy}>
              Revoke
            </button>
          </div>
        {/each}
      </div>
      <p class="list-footer">
        {#if self}
          You can't reset your own password or PIN here. Change them on
          <a href="/account">your account page</a>.
        {:else}
          A password reset keeps their passkeys; revoke a passkey here when a device is lost.
        {/if}
      </p>
    </section>

    <section class="group" data-testid="user-jellyfin">
      <h2 class="list-header">Jellyfin</h2>
      <div class="list-group">
        {#if !jellyfin}
          <div class="list-row"><span class="name muted">Checking Jellyfin…</span></div>
        {:else if !jellyfin.cfg.configured}
          <a class="list-row" href="/admin/services">
            <span class="name">Jellyfin isn't set up yet</span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </a>
        {:else}
          {#if u.jellyfin_user_id}
            <div
              class="list-row"
              data-jellyfin={u.has_jellyfin_token ? 'token' : 'no-token'}
              data-link-state={u.jellyfin_link_state}
            >
              <span class="text">
                <span>Linked to {linkedName}</span>
                <span class="sub">
                  {u.has_jellyfin_token
                    ? 'Watched status syncs both ways'
                    : 'Needs their Jellyfin sign-in once so watched status can sync'}
                </span>
              </span>
              <span class="state" class:ok={u.has_jellyfin_token}>
                <Icon name={u.has_jellyfin_token ? 'check' : 'warning'} size={18} />
                <span class="sr">{u.has_jellyfin_token ? 'Syncing' : 'Needs sign-in'}</span>
              </span>
            </div>
          {:else}
            <div class="list-row" data-link-state="unlinked">
              <span class="name">Not linked</span>
            </div>
          {/if}
          <button class="list-row" onclick={openLink} disabled={busy}>
            <span class="name">{u.jellyfin_user_id ? 'Change link' : 'Link to Jellyfin'}</span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </button>
          {#if u.jellyfin_user_id}
            <button class="list-row danger" onclick={unlink} disabled={busy}>Unlink</button>
          {/if}
        {/if}
      </div>
    </section>

    <section class="group" data-testid="user-account">
      <h2 class="list-header">Account</h2>
      <div class="list-group">
        {#if u.is_active}
          <button
            class="list-row"
            onclick={disable}
            disabled={busy || lastAdmin || self}
            data-floor={lastAdmin || self ? 'disable' : undefined}
          >
            <span class="name">Disable account</span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </button>
        {:else}
          <button class="list-row" onclick={enable} disabled={busy}>
            <span class="name">Turn account back on</span>
          </button>
        {/if}
        <button
          class="list-row danger"
          onclick={remove}
          disabled={busy || lastAdmin}
          data-floor={lastAdmin ? 'delete' : undefined}
        >
          Delete {u.name}…
        </button>
      </div>
      <p class="list-footer">
        {#if lastAdmin}
          The only admin can't be disabled or deleted.
        {:else if self}
          You can't disable your own account.
        {/if}
        Deleting can't be undone.
      </p>
    </section>
  {/if}
</div>

<ActionSheet
  open={!!asking}
  title={asking?.title}
  options={asking?.options ?? []}
  onClose={() => (asking = null)}
/>

<Sheet open={editing === 'name'} onClose={() => (editing = null)} label="Change name" detent="fit">
  {#snippet children()}
    <form class="edit" onsubmit={saveName}>
      <h2 class="section-title">Name</h2>
      <input type="text" bind:value={draftName} aria-label="Name for {u?.name}" autocomplete="off" />
      <button class="btn-primary" type="submit" disabled={!draftName.trim()}>Save</button>
    </form>
  {/snippet}
</Sheet>

<Sheet open={editing === 'jellyfin'} onClose={() => (editing = null)} label="Link to Jellyfin" detent="fit">
  {#snippet children()}
    <form class="edit" onsubmit={saveLink}>
      <h2 class="section-title">Link {u?.name} to Jellyfin</h2>
      <select bind:value={link.jellyfin_user_id} aria-label="Jellyfin user for {u?.name}">
        <option value="">Choose a Jellyfin user</option>
        {#each jellyfin?.users ?? [] as j (j.id)}<option value={j.id}>{j.name}</option>{/each}
      </select>
      {#if jellyfin && jellyfin.users.length === 0}
        <p class="footnote">Jellyfin didn't list any users. Test the connection in Services first.</p>
      {/if}
      <input
        type="text"
        bind:value={link.username}
        placeholder="Their Jellyfin username"
        aria-label="Jellyfin username for {u?.name}"
        autocomplete="off"
      />
      <input
        type="password"
        bind:value={link.password}
        placeholder="Their Jellyfin password"
        aria-label="Jellyfin password for {u?.name}"
        autocomplete="off"
      />
      <p class="footnote">
        Optional. With their Jellyfin sign-in, watched status syncs both ways. The password is used
        once and not kept.
      </p>
      <button class="btn-primary" type="submit" disabled={!link.jellyfin_user_id}>Link</button>
    </form>
  {/snippet}
</Sheet>

<style>
  .person {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .head {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    text-align: center;
  }
  .avatar {
    display: grid;
    place-items: center;
    width: 72px;
    height: 72px;
    margin-bottom: 8px;
    border-radius: var(--r-pill);
    color: #fff;
    font-size: var(--fs-title);
    font-weight: 600;
  }
  .role {
    margin: 0;
    font-size: var(--fs-subhead);
    color: var(--text-2);
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  .list-row {
    color: var(--text);
  }
  .list-row:disabled {
    cursor: default;
  }
  .list-row:disabled > *,
  .list-row.action:disabled,
  .list-row.danger:disabled {
    opacity: 0.45;
  }
  .action {
    color: var(--accent-text);
  }
  .danger {
    color: var(--negative);
  }
  .name {
    flex: 1;
  }
  .value {
    color: var(--text-3);
    text-align: right;
  }
  .muted {
    color: var(--text-3);
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .sub-row {
    min-height: 60px;
  }
  .dead .text {
    color: var(--text-3);
  }
  .chev {
    display: grid;
    flex: none;
    color: rgba(245, 240, 232, 0.35);
  }
  .state {
    display: grid;
    color: var(--warning);
  }
  .state.ok {
    color: var(--positive);
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .footnote {
    margin: 0;
  }
  .edit {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding-top: 4px;
  }

  @media (min-width: 721px) {
    .person {
      max-width: 640px;
    }
  }
</style>
