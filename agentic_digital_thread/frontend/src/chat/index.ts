// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Public interface of the chat module (Task 2.1). Task 3.2 mounts
 * <ChatSidebar/> inside the AppShell `.main` row after the view containers —
 * it renders the resize handle, the chat sidebar, the memory sidebar, the
 * sidebar-toggle rail, and the pop-out modal, and registers the
 * window-global functions other ported modules call.
 *
 * jsx-migration W3.1: the thread itself is store-driven — <ChatThread/>
 * renders the ONE `chatThreadStore` (created in useAskStream.ts) in both the
 * sidebar and the pop-out modal. */
export { default as ChatSidebar } from './ChatSidebar';
export { ChatModal } from './ChatModal';
export { ChatThread } from './ChatThread';
export { AssistantTurn } from './AssistantTurn';
export { chatThreadStore } from './useAskStream';
export { MemoryPanel, MEMORY_COLLAPSED_KEY } from './MemoryPanel';
export { SessionMenu } from './SessionMenu';
export { PromptLibrary, SaveModal } from './PromptLibrary';
export { FollowupChips, parseAnswerBlocks } from '../shared/thread/answer-blocks';
export { mdToSafeHtml, escHtml } from './md';
export { AnswerSegments, selectFinalAnswer } from './answer-segments';
export { getSystemLabel, getToolLabel, formatToolInput } from './tool-labels';
export { splitChartBlocks, stripChartBlocks } from './charts';
export { handleArchEvent, resetArchitectureState } from './arch-bridge';
