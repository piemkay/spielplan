<script>
  // §6.1's card: the film on top, the person's shelves under it. Nothing the model guesses reaches
  // it before the tap; P(seen) rides under the server-gated `model`, beside the reason.
  import { metaLine } from '$lib/rate.svelte.js';
  import { displayNames } from '$lib/titleCard.js';
  import Icon from './Icon.svelte';
  import RatePoster from './RatePoster.svelte';
  import RateShelves from './RateShelves.svelte';

  // `echo` names the film just placed in this card's meta line; `back` deals the card in from the
  // left, as Undo brought it back.
  let {
    card,
    echo = null,
    back = false,
    busy = false,
    pending = null,
    showModel = false,
    onPlace,
    onNotSeen,
    onPeek
  } = $props();

  const title = $derived({ ...card.title, kind: card.kind });
  const name = $derived(displayNames(title).primary);
  const lit = $derived(pending?.startsWith('place-') ? Number(pending.slice(6)) : null);
  // Built in JS: Svelte trims the whitespace that opens an element, gluing the separator.
  const pSeen = $derived(
    showModel && card.model?.p_seen != null ? ` · P(seen) ${card.model.p_seen.toFixed(2)}` : ''
  );
  const guess = $derived(
    showModel && echo?.model
      ? ` · guess ${echo.model.guess_word} · cdf ${Number(echo.model.cdf).toFixed(2)}`
      : ''
  );
</script>

<article class="ladder" data-testid="rate-card" data-card-token={card.token}>
  {#key card.token}
    <div class="film" style:--enter-x={back ? '-14px' : null}>
      <button
        class="art"
        class:greyed={pending === 'not_seen'}
        aria-label="About {name}"
        aria-haspopup="dialog"
        onclick={onPeek}
      ><RatePoster {title} showName={false} /></button>
      <div class="head">
        <h2 class="name" data-testid="rate-card-title">{name}</h2>
        {#if echo}
          <p class="line echo" data-testid="rate-echo">
            <Icon name="check" size={14} /><span class="clip"
              >{echo.name} · {echo.word}{#if guess}<span class="data">{guess}</span>{/if}</span
            >
          </p>
        {:else}
          <p class="line meta" data-testid="rate-card-meta">{metaLine(title)}</p>
        {/if}
        <p class="line reason" data-testid="rate-reason">
          <span class="clip">{card.reason}{#if pSeen}<span class="data">{pSeen}</span>{/if}</span>
        </p>
        <button
          class="hit unseen"
          data-testid="rate-not-seen"
          aria-label="Not seen: {name}"
          aria-busy={pending === 'not_seen'}
          onclick={onNotSeen}
        ><span class="face"><Icon name="eye-off" size={16} />Not seen</span></button>
      </div>
    </div>
    <RateShelves
      shelves={card.shelves}
      {name}
      film={card.title}
      kind={card.kind}
      {lit}
      {busy}
      {onPlace}
    />
  {/key}
</article>

<style>
  .ladder {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  p {
    margin: 0;
  }
  .film {
    --enter-x: 14px;
    --enter-s: 0.985;
    display: flex;
    gap: 16px;
    align-items: flex-start;
    animation: enter 260ms var(--ease) backwards;
  }
  .art {
    flex: none;
    width: 84px;
    height: 126px;
    padding: 0;
    border: none;
    border-radius: var(--r-poster);
    background: none;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.55), 0 2px 6px rgba(0, 0, 0, 0.4);
    -webkit-tap-highlight-color: transparent;
    transition: filter 160ms var(--ease);
  }
  .art.greyed {
    filter: grayscale(0.7) brightness(0.75);
  }
  .head {
    flex: 1;
    min-width: 0;
    min-height: 126px;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
  }
  .name {
    align-self: stretch;
    margin: -3px 0 0;
    font-family: var(--serif);
    font-weight: 400;
    font-size: var(--fs-title);
    line-height: 34px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .line {
    align-self: stretch;
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 4px;
    min-height: 18px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .clip {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .echo {
    color: var(--text-2);
    animation: fadeIn var(--dur-quick) var(--ease) both;
  }
  .echo > :global(svg) {
    flex: none;
  }
  .unseen {
    flex: none;
    margin-top: auto;
    margin-bottom: -6px;
    height: 44px;
    min-height: 44px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text-2);
  }
  .face {
    height: 32px;
    padding: 0 14px 0 12px;
    display: flex;
    align-items: center;
    gap: 6px;
    border-radius: var(--r-pill);
    background: var(--surface-2);
    font-size: var(--fs-footnote);
    line-height: 18px;
    font-weight: 600;
    white-space: nowrap;
    transition: background-color var(--dur-quick) var(--ease);
  }
  .unseen[aria-busy='true'] .face {
    background: var(--accent);
    color: var(--on-accent);
    animation: pop 180ms var(--ease);
  }
  /* A screen shorter than the phone the board was drawn on keeps the name and reason one line each. */
  @media (max-width: 720px) and (max-height: 860px) {
    .name {
      -webkit-line-clamp: 1;
      line-clamp: 1;
    }
  }
</style>
