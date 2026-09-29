<script>
  import '$lib/design.css';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { post } from '$lib/api.js';
  import { bootstrap, session, setUser } from '$lib/session.svelte.js';
  import BundleImport from '$lib/components/BundleImport.svelte';
  import FieldGroup from '$lib/components/FieldGroup.svelte';

  const STEPS = [
    { key: 'admin', title: 'Create the admin account', short: 'Admin' },
    { key: 'connectors', title: 'Connectors', short: 'Connectors' },
    { key: 'bundle', title: 'Import the bundle', short: 'Bundle' }
  ];

  let step = $state(0);
  let error = $state('');
  let busy = $state(false);
  // Whether an import finished on this screen, whose own line then says whether it is served.
  let importedHere = $state(false);

  let adminName = $state('');
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

{#snippet chevron()}
  <svg
    class="chev"
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
  </svg>
{/snippet}

<main class="wizard">
  <div class="column">
    <div class="top">
      <!-- The role sits on a childless sibling: a progressbar's children are presentational. -->
      <div class="steps">
        {#each STEPS as s, i (s.key)}
          <button
            type="button"
            data-testid="setup-step"
            class:on={i <= step}
            class:done={done.has(s.key)}
            aria-label={`${s.title}${done.has(s.key) ? ' — done' : ''}`}
            aria-current={i === step ? 'step' : undefined}
            onclick={() => (step = i)}
          >
            <span class="bar"></span>
            <span>{s.short}</span>
          </button>
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
      <p class="footnote">
        {session.setup?.note ?? 'Everything after the admin account can be done later in Admin.'}
      </p>
    </div>

    <h1 class="large-title">{STEPS[step].title}</h1>

    {#if step === 0}
      <p class="why">
        One admin account, and it is your own profile too: your ratings live on it. Everyone else
        is added later in Admin, under People — the only place accounts are made. Passkeys can be
        added from Account once you are in.
      </p>
      <section class="group">
        <h2 class="list-header">Passkeys work at</h2>
        <div class="list-group">
          <p class="list-row address" data-testid="setup-public-url">
            {session.publicUrl || 'PUBLIC_URL is not set'}
          </p>
        </div>
        <p class="list-footer">
          If this address ever changes, every passkey stops working and has to be added again.
        </p>
        <details class="technical">
          <summary>Technical details {@render chevron()}</summary>
          <p class="code">
            PUBLIC_URL sets this origin, and WebAuthn binds every registered credential to it.
          </p>
        </details>
      </section>
      {#if hasAdmin}
        <p class="why">An admin account already exists — this step is done.</p>
      {:else}
        <FieldGroup>
          <label>
            <span>Name</span>
            <input
              type="text"
              bind:value={adminName}
              autocomplete="username"
              autocapitalize="none"
              placeholder="Your name"
            />
          </label>
          <label>
            <span>Password</span>
            <input
              type="password"
              bind:value={adminPassword}
              autocomplete="new-password"
              placeholder="At least ten characters"
            />
          </label>
        </FieldGroup>
      {/if}
    {:else if step === 1}
      <p class="why">
        Optional now — all of these can be set later in Admin. Environment variables can fill them
        in on first boot, for automated installs. Import the bundle before you save Jellyfin: once it
        is saved, new library items start becoming titles, and one made before the import stays a
        second copy of a film the bundle brings.
      </p>
      <div class="list-group">
        <a class="list-row" href="/admin/services">
          <span>Jellyfin</span><span class="value">In Admin</span>{@render chevron()}
        </a>
        <a class="list-row" href="/admin/budget">
          <span>AI providers</span><span class="value">In Admin</span>{@render chevron()}
        </a>
        <a class="list-row" href="/admin/services">
          <span>TMDB, OMDb and Trakt</span><span class="value">In Admin</span>{@render chevron()}
        </a>
      </div>
    {:else}
      <p class="why">
        The same import as Admin's Movie data. It checks the whole bundle before writing anything,
        and the app serves it as soon as the import finishes — nothing needs restarting. This is
        the one movie-data import this install takes, so check the version in the report first.
      </p>
      <BundleImport
        onImported={() => {
          importedHere = true;
          return bootstrap();
        }}
      />
      <!-- An import watched on this screen reports the restart in its own line. -->
      {#if session.restartRequired && !importedHere}
        <div class="card restart" data-restart-required>
          <span class="badge warn">Restart needed</span>
          <p class="why">
            A bundle is imported, but the backend could not load it by itself (its log says why).
            Restart backend and worker:
          </p>
          <code class="code">docker compose restart backend worker</code>
        </div>
      {/if}
    {/if}

    <div class="actions">
      {#if error}<p class="err" role="alert">{error}</p>{/if}
      <div class="buttons">
        {#if step > 0}
          <button class="btn-secondary" onclick={() => (step = step - 1)}>Back</button>
        {/if}
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
</main>

<style>
  /* No shell here: the page carries the status-bar and home-indicator insets itself. */
  .wizard {
    min-height: 100vh;
    min-height: 100dvh;
    display: flex;
    justify-content: center;
    padding: max(24px, env(safe-area-inset-top)) max(var(--gutter), env(safe-area-inset-right))
      max(32px, env(safe-area-inset-bottom)) max(var(--gutter), env(safe-area-inset-left));
  }
  .column {
    width: min(560px, 100%);
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .top {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  /* Each step is a whole 48px control; the bar is only what it draws. */
  .steps {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    gap: 8px;
  }
  .steps button {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 8px;
    min-height: var(--touch);
    padding: 0;
    border: none;
    background: none;
    color: var(--text-3);
    font-size: var(--fs-caption);
    line-height: 16px;
    font-weight: 500;
    text-align: left;
  }
  .bar {
    display: block;
    height: 4px;
    border-radius: 2px;
    background: var(--progress-track);
  }
  /* A step the server recorded is behind you, even ahead of the cursor. */
  .done .bar {
    background: var(--progress-fill);
  }
  /* The progress ramp, not the accent: the primary action here is Create admin (§6.8). */
  .on:not(.done) .bar {
    background: var(--progress-now);
  }
  .steps [aria-current='step'] {
    color: var(--text);
    font-weight: 600;
  }
  /* Visually hidden but announced; `display: none` would drop the progress semantics. */
  .progress-value {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .top .footnote {
    margin: 0;
  }
  .why {
    margin: 0;
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  .address {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .technical {
    padding: 8px var(--gutter) 0;
  }
  .technical > summary {
    display: flex;
    align-items: center;
    gap: 4px;
    list-style: none;
    min-height: var(--touch);
    cursor: pointer;
    font-size: var(--fs-footnote);
    color: var(--text-2);
  }
  .technical > summary::-webkit-details-marker {
    display: none;
  }
  .technical[open] .chev {
    transform: rotate(90deg);
  }
  .technical .code {
    margin: 0;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
  }
  .list-row[href] {
    color: var(--text);
  }
  .chev {
    flex: none;
    color: rgba(245, 240, 232, 0.35);
  }
  .restart {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .actions {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .buttons {
    display: flex;
    gap: 12px;
  }
  .buttons .btn-primary {
    flex: 1;
    min-height: 50px;
    border-radius: var(--r-md);
  }
  .buttons .btn-secondary {
    min-height: 50px;
    border-radius: var(--r-md);
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
</style>
