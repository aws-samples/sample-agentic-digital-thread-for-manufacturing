// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  resetToWelcome,
  showReconcileBanner,
  startWizard,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetToWelcome();
});

describe('sticky stepper render order', () => {
  it('renders the progress stepper ABOVE any reconcile banner in the wizard flow', () => {
    // A post-completion reconcile check surfaces "N fields may match new
    // concepts" for OTHER systems. The stepper is `position: sticky; top: 0`;
    // if the banner were rendered first it would push the stepper down and
    // become the new sticky edge, which reads as the stepper coming unstuck.
    showReconcileBanner([
      { field_id: 'mes-mil.site', concept_id: 'site', role: 'identifier', confidence: 0.9 },
    ]);
    act(() => root.render(<ObFlow />));

    const stepper = container.querySelector('.ob-progress');
    const banner = container.querySelector('.ob-reconcile-banner');
    expect(stepper).not.toBeNull();
    expect(banner).not.toBeNull();

    // DOCUMENT_POSITION_FOLLOWING (4): the banner comes after the stepper.
    const relation = stepper!.compareDocumentPosition(banner!);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(relation & Node.DOCUMENT_POSITION_PRECEDING).toBeFalsy();
  });

  it('explains what the reconcile panel is and that it is optional', () => {
    showReconcileBanner([
      { field_id: 'mes-mil.site', concept_id: 'site', role: 'identifier', confidence: 0.9 },
    ]);
    act(() => root.render(<ObFlow />));

    const help = container.querySelector('.ob-reconcile-banner .ob-reconcile-help');
    expect(help?.textContent).toContain('Optional');
    expect(help?.textContent).toContain('Accept tags the field');
    expect(container.querySelector('.ob-reconcile-close')?.getAttribute('aria-label')).toBe('Close');
  });
});
