// Posters always come from this origin, so no phone address or Referer reaches a third party.
// App-minted ids carry the database's `art_epoch` as `?v=`: a re-seed reuses them, and a 200 is
// cached for 180 days.

import { session } from '$lib/session.svelte.js';

/** `derive/ids.APP_ID_MIN`: the first id this app mints; below it, the corpus's own ids. */
const APP_ID_MIN = 1_000_000_000;

/** The route's shortest 404 max-age (`art/poster.BROWSER_TRANSIENT`), which a backend test pins. */
export const MISSING_FOR_MS = 10 * 60 * 1000;
const missing = new Map();

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

/** `title_id` first: in the finish prompt, `id` is the prompt's own row. */
export function titleIdOf(title) {
  const raw = title?.title_id ?? title?.id;
  if (raw === null || raw === undefined || raw === '') return null;
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}

export function posterSrc(title) {
  const id = titleIdOf(title);
  if (id === null) return null;
  const epoch = id >= APP_ID_MIN ? session.artEpoch : null;
  const src = epoch
    ? `/api/art/${id}/poster?v=${encodeURIComponent(epoch)}`
    : `/api/art/${id}/poster`;
  return knownMissing(src) ? null : src;
}

/** Warm the HTTP cache during Rate's reveal hold, so the next card's art is ready. */
export function preloadPoster(title) {
  const src = posterSrc(title);
  if (!src || typeof Image === 'undefined') return null;
  const image = new Image();
  image.decoding = 'async';
  image.onerror = () => noteMissing(src);
  image.src = src;
  return image;
}
