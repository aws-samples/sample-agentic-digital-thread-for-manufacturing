// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Wave 2 / Task 2c — <ConceptProposalCard/>.
 *
 * Load-bearing properties:
 *  - the steward click is the ONLY write trigger, and Accept posts EXACTLY
 *    the selected rows in the /import body shape to the onboard-gated route;
 *  - skip/collision rows can never be selected (server classification wins);
 *  - a 403 is an honest error state, never a pre-disabled gate (flag 2 (b));
 *  - the import result is rendered row-by-row and a failed write never reads
 *    as success;
 *  - the post-success chip dispatches "run map_fields" through the chat-input
 *    path (welcomeSubmit) — the card never calls the agent directly.
 * Generic placeholder data only (system-a / widget). */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/chat', () => ({ welcomeSubmit: vi.fn() }));

import { welcomeSubmit } from '../views/onboard/chat';
import {
  ConceptProposalCard,
  ONBOARD_VOCAB_IMPORT_URL,
  type ConceptProposalResult,
} from '../views/onboard/components/ConceptProposalCard';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const RESULT: ConceptProposalResult = {
  system_id: 'system-a',
  proposals: [
    {
      id: 'widget-yield',
      name: 'Widget yield',
      description: 'Share of widgets passing final check.',
      action: 'insert',
    },
    {
      id: 'cycle-duration',
      name: 'Cycle duration',
      description: 'Elapsed time of one processing cycle.',
      action: 'insert',
    },
    {
      id: 'part-count',
      name: 'Part count',
      description: 'Number of parts produced.',
      action: 'skip_exact',
    },
    {
      id: 'part_count',
      name: 'Part count (variant)',
      description: 'Duplicate spelling of an existing concept.',
      action: 'skip_collision',
      collides_with: 'part-count',
    },
  ],
  insert_count: 2,
  skip_count: 1,
  collision_count: 1,
};

type Call = { url: string; init: RequestInit };

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function stub(bodies: unknown[]): Call[] {
  const calls: Call[] = [];
  let i = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      const body = bodies[Math.min(i, bodies.length - 1)];
      i += 1;
      if (body instanceof Error) throw body;
      if (body && typeof body === 'object' && '__status' in body) {
        const { __status, ...rest } = body as { __status: number } & Record<string, unknown>;
        return response(rest, __status);
      }
      return response(body);
    }),
  );
  return calls;
}

function checkbox(id: string): HTMLInputElement {
  return container.querySelector<HTMLInputElement>(`input[aria-label="select ${id}"]`)!;
}

function acceptButton(): HTMLButtonElement {
  return Array.from(container.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes('Accept selected'),
  ) as HTMLButtonElement;
}

async function mount(result = RESULT, sendChat?: (msg: string) => void) {
  await act(async () => {
    root.render(<ConceptProposalCard result={result} sendChat={sendChat} />);
  });
}

const OK_IMPORT = {
  status: 'ok',
  inserted: 2,
  skipped: 0,
  failed: 0,
  skipped_ids: [],
  collisions: [],
};

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  vi.mocked(welcomeSubmit).mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('row selection', () => {
  it('checks insert rows by default and leaves skip rows unchecked + disabled', async () => {
    stub([]);
    await mount();
    expect(checkbox('widget-yield').checked).toBe(true);
    expect(checkbox('cycle-duration').checked).toBe(true);
    expect(checkbox('part-count').checked).toBe(false);
    expect(checkbox('part-count').disabled).toBe(true);
    expect(checkbox('part_count').checked).toBe(false);
    expect(checkbox('part_count').disabled).toBe(true);
  });

  it('labels skip rows honestly', async () => {
    stub([]);
    await mount();
    expect(container.textContent).toMatch(/already in vocabulary/);
    expect(container.textContent).toMatch(/collides with part-count/);
  });

  it('collision rows cannot be selected', async () => {
    stub([]);
    await mount();
    await act(async () => {
      checkbox('part_count').click();
    });
    expect(checkbox('part_count').checked).toBe(false);
  });
});

