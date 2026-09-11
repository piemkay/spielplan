<script>
  /**
   * Member first-run onboarding. Spec v2.1 §6 preamble, §3.1 (the fifth setup step), §12 (M2).
   *
   * §6's preamble, which is the whole reason this is a screen and not a button:
   *
   *   "on iPhone, Web Push works only for a PWA added to the home screen (iOS 16.4+), and the
   *    permission request must run inside a user gesture; iOS has no programmatic install
   *    prompt, so member first-run onboarding *guides* Share → Add to Home Screen, detects
   *    standalone mode, and nags until push is granted."
   *
   * So this is a guided act with two halves, and the first half has two different mechanisms
   * depending on where it runs: a real Install button where the browser fires
   * `beforeinstallprompt`, and instructions where it never will. It detects which, and never
   * shows a button it cannot honour.
   *
   * Both halves are optional and BOTH endings complete §3.1's fifth step: "completed" and
   * "declined" are the same thing to a wizard, and an onboarding step that only completes on
   * yes is a step that blocks forever for anyone who says no.
   *
   * It lives on the account page because §3.2 already put the other per-device act — passkey
   * registration — there, and the forced first-login password change lands a new member on
   * that page (`/account?welcome=1`). What stops after completion is the *nag*: the controls
   * stay, because "turn notifications on later" has to be possible.
   */
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

  // Settled until the server says otherwise: a nag that flashes for one frame on every visit
  // to the account page is worse than one that arrives a beat late.
  let complete = $state(true);
  let vapidKey = $state(null);
  let devices = $state([]);
  let prompt = $state(null);
  let standalone = $state(false);
  // Optimistic until the Permissions API answers: "off, and here is the button" is the state
  // nearly everyone is in, and it is the one that costs nothing if it turns out to be wrong.
  let perm = $state('default');
  let busy = $state('');
  let error = $state('');
  let installOutcome = $state('');
  // This browser's own subscription, and the handle the server gave it. `devices` answers a
  // different question — `api/push.py`: "this member's devices. Never the household's" — and
  // deriving "on for this device" from that list is what told every member's SECOND phone it was
  // already registered, listed the first phone as if it were itself, and hid the enable button in
  // an unreachable `else`. Only the browser knows what the browser holds. [M4.11 finding 21]
  let localSub = $state(null);
  let localDevice = $state(null);
  // One line about this device's push state that is neither an error nor the state itself: a
  // subscription bound to a retired VAPID key, or an off switch that found nothing to switch off.
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
  // Deliberately NOT `perm === 'granted'`: granted-with-no-row is exactly the state a device is in
  // after the 90-day prune (§4.2), and a screen that read permission as registration would show
  // the off switch for a device the sender can no longer reach.

  /**
   * Which row in the list is the phone in the member's hand, where that is knowable.
   *
   * The server identifies a device by `sha256(endpoint)[:12]` and hands that handle back with every
   * subscribe, which is the only way this screen can learn it: §2 puts the app behind "one plain
   * HTTP port", and `crypto.subtle` does not exist outside a secure context, so the browser cannot
   * hash its own endpoint. Unknown stays unknown — a row is never called somebody else's device on
   * a guess.
   */
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
    // The browser may already hold a subscription this server has never been told about —
    // after a reinstall, or a service-worker update that rotated the endpoint. Re-posting is
    // an upsert on the endpoint, so it cannot fan out into duplicate rows.
    try {
      const synced = await syncSubscription({ vapidKey });
      if (synced?.stale) {
        // The key this subscription was minted against is not the key this server signs with, so
        // it is a device the push service will refuse to deliver to — off, with a reason, and the
        // member's own tap is what replaces it (`enablePush` unsubscribes first).
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
      // A device that cannot re-register is not an error worth a red box on the account page;
      // the button below is still there and still says what state it is in.
    }
  }

  /** §3.1's fifth step. Called on yes and on no alike — see the header. */
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

  /**
   * Straight from the click, with no await before `Notification.requestPermission()` — §6's
   * preamble requires the request to run inside the user gesture, and an `await` first is how
   * a gesture is lost.
   */
  async function enable() {
    busy = 'push';
    error = '';
    pushNote = '';
    noteKind = '';
    try {
      const result = await enablePush({ vapidKey });
      perm = result.permission === 'unsupported' ? perm : result.permission;
      if (result.subscriptions) devices = result.subscriptions;
      // From the act itself, not from a re-read: the endpoint is what makes this device "on" and
      // the handle is what marks its row. A second device that only re-read `/api/push/state`
      // would still be looking at the first one's row.
      if (result.endpoint) {
        localSub = result.endpoint;
        localDevice = result.device ?? null;
      }
      // Granted or denied, the member has answered the question.
      await finish();
    } catch (err) {
      // A refusal is an answer and lands above; this is a genuine failure — most often a
      // browser that will not mint a subscription without an application server key.
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
        // The DELETE answers with the member's REMAINING devices, so the other phone's row stays
        // on screen. `?? []` used to run for every answer, including the one where nothing was
        // deleted, and blanked a list of devices that were all still subscribed.
        if (result.subscriptions) devices = result.subscriptions;
      } else {
        // The same two fields, because `removed: false` is an answer about this browser and not
        // just an absence of one: both of `disablePush`'s early returns mean the browser holds no
        // `PushSubscription`, which is the single fact `localSub` models. Annotating without
        // clearing left `pushState` on 'on', so the card said "Notifications are on for this
        // device" directly above "there was nothing to turn off here", kept offering the off
        // switch, and kept the enable control — the only gesture §6's preamble lets ask for
        // permission — in the unreachable arm. That is finding 21's own defect, in the branch
        // written to repair it. [M4.11 finding 21; §4.2]
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
      <!-- §6 preamble: "iOS has no programmatic install prompt". There is no button to offer
           here — this list is the entire mechanism, not a consolation prize for one. -->
      <ol class="steps" data-testid="onboarding-ios-steps">
        <li>Tap the Share button in Safari's toolbar (the square with the arrow).</li>
        <li>Scroll down and choose <strong>Add to Home Screen</strong>.</li>
        <li>Open Spielplan from the new icon, then come back here for notifications.</li>
      </ol>
      <p class="why">
        Safari gives a page no way to ask to be installed, so these three taps are the whole
        mechanism on iOS — and on an iPhone notifications only work from that icon.
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
      <p class="why" data-testid="onboarding-push-state">
        This browser has no Web Push support. Nothing is lost: every prompt notifications would
        carry also waits for you inside the app.
      </p>
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
        <!-- The sending half ships with the M4 push stack and owns the key pair. Saying so is
             better than a button that fails with a DOMException nobody can act on. -->
        <p class="why" data-testid="onboarding-push-unconfigured">
          This server has no push key configured yet, so your browser may refuse to register.
          The in-app prompts work regardless.
        </p>
      {/if}
    {/if}

    {#if pushNote}
      <p class="why" data-testid="onboarding-push-note" data-note={noteKind}>{pushNote}</p>
    {/if}

    <!-- Outside the state branches, on purpose. §6's preamble makes notifications per-device and
         §4.2 keys the table on the member, so this list belongs to the ACCOUNT and is as worth
         showing to a device that is off as to one that is on: inside the `on` branch it was
         invisible to exactly the device that needed it — the member's second phone, which read
         the list as itself and was offered nothing but an off switch. [M4.11 finding 21] -->
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
