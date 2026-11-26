// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Learning a rule from the thread explorer is a steward action: POST
 * /api/thread/learn answers 403 for anyone else. When the client already
 * knows the signed-in user is not a steward, the Create Rule button is
 * replaced by a note; while the role is unknown the button stays and a 403
 * shows its message on the button. */

// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { canLearnRules, ResolutionSteps } from '../shared/thread/panels';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

const steps = [
  {
    rule: 'Brute-force scan',
    transform: 't',
    check: 'c',
    status: 'fuzzy' as const,
    proposal: { source_pattern: 'B-{id}', target_pattern: 'LOT{id}', confidence: 0.85 },
  },
];

const user = (is_admin: boolean) => ({ user_id: 'u', email: 'u@example.com', username: 'u', is_admin });

describe('canLearnRules', () => {
  it('is false only for a signed-in user known not to be a steward', () => {
    expect(canLearnRules({ user: user(false), loading: false })).toBe(false);
    expect(canLearnRules({ user: user(true), loading: false })).toBe(true);
    // Role not known yet (still loading, or no answer): keep the action; the
    // server decides and a 403 shows its message.
    expect(canLearnRules({ user: null, loading: true })).toBe(true);
    expect(canLearnRules({ user: null, loading: false })).toBe(true);
    expect(canLearnRules({ user: user(false), loading: true })).toBe(true);
  });
});

describe('<ResolutionSteps canLearn/>', () => {
  it('a non-steward sees a note instead of Create Rule, and can still dismiss', () => {
    const onLearn = vi.fn();
    const onDismiss = vi.fn();
    act(() => {
      root.render(<ResolutionSteps resolution={steps} canLearn={false} onLearn={onLearn} onDismiss={onDismiss} />);
    });
    const learn = container.querySelector('.learn-proposal') as HTMLElement;
    expect(learn).not.toBeNull();
    expect(learn.querySelector('.learn-btn-create')).toBeNull();
    expect(learn.querySelector('.learn-steward-note')!.textContent).toBe(
      'Only a steward (admin) can save this pattern as a rule.',
    );
    act(() => (learn.querySelector('.learn-btn-dismiss') as HTMLButtonElement).click());
    expect(onDismiss).toHaveBeenCalledWith(0);
    expect(onLearn).not.toHaveBeenCalled();
  });

  it('a steward (and the default) keeps the Create Rule button', () => {
    const noop = () => {};
    act(() => {
      root.render(<ResolutionSteps resolution={steps} canLearn onLearn={noop} onDismiss={noop} />);
    });
    expect(container.querySelector('.learn-btn-create')!.textContent).toBe('Create Rule');
    expect(container.querySelector('.learn-steward-note')).toBeNull();
    act(() => {
      root.render(<ResolutionSteps resolution={steps} onLearn={noop} onDismiss={noop} />);
    });
    expect(container.querySelector('.learn-btn-create')).not.toBeNull();
  });

  it('a 403 from the server shows its message on the button', () => {
    const noop = () => {};
    act(() => {
      root.render(
        <ResolutionSteps
          resolution={steps}
          stepStates={{ 0: { failed: 'Steward (admin) role required' } }}
          onLearn={noop}
          onDismiss={noop}
        />,
      );
    });
    expect(container.querySelector('.learn-btn-create')!.textContent).toBe(
      'Failed: Steward (admin) role required',
    );
  });
});