describe('accept', () => {
  it('posts exactly the selected rows in the /import body shape to the onboard route', async () => {
    const calls = stub([OK_IMPORT]);
    await mount();
    await act(async () => {
      checkbox('cycle-duration').click(); // deselect one insert row
    });
    await act(async () => {
      acceptButton().click();
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(ONBOARD_VOCAB_IMPORT_URL);
    const body = JSON.parse(String(calls[0].init.body));
    expect(body).toEqual({
      metadata: { name: 'onboarding-proposal-system-a' },
      concepts: [
        {
          id: 'widget-yield',
          name: 'Widget yield',
          description: 'Share of widgets passing final check.',
        },
      ],
    });
  });

  it('disables Accept when nothing is selected — but never on role', async () => {
    stub([]);
    await mount();
    expect(acceptButton().disabled).toBe(false);
    await act(async () => {
      checkbox('widget-yield').click();
      checkbox('cycle-duration').click();
    });
    expect(acceptButton().disabled).toBe(true);
  });

  it('renders a 403 as an honest role message, not a silent failure', async () => {
    stub([{ __status: 403, detail: 'Forbidden' }]);
    await mount();
    await act(async () => {
      acceptButton().click();
    });
    expect(container.textContent).toMatch(
      /your role cannot import concepts — ask an admin/i,
    );
  });

  it('renders a transport failure without claiming anything was written', async () => {
    stub([new Error('network down')]);
    await mount();
    await act(async () => {
      acceptButton().click();
    });
    expect(container.textContent).toMatch(/import failed — nothing was confirmed written/i);
  });
});

describe('import result', () => {
  it('renders the outcome row-by-row and names skipped and collided rows', async () => {
    stub([
      {
        status: 'ok',
        inserted: 1,
        skipped: 1,
        failed: 0,
        skipped_ids: ['cycle-duration'],
        collisions: [{ id: 'widget-yield', existing_id: 'widget_yield' }],
      },
    ]);
    // Only widget-yield + cycle-duration are selectable; post both.
    await mount();
    await act(async () => {
      acceptButton().click();
    });
    expect(container.textContent).toMatch(/cycle-duration — already in vocabulary \(skipped\)/);
    expect(container.textContent).toMatch(/widget-yield — collides with widget_yield; not written/);
  });

  it('names failed rows — a failed write never reads as success', async () => {
    stub([
      {
        status: 'ok',
        inserted: 1,
        skipped: 0,
        failed: 1,
        skipped_ids: [],
        collisions: [],
      },
    ]);
    await mount();
    await act(async () => {
      acceptButton().click();
    });
    expect(container.textContent).toMatch(/widget-yield — write not confirmed/);
    expect(container.textContent).toMatch(/cycle-duration — write not confirmed/);
    expect(container.textContent).toMatch(/1 write failed/);
    expect(container.textContent).not.toMatch(/— added/);
  });

  it('shows the added count and a chip that dispatches "run map_fields" via the chat-input path', async () => {
    stub([OK_IMPORT]);
    const sendChat = vi.fn();
    await mount(RESULT, sendChat);
    await act(async () => {
      acceptButton().click();
    });
    expect(container.textContent).toMatch(/2 concepts added — mapping will use them/);
    const chip = Array.from(container.querySelectorAll('button')).find((b) =>
      (b.textContent ?? '').includes('Run map_fields'),
    )!;
    await act(async () => {
      chip.click();
    });
    expect(sendChat).toHaveBeenCalledWith('run map_fields');
  });

  it('defaults the chip dispatcher to welcomeSubmit (the existing chat-input path)', async () => {
    stub([OK_IMPORT]);
    await mount(RESULT, undefined);
    await act(async () => {
      acceptButton().click();
    });
    const chip = Array.from(container.querySelectorAll('button')).find((b) =>
      (b.textContent ?? '').includes('Run map_fields'),
    )!;
    await act(async () => {
      chip.click();
    });
    expect(welcomeSubmit).toHaveBeenCalledWith('run map_fields');
  });

  it('locks further accepts after a completed import', async () => {
    stub([OK_IMPORT]);
    await mount();
    await act(async () => {
      acceptButton().click();
    });
    expect(acceptButton().disabled).toBe(true);
    expect(checkbox('widget-yield').disabled).toBe(true);
  });
});

describe('empty proposals', () => {
  it('renders a compact no-proposals line', async () => {
    stub([]);
    await mount({ system_id: 'system-a', proposals: [] });
    expect(container.textContent).toMatch(/no new concepts proposed/i);
    expect(acceptButton()).toBeUndefined();
  });
});
