<script>
  // The first-login lock is enforced server-side (`deps.active_user`); this page is the way through.
  import '$lib/design.css';
  import { goto } from '$app/navigation';
  import { post } from '$lib/api.js';
  import { refreshUser, session } from '$lib/session.svelte.js';
  import { supported } from '$lib/passkeys.js';

  let current = $state('');
  let next = $state('');
  let confirm = $state('');
  let error = $state('');
  let busy = $state(false);

  // Also reached voluntarily; only under the first-login lock is the current password a one-time one.
  const forced = $derived(session.user?.must_change_password !== false);
  const tooShort = $derived(next.length > 0 && next.length < 10);
  const mismatch = $derived(confirm.length > 0 && next !== confirm);

  async function submit(event) {
    event.preventDefault();
    error = '';
    busy = true;
    try {
      await post('/auth/password', { current_password: current, new_password: next });
      // The lock is clear only now, so re-read `/auth/me` for the nav rather than patching the flag.
      const user = await refreshUser();
      // §3.1 prompts for a passkey after the change: once, here, never as a standing nag.
      const owed = supported() && !user?.passkeys;
      await goto(owed ? '/account?welcome=1' : '/');
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="page">
  <form class="card" onsubmit={submit}>
    <h1>Choose a password</h1>
    <p class="why">
      {#if forced}
        This account was created with a one-time password. Setting your own unlocks the rest of
        the app; a passkey can be added afterwards from the account page.
      {:else}
        Your password stays available as a fallback on any device. Changing it signs every
        other session out; this one stays.
      {/if}
    </p>

    <label>
      <span class="data">{forced ? 'ONE-TIME PASSWORD' : 'CURRENT PASSWORD'}</span>
      <input type="password" bind:value={current} autocomplete="current-password" required />
    </label>
    <label>
      <span class="data">NEW PASSWORD · AT LEAST 10 CHARACTERS</span>
      <input type="password" bind:value={next} autocomplete="new-password" required />
    </label>
    <label>
      <span class="data">CONFIRM</span>
      <input type="password" bind:value={confirm} autocomplete="new-password" required />
    </label>

    {#if tooShort}<div class="err">Ten characters or more.</div>{/if}
    {#if mismatch}<div class="err">Those do not match.</div>{/if}
    {#if error}<div class="err">{error}</div>{/if}

    <button
      class="btn-primary"
      type="submit"
      disabled={busy || tooShort || mismatch || !current || !next || !confirm}
    >
      {busy ? 'Saving…' : 'Set password'}
    </button>
  </form>
</div>

<style>
  .page {
    min-height: 100vh;
    display: grid;
    place-items: center;
    /* The same status-bar inset as /login: no shell header carries it here. */
    padding: max(24px, env(safe-area-inset-top)) 24px 24px;
  }
  form {
    width: min(400px, 100%);
    padding: var(--card-pad-roomy);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }
</style>
