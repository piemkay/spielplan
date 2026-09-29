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
  import Icon from './Icon.svelte';

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
  const PUSH_WORD = { unsupported: 'Unavailable', on: 'On', denied: 'Blocked', off: 'Off' };
  const installed = $derived(standalone || installOutcome === 'accepted');

  // The server's device handle is the only way to mark this row: no `crypto.subtle` over plain HTTP.
  const scopeOf = (device) =>
    localDevice ? (device.device === localDevice ? 'this' : 'other') : 'unknown';
  const devicesWhy = $derived(
    !localSub
      ? 'Notifications reach these devices, not this one.'
      : localDevice
        ? 'Turning notifications off here leaves the others on.'
        : 'Notifications reach this device too. Turning them off here leaves the others on.'
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
          'Notifications stopped reaching this device after a change on the server. Turn them ' +
          'on again to fix it.';
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
        pushNote = 'Notifications were already off here. Your other devices keep theirs.';
      }
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = '';
    }
  }
</script>

<section
  class="device"
  data-testid="onboarding"
  data-onboarding-state={complete ? 'settled' : 'prompt'}
  data-platform={where}
  data-push-state={pushState}
  data-standalone={standalone}
>
  <h2 class="list-header">This device</h2>

  <ul class="list-group">
    <li>
      <div class="list-row">
        <Icon name="home-screen" tone="green" />
        <span>Add to Home Screen</span>
        <span class="value">{installed ? 'Installed' : 'Not yet'}</span>
      </div>
      <div class="detail">
        {#if standalone}
          <p data-testid="onboarding-installed">
            You are in the Home Screen app — on an iPhone, the only place notifications work.
          </p>
        {:else if where === 'ios-safari'}
          <!-- No direction word: the host moves the Sign-in group above or below this one. -->
          <ol class="steps" data-testid="onboarding-ios-steps">
            <li>
              Tap the Share button (the square with the arrow) — on newer iPhones it sits in the
              ••• menu beside the address bar.
            </li>
            <li>Scroll down and choose <strong>Add to Home Screen</strong>.</li>
            <li>Open Spielplan from the new icon and sign in there once.</li>
          </ol>
          <p>
            On an iPhone, notifications only work from that icon. The Home Screen app keeps its own
            sign-in, so it asks who you are once more — adding a passkey on this page first makes
            that a single tap.
          </p>
        {:else if where === 'ios-other'}
          <p data-testid="onboarding-ios-browser">
            On an iPhone only Safari can add Spielplan to the Home Screen. Open it in Safari to do
            it there.
          </p>
        {:else if prompt}
          <button
            class="btn-tinted"
            data-testid="onboarding-install"
            onclick={install}
            disabled={busy === 'install'}
          >
            {busy === 'install' ? 'Waiting for the browser…' : 'Install Spielplan'}
          </button>
        {:else}
          <p data-testid="onboarding-install-unavailable">
            This browser has not offered to install Spielplan — it may be installed already. It
            works the same in a tab.
          </p>
        {/if}
        {#if installOutcome}
          <p data-testid="onboarding-install-outcome">
            {installOutcome === 'accepted'
              ? 'Installed. Open Spielplan from the new icon from now on.'
              : 'Not installed — the browser prompt was dismissed.'}
          </p>
        {/if}
      </div>
    </li>

    <li>
      <div class="list-row">
        <Icon name="bell" tone="red" />
        <span>Notifications</span>
        <span class="value">{PUSH_WORD[pushState]}</span>
      </div>
      <div class="detail">
        {#if pushState === 'unsupported'}
          <!-- In a Safari tab the cause is the tab, not the browser: iOS pushes only to the home-screen app. -->
          {#if where === 'ios-safari'}
            <p data-testid="onboarding-push-state">
              On an iPhone, notifications come from the Home Screen app, not a Safari tab. Add the
              icon first and turn them on there — until then, everything they would tell you waits
              in the app.
            </p>
          {:else}
            <p data-testid="onboarding-push-state">
              This browser can't show notifications from Spielplan. Nothing is lost — everything
              they would tell you waits in the app.
            </p>
          {/if}
        {:else if pushState === 'on'}
          <p data-testid="onboarding-push-state">
            On for this device. Anything they tell you also waits in the app.
          </p>
          <button
            class="btn-secondary"
            data-testid="onboarding-push-disable"
            onclick={disable}
            disabled={busy === 'push'}
          >
            Turn notifications off
          </button>
        {:else if pushState === 'denied'}
          <p data-testid="onboarding-push-state">
            Notifications are blocked in this browser. Turn them on in its settings for this site.
          </p>
        {:else}
          <p data-testid="onboarding-push-state">
            Turn them on to be asked “Did you finish it?” when something plays to the end, and to
            hear when someone starts a Tonight room.
          </p>
          <button
            class="btn-tinted"
            data-testid="onboarding-push-enable"
            onclick={enable}
            disabled={busy === 'push'}
          >
            {busy === 'push' ? 'Waiting for the browser…' : 'Turn on notifications'}
          </button>
          {#if !vapidKey}
            <!-- Saying so beats a button that fails with a DOMException nobody can act on. -->
            <p data-testid="onboarding-push-unconfigured">
              Notifications are not set up on this server yet, so this may not work. Everything
              still shows up in the app.
            </p>
          {/if}
        {/if}

        {#if pushNote}
          <p data-testid="onboarding-push-note" data-note={noteKind}>{pushNote}</p>
        {/if}
      </div>
    </li>
  </ul>

  {#if complete}
    <p class="list-footer">Both are optional, and both are just for this device.</p>
  {:else}
    <p class="list-footer" data-testid="onboarding-prompt">
      Two things make Spielplan feel like an app on this phone: an icon on the Home Screen, and a
      nudge when something finishes playing. Both are optional and just for this device, and you
      will not be asked again.
    </p>
    <button class="btn-plain decline" data-testid="onboarding-decline" onclick={finish}>
      Not now — don't ask again
    </button>
  {/if}

  {#if error}
    <p class="err" role="alert" data-testid="onboarding-error">{error}</p>
  {/if}

  <!-- Outside the state branches: the list is the account's, and a second phone needs it most. -->
  {#if devices.length}
    <h3 class="list-header devices-head">Getting notifications</h3>
    <ul class="list-group" data-testid="onboarding-devices">
      {#each devices as device (device.id)}
        <li class="list-row" data-testid="onboarding-device" data-device={scopeOf(device)}>
          <span>{device.device_label ?? 'Unnamed device'}</span>
          {#if scopeOf(device) === 'this'}<span class="value">This device</span>{/if}
        </li>
      {/each}
    </ul>
    <p class="list-footer" data-testid="onboarding-devices-why">{devicesWhy}</p>
  {/if}
</section>

<style>
  .device {
    display: flex;
    flex-direction: column;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .list-group > li + li {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  /* A row's state in words, under its title: indented to the title, past the 30px tile. */
  .detail {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    padding: 0 var(--gutter) 12px 58px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .detail p {
    margin: 0;
  }
  .steps {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .steps strong {
    color: var(--text);
    font-weight: 600;
  }
  .detail .btn-tinted,
  .detail .btn-secondary {
    border-radius: var(--r-pill);
    font-size: var(--fs-subhead);
  }
  .decline {
    align-self: flex-start;
    margin-top: 4px;
    padding: 0 var(--gutter);
    color: var(--text-2);
  }
  .err {
    margin: 0;
    padding: 6px var(--gutter) 0;
    color: var(--negative);
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .devices-head {
    margin-top: 24px;
  }
</style>
