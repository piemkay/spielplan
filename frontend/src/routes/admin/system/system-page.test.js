/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import SystemPage from './+page.svelte';

/** `GET /api/admin/system`'s payload. */
const card = () => ({
  backup: { at: null, bytes: null, stale: true, stale_after_hours: 36 },
  jobs: [
    {
      name: 'jellyfin-sessions-poll',
      started_at: '2026-09-24T08:00:00+00:00',
      finished_at: '2026-09-24T08:00:01+00:00',
      ok: true,
      detail: {}
    }
  ],
  secrets: { configured: true, fingerprint: '0123456789ab', key_id: 'k1', unreadable: false },
  queue: {
    by_state: { pending: 9, leased: 0, done: 4, failed: 1, skipped: 0 },
    by_kind: [
      { kind: 'acquire', state: 'done', count: 4 },
      { kind: 'acquire', state: 'failed', count: 1 },
      { kind: 'acquire', state: 'pending', count: 9 }
    ]
  },
  acquisition: { queued: 0, running: 0, parked: 0, ready: 0, failed: 0, abandoned: 0 },
  last_syncs: [
    {
      name: 'jellyfin-seen-sync',
      connector: 'Jellyfin',
      at: '2026-09-24T07:45:00+00:00',
      detail: {}
    },
    { name: 'acquisition-drain', connector: 'Metadata sources', at: null, detail: null }
  ],
  logs: {
    scope: 'web process',
    since: '2026-09-24T06:00:00+00:00',
    records: [
      { at: '2026-09-24T06:00:01+00:00', level: 'INFO', logger: 'spielplan.app', message: 'booted' },
      {
        at: '2026-09-24T07:00:00+00:00',
        level: 'WARNING',
        logger: 'spielplan.sources',
        message: 'tmdb answered 429'
      },
      {
        at: '2026-09-24T07:30:00+00:00',
        level: 'ERROR',
        logger: 'spielplan.connectors',
        message: 'jellyfin refused the key'
      }
    ]
  }
});

const CONFIG = { bundle: { version: 'v20260926b', titles: 19071 } };

let target;
let system;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  system = card();
  vi.mocked(get).mockImplementation((path) => Promise.resolve(path === '/config' ? CONFIG : system));
});

