<script>
  // No model number reaches this card before the answer (§6.1's anchoring rule; the server
  // allow-lists the payload), and nothing is cached between cards.
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { metaLine } from '$lib/rate.svelte.js';

  let {
    card,
    reveal = null,
    holding = false,
    busy = false,
    pending = null,
    showModel = false,
    onVerdict,
    onNotSeen,
    onSkip,
    onContinue
  } = $props();

  const title = $derived(card?.title ?? {});
  // P(seen) rides under the server-gated `model`; show it only while the viewer's switch is on too.
  const pSeen = $derived(showModel ? (card?.model?.p_seen ?? null) : null);
  const labels = $derived(
    card?.verdict_labels ?? [
      [0, 'disliked'],
      [1, 'fine'],
      [2, 'liked']
    ]
  );
</script>

<article class="sweep" data-testid="rate-sweep-card" data-card-token={card?.token}>
  <div class="poster-slot">
    <RatePoster {title} showName={false} />
  </div>

  <div class="body">
    <h2 data-testid="rate-card-title">{title.name ?? '—'}</h2>
    <div class="data-lg" data-testid="rate-card-meta">{metaLine(title)}</div>

    <p class="why" data-testid="rate-queue-reason">{card?.reason ?? ''}</p>
    {#if pSeen != null}
      <p class="data model" data-testid="rate-queue-p-seen">P(seen) {pSeen.toFixed(2)}</p>
    {/if}

    {#if title.recall_aid}
      <p class="recall" data-testid="rate-recall-aid">{title.recall_aid}</p>
    {/if}
  </div>

  <div class="strip">
    {#if holding && reveal}
      <button
        class="reveal"
        data-testid="rate-reveal"
        data-reveal-available={reveal.available ? 'true' : 'false'}
        data-reveal-agreed={reveal.agreed ? 'true' : 'false'}
        onclick={onContinue}
      >
        <span class="eyebrow">PREDICTION</span>
        <span class="reveal-text">{reveal.text}</span>
        <span class="data next">next card →</span>
      </button>
    {:else}
      <div class="verdicts" role="group" aria-label="Your verdict">
        {#each labels as [value, label] (value)}
          <button
            class="verdict v{value}"
            class:picked={pending === `verdict-${value}`}
            data-testid="rate-verdict-{value}"
            data-verdict-label={label}
            aria-busy={pending === `verdict-${value}`}
            disabled={busy}
            onclick={() => onVerdict(value)}
          >{label}</button>
        {/each}
      </div>
      <div class="secondary">
        <!-- One seen-state control: a title you cannot remember is plain `unseen` (§4.2). -->
        <button
          class="text"
          class:picked={pending === 'not_seen'}
          data-testid="rate-not-seen"
          aria-busy={pending === 'not_seen'}
          disabled={busy}
          onclick={onNotSeen}
        >
          not seen
        </button>
        <!-- Skip writes no observation; it only suppresses the redraw for this sitting. -->
        <button
          class="text"
          class:picked={pending === 'skip'}
          data-testid="rate-skip"
          aria-busy={pending === 'skip'}
          disabled={busy}
          onclick={onSkip}
        >
          skip
        </button>
      </div>
    {/if}
  </div>
</article>

<style>
  .sweep {
    display: grid;
    grid-template-columns: 210px minmax(0, 1fr);
    grid-template-rows: 1fr auto;
    gap: 14px 20px;
    padding: 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    animation: fadeIn 0.15s ease;
  }
  .body {
    display: flex;
    flex-direction: column;
    min-width: 0;
    grid-column: 2;
    grid-row: 1;
  }
  .poster-slot {
    grid-column: 1;
    grid-row: 1 / span 2;
  }
  h2 {
    margin: 0;
    font-size: 26px;
    font-weight: 700;
    line-height: 1.12;
    text-wrap: pretty;
  }
  [data-testid='rate-card-meta'] {
    margin-top: 6px;
  }
  .why {
    margin: 10px 0 0;
  }
  .recall {
    margin: 12px 0 0;
    font-size: 13.5px;
    line-height: 1.5;
    color: var(--ink-3);
    max-width: 56ch;
    text-wrap: pretty;
  }
  .strip {
    grid-column: 2;
    grid-row: 2;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .verdicts {
    display: flex;
    gap: 8px;
  }
  .verdict {
    flex: 1;
    /* A flex item's `min-width: auto` would keep "disliked" from shrinking: three widths. */
    min-width: 0;
    min-height: var(--touch);
    padding: 16px 10px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line-2);
    background: var(--card-raised);
    color: var(--ink-2);
    font-family: var(--mono);
    font-size: 15px;
    cursor: pointer;
    transition: border-color 0.12s ease, background 0.12s ease, color 0.12s ease;
  }
  .verdict:hover:not(:disabled),
  .verdict:focus-visible {
    border-color: var(--ember);
    color: var(--ink);
  }
  .verdict:active:not(:disabled) {
    background: var(--ember-wash);
  }
  .verdict:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .verdict.picked:disabled {
    opacity: 1;
    border-color: var(--ember);
    background: var(--ember-wash);
    color: var(--ink);
  }
  .text.picked:disabled {
    color: var(--ink-2);
  }
  .model {
    margin: 4px 0 0;
  }
  .secondary {
    display: flex;
    gap: 18px;
  }
  .text {
    background: none;
    border: none;
    padding: 6px 0;
    color: var(--ink-4);
    font-family: var(--mono);
    font-size: 11.5px;
    text-decoration: underline dotted;
    text-underline-offset: 4px;
    cursor: pointer;
  }
  .text:hover:not(:disabled) {
    color: var(--ink-2);
  }
  .reveal {
    display: flex;
    flex-direction: column;
    gap: 5px;
    width: 100%;
    min-height: 76px;
    padding: 14px 15px;
    text-align: left;
    border-radius: var(--r-sm);
    border: 1px solid var(--ember-edge);
    background: var(--ember-wash);
    cursor: pointer;
    animation: fadeIn 0.2s ease;
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    color: var(--ink-4);
  }
  .reveal-text {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink);
  }
  .next {
    color: var(--ink-5);
  }

  /* Compact: the poster moves beside the text so the verdict strip stays above the fold. */
  @media (max-width: 720px) {
    .sweep {
      grid-template-columns: 108px minmax(0, 1fr);
      gap: 12px;
      padding: 12px;
    }
    .poster-slot {
      grid-row: 1;
    }
    h2 {
      font-size: 19px;
    }
    .recall {
      font-size: 12.5px;
      margin-top: 8px;
    }
    .strip {
      grid-column: 1 / -1;
      grid-row: 2;
    }
    .verdicts {
      gap: 6px;
    }
    .verdict {
      padding: 18px 6px;
      border-radius: 2px;
      font-size: 14px;
    }
  }

  /* design.css's coarse floor sets height only, and `skip` sits 18px from `not seen`, which writes. */
  @media (pointer: coarse) {
    .text {
      min-width: var(--touch);
    }
  }
</style>
