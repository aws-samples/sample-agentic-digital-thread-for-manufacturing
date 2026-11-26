// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Settings tab: the memory extraction model (a third select on the LLM card).
 *
 * - choices come from the catalog's `memory` list, the value from
 *   GET /api/admin/llm-settings `memory_model`;
 * - a one-line description says what the model does and that existing
 *   records stay;
 * - when the server says it is not selectable, the select is read-only and
 *   the reason is shown;
 * - the memory model has its own Save: it posts a background switch job
 *   (POST /api/admin/llm-settings/memory-model, after its own confirm),
 *   shows "Switching the memory to X..." while it polls the job, then
 *   refetches the settings and shows the outcome; Save Models never sends it;
 * - a page opened while a switch runs shows it running;
 * - a status read that fails is shown with the server's message: the
 *   controls stay as the last known state left them, the live model is
 *   read again, and the page shows the outcome once the status reads again;
 * - a switch whose outcome is unknown is shown as such, not as an error;
 * - a saved model the memory does not run, a saved value the next deploy
 *   ignores, and a model nothing saved would keep are named under the select. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { BrandingProvider } from '../contexts/BrandingContext';
import SettingsTab from '../pages/admin/tabs/SettingsTab';
import { answerConfirm, confirmDialog, confirmText, installConfirmHost } from './confirm-host';

// Poll fast and give up fast, so the tests do not wait seconds.
vi.mock('../pages/admin/memory-model-queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../pages/admin/memory-model-queries')>()),
  MEMORY_JOB_POLL_MS: 10,
  MEMORY_JOB_POLL_LIMIT_MS: 400,
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const MODELS = [
  { id: 'model-a', label: 'Model A' },
  { id: 'model-b', label: 'Model B' },
];
const MEMORY_MODELS = [
  { id: 'mem-a', label: 'Memory A', default: true },
  { id: 'mem-b', label: 'Memory B' },
];
const SWITCH = '/api/admin/llm-settings/memory-model';
const CURRENT_JOB = `${SWITCH}/job`;

let llmSettings: Record<string, unknown>;
let putResult: Record<string, unknown>;
let postResult: Record<string, unknown>;
let postStatus: number;
let currentJob: Record<string, unknown>;
let currentJobStatus: number;
/** What successive GETs of /job/<id> answer; the last one repeats. */
let jobReads: Record<string, unknown>[];
/** Runs when a job read answers a finished job: what the server reads then. */
let afterJobDone: (() => void) | null;

function getResponses(): Record<string, unknown> {
  return {
    '/api/admin/settings': { status: 'ok', demo_enabled: false },
    '/api/admin/models': { orchestrator: MODELS, agent: MODELS, memory: MEMORY_MODELS },
    '/api/admin/feedback-exploration': {
      feedback_exploration_enabled: false,
      source: 'default',
      env_kill_active: false,
    },
    '/api/admin/live-updates': { live_updates_enabled: false, interval_seconds: 120 },
    '/api/admin/llm-settings': llmSettings,
    '/api/admin/concept-packs': { packs: [] },
  };
}

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

interface FetchCall {
  url: string;
  method: string;
  body?: unknown;
}

let container: HTMLDivElement;
let root: Root;
let calls: FetchCall[];

/** A job read answering `{ httpStatus, message }` is that HTTP error. */
function jobRead(): Response {
  const record = jobReads.length > 1 ? jobReads.shift()! : jobReads[0];
  if (typeof record?.httpStatus === 'number') {
    return response({ status: 'error', message: record.message }, record.httpStatus);
  }
  if (record && record.status !== 'running') afterJobDone?.();
  return response(record);
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  calls = [];
  llmSettings = {
    orchestrator_model: 'model-a',
    agent_model: 'model-a',
    memory_model: 'mem-a',
    memory_model_selectable: true,
    memory_model_locked_reason: '',
  };
  putResult = { status: 'ok', orchestrator_model: 'model-b', agent_model: 'model-a' };
  postResult = { status: 'running', job_id: 'job-1', memory_model: 'mem-b', started_at: 1 };
  postStatus = 202;
  currentJob = { status: 'none' };
  currentJobStatus = 200;
  jobReads = [
    { status: 'running', job_id: 'job-1', memory_model: 'mem-b', started_at: 1 },
    {
      status: 'done',
      job_id: 'job-1',
      memory_model: 'mem-b',
      memory_model_changed: true,
      message: 'The memory extraction model was switched to mem-b and saved for the next deploy.',
    },
  ];
  afterJobDone = () => {
    llmSettings = { ...llmSettings, memory_model: 'mem-b' };
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
      calls.push({ url, method, body });
      if (method === 'GET') {
        if (url.startsWith(`${CURRENT_JOB}/`)) return jobRead();
        if (url === CURRENT_JOB) return response(currentJob, currentJobStatus);
        const responses = getResponses();
        return response(url in responses ? responses[url] : {});
      }
      if (method === 'PUT' && url === '/api/admin/llm-settings') return response(putResult);
      if (method === 'POST' && url === SWITCH) return response(postResult, postStatus);
      throw new Error(`unexpected ${method}: ${url}`);
    }),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const memorySelect = () => document.getElementById('settings-memory-model') as HTMLSelectElement | null;
