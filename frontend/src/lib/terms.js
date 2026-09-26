// A term by its label, never its id; the fallback mirrors `label_of` in `db/dna_terms.py`.

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
