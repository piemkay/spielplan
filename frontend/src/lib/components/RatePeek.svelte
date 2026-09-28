<script>
  // "About this film" (decision 528): a look at a title on Rate that never answers for it. The
  // card's own title draws the lead at once; the rest arrives with `GET /api/titles/{id}`.
  import { get } from '$lib/api.js';
  import { metaLine } from '$lib/rate.svelte.js';
  import { directedBy, genreLine } from '$lib/titleCard.js';
  import Headshot from '$lib/components/Headshot.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import RatePoster from '$lib/components/RatePoster.svelte';
  import Sheet from '$lib/components/Sheet.svelte';

  let { title, busy = false, onNotSeen = null, onClose } = $props();

  let open = $state(true);
  let data = $state(null);
  let error = $state('');

  $effect(() => {
    let cancelled = false;
    get(`/titles/${title.id}`)
      .then((res) => {
        if (!cancelled) data = res;
      })
      .catch((err) => {
        if (!cancelled) error = err.message;
      });
    return () => {
      cancelled = true;
    };
  });

  const meta = $derived([metaLine(title), genreLine(data?.genres)].filter(Boolean).join(' · '));
  const director = $derived(directedBy(data?.credits));
  const cast = $derived((data?.credits ?? []).filter((c) => c.role_class === 'cast').slice(0, 4));

  function closed() {
    open = false;
    onClose?.();
  }
</script>

<Sheet {open} onClose={closed} label="About this film" detent="fit" width={440}>
  {#snippet children(close)}
    <div class="bar">
      <h2>About this film</h2>
      <button class="btn-plain done" onclick={close}>Done</button>
    </div>
    <div class="peek" data-testid="rate-peek">
      <div class="lead">
        <div class="art"><RatePoster {title} showName={false} /></div>
        <div class="head">
          <h3 class="name">{title.name ?? '—'}</h3>
          <p class="footnote">{meta}</p>
          {#if director}<p class="footnote">{director}</p>{/if}
        </div>
      </div>
      {#if error}
        <p class="footnote" role="alert">{error}</p>
      {:else if data?.title?.overview}
        <p class="why overview">{data.title.overview}</p>
      {/if}
      {#if cast.length}
        <section class="cast" aria-labelledby="rate-peek-cast">
          <h3 id="rate-peek-cast">Cast</h3>
          <ul>
            {#each cast as c (c.person_id)}
              <li>
                <Headshot credit={c} />
                <span class="who">{c.name}</span>
                {#if c.character}<span class="role">{c.character}</span>{/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}
      <!-- Absent for a title already known seen: the one being placed on Rank. -->
      {#if onNotSeen}
        <button
          class="btn-secondary unseen"
          data-testid="rate-peek-not-seen"
          aria-label="Not seen: {title.name ?? ''}"
          disabled={busy}
          onclick={() => {
            close();
            onNotSeen();
          }}
        ><Icon name="eye-off" size={20} />Not seen</button>
      {/if}
      <p class="footnote note">Looking never counts as an answer.</p>
    </div>
  {/snippet}
</Sheet>

<style>
  .bar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    min-height: 44px;
    margin: 0 -8px;
  }
  h2 {
    grid-column: 2;
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .done {
    justify-self: end;
    font-weight: 600;
  }
  .peek {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding-top: 4px;
  }
  p {
    margin: 0;
  }
  .lead {
    display: flex;
    align-items: flex-end;
    gap: 16px;
  }
  .art {
    flex: none;
    width: 80px;
  }
  .head {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .name {
    margin: 0 0 4px;
    font-family: var(--serif);
    font-weight: 400;
    font-size: 24px;
    line-height: 28px;
    text-wrap: balance;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .head .footnote {
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .overview {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .cast {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .cast h3 {
    margin: 0;
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 4px;
    align-items: start;
  }
  li {
    --face: 56px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    font-size: var(--fs-caption);
    line-height: 16px;
  }
  .who {
    margin-top: 8px;
    font-weight: 600;
  }
  .who,
  .role {
    max-width: 100%;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .role {
    color: var(--text-3);
  }
  .unseen {
    width: 100%;
    margin-top: 8px;
    border-radius: var(--r-md);
  }
  .note {
    margin-top: -8px;
    text-align: center;
  }
</style>
