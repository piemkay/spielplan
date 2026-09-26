<script>
  // One envelope from `GET /api/rate` and the writes that answer it: the counter and Undo arrive
  // together, no belief is sent before the tap, and every write returns the next card. `?head=` is
  // repeated, one per pinned title, hence `getAll`.
  import { onDestroy, onMount } from 'svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { page } from '$app/stores';

  import RateBattleCard from '$lib/components/RateBattleCard.svelte';
  import RateBlockCounter from '$lib/components/RateBlockCounter.svelte';
  import RateClassBalance from '$lib/components/RateClassBalance.svelte';
  import RateModelLog from '$lib/components/RateModelLog.svelte';
  import RateRail from '$lib/components/RateRail.svelte';
  import RateSweepCard from '$lib/components/RateSweepCard.svelte';
  import RateUndo from '$lib/components/RateUndo.svelte';
  import {
    FIND_MIN_CHARS,
    KIND_LABELS,
    MODES,
    clearFinder,
    commit,
    correct,
    duel,
    findTitles,
    finder,
    load,
    modeName,
    notSeen,
    rate,
    rateTitle,
    reset,
    revealLine,
    setDecisive,
    setHead,
    setKinds,
    setMode,
    skip,
    undo,
    verdict
  } from '$lib/rate.svelte.js';
  import { session } from '$lib/session.svelte.js';

  const KIND_TABS = [
    ['movie', 'Films'],
    ['series', 'Series']
  ];

  const kinds = $derived(rate.session?.kinds ?? []);
  const mode = $derived(rate.session?.mode ?? 'mix');
  // While a reveal holds, the counter belongs to the card being shown.
  const block = $derived(rate.frozenBlock ?? rate.session?.block ?? null);
  const reveal = $derived(revealLine(rate.reveal));
  const showModel = $derived(!!session.user?.show_model);
  const modeWhy = $derived(MODES.find(([key]) => key === mode)?.[2] ?? '');

  // Read at init: `onMount` runs ahead of the effect below, so a deep link's head would be lost.
  let lastSearch = $page.url.search;
  setHead($page.url.searchParams.getAll('head'));

  onMount(load);
  onDestroy(reset);

  // With show_model off the server sends no `log` or `ledger`, so a flip re-reads once the chip's
  // write has landed (the epoch). `lastModelEpoch` is not `$state`, or this effect would loop.
  let lastModelEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastModelEpoch) return;
    lastModelEpoch = epoch;
    load();
  });

  // A client-side navigation to /rate?head=… does not remount, so pick it up reactively.
  $effect(() => {
    const search = $page.url.search;
    const ids = $page.url.searchParams.getAll('head');
    if (search === lastSearch) return;
    lastSearch = search;
    setHead(ids);
    if (rate.booted) load({ quiet: true });
  });

  // Debounced; `findTitles` drops any answer a newer keystroke has overtaken.
  let finding = $state(false);
  let findTimer = null;
  let findInput = $state(null);

  function toggleFind() {
    finding = !finding;
    if (!finding) clearFinder();
  }

  $effect(() => {
    if (finding && findInput) findInput.focus();
  });

  function onFindInput(event) {
    const value = event.currentTarget.value;
    finder.q = value;
    clearTimeout(findTimer);
    findTimer = setTimeout(() => findTitles(value), 250);
  }

  async function pick(item) {
    if (await rateTitle(item)) finding = false;
  }

  onDestroy(() => clearTimeout(findTimer));

  function toggleKind(kind) {
    // An empty selection is a 422, so the last active kind does not turn off.
    const on = kinds.includes(kind);
    if (on && kinds.length === 1) return;
    setKinds(on ? kinds.filter((k) => k !== kind) : [...kinds, kind]);
  }
</script>

