// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Demo mode + demo gate.
 *
 * The demo gate (/api/demo/status) shows/hides demo-only UI; fail-safe hides
 * on unknown.
 *
 * Demo mode is a KIOSK DRIVER over the NORMAL chat flow (uniformity refactor
 * — the former parallel pipeline is gone): each registry prompt is sent
 * through the SAME path a persona-chip click takes (window.sendMessage on the
 * composer, short display text via window._nextUserDisplayText), so the
 * answer streams into the chat sidebar via chatThreadStore with full
 * markdown/charts/citations, the architecture investigation graph animates
 * from the SAME SSE events, and sessions/memory behave exactly like
 * interactive chat. window.startNewSession() between prompts gives each
 * query a fresh session + graph (the old per-query reset).
 *
 * What remains demo-specific is only kiosk chrome:
 * - the view/tab setup (Architecture → Investigation, chat dock expanded);
 * - exit gestures (Space/Escape/background mousedown) routed through the
 *   "Leave demo mode?" pause-confirm (DemoModal renders the dialog);
 * - pacing: wait for the turn to settle (chatThreadStore), then a
 *   length-aware, pause-aware reading dwell before the next prompt
 *   (replaces the removed splitIntoPages/page-sweep overlay);
 * - demoActiveStore (lib/demo-active) suppresses the end-of-turn chat-modal
 *   auto-open so nothing covers the graph mid-show. */
import { chatThreadStore } from '../../chat/useAskStream';
import { fetchPrompts, getPromptQueryClient } from '../../chat/prompt-queries';
import { requestJson } from '../../api/client';
import type { RegistryPrompt } from '../../api/types/dashboard';
import { setDemoActive } from '../../lib/demo-active';
import { requestArchTab } from '../../views/architecture/arch-tab-bridge';
import { demoConfirmHide, demoConfirmShow } from './store';

interface CrossViewGlobals {
  switchView?: (v: string) => void;
  handleDemoBtn?: (e: Event) => void;
  sendMessage?: () => void;
  startNewSession?: () => void;
  stopStreaming?: () => void;
  toggleChatSidebar?: () => void;
  _nextUserDisplayText?: string | null;
}

const w = () => window as unknown as CrossViewGlobals;

// ── Demo runtime gate ─────────────────────────────────────────────────────────

/** Each entry carries the display value to restore when the demo is enabled. */
const DEMO_GATED_ELEMENTS = [
  { id: 'demo-btn', shown: '' },
];

/** Show/hide demo-only UI from /api/demo/status. Fail-safe: any error or
 * non-ok response hides the elements (treat unknown as off). */
export async function initDemoGate(): Promise<void> {
  const apply = (enabled: boolean) => {
    for (const { id, shown } of DEMO_GATED_ELEMENTS) {
      const el = document.getElementById(id);
      if (el) el.style.display = enabled ? shown : 'none';
    }
  };
  try {
    const st = await requestJson<{ enabled?: boolean } | null>('/api/demo/status');
    apply(!!(st && st.enabled === true));
  } catch {
    apply(false);
  }
}

// ── Demo mode ─────────────────────────────────────────────────────────────────

let _demoActive = false;
let _demoLastStop = 0;
/** Exit-gesture cooldown: gestures are ignored for the first second so the
 * click that started the demo can't immediately open the leave-confirm. */
let _demoExitReady = false;
/** True while the "Leave demo mode?" confirm is up — the run (turn wait /
 * reading dwell / next prompt) blocks until Stay or Leave. */
let _demoPaused = false;

/** A turn that produces no settle within this window aborts the run. */
const TURN_TIMEOUT_MS = 5 * 60_000;
/** Reading dwell after a settled answer: length-aware, clamped. */
const DWELL_MIN_MS = 6_000;
const DWELL_MAX_MS = 30_000;
const DWELL_MS_PER_CHAR = 12;

/** Named, module-scoped capture handler (NOT a per-start closure): a second
 * addEventListener with the same function+capture is a native no-op, so
 * registration is single-instance by construction, and removal never misses.
 * A background gesture during an active demo PAUSES the run and opens the
 * leave-confirm instead of instantly tearing the demo down. */
