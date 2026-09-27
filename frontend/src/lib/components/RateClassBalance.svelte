<script>
  // Counts cover the session's kinds, so the total names the kind when only one is selected. The
  // warning copy is the server's and the page shows it; `compact` is the header's small bar.
  import { armingLine, ratingsLabel, sentenceCase, sharePct } from '$lib/rate.svelte.js';

  let { balance, kinds = [], compact = false } = $props();

  const labels = $derived(balance?.labels ?? ['disliked', 'fine', 'liked']);
  const counts = $derived(balance?.counts ?? [0, 0, 0]);
  const shares = $derived(balance?.shares ?? [0, 0, 0]);
  const total = $derived(balance?.total ?? 0);
  const arming = $derived(armingLine(balance));
  const summary = $derived(`Your mix: ${labels.map((l, i) => `${counts[i]} ${l}`).join(', ')}`);
  // Worst to best, left to right, matching the stored ordinal; the list reads best first.
  const TONE = ['low', 'mid', 'high'];
  const bestFirst = $derived(labels.map((label, i) => ({ label, i })).reverse());
</script>

{#snippet bar()}
  {#each labels as label, i (label)}
    <span
      class="seg {TONE[i]}"
      data-testid={compact ? undefined : `rate-balance-segment-${label}`}
      data-share={sharePct(shares[i])}
      style:flex="{Math.max(shares[i] ?? 0, total ? 0.02 : 1 / 3)} 1 0"
    ></span>
  {/each}
{/snippet}

{#if compact}
  <div class="mini" role="img" aria-label={summary} data-testid="rate-mix">
    <span class="footnote">Your mix</span>
    <span class="bar thin">{@render bar()}</span>
  </div>
{:else}
  <section class="card mix" data-testid="rate-balance" data-warn={balance?.warn ? 'true' : 'false'}>
    <h3>Your mix</h3>
    <div class="bar" role="img" aria-label={summary}>{@render bar()}</div>
    <ul class="counts">
      {#each bestFirst as { label, i } (label)}
        <li data-testid="rate-balance-count-{label}">
          <span class="dot {TONE[i]}" aria-hidden="true"></span>
          <span class="label">{sentenceCase(label)}</span>
          <span class="n">{counts[i]}</span>
        </li>
      {/each}
    </ul>
    <p class="footnote">
      <span data-testid="rate-balance-total">{ratingsLabel(total, kinds)}</span> so far
    </p>
    {#if arming}<p class="footnote" data-testid="rate-balance-arming">{arming}</p>{/if}
  </section>
{/if}

<style>
  .mini {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .mix {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .bar {
    display: flex;
    gap: 2px;
    height: 8px;
    border-radius: var(--r-pill);
    overflow: hidden;
  }
  .bar.thin {
    width: 64px;
    height: 6px;
  }
  .seg {
    min-width: 2px;
  }
  .low {
    background: rgba(245, 240, 232, 0.28);
  }
  .mid {
    background: var(--text-3);
  }
  /* The loud end is the text colour, not the accent: the mix is never a selection. */
  .high {
    background: var(--text);
  }
  .counts {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-variant-numeric: tabular-nums;
  }
  .counts li {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .label {
    flex: 1;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: var(--r-pill);
    flex: none;
  }
  .footnote {
    margin: 0;
    font-variant-numeric: tabular-nums;
  }
</style>
