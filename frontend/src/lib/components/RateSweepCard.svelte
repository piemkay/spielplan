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
    onWhy
  } = $props();

  const title = $derived(card?.title ?? {});
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
</script>

<article class="sweep" data-testid="rate-sweep-card" data-card-token={card?.token}>
  <div class="hero">
    <div class="poster-slot">
      <RatePoster {title} showName={false} />
    </div>

    <div class="heading">
      <h2 class="title-1" data-testid="rate-card-title">{title.name ?? '—'}</h2>
      <p class="meta" data-testid="rate-card-meta">{metaLine(title)}</p>
    </div>

    <p class="why reason">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 3.5 13.8 10l6.7 2-6.7 2L12 20.5 10.2 14 3.5 12l6.7-2z" />
      </svg>
      <span>
        <span data-testid="rate-queue-reason">{card?.reason ?? ''}</span>
        <button class="hit why-link" data-testid="rate-why" onclick={onWhy}>Why these?</button>
      </span>
    </p>
    {#if pSeen != null}
      <p class="data" data-testid="rate-queue-p-seen">P(seen) {pSeen.toFixed(2)}</p>
    {/if}

    {#if title.recall_aid}
      <p class="footnote recall" data-testid="rate-recall-aid">{title.recall_aid}</p>
    {/if}
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
  .sweep {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 24px;
    animation: fadeIn 0.15s var(--ease);
  }
  .hero {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    text-align: center;
  }
  .poster-slot {
    height: min(300px, 34dvh);
    aspect-ratio: 2 / 3;
    margin-bottom: 4px;
  }
  .heading {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  h2 {
    text-wrap: balance;
  }
  p {
    margin: 0;
  }
  .meta {
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-variant-numeric: tabular-nums;
    color: var(--text-2);
  }
  .reason {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    text-wrap: pretty;
  }
  .reason svg {
    flex: none;
    margin-top: 2px;
  }
  .why-link {
    padding: 0;
    border: none;
    background: none;
    color: var(--accent-text);
    font: inherit;
  }
  .recall {
    max-width: 56ch;
    text-wrap: pretty;
  }
  .answers {
    margin-top: auto;
    /* Pinned above the tab bar when the card runs longer than the screen. */
    position: sticky;
    bottom: calc(var(--tabbar) + env(safe-area-inset-bottom) + 8px);
    padding-top: 8px;
    background: var(--bg);
  }
  .reveal {
    width: 100%;
    height: 76px;
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

  /* No tab bar from here on: the sidebar takes its place. */
  @media (min-width: 721px) {
    .answers {
      bottom: 16px;
    }
  }

  @media (min-width: 981px) {
    .sweep {
      gap: 32px;
    }
    .poster-slot {
      height: min(420px, 46dvh);
    }
    h2 {
      font-size: var(--fs-display);
      line-height: 48px;
    }
    .answers {
      position: static;
      width: 100%;
      max-width: 660px;
      margin-inline: auto;
    }
    .reveal {
      height: 100px;
    }
  }
</style>
