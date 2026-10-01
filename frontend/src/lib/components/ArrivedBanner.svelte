<script>
  // A wanted title that is in the library now. It stands until dismissed, or until the title is seen
  // or rated, which the server reads.
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import { dayMonth, dismissArrival } from '$lib/wish.svelte.js';

  let { arrived = [], onSelect } = $props();

  let gone = $state([]);
  let failure = $state('');

  async function dismiss(item) {
    failure = '';
    try {
      await dismissArrival(item.title_id);
      gone = [...gone, item.title_id];
    } catch (err) {
      failure = err.message;
    }
  }
</script>

{#each arrived.filter((a) => !gone.includes(a.title_id)) as item (item.title_id)}
  <div class="card arrived" role="status" data-testid="home-arrived" data-title={item.title_id}>
    <span class="thumb"><RatePoster title={item} showName={false} /></span>
    <div class="text">
      <p class="headline">{item.name} is here</p>
      <p class="footnote">On your wish list since {dayMonth(item.since)}. It's in the library now.</p>
      {#if failure}<p class="footnote" role="alert">{failure}</p>{/if}
    </div>
    {#if item.play_url}
      <a class="btn-primary act" href={item.play_url} target="_blank" rel="noreferrer">Play</a>
    {:else}
      <button class="btn-primary act" onclick={() => onSelect?.({ id: item.title_id, ...item })}>Open</button>
    {/if}
    <button class="dismiss" aria-label="Dismiss {item.name}" onclick={() => dismiss(item)}>
      <Icon name="close" size={18} />
    </button>
  </div>
{/each}

<style>
  .arrived {
    max-width: 560px;
    margin-bottom: 28px;
    padding: 12px 4px 12px 12px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .thumb {
    flex: none;
    width: 48px;
  }
  .thumb :global(.poster) {
    border-radius: var(--r-xs);
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .text p {
    margin: 0;
  }
  .headline {
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .footnote {
    color: var(--text-2);
  }
  .act {
    flex: none;
    min-height: 44px;
    padding: 0 14px;
    border-radius: var(--r-pill);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .dismiss {
    flex: none;
    width: 44px;
    height: 44px;
    border: none;
    background: none;
    color: var(--text-3);
    display: grid;
    place-items: center;
  }
</style>
