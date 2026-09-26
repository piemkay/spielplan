// Server order with nothing left out: the DNA figures are weights, never filters (§4.1 rule 2).
// A reject is never accepted back; the one action is a ledger row.

export const REJECTS_PATH = '/admin/dna/rejects';

/** @param {number} titleId */
export function evidencePath(titleId) {
  return `/admin/dna/evidence/${titleId}`;
}

export function rejectRows(envelope) {
  return Array.isArray(envelope?.rejects) ? envelope.rejects : [];
}

export function evidenceRows(envelope) {
  return Array.isArray(envelope?.tags) ? envelope.tags : [];
}

/** @param {string | number} text */
export function parseTitleId(text) {
  const trimmed = String(text ?? '').trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const id = Number(trimmed);
  return id >= 1 ? id : null;
}

/** @param {unknown} value */
export function figure(value) {
  if (value === null || value === undefined) return '-';
  if (typeof value !== 'number') return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/**
 * The action is left to the operator, and a reject naming no title stays title-scoped with an
 * empty box: a global default would turn one bad answer into a rule over every title.
 *
 * @param {{term: string, title_id?: number | null}} row
 * @param {number | null} [titleId] the title a low-evidence row belongs to
 */
export function ledgerPrefill(row, titleId = null) {
  const title = row.title_id ?? titleId;
  return { scope: 'title', term: row.term, title_id: title == null ? '' : String(title) };
}

export function titleOf(row) {
  if (row.name) return row.year ? `${row.name} (${row.year})` : row.name;
  return row.title_id == null ? 'no title' : `title ${row.title_id}`;
}