function demoExitOnEvent(e: Event): void {
  if (!_demoActive || !_demoExitReady) return;
  const target = e.target as HTMLElement | null;
  if (target && target.id === 'demo-btn') return;
  if (e.type === 'keydown') {
    const code = (e as KeyboardEvent).code;
    if (code !== 'Space' && code !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    // Confirm already up: Escape/Space = Stay (dismiss and resume).
    if (_demoPaused) demoConfirmStay();
    else demoPauseForConfirm();
    return;
  }
  if (e.type === 'mousedown') {
    e.stopPropagation();
    // While the confirm is up, mousedowns are inert — the dialog's buttons
    // act on the 'click' event, which this capture listener never touches.
    if (!_demoPaused) demoPauseForConfirm();
  }
}

/** Idempotent single-instance registration (removes any existing first). */
function registerExitListeners(): void {
  removeExitListeners();
  document.addEventListener('keydown', demoExitOnEvent, true); // capture phase
  document.addEventListener('mousedown', demoExitOnEvent, true); // capture phase
}

/** Idempotent removal — safe to call any number of times. */
function removeExitListeners(): void {
  document.removeEventListener('keydown', demoExitOnEvent, true);
  document.removeEventListener('mousedown', demoExitOnEvent, true);
}

function demoPauseForConfirm(): void {
  if (_demoPaused) return;
  _demoPaused = true;
  demoConfirmShow();
}

/** Stay button / Escape while confirm is up: dismiss and resume the demo
 * where it left off (nothing was torn down). */
export function demoConfirmStay(): void {
  _demoPaused = false;
  demoConfirmHide();
}

/** Leave button: dismiss and take the existing stop path (navigates to the
 * dashboard). */
export function demoConfirmLeave(): void {
  demoConfirmHide();
  stopDemoMode();
}

export function handleDemoBtn(e: Event): void {
  e.preventDefault();
  e.stopPropagation();
  if (Date.now() - _demoLastStop < 1500) return;
  if (_demoActive) {
    stopDemoMode();
  } else {
    startDemoMode();
  }
}

/** Expand the chat dock if collapsed (the sidebar IS the demo's answer
 * surface) — via the ChatSidebar-owned toggle so React state stays in sync. */
function ensureChatDockOpen(): void {
  const sidebar = document.getElementById('chat-sidebar');
  if (sidebar?.classList.contains('collapsed')) w().toggleChatSidebar?.();
}

/** Send one prompt through the NORMAL composer path (persona-chip contract:
 * full text in the input, short label as the displayed user message). */
function demoAsk(prompt: RegistryPrompt): boolean {
  const input = document.getElementById('question-input') as HTMLTextAreaElement | null;
  if (!input || typeof w().sendMessage !== 'function') return false;
  input.value = prompt.text || '';
  w()._nextUserDisplayText = prompt.display_text || prompt.title || prompt.text || '';
  w().sendMessage!();
  return true;
}

type SettleResult = 'ok' | 'error' | 'stopped';

/** Wait until the turn started after `baselineTurns` settles in the thread
 * store (streaming false). Pause-aware; resolves 'stopped' on demo stop and
 * 'error' on an error turn or timeout. */
function waitForTurnSettle(baselineTurns: number): Promise<SettleResult> {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const tick = setInterval(() => {
      if (!_demoActive) {
        clearInterval(tick);
        resolve('stopped');
        return;
      }
      if (_demoPaused) return;
      const { turns } = chatThreadStore.getState();
      if (turns.length > baselineTurns) {
        const assistant = turns[turns.length - 1].assistant;
        if (!assistant.streaming) {
          clearInterval(tick);
          resolve(assistant.error != null ? 'error' : 'ok');
          return;
        }
      }
      if (Date.now() - startedAt > TURN_TIMEOUT_MS) {
        clearInterval(tick);
        resolve('error');
      }
    }, 200);
  });
}

/** Length-aware reading dwell for the settled answer. */
function readingDwellMs(): number {
  const { turns } = chatThreadStore.getState();
  const answer = turns.length ? turns[turns.length - 1].assistant.answerMd || '' : '';
  return Math.max(DWELL_MIN_MS, Math.min(DWELL_MAX_MS, answer.length * DWELL_MS_PER_CHAR));
}

