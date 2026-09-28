<script>
  // Admin brings its own navigation (§6.6, decision 527): a back row on a phone, a sidebar on a
  // desktop. The shell above it keeps the top row and the re-auth banner.
  import { untrack } from 'svelte';
  import { page } from '$app/stores';
  import { session } from '$lib/session.svelte.js';
  import { spend } from '$lib/spendGuard.svelte.js';
  import Icon from '$lib/components/Icon.svelte';
  import { attention, facts, refresh, waiting } from './overview.svelte.js';

  let { children } = $props();

  const SECTIONS = [
    { href: '/admin', label: 'Overview', icon: 'home' },
    { href: '/admin/titles', label: 'New titles', icon: 'inbox' },
    { href: '/admin/people', label: 'People', icon: 'people' },
    { href: '/admin/services', label: 'Services', icon: 'server' },
    { href: '/admin/budget', label: 'Budget & AI', icon: 'budget' },
    { href: '/admin/movie-data', label: 'Movie data', icon: 'database' },
    { href: '/admin/corrections', label: 'Corrections', icon: 'pencil' },
    { href: '/admin/system', label: 'System', icon: 'pulse' }
  ];

  const path = $derived($page.url.pathname.replace(/\/$/, ''));
  const current = $derived(
    SECTIONS.find((s) =>
      s.href === '/admin' ? path === '/admin' : path === s.href || path.startsWith(`${s.href}/`)
    )?.href
  );
  const back = $derived(
    path === '/admin'
      ? { href: '/', label: 'Spielplan' }
      : path.startsWith('/admin/people/')
        ? { href: '/admin/people', label: 'People' }
        : { href: '/admin', label: 'Admin' }
  );

  const titlesWaiting = $derived(waiting(facts));
  const budgetNeedsYou = $derived(attention(facts).some((item) => item.key === 'budget'));

  // A finished re-auth remounts the section, so every read the re-prompt refused is asked again.
  let epoch = $state(0);
  let reprompted = false;
  $effect(() => {
    const need = !!session.user?.admin_reauth_required;
    if (reprompted && !need) epoch += 1;
    reprompted = need;
  });

  // Every section change re-reads what the sidebar counts, so what was done there shows here.
  $effect(() => {
    void epoch;
    void path;
    untrack(() => refresh(['system', 'llm']));
  });

  // Budget's own reads and saves are the newest word on the meter.
  $effect(() => {
    if (spend.llm) facts.llm = spend.llm;
  });
</script>

<div class="admin">
  <nav class="side" aria-label="Admin">
    <p class="wordmark" aria-hidden="true">Spiel<em>plan</em></p>
    <a class="home" href="/">
      <Icon name="chevron-left" size={20} />
      <span>Back to Spielplan</span>
    </a>
    <ul>
      {#each SECTIONS as section (section.href)}
        <li>
          <a
            class="item"
            href={section.href}
            aria-current={current === section.href ? 'page' : undefined}
          >
            <Icon name={section.icon} size={20} />
            <span class="label">{section.label}</span>
            {#if section.href === '/admin/titles' && titlesWaiting > 0}
              <span class="count">{titlesWaiting}<span class="sr">{' waiting'}</span></span>
            {:else if section.href === '/admin/budget' && budgetNeedsYou}
              <span class="dot" role="img" aria-label="needs attention"></span>
            {/if}
          </a>
        </li>
      {/each}
    </ul>
  </nav>

  <div class="section">
    <a class="back" href={back.href} data-testid="admin-back">
      <Icon name="chevron-left" size={24} />
      <span>{back.label}</span>
    </a>
    {#key epoch}{@render children()}{/key}
  </div>
</div>

<style>
  .side {
    display: none;
  }
  .back {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    min-height: 44px;
    margin: 0 0 4px -8px;
    padding: 0 8px 0 2px;
    font-size: var(--fs-body);
    line-height: 22px;
  }
  @media (pointer: coarse) {
    .back,
    .home,
    .item {
      min-height: var(--touch);
    }
  }

  @media (min-width: 721px) {
    .admin {
      display: grid;
      grid-template-columns: 232px minmax(0, 1fr);
      gap: 48px;
      align-items: start;
    }
    .back {
      display: none;
    }
    .side {
      position: sticky;
      top: 16px;
      display: flex;
      flex-direction: column;
      padding: 24px 12px;
      border-radius: var(--r-lg);
      background: var(--bg-elevated);
    }
    .section {
      max-width: 1080px;
      padding-top: 8px;
    }
    .section :global(.large-title) {
      font-size: var(--fs-display);
      line-height: 48px;
    }
  }

  .wordmark {
    margin: 0 12px 24px;
    font-family: var(--serif);
    font-size: var(--fs-title);
    line-height: 34px;
  }
  .home {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 40px;
    padding: 0 12px;
    font-size: var(--fs-subhead);
    font-weight: 600;
  }
  ul {
    list-style: none;
    margin: 16px 0 0;
    padding: 16px 0 0;
    box-shadow: inset 0 0.5px 0 var(--separator);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .item {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 40px;
    padding: 0 12px;
    border-radius: var(--r-sm);
    color: var(--text-2);
    font-size: var(--fs-subhead);
    line-height: 20px;
  }
  .item:hover {
    color: var(--text);
    background: var(--surface-1);
  }
  .item[aria-current='page'] {
    background: var(--surface-2);
    color: var(--text);
    font-weight: 600;
  }
  .label {
    flex: 1;
  }
  .count {
    min-width: 24px;
    height: 20px;
    padding: 0 6px;
    border-radius: var(--r-xs);
    background: var(--warning-tint);
    color: var(--warning);
    font-size: var(--fs-caption);
    font-weight: 600;
    line-height: 20px;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .dot {
    width: 6px;
    height: 6px;
    margin-right: 8px;
    border-radius: var(--r-pill);
    background: var(--warning);
  }
</style>
