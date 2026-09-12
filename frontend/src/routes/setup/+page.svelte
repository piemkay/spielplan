<script>
  /**
   * First-boot wizard. Spec v2.1 §3.1 — the sequence is normative and it ends at the bundle:
   *   create admin -> optional env-seeded connector config -> bundle import (the same importer
   *   the §6.6 Data tab exposes).
   *
   * Decision 164 took the last two steps away. Member accounts are made at §6.6's Users card,
   * because this page is the one place they could be made and it stopped being reachable the
   * moment an admin existed; member first-run onboarding is a per-phone act that happens on the
   * member's own device, not on the operator's screen during first boot.
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
    { key: 'bundle', title: 'Import the bundle' }
  ];

  let step = $state(0);
  let error = $state('');
  let busy = $state(false);

  // step 0
  let adminName = $state('admin');
  let adminPassword = $state('');

  const done = $derived(new Set((session.setup?.steps ?? []).filter((s) => s.done).map((s) => s.step)));
  // sec-14 took `has_admin` off the ANONYMOUS `/setup/state` payload, and this page is reachable
  // signed out. Read from a flag a stranger is no longer given, the check was `undefined`, so an
  // installed household app offered a passer-by "Create the admin account" with the fields
  // enabled (fe-46). `required` is the bit both callers get and §3.1 defines it as exactly "no
  // admin exists"; a payload not read yet counts as an admin existing, because the failure worth
  // defaulting against is showing the form to someone who must not see it.
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

  // The wizard no longer records `onboarding` on the operator's behalf: decision 164 leaves the
  // per-phone act to the member's own device (`push.js`'s `completeOnboarding`), so the last
  // step has nothing to write and Finish is a navigation.
  async function finish() {
    await goto('/');
  }
</script>

<div class="page">
  <div class="wrap">
    <!-- The steps are controls because §3.1's sequence is walkable: a step already done can be
         gone back to, and `01-first-boot.spec.js` reaches every one of the three that way. The
         container used to carry `role="progressbar"`, and ARIA makes a progressbar's children
         presentational — so the only controls that reach a step were announced as decoration, on
         the first screen a household ever meets. The role moves to a childless sibling, where it
         states the position (and the minimum it never had) without swallowing them.
         [§3.1; M4.15 finding 6, decision 280] -->
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
      <!-- §14 risk 4's whole mitigation is that this page warns loudly, and it was warning about
           a value it never showed (cs-33): the operator could not tell which origin they were
           about to bind every credential to without reading the server's environment. -->
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
        <li><span>Jellyfin</span><span class="data">configure in Admin · M1</span></li>
        <li><span>LLM providers</span><span class="data">configure in Admin · M5</span></li>
        <li><span>TMDB / OMDb / Trakt</span><span class="data">configure in Admin · M5</span></li>
      </ul>
    {:else}
      <p class="why">
        The same importer the Data tab exposes. Validation enforces every schema rule before
        anything is written.
      </p>
      <BundleImport onImported={() => bootstrap()} />
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
    /* `min-height: 100vh` stays: this page scrolls and carries no fixed bottom bar, so iOS
       Safari's large viewport costs it nothing. The top padding does move — `app.html` sets
       `viewport-fit=cover` and a translucent status bar, so the installed app draws from the
       physical top edge and a flat 40px starts the wizard under the clock. `max()` rather than
       an addition, so the page keeps exactly the padding it had everywhere `env()` is 0, which
       is every browser this suite runs. [§6 preamble; M4.15 finding 1] */
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
  /* Three 48px grey blocks above the heading, on the first screen a household ever meets. This
     is a 3px bar drawn as buttons, and `design.css`'s coarse block raises every `button` to
     `min-height: var(--touch)` — which a scoped rule declaring only `height` cannot outrank,
     because specificity decides a property a rule never states. §6's preamble writes that floor
     for the same phone this wizard's first screen is read on, so the coarse rule is right and
     it is this hairline that is the exception to it. The narrow fix is here, in the one
     component that is a bar drawn as buttons; decision 280 refuses the wide one, because a blanket
     `button { min-height: auto }` in design.css would spare this hairline by giving up the floor
     for everything else. [§6 preamble; M4.15 finding 6, decision 280] */
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
  /* A step already recorded server-side reads as done even after a reload — that is the
     whole point of `setup_step`, and the wizard was computing it and dropping it.
     In the progress ramp, not a hex: `design.css` mints --progress-track / -fill / -now for
     exactly three consumers and names "the first-boot wizard's steps" as one of them, and the
     rule beside it says a facet colour may not be borrowed, "because a facet colour is an
     identity that means one vocabulary term on every surface it appears". The literal here was
     `--facet-characters`, so a household that has seen the Map or a DNA chip had been taught
     that this green means one vocabulary term before it ever meant "step done". `--progress-fill`
     is the ramp's part-behind-you, and in §3.1's walkable sequence a step the server has recorded
     IS behind you whether or not the cursor has passed it — which is the distinction this rule
     and the one below exist to draw, and the ramp's brightness carries it.
     [§6.8; decision 276; M4.15 review cycle 1] */
  .progress button.done {
    background: var(--progress-fill);
  }
  /* Where you are in the sequence, in the progress ramp rather than the accent: §6.8 spends the
     ember on selection and primary actions, and on this screen the primary action is the button
     that creates the admin. [§6.8; decision 276] */
  .progress button.on:not(.done) {
    background: var(--progress-now);
  }
  /* Announced, never drawn: the bar a household sees IS the row above, and this carries the
     role so that row does not have to. `clip-path` rather than `display: none`, which would take
     it out of the accessibility tree as well and leave the wizard with no progress semantics at
     all. [decision 280] */
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
