// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — self-learning proposal-card components (SlCards.tsx). jsx-migration
 * W5.2: the former string-builder assertions (sl-cards.test.ts over slCard/
 * lgCard HTML strings) became rendered-DOM assertions over <SlCard/>/<LgCard/>
 * with the SAME semantic coverage: schema-verdict gating (not_found blocks
 * Accept with proof; verified line), pending-only actions, insight (Finding)
 * card rules, usage badge, overlap signal, XSS escaping, and the detected_gap
 * filter. Inline onclick strings became onClick props — click assertions
 * replace the onclick-attribute string checks. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { ToastRegion } from '../shared/ui/ToastRegion';
import { clear as clearToasts } from '../shared/ui/toast-store';
import {
  answerConfirm,
  confirmDialog,
  confirmText,
  installConfirmHost,
  runConfirmed,
} from './confirm-host';
import {
  acceptAllPendingMappings,
  acceptMapping,
  clearAllLearnings,
  clearRejectedMappings,
  clearSystemGuidance,
  loadLearnedGuidance,
  loadProposedMappings,
  populateUserDefineDatalists,
  proposalsStore,
  pruneGuidanceNote,
  reopenMapping,
  resetAllMappings,
  submitEdit,
} from '../views/architecture/proposals';
import {
  LearnedGuidanceList,
  PendingBadge,
  ProposalsList,
} from '../views/architecture/ProposalsPanel';
import {
  isTombstoneReason,
  LgCard,
  pendingCount,
  reviewBannerFlavour,
  SL_TYPE_META,
  SlCard,
  SlCardBody,
  TOMBSTONE_REASON_PREFIX,
  validMappings,
} from '../views/architecture/SlCards';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  clearToasts();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  clearToasts();
  proposalsStore.set({
    phase: 'idle',
    mappings: [],
    error: '',
    statusFilter: 'pending',
    optimistic: {},
    ruleLifecycle: {},
    guidancePhase: 'idle',
    guidance: [],
    guidanceError: '',
    edit: null,
  });
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const base: ProposedMapping = {
  mapping_id: 'm-1',
  proposed_type: 'value_mapping',
  status: 'pending',
  confidence: '0.8',
  from_system: 'erp',
  from_field: 'orders.status',
  from_value: 'SHIPPED',
  to_system: 'mes',
  to_field: 'runs.state',
  to_value: 'DONE',
};

const click = (el: Element | null) => {
  expect(el).not.toBeNull();
  act(() => {
    (el as HTMLElement).click();
  });
};

describe('SlCard: schema-verdict gating', () => {
  it('not_found renders the ⛔ block with proof, disables Accept, keeps Reject', () => {
    const onAccept = vi.fn();
    const onReject = vi.fn();
    render(
      <SlCard
        m={{ ...base, schema_verdict: 'not_found', schema_checked: 'orders.status' }}
        onAccept={onAccept}
        onReject={onReject}
      />,
    );
    const card = container.querySelector('.sl-card')!;
    expect(card.className).toBe('sl-card pending blocked');
    expect(card.textContent).toContain('⛔ Blocked — do not accept');
    const proof = card.querySelector('.sl-block-proof')!;
    expect(proof.textContent).toContain(
      'Checked orders.status against the onboarded ERP schema — the column is not present.',
    );
    expect(proof.querySelector('code')!.textContent).toBe('orders.status');
    // Accept disabled (no handler fires), Reject still actionable.
    const accept = card.querySelector<HTMLButtonElement>('.sl-accept')!;
    expect(accept.disabled).toBe(true);
    expect(accept.getAttribute('aria-disabled')).toBe('true');
    click(accept);
    expect(onAccept).not.toHaveBeenCalled();
    click(card.querySelector('.sl-reject'));
    expect(onReject).toHaveBeenCalledWith('m-1');
  });

  it('not_found suppresses the discovery rationale in the "why" details', () => {
    render(
      <SlCard
        m={{
          ...base,
          schema_verdict: 'not_found',
          schema_checked: 'orders.status',
          analysis: 'This mapping looks safe to accept',
          query_context: 'the trigger question',
        }}
      />,
    );
    const why = container.querySelector('.sl-why-body')!;
    expect(why.textContent).not.toContain('This mapping looks safe to accept');
    expect(why.textContent).toContain('earlier rationale suppressed');
    expect(why.textContent).toContain('the trigger question'); // provenance stays
  });

  it('verified renders the ✅ line and an enabled Accept', () => {
    const onAccept = vi.fn();
    render(
      <SlCard
        m={{ ...base, schema_verdict: 'verified', schema_checked: 'orders.status' }}
        onAccept={onAccept}
      />,
    );
    const verified = container.querySelector('.sl-verified')!;
    expect(verified.textContent).toContain('✅ Verified —');
    expect(verified.textContent).toContain('orders.status exists in the onboarded schema.');
    expect(verified.querySelector('code')!.textContent).toBe('orders.status');
    expect(container.querySelector('.sl-card')!.classList.contains('blocked')).toBe(false);
    click(container.querySelector('.sl-accept'));
    expect(onAccept).toHaveBeenCalledWith('m-1');
  });

  it('no verdict falls back to the free-text ai_recommendation', () => {
    render(<SlCard m={{ ...base, ai_recommendation: 'Check values first', ai_confidence: 'medium' }} />);
    const ai = container.querySelector<HTMLElement>('.sl-ai')!;
    expect(ai.textContent).toContain('⚠ Check values first');
    expect(ai.style.color).toBe('var(--amber)');
  });
});

