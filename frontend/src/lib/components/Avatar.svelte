<script module>
  import { TONES } from './Icon.svelte';

  const TILES = ['blue', 'purple', 'green', 'teal', 'amber', 'red'].map((tone) => TONES[tone]);

  /**
   * One person, one colour on every screen: the stored colour, else one keyed by account, never by
   * name. A guest has no account, so no colour of their own.
   * @param {{ id?: number|null, role?: string|null, colour?: string|null } | null | undefined} person
   */
  export function avatarColour(person) {
    if (person?.colour) return person.colour;
    if (person?.id == null) return null;
    return person.role === 'admin' ? TONES.graphite : TILES[person.id % TILES.length];
  }
</script>

<script>
  // A person's initial on their colour; `yes` ticks them on the reveal's approval line.
  let { name = '', person = null, size = 30, yes = undefined } = $props();

  const colour = $derived(avatarColour(person));
</script>

<span
  class="avatar"
  class:no={yes === false}
  class:guest={!colour}
  style:--size="{size}px"
  style:background={colour}
  aria-hidden="true"
>
  {(name || '?').charAt(0).toUpperCase()}
  {#if yes}
    <span class="yes">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
    </span>
  {/if}
</span>

<style>
  .avatar {
    position: relative;
    flex: none;
    display: grid;
    place-items: center;
    width: var(--size);
    height: var(--size);
    border-radius: var(--r-pill);
    color: #fff;
    font-size: max(var(--fs-footnote), calc(var(--size) * 0.42));
    line-height: 1;
    font-weight: 600;
  }
  .guest {
    background: var(--thumb);
    color: var(--text);
  }
  .no {
    opacity: 0.45;
  }
  .yes {
    position: absolute;
    right: -3px;
    bottom: -3px;
    display: grid;
    place-items: center;
    width: 16px;
    height: 16px;
    border-radius: var(--r-pill);
    background: var(--positive);
    color: var(--bg);
    box-shadow: 0 0 0 2px var(--bg);
  }
</style>
