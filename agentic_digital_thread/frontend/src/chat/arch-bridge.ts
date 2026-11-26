// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chat → investigation-graph bridge.
 *
 * The chat stream emits handleArchEvent(type, data) into the
 * session-persistent investigation graph. In the SPA the shared surface is
 * src/lib/arch-store.ts. Coordination contract (plan §5, Tasks 2.1/2.3):
 *
 * - When the store exposes the FULL event handler (Task 2.3 extends the
 *   Wave-1 stub additively with `handleArchEvent`), the bridge delegates the
 *   raw event to it exclusively — question subtrees, qinput/finding nodes and
 *   trace entries are its job.
 * - Against the plain Wave-1 stub interface (addNodes/addEdge/reset), the
 *   bridge falls back to minimal writes so the graph still accumulates:
 *   question nodes chained with 'then' edges, plus one dedup'd node per
 *   queried system. Node ids match the citation targets chat builds
 *   (`system-<tool>`), so cite badges resolve either way (fallback
 *   `findingNodeId || nodeId`). */
import { archStore } from '../lib/arch-store';
import { getSystemLabel } from './tool-labels';

interface RichArchStore {
  handleArchEvent?: (type: string, data?: unknown) => void;
}

interface UserMsgData {
  question?: string;
}

let _questionCounter = 0;
let _currentQuestionId: string | null = null;

/** Bridge a chat stream event into the investigation graph. */
export function handleArchEvent(type: string, data: unknown): void {
  const rich = (archStore as RichArchStore).handleArchEvent;
  if (typeof rich === 'function') {
    rich.call(archStore, type, data as Record<string, unknown> | undefined);
    return;
  }

  if (type === 'user_msg') {
    _questionCounter++;
    const qid = `question-${_questionCounter}`;
    const question = String((data as UserMsgData)?.question ?? '');
    const label = question.length > 60 ? question.slice(0, 60).trimEnd() + '…' : question;
    archStore.addNodes([{ id: qid, label, type: 'question' }]);
    // Chain questions with a 'then' edge so subtrees stay separable.
    if (_currentQuestionId) {
      archStore.addEdge({ source: _currentQuestionId, target: qid, label: 'then' });
    }
    _currentQuestionId = qid;
  } else if (type === 'tool_start') {
    const tool = String((data as { tool?: string })?.tool ?? '');
    if (!tool) return;
    const nodeId = `system-${tool}`; // matches the citation nodeId
    const nodeType =
      tool === 'explore_graph' ? 'neptune' : tool === 'get_system_schema' ? 'schema' : 'system';
    archStore.addNodes([{ id: nodeId, label: getSystemLabel(tool), type: nodeType }]);
    if (_currentQuestionId) {
      archStore.addEdge({ source: _currentQuestionId, target: nodeId });
    }
  }
  // Other event types carry no minimal-store graph shape.
}

/** startNewSession semantics — clear the investigation graph. */
export function resetArchitectureState(): void {
  _questionCounter = 0;
  _currentQuestionId = null;
  archStore.reset();
}
