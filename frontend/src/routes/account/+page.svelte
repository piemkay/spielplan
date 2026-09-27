<script>
  // A credential registered under another PUBLIC_URL is listed and marked dead (§14.4), not hidden.
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { get, post, api } from '$lib/api.js';
  import { session, bootstrap } from '$lib/session.svelte.js';
  import { registerPasskey, supported } from '$lib/passkeys.js';
  import DataSources from '$lib/components/DataSources.svelte';
  import Onboarding from '$lib/components/Onboarding.svelte';
  import RowIcon from '$lib/components/RowIcon.svelte';

  let credentials = $state([]);
  let tiers = $state({ tier_set: [], min: 2, max: 12, warning: '' });
  let tierDraft = $state('');
  let label = $state('');
  let pin = $state('');
  let pinPassword = $state('');
  let busy = $state('');
  // What the last action said, shown under the control that caused it.
  let feedback = $state(null);

  const canPasskey = $derived(supported());
  // A PIN-switched session cannot mint credentials (the server refuses); hiding the forms only
  // explains that before the refusal does.
  const pinSession = $derived(session.user?.auth_method === 'pin');
  const PIN_SESSION =
    'You switched to this profile with a PIN. Sign in with your password or a passkey to change ' +
    'how you sign in.';
  const jellyfin = $derived(session.user?.jellyfin);

  // Only on the one-time `?welcome=1` hand-off and until a passkey exists: never a standing nag.
  const welcome = $derived(
    $page.url.searchParams.get('welcome') === '1' && credentials.length === 0 && canPasskey
  );

  // Stored worst first; read and typed best first, as Rank lists them.
  const bestFirst = (set) => [...(set ?? [])].reverse().join(' ');

  function say(at, ok, text) {
    feedback = { at, ok, text };
  }

  // The DOM node is written back too: a one-way `value` only re-renders when `pin` changes.
  function onPinInput(event) {
    pin = event.currentTarget.value.replace(/\D/g, '');
    event.currentTarget.value = pin;
  }

  onMount(load);

  async function load() {
    credentials = (await get('/auth/passkey/credentials').catch(() => [])) ?? [];
    tiers = (await get('/rank/tiers').catch(() => tiers)) ?? tiers;
    tierDraft = bestFirst(tiers.tier_set);
  }

  async function saveTierSet() {
    feedback = null;
    busy = 'letters';
    try {
      const body = { tier_set: tierDraft.split(/[\s,]+/).filter(Boolean).reverse() };
      const result = await api('/rank/tiers', { method: 'PUT', body });
      tiers = { ...tiers, tier_set: result.tier_set };
      tierDraft = bestFirst(result.tier_set);
      const kept = result.tier_edits_kept;
      // "Shortly", not a clock time: the tier-set refit runs every minute.
      say(
        'letters',
        true,
        result.k_changed
          ? `Saved. Rank re-sorts into the new letters shortly; your ${kept} move${kept === 1 ? '' : 's'} by hand ${kept === 1 ? 'is' : 'are'} kept.`
          : 'Letters renamed. Nothing else changed.'
      );
    } catch (err) {
      say('letters', false, err.message || String(err));
    } finally {
      busy = '';
    }
  }

  async function addPasskey() {
    feedback = null;
    busy = 'passkey';
    try {
      await registerPasskey(label || defaultLabel());
      label = '';
      say('passkeys', true, 'Passkey added.');
      await load();
      await bootstrap();
    } catch (err) {
      say('passkeys', false, err.message || String(err));
    } finally {
      busy = '';
    }
  }

  function defaultLabel() {
    const ua = navigator.userAgent;
    if (/iPhone|iPad/.test(ua)) return 'iPhone';
    if (/Android/.test(ua)) return 'Android';
    return 'This browser';
  }

  async function removePasskey(id) {
    feedback = null;
    try {
      await api(`/auth/passkey/credentials/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await load();
      await bootstrap();
    } catch (err) {
      say('passkeys', false, err.message);
    }
  }

  async function savePin() {
    feedback = null;
    try {
      // Setting the PIN costs the password: the PIN is a convenience derived from it (decision 170).
      await post('/auth/pin', { pin, current_password: pinPassword });
      pin = '';
      pinPassword = '';
      say('pin', true, 'PIN saved — you can now switch to this profile from You on a shared phone.');
      await bootstrap();
    } catch (err) {
      say('pin', false, err.message);
    }
  }
</script>

{#snippet said(at)}
  {#if feedback?.at === at}
    <p class="said" class:err={!feedback.ok} role={feedback.ok ? 'status' : 'alert'}>
      {feedback.text}
    </p>
  {/if}
{/snippet}

{#snippet chevron()}
  <svg
    class="chev"
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
  </svg>
{/snippet}

{#snippet signIn()}
  <section class="group" data-testid="sign-in">
    <h2 class="list-header">Sign-in</h2>
    <div class="list-group">
      {#each credentials as c (c.id)}
        <div class="list-row" class:dead={!c.usable} data-testid="passkey">
          <RowIcon name="key" tone="blue" />
          <span class="text">
            <span>{c.label ?? 'Unnamed passkey'}</span>
            <span class="sub">
              {#if c.usable}
                {c.rp_id} · used {c.sign_count} time{c.sign_count === 1 ? '' : 's'}
              {:else}
                Made for {c.rp_id} — it no longer works at this address
              {/if}
            </span>
          </span>
          {#if !pinSession}
            <button
              class="btn-plain remove"
              aria-label="Remove {c.label ?? 'this passkey'}"
              onclick={() => removePasskey(c.id)}
            >
              Remove
            </button>
          {/if}
        </div>
      {:else}
        <div class="list-row" data-empty="passkeys">
          <RowIcon name="key" tone="blue" />
          <span class="text">Passkeys</span>
          <span class="value">None yet</span>
        </div>
      {/each}

      {#if canPasskey && !pinSession}
        <div class="form">
          <input
            type="text"
            placeholder="Name this device (optional)"
            aria-label="Name for the new passkey"
            bind:value={label}
          />
          <button class={welcome ? 'btn-primary' : 'btn-tinted'} onclick={addPasskey} disabled={!!busy}>
            {busy === 'passkey' ? 'Waiting for your device…' : 'Add a passkey'}
          </button>
          {@render said('passkeys')}
        </div>
      {:else if feedback?.at === 'passkeys'}
        <div class="form">{@render said('passkeys')}</div>
      {/if}

      <a class="list-row" href="/account/password">
        <RowIcon name="lock" />
        <span class="text">Password</span>
        <span class="value">Change</span>
        {@render chevron()}
      </a>

      {#if pinSession}
        <div class="list-row" data-testid="pin-card">
          <RowIcon name="key" tone="amber" />
          <span class="text">PIN for switching profiles</span>
          <span class="value">{session.user?.has_pin ? 'Set' : 'Not set'}</span>
        </div>
      {:else}
        <details data-testid="pin-card">
          <summary class="list-row">
            <RowIcon name="key" tone="amber" />
            <span class="text">PIN for switching profiles</span>
            <span class="value">{session.user?.has_pin ? 'Set' : 'Not set'}</span>
            {@render chevron()}
          </summary>
          <div class="form">
            <p class="note">
              Four digits that switch a phone someone else is signed in on over to you — handy
              when you pass it around. It can't sign you in from scratch, and setting it takes your
              password.
            </p>
            <label>
              <span class="footnote">Your password</span>
              <input type="password" autocomplete="current-password" bind:value={pinPassword} />
            </label>
            <!-- `inputmode` is only a hint; `onPinInput` strips what the server's `^[0-9]+$` would refuse. -->
            <label>
              <span class="footnote">New PIN</span>
              <input
                type="password"
                inputmode="numeric"
                maxlength="4"
                placeholder="Four digits"
                value={pin}
                oninput={onPinInput}
              />
            </label>
            <button class="btn-tinted" onclick={savePin} disabled={pin.length !== 4 || !pinPassword}>
              Save PIN
            </button>
            {@render said('pin')}
          </div>
        </details>
      {/if}
    </div>
    <p class="list-footer">
      {#if pinSession}
        <span data-pin-session>{PIN_SESSION}</span>
      {:else if !canPasskey}
        This browser can't use passkeys — you can still sign in with your password.
      {:else}
        A passkey is Face ID, a fingerprint or your phone's screen lock. Your password still works
        on any device.
      {/if}
    </p>
  </section>
{/snippet}

<div class="account">
  <h1 class="large-title">You</h1>

  {#if welcome}
    <div class="card welcome" role="status" data-passkey-prompt>
      <p class="headline">Add a passkey to this device</p>
      <p class="why">
        Face ID or a fingerprint instead of the password you just set. The password keeps working —
        this is the faster way in, not a replacement.
      </p>
    </div>
  {/if}

  <!-- On the welcome visit Sign-in comes first: the next step sends the member to the
       home-screen app, which has its own cookie jar and asks for a second sign-in. -->
  {#if welcome}{@render signIn()}{/if}

  <Onboarding />

  {#if !welcome}{@render signIn()}{/if}

  <section class="group" data-testid="tier-set">
    <h2 class="list-header">Preferences</h2>
    <details class="list-group" data-testid="tier-set-edit">
      <summary class="list-row">
        <RowIcon name="rank" tone="teal" />
        <span class="text">Rank letters</span>
        <span class="value" data-testid="tier-set-current">{bestFirst(tiers.tier_set)}</span>
        {@render chevron()}
      </summary>
      <div class="form">
        <label>
          <span class="footnote">
            Best first, with spaces between — {tiers.min} to {tiers.max} letters.
          </span>
          <input
            type="text"
            bind:value={tierDraft}
            aria-label="Rank letters"
            data-testid="tier-set-input"
          />
        </label>
        <p class="note">{tiers.warning}</p>
        <button class="btn-tinted" onclick={saveTierSet} disabled={!!busy || !tierDraft.trim()}>
          Save letters
        </button>
        {@render said('letters')}
      </div>
    </details>
    <p class="list-footer">The letters Rank sorts your titles into.</p>
  </section>

  <section class="group">
    <h2 class="list-header">Jellyfin</h2>
    <div class="list-group">
      {#if jellyfin?.linked}
        <div class="list-row" data-jellyfin="linked">
          <RowIcon name="film" tone="ember" />
          <span class="text">Linked</span>
          {#if jellyfin.state === 'needs_relink'}
            <span class="badge warn">Needs linking again</span>
          {:else}
            <svg
              class="linked"
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2.25"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path d="m5 12.5 4.5 4.5L19 7.5" />
            </svg>
          {/if}
        </div>
      {:else}
        <div class="list-row" data-jellyfin="unlinked">
          <RowIcon name="film" />
          <span class="text">Not linked</span>
        </div>
      {/if}
    </div>
    <p class="list-footer">
      {#if jellyfin?.state === 'needs_relink'}
        Jellyfin stopped accepting this link. Ask an admin to link it again.
      {:else if jellyfin?.linked}
        What you watch in Jellyfin counts as seen here, and the other way round.
      {:else}
        Everything works without it. Once an admin links it, what you watch in Jellyfin counts as
        seen here, and Spielplan asks “Did you finish it?” when a film ends.
      {/if}
    </p>
  </section>

  <!-- Licence notices bind every viewer, so they sit on the one page every member reaches (decision 293). -->
  <details class="sources" data-testid="account-technical">
    <summary class="list-row">
      <RowIcon name="info" />
      <span class="text">Where the film information comes from</span>
      {@render chevron()}
    </summary>
    <div class="credits"><DataSources /></div>
  </details>
</div>

<style>
  .account {
    max-width: 640px;
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  .list-group > * + * {
    box-shadow: inset 0 0.5px 0 var(--separator);
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
    overflow-wrap: anywhere;
  }
  .dead .text {
    color: var(--text-3);
  }
  .remove {
    flex: none;
    margin-right: -8px;
    color: var(--negative);
  }
  .chev {
    flex: none;
    color: rgba(245, 240, 232, 0.35);
    transition: transform 0.2s var(--ease);
  }
  .list-row[href] {
    color: var(--text);
  }
  summary {
    list-style: none;
    cursor: pointer;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  details[open] > summary .chev {
    transform: rotate(90deg);
  }
  /* A form that opens inside its group, under the row that names it. */
  .form {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 12px var(--gutter) 16px;
  }
  .form label {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .form .btn-tinted,
  .form .btn-primary {
    align-self: stretch;
    border-radius: var(--r-md);
  }
  .note,
  .said {
    margin: 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .said {
    color: var(--positive);
  }
  .said.err {
    color: var(--negative);
  }
  .linked {
    flex: none;
    margin-left: auto;
    color: var(--positive);
  }
  .list-row .badge {
    margin-left: auto;
  }
  .welcome {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .welcome p {
    margin: 0;
  }
  .headline {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .sources > summary {
    border-radius: var(--r-md);
    background: var(--surface-1);
  }
  .credits {
    padding-top: 32px;
  }
</style>
