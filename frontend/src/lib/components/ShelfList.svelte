<script>
  // The server's order is §6.0's table; never re-sort it.
  import ShelfRow from '$lib/components/ShelfRow.svelte';
  import { kindRegions, shelfRows } from '$lib/home.svelte.js';

  let { payload, onSelect, loading = false } = $props();

  const rows = $derived(shelfRows(payload));
  const regions = $derived(kindRegions(payload));
  const both = $derived((payload?.kinds ?? []).length > 1);
  const lettered = $derived(rows.some((row) => row.section.items.some((item) => item.tier)));
</script>

{#if loading && !rows.length}
  <p class="data" data-testid="shelves-loading">building your shelves…</p>
{:else if rows.length}
  <div class="shelves" data-testid="shelves" data-shelf-count={payload?.shelves_total ?? 0}>
    <p class="why legend" data-testid="shelves-from-library">
      Everything on these shelves is in your library.{#if lettered}{' '}<span data-testid="tier-legend"
          >Letters are tiers — a dashed outline means it's our guess, for a title you haven't rated
          yet.</span
        >{/if}
    </p>
    {#if both}
      {#each regions as region (region.kind)}
        <section class="region" data-testid="kind-region" data-kind={region.kind}>
          <h2 class="regionhead">{region.heading}</h2>
          {#each region.rows as row (row.shelf + ':' + row.section.kind)}
            <ShelfRow section={row.section} shelfId={row.shelf} ranking={row.ranking} {onSelect} />
          {/each}
        </section>
      {/each}
    {:else}
      {#each rows as row (row.shelf + ':' + row.section.kind)}
        <ShelfRow section={row.section} shelfId={row.shelf} ranking={row.ranking} {onSelect} />
      {/each}
    {/if}
  </div>
{:else}
  <div class="card empty" data-testid="shelves-empty">
    <h2>No shelves yet.</h2>
    <p class="why">
      Every shelf says why it is there, and one that can't isn't shown. Rate a few titles and they
      arrive.
    </p>
    <a class="btn-primary" href="/rate">Rate some titles</a>
  </div>
{/if}

<style>
  .shelves {
    display: flex;
    flex-direction: column;
  }
  .legend {
    margin: 0 0 16px;
  }
  .region + .region {
    margin-top: 10px;
    padding-top: 18px;
    border-top: 1px solid var(--line);
  }
  .regionhead {
    margin: 0 0 14px;
    font-size: 18px;
    font-weight: 600;
  }
  .empty {
    padding: var(--card-pad-roomy);
    display: flex;
    flex-direction: column;
    gap: 10px;
    align-items: center;
    text-align: center;
  }
  .empty h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
  }
  .empty .why {
    max-width: 48ch;
  }
</style>
