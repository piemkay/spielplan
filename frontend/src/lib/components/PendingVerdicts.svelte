<script>
  // Copy and link are the server's: never compose copy or fall back to a bare /rate.
  import RatePoster from '$lib/components/RatePoster.svelte';

  let { banner } = $props();
</script>

{#if banner && banner.count > 0 && banner.copy}
  <div class="card banner" role="status" data-testid="pending-verdicts" data-count={banner.count}>
    <span class="stack" aria-hidden="true">
      {#each banner.named.slice(0, 2) as title (title.title_id)}
        <span class="thumb"><RatePoster {title} showName={false} /></span>
      {/each}
    </span>
    <span class="text">
      <span class="line" data-testid="pending-verdicts-copy">{banner.copy.headline}</span>
      <span class="footnote line" data-testid="pending-verdicts-names">{banner.copy.names}</span>
    </span>
    <a class="btn-tinted cta hit" href={banner.cta.route} data-testid="pending-verdicts-cta" data-head={banner.head_title_ids.join(' ')}>
      {banner.cta.label}
    </a>
  </div>
{/if}

<style>
  .banner {
    max-width: 560px;
    min-height: 60px;
    margin-bottom: 24px;
    padding: 0 4px 0 12px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .stack {
    display: flex;
    flex: none;
  }
  .thumb {
    width: 28px;
    border-radius: var(--r-xs);
  }
  .thumb :global(.poster) {
    border-radius: var(--r-xs);
  }
  .thumb:first-child {
    position: relative;
    z-index: 1;
    box-shadow: 0 0 0 2px var(--surface-1);
  }
  .thumb + .thumb {
    margin-left: -16px;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .line {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .line:first-child {
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .cta {
    min-height: 32px;
    margin: 0 8px;
    padding: 0 14px;
    border-radius: var(--r-pill);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
</style>
