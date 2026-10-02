<script>
  // Copy and link are the server's: never compose copy or fall back to a bare /rate. The x hides the
  // row until tomorrow (decision 554); Home does the hiding.
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';

  let { banner, onHide = null } = $props();
</script>

{#if banner && banner.count > 0 && banner.copy}
  <div class="notice-bar" role="status" data-testid="pending-verdicts" data-count={banner.count}>
    <div class="thumb" aria-hidden="true"><RatePoster title={banner.named[0]} showName={false} /></div>
    <div class="text">
      <p class="headline" data-testid="pending-verdicts-copy">{banner.copy.headline}</p>
      <p class="line" data-testid="pending-verdicts-names">{banner.copy.names}</p>
    </div>
    <div class="act">
      <a class="pill" href={banner.cta.route} data-testid="pending-verdicts-cta" data-head={banner.head_title_ids.join(' ')}>
        {banner.cta.label}
      </a>
    </div>
    <button class="x" aria-label="Hide until tomorrow" onclick={() => onHide?.()}>
      <Icon name="close" size={18} />
    </button>
  </div>
{/if}
