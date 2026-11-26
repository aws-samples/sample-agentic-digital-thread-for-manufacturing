// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* OnboardView reads useWorkshopMode() once and hands it to the dialogs, so
 * the "also forget the connection registration" checkbox follows the same
 * workshop gate as Reset All. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  mode: 'unknown' as string,
  modalProps: [] as unknown[],
}));

vi.mock('../features/workshop/useWorkshopStory', () => ({ useWorkshopMode: () => h.mode }));
vi.mock('../views/onboard/catalog', () => ({ obLoadCatalog: vi.fn() }));
vi.mock('../views/onboard/chat', () => ({ obWelcomeChat: vi.fn() }));
vi.mock('../views/onboard/flow', () => ({ obResetAll: vi.fn() }));
vi.mock('../views/onboard/globals', () => ({ registerOnboardGlobals: vi.fn() }));
vi.mock('../views/onboard/state', () => ({ loadConcepts: vi.fn() }));
vi.mock('../views/onboard/runtime-questions-link', () => ({ useRuntimeQuestionsDeepLink: vi.fn() }));
vi.mock('../views/onboard/components/CatalogList', () => ({ CatalogList: () => null }));
vi.mock('../views/onboard/components/ObFlow', () => ({ ObFlow: () => null }));
vi.mock('../views/onboard/components/ObModals', () => ({
  ObModals: (props: unknown) => {
    h.modalProps.push(props);
    return null;
  },
}));

import OnboardView from '../views/onboard/OnboardView';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  h.modalProps = [];
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('OnboardView passes the workshop mode to the dialogs', () => {
  for (const mode of ['on', 'off', 'unknown']) {
    it(`passes '${mode}' to ObModals`, () => {
      h.mode = mode;
      act(() => root.render(<OnboardView />));
      expect(h.modalProps.at(-1)).toMatchObject({ workshopMode: mode });
    });
  }
});
