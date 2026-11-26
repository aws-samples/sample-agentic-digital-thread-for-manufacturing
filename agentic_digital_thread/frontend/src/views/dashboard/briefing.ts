// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* AI Operations Briefing — requestOpsBriefing / _startBriefingLoop
 * (DOM writes go through dashboard-store dispatches; AiBriefing.tsx renders
 * the card). Persistent
 * panel: first run 4s after start, auto-refresh every 5 minutes,
 * single-flight guarded, and role-keyed content cache (see below) so
 * remounts/role switches inside the TTL never re-trigger a generation.
 *
 * Transport (unchanged from Task 2.2): streams /api/ask through the shared
 * Wave-1 `postSse` reader under a DEDICATED `briefing-` Conversation Session
 * with `write_memory: false`. Briefings are synthetic turns: riding the
 * user's shared chat session id polluted conversation memory ("what was my
 * previous question?" answered with the briefing prompt — live-found on a
 * customer deployment 2026-08-09), and `briefing` is an AUTOMATED_SESSION_PREFIXES entry
 * (memory_client) precisely so these turns stay out of memory and the
 * session picker. The answer is rebuilt from the token stream with the
 * chat's AnswerSegments (collectBriefingText), then the text cleanup
 * pipeline (lib.ts pure helpers) runs on it. The markdown now renders through
 * the shared <Md/> component (same marked+DOMPurify semantics the old
 * mdInline/mdBlock helpers used). */
import { postSse } from '../../api/sse';
import { AnswerSegments } from '../../chat/answer-segments';
import { wrapResponseLang } from '../../lib/i18n-lang';
import {
  briefingBulletLines,
  briefingFromFirstCard,
  briefingNoteLines,
  cleanBriefingText,
  extractBriefingRaw,
  parseBriefingBullet,
} from './lib';
import type { BriefingBulletVm } from './store';
import { dashboardStore, patchBriefing } from './store';

let _briefingTimer: ReturnType<typeof setInterval> | null = null;
/** 4s warm-up boot timer (startBriefingLoop) — tracked so unmount within the
 * boot window doesn't fire a briefing POST + store writes after cleanup. */
let _briefingBootTimer: ReturnType<typeof setTimeout> | null = null;
/** 30s failure-retry watchdog (requestOpsBriefing catch) — tracked likewise. */
let _briefingRetryTimer: ReturnType<typeof setTimeout> | null = null;
let _briefingInFlight = false;
/** AbortController of the briefing whose /api/ask stream is OPEN RIGHT NOW.
 * Timers alone were not enough: a briefing already streaming kept the
 * orchestrator turn (and the server-side producer thread) alive after the
 * dashboard went away, and its failure path armed a fresh 30s retry that
 * re-entered the whole generation — a measured amplifier in the 2026-08-24
 * single-task CPU saturation. `stopBriefingLoop` now aborts it. */
let _briefingAbort: AbortController | null = null;

/** True for the AbortError a cancellation produces — EXPECTED, never an error
 * state and never a reason to retry (an aborted briefing was deliberately
 * abandoned; retrying it would defeat the cancellation). */
function isAbort(err: unknown): boolean {
  return !!err && (err as { name?: string }).name === 'AbortError';
}

export const BRIEFING_PROMPT =
  'Give me a 5-bullet operations briefing for the manufacturing floor. Each bullet ' +
  'must be one sentence. Cover only the most critical items: line status, quality, ' +
  'maintenance, and order risk. No tables, no headers, no follow-up questions. ' +
  'Put an ordinary caveat about a figure inside the bullet it qualifies; after the ' +
  'last bullet, write nothing except a footnote your instructions require.';

/** Dedicated synthetic Conversation Session for briefing turns — the
 * `briefing` prefix is an AUTOMATED_SESSION_PREFIXES entry server-side, so
 * these turns never reach conversation memory or the session picker. One id
 * per page load: successive refreshes coherently share it, and it can never
 * collide with the user's chat session. */
