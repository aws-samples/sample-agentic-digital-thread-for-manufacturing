// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Window globals the chat modules read/write. These form a cross-module
 * contract: other views and the e2e suite call chat-owned globals
 * (askQuestion, sendMessage, mdToSafeHtml, ...) and chat guards optional
 * globals owned by other modules (switchView, cy, showToast, ...). */

declare global {
  interface Window {
    // ── chat-owned state ────────────────────────────────────────────────
    _nextUserDisplayText?: string | null;
    _lastDisplayText?: string | null;
    _lastFindingId?: string | null;
    _lastIntelligenceSummary?: import('../api/types/chat').IntelligenceSummary | null;
    _lastAgentAnalysis?: import('../api/types/chat').AgentAnalysis | null;
    _onboardedSystems?: string[];

    // ── chat-owned functions registered for other ported modules ───────
    askQuestion?: (question: string) => void;
    sendMessage?: () => void;
    stopStreaming?: () => void;
    startNewSession?: () => void;
    openChatModal?: () => void;
    closeChatModal?: () => void;
    mdToSafeHtml?: (text: unknown) => string;
    parseAnswerBlocks?: (text: string) => {
      answer: string;
      followups: string[];
      actions: string[];
    };
    getSystemLabel?: (toolName: string) => string;
    getToolLabel?: (toolName: string) => string;
    refreshMemory?: (force?: boolean) => void;
    rehydrateSession?: (sid: string) => void;
    toggleChatSidebar?: () => void;
    toggleMemorySidebar?: () => void;

    // ── optional globals owned by other modules (guarded calls) ────
    switchView?: (view: string) => void;
    switchArchTab?: (tab: string) => void;
    // handleArchEvent/resetArchitectureState are DIRECT chat/arch-bridge
    // imports, not window globals: nothing in the SPA registers them on
    // window, so guarded window calls would silently no-op.
    /** Investigation-graph cytoscape instance — published by inv-graph.ts
     * initArchitecture, cleared by destroyInvGraph (`window.cy` is a
     * published contract). Consumed by
     * thread-clicks focusArchNode + ChatSidebar resize. */
    cy?: {
      resize: () => void;
      getElementById: (id: string) => {
        length: number;
        closedNeighborhood: () => unknown;
        select: () => void;
        data: (key: string) => string | undefined;
      };
      fit: (eles: unknown, padding: number) => void;
    } | null;
    openDataModal?: (
      title?: string,
      detail?: string,
      extras?: import('../shared/thread/data-modal').DataModalExtras,
    ) => void;
    closeDataModal?: () => void;
    showToast?: (msg: string) => void;
    refreshSuggestions?: () => void;
    saveChatModalAsWord?: () => void;
  }
}

export {};
