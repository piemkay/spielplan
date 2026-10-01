<script>
  // One term on the Taste chart (§6.5): its facet dot and label, a track with one marker per person,
  // and the films behind it as posters with no titles. Markers are told apart by their initials.
  import { facetColour } from '$lib/home.svelte.js';
  import { TRACK, markPair, markX, placeWord, strip } from '$lib/taste.svelte.js';
  import RatePoster from './RatePoster.svelte';

  /** @type {{ row: any, marks: {pos: number, initials: string, colour: string|null, who: string}[], onOpen: (film: any) => void }} */
  let { row, marks, onOpen } = $props();

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
  const words = $derived(
    `${row.label}. ${marks.map((m) => `${m.who}: ${placeWord(m.pos)}`).join('. ')}.`
  );
  const signed = (n) => `${n > 0 ? '+' : ''}${n.toFixed(2)}`;
</script>

<div class="trow" data-testid="taste-term" data-term={row.term}>
  <p class="sr-only">{words}</p>
  <div class="head" aria-hidden="true">
    <span class="dot" style:background={facetColour(row.facet)}></span>
    <span class="label">{row.label}</span>
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
          <span class="plus"><span aria-hidden="true">+{posters.plus}</span><span class="sr-only">and {posters.plus} more</span></span>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .trow {
    position: relative;
    padding: 10px var(--gutter);
  }
  .trow:not(:first-child) {
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  .head {
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
  .track > .mark {
    top: 2px;
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
  /* Four slots wide whatever a row holds, so the track is one width down the chart. */
  .posters {
    flex: none;
    width: 162px;
    display: flex;
    justify-content: flex-end;
    gap: 6px;
  }
  .thumb {
    position: relative;
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
    flex: none;
    display: grid;
    place-items: center;
    width: 36px;
    height: 54px;
    border-radius: var(--r-xs);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--fs-footnote);
    line-height: 18px;
  }
</style>
