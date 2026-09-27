<script>
  // The few icons Tonight draws, on the design's 24px grid (decision 527).
  let { name, size = 24 } = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.75"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {#if name === 'chevron-left'}
    <path d="M15 5 8 12l7 7" />
  {:else if name === 'chevron-right'}
    <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
  {:else if name === 'play'}
    <path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none" />
  {:else if name === 'sparkle'}
    <path d="M12 3.5 13.8 10l6.7 2-6.7 2L12 20.5 10.2 14 3.5 12l6.7-2z" />
  {:else if name === 'dice'}
    <rect x="4" y="4" width="16" height="16" rx="3.5" />
    {#each [[8.5, 8.5], [15.5, 15.5], [15.5, 8.5], [8.5, 15.5], [12, 12]] as [cx, cy] (`${cx}-${cy}`)}
      <circle {cx} {cy} r="1.1" fill="currentColor" />
    {/each}
  {:else if name === 'person'}
    <circle cx="12" cy="8.5" r="3.5" />
    <path d="M5 20a7 7 0 0 1 14 0" />
  {:else if name === 'people'}
    <circle cx="9" cy="9" r="3" />
    <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
    <circle cx="16.5" cy="8" r="2.5" />
    <path d="M16 13.5a4.5 4.5 0 0 1 5 4.5" />
  {:else if name === 'sliders'}
    <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
    <circle cx="16" cy="7" r="2" />
    <circle cx="10" cy="17" r="2" />
  {:else if name === 'share'}
    <path d="M12 3.5v11M8 7.5l4-4 4 4" />
    <path d="M6 11H5a1 1 0 0 0-1 1v7.5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V12a1 1 0 0 0-1-1h-1" />
  {:else if name === 'check'}
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  {:else if name === 'plus'}
    <path d="M12 5v14M5 12h14" />
  {:else if name === 'minus'}
    <path d="M5 12h14" />
  {/if}
</svg>

<style>
  svg {
    flex: none;
    display: block;
  }
</style>
