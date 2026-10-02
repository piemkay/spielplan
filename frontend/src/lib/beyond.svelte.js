// A search beyond the library (decision 558): More in Spielplan, the catalogue's titles not owned,
// and From TMDB, the titles Spielplan does not hold, under Home's own grid.
import { qs } from '$lib/api.js';
import { clearWish, setWishTmdb } from '$lib/wish.svelte.js';

export const TMDB_MIN = 3;
export const TMDB_WAIT_MS = 350;
export const MORE_LIMIT = 12;

/** More in Spielplan: the grid's own filters, over the titles the library does not hold. */
export function moreQuery(kinds, params, { offset = 0 } = {}) {
  return `/titles${qs({ kind: kinds, ...params, owned: 'not', limit: MORE_LIMIT, offset })}`;
}

/** From TMDB's read, or null under three letters, which the server would not send on. */
export function tmdbQuery(kinds, q) {
  const text = String(q ?? '').split(/\s+/).filter(Boolean).join(' ');
  return text.length < TMDB_MIN ? null : `/wish/tmdb${qs({ kind: kinds, q: text })}`;
}

export function kindNoun(kinds) {
  if (kinds.length > 1) return 'titles';
  return kinds[0] === 'series' ? 'series' : 'films';
}

export const tmdbKey = (hit) => `${hit.kind}:${hit.tmdb_id}`;

/** What this session wished from TMDB, by `tmdbKey`: the title it became and the want's state. */
export const tmdbWants = $state({});

/**
 * Want it on a From TMDB hit, or take the want back. `owned: true` means the library holds it: its
 * own card opens instead, and nothing was wished.
 */
export async function toggleTmdbWant(hit) {
  const key = tmdbKey(hit);
  const held = tmdbWants[key];
  if (held?.state === 'want') {
    const res = await clearWish(held.title_id);
    tmdbWants[key] = { title_id: held.title_id, state: res?.state ?? null };
    return { title_id: held.title_id, state: null, owned: false };
  }
  const res = await setWishTmdb(hit.kind, hit.tmdb_id);
  if (!res.owned) tmdbWants[key] = { title_id: res.title_id, state: res.state };
  return res;
}

/** A Want it that did not land, in the member's words. */
export function wantError(err) {
  if (err?.status === 503) return "TMDB didn't answer. Try again in a moment.";
  return `Could not save that — ${err?.message ?? 'try again'}`;
}
