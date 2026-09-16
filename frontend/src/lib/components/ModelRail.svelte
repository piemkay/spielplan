<script>
  /**
   * §6.7's model log — the transparency rail. Spec v2.1 §6.7, §6.8; decisions 117 and 118.
   *
   * §6.7: "A per-user toggle (default off) reveals an ephemeral log (last ~15 events, never
   * persisted) narrating every model write in one human-readable line … It is 'drag-and-drop
   * is data, not override' made visible, and the primary M2 debugging instrument."
   *
   * Proposal 118: "The rail is a right-hand drawer on wide layouts and a bottom sheet on
   * compact ones, opened from the profile toggle and from a keyboard shortcut. Entries carry
   * an event kind … used for colour-coding and filtering. It holds the last **15** events — a
   * pinned depth, not 'about fifteen'."
   *
   * THE GATE IS THE SERVER'S. Decision 117 turns the toggle off by removing `events` from
   * `/api/model-log` entirely — not by sending an empty list. This component therefore renders
   * nothing at all without an `events` array, and there is no client-side branch that could
   * reconstruct one.
   */
  import { eventTime, loadModelLog } from '$lib/home.svelte.js';
  import { dismiss } from '$lib/dismiss.js';

  let { open = false, onClose, suppressed = [] } = $props();

  let log = $state(null);
  let error = $state('');
  let filter = $state('');

  // Refetch every time the drawer opens: the log is ephemeral by definition, and a rail that
  // shows what the model did ten minutes ago while claiming to be live is worse than closed.
  $effect(() => {
    // Dropped on the way DOWN as well as on the way up. Without it the second open renders the
    // FIRST open's events under a header reading "never persisted" until the refetch lands -- and
    // the `{:else if !log}` branch below, which exists to say the drawer is reading, was
    // unreachable after the first open. Clearing it only on the way up leaves the same render one
    // frame long: `{#if open}` paints before this effect runs, so the drawer would still show the
    // previous open's events, briefly, under that header. §6.7's log is ephemeral, and a drawer
    // that holds one across a close is keeping it. [M4.9 finding 27]
    log = null;
    if (!open) return;
    let cancelled = false;
    error = '';
    loadModelLog(15)
      .then((res) => {
        if (!cancelled) log = res;
      })
      .catch((err) => {
        if (!cancelled) error = err.message;
      });
    return () => {
      cancelled = true;
    };
  });

  const events = $derived(log?.events ?? []);
  const kinds = $derived(log?.kinds ?? []);
  const shown = $derived(filter ? events.filter((e) => e.kind === filter) : events);

  /**
   * The shell's trigger, which is the one outside tap this drawer must NOT dismiss on.
   *
   * `rail.svelte.js` documents `toggleRail` as one rule for the button and proposal 118's `m`
   * shortcut — "Flip the drawer" — and the button lives in `+layout.svelte`'s header, outside
   * this node. A plain outside-tap dismissal closes on its pointerdown and the click that follows
   * flips it straight back open, so the control that opens the drawer would stop being able to
   * close it. The keystroke needs no such guard: `m` is not Escape, and Escape pressed while the
   * trigger happens to hold focus is still a dismissal.
   */
  const OPENER = '[data-testid="model-rail-open"]';

  /** @param {Event} event */
  function dismissRail(event) {
    const target = event.target;
    if (event.type === 'pointerdown' && target instanceof Element && target.closest(OPENER)) return;
    onClose?.();
  }
</script>

