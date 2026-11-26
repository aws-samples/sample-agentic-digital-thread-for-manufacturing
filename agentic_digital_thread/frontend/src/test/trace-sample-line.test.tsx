// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A system call's telemetry sample line in the chat trace. The system
 * runtime stores the line for the call, and the server sends it as the
 * tool_result's own `sample_line` field (the Recent sessions reload, as the
 * block's `sample_line`), never inside `summary`. The trace step shows it as
 * its own plain-text line below the summary, and the call's outcome is
 * classified from the summary alone: the line names series by id, and an id
 * such as `sensor_error` or `comm_exception` followed by a colon reads as an
 * error marker to the head classifier (lib/arch-store.ts envelopeHead). The
 * call's stored telemetry-coverage lines travel the same way
 * (`telemetry_coverage`) and show above the summary: in front of it they
 * moved the head too. The cases are shared with the server's tests
 * (fixtures/trace-sample-line-cases.json). */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemoryEvent } from '../api/types/memory';
import { ChatThread } from '../chat/ChatThread';
import { buildRehydrateThread } from '../chat/rehydrate';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import type { ToolTrace } from '../chat/turn-model';
import { classifyToolResult } from '../lib/arch-store';
import { setLang } from '../lib/i18n';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Case {
  id: string;
  summary: string;
  sample_line: string;
  appended_outcome: string;
}

interface CoverageCase {
  id: string;
  summary: string;
  telemetry_coverage: string[];
  outcome: string;
  prefixed_outcome: string;
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(path.join(HERE, 'fixtures', 'trace-sample-line-cases.json'), 'utf8'));
const CASES: Case[] = FIXTURE.cases;
const COVERAGE_CASES: CoverageCase[] = FIXTURE.coverage_cases;
const TOOL = 'sys-kappa___query_sys_kappa';

let container: HTMLDivElement;
let root: Root;
let store: ThreadStore;

