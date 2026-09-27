<script>
  // Write-only, like the provider cards: the route answers booleans and a field is never filled
  // from the server.
  import { saveKey, spend, testConnector } from '$lib/spendGuard.svelte.js';

  let { source } = $props();

  const trakt = $derived(source.name === 'trakt');
  const stored = $derived(
    trakt ? Boolean(source.has_client_id && source.has_client_secret) : Boolean(source.has_api_key)
  );
  // Trakt's probe carries only the client id (decision 453), so the id alone is enough to test.
  const testable = $derived(trakt ? Boolean(source.has_client_id) : Boolean(source.has_api_key));

  // `saveKey` sends only the fields that hold text, so another source's fields never go out.
  let form = $state({ api_key: '', client_id: '', client_secret: '' });
  let saved = $state(null);
  let result = $state(null);
  let testing = $state(false);

  // Blank once trimmed is empty: `saveKey` sends nothing for it, so Save is not armed by it.
  const typed = $derived(Object.values(form).some((v) => v.trim() !== ''));

  async function saveThisKey() {
    saved = await saveKey(source.name, form);
  }

  async function runTest() {
    testing = true;
    result = null;
    try {
      result = await testConnector(source.name);
    } finally {
      testing = false;
    }
  }
</script>

<section
  class="source"
  data-source={source.name}
  data-required={String(Boolean(source.required))}
  data-has-key={String(stored)}
>
  <p class="why">
    {#if source.required}
      Required. Without it, new titles can't get their film details, so they wait until a key is
      saved.
    {:else}
      Optional. Without it, new titles still arrive, with a little less detail.
    {/if}
  </p>

  {#if source.secrets_unreadable}
    <p class="alert" role="alert" data-key-unreadable>
      The saved credentials can't be read with this install's <span class="code">SECRETS_KEY</span>.
      Restore the <span class="code">.env</span> that was current when the backup was taken, or type
      them again below.
    </p>
  {/if}

  {#if trakt}
    <label class="field">
      <span>Client ID</span>
      <input
        type="password"
        autocomplete="off"
        bind:value={form.client_id}
        placeholder={source.has_client_id ? 'Saved' : 'Paste the client ID'}
      />
    </label>
    <label class="field">
      <span>Client secret</span>
      <input
        type="password"
        autocomplete="off"
        bind:value={form.client_secret}
        placeholder={source.has_client_secret ? 'Saved' : 'Paste the client secret'}
      />
    </label>
  {:else}
    <label class="field">
      <span>API key</span>
      <input
        type="password"
        autocomplete="off"
        bind:value={form.api_key}
        placeholder={source.has_api_key ? 'Saved' : 'Paste a key'}
      />
    </label>
  {/if}

  <div class="actions">
    <button
      class="btn-primary"
      onclick={saveThisKey}
      disabled={!typed || spend.busy === `key-${source.name}`}
    >
      Save
    </button>
    <button class="btn-secondary" onclick={runTest} disabled={!testable || testing}>
      {testing ? 'Testing…' : 'Test'}
    </button>
  </div>
  {#if source.name === 'omdb'}
    <p class="footnote" data-quota>Testing spends one request of OMDb's daily quota.</p>
  {/if}
  {#if saved && !saved.ok && saved.error}<p class="err" role="alert">{saved.error}</p>{/if}
  {#if result}
    <p class="why" data-test-result={result.ok ? 'ok' : 'fail'}>
      {result.ok ? 'It works.' : `It didn't work: ${result.error ?? 'no reason was given'}`}
    </p>
  {/if}

  {#if source.used_by}
    <details class="tech">
      <summary>Technical details</summary>
      <p class="code">used by {source.used_by} · stage 2</p>
    </details>
  {/if}
</section>

<style>
  .source {
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
  .tech summary {
    display: flex;
    align-items: center;
    min-height: var(--touch);
    list-style: none;
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    cursor: pointer;
  }
  .tech summary::-webkit-details-marker {
    display: none;
  }
  .tech .code {
    padding: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
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
