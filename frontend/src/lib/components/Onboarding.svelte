<script>
  // iOS has no install prompt and pushes only to the home-screen app, so this guides rather than
  // asks (§6 preamble). Yes and no both complete §3.1's fifth step.
  import { onMount } from 'svelte';
  import {
    completeOnboarding,
    disablePush,
    enablePush,
    installPrompt,
    isStandalone,
    localEndpoint,
    permissionState,
    platform,
    pushSupported,
    readState,
    showInstallPrompt,
    syncSubscription,
    watchInstallPrompt
  } from '$lib/push.js';

  // Settled until the server says otherwise, so the nag never flashes on every visit.
  let complete = $state(true);
  let vapidKey = $state(null);
  let devices = $state([]);
  let prompt = $state(null);
  let standalone = $state(false);
  // Optimistic until the Permissions API answers.
  let perm = $state('default');
  let busy = $state('');
  let error = $state('');
  let installOutcome = $state('');
  // This browser's own subscription: `devices` is the member's whole list, not this device.
  let localSub = $state(null);
  let localDevice = $state(null);
  // A line about this device's push state that is neither an error nor the state itself.
  let pushNote = $state('');
  let noteKind = $state('');

  const where = $derived(platform({ installPrompt: !!prompt }));
  const pushState = $derived(
    !pushSupported()
      ? 'unsupported'
      : localSub
        ? 'on'
        : perm === 'denied'
          ? 'denied'
          : 'off'
  );
  // Not `perm === 'granted'`: a granted device whose row was pruned is not registered.

  // The server's device handle is the only way to mark this row: no `crypto.subtle` over plain HTTP.
  const scopeOf = (device) =>
    localDevice ? (device.device === localDevice ? 'this' : 'other') : 'unknown';
  const devicesWhy = $derived(
    !localSub
      ? 'None of these is this browser — notifications are per-device, and this one is ' +
        'not registered yet.'
      : localDevice
        ? 'Notifications are per-device. The one you are on is marked; turning them off here ' +
          'leaves the others on.'
        : 'Notifications are per-device, and this one is registered too; turning them off ' +
          'here leaves the others on.'
  );

  onMount(() => {
    standalone = isStandalone();
    prompt = installPrompt();
    permissionState().then((answer) => (perm = answer));
    localEndpoint().then((endpoint) => (localSub = endpoint));
    load();
    return watchInstallPrompt((event) => (prompt = event));
  });

  async function load() {
    try {
      const state = await readState();
      complete = state.onboarding_complete;
      vapidKey = state.vapid_public_key;
      devices = state.subscriptions;
    } catch (err) {
      error = err.message || String(err);
      return;
    }
    // Re-post any subscription the server was never told about; it upserts on the endpoint.
    try {
      const synced = await syncSubscription({ vapidKey });
      if (synced?.stale) {
        // Minted under a key the server no longer signs with, so the push service will refuse it.
        localSub = null;
        localDevice = null;
        noteKind = 'stale';
        pushNote =
          'This device was registered before the server’s notification key changed, so nothing ' +
          'can reach it. Turning notifications on again re-registers it.';
      } else if (synced) {
        devices = synced.subscriptions;
        localDevice = synced.device ?? null;
      }
    } catch {
      // Not worth a red box: the button below still says what state it is in.
    }
  }

  /** Called on yes and on no alike. */
  async function finish() {
    if (complete) return;
    try {
      await completeOnboarding();
      complete = true;
    } catch (err) {
      error = err.message || String(err);
    }
  }

  async function install() {
    busy = 'install';
    error = '';
    try {
      const choice = await showInstallPrompt();
      installOutcome = choice.outcome;
      prompt = installPrompt(); // spent — the button goes away with the event
      standalone = isStandalone();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = '';
    }
  }

  // No await before `requestPermission()`: it must run inside the user gesture (§6 preamble).
  async function enable() {
    busy = 'push';
    error = '';
    pushNote = '';
    noteKind = '';
    try {
      const result = await enablePush({ vapidKey });
      perm = result.permission === 'unsupported' ? perm : result.permission;
      if (result.subscriptions) devices = result.subscriptions;
      // From the act itself, not a re-read: the list cannot say which row is this device.
      if (result.endpoint) {
        localSub = result.endpoint;
        localDevice = result.device ?? null;
      }
      // Granted or denied, the member has answered the question.
      await finish();
    } catch (err) {
      // A refusal lands above; this is a real failure, usually a missing application server key.
      error = err.message || String(err);
      perm = await permissionState();
    } finally {
      busy = '';
    }
  }

  async function disable() {
    busy = 'push';
    error = '';
    pushNote = '';
    noteKind = '';
    try {
      const result = await disablePush();
      if (result.removed) {
        localSub = null;
        localDevice = null;
        // The DELETE answers with the member's remaining devices.
        if (result.subscriptions) devices = result.subscriptions;
      } else {
        // `removed: false` means this browser holds no subscription, so clear it too.
        localSub = null;
        localDevice = null;
        noteKind = 'nothing-local';
        pushNote =
          'This browser holds no notification subscription, so there was nothing to turn off ' +
          'here. The devices listed below are your other ones.';
      }
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = '';
    }
  }
</script>

<section
  class="card"
  data-testid="onboarding"
  data-onboarding-state={complete ? 'settled' : 'prompt'}
  data-platform={where}
  data-push-state={pushState}
  data-standalone={standalone}
