<script>
  /**
   * Sign in. Spec v2.1 §3.2: "Primary: WebAuthn passkeys … Fallbacks: password login (argon2)
   * always available". Both are on this page, passkey first, and the password form is never
   * hidden behind a toggle — a household member whose phone will not cooperate must not have
   * to discover that the way in still exists.
   *
   * §3.1: an account created with a one-time password is locked to a password change at first
   * login, so a successful sign-in can legitimately land somewhere other than Home.
   */
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
    // The sign-in response carries identity only. The shell renders its navigation from
    // `/auth/me`, so read it before leaving this page or the rail arrives empty.
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
    /* dd27-standalone-header-under-status-bar. §6's preamble makes the installed PWA the
       primary form factor, and app.html asks for it literally: `viewport-fit=cover` plus
       `apple-mobile-web-app-status-bar-style=black-translucent` start the web view at the
       physical top edge with the status bar drawn over it — 47 px on an iPhone 13, 59 px from
       the 14 Pro on. There is no header here to carry the inset, so it lands on the page box.
       `max()` and not an added `env()`: 24 px already clears everything without a notch, and
       `env()` is 0 there, so no other engine moves a pixel. `min-height: 100vh` stays — this
       page scrolls, carries no fixed bottom bar, and the dynamic viewport belongs to the shell
       rather than to a card centred in an empty grid. */
    padding: max(24px, env(safe-area-inset-top)) 24px 24px;
  }
  form {
    width: min(380px, 100%);
    /* This card IS the screen: centred in an empty grid at min(380px, 100%) with
       nothing stacked beside it. */
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
  /* 11 px on a finger, for the reason `design.css`'s coarse block states and cannot enforce from
     there: this scoped rule is (0,2,0) and the shared `.data` floor is (0,1,0), so the divider on
     the first screen §3.1 hands every new member stayed at 10.5. Last, because the two tie on
     specificity and order is what is left to decide it. [§6 preamble; decision 275] */
  @media (pointer: coarse) {
    .or {
      font-size: 11px;
    }
  }
</style>
