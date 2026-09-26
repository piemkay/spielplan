<script>
  // The key is write-only: the route answers `has_api_key` only. Model and price go through the
  // page's one spend proposal, never the key route, because both move the estimate (decision 450).
  import {
    PROVIDER_LABELS,
    amend,
    basisLine,
    invalidate,
    propose,
    reask,
    saveKey,
    spend,
    testConnector
  } from '$lib/spendGuard.svelte.js';

  let { card } = $props();

  const name = $derived(card.name);
  const title = $derived(PROVIDER_LABELS[card.name] ?? card.name);
  const caps = $derived(title.toUpperCase());

  let form = $state({ api_key: '' });
  let saved = $state(null);
  let result = $state(null);
  let testing = $state(false);

  const proposed = $derived(spend.proposal?.providers?.[card.name] ?? {});
  const named = (field) => Object.prototype.hasOwnProperty.call(proposed, field);
  const basis = $derived(card.price_basis);
  const override = $derived(basis && basis !== 'unknown' && basis.source === 'override');
  const model = $derived(named('model') ? (proposed.model ?? '') : (card.model ?? ''));
  const priceIn = $derived(
    named('price_input') ? (proposed.price_input ?? '') : override ? basis.input : ''
  );
  const priceOut = $derived(
    named('price_output') ? (proposed.price_output ?? '') : override ? basis.output : ''
  );
  const touched = $derived(Object.keys(proposed).length > 0);

  let halfPrice = $state(false);

  // A half-typed pair is not in the proposal, so asking again on leaving the field would put a
  // confirmable figure on screen beside a price field that says something else.
  const askUnlessHalf = () => (halfPrice ? null : reask());

  async function saveThisKey() {
    saved = await saveKey(card.name, form);
  }

  async function runTest() {
    testing = true;
    result = null;
    try {
      result = await testConnector(card.name);
    } finally {
      testing = false;
    }
  }

  function pickModel(value) {
    const typed = value.trim();
    // Empty is null, "back to the provider default"; the stored model typed again is no change.
    const next = typed === '' ? null : typed;
    const fields = { model: next === (card.model ?? null) ? undefined : next };
    propose(amend(spend.proposal, { providers: { [card.name]: fields } }));
  }

  // The override is a pair or nothing (the route refuses half of one), so hold a half-typed pair.
  function pickPrice(pair) {
    const inText = pair.elements.namedItem('price_input').value.trim();
    const outText = pair.elements.namedItem('price_output').value.trim();
    halfPrice = (inText === '') !== (outText === '');
    if (halfPrice) return;
    if (inText === '' && outText === '') {
      // Clearing a stored override is a change; clearing one that was only proposed is not.
      const fields = override
        ? { price_input: null, price_output: null }
        : { price_input: undefined, price_output: undefined };
      propose(amend(spend.proposal, { providers: { [card.name]: fields } }));
      return;
    }
    const input = Number(inText);
    const output = Number(outText);
    if (!Number.isFinite(input) || !Number.isFinite(output) || input < 0 || output < 0) {
      halfPrice = true;
      return;
    }
    const fields = { price_input: input, price_output: output };
    propose(amend(spend.proposal, { providers: { [card.name]: fields } }));
  }
</script>

<section
  class="card provider"
  data-provider={card.name}
  data-configured={String(Boolean(card.configured))}
