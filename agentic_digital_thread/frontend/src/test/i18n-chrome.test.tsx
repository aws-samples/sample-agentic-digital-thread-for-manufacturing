// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — chrome i18n (lib/i18n.ts): the dictionaries carry
 * exactly the six locales with identical key sets (originally ported
 * byte-verbatim from the legacy static UI, removed 2026-07-31);
 * t() resolves per language with English→key fallback (never blank);
 * setLang persists to the stable localStorage key ('adt-lang',
 * i18n-lang.ts); useI18n re-renders chrome
 * (probe component + the real UserMenu labels) on language switch; and modal
 * follow-ups wrap the outgoing prompt with the response-language directive
 * (the modal-follow-up delta) while UI copies stay unwrapped. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { postSse } from '../api/sse';
import { createQueryClient } from '../api/query-client';
import { AuthProvider } from '../contexts/AuthContext';
import { setLang, t, useI18n } from '../lib/i18n';
import UserMenu from '../pages/app/UserMenu';
import { DataModal, dataModalStore, sendModalFollowup } from '../shared/thread/data-modal';

vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// vitest cwd = frontend/ (jsdom env: import.meta.url is not a file: URL here).
const PORTED_PATH = path.resolve(process.cwd(), 'src/lib/i18n.ts');

/** The I18N dictionaries, evaluated from the shipped object literal
 * (pure data — no code) so the structural checks run against the real
 * source of truth in lib/i18n.ts. */
function portedDicts(): Record<string, Record<string, string>> {
  const src = readFileSync(PORTED_PATH, 'utf-8');
  const decl = 'const I18N: Record<string, Record<string, string>> = {';
  const start = src.indexOf(decl);
  const end = src.indexOf('\n};', start) + 3; // past '};'
  const objText = src.slice(src.indexOf('{', start + decl.length - 1), end - 1); // '{ ... }'
  return new Function(`return ${objText}`)() as Record<string, Record<string, string>>;
}

// This node/jsdom combo ships a broken localStorage (getItem is not a
// function) — stub a Map-backed store (same pattern as user-menu.test.tsx).
let store: Map<string, string>;

beforeEach(() => {
  store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
  });
  // langStore is module state — reset the shared language between tests.
  setLang('en');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('dictionaries — structural contract (lib/i18n.ts)', () => {
  it('carries exactly the six locales with identical key sets', () => {
    const dicts = portedDicts();
    expect(Object.keys(dicts)).toEqual(['en', 'ja', 'de', 'fr', 'ko', 'zh']);
    const enKeys = Object.keys(dicts.en);
    expect(enKeys.length).toBeGreaterThan(0);
    for (const lang of Object.keys(dicts)) {
      expect(Object.keys(dicts[lang])).toEqual(enKeys);
    }
  });

  it('t() resolves EVERY key in EVERY locale to the shipped value', () => {
    const dicts = portedDicts();
    for (const [lang, dict] of Object.entries(dicts)) {
      setLang(lang);
      for (const [key, value] of Object.entries(dict)) {
        expect(t(key), `${lang}:${key}`).toBe(value);
      }
    }
  });
});

describe('t() — resolution and fallback', () => {
  it('resolves per language (spot checks)', () => {
    expect(t('nav.dashboard')).toBe('Dashboard'); // en default
    setLang('ja');
    expect(t('nav.dashboard')).toBe('ダッシュボード');
    setLang('de');
    expect(t('menu.signOut')).toBe('Abmelden');
    setLang('fr');
    expect(t('menu.language')).toBe('Langue');
    setLang('ko');
    expect(t('chat.send')).toBe('전송');
    setLang('zh');
    expect(t('nav.architecture')).toBe('架构');
  });

  it('falls back to English for a language without a dictionary', () => {
    setLang('pt'); // persisted, but no dictionary → UI stays English
    expect(t('nav.dashboard')).toBe('Dashboard');
    expect(t('menu.signOut')).toBe('Sign out');
  });

  it('falls back to the key itself for an unknown key — never blank', () => {
    setLang('ja');
    expect(t('no.such.key')).toBe('no.such.key');
  });
});

