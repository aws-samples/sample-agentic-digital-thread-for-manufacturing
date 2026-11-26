// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboarding chat (jsx-migration W5.1) — welcome chat, the /api/onboard/chat
 * SSE stream, per-step checkpoint chat, file attachment / spec upload, the
 * inline credential form, and the persona suggestion chips. The DOM building
 * moved to <ObFlow/> + <PersonaGroups/>; this module keeps the transport and
 * dispatches store actions:
 * - refreshSuggestions() reduces /api/prompts + /api/onboard/status into the
 *   ONE suggestionsStore rendered by BOTH .persona-groups-container hosts.
 * - obStreamChat() streams into a StreamSink (a flow `stream` entry or a
 *   checkpoint chat bubble); progress / checkpoint / tool-pill /
 *   credential_request events dispatch flow-store actions.
 * - obApplyMutations() updates the checkpoint's STRUCTURED content (and the
 *   shared stepContext.data), never the DOM. */
import { ApiError, authFetch, requestJson } from '../../api/client';
import { postSse } from '../../api/sse';
import { fetchPrompts } from '../../chat/prompt-queries';
import type {
  ChatTurn,
  OnboardChatBody,
} from '../../api/types/onboard';
import { obGuardStart } from './flow';
import {
  appendChatCheckpoint,
  appendCheckpointBubble,
  appendCredentialForm,
  appendError,
  appendFile,
  appendStream,
  appendToolPill,
  appendUser,
  flowStore,
  getEntry,
  markLastToolPillDone,
  patchProgress,
  removeEntry,
  resetStream,
  setChips,
  setCredentialFormStatus,
  setProgress,
  setStreamError,
  setStreamMd,
  type FlowEntry,
  updateCheckpointBubble,
  updateEntry,
} from './flow-store';
import { buildCredentialsBody, buildSpecUploadBody, detectOpenApiSpec } from './payloads';
import { ob, type ObStepContext } from './state';
import { setConnectNewOpen } from './catalog-store';
import {
  fetchCatalog,
  fetchOnboardStatus,
  getOnboardQueryClient,
} from './onboard-queries';
import { setSuggestions, suggestionsStore } from './suggestions-store';
import { toolLabel } from './util';

export interface ChatMutation {
  action?: string;
  name?: string;
  row_count?: number;
  index?: number | string;
  concept?: string;
  confidence?: number;
  source_pattern?: string;
  target_pattern?: string;
  target_system?: string;
  [k: string]: unknown;
}

interface ChatAction {
  type?: string;
  system_id?: string;
}

// ── Welcome chat ──────────────────────────────────────────────────────────────

export const FOLLOW_UP_CHIPS: Record<string, string[]> = {
  start: [
    'What does onboarding do?',
    'Connect a new system',
    'Show available systems',
  ],
  discover: ['Why were these tables found?', 'Exclude a table', 'What happens next?'],
  map: [
    'Why this concept mapping?',
    'Change a mapping',
    'What are manufacturing concepts?',
  ],
  resolve: [
    'How do identity rules work?',
    'What if no rules are needed?',
    'What happens in provisioning?',
  ],
};

/** Welcome-chat conversation history (was the obWelcomeChat closure). */
let welcomeHistory: ChatTurn[] = [];

/** (Re)seed the welcome state — chips + fresh history. Kept under the legacy
 * name: obReset() and the OnboardView mount call it exactly like the old
 * DOMContentLoaded wiring called obWelcomeChat(). */
export function obWelcomeChat(): void {
  welcomeHistory = [];
  setChips(FOLLOW_UP_CHIPS.start);
}

/** Send a welcome-chat message (the old closure `send()`); also the target
 * of the seed chips and obResetAndConnect. */
export function welcomeSubmit(rawMsg: string): void {
  const msg = (rawMsg || '').trim();
  if (!msg) return;

  // "Connect a new system" → open the Connect New System form directly
  // instead of a chat round-trip. (Was a scroll/pulse of the catalog aside,
  // which is invisible when the catalog is the closed mobile drawer and too
  // subtle on desktop — the form modal is the real affordance.)
  if (/connect\s+a?\s*new\s+system/i.test(msg)) {
    setConnectNewOpen(true);
    return;
  }

  setChips(null); // remove chips before sending
  appendUser(msg, 'bubble');
  welcomeHistory.push({ role: 'user', content: msg });

  const streamId = appendStream();
  void obStreamChat(
    {
      step: 'start',
      system_id: '',
      data: {},
      message: msg,
      history: welcomeHistory.slice(0, -1),
    },
    flowSink(streamId),
    (text, _mutations, action) => {
      welcomeHistory.push({ role: 'assistant', content: text });
      if (action && action.type === 'start_onboard' && action.system_id) {
        // Find the system in the shared catalog query and kick off onboarding.
        fetchCatalog(getOnboardQueryClient())
          .then((systems) => {
            const sys = systems.find((system) => system.id === action.system_id);
            if (sys) obGuardStart(sys);
          });
      } else {
        setChips(FOLLOW_UP_CHIPS.start);
      }
    },
  );
}

