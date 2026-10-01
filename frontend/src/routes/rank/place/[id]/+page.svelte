<script>
  // Place with questions (decision 528): a full-screen flow over the tab bar. The pair and its
  // five-step answer are Rate's; only the neighbour carries Not seen, since the title being placed
  // has been seen.
  import { onDestroy, onMount } from 'svelte';
  import { afterNavigate } from '$app/navigation';
  import { page } from '$app/state';
  import RateBattleCard from '$lib/components/RateBattleCard.svelte';
  import RatePeek from '$lib/components/RatePeek.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { answer, narrowing, notSeen, place, reset, resultLine, start } from '$lib/place.svelte.js';

  let { params } = $props();

  onMount(() => {
    start(Number(params.id), page.url.searchParams.get('kind') === 'series' ? 'series' : 'movie');
  });
  onDestroy(reset);

  // Rank opened this flow, so leaving goes back to it rather than stacking a second Rank on top.
  let fromApp = false;
  afterNavigate(({ from }) => (fromApp = !!from));
  function leave(event) {
    if (!fromApp) return;
    event.preventDefault();
    history.back();
  }

  const placed = $derived(
    place.pair?.left ?? place.done?.around.find((t) => t.id === place.done.title_id) ?? null
  );
  const card = $derived(place.pair && { ...place.pair, corrections: { sides: ['right'] } });
  const bar = $derived(place.pair && narrowing(place.pair));

  /** @type {{ side: 'left' | 'right', token: string } | null} the pair a look was opened on */
  let peek = $state(null);
</script>

<section class="flow" data-testid="rank-place"><div class="screen">
  {#if place.done}
    {@const done = place.done}
    <div class="result">
      <h1 class="title-1" data-testid="rank-place-done">{placed?.name ?? ''} sits in {done.tier}</h1>
      <p class="sub">{resultLine(done)}</p>
      <ol class="spot" aria-label="{placed?.name ?? ''}'s new spot in {done.tier}">
        {#each done.around as t (t.id)}
          {@const here = t.id === done.title_id}
          <li class:here aria-current={here || undefined}>
            <div class="art"><RatePoster title={t} showName="missing" /></div>
            <span class="sr-only">{t.name}{t.year ? `, ${t.year}` : ''}</span>
            {#if here}<span class="chip">New spot</span>{/if}
          </li>
        {/each}
      </ol>
    </div>
    <div class="actions">
      <a class="btn-primary" href="/rank" onclick={leave} data-testid="rank-place-finish">Done</a>
      <a class="btn-plain" href="/rank" onclick={leave}>Place another</a>
    </div>
  {:else}
    <header class="top">
      <a class="btn-plain cancel" href="/rank" onclick={leave} data-testid="rank-place-cancel">Cancel</a>
      <h1>Place {placed?.name ?? place.name}</h1>
    </header>
    {#if card}
      <section class="narrow" aria-label="Where {placed.name} can still go">
        <div class="track" aria-hidden="true">
          <span style:left="{bar.from * 100}%" style:width="{bar.span * 100}%"></span>
        </div>
        <p class="where" data-testid="rank-place-where">{bar.where}</p>
        <p class="footnote">{bar.count}</p>
      </section>
      <RateBattleCard
        {card}
        busy={place.busy}
        pending={place.pending}
        onDuel={answer}
        onCorrect={notSeen}
        onPeek={(side) => (peek = { side, token: card.token })}
      />
    {:else if !place.error}
      <p class="footnote wait">Loading…</p>
    {/if}
    {#if place.error}<p class="footnote error" role="alert">{place.error}</p>{/if}
  {/if}
</div></section>

{#if peek && card}
  {@const { side, token } = peek}
  <RatePeek
    title={card[side]}
    busy={place.busy}
    onNotSeen={side === 'right' ? () => place.pair?.token === token && notSeen() : undefined}
    onClose={() => (peek = null)}
  />
{/if}

<style>
  /* Over the tab bar and the top row, as a Tonight room is. */
  .flow {
    position: fixed;
    inset: 0;
    z-index: 55;
    padding: env(safe-area-inset-top) max(var(--gutter), env(safe-area-inset-right))
      calc(16px + env(safe-area-inset-bottom)) max(var(--gutter), env(safe-area-inset-left));
    background: var(--bg);
  }
  .screen {
    height: 100%;
    max-width: 560px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
  }
  p,
  h1,
  ol {
    margin: 0;
  }
  .top {
    flex: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    min-height: 44px;
  }
  .cancel {
    justify-self: start;
    margin-left: -8px;
  }
  .top h1 {
    grid-column: 2;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .narrow {
    flex: none;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    padding-top: 24px;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
  .track {
    position: relative;
    align-self: stretch;
    height: 6px;
    margin-bottom: 8px;
    border-radius: var(--r-pill);
    background: var(--thumb);
    overflow: hidden;
  }
  .track span {
    position: absolute;
    inset-block: 0;
    min-width: 6px;
    border-radius: var(--r-pill);
    background: var(--text);
    transition: left 0.2s var(--ease), width 0.2s var(--ease);
  }
  .where {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .wait,
  .error {
    padding-top: 24px;
    text-align: center;
  }
  .result {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
  }
  .sub {
    margin-top: 4px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
  }
  .spot {
    margin-top: 24px;
    padding: 0;
    list-style: none;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 85px));
    gap: 36px 6px;
  }
  .spot li {
    position: relative;
  }
  .art {
    border-radius: var(--r-poster);
  }
  .here .art {
    box-shadow: 0 0 0 2px var(--accent);
  }
  .chip {
    position: absolute;
    top: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    height: 24px;
    padding: 0 10px;
    display: flex;
    align-items: center;
    border-radius: var(--r-pill);
    background: var(--accent-tint);
    color: var(--accent-text);
    font-size: var(--fs-caption);
    line-height: 16px;
    font-weight: 600;
    white-space: nowrap;
  }
  .actions {
    flex: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .actions .btn-primary {
    min-height: 50px;
    border-radius: var(--r-md);
  }
  .actions .btn-plain {
    justify-content: center;
  }
  @media (min-width: 721px) {
    .narrow {
      padding-bottom: 20px;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .track span {
      transition: none;
    }
  }
</style>
