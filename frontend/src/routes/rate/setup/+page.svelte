<script>
  // The ladder's set-up (decision 547): a full-screen flow over the tab bar, one step per tier from the
  // best down, each named by its word and never by a letter, then the ladder it made.
  import { onDestroy, onMount } from 'svelte';
  import { afterNavigate, goto } from '$app/navigation';
  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { displayNames } from '$lib/titleCard.js';
  import {
    PAGE,
    cells,
    films,
    finish,
    hit,
    historyLine,
    more,
    next,
    picked,
    readyLine,
    reset,
    search,
    setup,
    start,
    stepOf,
    toggle,
    total,
    undo
  } from '$lib/ladderSetup.svelte.js';

  onMount(start);
  onDestroy(reset);

  // Opened from Home or Rate, so leaving goes back there rather than stacking another page.
  let fromApp = false;
  afterNavigate(({ from }) => (fromApp = !!from));

  const step = $derived(setup.steps[setup.at]);
  const draft = $derived(setup.drafts[setup.at]);
  const last = $derived(setup.at === setup.steps.length - 1);
  const grid = $derived(cells());
  const count = $derived(total());
  // The strip: the films put on the step directly above.
  const above = $derived(setup.at > 0 ? setup.drafts[setup.at - 1].picks : []);
  const done = $derived(setup.result);

  let q = $state('');
  let timer;
  let leaving = $state(false);
  /** @type {HTMLElement | undefined} */
  let list = $state();

  $effect(() => {
    if (setup.status === 'set') goto('/rate', { replaceState: true });
  });

  // A new step opens at its top.
  $effect(() => {
    void setup.at;
    list?.scrollTo?.(0, 0);
  });

  const name = (film) => displayNames(film).primary;

  function onQuery() {
    clearTimeout(timer);
    timer = setTimeout(() => search(q), 200);
  }

  function clearSearch() {
    clearTimeout(timer);
    q = '';
    search('');
  }

  function choose(film) {
    clearTimeout(timer);
    q = '';
    hit(film);
    list?.scrollTo?.(0, 0);
  }

  function go(move) {
    clearTimeout(timer);
    q = '';
    move();
  }

  function leave() {
    if (fromApp) history.back();
    else goto('/rate');
  }

  function hitLabel(film) {
    const on = stepOf(film.id);
    const where = on < 0 ? '' : `, on your ladder as ${setup.steps[on].word}`;
    return `${name(film)}${film.year ? `, ${film.year}` : ''}${where}`;
  }
</script>

