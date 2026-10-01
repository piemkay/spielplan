<script>
  // One term on the Taste chart (§6.5): its facet dot and label, a track with one marker per person,
  // and the films behind it as posters with no titles. Markers are told apart by their initials.
  // Given `expand`, the row and its "+N" open in place onto every film behind the term, named.
  import { facetColour } from '$lib/home.svelte.js';
  import { sentenceCase } from '$lib/rate.svelte.js';
  import { TRACK, markPair, markX, placeWord, strip } from '$lib/taste.svelte.js';
  import RatePoster from './RatePoster.svelte';

  /** @type {{ row: any, marks: {pos: number, initials: string, colour: string|null, who: string}[], onOpen: (film: any) => void, expand?: ((term: string) => Promise<{head: string, films: any[]}[]>) | null, none?: string, wide?: boolean, line?: boolean }} */
  let { row, marks, onOpen, expand = null, none = '', wide = false, line = false } = $props();

  // The track narrows on a narrow phone; every row's track is the same width, so rows compare.
  let measured = $state(0);
  const width = $derived(measured || TRACK);
  const xs = $derived(
    marks.length === 2
      ? markPair(marks[0].pos, marks[1].pos, width)
      : marks.map((m) => markX(m.pos, width))
  );
  // One marker reads from the middle; two read between each other, unless they had to part.
  const span = $derived.by(() => {
    const [from, to] = xs.length === 2 ? xs : [width / 2, xs[0]];
    const parted =
      xs.length === 2 && Math.abs(markX(marks[0].pos, width) - markX(marks[1].pos, width)) < 20;
    return parted ? null : { left: Math.min(from, to), width: Math.abs(to - from) };
  });
  const posters = $derived(strip(row.films ?? [], row.more ?? 0));
  const label = $derived(sentenceCase(row.label));
  const words = $derived(
    `${label}. ${marks.map((m) => `${m.who}: ${placeWord(m.pos)}`).join('. ')}.`
  );
  const signed = (n) => `${n > 0 ? '+' : ''}${n.toFixed(2)}`;

  let open = $state(false);
  // Kept with the row it was read for, so a re-read of the chart reads an open row again.
  let panel = $state.raw(null);
  const groups = $derived(panel?.row === row ? panel.groups : null);

  $effect(() => {
    if (!open || !expand || groups) return;
    const asked = row;
    expand(asked.term).then(
      (read) => (panel = { row: asked, groups: read }),
      (err) => (panel = { row: asked, groups: [], error: err.message })
    );
  });
</script>

