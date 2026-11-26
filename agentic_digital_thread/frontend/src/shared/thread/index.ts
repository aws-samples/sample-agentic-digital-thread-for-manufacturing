// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Public surface of the shared thread module.
 * Hosts (index architecture area, admin tab-thread) mount <ThreadPanel/>;
 * the app shell mounts <DataModal/> once, and ported dashboard/chat modules
 * call the imperative modal API (openDataModal etc.) unchanged. */

export { ThreadPanel } from './ThreadPanel';
export type { ThreadPanelProps } from './ThreadPanel';
export {
  askModalFollowup,
  closeDataModal,
  DataModal,
  openDataModal,
  resetModalConversation,
  saveChatModalAsWord,
  saveModalAsWord,
  sendModalFollowup,
  showToast,
} from './data-modal';
export { FollowupChips, parseAnswerBlocks } from './answer-blocks';
export type { ParsedAnswer } from './answer-blocks';
export { entityTypeKey, onboardedSystems, sysClass, sysIcon } from './html';
export type { SysClass, ThreadSystem } from './html';