describe('SlCard: status + actions', () => {
  it('pending shows Accept/Reject/Edit; accepted/rejected show none', () => {
    const onEdit = vi.fn();
    render(<SlCard m={base} onEdit={onEdit} />);
    expect(container.querySelector('.sl-card-actions')).not.toBeNull();
    click(container.querySelector('.sl-edit'));
    expect(onEdit).toHaveBeenCalledWith('m-1');

    render(<SlCard m={{ ...base, status: 'accepted' }} />);
    expect(container.querySelector('.sl-card-actions')).toBeNull();
    expect(container.querySelector('.sl-card')!.classList.contains('accepted')).toBe(true);

    render(<SlCard m={{ ...base, status: 'rejected' }} />);
    expect(container.querySelector('.sl-card-actions')).toBeNull();
  });

  it('confidence renders as a percentage, or — when absent', () => {
    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-conf b')!.textContent).toBe('80%');
    render(<SlCard m={{ ...base, confidence: undefined }} />);
    expect(container.querySelector('.sl-conf b')!.textContent).toBe('—');
  });

  it('escapes attacker-controlled fields (rendered as text, not markup)', () => {
    render(<SlCard m={{ ...base, from_system: `<script>alert('x')</script>` }} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('.sl-chip .sys')!.textContent).toBe(
      `<script>alert('x')</script>`,
    );
  });

  it('optimistic accept/reject replaces the actions with the applied marker', () => {
    render(<SlCard m={base} optimistic="accept" />);
    let card = container.querySelector<HTMLElement>('.sl-card')!;
    expect(card.classList.contains('accepted')).toBe(true);
    expect(card.querySelector('.sl-applied')!.textContent).toBe('✓ Applied');
    expect(card.querySelector('button.sl-accept')).toBeNull();
    expect(card.querySelector('.sl-status')!.textContent).toContain('accepted');

    render(<SlCard m={base} optimistic="reject" />);
    card = container.querySelector<HTMLElement>('.sl-card')!;
    expect(card.classList.contains('rejected')).toBe(true);
    expect(card.querySelector('.sl-applied--rejected')!.textContent).toBe('✗ Removed');
    expect(card.style.opacity).toBe('0.6');
  });
});

describe('SlCard: overlap signal (join_path / crosswalk, pending only)', () => {
  const badge = () => container.querySelector('.sl-overlap-badge')?.textContent;

  it('rates High/Med/Low from the overlap ratio', () => {
    const jp = { ...base, proposed_type: 'join_path' as const };
    render(<SlCard m={{ ...jp, overlap_ratio: '0.7', value_overlap: '12' }} />);
    expect(badge()).toBe('High');
    expect(container.querySelector('.sl-overlap-signal')!.textContent).toContain('12 shared');
    render(<SlCard m={{ ...jp, overlap_ratio: '0.3' }} />);
    expect(badge()).toBe('Med');
    render(<SlCard m={{ ...jp, overlap_ratio: '0.1' }} />);
    expect(badge()).toBe('Low');
  });

  it('shows "not measured" when no signal exists, and nothing for value_mapping', () => {
    render(<SlCard m={{ ...base, proposed_type: 'join_path' }} />);
    expect(container.querySelector('.sl-overlap-none')).not.toBeNull();
    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-overlap')).toBeNull();
  });
});

describe('SlCardBody', () => {
  it('field_annotation describes the field + note', () => {
    render(
      <SlCardBody
        m={{
          mapping_id: 'm-2',
          proposed_type: 'field_annotation',
          from_system: 'erp',
          from_field: 'orders.priority',
          query_context: 'Auto-analysis: priority is 1-5 where 1 is highest',
          tool_calls_before: '7',
        }}
      />,
    );
    expect(container.textContent).toContain('Add a domain note to');
    expect(container.querySelector('.sl-anno-note')!.textContent).toBe(
      'priority is 1-5 where 1 is highest',
    );
    expect(container.textContent).not.toContain('Auto-analysis:');
    const impact = container.querySelector('.sl-impact')!;
    expect(impact.textContent).toContain('it took 7 tool calls last time');
    expect(impact.querySelector('b:last-of-type')!.textContent).toBe('7');
  });

  it('relationship types phrase the impact per type', () => {
    render(<SlCardBody m={base} />);
    expect(container.textContent).toContain('translate values between these fields');
    expect(container.querySelector('.sl-arrow')).not.toBeNull();
    render(<SlCardBody m={{ ...base, proposed_type: 'identity_rule' }} />);
    expect(container.textContent).toContain('treat these fields as the same concept');
    render(<SlCardBody m={{ ...base, proposed_type: 'join_path' }} />);
    expect(container.textContent).toContain('join these fields directly');
  });
});

describe('LgCard (learned guidance)', () => {
  it('renders per-note prune buttons and the clear-learned action', () => {
    const onPrune = vi.fn();
    const onClearLearned = vi.fn();
    render(
      <LgCard
        sys={{ system_id: 'erp', notes: ['note one', 'note two'], length: 120, max_len: 4000 }}
        onPrune={onPrune}
        onClearLearned={onClearLearned}
      />,
    );
    const prunes = container.querySelectorAll('.lg-prune');
    expect(prunes.length).toBe(2);
    click(prunes[1]);
    expect(onPrune).toHaveBeenCalledWith('erp', 'note two');
    expect(container.textContent).toContain('2 learned');
    expect(container.querySelector('.lg-meter')!.textContent).toBe('120/4000 chars');
    click(container.querySelector('.sl-reject'));
    expect(onClearLearned).toHaveBeenCalledWith('erp');
  });

  it('renders the empty state without a clear button', () => {
    render(<LgCard sys={{ system_id: 'erp', notes: [] }} />);
    expect(container.textContent).toContain('No learned notes yet');
    expect(container.querySelector('.sl-reject')).toBeNull();
  });

  it('shows no character meter while there are no learned notes', () => {
    // The server counts the empty notes document's own JSON (108 chars), so a
    // meter here read "108/4000" on every card (walk 2026-11-12).
    render(<LgCard sys={{ system_id: 'erp', notes: [], length: 108, max_len: 4000 }} />);
    expect(container.textContent).toContain('0 learned');
    expect(container.querySelector('.lg-meter')).toBeNull();
    expect(container.textContent).not.toContain('/4000');
  });

  it('shows the meter once a note is learned', () => {
    render(<LgCard sys={{ system_id: 'erp', notes: ['n'], length: 120, max_len: 4000 }} />);
    expect(container.querySelector('.lg-meter')!.textContent).toBe('120/4000 chars');
  });
});

