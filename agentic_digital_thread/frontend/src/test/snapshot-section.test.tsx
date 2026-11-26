// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Snapshot section (Settings tab) — export / import / reconcile-repair UI
 * against agents/graph_snapshot_routes.py.
 *
 * Pins (each kills a named mutation):
 * - export walks EVERY cursor page and assembles the CLI file shape (edge
 *   endpoints as positional indexes, gids dropped) before the Blob download
 *   (mutation: stopping at the first chunk / shipping gids / wrong filename);
 * - the Import button stays disabled until the operator types the literal
 *   confirm token (mutation: relaxing the type-to-confirm gate);
 * - import POSTs {confirm, snapshot} and renders the reconcile table from the
 *   polled done status (mutation: wrong body / swallowing the report);
 * - a husk row is highlighted and unlocks "Repair now"; repair POSTs
 *   {repair:true} and re-renders report_after (mutation: repair body flag,
 *   dropping the after-report re-render). */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SnapshotSection, {
  CONFIRM_TOKEN,
  SNAPSHOT_FORMAT,
} from '../pages/admin/tabs/settings/SnapshotSection';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (URL as unknown as Record<string, unknown>).createObjectURL;
  delete (URL as unknown as Record<string, unknown>).revokeObjectURL;
});

async function renderSection() {
  await act(async () => {
    root.render(<SnapshotSection />);
    await Promise.resolve();
  });
}

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

/** Route-table fetch stub: GETs keyed by full url, POSTs keyed by url. */
function stubFetch(handler: (url: string, method: string, body: unknown) => unknown): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as unknown) : undefined;
      calls.push({ url, method, body });
      return response(handler(url, method, body));
    }),
  );
  return calls;
}

function click(id: string) {
  return act(async () => {
    document.getElementById(id)!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    // A macrotask flush lets the whole fetch/poll microtask chain settle
    // inside act (the stubbed fetch resolves in microtasks).
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** Load a snapshot file into the import input (jsdom File lacks .text();
 * the component reads through FileReader, which jsdom supports). */
async function pickFile(json: string, name = 'snap.json') {
  const input = document.getElementById('snapshot-import-file') as HTMLInputElement;
  const file = new File([json], name, { type: 'application/json' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
    // FileReader fires on a queued task — flush it inside act.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  // FileReader resolves asynchronously — wait for the parse to land.
  await vi.waitFor(() => {
    const status = document.getElementById('snapshot-import-status')!.textContent ?? '';
    const counts = document.getElementById('snapshot-import-counts');
    if (!counts && !status.includes('Invalid') && !status.includes('Could not')) {
      throw new Error('file not parsed yet');
    }
  });
}

const VALID_SNAPSHOT = {
  format: SNAPSHOT_FORMAT,
  exported_at: '2026-08-06T00:00:00Z',
  node_count: 2,
  edge_count: 1,
  nodes: [
    { labels: ['System'], props: { id: 'erp' } },
    { labels: ['Asset'], props: { id: 'a1' } },
  ],
  edges: [{ type: 'HAS_PARENT', from: 1, to: 0, props: {} }],
};

const HEALTHY_ROW = { system_id: 'erp', runtime_ok: true, target_ok: true, healthy: true };
const HUSK_ROW = { system_id: 'mes', runtime_ok: false, target_ok: true, healthy: false };

function cursorOf(url: string): string {
  return new URL(url, 'http://localhost').searchParams.get('cursor') ?? '';
}

describe('snapshot export', () => {
  it('walks every cursor page, assembles the CLI file shape, and downloads it', async () => {
    const chunks: Record<string, unknown> = {
      '': {
        format: SNAPSHOT_FORMAT,
        kind: 'nodes',
        items: [
          { gid: 'g1', labels: ['System'], props: { id: 'erp' } },
          { gid: 'g2', labels: ['Asset'], props: { id: 'a1' } },
        ],
        next_cursor: 'nodes:2',
      },
      'nodes:2': {
        format: SNAPSHOT_FORMAT,
        kind: 'nodes',
        items: [{ gid: 'g3', labels: ['Property'], props: { name: 'temp' } }],
        next_cursor: 'edges:0',
      },
      'edges:0': {
        format: SNAPSHOT_FORMAT,
        kind: 'edges',
        items: [{ type: 'HAS_PARENT', from_gid: 'g2', to_gid: 'g1', props: { note: 'x' } }],
        next_cursor: null,
      },
    };
    stubFetch((url) => {
      const chunk = chunks[cursorOf(url)];
      if (!chunk) throw new Error(`unexpected cursor: ${cursorOf(url)}`);
      return chunk;
    });

    // Capture the download through the Blob constructor (jsdom's Blob has no
    // .text()) and neuter the anchor plumbing.
    const parts: BlobPart[][] = [];
    const OrigBlob = globalThis.Blob;
    vi.stubGlobal(
      'Blob',
      class extends OrigBlob {
        constructor(p: BlobPart[] = [], o?: BlobPropertyBag) {
          super(p, o);
          parts.push(p);
        }
      },
    );
    Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:test'), revokeObjectURL: vi.fn() });
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this.download);
    });

    await renderSection();
    await click('snapshot-export-btn');
    await vi.waitFor(() =>
      expect(document.getElementById('snapshot-export-status')!.textContent).toContain(
        '✓ Exported 3 nodes / 1 edges',
      ),
    );

    expect(downloads).toHaveLength(1);
    expect(downloads[0]).toMatch(/^adt-graph-snapshot-\d{4}-\d{2}-\d{2}\.json$/);
    expect(parts).toHaveLength(1);
    const snapshot = JSON.parse(String(parts[0][0])) as Record<string, unknown>;
    expect(snapshot.format).toBe(SNAPSHOT_FORMAT);
    expect(snapshot.node_count).toBe(3);
    expect(snapshot.edge_count).toBe(1);
    // Edge endpoints are positional indexes; gids never land in the file.
    expect(snapshot.edges).toEqual([{ type: 'HAS_PARENT', from: 1, to: 0, props: { note: 'x' } }]);
    expect(JSON.stringify(snapshot)).not.toContain('gid');
    expect((snapshot.nodes as unknown[])[2]).toEqual({
      labels: ['Property'],
      props: { name: 'temp' },
    });
  });

  it('surfaces an export error inline without downloading', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response({ detail: 'graph unavailable' }, 500)),
    );
    const click2 = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    await renderSection();
    await click('snapshot-export-btn');
    await vi.waitFor(() =>
      expect(document.getElementById('snapshot-export-status')!.textContent).toContain(
        'Export failed: graph unavailable',
      ),
    );
    expect(click2).not.toHaveBeenCalled();
  });
});