{#if open}
  <!-- Proposal 131: "Every popover, menu and sheet dismisses on outside click and on Escape."
       Proposal 118 makes this a sheet on compact layouts, which is the shape that most needs it:
       62 vh of the screen whose only exit was the control in its corner. [proposals 127, 131] -->
  <aside
    class="rail"
    data-model-log
    aria-label="Model log"
    data-testid="model-rail"
    use:dismiss={dismissRail}
  >
    <header>
      <div>
        <div class="title">Model log</div>
        <div class="data">last 15 events · never persisted · §6.7</div>
      </div>
      <button class="close" onclick={onClose} aria-label="Close the model log" data-testid="model-rail-close">✕</button>
    </header>

    {#if error}
      <p class="data err" role="alert">{error}</p>
    {:else if log && !log.show_model}
      <!-- Reachable only for a moment: the control that opens this lives behind the same
           preference. Saying it plainly beats an empty drawer. -->
      <p class="why" data-testid="model-rail-off">{log.hint}</p>
    {:else if !log}
      <p class="data">reading the journal…</p>
    {:else}
      {#if kinds.length > 1}
        <div class="filters" role="group" aria-label="Event kind">
          <button class="chip" class:on={filter === ''} onclick={() => (filter = '')} data-testid="model-rail-filter-all">all</button>
          {#each kinds as k (k)}
            <button
              class="chip"
              class:on={filter === k}
              onclick={() => (filter = filter === k ? '' : k)}
              data-testid="model-rail-filter"
              data-kind={k}
            >{k}</button>
          {/each}
        </div>
      {/if}

      {#if shown.length}
        <ol class="events">
          {#each shown as e (e.id)}
            <li data-testid="model-rail-event" data-kind={e.kind} data-scope={e.scope}>
              <div class="meta data">
                <span class="kind" data-kind={e.kind}>{e.kind}</span>
                <span>{eventTime(e.at)}</span>
                <span class="scope">{e.scope}</span>
              </div>
              <!-- §6.7: "one human-readable line". Mono, because every one of them is a model
                   number, an id or a data annotation (§6.8). -->
              <div class="line">{e.text}</div>
            </li>
          {/each}
        </ol>
      {:else}
        <p class="why" data-testid="model-rail-empty">
          Nothing written yet. The rail narrates model writes only — panning, zooming and
          filtering leave no line, on purpose.
        </p>
      {/if}

      {#if suppressed?.length}
        <!-- Gated with the rest of decision 117's annotations. §6.0: a shelf that cannot
             justify itself is ABSENT, so without this list the absence is indistinguishable
             from a bug. -->
        <section class="suppressed">
          <div class="data heading">SHELVES THAT DID NOT SHIP</div>
          <ul>
            {#each suppressed as s, i (s.shelf + ':' + s.kind + ':' + i)}
              <li class="why" data-testid="model-rail-suppressed" data-shelf={s.shelf}>
                <!-- Joined in JS: Svelte collapses the whitespace around an {#if}, which turned
                     "because_anchor · series" into "because_anchor· series". -->
                <span class="sid">{[s.shelf, s.kind].filter(Boolean).join(' · ')}</span>
                {`— ${s.reason}`}
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    {/if}
  </aside>
{/if}

<style>
  /* The same inset the header reserves, for the same reason: `app.html` asks for
     `viewport-fit=cover` and a black-translucent status bar, so the installed web view begins
     above the header and a drawer anchored at a bare 54 px opens behind the clock. The compact
     override below is a bottom sheet (`top: auto`) and needs none of this. [§6 preamble;
     decision 279] */
  .rail {
    position: fixed;
    top: calc(54px + env(safe-area-inset-top));
    right: 0;
    bottom: 0;
    width: min(430px, 100%);
    overflow: auto;
    background: var(--ground-raised);
    border-left: 1px solid var(--line-3);
    padding: 16px 18px 40px;
    z-index: 55;
    animation: fadeIn 0.12s ease;
  }
  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 12px;
  }
  .title {
    font-size: 14.5px;
    font-weight: 600;
  }
  /* 48 px on both axes, not only the one `design.css`'s coarse block reaches. It raises
     `min-height` and never `min-width`, so this exit measured 48 by 32 on a phone. [§6 preamble] */
  .close {
    background: none;
    border: none;
    color: var(--ink-4);
    cursor: pointer;
    font-size: 14px;
    width: 32px;
    height: 32px;
  }
  .filters {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .chip {
    font-family: var(--mono);
    font-size: 10px;
    padding: 3px 8px;
    border-radius: var(--r-pill);
    border: 1px solid var(--line-2);
    background: transparent;
    color: var(--ink-3);
    cursor: pointer;
  }
  .chip.on {
    border-color: var(--ember);
    color: var(--ember-lift);
  }
  /* Both axes, on the pointer the rule is about, for both of this component's controls.
     `design.css`'s coarse block raises `min-height` on `button` and never `min-width`, and it
     gives `padding-inline` to three primitives a `.chip` is none of — so the exit came out 48 by
     32 and the kind filters 48 by 36: the same defect, eight lines apart, and only the exit was
     repaired. The filters are three and four characters of a 10 px mono face in a wrapping row
     with a 6 px gap, on the surface §6.7 calls the primary M2 debugging instrument, and a
     mis-tap changes which events it is showing.
     COARSE, not unconditional: the rule these complete lives behind `pointer: coarse`, so
     declaring the width outside it made both controls 48 wide and 32 tall on a mouse — the same
     lopsidedness rotated, and in `TitleDetail` it put the box over the end of the heading. A
     mouse has no 48 px floor; §6's preamble writes it for fingers. Still scoped here rather than
     added to design.css's coarse list, which decision-level would inflate every narrow control
     in the app. [§6 preamble; M4.15 review cycle 1] */
  @media (pointer: coarse) {
    .close,
    .chip {
      min-width: var(--touch);
    }
  }
  .events {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .events li {
    border: 1px solid var(--line);
    border-left: 2px solid var(--line-3);
    border-radius: var(--r-sm);
    background: var(--card);
    padding: 8px 10px;
  }
  /* Proposal 118: the event kind is used for colour-coding. Bound to the facet palette rather
     than to new colours — §6.8 ships eleven and one accent, and no more. */
  .events li[data-kind='verdict'] { border-left-color: var(--facet-mood); }
  .events li[data-kind='duel'] { border-left-color: var(--facet-pacing); }
  .events li[data-kind='tier_edit'] { border-left-color: var(--facet-structure); }
  .events li[data-kind='not_seen'] { border-left-color: var(--facet-register); }
  .events li[data-kind='undo'] { border-left-color: var(--facet-place); }
  .events li[data-kind='ledger_refit'],
  .events li[data-kind='ledger_incremental'] { border-left-color: var(--facet-themes); }
  .events li[data-kind='foldin'],
  .events li[data-kind='blend_weight'] { border-left-color: var(--facet-characters); }
  .events li[data-kind='placement'],
  .events li[data-kind='reconcile'] { border-left-color: var(--facet-visual); }
  .events li[data-kind='bundle_swap'] { border-left-color: var(--facet-era); }

  .meta {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 3px;
  }
  .kind {
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
  }
  .scope {
    margin-left: auto;
    opacity: 0.7;
  }
  .line {
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.5;
    color: var(--ink-2);
    word-break: break-word;
  }
  .err {
    color: var(--ember-lift);
  }
  .suppressed {
    margin-top: 18px;
    border-top: 1px solid var(--line);
    padding-top: 12px;
  }
  .heading {
    letter-spacing: 0.12em;
    margin-bottom: 6px;
  }
  .suppressed ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .sid {
    color: var(--ink-3);
  }

  /* Proposal 118: a bottom sheet on compact layouts. */
  @media (max-width: 720px) {
    .rail {
      top: auto;
      left: 0;
      right: 0;
      height: 62vh;
      border-left: none;
      border-top: 1px solid var(--line-3);
      border-radius: var(--r-lg) var(--r-lg) 0 0;
    }
  }
</style>