describe('validMappings / pendingCount', () => {
  it('filters detected_gap garbage and counts pending', () => {
    const list: ProposedMapping[] = [
      base,
      { ...base, mapping_id: 'm-3', from_field: 'detected_gap' },
      { ...base, mapping_id: 'm-4', status: 'accepted' },
    ];
    const valid = validMappings(list);
    expect(valid.map((m) => m.mapping_id)).toEqual(['m-1', 'm-4']);
    expect(pendingCount(valid)).toBe(1);
  });
});

describe('ProposalsPanel count lifecycle and guidance clarity', () => {
  const chip = (status: string) =>
    container.querySelector<HTMLButtonElement>(`[data-status-filter="${status}"]`)!;

  it('shows indeterminate header and chip counts while proposals are loading with no data', () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'loading',
      mappings: [],
    });

    render(
      <>
        <PendingBadge />
        <ProposalsList />
      </>,
    );

    expect(container.querySelector('#self-learning-count')!.textContent).toBe('… pending');
    expect(container.textContent).not.toContain('0 pending');
    expect(chip('pending').textContent).toBe('Pending …');
    expect(chip('accepted').textContent).toBe('Accepted …');
    expect(chip('rejected').textContent).toBe('Rejected …');
    expect(chip('superseded').textContent).toBe('History …');
  });

  it('shows honest zero header and chip counts after a successful empty response', () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      mappings: [],
    });

    render(
      <>
        <PendingBadge />
        <ProposalsList />
      </>,
    );

    expect(container.querySelector('#self-learning-count')!.textContent).toBe('0 pending');
    expect(chip('pending').textContent).toBe('Pending 0');
    expect(chip('accepted').textContent).toBe('Accepted 0');
    expect(chip('rejected').textContent).toBe('Rejected 0');
    expect(chip('superseded').textContent).toBe('History 0');
  });

  it('labels query guidance as future-query hints distinct from accepted proposals', () => {
    render(<LearnedGuidanceList />);

    expect(container.querySelector('.sl-toolbar h4')!.textContent).toBe('Query Guidance');
    const explainer = container.querySelector('.sl-intro')!.textContent!;
    expect(explainer).toContain('Auto-learned, per-system hints applied to future queries.');
    expect(explainer).toContain('distinct from reviewed proposals in the Accepted filter');
  });
});

describe('ProposalsList ordering and status filters', () => {
  it('renders pending confidence descending, preserving ties and placing missing confidence last', () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      mappings: [
        { ...base, mapping_id: 'missing-a', confidence: undefined },
        { ...base, mapping_id: 'low', confidence: '0.2' },
        { ...base, mapping_id: 'high-a', confidence: '0.9' },
        { ...base, mapping_id: 'high-b', confidence: 0.9 },
        { ...base, mapping_id: 'missing-b', confidence: undefined },
      ],
    });

    render(<ProposalsList />);

    expect(
      Array.from(container.querySelectorAll<HTMLElement>('.sl-card')).map(
        (card) => card.dataset.mappingId,
      ),
    ).toEqual(['high-a', 'high-b', 'low', 'missing-a', 'missing-b']);
  });

  it('defaults to pending only and switches chips, count, and per-status confidence order', async () => {
    // Status-scoped fetch: each chip click requests ONLY its bucket; the other
    // chips are labelled from the server status_counts.
    const rows: ProposedMapping[] = [
      { ...base, mapping_id: 'pending-low', confidence: '0.2' },
      { ...base, mapping_id: 'accepted-low', status: 'accepted', confidence: '0.3' },
      { ...base, mapping_id: 'history', status: 'superseded', confidence: '0.7' },
      { ...base, mapping_id: 'pending-high', confidence: '0.9' },
      { ...base, mapping_id: 'rejected', status: 'rejected', confidence: '0.6' },
      { ...base, mapping_id: 'accepted-high', status: 'accepted', confidence: '0.8' },
    ];
    const status_counts = { pending: 2, accepted: 2, rejected: 1, superseded: 1, expired: 0 };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const wanted = new URL(url, 'http://x').searchParams.get('status');
        return proposalResponse({
          mappings: rows.filter((m) => (m.status || 'pending') === wanted),
          status_counts,
        });
      }),
    );
    const flush = async () => {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
    };

    render(
      <>
        <PendingBadge />
        <ProposalsList />
      </>,
    );
    await act(async () => {
      await loadProposedMappings();
    });

    const visibleIds = () =>
      Array.from(container.querySelectorAll<HTMLElement>('.sl-card')).map(
        (card) => card.dataset.mappingId,
      );
    const chip = (status: string) =>
      container.querySelector<HTMLButtonElement>(`[data-status-filter="${status}"]`)!;

    expect(visibleIds()).toEqual(['pending-high', 'pending-low']);
    expect(container.querySelector('#self-learning-count')!.textContent).toBe('2 pending');
    expect(chip('pending').getAttribute('aria-pressed')).toBe('true');
    expect(chip('pending').textContent).toBe('Pending 2');
    expect(chip('accepted').textContent).toBe('Accepted 2');
    expect(chip('rejected').textContent).toBe('Rejected 1');
    expect(chip('superseded').textContent).toBe('History 1');

    click(chip('accepted'));
    await flush();
    expect(visibleIds()).toEqual(['accepted-high', 'accepted-low']);
    expect(container.querySelector('#self-learning-count')!.textContent).toBe('2 accepted');
    expect(chip('accepted').getAttribute('aria-pressed')).toBe('true');

    click(chip('rejected'));
    await flush();
    expect(visibleIds()).toEqual(['rejected']);
    expect(container.querySelector('#self-learning-count')!.textContent).toBe('1 rejected');

    click(chip('superseded'));
    await flush();
    expect(visibleIds()).toEqual(['history']);
    expect(container.querySelector('#self-learning-count')!.textContent).toBe('1 history');
  });
});

