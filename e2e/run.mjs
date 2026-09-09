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
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

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

const play = (args) =>
  spawnSync('npx', ['playwright', 'test', '--config', 'playwright.config.js', ...args, ...passthrough], {
    cwd: HERE,
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });

console.log('\n── phase 0: bring the stack up (app + fake Jellyfin), then reset to first boot ──');
execFileSync('docker', [...COMPOSE, 'up', '-d'], { cwd: ROOT, stdio: 'inherit' });
execFileSync('node', [join(HERE, 'reset.mjs')], { stdio: 'inherit' });

console.log('\n── phase 1: first boot and bundle import ──');
const first = play(['specs/01-first-boot.spec.js']);
if (first.status !== 0) process.exit(first.status ?? 1);

console.log('\n── restarting so the imported bundle is loaded (§10) ──');
execFileSync('docker', [...COMPOSE, 'restart', 'backend', 'worker'], { cwd: ROOT, stdio: 'inherit' });

const base = process.env.BASE_URL ?? 'http://localhost:8080';
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
process.exit(rest.status ?? 1);
