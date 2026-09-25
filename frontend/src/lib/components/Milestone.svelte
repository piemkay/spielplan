<script>
  /**
   * An honest placeholder for a surface the build order has not reached yet (spec §12).
   * It says what the surface will do, so the app never pretends a feature exists.
   *
   * It no longer names the milestone that owes the surface. A milestone label is the build's
   * vocabulary, and the member register never renders one (decision 486 clause 2): two members
   * read "Not built yet - this surface arrives with M6" over predicate syntax in the 2026-09-25
   * user test. Nothing in navigation leads here any more (decision 488); the route answers by
   * URL, and `05-milestones.spec.js` still fails by design on the day the surface ships.
   */
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
