<script>
  /**
   * §6.0's six shelves, in the table's order. Spec v2.1 §6.0 (M2), §4.1 rule 5; decisions 18
   * and 474; proposals 20 and 28.
   *
   * The order is the server's (`SHELF_IDS`, verbatim from §6.0's normative table) and this
   * component preserves it rather than re-sorting: the table is the spec.
   *
   * With Both selected, Home is two kind regions - Films, then Series - each carrying the shelves
   * in table order (decision 474). It used to alternate a Films row and a Series row per shelf,
   * which read as series stacked under the films. `kindRegions` maps the payload's own
   * kind-grouped `sections`; there is still no code path in which a Films array and a Series array
   * meet, because every row is one kind-scoped section.
   */
  import ShelfRow from '$lib/components/ShelfRow.svelte';
  import { kindRegions, shelfRows } from '$lib/home.svelte.js';

  let { payload, onSelect, loading = false } = $props();

  const rows = $derived(shelfRows(payload));
  const regions = $derived(kindRegions(payload));
  const both = $derived((payload?.kinds ?? []).length > 1);
  // The letters' one sentence, said once for the screen rather than on every badge (§6.8's quiet
  // reasons): each badge also names itself, for a finger that asks one card.
  const lettered = $derived(rows.some((row) => row.section.items.some((item) => item.tier)));
</script>

{#if loading && !rows.length}
  <p class="data" data-testid="shelves-loading">building your shelves…</p>
{:else if rows.length}
  <div class="shelves" data-testid="shelves" data-shelf-count={payload?.shelves_total ?? 0}>
    <!-- Every shelf draws on the household's own library (`home/shelves.py` reads owned titles
         only), and nothing said so: the search grid marks owned cards "in library" and the shelves
         carry no such chip, so a member read the shelves as the wider catalog (second household
         test, U2). Said once for the screen, since a chip on every card would say nothing. -->
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
  <!-- Not an error. §6.0's rule is that a shelf which cannot justify itself is ABSENT, so an
       empty Home is a legible first-week state rather than a failure — proposal 20's copy for
       the two named cases arrives separately, as `degraded`. -->
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
    /* The empty state Home renders into the same slot as its own, and it must not
       differ from it. */
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
