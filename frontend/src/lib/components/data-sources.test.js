/**
 * @vitest-environment jsdom
 *
 * The notices the displayed data's own terms make a condition of showing it.
 * Spec v2.1 §6.8, §10; decisions 293 and 298.
 *
 * Every install renders roughly ten thousand TMDB overviews, 8,418 IMDb scores, 8,409 Wikipedia
 * plots and 1,619 TVmaze rows, and until this block shipped not one of the notices those licences
 * require appeared anywhere in the product. §6.8's quiet register is why the answer is one surface
 * and not a credit line under every poster: the terms require a notice within the product, not a
 * source name on every tile (decision 293).
 *
 * MOUNTED AS WELL AS DRIVEN, NOT INSTEAD OF IT. The Playwright half in
 * `e2e/specs/19-phone-shell.spec.js` is what proves a signed-in phone can REACH the block, and
 * decision 226 forbids a row resting on a vitest id alone. What this layer adds is the words: a
 * licence notice is a string that has to be exact, and a test that reads it out of the mounted
 * component fails on a typo in the one place where a paraphrase is a licence breach rather than a
 * copy edit. Naming these ids in `spec_coverage.toml` is what stops them being deleted quietly.
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

/** The notice text for one source, read the way the e2e half reads it: by the hook the markup
 *  carries, so the two layers cannot drift onto different elements — and whitespace-normalised,
 *  because `textContent` keeps the source's own indentation while both a browser and Playwright's
 *  text matcher collapse it. Comparing the raw string would make the assertion a claim about how
 *  the markup is wrapped rather than about the words a member reads. */
const notice = (which) =>
  target.querySelector(`[data-notice="${which}"]`)?.textContent?.replace(/\s+/g, ' ').trim();

it('states the IMDb courtesy notice in the words IMDb fixes', () => {
  const app = mount(DataSources, { target });
  // Verbatim, and that is the whole assertion: IMDb's terms give the sentence, so "courtesy of
  // IMDb" with the URL dropped, or a trailing "Used by permission", is a different claim about
  // permission than the one that was granted.
  expect(notice('imdb')).toBe(
    'Information courtesy of IMDb (https://www.imdb.com). Used with permission.'
  );
  unmount(app);
});

it("carries TMDB's not-endorsed notice and renders no logo this tree does not hold", () => {
  const app = mount(DataSources, { target });
  // TMDB's terms fix the whole sentence and license only the bracketed category, so the head
  // clause is quoted and not chosen; two adjacent literals because the sentence is 104 characters
  // and this file holds a line. `test_static_contracts.py` derives it from the terms and holds
  // all three carriers to it, which is the half a `toBe` against a literal cannot do at all.
  // [decision 319]
  expect(notice('tmdb')).toBe(
    'This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise ' +
      'approved by TMDB.'
  );
  // Decision 298: the logo is a named slot, not a trademark file this repository invents, and
  // `frontend/static/tmdb-logo.svg` is not in the tree. The slot renders NOTHING while that is
  // true - asserted here rather than left to be discovered, because a broken image in the one
  // place the app makes a licence promise is worse than an absent one. The day the owner drops
  // the file in, this expectation is the line that has to change with it.
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
  // CC BY-SA and CC BY-NC both require the licence itself to be identified or linked, so the
  // credit without the link is half a credit. Three links, because three sources carry one.
  const licences = [...target.querySelectorAll('a.licence')].map((a) => a.getAttribute('href'));
  expect(licences).toEqual([
    'https://creativecommons.org/licenses/by-sa/4.0/',
    'https://creativecommons.org/licenses/by-sa/4.0/',
    'https://creativecommons.org/licenses/by-nc/4.0/'
  ]);
  unmount(app);
});
