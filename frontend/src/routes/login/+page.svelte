<script>
  // Passkey first, but the password form is never hidden behind a toggle (spec §3.2).
  import '$lib/design.css';
  import { goto } from '$app/navigation';
  import { post } from '$lib/api.js';
  import { refreshUser, setUser } from '$lib/session.svelte.js';
  import { signInWithPasskey, supported } from '$lib/passkeys.js';

  let name = $state('');
  let password = $state('');
  let error = $state('');
  let busy = $state(false);

  const canPasskey = $derived(supported());

  async function land(user) {
    setUser(user);
    // The sign-in response carries identity only; the shell's nav comes from `/auth/me`.
    if (!user.must_change_password) await refreshUser();
    await goto(user.must_change_password ? '/account/password' : '/');
  }

  async function passkey() {
    error = '';
    busy = true;
    try {
      await land(await signInWithPasskey(name));
    } catch (err) {
      // A dismissed prompt is not a failure worth shouting about; a rejected assertion is.
      error = err?.name === 'NotAllowedError' ? '' : err.message || String(err);
    } finally {
      busy = false;
    }
  }

  async function submit(event) {
    event.preventDefault();
    error = '';
    busy = true;
    try {
      await land(
        await post('/auth/login', { name, password, device_label: navigator.userAgent })
      );
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="page">
  <form class="card" onsubmit={submit}>
    <div class="brand">SPIELPLAN</div>
    <h1>Sign in</h1>
    <p class="why">
      Use the passkey on this device, or the password you were given — or set.
    </p>

    {#if canPasskey}
      <button class="btn-primary passkey" type="button" onclick={passkey} disabled={busy}>
        Sign in with a passkey
      </button>
      <div class="or data">OR</div>
    {/if}

    <label>
      <span class="data">NAME</span>
      <input type="text" bind:value={name} autocomplete="username" required />
    </label>
    <label>
      <span class="data">PASSWORD</span>
      <input type="password" bind:value={password} autocomplete="current-password" required />
    </label>

    {#if error}<div class="err">{error}</div>{/if}

    <button class="btn-primary" type="submit" disabled={busy || !name || !password}>
      {busy ? 'Checking…' : 'Sign in'}
    </button>
  </form>
</div>

<style>
  .page {
    min-height: 100vh;
    display: grid;
    place-items: center;
    /* The installed PWA draws the status bar over the page top (viewport-fit=cover). */
    padding: max(24px, env(safe-area-inset-top)) 24px 24px;
  }
  form {
    width: min(380px, 100%);
    padding: var(--card-pad-roomy);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .brand {
    font-weight: 700;
    font-size: 12px;
    letter-spacing: 0.13em;
    color: var(--ink-4);
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
  .or {
    text-align: center;
    color: var(--ink-4);
    font-size: 10.5px;
    letter-spacing: 0.12em;
  }
  /* This scoped rule outranks design.css's coarse `.data` floor, so restate it here, last. */
  @media (pointer: coarse) {
    .or {
      font-size: 11px;
    }
  }
</style>
