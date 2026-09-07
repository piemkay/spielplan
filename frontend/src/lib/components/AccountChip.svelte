<script>
  /**
   * Account chip. Spec v2.1 §3.2:
   *   "the account chip switches between member profiles, gated by the per-user PIN
   *    (the chip reads 'member · passkey + PIN'). Logout clears the session cookie only —
   *    passkeys remain registered."
   */
  import { authMethodLine, refreshUser, session, setShowModel } from '$lib/session.svelte.js';
  import { get, post } from '$lib/api.js';
  import { modelGateSettled } from '$lib/home.svelte.js';
  import { goto } from '$app/navigation';

  let { onLogout } = $props();

  let open = $state(false);
  let switchable = $state([]);
  let switching = $state(null);
  let pin = $state('');
  let error = $state('');

  const initial = $derived((session.user?.name ?? '?').charAt(0).toUpperCase());
  // fe-13: this was the constant 'passkey + PIN' for everyone, so a new member with neither
  // read their own chip as a claim about credentials they did not have. §3.2's own example
  // string is an inventory; `authMethodLine` reads the inventory `/auth/me` already carries.
  const method = $derived(authMethodLine(session.user));
  const others = $derived(switchable.filter((u) => u.id !== session.user?.id));

  async function toggle() {
    open = !open;
    error = '';
    switching = null;
    // Re-read on every open, not only when the list is empty: a PIN set on the other person's
    // phone makes them switchable here, and the guard that skipped the refetch meant the only
    // way to see it was a page reload (fe-13).
    if (open) {
      switchable = (await get('/auth/switchable').catch(() => [])) ?? [];
    }
  }

  async function toggleModel() {
    try {
      await setShowModel(!session.user?.show_model);
      // AFTER the await, not before. Decision 117's gate is applied by the server, so a
      // surface that re-reads its payload on the optimistic local flip races this write and
      // gets the pre-toggle answer back — the switch moves and the rail never appears.
      modelGateSettled();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitPin() {
    error = '';
    try {
      await post('/auth/switch', { user_id: switching.id, pin });
      // The switch response is identity only; the new profile's navigation is a different
      // answer (§6.6 is admin-role only), so re-read rather than patching the old object.
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

<div class="wrap">
  <button class="chip" onclick={toggle} aria-expanded={open} data-testid="account-chip">
    <span class="avatar">{initial}</span>
    <span>{session.user?.name ?? 'signed out'}</span>
    <span class="data">▾</span>
  </button>

  {#if open}
    <div class="menu">
      <div class="head">
        <div class="name">{session.user?.name}</div>
        <div class="data">{session.user?.role} · {method}</div>
      </div>

      {#if switching}
        <div class="pinbox">
          <div class="data">PIN for {switching.name}</div>
          <input
            type="password"
            inputmode="numeric"
            bind:value={pin}
            placeholder="••••"
            onkeydown={(e) => e.key === 'Enter' && submitPin()}
          />
          {#if error}<div class="err data">{error}</div>{/if}
          <div class="row">
            <button class="btn-primary" onclick={submitPin}>Switch</button>
            <button class="btn-ghost" onclick={() => (switching = null)}>Cancel</button>
          </div>
        </div>
      {:else}
        <!-- §6.6 is admin-role only. The entries come from the server's nav payload, so a
             member's browser never receives the admin links at all — hidden, not disabled. -->
        <div class="group">
          {#each session.user?.nav?.account ?? [] as entry (entry.key)}
            <a href={entry.href} data-nav={entry.key}>{entry.label}</a>
          {/each}
        </div>

        <!-- §6.7, owner decision 2026-08-29: one global per-user "show the model" toggle,
             default off, here rather than on a settings page — it is a debugging instrument
             reached often and briefly, and this dropdown is on every screen. It governs the
             transparency rail and the inline numeric annotations; the title card's model line
             is deliberately not gated (§6.0). -->
        <div class="group bordered">
          <button
            class="pref"
            role="switch"
            aria-checked={!!session.user?.show_model}
            onclick={toggleModel}
            data-testid="show-model-toggle"
            data-on={!!session.user?.show_model}
          >
            <span class="track" class:on={session.user?.show_model}><span class="knob"></span></span>
            <span class="preflabel">Show the model</span>
          </button>
          <div class="why hint">
            the §6.7 event rail and every inline number — the title card's b(t) · β · gate line
            is not gated and stays either way
          </div>
        </div>

        {#if others.length}
          <div class="group bordered">
            <div class="data heading">SWITCH USER</div>
            {#each others as u (u.id)}
              <button
                class="switch"
                onclick={() => {
                  switching = u;
                  pin = '';
                }}
              >
                <span class="avatar sm" style:background={u.colour ?? 'var(--card-raised)'}>
                  {u.name.charAt(0).toUpperCase()}
                </span>
                {u.name}
              </button>
            {/each}
          </div>
        {:else}
          <!-- The state every household starts in had no branch at all, so the section simply
               was not there and §3.2's switch looked unimplemented. A profile becomes
               switchable by setting a PIN, and that is on the account page (fe-13). -->
          <div class="group bordered">
            <div class="data heading">SWITCH USER</div>
            <div class="why hint">
              Nobody else has a switch PIN yet — a profile joins this list once it has one.
              <a href="/account">Set yours on the account page.</a>
            </div>
          </div>
        {/if}
      {/if}

      <div class="group bordered">
        <button class="danger" onclick={onLogout}>Log out</button>
      </div>
    </div>
  {/if}
</div>

<style>
  .wrap {
    position: relative;
    flex: none;
  }
  .chip {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 11px 5px 5px;
    border-radius: var(--r-pill);
    border: 1px solid var(--line-2);
    background: transparent;
    color: var(--ink-2);
    font-size: 12.5px;
    cursor: pointer;
  }
  .avatar {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    background: var(--ember);
    color: var(--ember-ink);
    font-size: 11px;
    font-weight: 700;
  }
  .avatar.sm {
    width: 20px;
    height: 20px;
    font-size: 10px;
  }
  .menu {
    position: absolute;
    top: 40px;
    right: 0;
    width: 248px;
    border-radius: var(--r-md);
    border: 1px solid var(--line-3);
    background: var(--card-raised);
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
    overflow: hidden;
    z-index: 40;
    animation: fadeIn 0.12s ease;
  }
  .head {
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
  }
  .name {
    font-size: 13px;
    font-weight: 600;
  }
  .group {
    padding: 6px;
    display: flex;
    flex-direction: column;
  }
  .bordered {
    border-top: 1px solid var(--line);
  }
  .heading {
    letter-spacing: 0.12em;
    padding: 6px 10px 4px;
  }
  .group a,
  .switch,
  .danger {
    padding: 9px 10px;
    border-radius: var(--r-sm);
    font-size: 12.5px;
    color: var(--ink-2);
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .group a:hover,
  .switch:hover {
    background: rgba(255, 255, 255, 0.06);
  }
  .danger {
    color: var(--ember-lift);
  }
  .danger:hover {
    background: var(--ember-wash);
  }
  .pinbox {
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .row {
    display: flex;
    gap: 8px;
  }
  .err {
    color: var(--ember-lift);
  }
  .pref {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 8px 10px;
    border: none;
    border-radius: var(--r-sm);
    background: none;
    color: var(--ink-2);
    font-size: 12.5px;
    cursor: pointer;
    text-align: left;
  }
  .pref:hover {
    background: rgba(255, 255, 255, 0.06);
  }
  .track {
    flex: none;
    width: 30px;
    height: 17px;
    border-radius: 999px;
    background: var(--line-2);
    border: 1px solid var(--line-2);
    position: relative;
    transition: background 0.12s ease;
  }
  .track.on {
    background: var(--ember);
    border-color: var(--ember);
  }
  .knob {
    position: absolute;
    top: 1px;
    left: 1px;
    width: 13px;
    height: 13px;
    border-radius: 50%;
    background: var(--ink);
    transition: transform 0.12s ease;
  }
  .track.on .knob {
    transform: translateX(13px);
    background: var(--ember-ink);
  }
  .preflabel {
    flex: 1;
  }
  .hint {
    padding: 0 10px 8px;
  }
</style>
