// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <AssistantTurn/> — one `.message.assistant` rendered from the ChatTurn
 * model (jsx-migration W3.1; zero-innerHTML migration). DOM-contract-
 * equivalent to the imperative per-turn DOM the old runAskTurn built:
 *
 *   .message.assistant [data-message-id/-user-message/-assistant-response]
 *     div (contentDiv)
 *       details.tool-calls-details            (after done-collapse)
 *         summary.tool-calls-summary-toggle   "Reasoning" + the turn's
 *              activity summary + "N calls · 1 failed" (summarizeReasoning)
 *         div.reasoning-container             (bare while streaming)
 *           [.stream-md runs interleaved with .tool-activity lines — one per
 *            run of back-to-back calls; each expands to .tool-pill.inline +
 *            .tool-trace rows (<ToolActivityGroup/>)]
 *       div (answer: <AnswerBody/> — md chunks + cite badges + charts +
 *            collapsible sections, all React elements)
 *       div > details.agent-analysis-details  (<AgentAnalysisPanel/>)
 *       div.response-canceled                 "⏹ Response canceled by user"
 *       div.turn-error                        (error: red paragraph BELOW the
 *            partial reasoning/pills + the "Ask again" chip-style button)
 *     div.answer-footer                       (<AnswerFooter/>: follow-up
 *          chips, then Sources chips + feedback thumbs on one row, then the
 *          Tier-2 evidence card when a source is open)
 *
 * When the answer lands, the reasoning folds into the <details> and the
 * answer would jump up by the folded height; ChatThread's landAnswer slides
 * it from where the streamed text was instead (after the follow scroll).
 *
 * Everything renders as React elements — markdown via the shared <Md/>
 * (react-markdown), tool-input blocks via <ToolInputBlock/>, the analysis
 * panel via <AgentAnalysisPanel/>, and the final answer via <AnswerBody/>
 * (chart split → citation links → section collapse, string-level). No
 * HTML-string sink exists on this path. */

import { useI18n } from '../lib/i18n';
import { useSystemNamesVersion } from '../lib/system-names';
import { stripControlBlocksForStream } from '../shared/thread/answer-blocks';
import { Md } from '../shared/ui/Md';
import { AgentAnalysisPanel } from './analysis-panel';
import { AnswerBody } from './AnswerBody';
import { AnswerFooter } from './AnswerFooter';
import { stripChartBlocks, stripChartBlocksForStream } from './charts';
import { groupBackToBack, summarizeReasoning } from './tool-activity';
import { ToolActivityGroup } from './ToolActivityGroup';
import type { ThreadStore } from './thread-store';
import type { ChatTurn } from './turn-model';
import './answer.css';


export interface AssistantTurnProps {
  turn: ChatTurn;
  /** turn.rev captured at render time (a copied number) — the memo wrapper in
   * ChatThread compares it across notifies; comparing `turn.rev` on both
   * sides would always be equal because the turn object mutates in place. */
  rev: number;
  store: ThreadStore;
}

