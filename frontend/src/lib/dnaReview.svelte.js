/**
 * §6.6 Data's review of DNA rejects and low-evidence tags: two orderings, and never a filter.
 * Spec v2.1 §6.6 Data, §4.1 rule 2, §8 stage 7; decisions 341 and 446.
 *
 * THE ORDER IS THE SERVER'S, AND NOTHING IS LEFT OUT. `dna/review` returns the rejects newest first
 * and one title's extracted tags weakest first, and this module hands both on exactly as they came:
 * no sort, no cut, no predicate. §4.1 rule 2 makes the three DNA weights weights and never filters,
 * and a "hide low confidence" toggle in the browser is the same cut one layer up - the plan's risk
 * section says so in those words. `test_data_surface_guards.py` reads this file and its component
 * for every shape of that cut, including the two decision 401 records (a null test and a truthiness
 * test on a weight), so a helper that needs to know whether a figure is present asks it of a value
 * and never of a weight by name.
 *
 * THE ONE ACTION IS A LEDGER ROW. §8 stage 7: "Failures drop, never repaired", so a reject is never
 * accepted back into `dna_tag`. What an operator can do is write a verdict, and `ledgerPrefill` is
 * the hand-off to the adjudication editor on the same page (decision 445).
 */

export const REJECTS_PATH = '/admin/dna/rejects';

/** @param {number} titleId */
export function evidencePath(titleId) {
  return `/admin/dna/evidence/${titleId}`;
}

/** The rejects as the route returned them: newest first, every one. */
export function rejectRows(envelope) {
  return Array.isArray(envelope?.rejects) ? envelope.rejects : [];
}

/** One title's tags as the route returned them: weakest first, every one, NULLs where they fell. */
export function evidenceRows(envelope) {
  return Array.isArray(envelope?.tags) ? envelope.tags : [];
}

/**
 * The title id typed into the low-evidence box, or null when it is not a whole positive number.
 *
 * @param {string | number} text
 */
export function parseTitleId(text) {
  const trimmed = String(text ?? '').trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const id = Number(trimmed);
  return id >= 1 ? id : null;
}

/**
 * A figure in the data voice: two decimals for a fraction, the integer as it is, and a dash for a
 * value nobody measured. About VALUES - it is handed any column and ranks, hides and compares none.
 *
 * @param {unknown} value
 */
export function figure(value) {
  if (value === null || value === undefined) return '-';
  if (typeof value !== 'number') return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/**
 * The verdict a review row opens the adjudication editor with: the term, the title, scoped to that
 * title. The action is left for the operator to choose - DROP, REPOINT and DROP_EVIDENCE rule very
 * differently (§8 stage 7), and a default here would be a verdict nobody picked. Scoped to the title
 * even for a reject that names none (decision 396's `unknown_title`): the box is then empty for the
 * operator to fill, where a global default would turn one bad answer into a rule over every title.
 *
 * @param {{term: string, title_id?: number | null}} row
 * @param {number | null} [titleId] the title a low-evidence row belongs to
 */
export function ledgerPrefill(row, titleId = null) {
  const title = row.title_id ?? titleId;
  return { scope: 'title', term: row.term, title_id: title == null ? '' : String(title) };
}

/** A reject's title as the row names it, or the id alone for a title this install does not hold. */
export function titleOf(row) {
  if (row.name) return row.year ? `${row.name} (${row.year})` : row.name;
  return row.title_id == null ? 'no title' : `title ${row.title_id}`;
}
