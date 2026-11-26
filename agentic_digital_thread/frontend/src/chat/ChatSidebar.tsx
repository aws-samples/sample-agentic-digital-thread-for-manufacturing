// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ChatSidebar/> — the mountable chat surface (Task 2.1). Ports, 1:1:
 * - the #resize-handle / #chat-sidebar / #memory-sidebar / .sidebar-toggles
 *   markup block (all contractual ids/classes kept);
 * - chat: sendMessage streaming turn (via useAskStream), Send↔Stop,
 *   startNewSession (askConfirm-if-streaming, session + arch-store reset),
 *   toggleChatSidebar, drag resize, persona/trace visibility, the pop-out
 *   modal, prompt library + save modal, bookmarks;
 * - memory: the memory panel + session rehydration (shared with the menu);
 * - session menu: the keyboard '+ New' menu.
 *
 * jsx-migration W3.1: the thread renders from the ONE chatThreadStore via
 * <ChatThread/> — in #messages here AND in the pop-out modal (#modal-messages)
 * — so the old _renderModalBody/syncToModal innerHTML-mirroring hack is gone.
 * Resets go through store actions (threadStore.clear(),
 * resetModalConversation()) instead of DOM/innerHTML writes.
 *
 * Task 3.2 mounts this inside the AppShell `.main` row, after the view
 * containers: <ChatSidebar/>. The component also registers the window-global
 * functions other ported modules call (askQuestion, sendMessage, ...). */
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import { adoptSessionId, resetSessionId } from '../lib/session';
import { readHashView } from '../lib/hash-view';
import { demoActiveStore } from '../lib/demo-active';
import { useI18n } from '../lib/i18n';
import { askConfirm } from '../shared/ui/confirm-store';
import { push as pushToast } from '../shared/ui/toast-store';
import { resetModalConversation, showToast } from '../shared/thread/data-modal';
import { resetArchitectureState } from './arch-bridge';
import { answerAlreadySurfaced } from './auto-open';
import { ChatModal, autoGrowInput } from './ChatModal';
import { ChatThread } from './ChatThread';
import { ExpandAnswerButton } from './ExpandAnswerButton';
import { InvestigationTrackButton } from './InvestigationTrackButton';
import { parseAnswerBlocks } from '../shared/thread/answer-blocks';
import { mdToSafeHtml } from './md';
import { MemoryPanel, MEMORY_COLLAPSED_KEY } from './MemoryPanel';
import { PromptLibrary, SaveModal, loadOnboardedSystems, type SaveModalState } from './PromptLibrary';
import { useSavePromptMutation } from './prompt-queries';
import { PersonaGroups } from '../views/onboard/components/PersonaGroups';
import { rehydrateSession as runRehydrateSession } from './rehydrate';
import { attachResizeDrag } from './resize-drag';
import { SessionMenu } from './SessionMenu';
import { createThreadClickHandler, type ClickLikeEvent } from './thread-clicks';
import { getSystemLabel, getToolLabel } from './tool-labels';
import { collapsedDrawerAttrs } from './drawer-attrs';
import { chatThreadStore, useAskStream, type AskOptions } from './useAskStream';
import { requestArchTabFocus } from '../views/architecture/arch-tab-bridge';

const CHAT_TOGGLE_SVG = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
  </svg>
);
const MEMORY_TOGGLE_SVG = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a2 2 0 01-2 2h-4a2 2 0 01-2-2v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z" />
    <line x1="10" y1="21" x2="14" y2="21" />
  </svg>
);

const MOBILE_DRAWER_MEDIA = '(max-width: 768px)';

type DrawerState = {
  chatCollapsed: boolean;
  memoryCollapsed: boolean;
};

function matchesMobileDrawer(): boolean {
  return typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia(MOBILE_DRAWER_MEDIA).matches;
}

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