afterEach(() => {
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(SystemPage, { target });
  await settle();
  return app;
}

const $ = (selector) => target.querySelector(selector);
const messages = () =>
  [...target.querySelectorAll('[data-testid="system-logs"] li')].map((li) => li.textContent);
const systemReads = () => vi.mocked(get).mock.calls.filter(([path]) => path === '/admin/system');

describe('the System page (decisions 454, 527)', () => {
  it('speaks plainly on top and keeps the verbatim facts under Technical details', async () => {
    const app = await open();
    try {
      expect($('[data-testid="system-backup"]').textContent).toContain('Never');
      expect($('[data-testid="system-backup-stale"]').textContent).toContain('No backup has finished');
      expect($('[data-testid="system-secrets"]').textContent).toContain('Loaded');
      expect($('[data-testid="system-bundle"]').textContent).toContain('v20260926b');
      expect($('[data-testid="system-bundle"]').textContent).toContain('19,071 titles');
      expect($('[data-testid="system-queue"]').textContent.replace(/\s+/g, ' ')).toContain('9Waiting');

      const technical = $('[data-testid="system-technical"]');
      expect(technical.tagName).toBe('DETAILS');
      expect(technical.open).toBe(false);
      expect(technical.textContent).toContain('0123456789ab');
      expect(technical.textContent).toContain('jellyfin-sessions-poll');
      expect($('[data-testid="system-queue-detail"]').textContent).toContain('pending 9');
      expect($('[data-queue-kind="acquire"]').textContent).toContain('failed 1');
      expect($('[data-last-sync="acquisition-drain"]').textContent).toContain('never succeeded');
      expect($('[data-last-sync="jellyfin-seen-sync"]').textContent).toMatch(/ago/);
      // The verbatim names sit only down there: the jobs list names each job plainly.
      expect($('[data-job="jellyfin-sessions-poll"]').textContent).toContain('Playback watch');
      expect($('[data-job="jellyfin-sessions-poll"]').textContent).not.toContain('jellyfin-sessions-poll');
    } finally {
      unmount(app);
    }
  });

  it('shows warnings and errors first, newest first', async () => {
    const app = await open();
    try {
      const shown = messages();
      expect(shown).toHaveLength(2);
      expect(shown[0]).toContain('jellyfin refused the key');
      expect(shown[1]).toContain('tmdb answered 429');
      expect($('[data-testid="system-logs"]').textContent).toContain('container log');
    } finally {
      unmount(app);
    }
  });

  it('narrows or widens the log lines by level and asks the server nothing to do it', async () => {
    const app = await open();
    try {
      expect(systemReads()).toHaveLength(1);
      const select = $('[data-testid="system-logs"] select');
      for (const [value, count] of /** @type {[string, number][]} */ ([
        ['error', 1],
        ['all', 3],
        ['warning', 2]
      ])) {
        select.value = value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        flushSync();
        expect(messages(), value).toHaveLength(count);
      }
      expect(messages()[0]).toContain('jellyfin refused the key');
      expect(systemReads(), 'the filter is applied to what the one read returned').toHaveLength(1);
      expect(post).not.toHaveBeenCalled();
      expect(api).not.toHaveBeenCalled();
    } finally {
      unmount(app);
    }
  });

  it("says a data folder is not writable, with the worker's fix and without its exception class", async () => {
    system.jobs.push({
      name: 'storage-check',
      started_at: '2026-09-24T08:00:00+00:00',
      finished_at: '2026-09-24T08:00:00+00:00',
      ok: false,
      detail: {
        error:
          'RuntimeError: /data/backups is not writable by this app (PermissionError: Permission ' +
          'denied). This app runs as uid 1000 and has to write there, so give it the directories: ' +
          'on the host, in the Spielplan directory, run `sudo chown -R 1000:1000 data/backups`'
      }
    });
    const app = await open();
    try {
      const warning = $('.alert[data-storage="unwritable"]');
      expect(warning.textContent).toContain("A data folder isn't writable");
      expect(warning.textContent).toContain('sudo chown -R 1000:1000 data/backups');
      expect(warning.textContent).not.toContain('RuntimeError');
    } finally {
      unmount(app);
    }
  });

  it('says the storage check passed, or that it has not run yet, and never invents either', async () => {
    system.jobs.push({
      name: 'storage-check',
      started_at: '2026-09-24T08:00:00+00:00',
      finished_at: '2026-09-24T08:00:00+00:00',
      ok: true,
      detail: { writable: 'raw artifacts cache import backups' }
    });
    const passing = await open();
    try {
      expect($('[data-testid="system-storage"]').dataset.storage).toBe('ok');
      expect($('[data-testid="system-storage"]').textContent).toContain('All writable');
    } finally {
      unmount(passing);
    }

    system = card();
    const unchecked = await open();
    try {
      expect($('[data-testid="system-storage"]').dataset.storage).toBe('unchecked');
      expect($('[data-testid="system-storage"]').textContent).toContain('Not checked yet');
    } finally {
      unmount(unchecked);
    }
  });

  it('says so in plain words when the encryption key cannot open every saved key', async () => {
    system.secrets.unreadable = true;
    const app = await open();
    try {
      expect($('[data-testid="system-secrets"]').textContent).toContain("Can't open every key");
      const warning = $('[data-testid="system-secrets-unreadable"]');
      expect(warning.textContent).toContain('.env');
      expect(warning.textContent).toContain('spielplan-secrets reset');
    } finally {
      unmount(app);
    }
  });

  it('carries no control that writes: disclosures and the log level are all there is', async () => {
    const app = await open();
    try {
      const controls = [...target.querySelectorAll('button, input, select, [role=button]')];
      expect(controls.map((c) => c.tagName)).toEqual(['SELECT']);
      expect(target.querySelectorAll('summary')).toHaveLength(3);
    } finally {
      unmount(app);
    }
  });
});
