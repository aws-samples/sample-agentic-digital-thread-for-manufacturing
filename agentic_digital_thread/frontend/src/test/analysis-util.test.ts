// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Vitest port of tests/unit/test_analysis_util.py and the selectFinalAnswer
 * assertions in tests/unit/test_answer_assembly.py.
 *
 * The helpers live in src/lib/analysis-util.js (a CJS-compatible IIFE
 * retained from the removed legacy bundle). We load it through a
 * CommonJS-emulating shim so the `module.exports` path is exercised exactly
 * like the python tests' node subprocess (`require(...)`), independent of the
 * package.json "type" field or the vitest environment.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const UTIL_JS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../lib/analysis-util.js',
);

const source = readFileSync(UTIL_JS, 'utf-8');

/** Emulate `require()` of the verbatim CJS-compatible IIFE. */
function loadCjs(): Record<string, any> {
  const mod = { exports: {} as Record<string, any> };
  // eslint-disable-next-line no-new-func
  new Function('module', 'exports', source)(mod, mod.exports);
  return mod.exports;
}

const m = loadCjs();

type Segment = { label: string; count: number; pct: number; color: string };

function run(
  seq: unknown,
  neptuneCount: number,
  palette?: string[],
): Segment[] {
  return palette !== undefined
    ? m.buildDistributionSegments(seq, neptuneCount, palette)
    : m.buildDistributionSegments(seq, neptuneCount);
}

// --- Module surface ----------------------------------------------------------

describe('module surface', () => {
  it('exports the helpers via module.exports (node-requirable)', () => {
    expect(typeof m.buildDistributionSegments).toBe('function');
    expect(Array.isArray(m.DISTRIBUTION_PALETTE)).toBe(true);
    expect(typeof m.selectFinalAnswer).toBe('function');
  });

  it('palette is neutral — no red-for-low', () => {
    // The palette must not encode a good/bad signal — no pure/danger red.
    expect(source).toContain('DISTRIBUTION_PALETTE');
    expect(source).toContain('buildDistributionSegments');
    for (const banned of ['#ff0000', '#f85149', 'var(--red)']) {
      expect(source, `palette must not use red-for-low (${banned})`).not.toContain(banned);
    }
  });
});

// --- Composition maths --------------------------------------------------------

describe('buildDistributionSegments', () => {
  it('empty input returns no segments', () => {
    expect(run([], 0)).toEqual([]);
    expect(run(null, 0)).toEqual([]);
  });

  it('graph only is a full bar', () => {
    const segs = run([], 3);
    expect(segs).toHaveLength(1);
    expect(segs[0].label).toBe('Knowledge Graph');
    expect(segs[0].count).toBe(3);
    expect(segs[0].pct).toBe(100);
  });

  it('graph first then systems by descending count', () => {
    const seq = [{ label: 'ERP' }, { label: 'MES' }, { label: 'ERP' }, { label: 'ERP' }];
    const segs = run(seq, 2);
    const labels = segs.map((s) => s.label);
    // Knowledge Graph anchors first, then systems by descending count.
    expect(labels[0]).toBe('Knowledge Graph');
    expect(labels[1]).toBe('ERP'); // 3 calls
    expect(labels[2]).toBe('MES'); // 1 call
    const counts = Object.fromEntries(segs.map((s) => [s.label, s.count]));
    expect(counts).toEqual({ 'Knowledge Graph': 2, ERP: 3, MES: 1 });
    // Percentages are whole numbers of a 6-call total.
    expect(new Set(segs.map((s) => s.pct))).toEqual(new Set([33, 50, 17]));
    for (const s of segs) expect(Number.isInteger(s.pct)).toBe(true);
  });

  it('percentages are share of total tool calls', () => {
    const seq = [{ label: 'ERP' }, { label: 'ERP' }, { label: 'ERP' }];
    const segs = run(seq, 1); // total = 4
    const byLabel = Object.fromEntries(segs.map((s) => [s.label, s]));
    expect(byLabel['Knowledge Graph'].pct).toBe(25);
    expect(byLabel.ERP.pct).toBe(75);
  });

  it('colors cycle from the neutral palette', () => {
    const seq = [
      { label: 'A' }, { label: 'B' }, { label: 'C' },
      { label: 'D' }, { label: 'E' }, { label: 'F' },
    ];
    // 6 systems + graph = 7 segments → palette (len 6) must wrap, never crash.
    const segs = run(seq, 1);
    expect(segs).toHaveLength(7);
    const colors = segs.map((s) => s.color);
    for (const c of colors) expect(c.startsWith('#')).toBe(true);
    // Wrap-around: 7th segment reuses the first palette color.
    expect(colors[6]).toBe(colors[0]);
  });

  it('custom palette is respected', () => {
    const segs = run([{ label: 'ERP' }], 1, ['#111111', '#222222']);
    expect(segs[0].color).toBe('#111111');
    expect(segs[1].color).toBe('#222222');
  });

  it('blank labels fall back and group', () => {
    const seq = [{ label: '' }, { label: null }, { label: '  ERP  ' }, { label: 'ERP' }];
    const segs = run(seq, 0);
    const counts = Object.fromEntries(segs.map((s) => [s.label, s.count]));
    // Blank/None collapse to "System"; whitespace-trimmed "ERP" groups together.
    expect(counts).toEqual({ ERP: 2, System: 2 });
  });
});

