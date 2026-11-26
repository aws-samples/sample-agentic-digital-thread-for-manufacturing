// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { readFileSync, readdirSync } from 'node:fs';
import ts from 'typescript';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import LoginPage from '../pages/login/LoginPage';
import { fillVersionBadge } from '../lib/version-badge';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SRC_ROOT = `${process.cwd()}/src`;
const PRODUCTION_EXTENSIONS = new Set(['.ts', '.tsx', '.js', '.jsx']);

function productionFiles(dir = SRC_ROOT): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) return entry.name === 'test' ? [] : productionFiles(path);
    const extension = entry.name.slice(entry.name.lastIndexOf('.'));
    return PRODUCTION_EXTENSIONS.has(extension) ? [path] : [];
  });
}

interface DirectFetchCall {
  file: string;
  line: number;
  url: string;
}

function directFetchCalls(): DirectFetchCall[] {
  return productionFiles().flatMap((path) => {
    const sourceText = readFileSync(path, 'utf8');
    const source = ts.createSourceFile(
      path,
      sourceText,
      ts.ScriptTarget.Latest,
      true,
      path.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
    const calls: DirectFetchCall[] = [];
    const visit = (node: ts.Node) => {
      if (
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === 'fetch'
      ) {
        const argument = node.arguments[0];
        calls.push({
          file: path.slice(SRC_ROOT.length + 1),
          line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1,
          url: argument?.getText(source) ?? '',
        });
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
    return calls;
  }).sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line);
}

const DIRECT_JSON_FILES = [
  'contexts/BrandingContext.tsx',
  'pages/app/AppShell.tsx',
  'pages/app/UserMenu.tsx',
  'pages/login/LoginPage.tsx',
  'pages/admin/AdminPage.tsx',
  'pages/admin/tabs/SettingsTab.tsx',
  'pages/admin/tabs/DemoScriptTab.tsx',
  'chat/PromptLibrary.tsx',
  'chat/Feedback.tsx',
  'chat/rehydrate.ts',
] as const;

const QUERY_ROUTED_FILES = [
  'lib/version-badge.ts',
  'api/reference-queries.ts',
  'shared/agents/AgentsFleet.tsx',
  'shared/agents/agent-queries.ts',
  'shared/agents/pricing.ts',
] as const;

const PRODUCTION_CALLER_FILES = [
  ...DIRECT_JSON_FILES,
  'lib/version-badge.ts',
  'shared/agents/AgentsFleet.tsx',
] as const;

function response(status: number, body: unknown, statusText = ''): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

describe('request migration inventory', () => {
  it('allows direct production fetch only in the typed client and checked raw-text path', () => {
    const calls = directFetchCalls();
    expect(calls.map(({ file, url }) => ({ file, url }))).toEqual([
      { file: 'api/client.ts', url: 'url' },
      { file: 'pages/admin/tabs/DemoScriptTab.tsx', url: "'/demo-script.md'" },
    ]);
  });

  it('routes scoped JSON callers through the typed client without authFetch/null paths', () => {
    for (const file of DIRECT_JSON_FILES) {
      const source = readFileSync(`${SRC_ROOT}/${file}`, 'utf8');
      expect(source, `${file} should import/use requestJson`).toContain('requestJson');
    }

    for (const file of [...DIRECT_JSON_FILES, ...QUERY_ROUTED_FILES]) {
      const source = readFileSync(`${SRC_ROOT}/${file}`, 'utf8');
      expect(source, `${file} should not retain authFetch`).not.toMatch(/\bauthFetch\b/);
    }

    // Production callers must not restore the old authFetch contract where a
    // null Response silently represented a handled 401. The fleet's optional
    // raw-Response injection is asserted separately below as a test seam.
    for (const file of PRODUCTION_CALLER_FILES) {
      const source = readFileSync(`${SRC_ROOT}/${file}`, 'utf8');
      expect(source, `${file} should not return on a null response`).not.toMatch(
        /if\s*\(\s*!(?:resp|response)\s*\)\s*(?:return\b|\{[^}]*\breturn\b)/s,
      );
    }

    const versionBadge = readFileSync(`${SRC_ROOT}/lib/version-badge.ts`, 'utf8');
    expect(versionBadge).toContain('.fetchQuery(versionQueryOptions())');
    expect(versionBadge).not.toContain('requestJson');

    const referenceQueries = readFileSync(`${SRC_ROOT}/api/reference-queries.ts`, 'utf8');
    expect(referenceQueries).toContain('queryJson<VersionInfo>');
    expect(referenceQueries).toContain("'/api/version'");

    const fleet = readFileSync(`${SRC_ROOT}/shared/agents/AgentsFleet.tsx`, 'utf8');
    expect(fleet).toContain('fleetQueryOptions(hours, fetchFn)');
    expect(fleet).toContain('logTailQueryOptions(fetchFn)');
    expect(fleet).not.toMatch(/\brequestJson\s*[<(]/);

    const agentQueries = readFileSync(`${SRC_ROOT}/shared/agents/agent-queries.ts`, 'utf8');
    expect(agentQueries).toContain('if (!fetchFn) return queryJson<T>(url, signal)');

    const pricing = readFileSync(`${SRC_ROOT}/shared/agents/pricing.ts`, 'utf8');
    expect(pricing).toContain("requestJson<ModelCatalog>('/api/admin/models')");
  });

  it('keeps the markdown request checked as text and leaves streamed transports on their owners', () => {
    const demoScript = readFileSync(`${SRC_ROOT}/pages/admin/tabs/DemoScriptTab.tsx`, 'utf8');
    expect(demoScript).toContain("fetch('/demo-script.md')");
    expect(demoScript).toContain("if (!resp.ok) throw new Error('Not found')");
    expect(demoScript).toContain('await resp.text()');

    const sidebar = readFileSync(`${SRC_ROOT}/chat/ChatSidebar.tsx`, 'utf8');
    expect(sidebar).toContain('useAskStream');
    expect(sidebar).not.toMatch(/requestJson[^\n]*\/api\/ask/);

    const sse = readFileSync(`${SRC_ROOT}/api/sse.ts`, 'utf8');
    expect(sse).toContain('const doFetch = options.fetchFn ?? authFetch');
    expect(sse).toContain('response.body.getReader()');

    // Fleet polling now belongs to TanStack Query. Production takes queryJson;
    // only an explicitly injected focused-test seam handles raw Responses.
    const agentQueries = readFileSync(`${SRC_ROOT}/shared/agents/agent-queries.ts`, 'utf8');
    expect(agentQueries).toContain('const response = await fetchFn(url, { signal })');
    expect(agentQueries).toContain("if (!response) throw new Error('Authentication required')");

    const pricing = readFileSync(`${SRC_ROOT}/shared/agents/pricing.ts`, 'utf8');
    expect(pricing).toContain('if (fetchFn)');
    expect(pricing).toContain('cat = await requestJson<ModelCatalog>');
  });
});

describe('request migration behavior', () => {
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
  });

  it('surfaces the login error-envelope detail from ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(response(401, { detail: 'Account is temporarily locked' }, 'Unauthorized')),
    );
    vi.stubGlobal('location', { href: 'http://localhost/login.html' });
    await act(async () => root.render(createElement(LoginPage)));
    (document.getElementById('email') as HTMLInputElement).value = 'operator';
    (document.getElementById('password') as HTMLInputElement).value = 'wrong';

    await act(async () => {
      document.getElementById('login-form')!.dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }),
      );
    });

    expect(document.getElementById('error-msg')!.textContent).toBe(
      'Account is temporarily locked',
    );
    // The login 401 must NOT trigger the global session-expiry redirect: in a
    // real browser that reloads login.html and wipes the inline error before
    // the user can read it (caught by e2e; jsdom masked the reload).
    expect(window.location.href).toBe('http://localhost/login.html');
  });

  it('keeps a failed best-effort version badge absent instead of misreporting', async () => {
    const badge = document.createElement('span');
    badge.id = 'version-badge';
    badge.textContent = 'v...';
    container.appendChild(badge);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(response(503, { detail: 'Version service unavailable' }, 'Unavailable')),
    );

    fillVersionBadge();
    expect(badge.textContent).toBe('');
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(badge.textContent).toBe('');
  });
});