export const BRIEFING_SESSION_ID =
  'briefing-' + Math.random().toString(36).slice(2, 10) + '-' + Date.now().toString(36);

// ── Role-keyed briefing cache (loading-state audit) ───────────────────────────
// Briefings are EXPENSIVE LLM generations, so re-activating the dashboard (or
// switching a briefing role back and forth) must serve the last generated
// content instantly instead of re-streaming /api/ask. Module-level map
// role → {content, fetchedAt}, same house pattern as systems.ts. Today the
// app has exactly ONE role (the fixed operations prompt — no role selector
// exists yet); the cache is keyed so a future role picker plugs in.
//
// TTL is deliberately UNDER the 5-min auto-refresh interval: an interval tick
// always sees an expired entry (auto-refresh preserved 1:1), while remounts /
// tab revisits inside the TTL never regenerate. An explicit user refresh
// (the card's button → force:true) always bypasses the cache.

export const DEFAULT_BRIEFING_ROLE = 'operations';
export const BRIEFING_CACHE_TTL_MS = 4.5 * 60_000;

interface CachedBriefing {
  phase: 'bullets' | 'block';
  bullets: BriefingBulletVm[];
  notes: string[];
  blockMd: string;
  timestamp: string;
  fetchedAt: number;
}

const _briefingCache = new Map<string, CachedBriefing>();

/** Test hook — reset the role-keyed cache + single-flight between specs. */
export function _resetBriefingCache(): void {
  _briefingCache.clear();
  _briefingInFlight = false;
  _briefingAbort = null;
}

/** The role's generation prompt. The default role IS the classic fixed
 * prompt (byte-identical); any other role gets a tailoring suffix. */
export function briefingPromptForRole(role: string): string {
  if (role === DEFAULT_BRIEFING_ROLE) return BRIEFING_PROMPT;
  return BRIEFING_PROMPT + ' Tailor the briefing for the ' + role + ' role.';
}

/** True while the briefing has no rendered result (the old "no content yet /
 * Analyzing placeholder" DOM check). */
function hasNoContent(): boolean {
  const phase = dashboardStore.get().briefing.phase;
  return phase === 'init' || phase === 'placeholder' || phase === 'unavailable';
}

/** Publish the active-only gate state for the existing briefing card. */
export function setBriefingLifecycle(active: boolean): void {
  patchBriefing({ lifecycle: active ? 'active' : 'inactive' });
}


export interface BriefingRequestOpts {
  /** Briefing role (cache key + prompt tailoring); defaults to the single
   * operations role the dashboard uses today. */
  role?: string;
  /** Explicit user refresh — bypasses the role cache (always regenerates). */
  force?: boolean;
}

/** Restore the role's cached briefing (if any) → may we SKIP regeneration?
 *
 * Extracted so the coordinator stays a lifecycle shell: the cache decision is
 * "render what we have, and say whether it is still fresh enough to stand in
 * for a generation". An explicit user refresh (`force`) always regenerates. */
function restoreCachedBriefing(role: string, force: boolean): boolean {
  const cached = _briefingCache.get(role);
  if (!cached) return false;
  patchBriefing({
    phase: cached.phase,
    bullets: cached.bullets,
    notes: cached.notes,
    blockMd: cached.blockMd,
    timestamp: cached.timestamp,
    analyzing: false,
  });
  return !force && Date.now() - cached.fetchedAt < BRIEFING_CACHE_TTL_MS;
}

/** The concurrency gates: an overlapping briefing (init call, 5-min
 * interval, retry) and a user query already streaming — a second concurrent
 * AgentCore session is exactly the amplification this file must not cause.
 * A HIDDEN tab is gated too (unless the user forced a refresh): measured on a
 * workshop account, one abandoned dashboard tab kept a full orchestrator turn
 * running every 5 to 10 minutes for hours, and a room of attendees would
 * multiply that against their own questions. The tab regenerates when it is
 * next looked at (`visibilitychange` below). */
