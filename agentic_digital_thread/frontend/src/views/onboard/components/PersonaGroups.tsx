// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <PersonaGroups/> (jsx-migration W5.1) — the persona suggestion chips,
 * rendered from the ONE suggestionsStore into BOTH hosts (chat sidebar
 * #persona-groups-container + modal #modal-persona-groups-container).
 * Replaces the refreshSuggestions() innerHTML builder; the click semantics
 * are unchanged: registry prompts stuff #question-input with the FULL text,
 * stash the short display text on window._nextUserDisplayText, and call
 * sendMessage(); plain questions call askQuestion(q). */
import { useI18n } from '../../../lib/i18n';
import { useStore } from '../../../lib/store';
import { LoadingState } from '../../../shared/ui/AsyncStates';
import { suggestionsStore, type PersonaPromptVm } from '../suggestions-store';

function sendPrompt(p: PersonaPromptVm): void {
  const w = window as unknown as {
    _nextUserDisplayText?: string;
    sendMessage?: () => void;
  };
  const input = document.getElementById('question-input') as HTMLInputElement | null;
  if (input) {
    input.value = p.text;
    // Show the short display text as the user message, send the full text.
    w._nextUserDisplayText = p.display;
  }
  if (typeof w.sendMessage === 'function') w.sendMessage();
}

export function PersonaGroups() {
  const { t } = useI18n();
  const state = useStore(suggestionsStore);

  if (state.kind === 'loading') {
    return <LoadingState label="Loading suggestions" compact className="persona-loading" />;
  }
  if (state.kind === 'default') {
    return (
      <div className="suggestion-chip" style={{ opacity: 0.5, cursor: 'default' }}>
        {t('chat.noSystems')}
      </div>
    );
  }
  if (state.kind === 'forbidden') {
    return (
      <div
        className="suggestion-chip"
        role="status"
        style={{ opacity: 0.5, cursor: 'default' }}
      >
        {t('chat.suggestionsNoAccess')}
      </div>
    );
  }
  if (state.kind === 'error') {
    return (
      <div
        className="suggestion-chip"
        role="status"
        style={{ opacity: 0.5, cursor: 'default' }}
      >
        {t('chat.suggestionsUnavailable')}
      </div>
    );
  }
  if (state.kind === 'single') {
    return (
      <div className="suggestion-chip" style={{ opacity: 0.5, cursor: 'default' }}>
        {state.text}
      </div>
    );
  }
  if (state.kind === 'plain') {
    return (
      <>
        {state.questions.map((q) => (
          <div
            key={q}
            className="suggestion-chip"
            onClick={() => {
              (window as unknown as { askQuestion?: (q: string) => void }).askQuestion?.(q);
            }}
          >
            {q}
          </div>
        ))}
      </>
    );
  }
  return (
    <>
      {state.groups.map((g) => (
        <details key={g.persona} className="persona-details">
          <summary className="persona-summary">
            {g.persona}
          </summary>
          <div className="persona-queries">
            {g.prompts.map((p) => (
              <div
                key={p.templateId}
                className="suggestion-chip"
                data-prompt-id={p.templateId}
                onClick={() => sendPrompt(p)}
              >
                {p.title}
              </div>
            ))}
          </div>
        </details>
      ))}
    </>
  );
}
