/**
 * §6.6 Data's three ledger editors: each ledger's routes, fields and form rules, and the one
 * hand-off the reject review makes into the verdict editor. Spec v2.1 §6.6 Data, §8 stages 3 and 7,
 * §6.4; decisions 173, 326, 342 and 445.
 *
 * THREE LEDGERS, THREE ENTRIES, AND NO SHARED WRITE. Proposal 105, adopted as provenance for §6.6
 * Data under decision 445, is "three separate editors with separate semantics, never one merged
 * screen": DNA verdicts apply at ingest, credit facts apply last at every derive, an axis is read
 * live. The server keeps it with three modules that share no function; this module keeps it by
 * giving each ledger its own paths and its own validator, so the one component that renders all
 * three (`LedgerEditor.svelte`, mounted once per ledger) cannot post one ledger's form to another's
 * route. What the three share is rendering, which is not a write path.
 *
 * THE SERVER IS THE AUTHORITY ON EVERY RULE. The validators below catch what a form can see - a
 * missing field, a title id that is not a number, a weight outside -1..1 - so the operator is not
 * sent round the network for a typo; everything they cannot see (a title this library does not
 * hold, a REPOINT onto a term the vocabulary lacks, a bundle row) is `curated/`'s refusal, shown as
 * the editor composed it. Each sentence here is ASCII because a failing vitest prints it.
 */

/** The corpus's own verdict spellings (`curated/adjudications.ACTIONS`), so the export folds back. */
export const VERDICT_ACTIONS = ['DROP', 'REPOINT', 'DROP_EVIDENCE'];

/** The two scopes the editor writes (`curated/adjudications.SCOPES`). */
export const VERDICT_SCOPES = ['title', 'global'];

/** The credit kinds the derive applies (`derive/ledgers.CORRECTION_KINDS`); the server refuses others. */
export const CORRECTION_KINDS = ['composer', 'composer_add'];

/**
 * Said before a DROP is saved. §8 stage 7: failures drop and are never repaired, and withdrawing a
 * verdict stops it applying without restoring what it dropped (decision 445) - so a verdict that
 * cannot be taken back by withdrawing it has to say so first.
 */
export const DROP_WARNING =
  'A dropped tag is gone until the title is extracted again (section 8 stage 7). Withdrawing ' +
  'this verdict later stops it applying and brings nothing back.';

/**
 * Said before a DROP_EVIDENCE is saved, for DROP_WARNING's reason and one more. The applier matches
 * the quote as a case-insensitive SUBSTRING of every quote the term has on the title and drops a
 * tag left with no quote (`derive/ledgers._drop_evidence`, §4.1 rule 1), so a short phrase is the
 * empty quote `curated/adjudications` refuses under another spelling: every quote gone, and the tag
 * with them. [M5.6 review cycle 1, m56-curated-03]
 */
export const DROP_EVIDENCE_WARNING =
  'The quote is matched as case-insensitive text inside each quote this term has on the title, so ' +
  'a short phrase drops every quote that contains it, and a tag with no quote left is dropped with ' +
  'them (section 4.1 rule 1). Neither comes back until the title is extracted again (section 8 ' +
  'stage 7), and withdrawing this verdict later brings nothing back.';

/**
 * Said before a REPOINT is saved. Where the title already carries the target the two tags merge and
 * the source row is deleted (`derive/ledgers._repoint`), and a withdrawn verdict restores nothing it
 * did (decision 445) - so a merge is not undone by withdrawing it. [M5.6 review cycle 1,
 * m56-curated-03]
 */
export const REPOINT_WARNING =
  'Each tag this verdict rules on moves onto the new term, and where a title already carries that ' +
  'term the two are merged into one tag holding both quotes. Withdrawing this verdict later stops ' +
  'it applying and moves nothing back: a merged tag stays merged until the title is extracted ' +
  'again (section 8 stage 7).';

const VERDICT_WARNINGS = {
  DROP: DROP_WARNING,
  REPOINT: REPOINT_WARNING,
  DROP_EVIDENCE: DROP_EVIDENCE_WARNING
};

/**
 * What the verdict form says before its save, for the action chosen; null before one is. All
 * three, because withdrawing any verdict stops it applying and undoes nothing it did (decision 445).
 *
 * @param {string} action
 */
export function verdictWarning(action) {
  return VERDICT_WARNINGS[String(action ?? '').trim().toUpperCase()] ?? null;
}

/**
 * Said while a household axis is open for editing. A save replaces the facet's whole axis -
 * `curated/axes.author` clears its terms and writes what was posted, decision 261's rule carried
 * from the loader to the editor - so the form holds every term the axis has and says so.
 * [M5.6 review cycle 1, m56-curated-02]
 */
