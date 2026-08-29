<script>
  /**
   * The forced first-login password change. Spec v2.1 §3.1:
   *   "a one-time password is issued, the account is locked to a password change at first
   *    login, and passkey registration is prompted afterwards."
   * The lock is enforced server-side (`deps.active_user`); this page is the only way through it.
   */
  import '$lib/design.css';
  import { goto } from '$app/navigation';
  import { post } from '$lib/api.js';
  import { refreshUser } from '$lib/session.svelte.js';
  import { supported } from '$lib/passkeys.js';

  let current = $state('');
  let next = $state('');
  let confirm = $state('');
  let error = $state('');
  let busy = $state(false);

  const tooShort = $derived(next.length > 0 && next.length < 10);
  const mismatch = $derived(confirm.length > 0 && next !== confirm);

  async function submit(event) {
    event.preventDefault();
    error = '';
    busy = true;
    try {
      await post('/auth/password', { current_password: current, new_password: next });
      // The lock is now clear, so this is the first moment `/auth/me` will answer with the
      // navigation payload the shell renders from. Re-read rather than patching the flag.
      const user = await refreshUser();
      // §3.1: "a one-time password is issued, the account is locked to a password change at
      // first login, and **passkey registration is prompted afterwards**." This is afterwards.
      // Once, on the way through — not a standing nag on an account page someone may never
      // want a passkey on.
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
      This account was created with a one-time password. Setting your own unlocks the rest of
      the app; a passkey can be added afterwards from the account page.
    </p>

    <label>
      <span class="data">ONE-TIME PASSWORD</span>
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
    padding: 24px;
  }
  form {
    width: min(400px, 100%);
    padding: 26px;
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
