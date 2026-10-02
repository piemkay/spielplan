<script>
  // Like these films, the Filters panel's last cell (decision 559): on a phone a row that opens the
  // picker as a sheet, the recipe's films under it; from 721 px a field across the panel, its films
  // as chips and its matches dropping under it. In the cell a chip's tap switches like and less like.
  import { recipe, removeFilm, setSign } from '$lib/recipe.svelte.js';
  import FilmPicker from './FilmPicker.svelte';
  import Icon from './Icon.svelte';
  import RecipeChip from './RecipeChip.svelte';

  let width = $state(typeof window === 'undefined' ? 390 : window.innerWidth);
  const desktop = $derived(width > 720);
  let open = $state(false);
</script>

<svelte:window bind:innerWidth={width} />

{#if desktop}
  <div class="cell wide" data-testid="filter-like">
    <span class="label">Like these films</span>
    <FilmPicker inline />
  </div>
{:else}
  <div class="cell list-row phone">
    <span class="label" id="like-label">Like these films</span>
    <button
      class="add"
      id="like-add"
      aria-labelledby="like-label like-add"
      aria-haspopup="dialog"
      data-testid="filter-like"
      onclick={() => (open = true)}
    >Add<Icon name="chevron-right" size={16} /></button>
    {#if recipe.films.length}
      <div class="picked" role="group" aria-label="In the recipe">
        {#each recipe.films as f (f.id)}
          <RecipeChip
            film={f}
            onFlip={() => setSign(f.id, f.sign === 'like' ? 'less' : 'like')}
            onRemove={() => removeFilm(f.id)}
          />
        {/each}
      </div>
    {/if}
  </div>
  <FilmPicker {open} onClose={() => (open = false)} />
{/if}

<style>
  /* The row's first line opens the sheet; the chips under it keep their own taps (board C2). */
  .phone {
    position: relative;
    flex-wrap: wrap;
  }
  .phone > .label {
    min-height: 36px;
    display: flex;
    align-items: center;
  }
  .add {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 52px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    padding: 0 var(--gutter);
    border: none;
    border-radius: 0;
    background: none;
    color: var(--text-3);
    font: inherit;
    cursor: pointer;
  }
  .add:focus-visible {
    outline-offset: -2px;
  }
  .picked {
    flex: 1 0 100%;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 4px 0 6px;
  }
  /* Spans the panel's grid, as board C2 draws it; Home's own cells carry the same hairline. */
  .wide {
    grid-column: 1 / -1;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 16px 14px;
    box-shadow: -0.5px -0.5px 0 var(--separator);
  }
  .wide > .label {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
  }
</style>
