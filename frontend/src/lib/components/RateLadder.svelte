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
  let held = $state(-1);
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
          class:placing={lit != null}
          class:aside={held >= 0}
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
      bind:held
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
    transition: opacity var(--dur-quick) var(--ease), transform var(--dur-quick) var(--ease);
  }
  /* It steps back with the other shelves while one places or is held. */
  .unseen.placing,
  .unseen.aside {
    opacity: 0.4;
  }
  .unseen.placing {
    transform: scale(0.97);
    transition-delay: 90ms;
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
  /* `--spare`: what a one-line name leaves under the last shelf (the screen less the top row, 8px, the
     126px head, 16px, seven 72px shelves and the tab bar). A second line costs 21px, so the name takes
     it where the shelves still clear the tab bar, or where the page scrolls anyway (§6.1). */
  @media (max-width: 720px) {
    .name {
      --spare: calc(
        100dvh - 44px - env(safe-area-inset-top) - var(--tabbar) - env(safe-area-inset-bottom) - 654px
      );
      max-height: calc(
        34px + clamp(0px, (var(--spare) - 20px) * 34, 34px) + clamp(0px, var(--spare) * -34, 34px)
      );
    }
  }
  /* The film in its own column beside the shelves, both sized by the Rate page. */
  @media (min-width: 721px) {
    .ladder {
      display: grid;
      grid-template-columns: var(--rate-grid);
      align-items: start;
      gap: 32px;
    }
    .film {
      flex-direction: column;
      gap: 16px;
    }
    .art {
      width: 100%;
      height: auto;
    }
    .head {
      align-self: stretch;
      min-height: 0;
    }
    .name {
      margin: 0;
      -webkit-line-clamp: 3;
      line-clamp: 3;
    }
    .reason {
      color: var(--text-2);
      white-space: normal;
    }
    .unseen {
      margin: 8px 0 0;
    }
  }
</style>