// ── File attachment ───────────────────────────────────────────────────────────

export function obAttachFile(): void {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.json,.yaml,.yml,.pem,.env,.csv,.pdf,.txt';
  input.onchange = async (e) => {
    const file = (e.target as HTMLInputElement).files?.[0];
    if (!file) return;
    const text = await file.text();
    const sizeFmt =
      file.size < 1024 ? file.size + ' B' : (file.size / 1024).toFixed(1) + ' KB';

    appendFile(file.name, sizeFmt);

    // Detect and handle OpenAPI spec (JSON or YAML)
    const { isSpec, specContent } = detectOpenApiSpec(text);
    if (isSpec) {
      try {
        // Upload may return 204/empty on success, so use the checked raw
        // compatibility transport rather than forcing JSON parsing.
        const resp = await authFetch('/api/upload/spec', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildSpecUploadBody(file.name, specContent)),
        });
        if (!resp) throw new ApiError(401, '/api/upload/spec', 'Authentication required');
        const responseText = (await resp.text()).trim();
        let responseBody: Record<string, unknown> | null = null;
        if (responseText) {
          try {
            responseBody = JSON.parse(responseText) as Record<string, unknown>;
          } catch {
            // Empty/plain-text success is a valid upload response contract.
          }
        }
        if (!resp.ok || responseBody?.error) {
          const detail = String(
            responseBody?.detail ||
              responseBody?.error ||
              responseBody?.message ||
              responseText ||
              resp.statusText ||
              `HTTP ${resp.status}`,
          );
          throw new ApiError(resp.status, '/api/upload/spec', detail);
        }
      } catch (e) {
        const detail = e instanceof Error ? ` (${e.message})` : '';
        appendError(
          `OpenAPI upload failed for "${file.name}"${detail}. Check the file and try again.`,
        );
        return;
      }
      // Tell the agent about the spec only after the upload is confirmed.
      _obSendAttachmentMessage(
        `I uploaded an OpenAPI spec: ${file.name}. Use it to discover and onboard this REST API system.`,
      );
    } else {
      _obSendAttachmentMessage(
        `I uploaded a file: ${file.name} (${sizeFmt}). Contents:\n\`\`\`\n${text.slice(0, 2000)}\n\`\`\``,
      );
    }
  };
  input.click();
}

export function _obSendAttachmentMessage(msg: string): void {
  // Route to whichever chat surface is live: the welcome chat in welcome
  // mode, else the wizard's persistent flow input (obSendChat inline).
  const s = flowStore.get();
  if (s.mode === 'welcome') {
    welcomeSubmit(msg);
    return;
  }
  if (ob.currentStep) void obSendChat(ob.currentStep, msg, null);
}

// ── Inline credential form (credential_request SSE event) ───────────────────

/** Store the credential values typed into <CredentialForm/> — the component
 * collects its input state and calls this on "Store Securely". */
export async function obSubmitCredentials(
  entryId: number,
  systemId: string,
  creds: Record<string, string>,
): Promise<void> {
  setCredentialFormStatus(entryId, 'storing');
  try {
    const data = await requestJson<{ error?: string; note?: string }>('/api/onboard/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildCredentialsBody(systemId, creds)),
    });
    if (data.error) {
      setCredentialFormStatus(entryId, 'idle', 'Error: ' + data.error);
      return;
    }
    setCredentialFormStatus(entryId, 'stored', undefined, data.note || undefined);
  } catch (error) {
    setCredentialFormStatus(entryId, 'idle', 'Failed: ' + (error as Error).message);
  }
}

// ── Persona suggestion chips (ONE store, BOTH hosts) ─────────────────────────

/** Retry delays after a failed suggestions read that left no suggestions to
 * show. The last delay repeats until a read succeeds. */
const SUGGESTIONS_RETRY_DELAYS_MS = [5_000, 15_000, 30_000, 60_000];
let suggestionsRetryTimer: ReturnType<typeof setTimeout> | null = null;
let suggestionsRetryAttempt = 0;

function clearSuggestionsRetry(): void {
  if (suggestionsRetryTimer !== null) clearTimeout(suggestionsRetryTimer);
  suggestionsRetryTimer = null;
}