export function AssistantTurn({ turn, store }: AssistantTurnProps) {
  const { t } = useI18n();
  // The Reasoning fold summary names systems by their catalog name.
  useSystemNamesVersion();
  const a = turn.assistant;
  const done = a.answerMd != null;

  // ── reasoning container: completed runs interleaved with pills/traces ───
  const pills = [...a.pills.values()];
  const runs = a.segments.completedRuns;
  // After done, the trailing run (rendered live below the last pill) was
  // removed in favor of the final answer — runs past the last pill's
  // boundary are hidden (the old "remove final streaming div" step).
  const lastBoundary = pills.length ? pills[pills.length - 1].segBoundary : 0;
  const visibleRunCount = done ? Math.min(lastBoundary, runs.length) : runs.length;

  const reasoningChildren: React.ReactNode[] = [];
  let runIdx = 0;
  // Back-to-back calls (no narration between them) fold into ONE activity
  // line (tool-activity.ts); narration runs interleave between the lines.
  for (const group of groupBackToBack(pills)) {
    while (runIdx < Math.min(group[0].segBoundary, visibleRunCount)) {
      // Chart-block tags are stripped from reasoning/streaming runs (review
      // fix — react-markdown would render the tag source as escaped text;
      // the old DOMPurify streaming path dropped it). The final answer's
      // charts render via <AnswerBody/>.
      reasoningChildren.push(
        <Md key={`run-${runIdx}`} className="stream-md" text={stripChartBlocks(runs[runIdx])} />,
      );
      runIdx++;
    }
    reasoningChildren.push(
      <ToolActivityGroup
        key={`grp-${group[0].toolId}`}
        pills={group}
        traces={a.traces}
        turnId={turn.id}
        store={store}
      />,
    );
  }
  while (runIdx < visibleRunCount) {
    reasoningChildren.push(
      <Md key={`run-${runIdx}`} className="stream-md" text={stripChartBlocks(runs[runIdx])} />,
    );
    runIdx++;
  }
  // The live in-progress run (rAF-coalesced by the store notify); kept
  // as-rendered under the canceled marker after an abort. Complete chart
  // tags AND a partial trailing one are hidden while streaming.
  if (!done && a.segments.streamingText) {
    reasoningChildren.push(
      <Md
        key="run-live"
        className="stream-md"
        text={stripControlBlocksForStream(stripChartBlocksForStream(a.segments.streamingText))}
      />,
    );
  }

  const reasoningContainer =
    reasoningChildren.length > 0 ? (
      <div className="reasoning-container">{reasoningChildren}</div>
    ) : null;

  // The fold reads as what the turn did ("Searched the knowledge graph ·
  // Queried ERP and CMMS", "5 calls · 1 failed"), not a bare call count. An
  // answer with nothing above it (no calls, no narration) gets no fold.
  const fold =
    a.reasoningCollapsed && reasoningContainer ? summarizeReasoning(pills, a.traces) : null;

  return (
    <div
      className="message assistant msg-enter"
      data-turn-id={turn.id}
      data-message-id={a.messageId ?? undefined}
      data-user-message={a.messageId ? turn.user.raw : undefined}
      data-assistant-response={a.messageId ? (a.answerMd ?? '').slice(0, 500) : undefined}
    >
      <div>
        {fold ? (
          <details className="tool-calls-details" data-tone={fold.tone}>
            <summary
              className="tool-calls-summary-toggle"
              title={[t('chat.reasoning'), fold.text, fold.meta].filter(Boolean).join(' · ')}
            >
              <span className="reasoning-fold-title">{t('chat.reasoning')}</span>
              {fold.text ? (
                <>
                  <span className="reasoning-fold-dot" aria-hidden="true" />
                  <span className="reasoning-fold-text">{fold.text}</span>
                </>
              ) : null}
              {fold.meta ? <span className="reasoning-fold-meta">{fold.meta}</span> : null}
            </summary>
            {reasoningContainer}
          </details>
        ) : (
          reasoningContainer
        )}
        {a.answerMd != null && <AnswerBody answerMd={a.answerMd} citations={a.citations} />}
        {a.analysisPanel && (
          <div>
            <AgentAnalysisPanel data={a.analysisPanel} />
          </div>
        )}
        {a.canceled && (
          <div className="response-canceled">
            <span aria-hidden="true">⏹ </span>
            {t('chat.canceled')}
          </div>
        )}
        {a.error != null && (
          // `event: error` / non-abort catch — the red paragraph renders
          // BELOW the reasoning and pills the turn already streamed, so the
          // attendee still sees which systems ran (plain text, JSX-escaped).
          // The muted line surfaces the correlation id for a support report;
          // "Ask again" re-sends this turn's POSTed question through the
          // delegated `.suggestion-chip[data-question]` path (askQuestion).
          <div className="turn-error">
            <p className="turn-error-text">
              {t('chat.errorPrefix')} {a.error}
              {a.errorRef != null && (
                <span className="turn-error-ref">
                  {t('chat.errorRef')} {a.errorRef}
                </span>
              )}
            </p>
            <button
              type="button"
              className="suggestion-chip ask-again-btn"
              data-question={turn.user.raw}
            >
              {t('chat.askAgain')}
            </button>
          </div>
        )}
      </div>
      {/* Only a finished answer has a footer: citations accumulate while the
       * calls stream, and Sources must not appear under a half-written
       * answer (the pre-footer rows were gated on done the same way). */}
      {done && <AnswerFooter turn={turn} store={store} />}
    </div>
  );
}

/* The per-turn memo wrapper lives in ChatThread.tsx (it also covers the user
 * message) and compares the value-captured `rev` prop. */
