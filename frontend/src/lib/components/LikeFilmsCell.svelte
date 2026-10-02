<script>
  // Like these films, the Filters panel's last cell (decision 559): on a phone a row that opens the
  // picker as a sheet, from 721 px a field across the panel whose matches drop under it.
  import FilmPicker from './FilmPicker.svelte';
  import Icon from './Icon.svelte';

  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const desktop = $derived(width > 720);
  let open = $state(false);
</script>

<svelte:window bind:innerWidth={width} />

{#if desktop}
  <div class="cell wide" data-testid="filter-like">
    <span class="label">Like these films</span>
    <FilmPicker inline />
  </div>
{:else}
  <button class="cell list-row" data-testid="filter-like" onclick={() => (open = true)}>
    <span class="label">Like these films</span>
    <span class="value">Add<Icon name="chevron-right" size={16} /></span>
  </button>
  <FilmPicker {open} onClose={() => (open = false)} />
{/if}

<style>
  .list-row {
    width: 100%;
    text-align: left;
  }
  .value {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  /* Spans the panel's grid, as board C2 draws it; Home's own cells carry the same hairline. */
  .wide {
    grid-column: 1 / -1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 16px 14px;
    box-shadow: -0.5px -0.5px 0 var(--separator);
  }
  .wide > .label {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
</style>
