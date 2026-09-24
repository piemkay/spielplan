<script>
  /**
   * One of §6.6's "TMDB / OMDb / Trakt keys with test buttons". Spec v2.1 §6.6, §8 stage 2,
   * §14.3; decisions 334, 452, 453; plan C1-C2, proposal 137.
   *
   * The same write-only idiom as the provider cards: the route answers booleans, the field is
   * never filled from the server, an empty field keeps the stored value, and the field is emptied
   * after every save. Trakt's client id is plaintext config on the server, but it is the value
   * every Trakt request carries, so it is written and reported the same way (decision 452).
   *
   * WHICH STAGE-2 SOURCES NEED A KEY IS SAID ON THE CARD (proposal 137, plan C2), so an acquisition
   * failure points at the right card: `required` is decision 334's rule -- TMDB is the one source
   * whose missing key parks a title at stage 2 -- and `used_by` is which stage-2 kinds read it.
   *
   * The test goes through the shared polite fetcher and stores nothing (decision 453). OMDb's free
   * tier is a daily request quota, so its button says the press spends one of them.
   */
  import { SOURCE_LABELS, saveKey, spend, testConnector } from '$lib/spendGuard.svelte.js';

  let { source } = $props();

  const title = $derived(SOURCE_LABELS[source.name] ?? source.name);
  const caps = $derived(title.toUpperCase());
  const trakt = $derived(source.name === 'trakt');
  const stored = $derived(
    trakt ? Boolean(source.has_client_id && source.has_client_secret) : Boolean(source.has_api_key)
  );
  // Trakt's probe is the public trending read, which carries the client id and not the secret
  // (decision 453), so the id alone is enough to test.
  const testable = $derived(trakt ? Boolean(source.has_client_id) : Boolean(source.has_api_key));

  // Every credential field any source carries; the card renders its own and `saveKey` sends only
  // fields that hold text, so the others are never on the wire.
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
  class="card"
  data-source={source.name}
  data-required={String(Boolean(source.required))}
  data-has-key={String(stored)}
>
  <h2>{title}</h2>
  <div class="data">
    {source.required ? 'required' : 'best-effort'}{source.used_by ? ` · ${source.used_by}` : ''}
  </div>
  <p class="why">
    {#if source.required}
      Required: without this key stage 2 parks every title, because no other source answers the
      detail it reads.
    {:else}
      Best-effort: without it stage 2 goes on with the other sources and the title is simply
      thinner.
    {/if}
  </p>

  {#if source.secrets_unreadable}
    <p class="alert" role="alert" data-key-unreadable>
      The stored {title} credentials cannot be decrypted with this install's
      <code>SECRETS_KEY</code>. Restore the <code>.env</code> that was current when the backup was
      taken, or type them again below.
    </p>
  {/if}

  {#if trakt}
    <div class="grid">
      <label>
        <span class="data">TRAKT CLIENT ID</span>
        <input
          type="password"
          autocomplete="off"
          bind:value={form.client_id}
          placeholder={source.has_client_id ? '•••••••• (stored)' : 'paste a client id'}
        />
      </label>
      <label>
        <span class="data">TRAKT CLIENT SECRET</span>
        <input
          type="password"
          autocomplete="off"
          bind:value={form.client_secret}
          placeholder={source.has_client_secret ? '•••••••• (stored)' : 'paste a client secret'}
        />
      </label>
    </div>
  {:else}
    <label>
      <span class="data">{caps} KEY</span>
      <input
        type="password"
        autocomplete="off"
        bind:value={form.api_key}
        placeholder={source.has_api_key ? '•••••••• (stored)' : 'paste a key'}
      />
    </label>
  {/if}

  <div class="row">
    <button
      class="btn-primary"
      onclick={saveThisKey}
      disabled={!typed || spend.busy === `key-${source.name}`}
    >
      Save {title} key
    </button>
    <button class="btn-ghost" onclick={runTest} disabled={!testable || testing}>
      {testing ? 'Testing…' : `Test ${title}`}
    </button>
  </div>
  {#if source.name === 'omdb'}
    <p class="why" data-quota>Testing spends one request of OMDb's daily quota.</p>
  {/if}
  {#if saved && !saved.ok && saved.error}<p class="err" role="alert">{saved.error}</p>{/if}
  {#if result}
    <div class="data probe" data-test-result={result.ok ? 'ok' : 'fail'}>
      {#if result.ok}
        the key works{result.status ? ` (HTTP ${result.status})` : ''}
      {:else}
        failed: {result.error ?? 'no reason was reported'}
      {/if}
    </div>
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
  label {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
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