function scheduleSuggestionsRetry(): void {
  if (suggestionsRetryTimer !== null) return;
  const delays = SUGGESTIONS_RETRY_DELAYS_MS;
  const delay = delays[Math.min(suggestionsRetryAttempt, delays.length - 1)];
  suggestionsRetryAttempt += 1;
  suggestionsRetryTimer = setTimeout(() => {
    suggestionsRetryTimer = null;
    void refreshSuggestions();
  }, delay);
}

/** Test hook: cancel any pending retry and reset the backoff. */
export function _resetSuggestionsRetry(): void {
  clearSuggestionsRetry();
  suggestionsRetryAttempt = 0;
}

export async function refreshSuggestions(): Promise<void> {
  try {
    await loadSuggestions();
    _resetSuggestionsRetry();
  } catch (e) {
    console.warn('refreshSuggestions failed:', e);
    // Never leave the boot 'loading' spinner up, and never report a failed
    // read as "no systems connected". Suggestions that already loaded stay up
    // (a later refresh failing does not blank them).
    const kind = suggestionsStore.get().kind;
    if (kind !== 'loading' && kind !== 'error' && kind !== 'forbidden') return;
    // Only the onboarding-status read is role-gated this way; a 403 from any
    // other read (e.g. /api/prompts) takes the ordinary failure path below.
    if (e instanceof ApiError && e.status === 403 && e.url === '/api/onboard/status') {
      // The role can't read onboarding status (integrator or Admin only).
      // Retrying won't change that; a later refreshSuggestions() call (e.g.
      // after a role change and reload) still recovers.
      clearSuggestionsRetry();
      setSuggestions({ kind: 'forbidden' });
      return;
    }
    setSuggestions({ kind: 'error' });
    scheduleSuggestionsRetry();
  }
}

/** Most saved prompts shown as chips when they stand in for suggestions. */
export const SAVED_PROMPT_CHIP_LIMIT = 40;

function savedPromptGroup(p: { category?: string }): string {
  return (p.category || '').trim() || 'Saved prompts';
}

/** Chip label: the title, else the display text, else the start of the text
 * (a saved prompt's title is optional). */
function savedPromptLabel(p: { title?: string; display_text?: string; text?: string }): string {
  const label = (p.title || '').trim() || (p.display_text || '').trim();
  if (label) return label;
  const text = (p.text || '').trim();
  return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}

async function loadSuggestions(): Promise<void> {
  const queryClient = getOnboardQueryClient();
  const status = await fetchOnboardStatus(queryClient);
  const qs = status.suggested_questions || [];
  if (qs.length === 1 && qs[0].includes('Onboard')) {
    setSuggestions({ kind: 'single', text: qs[0] });
    return;
  }

  const prompts = (await fetchPrompts(queryClient)) as Array<{
    id?: string;
    template_id?: string | number;
    source?: string;
    text?: string;
    persona?: string;
    category?: string;
    title?: string;
    display_text?: string;
  }>;
  // Defensive dedupe-by-template_id (registry rows preferred). The server now
  // returns deduped rows with stable registry ids, but this guards the persona
  // panel against any residual/duplicate rows so groups + prompts never double.
  const _seen = new Map<string | number, (typeof prompts)[number]>();
  (Array.isArray(prompts) ? prompts : []).forEach((p) => {
    const id = p && p.template_id;
    if (id == null) return;
    const prev = _seen.get(id);
    if (!prev || (p.source === 'registry' && prev.source !== 'registry'))
      _seen.set(id, p);
  });
  const deduped = _seen.size
    ? Array.from(_seen.values())
    : Array.isArray(prompts)
      ? prompts
      : [];
  const registry = deduped.filter(
    (p) => p.source === 'registry' && p.text && p.persona,
  );

  // Without registry prompts (the demo pack is off) and without server
  // questions (it writes them only for some system mixes), fall back to the
  // deployment's own saved prompts, grouped by their Prompt Library category,
  // so the chat is not left with no suggestions at all.
  // Sorted (the store's scan order is not stable) and capped (the library is
  // shared and unbounded; the chips render in both hosts).
  const saved = registry.length || qs.length
    ? []
    : deduped
        .filter((p) => p.source !== 'registry' && typeof p.text === 'string' && p.text.trim().length > 0)
        .map((p) => ({ ...p, title: savedPromptLabel(p) }))
        .sort((a, b) =>
          savedPromptGroup(a).localeCompare(savedPromptGroup(b))
          || a.title.localeCompare(b.title)
          || String(a.template_id).localeCompare(String(b.template_id)))
        .slice(0, SAVED_PROMPT_CHIP_LIMIT);
  if (!registry.length && !saved.length) {
    setSuggestions({ kind: 'plain', questions: qs });
    return;
  }

  const grouped = registry.length ? registry : saved;
  const personas: Record<string, typeof grouped> = {};
  grouped.forEach((p) => {
    const persona = registry.length ? (p.persona as string) : savedPromptGroup(p);
    if (!personas[persona]) personas[persona] = [];
    personas[persona].push(p);
  });

  setSuggestions({
    kind: 'personas',
    groups: Object.entries(personas).map(([persona, items]) => ({
      persona,
      prompts: items.map((p) => ({
        templateId: String(p.template_id),
        title: p.title || '',
        text: p.text as string,
        display: p.display_text || p.title,
      })),
    })),
  });
}

