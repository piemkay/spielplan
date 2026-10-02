<script>
  // A wanted title that is in the library now. It stands until dismissed, or until the title is seen
  // or rated, which the server reads. Its x is for good, with Undo on the toast (decision 554).
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { showToast } from '$lib/toast.svelte.js';
  import { dayMonth, dismissArrival, restoreArrival } from '$lib/wish.svelte.js';

  let { arrived = [], onSelect } = $props();

  let gone = $state([]);
  let failure = $state('');

  async function dismiss(item) {
    failure = '';
    try {
      await dismissArrival(item.title_id);
      gone = [...gone, item.title_id];
      showToast('Removed', { label: 'Undo', run: () => restore(item) });
    } catch (err) {
      failure = err.message;
    }
  }

  async function restore(item) {
    try {
      await restoreArrival(item.title_id, item.since);
      gone = gone.filter((id) => id !== item.title_id);
    } catch (err) {
      showToast(err.message);
    }
  }
</script>

{#each arrived.filter((a) => !gone.includes(a.title_id)) as item (item.title_id)}
  <div class="notice-bar" role="status" data-testid="home-arrived" data-title={item.title_id}>
    <div class="thumb" aria-hidden="true"><RatePoster title={item} showName={false} /></div>
    <div class="text">
      <p class="headline">{item.name} is here</p>
      {#if failure}
        <p class="line failure" role="alert">{failure}</p>
      {:else}
        <p class="line">On your wish list since {dayMonth(item.since)}. It's in the library now.</p>
      {/if}
    </div>
    <div class="act">
      {#if item.play_url}
        <a class="pill" href={item.play_url} target="_blank" rel="noreferrer">Play</a>
      {:else}
        <button class="pill" onclick={() => onSelect?.({ id: item.title_id, ...item })}>Open</button>
      {/if}
    </div>
    <button class="x" aria-label="Dismiss {item.name}" onclick={() => dismiss(item)}>
      <Icon name="close" size={18} />
    </button>
  </div>
{/each}
