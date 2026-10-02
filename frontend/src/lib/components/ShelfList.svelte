<script>
  // The server's order is §6.0's table; never re-sort it.
  import { onMount } from 'svelte';
  import Icon from '$lib/components/Icon.svelte';
  import ShelfRow from '$lib/components/ShelfRow.svelte';
  import WishListSheet from '$lib/components/WishListSheet.svelte';
  import { kindRegions, shelfRows } from '$lib/home.svelte.js';
  import { wishRowShown, wishSummary } from '$lib/wish.svelte.js';

  // `stale`: the shelves on screen answer another kind than the one asked for. `onHideWish` puts the
  // wish-list row away until tomorrow (decision 554).
  let { payload, onSelect, onHideWish = null, loading = false, stale = false } = $props();

  const rows = $derived(shelfRows(payload));
  const regions = $derived(kindRegions(payload));
  const both = $derived((payload?.kinds ?? []).length > 1);

  // Rows drawn as the list mounts (Home kept across tabs) are already there: only later ones arrive.
  let mounted = $state(false);
  onMount(() => (mounted = true));

  let wishOpen = $state(false);
</script>

<!-- Once, after the last shelf: under Worth getting, which the table puts last (decision 544). -->
{#snippet wishRow()}
  {#if wishRowShown(payload)}
    <div class="notice-bar wishrow" data-testid="home-wish-row">
      <span class="dot" aria-hidden="true"><Icon name="bookmark" size={16} /></span>
      <div class="text">
        <p class="headline">Wish list</p>
        <p class="line">{wishSummary(payload.wish)}</p>
      </div>
      <div class="act">
        <button class="pill" data-testid="home-wish-open" onclick={() => (wishOpen = true)}>See all</button>
      </div>
      <button class="x" aria-label="Hide until tomorrow" onclick={() => onHideWish?.()}>
        <Icon name="close" size={18} />
      </button>
    </div>
  {/if}
{/snippet}

{#if loading && !rows.length}
  <div class="shelves loading" data-testid="shelves-loading">
    <p class="sr-only" role="status">Loading your shelves…</p>
    {#each { length: 3 }, i (i)}
      <div class="skel" aria-hidden="true">
        <div class="bars"><span class="skeleton"></span><span class="skeleton"></span></div>
        <div class="cells">
          {#each { length: 4 }, j (j)}<span class="skeleton"></span>{/each}
        </div>
      </div>
    {/each}
  </div>
{:else if rows.length}
  <div
    class="shelves dims"
    data-testid="shelves"
    data-shelf-count={payload?.shelves_total ?? 0}
    aria-busy={stale}
  >
    {#if both}
      {#each regions as region (region.kind)}
        <section class="shelves" data-testid="kind-region" data-kind={region.kind}>
          <h2 class="list-header regionhead">{region.heading}</h2>
          {#each region.rows as row, i (row.shelf + ':' + row.section.kind)}
            <ShelfRow
              section={row.section}
              shelfId={row.shelf}
              {onSelect}
              level="h3"
              enter={mounted ? i : null}
            />
          {/each}
        </section>
      {/each}
    {:else}
      {#each rows as row, i (row.shelf + ':' + row.section.kind)}
        <ShelfRow section={row.section} shelfId={row.shelf} {onSelect} enter={mounted ? i : null} />
      {/each}
    {/if}
    {@render wishRow()}
  </div>
{:else if !payload?.setup_notice}
  <!-- Before the set-up Rate is closed and Home's notice already asks for it. -->
  <div class="card empty" data-testid="shelves-empty">
    <h2 class="section-title">No shelves yet</h2>
    <p class="why">Rate a few titles and your shelves arrive here, each with its reason.</p>
    <a class="btn-primary" href="/rate">Rate some titles</a>
  </div>
  {@render wishRow()}
{:else}
  {@render wishRow()}
{/if}

<WishListSheet open={wishOpen} onClose={() => (wishOpen = false)} {onSelect} />

<style>
  .shelves {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  /* Holds the status line, which design.css places absolutely. */
  .loading {
    position: relative;
  }
  .skel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .bars {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .bars > span {
    display: block;
    width: min(210px, 60%);
    height: 22px;
    border-radius: var(--r-xs);
  }
  .bars > span + span {
    width: min(150px, 45%);
    height: 16px;
  }
  .cells {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: 104px;
    gap: 10px;
    margin-right: calc(-1 * var(--gutter));
    overflow: hidden;
  }
  .cells > span {
    display: block;
    aspect-ratio: 2 / 3;
  }
  .regionhead {
    padding: 0;
    margin-bottom: -20px;
  }
  /* Closer to the shelf above than the shelves are to each other. */
  .shelves > .wishrow {
    margin-top: -12px;
  }
  .empty + .wishrow {
    margin-top: 16px;
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

  @media (min-width: 721px) {
    .cells {
      grid-auto-columns: var(--shelf-poster);
      gap: 16px;
      margin-right: 0;
    }
  }
</style>
