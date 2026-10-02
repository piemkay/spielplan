// A sticky Home notice's x (decision 554): the pending row, the set-up notice or the wish-list row
// hides until tomorrow, and the toast's Undo brings it back.
import { api } from '$lib/api.js';
import { showToast } from '$lib/toast.svelte.js';

/** @typedef {'pending' | 'setup' | 'wish_list'} Notice */

/** @param {Notice} notice @param {() => void} [onBack] re-reads what the notice stands on */
export async function putAway(notice, onBack) {
  await api(`/home/notices/${notice}`, { method: 'PUT' });
  showToast('Hidden until tomorrow', {
    label: 'Undo',
    run: () =>
      bringBack(notice)
        .then(() => onBack?.())
        .catch((err) => showToast(err.message))
  });
}

/** @param {Notice} notice */
export function bringBack(notice) {
  return api(`/home/notices/${notice}`, { method: 'DELETE' });
}
