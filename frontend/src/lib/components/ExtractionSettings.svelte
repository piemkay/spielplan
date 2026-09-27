<script>
  // Nothing defaults on the client: every value starts from the server's `llm` row, and a change
  // is stored only through the estimate panel's Confirm (decision 450).
  import {
    PROVIDER_LABELS,
    about,
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
  const model = $derived(providers.find((p) => p.name === provider)?.model ?? '');
  const stored = $derived(llm?.estimate?.per_title_usd ?? 'unknown');

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

  // A field set back to its stored value leaves the proposal, so an unchanged plan asks no confirm.
  function edit(field, value, stored) {
    propose(amend(spend.proposal, { [field]: same(value, stored) ? undefined : value }));
  }

  function pickParallel(name, on) {
    const next = new Set(chosen);
    if (on) next.add(name);
    else next.delete(name);
    // Server order, and null rather than []: the route refuses an empty list.
    const ordered = providers.map((p) => p.name).filter((n) => next.has(n));
    edit('parallel_providers', ordered.length ? ordered : null, settings.parallel_providers);
  }

  function unknownReason(estimate) {
    if (estimate.reason) return estimate.reason;
    const unpriced = (estimate.providers ?? []).filter((_, i) => estimate.basis?.[i] === 'unknown');
    const who = unpriced.map(label).join(', ') || 'A planned provider';
    return (
      `${who} has no known price for its model, so nothing is spent on a guess. Set a price on ` +
      'its card to give it one.'
    );
  }
</script>

{#snippet technical(estimate, reason)}
  <details class="tech">
    <summary>Technical details</summary>
    <div class="code lines">
      {#each estimate.basis ?? [] as basis, i (i)}
        <p data-basis={estimate.providers?.[i] ?? ''}>
          {label(estimate.providers?.[i])} · {basisLine(basis)}
        </p>
      {/each}
      <p>
        assumes {tokens(estimate.input_tokens_assumed)} tokens in and
        {tokens(estimate.output_tokens_assumed)} billed out per call, thinking tokens included
      </p>
      {#if reason && estimate.reason}<p>{estimate.reason}</p>{/if}
    </div>
  </details>
{/snippet}

{#snippet figures(preview)}
  {@const estimate = preview.estimate ?? {}}
  {@const projected = preview.projected ?? {}}
  {@const unknown = estimate.per_title_usd === 'unknown'}
  {@const monthly = projected.monthly_usd}
  <div class="figures" data-plan="pending">
    <p class="per-title" data-per-title={estimate.per_title_usd}>
      <span class="big">{unknown ? 'Unknown' : usd(estimate.per_title_usd)}</span>
      <span class="footnote">
        a title{#if !unknown && estimate.passes}, {estimate.passes} pass{estimate.passes === 1
            ? ''
            : 'es'} on {(estimate.providers ?? []).map(label).join(' + ')}{/if}
      </span>
    </p>
    {#if unknown}
      <p class="why" data-unknown-reason>{unknownReason(estimate)}</p>
    {/if}
    <p
      class="why"
      data-projected={monthly === null || monthly === undefined
        ? 'no-history'
        : monthly === 'unknown'
          ? 'unknown'
          : 'figure'}
      data-exceeds={String(Boolean(projected.exceeds_remaining))}
    >
      {#if monthly === null || monthly === undefined}
        No titles have arrived yet, so there's no pace to project a month from.
      {:else if monthly === 'unknown'}
        The month can't be estimated while the cost per title is unknown.
      {:else}
        {usd(monthly)} a month at the last {projected.window_days} days' pace ({projected.titles}
        title{projected.titles === 1 ? '' : 's'}).
      {/if}
      {#if projected.remaining_usd !== null && projected.remaining_usd !== undefined}
        {usd(projected.remaining_usd)} left this month.
      {:else}
        No cap is set.
      {/if}
    </p>
    {#if projected.exceeds_remaining}
      <p class="alert" role="alert" data-projected-exceeds>
        That's more than the {usd(projected.remaining_usd)} left this month: before the month is out,
        new titles wait, marked “over spend cap”, and nothing more is spent.
      </p>
    {/if}
    {@render technical(estimate, false)}
  </div>
{/snippet}

<section class="plan" id="plan" data-testid="llm-extraction" aria-labelledby="plan-title">
  <h2 class="list-header" id="plan-title">How new titles are read</h2>
  {#if llm}
    <div class="list-group">
      <label class="list-row">
        <span class="grow">
          <span>Provider</span>
          {#if model}<span class="sub">{model}</span>{/if}
        </span>
        <select
          class="inline"
          aria-label="Provider"
          value={provider}
          disabled={spend.busy === 'confirm'}
          onchange={(e) =>
            edit('extraction_provider', e.currentTarget.value || null, settings.extraction_provider)}
        >
          <option value="">None, new titles wait</option>
          {#each providers as p (p.name)}
            <option value={p.name} disabled={!p.has_api_key}>
              {label(p.name)}{p.has_api_key ? '' : ' (add a key first)'}
            </option>
          {/each}
        </select>
      </label>
      <label class="list-row">
        <span class="grow">Passes</span>
        <select
          class="inline"
          aria-label="Passes"
          value={passes === null ? '' : String(passes)}
          disabled={spend.busy === 'confirm'}
          onchange={(e) => edit('passes', Number(e.currentTarget.value), settings.passes)}
        >
          {#if passes === null}<option value="" disabled>As planned</option>{/if}
          {#each passOptions as n (n)}
            <option value={String(n)}>{n}</option>
          {/each}
        </select>
      </label>
      <label class="list-row">
        <span class="grow">Parallel mode</span>
        <button
          class="switch"
          role="switch"
          aria-checked={parallel}
          aria-label="Parallel mode"
          disabled={spend.busy === 'confirm'}
          onclick={() => edit('parallel', !parallel, settings.parallel === true)}
        ><span class="knob"></span></button>
      </label>
      {#if parallel}
        {#each providers as p (p.name)}
          <label class="list-row indent" data-parallel={p.name}>
            <span class="grow">{label(p.name)}{p.has_api_key ? '' : ' (add a key first)'}</span>
            <input
              type="checkbox"
              aria-label="Run {label(p.name)} in parallel"
              checked={chosen.includes(p.name)}
              disabled={!p.has_api_key || spend.busy === 'confirm'}
              onchange={(e) => pickParallel(p.name, e.currentTarget.checked)}
            />
          </label>
        {/each}
      {/if}
      <div class="list-row" data-plan="stored">
        <span class="grow">Cost per title</span>
        <span class="value" data-per-title={stored}>
          {stored === 'unknown' ? 'Unknown' : `About ${about(stored)}`}
        </span>
      </div>
    </div>
    <p class="list-footer">Every change shows its cost before it's saved.</p>
    <p class="list-footer">
      Parallel mode reads each title with every provider picked and keeps the union of their tags,
      with agreement as confidence: the union finds 93% of tags against 67% for the intersection,
      measured over providers. Agreement is a weight, never a filter, and the merge counts runs, so
      two passes of one provider count like two providers.
    </p>

    {#if spend.proposal}
      <div
        class="card estimate"
        data-testid="spend-estimate"
        data-estimate-state={panelState}
        aria-live="polite"
      >
        <h3>Before this is saved</h3>
        {#if spend.refused}
          <p class="alert" role="alert" data-estimate-refused>{spend.refused}</p>
        {/if}
        {#if spend.pending}
          {@render figures(spend.pending.preview)}
          {#if spend.pending.preview?.blocked}
            <p class="alert" role="alert" data-estimate-blocked>
              {spend.pending.preview.blocked}. Save that provider's key first: a plan naming a
              provider with no usable key is never saved.
            </p>
          {/if}
        {:else if spend.asking}
          <p class="footnote">Working out what it would cost…</p>
        {:else if spend.error}
          <p class="err" role="alert">{spend.error}</p>
        {:else}
          <p class="footnote">An edit is in progress: its cost is worked out when you leave the field.</p>
        {/if}
        <div class="actions">
          <button class="btn-primary" onclick={confirm} disabled={!canConfirm}>
            {spend.busy === 'confirm' ? 'Saving…' : 'Confirm'}
          </button>
          <button class="btn-secondary" onclick={cancel} disabled={spend.busy === 'confirm'}>
            Cancel
          </button>
        </div>
      </div>
    {:else}
      {@render technical(llm.estimate ?? {}, true)}
    {/if}
  {:else if !spend.llmError}
    <p class="footnote">Loading…</p>
  {/if}
</section>

<style>
  .plan {
    display: flex;
    flex-direction: column;
    scroll-margin-top: 16px;
  }
  .grow {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  label.list-row {
    cursor: pointer;
  }
  .indent {
    padding-left: 32px;
  }
  .indent input {
    width: 20px;
    height: 20px;
    margin: 0;
  }
  .list-row .value {
    margin-left: 0;
    font-variant-numeric: tabular-nums;
  }
  .inline {
    width: auto;
    min-width: var(--touch);
    max-width: 55%;
    min-height: var(--touch);
    padding: 0 28px 0 8px;
    background-color: transparent;
    background-position: right 4px center;
    color: var(--text-3);
    text-align: right;
    text-align-last: right;
  }
  .list-footer + .list-footer {
    padding-top: 8px;
  }
  .estimate {
    margin-top: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .figures {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .per-title {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 8px;
    font-variant-numeric: tabular-nums;
  }
  .big {
    font-size: var(--fs-section);
    line-height: 25px;
    font-weight: 600;
  }
  .why,
  .footnote {
    margin: 0;
    font-variant-numeric: tabular-nums;
  }
  .tech {
    margin-top: 8px;
  }
  .tech summary {
    display: flex;
    align-items: center;
    min-height: var(--touch);
    padding: 0 var(--gutter);
    list-style: none;
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    cursor: pointer;
  }
  .estimate .tech summary {
    padding: 0;
  }
  .tech summary::-webkit-details-marker {
    display: none;
  }
  .lines {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
    overflow-wrap: anywhere;
  }
  .estimate .lines {
    background: var(--surface-2);
  }
  .lines p {
    margin: 0;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .alert {
    margin: 0;
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--warning-tint);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .err {
    margin: 0;
    color: var(--negative);
    font-size: var(--fs-subhead);
  }
</style>
