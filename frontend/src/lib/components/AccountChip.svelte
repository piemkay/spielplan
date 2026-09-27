<script>
  // You (§3.2, decision 527): the avatar on every root opens this sheet. Who is signed in and how,
  // switching profile, the account and admin entries, Show the model, and Log out.
  import { authMethodLine, refreshUser, roleWord, session, setShowModel } from '$lib/session.svelte.js';
  import { get, post } from '$lib/api.js';
  import { modelGateSettled } from '$lib/home.svelte.js';
  import { toggleRail } from '$lib/rail.svelte.js';
  import { goto } from '$app/navigation';
  import Sheet from './Sheet.svelte';

  let { onLogout } = $props();

  let open = $state(false);
  let switchable = $state([]);
  let switching = $state(null);
  let pin = $state('');
  let error = $state('');

  const initial = $derived((session.user?.name ?? '?').charAt(0).toUpperCase());
  const method = $derived(authMethodLine(session.user));
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

<button
  class="avatar-btn hit"
  onclick={show}
  aria-haspopup="dialog"
  aria-expanded={open}
  aria-label="You — {session.user?.name ?? 'signed out'}"
  data-testid="account-chip"
>
  <span class="avatar">{initial}</span>
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
        <span class="avatar big">{initial}</span>
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
                <span class="avatar sm" style:background={u.colour ?? null}>{u.name.charAt(0).toUpperCase()}</span>
                <span>{u.name}</span>
              </button>
            {:else}
              <div class="list-row muted">No one else yet</div>
            {/each}
          </div>
          {#if !others.length}
            <p class="list-footer">
              People appear here once they set a PIN on
              <a href="/account" onclick={() => close()}>their account page</a>.
            </p>
          {/if}
        </section>

        <!-- Entries come from the server's nav payload: a member's browser never receives admin links. -->
        <section class="group">
          <div class="list-group">
            {#each session.user?.nav?.account ?? [] as entry (entry.key)}
              <a class="list-row" href={entry.href} data-nav={entry.key}>
                <span>{entry.label}</span>
                <svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9.5 5.5 6.5 6.5-6.5 6.5" /></svg>
              </a>
            {/each}
          </div>
        </section>

        <section class="group">
          <div class="list-group">
            <div class="list-row">
              <span id="show-model-label">Show the numbers</span>
              <button
                class="switch hit"
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
  .avatar {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border-radius: var(--r-pill);
    background: #3d6fb6;
    color: #fff;
    font-size: var(--fs-footnote);
    font-weight: 600;
  }
  .avatar.big {
    width: 72px;
    height: 72px;
    font-size: var(--fs-title);
  }
  .avatar.sm {
    width: 30px;
    height: 30px;
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
  .switch {
    position: relative;
    flex: none;
    margin-left: auto;
    width: 51px;
    height: 31px;
    min-height: 31px;
    padding: 2px;
    border: none;
    border-radius: var(--r-pill);
    background: rgba(245, 240, 232, 0.16);
    transition: background 0.2s var(--ease);
  }
  .switch[aria-checked='true'] {
    background: var(--accent);
  }
  .knob {
    display: block;
    width: 27px;
    height: 27px;
    border-radius: var(--r-pill);
    background: var(--text);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
    transition: transform 0.2s var(--ease);
  }
  .switch[aria-checked='true'] .knob {
    transform: translateX(20px);
  }
</style>
