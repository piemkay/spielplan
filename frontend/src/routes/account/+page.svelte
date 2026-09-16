<script>
  /**
   * Account. Spec v2.1 §3.2, §3.3, §14.4.
   *
   * Four things live here because all four are per-person and per-device:
   *   - passkeys, which are primary auth and are registered *from the profile page* (§3.2);
   *   - first-run onboarding — home-screen install and push permission (§6 preamble) — which
   *     is §3.1's fifth setup step and the only surface that can complete it. It sits first
   *     while it is still owed — except on the `?welcome=1` hand-off itself, where the passkey
   *     card is rendered above it and the markup below argues why;
   *   - the switch PIN, which is a shared-device convenience, not a login;
   *   - the Jellyfin link, which is optional and drives seen-sync only (§3.3).
   *
   * §14.4 is surfaced rather than documented: a credential registered against a different
   * PUBLIC_URL is listed and marked dead, because "my passkey stopped working" deserves an
   * answer on the screen instead of in the logs.
   */
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { get, post, api } from '$lib/api.js';
  import { session, bootstrap } from '$lib/session.svelte.js';
  import { registerPasskey, supported } from '$lib/passkeys.js';
  import Onboarding from '$lib/components/Onboarding.svelte';

  let credentials = $state([]);
  /** Decision 11: the tier set is a per-user preference, so it lives here and not in Admin. */
  let tiers = $state({ tier_set: [], min: 2, max: 12, warning: '' });
  let tierDraft = $state('');
  let label = $state('');
  let pin = $state('');
  let pinPassword = $state('');
  let busy = $state(false);
  let error = $state('');
  let note = $state('');

  const canPasskey = $derived(supported());
  // sec-01: a session minted by `POST /api/auth/switch` carries `auth_method === 'pin'` and
  // the server refuses every credential-minting route from it (`credentialed_user`). Hiding
  // the forms is so the refusal is not the first thing the person holding a handed-over phone
  // hears — the gate is the server's, and this is only its explanation.
  const pinSession = $derived(session.user?.auth_method === 'pin');
  const PIN_SESSION =
    'This profile was switched into with a PIN. Switch back with your password or a passkey ' +
    'to manage credentials.';

  // §3.1's "prompted afterwards": set once by the forced first-login password change, and
  // gone as soon as a passkey exists. A permanent version of this would be a nag on an
  // account that may never want one — §3.2 keeps the password fallback always available.
  const welcome = $derived(
    $page.url.searchParams.get('welcome') === '1' && credentials.length === 0 && canPasskey
  );

  /**
   * §3.2's PIN is four digits and the server's pattern is `^[0-9]+$`. The DOM node is written
   * back as well as the state: a one-way `value={pin}` only re-renders when `pin` changes, so
   * a rejected character stays visible in a box whose state no longer contains it.
   */
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

  /**
   * Decision 11: "on save, their cutpoints are re-initialised to the equal-mass quantiles of that
   * user's fitted `s` distribution for the new K … and a Ledger refit is queued for that user
   * alone". The warning is not decoration — it names what the save discards, which is the
   * one thing this control does that cannot be undone by saving the old set back.
   */
  async function saveTierSet() {
    error = '';
    note = '';
    busy = true;
    try {
      const body = { tier_set: tierDraft.split(/[\s,]+/).filter(Boolean) };
      const result = await api('/rank/tiers', { method: 'PUT', body });
      tiers = { ...tiers, tier_set: result.tier_set };
      tierDraft = result.tier_set.join(' ');
      note = result.k_changed
        ? `Tier set saved. Cutpoints re-initialised and a refit is queued; ${result.tier_edits_kept} past move${result.tier_edits_kept === 1 ? '' : 's'} kept.`
        : 'Tier set renamed. Your learned cutpoints are unchanged.';
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
      // Decision 170: §3.2 makes the password the account credential and the PIN a
      // convenience derived from it, so setting the PIN costs the password.
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
      Passkeys are primary. A password stays available as a fallback, and a PIN switches
      profiles on a device someone is already signed in on.
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
        <p class="why">This browser has no WebAuthn support — password sign-in still works.</p>
      {/if}

      {#if credentials.length === 0}
        <p class="why" data-empty="passkeys">No passkey registered on this account yet.</p>
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

  <!-- Which of these two cards comes first, and why it is not always the same one.
       §3.1's forced password change lands a new member here as `?welcome=1`, and the first
       thing the install step then tells them is to leave for the home-screen icon — where
       §3.2's HttpOnly cookie, held in that app's own jar, makes them sign in a second time.
       Offering the passkey *after* the instruction to leave is offering it too late, so on
       that one visit the order inverts. `welcome` is already precisely that visit — the
       `?welcome=1` hand-off, no credential registered yet, WebAuthn present — and every other
       visit keeps §3.1's own order, with the fifth setup step first while it is still owed.
       [fe-14-ios-install-journey-second-login-and-copy] -->
  {#if welcome}{@render passkeys()}{/if}

  <!-- §6 preamble / §3.1's fifth step. Its own component because it owns four asynchronous
       browser facts (permission, subscription, install prompt, standalone) that have nothing
       to do with the rest of this page. -->
  <Onboarding />

  {#if !welcome}{@render passkeys()}{/if}

  <!-- as-14: the forced first-login change was the only way anybody ever reached
       /account/password, so an unlocked member had no way to change their password at all
       while §3.2 keeps it the always-available fallback. -->
  <section class="card">
    <h2>Password</h2>
    <p class="why">
      The fallback that always works, on any device, with no authenticator to hand. Ten
      characters or more; changing it signs every other device out.
    </p>
    <div class="row">
      <a class="btn-ghost" href="/account/password">Change password</a>
    </div>
  </section>

  <section class="card">
    <h2>Switch PIN</h2>
    {#if pinSession}
      <p class="why" data-pin-session>{PIN_SESSION}</p>
    {:else}
      <p class="why">
        Four digits, for handing the TV remote over. It is not a way in — the device has to be
        signed in already, and your password sets it (decision 170).
        {#if session.user?.has_pin}<strong> A PIN is set.</strong>{/if}
      </p>
      <div class="row">
        <input
          type="password"
          autocomplete="current-password"
          placeholder="your password"
          bind:value={pinPassword}
        />
        <!-- §3.2 says four digits and the server's pattern is `^[0-9]+$`; `inputmode` is a
             keyboard hint, not a constraint, so the box used to send letters and read back a
             bare 422 (feroutes-pin-box). Stripping in the binding is what makes the field
             unable to hold what the server will refuse. -->
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
    <h2>Tier set</h2>
    <p class="why">
      The letters your Rank board uses, worst first. {tiers.warning}
    </p>
    <div class="row">
      <input
        type="text"
        bind:value={tierDraft}
        aria-label="Tier set"
        data-testid="tier-set-input"
      />
      <button class="btn-primary" onclick={saveTierSet} disabled={busy || !tierDraft.trim()}>
        Save tier set
      </button>
    </div>
    <p class="data" data-testid="tier-set-current">{(tiers.tier_set ?? []).join(' · ')}</p>
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
