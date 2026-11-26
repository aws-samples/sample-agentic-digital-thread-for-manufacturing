// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <AnswerBody/> — the final-answer container of an assistant turn
 * (zero-innerHTML migration). Replaces the sanitized-HTML string pipeline
 * (mdParse → sanitize → `[N]`-badge string replace → DOM chart reinjection →
 * DOM collapse post-processor) with element rendering:
 *
 *   buildAnswerModel (answer-sections.ts): chart split → citation links →
 *   section split, then <Md/> chunks interleaved with <ChartIframe/>, and
 *   long multi-section answers wrapped in <details class="response-section">.
 *
 * User toggle state is React state keyed by section index, so it survives
 * re-renders of the turn (rev bumps from trace toggles, feedback, etc.).
 *
 * Perf note: react-markdown's parse is heavier than marked's. <Md/> is
 * memoized on its text, and the model is memoized on answerMd — the answer
 * parses ONCE on done; during token streaming only the live `.stream-md` run
 * (AssistantTurn) re-parses per animation frame. */
import { useMemo, useState } from 'react';

import { Md } from '../shared/ui/Md';
import { buildAnswerModel } from './answer-sections';
import { ChartIframe } from './ChartIframe';
import type { ChartSplitPart } from './charts';
import type { Citation } from './turn-model';

function Chunks({ parts }: { parts: ChartSplitPart[] }) {
  return (
    <>
      {parts.map((p, i) =>
        p.kind === 'md' ? (
          <Md key={`md-${i}`} bare text={p.text} />
        ) : (
          <ChartIframe key={`chart-${i}`} attrs={p.attrs} />
        ),
      )}
    </>
  );
}

export interface AnswerBodyProps {
  answerMd: string;
  /** Final by `done` (findingNodeIds link on tool_result). */
  citations: readonly Citation[];
}

export function AnswerBody({ answerMd, citations }: AnswerBodyProps) {
  const model = useMemo(() => buildAnswerModel(answerMd, citations), [answerMd, citations]);
  /** Section-index → user override of the details open state. */
  const [openState, setOpenState] = useState<Record<number, boolean>>({});

  return (
    <div className="answer-body">
      <Chunks parts={model.intro} />
      {model.sections.map((sec, i) => (
        <details
          key={i}
          className="response-section"
          open={openState[i] ?? sec.defaultOpen}
          onToggle={(e) => {
            // Only this details' own toggle (markdown could nest others).
            if (e.target !== e.currentTarget) return;
            const now = (e.target as HTMLDetailsElement).open;
            setOpenState((s) => (s[i] === now ? s : { ...s, [i]: now }));
          }}
        >
          <summary className="response-section-summary">
            <Md bare inline text={sec.heading} />
          </summary>
          <div className="response-section-body">
            <Chunks parts={sec.parts} />
          </div>
        </details>
      ))}
    </div>
  );
}
