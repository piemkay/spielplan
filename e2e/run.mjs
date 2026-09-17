/**
 * Run the whole suite from a cold start, in the two phases the app actually has.
 *
 * §10's swap sequence ends in "restart backend + worker", so a bundle imported in phase one is
 * not *loaded* until the services come back. Without that restart between them, every spec
 * that needs an imported bundle skips — which looks like a pass and proves nothing.
 *
 *   node e2e/run.mjs [--project=desktop]
 */
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { baseUrl } from './env.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const COMPOSE = [
  'compose',
  '-f', 'docker-compose.yml',
  '-f', 'ops/compose.dev.yml',
  // §7.3's two-way sync needs a Jellyfin that answers; ops/compose.e2e.yml provides a fake.
  '-f', 'ops/compose.e2e.yml',
];
const passthrough = process.argv.slice(2);

// decision 299: the harness rebuilds the fixture it measures. Only `ci.yml` ever rebuilt
// `data/import`; locally this runner imported whatever the directory happened to hold, so the
// question "is the fixture current?" lived in a person's head — and a stale one cost this project
// six round trips in a single session. The rebuild is deterministic and costs seconds, which is
// why it is preferred over comparing mtimes and refusing: a comparison is a second thing to keep
// true and still leaves the rebuild to the person.
//
// The backend's venv, not whatever `python` resolves to. `make_bundle` builds a torch-shaped
// bundle out of the installed package, and the interpreter on PATH is a household's system
// Python as often as not. The statements below are `ci.yml`'s 'build a bundle fixture into the
// import directory' step, and `make_bundle.py` itself is not this milestone's to change — so the
// committed fixture stays valid and no rebuild is owed to anybody.
//
// TWO PLACES PER CHECKOUT, because this repository installs that interpreter in two and a harness
// that knows only one refuses on the machine it matters most on. `backend/.venv` is the one
// README's "Running it" and CLAUDE.md's command line name, and it is what a developer's box and
// this worktree have. The checkout-root `.venv` is what every `uv venv` in `.github/workflows`
// creates -- ci.yml's four jobs, release.yml's leg 5 and real-bundle.yml -- and none of them ever
// creates `backend/.venv` at all, so the first draft of this block exited 1 at its first statement
// on every runner in the project, taking ci.yml's e2e job and the release gate's last leg with it.
// Naming both is still decision 299's rule rather than a softening of it: these are the two
// interpreters this repository INSTALLS INTO, and neither is "whatever `python` resolves to".
// [M4.16 cycle 1, M416-C1-D3-04]
//
// And in the other checkout, which is `env.mjs`'s lesson one value over: the roadmap's parallel
// milestone pairs put a WORKTREE per lane on one machine, and a worktree does not necessarily
// carry its own venv -- measured on the M4.16 lane, which has none and runs its suite on the main
// checkout's. `git rev-parse --git-common-dir` names the shared `.git` whichever worktree asks, so
// its parent is that checkout. If none of the candidates exists the harness still refuses, and
// names them all.
const VENV_DIRS = [['backend', '.venv'], ['.venv']];
const venvPython = (root, dirs) => join(root, ...dirs,
  ...(process.platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python']));
const venvPythons = (root) => VENV_DIRS.map((dirs) => venvPython(root, dirs));
const mainCheckout = () => {
  try {
    const shared = execFileSync('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'],
      { cwd: ROOT, encoding: 'utf8' }).trim();
    return dirname(shared);
  } catch {
    // No git, or not a repository. The first candidate is then the only one there is, and the
    // refusal below is what reports it.
    return null;
  }
};
const MAIN = mainCheckout();
const INTERPRETERS = [...venvPythons(ROOT), ...(MAIN && MAIN !== ROOT ? venvPythons(MAIN) : [])];
const PYTHON = INTERPRETERS.find(existsSync) ?? INTERPRETERS[0];
const MAKE_BUNDLE = join(ROOT, 'backend', 'tests', 'fixtures', 'make_bundle.py');
const BUILD_FIXTURE = [
  'import pathlib, sys',
  "sys.path.insert(0, 'backend')",
  'from tests.fixtures import make_bundle as fx',
  "fx.make_bundle(pathlib.Path('data/import'))",
  "print('fixture bundle written into data/import')",
].join('; ');

// The JSON report `ops/coverage_gate.py` reads, and the one file two phases fight over. Playwright
// empties `outputDir` at the START of a run, so phase 2 deletes the report phase 1 wrote — and
// phase 1 alone closes 8 shipped rows. So phase 1's report is held here and written back beside
// phase 2's once the wipe has happened. It exists only under CI, where
// `playwright.config.js` adds the `json` reporter; a local run has no such file and must not fail
// looking for one.
const REPORT = join(HERE, '.results', 'report.json');
const PHASE_ONE_REPORT = join(HERE, '.results', 'report-phase-1.json');

const play = (args) =>
  spawnSync('npx', ['playwright', 'test', '--config', 'playwright.config.js', ...args, ...passthrough], {
    cwd: HERE,
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });

console.log('\n── phase 0: rebuild the fixture, bring the stack up, then reset to first boot ──');
// Before `reset.mjs`, because the reset is what makes the next boot a first boot: a bundle
// rebuilt after it would be a bundle the wizard has already been offered. And loudly, on both
// arms — the one thing this must never do is carry on against whatever is on disk, which is the
// state it was added to end. [decision 299]
if (!existsSync(PYTHON)) {
  console.error(
    `no backend interpreter at ${INTERPRETERS.join(' or ')}: the fixture bundle is built with ` +
      'the backend\'s own venv (see README "Running it"), and this harness will not measure a ' +
      'fixture it did not build'
  );
  process.exit(1);
}
if (!existsSync(MAKE_BUNDLE)) {
  console.error(`no fixture module at ${MAKE_BUNDLE}: there is nothing to rebuild data/import from`);
  process.exit(1);
}
// EMPTIED first, because `make_bundle` overlays rather than replaces. It `mkdir(exist_ok=True)`s
// both its root and its `artifacts/` (make_bundle.py:372, :610) and unlinks only the two sqlite
// files (:385, :543), so every other file a DIFFERENT bundle left in this directory survives the
// rebuild -- and `_inventory` (:739) rglobs the merged tree, so the BUNDLE.json written last
// describes the hybrid exactly and `validate` passes it without a word. An operator who staged
// the corpus export where README's "Importing a bundle" tells them to would have the fixture
// written over it and this gate would report its 206 passed against a tree nobody built. Before
// decision 299 the mixture could not arise, because nothing here wrote into the directory at all;
// the rebuild is what makes emptying part of the same instrument. release.yml already spends this
// line between its corpus leg and this harness, which is where the shape comes from, and
// `make_bundle.py` has 26 call sites and is not this milestone's to change -- so the clearing
// lives at the harness's altitude, next to the rebuild it belongs to.
// [decision 299; M4.16 cycle 1, M416-C1-D3-03]
//
// The contents, never the directory, for `e2e/reset.mjs`'s reason one value over: ./data/import
// is a host bind mount into the backend and the worker, so removing it hands its recreation to
// Docker -- which recreates it as root -- and leaves the running containers on the old inode.
// Named as they go, because a staged corpus bundle is somebody's 995 MB and this is the line
// that deletes it.
const IMPORT_DIR = join(ROOT, 'data', 'import');
mkdirSync(IMPORT_DIR, { recursive: true });
for (const entry of readdirSync(IMPORT_DIR)) {
  console.log(`  clearing data/import/${entry}`);
  try {
    rmSync(join(IMPORT_DIR, entry), { recursive: true, force: true });
  } catch (err) {
    // `reset.mjs`'s EACCES arm, one directory over: `.github/workflows/ci.yml` chowns this
    // mount to uid 1000 before the stack starts, and a `.unpacked-*` tree the importer left
    // behind belongs to the container. Say which command clears it while keeping the directory.
    throw new Error(
      `cannot clear ${IMPORT_DIR} (${err.code}): it holds files the container wrote as uid 1000. ` +
        'Run `sudo rm -rf data/import/*` (the contents, not the directory) and try again.'
    );
  }
}
execFileSync(PYTHON, ['-c', BUILD_FIXTURE], { cwd: ROOT, stdio: 'inherit' });
// And a post-condition on the rebuild, because the rebuild's own report is an unconditional
// `print` at the end of BUILD_FIXTURE: it says the bundle was written whether or not one was.
// `BUNDLE.json` is the file `Bundle.open` looks for, and a root without one falls through to
// `_archive_within` and imports whatever archive is lying there instead -- which is the fallback
// decision 299 exists to refuse, reached this time through a rebuild that did nothing.
if (!existsSync(join(IMPORT_DIR, 'BUNDLE.json'))) {
  console.error(
    `the fixture rebuild left no BUNDLE.json in ${IMPORT_DIR}: make_bundle writes it last, ` +
      'over the tree it has just inventoried, so a root without one is a bundle nothing built'
  );
  process.exit(1);
}
execFileSync('docker', [...COMPOSE, 'up', '-d'], { cwd: ROOT, stdio: 'inherit' });
execFileSync('node', [join(HERE, 'reset.mjs')], { stdio: 'inherit' });

console.log('\n── phase 1: first boot and bundle import ──');
const first = play(['specs/01-first-boot.spec.js']);
if (first.status !== 0) process.exit(first.status ?? 1);
const firstReport = existsSync(REPORT) ? readFileSync(REPORT) : null;

console.log('\n── restarting so the imported bundle is loaded (§10) ──');
execFileSync('docker', [...COMPOSE, 'restart', 'backend', 'worker'], { cwd: ROOT, stdio: 'inherit' });

const base = baseUrl();
// 60 attempts, and the bound stays explicit: a fixture bundle is loaded within a few of them,
// and if a real corpus bundle ever needs longer the number is raised here with a comment rather
// than by letting the loop run until something else gives up. Each attempt now carries its own
// deadline, because `fetch` has none of its own: a backend that accepts the connection and never
// answers held iteration 1 for ever, which is a budget quietly becoming no budget at all.
//
// Which is why the failure below counts attempts rather than seconds. An attempt costs its 5 s
// deadline plus the 1 s sleep, so sixty of them is a six-minute ceiling; the same sixty against
// a refused connection cost a second each and take one minute. One sentence cannot name both,
// and naming the smaller one sends the operator after a slow boot when the run measured sixty
// stalls -- the diagnosis the deadline exists to make visible. [M4.8 review cycle 2, E2E-2]
let loaded = false;
for (let i = 0; i < 60; i++) {
  try {
    const res = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(5000) });
    if (res.ok && (await res.json()).bundle) {
      loaded = true;
      break;
    }
  } catch {
    /* not up yet */
  }
  await new Promise((r) => setTimeout(r, 1000));
}
// The failure branch this loop never had. Nine spec files skip themselves when the app reports
// no bundle (`test.skip(!config.has_bundle, ...)`), so a phase 2 entered without one is a run of
// nothing but skips — which Playwright exits 0 for, and which reads from the outside as a green
// suite. §10's swap sequence ends in the restart above; if the bundle is not loaded after it,
// there is nothing for phase 2 to prove and the run has already failed.
if (!loaded) {
  console.error(
    'the restarted backend did not report a loaded bundle in 60 attempts (up to 6 minutes): ' +
      'phase 2 would only skip, which exits 0 and proves nothing (the swap sequence, spec ' +
      'section 10)'
  );
  process.exit(1);
}

console.log('\n── phase 2: everything else ──');
// `@first-boot`, not `@needs-db`: the tag names the phase that owns a file, which is the only
// distinction this line draws. The old spelling promised a database-dependence split that
// nothing implemented, so a DB-dependent spec written to the documented convention was inverted
// out of phase 2 and ran nowhere. [M4.8, finding 7]
const rest = play(['--grep-invert', '@first-boot']);
// After the run, because the run is what emptied the directory it goes back into.
if (firstReport !== null) writeFileSync(PHASE_ONE_REPORT, firstReport);
process.exit(rest.status ?? 1);
