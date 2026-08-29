<script>
  /**
   * An honest placeholder for a surface the build order has not reached yet (spec §12).
   * It names the milestone and what the surface will do, in the spec's own words, so the app
   * never shows a dead link and never pretends a feature exists.
   */
  let { surface, milestone, summary, points = [] } = $props();
</script>

<div class="wrap">
  <div class="data tag">{milestone}</div>
  <h1>{surface}</h1>
  <p class="lead">{summary}</p>
  {#if points.length}
    <ul>
      {#each points as p}<li class="why">{p}</li>{/each}
    </ul>
  {/if}
  <p class="why foot">Not built yet — this surface arrives with {milestone}.</p>
</div>

<style>
  .wrap { max-width: 60ch; display: flex; flex-direction: column; gap: 12px; }
  .tag { letter-spacing: .14em; }
  h1 { margin: 0; font-size: 21px; font-weight: 600; }
  .lead { margin: 0; font-size: 13.5px; line-height: 1.6; color: var(--ink-2); }
  ul { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; }
  .foot { padding-top: 6px; border-top: 1px solid var(--line); }
</style>
