<script>
  // §6.1's shelves, wherever they open (decision 551): one button per tier, best first, each with
  // the person's own films most like the one being placed. A tap places; a hold opens the held
  // shelf larger, sliding moves between shelves and release places (decision 545). No letter.
  import { haptic } from '$lib/motion.js';
  import { displayNames } from '$lib/titleCard.js';
  import RatePoster from './RatePoster.svelte';

  // `lit`: the tier just chosen, drawn in ember with the film joining it; `current`: where the
  // film already sits (the card's sheet).
  let {
    shelves = [],
    name = '',
    film = null,
    kind = 'movie',
    lit = null,
    current = null,
    busy = false,
    testid = 'rate-shelf',
    held = $bindable(-1),
    onPlace
  } = $props();

  const HOLD_MS = 250;
  const SLOP_PX = 10;

  let list = $state();
  let pressed = $state(-1);
  /** @type {null | {id: number, x0: number, y0: number, y: number, look: boolean, timer: any}} */
  let grip = null;
  // A release that placed is followed by the button's own click, which must not place again.
  let eatUntil = 0;

  const nameOf = (f) => displayNames(f).primary;
  function joined(names) {
    return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
  }
  const label = (s) => (s.films?.length ? `${s.word}, with ${joined(s.films.map(nameOf))}` : s.word);
  const postersOf = (s) => {
    const films = s.films ?? [];
    return (lit === s.tier && film ? [film, ...films.filter((f) => f.id !== film.id)] : films).slice(0, 4);
  };
  const countLine = (n) => `${n} ${kind === 'series' ? 'series' : n === 1 ? 'film' : 'films'}`;
  // The larger view opens away from the finger: under the top shelves, over them for the rest.
  const split = $derived(Math.floor(shelves.length / 2));

  function choose(tier) {
    if (busy || lit != null) return;
    onPlace?.(tier);
  }

  function tap(i) {
    if (Date.now() < eatUntil || grip?.look) return;
    choose(shelves[i].tier);
  }

  function indexAt(y) {
    const rows = [...list.querySelectorAll('[data-tier]')];
    const i = rows.findIndex((row) => y < row.getBoundingClientRect().bottom);
    return i < 0 ? rows.length - 1 : i;
  }

  function down(event) {
    if (busy || lit != null || event.button > 0) return;
    clearTimeout(grip?.timer);
    const el = event.currentTarget;
    const { pointerId: id, clientX: x0, clientY: y0 } = event;
    const g = { id, x0, y0, y: y0, look: false, timer: null };
    g.timer = setTimeout(() => look(g, el), HOLD_MS);
    grip = g;
    pressed = indexAt(event.clientY);
  }

  function look(g, el) {
    if (grip !== g || g.look) return;
    g.look = true;
    clearTimeout(g.timer);
    try {
      el.setPointerCapture?.(g.id);
    } catch {
      // A pointer already released has nothing to capture; its up event still arrives.
    }
    pressed = -1;
    held = indexAt(g.y);
  }

  function move(event) {
    const g = grip;
    if (!g || event.pointerId !== g.id) return;
    g.y = event.clientY;
    if (!g.look) {
      if (Math.hypot(event.clientX - g.x0, event.clientY - g.y0) >= SLOP_PX) look(g, event.currentTarget);
      return;
    }
    const i = indexAt(event.clientY);
    if (i === held) return;
    held = i;
    haptic();
  }

  function up(event) {
    const g = grip;
    if (!g || event.pointerId !== g.id) return;
    grip = null;
    clearTimeout(g.timer);
    pressed = -1;
    if (!g.look) return;
    eatUntil = Date.now() + 400;
    const i = held;
    held = -1;
    const box = list.getBoundingClientRect();
    const { clientX: x, clientY: y } = event;
    if (x >= box.left && x <= box.right && y >= box.top && y <= box.bottom && i >= 0) choose(shelves[i].tier);
  }

  function cancel(event) {
    if (!grip || (event && event.pointerId !== grip.id)) return;
    clearTimeout(grip.timer);
    grip = null;
    pressed = -1;
    held = -1;
  }
</script>