{#snippet check(px)}
  <svg width={px} height={px} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
{/snippet}

{#snippet poster(film, on, mark)}
  <span class="art" class:on>
    <RatePoster title={film} showName={false} lazy />
    {#if film.seen}<span class="watched" data-testid="setup-watched">{@render check(14)}</span>{/if}
    {#if mark}<span class="badge">{@render check(16)}</span>{/if}
  </span>
{/snippet}

<section class="flow" data-testid="setup-flow"><div class="screen">
  {#if setup.status === 'done' && done}
    <div class="top">
      <span></span>
      <h1 class="bar-title">Your ladder</h1>
      <a class="btn-plain end strong" href="/">Done</a>
    </div>
    <div class="scroll" data-testid="setup-done">
      <h2 class="large-title ready">Your ladder is ready</h2>
      <p class="lede">{readyLine(done.placed)}</p>
      <!-- role: WebKit drops a list's role once its markers are gone. -->
      <ol class="rungs" role="list" aria-label="Your ladder">
        {#each done.tiers as tier (tier.tier)}
          <li class="rung" class:empty={!tier.count} data-tier={tier.tier}>
            <span class="rung-word">{tier.word}</span>
            {#if tier.count}
              <span class="rung-count">{films(tier.count)}</span>
              {#if tier.first}
                <span class="rung-art"><RatePoster title={tier.first} showName={false} /></span>
              {/if}
            {/if}
          </li>
        {/each}
      </ol>
      {#if historyLine(done.earlier_ratings, done.rated_before)}
        <p class="footnote history">{historyLine(done.earlier_ratings, done.rated_before)}</p>
      {/if}
    </div>
    <div class="dock">
      <a class="btn-primary wide" href="/rate">Start rating</a>
    </div>
  {:else}
    <div class="top">
      <button class="btn-plain" aria-haspopup="dialog" data-testid="setup-leave" onclick={() => (count ? (leaving = true) : leave())}>
        Leave
      </button>
      <p class="counter">{#if step}Step {setup.at + 1} of {setup.steps.length}{/if}</p>
      <button
        class="btn-plain end"
        disabled={setup.at === 0}
        aria-label={setup.at > 0 ? `Undo, back to ${setup.steps[setup.at - 1].word}` : 'Undo'}
        data-testid="setup-undo"
        onclick={() => go(undo)}
      >Undo</button>
    </div>

    <div class="scroll" bind:this={list}>
      {#if step && draft}
        <h1 class="title-1 word" data-testid="setup-step" data-tier={step.tier}>{step.word}</h1>
        <p class="hint">{step.hint}</p>

        {#if above.length}
          <div class="strip" data-testid="setup-strip">
            <p class="strip-label">Not quite these · {setup.steps[setup.at - 1].word}</p>
            <div class="refs" aria-hidden="true">
              {#each (above.length > 6 ? above.slice(0, 5) : above) as film (film.id)}
                <span class="ref"><RatePoster title={film} showName={false} /></span>
              {/each}
              {#if above.length > 6}<span class="ref rest">+{above.length - 5}</span>{/if}
            </div>
          </div>
        {/if}

        <div class="find">
          <label class="search">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" /></svg>
            <input
              type="search"
              autocomplete="off"
              enterkeyhint="search"
              aria-label="Find a film"
              placeholder="A film you have seen"
              data-testid="setup-search"
              bind:value={q}
              oninput={onQuery}
            />
          </label>
          {#if q}<button class="btn-plain" onclick={clearSearch}>Cancel</button>{/if}
        </div>

        {#if q.trim()}
          {#if setup.hits?.length}
            <div class="grid" role="group" aria-label="Films that match">
              {#each setup.hits as film (film.id)}
                <button class="cell" data-testid="setup-hit" aria-label={hitLabel(film)} aria-describedby={film.seen ? 'setup-watched' : undefined} onclick={() => choose(film)}>
                  {@render poster(film, picked(film), stepOf(film.id) >= 0)}
                  <span class="lines">
                    <span class="name">{name(film)}</span>
                    {#if film.year}<span class="name year">{film.year}</span>{/if}
                  </span>
                </button>
              {/each}
            </div>
          {:else if setup.hits}
            <p class="footnote note">
              {q.trim().length < 2 ? 'Type at least two letters' : `Nothing matches “${q.trim()}”`}
            </p>
          {/if}
        {:else}
          <div class="grid" role="group" aria-label="Films for {step.word}">
            {#each grid as film (film.id)}
              {@const on = picked(film)}
              <button
                class="cell"
                data-testid="setup-film"
                data-title-id={film.id}
                aria-pressed={on}
                aria-label={name(film)}
                aria-describedby={film.seen ? 'setup-watched' : undefined}
                onclick={() => toggle(film)}
              >
                {@render poster(film, on, on)}
                <span class="name">{name(film)}</span>
              </button>
            {/each}
            {#if setup.loading && !draft.films.length}
              {#each { length: PAGE }, i (i)}<span class="skeleton cell-skeleton"></span>{/each}
            {/if}
          </div>
          {#if draft.more}
            <button class="btn-secondary wide more" data-testid="setup-more" disabled={setup.loading} onclick={more}>
              Show {PAGE} more
            </button>
          {:else if draft.films.length && !setup.loading}
            <p class="footnote note" data-testid="setup-end">
              That's the end of the list. Search finds any other film.
            </p>
          {/if}
        {/if}
      {/if}
      {#if setup.error}<p class="footnote note" role="alert">{setup.error}</p>{/if}
    </div>

    {#if step}
      <div class="dock">
        {#if !last}
          <button class="btn-primary wide" data-testid="setup-next" onclick={() => go(next)}>
            {draft?.picks.length ? 'Next' : `None for ${step.word}`}
          </button>
        {:else}
          <button
            class="btn-primary wide"
            data-testid="setup-finish"
            disabled={!count || setup.busy}
            aria-describedby={count ? undefined : 'setup-finish-why'}
            onclick={finish}
          >Finish</button>
          {#if !count}
            <p class="footnote why-not" id="setup-finish-why">
              Put at least one film on your ladder to finish.
            </p>
          {/if}
        {/if}
      </div>
    {/if}
  {/if}
  <span id="setup-watched" hidden>Watched</span>
</div></section>

<ActionSheet
  open={leaving}
  title="Leave the set-up? Your picks so far aren't kept."
  options={[{ label: 'Leave the set-up', destructive: true, onSelect: leave }]}
  onClose={() => (leaving = false)}
/>

<style>
  /* Over the tab bar and the top row, as Place with questions is. */
  .flow {
    position: fixed;
    inset: 0;
    z-index: 55;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) 0 env(safe-area-inset-left);
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
  h2,
  ol {
    margin: 0;
  }
  .top {
    flex: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    min-height: 44px;
    padding: 0 8px;
  }
  .top > :first-child {
    justify-self: start;
  }
  .end {
    justify-self: end;
  }
  .strong {
    font-weight: 600;
  }
  .btn-plain:disabled {
    opacity: 0.35;
  }
  .counter {
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .bar-title {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    white-space: nowrap;
  }
  .scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 0 var(--gutter) 24px;
  }
  .word {
    margin-top: 8px;
  }
  .hint {
    margin-top: 4px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-2);
  }
  .strip-label {
    margin-top: 12px;
    font-size: var(--fs-footnote);
    line-height: 18px;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--text-3);
  }
  .refs {
    margin-top: 6px;
    display: flex;
    gap: 8px;
  }
  .ref {
    flex: none;
    width: 48px;
  }
  .rest {
    height: 72px;
    display: grid;
    place-items: center;
    border-radius: var(--r-poster);
    background: var(--surface-3);
    color: var(--text-2);
    font-size: var(--fs-footnote);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .find {
    margin-top: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .search {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding-left: 12px;
    border-radius: var(--r-sm);
    background: var(--surface-2);
    color: var(--text-3);
  }
  /* Outranks design.css's field rule: the label draws the field, the input only holds the text. */
  .search > input[type='search'][aria-label] {
    flex: 1;
    min-width: 0;
    padding: 0 12px 0 0;
    background: none;
    border-radius: 0;
    outline: none;
  }
  .search:focus-within {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }
  .grid {
    margin-top: 12px;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px 8px;
  }
  .cell {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 0;
    border: none;
    background: none;
    color: var(--text-2);
    text-align: left;
  }
  .art {
    position: relative;
    display: block;
    border-radius: var(--r-poster);
    transition: box-shadow var(--dur-quick) var(--ease);
  }
  .art.on {
    box-shadow: 0 0 0 2px var(--text);
  }
  .watched,
  .badge {
    position: absolute;
    top: 6px;
    width: 22px;
    height: 22px;
    display: grid;
    place-items: center;
  }
  .watched {
    left: 6px;
    border-radius: var(--r-pill);
    background: rgba(12, 11, 10, 0.72);
    color: var(--text);
  }
  .badge {
    right: 6px;
    border-radius: var(--r-xs);
    background: var(--text);
    color: var(--bg);
    animation: pop var(--dur-base) var(--ease-spring);
  }
  .name {
    font-size: var(--fs-caption);
    line-height: 16px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .lines {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .year {
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .cell-skeleton {
    aspect-ratio: 2 / 3;
  }
  .wide {
    width: 100%;
  }
  .more {
    margin-top: 16px;
    min-height: 48px;
  }
  .note {
    margin-top: 16px;
    text-align: center;
  }
  .dock {
    flex: none;
    padding: 12px var(--gutter) calc(12px + env(safe-area-inset-bottom));
    background: var(--bar);
    -webkit-backdrop-filter: blur(24px) saturate(1.5);
    backdrop-filter: blur(24px) saturate(1.5);
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .dock .btn-primary {
    min-height: 50px;
  }
  .why-not {
    margin-top: 8px;
    text-align: center;
  }
  .ready {
    margin-top: 16px;
  }
  .lede {
    margin-top: 8px;
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
  }
  .rungs {
    margin-top: 20px;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .rung {
    min-height: 44px;
    padding: 0 8px;
    border-radius: var(--r-sm);
    background: var(--surface-1);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .rung-word {
    flex: 1;
    min-width: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rung.empty .rung-word {
    color: var(--text-2);
  }
  .rung-count {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .rung-art {
    flex: none;
    width: 26px;
  }
  .rung-art :global(.poster) {
    border-radius: var(--r-xs);
  }
  .history {
    margin-top: 16px;
  }
</style>
