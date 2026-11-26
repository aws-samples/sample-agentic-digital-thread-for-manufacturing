// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <PreMapProbe/> — per-system domain-context probing (T3.1 T-H).
 *
 * Rendered BETWEEN discover and the caller's map request, while the schema
 * is fresh in the steward's mind. No answer changes the map that runs next:
 * the wizard has no confirm step, so it asks the server only for questions
 * whose answer is filed as a proposal for review when given
 * (`durable_answers_only`; a held business-context answer is refused, never
 * kept where nothing saves it), and the card says so.
 *
 * It is deliberately NOT a gate. `/map` is untouched and this component never
 * blocks it: if the probe call fails, returns nothing material, or the steward
 * skips, `onContinue` fires and mapping proceeds exactly as before. The
 * component posts the discover response's tables VERBATIM (`tables` body
 * field); the SERVER owns fact production (`schema_facts_from_tables`) — no
 * client-side mirror of the fact shape. Questions are chosen server-side
 * (answerable AND material, capped), and each carries the schema evidence that
 * triggered it — pointing at the real column is what makes the answer specific
 * rather than generic.
 *
 * Answers are UNTRUSTED text: controlled input values only, and the server
 * sanitizes at write. A rejected answer, or one that was not filed, does not
 * advance. */
import { useEffect, useState } from 'react';

import { ApiError, requestJson } from '../../../api/client';
import type { DiscoveredTable } from '../../../api/types/onboard';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import { turnResultLine, type TurnResult } from './context-shared';

const MUTED = 'var(--muted)';
const RED = 'var(--red, #f87171)';

export function probeUrl(systemId: string): string {
  return `/api/onboard/guided/probe/${encodeURIComponent(systemId)}`;
}

/** The probe route reads its body as JSON only when the request says so: a
 * body sent without this header arrives as text and is refused 422. */
const JSON_HEADERS = { 'Content-Type': 'application/json' };

/** What one submitted answer did: the line to show and whether to move on.
 * A real answer moves on only once it was filed (`proposed`); a skip moves
 * on unless refused; a rejected or unfiled answer stays, with the reason. */
export function answerStep(
  outcome: TurnResult | undefined,
  skip: boolean,
): { line: { color: string; text: string } | null; advance: boolean } {
  if (outcome?.outcome === 'rejected') return { line: turnResultLine(outcome), advance: false };
  if (skip || outcome?.outcome === 'proposed') {
    return { line: outcome ? turnResultLine(outcome) : null, advance: true };
  }
  const why = outcome?.detail || outcome?.reason || 'no result came back';
  return { line: { color: RED, text: `Not filed: ${why}. Answer again, or skip.` }, advance: false };
}

export type ProbeQuestion = {
  id: string;
  prompt: string;
  evidence?: string;
  treatment_phase?: string;
};

export type DerivedEntry = {
  question_id: string;
  topic?: string;
  outcome: string;
  evidence?: string;
  derived_text?: string;
  reason?: string;
};

export type ProbeResponse = {
  system_id: string;
  results?: TurnResult[];
  questions: ProbeQuestion[];
  derived?: DerivedEntry[];
  draft_business_context?: string;
};

type Props = {
  systemId: string;
  /** Raw discover tables, forwarded verbatim to the probe body. */
  tables: DiscoveredTable[];
  /** Called when the steward is done (answered, skipped, or nothing to ask). */
  onContinue: () => void;
};

/** The derived entries that render as a note: landed, with text. A rejected
 * one (for example not filed, ``not_ingested``) shows nothing, so it never
 * holds the card open on its own. */
export function derivedNotes(derived: DerivedEntry[]): DerivedEntry[] {
  return derived.filter((d) => d.outcome !== 'rejected' && d.derived_text);
}

/** What the schema already answered — shown once, above the questions (or on
 * its own with a Continue button when there is nothing left to ask). */
