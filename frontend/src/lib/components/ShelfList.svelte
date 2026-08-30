<script>
  /**
   * §6.0's six shelves, in the table's order. Spec v2.1 §6.0 (M2), §4.1 rule 5; decision 18;
   * proposals 20 and 28.
   *
   * The order is the server's (`SHELF_IDS`, verbatim from §6.0's normative table) and this
   * component preserves it rather than re-sorting: the table is the spec.
   *
   * With both toggles on, every ranking shelf arrives as two sections and leaves as two rows.
   * `shelfRows` is the only place that flattening happens, and it maps rather than concatenates
   * — there is no code path in which a Films array and a Series array meet.
   */
  import ShelfRow from '$lib/components/ShelfRow.svelte';
  import { shelfRows } from '$lib/home.svelte.js';

  let { payload, onSelect, loading = false } = $props();

  const rows = $derived(shelfRows(payload));
</script>

{#if loading && !rows.length}
  <p class="data" data-testid="shelves-loading">building your shelves…</p>
{:else if rows.length}
  <div class="shelves" data-testid="shelves" data-shelf-count={payload?.shelves_total ?? 0}>
    {#each rows as row (row.shelf + ':' + row.section.kind)}
      <ShelfRow section={row.section} shelfId={row.shelf} ranking={row.ranking} {onSelect} />
    {/each}
  </div>
{:else}
  <!-- Not an error. §6.0's rule is that a shelf which cannot justify itself is ABSENT, so an
       empty Home is a legible first-week state rather than a failure — proposal 20's copy for
       the two named cases arrives separately, as `degraded`. -->
  <div class="card empty" data-testid="shelves-empty">
    <h2>No shelf can say why it exists yet.</h2>
    <p class="why">
      Every shelf carries a one-line why in vocabulary terms, and one that cannot is suppressed
      rather than shown bare. Rate a few titles and they arrive.
    </p>
    <a class="btn-primary" href="/rate">Rate some titles</a>
  </div>
{/if}

<style>
  .shelves {
    display: flex;
    flex-direction: column;
  }
  .empty {
    padding: 26px;
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
