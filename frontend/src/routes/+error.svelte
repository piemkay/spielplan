<script>
  // Reached by a client-router 404 (the SPA fallback) or a chunk that fails to import without a
  // deploy; SvelteKit's built-in page would be a dead end in the standalone PWA.
  import '$lib/design.css';
  import { page } from '$app/stores';

  const missing = $derived($page.status === 404);
</script>

<div class="page">
  <section class="fail" data-testid="app-error">
    <h1 class="title-1">{missing ? 'There is nothing here' : 'That screen did not load'}</h1>
    <p class="why">
      {missing
        ? 'This address is not a screen in Spielplan.'
        : 'Reloading usually fixes it — the app may have just been updated.'}
    </p>
    <div class="doors">
      <button class="btn-primary" onclick={() => location.reload()}>Reload</button>
      <a class="btn-secondary" href="/">Home</a>
    </div>
    <p class="footnote">
      Reload if you were in the middle of something, Home if you followed a link or typed the
      address.
    </p>
    <p class="footnote">
      Error {$page.status}{!missing && $page.error?.message ? ` · ${$page.error.message}` : ''}
    </p>
  </section>
</div>

<style>
  .page {
    display: grid;
    place-items: center;
    padding: 48px 0;
  }
  .fail {
    width: min(420px, 100%);
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  .fail p {
    margin: 0;
  }
  .doors {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 8px;
  }
  .doors > * {
    border-radius: var(--r-md);
  }
</style>
