<script>
  /**
   * §6.1's class-balance widget — "Running class-balance widget with its warning copy … the
   * measured 5× lever" — and §5.2's number behind it: "a 60%-'liked' labeller gives up ~0.07 ρ".
   *
   * Two rules govern this component.
   *
   *   1. **The copy is the server's, verbatim.** `class_balance.copy` is a measured claim, and
   *      "about five times more" is §5.2's lever written down. Paraphrasing it — or rebuilding
   *      the sentence here from `heaviest` and a template — changes a measurement into a
   *      slogan, so this renders the string it was given and never composes one.
   *   2. **The threshold is the server's too.** The widget shows `threshold` rather than a
   *      hard-coded 60%, so if the number is ever re-measured there is exactly one place it
   *      lives (`rate.balance.WARN_SHARE`) and this surface follows it.
   *
   * Proposal 43 ("phone-first means the rail is not optional") asks for the widget to collapse
   * to a three-segment bar on phones. It does — but only the per-class counts fold away. The
   * warning itself stays visible at every width: it is the single largest lever a labeller has,
   * and hiding it behind a tap on the form factor most of the labelling happens on would drop
   * exactly the thing §5.2 asked the UI to say.
   */
  import { sharePct } from '$lib/rate.svelte.js';

  let { balance } = $props();

  let open = $state(false);

  const labels = $derived(balance?.labels ?? ['disliked', 'fine', 'liked']);
  const counts = $derived(balance?.counts ?? [0, 0, 0]);
  const shares = $derived(balance?.shares ?? [0, 0, 0]);
  const total = $derived(balance?.total ?? 0);
  const threshold = $derived(sharePct(balance?.threshold ?? 0.6));
  // Presentation only: worst → best, left to right, matching the stored ordinal (proposal 52).
  const TONE = ['low', 'mid', 'high'];
</script>

<section class="balance" data-testid="rate-balance" data-warn={balance?.warn ? 'true' : 'false'}>
  <button
    class="head"
    data-testid="rate-balance-toggle"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >
    <span class="eyebrow">CLASS BALANCE</span>
    <span class="data" data-testid="rate-balance-total">{total} labels</span>
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
    <!-- Rendered exactly as sent. The sentence is a measurement, not a message. -->
    <p class="warn why" role="status" data-testid="rate-balance-warning">{balance.copy}</p>
    <p class="data" data-testid="rate-balance-threshold">
      warn &gt; {threshold}% of your running distribution
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
  /* §6.8 / proposal 130: uppercase mono eyebrow. */
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
  .high {
    background: var(--ember);
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
  .warn {
    margin: 2px 0 0;
    padding: 9px 10px;
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    border-radius: var(--r-sm);
    color: var(--ink-2);
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.55;
  }

  /* Proposal 43: on phones the widget is the bar; the counts fold behind the header tap.
     The warning is never folded. */
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
