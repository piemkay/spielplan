<script>
  // The warning copy and thresholds are the server's (`rate/balance.py`), rendered verbatim: the
  // copy is a measured claim. On phones only the counts fold; the warning never does.
  import { ratingsLabel, sharePct } from '$lib/rate.svelte.js';

  // Counts cover the session's kinds, so the total names the kind when only one is selected.
  let { balance, kinds = [] } = $props();

  let open = $state(false);

  const labels = $derived(balance?.labels ?? ['disliked', 'fine', 'liked']);
  const counts = $derived(balance?.counts ?? [0, 0, 0]);
  const shares = $derived(balance?.shares ?? [0, 0, 0]);
  const total = $derived(balance?.total ?? 0);
  const armsAt = $derived(balance?.arms_at ?? 0);
  // Worst to best, left to right, matching the stored ordinal.
  const TONE = ['low', 'mid', 'high'];
</script>

<section class="balance" data-testid="rate-balance" data-warn={balance?.warn ? 'true' : 'false'}>
  <button
    class="head"
    data-testid="rate-balance-toggle"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >
    <span class="eyebrow">YOUR SPREAD</span>
    <span class="data" data-testid="rate-balance-total">{ratingsLabel(total, kinds)}</span>
  </button>

  <div class="bar" role="img" aria-label={labels.map((l, i) => `${l} ${counts[i]}`).join(', ')}>
    {#each labels as label, i (label)}
      <span
        class="seg {TONE[i]}"
        data-testid="rate-balance-segment-{label}"
        data-share={sharePct(shares[i])}
        style:flex="{Math.max(shares[i] ?? 0, total ? 0.02 : 1 / 3)} 1 0"
      ></span>
    {/each}
  </div>

  <ul class="counts" class:open>
    {#each labels as label, i (label)}
      <li data-testid="rate-balance-count-{label}">
        <span class="dot {TONE[i]}"></span>
        <span class="label">{label}</span>
        <span class="data">{counts[i]} · {sharePct(shares[i])}%</span>
      </li>
    {/each}
  </ul>

  {#if balance?.warn && balance?.copy}
    <p class="warn why" role="status" data-testid="rate-balance-warning">{balance.copy}</p>
  {:else if armsAt && total < armsAt}
    <p class="why arming" data-testid="rate-balance-arming">
      A balance check starts at {armsAt} ratings.
    </p>
  {/if}
</section>

<style>
  .balance {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 13px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
  }
  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    text-align: left;
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    color: var(--ink-4);
  }
  .bar {
    display: flex;
    gap: 2px;
    height: 8px;
  }
  .seg {
    border-radius: 2px;
    min-width: 2px;
  }
  .low {
    background: rgba(236, 233, 228, 0.22);
  }
  .mid {
    background: rgba(236, 233, 228, 0.42);
  }
  /* The loud end is a neutral, not the accent: the distribution is never a selection (§6.8). */
  .high {
    background: var(--status);
  }
  .counts {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .counts li {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
  }
  .label {
    flex: 1;
    color: var(--ink-2);
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: var(--r-pill);
    flex: none;
  }
  /* A frame only: the prose register is `.why`'s, from design.css. */
  .arming {
    margin: 2px 0 0;
  }
  .warn {
    margin: 2px 0 0;
    padding: 9px 10px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-sm);
  }

  @media (max-width: 720px) {
    .counts {
      display: none;
    }
    .counts.open {
      display: flex;
    }
  }
  @media (min-width: 721px) {
    .head {
      cursor: default;
    }
  }
</style>
