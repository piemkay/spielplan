/**
 * A vocabulary term by its name, never by its id. Spec v2.1 §6.8 ("a one-line why in vocabulary
 * terms"); decision 486.
 *
 * `era.wwii` is the key and "World War II" is the term. A payload that carries a term carries its
 * `label` from `dna_term.label`, and this renders that label; for a payload that carries only the
 * id it falls back the way the backend's `label_of` in `db/dna_terms.py` does - the id's leaf,
 * underscores as spaces - so no surface ever prints `pacing.relentless`.
 */

/**
 * @param {string | {term?: string, label?: string | null} | null | undefined} t
 * @returns {string}
 */
export function termLabel(t) {
  if (t == null) return '';
  if (typeof t === 'object') {
    const label = typeof t.label === 'string' ? t.label.trim() : '';
    return label || termLabel(t.term ?? '');
  }
  const id = String(t);
  const dot = id.indexOf('.');
  return (dot === -1 ? id : id.slice(dot + 1)).replaceAll('_', ' ');
}
