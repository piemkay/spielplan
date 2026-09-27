<script>
  // A settings row's leading tile: a white glyph on one of the tile colours (decision 527).
  let { name, tone = 'graphite' } = $props();

  const TONES = {
    blue: '#3d6fb6',
    green: '#3f8f5a',
    teal: '#2f8c8c',
    amber: '#b7862c',
    red: '#c4473f',
    ember: '#c8613a',
    graphite: '#6b635b'
  };
</script>

<span class="tile" style:background={TONES[tone]} aria-hidden="true">
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
  >
    {#if name === 'key'}
      <circle cx="8" cy="15" r="4" /><path d="m11 12 8.5-8.5M16 7l2.5 2.5M14 9l2 2" />
    {:else if name === 'lock'}
      <rect x="5" y="10.5" width="14" height="10" rx="2" /><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
    {:else if name === 'home-screen'}
      <rect x="7" y="2.5" width="10" height="19" rx="2.5" /><path d="M12 9v6M9 12h6" />
    {:else if name === 'bell'}
      <path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z" /><path d="M10 20.5a2 2 0 0 0 4 0" />
    {:else if name === 'rank'}
      <path d="M9 20V9h6v11" /><path d="M3 20v-7h6" /><path d="M15 20v-5h6v5" /><path d="M2 20h20" />
    {:else if name === 'film'}
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M7.5 4.5v15M16.5 4.5v15M3.5 9h4M3.5 15h4M16.5 9h4M16.5 15h4" />
    {:else if name === 'info'}
      <circle cx="12" cy="12" r="8.5" /><path d="M12 11v5M12 8v.01" />
    {/if}
  </svg>
</span>

<style>
  .tile {
    display: grid;
    place-items: center;
    flex: none;
    width: 30px;
    height: 30px;
    border-radius: 8px;
    color: #fff;
  }
</style>
