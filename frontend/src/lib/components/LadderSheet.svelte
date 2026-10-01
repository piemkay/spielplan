<script>
  // §6.3 Without a drag: Rate's shelves for one title, as a short sheet over its card. The frames
  // and words draw at once from the card's tiers; the films land with `GET /api/rate/shelves`.
  import { page } from '$app/state';
  import { get } from '$lib/api.js';
  import { ms } from '$lib/motion.js';
  import { displayNames } from '$lib/titleCard.js';
  import RateShelves from './RateShelves.svelte';
  import Sheet from './Sheet.svelte';

  // `tiers`: the card's ranking tiers ({index, label, word, count}); `current`: the placed index.
  // `onPlace(tier)` moves the title and resolves once it has.
  let { title, tiers = [], current = null, onPlace, onClose } = $props();

  // The placed shelf stays lit this long before the sheet closes, as on Rate.
  const COMMIT_MS = 240;

  let open = $state(true);
  let shelves = $state(null);
  let lit = $state(null);
  // This sheet's place in the stack: the keys answer only while it is the top one.
  const depth = (page.state?.sheets ?? []).length + 1;

  $effect(() => {
    let cancelled = false;
    get(`/rate/shelves?title_id=${title.id}`)
      .then((res) => {
        if (!cancelled) shelves = res?.shelves ?? null;
      })
      .catch(() => {
        // The frames stand without their films and still place.
      });
    return () => {
      cancelled = true;
    };
  });

  const best = $derived([...tiers].sort((a, b) => b.index - a.index));
  const shown = $derived(
    shelves ?? best.map((t) => ({ tier: t.index, word: t.word, count: t.count ?? 0, films: [] }))
  );
  const name = $derived(displayNames(title).primary);
  const standing = $derived(best.find((t) => t.index === current)?.word ?? 'Not placed yet');

  async function choose(index, close) {
    if (lit != null) return;
    lit = index;
    const wait = ms(COMMIT_MS);
    if (wait) await new Promise((done) => setTimeout(done, wait));
    close();
    await onPlace(best.find((t) => t.index === index));
  }

  // The sheet's own close, so a key closes it the way Done does.
  let closeSheet = null;
  function onKey(event) {
    if ((page.state?.sheets ?? []).length !== depth || !closeSheet) return;
    if (event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
    if (!/^[1-9]$/.test(event.key)) return;
    const shelf = shown[Number(event.key) - 1];
    if (!shelf) return;
    event.preventDefault();
    choose(shelf.tier, closeSheet);
  }

  function closed() {
    open = false;
    onClose?.();
  }
</script>

<svelte:window onkeydown={onKey} />

<Sheet {open} onClose={closed} label="In your ranking" detent="fit" width={440}>
  {#snippet children(close)}
    <div data-testid="ladder-sheet">
      <div class="bar" {@attach () => void (closeSheet = close)}>
        <div class="who">
          <h2>{name}</h2>
          <p class="footnote">{standing}</p>
        </div>
        <button class="btn-plain done" data-testid="ladder-done" onclick={close}>Done</button>
      </div>
      <RateShelves
        shelves={shown}
        {name}
        film={title}
        kind={title.kind}
        {lit}
        {current}
        testid="ladder-shelf"
        onPlace={(tier) => choose(tier, close)}
      />
    </div>
  {/snippet}
</Sheet>

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 44px;
    margin: 0 -8px 12px 0;
  }
  .who {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  h2 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  p {
    margin: 0;
  }
  .done {
    flex: none;
    font-weight: 600;
  }
</style>
