<script>
  // The first-login lock is enforced server-side (`deps.active_user`); this page is the way through.
  import '$lib/design.css';
  import { goto } from '$app/navigation';
  import { post } from '$lib/api.js';
  import { refreshUser, session } from '$lib/session.svelte.js';
  import { supported } from '$lib/passkeys.js';
  import FieldGroup from '$lib/components/FieldGroup.svelte';

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

<main class="calm">
  <form class="column" onsubmit={submit}>
    <header class="head">
      <h1 class="large-title">Choose a password</h1>
      <p class="why">
        {#if forced}
          This account was made with a one-time password. Choose your own to open the rest of the
          app; you can add a passkey afterwards.
        {:else}
          Your password works on any device, passkey or not. Changing it signs out every other
          device; this one stays signed in.
        {/if}
      </p>
    </header>

    <div class="fields">
      <FieldGroup>
        <label>
          <span>{forced ? 'One-time password' : 'Current password'}</span>
          <input type="password" bind:value={current} autocomplete="current-password" required />
        </label>
        <label>
          <span>New password</span>
          <input type="password" bind:value={next} autocomplete="new-password" required />
        </label>
        <label>
          <span>Confirm</span>
          <input type="password" bind:value={confirm} autocomplete="new-password" required />
        </label>
      </FieldGroup>
      {#if tooShort}
        <p class="err">Ten characters or more.</p>
      {:else if mismatch}
        <p class="err">Those don't match.</p>
      {:else}
        <p class="footnote hint">At least ten characters.</p>
      {/if}
    </div>

    <div class="actions">
      <button
        class="btn-primary wide"
        type="submit"
        disabled={busy || tooShort || mismatch || !current || !next || !confirm}
      >
        {busy ? 'Saving…' : 'Set password'}
      </button>
      {#if error}<p class="err" role="alert">{error}</p>{/if}
      <!-- The installed app has no back button, so a voluntary visit needs its own way out. -->
      {#if !forced}<a class="btn-plain cancel" href="/account">Cancel</a>{/if}
    </div>
  </form>
</main>

<style>
  /* No shell here: the page carries the status-bar and home-indicator insets itself. */
  .calm {
    min-height: 100vh;
    min-height: 100dvh;
    display: grid;
    align-content: center;
    justify-items: center;
    padding: max(32px, env(safe-area-inset-top)) max(var(--gutter), env(safe-area-inset-right))
      max(32px, env(safe-area-inset-bottom)) max(var(--gutter), env(safe-area-inset-left));
  }
  .column {
    width: min(440px, 100%);
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .head {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .head .why {
    margin: 0;
  }
  .fields,
  .actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .actions {
    gap: 12px;
  }
  .hint,
  .err {
    margin: 0;
    padding: 0 var(--gutter);
  }
  .err {
    color: var(--negative);
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .wide {
    width: 100%;
    min-height: 50px;
    border-radius: var(--r-md);
  }
  .cancel {
    align-self: center;
  }
</style>
