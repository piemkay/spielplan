<script>
  // Copy and link are the server's: never compose copy or fall back to a bare /rate.
  import { bannerCountLine, bannerLabel, bannerText } from '$lib/home.svelte.js';

  let { banner } = $props();

  // Chosen by width rather than hidden with CSS: a hidden copy stays in the a11y tree. Chrome's
  // device emulation fires no resize, so there the register is only right on load.
  let width = $state(1024);
  const compact = $derived(width <= 720);

  const text = $derived(bannerText(banner, { compact }));
  const label = $derived(bannerLabel(banner, { compact }));
</script>

<svelte:window bind:innerWidth={width} />

{#if banner && banner.count > 0 && text}
  <div class="card banner" role="status" data-testid="pending-verdicts" data-count={banner.count}>
    <div class="text">
      <p class="line" data-testid="pending-verdicts-copy">{text}</p>
      <p class="footnote" data-testid="pending-verdicts-count">{bannerCountLine(banner)}</p>
    </div>
    <a class="btn-tinted" href={banner.cta.route} data-testid="pending-verdicts-cta" data-head={banner.head_title_ids.join(' ')}>
      {label}
    </a>
  </div>
{/if}

<style>
  .banner {
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px 16px;
    flex-wrap: wrap;
  }
  .text {
    flex: 1 1 14rem;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  p {
    margin: 0;
  }
  .line {
    font-size: var(--fs-callout);
    line-height: 21px;
  }
</style>
