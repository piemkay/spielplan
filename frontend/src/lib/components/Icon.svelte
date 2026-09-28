<script module>
  /** The icon-tile colours, for row tiles and avatars (decision 527). */
  export const TONES = {
    blue: '#3d6fb6',
    purple: '#7a55b8',
    green: '#3f8f5a',
    teal: '#2f8c8c',
    amber: '#b7862c',
    red: '#c4473f',
    ember: '#c8613a',
    graphite: '#6b635b'
  };
</script>

<script>
  // One 24px grid, 1.75 stroke. With `tone`, a list row's 30px leading tile: a white glyph on a
  // TONES colour, or `warn`/`wait` on their tints. The four answers are names too.
  let { name, size = 24, tone = '' } = $props();

  const THUMB = [
    'M7 10.5V20H4.5a1 1 0 0 1-1-1v-7.5a1 1 0 0 1 1-1z',
    'M7 10.5 10.8 3.6a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.2a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.2 20H7'
  ];
</script>

{#if tone}
  <span class="tile {tone}" style:background={TONES[tone]} aria-hidden="true">
    {@render glyph(18)}
  </span>
{:else}
  {@render glyph(size)}
{/if}

{#snippet glyph(px)}
  <svg
    width={px}
    height={px}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    {#if name === 'home'}
      <path d="M3.5 10.5 12 4l8.5 6.5V20a1 1 0 0 1-1 1H15v-6H9v6H4.5a1 1 0 0 1-1-1z" />
    {:else if name === 'inbox'}
      <path d="M3.5 13.5 6 5.5h12l2.5 8V19a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1z" />
      <path d="M3.5 13.5H9l1 2h4l1-2h5.5" />
    {:else if name === 'person'}
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    {:else if name === 'people'}
      <circle cx="9" cy="9" r="3" />
      <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
      <circle cx="16.5" cy="8" r="2.5" />
      <path d="M16 13.5a4.5 4.5 0 0 1 5 4.5" />
    {:else if name === 'server'}
      <rect x="4" y="4" width="16" height="7" rx="1.5" />
      <rect x="4" y="13" width="16" height="7" rx="1.5" />
      <path d="M8 7.5h.01M8 16.5h.01" />
    {:else if name === 'budget'}
      <circle cx="12" cy="12" r="8.5" />
      <path
        d="M14.8 9.2c-.5-.9-1.6-1.4-2.8-1.4-1.6 0-2.8.8-2.8 2.1 0 2.9 5.8 1.5 5.8 4.3 0 1.3-1.3 2.1-3 2.1-1.3 0-2.5-.6-3-1.6M12 6v1.8M12 16.3V18"
      />
    {:else if name === 'database'}
      <ellipse cx="12" cy="6" rx="7.5" ry="2.5" />
      <path d="M4.5 6v12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5V6" />
      <path d="M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5" />
    {:else if name === 'pencil'}
      <path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z" />
      <path d="m14 7 3 3" />
    {:else if name === 'pulse'}
      <path d="M3 12h4l2.5-6 5 12 2.5-6h4" />
    {:else if name === 'check'}
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    {:else if name === 'warning'}
      <path d="M12 4 2.8 19.5h18.4z" />
      <path d="M12 10v4.5M12 17.2v.01" />
    {:else if name === 'info'}
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5M12 8v.01" />
    {:else if name === 'clock'}
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    {:else if name === 'lock'}
      <rect x="5" y="10.5" width="14" height="10" rx="2" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
    {:else if name === 'key'}
      <circle cx="8" cy="15" r="4" />
      <path d="m11 12 8.5-8.5M16 7l2.5 2.5M14 9l2 2" />
    {:else if name === 'bell'}
      <path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z" />
      <path d="M10 20.5a2 2 0 0 0 4 0" />
    {:else if name === 'home-screen'}
      <rect x="7" y="2.5" width="10" height="19" rx="2.5" />
      <path d="M12 9v6M9 12h6" />
    {:else if name === 'rank'}
      <path d="M9 20V9h6v11" />
      <path d="M3 20v-7h6" />
      <path d="M15 20v-5h6v5" />
      <path d="M2 20h20" />
    {:else if name === 'film'}
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M7.5 4.5v15M16.5 4.5v15M3.5 9h4M3.5 15h4M16.5 9h4M16.5 15h4" />
    {:else if name === 'play'}
      <path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none" />
    {:else if name === 'sparkle'}
      <path d="M12 3.5 13.8 10l6.7 2-6.7 2L12 20.5 10.2 14 3.5 12l6.7-2z" />
    {:else if name === 'dice'}
      <rect x="4" y="4" width="16" height="16" rx="3.5" />
      {#each [[8.5, 8.5], [15.5, 15.5], [15.5, 8.5], [8.5, 15.5], [12, 12]] as [cx, cy] (`${cx}-${cy}`)}
        <circle {cx} {cy} r="1.1" fill="currentColor" />
      {/each}
    {:else if name === 'sliders'}
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="10" cy="17" r="2" />
    {:else if name === 'chevron-right'}
      <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
    {:else if name === 'chevron-left'}
      <path d="M15 5 8 12l7 7" />
    {:else if name === 'plus'}
      <path d="M12 5v14M5 12h14" />
    {:else if name === 'minus'}
      <path d="M5 12h14" />
    {:else if name === 'copy'}
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" />
    {:else if name === 'share'}
      <path d="M12 3.5v11M8 7.5l4-4 4 4" />
      <path d="M6 11H5a1 1 0 0 0-1 1v7.5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V12a1 1 0 0 0-1-1h-1" />
    {:else if name === 'eye-off' || name === 'not_seen'}
      <path d="M3.5 3.5l17 17" />
      <path d="M10.6 5.1A9.6 9.6 0 0 1 12 5c5 0 8.5 4.5 9.5 7a13 13 0 0 1-2.7 3.9" />
      <path d="M6.6 6.6C4.6 7.9 3.2 9.9 2.5 12c1 2.5 4.5 7 9.5 7 1.7 0 3.2-.5 4.6-1.2" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    {:else if name === 'liked'}
      {#each THUMB as d (d)}<path {d} />{/each}
    {:else if name === 'disliked'}
      <g transform="rotate(180 12 12)">{#each THUMB as d (d)}<path {d} />{/each}</g>
    {:else if name === 'fine'}
      <circle cx="12" cy="12" r="8.5" /><path d="M8.5 14.5h7" /><path d="M9.2 9.8h.01M14.8 9.8h.01" />
    {/if}
  </svg>
{/snippet}

<style>
  svg {
    flex: none;
    display: block;
  }
  .tile {
    display: grid;
    place-items: center;
    flex: none;
    width: 30px;
    height: 30px;
    border-radius: var(--r-poster);
    color: #fff;
  }
  .tile.warn {
    background: var(--warning-tint);
    color: var(--warning);
  }
  .tile.wait {
    background: var(--surface-2);
    color: var(--text-2);
  }
</style>
