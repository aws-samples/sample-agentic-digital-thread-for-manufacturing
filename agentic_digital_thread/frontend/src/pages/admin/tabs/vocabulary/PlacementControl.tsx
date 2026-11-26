// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';

import type { ContextPlacementResult } from '../../../../api/types/proposals';
import { ConfirmDialog } from '../../../../shared/ui/ConfirmDialog';
import {
  contextPlacementJobQueryOptions,
  startContextPlacementMutationOptions,
  type GuidanceSystemWithBase,
} from '../../guidance-queries';
import {
  actionStyle,
  AMBER,
  AuthorityTag,
  compactStyle,
  MONO,
  panelStyle,
  RED,
  systemDisplayLabel,
  useSystemDisplayNames,
} from './guidance-stability-ui';

function skipReasonCopy(reason: string): string {
  if (reason === 'context-unavailable') return 'Business context is unavailable.';
  if (reason === 'schema-unavailable') return 'Schema is unavailable.';
  return 'Required source data is unavailable.';
}

function PlacementTags({ running, failed, result }: {
  running: boolean;
  failed: boolean;
  result: ContextPlacementResult | null;
}) {
  const skipped = result?.skipped.length ?? 0;
  const zero = !!result && result.findings === 0 && skipped === 0;
  return (
    <>
      {running && <AuthorityTag state="PENDING" />}
      {failed && <AuthorityTag state="UNAVAILABLE" />}
      {!!result && result.findings > 0 && <AuthorityTag state="FOUND" />}
      {!!result && result.filed > 0 && <AuthorityTag state="PENDING" />}
      {skipped > 0 && <AuthorityTag state="UNAVAILABLE" />}
      {zero && <AuthorityTag state="ZERO" />}
    </>
  );
}

function PlacementResultView({ result, names }: {
  result: ContextPlacementResult;
  names: ReadonlyMap<string, string>;
}) {
  const zero = result.findings === 0 && result.skipped.length === 0;
  return (
    <div className="context-placement-result" style={{ ...compactStyle, marginTop: '0.45rem' }}>
      <div style={{ fontFamily: MONO }}>
        systems {result.systems_checked} · terms {result.terms_checked} · findings {result.findings} · filed {result.filed} · resolved {result.resolved_swept}
      </div>
      {result.findings > 0 && <div><AuthorityTag state="FOUND" /> Found — {result.findings} placement finding{result.findings === 1 ? '' : 's'}.</div>}
      {zero && <div>None — the completed check found no placement findings.</div>}
      {result.filed > 0 && <div><AuthorityTag state="PENDING" /> {result.filed} proposal{result.filed === 1 ? '' : 's'} filed, not applied. <a href="/#architecture">Open filed proposals</a></div>}
      {result.skipped.map((item, index) => (
        <div className="context-placement-skip" key={`${item.system_id}-${index}`}>
          <AuthorityTag state="UNAVAILABLE" /> {systemDisplayLabel(item.system_id, names)}: {skipReasonCopy(item.reason)}
        </div>
      ))}
      {result.truncated && <div style={{ color: AMBER }}>Result was truncated by server bounds.</div>}
    </div>
  );
}

function PlacementForm({ ids, names, systemId, busy, onSystem, onOpen }: {
  ids: string[];
  names: ReadonlyMap<string, string>;
  systemId: string;
  busy: boolean;
  onSystem: (id: string) => void;
  onOpen: () => void;
}) {
  return (
    <>
      <label style={{ ...compactStyle, display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
        Selected system
        <select aria-label="System for context placement check" value={systemId} onChange={(event) => onSystem(event.target.value)}>
          {ids.map((id) => <option key={id} value={id}>{systemDisplayLabel(id, names)}</option>)}
        </select>
      </label>
      <button type="button" style={{ ...actionStyle, marginTop: '0.45rem' }} disabled={!systemId || busy} onClick={onOpen}>
        Run scoped check
      </button>
    </>
  );
}

export function PlacementControl({ systems, enabled }: { systems: GuidanceSystemWithBase[]; enabled: boolean }) {
  const ids = systems.map((system) => system.system_id || '').filter(Boolean);
  const names = useSystemDisplayNames();
  const [systemId, setSystemId] = useState(ids[0] || '');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [jobId, setJobId] = useState('');
  const start = useMutation(startContextPlacementMutationOptions());
  const job = useQuery(contextPlacementJobQueryOptions(jobId, enabled && !!jobId));
  useEffect(() => {
    if (!systemId && ids[0]) setSystemId(ids[0]);
  }, [ids, systemId]);
  if (!enabled) return null;
  const running = start.isPending || job.data?.status === 'running';
  const failed = start.isError || job.data?.status === 'error';
  const result = job.data?.status === 'done' ? job.data.result : null;
  const launch = async () => {
    setConfirmOpen(false);
    setJobId('');
    try {
      const started = await start.mutateAsync({ systemId });
      setJobId(started.job_id);
    } catch {
      // Fixed failure copy below; no backend text is rendered.
    }
  };
  return (
    <section className="context-placement-control" aria-label="Context placement check" style={panelStyle}>
      <div style={{ display: 'flex', gap: '0.45rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: '0.78rem' }}>Context placement check</strong>
        <PlacementTags running={running} failed={failed} result={result} />
      </div>
      <p style={{ ...compactStyle, margin: '0.3rem 0 0.5rem' }}>Deterministically checks one selected system against other onboarded surfaces. It may file pending proposals; it never repairs or applies mappings.</p>
      <PlacementForm ids={ids} names={names} systemId={systemId} busy={running} onSystem={setSystemId} onOpen={() => setConfirmOpen(true)} />
      <ConfirmDialog open={confirmOpen} title="Run context placement check?" confirmLabel="Run check" onCancel={() => setConfirmOpen(false)} onConfirm={() => void launch()}>
        This reads context and schema for <strong>{systemDisplayLabel(systemId, names)}</strong> and related onboarded systems. Findings can create pending proposals for steward review; nothing is applied.
      </ConfirmDialog>
      {running && <div role="status" style={{ ...compactStyle, marginTop: '0.4rem' }}>Pending — job {jobId || 'launching'} is still running.</div>}
      {start.isError && <div role="alert" style={{ ...compactStyle, color: RED, marginTop: '0.4rem' }}>Unavailable — the context placement check could not start.</div>}
      {job.data?.status === 'error' && <div role="alert" style={{ ...compactStyle, color: RED, marginTop: '0.4rem' }}>Unavailable — the placement check did not complete.</div>}
      {result && <PlacementResultView result={result} names={names} />}
    </section>
  );
}
