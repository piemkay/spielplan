<script>
  // You (§3.2, decision 527): the avatar on every root opens this sheet. Who is signed in and how,
  // Your taste, switching profile, the account and admin entries, Show the model, and Log out.
  import { authMethodLine, refreshUser, roleWord, session, setShowModel } from '$lib/session.svelte.js';
  import { get, post } from '$lib/api.js';
  import { modelGateSettled } from '$lib/home.svelte.js';
  import { toggleRail } from '$lib/rail.svelte.js';
  import { goto } from '$app/navigation';
  import Avatar from './Avatar.svelte';
  import Sheet from './Sheet.svelte';

  let { onLogout } = $props();

  let open = $state(false);
  let switchable = $state([]);
  let switching = $state(null);
  let pin = $state('');
  let error = $state('');

  const method = $derived(authMethodLine(session.user));
  const tasteEntry = $derived((session.user?.nav?.account ?? []).find((e) => e.key === 'taste'));
  const entries = $derived((session.user?.nav?.account ?? []).filter((e) => e.key !== 'taste'));
  const others = $derived(switchable.filter((u) => u.id !== session.user?.id));
  const showModel = $derived(!!session.user?.show_model);

  async function show() {
    open = true;
    error = '';
    switching = null;
    // Re-read on every open: a PIN set on another phone makes that profile switchable.
    switchable = (await get('/auth/switchable').catch(() => [])) ?? [];
  }

  async function toggleModel() {
    try {
      await setShowModel(!session.user?.show_model);
      // After the await: the server applies the gate, so an earlier re-read gets the pre-toggle payload.
      modelGateSettled();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitPin() {
    error = '';
    try {
      await post('/auth/switch', { user_id: switching.id, pin });
      // The switch response is identity only; re-read for the new profile's nav.
      await refreshUser();
      open = false;
      pin = '';
      switching = null;
      await goto('/');
      location.reload();
    } catch (err) {
      error = err.message;
      pin = '';
    }
  }
</script>

{#snippet chevron()}
  <svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9.5 5.5 6.5 6.5-6.5 6.5" /></svg>
{/snippet}

<button
  class="avatar-btn hit"
  onclick={show}
  aria-haspopup="dialog"
  aria-expanded={open}
  aria-label="You — {session.user?.name ?? 'signed out'}"
  data-testid="account-chip"
>
  <Avatar name={session.user?.name} person={session.user} size={32} />
</button>

<Sheet {open} onClose={() => (open = false)} label="You" width={480}>
  {#snippet header(close)}
    <div class="bar">
      <button class="btn-plain done" onclick={close}>Done</button>
    </div>
  {/snippet}
  {#snippet children(close)}
    <div class="you">
      <div class="head">
        <Avatar name={session.user?.name} person={session.user} size={72} />
        <h2 class="title-1">{session.user?.name}</h2>
        <p class="line" data-testid="account-line">{roleWord(session.user?.role)} · {method}</p>
      </div>

      {#if error}<p class="err" role="alert">{error}</p>{/if}

      {#if switching}
        <section class="group">
          <h3 class="list-header">PIN for {switching.name}</h3>
          <div class="pinbox">
            <input
              type="password"
              inputmode="numeric"
              autocomplete="off"
              aria-label="PIN for {switching.name}"
              bind:value={pin}
              placeholder="Four digits"
              onkeydown={(e) => e.key === 'Enter' && submitPin()}
            />
            <div class="row">
              <button class="btn-primary" onclick={submitPin}>Switch</button>
              <button class="btn-secondary" onclick={() => (switching = null)}>Cancel</button>
            </div>
          </div>
        </section>
      {:else}
        {#if tasteEntry}
          <section class="group">
            <div class="list-group">
              <a class="list-row" href={tasteEntry.href} data-nav="taste" data-testid="taste-row" data-sveltekit-replacestate>
                <span>{tasteEntry.label}</span>
                {@render chevron()}
              </a>
            </div>
            <p class="list-footer">What sits high on your ladder, and where you and someone else meet and part.</p>
          </section>
        {/if}

        <section class="group">
          <h3 class="list-header">Switch profile</h3>
          <div class="list-group">
            {#each others as u (u.id)}
              <button
                class="list-row"
                onclick={() => {
                  switching = u;
                  pin = '';
                }}
              >
                <Avatar name={u.name} person={u} />
                <span>{u.name}</span>
              </button>
            {:else}
              <div class="list-row muted">No one else yet</div>
            {/each}
          </div>
          {#if !others.length}
            <p class="list-footer">
              People appear here once they set a PIN on
              <a href="/account" data-sveltekit-replacestate>their account page</a>.
            </p>
          {/if}
        </section>

        <!-- Entries come from the server's nav payload: a member's browser never receives admin links. -->
        <section class="group">
          <div class="list-group">
            {#each entries as entry (entry.key)}
              <a class="list-row" href={entry.href} data-nav={entry.key} data-sveltekit-replacestate>
                <span>{entry.label}</span>
                {@render chevron()}
              </a>
            {/each}
          </div>
        </section>

        <section class="group">
          <div class="list-group">
            <div class="list-row">
              <span id="show-model-label">Show the numbers</span>
              <button
                class="switch"
                role="switch"
                aria-checked={showModel}
                aria-labelledby="show-model-label"
                onclick={toggleModel}
                data-testid="show-model-toggle"
                data-on={showModel}
              ><span class="knob"></span></button>
            </div>
            {#if showModel}
              <button
                class="list-row"
                onclick={() => {
                  close();
                  toggleRail(true);
                }}
                data-testid="model-rail-open"
              >
                <span>Model log</span>
              </button>
            {/if}
          </div>
          <p class="list-footer" data-testid="show-model-hint">
            Shows the numbers behind your suggestions, and a log of what changed them.
          </p>
        </section>
      {/if}

      <section class="group">
        <div class="list-group">
          <button class="list-row logout" onclick={onLogout}>Log out</button>
        </div>
      </section>
    </div>
  {/snippet}
</Sheet>

<style>
  .avatar-btn {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    min-height: 44px;
    margin-right: -6px;
    padding: 0;
    border: none;
    background: none;
  }
  .bar {
    display: flex;
    justify-content: flex-end;
    margin: -4px -8px 0;
  }
  .done {
    font-weight: 600;
  }
  .you {
    display: flex;
    flex-direction: column;
    gap: 28px;
  }
  .head {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    text-align: center;
  }
  .head .title-1 {
    margin-top: 6px;
  }
  .line {
    margin: 0;
    font-size: var(--fs-subhead);
    color: var(--text-2);
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .muted {
    color: var(--text-3);
  }
  .chev {
    margin-left: auto;
    color: rgba(245, 240, 232, 0.35);
  }
  .list-row[href] {
    color: var(--text);
  }
  .logout {
    justify-content: center;
    color: var(--negative);
  }
  .pinbox {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .row {
    display: flex;
    gap: 8px;
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
    text-align: center;
  }
</style>