// ── SSE stream ────────────────────────────────────────────────────────────────

/** Where the streamed agent text lands — a flow `stream` entry or a
 * checkpoint chat bubble. */
export interface StreamSink {
  /** tool_start clears the stream target (legacy targetEl.innerHTML = ''). */
  reset(): void;
  /** token accumulation (markdown source; <Md/> renders it). */
  setMd(md: string): void;
  /** error / transport failure (plain text). */
  setError(text: string): void;
}

export function flowSink(entryId: number): StreamSink {
  /* Mutable anchor (tool-pill ordering fix): tool_start appends a pill entry
   * AFTER the current stream bubble, so post-tool tokens must land in a NEW
   * bubble below the pill — resetting the original left the answer rendering
   * ABOVE the tool call that produced it. reset() drops the old bubble when
   * it is empty and re-anchors to a fresh stream entry. */
  let id = entryId;
  return {
    reset: () => {
      const entry = getEntry(id);
      if (entry && entry.kind === 'stream' && !entry.md) removeEntry(id);
      else resetStream(id);
      id = appendStream();
    },
    setMd: (md) => setStreamMd(id, md),
    setError: (text) => setStreamError(id, text),
  };
}

export function bubbleSink(cpId: number, bubbleId: number): StreamSink {
  return {
    reset: () =>
      updateCheckpointBubble(cpId, bubbleId, (b) => ({
        ...b,
        text: '',
        typing: true,
        error: undefined,
      })),
    setMd: (md) =>
      updateCheckpointBubble(cpId, bubbleId, (b) => ({ ...b, text: md, typing: false })),
    setError: (text) =>
      updateCheckpointBubble(cpId, bubbleId, (b) => ({
        ...b,
        text: '',
        typing: false,
        error: text,
      })),
  };
}

export async function obStreamChat(
  body: OnboardChatBody,
  sink: StreamSink,
  onDone?: (text: string, mutations?: ChatMutation[], action?: ChatAction) => void,
): Promise<string> {
  let streamText = '';

  try {
    // Shared POST-SSE transport (src/api/sse.ts): authFetch (401 → login
    // redirect), null-body check, and the onboard grammar via
    // singleDataPerEvent (one data line consumes each event line — the
    // `eventType = null` reset in the legacy onboard.js loop).
    await postSse(
      '/api/onboard/chat',
      body,
      (eventType, rawData) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const data = rawData as Record<string, any>;
        if (eventType === 'progress') {
          // ensureProgress + advanceStep: create the chat progress dots on
          // first use; when a progress bar already exists (the wizard's),
          // just advance it — matching the legacy duplicated-id behavior
          // where advanceStep hit the wizard's #ob-step-<i> nodes.
          if (!flowStore.get().progress) {
            setProgress({
              variant: 'chat',
              activeIdx: Number(data.step) || 0,
              statusText: '',
              allDone: false,
              cancelHidden: true,
            });
          } else {
            patchProgress({ activeIdx: Number(data.step) || 0 });
          }
        } else if (eventType === 'checkpoint') {
          appendChatCheckpoint(String(data.step), data.result || {});
        } else if (eventType === 'tool_start') {
          appendToolPill(String(data.tool_id || data.tool), toolLabel(data.tool));
          streamText = '';
          sink.reset();
        } else if (eventType === 'tool_end') {
          markLastToolPillDone();
        } else if (eventType === 'token') {
          streamText += data.text;
          sink.setMd(streamText);
        } else if (eventType === 'done') {
          if (onDone) onDone(streamText, data.mutations, data.action);
        } else if (eventType === 'credential_request') {
          appendCredentialForm(
            (data.fields as { name: string; type?: string }[]) || [],
            String(data.system_id || ''),
          );
        } else if (eventType === 'error') {
          sink.setError('Error: ' + data.text);
        }
      },
      { singleDataPerEvent: true },
    );
  } catch (e) {
    sink.setError('The request failed: ' + (e as Error).message);
  }
  return streamText;
}

