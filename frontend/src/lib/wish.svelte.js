// The household's wish list and each person's Not for me (decision 544). Every write bumps `wishes.epoch`,
// so Home re-reads its shelves, banner and wish row whichever surface wrote.
import { api, get, post, qs } from '$lib/api.js';

export const wishes = $state({ epoch: 0 });

function changed(result) {
  wishes.epoch += 1;
  return result;
}

/** @param {number} titleId @param {'want' | 'not_for_me'} state */
export async function setWish(titleId, state) {
  return changed(await api(`/wish/${titleId}`, { method: 'PUT', body: { state } }));
}

/** Remove from the list, or undo Not for me. @param {number} titleId */
export async function clearWish(titleId) {
  return changed(await api(`/wish/${titleId}`, { method: 'DELETE' }));
}

/** Closes an arrival's banner; the server deletes the want behind it. @param {number} titleId */
export async function dismissArrival(titleId) {
  return changed(await post(`/wish/${titleId}/dismiss`));
}

export function loadWishList() {
  return get('/wish');
}

/** @param {'movie' | 'series'} kind @param {'me' | 'pair'} audience */
export function loadWorthGetting(kind, audience = 'me') {
  return get(`/home/worth-getting${qs({ kind, with: audience })}`);
}

/** Home's row under Worth getting: "4 wanted, 1 by both of you", where the household is two. */
export function wishSummary(summary) {
  const wanted = summary?.wanted ?? 0;
  if (!wanted) return 'Nothing on it yet';
  const both = summary?.both ?? 0;
  const by = summary?.members === 2 ? 'by both of you' : 'by more than one of you';
  return `${wanted.toLocaleString()} wanted${both ? `, ${both.toLocaleString()} ${by}` : ''}`;
}

/** The row stands under Worth getting, or after the last shelf while the list holds anything. */
export function wishRowShown(payload) {
  const shelf = (payload?.shelves ?? []).some((s) => s.id === 'worth_getting');
  return shelf || (payload?.wish?.wanted ?? 0) > 0;
}

function namesOf(people) {
  if (people.length <= 2) return people.join(' and ');
  return `${people.slice(0, -1).join(', ')} and ${people.at(-1)}`;
}

/** A group's heading, in the viewer's own words. @param {{id: number, name: string}[]} wanters */
export function groupHeading(wanters = [], viewerId = null) {
  const mine = wanters.some((w) => w.id === viewerId);
  const others = wanters.filter((w) => w.id !== viewerId).map((w) => w.name);
  if (!mine) return `${namesOf(others)} ${others.length === 1 ? 'wants' : 'want'}`;
  if (!others.length) return 'You want';
  if (others.length === 1) return 'You both want';
  return `You, ${namesOf(others)} want`;
}

/** An unowned title's card: "Jenny would likely enjoy it too.", or nothing. @param {string[]} names */
export function likelyTooLine(names = []) {
  return names.length ? `${namesOf(names)} would likely enjoy it too.` : '';
}

/** "Jenny: likely too", "you: maybe"; nothing for someone the scores say little about. */
export function othersLine(others = [], viewerId = null) {
  return others
    .filter((o) => o.likely === 'likely' || o.likely === 'maybe')
    .map((o) => `${o.id === viewerId ? 'you' : o.name}: ${o.likely === 'likely' ? 'likely too' : 'maybe'}`)
    .join(' · ');
}

// Spelled here: ICU versions disagree on September ("Sep" or "Sept").
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "12 Sep", in the viewer's own time zone. */
export function dayMonth(iso) {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? '' : `${at.getDate()} ${MONTHS[at.getMonth()]}`;
}

/** A Worth getting row's reason: the liked film it is like, or for two, the other's name first. */
export function likeLine(item, otherName = '') {
  const like = item?.like;
  const terms = like?.terms?.length ? like.terms.join(', ') : '';
  if (otherName) return [`${otherName} too`, terms].filter(Boolean).join(' · ');
  if (!like) return '';
  return [`Like ${like.name}`, terms].filter(Boolean).join(' · ');
}
