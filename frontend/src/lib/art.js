/**
 * Where a title's poster is. Spec v2.1 §6.8 ("Poster-forward 2:3 cards"); decisions 483 and 484.
 *
 * Always this app's own origin: the backend fetches the art and serves it behind the session, so
 * no `<img src>` here ever names a third-party host, and a phone's address and Referer never
 * reach one. The URL is built for every title rather than only for one whose payload carries a
 * `poster_path`, because a title without one may still have art - the household's Jellyfin holds
 * it, or the worker's TMDB lookup found it (decision 484) - and the server's 404 for a title with
 * none is cacheable, so the card that draws its tinted panel costs a request once rather than on
 * every render.
 *
 * KEYED ON EITHER SPELLING, `title_id` FIRST. The catalog, Rate and the title card send `id`;
 * the shelves, every Tonight payload and the finish prompt send `title_id` - and in the finish
 * prompt `id` is the prompt's own row, not the title's. A helper that read only `id` asked for
 * `/api/art/undefined/poster` on every Tonight card and for the wrong title on the prompt.
 *
 * AN APP-MINTED ID CARRIES THE DATABASE'S VERSION. A 200 is kept 180 days on this URL (decision
 * 483), and a re-seed hands ids from 1000000000 out again to whatever titles the new drain
 * reaches first - so an id-only URL showed the last database's poster under another title's
 * name. `/config` sends `art_epoch` (`art/poster.url_epoch` on the server, which changes exactly
 * when an id can be reused) and it rides as `?v=`, which the route ignores. A corpus id names
 * one title in every database and keeps its bare URL.
 */

import { session } from '$lib/session.svelte.js';

/** `derive/ids.APP_ID_MIN`: the first id this app mints; below it, the corpus's own ids. */
const APP_ID_MIN = 1_000_000_000;

/**
 * How long this page remembers that a poster URL answered with nothing: the shortest max-age the
 * route puts on a 404 (`art/poster.BROWSER_TRANSIENT`, ten minutes; a pending lookup gets half an
 * hour and "none anywhere" a day). Inside it the browser's own cache would answer the same URL
 * with the same 404, so remembering it here changes nothing a member sees - it only stops every
 * later card for that title (the shelf, the search grid, the title card, a re-render) from
 * drawing an `<img>` that fails again and prints another 404 into the console. Decision 483
 * already drops the image on error; this carries the drop from one card to the page. An `<img>`
 * cannot tell a 404 from a load that never reached the route, so a phone that was offline for a
 * moment shows that title's tinted panel - the designed state - for the same ten minutes.
 */
export const MISSING_FOR_MS = 10 * 60 * 1000;
const missing = new Map();

/** Remember that `src` answered with no image, so no card asks for it again for a while. */
export function noteMissing(src) {
  if (src) missing.set(src, Date.now() + MISSING_FOR_MS);
}

function knownMissing(src) {
  const until = missing.get(src);
  if (until === undefined) return false;
  if (until > Date.now()) return true;
  missing.delete(src);
  return false;
}

/** The title's id from whichever key its payload uses, or null when it names none. */
export function titleIdOf(title) {
  const raw = title?.title_id ?? title?.id;
  if (raw === null || raw === undefined || raw === '') return null;
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}

/**
 * The same-origin poster URL for a title, or null when there is no title to ask about - or when
 * this page has just been told that URL holds no image (`noteMissing`), so the card draws its
 * tinted panel without an `<img>` that would fail again.
 */
export function posterSrc(title) {
  const id = titleIdOf(title);
  if (id === null) return null;
  const epoch = id >= APP_ID_MIN ? session.artEpoch : null;
  const src = epoch
    ? `/api/art/${id}/poster?v=${encodeURIComponent(epoch)}`
    : `/api/art/${id}/poster`;
  return knownMissing(src) ? null : src;
}

/**
 * Ask the browser for a poster before it is drawn, so the next card's art is in its HTTP cache
 * when the card arrives. §6's budgets ("<2 s per sweep card, <1.5 s per battle") are measured
 * from the tap, and Rate's 1.2 s reveal hold is time the network would otherwise spend idle.
 */
export function preloadPoster(title) {
  const src = posterSrc(title);
  if (!src || typeof Image === 'undefined') return null;
  const image = new Image();
  image.decoding = 'async';
  image.onerror = () => noteMissing(src);
  image.src = src;
  return image;
}