describe('persistence — same key as the legacy fallback', () => {
  it("setLang writes localStorage 'adt-lang' (i18n-lang.js key)", () => {
    setLang('de');
    expect(localStorage.getItem('adt-lang')).toBe('de');
  });

  it('mirrors the applied UI language onto <html lang>', () => {
    setLang('ja');
    expect(document.documentElement.lang).toBe('ja');
    setLang('pt'); // no dictionary → documentElement.lang guards to 'en'
    expect(document.documentElement.lang).toBe('en');
  });
});

describe('chrome re-render on language switch', () => {
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
  });

  it('useI18n subscribers re-render when setLang changes the store', () => {
    function Probe() {
      const { t: tr } = useI18n();
      return <span id="i18n-probe">{tr('nav.onboard')}</span>;
    }
    act(() => root.render(<Probe />));
    expect(document.getElementById('i18n-probe')!.textContent).toBe('Onboard');
    act(() => setLang('ja'));
    expect(document.getElementById('i18n-probe')!.textContent).toBe('オンボード');
    act(() => setLang('zh'));
    expect(document.getElementById('i18n-probe')!.textContent).toBe('系统接入');
  });

  it('UserMenu labels and lang-select follow the language', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/auth/me'
          ? ({
              ok: true,
              status: 200,
              json: async () => ({
                user_id: 'u1',
                email: 'ops@example.com',
                username: null,
                is_admin: true,
              }),
            } as unknown as Response)
          : new Promise<Response>(() => {}),
      ),
    );
    const queryClient = createQueryClient();
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <UserMenu />
          </AuthProvider>
        </QueryClientProvider>,
      );
    });
    const dropdown = () => document.getElementById('user-dropdown')!;
    expect(dropdown().textContent).toContain('Sign out');
    expect(dropdown().textContent).toContain('Preferences');
    act(() => setLang('ja'));
    expect(dropdown().textContent).toContain('サインアウト');
    expect(dropdown().textContent).toContain('設定');
    expect(dropdown().textContent).toContain('管理パネル');
    expect((document.getElementById('lang-select') as HTMLSelectElement).value).toBe('ja');
    expect(document.getElementById('user-menu-trigger')!.getAttribute('title')).toBe(
      'アカウントメニュー',
    );
  });
});

describe('modal follow-ups — response-language directive (thread.js delta)', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    dataModalStore.set({
      open: false,
      title: '',
      contentMd: null,
      conversation: [],
      followupTurns: [],
    });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<DataModal />));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const input = () => document.getElementById('data-modal-chat-input') as HTMLInputElement;

  it('wraps the outgoing prompt with the selected language directive', async () => {
    act(() => setLang('ja'));
    input().value = 'What changed on Line 2?';
    await act(async () => sendModalFollowup());
    const body = vi.mocked(postSse).mock.calls[0][1] as { question: string };
    expect(body.question.startsWith('What changed on Line 2?\n\n[OUTPUT LANGUAGE:')).toBe(true);
    expect(body.question).toContain('Japanese (日本語)');
    expect(body.question).toContain('Do NOT translate the following');
    expect(body.question.endsWith(']')).toBe(true);
    // The rendered turn keeps the RAW question — the directive is wire-only.
    expect(dataModalStore.get().followupTurns[0].question).toBe('What changed on Line 2?');
  });

  it('leaves the prompt unchanged for English', async () => {
    act(() => setLang('en'));
    input().value = 'What changed on Line 2?';
    await act(async () => sendModalFollowup());
    const body = vi.mocked(postSse).mock.calls[0][1] as { question: string };
    expect(body.question).toBe('What changed on Line 2?');
  });

  it('translates the follow-up placeholder and Send (legacy data-i18n hooks)', () => {
    expect(input().placeholder).toBe('Ask a follow-up question about this result...');
    act(() => setLang('ja'));
    expect(input().placeholder).toBe('この結果についてフォローアップ質問をする…');
    expect(document.getElementById('data-modal-followup')!.textContent).toContain('送信');
  });
});