<div class="rate" data-testid="rate-surface">
  <header>
    <div class="titles">
      <h1>Rate</h1>
      <RateBlockCounter {block} {kinds} {mode} />
    </div>

    <div class="controls">
      <!-- Mix is where every entry point lands; a mode sticks only once the person changes it. -->
      <div class="group modes" role="group" aria-label="Mode">
        {#each MODES as [key, name] (key)}
          <button
            class="pill"
            data-testid="rate-mode-{key}"
            aria-pressed={mode === key}
            onclick={() => setMode(key)}
          >{name}</button>
        {/each}
      </div>

      <button
        class="pill find"
        data-testid="rate-find-toggle"
        aria-label="Find a title you know to rate"
        aria-expanded={finding}
        aria-controls="rate-find"
        onclick={toggleFind}
      >
        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor"
          stroke-width="1.6" stroke-linecap="round" aria-hidden="true">
          <circle cx="8.5" cy="8.5" r="5.5" />
          <path d="m13 13 4.5 4.5" />
        </svg>
        <span class="find-word">find</span>
      </button>

      <div class="group" role="group" aria-label="Kind">
        {#each KIND_TABS as [key, label] (key)}
          <button
            class="pill"
            data-testid="rate-kind-{key}"
            aria-pressed={kinds.includes(key)}
            onclick={() => toggleKind(key)}
          >{label}</button>
        {/each}
      </div>

      <RateUndo undo={rate.undo} busy={rate.busy} onUndo={undo} />
    </div>
  </header>

  {#if finding}
    <section class="find-panel card" id="rate-find" data-testid="rate-find">
      <input
        bind:this={findInput}
        class="find-input"
        type="search"
        data-testid="rate-find-input"
        placeholder="Rate a title you know..."
        aria-label="Find a title you know to rate"
        autocomplete="off"
        value={finder.q}
        oninput={onFindInput}
      />
      {#if finder.error}
        <p class="why" role="alert" data-testid="rate-find-error">{finder.error}</p>
      {:else if finder.items.length}
        <ul class="hits" data-testid="rate-find-results">
          {#each finder.items as item (item.id)}
            <li>
              <button
                class="hit"
                data-testid="rate-find-hit"
                data-title-id={item.id}
                disabled={!!item.rated || rate.busy}
                onclick={() => pick(item)}
              >
                <span class="hit-name">{item.name}</span>
                <span class="data">
                  {[item.year, KIND_LABELS[item.kind] ?? item.kind].filter(Boolean).join(' · ')}
                </span>
                {#if item.rated}
                  <span class="data rated" data-testid="rate-find-rated">
                    you rated it {item.rated}
                  </span>
                {/if}
              </button>
            </li>
          {/each}
        </ul>
      {:else if finder.searched && !finder.busy}
        <p class="why" data-testid="rate-find-none">Nothing matches "{finder.searched}".</p>
      {:else if finder.q.trim().length < FIND_MIN_CHARS}
        <p class="why" data-testid="rate-find-hint">
          Type a title you have seen, then tap it to rate it.
        </p>
      {/if}
    </section>
  {/if}

  <!-- One visible line, not a title= tooltip: a phone cannot read tooltips. -->
  <p class="why mode-why" data-testid="rate-mode-note">{modeName(mode)}: {modeWhy}</p>

  {#if rate.notice}
    <p class="banner notice why" role="status" data-testid="rate-notice">{rate.notice}</p>
  {/if}
  {#if rate.error}
    <p class="banner error why" role="alert" data-testid="rate-error">{rate.error}</p>
  {/if}

  <div class="columns">
    <div class="stage">
      {#if rate.loading}
        <p class="data" data-testid="rate-loading">opening a rating session…</p>
      {:else if rate.card?.type === 'sweep'}
        <RateSweepCard
          card={rate.card}
          {reveal}
          holding={rate.holding}
          busy={rate.busy}
          pending={rate.pending}
          {showModel}
          onVerdict={verdict}
          onNotSeen={notSeen}
          onSkip={skip}
          onContinue={commit}
        />
      {:else if rate.card?.type === 'battle'}
        <RateBattleCard
          card={rate.card}
          decisive={!!rate.session?.decisive}
          busy={rate.busy}
          pending={rate.pending}
          onDuel={duel}
          onCorrect={correct}
          onSkip={skip}
          onDecisive={setDecisive}
        />
      {:else if rate.drained}
        <!-- Keyed to `drained.cause`: an empty battle pool means too few ratings, not too many. -->
        <div class="drained card" data-testid="rate-drained">
          {#if rate.drained.cause === 'pool'}
            <h2>No pair to compare yet</h2>
            <p class="why">{rate.drained.text}</p>
            <button
              class="btn-ghost"
              data-testid="rate-drained-cta"
              disabled={rate.busy}
              onclick={() => setMode('sweep')}
            >Switch to Singles</button>
          {:else}
            <h2>Nothing left to queue</h2>
            <p class="why">{rate.drained.text}</p>
            <p class="why">
              "Sharpen my ranking" on the Rank page fine-tunes your tiers from here.
            </p>
            <a class="btn-ghost" data-testid="rate-drained-cta" href="/rank">Go to Rank</a>
          {/if}
        </div>
      {/if}
    </div>

    <div class="side">
      <RateClassBalance balance={rate.balance} {kinds} />
      <RateRail balance={rate.balance} {mode} {kinds} {showModel} />
      {#if showModel}
        <RateModelLog log={rate.log} ledger={rate.ledger} />
      {/if}
    </div>
  </div>
</div>

<style>
  .rate {
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 1180px;
  }
  header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .titles {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 240px;
    flex: 1;
  }
  h1 {
    margin: 0;
    font-size: 21px;
    font-weight: 600;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .group {
    display: flex;
    gap: 6px;
  }
  .find {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .mode-why {
    margin: 0;
  }
  .find-panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: var(--card-pad-tight);
  }
  .find-panel .why {
    margin: 0;
  }
  .find-input {
    width: 100%;
    min-height: var(--touch);
    padding: 0 12px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line-2);
    background: var(--card-raised);
    color: var(--ink);
    font: inherit;
  }
  .hits {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .hit {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
    width: 100%;
    min-height: var(--touch);
    padding: 8px 12px;
    text-align: left;
    border-radius: var(--r-sm);
    border: 1px solid var(--line);
    background: transparent;
    color: var(--ink-2);
    cursor: pointer;
  }
  .hit:hover:not(:disabled),
  .hit:focus-visible {
    border-color: var(--line-2);
    color: var(--ink);
  }
  .hit:disabled {
    cursor: default;
    opacity: 0.6;
  }
  .hit-name {
    font-weight: 600;
  }
  .rated {
    flex-basis: 100%;
  }
  .banner {
    margin: 0;
    padding: 10px 13px;
    border-radius: var(--r-sm);
    border: 1px solid var(--line-2);
    background: var(--card);
  }
  .banner.error {
    border-color: var(--ember-edge);
    background: var(--ember-wash);
    color: var(--ember-lift);
  }
  .columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 304px;
    gap: 18px;
    align-items: start;
  }
  .stage {
    min-width: 0;
  }
  .side {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .drained {
    padding: var(--card-pad-roomy);
    display: flex;
    flex-direction: column;
    gap: 10px;
    align-items: flex-start;
  }
  .drained h2 {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
  }
  .drained .why {
    max-width: 52ch;
    margin: 0;
  }

  /* Nothing in the rail is dropped: the side column moves under the card. */
  @media (max-width: 980px) {
    .columns {
      grid-template-columns: minmax(0, 1fr);
    }
    header {
      align-items: flex-start;
    }
  }

  /* Compact: the controls wrap rather than scroll sideways, so Undo is never off-screen. */
  @media (max-width: 720px) {
    .rate {
      gap: 10px;
    }
    h1 {
      font-size: 17px;
    }
    /* Under the card rather than above it, where it cost the battle card its toggle row. */
    .mode-why {
      order: 5;
    }
    .controls {
      gap: 8px;
      width: 100%;
    }
    .group {
      flex: none;
    }
    .group.modes {
      flex: 1 1 250px;
    }
    .group.modes .pill {
      flex: 1;
      padding-left: 6px;
      padding-right: 6px;
    }
    .find {
      justify-content: center;
      min-width: var(--touch);
      padding-left: 0;
      padding-right: 0;
    }
    .find-word {
      display: none;
    }
  }
</style>