describe('snapshot import', () => {
  it('shows counts after parsing and keeps Import disabled until the exact confirm text', async () => {
    stubFetch(() => ({}));
    await renderSection();
    await pickFile(JSON.stringify(VALID_SNAPSHOT));

    expect(document.getElementById('snapshot-import-counts')!.textContent).toContain(
      '2 nodes / 1 edges',
    );
    const btn = document.getElementById('snapshot-import-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);

    const confirmInput = document.getElementById('snapshot-import-confirm') as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    const type = (text: string) =>
      act(async () => {
        setValue.call(confirmInput, text);
        confirmInput.dispatchEvent(new Event('input', { bubbles: true }));
        await Promise.resolve();
      });

    await type('wipe-and-restor'); // near miss stays disabled
    expect(btn.disabled).toBe(true);
    await type(CONFIRM_TOKEN);
    expect(btn.disabled).toBe(false);
  });

  it('rejects a file with the wrong format field', async () => {
    stubFetch(() => ({}));
    await renderSection();
    await pickFile(JSON.stringify({ ...VALID_SNAPSHOT, format: 'other/9' }));
    expect(document.getElementById('snapshot-import-status')!.textContent).toContain(
      'unsupported snapshot format',
    );
    expect(document.getElementById('snapshot-import-btn')).toBeNull();
  });

  it('POSTs {confirm, snapshot}, polls to done, and renders the reconcile table', async () => {
    const calls = stubFetch((url, method) => {
      if (method === 'POST' && url === '/api/admin/graph/snapshot/import') {
        return { status: 'started' };
      }
      if (url === '/api/admin/graph/snapshot/import/status') {
        return { state: 'done', result: { reconcile: [HEALTHY_ROW, HUSK_ROW] } };
      }
      throw new Error(`unexpected ${method}: ${url}`);
    });
    await renderSection();
    await pickFile(JSON.stringify(VALID_SNAPSHOT));

    const confirmInput = document.getElementById('snapshot-import-confirm') as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    await act(async () => {
      setValue.call(confirmInput, CONFIRM_TOKEN);
      confirmInput.dispatchEvent(new Event('input', { bubbles: true }));
      await Promise.resolve();
    });
    await click('snapshot-import-btn');

    await vi.waitFor(() =>
      expect(document.getElementById('snapshot-import-status')!.textContent).toContain(
        '✓ Import complete',
      ),
    );
    const post = calls.find((c) => c.method === 'POST' && c.url === '/api/admin/graph/snapshot/import');
    expect(post).toBeTruthy();
    expect(post!.body).toEqual({ confirm: CONFIRM_TOKEN, snapshot: VALID_SNAPSHOT });

    const table = document.getElementById('snapshot-report-table')!;
    const rows = Array.from(table.querySelectorAll('tbody tr'));
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('erp');
    expect(rows[0].getAttribute('data-husk')).toBe('false');
    // Husk row is highlighted and named HUSK.
    expect(rows[1].getAttribute('data-husk')).toBe('true');
    expect(rows[1].textContent).toContain('mes');
    expect(rows[1].textContent).toContain('HUSK');
  });
});

describe('reconcile and repair', () => {
  it('Check system health renders the report table; healthy report shows no Repair button', async () => {
    stubFetch((url, method, body) => {
      if (method === 'POST' && url === '/api/admin/graph/snapshot/reconcile') {
        expect(body).toEqual({ repair: false });
        return { report: [HEALTHY_ROW] };
      }
      throw new Error(`unexpected ${method}: ${url}`);
    });
    await renderSection();
    await click('snapshot-reconcile-btn');
    await vi.waitFor(() =>
      expect(document.getElementById('snapshot-reconcile-status')!.textContent).toContain(
        '✓ All systems healthy',
      ),
    );
    expect(document.getElementById('snapshot-report-table')).toBeTruthy();
    expect(document.getElementById('snapshot-repair-btn')).toBeNull();
  });

  it('a husk report shows Repair now; repair POSTs {repair:true} and re-renders report_after', async () => {
    let repairStatusPolls = 0;
    const calls = stubFetch((url, method, body) => {
      if (method === 'POST' && url === '/api/admin/graph/snapshot/reconcile') {
        if ((body as { repair?: boolean }).repair === true) {
          return { status: 'started', report: [HUSK_ROW] };
        }
        return { report: [HUSK_ROW] };
      }
      if (url === '/api/admin/graph/snapshot/reconcile/status') {
        repairStatusPolls += 1;
        return {
          state: 'done',
          result: {
            repaired: ['mes'],
            healthy: [],
            report_after: [{ ...HUSK_ROW, runtime_ok: true, healthy: true }],
          },
        };
      }
      throw new Error(`unexpected ${method}: ${url}`);
    });
    await renderSection();
    await click('snapshot-reconcile-btn');
    await vi.waitFor(() => expect(document.getElementById('snapshot-repair-btn')).toBeTruthy());

    await click('snapshot-repair-btn');
    await vi.waitFor(() =>
      expect(document.getElementById('snapshot-reconcile-status')!.textContent).toContain(
        '✓ Repaired: mes',
      ),
    );
    const repairPost = calls.find(
      (c) =>
        c.method === 'POST' &&
        c.url === '/api/admin/graph/snapshot/reconcile' &&
        (c.body as { repair?: boolean }).repair === true,
    );
    expect(repairPost).toBeTruthy();
    expect(repairStatusPolls).toBeGreaterThan(0);

    // report_after re-rendered: the mes row is healthy now, Repair disappears.
    const rows = Array.from(
      document.getElementById('snapshot-report-table')!.querySelectorAll('tbody tr'),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].getAttribute('data-husk')).toBe('false');
    expect(rows[0].textContent).toContain('healthy');
    expect(document.getElementById('snapshot-repair-btn')).toBeNull();
  });

  it('surfaces a repair job error inline', async () => {
    stubFetch((url, method, body) => {
      if (method === 'POST' && url === '/api/admin/graph/snapshot/reconcile') {
        return (body as { repair?: boolean }).repair === true
          ? { status: 'started', report: [HUSK_ROW] }
          : { report: [HUSK_ROW] };
      }
      if (url === '/api/admin/graph/snapshot/reconcile/status') {
        return { state: 'error', error: 'RuntimeError: provisioning failed' };
      }
      throw new Error(`unexpected ${method}: ${url}`);
    });
    await renderSection();
    await click('snapshot-reconcile-btn');
    await vi.waitFor(() => expect(document.getElementById('snapshot-repair-btn')).toBeTruthy());
    await click('snapshot-repair-btn');
    await vi.waitFor(() =>
      expect(document.getElementById('snapshot-reconcile-status')!.textContent).toContain(
        'Repair failed: RuntimeError: provisioning failed',
      ),
    );
  });
});
