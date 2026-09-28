<script>
  // The list comes from `/auth/me`: which surfaces a user sees is a server decision, never a
  // client-side role check. A translucent tab bar on a phone, a sidebar on a desktop (decision 527),
  // and an icon rail between the two (decision 528).
  import { page } from '$app/stores';
  import { session } from '$lib/session.svelte.js';

  const ICONS = {
    home: ['M3.5 10.5 12 4l8.5 6.5V20a1 1 0 0 1-1 1H15v-6H9v6H4.5a1 1 0 0 1-1-1z'],
    rate: [
      'M7 10.5V20H4.5a1 1 0 0 1-1-1v-7.5a1 1 0 0 1 1-1z',
      'M7 10.5 10.8 3.6a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.2a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.2 20H7'
    ],
    tonight: ['M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z'],
    rank: ['M9 20V9h6v11', 'M3 20v-7h6', 'M15 20v-5h6v5', 'M2 20h20'],
    map: ['M9 4 3.5 6v14L9 18l6 2 5.5-2V4L15 6z', 'M9 4v14M15 6v14'],
    taste: ['M4 20V10M9.3 20V4M14.7 20v-8M20 20V7']
  };

  const surfaces = $derived(session.user?.nav?.surfaces ?? []);
  const current = $derived($page.url.pathname);
  const isActive = (href) => (href === '/' ? current === '/' : current.startsWith(href));
</script>

<div class="rail">
  <a class="brand" href="/" aria-label="Spielplan, Home">S<span class="rest">piel<em>plan</em></span></a>
  <nav aria-label="Main">
    {#each surfaces as s (s.key)}
      <a
        href={s.href}
        class:on={isActive(s.href)}
        aria-current={isActive(s.href) ? 'page' : undefined}
        data-surface={s.label}
      >
        <svg
          width="26"
          height="26"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.75"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          {#each ICONS[s.key] ?? [] as d (d)}<path {d} />{/each}
        </svg>
        <span class="label">{s.label}</span>
      </a>
    {/each}
  </nav>
</div>

<style>
  .brand {
    display: none;
  }
  nav {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 50;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(0, 1fr);
    height: calc(var(--tabbar) + env(safe-area-inset-bottom));
    padding: 0 max(8px, env(safe-area-inset-right)) env(safe-area-inset-bottom)
      max(8px, env(safe-area-inset-left));
    background: var(--bar);
    -webkit-backdrop-filter: blur(24px) saturate(1.5);
    backdrop-filter: blur(24px) saturate(1.5);
    box-shadow: inset 0 0.5px 0 var(--separator);
  }
  a {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    min-height: var(--touch);
    color: var(--text-3);
    font-size: var(--fs-tab);
    line-height: 12px;
    font-weight: 500;
  }
  a.on {
    color: var(--accent-text);
    font-weight: 600;
  }
  svg {
    transition: transform var(--dur-base) var(--ease-spring);
  }
  a:active svg {
    --press: 0.86;
    transform: scale(var(--press));
    transition-duration: var(--dur-press);
  }

  @media (min-width: 721px) {
    .rail {
      position: sticky;
      top: 0;
      flex: none;
      width: 232px;
      height: 100vh;
      height: 100dvh;
      display: flex;
      flex-direction: column;
      gap: 28px;
      padding: 28px 12px;
      background: var(--bg-elevated);
      box-shadow: inset -0.5px 0 0 var(--separator);
    }
    .brand {
      display: block;
      padding: 0 12px;
      color: var(--text);
      font-family: var(--serif);
      font-size: 28px;
      line-height: 32px;
    }
    nav {
      position: static;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 2px;
      height: auto;
      padding: 0;
      background: none;
      -webkit-backdrop-filter: none;
      backdrop-filter: none;
      box-shadow: none;
    }
    a:not(.brand) {
      flex-direction: row;
      justify-content: flex-start;
      gap: 12px;
      min-height: 40px;
      padding: 0 12px;
      border-radius: var(--r-sm);
      color: var(--text-2);
      font-size: var(--fs-subhead);
      line-height: 20px;
    }
    a.on {
      background: var(--surface-2);
      color: var(--text);
    }
    svg {
      width: 20px;
      height: 20px;
    }
  }

  /* Small tablets and a foldable's inner screen: a 72px rail under an "S" monogram. */
  @media (min-width: 721px) and (max-width: 1099px) {
    .rail {
      width: 72px;
      align-items: center;
      gap: 24px;
      padding: 12px 0;
    }
    .brand {
      padding: 0;
      font-size: 30px;
      line-height: 36px;
    }
    .rest {
      display: none;
    }
    nav {
      gap: 8px;
    }
    a:not(.brand) {
      flex-direction: column;
      justify-content: center;
      gap: 4px;
      width: 56px;
      min-height: 56px;
      padding: 0;
      color: var(--text-3);
      font-size: var(--fs-tab);
      line-height: 12px;
    }
    a.on {
      color: var(--accent-text);
    }
    svg {
      width: 24px;
      height: 24px;
    }
  }
</style>