export async function startDemoMode(): Promise<void> {
  if (Date.now() - _demoLastStop < 1500) return;
  if (_demoActive) return;

  // Fetch registry prompts
  let prompts: RegistryPrompt[] = [];
  try {
    const all = await fetchPrompts(getPromptQueryClient());
    prompts = all.filter((p) => p.source === 'registry' && p.text);
  } catch (e) {
    console.error('Demo: failed to load prompts', e);
    return;
  }
  if (!prompts.length) return;

  _demoActive = true;
  setDemoActive(true);
  const btn = document.getElementById('demo-btn');
  if (btn) {
    btn.textContent = '■ Stop Demo';
    btn.style.background = 'var(--red)';
  }

  // Stage: Architecture → Investigation tab, with the chat dock OPEN — the
  // answer narrates in the sidebar while the investigation graph animates.
  // (requestArchTab stashes the request if ArchitectureView has not lazily
  // mounted yet — first visit.)
  w().switchView?.('architecture');
  requestArchTab('investigation');
  ensureChatDockOpen();

  // Exit gestures (Space/Escape/mousedown) route through the leave-confirm;
  // cooldown so the starting click can't immediately open it.
  _demoExitReady = false;
  _demoPaused = false;
  setTimeout(() => {
    _demoExitReady = true;
  }, 1000);
  registerExitListeners();

  // Run the prompts sequentially through the normal chat flow. The finally
  // ALWAYS removes the capture listeners — a rejection mid-loop must never
  // leak them (a leaked listener force-navigated EVERY background mousedown
  // to the dashboard).
  try {
    for (let i = 0; i < prompts.length && _demoActive; i++) {
      await demoWaitWhilePaused();
      if (!_demoActive) break;
      // Fresh session per query — clears the thread + investigation graph
      // through the same '+ New' path a user takes.
      w().startNewSession?.();
      const baseline = chatThreadStore.getState().turns.length;
      if (!demoAsk(prompts[i])) {
        console.error('Demo: chat composer unavailable — stopping.');
        break;
      }
      const settled = await waitForTurnSettle(baseline);
      if (settled === 'stopped') break;
      if (settled === 'error') {
        // The thread shows the SAME red error paragraph chat shows — leave
        // it up long enough to read, then stop the run.
        await demoSleep(5000);
        break;
      }
      await demoSleep(readingDwellMs());
    }
  } catch (e) {
    console.error('Demo run failed:', e);
  } finally {
    removeExitListeners();
    if (_demoActive) stopDemoMode();
  }
}

export function stopDemoMode(): void {
  // Always safe to call (any number of times): listener removal is
  // idempotent, and a second call after teardown is a full no-op.
  removeExitListeners();
  if (!_demoActive) return;
  _demoActive = false;
  _demoPaused = false;
  _demoLastStop = Date.now();
  setDemoActive(false);
  demoConfirmHide();
  // Abort any in-flight turn through the normal Stop path (quiet canceled
  // marker in the thread — same as a user Stop).
  w().stopStreaming?.();
  const btn = document.getElementById('demo-btn');
  if (btn) {
    btn.textContent = '▶ Demo';
    btn.style.background = 'var(--accent)';
  }
  w().switchView?.('dashboard');
}

/** Pause-aware sleep: elapsed time only accumulates while the demo is NOT
 * paused (leave-confirm up), and resolves promptly on stop. */
function demoSleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    let elapsed = 0;
    const tick = setInterval(() => {
      if (!_demoActive) {
        clearInterval(tick);
        resolve();
        return;
      }
      if (_demoPaused) return;
      elapsed += 100;
      if (elapsed >= ms) {
        clearInterval(tick);
        resolve();
      }
    }, 100);
  });
}

/** Block while the leave-confirm is up (Stay clears the pause; Leave stops
 * the demo, which also unblocks). */
function demoWaitWhilePaused(): Promise<void> {
  if (!_demoActive || !_demoPaused) return Promise.resolve();
  return new Promise((resolve) => {
    const tick = setInterval(() => {
      if (!_demoActive || !_demoPaused) {
        clearInterval(tick);
        resolve();
      }
    }, 100);
  });
}

/** Register the demo button handler for the shell's #demo-btn (Task 3.2 wires
 * the click; the original relied on an inline onclick global). */
export function registerDemoGlobals(): void {
  w().handleDemoBtn = handleDemoBtn;
}