>
  <h2>{title}</h2>
  <div class="data" data-structured-output>structured output · {card.structured_output}</div>

  <!-- `has_api_key` is false for a stored key that will not decrypt: ask `secrets_unreadable` first. -->
  {#if !card.configured}
    <p class="why" data-unconfigured>
      Not configured: {card.secrets_unreadable
        ? 'its stored key will not open under this SECRETS_KEY, so stage 6 cannot call it.'
        : card.has_api_key
          ? 'no price is known for its model, so stage 6 cannot estimate or call it.'
          : 'no key is stored, so stage 6 cannot call it.'}
    </p>
  {/if}

  {#if card.secrets_unreadable}
    <p class="alert" role="alert" data-key-unreadable>
      The stored {title} key cannot be decrypted with this install's <code>SECRETS_KEY</code>.
      Restore the <code>.env</code> that was current when the backup was taken, or paste the key
      again below.
    </p>
  {/if}

  <label>
    <span class="data">{caps} KEY</span>
    <input
      type="password"
      autocomplete="off"
      bind:value={form.api_key}
      placeholder={card.has_api_key
        ? '•••••••• (stored)'
        : card.secrets_unreadable
          ? 'stored, will not open: paste it again'
          : 'paste a key'}
    />
  </label>
  <div class="row">
    <button
      class="btn-primary"
      onclick={saveThisKey}
      disabled={!form.api_key.trim() || spend.busy === `key-${card.name}`}
    >
      Save {title} key
    </button>
    <button class="btn-ghost" onclick={runTest} disabled={!card.has_api_key || testing}>
      {testing ? 'Testing…' : `Test ${title}`}
    </button>
  </div>
  {#if saved && !saved.ok && saved.error}<p class="err" role="alert">{saved.error}</p>{/if}
  {#if result}
    <!-- The probe reads the free models list: it bills nothing, and a listed model may still 404. -->
    <div class="data probe" data-test-result={result.ok ? 'ok' : 'fail'}>
      {#if result.ok}
        the key works{result.status ? ` (HTTP ${result.status})` : ''}
      {:else}
        failed: {result.error ?? 'no reason was reported'}
      {/if}
    </div>
  {/if}

  {#key spend.epoch}
    <label>
      <span class="data">{caps} MODEL</span>
      <input
        type="text"
        list={`models-${name}`}
        autocomplete="off"
        value={model}
        oninput={invalidate}
        onchange={(e) => pickModel(e.currentTarget.value)}
        onblur={reask}
      />
    </label>
    <datalist id={`models-${name}`}>
      {#each card.models ?? [] as m (m)}<option value={m}></option>{/each}
    </datalist>
    <form
      class="grid"
      onsubmit={(e) => e.preventDefault()}
      onchange={(e) => pickPrice(e.currentTarget)}
    >
      <label>
        <span class="data">{caps} PRICE IN · $/1M</span>
        <input
          name="price_input"
          type="number"
          min="0"
          step="0.01"
          inputmode="decimal"
          value={priceIn}
          oninput={invalidate}
          onblur={askUnlessHalf}
          placeholder={basis && basis !== 'unknown' && !override ? String(basis.input) : 'override'}
        />
      </label>
      <label>
        <span class="data">{caps} PRICE OUT · $/1M</span>
        <input
          name="price_output"
          type="number"
          min="0"
          step="0.01"
          inputmode="decimal"
          value={priceOut}
          oninput={invalidate}
          onblur={askUnlessHalf}
          placeholder={basis && basis !== 'unknown' && !override ? String(basis.output) : 'override'}
        />
      </label>
    </form>
  {/key}
  {#if halfPrice}
    <p class="why" data-price-half>
      A price override is both figures or neither, each a number of dollars per million tokens.
    </p>
  {/if}
  <div class="data" data-price-basis={basis === 'unknown' ? 'unknown' : (basis?.source ?? '')}>
    {#if basis === 'unknown' || !basis}
      price unknown: no figure is estimated for this model until an override gives it one
    {:else}
      {basisLine(basis)}
    {/if}
  </div>
  {#if touched}
    <p class="why" data-provider-pending>
      A change to this card is pending: its cost is shown under Extraction, where it is confirmed
      or cancelled.
    </p>
  {/if}
</section>

<style>
  h2 {
    margin: 0 0 2px;
    font-size: 15px;
    font-weight: 600;
  }
  .card {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .provider[data-configured='false'] {
    border-style: dashed;
    border-color: var(--line-2);
    background: transparent;
  }
  .provider[data-configured='false'] h2 {
    color: var(--ink-3);
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin: 0;
  }
  .row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .probe {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
  }
  .why {
    margin: 0;
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
  @media (max-width: 720px) {
    .grid {
      grid-template-columns: 1fr;
    }
  }
</style>