describe('SlCardBody: insight (Finding) proposals', () => {
  const insight: ProposedMapping = {
    mapping_id: 'm-i1',
    proposed_type: 'insight',
    status: 'pending',
    confidence: '0.7',
    from_system: 'erp',
    to_system: 'mes',
    query_context: 'Auto-analysis: Order dates in ERP lag MES completion by ~2 days.',
  };

  it('renders the narrative from query_context (Auto-analysis prefix stripped), no From→To chips', () => {
    render(<SlCardBody m={insight} />);
    expect(container.querySelector('.sl-anno-note')!.textContent).toBe(
      'Order dates in ERP lag MES completion by ~2 days.',
    );
    expect(container.textContent).not.toContain('Auto-analysis:');
    expect(container.textContent).toContain('Finding about');
    expect(container.querySelector('.sl-arrow')).toBeNull(); // no From→To chip row
    expect(container.textContent).toContain(
      'Informational — saved as context, never becomes a graph rule.',
    );
  });

  it('falls back when no finding text was captured and escapes the narrative', () => {
    render(<SlCardBody m={{ ...insight, query_context: undefined }} />);
    expect(container.textContent).toContain('No finding text captured.');
    render(<SlCardBody m={{ ...insight, query_context: '<script>x</script>' }} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('.sl-anno-note')!.textContent).toBe('<script>x</script>');
  });

  it('insight type meta renders the Finding badge with its own color', () => {
    expect(SL_TYPE_META.insight).toEqual({ label: 'Finding', color: '#0f766e' });
    render(<SlCard m={insight} />);
    const badge = container.querySelector<HTMLElement>('.sl-type')!;
    expect(badge.textContent).toBe('Finding');
    expect(badge.style.background).not.toBe('');
  });

  it('pending insight card: Accept marks reviewed, Reject dismisses, Edit is hidden', () => {
    render(<SlCard m={insight} />);
    expect(container.querySelector('.sl-accept')!.getAttribute('title')).toBe(
      'Mark this finding reviewed — informational only, writes nothing to the knowledge graph',
    );
    expect(container.querySelector('.sl-reject')!.getAttribute('title')).toBe(
      'Dismiss this finding',
    );
    expect(container.querySelector('.sl-edit')).toBeNull();
  });
});

describe('SlCard: usage signal (utility loop, item C)', () => {
  it('accepted + usage_count > 0 renders "used n×" with the last-used date', () => {
    render(
      <SlCard m={{ ...base, status: 'accepted', usage_count: 7, last_used_at: '2026-07-01T12:00:00Z' }} />,
    );
    const usage = container.querySelector('.sl-usage')!;
    expect(usage.textContent).toContain('used 7×');
    expect(usage.textContent).toContain(' · last ');
  });

  it('omitted when never used, when usage_count absent, or when not accepted', () => {
    render(<SlCard m={{ ...base, status: 'accepted', usage_count: 0 }} />);
    expect(container.querySelector('.sl-usage')).toBeNull();
    render(<SlCard m={{ ...base, status: 'accepted' }} />);
    expect(container.querySelector('.sl-usage')).toBeNull();
    render(<SlCard m={{ ...base, status: 'pending', usage_count: 5 }} />);
    expect(container.querySelector('.sl-usage')).toBeNull();
  });

  it('bad last_used_at drops the date suffix but keeps the count', () => {
    render(<SlCard m={{ ...base, status: 'accepted', usage_count: 3, last_used_at: 'not-a-date' }} />);
    const usage = container.querySelector('.sl-usage')!;
    expect(usage.textContent).toContain('used 3×');
    expect(usage.textContent).not.toContain(' · last ');
  });
});

describe('SlCard: review-reason banner (ADR-0042 D4) + crosswalk type', () => {
  it('crosswalk proposals render the Crosswalk type chip', () => {
    render(<SlCard m={{ ...base, proposed_type: 'crosswalk' }} />);
    const type = container.querySelector('.sl-type')!;
    expect(type.textContent).toBe('Crosswalk');
    expect(SL_TYPE_META.crosswalk.color).toBe('#4f46e5');
  });

  it('quarantined pending card: amber adjudication banner + quarantined class', () => {
    render(
      <SlCard
        m={{
          ...base,
          quarantined: 'true',
          review_reason: 'Flip-flopped 3 times — frozen pending adjudication.',
        }}
      />,
    );
    const card = container.querySelector('.sl-card')!;
    expect(card.className).toContain('quarantined');
    const banner = container.querySelector('.sl-review')!;
    expect(banner.className).toContain('sl-review--quarantine');
    expect(banner.getAttribute('role')).toBe('note');
    expect(banner.querySelector('.sl-review-head')!.textContent).toBe('⚖ Needs adjudication');
    expect(banner.querySelector('.sl-review-reason')!.textContent).toBe(
      'Flip-flopped 3 times — frozen pending adjudication.',
    );
  });

  it('re-proposed after rejection: rejected flavour with prior count (both shapes)', () => {
    render(
      <SlCard
        m={{
          ...base,
          review_reason: 'Previously rejected.',
          prior_rejections: [{ mapping_id: 'm-0', rejected_at: '2026-07-01' }],
        }}
      />,
    );
    let banner = container.querySelector('.sl-review')!;
    expect(banner.className).toContain('sl-review--rejected');
    expect(banner.querySelector('.sl-review-head')!.textContent).toBe(
      '↩ Re-proposed after rejection (1 prior rejection)',
    );
    // Legacy shape: plain list of ids, plural count.
    render(
      <SlCard
        m={{ ...base, review_reason: 'Previously rejected.', prior_rejections: ['a', 'b'] }}
      />,
    );
    banner = container.querySelector('.sl-review')!;
    expect(banner.querySelector('.sl-review-head')!.textContent).toBe(
      '↩ Re-proposed after rejection (2 prior rejections)',
    );
  });

  it('plain ambiguity note uses the neutral base flavour', () => {
    render(<SlCard m={{ ...base, review_reason: 'Ambiguous field semantics.' }} />);
    const banner = container.querySelector('.sl-review')!;
    expect(banner.className).toBe('sl-review');
    expect(banner.querySelector('.sl-review-head')!.textContent).toBe('⚠ Review note');
  });

  it('pending-only: no banner without review_reason, none on accepted cards, and quarantined!=true is not amber', () => {
    render(<SlCard m={{ ...base }} />);
    expect(container.querySelector('.sl-review')).toBeNull();
    render(
      <SlCard m={{ ...base, status: 'accepted', review_reason: 'stale', quarantined: 'true' }} />,
    );
    expect(container.querySelector('.sl-review')).toBeNull();
    expect(container.querySelector('.sl-card')!.className).not.toContain('quarantined');
    render(<SlCard m={{ ...base, review_reason: 'note', quarantined: 'false' }} />);
    expect(container.querySelector('.sl-review')!.className).toBe('sl-review');
  });
});


function proposalResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe('proposal request transport', () => {
  it('keeps prior proposals on non-2xx, exposes server detail, and retry recovers', async () => {
    const prior: ProposedMapping = { ...base, mapping_id: 'prior' };
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      mappings: [prior],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(proposalResponse({ detail: 'proposal store offline' }, 503))
      .mockResolvedValueOnce(proposalResponse({ mappings: [base] }));
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});

    await loadProposedMappings();
    expect(proposalsStore.get()).toMatchObject({
      phase: 'error',
      mappings: [prior],
      error: 'proposal store offline',
    });

    await loadProposedMappings();
    expect(proposalsStore.get()).toMatchObject({
      phase: 'ready',
      mappings: [base],
      error: '',
    });
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/proposed-mappings?status=pending', {
      credentials: 'same-origin',
    });
  });

  it('puts learned-guidance non-2xx into its explicit error state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(proposalResponse({ message: 'guidance unavailable' }, 502)),
    );
    vi.spyOn(console, 'error').mockImplementation(() => {});

    await loadLearnedGuidance();

    expect(proposalsStore.get()).toMatchObject({
      guidancePhase: 'error',
      guidanceError: 'guidance unavailable',
    });
  });

  it('surfaces failed catalog and concept suggestion requests instead of empty success', async () => {
    const message = document.createElement('div');
    message.id = 'ud-message';
    const systemsList = document.createElement('datalist');
    systemsList.id = 'ud-systems-list';
    const fieldsList = document.createElement('datalist');
    fieldsList.id = 'ud-fields-list';
    const conceptsList = document.createElement('datalist');
    conceptsList.id = 'ud-concepts-list';
    document.body.append(message, systemsList, fieldsList, conceptsList);
    vi.spyOn(console, 'error').mockImplementation(() => {});

    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/onboard/catalog'
          ? proposalResponse({ detail: 'catalog unavailable' }, 503)
          : proposalResponse({ concepts: [] }),
      ),
    );
    await populateUserDefineDatalists(true);
    expect(message.textContent).toContain('catalog unavailable');

    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/onboard/catalog'
          ? proposalResponse([])
          : proposalResponse({ error: 'concepts unavailable' }, 502),
      ),
    );
    await populateUserDefineDatalists(true);
    expect(message.textContent).toContain('concepts unavailable');
  });
});


describe('proposal notifications', () => {
  it('renders failed actions as urgent error toasts and never calls alert', async () => {
    render(<ToastRegion />);
    const alertSpy = vi.fn();
    vi.stubGlobal('alert', alertSpy);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(proposalResponse({ status: 'failed', error: 'gateway denied' })),
    );

    await act(async () => {
      await acceptMapping('m-1');
    });

    const notice = container.querySelector('[data-toast-kind="error"]');
    expect(notice?.textContent).toContain('Accept failed: gateway denied');
    expect(notice?.closest('[role="alert"]')).not.toBeNull();
    expect(alertSpy).not.toHaveBeenCalled();
    expect(acceptMapping.toString()).not.toContain('alert(');
  });

  it('renders successful actions as success toasts in the polite region', async () => {
    render(<ToastRegion />);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url.endsWith('/edit')
          ? proposalResponse({ status: 'updated' })
          : proposalResponse({ mappings: [] }),
      ),
    );

    await act(async () => {
      expect(await submitEdit('m-1', { description: 'confirmed' })).toBeNull();
    });

    const notice = container.querySelector('[data-toast-kind="success"]');
    expect(notice?.textContent).toContain('Proposal updated');
    expect(notice?.closest('[role="status"]')).not.toBeNull();
  });
});

