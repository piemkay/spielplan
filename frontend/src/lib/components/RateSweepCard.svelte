<script>
  // No model number reaches this card before the answer (§6.1's anchoring rule; the server
  // allow-lists the payload), and nothing is cached between cards.
  import AnswerTiles from '$lib/components/AnswerTiles.svelte';
  import Icon from '$lib/components/Icon.svelte';
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

  // The wire's verdict labels name the tiles; Not seen sits under the poster, as on a pair (decision 529).
  const answers = $derived(
    (card?.verdict_labels ?? []).map(([value, label]) => ({
      answer: label,
      value,
      label: sentenceCase(label),
      testid: `rate-verdict-${value}`
    }))
  );
  const inFlight = $derived(answers.find((a) => pending === `verdict-${a.value}`)?.answer ?? null);

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
  <div class="ask">
    <h2 class="question">How was it?</h2>
    <p class="sub reason">
      <span data-testid="rate-queue-reason">{reason}</span><span class="tail"
        >{'\u00a0· '}<button
          class="hit why-link"
          data-testid="rate-why"
          aria-haspopup="dialog"
          onclick={onWhy}>Why these?</button
        ></span
      >
    </p>
  </div>

  <div class="film">
    <button
      class="art"
      data-testid="rate-sweep-poster"
      aria-label="About {title.name ?? 'this title'}"
      aria-haspopup="dialog"
      onclick={onPeek}
    ><RatePoster {title} showName={false} /></button>
    <div class="under">
      <h3 class="name" data-testid="rate-card-title">{title.name ?? '—'}</h3>
      <p class="data" data-testid="rate-card-meta">{metaLine(title)}</p>
      <button
        class="hit unseen"
        data-testid="rate-not-seen"
        aria-label="Not seen: {title.name ?? 'this title'}"
        aria-busy={pending === 'not_seen'}
        disabled={busy}
        onclick={onNotSeen}
      ><span class="face"><Icon name="eye-off" size={16} />Not seen</span></button>
    </div>
    {#if title.recall_aid || pSeen != null}
      <div class="recall">
        {#if pSeen != null}
          <p class="data" data-testid="rate-queue-p-seen">P(seen) {pSeen.toFixed(2)}</p>
        {/if}
        {#if title.recall_aid}
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
        {/if}
      </div>
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
        onAnswer={(a) => onVerdict(a.value)}
      />
    {/if}
  </div>
</article>

<style>
  /* One frame with a pair (decision 529): the question, the film with Not seen under it, and the
     answers below at the same place. On a phone the poster takes what height is left. */
  .sweep {
    --gap: 12px;
    --col: var(--rate-col, min((100cqw - var(--gap)) / 2, 220px));
    container-type: inline-size;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
    animation: fadeIn 0.15s var(--ease);
  }
  p {
    margin: 0;
  }
  .ask {
    flex: none;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
  .question {
    margin: 0;
    font-family: var(--serif);
    font-weight: 400;
    font-size: 24px;
    line-height: 28px;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  /* One line: a long reason gives way, "Why these?" never does. */
  .reason {
    max-width: 100%;
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
  .film {
    flex: 0 1 auto;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, calc(var(--col) * 2 + var(--gap)));
    grid-template-rows: minmax(0, calc(var(--col) * 1.5)) auto auto;
    grid-template-areas: 'art' 'under' 'recall';
    justify-content: center;
  }
  .art {
    grid-area: art;
    justify-self: center;
    height: 100%;
    aspect-ratio: 2 / 3;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
    -webkit-tap-highlight-color: transparent;
  }
  .under {
    grid-area: under;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding-top: 6px;
    text-align: center;
  }
  .name,
  .under .data {
    max-width: 100%;
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .name {
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .under .data {
    line-height: 18px;
  }
  .unseen {
    height: 44px;
    min-height: 44px;
    margin: 2px 0 -6px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text-2);
  }
  .face {
    height: 32px;
    padding: 0 12px;
    display: flex;
    align-items: center;
    gap: 6px;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    white-space: nowrap;
  }
  .unseen:disabled {
    opacity: 0.45;
  }
  .recall {
    grid-area: recall;
    position: relative;
    margin-top: 8px;
    text-align: center;
  }
  .recall p.footnote {
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
  /* As tall as a pair's five steps, so both rows start at the same height. */
  .answers {
    flex: none;
    min-height: 74px;
    margin-top: auto;
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

  /* Left-aligned like every other page; the recall aid takes the second poster's column. */
  @media (min-width: 721px) {
    .sweep {
      --gap: var(--rate-gap, 32px);
      flex: none;
      gap: 20px;
    }
    .ask {
      align-items: flex-start;
      text-align: left;
    }
    .question {
      font-size: var(--fs-title);
      line-height: 34px;
    }
    .film {
      grid-template-columns: var(--col) var(--col);
      grid-template-rows: calc(var(--col) * 1.5) auto;
      grid-template-areas: 'art recall' 'under .';
      column-gap: var(--gap);
      justify-content: start;
    }
    .art {
      justify-self: start;
    }
    .under {
      align-items: flex-start;
      text-align: left;
    }
    .recall {
      margin-top: 0;
      text-align: left;
    }
    .recall p.footnote {
      -webkit-line-clamp: 10;
      line-clamp: 10;
      font-size: var(--fs-subhead);
      line-height: 20px;
      color: var(--text-2);
    }
    .answers {
      margin-top: 0;
    }
  }
</style>
