/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import FilterChip from './FilterChip.svelte';

let target;
let apps = [];

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  apps.forEach((app) => unmount(app));
  apps = [];
  target.remove();
});

function render(props) {
  apps.push(mount(FilterChip, { target, props: { testid: 'chip', ...props } }));
  flushSync();
  return target.querySelector('[data-testid="chip"]');
}

describe('a term chip', () => {
  it('flips on its body and is removed by its x', () => {
    const onFlip = vi.fn();
    const onRemove = vi.fn();
    const chip = render({ variant: 'term', mode: 'in', label: 'cozy & mellow', facet: 'mood', onFlip, onRemove });
    const body = chip.querySelector('[aria-label="Switch cozy & mellow to leave out"]');
    body.click();
    expect(onFlip).toHaveBeenCalledOnce();
    expect(onRemove).not.toHaveBeenCalled();
    chip.querySelector('[aria-label="Remove cozy & mellow"]').click();
    expect(onRemove).toHaveBeenCalledOnce();
    expect(onFlip).toHaveBeenCalledOnce();
  });

  it('is filled with its facet dot when included, outlined with a minus when left out', () => {
    const inc = render({ variant: 'term', mode: 'in', label: 'cozy & mellow', facet: 'mood', onRemove: () => {} });
    expect(inc.classList.contains('out')).toBe(false);
    expect(inc.querySelector('.dot').style.background).toBe('var(--facet-mood)');
    expect(inc.textContent).not.toContain('−');

    target.replaceChildren();
    const out = render({ variant: 'term', mode: 'out', label: 'heist', facet: 'themes', onRemove: () => {} });
    expect(out.classList.contains('out')).toBe(true);
    expect(out.textContent.trim()).toBe('− heist');
    expect(out.querySelector('[aria-label="Switch heist to include"]')).not.toBeNull();
  });
});

describe('a person chip', () => {
  it('shows a small headshot and only removes', () => {
    const onRemove = vi.fn();
    const chip = render({
      variant: 'person', label: 'Denis Villeneuve', onRemove,
      person: { person_ids: [77], person_id: 77, name: 'Denis Villeneuve', photo: true }
    });
    expect(chip.querySelector('.face img').getAttribute('src')).toBe('/api/art/person/77');
    expect(chip.querySelectorAll('button')).toHaveLength(1);
    chip.querySelector('[aria-label="Remove Denis Villeneuve"]').click();
    expect(onRemove).toHaveBeenCalledOnce();
  });
});

describe('any other chip', () => {
  it('is one button that removes what it names', () => {
    const onRemove = vi.fn();
    const chip = render({ label: 'Beyond your library', onRemove });
    expect(chip.tagName).toBe('BUTTON');
    expect(chip.getAttribute('aria-label')).toBe('Remove Beyond your library');
    chip.click();
    expect(onRemove).toHaveBeenCalledOnce();
  });
});
