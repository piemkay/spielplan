<script>
  /**
   * First-boot wizard. Spec v2.1 §3.1 — the sequence is normative:
   *   create admin -> optional env-seeded connector config -> bundle import (the same importer
   *   the §6.6 Data tab exposes) -> member-account creation -> member first-run onboarding.
   *
   * The ribbon says "a bundle-less app is a legal state" because it is: the bundle step can be
   * skipped and the app still works, showing the no-bundle state on artifact-dependent surfaces.
   */
  import '$lib/design.css';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { get, post } from '$lib/api.js';
  import { bootstrap, session, setUser } from '$lib/session.svelte.js';
  import BundleImport from '$lib/components/BundleImport.svelte';

  const STEPS = [
    { key: 'admin', title: 'Create the admin account' },
    { key: 'connectors', title: 'Connectors' },
    { key: 'bundle', title: 'Import the bundle' },
    { key: 'members', title: 'Member accounts' },
    { key: 'onboarding', title: 'Onboard the phones' }
  ];

  let step = $state(0);
  let error = $state('');
  let busy = $state(false);

  // step 0
  let adminName = $state('admin');
  let adminPassword = $state('');

  // step 3
  let memberName = $state('');
  let members = $state([]);

  const done = $derived(new Set((session.setup?.steps ?? []).filter((s) => s.done).map((s) => s.step)));
  const hasAdmin = $derived(session.setup?.has_admin ?? false);

  onMount(async () => {
    if (!session.setup) await bootstrap();
    if (session.setup?.has_admin) step = Math.max(step, 1);
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

  async function addMember() {
    error = '';
    busy = true;
    try {
      const created = await post('/setup/members', { name: memberName, role: 'member' });
      members = [...members, created];
      memberName = '';
      await bootstrap();
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  async function finish() {
    await post('/setup/onboarding/complete').catch(() => {});
    await bootstrap();
    await goto('/');
  }
</script>

<div class="page">
  <div class="wrap">
    <div class="progress" role="progressbar" aria-valuenow={step + 1} aria-valuemax={STEPS.length}>
      {#each STEPS as s, i (s.key)}
        <button
          type="button"
          class:on={i <= step}
          class:done={done.has(s.key)}
          aria-label={`${s.title}${done.has(s.key) ? ' — done' : ''}`}
          aria-current={i === step ? 'step' : undefined}
          onclick={() => (step = i)}
        ></button>
      {/each}
    </div>
    <div class="ribbon data">{session.setup?.note ?? 'first boot · a bundle-less app is a legal state'}</div>

    <h1>{STEPS[step].title}</h1>

    {#if step === 0}
      <p class="why">One admin, then members. Passkeys can be added afterwards from the profile page.</p>
      <p class="why">
        Passkeys are bound to the public origin. Changing PUBLIC_URL later invalidates every
        registered credential.
      </p>
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
        <li><span>Jellyfin</span><span class="data">configure in Admin · M1</span></li>
        <li><span>LLM providers</span><span class="data">configure in Admin · M5</span></li>
        <li><span>TMDB / OMDb / Trakt</span><span class="data">configure in Admin · M5</span></li>
      </ul>
    {:else if step === 2}
      <p class="why">
        The same importer the Data tab exposes. Validation enforces every schema rule before
        anything is written.
      </p>
      <BundleImport onImported={() => bootstrap()} />
    {:else if step === 3}
      <p class="why">
        Needed before the rating milestone, whose exit criterion requires both members’ verdicts.
      </p>
      <div class="addrow">
        <input type="text" bind:value={memberName} placeholder="name" />
        <button class="btn-primary" onclick={addMember} disabled={busy || !memberName}>Add</button>
      </div>
      {#each members as m (m.id)}
        <div class="otp card">
          <div><strong>{m.name}</strong> <span class="data">{m.role}</span></div>
          <div class="data-lg">one-time password · <code>{m.one_time_password}</code></div>
          <div class="why">{m.note}</div>
        </div>
      {/each}
      {#if session.setup?.member_count}
        <div class="data">{session.setup.member_count} member account(s) exist</div>
      {/if}
    {:else}
      <p class="why">
        On iPhone, push only works once the app is on the home screen, and permission must be
        asked inside a tap. There is no programmatic install prompt, so this step walks each
        phone through it.
      </p>
      <ol class="rows numbered">
        <li>Share → Add to Home Screen</li>
        <li>Open from the home screen — standalone mode detected</li>
        <li>Enable notifications</li>
      </ol>
      <p class="why">
        Push stays best effort. Every prompt it carries also exists as an in-app banner, and
        sessions additionally as a room code.
      </p>
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
    min-height: 100vh;
    padding: 40px 24px;
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
  .progress button {
    flex: 1;
    height: 3px;
    padding: 0;
    border: none;
    border-radius: 2px;
    background: var(--line-2);
    cursor: pointer;
  }
  /* A step already recorded server-side reads as done even after a reload — that is the
     whole point of `setup_step`, and the wizard was computing it and dropping it. */
  .progress button.done {
    background: #5fae7a;
  }
  .progress button.on:not(.done) {
    background: var(--ember);
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
  .rows.numbered {
    list-style: decimal;
    padding-left: 20px;
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
  .rows.numbered li {
    display: list-item;
  }
  .addrow {
    display: flex;
    gap: 8px;
  }
  .otp {
    /* One per member added: the wizard's repeated-row idiom, and `.rows li` in steps 1
       and 4 is the same box playing the same role. */
    padding: var(--card-pad-tight);
    display: flex;
    flex-direction: column;
    gap: 5px;
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
