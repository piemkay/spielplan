<script>
  // Reached by a client-router 404 (the SPA fallback) or a chunk that fails to import without a
  // deploy; SvelteKit's built-in page would be a dead end in the standalone PWA.
  import '$lib/design.css';
  import { page } from '$app/stores';
</script>

<div class="page">
  <div class="card fail" data-testid="app-error">
    <span class="data">ERROR {$page.status}</span>
    <h1>That screen did not load</h1>
    <p class="why">{$page.error?.message || 'No reason was given.'}</p>
    <div class="doors">
      <button class="btn-primary" onclick={() => location.reload()}>Reload</button>
      <a class="btn-ghost" href="/">Home</a>
    </div>
    <p class="why">
      Reload if you were in the middle of using the app; Home if you followed a link or typed the
      address.
    </p>
  </div>
</div>

<style>
  .page {
    display: grid;
    place-items: center;
    padding: 24px;
  }
  .fail {
    width: min(420px, 100%);
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
  }
  .doors {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  /* Centre the label in the 48px box, and no accent on a door out of an error page (§6.8). */
  .doors a {
    display: inline-flex;
    align-items: center;
    color: var(--ink-3);
  }
</style>
