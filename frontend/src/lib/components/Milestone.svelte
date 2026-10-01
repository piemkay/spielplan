<script>
  /** Placeholder for an unbuilt surface (spec §12); it never names a milestone (decision 486). */
  let { surface, summary } = $props();
</script>

<div class="wrap" data-testid="placeholder">
  <header class="head">
    <h1 class="large-title">{surface}</h1>
    <p class="lead">{summary}</p>
  </header>
  <section class="group">
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
</style>