describe('clear buttons read the response body', () => {
  // The clear endpoints return HTTP 200 even on failure, with {"error": ...}
  // in the body — a success toast on a failed clear is the exact regression
  // the ref fixed (silent AccessDenied 2026-07-19).
  it('clear-all: 200 body with {error} ⇒ error toast, never a success toast', async () => {
    render(<ToastRegion />);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/proposed-mappings/clear-all'
          ? proposalResponse({ error: 'AccessDenied: not authorized' })
          : proposalResponse({ mappings: [] }),
      ),
    );

    await runConfirmed(clearAllLearnings, 'confirm');

    const err = container.querySelector('[data-toast-kind="error"]');
    expect(err?.textContent).toContain('Clear all failed: AccessDenied: not authorized');
    expect(container.querySelector('[data-toast-kind="success"]')).toBeNull();
  });

  it('clear-rejected: 200 body with {error} ⇒ error toast, never a success toast', async () => {
    render(<ToastRegion />);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/proposed-mappings/clear-rejected'
          ? proposalResponse({ error: 'AccessDenied: not authorized' })
          : proposalResponse({ mappings: [] }),
      ),
    );

    await runConfirmed(clearRejectedMappings, 'confirm');

    const err = container.querySelector('[data-toast-kind="error"]');
    expect(err?.textContent).toContain('Clear rejected failed: AccessDenied: not authorized');
    expect(container.querySelector('[data-toast-kind="success"]')).toBeNull();
  });

  it('success toasts carry the cleared count from the body (0 when absent)', async () => {
    render(<ToastRegion />);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/proposed-mappings/clear-all'
          ? proposalResponse({ status: 'cleared', count: 7 })
          : url === '/api/proposed-mappings/clear-rejected'
            ? proposalResponse({ status: 'cleared' })
            : proposalResponse({ mappings: [] }),
      ),
    );

    await runConfirmed(clearAllLearnings, 'confirm');
    await runConfirmed(clearRejectedMappings, 'confirm');

    const toasts = Array.from(container.querySelectorAll('[data-toast-kind="success"]'))
      .map((el) => el.textContent ?? '')
      .join('\n');
    expect(toasts).toContain('All learnings cleared (7)');
    expect(toasts).toContain('Rejected mappings cleared (0)');
  });

  it('each bulk action asks with its own title, impact body and specific danger label', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const cases: Array<[() => Promise<void>, { title: string; body: string; buttons: string[] }]> = [
      [
        resetAllMappings,
        {
          title: 'Reset accepted mappings?',
          body: 'This will remove all accepted self-learned mappings from the knowledge graph.',
          buttons: ['Cancel', 'Reset mappings'],
        },
      ],
      [
        clearRejectedMappings,
        {
          title: 'Clear rejected mappings?',
          body: 'All rejected mappings are cleared from the list.',
          buttons: ['Cancel', 'Clear rejected'],
        },
      ],
      [
        clearAllLearnings,
        {
          title: 'Delete all learnings?',
          body: 'This will delete all learnings (pending, accepted, rejected) and remove accepted nodes from Neptune.',
          buttons: ['Cancel', 'Delete all learnings'],
        },
      ],
      [
        acceptAllPendingMappings,
        {
          title: 'Accept all pending mappings?',
          body: 'Every pending mapping is accepted and written to the knowledge graph.',
          buttons: ['Cancel', 'Accept all'],
        },
      ],
      [
        () => pruneGuidanceNote('mes', 'old note'),
        {
          title: 'Remove learned note?',
          body: 'This removes the learned note from the system guidance.',
          buttons: ['Cancel', 'Remove note'],
        },
      ],
      [
        () => clearSystemGuidance('mes'),
        {
          title: 'Remove all learned notes for MES?',
          body: 'The onboarded base guidance is kept.',
          buttons: ['Cancel', 'Remove all notes'],
        },
      ],
    ];
    for (const [action, expected] of cases) {
      let pending!: Promise<void>;
      await act(async () => {
        pending = action();
      });
      expect(confirmText()).toEqual(expected);
      // Accept-all is consequential but not destructive: default tone.
      expect(confirmDialog()!.querySelector('.confirm-danger-btn') !== null).toBe(
        action !== acceptAllPendingMappings,
      );
      await answerConfirm('cancel');
      await act(async () => {
        await pending;
      });
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('declined confirm ⇒ no request, no toast', async () => {
    render(<ToastRegion />);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await runConfirmed(clearAllLearnings, 'cancel');
    await runConfirmed(clearRejectedMappings, 'cancel');

    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelector('[data-toast-kind]')).toBeNull();
  });
});
describe('SlCard: provenance + evidence', () => {
  it('renders the stored provenance attribute when present', () => {
    render(<SlCard m={{ ...base, provenance: 'analysis-llm' }} />);
    expect(container.querySelector('.sl-prov')!.textContent).toBe('via analysis LLM');
    render(<SlCard m={{ ...base, provenance: 'heuristic' }} />);
    expect(container.querySelector('.sl-prov')!.textContent).toBe('via heuristic');
  });

  it('legacy rows (no provenance) fall back to the proposed_type mapping', () => {
    // value_mapping / insight / identity_rule → the orchestrator's own blocks
    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-prov')!.textContent).toBe('via orchestrator');
    // join_path / crosswalk → the join-discovery heuristics
    render(<SlCard m={{ ...base, proposed_type: 'crosswalk' }} />);
    expect(container.querySelector('.sl-prov')!.textContent).toBe('via heuristic');
    render(<SlCard m={{ ...base, proposed_type: 'join_path' }} />);
    expect(container.querySelector('.sl-prov')!.textContent).toBe('via heuristic');
    // field_annotation → the post-query analysis LLM
    render(<SlCard m={{ ...base, proposed_type: 'field_annotation' }} />);
    expect(container.querySelector('.sl-prov')!.textContent).toBe('via analysis LLM');
  });

  it('renders the method/reason evidence lines when present (#3 threading)', () => {
    render(
      <SlCard
        m={{
          ...base,
          method: 'looked up the asset registry by serial',
          reason: 'serial suffix matched across both systems',
        }}
      />,
    );
    const why = container.querySelector('.sl-why-body')!;
    expect(why.textContent).toContain('Method: looked up the asset registry by serial');
    expect(why.textContent).toContain('Reason: serial suffix matched across both systems');
  });

  it('method/reason alone open the why-details (no query_context needed)', () => {
    render(<SlCard m={{ ...base, query_context: undefined, method: 'direct lookup' }} />);
    expect(container.querySelector('.sl-why-body')!.textContent).toContain(
      'Method: direct lookup',
    );
  });

  it('labels the two confidences distinctly (proposed vs AI review)', () => {
    render(<SlCard m={{ ...base, confidence: '0.9', ai_confidence: 'low' }} />);
    const badges = container.querySelectorAll('.sl-conf');
    expect(badges.length).toBe(2);
    expect(badges[0].textContent).toBe('proposed 90%');
    expect(badges[1].textContent).toBe('AI review low');
    expect((badges[1] as HTMLElement).style.color).toBe('var(--red)');
  });

  it('omits the AI-review badge when no ai_confidence is present', () => {
    render(<SlCard m={base} />);
    expect(container.querySelectorAll('.sl-conf').length).toBe(1);
    expect(container.querySelector('.sl-conf')!.textContent).toBe('proposed 80%');
  });
});

