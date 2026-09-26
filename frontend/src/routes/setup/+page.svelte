<script>
  import '$lib/design.css';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { get, post } from '$lib/api.js';
  import { bootstrap, session, setUser } from '$lib/session.svelte.js';
  import BundleImport from '$lib/components/BundleImport.svelte';

  const STEPS = [
    { key: 'admin', title: 'Create the admin account' },
    { key: 'connectors', title: 'Connectors' },
    { key: 'bundle', title: 'Import the bundle' }
  ];

  let step = $state(0);
  let error = $state('');
  let busy = $state(false);
  // Whether an import finished on this screen, whose own line then says whether it is served.
  let importedHere = $state(false);

  let adminName = $state('admin');
  let adminPassword = $state('');

  const done = $derived(new Set((session.setup?.steps ?? []).filter((s) => s.done).map((s) => s.step)));
  // The anonymous `/setup/state` has no `has_admin`; `required` means exactly "no admin exists",
  // and an unread payload counts as an admin existing, so a stranger never gets the form.
  const hasAdmin = $derived(!(session.setup?.required ?? false));

  onMount(async () => {
    if (!session.setup) await bootstrap();
    if (hasAdmin) step = Math.max(step, 1);
  });

  async function createAdmin() {
    error = '';
    busy = true;
    try {
      const user = await post('/setup/admin', { name: adminName, password: adminPassword });
      setUser({ ...user, must_change_password: false });
      await bootstrap();
      step = 1;
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  // Onboarding is recorded on the member's own device, so Finish only navigates.
  async function finish() {
    await goto('/');
  }
</script>

<div class="page">
  <div class="wrap">
    <!-- The role sits on a childless sibling: a progressbar's children are presentational. -->
    <div class="progress">
      {#each STEPS as s, i (s.key)}
        <button
          type="button"
          data-testid="setup-step"
          class:on={i <= step}
          class:done={done.has(s.key)}
          aria-label={`${s.title}${done.has(s.key) ? ' — done' : ''}`}
          aria-current={i === step ? 'step' : undefined}
          onclick={() => (step = i)}
        ></button>
      {/each}
    </div>
    <span
      class="progress-value"
      role="progressbar"
      aria-label="Setup progress"
      aria-valuenow={step + 1}
      aria-valuemin="1"
      aria-valuemax={STEPS.length}
    ></span>
    <div class="ribbon data">{session.setup?.note ?? 'first boot · a bundle-less app is a legal state'}</div>

    <h1>{STEPS[step].title}</h1>

    {#if step === 0}
      <p class="why">
        One admin. Everyone else is added afterwards from Admin &gt; Users, which is the only
        place accounts are made. Passkeys can be added from the profile page.
      </p>
      <p class="why">
        Passkeys are bound to the public origin. Changing PUBLIC_URL later invalidates every
        registered credential.
      </p>
      <div class="data-lg" data-testid="setup-public-url">
        <code>{session.publicUrl || 'PUBLIC_URL is not set'}</code>
      </div>
      {#if hasAdmin}
        <p class="note">An admin account already exists — this step is done.</p>
      {:else}
        <label><span class="data">NAME</span><input type="text" bind:value={adminName} /></label>
        <label>
          <span class="data">PASSWORD · AT LEAST 10 CHARACTERS</span>
          <input type="password" bind:value={adminPassword} autocomplete="new-password" />
        </label>
      {/if}
    {:else if step === 1}
      <p class="why">
        Optional now, changeable later in Admin. Env vars may seed these on first boot for
        automated installs.
      </p>
      <ul class="rows">
        <li>
          <span>Jellyfin</span><a class="go" href="/admin/connectors">configure in Admin</a>
        </li>
        <li>
          <span>LLM providers</span><a class="go" href="/admin/connectors">configure in Admin</a>
        </li>
        <li>
          <span>TMDB / OMDb / Trakt</span><a class="go" href="/admin/connectors">configure in Admin</a>
        </li>
      </ul>
    {:else}
      <p class="why">
        The same importer the Data tab exposes. Validation enforces every schema rule before
        anything is written. The imported bundle is served as soon as the import finishes -
        nothing needs restarting.
      </p>
      <BundleImport
        onImported={() => {
          importedHere = true;
          return bootstrap();
        }}
      />
      <!-- An import watched on this screen reports the restart in its own line. -->
      {#if session.restartRequired && !importedHere}
        <p class="err" data-restart-required>
          A bundle is imported, but the backend could not load it by itself (its log says why).
          Restart backend and worker: <code>docker compose restart backend worker</code>
        </p>
      {/if}
    {/if}

    {#if error}<div class="err">{error}</div>{/if}

    <div class="actions">
      <button class="btn-ghost" onclick={() => (step = Math.max(0, step - 1))} disabled={step === 0}>
        Back
      </button>
      {#if step === 0 && !hasAdmin}
        <button class="btn-primary" onclick={createAdmin} disabled={busy || adminPassword.length < 10}>
          {busy ? 'Creating…' : 'Create admin'}
        </button>
      {:else if step === STEPS.length - 1}
        <button class="btn-primary" onclick={finish}>Finish</button>
      {:else}
        <button class="btn-primary" onclick={() => (step = step + 1)}>Continue</button>
      {/if}
    </div>
  </div>
</div>

<style>
  .page {
    /* The status-bar inset for the installed app (viewport-fit=cover). */
    min-height: 100vh;
    padding: max(40px, env(safe-area-inset-top)) 24px 24px;
    display: flex;
    justify-content: center;
  }
  .wrap {
    width: min(640px, 100%);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .progress {
    display: flex;
    gap: 6px;
  }
  /* A 3px bar drawn as buttons: restate `min-height`, which design.css's coarse floor raises. */
  .progress button {
    flex: 1;
    height: 3px;
    min-height: 3px;
    padding: 0;
    border: none;
    border-radius: 2px;
    background: var(--progress-track);
    cursor: pointer;
  }
  /* A step the server recorded is behind you, even ahead of the cursor. */
  .progress button.done {
    background: var(--progress-fill);
  }
  /* The progress ramp, not the accent: the primary action here is Create admin (§6.8). */
  .progress button.on:not(.done) {
    background: var(--progress-now);
  }
  /* Visually hidden but announced; `display: none` would drop the progress semantics. */
  .progress-value {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .ribbon {
    letter-spacing: 0.02em;
  }
  h1 {
    margin: 4px 0 0;
    font-size: 21px;
    font-weight: 600;
  }
  p.why {
    margin: 0;
  }
  .note {
    font-size: 12.5px;
    color: var(--ink-3);
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .rows li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--card);
    font-size: 13px;
  }
  .go {
    display: inline-flex;
    align-items: center;
    min-height: var(--touch);
    font-size: 12.5px;
  }
  code {
    font-family: var(--mono);
    color: var(--ember-lift);
    letter-spacing: 0.08em;
  }
  .actions {
    display: flex;
    gap: 10px;
    margin-top: 6px;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
  }
</style>
