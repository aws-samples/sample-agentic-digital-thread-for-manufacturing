// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <AgentTestLine/> — the agent test line on the chat flow's verify card: it
 * starts (once per card) the agent test for the verified system, shows it
 * running while it polls, then shows the result (agent-test.ts). */
import { useEffect, useState } from 'react';

import { watchAgentTest, type AgentTestView } from '../agent-test';

export function AgentTestLine({ runKey, systemId }: { runKey: string; systemId: string }) {
  const [view, setView] = useState<AgentTestView | null>(null);
  useEffect(() => watchAgentTest(runKey, systemId, setView), [runKey, systemId]);
  if (!view) return null;
  const outcome = view.phase === 'done' ? view.outcome : 'running';
  return (
    <p className={'ob-agent-test ' + outcome} role="status" aria-live="polite">
      {view.text}
    </p>
  );
}
