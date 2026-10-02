<script>
  // You (§3.2, decisions 527 and 553): the avatar on every root opens this sheet. Who is signed in and
  // how, Your taste and the wish list, switching profile, the account and admin entries, Show the
  // model, and Log out. No footnotes: each row's label says what it opens.
  import { authMethodLine, refreshUser, roleWord, session, setShowModel } from '$lib/session.svelte.js';
  import { get, post } from '$lib/api.js';
  import { modelGateSettled } from '$lib/home.svelte.js';
  import { toggleRail } from '$lib/rail.svelte.js';
  import { loadWishSummary } from '$lib/wish.svelte.js';
  import { goto } from '$app/navigation';
  import Avatar from './Avatar.svelte';
  import Sheet from './Sheet.svelte';
  import TitleDetail from './TitleDetail.svelte';
  import WishListSheet from './WishListSheet.svelte';

  let { onLogout } = $props();

  let open = $state(false);
  let switchable = $state([]);
  let switching = $state(null);
  let pin = $state('');
  let error = $state('');
  // The household's wanted count, Wish list's value; null until read.
  let wanted = $state(null);
  let wishOpen = $state(false);
  // A title opened from the wish list.
  let selected = $state(null);

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
    const [who, summary] = await Promise.all([
      get('/auth/switchable').catch(() => []),
      loadWishSummary().catch(() => null)
    ]);
    switchable = who ?? [];
    wanted = summary?.wanted ?? null;
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
        <section class="group">
          <div class="list-group">
            {#if tasteEntry}
              <a class="list-row" href={tasteEntry.href} data-nav="taste" data-testid="taste-row" data-sveltekit-replacestate>
                <span>{tasteEntry.label}</span>
                {@render chevron()}
              </a>
            {/if}
            <button
              class="list-row"
              data-testid="you-wish-row"
              aria-label={wanted === null ? undefined : `Wish list, ${wanted.toLocaleString()} wanted`}
              onclick={() => (wishOpen = true)}
            >
              <span>Wish list</span>
              {#if wanted !== null}<span class="value">{wanted.toLocaleString()}</span>{/if}
              {@render chevron()}
            </button>
          </div>
        </section>

        <!-- Only when someone else can be switched to (decision 553); the account page says how. -->
        {#if others.length}
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
              {/each}
            </div>
          </section>
        {/if}

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

<!-- Over You, so Back returns to it; a title opened from the list stacks over both. -->
<WishListSheet open={wishOpen} onClose={() => (wishOpen = false)} onSelect={(title) => (selected = title)} />
{#if selected}
  <TitleDetail
    titleId={selected.id}
    seed={selected}
    onClose={() => (selected = null)}
    onPerson={() => (selected = null)}
    onStateChange={() => {}}
  />
{/if}

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
  .chev {
    margin-left: auto;
    color: rgba(245, 240, 232, 0.35);
  }
  .value {
    font-variant-numeric: tabular-nums;
  }
  .value + .chev {
    margin-left: 0;
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
