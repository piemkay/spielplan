<script>
  // The gate is the server's: with the toggle off `/api/model-log` omits `events`, so nothing renders.
  import { eventTime, loadModelLog } from '$lib/home.svelte.js';
  import { dismiss } from '$lib/dismiss.js';

  let { open = false, onClose, suppressed = [] } = $props();

  let log = $state(null);
  let error = $state('');
  let filter = $state('');

  // Refetch on every open: the log is ephemeral.
  $effect(() => {
    // Cleared on close too: `{#if open}` paints before this effect runs, so a stale log would flash.
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

  // The shell's trigger toggles from outside this node, so its pointerdown must not dismiss.
  const OPENER = '[data-testid="model-rail-open"]';

  /** @param {Event} event */
  function dismissRail(event) {
    const target = event.target;
    if (event.type === 'pointerdown' && target instanceof Element && target.closest(OPENER)) return;
    onClose?.();
  }
</script>

{#if open}
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
        <div class="data">last 15 events · never saved</div>
      </div>
      <button class="close" onclick={onClose} aria-label="Close the model log" data-testid="model-rail-close">✕</button>
    </header>

    {#if error}
      <p class="data err" role="alert">{error}</p>
    {:else if log && !log.show_model}
      <!-- Reachable only for a moment: the opener sits behind the same preference. -->
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
            <li data-testid="model-rail-event" data-kind={e.kind}>
              <div class="meta data">
                <span class="kind" data-kind={e.kind}>{e.kind}</span>
                <span>{eventTime(e.at)}</span>
              </div>
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
        <!-- A shelf that cannot justify itself is absent (§6.0); this list tells that apart from a bug. -->
        <section class="suppressed">
          <div class="data heading">SHELVES THAT DID NOT SHIP</div>
          <ul>
            {#each suppressed as s, i (s.shelf + ':' + s.kind + ':' + i)}
              <li class="why" data-testid="model-rail-suppressed" data-shelf={s.shelf}>
                <!-- Joined in JS: Svelte collapses the whitespace around an {#if}, gluing the separator. -->
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
  /* Clear the installed app's status bar (viewport-fit=cover); the compact sheet needs none. */
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
  /* design.css's coarse floor sets min-height only; the width is for fingers, not for a mouse. */
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
  /* Kind colours borrow the facet palette: §6.8 ships eleven colours and one accent, no more. */
  .events li[data-kind='verdict'] { border-left-color: var(--facet-mood); }
  .events li[data-kind='duel'] { border-left-color: var(--facet-pacing); }
  .events li[data-kind='tier_edit'] { border-left-color: var(--facet-structure); }
  .events li[data-kind='not_seen'] { border-left-color: var(--facet-register); }
  .events li[data-kind='undo'] { border-left-color: var(--facet-place); }

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
