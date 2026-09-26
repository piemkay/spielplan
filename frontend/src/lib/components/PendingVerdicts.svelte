<script>
  // Copy and link are the server's: never compose copy or fall back to a bare /rate, and drop the
  // CTA when its route does not carry every named title (proposal 150).
  import { bannerCountLine, bannerHref, bannerLabel, bannerText } from '$lib/home.svelte.js';

  let { banner } = $props();

  // Chosen by width rather than hidden with CSS: a hidden copy stays in the a11y tree. Chrome's
  // device emulation fires no resize, so there the register is only right on load.
  let width = $state(1024);
  const compact = $derived(width <= 720);

  const text = $derived(bannerText(banner, { compact }));
  const href = $derived(bannerHref(banner));
  const label = $derived(bannerLabel(banner, { compact }));
</script>

<svelte:window bind:innerWidth={width} />

{#if banner && banner.count > 0 && text}
  <div class="banner" role="status" data-testid="pending-verdicts" data-count={banner.count}>
    <div class="text">
      <div class="line" data-testid="pending-verdicts-copy">{text}</div>
      <div class="why names" data-testid="pending-verdicts-count">{bannerCountLine(banner)}</div>
    </div>
    {#if href}
      <a class="btn-primary" {href} data-testid="pending-verdicts-cta" data-head={banner.head_title_ids.join(' ')}>
        {label}
      </a>
    {:else}
      <span class="data broken" data-testid="pending-verdicts-no-cta">
        queue link unavailable — it would not start with the titles named
      </span>
    {/if}
  </div>
{/if}

<style>
  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    padding: 12px 15px;
    margin-bottom: 16px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-md);
  }
  .line {
    font-size: 14px;
  }
  .names {
    margin-top: 3px;
  }
  .broken {
    color: var(--ember-lift);
  }
</style>
