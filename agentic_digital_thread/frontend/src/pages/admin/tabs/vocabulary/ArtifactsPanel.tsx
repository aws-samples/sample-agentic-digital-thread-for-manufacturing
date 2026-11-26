// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Value-mapping artifact surface (W4.1, ADR-0050) — the existing self-learning
 * proposal rows filtered to proposed_type=value_mapping, rendered with the
 * IMPORTED SlCards components (single card implementation — provenance labels,
 * usage badge, schema-verdict gating all come from there), plus an "Assert
 * value mapping" form over the EXISTING open POST
 * /api/proposed-mappings/user-define route (human origination).
 *
 * Steward gate (ADR-0050): accept/reject are admin-only on the server (403
 * otherwise). The UI mirrors that: for non-admins no accept/reject handlers
 * are wired AND the imported card's action row is suppressed with a
 * panel-scoped stylesheet (SlCard is imported, not re-implemented, so it has
 * no "read-only" prop — the scoped rule keeps the one card implementation).
 * Edit lives on the dashboard Self-Learning panel, not here — hidden for all.
 *
 * IdentityRule + AuthorityRule surfaces are EXPLICIT FOLLOW-UPS (banner only).
 * JSX only — no HTML-string sinks (repo sink policy). */
import { useState, type CSSProperties, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import type { UserDefinedMappingRequest } from '../../../../api/types/proposals';
import { useAuth } from '../../../../contexts/AuthContext';
import {
  mappingsForStatus,
  PROPOSAL_STATUS_FILTERS,
  SlCard,
  validMappings,
} from '../../../../views/architecture/SlCards';
import type { ProposalStatusFilter } from '../../../../views/architecture/proposals';
// The sl-card visual contract lives in dashboard.css (class-scoped rules only —
// no global selectors); the admin entry does not load it, so this panel does.
import '../../../../styles/dashboard.css';
import {
  acceptProposalMutationOptions,
  assertValueMappingMutationOptions,
  rejectProposalMutationOptions,
  valueMappingProposalsQueryOptions,
} from '../../admin-queries';

const h3Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 };
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const bannerStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.5rem 0.75rem',
  marginBottom: '1rem',
};
const inputStyle: CSSProperties = {
  padding: '0.35rem 0.5rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.78rem',
  boxSizing: 'border-box',
  width: '100%',
};
const labelStyle: CSSProperties = {
  display: 'block',
  fontSize: '0.68rem',
  color: 'var(--muted)',
  marginBottom: '0.2rem',
};
const saveBtnStyle: CSSProperties = {
  background: 'var(--accent)',
  color: '#000',
  fontWeight: 600,
  padding: '0.4rem 1rem',
  border: 'none',
  borderRadius: 4,
  fontSize: '0.8rem',
  cursor: 'pointer',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const filterBtnStyle = (active: boolean): CSSProperties => ({
  background: active ? 'var(--accent)' : 'var(--surface2)',
  color: active ? '#000' : 'var(--text2)',
  fontWeight: active ? 600 : 400,
  padding: '0.25rem 0.75rem',
  border: '1px solid var(--border)',
  borderRadius: 4,
  fontSize: '0.72rem',
  cursor: 'pointer',
});

// Panel-scoped rules over the imported cards (SlCard has no read-only prop):
// Edit is a dashboard affordance — hidden here for everyone; the accept/reject
// row is additionally suppressed for non-admins, mirroring the server-side
// steward gate. Static string constants — never server-derived.
const PANEL_CSS = '#vocab-artifacts-panel .sl-edit{display:none;}';
const READONLY_CSS = '#vocab-artifacts-panel .sl-card-actions{display:none;}';

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

interface ColoredStatus {
  text: string;
  color?: string;
}

const EMPTY_FORM = {
  from_system: '',
  from_field: '',
  from_value: '',
  to_system: '',
  to_field: '',
  to_value: '',
  concept: '',
  description: '',
};

type AssertFormState = typeof EMPTY_FORM;

/** The documented POST /api/proposed-mappings/user-define body: the seven
 * required fields + optional description; proposed_type is pinned to
 * value_mapping (this surface manages value mappings only). */
export function toUserDefineBody(form: AssertFormState): UserDefinedMappingRequest {
  return {
    from_system: form.from_system.trim(),
    from_field: form.from_field.trim(),
    from_value: form.from_value.trim(),
    to_system: form.to_system.trim(),
    to_field: form.to_field.trim(),
    to_value: form.to_value.trim(),
    concept: form.concept.trim(),
    proposed_type: 'value_mapping',
    description: form.description.trim(),
  };
}

/** Client-side required-field check mirroring the route's 400 (fast feedback). */
export function missingRequiredFields(body: UserDefinedMappingRequest): string[] {
  return (
    ['from_system', 'from_field', 'from_value', 'to_system', 'to_field', 'to_value', 'concept'] as const
  ).filter((key) => !body[key]);
}

const ASSERT_FIELDS: ReadonlyArray<{ key: keyof AssertFormState; label: string }> = [
  { key: 'from_system', label: 'From system' },
  { key: 'from_field', label: 'From field (table.column)' },
  { key: 'from_value', label: 'From value' },
  { key: 'to_system', label: 'To system' },
  { key: 'to_field', label: 'To field (table.column)' },
  { key: 'to_value', label: 'To value' },
  { key: 'concept', label: 'Concept' },
  { key: 'description', label: 'Description (optional)' },
];

export default function ArtifactsPanel() {
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  const queryClient = useQueryClient();

  const proposalsQuery = useQuery(valueMappingProposalsQueryOptions());
  const acceptMutation = useMutation(acceptProposalMutationOptions(queryClient));
  const rejectMutation = useMutation(rejectProposalMutationOptions(queryClient));
  const assertMutation = useMutation(assertValueMappingMutationOptions(queryClient));

  const [statusFilter, setStatusFilter] = useState<ProposalStatusFilter>('pending');
  const [actionStatus, setActionStatus] = useState<ColoredStatus>({ text: '' });
  const [form, setForm] = useState<AssertFormState>(EMPTY_FORM);
  const [assertStatus, setAssertStatus] = useState<ColoredStatus>({ text: '' });

  const mappings = validMappings(proposalsQuery.data ?? []);
  const visible = mappingsForStatus(mappings, statusFilter);

  const decide = async (verb: 'accept' | 'reject', mappingId: string) => {
    setActionStatus({ text: verb === 'accept' ? 'Accepting…' : 'Rejecting…', color: 'var(--muted)' });
    try {
      await (verb === 'accept' ? acceptMutation : rejectMutation).mutateAsync(mappingId);
      setActionStatus({
        text: verb === 'accept' ? '✓ Accepted — saved to the knowledge graph' : '✗ Rejected',
        color: 'var(--green, #4ade80)',
      });
    } catch (error) {
      // A 403 here means the steward gate refused (session lost admin) —
      // surface the server's message verbatim, like every other error.
      setActionStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  const submitAssert = async (event: FormEvent) => {
    event.preventDefault();
    const body = toUserDefineBody(form);
    const missing = missingRequiredFields(body);
    if (missing.length) {
      setAssertStatus({ text: 'Fill in: ' + missing.join(', '), color: 'var(--red, #f87171)' });
      return;
    }
    setAssertStatus({ text: 'Saving…', color: 'var(--muted)' });
    try {
      const result = await assertMutation.mutateAsync(body);
      setForm(EMPTY_FORM);
      setAssertStatus(
        result.status === 'pending'
          ? {
              text: 'Sent for review. A steward has to accept it before the knowledge graph changes.',
              color: 'var(--green, #4ade80)',
            }
          : {
              text: '✓ Saved. The value mapping was added to the knowledge graph',
              color: 'var(--green, #4ade80)',
            },
      );
    } catch (error) {
      setAssertStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  return (
    <div id="vocab-artifacts-panel">
      <style>{isAdmin ? PANEL_CSS : PANEL_CSS + READONLY_CSS}</style>

      <h3 style={h3Style}>Value mappings</h3>
      <p style={sectionHelpStyle}>
        Cross-system value translations — discovered by the agents or asserted by people — flow
        through the same propose-and-accept lifecycle. Accepting or rejecting a proposal is a
        steward (admin) action. Anyone can assert a mapping they know to be true: a steward's
        assertion applies at once, and anyone else's waits in the pending list for a steward.
      </p>

      <div id="vocab-artifacts-followup" role="note" style={bannerStyle}>
        Identity-rule and authority-rule management surfaces are planned follow-ups — this panel
        manages value mappings and query guidance today.
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.75rem' }}>
        {PROPOSAL_STATUS_FILTERS.map((filter) => (
          <button
            key={filter.value}
            type="button"
            className={`vocab-vm-filter${statusFilter === filter.value ? ' active' : ''}`}
            data-status={filter.value}
            style={filterBtnStyle(statusFilter === filter.value)}
            onClick={() => setStatusFilter(filter.value)}
          >
            {filter.label}
          </button>
        ))}
        <span
          id="vocab-vm-action-status"
          style={{ ...statusStyle, color: actionStatus.color ?? statusStyle.color, marginLeft: 'auto' }}
        >
          {actionStatus.text}
        </span>
      </div>

      {proposalsQuery.isPending ? (
        <div style={statusStyle}>Loading value mappings…</div>
      ) : proposalsQuery.isError ? (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
          Value mappings unavailable: {errorDetail(proposalsQuery.error)}{' '}
          <button type="button" onClick={() => void proposalsQuery.refetch()}>
            Retry
          </button>
        </div>
      ) : visible.length === 0 ? (
        <div className="vocab-vm-empty" style={statusStyle}>
          No {statusFilter === 'superseded' ? 'historical' : statusFilter} value mappings.
        </div>
      ) : (
        <div id="vocab-vm-cards" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
          {visible.map((mapping) => (
            <SlCard
              key={mapping.mapping_id}
              m={mapping}
              onAccept={isAdmin ? (id) => void decide('accept', id) : undefined}
              onReject={isAdmin ? (id) => void decide('reject', id) : undefined}
            />
          ))}
        </div>
      )}

      <h3 style={{ ...h3Style, marginTop: '1.5rem' }}>Assert a value mapping</h3>
      <p style={sectionHelpStyle}>
        Record a value translation you know to be true — for example that one system&apos;s status
        code means the same thing as another system&apos;s label. It is applied to the knowledge
        graph immediately with human provenance, superseding any open agent proposal on the same
        subject.
      </p>
      <form id="vocab-vm-assert-form" onSubmit={(e) => void submitAssert(e)}>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            gap: '0.5rem',
            marginBottom: '0.75rem',
          }}
        >
          {ASSERT_FIELDS.map(({ key, label }) => (
            <label key={key} style={{ minWidth: 0 }}>
              <span style={labelStyle}>{label}</span>
              <input
                id={`vocab-vm-${key.replace(/_/g, '-')}`}
                type="text"
                style={inputStyle}
                value={form[key]}
                onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
              />
            </label>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          <button id="vocab-vm-assert-btn" type="submit" style={saveBtnStyle} disabled={assertMutation.isPending}>
            {assertMutation.isPending ? 'Saving…' : 'Assert value mapping'}
          </button>
          <span
            id="vocab-vm-assert-status"
            role={assertStatus.color === 'var(--red, #f87171)' ? 'alert' : undefined}
            style={{ ...statusStyle, color: assertStatus.color ?? statusStyle.color }}
          >
            {assertStatus.text}
          </span>
        </div>
      </form>
    </div>
  );
}
