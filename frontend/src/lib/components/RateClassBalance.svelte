<script>
  // Counts cover the session's kinds, so the total names the kind when only one is selected. The
  // warning copy is the server's, verbatim; `compact` is the progress row's meter, which opens it.
  import { armingLine, ratingsLabel, sentenceCase, sharePct } from '$lib/rate.svelte.js';

  let { balance, kinds = [], compact = false, onOpen = undefined } = $props();

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

{#if compact}
  <button
    class="meter hit"
    data-testid="rate-mix"
    aria-label={summary}
    aria-haspopup="dialog"
    onclick={onOpen}
  >
    <span class="stack">
      {#each labels as label, i (label)}
        <span class={TONE[i]} style:flex="{Math.max(shares[i] ?? 0, total ? 0.02 : 1 / 3)} 1 0"></span>
      {/each}
    </span>
  </button>
{:else}
  <section class="mix" data-testid="rate-balance" data-warn={balance?.warn ? 'true' : 'false'}>
    <ul>
      {#each bestFirst as { label, i } (label)}
        <li data-testid="rate-balance-count-{label}">
          <span class="row"><span>{sentenceCase(label)}</span> <span class="n">{counts[i]}</span></span>
          <span class="bar" aria-hidden="true">
            <span class={TONE[i]} data-share={sharePct(shares[i])} style:width="{sharePct(shares[i])}%"
            ></span>
          </span>
        </li>
      {/each}
    </ul>
    {#if balance?.warn && balance?.copy}
      <p class="why" data-testid="rate-balance-warning">{balance.copy}</p>
    {/if}
    <p class="footnote">
      <span data-testid="rate-balance-total">{ratingsLabel(total, kinds)}</span> so far
    </p>
    {#if arming}<p class="footnote" data-testid="rate-balance-arming">{arming}</p>{/if}
  </section>
{/if}

<style>
  .meter {
    flex: none;
    width: 36px;
    min-height: 0;
    height: 30px;
    margin: -3px 0;
    padding: 0;
    border: none;
    background: none;
    display: grid;
    place-items: center;
  }
  .stack {
    width: 36px;
    height: 6px;
    display: flex;
    gap: 2px;
    border-radius: var(--r-pill);
    overflow: hidden;
  }
  .stack > span {
    transition: flex-grow var(--dur-slow) var(--ease);
  }
  .mix {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  ul {
    list-style: none;
    margin: 0 0 8px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  li {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .n {
    font-variant-numeric: tabular-nums;
    color: var(--text-2);
  }
  .bar {
    height: 12px;
    border-radius: var(--r-pill);
    overflow: hidden;
    background: var(--surface-2);
  }
  .bar > span {
    display: block;
    height: 100%;
    border-radius: var(--r-pill);
  }
  /* The loud end is the text colour, not the accent: the mix is never a selection. */
  .low {
    background: var(--text-3);
  }
  .mid {
    background: var(--text-2);
  }
  .high {
    background: var(--text);
  }
  p {
    margin: 0;
  }
  .footnote {
    font-variant-numeric: tabular-nums;
  }
</style>