const memoryButton = () => document.getElementById('settings-memory-model-btn') as HTMLButtonElement;
const memoryStatus = () => document.getElementById('settings-memory-model-status')!.textContent!;

async function renderTab(expectedMemory: string) {
  const client = createQueryClient();
  client.setQueryData(queryKeys.branding(), { company: 'TestCo', tagline: 'Tagline' });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <BrandingProvider updateTitle={false}>
          <SettingsTab onDemoEnabledChange={() => undefined} onBrandingSaved={() => undefined} />
        </BrandingProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() => {
    expect((document.getElementById('settings-orch-model') as HTMLSelectElement).value).toBe('model-a');
    expect(memorySelect()?.value).toBe(expectedMemory);
  });
}

const setSelectValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;

async function choose(id: string, value: string) {
  await act(async () => {
    const select = document.getElementById(id) as HTMLSelectElement;
    setSelectValue.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

async function click(id: string) {
  await act(async () => {
    (document.getElementById(id) as HTMLElement).click();
  });
}

const writes = () => calls.filter((c) => c.method !== 'GET');
const jobGets = () => calls.filter((c) => c.method === 'GET' && c.url.startsWith(`${CURRENT_JOB}/`));
const llmGets = () => calls.filter((c) => c.method === 'GET' && c.url === '/api/admin/llm-settings');

const SWITCH_CONFIRM = {
  title: 'Change the memory extraction model?',
  body:
    'AgentCore Memory switches to the new model in place. The switch takes about two minutes ' +
    'and runs in the background; you can leave this page. New extractions use it; records ' +
    'already extracted stay as they are.',
  buttons: ['Cancel', 'Change memory model'],
};

describe('memory extraction model', () => {
  it('offers the memory list with a description of what the model does', async () => {
    await renderTab('mem-a');
    const select = memorySelect()!;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['mem-a', 'mem-b']);
    expect(select.disabled).toBe(false);
    expect(container.textContent).toContain('Memory Extraction Model');
    const help = document.getElementById('settings-memory-model-help')!.textContent!;
    expect(help).toContain('facts, preferences and summaries');
    expect(help).toContain('existing records stay');
    expect(memoryButton().textContent).toBe('Save Memory Model');
  });

  it('Save Models never sends the memory model', async () => {
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await choose('settings-orch-model', 'model-b');
    await click('settings-llm-btn');
    expect(confirmText().title).toBe('Restart the orchestrator?');
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(writes()).toEqual([
        {
          url: '/api/admin/llm-settings',
          method: 'PUT',
          body: { orchestrator_model: 'model-b', agent_model: 'model-a' },
        },
      ]),
    );
    expect(confirmDialog()).toBeNull();
  });

  it('switches in the background after its own confirm, then shows the outcome', async () => {
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    expect(confirmText()).toEqual(SWITCH_CONFIRM);
    jobReads = [{ status: 'running', job_id: 'job-1', memory_model: 'mem-b', started_at: 1 }];
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(writes()).toEqual([{ url: SWITCH, method: 'POST', body: { memory_model: 'mem-b' } }]),
    );
    await vi.waitFor(() => expect(memoryStatus()).toContain('Switching the memory to Memory B'));
    expect(memorySelect()!.disabled).toBe(true);
    expect(memoryButton().disabled).toBe(true);
    await vi.waitFor(() => expect(jobGets().length).toBeGreaterThanOrEqual(2));
    expect(jobGets()[0].url).toBe(`${CURRENT_JOB}/job-1`);
    const getsBefore = llmGets().length;
    jobReads = [
      {
        status: 'done',
        job_id: 'job-1',
        memory_model: 'mem-b',
        memory_model_changed: true,
        message: 'The memory extraction model was switched to mem-b and saved for the next deploy.',
      },
    ];
    await vi.waitFor(() =>
      expect(memoryStatus()).toBe(
        '✓ The memory extraction model was switched to mem-b and saved for the next deploy.',
      ),
    );
    await vi.waitFor(() => expect(llmGets().length).toBeGreaterThan(getsBefore));
    expect(memorySelect()!.value).toBe('mem-b');
    expect(memorySelect()!.disabled).toBe(false);
    const polled = jobGets().length;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(jobGets().length).toBe(polled);
  });

  it('declining the memory confirm sends nothing', async () => {
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    await answerConfirm('cancel');
    expect(confirmDialog()).toBeNull();
    expect(writes()).toEqual([]);
  });

  it('an unchanged memory model has nothing to save', async () => {
    await renderTab('mem-a');
    expect(memoryButton().disabled).toBe(true);
  });

  it('is read-only with the reason when the server says it is not selectable', async () => {
    llmSettings = {
      ...llmSettings,
      memory_model: 'mem-b',
      memory_model_selectable: false,
      memory_model_locked_reason: 'Pinned by the workshop (Memory B).',
    };
    await renderTab('mem-b');
    expect(memorySelect()!.disabled).toBe(true);
    expect(memoryButton().disabled).toBe(true);
    expect(document.getElementById('settings-memory-model-locked')!.textContent).toBe(
      'Pinned by the workshop (Memory B).',
    );
  });

  it('shows a mixed memory as such and does not send it back', async () => {
    llmSettings = { ...llmSettings, memory_model: 'mixed' };
    await renderTab('mixed');
    const mixed = Array.from(memorySelect()!.options).find((o) => o.value === 'mixed')!;
    expect(mixed.textContent).toBe('Mixed (the strategies run different models)');
    expect(mixed.disabled).toBe(true);
    expect(memoryButton().disabled).toBe(true);
    await choose('settings-orch-model', 'model-b');
    await click('settings-llm-btn');
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].body).toEqual({ orchestrator_model: 'model-b', agent_model: 'model-a' });
  });

  it('shows a failed memory read', async () => {
    llmSettings = { ...llmSettings, memory_model: '', memory_model_error: 'Upstream failed (ref abc).' };
    await renderTab('');
    expect(document.getElementById('settings-memory-model-error')!.textContent).toContain(
      'Upstream failed (ref abc).',
    );
  });
});