export const AXIS_REPLACE_NOTE =
  'Saving replaces this axis whole: the poles and terms below become the axis, and a term you ' +
  'remove here stops turning it.';

/**
 * Said before a `composer` correction is saved and again before one is withdrawn. The applier
 * replaces every music credit the title carries (`derive/ledgers.apply_corrections`) and a
 * withdrawal reclaims only the credit it minted: the replaced one comes back only from a derive,
 * and a bundle title has no raw store to derive from (decision 162) and is never re-derived
 * (decision 445). So on most of a library, withdrawing a mistaken one leaves no composer at all.
 * [M5.6 review cycle 1, m56-curated-01]
 */
export const COMPOSER_WARNING =
  'A composer correction replaces every music credit this title carries. Withdrawing it brings ' +
  'none of them back: a title from the bundle has no raw store to derive them from again, so its ' +
  'replaced credit is gone for good, and an acquired title gets its back only when it is derived ' +
  'again (retry it from stage 3 on the board). Note the credit you are replacing first.';

/**
 * Each ledger's routes under `/api`, and the export link, which is a plain relative href the
 * browser downloads rather than a fetch - the file is the importer's own, and a household saves it.
 */
export const LEDGERS = {
  adjudications: {
    heading: 'DNA verdicts',
    artifact: 'adjudications_v1.tsv',
    path: '/admin/curated/adjudications',
    add: 'Add a verdict',
    save: 'Save verdict',
    empty: 'No verdict at the active vocabulary yet.'
  },
  corrections: {
    heading: 'Credit corrections',
    artifact: 'corrections_v1.tsv',
    path: '/admin/curated/corrections',
    add: 'Add a correction',
    save: 'Save correction',
    empty: 'No credit correction yet.'
  },
  axes: {
    heading: 'Per-facet axes',
    artifact: '<facet>.tsv',
    path: '/admin/curated/axes',
    add: 'Add an axis',
    save: 'Save axis',
    replace: 'Replace axis',
    empty: 'No facet has an axis.'
  }
};

/** @param {string} ledger */
export function ledgerOf(ledger) {
  const found = LEDGERS[ledger];
  if (!found) throw new Error(`there is no ledger named ${ledger}`);
  return found;
}

/**
 * The rows a list route returned, in its order: the household's first, because that is the row
 * that takes effect (decision 423). The axes route answers `{facets, axes}` rather than `{rows}`.
 */
export function rowsOf(ledger, envelope) {
  const rows = ledger === 'axes' ? envelope?.axes : envelope?.rows;
  return Array.isArray(rows) ? rows : [];
}

/** A row's identity: its id, or for an axis its facet (one axis per facet). */
export function rowKey(ledger, row) {
  return ledger === 'axes' ? row.facet : row.id;
}

/** The DELETE path that withdraws one household row. */
export function withdrawPath(ledger, row) {
  const base = ledgerOf(ledger).path;
  return ledger === 'axes' ? `${base}/${encodeURIComponent(row.facet)}` : `${base}/${row.id}`;
}

/**
 * The export link: one per ledger for verdicts and corrections, one per household axis for the
 * axes, because §6.4's file is `<facet>.tsv` and a household axis is exported as the file
 * `load_axes` would read back. Relative and under `/api`, like every URL this client builds.
 */
export function exportHref(ledger, row = null) {
  const base = `/api${ledgerOf(ledger).path}`;
  if (ledger !== 'axes') return `${base}/export`;
  return `${base}/${encodeURIComponent(row.facet)}/export`;
}

/** A household row may be withdrawn; a bundle row is read-only in the app (decision 445). */
export function withdrawable(row) {
  return row?.origin === 'household';
}

/** A blank form for one ledger's fields. */
export function emptyForm(ledger) {
  if (ledger === 'adjudications') {
    return {
      scope: 'title', term: '', action: '', title_id: '', target: '', quote: '', source: '', note: ''
    };
  }
  if (ledger === 'corrections') {
    return { title_id: '', kind: CORRECTION_KINDS[0], value: '', evidence: '', note: '' };
  }
  return { facet: '', left_pole: '', right_pole: '', weights: '' };
}

/**
 * Which fields a form shows, in order. `when` hides a field its other fields make meaningless -
 * a title for a global verdict, a target for anything but REPOINT, a quote for anything but
 * DROP_EVIDENCE - and a hidden field is sent as null, so a value typed before the choice changed
 * cannot ride along into a row that means something else.
 */
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
  ],
  axes: [
    { name: 'facet', label: 'Facet', type: 'facet' },
    { name: 'left_pole', label: 'Left pole', type: 'text' },
    { name: 'right_pole', label: 'Right pole', type: 'text' },
    { name: 'weights', label: 'Terms and weights, one "term weight" per line', type: 'textarea' }
  ]
};

