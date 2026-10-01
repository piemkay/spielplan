// Two ledgers with separate semantics (decision 445): each has its own paths and validator, so
// no form posts to another ledger's route. The server is the authority; these catch form typos.
// The sentences are ASCII because a failing vitest prints them.

/** The corpus's own verdict spellings (`curated/adjudications.ACTIONS`), so the export folds back. */
export const VERDICT_ACTIONS = ['DROP', 'REPOINT', 'DROP_EVIDENCE'];

/** The two scopes the editor writes (`curated/adjudications.SCOPES`). */
export const VERDICT_SCOPES = ['title', 'global'];

/** The credit kinds the derive applies (`derive/ledgers.CORRECTION_KINDS`); the server refuses others. */
export const CORRECTION_KINDS = ['composer', 'composer_add'];

// Withdrawing a verdict restores nothing it dropped (decision 445), so the form warns first.
export const DROP_WARNING =
  'A dropped tag is gone until the title is extracted again. Withdrawing this verdict later ' +
  'stops it applying and brings nothing back.';

// The quote matches as a case-insensitive substring, so a short phrase can drop every quote.
export const DROP_EVIDENCE_WARNING =
  'The quote is matched as case-insensitive text inside each quote this term has on the title, so ' +
  'a short phrase drops every quote that contains it, and a tag with no quote left is dropped with ' +
  'them. Neither comes back until the title is extracted again, and withdrawing this verdict ' +
  'later brings nothing back.';

// A REPOINT onto a term the title carries merges the tags, and withdrawing does not unmerge them.
export const REPOINT_WARNING =
  'Each tag this verdict rules on moves onto the new term, and where a title already carries that ' +
  'term the two are merged into one tag holding both quotes. Withdrawing this verdict later stops ' +
  'it applying and moves nothing back: a merged tag stays merged until the title is extracted ' +
  'again.';

const VERDICT_WARNINGS = {
  DROP: DROP_WARNING,
  REPOINT: REPOINT_WARNING,
  DROP_EVIDENCE: DROP_EVIDENCE_WARNING
};

/** @param {string} action */
export function verdictWarning(action) {
  return VERDICT_WARNINGS[String(action ?? '').trim().toUpperCase()] ?? null;
}

// Withdrawing a composer correction restores no credit: a bundle title is never re-derived.
export const COMPOSER_WARNING =
  'A composer correction replaces every music credit this title carries. Withdrawing it brings ' +
  'none of them back: a title from the bundle has no raw store to derive them from again, so its ' +
  'replaced credit is gone for good, and an acquired title gets its back only when it is derived ' +
  'again (retry it from Work out features in New titles). Note the credit you are replacing first.';

// The export link is a plain href the browser downloads: the file is the importer's own.
export const LEDGERS = {
  adjudications: {
    heading: 'Tag verdicts',
    artifact: 'adjudications_v1.tsv',
    path: '/admin/curated/adjudications',
    add: 'Add a verdict',
    save: 'Save verdict',
    empty: 'No verdict yet.'
  },
  corrections: {
    heading: 'Credit corrections',
    artifact: 'corrections_v1.tsv',
    path: '/admin/curated/corrections',
    add: 'Add a correction',
    save: 'Save correction',
    empty: 'No credit correction yet.'
  }
};

/** @param {string} ledger */
export function ledgerOf(ledger) {
  const found = LEDGERS[ledger];
  if (!found) throw new Error(`there is no ledger named ${ledger}`);
  return found;
}

export function withdrawPath(ledger, row) {
  return `${ledgerOf(ledger).path}/${row.id}`;
}

export function exportHref(ledger) {
  return `/api${ledgerOf(ledger).path}/export`;
}

/** A bundle row is read-only in the app (decision 445). */
export function withdrawable(row) {
  return row?.origin === 'household';
}

export function emptyForm(ledger) {
  if (ledger === 'adjudications') {
    return {
      scope: 'title', term: '', action: '', title_id: '', target: '', quote: '', source: '', note: ''
    };
  }
  return { title_id: '', kind: CORRECTION_KINDS[0], value: '', evidence: '', note: '' };
}