describe('a switch that does not finish', () => {
  it('shows the job error and the live model', async () => {
    jobReads = [
      {
        status: 'error',
        job_id: 'job-1',
        memory_model: 'mem-b',
        memory_model_changed: false,
        message:
          'The memory extraction model change did not finish: the memory did not settle. Nothing was saved for the next deploy; check the memory before saving again.',
      },
    ];
    afterJobDone = null;
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toContain('Error: The memory extraction model change did not finish'));
    await vi.waitFor(() => expect(memorySelect()!.value).toBe('mem-a'));
  });

  it('a worker that stopped is shown as stopped', async () => {
    jobReads = [
      {
        status: 'error',
        stale: true,
        job_id: 'job-1',
        memory_model: 'mem-b',
        message: 'The memory extraction model switch to mem-b stopped before it reported a result (no result within 900 s).',
      },
    ];
    afterJobDone = null;
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toContain('stopped before it reported a result'));
  });

  it.each([
    [409, 'Another memory extraction model save is in progress; try again when it finishes. Nothing was changed.'],
    [503, 'The memory extraction model save could not be coordinated with the other app tasks (the shared store did not answer); try again. Nothing was changed.'],
    [400, "memory_model 'mem-b' is not a memory extraction model offered in this deployment"],
  ])('a switch refused with %i shows why and the live model', async (status, message) => {
    postStatus = status;
    postResult = { status: 'error', message };
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toBe(`Error: ${message}`));
    await vi.waitFor(() => expect(memorySelect()!.value).toBe('mem-a'));
    expect(jobGets()).toEqual([]);
  });

  it('stops polling after its bound and says how to check', async () => {
    jobReads = [{ status: 'running', job_id: 'job-1', memory_model: 'mem-b', started_at: 1 }];
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toContain('still running'), { timeout: 3000 });
    expect(memoryStatus()).toContain('Reload the page');
    const polled = jobGets().length;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(jobGets().length).toBe(polled);
  });
});

