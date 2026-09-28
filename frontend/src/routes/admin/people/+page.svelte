<script>
  // The household's accounts (§6.6), and the only place one is made (decision 166).
  import { onMount } from 'svelte';
  import { get, post } from '$lib/api.js';
  import { session } from '$lib/session.svelte.js';
  import Icon from '$lib/components/Icon.svelte';
  import OneTimePassword from './OneTimePassword.svelte';
  import { avatarColour, signIn, subtitle } from './people.js';

  let rows = $state(null);
  let error = $state('');
  let busy = $state(false);
  let issued = $state(null);
  let name = $state('');
  let role = $state('member');

  const withPasskey = $derived((rows ?? []).filter((u) => u.passkeys > 0).length);

  onMount(load);

  async function load() {
    try {
      rows = (await get('/admin/users')) ?? [];
    } catch (err) {
      error = err.message || String(err);
    }
  }

  async function add(event) {
    event.preventDefault();
    error = '';
    busy = true;
    try {
      const made = await post('/admin/users', { name: name.trim(), role });
      issued = { name: made.name, password: made.one_time_password };
      name = '';
      role = 'member';
      await load();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = false;
    }
  }
</script>

<div class="people">
  <h1 class="large-title">People</h1>
  <p class="intro">
    Everyone signs in with their own password or passkey. You never see anyone's password.
  </p>

  {#if error}<p class="err" role="alert">{error}</p>{/if}

  {#if rows}
    <section class="group">
      <div class="list-group" data-testid="users-roster">
        {#each rows as u (u.id)}
          <a
            class="list-row person"
            class:off={!u.is_active}
            href="/admin/people/{u.id}"
            data-testid="user-row"
            data-user-id={u.id}
          >
            <span class="avatar" style:background={avatarColour(u)} aria-hidden="true">
              {u.name.charAt(0).toUpperCase()}
            </span>
            <span class="text">
              <span>{u.name}</span>
              <span class="sub" data-user-facts>{subtitle(u, session.user?.id)}</span>
              <span class="sub" data-user-signin>{signIn(u)}</span>
            </span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </a>
        {/each}
      </div>
      <p class="list-footer">
        {#if withPasskey === 0}
          Nobody has a passkey yet.
        {:else}
          {withPasskey} of {rows.length}
          {withPasskey === 1 ? 'has' : 'have'} a passkey.
        {/if}
      </p>
    </section>
  {:else if !error}
    <p class="footnote">Loading…</p>
  {/if}

  <section class="card add" data-testid="user-create">
    <h2>Add someone</h2>
    <p class="note">They get a one-time password for their first sign-in.</p>
    <form onsubmit={add}>
      <input
        type="text"
        bind:value={name}
        placeholder="Name"
        aria-label="New person's name"
        autocomplete="off"
      />
      <div class="segmented" role="group" aria-label="Role">
        <button type="button" aria-pressed={role === 'member'} onclick={() => (role = 'member')}>
          Member
        </button>
        <button type="button" aria-pressed={role === 'admin'} onclick={() => (role = 'admin')}>
          Admin
        </button>
      </div>
      <button class="btn-tinted" type="submit" disabled={busy || !name.trim()}>
        <Icon name="plus" size={18} />
        {busy ? 'Adding…' : 'Add a person'}
      </button>
    </form>
  </section>

  {#if issued}
    <OneTimePassword name={issued.name} password={issued.password} onDone={() => (issued = null)} />
  {/if}

  <!-- The address itself, so it can be compared with the one on the phone in hand. -->
  <p class="footnote" data-testid="users-public-url">
    {#if session.publicUrl}
      Passkeys only work at {session.publicUrl}. If that address changes, everyone adds their
      passkeys again.
    {:else}
      Passkeys need the app's public address, and none is set.
    {/if}
  </p>
</div>

<style>
  .people {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .intro {
    margin: -16px 0 8px;
    font-size: var(--fs-subhead);
    line-height: 20px;
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
  .person {
    min-height: 64px;
    color: var(--text);
  }
  .list-row + .list-row {
    box-shadow: none;
    background: linear-gradient(var(--separator), var(--separator)) 64px 0 / 100% 0.5px no-repeat;
  }
  .off {
    color: var(--text-3);
  }
  .avatar {
    display: grid;
    place-items: center;
    flex: none;
    width: 36px;
    height: 36px;
    border-radius: var(--r-pill);
    color: #fff;
    font-size: var(--fs-subhead);
    font-weight: 600;
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
  .chev {
    display: grid;
    flex: none;
    color: rgba(245, 240, 232, 0.35);
  }
  .add {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  h2 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .note {
    margin: 0 0 12px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  form .btn-tinted {
    align-self: flex-start;
  }
  .footnote {
    margin: 0;
    padding: 0 var(--gutter);
    overflow-wrap: anywhere;
  }

  @media (min-width: 721px) {
    .people {
      max-width: 640px;
    }
  }
</style>
