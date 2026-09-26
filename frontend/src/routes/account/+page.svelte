<script>
  // A credential registered under another PUBLIC_URL is listed and marked dead (§14.4), not hidden.
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { get, post, api } from '$lib/api.js';
  import { session, bootstrap } from '$lib/session.svelte.js';
  import { registerPasskey, supported } from '$lib/passkeys.js';
  import DataSources from '$lib/components/DataSources.svelte';
  import Onboarding from '$lib/components/Onboarding.svelte';

  let credentials = $state([]);
  let tiers = $state({ tier_set: [], min: 2, max: 12, warning: '' });
  let tierDraft = $state('');
  let label = $state('');
  let pin = $state('');
  let pinPassword = $state('');
  let busy = $state(false);
  let error = $state('');
  let note = $state('');

  const canPasskey = $derived(supported());
  // A PIN-switched session cannot mint credentials (the server refuses); hiding the forms only
  // explains that before the refusal does.
  const pinSession = $derived(session.user?.auth_method === 'pin');
  const PIN_SESSION =
    'You switched to this profile with a PIN. Sign in with your password or a passkey to change ' +
    'how you sign in.';

  // Only on the one-time `?welcome=1` hand-off and until a passkey exists: never a standing nag.
  const welcome = $derived(
    $page.url.searchParams.get('welcome') === '1' && credentials.length === 0 && canPasskey
  );

  // The DOM node is written back too: a one-way `value` only re-renders when `pin` changes.
  function onPinInput(event) {
    pin = event.currentTarget.value.replace(/\D/g, '');
    event.currentTarget.value = pin;
  }

  onMount(load);

  async function load() {
    credentials = (await get('/auth/passkey/credentials').catch(() => [])) ?? [];
    tiers = (await get('/rank/tiers').catch(() => tiers)) ?? tiers;
    tierDraft = (tiers.tier_set ?? []).join(' ');
  }

  async function saveTierSet() {
    error = '';
    note = '';
    busy = true;
    try {
      const body = { tier_set: tierDraft.split(/[\s,]+/).filter(Boolean) };
      const result = await api('/rank/tiers', { method: 'PUT', body });
      tiers = { ...tiers, tier_set: result.tier_set };
      tierDraft = result.tier_set.join(' ');
      // "Shortly", not a clock time: the tier-set refit runs every minute.
      note = result.k_changed
        ? `Tiers saved. Your board is being re-sorted into the new tiers and updates shortly; your ${result.tier_edits_kept} hand move${result.tier_edits_kept === 1 ? ' is' : 's are'} kept.`
        : 'Tiers renamed. Nothing else changed.';
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = false;
    }
  }

  async function addPasskey() {
    error = '';
    note = '';
    busy = true;
    try {
      await registerPasskey(label || defaultLabel());
      label = '';
      note = 'Passkey registered.';
      await load();
      await bootstrap();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = false;
    }
  }

  function defaultLabel() {
    const ua = navigator.userAgent;
    if (/iPhone|iPad/.test(ua)) return 'iPhone';
    if (/Android/.test(ua)) return 'Android';
    return 'This browser';
  }

  async function removePasskey(id) {
    error = '';
    try {
      await api(`/auth/passkey/credentials/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await load();
      await bootstrap();
    } catch (err) {
      error = err.message;
    }
  }

  async function savePin() {
    error = '';
    note = '';
    try {
      // Setting the PIN costs the password: the PIN is a convenience derived from it (decision 170).
      await post('/auth/pin', { pin, current_password: pinPassword });
      pin = '';
      pinPassword = '';
      note = 'PIN saved — this profile can now be switched to from the account chip.';
      await bootstrap();
    } catch (err) {
      error = err.message;
    }
  }
</script>

<div class="wrap">
  <header>
    <h1>Account</h1>
    <p class="why">
      Sign in with a passkey - Face ID, a fingerprint or your phone's screen lock - or with your
      password. A PIN lets you switch to your profile on a phone that is already signed in.
    </p>
  </header>

  {#if welcome}
    <div class="welcome" role="status" data-passkey-prompt>
      <div>
        <strong>Add a passkey to this device.</strong>
        <div class="why">
          Face ID or a fingerprint instead of the password you just set. The password keeps
          working — this is the faster way in, not a replacement.
        </div>
      </div>
    </div>
  {/if}

  {#if error}<div class="err" role="alert">{error}</div>{/if}
  {#if note}<div class="note" role="status">{note}</div>{/if}

  {#snippet passkeys()}
    <section class="card">
      <h2>Passkeys</h2>
      {#if !canPasskey}
        <p class="why">This browser can't use passkeys - you can still sign in with your password.</p>
      {/if}

      {#if credentials.length === 0}
        <p class="why" data-empty="passkeys">You haven't added a passkey yet.</p>
      {:else}
        <ul class="list">
          {#each credentials as c (c.id)}
            <li class:dead={!c.usable}>
              <div>
                <div class="name">{c.label ?? 'Unnamed passkey'}</div>
                <div class="data meta">
                  {c.rp_id} · used {c.sign_count} time{c.sign_count === 1 ? '' : 's'}
                  {#if !c.usable}· registered for a different address — no longer usable{/if}
                </div>
              </div>
              {#if !pinSession}
                <button class="btn-ghost" onclick={() => removePasskey(c.id)}>Remove</button>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}

      {#if pinSession}
        <p class="why" data-pin-session>{PIN_SESSION}</p>
      {:else}
        <div class="row">
          <input type="text" placeholder="Name this device (optional)" bind:value={label} />
          <button class="btn-primary" onclick={addPasskey} disabled={busy || !canPasskey}>
            {busy ? 'Waiting for the device…' : 'Add a passkey'}
          </button>
        </div>
      {/if}
    </section>
  {/snippet}

  <!-- On the welcome visit the passkey comes first: the next step sends the member to the
       home-screen app, which has its own cookie jar and asks for a second sign-in. -->
  {#if welcome}{@render passkeys()}{/if}

  <Onboarding />

  {#if !welcome}{@render passkeys()}{/if}

  <section class="card">
    <h2>Password</h2>
    <p class="why">
      Works on any device, even without a passkey. At least ten characters; changing it signs you
      out everywhere else.
    </p>
    <div class="row">
      <a class="btn-ghost" href="/account/password">Change password</a>
    </div>
  </section>

  <section class="card" data-testid="pin-card">
    <h2>PIN for switching profiles</h2>
    {#if pinSession}
      <p class="why" data-pin-session>{PIN_SESSION}</p>
    {:else}
      <p class="why">
        Four digits that let you switch to your profile on a phone or tablet someone is already
        signed in on - handy when you pass the phone around. It can't be used to sign in from
        scratch, and setting it takes your password.
        {#if session.user?.has_pin}<strong> A PIN is set.</strong>{/if}
      </p>
      <div class="row">
        <input
          type="password"
          autocomplete="current-password"
          placeholder="your password"
          bind:value={pinPassword}
        />
        <!-- `inputmode` is only a hint; `onPinInput` strips what the server's `^[0-9]+$` would refuse. -->
        <input
          type="password"
          inputmode="numeric"
          maxlength="4"
          placeholder="••••"
          value={pin}
          oninput={onPinInput}
        />
        <button class="btn-primary" onclick={savePin} disabled={pin.length !== 4 || !pinPassword}>
          Save PIN
        </button>
      </div>
    {/if}
  </section>

  <section class="card" data-testid="tier-set">
    <h2>Rank letters</h2>
    <p class="why">The letters your Rank board sorts titles into, worst first.</p>
    <p class="data letters" data-testid="tier-set-current">{(tiers.tier_set ?? []).join(' · ')}</p>
    <details class="fold" data-testid="tier-set-edit">
      <summary>Change the letters</summary>
      <p class="why">Type them worst first, with spaces between. {tiers.warning}</p>
      <div class="row">
        <input
          type="text"
          bind:value={tierDraft}
          aria-label="Rank letters"
          data-testid="tier-set-input"
        />
        <button class="btn-primary" onclick={saveTierSet} disabled={busy || !tierDraft.trim()}>
          Save letters
        </button>
      </div>
    </details>
  </section>

  <section class="card">
    <h2>Jellyfin</h2>
    {#if session.user?.jellyfin?.linked}
      <p class="why" data-jellyfin="linked">
        This account is linked to a Jellyfin user, so watched state flows both ways.
        {#if session.user.jellyfin.state === 'needs_relink'}
          <strong>
            The stored sign-in stopped working — ask an admin to link it again from the
            connectors page.
          </strong>
        {/if}
      </p>
    {:else}
      <p class="why" data-jellyfin="unlinked">
        Not linked. Everything works without it; linking adds two-way watched state and the
        “did you finish it?” prompt.
      </p>
    {/if}
  </section>

  <!-- Licence notices bind every viewer, so they sit on the one page every member reaches (decision 293). -->
  <details class="fold technical" data-testid="account-technical">
    <summary>Where the film information comes from</summary>
    <DataSources />
  </details>
</div>

<style>
  .wrap {
    max-width: 720px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  h1 {
    margin: 0 0 4px;
    font-size: 21px;
    font-weight: 600;
  }
  h2 {
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 600;
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 10px;
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
  .name {
    font-size: 13.5px;
  }
  .meta {
    margin-top: 2px;
  }
  .row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .row input {
    flex: 1;
    min-width: 160px;
  }
  .letters {
    margin: 0;
    font-size: 13px;
    color: var(--ink-2);
  }
  /* A summary is in none of design.css's coarse selectors, so it takes the 48px floor here. */
  .fold > summary {
    display: flex;
    align-items: center;
    cursor: pointer;
    font-size: 13px;
    color: var(--ink-3);
    padding: 6px 0;
  }
  .fold[open] > summary {
    margin-bottom: 10px;
  }
  /* A flex summary drops the engine's own marker, so the fold draws its state itself. */
  .fold > summary::after {
    content: '▾';
    margin-left: 8px;
    color: var(--ink-4);
  }
  .fold[open] > summary::after {
    content: '▴';
  }
  @media (pointer: coarse) {
    .fold > summary {
      min-height: var(--touch);
    }
  }
  .welcome {
    padding: 12px 15px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
    font-size: 13.5px;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }
  .note {
    color: var(--ink-2);
    font-size: 12.5px;
  }
</style>