describe('a page opened while a switch runs', () => {
  it('shows the running switch and its outcome', async () => {
    llmSettings = { ...llmSettings, memory_model: 'mem-a' };
    currentJob = { status: 'running', job_id: 'job-7', memory_model: 'mem-b', started_at: 1 };
    // Running until the test has seen it running; then the outcome.
    jobReads = [{ status: 'running', job_id: 'job-7', memory_model: 'mem-b', started_at: 1 }];
    await renderTab('mem-a');
    await vi.waitFor(() => expect(memoryStatus()).toContain('Switching the memory to Memory B'));
    expect(memorySelect()!.disabled).toBe(true);
    jobReads = [
      {
        status: 'done',
        job_id: 'job-7',
        memory_model: 'mem-b',
        memory_model_changed: true,
        message: 'The memory extraction model was switched to mem-b and saved for the next deploy.',
      },
    ];
    await vi.waitFor(() => expect(memoryStatus()).toContain('✓ The memory extraction model was switched'));
    expect(jobGets()[0].url).toBe(`${CURRENT_JOB}/job-7`);
    await vi.waitFor(() => expect(memorySelect()!.value).toBe('mem-b'));
  });

  it('says nothing about a switch that finished before the page opened', async () => {
    currentJob = { status: 'done', job_id: 'job-3', memory_model: 'mem-a', message: 'Old news.' };
    await renderTab('mem-a');
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(memoryStatus()).toBe('');
    expect(jobGets()).toEqual([]);
  });
});

const UNREADABLE =
  "The memory extraction model switch's status could not be read just now (the shared store " +
  'did not answer); it is read again shortly.';

describe('a status read that fails', () => {
  it('on load says the latest switch could not be read', async () => {
    currentJob = { status: 'error', message: UNREADABLE };
    currentJobStatus = 503;
    await renderTab('mem-a');
    await vi.waitFor(() =>
      expect(memoryStatus()).toBe(`Could not read whether a memory model switch is running: ${UNREADABLE}`),
    );
    expect(document.getElementById('settings-memory-model-status')!.getAttribute('style')).toContain('red');
  });

  it('after a running record keeps the controls, says why and shows the live model', async () => {
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    jobReads = [{ status: 'running', job_id: 'job-1', memory_model: 'mem-b', started_at: 1 }];
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toContain('Switching the memory to Memory B'));
    expect(memorySelect()!.value).toBe('mem-b');
    const getsBefore = llmGets().length;
    jobReads = [{ httpStatus: 503, message: UNREADABLE }];
    await vi.waitFor(() => expect(memoryStatus()).toContain(UNREADABLE));
    expect(memoryStatus()).toContain('Memory B');
    expect(memorySelect()!.disabled).toBe(true);
    expect(memoryButton().disabled).toBe(true);
    await vi.waitFor(() => expect(llmGets().length).toBeGreaterThan(getsBefore));
    await vi.waitFor(() => expect(memorySelect()!.value).toBe('mem-a'));
  });

  it('shows the outcome once the status reads again', async () => {
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    jobReads = [{ status: 'running', job_id: 'job-1', memory_model: 'mem-b', started_at: 1 }];
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toContain('Switching the memory to Memory B'));
    jobReads = [{ httpStatus: 503, message: UNREADABLE }];
    await vi.waitFor(() => expect(memoryStatus()).toContain(UNREADABLE));
    jobReads = [
      {
        status: 'done',
        job_id: 'job-1',
        memory_model: 'mem-b',
        memory_model_changed: true,
        message: 'The memory extraction model was switched to mem-b and saved for the next deploy.',
      },
    ];
    await vi.waitFor(() =>
      expect(memoryStatus()).toBe(
        '✓ The memory extraction model was switched to mem-b and saved for the next deploy.',
      ),
    );
    await vi.waitFor(() => expect(memorySelect()!.value).toBe('mem-b'));
    expect(memorySelect()!.disabled).toBe(false);
  });
});