function DerivedNotes({ derived }: { derived: DerivedEntry[] }) {
  const notes = derivedNotes(derived);
  if (notes.length === 0) return null;
  return (
    <div style={{ margin: '0.45rem 0' }}>
      <div style={{ fontSize: '0.72rem', color: MUTED }}>
        Noted from the discovered schema (no answer needed):
      </div>
      <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.1rem' }}>
        {notes.map((d) => (
          <li key={d.question_id} style={{ fontSize: '0.78rem', color: MUTED }}>
            {d.derived_text}
            {d.evidence && (
              <div style={{ fontSize: '0.72rem', color: MUTED, marginTop: '0.1rem' }}>
                based on: {d.evidence}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PreMapProbe({ systemId, tables, onContinue }: Props) {
  useSystemNamesVersion(); // the header names the system
  const [questions, setQuestions] = useState<ProbeQuestion[] | null>(null);
  const [derived, setDerived] = useState<DerivedEntry[]>([]);
  const [index, setIndex] = useState(0);
  const [answer, setAnswer] = useState('');
  const [line, setLine] = useState<{ color: string; text: string } | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await requestJson<ProbeResponse>(probeUrl(systemId), {
          method: 'POST',
          headers: JSON_HEADERS,
          body: JSON.stringify({ tables, answers: [], durable_answers_only: true }),
        });
        if (live) {
          setQuestions(res.questions ?? []);
          setDerived(derivedNotes(res.derived ?? []));
        }
      } catch {
        // Probing is additive — never stand between the steward and mapping.
        // BOTH lists go empty so the auto-continue effect below fires.
        if (live) {
          setQuestions([]);
          setDerived([]);
        }
      }
    })();
    return () => {
      live = false;
    };
  }, [systemId, tables]);

  // Nothing asked AND nothing derived (or probing unavailable) ⇒ get out of
  // the way. Anything derived is shown for review — no silent vanish.
  useEffect(() => {
    if (questions !== null && questions.length === 0 && derived.length === 0) {
      onContinue();
    }
  }, [questions, derived, onContinue]);

  if (questions === null || (questions.length === 0 && derived.length === 0)) {
    return null;
  }

  // Local non-null binding: `questions` is state and TS cannot narrow it inside
  // the async submit closure below.
  const asked: ProbeQuestion[] = questions;
  const current = asked[index];
  if (asked.length > 0 && !current) {
    onContinue();
    return null;
  }

  async function submit(skip: boolean) {
    if (!current) return; // unreachable: the buttons only render with a question
    setLine(null);
    try {
      const res = await requestJson<ProbeResponse>(probeUrl(systemId), {
        method: 'POST',
        headers: JSON_HEADERS,
        // Reviewed (no-stringify-keys): stringify builds the request BODY —
        // property order is irrelevant to the server; nothing keys off it.
        // nosemgrep
        body: JSON.stringify({
          tables,
          answers: [{ question_id: current.id, answer: skip ? '' : answer }],
          durable_answers_only: true,
        }),
      });
      const step = answerStep(res.results?.[0], skip);
      setLine(step.line);
      if (!step.advance) return; // fix or skip, don't advance
      setAnswer('');
      if (index + 1 >= asked.length) {
        onContinue();
      } else {
        setIndex((i) => i + 1);
      }
    } catch (e) {
      setLine({
        color: RED,
        text: e instanceof ApiError ? e.detail || e.message : 'Request failed',
      });
    }
  }

  return (
    <section
      aria-label="Pre-map questions"
      style={{
        background: 'var(--surface2)',
        border: '1px solid var(--border)',
        borderRadius: 8,
        padding: '0.9rem',
        marginBottom: '1rem',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.6rem' }}>
        <strong style={{ fontSize: '0.9rem' }}>Before mapping {systemName(systemId)}</strong>
        {asked.length > 0 && (
          <span style={{ fontSize: '0.72rem', color: MUTED }}>
            {index + 1} of {asked.length}
          </span>
        )}
        <button
          type="button"
          onClick={onContinue}
          style={{ marginLeft: 'auto', fontSize: '0.75rem' }}
        >
          Skip and map now
        </button>
      </div>
      <DerivedNotes derived={derived} />
      {current === undefined ? (
        // Nothing left to ask — derived notes above, explicit Continue below.
        <div style={{ marginTop: '0.4rem' }}>
          <button type="button" onClick={onContinue}>
            Continue
          </button>
        </div>
      ) : (
        <>
          <p style={{ fontSize: '0.78rem', color: MUTED, margin: '0.35rem 0 0.5rem' }}>
            Each answer is filed as a proposal for review when you submit it. It
            does not change the mapping that runs next.
          </p>
          <label htmlFor="probe-answer" className="ob-prompt-label" style={{ fontSize: '0.85rem' }}>
            {current.prompt}
          </label>
          {current.evidence && (
            <div style={{ fontSize: '0.72rem', color: MUTED, marginTop: '0.2rem' }}>
              based on: {current.evidence}
            </div>
          )}
          <textarea
            id="probe-answer"
            aria-label="Probe answer"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={3}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              marginTop: '0.4rem',
              background: 'var(--surface)',
              color: 'var(--text)',
              border: '1px solid var(--border)',
              borderRadius: 4,
              padding: '0.4rem',
              fontSize: '0.8rem',
              fontFamily: 'inherit',
            }}
          />
          <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.4rem' }}>
            <button type="button" onClick={() => submit(false)} disabled={!answer.trim()}>
              Next
            </button>
            <button type="button" onClick={() => submit(true)} style={{ fontSize: '0.78rem' }}>
              I don't know
            </button>
          </div>
        </>
      )}
      {line && (
        <div style={{ color: line.color, fontSize: '0.75rem', marginTop: '0.5rem' }}>
          {line.text}
        </div>
      )}
    </section>
  );
}

export default PreMapProbe;
