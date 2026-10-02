<script>
  // The recipe's films, first in Home's chip row with the Filters open or shut (decision 560).
  import { recipe, removeFilm } from '$lib/recipe.svelte.js';
  import RecipeChip from './RecipeChip.svelte';
  import RecipeGroupSheet from './RecipeGroupSheet.svelte';

  let openId = $state(null);
  let anchor = $state(null);

  const films = $derived(recipe.films);
  const film = $derived(films.find((f) => f.id === openId) ?? null);

  function toggle(id, el) {
    anchor = el;
    openId = openId === id ? null : id;
  }
</script>

{#each films as f (f.id)}
  <RecipeChip
    film={f}
    expanded={openId === f.id}
    onOpen={(el) => toggle(f.id, el)}
    onRemove={() => removeFilm(f.id)}
  />
{/each}
<RecipeGroupSheet open={film !== null} {film} {films} {anchor} onClose={() => (openId = null)} />