describe('a switch whose outcome is unknown', () => {
  it('is shown as unknown, not as an error', async () => {
    const message =
      'The memory extraction model switch to mem-b has not reported how it ended. Reload this ' +
      'page to read what the memory runs and what is saved.';
    jobReads = [{ status: 'unknown', stale: true, job_id: 'job-1', memory_model: 'mem-b', message }];
    afterJobDone = null;
    await renderTab('mem-a');
    await choose('settings-memory-model', 'mem-b');
    await click('settings-memory-model-btn');
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(memoryStatus()).toBe(`⚠ ${message}`));
    await vi.waitFor(() => expect(memorySelect()!.value).toBe('mem-a'));
  });
});

describe('a saved model the memory does not run', () => {
  it('names both under the select', async () => {
    llmSettings = { ...llmSettings, memory_model: 'mem-a', memory_model_saved: 'mem-b' };
    await renderTab('mem-a');
    await vi.waitFor(() =>
      expect(document.getElementById('settings-memory-model-saved')?.textContent).toBe(
        'Saved for the next deploy: Memory B. The memory runs Memory A now.',
      ),
    );
  });

  it('says nothing when there is no difference', async () => {
    await renderTab('mem-a');
    expect(document.getElementById('settings-memory-model-saved')).toBeNull();
    expect(document.getElementById('settings-memory-model-saved-invalid')).toBeNull();
    expect(document.getElementById('settings-memory-model-unsaved')).toBeNull();
  });
});

describe('a saved value the next deploy ignores', () => {
  it('names the saved text apart from drift', async () => {
    llmSettings = {
      ...llmSettings,
      memory_model: 'mem-a',
      memory_model_saved_invalid: 'global.anthropic.claude-sonnet-4-6',
    };
    await renderTab('mem-a');
    await vi.waitFor(() =>
      expect(document.getElementById('settings-memory-model-saved-invalid')?.textContent).toBe(
        'A saved memory model (global.anthropic.claude-sonnet-4-6) is not one this deployment ' +
          'offers; the next deploy ignores it.',
      ),
    );
    expect(document.getElementById('settings-memory-model-saved')).toBeNull();
  });
});

describe('a model nothing saved would keep', () => {
  beforeEach(() => {
    llmSettings = {
      ...llmSettings,
      memory_model: 'mem-b',
      memory_model_unsaved: true,
      memory_model_next_deploy: 'mem-a',
    };
  });

  it('says so under the select', async () => {
    await renderTab('mem-b');
    await vi.waitFor(() =>
      expect(document.getElementById('settings-memory-model-unsaved')?.textContent).toBe(
        'The memory runs Memory B, but nothing is saved for the next deploy, which would put ' +
          'Memory A back. Save Memory B to keep it.',
      ),
    );
  });

  it('saving the model the memory runs is the repair, with its own confirm', async () => {
    jobReads = [
      {
        status: 'done',
        job_id: 'job-1',
        memory_model: 'mem-b',
        memory_model_changed: false,
        message: 'The memory already ran mem-b; it is now saved for the next deploy, so a deploy keeps it.',
      },
    ];
    afterJobDone = () => {
      llmSettings = { ...llmSettings, memory_model_unsaved: undefined, memory_model_next_deploy: undefined };
    };
    await renderTab('mem-b');
    expect(memoryButton().disabled).toBe(false);
    await click('settings-memory-model-btn');
    expect(confirmText()).toEqual({
      title: 'Save the memory extraction model?',
      body: 'The memory already runs this model; nothing is switched. Saving it keeps it on the next deploy.',
      buttons: ['Cancel', 'Save memory model'],
    });
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(writes()).toEqual([{ url: SWITCH, method: 'POST', body: { memory_model: 'mem-b' } }]),
    );
    await vi.waitFor(() => expect(memoryStatus()).toContain('✓ The memory already ran mem-b'));
    await vi.waitFor(() => expect(document.getElementById('settings-memory-model-unsaved')).toBeNull());
  });
});