// --- Answer assembly (port of test_answer_assembly.py) ------------------------

describe('selectFinalAnswer', () => {
  const runSel = (segments: unknown): string => m.selectFinalAnswer(segments);

  it('helper is exported', () => {
    expect(source).toContain('selectFinalAnswer');
  });

  it('empty and blank return empty', () => {
    expect(runSel([])).toBe('');
    expect(runSel(null)).toBe('');
    expect(runSel(['   ', '\n'])).toBe('');
  });

  it('single segment is the answer', () => {
    expect(runSel(['The final answer.'])).toBe('The final answer.');
  });

  it('longest run wins over inter-tool reasoning', () => {
    // Short reasoning runs BEFORE the report are dropped; the report is kept.
    const report = '# Report\n\n' + 'row line. '.repeat(80);
    const segs = [
      "Let me query MES.",              // reasoning (short)
      "Now I'll query ERP and CMMS.",   // reasoning (short)
      report,                           // the real answer (longest)
    ];
    expect(runSel(segs)).toBe(report.trim());
  });

  it('trailing epilogue after housekeeping tool is appended', () => {
    // The P0 case: full report, then a trailing save_correlation tool call,
    // then a short epilogue. Report is longest → kept; epilogue → appended.
    const report = '# Cross-System Report\n\n' + 'data. '.repeat(100);
    const epilogue = 'Two correlations saved for review.';
    const segs = ['Analyzing systems.', report, epilogue];
    const out = runSel(segs);
    expect(out.startsWith('# Cross-System Report')).toBe(true);
    expect(out.endsWith(epilogue)).toBe(true);
    expect(out).not.toContain('Analyzing systems.'); // pre-report reasoning dropped
  });
});

describe('selectFinalAnswer from a start index, and the write-back tools', () => {
  it('selects from the start index, falling back to every run when nothing is there', () => {
    expect(m.selectFinalAnswer(['a long planning run here', 'short'], 1)).toBe('short');
    expect(m.selectFinalAnswer(['report text', 'x'], 2)).toBe('report text\n\nx');
    expect(m.selectFinalAnswer(['report text', 'x'], 0)).toBe('report text\n\nx');
  });

  it('names only the write-back tools, bare or gateway-prefixed', () => {
    for (const tool of ['save_correlation', 'save_value_mapping', 'propose_metric_definition',
      'digital-thread___save_correlation']) {
      expect(m.isWriteBackTool(tool)).toBe(true);
    }
    for (const tool of ['erp___query_erp', 'explore_graph', 'get_system_schema',
      'digital-thread___verify_relationship', '', 'save_correlation_x', undefined]) {
      expect(m.isWriteBackTool(tool)).toBe(false);
    }
  });
});
