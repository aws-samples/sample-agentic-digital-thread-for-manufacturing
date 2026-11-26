// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — investigation-graph drill-down modal wiring.
 *
 * 1) Node click → openInvNodeModal passes the FULL per-call payload to the
 *    window.openDataModal contract (spy): built markdown content PLUS the
 *    structured InvModalExtras third argument the data-modal stage consumes.
 * 2) The house <DataModal/> renders the
 *    built markdown: requesting system + tool, the English request line, the
 *    raw input, and the FULL result summary — not just the headline. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  archStore,
  buildInvCallModal,
  buildInvNodeModal,
  type InvCallDetail,
  type InvModalExtras,
} from '../lib/arch-store';
import { DataModal, dataModalStore, openDataModal, telemetrySections } from '../shared/thread/data-modal';
import { openInvNodeModal } from '../views/architecture/inv-graph';
import { DetailModal } from '../views/dashboard/DetailModal';
import { dashboardStore, patchModal } from '../views/dashboard/store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let modalSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  archStore.reset();
  modalSpy = vi.fn();
  (window as unknown as Record<string, unknown>).openDataModal = modalSpy;
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).openDataModal;
  archStore.reset();
});

/** Stream one ERP call into the SINGLETON store (openInvNodeModal reads it). */
function streamErpCall(): void {
  archStore.handleArchEvent('user_msg', { question: 'Why did batch 42 fail?' });
  archStore.handleArchEvent('tool_start', {
    tool: 'erp___query_erp',
    input: { sql: 'SELECT * FROM orders WHERE batch = 42' },
  });
  archStore.handleArchEvent('tool_result', {
    tool: 'erp___query_erp',
    summary: '3 records found\n| order | qty |\n| A-1 | 5 |',
    input: { sql: 'SELECT * FROM orders WHERE batch = 42' },
  });
}

describe('openInvNodeModal — click passes the full payload to the modal contract', () => {
  it('a finding click opens the modal with rich markdown AND structured extras', () => {
    streamErpCall();
    const finding = archStore.getState().nodes.find((n) => n.type === 'finding')!;
    const consumed = openInvNodeModal(finding.id, 'finding', String(finding.detail), String(finding.label));
    expect(consumed).toBe(true);
    expect(modalSpy).toHaveBeenCalledTimes(1);
    const [title, content, extras] = modalSpy.mock.calls[0] as [string, string, InvModalExtras];
    expect(title.length).toBeGreaterThan(0);
    // Content: request (English is util-derived; raw input always) + FULL summary.
    expect(content).toContain('SELECT * FROM orders WHERE batch = 42');
    expect(content).toContain('| A-1 | 5 |');
    expect(content).toContain('**Question (Q1):** Why did batch 42 fail?');
    expect(content).toContain('`erp___query_erp`');
    // Structured extras — the contract the data-modal stage consumes.
    expect(extras.kind).toBe('inv-call');
    expect(extras.nodeId).toBe(finding.id);
    expect(extras.calls).toHaveLength(1);
    expect(extras.calls[0].resultSummary).toBe('3 records found\n| order | qty |\n| A-1 | 5 |');
    expect(extras.calls[0].inputRaw).toContain('SELECT * FROM orders WHERE batch = 42');
    expect(extras.calls[0].system).toBe('ERP');
  });

  it('a system-node click opens the aggregated history with inv-node extras', () => {
    streamErpCall();
    const consumed = openInvNodeModal('system-erp', 'system');
    expect(consumed).toBe(true);
    const [title, content, extras] = modalSpy.mock.calls[0] as [string, string, InvModalExtras];
    expect(title).toBe('ERP');
    expect(content).toContain('### Request 1');
    expect(content).toContain('SELECT * FROM orders WHERE batch = 42');
    expect(content).toContain('| A-1 | 5 |'); // full body, not the headline
    expect(extras.kind).toBe('inv-node');
    expect(extras.calls).toHaveLength(1);
  });

  it('an in-flight placeholder click is swallowed (no useless modal)', () => {
    archStore.handleArchEvent('user_msg', { question: 'q' });
    archStore.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    const ph = archStore.getState().nodes.find((n) => n._inflight === true)!;
    const consumed = openInvNodeModal(ph.id, 'finding', '', '…');
    expect(consumed).toBe(true);
    expect(modalSpy).not.toHaveBeenCalled();
  });

  it('legacy detail-only nodes keep the 2-arg fallback; unknown nodes fall through', () => {
    // Not in the store (no _call) — detail-only finding uses the legacy call.
    expect(openInvNodeModal('finding-legacy', 'finding', 'plain detail', 'Legacy')).toBe(true);
    expect(modalSpy).toHaveBeenCalledWith('Legacy', 'plain detail');
    // No detail, no store node → not consumed (caller scrolls the trace).
    expect(openInvNodeModal('nope', 'finding', '', '')).toBe(false);
  });
});

