<script>
  /**
   * Rate. Spec v2.1 §6.1, with decision-doc proposals 34–53 and 153, and decision 35.
   *
   *   "**Modes:** **Mix** (default — alternates sweep and battle), Sweep, Battle; blocks of 15."
   *
   * The whole surface is one envelope from `GET /api/rate` and the writes that answer it. That
   * is not an implementation convenience — it is what makes three of §6.1's rules true rather
   * than merely intended:
   *
   *   * **The counter and Undo are the same number.** Decision 35 measures Undo's depth in "the
   *     counter the user is already reading". `session.block.counter` and `undo.available`
   *     arrive together, so the chip cannot disagree with the ticks above it.
   *   * **No belief before the tap.** §6.1 (Cosley 2003), extended to battles by proposal 34.
   *     The card carries no predicted class, no ledger score, no σ and no tier; the prediction
   *     rides on the response to the verdict and nowhere else.
   *   * **Next card preloaded.** §6 preamble's throughput budget. Every write answers with the
   *     next card, so the only request between two taps is the tap itself.
   *
   * `?head=` is §6.0's pending-verdicts banner arriving with its titles pinned to the front of
   * the queue — repeated parameters, one per title, which is why it is read with `getAll`.
   */
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
    MODES,
    commit,
    correct,
    duel,
    load,
    notSeen,
    rate,
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
  // While a reveal holds, the counter belongs to the card being shown, not to the next one.
  const held = $derived(rate.frozenBlock ?? rate.session?.block ?? null);
  // The server's `serving` is what the slot *would* draw. Undo can restore a card of the other
  // type — a sweep card popped back while the mode says battle — and the counter has to name
  // the card in front of the person rather than the one the slot would have dealt.
  const block = $derived(held && rate.card ? { ...held, serving: rate.card.type } : held);
  const reveal = $derived(revealLine(rate.reveal));
  const showModel = $derived(!!session.user?.show_model);
  const modeWhy = $derived(MODES.find(([key]) => key === mode)?.[1] ?? '');

  // Read synchronously at init, before the first `load()`. `onMount` runs ahead of the effect
  // below, so a deep link arriving as `/rate?head=41&head=57` would otherwise open its first
  // card with no head at all — the banner's CTA would look like it had done nothing.
  let lastSearch = $page.url.search;
  setHead($page.url.searchParams.getAll('head'));

  onMount(load);
  onDestroy(reset);

  // Decision 117's toggle changes what the SERVER sends, not what this page hides: with it off
  // the response carries no `log` and no `ledger` at all. So a flip has to re-read, and it has
  // to wait for the server to have the preference — `setShowModel` sets the local user
  // optimistically and awaits the POST afterwards, so refetching on the local flip races the
  // write and returns the pre-toggle payload. The account chip bumps this epoch once the write
  // has landed. `lastModelEpoch` is deliberately not `$state`: an effect that read and wrote
  // its own reactive memory would re-run forever.
  let lastModelEpoch = modelGate.epoch;
  $effect(() => {
    const epoch = modelGate.epoch;
    if (epoch === lastModelEpoch) return;
    lastModelEpoch = epoch;
    load();
  });

  // A client-side navigation to /rate?head=… does not remount this component, so the banner's
  // CTA has to be picked up reactively too, or following it a second time would do nothing.
  $effect(() => {
    const search = $page.url.search;
    const ids = $page.url.searchParams.getAll('head');
    if (search === lastSearch) return;
    lastSearch = search;
    setHead(ids);
    if (rate.booted) load({ quiet: true });
  });

  function toggleKind(kind) {
    // §4.1 rule 5 partitions every ranking surface; the empty selection is a 422 rather than
    // "everything", so the last active toggle does not turn off.
    const on = kinds.includes(kind);
    if (on && kinds.length === 1) return;
    setKinds(on ? kinds.filter((k) => k !== kind) : [...kinds, kind]);
  }
</script>

<div class="rate" data-testid="rate-surface">
  <header>
    <div class="titles">
      <h1>Rate</h1>
      <RateBlockCounter {block} {kinds} />
    </div>

    <div class="controls" data-nobar>
      <!-- §6.1's three modes. Mix is where every entry point lands (proposal 36); a mode
           becomes sticky only once the person changes it themselves. -->
      <div class="group" role="group" aria-label="Mode">
        {#each MODES as [key] (key)}
          <button
            class="pill"
            data-testid="rate-mode-{key}"
            aria-pressed={mode === key}
            onclick={() => setMode(key)}
          >{key}</button>
        {/each}
      </div>

      <!-- Proposal 46: the Rate surface carries the partition control itself, and the counter
           names the active partition. The queue and the battle pool never mix kinds. -->
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

  <!-- §6.8's register: the control says what it is, and one line says what it does. Hanging
       that sentence off each pill's `title` would make it a tooltip no phone can read and
       would give the button an accessible name it does not want. -->
  <p class="why mode-why" data-testid="rate-mode-note">{mode} — {modeWhy}</p>

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
          onDuel={duel}
          onCorrect={correct}
          onSkip={skip}
          onDecisive={setDecisive}
        />
      {:else if rate.drained}
        <!-- Proposal 37: the queue drains into an explicit end state rather than wrapping. -->
        <div class="drained card" data-testid="rate-drained">
          <h2>Nothing left to queue</h2>
          <p class="why">{rate.drained.text}</p>
          <p class="why">
            The §6.3 comparison queue sharpens the boundaries once the tier list has some.
          </p>
          <a class="btn-ghost" href="/rank">Go to Rank</a>
        </div>
      {/if}
    </div>

    <div class="side">
      <RateClassBalance balance={rate.balance} />
      <RateRail balance={rate.balance} {mode} />
      {#if showModel}
        <!-- §6.7, per-user toggle, default off. Everything in it describes a write that has
             already landed, which is the only reason it may be shown at all. -->
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
  .mode-why {
    margin: 0;
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
    padding: 26px;
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

  /* Proposal 16's one breakpoint, and proposal 43's rule for what happens at it: nothing in
     the rail is dropped — the side column moves under the card instead of beside it. */
  @media (max-width: 980px) {
    .columns {
      grid-template-columns: minmax(0, 1fr);
    }
    header {
      align-items: flex-start;
    }
  }

  /* The compact layout spends its vertical budget on the card, not on the chrome above it.
     The controls scroll sideways rather than stacking into three rows — §6 preamble's 48 px
     targets are non-negotiable, so the row that holds them has to move instead. */
  @media (max-width: 720px) {
    .rate {
      gap: 10px;
    }
    h1 {
      font-size: 17px;
    }
    .controls {
      gap: 8px;
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      width: 100%;
    }
    .group {
      flex: none;
    }
  }
</style>
