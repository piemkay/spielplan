/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import Headshot from './Headshot.svelte';

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

function render(credit) {
  const cell = target.appendChild(document.createElement('div'));
  apps.push(mount(Headshot, { target: cell, props: { credit } }));
  flushSync();
  return cell.querySelector('.face');
}

describe('a face on the title card', () => {
  it('is the photo from this origin, loaded lazily, when the credit has one', () => {
    const face = render({ person_id: 8101, name: 'Val Kilmer', photo: true });
    const img = face.querySelector('img');
    expect(img.getAttribute('src')).toBe('/api/art/person/8101');
    expect(img.getAttribute('loading')).toBe('lazy');
    expect(face.textContent.trim()).toBe('');
  });

  it('is two initials on a tone when there is no photo', () => {
    const face = render({ person_id: 8102, name: 'Jon Voight', photo: false });
    expect(face.querySelector('img')).toBeNull();
    expect(face.textContent.trim()).toBe('JV');
    expect(face.style.background).not.toBe('');
    expect(render({ person_id: 8103, name: 'Zendaya' }).textContent.trim()).toBe('Z');
  });

  it('falls back to the initials when the photo fails', () => {
    const face = render({ person_id: 8104, name: 'Tom Sizemore', photo: true });
    face.querySelector('img').dispatchEvent(new Event('error'));
    flushSync();
    expect(face.querySelector('img')).toBeNull();
    expect(face.textContent.trim()).toBe('TS');
  });
});
