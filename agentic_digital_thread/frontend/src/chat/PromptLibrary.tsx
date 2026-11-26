// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Prompt Library — initPromptLibrary / togglePromptPopover /
 * loadPromptLibrary / openSaveModal / saveSaveModal.
 *
 * The popover renders inside each composer (`.chat-input`) with a sticky
 * header carrying the '+ New prompt' compose button, a search field, and the
 * Registry / Saved groups. Selecting a card inserts its text into the
 * composer the popover belongs to. Class names and element ids match the
 * ported CSS + spec exactly. */
import { useEffect, useRef, useState } from 'react';

import { ApiError, requestJson } from '../api/client';
import { useI18n } from '../lib/i18n';
import { sessionSystemsPrefill } from './session-systems';
import { usePrompts, useSavePromptMutation, useSchedulePromptMutation } from './prompt-queries';
import { showToast } from '../shared/thread/data-modal';
import { ModalSurface } from '../shared/ui/ModalSurface';
// Hover fix: scoped rules that stop the generic `.chat-input
// button` accent background from hiding the icon on hover.
import './prompt-library.css';
import type {
  OnboardStatusSystems,
  Prompt,
  SynthesizeResponse,
} from '../api/types/prompts';

const LIB_ICON = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="m16 6 4 14" />
    <path d="M12 6v14" />
    <path d="M8 8v12" />
    <path d="M4 4v16" />
  </svg>
);

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/** Onboarded systems for prompt-card dimming (window._onboardedSystems).
 * Keeps BOTH canonical ids and system types so dimming works for
 * canonical-id required_systems AND legacy type-labeled prompts. */
export async function loadOnboardedSystems(): Promise<void> {
  try {
    const data = await requestJson<OnboardStatusSystems>('/api/onboard/status');
    const systems = data.onboarded_systems || [];
    const ids = systems.map((s) => s.id).filter((v): v is string => Boolean(v));
    const types = systems.map((s) => s.system_type).filter((v): v is string => Boolean(v));
    window._onboardedSystems = [...new Set([...ids, ...types])];
  } catch (error) {
    // /api/onboard/status is integrator/Admin-gated: a 403 is the EXPECTED
    // answer for every ordinary user, not a failure worth a toast on each
    // page load. Degrade silently — prompt cards render undimmed.
    if (error instanceof ApiError && error.status === 403) {
      window._onboardedSystems = [];
      return;
    }
    showToast('Could not load prompt system availability: ' + errorDetail(error));
  }
}

export interface PromptLibraryProps {
  /** Insert a chosen prompt's text into this composer. */
  onInsert: (text: string) => void;
  /** Open the shared save modal ('' + compose:true for '+ New prompt'). */
  onCompose: () => void;
}