describe('SlCard: what-it-touches line (B2 task 2.3)', () => {
  it('renders systems, concept, impact score, and supersede count on pending cards', () => {
    render(
      <SlCard
        m={{
          ...base,
          consequence: 3.42,
          touches: {
            systems: ['mes-poc', 'cmms-main'],
            concept: 'operator-id',
            conflict: '',
            supersede_count: 2,
          },
        }}
      />,
    );
    const line = container.querySelector('.sl-touches')!;
    expect(line.textContent).toBe(
      'Joins: MES Poc ↔ CMMS Main · concept operator-id · impact 3.4 · replaces 2 prior',
    );
  });

  it('omits the supersede segment at 0 and the whole line without enrichment', () => {
    render(
      <SlCard
        m={{ ...base, consequence: 1.0, touches: { systems: ['erp'], supersede_count: 0 } }}
      />,
    );
    expect(container.querySelector('.sl-touches')!.textContent).toBe('Joins: ERP · impact 1.0');

    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-touches')).toBeNull();
  });

  it('never renders the touches line on non-pending cards', () => {
    render(
      <SlCard
        m={{
          ...base,
          status: 'accepted',
          consequence: 3.42,
          touches: { systems: ['mes-poc', 'cmms-main'], concept: 'operator-id' },
        }}
      />,
    );
    expect(container.querySelector('.sl-touches')).toBeNull();
  });
});

describe('SlCard: usage badge evidence class (B3 surface)', () => {
  const accepted: ProposedMapping = {
    ...base,
    status: 'accepted',
    usage_count: '12',
  };

  it('appends the strongest evidence-class label to the usage badge', () => {
    render(<SlCard m={{ ...accepted, evidence_classes: ['route-intersection'] }} />);
    expect(container.querySelector('.sl-usage')!.textContent).toBe(
      'used 12× · routing evidence',
    );

    render(
      <SlCard
        m={{ ...accepted, evidence_classes: ['route-intersection', 'relationship-proof'] }}
      />,
    );
    expect(container.querySelector('.sl-usage')!.textContent).toBe('used 12× · verified use');

    render(<SlCard m={{ ...accepted, evidence_classes: ['tool-text'] }} />);
    expect(container.querySelector('.sl-usage')!.textContent).toBe('used 12× · observed use');
  });

  it('legacy rows without evidence_classes keep the badge byte-identical', () => {
    render(<SlCard m={accepted} />);
    expect(container.querySelector('.sl-usage')!.textContent).toBe('used 12×');
  });

  it('unknown classes add no segment', () => {
    render(<SlCard m={{ ...accepted, evidence_classes: ['weird-new-class'] }} />);
    expect(container.querySelector('.sl-usage')!.textContent).toBe('used 12×');
  });
});

/* ── ADR-0053 E6 (task 2.4): tombstone flag banner + reopen action ──────── */

// The EXACT pinned backend wording (self_learning._tombstone_review_reason):
// "re-proposal of a shape the steward rejected on <YYYY-MM-DD> (mapping <id>)".
const TOMBSTONE_REASON =
  're-proposal of a shape the steward rejected on 2026-07-01 (mapping m-prior)';

describe('SlCard: tombstone flag banner (ADR-0053 E6, task 2.4)', () => {
  it('renders the pinned tombstone review_reason verbatim with the distinct flavour', () => {
    render(
      <SlCard
        m={{
          ...base,
          review_reason: TOMBSTONE_REASON,
          prior_rejections: [{ mapping_id: 'm-prior', rejected_at: '2026-07-01T10:00:00Z' }],
        }}
      />,
    );
    const banner = container.querySelector('.sl-review')!;
    // Distinct labeling: tombstone wins over the legacy prior_rejections
    // flavour (tombstoned rows carry BOTH the flag wording and the history).
    expect(banner.className).toContain('sl-review--tombstone');
    expect(banner.className).not.toContain('sl-review--rejected');
    expect(banner.getAttribute('role')).toBe('note');
    expect(banner.querySelector('.sl-review-head')!.textContent).toBe(
      '🪦 Steward rejected this shape',
    );
    expect(banner.querySelector('.sl-review-reason')!.textContent).toBe(TOMBSTONE_REASON);
  });

  it('quarantine still wins over tombstone; non-tombstone wording keeps legacy flavours', () => {
    render(<SlCard m={{ ...base, quarantined: 'true', review_reason: TOMBSTONE_REASON }} />);
    expect(container.querySelector('.sl-review')!.className).toContain('sl-review--quarantine');
    // prior_rejections WITHOUT the pinned wording → legacy rejected flavour.
    render(
      <SlCard m={{ ...base, review_reason: 'Previously rejected.', prior_rejections: ['a'] }} />,
    );
    expect(container.querySelector('.sl-review')!.className).toContain('sl-review--rejected');
    // The reopen stamp is NOT a tombstone → neutral note.
    render(<SlCard m={{ ...base, review_reason: 're-opened by steward' }} />);
    expect(container.querySelector('.sl-review')!.className).toBe('sl-review');
  });

  it('pending-only: no tombstone banner on rejected cards', () => {
    render(<SlCard m={{ ...base, status: 'rejected', review_reason: TOMBSTONE_REASON }} />);
    expect(container.querySelector('.sl-review')).toBeNull();
  });

  it('isTombstoneReason keys on the exact pinned prefix', () => {
    expect(isTombstoneReason(TOMBSTONE_REASON)).toBe(true);
    expect(isTombstoneReason(`${TOMBSTONE_REASON_PREFIX}2026-01-02 (mapping x)`)).toBe(true);
    expect(isTombstoneReason('re-opened by steward')).toBe(false);
    expect(isTombstoneReason('Ambiguous field semantics.')).toBe(false);
    expect(isTombstoneReason(undefined)).toBe(false);
    expect(isTombstoneReason('')).toBe(false);
  });

  it('reviewBannerFlavour precedence: quarantine → tombstone → history → note', () => {
    const tomb = { ...base, review_reason: TOMBSTONE_REASON, prior_rejections: ['a'] };
    expect(reviewBannerFlavour(tomb, true).headCls).toBe(' sl-review--quarantine');
    expect(reviewBannerFlavour(tomb, false).headCls).toBe(' sl-review--tombstone');
    expect(
      reviewBannerFlavour({ ...base, review_reason: 'x', prior_rejections: ['a'] }, false),
    ).toEqual({
      head: '↩ Re-proposed after rejection (1 prior rejection)',
      headCls: ' sl-review--rejected',
    });
    expect(reviewBannerFlavour({ ...base, review_reason: 'x' }, false)).toEqual({
      head: '⚠ Review note',
      headCls: '',
    });
  });
});

