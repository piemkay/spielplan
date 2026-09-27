/**
 * Run the whole suite from a cold start: rebuild the fixture, reset to first boot, then one
 * Playwright pass in which every project depends on `first-boot`.
 *
 *   node e2e/run.mjs [--project=desktop]
 */
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const COMPOSE = [
  'compose',
  '-f', 'docker-compose.yml',
  '-f', 'ops/compose.dev.yml',
  '-f', 'ops/compose.e2e.yml',
];
const passthrough = process.argv.slice(2);

// Decision 299: the harness rebuilds the fixture it measures, with the backend's venv and never
// whatever `python` resolves to. Developers have `backend/.venv`, CI's `uv venv` makes `.venv`, and
// a worktree may have neither, so the main checkout's (the parent of the git common dir) is tried too.
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

console.log('\n-- rebuild the fixture, bring the stack up, then reset to first boot --');
// Before the reset, which is what makes the next boot a first boot.
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
// Emptied first: `make_bundle` overlays rather than replaces, and would inventory a staged corpus
// bundle's leftovers into a hybrid that validates. The contents, never the directory (a bind
// mount; see reset.mjs), each named as it goes because a staged corpus bundle is large.
const IMPORT_DIR = join(ROOT, 'data', 'import');
mkdirSync(IMPORT_DIR, { recursive: true });
for (const entry of readdirSync(IMPORT_DIR)) {
  console.log(`  clearing data/import/${entry}`);
  try {
    rmSync(join(IMPORT_DIR, entry), { recursive: true, force: true });
  } catch (err) {
    throw new Error(
      `cannot clear ${IMPORT_DIR} (${err.code}): it holds files the container wrote as uid 1000. ` +
        'Run `sudo rm -rf data/import/*` (the contents, not the directory) and try again.'
    );
  }
}
execFileSync(PYTHON, ['-c', BUILD_FIXTURE], { cwd: ROOT, stdio: 'inherit' });
// BUILD_FIXTURE prints unconditionally, and without BUNDLE.json the importer would fall back to
// any archive lying in the directory.
if (!existsSync(join(IMPORT_DIR, 'BUNDLE.json'))) {
  console.error(
    `the fixture rebuild left no BUNDLE.json in ${IMPORT_DIR}: make_bundle writes it last, ` +
      'over the tree it has just inventoried, so a root without one is a bundle nothing built'
  );
  process.exit(1);
}
execFileSync('docker', [...COMPOSE, 'up', '-d'], { cwd: ROOT, stdio: 'inherit' });
execFileSync('node', [join(HERE, 'reset.mjs')], { stdio: 'inherit' });

const run = spawnSync('npx', ['playwright', 'test', '--config', 'playwright.config.js', ...passthrough], {
  cwd: HERE,
  stdio: 'inherit',
  shell: process.platform === 'win32',
});
process.exit(run.status ?? 1);