<div class="shelves">
  <div
    class="list"
    class:deciding={lit != null}
    class:looking={held >= 0}
    role="group"
    aria-label="Where does {name} sit?"
    bind:this={list}
    onpointerdown={down}
    onpointermove={move}
    onpointerup={up}
    onpointercancel={cancel}
  >
    {#each shelves as s, i (s.tier)}
      <button
        class="shelf"
        class:lit={lit === s.tier}
        class:lifted={pressed === i || held === i}
        data-testid={testid}
        data-tier={s.tier}
        aria-label={label(s)}
        aria-current={current === s.tier ? 'true' : undefined}
        onclick={() => tap(i)}
      >
        {#each postersOf(s) as f (f.id)}
          <span class="p" style:--i={i}><RatePoster title={f} showName={false} /></span>
        {/each}
        <span class="word">{s.word}</span>
      </button>
    {/each}
  </div>

  {#if held >= 0}
    {@const s = shelves[held]}
    <div
      class="look"
      aria-hidden="true"
      style:top={held < split ? `calc(${split} * var(--pitch) + 8px)` : null}
      style:bottom={held < split ? null : `calc(100% - ${split} * var(--pitch) + 8px)`}
    >
      <div class="look-head">
        <span class="look-word">{s.word}</span>
        <span class="look-count">{countLine(s.count)}</span>
      </div>
      <div class="look-grid">
        {#each (s.films ?? []).slice(0, 4) as f (f.id)}
          <div class="cell">
            <span class="big"><RatePoster title={f} showName={false} /></span>
            <span class="cell-name">{nameOf(f)}</span>
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  /* A shelf poster's height; the Rate page sets a larger one on a desktop. */
  .shelves {
    --ph: var(--shelf-ph, 64px);
    --pitch: calc(var(--ph) + 8px);
    position: relative;
  }
  .list {
    display: flex;
    flex-direction: column;
    border-radius: 12px;
    overflow: hidden;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .shelf {
    position: relative;
    width: 100%;
    height: var(--pitch);
    min-height: var(--pitch);
    padding: 0 0 0 4px;
    border: none;
    border-radius: 0;
    background: var(--surface-1);
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 4px;
    text-align: left;
    -webkit-tap-highlight-color: transparent;
    transition-property: background-color, opacity;
    transition-duration: var(--dur-quick);
    transition-timing-function: var(--ease);
  }
  .shelf + .shelf {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .p {
    flex: none;
    width: calc(var(--ph) * 11 / 16);
    height: var(--ph);
    animation: fadeIn var(--dur-quick) var(--ease) calc(var(--i) * 24ms) both;
  }
  .p :global(.poster) {
    height: 100%;
    aspect-ratio: auto;
    border-radius: var(--r-xs);
  }
  .word {
    margin-left: auto;
    min-width: 0;
    padding: 0 16px 0 8px;
    font-size: var(--fs-subhead);
    line-height: 20px;
    color: var(--text-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .shelf[aria-current='true'] .word,
  .lifted .word {
    color: var(--text);
    font-weight: 600;
  }
  @media (hover: hover) {
    .list:not(.deciding, .looking) .shelf:hover:not(.lifted) {
      background: var(--surface-2);
    }
    .list:not(.deciding, .looking) .shelf:hover .word {
      color: var(--text);
    }
  }
  .lifted {
    background: var(--surface-3);
    transition-duration: 0ms;
  }
  /* The chosen shelf in ember with dark text, the film joining it; the rest step back. */
  .shelf.lit {
    background: var(--accent);
    transition-duration: 0ms;
    animation: press 180ms var(--ease);
  }
  .lit .word {
    color: var(--on-accent);
    font-weight: 600;
  }
  .lit .p:first-child {
    animation: fadeIn var(--dur-quick) var(--ease) 60ms both;
  }
  .deciding .shelf:not(.lit) {
    opacity: 0.4;
    transition-delay: 90ms;
  }
  .looking .shelf:not(.lifted) {
    opacity: 0.4;
  }
  @keyframes press {
    from {
      filter: brightness(0.85);
    }
  }
  .look {
    position: absolute;
    left: 0;
    right: 0;
    z-index: 2;
    padding: 16px;
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: 0 0 0 8px var(--bg);
    pointer-events: none;
    animation: fadeIn 90ms var(--ease) both;
  }
  .look-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .look-word {
    min-width: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .look-count {
    flex: none;
    margin-left: auto;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .look-grid {
    margin-top: 12px;
    min-height: calc(var(--ph) * 113 / 64 + 20px);
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    column-gap: 8px;
  }
  .cell {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .big :global(.poster) {
    height: calc(var(--ph) * 113 / 64);
    aspect-ratio: auto;
  }
  .cell-name {
    font-size: var(--fs-caption);
    line-height: 16px;
    color: var(--text-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