// A hidden field is sent as null, so a value typed before the choice changed cannot ride along.
export const FIELDS = {
  adjudications: [
    { name: 'action', label: 'Action', type: 'select', options: VERDICT_ACTIONS },
    { name: 'scope', label: 'Scope', type: 'select', options: VERDICT_SCOPES },
    { name: 'term', label: 'Term', type: 'text' },
    { name: 'title_id', label: 'Title id', type: 'text', when: (f) => f.scope === 'title' },
    { name: 'target', label: 'Repoint onto term', type: 'text', when: (f) => f.action === 'REPOINT' },
    { name: 'quote', label: 'Quote to drop', type: 'text', when: (f) => f.action === 'DROP_EVIDENCE' },
    { name: 'source', label: 'Source', type: 'text' },
    { name: 'note', label: 'Note', type: 'text' }
  ],
  corrections: [
    { name: 'title_id', label: 'Title id', type: 'text' },
    { name: 'kind', label: 'Kind', type: 'select', options: CORRECTION_KINDS },
    { name: 'value', label: 'Credit', type: 'text' },
    { name: 'evidence', label: 'Evidence', type: 'text' },
    { name: 'note', label: 'Note', type: 'text' }
  ]
};

export function visibleFields(ledger, form) {
  return FIELDS[ledger].filter((field) => !field.when || field.when(form));
}

function text(value) {
  const trimmed = String(value ?? '').trim();
  return trimmed === '' ? null : trimmed;
}

function titleId(value) {
  const trimmed = String(value ?? '').trim();
  return /^\d+$/.test(trimmed) && Number(trimmed) >= 1 ? Number(trimmed) : null;
}

function refuse(reason) {
  return { ok: false, reason };
}

function verdict(form) {
  const action = String(form.action ?? '').trim().toUpperCase();
  const scope = String(form.scope ?? '').trim().toLowerCase();
  if (!VERDICT_ACTIONS.includes(action)) return refuse('Choose DROP, REPOINT or DROP_EVIDENCE.');
  if (!VERDICT_SCOPES.includes(scope)) {
    return refuse('Choose whether the verdict rules on one title or on every title with the term.');
  }
  const term = text(form.term);
  if (term === null) return refuse('A verdict names the term it rules on.');
  const title = scope === 'title' ? titleId(form.title_id) : null;
  if (scope === 'title' && title === null) {
    return refuse('A title verdict names the title it rules on, as a number.');
  }
  const target = action === 'REPOINT' ? text(form.target) : null;
  if (action === 'REPOINT' && target === null) {
    return refuse('REPOINT names the vocabulary term the tag moves onto.');
  }
  const quote = action === 'DROP_EVIDENCE' ? text(form.quote) : null;
  if (action === 'DROP_EVIDENCE' && scope !== 'title') {
    return refuse("DROP_EVIDENCE rules on one title's quote, so it names the title.");
  }
  if (action === 'DROP_EVIDENCE' && quote === null) {
    return refuse('DROP_EVIDENCE names the quote it drops.');
  }
  return {
    ok: true,
    body: {
      scope, term, action, title_id: title, target, quote, source: text(form.source), note: text(form.note)
    }
  };
}

function correction(form) {
  const title = titleId(form.title_id);
  if (title === null) return refuse('A correction names the title it corrects, as a number.');
  const kind = String(form.kind ?? '').trim();
  if (!CORRECTION_KINDS.includes(kind)) {
    return refuse(`A correction's kind is ${CORRECTION_KINDS.join(' or ')}.`);
  }
  const value = text(form.value);
  if (value === null) return refuse('A correction names the credit it asserts.');
  const evidence = text(form.evidence);
  if (evidence === null) {
    return refuse('A correction carries the evidence that settles it; the derive refuses one without.');
  }
  return { ok: true, body: { title_id: title, kind, value, evidence, note: text(form.note) } };
}

/**
 * @param {string} ledger
 * @param {object} form
 * @returns {{ok: boolean, body?: any, reason?: string}}
 */
export function validate(ledger, form) {
  if (ledger === 'adjudications') return verdict(form);
  if (ledger === 'corrections') return correction(form);
  throw new Error(`there is no ledger named ${ledger}`);
}

// The reject review's hand-off into its sibling verdict editor. `seq` lets the same row tapped
// twice re-open a form the operator cancelled.
export const verdictDraft = $state({ seq: 0, prefill: null });

/** @param {{scope: string, term: string, title_id: string}} prefill */
export function openVerdict(prefill) {
  verdictDraft.prefill = { ...prefill };
  verdictDraft.seq += 1;
}
