// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Delegated click handling for the conversation thread (#messages) AND its
 * pop-out modal clone (#modal-messages).
 *
 * The legacy UI attached inline onclick attributes / element handlers; the modal
 * mirrors the thread by cloning innerHTML, which keeps attribute handlers but
 * drops property handlers. The SPA replaces both with ONE delegated handler
 * per container, driven by classes + data-attributes — so every interactive
 * element (tool pills, follow-up chips, citation badges, sources, feedback
 * thumbs, bookmarks, Self-Learning links) stays live in both surfaces. */
import { submitFeedback } from './Feedback';
import { boundRecordFor } from './provenance';
import { chatThreadStore, type AskOptions } from './useAskStream';

/** T2.4 wiring: resolve the clicked citation by IDENTITY, not by target —
 * `data-cite-target` (`system-<tool>`) is NON-UNIQUE both within a turn
 * (two same-system citations) and across turns. The clicked element's own
 * turn container must BE the active turn (its `data-turn-id` matches the
 * last turn) — a badge on an EARLIER turn always runs the legacy
 * focusArchNode path verbatim. Within the active turn the binding is the
 * citation ordinal (`data-cite-num` on the badge / Sources-row chip),
 * never find-first-target. True ⇒ the Tier-2 card opened (store dispatch —
 * both mounted <ChatThread/> surfaces re-render); false ⇒ the caller runs
 * the legacy focusArchNode path VERBATIM (chip fallback contract; also
 * covers rehydrated-history badges and legacy streams without records). */
function tryOpenEvidence(cite: HTMLElement, target: string): boolean {
  const { turns } = chatThreadStore.getState();
  const active = turns.length ? turns[turns.length - 1] : null;
  if (!active) return false;
  const turnEl = cite.closest<HTMLElement>('.message.assistant');
  if (!turnEl || !turnEl.dataset.turnId || turnEl.dataset.turnId !== active.id) return false;
  const num = Number(cite.dataset.citeNum);
  if (!Number.isInteger(num)) return false;
  const a = active.assistant;
  const citation = a.citations.find((c) => c.num === num);
  if (!citation || (citation.findingNodeId || citation.nodeId) !== target) return false;
  const record = boundRecordFor(a.provenance, a.pills.values(), citation.num);
  if (!record) return false;
  chatThreadStore.openEvidence(citation.num);
  return true;
}

/** Citation-badge navigation:
 * switchView('architecture') then focus/fit the target node after the view
 * transition (300ms), opening the data modal when the node carries detail. */
export function focusArchNode(target: string): void {
  window.switchView?.('architecture');
  setTimeout(() => {
    const cy = window.cy;
    if (cy) {
      cy.resize();
      const n = cy.getElementById(target);
      if (n.length) {
        cy.fit(n.closedNeighborhood(), 60);
        n.select();
        const detail = n.data('detail');
        if (detail) window.openDataModal?.(n.data('label'), detail);
      }
    }
  }, 300);
}

/** Self-Learning link navigation (intelligence/agent-analysis anchors). */
function navSelfLearning(): void {
  window.closeDataModal?.();
  window.switchView?.('architecture');
  setTimeout(() => window.switchArchTab?.('proposed'), 200);
}

/** KG-contribution chip navigation (data-nav="kg-node") — land on the
 * Knowledge Graph tab and focus the applied-rule node through the existing
 * selection machinery (neptune-graph focusKgNode). The architecture view is
 * lazy-loaded (AppShell) — a static import here would drag cytoscape into
 * the main chat chunk, so the module is imported dynamically (it shares the
 * lazy view's chunk). focusKgNode returning false (graph not loaded yet /
 * node absent) leaves the tab open with NO selection — the honest fallback,
 * never a fake highlight, no error thrown. */
function navKgNode(nodeId: string): void {
  window.closeDataModal?.();
  window.switchView?.('architecture');
  setTimeout(() => {
    window.switchArchTab?.('neptune');
    setTimeout(() => {
      import('../views/architecture/neptune-graph')
        .then((m) => m.focusKgNode(nodeId))
        .catch(() => {}); // chunk-load failure (deploy skew/offline) — tab stays open, no selection
    }, 300);
  }, 200);
}

export interface ThreadClickDeps {
  /** Follow-up chip → set composer text and send (askQuestion). `options`
   * is passed ONLY for "Ask again" on the last turn (askOptionsFor). */
  askQuestion: (question: string, options?: AskOptions) => void;
  /** Bookmark on a user message → save as prompt (quick-save or modal). */
  savePrompt: (text: string, btn: HTMLButtonElement) => void;
}

/** "Ask again" on the LAST turn re-asks a question whose answer never
 * reached the user, so the send carries `retry` and the server leaves that
 * attempt out of the history the new turn reads. Any other chip, and "Ask
 * again" on an older turn (the latest stored turn is then a different
 * question), is an ordinary question. Only error turns show "Ask again"
 * (<AssistantTurn/> renders it with the red error line). */
function askOptionsFor(chip: HTMLElement): AskOptions | undefined {
  if (!chip.classList.contains('ask-again-btn')) return undefined;
  const { turns } = chatThreadStore.getState();
  const last = turns.length ? turns[turns.length - 1] : null;
  const turnEl = chip.closest<HTMLElement>('.message.assistant');
  return last && turnEl?.dataset.turnId === last.id ? { retry: true } : undefined;
}

/** Structural event type — satisfied by both native MouseEvent and the React
 * synthetic event system, so the handler can attach via addEventListener or JSX. */
export interface ClickLikeEvent {
  target: EventTarget | null;
  preventDefault: () => void;
}

/** One handler, attached (via React onClick) to both thread containers. */
export function createThreadClickHandler(deps: ThreadClickDeps) {
  return (e: ClickLikeEvent): void => {
    const target = e.target as HTMLElement | null;
    if (!target) return;

    // Feedback thumbs
    const fb = target.closest<HTMLButtonElement>('.feedback-btn');
    if (fb) {
      const msg = fb.closest<HTMLElement>('.message');
      void submitFeedback(fb, msg?.dataset.messageId || '');
      return;
    }

    // Bookmark (save user question as prompt)
    const bm = target.closest<HTMLButtonElement>('.bookmark-btn');
    if (bm) {
      const msg = bm.closest<HTMLElement>('.message');
      deps.savePrompt(msg?.dataset.rawContent || msg?.textContent || '', bm);
      return;
    }

    // Citation badges + Sources row links. A citation with a bound
    // provenance record on the active turn opens the Tier-2 evidence card
    // (T2.4); WITHOUT a record the legacy arch-focus behavior runs verbatim.
    const cite = target.closest<HTMLAnchorElement>('a[data-cite-target]');
    if (cite) {
      e.preventDefault();
      const t = cite.dataset.citeTarget;
      if (t && tryOpenEvidence(cite, t)) return;
      if (t) focusArchNode(t);
      return;
    }

    // Self-Learning navigation (Agent Analysis / intelligence links)
    const nav = target.closest<HTMLAnchorElement>('a[data-nav="self-learning"]');
    if (nav) {
      e.preventDefault();
      navSelfLearning();
      return;
    }

    // KG-contribution rule chips → focus the matching KG-view node
    const kgNav = target.closest<HTMLAnchorElement>('a[data-nav="kg-node"]');
    if (kgNav) {
      e.preventDefault();
      const nodeId = kgNav.dataset.nodeId;
      if (nodeId) navKgNode(nodeId);
      return;
    }

    // Follow-up / persona suggestion chips ("Ask again" included)
    const chip = target.closest<HTMLElement>('.suggestion-chip[data-question]');
    if (chip) {
      const q = chip.dataset.question;
      const options = askOptionsFor(chip);
      if (q && options) deps.askQuestion(q, options);
      else if (q) deps.askQuestion(q);
      return;
    }

    // Tool pills → toggle the matching collapsible trace. Scope the lookup to
    // the pill's reasoning container so tool ids never collide across turns.
    const pill = target.closest<HTMLElement>('.tool-pill[data-tool-id]');
    if (pill) {
      const scope = pill.closest<HTMLElement>('.reasoning-container') || pill.parentElement;
      const toolId = pill.dataset.toolId || '';
      const trace = scope?.querySelector<HTMLElement>(
        `.tool-trace[data-tool-id="${CSS.escape(toolId)}"]`,
      );
      trace?.classList.toggle('visible');
    }
  };
}
