<script>
  // Passkey first, but the password form is never hidden behind a toggle (spec §3.2).
  import '$lib/design.css';
  import { goto } from '$app/navigation';
  import { post } from '$lib/api.js';
  import { refreshUser, setUser } from '$lib/session.svelte.js';
  import { signInWithPasskey, supported } from '$lib/passkeys.js';
  import FieldGroup from '$lib/components/FieldGroup.svelte';

  let name = $state('');
  let password = $state('');
  // Which way in failed, so the reason sits under the control that was used.
  let error = $state({ at: '', text: '' });
  let busy = $state('');

  const canPasskey = $derived(supported());

  async function land(user) {
    setUser(user);
    // The sign-in response carries identity only; the shell's nav comes from `/auth/me`.
    if (!user.must_change_password) await refreshUser();
    await goto(user.must_change_password ? '/account/password' : '/');
  }

  async function passkey() {
    error = { at: '', text: '' };
    busy = 'passkey';
    try {
      await land(await signInWithPasskey(name));
    } catch (err) {
      // A dismissed prompt is not a failure worth shouting about; a rejected assertion is.
      if (err?.name !== 'NotAllowedError') error = { at: 'passkey', text: err.message || String(err) };
    } finally {
      busy = '';
    }
  }

  async function submit(event) {
    event.preventDefault();
    error = { at: '', text: '' };
    busy = 'password';
    try {
      await land(
        await post('/auth/login', { name, password, device_label: navigator.userAgent })
      );
    } catch (err) {
      error = { at: 'password', text: err.message };
    } finally {
      busy = '';
    }
  }
</script>

{#snippet failed(at)}
  {#if error.at === at}<p class="err" role="alert">{error.text}</p>{/if}
{/snippet}

<main class="calm">
  <div class="column">
    <header class="brand">
      <h1 class="wordmark"><span class="sr-only">Sign in to </span>Spiel<em>plan</em></h1>
      <p class="tagline">What to watch tonight, for everyone at home.</p>
    </header>

    <form onsubmit={submit}>
      {#if canPasskey}
        <button class="btn-primary wide" type="button" onclick={passkey} disabled={!!busy}>
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.75"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <circle cx="8" cy="15" r="4" /><path d="m11 12 8.5-8.5M16 7l2.5 2.5M14 9l2 2" />
          </svg>
          Sign in with a passkey
        </button>
        {@render failed('passkey')}
        <p class="or">or</p>
      {/if}

      <div class="password">
        <FieldGroup>
          <label>
            <span>Name</span>
            <input
              type="text"
              bind:value={name}
              autocomplete="username"
              autocapitalize="none"
              placeholder="Your name"
              required
            />
          </label>
          <label>
            <span>Password</span>
            <input
              type="password"
              bind:value={password}
              autocomplete="current-password"
              placeholder="Password"
              required
            />
          </label>
        </FieldGroup>
        <button
          class="wide {canPasskey ? 'btn-secondary' : 'btn-primary'}"
          type="submit"
          disabled={!!busy || !name || !password}
        >
          {busy === 'password' ? 'Checking…' : 'Sign in'}
        </button>
        {@render failed('password')}
      </div>

      <p class="footnote first">
        First time? Use the name and <span class="nowrap">one-time</span> password you were given.
      </p>
    </form>
  </div>
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
    width: min(400px, 100%);
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .brand {
    display: flex;
    flex-direction: column;
    gap: 8px;
    text-align: center;
  }
  .wordmark {
    margin: 0;
    font-family: var(--serif);
    font-weight: 400;
    font-size: var(--fs-display);
    line-height: 48px;
  }
  .wordmark em {
    font-style: italic;
  }
  .tagline {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
  }
  form,
  .password {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .password {
    gap: 12px;
  }
  .wide {
    width: 100%;
    border-radius: var(--r-md);
  }
  .btn-primary.wide {
    min-height: 50px;
  }
  .or {
    margin: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  .or::before,
  .or::after {
    content: '';
    flex: 1;
    height: 0.5px;
    background: var(--separator);
  }
  .err {
    margin: 0;
    padding: 0 var(--gutter);
    color: var(--negative);
    font-size: var(--fs-subhead);
    line-height: 20px;
    text-align: center;
  }
  .nowrap {
    white-space: nowrap;
  }
  .first {
    margin: 0;
    padding: 0 var(--gutter);
    text-align: center;
    text-wrap: balance;
  }
</style>
