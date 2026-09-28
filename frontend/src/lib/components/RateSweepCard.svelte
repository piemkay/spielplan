<script>
  // No model number reaches this card before the answer (§6.1's anchoring rule; the server
  // allow-lists the payload), and nothing is cached between cards.
  import AnswerTiles from '$lib/components/AnswerTiles.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { metaLine, sentenceCase } from '$lib/rate.svelte.js';

  // `echo`, a snippet, stands in for the reason line a moment after a verdict; `back` deals the card
  // in from the left, as Undo brought it back.
  let {
    card,
    echo = null,
    back = false,
    busy = false,
    pending = null,
    failed = null,
    showModel = false,
    onVerdict,
    onNotSeen,
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
  const answerOf = (key) => answers.find((a) => key === `verdict-${a.value}`)?.answer ?? null;
  const inFlight = $derived(answerOf(pending));
  const pose = $derived(inFlight ?? (pending === 'not_seen' || pending === 'skip' ? pending : null));

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
      {#if echo}{@render echo()}{:else}<span data-testid="rate-queue-reason">{reason}</span><span
          class="tail"
          >{'\u00a0· '}<button
            class="hit why-link"
            data-testid="rate-why"
            aria-haspopup="dialog"
            onclick={onWhy}>Why these?</button
          ></span
        >{/if}
    </p>
  </div>

  {#key card?.token}
    <div class="film" style:--enter-x={back ? '-14px' : null}>
      <button
        class="art"
        data-pose={pose}
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
  {/key}

  <div class="answers">
    <AnswerTiles
      {answers}
      label="How was it?"
      pending={inFlight}
      failed={answerOf(failed)}
      disabled={busy}
      onAnswer={(a) => onVerdict(a.value)}
    />
  </div>
</article>

<style>
  /* One frame with a pair (decision 529): the question, the film with Not seen under it, and the
     answers below at the same place. On a phone the poster takes what height is left, up to the
     desktop's 220 px column (decision 530). */
  .sweep {
    --gap: 12px;
    --col: var(--rate-col, min((100cqw - var(--gap)) / 2, 220px));
    container-type: inline-size;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
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
    --enter-x: 14px;
    --enter-s: 0.985;
    flex: 0 1 auto;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, calc(var(--col) * 2 + var(--gap)));
    grid-template-rows: minmax(0, 330px) auto auto;
    grid-template-areas: 'art' 'under' 'recall';
    justify-content: center;
    animation: enter 260ms var(--ease) backwards;
  }
  .art {
    position: relative;
    grid-area: art;
    justify-self: center;
    height: 100%;
    aspect-ratio: 2 / 3;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
    -webkit-tap-highlight-color: transparent;
    transition: transform 160ms var(--ease), filter 160ms var(--ease), opacity 160ms var(--ease);
  }
  /* Liked rises toward the light: a glow painted once, only its opacity moves. */
  .art::after {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    box-shadow: 0 14px 40px -10px rgba(245, 240, 232, 0.3);
    opacity: 0;
    transition: opacity 160ms var(--ease);
    pointer-events: none;
  }
  .art[data-pose='liked']::after {
    opacity: 1;
  }
  .art[data-pose='disliked'] {
    filter: brightness(0.8);
  }
  .art[data-pose='not_seen'] {
    filter: grayscale(0.7) brightness(0.75);
  }
  .art[data-pose='skip'] {
    opacity: 0.85;
  }
  @media (prefers-reduced-motion: no-preference) {
    .art[data-pose='liked'] {
      transform: translateY(-6px) scale(1.015);
    }
    .art[data-pose='fine'] {
      transform: scale(0.99);
    }
    .art[data-pose='disliked'] {
      transform: translateY(4px) scale(0.985);
    }
    .art[data-pose='skip'] {
      transform: translateX(-6px);
    }
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
    transition: opacity var(--dur-quick) var(--ease);
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
    transition: transform var(--dur-base) var(--ease-spring), background var(--dur-quick) var(--ease);
  }
  .unseen:active:not(:disabled) .face {
    transform: scale(var(--press));
    transition-duration: var(--dur-press);
  }
  .unseen[aria-busy='true'] .face {
    background: var(--text);
    color: var(--bg);
    animation: pop 180ms var(--ease);
  }
  .unseen:disabled:not([aria-busy='true']) {
    opacity: 0.45;
    transition-delay: var(--busy-delay);
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