function briefingBlocked(force: boolean): boolean {
  if (_briefingInFlight) {
    console.log('[briefing] Skipped — a briefing is already in flight');
    return true;
  }
  // chat.js mirrors its isStreaming flag on window.
  if ((window as unknown as { isStreaming?: boolean }).isStreaming) {
    console.log('[briefing] Skipped — user query in progress');
    return true;
  }
  if (!force && typeof document !== 'undefined' && document.visibilityState === 'hidden') {
    console.log('[briefing] Skipped — tab is hidden');
    return true;
  }
  return false;
}

/** Stream ONE briefing generation → the cleaned answer text.
 *
 * The token stream is rebuilt into the answer the way the chat does it
 * (AnswerSegments: a tool call ends a text run, the answer is chosen from the
 * runs after the last call that reads data, a write-back call does not end
 * it). A normal turn's done event carries no text, so the old join of every
 * token let a draft bullet or a planning line written before a data call
 * render beside the final bullets. A done event that does carry the text
 * still wins, then the briefing cleanup runs on the selected answer.
 *
 * `signal` is the caller's per-request AbortController signal, so a
 * cancellation lands in the transport and surfaces as an AbortError. */
async function collectBriefingText(role: string, signal: AbortSignal): Promise<string> {
  const segments = new AnswerSegments();
  let doneText = '';
  await postSse(
    '/api/ask',
    {
      question: wrapResponseLang(briefingPromptForRole(role)),
      // Synthetic turn: dedicated automated session + explicit memory
      // suppression (AskRequest.write_memory honored only to DISABLE).
      session_id: BRIEFING_SESSION_ID,
      write_memory: false,
    },
    (eventType, data) => {
      const d = data as { text?: string; tool?: unknown };
      if (eventType === 'tool_start') {
        segments.onToolBoundary(typeof d?.tool === 'string' ? d.tool : undefined);
      }
      if (eventType === 'token' && d && d.text) segments.pushToken(d.text);
      if (eventType === 'done' && d && d.text && d.text.length > 10) doneText = d.text;
    },
    { signal },
  );
  return cleanBriefingText(extractBriefingRaw(doneText, segments.finalize()));
}