/** The fields a form shows now. */
export function visibleFields(ledger, form) {
  return FIELDS[ledger].filter((field) => !field.when || field.when(form));
}

/**
 * The facets the axis form offers. Adding offers only a facet with no axis at all, because a save
 * replaces the facet's whole axis: a household that picked a facet it had already authored and
 * typed the one term it meant to add lost every other term, with nothing on screen saying so. A
 * bundle facet is left out for the same reason and because the server refuses it anyway. Editing
 * offers only the facet being edited, so the form it prefilled cannot be saved over another one.
 * [M5.6 review cycle 1, m56-curated-02]
 *
 * @param {string[]} facets the declared facets, in the vocabulary's order
 * @param {{facet: string}[]} axes every axis at the version, household and bundle
 * @param {string | null} replacing the facet whose household axis is open for editing
 */
export function facetChoices(facets, axes, replacing = null) {
  if (replacing != null) return [replacing];
  const taken = new Set((axes ?? []).map((axis) => axis.facet));
  return (facets ?? []).filter((facet) => !taken.has(facet));
}

/**
 * A stored weight as the form writes it: the shortest decimal that reads back as the same float4.
 * `dna_axis_weight.weight` is a real, so 0.3 arrives as 0.30000001192092896, and a prefilled form
 * printing that would save the same number under a spelling nobody typed.
 *
 * @param {number} weight
 */
export function weightText(weight) {
  const n = Number(weight);
  if (!Number.isFinite(n)) return String(weight);
  for (let digits = 1; digits <= 9; digits += 1) {
    const text = String(Number(n.toPrecision(digits)));
    if (Math.fround(Number(text)) === Math.fround(n)) return text;
  }
  return String(n);
}

/** A household axis as the form edits it: its poles, and every term on its own "term weight" line. */
export function axisForm(row) {
  return {
    facet: row.facet,
    left_pole: row.left_pole ?? '',
    right_pole: row.right_pole ?? '',
    weights: (row.weights ?? []).map((w) => `${w.term} ${weightText(w.weight)}`).join('\n')
  };
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
 * An axis's rows as the textarea holds them: one "term weight" per line, blank lines skipped.
 * A term is one token, as every vocabulary term is (`mood.dread`); the weight runs -1 to 1, which
 * is `curated/axes`'s range and `load_axes`'.
 */
export function parseWeights(textarea) {
  const weights = [];
  const seen = new Set();
  const lines = String(textarea ?? '').split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line === '') continue;
    const parts = line.split(/\s+/);
    if (parts.length !== 2) return refuse(`Line ${i + 1} is not one term and one number.`);
    const [term, raw] = parts;
    const weight = Number(raw);
    if (!Number.isFinite(weight) || weight < -1 || weight > 1) {
      return refuse(`The number for ${term} is ${raw}; an axis runs from -1 to 1.`);
    }
    if (seen.has(term)) return refuse(`${term} appears twice on this axis; give it one number.`);
    seen.add(term);
    weights.push({ term, weight });
  }
  if (weights.length === 0) return refuse('An axis carries at least one term and its weight.');
  return { ok: true, weights };
}

function axis(form) {
  const facet = text(form.facet);
  if (facet === null) return refuse('Choose the facet the axis turns.');
  const left = text(form.left_pole);
  const right = text(form.right_pole);
  if (left === null || right === null) return refuse('An axis names both of its poles.');
  const parsed = parseWeights(form.weights);
  if (!parsed.ok) return parsed;
  return { ok: true, body: { facet, left_pole: left, right_pole: right, weights: parsed.weights } };
}

/**
 * One ledger's form checked and turned into the body its own route takes, or the reason it cannot
 * be sent. Three validators and a dispatch on the ledger name, never a shared body builder.
 *
 * @param {string} ledger
 * @param {object} form
 * @returns {{ok: boolean, body?: any, reason?: string}}
 */
export function validate(ledger, form) {
  if (ledger === 'adjudications') return verdict(form);
  if (ledger === 'corrections') return correction(form);
  if (ledger === 'axes') return axis(form);
  throw new Error(`there is no ledger named ${ledger}`);
}

/**
 * The reject review's hand-off into the verdict editor: the page's one piece of shared state.
 *
 * `DnaRejects` and the verdict editor are siblings on /admin/data, and "Write a ledger row" has to
 * open the one with the other's row in it. A counter beside the prefill so the same row tapped twice
 * still re-opens a form the operator has since cancelled.
 */
export const verdictDraft = $state({ seq: 0, prefill: null });

/** @param {{scope: string, term: string, title_id: string}} prefill */
export function openVerdict(prefill) {
  verdictDraft.prefill = { ...prefill };
  verdictDraft.seq += 1;
}