beforeEach(() => {
  const ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  setLang('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  store = createThreadStore();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const traceOf = (s: ThreadStore): ToolTrace => s.getState().turns.at(-1)!.assistant.traces.get('c')!;

const runCall = (data: Record<string, unknown>) =>
  act(() => {
    store.startTurn('What did the window hold?');
    store.applyEvent('tool_start', { tool: TOOL, tool_id: 'c', input: { question: 'q' } });
    store.applyEvent('tool_result', { tool: TOOL, tool_id: 'c', ...data });
    store.applyEvent('done', { status: 'complete' });
  });

describe('a stored sample line beside the summary', () => {
  it.each(CASES.map((c) => [c.id, c] as const))(
    '%s: the outcome is the summary alone, and the line is kept for display',
    (_id, c) => {
      // Reproduction: appended to the summary, the line moved the head the
      // classifier reads; the fixture records that outcome.
      expect(classifyToolResult(`${c.summary}\n\n${c.sample_line}`)).toBe(c.appended_outcome);
      runCall({ summary: c.summary, sample_line: c.sample_line });
      const trace = traceOf(store);
      expect(trace.resultMd).toBe(c.summary);
      expect(trace.sampleLine).toBe(c.sample_line);
      expect(trace.outcome).toBe(classifyToolResult(c.summary));
      expect(trace.outcome).toBe('success');
    },
  );

  it('shows the line as its own plain-text line below the summary', () => {
    const c = CASES[0];
    runCall({ summary: c.summary, sample_line: c.sample_line });
    act(() => root.render(<ChatThread store={store} />));
    const result = container.querySelector('.tool-activity .tool-trace[data-tool-id="c"] .trace-result')!;
    const line = result.querySelector('.trace-sample-line')!;
    expect(line.textContent).toBe(c.sample_line);
    expect(line.previousElementSibling?.textContent).toContain(c.summary);
    expect(result.lastElementChild).toBe(line);
  });

  it('shows nothing extra for a call without a line', () => {
    // Guard: a result without the field renders as before.
    runCall({ summary: '1 row' });
    act(() => root.render(<ChatThread store={store} />));
    expect(traceOf(store).sampleLine).toBeUndefined();
    expect(container.querySelector('.trace-sample-line')).toBeNull();
    runCall({ summary: '1 row', sample_line: '   ' });
    expect(traceOf(store).sampleLine).toBeUndefined();
  });
});

describe('stored coverage lines above the summary', () => {
  it.each(COVERAGE_CASES.map((c) => [c.id, c] as const))(
    '%s: the outcome is the summary alone, and the lines are kept for display',
    (_id, c) => {
      // Reproduction: in front of the summary, the lines moved the head the
      // classifier reads; the fixture records that outcome.
      expect(classifyToolResult(`${c.telemetry_coverage.join('\n\n')}\n\n${c.summary}`)).toBe(
        c.prefixed_outcome,
      );
      runCall({ summary: c.summary, telemetry_coverage: c.telemetry_coverage });
      const trace = traceOf(store);
      expect(trace.resultMd).toBe(c.summary);
      expect(trace.coverageLines).toEqual(c.telemetry_coverage);
      expect(trace.outcome).toBe(c.outcome);
    },
  );

  it('shows each line as its own plain-text line above the summary', () => {
    const c = COVERAGE_CASES[1];
    runCall({ summary: c.summary, telemetry_coverage: c.telemetry_coverage });
    act(() => root.render(<ChatThread store={store} />));
    const result = container.querySelector('.tool-activity .tool-trace[data-tool-id="c"] .trace-result')!;
    const lines = [...result.querySelectorAll('.trace-coverage-line')];
    expect(lines.map((l) => l.textContent)).toEqual(c.telemetry_coverage);
    expect(lines.at(-1)?.nextElementSibling?.textContent).toContain(c.summary);
  });

  it('keeps no empty or non-text line', () => {
    // Guard of the field's shape.
    runCall({ summary: '1 row', telemetry_coverage: ['', 7, '  '] });
    expect(traceOf(store).coverageLines).toBeUndefined();
  });
});

describe('a reopened chat (Recent sessions)', () => {
  it('replays the block sample_line beside its summary, outcome from the summary alone', () => {
    const c = CASES[1];
    const items: MemoryEvent[] = [
      { role: 'user', content: 'What did the window hold?', blocks: [{ type: 'text', text: 'What did the window hold?' }] },
      { role: 'assistant', content: '', blocks: [{ type: 'tool_use', tool_id: 'c', tool: TOOL, input: { question: 'q' } }] },
      {
        role: 'user',
        content: '',
        blocks: [{ type: 'tool_result', tool_id: 'c', tool: TOOL, status: 'success', summary: c.summary, sample_line: c.sample_line }],
      },
      { role: 'assistant', content: 'Done.', blocks: [{ type: 'text', text: 'Done.' }] },
    ];
    const { turns } = buildRehydrateThread(items);
    const trace = turns.at(-1)!.assistant.traces.get('c')!;
    expect(trace.resultMd).toBe(c.summary);
    expect(trace.sampleLine).toBe(c.sample_line);
    expect(trace.outcome).toBe('success');
  });

  it('replays the block telemetry_coverage above its summary, outcome from the summary alone', () => {
    // Reproduction (integration review 1026, MED): a reopened chat dropped the
    // stored coverage lines the live trace showed. The case's lines name an
    // id that reads as an error marker, so the outcome also proves the lines
    // are never classified.
    const c = COVERAGE_CASES[0];
    const items: MemoryEvent[] = [
      { role: 'user', content: 'What did the window hold?', blocks: [{ type: 'text', text: 'What did the window hold?' }] },
      { role: 'assistant', content: '', blocks: [{ type: 'tool_use', tool_id: 'c', tool: TOOL, input: { question: 'q' } }] },
      {
        role: 'user',
        content: '',
        blocks: [
          { type: 'tool_result', tool_id: 'c', tool: TOOL, status: 'success', summary: c.summary, telemetry_coverage: c.telemetry_coverage },
        ],
      },
      { role: 'assistant', content: 'Done.', blocks: [{ type: 'text', text: 'Done.' }] },
    ];
    const { turns } = buildRehydrateThread(items);
    const trace = turns.at(-1)!.assistant.traces.get('c')!;
    expect(trace.resultMd).toBe(c.summary);
    expect(trace.coverageLines).toEqual(c.telemetry_coverage);
    expect(trace.outcome).toBe(c.outcome);
  });
});
