/**
 * @vitest-environment jsdom
 */

import { mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it } from 'vitest';

import DataSources from './DataSources.svelte';

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  target.remove();
});

// Whitespace-normalised: textContent keeps the markup's indentation, which browsers collapse.
const notice = (which) =>
  target.querySelector(`[data-notice="${which}"]`)?.textContent?.replace(/\s+/g, ' ').trim();

it('states the IMDb courtesy notice in the words IMDb fixes', () => {
  const app = mount(DataSources, { target });
  // Verbatim: IMDb's terms fix the sentence.
  expect(notice('imdb')).toBe(
    'Information courtesy of IMDb (https://www.imdb.com). Used with permission.'
  );
  unmount(app);
});

it("carries TMDB's not-endorsed notice and renders no logo this tree does not hold", () => {
  const app = mount(DataSources, { target });
  // TMDB's terms fix the whole sentence.
  expect(notice('tmdb')).toBe(
    'This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise ' +
      'approved by TMDB.'
  );
  // The logo slot renders nothing while `static/tmdb-logo.svg` is absent.
  expect(target.querySelectorAll('img')).toHaveLength(0);
  unmount(app);
});

it('credits Wikipedia and TVmaze under CC BY-SA and OMDb under CC BY-NC', () => {
  const app = mount(DataSources, { target });
  expect(notice('wikipedia')).toBe(
    'Plot summaries and overviews from Wikipedia, by its contributors, under CC BY-SA 4.0.'
  );
  expect(notice('tvmaze')).toBe('Series data from TVmaze, under CC BY-SA 4.0.');
  expect(notice('omdb')).toBe('Ratings and plot text from OMDb, under CC BY-NC 4.0.');
  // CC BY-SA and BY-NC require the licence itself to be linked.
  const licences = [...target.querySelectorAll('a.licence')].map((a) => a.getAttribute('href'));
  expect(licences).toEqual([
    'https://creativecommons.org/licenses/by-sa/4.0/',
    'https://creativecommons.org/licenses/by-sa/4.0/',
    'https://creativecommons.org/licenses/by-nc/4.0/'
  ]);
  unmount(app);
});
