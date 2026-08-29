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
for (let i = 0; i < 60; i++) {
  try {
    const res = await fetch(`${base}/api/health`);
    if (res.ok && (await res.json()).bundle) break;
  } catch {
    /* not up yet */
  }
  await new Promise((r) => setTimeout(r, 1000));
}

console.log('\n── phase 2: everything else ──');
const rest = play(['--grep-invert', '@needs-db']);
process.exit(rest.status ?? 1);