export default function ChatSidebar() {
  // Chrome i18n (adoption pass): chat chrome renders t(key); re-renders on setLang.
  const { t } = useI18n();
  // ── refs to the imperative DOM surfaces ────────────────────────────────
  const sidebarRef = useRef<HTMLDivElement | null>(null);
  const handleRef = useRef<HTMLDivElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const modalInputRef = useRef<HTMLTextAreaElement | null>(null);

  // ── UI state ───────────────────────────────────────────────────────────
  // Trace window shown during active conversations, persona panel otherwise.
  const [threadVisible, setThreadVisible] = useState(false);
  const [drawers, setDrawers] = useState<DrawerState>({
    chatCollapsed: false,
    // Memory starts collapsed (index.html ships class "memory-sidebar collapsed").
    memoryCollapsed: true,
  });
  const { chatCollapsed: collapsed, memoryCollapsed } = drawers;
  const [mobileLayout, setMobileLayout] = useState(matchesMobileDrawer);
  const drawersRef = useRef(drawers);
  drawersRef.current = drawers;
  const desktopDrawersRef = useRef<DrawerState>(drawers);
  const mobileLayoutRef = useRef(false);
  const [memoryRefreshToken, setMemoryRefreshToken] = useState(0);
  const [modalOpen, setModalOpen] = useState(false);
  const modalOpenRef = useRef(modalOpen);
  modalOpenRef.current = modalOpen;
  const [saveModal, setSaveModal] = useState<SaveModalState | null>(null);
  const { mutateAsync: savePromptRequest } = useSavePromptMutation();

  const refreshMemory = useCallback(() => setMemoryRefreshToken((t) => t + 1), []);

  const openChatModal = useCallback(() => setModalOpen(true), []);
  const closeChatModal = useCallback(() => setModalOpen(false), []);

  /** End-of-turn notify (useAskStream `done`, substantive answer). The
   * takeover is RETIRED — this never opens the modal. When the settled
   * answer is already surfaced in-app (modal open, dock expanded — where the
   * expand affordance sits under the thread — or the architecture view; see
   * chat/auto-open.ts) nothing fires. Otherwise a clickable "response ready"
   * toast opens the modal on demand (explicit action). During a DEMO run
   * the toast is suppressed too — the demo narrates in the sidebar beside
   * the live investigation graph. */
  const notifyAnswerReady = useCallback(() => {
    if (demoActiveStore.get()) return;
    const surfaced = answerAlreadySurfaced({
      modalOpen: modalOpenRef.current,
      dockOpen: !drawersRef.current.chatCollapsed,
      view: readHashView(),
    });
    if (surfaced) return;
    pushToast('info', 'Response ready. Choose to view it.', {
      durationMs: 10000,
      onActivate: () => setModalOpen(true),
    });
  }, []);

  // ── streaming turn (Send↔Stop) ─────────────────────────────────────────
  const { isStreaming, isStreamingRef, sendMessage: runSend, stopStreaming } = useAskStream({
    getPlant: () =>
      (document.getElementById('plant-select') as HTMLSelectElement | null)?.value || 'all',
    showThread: () => setThreadVisible(true),
    onAnswerReady: notifyAnswerReady,
    onTurnSettled: refreshMemory,
  });

  // A send attempted while a turn streams is HELD: the composer keeps the
  // text (never cleared, never overwritten) and the surface it came from
  // shows one muted line until the answer finishes. No queueing.
  const [composerHeld, setComposerHeld] = useState<'sidebar' | 'modal' | null>(null);
  useEffect(() => {
    if (!isStreaming) setComposerHeld(null);
  }, [isStreaming]);

  // Leaving the page (refresh, close, a same-tab link) cancels a running
  // answer, so ask the browser to confirm — only while a turn streams.
  useEffect(() => {
    if (!isStreaming) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Older Chromium shows the prompt only when returnValue is set.
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [isStreaming]);

  /** Port of sendMessage(): read + clear the composer, then run the turn.
   * `options` (the "Ask again" retry flag) arrives only via askQuestion. */
  const sendComposer = useCallback(
    (options?: AskOptions) => {
      const input = inputRef.current;
      if (!input) return;
      const question = input.value.trim();
      if (!question) return;
      if (isStreamingRef.current) {
        setComposerHeld('sidebar');
        return;
      }
      input.value = '';
      input.style.height = 'auto';
      void runSend(question, options);
    },
    [runSend, isStreamingRef],
  );
  // Zero-argument on purpose: it is a button onClick and a window global.
  const sendMessage = useCallback(() => sendComposer(), [sendComposer]);

  /** Port of sendModalMessage(): mirror to the main input and send. */
  const sendModalMessage = useCallback(() => {
    const minput = modalInputRef.current;
    if (!minput) return;
    const q = minput.value.trim();
    if (!q) return;
    // Mid-turn the pop-out keeps its text (it used to vanish into the hidden
    // sidebar composer and look eaten).
    if (isStreamingRef.current) {
      setComposerHeld('modal');
      return;
    }
    minput.value = '';
    minput.style.height = 'auto';
    if (inputRef.current) inputRef.current.value = q;
    sendMessage();
  }, [sendMessage, isStreamingRef]);

  /** Port of askQuestion(): fill the composer, collapse suggestions, send.
   * A chip clicked mid-turn is held as an ordinary question (its retry flag,
   * if any, is dropped: the latest turn is then a different one). */
  const askQuestion = useCallback(
    (question: string, options?: AskOptions) => {
      const details = document.querySelector('.suggestions');
      if (details) details.removeAttribute('open');
      if (isStreamingRef.current) {
        // A chip or suggestion clicked mid-turn never overwrites a typed
        // draft. An EMPTY composer (on the surface in use) takes the chip's
        // question, so it is there to send when the answer finishes.
        const surface = modalOpenRef.current ? 'modal' : 'sidebar';
        const target = surface === 'modal' ? modalInputRef.current : inputRef.current;
        if (target && !target.value.trim()) target.value = question;
        setComposerHeld(surface);
        return;
      }
      if (inputRef.current) inputRef.current.value = question;
      sendComposer(options);
    },
    [sendComposer, isStreamingRef],
  );

  /** Stop from a composer button. Text in that composer means the attendee
   * typed the next question and clicked where Send was: confirm first (the
   * guard settles BEFORE any abort). An empty composer keeps the one-click
   * Stop. */
  const stopFromComposer = useCallback(
    async (composer: HTMLTextAreaElement | null) => {
      if (composer?.value.trim()) {
        const stop = await askConfirm({
          key: 'chat:stop',
          title: t('dialog.stopTitle'),
          body: t('dialog.stopBody'),
          confirmLabel: t('dialog.stopConfirm'),
          cancelLabel: t('common.cancel'),
          tone: 'danger',
        });
        // Declined, or the answer finished while the dialog was open.
        if (!stop || !isStreamingRef.current) return;
      }
      stopStreaming();
    },
    [stopStreaming, isStreamingRef, t],
  );

  // ── '+ New' (startNewSession) ──────────────────────────────────────────
  // Not streaming: resets synchronously (no await on that path — the demo
  // loop reads the cleared thread right after calling it). Streaming: asks
  // first, then cancels the query and resets in the same order as before.
  const startNewSession = useCallback(async () => {
    // Confirm cancellation of an in-flight query before resetting.
    if (isStreamingRef.current) {
      const cancelAndStart = await askConfirm({
        key: 'chat:new-session',
        title: t('dialog.newSessionTitle'),
        body: t('dialog.newSessionBody'),
        confirmLabel: t('dialog.newSessionConfirm'),
        cancelLabel: t('common.cancel'),
        tone: 'danger',
      });
      if (!cancelAndStart) return;
      // The answer may have finished while the dialog was open.
      if (isStreamingRef.current) stopStreaming();
    }
    resetSessionId();
    resetModalConversation();
    resetArchitectureState();
    // Store clear BEFORE the async AbortError catch runs, so the canceled
    // marker is skipped exactly like the old contentDiv.isConnected guard.
    chatThreadStore.clear();
    setThreadVisible(false);
    window.refreshSuggestions?.();
    refreshMemory();
  }, [isStreamingRef, stopStreaming, refreshMemory, t]);

  // ── session rehydration ─────────────────────────────────────────────────
  // Extracted to rehydrate.ts (wipe-guard semantics: an empty events fetch
  // never overwrites a non-empty live thread, and the session id is adopted
  // only when history is actually applied — see the module header).
  const rehydrateSession = useCallback(
    (sid: string) =>
      runRehydrateSession(sid, {
        store: chatThreadStore,
        adoptSession: adoptSessionId,
        resetModal: resetModalConversation,
        showThread: () => setThreadVisible(true),
        refreshMemory,
        onError: (error) => showToast('Could not load session: ' + errorDetail(error)),
      }),
    [refreshMemory],
  );

  // ── responsive drawer state ────────────────────────────────────────────
  // Desktop may intentionally show chat + memory side-by-side. Entering the
  // mobile breakpoint snapshots that arrangement, enforces one open drawer,
  // and leaving the breakpoint restores the exact desktop arrangement.
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(MOBILE_DRAWER_MEDIA);

    const applyLayout = (isMobile: boolean) => {
      const wasMobile = mobileLayoutRef.current;
      if (isMobile && !wasMobile) {
        desktopDrawersRef.current = drawersRef.current;
        setDrawers((current) =>
          !current.chatCollapsed && !current.memoryCollapsed
            ? { ...current, memoryCollapsed: true }
            : current,
        );
      } else if (!isMobile && wasMobile) {
        setDrawers(desktopDrawersRef.current);
      }
      mobileLayoutRef.current = isMobile;
      setMobileLayout(isMobile);
    };

    applyLayout(media.matches);
    const onChange = (event: MediaQueryListEvent) => applyLayout(event.matches);
    media.addEventListener?.('change', onChange);
    return () => media.removeEventListener?.('change', onChange);
  }, []);

  // ── sidebar collapse + memory collapse ─────────────────────────────────
  // The post-transition graph resize is deferred, so its handle is tracked and
  // cleared on unmount: an un-cleared timer fires against a torn-down window
  // (leaked work after unmount; in jsdom it surfaces as "window is not
  // defined"). One pending resize at a time — a rapid re-toggle supersedes it.
  const resizeTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(resizeTimerRef.current), []);

  const toggleChatSidebar = useCallback(() => {
    setDrawers((current) => {
      const opening = current.chatCollapsed;
      return {
        chatCollapsed: !current.chatCollapsed,
        memoryCollapsed: mobileLayout && opening ? true : current.memoryCollapsed,
      };
    });
    // Wait for the CSS transition to finish before resizing the graph.
    clearTimeout(resizeTimerRef.current);
    resizeTimerRef.current = setTimeout(() => window.cy?.resize(), 250);
  }, [mobileLayout]);

  const toggleMemorySidebar = useCallback(() => {
    setDrawers((current) => {
      const opening = current.memoryCollapsed;
      const nextMemoryCollapsed = !current.memoryCollapsed;
      localStorage.setItem(MEMORY_COLLAPSED_KEY, nextMemoryCollapsed ? '1' : '0');
      return {
        chatCollapsed: mobileLayout && opening ? true : current.chatCollapsed,
        memoryCollapsed: nextMemoryCollapsed,
      };
    });
  }, [mobileLayout]);

  /** The investigation button just switched the page underneath to the
   * investigation graph: clear whatever chat surface covers it. The pop-out
   * modal covers the page at every size; on the mobile layout every drawer
   * is full-screen, so both close (the response-ready toast can open the
   * modal over the memory drawer; the rail reopens either). The button that
   * held focus is now hidden, so focus moves to the Investigation tab. The
   * docked desktop chat stays open and keeps focus. The turn keeps
   * streaming: it lives in the module-level chatThreadStore. */
  const revealInvestigation = useCallback(() => {
    const hidesFocusedControl = modalOpenRef.current || mobileLayout;
    setModalOpen(false);
    if (mobileLayout) {
      setDrawers((current) => ({ ...current, chatCollapsed: true, memoryCollapsed: true }));
    }
    if (hidesFocusedControl) requestArchTabFocus('investigation');
  }, [mobileLayout]);

  // ── save-as-prompt (bookmark) ──────────────────────────────────────────
  const savePrompt = useCallback((text: string, btn: HTMLButtonElement) => {
    const userMsgs = document.querySelectorAll('#messages .message.user');
    if (userMsgs.length <= 1) {
      btn.classList.add('saving');
      void savePromptRequest({ text, title: text.slice(0, 50) })
        .then(() => {
          btn.classList.add('saved');
        })
        .catch((error: unknown) => {
          btn.classList.remove('saved');
          showToast('Could not save prompt: ' + errorDetail(error));
        })
        .finally(() => {
          setTimeout(() => btn.classList.remove('saving'), 400);
        });
    } else {
      setSaveModal({ defaultText: text, compose: false });
    }
  }, [savePromptRequest]);

  const openCompose = useCallback(() => setSaveModal({ defaultText: '', compose: true }), []);

  // ── delegated thread clicks (thread + modal — same handler) ────────────
  const threadClickRef = useRef<(e: ClickLikeEvent) => void>(() => {});
  useEffect(() => {
    threadClickRef.current = createThreadClickHandler({ askQuestion, savePrompt });
  }, [askQuestion, savePrompt]);
  const onThreadClick = useCallback((e: ClickLikeEvent) => threadClickRef.current(e), []);

  // ── drag resize (hardened port in resize-drag.ts) ──────────────────────
  // Pointer events instead of mouse events: a capture-phase document
  // mousedown listener (demo-exit) can swallow mousedown before it reaches
  // the handle, and chart iframes eat document mousemove mid-drag; see the
  // Attached whenever the desktop layout is active. Crossing into the mobile
  // drawer breakpoint detaches pointer resizing; returning to desktop restores
  // the same handler without remounting the sidebar.
  useEffect(() => {
    if (mobileLayout) return;
    const handle = handleRef.current;
    const sidebar = sidebarRef.current;
    if (!handle || !sidebar) return;
    return attachResizeDrag(handle, sidebar, {
      onRelease: () => window.cy?.resize(),
    });
  }, [mobileLayout]);

  // ── boot: onboarded systems + window globals ────
  // (The persona default chip is the suggestions store's initial state —
  // <PersonaGroups/> renders both hosts off the one store; jsx-migration W5.1.)
  useEffect(() => {
    void loadOnboardedSystems();
  }, []);

  // Register the window-global contract other ported modules call
  // (dashboard persona chips → askQuestion, onboard → sendMessage, thread →
  // mdToSafeHtml/parseAnswerBlocks, architecture → getSystemLabel, ...).
  useEffect(() => {
    window.askQuestion = askQuestion;
    window.sendMessage = sendMessage;
    window.stopStreaming = stopStreaming;
    window.startNewSession = startNewSession;
    window.openChatModal = openChatModal;
    window.closeChatModal = closeChatModal;
    window.mdToSafeHtml = mdToSafeHtml;
    window.parseAnswerBlocks = parseAnswerBlocks;
    window.getSystemLabel = getSystemLabel;
    window.getToolLabel = getToolLabel;
    window.refreshMemory = refreshMemory;
    window.rehydrateSession = rehydrateSession;
    window.toggleChatSidebar = toggleChatSidebar;
    window.toggleMemorySidebar = toggleMemorySidebar;
  }, [
    askQuestion,
    sendMessage,
    stopStreaming,
    startNewSession,
    openChatModal,
    closeChatModal,
    refreshMemory,
    rehydrateSession,
    toggleChatSidebar,
    toggleMemorySidebar,
  ]);

  const focusQuestionInput = useCallback(() => inputRef.current?.focus(), []);

  return (
    <>
      <div
        className="resize-handle"
        id="resize-handle"
        ref={handleRef}
        style={{ display: collapsed ? 'none' : '' }}
      />
      <div
        className={`chat-sidebar${collapsed ? ' collapsed' : ''}${mobileLayout && !collapsed ? ' mobile-drawer-open' : ''}`}
        id="chat-sidebar"
        ref={sidebarRef}
        data-drawer-state={collapsed ? 'closed' : 'open'}
        // A collapsed drawer is zero-width and transparent but its controls
        // stayed in the Tab order (and kept focus when it closed): inert
        // takes them out until it reopens. The pop-out modal is a sibling,
        // so it is unaffected.
        {...collapsedDrawerAttrs(collapsed)}
      >
        <div className="chat-header">
          <div className="chat-header-top">
            <div className="chat-header-title">
              <button
                className="chat-popout-btn"
                onClick={openChatModal}
                aria-label={t('chat.popOut')}
                title={t('chat.popOut')}
              >
                ⤢
              </button>
              <span>{t('chat.orchestrator')}</span>
              <InvestigationTrackButton streaming={isStreaming} onNavigate={revealInvestigation} />
            </div>
            <div className="chat-header-actions">
              <SessionMenu
                onNewSession={() => void startNewSession()}
                onRehydrate={rehydrateSession}
                focusQuestionInput={focusQuestionInput}
              />
            </div>
          </div>
          {/* Task 1H: the header #loading-indicator ("thinking" under the
              Orchestrator title) is deleted — it duplicated the stream-
              position .thinking-inline rendered by <ChatThread/>, showing
              TWO thinking affordances per streaming turn. */}
        </div>

        {/* Persona Investigation Panel */}
        <div
          className="persona-panel"
          id="persona-panel"
          style={{
            overflowY: 'auto',
            padding: '0.5rem 0.75rem',
            display: threadVisible ? 'none' : '',
          }}
          onClick={onThreadClick}
        >
          <div
            id="persona-groups-container"
            className="persona-groups-container"
          >
            <PersonaGroups />
          </div>
        </div>

        {/* Trace window — shown during active queries, hidden otherwise */}
        <div
          id="trace-window"
          style={{
            display: threadVisible ? 'flex' : 'none',
            flex: 1,
            overflowY: 'auto',
            padding: '0.5rem 0.75rem',
            borderTop: '1px solid var(--border)',
          }}
        >
          <div
            id="messages"
            className="messages"
            style={{ padding: 0 }}
            ref={messagesRef}
            onClick={onThreadClick}
          >
            <ChatThread store={chatThreadStore} containerRef={messagesRef} />
            {/* Explicit full-screen affordance — bottom of the thread scroll
                flow (never shifts the composer); opens the SAME modal as the
                header pop-out (one store, one session). */}
            <ExpandAnswerButton onOpen={openChatModal} />
          </div>
        </div>

        {/* position:relative is CONTRACTUAL for the prompt-library popover:
            the original initPromptLibrary() set it imperatively on every
            .chat-input — the CSS itself carries no position. */}
        <div className="chat-input" style={{ position: 'relative' }}>
          <textarea
            id="question-input"
            ref={inputRef}
            rows={2}
            placeholder={t('chat.inputPlaceholder')}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            onInput={(e) => autoGrowInput(e.target as HTMLTextAreaElement)}
          />
          <PromptLibrary
            onInsert={(text) => {
              const input = inputRef.current;
              if (input) {
                input.value = text;
                input.focus();
              }
            }}
            onCompose={openCompose}
          />
          <button
            id="send-btn"
            className={isStreaming ? 'streaming' : ''}
            onClick={isStreaming ? () => void stopFromComposer(inputRef.current) : sendMessage}
          >
            {isStreaming ? t('chat.stop') : t('chat.send')}
          </button>
        </div>
        {composerHeld === 'sidebar' && (
          <div className="chat-composer-hint" role="status">
            {t('chat.stillAnswering')}
          </div>
        )}
      </div>

      {/* Memory Sidebar */}
      <MemoryPanel
        refreshToken={memoryRefreshToken}
        onRehydrate={rehydrateSession}
        collapsed={memoryCollapsed}
        onToggleCollapsed={toggleMemorySidebar}
      />

      <div
        className="sidebar-toggles"
        id="sidebar-toggles"
        data-mobile-active-drawer={
          mobileLayout
            ? !collapsed
              ? 'chat'
              : !memoryCollapsed
                ? 'memory'
                : 'none'
            : 'desktop'
        }
      >
        <button
          type="button"
          className="sidebar-toggle-btn"
          id="chat-collapse-btn"
          onClick={toggleChatSidebar}
          title={collapsed ? 'Open chat' : 'Close chat'}
          aria-label={collapsed ? 'Open chat drawer' : 'Close chat drawer'}
          aria-controls="chat-sidebar"
          aria-expanded={!collapsed}
        >
          {CHAT_TOGGLE_SVG}
        </button>
        <button
          type="button"
          className="sidebar-toggle-btn"
          id="memory-toggle-btn"
          onClick={toggleMemorySidebar}
          title={memoryCollapsed ? 'Open memory' : 'Close memory'}
          aria-label={memoryCollapsed ? 'Open memory drawer' : 'Close memory drawer'}
          aria-controls="memory-sidebar"
          aria-expanded={!memoryCollapsed}
        >
          {MEMORY_TOGGLE_SVG}
        </button>
      </div>

      {/* Chat Popout Modal — renders the SAME chatThreadStore */}
      <ChatModal
        open={modalOpen}
        streaming={isStreaming}
        onClose={closeChatModal}
        onNewSession={() => void startNewSession()}
        onSendModal={sendModalMessage}
        onStop={() => void stopFromComposer(modalInputRef.current)}
        composerHeld={composerHeld === 'modal'}
        onThreadClick={onThreadClick}
        onCompose={openCompose}
        modalInputRef={modalInputRef}
        onTrackInvestigation={revealInvestigation}
      />

      {/* Save Prompt / New Prompt modal */}
      {saveModal && (
        <SaveModal
          state={saveModal}
          onClose={() => setSaveModal(null)}
          getConversation={() =>
            [...document.querySelectorAll('#messages .message')].map((m) => ({
              role: m.classList.contains('user') ? 'user' : 'assistant',
              content: (m.textContent || '').trim(),
            }))
          }
        />
      )}
    </>
  );
}
