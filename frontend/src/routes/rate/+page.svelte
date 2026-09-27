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
  import Sheet from '$lib/components/Sheet.svelte';
  import {
    FIND_MIN_CHARS,
    KIND_LABELS,
    MODES,
    armingLine,
    clearFinder,
    commit,
    continueRating,
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
  import { topbar } from '$lib/topbar.svelte.js';

  const KIND_TABS = [
    ['movie', 'Films'],
    ['series', 'Series']
  ];

  const kinds = $derived(rate.session?.kinds ?? []);
  const mode = $derived(rate.session?.mode ?? 'mix');
  // A block's end screen waits for the reveal of its last answer (decision 527).
  const done = $derived(rate.holding ? null : rate.done);
  // While a reveal holds, the counter belongs to the card being shown.
  const block = $derived(rate.frozenBlock ?? rate.done ?? rate.session?.block ?? null);
  const reveal = $derived(revealLine(rate.reveal));
  const showModel = $derived(!!session.user?.show_model);
  const arming = $derived(armingLine(rate.balance));

  // The progress and the mix move to a side column once there is room for one beside the card.
  let width = $state(0);
  const wide = $derived(width >= 1280);

  let menuOpen = $state(false);
  let whyOpen = $state(false);

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

  function closeFind() {
    finding = false;
    clearFinder();
  }

  // After the sheet has moved focus to itself.
  $effect(() => {
    if (!finding || !findInput) return;
    const timer = setTimeout(() => findInput?.focus({ preventScroll: true }));
    return () => clearTimeout(timer);
  });

  function onFindInput(event) {
    const value = event.currentTarget.value;
    finder.q = value;
    clearTimeout(findTimer);
    findTimer = setTimeout(() => findTitles(value), 250);
  }

  // Closed either way: a pick that could not be served says so on the page behind the sheet.
  async function pick(item) {
    await rateTitle(item);
    finding = false;
  }

  onDestroy(() => clearTimeout(findTimer));

  function toggleKind(kind) {
    // An empty selection is a 422, so the last active kind does not turn off.
    const on = kinds.includes(kind);
    if (on && kinds.length === 1) return;
    setKinds(on ? kinds.filter((k) => k !== kind) : [...kinds, kind]);
  }

  // The undo, mode and skip row is the shell's top row here, beside You (decision 527).
  $effect(() => {
    if (!topbar.host) return;
    topbar.content = rateBar;
    return () => {
      if (topbar.content === rateBar) topbar.content = null;
    };
  });
</script>

<svelte:window bind:innerWidth={width} />

{#snippet progress()}
  <RateBlockCounter {block}>
    {#if !wide}<RateClassBalance balance={rate.balance} {kinds} compact />{/if}
  </RateBlockCounter>
{/snippet}

{#snippet balanceNote(small = false)}
  {#if rate.balance?.warn && rate.balance?.copy}
    <p class="note" class:small role="status">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="8.5" /><path d="M12 11v5M12 8v.01" />
      </svg>
      <span data-testid="rate-balance-warning">{rate.balance.copy}</span>
    </p>
  {/if}
{/snippet}

<!-- In the sheet's body, not its header: the header's drag area captures the pointer. -->
{#snippet sheetBar(title, close)}
  <div class="sheet-bar">
    <h2>{title}</h2>
    <button class="btn-plain" onclick={close}>Done</button>
  </div>
{/snippet}

{#snippet rateBar()}
  <div class="bar">
    <RateUndo undo={rate.undo} busy={rate.busy} onUndo={undo} />
    <h1 class="title">
      {#if done}
        Rate
      {:else}
        <!-- Mix is where every entry point lands; a mode sticks only once the person changes it. -->
        <button
          class="mode"
          data-testid="rate-menu"
          aria-haspopup="dialog"
          aria-expanded={menuOpen}
          onclick={() => (menuOpen = true)}
        >
          <span>{modeName(mode)}</span>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m5.5 9.5 6.5 6.5 6.5-6.5" />
          </svg>
        </button>
      {/if}
    </h1>
    <div class="end">
      {#if !done}
        <button
          class="btn-plain icon"
          data-testid="rate-find-toggle"
          aria-label="Find a title you know to rate"
          aria-haspopup="dialog"
          aria-expanded={finding}
          onclick={() => (finding = true)}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" />
          </svg>
        </button>
        <!-- Skip writes no observation; it only suppresses the redraw for this sitting. -->
        <button
          class="btn-plain skip"
          data-testid="rate-skip"
          aria-busy={rate.pending === 'skip'}
          disabled={!rate.card || rate.busy || rate.holding}
          onclick={skip}
        >Skip</button>
      {/if}
    </div>
  </div>
{/snippet}

<div class="rate" class:wide data-testid="rate-surface">
  <div class="main">
    {#if !topbar.host}{@render rateBar()}{/if}

    {#if !wide}
      <section class="progress" aria-label="Progress">
        {@render progress()}
        <!-- The compact mix has no room for it, and §6.1 says it while rating. -->
        {#if arming && !done}
          <p class="footnote arming" data-testid="rate-balance-arming">{arming}</p>
        {/if}
      </section>
    {/if}

    {#if !done}{@render balanceNote(true)}{/if}
    {#if rate.notice}
      <p class="note" role="status">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="8.5" /><path d="M12 11v5M12 8v.01" />
        </svg>
        <span data-testid="rate-notice">{rate.notice}</span>
      </p>
    {/if}
    {#if rate.error}
      <p class="note error" role="alert">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M12 4 2.8 19.5h18.4z" /><path d="M12 10v4.5M12 17.2v.01" />
        </svg>
        <span data-testid="rate-error">{rate.error}</span>
      </p>
    {/if}

    <div class="stage">
      {#if rate.loading}
        <p class="footnote" data-testid="rate-loading">Finding something to rate…</p>
      {:else if done}
        <section class="done" data-testid="rate-done">
          <div class="done-body">
            <div class="done-head">
              <span class="done-mark" aria-hidden="true">
                <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                  stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m5 12.5 4.5 4.5L19 7.5" />
                </svg>
              </span>
              <h2 class="large-title">That's {done.size ?? 15}.</h2>
              <p class="done-sub">Your suggestions just got sharper.</p>
            </div>
            <RateClassBalance balance={rate.balance} {kinds} />
            {@render balanceNote()}
          </div>
          <!-- The last answer stays undoable here until the next one lands (decision 199). -->
          <div class="done-actions">
            <button class="btn-primary" data-testid="rate-done-more" onclick={continueRating}>
              Rate {done.size ?? 15} more
            </button>
            <a class="btn-secondary" data-testid="rate-done-home" href="/">Back to Home</a>
          </div>
        </section>
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
          onContinue={commit}
          onWhy={() => (whyOpen = true)}
        />
      {:else if rate.card?.type === 'battle'}
        <RateBattleCard
          card={rate.card}
          decisive={!!rate.session?.decisive}
          busy={rate.busy}
          pending={rate.pending}
          onDuel={duel}
          onCorrect={correct}
          onDecisive={setDecisive}
          onWhy={() => (whyOpen = true)}
        />
      {:else if rate.drained}
        <!-- Keyed to `drained.cause`: an empty pair pool means too few ratings, not too many. -->
        <div class="drained" data-testid="rate-drained">
          {#if rate.drained.cause === 'pool'}
            <h2 class="section-title">No pair to compare yet</h2>
            <p class="why">{rate.drained.text}</p>
            <button
              class="btn-secondary"
              data-testid="rate-drained-cta"
              disabled={rate.busy}
              onclick={() => setMode('sweep')}
            >Switch to Singles</button>
          {:else}
            <h2 class="section-title">Nothing left to queue</h2>
            <p class="why">{rate.drained.text}</p>
            <p class="why">
              "Sharpen my ranking" on the Rank page fine-tunes your tiers from here.
            </p>
            <a class="btn-secondary" data-testid="rate-drained-cta" href="/rank">Go to Rank</a>
          {/if}
        </div>
      {/if}
    </div>

    {#if showModel && !wide}
      <RateModelLog log={rate.log} ledger={rate.ledger} />
    {/if}
  </div>

  {#if wide}
    <aside class="side" aria-label="Progress">
      <div class="card progress-card">
        <h2>Your progress</h2>
        {@render progress()}
      </div>
      {#if !done}<RateClassBalance balance={rate.balance} {kinds} />{/if}
      {#if showModel}<RateModelLog log={rate.log} ledger={rate.ledger} />{/if}
    </aside>
  {/if}
</div>

<Sheet open={menuOpen} onClose={() => (menuOpen = false)} label="How to rate" detent="medium" width={440}>
  {#snippet children(close)}
    <div class="sheet-top">{@render sheetBar('How to rate', close)}</div>
    <div class="menu" data-testid="rate-menu-sheet">
      <p class="list-header">Mode</p>
      <div class="list-group" role="group" aria-label="Mode">
        {#each MODES as [key, name, why] (key)}
          <button
            class="list-row mode-row"
            data-testid="rate-mode-{key}"
            aria-pressed={mode === key}
            onclick={() => mode !== key && setMode(key)}
          >
            <span class="row-text">
              <span>{name}</span>
              <span class="footnote">{why}</span>
            </span>
            {#if mode === key}
              <svg class="check" width="20" height="20" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" stroke-width="2" stroke-linecap="round"
                stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
            {/if}
          </button>
        {/each}
      </div>

      <p class="list-header include">Include</p>
      <div class="kinds" role="group" aria-label="Include">
        {#each KIND_TABS as [key, label] (key)}
          <button
            class="pill"
            data-testid="rate-kind-{key}"
            aria-pressed={kinds.includes(key)}
            onclick={() => toggleKind(key)}
          >{label}</button>
        {/each}
      </div>
      <p class="list-footer">At least one stays on.</p>
    </div>
  {/snippet}
</Sheet>

<Sheet open={whyOpen} onClose={() => (whyOpen = false)} label="Why these?" detent="large" width={520}>
  {#snippet children(close)}
    <div class="sheet-top">{@render sheetBar('Why these?', close)}</div>
    <div class="why-sheet">
      {#if !wide}<RateClassBalance balance={rate.balance} {kinds} />{/if}
      <RateRail balance={rate.balance} {mode} {kinds} {showModel} />
    </div>
  {/snippet}
</Sheet>

<Sheet open={finding} onClose={closeFind} label="Rate a title you know" detent="large" width={520}>
  {#snippet children(close)}
    <div class="sheet-top">
      {@render sheetBar('Rate a title you know', close)}
      <input
        bind:this={findInput}
        type="search"
        data-testid="rate-find-input"
        placeholder="A title you have seen"
        aria-label="Find a title you know to rate"
        autocomplete="off"
        value={finder.q}
        oninput={onFindInput}
      />
    </div>
    <div class="find" data-testid="rate-find">
      {#if finder.error}
        <p class="why" role="alert" data-testid="rate-find-error">{finder.error}</p>
      {:else if finder.items.length}
        <ul class="list-group hits" data-testid="rate-find-results">
          {#each finder.items as item (item.id)}
            <li>
              <button
                class="list-row find-hit"
                data-testid="rate-find-hit"
                data-title-id={item.id}
                disabled={!!item.rated || rate.busy}
                onclick={() => pick(item)}
              >
                <span class="row-text">
                  <span>{item.name}</span>
                  <span class="footnote data">
                    {[item.year, KIND_LABELS[item.kind] ?? item.kind].filter(Boolean).join(' · ')}
                  </span>
                  {#if item.rated}
                    <span class="footnote" data-testid="rate-find-rated">You rated it {item.rated}</span>
                  {/if}
                </span>
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
    </div>
  {/snippet}
</Sheet>

<style>
  /* Fills the screen between the top row and the tab bar, so the answers sit at the bottom. */
  .rate {
    display: flex;
    min-height: calc(
      100dvh - 44px - env(safe-area-inset-top) - var(--tabbar) - env(safe-area-inset-bottom) - 32px
    );
  }
  .main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .bar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    column-gap: 8px;
    min-height: 44px;
  }
  .bar > :global(.undo) {
    grid-column: 1;
    grid-row: 1;
  }
  .bar > :global(.undo-reason) {
    grid-column: 1 / -1;
    grid-row: 2;
  }
  .title {
    grid-column: 2;
    grid-row: 1;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .mode {
    min-height: 44px;
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 0 8px;
    border: none;
    background: none;
    color: var(--text);
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .mode svg {
    color: var(--text-2);
  }
  .end {
    grid-column: 3;
    grid-row: 1;
    justify-self: end;
    display: flex;
    align-items: center;
  }
  .icon {
    padding: 0 8px;
  }
  .arming {
    margin: 8px 0 0;
  }
  .skip {
    padding-right: 0;
  }
  .note {
    margin: 0;
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    text-wrap: pretty;
  }
  .note svg {
    flex: none;
  }
  .note.small {
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .note.small svg {
    width: 18px;
    height: 18px;
  }
  .note.error {
    padding: 12px 16px;
    border-radius: var(--r-md);
    background: var(--negative-tint);
    color: var(--text);
  }
  .note.error svg {
    color: var(--negative);
  }
  .stage {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .drained {
    margin: auto 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  .drained .why {
    max-width: 52ch;
    margin: 0;
  }
  .done {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .done-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 16px;
  }
  .done-head {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    margin-bottom: 8px;
    text-align: center;
  }
  .done-mark {
    width: 88px;
    height: 88px;
    margin-bottom: 12px;
    display: grid;
    place-items: center;
    border-radius: var(--r-pill);
    background: var(--accent-tint);
    color: var(--accent-text);
  }
  .done-sub {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
  }
  .done-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .done-actions .btn-primary {
    min-height: 50px;
  }
  .side {
    width: 320px;
    flex: none;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .progress-card {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .progress-card h2 {
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .sheet-top {
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-bottom: 12px;
    background: var(--bg-elevated);
  }
  .sheet-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 44px;
  }
  .sheet-bar h2 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .sheet-bar .btn-plain {
    padding-right: 0;
    font-weight: 600;
  }
  .menu .include {
    margin-top: 24px;
  }
  .list-group {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .mode-row,
  .find-hit {
    cursor: pointer;
  }
  .row-text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  /* Choosing one of several: the neutral text colour, not the accent. */
  .check {
    flex: none;
    color: var(--text);
  }
  .kinds {
    display: flex;
    gap: 8px;
    padding: 0 var(--gutter);
  }
  .hits li + li {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .why-sheet {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .find {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .find .why {
    margin: 0;
  }
  .find-hit:disabled {
    cursor: default;
    opacity: 0.55;
  }

  @media (min-width: 721px) {
    .rate {
      min-height: calc(100dvh - 60px - 56px);
    }
    .done {
      width: 100%;
      max-width: 480px;
      margin: 0 auto;
    }
  }
  .rate.wide {
    gap: 48px;
    max-width: 1080px;
    margin: 0 auto;
  }
</style>
