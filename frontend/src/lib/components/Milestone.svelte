<script>
  /** Placeholder for an unbuilt surface (spec §12); it never names a milestone (decision 486). */
  let { surface, summary, points = [] } = $props();
</script>

<div class="wrap" data-testid="placeholder">
  <h1>{surface}</h1>
  <p class="lead">{summary}</p>
  {#if points.length}
    <ul>
      {#each points as p}<li class="why">{p}</li>{/each}
    </ul>
  {/if}
  <p class="why foot">Not built yet — this is coming in a later update.</p>
</div>

<style>
  .wrap { max-width: 60ch; display: flex; flex-direction: column; gap: 12px; }
  h1 { margin: 0; font-size: 21px; font-weight: 600; }
  .lead { margin: 0; font-size: 13.5px; line-height: 1.6; color: var(--ink-2); }
  ul { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; }
  .foot { padding-top: 6px; border-top: 1px solid var(--line); }
</style>
