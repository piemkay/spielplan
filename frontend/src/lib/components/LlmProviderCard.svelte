<script>
  // The key is write-only: the route answers `has_api_key` only. Model and price go through the
  // page's one spend proposal, never the key route, because both move the estimate (decision 450).
  import {
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

<section class="provider" data-provider={card.name} data-configured={String(Boolean(card.configured))}>
  <p class="footnote" data-structured-output>Structured output: {card.structured_output}</p>

  <!-- `has_api_key` is false for a stored key that will not decrypt: ask `secrets_unreadable` first. -->
  {#if !card.configured}
    <p class="why" data-unconfigured>
      Not ready: {card.secrets_unreadable
        ? "its saved key won't open with this install's secrets key, so it can't be used."
        : card.has_api_key
          ? "no price is known for its model, so it can't be estimated or used."
          : "no key is saved, so it can't be used."}
    </p>
  {/if}

  {#if card.secrets_unreadable}
    <p class="alert" role="alert" data-key-unreadable>
      The saved key can't be read with this install's <span class="code">SECRETS_KEY</span>. Restore
      the <span class="code">.env</span> that was current when the backup was taken, or paste the key
      again below.
    </p>
  {/if}

  <label class="field">
    <span>API key</span>
    <input
      type="password"
      autocomplete="off"
      bind:value={form.api_key}
      placeholder={card.has_api_key
        ? 'Saved'
        : card.secrets_unreadable
          ? "Saved, but it won't open: paste it again"
          : 'Paste a key'}
    />
  </label>
  <div class="actions">
    <button
      class="btn-primary"
      onclick={saveThisKey}
      disabled={!form.api_key.trim() || spend.busy === `key-${card.name}`}
    >
      Save key
    </button>
    <button class="btn-secondary" onclick={runTest} disabled={!card.has_api_key || testing}>
      {testing ? 'Testing…' : 'Test key'}
    </button>
  </div>
  {#if saved && !saved.ok && saved.error}<p class="err" role="alert">{saved.error}</p>{/if}
  {#if result}
    <!-- The probe reads the free models list: it bills nothing, and a listed model may still 404. -->
    <p class="why" data-test-result={result.ok ? 'ok' : 'fail'}>
      {result.ok ? 'The key works.' : `It didn't work: ${result.error ?? 'no reason was given'}`}
    </p>
  {/if}

  {#key spend.epoch}
    <label class="field">
      <span>Model</span>
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
    <form class="prices" onsubmit={(e) => e.preventDefault()} onchange={(e) => pickPrice(e.currentTarget)}>
      <label class="field">
        <span>Price in</span>
        <input
          name="price_input"
          type="number"
          min="0"
          step="0.01"
          inputmode="decimal"
          value={priceIn}
          oninput={invalidate}
          onblur={askUnlessHalf}
          placeholder={basis && basis !== 'unknown' && !override ? String(basis.input) : 'Price'}
        />
      </label>
      <label class="field">
        <span>Price out</span>
        <input
          name="price_output"
          type="number"
          min="0"
          step="0.01"
          inputmode="decimal"
          value={priceOut}
          oninput={invalidate}
          onblur={askUnlessHalf}
          placeholder={basis && basis !== 'unknown' && !override ? String(basis.output) : 'Price'}
        />
      </label>
    </form>
  {/key}
  <p class="footnote">
    Prices are dollars per million tokens. Leave both empty to use the price Spielplan ships with.
  </p>
  {#if halfPrice}
    <p class="why" data-price-half>
      A price is both figures or neither, each a number of dollars per million tokens.
    </p>
  {/if}
  <p class="code" data-price-basis={basis === 'unknown' ? 'unknown' : (basis?.source ?? '')}>
    {#if basis === 'unknown' || !basis}
      price unknown: no figure is estimated for this model until a price is set
    {:else}
      {basisLine(basis)}
    {/if}
  </p>
  {#if touched}
    <p class="why" data-provider-pending>
      This changes what a title costs. Close this to see the new figure, then confirm or cancel it.
    </p>
  {/if}
</section>

<style>
  .provider {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding-top: 8px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: var(--fs-footnote);
    color: var(--text-2);
  }
  .prices {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin: 0;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .why,
  .footnote,
  .code {
    margin: 0;
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