describe('SlCard: reopen action on rejected cards (ADR-0053 E6, task 2.4)', () => {
  it('rejected card with onReopen renders ↩ Re-open and fires the handler', () => {
    const onReopen = vi.fn();
    render(<SlCard m={{ ...base, status: 'rejected' }} onReopen={onReopen} />);
    const btn = container.querySelector('.sl-card-actions .sl-reopen')!;
    expect(btn.textContent).toBe('↩ Re-open');
    click(btn);
    expect(onReopen).toHaveBeenCalledWith('m-1');
  });

  it('no Re-open without the handler (legacy render) or on non-rejected cards', () => {
    render(<SlCard m={{ ...base, status: 'rejected' }} />);
    expect(container.querySelector('.sl-card-actions')).toBeNull();
    const onReopen = vi.fn();
    render(<SlCard m={base} onReopen={onReopen} />);
    expect(container.querySelector('.sl-reopen')).toBeNull();
    render(
      <SlCard m={{ ...base, status: 'accepted' }} onReopen={onReopen} onRequeue={vi.fn()} />,
    );
    expect(container.querySelector('.sl-reopen')).toBeNull();
  });
});

describe('reopenMapping transport (requeue idiom, task 2.4)', () => {
  it('test_reopen_posts_and_reloads: POST /reopen, silent reconcile, success toast', async () => {
    render(<ToastRegion />);
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      mappings: [{ ...base, status: 'rejected' }],
    });
    // The route returns the updated ROW (status flipped to pending).
    const fetchMock = vi.fn(async (url: string) =>
      url === '/api/proposed-mappings/m-1/reopen'
        ? proposalResponse({ ...base, status: 'pending', review_reason: 're-opened by steward' })
        : proposalResponse({ mappings: [{ ...base, status: 'pending' }] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      await reopenMapping('m-1');
      await new Promise((resolve) => setTimeout(resolve, 0)); // flush the un-awaited reconcile
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/proposed-mappings/m-1/reopen',
      expect.objectContaining({ method: 'POST' }),
    );
    // Silent reconcile replaced the store — the row is back in pending.
    expect(fetchMock.mock.calls.map((c) => c[0])).toContain('/api/proposed-mappings?status=pending');
    expect(proposalsStore.get()).toMatchObject({
      phase: 'ready',
      mappings: [{ mapping_id: 'm-1', status: 'pending' }],
    });
    const toast = container.querySelector('[data-toast-kind="success"]');
    expect(toast?.textContent).toContain(
      'Re-opened for review — the proposal is back in the pending queue',
    );
  });

  it('failed POST (400 non-rejected) renders the urgent error toast and skips the reload', async () => {
    render(<ToastRegion />);
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        proposalResponse({ error: 'only rejected proposals can be re-opened' }, 400),
      );
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      await reopenMapping('m-1');
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const toast = container.querySelector('[data-toast-kind="error"]');
    expect(toast?.textContent).toContain('Re-open failed: only rejected proposals can be re-opened');
    expect(toast?.closest('[role="alert"]')).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1); // no reconcile after a failure
    expect(container.querySelector('[data-toast-kind="success"]')).toBeNull();
  });
});

/* ── Task 2.2: advisory likely-low-value badge (server-computed verdict) ── */

describe('SlCard: likely-low-value badge (advisory, server-computed)', () => {
  it('renders the badge on a flagged row with the joined flags as the title', () => {
    render(
      <SlCard
        m={{
          ...base,
          likely_low_value: true,
          degeneracy: ['equal_literal_echo', 'same_system_same_field'],
        }}
      />,
    );
    const badge = container.querySelector('.sl-card-head .sl-low-value')!;
    expect(badge).not.toBeNull();
    expect(badge.textContent).toBe('likely low-value');
    expect(badge.getAttribute('title')).toBe('equal_literal_echo, same_system_same_field');
  });

  it('absent when the key is absent (clean row) and when explicitly false', () => {
    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-low-value')).toBeNull();
    render(<SlCard m={{ ...base, likely_low_value: false, degeneracy: [] }} />);
    expect(container.querySelector('.sl-low-value')).toBeNull();
  });

  it('advisory only: Accept stays enabled and clickable on a badged row', () => {
    const onAccept = vi.fn();
    render(
      <SlCard
        m={{ ...base, likely_low_value: true, degeneracy: ['equal_literal_echo'] }}
        onAccept={onAccept}
      />,
    );
    expect(container.querySelector('.sl-low-value')).not.toBeNull();
    const accept = container.querySelector<HTMLButtonElement>('.sl-accept')!;
    expect(accept.disabled).toBe(false);
    expect(accept.getAttribute('aria-disabled')).toBeNull();
    click(accept);
    expect(onAccept).toHaveBeenCalledWith('m-1');
    // Reject and Edit are also untouched.
    expect(container.querySelector('.sl-reject')).not.toBeNull();
    expect(container.querySelector('.sl-edit')).not.toBeNull();
  });
});
