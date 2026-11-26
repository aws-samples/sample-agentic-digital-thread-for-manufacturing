// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <AnswerFooter/> — everything under a finished answer in ONE footer band
 * (one divider instead of three): suggested next steps, then a single meta
 * row with the Sources chips on the left and the feedback thumbs on the
 * right, then the evidence card when a source is open.
 *
 *   div.answer-footer
 *     div.followup-chips[role=group]            (<FollowupChips/>)
 *     div.answer-footer-meta
 *       div.answer-sources                       "Sources" + chips
 *         a.source-chip[data-cite-target][data-cite-num][data-verification][data-outcome]
 *         a.source-chip.source-chip-earlier[data-cite-target][data-earlier]  ([E<k>])
 *       div.feedback-row (+ .feedback-details)   (<FeedbackRow/>)
 *     p.answer-evidence-note                     (reload only: evidence not kept)
 *     p.answer-evidence-note.answer-charts-note  (reload only: charts not all shown)
 *     div.evidence-card-host                     (<EvidenceCard/> + arch link)
 *
 * DOM contracts kept: Sources chips stay `a[data-cite-target][data-cite-num]`
 * without `.cite-badge` (tests/e2e/test_provenance.py; thread-clicks.ts
 * delegation), chips keep `.suggestion-chip[data-question]`, thumbs keep
 * `.feedback-row .feedback-btn[data-sentiment]`. Styling lives in answer.css
 * (no inline styles), so chips get real hover and focus states. */
import { useI18n } from '../lib/i18n';
import { systemName, useSystemNamesVersion } from '../lib/system-names';
import { FollowupChips } from '../shared/thread/answer-blocks';
import { citedSystemIds } from './cited-systems';
import { EvidenceCard } from './EvidenceCard';
import { FeedbackRow } from './Feedback';
import {
  boundRecordFor,
  chipLabel,
  citationOutcome,
  citedEarlierCitations,
  verificationBadge,
  type CitationOutcome,
} from './provenance';
import { focusArchNode } from './thread-clicks';
import type { ThreadStore } from './thread-store';
import { citationLabel } from './tool-labels';
import type { ChatTurn } from './turn-model';
import './answer.css';

/** The plain suffix a Sources chip carries when its call gave no answer. */
const OUTCOME_SUFFIX_KEY: Record<CitationOutcome, string> = {
  failed: 'chat.sourceNoAnswer',
  timeout: 'chat.sourceNoAnswer',
  empty: 'chat.sourceNoData',
};

/** Distinct tool names this turn called, in call order — the feedback
 * record's `tools_used` (capped: the server stores the list as sent). */
export const MAX_TOOLS_USED = 40;
export function toolsUsed(turn: ChatTurn): string[] {
  const out: string[] = [];
  for (const pill of turn.assistant.pills.values()) {
    const name = pill.tool.slice(0, 160);
    if (name && !out.includes(name)) out.push(name);
    if (out.length >= MAX_TOOLS_USED) break;
  }
  return out;
}

export function AnswerFooter({ turn, store }: { turn: ChatTurn; store: ThreadStore }) {
  const { t } = useI18n();
  // Re-render when catalog system names arrive (Sources chips name systems).
  useSystemNamesVersion();
  const a = turn.assistant;
  // This turn's own calls, then the earlier-question results ([E<k>]) the
  // answer actually cites (an uncited earlier result gets no chip).
  const current = a.citations.filter((c) => !c.earlier);
  const earlier = citedEarlierCitations(a.citations, a.answerMd ?? '');
  // The citations the footer shows: the feedback record's cited systems.
  const shown = [...current, ...earlier];
  // Tier-2 evidence card (T2.4): mounts when openEvidenceNum matches one of
  // THIS turn's citations (the store only sets it on the active turn).
  const openCitation =
    a.openEvidenceNum != null
      ? (current.find((c) => c.num === a.openEvidenceNum) ?? null)
      : null;
  const hasChips = a.followups.length > 0 || a.actions.length > 0;
  const hasSources = current.length > 0 || earlier.length > 0;
  if (!hasChips && !hasSources && !a.messageId && !openCitation && !a.chartsNotShown) return null;

  return (
    <div className="answer-footer">
      <FollowupChips followups={a.followups} actions={a.actions} />
      {(hasSources || a.messageId) && (
        <div className="answer-footer-meta">
          {hasSources && (
            <div className="answer-sources">
              <span className="answer-sources-label">{t('chat.sources')}</span>
              {current.map((s) => {
                // D2 dual binding (boundRecordFor: pill toolId primary,
                // citation_num fallback). A null record keeps the legacy
                // label verbatim (chipLabel) and carries no data-verification.
                const record = boundRecordFor(a.provenance, a.pills.values(), s.num);
                const badge = record ? verificationBadge(record) : null;
                // A call that gave no answer is marked, never dropped
                // (dropping it would renumber the inline citations).
                const outcome = citationOutcome(a.pills.values(), a.traces, s.num);
                return (
                  <a
                    key={s.num}
                    href="#"
                    className="source-chip"
                    data-cite-target={s.findingNodeId || s.nodeId}
                    data-cite-num={s.num}
                    data-verification={badge ?? undefined}
                    data-outcome={outcome ?? undefined}
                    title={badge ? `evidence: ${badge}` : undefined}
                  >
                    [{s.num}] {chipLabel(record, citationLabel(s), systemName)}
                    {outcome ? ` · ${t(OUTCOME_SUFFIX_KEY[outcome])}` : ''}
                  </a>
                );
              })}
              {earlier.map((s) => (
                <a
                  key={`E${s.num}`}
                  href="#"
                  className="source-chip source-chip-earlier"
                  data-cite-target={s.nodeId}
                  data-earlier="true"
                >
                  [E{s.num}] {citationLabel(s)} · {t('chat.sourceEarlier')}
                </a>
              ))}
            </div>
          )}
          {a.messageId && (
            <FeedbackRow
              messageId={a.messageId}
              userMessage={turn.user.raw}
              assistantResponse={(a.answerMd ?? '').slice(0, 500)}
              toolsUsed={toolsUsed(turn)}
              citedSystems={citedSystemIds(shown.map((c) => c.tool))}
              citedRules={(a.scratch.lastIntelligenceSummary?.rules_applied ?? []).slice(0, 20)}
            />
          )}
        </div>
      )}
      {a.evidenceNotKept && current.length > 0 && (
        <p className="answer-evidence-note">{t('chat.evidenceNotKept')}</p>
      )}
      {a.chartsNotShown && (
        <p className="answer-evidence-note answer-charts-note">{t('chat.chartsNotShown')}</p>
      )}
      {openCitation && (
        <div className="evidence-card-host">
          <EvidenceCard
            record={boundRecordFor(a.provenance, a.pills.values(), openCitation.num)}
            citation={openCitation}
            graphQueries={a.graphQueries}
            outcome={citationOutcome(a.pills.values(), a.traces, openCitation.num)}
            onClose={() => store.openEvidence(null)}
          />
          {/* The plan's "show in architecture" affordance — rendered by the
           * mount (the card is pure-props). Calls focusArchNode directly. */}
          <a
            href="#"
            className="evidence-arch-link"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              focusArchNode(openCitation.findingNodeId || openCitation.nodeId);
            }}
          >
            {t('chat.showInArchitecture')}
          </a>
        </div>
      )}
    </div>
  );
}