/** Per-step chat: `checkpointId` targets a checkpoint's chat thread; null
 * sends inline into the main flow (the wizard's persistent input). */
export async function obSendChat(
  stepContext: ObStepContext,
  message: string,
  checkpointId: number | null,
): Promise<void> {
  let sink: StreamSink;
  if (checkpointId == null) {
    appendUser(message, 'msg');
    sink = flowSink(appendStream());
  } else {
    appendCheckpointBubble(checkpointId, { role: 'user', text: message });
    const bubbleId = appendCheckpointBubble(checkpointId, {
      role: 'agent',
      text: '',
      typing: true,
    });
    sink = bubbleSink(checkpointId, bubbleId);
  }
  stepContext.history.push({ role: 'user', content: message });

  await obStreamChat(
    {
      step: stepContext.step,
      system_id: stepContext.systemId,
      data: stepContext.data,
      message,
      history: stepContext.history.slice(0, -1),
    },
    sink,
    (text, mutations) => {
      stepContext.history.push({ role: 'assistant', content: text });
      if (mutations && mutations.length > 0 && checkpointId != null)
        obApplyMutations(mutations, checkpointId, stepContext);
    },
  );
}

/** Agent-driven checkpoint mutations — applied to the checkpoint entry's
 * STRUCTURED content and the shared stepContext.data (never the DOM). */
export function obApplyMutations(
  mutations: ChatMutation[],
  checkpointId: number,
  stepContext: ObStepContext,
): void {
  const data = stepContext.data as {
    tables?: { name: string; row_count?: number }[];
    mappings?: { concept?: string; confidence?: number }[];
    rules?: unknown[];
  };
  updateEntry(checkpointId, (entry: FlowEntry) => {
    if (entry.kind !== 'checkpoint') return entry;
    let content = entry.content;
    for (const m of mutations) {
      if (m.action === 'remove_table' && stepContext.step === 'discover') {
        if (content.kind === 'schemaTable') {
          content = {
            kind: 'schemaTable',
            tables: content.tables.filter((t) => t.name !== m.name),
          };
        }
        data.tables = (data.tables || []).filter((t) => t.name !== m.name);
      } else if (m.action === 'add_table' && stepContext.step === 'discover') {
        if (content.kind === 'schemaTable') {
          content = {
            kind: 'schemaTable',
            tables: [
              ...content.tables,
              { name: m.name as string, row_count: m.row_count || 0 },
            ],
          };
        }
        (data.tables = data.tables || []).push({
          name: m.name as string,
          row_count: m.row_count || 0,
        });
      } else if (m.action === 'update_mapping' && stepContext.step === 'map') {
        const idx = parseInt(String(m.index), 10);
        if (content.kind === 'mappingTable' && !isNaN(idx) && content.rows[idx]) {
          content = {
            // Spread keeps the additive vocab_fit slice (C5) across chat-driven
            // row edits (the flow-store updateMappingRows idiom).
            ...content,
            rows: content.rows.map((r, i) => {
              if (i !== idx) return r;
              const tags = [...r.tags];
              const primaryIdx = tags.findIndex((t) => !t.skip);
              const next = {
                concept: m.concept || (primaryIdx >= 0 ? tags[primaryIdx].concept : ''),
                confidence:
                  m.confidence !== undefined
                    ? m.confidence <= 1
                      ? m.confidence
                      : m.confidence / 100
                    : primaryIdx >= 0
                      ? tags[primaryIdx].confidence
                      : 0.5,
              };
              if (primaryIdx >= 0) tags[primaryIdx] = { ...tags[primaryIdx], ...next };
              else if (next.concept) tags.push(next);
              return { ...r, review: false, tags };
            }),
          };
        }
        if (data.mappings?.[idx]) {
          if (m.concept) data.mappings[idx].concept = m.concept;
          if (m.confidence !== undefined) data.mappings[idx].confidence = m.confidence;
        }
      } else if (m.action === 'remove_rule' && stepContext.step === 'resolve') {
        const idx = parseInt(String(m.index), 10);
        if (content.kind === 'rules' && !isNaN(idx)) {
          content = {
            ...content,
            rules: content.rules.filter((_, i) => i !== idx),
          };
        }
        if (data.rules) data.rules.splice(idx, 1);
      } else if (m.action === 'add_rule' && stepContext.step === 'resolve') {
        if (content.kind === 'rules') {
          content = { ...content, rules: [...content.rules, m] };
        }
        (data.rules = data.rules || []).push(m);
      }
    }
    return { ...entry, content };
  });
}
