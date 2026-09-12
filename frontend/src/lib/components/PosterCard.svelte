<script module>
  /**
   * §8 stage 10's question — is this title placed with no crowd data behind it? — asked once.
   *
   * Exported because the badge and the sentence that explains the badge are now in two places:
   * the card wears the chip and the shelf carries the one-line why for the whole row, since a
   * `title=` tooltip does not exist on the form factor §6 makes primary (M4.15 finding 10,
   * decision 278). A second spelling of this test is the drift `toPosterTitle`'s comment already
   * records — 111 of 130 badges were false the one time these fields were dropped in transit — so
   * there is one copy of it and both readers call it.
   */
  export function isColdPlaced(title) {
    return title.e_source
      ? title.e_source === 'cold_tower'
      : title.item_n === 0 || (title.item_n == null && title.placement === 'cold_tower');
  }
</script>

<script>
  /**
   * Poster-forward 2:3 card (spec §6.8). There are no poster images in the corpus bundle —
   * only paths — so until an image source is configured the card renders a deterministic
   * tinted panel derived from the title's own id. It is a placeholder that is stable across
   * reloads, which matters: a card that changes colour every render reads as a bug.
   */
  // One runtime label, not three. This copy and the title card's had drifted apart — the card
  // two taps away had no kind branch at all — so the same episode read `24m/ep` here and
  // `0h 24m` there. [M4.9 finding 37]
  import { runtimeLabel } from '$lib/rate.svelte.js';

  let { title, onSelect } = $props();

  // A stable hue per title. FNV-1a over the name, so the same film is the same colour
  // everywhere it appears.
  function hue(text) {
    let x = 2166136261;
    for (let i = 0; i < text.length; i++) {
      x ^= text.charCodeAt(i);
      x = Math.imul(x, 16777619);
    }
    return (x >>> 0) % 360;
  }

  const h = $derived(hue(title.name ?? String(title.id)));
  const minutes = $derived(runtimeLabel(title));
  // Built in JS, not in markup: Svelte collapses the whitespace around an {#if} block, which
  // turned "1995 · 2h 50m" into "1995· 2h 50m".
  const meta = $derived([title.year ?? '—', minutes].filter(Boolean).join(' · '));
  // §8 stage 10's badge. `e_source` comes from `title_prior` where the payload has it; where it
  // does not, fall back to the placement stamp rather than silently badging nothing.
  const noCrowdData = $derived(isColdPlaced(title));
</script>

<button class="card-wrap" onclick={onSelect} title={title.name}>
  <div
    class="poster"
    style:background="linear-gradient(150deg, hsl({h} 22% 17%), hsl({(h + 40) % 360} 18% 11%))"
  >
    {#if noCrowdData}
      <!-- §8 stage 10: "new — model placement, no crowd data" until ratings accrue.

           Off `e_source`/`item_n`, NOT off `title.placement`. Those stopped being the same
           question when warm was defined from §5.1's gate: a title with 55 crowd ratings and a
           Backbone row is still placed by the Cold Tower, because 15% of its coordinate comes
           from there — and it would have worn a chip reading "no crowd data yet" on every
           poster in the library. `e_source = 'cold_tower'` is the honest test: no Backbone row
           at all.

           The sentence behind the chip belongs to the SURFACE, carried once rather than on every
           card (§6.8's quiet reasons; decision 278): `ShelfRow` puts it in the row's header, and
           Home's catalog grid — the other place this card renders, and one with no shelf header
           in it — puts it above the grid. The tooltip stays for the pointer, which is the one
           device that can ask an individual card; it is no longer the only answer anywhere.
           [review cycle 2: M415-C2-COMP-06] -->
      <span class="badge data" title="placed by the Cold Tower — no crowd data yet">new</span>
    {/if}
    {#if title.seen_state === 'seen'}
      <span class="seen data">seen</span>
    {/if}
  </div>
  <div class="meta">
    <div class="name">{title.name}</div>
    <div class="data">{meta}</div>
  </div>
</button>

<style>
  .card-wrap {
    display: flex;
    flex-direction: column;
    gap: 7px;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    text-align: left;
    color: inherit;
  }
  .card-wrap:hover .poster {
    border-color: var(--ember-edge);
  }
  .poster {
    /* design.css's `.poster` already sets the frame — aspect ratio, radius, border,
       positioning and clipping. Only what is this component's own belongs here. */
    transition: border-color 0.12s ease;
  }
  .badge,
  .seen {
    position: absolute;
    top: 7px;
    padding: 2px 7px;
    border-radius: var(--r-pill);
    font-size: 9px;
    letter-spacing: 0.06em;
  }
  /* A status the title carries, not a choice the household made, so it is not the accent: §6.8
     spends the ember on selection and primary actions, and a chip that means "no crowd data"
     competed with the one selected thing on every screen it appeared on. `--status` is opaque
     because this is drawn over a poster, where an alpha fill lets the artwork decide the
     contrast; `--ground` on it measures 9.4:1. [§6.8; decision 276] */
  .badge {
    left: 7px;
    background: var(--status);
    color: var(--ground);
  }
  .seen {
    right: 7px;
    background: rgba(13, 13, 15, 0.72);
    color: var(--ink-3);
  }
  .name {
    font-size: 12.5px;
    line-height: 1.25;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
</style>
