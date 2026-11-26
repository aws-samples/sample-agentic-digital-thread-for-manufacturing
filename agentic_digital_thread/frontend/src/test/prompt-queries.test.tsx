// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { notifyManager, QueryClientProvider } from '@tanstack/react-query';
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { SaveModal } from '../chat/PromptLibrary';
import { ModalSurface } from '../shared/ui/ModalSurface';
import {
  PROMPTS_REFETCH_INTERVAL_MS,
  useDeletePromptMutation,
  usePrompts,
  useSavePromptMutation,
  useSchedulePromptMutation,
  useUpdatePromptMutation,
} from '../chat/prompt-queries';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
notifyManager.setNotifyFunction((callback) => act(callback));

const roots: Root[] = [];
const containers: HTMLDivElement[] = [];

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function mount(element: React.ReactElement): { root: Root; container: HTMLDivElement } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.push(root);
  containers.push(container);
  act(() => root.render(element));
  return { root, container };
}

function PromptConsumer({ label }: { label: string }) {
  const prompts = usePrompts();
  return <span data-testid={label}>{prompts.data?.length ?? 'loading'}</span>;
}

function MutationProbe() {
  const save = useSavePromptMutation();
  const update = useUpdatePromptMutation();
  const remove = useDeletePromptMutation();
  const schedule = useSchedulePromptMutation();
  return (
    <div>
      <button onClick={() => save.mutate({ text: 'Question', title: 'Saved' })}>save</button>
      <button
        onClick={() => update.mutate({ promptId: 'prompt 1', updates: { title: 'Edited' } })}
      >
        update
      </button>
      <button onClick={() => remove.mutate({ promptId: 'prompt 1' })}>delete</button>
      <button
        onClick={() => schedule.mutate({ promptId: 'prompt 1', scheduleText: 'daily at 7am' })}
      >
        schedule
      </button>
      <output data-testid="save-status">{save.status}</output>
    </div>
  );
}

function NestedSaveHarness() {
  const [saveOpen, setSaveOpen] = useState(false);
  return (
    <ModalSurface open onClose={() => {}} label="Chat dialog" className="chat-modal-overlay">
      <button id="open-save" type="button" onClick={() => setSaveOpen(true)}>Save prompt</button>
      {saveOpen ? (
        <SaveModal
          state={{ defaultText: 'Question', compose: true }}
          onClose={() => setSaveOpen(false)}
          getConversation={() => []}
        />
      ) : null}
    </ModalSurface>
  );
}

function withClient(client: ReturnType<typeof createQueryClient>, children: React.ReactNode) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  for (const container of containers.splice(0)) container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('prompt queries', () => {
  it('deduplicates one /api/prompts request across two prompt consumers', async () => {
    const fetchMock = vi.fn(async () => response([{ id: 'p1', text: 'Question' }]));
    vi.stubGlobal('fetch', fetchMock);
    const client = createQueryClient();
    const { container } = mount(
      withClient(
        client,
        <>
          <PromptConsumer label="sidebar" />
          <PromptConsumer label="modal" />
        </>,
      ),
    );

    await vi.waitFor(() => {
      expect(container.querySelector('[data-testid="sidebar"]')?.textContent).toBe('1');
      expect(container.querySelector('[data-testid="modal"]')?.textContent).toBe('1');
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith('/api/prompts', expect.any(Object));
  });

  it('reuses fresh prompt data when a prompt surface closes and reopens', async () => {
    const fetchMock = vi.fn(async () => response([{ id: 'p1' }]));
    vi.stubGlobal('fetch', fetchMock);
    const client = createQueryClient();
    const { root, container } = mount(withClient(client, <PromptConsumer label="first" />));

    await vi.waitFor(() => {
      expect(container.querySelector('[data-testid="first"]')?.textContent).toBe('1');
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    act(() => root.render(withClient(client, null)));
    act(() => root.render(withClient(client, <PromptConsumer label="second" />)));

    await vi.waitFor(() => {
      expect(container.querySelector('[data-testid="second"]')?.textContent).toBe('1');
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('refetches prompts on the five-minute schedule', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => response([{ id: 'p1' }]));
    vi.stubGlobal('fetch', fetchMock);
    const client = createQueryClient();
    mount(withClient(client, <PromptConsumer label="scheduled" />));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledOnce();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PROMPTS_REFETCH_INTERVAL_MS - 1);
    });
    expect(fetchMock).toHaveBeenCalledOnce();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('invalidates the prompt key after each successful save, edit, delete, and schedule', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/schedule')) return response({ cron: '0 7 * * *' });
      if (init?.method === 'POST') return response({ id: 'p1' });
      return response({ success: true });
    });
    vi.stubGlobal('fetch', fetchMock);
    const client = createQueryClient();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const { container } = mount(withClient(client, <MutationProbe />));
    const buttons = [...container.querySelectorAll('button')];

    for (let index = 0; index < buttons.length; index += 1) {
      act(() => buttons[index].click());
      await vi.waitFor(() => expect(invalidate).toHaveBeenCalledTimes(index + 1));
    }

    for (const call of invalidate.mock.calls) {
      expect(call[0]).toEqual({ queryKey: queryKeys.prompts() });
    }
    expect(fetchMock.mock.calls.map(([url, init]) => [String(url), (init as RequestInit).method])).toEqual([
      ['/api/prompts', 'POST'],
      ['/api/prompts/prompt%201', 'PUT'],
      ['/api/prompts/prompt%201', 'DELETE'],
      ['/api/prompts/prompt%201/schedule', 'POST'],
    ]);
  });

  it('keeps SaveModal as the top nested dialog, traps focus, and Escape closes only it', () => {
    const client = createQueryClient();
    const { container } = mount(withClient(client, <NestedSaveHarness />));
    const trigger = container.querySelector<HTMLButtonElement>('#open-save')!;
    trigger.focus();
    act(() => trigger.click());

    const dialogs = container.querySelectorAll<HTMLElement>('[role="dialog"]');
    expect(dialogs).toHaveLength(2);
    expect(document.activeElement?.id).toBe('save-modal-prompt');
    expect(document.body.style.overflow).toBe('hidden');

    const first = container.querySelector<HTMLTextAreaElement>('#save-modal-prompt')!;
    const last = container.querySelector<HTMLButtonElement>('.save-modal-actions .primary')!;
    first.focus();
    act(() => first.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true })));
    expect(document.activeElement).toBe(last);

    act(() => last.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(container.querySelectorAll('[role="dialog"]')).toHaveLength(1);
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe('hidden');
  });

  it('does not invalidate prompts when a mutation fails', async () => {
    const fetchMock = vi.fn(async () => response({ detail: 'save rejected' }, 500));
    vi.stubGlobal('fetch', fetchMock);
    const client = createQueryClient();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const { container } = mount(withClient(client, <MutationProbe />));

    act(() => container.querySelector<HTMLButtonElement>('button')!.click());
    await vi.waitFor(() => {
      expect(container.querySelector('[data-testid="save-status"]')?.textContent).toBe('error');
    });
    expect(invalidate).not.toHaveBeenCalled();
  });
});

afterAll(() => {
  notifyManager.setNotifyFunction((callback) => callback());
});
