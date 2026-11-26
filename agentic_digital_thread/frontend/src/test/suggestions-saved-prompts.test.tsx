// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Saved prompts as suggestion chips. A deployment without the demo prompt
 * pack has no registry prompts, and the server only writes suggested
 * questions for some system mixes, so the chat showed no chips at all even
 * when the Prompt Library held the team's own questions. With no registry
 * prompts and no server questions, the chips now come from saved prompts,
 * grouped by category. Registry prompts and server questions still win. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getPromptQueryClient } from '../chat/prompt-queries';
import { setLang } from '../lib/i18n';
import * as chat from '../views/onboard/chat';
import { refreshSuggestions } from '../views/onboard/chat';
import { PersonaGroups } from '../views/onboard/components/PersonaGroups';
import { getOnboardQueryClient } from '../views/onboard/onboard-queries';
import { setSuggestions, suggestionsStore } from '../views/onboard/suggestions-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function json(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const SAVED = [
  { template_id: 'u1', source: 'user', category: 'Line golden', title: 'B1 schema', text: 'What data does the MES hold?' },
  { template_id: 'u2', source: 'user', category: 'Line golden', title: 'N5 count', text: 'How many segments completed?' },
  { template_id: 'u3', source: 'user', category: 'Line team', title: 'Mechanical', text: 'Any mechanical issues?' },
  { template_id: 'u4', source: 'user', title: 'Uncategorized', text: 'Show active alarms' },
  { template_id: 'u5', source: 'user', category: 'Line team', title: 'Empty', text: '' },
];
const REGISTRY = [
  { template_id: 'r1', source: 'registry', persona: 'Plant Manager', title: 'Briefing', text: 'Give me a briefing' },
];

let questions: string[];
let prompts: unknown[];
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  const ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  questions = [];
  prompts = SAVED;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (String(url).includes('/api/onboard/status')) {
        return json(200, {
          onboarded_systems: [{ id: 'mes-1', system_type: 'MES' }],
          suggested_questions: questions,
        });
      }
      if (String(url).includes('/api/prompts')) return json(200, prompts);
      return json(200, {});
    }),
  );
  setLang('en');
  setSuggestions({ kind: 'loading' });
  getOnboardQueryClient().clear();
  getPromptQueryClient().clear();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  (chat as { _resetSuggestionsRetry?: () => void })._resetSuggestionsRetry?.();
  setSuggestions({ kind: 'loading' });
  getOnboardQueryClient().clear();
  getPromptQueryClient().clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function load(): Promise<void> {
  await act(async () => {
    root.render(<PersonaGroups />);
  });
  await act(async () => {
    await refreshSuggestions();
  });
}

describe('saved prompts as suggestion chips', () => {
  it('groups saved prompts by category when there are no registry prompts or server questions', async () => {
    await load();
    const state = suggestionsStore.get();
    expect(state.kind).toBe('personas');
    if (state.kind !== 'personas') return;
    const groups = Object.fromEntries(state.groups.map((g) => [g.persona, g.prompts.map((p) => p.title)]));
    expect(groups).toEqual({
      'Line golden': ['B1 schema', 'N5 count'],
      'Line team': ['Mechanical'],
      'Saved prompts': ['Uncategorized'],
    });
    expect(container.textContent).toContain('Line golden');
    // The full text is what gets sent.
    const n5 = state.groups[0].prompts.find((p) => p.title === 'N5 count');
    expect(n5?.text).toBe('How many segments completed?');
  });

  it('keeps server questions ahead of saved prompts', async () => {
    questions = ['Which lines are down?'];
    await load();
    expect(suggestionsStore.get()).toEqual({ kind: 'plain', questions: ['Which lines are down?'] });
  });

  it('keeps registry prompts ahead of saved prompts', async () => {
    prompts = [...SAVED, ...REGISTRY];
    await load();
    const state = suggestionsStore.get();
    expect(state.kind).toBe('personas');
    if (state.kind !== 'personas') return;
    expect(state.groups.map((g) => g.persona)).toEqual(['Plant Manager']);
  });

  it('labels an untitled saved prompt with its text', async () => {
    prompts = [{ template_id: 'u9', source: 'user', category: 'Team', title: '', text: 'Which lines stopped overnight?' }];
    await load();
    const state = suggestionsStore.get();
    expect(state.kind).toBe('personas');
    if (state.kind !== 'personas') return;
    expect(state.groups[0].prompts[0].title).toBe('Which lines stopped overnight?');
  });

  it('orders saved prompts deterministically and caps how many become chips', async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      template_id: `m${i}`,
      source: 'user',
      category: i % 2 ? 'B group' : 'A group',
      title: `Q${String(59 - i).padStart(2, '0')}`,
      text: `question ${i}`,
    }));
    prompts = many;
    await load();
    const first = suggestionsStore.get();
    prompts = [...many].reverse();
    getPromptQueryClient().clear();
    getOnboardQueryClient().clear();
    await act(async () => {
      setSuggestions({ kind: 'loading' });
      await refreshSuggestions();
    });
    expect(suggestionsStore.get()).toEqual(first);
    if (first.kind !== 'personas') throw new Error(first.kind);
    expect(first.groups.map((g) => g.persona)).toEqual(['A group', 'B group']);
    const total = first.groups.reduce((n, g) => n + g.prompts.length, 0);
    expect(total).toBe(chat.SAVED_PROMPT_CHIP_LIMIT);
    for (const g of first.groups) {
      const titles = g.prompts.map((p) => p.title);
      expect(titles).toEqual([...titles].sort());
    }
  });

  it('a chip click sends the full text and shows the title as the user turn', async () => {
    const w = window as unknown as { sendMessage?: () => void; _nextUserDisplayText?: string };
    const input = document.createElement('input');
    input.id = 'question-input';
    document.body.appendChild(input);
    let sent: { text: string; display?: string } | null = null;
    w.sendMessage = () => {
      sent = { text: input.value, display: w._nextUserDisplayText };
    };
    try {
      await load();
      const chip = Array.from(container.querySelectorAll<HTMLElement>('.suggestion-chip')).find(
        (el) => el.textContent === 'N5 count',
      );
      expect(chip).toBeTruthy();
      await act(async () => {
        chip!.click();
      });
      expect(sent).toEqual({ text: 'How many segments completed?', display: 'N5 count' });
    } finally {
      input.remove();
      delete w.sendMessage;
      delete w._nextUserDisplayText;
    }
  });

  it('skips saved prompts whose text is only whitespace', async () => {
    prompts = [{ template_id: 'w1', source: 'user', category: 'Team', title: '', text: '   \n ' }];
    await load();
    expect(suggestionsStore.get()).toEqual({ kind: 'plain', questions: [] });
  });

  it('shows nothing when there are no saved prompts either', async () => {
    prompts = [];
    await load();
    expect(suggestionsStore.get()).toEqual({ kind: 'plain', questions: [] });
  });
});
