<script>
  // One envelope from `GET /api/rate` and the writes that answer it: the counter and Undo arrive
  // together, no belief is sent before the tap, and every write returns the next card. `?head=` is
  // repeated, one per pinned title, hence `getAll`.
  import { onDestroy, onMount } from 'svelte';
  import { modelGate } from '$lib/home.svelte.js';
  import { page } from '$app/stores';

  import ActionSheet from '$lib/components/ActionSheet.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import RateBeforeSetup from '$lib/components/RateBeforeSetup.svelte';
  import RateLadder from '$lib/components/RateLadder.svelte';
  import RateModelLog from '$lib/components/RateModelLog.svelte';
  import RatePeek from '$lib/components/RatePeek.svelte';
  import RateUndo from '$lib/components/RateUndo.svelte';
  import {
    KIND_TITLES,
    continueRating,
    load,
    notSeen,
    place,
    rate,
    reset,
    setHead,
    setKind,
    undo
  } from '$lib/rate.svelte.js';
  import { session } from '$lib/session.svelte.js';
  import { topbar } from '$lib/topbar.svelte.js';

  const closed = $derived(rate.setup?.done === false);
  const kind = $derived(rate.session?.kind ?? null);
  const showModel = $derived(!!session.user?.show_model);

  // The server's noun is the plural: one film still waits for its step.
  function waitingLine({ rated_before: n, noun }) {
    return n === 1
      ? `1 ${noun === 'films' ? 'film' : noun} you rated before still waits for its step.`
      : `${n} ${noun} you rated before still wait for their step.`;
  }

  let choosingKind = $state(false);
  /** @type {null | {title: any, token: string}} "About this film". */
  let peek = $state(null);

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

  // 1 is the top shelf; ignored while typing or while any sheet is open, and a held key answers
  // once (decision 550).
  function onKey(event) {
    if (event.repeat || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    const typing = event.target instanceof Element && event.target.closest('input, textarea, select');
    if (typing || document.querySelector('[aria-modal="true"]')) return;
    const key = event.key.toLowerCase();
    const card = rate.done || rate.loading ? null : rate.card;
    const shelf = card && /^[1-9]$/.test(key) ? card.shelves?.[Number(key) - 1] : null;
    const act =
      key === 'z' && rate.undo?.available
        ? undo
        : key === 'n' && card
          ? notSeen
          : shelf
            ? () => place(shelf.tier)
            : null;
    if (!act || rate.busy) return;
    event.preventDefault();
    act();
  }

  // Undo, the kind and the counter are the shell's top row here, beside You (decision 545).
  $effect(() => {
    if (!topbar.host) return;
    topbar.content = rateBar;
    return () => {
      if (topbar.content === rateBar) topbar.content = null;
    };
  });
</script>

<svelte:window onkeydown={onKey} />

{#snippet rateBar()}
  <div class="bar">
    {#if !closed}
      <RateUndo undo={rate.undo} pending={rate.pending === 'undo'} onUndo={undo} />
    {/if}
    <h1 class="title">
      {#if kind}
        <button
          class="hit kind"
          data-testid="rate-kind"
          aria-haspopup="dialog"
          aria-expanded={choosingKind}
          onclick={() => (choosingKind = true)}
        >
          <span class="kind-name">{KIND_TITLES[kind]}<svg width="12" height="12" viewBox="0 0 24 24"
              fill="none" stroke="currentColor" stroke-width="2.75" stroke-linecap="round"
              stroke-linejoin="round" aria-hidden="true"><path d="m5.5 9.5 6.5 6.5 6.5-6.5" /></svg></span>
          {#if !rate.done}
            <span class="counter" data-testid="rate-counter">{rate.session.block?.counter ?? ''}</span>
          {/if}
        </button>
      {:else}
        <span class="plain">Rate</span>
      {/if}
    </h1>
  </div>
{/snippet}

<div class="rate" data-testid="rate-surface">
  {#if !topbar.host}{@render rateBar()}{/if}
  <p class="sr-only" role="status">{rate.echo ? `${rate.echo.name} · ${rate.echo.word}` : ''}</p>

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

  {#if rate.loading}
    <!-- The frame's shape until the session lands; which card comes is not known yet. -->
    <div class="loading" data-testid="rate-loading">
      <p class="sr-only" role="status">Finding something to rate…</p>
      <span class="skeleton poster-slot"></span>
      <span class="skeleton shelves-slot"></span>
    </div>
  {:else if closed}
    <RateBeforeSetup earlierRatings={rate.setup.earlier_ratings ?? 0} />
  {:else if rate.done}
    <section class="done" data-testid="rate-done">
      <div class="done-head">
        <span class="done-mark" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <path pathLength="1" d="m5 12.5 4.5 4.5L19 7.5" />
          </svg>
        </span>
        <h2 class="large-title">That's 15.</h2>
        <p class="done-sub">Your suggestions just got sharper.</p>
        {#if rate.done.rated_before > 0}
          <p class="footnote" data-testid="rate-done-waiting">{waitingLine(rate.done)}</p>
        {/if}
      </div>
      <!-- The last answer stays undoable here until the next one lands (decision 199). -->
      <div class="done-actions">
        <button
          class="btn-primary"
          data-testid="rate-done-more"
          onclick={continueRating}
          {@attach (el) => el.focus({ preventScroll: true })}
        >Rate 15 more</button>
        <a class="btn-secondary" data-testid="rate-done-home" href="/">Back to Home</a>
      </div>
    </section>
  {:else if rate.card}
    <RateLadder
      card={rate.card}
      echo={rate.echo}
      back={rate.back}
      busy={rate.busy}
      pending={rate.pending}
      {showModel}
      onPlace={place}
      onNotSeen={notSeen}
      onPeek={() =>
        (peek = { title: { ...rate.card.title, kind: rate.card.kind }, token: rate.card.token })}
    />
  {:else if rate.drained}
    <p class="why drained" data-testid="rate-drained">{rate.drained.line}</p>
  {/if}
</div>

<!-- Below the fold on purpose: the rating screen itself fits without scrolling (decision 545). -->
{#if showModel}<RateModelLog log={rate.log} ledger={rate.ledger} />{/if}

{#if peek}
  {@const { token } = peek}
  <RatePeek
    title={peek.title}
    busy={rate.busy || rate.card?.token !== token}
    onNotSeen={() => rate.card?.token === token && notSeen()}
    onClose={() => (peek = null)}
  />
{/if}

<ActionSheet
  open={choosingKind}
  title="What to rate"
  options={Object.entries(KIND_TITLES).map(([key, label]) => ({
    label,
    checked: key === kind,
    onSelect: () => setKind(key)
  }))}
  onClose={() => (choosingKind = false)}
/>

<style>
  /* The screen between the top row and the tab bar, 8px clear of the bar (decision 545's board):
     of the main's 32px end padding it keeps 8. */
  .rate {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 8px;
    min-height: calc(
      100dvh - 44px - env(safe-area-inset-top) - var(--tabbar) - env(safe-area-inset-bottom) - 8px
    );
    margin-bottom: -24px;
  }
  .bar {
    display: grid;
    grid-template-columns: 44px minmax(0, 1fr);
    align-items: center;
    min-height: 44px;
    /* The avatar beside the row is 38px wide, so the title sits on the screen's centre. */
    padding-right: 6px;
  }
  .title {
    grid-column: 2;
    justify-self: center;
    min-width: 0;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .kind {
    height: 44px;
    min-height: 44px;
    padding: 0 12px;
    border: none;
    background: none;
    color: var(--text);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .kind-name {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .kind-name svg {
    color: var(--text-3);
  }
  .counter {
    font-size: var(--fs-caption);
    line-height: 16px;
    font-weight: 400;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .plain {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    padding: 0 8px;
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
  .loading {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .poster-slot {
    width: 84px;
    height: 126px;
  }
  .shelves-slot {
    height: 504px;
    border-radius: 12px;
  }
  .drained {
    margin: auto 0;
    max-width: 52ch;
  }
  .done {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 24px;
  }
  .done-head {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
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
  .done-mark path {
    stroke-dasharray: 1;
    animation: draw 280ms var(--ease) 120ms both;
  }
  .done-head h2 {
    animation: fadeIn 240ms var(--ease) 160ms both;
  }
  .done-sub {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    color: var(--text-2);
    animation: fadeIn 240ms var(--ease) 220ms both;
  }
  .done-head .footnote {
    margin: 4px 0 0;
  }
  .done-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
    animation: enter 200ms var(--ease) 420ms both;
  }
  .done-actions .btn-primary {
    min-height: 50px;
  }

  /* Two columns from the content's left edge: the film at the window's height (260x390 at 800, up
     to 280x420), and seven shelves of four posters and 170px for the word, fitting under the top
     row and growing with the window as long as the film keeps 180px. */
  @media (min-width: 721px) {
    .rate {
      --rate-col: calc(clamp(240px, 100dvh - 410px, 420px) * 2 / 3);
      --shelf-ph: clamp(64px, min((100dvh - 188px) / 7, (100cqw - 398px) / 2.75), 104px);
      --shelves-w: calc(var(--shelf-ph) * 2.75 + 186px);
      --rate-grid: min(var(--rate-col), 100cqw - 32px - var(--shelves-w)) var(--shelves-w);
      container-type: inline-size;
      min-height: 0;
      padding-top: 16px;
      margin-bottom: 0;
      gap: 16px;
    }
    .bar {
      display: flex;
      padding-right: 0;
    }
    .title {
      order: -1;
      margin-right: auto;
    }
    .kind {
      margin-left: -12px;
      align-items: flex-start;
    }
    .plain {
      margin-left: -8px;
    }
    .loading {
      display: grid;
      grid-template-columns: var(--rate-grid);
      align-items: start;
      gap: 32px;
    }
    .poster-slot {
      width: auto;
      height: auto;
      aspect-ratio: 2 / 3;
    }
    .shelves-slot {
      height: calc((var(--shelf-ph) + 8px) * 7);
    }
    .done {
      align-items: flex-start;
      gap: 32px;
    }
    .done-head {
      align-items: flex-start;
      text-align: left;
    }
    .done-actions {
      flex-direction: row;
      gap: 12px;
    }
    .done-actions > * {
      min-height: 50px;
      padding: 0 24px;
    }
  }
</style>
