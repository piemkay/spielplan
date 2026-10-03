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

/**
 * Want it on a title only TMDB knows (decision 558): `{title_id, state, owned, minted}`, where `owned`
 * means the library holds it and nothing was wished.
 *
 * @param {'movie' | 'series'} kind @param {number} tmdbId
 */
export async function setWishTmdb(kind, tmdbId) {
  return changed(await api(`/wish/tmdb/${kind}/${tmdbId}`, { method: 'PUT' }));
}

/** Remove from the list, or undo Not for me. @param {number} titleId */
export async function clearWish(titleId) {
  return changed(await api(`/wish/${titleId}`, { method: 'DELETE' }));
}

/** Closes an arrival's banner; the server deletes the want behind it. @param {number} titleId */
export async function dismissArrival(titleId) {
  return changed(await post(`/wish/${titleId}/dismiss`));
}

/** An arrival's Undo: its want back, with its own date. @param {number} titleId @param {string} since */
export async function restoreArrival(titleId, since) {
  return changed(await post(`/wish/${titleId}/restore`, { since }));
}

/** The household's wanted count, for You. */
export function loadWishSummary() {
  return get('/wish/summary');
}

export function loadWishList() {
  return get('/wish');
}

/**
 * @param {'movie' | 'series'} kind @param {number | 'everyone' | null} audience null is the viewer
 * @param {{ decade?: number | null, sort?: 'match' | 'newest' }} [filters]
 */
export function loadWorthGetting(kind, audience = null, { decade = null, sort = 'match' } = {}) {
  return get(`/home/worth-getting${qs({ kind, for: audience, decade, sort: sort === 'newest' ? sort : null })}`);
}

/** Home's row under Worth getting: "4 wanted, 1 by both of you", where the household is two. */
export function wishSummary(summary) {
  const wanted = summary?.wanted ?? 0;
  if (!wanted) return 'Nothing on it yet';
  const both = summary?.both ?? 0;
  const by = summary?.members === 2 ? 'by both of you' : 'by more than one of you';
  return `${wanted.toLocaleString()} wanted${both ? `, ${both.toLocaleString()} ${by}` : ''}`;
}

/** The row stands under Worth getting, or after the last shelf while the list holds anything,
 * unless the member put it away for the day (decision 554). */
export function wishRowShown(payload) {
  if (payload?.wish?.hidden) return false;
  const shelf = (payload?.shelves ?? []).some((s) => s.id === 'worth_getting');
  return shelf || (payload?.wish?.wanted ?? 0) > 0;
}

function namesOf(people) {
  if (people.length <= 2) return people.join(' and ');
  return `${people.slice(0, -1).join(', ')} and ${people.at(-1)}`;
}

/** An unowned title's card: "Jenny would likely enjoy it too.", or nothing. @param {string[]} names */
export function likelyTooLine(names = []) {
  return names.length ? `${namesOf(names)} would likely enjoy it too.` : '';
}

/** On a title others want: "you: likely too", "you: maybe", or nothing the scores can say. */
export function youLine(likely) {
  if (likely === 'likely') return 'you: likely too';
  return likely === 'maybe' ? 'you: maybe' : '';
}

/** "You, Jenny and Sam": people in the viewer's words. @param {{id: number, name: string}[]} people */
export function peopleLine(people = [], viewerId = null) {
  return namesOf(people.map((p) => (p.id === viewerId ? 'You' : p.name)));
}

/** Worth getting's lede for whom the list is for: the viewer, another member, or everyone. */
export function worthLede(kind, forWhom, viewerId = null) {
  const noun = kind === 'series' ? 'Series' : 'Films';
  if (forWhom === 'everyone') {
    return `${noun} the library doesn't have that would suit all of you, leaving out what anyone avoids.`;
  }
  const who = !forWhom || forWhom.id === viewerId ? 'you rate' : `${forWhom.name} rates`;
  return `${noun} the library doesn't have, closest to the ones ${who} highest.`;
}

// Spelled here: ICU versions disagree on September ("Sep" or "Sept").
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "12 Sep", in the viewer's own time zone. */
export function dayMonth(iso) {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? '' : `${at.getDate()} ${MONTHS[at.getMonth()]}`;
}

/** A Worth getting row's reason: the viewer's own liked film it is like, or nothing. */
export function likeLine(item) {
  const like = item?.like;
  const terms = like?.terms?.length ? like.terms.join(', ') : '';
  if (!like) return '';
  return [`Like ${like.name}`, terms].filter(Boolean).join(' · ');
}
