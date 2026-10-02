// A credit, term or More like this tapped on a card opened outside Home opens Home's grid, and Back
// reopens that card on the page it was opened on (decision 557 item 6). The page's own history entry
// carries the card as a stamp; Home's entry names the page, for its own way back.
import { goto, replaceState } from '$app/navigation';
import { page } from '$app/state';

const SURFACES = { rank: 'Rank', taste: 'Taste', compare: 'Compare', you: 'You' };

/**
 * Walks back past `depth` sheet entries, each sheet closing through its own Back path.
 *
 * @param {number} depth
 */
export function unwind(depth) {
  return new Promise((resolve) => {
    addEventListener('popstate', () => setTimeout(resolve), { once: true });
    history.go(-depth);
  });
}

/**
 * @param {string} href Home's URL, from `homeHref`
 * @param {{titleId: number, from: string}} card the card to reopen, and the surface that reopens it
 */
export async function jumpHome(href, { titleId, from }) {
  // Sheets still open under the card (You and its wish list) go first, so the stamp lands on the
  // page's own entry and Back finds no sheet entry whose sheet is gone.
  const depth = page.state?.sheets?.length ?? 0;
  if (depth) await unwind(depth);
  replaceState('', { ...page.state, returnCard: { titleId, from } });
  await goto(href, { state: { jumpedFrom: from } });
}

/** The page Home's current entry was jumped to from, by name; '' when it was not. */
export function jumpedFrom() {
  return SURFACES[page.state?.jumpedFrom] ?? '';
}

/**
 * The title whose card `from` reopens after Back, once: the stamp is cleared as it is read.
 *
 * @param {string} from
 * @returns {number | null}
 */
export function returningCard(from) {
  const stamp = page.state?.returnCard;
  if (!stamp || stamp.from !== from) return null;
  const { returnCard: _, ...rest } = page.state;
  replaceState('', rest);
  return stamp.titleId;
}
