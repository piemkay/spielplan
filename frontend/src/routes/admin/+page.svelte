<script>
  // Overview (§6.6, decision 527): what needs the admin, then health, then every section.
  import { onMount } from 'svelte';
  import Icon from '$lib/components/Icon.svelte';
  import { attention, facts, health, manage, refresh } from './overview.svelte.js';

  const SECTIONS = [
    { key: 'titles', href: '/admin/titles', label: 'New titles', icon: 'inbox', tone: 'amber' },
    { key: 'people', href: '/admin/people', label: 'People', icon: 'people', tone: 'blue' },
    { key: 'services', href: '/admin/services', label: 'Services', icon: 'server', tone: 'teal' },
    { key: 'budget', href: '/admin/budget', label: 'Budget & AI', icon: 'budget', tone: 'ember' },
    { key: 'movieData', href: '/admin/movie-data', label: 'Movie data', icon: 'database', tone: 'purple' },
    { key: 'corrections', href: '/admin/corrections', label: 'Corrections', icon: 'pencil', tone: 'graphite' },
    { key: 'system', href: '/admin/system', label: 'System', icon: 'pulse', tone: 'green' }
  ];

  // The four reads the attention cards are computed from.
  const INPUTS = ['system', 'llm', 'sources', 'jellyfin'];

  onMount(() => {
    refresh();
  });

  const settled = $derived(INPUTS.every((key) => facts[key] || facts.errors[key]));
  const unanswered = $derived(Object.values(facts.errors));
  const items = $derived(attention(facts));
  const checks = $derived(health(facts));
  const values = $derived(manage(facts));
</script>

<div class="overview">
  <h1 class="large-title"><span class="phone">Admin</span><span class="desk">Overview</span></h1>

  <section class="needs" data-testid="admin-attention" aria-live="polite">
    {#if !settled}
      <p class="footnote">Checking…</p>
    {:else if items.length}
      <h2 class="status warn">
        <Icon name="warning" size={22} />
        {items.length}
        {items.length === 1 ? 'thing needs' : 'things need'} you
      </h2>
      {#each items as item (item.key)}
        <article class="card attention" data-attention={item.key}>
          <div class="head">
            <Icon name={item.icon} tone="warn" />
            <h3>{item.headline}</h3>
          </div>
          <p class="body">{item.body}</p>
          <a class="capsule" href={item.href}>{item.action}</a>
        </article>
      {/each}
    {:else if !unanswered.length}
      <h2 class="status ok">
        <Icon name="check" size={22} />
        Everything's fine
      </h2>
    {/if}
    {#if unanswered.length}
      <p class="footnote" role="alert">Some checks didn't answer: {unanswered[0]}</p>
    {/if}
  </section>

  {#if facts.system}
    <section class="group health">
      <h2 class="list-header">Health</h2>
      <div class="list-group">
        {#each checks as check (check.key)}
          <a class="list-row" href={check.href} data-health={check.key} data-state={check.state}>
            <Icon
              name={check.state === 'ok' ? 'check' : check.state === 'warn' ? 'warning' : 'clock'}
              tone={check.state === 'ok' ? 'green' : check.state}
            />
            <span class="text">
              <span>{check.label}</span>
              <span class="sub">{check.detail}</span>
            </span>
            <span class="chev"><Icon name="chevron-right" size={16} /></span>
          </a>
        {/each}
      </div>
    </section>
  {/if}

  <section class="group manage">
    <h2 class="list-header">Manage</h2>
    <nav class="list-group" aria-label="Admin sections">
      {#each SECTIONS as section (section.key)}
        <a class="list-row" href={section.href}>
          <Icon name={section.icon} tone={section.tone} />
          <span class="name">{section.label}</span>
          <span class="value">{values[section.key]}</span>
          <span class="chev"><Icon name="chevron-right" size={16} /></span>
        </a>
      {/each}
    </nav>
  </section>

  <section class="group setup">
    <h2 class="list-header">Setup</h2>
    <div class="list-group">
      <a class="list-row" href="/setup">
        <Icon name="home" tone="graphite" />
        <span class="name">First-time setup</span>
        <span class="chev"><Icon name="chevron-right" size={16} /></span>
      </a>
    </div>
  </section>
</div>

<style>
  .overview {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }
  .large-title {
    margin-bottom: -16px;
  }
  .desk {
    display: none;
  }
  .needs {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .status {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    font-size: var(--fs-section);
    line-height: 25px;
    font-weight: 600;
  }
  .status.warn :global(svg) {
    color: var(--warning);
  }
  .status.ok :global(svg) {
    color: var(--positive);
  }
  .attention {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  h3 {
    margin: 0;
    font-size: var(--fs-body);
    line-height: 22px;
    font-weight: 600;
  }
  .body {
    margin: 0;
    font-size: var(--fs-callout);
    line-height: 21px;
    color: var(--text-2);
  }
  .capsule {
    position: relative;
    display: inline-flex;
    align-items: center;
    min-height: 34px;
    padding: 0 14px;
    border-radius: var(--r-pill);
    background: var(--accent-tint);
    color: var(--accent-text);
    font-size: var(--fs-subhead);
    line-height: 20px;
    font-weight: 600;
  }
  .footnote {
    margin: 0;
  }
  .group {
    display: flex;
    flex-direction: column;
  }
  .list-row {
    color: var(--text);
    min-height: 52px;
  }
  /* The hairline starts after the icon tile. */
  .list-row + .list-row {
    box-shadow: none;
    background: linear-gradient(var(--separator), var(--separator)) 58px 0 / 100% 0.5px no-repeat;
  }
  .text {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .sub {
    font-size: var(--fs-footnote);
    line-height: 18px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }
  .name {
    flex: 1;
  }
  .value {
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .chev {
    display: grid;
    flex: none;
    margin-right: -4px;
    color: rgba(245, 240, 232, 0.35);
  }

  @media (pointer: coarse) {
    .capsule::after {
      content: '';
      position: absolute;
      inset: -7px -2px;
    }
  }

  @media (min-width: 721px) {
    .phone {
      display: none;
    }
    .desk {
      display: inline;
    }
  }
  @media (min-width: 1100px) {
    .overview {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      grid-template-areas: 'title title' 'needs health' 'manage health' 'setup health';
      align-items: start;
      gap: 32px 48px;
    }
    .large-title {
      grid-area: title;
    }
    .needs {
      grid-area: needs;
    }
    .health {
      grid-area: health;
    }
    .manage {
      grid-area: manage;
    }
    .setup {
      grid-area: setup;
    }
  }
</style>
