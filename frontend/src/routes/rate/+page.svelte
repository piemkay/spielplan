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
   * the queue — repeated parameters, one per title, which is why it is read with `getAll`. The
   * finish prompt's "Rate it now" arrives the same way (the title card answers on the card itself
   * since decision 487), and this page's own "find" search pins through the same `head` (C5.2 of
   * the 2026-09-25 household test): a person who knows a film can rate it without marking it seen
   * and waiting for the queue to come round.
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
  // While a reveal holds, the counter belongs to the card being shown, not to the next one. The
  // line names the chosen mode rather than the card type (A2 of the 2026-09-26 household test),
  // so the served type no longer has to be patched onto it.
  const block = $derived(rate.frozenBlock ?? rate.session?.block ?? null);
  const reveal = $derived(revealLine(rate.reveal));
  const showModel = $derived(!!session.user?.show_model);
  const modeWhy = $derived(MODES.find(([key]) => key === mode)?.[2] ?? '');

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

  // "a title you know". Closed by default so the card keeps the phone's vertical budget; the
  // query waits a beat after the last keystroke, and `findTitles` drops any answer a newer
  // keystroke has overtaken.
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
      <RateBlockCounter {block} {kinds} {mode} />
    </div>

    <div class="controls">
      <!-- §6.1's three modes. Mix is where every entry point lands (proposal 36); a mode
           becomes sticky only once the person changes it themselves. Named by what each asks,
           not by the spec's words for them (decision 519). -->
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

      <!-- "a title you know" (C5.2), beside the modes: on a phone it shares their row, so the
           kinds and Undo keep one row of their own whatever the chip's words are. -->
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

  {#if finding}
    <!-- The person's own pick: a title they know, pinned to the head of the queue and rated
         on §6.1's card like every other. A title already rated says so and is not offered —
         the queue never serves one, and a tap that did nothing would be the defect this fixes. -->
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

  <!-- §6.8's register: the control says what it is, and one line says what it does. Hanging
       that sentence off each pill's `title` would make it a tooltip no phone can read and
       would give the button an accessible name it does not want. -->
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
        <!-- Proposal 37: the queue drains into an explicit end state rather than wrapping. The
             chrome is keyed to `drained.cause` because proposal 37 describes ONE of the three
             ways to have no card — the sweep queue spent — and this block was written for it
             alone: a heading saying nothing is left to queue, the §6.3 handoff, and Rank as the
             only way out. The battle pool has the opposite shape. It is empty because the person
             has rated too little, not too much, and `rate/session.py`'s `DRAINED_CAUSES` says so
             in the sentence below — so the heading contradicted it and the single CTA sent a
             first-week member with zero ratings to an empty tier board, which is the direction
             that cannot help. The server's `cause` exists precisely so this page picks its own
             heading instead of inferring one (§6.8; finding 20, cycle 1 M410-D8-01). -->
        <div class="drained card" data-testid="rate-drained">
          {#if rate.drained.cause === 'pool'}
            <h2>No pair to compare yet</h2>
            <p class="why">{rate.drained.text}</p>
            <!-- A mode change rather than a link: the sweep queue this names is on THIS surface,
                 and proposal 36 makes the mode sticky from an explicit change, which this is. -->
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
    /* Tight: a tool strip above the card being rated, and on a phone every pixel it takes is
       one the card does not get. */
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
    /* Proposal 37's drained state: it replaces the stage rather than joining a stack,
       and it is the same terminal-empty object as Home's and ShelfList's. */
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
     It used to scroll the controls sideways under `data-nobar`, so on an iPhone 13 the Series
     toggle and §6.1's persistent Undo sat off the right edge with nothing showing the row
     moved -- decision 35's chip "disables visibly" only if it can be seen at all. The row wraps
     now: the three modes as one segmented control with find beside them, then the kinds and
     Undo. One more row of 48 px targets (§6 preamble), and nothing out of sight.
     [C5.6 of the 2026-09-25 household test] */
  @media (max-width: 720px) {
    .rate {
      gap: 10px;
    }
    h1 {
      font-size: 17px;
    }
    /* The mode's one-line why moves under the card rather than going: above it, it cost the
       battle card the row its decisive toggle needed. */
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
    /* The modes' names are words now (decision 519), so the segments give their padding to the
       name rather than let the row outgrow a phone. */
    .group.modes .pill {
      flex: 1;
      padding-left: 6px;
      padding-right: 6px;
    }
    /* The magnifier alone on a phone: the word is the one thing in that row that can go. */
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
