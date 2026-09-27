<script>
  /** Placeholder for an unbuilt surface (spec §12); it never names a milestone (decision 486). */
  let { surface, summary, points = [] } = $props();
</script>

<div class="wrap" data-testid="placeholder">
  <header class="head">
    <h1 class="large-title">{surface}</h1>
    <p class="lead">{summary}</p>
  </header>
  <section class="group">
    {#if points.length}
      <h2 class="list-header">What it will do</h2>
      <ul class="list-group">
        {#each points as p}<li class="list-row">{p}</li>{/each}
      </ul>
    {/if}
    <p class="list-footer">Not built yet — this is coming in a later update.</p>
  </section>
</div>

<style>
  .wrap {
    max-width: 640px;
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .head {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .lead {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li.list-row {
    padding-block: 12px;
  }
</style>
