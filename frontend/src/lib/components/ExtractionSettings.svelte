<script>
  /**
   * §6.6's per-task assignment, parallel mode and pass count, and the estimate every change to them
   * shows before it is stored. Spec v2.1 §6.6, §8 stage 6, §9; decisions 324, 337, 338, 339, 343,
   * 450, 451.
   *
   * ONE ASSIGNMENT, BECAUSE M5 HAS ONE TASK (decision 339). §6.6 sketches three task slots and two
   * of them have no caller before M6, so this card offers the extraction provider alone: a setting
   * for a task nothing calls is a promise the surface cannot keep.
   *
   * NOTHING HERE DEFAULTS ON THE CLIENT. The toggle, the providers and the pass count start from
   * the `llm` row as the server read it, and an absent `passes` shows the count the server's own
   * plan uses (decision 324: off, one pass, stated once in `llm/spend`). A page that supplied its
   * own default would be a second place that decision lives.
   *
   * EVERY CHANGE IS A PROPOSAL, AND THE PANEL BELOW IS THE ONLY WAY TO STORE ONE (decision 450).
   * The panel is where the provider cards' model and price edits land too, because they move the
   * same figure. It shows the three numbers plan §7 check 1 names -- the per-title estimate with
   * the price it rests on, the projected month and what is left of the cap -- and Confirm carries
   * the figure it shows. Plan §9 is why they are not drawn alike: the per-title figure is small
   * enough to be waved through, and the month against the cap is the number that protects, so the
   * month carries the weight and its warning carries the alarm.
   *
   * Batch mode is rendered and never enabled (decision 338): the server says it is unavailable and
   * why, and a toggle shipped enabled and inert is the promise decision 338 refuses to make.
   */
  import {
    PROVIDER_LABELS,
    amend,
    basisLine,
    cancel,
    confirm,
    propose,
    spend,
    usd
  } from '$lib/spendGuard.svelte.js';

  const llm = $derived(spend.llm);
  const providers = $derived(llm?.providers ?? []);
  const settings = $derived(llm?.settings ?? {});
  const proposal = $derived(spend.proposal ?? {});
  const named = (field) => Object.prototype.hasOwnProperty.call(proposal, field);

  const provider = $derived(
    (named('extraction_provider') ? proposal.extraction_provider : settings.extraction_provider) ?? ''
  );
  const parallel = $derived(named('parallel') ? proposal.parallel : settings.parallel === true);
  const chosen = $derived(
    (named('parallel_providers') ? proposal.parallel_providers : settings.parallel_providers) ?? []
  );
  const passes = $derived(
    named('passes') ? proposal.passes : (settings.passes ?? llm?.estimate?.passes ?? null)
  );
  // The three the plan names, and a hand-typed stored count outside them shown as it is.
  const passOptions = $derived(
    [1, 2, 3].includes(Number(passes)) || passes === null ? [1, 2, 3] : [1, 2, 3, passes]
  );

  const canConfirm = $derived(
    Boolean(spend.pending) && !spend.pending.preview?.blocked && spend.busy !== 'confirm'
  );
  const panelState = $derived(
    spend.pending
      ? spend.pending.preview?.blocked
        ? 'blocked'
        : 'pending'
      : spend.asking
        ? 'asking'
        : spend.error
          ? 'error'
          : 'editing'
  );

  const label = (name) => PROVIDER_LABELS[name] ?? name;
  const tokens = (n) => (typeof n === 'number' ? n.toLocaleString('en') : '?');
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

  /**
   * One field amended into the page's proposal, or taken back out of it when it is back to what is
   * stored: a proposal that stores the value already there previews the same figure and asks for a
   * confirm that changes nothing.
   */
  function edit(field, value, stored) {
    propose(amend(spend.proposal, { [field]: same(value, stored) ? undefined : value }));
  }

  function pickParallel(name, on) {
    const next = new Set(chosen);
    if (on) next.add(name);
    else next.delete(name);
    // The server's own order, and null rather than [] for none: the route refuses an empty list,
    // and null is "clear parallel_providers" (decision 450).
    const ordered = providers.map((p) => p.name).filter((n) => next.has(n));
    edit('parallel_providers', ordered.length ? ordered : null, settings.parallel_providers);
  }

  /** Why the figure is "unknown", in the plan's own words, or the provider nobody priced. */
  function unknownReason(estimate) {
    if (estimate.reason) return estimate.reason;
    const unpriced = (estimate.providers ?? []).filter((_, i) => estimate.basis?.[i] === 'unknown');
    const who = unpriced.map(label).join(', ') || 'a planned provider';
    return (
      `${who} has no known price for its model. Stage 6 parks rather than bill at a guess; set a ` +
      'price override on its card to give it one (decision 343).'
    );
  }