{#snippet markers(list)}
  {#each list as m, i (i)}
    <span class="mark" style:background={m.colour ?? 'var(--thumb)'}>{m.initials}</span>
  {/each}
{/snippet}

<div class="trow" class:wide class:line data-testid="taste-term" data-term={row.term}>
  <div class="face">
    {#if expand}
      <button class="toggle" aria-expanded={open} onclick={() => (open = !open)}>
        <span class="sr-only">{words} Show the films</span>
      </button>
    {:else}
      <p class="sr-only">{words}</p>
    {/if}
    <div class="head" aria-hidden="true">
      <span class="dot" style:background={facetColour(row.facet)}></span>
      <span class="label">{label}</span>
      {#if row.model}
        <span class="data">d {signed(row.model.d)} · {row.model.carriers} carriers</span>
      {/if}
    </div>
    <div class="body">
      <span class="track" aria-hidden="true" bind:clientWidth={measured}>
        <span class="rule"></span>
        <span class="tick start"></span>
        <span class="tick end"></span>
        {#if span}<span class="span" style:left="{span.left}px" style:width="{span.width}px"></span>{/if}
        <span class="middle"></span>
        {#each marks as m, i}
          <span class="mark" style:left="{xs[i] - 10}px" style:background={m.colour ?? 'var(--thumb)'}
            >{m.initials}</span
          >
        {/each}
      </span>
      {#if posters.shown.length}
        <div class="posters">
          {#each posters.shown as film (film.id)}
            <button class="thumb press" aria-label="About {film.name}" onclick={() => onOpen(film)}>
              <RatePoster title={film} showName={false} lazy />
            </button>
          {/each}
          {#if posters.plus}
            <button class="plus press" aria-expanded={open} onclick={() => (open = !open)}>
              <span aria-hidden="true">+{posters.plus}</span>
              <span class="sr-only">{posters.plus} more. Show the films</span>
            </button>
          {/if}
        </div>
      {/if}
    </div>
  </div>
  {#if open && groups}
    <div class="panel" data-testid="taste-films">
      {#each groups as g (g.head)}
        <div class="films">
          <p class="ghead"><span class="marks" aria-hidden="true">{@render markers(marks)}</span>{g.head}</p>
          <div class="strip" data-nobar>
            {#each g.films as film (film.id)}
              <button class="thumb press" aria-label="About {film.name}" onclick={() => onOpen(film)}>
                <RatePoster title={film} showName={false} lazy />
              </button>
            {/each}
          </div>
          <p class="names" aria-hidden="true">{g.films.map((f) => f.name).join(' · ')}</p>
        </div>
      {:else}
        <p class="names" role={panel.error ? 'alert' : null}>{panel.error ?? none}</p>
      {/each}
    </div>
  {/if}
</div>

<style>
  .trow:not(:first-child) {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .face {
    position: relative;
    padding: 10px var(--gutter);
  }
  /* The whole face opens the row; the label and track let taps through, the posters keep theirs. */
  .toggle {
    position: absolute;
    inset: 0;
    width: 100%;
    padding: 0;
    border: none;
    background: none;
    transition: background var(--dur-base) var(--ease);
  }
  .toggle:active {
    background: var(--surface-2);
    transition-duration: 0s;
  }
  .toggle:focus-visible {
    outline-offset: -2px;
  }
  .head {
    position: relative;
    pointer-events: none;
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    height: 22px;
  }
  .dot {
    flex: none;
    width: 6px;
    height: 6px;
    border-radius: var(--r-pill);
  }
  .label {
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  .head .data {
    flex: none;
    margin-left: auto;
  }
  .body {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 54px;
    margin-top: 6px;
  }
  .track {
    position: relative;
    pointer-events: none;
    flex: none;
    width: clamp(96px, calc(100% - 174px), 152px);
    height: 24px;
  }
  .track > span {
    position: absolute;
    border-radius: 1px;
  }
  .rule {
    left: 0;
    top: 11px;
    width: 100%;
    height: 2px;
    background: var(--progress-track);
  }
  .tick {
    top: 7px;
    width: 2px;
    height: 10px;
    background: var(--progress-track);
  }
  .tick.start {
    left: 0;
  }
  .tick.end {
    right: 0;
  }
  .span {
    top: 11px;
    height: 2px;
    background: var(--text-3);
  }
  .middle {
    left: calc(50% - 1px);
    top: 4px;
    width: 2px;
    height: 16px;
    background: var(--text-2);
  }
  .mark {
    flex: none;
    width: 20px;
    height: 20px;
    display: grid;
    place-items: center;
    border-radius: var(--r-pill);
    box-shadow: 0 0 0 2px var(--surface-1);
    color: #fff;
    font-size: var(--fs-caption);
    line-height: 1;
    font-weight: 600;
  }
  .track > .mark {
    top: 2px;
    border-radius: var(--r-pill);
  }
  /* Four slots wide whatever a row holds, so the track is one width down the chart. */
  .posters {
    position: relative;
    pointer-events: none;
    flex: none;
    width: 162px;
    display: flex;
    justify-content: flex-end;
    gap: 6px;
  }
  .thumb {
    position: relative;
    pointer-events: auto;
    flex: none;
    width: 36px;
    min-height: 0;
    padding: 0;
    border: none;
    background: none;
  }
  .thumb :global(.poster) {
    border-radius: var(--r-xs);
  }
  .thumb::after {
    content: '';
    position: absolute;
    inset: 0 -3px;
  }
  .plus {
    position: relative;
    pointer-events: auto;
    flex: none;
    display: grid;
    place-items: center;
    width: 36px;
    height: 54px;
    padding: 0;
    border: none;
    border-radius: var(--r-xs);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
  .panel {
    padding: 4px var(--gutter) 14px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .ghead {
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--fs-footnote);
    line-height: 20px;
    color: var(--text-2);
  }
  .marks {
    flex: none;
    display: flex;
  }
  .marks > .mark + .mark {
    margin-left: -6px;
  }
  .strip {
    margin: 4px calc(-1 * var(--gutter)) 0;
    padding: 4px var(--gutter);
    display: flex;
    gap: 12px;
    overflow-x: auto;
  }
  .names {
    margin: 2px 0 0;
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
  /* A desktop row: the track takes the width the posters leave, and the posters take their desktop size. */
  @media (min-width: 721px) {
    .track {
      width: calc(100% - 208px);
    }
    .posters {
      width: 184px;
      gap: 8px;
    }
    .posters > .thumb,
    .plus {
      width: 40px;
    }
    .plus {
      height: 60px;
    }
  }
  @media (min-width: 1100px) {
    /* Compare's two columns (TasteChartDesktop). */
    .wide .track {
      width: clamp(96px, calc(100% - 196px), 176px);
    }
    /* A list the full width of the page: the label, the track and the posters on one line. */
    .line .face {
      display: grid;
      grid-template-columns: 236px minmax(0, 1fr);
      column-gap: 24px;
      align-items: center;
      padding: 8px var(--gutter);
    }
    .line .head {
      flex-wrap: wrap;
      row-gap: 0;
      height: auto;
    }
    .line .head .data {
      flex-basis: 100%;
      margin-left: 14px;
    }
    .line .body {
      margin-top: 0;
    }
    .line .panel {
      padding-left: calc(var(--gutter) + 260px);
    }
  }
</style>