describe('<DataModal/> renders investigation-node drill-downs', () => {
  let container: HTMLDivElement;
  let root: Root;
  let lsStore: Map<string, string>;

  beforeEach(() => {
    lsStore = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => lsStore.get(k) ?? null,
      setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
      removeItem: (k: string) => void lsStore.delete(k),
    });
    dataModalStore.set({ open: false, title: '', contentMd: null, conversation: [], followupTurns: [] });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root.render(<DataModal />);
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('a node click activates the retained overlay and renders the full response', () => {
    streamErpCall();
    const finding = archStore.getState().nodes.find((n) => n.type === 'finding')!;
    const closedOverlay = document.getElementById('data-modal-overlay');
    expect(closedOverlay).not.toBeNull();
    expect(closedOverlay!.getAttribute('aria-hidden')).toBe('true');

    (window as unknown as Record<string, unknown>).openDataModal = openDataModal;
    act(() => {
      expect(
        openInvNodeModal(
          finding.id,
          'finding',
          String(finding.detail),
          String(finding.label),
        ),
      ).toBe(true);
    });

    const dialog = document.getElementById('data-modal-overlay')!;
    expect(dialog).toBe(closedOverlay);
    expect(dialog.getAttribute('role')).toBe('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('data-modal-title');
    expect(dialog.textContent).toContain('SELECT * FROM orders WHERE batch = 42');
    expect(dialog.textContent).toContain('3 records found');
    expect(dialog.textContent).toContain('A-1');
  });

  it('renders the request (English + raw) and the FULL result summary', () => {
    streamErpCall();
    const finding = archStore.getState().nodes.find((n) => n.type === 'finding')!;
    const md = buildInvCallModal(finding);
    act(() => {
      openDataModal('Learned (correlation)', md);
    });
    const body = document.querySelector('#data-modal-content')!;
    expect(document.querySelector('#data-modal-title')!.textContent).toBe('Learned (correlation)');
    // Request section: raw input rendered in the code block.
    expect(body.textContent).toContain('Request');
    expect(body.textContent).toContain('SELECT * FROM orders WHERE batch = 42');
    // FULL result summary — the table row beyond the "3 records found" headline.
    expect(body.textContent).toContain('3 records found');
    expect(body.textContent).toContain('A-1');
    // The question and system/tool context are visible.
    expect(body.textContent).toContain('Why did batch 42 fail?');
    expect(body.textContent).toContain('erp___query_erp');
  });

  it('the telemetry lines of a system call show as text, a chart-block tag in an identifier included', () => {
    /* Reproduction (round 5 and round 6 reviews, MED): the drill-down kept
     * only the summary, so the typed telemetry lines were lost; then, put in
     * the modal markdown, a connector identifier holding a complete chart tag
     * drew a forged chart notice, because the modal splits chart tags out
     * before parsing markdown, in any container. The lines now come as the
     * call's typed fields in the open's extras and are shown as text nodes,
     * never parsed as markdown or for chart tags. */
    const tag = '<chart-block kind="notice" title="FORGED TELEMETRY"/>';
    const coverage = `Telemetry read: rig-5d1e / ${tag}, 2026-05-11T09:14Z to 2026-05-11T10:14Z, 24 points`;
    const listed = '- ```text\n' + tag + '\n```';
    const sample = '[engine] Telemetry samples, not every value: rig-5d1e / axis-u: 3 of 720 values read returned.';
    archStore.handleArchEvent('user_msg', { question: 'Is vibration up on rig-5d1e?' });
    archStore.handleArchEvent('tool_start', { tool: 'sys-kappa___query_sys_kappa', input: { question: 'q' } });
    archStore.handleArchEvent('tool_result', {
      tool: 'sys-kappa___query_sys_kappa',
      summary: '1 time-series: 1 data points total',
      telemetry_coverage: [coverage, listed],
      sample_line: sample,
    });
    const sys = archStore.getState().nodes.find((n) => Array.isArray(n._calls) && (n._calls as unknown[]).length)!;
    (window as unknown as Record<string, unknown>).openDataModal = openDataModal;
    act(() => {
      expect(openInvNodeModal(sys.id, 'system', '', String(sys.label))).toBe(true);
    });
    const body = document.querySelector('#data-modal-content')!;
    expect(body.querySelector('.chart-notice')).toBeNull();
    expect(body.querySelector('iframe')).toBeNull();
    const pre = body.querySelector('.modal-telemetry-lines')!;
    expect(pre.textContent).toBe([coverage, listed, sample].join('\n'));
    expect(body.querySelector('.modal-telemetry h4')!.textContent).toBe('Request 1');
    expect(body.textContent).toContain('1 time-series: 1 data points total');
  });

  it('a call modal shows its lines unlabelled, and a modal without them shows no section', () => {
    archStore.handleArchEvent('user_msg', { question: 'q' });
    archStore.handleArchEvent('tool_start', { tool: 'sys-kappa___query_sys_kappa', input: { question: 'q' } });
    archStore.handleArchEvent('tool_result', {
      tool: 'sys-kappa___query_sys_kappa',
      summary: '1 time-series: 1 data points total',
      sample_line: '[engine] Telemetry samples, not every value: rig-5d1e: 1 of 2 values read returned.',
    });
    const node = archStore.getState().nodes.find((n) => n._call)!;
    (window as unknown as Record<string, unknown>).openDataModal = openDataModal;
    act(() => {
      expect(openInvNodeModal(node.id, String(node.type), '', String(node.label))).toBe(true);
    });
    const body = document.querySelector('#data-modal-content')!;
    expect(body.querySelector('.modal-telemetry-lines')!.textContent).toContain('1 of 2 values read returned');
    expect(body.querySelector('.modal-telemetry h4')).toBeNull();
    // Guard: an open without extras (every other opener) shows no section.
    act(() => {
      openDataModal('Plain', 'Some result');
    });
    expect(document.querySelector('#data-modal-content .modal-telemetry')).toBeNull();
  });

  it('guard: an unfenced chart tag in opened content still draws', () => {
    act(() => {
      openDataModal('Chart', 'Before\n\n<chart-block kind="notice" title="A real notice"/>\n\nAfter');
    });
    const body = document.querySelector('#data-modal-content')!;
    expect(body.querySelector('.chart-notice')).not.toBeNull();
  });
});


/* The typed telemetry lines ride the call detail beside the summary
 * (round 5 review, MED) and stay out of the modal markdown, where the
 * modal's chart splitter and markdown parser would read them (round 6 and
 * round 7 reviews). Cases are the shared server/UI fixture
 * (fixtures/trace-sample-line-cases.json). */
describe('Architecture drill-down: the typed telemetry lines', () => {
  const FIXTURE = JSON.parse(
    readFileSync(resolve(__dirname, 'fixtures', 'trace-sample-line-cases.json'), 'utf8'),
  );
  const sample = FIXTURE.cases[0] as { summary: string; sample_line: string };
  const coverage = FIXTURE.coverage_cases[0] as {
    summary: string;
    telemetry_coverage: string[];
    outcome: string;
  };
  const TOOL = 'sys-kappa___query_sys_kappa';

  function streamTelemetryCall(): void {
    archStore.handleArchEvent('user_msg', { question: 'Is vibration trending up on rig-5d1e?' });
    archStore.handleArchEvent('tool_start', { tool: TOOL, input: { question: 'vibration on rig-5d1e' } });
    archStore.handleArchEvent('tool_result', {
      tool: TOOL,
      summary: coverage.summary,
      telemetry_coverage: coverage.telemetry_coverage,
      sample_line: sample.sample_line,
    });
  }

  function systemNode() {
    return archStore.getState().nodes.find((n) => Array.isArray(n._calls) && (n._calls as unknown[]).length)!;
  }

  it('the call detail keeps both lines beside the summary, and the modal sections show them in order', () => {
    // Reproduction: the drill-down kept neither line.
    streamTelemetryCall();
    const call = (systemNode()._calls as InvCallDetail[])[0];
    expect(call.resultSummary).toBe(coverage.summary);
    expect(call.telemetryCoverage).toEqual(coverage.telemetry_coverage);
    expect(call.sampleLine).toBe(sample.sample_line);
    expect(telemetrySections({ kind: 'inv-node', calls: [call] })).toEqual([
      { label: 'Request 1', lines: [...coverage.telemetry_coverage, sample.sample_line] },
    ]);
  });

  it('the lines are not in either modal markdown', () => {
    streamTelemetryCall();
    const sys = systemNode();
    const call = (sys._calls as InvCallDetail[])[0];
    for (const md of [buildInvNodeModal(sys), buildInvCallModal({ ...sys, _call: call } as never)]) {
      expect(md).not.toContain(coverage.telemetry_coverage[0]);
      expect(md).not.toContain(sample.sample_line);
    }
  });

  it('guard: the call is classified from the summary alone (lines naming sensor_error do not fail it)', () => {
    streamTelemetryCall();
    const call = (systemNode()._calls as InvCallDetail[])[0];
    expect(call.outcome ?? 'success').toBe(coverage.outcome);
  });
});

describe('<DetailModal/> dialog accessibility', () => {
  let container: HTMLDivElement;
  let root: Root;
  let trigger: HTMLButtonElement;

  beforeEach(() => {
    patchModal({ open: false, title: '', sub: '', key: null, lineage: null, content: null });
    trigger = document.createElement('button');
    trigger.textContent = 'Open inventory detail';
    document.body.appendChild(trigger);
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<DetailModal />));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    trigger.remove();
    dashboardStore.update((state) => ({
      ...state,
      modal: { ...state.modal, open: false, key: null, lineage: null },
    }));
  });

  it('has a stable label, initial focus, Escape dismissal, and focus restoration', () => {
    trigger.focus();
    act(() => {
      patchModal({
        open: true,
        title: 'Inventory by line',
        sub: 'Live ERP detail',
        content: { kind: 'loading', label: 'Loading rows…' },
      });
    });

    const dialog = document.getElementById('detail-modal-overlay')!;
    expect(dialog.parentElement).toBe(document.body);
    expect(document.body.style.overflow).toBe('hidden');
    expect(dialog.getAttribute('role')).toBe('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('detail-modal-title');
    expect(document.getElementById('detail-modal-title')!.textContent).toBe('Inventory by line');
    expect(document.activeElement).toBe(document.querySelector('.detail-modal-close'));

    act(() => {
      dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(document.getElementById('detail-modal-overlay')).toBeNull();
    expect(document.body.style.overflow).toBe('');
    expect(document.activeElement).toBe(trigger);
  });

  it('keeps overlay-click dismissal behavior', () => {
    act(() => {
      patchModal({
        open: true,
        title: 'Inventory by line',
        sub: '',
        content: { kind: 'loading', label: 'Loading rows…' },
      });
    });
    const dialog = document.getElementById('detail-modal-overlay')!;
    act(() => dialog.click());
    expect(document.getElementById('detail-modal-overlay')).toBeNull();
  });

  it('renders each reported OEE source and an honest titled dash when absent', () => {
    act(() => {
      patchModal({
        open: true,
        title: 'OEE drilldown',
        sub: 'OEE = Availability × Performance × Quality',
        content: {
          kind: 'oee',
          card: {
            metrics: [
              { metric_id: 'oee', label: 'OEE', status: 'ok', value: 0.8 },
              { metric_id: 'availability', label: 'Availability', status: 'ok', value: 0.9, system_id: 'mes-source' },
              { metric_id: 'performance', label: 'Performance', status: 'error', system_id: 'erp-source' },
              { metric_id: 'quality', label: 'Quality', status: 'ok', value: 0.98 },
            ],
          },
        },
      });
    });

    const rows = Array.from(document.querySelectorAll<HTMLTableRowElement>('.oee-drill-table tbody tr'));
    expect(rows).toHaveLength(4);
    expect(rows[1].cells[2].textContent).toBe('mes-source');
    expect(rows[2].cells[2].textContent).toBe('erp-source');
    expect(rows[3].cells[2].textContent).toBe('—');
    expect(rows[3].cells[2].title).toBe('source not reported');
  });

  it('dashboard.css keeps the detail backdrop viewport-fixed and its body internally scrollable', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/dashboard.css'), 'utf-8');
    const overlayRule = css.match(/\.detail-modal-overlay\s*\{[^}]*\}/)![0];
    expect(overlayRule).toContain('position: fixed');
    expect(overlayRule).toContain('inset: 0');
    expect(overlayRule).toMatch(/z-index:\s*(?:[1-9]\d{3,})/);

    const modalRule = css.match(/\.detail-modal\s*\{[^}]*\}/)![0];
    expect(modalRule).toContain('max-height: 85vh');
    expect(modalRule).toContain('overflow: hidden');

    const bodyRule = css.match(/\.detail-modal-body\s*\{[^}]*\}/)![0];
    expect(bodyRule).toContain('max-height: calc(85vh');
    expect(bodyRule).toContain('overflow-y: auto');
  });
});
