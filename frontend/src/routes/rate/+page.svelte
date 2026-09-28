<script>
  // One envelope from `GET /api/rate` and the writes that answer it: the counter and Undo arrive
  // together, no belief is sent before the tap, and every write returns the next card. `?head=` is
  // repeated, one per pinned title, hence `getAll`.
  import { onDestroy, onMount } from 'svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { page } from '$app/stores';

  import Icon from '$lib/components/Icon.svelte';
  import RateBattleCard from '$lib/components/RateBattleCard.svelte';
  import RateBlockCounter from '$lib/components/RateBlockCounter.svelte';
  import RateClassBalance from '$lib/components/RateClassBalance.svelte';
  import RateModelLog from '$lib/components/RateModelLog.svelte';
  import RatePeek from '$lib/components/RatePeek.svelte';
  import RateRail from '$lib/components/RateRail.svelte';
  import RateSweepCard from '$lib/components/RateSweepCard.svelte';
  import RateUndo from '$lib/components/RateUndo.svelte';
  import Sheet from '$lib/components/Sheet.svelte';
  import {
    FIND_MIN_CHARS,
    KIND_LABELS,
    MODES,
    clearFinder,
    continueRating,
    correct,
    duel,
    findTitles,
    finder,
    heavyClass,
    load,
    modeName,
    notSeen,
    rate,
    rateTitle,
    reset,
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
  // A block's end screen opens with the reply to its last answer, and echoes that guess (decision 530).
  const done = $derived(rate.done);
  const block = $derived(rate.done ?? rate.session?.block ?? null);
  // The card's dash lights on the tap; a skip or a correction never counts (§6.1).
  const answered = $derived(!!done || /^(verdict|duel|not_seen)/.test(rate.pending ?? ''));
  const showModel = $derived(!!session.user?.show_model);
  const heavy = $derived(heavyClass(rate.balance));

  let menuOpen = $state(false);
  let whyOpen = $state(false);
  let mixOpen = $state(false);
  /** @type {null | {title: any, side: 'left' | 'right' | null, token: string}} "About this film". */
  let peek = $state(null);

  function openPeek(side) {
    const card = rate.card;
    if (card) peek = { title: side ? card[side] : card.title, side, token: card.token };
  }

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

  // Closed either way, the menu under it too: a pick that could not be served says so on the page.
  async function pick(item) {
    await rateTitle(item);
    finding = false;
    menuOpen = false;
  }

  onDestroy(() => clearTimeout(findTimer));

  function toggleKind(kind) {
    // An empty selection is a 422, so the last active kind does not turn off.
    const on = kinds.includes(kind);
    if (on && kinds.length === 1) return;
    setKinds(on ? kinds.filter((k) => k !== kind) : [...kinds, kind]);
  }

  // Ignored while typing or while any sheet is open; a held key answers once (decision 528).
  function onKey(event) {
    if (event.repeat || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    const typing = event.target instanceof Element && event.target.closest('input, textarea, select');
    if (typing || document.querySelector('[aria-modal="true"]')) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const card = done || rate.loading ? null : rate.card;
    const much = event.shiftKey;
    const act = {
      z: () => rate.undo?.available && undo(),
      ...(card && { s: skip }),
      ...(card?.type === 'battle' && {
        ArrowLeft: () => duel(card.left.outcome, much),
        ArrowRight: () => duel(card.right.outcome, much),
        ArrowDown: () => duel('TIE'),
        q: () => correct('left'),
        p: () => correct('right')
      }),
      ...(card?.type === 'sweep' && {
        1: () => verdict(0),
        2: () => verdict(1),
        3: () => verdict(2),
        n: notSeen
      })
    }[key];
    if (!act || rate.busy) return;
    event.preventDefault();
    act();
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

<svelte:window onkeydown={onKey} />

<!-- The guess for the card just rated, in the reason line of the one now up: the answer given, the
     film it was about, then the server's words (§6.1). Agreeing looks the same as not. -->
{#snippet echoLine()}
  {@const e = rate.echo}
  <span
    class="echo"
    data-testid="rate-reveal"
    data-reveal-available={e.available ? 'true' : 'false'}
    data-reveal-agreed={e.agreed ? 'true' : 'false'}
  ><Icon name={e.said} size={14} /><span class="echo-text">{e.name} · {e.text}</span></span>
{/snippet}

<!-- In the sheet's body, not its header: the header's drag area captures the pointer. -->
{#snippet sheetBar(title, close)}
  <div class="sheet-bar">
    <h2>{title}</h2>
    <button class="btn-plain done" onclick={close}>Done</button>
  </div>
{/snippet}

{#snippet rateBar()}
  <div class="bar">
    <RateUndo undo={rate.undo} busy={rate.busy} pending={rate.pending === 'undo'} onUndo={undo} />
    <h1 class="title">
      {#if done}
        Rate
      {:else}
        <!-- Mix is where every entry point lands; a mode sticks only once the person changes it. -->
        <button
          class="mode hit"
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
        <!-- Skip writes no observation; it only suppresses the redraw for this sitting. -->
        <button
          class="btn-plain hit skip"
          data-testid="rate-skip"
          aria-busy={rate.pending === 'skip'}
          disabled={!rate.card || rate.busy}
          onclick={skip}
        >Skip</button>
      {/if}
    </div>
  </div>
{/snippet}

<div class="rate" data-testid="rate-surface">
  {#if !topbar.host}{@render rateBar()}{/if}

  <section class="progress" aria-label="Progress">
    <RateBlockCounter {block} {answered}>
      <RateClassBalance balance={rate.balance} {kinds} compact onOpen={() => (mixOpen = true)} />
    </RateBlockCounter>
  </section>
  <p class="sr-only" role="status">{rate.echo ? `${rate.echo.name} · ${rate.echo.text}` : ''}</p>

  {#if heavy && !done}
    <button
      class="heavy"
      data-testid="rate-balance-chip"
      aria-haspopup="dialog"
      onclick={() => (mixOpen = true)}
    >
      <span class="face">
        <Icon name="warning" size={16} />
        <span><strong>Heavy on {heavy}</strong> · See why</span>
      </span>
    </button>
  {/if}
  {#if rate.notice}
    <p class="note" role="status">
      <Icon name="info" size={20} />
      <span data-testid="rate-notice">{rate.notice}</span>
    </p>
  {/if}
  {#if rate.error}
    <p class="note error" role="alert">
      <Icon name="warning" size={20} />
      <span data-testid="rate-error">{rate.error}</span>
    </p>
  {/if}

  <div class="stage">
    {#if rate.loading}
      <!-- The frame's shape until the session lands; which card comes is not known yet. -->
      <div class="loading" data-testid="rate-loading">
        <p class="sr-only" role="status">Finding something to rate…</p>
        <span class="skeleton poster-slot"></span>
        <span class="skeleton answers-slot"></span>
      </div>
    {:else if done}
      <section class="done" data-testid="rate-done">
        <p class="echo-slot">{#if rate.echo}{@render echoLine()}{/if}</p>
        <div class="done-body">
          <div class="done-head">
            <span class="done-mark" aria-hidden="true">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
                <path pathLength="1" d="m5 12.5 4.5 4.5L19 7.5" />
              </svg>
            </span>
            <h2 class="large-title">That's {done.size ?? 15}.</h2>
            <p class="done-sub">Your suggestions just got sharper.</p>
          </div>
          <RateClassBalance balance={rate.balance} {kinds} />
        </div>
        <!-- The last answer stays undoable here until the next one lands (decision 199). -->
        <div class="done-actions">
          <button
            class="btn-primary"
            data-testid="rate-done-more"
            onclick={continueRating}
            {@attach (el) => el.focus({ preventScroll: true })}
          >
            Rate {done.size ?? 15} more
          </button>
          <a class="btn-secondary" data-testid="rate-done-home" href="/">Back to Home</a>
        </div>
      </section>
    {:else if rate.card?.type === 'sweep'}
      <RateSweepCard
        card={rate.card}
        echo={rate.echo ? echoLine : null}
        back={rate.back}
        busy={rate.busy}
        pending={rate.pending}
        failed={rate.failed}
        {showModel}
        onVerdict={verdict}
        onNotSeen={notSeen}
        onPeek={() => openPeek(null)}
        onWhy={() => (whyOpen = true)}
      />
    {:else if rate.card?.type === 'battle'}
      <RateBattleCard
        card={rate.card}
        echo={rate.echo ? echoLine : null}
        busy={rate.busy}
        pending={rate.pending}
        failed={rate.failed}
        onDuel={duel}
        onCorrect={correct}
        onPeek={openPeek}
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
</div>

<!-- Below the fold on purpose: the rating screen itself fits without scrolling (decision 528). -->
{#if showModel}<RateModelLog log={rate.log} ledger={rate.ledger} />{/if}

{#if peek}
  {@const { side, token } = peek}
  <RatePeek
    title={peek.title}
    busy={rate.busy || rate.card?.token !== token}
    onNotSeen={() => rate.card?.token === token && (side ? correct(side) : notSeen())}
    onClose={() => (peek = null)}
  />
{/if}

<Sheet open={mixOpen} onClose={() => (mixOpen = false)} label="Your mix" detent="fit" width={440}>
  {#snippet children(close)}
    <div class="sheet-top">{@render sheetBar('Your mix', close)}</div>
    <RateClassBalance balance={rate.balance} {kinds} />
  {/snippet}
</Sheet>

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

      <div class="list-group find-row">
        <button
          class="list-row"
          data-testid="rate-find-toggle"
          aria-haspopup="dialog"
          aria-expanded={finding}
          onclick={() => (finding = true)}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" />
          </svg>
          <span class="row-text">Find a title to rate</span>
          <Icon name="chevron-right" size={16} />
        </button>
      </div>
    </div>
  {/snippet}
</Sheet>

<Sheet open={whyOpen} onClose={() => (whyOpen = false)} label="Why these?" detent="large" width={520}>
  {#snippet children(close)}
    <div class="sheet-top">{@render sheetBar('Why these?', close)}</div>
    <div class="why-sheet">
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
  /* Exactly the screen between the top row and the tab bar, so nothing scrolls; of the main's 32px
     end padding it keeps 16 (decisions 528 and 530). */
  .rate {
    display: flex;
    flex-direction: column;
    gap: 12px;
    height: calc(
      100dvh - 44px - env(safe-area-inset-top) - var(--tabbar) - env(safe-area-inset-bottom) - 16px
    );
    margin-bottom: -16px;
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
  }
  .title {
    grid-column: 2;
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
    justify-self: end;
    display: flex;
    align-items: center;
  }
  .skip {
    padding-right: 0;
    transition: opacity var(--dur-quick) var(--ease);
  }
  .skip[aria-busy='true'] {
    opacity: 1;
    animation: pop 180ms var(--ease);
  }
  /* The row's end is the screen's gutter: a finger's reach may not widen the row past it. */
  .skip::after {
    right: 0;
  }
  .progress {
    flex: none;
  }
  .heavy {
    flex: none;
    align-self: center;
    min-height: 44px;
    margin: -6px 0;
    padding: 0;
    border: none;
    background: none;
    color: var(--accent-text);
    overflow: hidden;
    animation: grow var(--dur-base) var(--ease);
  }
  .heavy .face {
    height: 32px;
    padding: 0 12px 0 10px;
    display: flex;
    align-items: center;
    gap: 6px;
    border-radius: var(--r-pill);
    background: var(--warning-tint);
    font-size: var(--fs-footnote);
    line-height: 18px;
    white-space: nowrap;
  }
  .heavy .face > :global(svg) {
    color: var(--warning);
  }
  .heavy strong {
    font-weight: 600;
    color: var(--text);
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
    overflow: hidden;
    animation: grow var(--dur-base) var(--ease);
  }
  /* A notice or the balance chip grows in, so the poster eases down rather than jumping. */
  @keyframes grow {
    from {
      min-height: 0;
      max-height: 0;
      margin-block: -12px 0;
      padding-block: 0;
      opacity: 0;
    }
    to {
      max-height: 64px;
    }
  }
  .note > :global(svg) {
    flex: none;
  }
  .note.error {
    padding: 12px 16px;
    border-radius: var(--r-md);
    background: var(--negative-tint);
    color: var(--text);
  }
  .note.error > :global(svg) {
    color: var(--negative);
  }
  .stage {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .loading {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    padding-top: 58px;
  }
  .poster-slot {
    flex: 0 1 330px;
    min-height: 0;
    aspect-ratio: 2 / 3;
  }
  .answers-slot {
    flex: none;
    align-self: stretch;
    height: 64px;
    margin-top: auto;
    border-radius: var(--r-md);
  }
  .echo {
    min-width: 0;
    height: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    color: var(--text);
    white-space: nowrap;
    animation: enter var(--dur-quick) var(--ease);
  }
  .echo > :global(svg) {
    flex: none;
    color: var(--text-2);
  }
  .echo-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
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
  .echo-slot {
    flex: none;
    height: 18px;
    margin: 0;
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
    background: var(--surface-2);
    color: var(--text);
    --enter-s: 0.7;
    animation: enter 220ms var(--ease-spring) both;
  }
  .done-sub {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
    animation: fadeIn 240ms var(--ease) 220ms both;
  }
  .done-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
    animation: enter 200ms var(--ease) 420ms both;
  }
  .done-mark path {
    stroke-dasharray: 1;
    animation: draw 280ms var(--ease) 120ms both;
  }
  .done-head h2 {
    animation: fadeIn 240ms var(--ease) 160ms both;
  }
  .done :global(.bar > span) {
    animation: unclip 360ms var(--ease) 260ms both;
  }
  .done :global(li:nth-child(2) .bar > span) {
    animation-delay: 320ms;
  }
  .done :global(li:nth-child(3) .bar > span) {
    animation-delay: 380ms;
  }
  @keyframes unclip {
    from { clip-path: inset(0 100% 0 0); }
  }
  .done-actions .btn-primary {
    min-height: 50px;
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
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    gap: 8px;
    min-height: 44px;
  }
  .sheet-bar h2 {
    grid-column: 2;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .sheet-bar .done {
    justify-self: end;
    padding-right: 0;
    font-weight: 600;
  }
  .menu .include,
  .menu .find-row {
    margin-top: 24px;
  }
  .list-group {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .mode-row,
  .find-hit,
  .find-row .list-row {
    cursor: pointer;
  }
  .find-row .list-row {
    gap: 12px;
  }
  .find-row :global(svg:last-child) {
    color: var(--text-3);
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

  /* A left-aligned column like every other page, as tall as it needs: the posters take the
     window's height up to 420px and the answers follow right under them (decision 529). */
  @media (min-width: 721px) {
    .rate {
      --rate-gap: 32px;
      --rate-col: calc(clamp(240px, 100dvh - 410px, 420px) * 2 / 3);
      width: min(100%, calc(var(--rate-col) * 2 + var(--rate-gap)));
      height: auto;
      margin-bottom: 0;
      gap: 16px;
    }
    .stage {
      flex: none;
    }
    .loading {
      flex: none;
      align-items: flex-start;
      gap: 20px;
      padding-top: 72px;
    }
    .poster-slot {
      flex: none;
      width: var(--rate-col);
      height: calc(var(--rate-col) * 1.5);
    }
    .echo {
      justify-content: flex-start;
    }
    .heavy {
      align-self: flex-start;
    }
    .bar {
      display: flex;
    }
    .title {
      order: -1;
      margin-right: auto;
    }
    .mode {
      margin-left: -8px;
    }
    .done-head {
      align-items: flex-start;
      text-align: left;
    }
  }
</style>
