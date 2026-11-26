// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chat pop-out modal — the #chat-modal-overlay markup + the modal functions
 * (openChatModal /
 * closeChatModal / sendModalMessage / handleModalKeyPress).
 *
 * jsx-migration W3.1: the body renders the SAME chatThreadStore as the
 * sidebar via <ChatThread/> (the old innerHTML clone-mirroring is gone).
 * The persona empty-state toggles off the store's turns/history lengths;
 * the composer mirrors to the main input and continues the SAME session. */
import { useEffect, useRef, useSyncExternalStore } from 'react';

import { useI18n } from '../lib/i18n';
import { ModalSurface } from '../shared/ui/ModalSurface';

import { ChatThread } from './ChatThread';
import { InvestigationTrackButton } from './InvestigationTrackButton';
import { PromptLibrary } from './PromptLibrary';
import { PersonaGroups } from '../views/onboard/components/PersonaGroups';
import type { ClickLikeEvent } from './thread-clicks';
import { chatThreadStore } from './useAskStream';

const WORD_SVG = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <line x1="16" y1="13" x2="8" y2="13" />
    <line x1="16" y1="17" x2="8" y2="17" />
    <polyline points="10 9 9 9 8 9" />
  </svg>
);

/** Auto-grow a composer textarea — port of autoGrowInput. */
export function autoGrowInput(el: HTMLTextAreaElement): void {
  // Reviewed (useless-assignment): intentional autosize idiom — resetting to
  // 'auto' first forces reflow so scrollHeight reports the shrunk-to-fit size.
  // nosemgrep
  el.style.height = 'auto';
  el.style.height = el.scrollHeight + 'px';
}

export interface ChatModalProps {
  open: boolean;
  streaming: boolean;
  onClose: () => void;
  /** '+ New' — startNewSession() (the store clears; this body follows). */
  onNewSession: () => void;
  /** Send from the modal composer (mirrors to the main input). */
  onSendModal: () => void;
  /** Stop the in-flight stream (button in Stop state; the caller confirms
   * first when the composer holds text). */
  onStop: () => void;
  /** A send from this composer was held because a turn is still streaming:
   * show the muted "still answering" line under the composer. */
  composerHeld?: boolean;
  onThreadClick: (e: ClickLikeEvent) => void;
  onCompose: () => void;
  /** Modal composer ref (ChatSidebar's sendModalMessage reads it). */
  modalInputRef: React.RefObject<HTMLTextAreaElement>;
  /** The header's investigation button navigated: clear the chat off the
   * page (passed through to InvestigationTrackButton's onNavigate). */
  onTrackInvestigation?: () => void;
}

export function ChatModal({
  open,
  streaming,
  onClose,
  onNewSession,
  onSendModal,
  onStop,
  composerHeld = false,
  onThreadClick,
  onCompose,
  modalInputRef,
  onTrackInvestigation,
}: ChatModalProps) {
  const { t } = useI18n();
  const messagesRef = useRef<HTMLDivElement | null>(null);

  // Persona empty-state toggles off the thread contents (the old
  // _renderModalBody checked src.innerHTML.trim().length).
  const hasThread = useSyncExternalStore(
    chatThreadStore.subscribe,
    () => chatThreadStore.getState().turns.length + chatThreadStore.getState().history.length > 0,
    () => chatThreadStore.getState().turns.length + chatThreadStore.getState().history.length > 0,
  );

  // Empty thread → persona suggestions (the old renderModalBody called
  // refreshSuggestions() whenever the empty state showed).
  useEffect(() => {
    if (open && !hasThread) window.refreshSuggestions?.();
  }, [open, hasThread]);

  return (
    <ModalSurface
      open={open}
      onClose={onClose}
      initialFocus={modalInputRef}
      closeOnOverlayClick
      keepMounted
      labelledBy="chat-modal-title"
      className={`chat-modal-overlay${open ? ' open' : ''}`}
      id="chat-modal-overlay"
    >
      <div className="chat-modal">
        <div className="chat-modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span id="chat-modal-title" style={{ fontWeight: 500 }}>{t('chat.orchestrator')}</span>
            <InvestigationTrackButton streaming={streaming} onNavigate={onTrackInvestigation} />
          </div>
          <div className="chat-modal-actions" style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            {/* Task 1H: the header #modal-loading-indicator is deleted — same
                duplication as the sidebar's #loading-indicator against the
                same <ChatThread/> stream-position .thinking-inline. */}
            <button
              className="chat-modal-action"
              onClick={onNewSession}
              title="Start a new investigation (new session)"
            >
              {t('chat.new')}
            </button>
            <button
              className="chat-modal-action"
              onClick={() => window.saveChatModalAsWord?.()}
              title="Save this conversation as a Word document"
            >
              {WORD_SVG}
              Word
            </button>
            <button className="chat-modal-close" onClick={onClose}>
              {t('common.closeX')}
            </button>
          </div>
        </div>
        <div
          className="messages"
          id="modal-messages"
          ref={messagesRef}
          style={{ display: hasThread ? '' : 'none' }}
          onClick={onThreadClick}
        >
          {/* Mount only while open — the sidebar's <ChatThread/> keeps
              streaming; this one attaches to the SAME store on open.
              .chat-content-column centers bubbles/prose in one reading
              column (dashboard.css) so the 72ch measure cap (thread.css)
              no longer leaves a dead full-width bubble. */}
          {open && (
            <div className="chat-content-column">
              <ChatThread store={chatThreadStore} containerRef={messagesRef} />
            </div>
          )}
        </div>
        {/* Empty-state persona panel — mirrors the side panel's #persona-panel;
            the shared refreshSuggestions() builder renders persona rows into
            every .persona-groups-container, including this one. */}
        <div
          className="persona-panel"
          id="modal-persona-panel"
          style={{ display: hasThread ? 'none' : '' }}
          onClick={onThreadClick}
        >
          <div
            id="modal-persona-groups-container"
            className="persona-groups-container"
          >
            <PersonaGroups />
          </div>
        </div>
        {/* position:relative anchors the prompt-library popover (the original
            set it imperatively on every .chat-input; the CSS has no position). */}
        <div className="chat-input" style={{ position: 'relative' }}>
          <textarea
            id="modal-question-input"
            ref={modalInputRef}
            rows={2}
            placeholder={t('chat.inputPlaceholder')}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                onSendModal();
              }
            }}
            onInput={(e) => autoGrowInput(e.target as HTMLTextAreaElement)}
          />
          <PromptLibrary
            onInsert={(text) => {
              const input = modalInputRef.current;
              if (input) {
                input.value = text;
                input.focus();
              }
            }}
            onCompose={onCompose}
          />
          <button
            id="modal-send-btn"
            className={streaming ? 'streaming' : ''}
            onClick={streaming ? onStop : onSendModal}
          >
            {streaming ? t('chat.stop') : t('chat.send')}
          </button>
        </div>
        {composerHeld && (
          <div className="chat-composer-hint" role="status">
            {t('chat.stillAnswering')}
          </div>
        )}
      </div>
    </ModalSurface>
  );
}
