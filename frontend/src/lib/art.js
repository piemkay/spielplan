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
 */

/** The title's id from whichever key its payload uses, or null when it names none. */
export function titleIdOf(title) {
  const raw = title?.title_id ?? title?.id;
  if (raw === null || raw === undefined || raw === '') return null;
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}

/** The same-origin poster URL for a title, or null when there is no title to ask about. */
export function posterSrc(title) {
  const id = titleIdOf(title);
  return id === null ? null : `/api/art/${id}/poster`;
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
  image.src = src;
  return image;
}