>
  <h2>This device</h2>

  {#if complete}
    <p class="why">
      Install and notifications are per-device, so this section is here whenever you switch
      phones. Nothing below is required.
    </p>
  {:else}
    <p class="why" data-testid="onboarding-prompt">
      Two things make Spielplan feel like an app on this phone: putting it on the home screen,
      and letting it tell you when something finished playing. Both are optional, both are just
      for this device, and you will not be asked again.
    </p>
  {/if}

  <div class="step">
    <div class="label data">1 · on the home screen</div>
    {#if standalone}
      <p class="why" data-testid="onboarding-installed">
        Installed — you are running the home-screen app. This is the only place notifications
        work on an iPhone.
      </p>
    {:else if where === 'ios-safari'}
      <!-- No direction word: the host moves the Passkeys card above or below this one. -->
      <ol class="steps" data-testid="onboarding-ios-steps">
        <li>Tap the Share button in Safari's toolbar (the square with the arrow).</li>
        <li>Scroll down and choose <strong>Add to Home Screen</strong>.</li>
        <li>Open Spielplan from the new icon and sign in there once.</li>
      </ol>
      <p class="why">
        Safari gives a page no way to ask to be installed, so these three taps are the whole
        mechanism on iOS — and on an iPhone notifications only work from that icon. The icon
        opens with its own cookies, so the home-screen app keeps its own sign-in and will ask
        who you are one more time. Adding a passkey on this page is worth doing before you go.
      </p>
    {:else if where === 'ios-other'}
      <p class="why" data-testid="onboarding-ios-browser">
        On iOS only Safari can add a page to the home screen. Open Spielplan in Safari and this
        step will explain itself there.
      </p>
    {:else if prompt}
      <button
        class="btn-primary"
        data-testid="onboarding-install"
        onclick={install}
        disabled={busy === 'install'}
      >
        {busy === 'install' ? 'Waiting for the browser…' : 'Install Spielplan'}
      </button>
    {:else}
      <p class="why" data-testid="onboarding-install-unavailable">
        This browser has not offered an install prompt. Either the app is installed already, or
        the browser does not do installs — the app works the same either way, in a tab.
      </p>
    {/if}
    {#if installOutcome}
      <p class="why" data-testid="onboarding-install-outcome">
        {installOutcome === 'accepted'
          ? 'Installed. Open Spielplan from the new icon from now on.'
          : 'Not installed — the browser prompt was dismissed.'}
      </p>
    {/if}
  </div>

  <div class="step">
    <div class="label data">2 · notifications</div>
    {#if pushState === 'unsupported'}
      <!-- In a Safari tab the cause is the tab, not the browser: iOS pushes only to the home-screen app. -->
      {#if where === 'ios-safari'}
        <p class="why" data-testid="onboarding-push-state">
          On an iPhone notifications come from the home-screen app rather than from a Safari
          tab — add the icon in step 1 and turn them on there. Nothing is lost meanwhile: every
          prompt notifications would carry also waits for you inside the app.
        </p>
      {:else}
        <p class="why" data-testid="onboarding-push-state">
          This browser has no Web Push support. Nothing is lost: every prompt notifications would
          carry also waits for you inside the app.
        </p>
      {/if}
    {:else if pushState === 'on'}
      <p class="why" data-testid="onboarding-push-state">
        Notifications are on for this device. They are best-effort — anything they would have
        told you is also waiting in the app.
      </p>
      <button
        class="btn-ghost"
        data-testid="onboarding-push-disable"
        onclick={disable}
        disabled={busy === 'push'}
      >
        Turn notifications off
      </button>
    {:else if pushState === 'denied'}
      <p class="why" data-testid="onboarding-push-state">
        This browser is blocking notifications for Spielplan. We cannot ask again from here —
        it has to be changed in the browser's own site settings.
      </p>
    {:else}
      <p class="why" data-testid="onboarding-push-state">
        Off for this device — each phone or browser asks for itself. Turned on, this one gets the
        “did you finish it?” question when something plays to the end, and an invitation when
        someone starts a session.
      </p>
      <button
        class="btn-primary"
        data-testid="onboarding-push-enable"
        onclick={enable}
        disabled={busy === 'push'}
      >
        {busy === 'push' ? 'Waiting for the browser…' : 'Turn on notifications'}
      </button>
      {#if !vapidKey}
        <!-- Saying so beats a button that fails with a DOMException nobody can act on. -->
        <p class="why" data-testid="onboarding-push-unconfigured">
          This server has no push key configured yet, so your browser may refuse to register.
          The in-app prompts work regardless.
        </p>
      {/if}
    {/if}

    {#if pushNote}
      <p class="why" data-testid="onboarding-push-note" data-note={noteKind}>{pushNote}</p>
    {/if}

    <!-- Outside the state branches: the list is the account's, and a second phone needs it most. -->
    {#if devices.length}
      <ul class="list" data-testid="onboarding-devices">
        {#each devices as device (device.id)}
          <li data-testid="onboarding-device" data-device={scopeOf(device)}>
            <span>
              {device.device_label ?? 'Unnamed device'}{scopeOf(device) === 'this'
                ? ' · this device'
                : ''}
            </span>
            <span class="data">{device.device}</span>
          </li>
        {/each}
      </ul>
      <p class="why" data-testid="onboarding-devices-why">{devicesWhy}</p>
    {/if}
  </div>

  {#if !complete}
    <div class="row">
      <button class="btn-ghost" data-testid="onboarding-decline" onclick={finish}>
        Not now — don't ask again
      </button>
    </div>
  {/if}

  {#if error}
    <div class="err" role="alert" data-testid="onboarding-error">{error}</div>
  {/if}
</section>

<style>
  section {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  .step {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
  }
  .label {
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-3);
  }
  .steps {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 13.5px;
    color: var(--ink-2);
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    align-self: stretch;
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
    font-size: 13.5px;
  }
  .row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }
</style>
