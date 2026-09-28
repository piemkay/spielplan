<script>
  // The server's order is §6.0's table; never re-sort it.
  import ShelfRow from '$lib/components/ShelfRow.svelte';
  import { kindRegions, shelfRows } from '$lib/home.svelte.js';

  let { payload, onSelect, loading = false } = $props();

  const rows = $derived(shelfRows(payload));
  const regions = $derived(kindRegions(payload));
  const both = $derived((payload?.kinds ?? []).length > 1);
</script>

{#if loading && !rows.length}
  <p class="footnote" data-testid="shelves-loading">Loading your shelves…</p>
{:else if rows.length}
  <div class="shelves" data-testid="shelves" data-shelf-count={payload?.shelves_total ?? 0}>
    {#if both}
      {#each regions as region (region.kind)}
        <section class="shelves" data-testid="kind-region" data-kind={region.kind}>
          <h2 class="list-header regionhead">{region.heading}</h2>
          {#each region.rows as row (row.shelf + ':' + row.section.kind)}
            <ShelfRow section={row.section} shelfId={row.shelf} {onSelect} level="h3" />
          {/each}
        </section>
      {/each}
    {:else}
      {#each rows as row (row.shelf + ':' + row.section.kind)}
        <ShelfRow section={row.section} shelfId={row.shelf} {onSelect} />
      {/each}
    {/if}
  </div>
{:else}
  <div class="card empty" data-testid="shelves-empty">
    <h2 class="section-title">No shelves yet</h2>
    <p class="why">Rate a few titles and your shelves arrive here, each with its reason.</p>
    <a class="btn-primary" href="/rate">Rate some titles</a>
  </div>
{/if}

<style>
  .shelves {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .regionhead {
    padding: 0;
    margin-bottom: -20px;
  }
  .empty {
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: center;
    text-align: center;
    padding: var(--card-pad-roomy);
  }
  .empty .why {
    margin: 0;
    max-width: 44ch;
  }
</style>