/** The button + popover pair rendered inside a `.chat-input` composer. */
export function PromptLibrary({ onInsert, onCompose }: PromptLibraryProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const rootRef = useRef<HTMLDivElement | null>(null);
  const promptsQuery = usePrompts(open);

  const toggle = () => {
    const opening = !open;
    setOpen(opening);
    if (opening) setQuery('');
  };

  // Escape / outside-click close (document-level, as in initPromptLibrary).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && !t.closest('.chat-input')) setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('click', onClick);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('click', onClick);
    };
  }, [open]);

  const onboarded = window._onboardedSystems || [];
  const q = query.toLowerCase();
  const matches = (p: Prompt) =>
    !q ||
    (p.title || '').toLowerCase().includes(q) ||
    (p.text || '').toLowerCase().includes(q);

  const renderGroup = (label: string, group: Prompt[]) => {
    if (!group.length) return null;
    return (
      <>
        <div className="prompt-group-label">{label}</div>
        {group.map((p, i) => (
          <div
            key={p.id || `${label}-${i}`}
            className="prompt-card"
            data-title={p.title || ''}
            data-text={p.text || ''}
            style={{ display: matches(p) ? '' : 'none' }}
            onClick={() => {
              onInsert(p.text || '');
              setOpen(false);
            }}
          >
            <div className="prompt-card-title">{p.title || ''}</div>
            <div className="prompt-card-text">{p.display_text || (p.text || '').slice(0, 80)}</div>
            <div className="prompt-card-meta">
              {(p.required_systems || []).map((s) => (
                <span
                  key={s}
                  className={`prompt-systems-tag${!onboarded.includes(s) ? ' dimmed' : ''}`}
                >
                  {s}
                </span>
              ))}
            </div>
          </div>
        ))}
      </>
    );
  };

  const prompts = promptsQuery.data;
  const loadError = promptsQuery.error && !prompts ? errorDetail(promptsQuery.error) : null;
  const registry = (prompts || []).filter((p) => p.source === 'registry');
  const user = (prompts || []).filter((p) => p.source !== 'registry');

  return (
    <div ref={rootRef} style={{ display: 'contents' }}>
      <button
        type="button"
        className="prompt-library-btn"
        title={t('chat.promptLibrary')}
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
      >
        {LIB_ICON}
      </button>
      <div className={`prompt-popover${open ? ' open' : ''}`}>
        <div className="prompt-popover-header">
          <button
            type="button"
            className="prompt-new-btn"
            aria-label="Compose and save a new prompt from scratch"
            onClick={() => {
              setOpen(false);
              onCompose();
            }}
          >
            <span aria-hidden="true">+</span> {t('chat.newPrompt')}
          </button>
        </div>
        <input
          className="prompt-search"
          placeholder="Search prompts..."
          aria-label="Search prompts"
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <div className="prompt-results">
          {loadError ? (
            <div className="prompt-error" role="alert">Failed to load prompts: {loadError}</div>
          ) : prompts === undefined ? (
            <div className="prompt-loading">Loading...</div>
          ) : (
            <>
              {renderGroup('Registry', registry)}
              {renderGroup('Saved', user)}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export interface SaveModalState {
  defaultText: string;
  compose: boolean;
}

export interface SaveModalProps {
  state: SaveModalState;
  onClose: () => void;
  /** Conversation turns for the synthesize path (post-conversation save). */
  getConversation: () => Array<{ role: string; content: string }>;
}

/** Save Prompt / New Prompt modal — port of openSaveModal / saveSaveModal.
 * compose=true starts blank and skips the conversation synthesize. */
export function SaveModal({ state, onClose, getConversation }: SaveModalProps) {
  const { defaultText, compose } = state;
  const [cron, setCron] = useState('');
  const [saveError, setSaveError] = useState('');
  const [pinned, setPinned] = useState(false);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const { mutateAsync: savePromptRequest } = useSavePromptMutation();
  const { mutateAsync: schedulePromptRequest } = useSchedulePromptMutation();

  useEffect(() => {
    if (compose) {
      promptRef.current?.focus();
      return;
    }
    // Synthesize from conversation (post-conversation save path only)
    const messages = getConversation();
    if (messages.length > 1) {
      setCron('Synthesizing...');
      void requestJson<SynthesizeResponse>('/api/prompts/synthesize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages }),
      })
        .then((data) => {
          if (data.prompt && promptRef.current) promptRef.current.value = data.prompt;
          setCron('');
        })
        .catch((error: unknown) => {
          setCron('');
          setSaveError('Could not synthesize prompt: ' + errorDetail(error));
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    const text = (document.getElementById('save-modal-prompt') as HTMLTextAreaElement).value;
    const title = (document.getElementById('save-modal-title') as HTMLInputElement).value;
    const category = (document.getElementById('save-modal-category') as HTMLSelectElement).value;
    const schedule = (
      document.getElementById('save-modal-schedule') as HTMLInputElement
    ).value.trim();
    const systemsEl = document.getElementById('save-modal-systems') as HTMLInputElement | null;
    const required_systems = systemsEl
      ? systemsEl.value.split(',').map((s) => s.trim()).filter(Boolean)
      : [];
    setSaveError('');
    try {
      const saved = await savePromptRequest({ text, title, category, pinned, required_systems });
      const promptId = saved.id ?? saved.template_id;
      if (schedule && promptId) {
        const scheduleResult = await schedulePromptRequest({ promptId, scheduleText: schedule });
        if (scheduleResult.cron) setCron(scheduleResult.cron);
      }
      onClose();
    } catch (error) {
      setSaveError('Could not save prompt: ' + errorDetail(error));
    }
  };

  return (
    <ModalSurface
      open
      onClose={onClose}
      initialFocus={promptRef}
      closeOnOverlayClick
      labelledBy="save-modal-title"
      className="save-modal-overlay"
    >
      <div className="save-modal">
        <h3 id="save-modal-title">{compose ? 'New Prompt' : 'Save Prompt'}</h3>
        <label>
          Prompt
          <textarea id="save-modal-prompt" ref={promptRef} defaultValue={defaultText || ''} />
        </label>
        <label>
          Title
          <input
            id="save-modal-title"
            type="text"
            defaultValue={compose ? '' : (defaultText || '').slice(0, 50)}
          />
        </label>
        <label>
          Category
          <select id="save-modal-category" defaultValue="general">
            <option value="general">general</option>
            <option value="cross-system">cross-system</option>
            <option value="single-system">single-system</option>
            <option value="troubleshooting">troubleshooting</option>
          </select>
        </label>
        <label>
          Required systems (optional, comma-separated)
          {/* G3: prefill from the canonical ids actually queried this session
              (gateway tool-name prefixes). Post-conversation saves only (not
              compose); the uncontrolled input remains the override — user
              edits win. */}
          <input
            id="save-modal-systems"
            type="text"
            placeholder="inferred from this conversation — edit to override"
            defaultValue={compose ? '' : sessionSystemsPrefill()}
          />
        </label>
        <div className="toggle-row">
          <label>Pin to dashboard</label>
          <div
            className={`toggle-switch${pinned ? ' active' : ''}`}
            id="save-modal-pin"
            onClick={() => setPinned((p) => !p)}
          />
        </div>
        <label>
          Schedule (optional)
          <input id="save-modal-schedule" type="text" placeholder="e.g. every weekday at 7am" />
        </label>
        <div className="schedule-resolved" id="save-modal-cron" role={saveError ? 'alert' : undefined}>
          {saveError || cron}
        </div>
        <div className="save-modal-actions">
          <button className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" onClick={() => void save()}>
            Save
          </button>
        </div>
      </div>
    </ModalSurface>
  );
}
