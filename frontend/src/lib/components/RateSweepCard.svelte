<script>
  // No model number reaches this card before the answer (§6.1's anchoring rule; the server
  // allow-lists the payload), and nothing is cached between cards.
  import AnswerTiles from '$lib/components/AnswerTiles.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { metaLine, sentenceCase } from '$lib/rate.svelte.js';

  let {
    card,
    reveal = null,
    holding = false,
    busy = false,
    pending = null,
    showModel = false,
    onVerdict,
    onNotSeen,
    onContinue,
    onPeek,
    onWhy
  } = $props();

  const title = $derived(card?.title ?? {});
  // One line with "Why these?" after it, so the sentence's own full stop goes.
  const reason = $derived(String(card?.reason ?? '').replace(/\.$/, ''));
  // P(seen) rides under the server-gated `model`; show it only while the viewer's switch is on too.
  const pSeen = $derived(showModel ? (card?.model?.p_seen ?? null) : null);

  // The wire's verdict labels name the tiles; Not seen is the fourth (§4.2's one seen-state control).
  const answers = $derived([
    ...(card?.verdict_labels ?? []).map(([value, label]) => ({
      answer: label,
      value,
      label: sentenceCase(label),
      testid: `rate-verdict-${value}`
    })),
    { answer: 'not_seen', label: 'Not seen', testid: 'rate-not-seen' }
  ]);
  const inFlight = $derived(
    pending === 'not_seen'
      ? 'not_seen'
      : (answers.find((a) => pending === `verdict-${a.value}`)?.answer ?? null)
  );

  // The recall aid shows two lines until "more"; a new card starts clamped again.
  let recall = $state(null);
  let expanded = $state(false);
  let clamped = $state(false);
  $effect(() => {
    void card?.token;
    expanded = false;
  });
  $effect(() => {
    void title.recall_aid;
    clamped = !!recall && !expanded && recall.scrollHeight > recall.clientHeight + 1;
  });
</script>

<article class="sweep" data-testid="rate-sweep-card" data-card-token={card?.token}>
  <div class="hero">
    <div class="slot">
      <button
        class="art"
        data-testid="rate-sweep-poster"
        aria-label="About {title.name ?? 'this title'}"
        aria-haspopup="dialog"
        onclick={onPeek}
      ><RatePoster {title} showName={false} /></button>
    </div>

    <div class="text">
      <h2 class="name" data-testid="rate-card-title">{title.name ?? '—'}</h2>
      <p class="data" data-testid="rate-card-meta">{metaLine(title)}</p>
      <p class="why reason">
        <span data-testid="rate-queue-reason">{reason}</span><span class="tail"
          >{' · '}<button
            class="hit why-link"
            data-testid="rate-why"
            aria-haspopup="dialog"
            onclick={onWhy}>Why these?</button
          ></span
        >
      </p>
      {#if pSeen != null}
        <p class="data" data-testid="rate-queue-p-seen">P(seen) {pSeen.toFixed(2)}</p>
      {/if}
      {#if title.recall_aid}
        <div class="recall">
          <p class="footnote" class:open={expanded} bind:this={recall} data-testid="rate-recall-aid">
            {title.recall_aid}
          </p>
          {#if clamped}
            <span class="more footnote">…&nbsp;<button
                class="hit why-link"
                aria-expanded="false"
                onclick={() => (expanded = true)}>more</button
              ></span>
          {/if}
        </div>
      {/if}
    </div>
  </div>

  <div class="answers">
    {#if holding && reveal}
      <button
        class="reveal"
        data-testid="rate-reveal"
        data-reveal-available={reveal.available ? 'true' : 'false'}
        data-reveal-agreed={reveal.agreed ? 'true' : 'false'}
        onclick={onContinue}
      >
        <span class="reveal-text">{reveal.text}</span>
        <span class="next">
          Next
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />
          </svg>
        </span>
      </button>
    {:else}
      <AnswerTiles
        {answers}
        label="How was it?"
        pending={inFlight}
        disabled={busy}
        onAnswer={(a) => (a.answer === 'not_seen' ? onNotSeen() : onVerdict(a.value))}
      />
    {/if}
  </div>
</article>

<style>
  /* The poster takes what height is left, down to a thumbnail on a short screen (decision 528). */
  .sweep {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
    animation: fadeIn 0.15s var(--ease);
  }
  .hero {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    gap: 12px;
    text-align: center;
  }
  .slot {
    flex: 0 1 300px;
    min-height: 72px;
    max-width: 100%;
    display: flex;
    justify-content: center;
  }
  .art {
    height: 100%;
    aspect-ratio: 2 / 3;
    max-width: 100%;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
    -webkit-tap-highlight-color: transparent;
  }
  .text {
    flex: none;
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .name {
    margin: 0;
    font-family: var(--serif);
    font-weight: 400;
    font-size: 24px;
    line-height: 28px;
    text-wrap: balance;
  }
  p {
    margin: 0;
  }
  .text > .data {
    line-height: 18px;
  }
  /* One line: a long reason gives way, "Why these?" never does. */
  .reason {
    max-width: 100%;
    margin-top: 6px;
    display: flex;
    white-space: nowrap;
  }
  .reason > span:first-child {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tail {
    flex: none;
  }
  .why-link {
    padding: 0;
    border: none;
    background: none;
    color: var(--accent-text);
    font: inherit;
  }
  .recall {
    position: relative;
    width: 100%;
    max-width: 56ch;
    margin-top: 4px;
  }
  .recall p {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    text-wrap: pretty;
  }
  .recall p.open {
    display: block;
  }
  .more {
    position: absolute;
    right: 0;
    bottom: 0;
    padding-left: 20px;
    background: linear-gradient(90deg, transparent 0, var(--bg) 16px);
  }
  .answers {
    flex: none;
  }
  .reveal {
    width: 100%;
    height: 64px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 16px;
    border: none;
    border-radius: var(--r-md);
    background: var(--surface-1);
    color: var(--text);
    text-align: left;
    animation: fadeIn 0.2s var(--ease);
  }
  .reveal-text {
    flex: 1;
    display: block;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .reveal-text::first-letter {
    text-transform: uppercase;
  }
  .next {
    display: flex;
    align-items: center;
    gap: 2px;
    font-size: var(--fs-subhead);
    color: var(--text-3);
  }

  @media (min-width: 981px) {
    .sweep {
      gap: 32px;
    }
    .slot {
      flex-basis: 420px;
    }
    .name {
      font-size: var(--fs-display);
      line-height: 48px;
    }
    .reveal {
      height: 100px;
    }
  }
</style>
