// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect } from 'react';

import { readHashSubview } from '../../lib/hash-view';
import { QUESTIONS_SUBVIEW } from './components/RuntimeQuestions';
import { openRuntimeQuestions } from './flow-store';

/** Runtime steward questions (ADR-0059) open as a conversation IN the
 * onboarding chat whenever the hash carries the /#onboard/questions sub-view
 * (the bell's deep link) — on mount and on every later hashchange. */
export function useRuntimeQuestionsDeepLink(): void {
  useEffect(() => {
    const sync = () => {
      if (readHashSubview() === QUESTIONS_SUBVIEW) openRuntimeQuestions();
    };
    sync();
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);
}