function briefingTimestamp(): string {
  const now = new Date();
  return 'Updated ' + now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Commit a successful generation to the store AND the role cache (so a revisit
 * inside the TTL re-serves it instantly). Too-short output commits nothing. */
function commitBriefingText(role: string, clean: string, timestamp: string): void {
  if (clean.length <= 10) return;
  const bulletLines = briefingBulletLines(clean);
  const content: Omit<CachedBriefing, 'fetchedAt' | 'timestamp'> =
    bulletLines.length >= 3
      ? {
          // Styled bullet cards — inline markdown rendered via <Md inline/>.
          phase: 'bullets',
          bullets: bulletLines.map((line) => parseBriefingBullet(line)),
          // A caveat written after the bullets is not a bullet, and is kept.
          notes: briefingNoteLines(clean),
          blockMd: '',
        }
      : { phase: 'block', bullets: [], notes: [], blockMd: briefingFromFirstCard(clean) };
  _briefingCache.set(role, { ...content, timestamp, fetchedAt: Date.now() });
  patchBriefing({
    phase: content.phase,
    bullets: content.bullets,
    notes: content.notes,
    blockMd: content.blockMd,
  });
}

/** Arm the single 30s failure retry (cold start / IncompleteRead). Unforced: if
 * the role's cache is still fresh the retry just re-serves it. NEVER armed for
 * a cancellation — that is the caller's decision, made before calling here.
 *
 * SINGLE-OWNER slot: a second failure inside the 30s window must REPLACE the
 * pending retry, never accrete a second timer. An untracked timer survives
 * `stopBriefingLoop` and can re-enter an expensive generation on an inactive
 * dashboard — exactly the amplification this file exists to close. */
function scheduleBriefingRetry(role: string): void {
  if (_briefingRetryTimer) clearTimeout(_briefingRetryTimer);
  const timer = setTimeout(async () => {
    // Fire ONLY while still the slot's owner: a superseded (or stopped and
    // cleared) timer that still runs must not start a briefing.
    if (_briefingRetryTimer !== timer) return;
    _briefingRetryTimer = null;
    try {
      await requestOpsBriefing({ role });
    } catch {
      if (hasNoContent()) {
        patchBriefing({ phase: 'unavailable' });
      }
    }
  }, 30000);
  _briefingRetryTimer = timer;
}

/** Coordinator: owns the per-request AbortController and the try/catch/finally
 * lifecycle; every step above is a narrow helper. */
export async function requestOpsBriefing(opts?: BriefingRequestOpts): Promise<void> {
  const role = (opts && opts.role) || DEFAULT_BRIEFING_ROLE;
  const force = !!(opts && opts.force);
  // Serve the cached briefing INSTANTLY (role switch / tab revisit / remount);
  // inside the TTL that IS the answer — only force or expiry reaches the LLM.
  if (restoreCachedBriefing(role, force)) return;
  if (briefingBlocked(force)) return;

  _briefingInFlight = true;
  const controller = new AbortController();
  _briefingAbort = controller;
  // Spin the refresh button + "analyzing..." note; placeholder if no content.
  patchBriefing({
    analyzing: true,
    ...(hasNoContent() ? { phase: 'placeholder' as const } : {}),
  });

  try {
    const clean = await collectBriefingText(role, controller.signal);
    const timestamp = briefingTimestamp();
    commitBriefingText(role, clean, timestamp);
    // Hide analyzing indicator + update timestamp.
    patchBriefing({ analyzing: false, timestamp });
  } catch (err) {
    patchBriefing({ analyzing: false });
    // Cancellation (stopBriefingLoop / dashboard deactivation) is EXPECTED:
    // leave the rendered phase alone and arm NO retry.
    if (isAbort(err) || controller.signal.aborted) return;
    scheduleBriefingRetry(role);
  } finally {
    _briefingInFlight = false;
    if (_briefingAbort === controller) _briefingAbort = null;
  }
}

let _briefingVisibilityHandler: (() => void) | null = null;

/** Delay first call by 4s to let AgentCore warm up, then every 5 minutes.
 * Interval ticks on a hidden tab are skipped (`briefingBlocked`); the tab
 * catches up once when it becomes visible again, so a briefing is only ever
 * generated for someone who will read it. */
export function startBriefingLoop(): void {
  if (_briefingBootTimer) clearTimeout(_briefingBootTimer);
  _briefingBootTimer = setTimeout(() => {
    _briefingBootTimer = null;
    void requestOpsBriefing();
  }, 4000);
  if (_briefingTimer) clearInterval(_briefingTimer);
  _briefingTimer = setInterval(requestOpsBriefing, 5 * 60 * 1000);
  if (typeof document !== 'undefined' && !_briefingVisibilityHandler) {
    _briefingVisibilityHandler = () => {
      if (document.visibilityState === 'visible') void requestOpsBriefing();
    };
    document.addEventListener('visibilitychange', _briefingVisibilityHandler);
  }
}

/** Unmount / deactivation cleanup — the original page never unloaded the loop;
 * React must. Clears the 5-min interval AND the 4s boot / 30s retry timeouts,
 * and ABORTS an in-flight briefing stream so an abandoned dashboard stops
 * paying for (and holding open) an orchestrator turn. The abort surfaces as an
 * AbortError, which `requestOpsBriefing` treats as cancellation — no retry. */
export function stopBriefingLoop(): void {
  if (_briefingTimer) {
    clearInterval(_briefingTimer);
    _briefingTimer = null;
  }
  if (_briefingBootTimer) {
    clearTimeout(_briefingBootTimer);
    _briefingBootTimer = null;
  }
  if (_briefingRetryTimer) {
    clearTimeout(_briefingRetryTimer);
    _briefingRetryTimer = null;
  }
  if (_briefingAbort) {
    _briefingAbort.abort();
    _briefingAbort = null;
  }
  if (_briefingVisibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', _briefingVisibilityHandler);
    _briefingVisibilityHandler = null;
  }
}
