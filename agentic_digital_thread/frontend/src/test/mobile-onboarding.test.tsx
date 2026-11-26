// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, useEffect, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';

const h = vi.hoisted(() => ({ flowMounts: 0 }));

vi.mock('../views/onboard/catalog', () => ({ obLoadCatalog: vi.fn() }));
vi.mock('../views/onboard/chat', () => ({ obWelcomeChat: vi.fn() }));
vi.mock('../views/onboard/flow', () => ({ obResetAll: vi.fn() }));
vi.mock('../views/onboard/globals', () => ({ registerOnboardGlobals: vi.fn() }));
vi.mock('../views/onboard/state', () => ({ loadConcepts: vi.fn() }));
vi.mock('../views/onboard/components/CatalogList', () => ({
  CatalogList: ({ onNavigate }: { onNavigate?: () => void }) => (
    <button data-testid="choose-system" onClick={onNavigate}>
      Choose system
    </button>
  ),
}));
vi.mock('../views/onboard/components/ObFlow', () => ({
  ObFlow: () => {
    const [step, setStep] = useState('discover');
    useEffect(() => {
      h.flowMounts += 1;
    }, []);
    return (
      <section id="onboard-flow" data-testid="flow" data-step={step}>
        <button onClick={() => setStep('mapping')}>Advance wizard</button>
      </section>
    );
  },
}));
vi.mock('../views/onboard/components/ObModals', () => ({
  ObModals: () => null,
}));

import OnboardView from '../views/onboard/OnboardView';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ONBOARD_CSS = readFileSync(
  path.resolve(process.cwd(), 'src/styles/onboard.css'),
  'utf8',
);

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  h.flowMounts = 0;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  const queryClient = createQueryClient();
  act(() =>
    root.render(
      <QueryClientProvider client={queryClient}>
        <OnboardView />
      </QueryClientProvider>,
    ),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe('mobile onboarding catalog drawer', () => {
  it('opens and closes Systems without remounting or resetting the active wizard', () => {
    const toggle = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent?.trim().endsWith('Systems'),
    ) as HTMLButtonElement;
    const flow = container.querySelector('[data-testid="flow"]') as HTMLElement;
    const advance = Array.from(flow.querySelectorAll('button')).find(
      (button) => button.textContent === 'Advance wizard',
    ) as HTMLButtonElement;

    expect(toggle).toBeTruthy();
    expect(toggle.getAttribute('aria-controls')).toBe('system-catalog-drawer');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(h.flowMounts).toBe(1);

    act(() => advance.click());
    expect(flow.dataset.step).toBe('mapping');

    act(() => toggle.click());
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector('.onboard-layout')?.classList).toContain('catalog-open');

    const choose = container.querySelector('[data-testid="choose-system"]') as HTMLButtonElement;
    act(() => choose.click());

    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('.onboard-layout')?.classList).not.toContain('catalog-open');
    expect((container.querySelector('[data-testid="flow"]') as HTMLElement).dataset.step).toBe(
      'mapping',
    );
    expect(h.flowMounts).toBe(1);
  });
});

describe('mobile onboarding CSS contract', () => {
  it('keeps the 300px desktop catalog and defines the narrow drawer breakpoint', () => {
    expect(ONBOARD_CSS).toMatch(/grid-template-columns:\s*300px\s+minmax\(0,\s*1fr\)/);
    expect(ONBOARD_CSS).toMatch(/@media\s*\(max-width:\s*760px\)/);
    expect(ONBOARD_CSS).toMatch(/\.catalog-open\s+\.system-catalog\s*\{[^}]*translateX\(0\)/s);
    expect(ONBOARD_CSS).toContain('100dvh');
    expect(ONBOARD_CSS).toContain('env(safe-area-inset-bottom');
  });

  it('uses local horizontal scroll regions for tables and progress steps', () => {
    expect(ONBOARD_CSS).toMatch(/\.ob-table\s*\{[^}]*overflow-x:\s*auto/s);
    expect(ONBOARD_CSS).toMatch(/\.ob-progress-steps\s*\{[^}]*overflow-x:\s*auto/s);
    expect(ONBOARD_CSS).toMatch(/\.ob-progress-status\s*\{[^}]*text-overflow:\s*ellipsis/s);
    expect(ONBOARD_CSS).toMatch(/\.ob-modal\s*>\s*div:last-child[^{]*\{[^}]*flex-wrap:\s*wrap/s);
  });

  it('raises coarse-pointer onboarding controls to at least 44px', () => {
    const coarse = ONBOARD_CSS.slice(ONBOARD_CSS.indexOf('@media (pointer: coarse)'));
    expect(coarse).toContain('.ob-chat-send');
    expect(coarse).toContain('.ob-attach-btn');
    expect(coarse).toContain('.ob-cancel-btn');
    expect(coarse).toContain('.reset-btn');
    expect(coarse).toContain('.ob-scroll-bottom');
    expect(coarse).toContain('.ob-seed-chip');
    expect(coarse).toContain('.tag-remove');
    expect(coarse).toContain('min-width: 44px');
    expect(coarse).toContain('min-height: 44px');
    expect(ONBOARD_CSS).toContain(':focus-visible');
    expect(ONBOARD_CSS).toContain(':active');
  });
});
