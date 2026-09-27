<script>
  // Money in one place (decision 527): the cap, the plan, the providers and the queue's Launch.
  import { onMount } from 'svelte';
  import { PROVIDER_LABELS, openSpendGuard, spend } from '$lib/spendGuard.svelte.js';
  import ExtractionSettings from '$lib/components/ExtractionSettings.svelte';
  import FlywheelQueue from '$lib/components/FlywheelQueue.svelte';
  import LlmProviderCard from '$lib/components/LlmProviderCard.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import SpendMeter from '$lib/components/SpendMeter.svelte';
  import Icon from '$lib/components/Icon.svelte';

  let open = $state('');

  const providers = $derived(spend.llm?.providers ?? []);
  const card = $derived(providers.find((p) => p.name === open) ?? null);
  const settings = $derived(spend.llm?.settings ?? {});
  const title = $derived(PROVIDER_LABELS[open] ?? open);

  const inUse = (name) =>
    settings.extraction_provider === name ||
    (settings.parallel === true && (settings.parallel_providers ?? []).includes(name));

  function standing(p) {
    if (p.secrets_unreadable) return { tone: 'bad', word: "Key can't be read" };
    if (inUse(p.name)) {
      return p.configured ? { tone: 'ok', word: 'In use' } : { tone: 'bad', word: 'In use, not ready' };
    }
    if (p.has_api_key && !p.configured) return { tone: 'warn', word: 'No price' };
    return { word: p.has_api_key ? 'Key saved' : 'No key' };
  }

  onMount(() => {
    openSpendGuard();
  });
</script>

<div class="page">
  <h1 class="large-title">Budget &amp; AI</h1>

  <SpendMeter />

  <ExtractionSettings />

  <section class="group" aria-labelledby="providers-title">
    <h2 class="list-header" id="providers-title">AI providers</h2>
    {#if spend.llm}
      <div class="list-group">
        {#each providers as p (p.name)}
          {@const s = standing(p)}
          <button class="list-row" data-provider-row={p.name} onclick={() => (open = p.name)}>
            <span class="grow">{PROVIDER_LABELS[p.name] ?? p.name}</span>
            {#if s.tone}
              <span class="badge {s.tone}"><span class="dot" aria-hidden="true"></span>{s.word}</span>
            {:else}
              <span class="value">{s.word}</span>
            {/if}
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </button>
        {/each}
      </div>
      <p class="list-footer">A saved key is never shown again.</p>
    {/if}
  </section>

  <FlywheelQueue />
</div>

<Sheet open={Boolean(card)} onClose={() => (open = '')} label={title}>
  {#snippet children(close)}
    <div class="bar">
      <h2>{title}</h2>
      <button class="btn-plain" onclick={close}>Done</button>
    </div>
    {#if card}<LlmProviderCard {card} />{/if}
  {/snippet}
</Sheet>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 32px;
    max-width: 720px;
  }
  .large-title {
    padding-top: 8px;
    margin-bottom: -16px;
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  .grow {
    flex: 1;
    min-width: 0;
  }
  .list-row .value {
    margin-left: 0;
  }
  .badge .dot {
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
    background: currentColor;
  }
  .chev {
    display: grid;
    color: rgba(245, 240, 232, 0.35);
    margin-right: -4px;
  }
  .bar {
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 4px 0 8px;
    background: var(--bg-elevated);
  }
  .bar h2 {
    flex: 1;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .bar .btn-plain {
    margin-right: -8px;
    font-weight: 600;
  }
</style>