</script>

{#snippet figures(preview, plan)}
  {@const estimate = preview.estimate ?? {}}
  {@const projected = preview.projected ?? {}}
  {@const unknown = estimate.per_title_usd === 'unknown'}
  <div class="figures" data-plan={plan}>
    <!-- The reassuring number, drawn in the quiet voice (plan §9). -->
    <div class="per-title" data-per-title={estimate.per_title_usd}>
      <span class="data">PER TITLE</span>
      <span class="data-lg">{unknown ? 'unknown' : usd(estimate.per_title_usd)}</span>
      {#if !unknown && estimate.passes}
        <span class="data">
          {estimate.passes} pass{estimate.passes === 1 ? '' : 'es'} on {(estimate.providers ?? [])
            .map(label)
            .join(' + ')}
        </span>
      {/if}
    </div>
    {#if unknown}
      <p class="why" data-unknown-reason>{unknownReason(estimate)}</p>
    {/if}
    {#each estimate.basis ?? [] as basis, i (i)}
      <div class="data" data-basis={estimate.providers?.[i] ?? ''}>
        {label(estimate.providers?.[i])} · {basisLine(basis)}
      </div>
    {/each}
    <div class="data">
      assumes {tokens(estimate.input_tokens_assumed)} tokens in and
      {tokens(estimate.output_tokens_assumed)} billed out per call, thinking tokens included (§9)
    </div>

    <!-- The protecting number, drawn with the weight (plan §9): what a month at this install's
         own rate costs, against what is left of this month's cap (decision 451). -->
    <div
      class="month"
      data-projected={projected.monthly_usd === null || projected.monthly_usd === undefined
        ? 'no-history'
        : projected.monthly_usd === 'unknown'
          ? 'unknown'
          : 'figure'}
      data-exceeds={String(Boolean(projected.exceeds_remaining))}
    >
      {#if projected.monthly_usd === null || projected.monthly_usd === undefined}
        <span class="big">no acquisition history yet</span>
      {:else if projected.monthly_usd === 'unknown'}
        <span class="big">unknown a month</span>
      {:else}
        <span class="big">{usd(projected.monthly_usd)} a month</span>
        <span class="why">
          at the {projected.titles} title{projected.titles === 1 ? '' : 's'} filed in the last
          {projected.window_days} days
        </span>
      {/if}
      <span class="why">
        {#if projected.remaining_usd !== null && projected.remaining_usd !== undefined}
          against {usd(projected.remaining_usd)} left of this month's cap
        {:else}
          with no cap set
        {/if}
      </span>
    </div>
    {#if projected.exceeds_remaining}
      <p class="alert" role="alert" data-projected-exceeds>
        That is more than the {usd(projected.remaining_usd)} left of this month's cap: before the
        month is out, stage 6 parks titles "over spend cap" and bills nothing more.
      </p>
    {/if}
    {#if projected.reason}<p class="why">{projected.reason}</p>{/if}
  </div>
{/snippet}

<section class="card" data-testid="llm-extraction">
  <h2>Extraction</h2>
  <p class="why">
    Stage 6 reads each title's pack and extracts its DNA with the provider assigned here. Every
    change shows what it would cost before anything is stored.
  </p>

  {#if spend.llmError}<p class="err" role="alert">{spend.llmError}</p>{/if}
  {#if llm}
    <label>
      <span class="data">EXTRACTION PROVIDER</span>
      <select
        value={provider}
        disabled={spend.busy === 'confirm'}
        onchange={(e) =>
          edit('extraction_provider', e.currentTarget.value || null, settings.extraction_provider)}
      >
        <option value="">none (stage 6 parks every title)</option>
        {#each providers as p (p.name)}
          <option value={p.name} disabled={!p.has_api_key}>
            {label(p.name)}{p.has_api_key ? '' : ' (add a key first)'}
          </option>
        {/each}
      </select>
    </label>

    <!-- The checkbox is outside design.css's coarse block, so the label is the 48 px target. -->
    <label class="check">
      <input
        type="checkbox"
        checked={parallel}
        disabled={spend.busy === 'confirm'}
        onchange={(e) => edit('parallel', e.currentTarget.checked, settings.parallel === true)}
      />
      <span>Parallel mode</span>
    </label>
    <!-- §6.6's consensus numbers as the toggle's caption, labelled with the population they were
         measured over, and decision 337's note that the merge counts runs. -->
    <p class="why">
      Runs extraction on each selected provider and merges by the measured consensus rule: the
      union, with per-tag agreement as confidence. Union recalls 93% against 67% for intersection,
      measured over providers; agreement is a weight, never a filter. The merge counts runs (one
      provider at one pass), so two passes of one provider pool exactly as two providers do.
    </p>
    {#if parallel}
      <fieldset class="parallel">
        <legend class="data">RUN IN PARALLEL</legend>
        {#each providers as p (p.name)}
          <label class="check">
            <input
              type="checkbox"
              checked={chosen.includes(p.name)}
              disabled={!p.has_api_key || spend.busy === 'confirm'}
              onchange={(e) => pickParallel(p.name, e.currentTarget.checked)}
            />
            <span>Run {label(p.name)} in parallel{p.has_api_key ? '' : ' (add a key first)'}</span>
          </label>
        {/each}
      </fieldset>
    {/if}

    <label>
      <span class="data">PASSES</span>
      <select
        value={passes === null ? '' : String(passes)}
        disabled={spend.busy === 'confirm'}
        onchange={(e) => edit('passes', Number(e.currentTarget.value), settings.passes)}
      >
        {#if passes === null}<option value="" disabled>as the server plans it</option>{/if}
        {#each passOptions as n (n)}
          <option value={String(n)}>{n} pass{Number(n) === 1 ? '' : 'es'}</option>
        {/each}
      </select>
    </label>

    <label class="check" data-batch>
      <input type="checkbox" checked={false} disabled />
      <span>Batch mode</span>
    </label>
    <p class="why" data-batch-reason>{llm.batch?.reason ?? 'batch endpoints are not used'}</p>

    {#if spend.proposal}
      <div
        class="estimate"
        data-testid="spend-estimate"
        data-estimate-state={panelState}
        aria-live="polite"
      >
        <h3>Before this is stored</h3>
        {#if spend.refused}
          <p class="alert" role="alert" data-estimate-refused>{spend.refused}</p>
        {/if}
        {#if spend.pending}
          {@render figures(spend.pending.preview, 'pending')}
          {#if spend.pending.preview?.blocked}
            <p class="alert" role="alert" data-estimate-blocked>
              {spend.pending.preview.blocked}. Save that provider's key on its card first: a plan
              naming a provider with no usable key is never stored (decision 450).
            </p>
          {/if}
        {:else if spend.asking}
          <p class="data">asking what it would cost…</p>
        {:else if spend.error}
          <p class="err" role="alert">{spend.error}</p>
        {:else}
          <p class="why">An edit is in progress: its figure is asked when you leave the field.</p>
        {/if}
        <div class="row">
          <button class="btn-primary" onclick={confirm} disabled={!canConfirm}>
            {spend.busy === 'confirm' ? 'Storing…' : 'Confirm'}
          </button>
          <button class="btn-ghost" onclick={cancel} disabled={spend.busy === 'confirm'}>
            Cancel
          </button>
        </div>
      </div>
    {:else}
      <div class="stored">
        <span class="data">STORED PLAN</span>
        {@render figures(llm, 'stored')}
      </div>
    {/if}
  {:else if !spend.llmError}
    <p class="data">loading…</p>
  {/if}
</section>

<style>
  h2 {
    margin: 0 0 6px;
    font-size: 15px;
    font-weight: 600;
  }
  h3 {
    margin: 0;
    font-size: 13.5px;
    font-weight: 600;
  }
  .card {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .check {
    flex-direction: row;
    align-items: center;
    gap: 10px;
    min-height: var(--touch);
    min-width: var(--touch);
    cursor: pointer;
    font-size: 13.5px;
  }
  .check input {
    width: 18px;
    height: 18px;
    margin: 0;
  }
  .check input:disabled + span {
    color: var(--ink-4);
  }
  .parallel {
    margin: 0;
    padding: 0;
    border: none;
    display: flex;
    flex-direction: column;
  }
  .why {
    margin: 0;
  }
  .estimate {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--line-2);
    border-radius: var(--r-sm);
  }
  .stored {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .figures {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .per-title {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
  }
  .month {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .big {
    font-size: 17px;
    font-weight: 600;
    color: var(--ink);
  }
  .month[data-exceeds='true'] .big {
    color: var(--ember-lift);
  }
  .row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .alert {
    margin: 0;
    padding: 8px 10px;
    border: 1px solid var(--ember-lift);
    border-radius: var(--r-sm);
    color: var(--ember-lift);
    font-size: 12.5px;
    line-height: 1.45;
  }
  .err {
    color: var(--ember-lift);
    font-size: 12.5px;
    margin: 0;
  }
</style>
